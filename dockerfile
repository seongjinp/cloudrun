# private-dev — 개인 GCP 프로젝트 + GitHub 연동 Cloud Run 배포용 단일 스테이지 Dockerfile.
# 사내 GitLab CI(.gitlab-ci.yml, dockerfile-baseimage)의 base/app 2단계 분리·사내 Artifact
# Registry base image·Prisma DB 연동(extra_proxy)은 이 브랜치에서 걷어냈다 — virtual key/budget/
# spend-log 기능(Postgres 연동)이 개인 개발환경에서는 불필요하다는 판단.
FROM python:3.12.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade --no-cache-dir -r requirements.txt

COPY proxy_main.py config.yaml env.py ./

RUN useradd --create-home --uid 1000 appuser
USER appuser

# Cloud Run이 주입하는 $PORT를 그대로 따른다.
CMD ["sh", "-c", "python3 /app/proxy_main.py --config /app/config.yaml --host 0.0.0.0 --port ${PORT:-80}"]
