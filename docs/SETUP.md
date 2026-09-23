# 새 GCP 계정/프로젝트에 게이트웨이 세우기

GCP 계정이 바뀌어 처음부터 다시 세울 때 쓰는 매뉴얼이다. 2026-09-23에 실제로 재구축하면서 막혔던
지점을 전부 반영했다. 대부분은 `scripts/gcp_bootstrap.sh` 한 번으로 끝난다. 사람이 해야 하는 일은
**0단계와 마지막 axagent 반영**뿐이다.

## 현재 구성 (2026-09-23)

| 항목 | 값 |
| --- | --- |
| 프로젝트 | `project-7f9a422c-5de1-4d8a-856` (번호 `164768579030`) |
| 리전 | `asia-northeast3`(서울) |
| 서비스 | `cloudrun` — `https://cloudrun-164768579030.asia-northeast3.run.app` |
| 이미지 | `asia-northeast3-docker.pkg.dev/project-7f9a422c-5de1-4d8a-856/cloudrun/litellm:<태그>` |
| 런타임 SA | `litellm-gateway@…` — `aiplatform.user`·`secretmanager.secretAccessor`·`logging.logWriter` |
| 빌드 SA | `cloudbuild-builder@…` — `cloudbuild.builds.builder` |
| 비밀값 | Secret Manager `LITELLM_MASTER_KEY`, `CRYPTO_PROXY_PRIVATE_KEY` |
| 봉인 공개키 | `hZVLUiyOpE4MiwXcDzoxB9T0c0Fa3FCLwteP0VQ5VFA=` (kid `aa26803c48489af8`) |
| 모델 | Gemini만(3.5-flash-lite·3.6·3.7·3.8-flash × low/medium/high). 이 계정은 Vertex Claude를 쓸 수 없다. |

## 0. 사람이 먼저 할 일

1. **프로젝트 생성 + 결제 계정 연결**(콘솔). 결제가 없으면 API 활성화부터 실패한다.
2. **Vertex AI Model Garden에서 쓸 모델 확인.** Gemini는 따로 켤 필요가 없다. Claude는 약관 동의가
   필요하고, 계정에 따라 아예 쓸 수 없을 수도 있다(현재 계정이 그렇다). 쓸 수 없는 모델은
   `config.yaml`에서 뺀다.
3. **gcloud 설치·로그인.** 개발 컨테이너(aarch64)에는 gcloud가 기본으로 없다.
   ```bash
   curl -fsSL https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-arm.tar.gz | tar -xz -C ~
   ~/google-cloud-sdk/install.sh --quiet --usage-reporting=false --path-update=true --command-completion=false
   ```
   (x86_64면 파일명이 `...-linux-x86_64.tar.gz`)

   로그인은 **별도 터미널**에서 한다. Claude Code의 `! 명령`은 표준입력이 없어서 인증 코드를 받는
   단계에서 `EOFError`로 죽는다.
   ```bash
   ~/google-cloud-sdk/bin/gcloud auth login --no-launch-browser
   ~/google-cloud-sdk/bin/gcloud auth application-default login --no-launch-browser
   ~/google-cloud-sdk/bin/gcloud config set project <프로젝트 ID>
   ~/google-cloud-sdk/bin/gcloud auth application-default set-quota-project <프로젝트 ID>
   ```
4. `env.py`의 `VERTEX_AI_PROJECT` 기본값을 새 프로젝트 ID로 바꾼다. 부트스트랩이 서비스 env로도
   넣어 주지만, 저장소의 기본값도 맞춰 둔다.

## 1. 부트스트랩 (별도 터미널에서)

```bash
cd ~/cloudrun
PROJECT=<프로젝트 ID> bash scripts/gcp_bootstrap.sh
```

단계는 사전 점검 → API → Artifact Registry → SA·역할 → Secret → 빌드 → 배포 → 공개 허용 → 봉인 E2E
순이다. 있는 것은 건너뛰니 중간에 실패해도 고치고 다시 돌리면 된다. 끝나면 axagent에 넣을 값이
출력된다.

- `LITELLM_MASTER_KEY`가 없으면 입력을 받는다. **axagent `backend/.env`의 값과 같아야 한다.** 그
  값을 그대로 쓰면 axagent 쪽은 건드릴 필요가 없다.
- `CRYPTO_PROXY_PRIVATE_KEY`가 없으면 새로 만든다. 개인키는 화면에 나오지 않고, 공개키만 출력된다.
- 7단계(`allUsers` invoker)는 Claude Code 자동 모드의 분류기가 막는다. 스크립트를 사람이 직접 돌리면
  문제없다.

## 2. axagent 반영 (`~/axagent`, 둘 다 git 미추적 파일)

```bash
# crypto_proxy/.env
CRYPTO_PROXY_GATEWAY_URL=<부트스트랩이 출력한 URL>
CRYPTO_PROXY_GATEWAY_PUBLIC_KEY=<부트스트랩이 출력한 공개키>

# backend/.env
AGENT_GATEWAY_URL=http://127.0.0.1:8788   # 터널 포트(CRYPTO_PROXY_PORT). 게이트웨이 URL로 직결하면 403
LITELLM_MASTER_KEY=<Secret Manager 값과 동일>
```

```bash
cd ~/axagent
make crypto-stop && make crypto   # 터널은 기동할 때만 .env를 읽는다 — 반드시 재시작
make status                        # crypto:8788 RUNNING ← healthz OK
grep 'crypto_proxy 기동' var/log/crypto-proxy.log | tail -1   # kid·upstream이 새 값인지
make start   # 또는 make dev — backend가 기동하면서 /model/info 카탈로그를 fail-loud로 검증한다
```

## 막혔던 지점과 해결 (재구축 때 그대로 반복된다)

| 증상 | 원인 | 해결 |
| --- | --- | --- |
| `gcloud builds submit` → `PERMISSION_DENIED: The caller does not have permission` (Owner인데도) | 새 프로젝트는 빌드가 기본 Compute SA로 도는데, 그 SA에 역할이 하나도 없다 | 전용 `cloudbuild-builder` SA + `roles/cloudbuild.builds.builder`, `cloudbuild.yaml`에서 지정 |
| `gcloud builds submit --tag`가 Dockerfile을 못 찾는다 | 파일명이 소문자 `dockerfile`이다 | `cloudbuild.yaml`(`-f dockerfile`)로 빌드 |
| 옛 README의 `gcr.io/...` 경로 | Container Registry가 종료됐다 | Artifact Registry `<리전>-docker.pkg.dev/<프로젝트>/cloudrun/litellm` |
| 부팅 시 `CRYPTO_PROXY_PRIVATE_KEY가 X25519 raw 개인키(base64)가 아니다` | 임의 문자열을 넣었다 | X25519 raw 32B의 base64(44자)여야 한다. `generate_keypair()`로 만든다(부트스트랩이 한다) |
| axagent 터널 → `502`, 터널 로그 `up=403 0B` | Cloud Run이 비공개라 ID 토큰 없는 호출을 Google 앞단이 403으로 막는다 | `gcloud run services add-iam-policy-binding cloudrun --region=<리전> --member=allUsers --role=roles/run.invoker` |
| 터널이 옛 upstream·kid로 계속 붙는다 | 터널은 기동할 때만 `crypto_proxy/.env`를 읽는다 | `make crypto-stop && make crypto` |
| `gcloud auth login`이 `EOFError` | Claude Code `!`는 표준입력이 없다 | 별도 터미널에서 로그인 |

## 점검

```bash
python3 scripts/smoke_test.py --url <서비스 URL> --public-key <공개키> gemini-3.8-flash-medium
```

axagent 터널과 같은 방식으로 요청을 봉인해 `/_crypto`로 보내고, `/v1/models`와 모델 호출이 200인지
본다. 서비스가 비공개여도 `gcloud auth print-identity-token`을 실어 보내니 통과한다. 따라서 이
점검이 통과해도 **공개 허용이 됐다는 뜻은 아니다.** 공개 허용은 IAM에서 확인한다.

```bash
gcloud run services get-iam-policy cloudrun --region=asia-northeast3   # members에 allUsers / roles/run.invoker
```

마지막으로 axagent 터널을 거쳐 확인한다(200이면 끝):

```bash
cd ~/axagent && K=$(grep -m1 '^LITELLM_MASTER_KEY=' backend/.env | cut -d= -f2-)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $K" http://127.0.0.1:8788/v1/models
```
