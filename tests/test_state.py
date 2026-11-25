"""Workspace state: confirmations, corrections, proposals and recompute rules."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from channelops.model import Event, Kind, PostingPolicy, Role
from channelops.state import StateError, Workspace


class ConfirmationRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmation_requires_operator(self):
        with self.assertRaises(StateError):
            self.ws.confirm(Role.CHANNEL_OPS, "confirmed", "fp", "   ")

    def test_finance_confirmation_requires_policy(self):
        with self.assertRaises(StateError):
            self.ws.confirm(Role.FINANCE, "confirmed", "fp", "张三")
        self.ws.save_policy(PostingPolicy(returns_restock=False), "李四")
        entry = self.ws.confirm(Role.FINANCE, "confirmed", "fp", "张三")
        self.assertEqual(entry.decision, "confirmed")
        self.assertEqual(self.ws.load_policy().returns_restock, False)

    def test_new_confirmation_replaces_old_for_same_role(self):
        self.ws.confirm(Role.CHANNEL_OPS, "confirmed", "fp1", "甲")
        self.ws.confirm(Role.CHANNEL_OPS, "confirmed", "fp2", "乙")
        items = self.ws.load_confirmations()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].fingerprint, "fp2")

    def test_invalid_decision_rejected(self):
        with self.assertRaises(StateError):
            self.ws.confirm(Role.CHANNEL_OPS, "maybe", "fp", "甲")


class Corrections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, ident="C1", qty=5, target="E1"):
        return Event(
            event=ident,
            store="S1",
            sku="P1",
            kind=Kind.POS,
            qty=qty,
            at=date(2026, 10, 4),
            source="correction",
            corrected_of=target,
        )

    def test_duplicate_correction_rejected(self):
        self.ws.append_correction(self._event())
        with self.assertRaises(StateError):
            self.ws.append_correction(self._event())

    def test_negative_quantity_rejected(self):
        with self.assertRaises(StateError):
            self.ws.append_correction(self._event(qty=-1))

    def test_path_traversal_identifiers_rejected(self):
        with self.assertRaises(StateError):
            self.ws.append_correction(self._event(ident="../../etc/passwd"))

    def test_corrections_are_persisted(self):
        self.ws.append_correction(self._event())
        loaded = self.ws.load_corrections()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].corrected_of, "E1")
        self.assertEqual(loaded[0].source, "correction")


class ScenarioValidation(unittest.TestCase):
    def test_invalid_thresholds_rejected(self):
        from channelops.model import Scenario

        cases = [
            dict(stock_low_cover_days=50),
            dict(target_cover_days=0),
            dict(min_coverage_ratio=0),
            dict(min_coverage_ratio=1.5),
            dict(anchor_tolerance_days=-1),
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    Scenario(**case).validated()

    def test_service_rejects_invalid_scenario(self):
        import tempfile
        from pathlib import Path

        from channelops.model import Scenario
        from channelops.service import Service

        demo = Path(__file__).resolve().parent.parent / "demo"
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(demo, Path(tmp) / "ws")
            with self.assertRaises(ValueError):
                service.save_scenario(Scenario(min_coverage_ratio=2), "经理")


class ProposalRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_draft_is_updated_in_place(self):
        self.ws.upsert_draft("S1", "P1", 10, {"stock": 5}, ())
        self.ws.upsert_draft("S1", "P1", 4, {"stock": 9}, ())
        proposals = self.ws.load_proposals()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].qty, 4)
        self.assertEqual(proposals[0].status, "draft")

    def test_issued_proposal_is_frozen_against_recompute(self):
        self.ws.upsert_draft("S1", "P1", 10, {"stock": 5}, ())
        self.ws.issue("R-S1-P1", "经理")
        # A later recompute must not overwrite the issued row.
        returned = self.ws.upsert_draft("S1", "P1", 1, {"stock": 40}, ())
        self.assertEqual(returned.status, "issued")
        self.assertEqual(returned.qty, 10)

    def test_zero_draft_cannot_be_issued(self):
        self.ws.upsert_draft("S1", "P1", 0, {"stock": 5}, (), note="待补数据")
        with self.assertRaises(StateError):
            self.ws.issue("R-S1-P1", "经理")

    def test_issue_requires_operator_and_is_idempotent_guard(self):
        self.ws.upsert_draft("S1", "P1", 10, {"stock": 5}, ())
        with self.assertRaises(StateError):
            self.ws.issue("R-S1-P1", "  ")
        self.ws.issue("R-S1-P1", "经理")
        with self.assertRaises(StateError):
            self.ws.issue("R-S1-P1", "经理")

    def test_issued_risk_is_updated_but_quantity_is_not(self):
        self.ws.upsert_draft("S1", "P1", 10, {"stock": 5}, ())
        self.ws.issue("R-S1-P1", "经理")
        self.ws.update_issued_risk({"R-S1-P1": ("新证据下建议 0 件",)})
        proposal = self.ws.load_proposals()[0]
        self.assertEqual(proposal.qty, 10)
        self.assertEqual(proposal.risk, ("新证据下建议 0 件",))

    def test_reconcile_zeroes_orphan_drafts_but_not_issued(self):
        self.ws.upsert_draft("S1", "P1", 10, {}, ())
        self.ws.upsert_draft("S2", "P1", 8, {}, ())
        self.ws.issue("R-S2-P1", "经理")
        self.ws.reconcile_drafts({("S1", "P1")})
        proposals = {p.proposal_id: p for p in self.ws.load_proposals()}
        self.assertEqual(proposals["R-S2-P1"].qty, 8)
        self.assertEqual(proposals["R-S2-P1"].status, "issued")

    def test_audit_log_records_actions(self):
        self.ws.upsert_draft("S1", "P1", 10, {}, ())
        self.ws.issue("R-S1-P1", "经理")
        actions = [entry["action"] for entry in self.ws.read_audit()]
        self.assertIn("issue", actions)


if __name__ == "__main__":
    unittest.main()
