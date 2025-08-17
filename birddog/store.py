# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Common store support
"""

import sys
import os
import time
import requests
import threading
from collections import deque
from datetime import datetime
from abc import ABC, abstractmethod
import sqlite3
import boto3
from boto3.dynamodb.conditions import Key, Attr
from decimal import Decimal

from birddog.env import detect_environment
#from birddog.logging import get_logger
#_logger = get_logger()

# mod date store (abstract base class) -----------------------------------

class ModDateStore:
    def get_newer_updates(self, updates: dict) -> dict:
        """Given {title: mod_date}, return entries that are new or have a newer mod_date."""
        raise NotImplementedError

    def batch_store_updates(self, updates: dict):
        """Persist multiple updates to the store."""
        raise NotImplementedError

    def get_missing_titles(self, titles: list) -> list:
        """Return list of titles that are not in the store."""
        raise NotImplementedError

    def query_by_prefix(self, prefix: str, cutoff_date: str = None) -> dict:
        """Return {title: mod_date} for entries with matching prefix and optional date filter."""
        raise NotImplementedError

    def _normalized_prefix(self, title):
        return title.split("/", 1)[0].replace("_", " ")

# mod date store (sqlite version) ---------------------------------------

_MOD_DATE_PATH = ".cache/mod_dates.db"

class SQLiteModDateStore(ModDateStore):
    def __init__(self, db_path=_MOD_DATE_PATH):
        self._db_path = db_path
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS mod_dates (
                    archive_root TEXT,
                    title TEXT PRIMARY KEY,
                    mod_date TEXT
                )
            ''')
            conn.commit()

    def get_newer_updates(self, updates: dict) -> dict:
        if not updates:
            return {}
        titles = list(updates.keys())
        placeholders = ",".join("?" for _ in titles)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                f"SELECT title, mod_date FROM mod_dates WHERE title IN ({placeholders})", titles
            )
            existing = {row[0]: row[1] for row in cur.fetchall()}
        return {
            title: mod_date
            for title, mod_date in updates.items()
            if title not in existing or mod_date > existing[title]
        }

    def batch_store_updates(self, updates: dict):
        with sqlite3.connect(self._db_path) as conn:
            conn.executemany(
                "REPLACE INTO mod_dates (archive_root, title, mod_date) VALUES (?, ?, ?)",
                [
                    (self._normalized_prefix(title), title, mod_date)
                    for title, mod_date in updates.items()
                ]
            )

    def get_missing_titles(self, titles: list) -> list:
        if not titles:
            return []
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("SELECT title FROM mod_dates")
        stored_titles = {row[0] for row in cur.fetchall()}
        return [title for title in titles if title not in stored_titles]

    def query_by_prefix(self, archive_root: str, cutoff_date: str = None) -> dict:
        archive_root = self._normalized_prefix(archive_root)
        with sqlite3.connect(self._db_path) as conn:
            if cutoff_date:
                cur = conn.execute(
                    "SELECT title, mod_date FROM mod_dates WHERE archive_root = ? AND mod_date >= ?",
                    (archive_root, cutoff_date)
                )
            else:
                cur = conn.execute(
                    "SELECT title, mod_date FROM mod_dates WHERE archive_root = ?",
                    (archive_root,)
                )
        return {row[0]: row[1] for row in cur.fetchall()}

# mod date store (dynamodb version) ---------------------------------------


class DynamoDBModDateStore(ModDateStore):
    def __init__(self, table_name='birddog_mod_dates'):
        self._table_name = table_name
        self._dynamodb = boto3.resource('dynamodb')
        self._client = boto3.client('dynamodb')
        self._ensure_table_exists()
        self._table = self._dynamodb.Table(table_name)

    def _ensure_table_exists(self):
        existing_tables = self._client.list_tables()['TableNames']
        if self._table_name in existing_tables:
            return
        print(f"Creating DynamoDB table '{self._table_name}'...")
        self._dynamodb.create_table(
            TableName=self._table_name,
            KeySchema=[
                {'AttributeName': 'archive_root', 'KeyType': 'HASH'},
                {'AttributeName': 'title', 'KeyType': 'RANGE'}
            ],
            AttributeDefinitions=[
                {'AttributeName': 'archive_root', 'AttributeType': 'S'},
                {'AttributeName': 'title', 'AttributeType': 'S'}
            ],
            BillingMode='PAY_PER_REQUEST'
        ).wait_until_exists()
        print(f"Table '{self._table_name}' created successfully.")

    def _split_key(self, title):
        archive_root = self._normalized_prefix(title)
        return {'archive_root': {'S': archive_root}, 'title': {'S': title}}

    def _chunked(self, iterable, size):
        for i in range(0, len(iterable), size):
            yield iterable[i:i+size]

    # ==== Public Interface Methods ====

    def get_newer_updates(self, updates: dict) -> dict:
        if not updates:
            return {}

        existing = {}
        keys = [self._split_key(t) for t in updates.keys()]
        print(f"DDB: get_newer_updates - {len(keys)} items")
        for chunk in self._chunked(keys, 100):
            resp = self._client.batch_get_item(
                RequestItems={self._table_name: {'Keys': chunk}}
            )
            for item in resp['Responses'].get(self._table_name, []):
                existing[item['title']['S']] = item['mod_date']['S']

        return {
            title: mod_date
            for title, mod_date in updates.items()
            if title not in existing or mod_date > existing[title]
        }

    def batch_store_updates(self, updates: dict):
        with self._table.batch_writer() as batch:
            for title, mod_date in updates.items():
                archive_root = self._normalized_prefix(title)
                batch.put_item(Item={
                    'archive_root': archive_root,
                    'title': title,
                    'mod_date': mod_date
                })

    def get_missing_titles(self, titles: list) -> list:
        if not titles:
            return []

        present = set()
        keys = [self._split_key(t) for t in titles]
        for chunk in self._chunked(keys, 100):
            resp = self._client.batch_get_item(
                RequestItems={self._table_name: {'Keys': chunk}}
            )
            present.update(item['title']['S'] for item in resp['Responses'].get(self._table_name, []))

        return [t for t in titles if t not in present]

    def query_by_prefix(self, archive_root: str, cutoff_date: str = None) -> dict:
        archive_root = self._normalized_prefix(archive_root)
        kwargs = {'KeyConditionExpression': Key('archive_root').eq(archive_root)}

        items = []
        resp = self._table.query(**kwargs)
        items.extend(resp.get('Items', []))

        lek = resp.get('LastEvaluatedKey')
        while lek:
            resp = self._table.query(**kwargs, ExclusiveStartKey=lek)
            items.extend(resp.get('Items', []))
            lek = resp.get('LastEvaluatedKey')

        if cutoff_date:
            return {i['title']: i['mod_date'] for i in items if i['mod_date'] >= cutoff_date}
        return {i['title']: i['mod_date'] for i in items}

# platform-independent access to mod date store ----------------------------------

_mod_date_store = None

def get_mod_date_store():
    global _mod_date_store
    if not _mod_date_store:
        if detect_environment() == "aws":
            _mod_date_store = DynamoDBModDateStore()
        else:
            _mod_date_store = SQLiteModDateStore()
    return _mod_date_store

# string queue (abstract version) ---------------------------------------

class StringQueue(ABC):
    @abstractmethod
    def append(self, queue_name: str, strings: list):
        """Append a list of strings to the end of the named queue."""
        pass

    @abstractmethod
    def peek(self, queue_name: str, n: int) -> list:
        """Return the first n items from the front of the queue without removing them."""
        pass

    @abstractmethod
    def pop(self, queue_name: str, n: int):
        """Remove the first n items from the queue."""
        pass

    @abstractmethod
    def length(self, queue_name: str) -> int:
        """Return the number of items in the named queue."""
        pass

# string queue (sqlite version) ---------------------------------------

_STRING_QUEUE_PATH = ".cache/string_queues.db"

class SQLiteStringQueue(StringQueue):
    def __init__(self, db_path=_STRING_QUEUE_PATH):
        self._db_path = db_path
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS queue (
                    queue_name TEXT,
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    value TEXT NOT NULL
                )
            ''')

    def append(self, queue_name: str, strings: list):
        if not strings:
            return
        with sqlite3.connect(self._db_path) as conn:
            conn.executemany(
                "INSERT INTO queue (queue_name, value) VALUES (?, ?)",
                [(queue_name, s) for s in strings]
            )

    def peek(self, queue_name: str, n: int) -> list:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "SELECT value FROM queue WHERE queue_name = ? ORDER BY id ASC LIMIT ?",
                (queue_name, n)
            )
            return [row[0] for row in cur.fetchall()]

    def pop(self, queue_name: str, n: int):
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "SELECT id FROM queue WHERE queue_name = ? ORDER BY id ASC LIMIT ?",
                (queue_name, n)
            )
            ids_to_delete = [row[0] for row in cur.fetchall()]
            if ids_to_delete:
                conn.executemany("DELETE FROM queue WHERE id = ?", [(i,) for i in ids_to_delete])

    def length(self, queue_name: str) -> int:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE queue_name = ?",
                (queue_name,)
            )
            return cur.fetchone()[0]

# string queue (dynamodb version) ---------------------------------------

class DynamoDBStringQueue(StringQueue):
    def __init__(self, table_name='birddog_string_queues'):
        self._table_name = table_name
        self._dynamodb = boto3.resource('dynamodb')
        self._client = boto3.client('dynamodb')
        self._ensure_table_exists()
        self._table = self._dynamodb.Table(table_name)

    def _ensure_table_exists(self):
        if self._table_name in self._client.list_tables()['TableNames']:
            return
        print(f"Creating DynamoDB table '{self._table_name}'...")
        self._dynamodb.create_table(
            TableName=self._table_name,
            KeySchema=[
                {'AttributeName': 'queue_name', 'KeyType': 'HASH'},
                {'AttributeName': 'ts', 'KeyType': 'RANGE'}
            ],
            AttributeDefinitions=[
                {'AttributeName': 'queue_name', 'AttributeType': 'S'},
                {'AttributeName': 'ts', 'AttributeType': 'N'}
            ],
            BillingMode='PAY_PER_REQUEST'
        ).wait_until_exists()

    def _now(self):
        return time.time()

    def append(self, queue_name: str, strings: list):
        if not strings:
            return
        now = Decimal(str(time.time()))
        with self._table.batch_writer() as batch:
            for i, value in enumerate(strings):
                ts = now + Decimal(i) * Decimal('0.000001')
                batch.put_item(Item={
                    'queue_name': queue_name,
                    'ts': ts,
                    'value': value
                })

    def peek(self, queue_name: str, n: int) -> list:
        resp = self._table.query(
            KeyConditionExpression=Key('queue_name').eq(queue_name),
            Limit=n,
            ScanIndexForward=True
        )
        return [item['value'] for item in resp['Items']]

    def pop(self, queue_name: str, n: int):
        resp = self._table.query(
            KeyConditionExpression=Key('queue_name').eq(queue_name),
            Limit=n,
            ScanIndexForward=True
        )
        with self._table.batch_writer() as batch:
            for item in resp['Items']:
                batch.delete_item(Key={
                    'queue_name': queue_name,
                    'ts': item['ts']
                })

    def length(self, queue_name: str) -> int:
        total = 0
        last_evaluated_key = None
        while True:
            kwargs = {
                'KeyConditionExpression': Key('queue_name').eq(queue_name),
                'Select': 'COUNT',
                'ExclusiveStartKey': last_evaluated_key
            } if last_evaluated_key else {
                'KeyConditionExpression': Key('queue_name').eq(queue_name),
                'Select': 'COUNT'
            }
            resp = self._table.query(**kwargs)
            total += resp['Count']
            last_evaluated_key = resp.get('LastEvaluatedKey')
            if not last_evaluated_key:
                break
        return total

# platform-independent access to string queue ----------------------------------

_string_queue_store = None

def get_string_queue_store():
    global _string_queue_store
    if not _string_queue_store:
        #_logger.info(f"get_string_queue_store: detect_environment=='{detect_environment()}'")
        if detect_environment() == "aws":
            _string_queue_store = DynamoDBStringQueue()
        else:
            _string_queue_store = SQLiteStringQueue()
    return _string_queue_store
