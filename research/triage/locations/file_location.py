import os
import sys

# This explicitly adds your birddog root folder to the search path safely
root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if root_path not in sys.path:
    sys.path.insert(0, root_path)
import random
import re
from typing import Any, cast

from extract_location_from_descriptors import (
    extract_locations,
    locations_to_admin_units,
)
from read_all_locations import LocationMatcher

from birddog.database import Database
from birddog.log import get_logger
from birddog.translate import translation


def get_unique_random_integers(n, k):
    """Returns a list of n unique random integers from 1 to k (inclusive)."""
    random.seed()
    return sorted(random.sample(range(1, k + 1), n))


class FileLocationFinder:
    def __init__(self, debug_print: bool = False):
        self._logger = get_logger()
        self._db = Database()
        self._file_path = "./research/triage/locations/jg_communities_data.xlsx"
        self._matcher = LocationMatcher(self._file_path)

        self._regions_2_locations = {
            "Bessarabia":       {"location":"Chişinău",     "location_id":"-2276223"},
            "Bukovina":         {"location":"Chernivtsi",   "location_id":"-1037073"},
            "Moldavia":         {"location":"Chişinău",     "location_id":"-2276223"},
            "Podilia":          {"location":"Khmelnytskyy", "location_id":"-1041435"},
            "Podillia":         {"location":"Khmelnytskyy", "location_id":"-1041435"},
            "Podolia":          {"location":"Khmelnytskyy", "location_id":"-1041435"},
            "Ruthenia":         {"location":"Uzhhorod",     "location_id":"-1057311"},
            "Tavriya":          {"location":"Simferopol",   "location_id":"-1054041"},
            "Transcarpathia":   {"location":"Uzhhorod",     "location_id":"-1057311"},
            "Volhynia":         {"location":"Lutsk",        "location_id":"-1045249"},
            "Volyn":            {"location":"Lutsk",        "location_id":"-1045249"},
            "Zakarpattia":      {"location":"Uzhhorod",     "location_id":"-1057311"}
        }

        self._archive_locations = {
            "AGAD":     {"location":"Warszawa", "cyrillic_abbr":"ГАДА", "location_id":"-534433"},
            "AVPRI":    {"location":"Moscow", "cyrillic_abbr":"АВПРИ", "location_id":"-2960561"},
            "CDIAK":    {"location":"Kyiv", "cyrillic_abbr":"ЦДІАК", "location_id":"-1044367"},
            "DAARK":    {"location":"Simferopol", "cyrillic_abbr":"ДААРК", "location_id":"-1054041"},
            "DACHGO":   {"location":"Chernihiv", "cyrillic_abbr":"ДАЧгО", "location_id":"-1037057"},
            "DACHKO":   {"location":"Cherkasy", "cyrillic_abbr":"ДАЧкО", "location_id":"-1037001"},
            "DACHVO":   {"location":"Chernivtsi", "cyrillic_abbr":"ДАЧвО", "location_id":"-1037073"},
            "DADNO":    {"location":"Dnipro", "cyrillic_abbr":"ДАДнО", "location_id":"-1037865"},
            "DADO":     {"location":"Donetsk", "cyrillic_abbr":"ДАДоО", "location_id":"-1038078"},
            "DAHEO":    {"location":"Kherson", "cyrillic_abbr":"ДАХеО", "location_id":"-1041356"},
            "DAHMO":    {"location":"Khmelnytskyy", "cyrillic_abbr":"ДАХмО", "location_id":"-1041435"},
            "DAHO":     {"location":"Kharkiv", "cyrillic_abbr":"ДАХО", "location_id":"-1041320"},
            "DAIFO":    {"location":"Ivano-Frankivsk", "cyrillic_abbr":"ДАІФО", "location_id":"-1040327"},
            "DAK":      {"location":"Kyiv", "cyrillic_abbr":"ДАК", "location_id":"-1044367"},
            "DAKIRO":   {"location":"Kirovohrad", "cyrillic_abbr":"ДАКрО", "location_id":"-1041993"},
            "DAKO":     {"location":"Kyiv", "cyrillic_abbr":"ДАКО", "location_id":"-1044367"},
            "DAKRE":    {"location":"Kremenchuk", "cyrillic_abbr":"Архівний_відділ_виконавчого_комітету_Кременчуцької_міської_ради", "location_id":"-1043663"},
            "DALO":     {"location":"Lviv", "cyrillic_abbr":"ДАЛО", "location_id":"-1045268"},
            "DALUO":    {"location":"Luhansk", "cyrillic_abbr":"ДАЛуО", "location_id":"-1045160"},
            "DAMO":     {"location":"Mykolayiv", "cyrillic_abbr":"ДАМО", "location_id":"-1047257"},
            "DAOO":     {"location":"Odesa", "cyrillic_abbr":"ДАОО", "location_id":"-1049092"},
            "DAPO":     {"location":"Poltava", "cyrillic_abbr":"ДАПО", "location_id":"-1051195"},
            "DARO":     {"location":"Rivne", "cyrillic_abbr":"ДАРО", "location_id":"-1052476"},
            "DAS":      {"location":"Sevastopol", "cyrillic_abbr":"ДАС", "location_id":"-1053419"},
            "DASO":     {"location":"Sumy", "cyrillic_abbr":"ДАСО", "location_id":"-1055659"},
            "DATO":     {"location":"Ternopil", "cyrillic_abbr":"ДАТО", "location_id":"-1056204"},
            "DAVIO":    {"location":"Vinnitsa", "cyrillic_abbr":"ДАВіО", "location_id":"-1058303"},
            "DAVO":     {"location":"Lutsk", "cyrillic_abbr":"ДАВоО", "location_id":"-1045249"},
            "DAZHO":    {"location":"Zhitomir", "cyrillic_abbr":"ДАЖО", "location_id":"-1060903"},
            "DAZKO":    {"location":"Uzhhorod", "cyrillic_abbr":"ДАЗкО", "location_id":"-1057311"},
            "DAZPO":    {"location":"Zaporozh'ye", "cyrillic_abbr":"ДАЗпО", "location_id":"-1060168"},
            "DISZMO":   {"location":"Ostrog", "cyrillic_abbr":"ДІСЗМО", "location_id":"-1049602"},
            "GDA-MOD":  {"location":"Kyiv", "cyrillic_abbr":"ГДА МО", "location_id":"-1044367"},
            "GDA-MVS":  {"location":"Kyiv", "cyrillic_abbr":"ГДА МВС", "location_id":"-1044367"},
            "GDA-SSU":  {"location":"Kyiv", "cyrillic_abbr":"ГДА СБУ", "location_id":"-1044367"},
            "GDA-SZRU": {"location":"Kyiv", "cyrillic_abbr":"ГДА СЗРУ", "location_id":"-1044367"},
            "ILNAN":    {"location":"Kyiv", "cyrillic_abbr":"Національний_музей_Тараса_Шевченка", "location_id":"-1044367"},
            "IR-NBUV":  {"location":"Kyiv", "cyrillic_abbr":"ІР НБУВ", "location_id":"-1044367"},
            "KPDIMZ":   {"location":"Kamenets Podolskiy", "cyrillic_abbr":"Кам'янець-Подільський_державний_історичний_музей-заповідник", "location_id":"-1040849"},
            "KUIZA":    {"location":"Izmail", "cyrillic_abbr":"КУІзА", "location_id":"-1040491"},
            "NIAB":     {"location":"Minsk", "cyrillic_abbr":"НГАБ", "location_id":"-1946324"},
            "NBUV":     {"location":"Kyiv", "cyrillic_abbr":"НБУВ", "location_id":"-1044367"},
            "OMELNIK":  {"location":"Kremenchuk", "cyrillic_abbr":"Трудовий_архів_виконавчого_комітету_Омельницької_сільської_ради_Кременчуцького_району_Полтавської_області", "location_id":"-1043663"},
            "OMR":      {"location":"Odesa", "cyrillic_abbr":"OMR", "location_id":"-1049092"},
            "ONU":      {"location":"Odesa", "cyrillic_abbr":"ОНУ", "location_id":"-1049092"},
            "RGADA":    {"location":"Moscow", "cyrillic_abbr":"РДАДА", "location_id":"-2960561"},
            "RGIA":     {"location":"Leningrad", "cyrillic_abbr":"РДІА", "location_id":"-2996338"},
            "TSDAHOU":  {"location":"Kyiv", "cyrillic_abbr":"ЦДАГОУ", "location_id":"-1044367"},
            "TSDAVO":   {"location":"Kyiv", "cyrillic_abbr":"ЦДАВО", "location_id":"-1044367"},
            "TSDIAL":   {"location":"Lviv", "cyrillic_abbr":"ЦДІАЛ", "location_id":"-1045268"}
        }

    def find_archive_name_by_cyrillic_abbr(self, cyrillic_abbr: str) -> str | None:
        """
        Searches self._archive_locations for a matching 'cyrillic_abbr'
        and returns the main key (e.g., 'AGAD') if found.
        """
        for key, data in self._archive_locations.items():
            if data.get("cyrillic_abbr") == cyrillic_abbr:
                return key
        return None

    def get_doc_location(self, doc_id: int, only_smallest_locations: bool = True, debug_print: bool = False):
        """Identifies and returns the location ID for a given document.

        This function fetches descriptions associated with the document ID, extracts
        geographical places from those descriptions using an AI token, and utilizes
        a LocationMatcher to resolve and return the corresponding place ID.

        Args:
            doc_id (int): The unique identifier of the target document.
            only_smallest_locations (bool, optional): If True, only the locations with
                the smallest administrative rank are retained, for example, villages and not district centers.
            debug_print (bool, optional): If True, outputs detailed logging messages
                regarding the identified location name, and fallback logic.
                Defaults to False.

        Returns:
            The list of location IDs: The identified place identifiers.
                Returns None if no matching location can be determined.
        """
        if debug_print:
            print(f"***** Looking for locations for document {doc_id} *****")
        hf_token = os.getenv("HF_TOKEN", "")  # For session management

        descriptions, doc_archive_locs = self.get_doc_descriptions(doc_id, debug_print)
        descriptions, region_centres = self.separate_region_centres(descriptions, debug_print)
        extracted_places = extract_locations(list(descriptions), hf_token, debug_print)
        identified_locations = self.match_places_to_location_ids(doc_id, extracted_places, debug_print)

        # 1. Helper to recursively turn a dict (and any inner sets) into a hashable frozenset
        def _freeze_dict(d):
            return frozenset((k, frozenset(v) if isinstance(v, set) else v) for k, v in d.items())

        # 2. Combine both lists of dicts safely by converting their contents to frozensets
        frozen_union = {_freeze_dict(d) for d in doc_archive_locs} | {_freeze_dict(d) for d in region_centres}

        # 3. Reconstruct a list of normal mutable dictionaries for your loop to use
        united_list = [dict(f_set) for f_set in frozen_union]

        for location in united_list:
            loc_id = location["location_id"]
            location = self._matcher.location_name_dict.get(loc_id)
            if location:
                location["administrative_level"] = 2
                location["loc_id"] = loc_id
                hashable_items = (
                    (k, frozenset(v) if isinstance(v, set) else v)
                    for k, v in location.items()
                )
                identified_locations.add(frozenset(hashable_items))

        if not identified_locations:
            return []

        # Convert the frozenset to a dict
        identified_locations_dict_list = [dict(f_set) for f_set in identified_locations]
        
        if only_smallest_locations:
            target_level = min(loc["administrative_level"] for loc in identified_locations_dict_list)
            result = []
            msg = "Most specific locations:"
            for dict_loc in identified_locations_dict_list:
                if dict_loc.get("administrative_level") == target_level:
                    result.append(dict_loc["loc_id"])
                    main_name = dict_loc.get("main_name")
                    if main_name is not None:
                        msg = f"{msg} '{main_name}' "

            if debug_print:
                print(msg)
            identified_locations = result
        elif debug_print:
            # print the list of locations according to the administrative level
            settlements = [dict_loc for dict_loc in identified_locations_dict_list
                           if dict_loc.get("administrative_level") == 0]
            if len(settlements) > 0:
                msg = "All locations, settlements: "
                for dict_loc in settlements:
                    msg = f"{msg} '{dict_loc.get("main_name")}' "
                print(msg)
                
            districts = [dict_loc for dict_loc in identified_locations_dict_list
                           if dict_loc.get("administrative_level") == 1]
            if len(districts) > 0:
                msg = "All locations, districts: "
                for dict_loc in districts:
                    msg = f"{msg} '{dict_loc.get("main_name")}' "
                print(msg)

            provinces = [dict_loc for dict_loc in identified_locations_dict_list
                if dict_loc.get("administrative_level") == 2]
            if len(provinces) > 0:
                msg = "All locations, provinces: "
                for dict_loc in provinces:
                    msg = f"{msg} '{dict_loc.get('main_name')}' "
                print(msg)

        return identified_locations

    def get_archive_locations(self, archive_locs: list[dict], debug_print: bool, owning_pages: Any | None
    ) -> list[dict]:
        """Retrieves and compiles archive location information from document page hierarchies.

        This function processes a list of initial archive locations and expands it by examining
        the owning pages hierarchy of a document. For each owning page, it looks up the root
        label in the global _archive_locations dictionary and adds corresponding archive
        location information. Finally, it removes duplicate locations based on location_id.

        Args:
            archive_locs (list[dict]): Initial list of archive location dictionaries to be expanded.
            debug_print (bool, optional): If True, prints status messages when root labels
                cannot be found in the _archive_locations dictionary. Defaults to False.
            owning_pages (Any | None): List of owning page dictionaries from the document record,
                or None if no owning pages exist.

        Returns:
            list[dict]: A list of unique archive location dictionaries with duplicates removed
                based on location_id. Each dictionary contains location information such as
                location name, cyrillic abbreviation, and location_id.
        """
        if owning_pages:
            for page in owning_pages:
                page_id = page.get("Id")
                page_rec = cast(dict, self._db.read("Pages", page_id))
                root_label = page_rec.get("root_label")
                if root_label:
                    # Splits at the first '-' and retains everything before it
                    root_label = root_label.split("-", 1)[0]

                if root_label in self._archive_locations:
                    archive_locs.append(self._archive_locations[root_label])
                else:
                    if debug_print:
                        print(f"Could not find root label {root_label}")

        # Removes duplicates by using the hashable representation as a temporary key
        archive_locs = list(
            {
                (
                    frozenset(loc["location_id"].items())
                    if isinstance(loc["location_id"], dict)
                    else loc["location_id"]
                ): loc
                for loc in archive_locs
            }.values()
        )
        return archive_locs

    def separate_region_centres(self, descriptions: set[str], debug_print: bool = False)-> tuple[set[str], list[dict]]:
        all_words_to_delete = [
            'court', 'Peace', 'Justice', 'Judicial', 'Investigative', 'Sentence', 'Sentences',
            'the', 'statistical', 'economic', 'historical', 'philological', 'educational',
            'committee', 'council', 'ministry', 'Office','Department', 'Community', 'Funds',
            'State', 'Archive', 'Archives', 'ministers', 'University', 'institute', 'gymnasium',
            'statistics', 'Conscription', 'branch', 'Agency', 'Society',
            'council', 'councils', 'Judgment', 'Judgments', 'rural', "Men's", "Women's"]

        # delete also the religious terms
        religious_terms = ['Roman Catholic', 'churches', 'Church', 'synagogue', 'Jewish', 'Jews', 'rabbinate',
                           'Spiritual', 'Theological', 'Seminary', 'Consistory', 'Orthodox', 'Assumption', 'deanery',
                           'Trinity', 'Resurrection', 'Ascension', 'Intercession', 'Annunciation', 'Transfiguration']
        all_words_to_delete.extend(religious_terms)

        # delete also the documentation/archival terms
        archival_noise = [
            "Metric",
            "Confessional",
            "book",
            "books",
            "record",
            "records",
            "birth",
            "marriage",
            "death",
            "file",
            "files",
            "folder",
            "folders",
            "document",
            "documents",
            "Decree",
            "Decrees",
            "journal",
            "journals",
            "magistrate",
            "meeting",
            "meetings",
            "prior",
            "to",
            "after",
            "year",
            "years",
            "century",
        ]
        all_words_to_delete.extend(archival_noise)
        region_words = ["oblast", "province", "region", "voivodeship", "governorate"]

        descriptions_without_regions = set()
        region_centres = []
        for description in descriptions:
            found_province = False
            for region, centre in self._regions_2_locations.items():
                if contains_word(description, region):
                    found_province = True
                    region_centres.append(centre)
                    if debug_print:
                        print(f"Description '{description}' identified as relating to the region with location "
                            f"'{centre['location']}', ID {centre['location_id']}")
                    break

            words_to_delete = all_words_to_delete
            if found_province:
                description = remove_specific_word(description, region, False)
                all_words_to_delete.extend(region_words)

            description = remove_words_list(description, words_to_delete, True)

            description = replace_word(description, 'regional', 'region')
            description = replace_word(description, 'provincial', 'province')
            description = description.replace(" of ", " ")
            description = description.replace(" and ", ", ")

            if debug_print:
                print(f"Description shortened to '{description}'")

            descriptions_without_regions.add(description)

        return descriptions_without_regions, region_centres

    def get_doc_descriptions(self, doc_id: int, debug_print: bool = False)-> tuple[set[str], list[dict]]:
        """Retrieves and compiles descriptions and storage locations for a document.

        Args:
            doc_id (int): The unique identifier of the target document.
            debug_print (bool, optional): If True, prints status messages when
                records are missing. Defaults to False.

        Returns:
            set: A set containing the main document description followed by the
                storage unit locations/descriptions.
        """
        doc_rec = get_doc_record(self._db, doc_id)
        if not doc_rec:
            print(f"Could not find document with id {doc_id}")
            return set(), []

        doc_description = doc_rec.get("description")
        page_description = doc_rec.get("page_description")
        owning_pages = doc_rec.get("owning_pages")

        title = doc_rec.get("title")
        cyrillic_space_str, other_space_str, cyrillic_words = (
            separate_words_by_cyrillic(title)
        )
        if debug_print:
            print(f"Title Latin part: {other_space_str}")
        translated_cyrillic = []
        if cyrillic_space_str:
            # translate the Cyrillic part
            translated_cyrillic = translation(cyrillic_space_str)
            if debug_print:
                print(f"Title Cyrillic part: {cyrillic_space_str}")
                print(f"Translated Cyrillic part: {translated_cyrillic}")

        # Search for cyrillic words in _archive_locations cyrillic_abbr values
        doc_archive_locs = []
        for word in cyrillic_words:
            for value in self._archive_locations.values():
                if value.get("cyrillic_abbr") == word:
                    doc_archive_locs.append(value)

        doc_archive_locs = self.get_archive_locations(doc_archive_locs, debug_print, owning_pages)

        descriptions = set()

        # add document descriptions
        if doc_description:
            descriptions.add(doc_description)
        if page_description:
            descriptions.add(page_description)

        # add document name
        if other_space_str:
            descriptions.add(other_space_str)
        if translated_cyrillic:
            descriptions.add(str(translated_cyrillic))

        # adding storage unit (cases/opi/fund/archive) descriptions, starting from the cases
        while owning_pages:
            upper_level_pages = []
            for page in owning_pages:
                page_id = page.get("Id")
                page_rec = cast(dict, self._db.read("Pages", page_id))
                descr = page_rec.get("description")
                if descr:
                    descriptions.add(descr)
                upper_level_page = page_rec.get("parent")
                if upper_level_page:
                    upper_level_pages.extend(upper_level_page)

            owning_pages = upper_level_pages

        if debug_print:
            print(f"Found {len(descriptions)} descriptions {descriptions}")
        return descriptions, doc_archive_locs

    def scan_database(self, **kwargs):
        """Public wrapper to safely access the internal database scan."""
        return self._db.scan(**kwargs)

    def get_location_from_id(self, loc_id: str):
        return self._matcher.location_name_dict.get(loc_id)


    def match_places_to_location_ids(self, doc_id: int, extracted_places: list[str],
                                     debug_print: bool) ->  set[frozenset[tuple[str, Any]]]:
        loc_admin_units, province_names, district_names = locations_to_admin_units(extracted_places, debug_print)
        identified_locations = set()
    
        # Try to match all locations from each extraction
        for loc_admin_unit in loc_admin_units:
            extracted_loc_name = loc_admin_unit["location"]
            place_ids = self._matcher.find_location_id(extracted_loc_name, debug_print)
            found_admin_match = False
            final_id = None
            for place_id in place_ids:
                location = self._matcher.location_name_dict.get(place_id)
                # there may be more than 1 location with this name. is the district right?
                if location is not None and not district_names.isdisjoint(location["district_names"]):
                    found_admin_match = True
                    final_id = place_id
                    break
    
            if not found_admin_match:
                # no alternative location with the right district is found, check with the province
                for place_id in place_ids:
                    location = self._matcher.location_name_dict.get(place_id)
                    if location is not None and not province_names.isdisjoint(location["province_names"]):
                        found_admin_match = True
                        final_id = place_id
                        break

            if not found_admin_match and place_ids:
                final_id = place_ids[0]

            if final_id is not None:
                location = self._matcher.location_name_dict.get(final_id)
                if location:
                    location["administrative_level"] = loc_admin_unit["administrative_level"]
                    location["loc_id"] = final_id
                    hashable_items = (
                        (k, frozenset(v) if isinstance(v, set) else v)
                        for k, v in location.items()
                    )
                    identified_locations.add(frozenset(hashable_items))
                    if debug_print:
                        loc_name = location.get("main_name")
                        adm_level = location.get("administrative_level")
                        adm_status = "settlement" if adm_level == 0 else \
                            "district centre" if adm_level == 1 else "province capital"
                        print(f"Identified location '{extracted_loc_name}' for document ID {doc_id} as "
                              f"'{loc_name}', {adm_status}, ID={final_id}")
        return identified_locations


def separate_words_by_cyrillic(file_string):
    """Splits an input string into Cyrillic and non-Cyrillic words based on character content.

    This function:
    1. Splits the input string using underscores (_), colons (:), or periods (.) as delimiters
    2. Separates words into two categories:
        - Cyrillic words (containing Cyrillic characters)
        - Non-Cyrillic words (Latin/other characters)
    3. Returns three values: separate Cyrillic/non-Cyrillic strings and the original Cyrillic word list

    Args:
        file_string: String containing text with mixed character sets

    Returns:
        Tuple of (cyrillic_space_str, other_space_str, cyrillic_words)
    """
    # Split the string using any of the delimiters: _, :, or .
    words = re.split(r"[_:.]", file_string)

    # Filter out empty strings caused by consecutive delimiters
    words = [word for word in words if word]

    cyrillic_words = []
    other_words = []

    # Check each word for the presence of Cyrillic characters
    for word in words:
        if re.search(r"[\u0400-\u04FF]", word):
            cyrillic_words.append(word)
        else:
            other_words.append(word)

    # Convert lists into space-separated strings
    cyrillic_space_str = " ".join(cyrillic_words)
    other_space_str = " ".join(other_words)

    return cyrillic_space_str, other_space_str, cyrillic_words


def contains_word(text: str, target_word: str) -> bool:
    # Lookarounds: ensure no letters, digits, or hyphens touch the target word
    pattern = r"(?<![\w-])" + re.escape(target_word) + r"(?![\w-])"

    match = re.search(pattern, text)
    return bool(match)


def remove_words_list(text: str, target_words: list[str], ignore_case: bool = True) -> str:
    # If the list is empty, return the original text immediately
    if not target_words:
        return text

    # Escape each word and join them with the regex OR operator '|'
    # Example output: (wordone|wordtwo|wordthree)
    escaped_words = "|".join(re.escape(word) for word in target_words)
    words_pattern = f"({escaped_words})"

    # Wrap the joined words in your custom hyphen-safe lookarounds
    pattern = r"(?<![\w-])" + words_pattern + r"(?![\w-])"

    # Configure flags
    flags = re.IGNORECASE if ignore_case else 0

    # Remove all matching words in a single pass
    cleaned_text = re.sub(pattern, "", text, flags=flags)

    # Clean up extra whitespace left behind
    cleaned_text = re.sub(r"\s+", " ", cleaned_text).strip()

    # Fix stray spaces before periods, commas, or semicolons
    cleaned_text = re.sub(r"\s+([.,;])", r"\1", cleaned_text)

    return cleaned_text


def remove_specific_word(text: str, target_word: str, ignore_case: bool = True) -> str:
    # Custom lookarounds to prevent splitting on hyphens
    pattern = r"(?<![\w-])" + re.escape(target_word) + r"(?![\w-])"

    # Set the flags based on the boolean parameter
    flags = re.IGNORECASE if ignore_case else 0

    # Remove the word using the configured flags
    cleaned_text = re.sub(pattern, "", text, flags=flags)

    # Clean up extra whitespace left behind
    cleaned_text = re.sub(r"\s+", " ", cleaned_text).strip()
    return cleaned_text


def replace_word(text: str, w1: str, w2: str) -> str:
    # \b ensures we match 'w1' as a standalone word (separated by punctuation or spaces)
    # re.IGNORECASE makes the search case-insensitive
    pattern = r'\b' + re.escape(w1) + r'\b'
    return re.sub(pattern, w2, text, flags=re.IGNORECASE)

def get_doc_record(db, doc_id):
    if not doc_id:
        return {}
    doc_rec = db.read("Documents", doc_id)
    return doc_rec


#testing
if __name__ == "__main__":
    debug_print_ = True
    finder = FileLocationFinder(debug_print_)
#    doc_id_ = 12953
#    print(finder.get_doc_descriptions(doc_id_))

    doc_ids = [7544]
#    doc_ids = get_unique_random_integers(20 ,37738)

    print(f"Document IDs to process: {doc_ids}")
    for doc_id_ in doc_ids:
        finder.get_doc_location(doc_id_, only_smallest_locations=False, debug_print=debug_print_)
