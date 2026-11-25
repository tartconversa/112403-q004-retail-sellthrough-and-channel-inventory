"""Analytical engine: unit of analysis, demand evidence, coverage and bridges.

The single unit of analysis is ``(sku, region)``.  Sell-in, consumer demand,
stock age and cover are all computed on that same unit so they can be put in one
row and compared; a dealer shipment in April is never shown against store POS from
a different region or SKU.

Three rules drive the code:

1. **Consumer demand is net and comparable.**  Demand is POS minus consumer
   returns, summed over the stores with a trustworthy stock anchor.  A missing
   snapshot does not get an imputed number; it turns the sum into a one-sided
   interval and is listed as a pending-data exhibit.
2. **A transfer is stock movement, never a sale.**  Transfer-in/out only enter the
   stock identity.  They are excluded from sell-in and from consumer demand, and
   a transfer cannot create a second "receipt" for the receiving store.
3. **Evidence decides the answer.**  When both opening and closing stock are
   known, the stock bridge pins demand to a point.  With only scanner sales, the
   answer is the observed net POS as a floor.  It is a range, never a made-up
   point.

The analyzer is pure: it takes parsed records and returns a :class:`Review`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta

from .model import (
    Campaign,
    CampaignAttribution,
    Cell,
    Confirmation,
    Coverage,
    DemandRange,
    Event,
    Exhibit,
    FeedWindow,
    GateStatus,
    Issue,
    IssueSeverity,
    Kind,
    Period,
    PostingPolicy,
    Review,
    Role,
    Scenario,
    StockSnapshot,
    Store,
    StoreDemand,
)


# --------------------------------------------------------------------------- #
# Ledger helpers
# --------------------------------------------------------------------------- #


def applied_events(events: list[Event]) -> tuple[list[Event], list[Event], list[Event]]:
    """Return (effective, superseded, orphaned corrections).

    A correction is a replacement row naming the event it supersedes.  A
    correction whose target is missing would otherwise be applied *additively*,
    double-counting the original; such rows are withheld and reported instead.
    A row that both corrects another and is itself superseded is dropped by the
    chain, so the newest row in each chain wins.
    """
    ids = {event.event for event in events}
    superseded = {
        event.corrected_of
        for event in events
        if event.corrected_of and event.corrected_of in ids
    }
    orphaned = [
        event
        for event in events
        if event.corrected_of and event.corrected_of not in ids
    ]
    orphan_ids = {event.event for event in orphaned}
    effective = [
        event
        for event in events
        if event.event not in superseded and event.event not in orphan_ids
    ]
    replaced = [event for event in events if event.event in superseded]
    return effective, replaced, orphaned


def fingerprint(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def stock_effect(event: Event, policy: PostingPolicy) -> int:
    """Signed effect of an event on stock at its location.

    A consumer return only adds stock back when finance says it is restocked;
    otherwise it leaves the channel like a sale.
    """
    if event.kind is Kind.RECEIPT:
        return event.qty
    if event.kind is Kind.TRANSFER_IN:
        return event.qty
    if event.kind is Kind.TRANSFER_OUT:
        return -event.qty
    if event.kind is Kind.POS:
        return -event.qty
    if event.kind is Kind.RETURN:
        return event.qty if policy.returns_restock else 0
    return 0


# --------------------------------------------------------------------------- #
# Stock anchors
# --------------------------------------------------------------------------- #


def anchor(
    history: list[StockSnapshot],
    target: date,
    events: list[Event],
    policy: PostingPolicy,
    tolerance_days: int,
) -> tuple[int, date, bool] | None:
    """Stock at ``target`` from a snapshot at or before it.

    A snapshot within ``tolerance_days`` is rolled forward with the recorded
    events for that location/SKU; a staler snapshot is not silently treated as
    current - it yields ``None`` so the caller reports missing evidence.
    """
    candidates = [snapshot for snapshot in history if snapshot.day <= target]
    if not candidates:
        return None
    snap = max(candidates, key=lambda s: s.day)
    gap = (target - snap.day).days
    if gap > tolerance_days:
        return None
    if gap == 0:
        return snap.qty, snap.day, False
    moved = sum(
        stock_effect(event, policy)
        for event in events
        if event.sku == snap.sku
        and event.store == snap.location
        and snap.day < event.at <= target
    )
    return snap.qty + moved, snap.day, True


def _net_consumer(evidence: StoreDemand) -> int:
    return evidence.pos - evidence.returns


# --------------------------------------------------------------------------- #
# Per-store evidence
# --------------------------------------------------------------------------- #


def store_evidence(
    store: Store,
    sku: str,
    period: Period,
    as_of: date,
    events: list[Event],
    snapshots: list[StockSnapshot],
    scenario: Scenario,
    policy: PostingPolicy,
    pos_feeds: list[FeedWindow],
    *,
    target_end: date | None = None,
) -> StoreDemand:
    """Consumer demand evidence for one store/SKU in one period."""
    end = min(period.end, as_of)
    if target_end is not None:
        end = min(end, target_end)
    evidence = StoreDemand(store=store.store, sku=sku, region=store.region)
    history = sorted(
        [s for s in snapshots if s.location == store.store and s.sku == sku],
        key=lambda s: s.day,
    )
    in_period = [e for e in events if e.store == store.store and e.sku == sku and period.start <= e.at <= end]
    evidence.pos = sum(e.qty for e in in_period if e.kind is Kind.POS)
    evidence.returns = sum(e.qty for e in in_period if e.kind is Kind.RETURN)
    evidence.receipts = sum(e.qty for e in in_period if e.kind is Kind.RECEIPT)
    evidence.transfer_in = sum(e.qty for e in in_period if e.kind is Kind.TRANSFER_IN)
    evidence.transfer_out = sum(e.qty for e in in_period if e.kind is Kind.TRANSFER_OUT)
    evidence.net_pos = _net_consumer(evidence)

    pos_days = sorted({e.at for e in in_period if e.kind is Kind.POS})
    evidence.pos_days = len(pos_days)
    evidence.pos_last = pos_days[-1] if pos_days else None
    declared_feed = any(
        feed.covers(store.store, sku, period.start, end) for feed in pos_feeds if feed.feed == "pos"
    )
    # A feed declaration is evidence; otherwise require scans at both ends.
    evidence.pos_complete = declared_feed or bool(
        pos_days and pos_days[0] == period.start and pos_days[-1] == end
    )
    if pos_days and not evidence.pos_complete:
        # Measure the tail against the evaluation end, not wall-clock today: a
        # past period reviewed later must not look like a late-upload gap.
        tail_gap = (end - pos_days[-1]).days
        evidence.issues.append(
            Issue(
                code="pos-late",
                message=f"{store.store} 最后一笔扫码 {pos_days[-1]}，距评估日 {tail_gap} 天；可能为门店延迟上传",
                severity=IssueSeverity.INFO,
                scope=f"{sku}/{store.region}",
                subject=store.store,
            )
        )

    opening = anchor(history, period.start - timedelta(days=1), events, policy, scenario.anchor_tolerance_days)
    closing = anchor(history, end, events, policy, scenario.anchor_tolerance_days)
    if opening is not None:
        evidence.opening, evidence.opening_day, _ = opening
        evidence.opening_known = True
    if closing is not None:
        evidence.closing, evidence.closing_day, _ = closing
        evidence.closing_known = True

    # Demand is consumer net of returns.  Returns only reduce the stock outflow
    # when they are restocked; when they are scrapped they reduce demand alone.
    return_adjust = 0 if policy.returns_restock else evidence.returns

    if evidence.opening_known and evidence.closing_known:
        implied = (
            evidence.opening  # type: ignore[operator]
            + evidence.receipts
            + evidence.transfer_in
            - evidence.transfer_out
            - evidence.closing  # type: ignore[operator]
            - return_adjust
        )
        low = max(0, implied)
        evidence.low, evidence.high = low, low
        evidence.method = "stock-bridge"
        evidence.evidence = (
            f"期初 {evidence.opening}@{evidence.opening_day} + 收货 {evidence.receipts} "
            f"+ 调入 {evidence.transfer_in} - 调出 {evidence.transfer_out} "
            f"- 期末 {evidence.closing}@{evidence.closing_day} "
            + (f"- 未回库退货 {return_adjust} " if return_adjust else "")
            + f"= 动销 {low}"
        )
        # Reconcile the bridge against scanner sales whenever we have a basis
        # for "zero sales": either scans exist, or the feed is declared complete
        # (which covers the zero-scan, stock-moved case).  Without that basis,
        # absent scans are not evidence of zero demand and must not be flagged.
        if evidence.pos_days or evidence.pos_complete:
            gap = low - evidence.net_pos
            tolerance = max(
                scenario.pos_bridge_tolerance_units,
                int(round(abs(evidence.net_pos) * scenario.pos_bridge_tolerance_pct)),
            )
            if abs(gap) > tolerance:
                evidence.low = min(low, max(0, evidence.net_pos))
                evidence.high = max(low, evidence.net_pos)
                evidence.method = "stock-bridge-unreconciled"
                evidence.evidence += (
                    f"；与净扫码 {evidence.net_pos} 差 {gap:+d}（容差 {tolerance}），"
                    "库存与扫码未对平，动销按两者区间"
                )
                evidence.issues.append(
                    Issue(
                        code="bridge-gap",
                        message=f"{store.store}/{sku} 库存桥 {low} 与净扫码 {evidence.net_pos} 相差 {gap:+d}，"
                        "可能有漏记事件，动销按区间处理",
                        severity=IssueSeverity.WARNING,
                        scope=f"{sku}/{store.region}",
                        subject=store.store,
                    )
                )
    elif evidence.opening_known:
        ceiling = (
            evidence.opening  # type: ignore[operator]
            + evidence.receipts
            + evidence.transfer_in
            - evidence.transfer_out
            - return_adjust
        )
        evidence.low = evidence.net_pos
        evidence.high = max(evidence.net_pos, ceiling)
        evidence.method = "pos-plus-opening-bound"
        evidence.evidence = (
            f"仅有期初库存 {evidence.opening}@{evidence.opening_day}；净扫码 {evidence.net_pos} 为下限，"
            f"期末库存未知，按流入量给出上限 {evidence.high}"
        )
        evidence.issues.append(
            Issue(
                code="missing-closing-stock",
                message=f"{store.store}/{sku} 缺期末库存，动销按区间 [{evidence.low}, {evidence.high}]",
                severity=IssueSeverity.WARNING,
                scope=f"{sku}/{store.region}",
                subject=store.store,
            )
        )
    else:
        evidence.low = evidence.net_pos
        evidence.high = None
        evidence.method = "pos-floor-only" if evidence.net_pos or evidence.pos else "no-evidence"
        if evidence.closing_known:
            detail = f"仅有期末库存 {evidence.closing}@{evidence.closing_day}，期初未知，无法封顶"
            code = "missing-opening-stock"
        else:
            detail = "无期初/期末库存锚点"
            code = "no-stock-anchor"
        evidence.evidence = (
            f"{detail}；净扫码 {evidence.net_pos}（POS {evidence.pos} - 退货 {evidence.returns}）仅为下限"
        )
        evidence.issues.append(
            Issue(
                code=code,
                message=f"{store.store}/{sku} {detail}，动销只有下限，不能当作消费者需求点值",
                severity=IssueSeverity.BLOCKING,
                scope=f"{sku}/{store.region}",
                subject=store.store,
            )
        )
    evidence.comparable = evidence.high is not None and evidence.method != "no-evidence"
    return evidence


# --------------------------------------------------------------------------- #
# Dealer / warehouse ownership
# --------------------------------------------------------------------------- #


def dealer_region_map(stores: list[Store]) -> dict[str, str]:
    """Assign each dealer/warehouse node to exactly one region.

    A dealer can serve stores in more than one region.  Counting its receipt and
    snapshot in every child region would double-count one physical shipment.  The
    node is assigned to its most common region (ties broken by region name); the
    caller can surface the multi-region case with :func:`shared_dealers`.
    """
    votes: dict[str, list[str]] = defaultdict(list)
    for store in stores:
        if store.parent:
            votes[store.parent].append(store.region)
    assignment: dict[str, str] = {}
    for parent, regions in votes.items():
        counts: dict[str, int] = defaultdict(int)
        for region in regions:
            counts[region] += 1
        assignment[parent] = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return assignment


def shared_dealers(stores: list[Store]) -> dict[str, list[str]]:
    votes: dict[str, set[str]] = defaultdict(set)
    for store in stores:
        if store.parent:
            votes[store.parent].add(store.region)
    return {parent: sorted(regions) for parent, regions in votes.items() if len(regions) > 1}


# --------------------------------------------------------------------------- #
# One cell
# --------------------------------------------------------------------------- #


def _carrying_locations(
    sku: str,
    region: str,
    stores: list[Store],
    events: list[Event],
    snapshots: list[StockSnapshot],
) -> set[str]:
    """Locations that demonstrably carry the SKU.

    Evidence of carriage is any event or snapshot for the SKU at that location.
    A location with no trace is not treated as a missing feed; it is simply not
    part of that SKU's distribution.  All available history is used: a store that
    stocked the SKU last month and is missing its closing snapshot this month is
    a real gap, not an assortment change.
    """
    region_stores = [store for store in stores if store.region == region]
    candidates = {store.store for store in region_stores} | {
        store.parent for store in region_stores if store.parent
    }
    carrying: set[str] = set()
    for event in events:
        if event.sku == sku and event.store in candidates:
            carrying.add(event.store)
    for snapshot in snapshots:
        if snapshot.sku == sku and snapshot.location in candidates:
            carrying.add(snapshot.location)
    return carrying


def analyse_cell(
    sku: str,
    region: str,
    period: Period,
    as_of: date,
    stores: list[Store],
    events: list[Event],
    snapshots: list[StockSnapshot],
    scenario: Scenario,
    policy: PostingPolicy,
    pos_feeds: list[FeedWindow],
    stock_feeds: list[FeedWindow],
    prior: Period | None = None,
) -> Cell:
    region_stores = [store for store in stores if store.region == region]
    # Assortment is inferred from actual activity: a west store that never
    # stocks P4 must not make P1's inventory look incomplete by its absence.
    carrying = _carrying_locations(sku, region, stores, events, snapshots)
    region_stores = [store for store in region_stores if store.store in carrying]
    store_ids = {store.store for store in region_stores}
    # A dealer node belongs to exactly one region; assigning it by "any child in
    # this region" would count one receipt in every region it serves.
    dealer_owner = dealer_region_map(stores)
    parents = {
        store.parent
        for store in region_stores
        if store.parent and store.parent in carrying and dealer_owner.get(store.parent) == region
    }
    scoped = store_ids | parents

    # ---- sell-in ---------------------------------------------------------
    receipts = [
        e for e in events if e.kind is Kind.RECEIPT and e.sku == sku and e.store in scoped and period.contains(e.at)
    ]
    receipt_locations: dict[str, int] = defaultdict(int)
    for event in receipts:
        receipt_locations[event.store] += event.qty
    sell_in_gross = sum(event.qty for event in receipts)

    prior_receipts = None
    if prior is not None:
        prior_rows = [
            e
            for e in events
            if e.kind is Kind.RECEIPT and e.sku == sku and e.store in scoped and prior.contains(e.at)
        ]
        if policy.sell_in_scope == "dealer_only":
            prior_receipts = sum(e.qty for e in prior_rows if e.store in parents)
        else:
            prior_receipts = sum(e.qty for e in prior_rows)

    # Store-level receipts are store replenishment, not brand sell-in, unless
    # finance confirms the broader basis.
    if policy.sell_in_scope == "dealer_only":
        sell_in_net = sum(event.qty for event in receipts if event.store in parents)
    else:
        sell_in_net = sell_in_gross

    transfers = [
        e
        for e in events
        if e.sku == sku
        and e.store in store_ids
        and period.contains(e.at)
        and e.kind in (Kind.TRANSFER_IN, Kind.TRANSFER_OUT)
    ]
    transfer_in = sum(e.qty for e in transfers if e.kind is Kind.TRANSFER_IN)
    transfer_out = sum(e.qty for e in transfers if e.kind is Kind.TRANSFER_OUT)

    # ---- per-store consumer evidence -------------------------------------
    per_store = [
        store_evidence(store, sku, period, as_of, events, snapshots, scenario, policy, pos_feeds)
        for store in sorted(region_stores, key=lambda s: s.store)
    ]
    pos_total = sum(item.pos for item in per_store)
    returns = sum(item.returns for item in per_store)

    bounded = [item for item in per_store if item.high is not None]
    unbounded = [item for item in per_store if item.high is None and item.low > 0]
    low = sum(item.low for item in per_store if item.method != "no-evidence")
    if unbounded:
        high: int | None = None
    elif bounded:
        high = sum(item.high or 0 for item in bounded)
    else:
        high = None

    if bounded and not unbounded:
        method = "stock-bridge" if all(item.method == "stock-bridge" for item in bounded) else "bounded-pos"
    elif low > 0:
        method = "pos-floor-only"
    else:
        method = "no-evidence"
    demand = DemandRange(
        low=max(0, low),
        high=high,
        method=method,
        evidence=tuple(f"{item.store}:{item.method}" for item in per_store if item.method != "no-evidence"),
    )

    # ---- stock position (store + dealer/warehouse nodes) -----------------
    locations = sorted(
        scoped
        | {s.location for s in snapshots if s.sku == sku and s.location in scoped}
    )
    stock_by_location: dict[str, tuple[int, date, bool]] = {}
    for location in locations:
        history = [s for s in snapshots if s.location == location and s.sku == sku]
        found = anchor(history, period.end, events, policy, scenario.anchor_tolerance_days)
        if found is not None:
            stock_by_location[location] = found
    observed = tuple(sorted(stock_by_location))
    missing_locations = tuple(location for location in locations if location not in stock_by_location)
    stock_complete = bool(locations) and not missing_locations
    stock_end = sum(qty for qty, _, _ in stock_by_location.values()) if stock_by_location else None
    snapshot_days = [day for _, day, _ in stock_by_location.values()]
    snapshot_day = max(snapshot_days) if snapshot_days else None

    # A declared stock feed or complete per-location coverage justifies anchors.
    feed_missing = []
    for location in observed:
        covered = any(
            feed.covers(location, sku, period.start, period.end)
            for feed in stock_feeds
            if feed.feed == "stock"
        )
        rolled = stock_by_location[location][2]
        if not covered and rolled:
            feed_missing.append(location)

    # ---- stock age -------------------------------------------------------
    stock_age, oldest_age = _stock_age(
        sku=sku,
        period=period,
        scoped=scoped,
        events=events,
        observed=stock_by_location,
        stock_end=stock_end if stock_complete else None,
        snapshot_day=snapshot_day or period.end,
    )

    # ---- cover -----------------------------------------------------------
    coverage_days, cover_low, cover_high = _cover(
        stock=stock_end, complete=stock_complete, demand=demand, period=period
    )
    # Shelf cover uses store snapshots only.  The channel figure above includes
    # the regional warehouse, which is exactly where loaded stock hides; showing
    # both stops "cover looks fine" from being read as "the shelf is fine".
    store_stock = sum(
        stock_by_location[location][0] for location in store_ids if location in stock_by_location
    )
    store_complete = all(location in stock_by_location for location in store_ids)
    shelf_cover, _, _ = _cover(
        stock=store_stock if store_complete else None,
        complete=store_complete,
        demand=demand,
        period=period,
    )
    partial_cover, _, _ = _cover(
        stock=sum(qty for qty, _, _ in stock_by_location.values()) if observed else None,
        complete=False,
        demand=demand,
        period=period,
    )

    # ---- coverage and exhibits ------------------------------------------
    exhibits: list[Exhibit] = []
    for location in missing_locations:
        reason = "期末无库存快照"
        exhibits.append(Exhibit(subject=location, reason=reason, missing=("stock",), blocking=True))
    for location in feed_missing:
        exhibits.append(
            Exhibit(
                subject=location,
                reason="快照已按事件滚动到期末，但未声明库存数据源",
                missing=("stock-feed",),
                blocking=False,
            )
        )
    for item in per_store:
        if item.method == "no-evidence":
            exhibits.append(Exhibit(subject=item.store, reason="该期无扫码与库存", missing=("pos", "stock")))
        elif item.high is None:
            exhibits.append(
                Exhibit(subject=item.store, reason="无期初/期末库存锚点，仅净扫码下限", missing=("stock",), blocking=True)
            )
        elif not item.pos_complete:
            exhibits.append(
                Exhibit(subject=item.store, reason="POS 上传未覆盖整期，库存桥兜底", missing=("pos",), blocking=False)
            )
    for item in per_store:
        for issue in item.issues:
            if issue.code == "pos-late":
                exhibits.append(Exhibit(subject=item.store, reason=issue.message, missing=("pos",), blocking=False))
    # One exhibit per (subject, blocking): several checks can describe the same
    # gap, and repeating it makes one bad feed look like several problems.  The
    # first (most specific) reason wins; blocking gaps always come first.
    exhibits.sort(key=lambda exhibit: not exhibit.blocking)
    seen_exhibits: set[tuple[str, bool]] = set()
    unique_exhibits: list[Exhibit] = []
    for exhibit in exhibits:
        key = (exhibit.subject, exhibit.blocking)
        if key in seen_exhibits:
            continue
        seen_exhibits.add(key)
        unique_exhibits.append(exhibit)
    exhibits = unique_exhibits
    coverage = Coverage(
        sku=sku,
        region=region,
        expected_stores=tuple(sorted(store_ids)),
        # "Comparable" means the store has point evidence from a stock bridge; a
        # one-sided anchor yields a bound, not a number another store can be
        # compared with on equal footing.
        comparable_sales=tuple(sorted(item.store for item in per_store if item.method == "stock-bridge")),
        comparable_stock=observed,
        stock_locations_expected=tuple(locations),
        stock_locations_observed=observed,
        pos_days=max((item.pos_days for item in per_store), default=0),
        period_days=period.days,
        exhibits=exhibits,
        missing=tuple(sorted({need for exhibit in exhibits for need in exhibit.missing if exhibit.blocking})),
    )

    prior_demand = None
    if prior is not None:
        prior_store = [
            store_evidence(
                store, sku, prior, as_of, events, snapshots, scenario, policy, pos_feeds, target_end=period.start - timedelta(days=1)
            )
            for store in sorted(region_stores, key=lambda s: s.store)
        ]
        # Mirror the current-period aggregation exactly: the lower bound includes
        # every store with evidence (including floor-only stores), and the `high`
        # is None whenever any such store lacks a ceiling.  Summing only bounded
        # stores would drop a floor-only prior and inflate growth.
        p_observed = [item for item in prior_store if item.method != "no-evidence"]
        p_open = [item for item in p_observed if item.high is None]
        if p_observed:
            p_low = sum(item.low for item in p_observed)
            p_high = None if p_open else sum(item.high or 0 for item in p_observed)
            prior_demand = DemandRange(max(0, p_low), p_high, "prior-period")

    issues: list[Issue] = []
    if missing_locations:
        severity = IssueSeverity.BLOCKING if not observed else IssueSeverity.WARNING
        issues.append(
            Issue(
                code="stock-incomplete",
                message=f"{sku}/{region} 缺 {', '.join(missing_locations)} 的期末库存；"
                "覆盖天数按已知节点计算，不能当作全渠道库存",
                severity=severity,
                scope=f"{sku}/{region}",
                subject=",".join(missing_locations),
            )
        )
    if not bounded:
        issues.append(
            Issue(
                code="no-comparable-store",
                message=f"{sku}/{region} 没有同时具备期初期末库存的门店，动销只能是下限/区间",
                severity=IssueSeverity.BLOCKING,
                scope=f"{sku}/{region}",
            )
        )
    # A transfer needs a matching counter-entry; a one-sided transfer would move
    # stock without a source, so the reviewer is asked to confirm it.
    for event in transfers:
        counterpart = [
            other
            for other in events
            if other.sku == sku
            and other.at == event.at
            and other.qty == event.qty
            and other.kind
            is (Kind.TRANSFER_IN if event.kind is Kind.TRANSFER_OUT else Kind.TRANSFER_OUT)
        ]
        if not counterpart:
            issues.append(
                Issue(
                    code="transfer-unbalanced",
                    message=f"{event.event}：{sku} {event.store} {event.kind.value} {event.qty} 件无对应反向调拨，"
                    "请确认对方门店或改为退货/调整",
                    severity=IssueSeverity.WARNING,
                    scope=f"{sku}/{region}",
                    subject=event.event,
                )
            )

    return Cell(
        sku=sku,
        region=region,
        sell_in_gross=sell_in_gross,
        sell_in_net=sell_in_net,
        receipt_locations=dict(receipt_locations),
        transfer_in=transfer_in,
        transfer_out=transfer_out,
        pos=pos_total,
        returns=returns,
        demand=demand,
        prior_sell_in=prior_receipts,
        prior_demand=prior_demand,
        stock_end=stock_end,
        stock_known_locations=observed,
        stock_complete=stock_complete,
        stock_age_days=stock_age,
        oldest_stock_days=oldest_age,
        coverage_days=coverage_days,
        shelf_coverage_days=shelf_cover,
        coverage_days_partial=partial_cover,
        coverage_days_worst=cover_low,
        coverage_days_best=cover_high,
        coverage=coverage,
        stores=tuple(sorted(store_ids)),
        store_demand=tuple(per_store),
        issues=issues,
        missing_stock_locations=missing_locations,
        demand_window=(period.start, min(period.end, as_of)),
        stock_snapshot_day=snapshot_day,
        min_coverage_ratio=scenario.min_coverage_ratio,
    )


def _stock_age(
    sku: str,
    period: Period,
    scoped: set[str],
    events: list[Event],
    observed: dict[str, tuple[int, date, bool]],
    stock_end: int | None,
    snapshot_day: date,
) -> tuple[float | None, float | None]:
    """FIFO age of stock on hand at the snapshot day, in days.

    Only meaningful when every location has a snapshot; otherwise the omitted
    warehouse could hold the oldest goods.
    """
    if stock_end is None or stock_end <= 0:
        return None, None
    receipts = sorted(
        (
            e
            for e in events
            if e.kind is Kind.RECEIPT and e.sku == sku and e.store in scoped and e.at <= snapshot_day
        ),
        key=lambda e: e.at,
        reverse=True,
    )
    remaining = stock_end
    weighted = 0.0
    oldest: float | None = None
    for receipt in receipts:
        if remaining <= 0:
            break
        take = min(remaining, receipt.qty)
        age = max(0, (snapshot_day - receipt.at).days)
        weighted += take * age
        oldest = age if oldest is None else max(oldest, age)
        remaining -= take
    if remaining > 0:
        # Stock older than the visible receipt history.  Without an earlier lot
        # we cannot age it, but claiming 0 days would present unknown stock as
        # fresh.  Use the oldest visible lot as a floor when we have one; when we
        # have no receipt history at all, the age is unknowable.
        if oldest is None:
            return None, None
        weighted += remaining * oldest
    return weighted / stock_end, oldest


def _cover(
    stock: int | None, complete: bool, demand: DemandRange, period: Period
) -> tuple[float | None, float | None, float | None]:
    """Forward cover in days.

    Returns (central, low, high).  Demand low/high bound the daily run-rate, so
    cover is stock/(high/days) .. stock/(low/days).  An unbounded demand ceiling
    makes the central cover unknown; a zero floor makes the upper cover unknown.
    """
    if stock is None or stock < 0:
        return None, None, None
    if not complete:
        return None, None, None
    days = max(1, period.days)
    low = demand.low / days
    high = demand.high / days if demand.high is not None else None
    if high is None or high <= 0:
        return None, None, None
    # Demand is a range, so cover is a range.  The reported central value uses
    # the midpoint of the demand range - using the top of the range would report
    # the most pessimistic cover as if it were the estimate.
    midpoint = ((demand.low + demand.high) / 2) / days
    central = stock / midpoint if midpoint > 0 else None
    # worst = shortest cover (demand at the top of its range)
    # best  = longest cover (demand at the bottom of its range)
    worst = stock / high
    best = stock / low if low > 0 else None
    return central, worst, best


# --------------------------------------------------------------------------- #
# Campaigns
# --------------------------------------------------------------------------- #


def attribute_campaigns(
    cells: list[Cell],
    campaigns: list[Campaign],
    events: list[Event],
    period: Period,
    regional_confirmed: bool,
) -> list[CampaignAttribution]:
    """Attribute confirmed campaigns only; an unconfirmed scope is listed pending."""
    cell_by_key = {(cell.sku, cell.region): cell for cell in cells}
    for cell in cells:
        cell.pending_campaigns = tuple(
            sorted(
                c.campaign
                for c in campaigns
                if c.sku == cell.sku and c.region == cell.region and not (c.confirmed and regional_confirmed)
            )
        )

    attributions: list[CampaignAttribution] = []
    for campaign in campaigns:
        cell = cell_by_key.get((campaign.sku, campaign.region))
        if cell is None:
            continue
        in_scope = campaign.store in cell.stores if campaign.store else True
        campaign_pos = sum(
            e.qty
            for e in events
            if e.kind is Kind.POS
            and e.sku == campaign.sku
            and e.store == campaign.store
            and e.at is not None
            and campaign.covers(e.at)
            and period.contains(e.at)
        )
        baseline_pos = sum(
            e.qty
            for e in events
            if e.kind is Kind.POS
            and e.sku == campaign.sku
            and e.store == campaign.store
            and period.contains(e.at)
            and not campaign.covers(e.at)
        )
        confirmed = campaign.confirmed and regional_confirmed
        lift = None
        evidence = "活动范围未确认，不计算提升" if not confirmed else "无同期基线"
        overlaps = campaign.starts <= period.end and campaign.ends >= period.start
        if confirmed and in_scope and not overlaps:
            evidence = "活动不在本期，未参与归因"
        elif confirmed and in_scope:
            campaign_days = max(1, (min(campaign.ends, period.end) - max(campaign.starts, period.start)).days + 1)
            baseline_days = max(1, period.days - campaign_days)
            c_rate = campaign_pos / campaign_days
            b_rate = baseline_pos / baseline_days
            if b_rate > 0:
                lift = (c_rate - b_rate) / b_rate
                evidence = f"活动期日均 {c_rate:.1f} vs 非活动期日均 {b_rate:.1f}"
            else:
                evidence = "非活动期无扫码基线，不能归因"
        elif not in_scope:
            evidence = "活动门店不在该区域层级内"
        if confirmed and lift is not None:
            cell.campaign_lift[campaign.campaign] = lift
        attributions.append(
            CampaignAttribution(
                campaign=campaign.campaign,
                region=campaign.region,
                sku=campaign.sku,
                store=campaign.store,
                campaign_pos=campaign_pos,
                baseline_pos=baseline_pos,
                lift=lift,
                evidence=evidence,
                confirmed=confirmed,
                note=campaign.note,
            )
        )
    return attributions


# --------------------------------------------------------------------------- #
# Gate fingerprints
# --------------------------------------------------------------------------- #


def hierarchy_fingerprint(stores: list[Store]) -> str:
    return fingerprint([[s.store, s.parent, s.region, s.state] for s in stores])


def posting_fingerprint(policy: PostingPolicy, events: list[Event]) -> str:
    """Finance confirmation covers the posting basis and receipt/return rows."""
    return fingerprint(
        [policy.sell_in_scope, policy.returns_restock]
        + [
            [e.event, e.store, e.sku, e.kind.value, e.qty, e.at]
            for e in events
            if e.kind in (Kind.RECEIPT, Kind.RETURN)
        ]
    )


def campaign_fingerprint(campaigns: list[Campaign]) -> str:
    return fingerprint(
        [[c.campaign, c.region, c.sku, c.store, c.starts, c.ends, c.discount_pct] for c in campaigns]
    )


def dataset_fingerprint(events: list[Event], snapshots: list[StockSnapshot], feeds: list[FeedWindow]) -> str:
    return fingerprint(
        [[e.event, e.store, e.sku, e.kind.value, e.qty, e.at, e.corrected_of] for e in events]
        + [[s.location, s.sku, s.qty, s.day] for s in snapshots]
        + [[f.feed, f.location, f.sku, f.start, f.end] for f in feeds]
    )


# --------------------------------------------------------------------------- #
# Full review
# --------------------------------------------------------------------------- #


def analyse(
    period: Period,
    as_of: date,
    stores: list[Store],
    events: list[Event],
    snapshots: list[StockSnapshot],
    scenario: Scenario,
    policy: PostingPolicy,
    confirmations: list[Confirmation] | None = None,
    campaigns: list[Campaign] | None = None,
    pos_feeds: list[FeedWindow] | None = None,
    stock_feeds: list[FeedWindow] | None = None,
    include_prior: bool = True,
) -> Review:
    """Build a full review for every (sku, region) with any activity."""
    confirmations = confirmations or []
    campaigns = campaigns or []
    pos_feeds = pos_feeds or []
    stock_feeds = stock_feeds or []
    effective, replaced, orphaned = applied_events(events)

    skus = sorted({e.sku for e in effective} | {s.sku for s in snapshots})
    regions = sorted(
        {store.region for store in stores if store.state.lower() in ("active", "open", "")}
    )
    prior = period.prior() if include_prior else None

    cells: list[Cell] = []
    for sku in skus:
        for region in regions:
            if not _has_activity(sku, region, period, prior, stores, effective, snapshots):
                continue
            cells.append(
                analyse_cell(
                    sku,
                    region,
                    period,
                    as_of,
                    stores,
                    effective,
                    snapshots,
                    scenario,
                    policy,
                    pos_feeds,
                    stock_feeds,
                    prior=prior,
                )
            )
    gates = _gates(stores, effective, campaigns, policy, confirmations)
    regional_confirmed = next((gate.confirmed for gate in gates if gate.role is Role.REGIONAL_MANAGER), False)
    attributions = attribute_campaigns(cells, campaigns, effective, period, regional_confirmed)
    for cell in cells:
        cell.issues.extend(_cell_issues(stores, effective, cell))
    issues = _review_issues(stores, effective, period, cells)
    for event in orphaned:
        issues.append(
            Issue(
                code="orphan-correction",
                message=f"更正 {event.event} 指向不存在的 {event.corrected_of}，未计入；请核对更正对象",
                severity=IssueSeverity.BLOCKING,
                scope=event.sku,
                subject=event.event,
            )
        )
    for parent, regions in shared_dealers(stores).items():
        issues.append(
            Issue(
                code="shared-dealer",
                message=f"经销商/区域仓 {parent} 同时服务 {', '.join(regions)}；进货与库存已归属其中单一地区，"
                "避免同一笔收货在多个地区重复计入",
                severity=IssueSeverity.WARNING,
                scope=parent,
            )
        )

    dataset_fp = dataset_fingerprint(effective, snapshots, list(pos_feeds) + list(stock_feeds))
    hierarchy_fp = hierarchy_fingerprint(stores)
    campaign_fp = campaign_fingerprint(campaigns)
    policy_fp = fingerprint([policy.sell_in_scope, policy.returns_restock])
    return Review(
        period=period,
        as_of=as_of,
        scenario=scenario,
        posting_policy=policy,
        cells=cells,
        gates=gates,
        issues=issues,
        corrections=replaced,
        dataset_fingerprint=dataset_fp,
        hierarchy_fingerprint=hierarchy_fp,
        campaign_fingerprint=campaign_fp,
        policy_fingerprint=policy_fp,
        attributions=tuple(attributions),
    )


def _has_activity(
    sku: str,
    region: str,
    period: Period,
    prior: Period | None,
    stores: list[Store],
    events: list[Event],
    snapshots: list[StockSnapshot],
) -> bool:
    store_ids = {store.store for store in stores if store.region == region}
    parents = {store.parent for store in stores if store.region == region and store.parent}
    scoped = store_ids | parents
    # A dealer node alone cannot be a region row: the review compares sell-in to
    # *store* sell-through.  Require at least one store that carries the SKU,
    # otherwise a warehouse receipt with no downstream assortment yields an empty
    # "customer demand" row that means nothing.
    carrying = _carrying_locations(sku, region, stores, events, snapshots)
    if not (carrying & store_ids):
        return False
    for event in events:
        if event.sku == sku and event.store in scoped and (period.contains(event.at) or (prior and prior.contains(event.at))):
            return True
    for snapshot in snapshots:
        if snapshot.sku == sku and snapshot.location in scoped:
            return True
    return False


def _cell_issues(stores: list[Store], events: list[Event], cell: Cell) -> list[Issue]:
    issues: list[Issue] = []
    net = cell.transfer_in - cell.transfer_out
    if net:
        issues.append(
            Issue(
                code="transfer-net",
                message=f"{cell.sku}/{cell.region} 内部净调拨 {net:+d} 件，已从销售与进货中剔除，只影响库存",
                severity=IssueSeverity.INFO,
                scope=f"{cell.sku}/{cell.region}",
            )
        )
    if cell.sell_in_net > 0 and cell.demand.bounded and cell.sell_in_to_demand is not None and cell.sell_in_to_demand >= 1.5:
        issues.append(
            Issue(
                code="channel-loading",
                message=f"{cell.sku}/{cell.region} 进货 {cell.sell_in_net} 件 / 动销 {cell.demand.point:.0f} 件 "
                f"= {cell.sell_in_to_demand:.2f}，进货增长可能只是库存转移",
                severity=IssueSeverity.WARNING,
                scope=f"{cell.sku}/{cell.region}",
            )
        )
    return issues


def _gates(
    stores: list[Store],
    events: list[Event],
    campaigns: list[Campaign],
    policy: PostingPolicy,
    confirmations: list[Confirmation],
) -> list[GateStatus]:
    hierarchy_fp = hierarchy_fingerprint(stores)
    posting_fp = posting_fingerprint(policy, events)
    campaign_fp = campaign_fingerprint(campaigns)

    def status(role: Role, expected: str, detail: str, extra: object | None = None) -> GateStatus:
        relevant = [c for c in confirmations if c.role is role]
        if not relevant:
            return GateStatus(role=role, confirmed=False, stale=False, detail=detail)
        latest = sorted(relevant, key=lambda c: (c.confirmed_at, c.subject))[-1]
        stale = bool(latest.fingerprint and latest.fingerprint != expected)
        return GateStatus(
            role=role,
            confirmed=latest.decision == "confirmed" and not stale,
            stale=stale,
            detail=detail,
            confirmed_at=latest.confirmed_at,
            confirmed_by=str(latest.payload.get("by", "")) if latest.payload else "",
        )

    return [
        status(Role.CHANNEL_OPS, hierarchy_fp, "门店与经销层级关系"),
        status(Role.FINANCE, posting_fp, "签收与退货入账口径"),
        status(Role.REGIONAL_MANAGER, campaign_fp, "门店陈列与活动范围"),
    ]


def _review_issues(
    stores: list[Store],
    events: list[Event],
    period: Period,
    cells: list[Cell],
) -> list[Issue]:
    issues: list[Issue] = []
    store_ids = {store.store for store in stores}
    parents = {store.parent for store in stores if store.parent}
    feeding = parents | store_ids
    for event in events:
        if event.store not in feeding:
            issues.append(
                Issue(
                    code="unknown-location",
                    message=f"事件 {event.event} 的门店 {event.store} 不在层级表中，未计入任何区域",
                    severity=IssueSeverity.BLOCKING,
                    scope=event.sku,
                    subject=event.store,
                )
            )
    # Explain paired internal transfers once, so a reviewer reading a big stock
    # move does not mistake it for demand.
    paired: dict[tuple[str, str, int], list[Event]] = defaultdict(list)
    for event in events:
        if event.kind in (Kind.TRANSFER_IN, Kind.TRANSFER_OUT) and period.contains(event.at):
            paired[(event.sku, event.at.isoformat(), event.qty)].append(event)
    for (sku, day, qty), group in paired.items():
        if len({e.kind for e in group}) > 1:
            issues.append(
                Issue(
                    code="transfer-pair",
                    message=f"{sku} 在 {day} 有同量调入/调出 {qty} 件；按内部调拨处理，只移动库存，不计销售也不重复计进货",
                    severity=IssueSeverity.INFO,
                    scope=sku,
                )
            )
    # A dealer receipt for a SKU that never reaches a store cannot be compared to
    # consumer demand; report it instead of letting the shipment disappear.
    for event in events:
        if event.kind is not Kind.RECEIPT or not period.contains(event.at):
            continue
        if event.store not in parents:
            continue
        region = next(
            (store.region for store in stores if store.parent == event.store),
            None,
        )
        if region is None:
            continue
        store_ids_in_region = {store.store for store in stores if store.region == region}
        reaching = any(
            other.sku == event.sku and other.store in store_ids_in_region for other in events
        )
        if not reaching:
            issues.append(
                Issue(
                    code="dealer-only-sku",
                    message=f"{event.sku}/{region} 只有经销商收货与库存，没有任何门店记录；"
                    "无法计算消费者动销，该发货未纳入区域复盘行",
                    severity=IssueSeverity.WARNING,
                    scope=f"{event.sku}/{region}",
                    subject=event.store,
                )
            )
    return issues
