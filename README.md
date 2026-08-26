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
  전혀 읽히지 않는 사내 GitLab 전용 파일이라 삭제. 빌드는 Cloud Run의 "리포지토리에서 지속적
  배포" 트리거(Cloud Build가 push마다 자동으로 Dockerfile 빌드·이미지 push)에 맡긴다 — 단 **실제
  Cloud Run 배포는 자동이 아니다**(2026-08-26 실측: 이 트리거는 이미지 빌드+push까지만 하고
  `gcloud run deploy`/리비전 갱신 스텝이 없다). push 후 새 리비전을 띄우려면 아래 "배포" 절차대로
  수동으로 `gcloud run deploy`를 실행해야 한다.
- **qwen 모델 제거**: `config.yaml`의 `qwen-3.5`는 사내망 전용 vLLM(`10.36.114.31`, VPC 내부
  IP)을 가리켜 개인환경에서는 도달 불가능해 제거. 대신 `gemini-3.7-flash-medium`을 기본 모델
  (`default: true`)로 지정(2026-08-26 3.6 → 3.7 전환).
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
  **엔트리 수 ≠ 소비자(axagent) 피커 항목 수다**(2026-08-02 2축 재설계): `model_info.family`가 같은
  엔트리들은 피커에 한 줄로 접히고 그 차이가 **추론 수준**이 된다 — 현재 12엔트리 = 제품 모델 6종
  (`gemini-3.5-flash-lite`·`gemini-3.6-flash`·`gemini-3.7-flash` 각각 low/medium/high 3변형이
  한 줄씩). 자기선언 필드(`family`·`family_label`·
  `reasoning_transport`·`reasoning_level(s)`) 계약은 파일 상단 주석에 있다. 이 파일은 **배포의
  기술적 사실만** 소유하고, 접근 권한과 피커 설명 문구는 axagent admin > 모델 탭(DB)이 소유한다
  (그래서 옛 `description` 키는 제거됐다).
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
   - 이 트리거는 이미지를 빌드해 `gcr.io/<프로젝트>/github.com/seongjinp/cloudrun:<커밋 SHA>`로
     push까지만 한다 — Cloud Run 리비전은 갱신하지 않는다(아래 "배포" 절차 참고).
3. **Cloud Run 환경변수**
   - `LITELLM_MASTER_KEY` — 필수(콘솔의 "변수 및 보안 비밀" 또는 Secret Manager로 등록 권장).
   - `DATABASE_URL`, `LLM_API_KEY_QWEN`은 더 이상 필요 없음(각각 DB 미사용·qwen 모델 제거).

## 배포(코드/config 변경 후 매번 수동)

`private-dev`에 push하면 Cloud Build가 이미지를 빌드해 Container Registry에 push하지만, 그
이미지로 Cloud Run 리비전을 실제로 띄우는 건 별도 수동 단계다:

```sh
# 1. push된 커밋의 Cloud Build가 성공했는지 확인 (이미지 태그 = 커밋 SHA)
gcloud builds list --limit=3 --sort-by=~createTime

# 2. 그 이미지로 새 리비전 배포
gcloud run deploy cloudrun \
  --image=gcr.io/project-b70f2ac9-1f7b-4489-bd6/github.com/seongjinp/cloudrun:<커밋 SHA> \
  --region=europe-west1 \
  --platform=managed
```

`--image`만 지정하면 기존 리비전의 환경변수·리소스 설정은 그대로 유지된다. 배포 후
`gcloud run services describe cloudrun --region=europe-west1`로 반영된 이미지를 확인할 것.

## litellm 버전 업그레이드 시 체크리스트

1. `requirements.txt`의 `litellm[proxy]==X.Y.Z` 갱신.
2. `proxy_main.py`의 몽키패치가 새 버전에서도 유효한지 재검증(내부 클래스/메서드 시그니처 의존).
