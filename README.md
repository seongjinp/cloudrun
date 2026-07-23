# gr-lgcaip-project-fab0704-backend — LiteLLM 게이트웨이

이 리포는 LiteLLM 멀티 모델 게이트웨이를 GCP Cloud Run(`DEV_RUN_NAME_BACKEND` 등)에 배포하는
사내 GitLab CI/CD 프로젝트다. `develop`/`master`/`poc` 브랜치에 push하면 `.gitlab-ci.yml`이
이미지를 빌드·push하고 해당 환경의 Cloud Run 서비스에 자동 배포한다.

## 구조

- `requirements.txt` — `litellm[proxy,extra_proxy]==1.93.0` 단일 의존성(`extra_proxy`는 virtual
  key·team·budget·spend log를 위한 Prisma DB 연동에 필요).
- `dockerfile-baseimage` — 2단계 빌드로 base image(`back-baseimage:latest`)를 만든다: 사내
  Artifact Registry의 `python:3.12.11` 위에 nodejs/npm + 위 의존성을 설치하고 `prisma generate`를
  실행해 DB 클라이언트를 미리 생성해둔다. `dockerfile-baseimage`나 `requirements.txt`가 바뀔 때만
  다시 빌드된다(`build-base-*` job 규칙).
- `dockerfile` — `back-baseimage` 위에 `proxy_main.py`/`config.yaml`/`env.py`만 얹는 앱 이미지.
- `proxy_main.py` — LiteLLM 프록시 진입점 + 런타임 몽키패치(도구 이름 정규화 등, 사내 vLLM 연동
  실측 기반). 자세한 배경은 파일 상단 docstring 참고.
- `config.yaml` — 모델 카탈로그(vertex_ai Claude/Gemini, 사내 vLLM qwen). **모델 카탈로그의
  단일 출처는 이 파일**이다(`STORE_MODEL_IN_DB=False`, DB는 virtual key/budget 전용).
- `env.py` — 비민감 기본값(리전·로그 경로 등)만 채우는 폴백. `LITELLM_MASTER_KEY`·`DATABASE_URL`·
  `LLM_API_KEY_QWEN` 같은 민감값은 여기 두지 않고 GitLab CI/CD Variables + `--set-env-vars`로
  Cloud Run에 주입한다.
- `vendor/prisma-engines/{schema-engine,query-engine}.gz` — Prisma 엔진 바이너리를 vendoring한
  것. 사내망에서 `binaries.prisma.sh`가 완전히 차단되어 있어(실측 확인) prisma CLI의 자체 다운로드가
  빌드·기동 양쪽에서 실패한다. `dockerfile-baseimage`가 이 파일을 풀어서
  `PRISMA_SCHEMA_ENGINE_BINARY`/`PRISMA_QUERY_ENGINE_BINARY` 환경변수로 직접 지정해 네트워크
  호출을 건너뛴다.

## 필요한 GitLab CI/CD Variables (Settings > CI/CD > Variables, `develop`/dev 기준)

기존(인프라 제공): `DEV_GCP_PROJECT`, `DEV_RUN_NAME_BACKEND`, `DEV_DOCKER_REGISTRY`,
`DEV_SERVICE_ACCOUNT_KEY`.

이 게이트웨이를 위해 새로 등록해야 하는 것(masked 권장):
- `DEV_LITELLM_MASTER_KEY` — 프록시 마스터 키
- `DEV_DATABASE_URL` — virtual key/budget/spend log용 PostgreSQL DSN
- `DEV_LLM_API_KEY_QWEN` — 사내 vLLM(qwen) API 키(없으면 빈 문자열)

`master`(prd)/`poc` 브랜치로 확장할 때는 동일한 이름 규칙(`PRD_*`/`POC_*`)으로 등록하고
`deploy-prd`/`deploy-poc` job에도 동일하게 `--set-env-vars`를 추가해야 한다(아직 미적용).

## litellm 버전 업그레이드 시 체크리스트

1. `requirements.txt`의 `litellm[proxy,extra_proxy]==X.Y.Z` 갱신.
2. `proxy_main.py`의 몽키패치가 새 버전에서도 유효한지 재검증(내부 클래스/메서드 시그니처 의존).
3. Prisma 엔진 버전이 바뀌었으면 `vendor/prisma-engines/`의 두 바이너리도 다시 받아와야 한다
   (사내망에서 직접 받을 수 없으므로 인터넷 되는 환경에서 받아서 교체 — 실패 시 빌드 로그의
   `binaries.prisma.sh/all_commits/<commit>/<platform>/<engine>.gz` 형태 URL을 확인).

## 네트워크 관련 참고 (사내 GitLab Runner 실측)

- 기본 apt 미러(`mirror.kakao.com`)가 응답하지 않아 `dockerfile-baseimage`에서 `deb.debian.org`로
  교체한다.
- `ghcr.io`, `hub.docker.com`, `nodejs.org`, `binaries.prisma.sh` 등 임의 CDN/컨테이너 레지스트리는
  차단되어 있고, PyPI(`pypi.org`/`files.pythonhosted.org`)와 npm(`registry.npmjs.org`), 사내
  Artifact Registry는 정상 동작한다.
