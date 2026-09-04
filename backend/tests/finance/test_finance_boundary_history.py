"""어댑터 경계에서 끝난 실행도 흔적을 남기는가.

Controller 에 닿은 실행은 `FinanceAgentController.run` 이 저장한다. 문제는 **닿지
못한 실행**이었다 — 재무 상태를 못 읽었거나 시나리오 입력이 틀려 어댑터가 바로
돌아가는 경로는 `finance_agent_runs_v22` 에 아무것도 남기지 않았고, 남길 수도
없었다(`run_id` 가 UUID 가 아니었다).

여기서 고정하는 것:
  · 경계 회신도 저장된다 — 그리고 **한 번만** 저장된다
  · 저장 실패가 업무 답(`RUNTIME_NOT_READY`)을 뒤집지 않는다
  · `STATUS_QUERY` 는 여전히 결정론이고, 저장 대상이 아니다
  · 죽은 어댑터 구현이 사라졌다
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from typing import ClassVar
from unittest.mock import patch
from uuid import UUID

import pytest

from app.finance import adapter
from app.finance.application.orchestration import FinanceAgentController
from app.finance.execution import _finance_dept_meta
from app.finance.llm.planner import ToolAction
from app.finance.state import FinanceAgentState
from app.master.envelope import AgentRequest, ExecutionContext
from tests.finance.test_finance_adapter import _Context, _Policy

AS_OF = date(2025, 12, 31)


def req(mode="PRE_PURCHASE", as_of: date = AS_OF, payload=None) -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(
            request_id="REQ-HISTORY-0001",
            as_of=as_of,
            trigger="USER_REQUEST",
            policy_version="POLICY-V1",
        ),
        agent="finance",
        mode=mode,
        payload=payload or {},
    )


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


@pytest.fixture(autouse=True)
def controller_wired(monkeypatch):
    """Planner 와 **이력 저장**을 격리한다.

    🔴 이력 저장을 안 막으면 이 파일의 계약 테스트가 **실 DB 에 의존한다.** DB 가 없는
       곳에서는 `save_finance_execution` 이 터지고, Controller 는 그것을 의도대로
       `ERROR` 로 접는다 — 그래서 DeptMeta 를 보려던 테스트가 *"READY 인데 ERROR"* 로
       깨진다. **production 결함이 아니라 격리 누락**이다 (Tool 은 4개 다 돌았고
       `llm_status` 도 SUCCESS 였다).

    ★ 형제 파일 `test_finance_adapter.py` 는 처음부터 같은 줄을 갖고 있었다. 이 파일만
      빠져 있었다.

    ★ 이력 **자체**를 검증하는 테스트는 각자 `with patch(...)` 로 다시 덮어쓰므로
      영향이 없다 — 그쪽이 저장 호출 수를 직접 센다.
    """
    monkeypatch.setattr(
        adapter,
        "FinanceAgentController",
        lambda port: FinanceAgentController(port, _AdapterPlanner()),
    )
    monkeypatch.setattr("app.finance.execution.save_finance_execution", lambda **_kwargs: None)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: _Context())


# ---------------------------------------------------------------------------
# run_id — 저장할 수 있는 형식인가
# ---------------------------------------------------------------------------


def test_adapter_run_id_is_a_uuid_per_execution():
    """🔴 예전에는 `FIN-{request_id}-{call_seq}` 라 UUID 기본키에 넣을 수 없었다.

    UUID 로 바꾸되 **요청 식별자가 아니라 실행 식별자**여야 한다. 한때 uuid5 로
    (request_id, call_seq, mode) 를 넣어 결정론을 지키려 했는데, 그러면 같은 요청을
    두 번 실행할 때 같은 run_id 가 나온다 — 이력은 append-only 이고 run_id 가
    기본키라 **두 번째 실행이 통째로 사라진다.**
    """
    first = adapter._run_id(req())
    second = adapter._run_id(req())
    assert UUID(first) and UUID(second)  # 파싱되지 않으면 저장 자체가 불가능하다
    assert first != second


def test_same_request_executed_twice_writes_two_history_rows(monkeypatch):
    """동일 AgentRequest 재실행 → 서로 다른 run_id → 이력 2건 모두 저장된다.

    `/finance/agent` 는 `request_id` 와 `call_seq` 를 클라이언트에게서 받는다. 같은
    봉투를 두 번 보내는 것은 막을 수 없고, 막을 일도 아니다 — 재실행은 **지워야 할
    중복이 아니라 남아야 할 두 번째 실행**이다.
    """
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: None)
    with patch("app.finance.adapter.save_finance_execution") as saved:
        first_reply, _ = adapter.finance_port(req())
        second_reply, _ = adapter.finance_port(req())

    assert first_reply.run_id != second_reply.run_id
    assert saved.call_count == 2
    stored = [call.kwargs["reply"].run_id for call in saved.call_args_list]
    assert len(set(stored)) == 2  # 기본키 충돌 없음


def test_controller_run_ids_are_also_per_execution(wired):
    """Controller 경로도 같은 규칙이다 — 어댑터만 다르게 굴면 이력 축이 갈린다."""
    with patch("app.finance.execution.save_finance_execution") as saved:
        first, _ = adapter.finance_port(req())
        second, _ = adapter.finance_port(req())

    assert first.runtime_status == "READY"
    assert first.run_id != second.run_id
    assert saved.call_count == 2


# ---------------------------------------------------------------------------
# 경계 실행이력
# ---------------------------------------------------------------------------


def test_boundary_not_ready_is_recorded_once(monkeypatch):
    """재무 상태를 못 읽어 Controller 에 닿지 못한 실행도 이력에 남는다."""
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: None)
    with patch("app.finance.adapter.save_finance_execution") as saved:
        reply, metadata = adapter.finance_port(req())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert set(reply.missing_data) == {"finance_state", "finance_policy"}
    # ★ Controller 는 부르지도 않았다 — 이중 저장이 아니다.
    assert saved.call_count == 1
    assert saved.call_args.kwargs["reply"] is reply
    assert saved.call_args.kwargs["metadata"] is metadata


def test_as_of_mismatch_boundary_is_recorded(monkeypatch):
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: _Context())
    with patch("app.finance.adapter.save_finance_execution") as saved:
        reply, _ = adapter.finance_port(req(as_of=date(2026, 1, 1)))

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert saved.call_count == 1


def test_missing_payroll_source_boundary_is_recorded(monkeypatch):
    class _NoPayrollRef(_Context):
        class policy(_Policy):
            source_refs: ClassVar[dict[str, str]] = {}

    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: _NoPayrollRef())
    with patch("app.finance.adapter.save_finance_execution") as saved:
        reply, _ = adapter.finance_port(req())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert saved.call_count == 1


def test_invalid_scenario_input_is_recorded(monkeypatch):
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: _Context())
    with patch("app.finance.adapter.save_finance_execution") as saved:
        reply, _ = adapter.finance_port(req("SCENARIO_VALIDATION", payload={"nope": 1}))

    assert reply.runtime_status == "ERROR"
    assert reply.payload["validation_errors"]
    assert saved.call_count == 1


def test_ready_run_is_saved_by_the_controller_only(wired):
    """★ 이중 저장 금지 — 정상 완료는 Controller 가 한 번만 저장한다."""
    with (
        patch("app.finance.execution.save_finance_execution") as controller_saved,
        patch("app.finance.adapter.save_finance_execution") as adapter_saved,
    ):
        reply, _ = adapter.finance_port(req())

    assert reply.runtime_status == "READY"
    assert controller_saved.call_count == 1
    assert adapter_saved.call_count == 0


def test_history_failure_does_not_change_the_business_answer(monkeypatch):
    """이력이 안 써져도 "재무 상태를 못 읽었다"는 사실은 그대로다.

    다만 감추지는 않는다 — 실패 자체가 observations 에 남아 마스터가 볼 수 있다.
    """
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: None)
    with patch(
        "app.finance.adapter.save_finance_execution",
        side_effect=RuntimeError("no database here"),
    ):
        reply, metadata = adapter.finance_port(req())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert any("finance_run_persistence_failed" in item for item in metadata.observations)


# ---------------------------------------------------------------------------
# STATUS_QUERY — 결정론 유지, 저장 대상 아님
# ---------------------------------------------------------------------------


def test_status_query_stays_deterministic_and_calls_no_llm(wired, monkeypatch):
    """조회는 Planner 를 타지 않는다 — 단순 조회를 억지로 Agent 에 넣지 않는다."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("STATUS_QUERY must not build a Finance Agent Controller")

    monkeypatch.setattr(adapter, "FinanceAgentController", _boom)
    reply, metadata = adapter.finance_port(req("STATUS_QUERY"))

    assert reply.runtime_status == "READY"
    assert metadata.llm_attempts == 0
    assert reply.payload["payment_pressure"]
    assert reply.evidences


def test_status_query_is_not_written_to_the_v22_history(wired):
    """`finance_agent_runs_v22.mode` CHECK 가 두 core mode 만 허용한다.

    조회 이력을 남기려면 스키마를 고쳐야 하고 그것은 Finance 코드 밖이다 —
    억지로 넣으면 INSERT 가 CHECK 위반으로 죽는다. 지금은 남기지 않는 것이 맞다.
    """
    with patch("app.finance.adapter.save_finance_execution") as saved:
        reply, _ = adapter.finance_port(req("STATUS_QUERY"))

    assert reply.runtime_status == "READY"
    assert saved.call_count == 0


def test_status_query_never_labels_a_policy_value_with_the_state_row(monkeypatch):
    """🔴 정책값 근거가 스냅샷 id 로 떨어지면 **거짓 출처**다.

    조회는 부분 응답을 낸다 — 근거를 못 다는 claim 만 빠지고, 현재 잔액처럼 근거가
    멀쩡한 값은 그대로 답한다. 조회 전체가 새로 막히지 않는다.
    """

    class _NoPolicyRefs(_Context):
        class policy(_Policy):
            source_refs: ClassVar[dict[str, str]] = {
                "monthly_labor_cost_krw": "SRC-FIN-PERSONA",
                "payroll_date": "SRC-FIN-N6",
            }

    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: _NoPolicyRefs())
    reply, _ = adapter.finance_port(req("STATUS_QUERY"))

    assert reply.runtime_status == "READY"
    # 낼 수 있는 값은 그대로 낸다.
    assert reply.payload["available_cash"]
    assert reply.payload["payment_pressure"]
    # 근거를 못 다는 정책 claim 만 빠진다.
    assert "minimum_cash_balance_krw" not in reply.payload
    assert "critical_payment_dates" not in reply.payload
    assert "projection_days" not in reply.payload
    # 빠진 사실을 숨기지 않는다.
    assert "minimum_cash_balance_krw@policy_source_ref" in reply.missing_data
    assert "cashflow_projection_days@policy_source_ref" in reply.missing_data
    # 어떤 정책값도 재무 상태 행 id 를 근거로 달지 않는다.
    state_row_id = _Context.snapshot.finance_state_id
    for evidence in reply.evidences:
        if evidence.claim in {"minimum_cash_balance_krw", "projection_days"}:
            assert state_row_id not in evidence.ref_ids


def test_status_query_keeps_policy_refs_when_they_exist(wired):
    """출처가 있으면 그대로 싣는다 — 이번 변경이 정상 조회를 좁히지 않는다."""
    reply, _ = adapter.finance_port(req("STATUS_QUERY"))

    assert reply.runtime_status == "READY"
    by_claim = {item.claim: item for item in reply.evidences}
    assert by_claim["minimum_cash_balance_krw"].ref_ids == (
        "PROJECT-DEFINITION-V1.2:minimum_cash_balance",
    )
    assert by_claim["projection_days"].ref_ids == ("MVP-DECISION-20260825:FIN-CASH-01",)
    assert reply.payload["critical_payment_dates"] is not None


def test_status_query_llm_status_reflects_the_setting(wired, monkeypatch):
    """🔴 켜 뒀는데 안 부른 것은 `DISABLED` 가 아니라 `SKIPPED_TEMPLATE` 다."""
    monkeypatch.setattr("app.finance.adapter.finance_llm_enabled", lambda: True)
    _reply, metadata = adapter.finance_port(req("STATUS_QUERY"))
    assert metadata.llm_status == "SKIPPED_TEMPLATE"

    monkeypatch.setattr("app.finance.adapter.finance_llm_enabled", lambda: False)
    _reply, metadata = adapter.finance_port(req("STATUS_QUERY"))
    assert metadata.llm_status == "DISABLED"


# ---------------------------------------------------------------------------
# 죽은 구현이 사라졌는가
# ---------------------------------------------------------------------------


def test_legacy_adapter_implementations_are_gone():
    """★ 정본은 Controller 경로 하나다.

    `_pre_purchase` · `_scenario_validation` 은 새 Agent 이전의 어댑터 자체 구현이다.
    저장소 어디에서도 부르지 않는데 남아 있어서, 읽는 사람이 **어느 쪽이 정본인지**
    를 매번 다시 확인해야 했다.
    """
    assert not hasattr(adapter, "_pre_purchase")
    assert not hasattr(adapter, "_scenario_validation")
    # 죽은 구현만 쓰던 레거시 의존도 함께 사라졌다.
    assert not hasattr(adapter, "run_finance_procurement_with_context")
    assert not hasattr(adapter, "classify_base_stress")
    assert not hasattr(adapter, "_calculate_schedule_cap")


def test_finance_port_dispatches_only_to_the_controller_path():
    source = adapter.finance_port.__code__.co_names
    assert "_controller_pre_purchase" in source
    assert "_controller_scenario_validation" in source
    assert "_status_query" in source


# ---------------------------------------------------------------------------
# DeptMeta — 재무가 자기 실행 사실을 기계가 읽을 형태로 낸다
#
# Critic 의 `E-AUTHORITY` · `E-GRADE-LEAK` 는 이것이 없으면 아예 돌지 않는다.
# 마스터는 재무가 무엇을 읽었는지 알 수 없으므로 **재무가 직접 적어야 한다.**
# ---------------------------------------------------------------------------


def _dept_meta(metadata):
    return next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_dept_meta"
    )


def test_pre_purchase_emits_finance_dept_meta(wired):
    reply, metadata = adapter.finance_port(req())
    assert reply.runtime_status == "READY"

    meta = _dept_meta(metadata)
    inputs = meta["inputs_used"]["finance_cap_amount_krw"]
    assert inputs, "cap 을 냈으면 무엇을 읽었는지도 나와야 한다"
    assert meta["produced_fields"]


def test_dept_meta_inputs_are_observed_not_declared():
    """★ 선언이 아니라 **관측**이다 — 실제로 돌린 Tool 만 입력에 반영된다.

    목록을 손으로 적어 두고 실행과 어긋나면, Critic 은 우리가 적은 거짓말을 검사한다.
    """
    state = FinanceAgentState(req())
    state.tool_order.append("assess_finance_position")

    meta = _finance_dept_meta("PRE_PURCHASE", {"available_cash": 1000}, [state])
    inputs = meta["inputs_used"]["finance_cap_amount_krw"]
    assert "finance_state.current_cash_krw" in inputs
    # cap Tool 을 안 돌렸으므로 그 Tool 의 입력은 나타나지 않는다.
    assert "base_projection.projected_cash_by_date" not in inputs

    state.tool_order.append("calculate_purchase_finance_cap")
    widened = _finance_dept_meta("PRE_PURCHASE", {"available_cash": 1000}, [state])
    assert (
        "base_projection.projected_cash_by_date"
        in widened["inputs_used"]["finance_cap_amount_krw"]
    )


def test_dept_meta_reports_no_purchase_owned_inputs(wired):
    """PRE_PURCHASE 는 매입 소유 입력을 읽지 않는다 — 그래서 나타나지 않는다.

    ★ 읽게 되는 날이 오면 **숨기지 말고 나타나야 한다.** 그것이 `E-GRADE-LEAK` 의
      존재 이유이고, 여기서 고정하는 것은 *"안 읽었다"* 가 아니라 *"읽은 대로 적는다"* 다.
    """
    _reply, metadata = adapter.finance_port(req())
    inputs = _dept_meta(metadata)["inputs_used"]["finance_cap_amount_krw"]

    forbidden = {"grade_unit_price", "qty_kg", "total_qty_kg", "avg_unit_price", "sourcing_plan"}
    assert not forbidden & set(inputs)
    # payload 에도 매입 소유 축이 실리지 않았다 — 적은 것과 실제가 같다.
    assert not forbidden & set(_dept_meta(metadata)["produced_fields"])


def test_dept_meta_produced_fields_match_the_reply_payload(wired):
    """산출하지 않은 필드를 산출했다고 적으면 권한 검사가 엉뚱한 것을 본다."""
    reply, metadata = adapter.finance_port(req())
    produced = set(_dept_meta(metadata)["produced_fields"])

    assert produced == {key for key, value in reply.payload.items() if value is not None}
    # S3 전속 판정은 재무가 내지 않는다.
    assert "has_unmet_obligation" not in produced


def test_dept_meta_is_absent_when_the_run_is_not_ready(monkeypatch):
    """못 낸 실행에 사용 입력을 적으면, 하지 않은 일을 했다고 적는 것이다."""
    monkeypatch.setattr(adapter, "_load_context", lambda _as_of=None: None)
    with patch("app.finance.adapter.save_finance_execution"):
        reply, metadata = adapter.finance_port(req())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert not any("finance_dept_meta" in item for item in metadata.observations)


def test_scenario_validation_emits_dept_meta_with_produced_fields(wired, purchase_payload):
    """🔴 시나리오 판정도 DeptMeta 를 낸다.

    예전에는 경계(`PRE_PURCHASE`)만 냈다. 그러면 Critic 의 권한 검사(`E-AUTHORITY`)가
    **시나리오 산출 필드를 아예 못 본다** — 재무가 시나리오에서 S3 전속 판정을 내도
    검사가 돌지 않는다. 통과가 아니라 생략이다.

    ★ 다만 `inputs_used` 는 **비운다.** 시나리오 판정에는 재무 cap 등급 누출 검사에
      해당하는 축이 없다 — 없는 검사에 가짜 입력을 지어내지 않는다.
    """
    reply, metadata = adapter.finance_port(req("SCENARIO_VALIDATION", payload=purchase_payload))
    assert reply.runtime_status == "READY"

    meta = _dept_meta(metadata)
    assert meta["inputs_used"] == {}
    # 정적 목록이 아니라 **실제 확정된 payload** 에서 나온다.
    assert meta["produced_fields"] == sorted(
        key for key, value in reply.payload.items() if value is not None
    )
    assert meta["produced_fields"], "판정을 냈으면 산출 필드도 있어야 한다"
    # 재무는 S3 전속 판정을 내지 않는다.
    assert "has_unmet_obligation" not in meta["produced_fields"]


def test_finance_dept_meta_reaches_critic_and_runs_both_checks(wired):
    """실제 실행 → 마스터 실행계획 → critic_bridge → Critic 까지 한 번에 확인한다.

    🔴 이 연결이 없던 동안 Critic 은 매 실행 `finance: DeptMeta 미제출 —
       E-AUTHORITY·E-GRADE-LEAK 생략` 을 남겼다. 문구를 지우는 것이 목적이 아니라
       **두 검사가 실제로 도는 것**이 목적이므로, 커버리지가 늘었는지까지 본다.

    ★ `wired` 로 **DB 읽기만** 격리한다. 예전에는 실 `_load_context` 를 타서 DB 가 없는
      곳에서는 `RUNTIME_NOT_READY(finance_state, finance_policy)` 가 나왔다 — 검증하려던
      연결과 무관한 이유로 깨진 것이다. Controller · Tool · Evidence · DeptMeta ·
      마스터 운반 · Critic 은 **전부 실제로** 돈다.
    """
    from app.critic.service import run_critic_procurement
    from app.master import critic_bridge as bridge
    from app.master import wiring
    from app.master.budget import CallBudget
    from app.master.runner import MasterRunner
    from tests.master.test_critic_bridge import CONSTRAINTS, EVIDENCES, _proposal

    # 전역 레지스트리를 빌려 쓰고 **원래대로 돌려놓는다** — 뒤에 도는 테스트가
    # 우리가 남긴 배선을 물려받으면 실패 원인이 여기라는 것을 아무도 못 찾는다.
    saved_registry = wiring.registry()
    wiring.reset()
    wiring.register("finance", adapter.finance_port)
    context = ExecutionContext(
        request_id="REQ-DEPTMETA-E2E",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
    )
    try:
        runner = MasterRunner(context, wiring.registry(), CallBudget())
        with patch("app.finance.execution.save_finance_execution"):
            reply = runner.call("finance", "PRE_PURCHASE")
    finally:
        wiring._REGISTRY = saved_registry
    assert reply.runtime_status == "READY"

    # 마스터는 부서 관측을 **해석하지 않고** 실행계획에 담아 나른다.
    step = runner.plan.last("finance", "PRE_PURCHASE")
    assert step is not None and step.observations

    request = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
        observations={"finance": step.observations},
    )
    assert request.dept_meta is not None and "finance" in request.dept_meta

    verdict = run_critic_procurement(request)
    # 재무 미제출 문구가 사라졌다 — 물류는 이번 범위가 아니라 그대로 남는다.
    assert not any(
        s.startswith("finance:") and "DeptMeta" in s for s in verdict.skipped
    ), verdict.skipped
    # 두 검사가 실제로 돌았고, 재무 소유 입력만 썼으므로 findings 는 없다.
    #
    # 🔴 **커버리지 숫자로는 이걸 확인할 수 없다 (2026-09-01 마스터 정정).**
    #   전에는 `coverage["L1"][0] >= 7` 로 확인했는데, 그 7 은 `l1_ran += 2 if meta else 0`
    #   이 *"아무 부서나 하나 냈나"* 를 세어서 나온 값이었다. 재무만 내도 물류 몫까지
    #   가산돼 **숫자가 실제보다 후했다.**
    #
    #   이제 회신을 낸 부서가 전부 제출해야 가산된다. 이 테스트는 재무만 배선하므로
    #   가산이 없는 것이 맞다 — 대신 **위의 `skipped` 단언이 이 테스트의 증거다.**
    #   "재무 미제출 문구가 사라졌다" 가 곧 "재무 DeptMeta 가 Critic 에 닿았다" 이다.
    #
    #   ⚠️ 부분 제출이 0으로 세어지는 것은 **과소 보고**다. 어느 쪽으로 틀릴지 골라야
    #     한다면 안전한 쪽이고, 빠진 부서는 `skipped` 줄이 이름까지 적는다.
    assert verdict.coverage["L1"][0] == 5
    assert not [f.check_id for f in verdict.findings]


# ---------------------------------------------------------------------------
# 잘못된 SCENARIO_VALIDATION 입력 — 어댑터 밖으로 예외가 새지 않는다
#
# ★ `finance_port` 는 **예외를 올리지 않는다** (모듈 주석). 예외로 새면
#   `MasterRunner._invoke` 가 잡아 사유 없는 ERROR 로 접고, 어느 필드가 틀렸는지가
#   사라진다. 직접 API 로 들어오는 payload 는 마스터를 안 거치므로 더 그렇다.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"scenarios": []}, id="empty-scenarios"),
        pytest.param({"proposal_id": "P-1"}, id="missing-required-fields"),
        pytest.param({}, id="empty-payload"),
        pytest.param({"scenarios": [{"scenario_id": "S"}]}, id="malformed-scenario"),
    ],
)
def test_invalid_scenario_payload_is_a_recorded_error_not_an_exception(wired, payload):
    with patch("app.finance.adapter.save_finance_execution") as saved:
        reply, _metadata = adapter.finance_port(req("SCENARIO_VALIDATION", payload=payload))

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    # 어느 필드가 틀렸는지 남는다 — 비어 있으면 "틀렸다"는 사실만 남고 고칠 수가 없다.
    assert reply.payload["validation_errors"]
    assert all(reply.payload["validation_errors"])
    # 실행 사실이 이력에 남는다. Controller 에 닿지 않았으므로 한 번만이다.
    assert saved.call_count == 1
    assert saved.call_args.kwargs["reply"] is reply


def test_model_level_validation_error_falls_back_to_its_type(wired, purchase_payload):
    """★ `loc` 가 빈 오류도 **빈 문자열이 되지 않는다.**

    모델 수준 검증기(`@model_validator`)가 낸 오류는 `loc == ()` 이라 경로 표기가
    빈 문자열이 된다. 그대로 실으면 `validation_errors: [""]` 가 되어 *"뭔가 틀렸다"*
    만 남는다 — 그래서 `type` 으로 떨어진다 (`_invalid_scenario_input`).
    """
    payload = deepcopy(purchase_payload)
    payload["no_proposal_reason"] = "시나리오가 있는 제안에는 설정할 수 없다"

    with patch("app.finance.adapter.save_finance_execution"):
        reply, _ = adapter.finance_port(req("SCENARIO_VALIDATION", payload=payload))

    assert reply.runtime_status == "ERROR"
    assert "value_error" in reply.payload["validation_errors"]
    assert "" not in reply.payload["validation_errors"]
