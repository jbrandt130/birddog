# The code below uses the free Inference Client to query Qwen/Qwen2.5-7B-Instruct. We use Pydantic directly to enforce the JSON structure so the model returns only valid data.
import json
import os
from json import JSONDecodeError

from openai import OpenAI
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
    (cities, towns, villages) from the text. The model applies specific rules:
        - Strips adjectival suffixes (e.g., 'Skvirsky district' -> 'Skvir')
        - Resolves district/province names to their capital city
        - Removes settlement suffixes (village, town, city, etc.)
        - Extracts every single location mentioned, including those in parentheses

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

    system_prompt = (
        "You are a precise data extraction AI.\n"
        "Analyze the provided JSON array of text descriptors and identify geographical locations.\n"
        "CRITICAL EXTRACTION RULES:\n"
        "1. Extract specific city, town, or village names directly.\n"
        "2. Do NOT extract district or province names. Instead, resolve and extract ONLY the capital city of that district/province.\n"
        "3. STRIP ADJECTIVAL SUFFIXES: Convert adjectival regional names into their core capital town name forms "
        "(e.g., 'Skvirsky district' or 'Skvirsky' -> extract 'Skvir').\n"
        "4. STRIP SETTLEMENT SUFFIXES: Remove words like 'village', 'town', 'township', 'city', 'settlement', 'district', "
        "'province', 'county', 'region', 'oblast', 'gubernia', 'uezd', 'volost', 'selsoviet' from the extracted name. "
        "For example, 'Vovna village' -> 'Vovna', 'Kyiv city' -> 'Kyiv'.\n"
        "5. Extract EVERY SINGLE individual item mentioned. Do not omit any items from lists or descriptions."
        "If a descriptor contains multiple locations separated by commas, semicolons, or the word 'and', "
        "treat each as a separate location and return a separate entry for each.\n"
        "6. If a descriptor mentions an institution, check if a city or town is mentioned "
        "inside parentheses (e.g., in 'Central Archives... (Warsaw)', extract 'Warsaw').\n"
        "Extract ONLY the valid localized geographical place name, ignoring surrounding text.\n"
        "You must respond ONLY with a valid JSON object matching this schema:\n"
        f"{json.dumps(DocumentLocationsResponse.model_json_schema())}"
    )
    user_content = f"Analyze this list of descriptors: {json.dumps(descriptors)}"

    # Call the free Hugging Face API
    response = client.chat.completions.create(
        model="Qwen/Qwen2.5-7B-Instruct",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},  # Enforce valid JSON output
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

    if debug_print:
        msg = "Extracted locations: "
        for item in final_extracted:
            msg = f"{msg}{item.locations} "
        print(msg)

    # Convert to a list of strings, ensuring no None values.
    locations_as_strings = []
    for place_extraction in final_extracted:
        locations_as_strings.extend(place_extraction.locations)

    # dict.fromkeys removes duplicates while keeping the original list order
    unique_locations = list(dict.fromkeys(locations_as_strings))
    return unique_locations


if __name__ == "__main__":
    api_token = os.getenv("HF_TOKEN", "")

    list_of_descriptors = [
        "Case on Anna Grigorieva's claim against Franciszhin Shayts for 7 rubles 50 kopecks for service",
        "Justice of the Peace of the 1st precinct of the Kamianets-Podilskyi Judicial and Peace District, Kamianets-Podilskyi, Kamianets-Podilskyi district, Podilskyi province",
        "Justice of the Peace of the 1st District of the Kamyanets Judicial and Peace District, Kamyanets-Podilskyi, Kamyanets County, Podilsk Province",
        "State Archives of Khmelnytsky region. Funds of the pre-Soviet period",
        "Khmelnytskyy",
    ]

#    list_of_descriptors = [
#        "Białojezore, Kiev Governorate, Cherkasy County",
#        "Chapter XXV - Inventories of goods",
#        "Radziwill Archive",
#        "Central Archives of Historical Records (Warsaw) (AGAD)",
#        "Warszawa",
#    ]

    extracted_places = extract_locations(list_of_descriptors, api_token)
    print(extracted_places)
    # Expected Output: ['Białojezore', 'Warsaw', 'Warszawa']
