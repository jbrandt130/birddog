#
#
#
import unittest
import asyncio
import threading

from birddog.translate import (
    translation,
    translate_structure,
    )

from birddog.wiki import mw_read_page
from birddog.utility import is_english

# ------------------ TRANSLATE UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_translate(self):
        self.assertTrue(is_english('Hello'))
        uk_text = 'Привіт. Як справи?'
        self.assertFalse(is_english(uk_text))
        self.assertTrue(translation(uk_text) == 'Hello. How are you?')
        uk_text = 'собака кішка миша'.split(' ')
        self.assertTrue(translation(uk_text) == ['dog', 'cat', 'mouse'])

if __name__ == "__main__":
    unittest.main()
