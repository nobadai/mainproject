"""주입한 값을 **주입이라고 적는다** — 출처표가 DB 를 가리키면 안 된다.

매입 실측 2026-09-07.

`/master/request` 는 요청 본문에 `forecast` · `confirmed_orders` · `policy_values` 를
실을 수 있고(백테스트 통로), 그러면 **그 값이 DB 를 이긴다.**

```python
# app/master/service.py
forecast=request.forecast or _payload(inputs, "forecast"),
```

🔴 그런데 출처표가 그 사실을 안 적었다.

```text
셋 다 주입     inputs=None → input_sources {} · mocked_inputs ()  → 화면에 경고 0건
forecast 만    collect_inputs 가 DB 를 읽어 "MEASURED:v_ml_price_forecast" 를 적었다
               🔴 실제로 쓴 값은 주입분인데 출처는 DB 로 나갔다
```

★ **빈 것은 "모른다" 이지만 틀린 출처는 "안다고 잘못 말하는 것" 이다.** 뒤가 나쁘다.

⚠️ **`mocked_inputs` 와 섞지 않는다.** 그것은 `grade == "MOCK"` 만 세고 실행을 세우는
  데 쓴다. 주입은 세울 일이 아니라 적을 일이라 **다른 사실**이다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import wiring
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.inputs import MasterInputs, SourcedInput
from app.master.schemas import ProcurementRunRequest
from app.master.service import run_procurement

AS_OF = date(2025, 12, 31)

FORECAST = {"generated_at": "2025-12-31T06:00:00+09:00", "horizon_days": 18}
ORDERS = {"as_of": "2025-12-31", "item": "배추", "orders": [], "total_kg": 1.0}
POLICY = {"item_mix_ratio": {"배추": 0.4}}


def _loaded() -> MasterInputs:
    """DB 에서 셋 다 읽힌 상태. **주입이 이것을 이기는지**를 보려고 둔다."""
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
    """⚠️ **DB 를 치지 않는다.** `collect_inputs` 는 대역으로 갈아 끼운다."""
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


def _run(request_id: str, **injected: Any):
    return run_procurement(
        ProcurementRunRequest(
            as_of=AS_OF,
            policy_version="v1.3",
            item="배추",
            request_id=request_id,
            **injected,
        ),
        verifier=None,
    )


# ── ① 셋 다 주입 — 출처표가 비지 않는다 ───────────────────────────────────


def test_셋_다_주입하면_세_키가_전부_REQUEST(매입이_받은_payload):
    """🔴 **전에는 `input_sources` 가 통째로 비었다.**

    `_inputs_for` 가 `None` 을 돌려주고 `input_sources={}` 가 됐다 — 화면에 경고
    0건이다. 실제로 쓴 값은 셋 다 요청이 준 것이다.
    """
    response = _run("REQ-INJ-1", forecast=FORECAST, confirmed_orders=ORDERS, policy_values=POLICY)

    assert response.input_sources, "셋 다 주입인데 출처표가 비었다 — 화면이 침묵한다"
    for key in ("forecast", "confirmed_orders", "policy_values"):
        assert response.input_sources[key].startswith("REQUEST:"), (
            f"{key} 를 주입했는데 출처가 {response.input_sources.get(key)!r} 이다"
        )


# ── ② 일부만 주입 — 그 키만 바뀐다 ────────────────────────────────────────


def test_forecast_만_주입하면_그_키만_REQUEST(매입이_받은_payload):
    """🔴 **이쪽이 더 나쁜 갈래였다.**

    하나라도 빠지면 `collect_inputs` 가 DB 를 읽어 `MEASURED:...` 를 적는다.
    **실제로 쓴 값은 주입분인데 출처는 DB 로 나갔다** — 틀린 사실을 적극적으로
    말한다.
    """
    response = _run("REQ-INJ-2", forecast=FORECAST)

    assert response.input_sources["forecast"] == "REQUEST:forecast"
    assert response.input_sources["confirmed_orders"] == "DERIVED:뷰", "주입 안 한 키까지 건드렸다"
    assert response.input_sources["policy_values"] == "DERIVED:표", "주입 안 한 키까지 건드렸다"


# ── ③ 회귀 — 주입이 없으면 종전과 완전히 같다 ─────────────────────────────


def test_주입이_없으면_종전과_같다(매입이_받은_payload):
    """★ 이 판이 **안 건드려야 하는 것**이다. 주입 0건이면 적재층이 읽은 셋이 그대로다.

    ⚠️ `execution_calendar` 는 주입 축이 아니라 **달력 축**이라 여기서 빼고 본다
      (`#300` · `test_execution_calendar_in_sources.py` 가 그쪽을 잰다). 표 전체를
      통째로 대조하면 이 검사가 달력 판마다 같이 깨진다.
    """
    response = _run("REQ-INJ-3")

    적재층이_읽은_셋 = {
        key: value
        for key, value in response.input_sources.items()
        if key in ("forecast", "confirmed_orders", "policy_values")
    }
    assert 적재층이_읽은_셋 == _loaded().sources()
    assert not any(v.startswith("REQUEST:") for v in response.input_sources.values())


# ── ④ 화면 — 사람이 읽는 문장에도 나간다 ──────────────────────────────────


def test_주입_사실이_화면_문구에_나간다(매입이_받은_payload):
    """🔴 **출처표만 고치면 표를 안 읽는 사람에게는 여전히 안 보인다.**

    `answer.py` 가 `mocked_inputs` 경고와 **별도 줄**로 낸다 — 주입은 mock 이
    아니라 섞으면 둘 다 못 읽는다.
    """
    response = _run("REQ-INJ-4", forecast=FORECAST)

    assert response.report_text, "화면 문구가 없다"
    assert "요청이 직접 준 값" in response.report_text, (
        f"주입 사실이 화면에 안 갔다: {response.report_text}"
    )
    assert "forecast" in response.report_text


def test_주입이_없으면_화면에_그_문구가_없다(매입이_받은_payload):
    """★ 없는 경고를 만들지 않는다 — 매번 뜨는 경고는 아무도 안 읽는다."""
    response = _run("REQ-INJ-5")

    assert "요청이 직접 준 값" not in (response.report_text or "")


# ── ⑤ mock 과 안 섞인다 ───────────────────────────────────────────────────


def test_주입은_mocked_inputs_에_안_들어간다(매입이_받은_payload):
    """⚠️ **다른 사실이다.**

    `mocked_inputs` 는 `grade == "MOCK"` 만 세고, 세면 `ProcurementFlow` 가 실행을
    **세운다.** 주입을 거기 넣으면 백테스트가 통째로 안 돈다.
    """
    response = _run("REQ-INJ-6", forecast=FORECAST, confirmed_orders=ORDERS, policy_values=POLICY)

    assert response.mocked_inputs == [], f"주입을 mock 으로 셌다: {response.mocked_inputs}"
    assert response.end_code != "E4_NOT_STARTED", "주입 때문에 실행이 섰다"


# ── ⑥ 배선 — 부서 payload 도 같은 표를 받는다 ─────────────────────────────


def test_부서도_같은_출처표를_받는다(매입이_받은_payload):
    """★ 화면과 payload 가 **한 표를 읽는다** (`test_input_sources_carried.py`).

    옮기는 쪽을 재는 짝이 없으면 배선이 끊겨도 초록불이다.
    """
    response = _run("REQ-INJ-7", forecast=FORECAST)

    assert 매입이_받은_payload, "매입이 불려야 이 검사가 의미 있다"
    assert 매입이_받은_payload[0]["input_sources"] == response.input_sources
    assert 매입이_받은_payload[0]["input_sources"]["forecast"] == "REQUEST:forecast"
