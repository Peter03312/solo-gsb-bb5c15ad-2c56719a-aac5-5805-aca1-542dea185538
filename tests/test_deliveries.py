"""End-to-end tests for delivery admissions (whole-window manifest clearance)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=False, echo=False)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _iso(y: int, m: int, d: int, hh: int = 0) -> str:
    return datetime(y, m, d, hh, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _version(client, version_id: str, parent_ids: list[str] | None = None) -> None:
    r = client.post(
        "/versions", json={"version_id": version_id, "parent_ids": parent_ids or []}
    )
    assert r.status_code == 201, r.text


def _change(client, change_id, version_id, effective_at, allowed_uses):
    r = client.post(
        "/license-changes",
        json={
            "change_id": change_id,
            "version_id": version_id,
            "effective_at": effective_at,
            "allowed_uses": allowed_uses,
        },
    )
    assert r.status_code == 201, r.text


def _publish(client, release_id, effective_at, slots):
    r = client.post(
        "/releases",
        json={"release_id": release_id, "effective_at": effective_at, "slots": slots},
    )
    assert r.status_code == 201, r.text


def _deliver(client, delivery_id, start, end, entries):
    return client.post(
        "/deliveries/admissions",
        json={
            "delivery_id": delivery_id,
            "start": start,
            "end": end,
            "entries": entries,
        },
    )


@pytest.fixture()
def catalog(client):
    """A small catalog: root -> v1 -> v2 chain plus a slot that switches v1->v2.

    root:  research+review from 2026-01-01
    v1:    research+review from 2026-01-01
    v2:    research only   from 2026-01-01 (review dropped)
    slot prod: v1 @2026-02-01 (rev 0), v2 @2026-04-01 (rev 1)
    """
    _version(client, "root")
    _version(client, "v1", ["root"])
    _version(client, "v2", ["v1"])
    _change(client, "lc-root", "root", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "lc-v1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "lc-v2", "v2", _iso(2026, 1, 1), ["research"])
    _publish(
        client,
        "rel-1",
        _iso(2026, 2, 1),
        [{"slot": "prod", "version_id": "v1", "expected_revision": 0}],
    )
    _publish(
        client,
        "rel-2",
        _iso(2026, 4, 1),
        [{"slot": "prod", "version_id": "v2", "expected_revision": 1}],
    )
    return client


# -- happy path: admitted and frozen -----------------------------------------


def test_version_entry_admitted_over_whole_window(client):
    _version(client, "root")
    _change(client, "lc-root", "root", _iso(2026, 1, 1), ["research", "review"])
    resp = _deliver(
        client,
        "del-1",
        _iso(2026, 3, 1),
        _iso(2026, 5, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["admitted"] is True
    assert body["failures"] == []
    record = body["admission"]
    assert record["delivery_id"] == "del-1"
    assert record["entry_count"] == 1
    entry = record["entries"][0]
    assert entry["index"] == 0
    assert entry["version_id"] == "root"
    assert entry["slot"] is None
    assert entry["use"] == "research"
    assert len(entry["segments"]) == 1
    segment = entry["segments"][0]
    assert segment["allowed"] is True
    assert segment["version_id"] == "root"
    assert segment["release_id"] is None
    assert segment["revision"] is None
    assert segment["blockers"] == []
    assert {b["change_id"] for b in segment["basis"]} == {"lc-root"}
    # Traceable by delivery id; identical to the frozen record.
    fetched = client.get("/deliveries/admissions/del-1")
    assert fetched.status_code == 200
    assert fetched.json() == record


def test_slot_entry_segments_follow_release_switches(catalog):
    resp = _deliver(
        catalog,
        "del-slot",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        [{"slot": "prod", "use": "research"}],
    )
    assert resp.status_code == 201, resp.text
    record = resp.json()["admission"]
    segments = record["entries"][0]["segments"]
    # The window crosses rel-2 (2026-04-01): research stays allowed but the
    # resolved version changes, so the record splits there.
    assert [(s["version_id"], s["release_id"], s["revision"]) for s in segments] == [
        ("v1", "rel-1", 0),
        ("v2", "rel-2", 1),
    ]
    assert segments[0]["start"].startswith("2026-02-01T00:00:00")
    assert segments[0]["end"].startswith("2026-04-01T00:00:00")
    assert segments[1]["start"].startswith("2026-04-01T00:00:00")
    assert segments[1]["end"].startswith("2026-05-01T00:00:00")
    assert all(s["allowed"] for s in segments)
    assert {b["change_id"] for b in segments[0]["basis"]} == {"lc-root", "lc-v1"}
    assert {b["change_id"] for b in segments[1]["basis"]} == {"lc-root", "lc-v1", "lc-v2"}


def test_mixed_version_and_slot_entries_all_must_pass(catalog):
    resp = _deliver(
        catalog,
        "del-mixed",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        [
            {"version_id": "root", "use": "review"},
            {"slot": "prod", "use": "research"},
        ],
    )
    assert resp.status_code == 201, resp.text
    record = resp.json()["admission"]
    assert record["entry_count"] == 2
    assert record["entries"][0]["version_id"] == "root"
    assert record["entries"][1]["slot"] == "prod"
    assert len(record["entries"][1]["segments"]) == 2  # release switch at 04-01


def test_release_exactly_at_start_applies_and_at_end_is_ignored(catalog):
    # Window starts exactly at rel-2's effective instant: v2 resolves from the
    # first slice on. Window ends exactly at rel-3's instant: not counted.
    _version(catalog, "v3", ["v2"])
    _change(catalog, "lc-v3", "v3", _iso(2026, 1, 1), ["research", "review"])
    _publish(
        catalog,
        "rel-3",
        _iso(2026, 5, 1),
        [{"slot": "prod", "version_id": "v3", "expected_revision": 2}],
    )
    resp = _deliver(
        catalog,
        "del-edges",
        _iso(2026, 4, 1),
        _iso(2026, 5, 1),
        [{"slot": "prod", "use": "research"}],
    )
    assert resp.status_code == 201, resp.text
    segments = resp.json()["admission"]["entries"][0]["segments"]
    assert [(s["version_id"], s["revision"]) for s in segments] == [("v2", 1)]


def test_license_change_exactly_at_start_applies_and_at_end_is_ignored(client):
    _version(client, "root")
    _change(client, "lc-1", "root", _iso(2026, 3, 1), ["research"])
    # A change exactly at the window end must not influence the window.
    resp = _deliver(
        client,
        "del-lic-edges",
        _iso(2026, 3, 1),
        _iso(2026, 4, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 201, resp.text
    segments = resp.json()["admission"]["entries"][0]["segments"]
    assert len(segments) == 1
    assert {b["change_id"] for b in segments[0]["basis"]} == {"lc-1"}
    # And a change at the exact end instant stays invisible.
    _change(client, "lc-2", "root", _iso(2026, 4, 1), ["review"])
    resp = _deliver(
        client,
        "del-lic-edges-2",
        _iso(2026, 3, 1),
        _iso(2026, 4, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 201, resp.text
    segments = resp.json()["admission"]["entries"][0]["segments"]
    assert len(segments) == 1
    assert {b["change_id"] for b in segments[0]["basis"]} == {"lc-1"}


def test_omitted_delivery_id_is_generated(client):
    _version(client, "root")
    _change(client, "lc-root", "root", _iso(2026, 1, 1), ["research"])
    resp = client.post(
        "/deliveries/admissions",
        json={
            "start": _iso(2026, 2, 1),
            "end": _iso(2026, 3, 1),
            "entries": [{"version_id": "root", "use": "research"}],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["delivery_id"].startswith("d-")


# -- rejected manifests: failures reported, nothing persisted -----------------


def test_license_failure_reports_first_failing_window_and_no_record(catalog):
    # review is dropped by v2 from 2026-04-01 on; the first failing window
    # starts at the release switch, not at the window start.
    resp = _deliver(
        catalog,
        "del-deny",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        [{"slot": "prod", "use": "review"}],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["admitted"] is False
    assert body["admission"] is None
    assert len(body["failures"]) == 1
    failure = body["failures"][0]
    assert failure["index"] == 0
    assert failure["slot"] == "prod"
    assert failure["use"] == "review"
    first = failure["first_failure"]
    assert first["start"].startswith("2026-04-01T00:00:00")
    assert first["end"].startswith("2026-05-01T00:00:00")
    assert first["version_id"] == "v2"
    assert first["release_id"] == "rel-2"
    assert first["revision"] == 1
    assert [(b["version_id"], b["reason"]) for b in first["blockers"]] == [
        ("v2", "use_not_allowed")
    ]
    # A rejected delivery leaves no admission record behind.
    assert catalog.get("/deliveries/admissions/del-deny").status_code == 404


def test_first_failure_is_the_earliest_of_several(client):
    _version(client, "root")
    # Allowed only during March: gaps before and after.
    _change(client, "lc-1", "root", _iso(2026, 3, 1), ["research"])
    _change(client, "lc-2", "root", _iso(2026, 4, 1), ["review"])
    resp = _deliver(
        client,
        "del-gaps",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 200
    first = resp.json()["failures"][0]["first_failure"]
    # The earliest failure is the pre-license window, not the April gap.
    assert first["start"].startswith("2026-02-01T00:00:00")
    assert first["end"].startswith("2026-03-01T00:00:00")
    assert first["blockers"] == [
        {
            "version_id": "root",
            "reason": "no_valid_license",
            "change_id": None,
            "effective_at": None,
            "allowed_uses": [],
            "slot": None,
        }
    ]


def test_only_failing_entries_are_reported(catalog):
    resp = _deliver(
        catalog,
        "del-partial",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        [
            {"version_id": "root", "use": "research"},  # always allowed
            {"slot": "prod", "use": "review"},  # denied from 04-01
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["admitted"] is False
    assert [f["index"] for f in body["failures"]] == [1]


def test_slot_never_published_counts_as_failure(catalog):
    resp = _deliver(
        catalog,
        "del-ghost-slot",
        _iso(2026, 3, 1),
        _iso(2026, 5, 1),
        [{"slot": "ghost", "use": "research"}],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["admitted"] is False
    first = body["failures"][0]["first_failure"]
    assert first["version_id"] is None
    assert first["release_id"] is None
    assert first["revision"] is None
    assert first["blockers"] == [
        {
            "version_id": None,
            "reason": "slot_not_published",
            "change_id": None,
            "effective_at": None,
            "allowed_uses": [],
            "slot": "ghost",
        }
    ]
    assert catalog.get("/deliveries/admissions/del-ghost-slot").status_code == 404


def test_slot_published_only_mid_window_fails_on_the_unpublished_prefix(catalog):
    # prod's first publish is 2026-02-01; a window starting earlier fails on
    # the prefix even though the rest of the window would be allowed.
    resp = _deliver(
        catalog,
        "del-prefix",
        _iso(2026, 1, 15),
        _iso(2026, 3, 1),
        [{"slot": "prod", "use": "research"}],
    )
    assert resp.status_code == 200
    first = resp.json()["failures"][0]["first_failure"]
    assert first["start"].startswith("2026-01-15T00:00:00")
    assert first["end"].startswith("2026-02-01T00:00:00")
    assert first["blockers"][0]["reason"] == "slot_not_published"
    assert first["blockers"][0]["slot"] == "prod"


def test_revoked_change_is_ignored_across_the_window(client):
    _version(client, "root")
    _change(client, "lc-1", "root", _iso(2026, 1, 1), ["research"])
    client.post(
        "/license-changes/revocations",
        json={"change_id": "lc-1", "revocation_id": "rev-1", "reason": "登记有误"},
    )
    resp = _deliver(
        client,
        "del-revoked",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 200
    assert resp.json()["admitted"] is False
    assert resp.json()["failures"][0]["first_failure"]["blockers"][0]["reason"] == (
        "no_valid_license"
    )


def test_failed_delivery_can_succeed_after_catalog_is_fixed(catalog):
    entries = [{"slot": "canary", "use": "research"}]
    resp = _deliver(catalog, "del-retry", _iso(2026, 3, 1), _iso(2026, 5, 1), entries)
    assert resp.status_code == 200
    assert resp.json()["admitted"] is False
    # Fix the catalog: publish canary covering the window, then retry the very
    # same input — no record existed, so the retry is evaluated fresh.
    _publish(
        catalog,
        "rel-c1",
        _iso(2026, 3, 1),
        [{"slot": "canary", "version_id": "v1", "expected_revision": 0}],
    )
    resp = _deliver(catalog, "del-retry", _iso(2026, 3, 1), _iso(2026, 5, 1), entries)
    assert resp.status_code == 201, resp.text
    assert resp.json()["admitted"] is True
    # A different input with the same id after a FAILURE is not a conflict
    # either (nothing was recorded); it simply evaluates on its own.
    resp = _deliver(
        catalog,
        "del-fail-again",
        _iso(2026, 3, 1),
        _iso(2026, 5, 1),
        [{"slot": "ghost", "use": "research"}],
    )
    assert resp.status_code == 200
    resp = _deliver(
        catalog,
        "del-fail-again",
        _iso(2026, 3, 1),
        _iso(2026, 6, 1),
        [{"slot": "ghost", "use": "research"}],
    )
    assert resp.status_code == 200
    assert resp.json()["admitted"] is False


# -- idempotency / immutability / conflict ------------------------------------


def test_same_id_same_normalised_input_replays_first_record(catalog):
    first = _deliver(
        catalog,
        "del-idem",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        [
            {"version_id": "root", "use": " research "},
            {"slot": " prod ", "use": "research"},
        ],
    )
    assert first.status_code == 201
    # Same UTC window written with an offset, padded uses/slot names, same
    # ordered entries -> the normalised input is identical.
    retry = _deliver(
        catalog,
        "del-idem",
        "2026-02-01T08:00:00+08:00",
        "2026-05-01T08:00:00+08:00",
        [
            {"version_id": "root", "use": "research"},
            {"slot": "prod", "use": " research"},
        ],
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["replayed"] is True
    assert retry.json()["admitted"] is True
    assert retry.json()["admission"] == first.json()["admission"]


def test_later_catalog_changes_cannot_rewrite_a_frozen_record(catalog):
    first = _deliver(
        catalog,
        "del-frozen",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert first.status_code == 201
    # Tighten the catalog so the same window would now be denied.
    _change(catalog, "lc-tighten", "root", _iso(2026, 2, 15), ["review"])
    live = catalog.get(
        "/permissions/check",
        params={"version_id": "root", "use": "research", "at": _iso(2026, 2, 20)},
    )
    assert live.json()["allowed"] is False
    # The frozen record and the idempotent retry are unaffected.
    fetched = catalog.get("/deliveries/admissions/del-frozen")
    assert fetched.status_code == 200
    assert fetched.json() == first.json()["admission"]
    retry = _deliver(
        catalog,
        "del-frozen",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["admission"] == first.json()["admission"]


def test_same_id_different_input_is_409(catalog):
    first = _deliver(
        catalog,
        "del-conflict",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert first.status_code == 201
    # Different window.
    resp = _deliver(
        catalog,
        "del-conflict",
        _iso(2026, 2, 1),
        _iso(2026, 4, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 409
    # Different use.
    resp = _deliver(
        catalog,
        "del-conflict",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "review"}],
    )
    assert resp.status_code == 409
    # Different entry reference.
    resp = _deliver(
        catalog,
        "del-conflict",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"slot": "prod", "use": "research"}],
    )
    assert resp.status_code == 409
    # The original record is untouched.
    assert catalog.get("/deliveries/admissions/del-conflict").json() == first.json()[
        "admission"
    ]


def test_reordered_entries_are_different_input(catalog):
    entries = [
        {"version_id": "root", "use": "research"},
        {"slot": "prod", "use": "research"},
    ]
    resp = _deliver(catalog, "del-order", _iso(2026, 2, 1), _iso(2026, 5, 1), entries)
    assert resp.status_code == 201
    resp = _deliver(
        catalog,
        "del-order",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        list(reversed(entries)),
    )
    assert resp.status_code == 409


def test_unknown_delivery_id_is_404(client):
    resp = client.get("/deliveries/admissions/no-such-delivery")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# -- request validation: explicit errors, nothing persisted -------------------


def test_unknown_version_is_404_and_nothing_persisted(catalog):
    resp = _deliver(
        catalog,
        "del-unknown",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [
            {"version_id": "root", "use": "research"},
            {"version_id": "ghost", "use": "research"},
        ],
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert "ghost" in resp.json()["details"]["missing_version_ids"]
    assert catalog.get("/deliveries/admissions/del-unknown").status_code == 404


def test_naive_window_instants_are_422(catalog):
    resp = _deliver(
        catalog,
        "del-naive",
        "2026-02-01T00:00:00",
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 422
    resp = _deliver(
        catalog,
        "del-naive",
        _iso(2026, 2, 1),
        "2026-03-01T00:00:00",
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 422
    assert catalog.get("/deliveries/admissions/del-naive").status_code == 404


def test_empty_or_inverted_window_is_422(catalog):
    # Inverted.
    resp = _deliver(
        catalog,
        "del-window",
        _iso(2026, 3, 1),
        _iso(2026, 2, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_interval"
    # Same instant in two timezone spellings is still an empty window.
    resp = _deliver(
        catalog,
        "del-window",
        "2026-03-01T08:00:00+08:00",
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "research"}],
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_interval"
    assert catalog.get("/deliveries/admissions/del-window").status_code == 404


def test_blank_use_is_422(catalog):
    resp = _deliver(
        catalog,
        "del-blank",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "root", "use": "   "}],
    )
    assert resp.status_code == 422
    assert catalog.get("/deliveries/admissions/del-blank").status_code == 404


def test_empty_entry_list_is_422(catalog):
    resp = _deliver(catalog, "del-empty", _iso(2026, 2, 1), _iso(2026, 3, 1), [])
    assert resp.status_code == 422


def test_entry_must_reference_exactly_one_of_version_or_slot(catalog):
    # Both references at once.
    resp = _deliver(
        catalog,
        "del-both",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "root", "slot": "prod", "use": "research"}],
    )
    assert resp.status_code == 422
    # Neither reference.
    resp = _deliver(
        catalog,
        "del-neither",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"use": "research"}],
    )
    assert resp.status_code == 422
    # Blank slot name.
    resp = _deliver(
        catalog,
        "del-blank-slot",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"slot": "   ", "use": "research"}],
    )
    assert resp.status_code == 422


# -- evaluation semantics ------------------------------------------------------


def test_diamond_closure_is_evaluated_once(client):
    # Diamond: top derives from mid-a and mid-b, both from base.
    _version(client, "base")
    _version(client, "mid-a", ["base"])
    _version(client, "mid-b", ["base"])
    _version(client, "top", ["mid-a", "mid-b"])
    _change(client, "lc-base", "base", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "lc-a", "mid-a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "lc-b", "mid-b", _iso(2026, 1, 1), ["research"])
    _change(client, "lc-top", "top", _iso(2026, 1, 1), ["research", "review"])
    resp = _deliver(
        client,
        "del-diamond",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "top", "use": "research"}],
    )
    assert resp.status_code == 201, resp.text
    segment = resp.json()["admission"]["entries"][0]["segments"][0]
    assert sorted(b["version_id"] for b in segment["basis"]) == [
        "base",
        "mid-a",
        "mid-b",
        "top",
    ]
    # review is blocked by mid-b for the whole window.
    resp = _deliver(
        client,
        "del-diamond-2",
        _iso(2026, 2, 1),
        _iso(2026, 3, 1),
        [{"version_id": "top", "use": "review"}],
    )
    assert resp.status_code == 200
    blockers = resp.json()["failures"][0]["first_failure"]["blockers"]
    assert [(b["version_id"], b["reason"]) for b in blockers] == [
        ("mid-b", "use_not_allowed")
    ]


def test_segments_split_on_license_changes_inside_a_resolved_slice(client):
    _version(client, "root")
    _change(client, "lc-1", "root", _iso(2026, 1, 1), ["research"])
    _change(client, "lc-2", "root", _iso(2026, 3, 15), ["research", "review"])
    _publish(
        client,
        "rel-x",
        _iso(2026, 2, 1),
        [{"slot": "prod", "version_id": "root", "expected_revision": 0}],
    )
    resp = _deliver(
        client,
        "del-split",
        _iso(2026, 3, 1),
        _iso(2026, 4, 1),
        [{"slot": "prod", "use": "review"}],
    )
    assert resp.status_code == 200
    first = resp.json()["failures"][0]["first_failure"]
    # review becomes allowed only from 03-15; the first failing window is
    # [03-01, 03-15) with the lc-1 basis.
    assert first["start"].startswith("2026-03-01T00:00:00")
    assert first["end"].startswith("2026-03-15T00:00:00")
    assert first["version_id"] == "root"
    assert first["blockers"][0]["reason"] == "use_not_allowed"
    assert first["blockers"][0]["change_id"] == "lc-1"
    # research is admitted across the same window; the basis change at 03-15
    # still splits the frozen record into two segments.
    resp = _deliver(
        client,
        "del-split-ok",
        _iso(2026, 3, 1),
        _iso(2026, 4, 1),
        [{"slot": "prod", "use": "research"}],
    )
    assert resp.status_code == 201
    segments = resp.json()["admission"]["entries"][0]["segments"]
    assert len(segments) == 2
    assert {b["change_id"] for b in segments[0]["basis"]} == {"lc-1"}
    assert {b["change_id"] for b in segments[1]["basis"]} == {"lc-2"}


def test_delivery_evaluation_uses_one_consistent_snapshot(catalog):
    # A delivery admitted at 201 must equal point-in-time checks done with the
    # same data: cross-check the frozen segments against the timeline API.
    resp = _deliver(
        catalog,
        "del-xcheck",
        _iso(2026, 2, 1),
        _iso(2026, 5, 1),
        [{"slot": "prod", "use": "research"}],
    )
    assert resp.status_code == 201
    segments = resp.json()["admission"]["entries"][0]["segments"]
    for segment, version in zip(segments, ["v1", "v2"]):
        timeline = catalog.get(
            "/permissions/timeline",
            params={
                "version_id": version,
                "use": "research",
                "start": segment["start"],
                "end": segment["end"],
            },
        )
        assert timeline.status_code == 200
        assert len(timeline.json()["segments"]) == 1
        assert timeline.json()["segments"][0]["allowed"] is True
        assert timeline.json()["segments"][0]["basis"] == segment["basis"]


# -- concurrency ----------------------------------------------------------------


def test_concurrent_same_delivery_id_freezes_exactly_once(catalog):
    payload = {
        "delivery_id": "del-race",
        "start": _iso(2026, 2, 1),
        "end": _iso(2026, 5, 1),
        "entries": [{"slot": "prod", "use": "research"}],
    }
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker() -> None:
        resp = catalog.post("/deliveries/admissions", json=payload)
        with lock:
            results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(status for status, _ in results)
    assert statuses.count(201) == 1
    assert statuses.count(200) == 7
    admissions = {str(body["admission"]) for _, body in results}
    assert len(admissions) == 1  # every caller sees the same frozen record
    fetched = catalog.get("/deliveries/admissions/del-race")
    assert fetched.status_code == 200
