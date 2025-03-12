# Cache objects in persistent store

import json
import os

USE_LOCAL_FILESYSTEM    = True

class CacheMissError(Exception):
    """Exception raised on cache miss.

    Attributes:
        path -- path to missed object
    """
    def __init__(self, path):
        self.path = path
        super().__init__(self.path)

if USE_LOCAL_FILESYSTEM:

    CACHE_DIR       = './cache'

    def make_path_if_needed(path):
        """Make full subdirectory hierarchy for given path."""
        pos = path.rfind('/')
        if pos >= 0:
            os.makedirs(path[:pos], exist_ok=True)

    def save_cached_object(obj, path):
        """Store JSON serialized version of object at filepath location relative to CACHE_DIR"""
        path = f'{CACHE_DIR}/{path}'
        make_path_if_needed(path)
        with open(path, 'w', encoding="utf8") as file:
            file.write(json.dumps(obj))

    def load_cached_object(object_path):
        """Return object previously saved at filepath location relative to CACHE_DIR.
        Raises CacheMissError if cache entry is missing.
        """
        path = f'{CACHE_DIR}/{object_path}'
        try:
            with open(path, encoding="utf8") as file:
                return json.loads(file.read())
        except FileNotFoundError:
            raise CacheMissError(object_path)
else:

    # placeholder for database implementation
    raise NotImplementedError
