"""Finance Agent 평가 전 공통으로 쓰는 결정론 평가 데이터셋."""
# ruff: noqa: E501

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.finance import adapter
from app.finance.application.orchestration import FinanceAgentController
from app.finance.llm.planner import ToolAction
from app.finance.schemas import CashEvent
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply

AS_OF = date(2025, 12, 31)


class _EvaluationPlanner:
    model = "evaluation-finance-planner"

    def __init__(self):
        self.attempts = 0

    def decide(self, *, allowed_tools, missing_capabilities, **_kwargs):
        self.attempts += 1
        if not missing_capabilities:
            return ToolAction(finalize=True)
        preferred = (
            "assess_finance_position",
            "project_cashflow",
            "calculate_purchase_finance_cap",
            "analyze_payment_pressure",
            "evaluate_purchase_scenario",
            "validate_amount_adjustment",
        )
        return ToolAction(next(name for name in preferred if name in allowed_tools))


@pytest.fixture(autouse=True)
def controller_wired(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "FinanceAgentController",
        lambda port: FinanceAgentController(port, _EvaluationPlanner()),
    )
    monkeypatch.setattr("app.finance.execution.save_finance_execution", lambda **_kwargs: None)


@dataclass(frozen=True)
class FinanceEvaluationCase:
    """향후 Pipeline·bounded Agent·ReAct가 공통으로 소비할 정답 라벨."""

    case_id: str
    description: str
    input_condition: str
    runtime_status: str
    business_status: str
    rule_id: str
    evidence_claims: tuple[str, ...]
    numeric_relationship: str
    rationale: str


EVALUATION_CASES = (
    FinanceEvaluationCase("FIN-EVAL-01", "건전 현금", "정상 PurchaseProposal과 충분한 현금", "READY", "ok", "FIN-BASE-STRESS", ("scenario_projected_cash_min", "stress_projected_cash_min"), "BASE와 STRESS 최저현금이 최소현금 이상", "결정론 현금흐름이 모두 안전하다."),
    FinanceEvaluationCase("FIN-EVAL-02", "Finance Cap 초과", "결정론 Cap을 넘는 매입금액", "READY", "reject", "FIN-BASE-STRESS", ("finance_cap_amount_krw",), "제안금액이 Finance Cap 초과", "실행은 가능하지만 재무적으로 불가능하다."),
    FinanceEvaluationCase("FIN-EVAL-03", "BASE 통과·STRESS 실패", "STRESS 지급금액만 최소현금 미만", "READY", "conditional", "FIN-BASE-STRESS", ("scenario_projected_cash_min", "stress_projected_cash_min"), "BASE ≥ 최소현금, STRESS < 최소현금", "기존 BASE/STRESS 규칙의 조건부 결과다."),
    FinanceEvaluationCase("FIN-EVAL-04", "BASE 최소현금 위반", "기본 현금흐름 자체가 최소현금 미만", "READY", "reject", "FIN-BASE-MIN-CASH", ("scenario_projected_cash_min",), "기본 최저현금 < 최소현금", "어떤 시나리오도 기본 위반을 보정하지 않는다."),
    FinanceEvaluationCase("FIN-EVAL-05", "분할 지급일", "동일 분할금액에 서로 다른 payment_date", "READY", "현금흐름 결과", "FIN-BASE-STRESS", ("payment_schedule", "scenario_projected_cash_min"), "지급일 변경이 날짜별 최저현금을 변경", "단일 Finance Cap이 아닌 날짜 overlay가 정답이다."),
    FinanceEvaluationCase("FIN-EVAL-06", "N5 누락", "비분할안에서 purchase_payment_days 없음", "RUNTIME_NOT_READY", "skipped", "purchase_payment_days", (), "기본 지급일을 만들지 않음", "임의 지급일을 만들지 않고 fail closed 한다."),
    FinanceEvaluationCase("FIN-EVAL-07", "급여 출처 누락", "payroll 정책 source_refs 누락", "RUNTIME_NOT_READY", "skipped", "payroll_policy_source", (), "급여 현금 event를 0으로 대체하지 않음", "근거 없는 급여 투영은 낙관적 오류다."),
    FinanceEvaluationCase("FIN-EVAL-08", "as_of 불일치", "Finance state_date와 요청 as_of 불일치", "RUNTIME_NOT_READY", "skipped", "AS_OF_MISMATCH", (), "state_date ≠ request as_of", "현재·과거 상태를 자동 혼합하지 않는다."),
    FinanceEvaluationCase("FIN-EVAL-09", "Purchase 계약 오류", "필수 필드 또는 수량·금액 계약 위반", "ERROR", "skipped", "validation_errors", (), "입력 복구·추정 없음", "잘못된 계약은 명시적 validation ERROR다."),
    FinanceEvaluationCase("FIN-EVAL-10", "복수 시나리오 격리", "서로 다른 금액·지급일의 복수안", "READY", "시나리오별 결과", "FIN-BASE-STRESS", ("payment_schedule", "scenario_projected_cash_min"), "각 안의 현금흐름이 독립", "한 안의 결과를 다른 안에 재사용하지 않는다."),
)


class _Policy:
    purchase_payment_days = 7
    payroll_date = 10
    monthly_labor_cost_krw = Decimal(3_000_000)
    minimum_cash_balance_krw = Decimal(10_000_000)
    cashflow_projection_days = 30
    cash_priority_reference = "minimum_cash_balance_krw"
    cash_priority_high_ratio = Decimal(1)
    cash_priority_medium_ratio = Decimal("1.5")
    policy_version = "v1.3-PROVISIONAL"
    source_refs: ClassVar[dict[str, str]] = {
        "payroll_date": "POL-PAYROLL-DATE",
        "monthly_labor_cost_krw": "POL-PAYROLL-AMOUNT",
    }


class _Snapshot:
    state_date = AS_OF
    current_cash_krw = Decimal(50_000_000)
    finance_state_id = "FIN-EVAL-STATE"
    snapshot_id = "FIN-EVAL-SNAPSHOT"

    def model_dump(self, **_kwargs):
        return {"finance_state_id": self.finance_state_id, "sim_run_id": "SIM-EVAL", "state_date": self.state_date, "state_type": "DAY", "financing_mode": "NONE", "current_cash_krw": self.current_cash_krw, "minimum_operating_cash_krw": Decimal(10_000_000), "committed_outflows_krw": Decimal(0), "unsettled_purchase_payables_krw": Decimal(0), "receivables_krw": Decimal(0), "current_debt_krw": Decimal(0), "financial_limit_krw": Decimal(40_000_000)}


class _Context:
    snapshot = _Snapshot()
    policy = _Policy()
    cash_events = ()
    unresolved_sources = ()


@pytest.fixture
def evaluation_request(purchase_payload):
    return AgentRequest(ExecutionContext("REQ-FIN-EVAL", AS_OF, "USER_REQUEST", "POLICY-V1"), "finance", "SCENARIO_VALIDATION", payload=purchase_payload)


def test_평가_데이터셋이_필수_라벨을_모두_가진다():
    assert [case.case_id for case in EVALUATION_CASES] == [f"FIN-EVAL-{number:02d}" for number in range(1, 11)]
    assert all(case.runtime_status and case.business_status and case.rule_id for case in EVALUATION_CASES)
    assert all(case.numeric_relationship and case.rationale for case in EVALUATION_CASES)


def test_정상과_Cap_초과는_결정론_Adapter_결과를_라벨로_쓴다(monkeypatch, evaluation_request):
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: _Context())
    healthy, metadata = adapter.finance_port(evaluation_request)
    assert healthy.runtime_status == "READY"
    assert healthy.business_status == "ok"
    assert validate_reply(evaluation_request, healthy, metadata) == ()

    payload = deepcopy(evaluation_request.payload)
    payload["scenarios"][0]["total_amount_krw"] = 45_000_000
    # STRESS 금액은 제출 수량 × 제출 상한가에서 파생된다 — 상한가도 같이 올려야
    # 모순 없는 제안이 된다 (여기서 보려는 것은 Cap 초과이지 제안 모순이 아니다).
    payload["scenarios"][0]["max_price"] = 10_000
    payload["scenarios"][0]["sourcing_plan"] = [
        {"market": "가락", "grade": "상", "qty_kg": 4500, "grade_unit_price": 10_000}
    ]
    exceeded = AgentRequest(evaluation_request.context, "finance", "SCENARIO_VALIDATION", payload=payload)
    rejected, _ = adapter.finance_port(exceeded)
    assert rejected.runtime_status == "READY"
    assert rejected.business_status == "reject"
    assert rejected.payload["verdicts"][0]["finance_cap_amount_krw"] < 45_000_000


@pytest.mark.parametrize("case_id", ("FIN-EVAL-07", "FIN-EVAL-08", "FIN-EVAL-09"))
def test_준비불가와_계약오류_라벨은_기존_결정론_경계로_정의한다(case_id):
    assert next(case for case in EVALUATION_CASES if case.case_id == case_id).runtime_status in {"RUNTIME_NOT_READY", "ERROR"}


def _call(
    monkeypatch,
    request,
    *,
    cash=Decimal(50_000_000),
    state_date=AS_OF,
    days=7,
    payroll_refs=True,
    cash_events=(),
):
    policy = SimpleNamespace(
        purchase_payment_days=days,
        payroll_date=10,
        monthly_labor_cost_krw=Decimal(3_000_000),
        minimum_cash_balance_krw=Decimal(10_000_000),
        cashflow_projection_days=30,
        cash_priority_reference="minimum_cash_balance_krw",
        cash_priority_high_ratio=Decimal(1),
        cash_priority_medium_ratio=Decimal("1.5"),
        policy_version="v1.3-PROVISIONAL",
        source_refs=(
            _Policy.source_refs if payroll_refs else {"purchase_payment_days": "POL-N5"}
        ),
    )
    snapshot = SimpleNamespace(
        state_date=state_date,
        current_cash_krw=cash,
        finance_state_id="FIN-EVAL-STATE",
        snapshot_id="FIN-EVAL-SNAPSHOT",
        model_dump=lambda **_kwargs: {
            "finance_state_id": "FIN-EVAL-STATE",
            "sim_run_id": "SIM-EVAL",
            "state_date": state_date,
            "state_type": "DAY",
            "financing_mode": "NONE",
            "current_cash_krw": cash,
            "minimum_operating_cash_krw": Decimal(10_000_000),
            "committed_outflows_krw": Decimal(0),
            "unsettled_purchase_payables_krw": Decimal(0),
            "receivables_krw": Decimal(0),
            "current_debt_krw": Decimal(0),
            "financial_limit_krw": Decimal(40_000_000),
        },
    )
    context = SimpleNamespace(
        snapshot=snapshot,
        policy=policy,
        cash_events=tuple(cash_events),
        unresolved_sources=(),
    )
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: context)
    return adapter.finance_port(request)


def _split_payload(
    payload,
    *,
    second_purchase_date: str,
    unit_price: int = 5_000,
    max_price: int = 5_000,
):
    scenario = payload["scenarios"][0]
    quantity = 4500
    amount = quantity * unit_price
    half_amount = 2250 * unit_price
    half_max_amount = 2250 * max_price
    payment_dates = ("2026-01-07", "2026-01-14" if second_purchase_date == "2026-01-07" else "2026-01-21")
    scenario.update(
        total_amount_krw=amount,
        max_price=max_price,
        split_plan=[
            {"seq": 1, "date": "2025-12-31", "qty_kg": 2250},
            {"seq": 2, "date": second_purchase_date, "qty_kg": 2250},
        ],
        sourcing_plan=[
            {"market": "가락", "grade": "상", "qty_kg": quantity, "grade_unit_price": unit_price}
        ],
        payment_schedule=[
            {"seq": 1, "purchase_date": "2025-12-31", "payment_date": payment_dates[0], "qty_kg": 2250, "amount_krw": half_amount, "amount_max_krw": half_max_amount, "basis": "as_of_unit_price"},
            {"seq": 2, "purchase_date": second_purchase_date, "payment_date": payment_dates[1], "qty_kg": 2250, "amount_krw": half_amount, "amount_max_krw": half_max_amount, "basis": "as_of_unit_price"},
        ],
    )
    return payload


def test_FIN_EVAL_03_BASE_통과_STRESS_실패(monkeypatch, evaluation_request):
    payload = _split_payload(
        deepcopy(evaluation_request.payload),
        second_purchase_date="2026-01-07",
        unit_price=7_000,
        max_price=10_000,
    )
    reply, _ = _call(monkeypatch, AgentRequest(evaluation_request.context, "finance", "SCENARIO_VALIDATION", payload=payload))
    verdict = reply.payload["verdicts"][0]
    assert reply.runtime_status == "READY"
    assert reply.business_status == verdict["verdict"] == "conditional"
    assert verdict["rule_id"] == "FIN-BASE-STRESS"
    assert verdict["scenario_projected_cash_min"] >= 10_000_000
    assert verdict["stress_projected_cash_min"] < 10_000_000


def test_FIN_EVAL_04_BASE_최소현금_위반(monkeypatch, evaluation_request):
    reply, _ = _call(monkeypatch, evaluation_request, cash=Decimal(5_000_000))
    verdict = reply.payload["verdicts"][0]
    assert reply.runtime_status == "READY"
    assert reply.business_status == verdict["verdict"] == "reject"
    assert verdict["rule_id"] == "FIN-BASE-MIN-CASH"


def test_FIN_EVAL_05_분할_지급일이_현금흐름을_바꾼다(monkeypatch, evaluation_request):
    early = _split_payload(deepcopy(evaluation_request.payload), second_purchase_date="2026-01-07")
    late = _split_payload(deepcopy(evaluation_request.payload), second_purchase_date="2026-01-14")
    receivable = CashEvent(event_date=date(2026, 1, 15), event_type="RECEIVABLE", amount_krw=Decimal(10_000_000), direction="INFLOW", ref_id="FIN-EVAL-INFLOW")
    early_reply, _ = _call(monkeypatch, AgentRequest(evaluation_request.context, "finance", "SCENARIO_VALIDATION", payload=early), cash=Decimal(30_000_000), cash_events=(receivable,))
    late_reply, _ = _call(monkeypatch, AgentRequest(evaluation_request.context, "finance", "SCENARIO_VALIDATION", payload=late), cash=Decimal(30_000_000), cash_events=(receivable,))
    early_verdict = early_reply.payload["verdicts"][0]
    late_verdict = late_reply.payload["verdicts"][0]
    assert early_verdict["payment_schedule"] != late_verdict["payment_schedule"]
    assert early_verdict["scenario_projected_cash_min"] < late_verdict["scenario_projected_cash_min"]
    assert (early_verdict["verdict"], late_verdict["verdict"]) == ("reject", "ok")


def test_FIN_EVAL_06_N5_누락은_공개_진입점에서_명시적으로_드러난다(monkeypatch, evaluation_request):
    reply, _ = _call(monkeypatch, evaluation_request, days=None)
    assert (reply.runtime_status, reply.business_status) == ("RUNTIME_NOT_READY", "skipped")
    assert reply.missing_data == ("purchase_payment_days",)


def test_FIN_EVAL_07_급여_근거_누락(monkeypatch, evaluation_request):
    reply, _ = _call(monkeypatch, evaluation_request, payroll_refs=False)
    assert (reply.runtime_status, reply.business_status) == ("RUNTIME_NOT_READY", "skipped")
    assert set(reply.missing_data) == {
        "monthly_labor_cost_krw@policy_source_ref",
        "payroll_date@policy_source_ref",
    }


def test_FIN_EVAL_08_재무상태_as_of_불일치(monkeypatch, evaluation_request):
    reply, _ = _call(monkeypatch, evaluation_request, state_date=date(2025, 12, 30))
    assert (reply.runtime_status, reply.business_status) == ("RUNTIME_NOT_READY", "skipped")
    assert reply.missing_data == ("finance_state@2025-12-31",)


def test_FIN_EVAL_09_잘못된_Purchase_계약(monkeypatch, evaluation_request):
    payload = deepcopy(evaluation_request.payload)
    del payload["scenarios"][0]["sourcing_plan"]
    reply, _ = _call(monkeypatch, AgentRequest(evaluation_request.context, "finance", "SCENARIO_VALIDATION", payload=payload))
    assert (reply.runtime_status, reply.business_status) == ("ERROR", "skipped")
    assert reply.payload["validation_errors"]


def test_FIN_EVAL_10_복수_시나리오는_독립적이다(monkeypatch, evaluation_request):
    payload = deepcopy(evaluation_request.payload)
    split = _split_payload(
        {**payload, "scenarios": [deepcopy(payload["scenarios"][0])]},
        second_purchase_date="2026-01-14",
    )["scenarios"][0]
    split["label"] = "보수"
    payload["scenarios"].append(split)
    reply, _ = _call(monkeypatch, AgentRequest(evaluation_request.context, "finance", "SCENARIO_VALIDATION", payload=payload))
    verdicts = reply.payload["verdicts"]
    assert reply.runtime_status == "READY"
    assert [item["scenario_id"] for item in verdicts] == ["기본", "보수"]
    assert verdicts[0]["payment_schedule"] != verdicts[1]["payment_schedule"]
    assert verdicts[0]["scenario_projected_cash_min"] != verdicts[1]["scenario_projected_cash_min"]
