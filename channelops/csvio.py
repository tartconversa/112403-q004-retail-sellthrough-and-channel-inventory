"""Strict, dependency-free CSV / JSON ingestion.

Every loader collects structured :class:`Issue` objects instead of aborting on
the first bad row: the review UI needs to show *which* stores and SKUs are
excluded and why.  Hard structural failures (missing columns, non-numeric
quantities) are still surfaced as blocking issues.

Values are treated as untrusted: text is stripped of formula-leading characters
when exported, and identifiers are pattern-limited before they reach a path,
header, HTML page or database key.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

from .model import (
    Campaign,
    Event,
    Issue,
    IssueSeverity,
    Kind,
    StockSnapshot,
    Store,
)

STORES_HEADERS = ["store", "parent", "region", "state"]
EVENTS_HEADERS = ["event", "store", "sku", "kind", "qty", "at"]
STOCK_HEADERS = ["location", "sku", "qty", "day"]

_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class LoadError(ValueError):
    """Raised for fatal structural input problems."""


def safe_text(value: str | None) -> str:
    """Neutralise spreadsheet formula prefixes in text destined for export."""
    text = "" if value is None else str(value).strip()
    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text
    return text


def valid_ident(value: str) -> bool:
    return bool(_IDENT.match(value or ""))


def parse_date(value: str, field: str, row_no: int, issues: list[Issue]) -> date | None:
    raw = (value or "").strip()
    if not _DATE_RE.match(raw):
        issues.append(
            Issue(
                code="bad-date",
                message=f"第 {row_no} 行 {field} 不是 YYYY-MM-DD 日期：{safe_text(raw)!r}",
                severity=IssueSeverity.BLOCKING,
                subject=field,
            )
        )
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        issues.append(
            Issue(
                code="bad-date",
                message=f"第 {row_no} 行 {field} 不是有效日期：{raw!r}",
                severity=IssueSeverity.BLOCKING,
                subject=field,
            )
        )
        return None


def parse_int(value: str, field: str, row_no: int, issues: list[Issue], *, minimum: int | None = 0) -> int | None:
    raw = (value or "").strip()
    if raw == "":
        issues.append(
            Issue(
                code="missing-qty",
                message=f"第 {row_no} 行 {field} 为空，按无法核对处理",
                severity=IssueSeverity.BLOCKING,
                subject=field,
            )
        )
        return None
    try:
        number = int(raw)
    except ValueError:
        issues.append(
            Issue(
                code="bad-qty",
                message=f"第 {row_no} 行 {field} 不是整数：{safe_text(raw)!r}",
                severity=IssueSeverity.BLOCKING,
                subject=field,
            )
        )
        return None
    if minimum is not None and number < minimum:
        issues.append(
            Issue(
                code="negative-qty",
                message=f"第 {row_no} 行 {field} 为负（{number}）；数量必须非负",
                severity=IssueSeverity.BLOCKING,
                subject=field,
            )
        )
        return None
    return number


def _read_csv(path: Path, expected: list[str]) -> tuple[list[dict[str, str]], list[Issue]]:
    issues: list[Issue] = []
    if not path.exists():
        raise LoadError(f"找不到文件：{path.name}")
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames != expected:
                raise LoadError(
                    f"{path.name} 表头应为 {','.join(expected)}，实际为 "
                    f"{','.join(reader.fieldnames or [])}"
                )
            rows = []
            for row_no, row in enumerate(reader, start=2):
                if None in row or any(v is None for v in row.values()):
                    issues.append(
                        Issue(
                            code="malformed-row",
                            message=f"{path.name} 第 {row_no} 行列数不正确，已跳过",
                            severity=IssueSeverity.BLOCKING,
                            subject=path.name,
                        )
                    )
                    continue
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise LoadError(f"{path.name} 不是 UTF-8 编码：{exc}") from exc
    except csv.Error as exc:
        raise LoadError(f"{path.name} CSV 格式错误：{exc}") from exc
    return rows, issues


def load_stores(path: Path) -> tuple[list[Store], list[Issue]]:
    rows, issues = _read_csv(path, STORES_HEADERS)
    stores: list[Store] = []
    seen: set[str] = set()
    for row_no, row in enumerate(rows, start=2):
        name = safe_text(row["store"])
        parent = safe_text(row["parent"])
        region = safe_text(row["region"])
        state = safe_text(row["state"]) or "active"
        if not valid_ident(name) or (parent and not valid_ident(parent)):
            issues.append(
                Issue(
                    code="bad-identifier",
                    message=f"{path.name} 第 {row_no} 行门店/上级标识不合法，已跳过",
                    severity=IssueSeverity.BLOCKING,
                    subject=name or path.name,
                )
            )
            continue
        if name in seen:
            issues.append(
                Issue(
                    code="duplicate-store",
                    message=f"门店 {name} 重复定义，保留第一条",
                    severity=IssueSeverity.BLOCKING,
                    subject=name,
                )
            )
            continue
        seen.add(name)
        stores.append(Store(store=name, parent=parent, region=region, state=state))
    return stores, issues


def load_events(path: Path) -> tuple[list[Event], list[Issue]]:
    rows, issues = _read_csv(path, EVENTS_HEADERS)
    events: list[Event] = []
    seen: set[str] = set()
    for row_no, row in enumerate(rows, start=2):
        ident = safe_text(row["event"])
        store = safe_text(row["store"])
        sku = safe_text(row["sku"])
        kind_raw = safe_text(row["kind"]).lower()
        at = parse_date(row["at"], "at", row_no, issues)
        qty = parse_int(row["qty"], "qty", row_no, issues)
        if not (valid_ident(ident) and valid_ident(store) and valid_ident(sku)):
            issues.append(
                Issue(
                    code="bad-identifier",
                    message=f"{path.name} 第 {row_no} 行 event/store/sku 标识不合法，已跳过",
                    severity=IssueSeverity.BLOCKING,
                    subject=ident or path.name,
                )
            )
            continue
        if ident in seen:
            issues.append(
                Issue(
                    code="duplicate-event",
                    message=f"事件 {ident} 重复，保留第一条",
                    severity=IssueSeverity.BLOCKING,
                    subject=ident,
                )
            )
            continue
        try:
            kind = Kind(kind_raw)
        except ValueError:
            issues.append(
                Issue(
                    code="unknown-kind",
                    message=f"事件 {ident} 的类型 {safe_text(kind_raw)!r} 未知，已跳过",
                    severity=IssueSeverity.BLOCKING,
                    subject=ident,
                )
            )
            continue
        if at is None or qty is None:
            continue
        seen.add(ident)
        events.append(
            Event(event=ident, store=store, sku=sku, kind=kind, qty=qty, at=at, source=path.name)
        )
    return events, issues


def load_stock(path: Path) -> tuple[list[StockSnapshot], list[Issue]]:
    rows, issues = _read_csv(path, STOCK_HEADERS)
    snapshots: list[StockSnapshot] = []
    seen: set[tuple[str, str, date]] = set()
    for row_no, row in enumerate(rows, start=2):
        location = safe_text(row["location"])
        sku = safe_text(row["sku"])
        day = parse_date(row["day"], "day", row_no, issues)
        qty = parse_int(row["qty"], "qty", row_no, issues)
        if not (valid_ident(location) and valid_ident(sku)):
            issues.append(
                Issue(
                    code="bad-identifier",
                    message=f"{path.name} 第 {row_no} 行 location/sku 标识不合法，已跳过",
                    severity=IssueSeverity.BLOCKING,
                    subject=location or path.name,
                )
            )
            continue
        if day is None or qty is None:
            continue
        key = (location, sku, day)
        if key in seen:
            issues.append(
                Issue(
                    code="duplicate-snapshot",
                    message=f"{location}/{sku}/{day} 库存快照重复，保留第一条",
                    severity=IssueSeverity.WARNING,
                    subject=f"{location}/{sku}",
                )
            )
            continue
        seen.add(key)
        snapshots.append(StockSnapshot(location=location, sku=sku, qty=qty, day=day))
    return snapshots, issues


def load_campaigns(path: Path | None) -> tuple[list[Campaign], list[Issue]]:
    issues: list[Issue] = []
    if path is None or not path.exists():
        return [], issues
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(
            Issue(
                code="campaign-json",
                message=f"促销资料无法解析（{exc}），本轮按无促销资料处理",
                severity=IssueSeverity.WARNING,
                subject=path.name,
            )
        )
        return [], issues
    if not isinstance(payload, list):
        issues.append(
            Issue(
                code="campaign-json",
                message="促销资料应为对象数组",
                severity=IssueSeverity.BLOCKING,
                subject=path.name,
            )
        )
        return [], issues
    campaigns: list[Campaign] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            issues.append(
                Issue(
                    code="campaign-row",
                    message=f"促销资料第 {index + 1} 项不是对象，已跳过",
                    severity=IssueSeverity.BLOCKING,
                    subject=path.name,
                )
            )
            continue
        try:
            starts = datetime.strptime(str(item["starts"]), "%Y-%m-%d").date()
            ends = datetime.strptime(str(item["ends"]), "%Y-%m-%d").date()
            discount = float(item.get("discount_pct", 0) or 0)
        except (KeyError, ValueError, TypeError):
            issues.append(
                Issue(
                    code="campaign-row",
                    message=f"促销资料第 {index + 1} 项日期或折扣不合法，已跳过",
                    severity=IssueSeverity.WARNING,
                    subject=path.name,
                )
            )
            continue
        if ends < starts:
            issues.append(
                Issue(
                    code="campaign-range",
                    message=f"促销 {safe_text(item.get('campaign'))} 结束日早于开始日，已跳过",
                    severity=IssueSeverity.WARNING,
                    subject=safe_text(item.get("campaign")),
                )
            )
            continue
        campaigns.append(
            Campaign(
                campaign=safe_text(item.get("campaign", f"C{index + 1}")),
                region=safe_text(item.get("region", "")),
                sku=safe_text(item.get("sku", "")),
                store=safe_text(item.get("store", "")),
                starts=starts,
                ends=ends,
                discount_pct=discount,
                confirmed=bool(item.get("confirmed", False)),
                note=safe_text(item.get("note", "")),
            )
        )
    return campaigns, issues


def load_orders(
    path: Path | None,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int], dict[tuple[str, str], int], list[Issue]]:
    """Open orders, issued commitments and safety stock, keyed by (store, sku)."""
    issues: list[Issue] = []
    empty: tuple[
        dict[tuple[str, str], int], dict[tuple[str, str], int], dict[tuple[str, str], int]
    ] = ({}, {}, {})
    if path is None or not path.exists():
        return (*empty, issues)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(
            Issue(
                code="orders-json",
                message=f"订单资料无法解析（{exc}），本轮按无在途订单处理",
                severity=IssueSeverity.WARNING,
                subject=path.name,
            )
        )
        return (*empty, issues)

    def collect(section: str) -> dict[tuple[str, str], int]:
        result: dict[tuple[str, str], int] = {}
        rows = payload.get(section, []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return result
        for row in rows:
            if not isinstance(row, dict):
                continue
            store = safe_text(row.get("store"))
            sku = safe_text(row.get("sku"))
            try:
                qty = int(row.get("qty", 0))
            except (TypeError, ValueError):
                qty = 0
            if qty >= 0 and valid_ident(store) and valid_ident(sku):
                result[(store, sku)] = result.get((store, sku), 0) + qty
        return result

    return collect("open_orders"), collect("commitments"), collect("safety_stock"), issues


def stock_history(snapshots: list[StockSnapshot], location: str, sku: str) -> list[StockSnapshot]:
    return sorted(
        (s for s in snapshots if s.location == location and s.sku == sku), key=lambda s: s.day
    )
