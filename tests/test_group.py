import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class GroupIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("disp1", "dispatcher")
        self.maintenance = Actor("maint1", "maintenance")

    def tearDown(self):
        self.tmp.cleanup()

    def make_equipment(self, asset_no, group_id="G-1"):
        return self.service.create(
            self.admin,
            "equipment",
            {
                "asset_no": asset_no,
                "equipment_type": "elevator",
                "location": "Tower A",
                "inspection_interval_days": 365,
                "group_id": group_id,
            },
        )

    def severe_alarm(self, equipment, code="ENTRAPMENT-1"):
        return self.service.create(
            self.dispatcher,
            "alarm",
            {
                "equipment_id": equipment["id"],
                "code": code,
                "occurred_at": "2026-09-29T10:00:00Z",
                "severity": "severe",
            },
        )

    def test_severe_alarm_stops_whole_group_and_links_permits_maintenance_reinspection(self):
        e1 = self.make_equipment("E-1")
        e2 = self.make_equipment("E-2")
        e3 = self.make_equipment("E-3", group_id="G-2")

        # Pre-existing permit on E2 and maintenance orders on E1/E2.
        insp = self.service.create(self.admin, "inspection", {"equipment_id": e2["id"], "scheduled_at": "2026-09-01T09:00:00Z", "cycle_days": 365})
        self.service.transition(self.admin, insp["id"], "pass", {"findings": "ok"})
        permit = self.service.create(self.admin, "permit", {"equipment_id": e2["id"], "purpose": "temporary_operation", "requested_by": "ops"})
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        permit = self.service.transition(self.admin, permit["id"], "grant", {})

        w1 = self.service.create(self.maintenance, "maintenance", {"equipment_id": e1["id"], "work_type": "routine", "planned_at": "2026-10-01"})
        w2 = self.service.create(self.maintenance, "maintenance", {"equipment_id": e2["id"], "work_type": "repair", "planned_at": "2026-10-01"})
        w2 = self.service.transition(self.maintenance, w2["id"], "start", {})

        alarm = self.severe_alarm(e1)

        for unit_id in (e1["id"], e2["id"]):
            unit = self.service.get(unit_id)
            self.assertEqual(unit["status"], "suspended")
            self.assertEqual(unit["data"]["isolated_by"], alarm["id"])
            self.assertIn(e1["id"], unit["data"]["isolation_scope"])
            self.assertIn(e2["id"], unit["data"]["isolation_scope"])
        # Other groups keep running and can still be maintained.
        self.assertEqual(self.service.get(e3["id"])["status"], "in_service")
        self.service.create(self.maintenance, "maintenance", {"equipment_id": e3["id"], "work_type": "routine", "planned_at": "2026-10-01"})

        # Permit revoked, maintenance held together with the stop.
        self.assertEqual(self.service.get(permit["id"])["status"], "revoked")
        self.assertEqual(self.service.get(w1["id"])["status"], "on_hold")
        self.assertEqual(self.service.get(w2["id"])["status"], "paused")
        with self.assertRaises(ConflictError):
            self.service.create(self.maintenance, "maintenance", {"equipment_id": e1["id"], "work_type": "routine", "planned_at": "2026-10-02"})

        # One reinspection per stopped unit, tied to the alarm.
        reinspections = [i for i in self.service.list("inspection") if i["data"].get("trigger_alarm_id") == alarm["id"]]
        self.assertEqual(sorted(i["data"]["equipment_id"] for i in reinspections), sorted([e1["id"], e2["id"]]))

        isolation = self.service.get("isolation-" + alarm["id"])
        self.assertEqual(isolation["status"], "active")
        self.assertEqual(sorted(isolation["data"]["scope"]), sorted([e1["id"], e2["id"]]))

    def test_normal_alarm_only_stops_nothing_and_other_units_stay_maintainable(self):
        e1 = self.make_equipment("E-1")
        e2 = self.make_equipment("E-2")
        alarm = self.service.create(
            self.dispatcher,
            "alarm",
            {"equipment_id": e1["id"], "code": "DOOR", "occurred_at": "2026-09-29T10:00:00Z", "severity": "normal"},
        )
        self.assertEqual(alarm["data"]["severity"], "normal")
        self.assertEqual(self.service.get(e1["id"])["status"], "in_service")
        self.assertEqual(self.service.get(e2["id"])["status"], "in_service")
        self.service.create(self.maintenance, "maintenance", {"equipment_id": e2["id"], "work_type": "routine", "planned_at": "2026-10-01"})
        self.assertIsNone(self.service.repository.get_entity("isolation-" + alarm["id"]))

    def test_recovery_is_unit_by_unit_and_keeps_snapshot(self):
        e1 = self.make_equipment("E-1")
        e2 = self.make_equipment("E-2")
        alarm = self.severe_alarm(e1)

        # Rescue + close the alarm first.
        alarm = self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "Alpha"})
        job = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j1", "team": "Alpha"})
        self.service.transition(self.dispatcher, job["id"], "arrive", {})
        self.service.transition(self.dispatcher, job["id"], "complete", {"outcome": "freed"})
        alarm = self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "safe"})
        alarm = self.service.transition(self.dispatcher, alarm["id"], "close", {})

        # Remediation on E1 must close before E1 returns; E2 is unaffected.
        rem = self.service.create(self.admin, "remediation", {"equipment_id": e1["id"], "alarm_id": alarm["id"], "issue": "brake wear", "owner": "team", "due_at": "2026-10-05"})
        self.service.transition(self.admin, rem["id"], "submit_evidence", {"evidence": "PIC-1"})
        self.service.transition(self.admin, rem["id"], "verify", {})
        self.service.transition(self.admin, rem["id"], "close", {})

        reinspections = {i["data"]["equipment_id"]: i for i in self.service.list("inspection") if i["data"].get("trigger_alarm_id") == alarm["id"]}
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, e1["id"], "return_to_service", {})
        for inspection in reinspections.values():
            self.service.transition(self.admin, inspection["id"], "pass", {"findings": "fixed"})

        # Permit gate: isolated units only accept return_to_service permits.
        special = self.service.create(self.admin, "permit", {"equipment_id": e1["id"], "purpose": "temporary_operation", "requested_by": "ops"})
        special = self.service.transition(self.admin, special["id"], "request_review", {})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, special["id"], "grant", {})

        permit = self.service.create(self.admin, "permit", {"equipment_id": e1["id"], "purpose": "return_to_service", "requested_by": "ops"})
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        permit = self.service.transition(self.admin, permit["id"], "grant", {})

        returned = self.service.transition(self.admin, e1["id"], "return_to_service", {})
        self.assertEqual(returned["status"], "in_service")
        self.assertIsNone(returned["data"]["isolated_by"])
        snapshot = returned["data"]["recovery_snapshot"]
        self.assertEqual(snapshot["alarm_id"], alarm["id"])
        self.assertEqual(snapshot["permit_id"], permit["id"])
        self.assertTrue(any(a["status"] == "closed" for a in snapshot["alarms"]))
        self.assertTrue(any(i["status"] == "passed" for i in snapshot["inspections"]))

        # E2 remains isolated until its own checks pass.
        self.assertEqual(self.service.get(e2["id"])["status"], "suspended")
        isolation = self.service.get("isolation-" + alarm["id"])
        self.assertEqual(isolation["status"], "active")
        self.assertEqual(isolation["data"]["remaining"], [e2["id"]])

        permit2 = self.service.create(self.admin, "permit", {"equipment_id": e2["id"], "purpose": "return_to_service", "requested_by": "ops"})
        permit2 = self.service.transition(self.admin, permit2["id"], "request_review", {})
        permit2 = self.service.transition(self.admin, permit2["id"], "grant", {})
        self.service.transition(self.admin, e2["id"], "return_to_service", {})
        self.assertEqual(self.service.get("isolation-" + alarm["id"])["status"], "released")

    def _close_alarm_and_clear_unit(self, alarm, unit_id, issue=None):
        """Drive rescue/alarm/remediation/reinspection/return for one unit."""
        alarm = self.service.get(alarm["id"])
        if alarm["status"] in ("received", "resolved"):
            alarm = self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "T"})
        if alarm["status"] == "dispatched":
            job = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "k-" + unit_id, "team": "T"})
            self.service.transition(self.dispatcher, job["id"], "arrive", {})
            self.service.transition(self.dispatcher, job["id"], "complete", {"outcome": "freed"})
            alarm = self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "safe"})
            alarm = self.service.transition(self.dispatcher, alarm["id"], "close", {})
        if issue:
            rem = self.service.create(self.admin, "remediation", {"equipment_id": unit_id, "alarm_id": alarm["id"], "issue": issue, "owner": "team", "due_at": "2026-10-05"})
            self.service.transition(self.admin, rem["id"], "submit_evidence", {"evidence": "PIC"})
            self.service.transition(self.admin, rem["id"], "verify", {})
            self.service.transition(self.admin, rem["id"], "close", {})
        inspection = [
            i for i in self.service.list("inspection")
            if i["data"].get("trigger_alarm_id") == alarm["id"] and i["data"]["equipment_id"] == unit_id
        ][0]
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})
        permit = self.service.create(self.admin, "permit", {"equipment_id": unit_id, "purpose": "return_to_service", "requested_by": "ops"})
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        permit = self.service.transition(self.admin, permit["id"], "grant", {})
        return self.service.transition(self.admin, unit_id, "return_to_service", {})

    def test_held_maintenance_resumes_only_when_each_unit_returns(self):
        e1 = self.make_equipment("E-1")
        e2 = self.make_equipment("E-2")
        planned = self.service.create(self.maintenance, "maintenance", {"equipment_id": e1["id"], "work_type": "routine", "planned_at": "2026-10-01"})
        active = self.service.create(self.maintenance, "maintenance", {"equipment_id": e2["id"], "work_type": "repair", "planned_at": "2026-10-01"})
        active = self.service.transition(self.maintenance, active["id"], "start", {})
        alarm = self.severe_alarm(e1)
        self.assertEqual(self.service.get(planned["id"])["status"], "on_hold")
        self.assertEqual(self.service.get(active["id"])["status"], "paused")

        self._close_alarm_and_clear_unit(alarm, e1["id"], issue="door")
        self.assertEqual(self.service.get(planned["id"])["status"], "planned")
        # E2 still isolated: its in-progress work stays paused.
        self.assertEqual(self.service.get(active["id"])["status"], "paused")
        self._close_alarm_and_clear_unit(alarm, e2["id"])
        self.assertEqual(self.service.get(active["id"])["status"], "in_progress")

    def test_withdrawn_or_completed_jobs_of_old_round_do_not_close_new_alarm(self):
        e1 = self.make_equipment("E-1")
        alarm = self.severe_alarm(e1)
        alarm = self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "A"})
        self.assertEqual(alarm["data"]["round"], 1)
        job = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "r1", "team": "A"})
        self.assertEqual(job["data"]["round"], 1)
        self.service.transition(self.dispatcher, job["id"], "abort", {})

        # Round 1 finished by withdrawal; alarm escalated again -> new round.
        alarm = self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "re-escalate"})
        alarm = self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "B"})
        self.assertEqual(alarm["data"]["round"], 2)
        alarm = self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "pending new rescue"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.dispatcher, alarm["id"], "close", {})

        alarm = self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "B"})
        self.assertEqual(alarm["data"]["round"], 3)
        job2 = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "r2", "team": "B"})
        self.assertEqual(job2["data"]["round"], 3)
        self.service.transition(self.dispatcher, job2["id"], "arrive", {})
        self.service.transition(self.dispatcher, job2["id"], "complete", {"outcome": "freed"})
        alarm = self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "safe"})
        alarm = self.service.transition(self.dispatcher, alarm["id"], "close", {})
        self.assertEqual(alarm["status"], "closed")

    def test_rescue_queue_orders_by_arrival_and_severe_jump_ahead(self):
        e1 = self.make_equipment("E-1")
        e2 = self.make_equipment("E-2")
        e3 = self.make_equipment("E-3", group_id="G-9")
        a1 = self.severe_alarm(e1)
        a2 = self.service.create(self.dispatcher, "alarm", {"equipment_id": e2["id"], "code": "X", "occurred_at": "2026-09-29T10:00:00Z", "severity": "normal"})
        a3 = self.service.create(self.dispatcher, "alarm", {"equipment_id": e3["id"], "code": "ENTRAPMENT-3", "occurred_at": "2026-09-29T10:05:00Z", "severity": "severe"})
        for alarm in (a1, a2, a3):
            self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "T"})
        normal_eta = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": a2["id"], "dedupe_key": "n1", "team": "N", "eta": "2026-09-29T10:10:00Z"})
        severe_late = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": a1["id"], "dedupe_key": "s1", "team": "S", "eta": "2026-09-29T10:30:00Z"})
        severe_on_site = self.service.create(self.dispatcher, "rescue_job", {"alarm_id": a3["id"], "dedupe_key": "s2", "team": "S2"})
        self.service.transition(self.dispatcher, severe_on_site["id"], "arrive", {})

        queue = self.service.rescue_queue()
        ids = [item["rescue_job_id"] for item in queue]
        self.assertEqual(ids, [severe_on_site["id"], severe_late["id"], normal_eta["id"]])
        self.assertEqual([item["position"] for item in queue], [1, 2, 3])

    def test_recovery_board_shows_scope_and_blockers(self):
        e1 = self.make_equipment("E-1")
        e2 = self.make_equipment("E-2")
        alarm = self.severe_alarm(e1)
        board = self.service.recovery_board()
        self.assertEqual(len(board), 1)
        entry = board[0]
        self.assertEqual(sorted(entry["scope"]), sorted([e1["id"], e2["id"]]))
        by_id = {unit["equipment_id"]: unit for unit in entry["units"]}
        for unit in by_id.values():
            self.assertFalse(unit["ready"])
            kinds = {b["type"] for b in unit["blockers"]}
            self.assertIn("active_alarm", kinds)
            self.assertIn("reinspection_required", kinds)

    def test_permit_blocked_while_alarm_open_even_without_isolation(self):
        e1 = self.make_equipment("E-1", group_id=None)
        insp = self.service.create(self.admin, "inspection", {"equipment_id": e1["id"], "scheduled_at": "2026-09-01T09:00:00Z", "cycle_days": 365})
        self.service.transition(self.admin, insp["id"], "pass", {"findings": "ok"})
        self.service.create(self.dispatcher, "alarm", {"equipment_id": e1["id"], "code": "DOOR", "occurred_at": "2026-09-29T10:00:00Z", "severity": "normal"})
        permit = self.service.create(self.admin, "permit", {"equipment_id": e1["id"], "purpose": "special_inspection", "requested_by": "ops"})
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, permit["id"], "grant", {})


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.actor = Actor("inspector-1", "inspector")
        self.equipment = self.service.create(
            self.actor,
            "equipment",
            {"asset_no": "E-OFF", "equipment_type": "elevator", "location": "B", "inspection_interval_days": 365},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def merge(self, source, record, version, readings=None):
        payload = {"source_id": source, "record_id": record, "version": version, "equipment_id": self.equipment["id"]}
        if readings is not None:
            payload["readings"] = readings
        return self.service.merge_offline(self.actor, [payload])

    def test_versioned_merge_replay_and_supersede(self):
        result = self.merge("tab-1", "r-1", 1, {"temp_c": 30})
        self.assertEqual(result[0]["merge_outcome"], "created")
        equipment = self.service.get(self.equipment["id"])
        self.assertEqual(equipment["data"]["offline_fields"]["temp_c"]["value"], 30)

        # Same request replayed: duplicate, equipment must not be rewritten.
        replay = self.merge("tab-1", "r-1", 1, {"temp_c": 30})
        self.assertEqual(replay[0]["merge_outcome"], "duplicate")
        record = self.service.get(replay[0]["id"])
        self.assertEqual(record["data"]["version"], 1)

        # Newer version applies.
        updated = self.merge("tab-1", "r-1", 2, {"temp_c": 31})
        self.assertEqual(updated[0]["merge_outcome"], "updated")
        equipment = self.service.get(self.equipment["id"])
        self.assertEqual(equipment["data"]["offline_fields"]["temp_c"]["value"], 31)
        self.assertEqual(equipment["data"]["offline_fields"]["temp_c"]["marker"]["version"], 2)

        # Old version arriving late: kept queryable, never downgrades equipment.
        old = self.merge("tab-1", "r-1", 1, {"temp_c": 5})
        self.assertEqual(old[0]["merge_outcome"], "superseded")
        equipment = self.service.get(self.equipment["id"])
        self.assertEqual(equipment["data"]["offline_fields"]["temp_c"]["value"], 31)

        versions = self.service.offline_record_versions("tab-1", "r-1")
        self.assertEqual(len(versions), 1)  # one logical record, current version
        self.assertEqual(versions[0]["version"], 2)

    def test_replay_after_restart_does_not_change_equipment_twice(self):
        self.merge("tab-9", "r-9", 1, {"vibration": 0.2})
        first = self.service.get(self.equipment["id"])["data"]["offline_fields"]["vibration"]

        # Simulate service restart / scale-out: brand new service on same DB file.
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        result = restarted.merge_offline(
            Actor("inspector-1", "inspector"),
            [{"source_id": "tab-9", "record_id": "r-9", "version": 1, "equipment_id": self.equipment["id"], "readings": {"vibration": 0.2}}],
        )
        self.assertEqual(result[0]["merge_outcome"], "duplicate")
        again = self.service.get(self.equipment["id"])["data"]["offline_fields"]["vibration"]
        self.assertEqual(first, again)

        # v2 on the restarted service applies once.
        result = restarted.merge_offline(
            Actor("inspector-1", "inspector"),
            [{"source_id": "tab-9", "record_id": "r-9", "version": 2, "equipment_id": self.equipment["id"], "readings": {"vibration": 0.4}}],
        )
        self.assertEqual(result[0]["merge_outcome"], "updated")
        self.assertEqual(self.service.get(self.equipment["id"])["data"]["offline_fields"]["vibration"]["value"], 0.4)

        # Replaying v2 on either instance changes nothing.
        again2 = self.service.merge_offline(
            Actor("inspector-1", "inspector"),
            [{"source_id": "tab-9", "record_id": "r-9", "version": 2, "equipment_id": self.equipment["id"], "readings": {"vibration": 0.4}}],
        )
        self.assertEqual(again2[0]["merge_outcome"], "duplicate")


if __name__ == "__main__":
    unittest.main()
