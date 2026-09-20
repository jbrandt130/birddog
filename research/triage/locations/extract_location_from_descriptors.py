# The code below uses the free Inference Client to query Qwen/Qwen2.5-7B-Instruct. We use Pydantic directly to enforce the JSON structure so the model returns only valid data.
import builtins
import json
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


# System prompt and few-shot examples for extract_locations_batched
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
    "2. Extract every DISTINCT location mentioned. Do not skip any, and do not\n"
    "   list duplicates — if 'Chyhyryn district' appears 5 times, list it ONCE.\n"
    "3. Retain settlement suffixes (e.g., 'village', 'town', 'district', 'province').\n"
    "4. If a trailing suffix applies to a list of places (e.g., 'A, B, and C counties'),\n"
    "   append the suffix to EACH individual location.\n"
    "5. Do not include institutions, roads, or non-geographical features.\n"
    "6. A descriptor may be a comma- or semicolon-delimited list of proper\n"
    "   names with little or no surrounding prose — treat each such name as a\n"
    "   separate geographical location.\n"
    "7. Return strictly valid JSON matching this schema:\n"
    f"{json.dumps(BatchDocumentLocationsResponse.model_json_schema())}"
)

# Batched few-shot examples: input is a flat list of descriptors from N documents,
# output uses document_index to route each result back to the right document.
BATCH_FEW_SHOT_MESSAGES: list[ChatCompletionMessageParam] = [
    {
        "role": "user",
        "content": (
            "Analyze these 2 documents and extract locations for each:\n"
            'Document 0: "Justice of the Peace of the 1st precinct of the Kamianets-Podilskyi Judicial and Peace District, Kamianets-Podilskyi, Kamianets-Podilskyi district, Podilskyi province"\n'
            'Document 1: "Chapter XXV - Inventories of goods"\n'
            'Document 2: "Revision tale colony Efingar in Kherson district"'
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "extracted_locations": [
                    {
                        "document_index": 0,
                        "has_location": True,
                        "locations": [
                            "Kamianets-Podilskyi",
                            "Kamianets-Podilskyi district",
                            "Podilskyi province",
                        ],
                    },
                    {"document_index": 1, "has_location": False, "locations": []},
                    {
                        "document_index": 2,
                        "has_location": True,
                        "locations": [
                            "Efingar",
                            "Kherson district",
                        ],
                    },
                ]
            }
        ),
    },
    {
        "role": "user",
        "content": (
            "Analyze these 2 documents and extract locations for each:\n"
            'Document 0: "Central Archives of Historical Records (Warsaw) (AGAD)"\n'
            'Document 1: "of Cherkasy, Chyhyryn, Kaniv counties"'
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "extracted_locations": [
                    {
                        "document_index": 0,
                        "has_location": True,
                        "locations": ["Warsaw"],
                    },
                    {
                        "document_index": 1,
                        "has_location": True,
                        "locations": [
                            "Cherkasy county",
                            "Chyhyryn county",
                            "Kaniv county",
                        ],
                    },
                ]
            }
        ),
    },
]


def _make_client(api_token: str) -> OpenAI:
    """Create an OpenAI-compatible API client for the Qwen inference service."""
    return OpenAI(
        base_url="https://ztatyan--qwen-inference-service-serve.modal.run/v1",
        api_key=api_token,
    )


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

        # Wrap the initial call in a retry loop to handle 400 BadRequestError
        # (context window overflow) by reducing max_tokens and retrying.
        response = None
        attempt = 0
        effective_max_tokens = total_max_tokens
        while attempt < 3:
            attempt += 1
            try:
                response = client.chat.completions.create(
                    model="Qwen/Qwen2.5-7B-Instruct",
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=effective_max_tokens,
                    temperature=0.1,
                )
                break  # success — exit retry loop
            except Exception as e:
                if "maximum context length" in str(e) and attempt < 3:
                    effective_max_tokens = max(50, effective_max_tokens // 2)
                    print(f"  (context overflow — retrying with max_tokens={effective_max_tokens})")
                    continue
                raise

        # Parse the batch response, with retry on truncation (finish_reason="length")
        parsed = None
        content_str: str | None = None
        retry_count = 0
        max_retries = 3  # try a couple of times with increased tokens

        while retry_count < max_retries:
            retry_count += 1

            try:
                if response is None:
                    print(f"Error: No response object for batch starting at {start}")
                    # Fill remaining with empty lists
                    for i in range(start, min(start + batch_size, len(batches))):
                        if all_results[i] is None:
                            all_results[i] = []
                    break  # exit retry loop — nothing more to try
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
                finish_reason = (
                    getattr(response.choices[0], "finish_reason", None)
                    if response is not None else None
                )
                print(f"Error parsing batched AI response (attempt {retry_count}): {e}\n"
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
                finish_reason = (
                    getattr(response.choices[0], "finish_reason", None)
                    if response is not None else None
                )
                if finish_reason == "length" and retry_count < max_retries:
                    extra_tokens = len(chunk) * per_doc_output_tokens + 50
                    new_total = min(total_max_tokens + extra_tokens,
                                    max(100, 4096 - input_tokens_estimate - 50))
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


def locations_to_admin_units(
        locations: list[str], province_keywords: list[str], district_keywords: list[str],
        district_keywords_suffix_only: list[str], settlement_keywords: list[str],
        debug_print:bool = False) -> tuple[list[builtins.dict], set, set]:
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
        debug_print (bool): If True, enables verbose console output showing the identification process,
                           including the original location name, trimmed name, and assigned administrative level.

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
            if debug_print:
                print(f"'{loc}' identified as location '{trimmed}', level 'province'")
        else:
            # is it a district?
            (found, trimmed) = check_and_trim_keywords(loc, district_keywords, False)
            if not found:
                # try suffix only
                (found, trimmed) = check_and_trim_keywords(loc, district_keywords_suffix_only, True)
            if found:
                district_names.add(trimmed)
                admin_units.append({"location": trimmed, "administrative_level": 1})
                if debug_print:
                    print(f"Location '{loc}' matches '{trimmed}', level 'district'")
            else:
                # it is a settlement
                (found, trimmed) = check_and_trim_keywords(loc, settlement_keywords, False)
                if found:
                    admin_units.append({"location": trimmed, "administrative_level": 0})
                    if debug_print:
                        print(f"'Location '{loc}' matches '{trimmed}', level 'settlement'")
                else:
                    admin_units.append({"location": loc, "administrative_level": 0})
                    if debug_print:
                        print(f"No keywords in the settlement name '{loc}'")
    return admin_units, province_names, district_names