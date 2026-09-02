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

from app.master.commitment import CommitmentNotBuildable, build_commitment
from app.master.decision import (
    CommitmentOut,
    DecisionIn,
    DecisionOut,
    DecisionRejected,
    check_decidable,
    check_scenario_exists,
    next_seq,
    scenario_labels_of,
)
from app.master.decision_repository import list_decisions, save_decision
from app.master.run_repository import get_run, get_run_by_request_id


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

    seq = next_seq(existing)
    saved = save_decision(
        request_id=request_id,
        decision_seq=seq,
        decision=payload.decision,
        decided_by=payload.decided_by,
        end_code_at_decision=end_code,
        scenario_label=payload.scenario_label,
        condition_text=payload.condition_text,
        history_run_id=str(row["run_id"]),
        note=payload.note,
    )
    return saved.model_copy(
        update={"commitment": _commitment_for(request_id, seq, payload, response_payload)}
    )


def _commitment_for(
    request_id: str,
    decision_seq: int,
    payload: DecisionIn,
    response_payload: Mapping[str, Any],
) -> CommitmentOut | None:
    """승인이면 **확정 입고 약정**을 같이 낸다 (H1).

    ★ **적재 뒤에 만든다.** 약정을 못 만들어도 결정은 남아야 한다 — 사람이 승인한
      것은 사실이고, 그 사실을 약정 조립 실패가 지우면 안 된다.

    ★ **못 만들면 이유를 싣는다.** `None` 을 조용히 돌려주면 물류가 *"입고 예정이
      없다"* 로 읽는다. 없는 것과 못 만든 것은 다르다 (§1.2-10).

    ★ **오케 `cycle.py` 를 부르지 않는다** (지시 2026-09-01). 같은 변환이 거기에도
      있지만 그 경로는 M-1 에서 안 돌고, 회차 수량을 합쳐 **품목을 없앤다.**
    """
    if payload.decision != "APPROVE":
        return None

    matches = _scenarios_of(response_payload, payload.scenario_label)
    if not matches:
        return CommitmentOut(buildable=False, reason="승인한 안을 실행 응답에서 찾지 못했다.")
    if len(matches) > 1:
        # 🔴 첫 것을 조용히 고르면 **어느 안을 약정했는지가 운에 걸린다** (자기 리뷰).
        return CommitmentOut(
            buildable=False,
            reason=f"라벨 '{payload.scenario_label}' 이 {len(matches)}개다 — 유일하지 않다.",
        )
    scenario = matches[0]

    as_of = _as_of_of(response_payload)
    if as_of is None:
        return CommitmentOut(
            buildable=False, reason="실행 이력에 기준일이 없어 도착일을 걸 수 없다."
        )

    try:
        commitment = build_commitment(
            request_id=request_id,
            as_of=as_of,
            item=_item_of(response_payload),
            scenario=scenario,
            inbound_lead_days=_lead_days_of(response_payload),
            decision_seq=decision_seq,
        )
    except CommitmentNotBuildable as exc:
        return CommitmentOut(buildable=False, reason=str(exc))
    return CommitmentOut.of(commitment)


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
