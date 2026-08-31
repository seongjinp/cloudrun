"""봉투 조립·해체.

    요청: magic(4)="CPX1" ‖ version(1) ‖ kid(8) ‖ eph_pub(32) ‖ frames…
    응답: magic(4)="CPR1" ‖ version(1) ‖ resp_salt(16) ‖ frames…
    평문: uint32(len(meta_json), BE) ‖ meta_json ‖ body

**JSON이고 varint가 아니다.** `cbor2`는 양쪽 저장소 어디에도 없어(§3이 새 의존성을 기각한 근거를
스스로 위반한다) 표준 라이브러리를 쓰고, uint32 BE는 두 구현이 갈릴 곳이 없다.

메타에 `nonce16`이 없다 — 재전송 방어의 키는 평문 헤더의 `eph_pub`이 맡는다(설계 §5.9). 그 값은
복호 **전에** 볼 수 있고 지켜야 할 성질(임시키 1회 사용)과 정확히 일치한다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from crypto_proxy import KID_LEN, RESP_SALT_LEN, WIRE_VERSION
from crypto_proxy.wire.errors import ProtocolError
from crypto_proxy.wire.frames import open_stream, seal_stream
from crypto_proxy.wire.keys import SessionKeys, client_seal_keys, gateway_open_keys

REQ_MAGIC = b"CPX1"
RESP_MAGIC = b"CPR1"
REQ_HEADER_LEN = 4 + 1 + KID_LEN + 32
RESP_HEADER_LEN = 4 + 1 + RESP_SALT_LEN
CRYPTO_PATH = "/_crypto"


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ts: int
    method: str
    path: str
    query: str
    headers: list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class ResponseMeta:
    status: int
    headers: list[tuple[str, str]]


def encode_meta(meta: RequestMeta | ResponseMeta) -> bytes:
    raw = json.dumps(asdict(meta), separators=(",", ":"), ensure_ascii=False).encode()
    return len(raw).to_bytes(4, "big") + raw


def split_meta(plaintext: bytes) -> tuple[dict, bytes]:
    if len(plaintext) < 4:
        raise ProtocolError("메타 길이 필드가 없다")
    size = int.from_bytes(plaintext[:4], "big")
    if len(plaintext) < 4 + size:
        raise ProtocolError("메타가 선언 길이보다 짧다")
    try:
        meta = json.loads(plaintext[4 : 4 + size])
    except ValueError as exc:
        raise ProtocolError("메타가 JSON이 아니다") from exc
    if not isinstance(meta, dict):
        raise ProtocolError("메타가 객체가 아니다")
    return meta, plaintext[4 + size :]


def seal_request(
    gateway_pub: bytes,
    kid: bytes,
    meta: RequestMeta,
    body: bytes,
    *,
    resp_salt: bytes,
    eph_priv: bytes | None = None,
) -> tuple[bytes, SessionKeys]:
    eph_pub, keys = client_seal_keys(gateway_pub, kid, resp_salt, eph_priv=eph_priv)
    header = REQ_MAGIC + bytes([WIRE_VERSION]) + kid + eph_pub
    return header + seal_stream(keys.k_req, encode_meta(meta) + body), keys


def open_request(
    gateway_privs: dict[bytes, bytes], blob: bytes, *, resp_salt: bytes
) -> tuple[RequestMeta, bytes, SessionKeys]:
    if len(blob) < REQ_HEADER_LEN or blob[:4] != REQ_MAGIC:
        raise ProtocolError("요청 봉투 헤더가 계약과 다르다")
    if blob[4] != WIRE_VERSION:
        raise ProtocolError("와이어 버전 불일치")
    kid = blob[5 : 5 + KID_LEN]
    priv = gateway_privs.get(kid)
    if priv is None:
        raise ProtocolError("알 수 없는 kid")
    eph_pub = blob[5 + KID_LEN : REQ_HEADER_LEN]
    keys = gateway_open_keys(priv, kid, eph_pub, resp_salt)
    meta_dict, body = split_meta(open_stream(keys.k_req, blob[REQ_HEADER_LEN:]))
    try:
        meta = RequestMeta(
            ts=int(meta_dict["ts"]),
            method=str(meta_dict["method"]),
            path=str(meta_dict["path"]),
            query=str(meta_dict["query"]),
            headers=[(str(k), str(v)) for k, v in meta_dict["headers"]],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError("요청 메타 형태가 계약과 다르다") from exc
    return meta, body, keys


def seal_response_header(resp_salt: bytes) -> bytes:
    if len(resp_salt) != RESP_SALT_LEN:
        raise ProtocolError("resp_salt 길이가 계약과 다르다")
    return RESP_MAGIC + bytes([WIRE_VERSION]) + resp_salt


def parse_response_header(blob: bytes) -> bytes:
    if len(blob) < RESP_HEADER_LEN or blob[:4] != RESP_MAGIC:
        raise ProtocolError("응답 봉투 헤더가 계약과 다르다")
    if blob[4] != WIRE_VERSION:
        raise ProtocolError("와이어 버전 불일치")
    return blob[5:RESP_HEADER_LEN]
