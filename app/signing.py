"""Local persistent signing keys and JWS proof packaging.

Proof packages handed to offline auditors are signed with Ed25519 keys
(RFC 8037, JWS ``alg=EdDSA`` compact serialization, RFC 7515 §7.1). Keys live
in a local directory on the persistent volume:

    <keys-dir>/<key_id>.json   # metadata + PKCS8 private / SPKI public PEM
    <keys-dir>/active          # single line: the active key id

Rotating creates a fresh key and rewrites ``active``; every old key file is
retained so proof packages signed before the rotation stay verifiable, and
stored packages are never re-signed.

The signed payload is canonical UTF-8 JSON: keys sorted lexicographically,
compact separators (``,`` / ``:``) and non-ASCII characters emitted as-is
(``ensure_ascii=False``). Tampering with any covered field changes the
canonical bytes and therefore fails verification.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Canonical UTC timestamp used across key metadata (mirrors service.py).
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

JWS_ALG = "EdDSA"
SIGNING_ALGORITHM = "Ed25519"
CURVE = "Ed25519"
FORMAT_VERSION = 1

ACTIVE_POINTER_FILE = "active"


class SigningKeyUnavailableError(Exception):
    """Raised when no usable signing key can be loaded or persisted.

    Mapped to HTTP 503: callers see an explicit error instead of a crash or a
    silently unsigned package.
    """

    status_code = 503
    code = "signing_key_unavailable"

    def __init__(self, message: str, details: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def now_canonical() -> str:
    return datetime.now(timezone.utc).strftime(_TIME_FORMAT)


# -- base64url (RFC 4648 §5, no padding) ------------------------------------


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def canonical_json_bytes(value: object) -> bytes:
    """The one verification encoding: sorted, compact, UTF-8 JSON."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def public_key_jwk(key: Ed25519PublicKey, key_id: str | None = None) -> dict[str, str]:
    """RFC 8037 §2 OKP JWK for an Ed25519 public key."""
    jwk: dict[str, str] = {
        "kty": "OKP",
        "crv": CURVE,
        "x": b64url_encode(
            key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ),
    }
    if key_id is not None:
        jwk["kid"] = key_id
    return jwk


def jwk_to_public_key(jwk: dict[str, str]) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != CURVE:
        raise ValueError("仅支持 Ed25519 OKP 公钥（kty=OKP, crv=Ed25519）")
    return Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))


def verify_compact(
    protected_b64: str,
    payload_b64: str,
    signature_b64: str,
    public_key: Ed25519PublicKey,
) -> None:
    """Verify a JWS EdDSA compact signature. Raises InvalidSignature on failure."""
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    public_key.verify(b64url_decode(signature_b64), signing_input)


# -- key material -----------------------------------------------------------


@dataclass(frozen=True)
class StoredSigningKey:
    key_id: str
    created_at: str  # canonical UTC
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    def public_metadata(self, active: bool) -> dict[str, object]:
        return {
            "key_id": self.key_id,
            "algorithm": SIGNING_ALGORITHM,
            "jws_alg": JWS_ALG,
            "curve": CURVE,
            "created_at": self.created_at,
            "active": active,
            "public_key_pem": self.public_key_pem,
            "public_key_jwk": public_key_jwk(self.public_key, self.key_id),
        }

    @property
    def public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    @property
    def private_key_pem(self) -> str:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")


def _generate_key_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    return f"key-{stamp}-{uuid.uuid4().hex[:8]}"


def _atomic_write(path: str, text: str, *, mode: int | None = 0o600) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace)."""
    tmp_path = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise exc


class KeyStore:
    """File-system backed store of persistent Ed25519 signing keys."""

    def __init__(self, keys_dir: str) -> None:
        self.keys_dir = keys_dir
        self._lock = threading.RLock()

    # -- persistence helpers ----------------------------------------------

    def _key_path(self, key_id: str) -> str:
        return os.path.join(self.keys_dir, f"{key_id}.json")

    @property
    def _active_path(self) -> str:
        return os.path.join(self.keys_dir, ACTIVE_POINTER_FILE)

    def _ensure_dir(self) -> None:
        try:
            os.makedirs(self.keys_dir, exist_ok=True)
            try:
                os.chmod(self.keys_dir, 0o700)
            except OSError:
                pass
        except OSError as exc:
            raise SigningKeyUnavailableError(
                "签名密钥目录不可用：无法创建或写入密钥目录",
                {"keys_dir": self.keys_dir, "reason": str(exc)},
            ) from exc

    def _load_key_file(self, path: str) -> StoredSigningKey:
        key_id = os.path.basename(path)[: -len(".json")]
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            private_key = serialization.load_pem_private_key(
                data["private_key_pem"].encode("ascii"), password=None
            )
            if not isinstance(private_key, Ed25519PrivateKey):
                raise ValueError("私钥不是 Ed25519 密钥")
            if data.get("key_id") != key_id:
                raise ValueError("密钥文件内的 key_id 与文件名不一致")
            return StoredSigningKey(
                key_id=key_id,
                created_at=str(data.get("created_at", "")),
                private_key=private_key,
                public_key=private_key.public_key(),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise SigningKeyUnavailableError(
                "签名密钥文件损坏或无法读取",
                {"keys_dir": self.keys_dir, "key_id": key_id, "reason": str(exc)},
            ) from exc

    def _scan(self) -> dict[str, StoredSigningKey]:
        keys: dict[str, StoredSigningKey] = {}
        try:
            names = sorted(os.listdir(self.keys_dir))
        except FileNotFoundError:
            return keys
        except OSError as exc:
            raise SigningKeyUnavailableError(
                "签名密钥目录不可读",
                {"keys_dir": self.keys_dir, "reason": str(exc)},
            ) from exc
        for name in names:
            if name.endswith(".json"):
                key = self._load_key_file(os.path.join(self.keys_dir, name))
                keys[key.key_id] = key
        return keys

    def _read_active_pointer(self) -> str | None:
        try:
            with open(self._active_path, encoding="ascii") as handle:
                value = handle.read().strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SigningKeyUnavailableError(
                "活跃密钥指针无法读取",
                {"keys_dir": self.keys_dir, "reason": str(exc)},
            ) from exc
        return value or None

    def _write_active_pointer(self, key_id: str) -> None:
        try:
            _atomic_write(self._active_path, key_id + "\n", mode=0o600)
        except OSError as exc:
            raise SigningKeyUnavailableError(
                "活跃密钥指针无法写入",
                {"keys_dir": self.keys_dir, "reason": str(exc)},
            ) from exc

    def _persist_key(self, key: StoredSigningKey) -> None:
        body = {
            "key_id": key.key_id,
            "algorithm": SIGNING_ALGORITHM,
            "jws_alg": JWS_ALG,
            "curve": CURVE,
            "created_at": key.created_at,
            "public_key_pem": key.public_key_pem,
            "private_key_pem": key.private_key_pem,
        }
        try:
            _atomic_write(
                self._key_path(key.key_id),
                json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True),
                mode=0o600,
            )
        except OSError as exc:
            raise SigningKeyUnavailableError(
                "新签名密钥无法持久化",
                {"keys_dir": self.keys_dir, "reason": str(exc)},
            ) from exc

    def _new_key(self) -> StoredSigningKey:
        private_key = Ed25519PrivateKey.generate()
        return StoredSigningKey(
            key_id=_generate_key_id(),
            created_at=now_canonical(),
            private_key=private_key,
            public_key=private_key.public_key(),
        )

    # -- public API -------------------------------------------------------

    def ensure_active_key(self) -> StoredSigningKey:
        """Return the active key, bootstrapping or repairing the store if needed.

        - no keys at all -> generate the genesis key and persist it;
        - keys exist but the active pointer is missing -> adopt the newest key;
        - the pointer names a missing key -> explicit 503 (never sign blindly);
        - the directory is not writable/readable -> explicit 503.
        """
        with self._lock:
            self._ensure_dir()
            keys = self._scan()
            active_id = self._read_active_pointer()
            if active_id is not None:
                key = keys.get(active_id)
                if key is None:
                    raise SigningKeyUnavailableError(
                        "活跃签名密钥缺失：active 指针指向的密钥文件不存在",
                        {"keys_dir": self.keys_dir, "active_key_id": active_id},
                    )
                return key
            if keys:
                newest = max(keys.values(), key=lambda k: (k.created_at, k.key_id))
                self._write_active_pointer(newest.key_id)
                return newest
            genesis = self._new_key()
            self._persist_key(genesis)
            self._write_active_pointer(genesis.key_id)
            return genesis

    def rotate(self) -> tuple[StoredSigningKey, str | None]:
        """Generate and activate a fresh key; previous keys stay on disk.

        Returns ``(new_key, previous_key_id)``. Existing proof packages keep
        their original signature and remain verifiable with the old public key.
        """
        with self._lock:
            self._ensure_dir()
            keys = self._scan()
            previous_id = self._read_active_pointer()
            if previous_id is not None and previous_id not in keys:
                raise SigningKeyUnavailableError(
                    "活跃签名密钥缺失，拒绝轮换到新密钥前丢失旧密钥",
                    {"keys_dir": self.keys_dir, "active_key_id": previous_id},
                )
            new_key = self._new_key()
            self._persist_key(new_key)
            self._write_active_pointer(new_key.key_id)
            return new_key, previous_id

    def get_public_key(self, key_id: str) -> StoredSigningKey | None:
        with self._lock:
            path = self._key_path(key_id)
            if not os.path.exists(path):
                return None
            return self._load_key_file(path)

    def active_key_id(self) -> str | None:
        with self._lock:
            if not os.path.isdir(self.keys_dir):
                return None
            return self._read_active_pointer()

    def list_public_keys(self) -> list[dict[str, object]]:
        """Return metadata for every retained key (newest first); no privates."""
        with self._lock:
            self._ensure_dir()
            keys = self._scan()
            active_id = self._read_active_pointer()
            ordered = sorted(
                keys.values(), key=lambda k: (k.created_at, k.key_id), reverse=True
            )
            return [key.public_metadata(active=key.key_id == active_id) for key in ordered]


# -- proof package construction ---------------------------------------------

# Fields covered by the signature. Any change to one of them — or the order of
# closure entries, basis entries, blocker entries or use lists — changes the
# canonical payload bytes and fails verification.
COVERED_FIELDS = [
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
]

ENCODING_RULES: dict[str, object] = {
    "format": "jws-eddsa-ed25519-v1",
    "jws_compact_serialization": "RFC 7515 第 7.1 节紧凑序列化（BASE64URL(UTF8(protected)).BASE64URL(payload).BASE64URL(signature)），Ed25519 按 RFC 8037（alg=EdDSA）",
    "protected_header_json": "UTF-8 JSON，键按字典序排序，紧凑分隔符（',' 与 ':'），非 ASCII 字符原样输出（ensure_ascii=false）",
    "payload_encoding": "UTF-8 JSON，键按字典序排序，紧凑分隔符（',' 与 ':'），非 ASCII 字符原样输出（ensure_ascii=false）；必须与包内 payload 对象重新序列化出的字节逐字节一致",
    "base64url": "RFC 4648 第 5 节 base64url，无填充（去掉结尾的 '='）",
    "signing_input": "ASCII(BASE64URL(UTF8(protected_header_json))) + '.' + BASE64URL(payload_json_utf8_bytes)；对其做 Ed25519 签名",
    "signature_scheme": "Ed25519（RFC 8032），确定性签名；JWS 头 alg 固定为 EdDSA（RFC 8037）",
    "timestamp_format": "UTC 字符串，格式 YYYY-MM-DDTHH:MM:SS.ffffffZ（微秒精度，如 2026-10-05T01:23:45.123456Z）",
    "covered_fields": COVERED_FIELDS,
}


def build_signed_proof(
    payload: dict[str, object],
    key: StoredSigningKey,
    *,
    public_key_url: str,
    public_keys_url: str,
) -> dict[str, object]:
    """Sign the canonical payload and assemble the full proof package."""
    protected_header = {"alg": JWS_ALG, "kid": key.key_id}
    protected_bytes = canonical_json_bytes(protected_header)
    payload_bytes = canonical_json_bytes(payload)
    protected_b64 = b64url_encode(protected_bytes)
    payload_b64 = b64url_encode(payload_bytes)
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    signature = key.private_key.sign(signing_input)
    signature_b64 = b64url_encode(signature)

    return {
        "format_version": FORMAT_VERSION,
        "voucher_id": str(payload["voucher_id"]),
        "payload": payload,
        "signature": {
            "algorithm": SIGNING_ALGORITHM,
            "jws_alg": JWS_ALG,
            "key_id": key.key_id,
            "protected_header": protected_header,
            "protected": protected_b64,
            "payload": payload_b64,
            "signature": signature_b64,
            "compact_serialization": f"{protected_b64}.{payload_b64}.{signature_b64}",
            "public_key_pem": key.public_key_pem,
            "public_key_jwk": public_key_jwk(key.public_key, key.key_id),
        },
        "verification": {
            "encoding": ENCODING_RULES,
            "public_key_url": public_key_url.format(key_id=key.key_id),
            "public_keys_url": public_keys_url,
        },
    }


def verify_proof_package(package: dict[str, object]) -> tuple[bool, str]:
    """Self-contained verification helper (also used by tests/examples).

    Rebuilds the canonical bytes from the package payload and verifies the
    Ed25519 signature with the public key carried in the package or supplied
    out of band. Returns ``(ok, message)``.
    """
    try:
        signature = package["signature"]  # type: ignore[index]
        assert isinstance(signature, dict)
        header = json.loads(
            b64url_decode(str(signature["protected"])).decode("utf-8")
        )
        if header.get("alg") != JWS_ALG:
            return False, f"不支持的签名算法：{header.get('alg')!r}，仅支持 EdDSA/Ed25519"
        kid = header.get("kid")
        if kid != signature.get("key_id"):
            return False, "保护头 kid 与签名密钥标识不一致"
        payload = package["payload"]
        rebuilt = canonical_json_bytes(payload)
        carried = b64url_decode(str(signature["payload"]))
        if rebuilt != carried:
            return False, "载荷编码与验签规则不一致（包内 payload 无法重建出已签名字节）"
        public_key = serialization.load_pem_public_key(
            str(signature["public_key_pem"]).encode("ascii")
        )
        if not isinstance(public_key, Ed25519PublicKey):
            return False, "公钥不是 Ed25519 密钥"
        verify_compact(
            str(signature["protected"]),
            str(signature["payload"]),
            str(signature["signature"]),
            public_key,
        )
    except (KeyError, ValueError, AssertionError, json.JSONDecodeError) as exc:
        return False, f"证明包格式非法：{exc}"
    except InvalidSignature:
        return False, "签名验签失败：签名与载荷/公钥不匹配（字段可能被篡改）"
    return True, f"验签通过（Ed25519/EdDSA，密钥 {kid}）"
