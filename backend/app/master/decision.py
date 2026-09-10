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
from app.master.sales_approval import SaleConfirmationOut
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

RevalidationOutcome = Literal["PASSED", "CONDITIONAL", "FAILED", "ERROR"]

#: 최종 승인 클릭 시점에 **그때 선택된 1안을 다시 검증한 결과** (2026-09-04 · 판매 합의).
#:
#:   ```text
#:   PASSED       재검증 통과. 승인 기록 · Write 진행
#:   CONDITIONAL  통과했으나 새 조건이 붙었다 → 승인 기록 안 함
#:   FAILED       재검증에서 막혔다           → 승인 기록 안 함, 실패 이력은 남긴다
#:   ERROR        재검증 자체를 못 돌렸다     → 승인 기록 안 함 (RUNTIME_NOT_READY 등)
#:   ```
#:
#: 🔴 **`CONDITIONAL` 은 통과가 아니다.** 사용자가 승인한 대상은 **그때 화면에 있던
#:   그 안**이다. 새 조건이 붙으면 그것은 다른 안이라, `PASSED` 로 접으면 *사용자가
#:   본 적 없는 조건이 사용자 승인으로 기록된다.*
#:
#: 🔴 **`Decision` 과 섞지 않는다.** 저쪽은 **사람이 무엇을 눌렀나**이고 이쪽은 **그
#:   뒤 재검증이 어떻게 됐나**다. `decision` 에 `REVALIDATION_FAILED` 같은 값을 더하면
#:   *"승인하려다 막혔다"* 가 *"승인하지 않았다"* 로 뭉개지고, `decision` 값으로
#:   판단하는 번복 규칙(`next_seq` · `mark_current`)까지 흔들린다.
#:
#: 🟢 **이제 실제로 돈다** (M-4 · 2026-09-07 · `revalidation.revalidate_scenario`).
#:   `decision == "APPROVE"` 일 때만 채워진다 — 나머지 셋은 승인이 아니라 재검증할
#:   대상이 없고, 그때의 `None` 은 **"재검증에 실패했다"가 아니라 "재검증을 하지
#:   않았다"** 이다. 2026-09-07 이전 결정 전부도 그 `None` 이다.
#:
#: 🔴 **`PASSED` 여도 아직 아무 일도 안 일어난다.** 승인의 효력을 도메인 Write 로
#:   흘리는 것은 M-5 다 — `decision` 은 **의도**의 칸이고 이쪽은 **결과**의 칸이며,
#:   효력은 또 다른 것이다.

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

SALES_CYCLE = "SALES"
"""판매 사이클 실행 행의 `master_agent_runs.cycle` 값 (`persistence._SALES_CYCLE`).

🔴 **어느 어휘로 검사할지는 이 값이 정한다 — 요청 본문이 아니다** (2026-09-08 계약).
   본문으로 받으면 매입 실행에 `cycle="SALES"` 를 실어 보내 `SL1_PRESENTED` 어휘로
   검사받을 수 있고, 그 순간 승인 게이트가 **부르는 쪽 손에** 들어간다.
"""

#: 판매 승인이 성립하는 종료 코드. 🔴 **`_APPROVE_END_CODES` 와 섞지 않는다.**
#:
#: `sales_flow.SalesEndCode` 가 적어 둔 D-3 합의가 그대로 여기에도 걸린다 —
#:
#:   > 매입 `EndCode`(E1~E5) 에 값을 더하지 않는다. 층이 다르다. 한 어휘에 두
#:   > 사이클을 담으면 `E2_HELD` 가 *"매입 보류"* 와 *"판매 보류"* 를 동시에 뜻하게 된다.
#:
#: ⚠️ 두 집합을 한 `frozenset` 으로 합치면 **매입 실행에 `SL1_PRESENTED` 를 우겨도
#:   통과한다.** 코드가 어느 층의 것인지를 집합이 더 이상 구분하지 못하기 때문이다.
_SALES_APPROVE_END_CODES: frozenset[str] = frozenset({"SL1_PRESENTED"})

#: 판매에서 사람이 결정할 것이 있는 종료 코드.
#:
#: `SL4_NOT_STARTED` 는 뺀다 — `E4_NOT_STARTED` 와 같은 이유다. 시작조차 못 한 날은
#: 회사의 판단이 아니라 실행 환경 문제라 사람이 고를 것이 없다.
_SALES_DECIDABLE_END_CODES: frozenset[str] = frozenset(
    {"SL1_PRESENTED", "SL2_NO_CANDIDATE", "SL3_ALL_REJECTED", "SL5_BUDGET_EXHAUSTED"}
)


def approve_end_codes(cycle: str) -> frozenset[str]:
    """그 사이클에서 **승인이 성립하는** 종료 코드.

    🔴 **`cycle` 이 정한다.** 두 어휘를 따로 두는 이상, 어느 것을 볼지도 실행 행이
      정해야 한다 — 부르는 쪽이 정하면 어휘를 나눈 뜻이 없어진다.
    """
    return _SALES_APPROVE_END_CODES if cycle == SALES_CYCLE else _APPROVE_END_CODES


def decidable_end_codes(cycle: str) -> frozenset[str]:
    """그 사이클에서 **사람이 결정할 것이 있는** 종료 코드."""
    return _SALES_DECIDABLE_END_CODES if cycle == SALES_CYCLE else _DECIDABLE_END_CODES


class DecisionRejected(ValueError):
    """결정을 받을 수 없다. 라우터가 409/422 로 접는다.

    ★ 조용히 무시하지 않는다. 받아 놓고 안 적으면 사용자는 결정한 줄 안다.
    """

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        #: 요청 자체가 틀렸나(422) vs 지금 상태에서 받을 수 없나(409)
        self.conflict = conflict


class DecisionIn(BaseModel):
    """`POST /master/runs/{request_id}/decision` 요청 본문.

    🔴 **사이클 칸이 없다 — 일부러 없다** (2026-09-08 계약). 매입 어휘로 볼지 판매
      어휘로 볼지는 **실행 이력 행의 `cycle`** 이 정한다. 본문에 그 칸을 두면 매입
      실행에 `SALES` 를 실어 보내 `SL1_PRESENTED` 어휘로 검사받을 수 있고, 그러면
      승인 게이트가 부르는 쪽 손에 들어간다.

    ⚠️ **`scenario_label` 은 칸 이름과 값이 어긋나는 자리다.** 판매 후보에는 label 이
      없어 그 칸에 `scenario_id` 를 싣기로 했다 (판매 확정). 새 칸을 만들지 않은 이유는
      하나다 — **같은 안을 가리키는 이름이 둘이 되면 갈린다.**
    """

    model_config = {"extra": "forbid"}

    decision: Decision
    scenario_label: str | None = Field(
        default=None,
        description=(
            "APPROVE 일 때 필수. 그 실행이 실제로 내놓은 안을 가리킨다. "
            "🔴 매입은 `scenarios[].label`, 판매는 `candidates[].scenario.scenario_id` 다 "
            "— 판매 후보에는 label 이 없어 이 칸에 scenario_id 를 싣는다 (판매 확정 2026-09-08)."
        ),
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

    #: 최종 승인 시점 재검증이 돈 **새 실행**의 업무 키. DB 컬럼도 같은 이름이다.
    #:
    #: 🔴 **`follow_up_request_id` 와 다른 사건이다.** 저쪽은 `REQUEST_CHANGE` 가 낳은
    #:   재요청이라 결정 **뒤에** 사람이 다시 돌린 것이고, 이쪽은 결정을 **적기 전에**
    #:   서버가 돌린 것이다. 게다가 재검증은 **새 `as_of` · 새 `request_id`** 로 도는
    #:   반면 승인 자체는 원 실행(`history_run_id`)에 달려 있다 — 한 칸에 담으면 그
    #:   값이 어느 쪽인지 아무도 모른다.
    revalidation_request_id: str | None = None

    #: 그 재검증의 결과. `None` 은 **"재검증을 하지 않았다"** 이고 실패가 아니다 —
    #: 2026-09-07 이전 결정과, 아직 배선이 없는 지금의 모든 결정이 그렇다.
    revalidation_outcome: RevalidationOutcome | None = None
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

    #: 판매 승인이 판매 원장에 남긴 결과 (`sales` · `sale_items` · `CONFIRMED`).
    #:
    #: 🔴 **`commitment` · `transition` 과 섞지 않는다.** 저 둘은 **매입** 승인의
    #:   효력이고 이것은 **판매** 승인의 효력이다 — 한 칸에 담으면 어느 사이클의
    #:   승인이었는지가 값의 모양으로만 읽힌다.
    #:
    #: ★ 승인이 아니거나 매입 실행이면 `None` 이고, 승인인데 확정 못 했으면
    #:   `BLOCKED` 와 사유가 실린다 — 둘을 섞지 않는다 (§1.2-10).
    #:
    #: ★ 응답 전용이라 이력 조회에는 안 실린다.
    sale: SaleConfirmationOut | None = None


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


def scenario_ids_of(response_payload: Mapping[str, Any]) -> tuple[str, ...]:
    """판매 실행이 실제로 내놓은 후보의 `scenario_id` 목록.

    ```text
    매입 응답   scenarios[].label              "보수" · "기본"
    판매 응답   candidates[].scenario.scenario_id   "SALES-001-A-R1"  ← 여기
    ```

    ★ **`scenario_labels_of` 와 합치지 않는다.** 한 함수가 두 칸을 다 훑으면 매입
      실행에 판매 모양의 후보가 섞여 들어와도 그대로 통과한다 — 어느 층의 안을
      승인했는지가 응답 모양에 따라 갈린다.

    🔴 **탈락 후보도 목록에 든다.** 판매 응답은 `passed=False` 인 후보를 사유와 함께
      같이 내보내므로 (`SalesOutcome.rejected`), 여기서 거르면 *"제시되지 않은 안"*
      과 *"제시했으나 탈락한 안"* 이 같은 422 로 접힌다. 탈락안 승인을 막는 것은
      후보 판정(`CandidateVerdict.passed`)과 재검증이지 이 목록이 아니다.
    """
    return tuple(
        scenario_id
        for scenario in _sales_scenarios(response_payload)
        if isinstance(scenario_id := scenario.get("scenario_id"), str) and scenario_id
    )


def _sales_scenarios(response_payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """판매 응답이 실은 후보의 `scenario` 칸들.

    ★ **`candidates[].scenario` 를 훑는 자리를 하나로 둔다.** 판매가 그 칸의 모양을
      바꾸는 날 고칠 곳이 하나여야 한다.
    """
    out: list[Mapping[str, Any]] = []
    for candidate in response_payload.get("candidates") or ():
        if not isinstance(candidate, Mapping):
            continue
        scenario = candidate.get("scenario")
        if isinstance(scenario, Mapping):
            out.append(scenario)
    return tuple(out)


def scenario_ids_of_type(
    response_payload: Mapping[str, Any], scenario_type: str
) -> tuple[str, ...]:
    """그 `scenario_type` 인 판매 후보의 `scenario_id` **전부.**

    ```text
    scenario_id     후보 Identity        "SALES-001-A-R1"
    scenario_type   후보의 의미          축 이름 하나            ← 이것으로 찾는다
    ```

    ★ **판매가 정한 계약이다** (2026-09-10). 판매 후보에는 `label` 이 없고, 같은
      뜻을 두 칸에 복제하지 않기로 했다 — 그래서 후보를 **의미로 찾고** 가리킬 때는
      **Identity 로 가리킨다.**

    🔴 **축 이름을 코드에 열거하지 않는다.** `scenario_labels_of` 가 적어 둔 규율
      그대로다 — 어느 축인지는 부르는 쪽이 말한다. 여기가 축 이름을 알면 판매가
      축을 바꾸는 날 조용히 어긋난다.

    🔴 **하나로 좁히지 않는다.** 몇 개인지가 부르는 쪽의 판단 재료다 — 여기서 첫
      번째를 돌려주면 **배열 순서가 선택 규칙**이 되고, 판매가 *"배열 순서 기반
      선택은 쓰지 않는다"* 고 명시한 것을 이 함수가 혼자 뒤집는다.
    """
    return tuple(
        scenario_id
        for scenario in _sales_scenarios(response_payload)
        if scenario.get("scenario_type") == scenario_type
        and isinstance(scenario_id := scenario.get("scenario_id"), str)
        and scenario_id
    )


def available_scenario_names(
    response_payload: Mapping[str, Any], cycle: str
) -> tuple[str, ...]:
    """그 실행이 내놓은 안을 **가리키는 이름** 전부.

    🔴 **어느 칸을 읽을지도 `cycle` 이 정한다.** 승인 검사와 승인 어휘가 같은 것을
      보고 있어야 *"승인은 되는데 안을 못 찾는다"* 가 안 생긴다.
    """
    if cycle == SALES_CYCLE:
        return scenario_ids_of(response_payload)
    return scenario_labels_of(response_payload)


def check_decidable(end_code: str, decision: Decision, *, cycle: str) -> None:
    """지금 상태에서 이 결정을 받을 수 있나.

    ★ `E4` 에는 아무 결정도 받지 않는다. 부서가 못 돈 날을 사람이 "승인" 하면
      **아무도 판단하지 않은 계획이 승인된 것으로 남는다.**

    🔴 **`cycle` 에 기본값을 두지 않는다.** 안 주면 터져야 한다 — 기본값은 곧
      업무 규칙이고, 여기서는 *"안 밝히면 매입으로 본다"* 가 조용한 규칙이 된다.
      부르는 쪽은 실행 행에서 읽은 값을 그대로 넘긴다.

    :param cycle: 그 **실행 이력 행**의 `cycle`. 🔴 요청 본문에서 오지 않는다.
    """
    decidable = decidable_end_codes(cycle)
    approvable = approve_end_codes(cycle)
    if end_code not in decidable:
        raise DecisionRejected(
            f"{end_code} 인 실행에는 결정을 받지 않는다 — "
            "부서가 못 돈 날은 사람이 고를 것이 없다. 재시도는 새 요청이다.",
            conflict=True,
        )
    if decision == "APPROVE" and end_code not in approvable:
        raise DecisionRejected(
            f"{end_code} 에는 승인할 안이 없다 "
            f"(통과안은 {', '.join(sorted(approvable))} 에만 있다).",
            conflict=True,
        )
    if decision == "CANCEL" and end_code not in approvable:
        # ★ **물릴 승인이 있으려면 그날 통과안이 있었어야 한다.** 없는 승인을 취소하면
        #   이력에는 취소가 남고 장부에는 아무 일도 안 일어난다 — 그 둘이 갈리면
        #   나중에 *"왜 취소했는데 그대로지"* 를 아무도 못 푼다.
        raise DecisionRejected(
            f"{end_code} 에는 물릴 승인이 없다 "
            f"(승인은 {', '.join(sorted(approvable))} 에만 선다).",
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
