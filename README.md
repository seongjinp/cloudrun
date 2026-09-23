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
  IP)을 가리켜 개인환경에서는 도달 불가능해 제거. 대신 `gemini-3.8-flash-medium`을 기본 모델
  (`default: true`)로 지정(2026-09-03 3.7 → 3.8 전환).
- **`env.py`**: `VERTEX_AI_PROJECT`를 개인 GCP 프로젝트 ID(`project-7f9a422c-5de1-4d8a-856`)로
  교체(2026-09-23 GCP 계정 변경으로 `project-b70f2ac9-…`에서 이전).
- **Claude 모델 제거**(2026-09-23): 새 계정에서는 Vertex Model Garden의 Claude를 쓸 수 없어
  `claude-sonnet-5`·`claude-opus-4-8` 엔트리를 뺐다. Gemini만 남는다.

## 구조

- `requirements.txt` — `litellm[proxy]==1.98.0` 단일 의존성.
- `dockerfile` — `python:3.12.11-slim` 위에 의존성 설치 + `proxy_main.py`/`config.yaml`/`env.py`를
  얹는 단일 스테이지 앱 이미지. 비루트 사용자(`appuser`)로 기동.
- `proxy_main.py` — LiteLLM 프록시 진입점 + 런타임 몽키패치(도구 이름 정규화 등, 사내 vLLM 연동
  실측 기반 — hosted_vllm 모델이 없으면 그냥 아무 일도 하지 않는다). 자세한 배경은 파일 상단
  docstring 참고.
- `config.yaml` — 모델 카탈로그(vertex_ai Gemini). **모델 카탈로그의 단일 출처는 이
  파일**이다(`STORE_MODEL_IN_DB=False`).
  **엔트리 수 ≠ 소비자(axagent) 피커 항목 수다**(2026-08-02 2축 재설계): `model_info.family`가 같은
  엔트리들은 피커에 한 줄로 접히고 그 차이가 **추론 수준**이 된다 — 현재 15엔트리 = 제품 모델 7종
  (`gemini-3.5-flash-lite`·`gemini-3.6-flash`·`gemini-3.7-flash`·`gemini-3.8-flash` 각각
  low/medium/high 3변형이 한 줄씩). 자기선언 필드(`family`·`family_label`·
  `reasoning_transport`·`reasoning_level(s)`) 계약은 파일 상단 주석에 있다. 이 파일은 **배포의
  기술적 사실만** 소유하고, 접근 권한과 피커 설명 문구는 axagent admin > 모델 탭(DB)이 소유한다
  (그래서 옛 `description` 키는 제거됐다).
- `env.py` — 비민감 기본값(GCP 프로젝트·리전·로그 경로 등)만 채우는 폴백. `LITELLM_MASTER_KEY`
  같은 민감값은 여기 두지 않고 Cloud Run 콘솔(또는 `gcloud run deploy --set-env-vars`/Secret
  Manager)로 직접 주입한다.
- `scripts/gcp_bootstrap.sh` — 새 GCP 프로젝트 전체 세팅(재실행 안전). `scripts/smoke_test.py` — 봉인 E2E 점검.
- `cloudbuild.yaml` — 이미지 빌드 정의(소문자 `dockerfile` · 전용 빌드 SA).
- `scripts/add_litellm_users.py` — 엑셀로 LiteLLM 사용자 계정을 일괄 등록하는 수동 실행용 스크립트
  (CI/CD 미연동, 변경 없음). 사용법은 `scripts/README.md` 참고.

## GCP 구성 (2026-09-23 새 계정으로 재구축)

**처음부터 다시 세울 때는 [docs/SETUP.md](docs/SETUP.md)** — `scripts/gcp_bootstrap.sh` 한 번 + axagent `.env` 두 파일.

| 항목 | 값 |
| --- | --- |
| 프로젝트 | `project-7f9a422c-5de1-4d8a-856` |
| 리전 | `asia-northeast3`(서울) |
| 이미지 저장소 | Artifact Registry `asia-northeast3-docker.pkg.dev/project-7f9a422c-5de1-4d8a-856/cloudrun/litellm` |
| Cloud Run 서비스 | `cloudrun` — `https://cloudrun-164768579030.asia-northeast3.run.app` (`allUsers` invoker — axagent 터널은 ID 토큰 없이 부른다) |
| 런타임 SA | `litellm-gateway@…` — `roles/aiplatform.user`·`roles/secretmanager.secretAccessor`·`roles/logging.logWriter` |
| 빌드 SA | `cloudbuild-builder@…` — `roles/cloudbuild.builds.builder`(`cloudbuild.yaml`이 지정) |
| Secret Manager | `LITELLM_MASTER_KEY`, `CRYPTO_PROXY_PRIVATE_KEY` → 서비스 env로 `:latest` 마운트 |
| 봉인 공개키 | `hZVLUiyOpE4MiwXcDzoxB9T0c0Fa3FCLwteP0VQ5VFA=` (kid `aa26803c48489af8`) — axagent 쪽에 설정 |

재구축할 때 활성화할 API: `run`, `cloudbuild`, `artifactregistry`, `aiplatform`, `secretmanager`, `iam`.
Container Registry(`gcr.io`)는 종료돼서 새 프로젝트에서는 쓸 수 없다. 옛 README의 `gcr.io/...` 경로는 쓰지 않는다.

`CRYPTO_PROXY_PRIVATE_KEY`는 **X25519 raw 32B의 base64(44자)**여야 한다. 임의 문자열이면 부팅이
거부된다. 새로 만들 때는 `crypto_proxy.wire.keys.generate_keypair()`로 만들고, 개인키는 Secret
Manager에만, 공개키는 axagent에 둔다.

## 빌드·배포 (코드/config 변경 후 매번 수동)

```sh
TAG=$(git rev-parse --short HEAD)
IMAGE=asia-northeast3-docker.pkg.dev/project-7f9a422c-5de1-4d8a-856/cloudrun/litellm:$TAG

# 1. 이미지 빌드 → Artifact Registry push
gcloud builds submit . --region=asia-northeast3 --config=cloudbuild.yaml --substitutions=_IMAGE=$IMAGE

# 2. 새 리비전 배포 (env·secret·SA·리소스 설정은 기존 리비전 것이 유지된다)
gcloud run deploy cloudrun --image=$IMAGE --region=asia-northeast3
```

최초 생성 시에는 설정 전체를 준다(`NUM_WORKERS=1`은 명시한다 — 1보다 크면 부팅 단정이 거부한다):

```sh
gcloud run deploy cloudrun --image=$IMAGE --region=asia-northeast3 \
  --service-account=litellm-gateway@project-7f9a422c-5de1-4d8a-856.iam.gserviceaccount.com \
  --set-secrets=LITELLM_MASTER_KEY=LITELLM_MASTER_KEY:latest,CRYPTO_PROXY_PRIVATE_KEY=CRYPTO_PROXY_PRIVATE_KEY:latest \
  --set-env-vars=NUM_WORKERS=1 --memory=2Gi --cpu=1 --timeout=3600
```

## 봉인 터널 (crypto_proxy)

사내 운영 backend ↔ 이 게이트웨이 구간이 평문 HTTP라 보안 조직이 암호화를 권고했다. `proxy_main.py`의
**패치 ⑤**가 litellm 앱을 통째로 봉투 미들웨어로 감싸, `POST /_crypto`로 온 봉인 요청만 복호해
원래 scope로 재구성하고 응답을 다시 봉인해 스트리밍한다. 설계 정본은 axagent의
`docs/superpowers/specs/2026-08-31-crypto-proxy-payload-encryption-design.md`다.

**`crypto_proxy/`는 사본이다. 손으로 고치지 않는다.**

```bash
bash scripts/sync_crypto_proxy.sh     # 정본(axagent/crypto_proxy)에서 다시 복사
```

정본과 갈리면 **부팅이 거부된다** — `crypto_proxy/selftest.py`가 고정 키·고정 평문으로 봉투를 다시
구워 골든 벡터 바이트와 대조하고, 어긋나면 `VectorMismatch`로 죽는다. 이 저장소에는 테스트 러너가
없어서(pyproject·tests·CI 부재) 집행을 테스트가 아니라 **부팅**이 한다.

**개인키가 없으면 뜨지 않는다.** `CRYPTO_PROXY_PRIVATE_KEY`(X25519 raw 32B, base64)를 Cloud Run
서비스 env(Secret Manager)로 준다. 회전 중에는 `CRYPTO_PROXY_PRIVATE_KEY_PREV`를 함께 둘 수 있고
`kid`로 구분되므로 시행착오가 없다. **「키가 있을 때만 봉인」 같은 조건부로 만들지 않는다** — env 한
줄을 지우는 것만으로 조용히 평문으로 돌아가는 fail-open이 된다.

`NUM_WORKERS>1`이면 워커가 별도 프로세스로 떠서 이 파일을 안 거치고 패치 ①~⑤가 전부 사라지므로,
기동 단정이 그것도 거부한다.

**최초 전환은 양쪽이 동시에 바뀌어야 한다** — 어느 쪽을 먼저 바꿔도 그 사이 모델 트래픽이 전부
죽는다. 절차(리비전 태그 / 순차 전환)는 위 설계 §10.3이 소유한다.

## litellm 버전 업그레이드 시 체크리스트

1. `requirements.txt`의 `litellm[proxy]==X.Y.Z` 갱신.
2. `proxy_main.py`의 몽키패치가 새 버전에서도 유효한지 재검증(내부 클래스/메서드 시그니처 의존).
