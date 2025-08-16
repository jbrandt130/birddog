# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Common logging support
"""

import sys
import os
import requests
from collections import deque
from datetime import datetime

from birddog.env import detect_environment

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

import sqlite3
import threading

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

import boto3
from boto3.dynamodb.conditions import Key

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
        resp = self._table.query(**kwargs)
        while 'LastEvaluatedKey' in resp:
            more = self._table.query(**kwargs, ExclusiveStartKey=resp['LastEvaluatedKey'])
            resp['Items'].extend(more['Items'])
        result = {}
        for item in resp['Items']:
            if not cutoff_date or item['mod_date'] >= cutoff_date:
                result[item['title']] = item['mod_date']
        return result

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
