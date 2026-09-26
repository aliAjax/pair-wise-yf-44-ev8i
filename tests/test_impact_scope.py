import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ImpactScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.operator = Actor("op-1", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _unit(self, name="Reactor-1"):
        return self.service.create(
            self.admin, "unit", {"name": name, "location": "Plant-A"}
        )

    def _change(self, unit_ids, desc="Change alarm threshold"):
        return self.service.create(
            self.admin, "change", {"description": desc, "impacted_units": unit_ids}
        )

    def _assess(self, change_id, risk="medium"):
        return self.service.transition(
            self.admin, change_id, "assess", {"risk_level": risk, "analyst": "E-1"}
        )

    def _approve(self, change_id, approvals=("S-1", "S-2"), permit="MOC-1"):
        return self.service.transition(
            self.admin,
            change_id,
            "approve",
            {"approvals": list(approvals), "permit_id": permit},
        )

    def test_creation_snapshots_unit_status(self):
        unit = self._unit()
        change = self._change([unit["id"]])
        scope = change["data"]["impacted_units"]
        self.assertEqual(len(scope), 1)
        self.assertEqual(scope[0]["unit_id"], unit["id"])
        self.assertEqual(scope[0]["unit_name"], "Reactor-1")
        self.assertEqual(scope[0]["status_at_registration"], "operating")

    def test_creation_requires_impacted_units(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "change", {"description": "no scope"})

    def test_creation_rejects_unknown_unit(self):
        with self.assertRaises(ValidationError):
            self._change(["no-such-unit"])

    def test_legacy_unit_id_derives_scope(self):
        unit = self._unit()
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": unit["id"], "description": "legacy single unit"},
        )
        scope = change["data"]["impacted_units"]
        self.assertEqual(scope[0]["unit_id"], unit["id"])
        self.assertEqual(scope[0]["status_at_registration"], "operating")

    def test_approve_blocked_when_unit_status_changed(self):
        unit = self._unit()
        change = self._change([unit["id"]])
        self._assess(change["id"])
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"}
        )
        with self.assertRaises(ValidationError) as ctx:
            self._approve(change["id"])
        self.assertIn("Reactor-1", str(ctx.exception))
        self.service.transition(self.operator, unit["id"], "startup", {})
        approved = self._approve(change["id"])
        self.assertEqual(approved["status"], "approved")

    def test_shutdown_after_approval_reverts_review(self):
        unit = self._unit()
        change = self._change([unit["id"]])
        self._assess(change["id"])
        self._approve(change["id"])
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "trip"}
        )
        reverted = self.service.get(change["id"])
        self.assertEqual(reverted["status"], "assessed")
        inv = reverted["data"]["review_invalidated"]
        self.assertEqual(inv["reason"], "unit_shutdown")
        self.assertEqual(inv["unit_id"], unit["id"])
        self.assertEqual(inv["unit_name"], "Reactor-1")
        self.assertEqual(reverted["data"]["approvals"], [])
        actions = [a["action"] for a in self.service.audit_log(change["id"])]
        self.assertIn("invalidate_review", actions)

    def test_freeze_after_implement_reverts_review(self):
        unit = self._unit()
        change = self._change([unit["id"]])
        self._assess(change["id"])
        self._approve(change["id"])
        self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v2"}
        )
        self.service.transition(
            self.operator, unit["id"], "freeze", {"reason": "conserve"}
        )
        reverted = self.service.get(change["id"])
        self.assertEqual(reverted["status"], "assessed")
        self.assertEqual(reverted["data"]["review_invalidated"]["reason"], "unit_frozen")

    def test_unrelated_change_not_reverted(self):
        u1 = self._unit("R-1")
        u2 = self._unit("R-2")
        c1 = self._change([u1["id"]])
        c2 = self._change([u2["id"]])
        for cid in (c1["id"], c2["id"]):
            self._assess(cid)
            self._approve(cid)
        self.service.transition(
            self.operator, u1["id"], "shutdown", {"reason": "trip"}
        )
        self.assertEqual(self.service.get(c1["id"])["status"], "assessed")
        self.assertEqual(self.service.get(c2["id"])["status"], "approved")

    def test_scope_change_after_approval_invalidates(self):
        u1 = self._unit("R-1")
        u2 = self._unit("R-2")
        change = self._change([u1["id"]])
        self._assess(change["id"])
        self._approve(change["id"])
        updated = self.service.transition(
            self.admin,
            change["id"],
            "update_scope",
            {"impacted_units": [u1["id"], u2["id"]]},
        )
        self.assertEqual(updated["status"], "assessed")
        inv = updated["data"]["review_invalidated"]
        self.assertEqual(inv["reason"], "scope_changed")
        self.assertEqual(inv["added_units"], [u2["id"]])
        self.assertEqual(inv["removed_units"], [])
        snaps = {
            s["unit_id"]: s["status_at_registration"]
            for s in updated["data"]["impacted_units"]
        }
        self.assertEqual(snaps[u2["id"]], "operating")

    def test_scope_change_on_draft_keeps_status(self):
        u1 = self._unit("R-1")
        u2 = self._unit("R-2")
        change = self._change([u1["id"]])
        updated = self.service.transition(
            self.admin,
            change["id"],
            "update_scope",
            {"impacted_units": [u1["id"], u2["id"]]},
        )
        self.assertEqual(updated["status"], "draft")
        self.assertIsNone(updated["data"].get("review_invalidated"))
        self.assertEqual(len(updated["data"]["impacted_units"]), 2)

    def test_risk_raise_invalidates_and_requires_more_approvals(self):
        unit = self._unit()
        change = self._change([unit["id"]])
        self._assess(change["id"], risk="medium")
        self._approve(change["id"])
        updated = self._assess(change["id"], risk="high")
        self.assertEqual(updated["status"], "assessed")
        self.assertEqual(
            updated["data"]["review_invalidated"]["reason"], "risk_level_raised"
        )
        self.assertEqual(updated["data"]["required_approvals"], 3)
        with self.assertRaises(ValidationError):
            self._approve(change["id"], approvals=("S-1", "S-2"))
        approved = self._approve(change["id"], approvals=("S-1", "S-2", "S-3"))
        self.assertEqual(approved["status"], "approved")

    def test_risk_not_raised_keeps_approval(self):
        unit = self._unit()
        change = self._change([unit["id"]])
        self._assess(change["id"], risk="high")
        self._approve(change["id"], approvals=("S-1", "S-2", "S-3"))
        updated = self._assess(change["id"], risk="low")
        self.assertEqual(updated["status"], "approved")
        self.assertIsNone(updated["data"].get("review_invalidated"))

    def test_commission_blocked_until_reapproval_despite_verified_items(self):
        unit = self._unit()
        change = self._change([unit["id"]])
        self._assess(change["id"])
        self._approve(change["id"])
        self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v2"}
        )
        item = self.service.create(
            self.admin,
            "action_item",
            {"change_id": change["id"], "description": "Train operators", "owner": "O-1"},
        )
        self.service.transition(
            self.admin,
            item["id"],
            "complete",
            {"completed_by": "O-1", "evidence": "training-log"},
        )
        self.service.transition(self.admin, item["id"], "verify", {"verifier": "V-1"})
        self.service.transition(
            self.operator, unit["id"], "freeze", {"reason": "conserve"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, change["id"], "commission", {"tests_passed": True}
            )
        self.service.transition(self.operator, unit["id"], "unfreeze", {})
        self._approve(change["id"], permit="MOC-2")
        self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v3"}
        )
        done = self.service.transition(
            self.admin, change["id"], "commission", {"tests_passed": True}
        )
        self.assertEqual(done["status"], "commissioned")


if __name__ == "__main__":
    unittest.main()
