"""마스터가 판매에 보내는 **되먹임이 판매 계약 모양인가** — 그리고 **실제로 닿는가.**

🔴 **되먹임이 한 번도 안 돌았다** (실측 2026-09-07).

```text
어댑터가 읽는 자리   request.payload["feedback_attempt"]   ← 최상위
마스터가 넣던 자리   payload["feedback_context"] 안        ← 중첩
결과                되먹임 회차에도 feedback_attempt=0 · is_refeed=False
```

되먹임 상한 2 를 마스터 소유로 정해 놓고(`MAX_FEEDBACK_ATTEMPTS`) **회차가 전달되지
않았다.** 어댑터가 없던 동안은 되먹임이 안 돌아 안 드러났다 — `#380` 이 실 어댑터로
경로를 이으면서 비로소 잴 수 있는 자리가 됐다.

`feedback_context` 라는 이름 자체도 `SalesProposalInput` 에 없다. 어댑터가 모르는 키를
거르므로 **문 앞에서 거부되지도 않고 조용히 버려졌다** — 그래서 마스터 쪽에서는 아무
소리가 안 났다.

---

**판매·물류 회신으로 확정된 계약** (2026-09-07).

```text
payload
├─ feedback_attempt                        최상위. 마스터가 소유하는 회차
└─ feedback                                 SalesFeedback
     ├─ original_run_id   최초 판매 제안을 만든 run
     ├─ attempt           최상위 feedback_attempt 와 **같은 값**
     ├─ domain_replies[]  source_agent · capability · reply_ref
     │                    · runtime_status · business_status · payload
     └─ scenario_feedback[]  scenario_id · reply_refs
```

★ **두 층으로 잰다.**

  ① 아래 §1~§4 는 가짜 포트로 **봉투 모양**을 잰다 — 빠르고 갈래를 만들기 쉽다.
  ② §5 는 `app.main` 이 등록한 **실 판매 어댑터**로 *"회차가 정말 오르는가"* 를
    잰다 (`test_sales_registration.py` 와 같은 방식). ①만 있으면 마스터가 자기
    모양을 자기가 확인하는 것이라, **받는 쪽이 그것을 어떻게 읽는지**는 아무도 안 본다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main  # import 시점에 판매 어댑터를 등록한다. §5 의 전제다
from app.contracts.core import SuggestedAdjustment
from app.master import wiring
from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.ports import AgentRegistry
from app.master.runner import MasterRunner
from app.master.sales_flow import MAX_FEEDBACK_ATTEMPTS, SalesFlow
from app.sales.schemas import SalesFeedback, SalesProposalInput, SalesProposalReply
from tests.master.logistics_pre_sales import PRE_SALES_PAYLOAD

오늘 = date(2026, 9, 10)


# ---------------------------------------------------------------------------
# 가짜 포트 — 봉투는 진짜다
# ---------------------------------------------------------------------------


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-FB",
        as_of=오늘,
        trigger="USER_REQUEST",
        policy_version="v1.3-PROVISIONAL",
    )


def _reply(request: AgentRequest, **kw: Any) -> AgentReply:
    base: dict[str, Any] = {
        "request_id": request.context.request_id,
        "as_of": request.context.as_of,
        "agent": request.agent,
        "mode": request.mode,
        "run_id": f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}",
        "runtime_status": "READY",
        "business_status": "ok",
    }
    base.update(kw)
    return AgentReply(**base)


def _meta(request: AgentRequest, reply: AgentReply) -> ExecutionMetadata:
    return ExecutionMetadata(
        run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
    )


def _adjustment() -> SuggestedAdjustment:
    return SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=900.0,
        unit="kg",
        reason="한도 초과",
        ref_ids=("REF-1",),
    )


def _물류(payload: dict[str, Any] | None = None):
    def port(request: AgentRequest):
        reply = _reply(request, payload=payload or PRE_SALES_PAYLOAD)
        return reply, _meta(request, reply)

    return port


def _판매(요구: tuple[str, ...], 보낸것: list[dict]):
    def port(request: AgentRequest):
        보낸것.append(dict(request.payload))
        reply = _reply(
            request,
            payload={
                "situation": "다",
                "scenarios": [{"scenario_id": "SCN-1", "required_validations": list(요구)}],
            },
        )
        return reply, _meta(request, reply)

    return port


def _재무(payload: dict[str, Any] | None = None):
    def port(request: AgentRequest):
        reply = _reply(
            request,
            business_status="reject",
            payload=payload or {"verdict": "FAIL"},
            reasoning="한도 초과",
            suggested_adjustments=(_adjustment(),),
        )
        return reply, _meta(request, reply)

    return port


def _돌린다(요구: tuple[str, ...] = ("FINANCIAL_VALIDATION",), **포트) -> list[dict]:
    """되먹임이 두 번 도는 실행. **판매에 실제로 나간 payload 목록을 돌려준다.**"""
    보낸것: list[dict] = []
    registry = AgentRegistry()
    registry.register("inventory", 포트.get("inventory") or _물류())
    registry.register("sales", _판매(요구, 보낸것))
    registry.register("finance", 포트.get("finance") or _재무())
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=16))
    SalesFlow(runner, business_mode="SPOT_SALES").run()
    return 보낸것


# ---------------------------------------------------------------------------
# 1. 회차 — 최상위이고, 두 자리가 같은 값이다
# ---------------------------------------------------------------------------


def test_회차는_최상위에_실린다():
    """🔴 **이 한 줄이 되먹임이 도는지를 가른다.**

    어댑터는 `request.payload["feedback_attempt"]` 를 **최상위에서** 읽는다
    (`app/sales/adapter.py` `_proposal_input`). 중첩된 자리에 넣으면 되먹임 회차에도
    `0` 이 나가고 `is_refeed` 가 영원히 `False` 다.
    """
    보낸것 = _돌린다()

    assert [p["feedback_attempt"] for p in 보낸것] == [0, 1, 2], (
        f"회차가 최상위에서 오르지 않는다: {[p.get('feedback_attempt') for p in 보낸것]}"
    )


def test_최초_호출도_회차를_싣는다():
    """★ 칸이 회차마다 생겼다 사라지면 이력에서 두 모양을 비교해야 한다."""
    보낸것 = _돌린다()

    assert 보낸것[0]["feedback_attempt"] == 0
    assert "feedback" not in 보낸것[0], "되먹임이 아닌데 되먹임 칸이 있다"


def test_회차와_되먹임_안_회차가_같다():
    """🔴 **다르면 판매가 계약 오류로 닫는다** (판매 회신 2026-09-07).

    같은 값을 두 곳에 적는 자리라 **한 문장이 두 곳을 쓴다** (`_proposal_input`).
    `_feedback` 이 자기 몫을 따로 찍으면 두 값이 갈리는 날이 오고, 그날 되먹임이
    통째로 거부되는데 마스터 쪽에서는 아무 소리가 안 난다.
    """
    보낸것 = _돌린다()

    for payload in 보낸것[1:]:
        assert payload["feedback"]["attempt"] == payload["feedback_attempt"], (
            f"두 자리의 회차가 다르다: {payload['feedback_attempt']} vs "
            f"{payload['feedback']['attempt']}"
        )


def test_is_refeed_를_싣지_않는다():
    """🔴 **주인은 하나다** — 판매가 `feedback_attempt > 0` 으로 정한다 (판매 회신).

    같이 실으면 *"되먹임인가"* 의 주인이 둘이 되고, 한쪽만 채워지는 날 판매가
    1회차를 최초 호출로 읽는다.
    """
    보낸것 = _돌린다()

    assert all("is_refeed" not in p for p in 보낸것), "회차와 같은 사실을 두 칸으로 보낸다"


def test_칸_이름이_feedback_이다():
    """🔴 `feedback_context` 는 `SalesProposalInput` 에 없는 이름이라 조용히 버려진다."""
    보낸것 = _돌린다()

    assert "feedback" in 보낸것[1]
    assert "feedback_context" not in 보낸것[1], "판매가 모르는 이름으로 보낸다"


# ---------------------------------------------------------------------------
# 2. domain_replies — 원본을 나른다
# ---------------------------------------------------------------------------


def test_물류_회신은_logistics_로_나간다():
    """🔴 **마스터 `AgentName` 은 `inventory` 라 그대로 보내면 Literal 에 걸린다.**

    ★ **이름 매핑이지 의미 이동이 아니다** (`FEEDBACK_SOURCE_AGENT`). 바뀌는 것은
      부서를 부르는 낱말 하나뿐이고 상태·payload 는 부서가 보낸 그대로다.
    """
    보낸것 = _돌린다(요구=("SELLABLE_SUPPLY_CONTEXT", "FINANCIAL_VALIDATION"))

    출처 = {r["source_agent"] for r in 보낸것[1]["feedback"]["domain_replies"]}
    assert 출처 == {"logistics", "finance"}, f"판매 어휘가 아닌 이름이 나간다: {출처}"


def test_회신_원본을_그대로_나른다():
    """🔴 **재작성하지 않는다** (판매 회신). 마스터가 요약한 문장을 보내지 않는다."""
    보낸것 = _돌린다(finance=_재무({"verdict": "FAIL", "한도": 900}))

    회신 = 보낸것[1]["feedback"]["domain_replies"][0]
    assert 회신["capability"] == "FINANCIAL_VALIDATION"
    assert 회신["runtime_status"] == "READY"
    assert 회신["business_status"] == "reject"
    assert 회신["payload"]["한도"] == 900, "부서 payload 가 마스터를 거치며 바뀌었다"


def test_reply_ref_가_회신_run_id_다():
    """C-4 합의 — 회신 한 번을 가리키는 유일한 키다."""
    보낸것 = _돌린다()

    회신 = 보낸것[1]["feedback"]["domain_replies"][0]
    assert 회신["reply_ref"] == "FINANCE-SAL-1", f"회신 run_id 가 아니다: {회신['reply_ref']}"


def test_deprecated_scenario_id_를_새로_채우지_않는다():
    """D-2 합의 — 후보 ↔ 회신 연결의 주인은 `scenario_feedback` 이다."""
    보낸것 = _돌린다()

    for 회신 in 보낸것[1]["feedback"]["domain_replies"]:
        assert "scenario_id" not in 회신, "deprecated 칸을 새로 채운다 — 연결 주인이 둘이 된다"


def test_계약에_없는_칸을_보내지_않는다():
    """🔴 `SalesDomainReply` 는 `extra="forbid"` 다. 한 칸만 더 실어도 통째로 거부된다."""
    허용 = {
        "source_agent",
        "capability",
        "reply_ref",
        "runtime_status",
        "business_status",
        "payload",
    }
    보낸것 = _돌린다(요구=("SELLABLE_SUPPLY_CONTEXT", "FINANCIAL_VALIDATION"))

    for 회신 in 보낸것[1]["feedback"]["domain_replies"]:
        assert set(회신) <= 허용, f"계약에 없는 칸이 나간다: {set(회신) - 허용}"


def test_같은_회신을_두_번_싣지_않는다():
    """★ S-1 재사용으로 물류 회신 하나가 두 capability 에 걸린다.

    같은 (capability, 회신) 짝은 한 번만 실린다 — 목록은 회신을 나르고, 후보와의
    연결은 `scenario_feedback` 이 한다. 그래서 두 칸이 나뉘어 있다.
    """
    보낸것 = _돌린다(
        요구=("SELLABLE_SUPPLY_CONTEXT", "DELIVERY_FEASIBILITY_CONTEXT", "FINANCIAL_VALIDATION")
    )

    회신들 = 보낸것[1]["feedback"]["domain_replies"]
    참조 = [r["reply_ref"] for r in 회신들]
    assert 참조.count("INVENTORY-PRE-1") == 2, "capability 가 둘이면 두 줄이 맞다"
    assert len({(r["capability"], r["reply_ref"]) for r in 회신들}) == len(회신들), (
        f"같은 회신이 같은 capability 로 두 번 실린다: {회신들}"
    )


# ---------------------------------------------------------------------------
# 3. 조정안 — 그 회신 안에 있다
# ---------------------------------------------------------------------------


def test_조정안이_그_회신_payload_안에_있다():
    """④ **최상위 `adjustments` 칸을 없앤다** (판매 회신 2026-09-07).

    `SuggestedAdjustment` 는 그 부서 회신의 일부다. 최상위로 빼면 **어느 회신이 낸
    대안인지**가 사라지고, 같은 사실이 두 곳에 앉는다.
    """
    보낸것 = _돌린다()

    assert "adjustments" not in 보낸것[1], "조정안이 최상위로도 나간다"
    회신 = 보낸것[1]["feedback"]["domain_replies"][0]
    assert [a["axis"] for a in 회신["payload"]["suggested_adjustments"]] == ["amount"]


def test_대안을_안_낸_회신에는_칸을_안_만든다():
    """§1.2-10 — 빈 목록을 실으면 판매가 *"대안이 없다고 했다"* 로 읽는다."""
    보낸것 = _돌린다(요구=("SELLABLE_SUPPLY_CONTEXT", "FINANCIAL_VALIDATION"))

    물류회신 = next(
        r for r in 보낸것[1]["feedback"]["domain_replies"] if r["source_agent"] == "logistics"
    )
    assert "suggested_adjustments" not in 물류회신["payload"]


# ---------------------------------------------------------------------------
# 4. scenario_feedback · 왕복
# ---------------------------------------------------------------------------


def test_후보와_회신을_연결만_한다():
    """🔴 **마스터가 부서 내용을 요약해 scenario 별 payload 를 만들지 않는다** (판매 회신).

    요약을 끼워 넣으면 판매가 읽는 사실의 주인이 마스터가 된다 (§3.2.2).
    """
    보낸것 = _돌린다(요구=("SELLABLE_SUPPLY_CONTEXT", "FINANCIAL_VALIDATION"))

    연결 = 보낸것[1]["feedback"]["scenario_feedback"]
    assert [c["scenario_id"] for c in 연결] == ["SCN-1"]
    assert 연결[0]["reply_refs"] == ["INVENTORY-PRE-1", "FINANCE-SAL-1"]
    assert set(연결[0]) == {"scenario_id", "reply_refs"}, (
        f"연결 말고 다른 것이 들어 있다: {set(연결[0]) - {'scenario_id', 'reply_refs'}}"
    )


def test_최초_제안_run_이_계보로_남는다():
    """되먹임이 여러 번 돌아도 **최초** 를 가리킨다 — 덮으면 *"직전"* 이 된다."""
    보낸것 = _돌린다()

    assert 보낸것[1]["feedback"]["original_run_id"] == "SALES-GEN-1"
    assert 보낸것[2]["feedback"]["original_run_id"] == "SALES-GEN-1", (
        "2차 되먹임에서 원본이 1회차로 덮였다"
    )


def test_전선에_실은_것은_왕복해도_같다():
    """🔴 **`test_adjustment_wire_format.py` 와 같은 기준이다** (#175).

    `asdict` 는 튜플을 그대로 두는데 JSON 을 한 번 왕복하면 목록이 된다 — **같은 칸이
    경로에 따라 두 모양**이 되고, 받는 쪽이 `== [...]` 로 비교하면 in-process 에서만
    조용히 어긋난다. 칸마다 세지 않고 이 성질로 잠근다.
    """
    보낸것 = _돌린다(
        요구=("SELLABLE_SUPPLY_CONTEXT", "FINANCIAL_VALIDATION"),
        finance=_재무({"verdict": "FAIL", "reason_codes": ("LIMIT", "AR")}),
    )

    for payload in 보낸것[1:]:
        assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_판매_계약이_받아_준다():
    """🔴 **모양을 마스터가 혼자 정하지 않는다.** 판매 모델에 그대로 통과시킨다.

    ★ `SalesFeedback` 는 `extra="forbid"` 라 칸 하나만 어긋나도 여기서 죽는다.
    """
    보낸것 = _돌린다(요구=("SELLABLE_SUPPLY_CONTEXT", "FINANCIAL_VALIDATION"))

    받은것 = SalesFeedback.model_validate(보낸것[1]["feedback"])

    assert 받은것.attempt == 1
    assert {r.source_agent for r in 받은것.domain_replies} == {"logistics", "finance"}


# ---------------------------------------------------------------------------
# 5. 🔴 **실 어댑터로 잰다 — 회차가 정말 오르는가**
# ---------------------------------------------------------------------------


def _대역(payload: dict[str, Any], **kw: Any):
    """봉투를 말하는 대역. **회신을 지어내는 것이 아니라 봉투에 담아 돌려준다.**"""

    def port(request: AgentRequest):
        reply = _reply(
            request,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            payload=payload,
            **kw,
        )
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


@pytest.fixture
def 판매가_받은_것(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[SalesProposalInput, SalesProposalReply]]:
    """판매 코어가 **실제로 받은 typed 입력**과 **그때 낸 답**을 회차마다 기록한다.

    🔴 **대역이 아니라 감시자다.** 진짜 `run_proposal` 을 그대로 부르고 입력만 옆에
      적어 둔다 — 갈아 끼우면 어댑터가 무엇을 만들어 넘기는지를 못 잰다. 이 검사가
      증명하려는 것이 바로 그 자리다 (`_proposal_input` 이 `is_refeed` 를 세우는 곳).

    ★ **답도 같이 적는다.** 되먹임이 닿았다는 것을 *"입력에 칸이 있더라"* 로 재면
      판매가 그 칸을 안 읽어도 초록이다. **답이 달라지는 것**이 진짜 증거다.

    ⚠️ **재무가 전부 기각한다.** 통과 후보가 하나라도 있으면 되먹임하지 않으므로
      (C-1) 회차가 오르는 것을 볼 수 없다. 조정안까지 같이 내야 마스터가 *"다시
      물어도 같다"* 로 접지 않는다 (C-2).
    """
    from app.sales import adapter as sales_adapter

    받은것: list[tuple[SalesProposalInput, SalesProposalReply]] = []
    진짜 = sales_adapter.run_proposal

    def 감시(proposal_input: SalesProposalInput):
        답 = 진짜(proposal_input)
        받은것.append((proposal_input, 답))
        return 답

    monkeypatch.setattr(sales_adapter, "run_proposal", 감시)
    monkeypatch.setattr(sales_adapter, "save_sales_agent_run", lambda **kw: None)
    wiring.register("inventory", _대역(PRE_SALES_PAYLOAD))
    wiring.register(
        "finance",
        _대역(
            {"finance_verdict": "FAIL"},
            business_status="reject",
            reasoning="여신 한도를 넘는다",
            suggested_adjustments=(_adjustment(),),
        ),
    )
    return 받은것


def _본문(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/master/sales/run",
        json={
            "as_of": 오늘.isoformat(),
            "policy_version": "v1.3",
            "business_mode": "SPOT_SALES",
            "item": "배추",
            "user_request": "배추 2톤 다음 주에",
            "partner_id": "P-1",
            "requested_quantity_kg": 2000,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_되먹임_회차가_실제로_오른다(판매가_받은_것) -> None:
    """🔴 **이 검사가 이 판의 전부다.** 지금까지 `feedback_attempt` 는 0 에 고정돼 있었다.

    ```text
    고치기 전   0 · 0     회차가 중첩된 자리에 있어 어댑터가 못 읽는다
    고친 뒤     0 · 1     최상위로 실어 어댑터가 그대로 읽는다
    ```

    ★ **판매 코어가 받은 typed 입력으로 잰다.** 마스터가 무엇을 보냈는지가 아니라
      **판매가 무엇을 받았는지**가 잴 값이다 — 사이에 어댑터의 키 거르기가 있고,
      되먹임이 죽어 있던 자리가 정확히 거기다.

    ⚠️ **실측으로 1회차까지만 간다 — 그것이 지금의 사실이다** (2026-09-07).

      되먹임 상한은 2 지만(`MAX_FEEDBACK_ATTEMPTS`) 2회차는 **오늘 도달할 수 없다.**
      판매가 되먹임 회차에서 `FINANCIAL_VALIDATION` 을 요구 목록에서 빼기 때문이다
      (아래 `test_되먹임이_판매의_답을_바꾼다` 가 그 사실을 잰다). 남는 요구는 초기
      물류 컨텍스트뿐이고 그것은 **S-1 로 재사용돼 새 호출이 없으므로** 새 조정안도
      없다 — 마스터가 *"다시 물어도 같다"* 로 접는다 (C-2).

      즉 상한 2 는 아직 **호출 예산이 아니라 판매의 답**이 먼저 막고 있다. 회차 사다리
      자체(0·1·2)는 `test_회차는_최상위에_실린다` 가 가짜 포트로 잰다.

      🔴 **판매가 되먹임 회차에도 재무를 요구하기 시작하면 이 검사가 빨개진다.**
        그날은 2회차가 실제로 돌므로 아래 숫자를 `MAX_FEEDBACK_ATTEMPTS + 1` 로 올린다.
    """
    받은것 = 판매가_받은_것
    _본문(TestClient(app.main.app))

    회차 = [i.feedback_attempt for i, _ in 받은것]
    assert 회차 == [0, 1], (
        f"판매가 받은 회차가 안 오른다: {회차}. "
        "0 만 이어지면 회차가 최상위에 안 실린 것이고, "
        "[0, 1, 2] 라면 판매가 되먹임 회차에도 재무를 요구하기 시작한 것이다"
    )
    assert 회차[-1] <= MAX_FEEDBACK_ATTEMPTS, "마스터가 자기 상한을 넘겨 불렀다"


def test_판매가_되먹임_회차로_읽는다(판매가_받은_것) -> None:
    """🔴 **`is_refeed` 는 판매가 정한다** — 마스터는 회차만 싣는다 (판매 회신).

    회차가 안 닿으면 이 값이 영원히 `False` 이고, 그러면 판매는 **되먹임 회차를
    최초 호출과 똑같이** 처리한다. 그것이 어댑터 배선 뒤에도 계속되던 상태다.
    """
    받은것 = 판매가_받은_것
    _본문(TestClient(app.main.app))

    assert [i.is_refeed for i, _ in 받은것] == [False, True], (
        f"판매가 되먹임으로 안 읽는다: {[i.is_refeed for i, _ in 받은것]}"
    )
    assert all(not i.is_refeed for i, _ in 받은것 if i.feedback_attempt == 0), (
        "최초 호출이 되먹임으로 읽힌다"
    )


def test_되먹임이_판매의_답을_바꾼다(판매가_받은_것) -> None:
    """🔴 **되먹임이 닿았다는 가장 강한 증거다 — 판매의 산출물이 달라진다.**

    회차가 안 닿던 동안 판매는 두 번 다 **같은 입력을 본 것과 같아서** 같은 요구를
    냈다. 되먹임이 닿자 판매가 재무 요구를 빼고 답한다 — 마스터가 실은 재무 기각
    회신을 읽었다는 뜻이다.

    ```text
    최초 호출   FINANCIAL_VALIDATION · SELLABLE_SUPPLY_CONTEXT · DELIVERY_FEASIBILITY_CONTEXT
    1차 되먹임  SELLABLE_SUPPLY_CONTEXT · DELIVERY_FEASIBILITY_CONTEXT
    ```

    ★ **무엇을 요구하는지는 판매 소유의 사실이라 목록을 박지 않는다.** 잠그는 것은
      *"두 회차의 답이 다르다"* 하나다 — 같아지는 날이 되먹임이 다시 죽은 날이다.
    """
    받은것 = 판매가_받은_것
    _본문(TestClient(app.main.app))

    assert len(받은것) == 2, f"되먹임이 안 돌았다: 판매 호출 {len(받은것)}회"

    def 요구(답) -> set[tuple[str, ...]]:
        return {tuple(sorted(s.required_validations)) for s in 답.scenarios}

    최초, 되먹임 = 요구(받은것[0][1]), 요구(받은것[1][1])
    assert 최초, "최초 회차 후보가 없다"
    assert 최초 != 되먹임, (
        f"되먹임 회차의 답이 최초와 같다: {최초}. 되먹임이 판매에 안 닿았다는 뜻이다"
    )
    assert 받은것[1][0].feedback is not None, "되먹임 칸이 안 실렸다"
    assert 받은것[1][0].feedback.domain_replies, "부서 회신이 하나도 안 실렸다"


def test_판매가_부서_회신을_받는다(판매가_받은_것) -> None:
    """되먹임 회차에 **부서 회신 원본**이 실려 도착한다 — 마스터 요약이 아니라.

    ★ `SalesProposalInput` 은 `extra="forbid"` 다. 여기까지 왔다는 것 자체가 계약
      모양이 맞다는 뜻이고, 어긋났으면 어댑터가 `ERROR` 로 닫아 이 목록이 짧아진다.
    """
    받은것 = 판매가_받은_것
    _본문(TestClient(app.main.app))

    되먹임 = 받은것[1][0].feedback
    assert 되먹임 is not None, "되먹임 칸이 통째로 안 왔다"
    assert 되먹임.attempt == 받은것[1][0].feedback_attempt, "두 자리의 회차가 다르다"
    assert 되먹임.original_run_id == 받은것[0][0].execution_identity.run_id, (
        "최초 판매 제안 run 을 가리키지 않는다"
    )
    assert {r.source_agent for r in 되먹임.domain_replies} == {"logistics", "finance"}
    # ★ **후보 수를 숫자로 안 박는다** — 몇 안이 나오는지는 판매 소유의 사실이다.
    #   마스터가 잠그는 것은 *"후보마다 자기 회신이 연결돼 온다"* 뿐이다.
    assert 되먹임.scenario_feedback, "후보가 회신과 연결되지 않았다"
    assert all(c.reply_refs for c in 되먹임.scenario_feedback), "연결이 빈 후보가 있다"
