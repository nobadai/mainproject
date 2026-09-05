"""실행일 봉투가 **매입 payload 까지 실제로 간다** (2026-09-05).

🔴 **이 파일이 있는 이유.** 어제 `#265` 의 금액 변이 19건이 자기 단위 테스트에서만
돌았고, 나는 *"지적 0건"* 을 *"돌았고 통과했다"* 로 읽었다. 매입이 그것을 짚어 줬다.

★ 그래서 **만드는 쪽만 재지 않는다.** `test_execution_calendar.py` 가 봉투를 만드는
  것을 재고, 여기가 **그 봉투가 매입 손에 닿는 것**을 잰다. 통로 한 칸만 끊겨도
  여기가 운다.

```text
build_execution_calendar   → service._execution_calendar_payload
                           → ProcurementFlow(execution_calendar=…)
                           → payload["execution_calendar"]   ← 매입이 읽는 자리
```
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.master.budget import CallBudget
from app.master.calendar_walk import MAX_WALK_DAYS
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.execution_day import CalendarNotCovered
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner

AS_OF = date(2026, 1, 5)  # 월요일
CALENDAR = {"non_execution_days": ["2026-01-10", "2026-01-11"], "horizon_end": "2026-02-05"}


def _ctx() -> ExecutionContext:
    return ExecutionContext("REQ-20260105-0001", AS_OF, "USER_REQUEST", "v1.3")


def _port(payload: dict[str, Any] | None = None):
    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload=payload or {"cap": 1},
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    return port


def _flow(**kw: Any) -> tuple[list[dict[str, Any]], ProcurementFlow]:
    """매입이 **실제로 받은 payload** 를 모은다."""
    got: list[dict[str, Any]] = []

    def purchase(request: AgentRequest):
        got.append(dict(request.payload))
        return _port({"scenarios": [{"scenario_id": "SCN-1"}]})(request)

    registry = AgentRegistry()
    registry.register("finance", _port())
    registry.register("inventory", _port())
    registry.register("purchase", purchase)
    flow = ProcurementFlow(
        MasterRunner(_ctx(), registry, CallBudget(limit=12)),
        verifier=None,
        item="배추",
        **kw,
    )
    return got, flow


# ── ① Flow — 봉투가 매입 payload 에 실린다 ─────────────────────────────────


def test_봉투가_매입_payload_에_실린다():
    """🔴 **이 파일의 주장이다.** 전에는 매입이 달력을 아무 데서도 못 받았다."""
    got, flow = _flow(execution_calendar=CALENDAR)
    flow.run()

    assert got, "매입이 불려야 이 검사가 의미 있다"
    assert got[0]["execution_calendar"] == CALENDAR


def test_constraints_안에_넣지_않는다():
    """★ **`constraints` 는 부서가 낸 것**이고 (`AgentName` 으로 갈린다), 달력은
    마스터가 만든 값이다. 거기 얹으면 다음 사람이 물류 칸으로 읽는다."""
    got, flow = _flow(execution_calendar=CALENDAR)
    flow.run()

    assert "execution_calendar" not in got[0].get("constraints", {})
    assert "execution_calendar" in got[0], "최상위에 있어야 한다"


def test_봉투가_없으면_칸을_안_만든다():
    """⚠️ **빈 매핑으로 안 채운다.** 빈 목록은 *"비영업일이 없다"* 라는 확정이고,
    못 실은 것은 *"모른다"* 다 — 둘이 같아지면 아무도 못 가른다."""
    got, flow = _flow()
    flow.run()

    assert "execution_calendar" not in got[0]


# ── ② service — 실 경로에서 값이 만들어져 닿는다 ───────────────────────────


def _wire_capturing() -> list[dict[str, Any]]:
    from app.master import wiring

    seen: list[dict[str, Any]] = []

    def port(request: AgentRequest):
        if request.agent == "purchase":
            seen.append(dict(request.payload))
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        payload = {"scenarios": [{"scenario_id": "SCN-1"}]} if request.agent == "purchase" else {}
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload=payload,
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    wiring.reset()
    for part in ("finance", "inventory", "purchase"):
        wiring.register(part, port)
    return seen


@pytest.fixture
def 적재를_막는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: "RUN-FAKE-1")


def _run(as_of: date):
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    return run_procurement(
        ProcurementRunRequest(
            as_of=as_of, policy_version="v1.3", item="배추", request_id="REQ-CAL-0001"
        ),
        verifier=None,
    )


def test_run_procurement_이_봉투를_만들어_매입에_준다(적재를_막는다: None):
    """🔴 **라이브 경로다.** 단위 검사가 아니라 `run_procurement` 이 만든 값이다."""
    seen = _wire_capturing()
    _run(AS_OF)

    assert seen, "매입이 불려야 한다"
    envelope = seen[0]["execution_calendar"]
    assert envelope["horizon_end"] == (AS_OF + timedelta(days=MAX_WALK_DAYS)).isoformat()


def test_봉투가_개장_축을_타고_온다_요일이_아니다(
    monkeypatch: pytest.MonkeyPatch, 적재를_막는다: None
):
    """🔴 **이 파일의 핵심이다.** 통로가 뚫렸는지가 아니라 **어느 축이 왔는지**를 잰다.

    ★ 토요일을 열고 수요일을 닫은 가짜를 꽂는다. 요일 축이 조금이라도 섞여 있으면
      토요일이 목록에 들어와 여기가 운다 — `is_open` 만 보는 것을 잠그는 자리다.
    """
    토요일 = date(2026, 1, 10)
    수요일 = date(2026, 1, 14)

    class _토요일은_열고_수요일은_닫는_시장:
        def is_market_open(self, day: date) -> bool:
            return day != 수요일

    monkeypatch.setattr(
        "app.master.service.get_market_calendar", lambda: _토요일은_열고_수요일은_닫는_시장()
    )
    seen = _wire_capturing()
    _run(AS_OF)

    days = seen[0]["execution_calendar"]["non_execution_days"]
    assert days == [수요일.isoformat()], f"개장 축이 아니라 다른 축이 왔다 — {days}"
    assert 토요일.isoformat() not in days, "토요일을 요일로 밀었다"


def test_문_앞_축과_봉투_축이_따로_돈다(monkeypatch: pytest.MonkeyPatch, 적재를_막는다: None):
    """★ **두 축이 다른 물음에 답한다.**

    ```text
    문 앞   "ML 예측이 있어 판단을 도는가"   holiday_nm + 주말
    봉투    "시장에서 살 수 있는가"          is_open
    ```

    `as_of` 를 공휴일로 만들면 문 앞이 접고, 장은 계속 서게 둬도 그 판단이 안 바뀐다 —
    한 축이 다른 축을 덮어쓰지 않는다는 뜻이다.
    """

    class _언제나_공휴일:
        def is_holiday(self, day: date) -> bool:
            return True

    class _언제나_개장:
        def is_market_open(self, day: date) -> bool:
            return True

    monkeypatch.setattr("app.master.service.get_calendar", lambda: _언제나_공휴일())
    monkeypatch.setattr("app.master.service.get_market_calendar", lambda: _언제나_개장())
    seen = _wire_capturing()
    response = _run(AS_OF)

    assert response.end_code == "E4_NOT_STARTED", "문 앞이 공휴일 축으로 접어야 한다"
    assert not seen, "안 도는 날에는 매입을 안 부른다"


def test_달력이_끊기면_봉투를_안_싣고_못_봤다고_남긴다(
    monkeypatch: pytest.MonkeyPatch, 적재를_막는다: None
):
    """🔴 **반쪽 달력보다 없는 달력이 낫다.**

    덮인 데까지만 실으면 `horizon_end` 가 거짓말을 합니다 — 받는 쪽은 그 날까지 다
    봤다고 읽습니다. 안 실으면 매입은 오늘까지의 동작으로 돌고 **그 사실이 남습니다.**
    """

    class 지평_끝이_없는_시장:
        def is_market_open(self, day: date) -> bool:
            if day > date(2026, 1, 20):
                raise CalendarNotCovered(f"{day.isoformat()} 이 달력에 없다")
            return True

    monkeypatch.setattr("app.master.service.get_market_calendar", lambda: 지평_끝이_없는_시장())
    seen = _wire_capturing()
    response = _run(AS_OF)

    assert "execution_calendar" not in seen[0], "반쪽 달력을 실었다"
    assert any("실행일 봉투" in note for note in response.skipped_checks), (
        f"못 실은 사실이 안 남았다 — {response.skipped_checks}"
    )


def test_문_앞_판정은_통과하는데_봉투만_못_싣는_경우가_있다(
    monkeypatch: pytest.MonkeyPatch, 적재를_막는다: None
):
    """★ **두 물음이 다르다.** 문 앞은 *"오늘 도는가"* 하나이고 봉투는 **지평 전체**다.

    오늘은 덮이는데 지평 끝이 안 덮이면 **판단은 돌고 봉투만 빠진다** — 그것이 여기서
    E4 로 접히면 달력 한 칸 때문에 매입 판단이 멈춘 것이 된다.
    """

    class 오늘만_아는_시장:
        def is_market_open(self, day: date) -> bool:
            if day > AS_OF:
                raise CalendarNotCovered(f"{day.isoformat()} 이 달력에 없다")
            return True

    monkeypatch.setattr("app.master.service.get_market_calendar", lambda: 오늘만_아는_시장())
    seen = _wire_capturing()
    response = _run(AS_OF)

    assert seen, "매입이 안 불렸다 — 달력 때문에 판단이 멈췄다"
    assert response.end_code != "E4_NOT_STARTED"
    assert "execution_calendar" not in seen[0]
