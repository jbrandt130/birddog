from copy import copy
import unittest
from birddog.utility import (
    lastmod,
    is_numeric,
    form_text_item,
    equal_text,
    get_text,
    needs_translation,
    translate_page,
    )

# ------------------ UTILITY UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_lastmod(self):
        message = "Цю сторінку востаннє відредаговано о 19:15, 20 травня 2023."
        self.assertTrue(lastmod(message) == "2023,05,20,19:15")
        self.assertTrue(lastmod('xyz') == 'xyz')

    def test_multilingual(self):
        for text in ['1', '12-13', '098-101']:
            self.assertTrue(is_numeric(text))
        for text in ['', 'a12', '-13', '098-', 'x1']:
            self.assertFalse(is_numeric(text))
        for text in ['123', '', 'сторінку']:
            item1a = form_text_item(text, translate=False)
            item1b = form_text_item(text, translate=False)
            item2 = form_text_item(text, translate=True)
            self.assertTrue(equal_text(item1b, item2))
            self.assertTrue(equal_text(item1a, item2))
            self.assertTrue(get_text(item1a) == text)
        self.assertTrue(
            needs_translation(
                form_text_item('відредаговано', translate=False)))
        self.assertFalse(
            needs_translation(
                form_text_item('відредаговано', translate=True)))
        self.assertFalse(needs_translation(form_text_item('123')))
        self.assertFalse(needs_translation(form_text_item('')))
        item1 = {
            'abc': [1, 2], 
            'def': [form_text_item('востаннє'), 5], 
            'ghi': {'abc': form_text_item("сторінку")}
            }
        item2 = copy(item1)
        item2_translated = {
            'abc': [1, 2], 
            'def': [{'uk': 'востаннє', 'en': 'last'}, 5], 
            'ghi': {'abc': {'uk': 'сторінку', 'en': 'page'}}
            }
        translate_page(item2)
        self.assertTrue(item2 == item2_translated)

if __name__ == "__main__":
    unittest.main()
