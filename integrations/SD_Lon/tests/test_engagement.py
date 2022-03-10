import unittest

from more_itertools import one
from parameterized import parameterized

from integrations.SD_Lon.engagement import _is_external
from integrations.SD_Lon.engagement import (
    is_employment_id_and_no_salary_minimum_consistent,
)
from integrations.SD_Lon.tests.fixtures import get_read_employment_changed_fixture


class TestIsExternal(unittest.TestCase):
    def test_is_external_is_false_for_number_employment_id(self):
        assert not _is_external("12345")
        assert not _is_external("1")

    def test_is_external_is_true_for_non_number_employment_id(self):
        assert _is_external("ABCDE")


class TestIsEmploymentIdAndNoSalaryMinimumConsistent(unittest.TestCase):
    @parameterized.expand(
        [
            (None, "12345", 1, True),
            (9000, "External", 10000, True),
            (9000, "External", 8000, False),
            (9000, "12345", 8000, True),
            (9000, "12345", 10000, False),
        ]
    )
    def test_return_values(
        self, no_salary_minimum, employment_id, job_pos_id, expected
    ):
        engagement = one(
            get_read_employment_changed_fixture(
                employment_id=employment_id,
                job_pos_id=job_pos_id,
            )
        )["Employment"]

        assert (
            is_employment_id_and_no_salary_minimum_consistent(
                engagement, no_salary_minimum
            )
            == expected
        )