"""
envelope.py — 마스터 ↔ 에이전트 공용 봉투 (M-1 공통 이벤트 규약 v0.2)

정의서 v2.2 §7.1 이 정한 **Business 와 Execution 의 분리**를 타입으로 구현한다.

    AgentRequest        마스터 → 에이전트   context(request_id·as_of·mode) + 업무 입력
    AgentReply          에이전트 → 마스터   업무 결과 + 상태 2종 + Evidence + 조정안
    ExecutionMetadata   별도 저장           used_tools·tool_order·LLM 상태 (run_id 로 연결)

★ 새 어휘를 만들지 않는다.
  `RuntimeStatus` · `Verdict` · `Evidence` · `SuggestedAdjustment` 는 전부
  `app.contracts.core` 의 기존 타입을 그대로 쓴다. 신설은 봉투와 `Mode` 뿐이다.

🔴 **이 파일을 자리 이전 ③ 에서 제일 먼저 옮겼다** (2026-09-03).
  물류·판매가 봉투를 경유해 공용 계약을 **두 번째 경로로** 읽는다.

  ```text
  logistics/adapter.py:59 → app.master.envelope → 공용 계약
  ```

  파트가 자기 import 를 다 고쳐도 **여기가 옛 자리를 가리키면 shim 제거(④)에서
  같이 깨진다.** 물류 지적이고, 순서를 그래서 이렇게 잡았다.

★ 두 층으로 나눠 강제한다.
  ┌─ 타입 레벨 (즉시 ContractViolation) ─ 봉투가 성립하지 않는 것
  │    빈 request_id · call_seq < 1 · 에이전트가 못 받는 mode
  │    RUNTIME_NOT_READY 인데 missing_data 가 비었음
  └─ 검증 함수 (EnvelopeFinding 반환) ─ 마스터가 받아 보고 판단할 것
       바인딩 불일치 · Evidence 미첨부 · reasoning 규칙 위반

  전자는 **보낼 수 없게** 막고, 후자는 **받은 뒤 판정**한다. 후자를 예외로 터뜨리면
  에이전트 하나의 실수가 사이클 전체를 죽인다 — 마스터가 정할 몫이다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal, get_args

from app.contracts.core import (
    ContractViolation,
    Dept,
    Evidence,
    RuntimeStatus,
    SuggestedAdjustment,
    Verdict,
)

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 1. 어휘
# ---------------------------------------------------------------------------

AgentName = Literal["finance", "inventory", "purchase", "sales"]
"""★ `Dept` 와 다르다.

`Dept` 는 **밴드에 기여하는 조언자**(sales·inventory·finance)이고,
`AgentName` 은 **마스터가 호출하는 대상**이다. 매입은 제안자라 조언자가 아니다
(정의서 v2.2 §2.1).

★ **판매가 들어왔다** (판매 2026-09-06 통보). 판매 사이클에서 시나리오를 만드는 쪽이
  판매이므로 **마스터가 부를 대상**이다 — 매입 사이클의 매입과 같은 자리다.

  🔴 **그래서 판매는 `Dept` 어휘의 sales 와 같은 것이 아니다.** 한 글자가 두 어휘에
  다 있지만 뜻이 갈린다.

  ```text
  Dept 의 sales         매입 밴드에 조언을 보태는 쪽 — 축 조정을 제안할 수 있다
  AgentName 의 sales    판매 사이클의 제안자 — 조언자가 아니다
  ```

  `_AGENT_DEPT` 가 판매를 담지 않는 이유가 이것이다 (그 주석 참조)."""

Mode = Literal[
    "PRE_PURCHASE",
    "PRE_SALES",
    "SCENARIO_VALIDATION",
    "SALES_VALIDATION",
    "GENERATE_SCENARIOS",
    "GENERATE_SALES_PROPOSAL",
    "STATUS_QUERY",
]
"""호출 목적 (정의서 §3.2.3).

같은 에이전트가 서로 다른 업무를 수행하므로 **무엇을 요청하는지**를 실어 보낸다.
`PRE_PURCHASE` 는 *경계*를, `SCENARIO_VALIDATION` 은 *판정*을 돌려준다.

★ `SALES_VALIDATION` 은 **판매 제안 재무 검증**이다 (2026-09-02 재무 회신).
  매입 `SCENARIO_VALIDATION` 과 나눠 둔 이유는 책임이 다르기 때문이다 — 합치면
  `(agent, mode, call_seq)` 로 매입 검증과 판매 검증을 구분할 수 없고, 그러면
  payload 모양을 보고 무엇인지 **추측하는** 코드가 생긴다.

★ `GENERATE_SALES_PROPOSAL` 은 **판매가 시나리오를 만드는 호출**이다
  (판매 2026-09-06 통보). 매입 `GENERATE_SCENARIOS` 와 나눠 두는 이유는 바로 위
  `SALES_VALIDATION` 과 같다 — 합치면 `(agent, mode, call_seq)` 로 매입 시나리오
  생성과 판매 제안 생성을 구분할 수 없고, 그러면 payload 모양을 보고 무엇인지
  **추측하는** 코드가 다시 생긴다.

★ `PRE_SALES` 는 **물류가 판매용 판매가능·납기 컨텍스트를 주는 호출**이다
  (판매 2026-09-06 통보). `PRE_PURCHASE` 가 매입의 *경계*이듯 판매의 *경계*다.
  경계와 판정을 mode 로 가르는 결이 사이클을 건너서도 같다.

  🔴 두 mode 를 한 이름으로 합치지 않는다. 물류가 두 사이클에 같은 이름으로 답하면
  회신을 받은 마스터가 **어느 사이클의 경계인지** 를 payload 로 되짚어야 한다.

★ **capability 라우팅이 이 아래에 붙었다** (`Capability` · `CAPABILITY_ROUTING`).
  이 docstring 은 *"판매 Flow·어댑터·라우팅은 아직 없다"* 라고 적어 두었었는데,
  그중 라우팅 어휘와 Flow 골격(`sales_flow.py`)이 들어왔다. **어댑터 배선은
  여전히 없다** — 판매·물류 포트를 등록하는 일은 별도 작업이다."""

Trigger = Literal["ML_COMPLETE", "USER_REQUEST"]

LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]
"""그 부서 안에서 LLM 이 어떻게 됐나. **네 값의 뜻을 여기 적는다.**

```text
DISABLED           설정이 꺼져 있다 (`LLM_ENABLED=false` · 프로바이더 미지원)
SKIPPED_TEMPLATE   켜져 있는데 **이번 실행에서는 안 불렀다** — 부를 조건이 아니었다
SUCCESS            불렀고 쓸 수 있는 답을 받았다
FALLBACK           불렀는데 실패했다 — 규칙이 대신 답했다
```

🔴 **`DISABLED` 와 `SKIPPED_TEMPLATE` 를 섞지 않는다.** 섞으면 *"LLM 을 안 켰네"* 와
*"켰는데 이번엔 안 썼네"* 가 구분되지 않는다 — 매입이 8/31 에 지적했다. 앞은 설정
문제라 고칠 수 있고, 뒤는 그날의 사실이라 고칠 것이 없다. **둘을 한 값으로 내면
사람이 없는 문제를 찾는다.**

★ **새로 정한 규칙이 아니다.** 마스터 `IntentService` 와 Critic `JudgeService` 가
  이미 이대로 쓴다 (`master/llm/runtime.py` · `critic/llm/runtime.py`). 다만 **뜻이
  어디에도 안 적혀 있어서** 각 파트가 남의 코드를 읽고 유추해야 했다. 그것이
  어휘가 갈리는 진짜 원인이었다.

★ **`FALLBACK` 은 실패지 오류가 아니다.** 답은 나간다 — 규칙이 만든 답이다.
  그래서 이 값이 없으면 **모델이 죽은 날과 산 날이 화면에서 같아 보인다.**
"""

# ---------------------------------------------------------------------------
# 1-1. 닫힌 집합을 **누가 만들었나로 갈라** 강제한다 (2026-09-02)
# ---------------------------------------------------------------------------
#
# 🔴 **선언은 있는데 강제가 없는 자리**가 이번 주에만 셋이었다.
#   `Trigger`(매입 실측) · `Evidence.value` 가 float 인데 문자열(재무 실측) ·
#   `SuggestedAdjustment.unit` 이 자유 문자열. 전부 조용히 통과했다.
#
# ★ **그렇다고 전부 예외로 막으면 안 된다.** 갈리는 것은 *누가 채우는 값인가* 다.
#
# ```text
# 마스터가 만드는 값   예외로 막는다        trigger · request_id · policy_version · mode
#                     내 실수는 내가 즉시 안다. 부서를 부르기 전에 죽는 편이 싸다
#
# 부서가 채우는 값     findings 로 돌린다   business_status · runtime_status · llm_status
#                     예외로 막으면 **부서 하나의 실수가 사이클을 죽인다** —
#                     봉투 docstring 이 명시적으로 금하는 것이 이것이다
# ```
#
# ★ 집합의 주인은 위의 `Literal` 정의 하나다. `get_args` 로 읽어 **두 벌로 만들지
#   않는다** — 손으로 복사하면 어휘를 늘린 날 한쪽만 늘어난다.
TRIGGERS: frozenset[str] = frozenset(get_args(Trigger))
RUNTIME_STATUSES: frozenset[str] = frozenset(get_args(RuntimeStatus))
VERDICTS: frozenset[str] = frozenset(get_args(Verdict))
LLM_STATUSES: frozenset[str] = frozenset(get_args(LLMStatus))

PASSING_VERDICTS: frozenset[str] = frozenset({"ok", "conditional"})
"""사람에게 올려도 되는 판정. **허용목록이다 — 부정형이 아니다.**

🔴 전에는 `business_status != "reject"` 로 정했다. *"기각이 아니면 통과"* 이므로
  **어휘가 늘 때마다 새 값이 전부 통과 쪽으로 샜다.** 통과 조건을 세는 것이 아니라
  실패 하나를 빼는 구조였다 (#173).

  2026-09-02 에 실제로 하나 늘었다 — 재무가 `SALES_VALIDATION` 을 내면서
  `READY + skipped`(`INPUT_INCOMPLETE`) 가 생겼다. 재무 코드가 마스터가 어떻게
  읽을지까지 적어 뒀다: *"마스터는 재무가 정상 판정한 것으로 읽는다."*

★ `conditional` 은 통과에 남는다. 마스터는 최적안을 고르는 자리가 아니고 사람이
  보고 정한다 (계약 §3.4). `skipped` 는 다르다 — **판정을 안 낸 것**이지
  *"조건부로 괜찮다"* 가 아니다.

🔴 **바로 위 넷과 달리 `get_args` 로 파생시키지 않는다.** `VERDICTS - {...}` 로 쓰면
  다시 *"실패를 빼는 구조"* 가 되어 `#173` 이 고친 자리로 되돌아간다. 어휘가 늘면
  이 목록은 **손으로 늘리는 것이 맞다** — 늘리라고 빨간불이 나야 한다.

  대신 두 집합이 어긋나지 않는지는 대조한다 (`PASSING_VERDICTS <= VERDICTS` ·
  `tests/master/test_envelope_vocabulary.py`). 오타로 어휘 밖 값이 들어오면 잡힌다.

★ **두 Flow 가 같이 쓴다.** 매입(`flow.py`)과 판매(`sales_flow.py`)가 같은 질문을
  하므로 사이클이 아니라 **봉투**가 주인이다.
"""

_AGENT_MODES: dict[AgentName, frozenset[Mode]] = {
    # 판매 검증은 재무만 받는다 — 재고에 열면 없는 책임을 만든다.
    "finance": frozenset(
        {"PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION", "STATUS_QUERY"}
    ),
    # 물류는 두 사이클의 경계를 다 낸다 — `PRE_SALES` 가 판매 쪽 경계다.
    "inventory": frozenset({"PRE_PURCHASE", "PRE_SALES", "SCENARIO_VALIDATION", "STATUS_QUERY"}),
    "purchase": frozenset({"GENERATE_SCENARIOS", "STATUS_QUERY"}),
    # 판매는 제안만 만든다. 판매 제안의 재무 검증(`SALES_VALIDATION`)은 재무가 받는다 —
    # 제안자가 자기 제안을 검증하면 검증이 아니다.
    "sales": frozenset({"GENERATE_SALES_PROPOSAL", "STATUS_QUERY"}),
}

_AGENT_DEPT: dict[AgentName, Dept] = {
    "finance": "finance",
    "inventory": "inventory",
}
"""매입은 여기 없다 — 축 조정을 제안할 권한이 없다 (제안자 ≠ 조언자).

🔴 **판매도 여기 없다.** `Dept` 에 `"sales"` 가 있으니 넣을 수 있어 보이지만, 그것은
  **매입 밴드에 조언을 보태는 쪽**의 어휘다 (`app/contracts/core.py` `Dept`).
  판매 사이클의 판매는 제안자이지 조언자가 아니다.

  넣으면 `band_is_formed` · `blocking_agents` 가 판매를 조언자로 세어, 판매가
  답하지 않은 날 **매입 밴드가 성립하지 않은 것으로 읽힌다** — 없는 의존이
  생긴다. 판매가 축 조정을 제안하는 날이 오면 그때 별도로 정한다
  (판매 2026-09-06 통보 범위 밖)."""


def agent_dept(agent: AgentName) -> Dept | None:
    """에이전트 이름을 부서 어휘로. **없으면 `None`** (조정을 제안할 수 없는 쪽).

    🔴 **지금 두 어휘의 글자가 같다.** `AgentName` 의 `finance`·`inventory` 와
      `Dept` 의 그것이 같은 문자열이라 그냥 비교해도 통한다 — 그래서 위험하다.
      판매가 붙거나 어느 한쪽 이름이 바뀌는 날 **조용히 틀린다.**

    ★ 이름으로 재다 세 번 틀린 주에 만든 자리다 (`llm_attempts` · `runtime_status` ·
      회차 분할). **매핑의 주인을 하나로 두고 거기를 거친다.**
    """
    return _AGENT_DEPT.get(agent)


def agent_allowed_modes(agent: AgentName) -> frozenset[Mode]:
    return _AGENT_MODES[agent]


# ---------------------------------------------------------------------------
# 1-2. Capability — 판매가 요구하고 마스터가 라우팅한다 (판매 2026-09-06 통보)
# ---------------------------------------------------------------------------

Capability = Literal[
    "SELLABLE_SUPPLY_CONTEXT",
    "DELIVERY_FEASIBILITY_CONTEXT",
    "FINANCIAL_VALIDATION",
    "ADDITIONAL_SUPPLY_CONTEXT",
]
"""판매 후보가 **무엇으로 검증받아야 하는지** 스스로 말하는 어휘.

★ **매입과 방향이 반대다.** 매입 사이클은 마스터가 조언자(`flow.ADVISORS`)를 정해
  부른다. 판매는 후보마다 `required_validations[]` 로 capability 를 요구하고,
  마스터가 그것을 `(agent, mode)` 로 바꾼다 — **무엇이 필요한지는 제안자가 알고,
  누가 그것을 하는지는 조정자가 안다.**

🔴 **마스터는 `app.sales.schemas` 를 import 하지 않는다.** 같은 어휘가 그쪽
  `SalesCapability` 에 이미 있지만, 조정자가 부서 스키마를 런타임에 읽으면 부서가
  자기 파일을 고치는 날 마스터가 같이 깨진다 — 재무가 `ApprovedCommitmentFacts` 를
  Protocol 로 받은 것과 같은 이유다.

  **두 벌이 되어 갈리는 것은 테스트가 막는다.** `tests/master/test_sales_flow.py`
  가 양쪽을 import 해 `set(get_args(...))` 를 대조한다 — 테스트에서는 남의 모듈을
  읽어도 되고, 갈린 날 빨간불이 뜬다. 런타임 의존 없이 어휘만 잠그는 자리다.

★ **제자리는 `app/contracts/core.py` 승격이다.** 그건 판매 파일을 고쳐야 해서 판매
  owner 확인이 필요하고, 그때까지 여기 둔다 (설계 2026-09-06 정정 절)."""

CAPABILITIES: frozenset[str] = frozenset(get_args(Capability))
"""집합의 주인은 위 `Literal` 하나다 — `TRIGGERS` 와 같은 이유로 `get_args` 로 읽는다."""

CAPABILITY_ROUTING: dict[Capability, tuple[AgentName, Mode] | None] = {
    "FINANCIAL_VALIDATION": ("finance", "SALES_VALIDATION"),
    # 🔴 **기존 `/logistics/sales` 계산엔진을 그대로 부르지 않는다.** 판매 v1.7 §10 은
    #   *"existing Logistics /sales Adapter"* 로 적었지만, 그렇게 부르면 그 호출에는
    #   **봉투도 `call_seq` 도 `plan.signature` 도 CallBudget 도 Reply 보존도 없다** —
    #   같은 문서 §1 이 마스터 소유라고 적은 바로 그것들이다.
    #
    #   엔진은 재사용하고 **호출 경로만 봉투로 감싼다.** 어댑터가 안에서 기존 엔진을
    #   부르는 것은 자유다 — 바깥이 봉투여야 한다.
    "SELLABLE_SUPPLY_CONTEXT": ("inventory", "PRE_SALES"),
    "DELIVERY_FEASIBILITY_CONTEXT": ("inventory", "PRE_SALES"),
    # 🔴 **`None` 은 "아직 값을 안 정했다" 가 아니라 "부를 대상이 없다" 다.**
    #
    #   매입은 호출 단위(batch / ONE_BY_ONE)를 아직 회신하지 않았고, 그래서 매입에
    #   판매용 mode 를 만들지 않았다. 만들면 마스터가 매입 대신 호출 단위를 정하는
    #   것이 된다.
    #
    #   여기를 **비워 두면 `KeyError` 로 죽고**(마스터 배선 실수처럼 보인다),
    #   표에서 **빼면 조용히 건너뛴다**(사람이 *"검증됐다"* 로 읽는다). 둘 다 틀렸다.
    #   `None` 으로 적어 두면 `SalesFlow` 가 `unroutable_capabilities` 에 담아
    #   결과에 싣고, 화면에서 **"안 왔다"** 로 보인다 (§1.2-10 과 같은 태도).
    #
    #   매입이 호출 단위를 회신하면 여기에 `(agent, mode)` 를 채운다.
    "ADDITIONAL_SUPPLY_CONTEXT": None,
}
"""capability → 부를 대상. **`None` 은 못 부른다는 사실 자체다.**"""


def route_capability(capability: str) -> tuple[AgentName, Mode] | None:
    """capability 를 호출 대상으로 바꾼다. **못 부르면 `None`.**

    ★ **표에 없는 값도 `None` 이다.** 어휘가 갈려 마스터가 모르는 capability 가 오는
      날은 부를 대상이 없는 날과 결과가 같다 — 둘 다 *"이 후보를 통과로 칠 수 없다"* 로
      떨어져야 한다. 어휘가 갈렸다는 사실 자체는 `Capability` 대조 테스트가 잡는다.

    🔴 **`KeyError` 를 내지 않는다.** 부서가 보낸 값 하나로 사이클을 죽이지 않는다는
      봉투 규칙(`check_vocabulary` 와 같은 자리)이 라우팅에도 그대로 적용된다.
    """
    return CAPABILITY_ROUTING.get(capability)


# ---------------------------------------------------------------------------
# 2. 실행 컨텍스트 — 스냅샷을 대체하는 것 (정의서 §3.2.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionContext:
    """한 요청의 시점·정책을 고정한다.

    ★ T0 스냅샷은 폐지됐지만 **시점 계약은 남는다** (정의서 §1.2-6).
      각 에이전트의 Tool 이 `as_of` 로 조회를 자르지 않으면 백테스트가 성립하지 않는다.
      데이터 누수는 에러를 내지 않고 손익만 좋아지므로 계약으로 막아야 한다.

    🔴 **`sim_run_id` 가 왜 여기 있나** (물류 회신 `#325` · 2026-09-06).

      물류가 `get_current_logistics_read` 를 `(sim_run_id, as_of, usage_scope)` 로
      좁히면 그 함수에 마스터 값이 하나 필요해진다. 물류는 *"박지도 추론하지도
      않겠다"* 고 했고 맞는 말이다 — **어느 실행의 장부인가는 물류 사실이 아니다.**

      ★ **생성자 주입이 안 되는 자리다.** 취소·전이·하루넘김 셋은 어댑터가 객체라
        `app/main.py` 에서 `LogisticsTransitionAdapter(sim_run_id=…)` 로 넣었는데,
        에이전트 경로는 `register_agent("inventory", logistics_port)` — **평범한
        함수**다. 생성자가 없다. 남은 길은 봉투 하나뿐이다.

      ⚠️ **`payload` 가 아니다.** 거기 실으면 inventory 만의 관습이 되고 계약에
        안 보인다. *"계약 어휘는 검사로 같이 박는다"* (`#318` 교훈)를 지키려면
        고정된 자리여야 한다.

    ---

    🟡 **아직 필수가 아니다 — 재수출 shim 과 같은 ①②③ 방식이다.**

    ```text
    ①  자리를 만들고 마스터 런타임 둘이 채운다        ✅ 이 판
    ②  각 파트가 자기 경로에서 읽기 시작한다           ⬜ 파트별
    ③  참조가 다 옮겨지면 빈 값을 __post_init__ 이 막는다  ⬜ 마스터가 사전 통보
    ```

    🔴 **한 번에 필수로 만들면 다섯 파트가 같은 판에서 깨진다.** 생성 지점이 **64곳
      (파일 40개)** 이고 그중 62곳이 재무·물류·매입·마스터의 **테스트**다. 한 판에
      다 고치면 깨졌을 때 누구 것인지 못 가린다 — `contracts_core` 를 옮길 때
      배운 그대로다.

    ⚠️ **기본값이 "몰라도 도는" 함정이라는 것을 안다.** 그래서 값을 안 넣었다 —
      `BURN_IN_SIM_RUN_ID` 를 기본값으로 박았으면 안 채운 자리가 조용히 맞는
      답을 받아 ③ 이 영영 안 온다. 빈 문자열은 **틀린 답조차 아니라서** 쓰는
      쪽에서 반드시 걸린다.

    ★ **①이 지켜지는지는 사람이 안 센다.**
      `tests/master/test_envelope_sim_run_id.py` 가 마스터 런타임 두 경로를 실제로
      돌려 빈 값이 안 나가는지 본다.
    """

    request_id: str
    as_of: date
    trigger: Trigger
    policy_version: str
    sim_run_id: str = ""

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ContractViolation("request_id 는 비울 수 없다 — 검증 L1 바인딩의 근거다.")
        if not self.policy_version.strip():
            raise ContractViolation("policy_version 은 비울 수 없다 — 재현 4종의 하나다 (§3.2.4).")
        # 🔴 **닫힌 집합인데 아무도 안 봤다** (매입 실측 2026-09-02).
        #
        #   `Trigger` 는 타입 힌트일 뿐이고 dataclass 는 런타임에 Literal 을 강제하지
        #   않는다. 매입 하네스가 `trigger="MANUAL"` 을 넣었을 때 **입구를 통과해**
        #   LLM 6회·8.6초를 태우고 나서 DB CHECK 에서 죽었고, 나간 사유는
        #   *"재무 검토 기록을 저장하지 못해..."* 였다 — **매입이 재무 문제로 오진했다.**
        #
        # ★ 같은 파일의 `AgentRequest` 가 `mode` 를 막는 것과 **대칭을 맞춘 것**이다.
        #   새 규칙이 아니다.
        if self.trigger not in TRIGGERS:
            raise ContractViolation(
                f"trigger={self.trigger!r} 는 유효하지 않다. 허용: {sorted(TRIGGERS)} "
                "(마스터가 만드는 값이라 입구에서 막는다 — §봉투 어휘 강제 규칙)"
            )


# ---------------------------------------------------------------------------
# 3. AgentRequest — 마스터 → 에이전트
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRequest:
    """★ `budget_remaining` 을 싣지 않는다 (M-1 v0.2 · 재무 파트 합의).

    노출하면 판단이 오염된다 — "마지막 호출이니 보수적으로" 는 도메인 판단이 아니라
    예산 반응이다. 호출 예산은 **마스터의 관리 수단**이지 에이전트의 입력이 아니다.
    """

    context: ExecutionContext
    agent: AgentName
    mode: Mode
    call_seq: int = 1
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.call_seq < 1:
            raise ContractViolation(f"call_seq 는 1 이상이다 (받음: {self.call_seq}).")
        allowed = _AGENT_MODES[self.agent]
        if self.mode not in allowed:
            raise ContractViolation(
                f"{self.agent} 는 mode={self.mode} 를 받을 수 없다. 허용: {sorted(allowed)}"
            )


# ---------------------------------------------------------------------------
# 4. AgentReply — 에이전트 → 마스터 (Business)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentReply:
    """업무 결과만 담는다. 실행 흔적은 `ExecutionMetadata` 로 분리한다 (§7.1).

    ★ `next_agent` 필드는 없다.
      라우팅은 마스터 책임이다 (정의서 §2.3 · §3.3). 에이전트는
      `needs_followup` 으로 "추가 검증이 필요하다"까지만 말한다.
    """

    request_id: str
    as_of: date
    agent: AgentName
    mode: Mode
    run_id: str

    runtime_status: RuntimeStatus
    business_status: Verdict

    payload: Mapping[str, Any] = field(default_factory=dict)
    evidences: tuple[Evidence, ...] = ()
    suggested_adjustments: tuple[SuggestedAdjustment, ...] = ()
    reasoning: str = ""

    needs_followup: bool = False
    additional_validation_required: bool = False
    missing_data: tuple[str, ...] = ()
    missing_capability: tuple[str, ...] = ()

    judgment_fields: tuple[str, ...] = ()
    """★ 이 payload 필드는 **내가 내린 판정**이니 근거를 요구하라 (v0.4).

    자동 판별은 대문자 라벨(`MEDIUM`)만 잡는 **휴리스틱**이라 소문자 판정
    (`situation: "stable"`)을 놓친다. 도메인마다 표기가 달라 규칙으로 못 박을 수 없으므로
    **에이전트가 직접 선언**한다. 선언된 필드는 표기와 무관하게 Evidence 가 필요하다.

    휴리스틱은 자동 하한으로 남는다 — 선언하지 않아도 대문자 라벨은 여전히 걸린다."""

    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ContractViolation(
                "run_id 는 비울 수 없다 — 검증 Tool 이 ExecutionMetadata 를 찾는 키다 (§3.7.4)."
            )
        if self.runtime_status == "RUNTIME_NOT_READY" and not self.missing_data:
            raise ContractViolation(
                "RUNTIME_NOT_READY 는 missing_data 로 무엇이 없는지 밝혀야 한다. "
                "이름 없이 오면 마스터가 사용자에게 무엇을 요청할지 알 수 없다 (M-1 §5.1)."
            )
        for adj in self.suggested_adjustments:
            expected = _AGENT_DEPT.get(self.agent)
            if expected is None:
                raise ContractViolation(
                    f"{self.agent} 는 축 조정을 제안할 수 없다 (제안자는 조언자가 아니다)."
                )
            if adj.dept != expected:
                raise ContractViolation(f"{self.agent} 회신에 {adj.dept} 의 조정안이 섞였다.")

    @property
    def contributes_to_band(self) -> bool:
        """READY 가 아니면 밴드에 기여하지 않는다.

        조용히 건너뛰면 그 부서의 상한이 무한대로 남아 **무제한 매입이 통과한다.**
        마스터는 `not_ready` 로 명시 기록하고 종료 코드는 E4(미시작)로 다룬다.
        """
        return self.runtime_status == "READY"

    @property
    def worth_retry(self) -> bool:
        """`ERROR` 만 재시도 가치가 있다 (M-1 v0.2 §5.1).

        `RUNTIME_NOT_READY` 는 입력이 없어서 못 낸 답이므로 다시 불러도 같다.
        재시도하면 호출 예산만 태운다 (정의서 §1.2-12).
        """
        return self.runtime_status == "ERROR"


# ---------------------------------------------------------------------------
# 4-1. 회신에서 마스터가 뽑아 나르는 것 — **두 Flow 가 같이 쓴다**
# ---------------------------------------------------------------------------
#
# 🔴 셋 다 처음에는 `flow.py`(매입 Flow) 안에 있었고, 판매 Flow 가 생기면서
#   `from app.master.flow import ...` 로 남의 사이클 모듈을 가리켰다. **판매가 매입
#   모듈에 매인 것**이라 매입을 손대면 판매가 깨진다.
#
# ★ 셋은 사이클에 매인 것이 아니다. *"부서 회신 하나에서 무엇을 뽑아 나르나"* 는
#   봉투 수준의 질문이라 제자리가 여기다. 베껴서 두 벌로 만들면 `#173`(허용목록) ·
#   `#175`(전선 표준형)가 고친 자리가 한쪽에서만 살아난다.


@dataclass(frozen=True)
class SourcedEvidence:
    """부서가 낸 근거 하나 + **누가 어느 모드에서 냈는가.**

    ★ `Evidence` 자체에는 부서도 모드도 없다. 봉투가 그 문맥을 들고 있기 때문이다 -
      회신이 누구 것인지는 `AgentReply.agent` 가 안다. 응답으로 나갈 때는 그 문맥이
      사라지므로 여기서 붙여 준다.

    ★ **값은 손대지 않는다.** `evidence` 는 부서가 낸 것 그대로다.
    """

    agent: AgentName
    mode: str
    evidence: Evidence


@dataclass(frozen=True)
class AgentFailure:
    """기여하지 못한 부서 하나 — **이름만이 아니라 사유까지.**

    🔴 전에는 이름만 실었다. `"경계를 내지 못한 에이전트: finance"` 를 받은 사람이
      할 수 있는 것은 *"다시 돌려 본다"* 뿐이었고, 그건 조사가 아니라 추측이다
      (재현성 측정 2026-09-02 · 6회 중 2회 실패 사유를 이력만으로는 못 봄).

    ★ **매입 Flow 는 이미 사유를 실었다** (`flow.py` `_run` 의 매입 미가동 분기).
      새 규칙이 아니라 **대칭을 맞추는 것**이다. (이 문장은 셋이 `flow.py` 안에
      있을 때 *"같은 파일 안에서"* 라고 적혀 있었다 — 자리만 옮겼다.)

    ★ **마스터는 해석하지 않는다** (§3.2.2). 부서가 쓴 문장을 그대로 옮긴다.
    """

    agent: AgentName

    #: `ERROR` · `RUNTIME_NOT_READY`, 그리고 **아예 안 불린 경우** `NOT_CALLED`.
    #: 마지막 것은 `RuntimeStatus` 가 아니다 — 회신이 없었다는 뜻이라 회신의 상태로는
    #: 적을 수 없다. "안 부른 것과 못 부른 것은 다르다" 를 여기서도 지킨다.
    runtime_status: str

    reasoning: str = ""
    missing_data: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        """사람이 읽는 한 줄. **여기서만 만든다.**

        사유 문장과 화면 표시를 각자 조립하면 둘이 갈린다 - 근거를 검증과 화면이
        같은 객체로 보게 한 것과 같은 이유다.

        🔴 **이름 절의 문구는 세 경우에 다 참이어야 한다.** `missing_data` 에는
        세 종류가 같은 칸으로 온다::

            안 왔다              부서가 값을 안 보냈다
            왔는데 쓰지 말라      ML 이 `use_recommended=False` 로 표시했다 (#231)
            값이 look-ahead 다    `generated_at > as_of` (`purchase_agent/adapter.py`)

        *"없는 입력"* 은 첫째에만 참이라, 둘째가 오는 날 화면에 *"쓰지 말라고 표시해
        시나리오를 만들지 않았다 / 없는 입력: forecast.use_recommended"* 라는 앞뒤가
        반대인 줄이 나갔다.
        """
        parts = [self.reasoning.strip() or self.runtime_status]
        if self.missing_data:
            # 어휘 출처: 매입 `purchase_agent/adapter.py` 의 `_unusable_forecast_names`
            # 가 쓴 *"쓸 수 없는 입력"* 을 그대로 가져온다. 마스터가 말을 새로 만들면
            # 같은 사실에 부서마다 다른 낱말이 붙는다.
            #
            # ★ **두 갈래로 가르지 않는다.** 가르려면 마스터가 매입의
            #   `UNUSABLE_FORECAST_NAMES` 를 읽어야 하고, 그러면 마스터 문구가 매입
            #   내부 목록에 묶인다. 어느 쪽인지는 부서가 `reasoning` 으로 이미 말한다 -
            #   마스터는 문장을 새로 쓰지 않는다 (§3.2.2).
            parts.append(f"쓸 수 없는 입력: {', '.join(self.missing_data)}")
        return " / ".join(parts)


def wire_adjustment(adjustment: SuggestedAdjustment) -> dict[str, Any]:
    """봉투 표준형을 **전선에 실을 수 있는 모양**으로 편다 (#175).

    🔴 `asdict` 는 `date` 를 그대로 둔다. 그 dict 를 `json.dumps` 에 넣으면
      **TypeError 로 죽는다** — *"Object of type date is not JSON serializable"*.

      지금 안 터지는 이유가 더 나쁘다. 물류 어댑터가 `split_date` 를 표준형에
      **안 옮겨서**(`logistics/adapter.py:1122`) 늘 `None` 이라 통과한다.
      **물류가 칸을 채우는 순간 터진다** — 지금 그 작업 중이다 (매입 지적 2026-09-03).

    ★ **dataclass 의 타입은 안 바꾼다.** `split_date: date | None` 은 객체 안에서
      비교·연산이 되는 것이 맞다. **전선에 실을 때만** ISO 문자열로 편다.
      화면 쪽(pydantic)은 이미 알아서 한다 — 여기만 손으로 해야 하는 자리다.

    ★ **정규화는 보내는 쪽이 한다.** 매입은 *"받아서 바꾸는 쪽이 자연스럽다"* 고
      했지만, `asdict` 로 편 것이 마스터라 마스터가 책임진다. 받는 쪽이 여럿이 되면
      **각자 변환해 같은 사실의 주인이 여럿**이 된다.

    ★ **튜플도 목록으로 편다.** `asdict` 는 튜플을 그대로 두는데 JSON 을 한 번
      왕복하면 목록이 된다 — **같은 칸이 경로에 따라 두 모양**이 되고, 받는 쪽이
      `== [...]` 로 비교하면 in-process 에서만 조용히 어긋난다.

      기준은 하나다. **여기서 나간 dict 는 JSON 왕복을 거쳐도 같아야 한다**
      (`test_전선에_실은_것은_왕복해도_같다`). 칸마다 세지 않고 이 성질로 잠근다.
    """
    out: dict[str, Any] = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in asdict(adjustment).items()
    }
    if adjustment.split_date is not None:
        out["split_date"] = adjustment.split_date.isoformat()
    return out


_HAS_TIMEZONE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")
"""ISO 8601 오프셋이 붙었는가. `2026-09-04T06:00:00+09:00` · `...Z` 는 통과."""


def forecast_is_clean(forecast: Mapping[str, Any] | None, as_of: date) -> bool:
    """예측 생성 시각이 `as_of` 이후면 싣지 않는다.

    오염된 입력으로 시나리오를 만들면 **백테스트 손익만 좋아진다.**
    싣지 않으면 매입이 `RUNTIME_NOT_READY` 를 내고, 그 사실이 이력에 남는다.

    ★ **타임존이 없으면 싣지 않는다** (2026-08-27 매입 요청 반영).
      앞 10자만 비교하므로 오프셋이 없으면 `2026-09-04T23:00` 이 KST 로 09-05 인지
      UTC 로 09-04 인지 갈리지 않는다 — **이 검사 자체가 성립하지 않는다.**
      매입도 수신 시 거부하지만, 여기서 막으면 매입 호출 한 번을 아낀다.

    🔴 **매입 `ProcurementFlow._forecast_is_clean` 에서 여기로 올렸다** (M-1 · 순수 이동).

      판매도 같은 예측을 나르므로 검사가 두 벌이 되면 **한쪽만 고쳐지는 날**이 온다.
      그날 판매는 *"오늘 이후에 생성된 예측"* 으로 오늘을 판단하는데, 그것은 오류를
      내지 않고 **손익만 좋아진다** — 아무도 모른다.

      `PASSING_VERDICTS` · `wire_adjustment` 를 봉투로 올린 것과 같은 이유다.
      *"무엇을 실어도 되는가"* 는 사이클에 매인 물음이 아니다.
    """
    generated = (forecast or {}).get("generated_at")
    if not isinstance(generated, str):
        return True  # 시점 필드가 없으면 판단하지 않는다 — 매입이 수신 시 재검증한다
    if not _HAS_TIMEZONE.search(generated):
        return False
    return generated[:10] <= as_of.isoformat()


# ---------------------------------------------------------------------------
# 5. ExecutionMetadata — 별도 저장 (§7.1-②)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionMetadata:
    """실행 흔적. Business Reply 와 섞지 않는다.

    ★ 검증 Tool 의 ④ 실행 계획 온전성 검사가 이것을 읽는다 (정의서 §3.7.4).
      분리하되 `run_id` 로 접근할 수 있어야 한다.
    """

    run_id: str
    request_id: str
    agent: AgentName

    used_tools: tuple[str, ...] = ()
    tool_order: tuple[int, ...] = ()
    observations: tuple[str, ...] = ()
    rules_applied: tuple[str, ...] = ()
    replans: int = 0

    llm_status: LLMStatus = "DISABLED"
    llm_model: str = ""
    llm_attempts: int = 0
    llm_fallback_used: bool = False

    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if self.tool_order and len(self.tool_order) != len(self.used_tools):
            raise ContractViolation(
                f"tool_order({len(self.tool_order)}) 와 used_tools({len(self.used_tools)}) 의 "
                "길이가 다르다 — 실행 계획을 재현할 수 없다 (§1.2-11)."
            )


# ---------------------------------------------------------------------------
# 6. 검증 — 마스터가 회신을 받고 돌린다
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvelopeFinding:
    code: str
    where: str
    detail: str


# 3자리 이상 연속 숫자(천단위 구분 포함) = 수량·금액·날짜로 본다.
# "D+7" 같은 상대 표현은 통과시킨다 — 실측에서 정상 문장에 자주 쓰인다.
_BIG_NUMBER = re.compile(r"\d[\d,]{2,}")
_SENTENCE_SPLIT = re.compile(r"[.!?。]\s*|\n+")
_LABEL = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CLAIM_PATH = re.compile(r"^(?P<key>[^\[\].]+)\[(?P<sel>[^\]]+)\]\.(?P<sub>.+)$")

_MAX_REASONING_SENTENCES = 3

_PLAN_EXEMPT_MODES: frozenset[Mode] = frozenset({"STATUS_QUERY"})
"""★ `E-PLAN-EMPTY` 를 적용하지 않는 mode (v0.6 · 매입 파트 지적).

`STATUS_QUERY` 는 **판단이 아니라 조회**다. 밴드에 들어가지도 시나리오를 만들지도
않으므로 "어떻게 이 답이 나왔나"를 재현할 대상이 없다. Tool 없이 상태만 돌려주는 것이
정상 동작이다.

> 지적 전에는 매입이 검사를 피하려고 `used_tools: ["status_query"]` 를 넣고 있었다.
> **가짜 Tool 이름이 실행 계획에 남는다** — M-16 이 읽는 바로 그 기록이 거짓이 된다.
> 검사를 피하려고 이력을 오염시키게 만드는 검사는 잘못 놓인 검사다.

`contributes_to_band` 는 `runtime_status == READY` 만 보는 속성이라 mode 를 구분하지
못한다. 그래서 여기서 따로 뺀다."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_label(value: Any) -> bool:
    """판정 라벨인가 — 대문자·숫자·밑줄로만 된 문자열 (휴리스틱).

    `payment_pressure: "MEDIUM"` 은 숫자가 아니지만 매입의 행동을 바꾼다.
    근거 없이 오면 **LLM 이 만든 라벨과 구분되지 않는다.**
    """
    return isinstance(value, str) and bool(_LABEL.match(value))


def _is_item_list(value: Any) -> bool:
    """매핑들의 배열인가 — `scenarios: [{...}, {...}]`."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        return False
    return bool(value) and all(isinstance(item, Mapping) for item in value)


def _is_number_map(value: Any) -> bool:
    """숫자를 담은 매핑인가 — `cap_by_date: {"2026-01-02": 7636.72, ...}`.

    🔴 **여기 있던 구멍이다.** Mapping 은 위 세 갈래 어디에도 안 걸려 **근거 요구에서
    통째로 빠졌다.** 물류 `cap_by_date` 는 숫자 18개인데 하나도 요구되지 않았다 —
    물류가 자발적으로 근거를 실어 주고 있어서 드러나지 않았을 뿐, 빼도 아무 소리가
    안 났다 (실측 2026-08-31).

    ★ **날짜마다 요구하지 않는다.** 18개를 따로 받으면 근거가 18줄이 되는데, 그것들은
      **한 규칙이 만든 한 벌**이다 (`cap_by_date_policy` 가 그 규칙 이름이다).
      스칼라 배열을 *"통째로 하나의 근거"* 로 두는 것과 같은 판단이다.
    """
    if not isinstance(value, Mapping) or not value:
        return False
    return all(_is_number(item) for item in value.values())


#: 🔴 **봉투 자신의 어휘** — 도메인 값이 아니라 모든 부서가 공통으로 다는 메타다.
#: 근거를 요구하지 않고, 값 대조 대상으로도 세지 않는다.
#:
#: `soft_warnings` 가 여기 들어온 경위 (실측 2026-08-30 · `713e515`):
#: 물류 `SCENARIO_VALIDATION` 의
#: `soft_warnings: ['SNAPSHOT_ID_UNRESOLVED', 'GRADE_VOCABULARY_UNRESOLVED']` 에
#: 근거를 요구해서 `E-EVIDENCE-MISSING` 이 **매 실행 떴다.** 그런데 `Evidence.value`
#: 는 숫자라 **채울 방법이 없는 요구**였다 — 물류가 낼 수 있는 것은 "경고 2건" 같은
#: 개수뿐이고 그건 근거가 아니라 세어 본 것이다. **만족시킬 수 없는 검사는 기준이
#: 아니라 결함이다.**
#:
#: 그리고 `verifier._NOT_A_VALUE` 는 같은 키를 이미 *"값이 아니라 메타"* 로 빼고
#: 있었다 — **내 두 검사가 한 키를 두고 반대로 말하고 있었다.** 목록을 계약 쪽에
#: 한 벌만 두고 검증기가 그것을 읽는다.
#:
#: ⚠️ **이름으로만 뺀다.** 모양으로 빼려다 재무 `critical_payment_dates`
#: (날짜 배열) 의 근거 요구까지 지웠다 — `test_비어있지_않은_리스트는_근거가_
#: 필요하다` 가 잡았다. 날짜 배열과 코드 배열은 둘 다 문자열 배열이라 못 가른다.
ENVELOPE_META_KEYS: frozenset[str] = frozenset(
    {"policy_version_used", "as_of", "state_date", "soft_warnings"}
)


def required_claims(payload: Mapping[str, Any], judgment_fields: Sequence[str] = ()) -> set[str]:
    """근거가 필요한 값의 **경로 집합**.

    ★ v0.3 — 배열 payload 를 지원한다 (매입 파트 요청).
      매입은 `scenarios[]` 안에 같은 이름의 필드가 2~3벌 있어서 평면 1:1 이 성립하지 않는다.
      `claim` 에 **경로 표기**를 허용하고, 여기서 그 경로를 만든다.

    ★ 요구 강도가 층마다 다르다.

      | 위치 | 숫자 | 판정 라벨 |
      |---|---|---|
      | 최상위 | 필요 | **필요** — 홀로 서서 남의 행동을 바꾸는 판단이다 |
      | 최상위 · `judgment_fields` 선언분 | — | **필요** — 표기 무관 (v0.4) |
      | 배열 항목 안 | 필요 | **면제** — 구조 식별자이거나 그 에이전트 자신의 판정이다 |

      배열 항목의 라벨까지 요구하면 시나리오마다 `label` 근거를 만들어야 해서 과하다.
      숫자는 다르다 — **어디서 왔는지 없으면 LLM 이 만든 값과 구분되지 않는다.**

    ★ 배열은 **한 겹만** 파고든다. 더 깊은 중첩의 규칙은 도메인이 정한다.
    """
    declared = set(judgment_fields)
    out: set[str] = {key for key in declared if key in payload}
    for key, value in payload.items():
        if key in declared:
            continue  # 이미 넣었다 — 표기와 무관하게 요구한다
        if key in ENVELOPE_META_KEYS:
            # 봉투 어휘는 근거 대상이 아니다. 부서가 굳이 근거를 받고 싶으면
            # `judgment_fields` 에 선언하면 위 가지에서 다시 요구된다.
            continue
        if _is_item_list(value):
            for index, item in enumerate(value):
                for sub, sub_value in item.items():
                    if _is_number(sub_value):
                        out.add(f"{key}[{index}].{sub}")
        elif _is_number(value) or _is_label(value):
            out.add(key)
        elif _is_number_map(value):
            out.add(key)  # 숫자 매핑 — 한 규칙이 만든 한 벌이라 통째로 하나의 근거
        elif not isinstance(value, (str, bytes, Mapping)) and isinstance(value, Sequence) and value:
            out.add(key)  # 스칼라 배열 — 통째로 하나의 근거
    return out


def canonical_claim(payload: Mapping[str, Any], claim: str) -> str | None:
    """`scenarios[공격].total_amount_krw` → `scenarios[1].total_amount_krw`.

    배열 항목은 **번호로도 이름으로도** 가리킬 수 있다. 이름은 그 항목의 문자열 필드
    아무거나와 맞으면 된다(`label` · `scenario_id` 등) — 도메인마다 식별 필드가 달라서
    하나로 못 박지 않는다.

    가리키는 곳이 없으면 `None` — 고아 근거다.
    """
    match = _CLAIM_PATH.fullmatch(claim)
    if match is None:
        return claim if claim in payload else None

    key, selector, sub = match.group("key"), match.group("sel"), match.group("sub")
    items = payload.get(key)
    if not _is_item_list(items):
        return None

    index = _select_index(items, selector)
    if index is None or sub not in items[index]:
        return None
    return f"{key}[{index}].{sub}"


def _select_index(items: Sequence[Mapping[str, Any]], selector: str) -> int | None:
    if selector.isdigit():
        index = int(selector)
        return index if index < len(items) else None
    for index, item in enumerate(items):
        if any(value == selector for value in item.values() if isinstance(value, str)):
            return index
    return None


def check_binding(request: AgentRequest, reply: AgentReply) -> list[EnvelopeFinding]:
    """회신이 그 요청의 것인가 — 검증 L1 바인딩의 봉투 층 대응 (정의서 §3.7.5).

    스냅샷 폐지로 `snapshot_id` 대조가 사라진 자리를 `request_id`·`as_of` 가 메운다.
    """
    out: list[EnvelopeFinding] = []
    ctx = request.context
    if reply.request_id != ctx.request_id:
        out.append(
            EnvelopeFinding(
                "E-BIND-REQUEST",
                "reply.request_id",
                f"요청 {ctx.request_id} 에 회신 {reply.request_id} 가 왔다.",
            )
        )
    if reply.as_of != ctx.as_of:
        out.append(
            EnvelopeFinding(
                "E-BIND-AS-OF",
                "reply.as_of",
                f"요청 as_of={ctx.as_of} 인데 회신 as_of={reply.as_of} 다 — 시점 불일치.",
            )
        )
    if reply.agent != request.agent:
        out.append(
            EnvelopeFinding(
                "E-BIND-AGENT",
                "reply.agent",
                f"{request.agent} 를 불렀는데 {reply.agent} 가 답했다.",
            )
        )
    if reply.mode != request.mode:
        out.append(
            EnvelopeFinding(
                "E-BIND-MODE",
                "reply.mode",
                f"mode={request.mode} 로 불렀는데 {reply.mode} 로 답했다.",
            )
        )
    return out


def check_evidence_coverage(reply: AgentReply) -> list[EnvelopeFinding]:
    """payload 의 숫자·판정 라벨에 근거가 붙었는가 (정의서 §1.2-5).

    ★ 이것이 §1.2-3("LLM 은 숫자를 생성하지 않는다")의 집행 수단이다.
      `Evidence.source` 는 DB Fact·ML·Policy·`tool_calc` 뿐이라
      **LLM 이 만든 값은 어느 출처에도 해당하지 않는다.**

    ★ `claim` 은 경로 표기를 쓸 수 있다 — `scenarios[공격].total_amount_krw` (§required_claims).
    """
    if not reply.contributes_to_band:
        return []  # 못 돈 회신에 근거를 요구하지 않는다

    payload = reply.payload
    required = required_claims(payload, reply.judgment_fields)

    # 선언한 이름이 payload 에 없으면 오타다 — 조용히 넘어가면 검사가 통째로 빈다
    out_declared = [
        EnvelopeFinding(
            "E-JUDGMENT-UNKNOWN",
            f"judgment_fields[{name}]",
            f"'{name}' 을 판정 필드로 선언했으나 payload 에 없다 — 오타이거나 이름이 바뀌었다.",
        )
        for name in sorted(set(reply.judgment_fields) - set(payload))
    ]

    covered: set[str] = set()
    orphans: list[str] = []
    for evidence in reply.evidences:
        canonical = canonical_claim(payload, evidence.claim)
        if canonical is None:
            orphans.append(evidence.claim)
        else:
            covered.add(canonical)

    out = out_declared + [
        EnvelopeFinding(
            "E-EVIDENCE-MISSING",
            f"payload.{path}",
            f"{path} 에 대응하는 Evidence 가 없다.",
        )
        for path in sorted(required - covered)
    ]
    out += [
        EnvelopeFinding(
            "E-EVIDENCE-ORPHAN",
            f"evidences[{claim}]",
            f"payload 에서 '{claim}' 을 찾을 수 없다 — 무엇을 뒷받침하는지 불명.",
        )
        for claim in sorted(orphans)
    ]
    return out


def check_reasoning(reply: AgentReply) -> list[EnvelopeFinding]:
    """`reasoning` 규칙 (M-1 v0.2 §5.4).

    LLM 이 쓰는 자리다. 자유 서술이 아니라 Evidence 기반 짧은 rationale 이어야 한다.
    """
    out: list[EnvelopeFinding] = []
    text = reply.reasoning.strip()
    if not text:
        return out
    if _BIG_NUMBER.search(text):
        out.append(
            EnvelopeFinding(
                "E-REASONING-NUMERIC",
                "reply.reasoning",
                "설명문에 수량·금액·날짜로 보이는 숫자가 있다. "
                "숫자가 필요하면 Evidence 를 추가한다 (§1.2-3).",
            )
        )
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) > _MAX_REASONING_SENTENCES:
        out.append(
            EnvelopeFinding(
                "E-REASONING-TOO-LONG",
                "reply.reasoning",
                f"{len(sentences)} 문장 — {_MAX_REASONING_SENTENCES} 문장 이내로 쓴다.",
            )
        )
    return out


def check_vocabulary(reply: AgentReply) -> list[EnvelopeFinding]:
    """부서가 채운 상태값이 **닫힌 집합 안에 있는가.**

    🔴 **여기가 비어 있으면 fail-open 이다.** 마스터는 `business_status != "reject"`
      로 통과를 정하는데(`flow._acceptable`), 어휘 밖의 값은 *"reject 가 아니다"* 라
      **그냥 통과한다.** 부서가 `FAIL` 을 보내도 안이 사용자에게 올라간다.

    ★ **예외로 막지 않는다.** 부서가 채우는 값이라 예외를 던지면 부서 하나의 실수가
      사이클을 죽인다 — 이 모듈 docstring 이 금하는 것이 그것이다. 사실만 남기고
      무엇을 할지는 마스터가 정한다.

    ★ **판정 단계에는 이미 비슷한 검사가 있다** (`verifier._check_advisor_answered`
      의 *"마스터가 모르는 판정값"*). 그쪽은 `SCENARIO_VALIDATION` 판정만 보고
      검증 Tool 이 돌 때만 발화한다. **여기는 모든 회신·모든 모드에서 돈다** —
      봉투가 `mode` 를 검증 Tool 과 무관하게 막는 것과 같은 자리다.

    ★ 마스터 어휘가 낡았을 수도 있다. 그래서 문장이 *"부서가 틀렸다"* 가 아니라
      **"둘 중 하나가 낡았다"** 로 읽히게 쓴다.
    """
    out: list[EnvelopeFinding] = []
    if reply.runtime_status not in RUNTIME_STATUSES:
        out.append(
            EnvelopeFinding(
                "E-VOCAB-RUNTIME-STATUS",
                "reply.runtime_status",
                f"{reply.runtime_status!r} 는 마스터가 아는 값이 아니다. "
                f"허용: {sorted(RUNTIME_STATUSES)} — 부서 표기와 마스터 어휘 중 "
                "하나가 낡았다.",
            )
        )
    if reply.business_status not in VERDICTS:
        out.append(
            EnvelopeFinding(
                "E-VOCAB-BUSINESS-STATUS",
                "reply.business_status",
                f"{reply.business_status!r} 는 마스터가 아는 판정이 아니다. "
                f"허용: {sorted(VERDICTS)} — 부서 표기와 마스터 어휘 중 하나가 낡았다. "
                "이 값으로는 통과 판정을 신뢰할 수 없다 "
                "(마스터는 'reject 가 아니면 통과' 로 읽는다).",
            )
        )
    return out


def validate_reply(
    request: AgentRequest,
    reply: AgentReply,
    metadata: ExecutionMetadata | None = None,
) -> tuple[EnvelopeFinding, ...]:
    """마스터가 회신을 받고 돌리는 봉투 검증 전체.

    ★ 예외를 던지지 않고 findings 를 돌려준다.
      에이전트 하나의 실수로 사이클을 죽이지 않는다 — 무엇을 할지는 마스터가 정한다.
    """
    out: list[EnvelopeFinding] = []
    out += check_binding(request, reply)
    out += check_vocabulary(reply)
    out += check_evidence_coverage(reply)
    out += check_reasoning(reply)
    if metadata is not None:
        if metadata.llm_status not in LLM_STATUSES:
            # 이 값이 낡으면 *"모델이 죽어 규칙이 답한 날"* 과 *"애초에 안 쓴 날"* 이
            # 화면에서 같아 보인다 (`LLMStatus` 주석). 두 파트가 같은 값을 쓰는지는
            # `test_llm_status_vocabulary` 가 따로 대조한다 — 여기는 런타임 하한이다.
            out.append(
                EnvelopeFinding(
                    "E-VOCAB-LLM-STATUS",
                    "metadata.llm_status",
                    f"{metadata.llm_status!r} 는 마스터가 아는 값이 아니다. "
                    f"허용: {sorted(LLM_STATUSES)} — 부서 표기와 마스터 어휘 중 "
                    "하나가 낡았다.",
                )
            )
        if metadata.run_id != reply.run_id:
            out.append(
                EnvelopeFinding(
                    "E-BIND-RUN-ID",
                    "metadata.run_id",
                    f"회신 run_id={reply.run_id} 인데 메타데이터는 {metadata.run_id} 다.",
                )
            )
        plan_required = reply.contributes_to_band and reply.mode not in _PLAN_EXEMPT_MODES
        if plan_required and not metadata.used_tools:
            out.append(
                EnvelopeFinding(
                    "E-PLAN-EMPTY",
                    "metadata.used_tools",
                    "정상 회신인데 사용한 Tool 이 없다 — 실행 계획을 재현할 수 없다 (§1.2-11).",
                )
            )
    return tuple(out)
