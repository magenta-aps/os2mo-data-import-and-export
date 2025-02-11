import asyncio
from typing import Dict
from typing import Optional

import click
import requests
from click_option_group import MutuallyExclusiveOptionGroup
from click_option_group import optgroup
from fastramqpi.ra_utils.load_settings import load_settings
from more_itertools import pairwise
from more_itertools import prepend

from integrations.ad_integration import ad_reader
from integrations.opus import opus_helpers
from integrations.opus.opus_diff_import import import_one
from tools.data_fixers.class_tools import find_duplicates_classes
from tools.subtreedeleter import subtreedeleter_helper


def truncate_db(MOX_BASE: str = "http://localhost:5000/lora") -> None:
    r = requests.get(MOX_BASE + "/db/truncate")
    r.raise_for_status()


def prepare_re_import(
    settings: Optional[Dict] = None,
    opus_uuid: Optional[str] = None,
    connections: int = 4,
) -> None:
    """Remove all opus-units from MO"""
    settings = settings or load_settings()
    mox_base = settings.get("mox.base")
    if opus_uuid:
        session = requests.session()
        dub = find_duplicates_classes(session=session, mox_base=mox_base)
        if dub:
            raise Exception(
                "There are duplicate classes, remove them with tools/data_fixers/remove_duplicate_classes.py --delete"
            )
        subtreedeleter_helper(
            opus_uuid,
            delete_functions=True,
            keep_functions=["KLE", "Relateret Enhed"],
            connections=connections,
        )


async def import_opus(
    ad_reader=None,
    import_all: bool = False,
    import_last=False,
    opus_id=None,
    rundb_write: bool = True,
    dry_run: bool = False,
) -> None:
    """Import one or all files from opus even if no previous files have been imported"""
    settings = load_settings()
    filter_ids = settings.get("integrations.opus.units.filter_ids", [])
    dumps = opus_helpers.read_available_dumps()

    all_dates = dumps.keys()
    # Default is read first file only
    export_dates = [min(all_dates)]
    if import_last:
        export_dates = [max(all_dates)]
    elif import_all:
        export_dates = sorted(all_dates)

    all_export_dates = prepend(None, export_dates)
    date_pairs = pairwise(all_export_dates)
    for date1, date2 in date_pairs:
        await import_one(
            ad_reader,
            date2,
            date1,
            dumps,
            filter_ids,
            opus_id=opus_id,
            rundb_write=rundb_write,
            dry_run=dry_run,
        )


@click.command()
@optgroup.group(
    "Import Amount",
    cls=MutuallyExclusiveOptionGroup,
    help="Default: Imports first file",
)
@optgroup.option(
    "--import-all",
    is_flag=True,
    help="Import all opus files.",
)
@optgroup.option(
    "--import-last",
    is_flag=True,
    help="Import only latest file.",
)
@optgroup.group(
    "Clear data", cls=MutuallyExclusiveOptionGroup, help="Default: No data deleted"
)
@optgroup.option(
    "--delete-opus",
    is_flag=True,
    help="Delete Opus subtree. Deletes all units, engagements etc, but not KLE and related units",
)
@click.option(
    "--connections",
    default=4,
    help="The amount of concurrent requests made to OS2mo. Only applies to '--delete-opus'",
)
@click.option("--new-rundb", is_flag=True, help="Initialize new run-db")
@click.option("--use-ad", is_flag=True, help="Read from AD")
@click.option("--dry-run", is_flag=True)
def clear_and_reload(
    import_all: bool,
    import_last: bool,
    delete_opus: bool,
    new_rundb: bool,
    use_ad: bool,
    connections: int,
    dry_run: bool,
) -> None:
    """Tool for reimporting opus files.

    This tool will load the first opus-file into MO if no inputs are given.
    If the opus organisation allready exists, use either --delete-opus or --truncate.
    --delete-opus will use an uuid genereted from the first unit of the first opus-file and delete all units under this unit, and all relations to thoose units except KLE and related units.
    --truncate will truncate the database entirely.
    Add the --use-ad flag to connect to AD when reading users.
    """
    settings = load_settings()
    if new_rundb:
        opus_helpers.initialize_db(settings["integrations.opus.import.run_db"])

    opus_uuid = str(opus_helpers.find_opus_root_unit_uuid()) if delete_opus else None
    prepare_re_import(
        settings=settings,
        opus_uuid=opus_uuid,
        connections=connections,
    )
    AD = None
    if use_ad:
        AD = ad_reader.ADParameterReader()
        AD.cache_all(print_progress=True)
    asyncio.run(
        import_opus(
            ad_reader=AD,
            import_all=import_all,
            import_last=import_last,
            dry_run=dry_run,
        )
    )


if __name__ == "__main__":
    clear_and_reload()
