# Birddog

Birddog is a web-based tool for navigating and translating Ukrainian archival documents. It supports structured browsing, revision history, side-by-side comparison, and batch translation using Google Cloud and OpenAI APIs.

## Features

- Monitor updates to historical Ukrainian document pages
- Scrape metadata and revisions from government archives
- Generate and manage tracking spreadsheets
- Web UI for report browsing

### 🗂️ Project Overview

```bash
birddog/
├── birddog/              # Core application code
├── templates/            # Jinja2 HTML templates (Bootstrap-based)
├── static/               # Static Birddog client assets
├── resources/            # Application data including archive list and spreadsheet templates
├── test/                 # Unit tests
├── docs/                 # Project documentation
├── sh/                   # Shell scripts
├── notebooks/            # Jupyter notebooks
└── README.md             # You're here
```
---

## 🚀 Quickstart Guide

This guide helps you get started **locally** using the `local_run` script. No AWS setup required.

### ✅ Requirements

- Python 3.12
- `git`
- Google Cloud Translation API credentials (JSON)
- OpenAI API key (for GPT-powered classification)
- [Optional] Jupyter for notebooks

---

### 📦 1. Clone and set up the project

```bash
git clone https://github.com/jbrandt130/birddog.git
cd birddog
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

### 🔐 2. Set Environment Variables

Set the following environment variables to configure Birddog.

```
# Required for Flask session management
BIRDDOG_SECRET_KEY=pick_something_unique

# Required to use Google for translation
GOOGLE_TRANSLATE_API_KEY=your_api_key
BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE=True
```

#### Translation Settings

To run Birddog without invoking Google Cloud translation services (useful for debugging), 
adjust the environment as follows:
```
BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE=False
BIRDDOG_TRANSLATION_DEBUG=True
```

#### OPTIONAL: Password Recovery Settings

Birddog sends the user an encrypted token as part of the password recovery workflow. In
order to send email, Birddog requires an SMTP server with valid credentials. These are set
using the following environment variables:
```
BIRDDOG_SMTP_SERVER=smtp_server_address
BIRDDOG_SMTP_PORT=smtp_port_number
BIRDDOG_SMTP_USERNAME=valid_username
BIRDDOG_SMTP_PASSWORD=valid_password
```

---

### ▶️ 3. Run the local dev server

```bash
./sh/local_run
```

This launches the Flask development server with hot reload and debug logging. A few seconds after the app starts, the 
Birddog client will be opened in the default browser. The app uses the local directory `.cache` to store
persistent data for the app. This includes a page cache, user profile data, and databases.

---

### 🧪 4. Run tests and coverage

To run unit tests:

```bash
python -m unittest
```

This runs all tests in the `tests/` directory from the project root — no discovery flags needed.

To check test coverage:

```bash
./sh/coverage_report
```

---

### 📓 5. (Optional) Jupyter setup

To work with notebooks:

```bash
python3.12 -m venv venv-jupyter
source venv-jupyter/bin/activate
pip install -r requirements.txt
pip install notebook ipykernel
./sh/lab
```

---

### 📓 6. (Optional) AWS setup

Coming soon...

---

## License

MIT License
