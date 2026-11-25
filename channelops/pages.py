"""HTML pages for the review, replenishment, promotions, data and audit views."""

from __future__ import annotations

import urllib.parse
from datetime import date
from .model import (
    IssueSeverity,
    Role,
    Review,
)
from .render import (
    demand_chart,
    demand_text,
    days,
    e,
    num,
    page,
    pct,
    period_form,
    status_chip,
    table,
    total_demand,
)
from .service import Dashboard

ROLE_LABELS = {
    Role.CHANNEL_OPS: "渠道运营",
    Role.FINANCE: "财务",
    Role.REGIONAL_MANAGER: "区域经理",
}
NEED_LABELS = {
    "stock": "库存",
    "pos": "POS",
    "stock-feed": "库存数据源",
}
METHOD_LABELS = {
    "stock-bridge": "库存桥（点值）",
    "bounded-pos": "库存桥/上下限",
    "stock-bridge-unreconciled": "库存桥与扫码未对平",
    "pos-plus-opening-bound": "仅期初，区间",
    "pos-floor-only": "仅下限",
    "no-evidence": "无证据",
    "": "—",
}


def sell_in_scope_note(review: Review) -> str:
    if review.posting_policy.sell_in_scope == "dealer_only":
        return "仅经销商节点收货"
    return "含经销商与门店收货"


def gap_banner(dashboard: Dashboard) -> str:
    review = dashboard.review
    stale = [gate for gate in review.gates if gate.stale]
    missing = [gate for gate in review.gates if not gate.confirmed and not gate.stale]
    if not stale and not missing:
        return ""
    parts = []
    if stale:
        parts.append("以下确认在数据/口径更新后已失效：" + "、".join(ROLE_LABELS[g.role] for g in stale))
    if missing:
        parts.append("尚未确认：" + "、".join(ROLE_LABELS[g.role] for g in missing))
    return (
        '<div class="banner banner-warning" role="status">'
        + status_chip("复盘为暂定口径", "warning")
        + " "
        + e("；".join(parts))
        + ' · <a href="/data">前往确认</a></div>'
    )


def gate_strip(dashboard: Dashboard) -> str:
    chips = []
    for gate in dashboard.review.gates:
        label = ROLE_LABELS[gate.role]
        if gate.stale:
            chips.append(status_chip(f"{label} 确认已失效", "warning"))
        elif gate.confirmed:
            chips.append(status_chip(f"{label} 已确认", "good"))
        else:
            chips.append(status_chip(f"{label} 未确认", "serious"))
    return '<div class="gate-strip">' + "".join(chips) + "</div>"


def review_page(dashboard: Dashboard, start: date, end: date, flash=None) -> str:
    review = dashboard.review
    cells = review.cells
    total_sell_in = sum(cell.sell_in_net for cell in cells)
    total_low = sum(cell.demand.low for cell in cells)
    total_high = None if any(cell.demand.high is None for cell in cells) else sum(cell.demand.high or 0 for cell in cells)
    demand_total_text = total_demand(cells)
    complete_cells = [cell for cell in cells if cell.stock_complete and cell.demand.bounded]
    pending = sum(1 for cell in cells if cell.coverage.missing)
    high_cover = [cell for cell in cells if cell.coverage_days is not None and cell.coverage_days > review.scenario.stock_high_cover_days]

    kpis = f"""
<section class="kpis" aria-label="关键指标">
  <div class="kpi"><div class="kpi-label">经销进货（净）</div>
    <div class="kpi-value">{num(total_sell_in)}</div>
    <div class="kpi-note">同期 {num(sum(c.prior_sell_in or 0 for c in cells))} 件 · {e(sell_in_scope_note(review))}</div></div>
  <div class="kpi"><div class="kpi-label">消费者净购买</div>
    <div class="kpi-value">{e(demand_total_text)}</div>
    <div class="kpi-note">POS 减退货；仅统计有库存证据的门店</div></div>
  <div class="kpi"><div class="kpi-label">进货 / 动销</div>
    <div class="kpi-value">{'≥' if total_high is None else ''}{num(total_sell_in / total_low if total_low else None, 2)}</div>
    <div class="kpi-note">{'动销仅有下限，比值也仅是下限；' if total_high is None else ''}高于 1.5 提示进货可能停在区域仓</div></div>
  <div class="kpi"><div class="kpi-label">完整库存单元</div>
    <div class="kpi-value">{len(complete_cells)}<span class="kpi-den">/{len(cells)}</span></div>
    <div class="kpi-note">{pending or '无'} 个单元有门店或仓库待补数据</div></div>
</section>"""

    alerts = []
    for issue in review.issues:
        if issue.severity is IssueSeverity.BLOCKING:
            alerts.append(issue)
    for issue in review.issues:
        if issue.code in ("channel-loading",):
            alerts.append(issue)
    alert_html = ""
    if alerts:
        items = "".join(
            f"<li>{status_chip('阻断' if issue.blocking else '提示', 'critical' if issue.blocking else 'warning')} {e(issue.message)}</li>"
            for issue in alerts[:8]
        )
        alert_html = f'<section class="panel"><h2>需要先处理</h2><ul class="alerts">{items}</ul></section>'

    rows = []
    for cell in cells:
        ratio = cell.sell_in_to_demand
        ratio_text = "—" if ratio is None else f"{ratio:.2f}"
        ratio_tone = ""
        if ratio is not None and ratio >= 1.5:
            ratio_tone = "critical" if ratio >= 2 else "warning"
        ratio_cell = f'<td class="num">{ratio_text}</td>'
        if ratio_tone:
            ratio_cell = f'<td class="num">{status_chip(f"{ratio:.2f}", ratio_tone)}</td>'
        comparables = len(cell.coverage.comparable_sales)
        expected = len(cell.coverage.expected_stores)
        missing = "、".join(NEED_LABELS.get(need, need) for need in sorted(set(cell.coverage.missing))) or "—"
        rows.append(
            "<tr>"
            f'<td><a href="/cell?sku={urllib.parse.quote(cell.sku)}&amp;region={urllib.parse.quote(cell.region)}'
            f'&amp;start={start.isoformat()}&amp;end={end.isoformat()}"><strong>{e(cell.sku)}</strong> · {e(cell.region)}</a></td>'
            f'<td class="num">{num(cell.sell_in_net)}</td>'
            f'<td class="num">{pct(cell.sell_in_growth)}</td>'
            f'<td class="num">{e(demand_text(cell))}<div class="sub">{e(METHOD_LABELS.get(cell.demand.method, cell.demand.method))}</div></td>'
            f'<td class="num">{pct(cell.demand_growth)}</td>'
            f"{ratio_cell}"
            f'<td class="num">{num(cell.stock_end)}</td>'
            f'<td class="num">{e(days(cell.coverage_days))}</td>'
            f'<td class="num">{e(days(cell.shelf_coverage_days))}</td>'
            f'<td class="num">{e(days(cell.stock_age_days))}</td>'
            f'<td class="num">{comparables}/{expected}</td>'
            f'<td>{e(missing)}</td>'
            "</tr>"
        )
    review_table = table(
        [
            "商品 · 地区",
            "进货",
            "进货环比",
            "消费者净购买",
            "动销环比",
            "进货/动销",
            "期末库存",
            "全渠道覆盖",
            "门店覆盖",
            "库龄(天)",
            "可比门店",
            "待补数据",
        ],
        rows,
        caption="同一商品与地区口径：经销进货、消费者净购买、库存龄与覆盖天数",
        cls="data-table",
    )

    body = f"""
{gap_banner(dashboard)}
<header class="page-head">
  <div>
    <h1>区域复盘</h1>
    <p class="lede">把经销进货、消费者净购买、库存龄和覆盖天数放在同一 (商品 × 地区) 口径下。
    进货代表渠道承诺，动销代表消费者需求；两者差额是库存转移的信号，而不是增长。</p>
  </div>
  {period_form(start, end)}
</header>
{gate_strip(dashboard)}
{kpis}
<section class="panel">
  <div class="panel-head">
    <h2>进货与动销对照</h2>
    <p class="muted">每行一个可比较单元，按进货/动销从高到低排列。橙色区间来自库存桥或扫码下限，蓝点是同期经销进货。</p>
  </div>
  {demand_chart(cells)}
</section>
{alert_html}
<section class="panel">
  <div class="panel-head"><h2>复盘明细</h2>
  <p class="muted">覆盖 = 期末库存 ÷ 日动销。全渠道含区域仓（压货更易藏在这里）；门店覆盖只看货架。
  缺口未观测时不填点值。</p></div>
  {review_table}
</section>
"""
    aside = ""
    if high_cover:
        items = "".join(
            f"<li><strong>{e(cell.sku)} · {e(cell.region)}</strong> 覆盖 {e(days(cell.coverage_days))} 天，"
            f"库存 {num(cell.stock_end)} 件</li>"
            for cell in high_cover
        )
        aside = f"""
<aside class="aside panel">
  <h2>库存偏高</h2>
  <p class="muted">这些单元不会因进货额增长自动获得促销资源。</p>
  <ul>{items}</ul>
</aside>"""
    return page("区域复盘", body, active="/", flash=flash, aside=aside)


def replenishment_page(dashboard: Dashboard, start: date, end: date, flash=None) -> str:
    review = dashboard.review
    recommendations = {(item.store, item.sku): item for item in dashboard.recommendations}
    rows = []
    for proposal in sorted(dashboard.proposals, key=lambda p: (p.status, p.store, p.sku)):
        rec = recommendations.get((proposal.store, proposal.sku))
        basis = proposal.basis or {}
        risk = "；".join(proposal.risk) if proposal.risk else "—"
        status = (
            status_chip("已发承诺（保留）", "warning")
            if proposal.status == "issued"
            else status_chip("草稿（随补报重算）", "good")
        )
        action = ""
        if proposal.status == "draft" and proposal.qty > 0:
            action = (
                f'<form method="post" action="/replenishment/issue" class="inline">'
                f'<input type="hidden" name="proposal_id" value="{e(proposal.proposal_id)}">'
                f'<input type="hidden" name="start" value="{start.isoformat()}">'
                f'<input type="hidden" name="end" value="{end.isoformat()}">'
                f'<label class="sr">操作人<input name="by" required maxlength="80" placeholder="姓名"></label>'
                f'<button type="submit">向经销商发出</button></form>'
            )
        if proposal.status == "draft" and proposal.qty <= 0:
            action = '<span class="muted">待补数据，暂不可发</span>'
        cover = "—"
        if rec and rec.cover_days is not None:
            cover = days(rec.cover_days)
        rows.append(
            "<tr>"
            f"<td>{e(proposal.proposal_id)}<div class=\"sub\">{e(proposal.store)} · {e(proposal.sku)}</div></td>"
            f"<td>{status}</td>"
            f'<td class="num">{num(proposal.qty)}</td>'
            f'<td class="num">{e(cover)}</td>'
            f'<td class="num">{num(basis.get("stock"))}</td>'
            f'<td class="num">{num(basis.get("sell_in"))}</td>'
            f"<td>{e(risk)}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    table_html = table(
        ["方案 / 门店·SKU", "状态", "数量", "现有覆盖", "期末库存", "同期进货", "风险原因", "操作"],
        rows,
        caption="补货方案：草稿随补报重算，已发承诺保留并显示风险",
        cls="data-table",
    )

    issued_risks = [proposal for proposal in dashboard.proposals if proposal.status == "issued" and proposal.risk]
    risk_html = ""
    if issued_risks:
        items = "".join(
            f"<li><strong>{e(proposal.proposal_id)}</strong>（{num(proposal.qty)} 件，{e(proposal.store)} · "
            f"{e(proposal.sku)}）：{e('；'.join(proposal.risk))}</li>"
            for proposal in issued_risks
        )
        risk_html = (
            '<section class="panel panel-risk"><h2>已发承诺的风险（已冻结，不重写）</h2>'
            f"<ul class=\"alerts\">{items}</ul></section>"
        )
    needs = [item for item in dashboard.recommendations if item.status == "needs-data"]
    needs_html = ""
    if needs:
        items = "".join(
            f"<li><strong>{e(item.store)} · {e(item.sku)}</strong>：{e('；'.join(item.reasons))}</li>"
            for item in needs
        )
        needs_html = f'<section class="panel"><h2>待补数据，暂不出数</h2><ul>{items}</ul></section>'

    body = f"""
{gap_banner(dashboard)}
<header class="page-head">
  <div><h1>补货与承诺</h1>
  <p class="lede">建议数量 = 目标覆盖 + 安全库存 − 现有库存 − 在途订单 − 已向经销商承诺。
  补报 POS/退货/调拨更正后，未确认草稿会重算；已发出的采购承诺不重写，只标注风险。</p></div>
  {period_form(start, end, "/replenishment")}
</header>
{gate_strip(dashboard)}
{risk_html}
<section class="panel">
  <div class="panel-head"><h2>方案列表</h2>
  <p class="muted">发出承诺需填写操作人并写入审计日志；发出后数量冻结。</p></div>
  {table_html}
</section>
{needs_html}
"""
    return page("补货与承诺", body, active="/replenishment", flash=flash)


def promotions_page(dashboard: Dashboard, start: date, end: date, flash=None) -> str:
    rows = []
    for decision in dashboard.promotions:
        tone = "good" if decision.eligible else "serious"
        label = "可分配" if decision.eligible else "不分配"
        campaigns = "、".join(decision.confirmed_campaigns) or "—"
        pending = "、".join(decision.pending_campaigns) or "—"
        rows.append(
            "<tr>"
            f"<td><strong>{e(decision.sku)}</strong> · {e(decision.region)}</td>"
            f"<td>{status_chip(label, tone)}</td>"
            f'<td class="num">{e(days(decision.cover_days))}</td>'
            f'<td class="num">{pct(decision.demand_growth)}</td>'
            f'<td class="num">{pct(decision.sell_in_growth)}</td>'
            f'<td class="num">{decision.score:.1f}</td>'
            f"<td>{e(decision.reason)}</td>"
            f"<td>{e(campaigns)}</td>"
            f"<td>{e(pending)}</td>"
            "</tr>"
        )
    table_html = table(
        ["商品 · 地区", "结论", "覆盖天数", "动销增长", "进货增长", "评分", "理由", "已确认活动", "待确认活动"],
        rows,
        caption="促销资源分配：评分只看有证据的消费者需求增长，进货额增长不参与评分",
        cls="data-table",
    )
    attr_rows = []
    for attribution in dashboard.campaign_attr:
        state = (
            status_chip("已确认", "good")
            if attribution.confirmed
            else status_chip("待区域经理确认", "warning")
        )
        lift = "—" if attribution.lift is None else f"{attribution.lift:+.0%}"
        attr_rows.append(
            "<tr>"
            f"<td><strong>{e(attribution.campaign)}</strong></td>"
            f"<td>{e(attribution.sku)} · {e(attribution.region)}</td>"
            f"<td>{e(attribution.store or '—')}</td>"
            f'<td class="num">{num(attribution.campaign_pos)}</td>'
            f'<td class="num">{num(attribution.baseline_pos)}</td>'
            f'<td class="num">{e(lift)}</td>'
            f"<td>{state}</td>"
            f"<td>{e(attribution.evidence)}</td>"
            f'<td class="sub">{e(attribution.note or "—")}</td>'
            "</tr>"
        )
    attribution_table = table(
        ["活动", "商品 · 地区", "门店", "活动期扫码", "非活动期扫码", "提升", "确认", "证据", "备注"],
        attr_rows or ['<tr><td colspan="9" class="muted">无活动资料</td></tr>'],
        caption="促销活动归因：未确认活动不计算提升",
        cls="data-table",
    )
    body = f"""
<header class="page-head">
  <div><h1>促销资源</h1>
  <p class="lede">库存偏高（覆盖超过 {e(review_scenario(dashboard))} 天）的地区即使进货额增长，也不会自动获得促销资源。
  只有消费者净购买有证据且库存处于合理区间的单元进入排序。</p></div>
  {period_form(start, end, "/promotions")}
</header>
{gate_strip(dashboard)}
<section class="panel">
  <div class="panel-head"><h2>分配排序</h2>
  <p class="muted">评分 = 动销增长 × 可比较门店覆盖率。活动提升只在区域经理确认活动范围后归因。</p></div>
  {table_html}
</section>
<section class="panel">
  <div class="panel-head"><h2>活动归因</h2>
  <p class="muted">仅在活动资料标记 confirmed 且区域经理已确认陈列范围时计算提升；否则列为待确认。</p></div>
  {attribution_table}
</section>
"""
    return page("促销资源", body, active="/promotions", flash=flash)


def review_scenario(dashboard: Dashboard) -> str:
    return f"{dashboard.review.scenario.stock_high_cover_days:g}"


def data_page(dashboard: Dashboard, start: date, end: date, fingerprints: dict, flash=None) -> str:
    review = dashboard.review
    gate_rows = []
    for gate in review.gates:
        state = (
            status_chip("已确认", "good")
            if gate.confirmed
            else status_chip("确认已失效", "warning")
            if gate.stale
            else status_chip("未确认", "serious")
        )
        gate_rows.append(
            "<tr>"
            f"<td>{e(ROLE_LABELS[gate.role])}</td>"
            f"<td>{e(gate.detail)}</td>"
            f"<td>{state}</td>"
            f"<td class=\"mono\">{e(gate.confirmed_at or '—')}</td>"
            f"<td class=\"mono\">{e(fingerprints.get(gate.role, ''))}</td>"
            f'<td><form method="post" action="/data/confirm" class="inline">'
            f'<input type="hidden" name="role" value="{e(gate.role.value)}">'
            f'<input type="hidden" name="fingerprint" value="{e(fingerprints.get(gate.role, ""))}">'
            f'<input type="hidden" name="start" value="{start.isoformat()}">'
            f'<input type="hidden" name="end" value="{end.isoformat()}">'
            f'<label class="sr">确认人<input name="by" required maxlength="80" placeholder="姓名"></label>'
            f'<button type="submit">确认</button></form></td>'
            "</tr>"
        )
    gate_table = table(
        ["角色", "确认事项", "状态", "确认时间", "数据指纹", "操作"],
        gate_rows,
        caption="三方确认：渠道运营确认层级，财务确认入账口径，区域经理确认陈列与活动范围",
        cls="data-table",
    )

    exhibits = []
    for cell in review.cells:
        for exhibit in cell.coverage.exhibits:
            exhibits.append((cell, exhibit))
    exhibit_rows = [
        "<tr>"
        f"<td><strong>{e(cell.sku)}</strong> · {e(cell.region)}</td>"
        f"<td>{e(exhibit.subject)}</td>"
        f"<td>{e('、'.join(exhibit.missing) or '—')}</td>"
        f"<td>{status_chip('阻断比较' if exhibit.blocking else '提示', 'critical' if exhibit.blocking else 'warning')}</td>"
        f"<td>{e(exhibit.reason)}</td>"
        "</tr>"
        for cell, exhibit in exhibits
    ]
    exhibit_table = table(
        ["单元", "对象", "缺口", "影响", "说明"], exhibit_rows, caption="待补数据清单", cls="data-table"
    )

    source_rows = []
    for item in dashboard.data_issues:
        tone = "critical" if item.severity is IssueSeverity.BLOCKING else "warning"
        source_rows.append(
            "<tr>"
            f"<td class=\"mono\">{e(item.code)}</td>"
            f"<td>{status_chip('阻断' if item.blocking else '提示', tone)}</td>"
            f"<td>{e(item.message)}</td>"
            "</tr>"
        )
    for item in dashboard.workspace_issues:
        source_rows.append(
            "<tr>"
            f"<td class=\"mono\">{e(item.code)}</td>"
            f"<td>{status_chip('提示', 'warning')}</td>"
            f"<td>{e(item.message)}</td>"
            "</tr>"
        )
    source_table = table(["代码", "级别", "说明"], source_rows, caption="数据源问题", cls="data-table")

    policy = review.posting_policy
    scenario = review.scenario
    correction_rows = "".join(
        "<tr>"
        f"<td class=\"mono\">{e(c.event)}</td>"
        f"<td>{e(c.corrected_of or '新增')}</td>"
        f"<td>{e(c.store)} · {e(c.sku)}</td>"
        f"<td>{e(c.kind.value)}</td>"
        f'<td class="num">{num(c.qty)}</td>'
        f"<td class=\"mono\">{e(c.at)}</td>"
        f"<td>{e(c.note)}</td>"
        "</tr>"
        for c in dashboard.corrections
    )
    correction_table = table(
        ["编号", "更正对象", "门店 · SKU", "类型", "数量", "发生日", "备注"],
        [correction_rows] if correction_rows else ['<tr><td colspan="7" class="muted">尚无补报更正</td></tr>'],
        caption="补报与更正记录",
        cls="data-table",
    )

    body = f"""
{gap_banner(dashboard)}
<header class="page-head">
  <div><h1>数据与确认</h1>
  <p class="lede">确认只对当时的数据指纹有效。数据或口径变化后，旧确认会自动标记为失效，相关复盘回到暂定状态。</p></div>
  {period_form(start, end, "/data")}
</header>
{gate_strip(dashboard)}
<section class="panel">
  <h2>三方确认</h2>
  <p class="muted">财务确认前必须先保存入账口径；确认内容是数据指纹，不是勾选“已阅读”。</p>
  {gate_table}
</section>
<section class="panel">
  <div class="panel-head"><h2>入账口径（财务）</h2>
  <p class="muted">当前口径：门店收货{'计入' if policy.sell_in_scope == 'all' else '不计入'}经销进货；退货{'回库' if policy.returns_restock else '不回库（报废）'}。</p></div>
  <form method="post" action="/data/policy" class="grid-form">
    <input type="hidden" name="start" value="{start.isoformat()}">
    <input type="hidden" name="end" value="{end.isoformat()}">
    <label>进货口径
      <select name="sell_in_scope">
        <option value="all" {'selected' if policy.sell_in_scope == 'all' else ''}>经销商与门店收货都计入进货</option>
        <option value="dealer_only" {'selected' if policy.sell_in_scope == 'dealer_only' else ''}>仅经销商节点收货计入进货</option>
      </select>
    </label>
    <label>退货处理
      <select name="returns_restock">
        <option value="true" {'selected' if policy.returns_restock else ''}>退回门店库存（回库）</option>
        <option value="false" {'selected' if not policy.returns_restock else ''}>不回库（报废/退回仓）</option>
      </select>
    </label>
    <label>保存人<input name="by" required maxlength="80" placeholder="姓名"></label>
    <button type="submit">保存入账口径</button>
  </form>
</section>
<section class="panel">
  <div class="panel-head"><h2>复盘阈值（区域经理/运营）</h2>
  <p class="muted">阈值进入复盘参数并写入审计；修改不会重写已发承诺。</p></div>
  <form method="post" action="/data/scenario" class="grid-form">
    <input type="hidden" name="start" value="{start.isoformat()}">
    <input type="hidden" name="end" value="{end.isoformat()}">
    <label>高库存覆盖（天）<input name="stock_high_cover_days" inputmode="decimal" value="{scenario.stock_high_cover_days:g}" required></label>
    <label>低库存覆盖（天）<input name="stock_low_cover_days" inputmode="decimal" value="{scenario.stock_low_cover_days:g}" required></label>
    <label>目标覆盖（天）<input name="target_cover_days" inputmode="decimal" value="{scenario.target_cover_days:g}" required></label>
    <label>默认安全库存天数<input name="default_safety_days" inputmode="decimal" value="{scenario.default_safety_days:g}" required></label>
    <label>最低可比门店占比<input name="min_coverage_ratio" inputmode="decimal" value="{scenario.min_coverage_ratio:g}" required></label>
    <label>快照容忍天数<input name="anchor_tolerance_days" inputmode="numeric" value="{scenario.anchor_tolerance_days}" required></label>
    <label>保存人<input name="by" required maxlength="80" placeholder="姓名"></label>
    <button type="submit">保存阈值</button>
  </form>
</section>
<section class="panel">
  <h2>补报 POS / 退货 / 调拨更正</h2>
  <p class="muted">更正事件会取代被更正行，并触发未发方案的重新计算；已发承诺保留。</p>
  <form method="post" action="/data/correction" class="grid-form">
    <input type="hidden" name="start" value="{start.isoformat()}">
    <input type="hidden" name="end" value="{end.isoformat()}">
    <label>门店<input name="store" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_.:\\-]{{0,63}}"></label>
    <label>SKU<input name="sku" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_.:\\-]{{0,63}}"></label>
    <label>类型
      <select name="kind">
        <option value="pos">POS 销售</option>
        <option value="return">退货</option>
        <option value="transfer-in">调入</option>
        <option value="transfer-out">调出</option>
        <option value="receipt">收货</option>
      </select>
    </label>
    <label>数量<input name="qty" required inputmode="numeric" pattern="[0-9]+" min="0"></label>
    <label>发生日<input type="date" name="at" required></label>
    <label>被更正事件编号（新增留空）<input name="target" maxlength="64"></label>
    <label>更正编号（留空自动生成）<input name="correction_id" maxlength="64"></label>
    <label>操作人<input name="by" required maxlength="80"></label>
    <label class="wide">备注<input name="note" maxlength="500"></label>
    <button type="submit">提交更正</button>
  </form>
</section>
<section class="panel">
  <h2>待补数据</h2>
  {exhibit_table}
</section>
<section class="panel">
  <h2>更新数据文件</h2>
  <p class="muted">只接受 stores.csv、events.csv、stock.csv、campaigns.json、orders.json、feeds.json；
  上传先按表头/JSON 校验再原子替换，校验失败不会覆盖现有数据。</p>
  <form method="post" action="/data/upload" enctype="multipart/form-data" class="grid-form">
    <input type="hidden" name="start" value="{start.isoformat()}">
    <input type="hidden" name="end" value="{end.isoformat()}">
    <label class="wide">数据文件<input type="file" name="file" accept=".csv,.json" multiple required></label>
    <button type="submit">上传并校验</button>
  </form>
</section>
<section class="panel">
  <h2>数据源问题</h2>
  {source_table}
</section>
<section class="panel">
  <h2>补报与更正</h2>
  {correction_table}
</section>
"""
    return page("数据与确认", body, active="/data", flash=flash)


def audit_page(entries: list[dict], flash=None) -> str:
    rows = []
    for entry in reversed(entries):
        detail = entry.get("detail") or {}
        if not isinstance(detail, dict):
            detail = {"value": detail}
        rendered = "；".join(f"{key}={value}" for key, value in detail.items() if key != "by")
        by = detail.get("by", "")
        rows.append(
            "<tr>"
            f'<td class="mono">{e(entry.get("at", ""))}</td>'
            f"<td>{e(entry.get('action', ''))}</td>"
            f"<td>{e(rendered)}</td>"
            f"<td>{e(by)}</td>"
            "</tr>"
        )
    table_html = table(
        ["时间 (UTC)", "动作", "明细", "操作人"],
        rows or ['<tr><td colspan="4" class="muted">尚无记录</td></tr>'],
        caption="审计日志：确认、口径变更、补报更正与承诺发出",
        cls="data-table",
    )
    body = f"""
<header class="page-head">
  <div><h1>审计</h1>
  <p class="lede">每次确认、口径调整、补报更正和承诺发出都会留痕，便于复核“为什么当时给出这个数字”。</p></div>
</header>
<section class="panel">{table_html}</section>
"""
    return page("审计", body, active="/audit", flash=flash)


def cell_page(cell, dashboard: Dashboard, start: date, end: date, flash=None) -> str:
    """Drill-down: per-store evidence, the items making up the cell, and gaps."""
    review = dashboard.review
    rows = []
    for item in cell.store_demand:
        low = f"{item.low:,}"
        high = "—" if item.high is None else f"{item.high:,}"
        comparable = status_chip("可比", "good") if item.method != "no-evidence" and item.high is not None else status_chip("待补", "warning")
        rows.append(
            "<tr>"
            f"<td><strong>{e(item.store)}</strong></td>"
            f'<td class="num">{num(item.pos)}</td>'
            f'<td class="num">{num(item.returns)}</td>'
            f'<td class="num">{low} – {high}</td>'
            f'<td>{e(METHOD_LABELS.get(item.method, item.method))}</td>'
            f'<td class="num">{"" if item.opening is None else num(item.opening)}</td>'
            f'<td class="num">{"" if item.closing is None else num(item.closing)}</td>'
            f'<td class="num">{num(item.receipts)}</td>'
            f'<td class="num">{num(item.transfer_in)} / {num(item.transfer_out)}</td>'
            f"<td>{comparable}</td>"
            f"<td class=\"sub\">{e(item.evidence)}</td>"
            "</tr>"
        )
    store_table = table(
        [
            "门店",
            "POS",
            "退货",
            "动销区间",
            "口径",
            "期初",
            "期末",
            "收货",
            "调入/调出",
            "比较",
            "证据",
        ],
        rows,
        caption="逐店证据：调拨只移动库存，不计入销售；退货按财务口径处理",
        cls="data-table",
    )

    exhibit_rows = [
        "<tr>"
        f"<td>{e(exhibit.subject)}</td>"
        f"<td>{e('、'.join(NEED_LABELS.get(need, need) for need in exhibit.missing) or '—')}</td>"
        f"<td>{status_chip('阻断比较' if exhibit.blocking else '提示', 'critical' if exhibit.blocking else 'warning')}</td>"
        f"<td>{e(exhibit.reason)}</td>"
        "</tr>"
        for exhibit in cell.coverage.exhibits
    ]
    exhibit_table = table(
        ["对象", "缺口", "影响", "说明"],
        exhibit_rows or ['<tr><td colspan="4" class="muted">无可列缺口</td></tr>'],
        caption="该单元待补数据",
        cls="data-table",
    )

    issue_rows = "".join(
        "<tr>"
        f'<td class="mono">{e(issue.code)}</td>'
        f"<td>{status_chip('阻断' if issue.blocking else '提示', 'critical' if issue.blocking else 'warning')}</td>"
        f"<td>{e(issue.message)}</td>"
        "</tr>"
        for issue in cell.issues
    )
    issue_table = table(
        ["代码", "级别", "说明"],
        [issue_rows] if issue_rows else ['<tr><td colspan="3" class="muted">无</td></tr>'],
        caption="该单元提示",
        cls="data-table",
    )

    stock_note = (
        f"期末库存 {num(cell.stock_end)} 件，快照日 {e(cell.stock_snapshot_day or '—')}"
        if cell.stock_end is not None
        else "期末无可信库存快照"
    )
    if cell.missing_stock_locations:
        stock_note += f"；缺 {e('、'.join(cell.missing_stock_locations))}"
    age_note = "—" if cell.stock_age_days is None else f"{cell.stock_age_days:.1f} 天（FIFO 加权，最老 {cell.oldest_stock_days:.0f} 天）"

    body = f"""
{gap_banner(dashboard)}
<header class="page-head">
  <div><h1>{e(cell.sku)} · {e(cell.region)}</h1>
  <p class="lede">{e(start.isoformat())} 至 {e(end.isoformat())}。{e(cell.demand.evidence and '动销口径：' + ' / '.join(cell.demand.evidence) or '动销无证据')}</p></div>
  <a class="back" href="/?start={start.isoformat()}&amp;end={end.isoformat()}">← 返回复盘</a>
</header>
{gate_strip(dashboard)}
<section class="kpis" aria-label="单元指标">
  <div class="kpi"><div class="kpi-label">消费者净购买</div>
    <div class="kpi-value">{e(demand_text(cell))}</div>
    <div class="kpi-note">{e(METHOD_LABELS.get(cell.demand.method, cell.demand.method))}</div></div>
  <div class="kpi"><div class="kpi-label">经销进货（净）</div>
    <div class="kpi-value">{num(cell.sell_in_net)}</div>
    <div class="kpi-note">收货地点：{e('、'.join(f'{k} {v}' for k, v in cell.receipt_locations.items()) or '无')}</div></div>
  <div class="kpi"><div class="kpi-label">全渠道 / 门店覆盖</div>
    <div class="kpi-value">{e(days(cell.coverage_days))}<span class="kpi-den"> / {e(days(cell.shelf_coverage_days))} 天</span></div>
    <div class="kpi-note">全渠道含区域仓；门店只看货架</div></div>
  <div class="kpi"><div class="kpi-label">加权库龄</div>
    <div class="kpi-value">{e(age_note)}</div>
    <div class="kpi-note">{e(stock_note)}</div></div>
</section>
<section class="panel">
  <div class="panel-head"><h2>逐店证据</h2>
  <p class="muted">只有同时具备期初和期末锚点的门店才进入点值；其余给出区间或下限。</p></div>
  {store_table}
</section>
<section class="panel"><h2>待补数据</h2>{exhibit_table}</section>
<section class="panel"><h2>提示</h2>{issue_table}</section>
"""
    return page(f"{cell.sku} · {cell.region}", body, active="/", flash=flash)
