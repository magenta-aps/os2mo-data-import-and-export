# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
import datetime
import logging
import shutil
import sys
import time

import click
from fastramqpi.ra_utils.deprecation import deprecated
from fastramqpi.ra_utils.load_settings import load_settings
from fastramqpi.raclients.upload import file_uploader

from exporters.sql_export.lora_cache import get_cache as LoraCache
from exporters.utils.priority_by_class import lc_choose_public_address
from integrations.mora_helper import MoraHelper

logger = logging.getLogger("viborg_externe")


class ViborgEksterne:
    LOG_LEVEL = logging.DEBUG

    fieldnames = [
        "OrganisationsenhedUUID",
        "Organisationsenhed",
        "Enhedsnr",
        "Enhedstype",
        "Ledernavn",
        "Lederemail",
        "Tjenestenummer",
        "CPR-nummer",
        "Navn",
        "Engagementstype",
        "Startdato",
    ]

    def __init__(self):
        self._load_settings()
        self._configure_logging()

    def _load_settings(self):
        self.settings = load_settings()

    def _configure_logging(self):
        for name in logging.root.manager.loggerDict:
            if name in ("lora_cache", "mora-helper", "viborg_externe"):
                logging.getLogger(name).setLevel(self.LOG_LEVEL)
            else:
                logging.getLogger(name).setLevel(logging.ERROR)

        logging.basicConfig(
            format="%(levelname)s %(asctime)s %(name)s %(message)s",
            level=self.LOG_LEVEL,
            stream=sys.stdout,
        )

    def run(self, speedup=False, dry_run=True):
        mora_base = self.settings["mora.base"]
        if "exports_viborg_eksterne.outfile_basename" not in self.settings:
            print("Missing key in settings: exports_viborg_eksterne.outfile_basename")
            exit(1)
        outfile_name = self.settings["exports_viborg_eksterne.outfile_basename"]
        logger.info("writing to file %s", outfile_name)

        t = time.time()
        mh = MoraHelper(hostname=mora_base)

        if speedup:
            # Here we should activate read-only mode, actual state and
            # full history dumps needs to be in sync.

            # Full history does not calculate derived data, we must
            # fetch both kinds.
            lc = LoraCache(resolve_dar=True, full_history=False)
            lc.populate_cache(dry_run=dry_run, skip_associations=True)
            lc.calculate_derived_unit_data()

            lc_historic = LoraCache(
                resolve_dar=False, full_history=True, skip_past=True
            )
            lc_historic.populate_cache(dry_run=dry_run, skip_associations=True)
            # Here we should de-activate read-only mode
        else:
            lc = None
            lc_historic = None

        with file_uploader(self.settings, outfile_name) as filename:
            self.export_engagement(mh, filename, lc, lc_historic)
            # Copy for so we can upload with smb in job-runner.sh
            shutil.copyfile(filename, f"/tmp/{outfile_name}")
        logger.info("Time: {}s".format(time.time() - t))

        logger.info("Export completed")

    def export_engagement(self, mh: MoraHelper, filename, lc, lc_historic):
        rows = []

        logger.info("Reading users")
        if lc:
            employees = list(map(lambda x: x[0], lc.users.values()))
        else:
            employees = mh.read_all_users()

        logger.info("Reading engagements")
        # Todo: This O(#employees x #engagments), a pre-sorting of engagements would
        # make it O(#employees + #engagments) - consider if this is worth the effort
        for employee in employees:
            logger.info("employee: %r", employee)
            if lc:
                for row in self._gen_from_loracache(employee, lc, lc_historic):
                    rows.append(row)
            else:
                for row in self._gen_from_mo(employee, mh):
                    rows.append(row)

        mh._write_csv(self.fieldnames, rows, filename)

    def _get_disallowed_engagement_types(self):
        # Medarbejder (månedsløn) and Medarbejder (timeløn)
        return self.settings["exporters.plan2learn.allowed_engagement_types"]

    def _gen_from_loracache(self, employee, lc, lc_historic):
        for eng in filter(
            lambda x: x[0]["user"] == employee["uuid"], lc_historic.engagements.values()
        ):
            engv = eng[0]  # Historic information is here to catch future
            # engagements, not to use the actual historic information
            # if engv['user'] != employee['uuid']:
            #    continue
            if engv["engagement_type"] in self._get_disallowed_engagement_types():
                continue

            org_unit_uuid = engv["unit"]
            org_unit_name = lc.units[org_unit_uuid][0]["name"]
            org_unit_user_key = lc.units[org_unit_uuid][0]["user_key"]
            org_unit_type_uuid = lc.units[org_unit_uuid][0]["unit_type"]
            org_unit_type = lc.classes[org_unit_type_uuid]["title"]
            manager = lc.units[org_unit_uuid][0]["acting_manager_uuid"]

            if manager:
                manager_object = lc_historic.managers[manager][0]
                manager_name = lc.users[manager_object["user"]][0]["navn"]
                manager_email = ""

                manager_email_candidates = [
                    x[0]
                    for x in filter(
                        lambda x: (
                            x[0]["scope"] == "E-mail"
                            and x[0]["user"] == manager_object["user"]
                        ),
                        lc_historic.addresses.values(),
                    )
                ]

                chosen = lc_choose_public_address(
                    manager_email_candidates,
                    self.settings.get("exports_viborg_eksterne.email.priority", []),
                    lc,  # for class lookup
                )

                if chosen:
                    manager_email = chosen["value"]
            else:
                logger.warning(
                    "No manager found for org unit: {}".format(org_unit_uuid)
                )
                manager_name = ""
                manager_email = ""

            engagement_type = lc_historic.classes[engv["engagement_type"]]["title"]

            row = {
                "OrganisationsenhedUUID": org_unit_uuid,
                "Organisationsenhed": org_unit_name,
                "Enhedsnr": org_unit_user_key,
                "Enhedstype": org_unit_type,
                "Ledernavn": manager_name,
                "Lederemail": manager_email,
                "Tjenestenummer": engv["user_key"],
                "CPR-nummer": employee["cpr"],
                "Navn": employee["navn"],
                "Engagementstype": engagement_type,
                # 'Startdato': valid_from.strftime('%Y-%m-%d') + " 00:00:00",
                "Startdato": engv["from_date"] + " 00:00:00",
            }

            yield row

    @deprecated
    def _gen_from_mo(self, employee, mh):
        full_employee = mh.read_user(employee["uuid"])
        engagements = mh.read_user_engagement(
            employee["uuid"], read_all=True, skip_past=True
        )
        for eng in engagements:
            if (
                eng["engagement_type"]["uuid"]
                in self._get_disallowed_engagement_types()
            ):
                continue

            valid_from = datetime.datetime.strptime(eng["validity"]["from"], "%Y-%m-%d")

            org_unit_uuid = eng["org_unit"]["uuid"]
            manager = self._find_manager(org_unit_uuid, mh)
            if manager:
                manager_name = manager["person"]["name"]
                manager_email = self._find_manager_email(manager, mh)
            else:
                logger.warning(
                    "No manager found for org unit: {}".format(org_unit_uuid)
                )
                manager_name = ""
                manager_email = ""

            row = {
                "OrganisationsenhedUUID": org_unit_uuid,
                "Organisationsenhed": eng["org_unit"]["name"],
                "Enhedsnr": "Enhedsnr",
                "Enhedstype": "Enhedstype",
                "Ledernavn": manager_name,
                "Lederemail": manager_email,
                "Tjenestenummer": eng["user_key"],
                "CPR-nummer": full_employee["cpr_no"],
                "Navn": full_employee["name"],
                "Engagementstype": eng["engagement_type"]["name"],
                "Startdato": valid_from,
            }
            yield row

    def _find_manager(self, org_unit_uuid, mora_helper: MoraHelper):
        url = "ou/{}/details/manager"
        managers = mora_helper._mo_lookup(org_unit_uuid, url)
        responsibility_class = self.settings[
            "exporters.viborg.primary_manager_responsibility"
        ]

        for manager in managers:
            if responsibility_class in map(
                lambda x: x.get("uuid"), manager["responsibility"]
            ):
                return manager

        parent = mora_helper.read_ou(org_unit_uuid).get("parent")
        if not parent:
            return {}
        return self._find_manager(parent["uuid"], mora_helper)

    def _find_manager_email(self, manager, mora_helper: MoraHelper):
        person_uuid = manager.get("person").get("uuid")
        email = mora_helper.get_e_address(person_uuid, "EMAIL").get("value")
        return email


def main(speedup, dry_run=None):
    instance = ViborgEksterne()
    instance.run(speedup=speedup, dry_run=dry_run)


@click.command()
@click.option("--read-from-cache", is_flag=True, envvar="USE_CACHED_LORACACHE")
def cli(**args):
    logger.info("Starting with args: %r", args)
    main(speedup=True, dry_run=args["read_from_cache"])


if __name__ == "__main__":
    cli()
