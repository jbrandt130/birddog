# system packages
import os
import time
from copy import copy, deepcopy
from urllib.parse import quote
from flask import Flask, render_template, request, json, send_file, redirect, url_for, session, jsonify
#from flask import Flask, render_template, request, redirect, url_for, session, jsonify

from werkzeug.security import generate_password_hash, check_password_hash
import argparse
from cachetools import LRUCache

# Birddog packages
from birddog.core import Archive, ARCHIVE_LIST, check_page_changes, report_page_changes
from birddog.excel import export_page
from birddog.cache import load_cached_object, save_cached_object, CacheMissError

app = Flask(__name__)
app.secret_key = 'whats_the_worst_that_could_happen'  # For session management

# ---- UTILITY FUNCTIONS ------------------------------------------------------

# ---- USER MANAGEMENT --------------------------------------------------------

# Mock user storage (replace with a database)
# users = {}
alerts = {
    'test@example.com': ['Alert 1', 'Alert 2', 'Alert 3']
}

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

class PageLRU:
    def __init__(self, maxsize=10):
        self._reset_limit = 60 * 60 # seconds
        self._timer_start = time.time()
        self._lru = LRUCache(maxsize=maxsize)

    def _key(self, archive, subarchive, fond=None, opus=None, case=None):
        return (archive or '', subarchive or '', fond or '', opus or '', case or '')

    def lookup(self, archive, subarchive, fond=None, opus=None, case=None):
        # periodically flush the lru to ensure the pages don't become stale
        if time.time() - self._timer_start >= self._reset_limit:
            print('PageLRU: flushing all entries')
            self._lru.clear()
            self._timer_start = time.time()

        key = self._key(archive, subarchive, fond, opus, case)
        try:
            item = self._lru[key]
            print(f'PageLRU.lookup({key}): hit')
            return item
        except KeyError:
            print(f'PageLRU.lookup({key}): miss')
            if not fond:
                item = Archive(archive, subarchive=subarchive)
            elif not opus:
                parent = self.lookup(archive, subarchive)
                item = parent.lookup(fond)
            elif not case:
                parent = self.lookup(archive, subarchive, fond)
                item = parent.lookup(opus)
            else:
                parent = self.lookup(archive, subarchive, fond, opus)
                item = parent.lookup(case)
            self._lru[key] = item
            return item

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

# List all archives
@app.route("/archives", methods=['GET'])
def archive_list():
    return jsonify(ARCHIVE_LIST)

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

# ---- MAIN -------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Birddog Service')
    #parser.add_argument('-c', '--clear', action='store_true', help='clear results on startup')
    args = parser.parse_args()

    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(host='0.0.0.0', port=2002, debug=True)
