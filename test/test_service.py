import unittest
from unittest.mock import MagicMock, patch

from io import BytesIO

import openpyxl
from itsdangerous import URLSafeTimedSerializer

import birddog.service as service
from birddog.service import app


TEST_EMAIL = "birddog_test_user@example.com"
TEST_NAME = "Birddog Test User"
TEST_PASSWORD = "correct horse battery staple"


class _RuntimeStub:
    def __init__(self, state="running"):
        self.state = state


class _UserStub:
    def __init__(self, name=TEST_NAME, email=TEST_EMAIL):
        self.name = name
        self.email = email
        self._pw = TEST_PASSWORD
        self._preferences = {}

    # auth helpers used by service.Users.login
    def check_password(self, pw):
        return pw == self._pw

    # used by /change_password
    def change_password(self, current, new):
        if current != self._pw:
            return False
        self._pw = new
        return True

    # used by /reset_password/<token>
    def set_password(self, new):
        self._pw = new

    # used by Users.create
    def save(self):
        return None

    # used by / (home) and /preference/<key>
    def get_preference(self, key, default_value=None):
        return self._preferences.get(key, default_value)

    def set_preference(self, key, value):
        self._preferences[key] = value


class TestServiceHelpers(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.secret_key = "test_secret"
        service.serializer = URLSafeTimedSerializer(app.secret_key)

        # Provide sane globals expected by login_required/_get_current_user
        service.runtime = _RuntimeStub(state="running")
        service.users = service.Users()

        # Avoid template dependency in unit tests
        self._render_template_patcher = patch("birddog.service.render_template", lambda *a, **k: "OK")
        self._render_template_patcher.start()

        self.addCleanup(self._render_template_patcher.stop)

    def test_hide_is_stable_and_case_insensitive(self):
        h1 = service._hide("User@Example.com", salt="s")
        h2 = service._hide("user@example.com", salt="s")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 8)

        h3 = service._hide("user@example.com", salt="different_salt")
        self.assertNotEqual(h1, h3)

    def test_extract_oldid(self):
        self.assertEqual(service._extract_oldid("https://x/y?oldid=12345"), 12345)
        self.assertEqual(service._extract_oldid("https://x/y?nope=1"), 0)

    def test_ascii_filename(self):
        # Cyrillic transliteration + sanitization
        out = service.ascii_filename("Р-285/2:20")
        self.assertTrue(out.isascii())
        self.assertNotIn("/", out)
        self.assertNotIn(":", out)

        # Empty becomes default
        self.assertEqual(service.ascii_filename(""), "download")


class TestUsersManager(unittest.TestCase):
    def setUp(self):
        service.runtime = _RuntimeStub(state="running")

    def test_users_lookup_cache_miss_returns_none(self):
        mgr = service.Users(path="users")
        with patch("birddog.service.load_cached_object", side_effect=service.CacheMissError("miss")):
            self.assertIsNone(mgr.lookup(TEST_EMAIL))

    def test_users_lookup_success(self):
        mgr = service.Users(path="users")
        with patch("birddog.service.load_cached_object", return_value={"x": 1}) as lc,              patch("birddog.service.User") as User:
            User.from_dict.return_value = _UserStub()
            u = mgr.lookup(TEST_EMAIL)
            self.assertIsNotNone(u)
            lc.assert_called_once()
            User.from_dict.assert_called_once()

    def test_users_create_existing_returns_none(self):
        mgr = service.Users(path="users")
        with patch.object(mgr, "lookup", return_value=_UserStub()):
            self.assertIsNone(mgr.create(TEST_EMAIL, TEST_NAME, TEST_PASSWORD))

    def test_users_create_new_user_saves(self):
        mgr = service.Users(path="users")
        # Ensure lookup says user does not exist
        with patch.object(mgr, "lookup", return_value=None),              patch("birddog.service.User") as User:
            user_inst = _UserStub()
            User.return_value = user_inst
            mgr.create(TEST_EMAIL, TEST_NAME, TEST_PASSWORD)
            User.assert_called_once()
            # save() should be called once by create()
            # note: save() belongs to user_inst, not the mock class
            # If it wasn't invoked, we'd see pw unchanged etc; use a spy:
        user_inst = _UserStub()
        with patch.object(mgr, "lookup", return_value=None),              patch("birddog.service.User", return_value=user_inst) as User,              patch.object(user_inst, "save", wraps=user_inst.save) as save_spy:
            u = mgr.create(TEST_EMAIL, TEST_NAME, TEST_PASSWORD)
            self.assertIs(u, user_inst)
            save_spy.assert_called_once()

    def test_users_login_success_and_failure(self):
        mgr = service.Users(path="users")
        good_user = _UserStub()
        with patch.object(mgr, "lookup", return_value=good_user):
            self.assertIsNotNone(mgr.login(TEST_EMAIL, TEST_PASSWORD))
            self.assertIsNone(mgr.login(TEST_EMAIL, "wrong"))

        with patch.object(mgr, "lookup", return_value=None):
            self.assertIsNone(mgr.login(TEST_EMAIL, TEST_PASSWORD))


class TestServiceRoutes(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.secret_key = "test_secret"
        service.serializer = URLSafeTimedSerializer(app.secret_key)

        service.runtime = _RuntimeStub(state="running")
        service.users = MagicMock()

        # Avoid templates
        self._render_template_patcher = patch("birddog.service.render_template", lambda *a, **k: "OK")
        self._render_template_patcher.start()
        self.addCleanup(self._render_template_patcher.stop)

        self.client = app.test_client()

    def _login_session(self):
        with self.client.session_transaction() as sess:
            sess["user"] = {"email": TEST_EMAIL, "name": TEST_NAME}

    def test_get_current_user_error_paths(self):
        # No session
        with app.test_request_context("/"):
            user, resp, status = service._get_current_user()
            self.assertIsNone(user)
            self.assertEqual(status, 404)

        # Missing email
        with app.test_request_context("/"):
            from flask import session
            session["user"] = {"name": TEST_NAME}
            user, resp, status = service._get_current_user()
            self.assertIsNone(user)
            self.assertEqual(status, 404)

        # Unknown user
        service.users.lookup.return_value = None
        with app.test_request_context("/"):
            from flask import session
            session["user"] = {"email": TEST_EMAIL}
            user, resp, status = service._get_current_user()
            self.assertIsNone(user)
            self.assertEqual(status, 404)

        # Emergency shutdown
        service.users.lookup.return_value = _UserStub()
        service.runtime.state = "shutdown"
        with app.test_request_context("/"):
            from flask import session
            session["user"] = {"email": TEST_EMAIL}
            user, resp, status = service._get_current_user()
            self.assertIsNone(user)
            self.assertEqual(status, 503)

    def test_login_required_decorator_passes_user(self):
        service.runtime.state = "running"
        service.users.lookup.return_value = _UserStub()

        @service.login_required
        def _f(user, x):
            return {"ok": True, "email": user.email, "x": x}, 200

        with app.test_request_context("/"):
            from flask import session
            session["user"] = {"email": TEST_EMAIL}
            resp, status = _f(123)  # note wrapper signature: user injected
            self.assertEqual(status, 200)
            self.assertEqual(resp["email"], TEST_EMAIL)
            self.assertEqual(resp["x"], 123)

    def test_signup_validation_and_success(self):
        # missing fields
        resp = self.client.post("/signup", json={"email": TEST_EMAIL})
        self.assertEqual(resp.status_code, 400)

        # success
        service.users.create.return_value = _UserStub()
        resp = self.client.post("/signup", json={"name": TEST_NAME, "email": TEST_EMAIL, "password": TEST_PASSWORD})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True})
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["user"]["email"], TEST_EMAIL)

        # email exists
        service.users.create.return_value = None
        resp = self.client.post("/signup", json={"name": TEST_NAME, "email": TEST_EMAIL, "password": TEST_PASSWORD})
        self.assertEqual(resp.status_code, 400)

    def test_login_and_logout(self):
        # missing fields
        resp = self.client.post("/login", json={"email": TEST_EMAIL})
        self.assertEqual(resp.status_code, 400)

        # invalid
        service.users.login.return_value = None
        resp = self.client.post("/login", json={"email": TEST_EMAIL, "password": "wrong"})
        self.assertEqual(resp.status_code, 401)

        # valid
        service.users.login.return_value = _UserStub()
        resp = self.client.post("/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
        self.assertEqual(resp.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["user"]["email"], TEST_EMAIL)

        # logout redirects and clears session
        resp = self.client.get("/logout")
        self.assertEqual(resp.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertNotIn("user", sess)

    def test_change_password(self):
        self._login_session()
        user = _UserStub()
        service.users.lookup.return_value = user

        # missing current/new
        resp = self.client.post("/change_password", json={"current": "x"})
        self.assertEqual(resp.status_code, 400)

        # wrong current
        resp = self.client.post("/change_password", json={"current": "wrong", "new": "newpw"})
        self.assertEqual(resp.status_code, 403)

        # success
        resp = self.client.post("/change_password", json={"current": TEST_PASSWORD, "new": "newpw"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["success"], True)

    def test_set_user_preference(self):
        self._login_session()
        user = _UserStub()
        service.users.lookup.return_value = user

        # missing 'value'
        resp = self.client.post("/preference/hide_insignificant", json={})
        self.assertEqual(resp.status_code, 400)

        # success
        resp = self.client.post("/preference/hide_insignificant", json={"value": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["success"], True)
        self.assertTrue(user.get_preference("hide_insignificant"))

        # overwrite
        resp = self.client.post("/preference/hide_insignificant", json={"value": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(user.get_preference("hide_insignificant"))

    def test_set_user_preference_requires_login(self):
        resp = self.client.post("/preference/hide_insignificant", json={"value": True})
        self.assertEqual(resp.status_code, 404)

    def test_reset_password_request_unknown_user_does_not_send(self):
        service.users.lookup.return_value = None

        with patch("birddog.service.smtplib.SMTP") as SMTP:
            resp = self.client.post("/reset_password", json={"email": TEST_EMAIL})
            self.assertEqual(resp.status_code, 200)
            # Should not attempt SMTP for unknown user
            SMTP.assert_not_called()

    def test_reset_password_request_known_user_sends_email(self):
        service.users.lookup.return_value = _UserStub()
        service.serializer = URLSafeTimedSerializer("test_secret")

        smtp_cm = MagicMock()
        smtp_instance = MagicMock()
        smtp_cm.__enter__.return_value = smtp_instance

        with patch("birddog.service.smtplib.SMTP", return_value=smtp_cm) as SMTP:
            resp = self.client.post("/reset_password", json={"email": TEST_EMAIL})
            self.assertEqual(resp.status_code, 200)
            SMTP.assert_called_once()
            smtp_instance.starttls.assert_called_once()
            smtp_instance.login.assert_called_once()
            smtp_instance.send_message.assert_called_once()

    def test_reset_with_token_expired_or_unknown_user(self):
        # expired token: serializer.loads raises
        with patch.object(service.serializer, "loads", side_effect=Exception("bad")):
            resp = self.client.get("/reset_password/badtoken")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.decode(), "OK")

        # unknown user
        with patch.object(service.serializer, "loads", return_value=TEST_EMAIL):
            service.users.lookup.return_value = None
            resp = self.client.get("/reset_password/goodtoken")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.decode(), "OK")

    def test_reset_with_token_post_sets_password(self):
        user = _UserStub()
        with patch.object(service.serializer, "loads", return_value=TEST_EMAIL):
            service.users.lookup.return_value = user

            # missing password -> form with error
            resp = self.client.post("/reset_password/goodtoken", data={})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data.decode(), "OK")

            # valid password -> redirect
            resp = self.client.post("/reset_password/goodtoken", data={"password": "newpw"})
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(user._pw, "newpw")


class TestWatchlistAndResolveRoutes(unittest.TestCase):
    """Title-based /watchlist, /resolve, /archives routes (Stage 4)."""

    def setUp(self):
        app.config["TESTING"] = True
        app.secret_key = "test_secret"
        service.serializer = URLSafeTimedSerializer(app.secret_key)

        service.runtime = _RuntimeStub(state="running")
        service.users = MagicMock()

        self._render_template_patcher = patch("birddog.service.render_template", lambda *a, **k: "OK")
        self._render_template_patcher.start()
        self.addCleanup(self._render_template_patcher.stop)

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"email": TEST_EMAIL, "name": TEST_NAME}

    def _mock_user(self):
        user = MagicMock()
        user.email = TEST_EMAIL
        service.users.lookup.return_value = user
        return user

    def test_get_watchlist_formats_title_and_label(self):
        user = self._mock_user()
        user.get_watchlist.return_value = {
            "Архів:ДАЛО": {"cutoff_date": "2020-01-01T00:00:00Z", "last_checked_date": "2020-02-01T00:00:00Z"},
        }
        resp = self.client.get("/watchlist")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Архів:ДАЛО")
        self.assertEqual(data[0]["cutoff_date"], "2020-01-01T00:00:00Z")
        self.assertIn("label", data[0])

    def test_post_watchlist_calls_add_to_watchlist_with_title(self):
        user = self._mock_user()
        user.get_watchlist.return_value = {}
        resp = self.client.post("/watchlist", json={"title": "Архів:ДАЛО", "cutoff_date": "2020-01-01T00:00:00Z"})
        self.assertEqual(resp.status_code, 201)
        user.add_to_watchlist.assert_called_once_with("Архів:ДАЛО", "2020-01-01T00:00:00Z")

    def test_delete_watchlist_success_and_not_found(self):
        user = self._mock_user()
        user.remove_from_watchlist.return_value = True
        resp = self.client.delete("/watchlist/Архів:ДАЛО")
        self.assertEqual(resp.status_code, 204)
        user.remove_from_watchlist.assert_called_once_with("Архів:ДАЛО")

        user.remove_from_watchlist.return_value = False
        resp2 = self.client.delete("/watchlist/Архів:ДАЛО")
        self.assertEqual(resp2.status_code, 404)

    def test_check_watchlist_item_success_and_missing(self):
        user = self._mock_user()
        user.check_watchlist_item.return_value = [{"name": "x"}]
        user.get_watchlist.return_value = {}
        resp = self.client.get("/watchlist/Архів:ДАЛО/check?tree")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        user.check_watchlist_item.assert_called_once_with("Архів:ДАЛО", tree=True)

        user.check_watchlist_item.side_effect = KeyError("Архів:ДАЛО")
        resp2 = self.client.get("/watchlist/Архів:ДАЛО/check")
        self.assertEqual(resp2.status_code, 404)

    def test_resolve_requires_title(self):
        self._mock_user()
        resp = self.client.get("/resolve")
        self.assertEqual(resp.status_code, 400)

    def test_resolve_defaults_item_to_title_when_not_given(self):
        user = self._mock_user()
        user.resolve_item.return_value = []
        resp = self.client.get("/resolve?title=Архів:ДАЛО")
        self.assertEqual(resp.status_code, 200)
        user.resolve_item.assert_called_once_with("Архів:ДАЛО", "Архів:ДАЛО", tree=False, deep=False)

    def test_resolve_with_explicit_item_tree_and_deep(self):
        user = self._mock_user()
        user.resolve_item.return_value = {}
        resp = self.client.get("/resolve?title=Архів:ДАЛО&item=Архів:ДАЛО/104&tree=1&deep=1")
        self.assertEqual(resp.status_code, 200)
        user.resolve_item.assert_called_once_with("Архів:ДАЛО", "Архів:ДАЛО/104", tree=True, deep=True)

    def test_resolve_error_paths(self):
        user = self._mock_user()

        user.resolve_item.side_effect = KeyError("x")
        resp = self.client.get("/resolve?title=Архів:ДАЛО")
        self.assertEqual(resp.status_code, 404)

        user.resolve_item.side_effect = FileNotFoundError()
        resp2 = self.client.get("/resolve?title=Архів:ДАЛО")
        self.assertEqual(resp2.status_code, 404)

        user.resolve_item.side_effect = RuntimeError("boom")
        resp3 = self.client.get("/resolve?title=Архів:ДАЛО")
        self.assertEqual(resp3.status_code, 500)

    def test_resolve_batch_requires_items(self):
        self._mock_user()
        resp = self.client.post("/resolve/batch", json={})
        self.assertEqual(resp.status_code, 400)

        resp2 = self.client.post("/resolve/batch", json={"items": []})
        self.assertEqual(resp2.status_code, 400)

    def test_resolve_batch_requires_title_and_item_per_entry(self):
        self._mock_user()
        resp = self.client.post("/resolve/batch", json={"items": [{"title": "Архів:ДАЛО"}]})
        self.assertEqual(resp.status_code, 400)

    def test_resolve_batch_groups_by_title_and_merges_results(self):
        user = self._mock_user()
        user.resolve_many.side_effect = lambda title, items, tree=False: {"remaining": title}

        resp = self.client.post("/resolve/batch", json={"items": [
            {"title": "Архів:ДАЛО", "item": "Архів:ДАЛО/1"},
            {"title": "Архів:ДАЛО", "item": "Архів:ДАЛО/2"},
            {"title": "Архів:ДАЖО", "item": "Архів:ДАЖО/1"},
        ]})

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["unresolved"], {
            "Архів:ДАЛО": {"remaining": "Архів:ДАЛО"},
            "Архів:ДАЖО": {"remaining": "Архів:ДАЖО"},
        })

        self.assertEqual(user.resolve_many.call_count, 2)
        user.resolve_many.assert_any_call("Архів:ДАЛО", ["Архів:ДАЛО/1", "Архів:ДАЛО/2"], tree=False)
        user.resolve_many.assert_any_call("Архів:ДАЖО", ["Архів:ДАЖО/1"], tree=False)

    def test_resolve_batch_passes_tree_flag(self):
        user = self._mock_user()
        user.resolve_many.return_value = []
        self.client.post("/resolve/batch?tree=1", json={"items": [{"title": "Архів:ДАЛО", "item": "Архів:ДАЛО/1"}]})
        user.resolve_many.assert_called_once_with("Архів:ДАЛО", ["Архів:ДАЛО/1"], tree=True)

    def test_resolve_batch_error_paths(self):
        user = self._mock_user()
        items = {"items": [{"title": "Архів:ДАЛО", "item": "Архів:ДАЛО/1"}]}

        user.resolve_many.side_effect = KeyError("x")
        resp = self.client.post("/resolve/batch", json=items)
        self.assertEqual(resp.status_code, 404)

        user.resolve_many.side_effect = FileNotFoundError()
        resp2 = self.client.post("/resolve/batch", json=items)
        self.assertEqual(resp2.status_code, 404)

        user.resolve_many.side_effect = RuntimeError("boom")
        resp3 = self.client.post("/resolve/batch", json=items)
        self.assertEqual(resp3.status_code, 500)

    def test_archives_route_serves_dynamic_registry(self):
        self._mock_user()
        with patch("birddog.service.all_archive_roots", return_value=[{"title": "Архів:ДАЛО", "label": "DALO"}]):
            resp = self.client.get("/archives")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [{"title": "Архів:ДАЛО", "label": "DALO"}])

    def test_archives_post_updates_matching_title(self):
        user = self._mock_user()
        user.role = 'admin'
        with patch("birddog.service.archive_root_label", return_value="DALO") as mock_label, \
             patch("birddog.service.update_archive_root") as mock_update, \
             patch("birddog.service.all_archive_roots", return_value=[]):
            resp = self.client.post("/archives", json={"title": "Архів:ДАЛО", "label": "DALO", "description": "Lviv"})
        self.assertEqual(resp.status_code, 200)
        mock_label.assert_called_once_with("Архів:ДАЛО")
        mock_update.assert_called_once_with("Архів:ДАЛО", label="DALO", description="Lviv")

    def test_archives_post_accepts_array_payload(self):
        user = self._mock_user()
        user.role = 'admin'
        with patch("birddog.service.archive_root_label", return_value="DALO"), \
             patch("birddog.service.update_archive_root") as mock_update, \
             patch("birddog.service.all_archive_roots", return_value=[]):
            resp = self.client.post("/archives", json=[
                {"title": "Архів:ДАЛО", "label": "DALO", "description": "Lviv"},
                {"title": "Архів:ДАЖО", "label": "DAZHO", "description": "Zhytomyr"},
            ])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_update.call_count, 2)

    def test_archives_post_rejects_unknown_title(self):
        user = self._mock_user()
        user.role = 'admin'
        with patch("birddog.service.archive_root_label", return_value=None), \
             patch("birddog.service.update_archive_root") as mock_update:
            resp = self.client.post("/archives", json={"title": "Архів:НеІснує", "label": "X", "description": "Y"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Архів:НеІснує", resp.get_json()["error"])
        mock_update.assert_not_called()

    def test_archives_post_forbidden_for_non_admin(self):
        user = self._mock_user()
        user.role = 'user'
        with patch("birddog.service.update_archive_root") as mock_update:
            resp = self.client.post("/archives", json={"title": "Архів:ДАЛО", "label": "X", "description": "Y"})
        self.assertEqual(resp.status_code, 403)
        mock_update.assert_not_called()


class TestPageDataNoEarlierVersion(unittest.TestCase):
    """
    /page?compare= 's 'no_earlier_version' flag (issue #138 UI investigation):
    true whenever a comparison was requested but Page.compare() found nothing
    to compare against (the page's entire history postdates the requested
    date) -- lets the client show "NEW PAGE" even when history.length > 1.
    """
    TITLE = "Архів:ДАЖО/Д/999"

    def setUp(self):
        app.config["TESTING"] = True
        app.secret_key = "test_secret"
        service.serializer = URLSafeTimedSerializer(app.secret_key)
        service.runtime = MagicMock(state="running")
        service.users = MagicMock()
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"email": TEST_EMAIL, "name": TEST_NAME}
        user = MagicMock()
        user.role = "user"
        service.users.lookup.return_value = user

    def _fake_page(self, page_dict):
        page = MagicMock()
        page.page = page_dict
        page.title = self.TITLE
        page.kind = "case"
        page.name = "Д/999"
        page.needs_translation = False
        page.history.return_value = []
        return page

    def test_flagged_true_when_compare_finds_nothing(self):
        head = {"title": {"uk": "T"}, "lastmod": "2026-08-26T08:39:44Z"}
        compared = dict(head)  # no 'refmod' -- revert_to() found nothing
        page = self._fake_page(head)
        page.compare.return_value = self._fake_page(compared)
        service.runtime.lookup_by_title.return_value = page

        resp = self.client.get(f"/page?title={self.TITLE}&compare=2026-08-01T00:00:00Z")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["no_earlier_version"])

    def test_flagged_false_when_compare_succeeds(self):
        head = {"title": {"uk": "T"}, "lastmod": "2026-08-26T08:39:44Z"}
        compared = {**head, "refmod": "2026-01-01T00:00:00Z"}
        page = self._fake_page(head)
        page.compare.return_value = self._fake_page(compared)
        service.runtime.lookup_by_title.return_value = page

        resp = self.client.get(f"/page?title={self.TITLE}&compare=2026-01-01T00:00:00Z")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["no_earlier_version"])

    def test_flagged_false_when_no_compare_requested(self):
        head = {"title": {"uk": "T"}, "lastmod": "2026-08-26T08:39:44Z"}
        page = self._fake_page(head)
        service.runtime.lookup_by_title.return_value = page

        resp = self.client.get(f"/page?title={self.TITLE}")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["no_earlier_version"])
        page.compare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
