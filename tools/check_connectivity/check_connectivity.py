import click
from fastramqpi.ra_utils.load_settings import load_setting

from integrations.mora_helper import MoraHelper


@click.command()
@click.option(
    "--mora-base",
    default=load_setting("mora.base", "http://localhost:5000"),
    help="URL for OS2mo.",
)
def test_credentials(mora_base):
    """Script to check connectivity against MO."""
    mh = MoraHelper(mora_base)
    print("Organisation UUID: " + mh.read_organisation())


if __name__ == "__main__":
    test_credentials()
