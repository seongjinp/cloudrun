"""env.py — 비민감 기본값만 채우는 환경변수 폴백.

`proxy_main.py`가 litellm을 import하기 **전에** 이 모듈을 가장 먼저 import해 os.environ을
채운다(litellm/__init__.py가 자기 import 시점에 LITELLM_MODE를 곧바로 읽어 dotenv 로드 여부를
결정하므로 순서가 중요하다 — 이 파일 import가 그보다 늦으면 의미가 없다).

LITELLM_MASTER_KEY 등 민감값은 여기 두지 않는다 — Cloud Run 콘솔(또는 `gcloud run deploy
--set-env-vars`/Secret Manager)에서 직접 주입한다. 이미 실제 env var가 설정돼 있으면
(`setdefault`) 그 값이 우선한다 — 이 파일은 "폴백"이지 "강제 오버라이드"가 아니다.
"""

import os

# ── 저민감값 — 개인 GCP 프로젝트에 맞게 교체 ────────────────────────────────────────
os.environ.setdefault("VERTEX_AI_PROJECT", "project-b70f2ac9-1f7b-4489-bd6")
os.environ.setdefault("VERTEX_AI_LOCATION", "global")

# ── 고정값 — 이 배포 구성에서 이미 확정된 값 — 보통 바꿀 필요 없음 ────────
os.environ.setdefault("STORE_MODEL_IN_DB", "False")
os.environ.setdefault("GATEWAY_LOG_DIR", "/tmp/gateway-log")
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_TELEMETRY", "False")
