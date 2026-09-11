"""판매 사용자 승인 — **판매안을 승인할 방법이 생겼다** (2026-09-08 계약).

지금까지 판매안은 만들어졌는데 **승인할 방법이 없었다.**

```text
_APPROVE_END_CODES = frozenset({"E1_APPROVED"})     ← 매입 코드 하나뿐
판매 실행의 종료 코드는 SL1_PRESENTED               ← 그 집합에 없다 → 승인 거절
```

🔴 **이 파일이 잡으려는 여섯.**

```text
① 한 어휘에 합치기      E1 집합에 SL1 을 넣으면 매입 실행에 SL1 을 우겨도 통과한다
② 요청 본문으로 분기     매입 실행을 판매라고 우겨 승인 게이트를 우회한다
③ 없는 값 지어내기      delivery_date 없이 통과시키면 아무도 안 정한 날짜가 장부에 선다
④ 벽시계                 order_date 를 오늘로 읽으면 수금 곡선의 시점이 틀린다
⑤ 재검증 무시            막혔는데 confirm_sale 을 부르면 승인 게이트가 없는 것과 같다
⑥ 후보 판정의 필수값 삭제  값 없는 안이 화면에 올라 사용자가 고른 뒤에야 막힌다
```

★ **DB 를 치지 않는다.** 저장소는 in-memory 로 갈아 끼우고, `confirm_sale` 은
  대역으로 바꿔 *"불렸는가 · 무엇을 들고 불렸는가"* 만 본다 (`test_final_revalidation.py`
  와 같은 결).

🔴 **자기 생존 검사가 아래 ⑦에 있다.** 이 파일의 검사들이 **0건을 세고 초록이 되는
  것**을 막는다 — 실행이 아예 안 서면 `confirm_sale` 도 0회이고, 그러면 *"안 불렀다"*
  를 재는 검사가 전부 공짜로 통과한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.master import decision_service, sales_approval, wiring
from app.master.decision import (
    _APPROVE_END_CODES,
    _SALES_APPROVE_END_CODES,
    DecisionIn,
    DecisionOut,
    DecisionRejected,
    approve_end_codes,
    check_decidable,
    mark_current,
    scenario_ids_of,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.sales_flow import CandidateVerdict

REQ = "REQ-20260910-0001"
RUN_UUID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

#: 실측에 있던 판매 후보의 id (`master_agent_runs` REQ-20260910-0001 · SL1_PRESENTED).
#: 🔴 **label 이 없다** — 그래서 `DecisionIn.scenario_label` 에 이 값을 싣는다.
SCN = "SALES-001-A-R1"

#: 원 실행이 돈 날. 🔴 **`order_date` 가 이 날이어야 한다** — 벽시계가 아니다.
#:
#: 🔴 **오늘일 수 없는 날이어야 한다** (2026-09-10 에 터졌다). 전에는 `2026-09-10`
#:   이었는데 그날이 오자 *"벽시계를 읽었다면 오늘이 실린다"* 는 검사가 자기 픽스처와
#:   오늘을 구별하지 못해 빨개졌다 — **주석에 그 함정을 적어 두고 그 날짜를 골랐다.**
#:
#:   지금 값은 걷기 구간 안이고(`2026-01`) 이미 지난 날이라 다시 오늘이 될 수 없다.
원_실행일 = date(2026, 1, 30)

#: scenario 가 말한 납품일. 🔴 **`sale_date` 가 이 날이어야 한다.**
#: 원 실행일보다 뒤여야 한다 — 같이 옮긴다.
납품일 = date(2026, 2, 6)

#: 실행 이력 행이 실은 축. 🔴 **재검증이 이 값을 읽는다** (2026-09-11) — 없으면
#: `_revalidation_for` 가 `ERROR` 를 내고 확정까지 안 간다.
실행축 = "SIM-SALESCHAIN-20260911"

#: 두 번째 실행. 🔴 **하나로는 "넘긴 값이 실렸다" 와 "상수가 마침 그 값이다" 를
#:   못 가른다.**
다른_실행축 = "SIM-WALK-2026-V4"


# ---------------------------------------------------------------------------
# 대역 — 부서 · 저장소 · confirm_sale
# ---------------------------------------------------------------------------


class 부서:
    """등록된 어댑터 대역. 재검증이 부르는 자리다."""

    def __init__(self, business_status: str = "ok") -> None:
        self.business_status = business_status
        self.호출: list[tuple[str, str, date]] = []

        #: 재검증 봉투가 들고 온 실행 축. 🔴 **`호출` 과 따로 둔다** — 저 튜플을
        #: 넓히면 이미 셋으로 푸는 자리들이 같이 깨진다.
        self.축: list[str] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.호출.append((request.agent, request.mode, request.context.as_of))
        self.축.append(request.context.sim_run_id)
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status=self.business_status,
            reasoning="대역",
        )
        return reply, ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("tool_a",),
            tool_order=(1,),
        )


@pytest.fixture
def 부서들() -> dict[str, 부서]:
    wiring.reset()  # 루트 conftest 가 스냅샷을 떠 두므로 이 테스트 밖으로 안 샌다
    등록 = {"inventory": 부서(), "finance": 부서()}
    for 이름, 포트 in 등록.items():
        wiring.register(이름, 포트)
    return 등록


class 결정_저장소:
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


class 확정_대역:
    """`confirm_sale` 대역. **불렸는가 · 무엇을 들고 불렸는가**만 남긴다."""

    def __init__(self) -> None:
        self.호출: list[Any] = []

    def __call__(self, conn: Any, request: Any) -> Any:
        self.호출.append(request)

        class _결과:
            sale_id = "SALE-1"
            sale_item_id = "SI-SALE-1-1"

        return _결과()


class 커넥션_대역:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def 확정(monkeypatch) -> 확정_대역:
    """`confirm_approved_sale` 이 실제로 여는 두 자리를 대역으로 바꾼다."""
    대역 = 확정_대역()
    conn = 커넥션_대역()
    monkeypatch.setattr(sales_approval, "confirm_sale", 대역)
    monkeypatch.setattr(sales_approval, "_open", lambda connect: conn)
    대역.conn = conn  # type: ignore[attr-defined]
    return 대역


def _scenario(**덮어쓰기: Any) -> dict[str, Any]:
    """판매 후보 하나 — `SalesScenario` 가 받는 모양 그대로 (`extra="forbid"`)."""
    base: dict[str, Any] = {
        "scenario_id": SCN,
        "scenario_type": "BALANCED",
        "objective": "BALANCE",
        "business_mode": "SPOT_SALES",
        "item": "배추",
        "partner_id": "P-1",
        "quantity_kg": "2000",
        "unit_price_krw": "1200",
        "sales_amount_krw": "2400000",
        "delivery_date": 납품일.isoformat(),
        "payment_days": 30,
        "payment_terms_type": "SINGLE",
        "contribution_margin_krw": "400000",
        "supply": {"confirmed_quantity_kg": "2000"},
        "required_validations": [],
    }
    base.update(덮어쓰기)
    return {k: v for k, v in base.items() if v is not _없음}


class _없음:
    """`_scenario(delivery_date=_없음)` — 칸 자체를 빼는 표시."""


def _판매_실행(
    *,
    end_code: str = "SL1_PRESENTED",
    cycle: str = "SALES",
    candidates: tuple[dict[str, Any], ...] | None = None,
    sim_run_id: str = 실행축,
) -> dict[str, Any]:
    후보 = candidates if candidates is not None else ({"scenario": _scenario()},)
    return {
        "run_id": RUN_UUID,
        "request_id": REQ,
        "cycle": cycle,
        "item": "배추",
        "as_of": 원_실행일,
        "sim_run_id": sim_run_id,
        "request_payload": {"policy_version": "v1.3", "item": "배추"},
        "response_payload": {
            "end_code": end_code,
            "as_of": 원_실행일.isoformat(),
            "candidates": list(후보),
            "adjustments": [],
        },
    }


def _매입_실행(*, end_code: str = "E1_APPROVED") -> dict[str, Any]:
    return {
        "run_id": RUN_UUID,
        "request_id": REQ,
        "cycle": "PROCUREMENT",
        "item": "배추",
        "as_of": 원_실행일,
        "sim_run_id": 실행축,
        "request_payload": {"policy_version": "v1.3", "item": "배추"},
        "response_payload": {
            "end_code": end_code,
            "as_of": 원_실행일.isoformat(),
            "scenarios": [{"label": "기본", "qty_kg": 1000.0, "unit_price": 1200.0}],
            "adjustments": [],
        },
    }


@pytest.fixture
def 이력(monkeypatch) -> 결정_저장소:
    저장소 = 결정_저장소()
    monkeypatch.setattr(decision_service, "list_decisions", 저장소.list_decisions)
    monkeypatch.setattr(decision_service, "save_decision", 저장소.save_decision)
    return 저장소


def _실행을_세운다(monkeypatch, row: Mapping[str, Any]) -> None:
    monkeypatch.setattr(
        decision_service, "get_run_by_request_id", lambda request_id, **kw: dict(row)
    )
    monkeypatch.setattr(decision_service, "get_run", lambda run_id: dict(row))


def _승인(label: str = SCN, **kw: Any) -> DecisionIn:
    base: dict[str, Any] = {
        "decision": "APPROVE",
        "scenario_label": label,
        "decided_by": "이현서",
    }
    base.update(kw)
    return DecisionIn(**base)


# ---------------------------------------------------------------------------
# ① 어휘 — 둘이 따로 선다
# ---------------------------------------------------------------------------


def test_판매_승인_어휘와_매입_승인_어휘가_따로다():
    """🔴 **이 파일에서 가장 중요한 검사다** (D-3 합의 · `sales_flow.SalesEndCode`).

    > 매입 `EndCode`(E1~E5) 에 값을 더하지 않는다. 층이 다르다. 한 어휘에 두 사이클을
    > 담으면 `E2_HELD` 가 *"매입 보류"* 와 *"판매 보류"* 를 동시에 뜻하게 된다.

    ⚠️ 두 집합을 합치면 아래 `test_매입_실행에_SL1_을_우겨도_안_통한다` 가 같이
      빨개진다 — 이 검사는 **왜** 를, 저 검사는 **무엇이 새는지**를 잰다.
    """
    assert _APPROVE_END_CODES == frozenset({"E1_APPROVED"})
    assert _SALES_APPROVE_END_CODES == frozenset({"SL1_PRESENTED"})
    assert not (_APPROVE_END_CODES & _SALES_APPROVE_END_CODES), (
        "두 승인 어휘가 겹친다 — 한 집합에 두 사이클을 담으면 코드가 어느 층의 것인지를 "
        "집합이 더 이상 구분하지 못한다"
    )


def test_SL1_은_매입_승인_어휘에_없다():
    """★ 위 검사의 짝 — **집합의 내용**이 아니라 **한 값의 소속**을 잰다."""
    assert "SL1_PRESENTED" not in _APPROVE_END_CODES
    assert "E1_APPROVED" not in _SALES_APPROVE_END_CODES


def test_어느_어휘를_볼지는_cycle_이_정한다():
    assert approve_end_codes("SALES") == _SALES_APPROVE_END_CODES
    assert approve_end_codes("PROCUREMENT") == _APPROVE_END_CODES
    # ★ 모르는 사이클은 **매입 쪽**이다 — 판매 어휘가 기본이 되면 안 밝힌 실행에서
    #   SL1 이 통과한다.
    assert approve_end_codes("") == _APPROVE_END_CODES


# ---------------------------------------------------------------------------
# ② cycle 이 정한다 — 요청 본문이 아니다
# ---------------------------------------------------------------------------


def test_SL1_실행에_승인이_통과한다(monkeypatch, 이력, 부서들, 확정):
    """🔴 **이것이 이 판의 목적이다.** 전에는 여기서 409 가 났다."""
    _실행을_세운다(monkeypatch, _판매_실행())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.decision == "APPROVE"
    assert saved.end_code_at_decision == "SL1_PRESENTED"
    assert saved.sale is not None, "판매 승인인데 확정 결과 칸이 비었다"
    assert saved.sale.status == "CONFIRMED", saved.sale.reason


def test_매입_실행에_SL1_을_우겨도_안_통한다(monkeypatch, 이력, 부서들, 확정):
    """🔴 **어휘를 합치면 여기가 빨개진다.**

    `cycle="PROCUREMENT"` 인 실행의 종료 코드가 `SL1_PRESENTED` 라고 적혀 있어도
    매입 어휘로 검사한다 — 승인할 안이 없다.
    """
    행 = dict(_매입_실행())
    행["response_payload"] = {**행["response_payload"], "end_code": "SL1_PRESENTED"}
    _실행을_세운다(monkeypatch, 행)

    with pytest.raises(DecisionRejected):
        decision_service.record_decision(REQ, _승인("기본"))

    assert 확정.호출 == [], "매입 실행에서 판매 확정이 불렸다"


def test_판매_실행에_E1_을_우겨도_안_통한다(monkeypatch, 이력, 부서들, 확정):
    """★ 반대 방향도 막힌다 — 한 어휘였다면 이쪽도 샌다."""
    _실행을_세운다(monkeypatch, _판매_실행(end_code="E1_APPROVED"))

    with pytest.raises(DecisionRejected):
        decision_service.record_decision(REQ, _승인())

    assert 확정.호출 == []


def test_요청_본문으로는_사이클을_못_바꾼다():
    """🔴 **`DecisionIn` 에 사이클 칸이 없다 — 일부러 없다.**

    있으면 매입 실행에 `SALES` 를 실어 보내 `SL1_PRESENTED` 어휘로 검사받을 수 있고,
    그 순간 승인 게이트가 부르는 쪽 손에 들어간다. `extra="forbid"` 이므로 비슷한
    이름을 얹는 것도 문 앞에서 막힌다.
    """
    칸 = set(DecisionIn.model_fields)
    assert not {"cycle", "sales", "is_sales", "end_code"} & 칸, (
        f"요청 본문이 사이클을 정할 수 있는 칸이 생겼다: {sorted(칸)}"
    )
    with pytest.raises(ValueError):
        DecisionIn(decision="APPROVE", scenario_label=SCN, decided_by="이현서", cycle="SALES")


def test_check_decidable_은_cycle_없이는_안_불린다():
    """⚠️ **기본값을 두지 않는다.** 안 주면 터져야 한다 — 기본값은 곧 업무 규칙이고,
    여기서는 *"안 밝히면 매입으로 본다"* 가 조용한 규칙이 된다."""
    with pytest.raises(TypeError):
        check_decidable("SL1_PRESENTED", "APPROVE")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ③ scenario_id 로 안을 찾는다
# ---------------------------------------------------------------------------


def test_판매_후보는_scenario_id_로_열거된다():
    """🔴 **판매 후보에는 label 이 없다.** `scenarios[].label` 을 읽으면 목록이 비고,
    그러면 어느 id 를 실어도 *"제시된 안: (없음)"* 으로 422 다."""
    payload = _판매_실행()["response_payload"]

    assert scenario_ids_of(payload) == (SCN,)


def test_없는_scenario_id_는_거절된다(monkeypatch, 이력, 부서들, 확정):
    _실행을_세운다(monkeypatch, _판매_실행())

    with pytest.raises(DecisionRejected, match="내놓은 안이 아니다"):
        decision_service.record_decision(REQ, _승인("SALES-999-Z-R9"))

    assert 확정.호출 == []


# ---------------------------------------------------------------------------
# ④ 재검증 — 그 실행의 as_of 로 돌고, 통과해야만 확정한다
# ---------------------------------------------------------------------------


def test_재검증은_그_실행의_날로_돈다(monkeypatch, 이력, 부서들, 확정):
    """🔴 `#455` 계약 — 재검증이 서는 날은 **실행 이력 행이 정한다.** 벽시계가 아니다."""
    _실행을_세운다(monkeypatch, _판매_실행())

    decision_service.record_decision(REQ, _승인())

    쓴_날 = {as_of for 부 in 부서들.values() for (_a, _m, as_of) in 부.호출}
    assert 쓴_날 == {원_실행일}, f"재검증이 원 실행의 날로 안 돌았다: {쓴_날}"


def test_재검증이_통과해야만_확정한다(monkeypatch, 이력, 부서들, 확정):
    _실행을_세운다(monkeypatch, _판매_실행())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "PASSED"
    assert len(확정.호출) == 1, "재검증이 통과했는데 confirm_sale 이 안 불렸다"


def test_재검증이_막히면_확정하지_않는다(monkeypatch, 이력, 부서들, 확정):
    """🔴 **⑤ 를 잡는 자리.** 막혔는데 부르면 승인 게이트가 없는 것과 같다.

    ★ 그래도 **결정 행은 쓴다** (설계 §0) — *"승인하려다 막혔다"* 가 사라지면 안 된다.
    """
    부서들["finance"].business_status = "reject"
    _실행을_세운다(monkeypatch, _판매_실행())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "FAILED"
    assert 확정.호출 == [], "재검증이 막혔는데 confirm_sale 이 불렸다"
    assert saved.sale is not None and saved.sale.status == "BLOCKED"
    assert 이력.rows, "재검증이 막혔다고 결정 행까지 안 썼다"


def test_CONDITIONAL_도_통과가_아니다(monkeypatch):
    """★ 사용자가 승인한 대상은 **그때 화면에 있던 그 안**이다. 새 조건이 붙으면
    그것은 다른 안이라, `PASSED` 로 접으면 사용자가 본 적 없는 조건이 승인으로 남는다.
    """
    대역 = 확정_대역()
    결과 = sales_approval.confirm_approved_sale(
        request_id=REQ,
        run_id=str(RUN_UUID),
        as_of=원_실행일,
        policy_version="v1.3",
        scenario=_scenario(),
        revalidation_outcome="CONDITIONAL",
        sim_run_id=실행축,
        confirm=대역,
        connect=커넥션_대역,
    )

    assert 결과.status == "BLOCKED"
    assert 대역.호출 == []


# ---------------------------------------------------------------------------
# ⑤ 없는 값을 지어내지 않는다
# ---------------------------------------------------------------------------


def _확정(scenario: Mapping[str, Any], 대역: 확정_대역 | None = None):
    return sales_approval.confirm_approved_sale(
        request_id=REQ,
        run_id=str(RUN_UUID),
        as_of=원_실행일,
        policy_version="v1.3",
        scenario=scenario,
        revalidation_outcome="PASSED",
        sim_run_id=실행축,
        confirm=대역 or 확정_대역(),
        connect=커넥션_대역,
    )


def test_delivery_date_가_없으면_BLOCKED_이고_이름을_부른다():
    """🔴 **③ 을 잡는 자리.** 실측에서 후보 3안이 전부 `delivery_date=None` 이었다.

    ⚠️ `as_of + N` 같은 값을 만들면 **그 `N` 이 곧 업무 규칙**이 되고, 그 날짜가
      출고일·수금일·곡선의 시점이 된다 — 아무도 정한 적이 없는데.
    """
    대역 = 확정_대역()
    결과 = _확정(_scenario(delivery_date=None), 대역)

    assert 결과.status == "BLOCKED"
    # ★ 이 경로는 `user_request` 를 안 들고 온다 — 호출에 안 실린 것이 사실이다.
    assert 결과.missing_terms == ["REQUEST_MISSING_delivery_date"]
    assert "납품일" in 결과.reason and "delivery_date" in 결과.reason, (
        f"무엇이 없는지 이름을 안 부른다: {결과.reason}"
    )
    assert 대역.호출 == []


def test_payment_days_가_없으면_BLOCKED():
    대역 = 확정_대역()
    결과 = _확정(_scenario(payment_days=None), 대역)

    assert 결과.status == "BLOCKED"
    assert 결과.missing_terms == ["REQUEST_MISSING_payment_days"]
    assert "수금 유예일" in 결과.reason and "payment_days" in 결과.reason
    assert 대역.호출 == []


def test_칸이_아예_없어도_같다():
    """★ `None` 과 *"칸이 없다"* 를 갈라 한쪽만 막으면, 다른 쪽으로 샌다."""
    결과 = _확정(_scenario(delivery_date=_없음, payment_days=_없음))

    assert 결과.status == "BLOCKED"
    assert 결과.missing_terms == [
        "REQUEST_MISSING_delivery_date",
        "REQUEST_MISSING_payment_days",
    ]


def test_payment_days_0_은_없는_값이_아니다():
    """🔴 **`0` 은 정해진 조건이다** — *"당일 수금"*. `falsy` 로 세면 그 안이 조용히
    *"조건이 없는 안"* 이 된다."""
    assert sales_approval.missing_commercial_terms(_scenario(payment_days=0)) == ()


# ---------------------------------------------------------------------------
# ⑥ 무엇을 들고 confirm_sale 을 부르는가
# ---------------------------------------------------------------------------


def test_order_date_는_그_실행의_as_of_다(monkeypatch, 이력, 부서들, 확정):
    """🔴 **④ 를 잡는 자리.** 벽시계로 읽으면 `today()` 가 오늘이라 이 검사가 빨개진다.

    ⚠️ `order_date` 는 수금 곡선의 시점이 된다 — 오늘로 읽으면 백테스트가 무효가 된다.
    """
    _실행을_세운다(monkeypatch, _판매_실행())

    decision_service.record_decision(REQ, _승인())

    보낸것 = 확정.호출[0]
    assert 보낸것.order_date == 원_실행일, (
        f"order_date 가 그 실행의 as_of 가 아니다: {보낸것.order_date} != {원_실행일}"
    )
    # ★ 벽시계를 읽었다면 **오늘**이 실린다. 그 절이 성립하려면 픽스처가 오늘이
    #   아니어야 하는데, 전에는 그 전제를 **말 없이 기대만** 했다가 그날이 오자
    #   *"벽시계를 읽었다"* 라는 없는 사유로 빨개졌다 (2026-09-10).
    #
    # 🔴 전제를 먼저 말한다 — 못 가르는 날에는 **못 가른다고** 말한다.
    오늘 = datetime.now(UTC).date()
    assert 원_실행일 != 오늘, (
        f"픽스처 날짜가 오늘({오늘})과 같아 이 검사가 벽시계를 못 가른다 — "
        f"원_실행일 을 지난 날로 옮겨라. 이 줄이 빨간 것은 코드 잘못이 아니다"
    )
    assert 보낸것.order_date != 오늘, "벽시계를 읽었다"


def test_sale_date_는_scenario_의_납품일이다(monkeypatch, 이력, 부서들, 확정):
    """★ 납품일 정본은 `sales.sale_date` 이고 그 출처는 **그 안이 말한 날**이다."""
    _실행을_세운다(monkeypatch, _판매_실행())

    decision_service.record_decision(REQ, _승인())

    assert 확정.호출[0].sale_date == 납품일


def test_sim_run_id_는_마스터가_정한다(monkeypatch, 부서들, 확정):
    """★ 어느 실행의 장부인가는 마스터가 정한다 — 남의 조회를 베끼지 않는다.

    🔴 **이 검사가 버그를 지키고 있었다** (2026-09-11). 문서화 문자열의 뜻은 맞았는데
      단언이 `== BURN_IN_SIM_RUN_ID` 였다 — *"마스터가 정한다"* 가 *"상수다"* 로
      굳어 있었다. 이름은 그대로 두고 단언을 바꾼다.

    ★★ **값만 비교하면 아무것도 안 막는다** (`#579` 에서 덴 자리). 그래서 두 실행으로
      각각 재고, 그 값이 **같은 승인의 재검증 봉투와 같은 값**인지까지 본다 —
      둘이 갈리면 재검증은 이 실행을 보고 확정은 다른 실행의 `sales` 에 쓴다.
    """
    for 축 in (실행축, 다른_실행축):
        # ⚠️ **결정 이력을 매번 새로 깐다.** 한 저장소로 두 번 승인하면
        #   `_reject_repeat_approval` 이 막아 둘째 판이 아예 안 돈다 — 여기서 재는
        #   것은 재승인 규칙이 아니라 축이다.
        저장소 = 결정_저장소()
        monkeypatch.setattr(decision_service, "list_decisions", 저장소.list_decisions)
        monkeypatch.setattr(decision_service, "save_decision", 저장소.save_decision)
        _실행을_세운다(monkeypatch, _판매_실행(sim_run_id=축))
        decision_service.record_decision(REQ, _승인())

    확정_축 = [보낸것.sim_run_id for 보낸것 in 확정.호출]
    assert 확정_축 == [실행축, 다른_실행축], f"실행 행의 축이 확정에 안 실렸다: {확정_축}"

    # 🔴 **이 단언이 핵심이다.** 확정 축만 따로 움직이는 변이를 잡는 유일한 자리다.
    재검증_축 = [축 for 부 in 부서들.values() for 축 in 부.축]
    assert set(재검증_축) == set(확정_축), (
        f"같은 승인인데 재검증 봉투({sorted(set(재검증_축))})와 "
        f"판매 확정({sorted(set(확정_축))})이 다른 실행을 보고 있다"
    )
    assert 재검증_축, "재검증이 부서를 한 번도 안 불러 이 검사가 아무것도 안 재고 있다"


def test_축을_못_읽으면_확정하지_않는다(monkeypatch, 이력, 부서들, 확정):
    """🔴 **`or BURN_IN_SIM_RUN_ID` 로 메우는 길을 `record_decision` 에서도 막는다.**

    축이 안 실린 옛 실행 행으로 승인이 들어오면 번인 장부에 없던 판매가 쌓인다.
    터지지 않고 숫자만 틀리므로, 못 읽으면 아무것도 안 쓰는 것이 맞다.
    """
    행 = _판매_실행()
    행.pop("sim_run_id")
    _실행을_세운다(monkeypatch, 행)

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.revalidation_outcome == "ERROR"
    assert 확정.호출 == [], "축을 못 읽었는데 판매를 확정했다"
    assert saved.sale is not None and saved.sale.status == "BLOCKED"


def test_고른_안을_그대로_넘긴다(monkeypatch, 이력, 부서들, 확정):
    _실행을_세운다(monkeypatch, _판매_실행())

    decision_service.record_decision(REQ, _승인())

    보낸것 = 확정.호출[0]
    assert 보낸것.selected_scenario_id == SCN
    assert 보낸것.selected_scenario.scenario_id == SCN
    assert 보낸것.execution_identity.run_id == str(RUN_UUID)
    assert 보낸것.line.item_name == "배추"
    assert 보낸것.line.quantity_kg == Decimal(2000)


def test_승인이_출고를_하지_않는다(monkeypatch, 이력, 부서들, 확정):
    """🔴 *"승인 즉시 = 판매 확정"*, *"승인 즉시 ≠ 출고 즉시"* (2026-09-08 계약).

    예약·FEFO·출고·`DELIVERED`·채권은 **그 뒤 기존 orchestration** 이 자기 날에 한다.
    여기서 출고를 부르면 *"승인했지만 아직 안 나갔다"* 라는 상태가 사라진다.
    """
    _실행을_세운다(monkeypatch, _판매_실행())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.sale is not None and saved.sale.shipped is False
    원문 = (sales_approval.__file__,)
    for path in 원문:
        본문 = Path(path).read_text(encoding="utf-8")
        for 금지 in ("ship", "reserve_", "fefo", "FEFO", "DELIVERED"):
            assert f"{금지}(" not in 본문, f"승인 경로가 출고를 부른다: {금지}"


def test_판매_승인은_매입_약정을_만들지_않는다(monkeypatch, 이력, 부서들, 확정):
    """🔴 `_commitment_parts` 는 `response_payload["scenarios"]` 만 본다 — 판매
    응답에서는 늘 `buildable=False` 다. 그러면 *"판매를 확정했다"* 자리에 *"매입
    약정을 못 만들었다"* 가 실린다."""
    _실행을_세운다(monkeypatch, _판매_실행())

    saved = decision_service.record_decision(REQ, _승인())

    assert saved.commitment is None
    assert saved.transition is None


# ---------------------------------------------------------------------------
# ⑦ 후보 판정 — 값 없는 안은 화면에 안 올라간다
# ---------------------------------------------------------------------------


def _판정(**덮어쓰기: Any) -> CandidateVerdict:
    return CandidateVerdict(
        scenario=_scenario(**덮어쓰기),
        validations={"FINANCIAL_VALIDATION": {"runtime_status": "READY", "business_status": "ok"}},
    )


def test_상업조건이_다_있으면_후보가_통과한다():
    """🔴 **자기 생존 검사.** 아래 두 검사가 *"항상 탈락"* 을 재고 있지 않다는 것을
    이 한 줄이 보증한다."""
    assert _판정().passed is True
    assert _판정().missing_terms == ()


def test_delivery_date_없는_후보는_제시되지_않는다():
    """🔴 **⑥ 을 잡는 자리.** 값이 없는 안을 사용자가 고른 **뒤에야** 막는 것보다
    **승인 가능한 후보의 필수조건**으로 두는 편이 계약상 명확하다."""
    후보 = _판정(delivery_date=None)

    assert 후보.passed is False
    assert 후보.missing_terms == ("REQUEST_MISSING_delivery_date",)


def test_payment_days_없는_후보는_제시되지_않는다():
    후보 = _판정(payment_days=None)

    assert 후보.passed is False
    assert 후보.missing_terms == ("REQUEST_MISSING_payment_days",)


def test_탈락_사유가_부서_판정과_구별된다():
    """🔴 **부서 판정과 섞지 않는다.** 탈락 사유가 *"재무가 반려"* 처럼 보이면 사람이
    재무를 본다. *"납품일이 없다"* 로 보여야 판매를 본다.

    ⚠️ `validations` 에 가짜 항목을 밀어 넣는 구현이면 여기가 빨개진다 — 그 사전은
      **부서가 보낸 것만** 담는다.
    """
    후보 = _판정(delivery_date=None)

    assert "납품일" in 후보.detail
    assert set(후보.validations) == {"FINANCIAL_VALIDATION"}, (
        "상업조건 사유를 부서 판정 사전에 밀어 넣었다"
    )
    assert "FINANCIAL_VALIDATION(" not in 후보.detail, (
        "통과한 부서 판정이 탈락 사유로 나온다 — 사람이 재무를 보러 간다"
    )


def test_부서가_반려한_후보의_사유는_그대로_남는다():
    """★ 위 검사의 짝. **상업조건 검사를 더한 것이지 부서 판정을 가린 것이 아니다.**"""
    후보 = CandidateVerdict(
        scenario=_scenario(),
        validations={
            "FINANCIAL_VALIDATION": {
                "runtime_status": "READY",
                "business_status": "reject",
                "reasoning": "한도 초과",
            }
        },
    )

    assert 후보.passed is False
    assert 후보.missing_terms == ()
    assert "한도 초과" in 후보.detail


def test_후보_판정의_필수값_목록은_확정_쪽과_같은_것이다():
    """🔴 **주인이 하나다.** 두 곳에 베껴 두면 *"올려도 되는 안"* 과 *"확정할 수 있는
    안"* 이 갈리는 날이 온다."""
    assert sales_approval.REQUIRED_COMMERCIAL_TERMS == ("delivery_date", "payment_days")
    assert tuple(
        sales_approval.term_of_origin(o)
        for o in CandidateVerdict(scenario=_scenario(delivery_date=None)).missing_terms
    ) == sales_approval.missing_commercial_terms(_scenario(delivery_date=None))


# ---------------------------------------------------------------------------
# ⑧ 자기 생존 검사 — 0건을 세고 초록이 되지 않는다
# ---------------------------------------------------------------------------


def test_확정_대역이_실제로_불릴_수_있다(monkeypatch, 이력, 부서들, 확정):
    """🔴 **`assert 확정.호출 == []` 를 재는 검사들의 바닥이다.**

    실행이 아예 안 서면 `confirm_sale` 은 언제나 0회이고, 그러면 *"안 불렀다"* 를
    재는 검사가 전부 공짜로 통과한다. **불릴 수 있다**는 것을 여기서 한 번 보인다.
    """
    _실행을_세운다(monkeypatch, _판매_실행())

    decision_service.record_decision(REQ, _승인())

    assert len(확정.호출) == 1
    assert 확정.conn.commits == 1, "확정했는데 커밋을 안 했다"
    assert 확정.conn.closed == 1


def test_후보_목록이_비어_있지_않다():
    """★ `scenario_ids_of` 가 늘 빈 튜플을 돌려주면 *"없는 id 는 거절된다"* 가 공짜다."""
    assert scenario_ids_of(_판매_실행()["response_payload"]), (
        "후보 목록이 비었다 — 위 검사가 공짜다"
    )


def test_부서가_실제로_불린다(monkeypatch, 이력, 부서들, 확정):
    """★ `test_재검증은_그_실행의_날로_돈다` 가 **빈 집합**을 재고 있지 않다는 보증."""
    _실행을_세운다(monkeypatch, _판매_실행())

    decision_service.record_decision(REQ, _승인())

    assert [부.호출 for 부 in 부서들.values() if 부.호출], "재검증이 부서를 한 번도 안 불렀다"
