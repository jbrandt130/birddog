# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

# Cache objects in persistent store

import json
import os
from threading import Lock
from pathlib import Path

from birddog.logging import get_logger
_logger = get_logger()

_cache_lock = Lock() # could be overkill?

USE_LOCAL_FILESYSTEM = os.getenv("BIRDDOG_USE_LOCAL_CACHE", False) in ("true", "True", "1")

class CacheMissError(Exception):
    """Exception raised on cache miss.

    Attributes:
        path -- path to missed object
    """
    def __init__(self, path):
        self.path = path
        super().__init__(self.path)

if USE_LOCAL_FILESYSTEM:

    CACHE_DIR       = Path(__file__).resolve().parent.parent / '.cache'
    _logger.info(f'Using local folder {CACHE_DIR} for storage.')

    def _cache_path(object_path):
        return f'{CACHE_DIR}/{object_path}'

    def _make_path_if_needed(path):
        """Make full subdirectory hierarchy for given path."""
        pos = path.rfind('/')
        if pos >= 0:
            os.makedirs(path[:pos], exist_ok=True)

    def save_cached_object(obj, object_path):
        """Store JSON serialized version of object at object_path location relative to CACHE_DIR"""
        path = _cache_path(object_path)
        _make_path_if_needed(path)
        with _cache_lock:
            with open(path, 'w', encoding="utf8") as file:
                file.write(json.dumps(obj))

    def load_cached_object(object_path):
        """Return object previously saved at object_path relative to CACHE_DIR.
        Raises CacheMissError if cache entry is missing.
        """
        path = _cache_path(object_path)
        with _cache_lock:
            try:
                with open(path, encoding="utf8") as file:
                    buffer = file.read()
            except FileNotFoundError:
                raise CacheMissError(object_path)
        return json.loads(buffer)

    def remove_cached_object(object_path):
        path = _cache_path(object_path)
        if os.path.isfile(path):
            os.remove(path)

else:

    # AWS S3 interface
    import boto3

    CACHE_NAME = 'birddog-data'
    s3 = boto3.client('s3')
    bucket_created = False
    bucket_creation_lock = Lock()

    _logger.info(f'Using AWS S3 bucket {CACHE_NAME} for storage.')

    def _create_bucket():
        global bucket_created
        if bucket_created:
            return True
        with bucket_creation_lock:
            if bucket_created:
                return True
            try:
                s3.create_bucket(
                    ACL='private',
                    Bucket=CACHE_NAME,
                    CreateBucketConfiguration={
                        'LocationConstraint': 'us-east-2',
                    },
                )
            except (s3.exceptions.BucketAlreadyExists, s3.exceptions.BucketAlreadyOwnedByYou):
                # already exists
                pass
            bucket_created = True

    def _put_item(path, json_object):
        _logger.info(f"{f'saving {path}: {len(json_object)}'}")
        s3.put_object(
            Bucket=CACHE_NAME,
            Key=path,
            Body=json_object
        )

    def _get_item(path):
        try:
            response = s3.get_object(Bucket=CACHE_NAME, Key=path)
        except s3.exceptions.NoSuchKey:
            return None
        body = response['Body'].read().decode("utf-8")
        return body

    def save_cached_object(obj, object_path):
        """Store JSON serialized version of object keyed on object_path"""
        _create_bucket()
        with _cache_lock:
            _put_item(object_path, json.dumps(obj))

    def load_cached_object(object_path):
        """Return object previously saved at object_path.
        Raises CacheMissError if cache entry is missing.
        """
        _create_bucket()
        with _cache_lock:
            item = _get_item(object_path)
            if not item:
                raise CacheMissError(object_path)
        return json.loads(item)

    def remove_cached_object(object_path):
        _create_bucket()
        s3.delete_object(Bucket=CACHE_NAME, Key=object_path)
