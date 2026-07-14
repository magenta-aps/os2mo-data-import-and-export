# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
"""
Helper class to make a number of pre-defined queries into MO
"""

import asyncio
import datetime
import logging
import sys
from functools import partial
from operator import itemgetter
from typing import List
from uuid import UUID

import click
from anytree import PreOrderIter
from fastramqpi.ra_utils.asyncio_utils import gather_with_concurrency
from fastramqpi.raclients.upload import file_uploader
from gql import gql
from gql.client import AsyncClientSession
from more_itertools import first
from more_itertools import flatten
from more_itertools import only

from exporters.plan2learn.plan2learn_settings import Settings
from exporters.plan2learn.plan2learn_settings import Variant
from exporters.plan2learn.plan2learn_settings import get_unified_settings
from exporters.plan2learn.ship_files import ship_files
from exporters.sql_export.gql_lora_cache_async import GQLLoraCache
from exporters.sql_export.lora_cache import get_cache as LoraCache
from exporters.utils.priority_by_class import choose_public_address
from exporters.utils.priority_by_class import lc_choose_public_address
from integrations.mora_helper import MoraHelper

LOG_LEVEL = logging.DEBUG

logger = logging.getLogger("plan2learn")

for name in logging.root.manager.loggerDict:
    if name in ("LoraCache", "mora-helper", "plan2learn"):
        logging.getLogger(name).setLevel(LOG_LEVEL)
    else:
        logging.getLogger(name).setLevel(logging.ERROR)

logging.basicConfig(
    format="%(levelname)s %(asctime)s %(name)s %(message)s",
    level=LOG_LEVEL,
    stream=sys.stdout,
)

ACTIVE_JOB_FUNCTIONS = []  # Liste over aktive engagementer som skal eksporteres.


def get_e_address(e_uuid, scope, lc_historic):
    lora_addresses = lc_historic.addresses.values()
    # Retrieving all addresses and flattening the list by one link.
    lora_addresses = flatten(lora_addresses)
    # Iterator of all addresses for the current user
    lora_addresses = filter(lambda address: address["user"] == e_uuid, lora_addresses)
    # Iterator of all addresses for current user and correct scope
    lora_addresses = filter(lambda address: address["scope"] == scope, lora_addresses)
    candidates = lora_addresses
    # Sort addresses by startdate to pick the newest address first
    return sorted(candidates, key=lambda a: a["from_date"], reverse=True)


def get_filtered_phone_addresses(
    e_uuid: UUID, priority_list: List[UUID], lc_historic
) -> dict:
    """
    Takes UUID of a person and returns an object with only eligible numbers through a filter.
    Returns if a match on only the first element in the priority list is found.
    Defaults to an empty dict, if no address is found.

    args:
    uuid of a person, a list of uuid(s) to filter on and LoRaCache historic.

    returns:
    A dict with an eligible phone number, or an empty dict if none is found.
    """
    # Retrieve all addresses with the scope of "Telefon". These appear as dicts inside a list.
    phone_addresses = get_e_address(str(e_uuid), "Telefon", lc_historic)

    # Filter through all addresses, on the "adresse_type" uuid, and only return the ones existing in priority_list.
    addresses = filter(
        lambda p: p["adresse_type"] and UUID(p["adresse_type"]) in priority_list,
        phone_addresses,
    )

    # Sort addresses according to the "adresse_type" placement in priority_list, to only return the address that matches
    # the first element in priority_list.
    address = first(  # type: ignore
        sorted(addresses, key=lambda a: priority_list.index(UUID(a["adresse_type"]))),
        default={},
    )

    if address is not None:
        return address
    else:
        return {}


def get_email_addresses(
    e_uuid: UUID, priority_list: List[UUID], lc_historic, lc
) -> dict:
    """
    Takes UUID of a person and returns a list object with eligible emails through a priority list.

    args:
    uuid of a person, a priority list of uuid(s), LoRaCache historic.

    returns:
    A dict with an eligible email or an empty dict if none.
    """

    email_addresses = get_e_address(str(e_uuid), "E-mail", lc_historic)

    address = lc_choose_public_address(
        email_addresses, [str(uuid) for uuid in priority_list], lc
    )
    if address is not None:
        return address
    else:
        return {}


def get_e_address_mo(e_uuid, scope, mh, settings: Settings):
    candidates = mh.get_e_addresses(e_uuid, scope)

    if scope == "PHONE":
        priority_list = [str(u) for u in settings.plan2learn_phone_priority]
    elif scope == "EMAIL":
        priority_list = [str(u) for u in settings.plan2learn_email_priority]
    else:
        priority_list = []

    address = choose_public_address(candidates, priority_list)
    if address is not None:
        return address
    else:
        return {}  # like mora_helpers


def construct_bruger_row(user_uuid, cpr, name, email, phone):
    row = {
        "BrugerId": user_uuid,
        "CPR": cpr,
        "Navn": name,
        "E-mail": email or "",
        "Mobil": phone or "",
        "Stilling": None,  # To be populated later
    }
    return row


def export_bruger_lc(settings: Settings, node, used_cprs, lc, lc_historic):
    # TODO: If this is to run faster, we need to pre-sort into units,
    # to avoid iterating all engagements for each unit.
    lora_engagements = lc_historic.engagements.values()
    lora_engagements = flatten(lora_engagements)
    lora_engagements = filter(lambda engv: engv["unit"] == node.name, lora_engagements)
    lora_engagements = filter(
        lambda engv: UUID(engv["engagement_type"])
        in settings.exporters_plan2learn_allowed_engagement_types,
        lora_engagements,
    )
    lora_user_uuids = map(itemgetter("user"), lora_engagements)
    rows = []
    for user_uuid in lora_user_uuids:
        try:
            user = lc.users[user_uuid][0]
        except KeyError:
            logger.info(f"{user_uuid} not found")
            # Bandaid: sometimes we have a user_uuid that is not in lc.users as
            # the state is being changed while this program is running.
            continue
        cpr = user["cpr"]
        if cpr in used_cprs:
            # print('Skipping user: {} '.format(uuid))
            continue
        used_cprs.add(cpr)
        name = user["navn"]

        _phone_obj = get_filtered_phone_addresses(
            user_uuid, settings.plan2learn_phone_priority, lc_historic
        )

        _phone = None
        if _phone_obj:
            _phone = _phone_obj["value"]

        _email_obj = get_email_addresses(
            user_uuid, settings.plan2learn_email_priority, lc_historic, lc
        )

        _email = None
        if _email_obj:
            _email = _email_obj["value"]
        bruger_row = construct_bruger_row(user_uuid, cpr, name, _email, _phone)

        if settings.plan2learn_variant == Variant.rsd:
            # For Viborg this is handled during "export_engagements"
            user_engagements = [
                e for e in flatten(lc.engagements.values()) if e["user"] == user_uuid
            ]
            # Ensure the same engagement is selected each time by sorting on user_key
            # Assumes the user-key has a prefix of a 2 digit institution identifier
            # followed by a dash and then the engagement-id eg. AB-1234
            bruger_row["Stilling"] = None
            if user_engagements:
                user_engagement = min(user_engagements, key=lambda e: e["user_key"][3:])
                bruger_row["Stilling"] = user_engagement["extensions"]["udvidelse_1"]

        rows.append(bruger_row)
    return rows, used_cprs


def export_bruger_mo(settings: Settings, node, used_cprs, mh):
    employees = mh.read_organisation_people(
        node.name, split_name=False, read_all=True, skip_past=True
    )
    rows = []
    for uuid, employee in employees.items():
        if (
            UUID(employee["engagement_type_uuid"])
            not in settings.exporters_plan2learn_allowed_engagement_types
        ):
            continue
        user_uuid = employee["Person UUID"]
        name = employee["Navn"]
        cpr = employee["CPR-Nummer"]  # noqa: F821
        if cpr in used_cprs:
            # print('Skipping user: {} '.format(uuid))
            continue
        used_cprs.add(cpr)

        _phone_obj = get_e_address_mo(user_uuid, "PHONE", mh, settings)
        _phone = None
        if _phone_obj:
            _phone = _phone_obj["value"]

        _email_obj = get_e_address_mo(user_uuid, "EMAIL", mh, settings)
        _email = None
        if _email_obj:
            _email = _email_obj["value"]

        rows.append(construct_bruger_row(user_uuid, cpr, name, _email, _phone))
    return rows, used_cprs


def export_bruger(settings: Settings, mh, nodes, lc, lc_historic):
    #  fieldnames = ['BrugerId', 'CPR', 'Navn', 'E-mail', 'Mobil', 'Stilling']
    if lc and lc_historic:
        bruger_exporter = partial(export_bruger_lc, lc=lc, lc_historic=lc_historic)
    else:
        bruger_exporter = partial(export_bruger_mo, mh=mh)

    used_cprs: set[str] = set()
    rows = []
    for node in PreOrderIter(nodes["root"]):
        new_rows, used_cprs = bruger_exporter(
            settings=settings, node=node, used_cprs=used_cprs
        )
        rows.extend(new_rows)

    # Turns out, we need to update this once we reach engagements
    # mh._write_csv(fieldnames, rows, filename)
    return rows


def _split_dar(address):
    gade = post = by = ""
    if address:
        gade = address.split(",")[0]
        post = address.split(",")[1][1:5]
        by = address.split(",")[1][6:]
    return gade, post, by


def export_organisation(settings: Settings, mh, nodes, lc=None) -> list[dict]:
    rows = []
    for node in PreOrderIter(nodes["root"]):
        if lc:
            for unit in lc.units.values():
                # Units are never terminated, we can safely take first value
                unitv = unit[0]
                if unitv["uuid"] != node.name:
                    continue

                level_uuid = unitv["level"]
                level_titel = lc.classes[level_uuid]["title"] if level_uuid else ""
                too_deep = settings.integrations_SD_Lon_import_too_deep
                if level_titel in too_deep:
                    continue

                over_uuid = unitv["parent"] if unitv["parent"] else ""

                address = None
                for raw_address in lc.addresses.values():
                    if raw_address[0]["unit"] == unitv["uuid"]:
                        if raw_address[0]["scope"] == "DAR":
                            address = raw_address[0]["value"]

                gade, post, by = _split_dar(address)
                row = {
                    "AfdelingsID": unit[0]["uuid"],
                    "Afdelingsnavn": unit[0]["name"],
                    "Parentid": over_uuid,
                    "Gade": gade,
                    "Postnr": post,
                    "By": by,
                }
                rows.append(row)

        else:
            ou = mh.read_ou(node.name)
            level = ou["org_unit_level"]
            if level and level["name"] in settings.integrations_SD_Lon_import_too_deep:
                continue

            over_uuid = ou["parent"]["uuid"] if ou["parent"] else ""

            dar_address = mh.read_ou_address(node.name)
            gade, post, by = _split_dar(dar_address.get("Adresse"))

            row = {
                "AfdelingsID": ou["uuid"],
                "Afdelingsnavn": ou["name"],
                "Parentid": over_uuid,
                "Gade": gade,
                "Postnr": post,
                "By": by,
            }
            rows.append(row)

    return rows


def update_user_positions_viborg(brugere_rows, employee, engv, lc):
    for bruger in brugere_rows:
        if bruger["BrugerId"] == employee["uuid"]:
            # extension_3 from the job-function-configurator repo.
            udvidelse_3 = engv["extensions"].get("udvidelse_3")
            if udvidelse_3:
                bruger["Stilling"] = udvidelse_3
            else:
                job_function = engv["job_function"]
                stilling = lc.classes[job_function]["title"]
                bruger["Stilling"] = stilling


def find_start_date_viborg(active: bool, engv: dict) -> str:
    if active:
        return ""
    return engv["from_date"]


async def find_start_date_rsd(session: AsyncClientSession, uuid: str) -> str | None:
    res = await session.execute(
        gql("""query EngagementStartdate($uuids: [UUID!]) {
                  engagements(filter: { uuids: $uuids, from_date: null, to_date: null }) {
                    objects {
                      validities {
                        validity {
                          from
                        }
                      }
                    }
                  }
                }
                """),
        variable_values={"uuids": [uuid]},
    )
    objects = res["engagements"]["objects"]
    engagement = only(
        objects, too_long=ValueError(f"Multiple objects found for uuid={uuid}")
    )
    if engagement is None:
        # This can happen if an engagement is terminated or similar after the
        # loracache has been built, but before this runs.
        return None
    return min([e["validity"]["from"] for e in engagement["validities"]])


async def find_start_date(
    settings: Settings, active: bool, engv: dict, session: AsyncClientSession
) -> str | None:
    if settings.plan2learn_variant == Variant.viborg:
        return find_start_date_viborg(active, engv)
    elif settings.plan2learn_variant == Variant.rsd:
        return await find_start_date_rsd(session=session, uuid=engv["uuid"])
    else:
        raise NotImplementedError()


async def engagement_row(
    settings,
    session,
    employee_effects,
    lc,
    lc_historic,
    eksporterede_afdelinger,
    allowed_engagement_types,
    exported_engagements,
    mh,
    brugere_rows,
):
    err_msg = "Skipping {}, due to non-allowed engagement type"
    rows = []
    for eng in lc_historic.engagements.values():
        # We can consistenly access index 0, the historic export
        # is for the purpose of catching future engagements, not
        # to catch all validities
        engv = eng[0]

        # As this is not the historic cache, there should only be one user
        employee = employee_effects[0]

        if engv["user"] != employee["uuid"]:
            continue

        if engv["unit"] not in eksporterede_afdelinger:
            msg = "Unit {} is not included in the export"
            logger.info(msg.format(engv["unit"]))
            continue

        if UUID(engv["engagement_type"]) not in allowed_engagement_types:
            logger.debug(err_msg.format(eng))
            continue

        if engv["uuid"] in exported_engagements:
            continue
        exported_engagements.append(engv["uuid"])

        valid_from = datetime.datetime.strptime(engv["from_date"], "%Y-%m-%d")
        active = valid_from < datetime.datetime.now()

        start_dato = await find_start_date(settings, active, engv, session)
        if start_dato is None:
            continue
        # Currently we always set engagment to active, even if it is not.
        aktiv_status = 1

        if engv["uuid"] in lc.engagements:
            primary = lc.engagements[engv["uuid"]][0]["primary_boolean"]
        else:
            # This is a future engagement, we accept that the LoRa cache will
            # not provide the answer and search in MO.
            mo_engagements = mh.read_user_engagement(
                employee["uuid"],
                read_all=True,
                skip_past=True,
                calculate_primary=True,
            )
            primary = None
            for mo_eng in mo_engagements:
                if mo_eng["uuid"] == engv["uuid"]:
                    primary = mo_eng["is_primary"]
            if primary is None:
                msg = "Warning: Unable to find primary for {}!"
                logger.warning(msg.format(engv["uuid"]))
                print(msg.format(engv["uuid"]))
                primary = False
        if primary and settings.plan2learn_variant == Variant.viborg:
            primær = 1
            # Updates "brugere_rows" with "stilling".
            # Only for Viborg as this is handled differently for RSD
            update_user_positions_viborg(brugere_rows, employee, engv, lc)
        else:
            primær = 0

        stilingskode_id = engv["job_function"]
        ACTIVE_JOB_FUNCTIONS.append(stilingskode_id)
        eng_type = lc.classes[engv["engagement_type"]]["title"]

        row = {
            "EngagementId": engv["uuid"],
            "BrugerId": employee["uuid"],
            "AfdelingsId": engv["unit"],
            "AktivStatus": aktiv_status,
            "StillingskodeId": stilingskode_id,
            "Primær": primær,
            "Engagementstype": eng_type,
            "StartdatoEngagement": start_dato,
        }
        rows.append(row)
    return rows


async def export_engagement(
    settings: Settings,
    mh,
    eksporterede_afdelinger,
    brugere_rows,
    lc: GQLLoraCache,
    lc_historic: GQLLoraCache,
) -> list[dict]:
    allowed_engagement_types = settings.exporters_plan2learn_allowed_engagement_types
    # Keep a list of exported engagements to avoid exporting the same engagment
    # multiple times if it has multiple rows in MO.
    exported_engagements: list[str] = []
    async with lc.make_client() as session:
        rows = await gather_with_concurrency(
            100,
            *[
                engagement_row(
                    settings,
                    session,
                    employee_effects,
                    lc,
                    lc_historic,
                    eksporterede_afdelinger,
                    allowed_engagement_types,
                    exported_engagements,
                    mh,
                    brugere_rows,
                )
                for employee_effects in lc.users.values()
            ],
        )

    return [r for r in flatten(rows) if r is not None]


def export_stillingskode(mh, lc=None) -> list[dict]:
    rows = []
    if lc:
        job_function_facet = None
        for uuid, facet in lc.facets.items():
            if facet["user_key"] == "engagement_job_function":
                job_function_facet = uuid
        assert uuid is not None

        for klasse in lc.classes:
            if klasse["facet"] is not job_function_facet:
                continue

            if klasse["uuid"] not in ACTIVE_JOB_FUNCTIONS:
                continue

            row = {
                "StillingskodeID": klasse["uuid"],
                "AktivStatus": 1,
                "Stillingskode": klasse["title"],
                "Stillingskode#": klasse["uuid"],
            }
            rows.append(row)
    else:
        stillinger = mh.read_classes_in_facet("engagement_job_function")

        for stilling in stillinger[0]:
            if stilling["uuid"] not in ACTIVE_JOB_FUNCTIONS:
                continue

            row = {
                "StillingskodeID": stilling["uuid"],
                "AktivStatus": 1,
                "Stillingskode": stilling["name"],
                "Stillingskode#": stilling["uuid"],
            }
            rows.append(row)
    return rows


def export_leder_viborg(mh: MoraHelper, nodes, eksporterede_afdelinger):
    rows = []
    for node in PreOrderIter(nodes["root"]):
        if node.name not in eksporterede_afdelinger:
            # Denne afdeling er ikke med i afdelingseksport.
            continue

        manager = mh.read_ou_manager(node.name, inherit=False)
        if "uuid" in manager:
            row = {
                "BrugerId": manager.get("uuid"),
                "AfdelingsID": node.name,
                "AktivStatus": 1,
                "Titel": manager["Ansvar"],
            }
            rows.append(row)
    return rows


def export_leder_rsd(nodes, eksporterede_afdelinger, lc: GQLLoraCache):
    rows = []
    for node in PreOrderIter(nodes["root"]):
        if node.name not in eksporterede_afdelinger:
            # Denne afdeling er ikke med i afdelingseksport.
            continue

        managers = [
            manager
            for manager in flatten(lc.managers.values())
            if manager["unit"] == node.name
        ]
        if not managers:
            continue
        for manager in managers:
            if not manager["user"]:
                # Manager-role is vacant
                continue

            # If manager has more than one responsibility choose "Personaleledelse"
            responsibility = (
                max(
                    manager["manager_responsibility"],
                    key=lambda r: r == "Personaleledelse",
                )
                if manager["manager_responsibility"]
                else None
            )
            responsibility_name = (
                lc.classes[responsibility]["title"] if responsibility else ""
            )
            row = {
                "BrugerId": manager["user"],
                "AfdelingsID": node.name,
                "AktivStatus": "1",
                "Titel": responsibility_name,
                "OrganisationsfunktionsUUID": manager["uuid"],
            }
            rows.append(row)
    return rows


def export_leder(
    settings: Settings, nodes, eksporterede_afdelinger, mh: MoraHelper, lc: GQLLoraCache
):
    manager_titles = ["BrugerId", "AfdelingsID", "AktivStatus", "Titel"]
    if settings.plan2learn_variant == Variant.rsd:
        manager_titles.append("OrganisationsfunktionsUUID")
        rows = export_leder_rsd(nodes, eksporterede_afdelinger, lc)
    elif settings.plan2learn_variant == Variant.viborg:
        rows = export_leder_viborg(mh, nodes, eksporterede_afdelinger)
    else:
        raise NotImplementedError()
    return rows, manager_titles


def main(speedup, settings: Settings, dry_run=None):
    mh = MoraHelper(hostname=settings.mora_base)

    root_unit = str(settings.exporters_plan2learn_root_unit)

    if speedup:
        # Here we should activate read-only mode, actual state and
        # full history dumps needs to be in sync.

        # Full history does not calculate derived data, we must
        # fetch both kinds.
        lc = LoraCache(resolve_dar=True, full_history=False)
        lc.populate_cache(dry_run=dry_run, skip_associations=True)
        lc.calculate_derived_unit_data()
        lc.calculate_primary_engagements()

        lc_historic = LoraCache(resolve_dar=False, full_history=True, skip_past=True)
        lc_historic.populate_cache(dry_run=dry_run, skip_associations=True)
        # Here we should de-activate read-only mode
    else:
        lc = None
        lc_historic = None

    # Todo: We need the nodes structure to keep a consistent output,
    # consider if the 70 seconds is worth the implementation time of
    # reading this from cache.
    nodes = mh.read_ou_tree(root_unit)

    # read data-rows

    brugere_rows = export_bruger(settings, mh, nodes, lc, lc_historic)
    org_rows = export_organisation(settings, mh, nodes, lc)
    # Vi laver en liste over eksporterede afdelinger, så de som ikke er eksporterede
    # men alligevel har en leder, ignoreres i lederutrækket (typisk NY1 afdelinger).
    eksporterede_afdelinger = [r["AfdelingsID"] for r in org_rows]

    engagement_rows = asyncio.run(
        export_engagement(
            settings,
            mh,
            eksporterede_afdelinger,
            brugere_rows,
            lc,
            lc_historic,
        )
    )

    # TODO: Why is lc not passed into export_stillingskode?
    stillingskode_rows = export_stillingskode(mh)

    manager_rows, manager_titles = export_leder(
        settings, nodes, eksporterede_afdelinger, mh=mh, lc=lc
    )

    def upload(settings, filename, fieldnames, rows):
        with file_uploader(
            settings, filename, timeout=settings.file_upload_timeout
        ) as f:
            mh._write_csv(fieldnames, rows, f)

    upload(
        settings,
        "plan2learn_organisation.csv",
        ["AfdelingsID", "Afdelingsnavn", "Parentid", "Gade", "Postnr", "By"],
        org_rows,
    )

    upload(
        settings,
        "plan2learn_engagement.csv",
        [
            "EngagementId",
            "BrugerId",
            "AfdelingsId",
            "AktivStatus",
            "StillingskodeId",
            "Primær",
            "Engagementstype",
            "StartdatoEngagement",
        ],
        engagement_rows,
    )

    upload(
        settings,
        "plan2learn_stillingskode.csv",
        ["StillingskodeID", "AktivStatus", "Stillingskode", "Stillingskode#"],
        stillingskode_rows,
    )

    upload(
        settings,
        "plan2learn_leder.csv",
        manager_titles,
        manager_rows,
    )

    # Now exported the now fully populated brugere.csv

    upload(
        settings,
        "plan2learn_bruger.csv",
        ["BrugerId", "CPR", "Navn", "E-mail", "Mobil", "Stilling"],
        brugere_rows,
    )

    logger.info("Export completed")


@click.command()
@click.option(
    "--lora/--mo",
    "backend",
    required=True,
    default=None,
    help="Choose backend",
)
@click.option("--read-from-cache", is_flag=True, envvar="USE_CACHED_LORACACHE")
@click.option("--kubernetes", is_flag=True, envvar="KUBERNETES")
@click.option("--ship-files", is_flag=True, envvar="SHIP_FILES")
def cli(**args):
    logger.info("Starting with args: %r", args)
    settings = get_unified_settings(kubernetes_environment=args["kubernetes"])
    if args["backend"]:
        # True -> use LoRa
        main(settings=settings, speedup=True, dry_run=args["read_from_cache"])
    else:
        # False -> use MO
        main(settings=settings, speedup=False)
    if args["ship_files"]:
        ship_files(settings=settings)


if __name__ == "__main__":
    cli()
