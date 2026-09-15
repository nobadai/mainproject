"""어제 승인한 매입이 **오늘 실행에 실린다.**

2026-09-03 · #185.

🔴 마스터는 H1 확정 입고 약정을 **만들어서 저장까지** 해 놓고 **다음 실행에 안
실었다.** 어제 승인한 매입이 오늘 창고에 없는 것처럼 됐다.

```text
H1 약정을 만든다        ✅ commitment.build_commitment
승인 응답에 실어 낸다     ✅ decision_service
GET 으로 다시 본다       ✅ current_commitment
다음 실행이 받는다       🔴 없었다
```

★ *"값을 실어 주고 안 쓰는 것"* 을 매입에 지적했는데, 여기는 **만들어 놓고 안 실어
  보내는** 쪽이다. 같은 병의 반대편이다.

⚠️ **받는 쪽은 아직 없다.** 이 파일이 잠그는 것은 *"보냈는가"* 이지 *"반영됐는가"*
  가 아니다 — 되먹임 `adjustments` 때와 같은 순서다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner

AS_OF = date(2025, 12, 31)
SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]

COMMITMENT = {
    "approval_id": "H1-REQ-20251230-0001-1",
    "item": "배추",
    "as_of": "2025-12-30",
    "arrival_schedule": [{"date": "2026-01-02", "qty_kg": 500.0, "item": "배추"}],
}


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


class _Advisor:
    """경계 호출이 **무엇을 받았는지** 기록한다."""

    def __init__(self) -> None:
        self.pre_payloads: list[dict[str, Any] | None] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        pre = request.mode == "PRE_PURCHASE"
        if pre:
            self.pre_payloads.append(dict(request.payload) if request.payload else None)
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload={"cap": 1} if pre else {},
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta


def _purchase(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    run_id = f"PURCHASE-{request.call_seq}"
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent="purchase",
        mode=request.mode,
        run_id=run_id,
        runtime_status="READY",
        business_status="ok",
        payload={"scenarios": list(SCN)},
    )
    meta = ExecutionMetadata(
        run_id=run_id, request_id=request.context.request_id, agent="purchase"
    )
    return reply, meta


def _run(commitments=()):
    finance, inventory = _Advisor(), _Advisor()
    registry = AgentRegistry()
    registry.register("finance", finance)
    registry.register("inventory", inventory)
    registry.register("purchase", _purchase)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    outcome = ProcurementFlow(
        runner, item="배추", approved_commitments=commitments
    ).run()
    return outcome, finance, inventory


# ── ① 실린다 ────────────────────────────────────────────────────────────────


def test_경계_호출에_약정이_실린다():
    """🔴 **이 파일의 주장이다.**"""
    _, finance, _ = _run((COMMITMENT,))

    assert finance.pre_payloads[0] is not None
    assert finance.pre_payloads[0]["approved_commitments"] == [COMMITMENT]


def test_조언자_전원이_같은_것을_받는다():
    """재무는 현금으로, 물류는 창고로 본다 — **한쪽만 주면 둘이 다른 세상을 본다.**"""
    _, finance, inventory = _run((COMMITMENT,))

    assert finance.pre_payloads[0] == inventory.pre_payloads[0]


def test_마스터가_해석하지_않는다():
    """부서가 낸 모양 그대로 나른다 (§3.2.2) — 도착일도 수량도 안 건드린다."""
    _, finance, _ = _run((COMMITMENT,))
    sent = finance.pre_payloads[0]["approved_commitments"][0]

    assert sent["arrival_schedule"] == COMMITMENT["arrival_schedule"]
    assert sent["approval_id"] == COMMITMENT["approval_id"]


def test_여러_건이면_온_차례_그대로다():
    """고르지도 정렬하지도 않는다 — 고르는 것이 곧 판단이다."""
    second = {**COMMITMENT, "approval_id": "H1-REQ-20251229-0001-1"}
    _, finance, _ = _run((COMMITMENT, second))

    labels = [c["approval_id"] for c in finance.pre_payloads[0]["approved_commitments"]]
    assert labels == [COMMITMENT["approval_id"], second["approval_id"]]


# ── ② 없으면 만들지 않는다 ──────────────────────────────────────────────────


def test_승인이_없으면_칸을_안_만든다():
    """🔴 **빈 배열을 보내면 안 된다.**

    받는 쪽이 *"어제 승인이 없었다"* 와 *"마스터가 안 보낸다"* 를 구별할 수 없다
    (§1.2-10 · 0 과 모름은 다르다).
    """
    _, finance, _ = _run()

    assert finance.pre_payloads[0] is None


# ── ③ 회귀 — 기존 경로를 안 깬다 ────────────────────────────────────────────


def test_약정이_있어도_경계_수집이_그대로_돈다():
    """payload 를 얹었다고 밴드가 안 서면 안 된다."""
    outcome, _, _ = _run((COMMITMENT,))

    assert outcome.end_code == "E1_APPROVED"
    assert len(outcome.scenarios) == 1


def test_재시도도_같은_입력으로_부른다():
    """경계 재호출(`retryable`)이 약정 없이 부르면 두 호출의 입력이 갈린다."""
    calls: list[dict[str, Any] | None] = []

    def flaky(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        pre = request.mode == "PRE_PURCHASE"
        if pre:
            calls.append(dict(request.payload) if request.payload else None)
        first_pre = pre and len(calls) == 1
        run_id = f"INV-{request.mode[:3]}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            # 첫 경계 호출만 ERROR — retryable 이 한 번 더 부른다
            runtime_status="ERROR" if first_pre else "READY",
            business_status="skipped" if first_pre else "ok",
            payload={} if first_pre else ({"cap": 1} if pre else {}),
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    registry = AgentRegistry()
    registry.register("finance", _Advisor())
    registry.register("inventory", flaky)
    registry.register("purchase", _purchase)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    ProcurementFlow(runner, item="배추", approved_commitments=(COMMITMENT,)).run()

    assert len(calls) == 2, "재시도가 일어나야 이 검사가 의미 있다"
    assert calls[0] == calls[1], "재시도가 다른 입력으로 불렀다"


# ── ④ #310 — 매입도 같은 값을 받는다 (2026-09-06) ───────────────────────────
#
# 🔴 **`②` 의 docstring 이 *"받는 쪽은 아직 없다"* 라고 적어 두었는데, 생겼다.**
#
#   매입이 *"어제 승인 때문에 창고 여유가 줄었다"* 를 쓸 근거가 없었다. 숫자는
#   이어지는데(`cap_by_date` 7,645.6 → 4,058.6) **무엇이** 그 3,587kg 인지가
#   봉투에 없었다 — `approved_commitments` 가 경계 호출에만 실렸기 때문이다.


def _run_capturing(commitments=()):
    """매입이 **실제로 받은 payload** 까지 모은다."""
    got: list[dict[str, Any]] = []

    def purchase(request: AgentRequest):
        got.append(dict(request.payload))
        return _purchase(request)

    finance = _Advisor()
    registry = AgentRegistry()
    registry.register("finance", finance)
    registry.register("inventory", _Advisor())
    registry.register("purchase", purchase)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    ProcurementFlow(runner, item="배추", approved_commitments=commitments).run()
    return got, finance


def test_매입도_약정을_받는다():
    """🔴 **`#310` 의 본문이다.** 전에는 경계 호출에만 실렸다."""
    got, _ = _run_capturing((COMMITMENT,))

    assert got, "매입이 불려야 이 검사가 의미 있다"
    assert got[0]["approved_commitments"] == [COMMITMENT]


def test_경계와_매입이_같은_값을_본다():
    """★ 두 곳이 다른 목록을 보면 **물류가 본 미래 입고**와 **매입이 말하는 어제
    승인**이 갈리고, 갈린 날 화면과 창고가 다른 이야기를 한다."""
    got, finance = _run_capturing((COMMITMENT,))

    assert got[0]["approved_commitments"] == finance.pre_payloads[0]["approved_commitments"]


def test_매입_payload_의_constraints_밖이다():
    """⚠️ 부서가 낸 값이 아니라 **마스터가 이력에서 재조립한 값**이다 —
    `execution_calendar` 와 같은 자리다."""
    got, _ = _run_capturing((COMMITMENT,))

    assert "approved_commitments" not in got[0].get("constraints", {})
    assert "approved_commitments" in got[0], "최상위에 있어야 한다"


def test_매입도_없으면_칸을_안_만든다():
    """🔴 빈 배열을 보내면 *"어제 승인이 없었다"* 와 *"마스터가 안 보낸다"* 가 같아진다."""
    got, _ = _run_capturing()

    assert "approved_commitments" not in got[0]


def test_매입이_받는_약정에_도착_일정이_들어_있다():
    """★ 매입이 쓰려는 문장이 *"어제 승인분 3,587kg 이 01-07 에 온다"* 다.

    **수량과 도착일이 있어야** 그 문장이 나온다 — 라이브 실측에서
    `arrival_schedule` 이 `qty_kg · arrival_date · purchase_date · seq` 를 든다.
    """
    got, _ = _run_capturing((COMMITMENT,))
    schedule = got[0]["approved_commitments"][0]["arrival_schedule"]

    assert schedule, "도착 일정이 비면 '언제 온다' 를 못 쓴다"
    assert "qty_kg" in schedule[0]


def test_마스터가_매입_쪽에서도_해석하지_않는다():
    """★ 경계 호출과 같은 규율 — 온 그대로 나른다."""
    got, _ = _run_capturing((COMMITMENT,))

    assert got[0]["approved_commitments"][0] == COMMITMENT
