"""와이어 계층 예외.

**메시지에 평문·키·헤더 값을 싣지 않는다.** 이 예외는 양쪽에서 로그로 흘러가는데, 거기에 원문을
담으면 암호화 계층이 스스로 유출 경로가 된다.
"""


class WireError(Exception):
    """와이어 계층 실패의 최상위."""


class ProtocolError(WireError):
    """구조가 계약과 다르다 — 길이·매직·버전·플래그."""


class DecryptError(WireError):
    """AEAD 인증 실패 — 변조·재배열·키 불일치를 구분하지 않는다(구분하면 오라클이 된다)."""


class TruncatedStream(WireError):
    """FINAL 프레임 없이 스트림이 끝났다 — 잘린 답변이 정상으로 보이는 것을 막는다."""
