# system packages
import os
import threading
from copy import copy, deepcopy
from urllib.parse import quote, unquote
from datetime import datetime
from time import sleep
from flask import (
    Flask,
    render_template,
    request,
    json,
    send_file,
    redirect,
    url_for,
    session,
    jsonify)

from werkzeug.security import generate_password_hash, check_password_hash

# Birddog packages
from birddog.core import (
    PageLRU,
    ArchiveWatcher,
    check_page_changes,
    report_page_changes)
from birddog.excel import export_page
from birddog.cache import (
    load_cached_object,
    save_cached_object,
    remove_cached_object,
    CacheMissError)
from birddog.utility import ARCHIVES

# ---- FLASK INITIALIZATION  --------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = os.getenv('BIRDDOG_SECRET_KEY', '')  # For session management

# ---- USER MANAGEMENT --------------------------------------------------------

class Users:
    def __init__(self, session):
        self._path = 'users'
        self._session = session

    def _session_user(self, name, email):
        return {'name': name, 'email': email}

    def lookup(self, email):
        try:
            return load_cached_object(f'{self._path}/{email}.json')
        except CacheMissError:
            return None

    def create(self, email, name, password):
        if self.lookup(email):
            return False
        print('Creating new user:', name, email)
        user = {
            'name': name,
            'password': generate_password_hash(password)
            }
        save_cached_object(user, f'{self._path}/{email}.json')
        self._session['user'] = self._session_user(name, email)
        return True

    def login(self, email, password):
        user = self.lookup(email)
        if user and 'password' in user and check_password_hash(user['password'], password):
            self._session['user'] = self._session_user(user['name'], email)
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
    print('Creating new user:', name, email)
    return jsonify({'success': True})

# Login Route
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    if users.login(email, password):
        return jsonify({'success': True})
    print('Login failed:', email)
    return jsonify({'success': False, 'message': 'Invalid email or password'}), 401


# Logout Route
@app.route('/logout')
def logout():
    users.logout()
    return redirect(url_for('home'))

# ---- HELPER FUNCTIONS -------------------------------------------------------

from copy import copy
from collections import defaultdict

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

def unpack_standard_args(request):
    standard_args = ('archive', 'subarchive', 'fond', 'opus', 'case', 'translate', 'compare')
    return (request.args.get(arg) for arg in standard_args)

def get_page(request):
    archive_id, subarchive_id, fond_id, opus_id, case_id, translate, compare = unpack_standard_args(request)
    result = page_lru.lookup(archive_id, subarchive_id, fond_id, opus_id, case_id)
    if result:
        if result.kind == 'archive':
            subarchive_id = result.subarchive["en"]
        if compare:
            # avoid making changes to cached page - work on copy instead
            result = deepcopy(result)
            reference = deepcopy(result)
            reference.revert_to(compare)
            check_page_changes(result, reference)
        page = result.page
        page['archive'] = archive_id
        page['subarchive'] = subarchive_id
        page['fond'] = fond_id
        page['opus'] = opus_id
        page['case'] = case_id
        page['kind'] = result.kind
        page['name'] = result.name
        page['needs_translation'] = result.needs_translation
        page['history'] = _compress_history(result.history(cutoff_date='2023'))

    return result

# ---- SERVICE API ------------------------------------------------------------

archive_master_list = [(arc, sub['subarchive']['en']) for arc, archive in ARCHIVES.items() for sub in archive.values()]
print(archive_master_list)

# List all archives
@app.route("/archives", methods=['GET'])
def archive_list():
    return jsonify(archive_master_list)

@app.route("/page", methods=['GET'])
def page_data():
    page = get_page(request)
    return jsonify(page.page if page else None)

@app.route('/download', methods=['GET'])
def download_file():
    try:
        page = get_page(request)
        if page:
            filename = f'{page.name.replace("/", "_")}.xlsx'
            filepath = f'static/downloads/{filename}'
            os.makedirs('static/downloads', exist_ok=True)
            print(f'exporting to {filepath}')
            export_page(page, filepath)
            response = send_file(
                filepath,
                as_attachment=True,
                download_name=filename,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'  # Correct MIME type for Excel
            )
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
    except FileNotFoundError:
        abort(404)  # Return a 404 error if the file is missing
    except Exception as e:
        print(f'Error: {e}')
        abort(500)  # Internal server error

def _watchlist_key(archive, subarchive):
    return f'{archive}-{subarchive}'

## Get user's watchlist
@app.route('/watchlist', methods=['GET'])
def get_watchlist():
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    email = user['email']
    try:
        user_data = load_cached_object(f'users/{email}.json')
        watchlist = user_data.get('watchlist', {})
        # Convert to list format for frontend compatibility
        result = [
            {
                'archive': k.split('-')[0],
                'subarchive': k.split('-')[1],
                'last_checked_date': v['last_checked_date'],
                'cutoff_date': v['cutoff_date']
            }
            for k, v in watchlist.items()
        ]
        print(f'watchlist for {email}: {result}')
        return jsonify(result)
    except CacheMissError:
        return jsonify([])

def _watcher_cache_path(email, archive, subarchive):
    return f'watchers/{email}/{archive}-{subarchive}.json'

# Add to user's watchlist
@app.route('/watchlist', methods=['POST'])
def add_to_watchlist():
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json
    email = user['email']

    try:
        user_data = load_cached_object(f'users/{email}.json')
    except CacheMissError:
        user_data = {}

    watchlist = user_data.get('watchlist', {})

    key = _watchlist_key(data['archive'], data['subarchive'])
    watchlist[key] = {
        'last_checked_date': '',
        'cutoff_date': data['cutoff_date']
    }

    user_data['watchlist'] = watchlist
    save_cached_object(user_data, f'users/{email}.json')

    watcher = ArchiveWatcher(
        data['archive'], 
        data['subarchive'], 
        data['cutoff_date'],
        lru=page_lru)
    watcher.check()
    watcher_path = _watcher_cache_path(email, data['archive'], data['subarchive'])
    save_cached_object(watcher.save(), watcher_path)
    return jsonify({'success': True}), 201

# Remove from user's watchlist
@app.route('/watchlist/<archive>/<subarchive>', methods=['DELETE'])
def remove_from_watchlist(archive, subarchive):
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    email = user['email']

    try:
        print(f'Removing watcher[{email}]: {archive}-{subarchive}')
        user_data = load_cached_object(f'users/{email}.json')
        watchlist = user_data.get('watchlist', {})
        key = _watchlist_key(archive, subarchive)
        if key in watchlist:
            # remove from user's wathchlist
            del watchlist[key]
            user_data['watchlist'] = watchlist
            save_cached_object(user_data, f'users/{email}.json')
            # remove user's watcher data
            watcher_path = _watcher_cache_path(email, archive, subarchive)
            remove_cached_object(watcher_path)
            return '', 204
        else:
            return jsonify({'error': 'Entry not found'}), 404
    except CacheMissError:
        return jsonify({'error': 'User data not found'}), 404

# Check for updates on a specific watchlist item
@app.route('/watchlist/<archive>/<subarchive>/check', methods=['GET'])
def check_watchlist_item(archive, subarchive):
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401
    email = user['email']

    try:
        # Load user data and watchlist
        user_data = load_cached_object(f'users/{email}.json')
        watchlist = user_data.get('watchlist', {})

        key = _watchlist_key(archive, subarchive)
        if key not in watchlist:
            print(f'watchlist check: {key} not present')
            return jsonify({'error': 'Watchlist item not found'}), 404

        # load user's watcher for this archive
        cache_path = _watcher_cache_path(email, archive, subarchive)
        try:
            watcher_data = load_cached_object(cache_path)
        except CacheMissError:
            print(f'watcher load failed: {cache_path}')
            return jsonify({'error': 'No watcher found'}), 404
        watcher = ArchiveWatcher.load(watcher_data, lru=page_lru)
        # check for updates
        watcher.check()
        #print('watcher check:', watcher.unresolved)

        # save the watcher state
        save_cached_object(watcher.save(), cache_path)

        # record last check date in user data
        watchlist[key]['last_checked_date'] = datetime.now().strftime('%Y,%m,%d,%H:%M')
        user_data['watchlist'] = watchlist
        save_cached_object(user_data, f'users/{email}.json')

        if request.args.get('tree') is not None:
            result = watcher.unresolved_tree
        else:
            result = [{'name': key, **value} for key, value in watcher.unresolved.items()]
        return jsonify({'success': True, 'unresolved': result}), 200

    except CacheMissError:
        print(f'user data load failed: {email}')
        return jsonify({'error': 'User data not found'}), 404

@app.route('/resolve/<archive>/<subarchive>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>', methods=['GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['GET'])
def resolve_update(archive, subarchive, fond=None, opus=None, case=None):
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    email = user['email']

    try:
        # Load user data and watchlist
        user_data = load_cached_object(f'users/{email}.json')
        watchlist = user_data.get('watchlist', {})

        key = _watchlist_key(archive, subarchive)
        if key not in watchlist:
            return jsonify({'error': 'Watchlist item not found'}), 404

        # load user's watcher for this archive
        cache_path = _watcher_cache_path(email, archive, subarchive)
        try:
            watcher_data = load_cached_object(cache_path)
        except CacheMissError:
            return jsonify({'error': 'No watcher found'}), 404
        watcher = ArchiveWatcher.load(watcher_data, lru=page_lru)

        # resolve the item
        key = ArchiveWatcher.key(archive, subarchive, fond, opus, case)
        print(f'Resolving {key}')
        watcher.resolve(key, deep=request.args.get('tree', False))

        # save the watcher state
        save_cached_object(watcher.save(), cache_path)

        # return updated list of unresolved items
        if request.args.get('tree') is not None:
            result = watcher.unresolved_tree
        else:
            result = [{'name': key, **value} for key, value in watcher.unresolved.items()]
        return jsonify({'success': True, 'unresolved': result}), 200

    except CacheMissError:
        return jsonify({'error': 'Exception during resolve'}), 404

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
    print(f'translation progress: {page.name} {progress}/{total} {float(progress)/float(total)*100.:.1f}%')

def _translation_completion(task_id, results):
    with _translation_lock:
        item = _translation_tasks[task_id]
        item['running'] = False
        for user, tasks in _task_id_map.items():
            _task_id_map[user] = [task for task in tasks if task != task_id]
        if task_id in _translation_tasks:
            del _translation_tasks[task_id]
    print(f'translation completed: {item["page"].name}')

def _start_translation(email, page):
    with _translation_lock:
        task_id = next((k for k, v in _translation_tasks.items() if v["page"].name == page.name), None)
        if not task_id:
            task_id = page.translate(
                asynchronous=True,
                progress_callback=_translation_progress,
                completion_callback=_translation_completion)
            if task_id:
                print(f'translation started ({email}): {page.name}')
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
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401
    email = user['email']
    if archive:
        page = page_lru.lookup(archive, subarchive, fond, opus, case)
        if page:
            # start new translation
            _start_translation(email, page)
    return jsonify({
        'success': True,
        'translations': _active_translations(email)}), 200

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
