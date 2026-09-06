"""마스터 사용자 결정 — 스키마와 판단 규칙.

회의 미결정 12번("사용자 선택 이후 실제 실행 여부 기록")에 대한 답이다.

★ **LLM 이 없다.** 사람이 고른 것을 그대로 적는다. 해석할 것이 없다.

★ **`flow.py` 는 이 모듈을 임포트하지 않는다.**
  승인 게이트가 마스터가 부를 수 있는 툴 목록 안에 있으면 마스터가 스스로 통과시킬 수
  있다. 8/26 회의가 "승인 게이트를 툴 바깥에 두어 우회 불가하게" 로 정한 이유다.

★ **적재 실패를 삼키지 않는다.**
  `persistence.record` 는 실패를 삼킨다 — 이력이 없는 것보다 결과를 못 주는 것이 나쁘기
  때문이다. **결정은 반대다.** 안 남았는데 남았다고 하면 승인 없이 실행된 것과 같아진다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.master.commitment import ApprovedCommitment
from app.master.transition import TransitionOut

Decision = Literal["APPROVE", "REJECT_ALL", "REQUEST_CHANGE", "CANCEL"]

#: 🔴 **`CANCEL` 은 `REJECT_ALL` 과 다르다** (2026-09-05 전원 합의).
#:
#:   ```text
#:   REJECT_ALL   "이 안을 안 쓴다"      — 승인 **전** 판단. 장부를 안 건드린다
#:   CANCEL       "승인했던 것을 물린다"  — 승인 **후** 사실. 장부 다섯을 되돌린다
#:   ```
#:
#: ★ 둘을 한 어휘로 적으면 *"거절해서 장부가 없는 것"* 과 *"취소해서 장부가 물린
#:   것"* 이 같아진다. `#290` 이 `REJECT_ALL` 로 우회되는 것도 그 둘이 갈려 있지
#:   않아서였다.

#: 승인은 통과안이 있는 날에만 성립한다.
#:
#: ★ **취소도 같다.** 물릴 승인이 있으려면 그날 통과안이 있었어야 한다 — `E2_HELD` 인
#:   날에 "취소" 를 받으면 물릴 것이 없는데 이력에는 취소가 남는다.
_APPROVE_END_CODES: frozenset[str] = frozenset({"E1_APPROVED"})

#: 사람이 결정할 것이 있는 종료 코드.
#:
#: `E4_NOT_STARTED` 는 뺀다 — 부서가 못 돈 날은 **회사의 판단이 아니라 실행 환경 문제**라
#: 사람이 고를 것이 없다. 그날의 재시도는 결정이 아니라 새 요청이다.
_DECIDABLE_END_CODES: frozenset[str] = frozenset(
    {"E1_APPROVED", "E2_HELD", "E3_REJECTED", "E5_NO_FEASIBLE_PLAN"}
)


class DecisionRejected(ValueError):
    """결정을 받을 수 없다. 라우터가 409/422 로 접는다.

    ★ 조용히 무시하지 않는다. 받아 놓고 안 적으면 사용자는 결정한 줄 안다.
    """

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        #: 요청 자체가 틀렸나(422) vs 지금 상태에서 받을 수 없나(409)
        self.conflict = conflict


class DecisionIn(BaseModel):
    """`POST /master/runs/{request_id}/decision` 요청 본문."""

    model_config = {"extra": "forbid"}

    decision: Decision
    scenario_label: str | None = Field(
        default=None,
        description="APPROVE 일 때 필수. 그 실행이 실제로 내놓은 안의 label 이어야 한다.",
    )
    condition_text: str | None = Field(
        default=None,
        min_length=1,
        description="REQUEST_CHANGE 일 때 필수. 조건 없는 재요청은 그냥 거절이다.",
    )
    decided_by: str = Field(
        min_length=1,
        description="승인자. 승인자가 없는 승인은 승인이 아니다.",
    )
    history_run_id: str | None = Field(
        default=None,
        description=(
            "화면이 **보고 있던 실행**의 이력 행 id. 주면 그 실행으로 검사하고 그것을 "
            "가리켜 기록한다. 안 주면 서버가 최신 실행을 고르는데, 그 사이 재실행이 "
            "있었으면 사람이 본 것과 다른 안이 승인된 것으로 남는다."
        ),
    )
    note: str | None = None

    @model_validator(mode="after")
    def _shape_matches_decision(self) -> DecisionIn:
        """DB CHECK 과 같은 규칙을 입구에서도 건다.

        ★ 두 곳에 두는 것이 중복이 아니다 — DB 는 **다른 경로로 들어온 행**도 막고,
          여기는 **사용자에게 이유를 돌려준다**. 뒤에서 터지면 500 이 된다.
        """
        if self.decision == "APPROVE" and not self.scenario_label:
            raise ValueError("APPROVE 에는 고른 안(scenario_label)이 있어야 한다.")
        if self.decision != "APPROVE" and self.scenario_label:
            raise ValueError(
                f"{self.decision} 에는 scenario_label 을 넣지 않는다 — "
                "무엇을 거절했는지가 두 가지로 읽힌다."
            )
        if self.decision == "REQUEST_CHANGE" and not self.condition_text:
            raise ValueError("REQUEST_CHANGE 에는 조건(condition_text)이 있어야 한다.")
        return self


class ArrivalLegOut(BaseModel):
    """입고 1회분. **품목이 붙어 있다.**"""

    item: str
    qty_kg: float
    arrival_date: date
    purchase_date: date
    seq: int


class CommitmentOut(BaseModel):
    """승인이 만든 확정 입고 약정 (H1). 물류의 미래 창고 점유 입력이 된다.

    🔴 **`buildable=False` 를 `None` 과 섞지 않는다.**
      승인이 아니어서 약정이 없는 것(`None`)과, 승인했는데 못 만든 것은 다르다.
      후자를 조용히 비우면 물류가 *"입고 예정이 없다"* 로 읽는다 (§1.2-10).
    """

    buildable: bool = True
    reason: str | None = Field(
        default=None, description="못 만든 이유. `buildable=False` 일 때만 찬다."
    )

    approval_id: str | None = None
    item: str | None = None
    scenario_label: str | None = None
    total_qty_kg: float | None = None
    total_amount_krw: float | None = None
    inbound_lead_days: float | None = None
    first_arrival: date | None = None
    arrival_schedule: list[ArrivalLegOut] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list,
        description="약정은 섰으나 일정을 못 만든 사유 등 — 빈 일정을 설명한다.",
    )

    @classmethod
    def of(cls, commitment: ApprovedCommitment) -> CommitmentOut:
        return cls(
            approval_id=commitment.approval_id,
            item=commitment.item,
            scenario_label=commitment.scenario_label,
            total_qty_kg=commitment.total_qty_kg,
            total_amount_krw=commitment.total_amount_krw,
            inbound_lead_days=commitment.inbound_lead_days,
            first_arrival=commitment.first_arrival,
            arrival_schedule=[
                ArrivalLegOut(
                    item=leg.item,
                    qty_kg=leg.qty_kg,
                    arrival_date=leg.arrival_date,
                    purchase_date=leg.purchase_date,
                    seq=leg.seq,
                )
                for leg in commitment.arrival_schedule
            ],
            notes=list(commitment.notes),
        )


class DecisionOut(BaseModel):
    """적재된 결정 1건."""

    decision_id: UUID
    request_id: str
    decision_seq: int
    decision: Decision
    scenario_label: str | None = None
    condition_text: str | None = None
    decided_by: str
    follow_up_request_id: str | None = None
    end_code_at_decision: str
    #: 이 결정이 가리키는 실행 이력 행. DB 컬럼은 `master_decisions.run_id` 다.
    #: `None` 은 **"실행이 없다"가 아니라 "어느 실행인지 기록되지 않았다"** 이다 —
    #: 2026-08-30 이전 결정이 그렇다.
    history_run_id: str | None = None
    note: str | None = None
    created_at: datetime

    is_current: bool = Field(
        default=False,
        description="최신 결정인가. 번복이 있으면 이전 것은 False — 지우지 않고 접는다.",
    )

    #: 승인이 만든 확정 입고 약정 (H1). **승인이 아니면 `None`** 이고, 승인인데 못
    #: 만들었으면 `buildable=False` 와 사유가 실린다 — 둘을 섞지 않는다.
    #: 적재 대상이 아니라 응답 전용이라 이력 조회에는 안 실린다.
    commitment: CommitmentOut | None = None

    #: 그 약정이 재무·물류 장부를 실제로 바꾼 결과 (C 형태 ⑦). **약정이 서지
    #: 않았으면 `None`** 이고, 섰는데 전이 구현이 아직 없으면 `NOT_APPLIED` 와
    #: 빠진 파트가 실린다 — `None` 과 섞지 않는다. 반영하려다 실패한 것은 `FAILED` 다.
    #: 이것도 적재 대상이 아니라 응답 전용이라 이력 조회에는 안 실린다.
    transition: TransitionOut | None = None


# ── 판단 ────────────────────────────────────────────────────────────────


def scenario_labels_of(response_payload: Mapping[str, Any]) -> tuple[str, ...]:
    """그 실행이 실제로 내놓은 안의 label 목록.

    ★ 라벨을 열거로 박지 않고 **응답에서 읽는** 이유 — '보수·기본·공격' 은 매입의
      계약이다. 여기에 복제하면 매입이 라벨을 바꿀 때 조용히 어긋난다.
    """
    scenarios = response_payload.get("scenarios") or []
    out: list[str] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            continue
        label = scenario.get("label")
        if isinstance(label, str) and label:
            out.append(label)
    return tuple(out)


def check_decidable(end_code: str, decision: Decision) -> None:
    """지금 상태에서 이 결정을 받을 수 있나.

    ★ `E4` 에는 아무 결정도 받지 않는다. 부서가 못 돈 날을 사람이 "승인" 하면
      **아무도 판단하지 않은 계획이 승인된 것으로 남는다.**
    """
    if end_code not in _DECIDABLE_END_CODES:
        raise DecisionRejected(
            f"{end_code} 인 실행에는 결정을 받지 않는다 — "
            "부서가 못 돈 날은 사람이 고를 것이 없다. 재시도는 새 요청이다.",
            conflict=True,
        )
    if decision == "APPROVE" and end_code not in _APPROVE_END_CODES:
        raise DecisionRejected(
            f"{end_code} 에는 승인할 안이 없다 (통과안은 E1_APPROVED 에만 있다).",
            conflict=True,
        )
    if decision == "CANCEL" and end_code not in _APPROVE_END_CODES:
        # ★ **물릴 승인이 있으려면 그날 통과안이 있었어야 한다.** 없는 승인을 취소하면
        #   이력에는 취소가 남고 장부에는 아무 일도 안 일어난다 — 그 둘이 갈리면
        #   나중에 *"왜 취소했는데 그대로지"* 를 아무도 못 푼다.
        raise DecisionRejected(
            f"{end_code} 에는 물릴 승인이 없다 (승인은 E1_APPROVED 에만 선다).",
            conflict=True,
        )


def check_scenario_exists(
    scenario_label: str | None,
    available: Sequence[str],
) -> None:
    """고른 안이 그 실행에 실제로 있었나.

    ★ **이 검사가 이 모듈의 핵심이다.** 없는 안을 승인하면 이력에는 승인이 남고
      대조할 대상은 없다 — 나중에 "무엇을 승인했나" 에 답할 수 없다.
    """
    if scenario_label is None:
        return
    if scenario_label not in available:
        shown = ", ".join(available) if available else "(없음)"
        raise DecisionRejected(
            f"'{scenario_label}' 은 이 실행이 내놓은 안이 아니다. 제시된 안: {shown}",
        )


def next_seq(existing: Sequence[DecisionOut]) -> int:
    """번복은 덮어쓰지 않고 회차를 올린다."""
    return max((row.decision_seq for row in existing), default=0) + 1


def mark_current(rows: Sequence[DecisionOut]) -> list[DecisionOut]:
    """최신 회차 하나만 `is_current=True` 로.

    ★ DB 에 플래그를 두지 않는다. 플래그는 UPDATE 를 부르고, UPDATE 는 append-only 를
      깬다. 최대 회차에서 **파생**하면 이력이 그대로 남는다.
    """
    if not rows:
        return []
    top = max(row.decision_seq for row in rows)
    return [row.model_copy(update={"is_current": row.decision_seq == top}) for row in rows]
