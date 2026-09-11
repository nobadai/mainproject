"""
sales_approval.py — 판매 승인 → **판매 확정** (`confirm_sale`)

사람이 판매안을 승인하면 그 안이 `sales` · `sale_items` 에 `CONFIRMED` 로 선다.
그 자리가 여기다.

```text
사용자 승인
 → 선택 scenario 를 **그 실행의 as_of** 로 재검증 (revalidation.revalidate_scenario)
 → PASSED 면 confirm_sale()
 → sales · sale_items · CONFIRMED
 → 그 뒤는 기존 orchestration (예약 → FEFO → 출고 → DELIVERED → 채권)
```

🔴 **`transition.apply_approval` 을 재사용하지 않는다.** 저쪽은 매입 약정
   (`ApprovedCommitment`)을 받아 재무·물류 장부를 바꾼다 — 판매 확정은 받는 것도
   바꾸는 것도 다르다. 한 함수로 묶으면 그 함수가 두 사이클의 장부를 다 알게 된다.

🔴 **승인 즉시 = 판매 확정. 승인 즉시 ≠ 출고 즉시** (2026-09-08 계약).
   여기서 서는 것은 `sales.order_status = 'CONFIRMED'` 하나이고, 예약·FEFO·출고·
   `DELIVERED`·채권은 **그 뒤 기존 orchestration** 이 자기 날에 한다. 이 모듈이
   출고를 부르면 승인이 곧 출고가 되고, 그러면 *"승인했지만 아직 안 나갔다"* 라는
   상태가 사라진다.

🔴 **없는 값을 지어내지 않는다.** `delivery_date` · `payment_days` 가 비어 있으면
   `BLOCKED` 이고 **무엇이 없는지 이름을 부른다.** `as_of + N` 같은 값을 만들면 그
   `N` 이 곧 업무 규칙이 되고, 그 날짜가 출고일·수금일·곡선의 시점이 된다 — 아무도
   정한 적이 없는데.

★ **판매가 재무·물류를 직접 부르지 않는다.** 잇는 것은 마스터다 (§3.2.2). 이 모듈이
  아는 판매 쪽 이름은 **발표된 입력 계약**(`SalesConfirmationInput`)과 그것을 받는
  함수(`confirm_sale`) 둘뿐이고, 무슨 값을 어느 칸에 어떤 SQL 로 쓸지는 판매가 안다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.master.envelope import Capability
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.sales.persistence import (
    SalesPersistenceConflict,
    confirm_sale,
)
from app.sales.schemas import (
    SalesApprovalLine,
    SalesConfirmationInput,
    SalesExecutionIdentity,
    SalesScenario,
)

__all__ = [
    "CONTRACT_FULFILLMENT_MODE",
    "REQUEST_MISSING_PREFIX",
    "REQUIRED_COMMERCIAL_TERMS",
    "REQUIRED_FINANCIAL_SUMMARY_FIELDS",
    "TERMS_UNRESOLVED_PREFIX",
    "SaleConfirmationOut",
    "confirm_approved_sale",
    "financial_summary_of",
    "missing_commercial_terms",
    "missing_financial_summary_fields",
    "missing_financial_summary_reason",
    "missing_term_origin_vocabulary",
    "missing_term_origins",
    "missing_terms_reason",
    "preferred_request_field",
    "term_of_origin",
]


#: 🔴 **승인 가능한 후보의 필수조건.** 이 칸이 비어 있으면 사용자가 골라도 판매를
#: 확정할 수 없다 — `confirm_sale` 이 `sale_date` 와 수금 기일을 여기서 만든다
#: (`app/sales/persistence.py`: `due_date = sale_date + payment_days`).
#:
#: ★ **주인이 여기 하나다.** `sales_flow.CandidateVerdict` 가 후보를 사용자에게 올릴
#:   때 같은 목록을 읽는다 — 두 곳에 베껴 두면 *"올려도 되는 안"* 과 *"확정할 수 있는
#:   안"* 이 갈리는 날이 온다.
REQUIRED_COMMERCIAL_TERMS: tuple[str, ...] = ("delivery_date", "payment_days")

#: 사람이 읽는 이름. **칸 이름을 같이 적는다** — 화면이 무엇을 채워야 하는지 알아야 한다.
_TERM_NAMES: Mapping[str, str] = {
    "delivery_date": "납품일(delivery_date)",
    "payment_days": "수금 유예일(payment_days)",
}


def missing_commercial_terms(scenario: Mapping[str, Any]) -> tuple[str, ...]:
    """확정에 필요한 상업조건 중 **비어 있는 칸 이름**. 다 있으면 빈 튜플이다.

    ★ **`None` 만 없는 것으로 센다** (빈 문자열도 같이 센다 — JSON 왕복에서 날짜가
      `""` 로 오는 경우가 있다). `0` 은 없는 값이 아니다 — `payment_days=0` 은
      *"당일 수금"* 이라는 **정해진 조건**이다. `falsy` 로 세면 그 안이 조용히
      *"조건이 없는 안"* 이 된다.
    """
    return tuple(
        field
        for field in REQUIRED_COMMERCIAL_TERMS
        if scenario.get(field) is None or scenario.get(field) == ""
    )


#: 🔴 **호출에서 안 실렸다.** 부르는 쪽이 `preferred_<FIELD>` 를 안 보냈다 —
#: **고칠 사람은 화면 · 걷기 · API 호출자**다. 판매에 물어봐야 소용이 없다.
REQUEST_MISSING_PREFIX = "REQUEST_MISSING_"

#: 🔴 **정말 조건이 없다.** 보냈는데 결과에 안 실렸거나, 계약 이행 경로라 계약값에서
#: 나와야 하는데 안 나왔다 — **고칠 사람은 판매 · 계약**이다.
TERMS_UNRESOLVED_PREFIX = "TERMS_UNRESOLVED_"

#: 🔴 **계약 이행 경로.** 이 모드는 계약에 적힌 값을 쓰므로 마스터가 `preferred_*` 를
#: **안 보내는 것이 정상**이다. 여기서 `REQUEST_MISSING` 을 내면 없는 잘못을 화면에
#: 씌우는 것이 된다 — 호출자는 보낼 것이 없었다.
#:
#: ⚠️ **어휘의 주인은 판매다** (`SalesBusinessMode`). 그런데 `app.master.schemas` 를
#:   import 할 수 없다 — 저쪽이 이 모듈이 사는 `sales_flow` 를 import 하므로 고리가
#:   된다. 값이 갈리면 `test_missing_term_origin.py` 가 잡는다.
CONTRACT_FULFILLMENT_MODE = "CONTRACT_FULFILLMENT"


def preferred_request_field(field: str) -> str:
    """상업조건 칸 이름 → **사용자 요청에서 그 값을 싣는 칸 이름.**

    ```text
    delivery_date  ↔  user_request["preferred_delivery_date"]
    payment_days   ↔  user_request["preferred_payment_days"]
    ```

    ★ **대응이 여기 한 자리다.** 두 곳에 적으면 판매가 칸 이름을 바꾼 날 한쪽만
      고쳐지고, 그러면 *"보냈는데 안 실렸다"* 가 조용히 *"안 보냈다"* 로 뒤집힌다.
    """
    return f"preferred_{field}"


def missing_term_origin_vocabulary() -> tuple[str, ...]:
    """이 판이 낼 수 있는 어휘 전부. **`REQUIRED_COMMERCIAL_TERMS` 에서 나온다.**

    ★ **손으로 나열하지 않는다.** 상수가 늘면 어휘도 같이 늘어야 한다 — 베껴 두면
      새 칸이 하나 늘 때 그 칸만 원인 없이 나가는 날이 온다.
    """
    return tuple(
        f"{prefix}{field}"
        for field in REQUIRED_COMMERCIAL_TERMS
        for prefix in (REQUEST_MISSING_PREFIX, TERMS_UNRESOLVED_PREFIX)
    )


def term_of_origin(origin: str) -> str:
    """어휘에서 **칸 이름을 되꺼낸다.** 화면이 필드 이름만 필요할 때 쓴다.

    ★ 접두를 몰라도 되게 여기서 벗긴다 — 화면이 문자열을 직접 자르기 시작하면
      접두를 바꾸는 날 화면이 조용히 틀린 이름을 보여 준다.
    """
    for prefix in (REQUEST_MISSING_PREFIX, TERMS_UNRESOLVED_PREFIX):
        if origin.startswith(prefix):
            return origin[len(prefix) :]
    return origin


def missing_term_origins(
    scenario: Mapping[str, Any],
    *,
    user_request: Mapping[str, Any] | None = None,
    business_mode: str | None = None,
) -> tuple[str, ...]:
    """비어 있는 상업조건이 **어디서 끊겼는지.** 다 있으면 빈 튜플이다.

    ★ **더하는 것은 "어디서" 하나다.** *"무엇이 없나"* 는 `missing_commercial_terms`
      가 답하고 여기서 다시 세지 않는다 — 두 곳에서 세면 갈린다.

    ```text
    business_mode 가 CONTRACT_FULFILLMENT 가 **아니고**
      · user_request 에 preferred_<FIELD> 가 없음   → REQUEST_MISSING_<FIELD>
    그 밖 (계약 이행 경로거나 · 보냈는데 결과에 없음) → TERMS_UNRESOLVED_<FIELD>
    ```

    🔴 **`CONTRACT_FULFILLMENT` 를 가른다.** 그 경로는 계약값을 쓰므로 마스터가
      `preferred_*` 를 안 보내는 것이 정상이다 — 거기서 `REQUEST_MISSING` 을 내면
      **거짓말이 된다.**

    ⚠️ **`user_request` 를 안 주면 `REQUEST_MISSING` 이다.** 그것도 사실이다 —
      호출에 그 값이 실리지 않았다. 부를 때 들고 있으면 반드시 넘긴다.
    """
    return tuple(
        _origin_of(field, user_request=user_request, business_mode=business_mode)
        for field in missing_commercial_terms(scenario)
    )


def _origin_of(
    field: str,
    *,
    user_request: Mapping[str, Any] | None,
    business_mode: str | None,
) -> str:
    """칸 하나의 원인. **가르는 규칙은 여기 한 줄이다.**"""
    if business_mode != CONTRACT_FULFILLMENT_MODE and not _was_requested(field, user_request):
        return f"{REQUEST_MISSING_PREFIX}{field}"
    return f"{TERMS_UNRESOLVED_PREFIX}{field}"


def _was_requested(field: str, user_request: Mapping[str, Any] | None) -> bool:
    """부르는 쪽이 그 값을 **실었는가.**

    ★ **`missing_commercial_terms` 와 같은 셈법이다.** `0` 은 실린 값이다 —
      `preferred_payment_days=0` 은 *"당일 수금을 원한다"* 는 **정해진 요청**이고,
      `falsy` 로 세면 그 요청이 조용히 *"안 보냈다"* 가 되어 판매의 잘못이 화면
      잘못으로 뒤집힌다.
    """
    if user_request is None:
        return False
    value = user_request.get(preferred_request_field(field))
    return value is not None and value != ""


def missing_terms_reason(origins: tuple[str, ...]) -> str:
    """왜 확정할 수 없나 — **무엇이 없는지 이름을 부르고 어디서 끊겼는지 붙인다.**

    ★ 부서 판정 문장(`capability(runtime/business)`)과 **모양이 다르다.** 탈락 사유가
      *"재무가 반려"* 처럼 보이면 사람이 재무를 본다. *"납품일이 없다"* 로 보여야
      판매를 본다.

    ★ **한 함수가 두 경우를 다 낸다.** 문장을 두 벌로 두면 한쪽만 고치는 날이 온다.

    :param origins: `missing_term_origins` 가 낸 어휘. 접두 없는 칸 이름을 넣어도
        읽히지만 그때는 원인 절이 안 붙는다 — 원인을 아는 자리에서 부른다.
    """
    names = ", ".join(_TERM_NAMES.get(term_of_origin(o), term_of_origin(o)) for o in origins)
    parts = [f"{names} 이(가) 없어 판매를 확정할 수 없다 — 없는 값을 지어내지 않는다."]
    requested = _names_with(origins, REQUEST_MISSING_PREFIX)
    unresolved = _names_with(origins, TERMS_UNRESOLVED_PREFIX)
    if requested:
        parts.append(f"요청에 안 실렸다: {requested} — 화면·호출자가 채운다.")
    if unresolved:
        parts.append(f"조건이 정해지지 않았다: {unresolved} — 판매·계약이 정한다.")
    return " ".join(parts)


def _names_with(origins: tuple[str, ...], prefix: str) -> str:
    """그 원인에 해당하는 칸들의 사람 이름. 없으면 빈 문자열이다."""
    fields = [term_of_origin(o) for o in origins if o.startswith(prefix)]
    return ", ".join(_TERM_NAMES.get(field, field) for field in fields)


# ---------------------------------------------------------------------------
# 🔴 재무가 낸 기여이익 — **되먹임이 없는 안에는 이 길뿐이다** (2026-09-11)
# ---------------------------------------------------------------------------


#: 재검증에서 이 값을 낸 검증. 🔴 **어휘의 주인은 `envelope.Capability` 다** —
#:   `Literal` 이라 오타가 타입 검사에서 걸린다.
_FINANCIAL_VALIDATION: Capability = "FINANCIAL_VALIDATION"

#: 확정에 필요한 재무 요약 칸.
#:
#: ★★ **왜 이 길이 유일한가** (실측 2026-09-11).
#:
#:   ```text
#:   app/sales/persistence.py:380  _line_profit
#:     ① line.contribution_profit_krw      ← 마스터가 넘긴다   ← 🟢 이 길
#:     ② scenario.contribution_margin_krw  ← **비어 있다**
#:     ③ 없으면 SalesPersistenceConflict("missing contribution profit")
#:   ```
#:
#:   ②가 비는 이유는 사고가 아니라 **설계다.** 판매는 그 값을 되먹임 회신에서 받아
#:   적는데(`proposal.py:216`), **통과한 후보는 되먹임을 안 받는다** (계약 `C-1` ·
#:   `sales_flow:723`). 실측에서 `feedback_attempts` 전건 0 · 후보 `revision` 전건 0
#:   이었다. 그래서 **통과한 안은 영원히 확정될 수 없었다.**
#:
#:   🔴 **`C-1` 은 옳아서 안 건드렸다.** 통과한 안에 되먹임을 걸면 사용자가 볼 수
#:     있던 안이 바뀐다. 고리는 **마스터가 값을 날라서** 푼다.
REQUIRED_FINANCIAL_SUMMARY_FIELDS: tuple[str, ...] = (
    "contribution_margin_krw",
    "contribution_margin_rate",
)

#: 사람이 읽는 이름. `_TERM_NAMES` 와 같은 자리다.
_SUMMARY_NAMES: Mapping[str, str] = {
    "contribution_margin_krw": "기여이익(contribution_margin_krw)",
    "contribution_margin_rate": "기여이익률(contribution_margin_rate)",
}


def financial_summary_of(
    validations: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    """재검증 판정에서 **재무가 낸 요약**을 꺼낸다. 없으면 `None`.

    ```text
    validations["FINANCIAL_VALIDATION"]["payload"]["financial_summary"]
    ```

    🔴 **재검증이 낸 것이다 — 첫 검증이 아니다.** 확정은 재검증 **뒤에** 서므로,
      재검증이 그날 사실로 다시 센 값이 정본이다. 첫 검증 값
      (`candidates[].validations…`)을 쓰면 **「제안 시점 사실」로 장부가 서고**, 그
      사이 재고·원가가 움직인 것이 사라진다.

    ★ **여기가 이 매핑의 주인이다.** 부르는 쪽(`decision_service._sale_for`)이 세
      겹을 직접 파고들면 재무가 payload 모양을 바꾸는 날 그 자리가 조용히 `None` 이
      된다 — 그리고 `None` 은 *"재무가 안 냈다"* 와 구별되지 않는다.

    ⚠️ **`payload` 는 2026-09-11 에야 열렸다** (`revalidation._verdict_of`). 그전에는
      재검증이 그 칸을 버려서 여기서 꺼낼 것이 아무것도 없었다.
    """
    if validations is None:
        return None
    verdict = validations.get(_FINANCIAL_VALIDATION)
    if not isinstance(verdict, Mapping):
        return None
    payload = verdict.get("payload")
    if not isinstance(payload, Mapping):
        return None
    summary = payload.get("financial_summary")
    return summary if isinstance(summary, Mapping) else None


def missing_financial_summary_fields(
    financial_summary: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """확정에 필요한 재무 칸 중 **비어 있는 칸 이름**. 다 있으면 빈 튜플이다.

    ★ **`missing_commercial_terms` 와 같은 모양이다** — `None` 과 빈 문자열만 없는
      것으로 센다. `0` 은 없는 값이 아니다: `contribution_margin_krw=0` 은
      *"기여이익이 0 으로 확인됐다"* 는 **정해진 사실**이고, `falsy` 로 세면 그
      사실이 조용히 *"재무가 안 냈다"* 가 된다.

    🔴 **요약 자체가 없으면 두 칸 다 없는 것이다.** 통째로 `None` 인 것을 빈 튜플로
      내면 *"다 있다"* 가 되어 그 다음 줄이 `None` 을 장부에 싣는다.
    """
    if financial_summary is None:
        return REQUIRED_FINANCIAL_SUMMARY_FIELDS
    return tuple(
        field
        for field in REQUIRED_FINANCIAL_SUMMARY_FIELDS
        if financial_summary.get(field) is None or financial_summary.get(field) == ""
    )


def missing_financial_summary_reason(fields: tuple[str, ...]) -> str:
    """왜 확정할 수 없나 — **무엇이 없는지 이름을 부르고 누가 채우는지 붙인다.**

    ★ **`missing_terms_reason` 과 같은 모양이고 문장만 다르다.** 저쪽은 화면·판매가
      채우는 칸이고 여기는 **재무가 내는 값**이다 — 사람이 다음에 볼 자리가 다르므로
      문장이 그 자리를 가리켜야 한다.

    🔴 **0 으로 채우고 통과시키지 않는다.** 기여이익 0 으로 장부가 서면 그날의
      손익이 거짓이 되고, 그 거짓은 터지지 않는다 — 숫자만 틀린다.
    """
    names = ", ".join(_SUMMARY_NAMES.get(field, field) for field in fields)
    return (
        f"재무 판정에 {names} 이(가) 없어 판매를 확정할 수 없다"
        " — 없는 값을 지어내지 않는다. 재검증의 FINANCIAL_VALIDATION 이 채운다."
    )


def _decimal_of(value: Any) -> Decimal | None:
    """재무가 낸 수를 **그대로** `Decimal` 로 옮긴다. 못 옮기면 `None`.

    🔴 **계산이 아니다.** 수량 × 단가 − 원가 를 여기서 세면 그 순간 마스터가 재무가
      된다 (설계 ④). 이 함수는 **표현만** 바꾼다 — 값도 자릿수도 안 건드린다.

    ⚠️ **경로마다 타입이 다르다.** in-process 에서는 재무가 낸 `Decimal` 이 그대로
      오고, 이력을 한 번 왕복하면 `float` 나 문자열이 온다. `str` 을 거쳐 옮기는
      것이 `Decimal(float)` 의 이진 꼬리를 안 들이는 유일한 길이다.

    ⚠️ **`bool` 을 막는다.** 파이썬에서 `True` 는 `1` 이라 `Decimal(str(True))` 가
      아니라 그 앞에서 거른다 — 판매 스키마도 `_reject_boolean` 으로 같은 자리를
      막는다.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class SaleConfirmationOut(BaseModel):
    """판매 승인 1건이 원장에 남긴 결과.

    🔴 **세 값을 섞지 않는다** (`TransitionOut` 과 같은 규율).

      ```text
      CONFIRMED   sales · sale_items 가 섰다
      BLOCKED     확정할 수 없었다 — 아무것도 안 썼다 (재검증 미통과 · 없는 상업조건)
      FAILED      쓰려다 실패했다 — 롤백했다
      ```

      `BLOCKED` 를 `FAILED` 로 접으면 *"값이 없어 못 한 것"* 이 장애로 읽히고,
      반대로 접으면 **실패한 확정이 "조건이 없었다" 로 조용히 묻힌다.**
    """

    status: Literal["CONFIRMED", "BLOCKED", "FAILED"]
    reason: str = ""

    #: 🔴 **비어 있지 않으면 그것이 막은 이유다.** 부서 판정과 다른 칸에 둔다 —
    #: `validations` 에 가짜 항목을 밀어 넣으면 화면이 부서를 보러 간다.
    #:
    #: ★ **담는 것은 원인 어휘다** (`missing_term_origins`) — 칸 이름이 아니다.
    #:   `REQUEST_MISSING_<FIELD>` · `TERMS_UNRESOLVED_<FIELD>` 이고, 칸 이름은
    #:   `term_of_origin` 으로 되꺼낸다. 이름만 담으면 화면이 *"누가 고쳐야 하나"*
    #:   를 다시 추측하게 된다.
    missing_terms: list[str] = Field(default_factory=list)

    sale_id: str | None = None
    sale_item_id: str | None = None

    #: 🔴 **출고는 여기서 안 한다.** 확정과 출고가 같은 클릭에 붙으면 *"승인했지만
    #: 아직 안 나갔다"* 라는 상태가 사라진다 — 그 뒤는 기존 orchestration 이다.
    shipped: bool = False


def confirm_approved_sale(
    *,
    request_id: str,
    run_id: str,
    as_of: date | None,
    policy_version: str | None,
    scenario: Mapping[str, Any],
    revalidation_outcome: str | None,
    financial_summary: Mapping[str, Any] | None,
    confirm: Callable[[Any, SalesConfirmationInput], Any] | None = None,
    connect: Callable[[], Any] | None = None,
) -> SaleConfirmationOut:
    """승인된 판매안을 **판매 원장에 확정**한다.

    순서가 이 함수의 전부다.

    ```text
    1. 재검증 통과 확인   PASSED 가 아니면 여기서 끝난다 — confirm_sale 을 안 부른다
    2. 상업조건 확인      없는 값을 지어내지 않는다 → BLOCKED (이름을 부른다)
    3. 기여이익 확인      재무가 안 냈으면 지어내지 않는다 → BLOCKED (이름을 부른다)
    4. 기준일 확인        order_date 는 **그 실행의 as_of** 다 — 벽시계가 아니다
    5. 입력 계약 조립     커넥션 밖에서 (실패해도 DB 를 안 건드린다)
    6. confirm_sale       한 커넥션 · commit 한 번 · 실패하면 rollback
    ```

    🔴 **재검증이 막히면 부르지 않는다.** `CONDITIONAL` 도 통과가 아니다 — 사용자가
      승인한 대상은 **그때 화면에 있던 그 안**이고, 새 조건이 붙으면 다른 안이다
      (`decision.RevalidationOutcome`).

    🔴 **예외를 밖으로 던지지 않는다.** 이 함수가 불릴 때 결정은 **이미 적재됐다.**
      확정 실패가 예외로 올라가면 라우터가 500 을 내고, 사람이 보기에는 승인이 실패한
      것이 된다 — 실제로는 승인은 남았고 원장만 안 선 것이다 (`apply_approval` 과
      같은 규율).

    :param as_of: 🔴 **그 실행의 기준일.** `order_date` 가 되고, 벽시계가 아니다
        (판매·재무 확정 ③). 못 읽으면 `None` 이고 그때는 `BLOCKED` 다.
    :param financial_summary: 🔴 **재검증의 재무 판정이 낸 요약**
        (`financial_summary_of` 가 꺼낸다). 기여이익과 기여이익률이 여기서 온다 —
        되먹임을 안 받는 안에는 이 길뿐이다. **기본값이 없다**: 안 넘기면 터져야
        한다. 없으면(`None`) 지어내지 않고 `BLOCKED` 다.
    :param confirm: 확정 함수. 안 주면 `app.sales.persistence.confirm_sale` 이다.
    :param connect: 커넥션 팩토리. 안 주면 `app.sales.db.get_connection` 이다.
    """
    if revalidation_outcome != "PASSED":
        # 🔴 여기서 돌아선다 — **`confirm_sale` 을 부르지 않고 커넥션도 안 연다.**
        shown = revalidation_outcome or "안 돌았다"
        return SaleConfirmationOut(
            status="BLOCKED",
            reason=f"재검증이 통과하지 않아 판매를 확정하지 않았다 (재검증 결과: {shown}).",
        )

    missing = missing_term_origins(scenario)
    if missing:
        return SaleConfirmationOut(
            status="BLOCKED",
            reason=missing_terms_reason(missing),
            missing_terms=list(missing),
        )

    # 🔴 **기여이익을 지어내지 않는다** (2026-09-11). 0 으로 채우면 그날 손익이
    #    거짓이 되고, 그 거짓은 터지지 않는다 — 숫자만 틀린다.
    #
    # ⚠️ **`missing_terms` 에 안 담는다.** 저 칸의 어휘는 `missing_term_origins` 가
    #    내는 `REQUEST_MISSING_*` · `TERMS_UNRESOLVED_*` 이고 *"화면·판매·계약이
    #    채운다"* 는 뜻이다. 재무가 안 낸 값을 그 어휘에 섞으면 사람이 엉뚱한 파트를
    #    보러 간다 — 사유 문장이 재무를 가리킨다.
    없는재무칸 = missing_financial_summary_fields(financial_summary)
    if 없는재무칸:
        return SaleConfirmationOut(
            status="BLOCKED",
            reason=missing_financial_summary_reason(없는재무칸),
        )

    if as_of is None:
        # ⚠️ **오늘로 대신 채우지 않는다.** `order_date` 는 그 실행이 선 날이고,
        #   못 읽었으면 모르는 것이다 — 모르는 날짜를 지어내면 수금 곡선의 시점이 틀린다.
        return SaleConfirmationOut(
            status="BLOCKED",
            reason="원 실행의 기준일(as_of)을 못 읽어 주문일을 정할 수 없다.",
        )

    try:
        # 🔴 **커넥션 밖에서 조립한다** (`apply_approval` 과 같은 규율). 계약 위반은
        #   흔한 일인데, 커넥션을 연 뒤에 터지면 열린 트랜잭션이 남는다.
        confirmation = _confirmation_input(
            request_id=request_id,
            run_id=run_id,
            as_of=as_of,
            policy_version=policy_version,
            scenario=scenario,
            financial_summary=financial_summary,
        )
    except (ValidationError, SalesPersistenceConflict, ValueError) as exc:
        # ★ **`FAILED` 가 아니다.** 계약이 안 맞아 쓸 수 없는 것은 우리가 아는
        #   사실이지 실패가 아니다.
        return SaleConfirmationOut(
            status="BLOCKED", reason=f"판매 확정 입력을 만들 수 없다: {exc}"
        )

    do_confirm = confirm_sale if confirm is None else confirm
    conn = _open(connect)
    try:
        result = do_confirm(conn, confirmation)
        conn.commit()
    except SalesPersistenceConflict as exc:
        # ★ 판매가 *"이 사실로는 확정할 수 없다"* 고 말한 것이다. 문장은 판매가 쓴
        #   것을 그대로 옮긴다 — 마스터가 다시 쓰면 사유의 주인이 둘이 된다.
        conn.rollback()
        return SaleConfirmationOut(status="BLOCKED", reason=f"판매가 확정을 막았다: {exc}")
    except Exception as exc:  # noqa: BLE001 - 확정 실패가 적재된 결정을 지우면 안 된다.
        conn.rollback()
        return SaleConfirmationOut(status="FAILED", reason=f"판매 확정 적재 실패: {exc}")
    finally:
        conn.close()

    return SaleConfirmationOut(
        status="CONFIRMED",
        reason="판매를 확정했다 (CONFIRMED). 출고는 이 승인이 하지 않는다.",
        sale_id=getattr(result, "sale_id", None),
        sale_item_id=getattr(result, "sale_item_id", None),
    )


def _open(connect: Callable[[], Any] | None) -> Any:
    """판매 원장 커넥션. **판매 것을 쓴다** — 마스터가 자기 것을 열지 않는다."""
    if connect is not None:
        return connect()
    from app.sales.db import get_connection

    return get_connection()


def _confirmation_input(
    *,
    request_id: str,
    run_id: str,
    as_of: date,
    policy_version: str | None,
    scenario: Mapping[str, Any],
    financial_summary: Mapping[str, Any],
) -> SalesConfirmationInput:
    """`SalesConfirmationInput` 을 짓는다. **판매가 발표한 계약 그대로다.**

    ```text
    sale_date   scenario 의 delivery_date        납품일 정본은 sales.sale_date (판매 확정)
    order_date  그 실행의 as_of                   판매·재무 확정 ③ — 벽시계가 아니다
    sim_run_id  ledger_repository.BURN_IN_SIM_RUN_ID   어느 실행의 장부인가는 마스터가 정한다
    line.기여이익  재검증의 재무 판정               🔴 되먹임 없는 안에는 이 길뿐이다
    ```

    🔴 **옛 주석이 거짓이었다** (2026-09-11). 그 문장은 이랬다.

      ```text
      ★ 기여이익을 마스터가 다시 적지 않는다. line.contribution_profit_krw 와
        contribution_margin_rate 를 비워 두면 판매가 scenario 값을 쓴다
        (_line_profit). 여기에 값을 베껴 넣으면 같은 사실이 두 곳에 남는다.
      ```

      ★★ **통과 경로를 안 본 문장이다.** *"판매가 scenario 값을 쓴다"* 가 참이려면
        `scenario.contribution_margin_krw` 에 값이 있어야 하는데, **통과한 안에는 그
        값이 없다.** 판매는 그 값을 되먹임 회신에서 받아 적고(`proposal.py:216`),
        **통과한 후보는 되먹임을 안 받는다** (계약 `C-1`). 고리가 닫혀 있었고,
        그래서 통과한 안은 영원히 확정될 수 없었다 (실측: `sales` 0행).

      🟢 **지금도 마스터가 값을 「짓지」는 않는다.** 재무가 낸 것을 **읽어서 나른다** —
        수량 × 단가 − 원가 를 여기서 세지 않는다. 그 순간 마스터가 재무가 된다.
        `tests/master/test_finance_margin_carried.py` 가 이 함수 안에 산술 연산이
        없는지를 AST 로 지킨다.

      ⚠️ **같은 사실이 두 곳에 남는 것**은 맞다. 주인은 **재무**이고 여기는 그것을
        옮기는 자리다 — 그래서 값을 고르지도 고치지도 않고 두 칸을 그대로 옮긴다.

    ★ **`grade` 는 `None` 이다.** `SalesScenario` 에 등급 칸이 없다 — 없는 값을
      지어내지 않는다.

    :param financial_summary: 재검증의 재무 판정이 낸 요약. 🔴 **비어 있지 않은
        것은 부르는 쪽이 이미 확인했다** (`missing_financial_summary_fields`).
    """
    selected = SalesScenario.model_validate(dict(scenario))
    if selected.delivery_date is None:
        # ★ `missing_commercial_terms` 가 먼저 막는다. 여기는 최후 방어다 — 그 검사를
        #   지우면 이 줄이 터져서 알려 준다.
        raise SalesPersistenceConflict("selected scenario is missing delivery_date")
    if selected.quantity_kg is None or selected.unit_price_krw is None:
        raise SalesPersistenceConflict("selected scenario is missing quantity or unit price")
    return SalesConfirmationInput(
        execution_identity=SalesExecutionIdentity(
            request_id=request_id,
            run_id=run_id,
            as_of=as_of,
            policy_version=policy_version,
        ),
        selected_scenario=selected,
        selected_scenario_id=selected.scenario_id,
        sim_run_id=BURN_IN_SIM_RUN_ID,
        sale_date=selected.delivery_date,
        order_date=as_of,
        line=SalesApprovalLine(
            item_name=selected.item,
            quantity_kg=selected.quantity_kg,
            unit_price_krw_per_kg=selected.unit_price_krw,
            grade=None,
            # 🔴 **재무가 낸 것을 그대로 옮긴다.** `_line_profit` 이 이 칸을 **먼저**
            #    보고, 없으면 `scenario.contribution_margin_krw` 를 보는데 통과한
            #    안에는 그 값이 없다 (계약 `C-1`).
            contribution_profit_krw=_decimal_of(
                financial_summary.get("contribution_margin_krw")
            ),
            contribution_margin_rate=_decimal_of(
                financial_summary.get("contribution_margin_rate")
            ),
        ),
    )
