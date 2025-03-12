import os
import unittest
from birddog.cache import (
    CACHE_DIR,
    CacheMissError,
    save_cached_object,
    load_cached_object,
    )

# ------------------ UTILITY UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_cache(self):
        path = 'unittest_object1.json'
        cache_file = f'{CACHE_DIR}/{path}'
        if os.path.isfile(cache_file):
            os.remove(cache_file)
        for object1 in [None, '', 123, [{'a': 1, 'b': 2, 'c': 'відредаговано'}, 'abc', list(range(10))]]:
            save_cached_object(object1, path)
            object1_copy = load_cached_object(path)
            self.assertTrue(object1 == object1_copy)
        with self.assertRaises(CacheMissError):
            load_cached_object('unitttest_nonexistent.json')

if __name__ == "__main__":
    unittest.main()
