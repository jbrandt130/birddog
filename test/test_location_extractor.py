import json
import unittest
from unittest.mock import MagicMock, patch

import requests

from birddog.fetch import RateLimitedError
from birddog.locations.extractor import LocationExtractor


def completion(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def locations_reply(*per_doc):
    return completion(json.dumps({"extracted_locations": [
        {"document_index": i, "locations": locs} for i, locs in per_doc
    ]}))


class TestExtractLocationsBatched(unittest.TestCase):
    def setUp(self):
        self.extractor = LocationExtractor(MagicMock(), provider="groq")

    def run_with(self, replies, batches):
        with patch.object(self.extractor, "_create_completion", side_effect=replies) as call:
            return self.extractor.extract_locations_batched(batches), call

    def test_found_and_genuinely_empty(self):
        out, _ = self.run_with([locations_reply((0, ["Kyiv"]), (1, []))], [["in Kyiv"], ["chapter XXV"]])
        self.assertEqual(out, [["Kyiv"], []])

    def test_doc_missing_from_reply_is_none(self):
        out, _ = self.run_with([locations_reply((0, ["Kyiv"]))], [["in Kyiv"], ["somewhere"]])
        self.assertEqual(out, [["Kyiv"], None])

    def test_unparseable_reply_is_none(self):
        out, _ = self.run_with([completion("not json")], [["a"], ["b"]])
        self.assertEqual(out, [None, None])

    def test_empty_content_is_none_but_empty_input_is_empty(self):
        out, _ = self.run_with([completion("")], [["a"], []])
        self.assertEqual(out, [None, []])

    def test_truncated_reply_retried_with_more_tokens(self):
        out, call = self.run_with(
            [completion('{"extracted_locations": [', "length"), locations_reply((0, ["Lviv"]))], [["Lviv"]])
        self.assertEqual(out, [["Lviv"]])
        self.assertGreater(call.call_args_list[1].args[1], call.call_args_list[0].args[1])

    def test_context_overflow_retried_with_fewer_tokens(self):
        overflow = requests.exceptions.HTTPError("HTTP 400: maximum context length is 4096 tokens")
        reply = locations_reply((0, ["Lviv"]), (1, []), (2, []), (3, []))
        out, call = self.run_with([overflow, reply], [["Lviv"], ["a"], ["b"], ["c"]])
        self.assertEqual(out, [["Lviv"], [], [], []])
        self.assertLess(call.call_args_list[1].args[1], call.call_args_list[0].args[1])

    def test_logs_reported_usage(self):
        reply = locations_reply((0, ["Kyiv"]), (1, []))
        reply["usage"] = {"prompt_tokens": 1500, "completion_tokens": 40, "total_tokens": 1540}
        with patch("birddog.locations.extractor.fetch_url", return_value=reply):
            self.extractor.extract_locations_batched([["in Kyiv"], ["chapter XXV"]])
        logged = [c.args[0] for c in self.extractor._logger.info.call_args_list]
        usage = [m for m in logged if m.startswith("location extraction usage")]
        self.assertEqual(len(usage), 1)
        self.assertIn("2 docs, prompt=1500 completion=40/100 total=1540 tokens (770/doc)", usage[0])

    def test_rate_limit_propagates(self):
        with self.assertRaises(RateLimitedError):
            self.run_with([RateLimitedError("TPD", 3600)], [["a"]])

    def test_request_goes_through_fetch_url(self):
        with patch("birddog.locations.extractor.fetch_url", return_value=locations_reply((0, []))) as fetch:
            self.extractor.extract_locations_batched([["a"]])
        url = fetch.call_args.args[0]
        kwargs = fetch.call_args.kwargs
        self.assertEqual(url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["send_json"]["response_format"], {"type": "json_object"})
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))
        self.assertIsNotNone(kwargs["max_retry_after"])


class TestCheckCredentials(unittest.TestCase):
    def setUp(self):
        self.extractor = LocationExtractor(MagicMock(), provider="groq")
        self.extractor._provider_api_key = "test-key"

    def check(self, **fetch):
        with patch("birddog.locations.extractor.fetch_url", **fetch) as call:
            return self.extractor.check_credentials(), call

    def test_accepted_key_and_listed_model(self):
        (ok, message), call = self.check(return_value={"data": [{"id": self.extractor._model}]})
        self.assertTrue(ok)
        self.assertIn("key accepted", message)
        self.assertEqual(call.call_args.args[0], "https://api.groq.com/openai/v1/models")

    def test_model_missing_from_list(self):
        (ok, message), _ = self.check(return_value={"data": [{"id": "other-model"}]})
        self.assertFalse(ok)
        self.assertIn("is not offered", message)

    def test_rejected_key(self):
        response = MagicMock(status_code=401)
        err = requests.exceptions.HTTPError("HTTP 401: Invalid API Key", response=response)
        (ok, message), _ = self.check(side_effect=err)
        self.assertFalse(ok)
        self.assertIn("HTTP 401", message)

    def test_missing_key_makes_no_request(self):
        self.extractor._provider_api_key = ""
        (ok, message), call = self.check()
        self.assertFalse(ok)
        self.assertIn("GROQ_LOCATION_KEY is not set", message)
        call.assert_not_called()

    def test_outage_is_inconclusive(self):
        (ok, message), _ = self.check(side_effect=requests.exceptions.ConnectionError("down"))
        self.assertIsNone(ok)
        self.assertIn("could not verify", message)


class TestResponseCache(unittest.TestCase):
    """A batch retried after a rate limit must reuse the calls it already
    paid for, or it can never finish near the daily token limit."""

    def setUp(self):
        self.extractor = LocationExtractor(MagicMock(), provider="groq")
        self.messages = [{"role": "user", "content": "x"}]

    def test_identical_request_is_served_from_cache(self):
        reply = locations_reply((0, ["Kyiv"]))
        with patch("birddog.locations.extractor.fetch_url", return_value=reply) as fetch:
            first = self.extractor._create_completion(self.messages, 100, 1)
            second = self.extractor._create_completion(self.messages, 100, 1)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first, second)

    def test_retried_batch_only_repeats_the_refused_call(self):
        # two chunks of 10 docs: the first call succeeds, the second is
        # refused; on retry only the second goes to the service
        batches = [[f"doc {i}"] for i in range(20)]
        ok = locations_reply(*[(i, []) for i in range(10)])
        with patch("birddog.locations.extractor.fetch_url",
                   side_effect=[ok, RateLimitedError("TPD", 600)]) as fetch:
            with self.assertRaises(RateLimitedError):
                self.extractor.extract_locations_batched(batches, batch_size=10)
        self.assertEqual(fetch.call_count, 2)
        with patch("birddog.locations.extractor.fetch_url", return_value=ok) as fetch:
            out = self.extractor.extract_locations_batched(batches, batch_size=10)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(out, [[]] * 20)

    def test_different_max_tokens_is_a_different_request(self):
        reply = locations_reply((0, []))
        with patch("birddog.locations.extractor.fetch_url", return_value=reply) as fetch:
            self.extractor._create_completion(self.messages, 100, 1)
            self.extractor._create_completion(self.messages, 200, 1)
        self.assertEqual(fetch.call_count, 2)

    def test_failed_request_is_not_cached(self):
        reply = locations_reply((0, []))
        with patch("birddog.locations.extractor.fetch_url",
                   side_effect=[RateLimitedError("TPD", 600), reply]) as fetch:
            with self.assertRaises(RateLimitedError):
                self.extractor._create_completion(self.messages, 100, 1)
            self.extractor._create_completion(self.messages, 100, 1)
        self.assertEqual(fetch.call_count, 2)

    def test_entries_expire(self):
        reply = locations_reply((0, []))
        with patch("birddog.locations.extractor.fetch_url", return_value=reply) as fetch, \
             patch("birddog.locations.extractor.time.monotonic", side_effect=[0.0, 13 * 3600.0, 13 * 3600.0]):
            # stored at 0h; looked up again at 13h, past the 12h TTL
            self.extractor._create_completion(self.messages, 100, 1)
            self.extractor._create_completion(self.messages, 100, 1)
        self.assertEqual(fetch.call_count, 2)

    def test_cache_is_bounded(self):
        import birddog.locations.extractor as extractor_module
        with patch.object(extractor_module, "_RESPONSE_CACHE_SIZE", 2), \
             patch("birddog.locations.extractor.fetch_url", return_value=locations_reply((0, []))):
            for i in range(3):
                self.extractor._create_completion([{"role": "user", "content": str(i)}], 100, 1)
        self.assertEqual(len(self.extractor._response_cache), 2)


if __name__ == "__main__":
    unittest.main()
