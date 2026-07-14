#
# Copyright (c) 2017-2018, Magenta ApS
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
import csv
import os
import unittest

from chardet.universaldetector import UniversalDetector

from exporters import common_queries as cq
from integrations.mora_helper import MoraHelper

MORA_BASE = os.environ.get("MORA_BASE", "http://localhost:5000")
PATH_TO_TEST_FILE = os.environ.get(
    "PATH_TO_TEST_FILE",
    "exporters/tests/data/test_data_alle_ledere_uden_nedarvinger_os2mo.csv",
)


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        self.morah = MoraHelper(hostname=MORA_BASE)
        org = self.morah.read_organisation()
        # This assumes a single top-unit. Tests will fail if we have more.
        roots = self.morah.read_top_units(org)
        self.nodes = self.morah.read_ou_tree(roots[0]["uuid"])
        self.counts = self.morah._mo_lookup(org, "o/{}/")

        cq.export_orgs(self.morah, self.nodes, "all_employees.csv")
        cq.export_orgs(self.morah, self.nodes, "all_orgs.csv", include_employees=False)
        cq.export_managers(self.morah, self.nodes, "all_managers.csv")
        cq.export_adm_org(self.morah, self.nodes, "adm_org.csv")

    @classmethod
    def tearDownClass(self):
        os.remove("all_employees.csv")
        os.remove("all_orgs.csv")
        os.remove("all_managers.csv")
        os.remove("adm_org.csv")

    def _load_csv(self, filename):
        rows = []
        detector = UniversalDetector()
        with open(filename, "rb") as csvfile:
            for row in csvfile:
                detector.feed(row)
                if detector.done:
                    break
        detector.close()
        encoding = detector.result["encoding"]
        with open(filename, encoding=encoding) as csvfile:
            reader = csv.DictReader(csvfile, delimiter=";")
            for row in reader:
                rows.append(row)
        return rows

    def test_node_levels(self):
        """Test that the tree is not flat"""
        height = self.nodes["root"].height
        self.assertTrue(height > 2)

    def test_user_names(self):
        """Test that we did not mistakingly write the same username for
        multiple users. This could for example happen if we mistankingly
        exports the same use for every employee in an ou. We allow a
        small difference (15%) since it actally happens that an employee
        is listed more than once."""
        rows = self._load_csv("all_employees.csv")
        usernames = []
        for row in rows:
            if row["Brugernavn"] not in usernames:
                usernames.append(row["Brugernavn"])
        self.assertTrue(len(usernames) > len(rows) * 0.85)

    def test_adm_orgs(self):
        """Test that we have exported all OUs exactly once"""
        rows = self._load_csv("adm_org.csv")
        self.assertEqual(len(rows), len(self.nodes))

    def test_all_employees(self):
        """Test that we have exported all employees exactly once"""
        rows = self._load_csv("all_employees.csv")
        self.assertEqual(len(rows), self.counts["engagement_count"])

    def test_consistency(self):
        rows = self._load_csv("all_orgs.csv")
        self.assertEqual(len(rows), len(self.nodes))

    def test_empty_manager_fields(self):
        """
        Tests if fields in manager payloads can indeed be written as empty values,
        as they previously have been populated by leaders higher up in hierarchy.
        """
        rows = self._load_csv(PATH_TO_TEST_FILE)
        empty_manager_fields_from_csv = {
            "2xsub org": "",
            "3xsub org": "",
            "Ansvar": "",
            "E-mail": "",
            "Navn": "",
            "Telefon": "",
            "org": "Teknik og Miljø",
            "root": "Kolding Kommune",
            "sub org": "",
        }
        # Manager data is pulled in from "tests/datatest_data_alle_ledere_uden_nedarvinger_os2mo.csv", and rows can be
        # found in, the test data file "test_data_alle_ledere_uden_nedarvinger_os2mo.csv".
        assert rows[1] == empty_manager_fields_from_csv


if __name__ == "__main__":
    unittest.main()
