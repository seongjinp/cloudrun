"""온프렘 ↔ 모델 게이트웨이 구간 payload 암호화.

`app`을 import하지 않는 standalone leaf다(import-linter 계약). 여기 상수는 **양쪽이 공유하는
와이어 계약**이라 env로 열지 않는다 — 열면 양쪽을 다르게 설정해 버전 일치 검사를 무력화할 수 있다.
"""

WIRE_VERSION = 1
MAX_FRAME_PLAINTEXT = 65536
KID_LEN = 8
RESP_SALT_LEN = 16
