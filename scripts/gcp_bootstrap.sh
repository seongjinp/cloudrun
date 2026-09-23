#!/usr/bin/env bash
# 새 GCP 프로젝트에 이 게이트웨이를 처음부터 세운다. 여러 번 돌려도 안전하다(있는 것은 건너뛴다).
#
#   PROJECT=<프로젝트 ID> [REGION=asia-northeast3] [SERVICE=cloudrun] bash scripts/gcp_bootstrap.sh
#
# 전제: gcloud 로그인(`gcloud auth login`)이 끝났고, 프로젝트에 결제 계정이 연결돼 있고, 실행 계정이
# Owner다. 저장소 루트에서 실행한다. 절차 설명·함정은 docs/SETUP.md.
#
# 비밀값 두 개는 **없을 때만** 만든다:
#   LITELLM_MASTER_KEY        — 터미널에서 입력(화면에 안 보임). axagent backend/.env와 같은 값이어야 한다.
#   CRYPTO_PROXY_PRIVATE_KEY  — X25519 키쌍을 새로 만든다. 개인키는 Secret Manager로만 가고 공개키만 출력된다.
set -euo pipefail

: "${PROJECT:?PROJECT=<GCP 프로젝트 ID>를 지정한다}"
REGION="${REGION:-asia-northeast3}"
SERVICE="${SERVICE:-cloudrun}"
REPO="${REPO:-cloudrun}"
GCLOUD="$(command -v gcloud || echo "$HOME/google-cloud-sdk/bin/gcloud")"
g() { "$GCLOUD" --project="$PROJECT" --quiet "$@"; }

RUN_SA="litellm-gateway@$PROJECT.iam.gserviceaccount.com"
BUILD_SA="cloudbuild-builder@$PROJECT.iam.gserviceaccount.com"
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }

step "0. 사전 점검"
[ "$(g billing projects describe "$PROJECT" --format='value(billingEnabled)')" = "True" ] \
  || { echo "결제 계정이 연결돼 있지 않다 — 콘솔 > 결제에서 연결 후 재실행"; exit 1; }
python3 -c 'import cryptography' 2>/dev/null \
  || { echo "로컬 python3에 cryptography가 없다 — pip install cryptography"; exit 1; }
grep -q "\"VERTEX_AI_PROJECT\", \"$PROJECT\"" env.py \
  || echo "⚠ env.py의 VERTEX_AI_PROJECT가 $PROJECT가 아니다 — 서비스 env로 덮어쓰니 동작은 하지만 env.py도 고쳐 둔다"

step "1. API 활성화"
g services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com secretmanager.googleapis.com iam.googleapis.com

step "2. Artifact Registry 저장소"
g artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 \
  || g artifacts repositories create "$REPO" --repository-format=docker --location="$REGION"

step "3. 서비스 계정·역할"
ensure_sa() {
  g iam service-accounts describe "$1@$PROJECT.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || g iam service-accounts create "$1" --display-name="$2"
}
ensure_sa litellm-gateway "LiteLLM Cloud Run gateway"
ensure_sa cloudbuild-builder "Cloud Build builder"
for role in roles/aiplatform.user roles/secretmanager.secretAccessor roles/logging.logWriter; do
  g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUN_SA" --role="$role" --condition=None --format=none
done
# 새 프로젝트는 기본 Compute SA에 역할이 없어 빌드가 PERMISSION_DENIED로 막힌다 → 전용 빌드 SA.
g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$BUILD_SA" \
  --role=roles/cloudbuild.builds.builder --condition=None --format=none

step "4. Secret Manager"
if ! g secrets describe LITELLM_MASTER_KEY >/dev/null 2>&1; then
  read -rsp "LITELLM_MASTER_KEY (sk-로 시작, axagent backend/.env와 같은 값): " v; echo
  printf %s "$v" | g secrets create LITELLM_MASTER_KEY --data-file=- --replication-policy=automatic
  unset v
fi
if ! g secrets describe CRYPTO_PROXY_PRIVATE_KEY >/dev/null 2>&1; then
  tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
  python3 -c '
import base64, sys
sys.path.insert(0, ".")
from crypto_proxy.wire.keys import generate_keypair
priv, _ = generate_keypair()
open(sys.argv[1], "w").write(base64.b64encode(priv).decode())
' "$tmp"
  g secrets create CRYPTO_PROXY_PRIVATE_KEY --data-file="$tmp" --replication-policy=automatic
  rm -f "$tmp"
fi
# 공개키는 저장된 개인키에서 매번 다시 계산한다(형식 검증을 겸한다 — 임의 문자열이면 여기서 죽는다).
PUB="$(g secrets versions access latest --secret=CRYPTO_PROXY_PRIVATE_KEY | python3 -c '
import base64, sys
sys.path.insert(0, ".")
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
raw = base64.b64decode(sys.stdin.read().strip(), validate=True)
assert len(raw) == 32, "CRYPTO_PROXY_PRIVATE_KEY가 X25519 raw 32B(base64 44자)가 아니다"
print(base64.b64encode(X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
')"

step "5. 이미지 빌드"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/litellm:$(git rev-parse --short HEAD)"
g builds submit . --region="$REGION" --config=cloudbuild.yaml --substitutions="_IMAGE=$IMAGE"

step "6. Cloud Run 배포"
# NUM_WORKERS=1은 명시한다 — 1보다 크면 proxy_main의 기동 단정이 거부한다.
# VERTEX_AI_PROJECT를 서비스 env로도 준다 — env.py 기본값이 옛 프로젝트여도 이 값이 이긴다(setdefault).
g run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --platform=managed \
  --service-account="$RUN_SA" \
  --set-secrets=LITELLM_MASTER_KEY=LITELLM_MASTER_KEY:latest,CRYPTO_PROXY_PRIVATE_KEY=CRYPTO_PROXY_PRIVATE_KEY:latest \
  --set-env-vars="NUM_WORKERS=1,VERTEX_AI_PROJECT=$PROJECT" \
  --memory=2Gi --cpu=1 --timeout=3600

step "7. 공개 호출 허용"
# axagent 터널은 Google ID 토큰 없이 부른다 → allUsers invoker가 없으면 전부 403.
# 보호는 litellm 마스터키 + 봉인 계층이 한다(평문 모델 호출은 게이트웨이가 403으로 거부).
g run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member=allUsers --role=roles/run.invoker --format=none

URL="$(g run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"

step "8. 봉인 E2E 점검"
python3 scripts/smoke_test.py --url "$URL" --public-key "$PUB"

cat <<EOF

== 완료. axagent(~/axagent)에 반영할 값:
  crypto_proxy/.env
    CRYPTO_PROXY_GATEWAY_URL=$URL
    CRYPTO_PROXY_GATEWAY_PUBLIC_KEY=$PUB
  backend/.env
    AGENT_GATEWAY_URL=http://127.0.0.1:<CRYPTO_PROXY_PORT>   # 터널 경유(직결하면 403)
    LITELLM_MASTER_KEY=<Secret Manager LITELLM_MASTER_KEY와 같은 값>
  그 다음 axagent에서 make crypto → backend 재기동.
EOF
