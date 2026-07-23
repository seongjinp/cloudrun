# add_litellm_users.py — LiteLLM 사용자 계정 일괄 등록

엑셀 파일에 계정 정보를 채워 넣고 실행하면 LiteLLM 프록시(`config.yaml`이 가리키는 Admin API)에
사용자 계정을 등록하는 수동 실행용 스크립트다. CI/CD에는 엮여 있지 않다 — 사람이 로컬에서 직접
돌린다.

DB(Postgres/Prisma)에 직접 쓰지 않고 LiteLLM Admin API(`/user/new`, `/user/update`,
`/team/member_add`)를 호출한다. 가상 API 키는 자동 발급하지 않는다(`auto_create_key: false`로
명시). 비밀번호는 `/user/new`가 아니라 `/user/update`로 별도 설정한다 — litellm이 계정 생성과
비밀번호 설정을 서로 다른 엔드포인트로 나눠뒀기 때문.

## 준비

```bash
cd scripts
pip install -r requirements.txt
cp .env.example .env
# .env를 열어 LITELLM_MASTER_KEY 값을 채운다 (GitLab CI/CD Variables 또는 Cloud Run 콘솔에서 확인)
python add_litellm_users.py --init         # scripts/users.xlsx를 헤더만 있는 상태로 생성
```

## 엑셀 컬럼

| 컬럼         | 필수 | 기본값                                         | 비고                                                                                           |
| ------------ | ---- | ---------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `user_email` | O    | —                                              | 없으면 해당 행 실패 처리                                                                       |
| `user_id`    | X    | 이메일 `@` 앞부분                              | litellm user_id로 사용                                                                         |
| `user_role`  | X    | `internal_user`                                | 값 검증은 하지 않고 서버에 그대로 전달(fail-open) — 정확한 enum은 아래 "필드명이 다를 때" 참고 |
| `team_id`    | X    | 없음                                           | 여러 팀이면 쉼표로 구분: `team-a,team-b`                                                       |
| `password`   | X    | 신규 사용자는 `1234`, 기존 사용자는 변경 안 함 | 로그인 비밀번호. 값을 채우면 그 값을 쓰고, 기존 사용자 행에 값을 채우면 비밀번호가 변경된다    |

실행 후 자동으로 채워지는 컬럼: `status`(대기/완료/실패), `message`(결과 요약 또는 서버 에러 원문),
`processed_at`(처리 시각). **`status`가 `완료`인 행은 다음 실행 때 건너뛴다** — 엑셀 아래에 새 행을
계속 추가하면서 같은 파일로 반복 실행하면 된다.

**주의**: `password` 컬럼은 처리 후에도 셀 값을 지우지 않는다 — 재실행/재확인 편의를 위해 평문으로
남겨둔다(요청에 따른 동작). 이 파일은 `.gitignore`에 이미 포함돼 있어 git에는 올라가지 않지만
(`scripts/*.xlsx`), 로컬 파일 자체의 접근 권한은 사용자가 별도로 관리해야 한다. 빈 칸으로 둔 신규
사용자는 기본값 `1234`로 계정이 생성되므로, 실제 사용자에게 전달할 때 최초 로그인 후 비밀번호 변경을
꼭 안내한다.

## 실행

```bash
python add_litellm_users.py --dry-run   # 실제 생성 없이 GET /user/info 조회 결과만 미리 확인
python add_litellm_users.py             # 실제 실행 (scripts/users.xlsx 기준)
python add_litellm_users.py --file users.xlsx --sheet Sheet
```

실패한 행이 하나라도 있으면 종료 코드 1을 반환한다 — `message` 컬럼에서 원인을 확인한다.

## 필드명이 실제 서버와 다를 때

`user_role` enum 값, `/team/member_add`의 정확한 body 형태는 litellm 공개 문서 기준으로 작성했고
이 스크립트를 만든 환경에서는 사내망 서버(`fab0704-api.dev.gcp.lgchem.com`)에 접근할 수 없어 실제
1.93.0 응답으로 검증하지 못했다. 서버가 FastAPI라 `<LITELLM_BASE_URL>/docs`(Swagger)에서 정확한
스키마를 바로 확인할 수 있다. 다르면 `add_litellm_users.py` 상단의 `DEFAULT_USER_ROLE`,
`DEFAULT_TEAM_MEMBER_ROLE`과 `create_user`/`add_team_member` 함수만 고치면 된다.

`/user/update`(비밀번호 설정)의 `{"user_id": ..., "password": ...}` body 형태는 설치된 litellm
1.93.0 소스(`NewUserRequest`/`UpdateUserRequest` 스키마)로 직접 확인한 것이라 위 두 엔드포인트보다
신뢰도가 높다 — 다만 실서버 응답 자체는 마찬가지로 라이브 검증되지 않았다. 다르면
`update_user_password` 함수만 고치면 된다.

## 처음 실행할 때 권장 순서

1. `--dry-run`으로 접속·인증이 되는지 먼저 확인.
2. 테스트 계정 1행만 넣고 실제 실행 → `<LITELLM_BASE_URL>/ui`에서 유저 생성/팀 소속 확인, Keys
   탭에 가상 키가 자동 생성되지 않았는지 확인, 설정된 비밀번호(입력값 또는 기본값 `1234`)로 실제
   로그인이 되는지 확인.
3. 같은 파일로 다시 실행 → 방금 처리한 행은 `완료`라 API를 다시 호출하지 않고 스킵되는지 확인.
