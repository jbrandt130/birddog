# system packages
import os
from copy import copy, deepcopy
from urllib.parse import quote
from flask import Flask, render_template, request, json, send_file
import argparse
from cachetools import LRUCache

# Birddog packages
from birddog.core import Archive, ARCHIVE_LIST, check_page_changes, report_page_changes
from birddog.excel import export_page

app = Flask(__name__)

# ---- UTILITY FUNCTIONS ----------------------------------------------------------------

def json_response(item):
    response = app.response_class(
        response=json.dumps(item),
        status=200,
        mimetype='application/json'
    )
    return response

# ---- FRONT END PAGES ------------------------------------------------------------------

@app.route("/")
def home():
    message = "Hello World"
    return render_template('index.html', message=message)

# ---- HELPER FUNCTIONS -----------------------------------------------------------------

class PageLRU:
    def __init__(self, maxsize=10):
        self._lru = LRUCache(maxsize=maxsize)

    def _key(self, archive, subarchive, fond=None, opus=None, case=None):
        return (archive, subarchive, fond, opus, case)

    def lookup(self, archive, subarchive, fond=None, opus=None, case=None):
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

# ---- SERVICE API ----------------------------------------------------------------------

@app.route("/archives", methods=['GET'])
def archive_list():
    return json_response(ARCHIVE_LIST)

@app.route("/page", methods=['GET'])
def page_data():
    page = get_page(request)
    return json_response(page.page if page else None)

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

# ---- MAIN -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Birddog Service')
    #parser.add_argument('-c', '--clear', action='store_true', help='clear results on startup')
    args = parser.parse_args()

    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(host='0.0.0.0', port=2002, debug=True)
