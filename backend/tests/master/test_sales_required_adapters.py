"""판매 필수 어댑터 사전 점검 — **부르기 전에 배선을 본다.**

`run_sales()` 는 어댑터가 없어도 그냥 시작했다. 골격이 `AgentNotRegistered` 를
`SL4_NOT_STARTED` 로 받으므로 터지지는 않는다 (`sales_flow.py` 의 `run`).

🔴 **문제는 터지느냐가 아니라 그때 이미 다른 부서를 부른 뒤라는 것이다.**

```text
물류 호출 → 회신이 이력에 남는다 → 판매 미등록으로 SL4
                                    나중에 읽는 사람: "돌긴 돌았다"
```

매입이 같은 이유로 `wiring.missing()` 갈래에서 선다 (`service.py`). 판매도 같은
자리에 같은 태도로 선다 — **어휘만 판매 것이다** (`SL4_NOT_STARTED`).

⚠️ **순서가 있다.**

```text
① 개장 Gate        그 날 장부가 열렸는가
② 필수 어댑터 점검  누가 배선돼 있는가
```

  뒤집으면 안 열린 날에 *"어댑터 미등록"* 이라고 답한다 — 사람이 배선을 뒤지는데
  실제로는 그 날을 다시 열 일이다. 아래 ④가 그것을 잠근다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import wiring
from app.master.day_gate import DayGate
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.schemas import SalesRunRequest
from app.master.service import run_sales

평일 = date(2026, 9, 10)


# ── 가짜 부서 ────────────────────────────────────────────────────────────────


def _reply(request: AgentRequest, **kw) -> AgentReply:
    base = {
        "request_id": request.context.request_id,
        "as_of": request.context.as_of,
        "agent": request.agent,
        "mode": request.mode,
        "run_id": f"{request.agent.upper()}-{request.call_seq}",
        "runtime_status": "READY",
        "business_status": "ok",
    }
    base.update(kw)
    return AgentReply(**base)


def _port(payload: dict[str, Any] | None = None, capture: list | None = None, **reply_kw):
    def port(request: AgentRequest):
        if capture is not None:
            capture.append((request.agent, request.mode))
        reply = _reply(request, payload=payload or {}, **reply_kw)
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


_제안 = {
    "scenarios": [{"scenario_id": "SCN-1", "required_validations": ["FINANCIAL_VALIDATION"]}],
    "situation": "물량이 있다",
}


def _wire(*agents: str, capture: list | None = None, 물류_회신: dict | None = None) -> list:
    """**부른 이름만** 등록한다. 등록 안 한 부서는 배선이 빈 것이다.

    ★ 루트 `conftest.py` 가 등록을 스냅샷/복원하므로 이 테스트 밖으로 안 샌다.
    """
    called = capture if capture is not None else []
    wiring.reset()
    if "inventory" in agents:
        wiring.register("inventory", _port({"sellable": "yes"}, called, **(물류_회신 or {})))
    if "sales" in agents:
        wiring.register("sales", _port(_제안, called))
    if "finance" in agents:
        wiring.register("finance", _port({"verdict": "ok"}, called))
    return called


def _request(**kw) -> SalesRunRequest:
    base = {
        "as_of": 평일,
        "policy_version": "v1.3",
        "business_mode": "SPOT_SALES",
        "item": "배추",
    }
    base.update(kw)
    return SalesRunRequest(**base)


@pytest.fixture
def 막힌_개장(monkeypatch: pytest.MonkeyPatch) -> None:
    """개장 관문을 `BLOCKED` 로 꽂는다 — conftest 의 통과 fixture 를 덮는다."""
    monkeypatch.setattr(
        "app.master.service.check_day_gate",
        lambda as_of, **kw: DayGate(
            as_of=as_of,
            gate="BLOCKED",
            result="NOT_OPENED",
            reason="inventory 가 안 열렸다 (마지막 개장 2026-09-09 · 1일 전).",
            next_action="RETRY_OPEN_DAY",
        ),
    )


@pytest.fixture
def 적재를_지켜본다(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """`try_save_run` 에 **무엇을 넣으려 했는가**를 잡는다.

    ★ pytest 안에서는 `history_enabled()` 가 False 라 실제 INSERT 가 안 돈다.
    """
    seen: list[dict[str, Any]] = []

    def fake(**kwargs):
        seen.append(kwargs)
        return "RUN-FAKE-SALES"

    monkeypatch.setattr("app.master.persistence.try_save_run", fake)
    return seen


# ---------------------------------------------------------------------------
# ① 목록 — 왜 둘뿐인가
# ---------------------------------------------------------------------------


def test_판매_필수는_제안자와_최종_검증자_둘뿐이다():
    """★ 목록의 주인은 `wiring.py` 하나다 — 여기서는 그 값을 잠근다."""
    assert wiring.REQUIRED_FOR_SALES == ("sales", "finance")


def test_제안자가_목록에_있다():
    """★ 제안자가 없으면 후보가 0이다 — 시작할 이유가 없다."""
    assert "sales" in wiring.REQUIRED_FOR_SALES, (
        "판매가 필수에서 빠졌다 — 제안자 없이 물류부터 부르고 나서 SL4 로 접힌다"
    )


def test_물류는_필수가_아니다():
    """🔴 **판매 Flow 는 밴드가 없다** — 물류가 못 답해도 시작한다 (설계 §1-2).

    필수 목록에 넣으면 그 결정을 **배선 쪽에서 뒤집는** 셈이다.
    """
    assert "inventory" not in wiring.REQUIRED_FOR_SALES, (
        "물류를 판매 필수로 올렸다 — 밴드 없이 시작한다는 설계를 배선이 뒤집는다"
    )


def test_매입은_필수가_아니다():
    """★ 부족량이 있는 후보에만 필요한 **조건부**이고, 지금은 라우팅이 `None` 이라
    아예 안 불린다 — 안 부르는 대상을 문 앞 필수로 올리면 매일 선다.
    """
    from app.master.envelope import CAPABILITY_ROUTING

    assert CAPABILITY_ROUTING["ADDITIONAL_SUPPLY_CONTEXT"] is None, (
        "매입 라우팅이 채워졌다면 이 검사의 전제가 바뀐 것이다"
    )
    assert "purchase" not in wiring.REQUIRED_FOR_SALES


def test_판매_필수와_매입_필수는_다른_목록이다():
    """★ 겹치지만 같은 목록이 아니다 — 한쪽을 다른 쪽으로 대신 쓰면 판매가 물류
    미등록으로 서거나 매입이 물류 없이 돈다.
    """
    assert wiring.REQUIRED_FOR_SALES != wiring.REQUIRED_FOR_PROCUREMENT
    assert "inventory" in wiring.REQUIRED_FOR_PROCUREMENT, (
        "매입 필수를 건드렸다 — 이 조각은 매입 목록을 바꾸지 않는다"
    )


# ---------------------------------------------------------------------------
# ② 부르기 전에 선다
# ---------------------------------------------------------------------------


def test_제안자가_없으면_부서를_한_번도_안_부른다():
    """🔴 **여기가 이 조각의 핵심이다.**

    골격만 있으면 물류를 먼저 부르고 나서 판매 미등록으로 접힌다. 그 회신 한 건이
    이력에 남아 *"돌긴 돌았다"* 로 읽힌다.
    """
    called = _wire("inventory", "finance")

    response = run_sales(_request())

    assert called == [], f"판매 어댑터가 없는데 부서를 불렀다: {called}"
    assert response.end_code == "SL4_NOT_STARTED"


def test_사유에_빠진_이름이_적힌다():
    """★ *"어댑터 미등록"* 만 적으면 **무엇을 배선해야 하는지** 모른 채 코드를 뒤진다."""
    _wire("inventory", "finance")

    response = run_sales(_request())

    assert "sales" in response.reason, f"빠진 이름이 사유에 없다: {response.reason}"


def test_둘_다_없으면_둘_다_적는다():
    """★ 한 번에 하나씩 알려 주면 배선하고 다시 돌리기를 반복한다."""
    _wire("inventory")

    response = run_sales(_request())

    assert "sales" in response.reason and "finance" in response.reason, response.reason


def test_못_부른_날도_이력에_남는다(적재를_지켜본다):
    """🔴 **안 부른 것과 못 부른 것은 다르다.** 이력이 비면 둘이 같아 보인다."""
    _wire("inventory", "finance")

    response = run_sales(_request())

    assert len(적재를_지켜본다) == 1
    row = 적재를_지켜본다[0]
    assert row["cycle"] == "SALES"
    assert row["end_code"] == "SL4_NOT_STARTED"
    assert row["runtime_status"] == "RUNTIME_NOT_READY", "못 시작한 날이 '돈 날' 로 남았다"
    assert response.history_run_id == "RUN-FAKE-SALES"


def test_접힌_사유가_사람이_읽는_줄로도_나간다():
    """★ `SL4` 는 접힌 코드다 — 판매 문장이 없으므로 **왜 접혔는지**를 적는다."""
    _wire("inventory", "finance")

    response = run_sales(_request())

    assert response.report_text.startswith("시작하지 못했다 (SL4_NOT_STARTED)")
    assert "sales" in response.report_text


# ---------------------------------------------------------------------------
# ③ 🔴 물류 미등록은 판매를 세우지 않는다
# ---------------------------------------------------------------------------


def test_물류_미등록이_판매를_문_앞에서_세우지_않는다():
    """🔴 **물류를 필수에 더하면 이 검사가 빨개진다.**

    물류가 없어도 판매는 **시작은 한다** — 그 뒤 골격이 물류를 부르다 미등록으로
    접히더라도, 그것은 *"부르고 나서 못 받았다"* 이지 *"배선이 비어서 안 섰다"* 가
    아니다. 사유가 그 둘을 구분해야 사람이 옳은 곳을 본다.
    """
    _wire("sales", "finance")

    response = run_sales(_request())

    assert "어댑터 미등록" not in response.reason, (
        f"물류가 문 앞 필수 목록에 올라갔다 — 판매가 배선 점검에서 섰다: {response.reason}"
    )
    assert response.reason.startswith("에이전트 미등록"), f"골격까지 못 갔다: {response.reason}"


def test_물류가_못_답해도_판매는_끝까지_돈다():
    """🔴 **지금 실제 공백은 등록이 아니라 모드다.**

    `inventory` 는 등록돼 있고 `PRE_SALES` 분기만 없어서 **회신**으로 온다 —
    `RUNTIME_NOT_READY` 는 배선이 빈 것과 다르다. 그 회신은 후보의 탈락 사유가 될
    뿐 사이클을 세우지 않는다.
    """
    called = _wire(
        "inventory",
        "sales",
        "finance",
        물류_회신={"runtime_status": "RUNTIME_NOT_READY", "missing_data": ["PRE_SALES 분기 없음"]},
    )

    response = run_sales(_request())

    assert response.end_code == "SL1_PRESENTED", (
        f"물류 회신 하나에 판매가 접혔다: {response.reason}"
    )
    assert [agent for agent, _ in called] == ["inventory", "sales", "finance"]


def test_셋이_다_있으면_점검이_막지_않는다():
    """★ **이것이 없으면 위 검사들이 공짜로 초록이 된다** — 점검이 늘 막는 것은 아니다."""
    _wire("inventory", "sales", "finance")

    response = run_sales(_request())

    assert response.end_code == "SL1_PRESENTED", f"필수가 다 있는데 접혔다: {response.reason}"


# ---------------------------------------------------------------------------
# ④ 🔴 순서 — 개장이 먼저다
# ---------------------------------------------------------------------------


def test_안_열린_날에는_어댑터_이야기를_하지_않는다(막힌_개장):
    """🔴 **점검이 개장 Gate 보다 앞서면 조사 방향이 틀어진다.**

    안 열린 날인데 *"어댑터 미등록"* 이라고 답하면 사람이 배선을 뒤진다. 실제로는
    그 날을 다시 열 일이다.
    """
    _wire()  # 아무도 등록하지 않는다 — 두 관문이 다 걸릴 조건이다

    response = run_sales(_request())

    assert "안 열렸다" in response.reason, f"개장보다 배선 점검이 먼저 답했다: {response.reason}"
    assert "어댑터 미등록" not in response.reason


def test_안_열린_날_점검_조건이_실제로_성립한다():
    """★ 위 검사의 전제. 배선이 실제로 비어 있어야 *"개장이 먼저"* 를 잰 것이 된다."""
    _wire()

    assert wiring.missing(wiring.REQUIRED_FOR_SALES) == ("sales", "finance")
