import re

from rapidfuzz import distance


def _get_str_from_record(record, key):
    """Extract a string value from a NocoDB record dict, handling None by returning 'nan' to match pandas str(NaN) behavior."""
    val = record.get(key)
    if val is None:
        return "nan"
    return str(val)


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

    def __init__(self, db, regions_2_locations: dict,
                 province_capitals: dict[str, list[dict]], logger):
        """
        Populates location_name_dict from the NocoDB Locations table.
        - Handles main names and alternative names
        - Standardizes formatting
        - Sets default administrative level
        """
        self._logger = logger
        self._province_capital_ids = {
            place_name: [loc["location_id"] for loc in province_capitals[place_name]]
            for place_name in province_capitals
        }

        self.location_name_dict = {}
        self.names_with_location_ids = {}

        # Load records from the Locations table in NocoDB
        records = db.scan_all("Locations")

        # Iterate over each record to extract names
        for row in records:
            try:
                # 1. Safely handle potential missing location_id
                loc_id_val = row.get("location_id")
                if loc_id_val is None:
                    # Skipping row
                    continue

                loc_id = str(loc_id_val)

                # Initialize a list starting with the primary name (column 'modern_location_name')
                main_name = str(row["modern_location_name"]).strip()
                normalized_main_name = normalize_name(main_name)

                # Check if 'alternate_names' column has a valid value
                alt_names_val = row.get("alternate_names")
                if alt_names_val is not None:
                    alt_names = str(alt_names_val)
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

                # district names using the helper function
                district_names = [_get_str_from_record(row, "c1900_district"),
                                  _get_str_from_record(row, "c1930_district"),
                                  _get_str_from_record(row, "c1950_district"),
                                  _get_str_from_record(row, "c2000_district")]
                district_names = {s.strip().lower() for s in district_names if s.strip() and s.strip().lower() != 'nan'}

                # province names
                c1900_province = _get_str_from_record(row, "c1900_province")
                province_cols = [c1900_province,
                                  _get_str_from_record(row, "c1930_province"),
                                  _get_str_from_record(row, "c1950_province"),
                                  _get_str_from_record(row, "c2000_province")]
                province_names = province_cols.copy()
                if c1900_province in regions_2_locations:
                    for item in regions_2_locations[c1900_province]:
                        province_names.append(item["location"])
                curr_province_capital_ids = {
                        loc_id
                        for name in province_names
                        if name in self._province_capital_ids
                        for loc_id in self._province_capital_ids[name]
                    }

                # Set also the province capitals IDs per column - 4 items
                province_capital_ids_array = [set(), set(), set(), set()]
                for col_idx in range(4):
                    name = province_cols[col_idx]
                    province_capital_ids_array[col_idx] = {loc_id for loc_id in self._province_capital_ids.get(name, [])}
                if province_cols[0] in regions_2_locations:
                    province_capital_ids_array[0] |= {prov["location_id"] for prov in regions_2_locations[province_cols[0]]}
                #delete empty sets
                province_capital_ids_array = [item for item in province_capital_ids_array if item]
                # Convert to frozenset to deduplicate, then convert back to regular sets
                province_capital_ids_array = [set(fs) for fs in {frozenset(s) for s in province_capital_ids_array}]
                province_capital_ids_array = sorted(province_capital_ids_array, key=len)

                # Extract all keys matching the values
                province_names = {name.strip().lower() for name in self._province_capital_ids
                    if not set(self._province_capital_ids[name]).isdisjoint(curr_province_capital_ids) and
                      name.strip() and name.strip().lower() != 'nan'}

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
                    "administrative_level": administrative_level,
                    "main_name": main_name,
                    "district_names": district_names,
                    "province_names": province_names,
                    "province_capital_ids": curr_province_capital_ids,
                    "province_capital_ids_array": province_capital_ids_array
                }

            except (ValueError, KeyError):
                # Skipping the row
                continue

    def find_location_id(self, place_to_search: str) -> list[str]:
        """
        Finds the best matching location ID using Jaro-Winkler similarity scoring.
        - Normalizes and standardizes search term
        - Computes similarity scores against all name variants
        - Tracks the best matching location ID

        Returns:
            If match score < threshold (88) - empty list. Otherwise, the list of all location IDs featuring this name.
        """
        place_to_search_lower = normalize_name(place_to_search)

        # Set a threshold score (typically between 85 and 90 out of 100)
        threshold = 91 #88 # 93
        max_score = 0
        seen: set[str] = set()
        matching_locs_with_scores: list[tuple[str, float]] = []
        best_name = ""
        for loc_name in self.names_with_location_ids:
            score = distance.JaroWinkler.similarity(place_to_search_lower, loc_name) * 100
            loc_ids = self.names_with_location_ids.get(loc_name, [])
            if score > max_score:
                max_score = score
                best_name = loc_name
            if score >= threshold:
                for loc_id in loc_ids:
                    if loc_id not in seen:
                        matching_locs_with_scores.append((loc_id, score))
                        seen.add(loc_id)

        # sort by descending score
        matching_locs_with_scores.sort(key=lambda x: x[1], reverse=True)
        matching_ids = [x[0] for x in matching_locs_with_scores]

        if max_score < threshold:
            self._logger.info(f"No match found for '{place_to_search}'. Maximum score: {max_score}, "
                          f"best candidate {best_name}")
        else:
            msg = f"Location '{place_to_search}' is identified with score {max_score} as one of these locations: "
            for match in matching_locs_with_scores:
                loc = self.location_name_dict.get(match[0])
                if loc:
                    msg = f"{msg} (loc_id={match[0]}, name={loc.get('main_name')}) "
            self._logger.info(msg)
        return matching_ids


if __name__ == "__main__":
    # For testing, we would need a Database instance, but this requires NocoDB configuration
    # In practice, FileLocationFinder creates the Database and passes it to LocationMatcher
    # For now, we'll leave this as a placeholder showing the new signature
    # communities_file_path_ = "./research/triage/locations/jg_communities_data.xlsx"
    # from birddog.log import get_logger
    # _logger = get_logger()
    # matcher = LocationMatcher(communities_file_path_, {}, {}, _logger)

    # To test with actual database, uncomment the following lines (requires NocoDB setup):
    # from birddog.database import Database
    # db = Database()
    # from birddog.log import get_logger
    # _logger = get_logger()
    # matcher = LocationMatcher(db, {}, {}, _logger)

    # Access the encapsulated dataset via the class instance
    # loc_id_1 = '-1055659'
    # location = matcher.location_name_dict.get(loc_id_1)
    # loc_name_ = location.get("main_name") if location else "Unknown"
    # print(f"Location for id={loc_id_1}: {location}")
    # if not matcher.find_location_id(loc_name_):
    #     print(f"Id for location {loc_name_} not found")

    # loc_name_ = "Monastyryska"
    # if not matcher.find_location_id(loc_name_):
    #     print(f"Id for location {loc_name_} not found")
    pass