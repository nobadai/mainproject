"""결정 접수 — 실행 이력을 읽고, 규칙을 걸고, 적재한다.

★ `service.py` 와 나눈 이유 — 저쪽은 **Flow 실행**의 경계 변환이고 여기는 **사람의
  결정**이다. 한 파일에 두면 `run_procurement` 에서 결정 함수를 부르기가 쉬워지는데,
  그 순간 승인 게이트가 툴 안으로 들어온다.
"""

from __future__ import annotations

from typing import Any

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
from app.orchestrator.run_repository import get_run_by_request_id


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
    row = get_run_by_request_id(request_id)  # 없으면 LookupError
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
        note=payload.note,
    )


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
