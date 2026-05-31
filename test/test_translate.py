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


# ------------------ LANGUAGE DETECTION TESTS ------------------
class TestLanguageDetection(unittest.TestCase):
    """Verify auto-detection translates each source language to English."""

    def _translate(self, text):
        try:
            return translation(text)
        except TranslationDisabledError:
            self.skipTest('Translation is disabled in this configuration.')

    def test_ukrainian_sentence(self):
        src = 'Привіт. Як справи?'
        self.assertFalse(is_english(src))
        self.assertTrue(is_english(self._translate(src)))

    def test_ukrainian_batch(self):
        src = 'собака кішка миша'.split()
        self.assertEqual(self._translate(src), ['dog', 'cat', 'mouse'])

    def test_russian_sentence(self):
        src = 'Привет. Как дела?'
        self.assertFalse(is_english(src))
        self.assertTrue(is_english(self._translate(src)))

    def test_russian_batch(self):
        src = 'собака кошка мышь'.split()
        result = self._translate(src)
        self.assertEqual(len(result), 3)
        for r in result:
            self.assertTrue(is_english(r))

    def test_polish_sentence(self):
        src = 'Jak się masz?'   # 'się' is non-ASCII
        self.assertFalse(is_english(src))
        self.assertTrue(is_english(self._translate(src)))

    def test_polish_batch(self):
        src = 'pies kot mysz'.split()
        result = self._translate(src)
        self.assertEqual(len(result), 3)
        for r in result:
            self.assertTrue(is_english(r))

    def test_german_sentence(self):
        src = 'Wie schön ist das?'  # 'schön' has ö — non-ASCII
        self.assertFalse(is_english(src))
        self.assertTrue(is_english(self._translate(src)))

    def test_german_batch(self):
        src = 'Hund Katze Maus'.split()
        result = self._translate(src)
        self.assertEqual(len(result), 3)
        for r in result:
            self.assertTrue(is_english(r))


if __name__ == "__main__":
    unittest.main()
