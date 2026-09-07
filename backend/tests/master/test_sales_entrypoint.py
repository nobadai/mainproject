"""판매 진입점 `run_sales()` — 골격을 실제로 잇고, 이력에 사실대로 남긴다.

설계 2026-09-07 (`판매시나리오/260907_마스터_판매진입점_설계.md`).

```text
① 요청 스키마      🔴 예산 16 · 판매에만 있는 셋 · 매입 전용 사실을 안 싣는다
② 개장 Gate        안 열린 날은 SL4 로 접고 부서를 안 부른다
③ 실행일 Gate 없음  🔴 주말에도 판다
④ 이력 적재        🔴 SL4 를 READY 로 적으면 "못 시작한 날" 이 "돈 날" 로 남는다
⑤ 엔드포인트       사람이 눌러서 시작한다 — 비동기 ack 는 없다
```

★ **골격 자체는 `test_sales_flow.py` 가 잰다.** 여기서 재는 것은 *"진입점이 무엇을
  붙였나"* 다 — 관문 · 예산 · 이력 · 사람이 읽는 문장.
"""

from __future__ import annotations

from datetime import date
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import persistence, wiring
from app.master.day_gate import DayGate
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.router import router
from app.master.sales_flow import SALES_BUDGET
from app.master.schemas import ProcurementRunRequest, SalesBusinessMode, SalesRunRequest
from app.master.service import run_sales

#: 🔴 **토요일이다.** 매입은 이 날 실행일 관문에서 서고 판매는 그대로 간다.
토요일 = date(2026, 9, 12)
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


def _wire(capture: list | None = None) -> list:
    """물류 · 판매 · 재무를 등록한다. 후보 하나가 재무 검증 하나를 요구한다.

    ★ 루트 `conftest.py` 가 등록을 스냅샷/복원하므로 이 테스트 밖으로 안 샌다.
    """
    called = capture if capture is not None else []
    wiring.reset()
    wiring.register("inventory", _port({"sellable": "yes"}, called))
    wiring.register(
        "sales",
        _port(
            {
                "scenarios": [
                    {"scenario_id": "SCN-1", "required_validations": ["FINANCIAL_VALIDATION"]}
                ],
                "situation": "물량이 있다",
            },
            called,
        ),
    )
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
      그래서 저장소를 가로채지 않으면 **매핑이 틀려도 아무 일이 안 일어난다.**
    """
    seen: list[dict[str, Any]] = []

    def fake(**kwargs):
        seen.append(kwargs)
        return "RUN-FAKE-SALES"

    monkeypatch.setattr("app.master.persistence.try_save_run", fake)
    return seen


# ---------------------------------------------------------------------------
# ① 요청 스키마
# ---------------------------------------------------------------------------


def test_판매_예산_기본값은_16_이다():
    """🔴 **매입 12 를 복사하면 요청이 골격을 이긴다** (설계 §3).

    골격에 `SALES_BUDGET = 16` 이 있어도 요청이 12 를 들고 오면 **그 값이 이긴다.**
    소진은 `SL5_BUDGET_EXHAUSTED` 로 남는데, 그건 오류가 아니라 *"판단이 안 끝났다"* 라
    화면이 조용히 덜 본 결과를 보여준다.

    ```text
    후보 3 · 되먹임 2회  물류 1 + 판매 3 + 재무 9 = 13
    +2 물류 재조회 · +1 S-2 재시도                   = 16
    ```
    """
    assert _request().budget == 16
    assert _request().budget == SALES_BUDGET, "골격 상수와 요청 기본값이 갈렸다"


def test_매입_예산과_다르다():
    """★ 사이클이 다르면 예산도 다르다. **매입 12 를 건드려 맞추지 않는다.**"""
    매입 = ProcurementRunRequest(as_of=평일, policy_version="v1.3").budget

    assert 매입 == 12
    assert _request().budget != 매입, "판매 예산이 매입과 같아졌다 — 스키마를 복사하면 이렇게 된다"


def test_영업_모드_어휘가_판매_것과_같다():
    """🔴 **런타임에는 이것이 갈려도 아무 소리가 안 난다.**

    마스터는 `app.sales.schemas` 를 import 하지 않는다 (`Capability` 때와 같은 이유).
    갈리면 판매가 늘린 모드가 마스터 문 앞에서 422 로 막히고, 그건 화면에서
    *"그런 모드는 없다"* 로 읽힌다.

    ★ 테스트에서는 양쪽을 읽어도 된다 — 런타임 의존이 아니다.
    """
    from app.sales.schemas import SalesBusinessMode as 판매_어휘

    assert set(get_args(SalesBusinessMode)) == set(get_args(판매_어휘))


def test_매입_전용_사실은_판매_요청에_없다():
    """🔴 **`has_unmet_obligation` 은 판매가 준 사실이다** (설계 §2).

    매입 `E5` 판정 전용이고 **판매가 준 것을 매입이 쓰는** 값이다. 판매 요청에 되돌려
    실으면 판매가 자기가 준 사실을 자기 입력으로 다시 받는 순환이 된다.
    """
    with pytest.raises(ValueError, match="has_unmet_obligation"):
        _request(has_unmet_obligation=True)


def test_계약_밖_품목은_문_앞에서_걸린다():
    """★ 매입과 **같은 규칙**이다 — 두 사이클이 같은 3품목 계약을 쓴다."""
    with pytest.raises(ValueError, match="피마늘"):
        _request(item="피마늘")


def test_품목을_안_주는_것은_막지_않는다():
    """★ 안 준 것과 없는 것을 준 것은 다르다 — 안 주면 판매가 missing_data 로 답한다."""
    assert _request(item=None).item is None


# ---------------------------------------------------------------------------
# ② 개장 Gate
# ---------------------------------------------------------------------------


def test_안_열린_날은_SL4_로_접힌다(막힌_개장):
    """🔴 **안 열린 장부 위에서 판단하면 없는 상태를 읽거나 남의 날 상태를 읽는다.**"""
    response = run_sales(_request())

    assert response.end_code == "SL4_NOT_STARTED"
    assert response.candidates == [], "안 열린 날인데 후보를 만들었다"


def test_안_열린_날에는_부서를_한_번도_안_부른다(막힌_개장):
    """★ 한 번이라도 부르면 그 회신이 이력에 남고 *"돌긴 돌았다"* 로 읽힌다."""
    called = _wire()

    run_sales(_request())

    assert called == [], f"안 열린 날인데 부서를 불렀다: {called}"


def test_안_열린_날_응답이_관문_결과를_나른다(막힌_개장):
    """★ `end_code` 는 *"시작 안 했다"* 까지만 말한다 — **왜** 인지는 이 블록이 나른다."""
    response = run_sales(_request())

    assert response.day_gate is not None
    assert response.day_gate.gate == "BLOCKED"
    assert response.day_gate.next_action == "RETRY_OPEN_DAY"
    assert "안 열렸다" in response.reason


def test_안_열린_날도_이력에_남는다(막힌_개장, 적재를_지켜본다):
    """🔴 **안 부른 것과 못 시작한 것은 다르다.** 이력이 비면 둘이 같아 보인다."""
    response = run_sales(_request())

    assert len(적재를_지켜본다) == 1
    assert 적재를_지켜본다[0]["end_code"] == "SL4_NOT_STARTED"
    assert response.history_run_id == "RUN-FAKE-SALES", "적재가 돌려준 행 id 를 응답에 안 실었다"


# ---------------------------------------------------------------------------
# ③ 🔴 실행일 Gate 는 없다 — 주말에도 판다
# ---------------------------------------------------------------------------


def test_주말에도_판매는_돈다():
    """🔴 **파는 데는 ML 예측이 필요 없다** (설계 §1).

    같은 토요일에 매입은 실행일 관문에서 선다 (`test_execution_day.py`). 두 사이클이
    같은 날 다르게 답하는 것이 정상이고, 그것이 개장을 달력일로 정한 이유다.
    """
    called = _wire()

    response = run_sales(_request(as_of=토요일))

    assert response.end_code == "SL1_PRESENTED", f"주말 판매가 접혔다: {response.reason}"
    assert [agent for agent, _, _ in called] == ["inventory", "sales", "finance"]


def test_주말_사유에_실행일_이야기가_없다():
    """★ 판매가 접힐 이유에 *"실행일이 아니다"* 가 끼면 매입 어휘가 샌 것이다."""
    _wire()

    response = run_sales(_request(as_of=토요일))

    assert "실행일" not in response.reason, f"판매에 매입 관문 사유가 샜다: {response.reason}"


# ---------------------------------------------------------------------------
# ④ 🔴 이력 적재 — 매입 매핑을 그대로 쓰면 틀린 값이 조용히 들어간다
# ---------------------------------------------------------------------------


def test_SL4_는_미가동으로_적힌다():
    """🔴 **여기가 이 조각에서 가장 조용한 자리다** (설계 §4).

    `runtime_status_of` 는 표에 없는 코드에 **기본값 `READY`** 를 준다. 판매 코드를
    거기 넣으면 걸리지 않고 통과해서 *"못 시작한 날"* 이 *"돈 날"* 로 남는다.
    """
    assert persistence.sales_runtime_status_of("SL4_NOT_STARTED") == "RUNTIME_NOT_READY"


def test_매입_매핑을_그대로_쓰면_SL4_가_READY_로_샌다():
    """★ **왜 매핑을 따로 뒀는지**를 검사가 직접 보여준다.

    누가 `sales_runtime_status_of` 를 지우고 `runtime_status_of` 를 부르게 바꾸면
    이 검사가 그 결과를 말한다 — 예외가 아니라 **틀린 값**이 들어간다는 것을.
    """
    assert persistence.runtime_status_of("SL4_NOT_STARTED") == "READY", (
        "매입 매핑이 판매 코드를 알아보기 시작했다면 이 검사의 전제가 바뀐 것이다"
    )
    assert persistence.sales_runtime_status_of("SL4_NOT_STARTED") != persistence.runtime_status_of(
        "SL4_NOT_STARTED"
    )


def test_예산_소진은_환경_고장이_아니다():
    """🔴 **`ERROR` 로 적으면 어댑터가 죽은 날과 같아 보인다** (설계 §4).

    예산 소진은 마스터가 **스스로 끊은 것**이다 (§1.2-12). `ERROR` 로 적으면 사람이
    어댑터 로그를 뒤지는데, 실제로는 예산을 올리거나 후보 수를 줄일 일이다.

    *"판단이 안 끝났다"* 는 사실은 종료 코드가 이미 말한다 — 겹쳐 적지 않는다.
    """
    assert persistence.sales_runtime_status_of("SL5_BUDGET_EXHAUSTED") == "READY"


@pytest.mark.parametrize("end_code", ["SL1_PRESENTED", "SL2_NO_CANDIDATE", "SL3_ALL_REJECTED"])
def test_나머지는_돌긴_돈_날이다(end_code):
    assert persistence.sales_runtime_status_of(end_code) == "READY"


def test_판매_이력은_SALES_사이클로_적재된다(적재를_지켜본다):
    """★ `cycle` 이 갈려야 조회가 매입 자리에 판매를 띄우지 않는다.

    🟢 `SL*` 은 `end_code` 컬럼에 그대로 들어간다 — CHECK 가 없고, 그건 사이클마다
      어휘가 다르라고 **일부러 열어 둔** 것이다 (`master_agent_runs.sql:64`).
    """
    _wire()

    run_sales(_request())

    row = 적재를_지켜본다[0]
    assert row["cycle"] == "SALES"
    assert row["end_code"] == "SL1_PRESENTED"
    assert row["runtime_status"] == "READY"
    assert row["item"] == "배추", "요청 품목이 이력 칸에 안 실렸다"
    assert row["request_payload"]["business_mode"] == "SPOT_SALES"


def test_적재에_실행_계획이_실린다(적재를_지켜본다):
    """★ 계획이 비면 *"누구를 몇 번째로 불렀나"* 가 이력에서 사라진다 (M-16)."""
    _wire()

    run_sales(_request())

    plan = 적재를_지켜본다[0]["plan"]
    assert [(row["agent"], row["mode"]) for row in plan] == [
        ("inventory", "PRE_SALES"),
        ("sales", "GENERATE_SALES_PROPOSAL"),
        ("finance", "SALES_VALIDATION"),
    ]


# ---------------------------------------------------------------------------
# ⑤ Flow 연결 — 골격이 실제로 돈다
# ---------------------------------------------------------------------------


def test_통과_후보가_그대로_응답에_실린다():
    """★ **마스터가 통과 판정을 다시 세지 않는다** — 후보가 든 답을 옮긴다."""
    _wire()

    response = run_sales(_request())

    assert [c.scenario["scenario_id"] for c in response.candidates] == ["SCN-1"]
    assert response.candidates[0].passed
    assert response.candidates[0].detail == "통과"
    assert response.judgment["situation"] == "물량이 있다", "판매 판정부를 안 날랐다"


def test_사용자_요청을_판매_낱말로_나른다():
    """★ 칸 이름은 받는 쪽 것이다 (`SalesUserRequest` — raw_text · item · partner_id).

    ⚠️ `business_mode` 는 여기 안 들어간다 — 판매 쪽 모델이 `extra="forbid"` 이고 그
      칸은 한 층 위에 있다. 그 자리를 잇는 것은 어댑터 배선 조각이다.
    """
    called = _wire()

    run_sales(_request(user_request="배추 2톤 다음 주에", partner_id="P-1"))

    보낸 = {agent: payload for agent, _, payload in called}
    assert 보낸["sales"]["user_request"] == {
        "raw_text": "배추 2톤 다음 주에",
        "item": "배추",
        "partner_id": "P-1",
    }
    assert "business_mode" not in 보낸["sales"], (
        "판매 모델에 없는 칸을 실었다 — 문 앞에서 통째로 거부된다"
    )


def test_말하지_않은_칸은_안_만든다():
    """★ 빈 값을 실으면 받는 쪽이 *"사용자가 말 안 했다"* 와 구별할 수 없다 (§1.2-10)."""
    called = _wire()

    run_sales(_request(item=None, user_request=None, partner_id=None))

    보낸 = {agent: payload for agent, _, payload in called}
    assert "user_request" not in 보낸["sales"]


def test_예산은_요청_값으로_선다():
    """★ 진입점이 `sales_call_budget` 를 거치는지 — 상한이 실제로 끊는지로 잰다."""
    _wire()

    response = run_sales(_request(budget=1))

    assert response.end_code == "SL5_BUDGET_EXHAUSTED"


def test_어댑터가_없으면_SL4_로_정확히_말하고_끝난다():
    """★ **미등록은 오류가 아니라 상태다** (§5.3). 배선 전에도 그 사실을 말할 수 있다."""
    wiring.reset()

    response = run_sales(_request())

    assert response.end_code == "SL4_NOT_STARTED"
    assert "미등록" in response.reason


# ---------------------------------------------------------------------------
# ⑥ 사람이 읽는 문장 — 마스터가 판매 문장을 짓지 않는다
# ---------------------------------------------------------------------------


def test_제시된_날에는_마스터가_문장을_짓지_않는다():
    """🔴 **추천 문장과 순위는 판매 소유다** (판매 v1.7 §18 · 설계 §5).

    매입은 `render_answer` 로 리포트를 짓지만 판매는 판매 것을 나른다. 여기서 문장을
    만들기 시작하면 마스터가 **두 번째 추천자**가 되고, 화면에 두 벌의 말이 생긴다.
    """
    _wire()

    response = run_sales(_request())

    assert response.report_text == "", f"마스터가 판매 문장을 지었다: {response.report_text}"


def test_접힌_날에는_왜_접혔는지_한_줄이_있다(막힌_개장):
    """★ Flow 가 접히면 판매 문장 자체가 없다 — 그때만 **실행 사실**을 한 줄 적는다."""
    response = run_sales(_request())

    assert "SL4_NOT_STARTED" in response.report_text
    assert "시작하지 못했다" in response.report_text


# ---------------------------------------------------------------------------
# ⑦ 엔드포인트
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_엔드포인트가_그날의_결과를_200_으로_돌려준다(client):
    """★ 후보 없음·미시작은 오류가 아니라 **그날의 결과**다 (§5.3)."""
    _wire()

    r = client.post(
        "/master/sales/run",
        json={
            "as_of": 토요일.isoformat(),
            "policy_version": "v1.3",
            "business_mode": "SPOT_SALES",
            "item": "배추",
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["end_code"] == "SL1_PRESENTED"
    assert body["day_gate"]["gate"] == "PASS"
    assert body["candidates"][0]["passed"] is True


def test_어휘_밖_영업_모드는_문_앞에서_막힌다(client):
    r = client.post(
        "/master/sales/run",
        json={"as_of": 평일.isoformat(), "policy_version": "v1.3", "business_mode": "굿딜"},
    )

    assert r.status_code == 422


def test_비동기_ack_진입점은_만들지_않았다():
    """🔴 **판매는 사람이 눌러서 시작한다** (설계 §6).

    매입 `/master/trigger` 는 ML 완료 이벤트를 받는 스케줄러용이다. 판매에 같은 것을
    만들면 부를 사람이 없는 진입점이 하나 늘 뿐이다.
    """
    경로 = {getattr(route, "path", "") for route in router.routes}

    assert "/master/sales/run" in 경로
    assert "/master/sales/trigger" not in 경로
