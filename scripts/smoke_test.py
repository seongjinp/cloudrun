"""배포된 게이트웨이를 axagent 터널과 같은 방식(봉인 요청 → POST /_crypto)으로 두드려 본다.

    python3 scripts/smoke_test.py --url https://<서비스>.run.app --public-key <base64> [모델 ...]

마스터키는 `LITELLM_MASTER_KEY` env가 있으면 그것을, 없으면 Secret Manager(`gcloud secrets versions
access latest --secret=LITELLM_MASTER_KEY`)에서 읽는다. 키 값은 출력하지 않는다.
서비스가 비공개(allUsers invoker 없음)여도 돌도록 `gcloud auth print-identity-token`을 바깥
Authorization에 싣는다 — 안쪽(봉인된) Authorization이 litellm 마스터키다.

로컬 의존성: `cryptography`(`pip install cryptography`). 저장소 루트에서 실행한다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from crypto_proxy import RESP_SALT_LEN  # noqa: E402
from crypto_proxy.wire import envelope as E  # noqa: E402
from crypto_proxy.wire.frames import FrameDecoder  # noqa: E402
from crypto_proxy.wire.keys import client_seal_keys, derive_kid  # noqa: E402


def _gcloud() -> str:
    return shutil.which("gcloud") or os.path.expanduser("~/google-cloud-sdk/bin/gcloud")


def _master_key() -> str:
    env = os.environ.get("LITELLM_MASTER_KEY", "").strip()
    if env:
        return env
    return subprocess.check_output(
        [_gcloud(), "secrets", "versions", "access", "latest", "--secret=LITELLM_MASTER_KEY"]
    ).decode().strip()


def _identity_token() -> str | None:
    try:
        return subprocess.check_output(
            [_gcloud(), "auth", "print-identity-token"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def sealed_call(url: str, pub: bytes, master: str, idtok: str | None, method: str, path: str,
                body: bytes = b"") -> tuple[int, bytes]:
    kid = derive_kid(pub)
    # 응답 키는 게이트웨이가 고른 resp_salt에 달려 있어, 응답 헤더를 받은 뒤 같은 임시키로 다시 유도한다.
    eph = X25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    meta = E.RequestMeta(
        ts=int(time.time()), method=method, path=path, query="",
        headers=[("authorization", f"Bearer {master}"), ("content-type", "application/json")],
    )
    blob, _ = E.seal_request(pub, kid, meta, body, resp_salt=b"\0" * RESP_SALT_LEN, eph_priv=eph)
    headers = {"Content-Type": "application/octet-stream"}
    if idtok:
        headers["Authorization"] = f"Bearer {idtok}"
    req = urllib.request.Request(url.rstrip("/") + E.CRYPTO_PATH, data=blob, method="POST", headers=headers)
    raw = urllib.request.urlopen(req, timeout=300).read()
    _, keys = client_seal_keys(pub, kid, E.parse_response_header(raw), eph_priv=eph)
    dec = FrameDecoder(keys.k_resp)
    plain = b"".join(dec.feed(raw[E.RESP_HEADER_LEN:]))
    dec.finish()
    rmeta, rbody = E.split_meta(plain)
    return int(rmeta["status"]), rbody


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--public-key", required=True, help="게이트웨이 공개키(X25519 raw 32B, base64)")
    ap.add_argument("models", nargs="*", default=["gemini-3.8-flash-medium"])
    args = ap.parse_args()

    pub = base64.b64decode(args.public_key, validate=True)
    master, idtok = _master_key(), _identity_token()
    ok = True
    try:
        st, body = sealed_call(args.url, pub, master, idtok, "GET", "/v1/models")
    except urllib.error.HTTPError as exc:
        print(f"FAIL /_crypto → HTTP {exc.code} (403이면 Cloud Run invoker 권한·kid 불일치를 본다)")
        return 1
    print(f"GET /v1/models → {st}", [m["id"] for m in json.loads(body)["data"]] if st == 200 else body[:300])
    ok &= st == 200
    for model in args.models:
        payload = {"model": model, "messages": [{"role": "user", "content": "한 단어로 대답: 1+1은?"}]}
        st, body = sealed_call(args.url, pub, master, idtok, "POST", "/v1/chat/completions",
                               json.dumps(payload).encode())
        j = json.loads(body)
        print(f"{model} → {st}", j["choices"][0]["message"]["content"][:80] if st == 200 else str(j)[:300])
        ok &= st == 200
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
