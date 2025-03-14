from flask import Flask, render_template
import argparse

app = Flask(__name__)
            
@app.route("/")
def home():
    message = "Hello World"
    return render_template('index.html', message=message)

# run the application
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Birddog Service')
    #parser.add_argument('-c', '--clear', action='store_true', help='clear results on startup')
    args = parser.parse_args()

    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(host='0.0.0.0', port=2002, debug=False)
