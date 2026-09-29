from datetime import datetime

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError

ALARM_TERMINAL = ("closed", "false_alarm")
SEVERE_ALARM = "severe"
SEVERITY_VALUES = ("normal", "severe")

# Equipment data keys written/cleared by isolation and recovery.
ISO_KEYS = ("isolated", "isolation_reason", "isolated_by_alarm", "isolated_at", "suspended_at")
OFFLINE_STAMP_KEYS = ("last_offline_record", "last_offline_version")


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def parse_ts(value):
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_isolated(equipment):
    return bool(equipment and equipment["data"].get("isolated"))


def _equipment_map(lookup):
    return {item["id"]: item for item in _all(lookup, "equipment")}


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")
    if _is_isolated(equipment):
        raise ConflictError("equipment is isolated by an active alarm; maintenance is frozen")


def _validate_alarm(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("alarm requires equipment")
    severity = data.get("severity", "normal")
    if severity not in SEVERITY_VALUES:
        raise ValidationError("severity must be one of " + ",".join(SEVERITY_VALUES))
    data["severity"] = severity
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ALARM_TERMINAL
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] in ALARM_TERMINAL:
        raise ValidationError("rescue_job requires an active alarm")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _validate_offline_record(data, lookup):
    _require(data, ("source_id", "record_id"))
    if data.get("equipment_id") and not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("offline record references unknown equipment")
    try:
        version = int(data.get("version", 1))
    except (TypeError, ValueError):
        raise ValidationError("version must be an integer")
    if version < 1:
        raise ValidationError("version must be >= 1")


def _remediation_touches(remediation, equipment_id, alarm):
    data = remediation["data"]
    if data.get("equipment_id") == equipment_id:
        return True
    if data.get("alarm_id") == (alarm or {}).get("id"):
        return True
    return False


def _grant_permit(actor, entity, data, lookup):
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    if not equipment or equipment["status"] not in ("in_service", "suspended"):
        raise ConflictError("permit can only be granted for a serviceable equipment")
    if _is_isolated(equipment):
        raise ConflictError("permit blocked: equipment is inside an active stop scope")
    inspections = [i for i in _all(lookup, "inspection") if i["data"].get("equipment_id") == equipment["id"] and i["status"] == "passed"]
    if not inspections:
        raise ConflictError("permit requires a passed inspection")
    blocked_by = [
        r for r in _all(lookup, "remediation")
        if r["status"] != "closed" and r["data"].get("equipment_id") == equipment["id"]
    ]
    if blocked_by:
        raise ConflictError("permit blocked by open remediation")
    active_alarms = [
        a for a in _all(lookup, "alarm")
        if a["data"].get("equipment_id") == equipment["id"] and a["status"] not in ALARM_TERMINAL
    ]
    if active_alarms:
        raise ConflictError("permit blocked by active alarm")
    return {"granted_by": actor.user_id, "granted_at": now_iso()}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _complete_rescue(actor, entity, data, lookup):
    """An alarm can only close after a rescue that actually arrived on site."""
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close while rescue jobs are still active")
    if not jobs:
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    activated_at = parse_ts(entity["data"].get("activated_at") or entity["data"].get("occurred_at"))
    valid = [
        job for job in jobs
        if job["status"] == "completed"
        and job["data"].get("arrived_at")
        and (activated_at is None or (parse_ts(job["data"].get("arrived_at")) or activated_at) >= activated_at)
    ]
    if not valid:
        raise ConflictError("alarm cannot close: no completed on-site rescue for this alarm")
    return {"resolved_by": actor.user_id}


def _arrive_rescue(actor, entity, data, lookup):
    return {"arrived_at": data.get("arrived_at") or now_iso()}


def _complete_rescue_job(actor, entity, data, lookup):
    if not entity["data"].get("arrived_at"):
        raise ConflictError("rescue job must arrive on site before completion")
    return {"completed_at": data.get("completed_at") or now_iso()}


def _pass_inspection(actor, entity, data, lookup):
    return {"passed_at": data.get("passed_at") or now_iso(), "passed_by": actor.user_id}


def _guard_maintenance_transition(actor, entity, data, lookup):
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    if _is_isolated(equipment):
        raise ConflictError("equipment is isolated by an active alarm; maintenance is frozen")
    return {}


def _guard_return_to_service(actor, entity, data, lookup):
    if _is_isolated(entity):
        raise ConflictError("equipment is inside an active stop scope; use group recovery instead")
    return {}


def isolation_scope(alarm, equipment_by_id, all_equipment):
    """Determine which equipment ids a severe alarm must stop.

    Normal alarms stop only the alarming unit. Severe alarms stop the whole
    group (equipment sharing ``group_id``); an explicit ``scope_equipment_ids``
    list overrides the group membership.
    """
    target = equipment_by_id.get(alarm["data"].get("equipment_id"))
    explicit = alarm["data"].get("scope_equipment_ids") or []
    explicit = [eid for eid in explicit if eid in equipment_by_id]
    if explicit:
        ordered = []
        for eid in explicit:
            if eid not in ordered:
                ordered.append(eid)
        if target and target["id"] not in ordered:
            ordered.insert(0, target["id"])
        return ordered, "explicit"
    if alarm["data"].get("severity") == SEVERE_ALARM and target and target["data"].get("group_id"):
        group_id = target["data"]["group_id"]
        members = [
            item["id"] for item in all_equipment
            if item["data"].get("group_id") == group_id
        ]
        if target["id"] not in members:
            members.insert(0, target["id"])
        return members, "group"
    return [alarm["data"].get("equipment_id")], "unit"


def recovery_blockers_for(alarm, equipment, lookup):
    """List every reason one unit cannot return to service yet."""
    blockers = []
    equipment_id = equipment["id"]
    for other in _all(lookup, "alarm"):
        if other["status"] in ALARM_TERMINAL:
            continue
        if other["data"].get("equipment_id") != equipment_id:
            continue
        if other["id"] == alarm["id"]:
            continue
        blockers.append({"type": "active_alarm", "alarm_id": other["id"], "code": other["data"].get("code")})
    for remediation in _all(lookup, "remediation"):
        if remediation["status"] == "closed":
            continue
        if _remediation_touches(remediation, equipment_id, alarm):
            blockers.append({"type": "open_remediation", "remediation_id": remediation["id"], "issue": remediation["data"].get("issue")})
    isolated_at = parse_ts(equipment["data"].get("isolated_at"))
    passed = []
    for inspection in _all(lookup, "inspection"):
        if inspection["data"].get("equipment_id") != equipment_id or inspection["status"] != "passed":
            continue
        passed_at = parse_ts(inspection["data"].get("passed_at"))
        if isolated_at is None or (passed_at and passed_at >= isolated_at):
            passed.append(inspection)
    if not passed:
        blockers.append({"type": "missing_reinspection", "equipment_id": equipment_id})
    return blockers


def recovery_blockers(alarm, equipment_by_id, lookup):
    scope = alarm["data"].get("scope_equipment_ids") or []
    blockers = {}
    for equipment_id in scope:
        equipment = equipment_by_id.get(equipment_id)
        if not equipment:
            blockers[equipment_id] = [{"type": "missing_equipment"}]
            continue
        unit_blockers = recovery_blockers_for(alarm, equipment, lookup)
        if unit_blockers:
            blockers[equipment_id] = unit_blockers
    return blockers


def queue_priority(job):
    """Sort key for the rescue queue: severe first, then earliest arrival."""
    severe = job["data"].get("severity") == SEVERE_ALARM
    arrived_at = job["data"].get("arrived_at")
    eta = job["data"].get("eta")
    when = arrived_at or eta or job["created_at"]
    parsed = parse_ts(when)
    return (0 if severe else 1, parsed or datetime.max, job["created_at"], job["id"])


def rescue_queue(lookup):
    jobs = [
        job for job in _all(lookup, "rescue_job")
        if job["status"] not in ("completed", "aborted")
    ]
    jobs.sort(key=queue_priority)
    return jobs


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit", "offline_records": "offline_record",
        "offline_record_versions": "offline_record_version",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked", "offline_record": "current",
        "offline_record_version": "applied",
    }
    TRANSITIONS = {
        "equipment": {
            "suspend": (("in_service",), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended",), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
            "recover": (("closed",), "recovered"),
        },
        "rescue_job": {
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("dispatched", "on_site"), "aborted"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("equipment_id", "work_type", "planned_at"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id", "dedupe_key", "team"),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
        "offline_record": (),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("alarm", "recover"): ("recovered_by",),
        ("permit", "revoke"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
        "offline_record": ("admin", "maintenance", "inspector"),
    }
    ROLE_ACTIONS = {
        ("equipment", "suspend"): ("admin", "inspector"),
        ("equipment", "out_of_service"): ("admin", "inspector"),
        ("equipment", "return_to_service"): ("admin", "inspector"),
        ("inspection", "pass"): ("admin", "inspector"),
        ("inspection", "fail"): ("admin", "inspector"),
        ("inspection", "reschedule"): ("admin", "inspector"),
        ("maintenance", "start"): ("admin", "maintenance"),
        ("maintenance", "complete"): ("admin", "maintenance", "dispatcher"),
        ("alarm", "dispatch"): ("admin", "dispatcher"),
        ("alarm", "mark_false"): ("admin", "dispatcher", "inspector"),
        ("alarm", "resolve"): ("admin", "dispatcher"),
        ("alarm", "close"): ("admin", "dispatcher"),
        ("alarm", "recover"): ("admin", "inspector", "dispatcher"),
        ("rescue_job", "arrive"): ("admin", "dispatcher"),
        ("rescue_job", "complete"): ("admin", "dispatcher"),
        ("rescue_job", "abort"): ("admin", "dispatcher"),
        ("remediation", "submit_evidence"): ("admin", "maintenance", "inspector"),
        ("remediation", "verify"): ("admin", "inspector"),
        ("remediation", "reject"): ("admin", "inspector"),
        ("remediation", "close"): ("admin", "inspector"),
        ("permit", "request_review"): ("admin", "inspector"),
        ("permit", "grant"): ("admin", "inspector"),
        ("permit", "revoke"): ("admin", "inspector"),
        ("permit", "expire"): ("admin", "inspector"),
    }
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
        "offline_record": lambda a, d, l: _validate_offline_record(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("permit", "grant"): _grant_permit,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _complete_rescue,
        ("alarm", "recover"): _complete_rescue,
        ("rescue_job", "arrive"): _arrive_rescue,
        ("rescue_job", "complete"): _complete_rescue_job,
        ("inspection", "pass"): _pass_inspection,
        ("maintenance", "start"): _guard_maintenance_transition,
        ("maintenance", "complete"): _guard_maintenance_transition,
        ("equipment", "return_to_service"): _guard_return_to_service,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), ("admin",))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
