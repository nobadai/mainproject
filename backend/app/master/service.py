"""마스터 API 서비스 — 요청을 Flow 로 옮기고 결과를 응답으로 옮긴다.

★ 여기에 판단을 두지 않는다. 판단은 `flow.py` 에 있다.
  서비스는 **경계 변환**만 한다 — 그래야 API 모양이 바뀌어도 Flow 가 안 흔들린다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.master import persistence, wiring
from app.master.answer import facts_from_procurement, render_answer
from app.master.budget import CallBudget
from app.master.day_gate import check_day_gate
from app.master.decision import CommitmentOut
from app.master.decision_service import commitments_before, get_decisions
from app.master.envelope import ExecutionContext
from app.master.execution_calendar import build_execution_calendar
from app.master.execution_day import (
    CalendarNotCovered,
    ExecutionDayNotFound,
    HolidayCalendar,
    is_execution_day,
    next_execution_day,
)
from app.master.flow import ProcurementFlow, ProcurementOutcome, VerifierPort
from app.master.holiday_calendar import get_calendar
from app.master.inputs import (
    DEFAULT_GRADE,
    REQUEST_GRADE,
    MasterInputs,
    SourcedInput,
    collect_inputs,
    load_forecast,
)
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID, get_burn_in
from app.master.market_calendar import get_market_calendar
from app.master.plan import ExecutionPlan
from app.master.procurement_boundary import ProcurementBoundary, read_procurement_boundary
from app.master.report import render_report, report_filename
from app.master.run_repository import get_run_by_request_id
from app.master.runner import MasterRunner
from app.master.sales_flow import SalesFlow, SalesOutcome, sales_call_budget
from app.master.schemas import (
    AdjustmentOut,
    BlockedAgentOut,
    BurnInOut,
    DailyClosingOut,
    EvidenceOut,
    ProcurementRunRequest,
    ProcurementRunResponse,
    ReportOut,
    RunHistoryOut,
    SalesCandidateOut,
    SalesRunRequest,
    SalesRunResponse,
    StepOut,
)
from app.master.verifier import (
    MasterVerifier,
    SalesVerificationContext,
    SalesVerifier,
    SalesVerifierPort,
)

logger = logging.getLogger(__name__)

# 사유 문장에 요일을 적기 위한 이름. `date.weekday()` 순서 (월 0 … 일 6).
# ★ 로케일을 타지 않게 직접 적는다 — 서버 로케일에 따라 사유 문장이 갈리면 안 된다.
_WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")

# `date.weekday()` 가 토요일에 주는 값. 사유 문장이 **주말인지 공휴일인지**를 가리는 데만
# 쓴다 — 판정은 `execution_day` 가 한다.
_SATURDAY = 5


def make_request_id(as_of: str, seq: int = 1) -> str:
    """`REQ-20260826-0001`.

    ★ 시각이 아니라 **날짜 + 순번**이다. 같은 날 재실행을 구분하되 재현 가능해야 한다
      (§1.2-11). 순번 관리는 호출자 몫이며, 명시적으로 주는 편이 낫다.
    """
    return f"REQ-{as_of.replace('-', '')}-{seq:04d}"


def run_procurement(
    request: ProcurementRunRequest,
    verifier: VerifierPort | None = None,
) -> ProcurementRunResponse:
    """매입 Flow 를 한 번 돌리고 실행 계획을 적재한다.

    ★ 적재는 계산이 끝난 뒤다. 실패해도 응답을 막지 않는다 (§persistence).

    ★ `verifier` 를 주지 않으면 **마스터의 기본 검증 Tool** 이 붙는다 (§3.7.1).
      전에는 기본값이 `None` 이라 API 경로에서 검증이 통째로 건너뛰어졌다 —
      `verification_skipped: true` 로 드러나긴 했지만 아무도 안 봤다.
      **끄려면 명시적으로 꺼야 한다**(`NO_VERIFIER`)는 쪽이 안전하다.
    """
    verifier = MasterVerifier() if verifier is None else verifier
    started = time.perf_counter()
    request_id = request.request_id or make_request_id(request.as_of.isoformat())
    context = ExecutionContext(
        request_id=request_id,
        as_of=request.as_of,
        trigger=request.trigger,
        policy_version=request.policy_version,
        # 🔴 **어느 실행의 장부인가는 마스터가 정한다** (물류 `#325` · 2026-09-06).
        #   물류 조회 경로에는 생성자가 없어 봉투 말고 줄 자리가 없다 —
        #   `ExecutionContext` docstring 의 ①.
        #
        # 🔴 **요청이 주면 그 값이 이긴다** (`#531` 후속 · 2026-09-10). 전에는 여기서
        #    상수를 다시 적어 **걷기가 축을 줘도 축이 봉투 앞에서 끊겼다** — 판단 행이
        #    번인으로 앉고, 승인 경로가 그 행을 읽으니 원장까지 번인으로 돌아왔다.
        #
        # ★ **기본값을 없애지 않는다.** 라우터·화면은 이 칸을 안 준다. 대신 기본값으로
        #   떨어진 사실을 `_input_sources` 가 `DEFAULT:burn_in` 으로 적는다 —
        #   조용히 번인에 쌓지 않는다.
        sim_run_id=_sim_run_id_of(request),
    )

    # 🔴 **첫 관문은 개장이다** (계약 · 2026-09-06). 실행일 판정보다 **먼저**다 —
    #    그 날 장부가 안 열렸으면 실행일이어도 읽을 상태가 없다.
    #
    # ★ **두 관문은 다른 물음이다.** 토요일은 여기를 통과하고 아래에서 막힌다.
    day_gate = check_day_gate(request.as_of, sim_run_id=context.sim_run_id)
    if day_gate.gate == "BLOCKED":
        response = _empty_response(
            context,
            reason=day_gate.reason or "그날 장부가 안 열렸다",
            skipped_note="전 검사: 그날이 안 열려서 Flow 가 시작되지 않음",
        )
        response.day_gate = day_gate
        response.report_text = render_answer(facts_from_procurement(response))
        response.history_run_id = persistence.record(
            request, response, elapsed_ms=_elapsed(started), sim_run_id=context.sim_run_id
        )
        return response

    execution_day = _execution_day_verdict(request.as_of)
    if not execution_day.runs:
        # 주말·공휴일은 오류가 아니라 **안 도는 날**이다 — 어댑터 미등록과 같은 태도다 (§5.3).
        # 그날에는 ML 예측이 없다. 없는 값을 복사본으로 채워 판단하면
        # 그건 시장을 본 것이 아니라 금요일을 두 번 본 것이다.
        #
        # 🔴 **「시장이 안 선다」로 적지 않는다** (2026-09-09 · 매입이 짚었다).
        #   토요일은 `ml_calendar_days.is_open` 이 참이다 — 실측 34일 참 / 3일 거짓.
        #   시장은 서는데 **예측이 없는** 것이고, 우리가 안 도는 이유는 뒤쪽이다.
        response = _empty_response(
            context,
            reason=_not_execution_day_reason(request.as_of, execution_day.following),
            skipped_note="전 검사: 실행일이 아니어서 Flow 가 시작되지 않음",
        )
        # ★ **개장은 통과했다는 사실을 같이 낸다.** 토요일이 여기서 막힐 때 화면이
        #   *"안 열려서"* 와 *"장이 안 서서"* 를 구분할 수 있어야 한다.
        response.day_gate = day_gate
        # ★ 공휴일 축을 못 봤으면 그 사실도 같이 남긴다 — 접힌 이유가 주말이라도
        #   *"공휴일까지 봤다"* 로 읽히면 안 된다.
        response.skipped_checks = [*response.skipped_checks, *execution_day.skipped]
        # 못 돈 날도 사람이 읽을 수 있어야 한다 — 빈 응답을 그대로 내보내면 화면이 침묵한다
        response.report_text = render_answer(facts_from_procurement(response))
        # 🔴 **안 돈 날도 이력에 남긴다.** 어댑터 갈래와 같은 이유다 —
        #   안 부른 것과 안 도는 날인 것은 다르고, 이력이 비면 둘이 같아 보인다.
        response.history_run_id = persistence.record(
            request, response, elapsed_ms=_elapsed(started), sim_run_id=context.sim_run_id
        )
        return response

    missing = wiring.missing()
    if missing:
        # 어댑터 미구현은 오류가 아니라 "그 부서가 오늘 돌지 않는다"와 같다 (§5.3)
        response = _empty_response(
            context,
            reason=f"어댑터 미등록: {', '.join(missing)}",
            missing_adapters=list(missing),
        )
        # 못 돈 날도 사람이 읽을 수 있어야 한다 — 빈 응답을 그대로 내보내면 화면이 침묵한다
        response.report_text = render_answer(facts_from_procurement(response))
        # 어댑터가 없어 못 돈 날도 이력에 남긴다 — 안 부른 것과 못 부른 것은 다르다
        response.history_run_id = persistence.record(
            request, response, elapsed_ms=_elapsed(started), sim_run_id=context.sim_run_id
        )
        return response

    inputs = _inputs_for(request)
    commitments = _approved_commitments(request)
    # ⚠️ **달력이 출처표보다 먼저다** (`#300`). 출처표가 봉투를 실었는지도 적기 때문에,
    #   순서가 뒤집히면 아직 없는 것을 보고 매번 `MISSING` 이라고 적는다.
    calendar_envelope, calendar_skipped = _execution_calendar_payload(request.as_of)
    # 🔴 **주입한 키는 주입이라고 적는다** (매입 실측 2026-09-07 · `_input_sources`).
    sources = _input_sources(request, inputs, execution_calendar=calendar_envelope)
    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=request.budget))
    outcome = ProcurementFlow(
        runner,
        verifier=verifier,
        item=request.item,
        forecast=request.forecast or _payload(inputs, "forecast"),
        confirmed_orders=request.confirmed_orders or _payload(inputs, "confirmed_orders"),
        policy_values=request.policy_values or _payload(inputs, "policy_values"),
        prior_feedback=request.prior_feedback,
        approved_commitments=commitments.carried,
        # ★ **달력을 값으로 싣는다.** 매입은 봉투만 받는 파트라 `is_execution_day` 를
        #   인용해도 `calendar` 를 못 준다 — 인용하면 주말만 피한다.
        #   N4 · N5 와 같은 모양이다: 값은 아는 쪽이, 계산은 쓰는 쪽이.
        execution_calendar=calendar_envelope,
        # ★ 값과 출처를 **떼어 놓지 않는다.** 응답에만 싣던 것을 payload 에도 나른다.
        input_sources=sources,
        # 🔴 실어 주기만 하지 않고 **막는 쪽까지** 잇는다 (2026-09-03).
        #   응답의 `mocked_inputs` 는 화면 경고용이고, 이것은 실행을 세우는 용도다.
        mocked_inputs=inputs.mocked if inputs else (),
    ).run(has_unmet_obligation=request.has_unmet_obligation)

    response = _to_response(context, outcome, inputs, sources)
    response.day_gate = day_gate
    response.concerns = [
        *response.concerns,
        *_decision_collision(request_id),
        *_evidence_contract_concerns(outcome),
        # ★ 약정을 못 읽었거나 못 실은 사실 (#185 후속). 재호출로 안 고쳐지므로
        #   findings 가 아니라 concerns 다.
        *commitments.concerns,
    ]
    # 🔴 **돈 날에도 못 본 축은 적는다.** 공휴일 축이 빠진 채 "실행일이다" 라고 답했으면
    #   그건 *"주말이 아니다"* 까지만 확인한 것이다 — 비워 두면 다 봤다고 읽힌다
    #   (`verifier.py` 의 `skipped` 와 같은 규율).
    #
    # ★ **봉투를 못 실은 것도 같은 자리에 적는다.** 문 앞 판정은 통과했는데 지평 어딘가가
    #   안 덮이는 경우가 있다 — 그때 매입은 오늘까지의 동작(밀지 않음)으로 돈다.
    response.skipped_checks = [*response.skipped_checks, *execution_day.skipped, *calendar_skipped]
    response.report_text = render_answer(facts_from_procurement(response))
    # ★ 적재가 돌려준 행 id 를 **응답에 싣는다.** 화면이 승인할 때 이 값을 되돌려 줘야
    #   "내가 본 그것을 승인했다" 가 기록된다 (§DDL 안건 2026-08-30).
    response.history_run_id = persistence.record(
        request, response, elapsed_ms=_elapsed(started), sim_run_id=context.sim_run_id
    )
    return response


def run_sales(
    request: SalesRunRequest,
    verifier: SalesVerifierPort | None = None,
) -> SalesRunResponse:
    """판매 Flow 를 한 번 돌리고 이력에 남긴다 (설계 2026-09-07 §0).

    ```text
    개장 Gate  →  필수 어댑터 점검  →  SalesFlow  →  검증(Critic B)  →  이력 적재
    ```

    🔴 **검증을 지나간다** (2026-09-08). 그 전까지 `run_critic_sales` 는
      `critic/router.py` 에서 **HTTP 로만** 불렸다 — 매입은 판단 안에서 도는데 판매는
      아니었고, 그래서 *"두 시나리오 다 critic 검증을 거친다"* 가 사실이 아니었다.

    ★ `verifier` 를 주지 않으면 **기본 검증 Tool 이 붙는다** (`SalesVerifier`) —
      매입 `run_procurement` 와 같은 규율이다. 끄려면 명시적으로 꺼야 한다.

    ⚠️ **온전한 판정은 아직 안 난다.** 배분·로트 재료가 DB 에 0행이라 대개
      `skipped_checks` 에 *"못 냈다"* 가 남는다 — **통과가 아니다** (§3.7.6).

    🔴 **실행일 Gate 를 걸지 않는다 — 주말에도 판다.**

      두 관문은 다른 물음이다. 개장은 *"그 날 장부가 열렸는가"* 라 판매·매입이 같이
      지나고, 실행일은 *"장이 서서 ML 예측이 있는가"* 라 **매입만** 지난다. 팔 때는
      예측이 필요 없다 — 그것이 개장을 달력일로 정한 이유다 (설계 §1).

      토요일 요청은 개장을 통과하고 매입은 실행일에서 서지만 **판매는 그대로 간다.**
      여기에 `_execution_day_verdict` 를 복사해 넣으면 2026년 토요일 45일에 판매가
      멈춘다.

    🔴 **필수 어댑터는 부르기 전에 본다** (`wiring.REQUIRED_FOR_SALES`).

      골격이 `AgentNotRegistered` 를 `SL4_NOT_STARTED` 로 받으므로 터지지는 않는다
      (`sales_flow.py` 의 `run`). 하지만 **그때는 이미 다른 부서를 부른 뒤일 수 있다** —
      한 번이라도 부르면 그 회신이 이력에 남고, 나중에 읽는 사람이 *"돌긴 돌았다"* 로
      읽는다. 매입이 `wiring.missing()` 으로 문 앞에서 접는 것과 같은 이유다.

    ★ **필수는 제안자와 최종 검증자 둘뿐이다.** 물류는 여기 없다 — 판매는 밴드가 없어
      물류가 못 답해도 시작한다 (설계 §1-2). 목록이 왜 그 둘인지는 `wiring.py` 에 적혀
      있고, 여기서 다시 정하지 않는다.

    ★ **매입과 응답 조립을 공유하지 않는다.** 응답 모델도 종료 코드도 다르다 —
      묶으면 판매 종료 코드가 매입 어휘로 새거나 그 반대가 된다 (설계 §1).
    """
    verifier = SalesVerifier() if verifier is None else verifier
    started = time.perf_counter()
    request_id = request.request_id or make_request_id(request.as_of.isoformat())
    context = ExecutionContext(
        request_id=request_id,
        as_of=request.as_of,
        trigger=request.trigger,
        policy_version=request.policy_version,
        # ★ 어느 실행의 장부인가는 마스터가 정한다 (물류 `#325`) — 매입과 같은 값이다.
        #   판매도 물류를 부르므로(`PRE_SALES`) 같은 이유가 그대로 걸린다.
        #
        # 🔴 **요청이 주면 그 값이 이긴다 — 매입과 같은 자리다** (`#531` 후속).
        #    여기만 상수로 남기면 같은 날 매입 행과 판매 행이 서로 다른 실행에 앉고,
        #    `_procurement_boundary` 가 **남의 실행의 경계**를 읽는다.
        sim_run_id=_sim_run_id_of(request),
    )

    # 🔴 **첫 관문은 개장이다** — 매입과 **같은 판정 함수**를 부른다.
    #    잊으면 *"안 열린 날 판매가 돈다"* 인데, 막힌 게 아니라 안 막힌 것이라
    #    아무 오류도 안 난다. 그 조용한 실수를
    #    `tests/master/test_entrypoint_day_gate.py` 가 먼저 잡는다.
    day_gate = check_day_gate(request.as_of, sim_run_id=context.sim_run_id)
    if day_gate.gate == "BLOCKED":
        response = _empty_sales_response(
            context,
            reason=day_gate.reason or "그날 장부가 안 열렸다",
            skipped_note="전 검사: 그날이 안 열려서 판매 Flow 가 시작되지 않음",
        )
        response.day_gate = day_gate
        response.report_text = _sales_fold_note(response.end_code, response.reason)
        response.history_run_id = persistence.record_sales(
            request, response, elapsed_ms=_elapsed(started), sim_run_id=context.sim_run_id
        )
        return response

    # 🔴 **두 번째 관문은 배선이다** — 개장 **다음**이다. 안 열린 날인데 *"어댑터
    #    미등록"* 이라고 답하면 사람이 배선을 뒤지는데, 실제로는 그 날을 다시 열 일이다.
    #
    # ★ **빠진 이름을 사유에 적는다.** *"어댑터 미등록"* 만 적으면 무엇을 배선해야
    #   하는지 모른 채 코드를 뒤지게 된다.
    missing = wiring.missing(wiring.REQUIRED_FOR_SALES)
    if missing:
        # 미등록은 오류가 아니라 상태다 (§5.3) — 매입 갈래와 같은 태도, 판매 어휘.
        response = _empty_sales_response(
            context,
            reason=f"어댑터 미등록: {', '.join(missing)}",
            skipped_note="전 검사: 어댑터가 없어 판매 Flow 가 시작되지 않음",
        )
        response.day_gate = day_gate
        response.report_text = _sales_fold_note(response.end_code, response.reason)
        # 🔴 **못 부른 날도 이력에 남긴다.** 안 부른 것과 못 부른 것은 다르고,
        #    이력이 비면 둘이 같아 보인다.
        response.history_run_id = persistence.record_sales(
            request, response, elapsed_ms=_elapsed(started), sim_run_id=context.sim_run_id
        )
        return response

    # 🔴 **ML 예측은 마스터가 읽어 실어 준다** (판매 v1.7 §11 · M-1). 판매는 ML 을
    #    직접 부르지 않는다 — 부를 대상이 없다 (`inputs.py` 머리말).
    #
    # ★ **매입과 같은 자리다.** 매입은 `_inputs_for(request)` 로 읽어
    #   `ProcurementFlow(forecast=...)` 로 넘긴다. 여기도 진입점이 읽고 Flow 에 넘긴다 —
    #   Flow 가 직접 조회하면 조립기가 적재층을 겸하게 되고, 백테스트가 그날 값을
    #   꽂아 넣을 자리도 사라진다.
    forecast = _sales_forecast(request)
    runner = MasterRunner(context, wiring.registry(), sales_call_budget(request.budget))
    outcome = SalesFlow(
        runner,
        # 🔴 **매입에 실어 줄 경계 재료를 여기서 읽는다** (2026-09-10 · 라우팅 개방).
        #    `forecast` 와 같은 자리다 — 진입점이 읽고 Flow 에 넘긴다. Flow 가 직접
        #    조회하면 조립기가 적재층을 겸하게 된다.
        #
        # ★ **부서 호출이 0회다.** 값은 그날 매입 판단이 `PRE_PURCHASE` 로 받아 실행
        #   이력에 적어 둔 것이라, 물류·재무를 다시 부르지 않는다.
        procurement_boundary=_procurement_boundary(context),
        user_request=_sales_user_request(request),
        # 🔴 **최상위 필수 칸이라 따로 나른다** (`SalesProposalInput.business_mode`).
        #   `_sales_user_request` 안에 넣으면 판매 `SalesUserRequest` 가
        #   `extra="forbid"` 라 요청 전체가 문 앞에서 거부된다.
        business_mode=request.business_mode,
        forecast=forecast.payload if forecast.usable else None,
        # ★ 못 읽은 이유를 아는 곳은 여기다. Flow 는 자기가 아는 이유(look-ahead)만 쓴다.
        forecast_note=_sales_forecast_note(forecast),
        # 🔴 **mock 이면 세운다** — 매입과 같은 태도이고, 골격이 이미 그렇게 적어 두었다
        #    (`SalesFlow.mocked_inputs`). 실어 주기 시작했으니 막는 쪽도 같이 잇는다.
        mocked_inputs=("forecast",) if forecast.grade == "MOCK" else (),
    ).run()

    response = _to_sales_response(context, outcome)
    response.day_gate = day_gate

    # 🔴 **판단이 Critic B 를 지나간다.** 응답을 만든 뒤에 붙이는 이유는 검증이 후보와
    #    물류 컨텍스트를 **둘 다** 봐야 해서다 — 둘은 Flow 결과에만 같이 있다.
    #
    # ★ 매입이 싣는 모양을 그대로 따른다 — findings · concerns · skipped_checks.
    verification = verifier(
        SalesVerificationContext(
            as_of=request.as_of,
            item=request.item,
            candidates=tuple(c.scenario for c in outcome.candidates),
            supply_context=outcome.supply_context,
        )
    )
    response.findings = list(verification.findings)
    response.concerns = list(verification.concerns)
    response.skipped_checks = list(verification.skipped)

    response.report_text = _sales_fold_note(response.end_code, response.reason)
    response.history_run_id = persistence.record_sales(
        request, response, elapsed_ms=_elapsed(started), sim_run_id=context.sim_run_id
    )
    return response


def _procurement_boundary(context: ExecutionContext) -> ProcurementBoundary | None:
    """그날 매입 판단이 받아 둔 경계. **못 읽어도 사이클을 세우지 않는다.**

    🔴 **못 읽은 것과 안 읽은 것은 다르다.**

    ```text
    ProcurementBoundary(present=False, absent_reason=…)   물어봤는데 없다  → 사유가 봉투에 실린다
    None                                                  읽어 보지도 못했다
    ```

    ⚠️ **달력이 그날을 안 덮으면 주말 축만으로 다시 읽는다** (`_execution_day_axes` 와
      같은 처리). 잡지 않으면 공휴일 달력이 끊긴 날 **판매 사이클 전체가 선다** —
      경계는 조건부 재료이지 판매를 돌릴 수 있는지의 조건이 아니다.

    ⚠️ **`sim_run_id` 가 비면 읽지 않는다.** `read_procurement_boundary` 가 빈 축을
      `ValueError` 로 막는데(*"빈 축을 조용히 전체로 바꾸지 않는다"*), 그것을 여기서
      터뜨리면 봉투 `sim_run_id` 를 아직 안 채운 경로가 판매를 못 돌린다. 그 값은
      `ExecutionContext` 가 아직 필수로 안 만든 칸이다 (그 docstring 의 ①②③).
    """
    if not context.sim_run_id.strip():
        return None
    try:
        return read_procurement_boundary(
            as_of=context.as_of, sim_run_id=context.sim_run_id, calendar=get_calendar()
        )
    except CalendarNotCovered:
        return read_procurement_boundary(as_of=context.as_of, sim_run_id=context.sim_run_id)


def _sales_forecast(request: SalesRunRequest) -> SourcedInput:
    """판매에 실어 줄 ML 예측 하나 (§3.2.5 예외 · M-1).

    🔴 **셋을 다 모으지 않는다.** 매입은 `collect_inputs` 로 예측·확정주문·정책값을
      한꺼번에 읽지만 판매가 나르는 것은 **예측뿐**이다. 셋을 읽으면 판매가 쓰지도
      않는 `policy_values` 가 mock 인 날 판매가 서고, 그것은 없는 이유로 멈추는 것이다.

    ★ **못 읽어도 예외를 올리지 않는다** — 매입 `_inputs_for` 와 같은 태도다. ML DB 가
      죽었다고 판매가 통째로 못 도는 것은 아니다 (판매 v1.7: *"ML missing 은 전체
      Sales 실패가 아니다"*). 대신 **못 읽었다는 사실이 `SourcedInput` 에 남아** Flow 를
      거쳐 응답까지 간다.

    ★ 품목이 없으면 묻지 않는다 — 예측은 품목별이라 물을 대상이 없다.
    """
    if not request.item:
        return SourcedInput(
            key="forecast",
            payload=None,
            grade="MISSING",
            source="-",
            note="품목이 없어 ML 예측을 읽지 않았다",
        )
    try:
        return load_forecast(request.item, request.as_of)
    except Exception as exc:
        # ★ 조회 실패가 판매 실행을 막지 않는다. 다만 조용히 넘어가지도 않는다 —
        #   `_approved_commitments` 와 같은 자리다.
        logger.exception("판매에 실을 ML 예측 조회 실패 - 실행은 그대로 돈다")
        return SourcedInput(
            key="forecast",
            payload=None,
            grade="MISSING",
            source="-",
            note=f"ML 예측 조회 실패 ({type(exc).__name__})",
        )


def _sales_forecast_note(forecast: SourcedInput) -> str:
    """못 실은 이유 한 줄. **실을 수 있으면 빈 문자열이다.**

    ★ **문장을 새로 짓지 않는다.** 적재층이 쓴 `note` 를 그대로 옮기고, 없을 때만
      등급을 적는다 (`AgentFailure.detail` 과 같은 자리).
    """
    if forecast.usable:
        return ""
    return forecast.note or f"ML 예측을 못 실었다 ({forecast.grade})"


def _sales_user_request(request: SalesRunRequest) -> dict[str, Any] | None:
    """판매에 실어 보낼 사용자 요청. **묶기만 하고 해석하지 않는다** (§3.2.2).

    ★ **칸 이름은 판매 것이다** (`app/sales/schemas.py` `SalesUserRequest`) —
      `raw_text` · `item` · `partner_id`. 받는 쪽 낱말에 맞춘다.

    ★ **없는 칸은 안 만든다.** 빈 값을 실으면 받는 쪽이 *"사용자가 말 안 했다"* 와
      *"마스터가 안 보낸다"* 를 구별할 수 없다 (§1.2-10).

    ⚠️ **`business_mode` 는 여기 안 들어간다.** 판매 쪽 `SalesUserRequest` 는
      `extra="forbid"` 이고 `business_mode` 는 그 **바깥**(`SalesProposalInput`
      최상위)에 있다. 그 최상위 칸은 `SalesFlow(business_mode=...)` 가 나른다 —
      여기 넣으면 판매 문 앞에서 통째로 거부된다.

    🔴 **수량은 구조화된 칸으로 나른다** (실측 2026-09-07). 판매는 `raw_text` 를
      해석해 수량을 뽑지 않아서, 자유 문장만 보내면 `PROPOSAL_QUANTITY_REQUIRED`
      로 되돌아온다. 그래서 `SalesRunRequest.requested_quantity_kg` 가 생겼고
      여기서 **이름만 바꿔** 옮긴다 — 값을 만들지도 반올림하지도 않는다.

    ★ **나르지 않는 칸이 더 있다** (`preferred_*` 다섯). 요구한 caller 가 아직
      없어서인데, 그 사실은 `tests/master/test_sales_user_request_fields.py` 가
      판매 모델과 대조해 지킨다.
    """
    payload: dict[str, Any] = {}
    if request.user_request:
        payload["raw_text"] = request.user_request
    if request.item:
        payload["item"] = request.item
    if request.partner_id:
        payload["partner_id"] = request.partner_id
    if request.requested_quantity_kg is not None:
        # ★ **0 도 사실이다.** `if request.requested_quantity_kg` 로 적으면 *"0kg 을
        #   말했다"* 가 *"말하지 않았다"* 로 접힌다.
        #
        # 🔴 **`Decimal` 을 전선에 그대로 싣지 않는다** (#175 와 같은 규율).
        #   `json.dumps` 가 `Decimal` 에서 죽는다 — 봉투 payload 는 이력으로 한 번
        #   JSON 을 왕복하므로 여기서 편다. 봉투 표준형이 `target_value: float` 인
        #   것과 같은 자리이고, 판매 쪽 `Decimal` 파싱은 `str(float)` 을 거쳐
        #   값이 그대로다.
        payload["requested_quantity_kg"] = float(request.requested_quantity_kg)
    if request.preferred_delivery_date is not None:
        # 🔴 **없으면 안 싣는다.** 오늘이나 `as_of + N` 으로 대신 채우지 않는다 —
        #    그 값은 승인되면 `sales.sale_date` 가 되고 수금 기일의 출발점이 된다.
        payload["preferred_delivery_date"] = request.preferred_delivery_date.isoformat()
    if request.preferred_payment_days is not None:
        # ★ **0 도 사실이다** (`requested_quantity_kg` 과 같은 자리). `payment_days=0`
        #   은 *"당일 수금"* 이라는 정해진 조건이지 *"말하지 않았다"* 가 아니다.
        payload["preferred_payment_days"] = request.preferred_payment_days
    return payload or None


def _sales_fold_note(end_code: str, reason: str) -> str:
    """접힌 날 한 줄. **`SL1` 에서는 빈 문자열이다** (설계 §5).

    🔴 **마스터가 판매 문장을 짓지 않는다.** 추천 문장과 순위는 판매 소유이고 마스터는
      순위를 재계산하지 않는다 (판매 v1.7 §18). 매입은 `render_answer` 로 리포트를
      짓지만 판매는 판매 것을 나른다 — 여기서 문장을 만들기 시작하면 마스터가 **두
      번째 추천자**가 된다.

    ★ 다만 Flow 가 접힌 날(`SL2`~`SL5`)은 판매 문장 자체가 없다. 그때만 **왜 접혔는지**
      를 적고, 그것도 **실행 사실**이지 업무 판단이 아니다 — *"이 조건으로는 못 판다"*
      가 아니라 *"후보를 못 받았다"* 라고 쓴다.
    """
    말 = {
        "SL2_NO_CANDIDATE": "판매가 후보를 내지 못해 접혔다",
        "SL3_ALL_REJECTED": "후보가 전부 탈락해 접혔다",
        # ★ **탈락이라고 쓰지 않는다.** 후보는 살아 있고 판정만 안 끝났다 —
        #   여기서 "탈락" 이라고 적으면 종료 코드를 나눈 뜻이 문장에서 사라진다.
        "SL6_VALIDATION_UNRESOLVED": "후보는 있으나 검증이 끝나지 않아 접혔다",
        "SL4_NOT_STARTED": "시작하지 못했다",
        "SL5_BUDGET_EXHAUSTED": "호출 예산이 다해 판단이 끝나지 않았다",
    }.get(end_code)
    if 말 is None:
        # `SL1_PRESENTED` — 문장은 판매가 낸 것이 답이다.
        return ""
    return f"{말} ({end_code}): {reason}"


@dataclass(frozen=True)
class _ExecutionDayVerdict:
    """문 앞의 실행일 판정 하나. **본 것과 못 본 것을 같이 든다.**

    ```text
    runs       이 날 도는가
    following  안 돈다면 다음은 언제인가 (못 찾으면 None)
    skipped    못 본 축 — 비어 있으면 주말·공휴일을 다 봤다는 뜻이다
    ```
    """

    runs: bool
    following: date | None
    skipped: tuple[str, ...] = ()


def _execution_day_verdict(
    as_of: date, calendar: HolidayCalendar | None = None
) -> _ExecutionDayVerdict:
    """이 날 도는가. **공휴일 축을 붙여서 묻고, 못 붙으면 주말 축만으로 답한다.**

    🔴 **달력이 죽었다고 매입 판단이 멈추면 안 된다.** 공휴일을 못 보는 것은
       `#282` 이전의 동작이고, 그때도 판단은 돌았다. 여기서 막으면 ML 쪽 뷰 하나가
       마스터 전체의 정지 스위치가 된다.

    🔴 **그렇다고 조용히 넘어가지도 않는다.** 못 본 축은 `skipped` 로 나가서
       응답에 남는다 — *"안 한 것을 안 했다고 적는다"* (`verifier.py`).

    ★ **두 물음을 따로 판단한다.** *"오늘 도는가"* 는 달력이 오늘을 덮으면 답이
      나오고, *"다음은 언제인가"* 는 앞으로 걷다가 달력 밖으로 나갈 수 있다. 뒤가
      실패했다고 앞의 답까지 버리면, **덮이는 날의 공휴일 판정을 덮이지 않는 날
      때문에 잃는다.**

    ⚠️ 뷰가 `CURRENT_DATE` 까지만 나오므로 **오늘·미래를 묻는 호출은 달력 밖이다**
      (실측 2026-09-04: 뷰가 2025-09-04~2026-09-04). 그 경로가 곧 여기다.
    """
    calendar = get_calendar() if calendar is None else calendar
    skipped: list[str] = []

    try:
        runs = is_execution_day(as_of, calendar=calendar)
    except CalendarNotCovered as exc:
        skipped.append(f"공휴일 축: {as_of.isoformat()} 을 판정 못 함 — {exc}. 주말 축만 돌았다")
        runs = is_execution_day(as_of)

    if runs:
        return _ExecutionDayVerdict(True, None, tuple(skipped))

    following, why = _next_execution_day_or_none(as_of, calendar)
    if why:
        skipped.append(why)
    return _ExecutionDayVerdict(False, following, tuple(skipped))


def _next_execution_day_or_none(
    as_of: date, calendar: HolidayCalendar
) -> tuple[date | None, str]:
    """다음 실행일과, 그것을 **어떻게 골랐는지.** 사유가 비면 공휴일까지 보고 골랐다.

    ★ 달력 밖으로 나가면 **주말 축만으로 다시 고른다.** 사람이 읽는 사유에 *"다음에
      언제 도나"* 가 없는 것보다, 공휴일을 못 본 채 고른 날짜와 **그 사실**을 같이
      주는 편이 낫다.
    """
    try:
        return next_execution_day(as_of, calendar=calendar), ""
    except CalendarNotCovered as exc:
        why = f"공휴일 축: 다음 실행일을 찾다 달력 밖으로 나갔다 — {exc}. 주말 축만으로 골랐다"
    except ExecutionDayNotFound as exc:
        # 달력은 읽혔는데 상한까지 실행일이 없다 — 달력이 틀렸을 가능성이 크다.
        return None, f"다음 실행일: 못 찾았다 — {exc}"

    try:
        return next_execution_day(as_of), why
    except ExecutionDayNotFound as exc:  # pragma: no cover - 주말은 최대 이틀이다
        return None, f"다음 실행일: 못 찾았다 — {exc}"


def _execution_calendar_payload(as_of: date) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """매입에 실을 실행일 봉투와, **못 실었으면 그 사유.**

    🔴 **문 앞과 축이 다르다** (`#303` 후속 · 매입 리뷰).

      ```text
      문 앞     "그날 ML 예측이 있어 판단을 도는가"   holiday_nm + 주말   get_calendar()
      봉투      "그날 시장에서 살 수 있는가"          is_open            get_market_calendar()
      ```

      ★ 마스터가 토요일에 안 도는 이유는 *"장이 안 서서"* 가 아니라 **예측이 없어서**다.
        2026년 토요일 45일에 가락이 서고, 그 45일을 문 앞 축으로 밀면 **살 수 있는 날에
        못 산다고 계획한다.**

    ★ **묻는 범위도 다르다.** 문 앞은 하루를 묻고 봉투는 **지평 전체**를 묻는다 —
      오늘은 덮이는데 지평 끝이 안 덮이는 날이 있다.

    🔴 **못 덮으면 봉투를 통째로 안 싣는다.** 덮인 데까지만 실으면 지평이 거짓말을
       한다 — 받는 쪽은 `horizon_end` 까지 다 봤다고 읽는다. 반쪽 달력보다 **없는
       달력이 낫다**: 없으면 매입이 오늘까지의 동작(밀지 않음)으로 돌고, 그 사실이
       `skipped_checks` 에 남는다.

    ⚠️ **달력이 죽었다고 매입 판단을 멈추지 않는다.** 멈추면 시뮬레이션 전체가 선다.
      `execution_day.py` 가 정한 태도 그대로다 — *"부르는 쪽이 정한다."*
    """
    try:
        envelope = build_execution_calendar(as_of, market=get_market_calendar())
    except CalendarNotCovered as exc:
        return None, (
            (
                f"실행일 봉투: {as_of.isoformat()} 부터의 지평을 달력이 다 안 덮는다 — {exc}."
                " 매입에 비영업일 목록을 안 실었다 (매입은 회차일을 밀지 않는다)"
            ),
        )
    return envelope.as_payload(), ()


def _not_execution_day_reason(as_of: date, following: date | None) -> str:
    """안 도는 날의 사유. **왜 안 도는지와 언제 다시 도는지를 같이 적는다.**

    ★ **주말과 공휴일을 구분해 적는다.** 설날을 *"주말이라"* 로 적으면 사유가
      거짓말을 한다 — 사람이 달력을 다시 보게 된다.

    ★ 판정은 여기서 하지 않는다. `execution_day` 가 *"안 도는 날"* 이라고 했고,
      평일인데 안 도는 날은 공휴일뿐이다.
    """
    label = "주말" if as_of.weekday() >= _SATURDAY else "공휴일"
    head = (
        f"실행일이 아니다: {as_of.isoformat()}"
        f"({_WEEKDAY_NAMES[as_of.weekday()]})은 {label}이라 ML 예측이 없다. "
    )
    if following is None:
        # ⚠️ 못 찾은 것을 지어내지 않는다. 사유에 날짜가 없는 것이 **사실**이다.
        middle = "다음 실행일은 찾지 못했다. "
    else:
        middle = (
            f"다음 실행일은 {following.isoformat()}"
            f"({_WEEKDAY_NAMES[following.weekday()]})이다. "
        )
    tail = "경과일수는 달력일 그대로 센다 — 안 도는 날이 사라지는 것이 아니다."
    return head + middle + tail


def _decision_collision(request_id: str) -> list[str]:
    """🔴 **이미 결정이 붙은 업무 키로 다시 도는가.**

    `master_agent_runs` 는 append-only 라 같은 키로 두 번 돌면 **행이 둘**이
    되고, `get_run_by_request_id` 는 **최신 1건**을 돌려준다(그게 맞는 동작이다 —
    *"그 요청 어떻게 됐냐"* 에는 마지막 결과가 답이다).

    문제는 **결정과 결합할 때** 생긴다.

    ```text
    06:22  실행 A (안 3개)
    06:22  '기본' 승인          ← A 의 '기본' 을 골랐다
    06:23  실행 B (같은 키)      ← 이제 조회하면 B 가 나온다
           → 화면에는 "B 의 기본을 승인했다" 로 보인다
    ```

    두 실행의 같은 라벨이 다른 수량이면 **승인한 것과 다른 것이 승인된 것으로
    읽힌다.** 실측(2026-08-29 리허설)에서 재현했다.

    ★ **막지 않고 드러낸다.** 재실행 자체는 죄가 아니고, 승인 게이트를 마스터가
      들고 있으면 안 된다(8/26 회의). 사람이 보고 판단할 일이다.

    ★ **근본 해법이 들어왔다 (2026-08-30, `master_decisions.run_id`).** 결정이
      이제 실행 행을 가리키므로 *"그 결정이 이 실행을 가리키는 것처럼 보인다"* 는
      **더 이상 사실이 아니다.** 그래도 경고는 남긴다 — 이력 조회가 그 키의 **최신
      실행 하나**를 주기 때문에, 같은 키로 다시 돌리면 앞 결정이 보고 있던 계획이
      화면에서 사라진다. 바뀐 것은 **문구가 가리키는 위험**이다.

    ⚠️ 8/30 이전 결정은 `history_run_id` 가 `None` 이다 — 그때는 정말로 어느 실행인지
      모른다. 그 경우 문구가 옛말 그대로여야 맞다.
    """
    try:
        existing = get_decisions(request_id)
    except Exception:  # noqa: BLE001 — 경고를 못 만든다고 실행을 막지 않는다
        return []
    if not existing:
        return []
    current = next((row for row in existing if row.is_current), existing[-1])
    label = f" · {current.scenario_label}" if current.scenario_label else ""
    head = (
        f"DECISION-COLLISION: 이 업무 키에는 이미 결정이 있다 "
        f"({current.decision_seq}회차 {current.decision}{label}). "
    )
    if current.history_run_id:
        tail = (
            f"그 결정은 **다른 실행**({current.history_run_id[:8]}…)을 가리키므로 이 "
            "실행이 승인된 것은 아니다. 다만 이력 조회는 최신 실행 하나만 보여주므로 "
            "앞 결정이 보고 있던 계획이 화면에서 사라진다 — 조건을 바꿔 다시 만들려면 "
            "새 업무 키를 써라"
        )
    else:
        # 8/30 이전 결정 — 정말로 어느 실행인지 모른다
        tail = (
            "그 결정은 **어느 실행을 가리키는지 기록되지 않았다** (2026-08-30 이전). "
            "같은 키로 다시 돌면 그 결정이 이 실행을 가리키는 것처럼 보인다 — "
            "조건을 바꿔 다시 만들려면 새 업무 키를 써라"
        )
    return [head + tail]


def _inputs_for(request: ProcurementRunRequest) -> MasterInputs | None:
    """마스터가 실어 줄 셋을 모은다 (§3.2.5).

    ★ **요청이 직접 준 값이 이긴다.** 백테스트는 그날의 값을 그대로 넣어야 하므로
      적재층이 현재 DB 를 읽어 덮으면 안 된다.

    ★ 품목이 없으면 모으지 않는다 — 셋 다 품목 단위다. 매입이 `missing_data: ["item"]`
      으로 답하는 것이 정상 경로다.

    ★ **적재 실패가 Flow 를 막지 않는다.** 못 실으면 매입이 `missing_data` 로 답하고
      `E4` 가 된다 — 그것도 사실의 기록이다.
    """
    if not request.item:
        return None
    if request.forecast and request.confirmed_orders and request.policy_values:
        return None
    try:
        return collect_inputs(request.item, request.as_of)
    except Exception:  # noqa: BLE001
        return None


#: 요청 본문이 직접 실을 수 있는 입력 3종 (§3.2.5 의 백테스트 통로).
#: `run_procurement` 이 `request.<key> or _payload(inputs, key)` 로 쓰는 그 셋이고,
#: 이름이 갈리면 출처표가 값과 어긋나므로 **한 자리에서만 적는다.**
_INJECTABLE_INPUTS: tuple[str, ...] = ("forecast", "confirmed_orders", "policy_values")

#: 실행일 봉투가 출처표에서 쓰는 이름과 소스. `AgentRequest.payload` 의 키와 같은
#: 이름을 쓴다 — 받는 쪽이 표와 봉투를 **한 이름으로** 맞춰 읽는다.
_CALENDAR_KEY = "execution_calendar"
_CALENDAR_SOURCE = "market_calendar"

#: 실행 축이 출처표에서 쓰는 이름과 **기본값 소스**. `_CALENDAR_KEY` 와 같은 모양이다 —
#: 요청 칸 이름(`ProcurementRunRequest.sim_run_id`)을 그대로 쓴다.
_SIM_RUN_KEY = "sim_run_id"
_SIM_RUN_DEFAULT_SOURCE = "burn_in"


def _sim_run_id_of(request: ProcurementRunRequest | SalesRunRequest) -> str:
    """이번 실행의 축. **요청이 주면 그 값, 없으면 번인 상수.**

    🔴 **상수를 새로 만들지 않는다.** 값의 주인은 `ledger_repository.BURN_IN_SIM_RUN_ID`
       하나다 — 여기서 문자열을 다시 적으면 두 벌이 되고, 한쪽만 고치는 날 판단 행과
       원장이 갈린다.

    🔴 **떨어졌다는 사실은 출처표가 적는다.** 이 함수는 값만 고르고, *"요청이 줬나
       기본값인가"* 는 `_input_sources` 가 `REQUEST:` · `DEFAULT:` 로 나눠 적는다 —
       같은 사실을 두 자리에서 판정하지 않게 **판정 규칙은 `_sim_run_source` 하나**다.

    ★ **빈 문자열도 안 준 것으로 본다.** 공백만 든 축을 그대로 봉투에 실으면
      `ledger.sim_run_id_for` 가 뒤에서 터지고, 그 실패는 *"요청이 이상했다"* 가
      아니라 *"원장이 터졌다"* 로 읽힌다.
    """
    given = (request.sim_run_id or "").strip()
    return given or BURN_IN_SIM_RUN_ID


def _sim_run_source(request: ProcurementRunRequest | SalesRunRequest) -> str:
    """실행 축의 출처 한 줄. **값과 같은 판정을 쓴다** (`_sim_run_id_of`).

    ```text
    REQUEST:sim_run_id   요청이 줬다
    DEFAULT:burn_in      요청이 안 줘서 마스터 기본값으로 떨어졌다
    ```
    """
    given = (request.sim_run_id or "").strip()
    if given:
        return f"{REQUEST_GRADE}:{_SIM_RUN_KEY}"
    return f"{DEFAULT_GRADE}:{_SIM_RUN_DEFAULT_SOURCE}"


def _input_sources(
    request: ProcurementRunRequest,
    inputs: MasterInputs | None,
    *,
    execution_calendar: dict[str, Any] | None = None,
) -> dict[str, str]:
    """이번 실행이 **실제로 쓴 값**의 출처표.

    🔴 **주입은 mock 이 아니고 측정도 아니다** (매입 실측 2026-09-07).

      백테스트 통로는 정당하다 — *"요청이 직접 준 값이 이긴다"* 는 `_inputs_for` 가
      이미 적어 둔 계약이다. 문제는 **그 사실이 화면까지 안 갔다**는 것이다.

      .. code-block:: text

          전   셋 다 주입   input_sources {}          화면에 경고 0건
               forecast 만  MEASURED:v_ml_price_forecast
                            🔴 실제로 쓴 값은 주입분이다 — **틀린 사실을 적극적으로 말한다**

    ★ 빈 것은 *"모른다"* 이지만 틀린 출처는 *"안다고 잘못 말하는 것"* 이다. 뒤가 나쁘다.

    ★ **`mocked_inputs` 와 섞지 않는다.** 그것은 `grade == "MOCK"` 만 세고, 세면
      `ProcurementFlow` 가 실행을 세운다. 주입은 세울 일이 아니라 적을 일이다 —
      한 칸에 두 사실을 담으면 둘 다 못 읽는다.

    ```text
    REQUEST:<key>      요청 본문이 준 값 — 이번 실행이 실제로 쓴 것
    MEASURED:<source>  DB 에서 읽은 값
    DERIVED:<source>   DB 값에서 규칙으로 파생
    MISSING:- · MOCK:  (`inputs.py` 그대로)
    ```

    🔴 **실행일 봉투도 여기 적는다** (`#300` · 매입 보고 2026-09-10).

      봉투는 `AgentRequest.payload` 에만 실리고 `master_agent_runs` 는 그것을 안 담는다.
      그런데 *"주입한 키는 주입이라고 적는다"* 고 해 놓고 **봉투는 안 적었다.** 그래서
      받는 쪽은 확인할 표가 없어 **없는 표를 뒤졌고, 665건 전수 0건으로 읽었다** —
      실제로는 655건 중 `CalendarNotCovered` 0건, 즉 매번 실렸다.

      .. code-block:: text

          실었다      execution_calendar: "DERIVED:market_calendar"
          못 실었다   execution_calendar: "MISSING:-"

    ★ **어휘를 새로 만들지 않는다.** 봉투는 DB 시장달력에서 규칙으로 파생하므로
      위 다섯 중 `DERIVED` 다.

    ★ **못 실었을 때도 키를 적는다.** 키를 통째로 빼면 *"안 실렸다"* 가 *"모른다"* 와
      섞여 예전과 같아진다 — 없는 표를 다시 뒤지게 된다.

    🔴 **실행 축도 여기 적는다** (`#531` 후속 · 2026-09-10).

      요청이 `sim_run_id` 를 안 주면 마스터가 번인 상수로 떨어뜨린다. 그 자체는
      이번 판의 계약이지만, **떨어졌다는 사실이 아무 데도 안 적히면** *"말 안 하고
      번인에 쌓는"* 길이 그대로 남는다 — 나중에 읽는 사람이 그 판단 행이 **누가 고른
      실행에 앉았는지** 알 수 없다.

      .. code-block:: text

          요청이 줬다     sim_run_id: "REQUEST:sim_run_id"
          기본값이다      sim_run_id: "DEFAULT:burn_in"

    ★ **여기서도 키를 안 뺀다.** 봉투와 같은 이유다 — 「기본값이었다」와 「모른다」가
      섞이면 다시 코드를 뒤지게 된다.

    ★ **어휘 하나만 는다.** `REQUEST` 는 이미 있고 `DEFAULT` 를 `inputs.py` 에 뒀다 —
      값의 주인이 한 자리여야 화면과 부서 payload 가 한 표를 읽는다.
    """
    sources = dict(inputs.sources()) if inputs else {}
    for key in _INJECTABLE_INPUTS:
        if getattr(request, key, None):
            sources[key] = f"{REQUEST_GRADE}:{key}"
    sources[_CALENDAR_KEY] = f"DERIVED:{_CALENDAR_SOURCE}" if execution_calendar else "MISSING:-"
    sources[_SIM_RUN_KEY] = _sim_run_source(request)
    return sources


@dataclass(frozen=True)
class _CommitmentLookup:
    """어제까지의 약정 조회 결과. **실은 것과 못 실은 이유를 같이 든다** (#185 후속).

    🔴 전에는 `list[dict]` 하나였다. 그래서 **셋이 전부 빈 목록**이었다.

    ```text
    승인이 없었다        정상
    조회가 깨졌다        사고
    승인은 있는데 못 만들었다  사고
    ```

    받는 쪽에서 셋이 구별되지 않으면 *"어제 승인이 없었나 보다"* 로 읽힌다.
    §1.2-10 의 **0 과 모름은 다르다**가 그대로 걸리는 자리다.
    """

    carried: list[dict[str, Any]] = field(default_factory=list)
    concerns: tuple[str, ...] = ()


def _approved_commitments(request: ProcurementRunRequest) -> _CommitmentLookup:
    """어제까지 승인된 확정 입고 약정 (#185).

    🔴 **못 읽는 것을 없는 것으로 만들지 않는다.** 이력 DB 는 없어도 Flow 가 도는
      것이 계약이라(`history_enabled`) 예외를 올리지는 않는다. 대신 **못 읽었다는
      사실을 `concerns` 로 응답에 남긴다** — 조용히 비우면 어제 승인이 없었던 것과
      같아진다.

    ★ **재호출로 안 고쳐지므로 `findings` 가 아니다.** 매입을 몇 번 다시 불러도
      DB 가 안 읽히는 사실은 그대로다. 사람이 볼 것이지 재시도할 것이 아니다.

    ★ 품목이 없으면 묻지 않는다. 약정은 품목별이라 물을 대상이 없다.
    """
    if not request.item:
        return _CommitmentLookup()
    try:
        found = commitments_before(request.item, request.as_of)
    except Exception as exc:
        # ★ 이력 조회 실패가 매입 실행을 막지 않는다. 다만 조용히 넘어가지도 않는다.
        logger.exception("어제까지의 승인 약정 조회 실패 - 실행은 그대로 돈다")
        note = (
            f"어제까지 승인된 확정 입고 약정을 못 읽었다 (조회 실패: {type(exc).__name__})"
            " - 이번 실행은 어제 승인분을 모른 채 돌았다. '승인이 없었다' 와 다르다."
        )
        return _CommitmentLookup(concerns=(note,))
    return _CommitmentLookup(
        [c.model_dump(mode="json") for c in found if c.buildable],
        tuple(_commitment_gap(c) for c in found if _commitment_gap(c)),
    )


def _commitment_gap(commitment: CommitmentOut) -> str:
    """이 약정이 **온전히 실렸는가.** 아니면 그 사유. 온전하면 빈 문자열이다.

    🔴 **두 갈래를 다 본다.**

    ```text
    buildable=False              아예 안 실린다
    buildable + 빈 arrival_schedule  실리는데 도착 일정이 없다
    ```

      뒤가 더 위험하다 - 물류가 받기는 받고 **"입고 예정이 없다"** 로 읽는다.
      `CommitmentOut.notes` 가 그 사유를 이미 들고 있는데 아무도 안 봤다.
    """
    label = commitment.approval_id or commitment.scenario_label or "승인분"
    if not commitment.buildable:
        return (
            f"{label}: 어제 승인된 매입의 약정을 못 만들어 경계 호출에 안 실었다"
            f" - {commitment.reason}"
        )
    if not commitment.arrival_schedule:
        note = " / ".join(commitment.notes) or "사유 없음"
        return f"{label}: 약정은 섰는데 도착 일정이 비었다 - {note}"
    return ""


def _payload(inputs: MasterInputs | None, key: str):
    if inputs is None:
        return None
    sourced = getattr(inputs, key)
    return sourced.payload if sourced.usable else None


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# ---------------------------------------------------------------------------
# 변환
# ---------------------------------------------------------------------------


def _to_response(
    context: ExecutionContext,
    outcome: ProcurementOutcome,
    inputs: MasterInputs | None = None,
    sources: Mapping[str, str] | None = None,
) -> ProcurementRunResponse:
    """★ **출처표는 부서 payload 와 같은 표다** (`_input_sources`).

    같은 실행에서 `run_procurement` 이 한 번 만든 것을 그대로 받는다 — 여기서 다시
    `inputs.sources()` 를 부르면 주입분이 빠져 **화면과 payload 가 갈린다.**

    ⚠️ `mocked_inputs` 는 여기서도 `inputs.mocked` 그대로다. 주입은 mock 이 아니라
      섞지 않는다 (`inputs.injected_keys`).
    """
    if sources is None:
        sources = inputs.sources() if inputs else {}
    return ProcurementRunResponse(
        input_sources=dict(sources),
        mocked_inputs=list(inputs.mocked) if inputs else [],
        request_id=context.request_id,
        as_of=context.as_of,
        end_code=outcome.end_code,
        reason=outcome.reason,
        scenarios=[dict(s) for s in outcome.scenarios],
        judgment=dict(outcome.judgment),
        constraints={k: dict(v) for k, v in outcome.constraints.items()},
        evidences=_evidences_out(outcome),
        adjustments=_adjustments_out(outcome),
        verdicts={k: dict(v) for k, v in outcome.verdicts.items()},
        blocked_by=list(outcome.blocked_by),
        blocked_failures=_blocked_out(outcome),
        findings=list(outcome.findings),
        concerns=list(outcome.concerns),
        skipped_checks=list(outcome.skipped_checks),
        verification_skipped=outcome.verification_skipped,
        purchase_attempts=outcome.purchase_attempts,
        presentable=outcome.presentable,
        single_option=outcome.single_option,
        plan=_steps(outcome.plan),
        plan_signature=list(outcome.plan.signature),
    )


def _empty_response(
    context: ExecutionContext,
    reason: str,
    missing_adapters: list[str] | None = None,
    skipped_note: str = "전 검사: 어댑터 미등록으로 Flow 가 시작되지 않음",
) -> ProcurementRunResponse:
    """안 돈 날의 응답. **왜 안 돌았는지가 `reason` 과 `skipped_note` 로 남는다.**

    ⚠️ `skipped_note` 는 기본값이 어댑터 문구다. 다른 이유로 접을 때 그대로 쓰면
      **없던 어댑터 문제를 지어내는 것**이 되므로 부르는 쪽이 자기 사유를 준다.
    """
    adapters = list(missing_adapters or [])
    return ProcurementRunResponse(
        request_id=context.request_id,
        as_of=context.as_of,
        end_code="E4_NOT_STARTED",
        reason=reason,
        blocked_by=adapters,
        missing_adapters=adapters,
        verification_skipped=True,
        # 못 돈 날도 **무엇을 못 봤는지**는 남긴다 (§3.7.6)
        skipped_checks=[skipped_note],
    )


def _to_sales_response(context: ExecutionContext, outcome: SalesOutcome) -> SalesRunResponse:
    """판매 Flow 결과를 응답 모양으로. **매입 `_to_response` 와 따로 둔다.**

    🔴 공통 조립 함수로 묶지 않는 자리다 (설계 §1). 두 사이클은 응답 모델도 종료
      코드도 다르고, 공유하는 것은 **판정(`check_day_gate`)과 순서**이지 응답이 아니다.
    """
    return SalesRunResponse(
        request_id=context.request_id,
        as_of=context.as_of,
        end_code=outcome.end_code,
        reason=outcome.reason,
        candidates=_sales_candidates_out(outcome),
        judgment=dict(outcome.judgment),
        supply_context=dict(outcome.supply_context),
        context_failure=_sales_context_failure_out(outcome),
        # ★ **못 실은 사실을 응답까지 나른다** (M-1). 매입 `mocked_inputs` ·
        #   `input_sources` 와 같은 자리다 — 값이 어디서 왔는지(또는 안 왔는지)를
        #   결론과 떼어 놓지 않는다.
        ml_context_note=outcome.ml_context_note,
        # ★ 근거·조정안은 **고르지도 정렬하지도 않는다** — 매입과 같은 함수를 쓴다.
        #   부서가 낸 차례가 그 부서의 설명 순서다 (§3.2.2).
        evidences=_evidences_out(outcome),
        adjustments=_adjustments_out(outcome),
        feedback_attempts=outcome.feedback_attempts,
        plan=_steps(outcome.plan),
        plan_signature=list(outcome.plan.signature),
    )


def _sales_candidates_out(outcome: SalesOutcome) -> list[SalesCandidateOut]:
    """후보와 그 판정을 옮긴다. **통과 판정을 여기서 다시 세지 않는다.**

    ★ `passed` · `unvalidated` · `detail` 은 `CandidateVerdict` 의 property 를 그대로
      읽는다. 허용목록(`PASSING_VERDICTS`)이 늘어도 답이 한 곳에서만 바뀐다 — 여기서
      *"reject 가 아니면 통과"* 로 다시 세면 어휘가 는 날 새 값이 통과 쪽으로 샌다.
    """
    return [
        SalesCandidateOut(
            scenario=dict(c.scenario),
            validations={k: dict(v) for k, v in c.validations.items()},
            unroutable=list(c.unroutable),
            missing_terms=list(c.missing_terms),
            passed=c.passed,
            unvalidated=c.unvalidated,
            detail=c.detail,
        )
        for c in outcome.candidates
    ]


def _sales_context_failure_out(outcome: SalesOutcome) -> BlockedAgentOut | None:
    """물류가 초기 컨텍스트를 못 낸 사실. **없으면 `None` 이다.**

    ★ `detail` 을 여기서 다시 만들지 않고 `AgentFailure.detail` 을 부른다 — 매입
      `_blocked_out` 과 같은 자리다.
    """
    failure = outcome.context_failure
    if failure is None:
        return None
    return BlockedAgentOut(
        agent=failure.agent,
        runtime_status=failure.runtime_status,
        reasoning=failure.reasoning,
        missing_data=list(failure.missing_data),
        detail=failure.detail,
    )


def _empty_sales_response(
    context: ExecutionContext, reason: str, skipped_note: str = ""
) -> SalesRunResponse:
    """시작조차 못 한 날의 판매 응답.

    ★ **`SL4_NOT_STARTED` 다.** 매입의 `E4` 와 뜻은 같지만 **어휘는 갈려 있다** —
      한 어휘에 두 사이클을 담으면 화면과 이력이 어느 사이클의 종료인지를 payload 로
      되짚어야 한다 (D-3 합의).

    🔴 **검증이 안 돈 날이다.** `verification_skipped` 를 세우고 무엇을 못 봤는지도
      남긴다 — 매입 `_empty_response` 와 같은 자리다. 비워 두면 빈 `findings` 가
      *"Critic 을 지나 통과했다"* 로 읽힌다 (§3.7.6).
    """
    return SalesRunResponse(
        request_id=context.request_id,
        as_of=context.as_of,
        end_code="SL4_NOT_STARTED",
        reason=reason,
        verification_skipped=True,
        skipped_checks=[skipped_note] if skipped_note else [],
    )


def _evidence_contract_concerns(outcome: ProcurementOutcome) -> list[str]:
    """🔴 **근거의 값이 계약과 다른가.**

    `contracts_core.Evidence.value` 는 `float` 인데 `Evidence` 가 dataclass 라
    런타임 검증이 없다. 그래서 문자열을 넣어도 아무 데서도 안 걸리고, 실제로
    재무 `policy_version_used` 가 `"v1.3-PROVISIONAL"` 을 싣고 있다
    (`finance/capabilities/procurement.py:175`).

    ★ **값을 고치거나 버리지 않는다.** 고치면 남의 값을 덮어쓰는 것이고(§3.2.2),
      버리면 근거를 고르는 것이다. 원본은 그대로 나가고 여기서 **사실만 적는다.**

    ★ `findings` 가 아니라 `concerns` 다. **매입을 다시 불러도 안 고쳐진다** -
      남의 계약 문제라 사람이 봐야 한다 (§3.4). 재무 회신의 봉투 위반 때문에
      매입을 재호출하던 것과 같은 실수를 반복하지 않는다.

    ⚠️ 이 검사는 **증상만 잡는다.** 어느 쪽이 맞는지는 정하지 않는다 - 계약을
      넓힐지(문자열 근거를 허용) 재무가 다른 칸을 쓸지는 팀이 정할 일이다.
    """
    offenders = [
        f"{item.agent}:{item.evidence.claim}={item.evidence.value!r}"
        for item in outcome.evidences
        if not isinstance(item.evidence.value, (int, float))
        or isinstance(item.evidence.value, bool)
    ]
    if not offenders:
        return []
    message = (
        "EVIDENCE-VALUE-NOT-NUMERIC: 근거의 value 가 계약(float)과 다른 것이 "
        f"{len(offenders)}건이다 - {', '.join(offenders)}. 값은 그대로 나갔고 "
        "마스터가 고치지 않았다. 계약을 넓힐지 부서가 다른 칸을 쓸지는 팀이 정한다."
    )
    return [message]


def _evidences_out(outcome: ProcurementOutcome | SalesOutcome) -> list[EvidenceOut]:
    """부서 근거를 응답 모양으로. **고르지도 요약하지도 않는다.**

    ★ **두 사이클이 같이 쓴다.** 근거를 옮기는 규칙은 사이클에 매인 것이 아니라
      봉투 수준의 것이다 (`SourcedEvidence` 를 봉투로 올린 것과 같은 이유). 베끼면
      한쪽만 고쳐지는 날이 온다 — 응답 **모델**을 안 묶는 것과 다른 이야기다.

    ★ 순서를 손대지 않는다 - 부서가 낸 순서가 그 부서의 설명 순서다.
      마스터가 정렬하면 "이게 더 중요하다" 는 뜻이 생긴다 (§3.2.2).

    ★ 빈 목록은 "근거가 완비됐다" 가 아니라 **부서가 근거를 안 냈다**는 사실이다.
      화면이 그렇게 읽도록 스키마 설명에 적어 두었다.
    """
    return [
        EvidenceOut(
            agent=item.agent,
            mode=item.mode,
            claim=item.evidence.claim,
            source=item.evidence.source,
            value=item.evidence.value,
            unit=item.evidence.unit,
            evidence_grade=item.evidence.evidence_grade,
            evidence_detail=item.evidence.evidence_detail,
            ref_ids=list(item.evidence.ref_ids),
        )
        for item in outcome.evidences
    ]


def _adjustments_out(outcome: ProcurementOutcome | SalesOutcome) -> list[AdjustmentOut]:
    """조정안을 표준형 그대로 옮긴다. **고르지도 정렬하지도 않는다.**

    ★ 근거(`_evidences_out`)와 같이 **두 사이클이 같이 쓴다** — 표준형은 봉투 것이다.

    ★ 순서는 부서가 보낸 차례다. 정렬하면 그것이 우선순위로 읽힌다 -
      근거(`_evidences_out`)와 같은 이유다 (§3.2.2).
    """
    return [
        AdjustmentOut(
            dept=a.dept,
            axis=a.axis,
            target_value=a.target_value,
            unit=a.unit,
            reason=a.reason,
            ref_ids=list(a.ref_ids),
            # 부서가 안 채우면 빈 목록·None 그대로 나간다 - 없는 것을 만들지 않는다
            scenario_labels=list(a.scenario_labels),
            split_date=a.split_date,
        )
        for a in outcome.adjustments
    ]


def _blocked_out(outcome: ProcurementOutcome) -> list[BlockedAgentOut]:
    """막은 부서를 사유째로 옮긴다. **순서를 바꾸지 않는다.**

    ★ `detail` 을 여기서 다시 만들지 않고 `AgentFailure.detail` 을 부른다 -
      `reason` 문장에 들어간 것과 **같은 함수**여야 둘이 갈리지 않는다.
    """
    return [
        BlockedAgentOut(
            agent=f.agent,
            runtime_status=f.runtime_status,
            reasoning=f.reasoning,
            missing_data=list(f.missing_data),
            detail=f.detail,
        )
        for f in outcome.blocked_failures
    ]


def _steps(plan: ExecutionPlan) -> list[StepOut]:
    return [
        StepOut(
            seq=s.seq,
            agent=s.agent,
            mode=s.mode,
            call_seq=s.call_seq,
            run_id=s.run_id,
            runtime_status=s.runtime_status,
            business_status=s.business_status,
            used_tools=list(s.used_tools),
            finding_codes=list(s.finding_codes),
            missing_data=list(s.missing_data),
            reasoning=s.reasoning,
            llm_status=s.llm_status,
            llm_model=s.llm_model,
            llm_attempts=s.llm_attempts,
            llm_fallback_used=s.llm_fallback_used,
            replans=s.replans,
            # 마스터는 읽지 않고 나른다 - 순서도 부서가 낸 그대로다
            observations=list(s.observations),
        )
        for s in plan.steps
    ]


# ---------------------------------------------------------------------------
# 조회 — GET /master/runs/{request_id}
# ---------------------------------------------------------------------------


def get_run_report(request_id: str) -> ReportOut:
    """저장된 실행 하나를 보고서로.

    ★ **최신 실행을 쓴다** — `get_run_history` 와 같은 규칙이다. *"그 요청 어떻게
      됐냐"* 에는 마지막 결과가 답이다.

    ★ 매입안 보고서다. 조회(STATUS)는 안이 없어 보고서가 성립하지 않으므로
      사이클을 밝힌다 (2026-09-02).
    """
    row = get_run_by_request_id(request_id, cycle="PROCUREMENT")
    run = dict(row.get("response_payload") or {})
    if not run:
        raise LookupError(f"실행 원문이 없어 보고서를 만들 수 없습니다: {request_id}")
    run.setdefault("request_id", request_id)
    return ReportOut(
        request_id=request_id,
        filename=report_filename(run),
        markdown=render_report(run),
    )


def get_burn_in_history() -> BurnInOut:
    """번인 구간(에이전트 판단 전 30일)을 화면이 읽는 형태로.

    ★ **값을 만들지 않는다.** DB 에 심긴 것을 모양만 바꾼다 — 합계·증감률을 여기서
      계산하기 시작하면 재무가 내는 숫자와 갈릴 자리가 생긴다. 화면이 필요하면
      **가진 값으로** 그린다.
    """
    raw = get_burn_in()
    run = raw["run"]
    return BurnInOut(
        sim_run_id=run["sim_run_id"],
        run_type=run["run_type"],
        period_start=run["period_start"],
        period_end=run["period_end"],
        as_of=run["as_of"],
        status=run["status"],
        financing_mode=run.get("financing_mode"),
        note=run.get("note"),
        closings=[DailyClosingOut(**row) for row in raw["closings"]],
    )


def get_run_history(request_id: str) -> RunHistoryOut:
    """업무 키로 실행 이력을 찾는다.

    ★ 재실행하면 같은 `request_id` 로 행이 여럿 생긴다. **최신을 돌려준다** —
      "그 요청 어떻게 됐냐"에는 마지막 결과가 답이다. 전체 이력이 필요하면
      `run_id` 로 목록을 훑는다.

    ★ 결정은 **전부** 싣는다 (실행과 달리 최신 하나로 접지 않는다).
      번복이 있었다는 사실 자체가 답의 일부다 — `is_current` 로 최신만 표시한다.
    """
    # ★ 조회(STATUS)가 같은 업무 키로 적재되므로 사이클을 밝힌다 (2026-09-02).
    #   이 화면은 매입 실행 이력이다 - 안 밝히면 최신 조회가 매입 자리에 뜬다.
    row = get_run_by_request_id(request_id, cycle="PROCUREMENT")
    plan = list(row.get("plan") or [])
    return RunHistoryOut(
        request_id=row.get("request_id") or request_id,
        run_id=(None if row.get("run_id") is None else str(row["run_id"])),
        as_of=row["as_of"],
        # ★ 표에 `agent` 컬럼이 없다 (2026-09-02, master_agent_runs 로 이전).
        #   마스터 전용 표라 늘 같은 값이었고, 상수를 컬럼으로 두면 "언젠가 다른 값이
        #   들어올 수 있다" 로 읽힌다. 응답 모양은 유지한다 - 화면이 쓰고 있다.
        agent="master",
        cycle=row["cycle"],
        runtime_status=row["runtime_status"],
        elapsed_ms=row.get("elapsed_ms"),
        created_at=row["created_at"],
        plan=plan,
        plan_signature=[
            (str(s.get("agent")), str(s.get("mode")), int(s.get("call_seq", 1))) for s in plan
        ],
        request_payload=dict(row.get("request_payload") or {}),
        response_payload=dict(row.get("response_payload") or {}),
        decisions=get_decisions(request_id),
    )
