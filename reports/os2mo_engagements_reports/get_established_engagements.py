from uuid import UUID
from datetime import datetime, date
from typing import List

from dateutil import utils
from more_itertools import one
from fastapi.encoders import jsonable_encoder

from gql import gql

from raclients.graph.client import GraphQLClient
from ra_utils.job_settings import JobSettings
from reports.os2mo_engagements_reports import config


def established_person_engagements(settings: JobSettings) -> dict:
    """Reading all of active engagements with the persons uuid(s) engagement start date.
    Returns an object of active engagements persons uuid(s).
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


def get_validity_objects_from_gql(gql_query_dict: dict) -> list:
    """Filtering GraphQL payload on dates.
    Returns a list of objects with valid engagement dates and person uuid(s).
    """
    filtered_dates = filter(
        lambda startdate: one(startdate["objects"])["validity"]["from"],
        gql_query_dict["engagements"],
    )

    return list(filtered_dates)


def get_filtered_dates_from_today(gql_query_response: list) -> List[UUID]:
    """Applying filter to get uuids of persons which have started engagements as of today.
    Returns a list of eligible uuid(s) of the person(s).
    """
    filtered_dates = filter(
        lambda startd: startd["objects"][0]["validity"]["from"], gql_query_response
    )
    extracted_uuids = [
        one(obj["objects"])["employee_uuid"]
        for obj in filtered_dates
        if datetime.fromisoformat(one(obj["objects"])["validity"]["from"]).replace(
            tzinfo=None
        )
        == utils.today()
    ]

    return extracted_uuids


def retrieve_address_types_uuids(settings: JobSettings) -> dict:
    """Reading all types of addresses.
    Returns object containing uuid and scope on all active addresses."""
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
    """Filtering through address types to retrieve uuids of addresses with scope of email.
    Returns a list of all uuids with scope of email."""
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
    """Retrieving all desired details on the person from the active filtered engagements."""
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


def display_engagements(settings: JobSettings) -> dict:
    """Calls upon GraphQL queries and filters defined in this module, to bring together an object
    with all the details wanted on an engagement and on a person."""
    payload_of_active_engagements_objects = established_person_engagements(settings)

    address_type_uuids_and_scopes = retrieve_address_types_uuids(settings)

    list_of_email_uuids = get_email_address_type_uuid_from_gql(
        address_type_uuids_and_scopes
    )

    filtered_dates = get_validity_objects_from_gql(
        payload_of_active_engagements_objects
    )

    list_of_person_uuids = get_filtered_dates_from_today(filtered_dates)

    detailed_engagements = persons_details_from_engagement(
        settings, list_of_person_uuids, list_of_email_uuids
    )

    return detailed_engagements


def main() -> None:
    settings = config.get_engagement_settings()
    settings.start_logging_based_on_settings()
    display_engagements(settings)


if __name__ == "__main__":
    main()
