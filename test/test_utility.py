import time
import unittest
from copy import copy
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from birddog import utility as util
from birddog.utility import (
    lastmod,
    is_numeric,
    is_english,
    form_text_item,
    equal_text,
    get_text,
    match_text,
    is_linked,
    link_status,
    json_size,
    from_utc_format,
    to_utc_format,
    check_ukrainian_hyphen_numbers,
    check_numbers_hyphen_ukrainian,
    find_digits_around_ukrainian,
    translit_ukrainian_char,
    replace_with_translit,
    system_resource_report,
    fetch_url,
)

from birddog.translate import (
    needs_translation,
    translate_structure,
    TranslationDisabledError,
)


class TestUtility(unittest.TestCase):
    def test_lastmod(self):
        # Should extract and normalize a ukrainian month date string
        msg = "blah blah 19:15, 20 травня 2023 blah"
        self.assertEqual(lastmod(msg), "2023,05,20,19:15")

        # No match -> returns original
        self.assertEqual(lastmod("no date here"), "no date here")

    def test_numeric_and_english(self):
        self.assertTrue(is_numeric("123"))
        self.assertTrue(is_numeric("  12-34 "))
        self.assertTrue(is_numeric("12–34"))  # en dash
        self.assertFalse(is_numeric("12A"))
        self.assertFalse(is_numeric("A12"))

        self.assertTrue(is_english("hello"))
        self.assertTrue(is_english("123-45"))
        self.assertFalse(is_english("востаннє"))

    def test_link_helpers(self):
        self.assertEqual(link_status({}), "unlinked")
        self.assertEqual(link_status({"link": "/wiki/Foo"}), "linked")
        self.assertEqual(link_status({"link": "/wiki/Foo", "exists": True}), "exists")

        self.assertFalse(is_linked(None))
        self.assertFalse(is_linked({}))
        self.assertFalse(is_linked({"link": "/wiki/Foo", "exists": False}))
        self.assertTrue(is_linked({"link": "/wiki/Foo", "exists": True}))
        self.assertFalse(is_linked({"link": "/wiki/Foo?redlink=1", "exists": True}))

    def test_json_size_utf8(self):
        obj = {"a": "é", "b": "в"}  # multi-byte in utf-8
        # ensure_ascii=False means we should count UTF-8 bytes, not \uXXXX escapes
        size = json_size(obj)
        self.assertIsInstance(size, int)
        self.assertGreater(size, 0)

        # sanity: adding content should increase size
        size2 = json_size({**obj, "c": "more"})
        self.assertGreater(size2, size)

    def test_utc_formatting(self):
        self.assertEqual(from_utc_format("2025-12-28T01:02:03Z"), "2025,12,28,01:02")

        # to_utc_format pads missing components
        self.assertEqual(to_utc_format("2025"), "2025-01-01T00:00:00Z")
        self.assertEqual(to_utc_format("2025,12"), "2025-12-01T00:00:00Z")
        self.assertEqual(to_utc_format("2025,12,28"), "2025-12-28T00:00:00Z")
        self.assertEqual(to_utc_format("2025,12,28,13:37"), "2025-12-28T13:37:00Z")

    def test_ukrainian_digit_patterns(self):
        self.assertTrue(check_ukrainian_hyphen_numbers("А-12"))
        self.assertTrue(check_ukrainian_hyphen_numbers("А12"))
        self.assertFalse(check_ukrainian_hyphen_numbers("A-12"))
        self.assertFalse(check_ukrainian_hyphen_numbers("12-А"))

        self.assertTrue(check_numbers_hyphen_ukrainian("12-А"))
        self.assertTrue(check_numbers_hyphen_ukrainian("12А"))
        self.assertFalse(check_numbers_hyphen_ukrainian("12-A"))

        self.assertEqual(find_digits_around_ukrainian("12А34"), (2, 1))
        self.assertEqual(find_digits_around_ukrainian("12АБ34"), (2, 2))
        self.assertEqual(find_digits_around_ukrainian("12A34"), (-1, 0))

    def test_translit_and_replace(self):
        self.assertEqual(translit_ukrainian_char("А"), "A")
        self.assertEqual(translit_ukrainian_char("Б"), "B")
        self.assertEqual(translit_ukrainian_char("?"), "?")

        self.assertEqual(replace_with_translit("12А34", 2, 1), "12A34")
        self.assertEqual(replace_with_translit("12АБ34", 2, 2), "12AB34")
        self.assertEqual(replace_with_translit("", 0, 1), "")

    def test_form_text_item_and_helpers(self):
        # numeric -> en mirrors uk
        self.assertEqual(form_text_item("12-34"), {"uk": "12-34", "en": "12-34"})

        # english -> en mirrors uk
        self.assertEqual(form_text_item("page"), {"uk": "page", "en": "page"})

        # UA-12 prefix translit to en
        self.assertEqual(form_text_item("А-12"), {"uk": "А-12", "en": "A-12"})

        # 12-UA suffix translit to en
        self.assertEqual(form_text_item("12-А"), {"uk": "12-А", "en": "12-A"})

        # digits UA digits translit inside
        self.assertEqual(form_text_item("12А34"), {"uk": "12А34", "en": "12A34"})

        # Non-english, non-pattern -> en empty/absent -> currently returns only uk
        item = form_text_item("востаннє")
        self.assertEqual(item["uk"], "востаннє")
        self.assertNotIn("en", item)  # per current implementation

        # equal_text: compares en if present in both, else uk
        self.assertTrue(equal_text({"uk": "x", "en": "y"}, {"uk": "x", "en": "y"}))
        self.assertFalse(equal_text({"uk": "x", "en": "y"}, {"uk": "x", "en": "z"}))
        self.assertTrue(equal_text({"uk": "x"}, {"uk": "x", "en": "zzz"}))
        self.assertFalse(equal_text({"uk": "x"}, {"uk": "y"}))

        # get_text prefers en, else uk, else returns input if not dict
        self.assertEqual(get_text({"uk": "сторінку", "en": "page"}), "page")
        self.assertEqual(get_text({"uk": "сторінку"}), "сторінку")
        self.assertEqual(get_text("raw"), "raw")

        # match_text matches either uk or en
        self.assertTrue(match_text({"uk": "сторінку", "en": "page"}, "page"))
        self.assertTrue(match_text({"uk": "сторінку", "en": "page"}, "сторінку"))
        self.assertFalse(match_text({"uk": "сторінку", "en": "page"}, "other"))

    def test_system_resource_report_mocked(self):
        # Avoid real sleeps and psutil calls: patch psutil + sleep
        vm = SimpleNamespace(total=1000_000_000, available=400_000_000, percent=60.0)
        sm = SimpleNamespace(percent=25.0)
        net1 = SimpleNamespace(bytes_sent=1000, bytes_recv=2000)
        net2 = SimpleNamespace(bytes_sent=3000, bytes_recv=5000)

        with patch.object(util.psutil, "cpu_percent", return_value=12.5),              patch.object(util.psutil, "virtual_memory", return_value=vm),              patch.object(util.psutil, "swap_memory", return_value=sm),              patch.object(util.psutil, "net_io_counters", side_effect=[net1, net2]),              patch.object(util.time, "sleep", return_value=None),              patch.object(util.psutil, "getloadavg", side_effect=OSError("no loadavg")):
            r = system_resource_report(interval=1.0)

        self.assertIn("cpu_percent", r)
        self.assertEqual(r["cpu_percent"], 12.5)
        self.assertIn("memory", r)
        #self.assertAlmostEqual(r["memory"]["pressure_index"], 60.0 + 25.0, places=1)
        self.assertIn("network", r)
        # (3000-1000)/1/1024 ~= 1.953...
        self.assertGreater(r["network"]["tx_kbps"], 0)
        self.assertGreater(r["network"]["rx_kbps"], 0)

    def test_fetch_url_success_get_and_post(self):
        # Basic GET success path
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = "ok"
        resp.json.return_value = {"ok": True}
        resp.content = b"bin"

        with patch.object(util.requests, "get", return_value=resp) as mget,              patch.object(util, "_record_fetch_event") as mrec:
            self.assertEqual(fetch_url("http://example.com"), "ok")
            self.assertEqual(fetch_url("http://example.com", json=True), {"ok": True})
            self.assertEqual(fetch_url("http://example.com", content=True), b"bin")
            mget.assert_called()
            self.assertTrue(mrec.called)

        # Basic POST success path
        with patch.object(util.requests, "post", return_value=resp) as mpost,              patch.object(util, "_record_fetch_event"):
            self.assertEqual(fetch_url("http://example.com", method="POST"), "ok")
            mpost.assert_called()

    def test_fetch_url_retries_and_failures(self):
        # Force deterministic backoff: no jitter, no real sleep
        with patch.object(util.random, "uniform", return_value=0.0),              patch.object(util.time, "sleep", return_value=None):

            # Retry twice then succeed
            resp_ok = MagicMock(status_code=200, ok=True, text="ok")
            side = [util.requests.RequestException("boom"),
                    util.requests.RequestException("boom2"),
                    resp_ok]

            with patch.object(util.requests, "get", side_effect=side),                  patch.object(util, "_record_fetch_event"):
                self.assertEqual(fetch_url("http://example.com"), "ok")

            # 404 is raised as RuntimeError (not retried)
            resp_404 = MagicMock(status_code=404, ok=False)
            with patch.object(util.requests, "get", return_value=resp_404):
                with self.assertRaises(RuntimeError):
                    fetch_url("http://example.com/notfound")

            # 429 triggers retries and eventually FetchUrlFailError
            resp_429 = MagicMock(status_code=429, ok=False)
            with patch.object(util.requests, "get", return_value=resp_429),                  patch.object(util, "MAX_RETRIES", 2):
                with self.assertRaises(util.FetchUrlFailError):
                    fetch_url("http://example.com/ratelimited")

    def test_heartbeat_manager(self):
        # Subclass to count heartbeats
        class CounterHB(util.HeartbeatManager):
            def __init__(self, interval=0.01):
                super().__init__(interval=interval)
                self.count = 0
            def heartbeat(self):
                self.count += 1

        hb = CounterHB(interval=0.01)
        hb.start()
        time.sleep(0.05)
        c1 = hb.count
        self.assertGreater(c1, 0)

        hb.hold()
        time.sleep(0.05)
        c2 = hb.count
        # should not increase while held (allow 1 tick race)
        self.assertLessEqual(c2, c1 + 1)

        hb.release()
        time.sleep(0.05)
        c3 = hb.count
        self.assertGreater(c3, c2)

        hb.stop()
        # stop is idempotent
        hb.stop()


class TestTranslateStructure(unittest.TestCase):
    def test_translate_structure(self):
        item1 = {
            'abc': [1, 2],
            'def': [form_text_item("востаннє"), 5],
            'ghi': {'abc': form_text_item("сторінку")}
        }
        item2 = copy(item1)
        item2_translated = {
            'abc': [1, 2],
            'def': [{'uk': 'востаннє', 'en': 'last'}, 5],
            'ghi': {'abc': {'uk': 'сторінку', 'en': 'page'}}
        }
        try:
            translate_structure(item2)
            self.assertTrue(item2 == item2_translated)
        except TranslationDisabledError:
            # Acceptable for CI environments that disable translation.
            pass

    def test_needs_translation(self):
        self.assertFalse(needs_translation({'uk': 'page', 'en': 'page'}))
        self.assertTrue(needs_translation({'uk': 'сторінку'}))
        self.assertFalse(needs_translation({'uk': 'сторінку', 'en': 'page'}))


if __name__ == "__main__":
    unittest.main()
