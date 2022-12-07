from uuid import UUID
from datetime import datetime
from typing import List

from fastapi.encoders import jsonable_encoder

from gql import gql

from reports.os2mo_engagements_reports.config import EngagementSettings

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
    with GraphQLClient(
        url=f"{settings.mora_base}/graphql/v3",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    ) as session:
        r = session.execute(graphql_query)
        # Filter by start date being as of given day.
        filtered_dates = filter(
            lambda startd: startd["objects"]["validity"]["from"].isoformat() == datetime.now().isoformat(),
            r["engagements"],
        )

    return list(filtered_dates)


def persons_details_from_engagement(settings: EngagementSettings, uuidlist: List[UUID]) -> dict:
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
    variables = {"uuids": uuidlist}
    with GraphQLClient(
        url=f"{settings.mora_base}/graphql/v3",
        client_id=settings.client_id,
        client_secret=settings.client_secret,  # Any cause for worry???
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    ) as session:
        r = session.execute(graphql_query, variable_values=jsonable_encoder(variables))

    return r
