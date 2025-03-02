
#from googletrans import Translator
from deep_translator import GoogleTranslator
from httpcore._exceptions import ReadTimeout, ConnectTimeout
import requests

def is_english(s):
    try:
        s.encode(encoding='utf-8').decode('ascii')
    except UnicodeDecodeError:
        return False
    else:
        return True

#translator = Translator()
translator = GoogleTranslator(source='uk', target='en')

def translation(text):
        #return [translation(item) for item in text]
    #print('translating: ', text)
    result = None
    wait_time = 1.
    for i in range(5):
        try:
            if isinstance(text, (list, tuple)):
                result = translator.translate_batch(text)
            else:
                result = translator.translate(text)
            break
        except (requests.Timeout, ReadTimeout, ConnectTimeout) as err:
            print('translation timeout. retrying...')
        sleep(wait_time)
        wait_time *= 2
    assert result is not None 
    return result

def translate_field(items, field_name):
    print('translate_field', items)
    batch = [item[field_name] for item in items]
    batch = translation(batch)
    for i, text in enumerate(batch):
        items[i][field_name] = text    
