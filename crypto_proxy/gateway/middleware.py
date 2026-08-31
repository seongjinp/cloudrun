"""litellm 앱을 통째로 감싸는 최외곽 ASGI 미들웨어.

**`add_middleware()`가 아니라 앱을 감싼다.** `add_middleware`는 Starlette의 `ServerErrorMiddleware`
안쪽에 놓여, litellm이 처리 못 한 예외로 500을 낼 때 그 본문이 암호화를 우회해 평문으로 나간다.
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict

from crypto_proxy import RESP_SALT_LEN, WIRE_VERSION
from crypto_proxy.gateway.policy import PathPolicy
from crypto_proxy.wire import envelope as E
from crypto_proxy.wire.errors import WireError
from crypto_proxy.wire.frames import MAX_FRAME_PLAINTEXT, encode_frame
from crypto_proxy.wire.keys import KID_LEN


class _ReplayGuard:
    """`eph_pub` TTL LRU. **X25519 연산 전에** 검사한다 — 그 값은 평문 헤더에 있다.

    봉투 안의 값을 키로 쓰면 검사가 복호 뒤로 밀려 DoS 표면이 커진다. `eph_pub`은 지켜야 할 성질
    (임시키는 정확히 한 번만 쓰인다)과도 정확히 일치한다.
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._seen: OrderedDict[bytes, float] = OrderedDict()

    def check_and_record(self, eph_pub: bytes, now: float) -> bool:
        while self._seen and now - next(iter(self._seen.values())) > self._ttl:
            self._seen.popitem(last=False)
        if eph_pub in self._seen:
            return False
        self._seen[eph_pub] = now
        return True


class CryptoProxyMiddleware:
    def __init__(
        self,
        app,
        *,
        private_keys: dict[bytes, bytes],
        policy: PathPolicy,
        replay_ttl: float = 300.0,
        skew: float = 120.0,
    ) -> None:
        self._app = app
        self._keys = private_keys
        self._policy = policy
        self._skew = skew
        self._replay = _ReplayGuard(replay_ttl)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"]
        if self._policy.decide(path) == "plaintext":
            await self._app(scope, receive, send)
            return
        if path != E.CRYPTO_PATH:
            # 봉인 필수 경로에 평문이 왔거나(모델 트래픽 평문 시도) 아예 모르는 경로다.
            await self._fail(send, 403)
            return
        await self._tunnel(scope, receive, send)

    async def _fail(self, send, status: int) -> None:
        """평문 오류 — **인증되지 않는다.** 본문을 싣지 않고 내용도 알리지 않는다."""
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _tunnel(self, scope, receive, send) -> None:
        blob = b""
        while True:
            msg = await receive()
            blob += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        if len(blob) < E.REQ_HEADER_LEN or blob[:4] != E.REQ_MAGIC:
            await self._fail(send, 400)
            return

        now = time.time()
        eph_pub = blob[5 + KID_LEN : E.REQ_HEADER_LEN]
        if not self._replay.check_and_record(eph_pub, now):
            await self._fail(send, 409)
            return

        resp_salt = os.urandom(RESP_SALT_LEN)
        try:
            meta, body, keys = E.open_request(self._keys, blob, resp_salt=resp_salt)
        except WireError:
            await self._fail(send, 400)
            return
        if abs(now - meta.ts) > self._skew:
            await self._fail(send, 408)
            return

        inner = dict(scope)
        inner["method"] = meta.method
        inner["path"] = meta.path
        inner["raw_path"] = meta.path.encode()
        inner["query_string"] = meta.query.encode()
        inner["headers"] = [(k.lower().encode(), v.encode()) for k, v in meta.headers]

        await send(
            {
                "type": "http.response.start",
                "status": 200,  # 겉은 항상 200 — 진짜 상태는 봉투 안
                "headers": [
                    (b"content-type", b"application/octet-stream"),
                    (b"x-crypto-proxy-version", str(WIRE_VERSION).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": E.seal_response_header(resp_salt),
                "more_body": True,
            }
        )

        state = {"seq": 0}
        pending = bytearray()
        request_body = body

        body_delivered = False

        async def inner_receive():
            """본문을 **한 번만** 주고, 그 뒤로는 진짜 클라이언트 이벤트를 기다린다.

            매번 `http.request`를 즉시 돌려주면 안 된다 — Starlette `StreamingResponse`는
            `listen_for_disconnect`에서 `while True: await receive()`를 도는데, 즉시 반환하면
            그것이 **바쁜 대기**가 되어 이벤트 루프를 굶기고 `stream_response`가 영영 스케줄되지
            않는다(실측: 스트리밍 요청이 응답 헤더조차 못 내보내고 멈췄다. 비스트리밍은 그 루프가
            없어 멀쩡해서 유닛 테스트로는 안 잡혔다).

            바깥 `receive`로 위임하면 클라이언트가 끊을 때 `http.disconnect`가 그대로 전파된다.
            """
            nonlocal body_delivered
            if not body_delivered:
                body_delivered = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            return await receive()

        async def emit(payload: bytes, *, final: bool) -> None:
            offset = 0
            while True:
                chunk = payload[offset : offset + MAX_FRAME_PLAINTEXT]
                offset += MAX_FRAME_PLAINTEXT
                last = final and offset >= len(payload)
                await send(
                    {
                        "type": "http.response.body",
                        "body": encode_frame(keys.k_resp, state["seq"], chunk, is_final=last),
                        "more_body": not last,
                    }
                )
                state["seq"] += 1
                if offset >= len(payload):
                    return

        async def inner_send(message):
            if message["type"] == "http.response.start":
                pending.extend(
                    E.encode_meta(
                        E.ResponseMeta(
                            status=message["status"],
                            headers=[
                                (k.decode(), v.decode()) for k, v in message.get("headers", [])
                            ],
                        )
                    )
                )
                return
            if message["type"] != "http.response.body":
                return
            pending.extend(message.get("body", b""))
            more = message.get("more_body", False)
            payload = bytes(pending)
            pending.clear()
            await emit(payload, final=not more)

        await self._app(inner, inner_receive, inner_send)
