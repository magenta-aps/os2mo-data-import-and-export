from uuid import UUID
from datetime import datetime, date
from typing import List

from dateutil import utils
from more_itertools import one
from fastapi.encoders import jsonable_encoder

from gql import gql

# from reports.os2mo_engagements_reports.config import EngagementSettings
from reports.os2mo_engagements_reports import config

from raclients.graph.client import GraphQLClient
from ra_utils.job_settings import JobSettings


def established_person_engagements(settings: JobSettings) -> list:
    """Reading all of active engagements with the persons uuids engagement start date."""

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
        # Filter by start date being as of today at runtime.
        filtered_dates = filter(
            lambda startdate: datetime.fromisoformat(
                one(startdate["objects"])["validity"]["from"]
            ).replace(tzinfo=None)
            == utils.today(),
            response["engagements"],
        )
    extracted_uuids = []
    for obj in filtered_dates:
        extracted_uuids.append(obj["objects"][0]["employee_uuid"])
    return extracted_uuids


def persons_details_from_engagement(
    settings: JobSettings, uuidlist: List[UUID]
) -> dict:
    """Retrieving all desired details on the person from the active filtered engagements."""
    graphql_query = gql(
        """query PersonEngagementDetails ($uuidlist: [UUID!]) {
             employees(uuids: $uuidlist) {
               objects {
                 cpr_no
                 name
                 user_key
                 uuid
                 addresses(address_types: "f376deb8-4743-4ca6-a047-3241de8fe9d2") {
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
    variables = {"uuidlist": uuidlist}
    with GraphQLClient(
        url=f"{settings.mora_base}/graphql/v3",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    ) as session:
        r = session.execute(graphql_query, variable_values=jsonable_encoder(variables))
    return r


def display_engagements(settings: JobSettings):
    list_of_eligible_persons_uuids = established_person_engagements(settings)

    detailed_engagements = persons_details_from_engagement(
        settings, list_of_eligible_persons_uuids
    )

    return detailed_engagements


def main() -> None:
    pass


if __name__ == "__main__":
    settings = config.get_engagement_settings()
    settings.start_logging_based_on_settings()
    display_engagements(settings)

    main()
