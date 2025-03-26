# system packages
import os
from copy import copy, deepcopy
from urllib.parse import quote
from datetime import datetime
from flask import Flask, render_template, request, json, send_file, redirect, url_for, session, jsonify
#from flask import Flask, render_template, request, redirect, url_for, session, jsonify

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


app = Flask(__name__)
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
        if user and check_password_hash(user['password'], password):
            self._session['user'] = self._session_user(user['name'], email)
            return True
        return False

    def logout(self):
        self._session.pop('user', None)

users = Users(session)

# ---- FRONT END PAGES --------------------------------------------------------

# Mock user storage (replace with a database)
alerts = {
    'test@example.com': ['Alert 1', 'Alert 2', 'Alert 3']
}

# Home Route (Shows the landing page)
@app.route('/')
def home():
    user = session.get('user')
    if user:
        user_alerts = alerts.get(user['email'], [])
        return render_template('index.html', user=user, alerts=user_alerts)
    return render_template('index.html', user=None)

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

page_lru = PageLRU(maxsize=100)

def unpack_standard_args(request):
    standard_args = ('archive', 'subarchive', 'fond', 'opus', 'case', 'translate', 'compare')
    return (request.args.get(arg) for arg in standard_args)

def get_page(request):
    archive_id, subarchive_id, fond_id, opus_id, case_id, translate, compare = unpack_standard_args(request)
    result = page_lru.lookup(archive_id, subarchive_id, fond_id, opus_id, case_id)
    if result:
        if result.kind == 'archive':
            subarchive_id = result.subarchive["en"]
        if translate is not None:
            result.translate()
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
        page['history'] = result.history(limit=20)
        
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
        page_lru.lookup(data['archive'], data['subarchive']), data['cutoff_date'])
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
            return jsonify({'error': 'Watchlist item not found'}), 404
        
        # load user's watcher for this archive
        cache_path = _watcher_cache_path(email, archive, subarchive)
        try:
            watcher_data = load_cached_object(cache_path)
        except CacheMissError:
            return jsonify({'error': 'No watcher found'}), 404
        watcher = ArchiveWatcher.load(watcher_data)
        # check for updates
        watcher.check()
        print('watcher check:', watcher.unresolved)

        # save the watcher state
        save_cached_object(watcher.save(), cache_path)

        # record last check date in user data
        watchlist[key]['last_checked_date'] = datetime.now().strftime('%Y,%m,%d,%H:%M')
        user_data['watchlist'] = watchlist
        save_cached_object(user_data, f'users/{email}.json')

        result = [{'name': key, **value} for key, value in watcher.unresolved.items()]
        return jsonify({'success': True, 'unresolved': result}), 200

    except CacheMissError:
        return jsonify({'error': 'User data not found'}), 404

@app.route('/resolve/<archive>/<subarchive>', methods=['POST', 'GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>', methods=['POST', 'GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>', methods=['POST', 'GET'])
@app.route('/resolve/<archive>/<subarchive>/<fond>/<opus>/<case>', methods=['POST', 'GET'])
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
        watcher = ArchiveWatcher.load(watcher_data)

        # resolve the item
        key = ArchiveWatcher.key(archive, subarchive, fond, opus, case)
        print(f'Resolving {key}')
        watcher.resolve(key)

        # save the watcher state
        save_cached_object(watcher.save(), cache_path)

        # return updated list of unresolved items
        result = [{'name': key, **value} for key, value in watcher.unresolved.items()]
        return jsonify({'success': True, 'unresolved': result}), 200

    except CacheMissError:
        return jsonify({'error': 'Exception during resolve'}), 404

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
