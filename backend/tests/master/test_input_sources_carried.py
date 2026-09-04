"""실어 준 값이 **어디서 왔는지**도 같이 나른다.

2026-09-03 · 판매 요청 · 매입 `A-1` 답(ㄴ).

🔴 마스터는 등급을 안다. 그런데 **부서에게는 값만 보냈다.**

```python
# app/master/service.py:283  (이 판 이전)
sourced = getattr(inputs, key)
return sourced.payload if sourced.usable else None   # grade · source · note 를 버린다
```

```text
응답(화면)     input_sources · mocked_inputs   → 사람은 안다
부서 payload   아무것도 없음                    → 기계는 모른다
```

⚠️ **부서가 스스로 조심하는 수밖에 없었고 그건 계약이 아니라 습관이다.**
매입 `#190` 이 *"`ci_width_threshold` 는 mock 시연값"* 이라 적었는데, 정작 매입은
자기가 받은 예측이 mock 인지 **payload 로는 몰랐다.**

★ **응답과 같은 이름을 쓴다** (`input_sources`). 화면에서 본 것과 payload 를 대조할
  때 이름이 갈리면 안 된다 — 매입이 ㄱ(forecast 블록 안)을 반대한 이유다.

⚠️ **읽을 계획이 없어도 나른다.** `#127` 이 풀려 `ci_width_threshold` 를 재산정하는
  날 *"이 값이 mock 에서 나왔나"* 가 판정에 직접 걸린다.
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
SOURCES = {
    "forecast": "MOCK:purchase_agent/mocks",
    "confirmed_orders": "DERIVED:v_current_partner_demand",
    "policy_values": "DERIVED:agent_policy_config",
}


def _ctx() -> ExecutionContext:
    return ExecutionContext("REQ-20251231-0001", AS_OF, "USER_REQUEST", "v1.3")


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


def _seen() -> tuple[list[dict[str, Any]], Any]:
    """매입이 **실제로 받은 payload** 를 모은다."""
    got: list[dict[str, Any]] = []

    def purchase(request: AgentRequest):
        got.append(dict(request.payload))
        return _port({"scenarios": [{"scenario_id": "SCN-1"}]})(request)

    return got, purchase


def _flow(**kw: Any) -> tuple[list[dict[str, Any]], ProcurementFlow]:
    got, purchase = _seen()
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


# ── ① 핵심 — 출처가 payload 로 간다 ────────────────────────────────────────


def test_출처가_매입_payload_에_실린다():
    """🔴 **이 파일의 주장이다.** 전에는 값만 갔다."""
    got, flow = _flow(input_sources=SOURCES)
    flow.run()

    assert got, "매입이 불려야 이 검사가 의미 있다"
    assert got[0]["input_sources"] == SOURCES


def test_응답과_같은_이름을_쓴다():
    """★ 화면에서 본 것과 payload 를 대조할 때 이름이 갈리면 안 된다 (매입 A-1)."""
    from app.master.schemas import ProcurementRunResponse

    assert "input_sources" in ProcurementRunResponse.model_fields


def test_forecast_블록_안에_넣지_않는다():
    """⚠️ **ML 이 안 보낸 키를 얹으면 받는 쪽이 "ML 이 준 것" 으로 읽는다.**

    매입이 ㄱ 을 반대한 이유이고, `_FORECAST_ENVELOPE_KEYS` 가 *"ML 봉투에서
    내려보내는 필드"* 라는 정의를 지키는 자리다.
    """
    got, flow = _flow(
        input_sources=SOURCES,
        forecast={"generated_at": "2025-12-30T06:00:00+09:00", "horizon_days": 18},
    )
    flow.run()

    assert "input_sources" not in got[0]["forecast"], "예측 블록을 오염시켰다"
    assert "input_sources" in got[0], "최상위에 있어야 한다"


def test_출처가_없으면_칸을_안_만든다():
    """**없는 것을 만들지 않는다.** 빈 dict 를 실으면 *"모른다"* 와 *"셋 다 정상"* 이 같아진다."""
    got, flow = _flow()
    flow.run()

    assert "input_sources" not in got[0]


# ── ② mock 이라는 사실이 실제로 실린다 ─────────────────────────────────────


def test_mock_이라는_사실이_기계가_읽는_자리에_온다():
    """🔴 **이것이 이 판의 목적이다.**

    매입 `#190` 이 *"`ci_width_threshold` 는 mock 시연값"* 이라 적었는데, 정작
    매입은 자기가 받은 예측이 mock 인지 payload 로 몰랐다.
    """
    got, flow = _flow(input_sources=SOURCES)
    flow.run()

    assert got[0]["input_sources"]["forecast"].startswith("MOCK"), (
        "mock 인 사실이 부서에 안 갔다 — 부서가 스스로 조심하는 수밖에 없어진다"
    )


def test_재호출에도_같이_간다():
    """2회차 payload 에도 있어야 한다 — 회차마다 출처가 달라지지 않는다."""
    got, purchase = _seen()
    registry = AgentRegistry()
    registry.register("finance", _port())

    def rejecting(request: AgentRequest):
        reply, meta = _port()(request)
        if request.mode != "PRE_PURCHASE":
            reply = AgentReply(
                request_id=request.context.request_id,
                as_of=request.context.as_of,
                agent=request.agent,
                mode=request.mode,
                run_id=reply.run_id,
                runtime_status="READY",
                business_status="reject",
            )
        return reply, meta

    registry.register("inventory", rejecting)
    registry.register("purchase", purchase)
    ProcurementFlow(
        MasterRunner(_ctx(), registry, CallBudget(limit=12)),
        verifier=None,
        item="배추",
        input_sources=SOURCES,
    ).run()

    assert len(got) == 2, "재호출이 일어나야 이 검사가 의미 있다"
    assert got[1]["input_sources"] == SOURCES


# ── ③ 🔴 나르는 쪽 — service 배선 ──────────────────────────────────────────


def test_service_가_출처를_flow_에_넘긴다(monkeypatch):
    """🔴 **만드는 쪽만 재면 여기서 끊긴다.**

    위 검사들은 `ProcurementFlow` 를 **직접 세워** `input_sources` 를 넘긴다.
    `run_procurement` 이 `inputs.sources()` 를 안 넘겨도 전부 통과한다 —
    실제로 그 변이가 살아남았다 (실측 2026-09-03).

    ★ `#202` · `#206` 에 이어 **세 번째로 같은 모양**이다. 옮기는 쪽을 재는
      검사를 짝으로 두지 않으면 배선이 끊겨도 초록불이다.
    """
    from app.master import wiring
    from app.master.inputs import MasterInputs, SourcedInput
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    # ⚠️ 등급이 `MEASURED` 다. 전에는 `MOCK` 이었는데, 2026-09-03 부터 mock 입력은
    #   부서를 부르기 전에 실행을 세운다 — 그러면 이 검사가 재려는 것(출처가 매입까지
    #   가는가)을 못 잰다. 막는 쪽은 아래 짝 검사가 따로 본다.
    loaded = MasterInputs(
        forecast=SourcedInput("forecast", {"horizon_days": 18}, "MEASURED", "뷰", ""),
        confirmed_orders=SourcedInput("confirmed_orders", {"total_kg": 1.0}, "DERIVED", "뷰", ""),
        policy_values=SourcedInput("policy_values", {"contract_price_krw": 1}, "DERIVED", "표", ""),
    )
    monkeypatch.setattr("app.master.service.collect_inputs", lambda *a, **k: loaded)
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: None)

    got, purchase = _seen()
    wiring.reset()
    wiring.register("finance", _port())
    wiring.register("inventory", _port())
    wiring.register("purchase", purchase)

    run_procurement(
        ProcurementRunRequest(
            as_of=AS_OF, policy_version="v1.3", item="배추", request_id="REQ-SRC-1"
        ),
        verifier=None,
    )

    assert got, "매입이 불려야 이 검사가 의미 있다"
    assert got[0]["input_sources"]["forecast"].startswith("MEASURED"), (
        f"service 가 출처를 안 넘겼다: {got[0].get('input_sources')}"
    )


def test_service_가_mock_차단도_flow_에_넘긴다(monkeypatch):
    """🔴 **같은 파일이 경고한 그 짝이다** (2026-09-03).

    위 검사가 *"옮기는 쪽을 재는 검사를 짝으로 두지 않으면 배선이 끊겨도 초록불"* 이라
    적어 뒀다. `mocked_inputs` 배선에 같은 짝을 둔다.

    ★ 재는 것은 **부서를 안 부른다**는 것이다. 한 번이라도 부르면 그 회신이 이력에
      남고, 나중에 읽는 사람이 *"돌긴 돌았다"* 로 읽는다.
    """
    from app.master import wiring
    from app.master.inputs import MasterInputs, SourcedInput
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    loaded = MasterInputs(
        forecast=SourcedInput("forecast", {"horizon_days": 18}, "MOCK", "mocks", ""),
        confirmed_orders=SourcedInput("confirmed_orders", {"total_kg": 1.0}, "DERIVED", "뷰", ""),
        policy_values=SourcedInput("policy_values", {"contract_price_krw": 1}, "DERIVED", "표", ""),
    )
    monkeypatch.setattr("app.master.service.collect_inputs", lambda *a, **k: loaded)
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: None)

    got, purchase = _seen()
    wiring.reset()
    wiring.register("finance", _port())
    wiring.register("inventory", _port())
    wiring.register("purchase", purchase)

    response = run_procurement(
        ProcurementRunRequest(
            as_of=AS_OF, policy_version="v1.3", item="배추", request_id="REQ-SRC-2"
        ),
        verifier=None,
    )

    assert not got, "mock 입력인데 매입을 불렀다 — 차단 배선이 끊겼다"
    assert response.end_code == "E4_NOT_STARTED"
    assert "forecast" in response.reason, f"어느 입력이 mock 인지 안 말한다: {response.reason}"


# ── ④ ML 신뢰도 — use_recommended ─────────────────────────────────────────


def test_use_recommended_가_예측_payload_에_실린다():
    """🔴 매입 `#192` 가 *"payload 에 칸이 없다"* 로 진단했는데 **절반만 맞았다.**

    ```text
    is_filled · is_gated   행별   뷰가 daily[] 안에 넣어 이미 간다
    use_recommended        조합별  여기서 버리고 있었다
    ```
    """
    from app.master.inputs import _forecast_payload

    row = {
        "generated_at": "2025-12-31T06:00:00+09:00",
        "item": "배추",
        "unit": "원/kg",
        "current_price": 1650,
        "horizon_days": 18,
        "daily": [{"date": "2026-01-02", "predicted": 1700, "is_filled": False, "is_gated": False}],
        "model_version": "v1",
        "use_recommended": False,
    }

    out = _forecast_payload(row)

    assert out["use_recommended"] is False, "ML 이 '쓰지 말라' 고 한 사실이 안 갔다"
    assert out["daily"] is row["daily"], "daily 를 풀어 다시 조립하면 ML 이 준 모양이 바뀐다"


def test_행별_플래그는_daily_안에_그대로_있다():
    """★ `daily` 는 뷰가 만든 그대로 나른다 — 마스터가 손대지 않는다."""
    from app.master.inputs import _forecast_payload

    daily = [{"date": "2026-01-02", "predicted": 1700, "is_filled": True, "is_gated": True}]
    out = _forecast_payload(
        {
            "generated_at": "x", "item": "배추", "unit": "원/kg", "current_price": 1,
            "horizon_days": 1, "daily": daily, "model_version": "v1",
            "use_recommended": True,
        }
    )

    assert out["daily"][0]["is_filled"] is True
    assert out["daily"][0]["is_gated"] is True
