"""gateway-cloudrun/proxy_main.py — LiteLLM proxy 진입점 + 온프렘 백엔드 호환 런타임 패치
(Cloud Run 배포 전용 사본 — 로컬 dev는 `gateway/proxy_main.py`를 따로 쓴다, 2026-07-23 소스 분리).

`Dockerfile`의 CMD가 바닐라 `litellm --config ...` 대신 `python proxy_main.py --config ...`로
기동한다. 프록시를 띄우기 전에 몽키패치 4건을 적용한다(①~③은 qwen 실측 트리거 기반, ④는
gemini-3.6-flash reasoning 실측 기반, 3.7-flash 추가 시 동일 근거로 확장 — 아래 각 클래스/함수
docstring):

1. **도구 이름 정규화**(`_ToolNameNormalizeFilter`) — 약한 모델이 도구 정식명 대신 축약명으로
   호출해 CLI가 "No such tool available"로 데드엔드에 빠지는 것을 게이트웨이에서 교정한다.
2. **tool-call 인자 조각 무결성 검증**(`_ToolArgsIntegrityFilter`) — hosted_vllm 스트리밍
   경로의 tool-parser가 인자 델타를 훼손하는 것을 CLI에 닿기 전에 fail-loud로 차단한다
   (2026-07-12 prod gemma 실측으로 처음 발견됐으나, 2026-07-22 dev qwen 세션에서도 재현 —
   특정 모델 전용이 아니라 이 스트리밍 경로 일반의 방어다).
3. **mid-conversation system 재작성**(`_midconv_system_to_user`) — litellm의 어댑터 변환
   루프가 user/assistant 외 role을 무언 드롭해 CLI의 스킬 목록이 hosted_vllm 백엔드에
   도달하지 못하는 것을 우회한다.
4. **gemini flash reasoning 레지스트리 등록**(`_register_gemini_flash_reasoning_support`)
   — litellm 1.93.0 내장 model_cost에 gemini-3.6-flash/gemini-3.7-flash가 없어
   `supports_reasoning()`이 False를 반환, `reasoning_effort`/`thinking`이 `drop_params`로
   조용히 drop되던 것을 우회(2026-07-29 라이브 진단 — config.yaml에 reasoning_effort를 걸어도
   Vertex로 나가는 최종 요청에 전혀 안 실리고 있었음을 gcloud logging으로 직접 확인, gemini-3.6-flash
   기준). 각 모델은 litellm에 정식 등재되는 대로(3.6은 1.95.0-dev+) `_GEMINI_FLASH_REASONING_MODELS`
   에서 빼면 된다.

litellm 핀(1.93.0)이 고정이라 대상 클래스/메서드가 안정적이며, 핀을 올릴 때 재검증 대상이다
(핀은 `Dockerfile`의 베이스 이미지 태그·`pyproject.toml`의 dependencies가 단일 출처 — 여기 숫자를
따로 두지 않는다).

환경변수(DB DSN·마스터 키·API 키 포함)는 `env.py`가 이 모듈의 첫 import로 채운다(사용자 명시적
결정 — `env.py` 자체 docstring에 배경·위험 설명).
"""

from __future__ import annotations

import env  # noqa: F401 — 순서 필수: litellm이 import되기 전에 os.environ을 채운다(env.py 참고).

import contextvars
import os

# 이 요청의 유효 도구 이름 집합(openai tools[].function.name) — transform_request 패치가 요청마다
# set하고, 같은 요청 task에서 만들어지는 스트림 필터가 스냅샷한다(task-로컬이라 동시 요청 간 누수 0;
# task 경계로 비어 있으면 정규화가 그냥 꺼진다 — fail-open).
_REQUEST_TOOL_NAMES: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "gw_request_tool_names", default=frozenset()
)


def _toolname_logger():
    """도구명 정규화 진단 전용 로거. 로그 디렉터리는 GATEWAY_LOG_DIR env(기본 var/log) —
    Cloud Run 등 읽기전용 FS에서는 이 env로 /tmp 하위를 가리킨다(cwd 쓰기 불가 대응)."""
    import logging
    import os

    lg = logging.getLogger("gateway.toolname")
    if not lg.handlers:
        log_dir = os.environ.get("GATEWAY_LOG_DIR", "var/log")
        os.makedirs(log_dir, exist_ok=True)
        h = logging.FileHandler(os.path.join(log_dir, "gateway-toolname.log"))
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.WARNING)
        lg.propagate = False
    return lg


def _read(chunk, key):
    """ModelResponseStream(pydantic)·dict 양쪽에서 필드를 읽는다."""
    if isinstance(chunk, dict):
        return chunk.get(key)
    return getattr(chunk, key, None)


class _ToolArgsIntegrityFilter:
    """upstream tool-call **인자 조각의 조립 무결성**을 검증한다(hosted_vllm 스트리밍 경로 일반
    방어 — 최초 발견은 2026-07-12 prod gemma/사내 vLLM 실측: 서빙의 스트리밍 tool-parser가 인자
    델타를 중복(`fal`+`false`→"falfalse")·구분자 유실(`"render_charts"` 직후 `{`)로 훼손 → CLI가
    InputValidationError를 돌려주고 모델이 자기 탓으로 오인해 재시도 루프(gemma 35연속). litellm
    어댑터는 합성 재현으로 무죄 확정(초미세 78조각·reasoning 혼재 모두 바이트-동일 조립). **특정
    모델 전용이 아니다** — 2026-07-22 dev qwen 세션에서 같은 조립-실패 시그니처(JSON 파싱 불가)가
    재현돼(char 881 절단) 게이트웨이가 502로 정상 차단했다: hosted_vllm 스트리밍 경로를 타는
    모델이면 서빙 백엔드 무관하게 재발할 수 있는 일반 방어다.

    choices[0].delta.tool_calls의 인자 조각을 tool index별로 누적하고, finish 청크가 오면 조립
    문자열을 json 파싱으로 검증한다 — 무효면 훼손이 CLI에 닿기 전에 **APIError 502로 fail-loud**
    (모델·CLI의 혼란 루프 대신 명확한 인프라 에러 1회). 빈 버퍼(무인자 도구·인자 없는 청크)는
    검증하지 않는다. 정상 스트림은 무영향(qwen·gemma 정상 조립 실측)."""

    def __init__(self, stream):
        self._stream = stream
        self._sync_iter = None
        self._async_iter = None
        self._args_by_index: dict = {}
        self._model = "unknown"

    def _observe(self, chunk):
        if chunk is None or chunk == "None":
            return chunk
        self._model = str(_read(chunk, "model") or self._model)
        choices = _read(chunk, "choices") or []
        if not choices:
            return chunk
        first = choices[0]
        delta = _read(first, "delta")
        for tc in (_read(delta, "tool_calls") or []) if delta is not None else []:
            fn = _read(tc, "function")
            frag = _read(fn, "arguments") if fn is not None else None
            if frag:
                idx = _read(tc, "index") or 0
                self._args_by_index[idx] = self._args_by_index.get(idx, "") + str(frag)
        if _read(first, "finish_reason"):
            self._validate()
        return chunk

    def _validate(self):
        import json as _json

        for idx, buf in self._args_by_index.items():
            if not buf.strip():
                continue
            try:
                _json.loads(buf)
            except ValueError as exc:
                from litellm.exceptions import APIError

                raise APIError(
                    status_code=502,
                    message=(
                        "upstream streamed CORRUPTED tool-call arguments (assembled fragment "
                        f"for tool #{idx} is not valid JSON: {exc}) — hosted_vllm 스트리밍 경로의 "
                        "tool-parser 훼손(실측: prod gemma 2026-07-12·dev qwen 2026-07-22 — "
                        "특정 모델 전용 아님). 판별: 게이트웨이 우회 직접 curl."
                    ),
                    llm_provider="hosted_vllm",
                    model=self._model,
                ) from exc

    def __iter__(self):
        self._sync_iter = iter(self._stream)
        return self

    def __next__(self):
        return self._observe(next(self._sync_iter))

    def __aiter__(self):
        self._async_iter = self._stream.__aiter__()
        return self

    async def __anext__(self):
        return self._observe(await self._async_iter.__anext__())

    def __getattr__(self, name):
        return getattr(self.__dict__["_stream"], name)


class _ToolNameNormalizeFilter:
    """모델이 emit한 tool-call **이름을 CLI가 보기 전에 정식명으로 정규화**한다(도구명 규약 P2 —
    prod 적대 라운드5 R3-F1). 약한 모델(qwen 실측)이 `mcp__agent__propose`를 축약명
    `propose`로 호출하면 CLI가 "No such tool available: propose"를 돌려주고, 모델이 "propose
    도구가 등록되어 있지 않다"고 오판해 데드엔드에 빠진다(S13 ❌ — S12는 우연히 재호출로 회복).
    교정 피드백(사후 복구)이 아니라 **발생 자체를 제거**한다: 게이트웨이는 모델→CLI 사이에 있고,
    이 요청의 유효 도구 목록(_REQUEST_TOOL_NAMES — transform_request 패치가 채움)을 이미 아므로
    바깥 결합 없이 자기완결로 고칠 수 있다.

    규칙(결정론·fail-open): 청크의 tool_call 이름이 ① 유효 목록에 그대로 있으면 무변경 ② 정확히
    **한 개**의 유효 이름 X에 대해 `X == prefix + name` 꼴 접미(name 앞이 `__` 경계)면 X로 재작성
    ③ 그 외(미매칭·복수 매칭·목록 없음)는 무변경 — 재작성 실패는 어차피 CLI의 기존 에러 경로로
    떨어질 뿐이라 이 필터는 상황을 악화시킬 수 없다. 이름은 tool_call의 첫 청크에만 실리므로
    재작성도 그 청크 1회다. Bash·Read 등 built-in(bare 유효명)은 ①에서 그대로 통과한다."""

    def __init__(self, stream):
        self._stream = stream
        self._sync_iter = None
        self._async_iter = None
        self._valid = _REQUEST_TOOL_NAMES.get()  # 생성 시 스냅샷(요청 task-로컬)

    def _normalize_name(self, name: str) -> str | None:
        """재작성할 정식명(유일 마지막-세그먼트 매칭) 또는 None(무변경).

        **마지막-세그먼트**(`__` 이후 꼬리) 매칭이라 두 방향을 대칭으로 잡는다(라이브 실측 R3-F1'):
        ① 접두 드롭 — `propose` → `mcp__agent__propose`(꼬리 propose 일치)
        ② 접두 과잉 — `mcp__agent__Bash` → `Bash`(built-in에 mcp 접두를 잘못 붙인 케이스; dev S09
           qwen 실측 "No such tool available: mcp__agent__Bash"). 우리 도구 canonical은 전부 단일
           밑줄이라(`analysis_flow_step` 등) `__` 분할 꼬리가 canonical과 같아 충돌 없음.
        정확히 한 개 매칭일 때만 재작성(0·2+는 무변경 fail-open — CLI 기존 에러 경로로 떨어질 뿐)."""
        if not name or not self._valid or name in self._valid:
            return None
        seg = name.rsplit("__", 1)[-1]  # 마지막 세그먼트(bare면 name 자체)
        candidates = [v for v in self._valid if v.rsplit("__", 1)[-1] == seg]
        return candidates[0] if len(candidates) == 1 else None

    def _observe(self, chunk):
        if chunk is None or chunk == "None":
            return chunk
        choices = _read(chunk, "choices") or []
        if not choices:
            return chunk
        delta = _read(choices[0], "delta")
        for tc in (_read(delta, "tool_calls") or []) if delta is not None else []:
            fn = _read(tc, "function")
            if fn is None:
                continue
            orig = str(_read(fn, "name") or "")
            if not orig or not self._valid or orig in self._valid:
                continue  # 정상(정식명·built-in) — 흔한 경로, 로그 없음
            fixed = self._normalize_name(orig)
            # 비정상 도구명은 재작성 여부와 무관하게 진단 로그(_toolname_logger — 파일 캡처).
            # 재작성=해소, 무재작성=CLI "No such tool"로 떨어짐(관측 필요 — valid 후보 수 함께 기록).
            if fixed:
                _toolname_logger().warning("normalize tool name %r -> %r", orig, fixed)
                if isinstance(fn, dict):
                    fn["name"] = fixed
                else:
                    fn.name = fixed
            else:
                seg = orig.rsplit("__", 1)[-1]
                cand = [v for v in self._valid if v.rsplit("__", 1)[-1] == seg]
                _toolname_logger().warning(
                    "unresolved tool name %r (seg=%r, %d candidates) — valid=%s",
                    orig,
                    seg,
                    len(cand),
                    sorted(self._valid),
                )
        return chunk

    def __iter__(self):
        self._sync_iter = iter(self._stream)
        return self

    def __next__(self):
        return self._observe(next(self._sync_iter))

    def __aiter__(self):
        self._async_iter = self._stream.__aiter__()
        return self

    async def __anext__(self):
        return self._observe(await self._async_iter.__anext__())

    def __getattr__(self, name):
        return getattr(self.__dict__["_stream"], name)


def _midconv_system_to_user(messages):
    """messages 배열 **중간**의 `role:"system"` 항목을 user + <system-reminder> 래핑으로 재작성한다.

    근거(2026-07-11 적대 eval §4a-0 — 요약 = gateway/README 실측 기록, 판정 원문 = git history):
    claude CLI는 스킬 목록(available-skills 매니페스트)을 mid-conversation `role:"system"`
    메시지로 싣는데, litellm 1.91.1의 `translate_anthropic_messages_to_openai` 변환 루프는
    user/assistant 외 role을 **폴백 없이 스킵**한다 → hosted_vllm 백엔드(qwen 등)에는
    스킬 목록이 아예 도달하지 않아 Skill 자가 선택이 구조적으로 불가능했다(qwen 스킬 자가로드
    0→10 슬러그 실측).

    user + <system-reminder> 형태를 쓰는 이유: OpenAI-호환 서빙의 chat template 다수가
    mid-conversation system을 미지원/오처리하므로(위치 0 전용), Claude Code가 리마인더에 쓰는
    검증된 형태로 낮춘다. 이 함수는 어댑터 변환 경로에서만 불리므로 anthropic passthrough
    (구독 sonnet 등)는 자동으로 무영향이다. 최상위 `system` 파라미터는 별도 경로
    (`_add_system_message_to_messages`)라 건드리지 않는다."""
    out = []
    for m in messages or []:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "system":
            out.append(m)
            continue
        content = (
            m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        )
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n\n".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = str(content or "")
        out.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<system-reminder>\n{text}\n</system-reminder>",
                    }
                ],
            }
        )
    return out


_GEMINI_FLASH_COST_ENTRY = {
    "max_input_tokens": 1048576,
    "max_output_tokens": 65536,
    "litellm_provider": "vertex_ai",
    "mode": "chat",
    "supports_reasoning": True,
    "supports_function_calling": True,
    "supports_vision": True,
    "supports_system_messages": True,
    "supports_tool_choice": True,
}

# litellm 정식 등재 전인 gemini flash 계열 — 등재되는 대로 여기서 빼면 된다(gemini-3.6-flash는
# 1.95.0-dev3+에 정식 등재 확인됨; 3.7-flash는 3.6과 동일 근거로 선제 등록).
_GEMINI_FLASH_REASONING_MODELS = ("gemini-3.6-flash", "gemini-3.7-flash")


def _register_gemini_flash_reasoning_support() -> None:
    """litellm 1.93.0 내장 model_cost 레지스트리에 `_GEMINI_FLASH_REASONING_MODELS`의 모델들이
    없어 `supports_reasoning()`이 False를 반환 → `get_supported_openai_params`가 `reasoning_effort`/
    `thinking`을 지원 목록에서 빼먹고 전역 `drop_params: true`(config.yaml)가 이를 조용히
    drop하던 문제 우회(2026-07-29 gcloud logging 라이브 진단으로 확인, gemini-3.6-flash 기준 —
    config.yaml의 reasoning_effort 설정에도 불구하고 Vertex로 나가는 최종 optional_params에
    thinkingConfig가 전혀 없었음). config.yaml의 각 모델 (-low/-medium/-high) 엔트리가 이미 갖고
    있는 reasoning_effort/supports_reasoning 설정은 이 레지스트리 게이트를 통과해야 실제 적용된다.

    두 번째(진짜) 게이트 — `litellm.model_cost`에만 넣으면 안 된다(1차 배포로 실측): 실제
    `reasoning_effort`→`thinkingConfig` 변환은 `get_optional_params`가
    `custom_llm_provider == "vertex_ai" and model in litellm.vertex_chat_models`일 때만
    `VertexGeminiConfig().map_openai_params()`로 분기해서 실행된다(litellm/utils.py). 이
    `vertex_chat_models` 집합은 `model_cost` 딕셔너리 자체가 아니라, `register_model()`이
    엔트리의 `litellm_provider`가 정확히 문자열 `"vertex_ai-chat-models"`(하이픈 포함 — 요청의
    `custom_llm_provider="vertex_ai"`와 다른 내부 분류 문자열)일 때 별도로 채우는 부수효과라
    `model_cost`를 직접 몽키패치해도 이 집합엔 반영되지 않는다. 그래서 model_cost 갱신 뒤
    `vertex_chat_models`에도 모델명을 직접 추가해야 한다(로컬 재현: 이걸 빠뜨리면 supports_reasoning
    은 True인데도 최종 optional_params엔 thinkingConfig가 여전히 없음 — 추가하면 정상 생성 확인).
    litellm 1.95.0-dev3+엔 gemini-3.6-flash가 두 레지스트리 모두에 정식 등재돼 있음(직접 확인) —
    핀을 그 이상으로 올리면 해당 모델은 `_GEMINI_FLASH_REASONING_MODELS`에서 빼면 된다."""
    import litellm

    for model in _GEMINI_FLASH_REASONING_MODELS:
        for key in (f"vertex_ai/{model}", f"gemini/{model}", model):
            litellm.model_cost.setdefault(key, {}).update(_GEMINI_FLASH_COST_ENTRY)
            litellm.vertex_chat_models.add(key)


def _apply_patches() -> None:
    """런타임 패치 4건(멱등): ① AnthropicStreamWrapper.__init__을 감싸 upstream 앞단에
    tool-인자 무결성 필터 + tool-이름 정규화 필터를 끼운다. ② 어댑터 메시지 변환 앞단에서
    mid-conversation system을 user+system-reminder로 재작성한다(스킬 목록 등 — litellm 변환 루프의
    무언 드롭 우회). ③ HostedVLLMChatConfig.transform_request를 감싸 이 요청의 tools 이름 집합을
    contextvar에 채운다(이름 정규화 재료 — R3-F1). ④ litellm.model_cost에 gemini flash 계열(3.6/3.7)
    reasoning 지원 플래그를 등록한다(레지스트리 부재로 인한 drop_params 우회 —
    `_register_gemini_flash_reasoning_support` 참고). litellm 핀(1.93.0) 고정 전제 — 핀을
    올릴 때 패치 전부 재검증 대상이다(업스트림이 고치면 ②·④ 제거). (참고: fake-stream 강제 패치는
    2026-07-12 검증 후 원복 — 사용자 결정으로 운영 미채택. 필요 시 git history `c5fd5926`에서
    복원.)"""
    _register_gemini_flash_reasoning_support()

    from litellm.llms.anthropic.experimental_pass_through.adapters import (
        streaming_iterator as _si,
    )
    from litellm.llms.anthropic.experimental_pass_through.adapters import (
        transformation as _tf,
    )
    from litellm.llms.hosted_vllm.chat.transformation import HostedVLLMChatConfig

    wrapper = _si.AnthropicStreamWrapper
    if not getattr(wrapper, "_toolname_normalize_guard", False):
        _orig_init = wrapper.__init__

        def _patched_init(self, completion_stream, *args, **kwargs):
            _orig_init(
                self,
                _ToolNameNormalizeFilter(_ToolArgsIntegrityFilter(completion_stream)),
                *args,
                **kwargs,
            )

        wrapper.__init__ = _patched_init
        wrapper._toolname_normalize_guard = True

    if not getattr(HostedVLLMChatConfig, "_toolname_ctx_guard", False):
        _orig_tr = HostedVLLMChatConfig.transform_request

        def _patched_transform_request(
            self, model, messages, optional_params, litellm_params, headers
        ):
            # 이 요청의 유효 도구 이름(openai tools[].function.name)을 task-로컬로 노출 —
            # 이름 정규화 필터(_ToolNameNormalizeFilter)의 유일한 재료. 추출 실패는 빈 집합(fail-open).
            names: set[str] = set()
            for t in optional_params.get("tools") or []:
                fn = t.get("function") if isinstance(t, dict) else None
                name = fn.get("name") if isinstance(fn, dict) else None
                if name:
                    names.add(str(name))
            _REQUEST_TOOL_NAMES.set(frozenset(names))
            # 프로세스당 1회 도구 인벤토리 로깅(진단): 정규화가 built-in(Bash 등)까지 커버하는지
            # 확인하는 근거 — CLI가 tools 배열에 built-in을 싣는지 여기서 관측된다(오버접두 재작성 전제).
            if names and not getattr(
                _patched_transform_request, "_logged_inventory", False
            ):
                _patched_transform_request._logged_inventory = True
                _toolname_logger().warning("tool inventory (once): %s", sorted(names))
            return _orig_tr(
                self, model, messages, optional_params, litellm_params, headers
            )

        HostedVLLMChatConfig.transform_request = _patched_transform_request
        HostedVLLMChatConfig._toolname_ctx_guard = True

    adapter = _tf.LiteLLMAnthropicMessagesAdapter
    if not getattr(adapter, "_midconv_system_guard", False):
        _orig_translate = adapter.translate_anthropic_messages_to_openai

        def _patched_translate(self, messages, model=None):
            return _orig_translate(
                self, messages=_midconv_system_to_user(messages), model=model
            )

        adapter.translate_anthropic_messages_to_openai = _patched_translate
        adapter._midconv_system_guard = True

    # ④ Vertex Gemini 사고 요약 억제(GATEWAY_SUPPRESS_THOUGHTS, 기본 1 — env.py).
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


def main() -> None:
    _apply_patches()
    from litellm import run_server

    run_server()  # click 커맨드 — sys.argv(--config/--host/--port)를 읽어 프록시 기동


if __name__ == "__main__":
    main()
