"""실행일 봉투를 **출처표에 적는다** (`#300` · 매입 보고 2026-09-10).

🔴 **이 파일이 있는 이유.** 매입이 *"`#300` 달력이 `09-05` 에 섰는데 그 뒤 665건
전수 0건"* 이라고 보고했다. 그런데 배선은 서 있었다 —

```text
09-05 이후 매입 실행 655건 중 「실행일 봉투」 실패 사유   0건
→ CalendarNotCovered 가 한 번도 안 났으니 매번 실렸다
```

전수 0건이 나온 이유는 **표가 다르기 때문**이다. 봉투는 `AgentRequest.payload` 에
실리는데(`flow.py`), `master_agent_runs` 는 그것을 안 담는다.

🔴 **그리고 마스터 잘못이 하나 있다.** `_input_sources` 를 만들면서 *"주입한 키는
주입이라고 적는다"* 고 해 놓고 **실행일 봉투는 안 적었다.** 그래서 받는 쪽이 확인할
길이 없어 **없는 표를 뒤졌다.**

```text
실었다      execution_calendar: "DERIVED:market_calendar"
못 실었다   execution_calendar: "MISSING:-"
```

★ **어휘를 새로 만들지 않는다.** `REQUEST`·`MEASURED`·`DERIVED`·`MISSING`·`MOCK`
  다섯이 이미 있고, 이 값은 DB 시장달력에서 규칙으로 파생하므로 `DERIVED` 다.

★ **적히는 것 자체를 잰다.** 값이 비면 *"모른다"* 가 되어 예전과 같아진다 —
  받는 쪽이 없는 표를 다시 뒤진다.

⚠️ **DB 를 타지 않는다.** 달력도 적재층도 전부 대역이다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import wiring
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.execution_day import CalendarNotCovered
from app.master.inputs import MasterInputs, SourcedInput
from app.master.schemas import ProcurementRunRequest
from app.master.service import run_procurement

AS_OF = date(2025, 12, 31)

#: 출처표가 쓰는 이름. 🔴 **봉투가 실리는 payload 키와 같은 이름이어야 한다** —
#: 갈리면 받는 쪽이 표와 봉투를 못 맞춘다.
KEY = "execution_calendar"


def _loaded() -> MasterInputs:
    """적재층이 셋 다 읽은 상태. 달력 축과 **섞이지 않는지**도 같이 본다."""
    return MasterInputs(
        forecast=SourcedInput("forecast", {"horizon_days": 18}, "MEASURED", "뷰", ""),
        confirmed_orders=SourcedInput("confirmed_orders", {"total_kg": 1.0}, "DERIVED", "뷰", ""),
        policy_values=SourcedInput("policy_values", {"item_mix_ratio": {}}, "DERIVED", "표", ""),
    )


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


@pytest.fixture
def 매입이_받은_payload(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr("app.master.service.collect_inputs", lambda *a, **k: _loaded())
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: None)

    got: list[dict[str, Any]] = []

    def purchase(request: AgentRequest):
        got.append(dict(request.payload))
        return _port({"scenarios": [{"scenario_id": "SCN-1"}]})(request)

    wiring.reset()
    wiring.register("finance", _port())
    wiring.register("inventory", _port())
    wiring.register("purchase", purchase)
    return got


def _봉투가_선다(monkeypatch: pytest.MonkeyPatch) -> None:
    """달력이 지평을 다 덮는 날. **DB 를 안 타고** 봉투만 세운다."""

    class _봉투:
        def as_payload(self) -> dict[str, Any]:
            return {"non_execution_days": ["2026-01-01"], "horizon_end": "2026-01-18"}

    monkeypatch.setattr("app.master.service.get_market_calendar", lambda: object())
    monkeypatch.setattr("app.master.service.build_execution_calendar", lambda *a, **k: _봉투())


def _봉투가_안_선다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 달력이 지평을 다 안 덮는 날. `_execution_calendar_payload` 가 통째로 안 싣는다."""

    def 못_덮는다(*a: Any, **k: Any):
        raise CalendarNotCovered("2026-01-18 이 달력에 없다")

    monkeypatch.setattr("app.master.service.get_market_calendar", lambda: object())
    monkeypatch.setattr("app.master.service.build_execution_calendar", 못_덮는다)


def _run(request_id: str):
    return run_procurement(
        ProcurementRunRequest(
            as_of=AS_OF,
            policy_version="v1.3",
            item="배추",
            request_id=request_id,
        ),
        verifier=None,
    )


# ── ① 실으면 DERIVED 로 적힌다 ────────────────────────────────────────────


def test_봉투를_실으면_출처표에_DERIVED_로_적힌다(매입이_받은_payload, monkeypatch):
    """🔴 **전에는 아무 데도 안 적혔다.** 그래서 받는 쪽이 없는 표를 뒤졌다."""
    _봉투가_선다(monkeypatch)
    response = _run("REQ-CAL-1")

    assert KEY in response.input_sources, (
        f"봉투를 실었는데 출처표에 {KEY} 가 없다 — 받는 쪽이 확인할 표가 없다: "
        f"{response.input_sources}"
    )
    assert response.input_sources[KEY] == "DERIVED:market_calendar"


def test_실제로_실린_봉투와_출처표가_같은_사실을_말한다(매입이_받은_payload, monkeypatch):
    """★ **적는 쪽만 재지 않는다.** 표가 「실었다」고 하는데 payload 가 비면 표가 거짓말이다."""
    _봉투가_선다(monkeypatch)
    _run("REQ-CAL-2")

    assert 매입이_받은_payload, "매입이 불려야 이 검사가 의미 있다"
    assert 매입이_받은_payload[0].get(KEY), "출처표는 실었다는데 payload 에 봉투가 없다"
    assert 매입이_받은_payload[0]["input_sources"][KEY] == "DERIVED:market_calendar"


# ── ② 못 실으면 MISSING 으로 적힌다 ───────────────────────────────────────


def test_못_실으면_출처표에_MISSING_으로_적힌다(매입이_받은_payload, monkeypatch):
    """🔴 **키를 통째로 빼지 않는다.**

    빼면 *"안 실렸다"* 가 *"모른다"* 와 섞여 예전과 똑같아진다 — 없는 표를 다시 뒤진다.
    """
    _봉투가_안_선다(monkeypatch)
    response = _run("REQ-CAL-3")

    assert KEY in response.input_sources, (
        f"못 실었는데 키를 통째로 뺐다 — 「없다」와 「모른다」가 섞인다: {response.input_sources}"
    )
    assert response.input_sources[KEY] == "MISSING:-"


def test_못_실은_날에는_payload_에도_봉투가_없다(매입이_받은_payload, monkeypatch):
    """⚠️ `_execution_calendar_payload` 의 판단(*"못 덮으면 통째로 안 싣는다"*)은 그대로다."""
    _봉투가_안_선다(monkeypatch)
    response = _run("REQ-CAL-4")

    assert 매입이_받은_payload, "매입이 불려야 이 검사가 의미 있다"
    assert not 매입이_받은_payload[0].get(KEY), "반쪽 달력을 실었다"
    assert response.input_sources[KEY] == "MISSING:-"


# ── ③ 어휘를 새로 만들지 않는다 ───────────────────────────────────────────


@pytest.mark.parametrize("봉투가_서나", [True, False])
def test_다섯_어휘_밖의_등급을_만들지_않는다(매입이_받은_payload, monkeypatch, 봉투가_서나):
    """★ `REQUEST`·`MEASURED`·`DERIVED`·`MISSING`·`MOCK` 다섯뿐이다."""
    (_봉투가_선다 if 봉투가_서나 else _봉투가_안_선다)(monkeypatch)
    response = _run(f"REQ-CAL-5{int(봉투가_서나)}")

    등급 = response.input_sources[KEY].split(":", 1)[0]
    assert 등급 in {"REQUEST", "MEASURED", "DERIVED", "MISSING", "MOCK"}, (
        f"새 어휘를 만들었다: {등급}"
    )


# ── ④ 달력 축이 주입 축을 안 건드린다 ─────────────────────────────────────


def test_적재층이_읽은_셋은_그대로다(매입이_받은_payload, monkeypatch):
    """★ 이 판이 **안 건드려야 하는 것**이다. 달력 줄 하나만 는다."""
    _봉투가_선다(monkeypatch)
    response = _run("REQ-CAL-6")

    for key, value in _loaded().sources().items():
        assert response.input_sources[key] == value, f"{key} 까지 건드렸다"
    assert not any(v.startswith("REQUEST:") for v in response.input_sources.values())
