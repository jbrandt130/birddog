# Cache objects in persistent store

import json
import os

USE_LOCAL_FILESYSTEM    = False # True

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
        with open(path, 'w', encoding="utf8") as file:
            file.write(json.dumps(obj))

    def load_cached_object(object_path):
        """Return object previously saved at object_path relative to CACHE_DIR.
        Raises CacheMissError if cache entry is missing.
        """
        path = _cache_path(object_path)
        try:
            with open(path, encoding="utf8") as file:
                return json.loads(file.read())
        except FileNotFoundError:
            raise CacheMissError(object_path)

    def remove_cached_object(object_path):
        path = _cache_path(object_path)
        if os.path.isfile(path):
            os.remove(path)
        
else:

    # AWS dynamodb interface
    import boto3
    from botocore.exceptions import ClientError

    CACHE_TABLE_NAME  = 'birddog_cache'
    dynamodb    = boto3.client('dynamodb')

    def _table_exists(name):
        try:
            dynamodb.describe_table(TableName=name)
            return True
        except ClientError as e:
            return False
    
    def _create_table():
        if not _table_exists(CACHE_TABLE_NAME):
            resource = boto3.resource('dynamodb')
            table = resource.create_table(
                TableName=CACHE_TABLE_NAME,
                KeySchema=[
                    {
                        'AttributeName': 'path',
                        'KeyType': 'HASH'
                    }
                ],
                AttributeDefinitions=[
                    {
                        'AttributeName': 'path',
                        'AttributeType': 'S'
                    }
                ],
                #ProvisionedThroughput={
                #    'ReadCapacityUnits': 5,
                #    'WriteCapacityUnits': 5
                #},
                BillingMode='PAY_PER_REQUEST')
            table.wait_until_exists()

    def _delete_table():
        if _table_exists(CACHE_TABLE_NAME):
            dynamodb.delete_table(TableName=CACHE_TABLE_NAME)

    def _put_item(path, json_object):
        print(f'saving {path}: {len(json_object)}')
        response = dynamodb.put_item(
            TableName=CACHE_TABLE_NAME,
            Item={
                'path': {'S': path},
                'value': {'S': json_object},
            }
        )

    def _get_item(path):
        response = dynamodb.get_item(
            TableName=CACHE_TABLE_NAME,
            Key={
                'path': {'S': path}
            })
        if 'Item' in response:
            return response['Item']['value']['S']
        return None

    def save_cached_object(obj, object_path):
        """Store JSON serialized version of object keyed on object_path"""
        if not _table_exists(CACHE_TABLE_NAME):
            _create_table()
        _put_item(object_path, json.dumps(obj))

    def load_cached_object(object_path):
        """Return object previously saved at object_path.
        Raises CacheMissError if cache entry is missing.
        """
        if not _table_exists(CACHE_TABLE_NAME):
            raise CacheMissError(object_path)

        item = _get_item(object_path)
        if not item:
            raise CacheMissError(object_path)

        return json.loads(item)

    def remove_cached_object(object_path):
        if not _table_exists(CACHE_TABLE_NAME):
            return
        dynamodb.delete_item(
            TableName=CACHE_TABLE_NAME,
            Key={
                'path': {'S': object_path},
                }
            )

# ---------------

if False:
    from pynamodb.models import Model
    from pynamodb.attributes import UnicodeAttribute
    from pynamodb.exceptions import DeleteError

    class Cache(Model):
        class Meta:
            table_name = 'BirddogCache'
            region = "us-east-1"
            host = 'http://localhost:8000'
            read_capacity_units = 5
            write_capacity_units = 5
        path = UnicodeAttribute(hash_key=True)
        value = UnicodeAttribute(range_key=True)

    def save_cached_object(obj, object_path):
        """Store JSON serialized version of object with key given by path"""
        if not Cache.exists():
            Cache.create_table()
        item = Cache(object_path, json.dumps(obj))
        item.save()

    def load_cached_object(object_path):
        """Return object previously saved at path key.
        Raises CacheMissError if cache entry is missing.
        """
        if not Cache.exists():
            raise CacheMissError(object_path)
        try:
            item = Cache.get(object_path)
            return json.loads(item.value)
        except Cache.DoesNotExist:
            raise CacheMissError(object_path)

    def remove_cached_object(object_path):
        if not Cache.exists():
            return
        try:
            Cache.get(object_path)
        except Cache.DoesNotExist:
            return
        Cache.delete(object_path)
