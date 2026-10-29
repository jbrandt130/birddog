# The code below uses the free Inference Client to query Qwen/Qwen2.5-7B-Instruct. We use Pydantic directly to enforce the JSON structure so the model returns only valid data.
import builtins
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from json import JSONDecodeError
from pathlib import Path

import requests
from pydantic import BaseModel, Field, ValidationError

from birddog.fetch import fetch_url


# Rate-limit waits up to this long are sat through (inside fetch_url); longer
# ones (e.g. an exhausted tokens-per-day quota) raise birddog.fetch.RateLimitedError
# so the caller decides whether to wait, defer or give up.
_MAX_INLINE_RATE_LIMIT_WAIT_S = 120.0
# a batched completion of up to ~1000 output tokens can take well over
# fetch_url's default 10s
_COMPLETION_TIMEOUT_S = 90

# Completed responses are kept for a while, keyed on the exact request, so a
# batch that's retried after a rate limit reuses the calls it already paid
# for. Without this, near the daily token limit a batch needing several calls
# spends each freed slice of tokens on its first call, is refused on a later
# one, and starts over -- never finishing. The TTL covers retries spread
# over a day-limit wait; the size bounds memory (responses are ~1KB).
_RESPONSE_CACHE_TTL_S = 12 * 60 * 60
_RESPONSE_CACHE_SIZE = 256


# 1. Define the data structure to capture location hierarchy
class ExtractedLocation(BaseModel):
    locations: list[str] = Field(
        default_factory=list,
        description="All geographical locations found in the text."
    )


class DocumentLocationsResponse(BaseModel):
    extracted_locations: list[ExtractedLocation]


class PerDocExtraction(BaseModel):
    document_index: int = Field(
        ...,
        description="0-based index of the descriptor in the input array. Must match the position of the descriptor in the list."
    )
    locations: list[str] = Field(
        default_factory=list,
        description="All geographical locations found in the text."
    )


class BatchDocumentLocationsResponse(BaseModel):
    extracted_locations: list[PerDocExtraction]


# Load batch prompts from JSON file - must be done after BatchDocumentLocationsResponse is defined
_PROMPTS_PATH = r"resources/location_prompt.json"
with open(_PROMPTS_PATH, encoding="utf8") as f:
    _PROMPTS = json.load(f)

batch_system_prompt = _PROMPTS["batch_system_prompt"].replace(
    "{SCHEMA_PLACEHOLDER}",
    json.dumps(BatchDocumentLocationsResponse.model_json_schema()),
)

# Batched few-shot examples: input is a flat list of descriptors from N documents,
# output uses document_index to route each result back to the right document.
BATCH_FEW_SHOT_MESSAGES = _PROMPTS["batch_few_shot_messages"]


def _process_response(raw_json: dict, debug_print: bool = False) -> list[str]:
    """ Response processing logic for extract_locations_batched.

    Tries the batched schema first (PerDocExtraction with document_index), then
    falls back to the legacy single-doc schema (LocationExtraction without
    document_index) for backward compatibility.
    """
    # Try batched schema first
    try:
        parsed_data = BatchDocumentLocationsResponse(**raw_json)
    except (JSONDecodeError, ValidationError):
        # Fall back to legacy single-doc schema
        try:
            legacy = DocumentLocationsResponse(**raw_json)
            # Wrap in BatchDocumentLocationsResponse so downstream code is uniform
            parsed_data = BatchDocumentLocationsResponse(
                extracted_locations=[
                    PerDocExtraction(
                        document_index=0,
                        locations=item.locations,
                    )
                    for item in legacy.extracted_locations
                ]
            )
        except (JSONDecodeError, ValidationError) as e:
            print(f"parsing AI response: {e}")
            return []

    # Filter out entries without locations
    final_extracted = [
        item for item in parsed_data.extracted_locations
        if item.locations
    ]

    # Convert to a list of strings, ensuring no None values.
    locations_as_strings = []
    for place_extraction in final_extracted:
        locations_as_strings.extend(place_extraction.locations)

    # Deterministic cleanup: strip ordinal prefixes and common institution-related
    # words that the LLM sometimes leaves in (e.g., "2nd Lityn
    # District" -> "Lityn District"). This guards against the model
    # drifting from the rules in the system prompt.
    ordinal_pattern = re.compile(r"^\s*\d+(st|nd|rd|th)\s+", re.IGNORECASE)
    cleaned: list[str] = []
    for text in locations_as_strings:
        # Strip ordinal prefix (e.g., "2nd ", "1st ").
        text = ordinal_pattern.sub("", text)
        # Collapse runs of whitespace introduced by removals.
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cleaned.append(text)
    locations_as_strings = cleaned

    # builtins.dict.fromkeys removes duplicates while keeping the original list order
    unique_locations = list(builtins.dict.fromkeys(locations_as_strings))

    if debug_print:
        msg = "Extracted locations: "
        for item in unique_locations:
            msg = f"{msg}'{item}' "
        print(msg)

    return unique_locations


def check_and_trim_keywords(loc: str, keywords: list[str], suffix_only: bool) -> tuple[bool, str]:
    # Convert base location to lowercase for case-insensitive checking
    loc_lower = loc.lower()

    # Check keywords as prefixes
    if not suffix_only:
        for keyword in keywords:
            if loc_lower.startswith(keyword):
                trimmed = loc[len(keyword) + 1 :]
                return True, trimmed

    # Check keywords as suffixes
    for keyword in keywords:
        if loc_lower.endswith(keyword):
            trimmed = loc[: -(len(keyword) + 1)]
            return True, trimmed

    return False, loc

class LocationExtractor:
    """
    Main class that combines population and tree location data for fuzzy location matching.
    Handles location ID lookups using similarity scoring against multiple name variants.
    """

    def __init__(self, logger, provider: str = "modal"):
        """
        setting the model and provider link
        """
        self._logger = logger
        match provider:
            case "modal":
                self._provider_url = "https://ztatyan--qwen-inference-service-serve.modal.run/v1"
                self._provider_api_key = os.getenv("HF_TOKEN", "")
                self._model = "Qwen/Qwen2.5-7B-Instruct"
            case "groq":
                self._provider_url = "https://api.groq.com/openai/v1"
                self._provider_api_key = os.getenv("GROQ_LOCATION_KEY", "")
                self._model = "qwen/qwen3.8-27b"  # Updated to the current active free-tier ID
            case _:
                raise ValueError(f"Invalid provider {provider}")

        # request key -> (stored at, response); see _RESPONSE_CACHE_TTL_S
        self._response_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._response_cache_lock = threading.Lock()

    def _cache_key(self, messages: list[dict], max_tokens: int) -> str:
        request = json.dumps([self._provider_url, self._model, messages, max_tokens],
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(request.encode("utf-8")).hexdigest()

    def _cached_response(self, key: str) -> dict | None:
        with self._response_cache_lock:
            entry = self._response_cache.get(key)
            if entry is None:
                return None
            stored_at, response = entry
            if time.monotonic() - stored_at > _RESPONSE_CACHE_TTL_S:
                del self._response_cache[key]
                return None
            self._response_cache.move_to_end(key)
            return response

    def _cache_response(self, key: str, response: dict) -> None:
        with self._response_cache_lock:
            self._response_cache[key] = (time.monotonic(), response)
            self._response_cache.move_to_end(key)
            while len(self._response_cache) > _RESPONSE_CACHE_SIZE:
                self._response_cache.popitem(last=False)

    def _create_completion(self, messages: list[dict], max_tokens: int, num_docs: int) -> dict:
        """POSTs an OpenAI-compatible chat completion request and returns the
        response JSON.

        Goes through birddog.fetch, so calls share the per-host throttle (see the
        api.groq.com:api profile). Rate limits that clear within
        _MAX_INLINE_RATE_LIMIT_WAIT_S are waited out; longer ones raise
        RateLimitedError with the wait in retry_after. Other 4xx errors raise
        requests.HTTPError with the response text in the message.

        Logs the token usage the service reports for the call, so the daily
        token budget can be checked against real numbers.

        A response to an identical earlier request is reused from the cache
        (see _RESPONSE_CACHE_TTL_S) without calling the service."""
        key = self._cache_key(messages, max_tokens)
        cached = self._cached_response(key)
        if cached is not None:
            self._logger.info(f"location extraction usage: {num_docs} docs, cached response reused (0 tokens)")
            return cached
        response = fetch_url(
            f"{self._provider_url}/chat/completions",
            method="POST",
            send_json={
                "model": self._model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
                "temperature": 0.1,
            },
            headers={"Authorization": f"Bearer {self._provider_api_key}"},
            return_json=True,
            timeout=_COMPLETION_TIMEOUT_S,
            max_retry_after=_MAX_INLINE_RATE_LIMIT_WAIT_S,
        )
        self._log_usage(response, num_docs, max_tokens)
        self._cache_response(key, response)
        return response

    def _log_usage(self, response: dict, num_docs: int, max_tokens: int) -> None:
        usage = response.get("usage") if isinstance(response, dict) else None
        if not usage:
            self._logger.info(f"location extraction usage: {num_docs} docs, no usage reported")
            return
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = usage.get("total_tokens", prompt + completion)
        choices = response.get("choices") or [{}]
        self._logger.info(
            f"location extraction usage: {num_docs} docs, prompt={prompt} "
            f"completion={completion}/{max_tokens} total={total} tokens "
            f"({total / max(num_docs, 1):.0f}/doc), finish_reason={choices[0].get('finish_reason')}"
        )


    # Batched extraction: N descriptors in 1 API call
    def extract_locations_batched(self,
        batches: list[list[str]],          # list of descriptor lists, one per document
        batch_size: int | None = None     # if None, batches = all descriptors in 1 call
    ) -> list[list[str] | None]:
        """
        Extract geographical locations from many documents in batched API calls.

        Each element of `batches` is a list of descriptor strings for one document.
        The function sends at most `batch_size` documents per API call (default: all
        in one call). Returns a list of location lists, preserving order matching
        `batches`.

        IMPORTANT: This model has a 4096 total token context window. The function
        automatically caps batch_size to stay within context limits and reduces
        max_tokens to leave room for output.

        Returns:
            list[list[str] | None]: outer list indexed by document number, inner list is
            the extracted locations for that document (unique, cleaned, deduped).
            [] means the model found no locations; None means extraction failed for
            that document (unparseable/empty response, or the document was missing
            from the model's reply).

        Raises:
            birddog.fetch.RateLimitedError: the service is rate limited for longer
                than _MAX_INLINE_RATE_LIMIT_WAIT_S (e.g. a daily token quota).
            birddog.fetch.FetchUrlFailError / requests.HTTPError: the request failed.
        """
        if not batches:
            return []

        # If batch_size not set, default to all in one call
        num_batches = len(batches)
        if batch_size is None:
            batch_size = num_batches

        # Token budget plan for the 4096 context:
        # - Fixed overhead: ~700 tokens (system prompt + few-shot examples)
        # - User content: variable (depends on descriptor lengths)
        # - Output: must leave room
        #
        # We measure the user content after building it for each chunk, then size
        # max_tokens to fit. For Qwen2.5-7B-Instruct, the model context is 4096.

        # Conservative initial cap on batch size to avoid context overflow.
        # Each document needs enough output budget for its locations, even if the
        # model is verbose or repeats entries.
        MAX_BATCH_SIZE = 10
        batch_size = min(batch_size, MAX_BATCH_SIZE)

        all_results: list[list[str] | None] = [None] * num_batches

        # Process each batch chunk
        for start in range(0, num_batches, batch_size):
            chunk = batches[start:start + batch_size]

            # Build the user content: one block per document, all descriptors grouped
            # so the model produces exactly one result entry per document_index.
            doc_blocks = []
            for doc_idx, descr_list in enumerate(chunk):
                # Sort by length ascending to get consistent results when running several times
                descr_list = sorted(descr_list, key=len)
                descs = "\n".join(f"  - {json.dumps(d)}" for d in descr_list)
                doc_blocks.append(f"Document {doc_idx}:\n{descs}")
            user_content = (
                f"Analyze the following {len(chunk)} documents and extract "
                f"locations for each:\n" + "\n".join(doc_blocks)
            )

            # Compute max_tokens dynamically based on actual user content size.
            # We use a chars/2 token estimate (overestimate to avoid context overflow).
            # Worst case: English text = ~4 chars/token; code/JSON = ~3 chars/token.
            # Overestimating input is safe — it just leaves less room for output.
            # Output is ~25-50 tokens per doc (2-5 locations + JSON framing).
            input_tokens_estimate = (len(batch_system_prompt) + sum(len(m["content"])
                                      for m in BATCH_FEW_SHOT_MESSAGES if isinstance(m["content"], str)) +
                                      len(user_content)) // 2
            # Reserve at least 300 tokens for output, cap output at what's available.
            # Use 50 tokens/doc (very conservative) to handle verbose or redundant
            # model output without getting truncated mid-string.
            available_for_output = max(300, 4096 - input_tokens_estimate - 100)  # 100 token safety margin
            per_doc_output_tokens = 50
            total_max_tokens = min(available_for_output, len(chunk) * per_doc_output_tokens)

            # Hard safety cap: even if the estimate is wrong, ensure total tokens
            # (estimated input + requested output) never exceeds the 4096 limit.
            # Use chars/2 as worst-case input estimate for this cap.
            hard_cap = max(100, 4096 - (len(user_content) // 2) - 100)
            total_max_tokens = min(total_max_tokens, hard_cap)

            self._logger.info(f"[batch {start}: {len(chunk)} docs] "
                      f"input≈{input_tokens_estimate} tokens, "
                      f"max_tokens={total_max_tokens}")

            messages: list[dict] = [
                {"role": "system", "content": batch_system_prompt}
            ]
            messages.extend(BATCH_FEW_SHOT_MESSAGES)
            messages.append({"role": "user", "content": user_content})

            # Wrap the initial call in a retry loop to handle 400 BadRequestError
            # (context window overflow) by reducing max_tokens and retrying.
            # Rate limits and 5xx errors are handled by fetch_url; a rate limit
            # too long to wait out raises RateLimitedError, which propagates.
            response = None
            attempt = 0
            effective_max_tokens = total_max_tokens
            while attempt < 3:
                attempt += 1
                try:
                    response = self._create_completion(messages, effective_max_tokens, len(chunk))
                    break  # success — exit retry loop
                except requests.exceptions.HTTPError as e:
                    if "maximum context length" in str(e):
                        effective_max_tokens = max(50, effective_max_tokens // 2)
                        self._logger.error(f"  (context overflow — retrying with max_tokens={effective_max_tokens})")
                        continue
                    raise

            # Parse the batch response, with retry on truncation (finish_reason="length").
            # Every failure path below leaves the chunk's slots as None, so callers
            # can tell a failed extraction from a document with no locations.
            parsed = None
            content_str: str | None = None
            retry_count = 0
            max_retries = 3  # try a couple of times with increased tokens

            while retry_count < max_retries:
                retry_count += 1

                if response is None:
                    self._logger.error(f"No response object for batch starting at {start}")
                    break  # exit retry loop — nothing more to try
                choice = response["choices"][0]
                finish_reason = choice.get("finish_reason")
                try:
                    content_str = choice["message"].get("content")
                    if not content_str:
                        self._logger.error(f"Received empty content from AI response for batch starting at {start} "
                            f"(finish_reason={finish_reason})")
                        break  # exit retry loop — nothing more to try
                    raw_json = json.loads(content_str)
                except (JSONDecodeError, ValidationError) as e:
                    preview = (content_str or "")[:2000]
                    self._logger.error(f"parsing batched AI response (attempt {retry_count}): {e}\n"
                          f"  finish_reason={finish_reason}\n"
                          f"  content_preview: {preview!r}")
                    # If truncated, retry with more output budget
                    if finish_reason == "length" and retry_count < max_retries:
                        extra_tokens = len(chunk) * per_doc_output_tokens + 50
                        # Cap at 1000 to prevent the model from using the extra budget
                        # to repeat more before truncating again (it will produce the same
                        # output just longer, repeating the same location names).
                        new_total = min(total_max_tokens + extra_tokens,
                                        max(100, 4096 - input_tokens_estimate - 50),
                                        1000)
                        self._logger.error(f"  (truncated — retrying with max_tokens={new_total})")
                        response = self._create_completion(messages, new_total, len(chunk))
                        continue  # re-run the while loop with more tokens
                    # Otherwise (parse error or max_retries exhausted): give up
                    break  # exit retry loop

                # Successfully parsed JSON — validate against schema
                try:
                    parsed = BatchDocumentLocationsResponse(**raw_json)
                    break  # success — exit retry loop
                except ValidationError as e:
                    # Schema validation failed. Check if we were truncated; if so,
                    # retry with more tokens before giving up.
                    if finish_reason == "length" and retry_count < max_retries:
                        extra_tokens = len(chunk) * per_doc_output_tokens + 50
                        new_total = min(total_max_tokens + extra_tokens,
                                        max(100, 4096 - input_tokens_estimate - 50))
                        self._logger.error(f"  (schema error / truncated — retrying with max_tokens={new_total})")
                        response = self._create_completion(messages, new_total, len(chunk))
                        continue
                    self._logger.error(f"constructing BatchDocumentLocationsResponse (after {retry_count} retries): {e}")
                    break  # exit retry loop

            # Assign each document's locations to the correct result slot.
            # IMPORTANT: document_index is chunk-relative (0..len(chunk)-1).
            # We must map it to the global position via (start + chunk_index).
            ordinal_pattern = re.compile(r"^\s*\d+(st|nd|rd|th)\s+", re.IGNORECASE)

            # If the retry loop never produced a valid parsed response, leave the
            # chunk's slots as None (failed) and skip result assignment.
            if parsed is None:
                continue

            for item in parsed.extracted_locations:
                if item.document_index < 0 or item.document_index >= len(chunk):
                    self._logger.warning(f"document_index {item.document_index} out of range (max {len(chunk)-1})")
                    continue

                # Convert chunk-relative index → global index
                global_idx = start + item.document_index

                if not item.locations:
                    # Document had no locations — fill slot if not already filled
                    if 0 <= global_idx < num_batches and all_results[global_idx] is None:
                        all_results[global_idx] = []
                    continue

                # Clean locations for this document: ' of ', ordinals, dedupe
                cleaned = []
                for loc in item.locations:
                    loc = (loc.replace(" of ", " ") if " of " in loc else loc)
                    loc = ordinal_pattern.sub("", loc)
                    loc = re.sub(r"\s+", " ", loc).strip()
                    if loc:
                        cleaned.append(loc)

                if 0 <= global_idx < num_batches:
                    slot = all_results[global_idx]
                    if slot is None:
                        all_results[global_idx] = cleaned
                    else:
                        # Merge and dedupe
                        slot.extend(cleaned)
                        all_results[global_idx] = list(
                            builtins.dict.fromkeys(slot)
                        )
                else:
                    self._logger.warning(f"global_idx {global_idx} out of range (max {num_batches-1})")

        # A slot still None means extraction failed for that document: its chunk's
        # call failed, or the model left the document out of its reply. A document
        # with no descriptors has nothing to fail on, so it is [] either way.
        return [r if r is not None or batches[i] else [] for i, r in enumerate(all_results)]


    def locations_to_admin_units(self,
            locations: list[str], province_keywords: list[str], district_keywords: list[str],
            district_keywords_suffix_only: list[str], settlement_keywords: list[str]
            ) -> tuple[list[builtins.dict], set, set]:
        """
        Function: locations_to_admin_units

        Purpose: Converts raw location names from document descriptions into standardized administrative unit records
        with hierarchical levels.

        This function analyzes location names extracted from archival document descriptions
        and categorizes them according to their administrative rank (province, district, or settlement).
        It uses keyword-based pattern matching to determine the administrative level of each location name.

        Parameters:
            locations (list[str]): List of raw location names extracted from document descriptions.
                                  These names may include administrative suffixes like "province", "district",
                                  or settlement types, and are typically already processed by extract_locations_batched().
            province_keywords (list[str]): list of lowercase province keywords
            district_keywords (list[str]): list of lowercase district keywords
            district_keywords_suffix_only (list[str]): list of lowercase district keywords as suffixes only
            settlement_keywords (list[str]): list of lowercase settlement keywords

        Returns:
            tuple[list[builtins.dict], set, set]: A tuple containing:
                1. admin_units (list[builtins.dict]): A list of dictionaries where each dictionary represents an administrative unit
                                                     with the following structure:
                                                     {
                                                         "location" (str): The standardized location name with administrative
                                                                          suffix stripped (e.g., "kiev" instead of "kiev province"),
                                                         "administrative_level" (int): Hierarchical level where:
                                                          0 = settlement (village, town, city, etc.)
                                                          1 = district/county (sub-provincial administrative unit)
                                                          2 = province/governorate/oblast/voivodeship (provincial level)
                                                     }
                2. province_names (set): A set of standardized province-level location names (suffix stripped).
                3. district_names (set): A set of standardized district-level location names (suffix stripped).
        """
        admin_units = []
        province_names = set()
        district_names = set()
        for loc in locations:
            loc = loc.lower()
            # is it a province?
            (found, trimmed) = check_and_trim_keywords(loc, province_keywords, False)
            if found:
                province_names.add(trimmed)
                admin_units.append({"location": trimmed, "administrative_level": 2})
                self._logger.info(f"'{loc}' identified as location '{trimmed}', level 'province'")
            else:
                # is it a district?
                (found, trimmed) = check_and_trim_keywords(loc, district_keywords, False)
                if not found:
                    # try suffix only
                    (found, trimmed) = check_and_trim_keywords(loc, district_keywords_suffix_only, True)
                if found:
                    district_names.add(trimmed)
                    admin_units.append({"location": trimmed, "administrative_level": 1})
                    self._logger.info(f"Location '{loc}' matches '{trimmed}', level 'district'")
                else:
                    # it is a settlement
                    (found, trimmed) = check_and_trim_keywords(loc, settlement_keywords, False)
                    if found:
                        admin_units.append({"location": trimmed, "administrative_level": 0})
                        self._logger.info(f"'Location '{loc}' matches '{trimmed}', level 'settlement'")
                    else:
                        admin_units.append({"location": loc, "administrative_level": 0})
                        self._logger.info(f"No keywords in the settlement name '{loc}'")
        return admin_units, province_names, district_names