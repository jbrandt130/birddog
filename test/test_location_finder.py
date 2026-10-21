import unittest
from unittest.mock import MagicMock

from birddog.locations.finder import FileLocationFinder


def make_finder(archive_locs, region_centres, location_name_dict, extracted):
    """Bypasses __init__ (which loads resources and the Locations table) and
    stubs the per-step helpers get_doc_locations_batched calls."""
    finder = FileLocationFinder.__new__(FileLocationFinder)
    finder._logger = MagicMock()
    finder.get_doc_descriptions = lambda doc_id: ({"p1"}, set(), list(archive_locs))
    finder.extract_region_centres = lambda descs: (descs, [list(region_centres)] if descs else [])
    finder.delete_nuisance_words = lambda descs: descs
    finder.match_places_to_location_ids = lambda doc_id, places, *args: (set(), args[-1])
    finder._location_extractor = MagicMock()
    finder._location_extractor.extract_locations_batched.side_effect = lambda lists, batch_size: [
        extracted if items else [] for items in lists]
    finder._matcher = MagicMock(location_name_dict=location_name_dict)
    return finder


class TestCentreFallback(unittest.TestCase):
    def test_falls_back_to_smallest_region_centre_id(self):
        # nothing matched, no archive locations: the region centres from the
        # descriptions are used, keyed by location_id (regression: the sort
        # used "loc_id", which these entries don't have)
        centres = [{"location": "Zhitomir", "location_id": "-1060903"},
                   {"location": "Lutsk", "location_id": "-1045249"}]
        names = {"-1060903": {"main_name": "Zhitomir"}, "-1045249": {"main_name": "Lutsk"}}
        finder = make_finder([], centres, names, ["Nowhere"])
        error, loc_ids, *_ = finder.get_doc_locations_batched([1])[1]
        self.assertFalse(error)
        # only_smallest_locations keeps just the first after sorting
        self.assertEqual(loc_ids, ["-1045249"])

    def test_extraction_failure_flags_error(self):
        finder = make_finder([], [], {}, None)
        error, loc_ids, *_ = finder.get_doc_locations_batched([1])[1]
        self.assertTrue(error)
        self.assertEqual(loc_ids, [])


if __name__ == "__main__":
    unittest.main()
