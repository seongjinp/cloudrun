"""X25519 키 합의 · HKDF 유도 · kid 파생.

**`kid`는 공개키에서 파생한다** — 별도 값이면 「공개키는 새 것인데 kid는 옛 것」이 가능하고 그건
진단이 고약한 부류의 버그다. 파생이면 불일치가 원리적으로 불가능하다.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from crypto_proxy import KID_LEN, RESP_SALT_LEN
from crypto_proxy.wire.errors import ProtocolError

__all__ = [
    "KID_LEN",
    "RESP_SALT_LEN",
    "ProtocolError",
    "SessionKeys",
    "client_seal_keys",
    "derive_kid",
    "gateway_open_keys",
    "generate_keypair",
]

_KEY_LEN = 32
_INFO_REQ = b"crypto_proxy/v1/req"
_INFO_RESP = b"crypto_proxy/v1/resp"


@dataclass(frozen=True, slots=True)
class SessionKeys:
    """한 요청·응답 쌍에만 쓰이는 대칭키 둘. 스트림 전용이라 nonce는 0부터 센다."""

    k_req: bytes
    k_resp: bytes


def _require_raw_key(value: bytes, what: str) -> bytes:
    if not isinstance(value, bytes | bytearray) or len(value) != _KEY_LEN:
        raise ProtocolError(f"{what}: X25519 raw 키는 32바이트여야 한다")
    return bytes(value)


def generate_keypair() -> tuple[bytes, bytes]:
    """(private_raw 32B, public_raw 32B)."""
    priv = X25519PrivateKey.generate()
    return (
        priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


def derive_kid(public_key_bytes: bytes) -> bytes:
    return hashlib.sha256(_require_raw_key(public_key_bytes, "public_key")).digest()[:KID_LEN]


def _expand(prk: bytes, info: bytes) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=_KEY_LEN, info=info).derive(prk)


def _session_keys(shared: bytes, kid: bytes, eph_pub: bytes, resp_salt: bytes) -> SessionKeys:
    if len(resp_salt) != RESP_SALT_LEN:
        raise ProtocolError("resp_salt 길이가 계약과 다르다")
    # HKDF-Extract = HMAC(salt, ikm). cryptography는 Extract를 따로 노출하지 않는다.
    prk = hmac.new(eph_pub + kid, shared, hashlib.sha256).digest()
    return SessionKeys(
        k_req=_expand(prk, _INFO_REQ),
        # 응답 키에 게이트웨이 난수를 섞는다 — 이게 없으면 같은 봉투를 두 번 처리할 때
        # (키, nonce)가 재사용되어 키스트림과 GHASH 서브키가 복구된다(설계 §5.2).
        k_resp=_expand(prk, _INFO_RESP + eph_pub + resp_salt),
    )


def client_seal_keys(
    gateway_pub: bytes,
    kid: bytes,
    resp_salt: bytes,
    *,
    eph_priv: bytes | None = None,
) -> tuple[bytes, SessionKeys]:
    """온프렘 측. `eph_priv`는 **전송 시도마다** 새로 뽑는다(인자는 테스트·골든 벡터 전용)."""
    _require_raw_key(gateway_pub, "gateway_pub")
    priv = (
        X25519PrivateKey.from_private_bytes(_require_raw_key(eph_priv, "eph_priv"))
        if eph_priv is not None
        else X25519PrivateKey.generate()
    )
    eph_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = priv.exchange(X25519PublicKey.from_public_bytes(gateway_pub))
    return eph_pub, _session_keys(shared, kid, eph_pub, resp_salt)


def gateway_open_keys(
    gateway_priv: bytes, kid: bytes, eph_pub: bytes, resp_salt: bytes
) -> SessionKeys:
    """수신측. 같은 `prk`가 나오므로 같은 두 키가 나온다."""
    priv = X25519PrivateKey.from_private_bytes(_require_raw_key(gateway_priv, "gateway_priv"))
    shared = priv.exchange(X25519PublicKey.from_public_bytes(_require_raw_key(eph_pub, "eph_pub")))
    return _session_keys(shared, kid, eph_pub, resp_salt)
