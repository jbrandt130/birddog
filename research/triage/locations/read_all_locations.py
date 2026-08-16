import math
import re

import pandas as pd
from rapidfuzz import distance


def normalize_name(text: str) -> str:
    """Removes all spaces, hyphens, and non-alphanumeric punctuation."""
    # Matches anything that is NOT a letter or a number and removes it
    return re.sub(r"[^a-zA-Z0-9]", "", text.lower())


def read_tree_locations(tree_file_path: str):
    """
    Reads and preprocesses location data from a tree locations Excel file.
    - Loads first sheet with location_id as strings
    - Drops empty/blank location IDs
    - Prepares data for location matching by organizing country, region, and district info
    """
    # Load the first sheet, forcing the location_id column to read as strings
    df = pd.read_excel(tree_file_path, sheet_name=0, dtype={"location_id": str})

    # Drop rows where location_id is missing or entirely blank spaces
    df = df.dropna(subset=["location_id"])
    df["location_id"] = df["location_id"].str.strip()
    df = df[df["location_id"] != ""]

    # Target dictionary fields
    fields = [
        "country",
        "region",
        "district",
        "location_name",
        "modern_country",
        "current_location",
        "in_modern_ukraine",
    ]

    result_dict = {}

    # Group the dataset by the string location_id
    for loc_id, group in df.groupby("location_id"):
        loc_data = {}

        for field in fields:
            if field in group.columns:
                # Clear empty entries, remove spaces, and find unique elements
                clean_series = group[field].dropna().astype(str).str.strip()
                unique_values = clean_series[clean_series != ""].unique().tolist()
                loc_data[field] = unique_values
            else:
                loc_data[field] = []

        result_dict[loc_id] = loc_data

    return result_dict


class LocationMatcher:
    """
    Main class that combines population and tree location data for fuzzy location matching.
    Handles location ID lookups using similarity scoring against multiple name variants.
    """

    def __init__(self, populations_file_path: str, tree_file_path: str, ukraine_only:bool=True):
        """
        Initializes with two data sources:
        - Populations file: Main location repository
        - Tree file: Administrative hierarchy information
        Optionally filters to Ukraine-only locations
        """
        self.location_name_dict = {}

        self._read_population_locations(populations_file_path)
        self._add_tree_locations(tree_file_path, ukraine_only)

    def _read_population_locations(self, populations_file_path: str):
        """
        Populates location_name_dict from the main population register Excel file populations_file_path.
        - Handles main names and alternative names
        - Standardizes formatting
        - Sets default administrative level
        """
        self.location_name_dict = {}

        # Load the Excel spreadsheet
        df = pd.read_excel(populations_file_path)

        # Iterate over each row in the dataframe to extract names
        for index, row in df.iterrows():
            try:
                # 1. Safely handle potential NaN or missing IDs
                if pd.isna(row["location_id"]):
                    # Skipping row
                    continue

                loc_id = str(row["location_id"])

                # Initialize a list starting with the primary name (column 'name')
                main_name = str(row["name"]).strip()
                names_list = [normalize_name(main_name)]

                # Check if 'alternate_names' column has a valid comma-separated string
                if pd.notna(row["alternate_names"]):
                    # Split by commas and strip any surrounding whitespace from each name, convert to lower case
                    alt_names = [
                        normalize_name(name)
                        for name in str(row["alternate_names"]).split(",")
                        if name.strip()
                    ]
                    names_list.extend(alt_names)

                # 2. Populate your dictionary safely
                self.location_name_dict[loc_id] = {
                    "all_names": names_list,
                    "main_name": main_name,
                    "country": str(row["country"]).strip(),
                    "administrative_level": 0,  # administrative_level values: 2 - province capital, 1 - district capital, 0 - other
                }

            except (ValueError, KeyError):
                # Skipping row
                continue


    def _add_tree_locations(self, tree_file_path: str, ukraine_only:bool=True):
        """
        Enriches location data from tree_file_path Excel file.
        - Updates administrative levels (province/district/city capitals)
        - Adds alternative location names from current_location field
        - Filters unwanted locations if ukraine_only=True
        """
        # Load the Excel spreadsheet
        tree_dict = read_tree_locations(tree_file_path)

        # Iterate over each row in the dataframe
        for loc_id, loc_features in tree_dict.items():
            try:
                if ukraine_only and "No" in loc_features["in_modern_ukraine"]:
                    # Skipping row
                    continue

                # define the location administrative level
                if any(item in loc_features["location_name"] for item in loc_features["region"]):
                    administrative_level = 2
                elif any(item in loc_features["location_name"] for item in loc_features["district"]):
                    administrative_level = 1
                else:
                    administrative_level = 0

                current_locations = loc_features["current_location"]
                alternative_location_names = []
                for current_location in current_locations:
                    """An alternative location name is the substring of current_location before the first comma.
                    Safely get the substring before the first comma.
                    Handles None, empty strings, and strings without commas."""
                    if not current_location or (isinstance(current_location, float) and math.isnan(current_location)):  # Checks if text is None or an empty string ""
                        continue
                    else:
                        alternative_location_names.append(current_location.split(",")[0].strip().lower())

                if loc_id in self.location_name_dict:
                    # this location was already found in iijg_populations_merged.xlsx, update it
                    self.location_name_dict[loc_id]["administrative_level"] = administrative_level
                    if alternative_location_names:
                        self.location_name_dict[loc_id]["all_names"].extend(alternative_location_names)
                        #delete duplicates
                        self.location_name_dict[loc_id]["all_names"] = list(set(self.location_name_dict[loc_id]["all_names"]))
                else:
                    # add this location
                    all_names = loc_features["location_name"]
                    all_names.extend(alternative_location_names)
                    if all_names:
                        all_names = [name.lower() for name in all_names]
                        all_names = list(set(all_names))
                    self.location_name_dict[loc_id] = {
                        "all_names": all_names,
                        "main_name": loc_features["location_name"][0],
                        "country": str(loc_features["modern_country"][0]).strip(),
                        "administrative_level": administrative_level,
                    }

            except (ValueError, KeyError):
                # Skipping row
                continue
        print("******Finished analyzing the tree******")

    def find_location_id(
        self, place_to_search: str, debug_print: bool = False
    ) -> int | None:
        """
        Finds the best matching location ID using Jaro-Winkler similarity scoring.
        - Normalizes and standardizes search term
        - Computes similarity scores against all name variants
        - Tracks the best matching location ID
        - Prints debug output if requested

        Returns:
            Location ID if match score >= threshold (88), else None
        """
        place_to_search_lower = normalize_name(place_to_search)

        # Set a threshold score (typically between 85 and 90 out of 100)
        threshold = 88 # 93 # 91
        max_score = 0
        best_id = 0
        for loc_id, data in self.location_name_dict.items():
            # Safely fetch the 'all_names' list from the current dictionary entry
            names = data.get("all_names", [])

            for name in names:
                score = distance.JaroWinkler.similarity(place_to_search_lower, name) * 100
                if score > max_score:
                    best_id = loc_id
                    max_score = score

        best_data = self.location_name_dict.get(best_id)
        if max_score >= threshold:
            if debug_print and  best_data:
                print(
                    f"Location '{place_to_search}' is identified as '{best_data.get('main_name', '')}', "
                    f"{best_data.get('country', '')}, location ID={best_id} with score: {max_score}")
            return best_id

        if debug_print and  best_data:
            print(f"No match found for '{place_to_search}'. Maximum score: {max_score}, "
                  f"best candidate {best_data.get('main_name', '')}")

        return None


if __name__ == "__main__":
    populations_file_path_ = "./research/triage/locations/iijg_populations_merged.xlsx"
    tree_path_ = "./research/triage/locations/jg_communities_tree.xlsx"
    matcher = LocationMatcher(populations_file_path_, tree_path_, True)

    # Access the encapsulated dataset via the class instance
    loc_id_1 = '-1055659'
    location = matcher.location_name_dict.get(loc_id_1)
    loc_name = location.get("main_name") if location else "Unknown"
    print(f"Location for id={loc_id_1}: {location}")
    if not matcher.find_location_id(loc_name, debug_print=True):
        print(f"Id for location {loc_name} not found")

    loc_name = "Monastyryska"
    if not matcher.find_location_id(loc_name, debug_print=True):
        print(f"Id for location {loc_name} not found")
