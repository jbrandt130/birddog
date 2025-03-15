import os
from urllib.parse import quote
from flask import Flask, render_template, request, json, send_file
import argparse
from birddog.core import Archive, ARCHIVE_LIST
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

# ---- SERVICE API ----------------------------------------------------------------------

@app.route("/archives", methods=['GET'])
def archive_list():
    return json_response(ARCHIVE_LIST)

def get_page(request):
    archive_id = request.args.get('archive')
    fond_id = request.args.get('fond')
    opus_id = request.args.get('opus')
    case_id = request.args.get('case')
    translate = request.args.get('translate')

    result = None
    if archive_id in ARCHIVE_LIST:
        result = Archive(archive_id)
        if result is not None and fond_id:
            result = result.lookup(fond_id)
            if result is not None and opus_id:
                result = result.lookup(opus_id)
                if result is not None and case_id:
                    result = result.lookup(case_id)
    if translate is not None:
        result.translate()
    if result:
        page = result.page
        page['archive'] = archive_id
        page['fond'] = fond_id
        page['opus'] = opus_id
        page['case'] = case_id
        page['kind'] = result.kind
    return result

@app.route("/page", methods=['GET'])
def page():
    page = get_page(request)
    return json_response(page.page if page else None)

@app.route('/download', methods=['GET'])
def download_file():
    try:
        page = get_page(request)
        if page:
            filename = f'{page.ascii_name.replace("/", "_")}.xlsx'
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
