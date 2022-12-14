from uuid import UUID
from datetime import datetime, date
from typing import List

import csv
from dateutil import utils
from more_itertools import one
from fastapi.encoders import jsonable_encoder

from gql import gql

from raclients.graph.client import GraphQLClient
from ra_utils.job_settings import JobSettings
import pandas as pd
from reports.os2mo_engagements_reports import config


def established_person_engagements(settings: JobSettings) -> dict:
    """
    Reading all of active engagements with the persons uuid(s) engagement start date.

    args:
    Settings for GrapQLClient params to execute the graphQL query.

    returns:
    An object of active engagements with persons uuid(s) and dates on persons validity from.
    """

    graphql_query = gql(
        """query EstablishedEngagements ($engagement_date_to_query_from: DateTime) {
             engagements(from_date: $engagement_date_to_query_from) {
               objects {
                 employee_uuid
                 validity {
                   from
                 }
               }
             }
           }
        """
    )
    variables = {"engagement_date_to_query_from": date.today().isoformat()}
    with GraphQLClient(
        url=f"{settings.mora_base}/graphql/v3",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    ) as session:
        response = session.execute(
            graphql_query, variable_values=jsonable_encoder(variables)
        )
    return response


def get_filtered_dates_from_today(gql_query_response: dict) -> List[UUID]:
    """
    Applying filter to get uuids of persons which have started engagements as of today.

    args:
    A graphQL query with payload of active engagements.

    returns:
    A list of eligible uuid(s) of the person(s) that have started their engagements as of today.
    """

    def filter_today(obj):
        return (
            datetime.fromisoformat(one(obj["objects"])["validity"]["from"]).replace(
                tzinfo=None
            )
            == utils.today()
        )

    filtered_dates = filter(
        lambda startdate: one(startdate["objects"])["validity"]["from"],
        gql_query_response["engagements"],
    )

    filtered_engagements_by_today_date = filter(filter_today, filtered_dates)

    extracted_uuids = [
        one(person_uuids["objects"])["employee_uuid"]
        for person_uuids in filtered_engagements_by_today_date
    ]

    return extracted_uuids


def retrieve_address_types_uuids(settings: JobSettings) -> dict:
    """
    Reading all types of addresses.

    args:
    Settings for GrapQLClient params to execute the graphQL query.

    returns:
    An object containing uuid and scope on all active addresses.
    """
    graphql_query = gql(
        """query GetAddressTypes {
             addresses {
               objects {
                 address_type {
                   scope
                   uuid
                 }
               }
             }
           }
        """
    )
    with GraphQLClient(
        url=f"{settings.mora_base}/graphql/v3",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    ) as session:
        response = session.execute(graphql_query)
    return response


def get_email_address_type_uuid_from_gql(gql_query_dict: dict) -> list:
    """
    Filtering through address types to retrieve uuids of addresses with scope of email.

    args:
    A graphQL query with payload on address types.

    returns:
    A list of all uuids with scope of email.
    """
    filtered_email_address_uuids = filter(
        lambda address_type: one(address_type["objects"])["address_type"]["scope"]
        == "EMAIL",
        gql_query_dict["addresses"],
    )
    extracted_email_uuids = [
        one(obj["objects"])["address_type"]["uuid"]
        for obj in filtered_email_address_uuids
    ]

    return extracted_email_uuids


def persons_details_from_engagement(
    settings: JobSettings, uuidlist: List[UUID], address_type_uuid_list: List[UUID]
) -> dict:
    """
    Retrieving all desired details on the person from the active filtered engagements.

    args:
    Settings for GrapQLClient params to execute the graphQL query. List of person uuids
    to query on. List of address type uuids to query on.

    returns:
    A dict with all desired details on person by active engagement.
    """
    graphql_query = gql(
        """query PersonEngagementDetails ($uuidlist: [UUID!], $email_uuid_list: [UUID!]) {
             employees(uuids: $uuidlist) {
               objects {
                 cpr_no
                 name
                 user_key
                 uuid
                 addresses(address_types: $email_uuid_list) {
                   name
                 }
                 engagements {
                   org_unit {
                   name
                 }
                   validity {
                     from
                   }
                }
               }
             }
           }
        """
    )
    variables = {"uuidlist": uuidlist, "email_uuid_list": address_type_uuid_list}
    with GraphQLClient(
        url=f"{settings.mora_base}/graphql/v3",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    ) as session:
        response = session.execute(
            graphql_query, variable_values=jsonable_encoder(variables)
        )

    return response


def convert_start_engagement_data_to_csv(dict_data):
    """
    Mapping fields of payload from a new engagement to CSV format.

    args:
    A dictionary consisting of a payload with the fields of Person(s) name(s), Person(s) uuid(s),
    User key(s), Organisation unit name(s), CPR number(s), Email(s), Engagement date(s).

    returns:
    A CSV format of fields properly mapped to their representative fields.
    """
    out = []
    for employee in dict_data["employees"]:
        for obj in employee["objects"]:
            out.append(
                {
                    "Personens navn": obj["name"],
                    "Personens UUID": obj["uuid"],
                    "Ansættelsessted": obj["engagements"][0]["org_unit"][0]["name"],
                    "Ansættelsesdato": obj["engagements"][0]["validity"]["from"],
                    "CPR": obj["cpr_no"],
                    "Email": obj["addresses"][0]["name"],
                    "Brugervendtnøgle": obj["user_key"],
                }
            )

    return pd.DataFrame(out).to_csv(
        index=False, header=True, sep=";", quoting=csv.QUOTE_ALL
    )


def write_file(contents_of_file, path_to_file):
    """
    A generic way of writing any file.

    args:
    Data with the contents wished to be written. A path to where the file is wanted to be stored.

    returns:
    A written file with the desired contents.
    """
    with open(path_to_file, "w+", encoding="utf-8") as file:
        file.write(contents_of_file)


def display_engagements(settings: JobSettings) -> None:
    """
    Calls upon GraphQL queries and various filters defined in this module, to bring together an
    object with all the details wanted on an engagement and on a person.
    These details are written as CSV format and stored in a desired path.
    """
    payload_of_active_engagements_objects = established_person_engagements(settings)

    address_type_uuids_and_scopes = retrieve_address_types_uuids(settings)

    list_of_email_uuids = get_email_address_type_uuid_from_gql(
        address_type_uuids_and_scopes
    )

    list_of_person_uuids = get_filtered_dates_from_today(
        payload_of_active_engagements_objects
    )

    detailed_engagements = persons_details_from_engagement(
        settings, list_of_person_uuids, list_of_email_uuids
    )

    data_in_csv = convert_start_engagement_data_to_csv(detailed_engagements)

    write_file(data_in_csv, "reports/os2mo_engagements_reports/testing_csv.csv")


def main() -> None:
    settings = config.get_engagement_settings()
    settings.start_logging_based_on_settings()
    display_engagements(settings)


if __name__ == "__main__":
    main()
