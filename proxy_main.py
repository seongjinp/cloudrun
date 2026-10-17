"""gateway-cloudrun/proxy_main.py — LiteLLM proxy 진입점 + 런타임 패치 + 봉투 계층
(Cloud Run 배포 전용 사본 — 로컬 dev는 `gateway/proxy_main.py`를 따로 쓴다, 2026-07-23 소스 분리).

`Dockerfile`의 CMD가 바닐라 `litellm --config ...` 대신 `python proxy_main.py --config ...`로
기동한다. 프록시를 띄우기 전에 패치 2건을 적용하고, litellm 앱을 봉투 미들웨어로 감싼다.

1. **gemini-4 명시적 컨텍스트 캐시 생성 차단** — `_apply_patches` 안. 근거는 그 자리 주석.
2. **Vertex Gemini 사고 요약 억제**(`GATEWAY_SUPPRESS_THOUGHTS`, 기본 1) — 같은 곳. 롤백은 그 자리 주석.
3. **봉투 계층**(`_install_crypto_proxy`) — `POST /_crypto`로 온 봉인 요청만 복호해 원래 scope로
   재구성하고 응답을 다시 봉인한다. 키가 없으면 뜨지 않는다.

옛 hosted_vllm 호환 패치(도구 이름 정규화 · tool-call 인자 무결성 검증 · mid-conversation system
재작성)와 Gemini 3.8/4 Argon 레지스트리 등록은 지웠다 — 앞의 셋은 사내 vLLM(qwen) 전용이었고 그 모델을
카탈로그에서 뺐으며, 뒤의 둘은 litellm 1.105가 상류에서 내장 등록·번역한다. 필요하면 git history.

litellm 핀은 `requirements.txt`가 단일 출처다 — 핀을 올릴 때 패치 대상(`VertexGeminiConfig.map_openai_params`)을
재검증한다.

환경변수(DB DSN·마스터 키·API 키 포함)는 `env.py`가 이 모듈의 첫 import로 채운다(사용자 명시적
결정 — `env.py` 자체 docstring에 배경·위험 설명).
"""

from __future__ import annotations

import env  # noqa: F401 — 순서 필수: litellm이 import되기 전에 os.environ을 채운다(env.py 참고).

import os
import sys


def _apply_patches() -> None:
    """런타임 패치(멱등): gemini-4 명시적 캐시 차단 · Vertex Gemini 사고 요약 억제. litellm 핀
    고정 전제 — 핀을 올릴 때 대상 메서드 시그니처를 재검증한다."""
    # Vertex 명시적 컨텍스트 캐시 생성 차단(gemini-4 계열). litellm은 요청 콘텐츠의 `cache_control`
    # 마커를 Vertex `cachedContents`(명시적 캐시) 생성 호출로 바꾸는데, gemini-4-argon은 명시적
    # 캐시를 지원하지 않아 400("does not support explicit context caching")이 난다 — 앱의 claude
    # CLI가 큰 프롬프트에 `cache_control`을 싣는 경로에서 재현(2026-10-17, ≥ 최소 토큰 수일 때만).
    # 이 모델은 암시적 캐시만 쓰므로 명시적 캐시 단계를 건너뛰고 메시지를 그대로 넘긴다.
    from litellm.llms.vertex_ai.context_caching.vertex_ai_context_caching import (
        ContextCachingEndpoints,
    )

    if not getattr(ContextCachingEndpoints, "_no_explicit_cache_guard", False):
        _orig_check = ContextCachingEndpoints.check_and_create_cache
        _orig_acheck = ContextCachingEndpoints.async_check_and_create_cache

        def _no_explicit_cache(kwargs) -> bool:
            return "gemini-4" in str(kwargs.get("model", ""))

        def _patched_check(self, *args, **kwargs):
            if _no_explicit_cache(kwargs):
                return kwargs["messages"], kwargs["optional_params"], kwargs.get("cached_content")
            return _orig_check(self, *args, **kwargs)

        async def _patched_acheck(self, *args, **kwargs):
            if _no_explicit_cache(kwargs):
                return kwargs["messages"], kwargs["optional_params"], kwargs.get("cached_content")
            return await _orig_acheck(self, *args, **kwargs)

        ContextCachingEndpoints.check_and_create_cache = _patched_check
        ContextCachingEndpoints.async_check_and_create_cache = _patched_acheck
        ContextCachingEndpoints._no_explicit_cache_guard = True

    # Vertex Gemini 사고 요약 억제(GATEWAY_SUPPRESS_THOUGHTS, 기본 1 — env.py).
    # thinkingConfig에 includeThoughts=False를 강제한다: 사고는 켠 채(level/budget 유지) 사고 요약
    # 파트만 생성하지 않는다. litellm 표준 파라미터로는 이 조합을 표현할 수 없다 —
    # includeThoughts=False는 thinking이 꺼진 경우에만 나온다(_map_thinking_param 계열).
    #
    # 근거(실측 A/B, gemini-3.6-flash 18턴 × 2 arm): Gemini가 thinking의 `thought` 플래그를 간헐
    # 누락해 추론이 답변 채널로 새는데(하류에 복원 정보 없음), 억제는 **생성 단계**에서 일어나므로
    # mislabel될 대상 자체가 사라진다. 유출 4/18편 → 0/18편, 답변 길이 중앙값 832→826자(무변화),
    # 배달 실패·깨짐 0 유지. 대가: 사고 파트 0 → 소비자의 사고 타임라인 표시가 사라진다(수용됨).
    # 롤백은 서비스 env `GATEWAY_SUPPRESS_THOUGHTS=0`(재빌드 불필요).
    if os.environ.get("GATEWAY_SUPPRESS_THOUGHTS", "1") == "1":
        from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import (
            VertexGeminiConfig,
        )

        if not getattr(VertexGeminiConfig, "_suppress_thoughts_guard", False):
            _orig_map = VertexGeminiConfig.map_openai_params

            def _patched_map(self, non_default_params, optional_params, model, drop_params):
                out = _orig_map(self, non_default_params, optional_params, model, drop_params)
                cfg = out.get("thinkingConfig") if isinstance(out, dict) else None
                if isinstance(cfg, dict):
                    cfg["includeThoughts"] = False
                return out

            VertexGeminiConfig.map_openai_params = _patched_map
            VertexGeminiConfig._suppress_thoughts_guard = True


def _effective_num_workers() -> int:
    """실효 워커 수. 1보다 크면 워커가 **별도 프로세스**로 떠서 이 파일을 안 거치고,
    그러면 패치와 봉투 계층이 전부 조용히 사라진다. 둘 다 env로 덮어쓸 수 있다."""
    from litellm.constants import DEFAULT_NUM_WORKERS_LITELLM_PROXY

    raw = os.environ.get("NUM_WORKERS", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            sys.exit(f"NUM_WORKERS가 정수가 아니다: {raw!r}")
    return int(DEFAULT_NUM_WORKERS_LITELLM_PROXY)


def _install_crypto_proxy() -> None:
    """패치 ⑤ — litellm 앱을 **통째로** 봉투 미들웨어로 감싼다.

    `add_middleware()`가 아니라 앱 자체를 감싸는 이유: `add_middleware`는 Starlette의
    `ServerErrorMiddleware` **안쪽**에 놓여, litellm이 처리 못 한 예외로 500을 낼 때 그 본문이
    암호화를 우회해 평문으로 나간다. 통째로 감싸면 최외곽이라 그 구멍이 닫힌다.

    개인키가 없으면 **뜨지 않는다.** 평문으로 뜨는 것이 이 계층이 막으려는 상태 그 자체다.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from litellm.proxy import proxy_server

    from crypto_proxy import selftest
    from crypto_proxy.gateway.middleware import CryptoProxyMiddleware
    from crypto_proxy.gateway.policy import PathPolicy, load_litellm_routes
    from crypto_proxy.wire.keys import derive_kid

    # 사본 드리프트는 부팅 거부다 — 이 저장소에는 테스트 러너가 없어 테스트로 집행할 수 없다.
    selftest.verify_vectors()

    keys: dict[bytes, bytes] = {}
    for var in ("CRYPTO_PROXY_PRIVATE_KEY", "CRYPTO_PROXY_PRIVATE_KEY_PREV"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        try:
            priv = base64.b64decode(raw, validate=True)
            pub = (
                X25519PrivateKey.from_private_bytes(priv)
                .public_key()
                .public_bytes(Encoding.Raw, PublicFormat.Raw)
            )
        except Exception:  # noqa: BLE001 — 값을 로그에 싣지 않는다. 키 자료다.
            sys.exit(f"{var}가 X25519 raw 개인키(base64)가 아니다")
        keys[derive_kid(pub)] = priv
    if not keys:
        sys.exit("CRYPTO_PROXY_PRIVATE_KEY 없음 — 평문으로 뜰 수 없다")

    policy = PathPolicy(load_litellm_routes())
    policy.assert_model_routes_present()
    proxy_server.app = CryptoProxyMiddleware(proxy_server.app, private_keys=keys, policy=policy)
    # kid와 개수만 찍는다. 개인키·마스터키는 찍지 않는다.
    print(f"crypto_proxy 미들웨어 설치 — kid={sorted(k.hex() for k in keys)}", flush=True)


def main() -> None:
    _apply_patches()
    # **조건부로 만들지 않는다.** 「키가 있을 때만 봉인」으로 두면 env 한 줄을 지우는 것만으로
    # 조용히 평문으로 돌아간다 — 설계 §10.3이 `enforce=false` 류 전환 플래그를 금지한 이유가
    # 정확히 그것이다. 키가 없으면 **뜨지 않는다**(_install_crypto_proxy의 sys.exit).
    from litellm.proxy import proxy_server

    from crypto_proxy.gateway.middleware import CryptoProxyMiddleware

    _install_crypto_proxy()
    if not isinstance(proxy_server.app, CryptoProxyMiddleware):
        sys.exit("crypto_proxy 미들웨어 미설치 — 평문으로 뜰 수 없다")
    if _effective_num_workers() != 1:
        sys.exit("NUM_WORKERS>1이면 패치가 워커에 실리지 않는다")

    from litellm import run_server

    run_server()  # click 커맨드 — sys.argv(--config/--host/--port)를 읽어 프록시 기동


if __name__ == "__main__":
    main()
