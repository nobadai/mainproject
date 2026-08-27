"""재무 어댑터 — 번역이 계약을 지키는가.

★ DB 를 타지 않는다. `_load_context` 를 갈아 끼워 **번역만** 시험한다.
  실제 값의 정확성은 `app.finance.tools` 의 테스트가 본다 — 여기서 다시 보면
  같은 것을 두 번 검사하면서 도메인 변경에 어댑터 테스트가 깨진다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

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
    purchase_payment_days = 7
    minimum_cash_balance_krw = Decimal(10_000_000)
    cashflow_projection_days = 30
    cash_priority_reference = "minimum_cash_balance_krw"
    cash_priority_high_ratio = Decimal(1)
    cash_priority_medium_ratio = Decimal("1.5")
    policy_version = "v1.3-PROVISIONAL"
    payroll_date = 10
    monthly_labor_cost_krw = Decimal(3_000_000)


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


def test_마진_방어선은_missing_data_로_밝힌다(wired):
    """🔴 재무 payload 로 오기로 했는데(M-19) 구현된 Policy 에 필드가 없다.

    `0` 이나 임의값으로 채우면 매입이 그 값으로 손익분기를 계산한다 — 에러도 안 나고
    검증도 통과한다 (§1.2-10).
    """
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
# SCENARIO_VALIDATION — 아직 못 한다
# ---------------------------------------------------------------------------


def test_시나리오_판정은_못_한다는_사실이_드러난다(wired):
    """★ 추측 매핑을 하면 **틀린 값을 판정**하고 에러도 안 난다.

    못 하는 것을 `skipped` 로 밝히면 Flow 는 끝까지 돌고 사실은 이력에 남는다.
    """
    reply, meta = adapter.finance_port(req(mode="SCENARIO_VALIDATION"))
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert "purchase_scenario_schema" in reply.missing_data
    assert reply.missing_capability
    assert meta.used_tools == ()
