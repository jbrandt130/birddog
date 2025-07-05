# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Common logging support
"""

import logging
import sys
import os
import requests
from collections import deque
from datetime import datetime

# EPHEMERAL SYSTEM LOGGING --------------------------------------------------

#
# deployment environment sniffer

"""
_environment = None
def detect_environment():
    global _environment
    if not _environment:
        _environment = "local"
        try:
            r = requests.get("http://169.254.169.254/latest/meta-data/", timeout=0.1)
            if r.status_code == 200:
                _environment = "aws"
        except requests.RequestException:
            pass
    return _environment
"""

def detect_environment():
    if os.environ.get("BIRDDOG_AWS_ENVIRONMENT"):
        return "aws"
    return "local"

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

# PERSISTENT EVENT LOGGING --------------------------------------------------

import sqlite3
import threading
import pandas as pd

_EVENT_LOG_PATH = ".cache/request_logs.db"

class SQLLiteLogger():
    def __init__(self, db_path=_EVENT_LOG_PATH):
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
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

_DYNAMODB_TABLE_NAME = "birddog_request_logs"
#_AWS_REGION = "us-east-1"

class DynamoDBLogger:
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
            df["timestamp"] = pd.to_datetime(df["timestamp"])
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

class DummyLogger:
    def __init__(self):
        pass

    def log_request(self, user, method, path, status, duration):
        pass

    def load_logs(self, start_time, end_time):
        return pd.DataFrame()

_event_logger = None

def get_event_logger():
    global _event_logger
    if not _event_logger:
        if detect_environment() == "aws":
            #_event_logger = DummyLogger()
            _event_logger = DynamoDBLogger()
        else:
            #_event_logger = DynamoDBLogger()
            _event_logger = SQLLiteLogger()
            #_event_logger = DummyLogger()
    return _event_logger

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


