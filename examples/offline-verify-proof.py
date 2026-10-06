#!/usr/bin/env python3
"""离线核验用途判定凭证证明包（offline auditor example）。

本脚本只依赖 `cryptography`，不依赖本服务代码，可在完全离线的审计环境运行。
验签编码规则也写在证明包 `verification.encoding` 中，本脚本即按该规则实现：

    JWS 紧凑序列化（RFC 7515 §7.1）：BASE64URL(UTF8(protected))
                                    .BASE64URL(payload).BASE64URL(signature)
    Ed25519 按 RFC 8032 / JWS alg=EdDSA（RFC 8037）
    protected 与 payload 都是「UTF-8 JSON、键按字典序、紧凑分隔符」的规范编码

用法：

  1) 只验本地文件（完全离线；公钥用包内自带的 SPKI PEM）：
       python3 offline-verify-proof.py proof.json

  2) 用独立渠道取得的公钥文件（SPKI PEM）交叉核验：
       python3 offline-verify-proof.py proof.json --public-key key.pem

  3) 从运行中的服务拉取证明包与公钥（需要 urllib，无需第三方 HTTP 库）：
       python3 offline-verify-proof.py \\
           --server http://localhost:8000 --voucher-id approval-2026-03-research-001

退出码：验签通过 0；失败或参数错误 1。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from typing import Any


def b64url_decode(value: str) -> bytes:
    """RFC 4648 §5 base64url without padding."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_json_bytes(value: Any) -> bytes:
    """与服务端一致的规范 JSON：排序键、紧凑分隔、UTF-8、非 ASCII 原样。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def verify(package: dict[str, Any], public_pem: str | None = None) -> tuple[bool, str]:
    try:
        signature = package["signature"]
        protected_b64 = signature["protected"]
        payload_b64 = signature["payload"]
        signature_b64 = signature["signature"]

        header = json.loads(b64url_decode(protected_b64).decode("utf-8"))
        if header.get("alg") != "EdDSA":
            return False, f"不支持的 alg={header.get('alg')!r}（仅 EdDSA/Ed25519）"
        if header.get("kid") != signature.get("key_id"):
            return False, "保护头 kid 与签名 key_id 不一致"

        # The payload segment must equal the canonical re-encoding of the
        # payload object carried in the package: this is what binds every
        # covered field (version, use, decision time, registration time,
        # ancestors, basis, blockers and use intersection) to the signature.
        rebuilt_payload = canonical_json_bytes(package["payload"])
        signed_payload = b64url_decode(payload_b64)
        if rebuilt_payload != signed_payload:
            return False, "payload 重编码与已签名 payload 不一致（字段可能被篡改）"

        if public_pem is None:
            public_pem = signature["public_key_pem"]

        # Ed25519 verification without importing any application code.
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        public_key = load_pem_public_key(public_pem.encode("ascii"))
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        public_key.verify(b64url_decode(signature_b64), signing_input)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, f"证明包格式非法：{exc}"
    except Exception as exc:  # noqa: BLE001 - InvalidSignature and crypto errors
        name = type(exc).__name__
        if name == "InvalidSignature":
            return False, "验签失败：签名与载荷/公钥不匹配（字段可能被篡改）"
        return False, f"验签失败：{name}: {exc}"
    return True, f"验签通过（EdDSA/Ed25519，密钥 {header.get('kid')}）"


def fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _as_package(body: dict[str, Any]) -> dict[str, Any]:
    """Accept both the raw package and the export wrapper {"replayed","proof"}."""
    if "signature" in body and "payload" in body:
        return body
    if isinstance(body.get("proof"), dict):
        return body["proof"]
    raise ValueError("JSON 不是证明包（缺少 signature/payload 字段）")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线核验凭证证明包")
    parser.add_argument("proof_file", nargs="?", help="本地证明包 JSON 文件")
    parser.add_argument(
        "--server", help="服务 Base URL，如 http://localhost:8000（与 --voucher-id 同用）"
    )
    parser.add_argument("--voucher-id", help="要拉取的凭证标识")
    parser.add_argument(
        "--public-key",
        help="独立渠道取得的 SPKI PEM 公钥文件（不给定时使用包内自带公钥）",
    )
    args = parser.parse_args()

    if args.server and args.voucher_id:
        package = _as_package(
            fetch_json(
                f"{args.server.rstrip('/')}/permissions/vouchers/{args.voucher_id}/proof"
            )
        )
    elif args.proof_file:
        with open(args.proof_file, encoding="utf-8") as handle:
            package = _as_package(json.load(handle))
    else:
        parser.error("请提供本地证明包文件，或同时提供 --server 与 --voucher-id")
        return 1

    public_pem = None
    if args.public_key:
        with open(args.public_key, encoding="utf-8") as handle:
            public_pem = handle.read()

    ok, message = verify(package, public_pem)

    payload = package.get("payload", {})
    print(f"凭证标识     voucher_id      : {payload.get('voucher_id')}")
    print(f"版本         version_id      : {payload.get('version_id')}")
    print(f"用途         use             : {payload.get('use')}")
    print(f"判定时刻     at (UTC)        : {payload.get('at')}")
    print(f"登记时刻     registered_at   : {payload.get('registered_at')}")
    print(f"判定结果     allowed         : {payload.get('allowed')}")
    print(f"祖先闭包     closure         : {payload.get('closure')}")
    blockers = payload.get("blockers", [])
    print(f"阻断版本数   blockers        : {len(blockers)}")
    for blocker in blockers:
        print(f"    - {blocker.get('version_id')}: {blocker.get('reason')}")
    print(f"用途交集     intersection    : {payload.get('allowed_uses_intersection')}")
    sig = package.get("signature", {})
    print(f"签名算法     algorithm       : {sig.get('algorithm')} (JWS {sig.get('jws_alg')})")
    print(f"密钥标识     key_id          : {sig.get('key_id')}")
    print(f"JWS 紧凑串   compact         : {str(sig.get('compact_serialization', ''))[:80]}...")
    print("-" * 72)
    print(("✅ " if ok else "❌ ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
