"""Idempotent demo data used to initialise an empty database.

Enabled by the ``SEED_DEMO`` setting. Running it repeatedly is safe: each
entity is first looked up and reused, and change replays go through the same
service path as client submissions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .database import Database
from .service import LicenseService

logger = logging.getLogger("license_service.seed")

DEMO: list[dict[str, object]] = [
    {
        "version_id": "demo-root-a",
        "parent_ids": [],
        "note": "示例根版本 A",
        "changes": [
            {
                "change_id": "demo-chg-a-1",
                "effective_at": "2026-01-01T00:00:00Z",
                "allowed_uses": ["research", "review"],
            },
            {
                "change_id": "demo-chg-a-2",
                "effective_at": "2026-06-01T00:00:00Z",
                "allowed_uses": ["research"],
            },
        ],
    },
    {
        "version_id": "demo-root-b",
        "parent_ids": [],
        "note": "示例根版本 B",
        "changes": [
            {
                "change_id": "demo-chg-b-1",
                "effective_at": "2026-01-01T00:00:00Z",
                "allowed_uses": ["research", "commercial"],
            }
        ],
    },
    {
        # Child of both roots: allowed uses are the intersection.
        # research  => allowed; review/commercial => blocked.
        "version_id": "demo-derived-1",
        "parent_ids": ["demo-root-a", "demo-root-b"],
        "note": "示例派生版本（双父版本，用途交集）",
        "changes": [
            {
                "change_id": "demo-chg-d1-1",
                "effective_at": "2026-02-01T00:00:00Z",
                "allowed_uses": ["research", "review", "commercial"],
            }
        ],
    },
    {
        # Registered with no change at all: queries are blocked with
        # no_valid_license until a change is submitted.
        "version_id": "demo-no-license",
        "parent_ids": [],
        "note": "示例：尚未登记任何许可变更的版本",
        "changes": [],
    },
]


def seed_demo_data(db: Database) -> bool:
    """Populate demo data into an empty database.

    Returns ``True`` when data was inserted, ``False`` when the database
    already contained versions.
    """
    service = LicenseService(db)
    if db.count_versions() > 0:
        logger.info("数据库非空，跳过示例数据")
        return False

    logger.info("写入示例数据集…")
    for item in DEMO:
        version_id = str(item["version_id"])
        if db.get_version(version_id) is None:
            service.register_version(
                version_id=version_id,
                parent_ids=list(item["parent_ids"]),  # type: ignore[arg-type]
                note=str(item["note"]),
            )
        for change in item["changes"]:  # type: ignore[union-attr]
            service.submit_change(
                change_id=str(change["change_id"]),
                version_id=version_id,
                effective_at=datetime.fromisoformat(str(change["effective_at"])).replace(
                    tzinfo=timezone.utc
                ),
                allowed_uses=list(change["allowed_uses"]),  # type: ignore[index]
            )
    logger.info("示例数据写入完成：%d 个版本", len(DEMO))
    return True
