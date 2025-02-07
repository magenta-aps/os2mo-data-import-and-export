import unittest
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock
from unittest.mock import call
from unittest.mock import patch
from uuid import uuid4

from parameterized import parameterized

from integrations.opus import opus_helpers
from integrations.opus.opus_diff_import import MUTATION_DELETE_ENGAGEMENT
from integrations.opus.opus_diff_import import QUERY_FIND_ENGAGEMENT
from integrations.opus.opus_diff_import import QUERY_FIND_ENGAGEMENT_PRESENT
from integrations.opus.opus_diff_import import QUERY_FIND_MANAGER
from integrations.opus.opus_diff_import import QUERY_FIND_MANAGER_PRESENT
from integrations.opus.opus_diff_import import MOPostDryRun
from integrations.opus.opus_diff_import import OpusDiffImport

XML_DATE = datetime.fromisoformat("2020-01-01")
DAR_UUID = uuid4()


class OpusDiffImportTestbase(OpusDiffImport):
    def __init__(self, latest_date, *args, **kwargs):
        self.morahelper_mock = MagicMock()
        self.morahelper_mock.read_organisation.return_value = "org_uuid"
        self.morahelper_mock._mo_post.return_value.status_code = 201
        self.morahelper_mock.ensure_class_in_facet.return_code = uuid4()

        super().__init__(latest_date, *args, **kwargs)

    def _get_mora_helper(self, hostname, use_cache):
        return self.morahelper_mock

    def find_address(self, *_):
        # Make sure "DAR" returns a "DAR UUID" so we trigger an update of the "Adresse" address type (a postal address.)
        return DAR_UUID

    def _setup_gql_client(self):
        return MagicMock()

    def _find_classes(self, facet):
        if facet == "engagement_type":
            return ({"Ansat": "eng_type", "Softwaretester": "dummy"}, facet)
        if facet == "org_unit_type":
            return ({"uuid": "eng_type"}, facet)
        if facet == "org_unit_address_type":
            return (
                {
                    "SE": "SE_UUID",
                    "CVR": "CVR_UUID",
                    "EAN": "EAN_UUID",
                    "Pnummer": "PN_UUID",
                    "PhoneUnit": "Phone_UUID",
                    "AddressPostUnit": "Addr_UUID",
                },
                facet,
            )
        if facet == "employee_address_type":
            return (
                {
                    "AdressePostEmployee": "post_UUID",
                    "EmailEmployee": "email_UUID",
                    "PhoneEmployee": "phone_UUID",
                },
                facet,
            )
        return ({"uuid": "eng_type"}, facet)

    def _add_klasse_to_lora(self, klasse_name, facet_uuid):
        return self._add_klasse_to_lora


class OpusDiffImportTest_updates(OpusDiffImportTestbase):
    def __init__(self, *args, **kwargs):
        self._update_unit_types = MagicMock()
        self._update_unit_types.return_value = "DummyUUID"
        self.morahelper_mock._mo_post.return_value.status_code = 200
        self._perform_address_update = MagicMock()
        super().__init__(*args, **kwargs)

    def _update_unit_types(self, org_type):
        return self._update_unit_types

    def _perform_address_update(self, args, current, address_dict):
        return self._perform_address_update


class OpusDiffImportTest_counts(OpusDiffImportTestbase):
    def __init__(self, *args, **kwargs):
        self.update_employee = MagicMock()
        self.update_unit = MagicMock()
        self.terminate_detail = MagicMock()
        self._find_engagement = MagicMock()
        self.delete_engagement = MagicMock()

        super().__init__(*args, **kwargs)

    def update_employee(self, employee):
        return self.update_employee

    def update_unit(self, unit):
        return self.update_unit

    def terminate_detail(self, uuid, detail_type="engagement", end_date=None):
        return self.terminate_detail

    def _find_engagement(self, bvn, present=False):
        return self._find_engagement


class Opus_diff_import_tester(unittest.TestCase):
    """Tests for opus_diff_import funktions that does not use input from testfiles"""

    def setUp(self):
        self.file1 = Path.cwd() / "integrations/opus/tests/ZLPE20200101_delta.xml"
        self.file2 = Path.cwd() / "integrations/opus/tests/ZLPE20200102_delta.xml"
        self.expected_unit_count = 4
        self.expected_employee_count = 2
        self.expected_terminations = 1
        self.expected_cancelled = 1

        filter_ids = []
        (
            units,
            filtered_units,
            employees,
            terminated_employees,
            cancelled_employees,
        ) = opus_helpers.read_and_transform_data(self.file1, self.file2, filter_ids)
        self.units = list(units)
        self.filtered_units = list(filtered_units)
        self.employees = list(employees)
        self.terminated_employees = list(terminated_employees)
        self.cancelled_employees = list(cancelled_employees)

    def test_file_diff(self):
        self.assertEqual(len(self.units), self.expected_unit_count)
        self.assertEqual(
            len(self.employees),
            self.expected_employee_count,
        )

    def test_import_count(self):
        diff = OpusDiffImportTest_counts(
            XML_DATE, ad_reader=None, employee_mapping="test"
        )
        diff.start_import(
            self.units,
            self.employees,
            self.terminated_employees,
            self.cancelled_employees,
        )
        self.assertEqual(diff.update_unit.call_count, self.expected_unit_count)
        self.assertEqual(diff.update_employee.call_count, self.expected_employee_count)

        self.assertEqual(diff._find_engagement.call_count, self.expected_terminations)
        self.assertEqual(
            diff.delete_engagement.call_count, len(self.cancelled_employees)
        )

    def test_update_unit(self):
        diff = OpusDiffImportTestbase(XML_DATE, ad_reader=None, employee_mapping="test")
        diff.ensure_class_in_facet = MagicMock(return_value="dummy-class-uuid")
        for unit in self.units:
            diff.update_unit(unit)
            calculated_uuid = opus_helpers.generate_uuid(unit["@id"])
            if unit.get("street"):
                diff.helper._mo_post.assert_called_with(
                    "details/create",
                    {
                        "type": "address",
                        "value": DAR_UUID,
                        "address_type": {"uuid": "dummy-class-uuid"},
                        "validity": {"from": XML_DATE.strftime("%Y-%m-%d"), "to": None},
                        "org_unit": {"uuid": str(calculated_uuid)},
                        "visibility": None,
                    },
                )

    def test_perform_address_update_create(self):
        diff = OpusDiffImportTestbase(
            XML_DATE, ad_reader=None, employee_mapping={"dummy": 1}
        )
        address_type_uuid = str(uuid4())
        value = "address value"
        org_unit_uuid = uuid4()
        visibility = uuid4()
        fromdate = XML_DATE - timedelta(days=1)
        args = {
            "address_type": {"uuid": address_type_uuid},
            "value": value,
            "validity": {"from": fromdate.strftime("%Y-%m-%d"), "to": None},
            "unit_uuid": str(org_unit_uuid),
            "visibility": {"uuid": str(visibility)},
        }
        current = {"uuid": address_type_uuid}
        # Same
        diff._perform_address_update(args, current)
        diff.helper.assert_not_called()
        # new
        diff._perform_address_update(args, None)
        diff.helper._mo_post.assert_called_with(
            "details/create",
            {
                "type": "address",
                "value": value,
                "address_type": {"uuid": address_type_uuid},
                "validity": {"from": fromdate.strftime("%Y-%m-%d"), "to": None},
                "org_unit": {"uuid": str(org_unit_uuid)},
                "visibility": {"uuid": str(visibility)},
            },
        )

    @parameterized.expand(
        [
            (None, None, None),
            (None, "Username", "details/create"),
            ("Username", "new_username", "details/edit"),
            ("new_username", None, "details/terminate"),
        ]
    )
    def test_update_username(
        self,
        current_username,
        new_username,
        change_type,
    ):
        diff = OpusDiffImportTestbase(XML_DATE, ad_reader=None, employee_mapping="test")
        date = XML_DATE.strftime("%Y-%m-%d")
        diff.it_systems = {"Opus": "Opus_uuid"}
        diff.morahelper_mock.get_e_itsystems.return_value = [
            (
                {"user_key": current_username, "uuid": "dummyuuid"}
                if current_username
                else None
            )
        ]

        if change_type == "details/edit":
            expected = {
                "type": "it",
                "uuid": "dummyuuid",
                "data": {
                    "user_key": new_username,
                    "validity": {"from": date, "to": None},
                },
            }
        elif change_type == "details/create":
            expected = {
                "type": "it",
                "user_key": new_username,
                "itsystem": {"uuid": "Opus_uuid"},
                "person": {"uuid": "dummyuuid"},
                "validity": {"from": date, "to": None},
            }
        elif change_type == "details/terminate":
            expected = {
                "type": "it",
                "uuid": "dummyuuid",
                "validity": {"to": date},
            }
            diff.morahelper_mock._mo_post.return_value.status_code = 200

        diff.connect_it_system(new_username, "Opus", {}, "dummyuuid")
        if change_type:
            diff.morahelper_mock._mo_post.assert_called_once_with(change_type, expected)
        else:
            diff.morahelper_mock._mo_post.assert_not_called()

    def test_skip_multiple_usernames(
        self,
    ):
        diff = OpusDiffImportTestbase(XML_DATE, ad_reader=None, employee_mapping="test")
        diff.it_systems = {"Opus": "Opus_uuid"}
        diff.morahelper_mock.get_e_itsystems.return_value = [
            {"user_key": "username1", "uuid": "dummyuuid"},
            {"user_key": "username2", "uuid": "dummyuuid"},
        ]
        with self.assertLogs() as cm:
            diff.connect_it_system("new_username", "Opus", {}, "personuuid")
            diff.morahelper_mock._mo_post.assert_not_called()
            assert cm.output == [
                "WARNING:opusImport:Skipped connecting Opus IT system . More than one "
                "IT system found for person_uuid='personuuid'"
            ]

    @patch("integrations.opus.opus_helpers.find_opus_root_unit_uuid")
    def test_ensure_class_in_facet(self, root_uuid_mock):
        """Tests that calling ensure_class_in_facet calls morahelpers with the correct owner added"""
        root_uuid = uuid4()
        root_uuid_mock.return_value = root_uuid
        diff = OpusDiffImportTestbase(
            "2022-07-13", ad_reader=None, employee_mapping="test"
        )

        diff.ensure_class_in_facet("Facetname", "classbvn")
        diff.helper.ensure_class_in_facet.assert_called_once_with(
            "Facetname", "classbvn", owner=root_uuid
        )


class TestableOpus(OpusDiffImport):
    def _setup_gql_client(self):
        return MagicMock()


class _GetInstanceMixin:
    _xml_date = datetime.now()

    def get_instance(self, settings: dict, dry_run=False) -> OpusDiffImport:
        settings.setdefault("mora.base", "http://unused.url")
        with patch(
            "integrations.opus.opus_diff_import.load_settings", return_value=settings
        ):
            with patch("integrations.opus.opus_diff_import.MoraHelper"):
                instance = TestableOpus(
                    xml_date=self._xml_date,
                    ad_reader=None,
                    employee_mapping=object(),
                    dry_run=dry_run,
                )
                return instance


class TestCondenseEmployeeOpusAddresses(_GetInstanceMixin):
    """Test `OpusDiffImporter._condense_employee_opus_addresses`"""

    opus_employee = {
        "email": "foobar@example.com",
        "workPhone": "12345678",
        "address": "Testvej 1",
        "postalCode": "1234",
    }

    opus_employee_protected_address = {
        "email": "foobar@example.com",
        "workPhone": "12345678",
        # non-empty dict means the address is protected (?)
        "address": {"unknown": "content"},
        "postalCode": "1234",
    }

    dar_valid_uuid = "dar-valid-address-uuid"
    dar_invalid_uuid = None

    @parameterized.expand(
        [
            # Test flow with all feature flags turned off
            (
                {},  # empty feature flags
                opus_employee,
                dar_valid_uuid,
                {
                    "phone": opus_employee["workPhone"],
                    "email": opus_employee["email"],
                    "dar": dar_valid_uuid,
                },
            ),
            # Test "skip_employee_email" flag with normal Opus employee and valid DAR
            (
                {"integrations.opus.skip_employee_email": True},
                opus_employee,
                dar_valid_uuid,
                {"phone": opus_employee["workPhone"], "dar": dar_valid_uuid},
            ),
            # Test "skip_employee_address" flag with normal Opus employee and valid DAR
            (
                {"integrations.opus.skip_employee_address": True},
                opus_employee,
                dar_valid_uuid,
                {"phone": opus_employee["workPhone"], "email": opus_employee["email"]},
            ),
            # Test "skip_employee_phone" flag with normal Opus employee and valid DAR
            (
                {"integrations.opus.skip_employee_phone": True},
                opus_employee,
                dar_valid_uuid,
                {"email": opus_employee["email"], "dar": dar_valid_uuid},
            ),
            # Test protected addresses are always removed (regardless of
            # "skip_employee_address" flag.)
            (
                {"integrations.opus.skip_employee_address": False},
                opus_employee_protected_address,
                dar_valid_uuid,
                {"email": opus_employee["email"], "phone": opus_employee["workPhone"]},
            ),
            # Test that failed DAR lookups result in no "dar" key being added to the
            # result (regardless of "skip_employee_address" flag.)
            (
                {"integrations.opus.skip_employee_address": False},
                opus_employee,
                dar_invalid_uuid,
                {"email": opus_employee["email"], "phone": opus_employee["workPhone"]},
            ),
        ]
    )
    def test_feature_flags_are_respected(
        self,
        settings: dict,
        opus_employee: dict,
        dar_response: Optional[str],
        expected_result: dict,
    ) -> None:
        instance = self.get_instance(settings)
        with patch.object(
            instance,
            "find_address",
            return_value=dar_response,
        ):
            actual_result = instance._condense_employee_opus_addresses(opus_employee)
            assert actual_result == expected_result

    def test_skip_user_if_no_position(
        self,
    ):
        # Arrange
        diff = OpusDiffImportTestbase(XML_DATE, ad_reader=None, employee_mapping="test")

        # Act
        diff.update_employee({"position": None, "cpr": "123456789"})

        # Assert
        # If no position is found the function returns early and never calls 'read_user'
        diff.helper.read_user.assert_not_called()

    def test_skip_user_if_no_entrydate(
        self,
    ):
        # Arrange
        diff = OpusDiffImportTestbase(XML_DATE, ad_reader=None, employee_mapping="test")

        # Act
        diff.update_employee(
            {"entryDate": None, "position": "tester", "cpr": "123456789"}
        )

        # Assert
        # If no entryDate is found the function returns early and never calls 'read_user'
        diff.helper.read_user.assert_not_called()


class TestUpdateEmployeeAddress(_GetInstanceMixin):
    """Test `OpusDiffImporter._update_employee_address`"""

    opus_employee = {
        "email": "foobar@example.com",
        "workPhone": "12345678",
        "address": "Testvej 1",
        "postalCode": "1234",
    }

    expected_address_visibility = {
        "facet": "visibility",
        "bvn": "Intern",
        "title": "Må vises internt",
        "scope": "INTERNAL",
    }

    def test_dar_address_visibility(self):
        """Verify that we use the correct visibility class when creating or updating
        employee addresses of the type 'Adresse' (= postal addresses.)"""
        instance = self.get_instance({})
        with patch.object(instance, "ensure_class_in_facet") as ensure_class:
            # Make sure "DAR" returns a "DAR UUID" so we trigger an update of the
            # "Adresse" address type (a postal address.)
            with patch.object(
                instance,
                "find_address",
                return_value="dar-address-uuid",
            ):
                with patch.object(instance, "_perform_address_update"):
                    instance._update_employee_address("mo_uuid", self.opus_employee)
                    assert (
                        ensure_class.call_args.kwargs
                        == self.expected_address_visibility
                    )


class TestUpdateEmployeeManagerFunctions(_GetInstanceMixin):
    unit_name = "Testenhed"

    opus_employee = {
        "@id": "1",
        "isManager": "true",
        "leaveDate": None,
        "entryDate": datetime.now(),
        "superiorLevel": "ledertype",
        "subordinateLevel": "niveau 1",
        "position": "stillingsbetegnelse",
        "orgUnit": unit_name,
    }
    unit_uuid = str(opus_helpers.generate_uuid(opus_employee["orgUnit"]))
    manager_level = str(uuid4())
    manager_type = str(uuid4())
    manager_responsibility = str(uuid4())

    job_function_uuid = "job_function_uuid"
    engagement_type_uuid = "engagement_type_uuid"

    def test_create_manager(self):
        # Arrange
        instance = self.get_instance({})
        instance.helper._mo_lookup = MagicMock(
            return_value=[
                # This manager function should be disregarded
                {
                    "uuid": "uuid_2",
                    "user_key": "2",
                }
            ]
        )
        instance.helper._mo_post = MagicMock()
        instance.helper._mo_post.return_value.status_code = 201

        with patch.object(instance, "ensure_class_in_facet") as ensure_class:
            ensure_class.side_effect = [
                self.manager_level,
                self.manager_type,
                self.manager_responsibility,
            ]
            # Act
            instance.update_manager_status("mo_uuid", self.opus_employee)

            # Assert
            ensure_class.assert_called()
            assert ensure_class.call_args_list == [
                call("manager_level", "ledertype.niveau 1"),
                call("manager_type", "stillingsbetegnelse"),
                call("responsibility", "Lederansvar"),
            ]
            instance.helper._mo_post.assert_called_once_with(
                "details/create",
                {
                    "type": "manager",
                    "user_key": "1",
                    "org_unit": {"uuid": self.unit_uuid},
                    "person": {"uuid": "mo_uuid"},
                    "manager_type": {"uuid": self.manager_type},
                    "manager_level": {"uuid": self.manager_level},
                    "responsibility": [{"uuid": self.manager_responsibility}],
                    "validity": {"from": self.opus_employee["entryDate"], "to": None},
                },
            )

    def test_update_manager(self):
        # Arrange
        instance = self.get_instance({})
        instance.helper._mo_lookup = MagicMock(
            return_value=[
                {
                    "uuid": "uuid_1",
                    "user_key": "1",
                    "person": {"uuid": "mo_uuid"},
                    "org_unit": {"uuid": self.unit_uuid},
                    # Manager_level should be changed
                    "manager_level": {"uuid": str(uuid4())},
                    "manager_type": {"uuid": self.manager_type},
                    "responsibility": [{"uuid": self.manager_responsibility}],
                },
                # This manager function should be disregarded
                {
                    "uuid": "uuid_2",
                    "user_key": "2",
                },
            ]
        )
        instance.helper._mo_post = MagicMock()
        instance.helper._mo_post.return_value.status_code = 200
        validity = instance.validity(employee=self.opus_employee, edit=True)
        with patch.object(instance, "ensure_class_in_facet") as ensure_class:
            ensure_class.side_effect = [
                self.manager_level,
                self.manager_type,
                self.manager_responsibility,
            ]
            # Act
            instance.update_manager_status("mo_uuid", self.opus_employee)

            # Assert
            ensure_class.assert_called()
            assert ensure_class.call_args_list == [
                call("manager_level", "ledertype.niveau 1"),
                call("manager_type", "stillingsbetegnelse"),
                call("responsibility", "Lederansvar"),
            ]
            instance.helper._mo_post.assert_called_once_with(
                "details/edit",
                {
                    "type": "manager",
                    "uuid": "uuid_1",
                    "data": {
                        "org_unit": {"uuid": self.unit_uuid},
                        "person": {"uuid": "mo_uuid"},
                        "manager_type": {"uuid": self.manager_type},
                        "manager_level": {"uuid": self.manager_level},
                        "responsibility": [{"uuid": self.manager_responsibility}],
                        "validity": {"from": validity["from"], "to": None},
                    },
                },
            )

    def test_terminate_manager(self):
        # Arrange
        self.opus_employee["isManager"] = "false"
        instance = self.get_instance({})
        instance.helper._mo_lookup = MagicMock(
            return_value=[
                {
                    "uuid": "uuid_1",
                    "user_key": "1",
                },
                {
                    "uuid": "uuid_2",
                    "user_key": "2",
                },
            ]
        )
        instance.helper._mo_post = MagicMock()
        instance.helper._mo_post.return_value.status_code = 200

        validity = instance.validity(employee=self.opus_employee, edit=True)

        # Act
        with patch.object(instance, "ensure_class_in_facet"):
            instance.update_manager_status("mo_uuid", self.opus_employee)

            # Assert
            instance.helper._mo_post.assert_called_once_with(
                "details/terminate",
                {
                    "type": "manager",
                    "uuid": "uuid_1",
                    "validity": {"to": validity["from"]},
                },
            )

    def test_dry_run(self):
        """Test that the dry_run flag overwrites the _mo_post function"""
        # Arrange
        instance = self.get_instance({}, dry_run=False)
        # Act
        response = instance.helper._mo_post("test", payload={"dummy": "payload"})

        # Assert
        assert isinstance(response, MagicMock)

        # Arrange
        instance = self.get_instance({}, dry_run=True)
        # Act
        response = instance.helper._mo_post("test", payload={"dummy": "payload"})

        # Assert
        assert isinstance(response, MOPostDryRun)

    def test_update_engagement_noop(self):
        # Arrange
        instance = self.get_instance({})
        instance.helper._mo_post = MagicMock()
        instance.helper._mo_post.return_value.status_code = 200

        start_date = "2000-01-01"
        self.opus_employee["entryDate"] = start_date

        unit_uuid = str(opus_helpers.generate_uuid(self.opus_employee["orgUnit"]))
        mo_engagement = {
            "uuid": "engagement_uuid",
            "validity": {"from": start_date, "to": None},
            "engagement_type": {"uuid": self.engagement_type_uuid},
            "job_function": {"uuid": self.job_function_uuid},
            "org_unit": {"uuid": unit_uuid},
        }

        # Act
        with patch.object(
            instance,
            "ensure_class_in_facet",
            side_effect=[self.job_function_uuid, self.engagement_type_uuid],
        ):
            instance.update_engagement(mo_engagement, self.opus_employee)

        # Assert
        instance.helper._mo_post.assert_not_called()

    def test_update_engagement_earlier_start_date_(self):
        """Set a different start-date in opus to an earlier date and test that it is updated in MO"""
        # Arrange
        start_date = "2000-01-01"
        self.opus_employee["entryDate"] = start_date
        instance = self.get_instance({})
        instance.helper._mo_post = MagicMock()
        instance.helper._mo_post.return_value.status_code = 200

        validity = instance.validity(employee=self.opus_employee, edit=True)
        unit_uuid = str(opus_helpers.generate_uuid(self.opus_employee["orgUnit"]))
        mo_engagement = {
            "uuid": "engagement_uuid",
            "validity": {"from": str(datetime.now().date()), "to": None},
            "engagement_type": {"uuid": self.engagement_type_uuid},
            "job_function": {"uuid": self.job_function_uuid},
            "org_unit": {"uuid": unit_uuid},
        }

        # Act
        with patch.object(
            instance,
            "ensure_class_in_facet",
            side_effect=[self.job_function_uuid, self.engagement_type_uuid],
        ):
            instance.update_engagement(mo_engagement, self.opus_employee)

        # Assert
        instance.helper._mo_post.assert_called_once_with(
            "details/edit",
            {
                "type": "engagement",
                "uuid": "engagement_uuid",
                "data": {
                    "engagement_type": {"uuid": self.engagement_type_uuid},
                    "job_function": {"uuid": self.job_function_uuid},
                    "org_unit": {"uuid": unit_uuid},
                    "validity": {"from": start_date, "to": validity["to"]},
                },
            },
        )

    def test_update_engagement_later_start_date_(self):
        """Set a different start-date in opus to an later date and test that it is updated in MO"""
        # Arrange
        new_start_date = "2020-03-11"
        termination_date = "2020-03-10"
        old_start_date = "2010-03-11"
        self.opus_employee["entryDate"] = new_start_date
        instance = self.get_instance({})
        instance.helper._mo_post = MagicMock()
        instance.helper._mo_post.return_value.status_code = 200

        validity = instance.validity(employee=self.opus_employee, edit=True)
        unit_uuid = str(opus_helpers.generate_uuid(self.opus_employee["orgUnit"]))
        mo_engagement = {
            "uuid": "engagement_uuid",
            "validity": {"from": old_start_date, "to": None},
            "engagement_type": {"uuid": self.engagement_type_uuid},
            "job_function": {"uuid": self.job_function_uuid},
            "org_unit": {"uuid": unit_uuid},
        }

        # Act
        with patch.object(
            instance,
            "ensure_class_in_facet",
            side_effect=[self.job_function_uuid, self.engagement_type_uuid],
        ):
            instance.update_engagement(mo_engagement, self.opus_employee)

        # Assert

        assert instance.helper._mo_post.call_count == 2
        assert instance.helper._mo_post.call_args_list == [
            call(
                "details/edit",
                {
                    "type": "engagement",
                    "uuid": "engagement_uuid",
                    "data": {
                        "engagement_type": {"uuid": self.engagement_type_uuid},
                        "job_function": {"uuid": self.job_function_uuid},
                        "org_unit": {"uuid": unit_uuid},
                        "validity": {"from": new_start_date, "to": validity["to"]},
                    },
                },
            ),
            call(
                "details/terminate",
                {
                    "type": "engagement",
                    "uuid": "engagement_uuid",
                    "validity": {"from": old_start_date, "to": termination_date},
                },
            ),
        ]

    def test_delete_canceled(self):
        # Arrange
        eng_uuid = str(uuid4())
        instance = self.get_instance({})
        instance.gql_client.execute.return_value = {
            "engagement_delete": {"uuid": eng_uuid}
        }

        # Act
        with patch.object(instance, "_find_engagement", return_value=eng_uuid):
            instance.delete_engagement(1)

        # Assert
        instance.gql_client.execute.assert_called_once_with(
            MUTATION_DELETE_ENGAGEMENT, variable_values={"uuid": eng_uuid}
        )

    def test_find_engagement(self):
        # Arrange
        opus_id = 1234
        eng_uuid = str(uuid4())
        instance = self.get_instance({})
        instance.gql_client.execute.return_value = {
            "engagements": {"objects": [{"uuid": eng_uuid}]}
        }

        # Act
        res = instance._find_engagement(opus_id)

        # Assert
        instance.gql_client.execute.assert_called_once_with(
            QUERY_FIND_ENGAGEMENT, variable_values={"user_key": str(opus_id)}
        )
        assert res == eng_uuid

    def test_find_engagement_present(self):
        # Arrange
        opus_id = 1234
        eng_uuid = str(uuid4())
        instance = self.get_instance({})
        instance.gql_client.execute.return_value = {
            "engagements": {"objects": [{"uuid": eng_uuid}]}
        }

        # Act
        res = instance._find_engagement(opus_id, present=True)

        # Assert
        instance.gql_client.execute.assert_called_once_with(
            QUERY_FIND_ENGAGEMENT_PRESENT, variable_values={"user_key": str(opus_id)}
        )
        assert res == eng_uuid

    def test_find_manager_role(self):
        # Arrange
        opus_id = 1234
        manager_uuid = str(uuid4())
        instance = self.get_instance({})
        instance.gql_client.execute.return_value = {
            "managers": {"objects": [{"uuid": manager_uuid}]}
        }

        # Act
        res = instance._find_manager_role(opus_id)

        # Assert
        instance.gql_client.execute.assert_called_once_with(
            QUERY_FIND_MANAGER, variable_values={"user_key": str(opus_id)}
        )
        assert res == manager_uuid

    def test_find_manager_role_present(self):
        # Arrange
        opus_id = 1234
        manager_uuid = str(uuid4())
        instance = self.get_instance({})
        instance.gql_client.execute.return_value = {
            "managers": {"objects": [{"uuid": manager_uuid}]}
        }

        # Act
        res = instance._find_manager_role(opus_id, present=True)

        # Assert
        instance.gql_client.execute.assert_called_once_with(
            QUERY_FIND_MANAGER_PRESENT, variable_values={"user_key": str(opus_id)}
        )
        assert res == manager_uuid


if __name__ == "__main__":
    unittest.main()
