# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Common store support
"""

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Tuple

import sqlite3
import boto3
from boto3.dynamodb.conditions import Key

from decimal import Decimal

from birddog.env import detect_environment
from birddog.utility import json_size
from birddog.log import get_logger, LogService
_logger = get_logger()

# string queue (abstract version) ---------------------------------------

@dataclass(frozen=True)
class ClaimedItem:
    receipt: str   # opaque handle for ack/extend
    value: str

class AbstractStringQueue(ABC):
    @abstractmethod
    def append(self, queue_name: str, strings: list[str]):
        """Append a list of strings to the end of the named queue."""

    @abstractmethod
    def peek(self, queue_name: str, n: int) -> list[str]:
        """Return the first n items from the front of the queue without removing them."""

    @abstractmethod
    def length(self, queue_name: str) -> int:
        """Return the number of items in the named queue."""

    @abstractmethod
    def claim(self, queue_name: str, n: int, lease_ms: int, consumer_id: str) -> List[ClaimedItem]:
        """
        Atomically claim up to n visible (unleased/expired) items and return (receipt, value).
        Claimed items become invisible to other claimers until the lease expires.
        """

    @abstractmethod
    def ack(self, queue_name: str, receipts: list[str], consumer_id: str):
        """Acknowledge (delete) previously-claimed items."""

    @abstractmethod
    def extend(self, queue_name: str, receipts: list[str], lease_ms: int, consumer_id: str):
        """Extend leases for claimed items (no-op for missing/expired items)."""

# string queue (sqlite version) ---------------------------------------

_SQLITE_DEFAULT_STRING_QUEUE_PATH = ".cache/string_queues.db"

def _now_ms() -> int:
    return int(time.time() * 1000)

class SQLiteStringQueue(AbstractStringQueue):
    def __init__(self, table_name = "queue", db_path=_SQLITE_DEFAULT_STRING_QUEUE_PATH):
        self._db_path = db_path
        self._table_name = table_name
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table_name} (
                    queue_name     TEXT NOT NULL,
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    value          TEXT NOT NULL,
                    lease_until_ms INTEGER,
                    lease_owner    TEXT,
                    claimed_at_ms  INTEGER
                )
            """)
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_queue_visible ON {self._table_name}(queue_name, lease_until_ms, id)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_queue_owner ON {self._table_name}(queue_name, lease_owner, id)")
            conn.commit()

    def append(self, queue_name: str, strings: list[str]):
        if not strings:
            return
        if any(s == "" for s in strings):
            raise ValueError("empty string cannot be appended to a queue")
        with LogService("StringQueue", "append", path=queue_name, size=json_size(strings)):
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    f"INSERT INTO {self._table_name} (queue_name, value, lease_until_ms, lease_owner, claimed_at_ms) "
                    "VALUES (?, ?, NULL, NULL, NULL)",
                    [(queue_name, s) for s in strings]
                )
                conn.commit()

    def peek(self, queue_name: str, n: int) -> list[str]:
        if n <= 0:
            return []
        with LogService("StringQueue", "peek", path=queue_name) as log:
            now = _now_ms()
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    f"SELECT value FROM {self._table_name} "
                    "WHERE queue_name = ? AND (lease_until_ms IS NULL OR lease_until_ms <= ?) "
                    "ORDER BY id ASC LIMIT ?",
                    (queue_name, now, n)
                )
                payload = cur.fetchall()
            log.size = json_size(payload)
        return [row[0] for row in payload]

    def length(self, queue_name: str) -> int:
        with LogService("StringQueue", "length", path=queue_name):
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(f"SELECT COUNT(*) FROM {self._table_name} WHERE queue_name = ?", (queue_name,))
                return int(cur.fetchone()[0])

    def claim(self, queue_name: str, n: int, lease_ms: int, consumer_id: str) -> List[ClaimedItem]:
        if n <= 0:
            return []

        now = int(_now_ms())
        lease_until = now + int(lease_ms)

        with LogService("StringQueue", "claim", path=queue_name) as log:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")  # blocks other writers; prevents interleaving

                cur = conn.execute(
                    f"""
                    SELECT id, value
                    FROM {self._table_name}
                    WHERE queue_name = ?
                      AND (lease_until_ms IS NULL OR lease_until_ms <= ?)
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (queue_name, now, n),
                )
                rows = cur.fetchall()
                if not rows:
                    conn.commit()
                    log.size = 0
                    return []

                ids = [r[0] for r in rows]
                placeholders = ",".join("?" for _ in ids)

                # Acquire lease ONLY if still visible at update time (critical).
                conn.execute(
                    f"""
                    UPDATE {self._table_name}
                    SET lease_until_ms = ?, lease_owner = ?, claimed_at_ms = ?
                    WHERE queue_name = ?
                      AND (lease_until_ms IS NULL OR lease_until_ms <= ?)
                      AND id IN ({placeholders})
                    """,
                    [lease_until, consumer_id, now, queue_name, now, *ids],
                )

                # Re-read rows that we successfully leased (owner check).
                cur2 = conn.execute(
                    f"""
                    SELECT id, value
                    FROM {self._table_name}
                    WHERE queue_name = ?
                      AND lease_owner = ?
                      AND id IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    [queue_name, consumer_id, *ids],
                )
                leased = cur2.fetchall()

                conn.commit()

            log.size = json_size({"requested": n, "selected": len(rows), "claimed": len(leased)})

        return [ClaimedItem(receipt=str(r[0]), value=r[1]) for r in leased]

    def ack(self, queue_name: str, receipts: list[str], consumer_id: str):
        if not receipts:
            return
        ids = [int(r) for r in receipts]
        placeholders = ",".join("?" for _ in ids)

        with LogService("StringQueue", "ack", path=queue_name, size=len(receipts)):
            with sqlite3.connect(self._db_path) as conn:
                # Only delete items owned by this consumer; if lease expired/reclaimed, this won't delete.
                cur = conn.execute(
                    f"DELETE FROM {self._table_name} WHERE queue_name = ? AND lease_owner = ? AND id IN ({placeholders})",
                    [queue_name, consumer_id, *ids]
                )
                conn.commit()
                deleted = cur.rowcount
                if deleted != len(ids):
                    _logger.warning(
                        "ack: expected %d deletes, got %d (queue=%s owner=%s)",
                        len(ids), deleted, queue_name, consumer_id)

    def extend(self, queue_name: str, receipts: list[str], lease_ms: int, consumer_id: str):
        if not receipts:
            return
        now = _now_ms()
        lease_until = now + int(lease_ms)
        ids = [int(r) for r in receipts]
        placeholders = ",".join("?" for _ in ids)

        with LogService("StringQueue", "extend", path=queue_name, size=len(receipts)):
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    f"UPDATE {self._table_name} SET lease_until_ms = ? "
                    f"WHERE queue_name = ? AND lease_owner = ? AND id IN ({placeholders})",
                    [lease_until, queue_name, consumer_id, *ids]
                )
                conn.commit()
                deleted = cur.rowcount
                if deleted != len(ids):
                    _logger.warning(
                        "extend: expected %d deletes, got %d (queue=%s owner=%s)",
                        len(ids), deleted, queue_name, consumer_id)

    def dump(self, queue_name: str) -> list[dict]:
        """
        Debug helper: dump all rows for a queue, including lease fields.
        Read-only; no filtering on lease state.
        """
        with LogService("StringQueue", "dump_queue", path=queue_name) as log:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    f"""
                    SELECT
                        id,
                        queue_name,
                        value,
                        lease_until_ms,
                        lease_owner,
                        claimed_at_ms
                    FROM {self._table_name}
                    WHERE queue_name = ?
                    ORDER BY id ASC
                    """,
                    (queue_name,),
                )
                rows = cur.fetchall()

            out = []
            for (
                id_,
                qname,
                value,
                lease_until_ms,
                lease_owner,
                claimed_at_ms,
            ) in rows:
                out.append({
                    "id": id_,
                    "queue_name": qname,
                    "value": value,
                    "value_type": type(value).__name__,
                    "value_len": len(value) if isinstance(value, str) else None,
                    "lease_until_ms": lease_until_ms,
                    "lease_owner": lease_owner,
                    "claimed_at_ms": claimed_at_ms,
                })

            log.size = json_size({"returned": len(out)})
            return out

# string queue (dynamodb version) ---------------------------------------

class DynamoDBStringQueue(AbstractStringQueue):
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

    def append(self, queue_name: str, strings: list[str]):
        if not strings:
            return
        if any(s == "" for s in strings):
            raise ValueError("empty string cannot be appended to a queue")
        now = Decimal(str(time.time()))
        with LogService("StringQueue", "append", path=queue_name, size=json_size(strings)):
            with self._table.batch_writer() as batch:
                for i, value in enumerate(strings):
                    ts = now + Decimal(i) * Decimal('0.000001')
                    batch.put_item(Item={
                        'queue_name': queue_name,
                        'ts': ts,
                        'value': value,
                        # lease fields absent/null => visible
                    })

    def peek(self, queue_name: str, n: int) -> list[str]:
        if n <= 0:
            return []

        now_ms = Decimal(str(_now_ms()))

        # Read-ahead cap: prevent scanning the entire partition when everything is leased
        max_pages = 10
        page_limit = max(25, n * 5)

        visible: list[str] = []
        last_evaluated_key = None

        with LogService("StringQueue", "peek", path=queue_name) as log:
            scanned = 0

            for _ in range(max_pages):
                kwargs = {
                    "KeyConditionExpression": Key("queue_name").eq(queue_name),
                    "Limit": page_limit,
                    "ScanIndexForward": True,
                    "ConsistentRead": True,
                    "ProjectionExpression": "#v, lease_until_ms",
                    "ExpressionAttributeNames": {"#v": "value"},
                }
                if last_evaluated_key:
                    kwargs["ExclusiveStartKey"] = last_evaluated_key

                resp = self._table.query(**kwargs)
                items = resp.get("Items", [])
                scanned += len(items)

                for it in items:
                    lease_until = it.get("lease_until_ms")
                    if lease_until is None or Decimal(str(lease_until)) <= now_ms:
                        visible.append(it["value"])
                        if len(visible) >= n:
                            log.size = json_size({"scanned": scanned, "returned": len(visible)})
                            return visible

                last_evaluated_key = resp.get("LastEvaluatedKey")
                if not last_evaluated_key or not items:
                    break

            log.size = json_size({"scanned": scanned, "returned": len(visible)})
            return visible

    def length(self, queue_name: str) -> int:
        # unchanged from your code
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

    def claim(self, queue_name: str, n: int, lease_ms: int, consumer_id: str):
        if n <= 0:
            return []

        now_ms = Decimal(str(_now_ms()))
        lease_until_ms = now_ms + Decimal(int(lease_ms))

        claimed: list[ClaimedItem] = []
        max_pages = 10
        page_limit = max(25, n * 5)
        last_evaluated_key = None

        with LogService("StringQueue", "claim", path=queue_name) as log:
            scanned = 0

            for _ in range(max_pages):
                kwargs = {
                    "KeyConditionExpression": Key("queue_name").eq(queue_name),
                    "Limit": page_limit,
                    "ScanIndexForward": True,
                    "ConsistentRead": True,
                    # Pull lease fields so we can skip obviously leased items before UpdateItem
                    "ProjectionExpression": "#ts, #v, lease_until_ms, lease_owner",
                    "ExpressionAttributeNames": {"#ts": "ts", "#v": "value"},
                }
                if last_evaluated_key:
                    kwargs["ExclusiveStartKey"] = last_evaluated_key

                resp = self._table.query(**kwargs)
                items = resp.get("Items", [])
                scanned += len(items)

                if not items:
                    break

                for it in items:
                    if len(claimed) >= n:
                        log.size = json_size({"scanned": scanned, "claimed": len(claimed)})
                        return claimed

                    # Skip items that are clearly leased (reduces contention / conditional failures)
                    lu = it.get("lease_until_ms")
                    if lu is not None:
                        try:
                            if Decimal(str(lu)) > now_ms:
                                continue
                        except Exception:
                            # If parsing lease_until_ms fails, fall through and let the condition decide
                            pass

                    ts = it["ts"]
                    receipt = str(ts)

                    try:
                        # IMPORTANT:
                        # - attribute_exists(#v) prevents UpdateItem from recreating a deleted row
                        # - ReturnValues="ALL_OLD" returns the pre-update value from the same atomic write
                        resp2 = self._table.update_item(
                            Key={"queue_name": queue_name, "ts": ts},
                            UpdateExpression=(
                                "SET lease_until_ms = :lu, lease_owner = :o, claimed_at_ms = :now"
                            ),
                            ConditionExpression=(
                                "(attribute_not_exists(lease_until_ms) OR lease_until_ms <= :now) "
                                "AND attribute_exists(#v)"
                            ),
                            ExpressionAttributeNames={"#v": "value"},
                            ExpressionAttributeValues={
                                ":lu": lease_until_ms,
                                ":o": consumer_id,
                                ":now": now_ms,
                            },
                            ReturnValues="ALL_OLD",
                        )
                    except self._client.exceptions.ConditionalCheckFailedException:
                        continue

                    old = resp2.get("Attributes") or {}
                    value = old.get("value")
                    if isinstance(value, str) and value:
                        claimed.append(ClaimedItem(receipt=receipt, value=value))

                last_evaluated_key = resp.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

            log.size = json_size({"scanned": scanned, "claimed": len(claimed)})
            return claimed


    def ack(self, queue_name: str, receipts: list[str], consumer_id: str):
        if not receipts:
            return
        with LogService("StringQueue", "ack", path=queue_name, size=len(receipts)):
            for r in receipts:
                ts = Decimal(r)
                try:
                    # Only delete if we still own it, and it's a "real" row (has value).
                    self._table.delete_item(
                        Key={"queue_name": queue_name, "ts": ts},
                        ConditionExpression="lease_owner = :o AND attribute_exists(#v)",
                        ExpressionAttributeNames={"#v": "value"},
                        ExpressionAttributeValues={":o": consumer_id},
                    )
                except self._client.exceptions.ConditionalCheckFailedException:
                    # Lease expired or was reclaimed by another consumer before ack.
                    # The item was NOT deleted — it remains visible for re-claim/retry.
                    _logger.warning(
                        "StringQueue.ack: conditional check failed for queue=%s ts=%s consumer=%s "
                        "(lease lost — item not deleted, will be re-claimed)",
                        queue_name, ts, consumer_id,
                    )


    def extend(self, queue_name: str, receipts: list[str], lease_ms: int, consumer_id: str):
        if not receipts:
            return
        now_ms = Decimal(str(_now_ms()))
        lease_until_ms = now_ms + Decimal(int(lease_ms))
        with LogService("StringQueue", "extend", path=queue_name, size=len(receipts)):
            for r in receipts:
                ts = Decimal(r)
                try:
                    self._table.update_item(
                        Key={"queue_name": queue_name, "ts": ts},
                        UpdateExpression="SET lease_until_ms = :lu",
                        ConditionExpression="lease_owner = :o AND attribute_exists(#v)",
                        ExpressionAttributeNames={"#v": "value"},
                        ExpressionAttributeValues={":lu": lease_until_ms, ":o": consumer_id},
                    )
                except self._client.exceptions.ConditionalCheckFailedException:
                    pass

    def dump(self, queue_name: str, max_pages: int = 50, page_limit: int = 100):
        """
        Debug helper: return *all* items in a queue partition, including lease fields.

        Returns a list of dicts:
            {
                "queue_name": ...,
                "ts": ...,
                "value": ...,
                "lease_until_ms": ...,
                "lease_owner": ...,
                "claimed_at_ms": ...
            }

        This method:
          - does NOT filter on lease state
          - does NOT mutate anything
          - paginates defensively
        """
        items_out = []
        last_evaluated_key = None
        scanned = 0

        with LogService("StringQueue", "dump_queue", path=queue_name) as log:
            for _ in range(max_pages):
                kwargs = {
                    "KeyConditionExpression": Key("queue_name").eq(queue_name),
                    "Limit": page_limit,
                    "ScanIndexForward": True,
                    "ConsistentRead": True,
                    "ProjectionExpression": (
                        "queue_name, #ts, #v, "
                        "lease_until_ms, lease_owner, claimed_at_ms"
                    ),
                    "ExpressionAttributeNames": {
                        "#ts": "ts",
                        "#v": "value",
                    },
                }
                if last_evaluated_key:
                    kwargs["ExclusiveStartKey"] = last_evaluated_key

                resp = self._table.query(**kwargs)
                page_items = resp.get("Items", [])
                scanned += len(page_items)

                for it in page_items:
                    items_out.append({
                        "queue_name": it.get("queue_name"),
                        "ts": it.get("ts"),
                        "value": it.get("value"),
                        "value_type": type(it.get("value")).__name__,
                        "value_len": len(it.get("value")) if isinstance(it.get("value"), str) else None,
                        "lease_until_ms": it.get("lease_until_ms"),
                        "lease_owner": it.get("lease_owner"),
                        "claimed_at_ms": it.get("claimed_at_ms"),
                    })

                last_evaluated_key = resp.get("LastEvaluatedKey")
                if not last_evaluated_key or not page_items:
                    break

            log.size = json_size({
                "scanned": scanned,
                "returned": len(items_out),
            })

        return items_out

# platform-independent access to string queue ----------------------------------

if detect_environment() == "aws":
    StringQueue = DynamoDBStringQueue
else:
    StringQueue = SQLiteStringQueue

# key value store (abstract version) ---------------------------------------

class AbstractKeyValueStore(ABC):
    @abstractmethod
    def insert(self, namespace: str, key: str, value: str):
        pass

    @abstractmethod
    def update_if_exists(self, namespace: str, key: str, value: str):
        """Update value only if the entry already exists. No-op if it doesn't."""
        pass

    @abstractmethod
    def remove(self, namespace: str, key: str):
        pass

    @abstractmethod
    def remove_if_exists(self, namespace: str, key: str) -> bool:
        """Remove entry if it exists. Returns True if it was present and deleted, False otherwise."""
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

    @abstractmethod
    def insert_many(self, namespace: str, items: dict):
        """Upsert multiple key/value pairs as a single logged call (bulk insert())."""
        pass

    @abstractmethod
    def get_many(self, namespace: str, keys: list) -> dict:
        """Fetch multiple keys as a single logged call; keys with no entry are simply absent from the result."""
        pass

    @abstractmethod
    def remove_many(self, namespace: str, keys: list):
        """Remove multiple keys as a single logged call (bulk remove())."""
        pass

# key value store (sqlite version) ---------------------------------------

_KEY_VALUE_STORE_PATH = ".cache/key_value_store.db"

class SQLiteKeyValueStore(AbstractKeyValueStore):
    def __init__(self, db_path=_KEY_VALUE_STORE_PATH, table_name="kv"):
        self._db_path = db_path
        self._table_name = table_name
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table_name} (
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
                conn.execute(f"""
                    INSERT INTO {self._table_name}(namespace, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value
                """, (namespace, key, value))
                conn.commit()

    def update_if_exists(self, namespace: str, key: str, value: str):
        if not isinstance(value, str):
            raise TypeError("value must be str")
        with LogService("KVStore", "update_if_exists", path=namespace, size=len(key) + len(value)):
            with self._conn() as conn:
                conn.execute(f"""
                    UPDATE {self._table_name} SET value = ?
                    WHERE namespace = ? AND key = ?
                """, (value, namespace, key))
                conn.commit()

    def remove(self, namespace: str, key: str):
        with LogService("KVStore", "remove", path=namespace, size=len(key)):
            with self._conn() as conn:
                conn.execute(f"DELETE FROM {self._table_name} WHERE namespace = ? AND key = ?", (namespace, key))
                conn.commit()

    def remove_if_exists(self, namespace: str, key: str) -> bool:
        with LogService("KVStore", "remove_if_exists", path=namespace, size=len(key)):
            with self._conn() as conn:
                cur = conn.execute(
                    f"DELETE FROM {self._table_name} WHERE namespace = ? AND key = ?",
                    (namespace, key)
                )
                conn.commit()
                return cur.rowcount > 0

    def remove_all(self, namespace: str):
        with LogService("KVStore", "remove_all", path=namespace):
            with self._conn() as conn:
                conn.execute(f"DELETE FROM {self._table_name} WHERE namespace = ?", (namespace,))
                conn.commit()

    def get(self, namespace: str, key: str) -> str:
        with LogService("KVStore", "get", path=namespace) as log:
            with self._conn() as conn:
                cur = conn.execute(
                    f"SELECT value FROM {self._table_name} WHERE namespace = ? AND key = ?",
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
                    f"SELECT key, value FROM {self._table_name} WHERE namespace = ? ORDER BY key",
                    (namespace,)
                )
                payload = cur.fetchall()
            log.size = json_size(payload)
        return list(payload)

    def count(self, namespace: str) -> int:
        with LogService("KVStore", "count", path=namespace):
            with self._conn() as conn:
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {self._table_name} WHERE namespace = ?",
                    (namespace,)
                )
                return int(cur.fetchone()[0])

    def insert_many(self, namespace: str, items: dict):
        if not items:
            return
        size = sum(len(k) + len(v) for k, v in items.items())
        with LogService("KVStore", "insert_many", path=namespace, size=size):
            with self._conn() as conn:
                conn.executemany(f"""
                    INSERT INTO {self._table_name}(namespace, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value
                """, [(namespace, k, v) for k, v in items.items()])
                conn.commit()

    def get_many(self, namespace: str, keys: list) -> dict:
        if not keys:
            return {}
        with LogService("KVStore", "get_many", path=namespace) as log:
            placeholders = ",".join("?" * len(keys))
            with self._conn() as conn:
                cur = conn.execute(
                    f"SELECT key, value FROM {self._table_name} WHERE namespace = ? AND key IN ({placeholders})",
                    (namespace, *keys)
                )
                rows = cur.fetchall()
            log.size = json_size(rows)
        return dict(rows)

    def remove_many(self, namespace: str, keys: list):
        if not keys:
            return
        with LogService("KVStore", "remove_many", path=namespace, size=len(keys)):
            placeholders = ",".join("?" * len(keys))
            with self._conn() as conn:
                conn.execute(
                    f"DELETE FROM {self._table_name} WHERE namespace = ? AND key IN ({placeholders})",
                    (namespace, *keys)
                )
                conn.commit()

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

    def update_if_exists(self, namespace: str, key: str, value: str):
        if not isinstance(value, str):
            raise TypeError("value must be str")
        if not isinstance(key, str):
            raise TypeError("key must be str")
        with LogService("KVStore", "update_if_exists", path=namespace, size=len(key) + len(value)):
            try:
                self._table.put_item(
                    Item={"namespace": namespace, "key": key, "value": value},
                    ConditionExpression="attribute_exists(#ns) AND attribute_exists(#k)",
                    ExpressionAttributeNames={"#ns": "namespace", "#k": "key"},
                )
            except self._client.exceptions.ConditionalCheckFailedException:
                pass  # Item no longer exists; no-op

    def remove(self, namespace: str, key: str):
        if not isinstance(key, str):
            raise TypeError("key must be str")
        with LogService("KVStore", "remove", path=namespace, size=len(key)):
            self._table.delete_item(Key={"namespace": namespace, "key": key})

    def remove_if_exists(self, namespace: str, key: str) -> bool:
        if not isinstance(key, str):
            raise TypeError("key must be str")
        with LogService("KVStore", "remove_if_exists", path=namespace, size=len(key)):
            resp = self._table.delete_item(
                Key={"namespace": namespace, "key": key},
                ReturnValues="ALL_OLD",
            )
            return "Attributes" in resp

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
                ConsistentRead=True,
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
                ConsistentRead=True,
            )
            items.extend((it["key"], it.get("value", "")) for it in resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = self._table.query(
                    KeyConditionExpression=Key("namespace").eq(namespace),
                    ProjectionExpression="#k,#v",
                    ExpressionAttributeNames={"#k": "key", "#v": "value"},
                    ConsistentRead=True,
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
                ConsistentRead=True,
            )
            count = resp.get("Count", 0)
            while "LastEvaluatedKey" in resp:
                resp = self._table.query(
                    KeyConditionExpression=Key("namespace").eq(namespace),
                    Select="COUNT",
                    ConsistentRead=True,
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                )
                count += resp.get("Count", 0)
        return int(count)

    def insert_many(self, namespace: str, items: dict):
        if not items:
            return
        size = sum(len(k) + len(v) for k, v in items.items())
        with LogService("KVStore", "insert_many", path=namespace, size=size):
            with self._table.batch_writer() as batch:
                for key, value in items.items():
                    batch.put_item(Item={"namespace": namespace, "key": key, "value": value})

    def get_many(self, namespace: str, keys: list) -> dict:
        if not keys:
            return {}
        result = {}
        with LogService("KVStore", "get_many", path=namespace) as log:
            # batch_get_item is capped at 100 keys per call
            for i in range(0, len(keys), 100):
                request_keys = [{"namespace": namespace, "key": k} for k in keys[i:i + 100]]
                request = {self._table_name: {"Keys": request_keys, "ConsistentRead": True}}
                while request:
                    resp = self._dynamodb.batch_get_item(RequestItems=request)
                    for item in resp.get("Responses", {}).get(self._table_name, []):
                        result[item["key"]] = item.get("value", "")
                    request = resp.get("UnprocessedKeys") or None
            log.size = json_size(result)
        return result

    def remove_many(self, namespace: str, keys: list):
        if not keys:
            return
        with LogService("KVStore", "remove_many", path=namespace, size=len(keys)):
            with self._table.batch_writer() as batch:
                for key in keys:
                    batch.delete_item(Key={"namespace": namespace, "key": key})

# platform-independent access to key value store ----------------------------------

if detect_environment() == "aws":
    KeyValueStore = DynamoDBKeyValueStore
else:
    KeyValueStore = SQLiteKeyValueStore
