# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

# system packages
import os
import threading
import re
import time
from functools import wraps
import hashlib
from copy import copy, deepcopy
from datetime import datetime, timedelta, UTC
from collections import defaultdict
from email.message import EmailMessage
import smtplib
from unidecode import unidecode

from itsdangerous import URLSafeTimedSerializer

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
from birddog.runtime import Runtime, PageLRU
from birddog.excel import list_templates
from birddog.cache import (
    load_cached_object,
    CacheMissError)
from birddog.wiki import (
    all_archives,
    page_address,
    lineage,
    ARCHIVE_BY_ADDRESS,
    )
from birddog.user import User
from birddog.ai import list_column_classes, classify_table_columns
from birddog.utility import get_text, system_resource_report
from birddog.log import (
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

class Users:
    """
    User manager backed by S3 (via load_cached_object/save_cached_object).

    - No Flask session dependency.
    - No in-memory user cache (every lookup hits S3).
    - Per-email locks only for write/create races.
    """

    def __init__(self, path='users'):
        self._path = path
        self._locks = defaultdict(threading.Lock)

    def _user_path(self, email: str) -> str:
        return f"{self._path}/{email}.json"

    def lookup(self, email: str):
        """
        Return a User instance for the given email, or None if not found.
        """
        try:
            data = load_cached_object(self._user_path(email))
        except (CacheMissError, KeyError):
            return None
        return User.from_dict(email, data, runtime=runtime)

    def create(self, email: str, name: str, password: str):
        """
        Create a new user if it does not already exist.

        Returns:
            User instance on success, or None if the user already exists.
        """
        lock = self._locks[email]
        with lock:
            existing = self.lookup(email)
            if existing is not None:
                return None

            user = User(name, email, password)
            user.save()
            return user

    def login(self, email: str, password: str):
        """
        Validate credentials.

        Returns:
            User instance if email/password are valid, otherwise None.
        """
        user = self.lookup(email)
        if user and user.check_password(password):
            return user
        return None


def _get_current_user():
    user_session = session.get('user')
    if not user_session:
        return None, jsonify({'error': 'Not found'}), 404

    email = user_session.get('email')
    if not email:
        return None, jsonify({'error': 'Not found'}), 404

    user = users.lookup(email)
    if not user:
        return None, jsonify({'error': 'Not found'}), 404

    if runtime.state != "running":
        return None, jsonify({
            "error": "Service unavailable",
            "reason": "Birddog runtime emergency shutdown"
        }), 503

    return user, None, None

# decorator for endpoints requiring login
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, error_response, status = _get_current_user()
        if error_response:         # covers 404 or 503 cases
            return error_response, status
        return f(user, *args, **kwargs)
    return wrapper

# ---- FRONT END PAGES --------------------------------------------------------

# Home Route (Shows the landing page)
@app.route('/', methods=['GET'])
def home():
    user_session = None
    start_title = None
    if runtime.state == "running":
        user_session = session.get('user')
        start_title = None
        if user_session:
            user, error_response, status = _get_current_user()
            if error_response:
                return error_response, status
            if user:
                start_title = user.get_preference("last_page")
    return render_template(
        'index.html',
        user=user_session,
        start_title=start_title,
        runtime_state=runtime.state,
        database_available=runtime.database_update_enabled,
        debug=app.debug)

# ---- SESSION MANAGEMENT -----------------------------------------------------

from flask import request, jsonify, session, redirect, url_for

# Signup Route
@app.route('/signup', methods=['POST'])
def signup():
    data = request.get_json() or {}
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')

    if not all([name, email, password]):
        return jsonify({'success': False, 'message': 'Name, email, and password are required'}), 400

    user = users.create(email, name, password)
    if not user:
        return jsonify({'success': False, 'message': 'Email already exists'}), 400

    _logger.info(f"Creating new user: {name} {_hide(email)}")
    # Log them in immediately
    session['user'] = {'name': user.name, 'email': user.email}

    return jsonify({'success': True}), 200


# Login Route
@app.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = data.get('email')
    password = data.get('password')

    if not all([email, password]):
        return jsonify({'success': False, 'message': 'Email and password are required'}), 400

    user = users.login(email, password)
    if user:
        session['user'] = {'name': user.name, 'email': user.email}
        _logger.info(f"Login successful: {_hide(email)}")
        return jsonify({'success': True}), 200

    _logger.info(f"Login failed: {_hide(email)}")
    return jsonify({'success': False, 'message': 'Invalid email or password'}), 401


# Logout Route
@app.route('/logout', methods=['GET'])
def logout():
    session.pop('user', None)
    return redirect(url_for('home'))


# Change Password Route
@app.route('/change_password', methods=['POST'])
@login_required
def change_password(user):
    data = request.get_json() or {}
    current_pw = data.get('current')
    new_pw = data.get('new')

    if not all([current_pw, new_pw]):
        return jsonify(success=False, message='Current and new passwords are required'), 400

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

# ---- SERVICE API ------------------------------------------------------------

# List all archives
@app.route("/archives", methods=['GET'])
@login_required
def archive_list(user):
    return jsonify(ARCHIVE_MASTER_LIST)

@app.route('/page', methods=['GET'])
@login_required
def page_data(user):
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
            return "Missing required parameter: 'title'", 400
        if page:
            ref_date = request.args.get('compare')
            if ref_date:
                page = page.compare(ref_date)

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
@login_required
def download_file_start(user):
    data = request.json
    _logger.info(f"download: data={data}")
    try:
        page_title = data["title"]
        page = runtime.lookup_by_title(page_title)
        if page:
            # lookup default export settings for this page, if any
            # and refresh defaults based on request data
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

            # start export in background
            task_id = runtime.export_manager.export_page(
                page_title,
                compare = data["compare"],
                table_name=data["table"],
                template=data["template"],
                column_map=data["column_map"])

            return jsonify({"status": "in-progress", "task_id": task_id}), 202
        return 'Page not found', 404
    except FileNotFoundError:
        _logger.exception(f'File not found: {filepath}')
        return jsonify({'error': 'Page not found'}), 404
    except Exception as e:
        _logger.exception(f'Error: {e}')
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/download', methods=['GET'])
@login_required
def download_file_check(user):
    task_id = request.args.get('task_id')
    page_title = request.args.get("title")
    page = runtime.lookup_by_title(page_title)
    if not page:
        return 'Page not found', 404
    #_logger.info(f'/download: system resources: {system_resource_report()}')
    if runtime.export_manager.is_complete(task_id):
        # return file when done
        excel_io = runtime.export_manager.get_result(task_id)
        if excel_io:
            clean_name = ascii_filename(page.name if page.name else "unnamed")
            return send_file(
                excel_io,
                as_attachment=True,
                download_name=f'{clean_name}.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                conditional=False
            )
        return jsonify({'error': 'Internal server error'}), 500
    return jsonify({"status": "in-progress", "task_id": task_id}), 202

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

@app.route("/export", methods=['GET'])
@login_required
def export_dialog(user):
    # assemble and return payload for export dialog
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
        if default_table:
            # make sure selected table still exists
            table_names = [table["name"] for table in page.tables]
            if default_table not in table_names:
                _logger.info(f"ignoring non-existent table name: {default_table}")
                default_table = None
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

def _safe_split_pair(key, sep="-"):
    parts = key.split(sep, 1)
    return parts if len(parts) == 2 else (key, "")

def _format_watchlist(watchlist):
    return [
        {
            "archive": archive,
            "subarchive": subarchive,
            "last_checked_date": v.get("last_checked_date"),
            "cutoff_date": v.get("cutoff_date"),
            "title": ARCHIVE_BY_ADDRESS.get((archive, subarchive)),
        }
        for key, v in watchlist.items()
        for archive, subarchive in (_safe_split_pair(key),)
    ]

# Get user's watchlist
@app.route('/watchlist', methods=['GET'])
@login_required
def get_watchlist(user):
    result = _format_watchlist(user.get_watchlist())
    _logger.info(f'watchlist for {_hide(user.email)}: {result}')
    return jsonify(result)

# Add to user's watchlist
@app.route('/watchlist', methods=['POST'])
@login_required
def add_to_watchlist(user):
    data = request.json
    user.add_to_watchlist(
        archive=data['archive'],
        subarchive=data['subarchive'],
        cutoff_date=data['cutoff_date']
    )

    return jsonify(_format_watchlist(user.get_watchlist())), 201

# Remove from user's watchlist
@app.route('/watchlist/<archive>/<subarchive>', methods=['DELETE'])
@login_required
def remove_from_watchlist(user, archive, subarchive):
    _logger.info(f'Removing watcher[{_hide(user.email)}]: {archive}-{subarchive}')
    success = user.remove_from_watchlist(archive, subarchive)

    if success:
        return '', 204
    return jsonify({'error': 'Entry not found'}), 404

# Check for updates on a specific watchlist item
@app.route('/watchlist/<archive>/<subarchive>/check', methods=['GET'])
@login_required
def check_watchlist_item(user, archive, subarchive):
    try:
        tree = request.args.get('tree') is not None
        result = user.check_archive(archive, subarchive, tree=tree)

        return jsonify({
            'success': True,
            'unresolved': result,
            'watchlist': _format_watchlist(user.get_watchlist())
        }), 200

    except KeyError:
        return jsonify({'error': 'Watchlist item not found'}), 404


@app.route('/resolve', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
@login_required
def resolve_update(user, archive=None, subarchive=None, fond=None, opus=None, case=None):
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

def _active_translations():
    return [{
        'title': task["name"],
        'progress': task["completed"],
        'total': task["length"],
    } for task in runtime.active_translations]

@app.route('/translate', methods=['GET'])
@login_required
def translate(user):
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
        'translations': _active_translations()}), 200

# ---- DATABASE SYNC  -------------------------------------------------

def _active_updates():
    #return [{
    #    'name': task["name"],
    #    'progress': task["completed"],
    #    'total': task["length"],
    #} for task in runtime.active_database_updates]
    return runtime.active_database_updates

@app.route('/database_update', methods=['GET'])
@login_required
def database_update(user):
    page_title = request.args.get('title')
    deep = request.args.get('deep')
    if deep in ("0", "false", "False"):
        deep = False
    else:
        deep = bool(deep)
    if page_title:
        # start new update
        _logger.info(f"starting database update: {page_title} (deep={deep})")
        runtime.update_to_database(page_title, deep=deep)
    return jsonify({
        'title': page_title,
        'deep': deep,
        'enabled': runtime.database_update_enabled,
        'tasks': _active_updates()}), 200

@app.route('/database_cancel_task', methods=['POST'])
@login_required
def database_cancel_task(user):
    task_name = request.args.get('task')
    if not task_name:
        return jsonify({'error': 'Missing required parameter: task'}), 400
    try:
        runtime.cancel_update(task_name)
        return '', 200
    except KeyError:
        return jsonify({'error': f'Task not found: {task_name}'}), 404

@app.route('/database_status', methods=['GET'])
@login_required
def database_status(user):
    return render_template('database_status.html')

# ---- LOG ACCESS ---------------------------------------------------------------

@app.route('/log', methods=['GET'])
@login_required
def get_log(user):
    limit = request.args.get('limit', type=int)
    return jsonify(get_log_buffer().get_logs(limit)), 200

@app.route("/logs", methods=['GET'])
@login_required
def logs_view(user):
    return render_template("logs.html")

# ---- SERVICE LOG ACCESS ---------------------------------------------------------------

@app.route("/service_usage", methods=['GET'])
def service_usage_dashboard():
    range_opt = request.args.get("range", "1h")
    by_resource_opt = request.args.get("by_resource", None)
    now = datetime.now(UTC)
    delta = {
        "5m": timedelta(minutes=5),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "8h": timedelta(hours=8),
        "24h": timedelta(days=1),
        "7d": timedelta(days=7),
    }.get(range_opt, timedelta(hours=1))

    by = "resource" if by_resource_opt and by_resource_opt != "0" else None
    df = ServiceLogger.get_logger().load_logs(now - delta, now)
    summary = {}
    if not df.empty:
        summary = ServiceLogger.summarize_service_usage(
            df,
            sample_interval_minutes=delta.total_seconds() / 60.,
            by=by)
        summary=summary.to_dict(orient="records")

    return render_template("service_usage.html",
        summary=summary,
        selected_range=range_opt,
        by_resource=by_resource_opt,
        runtime_state=runtime.state,
    )

@app.route("/good_dog", methods=['GET'])
def unpause():
    if runtime.state != "running":
        runtime.unpause()
    return jsonify({'success': True, 'runstate': runtime.state}), 200

@app.route("/bad_dog", methods=['GET'])
def pause():
    if runtime.state != "paused":
        runtime.pause()
    return jsonify({'success': True, 'runstate': runtime.state}), 200

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

@app.route("/metrics", methods=['GET'])
@login_required
def metrics_dashboard(user):
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
users = Users()

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
