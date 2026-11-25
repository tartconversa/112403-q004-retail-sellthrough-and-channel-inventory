"""Analytical rules: evidence, transfers, coverage and channel loading."""

import unittest
from datetime import date

from channelops import analytics
from channelops.model import (
    Campaign,
    Event,
    FeedWindow,
    Kind,
    Period,
    PostingPolicy,
    Scenario,
    StockSnapshot,
    Store,
)


OCT = Period(date(2026, 10, 1), date(2026, 10, 31))


def stores():
    return [Store("S1", "D1", "east"), Store("S2", "D1", "east")]


def run(events, snapshots, **kwargs):
    kwargs.setdefault("scenario", Scenario())
    kwargs.setdefault("policy", PostingPolicy())
    return analytics.analyse(
        period=kwargs.pop("period", OCT),
        as_of=kwargs.pop("as_of", date(2026, 10, 31)),
        stores=kwargs.pop("stores", stores()),
        events=events,
        snapshots=snapshots,
        confirmations=[],
        **kwargs,
    )


class StockBridge(unittest.TestCase):
    def test_bridge_pins_demand_when_both_anchors_exist(self):
        events = [
            Event("E1", "S1", "P1", Kind.POS, 30, date(2026, 10, 10)),
            Event("E2", "S2", "P1", Kind.POS, 20, date(2026, 10, 12)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 100, date(2026, 9, 30)),
            StockSnapshot("S2", "P1", 60, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 70, date(2026, 10, 31)),
            StockSnapshot("S2", "P1", 40, date(2026, 10, 31)),
        ]
        review = run(events, snapshots)
        cell = review.cell("P1", "east")
        self.assertIsNotNone(cell)
        self.assertEqual(cell.demand.method, "stock-bridge")
        self.assertEqual((cell.demand.low, cell.demand.high), (50, 50))

    def test_no_snapshot_gives_floor_not_point(self):
        events = [Event("E1", "S1", "P1", Kind.POS, 30, date(2026, 10, 10))]
        review = run(events, [])
        cell = review.cell("P1", "east")
        self.assertEqual(cell.demand.high, None)
        self.assertEqual(cell.demand.low, 30)
        self.assertNotEqual(cell.demand.method, "stock-bridge")
        self.assertIsNone(cell.coverage_days)
        self.assertIn("stock", cell.coverage.missing)

    def test_missing_closing_snapshot_for_one_store_widens_range(self):
        events = [
            Event("E1", "S1", "P1", Kind.POS, 10, date(2026, 10, 10)),
            Event("E2", "S2", "P1", Kind.POS, 10, date(2026, 10, 10)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 100, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 90, date(2026, 10, 31)),
            StockSnapshot("S2", "P1", 100, date(2026, 9, 30)),
            # S2 closing deliberately absent
        ]
        review = run(events, snapshots)
        cell = review.cell("P1", "east")
        self.assertEqual(cell.demand.method, "bounded-pos")
        # Both stores' observed net POS sum to the 20-unit floor. S2 has only an
        # opening anchor, so its ceiling is "everything that ever entered" (100);
        # the region must carry that unfalsifiable width rather than collapse it.
        self.assertEqual(cell.demand.low, 20)
        self.assertEqual(cell.demand.high, 110)
        self.assertFalse(cell.stock_complete)
        self.assertIn("S2", cell.missing_stock_locations)

    def test_bridge_gap_widens_instead_of_claiming_a_point(self):
        events = [Event("E1", "S1", "P1", Kind.POS, 10, date(2026, 10, 10))]
        snapshots = [
            StockSnapshot("S1", "P1", 100, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 60, date(2026, 10, 31)),  # bridge says 40
        ]
        review = run(events, snapshots)
        store = review.cell("P1", "east").store_demand[0]
        self.assertEqual(store.method, "stock-bridge-unreconciled")
        self.assertEqual((store.low, store.high), (10, 40))


class TransferRules(unittest.TestCase):
    def test_transfer_never_counts_as_sale_or_a_second_receipt(self):
        events = [
            Event("E1", "S1", "P1", Kind.TRANSFER_OUT, 10, date(2026, 10, 5)),
            Event("E2", "S2", "P1", Kind.TRANSFER_IN, 10, date(2026, 10, 5)),
            Event("E3", "S1", "P1", Kind.POS, 5, date(2026, 10, 8)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 50, date(2026, 9, 30)),
            StockSnapshot("S2", "P1", 40, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 35, date(2026, 10, 31)),
            StockSnapshot("S2", "P1", 50, date(2026, 10, 31)),
        ]
        review = run(events, snapshots)
        cell = review.cell("P1", "east")
        self.assertEqual(cell.sell_in_net, 0)
        self.assertEqual(cell.demand.low, 5)
        self.assertEqual(cell.transfer_in, 10)
        self.assertEqual(cell.transfer_out, 10)
        s2 = next(item for item in cell.store_demand if item.store == "S2")
        self.assertEqual(s2.pos, 0)
        self.assertEqual(s2.low, 0)

    def test_one_sided_transfer_is_flagged(self):
        events = [Event("E1", "S1", "P1", Kind.TRANSFER_OUT, 10, date(2026, 10, 5))]
        snapshots = [StockSnapshot("S1", "P1", 50, date(2026, 9, 30)), StockSnapshot("S1", "P1", 40, date(2026, 10, 31))]
        review = run(events, snapshots)
        codes = {issue.code for issue in review.cell("P1", "east").issues}
        self.assertIn("transfer-unbalanced", codes)


class SellInScope(unittest.TestCase):
    def test_dealer_only_scope_excludes_store_receipts(self):
        events = [
            Event("E1", "D1", "P1", Kind.RECEIPT, 100, date(2026, 10, 3)),
            Event("E2", "S1", "P1", Kind.RECEIPT, 40, date(2026, 10, 4)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 60, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 60, date(2026, 10, 31)),
            StockSnapshot("D1", "P1", 0, date(2026, 10, 31)),
        ]
        broad = run(events, snapshots, policy=PostingPolicy(sell_in_scope="all")).cell("P1", "east")
        narrow = run(events, snapshots, policy=PostingPolicy(sell_in_scope="dealer_only")).cell("P1", "east")
        self.assertEqual(broad.sell_in_net, 140)
        self.assertEqual(narrow.sell_in_net, 100)

    def test_non_restocked_returns_reduce_demand_but_not_stock(self):
        events = [Event("E1", "S1", "P1", Kind.RETURN, 5, date(2026, 10, 10))]
        snapshots = [
            StockSnapshot("S1", "P1", 50, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 50, date(2026, 10, 31)),
        ]
        restocked = run(events, snapshots, policy=PostingPolicy(returns_restock=True)).cell("P1", "east")
        scrapped = run(events, snapshots, policy=PostingPolicy(returns_restock=False)).cell("P1", "east")
        self.assertEqual(restocked.demand.low, 0)
        self.assertEqual(scrapped.demand.low, 0)  # bridge would be -5, floored at 0
        self.assertEqual(restocked.returns, 5)
        self.assertEqual(scrapped.returns, 5)
        # demand floor cannot go negative under either basis
        self.assertGreaterEqual(scrapped.demand.low, 0)


class CoverageAndLoading(unittest.TestCase):
    def _loading_case(self):
        events = [
            Event("E1", "D1", "P1", Kind.RECEIPT, 200, date(2026, 10, 2)),
            Event("E2", "S1", "P1", Kind.POS, 5, date(2026, 10, 10)),
            Event("E3", "S2", "P1", Kind.POS, 5, date(2026, 10, 11)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 100, date(2026, 9, 30)),
            StockSnapshot("S2", "P1", 100, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 95, date(2026, 10, 31)),
            StockSnapshot("S2", "P1", 95, date(2026, 10, 31)),
            StockSnapshot("D1", "P1", 200, date(2026, 10, 31)),
        ]
        return run(events, snapshots)

    def test_channel_loading_ratio_and_flag(self):
        cell = self._loading_case().cell("P1", "east")
        self.assertAlmostEqual(cell.sell_in_to_demand, 20.0)
        self.assertIn("channel-loading", {issue.code for issue in cell.issues})

    def test_ratio_suppressed_when_coverage_is_thin(self):
        events = [Event("E1", "D1", "P1", Kind.RECEIPT, 200, date(2026, 10, 2))]
        snapshots = [
            StockSnapshot("S1", "P1", 10, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 10, date(2026, 10, 31)),
        ]
        review = run(events, snapshots)
        cell = review.cell("P1", "east")
        self.assertEqual(cell.sell_in_to_demand, None)

    def test_store_without_assortment_is_not_a_missing_feed(self):
        events = [Event("E1", "S1", "P1", Kind.POS, 20, date(2026, 10, 10))]
        snapshots = [
            StockSnapshot("S1", "P1", 50, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 30, date(2026, 10, 31)),
        ]
        cell = run(events, snapshots).cell("P1", "east")
        self.assertEqual(cell.demand.low, 20)
        # S2 never stocks P1, so it is outside this SKU's distribution. Counting
        # it as a missing POS feed would inflate the denominator with a fake zero.
        self.assertEqual(cell.coverage.expected_stores, ("S1",))
        self.assertNotIn("S2", cell.coverage.missing)
        self.assertEqual(cell.sell_in_to_demand, None)  # no receipts, ratio meaningless

    def test_missing_closing_store_is_an_exhibit_not_a_zero(self):
        events = [
            Event("E1", "S1", "P1", Kind.POS, 10, date(2026, 10, 10)),
            Event("E2", "S2", "P1", Kind.POS, 10, date(2026, 10, 10)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 100, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 90, date(2026, 10, 31)),
            StockSnapshot("S2", "P1", 100, date(2026, 9, 30)),
        ]
        cell = run(events, snapshots).cell("P1", "east")
        self.assertEqual(cell.coverage.comparable_sales, ("S1",))
        self.assertIn("S2", cell.coverage.expected_stores)
        self.assertTrue(any(exhibit.subject == "S2" for exhibit in cell.coverage.exhibits))

    def test_stock_age_uses_fifo_receipts(self):
        events = [
            Event("E1", "D1", "P1", Kind.RECEIPT, 100, date(2026, 8, 1)),
            Event("E2", "D1", "P1", Kind.RECEIPT, 100, date(2026, 10, 20)),
            Event("E3", "S1", "P1", Kind.POS, 10, date(2026, 10, 25)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 50, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 40, date(2026, 10, 31)),
            StockSnapshot("D1", "P1", 200, date(2026, 10, 31)),
        ]
        cell = run(events, snapshots).cell("P1", "east")
        self.assertIsNotNone(cell.stock_age_days)
        self.assertGreaterEqual(cell.stock_age_days, 11)

    def test_prior_month_demand_uses_calendar_month(self):
        events = [
            Event("E1", "S1", "P1", Kind.POS, 10, date(2026, 9, 10)),
            Event("E2", "S1", "P1", Kind.POS, 20, date(2026, 10, 10)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 60, date(2026, 8, 31)),
            StockSnapshot("S1", "P1", 50, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 30, date(2026, 10, 31)),
        ]
        cell = run(events, snapshots).cell("P1", "east")
        self.assertEqual((cell.prior_demand.low, cell.prior_demand.high), (10, 10))
        self.assertAlmostEqual(cell.demand_growth, 1.0)


class Gates(unittest.TestCase):
    def test_unconfirmed_review_is_provisional(self):
        review = run([], [])
        self.assertTrue(review.provisional)
        self.assertFalse(review.confirmed)
        self.assertTrue(all(not gate.confirmed for gate in review.gates))

    def test_stale_confirmation_after_hierarchy_change(self):
        from channelops.model import Confirmation, Role
        from channelops.model import Store as StoreModel

        base = run([], [])
        fp = analytics.hierarchy_fingerprint(stores())
        confirmed = analytics.analyse(
            period=OCT,
            as_of=date(2026, 10, 31),
            stores=stores(),
            events=[],
            snapshots=[],
            scenario=Scenario(),
            policy=PostingPolicy(),
            confirmations=[
                Confirmation(
                    role=Role.CHANNEL_OPS,
                    subject="channel_ops",
                    decision="confirmed",
                    fingerprint=fp,
                    confirmed_at="2026-11-01T00:00:00+00:00",
                )
            ],
        )
        gate = confirmed.gate(Role.CHANNEL_OPS)
        self.assertTrue(gate.confirmed)
        changed = stores() + [StoreModel("S9", "D1", "east")]
        stale = analytics.analyse(
            period=OCT,
            as_of=date(2026, 10, 31),
            stores=changed,
            events=[],
            snapshots=[],
            scenario=Scenario(),
            policy=PostingPolicy(),
            confirmations=confirmed.gates and [
                Confirmation(
                    role=Role.CHANNEL_OPS,
                    subject="channel_ops",
                    decision="confirmed",
                    fingerprint=fp,
                    confirmed_at="2026-11-01T00:00:00+00:00",
                )
            ],
        )
        stale_gate = stale.gate(Role.CHANNEL_OPS)
        self.assertFalse(stale_gate.confirmed)
        self.assertTrue(stale_gate.stale)


if __name__ == "__main__":
    unittest.main()


class ReviewRegressions(unittest.TestCase):
    def test_dealer_only_sku_is_reported_not_silently_dropped(self):
        stores = [Store("S1", "D1", "east")]
        events = [Event("E1", "D1", "P9", Kind.RECEIPT, 1000, date(2026, 10, 5))]
        snapshots = [StockSnapshot("D1", "P9", 1000, date(2026, 10, 31))]
        review = analytics.analyse(
            period=OCT, as_of=date(2026, 10, 31), stores=stores,
            events=events, snapshots=snapshots, scenario=Scenario(),
            policy=PostingPolicy(),
        )
        self.assertEqual(review.cells, [])
        messages = [issue.message for issue in review.issues if issue.code == "dealer-only-sku"]
        self.assertTrue(any("P9" in message for message in messages))

    """Regression cases from an adversarial review of the engine."""

    def test_prior_demand_includes_floor_only_store(self):
        stores = [Store("S1", "D1", "east"), Store("S2", "D1", "east")]
        events = [
            Event("A1", "S1", "P1", Kind.POS, 10, date(2026, 9, 10)),
            Event("B1", "S1", "P1", Kind.POS, 10, date(2026, 10, 10)),
            Event("A2", "S2", "P1", Kind.POS, 100, date(2026, 9, 10)),
            Event("B2", "S2", "P1", Kind.POS, 100, date(2026, 10, 10)),
        ]
        snapshots = [
            StockSnapshot("S1", "P1", 20, date(2026, 8, 31)),
            StockSnapshot("S1", "P1", 10, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 10, date(2026, 10, 31)),
        ]
        cell = run(events, snapshots).cell("P1", "east")
        # S2 has only a POS floor (100) in both periods, so the region floor is
        # 100 current vs 110 prior - both include the floor-only store.  The bug
        # was the prior dropping S2 entirely (prior floor 10), which turned flat
        # demand into a ~+900% spike.  Growth must now be modest/absent, not a spike.
        self.assertEqual(cell.prior_demand.low, 110)
        self.assertEqual(cell.demand_growth, None)  # current is unbounded -> no ratio

    def test_shared_dealer_is_not_double_counted(self):
        stores = [Store("S1", "D1", "east"), Store("S3", "D1", "west")]
        events = [Event("E1", "D1", "P1", Kind.RECEIPT, 100, date(2026, 10, 5))]
        snapshots = [
            StockSnapshot("D1", "P1", 100, date(2026, 10, 31)),
            StockSnapshot("S1", "P1", 10, date(2026, 10, 31)),
            StockSnapshot("S3", "P1", 10, date(2026, 10, 31)),
        ]
        review = analytics.analyse(
            period=OCT,
            as_of=date(2026, 10, 31),
            stores=stores,
            events=events,
            snapshots=snapshots,
            scenario=Scenario(),
            policy=PostingPolicy(),
        )
        # The east cell owns D1; the west cell only owns its own store.  One
        # 100-unit receipt therefore appears exactly once across regions, and the
        # shared warehouse's stock is not counted in both.
        total_sell_in = sum(cell.sell_in_net for cell in review.cells)
        self.assertEqual(total_sell_in, 100)
        east = review.cell("P1", "east")
        west = review.cell("P1", "west")
        self.assertIn("D1", east.stock_known_locations)
        self.assertNotIn("D1", west.stock_known_locations)
        self.assertIn("shared-dealer", {issue.code for issue in review.issues})

    def test_stock_age_unknown_without_receipt_history(self):
        events = [Event("X", "S1", "P1", Kind.POS, 0, date(2026, 10, 10))]
        snapshots = [
            StockSnapshot("S1", "P1", 100, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 100, date(2026, 10, 31)),
        ]
        cell = run(events, snapshots).cell("P1", "east")
        # 100 units that predate the visible history must not be called 0 days old.
        self.assertIsNone(cell.stock_age_days)

    def test_cover_is_midpoint_not_worst_case(self):
        from channelops.analytics import _cover

        demand = analytics.__dict__["DemandRange"](1, 1000, "x")
        central, worst, best = _cover(1000, True, demand, OCT)
        self.assertAlmostEqual(central, 61.94, places=1)
        self.assertAlmostEqual(worst, 31.0, places=1)
        self.assertAlmostEqual(best, 31000.0, places=0)

    def test_prior_sell_in_respects_dealer_scope(self):
        events = [
            Event("A1", "D1", "P1", Kind.RECEIPT, 100, date(2026, 9, 5)),
            Event("A2", "S1", "P1", Kind.RECEIPT, 50, date(2026, 9, 6)),
            Event("B1", "D1", "P1", Kind.RECEIPT, 100, date(2026, 10, 5)),
        ]
        snapshots = [StockSnapshot("S1", "P1", 0, date(2026, 10, 31))]
        review = analytics.analyse(
            period=OCT,
            as_of=date(2026, 10, 31),
            stores=[Store("S1", "D1", "east")],
            events=events,
            snapshots=snapshots,
            scenario=Scenario(),
            policy=PostingPolicy(sell_in_scope="dealer_only"),
        )
        cell = review.cell("P1", "east")
        self.assertEqual(cell.sell_in_net, 100)
        self.assertEqual(cell.prior_sell_in, 100)
        self.assertEqual(cell.sell_in_growth, 0.0)

    def test_orphan_correction_is_not_applied(self):
        from channelops.analytics import applied_events

        base = [Event("E1", "S1", "P1", Kind.POS, 10, date(2026, 10, 10))]
        correction = Event(
            "C1", "S1", "P1", Kind.POS, 99, date(2026, 10, 10),
            corrected_of="E99", source="correction",
        )
        effective, replaced, orphaned = applied_events(base + [correction])
        self.assertEqual([e.qty for e in effective], [10])
        self.assertEqual([e.event for e in orphaned], ["C1"])

    def test_campaign_outside_period_has_no_lift(self):
        from channelops.model import Campaign, CampaignAttribution

        stores = [Store("S1", "D1", "east")]
        events = [Event("P1", "S1", "P1", Kind.POS, 10, date(2026, 10, 10))]
        snapshots = [
            StockSnapshot("S1", "P1", 50, date(2026, 9, 30)),
            StockSnapshot("S1", "P1", 40, date(2026, 10, 31)),
        ]
        from channelops.model import Confirmation, Role

        campaign = Campaign(
            campaign="OLD", region="east", sku="P1", store="S1",
            starts=date(2026, 8, 1), ends=date(2026, 8, 10), confirmed=True,
        )
        campaign_fp = analytics.campaign_fingerprint([campaign])
        review = analytics.analyse(
            period=OCT, as_of=date(2026, 10, 31), stores=stores,
            events=events, snapshots=snapshots, scenario=Scenario(),
            policy=PostingPolicy(), campaigns=[campaign],
            confirmations=[
                Confirmation(
                    role=Role.REGIONAL_MANAGER, subject="regional_manager",
                    decision="confirmed", fingerprint=campaign_fp,
                    confirmed_at="2026-11-01T00:00:00+00:00",
                )
            ],
        )
        attribution = review.attributions[0]
        self.assertIsNone(attribution.lift)
        self.assertIn("不在本期", attribution.evidence)


if __name__ == "__main__":
    unittest.main()
