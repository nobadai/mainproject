"""비분할 SCENARIO_VALIDATION 재구성 계약.

🔴 예전 재구성은 세 가지를 동시에 잘못했다.

     purchase_date = as_of          → **오늘** 기준으로 N5 지급일을 만들었다
     qty_kg        = None           → 검증 일정에 수량이 없었다
     amount_max    = amount         → **STRESS 가 BASE 와 같아** 두 투영이 늘 같은 값을
                                      내고 함께 통과했다. 검사가 아무것도 가르지 못했다.

★ 재무는 매입이 제출한 사실을 **읽고 파생**할 뿐 소유하지 않는다. 파생한 STRESS 금액은
  재무 검증 메타데이터이지 고쳐 쓴 매입 제안이 아니다.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.finance import adapter
from app.finance.agent import FinanceAgentController
from app.finance.tool_registry import _scenario_schedule
from app.master.envelope import AgentRequest, ExecutionContext
from tests.finance.test_finance_adapter import _AdapterPlanner, _Context

AS_OF = date(2025, 12, 31)
N5 = 7  # `_Policy.purchase_payment_days`
HORIZON = date(2026, 1, 30)


def _req(payload: dict, mode: str = "SCENARIO_VALIDATION") -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(
            request_id="REQ-SCN",
            as_of=AS_OF,
            trigger="USER_REQUEST",
            policy_version="POLICY-V1",
        ),
        agent="finance",
        mode=mode,
        payload=payload,
    )


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "FinanceAgentController",
        lambda port: FinanceAgentController(port, _AdapterPlanner()),
    )
    monkeypatch.setattr("app.finance.run_repository.save_finance_execution", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_load_context", lambda: _Context())


def _non_split(**over) -> dict:
    """매입일이 `as_of` 와 **다른** 비분할 시나리오 — 그래야 N5 기준점이 드러난다.

    ★ `max_price`(2,000) > 단가(1,500) 라 STRESS 가 BASE 보다 크다. 옛 재구성은 둘을
      같게 만들어 이 차이를 지웠다.
    """
    scenario = {
        "total_qty_kg": 100,
        "total_amount_krw": 150_000,
        "max_price": 2_000,
        "split_plan": [{"seq": 1, "date": "2026-01-05", "qty_kg": 100}],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": 100, "grade_unit_price": 1_500}
        ],
    }
    scenario.update(over)
    return scenario


def _schedule(scenario: dict):
    return _scenario_schedule(
        scenario=scenario, as_of=AS_OF, horizon=HORIZON, default_payment_days=N5
    )


def _proposal(purchase_payload: dict, **scenario_over) -> dict:
    """실제 어댑터 경로용 **온전한 매입 제안**.

    어댑터는 `PurchaseProposal` 계약을 검증하므로 시나리오 조각만으로는 들어갈 수 없다 —
    그 검증은 재무가 매입 계약을 우회하지 않게 막는 장치이므로 그대로 둔다.
    """
    payload = deepcopy(purchase_payload)
    payload["scenarios"][0].update(scenario_over)
    return payload


def _proposal_scenario(**over) -> dict:
    """어댑터 경로에서 쓰는 비분할 시나리오.

    ★ 매입 계약이 **`split_plan[0].date == meta.as_of`** 를 요구한다. 그래서 이 경로의
      첫 매입일은 오늘과 같다 — 옛 `purchase_date = as_of` 가 지금까지 들키지 않은
      이유다. 매입 계약이 바뀌거나 재무 소유 payload 로 들어오면 달라지므로,
      기준점 자체는 `_non_split()` 단위 테스트가 따로 고정한다.
    """
    scenario = _non_split(
        split_plan=[{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 100}],
    )
    scenario.update(over)
    return scenario


# ---------------------------------------------------------------------------
# ① 매입일 ② N5 지급일
# ---------------------------------------------------------------------------


def test_non_split_purchase_date_comes_from_split_plan():
    """🔴 `as_of` 가 아니라 매입이 제출한 분할 일자다."""
    (payment,) = _schedule(_non_split())

    assert payment.purchase_date == date(2026, 1, 5)
    assert payment.purchase_date != AS_OF


def test_non_split_payment_date_applies_n5_from_purchase_date():
    """지급일은 **매입일** + N5 다. 오늘에서 세면 실제 지급 시점이 어긋난다."""
    (payment,) = _schedule(_non_split())

    assert payment.payment_date == date(2026, 1, 5) + __import__("datetime").timedelta(days=N5)
    assert payment.payment_date == date(2026, 1, 12)


# ---------------------------------------------------------------------------
# ③ BASE ④ STRESS ⑤ 둘이 갈린다
# ---------------------------------------------------------------------------


def test_non_split_base_uses_submitted_total_amount():
    (payment,) = _schedule(_non_split())
    assert payment.amount_krw == Decimal(150_000)


def test_non_split_stress_uses_submitted_qty_times_max_price():
    (payment,) = _schedule(_non_split())
    assert payment.amount_max_krw == Decimal(100) * Decimal(2_000)


def test_non_split_base_and_stress_can_differ():
    """🔴 예전에는 둘이 늘 같아 BASE/STRESS 검증이 무의미했다."""
    (payment,) = _schedule(_non_split())

    assert payment.amount_krw == Decimal(150_000)
    assert payment.amount_max_krw == Decimal(200_000)
    assert payment.amount_max_krw > payment.amount_krw


def test_stress_overlay_is_never_better_than_base_end_to_end(purchase_payload):
    """실제 실행에서 STRESS 투영은 BASE 보다 **좋아질 수 없다.**

    ★ 등호를 허용하는 이유: 투영 최저가 지급일 **이전**(예: as_of 당일)에 잡히면 두
      투영의 최저값이 같을 수 있다 — 실 DB 데이터가 그렇다. 그건 결함이 아니라 산술이다.
      진짜로 고친 것은 **금액이 갈린다**는 것이고, 그건 아래 일정에서 확인한다.
    """
    reply, _meta = adapter.finance_port(
        _req(_proposal(purchase_payload, **_proposal_scenario()))
    )

    assert reply.runtime_status == "READY"
    verdict = reply.payload["verdicts"][0]
    assert verdict["stress_projected_cash_min"] <= verdict["scenario_projected_cash_min"]

    # 🔴 여기가 핵심이다 — 예전에는 이 둘이 항상 같았다.
    (row,) = verdict["payment_schedule"]
    assert row["amount_max_krw"] > row["amount_krw"]


# ---------------------------------------------------------------------------
# ⑥ 정규화된 payment_schedule 모양
# ---------------------------------------------------------------------------


_ROW_KEYS = {
    "seq",
    "purchase_date",
    "payment_date",
    "qty_kg",
    "amount_krw",
    "amount_max_krw",
    "basis",
}


def test_reconstructed_row_uses_the_normalized_shape(purchase_payload):
    reply, _meta = adapter.finance_port(_req(_proposal(purchase_payload, **_proposal_scenario())))

    rows = reply.payload["verdicts"][0]["payment_schedule"]
    assert len(rows) == 1
    assert set(rows[0]) == _ROW_KEYS
    assert rows[0]["qty_kg"] is not None


def test_split_and_reconstructed_rows_share_one_shape(purchase_payload):
    """🔴 한 배열 안에서 행 모양이 갈리면 읽는 쪽이 매번 확인해야 한다."""
    reconstructed, _ = adapter.finance_port(
        _req(_proposal(purchase_payload, **_proposal_scenario()))
    )

    split_payload = _proposal(
        purchase_payload,
        total_qty_kg=2,
        total_amount_krw=200,
        max_price=120,
        sourcing_plan=[
            {"market": "가락", "grade": "상", "qty_kg": 2, "grade_unit_price": 100}
        ],
        split_plan=[
            {"seq": 1, "date": "2025-12-31", "qty_kg": 1},
            {"seq": 2, "date": "2026-01-01", "qty_kg": 1},
        ],
        payment_schedule=[
            {
                "seq": 1, "purchase_date": "2025-12-31", "payment_date": "2026-01-07",
                "qty_kg": 1, "amount_krw": 100, "amount_max_krw": 120, "basis": "as_of_unit_price",
            },
            {
                "seq": 2, "purchase_date": "2026-01-01", "payment_date": "2026-01-08",
                "qty_kg": 1, "amount_krw": 100, "amount_max_krw": 120, "basis": "as_of_unit_price",
            },
        ],
    )
    split, _ = adapter.finance_port(_req(split_payload))

    for reply in (reconstructed, split):
        assert reply.runtime_status == "READY", reply.reasoning
        for row in reply.payload["verdicts"][0]["payment_schedule"]:
            assert set(row) == _ROW_KEYS


# ---------------------------------------------------------------------------
# ⑦ 분할 회귀 ⑧ H1 확정분 회귀
# ---------------------------------------------------------------------------


def test_split_schedule_is_preserved_not_regenerated(purchase_payload):
    """제출된 분할 일정은 재무가 다시 만들지 않는다 — 검증만 한다."""
    scenario = deepcopy(purchase_payload["scenarios"][0])
    scenario.update(
        total_qty_kg=2,
        total_amount_krw=200,
        max_price=120,
        split_plan=[
            {"seq": 1, "date": "2025-12-31", "qty_kg": 1},
            {"seq": 2, "date": "2026-01-01", "qty_kg": 1},
        ],
        payment_schedule=[
            {
                "seq": 1, "purchase_date": "2025-12-31", "payment_date": "2026-01-07",
                "qty_kg": 1, "amount_krw": 100, "amount_max_krw": 120, "basis": "as_of_unit_price",
            },
            {
                "seq": 2, "purchase_date": "2026-01-01", "payment_date": "2026-01-08",
                "qty_kg": 1, "amount_krw": 100, "amount_max_krw": 120, "basis": "as_of_unit_price",
            },
        ],
    )
    schedule = _schedule(scenario)

    assert [item.payment_date.isoformat() for item in schedule] == ["2026-01-07", "2026-01-08"]
    assert [item.amount_krw for item in schedule] == [Decimal(100), Decimal(100)]
    assert [item.amount_max_krw for item in schedule] == [Decimal(120), Decimal(120)]


def test_pre_h1_split_still_enforces_n5_from_each_purchase_date(purchase_payload):
    scenario = deepcopy(purchase_payload["scenarios"][0])
    scenario.update(
        total_qty_kg=1,
        total_amount_krw=100,
        max_price=120,
        split_plan=[{"seq": 1, "date": "2025-12-31", "qty_kg": 1}],
        payment_schedule=[
            {
                "seq": 1, "purchase_date": "2025-12-31",
                "payment_date": "2026-01-09",  # N5 는 2026-01-07 이다
                "qty_kg": 1, "amount_krw": 100, "amount_max_krw": 120, "basis": "as_of_unit_price",
            }
        ],
    )
    with pytest.raises(ValueError, match="purchase_date plus policy days"):
        _schedule(scenario)


def test_h1_authoritative_payment_dates_are_not_rewritten(purchase_payload):
    """H1 확정분은 재무가 검증만 한다 — N5 규칙으로 덮어쓰지 않는다."""
    scenario = deepcopy(purchase_payload["scenarios"][0])
    scenario.update(
        h1_authoritative=True,
        total_qty_kg=1,
        total_amount_krw=100,
        max_price=120,
        split_plan=[{"seq": 1, "date": "2025-12-31", "qty_kg": 1}],
        payment_schedule=[
            {
                "seq": 1, "purchase_date": "2025-12-31",
                "payment_date": "2026-01-20",       # N5 와 다르지만 확정분이다
                "qty_kg": 1, "amount_krw": 100,
                "amount_max_krw": 999,              # qty × max_price 와 달라도 확정분이다
                "basis": "h1_authoritative",
            }
        ],
    )
    (payment,) = _schedule(scenario)

    assert payment.payment_date == date(2026, 1, 20)
    assert payment.amount_max_krw == Decimal(999)
    assert payment.basis == "h1_authoritative"


# ---------------------------------------------------------------------------
# ⑨ 금액 축 전용 ⑩ 입력이 없으면 fail closed
# ---------------------------------------------------------------------------


def test_finance_adjustment_axis_is_amount_only(purchase_payload):
    reply, _meta = adapter.finance_port(_req(_proposal(purchase_payload, **_proposal_scenario())))

    assert reply.runtime_status == "READY"
    for adjustment in reply.suggested_adjustments:
        assert adjustment.axis == "amount"
        assert adjustment.dept == "finance"


@pytest.mark.parametrize(
    ("missing", "expected_key"),
    [
        ("split_plan", "scenario_split_plan"),
        ("total_qty_kg", "scenario_total_qty_kg"),
        ("max_price", "scenario_max_price"),
    ],
)
def test_missing_reconstruction_input_fails_closed(missing, expected_key):
    """🔴 없는 값을 지어내지 않는다. 채워 넣으면 근거 없는 판정이 나간다."""
    scenario = _non_split()
    scenario.pop(missing)

    from app.finance.repository import FinanceDataNotReady

    with pytest.raises(FinanceDataNotReady) as raised:
        _schedule(scenario)
    assert raised.value.key == expected_key


def test_multi_split_without_payment_schedule_fails_closed():
    """분할이 여러 건인데 지급 일정이 없으면 **금액 배분은 매입이 정할 일**이다."""
    from app.finance.repository import FinanceDataNotReady

    scenario = _non_split(
        split_plan=[
            {"seq": 1, "date": "2026-01-05", "qty_kg": 50},
            {"seq": 2, "date": "2026-01-06", "qty_kg": 50},
        ]
    )
    with pytest.raises(FinanceDataNotReady) as raised:
        _schedule(scenario)
    assert raised.value.key == "scenario_payment_schedule"


def test_reconstruction_outside_horizon_fails_closed():
    scenario = _non_split(split_plan=[{"seq": 1, "date": "2026-01-28", "qty_kg": 100}])

    from app.finance.repository import FinanceDataNotReady

    with pytest.raises(FinanceDataNotReady) as raised:
        _schedule(scenario)
    assert raised.value.key == "default_purchase_payment_date"


# ---------------------------------------------------------------------------
# ⑪ Evidence ⑫ 소유권
# ---------------------------------------------------------------------------


def test_reconstruction_keeps_evidence_and_finance_ownership(purchase_payload):
    reply, _meta = adapter.finance_port(_req(_proposal(purchase_payload, **_proposal_scenario())))

    assert reply.runtime_status == "READY"
    assert reply.evidences
    for evidence in reply.evidences:
        assert evidence.ref_ids

    # 재무는 매입 소유 값을 산출하지 않는다.
    verdict = reply.payload["verdicts"][0]
    forbidden = {"grade_unit_price", "avg_unit_price", "sourcing_plan", "has_unmet_obligation"}
    assert not forbidden & set(verdict)


# ---------------------------------------------------------------------------
# critical_cash_date — 설계서가 정한 자리는 Trace 다
# ---------------------------------------------------------------------------


def test_critical_cash_date_stays_in_trace_not_business_payload(purchase_payload):
    """🔴 같은 값이 mode 마다 다른 자리에 있었다.

    설계서는 `critical_cash_date` 를 **Trace/Run History 항목**으로 못박았다
    (상세설계 · IO Contract §11 · Tool/Rule 명세). PRE_PURCHASE 는 지키고 있었는데
    SCENARIO_VALIDATION 만 verdict payload 로 올렸다.

    ★ 빼도 **추적성은 잃지 않는다** — Tool 결과 전체가 observation 으로 남는다.
    """
    import json

    reply, metadata = adapter.finance_port(
        _req(_proposal(purchase_payload, **_proposal_scenario()))
    )
    assert reply.runtime_status == "READY"

    verdict = reply.payload["verdicts"][0]
    assert "critical_cash_date" not in verdict
    assert "critical_cash_date" not in reply.payload
    assert "critical_cash_date" not in {item.claim for item in reply.evidences}

    # Trace 에는 남아 있다.
    traced = [
        item for item in metadata.observations if "critical_cash_date" in item
    ]
    assert traced, "Trace 에서까지 사라지면 추적성을 잃는다"
    assert json.loads(traced[0])["result"]["critical_cash_date"]
