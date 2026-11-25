"""HTTP layer: routing, CSRF, upload validation and end-to-end workflow."""

import http.cookiejar
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from channelops.web import build_server

DEMO = Path(__file__).resolve().parent.parent / "demo"


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = build_server(DEMO, Path(self.tmp.name) / "ws", "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def get(self, path):
        return self.opener.open(self.base + path, timeout=10)

    def token(self):
        html = self.get("/data").read().decode()
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def post(self, path, data, *, csrf=True):
        payload = dict(data)
        if csrf:
            payload["csrf"] = self.token()
        request = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(payload).encode(),
            method="POST",
        )
        try:
            response = self.opener.open(request, timeout=10)
            return response.status, response.geturl()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()


class Routing(HttpCase):
    def test_all_pages_render(self):
        for path in ("/", "/replenishment", "/promotions", "/data", "/audit"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])

    def test_cell_drilldown(self):
        response = self.get("/cell?sku=P2&region=east&start=2026-10-01&end=2026-10-31")
        html = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("逐店证据", html)

    def test_unknown_cell_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/cell?sku=NOPE&region=east")
        self.assertEqual(caught.exception.code, 404)

    def test_api_review_is_json(self):
        import json

        payload = json.loads(self.get("/api/review").read())
        self.assertIn("cells", payload)
        self.assertIn("gates", payload)
        self.assertTrue(payload["provisional"])

    def test_pages_have_no_inline_script(self):
        for path in ("/", "/replenishment", "/promotions", "/data", "/audit"):
            with self.subTest(path=path):
                html = self.get(path).read().decode()
                self.assertNotIn("<script", html.lower())

    def test_security_headers_present(self):
        response = self.get("/")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/etc/passwd")
        self.assertEqual(caught.exception.code, 404)

    def test_csv_export(self):
        response = self.get("/export.csv")
        text = response.read().decode("utf-8-sig")
        self.assertIn("demand_method", text.splitlines()[0])


class Csrf(HttpCase):
    def test_post_without_token_rejected(self):
        status, body = self.post(
            "/data/policy",
            {"sell_in_scope": "all", "returns_restock": "true", "by": "x"},
            csrf=False,
        )
        self.assertEqual(status, 403)

    def test_post_with_wrong_token_rejected(self):
        status, _ = self.post(
            "/data/policy",
            {"sell_in_scope": "all", "returns_restock": "true", "by": "x", "csrf": "bad"},
            csrf=False,
        )
        self.assertEqual(status, 403)


class Workflow(HttpCase):
    def test_finance_gate_requires_policy(self):
        html = self.get("/data").read().decode()
        fingerprint = re.search(r'name="fingerprint" value="([^"]+)"', html).group(1)
        status, body = self.post(
            "/data/confirm",
            {"role": "finance", "fingerprint": fingerprint, "by": "财务甲"},
        )
        self.assertEqual(status, 400)
        self.assertIn("入账口径", body)

    def test_policy_then_confirm_all_roles(self):
        status, _ = self.post(
            "/data/policy",
            {"sell_in_scope": "all", "returns_restock": "true", "by": "财务甲"},
        )
        self.assertEqual(status, 200)
        fingerprints = {}
        html = self.get("/data").read().decode()
        for role, fingerprint in re.findall(
            r'name="role" value="([^"]+)">.*?name="fingerprint" value="([^"]+)"', html, re.S
        ):
            fingerprints[role] = fingerprint
        for role, fingerprint in fingerprints.items():
            status, _ = self.post(
                "/data/confirm", {"role": role, "fingerprint": fingerprint, "by": "确认人"}
            )
            self.assertEqual(status, 200)
        html = self.get("/?start=2026-10-01&end=2026-10-31").read().decode()
        self.assertNotIn("尚未确认", html)

    def test_stale_fingerprint_rejected(self):
        status, body = self.post(
            "/data/confirm",
            {"role": "channel_ops", "fingerprint": "deadbeef", "by": "运营"},
        )
        self.assertEqual(status, 400)
        self.assertIn("指纹过期", body)

    def test_correction_flows_into_next_review(self):
        status, _ = self.post(
            "/data/correction",
            {
                "store": "S1",
                "sku": "P1",
                "kind": "pos",
                "qty": "3",
                "at": "2026-10-12",
                "by": "运营乙",
                "note": "补报扫码",
            },
        )
        self.assertEqual(status, 200)
        html = self.get("/data").read().decode()
        self.assertIn("补报扫码", html)

    def test_scenario_thresholds_persist_and_apply(self):
        before = self.get("/promotions?start=2026-10-01&end=2026-10-31").read().decode()
        self.assertIn("高于 45 天", before)
        status, _ = self.post(
            "/data/scenario",
            {
                "stock_high_cover_days": "3",
                "stock_low_cover_days": "1",
                "target_cover_days": "28",
                "default_safety_days": "7",
                "anchor_tolerance_days": "10",
                "by": "经理丁",
            },
        )
        self.assertEqual(status, 200)
        # Every region now exceeds the much stricter 3-day threshold, so the
        # cover-based reason must appear for the previously healthy P4/west.
        html = self.get("/promotions?start=2026-10-01&end=2026-10-31").read().decode()
        self.assertIn("高于 3 天", html)

    def test_scenario_threshold_validation(self):
        status, body = self.post(
            "/data/scenario",
            {
                "stock_high_cover_days": "10",
                "stock_low_cover_days": "20",
                "target_cover_days": "28",
                "default_safety_days": "7",
                "anchor_tolerance_days": "10",
                "by": "经理丁",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("低库存覆盖", body)

    def test_invalid_correction_reports_error(self):
        status, body = self.post(
            "/data/correction",
            {"store": "S1", "sku": "P1", "kind": "pos", "qty": "-1", "at": "2026-10-12", "by": "x"},
        )
        self.assertEqual(status, 400)

    def test_issue_flow_freezes_proposal(self):
        html = self.get("/replenishment?start=2026-10-01&end=2026-10-31").read().decode()
        match = re.search(r'name="proposal_id" value="([^"]+)"', html)
        self.assertIsNotNone(match)
        # Only positive drafts offer an issue control, so this match is a
        # proposal that can legitimately be issued.
        proposal_id = match.group(1)
        status, _ = self.post(
            "/replenishment/issue",
            {"proposal_id": proposal_id, "by": "经理丙", "start": "2026-10-01", "end": "2026-10-31"},
        )
        self.assertEqual(status, 200)
        html = self.get("/replenishment?start=2026-10-01&end=2026-10-31").read().decode()
        self.assertIn("已发承诺", html)


class Escaping(HttpCase):
    def test_campaign_note_is_escaped(self):
        # Free-text notes are the easiest injection surface; confirm they are
        # escaped rather than trusted.
        from channelops.csvio import load_campaigns
        import json
        demo = Path(self.tmp.name) / "data"
        demo.mkdir()
        for name in ("stores.csv", "events.csv", "stock.csv", "feeds.json"):
            (demo / name).write_bytes((DEMO / name).read_bytes())
        payload = json.loads((DEMO / "campaigns.json").read_text(encoding="utf-8"))
        payload[0]["note"] = "<script>alert(1)</script>"
        (demo / "campaigns.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        import shutil
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        self.server = build_server(demo, Path(self.tmp.name) / "ws2", "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        html = self.get("/promotions?start=2026-10-01&end=2026-10-31").read().decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class Upload(HttpCase):
    def _multipart(self, filename, content):
        boundary = "----testboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="csrf"\r\n\r\n'
            f"{self.token()}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return body, f"multipart/form-data; boundary={boundary}"

    def _upload(self, filename, content):
        body, content_type = self._multipart(filename, content)
        request = urllib.request.Request(
            self.base + "/data/upload", data=body, method="POST"
        )
        request.add_header("Content-Type", content_type)
        try:
            response = self.opener.open(request, timeout=10)
            return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_upload_rejects_unknown_filename(self):
        status, _ = self._upload("evil.sh", b"rm -rf /")
        self.assertEqual(status, 400)

    def test_upload_rejects_bad_header(self):
        status, _ = self._upload("stores.csv", b"wrong,header\n1,2\n")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
