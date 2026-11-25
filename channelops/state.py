"""Durable workspace state: confirmations, corrections, proposals and audit.

Everything the review writes lives in a single workspace directory as JSON /
JSONL.  Writes are atomic (temp file + rename) so a crash cannot leave a
half-written confirmation.  Paths are resolved under the workspace root and the
caller never supplies a filesystem path.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from .model import (
    Confirmation,
    Event,
    FeedWindow,
    Issue,
    IssueSeverity,
    Kind,
    PostingPolicy,
    Proposal,
    Role,
    Scenario,
)
from .csvio import valid_ident

STATE_VERSION = 1
_ROLE_VALUES = {role.value for role in Role}


class StateError(ValueError):
    """Raised for a rejected state transition or malformed state file."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"{path.name} 状态文件损坏：{exc}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Workspace:
    """Persistent review state for one dataset."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.confirmations_path = self.root / "confirmations.json"
        self.corrections_path = self.root / "corrections.json"
        self.proposals_path = self.root / "proposals.json"
        self.scenario_path = self.root / "scenario.json"
        self.policy_path = self.root / "posting_policy.json"
        self.audit_path = self.root / "audit.jsonl"
        self.feeds_path = self.root / "feeds.json"
        # Serialises the read-modify-write sequences below.  The threaded server
        # can serve two forms at once; without this, a page recompute could
        # overwrite an issue that landed in between.
        self._lock = threading.RLock()

    # -- confirmations -----------------------------------------------------
    def load_confirmations(self) -> list[Confirmation]:
        raw = _read_json(self.confirmations_path, [])
        if not isinstance(raw, list):
            raise StateError("confirmations.json 应为数组")
        result: list[Confirmation] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("role") not in _ROLE_VALUES:
                continue
            try:
                role = Role(item["role"])
            except ValueError:
                continue
            result.append(
                Confirmation(
                    role=role,
                    subject=str(item.get("subject", "")),
                    decision=str(item.get("decision", "")),
                    fingerprint=str(item.get("fingerprint", "")),
                    note=str(item.get("note", "")),
                    confirmed_at=str(item.get("confirmed_at", "")),
                    payload=item.get("payload") or {},
                )
            )
        return result

    def save_confirmations(self, items: list[Confirmation]) -> None:
        payload = [
            {
                "role": item.role.value,
                "subject": item.subject,
                "decision": item.decision,
                "fingerprint": item.fingerprint,
                "note": item.note,
                "confirmed_at": item.confirmed_at,
                "payload": item.payload,
            }
            for item in items
        ]
        _atomic_write(self.confirmations_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def confirm(self, role: Role, decision: str, fingerprint: str, by: str, note: str = "") -> Confirmation:
        with self._lock:
            return self._confirm(role, decision, fingerprint, by, note)

    def _confirm(self, role: Role, decision: str, fingerprint: str, by: str, note: str) -> Confirmation:
        if decision not in ("confirmed", "rejected"):
            raise StateError("decision 只能是 confirmed 或 rejected")
        if not by.strip():
            raise StateError("确认人不能为空")
        if role is Role.FINANCE and decision == "confirmed" and not self.load_policy():
            # finance confirmation must carry an explicit posting policy
            raise StateError("财务确认前需先保存入账口径")
        entry = Confirmation(
            role=role,
            subject=role.value,
            decision=decision,
            fingerprint=fingerprint,
            note=note.strip()[:500],
            confirmed_at=_now(),
            payload={"by": by.strip()[:80]},
        )
        items = [c for c in self.load_confirmations() if c.role is not role]
        items.append(entry)
        self.save_confirmations(items)
        self.audit("confirm", {"role": role.value, "decision": decision, "fingerprint": fingerprint, "by": by})
        return entry

    # -- posting policy ----------------------------------------------------
    def load_policy(self) -> PostingPolicy | None:
        raw = _read_json(self.policy_path, None)
        if not isinstance(raw, dict):
            return None
        try:
            return PostingPolicy(
                sell_in_scope=str(raw.get("sell_in_scope", "all")),
                returns_restock=bool(raw.get("returns_restock", True)),
            )
        except ValueError:
            return None

    def save_policy(self, policy: PostingPolicy, by: str) -> None:
        with self._lock:
            return self._save_policy(policy, by)

    def _save_policy(self, policy: PostingPolicy, by: str) -> None:
        _atomic_write(
            self.policy_path,
            json.dumps(
                {
                    "sell_in_scope": policy.sell_in_scope,
                    "returns_restock": policy.returns_restock,
                    "saved_by": by,
                    "saved_at": _now(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        self.audit("posting-policy", {"sell_in_scope": policy.sell_in_scope, "returns_restock": policy.returns_restock, "by": by})

    # -- scenario ----------------------------------------------------------
    def load_scenario(self) -> Scenario:
        raw = _read_json(self.scenario_path, None)
        if not isinstance(raw, dict):
            return Scenario()
        defaults = Scenario()
        values = {}
        for field_name in defaults.__dataclass_fields__:
            if field_name in raw:
                values[field_name] = raw[field_name]
        try:
            return Scenario(**values)
        except (TypeError, ValueError):
            return defaults

    def save_scenario(self, scenario: Scenario, by: str) -> None:
        with self._lock:
            return self._save_scenario(scenario, by)

    def _save_scenario(self, scenario: Scenario, by: str) -> None:
        payload = {name: getattr(scenario, name) for name in scenario.__dataclass_fields__}
        _atomic_write(self.scenario_path, json.dumps(payload, ensure_ascii=False, indent=2))
        self.audit("scenario", {**payload, "by": by})

    # -- feeds -------------------------------------------------------------
    def load_feeds(self) -> tuple[list[FeedWindow], list[Issue]]:
        raw = _read_json(self.feeds_path, [])
        issues: list[Issue] = []
        feeds: list[FeedWindow] = []
        if not isinstance(raw, list):
            return feeds, [
                Issue(
                    code="feeds-shape",
                    message="feeds.json 应为数组，已忽略",
                    severity=IssueSeverity.WARNING,
                )
            ]
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
        return feeds, issues

    # -- corrections -------------------------------------------------------
    def load_corrections(self) -> list[Event]:
        raw = _read_json(self.corrections_path, [])
        if not isinstance(raw, list):
            raise StateError("corrections.json 应为数组")
        events: list[Event] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                kind = Kind(str(item["kind"]))
                events.append(
                    Event(
                        event=str(item["event"]),
                        store=str(item["store"]),
                        sku=str(item["sku"]),
                        kind=kind,
                        qty=int(item["qty"]),
                        at=date.fromisoformat(str(item["at"])),
                        source="correction",
                        corrected_of=str(item.get("corrected_of", "")),
                        posted_at=date.fromisoformat(item["posted_at"]) if item.get("posted_at") else None,
                        actor=str(item.get("actor", "")),
                        note=str(item.get("note", "")),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return events

    def append_correction(self, event: Event) -> None:
        with self._lock:
            return self._append_correction(event)

    def _append_correction(self, event: Event) -> None:
        if not (valid_ident(event.event) and valid_ident(event.store) and valid_ident(event.sku)):
            raise StateError("更正事件的标识不合法")
        if event.qty < 0:
            raise StateError("更正数量不能为负")
        if event.corrected_of and not valid_ident(event.corrected_of):
            raise StateError("corrected_of 标识不合法")
        existing = self.load_corrections()
        if any(item.event == event.event for item in existing):
            raise StateError(f"更正事件 {event.event} 已存在")
        record = {
            "event": event.event,
            "store": event.store,
            "sku": event.sku,
            "kind": event.kind.value,
            "qty": event.qty,
            "at": event.at.isoformat(),
            "corrected_of": event.corrected_of,
            "posted_at": (event.posted_at or date.today()).isoformat(),
            "actor": event.actor[:80],
            "note": event.note[:500],
        }
        existing.append(event)
        payload = [
            {
                "event": item.event,
                "store": item.store,
                "sku": item.sku,
                "kind": item.kind.value,
                "qty": item.qty,
                "at": item.at.isoformat(),
                "corrected_of": item.corrected_of,
                "posted_at": (item.posted_at or date.today()).isoformat(),
                "actor": item.actor,
                "note": item.note,
            }
            for item in existing
        ]
        _atomic_write(self.corrections_path, json.dumps(payload, ensure_ascii=False, indent=2))
        self.audit("correction", record)

    # -- proposals ---------------------------------------------------------
    def load_proposals(self) -> list[Proposal]:
        raw = _read_json(self.proposals_path, [])
        if not isinstance(raw, list):
            raise StateError("proposals.json 应为数组")
        proposals: list[Proposal] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                proposals.append(
                    Proposal(
                        proposal_id=str(item["proposal_id"]),
                        store=str(item["store"]),
                        sku=str(item["sku"]),
                        qty=int(item["qty"]),
                        status=str(item["status"]),
                        created_at=str(item.get("created_at", "")),
                        updated_at=str(item.get("updated_at", "")),
                        issued_at=str(item.get("issued_at", "")),
                        basis=item.get("basis") or {},
                        risk=tuple(item.get("risk") or ()),
                        change_note=str(item.get("change_note", "")),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return proposals

    def save_proposals(self, proposals: list[Proposal]) -> None:
        payload = [
            {
                "proposal_id": item.proposal_id,
                "store": item.store,
                "sku": item.sku,
                "qty": item.qty,
                "status": item.status,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "issued_at": item.issued_at,
                "basis": item.basis,
                "risk": list(item.risk),
                "change_note": item.change_note,
            }
            for item in proposals
        ]
        _atomic_write(self.proposals_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def upsert_draft(
        self,
        store: str,
        sku: str,
        qty: int,
        basis: dict,
        risk: tuple[str, ...],
        now: str | None = None,
        note: str = "",
    ) -> Proposal:
        with self._lock:
            return self._upsert_draft(store, sku, qty, basis, risk, now, note)

    def _upsert_draft(
        self,
        store: str,
        sku: str,
        qty: int,
        basis: dict,
        risk: tuple[str, ...],
        now: str | None,
        note: str,
    ) -> Proposal:
        """Create or refresh the single draft for (store, sku).

        An ``issued`` row for the same store/SKU is left untouched: recompute may
        suggest a different quantity, but a promise already sent to the dealer is
        not silently rewritten.
        """
        now = now or _now()
        proposals = self.load_proposals()
        for item in proposals:
            if item.store == store and item.sku == sku and item.status == "issued":
                return item
        for index, item in enumerate(proposals):
            if item.store == store and item.sku == sku and item.status == "draft":
                proposals[index] = Proposal(
                    proposal_id=item.proposal_id,
                    store=store,
                    sku=sku,
                    qty=qty,
                    status="draft",
                    created_at=item.created_at,
                    updated_at=now,
                    basis=basis,
                    risk=risk,
                    change_note=note
                    or (
                        "补报/更正后已按新证据重算"
                        if item.basis and item.basis != basis
                        else item.change_note
                    ),
                )
                self.save_proposals(proposals)
                return proposals[index]
        proposal = Proposal(
            proposal_id=f"R-{store}-{sku}",
            store=store,
            sku=sku,
            qty=qty,
            status="draft",
            created_at=now,
            updated_at=now,
            basis=basis,
            risk=risk,
            change_note="首次生成",
        )
        proposals.append(proposal)
        self.save_proposals(proposals)
        return proposal

    def persist_drafts(self, computed: list[Proposal]) -> None:
        """Write computed drafts under the lock, leaving issued rows untouched.

        Issued rows are re-read from disk here so a stale page render cannot
        overwrite an issue that committed after the render began.
        """
        with self._lock:
            stored = self.load_proposals()
            issued = {(p.store, p.sku) for p in stored if p.status == "issued"}
            merged = [
                proposal
                for proposal in computed
                if proposal.proposal_id not in {p.proposal_id for p in stored if p.status == "issued"}
                and (proposal.store, proposal.sku) not in issued
            ]
            self.save_proposals(sorted(merged + [p for p in stored if p.status == "issued"], key=lambda p: (p.status, p.store, p.sku)))

    def reconcile_drafts(self, live_keys: set[tuple[str, str]]) -> None:
        with self._lock:
            return self._reconcile_drafts(live_keys)

    def _reconcile_drafts(self, live_keys: set[tuple[str, str]]) -> None:
        """Zero drafts whose (store, sku) left the current evidence set."""
        proposals = self.load_proposals()
        changed = False
        for index, item in enumerate(proposals):
            if item.status == "draft" and item.qty > 0 and (item.store, item.sku) not in live_keys:
                proposals[index] = Proposal(
                    proposal_id=item.proposal_id,
                    store=item.store,
                    sku=item.sku,
                    qty=0,
                    status="draft",
                    created_at=item.created_at,
                    updated_at=_now(),
                    basis=item.basis,
                    risk=(),
                    change_note="该门店/SKU 已不在当前复盘范围，草稿清零待复核",
                )
                changed = True
        if changed:
            self.save_proposals(proposals)

    def issue(self, proposal_id: str, by: str, note: str = "") -> Proposal:
        with self._lock:
            return self._issue(proposal_id, by, note)

    def _issue(self, proposal_id: str, by: str, note: str) -> Proposal:
        if not by.strip():
            raise StateError("发出承诺必须记录操作人")
        proposals = self.load_proposals()
        for index, item in enumerate(proposals):
            if item.proposal_id != proposal_id:
                continue
            if item.status == "issued":
                raise StateError(f"{proposal_id} 已发出，不能重复发出")
            if item.qty <= 0:
                raise StateError(f"{proposal_id} 数量为 0 或待补数据，不能发出承诺")
            proposals[index] = Proposal(
                proposal_id=item.proposal_id,
                store=item.store,
                sku=item.sku,
                qty=item.qty,
                status="issued",
                created_at=item.created_at,
                updated_at=_now(),
                issued_at=_now(),
                basis=item.basis,
                risk=item.risk,
                change_note=note[:500] or "已向经销商发出",
            )
            self.save_proposals(proposals)
            self.audit("issue", {"proposal_id": proposal_id, "by": by, "qty": item.qty})
            return proposals[index]
        raise StateError(f"找不到补货方案 {proposal_id}")

    def update_issued_risk(self, risks: dict[str, tuple[str, ...]]) -> None:
        with self._lock:
            return self._update_issued_risk(risks)

    def _update_issued_risk(self, risks: dict[str, tuple[str, ...]]) -> None:
        proposals = self.load_proposals()
        changed = False
        for index, item in enumerate(proposals):
            if item.status == "issued" and item.proposal_id in risks:
                new_risk = risks[item.proposal_id]
                if new_risk != item.risk:
                    proposals[index] = Proposal(
                        proposal_id=item.proposal_id,
                        store=item.store,
                        sku=item.sku,
                        qty=item.qty,
                        status=item.status,
                        created_at=item.created_at,
                        updated_at=_now(),
                        issued_at=item.issued_at,
                        basis=item.basis,
                        risk=new_risk,
                        change_note=item.change_note,
                    )
                    changed = True
        if changed:
            self.save_proposals(proposals)

    # -- audit -------------------------------------------------------------
    def audit(self, action: str, detail: dict) -> None:
        line = json.dumps({"at": _now(), "action": action, "detail": detail}, ensure_ascii=False)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_audit(self, limit: int = 200) -> list[dict]:
        if not self.audit_path.exists():
            return []
        entries = []
        with self.audit_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries[-limit:]
