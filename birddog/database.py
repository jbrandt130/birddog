# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

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

class SchemaError(DatabaseError):
    """Invalid Schema configuration"""

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

# ---------------------------------------------------------------------------
# Abstract Database class
# ---------------------------------------------------------------------------

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

    def scan_all(self, table_name):
        """
        Convenience method to scan all records in a table using scan()
        """
        records, cursor = self.scan(table_name)
        while cursor:
            more, cursor = self.scan(table_name, cursor=cursor)
            records.extend(more)
        return records

    # ---------------------------------------------------------------------------
    # API Specification:
    #     scan()              - page through table records
    #     lookup()            - look up record ids based on unique keys
    #     read()              - return records given record ids
    #     write()             - create/update records
    #     delete()            - delete records
    #     create_links()      - link a source record to one or more targetss
    #     delete_links()      - delete one or more links from source to targets
    #     get_links()         - return linked record ids from a given record
    #     set_attachments()   - attach one or more files to a record
    #     clear_attachments() - clear file attachments from a record
    # ---------------------------------------------------------------------------
    
    def scan(self, table_name, limit=100, cursor=None):
        """
        Page through records of a table without requiring the table key.

        Args:
            table_name:
                Logical table name (must exist in the NocoDB base).

            limit:
                Maximum number of records to return in this call. The backend may
                return fewer records (e.g., last page) but must not return more.

            cursor:
                Opaque paging token returned by a previous call to scan(), or None
                to request the first page. In this implementation the cursor is a
                stringified integer offset.

        Returns:
            (records, next_cursor)
                records:
                    List of record dicts as returned by NocoDB.
                next_cursor:
                    None if there are no more pages, otherwise an opaque token that
                    can be passed back to scan() to retrieve the next page.

        Semantics:
            - scan() is read-only and side-effect free.
            - Record ordering is backend-defined but stable across pages for a given
              traversal.
            - If cursor is invalid (non-integer for this backend), InvalidRecordId
              is raised.

        Errors:
            - InvalidTableName
            - InvalidRecordId
            - FailedIO
        """
        raise NotImplementedError

    def lookup(self, table_name, key_set):
        """
        Look up record_id(s) by the table’s key field.

        Args:
            table:
                Table name.

            key_set:
                A single key value (str) or a set of key values.

        Returns:
            - If key_set is a string: record_id (str) if found, else None.
            - If key_set is a set: dict of key:record_id values for all matching
              keys in the table.

        Required semantics:
            - Not-found is NOT an error; missing keys are omitted in the returned dict.

        Errors:
            - InvalidTableName
            - FailedIO
            - ValueError: key_set is not a string or set of strings
        """
        raise NotImplementedError

    def read(self, table_name, record_id):
        """
        Read record(s) by record_id.

        Args:
            table_name:
                Table name.

            record_id:
                A single record_id (str) or a sequence of record_ids.

        Returns:
            - If record_id is a string: dict for the record, or {} if not found.
            - If record_id is a sequence: list of dicts in the same order/length,
              using {} for not-found entries.

        Required semantics:
            - Not-found is NOT an error; return {} for that record_id.
            - Batch returns MUST preserve order and length.

        Errors:
            - InvalidTableName
            - InvalidRecordId          (syntactically invalid ID; not "not found")
            - FailedIO
        """
        raise NotImplementedError

    def write(self, table_name, records):
        """
        Create or update record(s) in a table using the table's key field.

        Args:
            table_name:
                Logical table name.

            records:
                Either a single dict (single-record write) or a list/tuple of dicts
                (batch write). Each dict must include the table key field defined in
                the Schema tables (self._schema[table_name]["key"]).

                Partial update semantics:
                    - Only fields present in each record dict are written.
                    - Fields not present are left unchanged.

                Validation semantics:
                    - Field names must exist in the logical schema (Schema tables),
                      unless they are present in the NocoDB column list for the table
                      (to allow system fields such as "Id" when needed).
                    - Field values must satisfy basic type validation based on the
                      Schema field type. For select fields, values must be within the
                      allowed values defined in "Schema Values".

        Returns:
            - If records is a single dict: the record_id (NocoDB "Id") of the written record.
            - If records is a batch: list of record_ids in the same order as input.

        Semantics / atomicity:
            - All-or-none for validation errors:
                * The full input (single or batch) is validated before any network
                  calls are made. If validation fails, no write is attempted and an
                  appropriate exception is raised.
            - IO/backend failures:
                * If a network or backend error occurs while applying updates/creates,
                  FailedIO is raised and partial updates MAY have occurred.

        Errors:
            - InvalidTableName
            - MissingKey
            - InvalidFieldName
            - InvalidFieldValue
            - FailedIO
        """
        raise NotImplementedError

    def delete(self, table_name, record_id):
        """
        Delete record(s) from a table by record_id (NocoDB "Id").

        Args:
            table_name:
                Logical table name.

            record_id:
                A single record_id (str/int) or a list/tuple of record_ids.

        Returns:
            Total number of records deleted (as reported by the backend). Deleting a
            non-existent record is not treated as an error; the backend may return
            fewer deletions than requested.

        Semantics / atomicity:
            - The implementation issues one or more batch DELETE requests to NocoDB.
            - On FailedIO, partial deletions may have occurred.

        Errors:
            - InvalidTableName
            - FailedIO
        """
        raise NotImplementedError

    def create_links(self, table_name, link_field, source_record, target_records):
        """
        Create relation link(s) from a source record to one or more target records.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table (must correspond to a
                NocoDB link-type column).

            source_record:
                Record_id ("Id") of the source record.

            target_records:
                Either a single target record_id or a list/tuple of target record_ids.

        Semantics:
            - Adds the specified target link(s) to the relation.
            - Should be idempotent if the backend ignores duplicates (NocoDB typically does).

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - FailedIO
        """
        raise NotImplementedError

    def delete_links(self, table_name, link_field, source_record, target_records):
        """
        Remove relation link(s) from a source record to one or more target records.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table.

            source_record:
                Record_id ("Id") of the source record.

            target_records:
                Either a single target record_id or a list/tuple of target record_ids.

        Semantics:
            - Removes the specified target link(s) if they exist.
            - Idempotent: if a link does not exist, no error is raised (backend behavior).

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - FailedIO
        """
        raise NotImplementedError

    def get_links(self, table_name, link_field, source_record):
        """
        Retrieve the linked target record_ids for a relation field on a source record.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table.

            source_record:
                Record_id ("Id") of the source record.

        Returns:
            A list of linked target record_ids (strings/ints as returned by NocoDB).
            If no links exist, returns an empty list.

        Semantics:
            - Supports both paged and non-paged NocoDB responses:
                * If NocoDB returns a paged payload with "list" and "pageInfo", the
                  method iterates pages until "isLastPage" is True.
                * If NocoDB returns a singular object with an "Id", that single Id is
                  returned as a one-element list.

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - FailedIO
        """
        raise NotImplementedError

    def set_attachments(self, table_name, record_id, attachment_field, file_paths):
        """
        Replace the contents of an attachment field with one or more files.

        Semantics:
          - Uploads all provided files
          - Replaces the attachment field entirely
          - Does NOT preserve existing attachments
        """
        raise NotImplementedError

    def clear_attachments(self, table_name, record_id, attachment_field):
        """
        Clear (remove) all files from an attachment field.

        Semantics:
          - Replaces the attachment field with an empty list
          - Does NOT delete files from NocoDB storage
        """
        raise NotImplementedError
