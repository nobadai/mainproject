"""매입 지급 일정의 재구성·검증·현금이벤트 변환.

이 파일이 소유하는 것
    비분할 재구성 (`split_plan[0].date` + N5)
    분할 일정 검증/정규화
    H1 확정분 보존
    BASE/STRESS 현금이벤트 생성
    일정 기반 상한 helper

여기 **없는 것**
    재무 상태/정책 적재 (`runtime_context`)
    현금흐름·상한 공식 (`app.finance.tools`)
    BASE/STRESS 판정 (`app.finance.rules`)

★ 재무는 매입이 제출한 사실을 **읽고 파생**할 뿐 소유하지 않는다.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from app.finance.repository import FinanceDataNotReady
from app.finance.schemas import CashEvent
from app.finance.state import ScenarioPayment


def _reconstructed_payment(
    scenario: Any,
    as_of: date,
    horizon: date,
    default_payment_days: int | None,
    amount: Decimal,
) -> ScenarioPayment:
    """`payment_schedule` 이 없는 제안을 **제출된 사실만으로** 재구성한다.

    🔴 예전에는 `purchase_date=as_of` · `qty_kg=None` · `amount_max=amount` 였다.
       세 가지가 동시에 잘못됐다.

         · 매입일을 `as_of` 로 두면 N5 지급일이 **실제 매입일이 아닌 오늘**에서 계산된다.
         · STRESS 금액이 BASE 와 같으면 **BASE/STRESS 검증이 아무것도 가르지 않는다** —
           두 투영이 같은 값을 내고 늘 함께 통과한다. 검사가 있으나 마나였다.

    ★ 재무는 매입이 제출한 사실을 **읽고 파생**할 뿐, 소유하지 않는다.
        purchase_date  ← split_plan[0].date        (매입 소유)
        payment_date   ← purchase_date + N5        (재무 정책)
        qty_kg         ← total_qty_kg              (매입 소유)
        BASE  amount   ← total_amount_krw          (매입 소유)
        STRESS amount  ← total_qty_kg × max_price  (매입 소유 값에서 파생)

      파생한 STRESS 금액은 **재무 검증 메타데이터**다. 매입 제안을 고쳐 쓴 것이 아니다.

    ★ 없는 값은 지어내지 않는다. 분할이 여러 건인데 지급 일정이 없으면 금액을 어떻게
      쪼갤지는 **매입이 정할 일**이라 재무가 배분을 발명하지 않고 fail-closed 한다.
    """
    if default_payment_days is None:
        raise FinanceDataNotReady("purchase_payment_days")

    split_plan = scenario.get("split_plan")
    if not isinstance(split_plan, list) or not split_plan:
        raise FinanceDataNotReady("scenario_split_plan")
    if len(split_plan) != 1:
        # 분할이 여러 건이면 각 건의 금액은 매입만 안다.
        raise FinanceDataNotReady("scenario_payment_schedule")

    split = split_plan[0]
    raw_date = split.get("date")
    if raw_date is None:
        raise FinanceDataNotReady("scenario_split_plan_date")
    purchase_date = date.fromisoformat(str(raw_date))

    payment_date = purchase_date + timedelta(days=default_payment_days)
    if not as_of < payment_date <= horizon:
        raise FinanceDataNotReady("default_purchase_payment_date")

    qty = _positive_decimal(scenario.get("total_qty_kg"), "scenario_total_qty_kg")
    max_price = _positive_decimal(scenario.get("max_price"), "scenario_max_price")

    return ScenarioPayment(
        seq=1,
        purchase_date=purchase_date,
        payment_date=payment_date,
        qty_kg=qty,
        amount_krw=amount,
        amount_max_krw=qty * max_price,
        basis="non_split_policy_reconstruction",
    )


def _payment_row(item: ScenarioPayment) -> dict[str, Any]:
    """재무 검증 일정 한 행. **모든 행이 같은 모양이다.**"""
    return {
        "seq": item.seq,
        "purchase_date": item.purchase_date.isoformat(),
        "payment_date": item.payment_date.isoformat(),
        "qty_kg": str(item.qty_kg) if item.qty_kg is not None else None,
        "amount_krw": str(item.amount_krw),
        "amount_max_krw": str(item.amount_max_krw),
        "basis": item.basis,
    }


def _positive_decimal(value: Any, missing_key: str) -> Decimal:
    """제출된 양수 사실을 읽는다. 없거나 양수가 아니면 **멈춘다.**"""
    if value is None:
        raise FinanceDataNotReady(missing_key)
    amount = Decimal(str(value))
    if amount <= 0:
        raise FinanceDataNotReady(missing_key)
    return amount


def _scenario_schedule(
    *,
    scenario: Any,
    as_of: date,
    horizon: date,
    default_payment_days: int | None,
) -> tuple[ScenarioPayment, ...]:
    amount = Decimal(str(scenario["total_amount_krw"]))
    if amount <= 0:
        raise ValueError("total_amount_krw must be positive")
    raw_schedule = scenario.get("payment_schedule")
    if raw_schedule is None:
        return (_reconstructed_payment(scenario, as_of, horizon, default_payment_days, amount),)
    if not isinstance(raw_schedule, list) or not raw_schedule:
        raise ValueError("payment_schedule must be a non-empty list")
    split_plan = scenario.get("split_plan")
    if not isinstance(split_plan, list) or len(split_plan) != len(raw_schedule):
        raise ValueError("payment_schedule must correspond one-to-one with split_plan")
    total_qty = Decimal(str(scenario["total_qty_kg"]))
    max_price = Decimal(str(scenario["max_price"]))
    authoritative_h1 = bool(
        scenario.get("h1_authoritative") or scenario.get("authoritative_h1_payment_data")
    )
    schedule: list[ScenarioPayment] = []
    for index, (row, split) in enumerate(zip(raw_schedule, split_plan, strict=True), start=1):
        required = {
            "seq", "purchase_date", "payment_date", "qty_kg", "amount_krw",
            "amount_max_krw", "basis",
        }
        if not required <= row.keys():
            raise ValueError("payment_schedule row is missing required Finance fields")
        purchase_date = date.fromisoformat(str(row["purchase_date"]))
        payment_date = date.fromisoformat(str(row["payment_date"]))
        payment_amount = Decimal(str(row["amount_krw"]))
        max_amount = Decimal(str(row["amount_max_krw"]))
        qty = Decimal(str(row["qty_kg"]))
        basis = str(row["basis"]).strip()
        if not isinstance(payment_date, date) or not as_of < payment_date <= horizon:
            raise ValueError("payment_date must be inside the Finance projection horizon")
        if int(row["seq"]) != index or int(split["seq"]) != index:
            raise ValueError("payment_schedule and split_plan seq must align")
        if purchase_date != date.fromisoformat(str(split["date"])):
            raise ValueError("payment_schedule purchase_date must equal split_plan date")
        split_qty = Decimal(str(split.get("qty_kg", split.get("quantity_kg"))))
        if qty != split_qty:
            raise ValueError("payment_schedule qty_kg must equal split_plan qty_kg")
        if payment_amount <= 0 or max_amount <= 0 or qty <= 0:
            raise ValueError("payment_schedule amounts and qty must be positive")
        if not basis:
            raise ValueError("payment_schedule basis must be non-empty")
        if not authoritative_h1:
            if default_payment_days is None:
                raise FinanceDataNotReady("purchase_payment_days")
            if payment_date != purchase_date + timedelta(days=default_payment_days):
                raise ValueError("payment_date must equal purchase_date plus policy days before H1")
            if max_amount != qty * max_price:
                raise ValueError("amount_max_krw must equal qty_kg times max_price")
        schedule.append(
            ScenarioPayment(
                index, purchase_date, payment_date, qty, payment_amount, max_amount, basis
            )
        )
    if sum((item.amount_krw for item in schedule), Decimal(0)) != amount:
        raise ValueError("payment_schedule amount sum must equal total_amount_krw")
    if sum((item.qty_kg or Decimal(0) for item in schedule), Decimal(0)) != total_qty:
        raise ValueError("payment_schedule qty sum must equal total_qty_kg")
    return tuple(schedule)


def _schedule_events(
    scenario_id: object, schedule: tuple[ScenarioPayment, ...], *, stress: bool
) -> tuple[CashEvent, ...]:
    return tuple(
        CashEvent(
            event_date=payment.payment_date,
            event_type="EXTRA_PURCHASE",
            amount_krw=payment.amount_max_krw if stress else payment.amount_krw,
            direction="OUTFLOW",
            ref_id=(
                f"SCENARIO:{scenario_id}:{'STRESS' if stress else 'BASE'}:"
                f"{index}:{payment.payment_date.isoformat()}"
            ),
            source_ref=str(scenario_id),
        )
        for index, payment in enumerate(schedule, start=1)
    )


def _calculate_schedule_cap(
    *,
    base_projection: Any,
    schedule: tuple[ScenarioPayment, ...],
    total_amount: Decimal,
    minimum_cash: Decimal,
) -> Decimal:
    balances = {
        point.projection_date: point.cash_balance_krw
        for point in base_projection.projected_cash_by_date
    }
    dates = sorted({*balances, *(item.payment_date for item in schedule)})
    current_balance = balances[base_projection.as_of]
    paid = Decimal(0)
    bounds: list[Decimal] = []
    schedule_by_date: dict[date, Decimal] = {}
    for payment in schedule:
        schedule_by_date[payment.payment_date] = (
            schedule_by_date.get(payment.payment_date, Decimal(0)) + payment.amount_krw
        )
    for current_date in dates:
        if current_date in balances:
            current_balance = balances[current_date]
        paid += schedule_by_date.get(current_date, Decimal(0))
        if paid > 0:
            fraction = paid / total_amount
            bounds.append((current_balance - minimum_cash) / fraction)
    if not bounds:
        raise FinanceDataNotReady("scenario_payment_schedule")
    return max(Decimal(0), min(bounds).quantize(Decimal(1), rounding=ROUND_FLOOR))
