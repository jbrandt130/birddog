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
├── template/             # Jinja2 HTML templates (Bootstrap-based)
├── static/               # Static Flask app assets
├── resources/            # auxilliary data, including archive list and spreadsheet templates
├── test/                 # Unit tests
├── docs/                 # Project documentation
├── notebooks/            # Jupyter notebooks
└── README.md             # You're here
```
---

## 🚀 Quickstart Guide

This guide helps you get started **locally** using the `run_local` script. No AWS setup required.

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

Create a `.env` file in the project root (or export them manually):

```ini
# .env
OPENAI_API_KEY=your-openai-key
GOOGLE_APPLICATION_CREDENTIALS=/full/path/to/google-cloud-translate-key.json

# Required for app functionality
BIRDDOG_SECRET_KEY=...
BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE=True

# Required for user password recovery mechanism only
BIRDDOG_SMTP_SERVER=...
BIRDDOG_SMTP_PORT=...
BIRDDOG_SMTP_USERNAME=...
BIRDDOG_SMTP_PASSWORD=...
```

> 💡 You can also set these directly in your shell for quick testing:
> `export OPENAI_API_KEY=...`

---

### ▶️ 3. Run the local dev server

```bash
./local_run --debug
```

This launches the Flask development server with hot reload and debug logging.

---

### 🧪 4. Run tests and coverage

To run unit tests:

```bash
python -m unittest
```

This runs all tests in the `tests/` directory from the project root — no discovery flags needed.

To check test coverage:

```bash
./coverage_report
```

---

### 📓 5. (Optional) Jupyter setup

To work with notebooks:

```bash
python3.12 -m venv venv-jupyter
source venv-jupyter/bin/activate
pip install -r requirements.txt
pip install notebook ipykernel
./lab --debug (optional)
```

---

### 📓 6. (Optional) AWS setup

Coming soon...

---

## License

MIT License
