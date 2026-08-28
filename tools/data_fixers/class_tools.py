import logging
from dataclasses import dataclass
from typing import Iterator
from typing import Never
from uuid import UUID

import click
from fastramqpi.ra_utils.load_settings import load_setting
from fastramqpi.ra_utils.tqdm_wrapper import tqdm
from fastramqpi.ra_utils.transpose_dict import transpose_dict
from fastramqpi.raclients.graph.client import GraphQLClient
from gql import gql
from gql.client import SyncClientSession
from more_itertools import unzip
from pydantic import AnyHttpUrl

logger = logging.getLogger(__name__)

MO_GRAPHQL_VERSION = "v29"


def raise_lora_discontinued_error() -> Never:
    # HACK: this is not ideal. The "good" solution would be to figure out which
    #   of these tools is needed and delete the ones that aren't, and then port
    #   the remaining ones to GraphQL. But the practical reality is that these
    #   tools get used very rarely and just the process of finding the right
    #   people and context to decide what needs to go is too much work, not even
    #   counting the time needed to then port to GraphQL and test, considering
    #   there are no integration tests for this code.
    raise NotImplementedError(
        "The LoRa API has been discontinued, so this tool is likely broken. Please fix this tool or remove it if it's no longer needed."
    )


def graphql_client(
    mora_base: str,
    client_id: str,
    client_secret: str,
    auth_realm: str,
    auth_server: AnyHttpUrl,
) -> GraphQLClient:
    """Construct a synchronous MO GraphQL client."""
    return GraphQLClient(
        url=f"{mora_base}/graphql/{MO_GRAPHQL_VERSION}",
        client_id=client_id,
        client_secret=client_secret,
        auth_realm=auth_realm,
        auth_server=auth_server,
        sync=True,
        httpx_client_kwargs={"timeout": None},
    )


@dataclass(frozen=True)
class ClassRelation:
    """A class-bearing field on a MO object type.

    Describes how to find objects referencing a class through the field, and
    how to update the field to point at another class.
    """

    collection: str  # top-level GraphQL collection, e.g. "engagements"
    response_field: str  # class field on the object, e.g. "engagement_type_response"
    mutation: str  # update mutation, e.g. "engagement_update"
    input_type: str  # GraphQL input type of the mutation
    input_field: str  # field on the update input, e.g. "engagement_type"
    # Server-side ClassFilter name. Fields without one require a full scan of
    # the collection, filtering client-side.
    filter_name: str | None = None
    is_list: bool = False  # whether the field holds a list of classes
    # (query field, input field) pairs which the update mutation requires even
    # when unchanged, e.g. rolebindings' ituser.
    extra_fields: tuple[tuple[str, str], ...] = ()


CLASS_RELATIONS = [
    ClassRelation("engagements", "engagement_type_response", "engagement_update", "EngagementUpdateInput", "engagement_type", filter_name="engagement_type"),
    ClassRelation("engagements", "job_function_response", "engagement_update", "EngagementUpdateInput", "job_function", filter_name="job_function"),
    ClassRelation("engagements", "primary_response", "engagement_update", "EngagementUpdateInput", "primary"),
    ClassRelation("addresses", "address_type_response", "address_update", "AddressUpdateInput", "address_type", filter_name="address_type"),
    ClassRelation("addresses", "visibility_response", "address_update", "AddressUpdateInput", "visibility", filter_name="visibility"),
    ClassRelation("associations", "association_type_response", "association_update", "AssociationUpdateInput", "association_type", filter_name="association_type"),
    ClassRelation("associations", "primary_response", "association_update", "AssociationUpdateInput", "primary"),
    ClassRelation("associations", "trade_union_response", "association_update", "AssociationUpdateInput", "trade_union"),
    ClassRelation("itusers", "primary_response", "ituser_update", "ITUserUpdateInput", "primary", filter_name="primary"),
    ClassRelation("kles", "kle_number_response", "kle_update", "KLEUpdateInput", "kle_number"),
    ClassRelation("kles", "kle_aspects_response", "kle_update", "KLEUpdateInput", "kle_aspects", is_list=True),
    ClassRelation("leaves", "leave_type_response", "leave_update", "LeaveUpdateInput", "leave_type"),
    ClassRelation("managers", "manager_type_response", "manager_update", "ManagerUpdateInput", "manager_type", filter_name="manager_type"),
    ClassRelation("managers", "manager_level_response", "manager_update", "ManagerUpdateInput", "manager_level"),
    ClassRelation("managers", "responsibilities_response", "manager_update", "ManagerUpdateInput", "responsibility", filter_name="responsibility", is_list=True),
    ClassRelation("org_units", "unit_type_response", "org_unit_update", "OrganisationUnitUpdateInput", "org_unit_type"),
    ClassRelation("org_units", "unit_level_response", "org_unit_update", "OrganisationUnitUpdateInput", "org_unit_level"),
    ClassRelation("org_units", "time_planning_response", "org_unit_update", "OrganisationUnitUpdateInput", "time_planning"),
    ClassRelation("org_units", "unit_hierarchy_response", "org_unit_update", "OrganisationUnitUpdateInput", "org_unit_hierarchy", filter_name="hierarchy"),
    ClassRelation("rolebindings", "role_response", "rolebinding_update", "RoleBindingUpdateInput", "role", filter_name="role", extra_fields=(("ituser_response", "ituser"),)),
]  # fmt: skip


@dataclass(frozen=True)
class ClassUsage:
    """A single validity of a MO object referencing the sought class."""

    relation: ClassRelation
    object_uuid: str
    validity: dict  # {"from": ..., "to": ...}
    class_uuids: list[str]  # current value(s) of the field in this validity
    extra: dict  # extra required update input fields, e.g. rolebindings' ituser


def _validity_selection(relations: list[ClassRelation]) -> str:
    fields = []
    for relation in relations:
        if relation.is_list:
            # List fields are paged in the GraphQL schema
            fields.append(f"{relation.response_field} {{ objects {{ uuid }} }}")
        else:
            fields.append(f"{relation.response_field} {{ uuid }}")
        for query_field, _ in relation.extra_fields:
            fields.append(f"{query_field} {{ uuid }}")
    return " ".join(fields)


def _build_usage_query(
    collection: str, relations: list[ClassRelation], filter_name: str | None
):
    if filter_name is not None:
        variables = "$limit: int!, $cursor: Cursor, $class_uuids: [UUID!]"
        object_filter = f"{{from_date: null, to_date: null, {filter_name}: {{uuids: $class_uuids}}}}"
    else:
        variables = "$limit: int!, $cursor: Cursor"
        object_filter = "{from_date: null, to_date: null}"
    return gql(
        f"""
        query FindClassUsages({variables}) {{
            {collection}(limit: $limit, cursor: $cursor, filter: {object_filter}) {{
                objects {{
                    uuid
                    validities(start: null, end: null) {{
                        validity {{ from to }}
                        {_validity_selection(relations)}
                    }}
                }}
                page_info {{ next_cursor }}
            }}
        }}
        """
    )


def find_class_usages(
    session: SyncClientSession, class_uuid: UUID, page_size: int = 500
) -> Iterator[ClassUsage]:
    """Find every object validity referencing the class with the given uuid.

    Uses server-side filtering for the class fields that support it, and falls
    back to a full scan of the collection for those that do not.
    """
    class_uuid_str = str(class_uuid)

    filtered = [r for r in CLASS_RELATIONS if r.filter_name is not None]
    query_groups = [(r.collection, [r], r.filter_name) for r in filtered]
    scanned: dict[str, list[ClassRelation]] = {}
    for relation in CLASS_RELATIONS:
        if relation.filter_name is None:
            scanned.setdefault(relation.collection, []).append(relation)
    query_groups.extend(
        (collection, relations, None) for collection, relations in scanned.items()
    )

    for collection, relations, filter_name in query_groups:
        query = _build_usage_query(collection, relations, filter_name)
        variables: dict = {"limit": page_size, "cursor": None}
        if filter_name is not None:
            variables["class_uuids"] = [class_uuid_str]
        while True:
            result = session.execute(query, variable_values=variables)
            data = result[collection]
            for obj in data["objects"]:
                for validity in obj["validities"]:
                    for relation in relations:
                        node = validity[relation.response_field]
                        if relation.is_list:
                            uuids = [c["uuid"] for c in node["objects"]] if node else []
                        else:
                            uuids = [node["uuid"]] if node else []
                        if class_uuid_str not in uuids:
                            continue
                        extra = {
                            input_field: validity[query_field]["uuid"]
                            for query_field, input_field in relation.extra_fields
                        }
                        yield ClassUsage(
                            relation=relation,
                            object_uuid=obj["uuid"],
                            validity=validity["validity"],
                            class_uuids=uuids,
                            extra=extra,
                        )
            cursor = data["page_info"]["next_cursor"]
            if cursor is None:
                break
            variables["cursor"] = cursor


def switch_class(
    session: SyncClientSession, usage: ClassUsage, old_uuid: UUID, new_uuid: UUID
) -> None:
    """Point one object validity at a different class."""
    relation = usage.relation
    if relation.is_list:
        # Replace old with new, deduplicating in case both were present
        new_value: list[str] | str = list(
            dict.fromkeys(
                str(new_uuid) if u == str(old_uuid) else u for u in usage.class_uuids
            )
        )
    else:
        new_value = str(new_uuid)
    mutation = gql(
        f"""
        mutation SwitchClass($input: {relation.input_type}!) {{
            {relation.mutation}(input: $input) {{ uuid }}
        }}
        """
    )
    session.execute(
        mutation,
        variable_values={
            "input": {
                "uuid": usage.object_uuid,
                "validity": usage.validity,
                relation.input_field: new_value,
                **usage.extra,
            }
        },
    )


def move_class_helper(
    session: SyncClientSession,
    old_uuid: UUID,
    new_uuid: UUID,
    dry_run: bool = False,
) -> int:
    """Move all objects from one class to another.

    Finds every object validity referencing 'old_uuid' and updates it to
    reference 'new_uuid' instead. Returns the number of updates made (or, on
    dry-run, that would have been made).
    """
    count = 0
    usages = find_class_usages(session, old_uuid)
    for usage in tqdm(usages, desc="Changing class for objects"):
        count += 1
        if dry_run:
            click.echo(
                f"Would update {usage.relation.input_field} on "
                f"{usage.relation.collection} {usage.object_uuid} "
                f"(validity {usage.validity['from']} - {usage.validity['to']})"
            )
            continue
        switch_class(session, usage, old_uuid, new_uuid)
    return count


def delete_class(session: SyncClientSession, uuid: UUID) -> None:
    """Delete the class with the given uuid."""
    mutation = gql(
        """
        mutation DeleteClass($uuid: UUID!) {
            class_delete(uuid: $uuid) {
                uuid
            }
        }
        """
    )
    session.execute(mutation, variable_values={"uuid": str(uuid)})


def filter_duplicates(
    class_uuids, class_bvns, class_titles, facet_uuids
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Transforms data from classes to a list of duplicate classes in facets.

    Example 1) there are two classes called "test", but they are in different facets:
    >>> info = (["class_uuid1", "class_uuid2"],["test", "Test"],["TEST", "Test"],["facet_uuid1", "facet_uuid2"])
    >>> filter_duplicates(*info)
    {}

    Example 2) there are two classes called "test" in the same facet:
    >>> info = ["class_uuid1", "class_uuid2"],["test", "Test"],["TEST", "Test"],["facet_uuid1", "facet_uuid1"]
    >>> filter_duplicates(*info)
    {('test', 'facet_uuid1'): [('class_uuid1', 'TEST'), ('class_uuid2', 'Test')]}
    """
    # find duplicates of (lowercase bvn, facet uuid)
    class_bvns_lower = [x.lower() for x in class_bvns]
    bvn_facets_lower = list(zip(class_bvns_lower, facet_uuids))
    dup_bvn_facets = set(x for x in bvn_facets_lower if bvn_facets_lower.count(x) > 1)
    uuid_title_map = tuple(zip(class_uuids, class_titles))

    bvn_map_lower = dict(zip(uuid_title_map, bvn_facets_lower))
    # Find alle the duplicates
    duplicate_bvn_facet = filter(
        lambda x: x[1] in dup_bvn_facets, bvn_map_lower.items()
    )  # type: ignore
    duplicate_bvn_facet = dict(duplicate_bvn_facet)  # type: ignore

    # Transpose the dict to be able to iterate over duplicates
    transposed = transpose_dict(duplicate_bvn_facet)  # type: ignore

    return transposed  # type: ignore


# TODO: set up integration tests that check that duplicates are acutally found. I tested it manually by spinning up mo
#       and creating a duplicate class manually.
def find_duplicate_classes(
    mora_base: str,
    client_id: str,
    client_secret: str,
    auth_realm: str,
    auth_server: AnyHttpUrl,
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Find classes within a facet that are duplicates.

    Returns a dict mapping (lowercase user-key, facet uuid) to lists of
    (uuid, name) of classes that are duplicates.
    """
    with graphql_client(
        mora_base=mora_base,
        client_id=client_id,
        client_secret=client_secret,
        auth_realm=auth_realm,
        auth_server=auth_server,
    ) as session:
        assert isinstance(session, SyncClientSession)
        q = gql(
            """
        query ClassToolsFindDuplicateClasses {
            classes {
                objects {
                    current {
                        uuid
                        user_key
                        name
                        facet_response {
                            uuid
                        }
                    }
                }
            }
        }
        """
        )
        res = session.execute(q)
        classes = res["classes"]["objects"]
        classes_current = [
            klass["current"] for klass in classes if klass["current"] is not None
        ]
        assert len(classes_current) == len(classes), "current should never be None"
        classes_tuples = (
            (
                klass["uuid"],
                klass["user_key"],
                klass["name"],
                klass["facet_response"]["uuid"],
            )
            for klass in classes_current
        )
        class_uuids, class_user_keys, class_names, facet_uuids = unzip(classes_tuples)
        return filter_duplicates(class_uuids, class_user_keys, class_names, facet_uuids)


def graphql_options(fn):
    fn = click.option(
        "--auth-server",
        envvar="AUTH_SERVER",
        default="http://localhost:5000/auth",
        help="URL of the Keycloak server",
    )(fn)
    fn = click.option(
        "--auth-realm", envvar="AUTH_REALM", default="mo", help="Keycloak realm"
    )(fn)
    fn = click.option(
        "--client-secret", envvar="CLIENT_SECRET", required=True, help="Client secret"
    )(fn)
    fn = click.option(
        "--client-id", envvar="CLIENT_ID", default="dipex", help="Client ID"
    )(fn)
    fn = click.option(
        "--mora-base",
        envvar="MORA_BASE",
        default="http://localhost:5000",
        help="URL for MO",
    )(fn)
    return fn


@click.group()
def cli():
    pass


@cli.command()
@click.option(
    "--delete",
    type=click.BOOL,
    default=False,
    is_flag=True,
    required=False,
    help="Remove any class that has duplicates",
)
@graphql_options
def remove_dup_classes(
    delete: bool,
    mora_base: str,
    client_id: str,
    client_secret: str,
    auth_realm: str,
    auth_server: str,
):
    """Tool to help remove classes from MO that are duplicates.

    This tool is written to help clean up engagement_types that had the same name, but with different casing.
    If no argument is given it will print the amount of duplicated classses.
    If the `--delete` flag is supplied you will be prompted to choose a class to keep for each duplicate.
    Objects related to the other class will be transferred to the selected class and the other class deleted.
    """
    duplicate_bvn_facet = find_duplicate_classes(
        mora_base=mora_base,
        client_id=client_id,
        client_secret=client_secret,
        auth_realm=auth_realm,
        auth_server=auth_server,  # type: ignore
    )

    if not delete:
        click.echo(f"There are {len(duplicate_bvn_facet)} duplicate class(es).")
        return

    with graphql_client(
        mora_base=mora_base,
        client_id=client_id,
        client_secret=client_secret,
        auth_realm=auth_realm,
        auth_server=auth_server,  # type: ignore
    ) as session:
        assert isinstance(session, SyncClientSession)
        for dup_class in tqdm(
            duplicate_bvn_facet.values(), desc="Deleting duplicate classes"
        ):
            _, titles = unzip(dup_class)
            title_set = set(titles)

            # Check if all found titles are exactly the same. Only prompt for a choice if they are not.
            keep = 1
            if len(title_set) != 1:
                click.echo("These are the choices:")
                # Generate a prompt to display
                msg = "\n".join(
                    f"  {i}: {x[1]}" for i, x in enumerate(dup_class, start=1)
                )
                click.echo(msg)
                keep = int(
                    click.prompt("Choose the one to keep", type=int, default="1")
                )
            kept_uuid, _ = dup_class[keep - 1]
            for i, (uuid, _) in enumerate(dup_class, start=1):
                if i == keep:
                    continue
                move_class_helper(session=session, old_uuid=uuid, new_uuid=kept_uuid)
                delete_class(session, uuid)


@cli.command()
@click.option(
    "--old-uuid",
    required=True,
    type=click.UUID,
    help="UUID of old class",
)
@click.option(
    "--new-uuid",
    required=True,
    type=click.UUID,
    help="UUID of new class",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the objects that would be updated without changing anything",
)
@graphql_options
def move_class(
    old_uuid: UUID,
    new_uuid: UUID,
    dry_run: bool,
    mora_base: str,
    client_id: str,
    client_secret: str,
    auth_realm: str,
    auth_server: str,
):
    """Switches class for all objects using this class given two UUIDs."""
    with graphql_client(
        mora_base=mora_base,
        client_id=client_id,
        client_secret=client_secret,
        auth_realm=auth_realm,
        auth_server=auth_server,  # type: ignore
    ) as session:
        assert isinstance(session, SyncClientSession)
        count = move_class_helper(
            session=session, old_uuid=old_uuid, new_uuid=new_uuid, dry_run=dry_run
        )
        action = "Would update" if dry_run else "Updated"
        click.echo(f"{action} {count} object validities")


@cli.command()
@click.option(
    "--mox-base",
    help="URL for MOX",
    type=click.STRING,
    default=load_setting("mox.base", "http://localhost:5000/lora/"),
)
@click.option(
    "--dry-run",
    default=False,
    is_flag=True,
    help="Dry run and print the generated object.",
)
def ensure_static_classes(mox_base, dry_run):
    raise_lora_discontinued_error()


@cli.command()
@click.option(
    "--mox-base",
    help="URL for MOX",
    type=click.STRING,
    default=load_setting("mox.base", "http://localhost:5000/lora/"),
)
@click.option(
    "--dry-run",
    default=False,
    is_flag=True,
    help="Dry run and print the generated object.",
)
def ensure_single_owner(mox_base, dry_run):
    raise_lora_discontinued_error()


if __name__ == "__main__":
    cli()
