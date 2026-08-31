"""경로 정책 — 기본이 거부다.

**litellm을 module-level import하지 않는다.** litellm은 backend 의존성이 아니라서 그러면 이 모듈이
axagent 테스트 환경에서 아예 import되지 않는다. 순수 판정(`PathPolicy`)과 지연 로더
(`load_litellm_routes`)를 나눠, 판정은 어디서나 전수 테스트되고 로더는 게이트웨이 부팅에서만 돈다.

**판정 순서가 계약이다** — 봉인 필수를 먼저 본다. 그래야 어떤 경로가 관리 카테고리에도 함께
등재되어도 모델 트래픽이 평문으로 새지 않는다(실패 방향이 한쪽이다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Decision = Literal["sealed_only", "plaintext", "reject"]

CRYPTO_PATH = "/_crypto"
# UI 정적 자산은 라우트 목록에 없다 — proxy_server.py가 StaticFiles로 마운트한다. 프리픽스로 잡는다.
UI_PREFIXES = ("/ui", "/swagger", "/docs", "/redoc", "/openapi.json")
# 카탈로그 읽기 — llm_api에도 있지만 사내 데이터가 없고 UI가 쓴다.
CATALOG_PATHS = frozenset({"/models", "/v1/models"})
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


def _member_paths(routes_enum: object, name: str) -> set[str]:
    """`LiteLLMRoutes`는 **`enum.Enum`이고 멤버의 `.value`가 경로 리스트다**(실측: litellm 1.93.0,
    `llm_api_routes` 189개).

    멤버를 그대로 `set()`에 넣으면 `TypeError: object of type 'LiteLLMRoutes' has no len()`으로
    게이트웨이가 부팅 중에 죽는다 — 이 저장소에서는 litellm이 없어 그 실수가 유닛으로 안 잡히므로,
    모양을 여기 한 곳에 가두고 `test_policy.py`가 가짜 Enum으로 고정한다.
    """
    member = getattr(routes_enum, name, None)
    values = getattr(member, "value", None)
    return set(values) if isinstance(values, list) else set()


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
    known: set[str] = set()
    for name in dir(R):
        if not name.startswith("_"):
            known |= _member_paths(R, name)
    llm_api = _member_paths(R, "llm_api_routes")
    if not llm_api:
        # 빈 집합이면 SEALED_ONLY도 비고 모든 모델 경로가 「모르는 경로」가 된다. 그건 거부로
        # 떨어져 fail-closed이긴 하지만, 원인이 안 보이는 실패라 여기서 이름을 붙여 죽인다.
        raise RuntimeError("litellm의 llm_api_routes를 읽지 못했다 — 라우트 분류 모양이 바뀌었다")
    return RouteSets(llm_api=frozenset(llm_api), plaintext=frozenset(known - llm_api))
