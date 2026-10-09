"""Persistent local request history for the Azure OpenAI test proxy."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid


SCHEMA_VERSION = 1
PREVIEW_BYTES = 4096
TEXT_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "application/x-ndjson",
    "text/",
)


def utc_now():
    return datetime.now(timezone.utc)


def as_timestamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_text_content(content_type):
    content_type = (content_type or "").lower()
    return any(item in content_type for item in TEXT_CONTENT_TYPES)


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS history_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    retention_mode TEXT NOT NULL
                        CHECK (retention_mode IN ('forever', 'max_count', 'max_age_days')),
                    retention_value INTEGER,
                    CHECK (
                        (retention_mode = 'forever' AND retention_value IS NULL)
                        OR (retention_mode = 'max_count' AND retention_value BETWEEN 1 AND 10000)
                        OR (retention_mode = 'max_age_days' AND retention_value BETWEEN 1 AND 3650)
                    )
                );
                CREATE TABLE IF NOT EXISTS request_history (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path_template TEXT NOT NULL,
                    actual_path TEXT NOT NULL,
                    query_string TEXT NOT NULL,
                    request_headers TEXT NOT NULL,
                    request_content_type TEXT NOT NULL,
                    request_body BLOB NOT NULL,
                    resource_name TEXT,
                    endpoint_type TEXT,
                    deployment_name TEXT,
                    response_status INTEGER,
                    response_headers TEXT NOT NULL DEFAULT '{}',
                    response_content_type TEXT NOT NULL DEFAULT '',
                    response_size INTEGER NOT NULL DEFAULT 0,
                    response_preview BLOB NOT NULL DEFAULT X'',
                    duration_ms INTEGER,
                    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
                    storage_error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_history_operation_created
                    ON request_history(document_id, method, path_template, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_history_created
                    ON request_history(created_at);
                CREATE TABLE IF NOT EXISTS response_body_chunks (
                    history_id TEXT NOT NULL REFERENCES request_history(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    body BLOB NOT NULL,
                    PRIMARY KEY (history_id, sequence)
                );
                """
            )
            row = connection.execute(
                "SELECT value FROM history_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported request history schema version {row['value']}."
                )
            connection.execute(
                "INSERT OR IGNORE INTO history_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO history_settings(id, retention_mode, retention_value)
                VALUES (1, 'forever', NULL)
                """
            )
            connection.commit()
        self.prune()

    def create_record(
        self,
        *,
        document_id,
        operation_id,
        method,
        path_template,
        actual_path,
        query_string,
        request_headers,
        request_content_type,
        request_body,
        resource_name=None,
        endpoint_type=None,
        deployment_name=None,
    ):
        history_id = str(uuid.uuid4())
        created_at = as_timestamp(utc_now())
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO request_history(
                    id, document_id, operation_id, method, path_template, actual_path,
                    query_string, request_headers, request_content_type, request_body,
                    resource_name, endpoint_type, deployment_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    history_id,
                    document_id,
                    operation_id,
                    method,
                    path_template,
                    actual_path,
                    query_string,
                    json.dumps(request_headers, ensure_ascii=False, separators=(",", ":")),
                    request_content_type or "",
                    bytes(request_body),
                    resource_name,
                    endpoint_type,
                    deployment_name,
                    created_at,
                ),
            )
            connection.commit()
        return history_id, utc_now()

    def append_response_chunk(self, history_id, sequence, body):
        if not body:
            return
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT response_preview FROM request_history WHERE id = ?",
                (history_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown history record {history_id}.")
            existing_preview = row["response_preview"] or b""
            if isinstance(existing_preview, str):
                existing_preview = existing_preview.encode("utf-8")
            preview = bytes(existing_preview) + bytes(body)[:PREVIEW_BYTES - len(existing_preview)]
            connection.execute(
                """
                INSERT INTO response_body_chunks(history_id, sequence, body)
                VALUES (?, ?, ?)
                """,
                (history_id, sequence, bytes(body)),
            )
            connection.execute(
                """
                UPDATE request_history
                SET response_size = response_size + ?, response_preview = ?
                WHERE id = ?
                """,
                (len(body), sqlite3.Binary(preview), history_id),
            )
            connection.commit()

    def complete_record(
        self,
        history_id,
        *,
        status,
        response_headers,
        response_content_type,
        duration_ms,
        completed=True,
        storage_error=None,
    ):
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE request_history
                SET response_status = ?, response_headers = ?, response_content_type = ?,
                    duration_ms = ?, completed = ?, storage_error = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(response_headers, ensure_ascii=False, separators=(",", ":")),
                    response_content_type or "",
                    max(0, int(duration_ms)),
                    int(completed),
                    storage_error,
                    as_timestamp(utc_now()),
                    history_id,
                ),
            )
            connection.commit()
        self.prune()

    def record_response(
        self,
        history_id,
        *,
        status,
        response_headers,
        response_content_type,
        body,
        duration_ms,
        completed=True,
        storage_error=None,
    ):
        if body:
            self.append_response_chunk(history_id, 0, body)
        self.complete_record(
            history_id,
            status=status,
            response_headers=response_headers,
            response_content_type=response_content_type,
            duration_ms=duration_ms,
            completed=completed,
            storage_error=storage_error,
        )

    def get_settings(self):
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT retention_mode, retention_value FROM history_settings WHERE id = 1"
            ).fetchone()
        return {
            "retention_mode": row["retention_mode"],
            "retention_value": row["retention_value"],
        }

    def update_settings(self, mode, value):
        if mode == "forever":
            value = None
        elif mode == "max_count":
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueError("按条数保留时，retention_value 必须是 1..10000 的整数。")
        elif mode == "max_age_days":
            if type(value) is not int or not 1 <= value <= 3650:
                raise ValueError("按天数保留时，retention_value 必须是 1..3650 的整数。")
        else:
            raise ValueError("retention_mode 必须是 forever、max_count 或 max_age_days。")
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE history_settings
                SET retention_mode = ?, retention_value = ?
                WHERE id = 1
                """,
                (mode, value),
            )
            connection.commit()
        deleted = self.prune()
        return {**self.get_settings(), "deleted_count": deleted}

    def prune(self):
        settings = self.get_settings()
        with self._lock, closing(self._connect()) as connection:
            deleted = 0
            if settings["retention_mode"] == "max_age_days":
                cutoff = as_timestamp(
                    utc_now() - timedelta(days=settings["retention_value"])
                )
                deleted = connection.execute(
                    "DELETE FROM request_history WHERE created_at < ?",
                    (cutoff,),
                ).rowcount
            elif settings["retention_mode"] == "max_count":
                deleted = connection.execute(
                    """
                    DELETE FROM request_history
                    WHERE id IN (
                        SELECT id
                        FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY document_id, method, path_template
                                ORDER BY created_at DESC, id DESC
                            ) AS position
                            FROM request_history
                        )
                        WHERE position > ?
                    )
                    """,
                    (settings["retention_value"],),
                ).rowcount
            connection.commit()
        return deleted

    def list_records(self, document_id, method, path_template, limit, offset):
        with self._lock, closing(self._connect()) as connection:
            total = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM request_history
                WHERE document_id = ? AND method = ? AND path_template = ?
                """,
                (document_id, method, path_template),
            ).fetchone()["count"]
            rows = connection.execute(
                """
                SELECT id, operation_id, method, path_template, actual_path, query_string,
                       request_content_type, length(request_body) AS request_size,
                       resource_name, endpoint_type, deployment_name, response_status,
                       response_content_type, response_size, response_preview, duration_ms,
                       completed, storage_error, created_at, completed_at
                FROM request_history
                WHERE document_id = ? AND method = ? AND path_template = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (document_id, method, path_template, limit, offset),
            ).fetchall()
        return total, [self._record_summary(row) for row in rows]

    def _record_summary(self, row):
        result = {key: row[key] for key in row.keys() if key != "response_preview"}
        result["completed"] = bool(result["completed"])
        preview = bytes(row["response_preview"] or b"")
        if not preview:
            result["response_summary"] = ""
        elif is_text_content(row["response_content_type"]):
            result["response_summary"] = preview.decode("utf-8", errors="replace")
        else:
            result["response_summary"] = f"二进制内容 · {row['response_size']} bytes"
        result["response_summary_truncated"] = row["response_size"] > len(preview)
        return result

    def get_record(self, history_id):
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, document_id, operation_id, method, path_template, actual_path,
                       query_string, request_headers, request_content_type,
                       length(request_body) AS request_size, resource_name, endpoint_type,
                       deployment_name, response_status, response_headers,
                       response_content_type, response_size, duration_ms, completed,
                       storage_error, created_at, completed_at
                FROM request_history WHERE id = ?
                """,
                (history_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["request_headers"] = json.loads(result["request_headers"])
        result["response_headers"] = json.loads(result["response_headers"])
        result["completed"] = bool(result["completed"])
        return result

    def get_request_body(self, history_id):
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT request_body FROM request_history WHERE id = ?",
                (history_id,),
            ).fetchone()
        return None if row is None else bytes(row["request_body"])

    def iter_response_body(self, history_id):
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                SELECT body FROM response_body_chunks
                WHERE history_id = ? ORDER BY sequence
                """,
                (history_id,),
            )
            for row in cursor:
                yield bytes(row["body"])
        finally:
            connection.close()

    def delete_record(self, history_id):
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM request_history WHERE id = ?",
                (history_id,),
            )
            connection.commit()
            return cursor.rowcount == 1

    def clear_operation(self, document_id, method, path_template):
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                DELETE FROM request_history
                WHERE document_id = ? AND method = ? AND path_template = ?
                """,
                (document_id, method, path_template),
            )
            connection.commit()
            return cursor.rowcount
