"""SALES_VALIDATION Runtime Status 분류 — **못 한 세 가지를 섞지 않는다.**

★ 이 파일이 지키는 것은 하나다. *"판정을 못 냈다"* 에는 원인이 셋 있고, 셋은
  사용자와 마스터가 할 일이 다르다.

    ```text
    제안에 사실이 빠졌다      INPUT_INCOMPLETE  → READY / skipped      (영업이 고친다)
    재무 자료·정책이 없다      RUNTIME_NOT_READY → RUNTIME_NOT_READY    (기다린다)
    실행이 실제로 어긋났다     ERROR             → ERROR / skipped      (재시도한다)
    ```

  섞이면 마스터가 재시도 여부를 잘못 고른다. `ERROR` 는 `worth_retry=true` 라
  자료 부족이 `ERROR` 로 올라가면 **고쳐질 리 없는 재시도**가 돌고, 그 끝에 판매
  실행 전체가 FAILED 로 닫힌다.

★ `data_quality` 는 **분류 근거가 아니다.** 위 세 경우 모두 `INCOMPLETE` 일 수 있다 —
  무엇이 없느냐가 아니라 **누가 못 채웠느냐**가 Runtime 을 정한다.
"""

import json
from contextlib import nullcontext
from datetime import date
from unittest.mock import patch
from uuid import UUID

import pytest

from app.finance import user_messages as messages
from app.finance.adapter import finance_port
from app.master.envelope import AgentRequest, ExecutionContext

_EXECUTION = "app.finance.execution"


def _request(payload, *, mode="SALES_VALIDATION"):
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-sales-runtime",
            as_of=date(2025, 12, 31),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
            sim_run_id="SIM-TEST-RUN",
        ),
        agent="finance",
        mode=mode,
        payload=payload,
    )


def _sales_payload(**overrides):
    """온전한 판매 제안. `None` 을 넘긴 키는 **빠진 것**으로 다룬다."""
    payload = {
        "scenario_id": "SC-001",
        "partner_id": "P-100",
        "item": "red_pepper",
        "quantity_kg": "100",
        "unit_price_krw": "10000",
        "reported_sales_amount_krw": "1000000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": "2026-01-05",
        "source_ref": "SALES-REPLY:R-9",
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def _run(
    request, finance_context, *, receivables=(), receivables_error=None, sales_result=None
):
    """실제 DB·LLM 없이 Controller 를 돌린다.

    ★ LLM 을 끄는 이유는 속도가 아니라 **무엇을 시험하는지** 때문이다. Runtime 분류는
      결정론 경로가 소유하므로 모델 생사에 따라 답이 달라지면 안 된다.

    ★ `receivables_error` 는 **여기서** 받는다. 부르는 쪽에서 같은 대상을 다시
      patch 하면 이 함수의 patch 가 안쪽에 들어가 덮어써 버린다 — 조용히 정상 실행이
      되고, 장애를 시험한다고 믿는 검사가 통과한다.
    """

    def _capture(query, params):
        return {"run_id": UUID("00000000-0000-0000-0000-000000000009")}

    receivable_patch = (
        patch("app.finance.adapter.load_partner_receivables", side_effect=receivables_error)
        if receivables_error is not None
        else patch(
            "app.finance.adapter.load_partner_receivables", return_value=list(receivables)
        )
    )
    # ★ 판정이 실제로 난 결과는 오늘 저장소로는 만들 수 없다 — 여신한도가 없어서
    #   판정이 닫힌다. 그래서 **Tool 결과만** 정본 모양으로 갈아 끼운다. 정책을
    #   지어내 초록불을 만드는 것과 다르다: 여기서 보는 것은 확정된 판정이 어떤
    #   **문장**으로 나가는가이지, 그 판정이 어떻게 나왔는가가 아니다.
    capability_patch = (
        patch.dict(
            "app.finance.application.harness._CAPABILITIES",
            {"evaluate_sales_scenario": lambda port, args, state: dict(sales_result)},
        )
        if sales_result is not None
        else nullcontext()
    )
    with (
        patch(
            "app.finance.adapter.get_current_finance_runtime_context",
            return_value=finance_context,
        ),
        receivable_patch,
        capability_patch,
        patch("app.finance.llm.planner.finance_llm_enabled", return_value=False),
        patch("app.finance.adapter.finance_llm_enabled", return_value=False),
        patch(f"{_EXECUTION}.get_db_schema", return_value="haetdeul"),
        patch(f"{_EXECUTION}.execute_returning_one", side_effect=_capture),
    ):
        return finance_port(request)


def _harness_trace(metadata) -> dict:
    """실행 흔적. `failure_kind` 는 **여기에만** 산다 (사용자 회신에는 문장이 간다)."""
    for observation in metadata.observations:
        parsed = json.loads(observation)
        if parsed.get("observation_type") == "finance_harness_trace":
            return parsed
    raise AssertionError("finance_harness_trace 관측이 없다")


def _branch_ids(metadata) -> list[str]:
    return [step["branch_id"] for step in _harness_trace(metadata)["steps"]]


# ---------------------------------------------------------------------------
# A. Sales 입력 누락 — 재무는 멀쩡하다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field", ["unit_price_krw", "partner_id", "payment_terms_type", "source_ref"]
)
def test_missing_sales_input_stays_ready(finance_context, missing_field):
    """제안에 사실이 빠진 것은 **재무 고장이 아니다.**

    영업이 채워야 할 칸이 비었다고 재무 실행이 실패한 것으로 보고하면, 마스터는
    재무를 다시 부른다 — 다시 불러도 같은 칸이 비어 있다.
    """
    reply, _metadata = _run(
        _request(_sales_payload(**{missing_field: None})), finance_context
    )

    assert reply.runtime_status == "READY"
    assert reply.business_status == "skipped"
    assert reply.payload["status"] == "INPUT_INCOMPLETE"
    assert reply.payload["finance_verdict"] is None
    assert missing_field in reply.payload["missing_fields"]
    # 🔴 이 둘이 이번 회귀의 핵심이다.
    assert reply.runtime_status != "ERROR"
    assert reply.runtime_status != "RUNTIME_NOT_READY"


def test_missing_sales_input_is_not_disguised_as_finance_missing_data(finance_context):
    """`missing_fields` 를 `missing_data` 로 옮기지 않는다. **책임 주체가 다르다.**"""
    reply, _metadata = _run(
        _request(_sales_payload(unit_price_krw=None)), finance_context
    )

    assert reply.payload["missing_fields"] == ["unit_price_krw"]
    assert "unit_price_krw" not in reply.payload["missing_data"]
    assert "unit_price_krw" not in reply.missing_data


# ---------------------------------------------------------------------------
# B. 재무 authoritative 자료 부족 — 재무 쪽 사정이다
# ---------------------------------------------------------------------------


def test_missing_finance_authority_is_runtime_not_ready(finance_context):
    """여신한도는 **거래처가 소유한 사실**이고 아직 조회 계약이 없다.

    없는 한도를 재무가 발명하지 않으므로 판정은 닫히고, 그때 Runtime 은
    `RUNTIME_NOT_READY` 다 — 실행은 정상이었고 자료가 없었을 뿐이라 `ERROR` 가 아니다.
    """
    reply, metadata = _run(_request(_sales_payload()), finance_context)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.payload["status"] == "RUNTIME_NOT_READY"
    assert reply.payload["finance_verdict"] is None
    assert "partner_credit_limit_krw" in reply.payload["missing_data"]
    assert _harness_trace(metadata)["failure_kind"] == "NOT_READY"
    assert reply.runtime_status != "ERROR"


def test_runtime_not_ready_does_not_ask_master_to_retry(finance_context):
    """`RUNTIME_NOT_READY` 는 후속이 필요하지 재시도가 필요한 것이 아니다."""
    reply, _metadata = _run(_request(_sales_payload()), finance_context)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.needs_followup is True


# ---------------------------------------------------------------------------
# C. 실제 실행 장애 — 여기서만 ERROR 다
# ---------------------------------------------------------------------------


def test_internal_failure_is_still_error(finance_context):
    """DB 조회가 실제로 터지면 `ERROR` 다. **ERROR 자체를 없애지 않는다.**"""
    reply, metadata = _run(
        _request(_sales_payload()),
        finance_context,
        receivables_error=RuntimeError("connection refused"),
    )

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert _harness_trace(metadata)["failure_kind"] == "INTERNAL"


def test_persistence_failure_is_still_error(finance_context):
    """이력을 남기지 못한 실행은 확정되지 않는다 — 이것도 실제 장애다."""
    with (
        patch(
            "app.finance.adapter.get_current_finance_runtime_context",
            return_value=finance_context,
        ),
        patch("app.finance.adapter.load_partner_receivables", return_value=[]),
        patch("app.finance.llm.planner.finance_llm_enabled", return_value=False),
        patch("app.finance.adapter.finance_llm_enabled", return_value=False),
        patch(f"{_EXECUTION}.get_db_schema", return_value="haetdeul"),
        patch(
            f"{_EXECUTION}.execute_returning_one",
            side_effect=RuntimeError("connection refused"),
        ),
    ):
        reply, _metadata = finance_port(_request(_sales_payload()))

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"


# ---------------------------------------------------------------------------
# D. data_quality 는 Runtime 을 정하지 않는다
# ---------------------------------------------------------------------------


def test_incomplete_data_quality_alone_never_means_error(finance_context):
    """Tool 이 끝까지 돌았고 `data_quality=INCOMPLETE` 인 두 경우.

    같은 `INCOMPLETE` 라도 **원인이 다르면 Runtime 이 다르다.** 그리고 둘 다
    `ERROR` 가 아니다 — `ERROR` 는 실행이 어긋났을 때만 쓴다.
    """
    input_incomplete, _ = _run(
        _request(_sales_payload(unit_price_krw=None)), finance_context
    )
    finance_not_ready, _ = _run(_request(_sales_payload()), finance_context)

    for reply in (input_incomplete, finance_not_ready):
        assert reply.payload["data_quality"] == "INCOMPLETE"
        assert reply.payload["finance_verdict"] is None
        assert reply.runtime_status != "ERROR"

    # 같은 data_quality, 다른 Runtime — 원인으로 갈렸다는 증거다.
    assert input_incomplete.runtime_status == "READY"
    assert finance_not_ready.runtime_status == "RUNTIME_NOT_READY"


# ---------------------------------------------------------------------------
# E. missing_data 계보
# ---------------------------------------------------------------------------


def test_missing_data_lineage_survives_to_the_agent_reply(finance_context):
    """`payload.missing_data` → `AgentReply.missing_data` 가 끊기지 않는다.

    공통 Envelope 는 `RUNTIME_NOT_READY` 에서 무엇이 없었는지 이름을 요구한다.
    이름이 사라지면 "재무가 못 봤다" 만 남고 **무엇을 준비해야 하는지**가 사라진다.
    """
    reply, _metadata = _run(_request(_sales_payload()), finance_context)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    payload_missing = reply.payload["missing_data"]
    assert payload_missing, "payload 에 원인 이름이 없다"
    assert set(payload_missing) <= set(reply.missing_data)


def test_batch_runtime_not_ready_keeps_every_branch_missing_data(finance_context):
    """batch 에서도 안별 원인이 top-level 로 모인다."""
    payload = {
        "scenarios": [
            _sales_payload(scenario_id="SC-A"),
            _sales_payload(scenario_id="SC-B", unit_price_krw="20000"),
        ]
    }
    reply, _metadata = _run(_request(payload), finance_context)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    for result in reply.payload["scenario_results"]:
        assert set(result["missing_data"]) <= set(reply.missing_data)


# ---------------------------------------------------------------------------
# F. branch_id — 다른 Mode 이름을 빌려 쓰지 않는다
# ---------------------------------------------------------------------------


def test_sales_branch_id_never_falls_back_to_another_mode_name(finance_context):
    """`scenario_id` 가 없다고 `PRE_PURCHASE` 로 찍히면 **관측 정보가 거짓**이다."""
    reply, metadata = _run(
        _request(_sales_payload(scenario_id=None)), finance_context
    )

    branch_ids = _branch_ids(metadata)
    assert branch_ids, "Trace 에 분기가 없다"
    assert "PRE_PURCHASE" not in branch_ids
    assert all(branch_id.startswith("SALES_VALIDATION") for branch_id in branch_ids)
    # 식별자가 없는 것은 업무 사실 누락이지 실행 장애가 아니다.
    assert reply.runtime_status == "READY"
    assert reply.payload["status"] == "INPUT_INCOMPLETE"


def test_existing_scenario_id_stays_the_branch_id(finance_context):
    """있는 식별자는 **그대로 쓴다.** 기존 분기 identity 는 바뀌지 않는다."""
    _reply, metadata = _run(
        _request(_sales_payload(scenario_id="SC-777")), finance_context
    )

    assert set(_branch_ids(metadata)) == {"SC-777"}


def test_sales_branches_without_scenario_id_do_not_collide(finance_context):
    """🔴 이번 결함의 실제 경로.

    판매 Tool 은 **인자를 받지 않는다.** 그래서 Harness 중복 차단 키
    `(branch_id, tool, arguments)` 에서 두 축이 이미 같고, branch_id 마저 겹치면
    2안의 **첫 호출**이 1안의 재호출과 구별되지 않는다. 그 반려는 terminal 이라
    `RuntimeError` → `ERROR/INTERNAL` 이 됐다 — 자료가 부족한 제안이 **실행 장애**로
    승격되던 자리가 여기다.
    """
    payload = {
        "scenarios": [
            _sales_payload(scenario_id=None),
            _sales_payload(scenario_id=None, unit_price_krw="20000"),
        ]
    }
    reply, metadata = _run(_request(payload), finance_context)

    trace = _harness_trace(metadata)
    assert trace["denials"] == []
    assert len(set(_branch_ids(metadata))) == 2
    # 두 안이 **모두** 실제로 돌았다.
    assert trace["executed_tools"] == ["evaluate_sales_scenario"] * 2
    assert reply.runtime_status != "ERROR"
    assert len(reply.payload["scenario_results"]) == 2


# ---------------------------------------------------------------------------
# G. 사용자 설명 — 판정하지 못한 결과를 승인처럼 말하지 않는다
# ---------------------------------------------------------------------------

#: "그대로 진행해도 된다" 는 뜻을 만드는 표현. 판정이 없을 때 나오면 안 된다.
_APPROVAL_PHRASES = (
    "진행하실 수 있습니다",
    "진행할 수 있습니다",
    "승인",
    "문제 없습니다",
    "문제없습니다",
    "그대로 진행",
)


def _assert_not_approval(reply, label: str) -> None:
    for phrase in _APPROVAL_PHRASES:
        assert phrase not in reply.reasoning, (
            f"{label}: 승인성 표현 {phrase!r} -> {reply.reasoning!r}"
        )


def test_input_incomplete_reply_never_reads_as_approval(finance_context):
    """🔴 `INPUT_INCOMPLETE` 은 `READY`/`skipped` 다 — **기계 계약은 옳았다.**

    그런데 설명만 승인 문장으로 나갔다. 재무가 보지도 못한 제안을 사용자는
    "진행해도 된다" 로 읽는다 — 기계 계약이 맞을수록 더 위험하다.
    """
    reply, _metadata = _run(
        _request(_sales_payload(unit_price_krw=None)), finance_context
    )

    # 기계 계약은 그대로다.
    assert reply.runtime_status == "READY"
    assert reply.business_status == "skipped"
    assert reply.payload["status"] == "INPUT_INCOMPLETE"
    # 설명만 고쳤다.
    _assert_not_approval(reply, "INPUT_INCOMPLETE")
    assert "매입" not in reply.reasoning
    assert reply.reasoning != messages.FINANCE_EXPLANATIONS["SCENARIO_ACCEPT"]
    assert reply.reasoning == messages.FINANCE_EXPLANATIONS["SALES_NOT_CONCLUDED"]


def test_runtime_not_ready_keeps_its_own_explanation(finance_context):
    """`RUNTIME_NOT_READY` 설명 계약은 이번 수정에 영향받지 않는다.

    이쪽은 `_explain` 이 Finalizer 를 부르기 **전에** 접히는 경로라 설명 표가 다르다.
    두 경로가 섞이면 자료 부족과 판정 불가가 같은 문장으로 나간다.
    """
    reply, _metadata = _run(_request(_sales_payload()), finance_context)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.reasoning == messages.NOT_READY
    _assert_not_approval(reply, "RUNTIME_NOT_READY")


def test_sales_pass_speaks_about_selling_not_buying(finance_context):
    """판정이 실제로 난 판매 제안은 **판매 문장**으로 답한다."""
    reply, _metadata = _run(
        _request(_sales_payload()),
        finance_context,
        sales_result={
            "status": "EVALUATED",
            "finance_verdict": "PASS",
            "scenario_id": "SC-001",
            "financial_summary": None,
            "rule_results": [],
            "reason_codes": [],
            "missing_fields": [],
            "missing_data": [],
            "data_quality": "COMPLETE",
            "max_finance_allowed_amount_krw": None,
            "max_finance_allowed_payment_terms_days": None,
            "evidence_refs": [],
        },
    )

    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert reply.payload["finance_verdict"] == "PASS"
    assert "매입" not in reply.reasoning, reply.reasoning
    assert reply.reasoning == messages.FINANCE_EXPLANATIONS["SALES_ACCEPT"]
