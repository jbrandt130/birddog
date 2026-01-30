import os
import time
import uuid
import unittest
import tempfile

import botocore

from birddog.store import (
    # String queues
    SQLiteStringQueue,
    DynamoDBStringQueue,

    # Key-value stores
    SQLiteKeyValueStore,
    DynamoDBKeyValueStore,
)


# ------------------ STORE UNIT TESTS ------------------

class TestStores(unittest.TestCase):
    """
    Runs a common test suite against:
      - SQLiteStringQueue + DynamoDBStringQueue
      - SQLiteKeyValueStore + DynamoDBKeyValueStore

    DynamoDB tests are integration-ish: they require AWS creds/permissions.
    If DynamoDB is not reachable/authorized, those subtests are skipped.
    """

    # --------- helpers ---------

    def _assert_eventually(self, predicate, timeout_s=2.0, interval_s=0.05, msg="condition not met"):
        deadline = time.time() + timeout_s
        last_exc = None
        while time.time() < deadline:
            try:
                if predicate():
                    return
            except Exception as e:
                last_exc = e
            time.sleep(interval_s)
        if last_exc:
            raise last_exc
        self.fail(msg)

    # -------------------------
    # StringQueue common tests
    # -------------------------

    def _run_common_string_queue_tests(self, q, impl_name: str):
        queue_name = f"unittest:queue:{impl_name}:{uuid.uuid4().hex}"
        consumer_a = f"consumer-a:{uuid.uuid4().hex}"
        consumer_b = f"consumer-b:{uuid.uuid4().hex}"

        q.append(queue_name, ["a", "b", "c"])
        self.assertEqual(q.peek(queue_name, 2), ["a", "b"])
        self.assertGreaterEqual(q.length(queue_name), 3)

        # claim FIFO + invisibility
        claimed1 = q.claim(queue_name, n=1, lease_ms=10_000, consumer_id=consumer_a)
        self.assertEqual(len(claimed1), 1)
        self.assertEqual(claimed1[0].value, "a")
        self.assertEqual(q.peek(queue_name, 3), ["b", "c"])

        claimed2 = q.claim(queue_name, n=1, lease_ms=10_000, consumer_id=consumer_b)
        self.assertEqual(len(claimed2), 1)
        self.assertEqual(claimed2[0].value, "b")

        # extend keeps invisibility
        q.extend(queue_name, [claimed1[0].receipt], lease_ms=10_000, consumer_id=consumer_a)

        # ack deletes
        q.ack(queue_name, [claimed2[0].receipt], consumer_id=consumer_b)
        self.assertEqual(q.peek(queue_name, 5), ["c"])

        claimed3 = q.claim(queue_name, n=1, lease_ms=10_000, consumer_id=consumer_b)
        self.assertEqual(claimed3[0].value, "c")
        q.ack(queue_name, [claimed3[0].receipt], consumer_id=consumer_b)
        self.assertEqual(q.peek(queue_name, 5), [])

        # lease expiry causes visibility again
        q.append(queue_name, ["x"])
        short = q.claim(queue_name, n=1, lease_ms=2_000, consumer_id=consumer_a)
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0].value, "x")

        # Immediately should be invisible (allow slight timing jitter)
        self._assert_eventually(
            lambda: q.peek(queue_name, 10) == [],
            timeout_s=1.0,
            interval_s=0.05,
            msg=f"peek still showed leased item for {impl_name}",
        )

        # Wait for lease to expire and ensure it becomes claimable again
        time.sleep(2.2)
        again = q.claim(queue_name, n=1, lease_ms=10_000, consumer_id=consumer_b)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].value, "x")

        q.ack(queue_name, [again[0].receipt], consumer_id=consumer_b)
        self.assertEqual(q.peek(queue_name, 10), [])

    # -------------------------
    # KeyValueStore common tests
    # -------------------------

    def _run_common_kv_store_tests(self, kv, impl_name: str):
        # Use unique namespace per run
        ns = f"unittest:kv:{impl_name}:{uuid.uuid4().hex}"
        k1, v1 = "k1", "v1"
        k2, v2 = "k2", "v2"
        k3, v3 = "k3", "v3"
        k_empty, v_empty = "empty", ""

        # get() on missing key should raise KeyError (both implementations do)
        with self.assertRaises(KeyError):
            kv.get(ns, "missing")

        # insert/get
        kv.insert(ns, k1, v1)
        self.assertEqual(kv.get(ns, k1), v1)
        self.assertEqual(kv.count(ns), 1)

        # upsert overwrites
        kv.insert(ns, k1, "v1b")
        self.assertEqual(kv.get(ns, k1), "v1b")
        self.assertEqual(kv.count(ns), 1)

        # insert more keys
        kv.insert(ns, k2, v2)
        kv.insert(ns, k3, v3)
        self.assertEqual(kv.count(ns), 3)

        # get_all returns sorted by key (both implementations sort by key)
        items = kv.get_all(ns)
        self.assertEqual([k for (k, _) in items], sorted([k1, k2, k3]))
        d = dict(items)
        self.assertEqual(d[k1], "v1b")
        self.assertEqual(d[k2], v2)
        self.assertEqual(d[k3], v3)

        # empty string values should round-trip
        kv.insert(ns, k_empty, v_empty)
        self.assertEqual(kv.get(ns, k_empty), v_empty)

        # remove missing key should be idempotent (no exception)
        kv.remove(ns, "does-not-exist")

        # remove existing key
        kv.remove(ns, k2)
        with self.assertRaises(KeyError):
            kv.get(ns, k2)
        self.assertEqual(kv.count(ns), 3)  # k1, k3, empty

        # remove_all wipes namespace
        kv.remove_all(ns)
        self.assertEqual(kv.count(ns), 0)
        self.assertEqual(kv.get_all(ns), [])
        with self.assertRaises(KeyError):
            kv.get(ns, k1)

    # -------------------------
    # The actual tests
    # -------------------------

    def test_string_queue(self):
        # SQLite queue
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "string_queues_test.db")
            sqlite_q = SQLiteStringQueue(db_path=db_path)
            with self.subTest(impl="sqlite"):
                self._run_common_string_queue_tests(sqlite_q, "sqlite")

        # DynamoDB queue
        table_name = f"birddog_string_queues_unittest_{uuid.uuid4().hex[:12]}"
        try:
            ddb_q = DynamoDBStringQueue(table_name=table_name)

            # Smoke check to validate creds/access early
            smoke_queue = f"smoke:{uuid.uuid4().hex}"
            ddb_q.append(smoke_queue, ["smoke"])
            claimed = ddb_q.claim(smoke_queue, n=1, lease_ms=5_000, consumer_id="smoke-consumer")
            if claimed:
                ddb_q.ack(smoke_queue, [claimed[0].receipt], consumer_id="smoke-consumer")

        except (
            botocore.exceptions.NoCredentialsError,
            botocore.exceptions.PartialCredentialsError,
            botocore.exceptions.EndpointConnectionError,
            botocore.exceptions.ClientError,
        ) as e:
            self.skipTest(f"DynamoDBStringQueue not available (skipping): {e}")
            return

        with self.subTest(impl="dynamodb"):
            self._run_common_string_queue_tests(ddb_q, "dynamodb")

    def test_key_value_store(self):
        # SQLite KV
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "key_value_store_test.db")
            sqlite_kv = SQLiteKeyValueStore(db_path=db_path)
            with self.subTest(impl="sqlite"):
                self._run_common_kv_store_tests(sqlite_kv, "sqlite")

        # DynamoDB KV
        table_name = f"birddog_key_value_store_unittest_{uuid.uuid4().hex[:12]}"
        try:
            ddb_kv = DynamoDBKeyValueStore(table_name=table_name)

            # Smoke check
            ns = f"smoke:{uuid.uuid4().hex}"
            ddb_kv.insert(ns, "k", "v")
            self.assertEqual(ddb_kv.get(ns, "k"), "v")
            ddb_kv.remove_all(ns)

        except (
            botocore.exceptions.NoCredentialsError,
            botocore.exceptions.PartialCredentialsError,
            botocore.exceptions.EndpointConnectionError,
            botocore.exceptions.ClientError,
        ) as e:
            self.skipTest(f"DynamoDBKeyValueStore not available (skipping): {e}")
            return

        with self.subTest(impl="dynamodb"):
            self._run_common_kv_store_tests(ddb_kv, "dynamodb")


if __name__ == "__main__":
    unittest.main()
