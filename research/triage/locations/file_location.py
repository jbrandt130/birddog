import copy
import os
import string
import sys
import unicodedata

import regex

# This explicitly adds your birddog root folder to the search path safely
root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if root_path not in sys.path:
    sys.path.insert(0, root_path)
import random
import re
from typing import Any, cast

from extract_location_from_descriptors import (
    LocationExtractor,
    locations_to_admin_units,
)
from read_all_locations import LocationMatcher

from birddog.database import Database
from birddog.log import get_logger
from birddog.translate import translation

_all_ukraine_locations = [
    {"location": "Cherkasy",        "location_id": "-1037001"},
    {"location": "Chernihiv",       "location_id": "-1037057"},
    {"location": "Chernivtsi",      "location_id": "-1037073"},
    {"location": "Dnipro",          "location_id": "-1037865"},
    {"location": "Donetsk",         "location_id": "-1038078"},
    {"location": "Ivano-Frankivsk", "location_id": "-1040327"},
    {"location": "Izmail",          "location_id": "-1040491"},
    {"location":"Kamenets Podolskiy","location_id":"-1040849"},
    {"location": "Kherson",         "location_id": "-1041356"},
    {"location": "Khmelnytskyy",    "location_id": "-1041435"},
    {"location": "Kharkiv",         "location_id": "-1041320"},
    {"location": "Kyiv",            "location_id": "-1044367"},
    {"location": "Kirovohrad",      "location_id": "-1041993"},
    {"location": "Kremenchuk",      "location_id": "-1043663"},
    {"location": "Lviv",            "location_id": "-1045268"},
    {"location": "Lutsk",           "location_id": "-1045249"},
    {"location": "Luhansk",         "location_id": "-1045160"},
    {"location": "Mykolayiv",       "location_id": "-1047257"},
    {"location": "Odesa",           "location_id": "-1049092"},
    {"location": "Ostrog",          "location_id": "-1049602"},
    {"location": "Poltava",         "location_id": "-1051195"},
    {"location": "Rivne",           "location_id": "-1052476"},
    {"location": "Sevastopol",      "location_id": "-1053419"},
    {"location": "Simferopol",      "location_id": "-1054041"},
    {"location": "Sumy",            "location_id": "-1055659"},
    {"location": "Ternopil",        "location_id": "-1056204"},
    {"location": "Uzhhorod",        "location_id": "-1057311"},
    {"location": "Vinnytsya",       "location_id": "-1058303"},
    {"location": "Zaporozh'ye",     "location_id": "-1060168"},
    {"location": "Zhitomir",        "location_id": "-1060903"},
]
_podolia_locations = [
                {"location": "Khmelnytskyy",    "location_id": "-1041435"},
                {"location": "Vinnitsa",        "location_id": "-1058303"},
                {"location": "Ternopil",        "location_id": "-1056204"},
                {"location": "Odesa",           "location_id": "-1049092"},
                {"location": "Cherkasy",        "location_id": "-1037001"},
                {"location": "Kyiv",            "location_id": "-1044367"},
            ]
_volyn_locations = [
                {"location": "Lutsk",           "location_id": "-1045249"},
                {"location": "Rivne",           "location_id": "-1052476"},
                {"location": "Zhitomir",        "location_id": "-1060903"},
                {"location": "Khmelnytskyy",    "location_id": "-1041435"},
                {"location": "Ternopil",        "location_id": "-1056204"},
            ]
_kiev_locations = [
                {"location": "Kyiv",            "location_id": "-1044367"},
                {"location": "Cherkasy",        "location_id": "-1037001"},
                {"location": "Zhitomir",        "location_id": "-1060903"},
                {"location": "Vinnitsa",        "location_id": "-1058303"},
                {"location": "Kirovohrad",      "location_id": "-1041993"},
            ]
_chernigov_locations = [
                {"location":"Chernihiv",        "location_id":"-1037057"},
                {"location":"Poltava",          "location_id":"-1051195"},
                {"location":"Kyiv",             "location_id":"-1044367"},
                {"location":"Sumy",             "location_id":"-1055659"}
            ]


def remove_leading_spaces_and_punctuation(text: str) -> str:
    # Combine standard punctuation and whitespace characters
    chars_to_remove = string.punctuation + string.whitespace

    # Strip the defined characters exclusively from the left side of the string
    return text.lstrip(chars_to_remove)


def remove_pattern_words(text: str, substring: str) -> str:
    escaped_sub = re.escape(substring)

    # Regex breakdown:
    # \b                -> Word boundary at the start of the integer
    # \d+               -> One or more digits
    # \s*-\s*           -> Hyphen with optional leading/trailing spaces
    # {escaped_sub}     -> The target substring
    # \b                -> Word boundary at the end of the substring
    pattern = rf"\b\d+\s*-\s*{escaped_sub}\b"

    # Remove matching target tokens
    cleaned = re.sub(pattern, "", text)

    # Clean up leftover double spaces or spaces right before punctuation
    cleaned = re.sub(r" +([,.;])", r"\1", cleaned)
    cleaned = re.sub(r" +", " ", cleaned).strip()

    return cleaned

def chop_to_max_length(text, max_length):
    # If the text is already short enough, return it immediately
    if len(text) < max_length:
        return text

    # Loop and remove the last word until the string is short enough
    while len(text) >= max_length and " " in text:
        # rsplit(" ", 1) splits the text at the very LAST space into two parts,
        # and we only keep the first part, effectively throwing away the last word.
        text = text.rsplit(" ", 1)[0]

        # Clean up any trailing spaces left behind before checking the length again
        text = text.rstrip()

    return text


# Helper to recursively turn a dict (and any inner sets) into a hashable frozenset
def freeze_dict(d):
    return frozenset(
        (k, frozenset(v) if isinstance(v, set) else v) for k, v in d.items()
    )

def has_positive_administrative_level(entry: frozenset) -> bool:
    """Check whether a frozenset entry relates to a settlement."""
    for k, v in entry:
        if k == "administrative_level" and v > 0:
            return True
    return False

def get_unique_random_integers(n, k):
    """Returns a list of n unique random integers from 1 to k (inclusive)."""
    random.seed()
    return sorted(random.sample(range(1, k + 1), n))


def title_case_all_caps(text):
    # This pattern finds words that are completely UPPERCASE (at least 2 letters long)
    # It ensures they are bounded by your separators or the ends of the text.
    pattern = r'(?:(?<=[ ,.;"])|^)[A-Z]{2,}(?=[ ,.;"]|$)'

    # We use a lambda function to convert the matched word to title case (e.g., USHITSA -> Ushitsa)
    return re.sub(pattern, lambda m: m.group(0).title(), text)


def clean_punctuation(text: str) -> str:
    # replace sequences of punctuation marks and spaces with a single comma and a space
    pattern = r"([,/?;\-\.\s]{2,})"

    # Replace the messy sequences with a single comma and a space
    cleaned_text = re.sub(pattern, ", ", text)

    return cleaned_text.strip()


def strip_list_noise(description: str) -> str:
    """Remove narrative noise fragments that confuse the model when
    the description is essentially a list of place names.

    Targets:
    - Leading fragments like "Date changed Sep", "see also"
    - Trailing quoted commentary like '"A very confusing collection ...'
    - Stray quotes and dashes at start/end
    - Tokens like "bk", "repeat" that appear in some descriptions
    """
    # remove double quotes
    description = description.replace('"', '')

    # Drop common fragments that precede or follow a name list
    noise_fragments = [
        r"\bdate\s+changed\b",
        r"\bsee\s+also\b",
        r"\ba\s+very\s+confusing\s+collection\b",
        r"\bbk\b",
        r"\bext\b",
        r"\bper\b",
        r"\brepeat\b",
        r"\bHebrew\b",
        r"\bRussian\b",
        r"\bHeb\b",
        r"\bRus\b",
    ]
    for frag in noise_fragments:
        description = re.sub(frag, ' ', description, flags=re.IGNORECASE)
    # Remove stray quotes and dashes at the edges
    description = description.strip(' "\'')
    description = re.sub(r'^\s*-\s*', '', description)
    description = re.sub(r'\s*-\s*$', '', description)
    # Collapse stray colon-dash fragments like ": - - Kamenets"
    description = re.sub(r':\s*-\s*-+', ' ', description)
    return description


def extract_name_tokens(descriptions: set[str]) -> list[str]:
    """Heuristically extract candidate place-name tokens from descriptions.

    Used as a last-resort fallback when the LLM extraction yields nothing.
    Only tokens that look like proper place names (initial capital, letters,
    apostrophes, hyphens) are considered; noise tokens like "bk", "repeat"
    are naturally excluded by this pattern.
    """
    tokens: list[str] = []
    for description in descriptions:
        parts = re.split(r"[;,.]", description)
        for part in parts:
            # A description may contain multiple space-separated names after
            # noise fragments are removed (e.g. "Luchintsi Kamenets").
            for token in part.split():
                token = token.strip('"\'')
                if len(token) >= 3 and re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", token):
                    tokens.append(token)
    return tokens


def wrong_province(entry: frozenset) -> bool:
    """Check the location is within the archive province."""
    for k, v in entry:
        if k == "correct_province" and v:
            return False

    return True


def needs_further_analysis(identified_location: frozenset) -> bool:
    return (has_positive_administrative_level(identified_location) or
            wrong_province(identified_location))


class FileLocationFinder:
    def __init__(self, provider: str = "modal"):
        """
        For provider "modal" the API key is stored in the environment variable "HF_TOKEN";
        for provider "groq" - in the environment variable "GROQ_LOCATION_KEY".
        """
        self._logger = get_logger()
        self._db = Database()
        self._file_path = "./research/triage/locations/jg_communities_data.xlsx"
        self._location_extractor = LocationExtractor(provider)

        # all these must be lowercase
        self._province_keywords = [
            "governorate",
            "gubernia",
            "oblast",
            "province",
            "provinces",
            "region",
            "regions",
            "republic",
            "voivodeship",
            "processed via",
            "listed @",
        ]
        self._district_keywords = [
            "district",
            "districts",
            "county",
            "counties",
            "uezd",
            "uyezd",
            "volost",
            "vol.",
            "powiat",
            "diocese",
        ]
        self._district_keywords_suffix_only = ["vol"]
        self._settlement_keywords = [
            "village",
            "villages",
            "town",
            "towns",
            "township",
            "city",
            "cities",
            "settlement",
            "selsoviet",
            "precinct",
            "precincts",
            "municipality",
            "mr.",
        ]

        self._regions_2_locations = {
            "MW/Chernigov":     [{"location": "Chernihiv", "location_id": "-1037057"}],
            "MW/Chernihiv":     [{"location": "Chernihiv", "location_id": "-1037057"}],
            "MW/Ekaterinoslav": [{"location": "Dnipro",     "location_id": "-1037865"}],
            "MW/Kherson":       [{"location": "Kherson",    "location_id": "-1041356"}],
            "MW/Kyiv":          _kiev_locations,
            "MW/Podolia":       _podolia_locations,
            "MW/Poltava":       [{"location": "Poltava",    "location_id": "-1051195"}],
            "MW/Vinnitsa":      [{"location": "Vinnitsa",   "location_id": "-1058303"}],
            "MW/Yekaterinoslav": [{"location": "Dnipro",    "location_id": "-1037865"}],
            "Bessarabia":       [{"location": "Chişinău",   "location_id": "-2276223"}],
            "Bukovina":         [{"location": "Chernivtsi", "location_id": "-1037073"}],
            "CDIAK":            _all_ukraine_locations,
            "Moldavia":         [{"location": "Chişinău",   "location_id": "-2276223"}],
            "Podilia":          _podolia_locations,
            "Podillia":         _podolia_locations,
            "Podolia":          _podolia_locations,
            "Ruthenia":         [{"location": "Uzhhorod",   "location_id": "-1057311"}],
            "Taurida":          [{"location": "Simferopol", "location_id": "-1054041"}],
            "Tavriya":          [{"location": "Simferopol", "location_id": "-1054041"}],
            "Transcarpathia":   [{"location": "Uzhhorod",   "location_id": "-1057311"}],
            "Ukraine":          _all_ukraine_locations,
            "Volhynia":         _volyn_locations,
            "Volyn":            _volyn_locations,
            "Zakarpattia":      [{"location": "Uzhhorod",   "location_id": "-1057311"}],
        }

        self._archive_locations = {
            "AGAD":     {"cyrillic_abbr":"ГАДА",     "locations": [{"location":"Warszawa",         "location_id":"-534433"}]},
            "AVPRI":    {"cyrillic_abbr":"АВПРИ",    "locations": [{"location":"Moscow",           "location_id":"-2960561"}]},
            "CDIAK":    {"cyrillic_abbr":"ЦДІАК",    "locations": _all_ukraine_locations                                     },
            "DAARK":    {"cyrillic_abbr":"ДААРК",    "locations": [{"location":"Simferopol",       "location_id":"-1054041"}]},
            "DACHGO":   {"cyrillic_abbr":"ДАЧгО",    "locations": _chernigov_locations                                       },
            "DACHKO":   {"cyrillic_abbr":"ДАЧкО",    "locations": [{"location":"Cherkasy",         "location_id":"-1037001"}]},
            "DACHVO":   {"cyrillic_abbr":"ДАЧвО",    "locations": [{"location":"Chernivtsi",       "location_id":"-1037073"}]},
            "DADNO":    {"cyrillic_abbr":"ДАДнО",    "locations": [{"location":"Dnipro",           "location_id":"-1037865"}]},
            "DADO":     {"cyrillic_abbr":"ДАДоО",    "locations": [{"location":"Donetsk",          "location_id":"-1038078"}]},
            "DAHEO":    {"cyrillic_abbr":"ДАХеО",    "locations": [{"location":"Kherson",          "location_id":"-1041356"}]},
            "DAHMO":    {"cyrillic_abbr":"ДАХмО",    "locations": [{"location":"Khmelnytskyy",     "location_id":"-1041435"}]},
            "DAHO":     {"cyrillic_abbr":"ДАХО",     "locations": [{"location":"Kharkiv",          "location_id":"-1041320"}]},
            "DAIFO":    {"cyrillic_abbr":"ДАІФО",    "locations": [{"location":"Ivano-Frankivsk",  "location_id":"-1040327"}]},
            "DAK":      {"cyrillic_abbr":"ДАК",      "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "DAKIRO":   {"cyrillic_abbr":"ДАКрО",    "locations": [{"location":"Kirovohrad",       "location_id":"-1041993"}]},
            "DAKO":     {"cyrillic_abbr":"ДАКО",     "locations": _kiev_locations                                            },
            "DAKRE":    {"cyrillic_abbr":"Архівний_відділ_виконавчого_комітету_Кременчуцької_міської_ради", "locations": [{"location":"Kremenchuk",       "location_id":"-1043663"}]},
            "DALO":     {"cyrillic_abbr":"ДАЛО",     "locations": [{"location":"Lviv",             "location_id":"-1045268"}]},
            "DALUO":    {"cyrillic_abbr":"ДАЛуО",    "locations": [{"location":"Luhansk",          "location_id":"-1045160"}]},
            "DAMO":     {"cyrillic_abbr":"ДАМО",     "locations": [{"location":"Mykolayiv",        "location_id":"-1047257"}]},
            "DAOO":     {"cyrillic_abbr":"ДАОО",     "locations": [{"location":"Odesa",            "location_id":"-1049092"}]},
            "DAPO":     {"cyrillic_abbr":"ДАПО",     "locations": [{"location":"Poltava",          "location_id":"-1051195"}]},
            "DARO":     {"cyrillic_abbr":"ДАРО",     "locations": [{"location":"Rivne",            "location_id":"-1052476"}]},
            "DAS":      {"cyrillic_abbr":"ДАС",      "locations": [{"location":"Sevastopol",       "location_id":"-1053419"}]},
            "DASO":     {"cyrillic_abbr":"ДАСО",     "locations": [{"location":"Sumy",             "location_id":"-1055659"}]},
            "DATO":     {"cyrillic_abbr":"ДАТО",     "locations": [{"location":"Ternopil",         "location_id":"-1056204"}]},
            "DAVIO":    {"cyrillic_abbr":"ДАВіО",    "locations": [{"location":"Vinnitsa",         "location_id":"-1058303"}]},
            "DAVO":     {"cyrillic_abbr":"ДАВоО",    "locations": [{"location":"Lutsk",            "location_id":"-1045249"}]},
            "DAZHO":    {"cyrillic_abbr":"ДАЖО",     "locations": [{"location":"Zhitomir",         "location_id":"-1060903"}]},
            "DAZKO":    {"cyrillic_abbr":"ДАЗкО",    "locations": [{"location":"Uzhhorod",         "location_id":"-1057311"}]},
            "DAZPO":    {"cyrillic_abbr":"ДАЗпО",    "locations": [{"location":"Zaporozh'ye",      "location_id":"-1060168"}]},
            "DISZMO":   {"cyrillic_abbr":"ДІСЗМО",   "locations": [{"location":"Ostrog",           "location_id":"-1049602"}]},
            "GDA-MOD":  {"cyrillic_abbr":"ГДА МО",   "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "GDA-MVS":  {"cyrillic_abbr":"ГДА МВС",  "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "GDA-SSU":  {"cyrillic_abbr":"ГДА СБУ",  "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "GDA-SZRU": {"cyrillic_abbr":"ГДА СЗРУ", "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "ILNAN":    {"cyrillic_abbr":"Національний_музей_Тараса_Шевченка", "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "IR-NBUV":  {"cyrillic_abbr":"ІР НБУВ",  "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "KPDIMZ":   {"cyrillic_abbr":"Кам'янець-Подільський_державний_історичний_музей-заповідник", "locations": [{"location":"Kamenets Podolskiy", "location_id":"-1040849"}]},
            "KUIZA":    {"cyrillic_abbr":"КУІзА",    "locations": [{"location":"Izmail",           "location_id":"-1040491"}]},
            "NIAB":     {"cyrillic_abbr":"НГАБ",     "locations": [{"location":"Minsk",            "location_id":"-1946324"}]},
            "NBUV":     {"cyrillic_abbr":"НБУВ",     "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "OMELNIK":  {"cyrillic_abbr":"Трудовий_архів_виконавчого_комітету_Омельницької_сільської_ради_Кременчуцького_району_Полтавської_області", "locations": [{"location":"Kremenchuk",       "location_id":"-1043663"}]},
            "OMR":      {"cyrillic_abbr":"OMR",      "locations": [{"location":"Odesa",            "location_id":"-1049092"}]},
            "ONU":      {"cyrillic_abbr":"ОНУ",      "locations": [{"location":"Odesa",            "location_id":"-1049092"}]},
            "RGADA":    {"cyrillic_abbr":"РДАДА",    "locations": [{"location":"Moscow",           "location_id":"-2960561"}]},
            "RGIA":     {"cyrillic_abbr":"РДІА",     "locations": [{"location":"Leningrad",        "location_id":"-2996338"}]},
            "TSDAHOU":  {"cyrillic_abbr":"ЦДАГОУ",   "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "TSDAVO":   {"cyrillic_abbr":"ЦДАВО",    "locations": [{"location":"Kyiv",             "location_id":"-1044367"}]},
            "TSDIAL":   {"cyrillic_abbr":"ЦДІАЛ",    "locations": [{"location":"Lviv",             "location_id":"-1045268"}]}
        }

        # modern UKRAINIAN region centres that are or once were the province capitals
        self._province_capitals = {
            "Basarabia": [],
            "Bessarabia": [],
            "Bucovina": [{"location": "Chernivtsi",      "location_id": "-1037073"}],
            "Cherkasy": [{"location": "Cherkasy",        "location_id": "-1037001"}],
            "Chernigov ": [{"location":"Chernihiv",        "location_id":"-1037057"}],
            "Don Voyska": [{"location": "Cherkasy",        "location_id": "-1037001"}],
            "Ekaterinoslav": [{"location": "Dnipro",     "location_id": "-1037865"}],
            "Galicia": [{"location": "Lviv",            "location_id": "-1045268"}],
            "KÃ¡rpÃ¡talja": [{"location": "Uzhhorod",        "location_id": "-1057311"}],
            "Kharkov": [{"location": "Kharkiv",         "location_id": "-1041320"}, {"location": "Poltava",         "location_id": "-1051195"}],
            "Kherson": [{"location": "Kherson",         "location_id": "-1041356"}],
            "Kiev": [{"location": "Kyiv",            "location_id": "-1044367"}],
            "LwÃ³w": [{"location": "Lviv",            "location_id": "-1045268"}],
            "Lwow": [{"location": "Lviv",            "location_id": "-1045268"}],
            "Minsk": [],
            "Moldavia": [],
            "Moldavian ASSR": [],
            "Mykolayiv": [{"location": "Mykolayiv",       "location_id": "-1047257"}],
            "Nikolaiev": [{"location": "Mykolayiv",       "location_id": "-1047257"}],
            "Podolia": [{"location":"Vinnitsa",         "location_id":"-1058303"}, {"location":"Kamenets Podolskiy","location_id":"-1040849"}],
            "Polesie": [],
            "Poltava": [{"location": "Poltava",         "location_id": "-1051195"}],
            "Russian SSR": [],
            "Slovakia": [],
            "StanisÅ‚awÃ³w": [{"location": "Ivano-Frankivsk", "location_id": "-1040327"}],
            "Stanislawow": [{"location": "Ivano-Frankivsk", "location_id": "-1040327"}],
            "Subcarpathia": [{"location": "Uzhhorod",        "location_id": "-1057311"}],
            "Tarnopol": [{"location": "Ternopil",        "location_id": "-1056204"}],
            "Taurida": [{"location": "Simferopol",      "location_id": "-1054041"}],
            "Transcarpathia": [{"location": "Uzhhorod",        "location_id": "-1057311"}],
            "Ukraine SSR": _all_ukraine_locations,
            "Vinnitsa": [{"location": "Vinnitsa",        "location_id": "-1058303"}],
            "Vinnytsya": [{"location": "Vinnitsa",        "location_id": "-1058303"}],
            "Volhynia": [{"location": "Zhitomir",        "location_id": "-1060903"}],
            "WoÅ‚yÅ„": _volyn_locations
        }

        self._matcher = LocationMatcher(self._file_path, self._regions_2_locations, self._province_capitals)

    def delete_nuisance_words(self, descriptions: set[str], debug_print: bool = False) -> set[str]:
        all_words_to_delete = [
            "board",
            "burgher",
            "burghers",
            "court",
            "Peace",
            "Justice",
            "Judicial",
            "Investigative",
            "Sentence",
            "Sentences",
            "the",
            "statistical",
            "economic",
            "historical",
            "philological",
            "educational",
            "medical",
            "governmental",
            "government",
            "institution",
            "institutions",
            "official",
            "officials",
            "committee",
            "ministry",
            "Office",
            "Department",
            "Fund",
            "Funds",
            "council",
            "councils",
            "duma",
            "State",
            "Archive",
            "Archives",
            "ministers",
            "University",
            "institute",
            "gymnasium",
            "statistics",
            "Conscription",
            "branch",
            "Agency",
            "Society",
            "Community",
            "Judgment",
            "Judgments",
            "rural",
            "bourgeois",
            "Men's",
            "Women's",
            "station",
            "accounting",
            "counting",
            "part"
        ]

        # delete also the religious terms
        religious_terms = [
            "Roman Catholic",
            "churches",
            "Church",
            "synagogue",
            "Jewish",
            "Jews",
            "rabbinate",
            "Prayer",
            "synod",
            "Spiritual",
            "Theological",
            "Seminary",
            "Consistory",
            "Orthodox",
            "Assumption",
            "deanery",
            "clergy",
            "cemetery",
            "suburb",
            "suburbs",
            "tserkovny",
            "Trinity",
            "Resurrection",
            "Ascension",
            "Intercession",
            "Annunciation",
            "Transfiguration",
        ]
        all_words_to_delete.extend(religious_terms)

        # delete also the documentation/archival terms
        archival_noise = [
            "about",
            "absent",
            "after",
            "birth",
            "births",
            "book",
            "books",
            "born",
            "census",
            "confessional",
            "death",
            "deaths",
            "deceased",
            "Decree",
            "Decrees",
            "div",
            "divorce",
            "divorced",
            "divorces",
            "document",
            "documents",
            "enumeration",
            "file",
            "files",
            "folder",
            "folders",
            "index",
            "journal",
            "journals",
            "Letter",
            "Letters",
            "magistrate",
            "marriage",
            "marriages",
            "matriculation",
            "meeting",
            "meetings",
            "metric",
            "Metrical",
            "prior",
            "record",
            "records",
            "registry",
            "register",
            "registers",
            "sheet",
            "sheets",
            "to",
            "tract",
            "year",
            "years",
        ]
        all_words_to_delete.extend(archival_noise)

        archive_abbreviations = list(self._archive_locations)
        shortened_descriptions = set()
        for description in descriptions:
            # remove Cyrillic characters
            # \p{Cyrillic} matches any character in the Cyrillic script
            description = regex.sub(r'[\u0400-\u04FF]', '', description)

            description = remove_words_list(description, archive_abbreviations, True)
            description = remove_words_list(description, all_words_to_delete, True)

            description = replace_word(description, "regional", "region")
            description = replace_word(description, "provincial", "province")
            description = replace_word(description, "parish", "village")
            description = replace_word(description, "city", "town")
            
            # case-insensitive replacement
            description = re.sub(re.escape(" M."), " village", description, flags=re.IGNORECASE)
            description = re.sub(re.escape(" Mr."), " village", description, flags=re.IGNORECASE)
            description = re.sub(re.escape(" St."), " village", description, flags=re.IGNORECASE)

            description = description.replace(" of ", " ")
            description = description.replace(" and ", ", ")
            description = description.replace(" (", ", ")
            description = description.replace("-(", ", ")
            description = description.replace(")", ", ")

            # remove http links: this pattern finds "http" and matches all characters until it hits a space
            description = re.sub(r"http\S*", "", description)

            # deleting  an integer, followed by a hyphen and word, like "51-div"
            description = remove_pattern_words(description, "div")
            description = remove_pattern_words(description, "divorces")
            description = remove_pattern_words(description, "born")
            description = remove_pattern_words(description, "marriages")
            description = remove_pattern_words(description, "deceased")

            # replace words looking like ' -  334' with spaces
            description = replace_hyphen_with_number(description)

            # We replace the found integers with an empty string
            description = re.sub(r"(?:(?<=[ \-,.;])|^)\d+(?=[ \-,.;]|$)", "", description)

            # Deletes single letters, maybe preceded by a hyphen
            description = re.sub(r"(?:(?<=[ ,.;])|^)-?[a-zA-Z](?=[ ,.;]|$)", "", description)

            # Deletes ordinals like "12th", "2nd", "1st".
            description = re.sub(r'\b\d+(?:st|nd|rd|th)\b', "", description)

            # do not allow all capital words - they too confuse the model
            description = title_case_all_caps(description)

            # --- STEP 1: REMOVALS (Order matters!) ---
            # 1. First, delete the single letters while they still have their dashes (e.g., "- B", "- M", "- D")
            description = re.sub(r"-\s*[A-Z]\b", "", description)

            # Strip narrative noise fragments that confuse the model when
            # the description is a list of place names (e.g. "Date changed Sep;",
            # "see also", "A very confusing collection", stray quotes/dashes).
            description = strip_list_noise(description)

            description = clean_punctuation(description)

            # Remove commas right before the closing quote (like ', "')
            description = re.sub(r',\s*"', '"', description)

            # --- STEP 3: BEAUTIFY SPACING ---
            # Pull punctuation tight to the word behind it
            description = re.sub(r"\s+,", ",", description)
            description = re.sub(r"\s+;", ";", description)
            # Add exactly one clean space AFTER commas and semicolons
            description = re.sub(r",\s*", ", ", description)
            description = re.sub(r";\s*", "; ", description)
            # Standardize double spaces down to single spaces
            description = re.sub(r" +", " ", description)
            # remove leading spaces_and punctuation
            description = remove_leading_spaces_and_punctuation(description)

            max_length = 200
            description = chop_to_max_length(description, max_length)

            if description and len(description) > 2:
                shortened_descriptions.add(description)
                if debug_print:
                    print(f"Description shortened to '{description}'")

        return shortened_descriptions

    def find_archive_name_by_cyrillic_abbr(self, cyrillic_abbr: str) -> str | None:
        """
        Searches self._archive_locations for a matching 'cyrillic_abbr'
        and returns the main key (e.g., 'AGAD') if found.
        """
        for key, data in self._archive_locations.items():
            if data.get("cyrillic_abbr") == cyrillic_abbr:
                return key
        return None

    def get_doc_locations_batched(
        self,
        doc_ids: list[int],
        batch_size: int = 20,
        only_smallest_locations: bool = True,
        debug_print: bool = False,
    ) -> dict[int, tuple[list[str], list[dict], list[list[dict]], set[str]]]:
        """Identifies locations for multiple documents in batched API calls.

        For each document, descriptions are split into priority1 (doc description,
        page description, translated title parts) and priority2 (owning_pages /
        storage unit descriptions). The AI extraction is run for priority1 always,
        and priority2 is only consulted if at least one document has no identified
        locations from priority1.

        Args:
            doc_ids: List of document IDs to process.
            batch_size: Maximum number of documents per API call (default 20).
            only_smallest_locations: Passed through to the per-doc location matching.
            debug_print: Passed through to per-doc location matching.

        Returns:
            dict[int, tuple[list[str], list[dict], list[dict]]]: mapping from doc_id to list of
            location IDs, archive locations, and extended archive location list including those found in descriptions.
        """
        # Step 1: Gather descriptors + archive locs + region centres for every doc upfront.
        # We now store two separate description lists per doc (priority1 and priority2).
        per_doc_inputs: list[dict] = []
        all_p1_lists: list[list[str]] = []
        all_p2_lists: list[list[str]] = []
        doc_archive_locs_lists: dict = {}
        for doc_id in doc_ids:
            if debug_print:
                print(f"***** Processing descriptions for document {doc_id} *****")
            priority1, priority2, doc_archive_locs = self.get_doc_descriptions(doc_id, debug_print)
            doc_archive_locs_lists[doc_id] = doc_archive_locs 
            priority1, region_centres_p1 = self.extract_region_centres(priority1, debug_print)
            priority1 = self.delete_nuisance_words(priority1, debug_print)
            priority2, region_centres_p2 = self.extract_region_centres(priority2, debug_print)
            priority2 = self.delete_nuisance_words(priority2, debug_print)

            # flatten list[list[dict]] -> list[dict] (dicts unhashable, so set().union won't work)
            united_centre_list = ([d for sublist in region_centres_p1 for d in sublist] +
                                  [d for sublist in region_centres_p2 for d in sublist] + doc_archive_locs)
            # remove duplicates
            united_centre_list = [dict(t) for t in {tuple(sorted(d.items())) for d in united_centre_list}]
            additional_centers = region_centres_p1 + region_centres_p2
            # remove duplicates
            additional_centers = [[dict(t) for t in {tuple(sorted(d.items())) for d in item}] for item in additional_centers]
            
            per_doc_inputs.append({
                "united_centre_list": united_centre_list,
                "additional_centers": additional_centers,
                "has_priority2": bool(priority2),
            })
            all_p1_lists.append(list(priority1))
            all_p2_lists.append(list(priority2))

        # Step 2: Batched AI extraction — priority1 first, then priority2 only if needed.
        # Only run priority2 if at least one document has no priority1 results.
        docs_needing_p2 = []
        identified_locations_per_doc = []
        district_names_per_doc = [set()] * len(doc_ids)
        all_extracted_p1 = self._location_extractor.extract_locations_batched(
            all_p1_lists, batch_size=batch_size, debug_print=debug_print
        )
        for doc_idx, doc_id in enumerate(doc_ids):
            extracted_p1 = all_extracted_p1[doc_idx]
            if debug_print and extracted_p1:
                print(f"***** Extracted locations for document {doc_id}: {extracted_p1} *****")
            district_names = district_names_per_doc[doc_idx]
            identified_locations, district_names = self.match_places_to_location_ids(doc_id,
                extracted_p1, doc_archive_locs_lists.get(doc_id, []),
                per_doc_inputs[doc_idx]["additional_centers"], district_names, debug_print)
            identified_locations_per_doc.append(identified_locations)
            district_names_per_doc[doc_idx] = district_names
            no_settlements_found = all(needs_further_analysis(e)
                                       for e in identified_locations) if identified_locations else True
            if no_settlements_found:
                docs_needing_p2.append(doc_idx)

        # Build priority2 lists only for those docs
        if docs_needing_p2:
            # Only include priority2 lists for docs that need it
            filtered_p2_lists = [all_p2_lists[i] if i in docs_needing_p2 else [] for i in range(len(doc_ids))]
            all_extracted_p2 = self._location_extractor.extract_locations_batched(
                filtered_p2_lists, batch_size=batch_size, debug_print=debug_print
            )
            # Fallback: only consult priority2 if priority1 produced nothing
            for doc_idx, doc_id in enumerate(doc_ids):
                identified_locations = identified_locations_per_doc[doc_idx]
                extracted_p2 = all_extracted_p2[doc_idx]
                if debug_print and extracted_p2:
                    print(f"***** Second extraction for document {doc_id}: {extracted_p2} *****")
                    district_names = district_names_per_doc[doc_idx]
                    identified_locations_p2, district_names = self.match_places_to_location_ids(doc_id,
                        extracted_p2, doc_archive_locs_lists.get(doc_id, []),
                        per_doc_inputs[doc_idx]["additional_centers"], district_names, debug_print)
                    identified_locations |= identified_locations_p2
                    identified_locations_per_doc[doc_idx] = identified_locations
                    district_names_per_doc[doc_idx] = district_names # include those from priority1

        # Step 3: Dispatch results back to each doc with the priority fallback rule:
        # try priority1; only fall back to priority2 if priority1 yielded nothing.
        results: dict[int, tuple[list[str], list[dict], list[list[dict]], set[str]]] = {}
        for doc_idx, doc_id in enumerate(doc_ids):
            identified_locations = identified_locations_per_doc[doc_idx]
            if not identified_locations:
                if doc_archive_locs_lists[doc_id]:
                    centre_list = doc_archive_locs_lists[doc_id]
                else:
                    centre_list = per_doc_inputs[doc_idx]["united_centre_list"]
                for location in centre_list:
                    loc_id = location["location_id"]
                    location = self._matcher.location_name_dict.get(loc_id)
                    if location:
                        location = copy.deepcopy(location)
                        location.pop("province_capital_ids_array", None)
                        location.pop("district_names", None)
                        location.pop("province_names", None)

                        location["administrative_level"] = 2
                        location["loc_id"] = loc_id
                        hashable_items = (
                            (k, frozenset(v) if isinstance(v, set) else v)
                            for k, v in location.items()
                        )
                        identified_locations.add(frozenset(hashable_items))
                        if only_smallest_locations:
                            break

            identified_locations_dict_list = [dict(f_set) for f_set in identified_locations]

            if only_smallest_locations:
                target_level = min(loc["administrative_level"] for loc in identified_locations_dict_list)
                result = [dict_loc["loc_id"] for dict_loc in identified_locations_dict_list
                    if dict_loc.get("administrative_level") == target_level]

                if debug_print:
                    names = [d.get("main_name") for d in identified_locations_dict_list
                        if d.get("administrative_level") == target_level and d.get("main_name")]
                    msg = "Most specific locations: " + " ".join(f"'{name}'" for name in names)
                    print(msg)

            else:
                result = [dict_loc["loc_id"] for dict_loc in identified_locations_dict_list]

            results[doc_id] = (result, doc_archive_locs_lists[doc_id],
                               per_doc_inputs[doc_idx]["additional_centers"], district_names_per_doc[doc_idx])

        return results

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
        seen = set()
        unique_locs = []
        for item in archive_locs:
            if "locations" in item:
                locs = item["locations"]
                for loc in locs:
                    loc_id = loc["location_id"]
                    # Convert to frozenset if it is a dictionary, otherwise leave it as is
                    key = (
                        frozenset(loc_id.items())
                        if isinstance(loc_id, dict)
                        else loc_id
                    )

                    if key not in seen:
                        seen.add(key)
                        unique_locs.append(loc)

        return unique_locs

    def extract_region_centres(self, descriptions: set[str], debug_print: bool = False)-> tuple[set[str], list[list[dict]]]:
        descriptions_after_extraction = []
        total_region_centres = []
        for description in descriptions:
            description_after_extraction, region_centres = self.remove_sentences_with_words(description, debug_print)
            if description_after_extraction:
                descriptions_after_extraction.append(description_after_extraction)
            total_region_centres.append(
                [rc for rc in region_centres if rc not in total_region_centres]
            )

        descriptions_after_extraction = set(descriptions_after_extraction)

        return descriptions_after_extraction, total_region_centres
        
    def remove_sentences_with_words(self, text: str, debug_print: bool = False) -> tuple[str, list[dict]]:
        archive_abbreviations = set(self._regions_2_locations.keys())
        archive_cities = {loc["location"] for loc in _all_ukraine_locations}
        all_keywords_original_case = archive_abbreviations | archive_cities
        # Convert target words to lowercase for case-insensitive matching
        abbreviations_to_check = {word.lower() for word in archive_abbreviations}
        cities_to_check = {city.lower() for city in archive_cities}
        words_not_to_delete = set(self._settlement_keywords) | set(self._district_keywords)

        # Normalize Unicode
        text = unicodedata.normalize("NFKC", text)
        # Split text by dots and semicolons (delimiters consumed)
        sentences = re.split(r"([.,;])", text)

        cleaned_pieces = []
        found_words = set()

        for sentence in sentences:
            if not sentence.strip():
                continue

            # Clean sentence punctuation to isolate words (keep slashes)
            clean_words = set(re.findall(r"\b[\w/]+\b", sentence.lower()))

            # are there words not to delete?
            matches = words_not_to_delete.intersection(clean_words)
            if matches:
                cleaned_pieces.append(sentence)
                continue

            # Find matches between this sentence and target words
            #first try the cities
            matches = cities_to_check.intersection(clean_words)
            if matches:
                found_words.update(matches)
                if debug_print:
                    print(f"Deleting the sentence: '{sentence}'")
            else:
                # now try the archive abbreviations
                matches = abbreviations_to_check.intersection(clean_words)
                if matches:
                    found_words.update(matches)
                    if debug_print:
                        print(f"Deleting the sentence: '{sentence}'")
                else:
                    cleaned_pieces.append(sentence)

        # Map lowercase found words back to their original casing from target_words
        original_casing_found = {word for word in all_keywords_original_case if word.lower() in found_words}
        region_centres: list[dict[str, str]] = []
        for key in original_casing_found:
            if key in self._regions_2_locations:
                region_centres.extend(self._regions_2_locations[key])
            else:
                for loc in _all_ukraine_locations:
                    if loc["location"] == key:
                        region_centres.append(loc)
                        break

        return "".join(cleaned_pieces).strip(), region_centres


    def get_doc_descriptions(self, doc_id: int, debug_print: bool = False) -> tuple[set[str], set[str], list[dict]]:
        """Retrieves and compiles descriptions and storage locations for a document.

        Descriptions are split into two priority tiers:
        - priority1: doc_description, page_description, translated_cyrillic, other_space_str
        - priority2: owning_pages descriptions (traversed hierarchically)

        Args:
            doc_id (int): The unique identifier of the target document.
            debug_print (bool, optional): If True, prints status messages when
                records are missing. Defaults to False.

        Returns:
            tuple[set[str], set[str], list[dict]]:
                - priority1 descriptions (doc description, page description, translated title parts)
                - priority2 descriptions (owning_pages / storage unit descriptions)
                - doc_archive_locs (archive location dictionaries)
        """
        doc_rec = get_doc_record(self._db, doc_id)
        if not doc_rec:
            print(f"Could not find document with id {doc_id}")
            return set(), set(), []

        doc_description = doc_rec.get("description")
        doc_comments = doc_rec.get("comments")
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

        priority1 = set()
        priority2 = set()

        # Priority 1: document-level descriptions
        if doc_description:
            priority1.add(doc_description)
        if doc_comments:
            priority1.add(doc_comments)
        if page_description:
            priority1.add(page_description)
        if other_space_str:
            priority1.add(other_space_str)
        if translated_cyrillic:
            priority1.add(str(translated_cyrillic))

        # Priority 2: owning_pages descriptions (storage units)
        while owning_pages:
            upper_level_pages = []
            for page in owning_pages:
                page_id = page.get("Id")
                page_rec = cast(dict, self._db.read("Pages", page_id))
                descr = page_rec.get("description")
                if descr:
                    priority2.add(descr)
                upper_level_page = page_rec.get("parent")
                if upper_level_page:
                    if  any(ulp.get('Id') == page_id for ulp in upper_level_page):
                        print(f"Error: the upper level page for page ID {page_id}, "
                              f"title {page.get('title')} is this same page!!")
                        upper_level_pages = []
                        break
                    upper_level_pages.extend(upper_level_page)

            owning_pages = upper_level_pages

        if debug_print:
            print(f"Priority1 descriptions ({len(priority1)}): {priority1}")
            print(f"Priority2 descriptions ({len(priority2)}): {priority2}")
        return priority1, priority2, doc_archive_locs

    def scan_database(self, **kwargs):
        """Public wrapper to safely access the internal database scan."""
        return self._db.scan(**kwargs)

    def get_location_from_id(self, loc_id: str):
        return self._matcher.location_name_dict.get(loc_id)


    def match_places_to_location_ids(self, doc_id: int, extracted_places: list[str], doc_archive_locs: list[dict],
            additional_centers: list[list[dict]], additional_districts: set[str], 
            debug_print: bool) ->  tuple[set[frozenset[tuple[str, Any]]], set[str]]:
        """Resolve AI-extracted place names to canonical location records.

        Takes raw location strings produced by the LLM extraction step and maps
        each one to a location record in the reference database. Because the same
        place name can appear in multiple districts or provinces, disambiguation
        uses the administrative context (district/province names) that was also
        extracted alongside the place name:

        1. Prefer a candidate whose district matches an extracted district name.
        2. Fall back to a candidate whose province matches an extracted province name.
        3. As a last resort, take the first candidate ID.

        Each resolved record is annotated with the extracted administrative_level
        and loc_id, then stored as a hashable frozenset in the returned set.

        Args:
            doc_id: Document being processed (used for debug logging only).
            extracted_places: Raw location name strings from the AI extraction step.
            doc_archive_locs: Archive location for the archives this document relates to.
            additional_centers: Archive locations from descriptions.
            debug_print: If True, prints the resolved location for each match.

        Returns:
            A set of frozensets, each representing one identified location record
            with keys like 'main_name', 'location_id', 'administrative_level',
            and 'loc_id'.
        """
        loc_admin_units, found_province_names, district_names = locations_to_admin_units(extracted_places,
            self._province_keywords, self._district_keywords,
            self._district_keywords_suffix_only, self._settlement_keywords, debug_print)
        # add the previously known districts
        district_names |= additional_districts
        # add the archive location provinces
        archive_loc_ids = {loc["location_id"] for loc in doc_archive_locs}
        additional_centre_ids = [{loc["location_id"] for loc in item} for item in additional_centers]

        # found_province_names->found_province_ids
        found_province_ids = set()
        for province_name in found_province_names:
            curr_province_ids = self._matcher.find_location_id(province_name, False)
            found_province_ids = found_province_ids | set(curr_province_ids)

        archive_province_center_ids = [archive_loc_ids, found_province_ids] + additional_centre_ids
        archive_province_center_ids = [s for s in archive_province_center_ids if s]
        archive_province_center_ids = sorted(archive_province_center_ids, key=len)

        identified_locations = set()
    
        # Try to match all locations from each extraction
        for loc_admin_unit in loc_admin_units:
            extracted_loc_name = loc_admin_unit["location"]
            place_ids = self._matcher.find_location_id(extracted_loc_name, debug_print)
            found_admin_match = False
            final_id = None
            correct_province: bool = True  # default
            for place_id in place_ids:
                location = self._matcher.location_name_dict.get(place_id)
                # there may be more than 1 location with this name. is the district right?
                if location is not None and not district_names.isdisjoint(location["district_names"]):
                    found_admin_match = True
                    final_id = place_id
                    break
    
            if not found_admin_match:
                # how many province capitals can there be?
                max_num_capitals = 1
                for place_idx, place_id in enumerate(place_ids):
                    location = self._matcher.location_name_dict.get(place_id)
                    if not location:
                        continue
                    province_capital_ids_array = location.get("province_capital_ids_array", set())
                    if not province_capital_ids_array:
                        continue
                    max_num_capitals = max(max_num_capitals, len(province_capital_ids_array[-1]))
                max_num_archive_locs = 1
                if archive_province_center_ids:
                    max_num_archive_locs = len(archive_province_center_ids[-1])
                max_sum = max_num_capitals + max_num_archive_locs + 1

                # no alternative location with the right district is found, check with the province
                # we compare pairs of document province capitals and the location province capitals,
                # which are both lists of sets of IDs. We start with pairs of sets with the smallest size sum,
                # and then go to the larger sums.
                for sum_sel_len in range(1, max_sum):
                    if found_admin_match:
                        break
                    for archive_set in archive_province_center_ids:
                        if found_admin_match:
                            break
                        archive_set_len = len(archive_set)
                        for place_idx, place_id in enumerate(place_ids):
                            location = self._matcher.location_name_dict.get(place_id)
                            if not location:
                                continue
                            province_capital_ids_array = location.get("province_capital_ids_array", set())
                            if not province_capital_ids_array:
                                continue
                            for doc_set in province_capital_ids_array:
                                if archive_set_len + len(doc_set) <= sum_sel_len:
                                    if not doc_set.isdisjoint(archive_set):
                                        found_admin_match = True
                                        final_id = place_id
                                        break
                                else:
                                    # entries in province_capital_ids_array are sorted by ascending length
                                    break

            if not found_admin_match and place_ids:
                correct_province = False
                final_id = place_ids[0]

            if final_id is not None:
                location = self._matcher.location_name_dict.get(final_id)
                if location:
                    try:
                        location = copy.deepcopy(location)
                        location.pop("province_capital_ids_array", None)
                        location.pop("district_names", None) # these were arrays
                        location.pop("province_names", None)
    
                        location["administrative_level"] = loc_admin_unit["administrative_level"]
                        location["loc_id"] = final_id
                        location["correct_province"] = correct_province
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
                                  f"'{loc_name}', {adm_status}, correct province: {correct_province}, ID={final_id}")
                    except TypeError:
                        print(f"TypeError in match_places_to_location_ids for location {location}")
                        raise  # re-raise the same exception

        return identified_locations, district_names


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


def replace_hyphen_with_number(text: str) -> str:
    """Repeatedly removes or replaces hyphen-number patterns until none remain."""
    pattern_start = r"^-\s*\d+"
    pattern_after = r"(?<=[ ,.])-\s*\d+"

    while True:
        new_text = re.sub(pattern_start, "", text)
        new_text = re.sub(pattern_after, " ", new_text)
        if new_text == text:
            break
        text = new_text

    return text

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
    finder = FileLocationFinder()
    finder.get_doc_locations_batched([60426], 1, only_smallest_locations=False, debug_print=True)
#    doc_id_ = 12953
#    print(finder.get_doc_descriptions(doc_id_))


