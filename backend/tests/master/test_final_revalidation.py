"""최종 승인 시점 재검증이 **실제로 돈다** — M-4 (설계 2026-09-07).

M-3 으로 칸은 섰다(`revalidation_request_id` · `revalidation_outcome`). 여기서 재는
것은 그 칸을 **무엇이 채우는가**다.

```text
지금까지   읽고 → 검사하고 → 적재한다
이제부터   읽고 → 검사하고 → 🔴 재검증하고 → 적재한다
```

🔴 **이 파일이 잡으려는 다섯.**

```text
① S-1 재사용                원 실행 회신을 다시 쓰면 바뀐 것을 못 보고 통과시킨다
② as_of                     원 실행 날로 돌면 "그 사이 바뀌었는가" 를 아무것도 안 잰다
③ CONDITIONAL 접기          원래도 조건부였던 것과 새 조건이 붙은 것은 다르다
④ 미개장을 FAILED 로        "돌리지 못한 것" 과 "막힌 것" 은 다르다
⑤ 결정 행을 안 쓰기          막혔다고 행을 안 쓰면 "승인하려다 막혔다" 가 사라진다
```

★ **DB 를 치지 않는다.** 저장소는 in-memory 로 갈아 끼우고, 개장 관문은
  `conftest.py` 가 이미 막아 둔다 (`test_db_isolation.py` 가 그 격리를 잰다).
  실행 이력 적재는 `try_save_run` 을 가로채 *"무엇을 넣으려 했는가"* 만 본다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.master import decision_service, persistence, revalidation, wiring
from app.master.day_gate import DayGate
from app.master.decision import DecisionIn, DecisionOut, mark_current
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata

REQ = "REQ-20260901-0001"
RUN_UUID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
LABEL = "기본"

#: 원 실행이 돈 날. **결정하는 날이 아니다** — ② 를 재려면 둘이 달라야 한다.
원_실행일 = date(2026, 9, 1)

#: 사람이 결정을 누른 날. 🔴 **진입점이 정해서 넘기는 값**이라 검사가 그냥 고른다
#: (2026-09-09 · `#452`). 전에는 `revalidation.today()` 로 벽시계를 읽었고, 그래서
#: 이 검사들이 **도는 날마다 다른 값**을 재고 있었다.
오늘 = date(2026, 9, 7)

#: 재검증 필수 둘의 라우팅 (`CAPABILITY_ROUTING`). 손으로 적은 것이 아니라 기대값이다.
필수_호출 = [("inventory", "PRE_SALES"), ("finance", "SALES_VALIDATION")]


# ---------------------------------------------------------------------------
# 대역 — 부서 · 저장소
# ---------------------------------------------------------------------------


def 조정(target_value: float = 18000000.0, reason: str = "한도 초과") -> dict[str, Any]:
    """부서 조정 제안 하나의 **표준형**(`wire_adjustment` 가 편 모양) 그대로."""
    return {
        "dept": "finance",
        "axis": "amount",
        "target_value": target_value,
        "unit": "KRW",
        "reason": reason,
        "ref_ids": ["FIN-1"],
        "scenario_labels": [LABEL],
        "split_date": None,
    }


def _표준형을_객체로(raw: Mapping[str, Any]):
    from app.contracts.core import SuggestedAdjustment

    return SuggestedAdjustment(
        dept=raw["dept"],
        axis=raw["axis"],
        target_value=raw["target_value"],
        unit=raw["unit"],
        reason=raw["reason"],
        ref_ids=tuple(raw["ref_ids"]),
        scenario_labels=tuple(raw["scenario_labels"]),
        split_date=raw["split_date"],
    )


class 부서:
    """등록된 어댑터 대역. **무엇을 어떤 as_of 로 물었는지 남긴다.**"""

    def __init__(self, business_status: str = "ok", adjustments: tuple = ()) -> None:
        self.business_status = business_status
        self.adjustments = adjustments
        self.호출: list[tuple[str, str, date, str]] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.호출.append(
            (request.agent, request.mode, request.context.as_of, request.context.request_id)
        )
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status=self.business_status,
            reasoning="대역",
            suggested_adjustments=tuple(
                _표준형을_객체로(a) for a in self.adjustments if request.agent == a["dept"]
            ),
        )
        return reply, ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("tool_a",),
            tool_order=(1,),
        )


@pytest.fixture
def 부서들(monkeypatch) -> dict[str, 부서]:
    """물류·재무를 등록한다. **필수 capability 둘이 그 둘로 라우팅된다.**"""
    wiring.reset()  # 루트 conftest 가 스냅샷을 떠 두므로 이 테스트 밖으로 안 샌다
    등록 = {"inventory": 부서(), "finance": 부서()}
    for 이름, 포트 in 등록.items():
        wiring.register(이름, 포트)
    return 등록


class 결정_저장소:
    """`master_decisions` 를 대신하는 append-only 리스트. **넘긴 인자를 그대로 둔다.**"""

    def __init__(self) -> None:
        self.rows: list[DecisionOut] = []
        self.인자: list[dict[str, Any]] = []

    def list_decisions(self, request_id: str) -> list[DecisionOut]:
        return mark_current([r for r in self.rows if r.request_id == request_id])

    def save_decision(self, **kw: Any) -> DecisionOut:
        self.인자.append(dict(kw))
        row = DecisionOut(
            decision_id=uuid4(),
            created_at=datetime.now(UTC),
            is_current=True,
            **{k: v for k, v in kw.items() if k != "note"},
            note=kw.get("note"),
        )
        self.rows.append(row)
        return row


def _run_row(
    *,
    labels: tuple[str, ...] = (LABEL,),
    required: tuple[str, ...] = (),
    adjustments: tuple[dict[str, Any], ...] = (),
    policy_version: str | None = "v1.3-PROVISIONAL",
    candidates: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    request_payload: dict[str, Any] = {"item": "배추"}
    if policy_version is not None:
        request_payload["policy_version"] = policy_version
    return {
        "run_id": RUN_UUID,
        "request_id": REQ,
        "item": "배추",
        "as_of": 원_실행일,
        "request_payload": request_payload,
        "response_payload": {
            "end_code": "E1_APPROVED",
            "as_of": 원_실행일.isoformat(),
            "scenarios": [
                {"label": label, "required_validations": list(required)} for label in labels
            ],
            "candidates": list(candidates),
            "adjustments": list(adjustments),
        },
    }


@pytest.fixture
def 이력(monkeypatch) -> 결정_저장소:
    저장소 = 결정_저장소()
    monkeypatch.setattr(decision_service, "list_decisions", 저장소.list_decisions)
    monkeypatch.setattr(decision_service, "save_decision", 저장소.save_decision)
    return 저장소


def _실행을_세운다(monkeypatch, row: dict[str, Any]) -> None:
    monkeypatch.setattr(
        decision_service, "get_run_by_request_id", lambda request_id, **kw: dict(row)
    )
    monkeypatch.setattr(decision_service, "get_run", lambda run_id: dict(row))


def _승인(**kw: Any) -> DecisionIn:
    base: dict[str, Any] = {"decision": "APPROVE", "scenario_label": LABEL, "decided_by": "이현서"}
    base.update(kw)
    return DecisionIn(**base)


# ---------------------------------------------------------------------------
# ① 재검증이 실제로 돈다 — S-1 재사용 금지
# ---------------------------------------------------------------------------


def test_승인은_부서를_다시_부른다(monkeypatch, 이력, 부서들):
    """🔴 **S-1(기여 호출 재사용)을 쓰면 이 검사가 빨개진다.**

    목적이 *"그 사이 바뀌었는가"* 라 재사용이 **틀린 규칙**이다 — 원 실행 회신을
    다시 쓰면 바뀐 것을 못 보고 통과시킨다 (설계 §1). 그래서 **부서를 실제로 부른
    자국**을 잰다.
    """
    _실행을_세운다(monkeypatch, _run_row())

    decision_service.record_decision(REQ, _승인())

    불린_것 = [(a, m) for 부 in 부서들.values() for (a, m, _as_of, _rid) in 부.호출]
    assert 불린_것 == 필수_호출, (
        f"필수 capability 둘을 다시 안 불렀다 — 원 실행 회신을 재사용한 것이다: {불린_것}"
    )


def test_원_실행이_ok_였어도_오늘_reject_면_FAILED(monkeypatch, 이력, 부서들):
    """★ 위 검사의 짝. **부르기만 하고 결과를 안 보면** 소용이 없다.

    원 실행에는 그 안이 통과로 남아 있다 (`E1_APPROVED` · 사용자가 승인 버튼을
    눌렀다). 오늘 재무가 막으면 **오늘 답을 따라야 한다.**
    """
    부서들["finance"].business_status = "reject"
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "FAILED"


def test_필수_둘은_후보가_요구하지_않아도_부른다(monkeypatch, 이력, 부서들):
    """★ 후보의 `required_validations` 는 *"제시하려면 무엇이 필요한가"* 이고, 필수
    둘은 *"승인 직전에 무엇을 다시 봐야 하는가"* 다 — 물음이 다르므로 목록도 다르다.
    """
    _실행을_세운다(monkeypatch, _run_row(required=()))

    decision_service.record_decision(REQ, _승인())

    assert [(a, m) for 부 in 부서들.values() for (a, m, _d, _r) in 부.호출] == 필수_호출


def test_후보가_요구한_것도_같이_부르되_두_번_부르지_않는다(monkeypatch, 이력, 부서들):
    """★ 조건부는 **그 후보가 요구했던 나머지**다. 필수와 겹치면 한 번만 부른다 —
    두 번 부르면 예산만 태우고 답은 같다."""
    _실행을_세운다(
        monkeypatch,
        _run_row(required=("FINANCIAL_VALIDATION", "DELIVERY_FEASIBILITY_CONTEXT")),
    )

    decision_service.record_decision(REQ, _승인())

    불린_것 = [(a, m) for 부 in 부서들.values() for (a, m, _d, _r) in 부.호출]
    assert 불린_것.count(("finance", "SALES_VALIDATION")) == 1
    # `DELIVERY_FEASIBILITY_CONTEXT` 도 물류로 라우팅된다 — 그래서 물류는 두 번이다.
    assert 불린_것.count(("inventory", "PRE_SALES")) == 2


# ---------------------------------------------------------------------------
# ② as_of 는 오늘이고, 새 업무 키로 돈다
# ---------------------------------------------------------------------------


def test_재검증은_그_실행의_날로_돈다(monkeypatch, 이력, 부서들):
    """🔴 **재검증이 서는 날은 그 실행의 날이다** (2026-09-09 · 마스터 판단).

    ⚠️ **이 검사는 뜻이 뒤집혔다.** 전에는 *"원 실행의 날로 돌면 아무것도 안 잰다"* 를
      지켰다. 그 걱정은 근거가 있었지만 **우리 시스템에서는 성립하지 않는다.**

      ```text
      전제였던 것   제안한 날과 고른 날 사이에 세상이 바뀐다 (실제 운영)
      실제           우리는 시뮬레이션이고, "그 사이" 는 **그날 안**에서 일어난다
                     — 그날의 입고·수금·출고가 이미 지나간 뒤 승인이 선다
      ```

    🔴 **그리고 벽시계로 돌면 승인이 막힌다** (실측 2026-09-09). 재검증의 첫 관문이
      개장이고, 화면이 오늘 누르면 **오늘은 안 열린 날**이라 `ERROR` 가 난다.

    ★ 그래서 그 날은 **부르는 쪽이 정하지 않는다.** 실행 이력 행이 정한다 — 화면이든
      걷기든 아무 날이나 넣을 수 없다.
    """
    _실행을_세운다(monkeypatch, _run_row())

    decision_service.record_decision(REQ, _승인())

    잰_날 = {as_of for 부 in 부서들.values() for (_a, _m, as_of, _r) in 부.호출}
    assert 잰_날 == {원_실행일}, f"그 실행의 날로 안 돌았다: {잰_날}"
    assert 오늘 not in 잰_날, "벽시계로 재검증을 돌렸다"


def test_새_업무_키로_돌고_그_키가_결정_행에_실린다(monkeypatch, 이력, 부서들):
    """★ `revalidation_request_id` 는 **재검증이 돈 실행**을 가리킨다.

    🔴 원 실행 키와 같으면 그 값이 무엇을 가리키는지 아무도 못 푼다 — 그래서 접두가
      `REQ` 가 아니라 `REV` 다.
    """
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    쓴_키 = {rid for 부 in 부서들.values() for (_a, _m, _d, rid) in 부.호출}
    assert saved.revalidation_request_id is not None
    assert saved.revalidation_request_id != REQ
    assert 쓴_키 == {saved.revalidation_request_id}
    assert saved.revalidation_request_id == revalidation.make_revalidation_request_id(원_실행일, 1)


def test_번복마다_다른_키를_받는다():
    """★ 같은 날 두 번째 승인이 같은 키를 받으면 앞 재검증을 덮어 가리킨다."""
    오늘 = date(2026, 9, 7)

    assert revalidation.make_revalidation_request_id(
        오늘, 1
    ) != revalidation.make_revalidation_request_id(오늘, 2)


# ---------------------------------------------------------------------------
# ③ 결과 매핑 — 🔴 CONDITIONAL 이 제일 틀리기 쉽다
# ---------------------------------------------------------------------------


def test_조건이_없으면_PASSED(monkeypatch, 이력, 부서들):
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "PASSED"


def test_원래도_조건부였고_같은_조건이면_PASSED(monkeypatch, 이력, 부서들):
    """🔴 **이 검사가 M-4 에서 제일 중요하다.**

    원 실행에 붙어 있던 조건이 그대로 다시 나온 것은 **새 조건이 아니다.**
    `business_status == "conditional"` 만 보고 접으면 여기가 `CONDITIONAL` 이 되고,
    사용자는 자기가 이미 본 조건을 영원히 다시 승인하게 된다.
    """
    부서들["finance"].business_status = "conditional"
    부서들["finance"].adjustments = (조정(),)
    _실행을_세운다(
        monkeypatch,
        _run_row(adjustments=(조정(),), candidates=(_후보_판정("conditional"),)),
    )

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "PASSED", 이력.인자[-1]


def test_조건이_늘면_CONDITIONAL(monkeypatch, 이력, 부서들):
    """🔴 **통과 쪽으로 기울면 사용자가 본 적 없는 조건이 사용자 승인으로 기록된다.**"""
    부서들["finance"].business_status = "conditional"
    부서들["finance"].adjustments = (조정(), 조정(target_value=12000000.0, reason="추가 한도"))
    _실행을_세운다(
        monkeypatch,
        _run_row(adjustments=(조정(),), candidates=(_후보_판정("conditional"),)),
    )

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "CONDITIONAL"


def test_조건이_줄면_PASSED(monkeypatch, 이력, 부서들):
    """★ 기준은 **"사용자가 본 것보다 나빠졌는가"** 다. 줄어든 것은 안 나빠졌다."""
    부서들["finance"].business_status = "conditional"
    부서들["finance"].adjustments = (조정(),)
    _실행을_세운다(
        monkeypatch,
        _run_row(
            adjustments=(조정(), 조정(target_value=12000000.0, reason="추가 한도")),
            candidates=(_후보_판정("conditional"),),
        ),
    )

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "PASSED"


def test_필수가_reject_면_FAILED_이고_CONDITIONAL_이_아니다(monkeypatch, 이력, 부서들):
    부서들["inventory"].business_status = "reject"
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "FAILED"


def test_판정을_안_낸_것도_통과가_아니다(monkeypatch, 이력, 부서들):
    """★ 통과는 **허용목록**(`PASSING_VERDICTS`)으로 정한다. *"reject 가 아니면 통과"*
    로 세면 어휘가 늘 때마다 새 값이 통과 쪽으로 샌다 (#173)."""
    부서들["finance"].business_status = "skipped"
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "FAILED"


def _후보_판정(business_status: str) -> dict[str, Any]:
    """판매 응답 모양의 후보 하나. **그 안이 그때 어떤 판정을 받았나.**"""
    return {
        "scenario": {"scenario_id": LABEL},
        "validations": {"FINANCIAL_VALIDATION": {"business_status": business_status}},
    }


# ---------------------------------------------------------------------------
# ④ ERROR — 재검증이 **실패한** 것이 아니라 **돌리지 못한** 것
# ---------------------------------------------------------------------------


def test_안_열린_날은_ERROR_이고_FAILED_가_아니다(monkeypatch, 이력, 부서들):
    """🔴 **막힌 것과 못 돌린 것은 다르다.**

    `FAILED` 로 적으면 사람이 *"조건이 안 맞았구나"* 로 읽고 안을 바꾸는데, 실제로는
    **그 날을 열 일**이다.
    """
    monkeypatch.setattr(
        revalidation,
        "check_day_gate",
        lambda as_of, **kw: DayGate(
            as_of=as_of, gate="BLOCKED", result="NOT_OPENED", reason="안 열렸다"
        ),
    )
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "ERROR"
    assert [부.호출 for 부 in 부서들.values()] == [[], []], "안 열린 날인데 부서를 불렀다"


def test_못_돌린_재검증에는_가짜_업무_키를_안_넣는다(monkeypatch, 이력, 부서들):
    """🔴 짝 CHECK(`master_decisions_revalidation_pairing`)가 `ERROR` 만 키 없이
    허용한다. 지어 넣으면 *"실행이 있었다"* 가 사실이 아닌 채로 남는다."""
    monkeypatch.setattr(
        revalidation,
        "check_day_gate",
        lambda as_of, **kw: DayGate(as_of=as_of, gate="BLOCKED", result="NEVER_OPENED"),
    )
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_request_id is None


def test_필수_어댑터가_없으면_ERROR(monkeypatch, 이력):
    """★ 미등록은 오류가 아니라 상태다 (§5.3) — 다만 **재검증을 못 돌린 상태**다."""
    wiring.reset()
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "ERROR"


def test_예산이_소진되면_ERROR(monkeypatch, 이력, 부서들):
    """🔴 **"다 봤는데 안 된다" 와 "다 못 봤다" 는 다르다.** 판매가 `SL5` 를 `SL3` 으로
    안 접는 것과 같은 판단이다."""
    monkeypatch.setattr(revalidation, "REVALIDATION_BUDGET", 1)
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "ERROR"


def test_정책판을_못_읽으면_ERROR(monkeypatch, 이력, 부서들):
    """★ 아무 값이나 채워 봉투를 만들면 재현 4종의 하나가 거짓이 된다 (§3.2.4)."""
    _실행을_세운다(monkeypatch, _run_row(policy_version=None))

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "ERROR"
    assert [부.호출 for 부 in 부서들.values()] == [[], []]


def test_라벨이_겹치면_ERROR(monkeypatch, 이력, 부서들):
    """🔴 첫 것을 조용히 고르면 **어느 안을 재검증했는지가 운에 걸린다.**"""
    _실행을_세운다(monkeypatch, _run_row(labels=(LABEL, LABEL)))

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "ERROR"


# ---------------------------------------------------------------------------
# ⑤ 🔴 결정 행은 **항상** 쓴다 (설계 §0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("business_status,기대", [("reject", "FAILED"), ("ok", "PASSED")])
def test_재검증_결과와_무관하게_결정_행이_남는다(monkeypatch, 이력, 부서들, business_status, 기대):
    """🔴 **"기록 안 함" 과 "기록한다" 는 다른 것이다.**

    ```text
    결정 행       항상 쓴다        decision=APPROVE 는 사용자가 누른 사실이다
    승인의 효력   PASSED 일 때만    도메인 Write 로 흘러가는 것 (M-5)
    ```

    막혔다고 행을 안 쓰면 *"승인하려다 막혔다"* 가 사라진다 — M-3 이 칸을 나눈 이유를
    되돌리는 것이다.
    """
    부서들["finance"].business_status = business_status
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert len(이력.rows) == 1
    assert saved.decision == "APPROVE", "재검증 결과가 decision 칸으로 샜다"
    assert saved.revalidation_outcome == 기대


def test_못_돌린_날에도_결정_행이_남는다(monkeypatch, 이력, 부서들):
    """★ 위와 같은 이유. `ERROR` 는 **가장 흔한 '행이 사라지는' 자리**다."""
    monkeypatch.setattr(
        revalidation,
        "check_day_gate",
        lambda as_of, **kw: DayGate(as_of=as_of, gate="BLOCKED", result="NOT_OPENED"),
    )
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert len(이력.rows) == 1
    assert saved.decision == "APPROVE"


# ---------------------------------------------------------------------------
# ⑥ 승인이 아닌 결정은 재검증할 대상이 없다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "REJECT_ALL", "decided_by": "이현서"},
        {"decision": "REQUEST_CHANGE", "condition_text": "더 싸게", "decided_by": "이현서"},
        {"decision": "CANCEL", "decided_by": "이현서"},
    ],
    ids=["REJECT_ALL", "REQUEST_CHANGE", "CANCEL"],
)
def test_승인이_아니면_재검증하지_않는다(monkeypatch, 이력, 부서들, payload):
    """★ 그때 두 칸의 `None` 은 *"재검증에 실패했다"* 가 아니라 **"재검증을 하지
    않았다"** 이다 — 재검증할 대상이 없다."""
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, DecisionIn(**payload))

    assert saved.revalidation_outcome is None
    assert saved.revalidation_request_id is None
    assert [부.호출 for 부 in 부서들.values()] == [[], []], "승인이 아닌데 부서를 불렀다"


# ---------------------------------------------------------------------------
# ⑦ 못 부른 요구 · 이력
# ---------------------------------------------------------------------------


def test_라우팅이_없는_조건부는_통과를_막지_않는다(monkeypatch, 이력, 부서들):
    """★ `ADDITIONAL_SUPPLY_CONTEXT` 는 라우팅이 `None` 이라 **원 실행에서도 똑같이 못
    불렀다** — 그 사이 나빠진 것이 아니다 (설계 §5). 못 물어봤다는 사실은 결과에
    남지만 `FAILED` 로 접지 않는다."""
    _실행을_세운다(monkeypatch, _run_row(required=("ADDITIONAL_SUPPLY_CONTEXT",)))

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "PASSED"


def test_재검증도_이력에_남는다(monkeypatch, 이력, 부서들):
    """🔴 **예산을 쓰고 부서를 부르는 호출은 이력에 남는다** (M-16 · 설계 §4).

    ★ `try_save_run` 을 가로채 *"무엇을 넣으려 했는가"* 만 본다 — pytest 안에서는
      `history_enabled()` 가 False 라 어차피 안 나가지만, 그건 **안 넣었다는 뜻이
      아니다.**
    """
    잡힌: list[dict[str, Any]] = []
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: 잡힌.append(kw))
    _실행을_세운다(monkeypatch, _run_row())

    saved = decision_service.record_decision(REQ, _승인())

    assert len(잡힌) == 1
    assert 잡힌[0]["cycle"] == "SALES"
    assert 잡힌[0]["request_id"] == saved.revalidation_request_id
    assert 잡힌[0]["as_of"] == 원_실행일
    assert 잡힌[0]["end_code"] == "PASSED"
    assert 잡힌[0]["runtime_status"] == "READY"


def test_못_돌린_재검증은_이력_행도_안_만든다(monkeypatch, 이력, 부서들):
    """★ 부른 것이 없으면 남길 실행도 없다 — `revalidation_request_id` 가 `None` 인
    것과 같은 사실이다."""
    잡힌: list[dict[str, Any]] = []
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: 잡힌.append(kw))
    monkeypatch.setattr(
        revalidation,
        "check_day_gate",
        lambda as_of, **kw: DayGate(as_of=as_of, gate="BLOCKED", result="NOT_OPENED"),
    )
    _실행을_세운다(monkeypatch, _run_row())

    decision_service.record_decision(REQ, _승인())

    assert 잡힌 == []


# ---------------------------------------------------------------------------
# ⑧ 조건 비교 단위 — 무엇을 같다고 보는가
# ---------------------------------------------------------------------------


def test_ok_는_조건이_아니다():
    """★ 판정은 닫힌 어휘 하나로 비교한다 — `ok` 만 *"조건 없음"* 이다."""
    assert revalidation.conditions_of({"FINANCIAL_VALIDATION": {"business_status": "ok"}}, ()) == (
        frozenset()
    )


def test_conditional_은_capability_이름까지_담는다():
    """★ 어느 검증이 조건을 걸었는지가 표지에 있어야 *"다른 검증이 조건을 걸었다"* 를
    새 조건으로 읽는다."""
    표지 = revalidation.conditions_of(
        {"FINANCIAL_VALIDATION": {"business_status": "conditional"}}, ()
    )

    assert 표지 == frozenset({"verdict:FINANCIAL_VALIDATION=conditional"})


def test_조정은_표준형_전체가_표지다():
    """🔴 **마스터가 표준형 안에서 무엇이 중요한지 고르지 않는다** (§3.2.2).

    같은 숫자에 다른 문장이면 **조건이 바뀐 것으로 본다** — 사용자가 화면에서 읽는
    것이 그 문장이기 때문이다. 보수적으로 기운 자리이고, 그 방향이 맞다.
    """
    앞 = revalidation.conditions_of({}, (조정(reason="한도 초과"),))
    뒤 = revalidation.conditions_of({}, (조정(reason="한도가 초과되었습니다"),))

    assert 앞 != 뒤


def test_라벨을_안_밝힌_조정은_그_안의_조건으로_세지_않는다():
    """🔴 **뜻이 둘인 값을 있는 쪽으로 읽으면 원 조건 집합이 커지고, 커진 만큼 결과가
    `PASSED` 로 접힌다.** 그 방향으로는 기울이지 않는다.

    `scenario_labels` 는 *"비어 있어도 된다 — 안 채운 것과 해당 없는 것을 여기서
    가르지 않는다"* (`SuggestedAdjustment`).
    """
    라벨_없음 = {**조정(), "scenario_labels": []}

    assert (
        revalidation.conditions_of_original(
            {"adjustments": [라벨_없음]},
            LABEL,
        )
        == frozenset()
    )


def test_원_실행_후보_판정을_못_읽으면_빈_집합이다():
    """★ *"모르면 통과"* 가 아니라 *"모르면 되돌린다"* 로 둔다 — 빈 집합이면 재검증에
    조건이 하나라도 붙는 순간 `CONDITIONAL` 이다."""
    매입_응답 = {"scenarios": [{"label": LABEL}]}

    assert revalidation.conditions_of_original(매입_응답, LABEL) == frozenset()


def test_판매_후보_판정도_읽는다():
    """★ 판매 응답은 `candidates[].scenario.scenario_id` 로 안을 가리킨다 — 승인
    요청이 실어 보내는 칸은 하나(`scenario_label`)라 받는 쪽이 두 이름을 다 안다."""
    판매_응답 = {"candidates": [_후보_판정("conditional")]}

    assert revalidation.conditions_of_original(판매_응답, LABEL) == frozenset(
        {"verdict:FINANCIAL_VALIDATION=conditional"}
    )
