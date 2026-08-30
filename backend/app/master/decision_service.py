"""결정 접수 — 실행 이력을 읽고, 규칙을 걸고, 적재한다.

★ `service.py` 와 나눈 이유 — 저쪽은 **Flow 실행**의 경계 변환이고 여기는 **사람의
  결정**이다. 한 파일에 두면 `run_procurement` 에서 결정 함수를 부르기가 쉬워지는데,
  그 순간 승인 게이트가 툴 안으로 들어온다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.master.decision import (
    DecisionIn,
    DecisionOut,
    DecisionRejected,
    check_decidable,
    check_scenario_exists,
    next_seq,
    scenario_labels_of,
)
from app.master.decision_repository import list_decisions, save_decision
from app.orchestrator.run_repository import get_run, get_run_by_request_id


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

    순서가 중요하다 — **읽고 → 검사하고 → 적재한다.** 적재 후 검사하면 잘못된 결정이
    이력에 남는다.

    :raises LookupError: 그 업무 키의 실행이 없다 (라우터가 404).
    :raises DecisionRejected: 지금 상태에서 받을 수 없다 (라우터가 409/422).
    """
    row = _run_for(request_id, payload.history_run_id)  # 없으면 LookupError
    response_payload = dict(row.get("response_payload") or {})

    end_code = _end_code_of(response_payload)
    check_decidable(end_code, payload.decision)
    check_scenario_exists(payload.scenario_label, scenario_labels_of(response_payload))

    existing = list_decisions(request_id)
    _reject_repeat_approval(existing, payload)

    return save_decision(
        request_id=request_id,
        decision_seq=next_seq(existing),
        decision=payload.decision,
        decided_by=payload.decided_by,
        end_code_at_decision=end_code,
        scenario_label=payload.scenario_label,
        condition_text=payload.condition_text,
        history_run_id=str(row["run_id"]),
        note=payload.note,
    )


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
        return dict(get_run_by_request_id(request_id))
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
    """같은 안을 두 번 승인하는 것만 막는다.

    ★ 번복 자체는 막지 않는다 — '기본' 을 승인했다가 '보수' 로 바꾸는 것은 정상적인
      업무다. 다만 **같은 안을 다시 승인**하는 것은 새 사실이 없어 이력만 늘린다
      (보통 버튼 두 번 누른 것이다).
    """
    if payload.decision != "APPROVE":
        return
    current = next((row for row in existing if row.is_current), None)
    if current is None:
        return
    if current.decision == "APPROVE" and current.scenario_label == payload.scenario_label:
        raise DecisionRejected(
            f"'{payload.scenario_label}' 은 이미 승인됐다 (회차 {current.decision_seq}). "
            "다른 안으로 바꾸려면 그 안을 보내라.",
            conflict=True,
        )


def get_decisions(request_id: str) -> list[DecisionOut]:
    """한 요청에 붙은 결정 전부. 최신 하나가 `is_current` 다."""
    return list_decisions(request_id)
