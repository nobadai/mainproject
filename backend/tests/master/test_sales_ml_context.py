"""ML 예측이 **판매 경로에도** 실려 가는가 (M-1).

판매는 ML 을 직접 부르지 않는다 — 마스터가 받아서 실어 나른다 (판매 v1.7 §11).
그 운반이 매입 경로에만 있어서, 판매는 시장 예측 없이 후보를 만들고 있었다.

```text
① 실은 것이 그대로 가는가            안 실으면 후보가 시장을 못 보고 만들어진다
② look-ahead 대조가 판매에도 걸리나  🔴 매입에만 있으면 판매는 미래를 보고 오늘을 판단한다
③ 대조가 한 벌인가                   두 벌이면 한쪽만 고쳐지는 날이 온다
④ 없으면 칸을 안 만드나              `None` 을 실으면 "없었다" 와 "안 보냈다" 가 같아진다
⑤ 못 실은 사실이 결과에 남나         멈추지 않는 것과 없던 일로 하는 것은 다르다
⑥ ML 이 에이전트가 아닌가            올리면 데이터 조회에 CallBudget 과 생략 규칙이 걸린다
```

🔴 **②가 이 파일의 이유다.** look-ahead 누수는 **오류를 내지 않는다.** 예측을 하루
  앞당겨 보면 후보의 손익만 좋아지고 화면은 조용하다 — 사람이 알아챌 자리가 없다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, get_args

import pytest

from app.master import (
    AgentRegistry,
    AgentReply,
    AgentRequest,
    CallBudget,
    ExecutionContext,
    ExecutionMetadata,
    MasterRunner,
    envelope,
    persistence,
    wiring,
)
from app.master import flow as procurement_flow
from app.master import sales_flow as sales_flow_module
from app.master.envelope import CAPABILITY_ROUTING, AgentName
from app.master.inputs import SourcedInput
from app.master.sales_flow import SalesFlow
from app.master.schemas import SalesRunRequest
from app.master.service import run_sales

AS_OF = date(2026, 9, 6)


def ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="SREQ-ML-1",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3-PROVISIONAL",
    )


def 예측(generated_at: str = "2026-09-06T06:00:00+09:00") -> dict[str, Any]:
    """ML 봉투 하나. **`load_forecast` 가 내는 모양 그대로다** (`inputs.py`)."""
    return {
        "generated_at": generated_at,
        "item": "배추",
        "unit": "KRW/kg",
        "current_price": 1200.0,
        "horizon_days": 7,
        "daily": [{"d": "2026-09-07", "q50": 1250.0}],
        "model_version": "auc-v3",
        "use_recommended": True,
    }


# ── 가짜 포트 ────────────────────────────────────────────────────────────────


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


def _port(payload: dict[str, Any] | None = None, capture: list | None = None):
    def port(request: AgentRequest):
        if capture is not None:
            capture.append((request.agent, request.mode, dict(request.payload)))
        reply = _reply(request, payload=payload or {})
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


_후보 = {
    "scenarios": [{"scenario_id": "SCN-1", "required_validations": ["FINANCIAL_VALIDATION"]}],
    "situation": "물량이 있다",
}


def _flow(capture: list, **kw) -> SalesFlow:
    registry = AgentRegistry()
    registry.register("inventory", _port({"sellable": "yes"}, capture))
    registry.register("sales", _port(dict(_후보), capture))
    registry.register("finance", _port({"verdict": "ok"}, capture))
    runner = MasterRunner(ctx(), registry, CallBudget(limit=16))
    return SalesFlow(runner, **kw)


def _제안_payload(capture: list) -> dict[str, Any]:
    """판매(`GENERATE_SALES_PROPOSAL`)가 실제로 받은 것."""
    보낸것 = [p for agent, mode, p in capture if mode == "GENERATE_SALES_PROPOSAL"]
    assert 보낸것, "판매를 아예 안 불렀다 — 이 검사가 재려는 자리에 도달하지 못했다"
    return 보낸것[0]


# ---------------------------------------------------------------------------
# ① 실은 것이 그대로 간다
# ---------------------------------------------------------------------------


def test_예측을_판매_제안에_그대로_싣는다():
    """🔴 **없으면 판매가 시장 예측 없이 후보를 만든다.**

    ★ **값을 고르거나 다시 계산하지 않는다** (§3.2.2). 마스터는 받은 봉투를 그대로
      나른다 — 여기서 필드를 골라 담으면 ML 이 칸을 늘린 날 조용히 빠진다.
    """
    capture: list = []
    _flow(capture, forecast=예측()).run()

    assert _제안_payload(capture)["ml_context"] == 예측()


def test_칸_이름은_판매_것이다():
    """받는 쪽 낱말에 맞춘다 — 판매 `SalesProposalInput.ml_context`.

    매입은 같은 값을 `forecast` 로 받는다. 마스터 쪽 이름을 그대로 보내면 판매 문 앞
    (`extra="forbid"`)에서 **통째로 거부된다.**
    """
    from app.sales.schemas import SalesProposalInput

    assert "ml_context" in SalesProposalInput.model_fields

    capture: list = []
    _flow(capture, forecast=예측()).run()
    보낸것 = _제안_payload(capture)
    assert "forecast" not in 보낸것


# ---------------------------------------------------------------------------
# ② look-ahead 대조 — 🔴 매입에만 있으면 판매가 미래를 본다
# ---------------------------------------------------------------------------


def test_오늘_이후에_생성된_예측은_판매에도_안_실린다():
    """🔴 **오염된 입력으로 시나리오를 만들면 백테스트 손익만 좋아진다.**

    매입 `_purchase_input` 이 하던 대조가 판매 경로에도 그대로 걸려야 한다. 안 걸리면
    판매는 *"오늘 이후에 생성된 예측"* 으로 오늘을 판단하는데, **오류가 안 난다.**
    """
    capture: list = []
    _flow(capture, forecast=예측("2026-09-07T06:00:00+09:00")).run()

    assert "ml_context" not in _제안_payload(capture)


def test_타임존이_없는_예측은_안_실린다():
    """앞 10자만 비교하므로 오프셋이 없으면 **대조 자체가 성립하지 않는다.**

    `2026-09-06T23:00` 이 KST 로 09-07 인지 UTC 로 09-06 인지 갈리지 않는다.
    """
    capture: list = []
    _flow(capture, forecast=예측("2026-09-06T23:00:00")).run()

    assert "ml_context" not in _제안_payload(capture)


def test_look_ahead_로_걸린_사실이_결과에_남는다():
    """조용히 빠지면 화면이 *"예측을 보고 만든 후보"* 로 읽는다."""
    outcome = _flow([], forecast=예측("2026-09-07T06:00:00+09:00")).run()

    assert "2026-09-06" in outcome.ml_context_note


# ---------------------------------------------------------------------------
# ③ 대조가 한 벌인가 — 베끼면 한쪽만 고쳐진다
# ---------------------------------------------------------------------------


def test_look_ahead_대조는_매입과_판매가_같은_한_벌이다():
    """🔴 **두 벌이 되는 날은 조용히 온다.**

    타임존 규칙(2026-08-27)이나 비교 자릿수가 한쪽에서만 바뀌면, 그날부터 두 사이클이
    **다른 예측을 같은 이름으로** 나른다. 그래서 봉투에 하나만 둔다.

    ★ 봉투가 주인인 것은 `PASSING_VERDICTS` · `wire_adjustment` 와 같은 이유다 —
      *"무엇을 실어도 되는가"* 는 사이클에 매인 물음이 아니다.
    """
    assert procurement_flow.forecast_is_clean is envelope.forecast_is_clean
    assert sales_flow_module.forecast_is_clean is envelope.forecast_is_clean


def test_매입_Flow_에_사본이_남아_있지_않다():
    """옮겼으면 옛 자리는 비어 있어야 한다. 남아 있으면 그것이 곧 두 벌이다."""
    assert not hasattr(procurement_flow.ProcurementFlow, "_forecast_is_clean")


# ---------------------------------------------------------------------------
# ④ 없으면 칸을 안 만든다 (§1.2-10)
# ---------------------------------------------------------------------------


def test_예측이_없으면_칸을_아예_안_만든다():
    """🔴 **`None` 을 실으면 받는 쪽이 두 가지를 구별할 수 없다.**

    ```text
    예측이 없었다        ML 이 오늘 것을 못 냈다
    마스터가 안 보냈다   운반이 안 붙었거나 look-ahead 로 걸렸다
    ```

    매입 `_purchase_input` 이 같은 이유로 그렇게 한다.
    """
    capture: list = []
    _flow(capture, forecast=None).run()

    assert "ml_context" not in _제안_payload(capture)


def test_되먹임_회차에도_같은_규칙이다():
    """회차가 바뀐다고 칸이 생기거나 사라지지 않는다.

    ★ `as_of` 도 예측도 회차마다 안 바뀌므로 답도 같아야 한다 (§3.4 재현성).
    """
    capture: list = []
    registry = AgentRegistry()
    registry.register("inventory", _port({"sellable": "yes"}, capture))
    registry.register("sales", _port(dict(_후보), capture))
    # 재무가 전부 거절하고 대안을 내면 되먹임이 돈다.
    registry.register(
        "finance",
        _port_reject(capture),
    )
    runner = MasterRunner(ctx(), registry, CallBudget(limit=16))
    SalesFlow(runner, forecast=예측()).run()

    보낸것 = [p for agent, mode, p in capture if mode == "GENERATE_SALES_PROPOSAL"]
    assert len(보낸것) > 1, "되먹임이 안 돌아 이 검사가 재려는 자리에 도달하지 못했다"
    assert all(p["ml_context"] == 예측() for p in 보낸것)


def _port_reject(capture: list):
    """재무 — 거절하면서 **권위 있는 대안**을 낸다. 그래야 되먹임이 돈다."""
    from app.contracts.core import SuggestedAdjustment

    def port(request: AgentRequest):
        capture.append((request.agent, request.mode, dict(request.payload)))
        reply = _reply(
            request,
            business_status="reject",
            reasoning="마진이 안 선다",
            suggested_adjustments=(
                SuggestedAdjustment(
                    dept="finance",
                    axis="amount",
                    target_value=18000000.0,
                    unit="KRW",
                    reason="마진이 안 선다",
                    ref_ids=("FIN-1",),
                ),
            ),
        )
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


# ---------------------------------------------------------------------------
# ⑤ 못 실은 사실이 결과에 남는다
# ---------------------------------------------------------------------------


def test_못_실은_사유를_결과가_들고_있다():
    """판매 v1.7 은 *"ML missing 은 전체 Sales 실패가 아니다"* 라고 했다.

    그래서 Flow 를 세우지 않는다. **다만 조용히 없어지지도 않는다** — 후보의 질이 왜
    떨어졌는지를 나중에 읽는 사람이 볼 수 있어야 한다 (`context_failure` 와 같은 자리).
    """
    outcome = _flow([], forecast=None, forecast_note="ML DB 조회 실패 (OperationalError)").run()

    assert outcome.end_code == "SL1_PRESENTED"  # 세우지 않는다
    assert outcome.ml_context_note == "ML DB 조회 실패 (OperationalError)"


def test_예측이_없는데_사유도_없으면_지어서라도_적는다():
    """빈 문자열은 *"실었다"* 를 뜻한다 — 안 실었는데 비어 있으면 거짓말이 된다."""
    outcome = _flow([], forecast=None).run()

    assert outcome.ml_context_note


def test_실었으면_사유가_비어_있다():
    """대조군이 조용해야 어긋남이 눈에 띈다."""
    outcome = _flow([], forecast=예측()).run()

    assert outcome.ml_context_note == ""


# ---------------------------------------------------------------------------
# ⑥ ML 은 에이전트가 아니라 **입력**이다
# ---------------------------------------------------------------------------


def test_ML_은_호출_대상_어휘에_없다():
    """🔴 에이전트로 올리면 **데이터 조회에 CallBudget 과 생략 규칙이 걸린다.**

    ML 은 호출 구조 밖의 독립 실행이라 부를 대상이 없다 (`inputs.py` 머리말 · §3.2.5).
    마스터가 읽어서 싣는 값이지, 물어보는 상대가 아니다.
    """
    assert "ml" not in get_args(AgentName)
    assert all(route is None or route[0] != "ml" for route in CAPABILITY_ROUTING.values())


# ---------------------------------------------------------------------------
# 진입점 — 매입과 같은 자리에서 읽어 Flow 에 넘긴다
# ---------------------------------------------------------------------------


@pytest.fixture
def _판매_배선(monkeypatch: pytest.MonkeyPatch) -> list:
    capture: list = []
    wiring.reset()
    wiring.register("inventory", _port({"sellable": "yes"}, capture))
    wiring.register("sales", _port(dict(_후보), capture))
    wiring.register("finance", _port({"verdict": "ok"}, capture))
    monkeypatch.setattr(persistence, "record_sales", lambda *a, **k: None)
    return capture


def _요청(**kw) -> SalesRunRequest:
    base = {
        "as_of": AS_OF,
        "policy_version": "v1.3",
        "business_mode": "SPOT_SALES",
        "item": "배추",
    }
    base.update(kw)
    return SalesRunRequest(**base)


def _적재(monkeypatch: pytest.MonkeyPatch, sourced: SourcedInput) -> None:
    """진입점이 부르는 적재층을 갈아 끼운다 — **실 DB 를 안 친다.**"""
    monkeypatch.setattr("app.master.service.load_forecast", lambda *a, **k: sourced)


def test_진입점이_읽어_판매까지_나른다(monkeypatch: pytest.MonkeyPatch, _판매_배선: list) -> None:
    """매입 `_inputs_for` → `ProcurementFlow(forecast=...)` 와 **같은 모양**이다.

    Flow 가 직접 조회하면 조립기가 적재층을 겸하게 되고, 백테스트가 그날 값을 꽂아
    넣을 자리도 사라진다.
    """
    _적재(
        monkeypatch,
        SourcedInput("forecast", 예측(), "MEASURED", "v_ml_price_forecast(as_of=2026-09-06)"),
    )

    run_sales(_요청())

    assert _제안_payload(_판매_배선)["ml_context"] == 예측()


def test_진입점이_못_읽으면_사유가_응답까지_간다(
    monkeypatch: pytest.MonkeyPatch, _판매_배선: list
) -> None:
    """🔴 **못 읽은 이유를 아는 곳은 적재층뿐이다.**

    Flow 는 자기가 아는 이유(look-ahead)만 안다. 적재층 사유를 안 나르면 화면이
    *"예측이 없었다"* 까지만 말하고 왜인지는 아무 데도 안 남는다.
    """
    _적재(
        monkeypatch,
        SourcedInput("forecast", None, "MISSING", "-", "2026-09-06 당일 예측 배치가 없다"),
    )

    response = run_sales(_요청())

    assert "ml_context" not in _제안_payload(_판매_배선)
    assert response.ml_context_note == "2026-09-06 당일 예측 배치가 없다"


def test_품목이_없으면_묻지도_않는다(monkeypatch: pytest.MonkeyPatch, _판매_배선: list) -> None:
    """예측은 품목별이라 물을 대상이 없다 — 매입 `_inputs_for` 와 같은 태도다."""

    def 부르면_터진다(*a, **k):
        raise AssertionError("품목이 없는데 예측을 조회했다")

    monkeypatch.setattr("app.master.service.load_forecast", 부르면_터진다)

    response = run_sales(_요청(item=None))

    assert "품목" in response.ml_context_note


def test_mock_예측이면_판매를_세운다(monkeypatch: pytest.MonkeyPatch, _판매_배선: list) -> None:
    """🔴 **경고와 차단은 다르다** (골격 `SalesFlow.mocked_inputs` · 매입과 같은 태도).

    mock 으로 내린 결론은 실측으로 읽히면 안 되는 정도가 아니라 **아예 내리면 안 되는**
    것이다. 실어 주기 시작했으니 막는 쪽도 같이 잇는다.
    """
    _적재(monkeypatch, SourcedInput("forecast", 예측(), "MOCK", "mocks.py"))

    response = run_sales(_요청())

    assert response.end_code == "SL4_NOT_STARTED"
    assert not _판매_배선, "mock 인데 부서를 불렀다 — 그 회신이 이력에 남는다"
