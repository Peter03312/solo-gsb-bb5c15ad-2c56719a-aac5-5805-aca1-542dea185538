"""SQLite persistence layer.

A single :class:`Database` owns the process-wide connection. SQLite itself
serialises writes; a process-level re-entrant lock additionally guards the
read-then-write transactions in the service layer so duplicate submissions
remain deterministic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any


class UniqueViolation(sqlite3.IntegrityError):
    """Marker raised by :meth:`Database.commit` on a UNIQUE constraint."""


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS versions (
        version_id   TEXT PRIMARY KEY,
        created_at   TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS version_parents (
        version_id TEXT NOT NULL REFERENCES versions(version_id),
        parent_id  TEXT NOT NULL REFERENCES versions(version_id),
        position   INTEGER NOT NULL,
        PRIMARY KEY (version_id, parent_id)
    )
    """,
    # The same parent may legitimately be referenced by different child
    # versions, so only the pair itself is unique.
    """
    CREATE TABLE IF NOT EXISTS license_changes (
        change_id     TEXT PRIMARY KEY,
        version_id    TEXT NOT NULL REFERENCES versions(version_id),
        effective_at  TEXT NOT NULL,
        allowed_uses  TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        created_at    TEXT NOT NULL
    )
    """,
    # A license change can be revoked at most once. The original row stays in
    # license_changes (it remains queryable and replayable); every evaluation
    # path filters out changes whose id appears here.
    """
    CREATE TABLE IF NOT EXISTS change_revocations (
        revocation_id TEXT PRIMARY KEY,
        change_id     TEXT NOT NULL UNIQUE REFERENCES license_changes(change_id),
        reason        TEXT NOT NULL,
        created_at    TEXT NOT NULL
    )
    """,
    # A batch import submits a non-empty ordered list of license changes under
    # one unique batch id. The whole batch either commits (every change plus the
    # subscription flip events it caused) or is rejected without a single row.
    # The first result is frozen verbatim: an identical retry (same batch id
    # and normalised list) returns it without re-applying anything or replaying
    # events; reusing the id with different content conflicts.
    """
    CREATE TABLE IF NOT EXISTS license_change_batches (
        batch_id     TEXT PRIMARY KEY,
        content_hash TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        result_json  TEXT NOT NULL
    )
    """,
    # A permission voucher freezes one permission decision as a traceable
    # credential: the normalised request inputs (version, trimmed use, UTC
    # instant) live in columns for idempotency/conflict checks, while the full
    # decision (closure, basis, blockers, intersection) is captured as JSON in
    # one write transaction with the evaluation. Rows are immutable history —
    # no API ever updates or deletes them; later license changes or revocations
    # must not rewrite a stored voucher.
    """
    CREATE TABLE IF NOT EXISTS permission_vouchers (
        voucher_id     TEXT PRIMARY KEY,
        version_id     TEXT NOT NULL REFERENCES versions(version_id),
        use_name       TEXT NOT NULL,
        at_value       TEXT NOT NULL,
        registered_at  TEXT NOT NULL,
        allowed        INTEGER NOT NULL,
        decision_json  TEXT NOT NULL
    )
    """,
    # A proof package freezes a signed export of a voucher. The whole package
    # (signed payload + signature + verification rules) is stored verbatim so
    # repeat and concurrent exports return byte-identical content; rows are
    # immutable history — no API ever updates or re-signs them, even after key
    # rotation. The key_id/algorithm columns make the binding explicit, but the
    # authoritative signature lives in proof_json.
    """
    CREATE TABLE IF NOT EXISTS voucher_proofs (
        voucher_id      TEXT PRIMARY KEY REFERENCES permission_vouchers(voucher_id),
        key_id          TEXT NOT NULL,
        algorithm       TEXT NOT NULL,
        exported_at     TEXT NOT NULL,
        proof_json      TEXT NOT NULL
    )
    """,
    # A permission subscription watches one fixed permission decision: a
    # unique subscription id plus the registered version, the trimmed use and
    # the timezone-aware decision instant (normalised to UTC). At creation the
    # then-current decision is atomically frozen as the immutable baseline
    # (allow/deny plus the blockers with their reasons). Rows are never updated
    # or deleted; subsequent license changes/revocations only append events.
    """
    CREATE TABLE IF NOT EXISTS permission_subscriptions (
        subscription_id  TEXT PRIMARY KEY,
        version_id       TEXT NOT NULL REFERENCES versions(version_id),
        use_name         TEXT NOT NULL,
        at_value         TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        baseline_allowed INTEGER NOT NULL,
        baseline_json    TEXT NOT NULL
    )
    """,
    # Every row is exactly one allow<->deny flip of one subscription, appended
    # in the SAME write transaction as the license change/revocation that
    # caused it. Sequence numbers are per subscription and start at 1; the
    # (subscription_id, seq) primary key both fixes the stable read order and
    # guarantees a retried/duplicated trigger can never append twice. Rows are
    # immutable history — no API ever updates or deletes them.
    """
    CREATE TABLE IF NOT EXISTS subscription_events (
        subscription_id     TEXT NOT NULL REFERENCES permission_subscriptions(subscription_id),
        seq                 INTEGER NOT NULL,
        trigger_kind        TEXT NOT NULL,
        trigger_id          TEXT NOT NULL,
        change_id           TEXT NOT NULL,
        effective_at        TEXT,
        before_allowed      INTEGER NOT NULL,
        after_allowed       INTEGER NOT NULL,
        before_blockers_json TEXT NOT NULL,
        after_blockers_json  TEXT NOT NULL,
        created_at          TEXT NOT NULL,
        PRIMARY KEY (subscription_id, seq)
    )
    """,
    # A version-slot publish carries a unique release id, one timezone-aware
    # effective instant and a non-empty list of slots; each slot names a
    # trimmed non-empty slot, an existing target version and the expected
    # revision it must replace. Rows are immutable history — no API ever
    # updates or deletes them; the first result is frozen verbatim in
    # result_json so an identical retry returns it without advancing anything.
    """
    CREATE TABLE IF NOT EXISTS releases (
        release_id    TEXT PRIMARY KEY,
        content_hash  TEXT NOT NULL,
        effective_at  TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        result_json   TEXT NOT NULL
    )
    """,
    # One row per slot of a release, inserted in the SAME transaction as the
    # release row and the slot-head advances. position preserves request order
    # (the content hash is order-sensitive); expected_revision records what the
    # client asserted and assigned_revision the revision this publish gave the
    # slot. Rows are immutable history.
    """
    CREATE TABLE IF NOT EXISTS release_slots (
        release_id        TEXT NOT NULL REFERENCES releases(release_id),
        position          INTEGER NOT NULL,
        slot_name         TEXT NOT NULL,
        version_id        TEXT NOT NULL REFERENCES versions(version_id),
        expected_revision INTEGER NOT NULL,
        assigned_revision INTEGER NOT NULL,
        effective_at      TEXT NOT NULL,
        PRIMARY KEY (release_id, position),
        UNIQUE (slot_name, assigned_revision)
    )
    """,
    # Mutable head pointer of every slot: exactly one row per trimmed slot
    # name, advanced inside the publish transaction after every validation has
    # passed. revision 0 is the first publish, then 1, 2, ...; the unique
    # (slot_name, revision) history constraint on release_slots (below) makes a
    # skipped or double-assigned revision impossible even across processes.
    """
    CREATE TABLE IF NOT EXISTS version_slot_heads (
        slot_name    TEXT PRIMARY KEY,
        revision     INTEGER NOT NULL,
        release_id   TEXT NOT NULL REFERENCES releases(release_id),
        version_id   TEXT NOT NULL REFERENCES versions(version_id),
        effective_at TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    # A delivery admission freezes one whole-window manifest decision under a
    # unique delivery id: the timezone-aware half-open usage window (normalised
    # to UTC) and an ordered, non-empty list of entries, each referencing either
    # a registered version or a version slot and stating one intended use. The
    # first result — every entry's per-segment resolved version and decision
    # basis — is frozen verbatim in result_json. Rows are immutable history: no
    # API ever updates or deletes them, and later catalog changes (new
    # releases, license changes, revocations) must not rewrite a stored
    # admission. A denied submission never inserts a row.
    """
    CREATE TABLE IF NOT EXISTS delivery_admissions (
        delivery_id  TEXT PRIMARY KEY,
        content_hash TEXT NOT NULL,
        start_value  TEXT NOT NULL,
        end_value    TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        result_json  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_changes_version_time "
    "ON license_changes(version_id, effective_at)",
    "CREATE INDEX IF NOT EXISTS idx_parents_version ON version_parents(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_parents_parent ON version_parents(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_revocations_change ON change_revocations(change_id)",
    "CREATE INDEX IF NOT EXISTS idx_vouchers_version ON permission_vouchers(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_version "
    "ON permission_subscriptions(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_sub_events_subscription "
    "ON subscription_events(subscription_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_release_slots_slot "
    "ON release_slots(slot_name, assigned_revision)",
    "CREATE INDEX IF NOT EXISTS idx_release_slots_version "
    "ON release_slots(version_id)",
]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "allowed_uses" in data and isinstance(data["allowed_uses"], str):
        data["allowed_uses"] = json.loads(data["allowed_uses"])
    return data


class Database:
    """Thin wrapper around a SQLite database connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit BEGIN/COMMIT below
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 30000")

    # -- lifecycle -------------------------------------------------------

    def init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # -- raw helpers ------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return row_to_dict(self._conn.execute(sql, params).fetchone())

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [row_to_dict(row) for row in self._conn.execute(sql, params).fetchall()]  # type: ignore[list-item]

    def begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def begin_read(self) -> None:
        """Open a deferred read transaction.

        In WAL mode the first SELECT pins a consistent snapshot that stays
        stable until :meth:`rollback`; being deferred it takes no write lock.
        Previews read versions, parent edges and changes through one such
        transaction so concurrent commits cannot mix snapshots.
        """
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        try:
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            self._conn.execute("ROLLBACK")
            raise

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # -- versions ---------------------------------------------------------

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM versions WHERE version_id = ?", (version_id,))

    def list_versions(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM versions ORDER BY created_at, version_id")

    def count_versions(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()
        return int(row["n"])

    def insert_version(self, version_id: str, created_at: str, note: str) -> None:
        self._conn.execute(
            "INSERT INTO versions (version_id, created_at, note) VALUES (?, ?, ?)",
            (version_id, created_at, note),
        )

    def insert_parent(self, version_id: str, parent_id: str, position: int) -> None:
        self._conn.execute(
            "INSERT INTO version_parents (version_id, parent_id, position) VALUES (?, ?, ?)",
            (version_id, parent_id, position),
        )

    def get_parents(self, version_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT parent_id FROM version_parents WHERE version_id = ? ORDER BY position, parent_id",
            (version_id,),
        ).fetchall()
        return [row["parent_id"] for row in rows]

    def get_children(self, version_id: str) -> list[str]:
        """All versions that reference ``version_id`` directly as a parent."""
        rows = self._conn.execute(
            "SELECT DISTINCT version_id FROM version_parents WHERE parent_id = ? "
            "ORDER BY version_id",
            (version_id,),
        ).fetchall()
        return [row["version_id"] for row in rows]

    def all_parent_edges(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT version_id, parent_id FROM version_parents ORDER BY position, parent_id"
        ).fetchall()
        return [(row["version_id"], row["parent_id"]) for row in rows]

    # -- license changes --------------------------------------------------

    def get_change(self, change_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM license_changes WHERE change_id = ?", (change_id,))

    def list_changes(self, version_id: str) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM license_changes WHERE version_id = ? "
            "ORDER BY effective_at, change_id",
            (version_id,),
        )

    def insert_change(
        self,
        change_id: str,
        version_id: str,
        effective_at: str,
        allowed_uses: list[str],
        content_hash: str,
        created_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO license_changes "
            "(change_id, version_id, effective_at, allowed_uses, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                change_id,
                version_id,
                effective_at,
                json.dumps(allowed_uses, ensure_ascii=False),
                content_hash,
                created_at,
            ),
        )

    def change_at_same_time(self, version_id: str, effective_at: str) -> dict[str, Any] | None:
        """Return the (single) change of a version at the exact same instant.

        Multiple changes for one version with one effective time are forbidden
        ("同版本同生效时刻出现不同变更应报冲突"). Replays with the same change id
        are handled before reaching this query.
        """
        return self.query_one(
            "SELECT * FROM license_changes WHERE version_id = ? AND effective_at = ? "
            "ORDER BY change_id LIMIT 1",
            (version_id, effective_at),
        )

    def latest_change(self, version_id: str, at: str) -> dict[str, Any] | None:
        # Revoked changes never take effect for any query instant.
        return self.query_one(
            "SELECT lc.* FROM license_changes lc "
            "LEFT JOIN change_revocations cr ON cr.change_id = lc.change_id "
            "WHERE lc.version_id = ? AND lc.effective_at <= ? AND cr.change_id IS NULL "
            "ORDER BY lc.effective_at DESC, lc.change_id LIMIT 1",
            (version_id, at),
        )

    # -- license change batches -------------------------------------------

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM license_change_batches WHERE batch_id = ?",
            (batch_id,),
        )

    def insert_batch(
        self,
        batch_id: str,
        content_hash: str,
        created_at: str,
        result_json: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO license_change_batches "
            "(batch_id, content_hash, created_at, result_json) "
            "VALUES (?, ?, ?, ?)",
            (batch_id, content_hash, created_at, result_json),
        )

    # -- change revocations ----------------------------------------------

    def get_revocation(self, revocation_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM change_revocations WHERE revocation_id = ?",
            (revocation_id,),
        )

    def get_revocation_of_change(self, change_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM change_revocations WHERE change_id = ?",
            (change_id,),
        )

    def list_revoked_change_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT change_id FROM change_revocations ORDER BY change_id"
        ).fetchall()
        return [row["change_id"] for row in rows]

    def insert_revocation(
        self,
        revocation_id: str,
        change_id: str,
        reason: str,
        created_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO change_revocations "
            "(revocation_id, change_id, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (revocation_id, change_id, reason, created_at),
        )

    # -- permission vouchers ----------------------------------------------

    def get_voucher(self, voucher_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM permission_vouchers WHERE voucher_id = ?",
            (voucher_id,),
        )

    def insert_voucher(
        self,
        voucher_id: str,
        version_id: str,
        use_name: str,
        at_value: str,
        registered_at: str,
        allowed: bool,
        decision_json: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO permission_vouchers "
            "(voucher_id, version_id, use_name, at_value, registered_at, "
            "allowed, decision_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                voucher_id,
                version_id,
                use_name,
                at_value,
                registered_at,
                1 if allowed else 0,
                decision_json,
            ),
        )

    # -- voucher proof packages -------------------------------------------

    def get_proof(self, voucher_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM voucher_proofs WHERE voucher_id = ?",
            (voucher_id,),
        )

    def insert_proof(
        self,
        voucher_id: str,
        key_id: str,
        algorithm: str,
        exported_at: str,
        proof_json: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO voucher_proofs "
            "(voucher_id, key_id, algorithm, exported_at, proof_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (voucher_id, key_id, algorithm, exported_at, proof_json),
        )

    # -- permission subscriptions -----------------------------------------

    def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM permission_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        )

    def list_subscriptions_for_versions(self, version_ids: list[str]) -> list[dict[str, Any]]:
        """All subscriptions watched on any of ``version_ids``.

        Called inside a write transaction after a change/revocation; a change
        on a version can only flip subscriptions on that version itself or its
        descendants (the caller passes the full deduplicated closure), so this
        candidate set plus the in-memory closure is exact.
        """
        if not version_ids:
            return []
        placeholders = ",".join("?" for _ in version_ids)
        return self.query_all(
            f"SELECT * FROM permission_subscriptions WHERE version_id IN ({placeholders}) "
            "ORDER BY created_at, subscription_id",
            tuple(version_ids),
        )

    def insert_subscription(
        self,
        subscription_id: str,
        version_id: str,
        use_name: str,
        at_value: str,
        created_at: str,
        baseline_allowed: bool,
        baseline_json: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO permission_subscriptions "
            "(subscription_id, version_id, use_name, at_value, created_at, "
            "baseline_allowed, baseline_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                subscription_id,
                version_id,
                use_name,
                at_value,
                created_at,
                1 if baseline_allowed else 0,
                baseline_json,
            ),
        )

    # -- subscription flip events -----------------------------------------

    def latest_subscription_events(self, subscription_ids: list[str]) -> dict[str, Any]:
        """Latest event row per subscription id (no rows -> baseline).

        One window-function read keeps "current allowed/blockers" of every
        candidate consistent with the surrounding transaction snapshot.
        """
        if not subscription_ids:
            return {}
        placeholders = ",".join("?" for _ in subscription_ids)
        rows = self.query_all(
            f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY subscription_id ORDER BY seq DESC
                ) AS rn
                FROM subscription_events
                WHERE subscription_id IN ({placeholders})
            ) WHERE rn = 1
            """,
            tuple(subscription_ids),
        )
        return {row["subscription_id"]: row for row in rows}

    def next_event_seq(self, subscription_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM subscription_events WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        return int(row["next_seq"])

    def insert_subscription_event(
        self,
        subscription_id: str,
        seq: int,
        trigger_kind: str,
        trigger_id: str,
        change_id: str,
        effective_at: str | None,
        before_allowed: bool,
        after_allowed: bool,
        before_blockers_json: str,
        after_blockers_json: str,
        created_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO subscription_events "
            "(subscription_id, seq, trigger_kind, trigger_id, change_id, "
            "effective_at, before_allowed, after_allowed, "
            "before_blockers_json, after_blockers_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                subscription_id,
                seq,
                trigger_kind,
                trigger_id,
                change_id,
                effective_at,
                1 if before_allowed else 0,
                1 if after_allowed else 0,
                before_blockers_json,
                after_blockers_json,
                created_at,
            ),
        )

    def list_subscription_events(
        self, subscription_id: str, after_seq: int, limit: int
    ) -> list[dict[str, Any]]:
        """Stable, cursor-ordered event page: strictly increasing seq."""
        return self.query_all(
            "SELECT * FROM subscription_events "
            "WHERE subscription_id = ? AND seq > ? "
            "ORDER BY seq ASC LIMIT ?",
            (subscription_id, after_seq, limit),
        )

    # -- version-slot releases --------------------------------------------

    def get_release(self, release_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM releases WHERE release_id = ?", (release_id,)
        )

    def list_releases(self) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM releases ORDER BY created_at, release_id"
        )

    def insert_release(
        self,
        release_id: str,
        content_hash: str,
        effective_at: str,
        created_at: str,
        result_json: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO releases "
            "(release_id, content_hash, effective_at, created_at, result_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (release_id, content_hash, effective_at, created_at, result_json),
        )

    def insert_release_slot(
        self,
        release_id: str,
        position: int,
        slot_name: str,
        version_id: str,
        expected_revision: int,
        assigned_revision: int,
        effective_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO release_slots "
            "(release_id, position, slot_name, version_id, expected_revision, "
            "assigned_revision, effective_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                release_id,
                position,
                slot_name,
                version_id,
                expected_revision,
                assigned_revision,
                effective_at,
            ),
        )

    def get_slot_head(self, slot_name: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM version_slot_heads WHERE slot_name = ?",
            (slot_name,),
        )

    def list_slot_heads(self) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM version_slot_heads ORDER BY slot_name"
        )

    def insert_slot_head(
        self,
        slot_name: str,
        revision: int,
        release_id: str,
        version_id: str,
        effective_at: str,
        updated_at: str,
    ) -> None:
        """Advance a slot that had never been published (its revision 0 row)."""
        self._conn.execute(
            "INSERT INTO version_slot_heads "
            "(slot_name, revision, release_id, version_id, effective_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slot_name, revision, release_id, version_id, effective_at, updated_at),
        )

    def update_slot_head(
        self,
        slot_name: str,
        revision: int,
        release_id: str,
        version_id: str,
        effective_at: str,
        updated_at: str,
        previous_revision: int,
    ) -> None:
        """Conditionally advance a slot head only when the revision still matches.

        The predicate makes the compare-and-set atomic inside the write
        transaction: the row moves from ``previous_revision`` (the revision the
        caller validated against) to ``revision``. ``rowcount`` is 0 when a
        concurrent publish won the slot, which the caller maps onto a
        revision-mismatch conflict (and rolls the whole release back).
        """
        cursor = self._conn.execute(
            "UPDATE version_slot_heads SET revision = ?, release_id = ?, "
            "version_id = ?, effective_at = ?, updated_at = ? "
            "WHERE slot_name = ? AND revision = ?",
            (
                revision,
                release_id,
                version_id,
                effective_at,
                updated_at,
                slot_name,
                previous_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError(
                f"slot head revision advanced concurrently: {slot_name}"
            )

    def resolve_slot_at(self, slot_name: str, at: str) -> dict[str, Any] | None:
        """Most recent publish of a slot effective at or before ``at``.

        Joins the immutable release history with its entries; the effective
        instant is the release-wide one (denormalised onto every entry) and
        revisions are dense per slot, so the highest assigned revision at or
        before ``at`` is exactly the historical target version.
        """
        return self.query_one(
            """
            SELECT rs.slot_name AS slot_name,
                   rs.assigned_revision AS revision,
                   rs.release_id AS release_id,
                   rs.version_id AS version_id,
                   rs.effective_at AS effective_at,
                   r.created_at AS release_created_at
            FROM release_slots rs
            JOIN releases r ON r.release_id = rs.release_id
            WHERE rs.slot_name = ? AND rs.effective_at <= ?
            ORDER BY rs.assigned_revision DESC
            LIMIT 1
            """,
            (slot_name, at),
        )

    def has_slot_any_release(self, slot_name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM release_slots WHERE slot_name = ? LIMIT 1",
            (slot_name,),
        ).fetchone()
        return row is not None

    def all_release_slots(self) -> list[dict[str, Any]]:
        """Every immutable slot entry, ordered for snapshot pinning.

        Admission evaluation walks each slot's release history inside one
        pinned snapshot; the effective instant (release-wide, denormalised onto
        the entry) orders the timeline and dense revisions break ties.
        """
        return self.query_all(
            "SELECT release_id, position, slot_name, version_id, "
            "expected_revision, assigned_revision, effective_at "
            "FROM release_slots "
            "ORDER BY slot_name, effective_at, assigned_revision, position, release_id"
        )

    # -- delivery admissions ----------------------------------------------

    def get_delivery_admission(self, delivery_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM delivery_admissions WHERE delivery_id = ?",
            (delivery_id,),
        )

    def insert_delivery_admission(
        self,
        delivery_id: str,
        content_hash: str,
        start_value: str,
        end_value: str,
        created_at: str,
        result_json: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO delivery_admissions "
            "(delivery_id, content_hash, start_value, end_value, created_at, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (delivery_id, content_hash, start_value, end_value, created_at, result_json),
        )
