import click
from fastramqpi.ra_utils.load_settings import load_setting
from fastramqpi.ra_utils.load_settings import load_settings
from more_itertools import first
from more_itertools import only
from os2mo_helpers.mora_helpers import MoraHelper

from integrations.ad_integration import ad_reader
from integrations.opus import opus_helpers
from integrations.opus.ad import LdapADGUIDReader
from integrations.opus.clear_and_import_opus import import_opus
from integrations.opus.opus_file_reader import get_opus_filereader
from tools.data_fixers.remove_from_lora import delete_object_and_orgfuncs


def find_type(opus_id, full_history):
    """Check if the object with the given id is a unit or an employee."""
    dumps = get_opus_filereader().list_opus_files()
    # Search in newest file first
    opus_files = sorted(dumps, reverse=True)
    if not full_history:
        opus_files = [first(opus_files)]
    for f in opus_files:
        units, employees = opus_helpers.parser(dumps[f], opus_id=opus_id)
        employees, terminated_employees = opus_helpers.split_employees_leaves(employees)
        employees = list(employees)
        if units:
            return "organisationenhed", only(units)
        elif employees:
            return "bruger", only(employees)

    terminated_employees = list(terminated_employees)
    if terminated_employees:
        msg = "Employee was terminated, try --full-history"
    else:
        msg = f"No object with {opus_id=} was found."
    raise ValueError(msg)


@click.command()
@click.option("--mox-base", default=load_setting("mox.base"))
@click.option("--mora-base", default=load_setting("mora.base"))
@click.option("--delete", is_flag=True)
@click.option("--full-history", is_flag=True)
@click.option("--opus-id", type=int, required=True)
@click.option("--use-ad", is_flag=True, help="Read from AD")
@click.option("--dry-run", is_flag=True)
def cli(mox_base, mora_base, delete, full_history, opus_id, use_ad, dry_run):
    """Reimport object from opus with given opus-ID to MO
    Optionally deletes the object and all related orgfuncs directly from Lora.
    Defaults to reading latest file only, but supports reading full history
    """
    helper = MoraHelper(hostname=mora_base)
    object_type, obj = find_type(opus_id, full_history)
    if object_type == "bruger":
        cpr = opus_helpers.read_cpr(obj)
        user = helper.read_user(user_cpr=cpr)
        uuid = user["uuid"] if user else None
    else:
        uuid = opus_helpers.generate_uuid(obj["@id"])

    if delete and uuid and not dry_run:
        delete_object_and_orgfuncs(uuid, mox_base, object_type)
    click.echo(
        f"{'Delete and reimport' if delete else 'Reimport'} '{object_type}' with {uuid=}"
    )
    reader: ad_reader.ADParameterReader | LdapADGUIDReader | None = None
    settings = load_settings()
    if (hostname := settings.get("integrations.opus.ldap_url")) and (
        port := settings.get("integrations.opus.ldap_port")
    ):
        reader = LdapADGUIDReader(host=hostname, port=port)
    elif settings.get("integrations.ad"):
        reader = ad_reader.ADParameterReader()
    import_opus(
        ad_reader=reader,
        import_all=full_history,
        import_last=not full_history,
        opus_id=opus_id,
        rundb_write=False,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    cli()
