"""Domain logic: version registration, license changes and permission checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple

from .database import Database
from .signing import (
    FORMAT_VERSION,
    SIGNING_ALGORITHM,
    KeyStore,
    SigningKeyUnavailableError,
    build_signed_proof,
)

# Canonical UTC timestamp used for storage and lexicographic comparison in SQL.
# Example: 2026-03-01T08:30:00.000000Z
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_canonical(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区信息")
    return value.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def parse_canonical(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def content_hash(version_id: str, effective_at: str, allowed_uses: list[str]) -> str:
    """Stable hash of a change's semantic content.

    Duplicate submissions are defined as the same change id *and* the same
    semantic content (version, effective instant, allowed-use set). Order of
    the allowed uses is irrelevant; duplicates inside it are ignored.
    """
    payload = {
        "version_id": version_id,
        "effective_at": effective_at,
        "allowed_uses": sorted(set(allowed_uses)),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def batch_content_hash(items: list[dict[str, Any]]) -> str:
    """Stable hash of a batch's ordered, normalised change list.

    A batch retry is identical only when the list is the same after the
    single-change normalisation (trimmed/deduplicated uses, UTC instants) in
    the SAME order. Explicitly omitted change ids are normalised to null.
    """
    payload = [
        {
            "change_id": item["change_id"],
            "version_id": item["version_id"],
            "effective_at": item["effective_at"],
            "allowed_uses": sorted(set(item["allowed_uses"])),
        }
        for item in items
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -- domain errors ---------------------------------------------------------


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


class InvalidTimeError(ServiceError):
    status_code = 422
    code = "invalid_time"


class InvalidIntervalError(ServiceError):
    status_code = 422
    code = "invalid_interval"


class SlotNotPublishedError(NotFoundError):
    """A slot has no published version at the requested historical instant."""

    code = "slot_not_published"


class AdmissionDeniedError(ServiceError):
    """A delivery manifest is not continuously allowed over its whole window.

    Maps to HTTP 403. Unlike every other validation failure this is the
    evaluated business outcome: ``failures`` lists one object per failed entry
    (its first failing half-open interval and the blocking reasons). A denial
    is a read-only outcome — the caller's transaction must roll back and no
    admission row is persisted.
    """

    status_code = 403
    code = "admission_denied"

    def __init__(
        self,
        message: str,
        failures: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details)
        self.failures = failures or []


# -- serialisation ---------------------------------------------------------


def _change_out(
    row: dict[str, Any], revocation: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "change_id": row["change_id"],
        "version_id": row["version_id"],
        "effective_at": parse_canonical(row["effective_at"]),
        "allowed_uses": list(row["allowed_uses"]),
        "content_hash": row["content_hash"],
        "created_at": parse_canonical(row["created_at"]),
        "revocation": _revocation_out(revocation) if revocation is not None else None,
    }


def _revocation_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "revocation_id": row["revocation_id"],
        "change_id": row["change_id"],
        "reason": row["reason"],
        "registered_at": parse_canonical(row["created_at"]),
    }


def _freeze_decision(state: dict[str, Any]) -> str:
    """Serialise an evaluated decision (datetimes -> canonical UTC) to JSON."""
    payload = {
        "allowed": state["allowed"],
        "closure": list(state["closure"]),
        "basis": [
            {
                "version_id": entry["version_id"],
                "change_id": entry["change_id"],
                "effective_at": to_canonical(entry["effective_at"]),
                "allowed_uses": list(entry["allowed_uses"]),
            }
            for entry in state["basis"]
        ],
        "blockers": [
            {
                "version_id": entry["version_id"],
                "reason": entry["reason"],
                "change_id": entry.get("change_id"),
                "effective_at": to_canonical(entry["effective_at"])
                if entry.get("effective_at") is not None
                else None,
                "allowed_uses": list(entry.get("allowed_uses", ())),
            }
            for entry in state["blockers"]
        ],
        "allowed_uses_intersection": list(state["allowed_uses_intersection"]),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _thaw_decision(blob: str) -> dict[str, Any]:
    """Inverse of :func:`_freeze_decision` for stored voucher history."""
    data = json.loads(blob)
    return {
        "allowed": data["allowed"],
        "closure": list(data["closure"]),
        "basis": [
            {
                "version_id": entry["version_id"],
                "change_id": entry["change_id"],
                "effective_at": parse_canonical(entry["effective_at"]),
                "allowed_uses": list(entry["allowed_uses"]),
            }
            for entry in data["basis"]
        ],
        "blockers": _blockers_from_jsonable(data["blockers"]),
        "allowed_uses_intersection": list(data["allowed_uses_intersection"]),
    }


def _blockers_jsonable(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serialise blocker entries (datetimes -> canonical UTC) for storage."""
    return [
        {
            "version_id": entry["version_id"],
            "reason": entry["reason"],
            "change_id": entry.get("change_id"),
            "effective_at": to_canonical(entry["effective_at"])
            if entry.get("effective_at") is not None
            else None,
            "allowed_uses": list(entry.get("allowed_uses", ())),
        }
        for entry in blockers
    ]


def _blockers_from_jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inverse of :func:`_blockers_jsonable`."""
    return [
        {
            "version_id": entry["version_id"],
            "reason": entry["reason"],
            "change_id": entry.get("change_id"),
            "effective_at": parse_canonical(entry["effective_at"])
            if entry.get("effective_at") is not None
            else None,
            "allowed_uses": list(entry.get("allowed_uses", ())),
        }
        for entry in rows
    ]


def _freeze_baseline(state: dict[str, Any]) -> str:
    """Freeze a subscription baseline: the allow flag plus every blocker.

    A baseline captures the then-current result atomically with subscription
    creation; flip events afterwards each carry before/after blocker lists, so
    the baseline snapshot uses the same canonical JSON encoding.
    """
    return json.dumps(
        {"allowed": state["allowed"], "blockers": _blockers_jsonable(state["blockers"])},
        ensure_ascii=False,
        sort_keys=True,
    )


def _thaw_baseline(blob: str) -> dict[str, Any]:
    """Inverse of :func:`_freeze_baseline`."""
    data = json.loads(blob)
    return {
        "allowed": bool(data["allowed"]),
        "blockers": _blockers_from_jsonable(data["blockers"]),
    }


def _freeze_batch_result(result: dict[str, Any]) -> str:
    """Freeze a batch import's first result verbatim (datetimes -> canonical).

    The frozen snapshot is the single source returned by every identical
    retry, so the response stays byte-for-byte stable even if a change is
    revoked or otherwise evolves later (change rows themselves remain
    immutable; only their revocation marker is live).
    """
    payload = {
        "batch_id": result["batch_id"],
        "created_at": to_canonical(result["created_at"]),
        "change_count": result["change_count"],
        "new_event_count": result["new_event_count"],
        "changes": [
            {
                "index": entry["index"],
                "replayed": entry["replayed"],
                "change": {
                    "change_id": entry["change"]["change_id"],
                    "version_id": entry["change"]["version_id"],
                    "effective_at": to_canonical(entry["change"]["effective_at"]),
                    "allowed_uses": list(entry["change"]["allowed_uses"]),
                    "content_hash": entry["change"]["content_hash"],
                    "created_at": to_canonical(entry["change"]["created_at"]),
                    "revocation": None,
                },
                "new_event_count": entry["new_event_count"],
            }
            for entry in result["changes"]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _thaw_batch_result(blob: str) -> dict[str, Any]:
    """Inverse of :func:`_freeze_batch_result` for replay/GET responses."""
    data = json.loads(blob)
    return {
        "batch_id": data["batch_id"],
        "created_at": parse_canonical(data["created_at"]),
        "change_count": int(data["change_count"]),
        "new_event_count": int(data["new_event_count"]),
        "changes": [
            {
                "index": int(entry["index"]),
                "replayed": bool(entry["replayed"]),
                "change": {
                    "change_id": entry["change"]["change_id"],
                    "version_id": entry["change"]["version_id"],
                    "effective_at": parse_canonical(entry["change"]["effective_at"]),
                    "allowed_uses": list(entry["change"]["allowed_uses"]),
                    "content_hash": entry["change"]["content_hash"],
                    "created_at": parse_canonical(entry["change"]["created_at"]),
                    "revocation": None,
                },
                "new_event_count": int(entry["new_event_count"]),
            }
            for entry in data["changes"]
        ],
    }


def release_content_hash(effective_at: str, slots: list[dict[str, Any]]) -> str:
    """Stable hash of a release's effective instant and ordered slot list.

    A retry is identical only when the normalised slots match in the SAME
    request order with the same trimmed slot names, target versions and
    expected revisions, and when the effective instant converts to the same
    UTC instant.
    """
    payload = {
        "effective_at": effective_at,
        "slots": [
            {
                "position": entry["position"],
                "slot": entry["slot"],
                "version_id": entry["version_id"],
                "expected_revision": entry["expected_revision"],
            }
            for entry in slots
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _freeze_release_result(result: dict[str, Any]) -> str:
    """Freeze a publish's first result verbatim (datetimes -> canonical UTC).

    The frozen snapshot is the single source returned by every identical
    retry, so the response stays byte-for-byte stable forever; release rows are
    immutable history regardless of later publishes.
    """
    payload = {
        "release_id": result["release_id"],
        "effective_at": to_canonical(result["effective_at"]),
        "created_at": to_canonical(result["created_at"]),
        "slot_count": result["slot_count"],
        "slots": [
            {
                "position": entry["position"],
                "slot": entry["slot"],
                "version_id": entry["version_id"],
                "expected_revision": entry["expected_revision"],
                "revision": entry["revision"],
            }
            for entry in result["slots"]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _thaw_release_result(blob: str) -> dict[str, Any]:
    """Inverse of :func:`_freeze_release_result` for replay/GET responses."""
    data = json.loads(blob)
    return {
        "release_id": data["release_id"],
        "effective_at": parse_canonical(data["effective_at"]),
        "created_at": parse_canonical(data["created_at"]),
        "slot_count": int(data["slot_count"]),
        "slots": [
            {
                "position": int(entry["position"]),
                "slot": entry["slot"],
                "version_id": entry["version_id"],
                "expected_revision": int(entry["expected_revision"]),
                "revision": int(entry["revision"]),
            }
            for entry in data["slots"]
        ],
    }


def admission_content_hash(
    start_value: str, end_value: str, entries: list[dict[str, Any]]
) -> str:
    """Stable hash of an admission request's window and ordered entry list.

    A retry is identical only when the window converts to the same UTC
    instants and the ordered entries match after normalisation: trimmed
    slot/use/delivery references, the explicit ``version``/``slot`` kind and
    the same trimmed use. Request ORDER is part of the content (the entries are
    an ordered manifest).
    """
    payload = {
        "start": start_value,
        "end": end_value,
        "entries": [
            {
                "position": entry["position"],
                "kind": entry["kind"],
                "ref": entry["ref"],
                "use": entry["use"],
            }
            for entry in entries
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _freeze_admission_result(result: dict[str, Any]) -> str:
    """Freeze an admission's first result verbatim (datetimes -> canonical UTC).

    The frozen snapshot is the single source returned by every identical
    retry and by GET, so the response stays byte-for-byte stable even if a slot
    is later published/re-pointed, a license changes or a change is revoked.
    """
    payload = {
        "delivery_id": result["delivery_id"],
        "created_at": to_canonical(result["created_at"]),
        "start": to_canonical(result["start"]),
        "end": to_canonical(result["end"]),
        "entry_count": result["entry_count"],
        "entries": [
            {
                "position": entry["position"],
                "kind": entry["kind"],
                "ref": entry["ref"],
                "use": entry["use"],
                "resolution": _freeze_admission_resolution(entry["resolution"]),
                "segments": [
                    _freeze_admission_segment(segment)
                    for segment in entry["segments"]
                ],
            }
            for entry in result["entries"]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _freeze_admission_resolution(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    if resolution is None:
        return None
    return {
        "kind": resolution["kind"],
        "ref": resolution["ref"],
        "version_id": resolution["version_id"],
        "release_id": resolution.get("release_id"),
        "revision": resolution.get("revision"),
        "effective_at": to_canonical(resolution["effective_at"]),
    }


def _freeze_admission_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": to_canonical(segment["start"]),
        "end": to_canonical(segment["end"]),
        "allowed": bool(segment["allowed"]),
        "resolution": _freeze_admission_resolution(segment["resolution"]),
        "version_id": segment["version_id"],
        "closure": list(segment["closure"]),
        "basis": [
            {
                "version_id": entry["version_id"],
                "change_id": entry["change_id"],
                "effective_at": to_canonical(entry["effective_at"]),
                "allowed_uses": list(entry["allowed_uses"]),
            }
            for entry in segment["basis"]
        ],
        "blockers": _blockers_jsonable(segment["blockers"]),
        "allowed_uses_intersection": list(segment["allowed_uses_intersection"]),
    }


def _thaw_admission_result(blob: str) -> dict[str, Any]:
    """Inverse of :func:`_freeze_admission_result` for replay/GET responses."""
    data = json.loads(blob)
    return {
        "delivery_id": data["delivery_id"],
        "created_at": parse_canonical(data["created_at"]),
        "start": parse_canonical(data["start"]),
        "end": parse_canonical(data["end"]),
        "entry_count": int(data["entry_count"]),
        "entries": [
            {
                "position": int(entry["position"]),
                "kind": entry["kind"],
                "ref": entry["ref"],
                "use": entry["use"],
                "resolution": _thaw_admission_resolution(entry["resolution"]),
                "segments": [
                    _thaw_admission_segment(segment) for segment in entry["segments"]
                ],
            }
            for entry in data["entries"]
        ],
    }


def _thaw_admission_resolution(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    if resolution is None:
        return None
    return {
        "kind": resolution["kind"],
        "ref": resolution["ref"],
        "version_id": resolution["version_id"],
        "release_id": resolution.get("release_id"),
        "revision": resolution.get("revision"),
        "effective_at": parse_canonical(resolution["effective_at"]),
    }


def _thaw_admission_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": parse_canonical(segment["start"]),
        "end": parse_canonical(segment["end"]),
        "allowed": bool(segment["allowed"]),
        "resolution": _thaw_admission_resolution(segment["resolution"]),
        "version_id": segment["version_id"],
        "closure": list(segment["closure"]),
        "basis": [
            {
                "version_id": entry["version_id"],
                "change_id": entry["change_id"],
                "effective_at": parse_canonical(entry["effective_at"]),
                "allowed_uses": list(entry["allowed_uses"]),
            }
            for entry in segment["basis"]
        ],
        "blockers": _blockers_from_jsonable(segment["blockers"]),
        "allowed_uses_intersection": list(segment["allowed_uses_intersection"]),
    }


def _admission_failure_from_segment(
    entry: dict[str, Any], segment: dict[str, Any]
) -> dict[str, Any]:
    """Build one failed entry report from its first failing segment."""
    blockers = segment["blockers"]
    return {
        "position": entry["position"],
        "kind": entry["kind"],
        "ref": entry["ref"],
        "use": entry["use"],
        "failure_start": segment["start"],
        "failure_end": segment["end"],
        "reason": blockers[0]["reason"] if blockers else "not_allowed",
        "blockers": blockers,
    }


def _proof_payload(voucher_row: dict[str, Any]) -> dict[str, Any]:
    """Build the signed payload for a voucher proof.

    The payload is sourced from the immutable stored snapshot
    (``decision_json``): every timestamp is already canonical UTC text and
    every list keeps its first-decision order, so the payload — and hence the
    signature — is identical whenever the same voucher is exported.
    """
    decision = json.loads(voucher_row["decision_json"])
    return {
        "format_version": FORMAT_VERSION,
        "voucher_id": voucher_row["voucher_id"],
        "version_id": voucher_row["version_id"],
        "use": voucher_row["use_name"],
        "at": voucher_row["at_value"],
        "registered_at": voucher_row["registered_at"],
        "allowed": bool(decision["allowed"]),
        "closure": list(decision["closure"]),
        "basis": [
            {
                "version_id": entry["version_id"],
                "change_id": entry["change_id"],
                "effective_at": entry["effective_at"],
                "allowed_uses": list(entry["allowed_uses"]),
            }
            for entry in decision["basis"]
        ],
        "blockers": [
            {
                "version_id": entry["version_id"],
                "reason": entry["reason"],
                "change_id": entry.get("change_id"),
                "effective_at": entry.get("effective_at"),
                "allowed_uses": list(entry.get("allowed_uses", ())),
            }
            for entry in decision["blockers"]
        ],
        "allowed_uses_intersection": list(decision["allowed_uses_intersection"]),
    }


def _version_out(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    changes = []
    for change in db.list_changes(row["version_id"]):
        revocation = db.get_revocation_of_change(change["change_id"])
        changes.append(_change_out(change, revocation))
    return {
        "version_id": row["version_id"],
        "created_at": parse_canonical(row["created_at"]),
        "note": row["note"],
        "parent_ids": db.get_parents(row["version_id"]),
        "changes": changes,
    }


# -- core service ----------------------------------------------------------


class LicenseService:
    def __init__(self, db: Database, key_store: KeyStore | None = None) -> None:
        self.db = db
        self.key_store = key_store

    # -- versions ---------------------------------------------------------

    def register_version(
        self,
        version_id: str | None,
        parent_ids: list[str],
        note: str = "",
    ) -> dict[str, Any]:
        """Register a new version and immutably pin its parent references."""
        version_id = version_id or f"v-{uuid.uuid4().hex}"

        with self.db.lock:
            missing = [pid for pid in parent_ids if self.db.get_version(pid) is None]
            if missing:
                raise NotFoundError(
                    "以下父版本不存在，引用前必须先登记",
                    {"missing_parent_ids": ",".join(missing)},
                )
            if self.db.get_version(version_id) is not None:
                raise ConflictError("版本标识已存在", {"version_id": version_id})

            created = to_canonical(now_utc())
            try:
                self.db.begin()
                self.db.insert_version(version_id, created, note)
                for position, pid in enumerate(parent_ids):
                    self.db.insert_parent(version_id, pid, position)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            return _version_out(self.db, self.db.get_version(version_id))  # type: ignore[arg-type]

    def get_version(self, version_id: str) -> dict[str, Any]:
        row = self.db.get_version(version_id)
        if row is None:
            raise NotFoundError("版本不存在", {"version_id": version_id})
        return _version_out(self.db, row)

    def list_versions(self) -> list[dict[str, Any]]:
        return [_version_out(self.db, row) for row in self.db.list_versions()]

    def ordered_closure(self, version_id: str) -> list[str]:
        """Return self plus every transitive ancestor, deduplicated, BFS order.

        Parent edges are frozen at registration time, so the graph is a DAG.
        """
        order: list[str] = []
        seen: set[str] = set()
        queue: deque[str] = deque([version_id])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            order.append(current)
            for parent in self.db.get_parents(current):
                if parent not in seen:
                    queue.append(parent)
        return order

    # -- license changes --------------------------------------------------

    def submit_change(
        self,
        change_id: str | None,
        version_id: str,
        effective_at: datetime,
        allowed_uses: list[str],
    ) -> tuple[dict[str, Any], bool]:
        """Submit a license change.

        Returns ``(change, replayed)``. A repeated submission with the same
        ``change_id`` and identical content is a replay and returns the stored
        result unchanged. Same id with different content, or two different
        changes at the same (version, effective instant), are conflicts.
        """
        if effective_at.tzinfo is None:
            raise InvalidTimeError("生效时刻必须带时区信息")

        change_id = change_id or f"c-{uuid.uuid4().hex}"
        effective = to_canonical(effective_at)
        uses = sorted(set(u.strip() for u in allowed_uses if u.strip()))
        digest = content_hash(version_id, effective, uses)

        with self.db.lock:
            if self.db.get_version(version_id) is None:
                raise NotFoundError("版本不存在，无法为其登记许可变更", {"version_id": version_id})

            existing = self.db.get_change(change_id)
            if existing is not None:
                same = (
                    existing["version_id"] == version_id
                    and existing["effective_at"] == effective
                    and existing["content_hash"] == digest
                )
                if not same:
                    raise ConflictError(
                        "相同变更标识已存在但内容不同",
                        {
                            "change_id": change_id,
                            "submitted_version_id": version_id,
                            "existing_version_id": existing["version_id"],
                            "submitted_effective_at": effective,
                            "existing_effective_at": existing["effective_at"],
                        },
                    )
                # Replays never touch the revocation table: re-submitting a
                # voided change returns the same record but cannot un-void it.
                revocation = self.db.get_revocation_of_change(change_id)
                return _change_out(existing, revocation), True

            twin = self.db.change_at_same_time(version_id, effective)
            if twin is not None and twin["change_id"] != change_id:
                raise ConflictError(
                    "该版本在同一生效时刻已存在不同变更",
                    {
                        "version_id": version_id,
                        "effective_at": effective,
                        "existing_change_id": twin["change_id"],
                        "submitted_change_id": change_id,
                    },
                )

            created = to_canonical(now_utc())
            try:
                self.db.begin()
                self.db.insert_change(change_id, version_id, effective, uses, digest, created)
                # The snapshot is captured in the SAME write transaction after
                # the insert, so it already contains the new change; every
                # subscription flip it appends commits atomically with the
                # change itself.
                snapshot = self._capture_snapshot(manage_transaction=False)
                self._propagate_subscription_flips(
                    snapshot=snapshot,
                    root_version_id=version_id,
                    trigger_kind="license_change",
                    trigger_id=change_id,
                    change_id=change_id,
                    effective_at=effective,
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                # Concurrent writers may have won the unique constraint; map
                # both id and (version,time) collisions onto clear conflicts.
                existing = self.db.get_change(change_id)
                if existing is not None:
                    if existing["content_hash"] == digest:
                        revocation = self.db.get_revocation_of_change(change_id)
                        return _change_out(existing, revocation), True
                    raise ConflictError(
                        "相同变更标识已存在但内容不同", {"change_id": change_id}
                    ) from None
                twin = self.db.change_at_same_time(version_id, effective)
                if twin is not None:
                    raise ConflictError(
                        "该版本在同一生效时刻已存在不同变更",
                        {
                            "version_id": version_id,
                            "effective_at": effective,
                            "existing_change_id": twin["change_id"],
                        },
                    ) from None
                raise

            stored = self.db.get_change(change_id)
            assert stored is not None
            return _change_out(stored), False

    def get_change(self, change_id: str) -> dict[str, Any]:
        row = self.db.get_change(change_id)
        if row is None:
            raise NotFoundError("变更不存在", {"change_id": change_id})
        # A revoked change remains fully queryable: its voiding is reported on
        # the same record rather than hiding it.
        revocation = self.db.get_revocation_of_change(change_id)
        return _change_out(row, revocation)

    # -- batch license changes --------------------------------------------

    def submit_changes_batch(
        self,
        batch_id: str | None,
        items: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Import an ordered, non-empty list of license changes in one batch.

        Returns ``(batch_result, replayed)``. Every item reuses the single
        change semantics: explicit change id, version, timezone-aware
        effective instant and the allowed-use full set. The ENTIRE batch is
        rejected (no change row, no batch row, no event) when any item is
        illegal: duplicate change id inside the batch, a change id that
        already exists with different content, a different change at the same
        (version, effective instant) either inside the batch or in storage, or
        an unknown version.

        All items apply in REQUEST ORDER inside ONE write transaction; after
        each insert the affected subscriptions are re-evaluated against a
        snapshot that already contains the earlier items of this batch, so a
        later item can flip a subscription back — both flips append their own
        events with consecutive per-subscription sequence numbers.

        A retry with the same ``batch_id`` and an identical normalised list
        returns the frozen first result without applying or replaying
        anything; the same id with different content is a conflict.
        """
        if not items:
            # The HTTP schema enforces this too; keep the service contract.
            raise ServiceError("变更列表不能为空")

        batch_id = batch_id or f"b-{uuid.uuid4().hex}"

        # Normalise every item exactly like a single submission; this hash (and
        # every validation below) therefore matches the single-change rules.
        normalised: list[dict[str, Any]] = []
        for pos, item in enumerate(items):
            effective_dt = item["effective_at"]
            if not isinstance(effective_dt, datetime) or effective_dt.tzinfo is None:
                raise InvalidTimeError(
                    f"第 {pos} 条变更的生效时刻必须带时区信息",
                    {"index": str(pos)},
                )
            effective = to_canonical(effective_dt)
            # Same normalisation as submit_change: trim/drop blank uses,
            # deduplicate (an empty set is a legal deny-all change); ids are
            # taken verbatim (a blank/None id triggers server generation).
            uses = sorted({u.strip() for u in item["allowed_uses"] if u.strip()})
            version_id = item["version_id"]
            change_id = item.get("change_id") or None
            normalised.append(
                {
                    "index": pos,
                    "change_id": change_id,
                    "version_id": version_id,
                    "effective_at": effective,
                    "allowed_uses": uses,
                    "digest": content_hash(version_id, effective, uses),
                }
            )

        digest = batch_content_hash(normalised)

        with self.db.lock:
            existing_batch = self.db.get_batch(batch_id)
            if existing_batch is not None:
                if existing_batch["content_hash"] != digest:
                    raise ConflictError(
                        "同一批次标识已存在但变更列表内容不同（批次标识不可复用指向不同内容）",
                        {"batch_id": batch_id},
                    )
                # An identical retry returns the frozen first result; nothing
                # is applied again and no event is ever replayed.
                return _thaw_batch_result(existing_batch["result_json"]), True

            # Whole-batch pre-validation: every illegal item rejects the batch
            # before the write transaction inserts anything.
            seen_change_ids: set[str] = set()
            seen_version_times: set[tuple[str, str]] = set()
            missing_versions: list[str] = []
            for entry in normalised:
                if entry["change_id"] is not None:
                    if entry["change_id"] in seen_change_ids:
                        raise ConflictError(
                            "批次内变更标识重复，整批拒绝",
                            {"batch_id": batch_id, "change_id": entry["change_id"],
                             "index": str(entry["index"])},
                        )
                    seen_change_ids.add(entry["change_id"])
                version_time = (entry["version_id"], entry["effective_at"])
                if version_time in seen_version_times:
                    raise ConflictError(
                        "批次内同一版本在同一生效时刻存在不同变更，整批拒绝",
                        {"batch_id": batch_id, "version_id": entry["version_id"],
                         "effective_at": entry["effective_at"],
                         "index": str(entry["index"])},
                    )
                seen_version_times.add(version_time)
                if self.db.get_version(entry["version_id"]) is None:
                    missing_versions.append(entry["version_id"])
            if missing_versions:
                raise NotFoundError(
                    "批次内存在未知版本，整批拒绝",
                    {"batch_id": batch_id,
                     "missing_version_ids": ",".join(sorted(set(missing_versions)))},
                )

            # Conflict checks against stored changes.
            for entry in normalised:
                change_id = entry["change_id"]
                if change_id is not None:
                    existing_change = self.db.get_change(change_id)
                    if existing_change is not None:
                        same = (
                            existing_change["version_id"] == entry["version_id"]
                            and existing_change["effective_at"] == entry["effective_at"]
                            and existing_change["content_hash"] == entry["digest"]
                        )
                        if not same:
                            raise ConflictError(
                                "相同变更标识已存在但内容不同，整批拒绝",
                                {"batch_id": batch_id, "change_id": change_id,
                                 "index": str(entry["index"])},
                            )
                # A different stored change at the same (version, instant)
                # conflicts exactly as in the single-submit path — even when
                # the allowed-use set matches. The only non-conflict is the
                # entry replaying the very change sitting at that instant.
                twin = self.db.change_at_same_time(
                    entry["version_id"], entry["effective_at"]
                )
                if twin is not None and twin["change_id"] != change_id:
                    raise ConflictError(
                        "该版本在同一生效时刻已存在不同变更，整批拒绝",
                        {"batch_id": batch_id, "version_id": entry["version_id"],
                         "effective_at": entry["effective_at"],
                         "existing_change_id": twin["change_id"],
                         "submitted_change_id": change_id,
                         "index": str(entry["index"])},
                    )

            created = to_canonical(now_utc())
            try:
                self.db.begin()
                # A concurrent first-writer may have won the batch id under
                # contention; re-check inside the write transaction.
                raced_batch = self.db.get_batch(batch_id)
                if raced_batch is not None:
                    if raced_batch["content_hash"] != digest:
                        raise ConflictError(
                            "同一批次标识已存在但变更列表内容不同（批次标识不可复用指向不同内容）",
                            {"batch_id": batch_id},
                        )
                    self.db.rollback()
                    return _thaw_batch_result(raced_batch["result_json"]), True

                results: list[dict[str, Any]] = []
                total_new_events = 0
                for entry in normalised:
                    change_id = entry["change_id"]
                    version_id = entry["version_id"]
                    effective = entry["effective_at"]
                    uses = entry["allowed_uses"]
                    item_digest = entry["digest"]

                    stored = (
                        self.db.get_change(change_id)
                        if change_id is not None
                        else None
                    )
                    if stored is not None:
                        # Passed pre-validation: same id AND identical content.
                        # A replay changes nothing and appends no events.
                        item_replayed = True
                        item_events = 0
                    else:
                        # A new (possibly auto-generated) change id. Same-time
                        # twins against earlier batch items were rejected
                        # pre-validation; insert and propagate flips.
                        if change_id is None:
                            change_id = f"c-{uuid.uuid4().hex}"
                        self.db.insert_change(
                            change_id, version_id, effective, uses,
                            item_digest, created,
                        )
                        # Re-pin the snapshot AFTER this insert within the SAME
                        # transaction: earlier batch items are visible to this
                        # item's subscription re-evaluation, so a flip caused by
                        # item N and flipped back by item N+1 yields two events.
                        snapshot = self._capture_snapshot(manage_transaction=False)
                        item_events = self._propagate_subscription_flips(
                            snapshot=snapshot,
                            root_version_id=version_id,
                            trigger_kind="license_change",
                            trigger_id=change_id,
                            change_id=change_id,
                            effective_at=effective,
                        )
                        item_replayed = False
                        stored = self.db.get_change(change_id)
                        assert stored is not None

                    total_new_events += item_events
                    results.append(
                        {
                            "index": entry["index"],
                            "replayed": item_replayed,
                            "change": _change_out(stored),
                            "new_event_count": item_events,
                        }
                    )

                batch_result = {
                    "batch_id": batch_id,
                    "created_at": parse_canonical(created),
                    "change_count": len(results),
                    "new_event_count": total_new_events,
                    "changes": results,
                }
                self.db.insert_batch(
                    batch_id=batch_id,
                    content_hash=digest,
                    created_at=created,
                    result_json=_freeze_batch_result(batch_result),
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raced_batch = self.db.get_batch(batch_id)
                if raced_batch is not None:
                    if raced_batch["content_hash"] == digest:
                        return _thaw_batch_result(raced_batch["result_json"]), True
                    raise ConflictError(
                        "同一批次标识已存在但变更列表内容不同（批次标识不可复用指向不同内容）",
                        {"batch_id": batch_id},
                    ) from None
                raise

            stored_batch = self.db.get_batch(batch_id)
            assert stored_batch is not None
            return _thaw_batch_result(stored_batch["result_json"]), False

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        """Return the frozen first result of a batch import by its id."""
        row = self.db.get_batch(batch_id)
        if row is None:
            raise NotFoundError(
                "许可变更批次不存在", {"batch_id": batch_id}
            )
        return _thaw_batch_result(row["result_json"])

    # -- change revocations ----------------------------------------------

    def revoke_change(
        self,
        change_id: str,
        revocation_id: str | None,
        reason: str,
    ) -> tuple[dict[str, Any], bool]:
        """Void a previously submitted license change.

        A revocation takes effect for every query instant immediately: single
        point checks, timeline segments and previews all ignore the revoked
        change afterwards. The original change row is never deleted. Returns
        ``(receipt, replayed)``.

        Idempotency follows the revocation id: retrying with the same
        ``revocation_id``, target change and reason returns the original
        receipt. Reusing an id with different content, or voiding the same
        change under a different id, is a conflict; an unknown target change
        is ``404``.
        """
        reason = reason.strip()
        if not reason:
            raise ServiceError("作废原因不能为空")
        revocation_id = revocation_id or f"r-{uuid.uuid4().hex}"

        with self.db.lock:
            target = self.db.get_change(change_id)
            if target is None:
                raise NotFoundError(
                    "目标许可变更不存在，无法作废", {"change_id": change_id}
                )

            by_id = self.db.get_revocation(revocation_id)
            by_change = self.db.get_revocation_of_change(change_id)

            if by_id is not None:
                same = (
                    by_id["change_id"] == change_id and by_id["reason"] == reason
                )
                if not same:
                    raise ConflictError(
                        "同一作废标识已存在但内容不同（作废标识不可复用指向不同内容）",
                        {
                            "revocation_id": revocation_id,
                            "submitted_change_id": change_id,
                            "existing_change_id": by_id["change_id"],
                            "submitted_reason": reason,
                            "existing_reason": by_id["reason"],
                        },
                    )
                return _revocation_out(by_id), True

            if by_change is not None:
                raise ConflictError(
                    "该许可变更已被另一作废标识作废，不能重复作废",
                    {
                        "change_id": change_id,
                        "existing_revocation_id": by_change["revocation_id"],
                        "submitted_revocation_id": revocation_id,
                    },
                )

            registered = to_canonical(now_utc())
            try:
                self.db.begin()
                self.db.insert_revocation(revocation_id, change_id, reason, registered)
                # Same transaction, post-insert snapshot: the voiding is
                # already visible, so flips are evaluated against the
                # post-revocation state and commit atomically with the receipt.
                snapshot = self._capture_snapshot(manage_transaction=False)
                self._propagate_subscription_flips(
                    snapshot=snapshot,
                    root_version_id=target["version_id"],
                    trigger_kind="revocation",
                    trigger_id=revocation_id,
                    change_id=change_id,
                    effective_at=target["effective_at"],
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                # Concurrent writers may have won one of the unique
                # constraints; map each race onto the same 409 semantics.
                by_id = self.db.get_revocation(revocation_id)
                if by_id is not None:
                    if by_id["change_id"] == change_id and by_id["reason"] == reason:
                        return _revocation_out(by_id), True
                    raise ConflictError(
                        "同一作废标识已存在但内容不同（作废标识不可复用指向不同内容）",
                        {"revocation_id": revocation_id},
                    ) from None
                by_change = self.db.get_revocation_of_change(change_id)
                if by_change is not None:
                    raise ConflictError(
                        "该许可变更已被另一作废标识作废，不能重复作废",
                        {
                            "change_id": change_id,
                            "existing_revocation_id": by_change["revocation_id"],
                        },
                    ) from None
                raise

            stored = self.db.get_revocation(revocation_id)
            assert stored is not None
            return _revocation_out(stored), False

    def get_revocation(self, revocation_id: str) -> dict[str, Any]:
        row = self.db.get_revocation(revocation_id)
        if row is None:
            raise NotFoundError(
                "作废回执不存在", {"revocation_id": revocation_id}
            )
        return _revocation_out(row)

    # -- permission query -------------------------------------------------

    def check_permission(self, version_id: str, use: str, at: datetime) -> dict[str, Any]:
        """Evaluate whether ``use`` is permitted for ``version_id`` at ``at``.

        For the queried version and every ancestor, take the latest change
        effective at or before ``at``; the permitted uses are the intersection
        over them. Any version without an effective change, or whose latest
        change does not include the requested use, blocks the request.
        """
        if at.tzinfo is None:
            raise InvalidTimeError("查询时刻必须带时区信息")
        use = use.strip()
        if not use:
            raise ServiceError("用途不能为空")

        if self.db.get_version(version_id) is None:
            raise NotFoundError("版本不存在", {"version_id": version_id})

        at_canonical = to_canonical(at)
        closure = self.ordered_closure(version_id)

        basis: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        intersection: set[str] | None = None
        missing_license = False

        for vid in closure:
            change = self.db.latest_change(vid, at_canonical)
            if change is None:
                missing_license = True
                blockers.append(
                    {"version_id": vid, "reason": "no_valid_license"}
                )
                continue

            allowed = set(change["allowed_uses"])
            basis.append(
                {
                    "version_id": vid,
                    "change_id": change["change_id"],
                    "effective_at": parse_canonical(change["effective_at"]),
                    "allowed_uses": sorted(allowed),
                }
            )
            intersection = allowed if intersection is None else intersection & allowed
            if use not in allowed:
                blockers.append(
                    {
                        "version_id": vid,
                        "reason": "use_not_allowed",
                        "change_id": change["change_id"],
                        "effective_at": parse_canonical(change["effective_at"]),
                        "allowed_uses": sorted(allowed),
                    }
                )

        # When some version has no effective license at all, no intersection
        # is meaningful (the request is denied regardless).
        if missing_license:
            result_intersection: list[str] = []
        else:
            result_intersection = sorted(intersection) if intersection is not None else []
        return {
            "version_id": version_id,
            "use": use,
            "at": at.astimezone(timezone.utc),
            "allowed": not blockers,
            "closure": closure,
            "basis": basis,
            "blockers": blockers,
            "allowed_uses_intersection": result_intersection,
        }

    # -- permission timeline (interval segmentation) ----------------------

    def check_permission_timeline(
        self,
        version_id: str,
        use: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Slice the half-open interval ``[start, end)`` into permission segments.

        All reads come from one pinned snapshot (the same mechanism as the
        change preview), so the ancestor closure and every change seen here
        belong to a single consistent view. Segment boundaries are the
        effective instants of the closure's non-revoked changes strictly
        inside the interval; a change exactly at ``start`` applies from the
        first segment on, a change exactly at ``end`` is not counted. Revoked
        changes are ignored for every segment. Segments are only split where
        the basis or the blocking information changes — adjacent identical
        segments are merged.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise InvalidTimeError("起止时刻必须带时区信息")
        use = use.strip()
        if not use:
            raise ServiceError("用途不能为空")

        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        if not start_utc < end_utc:
            raise InvalidIntervalError(
                "开始时刻必须严格早于结束时刻（半开区间 [start, end) 不能为空或倒置）",
                {
                    "start": to_canonical(start_utc),
                    "end": to_canonical(end_utc),
                },
            )

        with self.db.lock:
            snapshot = self._capture_snapshot()
        if version_id not in snapshot.versions:
            raise NotFoundError("版本不存在", {"version_id": version_id})

        start_c = to_canonical(start_utc)
        end_c = to_canonical(end_utc)
        closure = snapshot.closure_from(version_id)
        segments = _permission_segments(snapshot, version_id, use, start_c, end_c)

        return {
            "version_id": version_id,
            "use": use,
            "start": start_utc,
            "end": end_utc,
            "closure": closure,
            "segments": segments,
        }

    # -- permission vouchers (traceable frozen decisions) -----------------

    def issue_voucher(
        self,
        voucher_id: str | None,
        version_id: str,
        use: str,
        at: datetime,
    ) -> tuple[dict[str, Any], bool]:
        """Evaluate a permission decision and persist it as a frozen voucher.

        The first request for ``voucher_id`` applies the *existing* decision
        rules inside one write transaction against a single consistent
        snapshot, and stores the server-generated registration time together
        with the complete ancestor closure, basis, blockers and allowed-use
        intersection — allow and deny alike are recorded. Evaluation and
        storage are atomic: any failure rolls back and leaves no partial row.

        Stored vouchers are immutable history. A retry with the same voucher
        id, version, trimmed use and UTC-normalised instant returns the
        original voucher untouched; later license changes or revocations
        cannot rewrite it. Reusing the id against different inputs is a
        conflict. Unknown version, empty use or naive time error out before
        any row is written.
        """
        if at.tzinfo is None:
            raise InvalidTimeError("判定时刻必须带时区信息")
        use = use.strip()
        if not use:
            raise ServiceError("用途不能为空")
        voucher_id = voucher_id or f"p-{uuid.uuid4().hex}"
        at_canonical = to_canonical(at)

        with self.db.lock:
            existing = self.db.get_voucher(voucher_id)
            if existing is not None:
                same = (
                    existing["version_id"] == version_id
                    and existing["use_name"] == use
                    and existing["at_value"] == at_canonical
                )
                if not same:
                    raise ConflictError(
                        "同一凭证标识已存在但输入不同（凭证标识不可复用指向不同判定）",
                        {
                            "voucher_id": voucher_id,
                            "submitted_version_id": version_id,
                            "existing_version_id": existing["version_id"],
                            "submitted_use": use,
                            "existing_use": existing["use_name"],
                            "submitted_at": at_canonical,
                            "existing_at": existing["at_value"],
                        },
                    )
                return self._voucher_out(existing), True

            try:
                # BEGIN IMMEDIATE pins both a write lock and one snapshot: the
                # version lookup, closure evaluation and insert commit as one
                # atomic unit, so concurrent issuers cannot interleave.
                self.db.begin()
                if self.db.get_version(version_id) is None:
                    raise NotFoundError(
                        "版本不存在，无法签发判定凭证", {"version_id": version_id}
                    )
                snapshot = self._capture_snapshot(manage_transaction=False)
                state = _evaluate(snapshot, version_id, use, at_canonical, None)
                registered = to_canonical(now_utc())
                self.db.insert_voucher(
                    voucher_id=voucher_id,
                    version_id=version_id,
                    use_name=use,
                    at_value=at_canonical,
                    registered_at=registered,
                    allowed=state["allowed"],
                    decision_json=_freeze_decision(state),
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                # A concurrent writer may have won the same primary key: map
                # the race onto replay/conflict exactly like a sequential call.
                raced = self.db.get_voucher(voucher_id)
                if raced is not None:
                    same = (
                        raced["version_id"] == version_id
                        and raced["use_name"] == use
                        and raced["at_value"] == at_canonical
                    )
                    if same:
                        return self._voucher_out(raced), True
                    raise ConflictError(
                        "同一凭证标识已存在但输入不同（凭证标识不可复用指向不同判定）",
                        {
                            "voucher_id": voucher_id,
                            "submitted_version_id": version_id,
                            "existing_version_id": raced["version_id"],
                        },
                    ) from None
                raise

            stored = self.db.get_voucher(voucher_id)
            assert stored is not None
            return self._voucher_out(stored), False

    def get_voucher(self, voucher_id: str) -> dict[str, Any]:
        """Return the frozen first-saved decision for a voucher id."""
        row = self.db.get_voucher(voucher_id)
        if row is None:
            raise NotFoundError("判定凭证不存在", {"voucher_id": voucher_id})
        return self._voucher_out(row)

    # -- signed proof export (offline audit) ------------------------------

    def export_voucher_proof(self, voucher_id: str) -> tuple[dict[str, Any], bool]:
        """Return the fixed signed proof package for ``voucher_id``.

        The first export reads the immutable voucher row, signs its complete
        first-decision snapshot with the active local key and freezes the whole
        package in one write transaction. Repeat and concurrent exports return
        the same stored package (``replayed=true``); stored packages are never
        re-signed, including after key rotation. Historical vouchers are
        exportable exactly like new ones; an unknown voucher is ``404`` and an
        unusable signing key raises a signing-key error (HTTP 503).
        """
        if self.key_store is None:
            raise SigningKeyUnavailableError("签名密钥未配置")

        with self.db.lock:
            existing = self.db.get_proof(voucher_id)
            if existing is not None:
                return json.loads(existing["proof_json"]), True

            voucher_row = self.db.get_voucher(voucher_id)
            if voucher_row is None:
                raise NotFoundError(
                    "判定凭证不存在，无法导出证明包", {"voucher_id": voucher_id}
                )

            # Signing material is obtained before opening the write transaction
            # so a missing/unwritable key never leaves a half-open transaction.
            key = self.key_store.ensure_active_key()
            payload = _proof_payload(voucher_row)
            package = build_signed_proof(
                payload,
                key,
                public_key_url="/signing/keys/{key_id}/public",
                public_keys_url="/signing/keys",
            )
            package["exported_at"] = to_canonical(now_utc())
            proof_json = json.dumps(package, ensure_ascii=False, sort_keys=True)

            try:
                self.db.begin()
                self.db.insert_proof(
                    voucher_id=voucher_id,
                    key_id=key.key_id,
                    algorithm=SIGNING_ALGORITHM,
                    exported_at=package["exported_at"],
                    proof_json=proof_json,
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                # A concurrent export may have won the primary key: it carries
                # the identical voucher snapshot semantics — return that one
                # package instead of failing, so concurrent calls agree.
                raced = self.db.get_proof(voucher_id)
                if raced is not None:
                    return json.loads(raced["proof_json"]), True
                raise
            return package, False

    def get_voucher_proof(self, voucher_id: str) -> dict[str, Any]:
        """Return an already-frozen proof package without creating one."""
        row = self.db.get_proof(voucher_id)
        if row is not None:
            return json.loads(row["proof_json"])
        if self.db.get_voucher(voucher_id) is None:
            raise NotFoundError(
                "判定凭证不存在，无法导出证明包", {"voucher_id": voucher_id}
            )
        raise NotFoundError(
            "该凭证尚未导出过证明包",
            {"voucher_id": voucher_id, "reason": "proof_not_exported"},
        )

    @staticmethod
    def _voucher_out(row: dict[str, Any]) -> dict[str, Any]:
        decision = _thaw_decision(row["decision_json"])
        return {
            "voucher_id": row["voucher_id"],
            "version_id": row["version_id"],
            "use": row["use_name"],
            "at": parse_canonical(row["at_value"]),
            "registered_at": parse_canonical(row["registered_at"]),
            **decision,
        }

    # -- permission result subscriptions (flip event streams) -------------

    def create_subscription(
        self,
        subscription_id: str | None,
        version_id: str,
        use: str,
        at: datetime,
    ) -> tuple[dict[str, Any], bool]:
        """Subscribe to the permission result of ``(version, use, at)``.

        The first request evaluates the *existing* decision rules inside one
        write transaction against a single consistent snapshot and freezes the
        then-current result as an immutable baseline (allow/deny and the
        blocker reasons). Evaluation and storage are atomic: a failure rolls
        back and leaves no subscription.

        A retry with the same subscription id, version, trimmed use and
        UTC-normalised instant returns the original subscription untouched
        (``replayed=true``); reusing the id against different inputs is a
        conflict. Unknown version, empty use or a naive instant error out
        before any row is written.
        """
        if at.tzinfo is None:
            raise InvalidTimeError("判定时刻必须带时区信息")
        use = use.strip()
        if not use:
            raise ServiceError("用途不能为空")
        subscription_id = subscription_id or f"s-{uuid.uuid4().hex}"
        at_canonical = to_canonical(at)

        with self.db.lock:
            existing = self.db.get_subscription(subscription_id)
            if existing is not None:
                same = (
                    existing["version_id"] == version_id
                    and existing["use_name"] == use
                    and existing["at_value"] == at_canonical
                )
                if not same:
                    raise ConflictError(
                        "同一订阅标识已存在但输入不同（订阅标识不可复用指向不同判定）",
                        {
                            "subscription_id": subscription_id,
                            "submitted_version_id": version_id,
                            "existing_version_id": existing["version_id"],
                            "submitted_use": use,
                            "existing_use": existing["use_name"],
                            "submitted_at": at_canonical,
                            "existing_at": existing["at_value"],
                        },
                    )
                return self._subscription_out(existing), True

            try:
                # BEGIN IMMEDIATE pins both the write lock and one snapshot:
                # the version lookup, closure evaluation, baseline freeze and
                # the insert commit as one atomic unit, so a concurrent
                # change/revocation cannot interleave with the baseline.
                self.db.begin()
                if self.db.get_version(version_id) is None:
                    raise NotFoundError(
                        "版本不存在，无法建立许可结果订阅", {"version_id": version_id}
                    )
                snapshot = self._capture_snapshot(manage_transaction=False)
                state = _evaluate(snapshot, version_id, use, at_canonical, None)
                created = to_canonical(now_utc())
                self.db.insert_subscription(
                    subscription_id=subscription_id,
                    version_id=version_id,
                    use_name=use,
                    at_value=at_canonical,
                    created_at=created,
                    baseline_allowed=state["allowed"],
                    baseline_json=_freeze_baseline(state),
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                # A concurrent writer may have won the same primary key: map
                # the race onto replay/conflict exactly like a sequential call.
                raced = self.db.get_subscription(subscription_id)
                if raced is not None:
                    same = (
                        raced["version_id"] == version_id
                        and raced["use_name"] == use
                        and raced["at_value"] == at_canonical
                    )
                    if same:
                        return self._subscription_out(raced), True
                    raise ConflictError(
                        "同一订阅标识已存在但输入不同（订阅标识不可复用指向不同判定）",
                        {
                            "subscription_id": subscription_id,
                            "submitted_version_id": version_id,
                            "existing_version_id": raced["version_id"],
                        },
                    ) from None
                raise

            stored = self.db.get_subscription(subscription_id)
            assert stored is not None
            return self._subscription_out(stored), False

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Return the immutable baseline subscription for a subscription id."""
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise NotFoundError(
                "许可结果订阅不存在", {"subscription_id": subscription_id}
            )
        return self._subscription_out(row)

    def read_subscription_events(
        self,
        subscription_id: str,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        """Return one stable, cursor-ordered page of a subscription's flips.

        Events are read in strictly increasing ``seq`` order. The cursor is the
        last seen sequence number (0 = from the beginning); at most ``limit``
        events are returned together with ``next_cursor`` (the seq of the last
        returned event, echoing ``after_seq`` on an empty page) so the next
        call reads exactly the unseen suffix. An unknown subscription is a
        404 and leaves nothing behind.
        """
        if after_seq < 0:
            raise ServiceError("游标序号不能为负数")
        if limit < 1:
            raise ServiceError("每页条数必须为正整数")
        with self.db.lock:
            if self.db.get_subscription(subscription_id) is None:
                raise NotFoundError(
                    "许可结果订阅不存在", {"subscription_id": subscription_id}
                )
            # Fetch one extra row to decide whether another page exists.
            rows = self.db.list_subscription_events(
                subscription_id, after_seq, limit + 1
            )
        has_more = len(rows) > limit
        page = rows[:limit]
        events = [self._event_out(row) for row in page]
        next_cursor = page[-1]["seq"] if page else after_seq
        return {
            "subscription_id": subscription_id,
            "after_seq": after_seq,
            "limit": limit,
            "events": events,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    # -- version-slot releases (unified publish & historical replay) ------

    def publish_release(
        self,
        release_id: str | None,
        effective_at: datetime,
        slots: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Publish many registered versions as one unified slot release.

        A request carries a unique ``release_id``, one timezone-aware
        ``effective_at`` and a non-empty ordered list of slots; every entry
        names a trimmed slot, an existing target version and the expected
        revision it replaces. Validations:

        * slot names are non-empty after trimming and unique within the release;
        * every target version exists;
        * a slot's first publish asserts ``expected_revision == 0``;
        * afterwards the target must be a STRICT descendant of the slot's
          previously published version, the effective instant must be strictly
          later than that publish, and the expected revision must equal the
          previous revision + 1.

        Only after EVERY entry validates do the writes happen in ONE
        transaction: the immutable release row, one immutable entry row per
        slot and the per-slot head advance (0 -> 1 -> 2 ...). Any failure rolls
        the whole release back — no slot skips a revision and no partial
        release remains. A retry with the same release id and the identical
        normalised content returns the frozen first result (``replayed=true``)
        without advancing anything; the same id with different content is a
        conflict. The process lock, ``BEGIN IMMEDIATE``, the conditional head
        update and the database-level ``UNIQUE(slot_name, assigned_revision)``
        constraint together make concurrent publishes safe across processes.
        """
        if not isinstance(effective_at, datetime) or effective_at.tzinfo is None:
            raise InvalidTimeError("发布生效时刻必须带时区信息")
        if not slots:
            # The HTTP schema enforces this too; keep the service contract.
            raise ServiceError("槽位清单不能为空")

        release_id = release_id or f"rel-{uuid.uuid4().hex}"
        effective = to_canonical(effective_at)

        normalised: list[dict[str, Any]] = []
        seen_slots: set[str] = set()
        for pos, item in enumerate(slots):
            slot = str(item["slot"]).strip()
            if not slot:
                raise ServiceError(f"第 {pos} 个槽位的名称不能为空", {"index": str(pos)})
            if slot in seen_slots:
                raise ConflictError(
                    "槽名去首尾空格后在同一次发布中重复",
                    {"release_id": release_id, "slot": slot, "index": str(pos)},
                )
            seen_slots.add(slot)
            try:
                expected = int(item["expected_revision"])
            except (TypeError, ValueError) as exc:
                raise ServiceError(
                    f"第 {pos} 个槽位的预期修订号必须为非负整数",
                    {"slot": slot, "index": str(pos)},
                ) from exc
            if expected < 0:
                raise ServiceError(
                    f"第 {pos} 个槽位的预期修订号不能为负",
                    {"slot": slot, "index": str(pos)},
                )
            normalised.append(
                {
                    "position": pos,
                    "slot": slot,
                    "version_id": str(item["version_id"]),
                    "expected_revision": expected,
                }
            )

        digest = release_content_hash(effective, normalised)

        with self.db.lock:
            existing = self.db.get_release(release_id)
            if existing is not None:
                if existing["content_hash"] != digest:
                    raise ConflictError(
                        "同一发布标识已存在但内容不同（发布标识不可复用指向不同内容）",
                        {"release_id": release_id},
                    )
                return _thaw_release_result(existing["result_json"]), True

            # Whole-release pre-validation before opening the write transaction.
            missing = [
                entry["version_id"]
                for entry in normalised
                if self.db.get_version(entry["version_id"]) is None
            ]
            if missing:
                raise NotFoundError(
                    "发布目标中存在尚未登记的版本，整次发布拒绝",
                    {
                        "release_id": release_id,
                        "missing_version_ids": ",".join(sorted(set(missing))),
                    },
                )
            plans = self._validate_slots_against_heads(normalised, effective, None)

            created = to_canonical(now_utc())
            try:
                self.db.begin()
                # A concurrent first-writer may have won the release id under
                # contention; re-check inside the write transaction.
                raced = self.db.get_release(release_id)
                if raced is not None:
                    if raced["content_hash"] != digest:
                        raise ConflictError(
                            "同一发布标识已存在但内容不同（发布标识不可复用指向不同内容）",
                            {"release_id": release_id},
                        )
                    self.db.rollback()
                    return _thaw_release_result(raced["result_json"]), True

                # Re-validate against the pinned write snapshot: another process
                # may have published any of these slots between the pre-checks
                # and this transaction. The conditional head update plus the
                # UNIQUE(slot, revision) constraint are the final backstops.
                snapshot = self._capture_snapshot(manage_transaction=False)
                plans = self._validate_slots_against_heads(
                    normalised, effective, snapshot
                )

                slot_results: list[dict[str, Any]] = []
                for entry, plan in zip(normalised, plans):
                    slot_results.append(
                        {
                            "position": entry["position"],
                            "slot": entry["slot"],
                            "version_id": entry["version_id"],
                            "expected_revision": entry["expected_revision"],
                            "revision": plan["revision"],
                        }
                    )

                result = {
                    "release_id": release_id,
                    "effective_at": parse_canonical(effective),
                    "created_at": parse_canonical(created),
                    "slot_count": len(slot_results),
                    "slots": slot_results,
                }
                # The immutable release row is the parent of the slot entries
                # and the new head pointers, so it is written first inside the
                # same transaction; every remaining write either all commits
                # with it or is rolled back.
                self.db.insert_release(
                    release_id=release_id,
                    content_hash=digest,
                    effective_at=effective,
                    created_at=created,
                    result_json=_freeze_release_result(result),
                )
                for entry, plan in zip(normalised, plans):
                    self.db.insert_release_slot(
                        release_id=release_id,
                        position=entry["position"],
                        slot_name=entry["slot"],
                        version_id=entry["version_id"],
                        expected_revision=entry["expected_revision"],
                        assigned_revision=plan["revision"],
                        effective_at=effective,
                    )
                    if plan["first_publish"]:
                        self.db.insert_slot_head(
                            slot_name=entry["slot"],
                            revision=plan["revision"],
                            release_id=release_id,
                            version_id=entry["version_id"],
                            effective_at=effective,
                            updated_at=created,
                        )
                    else:
                        self.db.update_slot_head(
                            slot_name=entry["slot"],
                            revision=plan["revision"],
                            release_id=release_id,
                            version_id=entry["version_id"],
                            effective_at=effective,
                            updated_at=created,
                            previous_revision=plan["previous_revision"],
                        )

                self.db.commit()
            except Exception:
                self.db.rollback()
                # Concurrent writers may have won the release id or a slot
                # revision; map each race onto replay/conflict semantics.
                raced = self.db.get_release(release_id)
                if raced is not None:
                    if raced["content_hash"] == digest:
                        return _thaw_release_result(raced["result_json"]), True
                    raise ConflictError(
                        "同一发布标识已存在但内容不同（发布标识不可复用指向不同内容）",
                        {"release_id": release_id},
                    ) from None
                # The release itself lost no primary key: re-run the head checks
                # so a lost slot revision surfaces as the documented 409 rather
                # than an integrity error, and the response mentions the slot.
                self._validate_slots_against_heads(normalised, effective, None)
                raise

            stored = self.db.get_release(release_id)
            assert stored is not None
            return _thaw_release_result(stored["result_json"]), False

    def _validate_slots_against_heads(
        self,
        normalised: list[dict[str, Any]],
        effective: str,
        snapshot: Snapshot | None,
    ) -> list[dict[str, Any]]:
        """Validate revision/descendant/effective-time rules for every slot.

        Runs under the caller's db lock, either before the write transaction
        (``snapshot=None``; graph traversal hits the database) or INSIDE it with
        the pinned write snapshot. Target-version existence is checked by the
        caller. Returns one plan per slot (assigned revision + first-publish
        flag) or raises on the first invalid entry; the whole release is then
        rejected.
        """
        plans: list[dict[str, Any]] = []
        for entry in normalised:
            slot = entry["slot"]
            target = entry["version_id"]
            expected = entry["expected_revision"]
            head = self.db.get_slot_head(slot)

            if head is None:
                if expected != 0:
                    raise ConflictError(
                        "槽位首次发布的预期修订号必须为 0",
                        {
                            "slot": slot,
                            "expected_revision": str(expected),
                            "current_revision": "0",
                        },
                    )
                plans.append(
                    {"revision": 0, "first_publish": True, "previous_revision": None}
                )
                continue

            current = int(head["revision"])
            wanted = current + 1
            if expected != wanted:
                raise ConflictError(
                    "预期修订号与该槽当前修订不匹配（并发发布或漏发修订）",
                    {
                        "slot": slot,
                        "expected_revision": str(expected),
                        "current_revision": str(current),
                        "required_revision": str(wanted),
                    },
                )
            if effective <= head["effective_at"]:
                raise ConflictError(
                    "生效时刻必须严格晚于该槽上次发布的生效时刻",
                    {
                        "slot": slot,
                        "submitted_effective_at": effective,
                        "previous_effective_at": head["effective_at"],
                    },
                )
            previous_version = head["version_id"]
            if not self._is_strict_descendant(previous_version, target, snapshot):
                raise ConflictError(
                    "目标版本必须是该槽上次发布版本的严格后代",
                    {
                        "slot": slot,
                        "previous_version_id": previous_version,
                        "submitted_version_id": target,
                    },
                )
            plans.append(
                {
                    "revision": wanted,
                    "first_publish": False,
                    "previous_revision": current,
                }
            )
        return plans

    def _is_strict_descendant(
        self,
        ancestor_id: str,
        candidate_id: str,
        snapshot: Snapshot | None,
    ) -> bool:
        """True when ``candidate_id`` is a transitive descendant (never self).

        Parent edges are frozen at registration, so the version DAG only grows;
        inside a write transaction the pinned snapshot's children map is used.
        """
        if candidate_id == ancestor_id:
            return False
        if snapshot is not None:
            return candidate_id in set(snapshot.descendants_of(ancestor_id))
        seen = {ancestor_id}
        queue: deque[str] = deque([ancestor_id])
        while queue:
            current = queue.popleft()
            for child in self.db.get_children(current):
                if child == candidate_id:
                    return True
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return False

    def get_release(self, release_id: str) -> dict[str, Any]:
        """Return the immutable frozen result of a release by its id."""
        row = self.db.get_release(release_id)
        if row is None:
            raise NotFoundError("发布不存在", {"release_id": release_id})
        return _thaw_release_result(row["result_json"])

    def list_releases(self) -> list[dict[str, Any]]:
        return [_thaw_release_result(row["result_json"]) for row in self.db.list_releases()]

    def list_slot_heads(self) -> list[dict[str, Any]]:
        return [self._slot_head_out(row) for row in self.db.list_slot_heads()]

    def get_slot_head(self, slot: str) -> dict[str, Any]:
        slot = slot.strip()
        row = self.db.get_slot_head(slot) if slot else None
        if row is None:
            raise NotFoundError("槽位尚未发布", {"slot": slot or ""})
        return self._slot_head_out(row)

    @staticmethod
    def _slot_head_out(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "slot": row["slot_name"],
            "revision": int(row["revision"]),
            "release_id": row["release_id"],
            "version_id": row["version_id"],
            "effective_at": parse_canonical(row["effective_at"]),
            "updated_at": parse_canonical(row["updated_at"]),
        }

    def resolve_slot(self, slot: str, at: datetime) -> dict[str, Any]:
        """Resolve the target version a slot published most recently at ``at``.

        "Most recently" is the slot's highest revision whose release-wide
        effective instant is at or before the timezone-aware query instant.
        A slot that has never been published at all, or only later than the
        query instant, is an explicit error — callers must not guess a
        version.
        """
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise InvalidTimeError("查询时刻必须带时区信息")
        slot = slot.strip()
        if not slot:
            raise ServiceError("槽名不能为空")

        at_canonical = to_canonical(at)
        row = self.db.resolve_slot_at(slot, at_canonical)
        if row is not None:
            return {
                "slot": slot,
                "at": at.astimezone(timezone.utc),
                "version_id": row["version_id"],
                "release_id": row["release_id"],
                "revision": int(row["revision"]),
                "effective_at": parse_canonical(row["effective_at"]),
            }

        if self.db.has_slot_any_release(slot):
            raise SlotNotPublishedError(
                "该槽在查询时刻之前尚无已发布版本（首次发布晚于该时刻）",
                {"slot": slot, "at": at_canonical, "reason": "no_release_effective_at_or_before"},
            )
        raise SlotNotPublishedError(
            "槽位从未发布，无法按历史时刻解析",
            {"slot": slot, "at": at_canonical, "reason": "slot_never_published"},
        )

    def check_slot_permission(self, slot: str, use: str, at: datetime) -> dict[str, Any]:
        """Resolve ``slot`` at ``at`` and evaluate ``use`` under current rules.

        The resolution (historical target version, release id and revision) is
        returned together with the ordinary permission decision evaluated for
        that resolved version and the same instant.
        """
        resolution = self.resolve_slot(slot, at)
        decision = self.check_permission(resolution["version_id"], use, at)
        return {"resolution": resolution, **decision}

    # -- delivery admissions (whole-window immutable manifests) -----------

    def submit_delivery_admission(
        self,
        delivery_id: str | None,
        start: datetime,
        end: datetime,
        entries: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically admit a whole delivery manifest over its usage window.

        Each entry references either a registered ``version`` or a version
        ``slot`` and states one trimmed intended use. Slot references follow
        the slot's published re-points within the half-open window
        ``[start, end)`` — the target version is resolved per segment against
        the SAME database snapshot — and the resolved version is then judged
        with the existing ancestor/intersection/license/revocation rules for
        that segment. Releases or license changes effective exactly at
        ``start`` apply; anything exactly at ``end`` does not.

        Only when EVERY entry is allowed for the ENTIRE window is the
        admission record frozen in one write transaction with every entry's
        per-segment resolved version and decision basis; otherwise an
        :class:`AdmissionDeniedError` lists each failed entry's FIRST failing
        interval and its blocking reasons (a slot not published yet fails too)
        and NOTHING is persisted.

        A retry with the same delivery id and an identical normalised input
        (trimmed refs/uses, UTC instants, same order) returns the frozen first
        result; the same id with different content is a conflict. Unknown
        versions, naive times, an empty window or blank uses are rejected
        before any row is written.
        """
        if not isinstance(start, datetime) or start.tzinfo is None:
            raise InvalidTimeError("使用窗口起点必须带时区信息")
        if not isinstance(end, datetime) or end.tzinfo is None:
            raise InvalidTimeError("使用窗口终点必须带时区信息")
        if not entries:
            # The HTTP schema enforces this too; keep the service contract.
            raise ServiceError("交付条目清单不能为空")

        delivery_id = (delivery_id or "").strip() or f"del-{uuid.uuid4().hex}"
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        if not start_utc < end_utc:
            raise InvalidIntervalError(
                "使用窗口起点必须严格早于终点（半开区间 [start, end) 不能为空或倒置）",
                {"start": to_canonical(start_utc), "end": to_canonical(end_utc)},
            )
        start_c = to_canonical(start_utc)
        end_c = to_canonical(end_utc)

        normalised: list[dict[str, Any]] = []
        for pos, item in enumerate(entries):
            kind = str(item["kind"]).strip()
            if kind not in ("version", "slot"):
                raise ServiceError(
                    f"第 {pos} 条条目的引用类型必须是 version 或 slot",
                    {"index": str(pos)},
                )
            ref = str(item["ref"]).strip()
            if not ref:
                raise ServiceError(
                    f"第 {pos} 条条目的引用标识不能为空", {"index": str(pos)}
                )
            use = str(item["use"]).strip()
            if not use:
                raise ServiceError(
                    f"第 {pos} 条条目的用途不能为空", {"index": str(pos)}
                )
            normalised.append(
                {"position": pos, "kind": kind, "ref": ref, "use": use}
            )

        digest = admission_content_hash(start_c, end_c, normalised)

        with self.db.lock:
            existing = self.db.get_delivery_admission(delivery_id)
            if existing is not None:
                if existing["content_hash"] != digest:
                    raise ConflictError(
                        "同一交付标识已存在但窗口或条目内容不同（交付标识不可复用指向不同清单）",
                        {"delivery_id": delivery_id},
                    )
                return _thaw_admission_result(existing["result_json"]), True

            # Unknown versions are a request error (whole order rejected);
            # unpublished slots are instead an evaluated failure per entry.
            missing = sorted(
                {
                    entry["ref"]
                    for entry in normalised
                    if entry["kind"] == "version"
                    and self.db.get_version(entry["ref"]) is None
                }
            )
            if missing:
                raise NotFoundError(
                    "交付条目中引用了尚未登记的版本，整单拒绝",
                    {"delivery_id": delivery_id,
                     "missing_version_ids": ",".join(missing)},
                )

            try:
                # One BEGIN IMMEDIATE pins a single snapshot for every slot
                # resolution and permission evaluation, so a re-point and its
                # judgement can never come from different catalog states.
                self.db.begin()
                raced = self.db.get_delivery_admission(delivery_id)
                if raced is not None:
                    if raced["content_hash"] != digest:
                        raise ConflictError(
                            "同一交付标识已存在但窗口或条目内容不同（交付标识不可复用指向不同清单）",
                            {"delivery_id": delivery_id},
                        )
                    self.db.rollback()
                    return _thaw_admission_result(raced["result_json"]), True

                snapshot = self._capture_snapshot(manage_transaction=False)
                results: list[dict[str, Any]] = []
                failures: list[dict[str, Any]] = []
                for entry in normalised:
                    evaluated = self._evaluate_admission_entry(
                        snapshot, entry, start_c, end_c
                    )
                    results.append(evaluated)
                    for segment in evaluated["segments"]:
                        if not segment["allowed"]:
                            failures.append(
                                _admission_failure_from_segment(entry, segment)
                            )
                            break

                if failures:
                    # Denial is an evaluated outcome, not a write: roll the
                    # read transaction back and leave no admission row.
                    self.db.rollback()
                    raise AdmissionDeniedError(
                        "交付清单未在整个使用窗口内持续获准，未固化任何准入记录",
                        failures=failures,
                        details={"delivery_id": delivery_id},
                    )

                created = to_canonical(now_utc())
                result = {
                    "delivery_id": delivery_id,
                    "created_at": parse_canonical(created),
                    "start": start_utc,
                    "end": end_utc,
                    "entry_count": len(results),
                    "entries": results,
                }
                self.db.insert_delivery_admission(
                    delivery_id=delivery_id,
                    content_hash=digest,
                    start_value=start_c,
                    end_value=end_c,
                    created_at=created,
                    result_json=_freeze_admission_result(result),
                )
                self.db.commit()
            except AdmissionDeniedError:
                raise
            except Exception:
                self.db.rollback()
                # A concurrent first-writer may have won the delivery id.
                raced = self.db.get_delivery_admission(delivery_id)
                if raced is not None:
                    if raced["content_hash"] == digest:
                        return _thaw_admission_result(raced["result_json"]), True
                    raise ConflictError(
                        "同一交付标识已存在但窗口或条目内容不同（交付标识不可复用指向不同清单）",
                        {"delivery_id": delivery_id},
                    ) from None
                raise

            stored = self.db.get_delivery_admission(delivery_id)
            assert stored is not None
            return _thaw_admission_result(stored["result_json"]), False

    def get_delivery_admission(self, delivery_id: str) -> dict[str, Any]:
        """Return the immutable frozen admission record for a delivery id."""
        row = self.db.get_delivery_admission(delivery_id)
        if row is None:
            raise NotFoundError(
                "交付准入记录不存在（被拒绝的清单不会固化记录）",
                {"delivery_id": delivery_id},
            )
        return _thaw_admission_result(row["result_json"])

    def _evaluate_admission_entry(
        self,
        snapshot: Snapshot,
        entry: dict[str, Any],
        start_c: str,
        end_c: str,
    ) -> dict[str, Any]:
        """Resolve one entry over the window and slice its permission timeline.

        A version entry resolves to that version for the whole window (its
        ``resolution`` is a version pointer). A slot entry follows the slot's
        published history: the window is partitioned by re-points, and each
        resolution interval carries the effective release pointer. A prefix
        without an effective release is one synthetic segment blocked with
        ``slot_not_published``.
        """
        kind = entry["kind"]
        ref = entry["ref"]
        use = entry["use"]

        if kind == "version":
            resolution = {
                "kind": "version",
                "ref": ref,
                "version_id": ref,
                "release_id": None,
                "revision": None,
                "effective_at": parse_canonical(start_c),
            }
            permission_segments = _permission_segments(
                snapshot, ref, use, start_c, end_c
            )
            segments = [
                {**segment, "resolution": resolution, "version_id": ref}
                for segment in permission_segments
            ]
            return {**entry, "resolution": resolution, "segments": segments}

        intervals = snapshot.slot_resolution_intervals(ref, start_c, end_c)
        segments: list[dict[str, Any]] = []
        first_resolution: dict[str, Any] | None = None
        for seg_start, seg_end, target in intervals:
            if target is None:
                # No release effective at or before this segment: the slot is
                # not published for this part of the window — an explicit
                # failure, never a guessed version.
                resolution = {
                    "kind": "slot",
                    "ref": ref,
                    "version_id": None,
                    "release_id": None,
                    "revision": None,
                    "effective_at": parse_canonical(seg_start),
                }
                segment = {
                    "start": parse_canonical(seg_start),
                    "end": parse_canonical(seg_end),
                    "allowed": False,
                    "resolution": resolution,
                    "version_id": None,
                    "closure": [],
                    "basis": [],
                    "blockers": [
                        {
                            "version_id": None,
                            "reason": "slot_not_published",
                        }
                    ],
                    "allowed_uses_intersection": [],
                }
            else:
                resolution = {
                    "kind": "slot",
                    "ref": ref,
                    "version_id": target.version_id,
                    "release_id": target.release_id,
                    "revision": target.revision,
                    "effective_at": parse_canonical(target.effective_at),
                }
                permission_segments = _permission_segments(
                    snapshot, target.version_id, use, seg_start, seg_end
                )
                # _permission_segments may return several pieces when a license
                # change lands inside this resolution interval; attach the same
                # resolution to every piece.
                pieces = [
                    {**piece, "resolution": resolution, "version_id": target.version_id}
                    for piece in permission_segments
                ]
                if first_resolution is None:
                    first_resolution = resolution
                segments.extend(pieces)
                continue
            if first_resolution is None:
                first_resolution = resolution
            segments.append(segment)

        # Resolution reported at the entry level is the pointer governing the
        # first instant of the window (a slot may have been published exactly
        # at start; otherwise this is an unpublished-pointer entry).
        entry_resolution = first_resolution
        assert entry_resolution is not None
        return {**entry, "resolution": entry_resolution, "segments": segments}

    def _propagate_subscription_flips(
        self,
        *,
        snapshot: Snapshot,
        root_version_id: str,
        trigger_kind: str,
        trigger_id: str,
        change_id: str,
        effective_at: str,
    ) -> int:
        """Re-evaluate affected subscriptions and append flips in-transaction.

        Runs INSIDE the caller's open write transaction, with ``snapshot``
        already reflecting the just-committed-in-this-transaction
        change/revocation. Every subscription on the affected version itself
        or on any of its descendants is re-evaluated at its fixed decision
        instant with the existing ancestor/latest-effective/revocation rules;
        an event is appended ONLY when the allow status actually flips, and it
        is written in the very same transaction so the trigger and its events
        are atomic. Per-subscription sequence numbers start at 1 and grow by
        exactly one per flip; the process lock plus one write transaction
        guarantee no flip is missed or counted twice under concurrency.

        Returns the number of flip events appended (summed over every affected
        subscription); batch imports report this per change.
        """
        affected = snapshot.descendants_of(root_version_id)
        candidates = self.db.list_subscriptions_for_versions(affected)
        if not candidates:
            return 0

        last_events = self.db.latest_subscription_events(
            [row["subscription_id"] for row in candidates]
        )
        created = to_canonical(now_utc())
        event_count = 0
        for sub in candidates:
            last = last_events.get(sub["subscription_id"])
            if last is None:
                baseline = _thaw_baseline(sub["baseline_json"])
                before_allowed = baseline["allowed"]
                before_blockers = baseline["blockers"]
            else:
                before_allowed = bool(last["after_allowed"])
                before_blockers = _blockers_from_jsonable(
                    json.loads(last["after_blockers_json"])
                )

            state = _evaluate(
                snapshot,
                sub["version_id"],
                sub["use_name"],
                sub["at_value"],
                None,
            )
            if state["allowed"] == before_allowed:
                # Not effective yet, a later stored change covers the instant,
                # or only the basis/intersection changed without flipping the
                # allow status: no event.
                continue

            seq = self.db.next_event_seq(sub["subscription_id"])
            self.db.insert_subscription_event(
                subscription_id=sub["subscription_id"],
                seq=seq,
                trigger_kind=trigger_kind,
                trigger_id=trigger_id,
                change_id=change_id,
                # A revocation is effective at every query instant; keep the
                # original change's effective time for traceability.
                effective_at=effective_at,
                before_allowed=before_allowed,
                after_allowed=state["allowed"],
                before_blockers_json=json.dumps(
                    _blockers_jsonable(before_blockers),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                after_blockers_json=json.dumps(
                    _blockers_jsonable(state["blockers"]),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                created_at=created,
            )
            event_count += 1
        return event_count

    @staticmethod
    def _subscription_out(row: dict[str, Any]) -> dict[str, Any]:
        baseline = _thaw_baseline(row["baseline_json"])
        return {
            "subscription_id": row["subscription_id"],
            "version_id": row["version_id"],
            "use": row["use_name"],
            "at": parse_canonical(row["at_value"]),
            "created_at": parse_canonical(row["created_at"]),
            "baseline_allowed": bool(row["baseline_allowed"]),
            "baseline_blockers": baseline["blockers"],
        }

    @staticmethod
    def _event_out(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "trigger_kind": row["trigger_kind"],
            "trigger_id": row["trigger_id"],
            "change_id": row["change_id"],
            "effective_at": parse_canonical(row["effective_at"])
            if row.get("effective_at")
            else None,
            "before_allowed": bool(row["before_allowed"]),
            "after_allowed": bool(row["after_allowed"]),
            "before_blockers": _blockers_from_jsonable(
                json.loads(row["before_blockers_json"])
            ),
            "after_blockers": _blockers_from_jsonable(
                json.loads(row["after_blockers_json"])
            ),
            "created_at": parse_canonical(row["created_at"]),
        }

    # -- change preview (no writes) ---------------------------------------

    def preview_change(
        self,
        *,
        change_id: str | None,
        version_id: str,
        effective_at: datetime,
        allowed_uses: list[str],
        use: str,
        at: datetime,
    ) -> dict[str, Any]:
        """Dry-run a change without persisting it.

        Evaluates the given ``use`` at query time ``at`` for the target
        version and every descendant twice — once on the current snapshot and
        once as if the proposed change were committed — and returns only the
        versions whose allow/deny status flips, with before/after blockers.

        Conflict semantics mirror :meth:`submit_change`: unknown version ->
        404; same change id with different content, or a different change at
        the same (version, effective instant) -> 409; same id and content is a
        replay with no effect. All reads come from one pinned read snapshot so
        concurrent commits cannot interleave into the evaluation.
        """
        if effective_at.tzinfo is None:
            raise InvalidTimeError("生效时刻必须带时区信息")
        if at.tzinfo is None:
            raise InvalidTimeError("查询时刻必须带时区信息")
        use = use.strip()
        if not use:
            raise ServiceError("用途不能为空")

        effective = to_canonical(effective_at)
        uses = sorted(set(u.strip() for u in allowed_uses if u.strip()))
        change_id = change_id or f"c-{uuid.uuid4().hex}"
        digest = content_hash(version_id, effective, uses)

        with self.db.lock:
            if self.db.get_version(version_id) is None:
                raise NotFoundError("版本不存在，无法为其预演许可变更", {"version_id": version_id})

            existing = self.db.get_change(change_id)
            replayed = False
            if existing is not None:
                same = (
                    existing["version_id"] == version_id
                    and existing["effective_at"] == effective
                    and existing["content_hash"] == digest
                )
                if not same:
                    raise ConflictError(
                        "相同变更标识已存在但内容不同",
                        {
                            "change_id": change_id,
                            "submitted_version_id": version_id,
                            "existing_version_id": existing["version_id"],
                            "submitted_effective_at": effective,
                            "existing_effective_at": existing["effective_at"],
                        },
                    )
                replayed = True
            else:
                twin = self.db.change_at_same_time(version_id, effective)
                if twin is not None and twin["change_id"] != change_id:
                    raise ConflictError(
                        "该版本在同一生效时刻已存在不同变更",
                        {
                            "version_id": version_id,
                            "effective_at": effective,
                            "existing_change_id": twin["change_id"],
                            "submitted_change_id": change_id,
                        },
                    )
            snapshot = self._capture_snapshot()

        at_canonical = to_canonical(at)
        scope = snapshot.descendants_of(version_id)
        override = None if replayed else EffectiveChange(
            change_id=change_id,
            version_id=version_id,
            effective_at=effective,
            allowed_uses=uses,
        )

        impacted: list[dict[str, Any]] = []
        for node in scope:
            before = _evaluate(snapshot, node, use, at_canonical, None)
            after = (
                before
                if override is None
                else _evaluate(snapshot, node, use, at_canonical, override)
            )
            if before["allowed"] == after["allowed"]:
                # Query before the effective instant, a later stored change
                # already covering the proposal, or simply no difference.
                continue
            impacted.append(
                {
                    "version_id": node,
                    "closure": before["closure"],
                    "before_allowed": before["allowed"],
                    "after_allowed": after["allowed"],
                    "before_blockers": before["blockers"],
                    "after_blockers": after["blockers"],
                    "before_basis": before["basis"],
                    "after_basis": after["basis"],
                    "before_allowed_uses_intersection": before["allowed_uses_intersection"],
                    "after_allowed_uses_intersection": after["allowed_uses_intersection"],
                }
            )

        return {
            "replayed": replayed,
            "change": {
                "change_id": change_id,
                "version_id": version_id,
                "effective_at": parse_canonical(effective),
                "allowed_uses": uses,
            },
            "query": {"use": use, "at": at.astimezone(timezone.utc)},
            "scope": scope,
            "impacted_versions": impacted,
        }

    def _capture_snapshot(self, *, manage_transaction: bool = True) -> Snapshot:
        """Pin one consistent read snapshot of versions, edges and changes.

        Runs inside the caller's db lock. With ``manage_transaction`` it opens
        a deferred read transaction (WAL): the first SELECT pins a consistent
        snapshot that stays stable until the rollback, and taking no write lock
        keeps previews read-only. When ``manage_transaction`` is false the
        caller already owns an open write transaction (voucher issuing): all
        SELECTs then share that transaction's snapshot and its write lock, so
        the evaluation and the subsequent insert are atomic against every
        other writer.
        """
        if manage_transaction:
            self.db.begin_read()
        try:
            versions = {row["version_id"] for row in self.db.list_versions()}
            edges = self.db.all_parent_edges()
            revoked = set(self.db.list_revoked_change_ids())
            changes: dict[str, list[EffectiveChange]] = {}
            rows = self.db.query_all(
                "SELECT * FROM license_changes ORDER BY version_id, effective_at, change_id"
            )
            for row in rows:
                changes.setdefault(row["version_id"], []).append(
                    EffectiveChange(
                        change_id=row["change_id"],
                        version_id=row["version_id"],
                        effective_at=row["effective_at"],
                        allowed_uses=list(row["allowed_uses"]),
                    )
                )
            slot_releases: dict[str, list[SlotRelease]] = {}
            for row in self.db.all_release_slots():
                slot_releases.setdefault(row["slot_name"], []).append(
                    SlotRelease(
                        slot_name=row["slot_name"],
                        version_id=row["version_id"],
                        release_id=row["release_id"],
                        revision=int(row["assigned_revision"]),
                        effective_at=row["effective_at"],
                    )
                )
            return Snapshot(
                versions=versions,
                edges=edges,
                changes=changes,
                revoked_change_ids=revoked,
                slot_releases=slot_releases,
            )
        finally:
            if manage_transaction:
                self.db.rollback()


# -- immutable in-memory snapshot ------------------------------------------


class EffectiveChange(NamedTuple):
    change_id: str
    version_id: str
    effective_at: str  # canonical UTC
    allowed_uses: list[str]


class SlotRelease(NamedTuple):
    slot_name: str
    version_id: str
    release_id: str
    revision: int
    effective_at: str  # canonical UTC (release-wide)


@dataclass(frozen=True)
class Snapshot:
    versions: set[str]
    edges: list[tuple[str, str]]  # (child, parent) in registration order
    changes: dict[str, list[EffectiveChange]]  # keyed by version, time-sorted
    revoked_change_ids: set[str] = frozenset()  # type: ignore[assignment]
    parents_map: dict[str, list[str]] = field(default_factory=dict)
    children_map: dict[str, list[str]] = field(default_factory=dict)
    # Per-slot immutable publish history, ordered by effective instant then
    # dense revision; admission evaluation re-points a slot reference across the
    # whole usage window using exactly this history.
    slot_releases: dict[str, list[SlotRelease]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parents: dict[str, list[str]] = {vid: [] for vid in self.versions}
        children: dict[str, set[str]] = {vid: set() for vid in self.versions}
        for child, parent in self.edges:  # edges arrive ordered by position
            parents.setdefault(child, []).append(parent)
            children.setdefault(parent, set()).add(child)
        object.__setattr__(self, "parents_map", parents)
        object.__setattr__(
            self, "children_map", {vid: sorted(kids) for vid, kids in children.items()}
        )

    def closure_from(self, version_id: str) -> list[str]:
        """Self plus all ancestors, BFS, deduplicated (diamond-safe)."""
        parents = self.parents_map
        order: list[str] = []
        seen = {version_id}
        queue = deque([version_id])
        while queue:
            current = queue.popleft()
            order.append(current)
            for parent in parents.get(current, []):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return order

    def descendants_of(self, version_id: str) -> list[str]:
        """The version itself plus every transitive descendant, deduplicated."""
        children = self.children_map
        order: list[str] = []
        seen = {version_id}
        queue = deque([version_id])
        while queue:
            current = queue.popleft()
            order.append(current)
            for child in children.get(current, []):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return order

    def latest_change(
        self,
        version_id: str,
        at_canonical: str,
        override: EffectiveChange | None,
    ) -> EffectiveChange | None:
        """Latest change effective at or before the query instant.

        ``override`` is the hypothetical change for exactly its target
        version. If it is not effective yet, or a stored change is effective
        later than it (already covering that instant), the stored latest
        change wins — exactly matching the "latest effective change" rule
        applied after a real commit.
        """
        stored: EffectiveChange | None = None
        for change in self.changes.get(version_id, ()):  # time-sorted
            if change.change_id in self.revoked_change_ids:
                # A voiding applies at every query instant: skipped entirely.
                continue
            if change.effective_at <= at_canonical:
                stored = change
            else:
                break
        if override is None or override.version_id != version_id:
            return stored
        if override.effective_at > at_canonical:
            return stored
        if stored is not None and stored.effective_at > override.effective_at:
            return stored
        return override

    def slot_resolution_intervals(
        self,
        slot_name: str,
        start_c: str,
        end_c: str,
    ) -> list[tuple[str, str, SlotRelease | None]]:
        """Partition ``[start_c, end_c)`` by the slot's published re-points.

        Returns consecutive half-open intervals ``(seg_start, seg_end, target)``
        covering the whole window. ``target`` is the most recent release of the
        slot effective at or before the segment start (a release exactly at
        ``start_c`` already applies; a release exactly at ``end_c`` is excluded);
        it is ``None`` for the prefix (or whole window) before the slot's first
        effective release — admissions treat an unpublished slot as a failure
        rather than guessing a version.
        """
        history = self.slot_releases.get(slot_name, ())
        # Strictly interior re-points split segments; a release at start opens
        # the first segment and one at end never applies.
        edges = [start_c]
        edges.extend(
            release.effective_at
            for release in history
            if start_c < release.effective_at < end_c
        )
        edges.append(end_c)

        # Prefix of releases up to each boundary; history is time/revision
        # sorted, so the last effective entry is the current target.
        index = 0
        current: SlotRelease | None = None
        intervals: list[tuple[str, str, SlotRelease | None]] = []
        for edge_index, seg_start in enumerate(edges[:-1]):
            while index < len(history) and history[index].effective_at <= seg_start:
                current = history[index]
                index += 1
            intervals.append((seg_start, edges[edge_index + 1], current))
        return intervals


def _evaluate(
    snapshot: Snapshot,
    version_id: str,
    use: str,
    at_canonical: str,
    override: EffectiveChange | None,
) -> dict[str, Any]:
    """Pure counterpart of LicenseService.check_permission on a snapshot."""
    closure = snapshot.closure_from(version_id)
    basis: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    intersection: set[str] | None = None
    missing_license = False

    for vid in closure:
        change = snapshot.latest_change(vid, at_canonical, override)
        if change is None:
            missing_license = True
            blockers.append({"version_id": vid, "reason": "no_valid_license"})
            continue

        allowed = set(change.allowed_uses)
        basis.append(
            {
                "version_id": vid,
                "change_id": change.change_id,
                "effective_at": parse_canonical(change.effective_at),
                "allowed_uses": sorted(allowed),
            }
        )
        intersection = allowed if intersection is None else intersection & allowed
        if use not in allowed:
            blockers.append(
                {
                    "version_id": vid,
                    "reason": "use_not_allowed",
                    "change_id": change.change_id,
                    "effective_at": parse_canonical(change.effective_at),
                    "allowed_uses": sorted(allowed),
                }
            )

    if missing_license:
        result_intersection: list[str] = []
    else:
        result_intersection = sorted(intersection) if intersection is not None else []
    return {
        "allowed": not blockers,
        "closure": closure,
        "basis": basis,
        "blockers": blockers,
        "allowed_uses_intersection": result_intersection,
    }


def _segment_state_key(state: dict[str, Any]) -> tuple[Any, ...]:
    """Hashable identity of a segment: allowed flag, basis and blockers.

    Two adjacent slices with equal keys carry the same 判定依据 and 阻断信息
    and must be merged into one continuous segment.
    """
    basis_key = tuple(
        (
            entry["version_id"],
            entry["change_id"],
            to_canonical(entry["effective_at"]),
            tuple(entry["allowed_uses"]),
        )
        for entry in state["basis"]
    )
    blocker_key = tuple(
        (
            entry["version_id"],
            entry["reason"],
            entry.get("change_id"),
            to_canonical(entry["effective_at"])
            if entry.get("effective_at") is not None
            else None,
            tuple(entry.get("allowed_uses", ())),
        )
        for entry in state["blockers"]
    )
    return (
        state["allowed"],
        basis_key,
        blocker_key,
        tuple(state["allowed_uses_intersection"]),
    )


def _segment_payload(start_c: str, end_c: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": parse_canonical(start_c),
        "end": parse_canonical(end_c),
        "allowed": state["allowed"],
        "closure": state["closure"],
        "basis": state["basis"],
        "blockers": state["blockers"],
        "allowed_uses_intersection": state["allowed_uses_intersection"],
    }


def _permission_segments(
    snapshot: Snapshot,
    version_id: str,
    use: str,
    start_c: str,
    end_c: str,
) -> list[dict[str, Any]]:
    """Slice ``[start_c, end_c)`` for one fixed resolved version, merged.

    Segment boundaries are the effective instants (canonical UTC) of the
    closure's non-revoked changes strictly inside the interval; a change
    exactly at ``start_c`` applies from the first segment on, one exactly at
    ``end_c`` is not counted, and revoked changes are ignored. Adjacent slices
    with identical basis/blockers/intersection merge into one segment.
    """
    closure = snapshot.closure_from(version_id)
    interior: set[str] = set()
    for vid in closure:
        for change in snapshot.changes.get(vid, ()):
            if change.change_id in snapshot.revoked_change_ids:
                # Revoked changes neither split segments nor set policy.
                continue
            if start_c < change.effective_at < end_c:
                interior.add(change.effective_at)
    edges = [start_c, *sorted(interior), end_c]

    segments: list[dict[str, Any]] = []
    open_segment: tuple[tuple[Any, ...], dict[str, Any], str] | None = None
    for index in range(len(edges) - 1):
        seg_start = edges[index]
        # The state is constant within [seg_start, next edge): only a
        # change can alter it, and every in-interval change is an edge.
        state = _evaluate(snapshot, version_id, use, seg_start, None)
        key = _segment_state_key(state)
        if open_segment is not None and open_segment[0] == key:
            # Identical basis and blockers: extend the previous segment.
            continue
        if open_segment is not None:
            segments.append(
                _segment_payload(open_segment[2], seg_start, open_segment[1])
            )
        open_segment = (key, state, seg_start)
    assert open_segment is not None  # start < end guarantees >= 1 segment
    segments.append(_segment_payload(open_segment[2], end_c, open_segment[1]))
    return segments
