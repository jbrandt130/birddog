# Birddog

Birddog is a web-based tool for navigating and translating Ukrainian archival documents hosted on [WikiSource](https://uk.wikisource.org). It lets users track and evaluate changes to Wiki page content, manage a watchlist of archives, and export tracking spreadsheets for downstream processing.

It also includes an experimental set of location-processing tools (under `research/`) for extracting and matching geographic references in document descriptions.

## Features

- User accounts (signup / login / password reset)
- Monitor updates to historical Ukrainian document pages via a per-user watchlist
- Scrape metadata and revisions from government archives
- Browse/resolve workflow for reviewing page changes
- Google Cloud–powered translation of page content
- Generate and export tracking spreadsheets (Excel)
- Web UI for report browsing, logs, and usage metrics
- [Experimental] Location processing tools (`research/`)
  - Location matching against administrative hierarchies
  - AI-powered location extraction from text descriptions
  - Cyrillic text handling for Russian/Ukrainian place names
  - Filtering by administrative level (e.g., villages vs. district centers)

## Project Structure

```bash
birddog/
├── application.py         # WSGI entry point (gunicorn / `python application.py`)
├── birddog/                # Core application code
│   ├── service.py          #   Flask app / HTTP routes
│   ├── abstract_database.py#   Database abstraction (interface + error types)
│   ├── nocodb_database.py  #   NocoDB backend implementation
│   ├── database.py         #   Selects the active Database implementation
│   ├── watcher.py          #   Per-user watchlist change detection
│   ├── wiki.py              #   WikiSource scraping/parsing
│   ├── tracker.py           #   Tracking spreadsheet generation
│   ├── translate.py         #   Google Cloud Translate integration
│   ├── user.py               #   Accounts / auth
│   ├── store.py, cache.py    #   Storage and caching helpers
│   └── ...
├── templates/              # Jinja2 HTML templates (Bootstrap-based)
├── static/                 # Static Birddog client assets
├── resources/               # Application data, archive lists, db_config.json
├── docs/                    # End-user guide and API reference
├── test/                    # Unit tests (unittest)
├── sh/                      # Dev/run/deploy shell scripts
├── nocodb-dev/               # Local NocoDB (Docker) setup for development
├── notebooks/                # Jupyter notebooks
└── research/                  # Experimental tools
    └── triage/locations/       # Location processing pipeline
```

---

## Requirements

- Python 3.12+
- `git`
- Docker (for running NocoDB locally — see [Persistence](#persistence))
- Google Cloud Translate API key (for translation)
- [Optional] Jupyter Notebooks
- [Optional] Modal account (for deploying the Qwen model used by the research tools)

## Install Dependencies

```bash
pip install -r requirements.txt

# Only needed for the research/location tools:
pip install rapidfuzz modal
```

---

## Persistence

Birddog has two separate persistence layers that are easy to conflate:

1. **Internal application state** — users, sessions, watchlists, task queues, and the object cache. This is Birddog's own storage, private to the running service.
2. **NocoDB output database** — archive pages and documents are written into NocoDB tables as Birddog's downstream deliverable, replacing an older spreadsheet-based workflow. This is the "product" of a Birddog run, not internal state.

### Internal application state

Internal state is split across two mechanisms, each of which picks its backend automatically based on `birddog/env.py`'s `detect_environment()` (driven by `BIRDDOG_AWS_ENVIRONMENT`):

- **Key/value store and task queues** (`birddog/store.py`) — used for things like the watchlist (`watcher.py`), tracker state (`tracker.py`), user records (`user.py`), and the task queue (`task.py`). Backed by **SQLite** locally (files under `.cache/`, e.g. `.cache/key_value_store.db`, `.cache/string_queues.db`) and by **DynamoDB** when `detect_environment()` returns `"aws"`.
- **Object cache** (`birddog/cache.py`) — used for larger cached payloads (e.g. scraped page content). Backed by the **local filesystem** under `.cache/` when `BIRDDOG_USE_LOCAL_CACHE` is set, and by an **S3** bucket (`birddog-data`) otherwise.

In short: run locally and everything lives under `.cache/` (filesystem + SQLite); run on AWS and it's S3 + DynamoDB instead. `sh/local_run` sets `BIRDDOG_USE_LOCAL_CACHE=True` for you; `sh/aws_run` sets `BIRDDOG_AWS_ENVIRONMENT=1` on the deployed environment.

### NocoDB output database

Archive pages and documents scraped/tracked by Birddog are persisted into NocoDB tables behind a small database abstraction, so the output backend could in principle be swapped without touching the application code that produces it:

- `birddog/abstract_database.py` defines the abstract `Database` interface (`scan`, `read`, `write`, `delete`, `create_links`/`delete_links`, attachments, etc.) and the shared error hierarchy (`ConfigError`, `FailedIO`, `InvalidRecordId`, ...).
- `birddog/nocodb_database.py` is the current (and only) implementation, backed by [NocoDB](https://nocodb.com/).
- `birddog/database.py` / `birddog/env.py` select the active implementation; today this always resolves to `NocoDBDatabase`.

#### Configuring the NocoDB connection

Connection settings for each deployment target live in `resources/db_config.json`, keyed by environment name:

| Environment    | Purpose                              |
|----------------|---------------------------------------|
| `LOCAL`        | Docker-hosted NocoDB on your machine  |
| `CLOUD_STAGE`   | Hosted NocoDB Cloud, staging base     |
| `CLOUD_PROD`    | Hosted NocoDB Cloud, production base  |

Each entry specifies the NocoDB host, base ID, request rate-limiting settings, and the *name* of an environment variable that holds the API token for that environment (the token itself is never checked in).

At runtime, Birddog selects an environment via:

```bash
BIRDDOG_NOCODB_ENV=LOCAL   # one of: LOCAL, CLOUD_STAGE, CLOUD_PROD
```

and expects the corresponding token environment variable to be set, per `resources/db_config.json`:

| `BIRDDOG_NOCODB_ENV` | Token env var                    |
|-----------------------|------------------------------------|
| `LOCAL`                | `NOCODB_API_TOKEN_LOCAL`           |
| `CLOUD_STAGE` / `CLOUD_PROD` | `BIRDDOG_CLOUD_NOCODB_API_TOKEN` |

#### Running NocoDB locally

For local development, run NocoDB via Docker Compose:

```bash
cd nocodb-dev
docker compose up -d
```

This starts NocoDB on `http://localhost:8080`, persisting its SQLite data to the `nocodb/` directory at the repo root (update the volume path in `nocodb-dev/docker-compose.yml` if your checkout lives elsewhere). Create an API token in the NocoDB UI, export it as `NOCODB_API_TOKEN_LOCAL`, and set `BIRDDOG_NOCODB_ENV=LOCAL` before starting Birddog (`sh/local_run` does this for you).

---

## Setup

### 1. Environment Variables

Set these in your `.env` file or directly in your environment:

```bash
# Required for Flask session management
BIRDDOG_SECRET_KEY=your_secret_key_here

# Required: selects the NocoDB output backend (see Persistence section above)
BIRDDOG_NOCODB_ENV=LOCAL
NOCODB_API_TOKEN_LOCAL=your_local_nocodb_api_token

# Required for Google Cloud Translation
GOOGLE_TRANSLATE_API_KEY=your_google_api_key

# Optional: password-reset email (SMTP)
BIRDDOG_SMTP_SERVER=smtp.example.com
BIRDDOG_SMTP_PORT=587
BIRDDOG_SMTP_USERNAME=your_smtp_username
BIRDDOG_SMTP_PASSWORD=your_smtp_password

# Optional: use local filesystem cache instead of the default cache backend
BIRDDOG_USE_LOCAL_CACHE=True

# Set automatically in the AWS deployment; do not set locally
# BIRDDOG_AWS_ENVIRONMENT=1

# For AI-powered location extraction (research tools only)
HF_TOKEN=your_huggingface_token  # Free tier token works for basic use

# Optional: Modal deployment URL (research tools only, if using custom deployment)
# MODAL_QWEN_URL=https://your-deployment.modal.run
```

### 2. Translation Settings

To disable Google Cloud Translation (useful for debugging):
```bash
BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE=False
BIRDDOG_TRANSLATION_DEBUG=True
```

---

## Running Birddog

With NocoDB running locally (see [Persistence](#persistence)) and your environment variables set:

```bash
sh/local_run
```

This sets `BIRDDOG_NOCODB_ENV=LOCAL` and `BIRDDOG_USE_LOCAL_CACHE=True`, starts the Flask app on port 2003 in debug mode, and opens it in your browser. Equivalently:

```bash
python3 -m birddog.service --port 2003 --debug
```

In production, the app is served via `application.py` (a standard WSGI entry point for `gunicorn`).

See `docs/help.md` for an end-user walkthrough of the UI and `docs/birddog_api_reference.md` for the full HTTP API reference.

---

## AWS Deployment

Birddog deploys to AWS Elastic Beanstalk, managed via `sh/aws_run`. This requires the AWS CLI, the `eb` CLI, and an AWS profile named `birddog-admin` (`aws configure --profile birddog-admin`).

```bash
sh/aws_run init      # one-time: eb init for the birddog EB application
sh/aws_run deploy     # create birddog-env if missing, (re-)apply env vars, deploy current code
sh/aws_run setenv       # (re-)apply environment variables without deploying
sh/aws_run status         # eb status
sh/aws_run logs            # fetch EB logs, copies the web server log into ./var
sh/aws_run terminate         # tear down the birddog-env environment
```

`deploy`/`setenv` push `BIRDDOG_*` and `GOOGLE_TRANSLATE_API_KEY` env vars to the environment (values are read from your shell, so export them first — they are intentionally not stored in the script). The deployed app talks to hosted NocoDB Cloud, selecting the target base via `BIRDDOG_NOCODB_ENV` (a key in `resources/db_config.json`). Pass `--stage` (default) or `--prod` before the command to choose it, e.g. `sh/aws_run --prod deploy`.

`.ebextensions/01-root-volume.config` bumps the EB instance root volume to 30GB for the Birddog environment.

---

## Location Processing Tools (experimental)

The `research/triage/locations` directory contains standalone, experimental tools for extracting and matching geographical locations from archival document descriptions. These are not part of the deployed Birddog service.

### 1. `read_all_locations.py`
- Builds a comprehensive location dictionary from population registers and administrative hierarchies
- Enables fuzzy matching of place names

### 2. `file_location.py`
- Combines location extraction, matching, and filtering
- Returns only locations at the smallest administrative level (e.g., villages)

### 3. `extract_location_from_descriptors.py`
- Uses an AI model (Qwen/Qwen2.5-7B-Instruct) to extract locations from text
- Requires a Hugging Face token or Modal deployment

### 4. `deploy_modal.py` (optional)
- Deploys the Qwen model via Modal.ai for high-availability processing
- Requires a Modal account and API key setup

### Example Usage

From a Python interpreter:

```python
from research.triage.locations.file_location import get_doc_location

# Get location IDs for a document (returns smallest administrative level matches)
location_ids = get_doc_location(document_id=406970, only_smallest_locations=True)
```

These tools currently rely on manual testing via `if __name__ == "__main__":` blocks in each script.

---

## Testing

Core application code is covered by a `unittest` suite under `test/` (database, service routes, watcher, wiki scraping, tracker, translation, etc.). Run it with:

```bash
python -m unittest discover test
```

Note: this project's virtualenv does not include `pytest` — use `unittest`, not `pytest`.

---

## License

MIT License
