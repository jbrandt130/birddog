import os
import re

from extract_location_from_descriptors import extract_locations
from read_all_locations import LocationMatcher

from birddog.database import Database
from birddog.log import get_logger
from birddog.translate import translation

_logger = get_logger()
_db = Database()

archive_locations = {
    "AGAD":     {"location":"Warszawa", "cyrillic_abbr":"ГАДА"},
    "AVPRI":    {"location":"Moscow", "cyrillic_abbr":"АВПРИ"},
    "CDIAK":    {"location":"Kyiv", "cyrillic_abbr":"ЦДІАК"},
    "DAARK":    {"location":"Simferopol", "cyrillic_abbr":"ДААРК"},
    "DACHGO":   {"location":"Chernihiv", "cyrillic_abbr":"ДАЧгО"},
    "DACHKO":   {"location":"Cherkasy", "cyrillic_abbr":"ДАЧкО"},
    "DACHVO":   {"location":"Chernivtsi", "cyrillic_abbr":"ДАЧвО"},
    "DADNO":    {"location":"Dnipro", "cyrillic_abbr":"ДАДнО"},
    "DADO":     {"location":"Donetsk", "cyrillic_abbr":"ДАДоО"},
    "DAHEO":    {"location":"Kherson", "cyrillic_abbr":"ДАХеО"},
    "DAHMO":    {"location":"Khmelnytskyy", "cyrillic_abbr":"ДАХмО"},
    "DAHO":     {"location":"Kharkiv", "cyrillic_abbr":"ДАХО"},
    "DAIFO":    {"location":"Ivano-Frankivsk", "cyrillic_abbr":"ДАІФО"},
    "DAK":      {"location":"Kyiv", "cyrillic_abbr":"ДАК"},
    "DAKIRO":   {"location":"Kirovohrad", "cyrillic_abbr":"ДАКрО"},
    "DAKO":     {"location":"Kyiv", "cyrillic_abbr":"ДАКО"},
    "DAKRE":    {"location":"Kremenchuk", "cyrillic_abbr":"Архівний_відділ_виконавчого_комітету_Кременчуцької_міської_ради"},
    "DALO":     {"location":"Lviv", "cyrillic_abbr":"ДАЛО"},
    "DALUO":    {"location":"Luhansk", "cyrillic_abbr":"ДАЛуО"},
    "DAMO":     {"location":"Mykolayiv", "cyrillic_abbr":"ДАМО"},
    "DAOO":     {"location":"Odesa", "cyrillic_abbr":"ДАОО"},
    "DAPO":     {"location":"Poltava", "cyrillic_abbr":"ДАПО"},
    "DARO":     {"location":"Rivne", "cyrillic_abbr":"ДАРО"},
    "DAS":      {"location":"Sevastopol", "cyrillic_abbr":"ДАС"},
    "DASO":     {"location":"Sumy", "cyrillic_abbr":"ДАСО"},
    "DATO":     {"location":"Ternopil", "cyrillic_abbr":"ДАТО"},
    "DAVIO":    {"location":"Vinnitsa", "cyrillic_abbr":"ДАВіО"},
    "DAVO":     {"location":"Lutsk", "cyrillic_abbr":"ДАВоО"},
    "DAZHO":    {"location":"Zhitomir", "cyrillic_abbr":"ДАЖО"},
    "DAZKO":    {"location":"Ungvár", "cyrillic_abbr":"ДАЗкО"},
    "DAZPO":    {"location":"Zaporozh'ye", "cyrillic_abbr":"ДАЗпО"},
    "DISZMO":   {"location":"Ostrog", "cyrillic_abbr":"ДІСЗМО"},
    "ILNAN":    {"location":"Kyiv", "cyrillic_abbr":"Національний_музей_Тараса_Шевченка"},
    "KPDIMZ":   {"location":"Kamenets Podolskiy", "cyrillic_abbr":"Кам'янець-Подільський_державний_історичний_музей-заповідник"},
    "KUIZA":    {"location":"Izmail", "cyrillic_abbr":"КУІзА"},
    "NIAB":     {"location":"Minsk", "cyrillic_abbr":"НГАБ"},
    "OMELNIK":  {"location":"Kremenchuk", "cyrillic_abbr":"Трудовий_архів_виконавчого_комітету_Омельницької_сільської_ради_Кременчуцького_району_Полтавської_області"},
    "OMR":      {"location":"Odesa", "cyrillic_abbr":"OMR"},
    "ONU":      {"location":"Odesa", "cyrillic_abbr":"ОНУ"},
    "RGADA":    {"location":"Moscow", "cyrillic_abbr":"РДАДА"},
    "RGIA":     {"location":"Leningrad", "cyrillic_abbr":"РДІА"},
    "TSDAHOU":  {"location":"Kyiv", "cyrillic_abbr":"ЦДАГОУ"},
    "TSDAVO":   {"location":"Kyiv", "cyrillic_abbr":"ЦДАВО"},
    "TSDIAL":   {"location":"Lviv", "cyrillic_abbr":"ЦДІАЛ"}
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
        return []
    
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

    archive_locs = []
    if owning_pages:
        for page in owning_pages:
            page_id = page.get("Id")
            page_rec = db.read("Pages", page_id)
            root_label = page_rec.get("root_label")
            if root_label in archive_locations:
                archive_locs.append(archive_locations[root_label]["location"])
            else:
                if debug_print:
                    print(f"Could not find root label {root_label}")

    # Search for cyrillic words in archive_locations cyrillic_abbr values
    for word in cyrillic_words:
        for value in archive_locations.values():
            if value.get("cyrillic_abbr") == word:
                location_value = value.get("location")
                if location_value is not None:
                    archive_locs.append(location_value)

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

    if archive_locs:
        descriptions.extend(archive_locs)

    if debug_print:
        print(f"Found {len(descriptions)} descriptions {descriptions}")
    return descriptions


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

    descriptions = get_doc_descriptions(_db, doc_id, debug_print)
    extracted_places = extract_locations(descriptions, hf_token, debug_print)
    identified_locations = {}

    # Try to match all locations (most_specific and additional) from each extraction
    for place_extraction in extracted_places:
        place_id = matcher.find_location_id(place_extraction, debug_print)
        if place_id:
            location = matcher.location_name_dict.get(place_id)
            if location is not None:
                identified_locations[place_id] = location
                if debug_print:
                    loc_name = location.get("main_name") if location else "Unknown"
                    loc_country = location.get("country") if location else "Unknown"
                    print(f"Identified location {place_extraction} for document ID {doc_id} as {loc_name} "
                          f"in {loc_country}, ID={place_id}")

    if not identified_locations:
        return []

    # Removes duplicates
    seen = set()
    unique_list = {}

    for place_id, loc in identified_locations.items():
        if loc["main_name"] not in seen:
            seen.add(loc["main_name"])
            unique_list[place_id] = loc
    identified_locations = unique_list

    if only_smallest_locations:
        target_level = min(loc['administrative_level'] for place_id, loc in identified_locations.items()
                           if loc and isinstance(loc, dict))
        result = []
        smallest_location_msg = "Smallest locations: "
        for place_id, loc in identified_locations.items():
            if loc and isinstance(loc, dict) and loc.get('administrative_level') == target_level:
                result.append(place_id)
                main_name = loc.get("main_name")
                if main_name is not None:
                    smallest_location_msg += main_name + ' '

        if debug_print:
            print(smallest_location_msg)
        return result

    return identified_locations


#testing
if __name__ == "__main__":
#    doc_id_ = 473618 # 294264
#    print(get_doc_descriptions(_db, doc_id_))

#    doc_ids = [473618, 467097, 465462, 449456, 441443, 438665, 427846, 422578, 410602, 406970, 401442, 397570, 393437, 322483, 316602, 314037, 304954, 294264, 272675, 272621]
    doc_ids = [316602]
    for doc_id_ in doc_ids:
        print(get_doc_location(doc_id_, only_smallest_locations=True, debug_print=False))
