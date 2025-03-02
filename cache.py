import json
import os

cache_dir = './cache'

def make_path_if_needed(fname):
    pos = fname.rfind('/')
    if pos >= 0:
        os.makedirs(fname[:pos], exist_ok=True)

def save_cached_object(obj, fname):
    fname = f'{cache_dir}/{fname}'
    make_path_if_needed(fname)
    with open(fname, 'w') as f:
        f.write(json.dumps(obj))

def load_cached_object(fname):
    fname = f'{cache_dir}/{fname}'
    with open(fname) as f:
        return json.loads(f.read())

