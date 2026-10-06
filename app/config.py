"""Application configuration.

All values can be overridden through environment variables, which keeps the
container image free of deployment specific settings (see README.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # SQLite database file. Inside Compose this points at a mounted volume.
    db_path: str = os.getenv("DB_PATH", "/data/licenses.db")
    # Directory holding the persistent Ed25519 signing keys for voucher proof
    # packages. When unset it defaults to a "signing-keys" folder next to the
    # database (see __post_init__), so a fresh checkout keeps working.
    signing_keys_dir: str | None = os.getenv("SIGNING_KEYS_DIR")
    # Host/port used by uvicorn.
    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = int(os.getenv("APP_PORT", "8000"))
    # Create schema on startup if it does not exist yet.
    init_db: bool = _as_bool(os.getenv("INIT_DB"), default=True)
    # Seed a small, fixed demo dataset on first boot (idempotent).
    seed_demo: bool = _as_bool(os.getenv("SEED_DEMO"), default=True)
    # Generate (or repair the active pointer of) the signing key on startup.
    init_signing_key: bool = _as_bool(os.getenv("INIT_SIGNING_KEY"), default=True)
    # Print resolved configuration at startup (useful for container logs).
    echo: bool = _as_bool(os.getenv("CONFIG_ECHO"), default=True)

    def __post_init__(self) -> None:
        # Derive the key directory from the actual db_path unless SIGNING_KEYS_DIR
        # was set explicitly; keeps ephemeral/test databases self-contained.
        if not self.signing_keys_dir:
            default_dir = os.path.join(
                os.path.dirname(os.path.abspath(self.db_path)), "signing-keys"
            )
            object.__setattr__(self, "signing_keys_dir", default_dir)

    @property
    def base_url(self) -> str:
        host = "localhost" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{host}:{self.port}"


settings = Settings()
