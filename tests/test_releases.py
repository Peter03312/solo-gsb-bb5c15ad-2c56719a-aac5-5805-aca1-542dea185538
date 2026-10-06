"""End-to-end tests for version-slot releases (unified publish & replay)."""

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


def _chain(client, n: int, root: str = "r0") -> None:
    """Register root..root+n-1 as a strict linear descendant chain."""
    r = client.post("/versions", json={"version_id": root, "parent_ids": []})
    assert r.status_code == 201, r.text
    for i in range(1, n):
        vid = f"v{i}"
        r = client.post(
            "/versions",
            json={"version_id": vid, "parent_ids": [f"v{i-1}" if i > 1 else root]},
        )
        assert r.status_code == 201, r.text


def _publish(client, release_id, effective_at, slots, **extra):
    body = {"release_id": release_id, "effective_at": effective_at, "slots": slots}
    body.update(extra)
    return client.post("/releases", json=body)


def _slot(client, slot, version, expected):
    return {"slot": slot, "version_id": version, "expected_revision": expected}


# -- happy path ------------------------------------------------------------


def test_first_publish_assigns_revision_zero_for_every_slot(client):
    _chain(client, 3)
    resp = _publish(
        client,
        "rel-1",
        _iso(2026, 3, 1),
        [_slot(client, "prod", "v1", 0), _slot(client, " canary ", "r0", 0)],
    )
    assert resp.status_code == 201, resp.text
    release = resp.json()["release"]
    assert resp.json()["replayed"] is False
    assert release["release_id"] == "rel-1"
    assert release["slot_count"] == 2
    # Slot names are trimmed and request order is frozen.
    assert [(s["position"], s["slot"], s["revision"], s["expected_revision"]) for s in release["slots"]] == [
        (0, "prod", 0, 0),
        (1, "canary", 0, 0),
    ]
    assert release["effective_at"].startswith("2026-03-01T00:00:00")


def test_subsequent_publish_advances_by_exactly_one(client):
    _chain(client, 4)
    assert _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 0)]).status_code == 201
    resp = _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "prod", "v2", 1)])
    assert resp.status_code == 201, resp.text
    assert resp.json()["release"]["slots"][0]["revision"] == 1
    resp = _publish(client, "rel-3", _iso(2026, 5, 1), [_slot(client, "prod", "v3", 2)])
    assert resp.status_code == 201
    assert resp.json()["release"]["slots"][0]["revision"] == 2


def test_other_slots_in_same_release_advance_independently(client):
    _chain(client, 3)
    _publish(
        client,
        "rel-1",
        _iso(2026, 3, 1),
        [_slot(client, "prod", "v1", 0), _slot(client, "canary", "r0", 0)],
    )
    # Only canary advances; prod keeps revision 0.
    resp = _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "canary", "v1", 1)])
    assert resp.status_code == 201, resp.text
    heads = {h["slot"]: h["revision"] for h in client.get("/slots").json()}
    assert heads == {"prod": 0, "canary": 1}


def test_second_publish_target_may_be_indirect_descendant(client):
    # Diamond: vd has parents v1, v2 (both descend from r0); it is a strict
    # descendant of either previously published grandparent.
    client.post("/versions", json={"version_id": "r0", "parent_ids": []})
    client.post("/versions", json={"version_id": "v1", "parent_ids": ["r0"]})
    client.post("/versions", json={"version_id": "v2", "parent_ids": ["r0"]})
    client.post("/versions", json={"version_id": "vd", "parent_ids": ["v1", "v2"]})
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "r0", 0)])
    resp = _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "prod", "vd", 1)])
    assert resp.status_code == 201, resp.text


def test_omitted_release_id_is_generated(client):
    _chain(client, 2)
    resp = client.post(
        "/releases",
        json={"effective_at": _iso(2026, 3, 1), "slots": [_slot(client, "prod", "r0", 0)]},
    )
    assert resp.status_code == 201
    assert resp.json()["release"]["release_id"].startswith("rel-")


def test_timezone_offset_normalised_to_utc(client):
    _chain(client, 2)
    resp = _publish(
        client,
        "rel-1",
        "2026-04-01T08:00:00+08:00",
        [_slot(client, "prod", "r0", 0)],
    )
    assert resp.status_code == 201
    assert resp.json()["release"]["effective_at"].startswith("2026-04-01T00:00:00")


# -- request validation ----------------------------------------------------


def test_empty_slot_list_is_422(client):
    _chain(client, 1)
    resp = _publish(client, "rel-x", _iso(2026, 3, 1), [])
    assert resp.status_code == 422


def test_blank_slot_name_is_422(client):
    _chain(client, 1)
    resp = _publish(
        client, "rel-x", _iso(2026, 3, 1), [_slot(client, "   ", "r0", 0)]
    )
    assert resp.status_code == 422


def test_duplicate_slot_names_after_trim_are_422(client):
    _chain(client, 1)
    resp = _publish(
        client,
        "rel-x",
        _iso(2026, 3, 1),
        [_slot(client, "prod", "r0", 0), _slot(client, " prod ", "r0", 0)],
    )
    assert resp.status_code == 422


def test_naive_effective_at_is_422(client):
    _chain(client, 1)
    resp = _publish(
        client,
        "rel-x",
        "2026-03-01T00:00:00",
        [_slot(client, "prod", "r0", 0)],
    )
    assert resp.status_code == 422


def test_negative_expected_revision_is_422(client):
    _chain(client, 1)
    resp = _publish(
        client, "rel-x", _iso(2026, 3, 1), [_slot(client, "prod", "r0", -1)]
    )
    assert resp.status_code == 422


def test_missing_target_version_is_404_and_whole_release_rejected(client):
    _chain(client, 1)
    resp = _publish(
        client,
        "rel-x",
        _iso(2026, 3, 1),
        [_slot(client, "prod", "r0", 0), _slot(client, "canary", "ghost", 0)],
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert "ghost" in resp.json()["details"]["missing_version_ids"]
    # Nothing was published.
    assert client.get("/releases").json() == []
    assert client.get("/slots").json() == []


# -- per-slot progression rules -------------------------------------------


def test_first_publish_with_nonzero_expected_revision_is_409(client):
    _chain(client, 2)
    resp = _publish(client, "rel-x", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 1)])
    assert resp.status_code == 409
    assert resp.json()["details"]["current_revision"] == "0"


def test_expected_revision_must_equal_current_plus_one(client):
    _chain(client, 4)
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 0)])
    _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "prod", "v2", 1)])
    # current is 1; next must be 2 — sending 1 again is a stale expectation.
    resp = _publish(client, "rel-x", _iso(2026, 5, 1), [_slot(client, "prod", "v3", 1)])
    assert resp.status_code == 409
    details = resp.json()["details"]
    assert details["current_revision"] == "1"
    assert details["required_revision"] == "2"
    # Skipping ahead is rejected too.
    resp = _publish(client, "rel-y", _iso(2026, 5, 1), [_slot(client, "prod", "v3", 3)])
    assert resp.status_code == 409


def test_target_must_be_strict_descendant_not_self(client):
    _chain(client, 2)
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 0)])
    resp = _publish(client, "rel-x", _iso(2026, 4, 1), [_slot(client, "prod", "v1", 1)])
    assert resp.status_code == 409
    assert "严格后代" in resp.json()["message"]


def test_target_on_sibling_branch_is_409(client):
    client.post("/versions", json={"version_id": "r0", "parent_ids": []})
    client.post("/versions", json={"version_id": "a1", "parent_ids": ["r0"]})
    client.post("/versions", json={"version_id": "b1", "parent_ids": ["r0"]})
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "a1", 0)])
    resp = _publish(client, "rel-x", _iso(2026, 4, 1), [_slot(client, "prod", "b1", 1)])
    assert resp.status_code == 409


def test_effective_at_must_be_strictly_later_than_last_slot_publish(client):
    _chain(client, 3)
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 0)])
    # Equal instant is not strictly later.
    resp = _publish(client, "rel-eq", _iso(2026, 3, 1), [_slot(client, "prod", "v2", 1)])
    assert resp.status_code == 409
    # Earlier instant rejected.
    resp = _publish(client, "rel-earlier", _iso(2026, 2, 1), [_slot(client, "prod", "v2", 1)])
    assert resp.status_code == 409
    # The slot did not advance.
    assert client.get("/slots/by-name/prod").json()["revision"] == 0


def test_effective_at_compares_per_slot_not_release_wide(client):
    # Two slots last published at different instants; a release valid for one
    # slot may still be too early for another — the whole release is rejected.
    _chain(client, 3)
    _publish(
        client,
        "rel-1",
        _iso(2026, 3, 1),
        [_slot(client, "early", "v1", 0), _slot(client, "late", "r0", 0)],
    )
    _publish(client, "rel-2", _iso(2026, 6, 1), [_slot(client, "late", "v1", 1)])
    resp = _publish(
        client,
        "rel-3",
        _iso(2026, 4, 1),
        [_slot(client, "early", "v2", 1), _slot(client, "late", "v2", 2)],
    )
    assert resp.status_code == 409
    assert resp.json()["details"]["slot"] == "late"
    # Both slots keep their old revisions.
    heads = {h["slot"]: h["revision"] for h in client.get("/slots").json()}
    assert heads == {"early": 0, "late": 1}


# -- atomicity -------------------------------------------------------------


def test_any_invalid_slot_rejects_whole_release_without_partial_records(client):
    _chain(client, 4)
    # Establish prod@1 (v2) and canary@0 (r0).
    _publish(
        client,
        "rel-1",
        _iso(2026, 3, 1),
        [_slot(client, "prod", "v1", 0), _slot(client, "canary", "r0", 0)],
    )
    _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "prod", "v2", 1)])

    before_releases = len(client.get("/releases").json())
    # canary expects 1 (fine), but its target must be a strict descendant of
    # canary's previous version r0; "side" is a root and therefore invalid.
    client.post("/versions", json={"version_id": "side", "parent_ids": []})
    resp = _publish(
        client,
        "rel-bad",
        _iso(2026, 6, 1),
        [
            _slot(client, "prod", "v3", 2),
            _slot(client, "canary", "side", 1),
        ],
    )
    assert resp.status_code == 409
    after = client.get("/releases").json()
    assert len(after) == before_releases
    assert all(r["release_id"] != "rel-bad" for r in after)
    heads = {h["slot"]: h for h in client.get("/slots").json()}
    assert heads["prod"]["revision"] == 1
    assert heads["canary"]["revision"] == 0


# -- idempotency / conflict ------------------------------------------------


def test_same_release_id_same_normalised_content_replays_first_result(client):
    _chain(client, 3)
    payload = {
        "release_id": "rel-1",
        "effective_at": _iso(2026, 3, 1),
        "slots": [
            {"slot": " prod ", "version_id": "v1", "expected_revision": 0},
            {"slot": "canary", "version_id": "r0", "expected_revision": 0},
        ],
    }
    first = client.post("/releases", json=payload)
    assert first.status_code == 201
    # Same UTC instant written with an offset; slot with different padding;
    # slots in the same order -> normalised identical content.
    retry = client.post(
        "/releases",
        json={
            "release_id": "rel-1",
            "effective_at": "2026-03-01T08:00:00+08:00",
            "slots": [
                {"slot": "prod", "version_id": "v1", "expected_revision": 0},
                {"slot": " canary ", "version_id": "r0", "expected_revision": 0},
            ],
        },
    )
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["release"] == first.json()["release"]
    # Replays never advance anything.
    heads = {h["slot"]: h["revision"] for h in client.get("/slots").json()}
    assert heads == {"prod": 0, "canary": 0}


def test_same_release_id_different_content_is_409(client):
    _chain(client, 3)
    _publish(
        client,
        "rel-1",
        _iso(2026, 3, 1),
        [_slot(client, "prod", "v1", 0)],
    )
    # Different slot list content with the same release id.
    resp = _publish(
        client,
        "rel-1",
        _iso(2026, 4, 1),
        [_slot(client, "prod", "v2", 1)],
    )
    assert resp.status_code == 409
    # Different effective instant only.
    resp = _publish(client, "rel-1", _iso(2026, 5, 1), [_slot(client, "prod", "v1", 0)])
    assert resp.status_code == 409


def test_reordered_slots_are_different_content(client):
    _chain(client, 2)
    _publish(
        client,
        "rel-1",
        _iso(2026, 3, 1),
        [_slot(client, "prod", "r0", 0), _slot(client, "canary", "r0", 0)],
    )
    resp = _publish(
        client,
        "rel-1",
        _iso(2026, 3, 1),
        [_slot(client, "canary", "r0", 0), _slot(client, "prod", "r0", 0)],
    )
    assert resp.status_code == 409


def test_release_record_is_immutable_history(client):
    _chain(client, 4)
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 0)])
    _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "prod", "v2", 1)])
    first = client.get("/releases/rel-1").json()
    again = client.get("/releases/rel-1").json()
    assert first == again
    assert first["slots"][0]["revision"] == 0
    listing = [r["release_id"] for r in client.get("/releases").json()]
    assert listing == ["rel-1", "rel-2"]
    assert client.get("/releases/nope").status_code == 404


# -- historical resolution -------------------------------------------------


def test_resolve_slot_returns_most_recent_release_at_instant(client):
    _chain(client, 4)
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 0)])
    _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "prod", "v2", 1)])
    _publish(client, "rel-3", _iso(2026, 5, 1), [_slot(client, "prod", "v3", 2)])

    # Before everything: explicit error.
    resp = client.get("/slot-resolutions/prod", params={"at": _iso(2026, 2, 1)})
    assert resp.status_code == 404
    assert resp.json()["error"] == "slot_not_published"
    assert resp.json()["details"]["reason"] == "no_release_effective_at_or_before"

    # Exactly at an effective instant the new publish applies.
    resp = client.get("/slot-resolutions/prod", params={"at": _iso(2026, 3, 1)})
    assert resp.status_code == 200
    assert resp.json()["version_id"] == "v1"
    assert resp.json()["revision"] == 0
    assert resp.json()["release_id"] == "rel-1"

    # Between releases: the previous one.
    resp = client.get("/slot-resolutions/prod", params={"at": _iso(2026, 4, 20)})
    assert resp.json()["version_id"] == "v2"
    assert resp.json()["revision"] == 1

    # Timezone offset pointing at the same UTC instant resolves identically.
    resp = client.get(
        "/slot-resolutions/prod",
        params={"at": "2026-05-01T08:00:00+08:00"},
    )
    assert resp.status_code == 200
    assert resp.json()["version_id"] == "v3"
    assert resp.json()["revision"] == 2


def test_resolve_never_published_slot_is_explicit_404(client):
    resp = client.get("/slot-resolutions/ghost", params={"at": _iso(2026, 4, 1)})
    assert resp.status_code == 404
    assert resp.json()["error"] == "slot_not_published"
    assert resp.json()["details"]["reason"] == "slot_never_published"


def test_resolve_requires_timezone(client):
    resp = client.get("/slot-resolutions/prod", params={"at": "2026-03-01T00:00:00"})
    assert resp.status_code == 422


def test_resolved_version_is_evaluated_by_existing_permission_rules(client):
    client.post("/versions", json={"version_id": "r0", "parent_ids": []})
    client.post("/versions", json={"version_id": "v1", "parent_ids": ["r0"]})
    client.post("/versions", json={"version_id": "v2", "parent_ids": ["v1"]})
    _publish(client, "rel-1", _iso(2026, 3, 1), [_slot(client, "prod", "v1", 0)])
    _publish(client, "rel-2", _iso(2026, 4, 1), [_slot(client, "prod", "v2", 1)])
    # r0 and v1 both permit commercial; v2 (the later target) drops it. Along
    # a descendant chain the intersection can only shrink, so advancing the
    # slot from v1 to v2 historically changes the decision from allow to deny.
    client.post(
        "/license-changes",
        json={
            "change_id": "lc-r0",
            "version_id": "r0",
            "effective_at": _iso(2026, 1, 1),
            "allowed_uses": ["research", "review", "commercial"],
        },
    )
    client.post(
        "/license-changes",
        json={
            "change_id": "lc-v1",
            "version_id": "v1",
            "effective_at": _iso(2026, 1, 1),
            "allowed_uses": ["research", "review", "commercial"],
        },
    )
    client.post(
        "/license-changes",
        json={
            "change_id": "lc-v2",
            "version_id": "v2",
            "effective_at": _iso(2026, 1, 1),
            "allowed_uses": ["research", "review"],
        },
    )

    # Mid-March resolves to v1: commercial allowed under the then-current target.
    mar = client.get(
        "/slot-permissions/prod",
        params={"use": "commercial", "at": _iso(2026, 3, 15)},
    )
    assert mar.status_code == 200, mar.text
    assert mar.json()["allowed"] is True
    assert mar.json()["version_id"] == "v1"
    assert mar.json()["resolution"]["revision"] == 0
    assert "commercial" in mar.json()["allowed_uses_intersection"]

    # After rel-2 resolves to v2: the same request is historically denied.
    apr = client.get(
        "/slot-permissions/prod",
        params={"use": "commercial", "at": _iso(2026, 4, 15)},
    )
    assert apr.status_code == 200
    assert apr.json()["allowed"] is False
    assert apr.json()["version_id"] == "v2"
    assert apr.json()["resolution"]["revision"] == 1
    assert {b["version_id"] for b in apr.json()["blockers"]} == {"v2"}

    # research stays allowed at both instants but the resolved version differs.
    assert client.get(
        "/slot-permissions/prod",
        params={"use": "research", "at": _iso(2026, 3, 15)},
    ).json()["version_id"] == "v1"
    assert client.get(
        "/slot-permissions/prod",
        params={"use": "research", "at": _iso(2026, 4, 15)},
    ).json()["version_id"] == "v2"

    # Slot never published at the query instant -> explicit error, no guess.
    early = client.get(
        "/slot-permissions/prod",
        params={"use": "commercial", "at": _iso(2026, 1, 15)},
    )
    assert early.status_code == 404
    assert early.json()["error"] == "slot_not_published"


# -- concurrency -----------------------------------------------------------


def test_concurrent_first_publishes_win_exactly_one(client):
    _chain(client, 2)
    outcomes: list[tuple[int, int]] = []

    def worker(i: int) -> None:
        rid = f"race-{i}"
        resp = _publish(
            client,
            rid,
            f"2026-04-{i + 1:02d}T00:00:00Z",
            [_slot(client, "race1", "v1", 0)],
        )
        outcomes.append((i, resp.status_code))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o[1] == 201]
    assert len(winners) == 1
    assert len([o for o in outcomes if o[1] == 409]) == 7
    assert client.get("/slots/by-name/race1").json()["revision"] == 0


def test_concurrent_chain_publishes_never_skip_revisions(client):
    _chain(client, 12)
    won: list[int] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        for attempt in range(8):
            head = client.get("/slots/by-name/chain")
            if head.status_code == 404:
                current = -1
            else:
                current = head.json()["revision"]
            expected = 0 if current == -1 else current + 1
            target = "r0" if current == -1 else f"v{min(current + 1, 11)}"
            day = current + 2  # strictly later effective instant per win
            resp = _publish(
                client,
                f"chain-{idx}-{attempt}-{expected}",
                f"2027-{day // 28 + 1:02d}-{day % 28 + 1:02d}T00:00:00Z",
                [_slot(client, "chain", target, expected)],
            )
            if resp.status_code == 201:
                with lock:
                    won.append(resp.json()["release"]["slots"][0]["revision"])
            elif resp.status_code != 409:
                raise AssertionError(f"unexpected status {resp.status_code}: {resp.text}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(won) == list(range(len(won)))
    final = client.get("/slots/by-name/chain").json()
    assert final["revision"] == len(won) - 1
    # Every revision 0..N-1 exists exactly once in the immutable history.
    releases = client.get("/releases").json()
    revisions = sorted(
        s["revision"] for r in releases for s in r["slots"] if s["slot"] == "chain"
    )
    assert revisions == list(range(len(revisions)))


def test_concurrent_same_release_id_commits_or_replays_exactly_once(client):
    _chain(client, 2)
    payload = {
        "release_id": "same-id",
        "effective_at": _iso(2026, 3, 1),
        "slots": [_slot(client, "prod", "v1", 0)],
    }
    statuses: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        resp = client.post("/releases", json=payload)
        with lock:
            statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses).count(201) == 1
    assert sorted(statuses).count(200) == 7
    releases = client.get("/releases").json()
    assert [r["release_id"] for r in releases] == ["same-id"]
