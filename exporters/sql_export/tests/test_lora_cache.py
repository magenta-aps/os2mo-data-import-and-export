from unittest import mock
from unittest import TestCase

from ..lora_cache import LoraCache


class _TestableLoraCache(LoraCache):
    def _load_settings(self):
        return {"mox.base": "bogus://"}

    def _read_org_uuid(self):
        return "not-a-mo-org-uuid"


class _SpecificException(Exception):
    pass


class TestPerformLoraLookup(TestCase):
    def test_retry(self):
        # Test that a failing GET request is retried the number of times set in
        # `@retry(stop_max_attempt_number=7)`
        instance = _TestableLoraCache()
        num_retries = 7
        # Eventually, the exception bubbles up from the retry logic. Catch it
        # here, so we can verify the call count, etc.
        with self.assertRaises(_SpecificException):
            with mock.patch("requests.get", side_effect=_SpecificException()) as _get:
                instance._perform_lora_lookup("not-an-url", {})
            _get.assert_called()
            self.assertEqual(_get.call_count, num_retries)