"""End-to-end API tests (FastAPI TestClient over a temporary SQLite file)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.seed import seed_demo_data
from app.service import LicenseService


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=False, echo=False)
    app = create_app(settings)
    # The lifespan creates the schema (INIT_DB=true) and owns the connection.
    with TestClient(app) as test_client:
        yield test_client


def _iso(y: int, m: int, d: int, hh: int = 0) -> str:
    return datetime(y, m, d, hh, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _register(client, version_id, parents=None, note=""):
    resp = client.post(
        "/versions",
        json={"version_id": version_id, "parent_ids": parents or [], "note": note},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _change(client, change_id, version_id, at, uses):
    resp = client.post(
        "/license-changes",
        json={
            "change_id": change_id,
            "version_id": version_id,
            "effective_at": at,
            "allowed_uses": uses,
        },
    )
    return resp


def _check(client, version_id, use, at):
    return client.get(
        "/permissions/check",
        params={"version_id": version_id, "use": use, "at": at},
    )


def _preview(client, payload):
    return client.post("/license-changes/preview", json=payload)


def _preview_flip(client, change_id, version_id, effective_at, uses, use, at):
    return _preview(
        client,
        {
            "change_id": change_id,
            "version_id": version_id,
            "effective_at": effective_at,
            "allowed_uses": uses,
            "use": use,
            "at": at,
        },
    )


# -- registration ----------------------------------------------------------


def test_register_version_with_unknown_parent_is_404(client):
    resp = client.post(
        "/versions", json={"version_id": "v1", "parent_ids": ["ghost"]}
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_parent_ids_deduplicated_and_immutable(client):
    _register(client, "p1")
    body = _register(client, "c1", ["p1", "p1"])
    assert body["version"]["parent_ids"] == ["p1"]

    # Re-registering the same id conflicts; there is no edit endpoint.
    resp = client.post("/versions", json={"version_id": "c1", "parent_ids": []})
    assert resp.status_code == 409


def test_unknown_version_query_is_404(client):
    assert client.get("/versions/nope").status_code == 404
    assert _check(client, "nope", "research", _iso(2026, 5, 1)).status_code == 404


# -- license changes -------------------------------------------------------


def test_change_requires_existing_version(client):
    resp = _change(client, "c1", "nope", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 404


def test_duplicate_submission_same_id_same_content_is_idempotent(client):
    _register(client, "v1")
    payload = {
        "change_id": "c1",
        "version_id": "v1",
        "effective_at": _iso(2026, 1, 1),
        "allowed_uses": ["research", "review"],
    }
    first = client.post("/license-changes", json=payload)
    assert first.status_code == 201
    assert first.json()["replayed"] is False

    # Same content, uses reordered and duplicated -> identical result.
    second = client.post(
        "/license-changes",
        json={**payload, "allowed_uses": ["review", "research", "research"]},
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["change"] == first.json()["change"]


def test_same_id_different_content_conflicts(client):
    _register(client, "v1")
    assert _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"]).status_code == 201

    # Different uses.
    resp = _change(client, "c1", "v1", _iso(2026, 1, 1), ["commercial"])
    assert resp.status_code == 409
    # Different effective time with the same id.
    resp = _change(client, "c1", "v1", _iso(2026, 2, 1), ["research"])
    assert resp.status_code == 409
    # Different target version with the same id.
    _register(client, "v2")
    resp = _change(client, "c1", "v2", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 409


def test_same_version_same_time_different_change_conflicts(client):
    _register(client, "v1")
    assert _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"]).status_code == 201
    resp = _change(client, "c2", "v1", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 409
    assert "同一生效时刻" in resp.json()["message"]


def test_latest_change_wins_and_naive_time_rejected(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "v1", _iso(2026, 6, 1), ["research"])

    before = _check(client, "v1", "review", _iso(2026, 5, 1)).json()
    assert before["allowed"] is True
    after = _check(client, "v1", "review", _iso(2026, 6, 1)).json()
    assert after["allowed"] is False
    assert after["blockers"][0]["reason"] == "use_not_allowed"
    assert after["blockers"][0]["change_id"] == "c2"

    # Timezone-aware values in other offsets are accepted; naive is 422.
    resp = client.post(
        "/license-changes",
        json={
            "change_id": "c3",
            "version_id": "v1",
            "effective_at": "2026-09-01T08:00:00+08:00",
            "allowed_uses": ["research"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["change"]["effective_at"].startswith("2026-09-01T00:00:00")

    bad = client.post(
        "/license-changes",
        json={
            "change_id": "c4",
            "version_id": "v1",
            "effective_at": "2026-09-01T00:00:00",
            "allowed_uses": ["research"],
        },
    )
    assert bad.status_code == 422


# -- permission evaluation -------------------------------------------------


def test_ancestor_intersection_denies_and_reports_blockers(client):
    _register(client, "a")
    _register(client, "b")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research", "commercial"])
    _register(client, "d1", ["a", "b"])
    _change(client, "cd1", "d1", _iso(2026, 2, 1), ["research", "review", "commercial"])

    at = _iso(2026, 3, 1)
    ok = _check(client, "d1", "research", at).json()
    assert ok["allowed"] is True
    assert ok["closure"] == ["d1", "a", "b"]
    assert {entry["version_id"] for entry in ok["basis"]} == {"d1", "a", "b"}
    assert ok["allowed_uses_intersection"] == ["research"]

    denied = _check(client, "d1", "review", at).json()
    assert denied["allowed"] is False
    reasons = {(bl["version_id"], bl["reason"]) for bl in denied["blockers"]}
    assert ("b", "use_not_allowed") in reasons
    assert all(bl["version_id"] != "a" for bl in denied["blockers"])


def test_diamond_ancestors_deduplicated(client):
    # a <- b <- d1, a <- d1 : a must only appear once in the closure.
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research"])
    _register(client, "d1", ["b", "a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research"])

    body = _check(client, "d1", "research", _iso(2026, 2, 1)).json()
    assert body["closure"] == ["d1", "b", "a"]
    assert body["allowed"] is True


def test_no_valid_license_blocks(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 3, 1), ["research"])
    # Before the change takes effect there is no valid license.
    body = _check(client, "a", "research", _iso(2026, 2, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]
    assert body["basis"] == []
    assert body["allowed_uses_intersection"] == []

    # A child inherits the block from its parent.
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "commercial"])
    body = _check(client, "d1", "research", _iso(2026, 2, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]


def test_naive_query_at_is_422(client):
    _register(client, "a")
    resp = client.get(
        "/permissions/check",
        params={"version_id": "a", "use": "research", "at": "2026-01-01T00:00:00"},
    )
    assert resp.status_code == 422


# -- demo seed -------------------------------------------------------------


def test_demo_seed_is_idempotent(tmp_path):
    db_path = str(tmp_path / "seed.db")
    db = Database(db_path)
    db.init_schema()
    assert seed_demo_data(db) is True
    # Second run on the non-empty DB is skipped; resubmitting directly still
    # returns the same stored change (replay).
    assert seed_demo_data(db) is False
    change, replayed = LicenseService(db).submit_change(
        change_id="demo-chg-a-1",
        version_id="demo-root-a",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        allowed_uses=["research", "review"],
    )
    assert replayed is True
    assert change["change_id"] == "demo-chg-a-1"
    db.close()


def test_demo_fixture_permissions(tmp_path):
    db_path = str(tmp_path / "seed2.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        at = _iso(2026, 3, 1)
        research = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "research", "at": at},
        ).json()
        assert research["allowed"] is True

        commercial = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "commercial", "at": at},
        ).json()
        assert commercial["allowed"] is False
        assert commercial["blockers"][0]["version_id"] == "demo-root-a"

        nolicense = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-no-license", "use": "research", "at": at},
        ).json()
        assert nolicense["allowed"] is False
        assert nolicense["blockers"][0]["reason"] == "no_valid_license"

        # After root-a's June change drops review, intersection still allows
        # only research; research stays allowed.
        later = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "research", "at": _iso(2026, 7, 1)},
        ).json()
        assert later["allowed"] is True


# -- change preview --------------------------------------------------------


def _preview_graph(client):
    # a <- b <- d1, a <- d2 <- d1  (diamond: a is a shared ancestor of d1).
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d2", ["a"])
    _change(client, "cd2", "d2", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d1", ["b", "d2"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])


def test_preview_flips_target_and_descendants_deny_to_allow(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    at = _iso(2026, 5, 1)
    # Current state: review is blocked by a for both a and d1.
    assert _check(client, "a", "review", at).json()["allowed"] is False
    assert _check(client, "d1", "review", at).json()["allowed"] is False

    resp = _preview_flip(client, "ca2", "a", _iso(2026, 4, 1), ["research", "review"], "review", at)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["scope"] == ["a", "d1"]
    flips = {entry["version_id"]: entry for entry in body["impacted_versions"]}
    assert set(flips) == {"a", "d1"}
    for entry in flips.values():
        assert entry["before_allowed"] is False
        assert entry["after_allowed"] is True
        assert entry["before_blockers"]
        assert all(bl["version_id"] == "a" and bl["reason"] == "use_not_allowed"
                   for bl in entry["before_blockers"])
        assert entry["after_blockers"] == []
    assert flips["d1"]["closure"] == ["d1", "a"]
    assert flips["a"]["after_allowed_uses_intersection"] == ["research", "review"]


def test_preview_flip_allow_to_deny_reports_after_blocker(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    at = _iso(2026, 5, 1)
    body = _preview_flip(client, "ca2", "a", _iso(2026, 4, 1), ["research"], "review", at).json()
    flips = {entry["version_id"]: entry for entry in body["impacted_versions"]}
    assert set(flips) == {"a", "d1"}
    for entry in flips.values():
        assert entry["before_allowed"] is True
        assert entry["after_allowed"] is False
        assert entry["before_blockers"] == []
        assert len(entry["after_blockers"]) == 1
        blocker = entry["after_blockers"][0]
        assert blocker["version_id"] == "a"
        assert blocker["reason"] == "use_not_allowed"
        assert blocker["change_id"] == "ca2"


def test_preview_query_before_effective_time_has_no_impact(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    at = _iso(2026, 3, 1)

    # Effective after the query instant: the proposal is not yet in force.
    body = _preview_flip(client, "cf", "a", _iso(2026, 4, 1), ["research", "review"], "review", at).json()
    assert body["scope"] == ["a"]
    assert body["impacted_versions"] == []
    # And nothing was written.
    assert _check(client, "a", "review", at).json()["allowed"] is False
    assert client.get("/license-changes/cf").status_code == 404


def test_preview_covered_by_later_stored_change_has_no_impact(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research"])
    # A stored change already effective later than the proposal wins.
    _change(client, "ca3", "a", _iso(2026, 5, 1), ["research", "review"])
    at = _iso(2026, 8, 1)

    body = _preview_flip(client, "ca2", "a", _iso(2026, 3, 1), ["review", "research"], "review", at).json()
    assert body["impacted_versions"] == []
    assert client.get("/license-changes/ca2").status_code == 404

    # A proposal effective later than every stored change does flip the state.
    body = _preview_flip(client, "ca4", "a", _iso(2026, 6, 1), ["research"], "review", at).json()
    entry = body["impacted_versions"][0]
    assert entry["version_id"] == "a"
    assert entry["before_allowed"] is True
    assert entry["after_allowed"] is False
    assert entry["after_blockers"][0]["change_id"] == "ca4"
    assert client.get("/license-changes/ca4").status_code == 404


def test_preview_unknown_version_is_404(client):
    resp = _preview_flip(client, "c1", "ghost", _iso(2026, 1, 1), ["research"], "research", _iso(2026, 2, 1))
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_preview_change_id_conflict_is_409(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research"])
    resp = _preview_flip(client, "c1", "a", _iso(2026, 2, 1), ["commercial"], "commercial", _iso(2026, 3, 1))
    assert resp.status_code == 409
    assert "变更标识" in resp.json()["message"]
    # Preview still writes nothing.
    assert _change(client, "c2", "a", _iso(2026, 2, 1), ["commercial"]).status_code == 201


def test_preview_same_version_same_time_conflict_is_409(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research"])
    resp = _preview_flip(client, "c2", "a", _iso(2026, 1, 1), ["review"], "review", _iso(2026, 2, 1))
    assert resp.status_code == 409
    assert "同一生效时刻" in resp.json()["message"]


def test_preview_same_id_same_content_replay_has_no_impact(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research", "review"])
    at = _iso(2026, 5, 1)
    body = _preview_flip(client, "c1", "a", _iso(2026, 1, 1), ["review", "research", "review"], "review", at).json()
    assert body["replayed"] is True
    assert body["impacted_versions"] == []

    # Even a proposal that *would* conflict on time is neutralised when the
    # stored change with that id is itself the one at that time.
    assert _check(client, "a", "review", at).json()["allowed"] is True


def test_preview_naive_times_are_422(client):
    _register(client, "a")
    base = {
        "change_id": "c1",
        "version_id": "a",
        "effective_at": _iso(2026, 1, 1),
        "allowed_uses": ["research"],
        "use": "research",
        "at": _iso(2026, 2, 1),
    }
    bad_effective = {**base, "effective_at": "2026-01-01T00:00:00"}
    assert _preview(client, bad_effective).status_code == 422
    bad_query = {**base, "at": "2026-02-01T00:00:00"}
    assert _preview(client, bad_query).status_code == 422


def test_preview_diamond_descendant_counted_once(client):
    _preview_graph(client)
    at = _iso(2026, 5, 1)
    body = _preview_flip(
        client, "ca2", "a", _iso(2026, 4, 1), ["research"], "review", at
    ).json()
    # Each descendant of a appears exactly once despite the diamond edges.
    assert body["scope"] == ["a", "b", "d2", "d1"]
    assert sorted(entry["version_id"] for entry in body["impacted_versions"]) == ["a", "b", "d1", "d2"]
    d1 = next(entry for entry in body["impacted_versions"] if entry["version_id"] == "d1")
    assert d1["closure"] == ["d1", "b", "d2", "a"]
    # a blocks d1 exactly once.
    assert [bl["version_id"] for bl in d1["after_blockers"]] == ["a"]
    # No writes happened.
    assert client.get("/license-changes/ca2").status_code == 404


def test_preview_fills_no_valid_license_gap(client):
    # A change on a version with no earlier license can remove its block.
    _register(client, "a")
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research"])
    at = _iso(2026, 5, 1)
    assert _check(client, "d1", "research", at).json()["allowed"] is False

    body = _preview_flip(client, "ca1", "a", _iso(2026, 3, 1), ["research"], "research", at).json()
    flips = {entry["version_id"]: entry for entry in body["impacted_versions"]}
    assert set(flips) == {"a", "d1"}
    assert flips["a"]["before_blockers"][0]["reason"] == "no_valid_license"
    assert flips["d1"]["before_blockers"][0]["reason"] == "no_valid_license"
    assert all(entry["after_allowed"] is True for entry in flips.values())


def test_preview_does_not_write_under_concurrent_commits(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    for i in range(6):
        _register(client, f"d{i}", ["a"])
        _change(client, f"cd{i}", f"d{i}", _iso(2026, 1, 1), ["research", "review"])

    # Exercise the snapshot guarantee directly: the one process-wide SQLite
    # connection is shared by all threads (check_same_thread=False), so a
    # preview's reads must be serialised against writes via the db lock and
    # must never persist their hypothetical change.
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    errors: list[str] = []

    def preview_loop(idx: int) -> None:
        for i in range(25):
            try:
                result = service.preview_change(
                    change_id=f"cp-{idx}-{i}",
                    version_id="a",
                    effective_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
                    allowed_uses=["research", "review"],
                    use="review",
                    at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"preview raised: {type(exc).__name__}: {exc}")
                return
            scope = set(result["scope"])
            for entry in result["impacted_versions"]:
                if not set(entry["closure"]) <= scope:
                    errors.append("closure mixed with versions outside snapshot scope")
                    return
                if len(entry["closure"]) != len(set(entry["closure"])):
                    errors.append("diamond descendant counted twice")
                    return

    def submit_loop(idx: int) -> None:
        from datetime import timedelta

        base_day = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for i in range(25):
            # Disjoint effective instants per thread -> no same-time 409.
            effective = base_day + timedelta(days=idx * 31 + i)
            try:
                _, replayed = service.submit_change(
                    change_id=f"w-{idx}-{i}",
                    version_id="a",
                    effective_at=effective,
                    allowed_uses=["research"],
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"submit raised: {type(exc).__name__}: {exc}")
                return
            if replayed:
                errors.append("fresh change unexpectedly replayed")
                return

    import threading

    threads = [threading.Thread(target=preview_loop, args=(i,)) for i in range(3)]
    threads += [threading.Thread(target=submit_loop, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # None of the preview-only change ids leaked into the store.
    for idx in range(3):
        for i in range(25):
            assert client.get(f"/license-changes/cp-{idx}-{i}").status_code == 404


# -- permission timeline (interval segmentation) ----------------------------


def _timeline(client, version_id, use, start, end):
    return client.get(
        "/permissions/timeline",
        params={"version_id": version_id, "use": use, "start": start, "end": end},
    )


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_timeline_segments_follow_change_boundaries(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "v1", _iso(2026, 6, 1), ["research"])

    resp = _timeline(client, "v1", "review", _iso(2026, 3, 1), _iso(2026, 9, 1))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_id"] == "v1"
    assert body["use"] == "review"
    assert body["closure"] == ["v1"]

    segments = body["segments"]
    assert len(segments) == 2
    first, second = segments

    assert first["start"].startswith("2026-03-01T00:00:00")
    assert first["end"].startswith("2026-06-01T00:00:00")
    assert first["allowed"] is True
    assert [entry["change_id"] for entry in first["basis"]] == ["c1"]
    assert first["blockers"] == []
    assert first["allowed_uses_intersection"] == ["research", "review"]

    assert second["start"].startswith("2026-06-01T00:00:00")
    assert second["end"].startswith("2026-09-01T00:00:00")
    assert second["allowed"] is False
    assert [entry["change_id"] for entry in second["basis"]] == ["c2"]
    assert second["blockers"][0]["version_id"] == "v1"
    assert second["blockers"][0]["reason"] == "use_not_allowed"
    assert second["blockers"][0]["change_id"] == "c2"

    # Continuous, non-overlapping coverage of the whole interval.
    assert segments[0]["end"] == segments[1]["start"]


def test_timeline_half_open_boundaries(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _change(client, "c2", "v1", _iso(2026, 3, 1), ["review"])  # exactly at start
    _change(client, "c3", "v1", _iso(2026, 5, 1), ["research"])  # exactly at end

    body = _timeline(client, "v1", "review", _iso(2026, 3, 1), _iso(2026, 5, 1)).json()
    # The change at the start applies immediately; the one at the end is not
    # counted, so the whole interval is a single allowed segment.
    assert len(body["segments"]) == 1
    segment = body["segments"][0]
    assert segment["allowed"] is True
    assert [entry["change_id"] for entry in segment["basis"]] == ["c2"]
    assert segment["start"].startswith("2026-03-01T00:00:00")
    assert segment["end"].startswith("2026-05-01T00:00:00")

    # The point check at the end instant already sees c3 (half-open difference).
    assert _check(client, "v1", "review", _iso(2026, 5, 1)).json()["allowed"] is False


def test_timeline_no_valid_license_prefix(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 3, 1), ["research"])

    body = _timeline(client, "v1", "research", _iso(2026, 1, 1), _iso(2026, 5, 1)).json()
    assert len(body["segments"]) == 2
    first, second = body["segments"]
    assert first["allowed"] is False
    assert first["basis"] == []
    assert first["blockers"] == [
        {"version_id": "v1", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]
    assert first["allowed_uses_intersection"] == []
    assert second["allowed"] is True
    assert second["allowed_uses_intersection"] == ["research"]


def test_timeline_diamond_closure_counted_once(client):
    # Diamond: a <- b <- d1, a <- d1.
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 4, 1), ["research"])
    _register(client, "d1", ["b", "a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    body = _timeline(client, "d1", "research", _iso(2026, 2, 1), _iso(2026, 6, 1)).json()
    assert body["closure"] == ["d1", "b", "a"]  # shared ancestor exactly once
    assert len(body["segments"]) == 2
    first, second = body["segments"]
    # b has no license before 2026-04-01: the whole interval start is denied.
    assert first["allowed"] is False
    assert [(bl["version_id"], bl["reason"]) for bl in first["blockers"]] == [
        ("b", "no_valid_license")
    ]
    # From 04-01 on every version in the closure allows research.
    assert second["allowed"] is True
    assert second["allowed_uses_intersection"] == ["research"]


def test_timeline_reports_all_blockers_per_segment(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "b")
    _change(client, "cb", "b", _iso(2026, 4, 1), ["research", "review"])
    _register(client, "d1", ["a", "b"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    body = _timeline(client, "d1", "review", _iso(2026, 2, 1), _iso(2026, 6, 1)).json()
    assert len(body["segments"]) == 2
    first, second = body["segments"]
    # Before b's first change: a blocks (use_not_allowed) *and* b blocks
    # (no_valid_license) — every blocking version is reported with its reason.
    assert first["allowed"] is False
    reasons = {(bl["version_id"], bl["reason"]) for bl in first["blockers"]}
    assert reasons == {("a", "use_not_allowed"), ("b", "no_valid_license")}
    # After 04-01 only a still blocks.
    assert second["allowed"] is False
    assert [(bl["version_id"], bl["reason"]) for bl in second["blockers"]] == [
        ("a", "use_not_allowed")
    ]


def test_timeline_segments_agree_with_point_checks(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "ca2", "a", _iso(2026, 6, 1), ["research"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 2, 1), ["research", "review"])

    body = _timeline(client, "d1", "review", _iso(2026, 1, 15), _iso(2026, 8, 15)).json()
    segments = body["segments"]
    # Segments tile the whole interval without gaps or overlaps.
    assert segments[0]["start"] == body["start"]
    assert segments[-1]["end"] == body["end"]
    for left, right in zip(segments, segments[1:]):
        assert left["end"] == right["start"]

    # Every sampled instant matches the single-instant check contract.
    samples = [_iso(2026, 1, 20)] + [_iso(2026, month, 10) for month in range(2, 8)]
    for at in samples:
        instant = _parse_ts(at)
        segment = next(
            s
            for s in segments
            if _parse_ts(s["start"]) <= instant < _parse_ts(s["end"])
        )
        point = _check(client, "d1", "review", at).json()
        assert segment["allowed"] == point["allowed"]
        assert segment["blockers"] == point["blockers"]
        assert segment["basis"] == point["basis"]
        assert segment["allowed_uses_intersection"] == point["allowed_uses_intersection"]


def test_timeline_unknown_version_is_404(client):
    resp = _timeline(client, "ghost", "research", _iso(2026, 1, 1), _iso(2026, 2, 1))
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_timeline_naive_times_are_422(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    naive_start = client.get(
        "/permissions/timeline",
        params={
            "version_id": "v1",
            "use": "research",
            "start": "2026-01-01T00:00:00",
            "end": _iso(2026, 2, 1),
        },
    )
    assert naive_start.status_code == 422
    assert naive_start.json()["error"] == "invalid_time"

    naive_end = client.get(
        "/permissions/timeline",
        params={
            "version_id": "v1",
            "use": "research",
            "start": _iso(2026, 1, 1),
            "end": "2026-02-01T00:00:00",
        },
    )
    assert naive_end.status_code == 422
    assert naive_end.json()["error"] == "invalid_time"


def test_timeline_inverted_or_empty_interval_is_422(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    inverted = _timeline(client, "v1", "research", _iso(2026, 2, 1), _iso(2026, 1, 1))
    assert inverted.status_code == 422
    assert inverted.json()["error"] == "invalid_interval"

    empty = _timeline(client, "v1", "research", _iso(2026, 1, 1), _iso(2026, 1, 1))
    assert empty.status_code == 422
    assert empty.json()["error"] == "invalid_interval"

    # The same instant written in two offsets is still an empty interval.
    same_instant = client.get(
        "/permissions/timeline",
        params={
            "version_id": "v1",
            "use": "research",
            "start": "2026-01-01T08:00:00+08:00",
            "end": "2026-01-01T00:00:00Z",
        },
    )
    assert same_instant.status_code == 422
    assert same_instant.json()["error"] == "invalid_interval"


def test_timeline_empty_use_is_400(client):
    _register(client, "v1")
    resp = _timeline(client, "v1", "   ", _iso(2026, 1, 1), _iso(2026, 2, 1))
    assert resp.status_code == 400


# -- change revocation ------------------------------------------------------


def _revoke(client, change_id, revocation_id="rev-1", reason="登记有误"):
    return client.post(
        "/license-changes/revocations",
        json={
            "change_id": change_id,
            "revocation_id": revocation_id,
            "reason": reason,
        },
    )


def _revocation(client, revocation_id):
    return client.get(f"/license-changes/revocations/{revocation_id}")


# -- permission vouchers ----------------------------------------------------


def _issue_voucher(client, voucher_id, version_id, use, at):
    return client.post(
        "/permissions/vouchers",
        json={
            "voucher_id": voucher_id,
            "version_id": version_id,
            "use": use,
            "at": at,
        },
    )


def _get_voucher(client, voucher_id):
    return client.get(f"/permissions/vouchers/{voucher_id}")


def test_voucher_first_issue_records_full_decision_allow_and_deny(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research"])

    at = _iso(2026, 5, 1)
    allowed = _issue_voucher(client, "v-ok", "d1", "research", at)
    assert allowed.status_code == 201, allowed.text
    body = allowed.json()
    assert body["replayed"] is False
    v = body["voucher"]
    assert v["voucher_id"] == "v-ok"
    assert v["version_id"] == "d1"
    assert v["use"] == "research"
    assert v["at"] == at
    assert v["registered_at"].startswith("20")
    assert v["allowed"] is True
    assert v["closure"] == ["d1", "a"]
    assert {b["version_id"] for b in v["basis"]} == {"d1", "a"}
    assert v["blockers"] == []
    assert v["allowed_uses_intersection"] == ["research"]

    # Denials are persisted as vouchers too — allow and deny both leave a trace.
    denied = _issue_voucher(client, "v-no", "d1", "review", at)
    assert denied.status_code == 201
    dv = denied.json()["voucher"]
    assert dv["allowed"] is False
    assert dv["blockers"] == [
        {
            "version_id": "d1",
            "reason": "use_not_allowed",
            "change_id": "cd1",
            "effective_at": _iso(2026, 1, 1),
            "allowed_uses": ["research"],
        }
    ]
    assert dv["allowed_uses_intersection"] == ["research"]


def test_voucher_decision_matches_live_point_check(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 2, 1), ["research", "review"])

    at = _iso(2026, 3, 1)
    voucher = _issue_voucher(client, "v1", "d1", "research", at).json()["voucher"]
    live = _check(client, "d1", "research", at).json()
    for field in (
        "version_id",
        "use",
        "at",
        "allowed",
        "closure",
        "basis",
        "blockers",
        "allowed_uses_intersection",
    ):
        assert voucher[field] == live[field]
    # Voucher-only extra field.
    assert voucher["registered_at"]


def test_voucher_retry_same_inputs_returns_original_voucher(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])

    first = _issue_voucher(client, "v1", "a", "  review  ", "2026-05-01T08:00:00+08:00")
    assert first.status_code == 201
    first_body = first.json()
    assert first_body["voucher"]["use"] == "review"
    assert first_body["voucher"]["at"] == "2026-05-01T00:00:00Z"

    # Same id, trimmed use, same instant expressed in UTC -> replay, identical.
    retry = _issue_voucher(client, "v1", "a", "review", "2026-05-01T00:00:00Z")
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["voucher"] == first_body["voucher"]

    # GET always returns the same first-saved result.
    fetched = _get_voucher(client, "v1")
    assert fetched.status_code == 200
    assert fetched.json() == first_body["voucher"]


def test_voucher_same_id_different_inputs_conflict(client):
    _register(client, "a")
    _register(client, "b")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research"])

    assert _issue_voucher(client, "v1", "a", "research", _iso(2026, 5, 1)).status_code == 201

    different_use = _issue_voucher(client, "v1", "a", "review", _iso(2026, 5, 1))
    assert different_use.status_code == 409
    assert different_use.json()["error"] == "conflict"

    different_time = _issue_voucher(client, "v1", "a", "research", _iso(2026, 6, 1))
    assert different_time.status_code == 409

    different_version = _issue_voucher(client, "v1", "b", "research", _iso(2026, 5, 1))
    assert different_version.status_code == 409

    # The original voucher is untouched and still fetchable.
    original = _get_voucher(client, "v1").json()
    assert original["version_id"] == "a"
    assert original["use"] == "research"
    assert original["at"] == _iso(2026, 5, 1)


def test_voucher_history_immutable_after_change_and_revocation(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    at = _iso(2026, 5, 1)

    first = _issue_voucher(client, "v1", "a", "review", at)
    assert first.status_code == 201
    frozen = first.json()["voucher"]
    assert frozen["allowed"] is True
    assert frozen["basis"][0]["change_id"] == "ca"

    # Revoking the underlying change flips the live decision for every instant.
    assert _revoke(client, "ca", "r1", "登记有误").status_code == 201
    live = _check(client, "a", "review", at).json()
    assert live["allowed"] is False
    assert live["blockers"][0]["reason"] == "no_valid_license"

    # The voucher still reports the first-saved result verbatim.
    fetched = _get_voucher(client, "v1").json()
    assert fetched == frozen
    assert fetched["allowed"] is True
    assert fetched["basis"][0]["change_id"] == "ca"
    assert fetched["blockers"] == []

    # Retrying the same request afterwards replays the original as well.
    retry = _issue_voucher(client, "v1", "a", "review", at)
    assert retry.status_code == 200
    assert retry.json()["voucher"] == frozen


def test_voucher_history_immutable_after_later_change(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research", "review"])
    # Voucher for a July review request: currently allowed on ca1.
    at = _iso(2026, 7, 1)
    frozen = _issue_voucher(client, "v1", "a", "review", at).json()["voucher"]
    assert frozen["allowed"] is True

    # A narrowing change registered afterwards changes live July judgments...
    assert _change(client, "ca2", "a", _iso(2026, 6, 1), ["research"]).status_code == 201
    assert _check(client, "a", "review", at).json()["allowed"] is False
    # ...but never rewrites the stored credential.
    assert _get_voucher(client, "v1").json() == frozen


def test_voucher_unknown_id_is_404(client):
    resp = _get_voucher(client, "ghost")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_voucher_unknown_version_errors_without_leaving_row(client):
    resp = _issue_voucher(client, "v1", "ghost", "research", _iso(2026, 5, 1))
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"

    # No half-finished row: the same id can still be issued for a real version.
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    second = _issue_voucher(client, "v1", "a", "research", _iso(2026, 5, 1))
    assert second.status_code == 201
    assert second.json()["replayed"] is False


def test_voucher_invalid_inputs_are_422_without_row(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])

    blank_use = _issue_voucher(client, "v1", "a", "   ", _iso(2026, 5, 1))
    assert blank_use.status_code == 422
    naive_at = _issue_voucher(client, "v2", "a", "research", "2026-05-01T00:00:00")
    assert naive_at.status_code == 422

    # Nothing was persisted; both ids are still unknown.
    assert _get_voucher(client, "v1").status_code == 404
    assert _get_voucher(client, "v2").status_code == 404


def test_voucher_without_id_is_generated(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    resp = client.post(
        "/permissions/vouchers",
        json={"version_id": "a", "use": "research", "at": _iso(2026, 5, 1)},
    )
    assert resp.status_code == 201
    assert resp.json()["voucher"]["voucher_id"].startswith("p-")


def test_voucher_concurrent_same_id_issues_exactly_once(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    outcomes: list[tuple[str, int, bool]] = []
    lock = __import__("threading").Lock()

    def worker() -> None:
        try:
            _, replayed = service.issue_voucher(
                voucher_id="v1",
                version_id="a",
                use="research",
                at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            )
            with lock:
                outcomes.append(("ok", 200 if replayed else 201, replayed))
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append((type(exc).__name__, getattr(exc, "status_code", 0), False))

    import threading

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(name == "ok" for name, _, _ in outcomes), outcomes
    assert sorted(code for _, code, _ in outcomes).count(201) == 1
    assert all(replayed == (code == 200) for _, code, replayed in outcomes)
    assert _get_voucher(client, "v1").status_code == 200


def test_voucher_on_demo_seed(tmp_path):
    db_path = str(tmp_path / "seed-voucher.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        # review on demo-derived-1 is blocked by demo-root-b in March.
        resp = test_client.post(
            "/permissions/vouchers",
            json={
                "voucher_id": "demo-voucher-1",
                "version_id": "demo-derived-1",
                "use": "review",
                "at": "2026-03-01T00:00:00Z",
            },
        )
        assert resp.status_code == 201
        voucher = resp.json()["voucher"]
        assert voucher["allowed"] is False
        assert voucher["closure"] == ["demo-derived-1", "demo-root-a", "demo-root-b"]
        assert any(
            bl["version_id"] == "demo-root-b" and bl["reason"] == "use_not_allowed"
            for bl in voucher["blockers"]
        )

        retry = test_client.post(
            "/permissions/vouchers",
            json={
                "voucher_id": "demo-voucher-1",
                "version_id": "demo-derived-1",
                "use": " review ",
                "at": "2026-03-01T08:00:00+08:00",
            },
        )
        assert retry.status_code == 200
        assert retry.json()["replayed"] is True
        assert retry.json()["voucher"] == voucher



def test_revoke_returns_receipt_and_is_traceable(client):
    _register(client, "v1")
    first = _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    assert first.status_code == 201

    resp = _revoke(client, "c1", "r1", "用途登记有误，撤回")
    assert resp.status_code == 201, resp.text
    receipt = resp.json()
    assert receipt["replayed"] is False
    assert receipt["receipt"] == {
        "revocation_id": "r1",
        "change_id": "c1",
        "reason": "用途登记有误，撤回",
        "registered_at": receipt["receipt"]["registered_at"],
    }
    assert receipt["receipt"]["registered_at"]  # server-assigned instant
    assert receipt["receipt"]["registered_at"].startswith("20")

    # The receipt can be retrieved again by its own unique id.
    fetched = _revocation(client, "r1")
    assert fetched.status_code == 200
    assert fetched.json() == receipt["receipt"]


def test_revoke_same_id_same_content_retry_returns_original_receipt(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    first = _revoke(client, "c1", "r1", "登记有误")
    assert first.status_code == 201

    retry = _revoke(client, "c1", "r1", "登记有误")
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["receipt"] == first.json()["receipt"]


def test_revoke_reusing_id_with_different_content_conflicts(client):
    _register(client, "v1")
    _register(client, "v2")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _change(client, "c2", "v2", _iso(2026, 1, 1), ["research"])
    assert _revoke(client, "c1", "r1", "第一个原因").status_code == 201

    # Same revocation id, different reason.
    resp = _revoke(client, "c1", "r1", "另一个原因")
    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"

    # Same revocation id pointing at a different change.
    resp = _revoke(client, "c2", "r1", "第一个原因")
    assert resp.status_code == 409

    # The original receipt is untouched and still retrievable.
    fetched = _revocation(client, "r1").json()
    assert fetched["change_id"] == "c1"
    assert fetched["reason"] == "第一个原因"


def test_revoke_same_change_under_another_id_conflicts(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    assert _revoke(client, "c1", "r1").status_code == 201

    resp = _revoke(client, "c1", "r2")
    assert resp.status_code == 409
    assert resp.json()["details"]["existing_revocation_id"] == "r1"
    assert _revocation(client, "r2").status_code == 404


def test_revoke_unknown_change_is_404(client):
    resp = _revoke(client, "ghost", "r1")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_revoke_blank_reason_is_422(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    resp = client.post(
        "/license-changes/revocations",
        json={"change_id": "c1", "revocation_id": "r1", "reason": "   "},
    )
    assert resp.status_code == 422


def test_revoked_change_still_queryable_but_marked(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _revoke(client, "c1", "r1", "登记有误")

    resp = client.get("/license-changes/c1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["change_id"] == "c1"
    assert body["revocation"] is not None
    assert body["revocation"]["revocation_id"] == "r1"
    assert body["revocation"]["change_id"] == "c1"
    assert body["revocation"]["reason"] == "登记有误"

    # The version detail carries the same revocation marker.
    version = client.get("/versions/v1").json()
    assert version["changes"][0]["revocation"]["revocation_id"] == "r1"


def test_revoked_change_ignored_by_point_check_at_every_instant(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "a", _iso(2026, 6, 1), ["research"])
    _revoke(client, "c2", "r1", "收紧变更登记错误")

    # Before the revoked change existed the earlier change applied anyway.
    assert _check(client, "a", "review", _iso(2026, 5, 1)).json()["allowed"] is True
    # After it: it is ignored, so the earlier change remains the latest active.
    body = _check(client, "a", "review", _iso(2026, 7, 1)).json()
    assert body["allowed"] is True
    assert body["basis"][0]["change_id"] == "c1"
    assert body["blockers"] == []


def test_revoke_only_change_yields_no_valid_license(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research"])
    _revoke(client, "c1", "r1", "登记有误")

    body = _check(client, "a", "research", _iso(2026, 7, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]
    assert body["basis"] == []


def test_revoked_change_blocks_descendants_via_no_valid_license(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])
    _revoke(client, "ca", "r1", "登记有误")

    body = _check(client, "d1", "review", _iso(2026, 5, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]


def test_replay_of_revoked_change_does_not_restore_it(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _revoke(client, "c1", "r1", "登记有误")

    # Same id and content: replay returns 200, still marked revoked.
    replay = _change(client, "c1", "v1", _iso(2026, 1, 1), ["review", "research"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["change"]["revocation"] is not None
    assert replay.json()["change"]["revocation"]["revocation_id"] == "r1"

    # And point checks still ignore it.
    assert _check(client, "v1", "review", _iso(2026, 5, 1)).json()["allowed"] is False


def test_revoked_change_ignored_by_timeline(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "v1", _iso(2026, 6, 1), ["research"])
    _revoke(client, "c2", "r1", "登记有误")

    body = _timeline(client, "v1", "review", _iso(2026, 3, 1), _iso(2026, 9, 1)).json()
    # Only non-revoked changes segment the interval: c2 is gone, so the whole
    # interval is one allowed segment based on c1, same fields as before.
    assert len(body["segments"]) == 1
    segment = body["segments"][0]
    assert segment["allowed"] is True
    assert [entry["change_id"] for entry in segment["basis"]] == ["c1"]
    assert segment["blockers"] == []
    assert segment["allowed_uses_intersection"] == ["research", "review"]
    assert segment["start"].startswith("2026-03-01T00:00:00")
    assert segment["end"].startswith("2026-09-01T00:00:00")


def test_revoking_latest_change_exposes_earlier_one_in_timeline(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _change(client, "c2", "v1", _iso(2026, 6, 1), ["research", "review"])
    _revoke(client, "c2", "r1", "登记有误")

    body = _timeline(client, "v1", "review", _iso(2026, 3, 1), _iso(2026, 9, 1)).json()
    assert len(body["segments"]) == 1
    segment = body["segments"][0]
    # review is denied throughout on the sole remaining change c1.
    assert segment["allowed"] is False
    assert segment["blockers"][0]["change_id"] == "c1"


def test_preview_uses_revoked_snapshot_and_does_not_persist(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research"])
    # A voided proposal that would have opened up review.
    _change(client, "ca2", "a", _iso(2026, 4, 1), ["research", "review"])
    _revoke(client, "ca2", "r1", "登记有误")

    at = _iso(2026, 5, 1)
    # Current evaluation already ignores the revoked change.
    assert _check(client, "a", "review", at).json()["allowed"] is False

    body = _preview_flip(
        client, "cp1", "a", _iso(2026, 4, 15), ["research", "review"], "review", at
    ).json()
    flips = {entry["version_id"]: entry for entry in body["impacted_versions"]}
    assert set(flips) == {"a"}
    assert flips["a"]["before_blockers"][0]["change_id"] == "ca1"
    assert flips["a"]["after_allowed"] is True
    assert client.get("/license-changes/cp1").status_code == 404

    # A preview at the same instant as the revoked change still conflicts:
    # the existing same-version/same-time rule ignores revocation state.
    conflict = _preview_flip(
        client, "cp2", "a", _iso(2026, 4, 1), ["commercial"], "commercial", at
    )
    assert conflict.status_code == 409


def test_revoke_is_idempotent_under_concurrent_retries(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    statuses: list[tuple[str, int, bool]] = []
    lock = __import__("threading").Lock()

    def worker() -> None:
        try:
            _, replayed = service.revoke_change("c1", "r1", "登记有误")
            with lock:
                statuses.append(("ok", 200 if replayed else 201, replayed))
        except Exception as exc:  # noqa: BLE001
            with lock:
                statuses.append((type(exc).__name__, getattr(exc, "status_code", 0), False))

    import threading

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(name == "ok" for name, _, _ in statuses), statuses
    # Exactly one first-time creation; every other identical retry replays.
    assert sorted(code for _, code, _ in statuses).count(201) == 1
    assert all(replayed == (code == 200) for _, code, replayed in statuses)
    assert _revocation(client, "r1").status_code == 200


def test_demo_seed_revocation_round_trip(tmp_path):
    db_path = str(tmp_path / "seed-rev.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        # demo-root-a narrows to research on 2026-06-01 via demo-chg-a-2.
        resp = test_client.post(
            "/license-changes/revocations",
            json={
                "change_id": "demo-chg-a-2",
                "revocation_id": "rev-demo-a-2",
                "reason": "示例：收紧变更登记有误",
            },
        )
        assert resp.status_code == 201
        # After voiding, review stays allowed in July for the derived version
        # (root-a's earlier research+review change is the latest active one),
        # subject to root-b's still-existing block.
        body = test_client.get(
            "/permissions/check",
            params={
                "version_id": "demo-derived-1",
                "use": "review",
                "at": "2026-07-01T00:00:00Z",
            },
        ).json()
        assert body["allowed"] is False
        blockers = {(bl["version_id"], bl["reason"]) for bl in body["blockers"]}
        assert blockers == {("demo-root-b", "use_not_allowed")}
        assert all(entry["change_id"] != "demo-chg-a-2" for entry in body["basis"])
        # The receipt is traceable and the original change still retrievable.
        receipt = test_client.get("/license-changes/revocations/rev-demo-a-2")
        assert receipt.status_code == 200
        assert receipt.json()["change_id"] == "demo-chg-a-2"
        original = test_client.get("/license-changes/demo-chg-a-2")
        assert original.status_code == 200
        assert original.json()["revocation"]["revocation_id"] == "rev-demo-a-2"


def test_timeline_on_demo_seed(tmp_path):
    db_path = str(tmp_path / "seed3.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        body = test_client.get(
            "/permissions/timeline",
            params={
                "version_id": "demo-derived-1",
                "use": "review",
                "start": "2026-01-15T00:00:00Z",
                "end": "2026-07-15T00:00:00Z",
            },
        ).json()
        assert body["closure"] == ["demo-derived-1", "demo-root-a", "demo-root-b"]
        segments = body["segments"]
        assert len(segments) == 3
        # Before the derived version's first change: it has no_valid_license,
        # and demo-root-b's license already excludes review.
        assert segments[0]["allowed"] is False
        first_blockers = {(bl["version_id"], bl["reason"]) for bl in segments[0]["blockers"]}
        assert first_blockers == {
            ("demo-derived-1", "no_valid_license"),
            ("demo-root-b", "use_not_allowed"),
        }
        # Then only demo-root-b blocks review ...
        assert [bl["version_id"] for bl in segments[1]["blockers"]] == ["demo-root-b"]
        # ... and after root-a's June change both roots block it.
        assert {bl["version_id"] for bl in segments[2]["blockers"]} == {
            "demo-root-a",
            "demo-root-b",
        }
        assert all(bl["reason"] == "use_not_allowed" for bl in segments[2]["blockers"])

        # research stays allowed from the derived version's first change on;
        # root-a's June change still splits the timeline (basis changed).
        research = test_client.get(
            "/permissions/timeline",
            params={
                "version_id": "demo-derived-1",
                "use": "research",
                "start": "2026-01-15T00:00:00Z",
                "end": "2026-07-15T00:00:00Z",
            },
        ).json()
        assert [s["allowed"] for s in research["segments"]] == [False, True, True]
        assert research["segments"][1]["allowed_uses_intersection"] == ["research"]
        assert research["segments"][2]["allowed_uses_intersection"] == ["research"]
        basis_june = {
            entry["version_id"]: entry["change_id"]
            for entry in research["segments"][2]["basis"]
        }
        assert basis_june["demo-root-a"] == "demo-chg-a-2"

# -- signed voucher proof packages -----------------------------------------

import copy
import json
import threading

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from app.signing import (
    SigningKeyUnavailableError,
    b64url_decode,
    b64url_encode,
    canonical_json_bytes,
    verify_proof_package,
)


def _proof_setup(client, voucher_id="pv-1", use="research", at="2026-05-01T00:00:00Z"):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    resp = _issue_voucher(client, voucher_id, "a", use, at)
    assert resp.status_code == 201, resp.text
    return resp.json()["voucher"]


def _export_proof(client, voucher_id):
    return client.post(f"/permissions/vouchers/{voucher_id}/proof")


def _verify_independently(pkg, public_pem):
    sig = pkg["signature"]
    pub = load_pem_public_key(public_pem.encode("ascii"))
    signing_input = f"{sig['protected']}.{sig['payload']}".encode("ascii")
    pub.verify(b64url_decode(sig["signature"]), signing_input)


def _retamper(pkg, mutate):
    tampered = copy.deepcopy(pkg)
    mutate(tampered["payload"])
    tampered["signature"]["payload"] = b64url_encode(
        canonical_json_bytes(tampered["payload"])
    )
    return tampered


def test_proof_first_export_returns_201_and_is_verifiable(client):
    voucher = _proof_setup(client)
    resp = _export_proof(client, "pv-1")
    assert resp.status_code == 201
    body = resp.json()
    assert body["replayed"] is False
    pkg = body["proof"]

    assert pkg["voucher_id"] == "pv-1"
    assert pkg["format_version"] >= 1
    payload = pkg["payload"]
    # The signed payload is the complete first-decision snapshot ...
    for field in (
        "format_version",
        "voucher_id",
        "version_id",
        "use",
        "at",
        "registered_at",
        "allowed",
        "closure",
        "basis",
        "blockers",
        "allowed_uses_intersection",
    ):
        assert field in payload
    assert payload["voucher_id"] == "pv-1"
    assert payload["version_id"] == "a"
    assert payload["use"] == "research"
    assert payload["at"] == "2026-05-01T00:00:00.000000Z"
    assert payload["registered_at"] == voucher["registered_at"].replace(
        "+00:00", "Z"
    )
    assert payload["allowed"] is True
    assert payload["closure"] == ["a"]
    assert payload["basis"][0]["change_id"] == "ca"
    assert payload["allowed_uses_intersection"] == ["research", "review"]

    sig = pkg["signature"]
    assert sig["algorithm"] == "Ed25519"
    assert sig["jws_alg"] == "EdDSA"
    assert sig["key_id"]
    # Protected header is canonical {"alg": "EdDSA", "kid": key_id}.
    assert json.loads(b64url_decode(sig["protected"])) == {
        "alg": "EdDSA",
        "kid": sig["key_id"],
    }
    # The payload segment is the canonical encoding of the carried payload.
    assert b64url_decode(sig["payload"]) == canonical_json_bytes(payload)
    assert sig["compact_serialization"] == ".".join(
        [sig["protected"], sig["payload"], sig["signature"]]
    )

    # Public key fetchable by id (PEM) and carried inside the package; both
    # verify the signature.
    pub_resp = client.get(f"/signing/keys/{sig['key_id']}/public")
    assert pub_resp.status_code == 200
    pub_json = pub_resp.json()
    assert pub_json["active"] is True
    assert pub_json["public_key_jwk"]["kty"] == "OKP"
    assert pub_json["public_key_jwk"]["crv"] == "Ed25519"
    _verify_independently(pkg, pub_json["public_key_pem"])
    _verify_independently(pkg, sig["public_key_pem"])

    # The bundled self-check helper agrees.
    ok, message = verify_proof_package(pkg)
    assert ok, message

    # Encoding rules are explicit and enumerate every covered field.
    rules = pkg["verification"]["encoding"]
    assert rules["format"] == "jws-eddsa-ed25519-v1"
    assert set(rules["covered_fields"]) == {
        "allowed",
        "allowed_uses_intersection",
        "at",
        "basis",
        "blockers",
        "closure",
        "format_version",
        "registered_at",
        "use",
        "version_id",
        "voucher_id",
    }
    # The public key URL is the concrete path for this package's key id.
    assert (
        pkg["verification"]["public_key_url"]
        == f"/signing/keys/{sig['key_id']}/public"
    )
    assert pkg["verification"]["public_keys_url"] == "/signing/keys"


def test_proof_denied_voucher_carries_blockers_and_verifies(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _issue_voucher(client, "pv-deny", "a", "review", "2026-05-01T00:00:00Z")
    pkg = _export_proof(client, "pv-deny").json()["proof"]
    payload = pkg["payload"]
    assert payload["allowed"] is False
    assert payload["blockers"] == [
        {
            "version_id": "a",
            "reason": "use_not_allowed",
            "change_id": "ca",
            "effective_at": "2026-01-01T00:00:00.000000Z",
            "allowed_uses": ["research"],
        }
    ]
    ok, message = verify_proof_package(pkg)
    assert ok, message


def test_proof_repeat_export_returns_same_fixed_package(client):
    _proof_setup(client)
    first = _export_proof(client, "pv-1")
    assert first.status_code == 201
    first_pkg = first.json()["proof"]

    second = _export_proof(client, "pv-1")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["proof"] == first_pkg

    # GET returns the same frozen package without signing anything.
    fetched = client.get("/permissions/vouchers/pv-1/proof")
    assert fetched.status_code == 200
    assert fetched.json() == first_pkg


def test_proof_concurrent_exports_freeze_exactly_one(client):
    _proof_setup(client)
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    outcomes: list[tuple[bool, str]] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            package, replayed = service.export_voucher_proof("pv-1")
            with lock:
                outcomes.append((replayed, package["signature"]["signature"]))
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(("error", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(replayed != "error" for replayed, _ in outcomes), outcomes
    assert sum(1 for replayed, _ in outcomes if replayed is False) == 1
    signatures = {sig for _, sig in outcomes}
    assert signatures == {outcomes[0][1]}  # one signature => same package
    # One row in storage.
    import sqlite3

    conn = sqlite3.connect(service.db.path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM voucher_proofs WHERE voucher_id = 'pv-1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_proof_unknown_voucher_is_404_and_creates_nothing(client):
    resp = _export_proof(client, "ghost")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert client.get("/permissions/vouchers/ghost/proof").status_code == 404
    import sqlite3

    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    conn = sqlite3.connect(service.db.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM voucher_proofs").fetchone()[0] == 0
    finally:
        conn.close()


def test_proof_get_before_export_is_404(client):
    _proof_setup(client)
    resp = client.get("/permissions/vouchers/pv-1/proof")
    assert resp.status_code == 404
    assert resp.json()["details"].get("reason") == "proof_not_exported"


def test_proof_historical_voucher_exportable_after_state_changes(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _issue_voucher(client, "pv-old", "a", "review", "2026-05-01T00:00:00Z")
    # Change the world: revoke the basis change and issue a later voucher.
    rev = client.post(
        "/license-changes/revocations",
        json={
            "change_id": "ca",
            "revocation_id": "rev-ca",
            "reason": "x",
        },
    )
    assert rev.status_code == 201
    _issue_voucher(client, "pv-new", "a", "research", "2026-09-01T00:00:00Z")

    # The pre-existing (historical) voucher still exports its first snapshot.
    pkg = _export_proof(client, "pv-old").json()["proof"]
    assert pkg["payload"]["allowed"] is True
    assert pkg["payload"]["basis"][0]["change_id"] == "ca"
    ok, message = verify_proof_package(pkg)
    assert ok, message
    # And the newer historical voucher exports too.
    pkg2 = _export_proof(client, "pv-new").json()["proof"]
    assert pkg2["payload"]["allowed"] is False
    assert pkg2["payload"]["blockers"][0]["reason"] == "no_valid_license"
    ok2, message2 = verify_proof_package(pkg2)
    assert ok2, message2
    # Different signatures (different snapshots, same key is fine).
    assert pkg["signature"]["signature"] != pkg2["signature"]["signature"]


def test_proof_tampering_any_covered_field_fails_verification(client):
    _proof_setup(client)
    pkg = _export_proof(client, "pv-1").json()["proof"]
    pub_pem = pkg["signature"]["public_key_pem"]

    def rejected(tampered):
        sig = tampered["signature"]
        try:
            _verify_independently(tampered, pub_pem)
        except InvalidSignature:
            return True
        return False

    scalar_mutations = {
        "version_id": lambda p: p.__setitem__("version_id", "a-evil"),
        "use": lambda p: p.__setitem__("use", "review"),
        "at": lambda p: p.__setitem__("at", "2026-05-02T00:00:00.000000Z"),
        "registered_at": lambda p: p.__setitem__(
            "registered_at", "2026-01-01T00:00:00.000000Z"
        ),
        "allowed": lambda p: p.__setitem__("allowed", not p["allowed"]),
        "closure_add": lambda p: p["closure"].append("ghost-ancestor"),
        "intersection": lambda p: p.__setitem__(
            "allowed_uses_intersection", ["research", "commercial"]
        ),
        "basis_use": lambda p: p["basis"][0]["allowed_uses"].append("commercial"),
    }
    for name, mutate in scalar_mutations.items():
        assert rejected(_retamper(pkg, mutate)), name

    # Reordering a multi-ancestor closure also invalidates the signature.
    _register(client, "p1")
    _register(client, "p2")
    _register(client, "multi", ["p1", "p2"])
    _change(client, "cp1", "p1", _iso(2026, 1, 1), ["research"])
    _change(client, "cp2", "p2", _iso(2026, 1, 1), ["research"])
    _change(client, "cm", "multi", _iso(2026, 1, 1), ["research"])
    _issue_voucher(client, "pv-multi", "multi", "research", "2026-05-01T00:00:00Z")
    multi_pkg = _export_proof(client, "pv-multi").json()["proof"]
    assert len(multi_pkg["payload"]["closure"]) >= 2
    assert rejected(_retamper(multi_pkg, lambda p: p["closure"].reverse()))

    # Blockers on a denied voucher.
    _register(client, "b")
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research"])
    _issue_voucher(client, "pv-2", "b", "commercial", "2026-05-01T00:00:00Z")
    denied = _export_proof(client, "pv-2").json()["proof"]
    assert denied["payload"]["blockers"]
    assert rejected(
        _retamper(denied, lambda p: p["blockers"][0].__setitem__(
            "reason", "no_valid_license"))
    )

    # Flipping signature bytes directly fails too.
    flipped = copy.deepcopy(pkg)
    raw = bytearray(b64url_decode(flipped["signature"]["signature"]))
    raw[0] ^= 0xFF
    flipped["signature"]["signature"] = b64url_encode(bytes(raw))
    assert rejected(flipped)

    # The self-check helper reports the tamper reason.
    tampered = _retamper(pkg, lambda p: p.__setitem__("use", "review"))
    ok, message = verify_proof_package(tampered)
    assert ok is False
    assert "验签失败" in message


def test_proof_rotation_keeps_old_package_verifiable_and_not_resigned(client):
    _proof_setup(client)
    old_pkg = _export_proof(client, "pv-1").json()["proof"]
    old_kid = old_pkg["signature"]["key_id"]
    old_pem = client.get(f"/signing/keys/{old_kid}/public").json()["public_key_pem"]

    rotated = client.post("/signing/keys/rotate")
    assert rotated.status_code == 201
    rotation = rotated.json()
    new_kid = rotation["key"]["key_id"]
    assert rotation["previous_key_id"] == old_kid
    assert new_kid != old_kid
    assert rotation["key"]["active"] is True

    # The old package is byte-identical, still signed by the old key, and still
    # verifies with the old public key after rotation.
    again = _export_proof(client, "pv-1")
    assert again.status_code == 200
    assert again.json()["proof"] == old_pkg
    assert again.json()["proof"]["signature"]["key_id"] == old_kid
    _verify_independently(old_pkg, old_pem)

    # The old public key is still fetchable (now inactive); the list has both.
    old_pub = client.get(f"/signing/keys/{old_kid}/public")
    assert old_pub.status_code == 200
    assert old_pub.json()["active"] is False
    listed = client.get("/signing/keys").json()
    ids = {k["key_id"]: k["active"] for k in listed["keys"]}
    assert ids == {old_kid: False, new_kid: True}
    assert listed["active_key_id"] == new_kid

    # A brand-new export is signed with the new key and verifies with it.
    _issue_voucher(client, "pv-rot", "a", "research", "2026-08-01T00:00:00Z")
    new_pkg = _export_proof(client, "pv-rot").json()["proof"]
    assert new_pkg["signature"]["key_id"] == new_kid
    new_pem = client.get(f"/signing/keys/{new_kid}/public").json()["public_key_pem"]
    _verify_independently(new_pkg, new_pem)
    # Cross verification fails: old package under new key, new under old key.
    for pkg, pem in ((old_pkg, new_pem), (new_pkg, old_pem)):
        try:
            _verify_independently(pkg, pem)
            raise AssertionError("cross-key verification unexpectedly passed")
        except InvalidSignature:
            pass


def test_proof_signing_key_unavailable_returns_503(client):
    _proof_setup(client)
    service = client.app.state.app_state.service  # type: ignore[attr-defined]

    def boom():
        raise SigningKeyUnavailableError("模拟：签名密钥目录不可写")

    service.key_store.ensure_active_key = boom  # type: ignore[method-assign]
    resp = _export_proof(client, "pv-1")
    assert resp.status_code == 503
    assert resp.json()["error"] == "signing_key_unavailable"
    # Nothing was frozen; once the key is restored, export works normally.
    from app.signing import KeyStore

    service.key_store = KeyStore(service.key_store.keys_dir)
    ok_resp = _export_proof(client, "pv-1")
    assert ok_resp.status_code == 201


def test_proof_persists_across_restart(tmp_path):
    db_path = str(tmp_path / "restart.db")
    keys_dir = str(tmp_path / "keys")
    settings = Settings(
        db_path=db_path,
        signing_keys_dir=keys_dir,
        init_db=True,
        seed_demo=False,
        echo=False,
    )
    with TestClient(create_app(settings)) as first:
        _register(first, "a")
        _change(first, "ca", "a", _iso(2026, 1, 1), ["research"])
        _issue_voucher(first, "pv-1", "a", "research", "2026-05-01T00:00:00Z")
        pkg = _export_proof(first, "pv-1").json()["proof"]
        kid = pkg["signature"]["key_id"]

    # Restart with the same persistent DB and key directory: no re-signing,
    # same package; rotating afterwards preserves the old key on disk.
    with TestClient(create_app(settings)) as second:
        fetched = second.get("/permissions/vouchers/pv-1/proof")
        assert fetched.status_code == 200
        assert fetched.json() == pkg
        repeat = _export_proof(second, "pv-1")
        assert repeat.status_code == 200
        assert repeat.json()["proof"] == pkg
        assert second.get(f"/signing/keys/{kid}/public").status_code == 200


def test_proof_on_demo_seed(tmp_path):
    db_path = str(tmp_path / "seed-proof.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        issue = test_client.post(
            "/permissions/vouchers",
            json={
                "voucher_id": "demo-proof-1",
                "version_id": "demo-derived-1",
                "use": "review",
                "at": "2026-03-01T00:00:00Z",
            },
        )
        assert issue.status_code == 201
        pkg = test_client.post(
            "/permissions/vouchers/demo-proof-1/proof"
        ).json()["proof"]
        assert pkg["payload"]["allowed"] is False
        assert pkg["payload"]["closure"] == [
            "demo-derived-1",
            "demo-root-a",
            "demo-root-b",
        ]
        assert {b["version_id"] for b in pkg["payload"]["blockers"]} == {
            "demo-root-b"
        }
        ok, message = verify_proof_package(pkg)
        assert ok, message


# -- permission result subscriptions --------------------------------------


def _subscribe(client, subscription_id, version_id, use, at):
    return client.post(
        "/permissions/subscriptions",
        json={
            "subscription_id": subscription_id,
            "version_id": version_id,
            "use": use,
            "at": at,
        },
    )


def _get_subscription(client, subscription_id):
    return client.get(f"/permissions/subscriptions/{subscription_id}")


def _events(client, subscription_id, after_seq=0, limit=100):
    return client.get(
        f"/permissions/subscriptions/{subscription_id}/events",
        params={"after_seq": after_seq, "limit": limit},
    )


def _event_seqs(client, subscription_id):
    body = _events(client, subscription_id).json()
    return [e["seq"] for e in body["events"]]


def test_subscription_baseline_freezes_current_decision(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    # review is denied at creation: baseline freezes deny with the blocker.
    resp = _subscribe(client, "sub-1", "v1", "research", _iso(2026, 5, 1))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    sub = body["subscription"]
    assert sub["subscription_id"] == "sub-1"
    assert sub["version_id"] == "v1"
    assert sub["use"] == "research"
    assert sub["at"] == "2026-05-01T00:00:00Z"
    assert sub["baseline_allowed"] is True
    assert sub["baseline_blockers"] == []

    denied = _subscribe(client, "sub-2", "v1", " review ", _iso(2026, 5, 1)).json()[
        "subscription"
    ]
    assert denied["use"] == "review"
    assert denied["baseline_allowed"] is False
    assert denied["baseline_blockers"] == [
        {
            "version_id": "v1",
            "reason": "use_not_allowed",
            "change_id": "c1",
            "effective_at": "2026-01-01T00:00:00Z",
            "allowed_uses": ["research"],
        }
    ]

    # GET returns the same immutable baseline.
    assert _get_subscription(client, "sub-2").json() == denied


def test_subscription_retry_same_inputs_returns_original(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    first = _subscribe(client, "sub-1", "v1", " research ", _iso(2026, 3, 1, 8))
    assert first.status_code == 201
    first_body = first.json()

    # Same UTC instant expressed in Z, trimmed use -> 200 replay, byte-equal.
    retry = _subscribe(
        client, "sub-1", "v1", "research", "2026-03-01T08:00:00Z"
    )
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["subscription"] == first_body["subscription"]

    # A flip happens in between: a retry must still return the ORIGINAL baseline.
    later = _change(client, "c2", "v1", _iso(2026, 2, 1), ["research", "review"])
    assert later.status_code == 201
    again = _subscribe(client, "sub-1", "v1", "research", _iso(2026, 3, 1, 8))
    assert again.status_code == 200
    assert again.json()["subscription"]["created_at"] == first_body["subscription"][
        "created_at"
    ]


def test_subscription_same_id_different_inputs_conflict(client):
    _register(client, "v1")
    _register(client, "v2")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "sub-1", "v1", "research", _iso(2026, 5, 1))

    other_use = _subscribe(client, "sub-1", "v1", "review", _iso(2026, 5, 1))
    assert other_use.status_code == 409
    other_version = _subscribe(client, "sub-1", "v2", "research", _iso(2026, 5, 1))
    assert other_version.status_code == 409
    other_at = _subscribe(client, "sub-1", "v1", "research", _iso(2026, 6, 1))
    assert other_at.status_code == 409

    # Original subscription stays untouched.
    got = _get_subscription(client, "sub-1").json()
    assert got["version_id"] == "v1" and got["use"] == "research"


def test_subscription_invalid_inputs_error_without_row(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    unknown = _subscribe(client, "sub-x", "no-such-version", "research", _iso(2026, 5, 1))
    assert unknown.status_code == 404
    assert _get_subscription(client, "sub-x").status_code == 404
    assert _events(client, "sub-x").status_code == 404

    naive = _subscribe(
        client, "sub-y", "v1", "research", "2026-05-01T00:00:00"
    )
    assert naive.status_code == 422
    assert _get_subscription(client, "sub-y").status_code == 404

    blank_use = _subscribe(client, "sub-z", "v1", "   ", _iso(2026, 5, 1))
    assert blank_use.status_code == 422
    assert _get_subscription(client, "sub-z").status_code == 404


def test_subscription_without_id_is_generated(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    resp = client.post(
        "/permissions/subscriptions",
        json={"version_id": "v1", "use": "research", "at": _iso(2026, 5, 1)},
    )
    assert resp.status_code == 201
    assert resp.json()["subscription"]["subscription_id"].startswith("s-")


def test_flip_events_append_on_change_and_revoke_chain(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    sub = _subscribe(client, "sub-1", "v1", "review", _iso(2026, 9, 1)).json()[
        "subscription"
    ]
    assert sub["baseline_allowed"] is False

    # c2 opens review: deny -> allow.
    assert _change(client, "c2", "v1", _iso(2026, 3, 1), ["research", "review"]).status_code == 201
    # c3 closes it again: allow -> deny.
    assert _change(client, "c3", "v1", _iso(2026, 6, 1), ["research"]).status_code == 201
    # Revoking c3 re-exposes c2: deny -> allow.
    assert _revoke(client, "c3", "rev-c3", "撤回收紧").status_code == 201
    # Revoking c2 re-exposes c1 (research only): allow -> deny.
    assert _revoke(client, "c2", "rev-c2", "撤回放开").status_code == 201

    page = _events(client, "sub-1").json()
    assert page["next_cursor"] == 4 and page["has_more"] is False
    events = page["events"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]

    assert events[0]["trigger_kind"] == "license_change"
    assert events[0]["trigger_id"] == "c2"
    assert events[0]["change_id"] == "c2"
    assert events[0]["effective_at"] == "2026-03-01T00:00:00Z"
    assert events[0]["before_allowed"] is False
    assert events[0]["after_allowed"] is True
    assert events[0]["before_blockers"][0]["reason"] == "use_not_allowed"
    assert events[0]["after_blockers"] == []

    assert events[1]["trigger_id"] == "c3"
    assert events[1]["before_allowed"] is True
    assert events[1]["after_allowed"] is False
    assert events[1]["after_blockers"][0]["change_id"] == "c3"

    assert events[2]["trigger_kind"] == "revocation"
    assert events[2]["trigger_id"] == "rev-c3"
    assert events[2]["change_id"] == "c3"
    assert events[2]["effective_at"] == "2026-06-01T00:00:00Z"
    assert events[2]["before_allowed"] is False
    assert events[2]["after_allowed"] is True

    assert events[3]["trigger_kind"] == "revocation"
    assert events[3]["trigger_id"] == "rev-c2"
    assert events[3]["change_id"] == "c2"
    assert events[3]["before_allowed"] is True
    assert events[3]["after_allowed"] is False
    # c1 is now the latest effective change again.
    assert events[3]["after_blockers"][0]["change_id"] == "c1"

    # Retrying a committed change never produces a duplicate event.
    replay = _change(
        client, "c2", "v1", _iso(2026, 3, 1), ["review", "research"]
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert _event_seqs(client, "sub-1") == [1, 2, 3, 4]
    # Replaying a revocation likewise appends nothing.
    rev_replay = _revoke(client, "c3", "rev-c3", "撤回收紧")
    assert rev_replay.status_code == 200
    assert _event_seqs(client, "sub-1") == [1, 2, 3, 4]


def test_non_flipping_change_appends_no_event(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "sub-1", "v1", "research", _iso(2026, 9, 1))

    # Adding review does not flip research (still allowed): no event.
    assert _change(client, "c2", "v1", _iso(2026, 3, 1), ["research", "review"]).status_code == 201
    assert _events(client, "sub-1").json()["events"] == []


def test_future_effective_change_does_not_flip_past_subscription(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    # Fixed decision instant is February; nothing effective then allows review.
    _subscribe(client, "sub-1", "v1", "review", _iso(2026, 2, 1))

    assert _change(client, "c2", "v1", _iso(2026, 3, 1), ["research", "review"]).status_code == 201
    assert _events(client, "sub-1").json()["events"] == []

    # A subscription created later at a May instant starts from the new state,
    # with no backfilled history.
    later = _subscribe(client, "sub-2", "v1", "review", _iso(2026, 5, 1)).json()[
        "subscription"
    ]
    assert later["baseline_allowed"] is True
    assert _events(client, "sub-2").json()["events"] == []


def test_change_flips_descendant_subscriptions_only(client):
    _register(client, "root")
    _register(client, "other-root")
    _register(client, "child", parents=["root"])
    # Diamond: grandchild reaches root both directly and via child (counted once).
    _register(client, "grandchild", parents=["child", "root"])
    # Grandchild's own January change already includes review: its review status
    # is then controlled solely by the root/child intersection.
    _change(client, "c-root", "root", _iso(2026, 1, 1), ["research"])
    _change(client, "c-other-root", "other-root", _iso(2026, 1, 1), ["research"])
    _change(client, "c-child", "child", _iso(2026, 1, 1), ["research"])
    _change(
        client,
        "c-grandchild",
        "grandchild",
        _iso(2026, 1, 1),
        ["research", "review"],
    )

    for sid, vid in [
        ("s-root", "root"),
        ("s-child", "child"),
        ("s-grand", "grandchild"),
        ("s-other", "other-root"),
    ]:
        _subscribe(client, sid, vid, "review", _iso(2026, 9, 1))

    # Relaxing root flips root; child/grandchild still deny on child's change.
    assert _change(
        client, "c-root-2", "root", _iso(2026, 3, 1), ["research", "review"]
    ).status_code == 201
    assert _event_seqs(client, "s-root") == [1]
    assert _events(client, "s-child").json()["events"] == []
    assert _events(client, "s-grand").json()["events"] == []
    assert _events(client, "s-other").json()["events"] == []

    # Relaxing the child then flips child and grandchild (root counted once).
    assert _change(
        client, "c-child-2", "child", _iso(2026, 4, 1), ["research", "review"]
    ).status_code == 201
    assert _event_seqs(client, "s-child") == [1]
    assert _event_seqs(client, "s-grand") == [1]
    assert _events(client, "s-other").json()["events"] == []

    # Revoking child's change flips child and grandchild back exactly once.
    assert _revoke(client, "c-child-2", "rev-child", "x").status_code == 201
    assert [e["seq"] for e in _events(client, "s-child").json()["events"]] == [1, 2]
    assert [e["seq"] for e in _events(client, "s-grand").json()["events"]] == [1, 2]


def test_subscription_event_paging_is_stable(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "sub-1", "v1", "review", _iso(2026, 12, 1))
    _change(client, "c2", "v1", _iso(2026, 2, 1), ["research", "review"])
    _change(client, "c3", "v1", _iso(2026, 3, 1), ["research"])
    _revoke(client, "c3", "rev-c3", "x")
    assert _event_seqs(client, "sub-1") == [1, 2, 3]

    first = _events(client, "sub-1", after_seq=0, limit=1).json()
    assert [e["seq"] for e in first["events"]] == [1]
    assert first["next_cursor"] == 1 and first["has_more"] is True

    second = _events(client, "sub-1", after_seq=1, limit=1).json()
    assert [e["seq"] for e in second["events"]] == [2]
    assert second["next_cursor"] == 2 and second["has_more"] is True

    third = _events(client, "sub-1", after_seq=2, limit=1).json()
    assert [e["seq"] for e in third["events"]] == [3]
    assert third["next_cursor"] == 3 and third["has_more"] is False

    tail = _events(client, "sub-1", after_seq=3, limit=10).json()
    assert tail["events"] == []
    assert tail["next_cursor"] == 3 and tail["has_more"] is False

    # Larger page walks the same stable order in one go.
    full = _events(client, "sub-1", after_seq=0, limit=10).json()
    assert [e["seq"] for e in full["events"]] == [1, 2, 3]
    assert full["has_more"] is False

    # Invalid cursor / page size are rejected.
    assert client.get(
        "/permissions/subscriptions/sub-1/events", params={"after_seq": -1}
    ).status_code == 422
    assert client.get(
        "/permissions/subscriptions/sub-1/events", params={"limit": 0}
    ).status_code == 422


def test_distinct_subscriptions_same_watched_decision_both_flip(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "sub-a", "v1", "review", _iso(2026, 9, 1))
    _subscribe(client, "sub-b", "v1", "review", _iso(2026, 9, 1))
    assert _change(
        client, "c2", "v1", _iso(2026, 3, 1), ["research", "review"]
    ).status_code == 201
    assert _event_seqs(client, "sub-a") == [1]
    assert _event_seqs(client, "sub-b") == [1]


def test_concurrent_subscriptions_versus_change_flip_once_each(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    import threading

    outcomes: list[str] = []
    lock = threading.Lock()

    def subscribe(idx: int) -> None:
        try:
            sub, _ = service.create_subscription(
                subscription_id=f"sub-{idx}",
                version_id="v1",
                use="review",
                at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            with lock:
                outcomes.append(f"created-{sub['baseline_allowed']}")
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(f"error-{type(exc).__name__}")

    def flip_change() -> None:
        service.submit_change(
            change_id="c2",
            version_id="v1",
            effective_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            allowed_uses=["research", "review"],
        )

    threads = [threading.Thread(target=subscribe, args=(i,)) for i in range(8)]
    threads.append(threading.Thread(target=flip_change))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(not o.startswith("error") for o in outcomes), outcomes
    # Each subscription is internally consistent: either it saw the final state
    # in its baseline (no events) or it froze the old deny baseline and got
    # exactly one flip event — never duplicated, never missed.
    state_from_event = {True: 0, False: 0}
    for idx in range(8):
        sid = f"sub-{idx}"
        sub = _get_subscription(client, sid).json()
        events = _events(client, sid).json()["events"]
        if sub["baseline_allowed"] is True:
            assert events == []
            state_from_event[True] += 1
        else:
            assert [e["seq"] for e in events] == [1]
            assert events[0]["after_allowed"] is True
            state_from_event[False] += 1
    assert state_from_event[True] + state_from_event[False] == 8


def test_concurrent_same_subscription_id_creates_once(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    import threading

    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            _, replayed = service.create_subscription(
                subscription_id="sub-1",
                version_id="v1",
                use="research",
                at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            )
            with lock:
                codes.append(200 if replayed else 201)
        except Exception as exc:  # noqa: BLE001
            with lock:
                codes.append(getattr(exc, "status_code", 0))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(201) == 1
    assert sorted(codes).count(200) == 7


def test_concurrent_revocations_flip_exactly_once(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "sub-1", "v1", "review", _iso(2026, 9, 1))
    assert _change(
        client, "c2", "v1", _iso(2026, 3, 1), ["research", "review"]
    ).status_code == 201
    assert _event_seqs(client, "sub-1") == [1]

    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    import threading

    results: list[tuple[str, int]] = []
    lock = threading.Lock()

    def revoke(idx: int) -> None:
        try:
            _, replayed = service.revoke_change(
                change_id="c2",
                revocation_id=f"rev-{idx}",
                reason="并发作废",
            )
            with lock:
                results.append(("ok", 200 if replayed else 201))
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(("conflict", getattr(exc, "status_code", 0)))

    threads = [threading.Thread(target=revoke, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for kind, code in results if kind == "ok" and code == 201) == 1
    assert all(
        code == 409 for kind, code in results if kind == "conflict"
    ), results
    # The one successful revocation appended exactly one flip (seq 2).
    assert _event_seqs(client, "sub-1") == [1, 2]
    events = _events(client, "sub-1").json()["events"]
    assert events[1]["trigger_kind"] == "revocation"
    assert events[1]["after_allowed"] is False


def test_subscription_on_demo_seed(tmp_path):
    db_path = str(tmp_path / "seed-sub.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        # review is blocked by root-b on demo-derived-1 in March.
        resp = test_client.post(
            "/permissions/subscriptions",
            json={
                "subscription_id": "demo-sub-1",
                "version_id": "demo-derived-1",
                "use": "review",
                "at": "2026-03-01T08:00:00+08:00",
            },
        )
        assert resp.status_code == 201
        sub = resp.json()["subscription"]
        assert sub["at"] == "2026-03-01T00:00:00Z"
        assert sub["baseline_allowed"] is False
        assert any(
            bl["version_id"] == "demo-root-b" and bl["reason"] == "use_not_allowed"
            for bl in sub["baseline_blockers"]
        )

        retry = test_client.post(
            "/permissions/subscriptions",
            json={
                "subscription_id": "demo-sub-1",
                "version_id": "demo-derived-1",
                "use": " review ",
                "at": "2026-03-01T00:00:00Z",
            },
        )
        assert retry.status_code == 200
        assert retry.json()["subscription"] == sub
# -- license change batch imports -----------------------------------------


def _batch(client, batch_id, changes):
    return client.post(
        "/license-changes/batches",
        json={"batch_id": batch_id, "changes": changes},
    )


def _batch_change(change_id, version_id, at, uses):
    return {
        "change_id": change_id,
        "version_id": version_id,
        "effective_at": at,
        "allowed_uses": uses,
    }


def test_batch_applies_changes_in_order_and_reports_flips(client):
    _register(client, "v1")
    # Baseline at September: c1 allows research+review.
    assert _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"]).status_code == 201
    assert _subscribe(client, "s1", "v1", "review", _iso(2026, 9, 1)).status_code == 201

    # Batch: narrow in March (flip deny), open again in June (flip back allow).
    resp = _batch(
        client,
        "b1",
        [
            _batch_change("c2", "v1", _iso(2026, 3, 1), ["research"]),
            _batch_change("c3", "v1", _iso(2026, 6, 1), ["research", "review"]),
        ],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    batch = body["batch"]
    assert batch["batch_id"] == "b1"
    assert batch["change_count"] == 2
    assert batch["new_event_count"] == 2
    assert [entry["index"] for entry in batch["changes"]] == [0, 1]
    assert all(entry["replayed"] is False for entry in batch["changes"])
    assert [entry["new_event_count"] for entry in batch["changes"]] == [1, 1]
    assert [entry["change"]["change_id"] for entry in batch["changes"]] == ["c2", "c3"]

    # Even though a later batch item flips the status back, BOTH flips stay.
    events = _events(client, "s1").json()["events"]
    assert [e["seq"] for e in events] == [1, 2]
    assert events[0]["trigger_id"] == "c2"
    assert (events[0]["before_allowed"], events[0]["after_allowed"]) == (True, False)
    assert events[1]["trigger_id"] == "c3"
    assert (events[1]["before_allowed"], events[1]["after_allowed"]) == (False, True)

    # The final live decision is allowed again.
    assert _check(client, "v1", "review", _iso(2026, 9, 1)).json()["allowed"] is True


def test_batch_non_flipping_change_appends_no_event(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "s1", "v1", "research", _iso(2026, 9, 1))

    resp = _batch(
        client,
        "b1",
        [
            # Adds review but keeps research: no flip for a research watcher.
            _batch_change("c2", "v1", _iso(2026, 3, 1), ["research", "review"]),
            # Future change relative to the fixed instant: no flip either.
            _batch_change("c3", "v1", _iso(2026, 12, 1), ["research"]),
        ],
    )
    assert resp.status_code == 201
    batch = resp.json()["batch"]
    assert [entry["new_event_count"] for entry in batch["changes"]] == [0, 0]
    assert batch["new_event_count"] == 0
    assert _events(client, "s1").json()["events"] == []


def test_batch_real_flips_observe_snapshot_after_each_item(client):
    # Without re-pinning the snapshot per item the second item would compare
    # against the pre-batch baseline and never see the first item's flip.
    _register(client, "root")
    _register(client, "child", parents=["root"])
    _change(client, "cr1", "root", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "cc1", "child", _iso(2026, 1, 1), ["research", "review"])
    _subscribe(client, "s-root", "root", "review", _iso(2026, 9, 1))
    _subscribe(client, "s-child", "child", "review", _iso(2026, 9, 1))

    resp = _batch(
        client,
        "b1",
        [
            _batch_change("cr2", "root", _iso(2026, 3, 1), ["research"]),
            _batch_change("cr3", "root", _iso(2026, 4, 1), ["research", "review"]),
        ],
    )
    assert resp.status_code == 201
    batch = resp.json()["batch"]
    # root + descendant each flip once per item: 2 events per item.
    assert [entry["new_event_count"] for entry in batch["changes"]] == [2, 2]
    assert batch["new_event_count"] == 4
    for sid in ("s-root", "s-child"):
        events = _events(client, sid).json()["events"]
        assert [(e["seq"], e["trigger_id"]) for e in events] == [
            (1, "cr2"),
            (2, "cr3"),
        ]
        assert (events[0]["before_allowed"], events[0]["after_allowed"]) == (True, False)
        assert (events[1]["before_allowed"], events[1]["after_allowed"]) == (False, True)


def test_batch_duplicate_change_id_within_batch_is_rejected_atomically(client):
    _register(client, "v1")
    _change(client, "seed", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "s1", "v1", "review", _iso(2026, 9, 1))

    resp = _batch(
        client,
        "b1",
        [
            _batch_change("dup", "v1", _iso(2026, 3, 1), ["research", "review"]),
            _batch_change("dup", "v1", _iso(2026, 4, 1), ["research"]),
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    assert resp.json()["details"]["change_id"] == "dup"
    # Whole batch rejected: no change rows, no batch row, no events.
    assert client.get("/license-changes/dup").status_code == 404
    assert client.get("/license-changes/batches/b1").status_code == 404
    assert _events(client, "s1").json()["events"] == []


def test_batch_same_version_same_time_within_batch_conflicts_atomically(client):
    _register(client, "v1")
    _change(client, "seed", "v1", _iso(2026, 1, 1), ["research"])

    resp = _batch(
        client,
        "b1",
        [
            _batch_change("x1", "v1", _iso(2026, 3, 1), ["research"]),
            _batch_change("x2", "v1", _iso(2026, 3, 1), ["research", "review"]),
        ],
    )
    assert resp.status_code == 409
    assert "同一生效时刻" in resp.json()["message"]
    assert client.get("/license-changes/x1").status_code == 404
    assert client.get("/license-changes/x2").status_code == 404
    assert client.get("/license-changes/batches/b1").status_code == 404


def test_batch_conflict_with_stored_change_rejects_whole_batch(client):
    _register(client, "v1")
    _change(client, "old", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "s1", "v1", "review", _iso(2026, 9, 1))

    # Same id, different content.
    resp = _batch(
        client,
        "b1",
        [_batch_change("old", "v1", _iso(2026, 1, 1), ["commercial"])],
    )
    assert resp.status_code == 409
    # Different id at the same (version, effective instant), even with same uses.
    resp = _batch(
        client,
        "b2",
        [_batch_change("twin", "v1", _iso(2026, 1, 1), ["research"])],
    )
    assert resp.status_code == 409
    assert resp.json()["details"]["existing_change_id"] == "old"
    # A legal item appearing before the conflicting one must not be persisted.
    resp = _batch(
        client,
        "b3",
        [
            _batch_change("fine", "v1", _iso(2026, 3, 1), ["research", "review"]),
            _batch_change("old", "v1", _iso(2026, 2, 1), ["research"]),
        ],
    )
    assert resp.status_code == 409
    assert client.get("/license-changes/fine").status_code == 404
    assert client.get("/license-changes/batches/b3").status_code == 404
    assert _events(client, "s1").json()["events"] == []


def test_batch_unknown_version_is_404_without_rows(client):
    _register(client, "v1")
    resp = _batch(
        client,
        "b1",
        [
            _batch_change("c1", "v1", _iso(2026, 1, 1), ["research"]),
            _batch_change("c2", "ghost", _iso(2026, 2, 1), ["research"]),
        ],
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert "ghost" in resp.json()["details"]["missing_version_ids"]
    assert client.get("/license-changes/c1").status_code == 404
    assert client.get("/license-changes/batches/b1").status_code == 404


def test_batch_invalid_fields_are_422_without_rows(client):
    _register(client, "v1")

    # Empty list.
    assert _batch(client, "b0", []).status_code == 422
    # Naive effective time.
    resp = _batch(
        client,
        "b1",
        [_batch_change("c1", "v1", "2026-03-01T00:00:00", ["research"])],
    )
    assert resp.status_code == 422
    # Blank entry inside allowed_uses.
    resp = _batch(
        client,
        "b2",
        [_batch_change("c2", "v1", _iso(2026, 3, 1), ["   "])],
    )
    assert resp.status_code == 422
    # Missing required field.
    resp = client.post(
        "/license-changes/batches",
        json={"batch_id": "b3", "changes": [{"version_id": "v1", "allowed_uses": ["x"]}]},
    )
    assert resp.status_code == 422
    for bid in ("b0", "b1", "b2", "b3"):
        assert client.get(f"/license-changes/batches/{bid}").status_code == 404


def test_batch_identical_retry_returns_first_frozen_result_and_no_event_replay(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "s1", "v1", "review", _iso(2026, 9, 1))

    payload = {
        "batch_id": "b1",
        "changes": [
            _batch_change("c2", "v1", _iso(2026, 3, 1), ["research", "review"]),
            _batch_change("c3", "v1", _iso(2026, 6, 1), ["research"]),
        ],
    }
    first = _batch(client, "b1", payload["changes"])
    assert first.status_code == 201
    first_body = first.json()
    assert [e["seq"] for e in _events(client, "s1").json()["events"]] == [1, 2]

    # Same batch id + normalised list (uses reordered/duplicated, same instant
    # in another offset) -> 200 replay, batch object byte-identical.
    retry_changes = [
        _batch_change("c2", "v1", "2026-03-01T08:00:00+08:00", ["review", "research", "review"]),
        _batch_change("c3", "v1", _iso(2026, 6, 1), ["research"]),
    ]
    retry = _batch(client, "b1", retry_changes)
    assert retry.status_code == 200
    retry_body = retry.json()
    assert retry_body["replayed"] is True
    assert retry_body["batch"] == first_body["batch"]

    # Events are never replayed.
    assert [e["seq"] for e in _events(client, "s1").json()["events"]] == [1, 2]

    # GET returns the same frozen batch object.
    fetched = client.get("/license-changes/batches/b1")
    assert fetched.status_code == 200
    assert fetched.json() == first_body["batch"]


def test_batch_same_id_different_content_conflicts_and_does_not_replay(client):
    _register(client, "v1")
    first = _batch(
        client,
        "b1",
        [_batch_change("c1", "v1", _iso(2026, 1, 1), ["research"])],
    )
    assert first.status_code == 201

    # Different content (different list order, different uses or another id).
    resp = _batch(
        client,
        "b1",
        [_batch_change("c1", "v1", _iso(2026, 1, 1), ["research", "review"])],
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"

    resp = _batch(
        client,
        "b1",
        [
            _batch_change("c2", "v1", _iso(2026, 2, 1), ["research"]),
            _batch_change("c1", "v1", _iso(2026, 1, 1), ["research"]),
        ],
    )
    assert resp.status_code == 409
    # The conflicting request left nothing behind.
    assert client.get("/license-changes/c2").status_code == 404


def test_batch_replayed_item_mixes_with_new_items(client):
    _register(client, "v1")
    _change(client, "old", "v1", _iso(2026, 1, 1), ["research"])
    _subscribe(client, "s1", "v1", "review", _iso(2026, 9, 1))

    resp = _batch(
        client,
        "b1",
        [
            # Existing identical change: a replay item — no write, no events.
            _batch_change("old", "v1", _iso(2026, 1, 1), ["research"]),
            # New change that actually flips.
            _batch_change("new", "v1", _iso(2026, 3, 1), ["research", "review"]),
        ],
    )
    assert resp.status_code == 201, resp.text
    entries = resp.json()["batch"]["changes"]
    assert entries[0]["replayed"] is True
    assert entries[0]["new_event_count"] == 0
    assert entries[1]["replayed"] is False
    assert entries[1]["new_event_count"] == 1
    assert resp.json()["batch"]["new_event_count"] == 1
    assert [e["seq"] for e in _events(client, "s1").json()["events"]] == [1]

    # Retrying the whole batch replays everything (still no extra events).
    retry = _batch(
        client,
        "b1",
        [
            _batch_change("old", "v1", _iso(2026, 1, 1), ["research"]),
            _batch_change("new", "v1", _iso(2026, 3, 1), ["research", "review"]),
        ],
    )
    assert retry.status_code == 200
    assert [e["seq"] for e in _events(client, "s1").json()["events"]] == [1]


def test_batch_changes_interoperate_with_single_submit_and_revocation(client):
    _register(client, "v1")
    resp = _batch(
        client,
        "b1",
        [_batch_change("bc1", "v1", _iso(2026, 1, 1), ["research", "review"])],
    )
    assert resp.status_code == 201

    # The batch-committed change is queryable and replayable via the single API.
    single_replay = _change(client, "bc1", "v1", _iso(2026, 1, 1), ["review", "research"])
    assert single_replay.status_code == 200
    assert single_replay.json()["replayed"] is True
    assert client.get("/license-changes/bc1").status_code == 200

    # It can be revoked via the existing revocation endpoint.
    assert _revoke(client, "bc1", "r-bc1", "x").status_code == 201

    # The frozen batch result is unaffected by the later revocation, while the
    # live change lookup still shows the revocation marker.
    frozen = client.get("/license-changes/batches/b1").json()
    assert frozen["changes"][0]["change"]["revocation"] is None
    assert client.get("/license-changes/bc1").json()["revocation"]["revocation_id"] == "r-bc1"


def test_batch_without_id_is_generated(client):
    _register(client, "v1")
    resp = _batch(
        client,
        None,
        [_batch_change(None, "v1", _iso(2026, 1, 1), ["research"])],
    )
    assert resp.status_code == 201
    assert resp.json()["batch"]["batch_id"].startswith("b-")
    assert resp.json()["batch"]["changes"][0]["change"]["change_id"].startswith("c-")


def test_batch_unknown_id_get_is_404(client):
    resp = client.get("/license-changes/batches/no-such-batch")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_batch_descendant_diamond_subscription_counts_flips_once_each(client):
    _register(client, "a")
    _register(client, "b", parents=["a"])
    _register(client, "d1", parents=["b", "a"])
    for vid in ("a", "b", "d1"):
        _change(client, f"c-{vid}", vid, _iso(2026, 1, 1), ["research", "review"])
    _subscribe(client, "s-d1", "d1", "review", _iso(2026, 9, 1))

    # Narrowing a inside a batch flips the diamond descendant exactly once.
    resp = _batch(
        client,
        "b1",
        [_batch_change("ca2", "a", _iso(2026, 3, 1), ["research"])],
    )
    assert resp.status_code == 201
    assert resp.json()["batch"]["changes"][0]["new_event_count"] == 1
    events = _events(client, "s-d1").json()["events"]
    assert len(events) == 1
    assert [bl["version_id"] for bl in events[0]["after_blockers"]] == ["a"]


def test_batch_concurrent_same_id_commits_exactly_once(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    import threading

    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            _, replayed = service.submit_changes_batch(
                batch_id="b1",
                items=[
                    {
                        "change_id": "c2",
                        "version_id": "v1",
                        "effective_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
                        "allowed_uses": ["research", "review"],
                    }
                ],
            )
            with lock:
                codes.append(200 if replayed else 201)
        except Exception as exc:  # noqa: BLE001
            with lock:
                codes.append(getattr(exc, "status_code", 0))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(201) == 1, codes
    assert codes.count(200) == 7, codes
    assert client.get("/license-changes/c2").status_code == 200


def test_batch_on_demo_seed(tmp_path):
    db_path = str(tmp_path / "seed-batch.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        # Watch review on demo-derived-1 at a late-2026 instant: blocked by
        # both roots (root-a narrowed in June, root-b never allowed review).
        sub = test_client.post(
            "/permissions/subscriptions",
            json={
                "subscription_id": "demo-batch-sub",
                "version_id": "demo-derived-1",
                "use": "review",
                "at": "2026-09-01T00:00:00Z",
            },
        )
        assert sub.status_code == 201
        assert sub.json()["subscription"]["baseline_allowed"] is False

        # Batch: root-a reopens review in August and root-b does the same in
        # the same transaction. The descendant flips exactly once per item.
        resp = test_client.post(
            "/license-changes/batches",
            json={
                "batch_id": "demo-batch-1",
                "changes": [
                    {
                        "change_id": "demo-batch-a-3",
                        "version_id": "demo-root-a",
                        "effective_at": "2026-08-01T00:00:00Z",
                        "allowed_uses": ["research", "review"],
                    },
                    {
                        "change_id": "demo-batch-b-2",
                        "version_id": "demo-root-b",
                        "effective_at": "2026-08-15T00:00:00Z",
                        "allowed_uses": ["research", "review", "commercial"],
                    },
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        batch = resp.json()["batch"]
        # First item still blocked by root-b: no descendant flip; second item
        # removes the last blocker: the descendant flips deny -> allow once.
        assert [entry["new_event_count"] for entry in batch["changes"]] == [0, 1]
        events = test_client.get(
            "/permissions/subscriptions/demo-batch-sub/events"
        ).json()["events"]
        assert len(events) == 1
        assert events[0]["trigger_id"] == "demo-batch-b-2"
        assert events[0]["after_allowed"] is True

        # An identical retry replays the frozen result without new events.
        retry = test_client.post(
            "/license-changes/batches",
            json={
                "batch_id": "demo-batch-1",
                "changes": [
                    {
                        "change_id": "demo-batch-a-3",
                        "version_id": "demo-root-a",
                        "effective_at": "2026-08-01T00:00:00Z",
                        "allowed_uses": ["review", "research"],
                    },
                    {
                        "change_id": "demo-batch-b-2",
                        "version_id": "demo-root-b",
                        "effective_at": "2026-08-15T00:00:00Z",
                        "allowed_uses": ["commercial", "research", "review"],
                    },
                ],
            },
        )
        assert retry.status_code == 200
        assert retry.json()["replayed"] is True
        assert retry.json()["batch"] == batch
        assert (
            len(
                test_client.get(
                    "/permissions/subscriptions/demo-batch-sub/events"
                ).json()["events"]
            )
            == 1
        )

