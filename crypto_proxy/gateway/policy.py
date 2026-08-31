"""경로 정책 — 기본이 거부다.

**litellm을 module-level import하지 않는다.** litellm은 backend 의존성이 아니라서 그러면 이 모듈이
axagent 테스트 환경에서 아예 import되지 않는다. 순수 판정(`PathPolicy`)과 지연 로더
(`load_litellm_routes`)를 나눠, 판정은 어디서나 전수 테스트되고 로더는 게이트웨이 부팅에서만 돈다.

**판정 순서가 계약이다** — 봉인 필수를 먼저 본다. 그래야 어떤 경로가 관리 카테고리에도 함께
등재되어도 모델 트래픽이 평문으로 새지 않는다(실패 방향이 한쪽이다).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Decision = Literal["sealed_only", "plaintext", "reject"]

CRYPTO_PATH = "/_crypto"
# UI 자산·온보딩은 라우트 분류 목록에 **없다** — StaticFiles 마운트이거나 분류 밖 라우터다.
# 프리픽스로 잡는다. 아래 목록은 litellm 1.98.0의 실등록 라우트 표를 훑어 얻었다
# (`proxy_server.app.routes` — 분류 enum과 독립된 출처다).
UI_PREFIXES = (
    "/ui",
    "/swagger",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/_next",
    "/litellm-asset-prefix",
    "/get_favicon",
    "/get_image",
    "/get_logo_url",
    "/fallback/login",
    "/onboarding/",
)
# 데이터 플레인 집합에도 들어 있지만 **평문으로 남기기로 결정한** 경로들. 근거는 설계 §7.1-③:
# 카탈로그·모델 메타 조회라 사내 데이터가 없고 관리 UI가 쓴다 — UI를 살리는 쪽으로 정했다.
# `/model/info`는 1.93.0에서는 `llm_api_routes` 밖이었는데 **1.98.0이 거기 넣었다**(실측). 그
# 버전 차이가 승인된 결정을 조용히 뒤집지 않도록 여기 명시한다. 잔여 위험(설정 되돌림 시 부팅당
# 1회 마스터키 헤더 노출)은 설계 §14가 수용으로 기록한다.
CATALOG_PATHS = frozenset({"/models", "/v1/models", "/model/info"})
# 부팅 단정이 요구하는 최소 집합. 여기가 비면 litellm 라우트 분류가 바뀐 것이다.
REQUIRED_MODEL_PATHS = frozenset({"/v1/messages", "/v1/chat/completions"})


@dataclass(frozen=True, slots=True)
class RouteSets:
    llm_api: frozenset[str]
    plaintext: frozenset[str]


class PathPolicy:
    def __init__(self, routes: RouteSets) -> None:
        self._sealed_only = frozenset(routes.llm_api) - CATALOG_PATHS
        self._plaintext = frozenset(routes.plaintext) | CATALOG_PATHS

    def decide(self, path: str) -> Decision:
        if path == CRYPTO_PATH or path in self._sealed_only:
            return "sealed_only"
        if path in self._plaintext or path.startswith(UI_PREFIXES):
            return "plaintext"
        return "reject"

    def assert_model_routes_present(self) -> None:
        missing = REQUIRED_MODEL_PATHS - self._sealed_only
        if missing:
            raise RuntimeError(
                f"모델 경로가 봉인 집합에 없다 — litellm 라우트 분류가 바뀌었다: {sorted(missing)}"
            )

    def assert_platform_probes_pass(self) -> None:
        """플랫폼 헬스 프로브가 통과하는가 — 아니면 컨테이너가 영영 못 뜬다."""
        blocked = sorted(p for p in PLATFORM_PROBE_PATHS if self.decide(p) != "plaintext")
        if blocked:
            raise RuntimeError(
                f"플랫폼 헬스 프로브가 평문으로 통과하지 못한다 — 기동 프로브가 403을 받아 "
                f"리비전이 뜨지 못한다: {blocked}"
            )


def _member_paths(routes_enum: object, name: str) -> set[str]:
    """`LiteLLMRoutes`는 **`enum.Enum`이고 멤버의 `.value`가 경로 모음**이다.

    두 가지가 여기서 틀리기 쉽고 **둘 다 실제로 틀렸다**:

    1. 멤버를 그대로 `set()`에 넣으면 `TypeError: … has no len()`으로 부팅 중에 죽는다.
    2. **모음의 타입이 하나가 아니다** — litellm 1.98.0 실측: 28개는 `list`인데 `public_routes`만
       `frozenset`이다. `isinstance(values, list)`로 거르면 그 하나가 통째로 사라지고, 거기에
       **Cloud Run 시작 프로브 경로 `/health/liveliness`가 들어 있어 서비스가 영영 못 뜬다**
       (403으로 프로브가 24회 실패 → 리비전 기동 실패. 실배포로만 드러났다).

    그래서 문자열이 아닌 임의의 iterable을 받는다.
    """
    member = getattr(routes_enum, name, None)
    values = getattr(member, "value", None)
    if isinstance(values, str) or not isinstance(values, Iterable):
        return set()
    return {v for v in values if isinstance(v, str)}


def load_litellm_routes() -> RouteSets:
    """설치된 litellm에서 라우트 분류를 읽는다. **게이트웨이 부팅에서만 부른다.**

    손으로 적은 목록은 litellm 버전을 올릴 때 조용히 낡는다 — 런타임 파생이면 설치된 버전을
    자동으로 따라간다.
    """
    # 지연 import가 계약이다 — 위 docstring 참고. module level로 올리면 axagent에서 이 모듈이 죽는다.
    from litellm.proxy._types import LiteLLMRoutes as R

    # **카테고리 이름을 손으로 나열하지 않는다.** litellm 1.93.0은 28개 카테고리를 정의하는데
    # 초안이 7개만 적어 66개 실경로(admin_viewer·global_spend_tracking·internal_user·compliance…)가
    # 통째로 거부됐다 — 관리 UI가 그만큼 깨진다. 데이터 플레인(`llm_api_routes`)을 빼고 **litellm이
    # 아는 나머지 전부**를 평문으로 삼으면 카테고리가 늘어도 따라간다. 어느 카테고리에도 없는 경로는
    # 여전히 「모르는 경로」라 거부된다 — fail-closed 기본값은 그대로다.
    # `__members__`를 쓴다 — `dir()`는 멤버가 아닌 이름도 섞어 주고 무엇이 카테고리인지 모호하다.
    members = getattr(R, "__members__", None)
    if not members:
        raise RuntimeError("litellm의 LiteLLMRoutes가 Enum이 아니다 — 라우트 분류 모양이 바뀌었다")
    known: set[str] = set()
    for name in members:
        known |= _member_paths(R, name)
    llm_api = _member_paths(R, "llm_api_routes")
    if not llm_api:
        # 빈 집합이면 SEALED_ONLY도 비고 모든 모델 경로가 「모르는 경로」가 된다. 그건 거부로
        # 떨어져 fail-closed이긴 하지만, 원인이 안 보이는 실패라 여기서 이름을 붙여 죽인다.
        raise RuntimeError("litellm의 llm_api_routes를 읽지 못했다 — 라우트 분류 모양이 바뀌었다")
    return RouteSets(llm_api=frozenset(llm_api), plaintext=frozenset(known - llm_api))


# 플랫폼 헬스 프로브. 이것이 거부되면 컨테이너가 **영영 뜨지 못한다** — 관리 UI가 깨지는 것과
# 실패의 무게가 다르다. litellm의 `public_routes`(frozenset)에 실재하지만, 그 집합을 놓치는
# 실수가 실제로 났으므로 부팅 단정으로 못박는다.
# 철자 둘 다 litellm의 `public_routes`에 실재한다(1.98.0 실측). Cloud Run 기동 프로브가 실제로
# 친 것은 `/health/liveliness`다(403 24회 → 리비전 기동 실패). `/health/readiness`는 실재하지
# 않으므로 넣지 않는다 — 없는 경로를 요구하면 단정이 영원히 빨갛다.
PLATFORM_PROBE_PATHS = frozenset({"/health/liveliness", "/health/liveness"})
