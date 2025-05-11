# system packages
import os
import threading
import re
import unicodedata
from io import BytesIO
from copy import copy, deepcopy
from datetime import datetime
from cachetools import LRUCache
from collections import defaultdict
from itsdangerous import URLSafeTimedSerializer
import smtplib
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask,
    render_template,
    request,
    send_file,
    redirect,
    url_for,
    session,
    jsonify)

# Birddog packages
from birddog.core import (
    PageLRU,
    ArchiveWatcher)
from birddog.excel import export_page
from birddog.cache import (
    load_cached_object,
    save_cached_object,
    remove_cached_object,
    CacheMissError)
from birddog.wiki import check_page_changes, all_archives

from birddog.logging import get_logger, get_log_buffer
_logger = get_logger()

# ---- INITIALIZATION  --------------------------------------------------

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

# ---- USER MANAGEMENT --------------------------------------------------------

def _hide(text):
    return f'{text[:3]}...'

def _watcher_cache_path(email, archive, subarchive):
    return f'watchers/{email}/{archive}-{subarchive}.json'

class User:
    def __init__(self, name, email, password, watchlist=None, is_hashed=False):
        self.name = name
        self.email = email
        self.password_hash = password if is_hashed else generate_password_hash(password)
        self.watchlist = watchlist or {}
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

    def check_archive(self, archive, subarchive, page_lru, tree=False):
        key = _watchlist_key(archive, subarchive)
        with self._lock:
            if key not in self.watchlist:
                raise KeyError(f"Watchlist item not found: {key}")

            path = _watcher_cache_path(self.email, archive, subarchive)
            try:
                watcher_data = load_cached_object(path)
                watcher = ArchiveWatcher.load(watcher_data, lru=page_lru)
            except CacheMissError:
                watcher = ArchiveWatcher(
                    archive, subarchive,
                    self.watchlist[key]['cutoff_date'], lru=page_lru
                )

            watcher.check()
            save_cached_object(watcher.save(), path)

            self.watchlist[key]['last_checked_date'] = datetime.now().strftime('%Y,%m,%d,%H:%M')
            self.save()

            # Return just the result, not the watcher itself
            if tree:
                return watcher.unresolved_tree
            else:
                return [{'name': k, **v} for k, v in watcher.unresolved.items()]

    def resolve_item(self, archive, subarchive, fond=None, opus=None, case=None, page_lru=None, tree=False):
        key = _watchlist_key(archive, subarchive)

        with self._lock:
            if key not in self.watchlist:
                raise KeyError('Watchlist item not found')

            path = _watcher_cache_path(self.email, archive, subarchive)
            try:
                watcher_data = load_cached_object(path)
            except CacheMissError:
                raise FileNotFoundError('No watcher found')

            watcher = ArchiveWatcher.load(watcher_data, lru=page_lru)

            resolve_key = ArchiveWatcher.key(archive, subarchive, fond, opus, case)
            _logger.info(f'Resolving {resolve_key}')
            watcher.resolve(resolve_key, deep=tree)

            save_cached_object(watcher.save(), path)

            if tree:
                return watcher.unresolved_tree
            else:
                return [{'name': k, **v} for k, v in watcher.unresolved.items()]

    def save(self):
        with self._lock:
            save_cached_object(self.to_dict(), f'users/{self.email}.json')

    def to_dict(self):
        return {
            'name': self.name,
            'password': self.password_hash,
            'watchlist': self.watchlist
        }

    @classmethod
    def from_dict(cls, email, d):
        return cls(
            name=d['name'],
            email=email,
            password=d['password'],
            watchlist=d.get('watchlist', {}),
            is_hashed=True
        )

class Users:
    def __init__(self, session, max_users=10):
        self._path = 'users'
        self._session = session
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

users = Users(session)

# ---- FRONT END PAGES --------------------------------------------------------

# Home Route (Shows the landing page)
@app.route('/')
def home():
    user = session.get('user')
    return render_template('index.html', user=user, debug=app.debug)

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
    user_session = session.get('user')
    if not user_session:
        return jsonify(success=False, message='Not logged in'), 401

    email = user_session['email']
    data = request.get_json()
    current_pw = data.get('current')
    new_pw = data.get('new')

    user = users.lookup(email)
    if not user:
        return jsonify(success=False, message='User not found'), 404

    if user.change_password(current_pw, new_pw):
        return jsonify(success=True, message='Password changed successfully'), 200
    else:
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

def _compress_history(history, max_entries=30):
    if len(history) <= max_entries:
        return history

    # Sort oldest to newest
    history = sorted(history, key=lambda x: x['modified'])

    hist_by_day = {}
    for h in history:
        day = h['modified'][:10]
        # only keep the first seen entry per day (oldest)
        if day not in hist_by_day:
            hist_by_day[day] = copy(h)

    compressed = list(hist_by_day.values())
    compressed = sorted(compressed, key=lambda x: x['modified'], reverse=True)

    # If still too many, drop oldest until we're within the limit
    if len(compressed) > max_entries:
        compressed = compressed[:max_entries]

    return compressed

page_lru = PageLRU(maxsize=500)

def _get_current_user():
    user_session = session.get('user')
    if not user_session:
        return None, jsonify({'error': 'Not logged in'}), 401

    email = user_session.get('email')
    user = users.lookup(email)
    if not user:
        return None, jsonify({'error': 'User not found'}), 404

    return user, None, None

def _compare_page(page, ref_date):
    # avoid making changes to cached page - work on copy instead
    result = deepcopy(page)
    reference = deepcopy(result)
    reference.revert_to(ref_date)
    check_page_changes(result, reference)
    return result

# ---- SERVICE API ------------------------------------------------------------

archive_master_list = all_archives()

# List all archives
@app.route("/archives", methods=['GET'])
def archive_list():
    return jsonify(archive_master_list)

@app.route('/page/<archive>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/page/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
def page_data(archive, subarchive=None, fond=None, opus=None, case=None):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    try:
        page = page_lru.lookup(archive, subarchive, fond, opus, case)
        if page:
            if page.kind == 'archive':
                subarchive = page.subarchive["en"]
            compare = request.args.get('compare')
            if compare:
                page = _compare_page(page, compare)
            # prevent mutation of page data in LRU/cache
            page_dict = deepcopy(page.page)
            page_dict['archive'] = archive
            page_dict['subarchive'] = subarchive
            page_dict['fond'] = fond
            page_dict['opus'] = opus
            page_dict['case'] = case
            page_dict['kind'] = page.kind
            page_dict['name'] = page.name
            page_dict['needs_translation'] = page.needs_translation
            page_dict['history'] = _compress_history(page.history(cutoff_date='2023'))
            return jsonify(page_dict), 200
        _logger.error(f'PageLRU({archive}, {subarchive}, {fond}, {opus}, {case}) returned None')
        return 'Page not found', 404
    except PageLRU.NotFoundError:
        _logger.error(f'PageLRU({archive}, {subarchive}, {fond}, {opus}, {case}) raised NotFoundError')
        return 'Page not found', 404

from unidecode import unidecode

def ascii_filename(name):
    # Transliterate non-ASCII characters into closest ASCII representation
    new_name = unidecode(name)
    new_name = new_name.replace("-_", "")
    new_name = re.sub(r'[^\w\-_.]', '_', new_name)
    _logger.info(f'normalizing filename: {name} -> {new_name}')
    return new_name or "download"

@app.route('/download/<archive>', methods=['GET'])
@app.route('/download/<archive>/<subarchive>', methods=['GET'])
@app.route('/download/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/download/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/download/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
def download_file(archive, subarchive=None, fond=None, opus=None, case=None):
    try:
        page = page_lru.lookup(archive, subarchive, fond, opus, case)
        if page:
            page.prepare_to_download()
            # put the page into a comparison state if requested
            compare = request.args.get('compare')
            if compare:
                page = _compare_page(page, compare)

            _logger.info(f'exporting spreadsheet to memory buffer')
            clean_name = ascii_filename(page.name if page.name else "unnamed")

            excel_io = BytesIO()
            export_page(page, excel_io, lru=page_lru)
            excel_io.seek(0)  # Rewind buffer for reading

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

## Get user's watchlist
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
    else:
        return jsonify({'error': 'Entry not found'}), 404

# Check for updates on a specific watchlist item
@app.route('/watchlist/<archive>/<subarchive>/check', methods=['GET'])
def check_watchlist_item(archive, subarchive):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    try:
        tree = request.args.get('tree') is not None
        result = user.check_archive(archive, subarchive, page_lru, tree=tree)

        return jsonify({
            'success': True,
            'unresolved': result,
            'watchlist': _format_watchlist(user.watchlist)
        }), 200

    except KeyError:
        return jsonify({'error': 'Watchlist item not found'}), 404

@app.route('/resolve/<archive>/<subarchive>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
def resolve_update(archive, subarchive, fond=None, opus=None, case=None):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status

    tree = request.args.get('tree') is not None

    _logger.info(f'resolve_update: {archive}, {subarchive}, {fond}, {opus}, {case}')
    try:
        result = user.resolve_item(
            archive, subarchive,
            fond=fond, opus=opus, case=case,
            page_lru=page_lru, tree=tree
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

_translation_tasks = {}
_task_id_map = {}
_translation_lock = threading.RLock()

def _add_user_task(email, task_id):
    with _translation_lock:
        if not email in _task_id_map:
            _task_id_map[email] = []
        if task_id not in _task_id_map[email]:
            _task_id_map[email].append(task_id)

def _active_translations(email):
    with _translation_lock:
        user_tasks = _task_id_map.get(email, [])
        return [{
            'page_name': _translation_tasks[tid]['page'].name,
            'progress': _translation_tasks[tid]['progress'],
            'total': _translation_tasks[tid]['total'],
            'running': _translation_tasks[tid]['running']
        } for tid in user_tasks]

def _translation_progress(task_id, progress, total):
    with _translation_lock:
        item = _translation_tasks[task_id]
        page = item['page']
        item['progress'] = progress
        item['total'] = total
        item['running'] = True
    _logger.info(f'translation progress: {page.name} {progress}/{total} {float(progress)/float(total)*100.:.1f}%')

def _translation_completion(task_id, results):
    with _translation_lock:
        item = _translation_tasks[task_id]
        item['running'] = False
        for user, tasks in _task_id_map.items():
            _task_id_map[user] = [task for task in tasks if task != task_id]
        if task_id in _translation_tasks:
            del _translation_tasks[task_id]
    _logger.info(f'translation completed: {item["page"].name}')

def _start_translation(email, page):
    with _translation_lock:
        task_id = next((k for k, v in _translation_tasks.items() if v["page"].name == page.name), None)
        if not task_id:
            task_id = page.translate(
                asynchronous=True,
                progress_callback=_translation_progress,
                completion_callback=_translation_completion)
            if task_id:
                _logger.info(f'translation started ({_hide(email)}): {page.name}')
                _translation_tasks[task_id] = {
                    'page': page,
                    'progress': 0,
                    'total': 1,
                    'running': True,
                }
                _add_user_task(email, task_id)
        else:
            # page is already being translated, possibly by another user
            # add to users tasks so user gets progress/completion
            _add_user_task(email, task_id)

@app.route('/translate', methods=['GET'])
@app.route('/translate/<archive>/<subarchive>', methods=['GET'])
@app.route('/translate/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/translate/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/translate/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
def translate_page(archive=None, subarchive=None, fond=None, opus=None, case=None):
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status
    if archive:
        page = page_lru.lookup(archive, subarchive, fond, opus, case)
        if page:
            # start new translation
            _start_translation(user.email, page)
    return jsonify({
        'success': True,
        'translations': _active_translations(user.email)}), 200

# ---- LOG ACCESS ---------------------------------------------------------------

@app.route('/log')
def get_log():
    user, error_response, status = _get_current_user()
    if error_response:
        return error_response, status
    limit = request.args.get('limit', type=int)
    return jsonify(get_log_buffer().get_logs(limit)), 200

# ---- MAIN -------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Run in debug mode")
    parser.add_argument("--port", type=int, default=2002, help="Port to run the server on")
    args = parser.parse_args()

    app.run(
        debug=args.debug,
        port=args.port,
        host="0.0.0.0"  # Allow external connections
    )
