# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from openai import OpenAI, RateLimitError
import os
import json
import time
from datetime import datetime

_backoff_until = 0  # epoch timestamp
_BACKOFF_DURATION = 4 * 60 * 60  # 4 hours in seconds

from birddog.utility import get_logger

_logger = get_logger()

_key = os.getenv("OPENAI_API_KEY")
_ai_client = None

def _get_client():
    global _ai_client
    if not _ai_client:
        _logger.info(f'Creating OpenAI client')
        _ai_client = OpenAI(api_key=_key) if _key else None
    return _ai_client

class ServiceError(Exception):
    pass

def table_column_classifier(headers, class_descriptions, sample_rows=None, max_rows=3):
    """
    Classify table headers into user-defined categories, returning a list aligned with the input.

    Enforces:
        - One class per header
        - At most one header per class (except 'Other')

    Args:
        headers (list of str): List of column headers (e.g., in Ukrainian).
        class_descriptions (dict): Mapping from class name to description.
        sample_rows (list of lists): Optional sample rows of the table.
        max_rows (int): Max number of sample rows to include in prompt.

    Returns:
        list of str: Classification for each header in order.
    """
    classes_text = "\n".join(f"- {cat}: {desc}" for cat, desc in class_descriptions.items())
    headers_with_index = "\n".join(f"{i}: {h}" for i, h in enumerate(headers))

    prompt_parts = [
        "You are given a list of table column headers and their indices:",
        headers_with_index,
        "",
        "Classify each header into one of the following types based on the descriptions below:",
        classes_text,
    ]

    if sample_rows:
        sample_text = "\n".join(
            f"Row {i+1}: {row}" for i, row in enumerate(sample_rows[:max_rows])
        )
        prompt_parts += [
            "",
            "Here are some sample rows of the table:",
            sample_text
        ]

    prompt_parts += [
        "",
        "Rules:",
        "- Assign exactly one type to each header.",
        "- Use each type at most once, except for 'Other', which can be reused.",
        "- If a header does not clearly fit any type, classify it as 'OTHER'.",
        "",
        "Respond with a JSON list of strings (no objects), where the i-th element is the classification of the i-th header.",
        'Example: ["SEQUENCE", "DESCRIPTION", "OTHER", "DATE"]',
        "Include no other text in your response. Include only the JSON list of strings. The strings must be double-quoted."
    ]

    full_prompt = "\n".join(prompt_parts)

    try:
        response = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a multilingual assistant that classifies table headers based on user-defined categories and sample data."},
                {"role": "user", "content": full_prompt}
            ],
            temperature=0.2,
        )

        result = response.choices[0].message.content
        _logger.info(f'table_column_classifier result: {result}')
        parsed = json.loads(response.choices[0].message.content)

        # Ensure it's a list of the right length
        if isinstance(parsed, list) and len(parsed) == len(headers):
            return parsed
        else:
            _logger.warning("⚠️ GPT response was valid JSON but did not match expected structure. Using fallback.")
    except RateLimitError:
        raise ServiceError()
    except Exception as e:
        _logger.warning(f"⚠️ Failed to parse GPT response: {type(e)}\n{e}\nUsing fallback.")
    return ["OTHER"] * len(headers)

def classify_table_columns(page):
    global _backoff_until
    classes = {
        "DATE": "A column indicating a single date or a date range",
        "DESCRIPTION": "A column containing a textual description of the item",
        "ID": "A unique row identifier, number, or code"
    }
    now = time.time()
    if now < _backoff_until:
        return ["ID", "DESCRIPTION", "DATE"] + ["OTHER"] * (len(page.header) - 3)

    max_rows = 3
    rows = [ [ item['text']['uk'] for item in row ] for row in page.children[:max_rows] ]
    try:
        return table_column_classifier([col['uk'] for col in page.header], classes, sample_rows=rows)
    except ServiceError:
        _backoff_until = time.time() + _BACKOFF_DURATION
        _logger.warning("Rate limit hit. Backing off table classification until %s",
                           datetime.fromtimestamp(_backoff_until).isoformat())
        return ["ID", "DESCRIPTION", "DATE"] + ["OTHER"] * (len(page.header) - 3)
