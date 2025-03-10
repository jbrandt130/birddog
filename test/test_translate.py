#
#
#
import unittest
from birddog.translate import is_english, translation

# ------------------ TRANSLATE UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_translate(self):
        self.assertTrue(is_english('Hello'))
        uk_text = 'Привіт. Як справи?'
        self.assertFalse(is_english(uk_text))
        self.assertTrue(translation(uk_text) == 'Greetings. How are you doing?')
        uk_text = 'собака кішка миша'.split(' ')
        self.assertTrue(translation(uk_text) == ['dog', 'cat', 'mouse'])

if __name__ == "__main__":
    unittest.main()
