"""골든 벡터 자가검증 — 양쪽이 부팅 시 부른다.

**테스트 파일이 아니라 런타임 함수인 이유:** 수신측 사본이 사는 `cloudrun` 저장소에는 테스트
러너가 없다(pyproject·tests·CI 전부 부재, 2026-08-31 실측). 돌지 않는 테스트는 가드가 아니므로
사본 드리프트의 집행을 **부팅 거부**로 옮긴다 — 러너 유무와 무관하게 배포 직후 드러난다.
"""

from __future__ import annotations

import json
from pathlib import Path

from crypto_proxy import RESP_SALT_LEN
from crypto_proxy.wire import envelope as E
from crypto_proxy.wire.errors import WireError

VECTORS_PATH = Path(__file__).with_name("wire") / "testdata" / "vectors.json"


class VectorMismatch(WireError):
    """골든 벡터와 이 구현이 어긋났다 — 사본이 갈렸다는 뜻이다."""


def verify_vectors(path: Path | None = None) -> None:
    data = json.loads((path or VECTORS_PATH).read_text(encoding="utf-8"))
    gw_priv = bytes.fromhex(data["gateway_private"])
    gw_pub = bytes.fromhex(data["gateway_public"])
    kid = bytes.fromhex(data["kid"])
    eph_priv = bytes.fromhex(data["ephemeral_private"])
    salt = bytes.fromhex(data["resp_salt"])
    meta = E.RequestMeta(
        ts=data["meta"]["ts"],
        method=data["meta"]["method"],
        path=data["meta"]["path"],
        query=data["meta"]["query"],
        headers=[(k, v) for k, v in data["meta"]["headers"]],
    )
    body = bytes.fromhex(data["body"])

    if E.RESP_HEADER_LEN != 5 + RESP_SALT_LEN:
        raise VectorMismatch("응답 헤더 길이 상수가 계약과 다르다")
    if E.seal_response_header(salt).hex() != data["response_header"]:
        raise VectorMismatch("응답 헤더 바이트가 골든 벡터와 다르다")

    blob, _ = E.seal_request(gw_pub, kid, meta, body, resp_salt=salt, eph_priv=eph_priv)
    if blob.hex() != data["sealed_request"]:
        raise VectorMismatch("요청 봉투 바이트가 골든 벡터와 다르다")

    got_meta, got_body, _ = E.open_request({kid: gw_priv}, blob, resp_salt=salt)
    if got_meta != meta or got_body != body:
        raise VectorMismatch("왕복이 원본과 다르다")
