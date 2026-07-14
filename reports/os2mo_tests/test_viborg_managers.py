#
# Copyright (c) 2017-2018, Magenta ApS
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#

"""
in order to run the os2mo tests Your python-environment must
have all os2mo-requirements and os2mo it self installed.

Furthermore You must define the path to the os2mo source directory

in order to record a test result use this temporarily in a test,
adapted to Your test: self.cache_new_result(x=1, y=2)
"""

import json
import os
import pathlib
import sys

import mock
import requests
import viborg_managers
from freezegun import freeze_time

from integrations.mora_helper import MoraHelper

sys.path[0:0] = [
    os.environ["OS2MO_SRC_DIR"] + "/backend/tests",
    pathlib.Path(__file__).parent / "..",  # type: ignore
]
import util  # noqa


testdata = pathlib.Path(__file__).resolve().parent / "data"


@freeze_time("2019-08-26")
class Tests(util.LoRATestCase):
    maxDiff = None

    def cache_new_result(self, **kwargs):
        with open(self._test_data_result, "w", encoding="utf-8") as f:
            json.dump(kwargs, f, indent=4, ensure_ascii=False)

    def read_old_result(self, **kwargs):
        with open(self._test_data_result, "r", encoding="utf-8") as f:
            return json.load(f)

    def get(self, *args, **kwargs):
        args = list(args)
        # recognize mo?
        if "/service/" in args[0]:
            args[0] = "/service" + args[0].split("/service")[-1]
            response = self.client.open(*args, follow_redirects=True)
            response_mock = mock.MagicMock()
            response_mock.status_code = response.status_code
            response_mock.ok = response.status_code == 200
            response_mock.json = lambda: response.get_json()
            return response_mock
        else:
            return requests._orgget(*args, **kwargs)

    def setUp(self):
        super().setUp()
        util.amqp.publish_message = lambda a, b, c, d, e: None
        self.mh = MoraHelper()
        if not getattr(requests, "_orgget", False):
            requests._orgget = requests.get
            requests.get = self.get
        self._test_data_result = str(
            testdata
            / (
                pathlib.Path(__file__).stem
                + "_"
                + self._testMethodName
                + "_result.json"
            )
        )

    def tearDown(self):
        if getattr(requests, "_orgget", False):
            requests.get = requests._orgget
            del requests._orgget

    def run_report(self):
        self.mh.read_organisation()
        root_uuid = viborg_managers.get_root_org_unit_uuid(self.mh, "Hjørring Kommune")
        nodes = self.mh.read_ou_tree(root_uuid)
        viborg_managers.find_people(self.mh, nodes)
        fieldnames, rows = viborg_managers.prepare_report(self.mh, nodes)
        rows = viborg_managers.collapse_same_manager_more_departments(rows)
        return fieldnames, rows

    def test_normal(self):
        "initially approved report run on the normal.sql fixture from os2mo tests"
        self.load_sql_fixture("normal.sql")
        fieldnames, rows = self.run_report()

        reference = self.read_old_result()
        self.assertEqual(len(reference["rows"]), len(rows))
        self.assertEqual(reference["fieldnames"], fieldnames)
        for ref, this in zip(reference["rows"], rows):
            self.assertEqual(ref, this)

    def test_collapsed_managers(self):
        self.load_sql_fixture("normal.sql")
        # now terminate 2 managers
        # and give these departments to a third
        # OBS - nobody is fired - they are still employees

        # Edith Højlund Rasmussen i Renovation
        # Kai Juul Svendsen, Park og vej
        # are stripped of manager roles
        for_termination = [
            {
                "type": "manager",
                "uuid": "7f61490f-2fc4-41ce-9a67-668969702d50",
                "validity": {"to": "2019-08-25"},
                "force": True,
            },
            {
                "type": "manager",
                "uuid": "d5916fa5-96b1-4640-a71c-a6d7e69d3980",
                "validity": {"to": "2019-08-25"},
                "force": True,
            },
        ]
        for t in for_termination:
            self.assertRequest("/service/details/terminate", json=t)

        # Axel Krumbæk Dam from IT-support is now
        # boss in the other two depts too
        for_adding = [
            {
                "type": "manager",
                "person": {"uuid": "d53a3cce-2054-4002-9099-980ea5bd6129"},
                "responsibility": [{"uuid": "cf08cacb-7c7d-49f2-8b4e-d7c4b8ab233c"}],
                "validity": {"from": "2019-08-01", "to": None},
                "manager_type": {"uuid": "d4c5983b-c4cd-43f2-b18a-653387172b08"},
                "manager_level": {"uuid": "049fb201-fc32-40e3-80c7-4cd7cb89a9a3"},
                "org": {"uuid": "c5395419-4c76-417f-9939-5a4bf81648d8"},
                "org_unit": {"uuid": "dac3b1ef-3d36-4464-9839-f611a4215cb5"},
            },
            {
                "type": "manager",
                "person": {"uuid": "d53a3cce-2054-4002-9099-980ea5bd6129"},
                "responsibility": [{"uuid": "cf08cacb-7c7d-49f2-8b4e-d7c4b8ab233c"}],
                "validity": {"from": "2019-08-01", "to": None},
                "manager_type": {"uuid": "d4c5983b-c4cd-43f2-b18a-653387172b08"},
                "manager_level": {"uuid": "049fb201-fc32-40e3-80c7-4cd7cb89a9a3"},
                "org": {"uuid": "c5395419-4c76-417f-9939-5a4bf81648d8"},
                "org_unit": {"uuid": "1a477478-41b4-4806-ac3a-e220760a0c89"},
            },
        ]
        for a in for_adding:
            self.assertRequest("/service/details/create", json=a)

        #  run report and compare result with reference
        fieldnames, rows = self.run_report()
        reference = self.read_old_result()
        self.assertEqual(len(reference["rows"]), len(rows))
        self.assertEqual(reference["fieldnames"], fieldnames)
        for ref, this in zip(reference["rows"], rows):
            self.assertEqual(ref, this)
