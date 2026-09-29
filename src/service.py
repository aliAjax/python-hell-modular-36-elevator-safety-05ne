import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    RecoveryBlocked,
    ValidationError,
)
from .rules import (
    ISO_KEYS,
    ALARM_TERMINAL,
    RuleEngine,
    isolation_scope,
    now_iso,
    recovery_blockers,
    recovery_blockers_for,
    rescue_queue,
)

# Equipment fields a field device is allowed to patch through offline records.
OFFLINE_EQUIPMENT_PATCH_FIELDS = ("location", "field_note")


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
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        if kind == "rescue_job":
            alarm = self.repository.get_entity(payload.get("alarm_id"))
            if alarm and "severity" not in payload:
                payload["severity"] = alarm["data"].get("severity", "normal")
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if kind == "alarm":
            entity = self._apply_isolation(actor, entity)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _apply_isolation(self, actor, alarm):
        """Stop the units inside the alarm scope and cascade permit/maintenance rules.

        Every alarm stops its own unit; a severe alarm widens the stop scope to
        the whole equipment group. Units already isolated by another alarm keep
        that ownership. Re-inspections are scheduled once per (alarm, unit) and
        permits pending review are pushed back to blocked.
        """
        target = self.repository.get_entity(alarm["data"]["equipment_id"])
        if not target:
            return alarm
        all_equipment = self.repository.list_entities(kind="equipment")
        equipment_by_id = {item["id"]: item for item in all_equipment}
        scope_ids, scope_kind = isolation_scope(alarm, equipment_by_id, all_equipment)

        inserts = []
        updates = []
        audits = []
        stamped_at = now_iso()
        try:
            cycle_days = int(target["data"].get("inspection_interval_days", 365))
        except (TypeError, ValueError):
            cycle_days = 365
        all_permits = self.repository.list_entities(kind="permit")
        all_inspections = self.repository.list_entities(kind="inspection")

        for equipment_id in scope_ids:
            equipment = equipment_by_id.get(equipment_id)
            if not equipment:
                continue
            data = dict(equipment["data"])
            previous_status = equipment["status"]
            changed = False
            if not data.get("isolated"):
                data.update({
                    "isolated": True,
                    "isolation_reason": "active_alarm",
                    "isolated_by_alarm": alarm["id"],
                    "isolated_at": stamped_at,
                    "suspended_at": stamped_at,
                })
                new_status = "suspended" if previous_status == "in_service" else previous_status
                updates.append((equipment["id"], equipment["version"], new_status, data))
                audits.append({
                    "entity_id": equipment["id"],
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "isolate",
                    "from_status": previous_status,
                    "to_status": new_status,
                    "detail": {"alarm_id": alarm["id"], "scope": scope_kind, "severity": alarm["data"].get("severity", "normal")},
                })
                changed = True
            # Permits waiting for review cannot pass while the unit is stopped.
            for permit in all_permits:
                if permit["data"].get("equipment_id") != equipment_id:
                    continue
                if permit["status"] == "pending_review":
                    permit_data = dict(permit["data"])
                    permit_data["blocked_reason"] = "isolation:" + alarm["id"]
                    updates.append((permit["id"], permit["version"], "blocked", permit_data))
                    audits.append({
                        "entity_id": permit["id"],
                        "actor_id": actor.user_id,
                        "actor_role": actor.role,
                        "action": "isolation_cascade",
                        "from_status": "pending_review",
                        "to_status": "blocked",
                        "detail": {"alarm_id": alarm["id"], "equipment_id": equipment_id},
                    })
            if changed:
                inspection_id = "insp-" + hashlib.sha256((alarm["id"] + "|" + equipment_id).encode("utf-8")).hexdigest()[:16]
                existing = self.repository.get_entity(inspection_id)
                duplicate = existing is not None
                if not duplicate:
                    for inspection in all_inspections:
                        if (
                            inspection["data"].get("equipment_id") == equipment_id
                            and inspection["data"].get("reinspection_for_alarm") == alarm["id"]
                            and inspection["status"] in ("scheduled", "failed")
                        ):
                            duplicate = True
                            break
                if not duplicate:
                    inspection_data = {
                        "equipment_id": equipment_id,
                        "scheduled_at": stamped_at,
                        "cycle_days": cycle_days,
                        "reinspection_for_alarm": alarm["id"],
                        "kind": "reinspection",
                    }
                    inserts.append({
                        "id": inspection_id,
                        "kind": "inspection",
                        "status": "scheduled",
                        "data": inspection_data,
                        "created_by": actor.user_id,
                    })
                    audits.append({
                        "entity_id": inspection_id,
                        "actor_id": actor.user_id,
                        "actor_role": actor.role,
                        "action": "create",
                        "from_status": None,
                        "to_status": "scheduled",
                        "detail": {"kind": "inspection", "reinspection_for_alarm": alarm["id"]},
                    })

        alarm_data = dict(alarm["data"])
        alarm_data.update({
            "activated_at": alarm_data.get("activated_at") or alarm_data.get("occurred_at") or stamped_at,
            "scope_equipment_ids": scope_ids,
            "scope_kind": scope_kind,
        })
        updates.append((alarm["id"], alarm["version"], alarm["status"], alarm_data))
        audits.append({
            "entity_id": alarm["id"],
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": "isolation_applied",
            "from_status": alarm["status"],
            "to_status": alarm["status"],
            "detail": {"scope_equipment_ids": scope_ids, "scope_kind": scope_kind},
        })

        self.repository.save_batch(inserts=inserts, updates=updates, audits=audits)
        return self.repository.get_entity(alarm["id"])

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if entity["kind"] == "alarm" and action == "recover":
            return self.recover_group(actor, entity_id, patch, expected_version)
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected_version if expected_version is not None else entity["version"], next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def recover_group(self, actor, alarm_id, patch=None, expected_version=None):
        """Recover every unit in the stop scope after per-unit verification.

        Each unit must have no other active alarm, no open remediation and a
        re-inspection passed after isolation started. Otherwise recovery is
        refused and every blocking reason is returned. A snapshot of alarms,
        remediation and inspection state is kept on the alarm for evidence.
        """
        alarm = self.repository.get_entity(alarm_id)
        if not alarm:
            raise NotFoundError("entity not found: " + alarm_id)
        if alarm["kind"] != "alarm":
            raise ValidationError("group recovery only applies to alarms")
        if alarm["status"] != "closed":
            raise InvalidTransition("cannot recover from status " + alarm["status"])
        expected = int(expected_version) if expected_version is not None else alarm["version"]
        if expected != alarm["version"]:
            raise ConflictError("version conflict: expected %s, found %s" % (expected, alarm["version"]))

        scope_ids = list(dict(alarm["data"]).get("scope_equipment_ids") or [alarm["data"].get("equipment_id")])
        equipment_by_id = {item["id"]: item for item in self.repository.list_entities(kind="equipment")}
        blockers = recovery_blockers(alarm, equipment_by_id, self._lookup)
        if blockers:
            raise RecoveryBlocked(
                "group recovery blocked for %d unit(s)" % len(blockers),
                blockers=blockers,
            )

        updates = []
        audits = []
        recovered_at = now_iso()
        snapshots = []

        all_alarms = self.repository.list_entities(kind="alarm")
        all_remediations = self.repository.list_entities(kind="remediation")
        all_inspections = self.repository.list_entities(kind="inspection")

        for equipment_id in scope_ids:
            equipment = equipment_by_id.get(equipment_id)
            if not equipment:
                continue
            data = dict(equipment["data"])
            is_owner = data.get("isolated_by_alarm") == alarm["id"]
            unit_snapshot = {
                "equipment_id": equipment_id,
                "asset_no": data.get("asset_no"),
                "status_before": equipment["status"],
                "ownership_released": is_owner,
                "alarms": [
                    {"id": item["id"], "status": item["status"], "code": item["data"].get("code")}
                    for item in all_alarms if item["data"].get("equipment_id") == equipment_id
                ],
                "remediations": [
                    {"id": item["id"], "status": item["status"], "issue": item["data"].get("issue")}
                    for item in all_remediations
                    if item["data"].get("equipment_id") == equipment_id
                    or item["data"].get("alarm_id") == alarm["id"]
                ],
                "inspections": [
                    {
                        "id": item["id"],
                        "status": item["status"],
                        "passed_at": item["data"].get("passed_at"),
                        "reinspection_for_alarm": item["data"].get("reinspection_for_alarm"),
                    }
                    for item in all_inspections
                    if item["data"].get("equipment_id") == equipment_id
                ],
            }
            if is_owner:
                for key in ISO_KEYS:
                    data.pop(key, None)
                new_status = "in_service"
                updates.append((equipment["id"], equipment["version"], new_status, data))
                audits.append({
                    "entity_id": equipment["id"],
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "group_recover",
                    "from_status": equipment["status"],
                    "to_status": new_status,
                    "detail": {"alarm_id": alarm["id"], "snapshot": unit_snapshot},
                })
                unit_snapshot["status_after"] = new_status
            else:
                unit_snapshot["status_after"] = equipment["status"]
            snapshots.append(unit_snapshot)

        alarm_data = dict(alarm["data"])
        record = {
            "recovered_at": recovered_at,
            "recovered_by": (patch or {}).get("recovered_by", actor.user_id),
            "scope_equipment_ids": scope_ids,
            "units": snapshots,
        }
        history = list(alarm_data.get("recovery_snapshots", []))
        history.append(record)
        alarm_data["recovery_snapshots"] = history
        alarm_data["last_recovery"] = record
        updates.append((alarm["id"], alarm["version"], "recovered", alarm_data))
        audits.append({
            "entity_id": alarm["id"],
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": "recover",
            "from_status": "closed",
            "to_status": "recovered",
            "detail": {"scope_equipment_ids": scope_ids, "snapshot": record},
        })

        self.repository.save_batch(updates=updates, audits=audits)
        return self.repository.get_entity(alarm["id"])

    def rescue_queue(self):
        """Active rescue jobs ordered severe-first then by earliest arrival."""
        return rescue_queue(self._lookup)

    def isolation_overview(self):
        """Current stop scopes with the blockers that keep each unit stopped."""
        alarms = self.repository.list_entities(kind="alarm")
        equipment_by_id = {item["id"]: item for item in self.repository.list_entities(kind="equipment")}
        active = [alarm for alarm in alarms if alarm["status"] not in ALARM_TERMINAL + ("recovered",)]
        overview = []
        for alarm in active:
            scope_ids = list(alarm["data"].get("scope_equipment_ids") or [alarm["data"].get("equipment_id")])
            units = []
            for equipment_id in scope_ids:
                equipment = equipment_by_id.get(equipment_id)
                if not equipment:
                    continue
                units.append({
                    "equipment_id": equipment_id,
                    "asset_no": equipment["data"].get("asset_no"),
                    "group_id": equipment["data"].get("group_id"),
                    "status": equipment["status"],
                    "isolated_by_alarm": equipment["data"].get("isolated_by_alarm"),
                    "blockers": recovery_blockers_for(alarm, equipment, self._lookup),
                })
            overview.append({
                "alarm_id": alarm["id"],
                "status": alarm["status"],
                "severity": alarm["data"].get("severity", "normal"),
                "code": alarm["data"].get("code"),
                "scope_kind": alarm["data"].get("scope_kind"),
                "scope_equipment_ids": scope_ids,
                "units": units,
            })
        return {"items": overview}

    def merge_offline(self, actor, records, request_id=None):
        """Merge field inspection records by (source_id, record_id) with versions.

        A higher version supersedes the stored one; older submissions are kept
        with status ``ignored_old`` so they stay queryable. A ``request_id``
        makes the whole batch idempotent: after a restart or on another
        instance a replay returns the stored result and never touches
        equipment twice.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        request_id = str(request_id or "").strip() or None
        if request_id:
            stored = self.repository.get_merge_request(request_id)
            if stored is not None:
                response = dict(stored["response"])
                response["replayed"] = True
                response["request_id"] = request_id
                return response

        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            self.rules.validate_create(actor, "offline_record", dict(raw), self._lookup)

        inserts = []
        updates = []
        audits = []
        result_items = []
        equipment_patches = {}

        for raw in records:
            source_id = str(raw["source_id"]).strip()
            record_id = str(raw["record_id"]).strip()
            new_version = int(raw.get("version", 1))
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            version_entity_id = entity_id + "-v" + str(new_version)
            current = self.repository.get_entity(entity_id)
            version_row = self.repository.get_entity(version_entity_id)

            if version_row is not None:
                result_items.append({"record_id": entity_id, "version": new_version, "outcome": "duplicate"})
                continue

            version_payload = {
                "source_id": source_id,
                "record_id": record_id,
                "version": new_version,
                "payload": dict(raw),
                "merged_by": actor.user_id,
                "merged_at": now_iso(),
            }

            if current is None:
                payload = dict(raw)
                payload["current_version"] = new_version
                inserts.append({"id": entity_id, "kind": "offline_record", "status": "current", "data": payload, "created_by": actor.user_id})
                inserts.append({"id": version_entity_id, "kind": "offline_record_version", "status": "applied", "data": version_payload, "created_by": actor.user_id})
                audits.append({
                    "entity_id": entity_id, "actor_id": actor.user_id, "actor_role": actor.role,
                    "action": "merge_offline", "from_status": None, "to_status": "current",
                    "detail": {"source_id": source_id, "record_id": record_id, "version": new_version, "outcome": "created"},
                })
                outcome = "created"
                if raw.get("equipment_id"):
                    equipment_patches.setdefault(raw["equipment_id"], {})["record"] = (entity_id, new_version, raw)
            else:
                current_version = int(current["data"].get("current_version", 1))
                if new_version > current_version:
                    prior_id = entity_id + "-v" + str(current_version)
                    prior = self.repository.get_entity(prior_id)
                    if prior and prior["status"] == "applied":
                        prior_data = dict(prior["data"])
                        updates.append((prior_id, prior["version"], "superseded", prior_data))
                    payload = dict(raw)
                    payload["current_version"] = new_version
                    updates.append((entity_id, current["version"], "current", payload))
                    inserts.append({"id": version_entity_id, "kind": "offline_record_version", "status": "applied", "data": version_payload, "created_by": actor.user_id})
                    audits.append({
                        "entity_id": entity_id, "actor_id": actor.user_id, "actor_role": actor.role,
                        "action": "merge_offline", "from_status": "current", "to_status": "current",
                        "detail": {"source_id": source_id, "record_id": record_id, "version": new_version, "outcome": "updated", "from_version": current_version},
                    })
                    outcome = "updated"
                    if raw.get("equipment_id"):
                        equipment_patches.setdefault(raw["equipment_id"], {})["record"] = (entity_id, new_version, raw)
                else:
                    version_payload["superseded_by"] = current_version
                    inserts.append({"id": version_entity_id, "kind": "offline_record_version", "status": "ignored_old", "data": version_payload, "created_by": actor.user_id})
                    audits.append({
                        "entity_id": version_entity_id, "actor_id": actor.user_id, "actor_role": actor.role,
                        "action": "merge_offline", "from_status": None, "to_status": "ignored_old",
                        "detail": {"source_id": source_id, "record_id": record_id, "version": new_version, "current_version": current_version, "outcome": "ignored_old"},
                    })
                    outcome = "ignored_old"
            result_items.append({"record_id": entity_id, "version": new_version, "outcome": outcome})

        # Apply whitelisted equipment patches once per unit, even when several
        # records in the same batch target it.
        for equipment_id, bundle in equipment_patches.items():
            entity_id, version, raw = bundle["record"]
            equipment = self.repository.get_entity(equipment_id)
            if not equipment:
                continue
            data = dict(equipment["data"])
            for field in OFFLINE_EQUIPMENT_PATCH_FIELDS:
                if raw.get(field) is not None:
                    data[field] = raw[field]
            data["last_offline_record"] = entity_id
            data["last_offline_version"] = version
            updates.append((equipment_id, equipment["version"], equipment["status"], data))
            audits.append({
                "entity_id": equipment_id, "actor_id": actor.user_id, "actor_role": actor.role,
                "action": "offline_patch", "from_status": equipment["status"], "to_status": equipment["status"],
                "detail": {"record_id": entity_id, "version": version},
            })

        response = {"items": result_items, "replayed": False, "request_id": request_id}
        self.repository.save_batch(
            inserts=inserts,
            updates=updates,
            audits=audits,
            merge_request=(request_id, actor.user_id, response) if request_id else None,
        )
        return response

    def offline_versions(self, record_id):
        if not record_id:
            raise ValidationError("record_id is required")
        rows = self.repository.list_entities(kind="offline_record_version")
        versions = [
            row for row in rows
            if row["data"].get("source_id") and (
                row["id"].rsplit("-v", 1)[0] == record_id
            )
        ]
        versions.sort(key=lambda row: int(row["data"].get("version", 0)))
        return {"items": versions}

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
