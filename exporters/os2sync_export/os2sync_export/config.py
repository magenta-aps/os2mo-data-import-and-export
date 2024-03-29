import logging
from functools import lru_cache
from pathlib import Path
from typing import Any
from typing import cast
from typing import Dict
from typing import List
from typing import Literal
from typing import Tuple
from uuid import UUID

from pydantic import AnyHttpUrl
from pydantic import BaseSettings
from ra_utils.apply import apply
from ra_utils.job_settings import JobSettings
from ra_utils.load_settings import load_settings
from raclients.graph.client import GraphQLClient

logger = logging.getLogger(__name__)


def json_config_settings_source(settings: BaseSettings) -> Dict[str, Any]:  # type: ignore
    """
    Read config from settings.json.

    Reads all keys starting with 'os2sync.' and a few common settings into Settings.
    """
    try:
        all_settings = load_settings()
    except FileNotFoundError:
        # No settingsfile found. Using environment variables"
        return {}
    # Read os2sync specific settings
    os2sync_settings: Dict[str, str] = dict(
        filter(
            apply(lambda key, value: key.startswith("os2sync")), all_settings.items()
        )
    )

    # replace dots with underscore. eg: os2sync.ignored.unit_levels -> os2sync_ignored_unit_levels
    final_settings = {
        key.replace(".", "_"): val for key, val in os2sync_settings.items()
    }

    # Add needed common settings
    municipality = all_settings.get("municipality.cvr")
    if municipality:
        final_settings["municipality"] = municipality
    mora_base = all_settings.get("mora.base")
    if mora_base:
        final_settings["mora_base"] = mora_base

    return final_settings


class Settings(JobSettings):
    # common:
    municipality: str  # Called "municipality.cvr" in settings.json
    mora_base: AnyHttpUrl = cast(
        AnyHttpUrl, "http://localhost:5000"
    )  # "mora.base" from settings.json + /service

    # os2sync:
    os2sync_top_unit_uuid: UUID
    os2sync_api_url: AnyHttpUrl | Literal["stub"] = cast(
        AnyHttpUrl, "http://localhost:8081/api"
    )

    os2sync_use_lc_db: bool = False
    os2sync_hash_cache: Path = Path("/opt/dipex/os2sync_hash_cache")
    os2sync_xfer_cpr: bool = False

    os2sync_autowash: bool = False
    os2sync_ca_verify_os2sync: bool = True
    os2sync_ca_verify_os2mo: bool = True

    os2sync_phone_scope_classes: List[UUID] = []
    os2sync_landline_scope_classes: List[UUID] = []
    os2sync_email_scope_classes: List[UUID] = []
    os2sync_ignored_unit_levels: List[UUID] = []
    os2sync_ignored_unit_types: List[UUID] = []
    os2sync_templates: Dict = {}

    os2sync_sync_managers: bool = False
    os2sync_use_contact_for_tasks: bool = False
    os2sync_employee_engagement_address: List[str] = []
    os2sync_uuid_from_it_systems: List[str] = []

    os2sync_truncate_length: int = 200

    os2sync_user_key_it_system_name: str = "Active Directory"

    os2sync_filter_hierarchy_names: Tuple = tuple()  # Title in MO
    os2sync_filter_users_by_it_system: bool = False

    # To be used with the job-function-configurator integration.
    os2sync_use_extension_field_as_job_function: bool = False

    os2sync_enable_kle: bool = False

    class Config:

        env_file_encoding = "utf-8"

        @classmethod
        def customise_sources(
            cls,
            init_settings,
            env_settings,
            file_secret_settings,
        ):
            return (
                init_settings,
                env_settings,
                json_config_settings_source,
                file_secret_settings,
            )


@lru_cache()
def get_os2sync_settings(*args, **kwargs) -> Settings:
    return Settings(*args, **kwargs)


def setup_gql_client(settings: Settings) -> GraphQLClient:

    return GraphQLClient(
        url=f"{settings.mora_base}/graphql/v3",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        auth_realm=settings.auth_realm,
        auth_server=settings.auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    )


if __name__ == "__main__":
    print(get_os2sync_settings())
