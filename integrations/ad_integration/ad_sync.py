import logging
from datetime import datetime
from functools import partial
from operator import itemgetter
from typing import Any
from typing import Dict
from typing import Iterator
from typing import Optional
from typing import Tuple

import click
import sentry_sdk
from fastramqpi.ra_utils.apply import apply
from fastramqpi.ra_utils.jinja_filter import create_filters
from fastramqpi.ra_utils.load_settings import load_settings
from fastramqpi.ra_utils.tqdm_wrapper import tqdm
from more_itertools import only
from more_itertools import partition

from exporters.sql_export.lora_cache import get_cache as LoraCache
from integrations.mora_helper import MoraHelper

from .ad_logger import start_logging
from .ad_reader import ADParameterReader

logger = logging.getLogger("AdSyncRead")


# how to check these classes for noobs
# look at :https://os2mo-test.holstebro.dk/service/o/ORGUUID/f/
# It must be addresses, so we find the address thing for employees
# https://os2mo-test.holstebro.dk/service/o/ORGUUID/f/employee_address_type/
# There You have it - for example the mobile phone
# Now You may wonder if the VISIBLE/SECRET are right:
# Find them here https://os2mo-test.holstebro.dk/service/o/ORGUUID/f/visibility/


# AD has no concept of temporality, validity is always from now to infinity.
VALIDITY = {"from": datetime.strftime(datetime.now(), "%Y-%m-%d"), "to": None}


seeded_create_filters = partial(create_filters, tuple_keys=("uuid", "ad_object"))


class ConfigurationError(Exception):
    pass


class AddressDecisionList:
    """Given an AD object and a set of MO adresses, an instance of this class
    represents the updates required to synchronize the MO address data with
    the current state in the AD object.

    Each `AddressDecisionList` instance is an iterable over one or more
    decisions. Each decision is a tuple of `(decision_type, address, *args).`
    Client code can iterate over the list of decisions and effectuate MO API
    calls based on their decision type.

    :param uuid: uuid of the user
    :param ad_object: AD object for user
    :param user_addresses: MO addresses for user
    :param address_mapping: The contents of "user_addresses" in setting
    "ad_mo_sync_mapping"
    :param visibility: The contents of `AdMoSync.visibility`
    """

    # Decision types
    CREATE = "create"
    EDIT = "edit"
    TERMINATE = "terminate"

    def __init__(
        self,
        uuid: str,
        ad_object: dict,
        user_addresses: list,
        address_mapping: dict,
        visibility: dict,
    ):
        self._uuid = uuid
        self._ad_object = ad_object
        self._user_addresses = user_addresses
        self._address_mapping = address_mapping
        self._visibility = visibility
        self._decisions = self._build()

    def __iter__(self) -> Iterator[Tuple[str, Optional[Dict], Optional[Any]]]:
        return self._decisions

    def _build(self):
        for field, (
            address_type_uuid,
            visibility_uuid,
        ) in self._address_mapping.items():
            user_addresses = list(
                filter(
                    partial(self._match_address, address_type_uuid, visibility_uuid),
                    self._user_addresses,
                )
            )

            # No corresponding MO addresses found, let's create one
            if not user_addresses:
                # The field exists in AD, but there's no corresponding MO
                # address.
                if self._ad_object.get(field):
                    # Yield a MO address creation.
                    yield (
                        self.CREATE,
                        None,  # empty MO address
                        self._uuid,
                        self._ad_object[field],
                        (address_type_uuid, visibility_uuid),
                    )
                    continue

            # Field not in AD, terminate all corresponding MO addresses
            if self._ad_object.get(field) is None:
                for address in user_addresses:
                    yield (self.TERMINATE, address)
                continue

            # At this point we know that AD has a value for this field.
            # Only process the MO addresses that differ from AD.
            differing_addresses = filter(
                partial(self._mo_and_ad_differs, field),
                user_addresses,
            )

            # First element in `differing_addresses` is edited, and all other
            # elements are terminated.
            first_address = next(differing_addresses, None)
            if first_address:
                yield (
                    self.EDIT,
                    first_address,
                    self._ad_object[field],
                    (address_type_uuid, visibility_uuid),
                )
            for address in differing_addresses:
                yield (self.TERMINATE, address)

    def _mo_and_ad_differs(self, field: str, address: dict) -> bool:
        value_differs = address["value"] != self._ad_object[field]

        _, visibility_scope = self._address_mapping[field]
        current_visibility = (
            address["visibility"]["uuid"] if address.get("visibility") else None
        )
        target_visibility_class = (
            self._visibility[visibility_scope] if visibility_scope is not None else None
        )

        visibility_differs = current_visibility != target_visibility_class

        return value_differs or visibility_differs

    def _match_address(
        self, address_type_uuid: str, visibility_uuid: str, address: dict
    ) -> bool:
        return address is not None and self._match_address_type_uuid(
            address_type_uuid, address
        )

    def _match_address_type_uuid(self, address_type_uuid, address) -> bool:
        # Filter out addresses with wrong type
        return address["address_type"]["uuid"] == address_type_uuid


class AdMoSync:
    def __init__(self, all_settings=None):
        logger.info("AD Sync Started")
        self._setup_settings(all_settings)  # Populates `self.settings`
        self.lc = self._setup_lora_cache()  # Depends on `self.settings`
        self.helper = self._setup_mora_helper()  # Depends on `self.settings`
        self._setup_visibilities()  # Depends on `self.settings` and `self.helper`
        self.org = self.helper.read_organisation()

    def _setup_settings(self, all_settings):
        self.settings = all_settings
        if self.settings is None:
            self.settings = load_settings()

    def _setup_visibilities(self):
        mo_visibilities = self.helper.read_classes_in_facet("visibility")[0]
        self.visibility = {
            "PUBLIC": self.settings["address.visibility.public"],
            "INTERNAL": self.settings["address.visibility.internal"],
            "SECRET": self.settings["address.visibility.secret"],
        }

        # Check that the configured visibilities are found in MO
        configured_visibilities = set(self.visibility.values())
        mo_visibilities = set(map(itemgetter("uuid"), mo_visibilities))
        # If the configured visibiltities are not a subset, at least one is missing.
        if not configured_visibilities.issubset(mo_visibilities):
            raise Exception("Error in visibility class configuration")

    def _setup_mora_helper(self):
        return MoraHelper(hostname=self.settings["mora.base"], use_cache=False)

    def _setup_lora_cache(self):
        # Possibly get IT-system directly from LoRa for better performance.
        lora_speedup = self.settings.get(
            "integrations.ad.ad_mo_sync_direct_lora_speedup", False
        )
        if lora_speedup:
            print("Retrieve LoRa dump")
            lc = LoraCache(resolve_dar=False, full_history=False)
            lc.populate_cache(skip_associations=True, dry_run=False)
            # skip reading lora - not for prod
            # lc.populate_cache(dry_run=True, skip_associations=True)
            lc.calculate_primary_engagements()
            print("Done")
            return lc
        print("Use direct MO access")
        return None

    def _read_all_mo_users(self):
        """Return a list of all employees in MO.

        :return: List af all employees.
        """
        logger.info("Read all MO users")
        if self.lc:
            employees = [
                employee[0] for employee in self.lc.users.values() if len(employee) > 0
            ]
        else:
            employees = self.helper.read_all_users()
        logger.info("Done reading all MO users")
        return employees

    def _read_itconnections_raw(self, uuid, it_system_uuid=None):
        logger.debug(f"Read it-system for user {uuid}")
        if self.lc:
            itconnections = map(itemgetter(0), self.lc.it_connections.values())
            itconnections = filter(lambda it: it["user"] == uuid, itconnections)
            if it_system_uuid:
                itconnections = filter(
                    lambda it: it["itsystem"] == it_system_uuid, itconnections
                )
        else:
            itconnections = self.helper.get_e_itsystems(uuid, it_system_uuid)
        return itconnections

    def _read_itconnections(self, uuid, it_system_uuid=None):
        # Figure out which fields to extract from the it-system
        # Differs by source, as LoraCache and MO are not equivalent!
        extractor = itemgetter("user_key", "uuid")
        if self.lc:
            extractor = itemgetter("username", "uuid")
        # Fetch itconnections and extract fields
        itconnections = self._read_itconnections_raw(uuid, it_system_uuid)
        itconnections = map(extractor, itconnections)
        return itconnections

    def _get_address_decision_list(self, uuid, ad_object):
        """Construct a `AddressDecisionList` instance for `ad_object`

        :param uuid: uuid of the user
        :param ad_object: AD object for user
        :return: An `AddressDecisionList` instance
        """

        def to_mo_address(addr):
            return {
                "uuid": addr["uuid"],
                "address_type": {"uuid": addr["adresse_type"]},
                "visibility": {"uuid": addr["visibility"]},
                "value": addr["value"],
                "validity": {
                    "from": addr["from_date"],
                    "to": addr["to_date"],
                },
            }

        # Populate list of `user_addresses`
        if self.lc:
            # Retrieve user addresses from LoraCache
            user_addresses = self.lc.addresses.values()
            user_addresses = map(itemgetter(0), user_addresses)
            user_addresses = filter(lambda addr: addr["user"] == uuid, user_addresses)
            user_addresses = map(to_mo_address, user_addresses)
            user_addresses = list(user_addresses)
        else:
            # Retrieve user addresses from MO
            user_addresses = self.helper.get_e_addresses(uuid)

        return AddressDecisionList(
            uuid,
            ad_object,
            user_addresses,
            self.mapping["user_addresses"],
            self.visibility,
        )

    def _create_address(self, uuid, value, klasse):
        """Create a new address for a user.

        :param uuid: uuid of the user.
        :param: value Value of of the adress.
        :param: klasse: The address type and vissibility of the address.
        """
        payload = {
            "value": value,
            "address_type": {"uuid": klasse[0]},
            "person": {"uuid": uuid},
            "type": "address",
            "validity": VALIDITY,
            "org": {"uuid": self.org},
        }
        if klasse[1] is not None:
            payload["visibility"] = {"uuid": self.visibility[klasse[1]]}
        logger.debug("Create payload: {}".format(payload))
        response = self.helper._mo_post("details/create", payload)
        logger.debug("Response: {}".format(response.text))

    def _edit_address(self, address_uuid, value, klasse, validity=VALIDITY):
        """Edit an exising address to a new value.

        :param address_uuid: uuid of the address object.
        :param value: The new value
        :param: klasse: The address type and vissibility of the address.
        """
        payload = [
            {
                "type": "address",
                "uuid": address_uuid,
                "data": {
                    "validity": validity,
                    "value": value,
                    "address_type": {"uuid": klasse[0]},
                },
            }
        ]
        if klasse[1] is not None:
            payload[0]["data"]["visibility"] = {"uuid": self.visibility[klasse[1]]}

        logger.debug("Edit payload: {}".format(payload))
        response = self.helper._mo_post("details/edit", payload)
        logger.debug("Response: {}".format(response.text))

    def _edit_engagement(self, uuid: str, ad_object):
        if "engagements" not in self.mapping:
            return

        def _make_exclude_function(fieldspec):
            """Given a callable `fieldspec`, return a function which will
            return True if the date found by `fieldspec` is before today, and
            otherwise will return False.

            This is used to exclude engagements with a "from date" in the
            future.
            """

            threshold = datetime.now().date()

            def _func(engagement):
                value = fieldspec(engagement)
                if value is None:
                    return False  # from_date is infinitely far in the future
                if isinstance(value, str):
                    value = datetime.strptime(value, "%Y-%m-%d").date()
                return value < threshold

            return _func

        if self.lc:
            # Read user's current engagements, e.g. exclude engagements that
            # ended in the past.
            engagements = self.lc.engagements.values()
            engagements = map(itemgetter(0), engagements)
            engagements = filter(lambda eng: eng["user"] == uuid, engagements)
            # Skip engagements beginning in the future
            engagements = filter(
                _make_exclude_function(itemgetter("from_date")),
                list(engagements),
            )
            # Notice, this will only update current row, if more rows exists, they
            # will not be updated until the first run after that row has become
            # current. To fix this, we will need to ad option to LoRa cache to be
            # able to return entire object validity (poc-code exists).
        else:
            # Read user's current engagements, e.g. exclude engagements that
            # ended in the past.
            engagements = self.helper.read_user_engagement(
                uuid,
                calculate_primary=True,
                read_all=True,
                skip_past=True,
            )
            # Skip engagements beginning in the future
            engagements = filter(
                _make_exclude_function(lambda eng: eng["validity"]["from"]),
                list(engagements),
            )

        for engagement in engagements:
            if self.lc:
                ad_object_compare = ad_object if engagement["primary_boolean"] else {}
            else:
                ad_object_compare = ad_object if engagement["is_primary"] else {}

            to_date = (
                engagement["to_date"]
                if self.lc
                else engagement.get("validity", {}).get("to")
            )
            validity = {
                "from": VALIDITY["from"],  # today
                "to": to_date if to_date != "9999-12-31" else None,
            }
            self._edit_engagement_post_to_mo(
                uuid, ad_object_compare, engagement, validity
            )

    def _edit_engagement_post_to_mo(self, uuid, ad_object, mo_engagement, validity):
        # Populate `mo_data` with a value for each mapped field whose value has
        # changed when comparing the AD object to the current MO object.
        mo_data = {}
        for ad_field, mo_field in self.mapping["engagements"].items():
            # Default `mo_value` to an empty string. In case the field is
            # dropped from the AD object, this will empty its value in MO.
            new_mo_value = ad_object.get(ad_field, None)
            old_mo_value = mo_engagement.get(mo_field)

            # If we cannot read the field, maybe it is because our
            # mo_engagement is from LoraCache, and thus is different from MO
            # and must be read differently.
            if old_mo_value is None and "extensions" in mo_engagement:
                old_mo_value = self._edit_engagement_read_lc_extensions(
                    mo_field,
                    mo_engagement,
                )

            if old_mo_value == new_mo_value:
                logger.debug("No change, not editing engagement")
                continue

            mo_data[mo_field] = new_mo_value

        # Check that the loop added at least one key/value pair to `mo_data`.
        if not mo_data:
            logger.debug("Nothing to post, returning")
            return

        # Then, add the validity.
        mo_data["validity"] = validity

        payload = {
            "type": "engagement",
            "uuid": mo_engagement["uuid"],
            "data": mo_data,
        }
        logger.debug("Edit payload: %r", payload)
        response = self.helper._mo_post("details/edit", payload)
        self.stats["engagements"] += 1
        self.stats["users"].add(uuid)
        logger.debug("Response: %r", response.text)

    def _edit_engagement_read_lc_extensions(self, mo_field, mo_engagement):
        # TODO: This is specific to the LoraCache-based implementation (when
        # `self.lc` is not None.) Refactor to use a shared class for reading
        # engagements from LoraCache.
        field_mapping = {
            f"extension_{x}": mo_engagement["extensions"][f"udvidelse_{x}"]
            for x in range(1, 11)
        }
        if mo_field not in field_mapping:
            raise ConfigurationError("MO field %r is not mapped" % mo_field)
        return field_mapping[mo_field]

    def _create_it_system(self, person_uuid, ad_username, mo_itsystem_uuid):
        payload = {
            "type": "it",
            "user_key": ad_username,
            "itsystem": {"uuid": mo_itsystem_uuid},
            "person": {"uuid": person_uuid},
            "validity": VALIDITY,
        }
        logger.debug("Create it system payload: {}".format(payload))
        response = self.helper._mo_post("details/create", payload)
        logger.debug("Response: {}".format(response.text))
        response.raise_for_status()

    def _update_it_system(self, ad_username, binding_uuid):
        payload = {
            "type": "it",
            "data": {"user_key": ad_username, "validity": VALIDITY},
            "uuid": binding_uuid,
        }
        logger.debug("Update it system payload: {}".format(payload))
        response = self.helper._mo_post("details/edit", payload)
        logger.debug("Response: {}".format(response.text))
        response.raise_for_status()

    def _edit_it_system(self, uuid, ad_object):
        mo_itsystem_uuid = self.mapping["it_systems"]["samAccountName"]
        itconnections = self._read_itconnections(uuid, mo_itsystem_uuid)
        # Here it_systems is a 2 tuple (mo_username, binding_uuid)
        mo_username, binding_uuid = only(itconnections, ("", ""))
        # Username currently in AD
        ad_username = ad_object["SamAccountName"]

        # If mo_username is blank, we found a user who needs a new entry created
        if mo_username == "":
            self._create_it_system(uuid, ad_username, mo_itsystem_uuid)
            self.stats["it_systems"] += 1
            self.stats["users"].add(uuid)
        elif mo_username != ad_username:  # We need to update the mo_username
            self._update_it_system(ad_username, binding_uuid)
            self.stats["it_systems"] += 1
            self.stats["users"].add(uuid)

    def _edit_user_addresses(self, uuid, ad_object):
        decision_list = self._get_address_decision_list(uuid, ad_object)
        for decision, address, *args in decision_list:
            if decision == AddressDecisionList.CREATE:
                self._create_address(*args)
                # Update internal stats
                self.stats["addresses"][0] += 1
                self.stats["users"].add(uuid)
            elif decision == AddressDecisionList.EDIT:
                self._edit_address(address["uuid"], *args)
                # Update internal stats
                self.stats["addresses"][1] += 1
                self.stats["users"].add(uuid)
            elif decision == AddressDecisionList.TERMINATE:
                self._finalize_user_addresses_post_to_mo(address)
            else:
                raise ValueError(
                    "unknown decision %r (address=%r, args=%r)"
                    % (decision, address, args)
                )

    def _finalize_it_system(self, uuid):
        if "it_systems" not in self.mapping:
            return

        # Figure out how to find the itsystems uuid
        # Differs by source, as LoraCache and MO are not equivalent!
        def itsystem_uuid_extractor_mo(it):
            return it["itsystem"]["uuid"]

        def itsystem_uuid_extractor_lora(it):
            return it["itsystem"]

        itsystem_uuid_extractor = itsystem_uuid_extractor_mo
        if self.lc:
            itsystem_uuid_extractor = itsystem_uuid_extractor_lora

        it_system_uuids = self.mapping["it_systems"].values()

        def _keep_only_mapped(itconnection):
            itsystem_uuid = itsystem_uuid_extractor(itconnection)
            return itsystem_uuid in it_system_uuids

        def _keep_only_open(itconnection):
            # Figure out how to find the itsystem connection end-date
            # Differs by source, as LoraCache and MO are not equivalent!
            if self.lc:
                to_date = itconnection["to_date"]
            else:
                to_date = itconnection["validity"]["to"]
            return to_date is None

        itconnections = self._read_itconnections_raw(uuid)
        itconnections = filter(_keep_only_mapped, itconnections)
        itconnections = filter(_keep_only_open, itconnections)
        itconnections = map(itemgetter("uuid"), itconnections)

        today = datetime.strftime(datetime.now(), "%Y-%m-%d")
        for uuid in itconnections:
            payload = {
                "type": "it",
                "uuid": uuid,
                "validity": {"to": today},
            }
            logger.debug("Finalize payload: {}".format(payload))
            response = self.helper._mo_post("details/terminate", payload)
            logger.debug("Response: {}".format(response.text))

    def _finalize_user_addresses(self, uuid, ad_object):
        if "user_addresses" not in self.mapping:
            return

        @apply
        def _is_terminate(decision, address, *args):
            return decision == AddressDecisionList.TERMINATE

        @apply
        def _has_no_end_date(decision, address, *args):
            return address["validity"]["to"] is None

        @apply
        def _extract_address(decision, address, *args):
            return address

        decision_list = list(self._get_address_decision_list(uuid, ad_object))
        decision_list = filter(_is_terminate, decision_list)
        decision_list = filter(_has_no_end_date, decision_list)
        decision_list = map(_extract_address, decision_list)

        for address in decision_list:
            self._finalize_user_addresses_post_to_mo(address)

    def _finalize_user_addresses_post_to_mo(self, mo_address: dict):
        today = datetime.strftime(datetime.now(), "%Y-%m-%d")
        payload = {
            "type": "address",
            "uuid": mo_address["uuid"],
            "validity": {"to": today},
        }
        logger.debug("Finalize payload: {}".format(payload))
        response = self.helper._mo_post("details/terminate", payload)
        logger.debug("Response: {}".format(response.text))
        return response

    def _edit_user_attrs(self, employee, ad_object):
        user_attrs_mapping = self.mapping.get("user_attrs", {}).items()
        user_attrs_changed = {
            mo_field_name: ad_object.get(ad_field_name)
            for ad_field_name, mo_field_name in user_attrs_mapping
            if (
                ad_object.get(ad_field_name) is not None
                and ad_object[ad_field_name] != employee.get(mo_field_name)
            )
        }
        if user_attrs_changed:
            user_attrs_changed["validity"] = VALIDITY
            self.stats["users"].add(employee["uuid"])
            return self.helper.update_user(employee["uuid"], user_attrs_changed)

    def _terminate_single_user(self, uuid: str, ad_object: dict):
        self._finalize_it_system(uuid)
        self._finalize_user_addresses(uuid, ad_object)
        self._edit_engagement(uuid, {})

    def _update_single_user(
        self,
        employee,
        ad_object,
        terminate_disabled,
        terminate_disabled_filters,
    ):
        """Update all fields for a single user.

        :param employee: MO employee object (dict)
        :param ad_object: Dict with the AD information for the user.
        :param terminate_disabled: terminate *all* disabled users if True
        :param terminate_disabled_filters: list of filter functions
        """
        # If `user_attrs` is set up, transfer values from mapped AD user fields
        # to corresponding MO user attributes.
        if self.mapping.get("user_attrs"):
            self._edit_user_attrs(employee, ad_object)

        # Debug log if enabled is not found
        if "Enabled" not in ad_object:
            logger.info("Enabled not in ad_object")
        user_enabled = ad_object.get("Enabled", True)

        # If terminate_disabled is None, we decide on a per-user basis using the
        # terminate_disabled_filters, by invariant we at least one exist.
        if terminate_disabled is None:
            terminate_disabled = all(
                terminate_disabled_filter((employee["uuid"], ad_object))
                for terminate_disabled_filter in terminate_disabled_filters
            )

        # Check whether the current user is disabled, and terminate them, if we are
        # configured to terminate disabled users.
        if terminate_disabled and not user_enabled:
            # Set validity end --> today if in the future
            self._terminate_single_user(employee["uuid"], ad_object)
            return

        # Sync the user, whether disabled or not
        if "it_systems" in self.mapping:
            self._edit_it_system(employee["uuid"], ad_object)

        if "engagements" in self.mapping:
            self._edit_engagement(employee["uuid"], ad_object)

        if "user_addresses" in self.mapping:
            self._edit_user_addresses(employee["uuid"], ad_object)

    def _setup_ad_reader_and_cache_all(self, index, cache_all=True):
        ad_reader = ADParameterReader(index=index)
        print("Retrieve AD dump")
        if cache_all:
            ad_reader.cache_all(print_progress=True)
        print("Done")
        logger.info("Done with AD caching")
        return ad_reader

    def _verify_it_systems(self):
        """Verify that all configured it-systems exist."""
        if "it_systems" not in self.mapping:
            return

        # Set of UUIDs of all it_systems in MO
        mo_it_systems = set(map(itemgetter("uuid"), self.helper.read_it_systems()))

        @apply
        def filter_found(it_system, it_system_uuid):
            return it_system_uuid not in mo_it_systems

        # List of tuples (name, uuid) of it-systems configured in settings
        configured_it_systems = self.mapping["it_systems"].items()
        # Remove all the ones that exist in MO
        configured_it_systems = filter(filter_found, configured_it_systems)

        for it_system, it_system_uuid in configured_it_systems:
            msg = "{} with uuid {}, not found in MO"
            raise Exception(msg.format(it_system, it_system_uuid))

    def update_single_user(self, uuid):
        employees = [self.helper.read_user(uuid)]
        self._update_users(employees, ad_cache_all=False)

    def update_all_users(self):
        employees = self._read_all_mo_users()
        self._update_users(employees)

    def _update_users(self, employees_in, ad_cache_all=True):
        # Iterate over all AD's
        for index, _ in enumerate(self.settings["integrations.ad"]):
            employees = employees_in

            self.stats = {
                "ad-index": index,
                "addresses": [0, 0],
                "engagements": 0,
                "it_systems": 0,
                "users": set(),
            }

            ad_reader = self._setup_ad_reader_and_cache_all(
                index=index, cache_all=ad_cache_all
            )
            ad_settings = ad_reader._get_setting()

            # move to read_conf_settings og valider på tværs af alle-ad'er
            # så vi ikke overskriver addresser, itsystemer og extensionfelter
            # fra et ad med  med værdier fra et andet
            self.mapping = ad_settings["ad_mo_sync_mapping"]
            self._verify_it_systems()

            used_mo_fields = []

            for key in self.mapping.keys():
                for ad_field, mo_combi in self.mapping.get(key, {}).items():
                    if mo_combi in used_mo_fields:
                        msg = "MO field {} used more than once"
                        raise Exception(msg.format(mo_combi))
                    used_mo_fields.append(mo_combi)

            def add_employee_cpr(employee):
                """Convert an employee to a tuple (cpr, employee)."""
                if "cpr" in employee:
                    cpr = employee["cpr"]
                else:
                    uuid = employee["uuid"]
                    user = self.helper.read_user(uuid)
                    cpr = user.get("cpr_no")
                    if not cpr:
                        logger.warning("no 'cpr_no' for MO user %r", uuid)
                return cpr, employee

            @apply
            def cpr_uuid_to_uuid_ad(cpr, employee):
                ad_object = ad_reader.read_user(cpr=cpr, cache_only=ad_cache_all)
                return employee, ad_object

            @apply
            def filter_no_ad_object(employee, ad_object):
                return ad_object

            # Lookup filter jinja templates
            pre_filters = seeded_create_filters(ad_settings["ad_mo_sync_pre_filters"])
            terminate_disabled_filters = seeded_create_filters(
                ad_settings["ad_mo_sync_terminate_disabled_filters"]
            )
            # Lookup whether or not to terminate missing users
            terminate_missing = ad_settings["ad_mo_sync_terminate_missing"]
            # Decide whether missing users should only be terminated if and only if
            # they have an AD it system in their MO account.
            terminate_missing_require_itsystem = ad_settings[
                "ad_mo_sync_terminate_missing_require_itsystem"
            ]
            # Lookup whether or not to terminate disabled users
            terminate_disabled = ad_settings["ad_mo_sync_terminate_disabled"]

            # If not globally configured, and no user filters are configured either,
            # we default terminate_disabled to False
            if terminate_disabled is None and not terminate_disabled_filters:
                terminate_disabled = False

            # Iterate over all users and sync AD informations to MO.
            employees = map(add_employee_cpr, employees)
            employees = map(cpr_uuid_to_uuid_ad, employees)

            # Remove all entries without ad_object
            missing_employees, employees = partition(filter_no_ad_object, employees)

            # Run all pre filters
            for pre_filter in pre_filters:
                employees = filter(pre_filter, employees)

            # Call update_single_user on each remaining users
            print("Updating users")
            employees = list(employees)
            employees = tqdm(employees)
            for employee, ad_object in employees:
                # TODO: Convert this function into two seperate phases.
                # 1. A map from uuid, ad_object to mo_endpoints + mo_payloads
                # 2. Bulk updating of MO using the data from 1.
                self._update_single_user(
                    employee, ad_object, terminate_disabled, terminate_disabled_filters
                )

            # Call terminate on each missing user
            if terminate_missing:
                print("Terminating missing users")

                @apply
                def has_it_system(uuid, ad_object):
                    mo_itsystem_uuid = self.mapping["it_systems"]["samAccountName"]
                    itconnections = self._read_itconnections(uuid, mo_itsystem_uuid)
                    mo_username, _ = only(itconnections, ("", ""))
                    return mo_username != ""

                if terminate_missing_require_itsystem:
                    missing_employees = filter(has_it_system, missing_employees)

                missing_employees = list(missing_employees)
                missing_employees = tqdm(missing_employees)

                # TODO: Convert this function into two seperate phases.
                # 1. A map from uuid, ad_object to mo_endpoints + mo_payloads
                # 2. Bulk updating of MO using the data from 1.
                for mo_object, ad_object in missing_employees:
                    self._terminate_single_user(mo_object["uuid"], ad_object)

            logger.info("Stats: {}".format(self.stats))


@click.command()
@click.option(
    "--sync-user",
    help="Sync a single user.",
    type=click.UUID,
)
def sync(sync_user):
    sync = AdMoSync()

    if "crontab.SENTRY_DSN" in sync.settings:
        sentry_sdk.init(dsn=sync.settings["crontab.SENTRY_DSN"])

    if sync_user:
        sync.update_single_user(str(sync_user))
    else:
        sync.update_all_users()


if __name__ == "__main__":
    start_logging()
    sync()
