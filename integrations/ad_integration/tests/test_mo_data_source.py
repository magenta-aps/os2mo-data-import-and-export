from unittest import TestCase

import pytest

from ..ad_writer import EngagementDatesError
from ..ad_writer import LoraCacheSource
from ..ad_writer import MOGraphqlSource
from ..ad_writer import MORESTSource
from ..utils import AttrDict
from .mocks import MockMORESTSource


def mock_MOGraphqlSource(return_value):
    mock_source = MOGraphqlSource
    mock_source._read_employees = lambda x, y: return_value
    return mock_source({})


class TestMOGraphqlSource(TestCase):
    # TODO: parameterize or use hypothesis
    def test_create_manager_map_no_manager(self):
        test_data = [
            {
                "uuid": "0004b952-a513-430b-b696-8d393d7eb2bb",
                "objects": [{"engagements": []}],
            }
        ]
        mock_source = mock_MOGraphqlSource(test_data)
        assert mock_source.manager_map == {"0004b952-a513-430b-b696-8d393d7eb2bb": None}

    def test_create_manager_map_has_manager(self):
        test_data = [
            {
                "uuid": "002a1aed-d015-4b86-86a4-c37cd8df1e18",
                "objects": [
                    {
                        "engagements": [
                            {
                                "employee_uuid": "002a1aed-d015-4b86-86a4-c37cd8df1e18",
                                "uuid": "4656c111-d1e5-4a0d-9514-633b03e4f60f",
                                "is_primary": True,
                                "org_unit": [
                                    {
                                        "managers": [
                                            {
                                                "employee_uuid": "30b2f8fa-e7c6-43c3-ae59-6649b60e78d2"
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
        mock_source = mock_MOGraphqlSource(test_data)
        assert mock_source.manager_map == {
            "002a1aed-d015-4b86-86a4-c37cd8df1e18": "30b2f8fa-e7c6-43c3-ae59-6649b60e78d2"
        }

    def test_create_manager_map_has_manager_not_primary(self):
        test_data = [
            {
                "uuid": "002a1aed-d015-4b86-86a4-c37cd8df1e18",
                "objects": [
                    {
                        "engagements": [
                            {
                                "employee_uuid": "002a1aed-d015-4b86-86a4-c37cd8df1e18",
                                "uuid": "4656c111-d1e5-4a0d-9514-633b03e4f60f",
                                "is_primary": None,
                                "org_unit": [
                                    {
                                        "managers": [
                                            {
                                                "employee_uuid": "30b2f8fa-e7c6-43c3-ae59-6649b60e78d2"
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
        mock_source = mock_MOGraphqlSource(test_data)
        assert mock_source.manager_map == {"002a1aed-d015-4b86-86a4-c37cd8df1e18": None}


class TestLoraCacheSource(TestCase):
    def setUp(self):
        self.user = self.setup_user()
        self.lc = AttrDict(
            {
                "users": {
                    self.user["uuid"]: [self.user],
                },
                "engagements": {
                    "key-1": [
                        # Current registration
                        {
                            "user": self.user["uuid"],
                            "from_date": "2020-01-01",
                            "to_date": None,
                        },
                        # Previous registration
                        {
                            "user": self.user["uuid"],
                            "from_date": "2019-01-01",
                            "to_date": "2021-01-01",
                        },
                    ],
                },
            }
        )
        self.lc_historic = AttrDict(
            {
                "engagements": {
                    # Add some historic engagements that must not be read by
                    # `get_engagement_dates`.
                    "key-1": [
                        # Current historic registration
                        {
                            "user": self.user["uuid"],
                            "from_date": "2010-01-01",
                            "to_date": "2019-12-31",
                        },
                    ]
                }
            }
        )

    def setup_user(self):
        return {
            "uuid": "some_uuid_here",
            "navn": "some_name some_lastname",
            "efternavn": "some_lastname",
            "fornavn": "some_name",
            "kaldenavn": "some_alias some_lastname_alias",
            "kaldenavn_fornavn": "some_alias",
            "kaldenavn_efternavn": "some_lastname_alias",
            "cpr": "some_cpr",
        }

    def test_read_user(self):
        datasource = self._get_datasource(None, None)
        result = datasource.read_user("some_uuid_here")
        self.assertEqual(
            result,
            {
                "uuid": "some_uuid_here",
                "name": "some_name some_lastname",
                "surname": "some_lastname",
                "givenname": "some_name",
                "nickname": "some_alias some_lastname_alias",
                "nickname_givenname": "some_alias",
                "nickname_surname": "some_lastname_alias",
                "cpr_no": "some_cpr",
            },
        )

    def test_get_engagement_dates(self):
        datasource = self._get_datasource("2020-01-01", None)
        result = datasource.get_engagement_dates(self.user["uuid"])
        self.assertEqual(
            [list(elem) for elem in result],  # consume each iterable in result
            [["2020-01-01"], [None]],
        )

    def test_get_engagement_endpoint_dates(self):
        datasource = self._get_datasource("2020-01-01", None)
        result = datasource.get_engagement_endpoint_dates(self.user["uuid"])
        # "to_date" of None must be converted into "9999-12-31"
        self.assertEqual(result, ("2020-01-01", "9999-12-31"))

    def _get_datasource(self, from_date, to_date):
        return LoraCacheSource(
            self.lc, self.lc_historic, MockMORESTSource(from_date, to_date)
        )


class TestEngagementDates:
    MOSource = MORESTSource(settings={"global": {"mora.base": "http://lol.money:1337"}})

    def test_valid_interval(self, monkeypatch):
        start, end = "2021-01-01", "2021-02-02"

        def _valid_dates(*args):
            return [start], [end]

        monkeypatch.setattr(self.MOSource, "get_engagement_dates", _valid_dates)
        assert self.MOSource.get_engagement_endpoint_dates("test") == (start, end)

    def test_sentinels(self, monkeypatch):
        start, end = "1930-01-01", "9999-12-31"

        def _falsy_dates(*args):
            return [None], [""]

        monkeypatch.setattr(self.MOSource, "get_engagement_dates", _falsy_dates)
        assert self.MOSource.get_engagement_endpoint_dates("test") == (start, end)

    def test_invalid_dates(self, monkeypatch):
        start, end = "2021-12-31", "2021-01-01"

        def _invalid_dates(*args):
            return [start], [end]

        monkeypatch.setattr(self.MOSource, "get_engagement_dates", _invalid_dates)
        with pytest.raises(EngagementDatesError, match=f"{start}.*{end}"):
            self.MOSource.get_engagement_endpoint_dates("test")

        def _empty_dates(*args):
            return [], []

        monkeypatch.setattr(self.MOSource, "get_engagement_dates", _empty_dates)
        with pytest.raises(EngagementDatesError, match="9999-12-31.*1930-01-01"):
            self.MOSource.get_engagement_endpoint_dates("test")
