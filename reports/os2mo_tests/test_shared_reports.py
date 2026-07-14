#!/usr/bin/env python3
# --------------------------------------------------------------------------------------
# Imports
# --------------------------------------------------------------------------------------
import json

import pandas as pd
import pytest
from anytree import Node
from pandas._testing import assert_frame_equal

from integrations.mora_helper import MoraHelper
from reports.shared_reports import CustomerReports
from reports.shared_reports import report_to_csv

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def mock_data(test_data):
    mock_data_file = test_data / "shared_reports_mock.json"
    return json.load(mock_data_file.open())


@pytest.fixture(autouse=True)
def mock_mora_init(monkeypatch):
    monkeypatch.setattr(MoraHelper, "read_organisation", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        MoraHelper,
        "read_top_units",
        lambda *args, **kwargs: [{"name": "test", "uuid": "nice_uuid"}],
    )
    monkeypatch.setattr(
        MoraHelper, "read_ou_tree", lambda *args, **kwargs: {"root": Node("test")}
    )


@pytest.fixture
def mock_mora(monkeypatch, mock_data):
    def _mock_mora(func: str):
        monkeypatch.setattr(
            MoraHelper,
            func,
            lambda *args, **kwargs: mock_data[func],
        )

    return _mock_mora


@pytest.fixture
def reload_report(temp_dir):
    def _reload_report(report: pd.DataFrame):
        csv_out = temp_dir / "test.csv"
        report_to_csv(report, csv_out)
        csv_report: pd.DataFrame = pd.read_csv(csv_out, sep=";", dtype="object")
        return csv_report

    return _reload_report


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------


class TestCustomerReports:
    host = "http://lol"
    customer = "test"

    @pytest.fixture
    def reports(self):
        yield CustomerReports(hostname=self.host, org_name=self.customer)

    def test_init(self, monkeypatch):
        # Our mora_init mock should work
        assert CustomerReports(hostname=self.host, org_name=self.customer)

        # Customer not in top units (overwrite monkeypatch from MoraHelper init fixture)
        monkeypatch.setattr(
            MoraHelper,
            "read_top_units",
            lambda *args, **kwargs: [{"name": "fail", "uuid": "nice_uuid"}],
        )
        err_msg = f"Organisation unit {self.customer} not found in organisation units"

        with pytest.raises(ValueError, match=err_msg):
            CustomerReports(hostname=self.host, org_name=self.customer)

    def test_employees(self, mock_mora, reports, reload_report):
        mock_mora("read_organisation_people")
        mock_mora("read_user_address")
        employees = reports.employees()
        assert_frame_equal(employees, reload_report(employees))

    def test_managers(self, mock_mora, reports, reload_report):
        mock_mora("read_ou_manager")
        mock_mora("read_user_address")
        mock_mora("_create_path_dict")
        managers = reports.managers()
        assert_frame_equal(managers, reload_report(managers))

    def test_organisation_overview(self, reports, mock_mora, reload_report):
        mock_mora("read_ou_address")
        mock_mora("_create_path_dict")

        organisations = reports.organisation_overview()
        assert_frame_equal(organisations, reload_report(organisations))

    def test_organisation_employees(self, reports, mock_mora, reload_report):
        mock_mora("read_ou_address")
        mock_mora("read_organisation_people")
        mock_mora("read_user_address")
        mock_mora("_create_path_dict")

        organisations = reports.organisation_employees()
        assert_frame_equal(organisations, reload_report(organisations))

    def test_organisation_units(self, reports, mock_mora, reload_report):
        mock_mora("read_ou")
        organisation_units = reports.organisation_units()
        assert_frame_equal(organisation_units, reload_report(organisation_units))
