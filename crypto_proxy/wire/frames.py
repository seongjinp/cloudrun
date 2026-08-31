"""STREAM 프레이밍 + AES-256-GCM.

    frame = flags(1) ‖ clen(4, BE) ‖ AESGCM(key, nonce=seq(12,BE), plaintext, aad)
            flags bit0 = is_final   ← 평문. 수신측이 **복호 전에** 알아야 한다
    aad   = version(1) ‖ flags(1)

`is_final`이 와이어에 있어야 하는 이유: AAD에만 두면 수신측이 복호 전에 알 수 없어 두 값으로 각각
복호를 시도해야 하고, 「태그 실패 = 변조」라는 해석이 무너져 절단 검출이 통째로 흔들린다. 평문
플래그로 내보내고 AAD로 인증하면 둘 다 성립한다.

`direction`·`seq`를 AAD에서 뺀 이유: 방향은 키가 이미 가르고(`K_req`≠`K_resp`), `seq`는 곧 nonce라
GCM이 이미 바인딩한다. 같은 값을 두 곳에 적으면 구현이 갈릴 지점만 늘어난다.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from crypto_proxy import MAX_FRAME_PLAINTEXT, WIRE_VERSION
from crypto_proxy.wire.errors import DecryptError, ProtocolError, TruncatedStream

FRAME_HEADER_LEN = 5
_TAG_LEN = 16
_FINAL_BIT = 0x01
_MAX_CIPHERTEXT = MAX_FRAME_PLAINTEXT + _TAG_LEN


def _aad(version: int, flags: int) -> bytes:
    return bytes([version, flags])


def _nonce(seq: int) -> bytes:
    return seq.to_bytes(12, "big")


def encode_frame(key: bytes, seq: int, plaintext: bytes, *, is_final: bool) -> bytes:
    if len(plaintext) > MAX_FRAME_PLAINTEXT:
        raise ProtocolError("프레임 평문이 상한을 넘는다")
    flags = _FINAL_BIT if is_final else 0
    ct = AESGCM(key).encrypt(_nonce(seq), plaintext, _aad(WIRE_VERSION, flags))
    return bytes([flags]) + len(ct).to_bytes(4, "big") + ct


class FrameDecoder:
    """청크가 프레임 경계와 무관하게 도착해도 되는 증분 디코더.

    `version` 인자는 다운그레이드 테스트 전용이다 — 실제 경로는 항상 `WIRE_VERSION`이다.
    """

    def __init__(self, key: bytes, *, version: int = WIRE_VERSION) -> None:
        self._aead = AESGCM(key)
        self._version = version
        self._buf = bytearray()
        self._seq = 0
        self.done = False
        self.seen_nonces: list[int] = []

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        out: list[bytes] = []
        while True:
            if self.done and self._buf:
                raise ProtocolError("FINAL 이후 프레임이 왔다 — 스트림 연장")
            if len(self._buf) < FRAME_HEADER_LEN:
                return out
            flags = self._buf[0]
            if flags & ~_FINAL_BIT:
                raise ProtocolError("예약 플래그 비트가 0이 아니다")
            clen = int.from_bytes(self._buf[1:FRAME_HEADER_LEN], "big")
            if clen > _MAX_CIPHERTEXT or clen < _TAG_LEN:
                # 할당 전에 거른다 — 선언 길이를 믿고 버퍼를 잡으면 DoS 표면이 된다.
                raise ProtocolError("선언된 프레임 길이가 계약 밖이다")
            if len(self._buf) < FRAME_HEADER_LEN + clen:
                return out
            ct = bytes(self._buf[FRAME_HEADER_LEN : FRAME_HEADER_LEN + clen])
            del self._buf[: FRAME_HEADER_LEN + clen]
            try:
                pt = self._aead.decrypt(_nonce(self._seq), ct, _aad(self._version, flags))
            except InvalidTag as exc:
                # 변조·재배열·키 불일치를 구분하지 않는다 — 구분하면 오라클이 된다.
                raise DecryptError("프레임 인증 실패") from exc
            self.seen_nonces.append(self._seq)
            self._seq += 1
            self.done = bool(flags & _FINAL_BIT)
            out.append(pt)

    def finish(self) -> None:
        if self._buf:
            raise ProtocolError("프레임 경계에서 끝나지 않았다")
        if not self.done:
            raise TruncatedStream("FINAL 프레임 없이 스트림이 끝났다")


def seal_stream(key: bytes, plaintext: bytes) -> bytes:
    out = bytearray()
    seq = 0
    view = memoryview(plaintext)
    while True:
        chunk = bytes(view[:MAX_FRAME_PLAINTEXT])
        view = view[MAX_FRAME_PLAINTEXT:]
        last = not view
        out += encode_frame(key, seq, chunk, is_final=last)
        seq += 1
        if last:
            return bytes(out)


def open_stream(key: bytes, blob: bytes) -> bytes:
    dec = FrameDecoder(key)
    parts = dec.feed(blob)
    dec.finish()
    return b"".join(parts)
