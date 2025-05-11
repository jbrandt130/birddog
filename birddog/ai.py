# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

import os
import json
import time
from datetime import datetime

_backoff_until = 0  # epoch timestamp
_BACKOFF_DURATION = 4 * 60 * 60  # 4 hours in seconds

from birddog.utility import get_logger
_logger = get_logger()

# ---------- AI CLIENT ----------------------------

from abc import ABC, abstractmethod

class ServiceError(Exception):
    """Raised when a chat client service call fails."""
    def __init__(self, message, *, cause=None):
        super().__init__(message)
        self.cause = cause


class ChatClient(ABC):
    """Abstract base class for chat-based inference clients."""

    @abstractmethod
    def write(self, prompt: str, system_prompt: str = None) -> str:
        """
        Send a message to the chat service and return the response.

        Args:
            text (str): The input message to send.

        Returns:
            str: The response from the model.

        Raises:
            ServiceError: If the request fails or the service returns an error.
        """
        pass

from openai import OpenAI, RateLimitError

class OpenAIClient(ChatClient):
    """Client for OpenAI's chat API."""

    def __init__(self, model="gpt-4o", client=None):
        self._model = model
        self._client = client

    def _get_client(self):
        if not self._client:
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise ServiceError("Missing OPENAI_API_KEY")
            _logger.info(f'Creating OpenAI client')
            self._client = OpenAI(api_key=key)
        return self._client

    def write(self, prompt: str, system_prompt: str = None) -> str:
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            response = self._get_client().chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.2,
            )
            result = response.choices[0].message.content
            _logger.info(f'OpenAI chat result: {result}')
            return result
        except RateLimitError:
            raise ServiceError("Rate Limit")

import boto3

class AWSBedrockClient(ChatClient):
    """Client for AWS Bedrock chat-based models."""

    def __init__(self, model="anthropic.claude-3-sonnet-20240229-v1:0", client=None):
        self._model = model
        self._client = client

    def _get_client(self):
        if not self._client:
            self._client = boto3.session.Session().client("bedrock-runtime", region_name="us-east-1")
        return self._client

    def _message_body(self, prompt, system_prompt=None):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": [{"type": "text", "text": system_prompt}]})
        messages.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            #"max_tokens_to_sample": 1024,
            "temperature": 0.2,
            "messages": messages
            }

    def write(self, prompt: str, system_prompt: str = None) -> str:
        try:
            body = self._message_body(prompt, system_prompt)
            response = self._get_client().invoke_model(
                modelId=self._model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body))
            content = json.loads(response['body'].read())
            return content["content"][0]['text']
        except Exception as e:
            raise ServiceError("AWS Bedrock request failed", cause=e)

_ai_client = None

def _get_client():
    global _ai_client
    if not _ai_client:
        use_aws = True
        if use_aws:
            _logger.info(f'Creating AWS Bedrock client')
            _ai_client = AWSBedrockClient()            
        else:
            _logger.info(f'Creating OpenAI client')
            _ai_client = OpenAIClient()

    return _ai_client

# ---------- TABLE HEADER CLASSIFIER ----------------------------

def _form_table_column_classifier_prompt(headers, class_descriptions, sample_rows=None, max_rows=3, hints=None):
    classes_text = "\n".join(f"- {cat}: {desc}" for cat, desc in class_descriptions.items())
    headers_with_index = "\n".join(f"{i}: {h}" for i, h in enumerate(headers))

    prompt_parts = [
        "You are given a list of table column headers in Ukranian and their indices:",
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

    if hints:
        prompt_parts += [
            "",
            "Here are some additional hints to help:",
            hints
        ]

    prompt_parts += [
        "",
        "Rules:",
        "- Assign exactly one type to each header.",
        "- Use each type at most once, except for 'OTHER', which can be reused.",
        "- If a header does not clearly fit any type, classify it as 'OTHER'.",
        "",
        "Respond with a JSON list of strings (no objects), where the i-th element is the classification of the i-th header.",
        'Example: ["SEQUENCE", "DESCRIPTION", "OTHER", "DATE"]',
        "Include no other text in your response. Include only the JSON list of strings. The strings must be double-quoted."
    ]

    return "\n".join(prompt_parts)

def table_column_classifier(headers, class_descriptions, sample_rows=None, max_rows=3, hints=None):
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
    full_prompt = _form_table_column_classifier_prompt(
        headers, class_descriptions, sample_rows, max_rows, hints)

    system_prompt = "You are a multilingual assistant that classifies table headers based on user-defined categories and sample data."
    #result = _get_client().write(full_prompt, system_prompt=system_prompt)
    _logger.info(f'table_column_classifier prompt: {full_prompt}')
    result = _get_client().write(full_prompt)
    _logger.info(f'table_column_classifier result: {result}')
    parsed = json.loads(result)

    # Ensure it's a list of the right length
    if isinstance(parsed, list) and len(parsed) == len(headers):
        return parsed
    _logger.warning("GPT response was valid JSON but did not match expected structure. Using fallback.")
    raise ServiceError("Invalid Response")

def _normalize(labels, required_labels, other_label="OTHER"):
    required_set = set(required_labels)
    seen = set()
    normalized = []
    is_valid = True

    for label in labels:
        if label in required_set:
            if label in seen:
                normalized.append(other_label)
                is_valid = False # duplicated label
            else:
                seen.add(label)
                normalized.append(label)
        else:
            if label != other_label:
                is_valid = False # unrecognized label
            normalized.append(other_label)

    is_usable = seen == required_set
    is_valid = is_valid and is_usable  # collapse the final check

    return normalized, is_usable, is_valid

def _default_labels(page):
    return ["ID", "DESCRIPTION", "DATE"] + ["OTHER"] * (len(page.header) - 3)

_total_inferences = 0
_successful_inferences = 0

def classify_table_columns(page):
    global _backoff_until, _successful_inferences, _total_inferences
    classes = {
        "DATE": "A column indicating a single date or a date range",
        "DESCRIPTION": "A column containing a textual description of the item",
        "ID": "A unique row identifier, number, or code"
    }
    hints = "\n".join([
        "The ID column is always first.",
        ""])
    now = time.time()
    if now < _backoff_until:
        return _default_labels(page)

    max_rows = 3
    rows = [ [ item['text']['uk'] for item in row ] for row in page.children[:max_rows] ]
    try:
        _total_inferences += 1
        mapping = table_column_classifier(
            [col['uk'] for col in page.header], classes, sample_rows=rows, hints=hints)
        mapping, is_usable, is_valid = _normalize(mapping, classes.keys())
        if not is_usable:
            mapping = _default_labels(page)
        if is_valid:
            _successful_inferences += 1
        _logger.info(f'classify_table_columns result (valid={is_valid}): {mapping}')
        _logger.info(f'classify_table_columns success rate: {_successful_inferences}/{_total_inferences}'
                     f' ({100. * _successful_inferences / _total_inferences}%)')
        return {
            "mapping": mapping,
            "success": is_valid
            }
    except ServiceError as e:
        _logger.error(f"ServiceError: {e.cause}")
        _backoff_until = time.time() + _BACKOFF_DURATION
        _logger.warning("Rate limit hit. Backing off table classification until %s",
                           datetime.fromtimestamp(_backoff_until).isoformat())
        # unable to run inference - return fallback and let caller know
        return {
            "mapping": _default_labels(page),
            "success": False
            }
