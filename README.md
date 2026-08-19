# Birddog with Location Processing Tools

Birddog is a web-based tool for navigating and translating Ukrainian archival documents, enhanced with location processing capabilities for geospatial context in archival research.

## Features

- Monitor updates to historical Ukrainian document pages
- Scrape metadata and revisions from government archives
- Generate and manage tracking spreadsheets
- Web UI for report browsing
- 📍 Advanced location processing (new)
  - Location matching against administrative hierarchies
  - AI-powered location extraction from text descriptions
  - Cyrillic text handling for Russian/Ukrainian place names
  - Filtering by administrative level (e.g., villages vs. district centers)

## Project Structure

```bash
birddog/
├── birddog/              # Core application code
├── templates/            # Jinja2 HTML templates (Bootstrap-based)
├── static/               # Static Birddog client assets
├── resources/            # Application data including archive lists
├── test/                 # Unit tests (in progress for location tools)
├── docs/                 # Project documentation
├── sh/                   # Shell scripts
├── notebooks/            # Jupyter notebooks
└── research/             # Experimental and research tools
    └── triage/           # Location processing pipeline
        ├── locations/
        │   ├── read_all_locations.py      # Location matching against population data
        │   ├── file_location.py          # Document location identification
        │   ├── extract_location_from_descriptors.py  # LLM-powered location extraction
        │   └── deploy_modal.py           # Modal deployment for Qwen model (optional)
```

---

## 🚀 Quickstart Guide

### ✅ Requirements

- Python 3.12+
- `git`
- Google Cloud Translate API key (for translation)
- [Optional] Jupyter Notebooks
- [Optional] Modal account (for deploying Qwen model)

### 📦 Install Dependencies

```bash
# Install core dependencies
pip install -r requirements.txt

# Install additional dependencies for location tools
pip install rapidfuzz modal  # Adds fuzzy matching and modal deployment capabilities
```

---

## 🔐 Setup

### 1. Environment Variables

Set these in your `.env` file or directly in your environment:

```bash
# Required for Flask session management
BIRDDOG_SECRET_KEY=your_secret_key_here

# Required for Google Cloud Translation
GOOGLE_TRANSLATE_API_KEY=your_google_api_key

# For AI-powered location extraction
HF_TOKEN=your_huggingface_token  # Free tier token works for basic use

# Optional: Modal deployment URL (if using custom deployment)
# MODAL_QWEN_URL=https://your-deployment.modal.run
```

### 2. Translation Settings

To disable Google Cloud Translation (useful for debugging):
```bash
BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE=False
BIRDDOG_TRANSLATION_DEBUG=True
```

---

## 📍 New: Location Processing Tools

The `research/triage/locations` directory contains specialized tools for extracting and matching geographical locations from archival document descriptions:

### 1. `read_all_locations.py`
- Builds a comprehensive location dictionary from population registers and administrative hierarchies
- Enables fuzzy matching of place names

### 2. `file_location.py`
- Combines location extraction, matching, and filtering
- Returns only locations at the smallest administrative level (e.g., villages)

### 3. `extract_location_from_descriptors.py`
- Uses an AI model (Qwen/Qwen2.5-7B-Instruct) to extract locations from text
- Requires Hugging Face token or Modal deployment

### 4. `deploy_modal.py` (optional)
- Deploys the Qwen model via Modal.ai for high-availability processing
- Requires Modal account and API key setup

---

## 🧪 Example Usage

From a Python interpreter:

```python
from research.triage.locations.file_location import get_doc_location

# Get location IDs for a document (returns smallest administrative level matches)
location_ids = get_doc_location(document_id=406970, only_smallest_locations=True)
```

---

## 🧪 Testing (Currently Manual)

The location tools currently rely on manual testing via `if __name__ == "__main__":` blocks in each script. Consider adding formal unit tests to the `test/` directory in future development.

---

## 📚 Next Steps

For developers:

1. Install dependencies including `pip install rapidfuzz modal`
2. Set required environment variables
3. Explore `research/triage/locations` tools in an interactive Python session
4. Configure Modal deployment (optional) for production use

---

## License

MIT License