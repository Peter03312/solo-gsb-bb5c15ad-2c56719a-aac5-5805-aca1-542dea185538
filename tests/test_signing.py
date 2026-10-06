"""Unit tests for the local persistent signing-key store."""

from __future__ import annotations

import json
import os

import pytest

from app.signing import (
    JWS_ALG,
    SIGNING_ALGORITHM,
    KeyStore,
    SigningKeyUnavailableError,
    StoredSigningKey,
    b64url_decode,
    b64url_encode,
    canonical_json_bytes,
    build_signed_proof,
    verify_proof_package,
)
from cryptography.hazmat.primitives.serialization import load_pem_public_key


def _store(tmp_path, name="keys"):
    return KeyStore(str(tmp_path / name))


def test_genesis_key_is_created_persisted_and_marked_active(tmp_path):
    store = _store(tmp_path)
    key = store.ensure_active_key()
    assert os.path.isfile(store._key_path(key.key_id))
    assert open(store._active_path, encoding="ascii").read().strip() == key.key_id

    # A fresh store over the same directory loads the same key (stability).
    reopened = _store(tmp_path)
    again = reopened.ensure_active_key()
    assert again.key_id == key.key_id
    assert again.private_key.private_bytes_raw() == key.private_key.private_bytes_raw()

    # Metadata file holds PEMs, never exposes the private key via public listing.
    on_disk = json.load(open(store._key_path(key.key_id), encoding="utf-8"))
    assert "private_key_pem" in on_disk
    listed = store.list_public_keys()
    assert len(listed) == 1
    assert "private_key_pem" not in listed[0]
    assert listed[0]["active"] is True
    assert listed[0]["algorithm"] == SIGNING_ALGORITHM
    assert listed[0]["jws_alg"] == JWS_ALG


def test_rotate_retains_old_keys_and_switches_active_pointer(tmp_path):
    store = _store(tmp_path)
    first = store.ensure_active_key()
    new_key, previous = store.rotate()
    assert previous == first.key_id
    assert new_key.key_id != first.key_id
    assert store.active_key_id() == new_key.key_id

    # Both key files retained; listing reports exactly one active, newest first.
    keys = store.list_public_keys()
    assert {k["key_id"] for k in keys} == {first.key_id, new_key.key_id}
    assert [k["active"] for k in keys] == [True, False]
    assert keys[0]["key_id"] == new_key.key_id

    # The old public key remains fetchable by id.
    old = store.get_public_key(first.key_id)
    assert isinstance(old, StoredSigningKey)
    assert store.get_public_key("does-not-exist") is None


def test_old_proof_still_verifies_after_rotation(tmp_path):
    store = _store(tmp_path)
    old_key = store.ensure_active_key()
    payload = {"voucher_id": "v", "use": "research", "at": "2026-05-01T00:00:00.000000Z"}
    package = build_signed_proof(
        payload,
        old_key,
        public_key_url="/signing/keys/{key_id}/public",
        public_keys_url="/signing/keys",
    )
    store.rotate()

    # The package keeps the old kid and verifies with the retained old key.
    assert package["signature"]["key_id"] == old_key.key_id
    old_pem = store.get_public_key(old_key.key_id).public_key_pem  # type: ignore[union-attr]
    sig = package["signature"]
    load_pem_public_key(old_pem.encode()).verify(
        b64url_decode(sig["signature"]),
        f"{sig['protected']}.{sig['payload']}".encode(),
    )
    ok, message = verify_proof_package(package)
    assert ok, message


def test_rotation_second_gives_distinct_ids_and_repeats_keep_growing(tmp_path):
    store = _store(tmp_path)
    store.ensure_active_key()
    k2, prev2 = store.rotate()
    k3, prev3 = store.rotate()
    assert prev2 != k2.key_id
    assert prev3 == k2.key_id
    assert len(store.list_public_keys()) == 3
    assert k3.key_id not in {k2.key_id, prev2}


def test_unwritable_keys_directory_raises_explicit_unavailable(tmp_path, monkeypatch):
    # Point the store at a regular file used as a directory: makedirs fails.
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    store = KeyStore(str(blocker))
    with pytest.raises(SigningKeyUnavailableError) as exc:
        store.ensure_active_key()
    assert exc.value.status_code == 503
    assert exc.value.code == "signing_key_unavailable"

    # Rotation surfaces the same explicit error instead of crashing.
    with pytest.raises(SigningKeyUnavailableError):
        store.rotate()


def test_active_pointer_targeting_missing_key_is_explicit_unavailable(tmp_path):
    store = _store(tmp_path)
    key = store.ensure_active_key()
    # Simulate external deletion of the active key file.
    os.unlink(store._key_path(key.key_id))
    with pytest.raises(SigningKeyUnavailableError):
        KeyStore(store.keys_dir).ensure_active_key()
    with pytest.raises(SigningKeyUnavailableError):
        KeyStore(store.keys_dir).rotate()


def test_corrupt_key_file_is_explicit_unavailable(tmp_path):
    store = _store(tmp_path)
    key = store.ensure_active_key()
    with open(store._key_path(key.key_id), "w", encoding="utf-8") as handle:
        handle.write("{ not valid json")
    with pytest.raises(SigningKeyUnavailableError):
        KeyStore(store.keys_dir).ensure_active_key()


def test_pointer_repaired_when_keys_exist_but_pointer_missing(tmp_path):
    store = _store(tmp_path)
    key = store.ensure_active_key()
    os.unlink(store._active_path)
    recovered = KeyStore(store.keys_dir).ensure_active_key()
    assert recovered.key_id == key.key_id
    with open(store._active_path, encoding="ascii") as handle:
        assert handle.read().strip() == key.key_id


def test_canonical_encoding_is_deterministic(tmp_path):
    payload = {
        "b": ["y", "x"],
        "a": {"z": 1, "a": "研究"},
        "n": True,
    }
    one = canonical_json_bytes(payload)
    two = canonical_json_bytes(json.loads(json.dumps(payload)))
    assert one == two
    # Sorted keys, compact separators, non-ASCII emitted literally.
    assert one == '{"a":{"a":"研究","z":1},"b":["y","x"],"n":true}'.encode("utf-8")


def test_b64url_roundtrip_and_no_padding():
    raw = bytes(range(256)) * 3
    encoded = b64url_encode(raw)
    assert "=" not in encoded and "+" not in encoded and "/" not in encoded
    assert b64url_decode(encoded) == raw


def test_signed_proof_roundtrip_and_tamper_rejection(tmp_path):
    store = _store(tmp_path)
    key = store.ensure_active_key()
    package = build_signed_proof(
        {"voucher_id": "v1", "use": "u", "at": "2026-01-01T00:00:00.000000Z"},
        key,
        public_key_url="/signing/keys/{key_id}/public",
        public_keys_url="/signing/keys",
    )
    ok, _ = verify_proof_package(package)
    assert ok

    # Decode, mutate one covered field, re-encode the payload segment: fails.
    import copy

    tampered = copy.deepcopy(package)
    tampered["payload"]["use"] = "other"
    tampered["signature"]["payload"] = b64url_encode(
        canonical_json_bytes(tampered["payload"])
    )
    ok, message = verify_proof_package(tampered)
    assert ok is False
    assert "验签失败" in message
