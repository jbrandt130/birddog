# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Common logging support
"""

import logging
import sys
import time
import threading
from collections import deque
from datetime import datetime, UTC, timezone
import sqlite3

import boto3
from boto3.dynamodb.conditions import Key

import pandas as pd

from birddog.env import detect_environment

_SQLLITE_LOG_PATH = ".cache/logs.db"

def _iso_utc(dt) -> str:
    """Normalize to ISO-8601 UTC with microseconds (e.g., '2025-09-08T19:13:45.123456+00:00')."""
    if isinstance(dt, str):
        ts = pd.to_datetime(dt, utc=True, errors="raise")
        dt = ts.to_pydatetime()
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec="microseconds")
    raise TypeError("start_time/end_time must be datetime or ISO string")

# EPHEMERAL SYSTEM LOGGING --------------------------------------------------

class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity=1000):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record):
        self.buffer.append(self.format(record))

    def get_logs(self, limit=None):
        if limit:
            return list(self.buffer)[-limit:]
        return list(self.buffer)

LOGGING_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_log_buffer_handler = InMemoryLogHandler()
_log_buffer_handler.setFormatter(logging.Formatter(LOGGING_FORMAT))

def get_logger():
    # Configure the logging system
    logging.basicConfig(
        level=logging.INFO,  # Change to DEBUG for more detailed logs
        format=LOGGING_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),  # <-- Critical for EB log capture
            _log_buffer_handler
        ]
    )
    return logging.getLogger(__name__)

def get_log_buffer():
    return _log_buffer_handler


# helper for log truncation

def _to_iso_utc_str(date_string):
    if isinstance(date_string, datetime):
        return date_string.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds")
    # assume user passed formatted string
    return str(date_string)

# PERSISTENT EVENT LOGGING (abstract base class) ------------------------

class EventLogger:
    _logger = None

    @staticmethod
    def get_logger():
        if not EventLogger._logger:
            if detect_environment() == "aws":
                EventLogger._logger = DynamoDBEventLogger()
            else:
                EventLogger._logger = SQLLiteEventLogger()
        return EventLogger._logger

    def log_request(self, user, method, path, status, duration):
        raise NotImplementedError

    def load_logs(self, start_time, end_time):
        raise NotImplementedError

    def truncate(self, cutoff):
        """Remove all log entries older than cutoff (inclusive-exclusive: timestamp < cutoff)."""
        raise NotImplementedError

# SQLITE-BASED PERSISTENT EVENT LOGGING ------------------------

class SQLLiteEventLogger(EventLogger):
    def __init__(self, db_path=_SQLLITE_LOG_PATH):
        super().__init__()
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    user TEXT,
                    method TEXT,
                    path TEXT,
                    status INTEGER,
                    duration INTEGER
                )
            ''')
            conn.commit()

    def log_request(self, user, method, path, status, duration):
        timestamp = datetime.utcnow().isoformat()
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                '''
                INSERT INTO request_logs (timestamp, user, method, path, status, duration)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    timestamp,
                    user,
                    method,
                    path,
                    int(status),
                    int(duration * 1000)
                )
            )
            conn.commit()

    def load_logs(self, start_time, end_time):
        with sqlite3.connect(self._db_path) as conn:
            query = """
            SELECT * FROM request_logs
            WHERE timestamp BETWEEN ? and ?
            """
            if isinstance(start_time, datetime):
                start_time = start_time.isoformat()
            if isinstance(end_time, datetime):
                end_time = end_time.isoformat()
            df = pd.read_sql_query(query, conn, params=(start_time, end_time))
            df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601")
            return df

    def truncate(self, cutoff):
        # Accept str or datetime; store as ISO string for SQLite datetime() casting
        cutoff_s = _to_iso_utc_str(cutoff)

        with self._lock, sqlite3.connect(self._db_path) as conn:
            # datetime() makes the comparison robust to tz/precision differences
            cur = conn.execute(
                "DELETE FROM request_logs WHERE datetime(timestamp) < datetime(?)",
                (cutoff_s,),
            )
            conn.commit()
            return cur.rowcount

# DYNAMODB-BASED PERSISTENT EVENT LOGGING ------------------------

import boto3
from boto3.dynamodb.conditions import Key

_DYNAMODB_TABLE_NAME = "birddog_request_logs"

class DynamoDBEventLogger(EventLogger):
    def __init__(self, table_name=_DYNAMODB_TABLE_NAME):
        self._ensure_dynamodb_log_table()
        self._table = boto3.resource("dynamodb").Table(table_name)
        self._lock = threading.Lock()

    def log_request(self, user, method, path, status, duration):
        timestamp = datetime.utcnow().isoformat()
        with self._lock:
            self._table.put_item(Item={
                "PK": "LOG",
                "timestamp": timestamp,
                "user": user,
                "method": method,
                "path": path,
                "status": int(status),
                "duration": int(duration*1000),
            })

    def load_logs(self, start_time, end_time):
        if isinstance(start_time, datetime):
            start_time = start_time.isoformat()
        if isinstance(end_time, datetime):
            end_time = end_time.isoformat()

        response = self._table.query(
            KeyConditionExpression=Key("PK").eq("LOG") & Key("timestamp").between(start_time, end_time)
        )

        items = response.get("Items", [])

        # Pagination (if needed)
        while "LastEvaluatedKey" in response:
            response = self._table.query(
                KeyConditionExpression=Key("PK").eq("LOG") & Key("timestamp").between(start_time, end_time),
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items", []))

        df = pd.DataFrame(items)
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601")
        return df

    def _ensure_dynamodb_log_table(self, table_name=_DYNAMODB_TABLE_NAME):
        dynamodb = boto3.client("dynamodb")

        # Check if the table exists
        existing_tables = dynamodb.list_tables()["TableNames"]
        if table_name in existing_tables:
            #_logger.info(f"✅ Table '{table_name}' already exists.")
            return

        # Create the table
        #_logger.info(f"⏳ Creating DynamoDB table '{table_name}'...")
        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},      # Partition key
                {"AttributeName": "timestamp", "KeyType": "RANGE"}  # Sort key
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST"
        )

        # Wait until table is active
        waiter = boto3.resource("dynamodb").meta.client.get_waiter("table_exists")
        waiter.wait(TableName=table_name)
        #print(f"✅ Table '{table_name}' created and ready.")

    def truncate(self, cutoff):
        # Match stored format: this table writes naive utcnow().isoformat()
        cutoff_s = _to_iso_utc_str(cutoff)

        deleted = 0
        exclusive_start_key = None
        while True:
            kwargs = {
                "KeyConditionExpression": Key("PK").eq("LOG") & Key("timestamp").lt(cutoff_s),
                "Limit": 100,
            }
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key

            resp = self._table.query(**kwargs)
            items = resp.get("Items", [])

            if not items:
                break

            with self._table.batch_writer() as batch:
                for it in items:
                    batch.delete_item(Key={"PK": it["PK"], "timestamp": it["timestamp"]})
                    deleted += 1

            exclusive_start_key = resp.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        return deleted


# EVENT METRICS --------------------------------------------------

def summarize_duration_by_path_group(df):
    """
    Returns summary stats (count, avg, min, max duration) grouped by path_group.

    Parameters:
        df (pd.DataFrame): Must contain 'path' and 'duration' columns.

    Returns:
        pd.DataFrame: Grouped summary with count, avg, min, max duration.
    """
    if "path" not in df.columns or "duration" not in df.columns:
        raise ValueError("DataFrame must include 'path' and 'duration' columns")

    # Add path_group column
    def extract_root(path):
        parts = path.strip("/").split("/")
        return "/" + parts[0] if parts else "/"

    df = df.copy()
    df["path_group"] = df["path"].apply(extract_root)

    # Group and aggregate
    summary = df.groupby("path_group")["duration"].agg(
        count="count",
        avg="mean",
        min="min",
        max="max"
    ).sort_values(by="count", ascending=False)

    return summary

def user_histogram(df):
    return df["user"].value_counts()


# SERVICE CALL LOGGING (abstract base class) ------------------------

class ServiceLogger:
    _logger = None

    @staticmethod
    def get_logger():
        if not ServiceLogger._logger:
            if detect_environment() == "aws":
                ServiceLogger._logger = DynamoDBServiceLogger()
            else:
                ServiceLogger._logger = SQLLiteServiceLogger()
        return ServiceLogger._logger

    def log_service_call(self, resouce, method, path, size, duration):
        raise NotImplementedError

    def load_logs(self, start_time, end_time):
        raise NotImplementedError

    def truncate(self, cutoff):
        """Remove all log entries older than cutoff (inclusive-exclusive: timestamp < cutoff)."""
        raise NotImplementedError

    @staticmethod
    def summarize_service_usage(df: pd.DataFrame, by=None, sample_interval_minutes=None) -> pd.DataFrame:
        """
        Summarize a service-call log DataFrame.

        Parameters
        ----------
        df : DataFrame with columns: 'resource', 'method', 'size', 'duration'
        by : str or sequence[str]
            Grouping keys, e.g. ("resource", "method") or "resource"

        Returns
        -------
        DataFrame with columns: <by...>, count, cumulative_size, cumulative_duration
        """
        if not by:
            by = ["resource", "method"]
        if isinstance(by, str):
            by = [by]

        needed = {"resource", "size", "duration"} | ({"method"} if "method" in by else set())
        missing = needed - set(df.columns)
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

        # Coerce types
        size = pd.to_numeric(df["size"], errors="coerce").fillna(0)
        dur_col = df["duration"]
        if pd.api.types.is_timedelta64_dtype(dur_col):
            duration = dur_col.dt.total_seconds().fillna(0.0)
        else:
            duration = pd.to_numeric(dur_col, errors="coerce").fillna(0.0)

        tmp = df.copy()
        tmp["size"] = size
        tmp["duration"] = duration

        grouped = (
            tmp.groupby(by, dropna=False, observed=True)
               .agg(
                   count=("size", "size"),
                   cumulative_size=("size", "sum"),
                   cumulative_duration=("duration", "sum"),
               )
               .reset_index()
        )

        if sample_interval_minutes and sample_interval_minutes > 0:
            # include per-minute rates
            grouped["count_per_minute"] = grouped["count"] / sample_interval_minutes
            grouped["size_per_minute"] = grouped["cumulative_size"] / sample_interval_minutes

        return grouped

# SQLITE-BASED SERVICE CALL LOGGING ------------------------

class SQLLiteServiceLogger(ServiceLogger):
    def __init__(self, db_path=_SQLLITE_LOG_PATH):
        super().__init__()
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS service_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    resource TEXT,
                    method TEXT,
                    path TEXT,
                    size INTEGER,
                    duration INTEGER
                );
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_service_logs_ts ON service_logs(timestamp);
            ''')
            conn.commit()

    def log_service_call(self, resource, method, path, size, duration):
        # timezone-aware ISO string; include microseconds for ordering fidelity
        timestamp = datetime.now(UTC).isoformat(timespec="microseconds")

        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                '''
                INSERT INTO service_logs (timestamp, resource, method, path, size, duration)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    timestamp,
                    resource,
                    method,
                    path,
                    int(size or 0),
                    int((duration or 0) * 1000),  # ms, consistent with your other loggers
                )
            )
            conn.commit()

    def load_logs(self, start_time, end_time):
        start_s = _iso_utc(start_time)
        end_s   = _iso_utc(end_time)

        query = """
            SELECT * FROM service_logs
            WHERE timestamp BETWEEN ? AND ?
        """
        with sqlite3.connect(self._db_path) as conn:
            df = pd.read_sql_query(query, conn, params=(start_s, end_s))

        if not df.empty:
            # Stored as ISO strings with +00:00; parse as UTC
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        return df

    def truncate(self, cutoff):
        # Accept str or datetime; normalize to full ISO-8601 UTC with microseconds (how this table writes)
        cutoff_s = _to_iso_utc_str(cutoff)

        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM service_logs WHERE datetime(timestamp) < datetime(?)",
                (cutoff_s,),
            )
            conn.commit()
            return cur.rowcount

# DYNAMODB-BASED SERVICE CALL LOGGING ------------------------

_DYNAMODB_SERVICE_TABLE_NAME = "birddog_service_logs"

class DynamoDBServiceLogger(ServiceLogger):
    def __init__(self, table_name=_DYNAMODB_SERVICE_TABLE_NAME):
        super().__init__()
        self._ensure_dynamodb_log_table(table_name)
        self._table = boto3.resource("dynamodb").Table(table_name)
        self._lock = threading.Lock()

    def log_service_call(self, resource, method, path, size, duration):
        """
        Store one service-call log entry.
          - size: bytes (None -> 0)
          - duration: seconds (float) -> stored as milliseconds (int), mirroring SQLite/Event logger
        """
        timestamp = datetime.utcnow().isoformat()
        with self._lock:
            self._table.put_item(Item={
                "PK": "LOG",
                "timestamp": timestamp,
                "resource": resource,
                "method": method,
                "path": path,
                "size": int(size or 0),
                "duration": int((duration or 0) * 1000),
            })

    def load_logs(self, start_time, end_time):
        """
        Load logs in [start_time, end_time] (inclusive) into a pandas DataFrame.
        Accepts datetimes or ISO8601 strings. Returns an empty DF if none found.
        """
        if isinstance(start_time, datetime):
            start_time = start_time.isoformat()
        if isinstance(end_time, datetime):
            end_time = end_time.isoformat()

        response = self._table.query(
            KeyConditionExpression=Key("PK").eq("LOG") & Key("timestamp").between(start_time, end_time)
        )

        items = response.get("Items", [])

        # Pagination
        while "LastEvaluatedKey" in response:
            response = self._table.query(
                KeyConditionExpression=Key("PK").eq("LOG") & Key("timestamp").between(start_time, end_time),
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items", []))

        df = pd.DataFrame(items)
        if not df.empty:
            # Normalize types/columns to match SQLite logger conventions
            df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601")
            if "size" in df.columns:
                df["size"] = pd.to_numeric(df["size"], errors="coerce").fillna(0).astype("int64")
            else:
                df["size"] = 0
            if "duration" in df.columns:
                # duration stored in milliseconds to mirror sibling class
                df["duration"] = pd.to_numeric(df["duration"], errors="coerce").fillna(0).astype("int64")
            else:
                df["duration"] = 0
            for c in ("resource", "method", "path"):
                if c not in df.columns:
                    df[c] = ""
            # Optional: sort by time
            df = df.sort_values("timestamp").reset_index(drop=True)

        return df

    def _ensure_dynamodb_log_table(self, table_name=_DYNAMODB_SERVICE_TABLE_NAME):
        dynamodb = boto3.client("dynamodb")
        existing_tables = dynamodb.list_tables()["TableNames"]
        if table_name in existing_tables:
            return

        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},          # Partition key
                {"AttributeName": "timestamp", "KeyType": "RANGE"}   # Sort key
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST"
        )

        waiter = boto3.resource("dynamodb").meta.client.get_waiter("table_exists")
        waiter.wait(TableName=table_name)

    def truncate(self, cutoff):
        # Match stored format: this table writes naive utcnow().isoformat()
        cutoff_s = _to_iso_utc_str(cutoff)

        deleted = 0
        exclusive_start_key = None
        while True:
            kwargs = {
                "KeyConditionExpression": Key("PK").eq("LOG") & Key("timestamp").lt(cutoff_s),
                "Limit": 100,
            }
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key

            resp = self._table.query(**kwargs)
            items = resp.get("Items", [])

            if not items:
                break

            with self._table.batch_writer() as batch:
                for it in items:
                    batch.delete_item(Key={"PK": it["PK"], "timestamp": it["timestamp"]})
                    deleted += 1

            exclusive_start_key = resp.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        return deleted

# SERVICE CALL CONTEXT MANAGER ------------------------------

class LogService:
    def __init__(self, resource, method, path="", size=0):
        self.resource = resource
        self.method = method
        self.path = path
        self.size = size
        self.start = None
        self.logger = ServiceLogger.get_logger()

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        duration = time.perf_counter() - self.start
        self.logger.log_service_call(
            self.resource, self.method, self.path, self.size, duration
        )
