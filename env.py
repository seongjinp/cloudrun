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
