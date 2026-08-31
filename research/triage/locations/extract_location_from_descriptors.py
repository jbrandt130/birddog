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

    # Initialize the free client using Qwen/Qwen2.5-7B-Instruct
    # More powerful options:
    # 1) Qwen/Qwen2.5-14B-Instruct & Qwen/Qwen2.5-32B-Instruct:
    # Double to quadruple the capacity of the 7B model.
    # These handle complex reasoning and ordering tasks noticeably better than 7B.
    # Qwen2.5-7B-Instruct or Qwen2.5-14B-Instruct yield faster, more consistent response times with fewer rate limits or timeouts.
    # 2) Qwen/Qwen2.5-72B-Instruct: Alibaba’s flagship open-weights model in the 2.5 line.
    # It rivals top commercial closed-source models in general reasoning and geography/multilingual extraction tasks.
    # 3) mistralai/Mistral-Small-24B-Instruct: A solid middle-ground model with strong performance.

    # Initialize client pointing to your Modal deployment
    client = OpenAI(
        base_url="https://ztatyan--qwen-inference-service-serve.modal.run/v1",
        api_key=api_token,
    )

    # Define few-shot examples to teach the model trailing suffix distribution
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
    try:
        # Get the string content safely
        content_str = response.choices[0].message.content
        if not content_str:
            print("Error: Received empty or missing content from AI response.")
            return []

        raw_json = json.loads(content_str)
        parsed_data = DocumentLocationsResponse(**raw_json)
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


def check_and_trim_keywords(loc: str, keywords: list[str]) -> tuple[bool, str]:
    # check keywords as prefixes
    for keyword in keywords:
        if loc.startswith(keyword):
            trimmed = loc[len(keyword) + 1 :]
            return True, trimmed

    # check keywords as suffixes
    for keyword in keywords:
        if loc.endswith(keyword):
            trimmed = loc[: -(len(keyword) + 1)]
            return True, trimmed

    return False, loc


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
    province_keywords = ['province', 'provinces', 'governorate', 'oblast', 'gubernia', 'voivodeship',
                         'region', 'regions', 'republic']
    district_keywords = ['district', 'districts', 'county', 'counties', 'uezd', 'uyezd', 'volost', 'powiat', 'diocese']
    settlement_keywords = ['village', 'villages', 'town', 'towns', 'township', 'city', 'cities',
                           'settlement', 'selsoviet', 'precinct', 'precincts', 'Municipality']

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

