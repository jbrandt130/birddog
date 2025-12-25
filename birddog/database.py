# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

from abc import ABC, abstractmethod
from pydantic import BaseModel, Field, constr


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class DatabaseError(Exception):
    """Base class for all database-related errors."""


class InvalidTableName(DatabaseError):
    """Table name is unknown or not configured."""


class InvalidRecordId(DatabaseError):
    """
    Record ID is syntactically invalid for this backend.

    Note: a *valid* record_id that simply does not exist is NOT an error;
    read() and lookup() handle that as "not found" (empty dict / None).
    """


class InvalidFieldName(DatabaseError):
    """Field name does not exist in the table schema."""


class InvalidFieldValue(DatabaseError):
    """
    Field value fails local validation:

    - wrong type (e.g., string where number is required)
    - not in allowed choices for single_select / multi_select
    - violates a simple local constraint (nullable=False and value is None, etc.)

    These errors are detected before any writes are attempted.
    """


class MissingKey(DatabaseError):
    """
    Payload for write() is missing the table's key field.

    The key field name is describe_table(table).key.
    """


class InvalidSourceId(DatabaseError):
    """
    Source record identifier passed to link()/unlink() is syntactically invalid.

    As with InvalidRecordId, this refers to ID *format*, not to "record not found".
    """


class InvalidTargetId(DatabaseError):
    """
    Target record identifier passed to link()/unlink() is syntactically invalid.

    As with InvalidRecordId, this refers to ID *format*, not to "record not found".
    """


class FileNotFound(DatabaseError):
    """Local file path does not exist or is unreadable for attach_file()."""


class FailedIO(DatabaseError):
    """
    Underlying IO / backend failure:

    - network error
    - remote HTTP error (4xx/5xx)
    - database connection/transaction error
    - storage failure, etc.

    After FailedIO, the final state of the affected records MAY be partial or
    unknown depending on the backend. See method docstrings for details.
    """


class Database:
    """
    Abstract interface for a Birddog-compatible database backend.

    Core concepts:
        - A BACKEND-specific record identifier (record_id: str).
        - A TABLE-specific business key field (TableSchema.key), used for lookups.
        - Table schemas described via Pydantic models (TableSchema, FieldSpec).

    Methods generally support both scalar and batch usage:
        - If you pass a scalar (str, dict), you get a scalar return value.
        - If you pass a list, you get a list of matching shape/order,
          except where documented otherwise (e.g., delete -> int count).

    All-or-none semantics:
        - For validation errors (MissingKey, InvalidFieldName, InvalidFieldValue),
          NO writes are attempted for any element of the batch.
        - For IO/backend errors (FailedIO), the final state may be partial or
          unknown; callers MUST assume some records may have been mutated.
    """
    pass

