import os
import re
from typing import Any

from extract_location_from_descriptors import (
    extract_locations,
    locations_to_admin_units,
)
from read_all_locations import LocationMatcher

from birddog.database import Database
from birddog.log import get_logger
from birddog.translate import translation

_logger = get_logger()
_db = Database()

archive_locations = {
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
    "ILNAN":    {"location":"Kyiv", "cyrillic_abbr":"Національний_музей_Тараса_Шевченка", "location_id":"-1044367"},
    "KPDIMZ":   {"location":"Kamenets Podolskiy", "cyrillic_abbr":"Кам'янець-Подільський_державний_історичний_музей-заповідник", "location_id":"-1040849"},
    "KUIZA":    {"location":"Izmail", "cyrillic_abbr":"КУІзА", "location_id":"-1040491"},
    "NIAB":     {"location":"Minsk", "cyrillic_abbr":"НГАБ", "location_id":"-1946324"},
    "OMELNIK":  {"location":"Kremenchuk", "cyrillic_abbr":"Трудовий_архів_виконавчого_комітету_Омельницької_сільської_ради_Кременчуцького_району_Полтавської_області", "location_id":"-1043663"},
    "OMR":      {"location":"Odesa", "cyrillic_abbr":"OMR", "location_id":"-1049092"},
    "ONU":      {"location":"Odesa", "cyrillic_abbr":"ОНУ", "location_id":"-1049092"},
    "RGADA":    {"location":"Moscow", "cyrillic_abbr":"РДАДА", "location_id":"-2960561"},
    "RGIA":     {"location":"Leningrad", "cyrillic_abbr":"РДІА", "location_id":"-2996338"},
    "TSDAHOU":  {"location":"Kyiv", "cyrillic_abbr":"ЦДАГОУ", "location_id":"-1044367"},
    "TSDAVO":   {"location":"Kyiv", "cyrillic_abbr":"ЦДАВО", "location_id":"-1044367"},
    "TSDIAL":   {"location":"Lviv", "cyrillic_abbr":"ЦДІАЛ", "location_id":"-1045268"}
}


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
        Tuple of (cyrillic_space_str, other_space_str, cyrillic_words)"
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


def get_doc_record(db, doc_id):
    if not doc_id:
        return {}
    doc_rec = db.read("Documents", doc_id)
    return doc_rec


def get_doc_descriptions(db, doc_id: int, debug_print:bool = False):
    """Retrieves and compiles descriptions and storage locations for a document.

    Args:
        db: The database connection or instance used to read records.
        doc_id (int): The unique identifier of the target document.
        debug_print (bool, optional): If True, prints status messages when
            records are missing. Defaults to False.

    Returns:
        list: A list containing the main document description followed by the
            compiled storage unit locations/descriptions. Returns an empty
            list if the document cannot be found.
    """
    doc_rec = get_doc_record(db, doc_id)
    if not doc_rec:
        print(f"Could not find document with id {doc_id}")
        return [], []
    
    doc_description = doc_rec.get("description")
    owning_pages = doc_rec.get("owning_pages")

    title = doc_rec.get("title")
    cyrillic_space_str, other_space_str, cyrillic_words = separate_words_by_cyrillic(title)
    if debug_print:
        print(f"Title Latin part: {other_space_str}")
    translated_cyrillic = {}
    if cyrillic_space_str:
        # translate the Cyrillic part
        translated_cyrillic = translation(cyrillic_space_str)
        if debug_print:
            print(f"Title Cyrillic part: {cyrillic_space_str}")
            print(f"Translated Cyrillic part: {translated_cyrillic}")

    # Search for cyrillic words in archive_locations cyrillic_abbr values
    doc_archive_locs = []
    for word in cyrillic_words:
        for value in archive_locations.values():
            if value.get("cyrillic_abbr") == word:
                doc_archive_locs.append(value)

    doc_archive_locs = get_archive_locations(doc_archive_locs, db, debug_print, owning_pages)

    descriptions = []

    # add document descriptions
    if doc_description:
        descriptions.append(doc_description)

    # add document name
    if other_space_str:
        descriptions.append(other_space_str)
    if translated_cyrillic:
        descriptions.append(translated_cyrillic)

    # adding storage unit (cases/opi/fund/archive) descriptions, starting from the cases
    while owning_pages:
        upper_level_pages = []
        for page in owning_pages:
            page_id = page.get("Id")
            page_rec = db.read("Pages", page_id)
            descr = page_rec.get("description")
            if descr:
                descriptions.append(descr)
            upper_level_page = page_rec.get("parent")
            if upper_level_page:
                upper_level_pages.extend(upper_level_page)

        owning_pages = upper_level_pages

    if debug_print:
        print(f"Found {len(descriptions)} descriptions {descriptions}")
    return descriptions, doc_archive_locs


def get_archive_locations(archive_locs: list[dict], db, debug_print: bool, owning_pages: Any | None) -> list[dict]:
    """Retrieves and compiles archive location information from document page hierarchies.

    This function processes a list of initial archive locations and expands it by examining
    the owning pages hierarchy of a document. For each owning page, it looks up the root
    label in the global archive_locations dictionary and adds corresponding archive
    location information. Finally, it removes duplicate locations based on location_id.

    Args:
        archive_locs (list[dict]): Initial list of archive location dictionaries to be expanded.
        db: The database connection or instance used to read page records.
        debug_print (bool, optional): If True, prints status messages when root labels
            cannot be found in the archive_locations dictionary. Defaults to False.
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
            page_rec = db.read("Pages", page_id)
            root_label = page_rec.get("root_label")
            if root_label in archive_locations:
                archive_locs.append(archive_locations[root_label])
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


def get_doc_location(doc_id: int, only_smallest_locations:bool = True, debug_print:bool = False):
    """Identifies and returns the location ID for a given document.

    This function fetches descriptions associated with the document ID, extracts
    geographical places from those descriptions using an AI token, and utilizes
    a LocationMatcher to resolve and return the corresponding place ID.

    Args:
        doc_id (int): The unique identifier of the target document.
        only_smallest_locations (bool, optional): If True, only the locations with
            the smallest administrative rank are retained, for example, villages and not district centers.
        debug_print (bool, optional): If True, outputs detailed logging messages
            regarding the identified location name, country, and fallback logic.
            Defaults to False.

    Returns:
        The list of location IDs: The identified place identifiers.
            Returns None if no matching location can be determined.
    """
    hf_token = os.getenv("HF_TOKEN", "")  # For session management
    file_path_ = "./research/triage/locations/iijg_populations_merged.xlsx"
    tree_path_ = "./research/triage/locations/jg_communities_tree.xlsx"
    matcher = LocationMatcher(file_path_, tree_path_, True)

    descriptions, doc_archive_locs = get_doc_descriptions(_db, doc_id, debug_print)
    extracted_places = extract_locations(descriptions, hf_token, debug_print)
    loc_admin_units = locations_to_admin_units(extracted_places, debug_print)
    identified_locations = {}

    # Try to match all locations from each extraction
    for loc_admin_unit in loc_admin_units:
        extracted_loc_name = loc_admin_unit["location"]
        place_id = matcher.find_location_id(extracted_loc_name, debug_print)
        if place_id:
            location = matcher.location_name_dict.get(place_id)
            if location is not None:
                if place_id in identified_locations:
                    identified_locations[place_id] ["administrative_level"] = min(
                        identified_locations[place_id] ["administrative_level"],
                        loc_admin_unit["administrative_level"])
                else:
                    location["administrative_level"] = loc_admin_unit["administrative_level"]
                    identified_locations[place_id] = location
                if debug_print:
                    loc_name = location.get("main_name")
                    loc_country = location.get("country")
                    adm_level = location.get("administrative_level")
                    adm_status = "settlement" if adm_level == 0 else "district centre" if adm_level == 1 else "province capital"
                    print(f"Identified location '{extracted_loc_name}' for document ID {doc_id} as '{loc_name}', {adm_status} "
                          f"in {loc_country}, ID={place_id}")

    # add archive locations
    for archive_loc in doc_archive_locs:
        loc_id = archive_loc["location_id"]
        if loc_id not in identified_locations:
            identified_locations[loc_id] = {"administrative_level": 2, "main_name": archive_loc["location"]}
    if not identified_locations:
        return []

    if only_smallest_locations:
        target_level = min(loc['administrative_level'] for place_id, loc in identified_locations.items()
                           if loc and isinstance(loc, dict))
        result = []
        smallest_location_msg = "Most specific locations: "
        for place_id, loc in identified_locations.items():
            if loc and isinstance(loc, dict) and loc.get('administrative_level') == target_level:
                result.append(place_id)
                main_name = loc.get("main_name")
                if main_name is not None:
                    smallest_location_msg = f"{smallest_location_msg} '{main_name}' "

        if debug_print:
            print(smallest_location_msg)
        identified_locations = result

    return identified_locations


#testing
if __name__ == "__main__":
#    doc_id_ = 473618 # 294264
#    print(get_doc_descriptions(_db, doc_id_))

#    doc_ids = [473618, 467097, 465462, 449456, 441443, 438665, 427846, 422578, 410602, 406970, 401442, 397570, 393437, 322483, 316602, 314037, 304954, 294264, 272675, 272621]
    doc_ids = [304954]
    for doc_id_ in doc_ids:
        print(get_doc_location(doc_id_, only_smallest_locations=True, debug_print=True))
