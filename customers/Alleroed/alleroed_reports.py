# SPDX-FileCopyrightText: 2023 Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""
This module contains the various reports run for the customer Alleroed municipality.

Actualstate report - from reports/query_actualstate.py

Common query reports - from exporters/ballerup.py

Employee phonebook report - from customers/Frederikshavn/frederikshavn_employee_phonebook.py
"""

import os
import time
from typing import Any
from uuid import UUID

import pandas as pd
from anytree import PreOrderIter
from fastramqpi.ra_utils.job_settings import JobSettings
from fastramqpi.raclients.graph.client import GraphQLClient
from fastramqpi.raclients.upload import file_uploader
from fastramqpi.raclients.upload import run_report_and_upload
from gql import gql
from gql.client import SyncClientSession
from more_itertools import first
from more_itertools import one
from more_itertools import prepend
from sqlalchemy import or_
from sqlalchemy import select

from exporters import common_queries as cq
from exporters.sql_export.sql_table_defs import WAdresse as Adresse
from exporters.sql_export.sql_table_defs import WBruger as Bruger
from exporters.sql_export.sql_table_defs import WEngagement as Engagement
from exporters.sql_export.sql_table_defs import WEnhed as Enhed
from integrations.mora_helper import MoraHelper
from reports.query_actualstate import expand_org_path
from reports.query_actualstate import run_report
from reports.query_actualstate import set_of_org_units

MORA_BASE = os.environ.get("MORA_BASE", "http://localhost:5000")


def get_managers_for_export(
    gql_session: SyncClientSession, org_uuid: list[UUID]
) -> list[dict[str, Any]]:
    """
    Makes a GraphQL call, to retrieve an Organisation Units' manager(s)
    and all relevant details.

    :args:
    GraphQL session
    Organisation Unit uuid.

    :returns:
    A list with a payload of manager object consisting of: responsibilities,
    persons name and addresses.

    :example:
    "[{'objects': [{'employee': [{'addresses': [{'address_type': {'scope': 'EMAIL'},
                                            'name': 'tracya@kolding.dk'},
                                           {'address_type': {'scope': 'DAR'},
                                            'name': 'Finmarken 94, 6000 '
                                                    'Kolding'},
                                           {'address_type': {'scope': 'PHONE'},
                                            'name': '67338448'}],
                             'name': 'Tracy Andersen'}],
               'responsibilities': [{'full_name': 'Personale: '
                                                  'ansættelse/afskedigelse'},
                                    {'full_name': 'Personale: Sygefravær'},
                                    {'full_name': 'Ansvar for bygninger og '
                                                  'arealer'}]}]}]"
    """

    graphql_query = gql(
        """
    query FindManagers($org_unit_uuid: [UUID!]) {
      managers(filter: { org_units: $org_unit_uuid }) {
        objects {
          validities {
            responsibilities {
              full_name
            }
            employee {
              name
              addresses {
                address_type {
                  scope
                }
                name
              }
            }
          }
        }
      }
    }
      """
    )
    response = gql_session.execute(
        graphql_query, variable_values={"org_unit_uuid": org_uuid}
    )

    return response["managers"]["objects"]


def setup_alleroed_gql_client(
    settings: JobSettings,
) -> GraphQLClient:
    return GraphQLClient(
        url=f"{settings.mora_base}/graphql/v22",
        client_id=settings.client_id,
        client_secret=settings.client_secret,  # type: ignore
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,  # type: ignore
        sync=True,
        httpx_client_kwargs={"timeout": None},
    )


def export_udvalg(mh, nodes, filename, fieldnames, org_types):
    """Traverses a tree of OUs, find members of 'udvalg'
    :param mh: Instance of MoraHelper to do the actual work
    :param nodes: The nodes of the OU tree
    :param fieldnames: Fieldnames for structur of the 'udvalg'
    :param org_types: Org types belong to this kind of 'udvalg'
    """
    fieldnames = fieldnames + [
        "Fornavn",
        "Efternavn",
        "Brugernavn",
        "Post",
        "Leder",
        "E-mail",
        "Telefon",
    ]
    rows = []
    for node in PreOrderIter(nodes["root"]):
        path_dict = mh._create_path_dict(fieldnames, node, org_types)
        if not path_dict:
            continue
        employees = mh.read_organisation_people(
            node.name, "association", split_name=True
        )
        for uuid, employee in employees.items():
            row = {}
            address = mh.read_user_address(uuid, username=True)
            mh.read_user_manager_status(uuid)
            if mh.read_user_manager_status(uuid):
                row["Leder"] = "Ja"
            row.update(path_dict)  # Path
            row.update(address)  # Brugernavn
            row.update(employee)  # Everything else
            rows.append(row)
    mh._write_csv(fieldnames, rows, filename)


def write_multiple_managers_from_graphql_payload(
    mh: MoraHelper, gql_session: SyncClientSession, name_of_file
) -> None:
    """
    This function will call upon GraphQL queries to retrieve necessary manager
    details for each wanted Organisation Unit and all of its children recursively.
    All details will then be written as a CSV formatted report on manager(s), with any
    desired details. Fields may be modified and added, as seen fit. As of now
    the following fields will be written:
    "root", "org", "sub org" (will keep adding sub orgs till the height of the root),
    "Ansvar", "Navn, "Telefon" and "E-mail.
    Multiple managers are supported.

    If no managers are found, the fields will be written with empty values.

    :args:
    MoraHelpers to find Organisation Units recursively.
    A GraphQL session to perform queries.
    """

    def get_email_from_address_object(employee: dict) -> str | None:  # type: ignore
        """
        Function for extracting the e-mail address of the manager.

        :args:
        A Manager response with an "addresses" object.

        :returns:
        The first e-mail address of the manager, if the filter applied is successful.
        Will return None, if no email was found.

        :example:
        "'benth@kolding.dk'"
        """
        if employee.get("validities")[0]["employee"] is not None:  # type: ignore
            filtered_email_address_object = list(
                filter(
                    lambda address_type: address_type["address_type"]["scope"]
                    == "EMAIL",
                    one(one(employee["validities"])["employee"])["addresses"],
                )
            )
            if filtered_email_address_object:
                return first(filtered_email_address_object)["name"]
        else:
            return None  # Empty E-mails.

    def get_phone_from_address_object(employee: dict) -> str | None:  # type: ignore
        """
        Function for extracting the phone number of the manager.

        :args:
        A Manager response with an "addresses" object.

        :returns:
        The first phone number of the manager, if the filter applied is successful.
        Will return None, if no phone number was found.

        :example:
        "'67338448'"
        """
        if employee.get("validities")[0]["employee"] is not None:  # type: ignore
            filtered_phone_address_object = list(
                filter(
                    lambda address_type: address_type["address_type"]["scope"]
                    == "PHONE",
                    one(one(employee["validities"])["employee"])["addresses"],
                )
            )
            if filtered_phone_address_object:
                return first(filtered_phone_address_object)["name"]
        else:
            return None  # Empty phones.

    def get_name_from_manager_object(employee: dict) -> str | None:
        """
        Function for extracting the name of the manager.

        :args:
        A Manager object.

        :returns:
        The name of the manager.

        :example:
        "'Bent Lindstrøm Hansen'"
        """
        if (
            employee.get("validities")
            and employee["validities"][0].get("employee")
            and employee["validities"][0]["employee"][0].get("name")
        ):
            return employee["validities"][0]["employee"][0]["name"]
        else:
            return None

    def get_responsibilities_from_manager_object(
        manager_responsibility: dict,
    ) -> str | None:
        """
        This function will extract the primary responsibility of a manager.
        According to old logic from MoraHelpers, the main responsibility is
        "Personale: ansættelse/afskedigelse" - if this was not found, the
        primary responsibility would default to the last element in the list.

        :args:
        A Manager response with a "responsibilities" object.

        :returns:
        The name of the primary responsibility, if the filter applied is successful.
        Will return the last element in the "responsibilities" list, if filter did
        not find primary responsibility.
        Will return None, if manager has no responsibilities.

        :example:
        "'Personale: ansættelse/afskedigelse'" if successful.

        "'Personale: MUS-kompetence'" if filter was not successful:
        """
        responsibilities = one(manager_responsibility["validities"])["responsibilities"]
        filtered_responsibility_object = list(
            filter(
                lambda primary_responsibility: primary_responsibility["full_name"]
                == "Personale: ansættelse/afskedigelse",  # According to MoraHelpers.
                responsibilities,
            )
        )
        if filtered_responsibility_object:
            return first(filtered_responsibility_object)["full_name"]
        elif responsibilities:  # Default in MoraHelpers.
            return responsibilities[-1]["full_name"]
        else:  # In case of no responsibilities found.
            return None

    fieldnames = mh._create_fieldnames(nodes)
    fieldnames += ["Ansvar", "Navn", "Telefon", "E-mail"]
    rows = []
    for node in PreOrderIter(
        nodes["root"]
    ):  # Finding Organisation Unit nodes recursively.
        list_of_manager_object_data = get_managers_for_export(gql_session, node.name)
        if list_of_manager_object_data:  # If managers are found.
            for manager in list_of_manager_object_data:
                row = {}
                # Finding all the OUs children and writing a "sub org" field for each child node.
                root_org_and_all_its_children = mh._create_path_dict(fieldnames, node)
                row.update(root_org_and_all_its_children)
                row["Navn"] = get_name_from_manager_object(
                    manager
                )  # Name of the manager.
                row["Ansvar"] = get_responsibilities_from_manager_object(
                    manager
                )  # Responsibility.
                row["E-mail"] = get_email_from_address_object(
                    manager
                )  # Retrieving e-mail.
                row["Telefon"] = get_phone_from_address_object(
                    manager
                )  # Retrieving phone number.
                rows.append(row)
        if (
            not list_of_manager_object_data
        ):  # If not managers are found, write empty values to CSV.
            row = {}
            root_org_and_all_its_children = mh._create_path_dict(fieldnames, node)
            row.update(root_org_and_all_its_children)  # Path
            rows.append(row)

    mh._write_csv(fieldnames, rows, name_of_file)


def list_employees_for_phonebook(session, org_name: str) -> list:
    """
    Lists all employees in an organisation unit with relevant fields.

    Args:
        session: A SQLAlchemy session to make queries on.
        org_name: Name of the organisation unit to retrieve data from.

    Returns:
        A list of tuples with titles as first element and data on
        employees in subsequent tuples.

    Example:
        [
            ("Navn", "Mobil", "Telefon", "Enhed", "Stilling"),
            ("Fornavn Efternavn", 0123456789, "12345678",
                "Enhedsnavn", "Stillingsbetegnelse")
        ]
    """

    stmt = (
        select(
            Bruger.uuid,
            Bruger.fornavn + " " + Bruger.efternavn,
            Enhed.navn,
            Engagement.stillingsbetegnelse_titel,
            Adresse.adressetype_bvn,
            Adresse.værdi,
        )
        .join_from(Bruger, Engagement, Bruger.uuid == Engagement.bruger_uuid)
        .join_from(Engagement, Enhed, Engagement.enhed_uuid == Enhed.uuid)
        .join_from(
            Bruger,
            Adresse,
            Bruger.uuid == Adresse.bruger_uuid,
        )
        .where(
            or_(
                Adresse.adressetype_bvn == "MobilePhoneEmployee",
                Adresse.adressetype_bvn == "PhoneEmployee",
            ),
            or_(
                Adresse.synlighed_titel.is_(None),
                Adresse.synlighed_titel != "Hemmelig",
            ),
        )
        .order_by(Bruger.efternavn)
        .distinct()
    )

    with session:
        result = tuple(session.execute(stmt))

    phonebook = dict()  # type: ignore
    for user_uuid, name, unit, prof, addr_type, addr_value in result:
        key = (user_uuid, name, unit, prof)
        phone_numbers = phonebook.get(
            key, {"PhoneEmployee": "", "MobilePhoneEmployee": ""}
        )
        phone_numbers[addr_type] = addr_value
        phonebook[key] = phone_numbers

    rows = [("Navn", "Mobil nr.", "Telefon nr.", "Afdeling", "Stillingsbetegnelse")]
    return rows + [
        # The key tuple contains these values (user_uuid, name, unit, prof)
        (key[1], value["MobilePhoneEmployee"], value["PhoneEmployee"], key[2], key[3])
        for key, value in phonebook.items()
    ]


def list_alleroed_employees(session, org_name: str) -> list:
    """Lists all employees in organisation.

    Returns a list of tuples with titles as first element
    and data on employees in subsequent tuples. Example:
    [
        (Navn", "CPR", "Email", "Telefon", "Enhed", "Stilling"),
        ("Fornavn Efternavn", 0123456789,  "email@example.com", "12345678",
            "Enhedsnavn", "Stillingsbetegnelse")
    ]
    """
    alle_enheder = set_of_org_units(session, org_name)

    Emails = (
        session.query(Adresse.værdi, Adresse.bruger_uuid)
        .filter(
            Adresse.adressetype_bvn == "EmailEmployee",
            or_(
                Adresse.synlighed_titel.is_(None),
                Adresse.synlighed_titel != "Hemmelig",
            ),
        )
        .subquery()
    )
    Phonenr = (
        session.query(Adresse.værdi, Adresse.bruger_uuid)
        .filter(
            Adresse.adressetype_scope == "Telefon",
            or_(
                Adresse.synlighed_titel.is_(None),
                Adresse.synlighed_titel != "Hemmelig",
            ),
        )
        .subquery()
    )
    query = (
        session.query(
            Bruger.uuid,
            Bruger.fornavn + " " + Bruger.efternavn,
            Bruger.cpr,
            Emails.c.værdi,
            Phonenr.c.værdi,
            Enhed.navn,
            Enhed.organisatorisk_sti,
            Engagement.stillingsbetegnelse_titel,
        )
        .filter(
            Enhed.uuid == Engagement.enhed_uuid,
            Engagement.enhed_uuid.in_(alle_enheder),
            Engagement.bruger_uuid == Bruger.uuid,
        )
        .join(Emails, Emails.c.bruger_uuid == Bruger.uuid, isouter=True)
        .join(Phonenr, Phonenr.c.bruger_uuid == Bruger.uuid, isouter=True)
        .order_by(Bruger.efternavn)
    )
    data = query.all()
    data_df = pd.DataFrame(
        data,
        columns=[
            "UUID",
            "Navn",
            "CPR",
            "AD-Email",
            "AD-Telefonnummer",
            "Enhed",
            "Sti",
            "Stilling",
        ],
    )
    data_df = expand_org_path(data_df, "Sti")
    # Return data as a list of tuples with columns as the first element
    parsed_data = list(prepend(data_df.columns, data_df.to_records(index=False)))
    return parsed_data


if __name__ == "__main__":
    settings = JobSettings()
    settings.start_logging_based_on_settings()

    run_report_and_upload(
        settings,
        "Medarbejdertelefonbog.xlsx",
        run_report,
        list_employees_for_phonebook,
        "Medarbejdertelefonbog",  # type: ignore
        "Allerød",  # type: ignore
    )

    run_report_and_upload(
        settings,
        "Ansatte.xlsx",
        run_report,
        list_alleroed_employees,
        "Ansatte",  # type: ignore
        "Allerød",  # type: ignore
    )

    gql_client = setup_alleroed_gql_client(settings=settings)
    threaded_speedup = False
    t = time.time()

    mh = MoraHelper(hostname=MORA_BASE)

    org = mh.read_organisation()
    roots = mh.read_top_units(org)
    print(roots)
    for root in roots:
        if root["name"] == "Allerød":
            alleroed = root["uuid"]

    if threaded_speedup:
        cq.pre_cache_users(mh)
        print(f"Build cache: {time.time() - t}")

    nodes = mh.read_ou_tree(alleroed)
    print(f"Read nodes: {time.time() - t}s")

    with gql_client as session:
        print("Initiating a GraphQL session.")
        print("Retrieving queries to write from.")

        with file_uploader(settings, "Alle_lederfunktioner_os2mo.csv") as filename:
            write_multiple_managers_from_graphql_payload(mh, session, filename)
        print("Successfully wrote all necessary manager details to csv.")

    print(f"Alle ledere: {time.time() - t}s")

    with file_uploader(settings, "AlleBK-stilling-email_os2mo.csv") as filename:
        cq.export_all_employees(mh, nodes, filename)
    print("AlleBK-stilling-email: {}s".format(time.time() - t))

    with file_uploader(
        settings, "Alleroed_org_incl-medarbejdere_os2mo.csv"
    ) as filename:
        cq.export_orgs(mh, nodes, filename)
    print("Alleroed org incl medarbejdere: {}s".format(time.time() - t))

    with file_uploader(
        settings, "Adm-org-incl-start-og-stopdata-og-enhedstyper-os2mo.csv"
    ) as filename:
        cq.export_adm_org(mh, nodes, filename)
    print("Adm-org-incl-start-stop: {}s".format(time.time() - t))

    try:  # Handle possibility of this report failing, as has been the case.
        with file_uploader(settings, "teams-tilknyttede-os2mo.csv") as filename:
            cq.export_all_teams(mh, nodes, filename)
        print("Teams: {}s".format(time.time() - t))
    except ValueError as exc:
        print("Something went wrong:", exc.args[0])

    print("Export completed")
