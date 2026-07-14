import copy
import json
import uuid
from contextlib import ExitStack
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

import requests
from winrm import Session

from integrations.mora_helper import MoraHelper

from ..ad_common import AD
from ..ad_writer import MORESTSource
from .test_utils import TestADWriterMixin

MO_ROOT_ORG_UNIT_UUID = "not-a-mo-org-unit-uuid"
MO_ROOT_ORG_UNIT_NAME = "not-a-mo-org-unit-name"
MO_USER_LOCATION = MO_ROOT_ORG_UNIT_NAME
MO_CHILD_ORG_UNIT_UUID = uuid4()

MO_UUID = "mo-user-uuid"
MO_USER_CPR = "mo-user-cpr"
MO_USER_SAM = "mo-user-sam"

MO_MANAGER_UUID = "mo-manager-uuid"
MO_MANAGER_CPR = "mo-manager-cpr"
MO_MANAGER_SAM = "mo-manager-sam"

AD_UUID_FIELD = "uuidField"
UNKNOWN_CPR_NO = "not-a-cpr-no"
MO_AD_IT_SYSTEM_UUID = uuid4()


class MockAD(AD):
    def __init__(self):
        self.session = Mock(spec=Session)
        self.all_settings = {"primary": {"search_base": ""}}


class MockADParameterReader(TestADWriterMixin):
    """Mock implementation of `ADParameterReader` which always returns the same
    AD user."""

    generate_dynamic_person = False

    def __init__(self, mo_uuid=MO_UUID):
        super().__init__()
        self._mo_uuid = mo_uuid

    def read_user(self, cpr=None, **kwargs):
        def _override_objectguid(ad_person):
            ad_person["ObjectGUID"] = self._mo_uuid
            ad_person["ObjectGuid"] = self._mo_uuid
            return ad_person

        return self._prepare_get_from_ad(ad_transformer=_override_objectguid)

    def read_it_all(self, **kwargs):
        return [self.read_user()]

    def cache_all(self, **kwargs):
        return self.read_it_all()

    def get_all_samaccountname_values(self):
        return {self.read_user()["SamAccountName"]}


class MockEmptyADReader(MockADParameterReader):
    """Mock implementation of `ADParameterReader` which simulates an empty AD"""

    def read_user(self, **kwargs):
        return None

    def read_it_all(self, **kwargs):
        return []

    def cache_all(self, **kwargs):
        return self.read_it_all()


class MockUnknownCPRADParameterReader(MockADParameterReader):
    def read_user(self, cpr=None, **kwargs):
        if cpr == UNKNOWN_CPR_NO:
            return None
        return super().read_user(cpr=cpr, **kwargs)


class MockADParameterReaderWithManager(MockADParameterReader):
    def read_user(self, cpr=None, **kwargs):
        if cpr == MO_MANAGER_CPR:
            return {
                "cpr_field": MO_MANAGER_CPR,
                "SamAccountName": MO_MANAGER_SAM,
                "DistinguishedName": "manager-dn",
            }
        else:
            return {
                "cpr_field": MO_USER_CPR,
                "SamAccountName": MO_USER_SAM,
                "manager": None,
            }


class MockOnlyCPRADParameterReader(MockADParameterReader):
    """Mock an `ADParameterReader` which only returns an AD user if given a CPR (and
    returns None if given a username.)
    """

    def read_user(self, cpr=None, user=None, **kwargs):
        if user:
            return None
        if cpr:
            return super().read_user(cpr=cpr, **kwargs)


class MockADParameterReaderWithMOUUID(MockADParameterReader):
    """Mock an `ADParameterReader` which returns an AD user whose `AD_FIELD_UUID` field
    contains `MO_UUID`.
    """

    def read_user(self, **kwargs):
        ad_user = super().read_user(**kwargs)
        ad_user[AD_UUID_FIELD] = MO_UUID
        return ad_user


class MockMORESTSource(MORESTSource):
    def __init__(self, from_date, to_date):
        self.from_date = from_date
        self.to_date = to_date

    def get_engagement_dates(self, uuid):
        # Return 2-tuple of (from_dates, to_dates)
        return [self.from_date], [self.to_date]


class MockMORESTSourcePreview(MORESTSource):
    def __init__(self):
        self.helper = MockMoraHelper(MO_USER_CPR)

    def find_primary_engagement(self, uuid):
        return "employment-number", "title", "eng-org-unit", "eng-uuid"

    def read_user(self, uuid):
        mo_user = copy.deepcopy(self.helper.read_user())
        mo_user.update(
            {
                "givenname": "Tester",
                "surname": "Testesen",
            }
        )
        if uuid == MO_MANAGER_UUID:
            mo_user.update(
                {
                    "uuid": MO_MANAGER_UUID,
                    "cpr_no": MO_MANAGER_CPR,
                    "name": "Leder Ledersen",
                }
            )
        return mo_user

    def get_engagement_dates(self, uuid):
        return [None], [None]


class MockLoraCache:
    # This implements enough of the real `LoraCache` to make
    # `ad_sync.AdMoSync._edit_engagement` happy.

    def __init__(self, mo_values, mo_engagements=None):
        self._mo_values = mo_values
        self._mo_engagements = mo_engagements

    @property
    def users(self):
        return {self._mo_values["uuid"]: [self._mo_values]}

    @property
    def engagements(self):
        extensions = {"udvidelse_%d" % n: "old mo value #%d" % n for n in range(1, 11)}
        if self._mo_engagements is not None:
            return {
                eng["uuid"]: [
                    {
                        "uuid": eng["uuid"],
                        "user": self._mo_values["uuid"],
                        "unit": MO_ROOT_ORG_UNIT_UUID,
                        "user_key": "engagement_%s" % self._mo_values["uuid"],
                        "primary_boolean": eng["is_primary"],
                        "from_date": eng["validity"]["from"],
                        "to_date": eng["validity"]["to"],
                        "extensions": extensions,
                        "job_function": eng.get("job_function", {}).get("uuid"),
                        "primary_type": None,
                        "engagement_type": None,
                    }
                ]
                for eng in self._mo_engagements
            }
        else:
            return {
                "engagement_uuid": [
                    {
                        "uuid": "engagement_uuid",
                        "user": self._mo_values["uuid"],
                        "unit": MO_ROOT_ORG_UNIT_UUID,
                        "user_key": "engagement_%s" % self._mo_values["uuid"],
                        "primary_boolean": True,
                        "from_date": "1960-01-01",
                        "to_date": None,
                        "extensions": extensions,
                        # Additional keys read by `AdLifeCycle._gen_filtered_employees`
                        "job_function": None,
                        "primary_type": None,
                        "engagement_type": None,
                    }
                ]
            }


class MockLoraCacheExtended(MockLoraCache):
    """Mocks enough of `LoraCache` to test `AdLifeCycle`"""

    def populate_cache(self, **kwargs):
        pass

    def calculate_derived_unit_data(self):
        pass

    def calculate_primary_engagements(self):
        pass

    @property
    def units(self):
        # Return a single org unit (= the root org unit)
        return {
            MO_ROOT_ORG_UNIT_UUID: [
                {
                    "uuid": MO_ROOT_ORG_UNIT_UUID,
                    "name": MO_ROOT_ORG_UNIT_NAME,
                    "location": MO_ROOT_ORG_UNIT_NAME,
                    "user_key": None,
                    "unit_type": None,
                    "level": None,
                    "parent": None,
                }
            ]
        }

    @property
    def classes(self):
        return {None: {"title": "not-a-title"}}

    def _load_settings(self):
        return {}

    def _read_org_uuid(self):
        return "not-a-mo-org-uuid"


class MockLoraCacheWithManager(MockLoraCacheExtended):
    def __init__(self):
        self._mo_values_employee = {
            "uuid": MO_UUID,
            "cpr": MO_USER_CPR,
            "navn": "Ansat Ansatsen",
            "efternavn": "Ansatsen",
            "fornavn": "Ansat",
            "kaldenavn": "Kaldenavn Ansatsen",
            "kaldenavn_fornavn": "Kaldenavn",
            "kaldenavn_efternavn": "Ansatsen",
        }
        self._mo_values_manager = {
            "uuid": MO_MANAGER_UUID,
            "cpr": MO_MANAGER_CPR,
            "navn": "Leder Ledersen",
            "efternavn": "Ledersen",
            "fornavn": "Leder",
            "kaldenavn": "Kaldenavn Ledersen",
            "kaldenavn_fornavn": "Kaldenavn",
            "kaldenavn_efternavn": "Ledersen",
        }
        super().__init__(mo_values=self._mo_values_employee)

    @property
    def users(self):
        return {
            self._mo_values_employee["uuid"]: [self._mo_values_employee],
            self._mo_values_manager["uuid"]: [self._mo_values_manager],
        }


class MockLoraCacheEmptyEmployee(MockLoraCacheExtended):
    @property
    def users(self):
        return {self._mo_values["uuid"]: []}


class MockLoraCacheEmptyUnit(MockLoraCacheExtended):
    """Mock a LoraCache where there are no organisational units"""

    @property
    def units(self):
        return {}


class MockLoraCacheDanglingParentUnit(MockLoraCacheExtended):
    """Mock a LoraCache where organisational unit we look for has an unknown
    parent organisational unit UUID.
    """

    @property
    def units(self):
        return {
            MO_CHILD_ORG_UNIT_UUID: [
                {
                    "uuid": MO_CHILD_ORG_UNIT_UUID,
                    "parent": uuid4(),
                }
            ],
        }


class MockLoraCacheParentChildUnit(MockLoraCacheExtended):
    """Mock a LoraCache where a child unit points correctly to its parent unit
    (which is also the root unit in this case.)
    """

    @property
    def units(self):
        return {
            MO_ROOT_ORG_UNIT_UUID: [
                {
                    "uuid": MO_ROOT_ORG_UNIT_UUID,
                    "parent": None,
                }
            ],
            MO_CHILD_ORG_UNIT_UUID: [
                {
                    "uuid": MO_CHILD_ORG_UNIT_UUID,
                    "parent": MO_ROOT_ORG_UNIT_UUID,
                }
            ],
        }


class MockLoraCacheParentUnitUnset(MockLoraCacheExtended):
    """Mock a LoraCache where a child unit does not point correctly to its
    parent unit, due to its 'parent' key being None.
    """

    @property
    def units(self):
        return {
            MO_ROOT_ORG_UNIT_UUID: [
                {
                    "uuid": MO_ROOT_ORG_UNIT_UUID,
                    "parent": None,
                }
            ],
            MO_CHILD_ORG_UNIT_UUID: [
                {
                    "uuid": MO_CHILD_ORG_UNIT_UUID,
                    "parent": None,
                }
            ],
        }


class MockLoraCacheUnitAddress(MockLoraCacheExtended):
    _job_function_class_uuid = "job_function_class_uuid"

    def __init__(self, address_value):
        self._address_value = address_value
        mo_values = {"uuid": MO_UUID}
        mo_engagements = [
            {
                "uuid": "eng_%s" % MO_UUID,
                "is_primary": True,
                "validity": {"from": "1960-01-01", "to": None},
                "job_function": {"uuid": self._job_function_class_uuid},
            }
        ]
        super().__init__(mo_values, mo_engagements=mo_engagements)

    @property
    def classes(self):
        return {self._job_function_class_uuid: {"title": "Job function title"}}

    @property
    def addresses(self):
        address_uuid = "address-uuid"
        address = {
            "unit": MO_ROOT_ORG_UNIT_UUID,
            "scope": "DAR",
            "value": self._address_value,
        }
        return {address_uuid: [address]}


class MockMoraHelper(MoraHelper):
    def __init__(self, cpr, mo_uuid=None, read_ou_addresses=None):
        self._mo_user = {"cpr_no": cpr, "uuid": mo_uuid or MO_UUID}
        self._read_ou_addresses = read_ou_addresses or {}
        self._read_user_calls = []
        self._mo_post_calls = []
        super().__init__()

    def read_organisation(self):
        return "not-a-org-uuid"

    def read_user(self, user_cpr=None, **kwargs):
        self._read_user_calls.append(user_cpr)
        if user_cpr == UNKNOWN_CPR_NO:
            return None
        return self._mo_user

    def read_all_users(self):
        return [self._mo_user]

    def read_ou(self, uuid):
        return {
            "name": MO_ROOT_ORG_UNIT_NAME,
            "user_key": "org_unit_user_key",
            "org_unit_type": {"uuid": "org_unit_type_uuid"},
            "org_unit_level": {"uuid": "org_unit_level_uuid"},
            "parent": None,
        }

    def read_ou_address(self, uuid, scope=None, **kwargs):
        if scope == "EMAIL":
            return []
        if scope == "DAR":
            return self._read_ou_addresses

    def read_engagement_manager(self, engagement_uuid):
        return {}

    def get_e_addresses(self, e_uuid, scope=None):
        return [
            {
                "value": "address-value",
                "address_type": {"uuid": "address-type-uuid"},
            }
        ]

    def _mo_post(self, url, payload, force=True):
        # Raise exception if payload cannot be serialized as JSON, e.g. if it contains
        # types that cannot be serialized as JSON (`uuid.UUID`, etc.)
        json.dumps(payload)
        # Record the MO API call
        self._mo_post_calls.append(dict(url=url, payload=payload, force=force))
        # Mock the MO API response
        mock_response = Mock(spec=requests.Response, status_code=201)
        mock_response.json.return_value = str(uuid.uuid4())
        return mock_response


class MockMOGraphqlSource:
    def __init__(self, *args, **kwargs):
        pass

    def get_manager_uuid(self, mo_user: dict, eng_uuid: str) -> str | None:
        if mo_user["uuid"] == MO_UUID:
            return MO_MANAGER_UUID
        return None


class MockADWriterContext(ExitStack):
    """Mock enough of `ADWriter` dependencies to allow it to instantiate in a test.
    Usage:
    >>> with MockADWriterContext():
    >>>     ad_writer = ADWriter(...)
    >>>     ad_writer.some_method(...)
    """

    all_settings = {
        "primary": {
            "method": "ntlm",
            "cpr_separator": "",
            "system_user": "system_user",
            "password": "password",
            "search_base": "search_base",
            "cpr_field": "cpr_field",  # read by `ADWriter.get_from_ad`
            "properties": [],  # read by `ADWriter._properties`
        },
        "primary_write": {
            "cpr_field": "cpr_field",
            "uuid_field": "uuid_field",
            "org_field": "org_field",
            "level2orgunit_field": "level2orgunit_field",
            "level2orgunit_type": "level2orgunit_type",
            "upn_end": "upn_end",
            "mo_to_ad_fields": {},
            "template_to_ad_fields": {},
            "template_to_ad_fields_when_disable": {},
        },
        "global": {"mora.base": "", "servers": ["server"]},
    }

    def __init__(self, **kwargs):
        super().__init__()

        self._read_ou_addresses = kwargs.get("read_ou_addresses")

        settings = copy.deepcopy(self.all_settings)

        template_to_ad_fields = kwargs.get("template_to_ad_fields", {})
        template_to_ad_fields_when_disable = kwargs.get(
            "template_to_ad_fields_when_disable", {}
        )
        settings["primary_write"]["template_to_ad_fields"].update(template_to_ad_fields)
        settings["primary_write"]["template_to_ad_fields_when_disable"].update(
            template_to_ad_fields_when_disable
        )

        skip_locations = kwargs.get("skip_locations")
        if skip_locations:
            settings["primary_write"]["skip_locations"] = skip_locations

        # This feature flag defaults to True in tests using `MockADWriterContext` even
        # though `read_ad_conf_settings._read_primary_write_information` defaults to
        # False.
        # This is because the tests that verify code paths related to `get_manager_uuid`
        # currently can only test `ADWriter` when  `use_future_managers` is True.
        settings["primary_write"]["use_future_managers"] = kwargs.get(
            "use_future_managers", True
        )

        self._settings = settings

        self._run_ps_response = kwargs.get("run_ps_response") or MagicMock()

    def __enter__(self):
        super().__enter__()
        for ctx in self._context_managers:
            self.enter_context(ctx)
        return self

    @property
    def _context_managers(self):
        prefix = "integrations.ad_integration"
        self.mock_session = MagicMock()
        self.mock_session.run_ps = MagicMock(return_value=self._run_ps_response)
        yield patch(f"{prefix}.ad_common.read_settings", return_value=self._settings)
        yield patch(
            f"{prefix}.ad_writer.ADWriter._create_session",
            return_value=self.mock_session,
        )
        yield patch(
            f"{prefix}.ad_writer.MORESTSource", return_value=MockMORESTSourcePreview()
        )
        yield patch(
            f"{prefix}.user_names.ADParameterReader",
            return_value=MockADParameterReader(),
        )
        yield patch(
            f"{prefix}.ad_writer.ADParameterReader",
            return_value=MockADParameterReader(),
        )
        yield patch(
            f"{prefix}.ad_writer.MoraHelper",
            return_value=MockMoraHelper(
                cpr="",
                read_ou_addresses=self._read_ou_addresses,
            ),
        )
        yield patch(f"{prefix}.ad_writer.MOGraphqlSource", new=MockMOGraphqlSource)
