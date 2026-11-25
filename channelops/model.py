"""Domain model for offline-channel sell-through analysis.

All quantities are synthetic and expressed in pieces.  The model is deliberately
plain: CSV/JSON in, dataclasses out, so the analytical rules stay testable without
a server.

Evidence discipline is encoded in the types:

* a :class:`DemandRange` always names the method that produced it, and a missing
  stock anchor can only widen it - never collapse it to a point;
* a :class:`Cell` keeps sell-in, consumer demand and transfer flows in separate
  fields so a transfer can never masquerade as a sale or a second purchase;
* every confirmation records the fingerprint of the data it was made against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Iterable


class Kind(str, Enum):
    """Event families accepted by the ledger.

    Dealer-side receipts and consumer-side sales are kept apart on purpose: a
    dealer sell-in is a shipment commitment, not evidence of consumer demand.
    """

    RECEIPT = "receipt"          # dealer / regional-warehouse receipt (sell-in)
    POS = "pos"                  # store scan sale of consumer goods
    RETURN = "return"            # consumer return at a store
    TRANSFER_IN = "transfer-in"  # inter-store / warehouse -> store transfer, in
    TRANSFER_OUT = "transfer-out"


SELL_IN_KINDS = {Kind.RECEIPT}
CONSUMER_KINDS = {Kind.POS, Kind.RETURN}
TRANSFER_KINDS = {Kind.TRANSFER_IN, Kind.TRANSFER_OUT}
STORE_KINDS = {Kind.POS, Kind.RETURN}


class Role(str, Enum):
    """Roles that own a confirmation gate."""

    CHANNEL_OPS = "channel_ops"        # store + dealer hierarchy
    FINANCE = "finance"                # receipt / return posting basis
    REGIONAL_MANAGER = "regional_manager"  # display + campaign scope


class Tier(str, Enum):
    DEALER = "dealer"
    STORE = "store"


class IssueSeverity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.WARNING
    scope: str = ""
    subject: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity is IssueSeverity.BLOCKING


@dataclass(frozen=True)
class Store:
    store: str
    parent: str
    region: str
    state: str = "active"
    tier: str = "store"
    closed_at: date | None = None


@dataclass(frozen=True)
class StockSnapshot:
    location: str
    sku: str
    qty: int
    day: date


@dataclass(frozen=True)
class Event:
    event: str
    store: str
    sku: str
    kind: Kind
    qty: int
    at: date
    source: str = ""
    # A correction is a full replacement row; ``corrected_of`` names the event it
    # supersedes.  The ledger keeps the whole chain for audit but only applies the
    # latest correction in each chain.
    corrected_of: str = ""
    posted_at: date | None = None
    actor: str = ""
    note: str = ""

    @property
    def sign(self) -> int:
        """Direction of the effect on stock at ``store``."""
        if self.kind in (Kind.TRANSFER_OUT, Kind.POS):
            return -1
        return 1

    @property
    def is_correction(self) -> bool:
        return bool(self.corrected_of) or self.source == "correction"


@dataclass
class Orders:
    """Open orders and safety-stock promises a replenishment proposal must respect."""

    open_orders: dict[tuple[str, str], int] = field(default_factory=dict)   # (store, sku) -> qty
    commitments: dict[tuple[str, str], int] = field(default_factory=dict)  # issued sell-in commitments
    safety_stock: dict[tuple[str, str], int] = field(default_factory=dict)  # explicit safety stock

    def open_qty(self, store: str, sku: str) -> int:
        return self.open_orders.get((store, sku), 0)

    def committed_qty(self, store: str, sku: str) -> int:
        return self.commitments.get((store, sku), 0)

    def safety_qty(self, store: str, sku: str) -> int | None:
        return self.safety_stock.get((store, sku))


@dataclass(frozen=True)
class PostingPolicy:
    """Finance-owned posting basis for receipts and consumer returns.

    ``sell_in_scope`` decides whether a receipt at a store counts as channel
    sell-in (``all``) or only receipts at a dealer / regional-warehouse node do
    (``dealer_only``).  ``returns_restock`` decides whether a consumer return is
    put back on the shelf; when it is false the stock identity and the demand
    bridge differ, and the analyzer applies the confirmed basis consistently.
    """

    sell_in_scope: str = "all"
    returns_restock: bool = True

    def __post_init__(self) -> None:
        if self.sell_in_scope not in ("all", "dealer_only"):
            raise ValueError(f"unknown sell_in_scope {self.sell_in_scope!r}")


@dataclass(frozen=True)
class Confirmation:
    """An explicit sign-off by the role that owns the decision."""

    role: Role
    subject: str
    decision: str          # "confirmed", "rejected", "adjusted"
    fingerprint: str = ""
    note: str = ""
    confirmed_at: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class Exhibit:
    """A data gap or a store excluded from a comparison, with its reason."""

    subject: str
    reason: str
    missing: tuple[str, ...] = ()
    blocking: bool = False


@dataclass
class Coverage:
    """Data coverage for one (sku, region) cell."""

    sku: str
    region: str
    expected_stores: tuple[str, ...] = ()
    comparable_sales: tuple[str, ...] = ()
    comparable_stock: tuple[str, ...] = ()
    stock_locations_expected: tuple[str, ...] = ()
    stock_locations_observed: tuple[str, ...] = ()
    pos_days: int = 0
    period_days: int = 0
    exhibits: list[Exhibit] = field(default_factory=list)
    missing: tuple[str, ...] = ()

    @property
    def sales_ratio(self) -> float:
        if not self.expected_stores:
            return 0.0
        return len(self.comparable_sales) / len(self.expected_stores)

    @property
    def stock_complete(self) -> bool:
        return bool(self.stock_locations_expected) and not [
            exhibit for exhibit in self.exhibits if exhibit.blocking
        ]

    @property
    def stock_ratio(self) -> float:
        expected = len(self.stock_locations_expected)
        if expected == 0:
            return 0.0
        return len(self.stock_locations_observed) / expected

    @property
    def coverage_ratio(self) -> float:
        return self.sales_ratio

    @property
    def ratio(self) -> float:
        return self.sales_ratio

    @property
    def complete(self) -> bool:
        return bool(self.expected_stores) and not self.missing


@dataclass(frozen=True)
class DemandRange:
    """Consumer demand for a cell, with the evidence that bounds it.

    ``high`` is ``None`` when the evidence only supplies a lower bound: a
    one-sided interval is honest, whereas inventing a ceiling would not be.
    """

    low: int
    high: int | None
    method: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.low < 0:
            raise ValueError(f"negative demand lower bound {self.low}")
        if self.high is not None and self.high < self.low:
            raise ValueError(f"invalid demand range {self.low}..{self.high}")

    @property
    def bounded(self) -> bool:
        return self.high is not None

    @property
    def point(self) -> float:
        if self.high is None:
            return float(self.low)
        return (self.low + self.high) / 2

    @property
    def is_point(self) -> bool:
        return self.high is not None and self.low == self.high


@dataclass(frozen=True)
class FeedWindow:
    """A declared POS or stock feed coverage interval (inclusive)."""

    feed: str          # "pos" | "stock"
    location: str      # store / dealer node, "*" for any
    sku: str           # SKU, "*" for any
    start: date
    end: date

    def covers(self, location: str, sku: str, start: date, end: date) -> bool:
        return (
            self.location in ("*", location)
            and self.sku in ("*", sku)
            and self.start <= start
            and self.end >= end
        )


@dataclass
class StoreDemand:
    """Per-store consumer evidence, retained for the drill-down page."""

    store: str
    sku: str
    region: str
    pos: int = 0
    returns: int = 0
    net_pos: int = 0
    receipts: int = 0
    transfer_in: int = 0
    transfer_out: int = 0
    opening: int | None = None
    opening_day: date | None = None
    closing: int | None = None
    closing_day: date | None = None
    bridge: int | None = None
    pos_days: int = 0
    pos_last: date | None = None
    pos_complete: bool = False
    opening_known: bool = False
    closing_known: bool = False
    low: int = 0
    high: int | None = None
    method: str = "no-evidence"
    evidence: tuple[str, ...] = ()
    comparable: bool = False
    issues: list[Issue] = field(default_factory=list)

    @property
    def demand(self) -> DemandRange:
        return DemandRange(self.low, self.high, self.method, self.evidence)

    @property
    def bounded_demand(self) -> bool:
        return self.high is not None and self.method != "no-evidence"


@dataclass
class Cell:
    """One (sku, region) analytical cell."""

    sku: str
    region: str
    sell_in_gross: int
    sell_in_net: int
    receipt_locations: dict[str, int]
    transfer_in: int
    transfer_out: int
    pos: int
    returns: int
    demand: DemandRange
    prior_sell_in: int | None
    prior_demand: DemandRange | None
    stock_end: int | None
    stock_known_locations: tuple[str, ...]
    stock_complete: bool
    stock_age_days: float | None
    oldest_stock_days: float | None
    coverage_days: float | None
    shelf_coverage_days: float | None
    coverage_days_partial: float | None
    coverage_days_worst: float | None
    coverage_days_best: float | None
    coverage: Coverage
    target: int | None = None
    stores: tuple[str, ...] = ()
    store_demand: tuple[StoreDemand, ...] = ()
    issues: list[Issue] = field(default_factory=list)
    campaign_lift: dict[str, float] = field(default_factory=dict)
    pending_campaigns: tuple[str, ...] = ()
    demand_window: tuple[date, date] | None = None
    stock_snapshot_day: date | None = None
    missing_stock_locations: tuple[str, ...] = ()
    min_coverage_ratio: float = 0.5

    @property
    def sell_in_growth(self) -> float | None:
        if self.prior_sell_in is None or self.prior_sell_in == 0:
            return None
        return (self.sell_in_net - self.prior_sell_in) / self.prior_sell_in

    @property
    def demand_growth(self) -> float | None:
        """Demand growth, only when both periods have a bounded interval.

        Comparing a lower bound against a prior point (or vice versa) would
        manufacture growth from missing data, so a one-sided interval yields no
        growth figure rather than a misleading one.
        """
        if (
            self.prior_demand is None
            or not self.demand.bounded
            or not self.prior_demand.bounded
            or self.prior_demand.point == 0
        ):
            return None
        return (self.demand.point - self.prior_demand.point) / self.prior_demand.point

    @property
    def demand_reliable(self) -> bool:
        """Whether demand covers enough comparable stores to compare ratios."""
        return bool(
            self.coverage.expected_stores
            and self.coverage.ratio >= 0.0
            and self.demand.method != "no-evidence"
        )

    @property
    def ratio_reliable(self) -> bool:
        """Sell-in/demand ratios are only meaningful on comparable coverage."""
        return self.demand_reliable and self.coverage.sales_ratio >= 0.5

    @property
    def sell_in_to_demand(self) -> float | None:
        """Sell-in relative to evidence-backed consumer demand.

        Values far above 1 mean channel loading: the quarter may have hit its
        sell-in target while the goods still sit in a regional warehouse.  The
        ratio is suppressed when the comparable-store coverage is too thin,
        because the denominator would then cover fewer stores than the numerator.
        """
        if not self.ratio_reliable or self.demand.point <= 0 or self.sell_in_net <= 0:
            return None
        return self.sell_in_net / self.demand.point

    @property
    def stock_turn(self) -> float | None:
        if not self.stock_complete or self.stock_end is None or self.demand.point <= 0:
            return None
        return self.demand.point / self.stock_end

    @property
    def transfer_net(self) -> int:
        return self.transfer_in - self.transfer_out


@dataclass(frozen=True)
class Campaign:
    campaign: str
    region: str
    sku: str
    store: str
    starts: date
    ends: date
    discount_pct: float = 0.0
    confirmed: bool = False
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.campaign}|{self.region}|{self.sku}|{self.store}|{self.starts}|{self.ends}"

    def covers(self, day: date) -> bool:
        return self.starts <= day <= self.ends


@dataclass(frozen=True)
class CampaignAttribution:
    campaign: str
    region: str
    sku: str
    store: str
    campaign_pos: int
    baseline_pos: int
    lift: float | None
    evidence: str
    confirmed: bool
    note: str = ""


@dataclass
class Replenishment:
    store: str
    sku: str
    on_hand: int | None
    cover_days: float | None
    safety_stock: int
    safety_source: str
    open_order: int
    committed: int
    proposed: int
    target_cover_days: float
    status: str
    reasons: tuple[str, ...] = ()
    basis: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Proposal:
    """A replenishment proposal, possibly already issued to a dealer."""

    proposal_id: str
    store: str
    sku: str
    qty: int
    status: str            # draft | issued
    created_at: str
    updated_at: str
    issued_at: str = ""
    basis: dict = field(default_factory=dict)
    risk: tuple[str, ...] = ()
    change_note: str = ""


@dataclass
class PromotionDecision:
    region: str
    sku: str
    cover_days: float | None
    demand_growth: float | None
    sell_in_growth: float | None
    eligible: bool
    score: float
    reason: str
    confirmed_campaigns: tuple[str, ...] = ()
    pending_campaigns: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass
class Scenario:
    """Parameters a reviewer can tune; every run records them."""

    stock_high_cover_days: float = 45.0
    stock_low_cover_days: float = 14.0
    target_cover_days: float = 28.0
    default_safety_days: float = 7.0
    min_coverage_ratio: float = 0.5
    anchor_tolerance_days: int = 10
    pos_bridge_tolerance_pct: float = 0.05
    pos_bridge_tolerance_units: int = 1

    def validated(self) -> "Scenario":
        if self.stock_low_cover_days < 0:
            raise ValueError("低库存覆盖不能为负")
        if self.stock_low_cover_days >= self.stock_high_cover_days:
            raise ValueError("低库存覆盖必须小于高库存覆盖")
        if self.target_cover_days <= 0:
            raise ValueError("目标覆盖须为正")
        if self.default_safety_days < 0:
            raise ValueError("安全库存天数不能为负")
        if self.anchor_tolerance_days < 0:
            raise ValueError("快照容忍天数不能为负")
        if not 0 < self.min_coverage_ratio <= 1:
            raise ValueError("最低可比门店占比须在 (0, 1] 之间")
        return self


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def prior(self) -> "Period":
        """The comparison period.

        A whole-calendar-month period compares against the previous calendar
        month, so a 31-day October is not compared with a 31-day window that
        straddles August and September.  Any other range shifts by its own length.
        """
        from datetime import timedelta

        if self.start.day == 1 and (self.end + timedelta(days=1)).day == 1:
            prior_end = self.start - timedelta(days=1)
            prior_start = prior_end.replace(day=1)
            return Period(prior_start, prior_end)
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))


@dataclass
class GateStatus:
    role: Role
    confirmed: bool
    stale: bool
    detail: str
    confirmed_at: str = ""
    confirmed_by: str = ""


@dataclass
class Review:
    period: Period
    as_of: date
    scenario: Scenario
    posting_policy: PostingPolicy
    cells: list[Cell]
    gates: list[GateStatus]
    issues: list[Issue]
    corrections: list[Event]
    dataset_fingerprint: str
    hierarchy_fingerprint: str
    campaign_fingerprint: str = ""
    policy_fingerprint: str = ""
    attributions: tuple[CampaignAttribution, ...] = ()

    def gate(self, role: Role) -> GateStatus:
        for gate in self.gates:
            if gate.role is role:
                return gate
        raise KeyError(role)

    def cell(self, sku: str, region: str) -> Cell | None:
        for cell in self.cells:
            if cell.sku == sku and cell.region == region:
                return cell
        return None

    @property
    def provisional(self) -> bool:
        return any(not gate.confirmed or gate.stale for gate in self.gates)

    @property
    def blocking_issues(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.blocking]

    @property
    def confirmed(self) -> bool:
        return all(gate.confirmed and not gate.stale for gate in self.gates)


def dates_between(start: date, end: date) -> Iterable[date]:
    from datetime import timedelta

    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
