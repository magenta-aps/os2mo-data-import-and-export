from uuid import UUID
from datetime import datetime
from typing import List, Dict, Any
from more_itertools import flatten
from more_itertools import one
from fastapi.encoders import jsonable_encoder

from gql import gql

from reports.os2mo_engagements_reports.config import EngagementSettings
from reports.os2mo_engagements_reports import config


from raclients.graph.client import GraphQLClient


def established_person_engagements(settings: EngagementSettings) -> list:
    """Reading all of active engagements with the persons uuids engagement start date."""

    graphql_query = gql(
        """query EstablishedEngagements {
             engagements(from_date: "2022-01-01") {
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
    assert 55 == 55
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
        # Filter by start date being as of today at runtime.
        filtered_dates = filter(
            lambda startdate: one(startdate["objects"])["validity"]["from"] == "2021-07-09T00:00:00+02:00",
            # datetime.now().isoformat(timespec='seconds')
            response["engagements"]
        )
        assert 3 == 3
    extracted_uuids = []
    for obj in filtered_dates:
        extracted_uuids.append(obj["objects"][0]["employee_uuid"])
    assert 4 == 4
    return extracted_uuids


def persons_details_from_engagement(settings: EngagementSettings, uuidlist: List[UUID]) -> dict:
    """Retrieving all desired details on the person from the active filtered engagements."""
#    uuid_list = established_person_engagements(settings)
    assert 9 == 9
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
    assert 1 == 1
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
    assert 2 == 2

    return r


def display_engagements(settings: EngagementSettings):
    list_of_eligible_persons_uuids = established_person_engagements(settings)

    detailed_engagements = persons_details_from_engagement(settings, list_of_eligible_persons_uuids)

    assert 3 == 3

    return detailed_engagements


def main() -> None:
    print("Starting session")
    pass


if __name__ == "__main__":
    settings = config.get_engagement_settings()
    settings.start_logging_based_on_settings()
    display_engagements(settings)

    main()
