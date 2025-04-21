#
#
#
import unittest
import asyncio
import threading

from birddog.translate import (
    is_english, 
    translation,
    )

from birddog.wiki import read_page
from birddog.utility import translate_page

# ------------------ TRANSLATE UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_translate(self):
        self.assertTrue(is_english('Hello'))
        uk_text = 'Привіт. Як справи?'
        self.assertFalse(is_english(uk_text))
        self.assertTrue(translation(uk_text) == 'Hello. How are you?')
        uk_text = 'собака кішка миша'.split(' ')
        self.assertTrue(translation(uk_text) == ['dog', 'cat', 'mouse'])

    def test_full_translation_cycle(self):
        page = read_page("https://uk.wikisource.org/wiki/%D0%90%D1%80%D1%85%D1%96%D0%B2:%D0%94%D0%90%D0%9A%D0%9E/1/7")

        completed = threading.Event()
        progress_steps = []
        result_holder = {}

        def on_progress(task_id, progress, total):
            print(f'translation progress ({task_id}): {progress}/{total}')
            progress_steps.append((progress, total))

        def on_complete(task_id, result):
            print(f'translation complete ({task_id}): {result}')
            result_holder["result"] = result
            completed.set()

        translate_page(
            page=page,
            dry_run=False,
            asynchronous=True,
            progress_callback=on_progress,
            completion_callback=on_complete
        )

        success = completed.wait(timeout=10)
        self.assertTrue(success, "Translation task did not complete in time")

        # Validate completion
        self.assertIn("result", result_holder)
        result = result_holder["result"]
        self.assertIsInstance(result, list)
        #self.assertIn("translated_text", result)

        # Validate progress updates were received
        self.assertTrue(progress_steps)
        #self.assertTrue(all(0 <= p <= 100 for p in progress_steps))

if __name__ == "__main__":
    unittest.main()
