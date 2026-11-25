"""Application service: dataset loading, review assembly, proposals.

The web layer and the CLI both call through here, so the same rules apply whether
a reviewer uses the browser or a script.  The service owns no global state beyond
the data directory and the workspace directory it is constructed with.
"""

from __future__ import annotations

import copy
import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from . import analytics
from .csvio import (
    LoadError,
    load_campaigns,
    load_events,
    load_orders,
    load_stock,
    load_stores,
)
from .model import (
    Campaign,
    Event,
    FeedWindow,
    Issue,
    IssueSeverity,
    Kind,
    Orders,
    PostingPolicy,
    Proposal,
    Replenishment,
    Review,
    Role,
    Scenario,
    Store,
)
from .replenish import promotion_decisions, proposal_risk, recommend
from .state import StateError, Workspace, _now


DEFAULT_PERIOD_DAYS = 30
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = {".csv", ".json"}
CORE_FILES = ("stores.csv", "events.csv", "stock.csv")


@dataclass
class Dataset:
    stores: list[Store]
    events: list[Event]
    snapshots: list
    campaigns: list[Campaign]
    orders: Orders
    feeds: list[FeedWindow]
    issues: list[Issue]
    source: str


@dataclass
class Dashboard:
    review: Review
    recommendations: list[Replenishment]
    proposals: list[Proposal]
    promotions: list
    campaign_attr: list
    gates_ready: bool
    workspace_issues: list[Issue]
    data_issues: list[Issue]
    corrections: list[Event]

    def proposal_for(self, store: str, sku: str) -> Proposal | None:
        for proposal in self.proposals:
            if proposal.store == store and proposal.sku == sku:
                return proposal
        return None


def _default_period(events: list[Event], snapshots: list) -> tuple[date, date]:
    """Latest calendar month with activity, clipped to the last observed day.

    Clipping to the last event avoids presenting a whole month of "missing data"
    for days that have simply not happened yet.
    """
    days = [event.at for event in events] + [snapshot.day for snapshot in snapshots]
    if not days:
        today = date.today()
        return today.replace(day=1), today
    last = max(days)
    start = last.replace(day=1)
    return start, last


class Service:
    def __init__(self, data_dir: Path, workspace_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.workspace = Workspace(Path(workspace_dir))
        self._cache: tuple[tuple[float, ...], Dataset] | None = None

    # -- data --------------------------------------------------------------
    def _signature(self) -> tuple[float, ...]:
        files = sorted(self.data_dir.glob("*"))
        return tuple(
            sorted(
                (path.name, path.stat().st_mtime, path.stat().st_size)
                for path in files
                if path.is_file() and path.suffix in ALLOWED_UPLOAD_SUFFIXES
            )
        )

    def dataset(self, *, refresh: bool = False) -> Dataset:
        signature = self._signature()
        if self._cache is not None and self._cache[0] == signature and not refresh:
            return self._cache[1]
        issues: list[Issue] = []
        for name in CORE_FILES:
            if not (self.data_dir / name).exists():
                raise LoadError(f"数据目录缺少 {name}")
        stores, store_issues = load_stores(self.data_dir / "stores.csv")
        events, event_issues = load_events(self.data_dir / "events.csv")
        snapshots, stock_issues = load_stock(self.data_dir / "stock.csv")
        campaigns, campaign_issues = load_campaigns(self._optional("campaigns.json"))
        open_orders, commitments, safety, order_issues = load_orders(self._optional("orders.json"))
        issues.extend(store_issues + event_issues + stock_issues + campaign_issues + order_issues)
        feeds, feed_issues = self.workspace.load_feeds()
        # Dataset-level feed declarations may also arrive as feeds.json in the
        # data drop; merge both without letting one override the other.
        file_feeds, file_feed_issues = self._load_data_feeds()
        issues.extend(feed_issues + file_feed_issues)
        feeds = feeds + file_feeds
        dataset = Dataset(
            stores=stores,
            events=events,
            snapshots=snapshots,
            campaigns=campaigns,
            orders=Orders(open_orders=open_orders, commitments=commitments, safety_stock=safety),
            feeds=feeds,
            issues=issues,
            source=str(self.data_dir),
        )
        self._cache = (signature, dataset)
        return dataset

    def _optional(self, name: str) -> Path | None:
        path = self.data_dir / name
        return path if path.exists() else None

    def _load_data_feeds(self) -> tuple[list[FeedWindow], list[Issue]]:
        path = self._optional("feeds.json")
        if path is None:
            return [], []
        issues: list[Issue] = []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [], [
                Issue(
                    code="feeds-json",
                    message=f"feeds.json 无法解析（{exc}），POS 覆盖按扫码日期推断",
                    severity=IssueSeverity.WARNING,
                )
            ]
        feeds: list[FeedWindow] = []
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    continue
                try:
                    feeds.append(
                        FeedWindow(
                            feed=str(item["feed"]),
                            location=str(item.get("location", "*")),
                            sku=str(item.get("sku", "*")),
                            start=date.fromisoformat(str(item["start"])),
                            end=date.fromisoformat(str(item["end"])),
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    issues.append(
                        Issue(
                            code="feed-row",
                            message=f"feeds.json 第 {index + 1} 项不合法，已跳过",
                            severity=IssueSeverity.WARNING,
                        )
                    )
        else:
            issues.append(
                Issue(
                    code="feeds-shape",
                    message="feeds.json 应为数组，已忽略",
                    severity=IssueSeverity.WARNING,
                )
            )
        return feeds, issues

    # -- review ------------------------------------------------------------
    def review(
        self,
        period: tuple[date, date] | None = None,
        as_of: date | None = None,
        scenario: Scenario | None = None,
    ) -> Dashboard:
        dataset = self.dataset()
        scenario = scenario or self.workspace.load_scenario()
        policy = self.workspace.load_policy() or PostingPolicy()
        corrections = self.workspace.load_corrections()
        all_events = list(dataset.events) + corrections
        workspace_issues: list[Issue] = []
        if self.workspace.load_policy() is None:
            workspace_issues.append(
                Issue(
                    code="policy-default",
                    message="财务尚未确认入账口径，当前按默认口径试算（门店收货计入进货、退货回库）",
                    severity=IssueSeverity.WARNING,
                )
            )
        if period is None:
            period = _default_period(all_events, dataset.snapshots)
        period_start, period_end = period
        if period_end < period_start:
            raise ValueError("结束日期早于开始日期")
        as_of = as_of or max(period_end, max((e.at for e in all_events), default=period_end))
        confirmations = self.workspace.load_confirmations()
        review = analytics.analyse(
            period=analytics.Period(period_start, period_end),
            as_of=as_of,
            stores=dataset.stores,
            events=all_events,
            snapshots=dataset.snapshots,
            scenario=scenario,
            policy=policy,
            confirmations=confirmations,
            campaigns=dataset.campaigns,
            pos_feeds=[f for f in dataset.feeds if f.feed == "pos"],
            stock_feeds=[f for f in dataset.feeds if f.feed == "stock"],
        )
        review.issues.extend(dataset.issues)
        local_issues, recommendations, proposals = self._compute_proposals(
            review, dataset, scenario
        )
        workspace_issues.extend(local_issues)
        promotions = promotion_decisions(review, scenario)
        gates_ready = review.confirmed and not any(issue.blocking for issue in review.issues if issue.code.startswith("unknown"))
        return Dashboard(
            review=review,
            recommendations=recommendations,
            proposals=proposals,
            promotions=promotions,
            campaign_attr=list(review.attributions),
            gates_ready=gates_ready,
            workspace_issues=workspace_issues,
            data_issues=dataset.issues,
            corrections=corrections,
        )

    def _compute_proposals(
        self, review: Review, dataset: Dataset, scenario: Scenario
    ) -> tuple[list[Issue], list[Replenishment], list[Proposal]]:
        """Compute drafts and issued-commitment risk without writing state.

        Reads must be side-effect-free: a page view under the threaded server
        must never race an explicit issue.  Drafts are materialised through
        :meth:`persist_drafts` when the user acts on a recommendation.
        """
        issues: list[Issue] = []
        stored = self.workspace.load_proposals()
        stored_by_key = {(p.store, p.sku): p for p in stored}
        existing_issued = {
            (proposal.store, proposal.sku): proposal
            for proposal in stored
            if proposal.status == "issued"
        }
        # Issued orders are a fact of the channel: fold them into the Orders the
        # recommendation engine subtracts, so a draft can never re-order them.
        orders = copy.deepcopy(dataset.orders)
        for (store, sku), proposal in existing_issued.items():
            # The drop's commitments.json may or may not already include a promise
            # issued through this app.  Taking the larger figure avoids counting the
            # same promise twice, which would silently under-order.
            orders.commitments[(store, sku)] = max(orders.committed_qty(store, sku), proposal.qty)
        recommendations = recommend(review, dataset.stores, orders, scenario)

        recommendation_by_key = {(item.store, item.sku): item for item in recommendations}
        now = _now()
        computed: list[Proposal] = []
        for recommendation in recommendations:
            key = (recommendation.store, recommendation.sku)
            if key in existing_issued:
                continue
            previous = stored_by_key.get(key)
            if recommendation.status == "needs-data":
                qty = 0
                note = "待补数据，暂不出数；补报后按新证据重算"
            else:
                qty = recommendation.proposed
                note = (
                    "补报/更正后已按新证据重算"
                    if previous and previous.basis and previous.basis != recommendation.basis
                    else (previous.change_note if previous else "首次生成")
                )
            computed.append(
                Proposal(
                    proposal_id=previous.proposal_id if previous else f"R-{recommendation.store}-{recommendation.sku}",
                    store=recommendation.store,
                    sku=recommendation.sku,
                    qty=qty,
                    status="draft",
                    created_at=previous.created_at if previous else now,
                    updated_at=now,
                    basis=recommendation.basis,
                    risk=(),
                    change_note=note,
                )
            )
        live_keys = set(recommendation_by_key)
        # A stored draft whose store/SKU left the evidence set is shown at zero,
        # labelled, rather than silently dropping out of the list.
        for key, previous in stored_by_key.items():
            if previous.status == "draft" and key not in live_keys:
                computed.append(
                    Proposal(
                        proposal_id=previous.proposal_id,
                        store=previous.store,
                        sku=previous.sku,
                        qty=0,
                        status="draft",
                        created_at=previous.created_at,
                        updated_at=now,
                        basis=previous.basis,
                        risk=(),
                        change_note="该门店/SKU 已不在当前复盘范围，草稿清零待复核",
                    )
                )
        issued = [proposal for proposal in stored if proposal.status == "issued"]
        risks: dict[str, tuple[str, ...]] = {}
        refreshed_issued: list[Proposal] = []
        for proposal in issued:
            current = recommendation_by_key.get((proposal.store, proposal.sku))
            resolution = proposal_risk(proposal, current)
            risks[proposal.proposal_id] = resolution
            refreshed_issued.append(
                replace(proposal, risk=resolution) if resolution != proposal.risk else proposal
            )
            if resolution:
                issues.append(
                    Issue(
                        code="issued-risk",
                        message=f"已发承诺 {proposal.proposal_id}（{proposal.qty} 件）在新证据下存在风险："
                        + "；".join(resolution),
                        severity=IssueSeverity.WARNING,
                        scope=proposal.sku,
                        subject=proposal.store,
                    )
                )
        proposals = sorted(computed + refreshed_issued, key=lambda p: (p.status, p.store, p.sku))
        return issues, recommendations, proposals

    def persist_drafts(self, proposals: list[Proposal]) -> None:
        """Materialise computed drafts after an explicit action.

        Issued rows are re-read inside the workspace lock, so a page render that
        started earlier can never overwrite a commitment issued in between.
        """
        self.workspace.persist_drafts(proposals)

    # -- mutations ---------------------------------------------------------
    def default_period(self) -> tuple[date, date]:
        """Latest period with activity, without running the full analysis."""
        dataset = self.dataset()
        events = list(dataset.events) + self.workspace.load_corrections()
        return _default_period(events, dataset.snapshots)

    def confirm(self, role: Role, fingerprint: str, by: str, decision: str = "confirmed", note: str = "") -> None:
        if role is Role.FINANCE and decision == "confirmed":
            if self.workspace.load_policy() is None:
                raise StateError("财务确认前需先保存签收与退货入账口径")
        self.workspace.confirm(role, decision, fingerprint, by, note)
        self._invalidate()

    def save_policy(self, policy: PostingPolicy, by: str) -> None:
        self.workspace.save_policy(policy, by)
        self._invalidate()

    def save_scenario(self, scenario: Scenario, by: str) -> None:
        self.workspace.save_scenario(scenario.validated(), by)

    def add_correction(self, event: Event, period: tuple[date, date] | None = None) -> None:
        self.workspace.append_correction(event)
        self._materialize_drafts(period)
        self._invalidate()

    def issue_proposal(
        self,
        proposal_id: str,
        by: str,
        note: str = "",
        period: tuple[date, date] | None = None,
    ) -> None:
        # Make sure the draft the user is acting on reflects current evidence for
        # the same period the page showed, then issue it under the lock.
        self._materialize_drafts(period)
        self.workspace.issue(proposal_id, by, note)
        self._invalidate()

    def _materialize_drafts(self, period: tuple[date, date] | None = None) -> None:
        """Persist the current draft set after an explicit mutation.

        Kept separate from :meth:`review` so page renders stay read-only.
        """
        dashboard = self.review(period)
        drafts = [proposal for proposal in dashboard.proposals if proposal.status == "draft"]
        self.workspace.persist_drafts(drafts)

    def _invalidate(self) -> None:
        # The dataset cache keys on file mtimes, so workspace edits (which do not
        # touch the data drop) need no cache eviction; the next review rebuilds.
        pass

    # -- corrections from request text ------------------------------------
    def parse_correction(
        self,
        *,
        target: str,
        kind: str,
        qty: str,
        at: str,
        store: str,
        sku: str,
        note: str,
        actor: str,
        correction_id: str = "",
    ) -> Event:
        from .csvio import parse_int, safe_text, valid_ident

        issues: list[Issue] = []
        try:
            kind_value = Kind(kind)
        except ValueError as exc:
            raise StateError(f"未知事件类型 {kind!r}") from exc
        if not valid_ident(store) or not valid_ident(sku):
            raise StateError("门店或 SKU 标识不合法")
        if target and not valid_ident(target):
            raise StateError("被更正事件标识不合法")
        quantity = parse_int(qty, "qty", 0, issues)
        if quantity is None:
            raise StateError("数量必须是非负整数")
        try:
            day = date.fromisoformat(at)
        except ValueError as exc:
            raise StateError("日期必须是 YYYY-MM-DD") from exc
        ident = correction_id.strip() or f"C-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if not valid_ident(ident):
            raise StateError("更正编号不合法")
        return Event(
            event=ident,
            store=store,
            sku=sku,
            kind=kind_value,
            qty=quantity,
            at=day,
            source="correction",
            corrected_of=target.strip(),
            posted_at=date.today(),
            actor=safe_text(actor) or "unknown",
            note=safe_text(note),
        )

    # -- uploads -----------------------------------------------------------
    def install_upload(self, filename: str, blob: bytes) -> None:
        """Install one CSV/JSON drop atomically into the data directory.

        Only the six known filenames are accepted, so a crafted name cannot escape
        the data directory; content is size-capped and parsed by the caller.
        """
        name = Path(filename).name
        if name not in CORE_FILES + ("campaigns.json", "orders.json", "feeds.json"):
            raise StateError(f"不接受文件 {name}；只接受 {', '.join(CORE_FILES)}、campaigns.json、orders.json、feeds.json")
        if len(blob) > MAX_UPLOAD_BYTES:
            raise StateError("文件过大（上限 8 MB）")
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StateError("文件必须是 UTF-8 编码") from exc
        # Parse before installing: a broken drop must not poison the live dataset.
        self._validate_drop(name, text)
        temp = self.data_dir / f".{name}.new"
        temp.write_text(text, encoding="utf-8", newline="")
        target = self.data_dir / name
        temp.replace(target)
        self._cache = None

    def _validate_drop(self, name: str, text: str) -> None:
        import csv as csv_module

        from .csvio import EVENTS_HEADERS, STOCK_HEADERS, STORES_HEADERS

        if name == "stores.csv":
            self._check_csv(text, STORES_HEADERS)
        elif name == "events.csv":
            self._check_csv(text, EVENTS_HEADERS)
        elif name == "stock.csv":
            self._check_csv(text, STOCK_HEADERS)
        else:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise StateError(f"{name} 不是合法 JSON：{exc}") from exc
            if not isinstance(payload, (list, dict)):
                raise StateError(f"{name} 顶层应为数组或对象")

    @staticmethod
    def _check_csv(text: str, headers: list[str]) -> None:
        import csv as csv_module

        reader = csv_module.reader(io.StringIO(text), strict=True)
        try:
            first = next(reader)
        except StopIteration as exc:
            raise StateError("CSV 为空") from exc
        except csv_module.Error as exc:
            raise StateError(f"CSV 格式错误：{exc}") from exc
        if first != headers:
            raise StateError(f"表头应为 {','.join(headers)}，实际为 {','.join(first)}")

    def snapshot_bundle(self) -> bytes:
        """Return the data drop as a zip for offline review (no secrets inside)."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in CORE_FILES + ("campaigns.json", "orders.json", "feeds.json"):
                path = self.data_dir / name
                if path.exists():
                    archive.write(path, arcname=name)
        return buffer.getvalue()

    # -- helpers -----------------------------------------------------------
    def gate_fingerprints(self, dashboard: Dashboard | None = None) -> dict[Role, str]:
        dataset = self.dataset()
        corrections = self.workspace.load_corrections()
        effective, _, _ = analytics.applied_events(dataset.events + corrections)
        return {
            Role.CHANNEL_OPS: analytics.hierarchy_fingerprint(dataset.stores),
            Role.FINANCE: analytics.posting_fingerprint(
                self.workspace.load_policy() or PostingPolicy(), effective
            ),
            Role.REGIONAL_MANAGER: analytics.campaign_fingerprint(dataset.campaigns),
        }

    def csv_export(self, dashboard: Dashboard) -> str:
        """One row per (sku, region) with evidence and pending-data columns."""
        import csv as csv_module

        buffer = io.StringIO()
        writer = csv_module.writer(buffer)
        writer.writerow(
            [
                "sku",
                "region",
                "sell_in_net",
                "prior_sell_in",
                "pos",
                "returns",
                "demand_low",
                "demand_high",
                "demand_method",
                "transfer_in",
                "transfer_out",
                "stock_end",
                "stock_complete",
                "stock_age_days",
                "coverage_days",
                "coverage_days_worst",
                "coverage_days_best",
                "sell_in_to_demand",
                "comparable_stores",
                "pending_data",
                "flags",
            ]
        )
        for cell in dashboard.review.cells:
            writer.writerow(
                [
                    cell.sku,
                    cell.region,
                    cell.sell_in_net,
                    "" if cell.prior_sell_in is None else cell.prior_sell_in,
                    cell.pos,
                    cell.returns,
                    cell.demand.low,
                    "" if cell.demand.high is None else cell.demand.high,
                    cell.demand.method,
                    cell.transfer_in,
                    cell.transfer_out,
                    "" if cell.stock_end is None else cell.stock_end,
                    "yes" if cell.stock_complete else "no",
                    "" if cell.stock_age_days is None else f"{cell.stock_age_days:.1f}",
                    "" if cell.coverage_days is None else f"{cell.coverage_days:.1f}",
                    "" if cell.coverage_days_worst is None else f"{cell.coverage_days_worst:.1f}",
                    "" if cell.coverage_days_best is None else f"{cell.coverage_days_best:.1f}",
                    "" if cell.sell_in_to_demand is None else f"{cell.sell_in_to_demand:.2f}",
                    "|".join(cell.coverage.comparable_sales),
                    "|".join(cell.coverage.missing),
                    "|".join(sorted({issue.code for issue in cell.issues})),
                ]
            )
        return buffer.getvalue()
