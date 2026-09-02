"""사용자에게 보이는 재무 문장. **한 곳에서만 쓴다.**

이 파일이 소유하는 것
    사람이 읽는 한국어 설명 — 확정 설명 · 준비되지 않음 · 실패 · 조회 · 판정 사유

여기 **없는 것**
    기계 계약 값. `READY` · `ok` · `reject` · `FIN-BASE-STRESS` · Tool 이름 ·
    payload 키 · `missing_data` 식별자 · Trace 필드는 **번역하지 않는다** — 그것은
    프론트와 Critic 과 마스터가 읽는 주소이고, 번역하면 아무 데도 닿지 않는다.

★ 나누는 기준은 **누가 읽는가** 다.

    사람이 읽는 것   reasoning · verdict.reason      → 여기, 한국어, 업무 언어
    기계가 읽는 것   status · id · key · Trace       → 그대로, 영어 식별자

★ 그래서 여기 있는 문장에는 구현 용어가 없다. 읽는 사람은 Tool 도 Capability 도
  Harness 도 Planner 도 모른다 — 알아야 할 이유도 없다. *"무엇이 어떻게 됐고 왜
  그런가"* 만 있으면 된다. 기술적인 사유는 실행 Trace 에 그대로 남는다.

★ **숫자를 쓰지 않는다.** 설명은 고정 문장을 고르는 구조이고, `_validate_ready_reasoning`
  이 숫자를 막는다. 금액은 이미 결정론 코드가 payload 와 Evidence 에 실었다 — 설명이
  그것을 다시 만들 이유가 없고, 만들면 그 순간 숫자의 주인이 바뀐다.
"""

from __future__ import annotations

from app.finance.schemas import FinanceMode

#: 검토가 끝났을 때 사용자에게 나가는 확정 설명.
#:
#: ★ **키는 기계 계약이고 값만 표시 문장이다.** Finalizer 는 이 키 중 하나를 고를 뿐이라,
#:   문장을 어떻게 고쳐 써도 LLM 이 재무 숫자를 새로 만들 자리는 생기지 않는다.
FINANCE_EXPLANATIONS: dict[str, str] = {
    "PRE_BOUNDARY": (
        "현재 보유 현금과 앞으로 예정된 입출금을 함께 반영해 "
        "이번에 매입에 쓸 수 있는 금액을 정리했습니다."
    ),
    "SCENARIO_ACCEPT": (
        "현재 자금 상태와 예상 현금흐름을 기준으로 보면 "
        "제안하신 매입 조건은 그대로 진행하실 수 있습니다."
    ),
    # ★ `conditional` 을 `ok` 와 같은 문장으로 묶지 않는다. 두 결과는 사용자가 할 일이
    #   다르다 — 하나는 그대로 진행이고, 하나는 조정한 뒤 다시 보는 것이다.
    "SCENARIO_CONDITIONAL": (
        "평소 흐름이라면 감당할 수 있지만, 대금 회수가 늦어지는 상황까지 "
        "가정하면 운영에 필요한 자금이 빠듯해집니다. "
        "매입 금액이나 지급 시기를 조정하시면 안전하게 진행하실 수 있습니다."
    ),
    "SCENARIO_REJECT": (
        "현재 자금 상태에서는 제안하신 조건 그대로 매입을 진행하기 어렵습니다. "
        "진행할 수 있는 조정 범위가 있는 경우에는 함께 안내해 드렸습니다."
    ),
}

#: 필요한 재무 정보가 없어 답을 내지 못한 경우.
#:
#: ★ 무엇이 없는지는 `missing_data` 에 식별자로 남는다. 문장에는 담지 않는다 —
#:   `purchase_payment_days` 같은 이름은 읽는 사람에게 아무것도 알려 주지 않는다.
NOT_READY = (
    "재무 검토에 필요한 정보가 확인되지 않아 지금은 정확한 판단을 내리기 어렵습니다. "
    "필요한 자료가 준비된 뒤에 다시 검토해야 합니다."
)

CONTEXT_UNAVAILABLE = (
    "현재 자금 현황 자료를 확인하지 못해 재무 검토를 진행하지 못했습니다. "
    "자료가 준비된 뒤에 다시 검토해야 합니다."
)

AS_OF_MISMATCH = (
    "보유한 자금 자료의 기준일이 요청하신 날짜와 달라 지금은 검토를 진행할 수 없습니다. "
    "해당 날짜의 자금 현황이 반영된 뒤에 다시 검토해야 합니다."
)

PAYROLL_SOURCE_MISSING = (
    "인건비 지급 계획의 근거를 확인하지 못해 앞으로의 현금흐름을 계산할 수 없습니다. "
    "이 정보가 확인된 뒤에 다시 검토해야 합니다."
)

PAYMENT_DAYS_MISSING = (
    "매입 대금을 언제 지급하는지에 대한 기준이 없어 매입 가능 금액을 계산할 수 없습니다. "
    "지급 기준이 정해진 뒤에 다시 검토해야 합니다."
)

SCENARIO_SCHEMA_MISSING = (
    "매입 제안을 읽어 들일 기준이 아직 정해지지 않아 이번 안건은 판정하지 못했습니다."
)

MODE_NOT_SUPPORTED = "요청하신 종류의 재무 검토는 아직 제공되지 않습니다."

#: 전달받은 요청 자체가 재무 검토 기준에 맞지 않는 경우.
#:
#: ★ `RUNTIME_NOT_READY` 와 구분한다. 저쪽은 *"자료가 아직 없다"* 라 기다리면 되고,
#:   이쪽은 *"보내 주신 내용이 맞지 않는다"* 라 요청을 고쳐야 한다 — 다음에 할 일이
#:   다르므로 같은 문장으로 뭉뚱그리지 않는다.
INVALID_REQUEST = (
    "전달받은 매입 조건이 재무 검토 기준에 맞지 않아 검토를 진행하지 못했습니다. "
    "매입 금액과 지급 일정을 다시 확인해 주시기 바랍니다."
)

INVALID_REQUEST_AS_OF = (
    "매입 제안의 기준일이 이번 요청의 기준일과 달라 검토를 진행하지 못했습니다. "
    "같은 기준일로 다시 요청해 주시기 바랍니다."
)

#: 우리 쪽 사정으로 검토를 끝내지 못한 경우.
#:
#: ★ 원인은 사용자가 할 수 있는 일이 아니다. 그래서 무엇이 잘못됐는지 늘어놓지 않고
#:   **다음에 할 일**만 말한다. 기술적 사유는 실행 Trace 에 그대로 남는다.
INTERNAL_FAILURE = (
    "재무 검토를 끝까지 진행하지 못했습니다. 잠시 후 다시 요청해 주시기 바랍니다."
)

RESULT_NOT_TRUSTWORTHY = (
    "재무 검토 결과를 확인하는 과정에서 문제가 발견되어 결과를 내보내지 않았습니다. "
    "잠시 후 다시 요청해 주시기 바랍니다."
)

PERSISTENCE_FAILED = (
    "재무 검토 기록을 저장하지 못해 결과를 확정하지 못했습니다. "
    "잠시 후 다시 요청해 주시기 바랍니다."
)

#: 실행이력 조회에서 해당 기록을 찾지 못한 경우 (HTTP 404 본문).
#:
#: ★ 상태 코드는 그대로 `404` 다 — 그것이 기계가 읽는 값이고, 문장만 사람 몫이다.
RUN_NOT_FOUND = "요청하신 재무 검토 기록을 찾지 못했습니다."

#: "지금 자금 상황" 조회 응답.
STATUS_QUERY = "현재 보유 현금과 자금 현황을 정리했습니다."

STATUS_QUERY_PARTIAL = (
    "현재 보유 현금은 확인했지만, 인건비 지급 근거가 없어 "
    "앞으로의 현금흐름은 계산하지 못했습니다."
)

#: 시나리오 판정 사유 (`payload.verdicts[].reason`). 판정 자체는 Rule 이 정한다 —
#: 여기 있는 것은 **그 판정을 사람 말로 옮긴 문장**뿐이다.
BASE_MINIMUM_CASH_VIOLATED = (
    "이번 매입과 무관하게 현재 자금 흐름 자체가 운영에 필요한 최소 자금에 못 미칩니다."
)

SCENARIO_REASON_OK = (
    "평소 흐름과 대금 회수가 늦어지는 상황 모두에서 운영 자금이 부족해지지 않습니다."
)

SCENARIO_REASON_CONDITIONAL = (
    "평소 흐름에서는 감당할 수 있지만, 대금 회수가 늦어지면 운영 자금이 부족해질 수 있습니다."
)

SCENARIO_REASON_REJECT = "평소 흐름에서도 운영에 필요한 자금이 부족해집니다."


def explanation_keys(mode: FinanceMode, business_status: str) -> list[str]:
    """이 결과에 **고를 수 있는** 설명 키.

    ★ 한 벌만 만든다. Provider 별로 따로 적으면 같은 재무 결과가 Provider 에 따라 다른
      설명을 고를 수 있게 열린다 — 결과는 같은데 사용자에게 다른 말이 나가는 것이다.
    """
    if mode == "PRE_PURCHASE":
        return ["PRE_BOUNDARY"]
    if business_status == "reject":
        return ["SCENARIO_REJECT"]
    if business_status == "conditional":
        return ["SCENARIO_CONDITIONAL"]
    return ["SCENARIO_ACCEPT"]


def explanation_for(mode: FinanceMode, business_status: str) -> str:
    """결정론 확정 설명. LLM 이 답하지 못해도 **사용자는 같은 뜻의 문장을 받는다.**

    🔴 LLM 경로만 다듬고 대체 경로를 두면, 모델이 죽은 날에만 사용자에게 다른 말투가
       나간다 — 가장 설명이 필요한 날에 설명이 제일 나빠진다.
    """
    return FINANCE_EXPLANATIONS[explanation_keys(mode, business_status)[0]]
