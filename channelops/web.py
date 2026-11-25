"""A small standard-library HTTP application.

Design notes:

* explicit routing table; unknown paths 404 rather than serving files;
* every POST requires a per-session CSRF token embedded in the page;
* request bodies are size-capped before reading, uploads are extension-checked,
  and no user-supplied path is ever opened;
* responses set a strict CSP, no external scripts, and ``nosniff``;
* the server binds to localhost by default.
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import re
import secrets
import sys
import traceback
import urllib.parse
from datetime import date
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .csvio import LoadError, safe_text
from .model import PostingPolicy, Role, Scenario
from .pages import (
    audit_page,
    cell_page,
    data_page,
    promotions_page,
    replenishment_page,
    review_page,
)
from .render import page
from .service import Service
from .state import StateError

MAX_BODY_BYTES = 10 * 1024 * 1024
COOKIE_NAME = "cops_sid"
TOKEN_FIELD = "csrf"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _try_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str, fallback: date) -> date:
    parsed = _try_date(value)
    return parsed if parsed is not None else fallback


class Sessions:
    """In-memory CSRF token store keyed by an opaque session id."""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def token(self, sid: str | None) -> tuple[str, str]:
        if sid and sid in self._tokens:
            return sid, self._tokens[sid]
        sid = secrets.token_urlsafe(24)
        token = secrets.token_urlsafe(32)
        self._tokens[sid] = token
        return sid, token

    def check(self, sid: str | None, token: str | None) -> bool:
        if not sid or not token:
            return False
        expected = self._tokens.get(sid)
        return bool(expected) and hmac.compare_digest(expected, token)


class App:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.sessions = Sessions()

    # -- helpers -----------------------------------------------------------
    def flash_from_query(self, query: dict) -> tuple[str, str] | None:
        message = (query.get("msg") or [""])[0]
        tone = (query.get("tone") or ["ok"])[0]
        if message:
            return (tone if tone in ("ok", "warn") else "ok", safe_text(message)[:300])
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ChannelOps/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # -- logging -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- response helpers --------------------------------------------------
    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Cache-Control", "no-store")

    def send_html(self, status: int, body: str, *, set_sid: str | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self._security_headers("text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        sid = set_sid or self._sid
        if sid:
            cookie = f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def error_page(self, status: int, message: str, sid: str, token: str) -> None:
        body = page(
            "出错了",
            f'<section class="panel"><h1>{status}</h1><p>{html.escape(message)}</p>'
            f'<p><a href="/">返回区域复盘</a></p></section>',
        )
        self.send_html(status, body)

    # -- request helpers ---------------------------------------------------
    _sid: str = ""

    def session(self) -> tuple[str, str]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        return self.app.sessions.token(sid)

    def read_form(self) -> tuple[dict[str, list[str]], list[tuple[str, bytes]]]:
        length = self.headers.get("Content-Length", "0")
        try:
            size = int(length)
        except ValueError:
            size = 0
        if size < 0 or size > MAX_BODY_BYTES:
            raise StateError("请求体过大")
        body = self.rfile.read(size) if size else b""
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            return self._parse_multipart(body, content_type)
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        return form, []

    @staticmethod
    def _parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, list[str]], list[tuple[str, bytes]]]:
        boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
        if not boundary_match:
            raise StateError("multipart 请求缺少 boundary")
        boundary = boundary_match.group(1).encode("utf-8")
        form: dict[str, list[str]] = {}
        files: list[tuple[str, bytes]] = []
        for part in body.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            headers_blob, content = part.split(b"\r\n\r\n", 1)
            if content.endswith(b"\r\n"):
                content = content[:-2]
            disposition = _header(headers_blob, b"content-disposition")
            name_match = re.search(r'name="([^"]*)"', disposition)
            if not name_match:
                continue
            name = name_match.group(1)
            file_match = re.search(r'filename="([^"]*)"', disposition)
            if file_match is not None:
                if file_match.group(1):
                    files.append((file_match.group(1), content))
            else:
                form.setdefault(name, []).append(content.decode("utf-8", "replace"))
        return form, files

    def verify_csrf(self, form: dict[str, list[str]]) -> bool:
        sid = self._sid
        return self.app.sessions.check(sid, (form.get(TOKEN_FIELD) or [""])[0])

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:
        try:
            self._sid, token = self.session()
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/static/app.css":
                return self.serve_static()
            if parsed.path == "/healthz":
                return self.send_html(200, "ok")
            return self.route_get(parsed.path, query, token)
        except (LoadError, StateError, ValueError) as exc:
            self._sid, token = self.session()
            self.error_page(400, str(exc), self._sid, token)
        except Exception:
            traceback.print_exc()
            self._sid, token = self.session()
            self.error_page(500, "服务器内部错误", self._sid, token)

    def route_get(self, path: str, query: dict, token: str) -> None:
        service = self.app.service
        period, start, end = self.period_from(query)
        flash = self.app.flash_from_query(query)
        if path == "/":
            dashboard = service.review(period)
            body = review_page(dashboard, start, end, flash)
        elif path == "/replenishment":
            dashboard = service.review(period)
            body = replenishment_page(dashboard, start, end, flash)
        elif path == "/promotions":
            dashboard = service.review(period)
            body = promotions_page(dashboard, start, end, flash)
        elif path == "/data":
            dashboard = service.review(period)
            fingerprints = service.gate_fingerprints(dashboard)
            body = data_page(dashboard, start, end, fingerprints, flash)
        elif path == "/cell":
            dashboard = service.review(period)
            sku = (query.get("sku") or [""])[0]
            region = (query.get("region") or [""])[0]
            cell = dashboard.review.cell(sku, region)
            if cell is None:
                return self.error_page(404, "找不到该商品/地区单元", self._sid, token)
            body = cell_page(cell, dashboard, start, end, flash)
        elif path == "/audit":
            body = audit_page(service.workspace.read_audit(300), flash)
        elif path == "/export.csv":
            dashboard = service.review(period)
            return self.send_csv(dashboard)
        elif path == "/export.zip":
            payload = service.snapshot_bundle()
            return self.send_bytes(
                "application/zip",
                payload,
                "attachment; filename=channelops-data.zip",
            )
        elif path == "/api/review":
            dashboard = service.review(period)
            return self.send_json(self.review_json(dashboard))
        else:
            return self.error_page(404, "页面不存在", self._sid, token)
        # Inject the CSRF token into every POST form on the page.
        body = self.inject_token(body, token)
        # Expose the same token for the upload form (which posts multipart and is
        # checked via a header, since parse_qs cannot read multipart fields).
        body = body.replace("</body>", f'<meta name="csrf-token" content="{html.escape(token)}"></body>')
        self.send_html(200, body)

    def do_POST(self) -> None:
        self._sid, token = self.session()
        try:
            form, files = self.read_form()
            if not self.verify_csrf(form):
                return self.error_page(403, "表单已过期，请刷新页面后重试", self._sid, token)
            if files:
                return self.handle_upload(files)
            path = urllib.parse.urlsplit(self.path).path
            service = self.app.service
            start = self.field(form, "start")
            end = self.field(form, "end")
            safe_start = _try_date(start) or service.default_period()[0]
            safe_end = _try_date(end) or service.default_period()[1]
            redirect_query = f"start={safe_start.isoformat()}&end={safe_end.isoformat()}"
            if path == "/data/confirm":
                role_raw = self.field(form, "role")
                try:
                    role = Role(role_raw)
                except ValueError:
                    raise StateError("未知角色")
                by = self.field(form, "by")
                fingerprint = self.field(form, "fingerprint")
                if role is Role.FINANCE and service.workspace.load_policy() is None:
                    raise StateError("财务确认前需先保存签收与退货入账口径")
                expected = service.gate_fingerprints().get(role, "")
                if fingerprint != expected:
                    raise StateError("数据已变化，确认指纹过期；页面已刷新，请重新确认")
                service.confirm(role, fingerprint, by)
                return self.redirect(f"/data?{redirect_query}&msg={urllib.parse.quote('确认已记录')}&tone=ok")
            if path == "/data/policy":
                scope = self.field(form, "sell_in_scope")
                restock = self.field(form, "returns_restock") == "true"
                by = self.field(form, "by")
                if not by.strip():
                    raise StateError("保存人不能为空")
                service.save_policy(PostingPolicy(sell_in_scope=scope, returns_restock=restock), by)
                return self.redirect(
                    f"/data?{redirect_query}&msg={urllib.parse.quote('入账口径已保存，相关财务确认需重新确认')}&tone=warn"
                )
            if path == "/data/scenario":
                by = self.field(form, "by")
                if not by.strip():
                    raise StateError("保存人不能为空")
                current = service.workspace.load_scenario()
                try:
                    updated = Scenario(
                        stock_high_cover_days=float(
                            self.field(form, "stock_high_cover_days") or current.stock_high_cover_days
                        ),
                        stock_low_cover_days=float(
                            self.field(form, "stock_low_cover_days") or current.stock_low_cover_days
                        ),
                        target_cover_days=float(
                            self.field(form, "target_cover_days") or current.target_cover_days
                        ),
                        default_safety_days=float(
                            self.field(form, "default_safety_days") or current.default_safety_days
                        ),
                        min_coverage_ratio=float(
                            self.field(form, "min_coverage_ratio") or current.min_coverage_ratio
                        ),
                        anchor_tolerance_days=int(
                            self.field(form, "anchor_tolerance_days") or current.anchor_tolerance_days
                        ),
                        pos_bridge_tolerance_pct=current.pos_bridge_tolerance_pct,
                        pos_bridge_tolerance_units=current.pos_bridge_tolerance_units,
                    )
                except (TypeError, ValueError):
                    raise StateError("阈值必须是数字")
                try:
                    service.save_scenario(updated, by)
                except ValueError as exc:
                    raise StateError(str(exc)) from exc
                return self.redirect(
                    f"/data?{redirect_query}&msg={urllib.parse.quote('阈值已保存，补货草稿与促销排序将按新阈值重算')}&tone=ok"
                )
            if path == "/data/correction":
                event = service.parse_correction(
                    target=self.field(form, "target"),
                    kind=self.field(form, "kind"),
                    qty=self.field(form, "qty"),
                    at=self.field(form, "at"),
                    store=self.field(form, "store"),
                    sku=self.field(form, "sku"),
                    note=self.field(form, "note"),
                    actor=self.field(form, "by"),
                    correction_id=self.field(form, "correction_id"),
                )
                service.add_correction(event, (safe_start, safe_end))
                return self.redirect(
                    f"/data?{redirect_query}&msg={urllib.parse.quote('更正已入账；草稿方案已重算，已发承诺保留')}&tone=ok"
                )
            if path == "/replenishment/issue":
                proposal_id = self.field(form, "proposal_id")
                by = self.field(form, "by")
                service.issue_proposal(proposal_id, by, period=(safe_start, safe_end))
                return self.redirect(
                    f"/replenishment?{redirect_query}&msg={urllib.parse.quote('承诺已发出并冻结')}&tone=ok"
                )
            if path == "/data/upload":
                raise StateError("上传需使用 multipart/form-data")
            return self.error_page(404, "页面不存在", self._sid, token)
        except (LoadError, StateError, ValueError) as exc:
            self._sid, token = self.session()
            self.error_page(400, str(exc), self._sid, token)
        except Exception:
            traceback.print_exc()
            self._sid, token = self.session()
            self.error_page(500, "服务器内部错误", self._sid, token)

    # -- form fields -------------------------------------------------------
    @staticmethod
    def field(form: dict[str, list[str]], name: str) -> str:
        values = form.get(name) or [""]
        return values[0].strip()

    def period_from(self, query: dict) -> tuple[tuple[date, date] | None, date, date]:
        service = self.app.service
        default_start, default_end = service.default_period()
        start = _parse_date((query.get("start") or [""])[0], default_start)
        end = _parse_date((query.get("end") or [""])[0], default_end)
        if end < start:
            start, end = end, start
        return (start, end), start, end

    # -- uploads -----------------------------------------------------------
    def handle_upload(self, files: list[tuple[str, bytes]]) -> None:
        installed = []
        for filename, content in files:
            self.app.service.install_upload(filename, content)
            installed.append(html.escape(Path(filename).name, quote=False))
        self.redirect(
            "/data?msg="
            + urllib.parse.quote(f"{'、'.join(installed)} 已安装并通过表头校验；相关确认将按新指纹复核")
            + "&tone=ok"
        )

    # -- output ------------------------------------------------------------
    def inject_token(self, body: str, token: str) -> str:
        hidden = f'<input type="hidden" name="{TOKEN_FIELD}" value="{html.escape(token)}">'
        body = re.sub(
            r'(<form\b[^>]*\bmethod="post"[^>]*>)',
            lambda match: match.group(1) + hidden,
            body,
            flags=re.IGNORECASE,
        )
        return body.replace("<body ", f'<body data-csrf="{html.escape(token)}" ', 1)

    def send_csv(self, dashboard) -> None:
        payload = self.app.service.csv_export(dashboard).encode("utf-8-sig")
        self.send_bytes("text/csv; charset=utf-8", payload, "attachment; filename=channelops-review.csv")

    def send_bytes(self, content_type: str, payload: bytes, disposition: str = "") -> None:
        self.send_response(200)
        self._security_headers(content_type)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_json(self, payload: object) -> None:
        self.send_bytes(
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def review_json(self, dashboard) -> dict:
        review = dashboard.review
        return {
            "period": {"start": str(review.period.start), "end": str(review.period.end)},
            "as_of": str(review.as_of),
            "provisional": review.provisional,
            "gates": [
                {
                    "role": gate.role.value,
                    "confirmed": gate.confirmed,
                    "stale": gate.stale,
                    "confirmed_at": gate.confirmed_at,
                }
                for gate in review.gates
            ],
            "cells": [
                {
                    "sku": cell.sku,
                    "region": cell.region,
                    "sell_in": cell.sell_in_net,
                    "pos": cell.pos,
                    "returns": cell.returns,
                    "demand": {
                        "low": cell.demand.low,
                        "high": cell.demand.high,
                        "method": cell.demand.method,
                    },
                    "stock_end": cell.stock_end,
                    "stock_complete": cell.stock_complete,
                    "coverage_days": cell.coverage_days,
                    "stock_age_days": cell.stock_age_days,
                    "comparable_stores": list(cell.coverage.comparable_sales),
                    "expected_stores": list(cell.coverage.expected_stores),
                    "pending_data": list(cell.coverage.missing),
                }
                for cell in review.cells
            ],
            "recommendations": [
                {
                    "store": item.store,
                    "sku": item.sku,
                    "status": item.status,
                    "proposed": item.proposed,
                    "on_hand": item.on_hand,
                    "safety_stock": item.safety_stock,
                    "open_order": item.open_order,
                    "committed": item.committed,
                }
                for item in dashboard.recommendations
            ],
            "proposals": [
                {
                    "id": proposal.proposal_id,
                    "status": proposal.status,
                    "qty": proposal.qty,
                    "risk": list(proposal.risk),
                }
                for proposal in dashboard.proposals
            ],
        }

    def serve_static(self) -> None:
        path = STATIC_DIR / "app.css"
        if not path.exists():
            return self.error_page(404, "样式文件缺失", *self.session())
        payload = path.read_bytes()
        self.send_response(200)
        self._security_headers("text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_HEAD = do_GET


def _header(blob: bytes, name: bytes) -> str:
    for line in blob.split(b"\r\n"):
        if line.lower().startswith(name + b":"):
            return line.split(b":", 1)[1].strip().decode("utf-8", "replace")
    return ""


def build_server(
    data_dir: Path, workspace_dir: Path, host: str = "127.0.0.1", port: int = 8000
) -> ThreadingHTTPServer:
    service = Service(data_dir, workspace_dir)
    app = App(service)
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="线下渠道动销识别与压货风险预警")
    parser.add_argument("--data", default="samples", help="数据目录（stores/events/stock 等）")
    parser.add_argument("--workspace", default="workspace", help="确认、更正、方案与审计的持久化目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = build_server(Path(args.data), Path(args.workspace), args.host, args.port)
    host, port = server.server_address[:2]
    print(f"渠道动销复盘已启动：http://{host}:{port}/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
