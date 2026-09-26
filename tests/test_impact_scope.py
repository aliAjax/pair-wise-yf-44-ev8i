import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ImpactScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _unit(self, name):
        return self.service.create(
            self.admin, "unit", {"name": name, "location": "Plant-A"}
        )

    def _change(self, unit_ids, description="更换联锁逻辑"):
        return self.service.create(
            self.admin,
            "change",
            {"affected_unit_ids": list(unit_ids), "description": description},
        )

    def _approve(self, change, risk="medium", approvals=("S-1", "S-2")):
        self.service.transition(
            self.admin,
            change["id"],
            "assess",
            {"risk_level": risk, "analyst": "E-1"},
        )
        return self.service.transition(
            self.admin,
            change["id"],
            "approve",
            {"approvals": list(approvals), "permit_id": "MOC-1"},
        )

    def test_create_snapshots_each_unit_status(self):
        u1 = self._unit("Reactor-1")
        u2 = self._unit("Reactor-2")
        change = self._change([u1["id"], u2["id"]])
        self.assertEqual(change["data"]["affected_unit_ids"], [u1["id"], u2["id"]])
        snapshots = change["data"]["affected_units"]
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(snap["status"] == "operating" for snap in snapshots))
        self.assertTrue(all(snap["captured_at"] for snap in snapshots))
        self.assertEqual(snapshots[0]["unit_name"], "Reactor-1")
        # legacy single-unit field is kept consistent
        self.assertEqual(change["data"]["unit_id"], u1["id"])

    def test_create_requires_at_least_one_affected_unit(self):
        with self.assertRaises(ValidationError):
            self._change([])
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin, "change", {"description": "无影响装置"}
            )

    def test_unknown_affected_unit_rejected(self):
        with self.assertRaises(ValidationError):
            self._change(["does-not-exist"])

    def test_unit_shutdown_after_approval_returns_change_to_assessed(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self._approve(change)
        updated = self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "临时检修"}
        )
        self.assertEqual(updated["status"], "shutdown")
        change = self.service.get(change["id"])
        self.assertEqual(change["status"], "assessed")
        blocks = change["data"]["blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["unit_id"], unit["id"])
        self.assertEqual(blocks[0]["unit_status"], "shutdown")
        self.assertIn("Reactor-1", blocks[0]["message"])
        self.assertEqual(
            change["data"]["void_reason"]["code"], "affected_unit_shutdown"
        )

    def test_unit_freeze_after_approval_returns_change_to_assessed(self):
        unit = self._unit("Tower-A")
        change = self._change([unit["id"]])
        self._approve(change)
        self.service.transition(
            self.admin, unit["id"], "freeze", {"reason": "工艺冻结"}
        )
        change = self.service.get(change["id"])
        self.assertEqual(change["status"], "assessed")
        self.assertEqual(change["data"]["blocks"][0]["unit_status"], "frozen")
        self.assertIn("冻结", change["data"]["blocks"][0]["message"])

    def test_only_affected_changes_are_returned(self):
        u1 = self._unit("Reactor-1")
        u2 = self._unit("Reactor-2")
        c1 = self._change([u1["id"]], "change on R1")
        c2 = self._change([u2["id"]], "change on R2")
        self._approve(c1)
        self._approve(c2)
        self.service.transition(
            self.admin, u1["id"], "shutdown", {"reason": "检修"}
        )
        self.assertEqual(self.service.get(c1["id"])["status"], "assessed")
        self.assertEqual(self.service.get(c2["id"])["status"], "approved")

    def test_draft_and_assessed_changes_are_not_auto_returned(self):
        unit = self._unit("Reactor-1")
        draft = self._change([unit["id"]], "draft change")
        assessed = self._change([unit["id"]], "assessed change")
        self.service.transition(
            self.admin,
            assessed["id"],
            "assess",
            {"risk_level": "low", "analyst": "E-1"},
        )
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "检修"}
        )
        self.assertEqual(self.service.get(draft["id"])["status"], "draft")
        self.assertEqual(self.service.get(assessed["id"])["status"], "assessed")

    def test_startup_clears_block_but_change_still_needs_reapproval(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self._approve(change)
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "检修"}
        )
        self.service.transition(
            self.admin, unit["id"], "startup", {"reason": "恢复开车"}
        )
        change = self.service.get(change["id"])
        self.assertEqual(change["status"], "assessed")
        self.assertEqual(change["data"]["blocks"], [])

    def test_cannot_approve_while_affected_unit_is_down(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self.service.transition(
            self.admin,
            change["id"],
            "assess",
            {"risk_level": "low", "analyst": "E-1"},
        )
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "检修"}
        )
        with self.assertRaises(ValidationError) as context:
            self.service.transition(
                self.admin,
                change["id"],
                "approve",
                {"approvals": ["S-1"], "permit_id": "P-1"},
            )
        self.assertIn("Reactor-1", str(context.exception))

    def test_revise_add_unit_after_approval_voids_review(self):
        u1 = self._unit("Reactor-1")
        u2 = self._unit("Reactor-2")
        change = self._change([u1["id"]])
        self._approve(change)
        change = self.service.transition(
            self.admin,
            change["id"],
            "revise",
            {
                "description": change["data"]["description"],
                "affected_unit_ids": [u1["id"], u2["id"]],
            },
        )
        self.assertEqual(change["status"], "assessed")
        self.assertEqual(
            change["data"]["void_reason"]["code"], "scope_or_risk_changed"
        )
        self.assertIn("Reactor-2", change["data"]["void_reason"]["message"])
        self.assertEqual(
            change["data"]["affected_unit_ids"], [u1["id"], u2["id"]]
        )

    def test_revise_remove_unit_after_approval_voids_review(self):
        u1 = self._unit("Reactor-1")
        u2 = self._unit("Reactor-2")
        change = self._change([u1["id"], u2["id"]])
        self._approve(change)
        change = self.service.transition(
            self.admin,
            change["id"],
            "revise",
            {
                "description": change["data"]["description"],
                "affected_unit_ids": [u1["id"]],
            },
        )
        self.assertEqual(change["status"], "assessed")
        self.assertIn("移除", change["data"]["void_reason"]["message"])

    def test_revise_raise_risk_level_voids_review(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self._approve(change, risk="medium")
        change = self.service.transition(
            self.admin,
            change["id"],
            "revise",
            {
                "description": change["data"]["description"],
                "affected_unit_ids": change["data"]["affected_unit_ids"],
                "risk_level": "high",
            },
        )
        self.assertEqual(change["status"], "assessed")
        self.assertEqual(change["data"]["required_approvals"], 3)
        self.assertIn("调高", change["data"]["void_reason"]["message"])

    def test_revise_lower_risk_level_keeps_approval(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self._approve(change, risk="high", approvals=("S-1", "S-2", "S-3"))
        change = self.service.transition(
            self.admin,
            change["id"],
            "revise",
            {
                "description": change["data"]["description"],
                "affected_unit_ids": change["data"]["affected_unit_ids"],
                "risk_level": "low",
            },
        )
        self.assertEqual(change["status"], "approved")
        self.assertIsNone(change["data"]["void_reason"])
        self.assertEqual(change["data"]["required_approvals"], 1)

    def test_revise_description_only_keeps_approval(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]], "old description")
        self._approve(change)
        change = self.service.transition(
            self.admin,
            change["id"],
            "revise",
            {
                "description": "new description",
                "affected_unit_ids": change["data"]["affected_unit_ids"],
            },
        )
        self.assertEqual(change["status"], "approved")
        self.assertEqual(change["data"]["description"], "new description")

    def test_revise_cannot_recommission_without_reapproval(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self._approve(change)
        change = self.service.transition(
            self.admin,
            change["id"],
            "implement",
            {"procedure_version": "v1"},
        )
        # scope change after implementation voids the review
        extra = self._unit("Reactor-9")
        change = self.service.transition(
            self.admin,
            change["id"],
            "revise",
            {
                "description": change["data"]["description"],
                "affected_unit_ids": [unit["id"], extra["id"]],
            },
        )
        self.assertEqual(change["status"], "assessed")
        from src.domain import InvalidTransition
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin,
                change["id"],
                "commission",
                {"tests_passed": True},
            )

    def test_commission_blocked_when_unit_down_even_if_items_verified(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self._approve(change)
        self.service.transition(
            self.admin,
            change["id"],
            "implement",
            {"procedure_version": "v1"},
        )
        item = self.service.create(
            self.admin,
            "action_item",
            {"change_id": change["id"], "description": "盲板隔离", "owner": "O-1"},
        )
        self.service.transition(
            self.admin,
            item["id"],
            "complete",
            {"completed_by": "O-1", "evidence": "照片"},
        )
        self.service.transition(
            self.admin, item["id"], "verify", {"verifier": "V-1"}
        )
        # unit shut down after implementation -> auto return to assessed
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "装置故障"}
        )
        change = self.service.get(change["id"])
        self.assertEqual(change["status"], "assessed")
        self.assertEqual(len(change["data"]["blocks"]), 1)
        # re-approval is refused while the unit is still down; even though
        # every action item is verified, the change cannot move toward投产
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                change["id"],
                "approve",
                {"approvals": ["S-1", "S-2"], "permit_id": "MOC-2"},
            )

    def test_block_named_after_specific_down_unit_only(self):
        u1 = self._unit("Reactor-1")
        u2 = self._unit("Reactor-2")
        change = self._change([u1["id"], u2["id"]])
        self._approve(change)
        self.service.transition(
            self.admin, u1["id"], "shutdown", {"reason": "检修"}
        )
        change = self.service.get(change["id"])
        blocks = change["data"]["blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["unit_id"], u1["id"])

    def test_audit_records_auto_return(self):
        unit = self._unit("Reactor-1")
        change = self._change([unit["id"]])
        self._approve(change)
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "检修"}
        )
        log = self.service.audit_log(change["id"])
        actions = [entry["action"] for entry in log]
        self.assertIn("auto_return", actions)
        entry = next(entry for entry in log if entry["action"] == "auto_return")
        self.assertEqual(entry["from_status"], "approved")
        self.assertEqual(entry["to_status"], "assessed")
        self.assertEqual(entry["detail"]["block"]["unit_id"], unit["id"])


if __name__ == "__main__":
    unittest.main()
