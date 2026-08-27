"""재무 어댑터 — 번역이 계약을 지키는가.

★ DB 를 타지 않는다. `_load_context` 를 갈아 끼워 **번역만** 시험한다.
  실제 값의 정확성은 `app.finance.tools` 의 테스트가 본다 — 여기서 다시 보면
  같은 것을 두 번 검사하면서 도메인 변경에 어댑터 테스트가 깨진다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

from app.master.adapters import finance as adapter
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply

AS_OF = date(2025, 12, 31)


def ctx(as_of: date = AS_OF) -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-T-0001",
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
    )


def req(mode="PRE_PURCHASE", as_of: date = AS_OF) -> AgentRequest:
    return AgentRequest(context=ctx(as_of), agent="finance", mode=mode)


class _Policy:
    margin_defense_floor_rate = Decimal("0.267")
    purchase_payment_days = 7
    minimum_cash_balance_krw = Decimal(10_000_000)
    cashflow_projection_days = 30
    cash_priority_reference = "minimum_cash_balance_krw"
    cash_priority_high_ratio = Decimal(1)
    cash_priority_medium_ratio = Decimal("1.5")
    policy_version = "v1.3-PROVISIONAL"
    payroll_date = 10
    monthly_labor_cost_krw = Decimal(3_000_000)
    source_refs: ClassVar[dict[str, str]] = {
        "purchase_payment_days": "FINANCE-DECISION-20260827:N5",
        "payroll_date": "FINANCE-DECISION-20260827:N6",
        "monthly_labor_cost_krw": "PERSONA-V1.5:monthly_labor_cost",
        "minimum_cash_balance_krw": "PROJECT-DEFINITION-V1.2:minimum_cash_balance",
        "cashflow_projection_days": "MVP-DECISION-20260825:FIN-CASH-01",
        "margin_defense_floor_rate": "PROJECT-DEFINITION-V1.2:MARGIN-DEFENSE-GRACE",
    }


class _Snapshot:
    state_date = AS_OF
    current_cash_krw = Decimal(50_000_000)
    finance_state_id = "FIN-STATE-1"


class _Context:
    snapshot = _Snapshot()
    policy = _Policy()
    cash_events: tuple = ()


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(adapter, "_load_context", lambda: _Context())


# ---------------------------------------------------------------------------
# PRE_PURCHASE
# ---------------------------------------------------------------------------


def test_봉투_검증을_통과한다(wired):
    """★ 어댑터가 스스로 계약을 지켜야 한다.

    마스터가 findings 를 내는 것은 **부서가 계약을 어겼다**는 뜻이다. 우리가 만든
    어댑터가 그걸 내면 남 탓할 자리가 없다.
    """
    request = req()
    reply, meta = adapter.finance_port(request)
    assert [f.code for f in validate_reply(request, reply, meta)] == []


def test_확정_7필드를_싣는다(wired):
    reply, _ = adapter.finance_port(req())
    assert reply.runtime_status == "READY"
    for field in (
        "available_cash",
        "finance_cap_amount_krw",
        "base_projected_cash_min",
        "purchase_payment_days",
        "payment_pressure",
        "critical_payment_dates",
    ):
        assert field in reply.payload, field


def test_마진_방어선을_읽어서_싣는다(wired):
    """★ 어댑터가 **계산하지 않는다** (2026-08-27 재무 확인).

    `break_even_cm + 0.02` 를 여기서 만들면 두 곳에서 같은 값을 계산하게 되고,
    N9 후 재산정 때 한쪽만 바뀐다. Policy 를 읽어 싣기만 한다.
    """
    reply, _ = adapter.finance_port(req())
    assert reply.payload["margin_defense_floor_rate"] == 0.267
    assert "margin_defense_floor_rate" not in reply.missing_data


def test_마진_방어선이_없으면_missing_data_로_밝힌다(monkeypatch):
    """`0` 으로 채우면 매입이 그 값으로 손익분기를 계산한다 — 에러도 안 나고
    검증도 통과한다 (§1.2-10)."""

    class _NoFloor(_Context):
        class policy(_Policy):
            margin_defense_floor_rate = None

    monkeypatch.setattr(adapter, "_load_context", lambda: _NoFloor())
    reply, _ = adapter.finance_port(req())
    assert "margin_defense_floor_rate" not in reply.payload
    assert "margin_defense_floor_rate" in reply.missing_data
    assert reply.runtime_status == "READY"  # 상한 계산 자체는 막지 않는다


def test_as_of_가_다르면_RUNTIME_NOT_READY(wired):
    """★ 재무 상태 기준일이 다르면 **그날의 사실이 아니다** (§1.2-6).

    누수는 에러를 내지 않고 백테스트 손익만 좋아진다.
    """
    reply, _ = adapter.finance_port(req(as_of=date(2026, 8, 27)))
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data  # 무엇이 없는지 이름이 있어야 한다
    assert not reply.contributes_to_band


def test_컨텍스트가_없으면_ERROR_가_아니라_NOT_READY(monkeypatch):
    """다시 불러도 같은 답이면 재시도 가치가 없다 (M-1 §5.1)."""
    monkeypatch.setattr(adapter, "_load_context", lambda: None)
    reply, _ = adapter.finance_port(req())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert not reply.worth_retry


def test_실제로_부른_Tool_만_남는다(wired):
    _, meta = adapter.finance_port(req())
    assert meta.used_tools == (
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    )
    assert len(meta.tool_order) == len(meta.used_tools)


def test_판정_라벨에_근거_수치가_붙는다(wired):
    """`payment_pressure: "LOW"` 는 숫자가 아니지만 매입의 행동을 바꾼다."""
    reply, _ = adapter.finance_port(req())
    ev = next(e for e in reply.evidences if e.claim == "payment_pressure")
    assert ev.unit == "ratio"
    assert "payment_pressure" in reply.judgment_fields


def test_목록형_근거는_개수가_아니라_임계값이다(wired):
    """★ 개수를 넣으면 답의 길이를 세어 답이라고 적는 것이다.

    나중에 "왜 그날이 위험일인가"를 보는 사람에게 아무것도 말해 주지 않는다.
    """
    reply, _ = adapter.finance_port(req())
    ev = next(e for e in reply.evidences if e.claim == "critical_payment_dates")
    assert ev.value == float(_Policy.minimum_cash_balance_krw)
    assert ev.unit == "KRW"


# ---------------------------------------------------------------------------
# critical_payment_dates — 지급일만 후보다 (2026-08-27 재무 정의)
# ---------------------------------------------------------------------------


class _Event:
    """project_cashflow 가 중복 판정에 쓰는 (date, event_type, ref_id) 까지 갖춘다."""

    def __init__(self, day: int, amount: int, direction: str = "OUTFLOW"):
        self.event_date = date(2026, 1, day)
        self.amount_krw = Decimal(amount)
        self.direction = direction
        self.event_type = "TEST"
        self.ref_id = f"EV-{day}-{direction}"


def _with_events(monkeypatch, events):
    class _C(_Context):
        cash_events = tuple(events)

    monkeypatch.setattr(adapter, "_load_context", lambda: _C())


def test_지급이_없는_날은_후보가_아니다(monkeypatch):
    """★ 제 초안을 재무가 되돌렸다.

    처음에는 "잔액이 최소현금 아래인 날"로 정의했는데 그건 `critical_cash_date` 다.
    매입은 **분할 회차 지급일이 겹치는지**를 보므로, 지급이 없는 날은 겹칠 수가 없다.
    """
    _with_events(monkeypatch, [_Event(20, 1_000_000)])
    reply, _ = adapter.finance_port(req())
    dates = reply.payload["critical_payment_dates"]
    # 급여(10일 3,000,000)가 20일 1,000,000 보다 크므로 급여일이 뽑힌다
    assert dates == ["2026-01-10"]
    # 지급이 없는 날은 어느 것도 후보가 아니다
    assert all(d in {"2026-01-10", "2026-01-20"} for d in dates)


def test_일일_유출이_가장_큰_지급일을_고른다(monkeypatch):
    """급여 3,000,000 보다 큰 지급이 들어오면 그쪽이 뽑힌다."""
    _with_events(monkeypatch, [_Event(5, 1_000_000), _Event(20, 9_000_000)])
    reply, _ = adapter.finance_port(req())
    assert reply.payload["critical_payment_dates"] == ["2026-01-20"]


def test_유입은_지급_집중도에_세지_않는다(monkeypatch):
    """지급 부담을 보는 값이라 들어오는 돈은 후보가 아니다."""
    _with_events(monkeypatch, [_Event(5, 99_000_000, "INFLOW"), _Event(20, 9_000_000)])
    reply, _ = adapter.finance_port(req())
    assert reply.payload["critical_payment_dates"] == ["2026-01-20"]


def test_현금_최저일은_Business_Reply_에_싣지_않는다(monkeypatch):
    """★ 개념이 달라 필드를 나눴다가 재무 요청으로 다시 뺐다 (2026-08-27).

    `critical_cash_date` 는 Finance Trace / Run History 에서 관리한다.
    **계약 필드는 읽는 쪽이 있을 때만 늘어야 한다** — 매입이 쓰지 않는 값이다.
    """
    _with_events(monkeypatch, [_Event(20, 9_000_000)])
    reply, _ = adapter.finance_port(req())
    assert "critical_cash_date" not in reply.payload
    assert "critical_cash_date" not in {e.claim for e in reply.evidences}


# ---------------------------------------------------------------------------
# 정책값 출처 — DB 인가 Schema default 인가 (2026-08-27 재무 후속회신 §3)
# ---------------------------------------------------------------------------


def test_출처가_다_있으면_missing_data_가_비어_있다(wired):
    reply, _ = adapter.finance_port(req())
    assert not [m for m in reply.missing_data if m.endswith("@policy_source_ref")]


def test_급여_출처가_없으면_투영을_만들지_않는다(monkeypatch):
    """🔴 값이 아니라 **출처**의 문제인데, 급여만은 계산까지 막는다.

    Repository 가 그 키를 조회하지 않으면 Pydantic 기본값이 대신 쓰이는데, 값은
    멀쩡히 나오고 에러도 안 난다 — **DB 를 고쳐도 반영되지 않는다는 사실만 숨는다.**
    실제로 `payroll_date` 가 그 상태였다. DB(10)와 default(10)가 우연히 같았다.

    ★ 재무가 2026-08-27(#63) `build_payroll_schedule` 을 fail-closed 로 바꿨다 —
      출처 없는 급여 이벤트를 만들지 않는다(M-23). 그러면 **급여 유출이 통째로 빠진
      투영**이 나오고 `finance_cap` 이 낙관적으로 부풀려진다.

    ★ 그래서 `READY` 로 두고 이름만 밝히지 않는다. 다만 **`ERROR` 도 아니다** —
      다시 불러도 같으므로 `RUNTIME_NOT_READY` 다 (M-1 §5.1).
    """

    class _NoRef(_Context):
        class policy(_Policy):
            source_refs: ClassVar[dict[str, str]] = {}

    monkeypatch.setattr(adapter, "_load_context", lambda: _NoRef())
    reply, _ = adapter.finance_port(req())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert set(reply.missing_data) == {
        "monthly_labor_cost_krw@policy_source_ref",
        "payroll_date@policy_source_ref",
    }


def test_급여_아닌_정책값은_출처가_없어도_돈다(monkeypatch):
    """★ 급여만 특별하다. 나머지는 값을 쓸 수 있으므로 이름만 밝히고 지나간다."""

    class _PayrollOnly(_Context):
        class policy(_Policy):
            source_refs: ClassVar[dict[str, str]] = {
                "monthly_labor_cost_krw": "PERSONA-V1.5:monthly_labor_cost",
                "payroll_date": "FINANCE-DECISION-20260827:N6",
            }

    monkeypatch.setattr(adapter, "_load_context", lambda: _PayrollOnly())
    reply, _ = adapter.finance_port(req())
    assert reply.runtime_status == "READY"
    assert "purchase_payment_days@policy_source_ref" in reply.missing_data
