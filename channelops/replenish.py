"""Replenishment and promotion allocation rules.

Two hard constraints from the operating model:

* a proposal must subtract stock already on order (open orders) *and* quantity
  already committed to the dealer, so a recompute can never double-order;
* a high-stock region must not win promotion resources merely because its sell-in
  grew.  Promotion eligibility is driven by evidence-backed demand and stock
  cover, and sell-in growth is explicitly not a scoring input.

Draft proposals are recomputed freely.  Issued proposals are never rewritten:
their quantity is frozen and any divergence from the new evidence is recorded as
a risk reason instead.
"""

from __future__ import annotations

from datetime import date

from .model import (
    Orders,
    PromotionDecision,
    Proposal,
    Replenishment,
    Review,
    Scenario,
    Store,
    StoreDemand,
)

NO_DATA = "待补数据"


def _demand_high(item: StoreDemand) -> int | None:
    return item.high if item.method != "no-evidence" else None


def store_run_rate(item: StoreDemand, period_days: int) -> float | None:
    high = _demand_high(item)
    if high is None:
        return None
    return high / max(1, period_days)


def recommend(
    review: Review,
    stores: list[Store],
    orders: Orders,
    scenario: Scenario | None = None,
) -> list[Replenishment]:
    """Compute one replenishment recommendation per store and SKU."""
    scenario = scenario or review.scenario
    store_by_id = {store.store: store for store in stores}
    results: list[Replenishment] = []
    for cell in review.cells:
        days = max(1, (cell.demand_window[1] - cell.demand_window[0]).days + 1) if cell.demand_window else review.period.days
        for item in cell.store_demand:
            store = store_by_id.get(item.store)
            if store is None or store.state.lower() not in ("active", "open", ""):
                continue
            rate = store_run_rate(item, days)
            reasons: list[str] = []
            if rate is None:
                reasons.append("门店动销只有下限或无证据，无法估算安全库存与目标覆盖")

            explicit = orders.safety_qty(item.store, cell.sku)
            if explicit is not None:
                safety = explicit
                safety_source = "订单资料"
            elif rate is not None:
                safety = int(round(rate * scenario.default_safety_days))
                safety_source = f"按 {scenario.default_safety_days:g} 天动销推算"
            else:
                safety = 0
                safety_source = NO_DATA

            # On-hand: the store's own closing snapshot, rolled forward.  We read
            # it from the per-store evidence bridge rather than the region total:
            # a regional warehouse is not shelf stock.
            on_hand = item.closing
            open_order = orders.open_qty(item.store, cell.sku)
            committed = orders.committed_qty(item.store, cell.sku)

            status = "ok"
            proposed = 0
            if on_hand is None:
                status = "needs-data"
                reasons.append("缺门店期末库存快照，不能下补货数量")
            elif rate is None:
                status = "needs-data"
            else:
                target_units = rate * scenario.target_cover_days
                need = target_units + safety - on_hand - open_order - committed
                proposed = max(0, int(round(need)))
                if proposed == 0:
                    status = "ok"
                    reasons.append(
                        f"现有 {on_hand} 件 + 在途 {open_order} + 已承诺 {committed} 已覆盖目标 "
                        f"{target_units:.0f} 件与安全库存 {safety} 件"
                    )
                else:
                    status = "propose"
                    reasons.append(
                        f"目标 {target_units:.0f} 件 + 安全库存 {safety} 件 - 现库存 {on_hand} "
                        f"- 在途 {open_order} - 已承诺 {committed} = 建议 {proposed} 件"
                    )
            if committed:
                reasons.append(f"已向经销商承诺 {committed} 件，本建议未重复计入")
            if open_order:
                reasons.append(f"在途未到货订单 {open_order} 件已扣减")

            results.append(
                Replenishment(
                    store=item.store,
                    sku=cell.sku,
                    on_hand=on_hand,
                    cover_days=(
                        on_hand / rate if on_hand is not None and rate and rate > 0 else None
                    ),
                    safety_stock=safety,
                    safety_source=safety_source,
                    open_order=open_order,
                    committed=committed,
                    proposed=proposed,
                    target_cover_days=scenario.target_cover_days,
                    status=status,
                    reasons=tuple(reasons),
                    basis={
                        "demand_low": item.low,
                        "demand_high": item.high,
                        "demand_method": item.method,
                        "run_rate": round(rate, 4) if rate is not None else None,
                        "stock": on_hand,
                        "stock_day": str(item.closing_day) if item.closing_day else "",
                        "coverage": cell.coverage_days,
                        "sell_in": cell.sell_in_net,
                    },
                )
            )
    return results


def promotion_decisions(review: Review, scenario: Scenario | None = None) -> list[PromotionDecision]:
    """Rank regions for promotion resources.

    Eligibility requires *stock cover inside the target band* and evidence-backed
    demand.  Sell-in growth appears in the reason text but never in the score.
    """
    scenario = scenario or review.scenario
    decisions: list[PromotionDecision] = []
    for cell in review.cells:
        growth = cell.demand_growth
        cover = cell.coverage_days
        evidence = list(cell.demand.evidence[:2])
        reasons: list[str] = []
        eligible = True

        if cell.stock_end is None or not cell.stock_complete:
            eligible = False
            reasons.append("库存不完整，无法判断是否压货，暂不分配促销资源")
        if cover is None:
            eligible = False
            reasons.append("覆盖天数不可计算")
        # Use the worst-case (shortest) cover for the high-stock gate: if even the
        # pessimistic estimate says the shelf is over-stocked, added promotion
        # spend would amplify loading.  This keeps the rule conservative when
        # demand itself is uncertain.
        pessimistic = cell.coverage_days_worst if cell.coverage_days_worst is not None else cover
        if cover is not None and pessimistic > scenario.stock_high_cover_days:
            eligible = False
            reasons.append(
                f"覆盖 {pessimistic:.0f} 天，高于 {scenario.stock_high_cover_days:g} 天，库存偏高，"
                "不因进货额增长获得促销资源"
            )
        elif cover is not None and cover < scenario.stock_low_cover_days:
            reasons.append(f"覆盖 {cover:.0f} 天偏低，促销可能加速缺货，需先补货")
            eligible = False
        if growth is None:
            eligible = False
            reasons.append("无同期可比动销，无法证明需求增长")
        elif growth <= 0:
            reasons.append("消费者净购买未增长，不增加促销资源")
            eligible = False

        # Score: demand growth only, damped for regions with thin coverage.
        if eligible and growth is not None:
            confidence = min(1.0, cell.coverage.sales_ratio)
            score = round(growth * confidence * 100, 1)
            reasons.append(
                f"可比较门店 {len(cell.coverage.comparable_sales)}/{len(cell.coverage.expected_stores)}，"
                f"动销增长 {growth:+.0%} 计分"
            )
            if cell.sell_in_growth is not None:
                reasons.append(
                    f"进货额增长 {cell.sell_in_growth:+.0%} 仅供参考，不计入促销评分"
                )
        else:
            score = 0.0

        decisions.append(
            PromotionDecision(
                region=cell.region,
                sku=cell.sku,
                cover_days=cover,
                demand_growth=growth,
                sell_in_growth=cell.sell_in_growth,
                eligible=eligible,
                score=score,
                reason="；".join(reasons) or "无数据",
                confirmed_campaigns=tuple(sorted(cell.campaign_lift)),
                pending_campaigns=cell.pending_campaigns,
                evidence=tuple(evidence),
            )
        )
    decisions.sort(key=lambda decision: (-decision.eligible, -decision.score, decision.region))
    return decisions


def proposal_risk(proposal: Proposal, recommendation: Replenishment | None) -> tuple[str, ...]:
    """Reasons an issued commitment now looks wrong.

    Issued quantities are frozen, so the only honest output is the risk list.
    """
    reasons: list[str] = []
    basis = proposal.basis or {}
    if recommendation is None:
        return ("当前口径下该门店/SKU 已无可比动销或库存证据，无法复核已发承诺",)
    current = recommendation.basis or {}
    if recommendation.status == "needs-data":
        reasons.append("补报后仍缺门店库存，已发承诺无法复核")
    if basis.get("demand_low") not in (None, current.get("demand_low")) or basis.get(
        "demand_high"
    ) != current.get("demand_high"):
        reasons.append(
            f"动销区间由 {basis.get('demand_low')}..{basis.get('demand_high')} "
            f"变为 {current.get('demand_low')}..{current.get('demand_high')}"
        )
    if basis.get("stock") not in (None, current.get("stock")):
        reasons.append(f"期末库存由 {basis.get('stock')} 变为 {current.get('stock')}")
    if basis.get("sell_in") not in (None, current.get("sell_in")):
        reasons.append(f"经销进货由 {basis.get('sell_in')} 变为 {current.get('sell_in')}")
    # The issued quantity is folded into `committed` before recompute (so drafts
    # cannot re-order it).  A zero "proposed" therefore reflects the promise
    # itself, not new evidence.  Only call it a problem when the *underlying
    # need* changed: compare the demand/stock inputs, and compare the proposal
    # against the need that excludes the promise.
    inputs_changed = bool(reasons)
    uncommitted_need = proposal.qty + recommendation.proposed
    if inputs_changed:
        if recommendation.proposed == 0 and proposal.qty > 0:
            reasons.append("在新证据下已无需补货，已发承诺可能造成压货")
        elif proposal.qty > uncommitted_need:
            reasons.append(
                f"新证据下所需 {uncommitted_need} 件，已发承诺 {proposal.qty} 件偏高"
            )
    return tuple(reasons)
