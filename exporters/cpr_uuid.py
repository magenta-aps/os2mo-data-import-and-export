import logging
import sys
from functools import partial
from operator import methodcaller
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID

import click
from fastramqpi.ra_utils.load_settings import load_setting
from fastramqpi.ra_utils.tqdm_wrapper import tqdm
from pydantic import BaseModel

from integrations.mora_helper import MoraHelper

logger = logging.getLogger("cpr_uuid")


class ExportUser(BaseModel):
    cpr: Optional[str]
    mo_uuid: UUID
    ad_guid: Optional[UUID]
    sam_account_name: Optional[str]


def create_mapping(helper, use_ad) -> List[ExportUser]:
    def cache_ad_reader() -> Any:
        from integrations.ad_integration.ad_reader import ADParameterReader

        print("Caching all users from AD...")
        ad_reader = ADParameterReader()
        ad_reader.cache_all(print_progress=True)
        print("OK")
        return ad_reader

    def enrich_user_dict_from_ad(ad_reader: Any, user: ExportUser) -> ExportUser:
        ad_info = ad_reader.read_user(cpr=user.cpr, cache_only=True)
        if ad_info:
            user.ad_guid = ad_info["ObjectGuid"]
            user.sam_account_name = ad_info["SamAccountName"]
        return user

    def to_user_dict(employee: Dict[str, Any]) -> ExportUser:
        # AD properties will be enriched if available
        cpr = employee.get("cpr_no")
        if not cpr:
            logger.warning("no 'cpr_no' for MO user %r", employee["uuid"])
        return ExportUser(
            cpr=cpr,
            mo_uuid=employee["uuid"],
        )  # type: ignore

    print("Fetching all users from MO...")
    employees = helper.read_all_users()
    total = len(employees)
    print("OK")

    employees = map(to_user_dict, employees)

    if use_ad:
        ad_reader = cache_ad_reader()
        employees = map(partial(enrich_user_dict_from_ad, ad_reader), employees)

    print("Processing all...")
    employees = tqdm(employees, total=total)
    employees = list(employees)
    print("OK")
    return employees


def main(mora_base: str, use_ad: bool, output_file_path: str) -> None:
    mh = MoraHelper(hostname=mora_base)

    employees: List[ExportUser] = create_mapping(mh, use_ad)
    employee_dicts: List[Dict] = list(map(methodcaller("dict"), employees))

    fields = ["cpr", "mo_uuid", "ad_guid", "sam_account_name"]
    mh._write_csv(fields, employee_dicts, output_file_path)


def init_log() -> None:
    LOG_LEVEL = logging.DEBUG

    # detail_logging = ('AdCommon', 'mora-helper', 'AdReader', 'cpr_uuid')
    detail_logging = ("mora-helper", "AdReader", "cpr_uuid")
    for name in logging.root.manager.loggerDict:  # type: ignore
        if name in detail_logging:
            logging.getLogger(name).setLevel(LOG_LEVEL)
        else:
            logging.getLogger(name).setLevel(logging.ERROR)

    logging.basicConfig(
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
        level=LOG_LEVEL,
        stream=sys.stdout,
    )


@click.command()
@click.option(
    "--mora-base",
    default=load_setting("mora.base", "http://localhost:5000"),
    help="URL for OS2mo.",
)
@click.option(
    "--use-ad",
    default=False,
    is_flag=True,
    help="Enrich with AD data.",
)
@click.option(
    "--output-file-path",
    default="cpr_mo_ad_map.csv",
    type=click.Path(),
    help="Path to write output file to.",
    show_default=True,
)
def cli(mora_base: str, use_ad: bool, output_file_path: str) -> None:
    """MO CPR, MO UUID, AD GUID, AD SAM CSV Exporter."""
    init_log()
    main(mora_base, use_ad, output_file_path)


if __name__ == "__main__":
    cli()
