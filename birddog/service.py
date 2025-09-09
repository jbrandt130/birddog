# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

# system packages
import os
import threading
import re
import time
import hashlib
from io import BytesIO
from copy import copy, deepcopy
from datetime import datetime, timedelta, UTC
from collections import defaultdict
from email.message import EmailMessage
import smtplib
from unidecode import unidecode

from cachetools import LRUCache
from itsdangerous import URLSafeTimedSerializer
from werkzeug.security import generate_password_hash, check_password_hash

from flask import (
    Flask,
    render_template,
    request,
    send_file,
    redirect,
    url_for,
    session,
    jsonify,
    g)

# Birddog packages
from birddog.runtime import Runtime, PageLRU, ArchiveWatcher
from birddog.excel import export_page, list_templates
from birddog.cache import (
    load_cached_object,
    save_cached_object,
    remove_cached_object,
    CacheMissError)
from birddog.wiki import (
    check_page_changes, 
    all_archives, 
    page_address,
    lineage
    )
from birddog.ai import list_column_classes, classify_table_columns
from birddog.utility import get_text
from birddog.logging import (
    get_logger,
    get_log_buffer,
    detect_environment,
    EventLogger,
    ServiceLogger,
    summarize_duration_by_path_group,
    user_histogram,
    )

# ---- INITIALIZATION  --------------------------------------------------

_logger = get_logger()
_event_logger = EventLogger.get_logger()
#get_event_logger()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = os.getenv('BIRDDOG_SECRET_KEY', '')  # For session management
serializer = URLSafeTimedSerializer(app.secret_key)

SMTP_SERVER = os.getenv('BIRDDOG_SMTP_SERVER', '')  # For password reset
SMTP_PORT = os.getenv('BIRDDOG_SMTP_PORT', '')  # For password reset
SMTP_USERNAME = os.getenv('BIRDDOG_SMTP_USERNAME', '')  # For password reset
SMTP_PASSWORD = os.getenv('BIRDDOG_SMTP_PASSWORD', '')  # For password reset

# ---- APP GLOBALS  -----------------------------------------------------------

ARCHIVE_MASTER_LIST     = all_archives()
users                   = None
runtime                 = None

# ---- USER MANAGEMENT --------------------------------------------------------

def _hide(email: str, salt: str = app.secret_key) -> str:
    """Returns a short, anonymized hash of an email address."""
    hasher = hashlib.sha256()
    hasher.update((salt + email.lower().strip()).encode("utf-8"))
    return hasher.hexdigest()[:8]

def _watcher_cache_path(email, archive, subarchive):
    return f'watchers/{email}/{archive}-{subarchive}.json'

class User:
    def __init__(self, name, email, password, watchlist=None, preferences=None, is_hashed=False):
        self.name = name
        self.email = email
        self.password_hash = password if is_hashed else generate_password_hash(password)
        self.watchlist = watchlist or {}
        self.preferences = preferences or {}
        self._lock = threading.RLock()

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def change_password(self, current_password, new_password):
        with self._lock:
            if not self.check_password(current_password):
                return False
            self.password_hash = generate_password_hash(new_password)
            self.save()
            return True

    def set_password(self, new_password):
        with self._lock:
            self.password_hash = generate_password_hash(new_password)
            self.save()

    def add_to_watchlist(self, archive, subarchive, cutoff_date):
        key = _watchlist_key(archive, subarchive)
        with self._lock:
            self.watchlist[key] = {
                'last_checked_date': '',
                'cutoff_date': cutoff_date
            }
            self.save()

    def remove_from_watchlist(self, archive, subarchive):
        key = _watchlist_key(archive, subarchive)
        with self._lock:
            if key not in self.watchlist:
                return False
            del self.watchlist[key]
            self.save()

        # Remove associated watcher file (outside lock)
        watcher_path = _watcher_cache_path(self.email, archive, subarchive)
        try:
            remove_cached_object(watcher_path)
        except CacheMissError:
            pass  # it's already gone
        return True

    def check_archive(self, archive, subarchive, tree=False):
        key = _watchlist_key(archive, subarchive)
        with self._lock:
            if key not in self.watchlist:
                raise KeyError(f"Watchlist item not found: {key}")

            path = _watcher_cache_path(self.email, archive, subarchive)
            try:
                watcher_data = load_cached_object(path)
                watcher = ArchiveWatcher.load(watcher_data, runtime=runtime)
            except CacheMissError:
                watcher = ArchiveWatcher(
                    archive, subarchive,
                    self.watchlist[key]['cutoff_date'],
                    runtime=runtime)

            watcher.check()
            save_cached_object(watcher.save(), path)

            self.watchlist[key]['last_checked_date'] = datetime.now().strftime('%Y,%m,%d,%H:%M')
            self.save()

            # Return just the result, not the watcher itself
            if tree:
                return watcher.unresolved_tree
            return [{'name': k, **v} for k, v in watcher.unresolved.items()]

    def resolve_item(self, archive, subarchive, fond=None, opus=None, case=None, tree=False, deep=False):
        key = _watchlist_key(archive, subarchive)

        with self._lock:
            if key not in self.watchlist:
                raise KeyError('Watchlist item not found')

            path = _watcher_cache_path(self.email, archive, subarchive)
            try:
                watcher_data = load_cached_object(path)
            except CacheMissError:
                raise FileNotFoundError('No watcher found')

            watcher = ArchiveWatcher.load(watcher_data)

            resolve_key = ArchiveWatcher.key(archive, subarchive, fond, opus, case)
            _logger.info(f'Resolving {resolve_key}, deep={deep}, tree={tree}')
            watcher.resolve(resolve_key, deep=deep)

            save_cached_object(watcher.save(), path)

            if tree:
                return watcher.unresolved_tree
            else:
                return [{'name': k, **v} for k, v in watcher.unresolved.items()]

    def set_preference(self, key, value):
        self.preferences[key] = value
        self.save()

    def get_preference(self, key, default_value=None):
        return self.preferences.get(key, default_value)

    def save(self):
        with self._lock:
            save_cached_object(self.to_dict(), f'users/{self.email}.json')

    def to_dict(self):
        return {
            'name': self.name,
            'password': self.password_hash,
            'watchlist': self.watchlist,
            'preferences': self.preferences
        }

    @classmethod
    def from_dict(cls, email, d):
        return cls(
            name=d['name'],
            email=email,
            password=d['password'],
            watchlist=d.get('watchlist', {}),
            preferences=d.get('preferences', {}),
            is_hashed=True
        )

class Users:
    def __init__(self, user_session, max_users=10):
        self._path = 'users'
        self._session = user_session
        self._cache = LRUCache(maxsize=max_users)
        self._locks = defaultdict(threading.Lock)

    def _session_user(self, name, email):
        return {'name': name, 'email': email}

    def lookup(self, email):
        with self._locks[email]:
            if email in self._cache:
                return self._cache[email]
            try:
                data = load_cached_object(f'{self._path}/{email}.json')
                user = User.from_dict(email, data)
                self._cache[email] = user
                return user
            except CacheMissError:
                return None

    def create(self, email, name, password):
        if self.lookup(email):
            return False
        _logger.info(f"Storing new user: {name}, {_hide(email)}")
        user = User(name, email, password)
        with self._locks[email]:
            user.save()
            self._cache[email] = user
        self._session['user'] = self._session_user(name, email)
        return True

    def login(self, email, password):
        user = self.lookup(email)
        if user and user.check_password(password):
            self._session['user'] = self._session_user(user.name, email)
            return True
        return False

    def logout(self):
        self._session.pop('user', None)


def _get_current_user():
    user_session = session.get('user')
    if not user_session:
        return None, jsonify({'error': 'Not found'}), 404

    email = user_session.get('email')
    user = users.lookup(email)
    if not user:
        return None, jsonify({'error': 'Not found'}), 404

    if runtime.killed:
        return None, jsonify({
            "error": "Service unavailable",
            "reason": "Birddog runtime emergency shutdown"
        }), 503

    return user, None, None

# ---- FRONT END PAGES --------------------------------------------------------

# Home Route (Shows the landing page)
@app.route('/')
def home():
    user_session = None
    start_title = None
    if not runtime.killed:
        user_session = session.get('user')
        start_title = None
        if user_session:
            user, error_response, status = _get_current_user()
            if user:
                start_title = user.get_preference("last_page")
    return render_template(
        'index.html', 
        user=user_session, 
        start_title=start_title,
        killed=runtime.killed, 
        debug=app.debug)

# ---- SESSION MANAGEMENT -----------------------------------------------------

# Signup Route
@app.route('/signup', methods=['POST'])
def signup():
    data = request.json
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')

    if not users.create(email, name, password):
        return jsonify({'success': False, 'message': 'Email already exists'}), 400
    _logger.info(f"Creating new user: {name} {_hide(email)}")
    return jsonify({'success': True})

# Login Route
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    if users.login(email, password):
        return jsonify({'success': True})
    _logger.info(f"Login failed: {_hide(email)}")
    return jsonify({'success': False, 'message': 'Invalid email or password'}), 401

# Logout Route
@app.route('/logout')
def logout():
    users.logout()
    return redirect(url_for('home'))

# Change Password Route
@app.route('/change_password', methods=['POST'])
def change_password():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status
    if not user:
        return jsonify(success=False, message='Not logged in'), 401

    email = user['email']
    data = request.get_json()
    current_pw = data.get('current')
    new_pw = data.get('new')

    user = users.lookup(email)
    if not user:
        return jsonify(success=False, message='User not found'), 404

    if user.change_password(current_pw, new_pw):
        return jsonify(success=True, message='Password changed successfully'), 200
    return jsonify(success=False, message='Current password is incorrect'), 403

@app.route('/reset_password', methods=['POST'])
def reset_password_request():

    def _send_reset_email(to_email, token):
        reset_url = url_for('reset_with_token', token=token, _external=True)
        msg = EmailMessage()
        msg['Subject'] = 'Reset your Bird Dog password'
        msg['From'] = 'birddogpound2025@gmail.com'
        msg['To'] = to_email
        msg.set_content(f'Click the link to reset your password: {reset_url}')

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)

    data = request.get_json()
    email = data.get('email')
    user = users.lookup(email)
    if not user:
        return jsonify(success=True, message='If that email is registered, a reset link was sent.')

    token = serializer.dumps(email, salt='reset-password')
    _logger.info(f'sending password reset to {_hide(email)}')
    _send_reset_email(email, token)
    return jsonify(success=True, message='Check your email for a reset link.')

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_with_token(token):
    try:
        email = serializer.loads(token, salt='reset-password', max_age=3600)
    except Exception:
        return render_template('reset_password_expired.html')

    user = users.lookup(email)
    if not user:
        return render_template('reset_password_expired.html')

    if request.method == 'POST':
        new_pw = request.form.get('password')
        if not new_pw:
            return render_template('reset_password_form.html', token=token, error="Password is required")

        user.set_password(new_pw)
        return redirect(url_for('home'))

    return render_template('reset_password_form.html', token=token)

# ---- HELPER FUNCTIONS -------------------------------------------------------

# Helper to extract oldid from URL
def _extract_oldid(url):
    match = re.search(r'oldid=(\d+)', url)
    return int(match.group(1)) if match else 0

def _compress_history(history, max_entries=50):

    # Sort by modified date, then by oldid DESCENDING (newer edit first)
    history = sorted(
        history,
        key=lambda x: (x['modified'], -_extract_oldid(x['link']))
    )

    # Remove duplicates: keep only the first entry per modified date
    seen_dates = set()
    unique_history = []
    for h in history:
        if h['modified'] not in seen_dates:
            unique_history.append(copy(h))
            seen_dates.add(h['modified'])

    #_logger.info(f'_compress_history: unique_history={json.dumps(unique_history, indent=4)}')

    # Skip compression if already within limits
    if len(unique_history) <= max_entries:
        return unique_history

    # Compress to 1 entry per day (oldest)
    hist_by_day = {}
    for h in unique_history:
        day = h['modified'][:10]
        if day not in hist_by_day:
            hist_by_day[day] = copy(h)

    # Sort compressed by modified DESCENDING (newest first)
    compressed = sorted(hist_by_day.values(), key=lambda x: x['modified'], reverse=True)

    # Drop oldest if still above limit
    #compressed = compressed[:max_entries]

    #_logger.info(f'_compress_history: compressed={json.dumps(compressed, indent=4)}')

    return compressed

def _compare_page(page, ref_date):
    # avoid making changes to cached page - work on copy instead
    result = page.detached_copy()
    reference = page.detached_copy()
    reference.revert_to(ref_date)
    check_page_changes(result, reference)
    return result

# ---- SERVICE API ------------------------------------------------------------

# List all archives
@app.route("/archives", methods=['GET'])
def archive_list():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status
    return jsonify(ARCHIVE_MASTER_LIST)

@app.route('/page', methods=['GET'])
@app.route('/page/<archive>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
def page_data(archive=None, subarchive=None, fond=None, opus=None, case=None):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    try:
        page = None
        page_title = request.args.get('title')
        if page_title:
            #page_title = request.args.get('title')
            _logger.info(f'/page looking up title: {page_title}')
            page = runtime.lookup_by_title(page_title)
            address = page_address(page_title)
            _logger.info(f'/page mapping title to address: {address}')
            (archive, subarchive, fond, opus, case) = address
        else:
            _logger.info(f"/page request: {archive}, {subarchive}, {fond}, {opus}, {case}")
            page = runtime.lookup_by_address(archive, subarchive, fond, opus, case)
        if page:
            compare = request.args.get('compare')
            if compare:
                page = _compare_page(page, compare)

            # recheck page address (which could be different)
            address = page_address(page.title)
            true_fond, true_opus, true_case = address[2:]
            subarchive = address[1]
            
            # prevent mutation of page data in LRU/cache
            page_dict = deepcopy(page.page)
            page_dict['title'] = page.title
            page_dict['lineage'] = lineage(page.title)
            page_dict['archive'] = archive
            page_dict['subarchive'] = subarchive
            page_dict['fond'] = true_fond
            page_dict['opus'] = true_opus
            page_dict['case'] = true_case
            page_dict['kind'] = page.kind
            page_dict['name'] = page.name
            page_dict['needs_translation'] = page.needs_translation
            page_dict['history'] = _compress_history(page.history(cutoff_date='2000'))

            user.set_preference("last_page", page.title)
            return jsonify(page_dict), 200
        _logger.error(f'PageLRU({archive}, {subarchive}, {fond}, {opus}, {case}) returned None')
        return 'Page not found', 404
    except PageLRU.NotFoundError:
        _logger.error(f'PageLRU({archive}, {subarchive}, {fond}, {opus}, {case}) raised NotFoundError')
        return 'Page not found', 404

def ascii_filename(name):
    # Transliterate non-ASCII characters into closest ASCII representation
    new_name = unidecode(name)
    new_name = new_name.replace("-_", "")
    new_name = re.sub(r'[^\w\-_.]', '_', new_name)
    _logger.info(f'normalizing filename: {name} -> {new_name}')
    return new_name or "download"

# ---- EXPORT -------------------------------------------------

@app.route('/download', methods=['POST'])
def download_file():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    data = request.json
    _logger.info(f"download: data={data}")

    try:
        page = runtime.lookup_by_title(data["title"])
        if page:
            # avoid persisting any subsequent changes
            page = page.detached_copy()

            page.prepare_to_download()
            # put the page into a comparison state if requested
            compare = data["compare"]
            if compare:
                page = _compare_page(page, compare)

            page_defaults_key = f"export_{page.title}"
            defaults = user.get_preference(page_defaults_key, dict())
            defaults["template"] = data["template"]
            defaults["table"] = data["table"]
            column_map = defaults.get("column_map", dict())
            column_map[data["table"]] = data["column_map"]
            table_names = [table["name"] for table in page.tables]
            column_map = {key: value for key, value in column_map.items() if key in table_names}
            defaults["column_map"] = column_map
            user.set_preference(page_defaults_key, defaults)

            excel_io = BytesIO()
            export_page(
                page, 
                excel_io, 
                table_name=data["table"],
                template=data["template"], 
                column_map=data["column_map"], 
                lru=runtime.page_lru)
            excel_io.seek(0)  # Rewind buffer for reading

            clean_name = ascii_filename(page.name if page.name else "unnamed")
            return send_file(
                excel_io,
                as_attachment=True,
                download_name=f'{clean_name}.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                conditional=False
            )
        return 'Page not found', 404
    except FileNotFoundError:
        _logger.exception(f'File not found: {filepath}')
        return jsonify({'error': 'Page not found'}), 404
    except Exception as e:
        _logger.exception(f'Error: {e}')
        return jsonify({'error': 'Internal server error'}), 500

def _make_unique(string_list):
    # ensure strings in a list are unique (for column headers in export dialog)
    result = []
    seen = set()
    n = 2
    for source_string in string_list:
        s = source_string
        while s in seen:
            s = f"{source_string}{n}"
            n += 1
        result.append(s)
        seen.add(s)
    return result

@app.route("/export")
def export_dialog():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    try:
        page_title = request.args.get('title')
        if not page_title:
            return jsonify({'error': 'Missing required parameter "title"'}), 400
        page = runtime.lookup_by_title(page_title)
    except Exception as e:
        _logger.exception(f'Error: {e}')
        return jsonify({'error': 'Page not found'}), 404

    templates = list_templates()
    # allow for case pages to be downloaded by choosing opus as default, otherwise
    # match template name to kind of page (archive, fond, opus)

    column_headers = {}
    for table in page.tables:
        column_headers[table["name"]] = _make_unique([get_text(item) for item in table["header"]])

    #column_headers = [get_text(item) for item in page.header]
    #column_headers = _make_unique(column_headers)
    default_template = None
    header_map = {}
    default_table = None

    defaults = user.get_preference(f"export_{page.title}")
    if defaults:
        default_template = defaults.get("template")
        header_map = defaults.get("column_map", dict())
        default_table = defaults.get("table")
    if not default_template:
        default_template = (
            [item for item in list_templates() if page.kind in item] or ["opus.xlsx"])[0]
    if not default_table:
        default_table = page.tables[0]["name"] if page.tables else ""
    for table in page.tables:
        if table["name"] not in header_map:
            classification = classify_table_columns(table)
            # form mapping from column header type to column index
            header_map[table["name"]] = { 
                col_type: [i] for i, col_type in enumerate(classification["mapping"]) 
                }
            _logger.info(f"inferred header map: {header_map}")

    data = {
        "title":                page_title,
        "default_template":     default_template,
        "default_table":        default_table,
        "templates":            templates, 
        "column_classes":       list_column_classes(), 
        "column_headers":       column_headers,
        "column_header_map":    header_map,    
    }
    return jsonify(data), 200

# ---- WATCHLIST MANAGEMENT -------------------------------------------------

def _watchlist_key(archive, subarchive):
    return f'{archive}-{subarchive}'

def _format_watchlist(watchlist):
    return [
        {
            'archive': k.split('-')[0],
            'subarchive': k.split('-')[1],
            'last_checked_date': v['last_checked_date'],
            'cutoff_date': v['cutoff_date']
        }
        for k, v in watchlist.items() ]

# Get user's watchlist
@app.route('/watchlist', methods=['GET'])
def get_watchlist():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    result = _format_watchlist(user.watchlist)
    _logger.info(f'watchlist for {_hide(user.email)}: {result}')
    return jsonify(result)

# Add to user's watchlist
@app.route('/watchlist', methods=['POST'])
def add_to_watchlist():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    data = request.json
    user.add_to_watchlist(
        archive=data['archive'],
        subarchive=data['subarchive'],
        cutoff_date=data['cutoff_date']
    )

    return jsonify(_format_watchlist(user.watchlist)), 201

# Remove from user's watchlist
@app.route('/watchlist/<archive>/<subarchive>', methods=['DELETE'])
def remove_from_watchlist(archive, subarchive):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    _logger.info(f'Removing watcher[{_hide(user.email)}]: {archive}-{subarchive}')
    success = user.remove_from_watchlist(archive, subarchive)

    if success:
        return '', 204
    return jsonify({'error': 'Entry not found'}), 404

# Check for updates on a specific watchlist item
@app.route('/watchlist/<archive>/<subarchive>/check', methods=['GET'])
def check_watchlist_item(archive, subarchive):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    try:
        tree = request.args.get('tree') is not None
        result = user.check_archive(archive, subarchive, tree=tree)

        return jsonify({
            'success': True,
            'unresolved': result,
            'watchlist': _format_watchlist(user.watchlist)
        }), 200

    except KeyError:
        return jsonify({'error': 'Watchlist item not found'}), 404


@app.route('/resolve', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
def resolve_update(archive=None, subarchive=None, fond=None, opus=None, case=None):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    page_title = request.args.get('title')
    tree = request.args.get('tree') is not None
    deep = request.args.get('deep') is not None

    _logger.info(f'resolve_update(deep={deep}): {archive}, {subarchive}, {fond}, {opus}, {case}')
    try:
        result = user.resolve_item(
            archive, subarchive,
            fond=fond, opus=opus, case=case,
            tree=tree, deep=deep
        )

        return jsonify({'success': True, 'unresolved': result}), 200

    except KeyError:
        return jsonify({'error': 'Watchlist item not found'}), 404
    except FileNotFoundError:
        return jsonify({'error': 'No watcher found'}), 404
    except Exception:
        _logger.exception("Error during resolve")
        return jsonify({'error': 'Exception during resolve'}), 500

# ---- TRANSLATION MANAGEMENT -------------------------------------------------

def _active_translations(email):
    return [{
        'title': task["name"],
        'progress': task["completed"],
        'total': task["length"],
    } for task in runtime.active_translations]

@app.route('/translate', methods=['GET'])
def translate(archive=None, subarchive=None, fond=None, opus=None, case=None):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    page = None
    page_title = request.args.get('title')
    if page_title:
        page = runtime.lookup_by_title(page_title)
    if page:
        # start new translation
        runtime.start_translation(page)
    return jsonify({
        'enabled': runtime.translation_enabled,
        'available': runtime.translation_available,
        'translations': _active_translations(user.email)}), 200

# ---- LOG ACCESS ---------------------------------------------------------------

@app.route('/log')
def get_log():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status
    limit = request.args.get('limit', type=int)
    return jsonify(get_log_buffer().get_logs(limit)), 200

@app.route("/logs")
def logs_view():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status
    return render_template("logs.html")

# ---- SERVICE LOG ACCESS ---------------------------------------------------------------

@app.route("/service_usage")
def service_usage_dashboard():
    #user, error_response, status = _get_current_user()
    #if error_response:
    #    return error_response, status

    range_opt = request.args.get("range", "24h")
    by_resource_opt = request.args.get("by_resource", None)
    now = datetime.now(UTC)
    delta = {
        "5m": timedelta(minutes=5),
        "1h": timedelta(hours=1),    
        "24h": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }.get(range_opt, timedelta(days=1))

    by = "resource" if by_resource_opt and by_resource_opt != "0" else None
    df = ServiceLogger.get_logger().load_logs(now - delta, now)
    summary = ServiceLogger.summarize_service_usage(
        df, 
        sample_interval_minutes=delta.total_seconds() / 60.,
        by=by)
    summary=summary.to_dict(orient="records")
    
    return render_template("service_usage.html",
        summary=summary,
        selected_range=range_opt,
        by_resource=by_resource_opt,
    )

# ---- APP METRICS ---------------------------------------------------------------

@app.before_request
def start_timer():
    g.start_time = time.time()

@app.after_request
def log_request(response):
    duration = time.time() - g.start_time
    method = request.method
    path = request.path
    email = session.get("user", {}).get("email")
    user_id = _hide(email) if email else "unknown"
    status_code = response.status_code

    _logger.info(f"REQUEST: {user_id}, {method}, {path}, {status_code}, {duration:.4f}s")
    _event_logger.log_request(user_id, method, path, status_code, duration)
    return response

@app.route("/metrics")
def metrics_dashboard():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    range_opt = request.args.get("range", "24h")
    now = datetime.now(UTC)

    delta = {
        "24h": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }.get(range_opt, timedelta(days=1))

    df = _event_logger.load_logs(now - delta, now)
    user_hist = user_histogram(df).to_dict()
    duration_summary_df = summarize_duration_by_path_group(df).reset_index().round(4)
    summary_list = duration_summary_df.to_dict(orient="records")

    return render_template("metrics.html",
        user_histogram=user_hist,
        duration_summary=summary_list,
        selected_range=range_opt,
    )

# ---- MAIN -------------------------------------------------------------------

# initialize globals
users = Users(session)

def create_app():
    # Build/configure the Flask app here, but DO NOT start runtime here.
    return app

def start_runtime_once():
    global runtime
    if runtime is None:
        runtime = Runtime()
        runtime.start()

# Required for WSGI: this is the callable Gunicorn or EB will look for
if __name__ != "__main__":
    application = create_app()
    # Safe to start here: no reloader in production; runs once per worker.
    if detect_environment() == "aws":
        start_runtime_once()

# Local development entry point
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--port", type=int, default=2002)
    args = parser.parse_args()

    app = create_app()

    # If using the reloader, only start runtime in the reloader CHILD process.
    use_reloader = bool(args.debug)
    if use_reloader:
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            start_runtime_once()
    else:
        start_runtime_once()

    app.run(
        debug=args.debug,
        port=args.port,
        host="0.0.0.0",
        use_reloader=use_reloader
    )
