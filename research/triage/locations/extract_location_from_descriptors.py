# The code below uses the free Inference Client to query Qwen/Qwen2.5-7B-Instruct. We use Pydantic directly to enforce the JSON structure so the model returns only valid data.
import builtins
import json
import os
import re
from json import JSONDecodeError

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field, ValidationError


# 1. Define the data structure to capture location hierarchy
class LocationExtraction(BaseModel):
    has_location: bool = Field(
        description="True if the text contains any geographical location, city, region, or country. False otherwise."
    )
    locations: list[str] = Field(
        default_factory=list,
        description="All geographical locations found in the text."
    )


class DocumentLocationsResponse(BaseModel):
    extracted_locations: list[LocationExtraction]


class PerDocExtraction(BaseModel):
    document_index: int = Field(
        ...,
        description="0-based index of the descriptor in the input array. Must match the position of the descriptor in the list."
    )
    has_location: bool = Field(
        description="True if the text contains any geographical location, city, region, or country. False otherwise."
    )
    locations: list[str] = Field(
        default_factory=list,
        description="All geographical locations found in the text."
    )


class BatchDocumentLocationsResponse(BaseModel):
    extracted_locations: list[PerDocExtraction]


# System prompt and few-shot examples shared by extract_locations and extract_locations_batched
system_prompt = (
    "You are a precise data extraction AI.\n"
    "Analyze the provided text array and extract every single geographical location mentioned.\n"
    "CRITICAL RULES:\n"
    "1. Extract EVERY individual mention separately. Do not skip any.\n"
    "2. Retain settlement suffixes (e.g., 'village', 'town', 'district', 'province').\n"
    "3. If a trailing suffix applies to a list of places (e.g., 'A, B, and C counties'), append the suffix to EACH individual location.\n"
    "4. Do not include institutions, roads, or non-geographical features.\n"
    "5. Return strictly valid JSON matching this schema:\n"
    f"{json.dumps(DocumentLocationsResponse.model_json_schema())}"
)

# Batched system prompt: requires document_index in each PerDocExtraction
batch_system_prompt = (
    "You are a precise data extraction AI.\n"
    "You are given MULTIPLE documents. Each document has one or more descriptor strings.\n"
    "Extract every single geographical location mentioned in EACH document, then produce\n"
    "EXACTLY ONE result entry per document_index (0-based). Do NOT produce multiple\n"
    "entries for the same document_index — combine all locations found in a document into\n"
    "its single result entry.\n"
    "CRITICAL RULES:\n"
    "1. Output EXACTLY one 'PerDocExtraction' entry per document_index (0, 1, 2, ...).\n"
    "   If a document has no locations, output has_location=false and locations=[] for it.\n"
    "2. Extract EVERY individual mention separately. Do not skip any.\n"
    "3. Retain settlement suffixes (e.g., 'village', 'town', 'district', 'province').\n"
    "4. If a trailing suffix applies to a list of places (e.g., 'A, B, and C counties'),\n"
    "   append the suffix to EACH individual location.\n"
    "5. Do not include institutions, roads, or non-geographical features.\n"
    "6. Return strictly valid JSON matching this schema:\n"
    f"{json.dumps(BatchDocumentLocationsResponse.model_json_schema())}"
)

FEW_SHOT_MESSAGES: list[ChatCompletionMessageParam] = [
    {
        "role": "user",
        "content": "Analyze this list of descriptors: [\"case is based on claim of official of Kholm, Velikoluka, Zolotonosha, Kremenchuk districts\"]"
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "extracted_locations": [
                {
                    "has_location": True,
                    "locations": [
                        "Kholm district",
                        "Velikoluka district",
                        "Zolotonosha district",
                        "Kremenchuk district"
                    ]
                }
            ]
        })
    },
    {
        "role": "user",
        "content": "Analyze this list of descriptors: [\"of Cherkasy, Chyhyryn, Kaniv counties\"]"
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "extracted_locations": [
                {
                    "has_location": True,
                    "locations": [
                        "Cherkasy county",
                        "Chyhyryn county",
                        "Kaniv county"
                    ]
                }
            ]
        })
    },
    {
        "role": "user",
        "content": "Analyze this list of descriptors: [\"of resolutions bishops Bohodukhiv, Chuhuiv. Clerical of Izium, Kupiansk counties.\"]"
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "extracted_locations": [
                {
                    "has_location": True,
                    "locations": [
                        "Bohodukhiv",
                        "Chuhuiv",
                        "Izium county",
                        "Kupiansk county"
                    ]
                }
            ]
        })
    }
]


# Batched few-shot examples: input is a flat list of descriptors from N documents,
# output uses document_index to route each result back to the right document.
BATCH_FEW_SHOT_MESSAGES: list[ChatCompletionMessageParam] = [
    {
        "role": "user",
        "content": (
            "Analyze these 2 documents and extract locations for each:\n"
            'Document 0: "Justice of the Peace of the 1st precinct of the Kamianets-Podilskyi Judicial and Peace District, Kamianets-Podilskyi, Kamianets-Podilskyi district, Podilskyi province"\n'
            'Document 1: "Chapter XXV - Inventories of goods"'
        )
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "extracted_locations": [
                {
                    "document_index": 0,
                    "has_location": True,
                    "locations": [
                        "Kamianets-Podilskyi",
                        "Kamianets-Podilskyi district",
                        "Podilskyi province"
                    ]
                },
                {
                    "document_index": 1,
                    "has_location": False,
                    "locations": []
                }
            ]
        })
    },
    {
        "role": "user",
        "content": (
            "Analyze these 2 documents and extract locations for each:\n"
            'Document 0: "Central Archives of Historical Records (Warsaw) (AGAD)"\n'
            'Document 1: "of Cherkasy, Chyhyryn, Kaniv counties"'
        )
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "extracted_locations": [
                {
                    "document_index": 0,
                    "has_location": True,
                    "locations": ["Warsaw"]
                },
                {
                    "document_index": 1,
                    "has_location": True,
                    "locations": [
                        "Cherkasy county",
                        "Chyhyryn county",
                        "Kaniv county"
                    ]
                }
            ]
        })
    }
]


def _make_client(api_token: str) -> OpenAI:
    """Create an OpenAI-compatible API client for the Qwen inference service."""
    return OpenAI(
        base_url="https://ztatyan--qwen-inference-service-serve.modal.run/v1",
        api_key=api_token,
    )


def _process_response(raw_json: dict, debug_print: bool = False) -> list[str]:
    """Shared response processing logic for extract_locations and extract_locations_batched.

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
                        has_location=item.has_location,
                        locations=item.locations,
                    )
                    for item in legacy.extracted_locations
                ]
            )
        except (JSONDecodeError, ValidationError) as e:
            print(f"Error parsing AI response: {e}")
            return []

    # Filter out entries without locations
    final_extracted = [
        item for item in parsed_data.extracted_locations
        if item.has_location and item.locations
    ]

    # Convert to a list of strings, ensuring no None values.
    locations_as_strings = []
    for place_extraction in final_extracted:
        locations_as_strings.extend(place_extraction.locations)

    # get rid of the substrings ' of '
    locations_as_strings = [text.replace(" of ", " ") if " of " in text else text for text in locations_as_strings]

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


# 2. Define the extraction function
def extract_locations(descriptors: list[str], api_token: str, debug_print:bool = False) -> list[str]:
    """Extract geographical locations from text descriptors using an AI model.

    Sends a list of descriptor strings to the Qwen/Qwen2.5-7B-Instruct model
    via the OpenAI-compatible API and extracts geographical location mentions
    (cities, towns, villages) from the text.

    Args:
        descriptors: List of text descriptors to analyze for locations.
        api_token: API token for the inference service (Hugging Face or Modal).
        debug_print: If True, prints extracted locations for debugging.

    Returns:
        List of unique, processed geographical location names as strings.
    """
    if not descriptors:
        return []

    client = _make_client(api_token)

    user_content = f"Analyze this list of descriptors: {json.dumps(descriptors)}"

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt}
    ]
    messages.extend(FEW_SHOT_MESSAGES)
    messages.append({"role": "user", "content": user_content})

    response = client.chat.completions.create(
        model="Qwen/Qwen2.5-7B-Instruct",
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=1000,
        temperature=0.1,
    )

    # Parse and validate the response
    content_str: str | None = None
    try:
        # Get the string content safely
        content_str = response.choices[0].message.content
        if not content_str:
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            print(f"Error: Received empty or missing content from AI response. "
                  f"finish_reason={finish_reason}")
            return []
        raw_json = json.loads(content_str)
    except (JSONDecodeError, ValidationError) as e:
        preview = (content_str or "")[:2000]
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        print(f"Error parsing AI response: {e}\n"
              f"  finish_reason={finish_reason}\n"
              f"  content_preview: {preview!r}")
        return []

    extracted = _process_response(raw_json, debug_print)
    return extracted


def check_and_trim_keywords(loc: str, keywords: list[str]) -> tuple[bool, str]:
    # Convert base location to lowercase for case-insensitive checking
    loc_lower = loc.lower()

    # Check keywords as prefixes
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


# 3. Batched extraction: N descriptors in 1 API call
def extract_locations_batched(
    batches: list[list[str]],          # list of descriptor lists, one per document
    api_token: str,
    batch_size: int | None = None,     # if None, batches = all descriptors in 1 call
    debug_print: bool = False,
) -> list[list[str]]:
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
        list[list[str]]: outer list indexed by document number, inner list is
        the extracted locations for that document (unique, cleaned, deduped).
    """
    if not batches:
        return []

    # If batch_size not set, default to all in one call
    if batch_size is None:
        batch_size = len(batches)

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

    all_results: list[list[str] | None] = [None] * len(batches)

    # Process each batch chunk
    for start in range(0, len(batches), batch_size):
        chunk = batches[start:start + batch_size]

        # Build the user content: one block per document, all descriptors grouped
        # so the model produces exactly one result entry per document_index.
        doc_blocks = []
        for doc_idx, descr_list in enumerate(chunk):
            descs = "\n".join(f"  - {json.dumps(d)}" for d in descr_list)
            doc_blocks.append(f"Document {doc_idx}:\n{descs}")
        user_content = (
            f"Analyze the following {len(chunk)} documents and extract "
            f"locations for each:\n" + "\n".join(doc_blocks)
        )

        # Compute max_tokens dynamically based on actual user content size.
        # We use a chars/3.5 token estimate (closer to typical transformer
        # behavior than chars/4). Worst case: English text = ~4 chars/token;
        # code/JSON = ~3 chars/token. Use 3.5 as a safe middle ground.
        # Output is ~25-50 tokens per doc (2-5 locations + JSON framing).
        input_tokens_estimate = (len(batch_system_prompt) + sum(len(m["content"])
                                  for m in BATCH_FEW_SHOT_MESSAGES if isinstance(m["content"], str)) +
                                  len(user_content)) // 3
        # Reserve at least 300 tokens for output, cap output at what's available.
        # Use 150 tokens/doc to handle verbose or redundant model output
        # without getting truncated mid-string.
        available_for_output = max(300, 4096 - input_tokens_estimate - 50)  # 50 token safety margin
        per_doc_output_tokens = 150  # generous allowance
        total_max_tokens = min(available_for_output, len(chunk) * per_doc_output_tokens)

        if debug_print:
            print(f"[batch {start}: {len(chunk)} docs] "
                  f"input≈{input_tokens_estimate} tokens, "
                  f"max_tokens={total_max_tokens}")

        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": batch_system_prompt}
        ]
        messages.extend(BATCH_FEW_SHOT_MESSAGES)
        messages.append({"role": "user", "content": user_content})

        client = _make_client(api_token)
        response = client.chat.completions.create(
            model="Qwen/Qwen2.5-7B-Instruct",
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=total_max_tokens,
            temperature=0.1,
        )

        # Parse the batch response, with retry on truncation (finish_reason="length")
        parsed = None
        content_str: str | None = None
        retry_count = 0
        max_retries = 3  # try a couple of times with increased tokens

        while retry_count < max_retries:
            retry_count += 1

            try:
                content_str = response.choices[0].message.content
                if not content_str:
                    print(f"Error: Received empty content from AI response for batch starting at {start} "
                          f"(finish_reason={response.choices[0].finish_reason})")
                    # Fill remaining with empty lists
                    for i in range(start, min(start + batch_size, len(batches))):
                        if all_results[i] is None:
                            all_results[i] = []
                    break  # exit retry loop — nothing more to try
                raw_json = json.loads(content_str)
            except (JSONDecodeError, ValidationError) as e:
                preview = (content_str or "")[:2000]
                finish_reason = getattr(response.choices[0], "finish_reason", None)
                print(f"Error parsing batched AI response (attempt {retry_count}): {e}\n"
                      f"  finish_reason={finish_reason}\n"
                      f"  content_preview: {preview!r}")
                # If truncated, retry with more output budget
                if finish_reason == "length" and retry_count < max_retries:
                    extra_tokens = len(chunk) * per_doc_output_tokens + 50
                    new_total = min(total_max_tokens + extra_tokens,
                                    4096 - input_tokens_estimate - 50)
                    print(f"  (truncated — retrying with max_tokens={new_total})")
                    response = client.chat.completions.create(
                        model="Qwen/Qwen2.5-7B-Instruct",
                        messages=messages,
                        response_format={"type": "json_object"},
                        max_tokens=new_total,
                        temperature=0.1,
                    )
                    continue  # re-run the while loop with more tokens
                # Otherwise (parse error or max_retries exhausted): give up
                for i in range(start, min(start + batch_size, len(batches))):
                    if all_results[i] is None:
                        all_results[i] = []
                break  # exit retry loop

            # Successfully parsed JSON — validate against schema
            try:
                parsed = BatchDocumentLocationsResponse(**raw_json)
                break  # success — exit retry loop
            except ValidationError as e:
                # Schema validation failed. Check if we were truncated; if so,
                # retry with more tokens before giving up.
                finish_reason = getattr(response.choices[0], "finish_reason", None)
                if finish_reason == "length" and retry_count < max_retries:
                    extra_tokens = len(chunk) * per_doc_output_tokens + 50
                    new_total = min(total_max_tokens + extra_tokens,
                                    4096 - input_tokens_estimate - 50)
                    print(f"  (schema error / truncated — retrying with max_tokens={new_total})")
                    response = client.chat.completions.create(
                        model="Qwen/Qwen2.5-7B-Instruct",
                        messages=messages,
                        response_format={"type": "json_object"},
                        max_tokens=new_total,
                        temperature=0.1,
                    )
                    continue
                print(f"Error constructing BatchDocumentLocationsResponse (after {retry_count} retries): {e}")
                for i in range(start, min(start + batch_size, len(batches))):
                    if all_results[i] is None:
                        all_results[i] = []
                break  # exit retry loop

        # Assign each document's locations to the correct result slot.
        # IMPORTANT: document_index is chunk-relative (0..len(chunk)-1).
        # We must map it to the global position via (start + chunk_index).
        ordinal_pattern = re.compile(r"^\s*\d+(st|nd|rd|th)\s+", re.IGNORECASE)

        # If the retry loop never produced a valid parsed response, fill the
        # remaining slots with empty lists and skip result assignment.
        if parsed is None:
            for i in range(start, min(start + batch_size, len(batches))):
                if all_results[i] is None:
                    all_results[i] = []
            continue

        for item in parsed.extracted_locations:
            # Convert chunk-relative index → global index
            global_idx = start + item.document_index

            if not item.has_location or not item.locations:
                # Document had no locations — fill slot if not already filled
                if 0 <= global_idx < len(batches) and all_results[global_idx] is None:
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

            if 0 <= global_idx < len(batches):
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
                print(f"Warning: global_idx {global_idx} out of range (max {len(batches)-1})")

    # Ensure all slots are filled (in case some batches failed), and return
    # as list[list[str]] — pyrefly can't track the in-place None→[] fill above.
    return [r if r is not None else [] for r in all_results]


def locations_to_admin_units(locations: list[str], debug_print:bool = False) -> tuple[list[builtins.dict], set, set]:
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
                              or settlement types, and are typically already processed by extract_locations().
        debug_print (bool): If True, enables verbose console output showing the identification process,
                           including the original location name, trimmed name, and assigned administrative level.

    Returns:
        list[builtins.dict]: A list of dictionaries where each dictionary represents an administrative unit
                             with the following structure:
                             {
                                 "location" (str): The standardized location name with administrative
                                                  suffix stripped (e.g., "kiev" instead of "kiev province"),
                                 "administrative_level" (int): Hierarchical level where:
                                  0 = settlement (village, town, city, etc.)
                                  1 = district/county (sub-provincial administrative unit)
                                  2 = province/governorate/oblast/voivodeship (provincial level)
                             }
    """
    # all these must be lowercase
    province_keywords = ['governorate', 'gubernia', 'oblast', 'province', 'provinces',
                         'region', 'regions', 'republic', 'voivodeship']
    district_keywords = ['district', 'districts', 'county', 'counties', 'uezd', 'uyezd', 'volost', 'powiat', 'diocese']
    settlement_keywords = ['village', 'villages', 'town', 'towns', 'township', 'city', 'cities',
                           'settlement', 'selsoviet', 'precinct', 'precincts', 'municipality', 'mr.']

    admin_units = []
    province_names = set()
    district_names = set()
    for loc in locations:
        loc = loc.lower()
        # is it a province?
        (found, trimmed) = check_and_trim_keywords(loc, province_keywords)
        if found:
            province_names.add(trimmed)
            admin_units.append({"location": trimmed, "administrative_level": 2})
            if debug_print:
                print(f"'{loc}' identified as location '{trimmed}', level 'province'")
        else:
            # is it a district?
            (found, trimmed) = check_and_trim_keywords(loc, district_keywords)
            if found:
                district_names.add(trimmed)
                admin_units.append({"location": trimmed, "administrative_level": 1})
                if debug_print:
                    print(f"Location '{loc}' matches '{trimmed}', level 'district'")
            else:
                # it is a settlement
                (found, trimmed) = check_and_trim_keywords(loc, settlement_keywords)
                if found:
                    admin_units.append({"location": trimmed, "administrative_level": 0})
                    if debug_print:
                        print(f"'Location '{loc}' matches '{trimmed}', level 'settlement'")
                else:
                    admin_units.append({"location": loc, "administrative_level": 0})
                    if debug_print:
                        print(f"No keywords in the settlement name '{loc}'")
    return admin_units, province_names, district_names


if __name__ == "__main__":
    api_token_ = os.getenv("HF_TOKEN", "")

    list_of_descriptors = [
        "Case on Anna Grigorieva's claim against Franciszhin Shayts for 7 rubles 50 kopecks for service",
        "Justice of the Peace of the 1st precinct of the Kamianets-Podilskyi Judicial and Peace District, Kamianets-Podilskyi, Kamianets-Podilskyi district, Podilskyi province",
        "Justice of the Peace of the 1st District of the Kamyanets Judicial and Peace District, Kamyanets-Podilskyi, Kamyanets County, Podilsk Province",
        "State Archives of Khmelnytsky region. Funds of the pre-Soviet period",
        "Khmelnytskyy",
        "Białojezore, Kiev Governorate, Cherkasy County",
        "Chapter XXV - Inventories of goods",
        "Radziwill Archive",
        "Central Archives of Historical Records (Warsaw) (AGAD)",
        "Warszawa",
    ]

    extracted_places = extract_locations(list_of_descriptors, api_token_)

    print(locations_to_admin_units(extracted_places))
    # Expected Output: [{'location': 'kamianets-podilskyi', 'administrative_level': 0}, {'location': 'kamianets-podilskyi', 'administrative_level': 1}, {'location': 'podilskyi', 'administrative_level': 2}, {'location': 'kamyanets', 'administrative_level': 0}, {'location': 'kamyanets', 'administrative_level': 1}, {'location': 'podilsk', 'administrative_level': 2}, {'location': 'khmelnytsky', 'administrative_level': 2}, {'location': 'białojezore', 'administrative_level': 0}, {'location': 'kiev', 'administrative_level': 2}, {'location': 'cherkasy', 'administrative_level': 1}, {'location': 'warsaw', 'administrative_level': 0}]

