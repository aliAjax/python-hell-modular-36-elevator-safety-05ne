import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, RecoveryBlocked
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class GroupIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("disp-1", "dispatcher")
        self.inspector = Actor("insp-1", "inspector")
        self.maintenance = Actor("maint-1", "maintenance")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no, group_id=None):
        payload = {"asset_no": asset_no, "equipment_type": "elevator", "location": "Tower A", "inspection_interval_days": 365}
        if group_id:
            payload["group_id"] = group_id
        return self.service.create(self.admin, "equipment", payload)

    def act(self, actor, entity, action, data=None, version=None):
        return self.service.transition(actor, entity["id"], action, data or {}, version)

    def test_normal_alarm_stops_only_alarming_unit(self):
        e1 = self.equipment("E-1", "G-1")
        e2 = self.equipment("E-2", "G-1")
        alarm = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "occurred_at": "2026-09-29T10:00:00Z"})
        self.assertEqual(self.service.get(e1["id"])["status"], "suspended")
        self.assertTrue(self.service.get(e1["id"])["data"]["isolated"])
        self.assertEqual(self.service.get(e2["id"])["status"], "in_service")
        self.assertNotIn("isolated", self.service.get(e2["id"])["data"])
        self.assertEqual(alarm["data"]["scope_kind"], "unit")
        self.assertEqual(alarm["data"]["scope_equipment_ids"], [e1["id"]])

    def test_severe_alarm_stops_whole_group_and_freezes_maintenance(self):
        e1 = self.equipment("E-1", "G-1")
        e2 = self.equipment("E-2", "G-1")
        e3 = self.equipment("E-3", "G-2")
        alarm = self.service.create(self.dispatcher, "alarm", {
            "equipment_id": e1["id"], "code": "TRAP", "severity": "severe", "occurred_at": "2026-09-29T10:00:00Z",
        })
        scope = alarm["data"]["scope_equipment_ids"]
        self.assertEqual(set(scope), {e1["id"], e2["id"]})
        self.assertEqual(self.service.get(e1["id"])["status"], "suspended")
        self.assertEqual(self.service.get(e2["id"])["status"], "suspended")
        self.assertEqual(self.service.get(e3["id"])["status"], "in_service")
        # A re-inspection is scheduled for every newly isolated unit.
        inspections = self.service.list("inspection")
        self.assertEqual({i["data"]["equipment_id"] for i in inspections}, {e1["id"], e2["id"]})
        self.assertTrue(all(i["data"].get("reinspection_for_alarm") == alarm["id"] for i in inspections))

        # Maintenance on isolated units is frozen for everyone.
        with self.assertRaises(ConflictError):
            self.service.create(self.maintenance, "maintenance", {"equipment_id": e2["id"], "work_type": "routine", "planned_at": "2026-10-01"})
        # Same-type equipment outside the scope can still be maintained.
        ok = self.service.create(self.maintenance, "maintenance", {"equipment_id": e3["id"], "work_type": "routine", "planned_at": "2026-10-01"})
        self.assertEqual(ok["status"], "planned")

        # Permits for isolated units cannot be granted while stopped.
        permit = self.service.create(self.inspector, "permit", {"equipment_id": e2["id"], "purpose": "return_to_service", "requested_by": "ops"})
        permit = self.act(self.inspector, permit, "request_review")
        with self.assertRaises(ConflictError):
            self.act(self.inspector, permit, "grant")

        # Equipment cannot return to service one at a time; group recovery required.
        with self.assertRaises(ConflictError):
            self.act(self.inspector, self.service.get(e1["id"]), "return_to_service")

    def test_pending_permit_is_cascade_blocked_by_severe_alarm(self):
        e1 = self.equipment("E-1", "G-1")
        e2 = self.equipment("E-2", "G-1")
        # Give e2 a passed inspection first, then put a permit into review.
        inspection = self.service.create(self.inspector, "inspection", {"equipment_id": e2["id"], "scheduled_at": "2026-09-01T09:00:00Z", "cycle_days": 365})
        self.act(self.inspector, inspection, "pass", {"findings": "normal"})
        permit = self.service.create(self.inspector, "permit", {"equipment_id": e2["id"], "purpose": "special_inspection", "requested_by": "ops"})
        permit = self.act(self.inspector, permit, "request_review")
        self.assertEqual(permit["status"], "pending_review")

        self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "severity": "severe", "occurred_at": "2026-09-29T10:00:00Z"})
        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "blocked")
        self.assertIn("isolation:", permit["data"]["blocked_reason"])

    def _rescue_to_closed(self, actor, alarm, outcome="freed"):
        alarm = self.act(actor, self.service.get(alarm["id"]), "dispatch", {"team": "Alpha"})
        job = self.service.create(actor, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "job-" + alarm["id"][:8], "team": "Alpha", "eta": "2026-09-29T10:30:00Z"})
        job = self.act(actor, self.service.get(job["id"]), "arrive", {"arrived_at": "2026-09-29T10:20:00Z"})
        job = self.act(actor, self.service.get(job["id"]), "complete", {"outcome": outcome, "completed_at": "2026-09-29T10:25:00Z"})
        alarm = self.act(actor, self.service.get(alarm["id"]), "resolve", {"resolution": "safe"})
        alarm = self.act(actor, self.service.get(alarm["id"]), "close")
        return alarm

    def test_group_recovery_requires_reinspection_and_reports_blockers(self):
        e1 = self.equipment("E-1", "G-1")
        e2 = self.equipment("E-2", "G-1")
        alarm = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "severity": "severe", "occurred_at": "2026-09-29T10:00:00Z"})
        alarm = self._rescue_to_closed(self.dispatcher, alarm)

        # First attempt: re-inspections are still scheduled -> blocked for both units.
        with self.assertRaises(RecoveryBlocked) as caught:
            self.act(self.inspector, self.service.get(alarm["id"]), "recover", {"recovered_by": "insp-1"})
        blocker_types = {eid: [b["type"] for b in items] for eid, items in caught.exception.blockers.items()}
        self.assertEqual(set(blocker_types), {e1["id"], e2["id"]})
        self.assertIn("missing_reinspection", blocker_types[e1["id"]])
        self.assertIn("missing_reinspection", blocker_types[e2["id"]])

        # An open remediation also blocks recovery.
        remediation = self.service.create(self.inspector, "remediation", {"equipment_id": e1["id"], "issue": "brake wear", "owner": "Maint", "due_at": "2026-10-10"})
        with self.assertRaises(RecoveryBlocked) as caught:
            self.act(self.inspector, self.service.get(alarm["id"]), "recover", {"recovered_by": "insp-1"})
        self.assertIn("open_remediation", [b["type"] for b in caught.exception.blockers[e1["id"]]])
        remediation = self.service.get(remediation["id"])
        remediation = self.act(self.maintenance, remediation, "submit_evidence", {"evidence": "IMG-9"})
        remediation = self.act(self.inspector, remediation, "verify")
        self.act(self.inspector, remediation, "close")

        # Pass the auto-scheduled re-inspections (timestamps after isolation).
        for inspection in self.service.list("inspection"):
            self.act(self.inspector, self.service.get(inspection["id"]), "pass", {"findings": "ok", "passed_at": "2026-09-29T15:00:00Z"})

        alarm = self.act(self.inspector, self.service.get(alarm["id"]), "recover", {"recovered_by": "insp-1"})
        self.assertEqual(alarm["status"], "recovered")
        self.assertEqual(self.service.get(e1["id"])["status"], "in_service")
        self.assertEqual(self.service.get(e2["id"])["status"], "in_service")
        self.assertNotIn("isolated", self.service.get(e1["id"])["data"])
        self.assertNotIn("isolated", self.service.get(e2["id"])["data"])
        snapshot = alarm["data"]["last_recovery"]
        self.assertEqual(snapshot["recovered_by"], "insp-1")
        self.assertEqual({u["equipment_id"] for u in snapshot["units"]}, {e1["id"], e2["id"]})
        for unit in snapshot["units"]:
            self.assertEqual(unit["status_before"], "suspended")
            self.assertEqual(unit["status_after"], "in_service")
            self.assertTrue(any(i["status"] == "passed" for i in unit["inspections"]))

    def test_recovery_refused_when_another_alarm_stays_active(self):
        e1 = self.equipment("E-1", "G-1")
        severe = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "severity": "severe", "occurred_at": "2026-09-29T10:00:00Z"})
        second = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "FIRE", "occurred_at": "2026-09-29T11:00:00Z"})
        severe = self._rescue_to_closed(self.dispatcher, severe)
        for inspection in self.service.list("inspection"):
            self.act(self.inspector, self.service.get(inspection["id"]), "pass", {"findings": "ok", "passed_at": "2026-09-29T15:00:00Z"})
        with self.assertRaises(RecoveryBlocked) as caught:
            self.act(self.inspector, self.service.get(severe["id"]), "recover", {"recovered_by": "insp-1"})
        active = [b for b in caught.exception.blockers[e1["id"]] if b["type"] == "active_alarm"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["alarm_id"], second["id"])


class RescueQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("disp-1", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no, group_id=None):
        payload = {"asset_no": asset_no, "equipment_type": "elevator", "location": "L", "inspection_interval_days": 365}
        if group_id:
            payload["group_id"] = group_id
        return self.service.create(self.admin, "equipment", payload)

    def act(self, entity, action, data=None):
        return self.service.transition(self.dispatcher, entity["id"], action, data or {})

    def test_queue_orders_by_arrival_with_severe_jumping_ahead(self):
        e1, e2, e3 = self.equipment("E-1"), self.equipment("E-2"), self.equipment("E-3")
        normal1 = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "A", "occurred_at": "2026-09-29T09:00:00Z"})
        normal2 = self.service.create(self.dispatcher, "alarm", {"equipment_id": e2["id"], "code": "B", "occurred_at": "2026-09-29T09:05:00Z"})
        severe = self.service.create(self.dispatcher, "alarm", {"equipment_id": e3["id"], "code": "C", "severity": "severe", "occurred_at": "2026-09-29T09:10:00Z"})

        def job(alarm, key, eta=None, severity=None):
            payload = {"alarm_id": alarm["id"], "dedupe_key": key, "team": "T"}
            if eta:
                payload["eta"] = eta
            if severity:
                payload["severity"] = severity
            created = self.service.create(self.dispatcher, "rescue_job", payload)
            if severity is None:
                self.assertEqual(created["data"]["severity"], alarm["data"]["severity"])
            return created

        j1 = job(normal1, "k1", "2026-09-29T10:05:00Z")
        j2 = job(normal2, "k2", "2026-09-29T10:00:00Z")
        j3 = job(severe, "k3", "2026-09-29T11:00:00Z", severity="severe")

        # Before arrival: severe jumps first, then normal jobs by earliest eta.
        queue = self.service.rescue_queue()
        self.assertEqual([j["id"] for j in queue], [j3["id"], j2["id"], j1["id"]])

        # j1 arrives; severe still leads, then actual arrival time orders the rest.
        self.act(self.service.get(j1["id"]), "arrive", {"arrived_at": "2026-09-29T09:50:00Z"})
        queue = self.service.rescue_queue()
        self.assertEqual([j["id"] for j in queue], [j3["id"], j1["id"], j2["id"]])

        # Completed and aborted jobs leave the queue.
        self.act(self.service.get(j3["id"]), "arrive", {"arrived_at": "2026-09-29T09:55:00Z"})
        self.act(self.service.get(j3["id"]), "complete", {"outcome": "freed", "completed_at": "2026-09-29T09:58:00Z"})
        self.act(self.service.get(j2["id"]), "abort", {"reason": "duplicate"})
        queue = self.service.rescue_queue()
        self.assertEqual([j["id"] for j in queue], [j1["id"]])

    def test_aborted_or_completed_old_job_cannot_close_a_new_alarm(self):
        e1 = self.equipment("E-1")
        first = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "occurred_at": "2026-09-29T09:00:00Z"})
        # A job that is dispatched and then aborted must not be enough to close.
        self.act(first, "dispatch", {"team": "T"})
        aborted = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": first["id"], "dedupe_key": "abort-1", "team": "T"})
        self.act(aborted, "abort", {"reason": "sent elsewhere"})
        first = self.service.get(first["id"])
        first = self.service.transition(self.dispatcher, first["id"], "resolve", {"resolution": "x"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.dispatcher, first["id"], "close", {})

        # A proper on-site rescue closes the first alarm.
        good = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": first["id"], "dedupe_key": "good-1", "team": "T"})
        self.act(good, "arrive", {"arrived_at": "2026-09-29T09:40:00Z"})
        self.act(good, "complete", {"outcome": "freed", "completed_at": "2026-09-29T09:45:00Z"})
        first = self.service.transition(self.dispatcher, first["id"], "close", {})
        self.assertEqual(first["status"], "closed")

        # A new alarm on the same unit cannot be closed using the old job:
        # an abort-only job for the new alarm is rejected at close time.
        second = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "occurred_at": "2026-09-29T12:00:00Z"})
        self.act(second, "dispatch", {"team": "T"})
        abort2 = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": second["id"], "dedupe_key": "abort-2", "team": "T"})
        self.act(abort2, "abort", {"reason": "false activation"})
        second = self.service.get(second["id"])
        second = self.service.transition(self.dispatcher, second["id"], "resolve", {"resolution": "x"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.dispatcher, second["id"], "close", {})

    def test_cannot_create_rescue_job_for_closed_alarm(self):
        e1 = self.equipment("E-1")
        alarm = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "occurred_at": "2026-09-29T09:00:00Z"})
        self.act(alarm, "dispatch", {"team": "T"})
        job = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j", "team": "T"})
        self.act(job, "arrive")
        self.act(job, "complete", {"outcome": "freed"})
        alarm = self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "x"})
        self.service.transition(self.dispatcher, alarm["id"], "close", {})
        with self.assertRaises(Exception):
            self.service.create(self.dispatcher, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j2", "team": "T"})


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.maintenance = Actor("maint-1", "maintenance")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(self.admin, "equipment", {"asset_no": asset_no, "equipment_type": "elevator", "location": "Old", "inspection_interval_days": 365})

    def record(self, source, record_id, version, equipment_id=None, **extra):
        payload = {"source_id": source, "record_id": record_id, "version": version, **extra}
        if equipment_id:
            payload["equipment_id"] = equipment_id
        return payload

    def test_versions_merge_and_old_versions_remain_queryable(self):
        e1 = self.equipment()
        first = self.service.merge_offline(self.maintenance, [self.record("tab-1", "r-1", 1, e1["id"], location="Lobby", field_note="v1")])
        self.assertEqual(first["items"][0]["outcome"], "created")
        self.assertEqual(self.service.get(e1["id"])["data"]["location"], "Lobby")
        self.assertEqual(self.service.get(e1["id"])["data"]["last_offline_version"], 1)

        second = self.service.merge_offline(self.maintenance, [self.record("tab-1", "r-1", 2, e1["id"], location="Lobby-2", field_note="v2")])
        self.assertEqual(second["items"][0]["outcome"], "updated")
        self.assertEqual(self.service.get(e1["id"])["data"]["location"], "Lobby-2")
        self.assertEqual(self.service.get(e1["id"])["data"]["last_offline_version"], 2)

        # A late old version is a no-op duplicate and does not overwrite equipment.
        old = self.service.merge_offline(self.maintenance, [self.record("tab-1", "r-1", 1, e1["id"], location="STALE", field_note="old")])
        self.assertEqual(old["items"][0]["outcome"], "duplicate")
        # A lower version never seen before (v1 arriving after v2) is kept as ignored_old.
        self.service.merge_offline(self.maintenance, [self.record("tab-1", "r-2", 2, e1["id"], location="Lobby-2", field_note="v2")])
        older = self.service.merge_offline(self.maintenance, [self.record("tab-1", "r-2", 1, e1["id"], location="STALE", field_note="old")])
        self.assertEqual(older["items"][0]["outcome"], "ignored_old")
        self.assertEqual(self.service.get(e1["id"])["data"]["location"], "Lobby-2")
        self.assertEqual(self.service.get(e1["id"])["data"]["last_offline_version"], 2)

        versions = self.service.offline_versions("offline-" + __import__("hashlib").sha256(b"tab-1\0r-1").hexdigest()[:32])["items"]
        self.assertEqual([v["data"]["version"] for v in versions], [1, 2])
        self.assertEqual([v["status"] for v in versions], ["superseded", "applied"])

    def test_request_id_makes_merge_idempotent_across_replays(self):
        e1 = self.equipment()
        records = [self.record("tab-1", "r-9", 1, e1["id"], location="Once", field_note="x")]
        first = self.service.merge_offline(self.maintenance, records, request_id="req-42")
        self.assertFalse(first["replayed"])
        location_version = self.service.get(e1["id"])["version"]

        # Replaying the same request after a "restart" returns stored output and
        # does not modify the equipment a second time.
        replay = self.service.merge_offline(self.maintenance, records, request_id="req-42")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.service.get(e1["id"])["version"], location_version)
        self.assertEqual(self.service.get(e1["id"])["data"]["location"], "Once")

        # A different request with the same records still applies exactly once
        # (record version duplicate, no equipment patch).
        again = self.service.merge_offline(self.maintenance, records, request_id="req-43")
        self.assertEqual(again["items"][0]["outcome"], "duplicate")
        self.assertEqual(self.service.get(e1["id"])["version"], location_version)


class IsolationOverviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("disp-1", "dispatcher")
        self.inspector = Actor("insp-1", "inspector")

    def tearDown(self):
        self.tmp.cleanup()

    def act(self, actor, entity, action, data=None):
        return self.service.transition(actor, entity["id"], action, data or {})

    def test_overview_lists_scope_and_live_blockers(self):
        e1 = self.service.create(self.admin, "equipment", {"asset_no": "E-1", "equipment_type": "elevator", "location": "T", "inspection_interval_days": 365, "group_id": "G"})
        e2 = self.service.create(self.admin, "equipment", {"asset_no": "E-2", "equipment_type": "elevator", "location": "T", "inspection_interval_days": 365, "group_id": "G"})
        alarm = self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "TRAP", "severity": "severe", "occurred_at": "2026-09-29T10:00:00Z"})

        overview = self.service.isolation_overview()["items"]
        self.assertEqual(len(overview), 1)
        entry = overview[0]
        self.assertEqual(entry["alarm_id"], alarm["id"])
        self.assertEqual(set(entry["scope_equipment_ids"]), {e1["id"], e2["id"]})
        for unit in entry["units"]:
            self.assertTrue(any(b["type"] == "missing_reinspection" for b in unit["blockers"]))

        # After the re-inspection passes the blocker disappears from the overview.
        inspections = {i["data"]["equipment_id"]: i for i in self.service.list("inspection")}
        for equipment_id in (e1["id"], e2["id"]):
            self.act(self.inspector, inspections[equipment_id], "pass", {"findings": "ok", "passed_at": "2026-09-29T16:00:00Z"})
        overview = self.service.isolation_overview()["items"]
        for unit in overview[0]["units"]:
            self.assertEqual(unit["blockers"], [])


if __name__ == "__main__":
    unittest.main()
