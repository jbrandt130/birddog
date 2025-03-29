# Birddog

**Birddog** is a Flask-based web app for tracking changes to Ukrainian records on [Wikisource](https://uk.wikisource.org) and building structured tracking spreadsheets for archival documents processing.

Originally a set of Python scripts and Jupyter notebooks, Birddog is evolving into a full web application using Flask and Bootstrap to provide a more user-friendly interface and persistent tracking.

## Features

- Monitor updates to historical Ukrainian document pages
- Scrape metadata and revisions from government archives
- Generate and manage tracking spreadsheets
- Web UI for report browsing

## Installation

```bash
git clone git@github.com:jbrandt130/birddog.git
cd birddog
pip install -r requirements.txt
```

## Running the Web App

```bash
./start_service.sh [--debug]
```

When running in debug mode, the local filesystem is used as persistent storage. When not in debug mode, data is stored on AWS S3.

## Directory Structure

- `birddog/` - Python modules implementing Birddog
- `templates/` – Jinja2 HTML templates (Bootstrap-based)
- `static/` – Static Flask app assets
- `resources/` – auxilliary data, including master list of archives and spreadsheet templates
- `notebooks/` – Legacy and exploratory Jupyter notebooks
- `test/` – Unit tests
- `start_service.sh` – Startup script for the web service

## License

MIT License
