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
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

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
    "REQUIRED_COMMERCIAL_TERMS",
    "SaleConfirmationOut",
    "confirm_approved_sale",
    "missing_commercial_terms",
    "missing_terms_reason",
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


def missing_terms_reason(missing: tuple[str, ...]) -> str:
    """왜 확정할 수 없나 — **무엇이 없는지 이름을 부른다.**

    ★ 부서 판정 문장(`capability(runtime/business)`)과 **모양이 다르다.** 탈락 사유가
      *"재무가 반려"* 처럼 보이면 사람이 재무를 본다. *"납품일이 없다"* 로 보여야
      판매를 본다.
    """
    names = ", ".join(_TERM_NAMES.get(field, field) for field in missing)
    return f"{names} 이(가) 없어 판매를 확정할 수 없다 — 없는 값을 지어내지 않는다."


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
    confirm: Callable[[Any, SalesConfirmationInput], Any] | None = None,
    connect: Callable[[], Any] | None = None,
) -> SaleConfirmationOut:
    """승인된 판매안을 **판매 원장에 확정**한다.

    순서가 이 함수의 전부다.

    ```text
    1. 재검증 통과 확인   PASSED 가 아니면 여기서 끝난다 — confirm_sale 을 안 부른다
    2. 상업조건 확인      없는 값을 지어내지 않는다 → BLOCKED (이름을 부른다)
    3. 기준일 확인        order_date 는 **그 실행의 as_of** 다 — 벽시계가 아니다
    4. 입력 계약 조립     커넥션 밖에서 (실패해도 DB 를 안 건드린다)
    5. confirm_sale       한 커넥션 · commit 한 번 · 실패하면 rollback
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

    missing = missing_commercial_terms(scenario)
    if missing:
        return SaleConfirmationOut(
            status="BLOCKED",
            reason=missing_terms_reason(missing),
            missing_terms=list(missing),
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
) -> SalesConfirmationInput:
    """`SalesConfirmationInput` 을 짓는다. **판매가 발표한 계약 그대로다.**

    ```text
    sale_date   scenario 의 delivery_date        납품일 정본은 sales.sale_date (판매 확정)
    order_date  그 실행의 as_of                   판매·재무 확정 ③ — 벽시계가 아니다
    sim_run_id  ledger_repository.BURN_IN_SIM_RUN_ID   어느 실행의 장부인가는 마스터가 정한다
    ```

    ★ **기여이익을 마스터가 다시 적지 않는다.** `line.contribution_profit_krw` 와
      `contribution_margin_rate` 를 비워 두면 판매가 scenario 값을 쓴다
      (`_line_profit`). 여기에 값을 베껴 넣으면 같은 사실이 두 곳에 남는다.

    ★ **`grade` 는 `None` 이다.** `SalesScenario` 에 등급 칸이 없다 — 없는 값을
      지어내지 않는다.
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
        ),
    )
