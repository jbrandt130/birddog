import unittest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager
import threading
import time

import requests as _real_requests  # for exception classes

import birddog.fetch as reqmod
from birddog.fetch import fetch_url


# ------------------ TEST HELPERS ------------------

class ResponseStub:
    def __init__(self, status_code=200, ok=True, text="ok", headers=None, json_obj=None, content=b"bin"):
        self.status_code = status_code
        self.ok = ok
        self.text = text
        self.headers = headers or {}
        self._json_obj = json_obj
        self.content = content

    def json(self):
        if isinstance(self._json_obj, Exception):
            raise self._json_obj
        return self._json_obj


class FakeThrottle:
    """
    Lightweight throttle double for fetch_url tests.

    - acquire() is a no-op context manager, but records calls
    - report_http/exception record outcomes for assertions
    """
    def __init__(self):
        self.acquire_calls = []
        self.http_reports = []
        self.exc_reports = []

    @contextmanager
    def acquire(self, url_or_key: str):
        self.acquire_calls.append(url_or_key)
        yield

    def report_http(self, url_or_key, status_code, headers=None, *, is_rate_limited_payload=False):
        self.http_reports.append((url_or_key, status_code, dict(headers or {}), bool(is_rate_limited_payload)))

    def report_exception(self, url_or_key):
        self.exc_reports.append(url_or_key)

    def format_report(self):
        return ""


# ------------------ FETCH_URL TESTS ------------------

class TestFetchUrl(unittest.TestCase):
    def setUp(self):
        # Keep tests deterministic & fast
        self.fake_throttle = FakeThrottle()
        self.sleep_calls = []

        self.patches = [
            patch.object(reqmod, "THROTTLE", self.fake_throttle),
            patch.object(reqmod.time, "sleep", side_effect=lambda s: self.sleep_calls.append(s)),
            patch.object(reqmod.random, "uniform", side_effect=lambda a, b: (a + b) / 2.0),  # deterministic
            patch.object(reqmod, "_logger", MagicMock()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()


    def test_fetch_url_success_text_get(self):
        resp = ResponseStub(status_code=200, ok=True, text="hello", headers={"Content-Type": "text/plain"})
        with patch.object(reqmod.requests, "get", return_value=resp) as mget:
            out = fetch_url("https://uk.wikisource.org/w/api.php", params={"a": "b"})
            self.assertEqual(out, "hello")
            mget.assert_called_once()
            self.assertEqual(len(self.fake_throttle.acquire_calls), 1)
            self.assertEqual(len(self.fake_throttle.http_reports), 1)
            self.assertEqual(self.fake_throttle.http_reports[0][1], 200)

    def test_fetch_url_success_json(self):
        resp = ResponseStub(
            status_code=200,
            ok=True,
            headers={"Content-Type": "application/json"},
            json_obj={"ok": True},
        )
        with patch.object(reqmod.requests, "get", return_value=resp):
            out = fetch_url("https://commons.wikimedia.org/w/api.php", return_json=True)
            self.assertEqual(out, {"ok": True})


    def test_fetch_url_success_content(self):
        resp = ResponseStub(status_code=200, ok=True, content=b"XYZ", headers={"Content-Type": "application/octet-stream"})
        with patch.object(reqmod.requests, "get", return_value=resp):
            out = fetch_url("https://example.com/file.bin", content=True)
            self.assertEqual(out, b"XYZ")


    def test_fetch_url_post_json(self):
        resp = ResponseStub(status_code=200, ok=True, text="ok")
        with patch.object(reqmod.requests, "post", return_value=resp) as mpost:
            out = fetch_url(
                "https://nocodb.example.elasticbeanstalk.com/api/v2/tables",
                method="POST",
                send_json={"x": 1},
                params={"q": "1"},
            )
            self.assertEqual(out, "ok")
            mpost.assert_called_once()
            _, kwargs = mpost.call_args
            self.assertEqual(kwargs["json"], {"x": 1})
            self.assertEqual(kwargs["params"], {"q": "1"})

    def test_fetch_url_404_raises_runtime_error_with_body(self):
        resp = ResponseStub(status_code=404, ok=False, text="not found")
        with patch.object(reqmod.requests, "get", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_url("https://uk.wikisource.org/wiki/Nope")
            self.assertIn("404", str(ctx.exception))
            self.assertIn("not found", str(ctx.exception))

    def test_fetch_url_nontransient_400_raises_http_error(self):
        resp = ResponseStub(status_code=400, ok=False, text="bad request")
        with patch.object(reqmod.requests, "get", return_value=resp):
            with self.assertRaises(_real_requests.exceptions.HTTPError):
                fetch_url("https://commons.wikimedia.org/w/api.php")

    def test_fetch_url_transient_500_retries_then_succeeds(self):
        resp1 = ResponseStub(status_code=500, ok=False, text="server error")
        resp2 = ResponseStub(status_code=200, ok=True, text="ok")
        with patch.object(reqmod.requests, "get", side_effect=[resp1, resp2]) as mget:
            out = fetch_url("https://translate.googleapis.com/v3/projects/x:translateText", max_retries=3)
            self.assertEqual(out, "ok")
            self.assertEqual(mget.call_count, 2)
            self.assertGreaterEqual(len(self.sleep_calls), 1)
            self.assertEqual(len(self.fake_throttle.acquire_calls), 2)

    def test_fetch_url_timeout_retries_then_fails(self):
        with patch.object(reqmod.requests, "get", side_effect=_real_requests.exceptions.Timeout("t")) as mget:
            with self.assertRaises(reqmod.FetchUrlFailError):
                fetch_url("https://uk.wikisource.org/w/api.php", max_retries=3, base_backoff=0.1, max_backoff=0.2)
            self.assertEqual(mget.call_count, 3)
            self.assertEqual(len(self.fake_throttle.exc_reports), 3)

    def test_fetch_url_429_retries_then_fails_with_fetch_url_fail(self):
        # On repeated 429, fetch_url will retry up to max_retries then raise FetchUrlFailError.
        resp = ResponseStub(status_code=429, ok=False, text="rate limited", headers={"Retry-After": "1"})
        with patch.object(reqmod.requests, "get", return_value=resp) as mget:
            with self.assertRaises(reqmod.FetchUrlFailError):
                fetch_url("https://commons.wikimedia.org/w/api.php", max_retries=3, base_backoff=10.0)
            self.assertEqual(mget.call_count, 3)
            self.assertEqual(len(self.fake_throttle.http_reports), 3)
            # Ensure we did NOT sleep exponential backoff (base_backoff=10 would show huge values).
            self.assertTrue(all(0.04 <= s <= 0.20 for s in self.sleep_calls), msg=f"sleep_calls={self.sleep_calls}")

    def test_fetch_url_mediawiki_ratelimited_payload_marks_throttle(self):
        resp = ResponseStub(
            status_code=200,
            ok=True,
            headers={"Content-Type": "application/json"},
            json_obj={"error": {"code": "ratelimited", "info": "slow down"}},
            text='{"error":{"code":"ratelimited"}}',
        )
        with patch.object(reqmod.requests, "get", return_value=resp):
            with self.assertRaises(reqmod.FetchUrlFailError):
                fetch_url("https://uk.wikisource.org/w/api.php", max_retries=1)
            self.assertEqual(len(self.fake_throttle.http_reports), 1)
            _, status, _, is_rl_payload = self.fake_throttle.http_reports[0]
            self.assertEqual(status, 200)
            self.assertTrue(is_rl_payload)



    def test_fetch_url_mediawiki_maxlag_payload_detected(self):
        resp = ResponseStub(
            status_code=200,
            ok=True,
            headers={"Content-Type": "application/json"},
            json_obj={"error": {"code": "maxlag", "info": "lagging"}},
            text='{"error":{"code":"maxlag"}}',
        )
        with patch.object(reqmod.requests, "get", return_value=resp):
            with self.assertRaises(reqmod.FetchUrlFailError):
                fetch_url("https://commons.wikimedia.org/w/api.php", max_retries=1)
            self.assertEqual(len(self.fake_throttle.http_reports), 1)

# ------------------ ADAPTIVE THROTTLE TESTS ------------------

class TestAdaptiveThrottle(unittest.TestCase):
    def setUp(self):
        self.t = 1000.0
        self.sleep_calls = []

        def sleep_and_advance(s):
            self.sleep_calls.append(float(s))
            self.t += float(s)

        # Use string-based patching so we always patch the symbols as referenced
        # inside birddog.fetch (avoids "patched the wrong sleep" hangs).
        self.patches = [
            patch("birddog.fetch._now", side_effect=lambda: self.t),
            patch("birddog.fetch.time.sleep", side_effect=sleep_and_advance),
            patch("birddog.fetch.random.uniform", side_effect=lambda a, b: (a + b) / 2.0),
        ]
        for p in self.patches:
            p.start()

        self.logger = MagicMock()

        self.profile = reqmod.HostProfile(
            initial_rps=1.0, min_rps=0.1, max_rps=10.0, bucket_capacity=1.0,
            ai_step=0.5, increase_every_s=10.0, md_429=0.5, md_transient=0.9,
            transient_cooldown_s=(1.0, 1.0),
            burst_error_window_s=30.0, burst_error_threshold=3,
            burst_md=0.7, burst_cooldown_s=(2.0, 2.0),
            max_in_flight=2,
        )

        self.th = reqmod.AdaptiveThrottle(
            profiles={"example.com:api": self.profile},
            default_profile=self.profile,
            resolver=reqmod.HostKeyResolver(),
            logger=self.logger,
            sleep_jitter=0.0,
        )

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_unknown_host_warns_once_uses_default_profile(self):
        with self.th.acquire("https://unknown.example/api"):
            pass

        # Second fix: advance fake time between acquires so the next acquire
        # has tokens regardless of whether sleep is exercised.
        self.t += 10.0

        with self.th.acquire("https://unknown.example/api"):
            pass

        self.assertTrue(self.logger.warning.called)
        self.assertEqual(self.logger.warning.call_count, 1)

    def test_token_bucket_sleeps_when_empty(self):
        with self.th.acquire("https://example.com/x"):
            pass

        def sleep_and_advance(s):
            self.sleep_calls.append(s)
            self.t += s

        with patch.object(reqmod.time, "sleep", side_effect=sleep_and_advance):
            with self.th.acquire("https://example.com/x"):
                pass

        self.assertGreaterEqual(len(self.sleep_calls), 1)
        self.assertAlmostEqual(self.sleep_calls[0], 1.0, places=3)

    def test_report_429_decreases_rate_and_blocks(self):
        hk = "example.com:api"
        st = self.th._get_state(hk)
        self.assertAlmostEqual(st.rate_rps, 1.0, places=6)

        self.th.report_http("https://example.com/x", 429, headers={"Retry-After": "3"})
        st = self.th._get_state(hk)
        self.assertAlmostEqual(st.rate_rps, 0.5, places=6)
        self.assertAlmostEqual(st.blocked_until, self.t + 3.0, places=6)

    def test_transient_sets_cooldown(self):
        hk = "example.com:api"
        self.th.report_http("https://example.com/x", 500, headers={})
        st = self.th._get_state(hk)
        # cooldown is deterministic: transient_cooldown_s=(1,1)
        self.assertAlmostEqual(st.blocked_until, self.t + 1.0, places=6)
        self.assertLess(st.rate_rps, 1.0)

    def test_transient_burst_stronger_backoff(self):
        hk = "example.com:api"
        self.th.report_http("https://example.com/x", 500, headers={})
        self.th.report_http("https://example.com/x", 500, headers={})
        self.th.report_http("https://example.com/x", 500, headers={})
        st = self.th._get_state(hk)
        self.assertLess(st.rate_rps, 0.9)
        self.assertAlmostEqual(st.blocked_until, self.t + 2.0, places=6)

    def test_success_additive_increase_after_interval(self):
        hk = "example.com:api"
        st = self.th._get_state(hk)
        self.assertAlmostEqual(st.rate_rps, 1.0, places=6)

        # Not enough time -> no increase
        self.th.report_http("https://example.com/x", 200, headers={})
        st = self.th._get_state(hk)
        self.assertAlmostEqual(st.rate_rps, 1.0, places=6)

        # Advance time past increase_every_s
        self.t += 10.0
        self.th.report_http("https://example.com/x", 200, headers={})
        st = self.th._get_state(hk)
        self.assertAlmostEqual(st.rate_rps, 1.5, places=6)

    def test_max_in_flight_semaphore_caps_concurrency(self):
        import time
        # Make token bucket non-limiting so we test ONLY max_in_flight.
        sem_profile = reqmod.HostProfile(
            initial_rps=1000.0, min_rps=0.1, max_rps=1000.0, bucket_capacity=10.0,
            ai_step=0.0, increase_every_s=999999.0, md_429=0.5, md_transient=0.9,
            transient_cooldown_s=(0.0, 0.0),
            burst_error_window_s=30.0, burst_error_threshold=3,
            burst_md=0.7, burst_cooldown_s=(0.0, 0.0),
            max_in_flight=2,
        )

        th = reqmod.AdaptiveThrottle(
            profiles={"example.com:api": sem_profile},
            default_profile=sem_profile,
            resolver=reqmod.HostKeyResolver(),
            logger=self.logger,
            sleep_jitter=0.0,
        )

        started = []
        started_lock = threading.Lock()
        proceed = threading.Event()
        release = threading.Event()

        def worker(i):
            with th.acquire("https://example.com/x"):
                with started_lock:
                    started.append(i)
                proceed.wait(timeout=1.0)
                release.wait(timeout=1.0)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t3 = threading.Thread(target=worker, args=(3,))

        t1.start(); t2.start()

        # Wait until two workers have entered the lease
        deadline = time.time() + 1.0
        while True:
            with started_lock:
                n = len(started)
            if n >= 2 or time.time() >= deadline:
                break
            time.sleep(0.001)

        self.assertEqual(sorted(started), [1, 2], msg=f"started={started}")

        t3.start()

        # Give scheduler a moment; third should be blocked by semaphore (max_in_flight=2)
        time.sleep(0.02)
        self.assertEqual(sorted(started), [1, 2], msg=f"started={started}")

        proceed.set()
        release.set()

        t1.join(timeout=1.0)
        t2.join(timeout=1.0)
        t3.join(timeout=1.0)

        with started_lock:
            self.assertEqual(sorted(started), [1, 2, 3], msg=f"started={started}")

if __name__ == "__main__":
    unittest.main()
