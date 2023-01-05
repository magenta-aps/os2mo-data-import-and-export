from uuid import UUID
from datetime import datetime, date
from typing import List

import csv
from dateutil import utils
from more_itertools import one
from fastapi.encoders import jsonable_encoder

from gql import gql
from gql.client import SyncClientSession

import pandas as pd
from raclients.graph.client import GraphQLClient
from ra_utils.job_settings import JobSettings
from reports.os2mo_new_and_ended_engagement_reports import config
from reports.os2mo_new_and_ended_engagement_reports.config import EngagementSettings
from reports.os2mo_new_and_ended_engagement_reports.config import setup_gql_client
from reports.os2mo_new_and_ended_engagement_reports.config import get_engagement_settings


def gql_query_validity_field(
    validity_from: bool = False, validity_to: bool = False
) -> str:
    """GQL query to return to use as input, depending on what type of engagement is wanted."""
    if validity_from:
        return """query EstablishedEngagements ($engagement_date_to_query_from: DateTime) {
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

    if validity_to:
        return """query EstablishedEngagements ($engagement_date_to_query_from: DateTime) {
             engagements(from_date: $engagement_date_to_query_from) {
               objects {
                 employee_uuid
                 validity {
                   to
                 }
               }
             }
           }
        """


def gql_query_persons_details_to_display(
    started_engagement: bool = False, ended_engagement: bool = False
) -> str:
    """GQL query to return to use as input, depending on what type of engagement is wanted."""
    if started_engagement:
        return """query PersonEngagementDetails ($uuidlist: [UUID!], $email_uuid_list: [UUID!]) {
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

    if ended_engagement:
        return """query PersonEngagementDetails ($uuidlist: [UUID!], $email_uuid_list: [UUID!]) {
             employees(uuids: $uuidlist) {
               objects {
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
                     to
                   }
                }
               }
             }
           }
        """


def established_person_engagements(
    gql_session: SyncClientSession,

    validity_field_from: bool = None,
    validity_field_to: bool = None,
) -> dict:
    """
    Reading all of active engagements with the persons uuid(s) engagement start date.

    args:
    Settings for GrapQLClient params to execute the graphQL query.

    Optional param of either "validity_field_from" or "validity_field_to" to
    specify what engagement validity to retrieve data of.

    returns:
    An object of active engagements with persons uuid(s) and dates on persons validity from.
    """
    if validity_field_from:
        graphql_query = gql(gql_query_validity_field(validity_from=True))

    elif validity_field_to:
        graphql_query = gql(gql_query_validity_field(validity_to=True))

    variables = {"engagement_date_to_query_from": date.today().isoformat()}
    # with GraphQLClient(
    #     url=f"{settings.mora_base}/graphql/v3",
    #     client_id=settings.client_id,
    #     client_secret=settings.client_secret,
    #     auth_realm=settings.auth_realm,
    #     auth_server=settings.auth_server,
    #     sync=True,
    #     httpx_client_kwargs={"timeout": None},
    # ) as session:
    #     response = session.execute(
    #         graphql_query, variable_values=jsonable_encoder(variables)
    #     )

    response = gql_session.execute(graphql_query, variable_values=jsonable_encoder(variables))

    return response


def get_filtered_engagements_for_started_today(gql_query_response: dict) -> List[UUID]:
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


def get_filtered_engagements_for_ended_today(gql_query_response: dict) -> List[UUID]:
    """
    Applying filter to get uuids of persons which have ended their engagements as of today.

    args:
    A graphQL query with payload of ended engagements.

    returns:
    A list of eligible uuid(s) of the person(s) that have ended their engagements as of today.
    """

    def filter_to_today(obj):
        return (
            datetime.fromisoformat(one(obj["objects"])["validity"]["to"]).replace(
                tzinfo=None
            )
            == utils.today()
        )

    filtered_dates = filter(
        lambda enddate: one(enddate["objects"])["validity"]["to"],
        gql_query_response["engagements"],
    )

    filtered_engagements_by_today_date = filter(filter_to_today, filtered_dates)

    extracted_uuids = [
        one(person_uuids["objects"])["employee_uuid"]
        for person_uuids in filtered_engagements_by_today_date
    ]

    return extracted_uuids


def retrieve_address_types_uuids(gql_session: SyncClientSession) -> dict:
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
    # with GraphQLClient(
    #     url=f"{settings.mora_base}/graphql/v3",
    #     client_id=settings.client_id,
    #     client_secret=settings.client_secret,
    #     auth_realm=settings.auth_realm,
    #     auth_server=settings.auth_server,
    #     sync=True,
    #     httpx_client_kwargs={"timeout": None},
    # ) as session:
    #     response = session.execute(graphql_query)

    response = gql_session.execute(graphql_query)

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
    gql_session: SyncClientSession,
    uuidlist: List[UUID],
    address_type_uuid_list: List[UUID],
    started_engagement_details: bool = False,
    ended_engagement_details: bool = False,
) -> dict:
    """
    Retrieving all desired details on the person from the filtered engagements.

    args:
    Settings for GrapQLClient params to execute the graphQL query. List of person uuids
    to query on. List of address type uuids to query on.

    Optional param of either "started_engagement_details" or "ended_engagement_details" to
    specify what type of engagement to retrieve details of.

    returns:
    A dict with all desired details on person by active engagement.
    """
    if started_engagement_details:
        graphql_query = gql(
            gql_query_persons_details_to_display(started_engagement=True)
        )

    if ended_engagement_details:
        graphql_query = gql(gql_query_persons_details_to_display(ended_engagement=True))

    variables = {"uuidlist": uuidlist, "email_uuid_list": address_type_uuid_list}
    # with GraphQLClient(
    #     url=f"{settings.mora_base}/graphql/v3",
    #     client_id=settings.client_id,
    #     client_secret=settings.client_secret,
    #     auth_realm=settings.auth_realm,
    #     auth_server=settings.auth_server,
    #     sync=True,
    #     httpx_client_kwargs={"timeout": None},
    # ) as session:
    #     response = session.execute(
    #         graphql_query, variable_values=jsonable_encoder(variables)
    #     )
    response = gql_session.execute(graphql_query, variable_values=jsonable_encoder(variables))
    return response


def convert_person_and_engagement_data_to_csv(
    dict_data, started: bool = False, ended: bool = False
):
    """
    Mapping fields of payload from engagement to CSV format.

    args:
    A dictionary consisting of a payload with the fields of Person(s) name(s), Person(s) uuid(s),
    User key(s), Organisation unit name(s), CPR number(s), Email(s), Engagement date(s).

    Optional param of either "started" or "ended" to specify what type of engagement to write
    details of.

    returns:
    A CSV format of fields properly mapped to their representative fields.
    """
    out = []
    if started:
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

    if ended:
        for employee in dict_data["employees"]:
            for obj in employee["objects"]:
                out.append(
                    {
                        "Personens navn": obj["name"],
                        "Personens UUID": obj["uuid"],
                        "Ansættelsessted": obj["engagements"][0]["org_unit"][0]["name"],
                        "Ansættelsesudløbsdato": obj["engagements"][0]["validity"][
                            "to"
                        ],
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


def display_engagements(gql_session: SyncClientSession) -> None:
    """
    Calls upon GraphQL queries and various filters defined in this module, to bring together an
    object with all the details wanted on an engagement and on a person.
    These details are written as CSV format and stored in a desired path.
    """

    # Pulling address types so email uuids can be found.
    address_type_uuids_and_scopes = retrieve_address_types_uuids(gql_session)

    # Finding email uuids.
    list_of_email_uuids = get_email_address_type_uuid_from_gql(
        address_type_uuids_and_scopes
    )

    # Getting active payload with validity field "from" engagements.
    payload_of_started_engagements_objects = established_person_engagements(
        gql_session, validity_field_from=True
    )
    # Getting engagements that have an end-date with validity field "to" engagements.
    payload_of_ended_engagements_objects = established_person_engagements(
        gql_session, validity_field_to=True
    )

    # Finding uuids of persons that have started new engagements.
    list_of_person_uuids_started_engagement = (
        get_filtered_engagements_for_started_today(
            payload_of_started_engagements_objects
        )
    )
    # Finding uuids of persons that have ended engagements.
    list_of_person_uuids_ended_engagements = get_filtered_engagements_for_ended_today(
        payload_of_ended_engagements_objects
    )

    # Retrieving details on person with new started engagement.
    details_of_started_engagements = persons_details_from_engagement(
        gql_session,
        list_of_person_uuids_started_engagement,
        list_of_email_uuids,
        started_engagement_details=True,
    )
    # Retrieving details on person with ended engagement.
    details_of_ended_engagements = persons_details_from_engagement(
        gql_session,
        list_of_person_uuids_ended_engagements,
        list_of_email_uuids,
        ended_engagement_details=True,
    )

    # Converting details on new started engagements to CSV.
    started_engagements_data_in_csv = convert_person_and_engagement_data_to_csv(
        details_of_started_engagements, started=True
    )
    # Converting details on ended engagements to csv.
    ended_engagements_data_in_csv = convert_person_and_engagement_data_to_csv(
        details_of_ended_engagements, ended=True
    )

    # Generating a file on newly established engagements.
    write_file(
        started_engagements_data_in_csv,
        "reports/os2mo_new_and_ended_engagement_reports/testing_gqlsession_started_engagement_csv.csv",
    )
    # Generating a file  on ended engagements.
    write_file(
        ended_engagements_data_in_csv,
        "reports/os2mo_new_and_ended_engagement_reports/testing_gqlsession_ended_engagement_csv.csv",
    )


def main() -> None:
    settings = get_engagement_settings()
    settings.start_logging_based_on_settings()

    gql_session = setup_gql_client(settings=settings)

    display_engagements(gql_session)


if __name__ == "__main__":
    main()
