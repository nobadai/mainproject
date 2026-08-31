"""재무 어댑터 — 번역이 계약을 지키는가.

★ DB 를 타지 않는다. `_load_context` 를 갈아 끼워 **번역만** 시험한다.
  실제 값의 정확성은 `app.finance.tools` 의 테스트가 본다 — 여기서 다시 보면
  같은 것을 두 번 검사하면서 도메인 변경에 어댑터 테스트가 깨진다.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

from app.finance import adapter
from app.finance.agent import FinanceAgentController, ToolAction
from app.master.envelope import AgentReply, AgentRequest, ExecutionContext, ExecutionMetadata, validate_reply

AS_OF = date(2025, 12, 31)


def ctx(as_of: date = AS_OF) -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-T-0001",
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
    )


def req(mode="PRE_PURCHASE", as_of: date = AS_OF, payload=None) -> AgentRequest:
    return AgentRequest(context=ctx(as_of), agent="finance", mode=mode, payload=payload or {})


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
    #: ★ **실제 DB 와 같은 이름을 쓴다** (2026-08-27 재무 Persona v1.6 정렬).
    #:   픽스처가 실물과 다른 이름을 들고 있으면 나중에 읽는 사람이 *"어느 쪽이
    #:   정본인가"* 를 다시 확인해야 한다. 어댑터는 이름을 검사하지 않고 **존재만**
    #:   보므로 값이 통과하는 것과는 무관하다 — 읽는 사람을 위한 정렬이다.
    source_refs: ClassVar[dict[str, str]] = {
        "purchase_payment_days": "FINANCE-DECISION-20260827:N5",
        "payroll_date": "SRC-FIN-N6",
        "monthly_labor_cost_krw": "SRC-FIN-PERSONA",
        "minimum_cash_balance_krw": "PROJECT-DEFINITION-V1.2:minimum_cash_balance",
        "cashflow_projection_days": "MVP-DECISION-20260825:FIN-CASH-01",
        "margin_defense_floor_rate": "PROJECT-DEFINITION-V1.2:MARGIN-DEFENSE-GRACE",
        "cash_priority_reference": "POL-CASH-PRIORITY",
        "cash_priority_high_ratio": "POL-CASH-HIGH",
        "cash_priority_medium_ratio": "POL-CASH-MEDIUM",
    }


class _Snapshot:
    state_date = AS_OF
    current_cash_krw = Decimal(50_000_000)
    finance_state_id = "FIN-STATE-1"
    snapshot_id = "FIN-SNAPSHOT-1"
    minimum_operating_cash_krw = Decimal(10_000_000)
    committed_outflows_krw = Decimal(0)
    unsettled_purchase_payables_krw = Decimal(0)
    receivables_krw = Decimal(0)
    current_debt_krw = Decimal(0)
    financial_limit_krw = Decimal(40_000_000)

    def model_dump(self, **_kwargs):
        return {
            "finance_state_id": self.finance_state_id,
            "sim_run_id": "SIM-1",
            "state_date": self.state_date,
            "state_type": "DAILY",
            "financing_mode": "NONE",
            "current_cash_krw": self.current_cash_krw,
            "minimum_operating_cash_krw": self.minimum_operating_cash_krw,
            "committed_outflows_krw": self.committed_outflows_krw,
            "unsettled_purchase_payables_krw": self.unsettled_purchase_payables_krw,
            "receivables_krw": self.receivables_krw,
            "current_debt_krw": self.current_debt_krw,
            "financial_limit_krw": self.financial_limit_krw,
        }


class _Context:
    snapshot = _Snapshot()
    policy = _Policy()
    cash_events: tuple = ()
    unresolved_sources: tuple = ()


@pytest.fixture(autouse=True)
def controller_wired(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "FinanceAgentController",
        lambda port: FinanceAgentController(port, _AdapterPlanner()),
    )
    monkeypatch.setattr("app.finance.agent.save_finance_execution", lambda **_kwargs: None)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(adapter, "_load_context", lambda: _Context())


class _AdapterPlanner:
    model = "test-finance-planner"

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


@pytest.mark.parametrize("mode", ["PRE_PURCHASE", "SCENARIO_VALIDATION"])
def test_컨트롤러_위임은_실행_메타데이터를_그대로_반환한다(
    wired, monkeypatch, purchase_payload, mode
):
    request = req(mode, payload=purchase_payload if mode == "SCENARIO_VALIDATION" else {})
    controller_reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent="finance",
        mode=mode,
        run_id="controller-run",
        runtime_status="READY",
        business_status="ok",
    )
    controller_metadata = ExecutionMetadata(
        run_id="controller-run",
        request_id=request.context.request_id,
        agent="finance",
        used_tools=("controller-tool",),
        tool_order=(7,),
        llm_status="FALLBACK",
        llm_model="finance-test-model",
        llm_attempts=3,
        llm_fallback_used=True,
        replans=2,
        elapsed_ms=41,
    )
    received = []

    class _Controller:
        def __init__(self, _port):
            pass

        def run(self, controller_request):
            received.append(controller_request)
            return controller_reply, controller_metadata

    monkeypatch.setattr(adapter, "FinanceAgentController", _Controller)
    reply, metadata = adapter.finance_port(request)

    assert received and received[0].context.policy_version == "POLICY-V1"
    assert reply.run_id == "controller-run"
    assert metadata is controller_metadata
    assert metadata.used_tools == ("controller-tool",)
    assert metadata.llm_status == "FALLBACK"


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
                "monthly_labor_cost_krw": "SRC-FIN-PERSONA",
                "payroll_date": "SRC-FIN-N6",
            }

    monkeypatch.setattr(adapter, "_load_context", lambda: _PayrollOnly())
    reply, _ = adapter.finance_port(req())
    assert reply.runtime_status == "READY"
    assert "purchase_payment_days@policy_source_ref" in reply.missing_data


# ---------------------------------------------------------------------------
# STATUS_QUERY — 조회는 경계가 아니라 상태를 답한다
#
# 🔴 재무 파트 리뷰 요청 (2026-08-28). 이 넷이 없어서 `projection_days` ·
#    `critical_payment_dates` 의 Evidence 누락이 머지 직전까지 안 잡혔다.
# ---------------------------------------------------------------------------


def test_조회가_봉투_검증을_통과한다(wired):
    """🔴 리뷰에서 나온 것 — `validate_reply()` 가 실제로 비어야 한다.

    `projection_days`(숫자)와 `critical_payment_dates`(스칼라 배열)는 둘 다
    `required_claims` 대상이라 Evidence 가 없으면 `E-EVIDENCE-MISSING` 이다.
    payload 에 값을 더할 때 근거를 안 달면 여기서 걸린다.
    """
    request = req(mode="STATUS_QUERY")
    reply, meta = adapter.finance_port(request)
    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert validate_reply(request, reply, meta) == ()


def test_조회는_매입용_경계를_안_싣는다(wired):
    """`finance_cap` · `purchase_payment_days` 는 *매입 판단을 위한 경계*다.

    "지금 자금 상황" 을 묻는 사람에게는 답이 아니다 — 조회와 실행의 답이 같아지면
    화면이 무엇을 보여야 할지 알 수 없다.
    """
    reply, _ = adapter.finance_port(req(mode="STATUS_QUERY"))
    for boundary in ("finance_cap", "purchase_payment_days", "margin_defense_floor_rate"):
        assert boundary not in reply.payload
    # 대신 상태는 싣는다
    assert reply.payload["available_cash"] == 50_000_000.0
    assert reply.payload["payment_pressure"] == "LOW"


def test_급여_출처가_없으면_현금은_답하고_투영만_뺀다(monkeypatch):
    """🔴 `PRE_PURCHASE` 와 갈리는 지점이다.

    실행 경로는 급여 유출이 빠진 투영으로 만든 `finance_cap` 이 **낙관적으로
    틀리기** 때문에 통째로 멈춘다. 조회는 실행으로 이어지지 않으므로, 투영이
    필요 없는 값(현재 잔액)은 답하고 **투영이 필요한 값만** 빼고 이름을 밝힌다.
    """

    class _NoPayroll(_Policy):
        source_refs: ClassVar[dict[str, str]] = {
            k: v for k, v in _Policy.source_refs.items() if k != "monthly_labor_cost_krw"
        }

    class _Ctx(_Context):
        policy = _NoPayroll()

    monkeypatch.setattr(adapter, "_load_context", lambda: _Ctx())
    request = req(mode="STATUS_QUERY")
    reply, meta = adapter.finance_port(request)

    assert reply.runtime_status == "READY"  # 조회 자체는 막지 않는다
    assert reply.payload["available_cash"] == 50_000_000.0
    assert reply.payload["minimum_cash_balance_krw"] == 10_000_000.0
    for projected in (
        "projection_days",
        "projected_cash_min",
        "payment_pressure",
        "critical_payment_dates",
    ):
        assert projected not in reply.payload
    assert "monthly_labor_cost_krw@policy_source_ref" in reply.missing_data
    # 뺀 값에 근거를 남기지 않았으니 봉투도 통과해야 한다
    assert validate_reply(request, reply, meta) == ()


def test_정책값_근거는_Policy_출처를_가리킨다(wired):
    """🔴 값이 Policy 에서 왔으면 근거도 Policy 를 가리켜야 한다.

    스냅샷 id(`FIN-STATE-1`)를 달면 *"재무 상태 행에서 온 수"* 라고 말하는 것이라
    **거짓 출처**다. 나중에 *"이 수가 어디서 왔나"* 를 따라가면 엉뚱한 곳에 닿는다.
    """
    reply, _ = adapter.finance_port(req(mode="STATUS_QUERY"))
    by_claim = {e.claim: e for e in reply.evidences}

    assert by_claim["minimum_cash_balance_krw"].ref_ids == (
        "PROJECT-DEFINITION-V1.2:minimum_cash_balance",
    )
    assert by_claim["projection_days"].ref_ids == ("MVP-DECISION-20260825:FIN-CASH-01",)
    # 스냅샷에서 온 값은 스냅샷을 가리킨다 — 둘이 섞이지 않는다
    assert by_claim["available_cash"].ref_ids == ("FIN-STATE-1",)


def test_목록형_조회_근거도_개수가_아니라_임계값이다(wired):
    """`critical_payment_dates` 는 스칼라 배열이라 **통째로 하나의 근거**를 요구한다.

    개수(1건)를 넣으면 답의 길이를 세어 답이라고 적는 것이다 — 그 목록을 만든
    임계값을 넣어야 *"왜 그날이 위험일인가"* 에 답이 된다.
    """
    reply, _ = adapter.finance_port(req(mode="STATUS_QUERY"))
    ev = next(e for e in reply.evidences if e.claim == "critical_payment_dates")
    assert ev.value == 10_000_000.0  # minimum_cash_balance_krw — 임계값
    assert ev.unit == "KRW"


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — Master가 전달하는 Purchase proposal 계약
# ---------------------------------------------------------------------------


def test_정상_Purchase_시나리오를_READY_판정으로_반환한다(wired, purchase_payload):
    request = req("SCENARIO_VALIDATION", payload=purchase_payload)
    reply, meta = adapter.finance_port(request)

    assert reply.runtime_status == "READY"
    assert reply.payload["verdicts"]
    assert reply.payload["verdicts"][0]["scenario_id"] == "기본"
    assert validate_reply(request, reply, meta) == ()


def test_Finance_Cap을_초과하면_정상_업무_reject를_반환한다(wired, purchase_payload):
    payload = deepcopy(purchase_payload)
    scenario = payload["scenarios"][0]
    scenario["total_amount_krw"] = 45_000_000
    scenario["sourcing_plan"] = [
        {"market": "가락", "grade": "상", "qty_kg": 4500, "grade_unit_price": 10_000}
    ]

    reply, _ = adapter.finance_port(req("SCENARIO_VALIDATION", payload=payload))
    assert reply.runtime_status == "READY"
    assert reply.business_status == "reject"
    assert reply.payload["verdicts"][0]["verdict"] == "reject"


def test_분할_지급의_실제_payment_date를_현금흐름에_쓴다(wired, purchase_payload):
    payload = deepcopy(purchase_payload)
    scenario = payload["scenarios"][0]
    scenario.update(
        total_qty_kg=2,
        total_amount_krw=200,
        max_price=120,
        split_plan=[
            {"seq": 1, "date": "2025-12-31", "qty_kg": 1},
            {"seq": 2, "date": "2026-01-01", "qty_kg": 1},
        ],
        sourcing_plan=[{"market": "가락", "grade": "상", "qty_kg": 2, "grade_unit_price": 100}],
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

    reply, _ = adapter.finance_port(req("SCENARIO_VALIDATION", payload=payload))
    assert reply.runtime_status == "READY"
    assert [row["payment_date"] for row in reply.payload["verdicts"][0]["payment_schedule"]] == [
        "2026-01-07", "2026-01-08"
    ]


def test_필수_Purchase_시나리오_필드가_없으면_명시적으로_ERROR(wired, purchase_payload):
    payload = deepcopy(purchase_payload)
    del payload["scenarios"][0]["sourcing_plan"]

    reply, _ = adapter.finance_port(req("SCENARIO_VALIDATION", payload=payload))
    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.payload["validation_errors"]


def test_Purchase_모델_수준_검증_오류도_안전하게_반환한다(wired, purchase_payload):
    payload = deepcopy(purchase_payload)
    payload["no_proposal_reason"] = "시나리오가 있는 제안에는 설정할 수 없다"

    reply, _ = adapter.finance_port(req("SCENARIO_VALIDATION", payload=payload))

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.payload["validation_errors"]
    assert "value_error" in reply.payload["validation_errors"]


def test_Purchase_as_of_불일치는_명시적으로_ERROR(wired, purchase_payload):
    payload = deepcopy(purchase_payload)
    payload["meta"]["as_of"] = "2025-12-30"
    payload["scenarios"][0]["split_plan"][0]["date"] = "2025-12-30"

    reply, _ = adapter.finance_port(req("SCENARIO_VALIDATION", payload=payload))
    assert reply.runtime_status == "ERROR"
    assert reply.payload["validation_errors"] == ["proposal.meta.as_of"]
