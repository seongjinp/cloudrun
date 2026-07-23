# gateway/Dockerfile — Cloud Run 배포용 LiteLLM 게이트웨이 이미지.
# =============================================================================
# 공식 LiteLLM 이미지(Prisma·마이그레이션 스크립트 내장) 위에 우리 런타임 패치(proxy_main.py)와
# Cloud 전용 모델 config를 얹는다. 로컬 dev는 이 이미지를 쓰지 않는다(uv 프로젝트 그대로) —
# 이 파일은 Cloud Run(및 cloudrun 배포 미러 repo) 전용이다.
#
# ⚠️ 베이스 태그는 gateway/pyproject.toml의 litellm 핀과 반드시 동일해야 한다(패치가 어댑터 내부
#    시그니처에 의존 — 버전 스큐 시 몽키패치가 조용히 빗나간다). 핀 = v1.93.0.
FROM ghcr.io/berriai/litellm:v1.93.0

WORKDIR /app

# vertex_ai 인증은 베이스 litellm 이미지가 이미 커버한다(vertex는 litellm 1급 프로바이더 —
# google-auth 코어 의존성 + REST 호출, 무거운 google-cloud-aiplatform SDK 불요). 베이스 이미지의
# python은 pip 없는 최소 venv(/app/.venv)라 추가 pip install이 불가하고, 실배포 실측상 불필요하다.
# 우리 런타임 패치 + Cloud config.
COPY proxy_main.py /app/gateway/proxy_main.py
COPY config.cloud.yaml /app/config.cloud.yaml

# 읽기전용 FS(Cloud Run)에서 진단 로거가 쓸 수 있는 유일한 경로 = /tmp (Task 2의 env 계약).
ENV GATEWAY_LOG_DIR=/tmp/gateway-log

# Cloud Run이 주입하는 $PORT를 그대로 따른다(이미지 기본 CMD의 --port 4000 하드코딩은 Click 옵션
# 우선순위상 PORT env보다 이겨서 무시된다 — 셸 형태 CMD로 실행 시점에 ${PORT} 치환해 회피).
# proxy_main.main()이 패치 적용 후 litellm.run_server()(click)를 sys.argv로 기동한다.
ENTRYPOINT []
CMD ["sh", "-c", "python /app/gateway/proxy_main.py --config /app/config.cloud.yaml --host 0.0.0.0 --port ${PORT:-4000}"]
