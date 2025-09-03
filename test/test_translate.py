#
#
#
import unittest
import asyncio
import threading

from birddog.translate import (
    translation,
    translate_structure,
    get_translation_items,
    TranslationDisabledError,
    )

from birddog.wiki import mw_read_page
from birddog.utility import is_english

# ------------------ TRANSLATE UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_translate(self):
        self.assertTrue(is_english('Hello'))
        uk_text = 'Привіт. Як справи?'
        self.assertFalse(is_english(uk_text))
        try:
            self.assertTrue(translation(uk_text) == 'Hello. How are you?')
        except TranslationDisabledError:
            print('Translation is disabled in this configuration. Skipping further tests.')
            return
        uk_text = 'собака кішка миша'.split(' ')
        self.assertTrue(translation(uk_text) == ['dog', 'cat', 'mouse'])
        page = mw_read_page("Архів:ДАХмО/1")
        self.assertFalse(get_translation_items(page) == [])
        translate_structure(page)
        self.assertTrue(get_translation_items(page) == [])

if __name__ == "__main__":
    unittest.main()
