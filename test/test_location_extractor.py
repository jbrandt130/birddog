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


if __name__ == "__main__":
    unittest.main()
