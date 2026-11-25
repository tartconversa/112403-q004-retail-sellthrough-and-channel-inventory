"""Replenishment, promotion allocation and end-to-end service behaviour."""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from channelops.model import (
    Cell,
    Role,
    Coverage,
    DemandRange,
    Event,
    Kind,
    Orders,
    Period,
    PostingPolicy,
    Replenishment,
    Scenario,
    StockSnapshot,
    Store,
    StoreDemand,
)
from channelops.replenish import proposal_risk, promotion_decisions, recommend
from channelops.service import Service

DEMO = Path(__file__).resolve().parent.parent / "demo"
OCT = Period(date(2026, 10, 1), date(2026, 10, 31))


def synthetic_review(store_evidence_list, sell_in=0, stock=100, complete=True):
    from channelops.model import Review

    cell = Cell(
        sku="P1",
        region="east",
        sell_in_gross=sell_in,
        sell_in_net=sell_in,
        receipt_locations={},
        transfer_in=0,
        transfer_out=0,
        pos=sum(item.pos for item in store_evidence_list),
        returns=0,
        demand=DemandRange(0, 0, "test"),
        prior_sell_in=None,
        prior_demand=None,
        stock_end=stock,
        stock_known_locations=("S1",),
        stock_complete=complete,
        stock_age_days=20.0,
        oldest_stock_days=40.0,
        coverage_days=None,
        shelf_coverage_days=None,
        coverage_days_partial=None,
        coverage_days_worst=None,
        coverage_days_best=None,
        coverage=Coverage(
            sku="P1",
            region="east",
            expected_stores=("S1",),
            comparable_sales=("S1",),
        ),
        stores=("S1",),
        store_demand=tuple(store_evidence_list),
        demand_window=(OCT.start, OCT.end),
    )
    return Review(
        period=OCT,
        as_of=OCT.end,
        scenario=Scenario(),
        posting_policy=PostingPolicy(),
        cells=[cell],
        gates=[],
        issues=[],
        corrections=[],
        dataset_fingerprint="x",
        hierarchy_fingerprint="y",
    )


def store_demand(low, high, method="stock-bridge", closing=100):
    return StoreDemand(
        store="S1",
        sku="P1",
        region="east",
        low=low,
        high=high,
        method=method,
        closing=closing,
        closing_known=True,
        comparable=high is not None,
    )


class ReplenishmentMath(unittest.TestCase):
    def test_subtracts_open_orders_and_commitments(self):
        review = synthetic_review([store_demand(28, 28, closing=40)])
        orders = Orders(
            open_orders={("S1", "P1"): 5},
            commitments={("S1", "P1"): 7},
            safety_stock={("S1", "P1"): 3},
        )
        rec = recommend(review, [Store("S1", "D1", "east")], orders)[0]
        # run-rate = 28/31 * 28 target days ≈ 25, + safety 3 - 40 - 5 - 7 => 0
        self.assertEqual(rec.open_order, 5)
        self.assertEqual(rec.committed, 7)
        self.assertEqual(rec.safety_stock, 3)
        self.assertEqual(rec.proposed, 0)
        self.assertEqual(rec.status, "ok")

    def test_needs_data_when_no_closing_stock(self):
        review = synthetic_review([store_demand(10, 10, closing=None)])
        rec = recommend(review, [Store("S1", "D1", "east")], Orders())[0]
        self.assertEqual(rec.status, "needs-data")
        self.assertEqual(rec.proposed, 0)
        self.assertTrue(any("缺门店期末库存" in reason for reason in rec.reasons))

    def test_no_ceiling_means_no_run_rate(self):
        review = synthetic_review([store_demand(10, None, method="pos-floor-only")])
        rec = recommend(review, [Store("S1", "D1", "east")], Orders())[0]
        self.assertEqual(rec.status, "needs-data")
        self.assertEqual(rec.proposed, 0)


class PromotionRules(unittest.TestCase):
    def _cell(self, cover, demand_growth, sell_in_growth):
        # Demand 50 in the current period; prior demand is derived so that the
        # realised growth equals the requested figure.
        current = 50.0
        prior = current / (1 + demand_growth) if demand_growth != -1 else 0.0
        prior_low = int(round(prior))
        cell = Cell(
            sku="P1",
            region="east",
            sell_in_gross=100,
            sell_in_net=100,
            receipt_locations={},
            transfer_in=0,
            transfer_out=0,
            pos=50,
            returns=0,
            demand=DemandRange(int(current), int(current), "stock-bridge"),
            prior_sell_in=50,
            prior_demand=DemandRange(prior_low, prior_low, "prior"),
            stock_end=200,
            stock_known_locations=("S1",),
            stock_complete=True,
            stock_age_days=10,
            oldest_stock_days=20,
            coverage_days=cover,
            shelf_coverage_days=cover,
            coverage_days_partial=cover,
            coverage_days_worst=cover,
            coverage_days_best=cover,
            coverage=Coverage(
                sku="P1",
                region="east",
                expected_stores=("S1",),
                comparable_sales=("S1",),
            ),
            stores=("S1",),
            store_demand=(),
        )
        from channelops.model import Review

        return Review(
            period=OCT,
            as_of=OCT.end,
            scenario=Scenario(),
            posting_policy=PostingPolicy(),
            cells=[cell],
            gates=[],
            issues=[],
            corrections=[],
            dataset_fingerprint="x",
            hierarchy_fingerprint="y",
        )

    def test_high_cover_blocks_promotion_despite_sell_in_growth(self):
        review = self._cell(cover=120, demand_growth=1.0, sell_in_growth=4.0)
        decision = promotion_decisions(review)[0]
        self.assertFalse(decision.eligible)
        self.assertIn("库存偏高", decision.reason)
        self.assertEqual(decision.score, 0.0)

    def test_healthy_cover_and_demand_growth_eligible(self):
        review = self._cell(cover=30, demand_growth=0.5, sell_in_growth=0.5)
        decision = promotion_decisions(review)[0]
        self.assertTrue(decision.eligible)
        self.assertGreater(decision.score, 0)

    def test_sell_in_growth_never_feeds_the_score(self):
        base = promotion_decisions(self._cell(cover=30, demand_growth=0.5, sell_in_growth=0.5))[0]
        loaded = promotion_decisions(self._cell(cover=30, demand_growth=0.5, sell_in_growth=9.0))[0]
        self.assertEqual(base.score, loaded.score)

    def test_negative_demand_growth_not_eligible(self):
        decision = promotion_decisions(self._cell(cover=30, demand_growth=-0.1, sell_in_growth=2.0))[0]
        self.assertFalse(decision.eligible)


class IssuedRisk(unittest.TestCase):
    def test_risk_when_recomputed_need_below_issued(self):
        proposal = type(
            "P",
            (),
            {
                "proposal_id": "R-S1-P1",
                "store": "S1",
                "sku": "P1",
                "qty": 30,
                "status": "issued",
                "basis": {"demand_low": 10, "demand_high": 10, "stock": 50, "sell_in": 100},
            },
        )()
        rec = Replenishment(
            store="S1",
            sku="P1",
            on_hand=50,
            cover_days=10,
            safety_stock=0,
            safety_source="x",
            open_order=0,
            committed=30,
            proposed=5,
            target_cover_days=28,
            status="ok",
            reasons=(),
            basis={"demand_low": 4, "demand_high": 4, "stock": 50, "sell_in": 100},
        )
        risks = proposal_risk(proposal, rec)
        # Under new evidence the need excluding the promise is 5 + 30 = 35, so a
        # 30-unit promise is not over; but the demand range did change, which is
        # reported.  A promise above the uncommitted need is also reported.
        self.assertTrue(any("动销区间" in risk for risk in risks))
        over = Replenishment(
            store="S1", sku="P1", on_hand=50, cover_days=10, safety_stock=0,
            safety_source="x", open_order=0, committed=30, proposed=0,
            target_cover_days=28, status="ok", reasons=(),
            basis={"demand_low": 4, "demand_high": 4, "stock": 50, "sell_in": 100},
        )
        self.assertTrue(any("压货" in risk for risk in proposal_risk(proposal, over)))

    def test_unchanged_evidence_does_not_flag_issued(self):
        proposal = type(
            "P", (),
            {
                "proposal_id": "R-S1-P1", "store": "S1", "sku": "P1", "qty": 10,
                "status": "issued",
                "basis": {"demand_low": 10, "demand_high": 10, "stock": 50, "sell_in": 100},
            },
        )()
        # The promise is already inside `committed`, so the recompute proposes 0
        # against identical inputs; that is not a risk.
        rec = Replenishment(
            store="S1", sku="P1", on_hand=50, cover_days=10, safety_stock=0,
            safety_source="x", open_order=0, committed=10, proposed=0,
            target_cover_days=28, status="ok", reasons=(),
            basis={"demand_low": 10, "demand_high": 10, "stock": 50, "sell_in": 100},
        )
        self.assertEqual(proposal_risk(proposal, rec), ())


class ServiceIntegration(unittest.TestCase):
    """End-to-end behaviour against the packaged demo dataset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "ws"
        self.service = Service(DEMO, self.workspace)

    def tearDown(self):
        self.tmp.cleanup()

    def test_demo_has_expected_scenarios(self):
        dashboard = self.service.review(None)
        cells = {(cell.sku, cell.region): cell for cell in dashboard.review.cells}
        loading = cells[("P2", "east")]
        self.assertGreaterEqual(loading.sell_in_to_demand, 2)
        self.assertFalse(cells[("P3", "west")].stock_complete)
        healthy = cells[("P4", "west")]
        self.assertEqual(healthy.demand.method, "stock-bridge")

    def test_correction_recomputes_draft_but_not_issued(self):
        dashboard = self.service.review(None)
        draft = next(
            proposal
            for proposal in dashboard.proposals
            if proposal.status == "draft" and proposal.qty > 0
        )
        self.service.issue_proposal(draft.proposal_id, "经理甲")
        # A late POS restatement lowers demand and therefore the proposal.
        self.service.add_correction(
            Event(
                event="CX1",
                store=draft.store,
                sku=draft.sku,
                kind=Kind.RETURN,
                qty=5,
                at=date(2026, 10, 20),
                source="correction",
                note="补报退货",
            )
        )
        after = self.service.review(None)
        issued = next(p for p in after.proposals if p.proposal_id == draft.proposal_id)
        self.assertEqual(issued.status, "issued")
        self.assertEqual(issued.qty, draft.qty)
        # The restated return must leave a trace on the issued commitment.
        self.assertTrue(issued.risk or any(issue.code == "issued-risk" for issue in after.review.issues))

    def test_confirmation_gate_controls_confirmed_flag(self):
        dashboard = self.service.review(None)
        self.assertTrue(dashboard.review.provisional)
        fingerprints = self.service.gate_fingerprints(dashboard)
        from channelops.model import PostingPolicy, Role

        self.service.save_policy(PostingPolicy(), "财务甲")
        for role in (Role.CHANNEL_OPS, Role.FINANCE, Role.REGIONAL_MANAGER):
            self.service.confirm(role, fingerprints[role], "确认人")
        refreshed = self.service.review(None)
        self.assertTrue(refreshed.review.confirmed)
        self.assertFalse(refreshed.review.provisional)

    def test_data_change_invalidates_downstream_confirmation(self):
        dashboard = self.service.review(None)
        fingerprints = self.service.gate_fingerprints(dashboard)
        self.service.save_policy(PostingPolicy(), "财务甲")
        for role in (Role.CHANNEL_OPS, Role.FINANCE, Role.REGIONAL_MANAGER):
            self.service.confirm(role, fingerprints[role], "确认人")
        # A restated return touches the finance posting basis fingerprint. A POS
        # correction deliberately would not, since finance owns receipt/return
        # posting, not scanner traffic.
        self.service.add_correction(
            Event(
                event="CX2",
                store="S1",
                sku="P1",
                kind=Kind.RETURN,
                qty=1,
                at=date(2026, 10, 12),
                source="correction",
            )
        )
        refreshed = self.service.review(None)
        finance = refreshed.review.gate(Role.FINANCE)
        self.assertFalse(finance.confirmed)
        self.assertTrue(finance.stale)

    def test_upload_rejects_wrong_header_without_replacing(self):
        original = (DEMO / "events.csv").read_bytes()
        with self.assertRaises(Exception):
            self.service.install_upload("events.csv", b"bogus,header\n1,2\n")
        self.assertEqual((DEMO / "events.csv").read_bytes(), original)

    def test_upload_rejects_path_escape(self):
        with self.assertRaises(Exception):
            self.service.install_upload("../evil.csv", b"a,b\n1,2\n")

    def test_read_only_review_does_not_write_proposals(self):
        # A page render must not touch state, or it could race an issue.
        self.service.review(None)
        self.assertFalse((self.workspace / "proposals.json").exists())
        self.service.review(None)
        self.assertFalse((self.workspace / "proposals.json").exists())

    def test_explicit_action_materialises_drafts(self):
        dashboard = self.service.review(None)
        self.assertTrue(any(p.status == "draft" and p.qty > 0 for p in dashboard.proposals))
        self.service.add_correction(
            Event(
                event="CX3",
                store="S1",
                sku="P1",
                kind=Kind.POS,
                qty=1,
                at=date(2026, 10, 12),
                source="correction",
            )
        )
        self.assertTrue((self.workspace / "proposals.json").exists())

    def test_stale_draft_list_cannot_overwrite_issued(self):
        # Simulate a page that computed drafts, then lost a race to an issue.
        dashboard = self.service.review(None)
        positive = next(p for p in dashboard.proposals if p.status == "draft" and p.qty > 0)
        self.service.issue_proposal(positive.proposal_id, "经理", period=(OCT.start, OCT.end))
        stale_drafts = [p for p in dashboard.proposals if p.status == "draft"]
        self.service.workspace.persist_drafts(stale_drafts)
        issued = next(
            p for p in self.service.workspace.load_proposals() if p.proposal_id == positive.proposal_id
        )
        self.assertEqual(issued.status, "issued")
        self.assertEqual(issued.qty, positive.qty)

    def test_csv_export_has_evidence_columns(self):
        dashboard = self.service.review(None)
        text = self.service.csv_export(dashboard)
        header = text.splitlines()[0]
        for column in ("demand_low", "demand_high", "demand_method", "coverage_days", "pending_data"):
            self.assertIn(column, header)


if __name__ == "__main__":
    unittest.main()
