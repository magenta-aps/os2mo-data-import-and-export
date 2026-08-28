import click
from gql import gql
from gql.client import SyncClientSession
from more_itertools import only

from tools.data_fixers.class_tools import delete_class
from tools.data_fixers.class_tools import graphql_client
from tools.data_fixers.class_tools import move_class_helper


def get_class_uuid(session: SyncClientSession, facet_user_key, user_key):
    q = gql(
        """
    query ClassQuery($facet_user_keys: [String!], $user_keys:[String!])
        {
            classes(filter: {facet_user_keys: $facet_user_keys, user_keys: $user_keys})
            {
                objects {
                    uuid
                }
            }
        }
    """
    )
    res = session.execute(
        q,
        variable_values={"facet_user_keys": [facet_user_key], "user_keys": [user_key]},
    )
    classes: dict = only(res["classes"]["objects"], {})
    return classes.get("uuid")


@click.command()
@click.option("--remove", prompt=True, help="User-key of the class to remove")
@click.option("--keep", prompt=True, help="User-key of the class to keep")
@click.option(
    "--facet", prompt=True, help="User-key of the facet to which the classes belong"
)
@click.option("--mora_base", envvar="MORA_BASE", default="http://localhost:5000")
@click.option("--client_id", envvar="CLIENT_ID", default="dipex")
@click.option("--client_secret", envvar="CLIENT_SECRET")
@click.option("--auth_realm", envvar="AUTH_REALM", default="mo")
@click.option(
    "--auth_server", envvar="AUTH_SERVER", default="http://localhost:5000/auth"
)
@click.option("--dry-run", is_flag=True)
def merge_classes(
    facet: str,
    remove: str,
    keep: str,
    mora_base: str,
    client_id: str,
    client_secret: str,
    auth_realm: str,
    auth_server: str,
    dry_run: bool,
) -> None:
    """Helper tool to merge two classes into one.

    Moves any object from the removed class to the kept class before deleting the removed class.

    """

    with graphql_client(
        mora_base=mora_base,
        client_id=client_id,
        client_secret=client_secret,
        auth_realm=auth_realm,
        auth_server=auth_server,  # type: ignore
    ) as session:
        assert isinstance(session, SyncClientSession)
        keep_uuid = get_class_uuid(session=session, facet_user_key=facet, user_key=keep)
        if keep_uuid is None:
            click.echo(f"No class with user-key {keep} found in facet {facet}")
            return
        remove_uuid = get_class_uuid(
            session=session,
            facet_user_key=facet,
            user_key=remove,
        )
        if remove_uuid is None:
            click.echo(f"No class with user-key {remove} found in facet {facet}")
            return

        if dry_run:
            click.echo(f"Keeping class with uuid {keep_uuid}")
            click.echo(f"Removing class with uuid {remove_uuid}")

        count = move_class_helper(
            session=session,
            old_uuid=remove_uuid,
            new_uuid=keep_uuid,
            dry_run=dry_run,
        )
        if dry_run:
            click.echo(f"Would move {count} object validities to the kept class")
            return
        delete_class(session=session, uuid=remove_uuid)


if __name__ == "__main__":
    merge_classes()
