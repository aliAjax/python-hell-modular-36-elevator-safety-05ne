import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine, recovery_blockers


SYSTEM_ACTOR = Actor("system", "admin")


def _now():
    return datetime.now(timezone.utc)


def _parse_time(value, field):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValidationError(field + " must be ISO-8601")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        # Keep server-assigned defaults (severity, round) produced by validation.
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "alarm" and payload.get("severity") == "severe":
            self._isolate_group(entity)
        return entity

    # ----- group isolation -------------------------------------------------

    def _isolation_scope(self, equipment, alarm):
        """A severe alarm stops every in-service unit of the same group.

        Units sharing a physical group (same ``group_id``) stop together. When
        the reporting unit has no group, the scope is the reporting unit only,
        while any other unit explicitly pointing at the same location is
        included so bank-level entrapment is not missed.
        """
        group_id = equipment["data"].get("group_id")
        units = []
        for item in self.repository.list_entities(kind="equipment"):
            if item["status"] not in ("in_service", "suspended"):
                continue
            if group_id:
                if item["data"].get("group_id") == group_id:
                    units.append(item)
            elif item["id"] == equipment["id"]:
                units.append(item)
        if not any(item["id"] == equipment["id"] for item in units):
            units.append(equipment)
        return units

    def _isolate_group(self, alarm):
        equipment = self.repository.get_entity(alarm["data"]["equipment_id"])
        scope = self._isolation_scope(equipment, alarm)
        scope_ids = [item["id"] for item in scope]
        now_text = _now().isoformat(timespec="seconds")
        scheduled = (
            (_now() + timedelta(days=int(equipment["data"].get("inspection_interval_days", 365) or 365)))
            .isoformat(timespec="seconds")
        )
        existing = self.repository.get_entity("isolation-" + alarm["id"])
        isolation = None
        if existing:
            data = dict(existing["data"])
            data["scope"] = scope_ids
            data["updated_at"] = now_text
            isolation = self.repository.system_update(existing["id"], existing["status"], data)
            self.audit.record(existing["id"], SYSTEM_ACTOR, "scope_extended", existing["status"], existing["status"], {"alarm_id": alarm["id"], "scope": scope_ids})
        else:
            isolation = self.repository.create_entity(
                "isolation-" + alarm["id"],
                "isolation",
                "active",
                {
                    "alarm_id": alarm["id"],
                    "equipment_id": equipment["id"],
                    "severity": alarm["data"].get("severity", "severe"),
                    "scope": scope_ids,
                    "created_at": now_text,
                },
                "system",
            )
            self.audit.record(isolation["id"], SYSTEM_ACTOR, "create", None, "active", {"kind": "isolation", "alarm_id": alarm["id"], "scope": scope_ids})

        reinspection_keys = {
            (item["data"].get("equipment_id"), item["data"].get("trigger_alarm_id"))
            for item in self.repository.list_entities(kind="inspection")
            if item["data"].get("trigger_alarm_id") == alarm["id"]
        }
        for unit in scope:
            latest = self.repository.get_entity(unit["id"])
            if latest["status"] == "in_service":
                data = dict(latest["data"])
                data["isolated_by"] = alarm["id"]
                data["isolated_at"] = now_text
                data["isolation_scope"] = scope_ids
                self.repository.system_update(unit["id"], "suspended", data)
                self.audit.record(unit["id"], SYSTEM_ACTOR, "suspend", "in_service", "suspended", {"reason": "severe alarm", "alarm_id": alarm["id"], "isolation_id": isolation["id"]})
            elif latest["status"] == "suspended" and not latest["data"].get("isolated_by"):
                data = dict(latest["data"])
                data["isolated_by"] = alarm["id"]
                data["isolated_at"] = now_text
                data["isolation_scope"] = scope_ids
                self.repository.system_update(unit["id"], "suspended", data)
                self.audit.record(unit["id"], SYSTEM_ACTOR, "isolation_mark", "suspended", "suspended", {"alarm_id": alarm["id"], "isolation_id": isolation["id"]})

            # Permits, maintenance and re-inspection change together with the stop.
            self._revoke_unit_permits(latest, alarm)
            self._hold_unit_maintenance(latest, alarm)
            if (latest["id"], alarm["id"]) not in reinspection_keys:
                inspection = self.repository.create_entity(
                    "reinspection-%s-%s" % (alarm["id"], latest["id"]),
                    "inspection",
                    "scheduled",
                    {
                        "equipment_id": latest["id"],
                        "scheduled_at": scheduled,
                        "cycle_days": int(latest["data"].get("inspection_interval_days", 365) or 365),
                        "kind": "reinspection",
                        "trigger_alarm_id": alarm["id"],
                        "isolation_id": isolation["id"],
                    },
                    "system",
                )
                self.audit.record(inspection["id"], SYSTEM_ACTOR, "create", None, "scheduled", {"kind": "inspection", "reason": "group isolation", "alarm_id": alarm["id"]})
        return isolation

    def _revoke_unit_permits(self, unit, alarm):
        for permit in self.repository.list_entities(kind="permit"):
            if permit["data"].get("equipment_id") != unit["id"]:
                continue
            if permit["status"] in ("granted", "pending_review"):
                data = dict(permit["data"])
                data["revoked_reason"] = "group isolation"
                data["revoked_by_alarm"] = alarm["id"]
                updated = self.repository.system_update(permit["id"], "revoked", data)
                self.audit.record(permit["id"], SYSTEM_ACTOR, "revoke", permit["status"], updated["status"], {"reason": "group isolation", "alarm_id": alarm["id"]})

    def _hold_unit_maintenance(self, unit, alarm):
        for work in self.repository.list_entities(kind="maintenance"):
            if work["data"].get("equipment_id") != unit["id"]:
                continue
            data = dict(work["data"])
            data["held_by_alarm"] = alarm["id"]
            if work["status"] == "in_progress":
                updated = self.repository.system_update(work["id"], "paused", data)
                self.audit.record(work["id"], SYSTEM_ACTOR, "pause", work["status"], updated["status"], {"reason": "group isolation", "alarm_id": alarm["id"]})
            elif work["status"] == "planned":
                updated = self.repository.system_update(work["id"], "on_hold", data)
                self.audit.record(work["id"], SYSTEM_ACTOR, "hold", work["status"], updated["status"], {"reason": "group isolation", "alarm_id": alarm["id"]})

    def _resume_unit_maintenance(self, unit, alarm_id):
        for work in self.repository.list_entities(kind="maintenance"):
            if work["data"].get("equipment_id") != unit["id"] or work["data"].get("held_by_alarm") != alarm_id:
                continue
            data = dict(work["data"])
            data.pop("held_by_alarm", None)
            if work["status"] == "paused":
                updated = self.repository.system_update(work["id"], "in_progress", data)
                self.audit.record(work["id"], SYSTEM_ACTOR, "resume", work["status"], updated["status"], {"reason": "unit returned", "alarm_id": alarm_id})
            elif work["status"] == "on_hold":
                updated = self.repository.system_update(work["id"], "planned", data)
                self.audit.record(work["id"], SYSTEM_ACTOR, "release_hold", work["status"], updated["status"], {"reason": "unit returned", "alarm_id": alarm_id})

    # ----- transitions -----------------------------------------------------

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "equipment" and action == "return_to_service":
            self._after_unit_returned(entity, updated)
        return updated

    def _after_unit_returned(self, before, equipment):
        alarm_id = before["data"].get("isolated_by")
        if not alarm_id:
            return
        self._resume_unit_maintenance(equipment, alarm_id)
        isolation = self.repository.get_entity("isolation-" + alarm_id)
        if not isolation or isolation["status"] != "active":
            return
        remaining = []
        for unit_id in isolation["data"].get("scope", []):
            unit = self.repository.get_entity(unit_id)
            if unit and unit["status"] == "suspended" and unit["data"].get("isolated_by") == alarm_id:
                remaining.append(unit_id)
        data = dict(isolation["data"])
        data["returned"] = [unit_id for unit_id in data.get("scope", []) if unit_id not in remaining]
        if remaining:
            data["remaining"] = remaining
            updated = self.repository.system_update(isolation["id"], "active", data)
            self.audit.record(isolation["id"], SYSTEM_ACTOR, "unit_returned", "active", "active", {"equipment_id": equipment["id"], "remaining": remaining})
        else:
            data["released_at"] = _now().isoformat(timespec="seconds")
            self.repository.system_update(isolation["id"], "released", data)
            self.audit.record(isolation["id"], SYSTEM_ACTOR, "release", "active", "released", {"equipment_id": equipment["id"]})

    # ----- rescue queue ----------------------------------------------------

    def rescue_queue(self):
        """Jobs queue by ETA/arrival; severe jobs cut ahead of normal ones."""
        jobs = [job for job in self.repository.list_entities(kind="rescue_job") if job["status"] in ("dispatched", "on_site")]

        def key(job):
            data = job["data"]
            severe = 0 if data.get("severity") == "severe" else 1
            on_site = 0 if job["status"] == "on_site" else 1
            arrived = data.get("arrived_at") or data.get("eta") or job.get("created_at", "")
            return (severe, on_site, arrived, job["id"])

        ordered = sorted(jobs, key=key)
        items = []
        for index, job in enumerate(ordered, start=1):
            items.append(
                {
                    "position": index,
                    "rescue_job_id": job["id"],
                    "alarm_id": job["data"].get("alarm_id"),
                    "team": job["data"].get("team"),
                    "severity": job["data"].get("severity", "normal"),
                    "status": job["status"],
                    "eta": job["data"].get("eta"),
                    "arrived_at": job["data"].get("arrived_at"),
                }
            )
        return items

    # ----- recovery board --------------------------------------------------

    def recovery_board(self):
        board = []
        for isolation in self.repository.list_entities(kind="isolation"):
            alarm = self.repository.get_entity(isolation["data"]["alarm_id"])
            units = []
            returned = list(isolation["data"].get("returned", []))
            for unit_id in isolation["data"].get("scope", []):
                unit = self.repository.get_entity(unit_id)
                if not unit:
                    continue
                blockers = recovery_blockers(
                    self._lookup, unit, isolation["data"]["alarm_id"]
                ) if unit["status"] == "suspended" and unit["data"].get("isolated_by") == isolation["data"]["alarm_id"] else []
                units.append(
                    {
                        "equipment_id": unit_id,
                        "status": unit["status"],
                        "blockers": blockers,
                        "ready": not blockers and unit["status"] == "suspended",
                        "returned": unit_id in returned or unit["status"] == "in_service",
                        "recovery_snapshot": unit["data"].get("recovery_snapshot"),
                    }
                )
            board.append(
                {
                    "isolation_id": isolation["id"],
                    "alarm_id": isolation["data"]["alarm_id"],
                    "severity": isolation["data"].get("severity"),
                    "alarm_status": alarm["status"] if alarm else None,
                    "scope": isolation["data"].get("scope", []),
                    "status": isolation["status"],
                    "units": units,
                }
            )
        return board

    # ----- offline merge ---------------------------------------------------

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity.

        Records carry an explicit integer version: higher versions overwrite
        older ones, equal versions are idempotent replays, and lower versions
        are kept queryable but never applied. Equipment reading updates happen
        atomically, so a replay after restart or scale-out cannot change the
        same equipment twice.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            payload = dict(raw)
            payload["source_id"] = source_id
            payload["record_id"] = record_id
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            entity, outcome = self.repository.upsert_offline_record(entity_id, payload)
            if outcome == "equipment_conflict":
                raise ConflictError("offline record references equipment that cannot be updated: " + str(payload.get("equipment_id")))
            action_map = {
                "created": "merge_offline",
                "updated": "merge_offline_update",
                "duplicate": "merge_offline_replay",
                "superseded": "merge_offline_superseded",
            }
            self.audit.record(
                entity_id,
                actor,
                action_map[outcome],
                entity["status"],
                entity["status"],
                {"source_id": source_id, "record_id": record_id, "version": payload["version"], "outcome": outcome},
            )
            item = dict(entity)
            item["merge_outcome"] = outcome
            created.append(item)
        return created

    def offline_record_versions(self, source_id=None, record_id=None):
        records = self.repository.list_entities(kind="offline_record")
        items = []
        for entity in records:
            data = entity["data"]
            if source_id and data.get("source_id") != source_id:
                continue
            if record_id and data.get("record_id") != record_id:
                continue
            items.append(
                {
                    "id": entity["id"],
                    "source_id": data.get("source_id"),
                    "record_id": data.get("record_id"),
                    "version": data.get("version"),
                    "equipment_id": data.get("equipment_id"),
                    "applied": data.get("applied", False),
                    "created_at": entity["created_at"],
                    "updated_at": entity["updated_at"],
                }
            )
        items.sort(key=lambda item: (str(item["source_id"]), str(item["record_id"]), -int(item["version"] or 1)))
        return items

    # ----- basic access ----------------------------------------------------

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
