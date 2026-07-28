# gr-lgcaip-project-fab0704-backend — LiteLLM 게이트웨이 (private-dev)

`main` 브랜치는 사내 GitLab CI/CD 기준 구성(`.gitlab-ci.yml`, base/app 2단계 빌드, 사내
Artifact Registry, Prisma DB 연동)을 그대로 유지한다. 이 `private-dev` 브랜치는 **개인 GCP
프로젝트 + GitHub 연동 Cloud Run**에서 돌리기 위해 사내 전용 의존성을 걷어낸 버전이다.

## main 대비 달라진 점

- **base image**: 사내 Artifact Registry(`asia-northeast3-docker.pkg.dev/pjt-prd-lgcaip-cicd/...`)
  대신 공개 `python:3.12.11-slim`을 직접 사용. 개인 GCP 계정은 사내 레지스트리에 대한 IAM 권한이
  없어 pull 단계에서 403으로 막힌다(로컬 재현 확인됨) — 이게 원래 빌드 에러의 원인이었다.
- **빌드 구조**: `dockerfile-baseimage` + `dockerfile` 2단계 분리를 걷어내고 `dockerfile` 하나로
  통합. 2단계 분리는 사내 CI의 빌드시간 최적화(base image 캐싱)용이었는데, 개인 개발환경에서는
  이 이점보다 단순함이 더 중요하다고 판단.
- **DB 연동 제거**: virtual key/budget/spend-log 기능(`litellm[proxy,extra_proxy]` + Prisma +
  Postgres)이 개인 개발환경에서는 불필요해 `extra_proxy`를 뺐다. 따라서 `vendor/prisma-engines/`
  (사내망에서 `binaries.prisma.sh`가 막혀 있어 vendoring했던 Prisma 엔진 바이너리)도 삭제.
  `STORE_MODEL_IN_DB=False`라 `DATABASE_URL` 없이도 프록시가 기동한다. 나중에 필요해지면
  `requirements.txt`에 `extra_proxy`를 다시 추가하고 Postgres(Cloud SQL 등)를 준비하면 된다.
- **CI 설정 제거**: `.gitlab-ci.yml`은 GitHub 저장소를 Cloud Run에 직접 연동하는 이 구성에서는
  전혀 읽히지 않는 사내 GitLab 전용 파일이라 삭제. 배포는 Cloud Run의 "리포지토리에서 지속적
  배포" 기능(Cloud Build가 push마다 자동으로 Dockerfile 빌드·배포)에 맡긴다.
- **qwen 모델 제거**: `config.yaml`의 `qwen-3.5`는 사내망 전용 vLLM(`10.36.114.31`, VPC 내부
  IP)을 가리켜 개인환경에서는 도달 불가능해 제거. 대신 `gemini-3.6-flash`를 기본 모델
  (`default: true`)로 지정.
- **`env.py`**: `VERTEX_AI_PROJECT`를 개인 GCP 프로젝트 ID(`project-b70f2ac9-1f7b-4489-bd6`)로
  교체.

## 구조

- `requirements.txt` — `litellm[proxy]==1.93.0` 단일 의존성.
- `dockerfile` — `python:3.12.11-slim` 위에 의존성 설치 + `proxy_main.py`/`config.yaml`/`env.py`를
  얹는 단일 스테이지 앱 이미지. 비루트 사용자(`appuser`)로 기동.
- `proxy_main.py` — LiteLLM 프록시 진입점 + 런타임 몽키패치(도구 이름 정규화 등, 사내 vLLM 연동
  실측 기반 — hosted_vllm 모델이 없으면 그냥 아무 일도 하지 않는다). 자세한 배경은 파일 상단
  docstring 참고.
- `config.yaml` — 모델 카탈로그(vertex_ai Claude/Gemini). **모델 카탈로그의 단일 출처는 이
  파일**이다(`STORE_MODEL_IN_DB=False`).
- `env.py` — 비민감 기본값(GCP 프로젝트·리전·로그 경로 등)만 채우는 폴백. `LITELLM_MASTER_KEY`
  같은 민감값은 여기 두지 않고 Cloud Run 콘솔(또는 `gcloud run deploy --set-env-vars`/Secret
  Manager)로 직접 주입한다.
- `scripts/add_litellm_users.py` — 엑셀로 LiteLLM 사용자 계정을 일괄 등록하는 수동 실행용 스크립트
  (CI/CD 미연동, 변경 없음). 사용법은 `scripts/README.md` 참고.

## Cloud Run 배포 전 체크리스트

1. **GCP 프로젝트 준비** (`project-b70f2ac9-1f7b-4489-bd6`)
   - Vertex AI API(`aiplatform.googleapis.com`) 활성화.
   - Vertex AI Model Garden에서 사용할 Claude/Gemini 모델 활성화(승인 필요한 모델도 있음).
   - Cloud Run 서비스에 붙는 서비스 계정에 `roles/aiplatform.user` 부여(Cloud Run은 별도 키
     파일 없이 이 서비스 계정으로 Vertex AI를 호출하는 ADC를 자동으로 쓴다).
2. **Cloud Run 지속적 배포 트리거 설정**
   - 소스 브랜치를 `private-dev`로 지정(또는 원하는 브랜치로).
   - Build Type: Dockerfile, 경로는 리포 루트의 `dockerfile`(소문자 — macOS 대소문자 미구분
     파일시스템 때문에 `Dockerfile`로 못 바꿨다. 트리거 설정 화면에서 파일명을 정확히
     `dockerfile`로 지정해야 함).
3. **Cloud Run 환경변수**
   - `LITELLM_MASTER_KEY` — 필수(콘솔의 "변수 및 보안 비밀" 또는 Secret Manager로 등록 권장).
   - `DATABASE_URL`, `LLM_API_KEY_QWEN`은 더 이상 필요 없음(각각 DB 미사용·qwen 모델 제거).

## litellm 버전 업그레이드 시 체크리스트

1. `requirements.txt`의 `litellm[proxy]==X.Y.Z` 갱신.
2. `proxy_main.py`의 몽키패치가 새 버전에서도 유효한지 재검증(내부 클래스/메서드 시그니처 의존).
