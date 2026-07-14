import copy
import inspect
import logging
import re
from datetime import datetime
from unittest import TestCase
from unittest import mock
from unittest.mock import MagicMock

from fastramqpi.ra_utils.lazy_dict import LazyDict
from freezegun import freeze_time
from hypothesis import given
from hypothesis import strategies as st
from jinja2.exceptions import UndefinedError
from more_itertools import first_true
from more_itertools import only
from parameterized import parameterized

from integrations.mora_helper import MoraHelper

from ..ad_exceptions import CommandFailure
from ..ad_exceptions import CprNotFoundInADException
from ..ad_exceptions import CprNotNotUnique
from ..ad_exceptions import NoPrimaryEngagementException
from ..ad_exceptions import SamAccountNameNotUnique
from ..ad_template_engine import INVALID
from ..ad_template_engine import illegal_parameters
from ..ad_writer import ADWriter
from ..ad_writer import LoraCacheSource
from ..user_names import UserNameSetInAD
from ..utils import AttrDict
from .mocks import MO_MANAGER_CPR
from .mocks import MO_MANAGER_SAM
from .mocks import MO_MANAGER_UUID
from .mocks import MO_ROOT_ORG_UNIT_NAME
from .mocks import MO_USER_CPR
from .mocks import MO_USER_SAM
from .mocks import MO_UUID
from .mocks import MockADParameterReaderWithManager
from .mocks import MockADWriterContext
from .mocks import MockLoraCacheUnitAddress
from .mocks import MockLoraCacheWithManager
from .mocks import MockMOGraphqlSource
from .mocks import MockMORESTSource
from .test_utils import TestADWriterMixin
from .test_utils import dict_modifier
from .test_utils import mo_modifier

JOB_TITLE_AD_FIELD_NAME = "titel"
JOB_TITLE_TEMPLATE = "{{ ad_values.get('titel') or mo_values['title'] }}"

_SYNC_TIMESTAMP = "2020-01-01 12:30:00"


class TestADWriter(TestCase, TestADWriterMixin):
    def setUp(self):
        self._setup_adwriter()

    def _verify_identical_common_code(self, num_expected_scripts, num_common_lines=5):
        """Verify that common code in all scripts is identical.

        I.e. that all scripts start with the same num_common_lines lines.
        """
        self.assertEqual(len(self.ad_writer.scripts), num_expected_scripts)
        # 1. Convert each script from a string into a list of strings (lines)
        lines = [script.split("\n") for script in self.ad_writer.scripts]
        self.assertGreaterEqual(len(lines[0]), num_common_lines)
        self.assertEqual(len(lines), num_expected_scripts)
        # 2. Get the common lines (first `num_common_lines` lines in each
        # script.)
        common_lines = [x[:num_common_lines] for x in lines]
        self.assertEqual(len(common_lines[0]), num_common_lines)
        self.assertEqual(len(common_lines), num_expected_scripts)
        # 3. Zip the lines producing `num_common_lines` tuples of 'n' elements,
        # where 'n' is len(scripts).
        zip_lines = list(zip(*common_lines))
        self.assertEqual(len(zip_lines[0]), num_expected_scripts)
        self.assertEqual(len(zip_lines), num_common_lines)
        # Check that all zip_lines are identical
        for zip_line in zip_lines:
            self.assertEqual(len(set(zip_line)), 1)
        # Return common code
        return common_lines[0]

    def _add_to_template_to_ad_fields(self, ad_field_name, template):
        return dict_modifier(
            {"integrations.ad_writer.template_to_ad_fields": {ad_field_name: template}}
        )

    def _assert_script_contains_field(self, script, name, value):
        """Assert that AD PowerShell script `script` includes the AD field
        called `name` and that the field value is `value`.
        """
        self.assertRegex(script, f'"{name}"="{value}"')

    def _get_script_contents(self, index=0):
        return self.ad_writer.scripts[index].split("\n")[5].strip()

    @parameterized.expand(
        [
            # Test without any changes to settings
            [
                dict_modifier({}),
            ],
            # Test with overridden password
            [
                dict_modifier({"primary": {"password": "Password1"}}),
            ],
            [
                dict_modifier({"primary": {"password": "Hunter2"}}),
            ],
            # Test with overridden user
            [
                dict_modifier({"primary": {"system_user": "R2D2"}}),
            ],
            [
                dict_modifier({"primary": {"system_user": "C-3PO"}}),
            ],
        ]
    )
    def test_common_ps_code(self, settings_transformer):
        """Test ps_script common code (first five lines of each script).

        The common code lines are identical for all writes to the AD.
        This is verified by the 'verify_identical_common_code' method.

        Create_user is used as a test-case to provoke the creation of a PS script,
        but only the common top of the code is actually tested.
        """
        self._setup_adwriter(settings_transformer)

        # Assert no scripts were produced from initializing ad_writer itself
        self.assertGreaterEqual(len(self.ad_writer.scripts), 0)

        # Expected outputs
        num_expected_scripts = 1

        # Run create user and fetch scripts
        uuid = "invalid-provided-and-accepted-due-to-mocking"
        self.ad_writer.create_user(mo_uuid=uuid, create_manager=False)
        # Check that scripts were produced
        self.assertEqual(len(self.ad_writer.scripts), num_expected_scripts)

        # Verify that the first 4 lines are identical for all scripts
        common_ps = self._verify_identical_common_code(num_expected_scripts)
        common_ps = [x.strip() for x in common_ps]
        expected_ps = [
            self.ad_writer._ps_boiler_plate()["encoding"],
            '$User = "' + self.settings["primary"]["system_user"] + '"',
            '$PWord = ConvertTo-SecureString –String "'
            + self.settings["primary"]["password"]
            + '" –AsPlainText -Force',
            '$TypeName = "System.Management.Automation.PSCredential"',
            (
                "$UserCredential = New-Object –TypeName $TypeName "
                "–ArgumentList $User, $PWord"
            ),
        ]
        self.assertEqual(common_ps, expected_ps)

    # Jinja to determine if mo_values['employment_number'] is a prime number
    is_prime_jinja = """
        {#- Aliased to n for readability -#}
        {%- set n = mo_values['employment_number'] | int -%}
        {#- Negative numbers, zero and one are not primes -#}
        {%- if n <= 1 -%}
            false
        {%- else -%}
            {#- Using vars as dict allows settings by reference -#}
            {%- set vars = {'is_prime': True} -%}

            {#- Really we only need to check until sqrt(n) -#}
            {%- for i in range(2,n) -%}
                {#- We cannot use percentage for modulus -#}
                {%- set quotient = (n/i) | int -%}
                {%- set remainder = n - i*quotient -%}

                {#- If it divides without remainder, it is not prime -#}
                {%- if remainder == 0 -%}
                    {#- Update is_prime by reference -#}
                    {%- set _ = vars.update({'is_prime': False}) -%}
                {%- endif -%}
            {%- endfor -%}

            {#- Print whether we found a prime or not -#}
            {%- if vars['is_prime'] -%}
                true
            {%- else -%}
                false
            {%- endif -%}
        {%- endif -%}
    """

    @parameterized.expand(
        [
            # Test without any changes
            [dict_modifier({}), None, ""],
            # Test with new employment number
            [dict_modifier({}), mo_modifier({"employment_number": "42"}), ""],
            [dict_modifier({}), mo_modifier({"employment_number": "100"}), ""],
            # Test mo_to_ad_fields
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.mo_to_ad_fields": {
                            "unit_user_key": "unit_user_key"
                        }
                    }
                ),
                None,
                '"unit_user_key"="Musik";',
            ],
            # Test with added template_field
            # Simple field lookup
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "unit_user_key": "{{ mo_values['unit_user_key'] }}"
                        }
                    }
                ),
                None,
                '"unit_user_key"="Musik";',
            ],
            # Test with timestamp as added template_field
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "extensionAttribute21": "{{ sync_timestamp }}",
                        }
                    }
                ),
                None,
                f'"extensionAttribute21"="{_SYNC_TIMESTAMP}";',
            ],
            # Field lookup and processing
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "street_number": """
                                {%- set street = mo_values['unit_streetname'] -%}
                                {{ street.split(' ')[-1] }}
                                {#- Comment used to trim whitespace -#}
                            """
                        }
                    }
                ),
                None,
                '"street_number"="451";',
            ],
            # Test if employment number is prime
            # 101 is prime
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "employment_number_is_prime": is_prime_jinja
                        }
                    }
                ),
                mo_modifier({"employment_number": "101"}),
                '"employment_number_is_prime"="true";',
            ],
            # 100 is not prime
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "employment_number_is_prime": is_prime_jinja
                        }
                    }
                ),
                mo_modifier({"employment_number": "100"}),
                '"employment_number_is_prime"="false";',
            ],
            # 263 is prime
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "employment_number_is_prime": is_prime_jinja
                        }
                    }
                ),
                mo_modifier({"employment_number": "263"}),
                '"employment_number_is_prime"="true";',
            ],
            # 267 is not prime
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "employment_number_is_prime": is_prime_jinja
                        }
                    }
                ),
                mo_modifier({"employment_number": "267"}),
                '"employment_number_is_prime"="false";',
            ],
        ]
    )
    @freeze_time(_SYNC_TIMESTAMP)
    def test_user_create_custom_fields(
        self, settings_transformer, mo_transformer, expected
    ):
        """Test user create ps_script code.

        The common code is not tested.
        """
        self._setup_adwriter(None, mo_transformer, settings_transformer)
        # Assert no scripts were produced from initializing ad_writer itself
        self.assertGreaterEqual(len(self.ad_writer.scripts), 0)

        # Expected outputs
        num_expected_scripts = 1

        # Run create user and fetch scripts
        uuid = "invalid-provided-and-accepted-due-to-mocking"
        self.ad_writer.create_user(mo_uuid=uuid, create_manager=False)
        # Check that scripts were produced
        self.assertEqual(len(self.ad_writer.scripts), num_expected_scripts)

        # Verify that the first 4 lines are identical for all scripts
        self._verify_identical_common_code(num_expected_scripts)

        # Check that the create user ps looks good
        create_user_ps = self._get_script_contents(index=0)

        mo_values = self.ad_writer.read_ad_information_from_mo(uuid)

        if "extensionAttribute21" in expected:
            # When checking datetime we need to drop the seconds before comparing
            expected = expected[:-11]

        expected_content = [
            "New-ADUser",
            '-Name "'
            + mo_values["full_name"]
            + " - "
            + mo_values["sam_account_name"]
            + '"',
            '-Displayname "' + mo_values["full_name"] + '"',
            '-GivenName "' + mo_values["name"][0] + '"',
            '-SurName "' + mo_values["name"][1] + '"',
            '-SamAccountName "' + mo_values["sam_account_name"] + '"',
            '-EmployeeNumber "' + mo_values["employment_number"] + '"',
            "-Credential $usercredential",
            '-UserPrincipalName "' + mo_values["sam_account_name"] + '@epn_end"',
            "-OtherAttributes",
            '"level2orgunit_field"="Ingen";',
            '"org_field"="Kommune\\Forvalting\\Enhed\\";',
            '"uuid_field"="' + mo_values["uuid"] + '";',
            '"cpr_field"="'
            + mo_values["cpr"][0:6]
            + "ad_cpr_sep"
            + mo_values["cpr"][6:]
            + '"',
            '-Path "search_base"',
            expected,
        ]
        for content in expected_content:
            self.assertIn(content, create_user_ps)

    def test_user_edit(self):
        """Test user edit ps_script code.

        The common code is not tested.
        """
        # Assert no scripts were produced from initializing ad_writer itself
        self.assertGreaterEqual(len(self.ad_writer.scripts), 0)

        # Expected outputs
        num_expected_scripts = 1

        # Run create user and fetch scripts
        uuid = "invalid-provided-and-accepted-due-to-mocking"
        self.ad_writer.sync_user(mo_uuid=uuid, sync_manager=False)
        # Check that scripts were produced
        self.assertEqual(len(self.ad_writer.scripts), num_expected_scripts)

        # Verify that the first 4 lines are identical for all scripts
        self._verify_identical_common_code(num_expected_scripts)

        # Check that the create user ps looks good
        edit_user_ps = self._get_script_contents()
        mo_values = self.ad_writer.read_ad_information_from_mo(uuid)
        expected_content = [
            "Get-ADUser",
            "-Filter 'SamAccountName -eq \"" + mo_values["sam_account_name"] + "\"'",
            "-Credential $usercredential",
            "|",
            "Set-ADUser",
            "-Credential $usercredential",
            '-Displayname "' + mo_values["full_name"] + '"',
            '-GivenName "' + mo_values["name"][0] + '"',
            '-SurName "' + mo_values["name"][1] + '"',
            '-EmployeeNumber "' + mo_values["employment_number"] + '"',
            "-Replace",
            '"level2orgunit_field"="Ingen";',
            '"org_field"="Kommune\\Forvalting\\Enhed\\";',
        ]
        for content in expected_content:
            self.assertIn(content, edit_user_ps)

    def test_user_create_ad_values(self):
        """Test that `ad_values` is present (and empty) when creating a new AD
        user. The template `JOB_TITLE_TEMPLATE` uses the MO job title to fill
        in the job title in the appropriate AD field.
        """
        self._setup_adwriter(
            early_transform_settings=self._add_to_template_to_ad_fields(
                JOB_TITLE_AD_FIELD_NAME,
                JOB_TITLE_TEMPLATE,
            )
        )
        mo_uuid = "invalid-provided-and-accepted-due-to-mocking"
        self.ad_writer.create_user(mo_uuid, create_manager=False)
        # Find the "create AD user" script
        create_script = first_true(
            self.ad_writer.scripts, pred=lambda s: "New-ADUser" in s
        )
        # Assert that it contains the mapped job title field
        self._assert_script_contains_field(
            create_script, JOB_TITLE_AD_FIELD_NAME, self.mo_values_func()["title"]
        )

    @parameterized.expand(
        [
            # Tuples of (AD job title, which value to use)
            (None, "mo-value"),
            ("", "mo-value"),
            ("Tester", "ad-value"),
        ]
    )
    def test_user_update_ad_values(self, ad_value, expectation):
        """Test that `ad_values` is present (and empty) when updating an
        existing AD user. The template `JOB_TITLE_TEMPLATE` only populates the
        AD job title with the MO job title if the current AD job title is
        empty.
        """
        self._setup_adwriter(
            early_transform_settings=self._add_to_template_to_ad_fields(
                JOB_TITLE_AD_FIELD_NAME,
                JOB_TITLE_TEMPLATE,
            ),
            transform_ad_values=dict_modifier(
                {JOB_TITLE_AD_FIELD_NAME: ad_value},
            ),
        )
        mo_uuid = "invalid-provided-and-accepted-due-to-mocking"
        mo_value = self.mo_values_func()["title"]
        self.ad_writer.sync_user(mo_uuid, ad_dump=None, sync_manager=False)
        # Find the "update AD user" script
        update_script = first_true(
            self.ad_writer.scripts, pred=lambda s: "Set-ADUser" in s
        )
        # Assert that the update script uses the current AD job title
        self._assert_script_contains_field(
            update_script,
            JOB_TITLE_AD_FIELD_NAME,
            ad_value if ad_value else mo_value,
        )

    def test_duplicate_ad_field_entries(self):
        """Test user edit ps_script code.

        The common code is not tested.
        """
        # These keys conflict, and thus, no primary_write settings are emitted
        settings_transformer = dict_modifier(
            {
                "integrations.ad_writer.template_to_ad_fields": {
                    "Name": "{{ mo_values['unit'] }}"
                },
                "integrations.ad_writer.mo_to_ad_fields": {"unit": "name"},
            }
        )
        with self.assertRaises(ValueError):
            self._setup_adwriter(early_transform_settings=settings_transformer)

    def test_non_overwritten_default(self):
        """Test that a mistake in overriding a default results in an error."""
        # Assert no scripts were produced from initializing ad_writer itself
        self.assertGreaterEqual(len(self.ad_writer.scripts), 0)

        # Add another mapping for the display name AD field
        # In `TestADMixin._prepare_settings.default_settings`, this is spelled
        # `Displayname` (notice the case difference.)
        display_name = {"displayname": "{{ mo_values['name'][0] }}"}
        self.settings["primary_write"]["template_to_ad_fields"].update(display_name)

        with self.assertRaises(ValueError):
            self.ad_writer.sync_user(
                mo_uuid="invalid-provided-and-accepted-due-to-mocking",
                sync_manager=False,
            )

    def test_user_edit_illegal_parameter(self):
        """Test user edit ps_script code with illegal parameter

        The common code is not tested.

        This test simply ensures that the illegal parameter is dropped.
        """
        # Assert no scripts were produced from initializing ad_writer itself
        self.assertGreaterEqual(len(self.ad_writer.scripts), 0)
        illegal_parameters["Set-ADUser"].append("Displayname")

        settings_transformer = dict_modifier({})
        self._setup_adwriter(settings_transformer)
        # Expected outputs
        num_expected_scripts = 1

        # Run create user and fetch scripts
        uuid = "invalid-provided-and-accepted-due-to-mocking"
        self.ad_writer.sync_user(mo_uuid=uuid, sync_manager=False)
        # Check that scripts were produced
        self.assertEqual(len(self.ad_writer.scripts), num_expected_scripts)

        # Verify that the first 4 lines are identical for all scripts
        self._verify_identical_common_code(num_expected_scripts)

        # Check that the create user ps looks good
        edit_user_ps = self._get_script_contents()
        self.assertNotIn("Displayname", edit_user_ps)

    def test_user_edit_illegal_attribute(self):
        """Test user edit ps_script code with illegal attribute.

        The common code is not tested.

        This test simply ensures that the illegal attribute is dropped.
        """
        # Assert no scripts were produced from initializing ad_writer itself
        self.assertGreaterEqual(len(self.ad_writer.scripts), 0)

        settings_transformer = dict_modifier(
            {"integrations.ad_writer.template_to_ad_fields": {"Name": "John"}}
        )

        self._setup_adwriter(settings_transformer)
        # Expected outputs
        num_expected_scripts = 1

        # Run create user and fetch scripts
        uuid = "invalid-provided-and-accepted-due-to-mocking"
        self.ad_writer.sync_user(mo_uuid=uuid, sync_manager=False)
        # Check that scripts were produced
        self.assertEqual(len(self.ad_writer.scripts), num_expected_scripts)

        # Verify that the first 4 lines are identical for all scripts
        self._verify_identical_common_code(num_expected_scripts)

        # Check that the create user ps looks good
        edit_user_ps = self._get_script_contents()
        self.assertNotIn('"Name"="John"', edit_user_ps)

    def test_duplicated_ad_field_name(self):
        # Test configuration where 'Name' is mapped both via
        # `mo_to_ad_fields` *and* `template_to_ad_fields`
        add_dupe_field = dict_modifier(
            {
                "integrations.ad_writer.mo_to_ad_fields": {"unit": "Name"},
            }
        )
        with self.assertRaises(ValueError):
            self._setup_adwriter(early_transform_settings=add_dupe_field)

    @parameterized.expand(
        [
            # Verify different employment numbers
            [
                dict_modifier({}),
                mo_modifier({"employment_number": "267"}),
                dict_modifier({}),
                [],
            ],
            [
                dict_modifier({}),
                mo_modifier({"employment_number": "42"}),
                dict_modifier({}),
                [],
            ],
            # Test mo_to_ad_fields
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.mo_to_ad_fields": {"unit": "Name"},
                    }
                ),
                mo_modifier({"employment_number": "42"}),
                dict_modifier(
                    {
                        "name": ("John Deere", "Enhed"),
                    }
                ),
                ["Name"],
            ],
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.mo_to_ad_fields": {
                            "unit": "Name",
                            "employment_number": "extension_field2",
                        },
                    }
                ),
                mo_modifier({"employment_number": "42"}),
                dict_modifier(
                    {
                        "name": ("John Deere", "Enhed"),
                        "extension_field2": (None, "42"),
                    }
                ),
                ["Name"],
            ],
            # Test template_to_ad_fields
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "adjusted_number": (
                                "{{ mo_values['employment_number']|int + 5 }}"
                            )
                        },
                    }
                ),
                mo_modifier({"employment_number": "42"}),
                dict_modifier(
                    {
                        "adjusted_number": (None, "47"),
                    }
                ),
                [],
            ],
            # Test template_to_ad_fields
            [
                dict_modifier(
                    {
                        "integrations.ad_writer.template_to_ad_fields": {
                            "adjusted_number": (
                                "{{ mo_values['employment_number']|int + 5 }}"
                            ),
                            "Enabled": "Invalid",
                        },
                    }
                ),
                mo_modifier(
                    {
                        "employment_number": "42",
                    }
                ),
                dict_modifier(
                    {
                        "adjusted_number": (None, "47"),
                        "enabled": (True, "Invalid"),
                    }
                ),
                [],
            ],
        ]
    )
    def test_sync_compare(
        self,
        settings_transformer,
        mo_transformer,
        expected_transformer,
        removed_fields,
    ):
        # If test case defines `removed_fields`, remove these fields from the
        # `integrations.ad_writer.template_to_ad_fields` setting, as they are
        # defined by the `integrations.ad_writer.mo_to_ad_fields` setting
        # instead.
        def remove_template_fields(settings):
            key = "integrations.ad_writer.template_to_ad_fields"
            to_remove = set([f.lower() for f in removed_fields])
            for field in copy.copy(settings[key]):
                if field.lower() in to_remove:
                    del settings[key][field]
            return settings

        def actual_settings_transformer(settings):
            return settings_transformer(remove_template_fields(settings))

        self._setup_adwriter(
            early_transform_settings=actual_settings_transformer,
            transform_mo_values=mo_transformer,
        )

        uuid = "invalid-provided-and-accepted-due-to-mocking"
        mo_values = self.ad_writer.read_ad_information_from_mo(uuid)
        mo_values["manager_cpr"] = None
        ad_values = self._prepare_get_from_ad(lambda x: x)
        ad_values["Name"] = "John Deere"

        def find_ad_user(cpr, ad_dump=None):
            return ad_values

        self.ad_writer._find_ad_user = find_ad_user

        mismatch = self.ad_writer._sync_compare(mo_values, None)
        expected = {
            "level2orgunit_field": (None, "Ingen"),
            "org_field": (None, "Kommune\\Forvalting\\Enhed\\"),
            "name": (ad_values["Name"], mo_values["name_sam"]),
            "displayname": (None, " ".join(mo_values["name"])),
            "givenname": (ad_values["GivenName"], mo_values["name"][0]),
            "surname": (None, mo_values["name"][1]),
            "employeenumber": (None, mo_values["employment_number"]),
        }
        expected = expected_transformer(expected)
        self.assertEqual(mismatch, expected)

        # Apply changes to AD, and check _sync_compare returns nothing
        ad_keys = ad_values.keys()
        for lower_ad_key, changeset in expected.items():
            cased_ad_key = next(
                filter(lambda ad_key: ad_key.lower() == lower_ad_key, ad_keys),
                lower_ad_key,
            )
            ad_values[cased_ad_key] = changeset[1]
        mismatch = self.ad_writer._sync_compare(mo_values, None)
        self.assertEqual(mismatch, {})

    def test_sync_compare_context_includes_ad_values(self):
        self._setup_adwriter()

        # The `ad_values` passed in the template context has all keys in lower
        # case
        ad_values = {key.lower(): val for key, val in self.ad_values_func().items()}

        mo_values = self.ad_writer.read_ad_information_from_mo("mo-uuid")
        mo_values["manager_cpr"] = None

        # Call `_sync_compare` with a mocked `_render_field_template` method
        # so we can inspect the template contexts passed to it
        with mock.patch.object(self.ad_writer, "_render_field_template") as m:
            self.ad_writer._sync_compare(mo_values, None)

        # Assert that all calls to mocked template render method includes a
        # valid `ad_values` dict in the template context
        template_contexts = [
            call.args[0] for call in m.mock_calls if len(call.args) == 2
        ]
        self.assertTrue(
            all(context["ad_values"] == ad_values for context in template_contexts)
        )

    @mock.patch("time.sleep")
    def test_rename_ad_user(self, *args):
        # Configure ADWriter to write an AD "Name" attribute consisting of the
        # user's full name, plus " testing".
        settings_transformer = dict_modifier(
            {
                "integrations.ad_writer.template_to_ad_fields": {
                    "Name": "{{ mo_values['full_name'] }} - testing"
                },
            }
        )

        # TODO: this duplicates code found in "test_mo_to_ad_sync.py".
        def remove_manager_cpr(mo_values, *args, **kwargs):
            del mo_values["manager_cpr"]
            return mo_values

        self._setup_adwriter(
            early_transform_settings=settings_transformer,
            transform_mo_values=remove_manager_cpr,
        )

        # Construct a mock AD dump, where the user has a Name attr consisting
        # of their full name only.
        ad_dump = [self._prepare_get_from_ad(ad_transformer=None)]

        # Invoke the AD rename
        mo_user_uuid = "not-really-a-uuid"
        self.ad_writer.sync_user(mo_user_uuid, ad_dump=ad_dump)

        # Assert that we issued two AD PowerShell commands: one for the rename,
        # and one for updating the other AD user attributes.
        self.assertEqual(len(self.ad_writer.scripts), 2)

        # Fetch the "rename" command itself: the last line of the first script
        rename_cmd = self.ad_writer.scripts[0].splitlines()[-1]
        # Assert that the "rename" command does indeed attempt to rename the AD
        # user to "Firstname Lastname - testing", due to the mapping in
        # `settings_transformer`.
        expected_new_name = "%s - testing" % ad_dump[0]["Name"]
        expected_cmd_fragment = '-NewName "%s"' % expected_new_name
        self.assertIn(expected_cmd_fragment, rename_cmd)

    def test_add_manager(self):
        mo_values = self.ad_writer.read_ad_information_from_mo(
            uuid="0", read_manager=True
        )
        self.ad_writer.add_manager_to_user(
            "MGORE", manager_sam=mo_values["manager_sam"]
        )
        # Expected outputs
        num_expected_scripts = 1
        self.assertEqual(len(self.ad_writer.scripts), num_expected_scripts)
        # Verify that the first 4 lines are identical for all scripts
        self._verify_identical_common_code(num_expected_scripts)
        # Check that the create user ps looks good
        add_manager_ps = self._get_script_contents()

        expected_line = (
            "Get-ADUser -Filter 'SamAccountName -eq \"MGORE\"'"
            + " -Credential $usercredential |Set-ADUser -Manager "
            + mo_values["manager_sam"]
            + " -Credential $usercredential"
        )
        self.assertEqual(add_manager_ps, expected_line)

    @parameterized.expand(
        [
            # AD dump is None
            (None, CprNotFoundInADException),
            # AD dump is empty list
            ([], CprNotFoundInADException),
            # AD dump has user without CPR
            ([{"foo": "bar"}], CprNotFoundInADException),
            # AD dump has exactly one matching user
            ([{"cpr": "112233-4455"}], None),
            # AD dump has multiple users with identical CPRs
            (
                [{"cpr": "112233-4455"}, {"cpr": "112233-4455"}],
                CprNotNotUnique,
            ),
        ]
    )
    def test_find_ad_user_ad_dump(self, ad_dump, expected_exception):
        cpr = "112233-4455"

        def settings_transformer(settings):
            settings["integrations.ad"][0].update({"cpr_field": "cpr"})
            return settings

        self._setup_adwriter(
            early_transform_settings=settings_transformer, mock_find_ad_user=False
        )

        if expected_exception:
            with self.assertRaises(expected_exception):
                self.ad_writer._find_ad_user(cpr, ad_dump=ad_dump)
        else:
            ad_user = self.ad_writer._find_ad_user(cpr, ad_dump=ad_dump)
            self.assertDictEqual(ad_user, {"cpr": "112233-4455"})

    def test_template_fails_on_undefined_variable(self):
        settings_transformer = dict_modifier(
            {
                "integrations.ad_writer.template_to_ad_fields": {
                    "Displayname": "{{ unknown_variable }}"
                },
            }
        )
        self._setup_adwriter(early_transform_settings=settings_transformer)
        with self.assertRaises(UndefinedError):
            self.ad_writer.sync_user(mo_uuid="mo-uuid", sync_manager=False)

    def test_fullnames_are_empty_when_constituants_are_empty(self):
        self._setup_adwriter()
        uuid = "some_uuid_here"

        def get_mo_values(firstname, surname, nickname_firstname, nickname_surname):
            user = {
                "uuid": "some_uuid_here",
                "navn": "some_name some_lastname",
                "efternavn": surname,
                "fornavn": firstname,
                "kaldenavn": "",
                "kaldenavn_fornavn": nickname_firstname,
                "kaldenavn_efternavn": nickname_surname,
                "cpr": "some_cpr",
            }
            mock_lora_cache = AttrDict(
                {
                    "users": {user["uuid"]: [user]},
                    "engagements": {
                        "engagement_uuid": [
                            {
                                "user": user["uuid"],
                                "primary_boolean": True,
                                "user_key": "some_userkey",
                                "job_function": "job_function_title_uuid",
                                "unit": "some_unit",
                                "uuid": "engagement_uuid",
                                "from_date": None,
                                "to_date": None,
                            }
                        ]
                    },
                    "classes": {"job_function_title_uuid": {"title": "some_job_title"}},
                    "units": {
                        "some_unit": [
                            {
                                "uuid": "some_unit",
                                "name": "some_unit_name",
                                "user_key": "some_unit_userkey",
                                "location": "some_unit_location",
                                "unit_type": "some_unit_type",
                                "level": "some_unit_level",
                                "parent": None,
                            }
                        ]
                    },
                    "addresses": {},
                    "it_connections": {
                        "it_uuid": [
                            {
                                "uuid": "it_connection_uuid",
                                "user": "user_uuid",
                                "unit": None,
                                "username": "opus_username",
                                "itsystem": "it_uuid",
                                "from_date": "1930-01-01",
                                "to_date": None,
                            }
                        ]
                    },
                }
            )
            self.ad_writer.helper = mock.MagicMock(spec=MoraHelper)
            self.ad_writer.lc = mock_lora_cache
            self.ad_writer.lc_historic = mock_lora_cache
            self.ad_writer.datasource = LoraCacheSource(
                mock_lora_cache,  # lc
                mock_lora_cache,  # lc_historic
                MockMORESTSource(from_date=None, to_date=None),
            )
            mo_values = self.ad_writer._read_ad_information_from_mo(uuid)
            return mo_values

        mo_values = get_mo_values("Ursula", "Uniknavn", "Anne", "Jensen")
        assert isinstance(mo_values, LazyDict)
        assert mo_values["name"] == ("Ursula", "Uniknavn")
        assert mo_values["full_name"] == "Ursula Uniknavn"

        assert mo_values["nickname"] == ("Anne", "Jensen")
        assert mo_values["full_nickname"] == "Anne Jensen"

        mo_values = get_mo_values("", "", "", "")
        assert isinstance(mo_values, LazyDict)
        assert mo_values["name"] == ("", "")
        assert mo_values["full_name"] == ""

        assert mo_values["nickname"] == ("", "")
        assert mo_values["full_nickname"] == ""

        # Test all fields can be read
        for key in mo_values.keys():
            mo_values[key]

    def test_create_user_can_create_manager(self):
        # Test what happens when passing `create_manager=True` to `create_user`
        self._setup_adwriter()
        # Remember SamAccountName of user to be created
        expected_sam_account_name = self.mo_values_func()["sam_account_name"]
        # Replace `_wait_for_replication` with do-nothing stub.
        self.ad_writer._wait_for_replication = lambda sam_account_name: None
        # Create AD user and then set manager
        status, actual_sam_account_name = self.ad_writer.create_user(
            mo_uuid="mo-user-uuid", create_manager=True
        )
        # Assert we ran a PowerShell command to set user's manager
        self.assertIn("Set-ADUser -Manager", self.ad_writer.scripts[-1])
        # Assert we received the proper result from `create_user`
        self.assertTrue(status)
        self.assertEqual(actual_sam_account_name, expected_sam_account_name)

    def test_create_user_non_empty_response_is_error(self):
        # Test what happens when `create_user` encounters a non-empty response
        # from `_run_ps_script`.
        self._setup_adwriter()
        # Replace `get_ad_user` with function which always returns empty
        # response (otherwise `create_user` will raise an exception before we
        # reach the code we want to test.)
        self.ad_writer.get_from_ad = lambda **kwargs: {}
        # Replace `_run_ps_script` with function returning non-empty response
        self.ad_writer._run_ps_script = lambda ps_script: {"not": "empty"}
        # Try to create AD user
        status, msg = self.ad_writer.create_user(
            mo_uuid="mo-user-uuid", create_manager=False
        )
        self.assertFalse(status)
        self.assertRegex(msg, "Create user failed,.*")

    def test_create_user_bails_early_if_no_engagements(self):
        # Test what happens when `create_user` encounters an empty MO user
        self._setup_adwriter()
        # Replace `read_ad_information_from_mo` with function returning None
        self.ad_writer.read_ad_information_from_mo = (
            lambda mo_uuid, create_manager: None
        )
        # Assert that `create_user` bails early with an exception
        with self.assertRaises(NoPrimaryEngagementException):
            self.ad_writer.create_user(mo_uuid="mo-user-uuid", create_manager=False)

    def test_create_user_bails_early_if_ad_user_exists(self):
        # Test what happens if `create_user` encounters an already existing AD
        # user (either by username or CPR number lookup.)

        # Avoid circular imports
        from .mocks import MockADParameterReader
        from .mocks import MockOnlyCPRADParameterReader

        def assert_adwriter_get_ad_user_raises(
            mock_ad_reader_class, expected_exception
        ):
            self._setup_adwriter(mock_ad_reader_class=mock_ad_reader_class)
            # Assert we bail early with the proper exception
            with self.assertRaises(expected_exception):
                self.ad_writer.create_user(mo_uuid=MO_UUID, create_manager=False)

        # Replace `ADWriter._reader` with mock `ADParameterReader` which always returns
        # an AD user for *any* `user` or `cpr` argument.
        # This means that `ADWriter._check_if_user_exists` will "see" a mock AD user
        # when calling `ADWriter._reader.read_user(user=...)` and bail early.
        assert_adwriter_get_ad_user_raises(
            MockADParameterReader, SamAccountNameNotUnique
        )

        # Replace `ADWriter._reader` with mock `ADParameterReader` which returns None
        # when called with the `user` keyword argument, and a mock AD user when called
        # with the `cpr` keyword argument
        # This means that `ADWriter._check_if_user_exists` will "see" a mock AD user
        # when calling `ADWriter._reader.read_user(cpr=...)` and bail early.
        assert_adwriter_get_ad_user_raises(
            MockOnlyCPRADParameterReader, CprNotNotUnique
        )

    def test_other_attributes_skip_empty(self):
        # Configure template which tries to write `mo_values['foobar']` to the
        # 'fooBar' AD field.
        self._setup_adwriter(
            early_transform_settings=self._add_to_template_to_ad_fields(
                "fooBar",
                "{{ mo_values['foobar'] }}",
            ),
        )

        # Build `mo_values` containing an *empty* 'foobar' key
        mo_values = self._prepare_mo_values(
            mo_values_transformer=mo_modifier({"foobar": None})
        )

        # Render "New-ADUser" command using `mo_values`
        ps_script = self.ad_writer._get_create_user_command(mo_values, "sam")

        # Assert the command does not try to set "fooBar"="None"
        self.assertNotIn('"fooBar"="None"', ps_script)

    @parameterized.expand(
        [
            # Case 1: test that the old "search_base" setting is used if the
            # new setting "new_ad_user_path" is not provided.
            (
                {
                    "integrations.ad": [
                        {
                            "search_base": "custom_search_path",
                            # We need to provide these to make
                            # `_read_global_settings` pass.
                            "system_user": "system_user",
                            "password": "password",
                            # We need to provide this to make
                            # `_read_primary_ad_settings` pass.
                            "cpr_field": "cpr_field",
                        }
                    ]
                },
                "custom_search_path",
            ),
            # Case 2: test that the new "new_ad_user_path" settings is used, if
            # it is provided.
            (
                {"integrations.ad_writer.new_ad_user_path": "custom_ad_path"},
                "custom_ad_path",
            ),
        ]
    )
    def test_new_ad_user_path_setting_is_used(self, settings, expected_path_argument):
        self._setup_adwriter(early_transform_settings=dict_modifier(settings))

        # Test `_get_new_ad_user_path_argument`
        path_argument = self.ad_writer._get_new_ad_user_path_argument()
        self.assertRegex(path_argument, f' -Path "{expected_path_argument}"')

        # Test `_get_create_user_command` - the `ps_script` must end with a
        # "-Path" argument matching our expected path argument.
        mock_mo_user = mock.MagicMock()
        ps_script = self.ad_writer._get_create_user_command(
            mock_mo_user, "sam_account_name"
        )
        self.assertRegex(ps_script, f'.* -Path "{expected_path_argument}"$')


class _TestRealADWriter(TestCase):
    @staticmethod
    def mock_run_ps_response(status_code=0, std_out="{}", std_err=None):
        run_ps_response = MagicMock()
        run_ps_response.status_code = status_code
        run_ps_response.std_out = std_out
        run_ps_response.std_err = std_err
        return run_ps_response

    @staticmethod
    def _get_from_ad_matching_nothing(user=None, cpr=None, server=None):
        # Provide a mock implementation of `ADWriter.get_from_ad` which returns nothing
        return {}

    @staticmethod
    def _get_from_ad_matching_manager(user=None, cpr=None, server=None):
        # Provide a mock implementation of `ADWriter.get_from_ad` which can look up the
        # manager's 'SamAccountName'.
        employee_ad_user = {
            "SamAccountName": "user_sam",
            "Manager": None,
        }
        manager_ad_user = {
            "SamAccountName": MO_MANAGER_SAM,
            "DistinguishedName": "manager-dn",
        }
        if cpr == MO_USER_CPR:  # == the MO employee's own CPR
            return [employee_ad_user]
        if cpr == MO_MANAGER_CPR:
            return [manager_ad_user]

    def _prepare_adwriter(self, **kwargs):
        template_to_ad_fields = kwargs.pop("template_to_ad_fields", {})
        template_to_ad_fields_when_disable = kwargs.pop(
            "template_to_ad_fields_when_disable", {}
        )
        skip_locations = kwargs.pop("skip_locations", None)
        read_ou_addresses = kwargs.pop("read_ou_addresses", None)
        get_from_ad = kwargs.pop("get_from_ad", self._get_from_ad_matching_nothing)
        with MockADWriterContext(
            template_to_ad_fields=template_to_ad_fields,
            template_to_ad_fields_when_disable=template_to_ad_fields_when_disable,
            skip_locations=skip_locations,
            read_ou_addresses=read_ou_addresses,
            run_ps_response=kwargs.pop("run_ps_response", None),
            use_future_managers=kwargs.pop("use_future_managers", True),
        ):
            instance = ADWriter(**kwargs)
            instance.get_from_ad = get_from_ad
            return instance


class TestInitNameCreator(_TestRealADWriter):
    """Test `ADWriter._init_name_creator`.

    In this test, we instantiate the 'real' `ADWriter` rather than the
    `ADWriterTestSubclass` used in `TestADWriter`. This is because we want to
    test the behavior of the real `ADWriter._init_name_creator` method, rather
    than the overridden method in `ADWriterTestSubclass`.
    """

    def test_init_name_creator_reads_ad_usernames(self):
        """Calling `ADWriter` with the default args should automatically load
        occupied usernames from AD.
        """
        ad_writer = self._prepare_adwriter()
        # Assert that the single AD user mocked by `MockADParameterReader` is
        # loaded as an occupied name.
        self.assertGreater(len(ad_writer.name_creator.occupied_names), 0)
        # Assert that we loaded the usernames from AD.
        self.assertEqual(len(ad_writer.name_creator._loaded_occupied_name_sets), 1)
        self.assertIsInstance(
            ad_writer.name_creator._loaded_occupied_name_sets[0], UserNameSetInAD
        )

    def test_init_name_creator_skips_ad_usernames(self):
        """Calling `ADWriter` with `skip_occupied_names=True` should skip
        loading occupied usernames from AD as well as other sources of occupied
        usernames.
        """
        ad_writer = self._prepare_adwriter(skip_occupied_names=True)
        # Assert that no occupied usernames were loaded, and no username sets
        # were instantiated.
        self.assertSetEqual(ad_writer.name_creator.occupied_names, set())
        self.assertEqual(len(ad_writer.name_creator._loaded_occupied_name_sets), 0)


class TestSyncCompare(_TestRealADWriter):
    _ad_user_employee = {
        "cpr_field": MO_USER_CPR,
        "SamAccountName": MO_USER_SAM,
        "Manager": "old_manager_dn",
    }

    def test_compare_fields_converts_ad_list(self):
        """Some AD fields contain one-element lists, rather than the usual strings,
        numbers or UUIDs.

        In such cases, we "unpack" the single-element list before comparing it to the
        corresponding MO value - otherwise the comparison will not work as expected.

        See: #47148
        """
        # Arrange
        mo_value = "mo_value"
        ad_list_element = "ad_list_element"
        ad_user = {"ad_field_name": [ad_list_element]}
        ad_writer = self._prepare_adwriter()
        # Act
        mismatch = ad_writer._compare_fields("ad_field_name", mo_value, ad_user)
        # Assert
        self.assertIn("ad_field_name", mismatch)
        self.assertEqual(mismatch["ad_field_name"], (ad_list_element, mo_value))

    @parameterized.expand(
        [
            # 1. The MO value is None, and the AD value is None - not a mismatch
            (None, None, {}),
            # 2. The MO value is "None", and the AD value is None - not a mismatch
            ("None", None, {}),
            # 3. The MO value is None, and the AD value is "None" - not a mismatch
            (None, "None", {}),
            # 4. The MO value is "None", and the AD value is "None" - not a mismatch
            ("None", "None", {}),
            # 5. Neither MO nor AD is None or "None" - not a mismatch
            ("not none", "not none", {}),
            # 6. The MO value is None, but the AD value is not None - a mismatch
            (None, "not none", {"ad_field_name": ("not none", None)}),
            # 7. The MO value is "None", but the AD value is not None - a mismatch
            ("None", "not none", {"ad_field_name": ("not none", None)}),
            # 8. The MO value is not None, but the AD value is None - a mismatch
            ("not none", None, {"ad_field_name": (None, "not none")}),
            # 9. The MO value is not None, but the AD value is "None" - a mismatch
            ("not none", "None", {"ad_field_name": (None, "not none")}),
        ]
    )
    def test_compare_fields_handles_none(self, mo_value, ad_value, expected_mismatch):
        """When comparing the MO value `"None"` to the AD value `None`, do not consider
        the AD and MO fields to differ - otherwise we will make a lot of pointless
        updates in AD, giving empty AD fields the value `"None"`.

        Even if no other AD fields need to be updated, this will cause `mo_to_ad_sync`
        to update every AD user, causing the program to take a very long time to finish.

        Also, consider the reverse situation (AD value is `"None"`, MO value is `None`)
        as a non-mismatch.

        See: #50291
        """
        # Arrange
        ad_user = {"ad_field_name": ad_value}
        ad_writer = self._prepare_adwriter()
        # Act
        mismatch = ad_writer._compare_fields("ad_field_name", mo_value, ad_user)
        # Assert
        self.assertEqual(mismatch, expected_mismatch)

    @parameterized.expand(
        [
            (
                [],  # manager not present in AD
                None,  # expected `mismatch["manager"]`
                "could not find manager by cpr",  # expected logging
            ),
            (
                # manager present in AD
                [{"cpr_field": "manager_cpr", "DistinguishedName": "manager_dn"}],
                ("old_manager_dn", "manager_dn"),  # expected `mismatch["manager"]`
                "Manager should be updated",  # expected logging
            ),
        ]
    )
    def test_handles_manager_cpr_not_in_ad_dump(
        self, ad_dump_extra, expected_manager_mismatch, expected_log_message
    ):
        """When `ADWriter.sync_user` is called from `run_mo_to_ad_sync`, an `ad_dump`
        is provided. However, sometimes `sync_user` cannot find the AD user specified
        by `mo_values["manager_cpr"] in `ad_dump` and crashes unexpectedly.

        This test verifies that we no longer crash in such cases, but simply do not
        update the `Manager` attribute in AD.

        See: #50160
        """
        ad_writer = self._prepare_adwriter()
        ad_dump = [
            # employee AD user
            self._ad_user_employee,
            # 0 or 1 manager AD users
            *ad_dump_extra,
        ]
        mo_values = {
            "cpr": MO_USER_CPR,
            "full_name": "Full Name",
            "manager_cpr": "manager_cpr",
        }
        with self.assertLogs() as actual_log_messages:
            mismatch = ad_writer._sync_compare(mo_values, ad_dump=ad_dump)

        # Assert we got the expected `mismatch["manager"]` value
        actual_manager_mismatch = mismatch.get("manager")
        self.assertEqual(actual_manager_mismatch, expected_manager_mismatch)

        # Assert we logged the expected message
        self.assertEqual(actual_log_messages.records[-1].message, expected_log_message)

    def test_sync_compare_skips_invalid_mo_values(self):
        ad_writer = self._prepare_adwriter(
            # Map MO unit address to a hypothetical AD field called `ad_field_name`
            template_to_ad_fields={"ad_field_name": "{{ mo_values['unit_city'] }}"},
            # Ensure that MO returns an invalid unit address (DAR offline scenario)
            read_ou_addresses={"Adresse": "Ukendt"},
        )
        ad_dump = [self._ad_user_employee]
        mo_values = ad_writer.read_ad_information_from_mo("uuid")

        with self.assertLogs() as actual_log_messages:
            mismatch = ad_writer._sync_compare(mo_values, ad_dump=ad_dump)

        # Assert that we did not consider the MO and AD values to be different and thus
        # need synchronization.
        self.assertNotIn("ad_field_name", mismatch)

        # Assert that we logged the expected message
        self.assertEqual(
            # Find matching log message
            only(
                record.message
                for record in actual_log_messages.records
                if "INVALID" in record.message
            ),
            # Expected content
            "'ad_field_name': MO value is INVALID, not changing AD value None",
        )

        # Assert that the generated PowerShell script does not include the invalid value
        with mock.patch.object(ad_writer, "_run_ps_script") as mock_run_ps_script:
            ad_writer.sync_user("mo_uuid", ad_dump=ad_dump)
            ps_script = mock_run_ps_script.call_args[0][0]
            self.assertNotIn(f'"ad_field_name"="{INVALID}"', ps_script)


class TestPreview(_TestRealADWriter):
    def test_preview_create_command(self):
        ad_writer = self._prepare_adwriter()
        create_cmds = ad_writer._preview_create_command(MO_UUID)
        self.assertEqual(len(create_cmds), 2)
        self.assertIn("New-ADUser", create_cmds[0])
        self.assertIn("Set-ADUser -Manager", create_cmds[1])

    def test_preview_sync_command_can_preview_rename_when_ad_dump_is_none(self):
        ad_writer = self._prepare_adwriter(
            get_from_ad=self._get_from_ad_matching_nothing
        )
        (
            sync_cmd,
            rename_cmd,
            rename_cmd_target,
            add_manager_cmd,
        ) = ad_writer._preview_sync_command(MO_UUID, "user_sam")
        # Examine 'sync_cmd'
        self.assertIn("Get-ADUser", sync_cmd)
        self.assertIn("Set-ADUser", sync_cmd)
        # Examine 'rename_cmd'
        self.assertIn("Get-ADUser", rename_cmd)
        self.assertIn("Rename-ADobject", rename_cmd)
        self.assertIn('-NewName "<new name>"', rename_cmd)
        self.assertEqual("<nonexistent AD user>", rename_cmd_target)
        # Examine 'add_manager_cmd'
        self.assertEqual("", add_manager_cmd)

    def test_preview_sync_command_can_preview_rename(self):
        reader = MockADParameterReaderWithManager()
        lc = MockLoraCacheWithManager()
        mo_name = lc._mo_values_employee["navn"]

        # Mock `ad_dump` which only contains an employee, and no manager
        ad_dump = [reader.read_user(cpr=MO_USER_CPR)]

        with MockADWriterContext(
            template_to_ad_fields={"Name": "{{ mo_values['full_name'] }}"}
        ):
            writer = ADWriter(lc=lc, lc_historic=lc)
            (
                sync_cmd,
                rename_cmd,
                rename_cmd_target,
                add_manager_cmd,
            ) = writer._preview_sync_command(MO_UUID, "user_sam", ad_dump=ad_dump)
            # Examine 'sync_cmd'
            self.assertIn("Get-ADUser", sync_cmd)
            self.assertIn("Set-ADUser", sync_cmd)
            # Examine 'rename_cmd'
            self.assertIn("Get-ADUser", rename_cmd)
            self.assertIn("Rename-ADobject", rename_cmd)
            self.assertIn(f'-NewName "{mo_name}"', rename_cmd)
            self.assertIsNone(rename_cmd_target)
            # Examine 'add_manager_cmd'
            self.assertEqual("", add_manager_cmd)

    def test_preview_sync_command_can_preview_add_manager(self):
        reader = MockADParameterReaderWithManager()
        lc = MockLoraCacheWithManager()
        ad_dump = [
            reader.read_user(cpr=MO_USER_CPR),  # employee
            reader.read_user(cpr=MO_MANAGER_CPR),  # manager
        ]
        with MockADWriterContext():
            writer = ADWriter(lc=lc, lc_historic=lc)
            (
                sync_cmd,
                rename_cmd,
                rename_cmd_target,
                add_manager_cmd,
            ) = writer._preview_sync_command(MO_UUID, "user_sam", ad_dump=ad_dump)
            # Examine 'sync_cmd'
            self.assertIn("Get-ADUser", sync_cmd)
            self.assertIn("Set-ADUser", sync_cmd)
            # Examine 'rename_cmd'
            self.assertEqual("", rename_cmd)
            # Examine 'add_manager_cmd'
            self.assertIn("Get-ADUser", add_manager_cmd)
            self.assertIn("Set-ADUser", add_manager_cmd)
            self.assertIn(f"-Manager {MO_MANAGER_SAM}", add_manager_cmd)

    @parameterized.expand(
        [
            # 1. Test disabling AD user without additional field mapping.
            (
                # `enable` argument
                False,
                # expected value of `-Enable` argument in PowerShell command
                "$false",
                # additional field mapping
                None,
                # expected output from additional field mapping
                None,
            ),
            # 2. Test disabling AD user with an additional field mapping.
            (
                # `enable` argument
                False,
                # expected value of `-Enable` argument in PowerShell command
                "$false",
                # additional field mapping
                {"extensionAttribute": "{{ now.strftime('%Y-%m-%d') }}"},
                # expected output from additional field mapping
                '"extensionAttribute"="%s"' % datetime.now().strftime("%Y-%m-%d"),
            ),
            # 3. Test enabling AD user without additional field mapping.
            (
                # `enable` argument
                True,
                # expected value of `-Enable` argument in PowerShell command
                "$true",
                # additional field mapping
                None,
                # expected output from additional field mapping
                None,
            ),
            # 4. Test enabling AD user with an additional field mapping.
            # Since we are enabling the AD user, the additional field mapping should
            # not be used.
            (
                # `enable` argument
                True,
                # expected value of `-Enable` argument in PowerShell command
                "$true",
                # additional field mapping
                {"extensionAttribute": "{{ now.strftime('%Y-%m-%d') }}"},
                # expected output from additional field mapping
                None,
            ),
        ]
    )
    def test_preview_enable_command(
        self,
        enable_arg: bool,
        expected_enabled_arg_value: str,
        additional_field_mapping: dict,
        expected_additional_output: str,
    ):
        ad_writer_context_kwargs = (
            {"template_to_ad_fields_when_disable": additional_field_mapping}
            if additional_field_mapping
            else {}
        )
        ad_writer = self._prepare_adwriter(**ad_writer_context_kwargs)
        enable_cmd = ad_writer._get_enable_user_cmd("sam_account_name", enable_arg)
        self.assertIn("Get-ADUser", enable_cmd)
        self.assertIn("Set-ADUser", enable_cmd)
        self.assertIn("-Enabled %s" % expected_enabled_arg_value, enable_cmd)
        if expected_additional_output:
            self.assertIn(expected_additional_output, enable_cmd)


class TestReadADInformationFromMO(_TestRealADWriter):
    @parameterized.expand(
        [
            # When DAR lookups work, MO returns addresses on the format
            # "Testvej 123, 1234 Testby".
            # Check that we parse such values correctly into its constituent parts.
            (
                {"Adresse": "Testvej 123, 1234 Testby"},
                {"postal_code": "1234", "city": "Testby", "streetname": "Testvej 123"},
            ),
            # When DAR is offline, MO returns addresses with the literal value "Ukendt".
            # We convert them into invalid unit addresses.
            (
                {"Adresse": "Ukendt"},
                ADWriter.INVALID_UNIT_ADDRESS,
            ),
            # If we get an invalid Danish postal address, we also convert them into an
            # invalid unit address.
            (
                {"Adresse": "Ikke en gyldig dansk postadresse"},
                ADWriter.INVALID_UNIT_ADDRESS,
            ),
            # Empty MO response causes an invalid unit address as well.
            (
                {},
                ADWriter.INVALID_UNIT_ADDRESS,
            ),
        ]
    )
    def test_parsed_addresses(self, address, expected_parsed_address):
        ad_writers = (
            ("ADWriter using MO API", self._get_mo_ad_writer(address)),
            ("ADWriter using LoraCache", self._get_loracache_ad_writer(address)),
        )
        for name, ad_writer in ad_writers:
            with self.subTest(name):
                mo_values = ad_writer.read_ad_information_from_mo(MO_UUID)
                self.assertEqual(
                    mo_values["_parsed_addresses"], expected_parsed_address
                )
                if expected_parsed_address is ADWriter.INVALID_UNIT_ADDRESS:
                    self.assertEqual(mo_values["unit_postal_code"], INVALID)
                    self.assertEqual(mo_values["unit_city"], INVALID)
                    self.assertEqual(mo_values["unit_streetname"], INVALID)

    def _get_mo_ad_writer(self, address):
        return self._prepare_adwriter(read_ou_addresses=address)

    def _get_loracache_ad_writer(self, address):
        lc = MockLoraCacheUnitAddress(address_value=address.get("Adresse"))
        return self._prepare_adwriter(lc=lc, lc_historic=lc)


class TestEnableUser(_TestRealADWriter):
    @parameterized.expand(
        [
            (False, "disabled AD user"),
            (True, "enabled AD user"),
        ]
    )
    def test_enable_user_handles_ok_response(self, enable: bool, expected_msg: str):
        # Arrange: mock an "OK" response from WinRM
        ad_writer = self._prepare_adwriter(run_ps_response=self.mock_run_ps_response())
        # Act
        result = ad_writer.enable_user("sam_account_name", enable)
        # Assert (here `True` means the PowerShell command executed successfully)
        self.assertEqual(result, (True, expected_msg))

    def test_enable_user_handles_error_response(self):
        # Arrange: mock an "error" response from WinRM
        ad_writer = self._prepare_adwriter(
            run_ps_response=self.mock_run_ps_response(
                status_code=1,
                std_err="something wrong",
            )
        )
        # Act and assert
        with self.assertRaises(CommandFailure):
            ad_writer.enable_user("sam_account_name", False)

    def test_get_enable_user_cmd_strips_linebreaks(self):
        ad_writer = self._prepare_adwriter()
        ps_script = ad_writer._get_enable_user_cmd("user", enable=False)
        # Find actual "Get-ADUser NNN | Set-ADUser NNN -Enabled $false" line in PS
        match = re.search("Get-ADUser .*", ps_script, re.MULTILINE)
        # Assert that this line does not contain any unintended line breaks, caused by
        # forgetting to call `AD.remove_redundant`.
        assert "\n" not in match.group()


class TestSkipLocation(_TestRealADWriter):
    _combinations = [
        # 1. If `skip_locations` is not set, expect `mo_values` to be present, and
        # contain the expected location.
        (
            # Value of the `skip_locations` setting
            None,
            # Assert that `mo_values` is returned and contains the expected location
            lambda mo_values: mo_values["location"] == MO_ROOT_ORG_UNIT_NAME,
        ),
        # 2. If `skip_locations` is set, but does not match the location in `mo_values`,
        # expect `mo_values` to be present and contain the expected location.
        (
            ["Something that is not MO_ROOT_ORG_UNIT_NAME"],
            lambda mo_values: mo_values["location"] == MO_ROOT_ORG_UNIT_NAME,
        ),
        # 3. If `skip_locations` is set, and matches the location in `mo_values`,
        # expect `mo_values` to be None, indicating that this MO user should be skipped.
        (
            [MO_ROOT_ORG_UNIT_NAME],
            lambda mo_values: mo_values is None,
        ),
    ]

    @parameterized.expand(_combinations)
    def test_skip_location_mo(self, skip_locations, test_condition):
        """Test `ADWriter.read_ad_information_from_mo` when reading from MO"""
        ad_writer = self._prepare_adwriter(skip_locations=skip_locations)
        mo_values = ad_writer.read_ad_information_from_mo(MO_UUID)
        assert test_condition(mo_values)

    @parameterized.expand(_combinations)
    def test_skip_location_loracache(self, skip_locations, test_condition):
        """Test `ADWriter.read_ad_information_from_mo` when reading from LoraCache"""
        lc = MockLoraCacheUnitAddress(address_value=None)
        ad_writer = self._prepare_adwriter(
            skip_locations=skip_locations, lc=lc, lc_historic=lc
        )
        mo_values = ad_writer.read_ad_information_from_mo(MO_UUID)
        assert test_condition(mo_values)

    @given(
        st.lists(st.text()) | st.none(),
        st.builds(lambda xs: "\\".join(xs), st.lists(st.text())) | st.none(),
    )
    def test_skip_unit(self, skip_locations, location):
        ad_writer = self._prepare_adwriter(skip_locations=skip_locations)
        result = ad_writer._skip_unit({"location": location})
        if not skip_locations:
            assert result is False
        elif location and any(part in skip_locations for part in location.split("\\")):
            assert result is True
        else:
            assert result is None


class TestRenameADUser(_TestRealADWriter):
    def test_sleep(self):
        # Arrange: mock a successful WinRM invocation
        ad_writer = self._prepare_adwriter(run_ps_response=self.mock_run_ps_response())

        with mock.patch("time.sleep") as mock_time_sleep:
            # Act
            ad_writer._rename_ad_user("user_sam", "New Name")
            # Assert
            mock_time_sleep.assert_called_once_with(1)


class TestGetManagerUUID(_TestRealADWriter):
    def test_get_manager_uuid(self):
        ad_writer = self._prepare_adwriter(
            get_from_ad=self._get_from_ad_matching_manager
        )
        mo_values = ad_writer.read_ad_information_from_mo(MO_UUID)
        assert mo_values["_manager_uuid"] == MO_MANAGER_UUID
        assert mo_values["_manager_mo_user"]["uuid"] == MO_MANAGER_UUID
        assert mo_values["read_manager"] is True
        assert mo_values["manager_name"] == mo_values["_manager_mo_user"]["name"]
        assert mo_values["manager_cpr"] == mo_values["_manager_mo_user"]["cpr_no"]
        assert mo_values["manager_mail"] == "address-value"
        assert mo_values["manager_sam"] == MO_MANAGER_SAM


class TestUseFutureManagersFeatureFlag:
    def test_effect_when_false(self, caplog):
        with MockADWriterContext(use_future_managers=False):
            with caplog.at_level(logging.INFO):
                ad_writer = ADWriter()
                # Assert that `ADWriter._use_graphql_source_if_feature_flagged` does
                # not monkeypatch `self.datasource.get_manager_uuid`.
                assert inspect.getsourcelines(
                    ad_writer.datasource.get_manager_uuid
                ) == inspect.getsourcelines(MockMORESTSource.get_manager_uuid)
                assert "Not using MOGraphqlSource" in caplog.text

    def test_effect_when_true(self, caplog):
        with MockADWriterContext(use_future_managers=True):
            with caplog.at_level(logging.INFO):
                ad_writer = ADWriter()
                # Assert that `ADWriter._use_graphql_source_if_feature_flagged`
                # monkeypatches `self.datasource.get_manager_uuid` when True.
                assert inspect.getsourcelines(
                    ad_writer.datasource.get_manager_uuid
                ) == inspect.getsourcelines(MockMOGraphqlSource.get_manager_uuid)
                assert "Using MOGraphqlSource to patch" in caplog.text


class TestUpdateManager(_TestRealADWriter):
    # Mock an AD with an employee and a manager. The employee does not yet have its
    # `Manager` field set, so `ADWriter.sync_user` should try to update it.
    _ad_dump = [
        # Employee
        {
            "cpr_field": MO_USER_CPR,
            "SamAccountName": MO_USER_SAM,
            "Manager": None,
        },
        # Manager
        {
            "cpr_field": MO_MANAGER_CPR,
            "SamAccountName": MO_MANAGER_SAM,
            "DistinguishedName": "foo",
        },
    ]

    @parameterized.expand(
        [
            # Case 1: `ADWriter.datasource.get_manager_uuid` returns a manager UUID for
            # the employee in question. The AD user should be updated.
            (
                # Manager UUID
                MO_MANAGER_UUID,
                # Assertion
                lambda mock_add_manager: mock_add_manager.assert_called_once_with(
                    user_sam=MO_USER_SAM,
                    manager_sam=MO_MANAGER_SAM,
                ),
            ),
            # Case 2: `ADWriter.datasource.get_manager_uuid` returns None for the
            # employee in question. The AD user should *not* be updated.
            (
                # Manager UUID
                None,
                # Assertion
                lambda mock_add_manager: mock_add_manager.assert_not_called(),
            ),
        ]
    )
    def test_sync_user_calls_add_manager_to_user(self, mo_manager_uuid, assertion):
        ad_writer = self._prepare_adwriter(run_ps_response=self.mock_run_ps_response())
        with mock.patch.object(
            ad_writer.datasource,
            "get_manager_uuid",
            return_value=mo_manager_uuid,
        ):
            with mock.patch.object(
                ad_writer, "add_manager_to_user"
            ) as mock_add_manager:
                ad_writer.sync_user(MO_UUID, ad_dump=self._ad_dump, sync_manager=True)
                assertion(mock_add_manager)
