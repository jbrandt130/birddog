"""Translation support from Ukrainian to English"""

from time import sleep
import requests
from deep_translator import GoogleTranslator
from httpcore._exceptions import ReadTimeout, ConnectTimeout

def is_english(text):
    """True if argument is decodable to ascii (misnomer?)"""
    try:
        text.encode(encoding='utf-8').decode('ascii')
    except UnicodeDecodeError:
        return False
    return True

_translator = GoogleTranslator(source='uk', target='en')

def translation(text):
    """ Translate single text string or list/tuple of strings """
    result = None
    wait_time = 1.
    for _ in range(5):
        try:
            if isinstance(text, (list, tuple)):
                result = _translator.translate_batch(text)
            else:
                result = _translator.translate(text)
            break
        except (requests.Timeout, ReadTimeout, ConnectTimeout):
            print('translation timeout. retrying...')
        sleep(wait_time)
        wait_time *= 2
    assert result is not None
    return result
