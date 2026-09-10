"""결정 접수 — 실행 이력을 읽고, 규칙을 걸고, 적재한다.

★ `service.py` 와 나눈 이유 — 저쪽은 **Flow 실행**의 경계 변환이고 여기는 **사람의
  결정**이다. 한 파일에 두면 `run_procurement` 에서 결정 함수를 부르기가 쉬워지는데,
  그 순간 승인 게이트가 툴 안으로 들어온다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any
from uuid import UUID

from app.master.commitment import (
    ApprovedCommitment,
    CommitmentNotBuildable,
    build_commitment,
)
from app.master.decision import (
    SALES_CYCLE,
    CommitmentOut,
    DecisionIn,
    DecisionOut,
    DecisionRejected,
    available_scenario_names,
    check_decidable,
    check_scenario_exists,
    next_seq,
)
from app.master.decision_repository import list_decisions, save_decision
from app.master.revalidation import (
    Revalidation,
    conditions_of_original,
    find_scenario,
    revalidate_scenario,
)
from app.master.run_repository import get_run, get_run_by_request_id, list_runs
from app.master.sales_approval import SaleConfirmationOut, confirm_approved_sale
from app.master.transition import TransitionOut, apply_approval


def _end_code_of(response_payload: dict[str, Any]) -> str:
    """실행 응답에서 종료 코드를 읽는다.

    ★ 행의 `runtime_status` 를 쓰지 않는다 — 그건 `E4` 만 구분하는 3값 어휘라
      `E2`(보류)·`E3`(반려)·`E5`(계획 없음)가 전부 `READY` 로 접혀 있다.
      결정 규칙은 다섯을 구분해야 한다.
    """
    end_code = response_payload.get("end_code")
    if not isinstance(end_code, str) or not end_code:
        raise DecisionRejected(
            "실행 이력에 종료 코드가 없다 — 결정을 걸 기준이 없다.", conflict=True
        )
    return end_code


def record_decision(request_id: str, payload: DecisionIn) -> DecisionOut:
    """결정 1건을 받아 적재하고 돌려준다.

    순서가 중요하다 — **읽고 → 검사하고 → 🔴 재검증하고 → 적재한다.** 적재 후
    검사하면 잘못된 결정이 이력에 남고, **재검증 뒤에 적재해야 그 결과가 같은 행에
    담긴다** (설계 2026-09-07 §1).

    🔴 **재검증이 막혀도 결정 행은 쓴다** (설계 §0). `decision=APPROVE` 는 **사용자가
      누른 사실**이고, `revalidation_outcome` 이 그 옆에 결과를 적는다.

      ```text
      결정 행       항상 쓴다        무엇을 눌렀나
      승인의 효력   PASSED 일 때만    도메인 Write 로 흘러가는 것 (M-5)
      ```

      막혔다고 행을 안 쓰면 *"승인하려다 막혔다"* 가 사라지고, 그것은 M-3 이 칸을
      나눈 이유를 되돌리는 것이다.

    ⚠️ **`PASSED` 여도 아직 아무 일도 안 일어난다.** 승인 후 도메인 Write 는 M-5 이고
      그 앞에 실행 원장(saga) 문제가 있다 (설계 §5).

    🔴 **`as_of` 는 여기서 안 만든다** (2026-09-09 · `#452`). 재검증이 설 날이고,
      **진입점이 정해서 넘긴다** — 운영은 `router.master_decide` 가 `clock` 을 읽고,
      발화문 경로는 `ask_service` 가 그 요청의 `as_of` 를 넘긴다. 이 함수가 시계를
      읽으면 걷기가 승인 경로를 타는 날 곡선에 벽시계가 섞인다.

      ⚠️ **기본값을 두지 않는다.** 안 넘기면 터져야 한다 — 기본값은 곧 업무 규칙이다.

    ⚠️ **승인이 아니어도 받는다.** `REJECT_ALL` · `REQUEST_CHANGE` · `CANCEL` 은
      재검증을 안 돌리므로 이 값을 안 쓰지만, 넘기고 안 넘기고가 결정 종류에 따라
      갈리면 **부르는 쪽이 종류를 먼저 알아야 한다.** 받는 것은 항상 같다.

    :param as_of: 이 결정이 서는 날. 승인이면 재검증이 그날로 돈다.
    :raises LookupError: 그 업무 키의 실행이 없다 (라우터가 404).
    :raises DecisionRejected: 지금 상태에서 받을 수 없다 (라우터가 409/422).
    """
    row = _run_for(request_id, payload.history_run_id)  # 없으면 LookupError
    response_payload = dict(row.get("response_payload") or {})

    # 🔴 **어느 어휘로 검사할지는 실행 행이 정한다** (2026-09-08 계약). `payload` 에서
    #    읽지 않는다 — 읽으면 매입 실행을 판매라고 우겨 `SL1_PRESENTED` 어휘로
    #    검사받을 수 있고, 그 순간 승인 게이트가 부르는 쪽 손에 들어간다.
    cycle = _cycle_of(row)

    end_code = _end_code_of(response_payload)
    check_decidable(end_code, payload.decision, cycle=cycle)
    check_scenario_exists(
        payload.scenario_label, available_scenario_names(response_payload, cycle)
    )

    existing = list_decisions(request_id)
    _reject_repeat_approval(existing, payload)

    seq = next_seq(existing)
    revalidation = _revalidation_for(row, response_payload, payload, seq)
    saved = save_decision(
        request_id=request_id,
        decision_seq=seq,
        decision=payload.decision,
        decided_by=payload.decided_by,
        end_code_at_decision=end_code,
        scenario_label=payload.scenario_label,
        condition_text=payload.condition_text,
        history_run_id=str(row["run_id"]),
        revalidation_request_id=None if revalidation is None else revalidation.request_id,
        revalidation_outcome=None if revalidation is None else revalidation.outcome,
        note=payload.note,
    )
    if cycle == SALES_CYCLE:
        # 🔴 **매입 약정을 만들지 않는다.** `_commitment_parts` 는
        #    `response_payload["scenarios"]` 만 보므로 판매 응답에서는 늘
        #    `buildable=False` 를 낸다 — *"판매를 확정했다"* 자리에 *"매입 약정을 못
        #    만들었다"* 가 실린다.
        return saved.model_copy(
            update={"sale": _sale_for(request_id, row, response_payload, payload, revalidation)}
        )
    out, commitment = _commitment_parts(request_id, seq, payload, response_payload)
    return saved.model_copy(
        update={
            "commitment": out,
            # 🔴 **축은 실행 행에서 온다.** 여기서 상수를 읽지 않는다 —
            #    `_sim_run_id_of` 가 왜인지를 적었다.
            "transition": _transition_for(commitment, sim_run_id=_sim_run_id_of(row)),
        }
    )


def _cycle_of(row: Mapping[str, Any]) -> str:
    """그 실행이 무슨 사이클이었나. **실행 이력 행이 정본이다.**

    ★ **응답 payload 의 모양으로 짐작하지 않는다.** `candidates` 가 있으면 판매라고
      읽으면, 매입 응답에 그 키가 섞이는 날 어휘가 조용히 갈린다. `cycle` 은 표의
      컬럼이고 CHECK 이 어휘를 잠근다 (`master_agent_runs.sql`).
    """
    cycle = row.get("cycle")
    return cycle if isinstance(cycle, str) else ""


def _sale_for(
    request_id: str,
    row: Mapping[str, Any],
    response_payload: Mapping[str, Any],
    payload: DecisionIn,
    revalidation: Revalidation | None,
) -> SaleConfirmationOut | None:
    """승인이면 **판매를 확정한다** (`confirm_sale`).

    ★ **승인이 아니면 `None`** 이다 — 거절·조건부 재요청·취소에는 확정할 것이 없고,
      그때의 `None` 은 *"확정에 실패했다"* 가 아니다 (`_transition_for` 와 같은 태도).

    🔴 **재검증 결과를 여기서 다시 재지 않는다.** `_revalidation_for` 가 낸 것을
      그대로 넘긴다 — 두 곳에서 판정하면 *"재검증은 막혔는데 확정은 됐다"* 가 가능해진다.

    ★ **`as_of` 는 그 실행의 날이다** (`_as_of_of`). 벽시계를 읽지 않는다 —
      `order_date` 가 되고, 그것이 곧 수금 곡선의 시점이다.
    """
    if payload.decision != "APPROVE" or payload.scenario_label is None:
        return None
    scenario = find_scenario(response_payload, payload.scenario_label)
    if scenario is None:
        return SaleConfirmationOut(
            status="BLOCKED",
            reason=f"승인한 안 '{payload.scenario_label}' 을 원 실행에서 유일하게 찾지 못했다.",
        )
    return confirm_approved_sale(
        request_id=request_id,
        run_id=str(row["run_id"]),
        as_of=_as_of_of(response_payload),
        policy_version=_policy_version_of(row),
        scenario=scenario,
        revalidation_outcome=None if revalidation is None else revalidation.outcome,
    )


def _revalidation_for(
    row: Mapping[str, Any],
    response_payload: Mapping[str, Any],
    payload: DecisionIn,
    decision_seq: int,
) -> Revalidation | None:
    """승인이면 **선택된 1안을 `as_of` 로 다시 검증한다** (설계 2026-09-07 · M-4).

    ★ **`as_of` 를 만들지 않고 흘린다.** 받은 날을 그대로 `revalidate_scenario` 에
      넘긴다 — 중간에서 손대면 진입점이 정한 날과 부서가 받은 날이 갈린다.

    🔴 **`APPROVE` 일 때만이다.** `REJECT_ALL` · `REQUEST_CHANGE` · `CANCEL` 은 승인이
      아니라 재검증할 대상이 없다 — 그때 두 칸은 `None` 이고, 그 `None` 은 *"재검증에
      실패했다"* 가 아니라 **"재검증을 하지 않았다"** 이다.

    ★ **`_commitment_parts` 앞에서 돈다.** 약정은 결정을 적은 **뒤에** 만들지만
      재검증은 **적기 전에** 돌아야 한다 — 결과가 같은 행에 담기기 때문이다.

    :returns: 승인이 아니면 `None`. 승인인데 못 돌렸으면 `ERROR` 가 실린 `Revalidation`
              이다 — 둘을 섞지 않는다 (§1.2-10).
    """
    if payload.decision != "APPROVE" or payload.scenario_label is None:
        return None

    scenario = find_scenario(response_payload, payload.scenario_label)
    if scenario is None:
        # ★ `check_scenario_exists` 를 이미 지났는데도 못 찾는 경우가 있다 — 라벨이
        #   겹치면 유일하지 않아 `None` 이 온다. 어느 안을 재검증했는지가 운에 걸리는
        #   것보다 **못 돌렸다고 적는 편**이 낫다 (`_commitment_parts` 와 같은 판단).
        return Revalidation(
            outcome="ERROR",
            reason=f"승인한 안 '{payload.scenario_label}' 을 원 실행에서 유일하게 찾지 못했다.",
        )

    # 🔴 **재검증이 설 날은 그 실행의 날이다** (2026-09-09 · 마스터 판단).
    #    부르는 쪽에서 받지 않는다 — 받으면 화면이든 걷기든 아무 날이나 넣을 수 있고,
    #    그 순간 재검증이 자기가 언제 도는지를 남에게 맡기게 된다. 실행 행이 정한다.
    #
    #    ⚠️ 벽시계를 읽던 것을 진입점으로 올렸다가(#452) 여기까지 내렸다. 이유는
    #      실측이다 — 2026-01-20 안을 오늘 승인하면 재검증이 **오늘** 로 개장을 묻고,
    #      오늘은 안 열린 날이라 `ERROR` 가 나 승인이 막혔다.
    as_of = _as_of_of(response_payload)
    if as_of is None:
        return Revalidation(
            outcome="ERROR",
            reason="원 실행의 기준일을 못 읽어 재검증할 날을 정할 수 없다.",
        )

    policy_version = _policy_version_of(row)
    if policy_version is None:
        # 🔴 **정책판 없이 봉투를 만들 수 없다** (`ExecutionContext` 가 막는다). 아무
        #   값이나 채워 넣으면 재현 4종의 하나가 거짓이 된다 (§3.2.4).
        return Revalidation(
            outcome="ERROR",
            reason="원 실행의 policy_version 을 못 읽어 재검증 봉투를 만들 수 없다.",
        )

    return revalidate_scenario(
        scenario=scenario,
        original_conditions=conditions_of_original(response_payload, payload.scenario_label),
        decision_seq=decision_seq,
        policy_version=policy_version,
        as_of=as_of,
        item=row.get("item") if isinstance(row.get("item"), str) else None,
    )


def _policy_version_of(row: Mapping[str, Any]) -> str | None:
    """원 실행이 돈 정책판. **실행 이력 행의 요청 원문에서 읽는다.**

    ★ **재검증이 새 정책판을 고르지 않는다.** `as_of` 는 고르는 날로 옮기지만 정책판까지
      바뀌면 *"그 사이 무엇이 바뀌었나"* 에 축이 둘 섞인다 — 재검증이 재는 것은
      **시장과 장부**이지 회사가 규칙을 바꿨는지가 아니다.
    """
    request_payload = row.get("request_payload")
    if not isinstance(request_payload, Mapping):
        return None
    value = request_payload.get("policy_version")
    return value if isinstance(value, str) and value.strip() else None


def _sim_run_id_of(row: Mapping[str, Any]) -> str | None:
    """이 결정이 걸린 실행의 축. **실행 이력 행이 정본이다.**

    ★★ **`ledger.sim_run_id_for` 가 요구한 계약이 이것이다.** 그 함수는 *"마스터
      실행 이력이 `sim_run_id` 를 싣도록 계약을 세우고 이 함수가 그것을 읽어야
      한다"* 고 적어 두었고, `master_agent_runs.sim_run_id` 가 그 칸이다
      (`Refs #150` · 2026-09-08 · `run_repository._COLUMNS`).

    🔴 **없으면 메우지 않는다.** `BURN_IN_SIM_RUN_ID` 로 채우면 축이 안 실린 옛
       실행의 승인이 조용히 번인 장부에 앉고, 재무는 자기 축을 읽으므로 **채무와
       매입 원장이 서로 다른 실행에 앉는다.** `None` 을 그대로 흘리면 원장 계산이
       터지고 그 사실이 `TransitionOut.reason` 에 남는다.
    """
    value = row.get("sim_run_id")
    return value if isinstance(value, str) and value.strip() else None


def _transition_for(
    commitment: ApprovedCommitment | None, *, sim_run_id: str | None
) -> TransitionOut | None:
    """약정이 섰으면 그것을 재무·물류 장부에 반영한다 (C 형태 ⑦).

    ★ **약정이 없으면 부르지 않는다.** 승인이 아니거나 약정을 못 만든 날에는 반영할
      사실 자체가 없다 — 그때의 `None` 은 *"전이가 실패했다"* 가 아니다.

    ★ **여기서도 결정을 죽이지 않는다.** `apply_approval` 은 예외를 밖으로 내지
      않고 `FAILED` 를 값으로 돌려준다. 적재된 결정이 전이 실패로 지워지면,
      사람이 승인한 사실이 사라진다.

    :param sim_run_id: 실행 이력 행이 실은 축. 🔴 **여기서 짓지 않는다.**
    """
    if commitment is None:
        return None
    return apply_approval(commitment, sim_run_id=sim_run_id)


def _commitment_for(
    request_id: str,
    decision_seq: int,
    payload: DecisionIn,
    response_payload: Mapping[str, Any],
) -> CommitmentOut | None:
    """승인이 만든 약정의 **응답 모양**만 낸다.

    ★ `_commitment_parts` 의 앞쪽만 돌려주는 얇은 겉면이다. 재조립 경로
      (`current_commitment`)는 약정 객체가 필요 없고, 응답 모양만 쓴다.
    """
    return _commitment_parts(request_id, decision_seq, payload, response_payload)[0]


def _commitment_parts(
    request_id: str,
    decision_seq: int,
    payload: DecisionIn,
    response_payload: Mapping[str, Any],
) -> tuple[CommitmentOut | None, ApprovedCommitment | None]:
    """승인이면 **확정 입고 약정**을 같이 낸다 (H1).

    ★ 응답 모양(`CommitmentOut`)과 **약정 객체**를 함께 돌려준다. 상태전이는 객체가
      있어야 걸리는데, 응답 모양에서 되만드는 것은 **같은 사실을 두 번 만드는 것**이라
      둘이 갈리는 날이 온다. 만든 자리에서 그대로 넘긴다.

    ★ **적재 뒤에 만든다.** 약정을 못 만들어도 결정은 남아야 한다 — 사람이 승인한
      것은 사실이고, 그 사실을 약정 조립 실패가 지우면 안 된다.

    ★ **못 만들면 이유를 싣는다.** `None` 을 조용히 돌려주면 물류가 *"입고 예정이
      없다"* 로 읽는다. 없는 것과 못 만든 것은 다르다 (§1.2-10).

    ★ **오케 `cycle.py` 를 부르지 않는다** (지시 2026-09-01). 같은 변환이 거기에도
      있지만 그 경로는 M-1 에서 안 돌고, 회차 수량을 합쳐 **품목을 없앤다.**
    """
    if payload.decision != "APPROVE":
        return None, None

    matches = _scenarios_of(response_payload, payload.scenario_label)
    if not matches:
        return CommitmentOut(buildable=False, reason="승인한 안을 실행 응답에서 찾지 못했다."), None
    if len(matches) > 1:
        # 🔴 첫 것을 조용히 고르면 **어느 안을 약정했는지가 운에 걸린다** (자기 리뷰).
        return CommitmentOut(
            buildable=False,
            reason=f"라벨 '{payload.scenario_label}' 이 {len(matches)}개다 — 유일하지 않다.",
        ), None
    scenario = matches[0]

    as_of = _as_of_of(response_payload)
    if as_of is None:
        return CommitmentOut(
            buildable=False, reason="실행 이력에 기준일이 없어 도착일을 걸 수 없다."
        ), None

    try:
        commitment = build_commitment(
            request_id=request_id,
            as_of=as_of,
            item=_item_of(response_payload),
            scenario=scenario,
            inbound_lead_days=_lead_days_of(response_payload),
            decision_seq=decision_seq,
            purchase_payment_days=_payment_days_of(response_payload),
        )
    except CommitmentNotBuildable as exc:
        return CommitmentOut(buildable=False, reason=str(exc)), None
    return CommitmentOut.of(commitment), commitment


def _run_for(request_id: str, history_run_id: str | None) -> dict[str, Any]:
    """결정이 걸릴 **실행 한 건**을 고른다.

    ★ 🔴 **화면이 본 실행으로 검사한다.** `history_run_id` 를 주면 그 행을 읽고,
      종료 코드도 시나리오 라벨도 **그 실행 것**을 쓴다. 최신 실행으로 검사하면
      *"사람이 본 안"* 과 *"검사한 안"* 이 갈린다 — 라벨이 같아 눈에 안 띈다.

    ★ **막지 않고 드러낸다.** 그 사이 재실행이 있어 최신이 아니게 됐어도 거절하지
      않는다. 사람이 그 실행을 보고 결정한 것은 **사실**이고, 그 사실을 그대로
      적는 것이 이 표의 일이다 (8/26 회의: 승인 게이트를 마스터가 들지 않는다).
      낡았다는 것은 `run_id` 가 최신 행과 다르다는 사실로 이미 드러난다.

    ★ 안 주면 예전처럼 최신을 고른다. 다른 클라이언트가 깨지지 않게 하려는 것이고,
      그 경우 **경합이 남는다** — 화면은 반드시 실어 보내야 한다.

    :raises DecisionRejected: 준 실행이 이 업무 키의 것이 아니다 (422).
    """
    if history_run_id is None:
        # ★ 승인 대상은 매입 실행이다. 조회는 승인할 수 없다 (2026-09-02).
        return dict(get_run_by_request_id(request_id, cycle="PROCUREMENT"))
    try:
        run = dict(get_run(UUID(history_run_id)))
    except ValueError as exc:  # UUID 파싱 실패
        raise DecisionRejected(f"실행 id 형식이 아니다: {history_run_id}") from exc
    if run.get("request_id") != request_id:
        # DB 의 복합 FK 가 이것을 최종적으로 막지만, 여기서 잡아야 이유를 돌려준다.
        raise DecisionRejected(
            f"실행 {history_run_id} 는 업무 키 {request_id} 의 것이 아니다 "
            f"(그 실행의 업무 키: {run.get('request_id')})."
        )
    return run


def _reject_repeat_approval(existing: list[DecisionOut], payload: DecisionIn) -> None:
    """이미 승인이 서 있으면 **어느 안이든** 다시 승인하지 못한다.

    ★ 전에는 *"번복 자체는 막지 않는다 — '기본' 을 승인했다가 '보수' 로 바꾸는 것은
      정상적인 업무다"* 였고, **그때는 맞았다.** 승인이 이력에만 남고 장부를 바꾸지
      않던 때의 판단이다. 같은 안 재승인만 막으면 됐다 (버튼 두 번).

    🔴 **지금은 승인이 장부를 바꾼다.** 번복은 `decision_seq` 를 올리므로
      `purchase_id` 도 달라져 `ON CONFLICT` 가 안 걸린다 — 앞 승인이 만든
      `purchases` · `payables` · `unsettled` 가 **그대로 남고 뒤 승인이 얹힌다.**
      되돌리는 경로가 저장소에 없다 (`purchases.CANCELLED` 는 CHECK 에만 있고 쓰는
      코드가 0곳이다). 어제까지는 번복이 `finance_state_ambiguous` 로 다음 날을
      막아 우연히 드러났는데, #285 가 한 행에 누적하게 되면서 **다음 날이 정상으로
      서고 조용히 틀린다.**

    ★ **언제 푸나** — 취소 경로(`purchases.CANCELLED` 쓰기 · payable 역분개 ·
      `confirmed_inbound` 정리)가 생기면 이 조건을 도로 라벨 비교로 좁힌다. 그때까지는
      번복을 받는 것보다 막고 이유를 말하는 것이 낫다.

    ★ **`current.decision == "APPROVE"` 일 때만 막는다.** 첫 승인 · 거절 뒤 승인 ·
      조건부 재요청 뒤 승인은 앞선 장부가 없거나 이미 접혔으므로 그대로 열려 있다.
      이 조건을 지우면 사람이 거절한 뒤 아무것도 못 하게 된다.
    """
    if payload.decision != "APPROVE":
        return
    current = next((row for row in existing if row.is_current), None)
    if current is None or current.decision != "APPROVE":
        return
    raise DecisionRejected(
        f"'{payload.scenario_label}' 를 승인할 수 없다 — 이 실행에는 이미 승인된 안이 "
        f"있다 (회차 {current.decision_seq} · '{current.scenario_label}'). "
        "앞 승인이 만든 장부를 되돌리는 경로가 아직 없어, 번복하면 두 승인이 모두 "
        "장부에 남는다.",
        conflict=True,
    )


def get_decisions(request_id: str) -> list[DecisionOut]:
    """한 요청에 붙은 결정 전부. 최신 하나가 `is_current` 다."""
    return list_decisions(request_id)


def current_commitment(request_id: str) -> CommitmentOut | None:
    """현재 유효한 승인이 만든 확정 입고 약정 (H1 · `GET /runs/{id}/commitment`).

    물류 회신(2026-09-01)이 전달 방식 ⓐ(GET)에 동의해 열었다. **승인 응답을 놓친
    소비자가 약정을 다시 볼 유일한 길**이다 — 적재는 계약이 굳은 뒤로 미뤘다.

    ★ **결정 시점의 실행으로 재조립한다.** 약정을 저장해 두지 않았으므로, 현재 결정이
      가리키는 실행(`history_run_id`)을 읽어 같은 함수로 다시 만든다 — 승인 응답에
      실렸던 것과 같은 값이 나온다 (같은 입력·같은 코드).

    ★ 번복은 여기서 저절로 반영된다 — `is_current` 인 결정 하나만 보므로, 앞 승인의
      약정은 이 경로에서 **사라진다.** 앞 약정을 이미 받아 간 소비자에게 취소를
      알리는 일은 이 경로가 못 한다 (전달 계약 미결 — H1 리뷰 §3).

    :returns: 승인이 없으면 `None` — 라우터가 404 로 접는다. 승인인데 못 만들면
              `buildable=False` 와 사유가 실린다. 둘을 섞지 않는다 (§1.2-10).
    """
    current = next((row for row in list_decisions(request_id) if row.is_current), None)
    if current is None or current.decision != "APPROVE":
        return None

    row = _run_for(request_id, current.history_run_id)
    replay = DecisionIn(
        decision="APPROVE",
        scenario_label=current.scenario_label,
        decided_by=current.decided_by,
        history_run_id=current.history_run_id,
    )
    return _commitment_for(
        request_id, current.decision_seq, replay, dict(row.get("response_payload") or {})
    )


def commitments_before(item: str, as_of: date, *, limit: int = 50) -> list[CommitmentOut]:
    """그 품목에서 **`as_of` 이전에** 승인된 확정 입고 약정 전부 (#185).

    🔴 **오늘 실행이 어제를 아는 유일한 길이다.** 전에는 `current_commitment` 이
      `request_id` 를 알아야만 답할 수 있어, *"피마늘 · 1-02 까지 승인된 것을 다
      다오"* 를 물을 수가 없었다. 그래서 **어제 승인한 매입이 오늘 창고에 없는 것처럼**
      됐다.

    ★ **새로 적재하지 않는다.** `current_commitment` 을 그대로 부른다 — 약정을
      만드는 함수가 하나뿐이어야 **같은 사실의 주인이 하나**로 남는다. 표를 새로
      만들어 두 벌로 두면 재조립본과 적재본이 갈리는 날이 온다.

    ★ **`as_of` 당일은 뺀다** (`as_of_before` 가 `<`). 오늘 것을 같이 세면 실행이
      자기 자신을 입력으로 먹는다.

    ★ **번복은 저절로 빠진다** — `current_commitment` 이 `is_current` 하나만 보므로
      뒤집힌 승인의 약정은 이 목록에 안 들어온다.

    :returns: 승인이 없으면 **빈 목록**. 없는 것을 만들지 않는다 — 빈 목록과
              *"조회를 못 했다"* 는 다르고, 후자는 예외로 올라간다.
    """
    seen: set[str] = set()
    out: list[CommitmentOut] = []
    for run in list_runs(item=item, as_of_before=as_of, limit=limit):
        request_id = run["request_id"] if isinstance(run, dict) else run.request_id
        if request_id in seen:
            continue  # 한 업무 키에 실행이 여럿이어도 약정은 하나다
        seen.add(request_id)
        commitment = current_commitment(request_id)
        if commitment is not None:
            out.append(commitment)
    return out


# ── 승인 → 약정 (H1) ────────────────────────────────────────────────────


def _scenarios_of(
    response_payload: Mapping[str, Any], label: str | None
) -> list[Mapping[str, Any]]:
    """승인한 라벨의 안 **전부**. 라벨로 찾고, 유일성 판정은 호출자가 한다.

    ★ 첫 것을 돌려주는 함수였다가 목록으로 바꿨다 — 라벨이 겹치는 날 첫 것을
      조용히 고르면 검사도 이력도 그 선택을 모른다 (2026-09-01 자기 리뷰).
    """
    return [
        scenario
        for scenario in response_payload.get("scenarios") or ()
        if isinstance(scenario, Mapping) and scenario.get("label") == label
    ]


def _item_of(response_payload: Mapping[str, Any]) -> str | None:
    """실행의 품목. 판정부 `meta.item` 이 정본이다 (매입 보증 2026-09-01)."""
    meta = (response_payload.get("judgment") or {}).get("meta") or {}
    item = meta.get("item")
    return item if isinstance(item, str) and item else None


def _as_of_of(response_payload: Mapping[str, Any]) -> date | None:
    """실행의 기준일.

    🔴 **여기서 예외를 던지면 안 된다 (2026-09-01 실측).** 처음에 `DecisionRejected`
      를 올렸더니 **결정은 이미 적재된 뒤라** 저장은 되고 응답은 409 가 나갔다.
      *"약정을 못 만들어도 결정은 남아야 한다"* 고 적어 놓고 그 반대를 했다.
      못 읽으면 `None` 을 돌려주고 사유를 약정에 싣는다.
    """
    raw = response_payload.get("as_of")
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _lead_days_of(response_payload: Mapping[str, Any]) -> Any:
    """N4. **물류가 준 값을 그대로 읽는다** — 없으면 없는 대로 넘긴다."""
    inventory = (response_payload.get("constraints") or {}).get("inventory") or {}
    return inventory.get("inbound_lead_days")


def _payment_days_of(response_payload: Mapping[str, Any]) -> Any:
    """N5. **재무가 준 값을 그대로 읽는다** — 없으면 없는 대로 넘긴다.

    ★ `_lead_days_of`(N4)와 **같은 모양이다.** 부서가 봉투로 값을 주고 마스터는
      옮기기만 한다 — 마스터가 재무 정책 표를 다시 읽으면 같은 사실의 주인이 둘이 된다
      (`finance/capabilities/procurement.py` 가 `purchase_payment_days` 를 싣는다).
    """
    finance = (response_payload.get("constraints") or {}).get("finance") or {}
    return finance.get("purchase_payment_days")
