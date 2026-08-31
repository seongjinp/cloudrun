#!/usr/bin/env bash
# axagent의 crypto_proxy 정본에서 **수신측이 쓰는 부분만** 복사한다.
#
# 정본은 axagent다. 이 저장소의 사본을 손으로 고치지 않는다 — 고치면 골든 벡터가 어긋나
# 게이트웨이가 부팅을 거부한다(crypto_proxy/selftest.py). 그게 이 사본을 안전하게 만드는 기전이고,
# 이 저장소에 테스트 러너가 없기 때문에 집행을 부팅으로 옮긴 이유이기도 하다.
#
# client/·tools/·tests/·__main__.py는 온프렘 전용이라 싣지 않는다 — 이미지에 들어갈 이유가 없고,
# 들어가면 수신측이 필요로 하지 않는 의존성(httpx·uvicorn)까지 딸려 온다.
set -euo pipefail
src="${1:-$HOME/axagent/crypto_proxy}"
dst="$(cd "$(dirname "$0")/.." && pwd)/crypto_proxy"
[ -d "$src/wire" ] || { echo "✗ 정본을 못 찾았다: $src" >&2; exit 1; }
rm -rf "$dst"
mkdir -p "$dst"
cp "$src/__init__.py" "$src/selftest.py" "$dst/"
cp -r "$src/wire" "$src/gateway" "$dst/"
find "$dst" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "✓ crypto_proxy 사본 갱신 — $dst"
find "$dst" -name '*.py' -o -name '*.json' | sort | sed 's|^|  |'
