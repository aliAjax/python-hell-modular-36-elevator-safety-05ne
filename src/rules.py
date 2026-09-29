from datetime import datetime

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


SEVERITY_LEVELS = ("normal", "severe")
MAINTENANCE_OPEN_STATUSES = ("planned", "in_progress", "paused", "on_hold")
ALARM_OPEN_STATUSES = ("received", "dispatched", "resolved")
TERMINAL_STATUSES = ("closed", "false_alarm", "out_of_service", "completed", "aborted")


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


def _equipment(lookup, equipment_id):
    return _find_one(lookup, "equipment", "id", equipment_id)


def _open_alarms_for(lookup, equipment_id):
    return [
        alarm
        for alarm in _all(lookup, "alarm")
        if alarm["data"].get("equipment_id") == equipment_id and alarm["status"] in ALARM_OPEN_STATUSES
    ]


def _open_remediations_for(lookup, equipment_id, alarm_id=None):
    linked = []
    for item in _all(lookup, "remediation"):
        if item["status"] == "closed":
            continue
        target_alarm = item["data"].get("alarm_id")
        if item["data"].get("equipment_id") == equipment_id or (alarm_id and target_alarm == alarm_id):
            linked.append(item)
    return linked


def _passed_reinspection(lookup, equipment_id, alarm_id):
    for inspection in _all(lookup, "inspection"):
        data = inspection["data"]
        if (
            data.get("equipment_id") == equipment_id
            and data.get("trigger_alarm_id") == alarm_id
            and inspection["status"] == "passed"
        ):
            return True
    return False


def recovery_blockers(lookup, equipment, isolation_alarm_id=None):
    """Per-unit checks required before a stopped unit may run again.

    Order of blockers is fixed so the recovery page shows a stable list:
    alarm -> remediation -> re-inspection. The alarm that opened the group
    isolation stops every unit in the scope, even units other than the one
    that reported it.
    """
    blockers = []
    open_alarms = _open_alarms_for(lookup, equipment["id"])
    isolation_alarm = (
        _find_one(lookup, "alarm", "id", isolation_alarm_id) if isolation_alarm_id else None
    )
    if isolation_alarm and isolation_alarm["status"] in ALARM_OPEN_STATUSES:
        if not any(item["id"] == isolation_alarm["id"] for item in open_alarms):
            open_alarms.append(isolation_alarm)
    for alarm in open_alarms:
        blockers.append(
            {
                "type": "active_alarm",
                "entity_id": alarm["id"],
                "status": alarm["status"],
                "message": "active alarm %s is not closed" % alarm["id"],
            }
        )
    for remediation in _open_remediations_for(lookup, equipment["id"], isolation_alarm_id):
        blockers.append(
            {
                "type": "open_remediation",
                "entity_id": remediation["id"],
                "status": remediation["status"],
                "message": "remediation %s is not closed" % remediation["id"],
            }
        )
    if isolation_alarm_id and not _passed_reinspection(lookup, equipment["id"], isolation_alarm_id):
        blockers.append(
            {
                "type": "reinspection_required",
                "alarm_id": isolation_alarm_id,
                "message": "post-incident re-inspection has not passed",
            }
        )
    return blockers


def _isolation_snapshot(lookup, equipment, alarm_id):
    alarms = _all(lookup, "alarm")
    remediations = _all(lookup, "remediation")
    inspections = _all(lookup, "inspection")
    jobs = _all(lookup, "rescue_job")
    permits = _all(lookup, "permit")
    maintenances = _all(lookup, "maintenance")
    return {
        "equipment_id": equipment["id"],
        "alarm_id": alarm_id,
        "alarms": [
            {"id": item["id"], "status": item["status"], "code": item["data"].get("code")}
            for item in alarms
            if item["data"].get("equipment_id") == equipment["id"]
        ],
        "remediations": [
            {"id": item["id"], "status": item["status"], "issue": item["data"].get("issue")}
            for item in remediations
            if item["data"].get("equipment_id") == equipment["id"]
            or item["data"].get("alarm_id") == alarm_id
        ],
        "inspections": [
            {"id": item["id"], "status": item["status"]}
            for item in inspections
            if item["data"].get("equipment_id") == equipment["id"]
            and item["data"].get("trigger_alarm_id") == alarm_id
        ],
        "rescue_jobs": [
            {"id": item["id"], "status": item["status"], "round": item["data"].get("round")}
            for item in jobs
            if item["data"].get("alarm_id") == alarm_id
        ],
        "permits": [
            {"id": item["id"], "status": item["status"], "purpose": item["data"].get("purpose")}
            for item in permits
            if item["data"].get("equipment_id") == equipment["id"]
        ],
        "maintenances": [
            {"id": item["id"], "status": item["status"], "work_type": item["data"].get("work_type")}
            for item in maintenances
            if item["data"].get("equipment_id") == equipment["id"]
        ],
    }


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")
    group_id = data.get("group_id")
    if group_id is not None and not str(group_id).strip():
        raise ValidationError("group_id cannot be blank")


def _validate_inspection(data, lookup):
    equipment = _equipment(lookup, data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    equipment = _equipment(lookup, data.get("equipment_id"))
    if not equipment:
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")
    # Isolation stops the whole group: maintenance on a stopped unit is held
    # until the unit is returned individually.
    if equipment["status"] == "suspended" and equipment["data"].get("isolated_by"):
        raise ConflictError("maintenance blocked while equipment is isolated")


def _validate_alarm(data, lookup):
    if not _equipment(lookup, data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    severity = data.get("severity", "normal")
    if severity not in SEVERITY_LEVELS:
        raise ValidationError("invalid severity")
    data["severity"] = severity
    data["requires_rescue"] = bool(data.get("requires_rescue", data.get("code", "").startswith("ENTRAPMENT")))
    data["round"] = 0
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] in ("closed", "false_alarm"):
        raise ValidationError("rescue_job requires an active alarm")
    data["severity"] = alarm["data"].get("severity", "normal")
    current_round = alarm["data"].get("round", 0)
    data["round"] = current_round
    if data.get("eta"):
        try:
            datetime.fromisoformat(str(data["eta"]).replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError("eta must be ISO-8601")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    if data.get("equipment_id") and not _equipment(lookup, data.get("equipment_id")):
        raise ValidationError("remediation requires equipment")
    if data.get("alarm_id") and not _find_one(lookup, "alarm", "id", data.get("alarm_id")):
        raise ValidationError("remediation alarm_id does not exist")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _equipment(lookup, data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _grant_permit(actor, entity, data, lookup):
    equipment = _equipment(lookup, entity["data"].get("equipment_id"))
    if not equipment or equipment["status"] not in ("in_service", "suspended"):
        raise ConflictError("permit can only be granted for a serviceable equipment")
    isolation_alarm_id = equipment["data"].get("isolated_by")
    if isolation_alarm_id:
        # A stopped unit is released one at a time and only after every check.
        if entity["data"].get("purpose") != "return_to_service":
            raise ConflictError("isolated equipment only accepts return_to_service permits")
        blockers = recovery_blockers(lookup, equipment, isolation_alarm_id)
        if blockers:
            raise ConflictError("permit blocked: " + "; ".join(item["message"] for item in blockers))
    else:
        if _open_alarms_for(lookup, equipment["id"]):
            raise ConflictError("permit blocked by active alarm")
        inspections = [
            i for i in _all(lookup, "inspection")
            if i["data"].get("equipment_id") == equipment["id"] and i["status"] == "passed"
        ]
        if not inspections:
            raise ConflictError("permit requires a passed inspection")
        if _open_remediations_for(lookup, equipment["id"]):
            raise ConflictError("permit blocked by open remediation")
    return {"granted_by": actor.user_id, "granted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _dispatch_alarm(actor, entity, data, lookup):
    # Each dispatch opens a new rescue round; jobs from withdrawn or finished
    # rounds never block closure of a freshly escalated alarm.
    return {"round": int(entity["data"].get("round", 0)) + 1}


def _arrive_rescue(actor, entity, data, lookup):
    patch = {"arrived_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    if data.get("eta"):
        patch["eta"] = data["eta"]
    return patch


def _complete_rescue(actor, entity, data, lookup):
    current_round = entity["data"].get("round", 0)
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    current = [j for j in jobs if j["data"].get("round", 0) == current_round]
    if not current or any(job["status"] not in ("completed", "aborted") for job in current):
        raise ConflictError("alarm cannot close before rescue jobs of the active round are complete")
    return {"resolved_by": actor.user_id}


def _start_maintenance(actor, entity, data, lookup):
    equipment = _equipment(lookup, entity["data"].get("equipment_id"))
    if equipment and equipment["status"] == "suspended" and equipment["data"].get("isolated_by"):
        raise ConflictError("maintenance cannot start while equipment is isolated")
    return {}


def _resume_maintenance(actor, entity, data, lookup):
    equipment = _equipment(lookup, entity["data"].get("equipment_id"))
    if equipment and equipment["status"] == "suspended" and equipment["data"].get("isolated_by"):
        raise ConflictError("maintenance stays held until the unit is returned")
    return {}


def _return_equipment(actor, entity, data, lookup):
    isolation_alarm_id = entity["data"].get("isolated_by")
    blockers = recovery_blockers(lookup, entity, isolation_alarm_id)
    if blockers:
        raise ConflictError("return to service blocked: " + "; ".join(item["message"] for item in blockers))
    patch = {
        "recovered_by": actor.user_id,
        "recovered_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "recovery_snapshot": _isolation_snapshot(lookup, entity, isolation_alarm_id),
        "isolated_by": None,
        "isolation_scope": None,
    }
    permit = None
    if isolation_alarm_id:
        for item in _all(lookup, "permit"):
            permit_data = item["data"]
            if (
                permit_data.get("equipment_id") == entity["id"]
                and item["status"] == "granted"
                and permit_data.get("purpose") == "return_to_service"
            ):
                permit = item
                break
    if permit:
        patch["recovery_snapshot"]["permit_id"] = permit["id"]
    return patch


def _validate_offline_record(data, lookup):
    if data.get("equipment_id") and not _equipment(lookup, data.get("equipment_id")):
        raise ValidationError("offline record references unknown equipment")
    if "readings" in data and data["readings"] is not None and not isinstance(data["readings"], dict):
        raise ValidationError("readings must be an object")
    version = data.get("version", 1)
    try:
        number = int(version)
    except (TypeError, ValueError):
        raise ValidationError("version must be an integer")
    if number < 1:
        raise ValidationError("version must be >= 1")
    data["version"] = number


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit", "offline_records": "offline_record", "offline-records": "offline_record",
        "isolations": "isolation",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked", "offline_record": "merged", "isolation": "active",
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
            "pause": (("in_progress",), "paused"),
            "resume": (("paused",), "in_progress"),
            "hold": (("planned",), "on_hold"),
            "release_hold": (("on_hold",), "planned"),
        },
        "alarm": {
            "dispatch": (("received", "resolved"), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
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
        "isolation": {
            "release": (("active",), "released"),
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
        "offline_record": ("source_id", "record_id"),
        "isolation": ("alarm_id", "scope"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
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
        "offline_record": ("admin", "inspector", "maintenance", "dispatcher"),
        "isolation": ("*",),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector"),
        "fail": ("admin", "inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "pause": ("admin", "maintenance", "dispatcher", "inspector"),
        "resume": ("admin", "maintenance", "dispatcher", "inspector"),
        "hold": ("admin", "maintenance", "dispatcher", "inspector"),
        "release_hold": ("admin", "maintenance", "dispatcher", "inspector"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector"),
        "arrive": ("admin", "dispatcher"),
        "abort": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector", "dispatcher"),
        "expire": ("admin", "inspector"),
        "release": ("*",),
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
        ("alarm", "dispatch"): _dispatch_alarm,
        ("alarm", "close"): _complete_rescue,
        ("rescue_job", "arrive"): _arrive_rescue,
        ("maintenance", "start"): _start_maintenance,
        ("maintenance", "resume"): _resume_maintenance,
        ("equipment", "return_to_service"): _return_equipment,
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
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def recovery_blockers(self, lookup, equipment, isolation_alarm_id=None):
        return recovery_blockers(lookup, equipment, isolation_alarm_id)
