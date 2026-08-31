import re

import pandas as pd
from rapidfuzz import distance


def normalize_name(text: str) -> str:
    """Removes all spaces, hyphens, and non-alphanumeric punctuation while retaining Unicode letters."""
    # Matches anything that is NOT a Unicode word character (letter/digit) or an underscore,
    # plus the underscore itself if you want to strip it.
    return re.sub(r"[^\w']|_", "", text.lower())

class LocationMatcher:
    """
    Main class that combines population and tree location data for fuzzy location matching.
    Handles location ID lookups using similarity scoring against multiple name variants.
    """

    def __init__(self, communities_file_path: str):
        """
        Populates location_name_dict from the main population register Excel file populations_file_path.
        - Handles main names and alternative names
        - Standardizes formatting
        - Sets default administrative level
        """
        self.location_name_dict = {}
        self.names_with_location_ids = {}

        # Load the Excel spreadsheet
        df = pd.read_excel(communities_file_path)

        # Iterate over each row in the dataframe to extract names
        for index, row in df.iterrows():
            try:
                # 1. Safely handle potential NaN or missing IDs
                if pd.isna(row["location_id"]):
                    # Skipping row
                    continue

                loc_id = str(row["location_id"])

                # Initialize a list starting with the primary name (column 'name')
                main_name = str(row["modern_location_name"]).strip()
                normalized_main_name = normalize_name(main_name)

                # Check if 'alternate_names' column has a valid comma-separated string
                if pd.notna(row["alternate_names"]):
                    alt_names = str(row["alternate_names"])
                    # remove_brackets like "[Pol]"
                    alt_names = re.sub(r'\[[^\]]*\]', '', alt_names)
                    # Split by commas and strip any surrounding whitespace from each name, convert to lower case
                    alt_names = [normalize_name(name) for name in alt_names.split(",") if name.strip()]
                    for name in alt_names:
                        self.names_with_location_ids.setdefault(name, []).append(loc_id)
                else:
                    alt_names = []
                if normalized_main_name not in alt_names:
                    alt_names.append(normalized_main_name)
                    self.names_with_location_ids.setdefault(normalized_main_name, []).append(loc_id)

                # district names
                district_names = [str(row["c1900_district"]), str(row["c1930_district"]),
                                  str(row["c1950_district"]), str(row["c2000_district"])]
                district_names = {s.strip().lower() for s in district_names if s.strip() and s.strip().lower() != 'nan'}

                # province names
                province_names = [str(row["c1900_province"]), str(row["c1930_province"]),
                                  str(row["c1950_province"]), str(row["c2000_province"])]
                province_names = {s.strip().lower() for s in province_names if s.strip() and s.strip().lower() != 'nan'}

                # Set the administrative_level. Possible values:
                # 2 - province capital, 1 - district capital, 0 - other
                if province_names.isdisjoint(alt_names):
                    if district_names.isdisjoint(alt_names):
                        # a regular settlement
                        administrative_level = 0
                    else:
                        # district centre
                        administrative_level = 1
                else:
                    # province capital
                    administrative_level = 2

                # 2. Populate your dictionary safely
                self.location_name_dict[loc_id] = {
                    "main_name": main_name,
                    "district_names": district_names,
                    "province_names": province_names,
                    "administrative_level": administrative_level,
                }

            except (ValueError, KeyError):
                # Skipping the row
                continue


    def find_location_id(self, place_to_search: str, debug_print: bool = False) -> list[int]:
        """
        Finds the best matching location ID using Jaro-Winkler similarity scoring.
        - Normalizes and standardizes search term
        - Computes similarity scores against all name variants
        - Tracks the best matching location ID
        - Prints debug output if requested

        Returns:
            If match score < threshold (88) - empty list. Otherwise, the list of all location IDs featuring this name.
        """
        place_to_search_lower = normalize_name(place_to_search)

        # Set a threshold score (typically between 85 and 90 out of 100)
        threshold = 91 #88 # 93
        max_score = 0
        best_name = ""
        for loc_name in self.names_with_location_ids:
            score = distance.JaroWinkler.similarity(place_to_search_lower, loc_name) * 100
            if score > max_score:
                max_score = score
                best_name = loc_name

        if max_score < threshold:
            if debug_print:
                print(f"No match found for '{place_to_search}'. Maximum score: {max_score}, "
                      f"best candidate {best_name}")
            matching_loc_ids = []
        else:
            matching_loc_ids = self.names_with_location_ids.get(best_name, [])
            if debug_print:
                msg = f"Location '{place_to_search}' is identified with score {max_score} as one of these locations: "
                for loc_id in matching_loc_ids:
                    loc = self.location_name_dict.get(loc_id)
                    if loc:
                        msg = f"{msg} (loc_id={loc_id}, name={loc.get('main_name')}) "
                print(msg)
        return matching_loc_ids



if __name__ == "__main__":
    communities_file_path_ = "./research/triage/locations/jg_communities_data.xlsx"
    matcher = LocationMatcher(communities_file_path_)

    # Access the encapsulated dataset via the class instance
    loc_id_1 = '-1055659'
    location = matcher.location_name_dict.get(loc_id_1)
    loc_name_ = location.get("main_name") if location else "Unknown"
    print(f"Location for id={loc_id_1}: {location}")
    if not matcher.find_location_id(loc_name_, debug_print=True):
        print(f"Id for location {loc_name_} not found")

    loc_name_ = "Monastyryska"
    if not matcher.find_location_id(loc_name_, debug_print=True):
        print(f"Id for location {loc_name_} not found")
