from collections import Counter
from operator import itemgetter

import click
import requests
from fastramqpi.ra_utils.load_settings import load_settings

from integrations.mora_helper import MoraHelper


def check_duplicate_cpr(mora_base: str) -> list:
    helper = MoraHelper(hostname=mora_base)
    users = helper.read_all_users()
    users = filter(lambda u: u.get("cpr_no"), users)
    cprs = dict(map(itemgetter("uuid", "cpr_no"), users))
    duplicates = [i for i, cnt in Counter(cprs.values()).items() if cnt > 1]
    duplicate_uuids = dict(filter(lambda x: x[1] in duplicates, cprs.items()))
    return duplicate_uuids  # type: ignore


@click.command()
@click.option(
    "--delete",
    type=click.BOOL,
    default=False,
    is_flag=True,
    required=False,
    help="Remove all user objects that has the same cpr-number as another user",
)
def cli(delete):
    """Find users in MO that have the same CPR number.

    Prints the number of cpr-numbers that are used by more than one user and the list of uuids for the users sharing a cpr-number.
    Add the `--delete` flag to remove all users that share the same cpr-number of another MO user.
    """
    settings = load_settings()
    mox_base = settings.get("mox.base")
    mora_base = settings.get("mora.base")
    uuids = check_duplicate_cpr(mora_base)

    if delete:
        for uuid in uuids:
            r = requests.delete(f"{mox_base}/organisation/bruger/{uuid}")
            r.raise_for_status()
    else:
        click.echo(
            f"There are {len(uuids)} CPR-number(s) assigned to more than one user"
        )
        if uuids:
            click.echo(uuids)


if __name__ == "__main__":
    cli()
