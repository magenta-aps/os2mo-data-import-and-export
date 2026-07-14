import logging
import random
from operator import itemgetter

import click
import sentry_sdk
from click_option_group import RequiredMutuallyExclusiveOptionGroup
from click_option_group import optgroup
from fastramqpi.ra_utils.apply import apply
from fastramqpi.ra_utils.load_settings import load_settings
from fastramqpi.ra_utils.tqdm_wrapper import tqdm

from integrations.mora_helper import MoraHelper

from .ad_common import AD
from .ad_exceptions import ImproperlyConfigured
from .ad_logger import start_logging
from .ad_reader import ADParameterReader

logger = logging.getLogger("MoUuidAdSync")


class SyncMoUuidToAd(AD):
    """Syncronize MO UUIDs to AD.

    Walks through all users in AD, search in MO by cpr and writes the MO
    uuid on the users AD account.
    """

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._check_ad_uuid_field_is_configured()
        self.helper = self._get_mora_helper()
        self.org_uuid = self.helper.read_organisation()
        self.reader = ADParameterReader()
        self.stats = {
            "attempted_users": 0,
            "user_not_in_mo": 0,
            "already_ok": 0,
            "updated": 0,
        }

    def perform_sync(self, ad_users, mo_users):
        separator = self.all_settings["primary"].get("cpr_separator", "")
        cpr_field = self.all_settings["primary"]["cpr_field"]

        def extract_cpr(ad_user):
            self.stats["attempted_users"] += 1
            cpr = ad_user.get(cpr_field)
            if separator:
                cpr = cpr.replace(separator, "")
            return ad_user, cpr

        @apply
        def lookup_mo_uuid(ad_user, cpr):
            mo_uuid = mo_users.get(cpr)
            return ad_user, mo_uuid

        @apply
        def filter_unmatched_mo(ad_user, mo_uuid):
            if not mo_uuid:
                logger.info("cpr not in MO, sam {}".format(ad_user["SamAccountName"]))
                self.stats["user_not_in_mo"] += 1
                return False
            return True

        ad_uuid_field = self.settings["integrations.ad.write.uuid_field"]

        @apply
        def filter_already_synced(ad_user, mo_uuid):
            expected_mo_uuid = ad_user.get(ad_uuid_field)
            if expected_mo_uuid is None:
                msg = (
                    "Read None from UUID field in AD!"
                    + "Perhaps uuid_field or properties are misconfigured?"
                )
                logger.warning(msg)
            if expected_mo_uuid == mo_uuid:
                logger.info("uuid for {} correct in AD".format(mo_uuid))
                self.stats["already_ok"] += 1
                return False
            logger.debug(
                "uuid for {} not correct in AD: {}".format(mo_uuid, expected_mo_uuid)
            )
            return True

        server_strings = [""]
        if self.all_settings["global"].get("servers"):
            server_strings = [
                " -Server {} ".format(server)
                for server in self.all_settings["global"].get("servers")
            ]

        @apply
        def construct_powershell_script(ad_user, mo_uuid):
            logger.debug("Syncronizing uuid {} into AD".format(mo_uuid))
            server_string = random.choice(server_strings)
            ps_script = (
                self._build_user_credential()
                + "Get-ADUser "
                + server_string
                + " -Filter 'SamAccountName -eq \""
                + ad_user["SamAccountName"]
                + "\"' -Credential $usercredential | "
                + " Set-ADUser -Credential $usercredential "
                + ' -Replace @{"'
                + ad_uuid_field
                + '"="'
                + mo_uuid
                + '"} '
                + server_string
            )
            logger.debug("PS-script: {}".format(ps_script))
            return ps_script

        logger.info("Will now process {} users".format(len(ad_users)))

        users = tqdm(ad_users)
        users = map(extract_cpr, users)
        users = map(lookup_mo_uuid, users)
        users = filter(filter_unmatched_mo, users)
        users = filter(filter_already_synced, users)
        users = list(users)

        print(self.stats)
        logger.info(self.stats)

        logger.info("Will now attempt to sync {} users".format(len(users)))
        users = tqdm(users)
        users = map(construct_powershell_script, users)

        # Actually fire the powershell scripts, and trigger side-effects
        for ps_script in users:
            try:
                response = self._run_ps_script(ps_script)
            except Exception:
                logger.exception("failed to write MO UUID (ps_script=%r)", ps_script)
            else:
                logger.debug("Response: {}".format(response))
                if response:
                    msg = "Unexpected response: {}".format(response)
                    logger.exception(msg)
                    raise Exception(msg)
                self.stats["updated"] += 1
        print(self.stats)
        logger.info(self.stats)

    def sync_one(self, cprno):
        print("Fetch AD User")
        ad_user = self.reader.read_user(cpr=cprno)
        if not ad_user:
            msg = "AD User not found"
            logger.exception(msg)
            raise Exception(msg)

        print("Fetch MO User")
        mo_user = self.helper.read_user(user_cpr=cprno, org_uuid=self.org_uuid)
        try:
            mo_uuid = mo_user["uuid"]
        except (KeyError, TypeError):
            msg = "MO User not found"
            logger.exception(msg)
            raise Exception(msg)

        ad_users = [ad_user]
        mo_users = {cprno: mo_uuid}

        print("Starting Sync")
        self.perform_sync(ad_users, mo_users)

    def sync_all(self):
        def has_cpr_no(mo_user):
            if "cpr_no" not in mo_user:
                logger.warning("no 'cpr_no' for MO user %r", mo_user["uuid"])
                return False
            return True

        print("Fetch AD Users")
        ad_users = self.reader.read_it_all(print_progress=True)

        print("Fetch MO Users")
        mo_users = self.helper.read_all_users()
        mo_users = filter(has_cpr_no, mo_users)
        mo_users = dict(map(itemgetter("cpr_no", "uuid"), mo_users))

        print("Starting Sync")
        self.perform_sync(ad_users, mo_users)

    def _check_ad_uuid_field_is_configured(self):
        # Check configuration
        ad_uuid_field = self.settings["integrations.ad.write.uuid_field"]
        if not any(
            ad_uuid_field in ad_settings["properties"]
            for ad_settings in self.settings["integrations.ad"]
        ):
            msg = "'uuid_field' %r not in 'properties' of any AD"
            logger.warning(msg, ad_uuid_field)
            raise ImproperlyConfigured(msg % (ad_uuid_field))

    def _get_mora_helper(self):
        return MoraHelper(
            hostname=self.all_settings["global"]["mora.base"], use_cache=False
        )


@click.command()
@click.option(
    "--debug",
    help="Set logging level to DEBUG (default is INFO)",
    is_flag=True,
    default=False,
)
@optgroup.group("Action", cls=RequiredMutuallyExclusiveOptionGroup)
@optgroup.option("--sync-all", is_flag=True)
@optgroup.option("--sync-cpr")
def cli(**args):
    start_logging()

    # Set log level according to --debug command line arg
    logger.setLevel(logging.INFO)

    if args.get("debug"):
        logger.setLevel(logging.DEBUG)

    logger.debug(args)

    sync = SyncMoUuidToAd()

    if "crontab.SENTRY_DSN" in sync.settings:
        sentry_sdk.init(dsn=sync.settings["crontab.SENTRY_DSN"])

    if args.get("sync_all"):
        sync.sync_all()
    if args.get("sync_cpr"):
        sync.sync_one(args["sync_cpr"])
    logger.info("Sync done")


if __name__ == "__main__":
    cli()
