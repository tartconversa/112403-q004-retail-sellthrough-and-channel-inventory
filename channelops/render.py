"""Server-rendered HTML helpers and the inline SVG comparison chart.

No template engine is used: every interpolation goes through :func:`e`, which
escapes text, so dataset values (store names, notes, imported identifiers) can
never inject markup.  Charts are inline SVG with a table fallback rendered on the
same page.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Iterable

from .model import Cell

# Categorical slots 1-2 of the validated reference palette.  Both survive CVD
# separation against each other; there are only ever two series here.
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "var(--grid)"
# Series colours resolve through CSS custom properties so the same markup works
# in light and dark mode.  Fallbacks keep the SVG readable if the stylesheet is
# missing; both pairs are validated against their own surface.
SERIES_SELL_IN = "var(--series-sell-in, #2a78d6)"
SERIES_DEMAND = "var(--series-demand, #eb6834)"
SURFACE = "var(--surface-1, #fcfcfb)"
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def num(value: int | float | None, digits: int = 0, dash: str = "—") -> str:
    if value is None:
        return dash
    if digits == 0:
        return f"{int(round(value)):,}"
    return f"{value:,.{digits}f}"


def pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.{digits}f}%"


def days(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


def demand_text(cell: Cell) -> str:
    if cell.demand.method == "no-evidence":
        return "无证据"
    if cell.demand.high is None:
        return f"≥ {cell.demand.low:,}"
    if cell.demand.low == cell.demand.high:
        return f"{cell.demand.low:,}"
    return f"{cell.demand.low:,} – {cell.demand.high:,}"


def status_chip(label: str, tone: str) -> str:
    """Status is icon + label, never colour alone."""
    icon = {"good": "✓", "warning": "!", "serious": "▲", "critical": "✕"}.get(tone, "•")
    return f'<span class="chip chip-{e(tone)}"><span aria-hidden="true">{icon}</span> {e(label)}</span>'


def range_label(cell: Cell) -> str:
    if cell.demand.high is None:
        return f"≥{cell.demand.low:,}"
    if cell.demand.low == cell.demand.high:
        return f"{cell.demand.low:,}"
    return f"{cell.demand.low:,}–{cell.demand.high:,}"


def total_demand(cells: list[Cell]) -> str:
    """Aggregate demand across cells, keeping a one-sided bound honest."""
    low = sum(cell.demand.low for cell in cells)
    if any(cell.demand.high is None for cell in cells):
        return f"≥{low:,}"
    high = sum(cell.demand.high or 0 for cell in cells)
    if low == high:
        return f"{low:,}"
    return f"{low:,} – {high:,}"


# --------------------------------------------------------------------------- #
# Dumbbell chart: sell-in (dot) vs consumer demand interval (line)
# --------------------------------------------------------------------------- #


def demand_chart(cells: list[Cell], width: int = 760) -> str:
    """One row per cell: a sell-in dot and a consumer-demand interval on one axis.

    A dual measure is deliberately avoided: both series are pieces on the same
    scale, so a longer sell-in dot to the right of the demand interval is
    immediately readable as channel loading.
    """
    if not cells:
        return '<p class="muted">当前口径下没有可比较的商品/地区单元。</p>'
    # Most channel-loaded first: the row that most needs a conversation is on top.
    cells = sorted(
        cells,
        key=lambda cell: (cell.sell_in_to_demand is None, -(cell.sell_in_to_demand or 0), cell.sku, cell.region),
    )
    label_w = 210
    plot_w = width - label_w - 70
    row_h = 46
    top = 36
    height = top + row_h * len(cells) + 24
    maximum = max(
        [cell.sell_in_net for cell in cells]
        + [cell.demand.high if cell.demand.high is not None else cell.demand.low for cell in cells]
        + [1]
    )
    scale_max = _nice_max(maximum)

    def x(value: float) -> float:
        return label_w + (value / scale_max) * plot_w

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="chart-title chart-desc" preserveAspectRatio="xMinYMin meet">',
        '<title id="chart-title">经销进货与消费者净购买区间</title>',
        '<desc id="chart-desc">蓝点为经销进货，橙色横线为有库存证据支持的消费者净购买区间；'
        "横线右端带箭头表示只有下限。两者同轴，单位均为件。</desc>",
    ]
    # Gridlines and axis ticks.
    for tick in range(0, 5):
        value = scale_max * tick / 4
        gx = x(value)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{top - 12}" x2="{gx:.1f}" y2="{height - 20}" '
            f'stroke="{GRID}" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{height - 6}" text-anchor="middle" class="axis-label">'
            f"{num(value)}</text>"
        )
    for index, cell in enumerate(cells):
        cy = top + index * row_h + row_h / 2
        parts.append(
            f'<text x="0" y="{cy - 3}" class="row-label">{e(cell.sku)} · {e(cell.region)}</text>'
        )
        small = f"可比较门店 {len(cell.coverage.comparable_sales)}/{len(cell.coverage.expected_stores)}"
        if cell.coverage.missing:
            small += " · 待补数据"
        parts.append(f'<text x="0" y="{cy + 13}" class="row-sub">{e(small)}</text>')

        # demand interval
        low_x = x(cell.demand.low)
        if cell.demand.high is None:
            high_x = label_w + plot_w
            parts.append(
                f'<line x1="{low_x:.1f}" y1="{cy}" x2="{high_x - 8:.1f}" y2="{cy}" '
                f'stroke="{SERIES_DEMAND}" stroke-width="4" stroke-linecap="round" />'
            )
            parts.append(
                f'<path d="M{high_x - 9:.1f} {cy - 6} L{high_x:.1f} {cy} L{high_x - 9:.1f} {cy + 6}" '
                f'fill="none" stroke="{SERIES_DEMAND}" stroke-width="2.5" />'
            )
            demand_label = f"≥{cell.demand.low:,}"
        else:
            high_x = x(cell.demand.high)
            if cell.demand.high > cell.demand.low:
                parts.append(
                    f'<line x1="{low_x:.1f}" y1="{cy}" x2="{high_x:.1f}" y2="{cy}" '
                    f'stroke="{SERIES_DEMAND}" stroke-width="4" stroke-linecap="round" />'
                )
                for edge in (low_x, high_x):
                    parts.append(
                        f'<line x1="{edge:.1f}" y1="{cy - 7}" x2="{edge:.1f}" y2="{cy + 7}" '
                        f'stroke="{SERIES_DEMAND}" stroke-width="2" />'
                    )
            else:
                parts.append(
                    f'<circle cx="{low_x:.1f}" cy="{cy}" r="5" fill="{SERIES_DEMAND}" '
                    f'stroke="{SURFACE}" stroke-width="2" />'
                )
            demand_label = range_label(cell)
        # sell-in dot, 2px surface ring so it reads over the interval
        sx = x(cell.sell_in_net)
        title = f"{cell.sku} {cell.region} 进货 {cell.sell_in_net:,} 件，消费者净购买 {demand_label} 件"
        parts.append(
            f'<circle cx="{sx:.1f}" cy="{cy}" r="6" fill="{SERIES_SELL_IN}" '
            f'stroke="{SURFACE}" stroke-width="2"><title>{e(title)}</title></circle>'
        )
        # direct labels (relief for the light-mode contrast warning on orange)
        label = f"进 {cell.sell_in_net:,} / 销 {demand_label}"
        approx = 26 + 12 * len(label)
        anchor = "start"
        lx = max(sx, high_x) + 10
        if lx + approx > width:
            anchor = "end"
            lx = min(sx, low_x) - 10
        parts.append(
            f'<text x="{lx:.1f}" y="{cy + 4}" text-anchor="{anchor}" class="value-label">'
            f"{e(label)}</text>"
        )
    parts.append("</svg>")
    legend = (
        '<div class="chart-legend">'
        '<span><span class="swatch-dot" style="background:var(--series-sell-in)"></span>经销进货</span>'
        '<span><span class="swatch-line" style="background:var(--series-demand)"></span>'
        "消费者净购买区间（有库存证据）</span>"
        '<span class="muted">单位：件；箭头右端表示仅有下限</span>'
        "</div>"
    )
    return legend + "".join(parts)


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 1.5, 2, 2.5, 5, 10):
        candidate = step * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


NAV = [
    ("/", "区域复盘"),
    ("/replenishment", "补货与承诺"),
    ("/promotions", "促销资源"),
    ("/data", "数据与确认"),
    ("/audit", "审计"),
]


def page(
    title: str,
    body: str,
    *,
    active: str = "",
    flash: tuple[str, str] | None = None,
    aside: str = "",
) -> str:
    nav_parts = []
    for href, label in NAV:
        current = ' aria-current="page"' if href == active else ""
        nav_parts.append(f'<a href="{href}"{current}>{e(label)}</a>')
    nav = "".join(nav_parts)
    flash_html = ""
    if flash:
        tone, message = flash
        flash_html = (
            f'<div class="flash flash-{e(tone)}" role="status">{status_chip(message, "good" if tone == "ok" else "warning")}</div>'
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · 渠道动销复盘</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<a class="skip" href="#main">跳到正文</a>
<header class="topbar">
  <div class="brand"><span class="brand-mark" aria-hidden="true"></span>渠道动销复盘</div>
  <nav aria-label="主导航">{nav}</nav>
</header>
{flash_html}
<main id="main" class="page{' has-aside' if aside else ''}">
  {body}
  {aside}
</main>
<footer class="foot">所有数据均为合成样例；件为计量单位。页面不包含外部脚本或外部字体。</footer>
</body>
</html>"""


def period_form(start: date, end: date, action: str = "/") -> str:
    return f"""
<form class="period-form" method="get" action="{e(action)}" role="search">
  <label>口径期间
    <input type="date" name="start" value="{start.isoformat()}" required>
    <span class="to">至</span>
    <input type="date" name="end" value="{end.isoformat()}" required>
  </label>
  <button type="submit">应用期间</button>
</form>"""


def table(headers: Iterable[str], rows: Iterable[str], *, caption: str = "", cls: str = "") -> str:
    head = "".join(f"<th scope=\"col\">{e(h)}</th>" for h in headers)
    caption_html = f"<caption>{e(caption)}</caption>" if caption else ""
    return (
        f'<div class="table-wrap"><table class="{e(cls)}">{caption_html}'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )
