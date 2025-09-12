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
from typing import List, Tuple
from datetime import datetime
from abc import ABC, abstractmethod
import sqlite3
import boto3
from boto3.dynamodb.conditions import Key, Attr

from decimal import Decimal

from birddog.env import detect_environment
from birddog.utility import json_size
from birddog.logging import get_logger, LogService
_logger = get_logger()

# mod date store (abstract base class) -----------------------------------

class ModDateStore:
    def get_newer_updates(self, updates: dict) -> dict:
        """Given {title: mod_date}, return entries that are new or have a newer mod_date."""
        raise NotImplementedError

    def batch_store_updates(self, updates: dict):
        """Persist multiple updates to the store."""
        raise NotImplementedError

    def get_all_titles(self: list) -> set:
        """Return set of all titles in the store."""
        raise NotImplementedError

    def get_missing_titles(self, titles: list) -> list:
        """Return list of titles that are not in the store."""
        all_titles = self.get_all_titles()
        return [title for title in titles if title not in all_titles]

    def query_by_prefix(self, prefix: str, cutoff_date: str = None) -> dict:
        """Return {title: mod_date} for entries with matching prefix and optional date filter."""
        raise NotImplementedError

    def normalized_prefix(self, title):
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
        with LogService("ModDateStore", "get_newer_updates") as log:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    f"SELECT title, mod_date FROM mod_dates WHERE title IN ({placeholders})", titles
                )
                payload = cur.fetchall()
            log.size = json_size(payload)
            existing = {row[0]: row[1] for row in payload}
        return {
            title: mod_date
            for title, mod_date in updates.items()
            if title not in existing or mod_date > existing[title]
        }

    def batch_store_updates(self, updates: dict):
        with LogService("ModDateStore", "batch_store_updates", size=json_size(updates)):
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    "REPLACE INTO mod_dates (archive_root, title, mod_date) VALUES (?, ?, ?)",
                    [
                        (self.normalized_prefix(title), title, mod_date)
                        for title, mod_date in updates.items()
                    ]
                )

    def get_all_titles(self) -> set:
        with LogService("ModDateStore", "get_all_titles") as log:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute("SELECT title FROM mod_dates")
                payload = cur.fetchall()
            log.size = json_size(payload)
            return {row[0] for row in payload}

    def query_by_prefix(self, archive_root: str, cutoff_date: str = None) -> dict:
        archive_root = self.normalized_prefix(archive_root)
        with LogService("ModDateStore", "query_by_prefix", path=archive_root) as log:
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
                payload = cur.fetchall()
            log.size = json_size(payload)
        return {row[0]: row[1] for row in payload}

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

    def _split_key(self, title):
        archive_root = self.normalized_prefix(title)
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
        with LogService("ModDateStore", "get_newer_updates") as log:
            for chunk in self._chunked(keys, 100):
                resp = self._client.batch_get_item(
                    RequestItems={self._table_name: {'Keys': chunk}}
                )
                for item in resp['Responses'].get(self._table_name, []):
                    existing[item['title']['S']] = item['mod_date']['S']
                log.size = json_size(existing)
        return {
            title: mod_date
            for title, mod_date in updates.items()
            if title not in existing or mod_date > existing[title]
        }

    def batch_store_updates(self, updates: dict):
        with LogService("ModDateStore", "batch_store_updates", size=json_size(updates)):
            with self._table.batch_writer() as batch:
                for title, mod_date in updates.items():
                    archive_root = self.normalized_prefix(title)
                    batch.put_item(Item={
                        'archive_root': archive_root,
                        'title': title,
                        'mod_date': mod_date
                    })

    def get_all_titles(self) -> set:
        """Return the set of all titles in the table (bandwidth-efficient)."""
        titles = set()
        # 'title' isn't a reserved word, but alias it anyway for safety
        expr_names = {'#t': 'title'}

        with LogService("ModDateStore", "get_all_titles") as log:
            resp = self._table.scan(
                ProjectionExpression='#t',
                ExpressionAttributeNames=expr_names
            )
            items = resp.get('Items', [])
            titles.update(i['title'] for i in items)

            lek = resp.get('LastEvaluatedKey')
            while lek:
                resp = self._table.scan(
                    ProjectionExpression='#t',
                    ExpressionAttributeNames=expr_names,
                    ExclusiveStartKey=lek
                )
                items = resp.get('Items', [])
                titles.update(i['title'] for i in items)
                lek = resp.get('LastEvaluatedKey')

            # Optional: log approximate JSON size of the payload we would have returned
            # (kept consistent with your existing pattern)
            log.size = json_size(list(titles))

        return titles

    def query_by_prefix(self, archive_root: str, cutoff_date: str = None) -> dict:
        archive_root = self.normalized_prefix(archive_root)
        kwargs = {'KeyConditionExpression': Key('archive_root').eq(archive_root)}

        items = []
        with LogService("ModDateStore", "query_by_prefix", path=archive_root) as log:
            resp = self._table.query(**kwargs)
            items.extend(resp.get('Items', []))

            lek = resp.get('LastEvaluatedKey')
            while lek:
                resp = self._table.query(**kwargs, ExclusiveStartKey=lek)
                items.extend(resp.get('Items', []))
                lek = resp.get('LastEvaluatedKey')
            log.size = json_size(items)
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
    def pop(self, queue_name: str, n: int) -> list:
        """Remove and return the first n items from the queue."""
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
        with LogService("StringQueue", "append", path=queue_name, size=json_size(strings)):
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    "INSERT INTO queue (queue_name, value) VALUES (?, ?)",
                    [(queue_name, s) for s in strings]
                )

    def peek(self, queue_name: str, n: int) -> list:
        with LogService("StringQueue", "peek", path=queue_name) as log:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    "SELECT value FROM queue WHERE queue_name = ? ORDER BY id ASC LIMIT ?",
                    (queue_name, n)
                )
                payload = cur.fetchall()
            log.size = json_size(payload)
        return [row[0] for row in payload]

    def pop(self, queue_name: str, n: int) -> list[str]:
        if n <= 0:
            return []
        with LogService("StringQueue", "pop", path=queue_name) as log:
            with sqlite3.connect(self._db_path) as conn:
                # Lock rows so another process can't interleave deletes
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "SELECT id, value FROM queue WHERE queue_name = ? "
                    "ORDER BY id ASC LIMIT ?",
                    (queue_name, n)
                )
                rows = cur.fetchall()
                log.size = json_size(rows)
                if not rows:
                    return []
                ids = [r[0] for r in rows]
                # Delete just the selected rows
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM queue WHERE id IN ({placeholders})", ids)
                # The context manager will commit here
            return [r[1] for r in rows]

    def length(self, queue_name: str) -> int:
        with LogService("StringQueue", "length", path=queue_name):
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
        with LogService("StringQueue", "append", path=queue_name, size=json_size(strings)):
            with self._table.batch_writer() as batch:
                for i, value in enumerate(strings):
                    ts = now + Decimal(i) * Decimal('0.000001')
                    batch.put_item(Item={
                        'queue_name': queue_name,
                        'ts': ts,
                        'value': value
                    })

    def peek(self, queue_name: str, n: int) -> list:
        with LogService("StringQueue", "peek", path=queue_name) as log:
            resp = self._table.query(
                KeyConditionExpression=Key('queue_name').eq(queue_name),
                Limit=n,
                ScanIndexForward=True
            )
            items = resp.get('Items', [])
            if not items:
                return []
            values = [it['value'] for it in items]
            log.size = json_size(items)
        return values

    def pop(self, queue_name: str, n: int) -> list[str]:
        if n <= 0:
            return []
        with LogService("StringQueue", "pop", path=queue_name) as log:
            resp = self._table.query(
                KeyConditionExpression=Key('queue_name').eq(queue_name),
                Limit=n,
                ScanIndexForward=True,     # oldest first
                ConsistentRead=True        # reduce staleness on concurrent writers
            )
            items = resp.get('Items', [])
            if not items:
                return []
            values = [it['value'] for it in items]
            with self._table.batch_writer() as batch:
                for it in items:
                    batch.delete_item(Key={'queue_name': queue_name, 'ts': it['ts']})
            log.size = json_size(items)
        return values

    def length(self, queue_name: str) -> int:
        total = 0
        last_evaluated_key = None
        with LogService("StringQueue", "length", path=queue_name):
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

# key value store (abstract version) ---------------------------------------

class KeyValueStore(ABC):
    @abstractmethod
    def insert(self, namespace: str, key: str, value: str):
        pass

    @abstractmethod
    def remove(self, namespace: str, key: str):
        pass

    @abstractmethod
    def remove_all(self, namespace: str):
        pass

    @abstractmethod
    def get(self, namespace: str, key: str) -> str:
        pass

    @abstractmethod
    def get_all(self, namespace: str) -> list:
        pass

    @abstractmethod
    def count(self, namespace: str) -> int:
        pass

# key value store (sqlite version) ---------------------------------------

_KEY_VALUE_STORE_PATH = ".cache/key_value_store.db"

class SQLiteKeyValueStore(KeyValueStore):
    def __init__(self, db_path: str = _KEY_VALUE_STORE_PATH):
        self._db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv (
                    namespace TEXT NOT NULL,
                    key       TEXT NOT NULL,
                    value     TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
            """)
            conn.commit()

    # --- API ---

    def insert(self, namespace: str, key: str, value: str):
        if not isinstance(value, str):
            raise TypeError("value must be str")
        with LogService("KVStore", "insert", path=namespace, size=len(key) + len(value)):
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO kv(namespace, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value
                """, (namespace, key, value))
                conn.commit()

    def remove(self, namespace: str, key: str):
        with LogService("KVStore", "remove", path=namespace, size=len(key)):
            with self._conn() as conn:
                conn.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
                conn.commit()

    def remove_all(self, namespace: str):
        with LogService("KVStore", "remove_all", path=namespace):
            with self._conn() as conn:
                conn.execute("DELETE FROM kv WHERE namespace = ?", (namespace,))
                conn.commit()

    def get(self, namespace: str, key: str) -> str:
        with LogService("KVStore", "get", path=namespace) as log:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT value FROM kv WHERE namespace = ? AND key = ?",
                    (namespace, key)
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"{namespace}:{key} not found")
            log.size = json_size(row)
            return row[0]

    def get_all(self, namespace: str) -> List[Tuple[str, str]]:
        with LogService("KVStore", "get_all", path=namespace) as log:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT key, value FROM kv WHERE namespace = ? ORDER BY key",
                    (namespace,)
                )
                payload = cur.fetchall()
            log.size = json_size(payload)
        return [(k, v) for (k, v) in payload]

    def count(self, namespace: str) -> int:
        with LogService("KVStore", "count", path=namespace):
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM kv WHERE namespace = ?",
                    (namespace,)
                )
                return int(cur.fetchone()[0])

# key value store (dynamodb version) ---------------------------------------

class DynamoDBKeyValueStore:
    def __init__(self, table_name='birddog_key_value_store'):
        self._table_name = table_name
        self._dynamodb = boto3.resource('dynamodb')
        self._client = boto3.client('dynamodb')
        self._ensure_table_exists()
        self._table = self._dynamodb.Table(table_name)

    def _ensure_table_exists(self):
        if self._table_name in self._client.list_tables()['TableNames']:
            return
        self._dynamodb.create_table(
            TableName=self._table_name,
            KeySchema=[
                {'AttributeName': 'namespace', 'KeyType': 'HASH'},
                {'AttributeName': 'key',       'KeyType': 'RANGE'},
            ],
            AttributeDefinitions=[
                {'AttributeName': 'namespace', 'AttributeType': 'S'},
                {'AttributeName': 'key',       'AttributeType': 'S'},
            ],
            BillingMode='PAY_PER_REQUEST'
        ).wait_until_exists()

    # --- methods ---

    def insert(self, namespace: str, key: str, value: str):
        if not isinstance(value, str):
            raise TypeError("value must be str")
        if not isinstance(key, str):
            raise TypeError("key must be str")
        with LogService("KVStore", "insert", path=namespace, size=len(key) + len(value)):
            self._table.put_item(Item={
                "namespace": namespace,
                "key": key,
                "value": value
            })

    def remove(self, namespace: str, key: str):
        if not isinstance(key, str):
            raise TypeError("key must be str")
        with LogService("KVStore", "remove", path=namespace, size=len(key)):
            self._table.delete_item(Key={"namespace": namespace, "key": key})

    def remove_all(self, namespace: str):
        with LogService("KVStore", "remove_all", path=namespace):
            resp = self._table.query(
                KeyConditionExpression=Key("namespace").eq(namespace),
                ProjectionExpression="#ns,#k",
                ExpressionAttributeNames={"#ns": "namespace", "#k": "key"},
            )
            items = resp.get("Items", [])
            while "LastEvaluatedKey" in resp:
                resp = self._table.query(
                    KeyConditionExpression=Key("namespace").eq(namespace),
                    ProjectionExpression="#ns,#k",
                    ExpressionAttributeNames={"#ns": "namespace", "#k": "key"},
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                )
                items.extend(resp.get("Items", []))

            if items:
                with self._table.batch_writer() as batch:
                    for item in items:
                        batch.delete_item(Key={"namespace": item["namespace"], "key": item["key"]})

    def get(self, namespace: str, key: str) -> str:
        if not isinstance(key, str):
            raise TypeError("key must be str")
        with LogService("KVStore", "get", path=namespace) as log:
            resp = self._table.get_item(
                Key={"namespace": namespace, "key": key},
                ProjectionExpression="#v",
                ExpressionAttributeNames={"#v": "value"},
            )
            item = resp.get("Item")
            if not item:
                raise KeyError(f"{namespace}:{key} not found")
            log.size = json_size(item)
        val = item.get("value")
        return val if isinstance(val, str) else ("" if val is None else str(val))

    def get_all(self, namespace: str):
        items = []
        with LogService("KVStore", "get_all", path=namespace) as log:
            resp = self._table.query(
                KeyConditionExpression=Key("namespace").eq(namespace),
                ProjectionExpression="#k,#v",
                ExpressionAttributeNames={"#k": "key", "#v": "value"},
            )
            items.extend((it["key"], it.get("value", "")) for it in resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = self._table.query(
                    KeyConditionExpression=Key("namespace").eq(namespace),
                    ProjectionExpression="#k,#v",
                    ExpressionAttributeNames={"#k": "key", "#v": "value"},
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                )
                items.extend((it["key"], it.get("value", "")) for it in resp.get("Items", []))
            log.size = json_size(items)
            items.sort(key=lambda kv: kv[0])
        return items

    def count(self, namespace: str) -> int:
        with LogService("KVStore", "count", path=namespace):
            resp = self._table.query(
                KeyConditionExpression=Key("namespace").eq(namespace),
                Select="COUNT",
            )
            count = resp.get("Count", 0)
            while "LastEvaluatedKey" in resp:
                resp = self._table.query(
                    KeyConditionExpression=Key("namespace").eq(namespace),
                    Select="COUNT",
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                )
                count += resp.get("Count", 0)
        return int(count)

# platform-independent access to key value store ----------------------------------

_key_value_store = None

def get_key_value_store():
    global _key_value_store
    if not _key_value_store:
        #_logger.info(f"get_string_queue_store: detect_environment=='{detect_environment()}'")
        if detect_environment() == "aws":
            _key_value_store = DynamoDBKeyValueStore()
        else:
            #_key_value_store = DynamoDBKeyValueStore()
            _key_value_store = SQLiteKeyValueStore()
    return _key_value_store
