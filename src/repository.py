import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        entities = self.list_entities(kind=kind)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def system_update(self, entity_id, status, data):
        """Apply a cascade side effect without an optimistic version check.

        Cascade updates run inside one orchestration step; callers are trusted
        internal use cases, not API clients, and each change is still audited.
        """
        return self.update_entity(entity_id, None, status, data)

    def upsert_offline_record(self, entity_id, payload):
        """Atomically merge a versioned offline record.

        Returns (entity, outcome) where outcome is one of:
        "created", "updated", "duplicate", "superseded", "equipment_conflict".
        The whole read/merge/write happens in one transaction so the same
        request replayed after a restart or on another instance cannot change
        equipment readings twice.
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, version, data FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            incoming_version = int(payload["version"])
            if row:
                current = {
                    "status": row["status"],
                    "version": int(row["version"]),
                    "data": json.loads(row["data"]),
                }
                stored_version = int(current["data"].get("version", 1))
                if incoming_version == stored_version:
                    outcome = "duplicate"
                elif incoming_version < stored_version:
                    outcome = "superseded"
                else:
                    applied_readings = self._apply_offline_readings(connection, current, payload, now)
                    if applied_readings is None:
                        connection.rollback()
                        return self.get_entity(entity_id), "equipment_conflict"
                    merged = dict(current["data"])
                    merged.update(payload)
                    merged["version"] = incoming_version
                    merged["applied"] = True
                    merged["updated_at_offline"] = now
                    connection.execute(
                        "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                        "WHERE id = ?",
                        (current["status"], json.dumps(merged, ensure_ascii=False, sort_keys=True), now, entity_id),
                    )
                    outcome = "updated"
                connection.commit()
            else:
                stored_payload = dict(payload)
                stored_payload["applied"] = bool(stored_payload.get("readings"))
                self._apply_offline_readings(connection, None, payload, now)
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                    "VALUES (?, ?, 'merged', 1, ?, 'offline', ?, ?)",
                    (
                        entity_id,
                        "offline_record",
                        json.dumps(stored_payload, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
                outcome = "created"
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id), outcome

    @staticmethod
    def _apply_offline_readings(connection, current_record, payload, now):
        readings = payload.get("readings")
        equipment_id = payload.get("equipment_id")
        if not readings or not equipment_id:
            return True
        row = connection.execute(
            "SELECT version, data FROM entities WHERE id = ? AND kind = 'equipment'",
            (equipment_id,),
        ).fetchone()
        if not row:
            return None
        equipment_data = json.loads(row["data"])
        offline_fields = equipment_data.get("offline_fields", {})
        incoming_marker = {"source_id": payload.get("source_id"), "record_id": payload.get("record_id"), "version": int(payload["version"])}
        for key, value in readings.items():
            field = offline_fields.get(key)
            if current_record is None:
                # First time this record is seen: always apply.
                offline_fields[key] = {"value": value, "marker": incoming_marker}
                continue
            if field and field.get("marker") == incoming_marker:
                continue  # same request replayed; do not touch equipment twice
            offline_fields[key] = {"value": value, "marker": incoming_marker}
        equipment_data["offline_fields"] = offline_fields
        connection.execute(
            "UPDATE entities SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(equipment_data, ensure_ascii=False, sort_keys=True), now, equipment_id),
        )
        return True

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
