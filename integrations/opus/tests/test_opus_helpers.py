import unittest
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from hypothesis import given
from hypothesis.strategies import text
from parameterized import parameterized

from integrations.opus import opus_helpers

testfile1 = Path.cwd() / "integrations/opus/tests/ZLPE20200101_delta.xml"
testfile2 = Path.cwd() / "integrations/opus/tests/ZLPE20200102_delta.xml"


class test_opus_helpers(TestCase):
    @given(text(), text())
    def test_generate_uuid(self, value, municipality_name):
        uuid1 = opus_helpers.generate_uuid(value, municipality_name)
        uuid2 = opus_helpers.generate_uuid(value, municipality_name)
        self.assertEqual(uuid1, uuid2)

    @parameterized.expand(
        [
            (testfile1,),
            (testfile2,),
        ]
    )
    def test_file_diff_same(self, file):
        file_diffs = opus_helpers.file_diff(file, file, disable_tqdm=True)
        self.assertEqual(file_diffs["units"], [])
        self.assertEqual(file_diffs["employees"], [])

    def test_file_diff(self):
        file_diffs = opus_helpers.file_diff(testfile1, testfile2, disable_tqdm=True)
        self.assertNotEqual(file_diffs["units"], [])
        self.assertNotEqual(file_diffs["employees"], [])

    @parameterized.expand(
        [
            (testfile1,),
            (testfile2,),
        ]
    )
    def test_parser(self, file):
        self.units, self.employees = opus_helpers.parser(file)
        self.assertIsInstance(self.units, list)
        self.assertIsInstance(self.employees, list)
        self.assertIsInstance(self.units[0], dict)
        self.assertIsInstance(self.employees[0], dict)

    @parameterized.expand(
        [
            (testfile1, [], set()),
            (testfile2, [], set()),
            (testfile1, ["1"], {"1", "2", "3"}),
            (testfile2, ["1"], {"1", "2", "3", "4", "5"}),
        ]
    )
    def test_all_filtered_ids_fiter_top_unit(self, file, filter_ids, expected):
        all_filtered = opus_helpers.find_all_filtered_units(file, filter_ids)
        self.assertEqual(len(all_filtered), len(expected))
        self.assertEqual({ou["@id"] for ou in all_filtered}, expected)

    @parameterized.expand(
        [
            (testfile1, 1),
            (testfile2, 2),
        ]
    )
    def test_split_employees_active(self, inputfile, expected):
        file_diffs = opus_helpers.file_diff(None, inputfile, disable_tqdm=True)
        employees, leaves = opus_helpers.split_employees_leaves(file_diffs["employees"])
        employees = list(employees)
        leaves = list(leaves)
        self.assertEqual(len(leaves), expected)

    @parameterized.expand(
        [
            ([], [3, 0, 4, 1, 0], None),
            (["1"], [0, 3, 0, 1, 0], None),
            # Read specific opus_id
            ([], [1, 0, 0, 0, 0], 1),
            (["1"], [0, 3, 0, 0, 0], 1),
        ]
    )
    def test_full_(self, filter_ids, expected, opus_id):
        data = opus_helpers.read_and_transform_data(
            None,
            testfile1,
            filter_ids,
            disable_tqdm=True,
            opus_id=opus_id,
        )
        # data is a tuple of units, filtered units, employess, terminated employees and canceled_employees
        # test that the length of each is as expected
        for i, x in enumerate(data):
            this = list(x)
            assert len(this) == expected[i], (
                f"Expected {expected[i]} objects, got {len(this)}\n{this}"
            )

    def test_find_changed_parent(self):
        org1, _ = opus_helpers.parser(testfile1)
        org2 = deepcopy(org1)
        org2[2]["parentOrgUnit"] = "CHANGED!"
        diffs = opus_helpers.find_changes(before=org1, after=org2, disable_tqdm=True)
        self.assertEqual(diffs, [org2[2]])

    def test_find_cancelled(self):
        file_diffs = opus_helpers.file_diff(testfile1, testfile2, disable_tqdm=True)
        assert len(file_diffs["cancelled_employees"]) == 1, (
            "Expected to find 1 cancelled employee"
        )


if __name__ == "__main__":
    unittest.main()
