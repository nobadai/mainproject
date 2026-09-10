"""추가공급 라우팅 — 판매 후보의 부족량이 매입 경계 문의로 나가는 길 (2026-09-10).

매입이 `#485` 에서 `SUPPLY_CAPACITY_QUERY` 를 구현하면서
`CAPABILITY_ROUTING["ADDITIONAL_SUPPLY_CONTEXT"]` 에 걸어 둔 조건이 충족됐다.
그 라우팅을 열면 **네 가지가 한꺼번에 참이어야** 길이 성립한다.

```text
① 봉투가 그 mode 를 매입에 허용하는가       아니면 호출 순간에 터진다
② 라우팅이 그 mode 를 가리키는가            `GENERATE_SCENARIOS` 면 매입안이 만들어진다
③ 부족량과 경계 재료가 봉투에 실리는가       안 실리면 매입이 조용히 null 로 답한다
④ 품목으로 묶어 한 번씩 부르는가            안 묶으면 같은 답이 여러 번 온다
```

🔴 **`③` 이 이 파일의 이유다.** `②` 만 열고 `③` 을 안 하면 `_judge` 의 기본 경로가
  **후보를 통째로** 매입에 보낸다. 후보에 `item` 은 최상위라 매입은 오류를 내지 않고,
  부족량(`supply.required_additional_quantity_kg`)과 경계 재료가 빠진 채
  `procurable_quantity_kg=null` · `basis=unknown` 으로 답한다 — **물어본 값이 안
  실렸다는 사실이 어디에도 안 남는다.**

⚠️ **매입 어댑터를 부르지 않는다.** 여기서 재는 것은 *"마스터가 무엇을 실어 보내는가"*
  이지 매입이 그것으로 무엇을 계산하는가가 아니다. 매입 계산은 매입 스위트가 잰다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.contracts.core import SuggestedAdjustment
from app.master import (
    AgentRegistry,
    AgentReply,
    AgentRequest,
    CallBudget,
    ExecutionContext,
    ExecutionMetadata,
    MasterRunner,
)
from app.master.envelope import CAPABILITY_ROUTING, agent_allowed_modes
from app.master.procurement_boundary import ProcurementBoundary
from app.master.sales_flow import (
    BOUNDARY_FIELDS,
    MAX_FEEDBACK_ATTEMPTS,
    SALES_BUDGET,
    SalesFlow,
)
from tests.master.logistics_pre_sales import PRE_SALES_PAYLOAD

AS_OF = date(2026, 9, 6)
매입경로 = ("purchase", "SUPPLY_CAPACITY_QUERY")


# ── 봉투 · 가짜 포트 ─────────────────────────────────────────────────────────


def ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="SREQ-ASC",
        as_of=AS_OF,
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
        run_id=reply.run_id,
        request_id=request.context.request_id,
        agent=request.agent,
        used_tools=("tool_a",),
        tool_order=(1,),
    )


def 후보(
    scenario_id: str,
    *,
    item: str | None = "배추",
    부족량: float | None = 400.0,
    요구: tuple[str, ...] = ("ADDITIONAL_SUPPLY_CONTEXT",),
) -> dict[str, Any]:
    """판매 후보 하나.

    🔴 **부족량은 `supply` 한 겹 안이다** (`sales.schemas.ScenarioSupply`). 최상위에
      두면 마스터가 못 읽고 **아무 후보도 안 물어보는데 오류는 안 난다.**

    ★ 상업조건 둘을 늘 채운다 — 없으면 `missing_terms` 로 먼저 탈락해서 이 파일이
      재려는 자리(매입 호출)에 닿기 전에 갈린다.
    """
    out: dict[str, Any] = {
        "scenario_id": scenario_id,
        "required_validations": list(요구),
        "delivery_date": "2026-01-20",
        "payment_days": 30,
    }
    if item is not None:
        out["item"] = item
    if 부족량 is not None:
        out["supply"] = {"required_additional_quantity_kg": 부족량}
    return out


def 물류():
    def port(request: AgentRequest):
        reply = _reply(request, payload=PRE_SALES_PAYLOAD)
        return reply, _meta(request, reply)

    return port


def 판매(회차들: list[list[dict[str, Any]]]):
    """회차마다 다른 후보를 낸다. 마지막 회차 목록이 이후에 반복된다."""
    본 = {"n": 0}

    def port(request: AgentRequest):
        index = min(본["n"], len(회차들) - 1)
        본["n"] += 1
        reply = _reply(request, payload={"scenarios": list(회차들[index]), "situation": "다"})
        return reply, _meta(request, reply)

    return port


def 매입(잡힌: list[dict[str, Any]], business_status: str = "ok"):
    """매입 — `SUPPLY_CAPACITY_QUERY`. **받은 봉투를 그대로 적어 둔다.**"""

    def port(request: AgentRequest):
        잡힌.append({"mode": request.mode, "payload": dict(request.payload)})
        reply = _reply(
            request,
            business_status=business_status,
            payload={
                "item": request.payload.get("item"),
                "procurable_quantity_kg": None,
                "risks": ["아직 모른다"],
            },
            reasoning="경계만 답했다",
        )
        return reply, _meta(request, reply)

    return port


def 흐름(
    후보들: list[list[dict[str, Any]]],
    잡힌: list[dict[str, Any]],
    *,
    경계: ProcurementBoundary | None = None,
    매입등록: bool = True,
    budget: int = SALES_BUDGET,
) -> SalesFlow:
    registry = AgentRegistry()
    registry.register("inventory", 물류())
    registry.register("sales", 판매(후보들))
    if 매입등록:
        registry.register("purchase", 매입(잡힌))
    runner = MasterRunner(ctx(), registry, CallBudget(limit=budget))
    return SalesFlow(runner, business_mode="SPOT_SALES", procurement_boundary=경계)


def 읽은경계() -> ProcurementBoundary:
    """실제로 읽어 온 경계. **`rental_cap_kg` 가 `0.0` 인 것이 실측이다** — 그건
    *"자리가 없다"* 라는 **읽은 값**이라 `None` 과 반드시 갈려야 한다."""
    return ProcurementBoundary(
        present=True,
        source_ref="PROC-RUN-77/2026-09-06",
        warehouse_free_kg=12000.0,
        rental_cap_kg=0.0,
        finance_cap_amount_krw=48000000,
        inbound_lead_days=2,
    )


# ── ① 라우팅이 열렸는가 ──────────────────────────────────────────────────────


def test_추가공급은_매입_경계_문의로_라우팅된다():
    """🔴 **`GENERATE_SCENARIOS` 로 가면 판매 사이클 안에서 매입안이 만들어진다** —
    설계가 금지한 그것이고, 평균 11.2초를 태우면서 **오류는 안 난다.**"""
    assert CAPABILITY_ROUTING["ADDITIONAL_SUPPLY_CONTEXT"] == 매입경로


def test_봉투가_매입에게_그_mode_를_허용한다():
    """★ 라우팅표와 `_AGENT_MODES` 는 **다른 자리**다. 표만 열고 봉투를 안 열면
    호출 순간에 `ContractViolation` 으로 터진다 — 배선이 열린 줄 알고 나가서 죽는다."""
    assert "SUPPLY_CAPACITY_QUERY" in agent_allowed_modes("purchase")


def test_라우팅표를_닫으면_부르는_쪽도_같이_닫힌다(monkeypatch):
    """🔴 **부르는 쪽과 판정하는 쪽의 주인이 하나여야 한다.**

    매입 호출은 후보를 돌기 전에 일어나므로 `_judge` 의 라우팅 분기를 안 거친다.
    그 자리에 `("purchase", "SUPPLY_CAPACITY_QUERY")` 를 손으로 박으면 표를 `None` 으로
    되돌려도 **호출은 그대로 나가고** `_judge` 만 *"부를 대상이 없다"* 로 답한다 —
    한 capability 에 대해 두 자리가 서로 다른 답을 갖는다.

    ⚠️ 실제로 그렇게 짰다가 변이 ①에서 잡혔다. 표를 `None` 으로 되돌렸는데 봉투를
      재는 검사들이 **그대로 초록이었다.**
    """
    잡힌: list[dict[str, Any]] = []
    monkeypatch.setitem(CAPABILITY_ROUTING, "ADDITIONAL_SUPPLY_CONTEXT", None)
    out = 흐름([[후보("SCN-1")]], 잡힌, 경계=읽은경계()).run()

    assert 잡힌 == [], "라우팅표가 닫혔는데 매입을 불렀다"
    assert "ADDITIONAL_SUPPLY_CONTEXT" in out.unroutable_capabilities


def test_매입이_받는_mode_는_시나리오_생성과_다른_이름이다():
    """🔴 한 이름으로 합치면 `(agent, mode, call_seq)` 로 **7노드를 돈 실행**과
    **시세만 읽은 실행**을 구분할 수 없다 — 걸리는 시간부터 다른 두 실행이다."""
    assert "GENERATE_SCENARIOS" in agent_allowed_modes("purchase")
    assert 매입경로[1] != "GENERATE_SCENARIOS"


# ── ② 무엇을 싣는가 ─────────────────────────────────────────────────────────


def test_매입이_읽는_것은_후보가_아니라_부족량이다():
    """🔴 **이 파일의 핵심.** `_judge` 의 기본 경로는 후보를 통째로 보내는데(재무가 그
    모양을 읽으므로 옳다), 매입이 읽는 칸은 다르다. 후보를 그대로 보내면 매입은
    오류 없이 `null` 로 답하고 **아무도 그 사실을 모른다.**"""
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1", item="배추", 부족량=400.0)]], 잡힌, 경계=읽은경계()).run()

    assert len(잡힌) == 1
    payload = 잡힌[0]["payload"]
    assert payload["item"] == "배추"
    assert payload["required_additional_quantity_kg"] == 400.0
    # 🔴 후보를 통째로 보냈다면 이 칸들이 실려 온다.
    assert "scenario_id" not in payload
    assert "required_validations" not in payload


def test_경계_네_값과_출처가_봉투에_실린다():
    """🔴 **매입은 「가능량」을 남의 값 둘로 만든다** —
    `min(창고 여유, 매입 가능액 ÷ 단가)`. 안 실으면 그 계산이 아예 안 돈다.

    ★ **`source_ref` 도 같이 싣는다.** 경계는 「그날 매입 판단 시점」의 값이라
      판단 뒤 출고가 나가면 창고가 바뀐다 — 어느 실행의 언제 값인지를 숨기지 않는다.
    """
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1")]], 잡힌, 경계=읽은경계()).run()

    payload = 잡힌[0]["payload"]
    assert payload["warehouse_free_kg"] == 12000.0
    assert payload["rental_cap_kg"] == 0.0
    assert payload["finance_cap_amount_krw"] == 48000000
    assert payload["inbound_lead_days"] == 2
    assert payload["source_ref"] == "PROC-RUN-77/2026-09-06"


def test_읽은_0_과_못_읽은_None_이_갈린다():
    """🔴 **`rental_cap_kg` 실측이 `0.0` 이다.** 그건 *"자리가 없다"* 라는 **읽은 값**
    이고, `None` 은 *"못 읽었다"* 다. 뭉개면 매입이 못 읽은 칸을 자리 없음으로 읽는다.

    ⚠️ 이 검사는 `0.0 == None` 이 거짓인 것을 재는 것이 아니라, **한 봉투 안에 둘이
      같이 실릴 수 있는지**를 잰다 — 하나로 접는 구현은 여기서 걸린다.
    """
    잡힌: list[dict[str, Any]] = []
    경계 = ProcurementBoundary(
        present=True,
        source_ref="PROC-RUN-78/2026-09-06",
        warehouse_free_kg=None,
        rental_cap_kg=0.0,
        finance_cap_amount_krw=None,
        inbound_lead_days=None,
    )
    흐름([[후보("SCN-1")]], 잡힌, 경계=경계).run()

    payload = 잡힌[0]["payload"]
    assert payload["rental_cap_kg"] == 0.0
    assert payload["warehouse_free_kg"] is None
    assert payload["finance_cap_amount_krw"] is None


def test_경계_칸은_못_읽어도_빠지지_않는다():
    """★ 칸을 빼면 *"안 물어봤다"* 와 *"물어봤는데 못 읽었다"* 가 같아진다."""
    잡힌: list[dict[str, Any]] = []
    경계 = ProcurementBoundary(present=False, absent_reason="NOT_EXECUTION_DAY")
    흐름([[후보("SCN-1")]], 잡힌, 경계=경계).run()

    payload = 잡힌[0]["payload"]
    for 칸 in BOUNDARY_FIELDS:
        assert 칸 in payload, f"{칸} 이 빠졌다"
        assert payload[칸] is None


def test_마스터는_가능량을_계산하지_않는다():
    """🔴 **나눗셈에 쓰는 단가가 매입 것이다.** 마스터가 계산하면 단가가 바뀌는 날 두
    값이 갈리고, 그때 어느 쪽이 참인지 아무도 말해 주지 않는다. **재료만 준다.**"""
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1")]], 잡힌, 경계=읽은경계()).run()

    payload = 잡힌[0]["payload"]
    assert "procurable_quantity_kg" not in payload
    assert "expected_unit_price_krw" not in payload
    assert "basis" not in payload


# ── ③ 못 읽어도 부른다 ──────────────────────────────────────────────────────


def test_경계를_못_읽어도_매입을_부른다():
    """🔴 **매입 `#485` §1.2 가 계약이다** — 재료가 없으면
    `procurable_quantity_kg=null` · `basis=unknown` · `risks` 에 사유로 답한다.

    여기서 건너뛰면 그 계약이 한 번도 안 쓰이고, 화면에는 *"안 왔다"* 만 남아
    **매입에 물어봤는지조차** 안 보인다.
    """
    잡힌: list[dict[str, Any]] = []
    경계 = ProcurementBoundary(present=False, absent_reason="LEDGER_GAP")
    흐름([[후보("SCN-1")]], 잡힌, 경계=경계).run()

    assert len(잡힌) == 1, "경계를 못 읽었다고 호출을 건너뛰었다"


def test_못_읽은_사유가_봉투에_실린다():
    """★★ **매입 `basis="unknown"` 을 푸는 값이다.** 그 값만으로는 *"마스터가 안
    실었다"* 와 *"실렸는데 계산이 안 됐다"* 가 뭉개진다. 사유가 옆에 있으면 화면이
    **"토요일이라 못 물어봤다"** 까지 말한다."""
    잡힌: list[dict[str, Any]] = []
    경계 = ProcurementBoundary(present=False, absent_reason="NO_PROCUREMENT_RUN")
    흐름([[후보("SCN-1")]], 잡힌, 경계=경계).run()

    assert 잡힌[0]["payload"]["supply_context_absent"] == "NO_PROCUREMENT_RUN"


def test_읽은_날에는_못_읽은_사유가_안_실린다():
    """★ 읽었는데 사유가 붙으면 둘 중 하나가 거짓이다 (`ProcurementBoundary` 불변조건과
    같은 규율을 봉투에서도 지킨다)."""
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1")]], 잡힌, 경계=읽은경계()).run()

    assert "supply_context_absent" not in 잡힌[0]["payload"]


# ── ④ 품목으로 묶는다 ──────────────────────────────────────────────────────


def test_같은_품목_후보가_여럿이면_한_번만_부른다():
    """🔴 **매입은 품목 하나를 받아 하나를 답한다**
    (`sales.schemas.PurchaseAdditionalSupplyResult` 가 최상위 단수). 같은 품목을 세 번
    물으면 **같은 답이 세 번** 온다 — 예산만 타고 사실은 안 는다."""
    잡힌: list[dict[str, Any]] = []
    out = 흐름(
        [
            [
                후보("SCN-1", item="배추", 부족량=400.0),
                후보("SCN-2", item="배추", 부족량=700.0),
                후보("SCN-3", item="배추", 부족량=550.0),
            ]
        ],
        잡힌,
    ).run()

    assert len(잡힌) == 1, f"배추를 {len(잡힌)} 번 물었다"
    assert out.plan.call_count("purchase", "SUPPLY_CAPACITY_QUERY") == 1


def test_품목이_서로_다르면_품목마다_부른다():
    """★ 묶는 기준이 «후보» 가 아니라 «품목» 이라는 것을 반대쪽에서 잠근다 — 무조건
    한 번만 부르는 구현은 여기서 걸린다."""
    잡힌: list[dict[str, Any]] = []
    흐름(
        [
            [
                후보("SCN-1", item="배추", 부족량=400.0),
                후보("SCN-2", item="무", 부족량=200.0),
                후보("SCN-3", item="양파", 부족량=100.0),
            ]
        ],
        잡힌,
    ).run()

    assert [잡힌[i]["payload"]["item"] for i in range(len(잡힌))] == ["배추", "무", "양파"]


def test_요청량은_그_품목_후보_중_가장_큰_것이다():
    """🔴 **묻는 것이 «얼마까지 되나» 라 작은 쪽으로 물으면 답이 그만큼 잘린다.**

    700 이 되는지 물어야 400 후보도 같이 판정할 수 있고, 400 으로 물으면 700 후보는
    **다시 물어야 한다** — 그런데 다시 묻는 배선이 없다.
    """
    잡힌: list[dict[str, Any]] = []
    흐름(
        [
            [
                후보("SCN-1", item="배추", 부족량=400.0),
                후보("SCN-2", item="배추", 부족량=700.0),
                후보("SCN-3", item="배추", 부족량=550.0),
            ]
        ],
        잡힌,
    ).run()

    assert 잡힌[0]["payload"]["required_additional_quantity_kg"] == 700.0


def test_품목마다_그_품목의_최대_부족량을_묻는다():
    """★ 최대를 **품목별로** 잡는지 본다 — 전체 최대 하나를 모든 품목에 쓰는 구현은
    여기서 걸린다 (무에 700 을 물으면 안 된다)."""
    잡힌: list[dict[str, Any]] = []
    흐름(
        [
            [
                후보("SCN-1", item="배추", 부족량=700.0),
                후보("SCN-2", item="무", 부족량=200.0),
                후보("SCN-3", item="무", 부족량=150.0),
            ]
        ],
        잡힌,
    ).run()

    실린것 = {
        잡힌[i]["payload"]["item"]: 잡힌[i]["payload"]["required_additional_quantity_kg"]
        for i in range(len(잡힌))
    }
    assert 실린것 == {"배추": 700.0, "무": 200.0}


# ── ⑤ 안 부르는 자리 ───────────────────────────────────────────────────────


def test_부족량이_없는_후보는_안_부른다():
    """★ 모자라지 않은데 더 대 달라고 묻지 않는다."""
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1", 부족량=None)]], 잡힌).run()

    assert 잡힌 == []


def test_부족량이_0_인_후보는_안_부른다():
    """★ `0` 은 *"모자라지 않는다"* 라는 **읽은 값**이다 — 읽었으니 물어볼 것이 없다."""
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1", 부족량=0.0)]], 잡힌).run()

    assert 잡힌 == []


def test_요구하지_않은_후보는_안_부른다():
    """🔴 **마스터가 요구를 지어내지 않는다** (§3.2.2). 부족량이 실려 있어도 판매가
    검증을 요구하지 않았으면 묻지 않는다 — 무엇이 필요한지는 제안자가 안다."""
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1", 부족량=900.0, 요구=())]], 잡힌).run()

    assert 잡힌 == []


def test_부족량을_못_읽은_후보는_통과가_아니다():
    """🔴 **조용히 건너뛰면 «검증됐다» 로 읽힌다.** 안 부른 것이 통과가 되면 안 된다.

    ⚠️ 판매 계약상 이 조합은 안 나온다 (`additional_supply_required` 가 부족량 > 0 과
      한 몸이다). 그래도 통과시키지 않는 것이 규율이다.
    """
    잡힌: list[dict[str, Any]] = []
    out = 흐름([[후보("SCN-1", 부족량=None)]], 잡힌).run()

    assert out.presented == ()
    assert "ADDITIONAL_SUPPLY_CONTEXT" in out.unroutable_capabilities


def test_매입_미등록은_판매_사이클을_세우지_않는다():
    """🔴 **매입은 조건부다** (`wiring.REQUIRED_FOR_SALES` 에 없다). 미등록으로
    `SL4_NOT_STARTED` 를 내면 *"시작하지 못했다"* 가 되는데, 실제로는 **후보까지 다
    받은 뒤**다 — 사람이 배선을 뒤지는 동안 화면은 거짓말을 한다.

    ★ 못 물어봤다는 사실은 사라지지 않고 `unroutable` 로 남는다.
    """
    잡힌: list[dict[str, Any]] = []
    out = 흐름([[후보("SCN-1")]], 잡힌, 매입등록=False).run()

    assert out.end_code != "SL4_NOT_STARTED"
    assert "ADDITIONAL_SUPPLY_CONTEXT" in out.unroutable_capabilities


# ── ⑥ 판정으로 이어지는가 ──────────────────────────────────────────────────


def test_매입_회신이_그_품목_후보_전부의_판정이_된다():
    """★ 한 번 물어 여러 후보에 나눠 쓴다 — 그것이 품목으로 묶는 값이다."""
    잡힌: list[dict[str, Any]] = []
    out = 흐름(
        [
            [
                후보("SCN-1", item="배추", 부족량=400.0),
                후보("SCN-2", item="배추", 부족량=700.0),
            ]
        ],
        잡힌,
    ).run()

    assert len(out.candidates) == 2
    for 후 in out.candidates:
        assert "ADDITIONAL_SUPPLY_CONTEXT" in 후.validations


def test_매입이_반려하면_그_후보는_통과가_아니다():
    """★ 봉투를 실어 보내는 것으로 끝이 아니라 **판정으로 이어지는지**까지 본다."""
    잡힌: list[dict[str, Any]] = []
    out = 흐름([[후보("SCN-1")]], 잡힌).run()
    assert out.end_code == "SL1_PRESENTED"

    잡힌2: list[dict[str, Any]] = []
    registry = AgentRegistry()
    registry.register("inventory", 물류())
    registry.register("sales", 판매([[후보("SCN-1")]]))
    registry.register("purchase", 매입(잡힌2, business_status="reject"))
    runner = MasterRunner(ctx(), registry, CallBudget(limit=SALES_BUDGET))
    반려 = SalesFlow(runner, business_mode="SPOT_SALES").run()

    assert 반려.presented == ()


# ── ⑦ 예산 ────────────────────────────────────────────────────────────────


def test_예산이_최악을_덮는다():
    """🔴 **최악을 직접 센다.** 후보 셋이 서로 다른 품목이고 되먹임이 끝까지 가면
    매입 호출이 회차마다 셋이다.

    ```text
    물류 PRE_SALES                1
    판매 GENERATE_SALES_PROPOSAL  3   (최초 1 + 되먹임 2)
    재무 SALES_VALIDATION         9   (후보 3 × 회차 3)   ← 이 판에서는 안 부른다
    매입 SUPPLY_CAPACITY_QUERY    9   (품목 3 × 회차 3)
    ```
    """
    회차 = MAX_FEEDBACK_ATTEMPTS + 1
    assert 1 + 회차 + 3 * 회차 + 3 * 회차 <= SALES_BUDGET


def test_되먹임_회차마다_다시_묻는다():
    """★ **앞 회차 답을 재사용하지 않는다.** 후보가 바뀌면 부족량도 바뀌므로, 재사용은
    새 부족량에 옛 경계를 붙이는 것이 된다 (S-1 재사용이 ②에만 걸리는 이유와 같다).

    ⚠️ **되먹임이 돌려면 «권위 있는 대안» 이 있어야 한다** (C-2). 그래서 재무를 같이
      요구시키고 재무가 대안과 함께 반려한다 — 매입 반려만으로는 *"다시 물어도 같다"*
      로 접혀서 2회차가 아예 안 온다.

    ★ **대안을 내는 쪽이 재무인 것에 뜻이 있다.** 매입은 제안자라 조언자가 아니고
      (`_AGENT_DEPT` 가 매입을 안 담는 이유), 축 조정을 제안할 자리가 아니다.
    """
    잡힌: list[dict[str, Any]] = []
    요구 = ("FINANCIAL_VALIDATION", "ADDITIONAL_SUPPLY_CONTEXT")

    def 재무(request: AgentRequest):
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
        return reply, _meta(request, reply)

    registry = AgentRegistry()
    registry.register("inventory", 물류())
    registry.register(
        "sales",
        판매(
            [
                [후보("SCN-1", item="배추", 부족량=400.0, 요구=요구)],
                [후보("SCN-2", item="배추", 부족량=900.0, 요구=요구)],
            ]
        ),
    )
    registry.register("finance", 재무)
    registry.register("purchase", 매입(잡힌))
    runner = MasterRunner(ctx(), registry, CallBudget(limit=SALES_BUDGET))
    SalesFlow(runner, business_mode="SPOT_SALES", max_feedback_attempts=1).run()

    실린량 = [잡힌[i]["payload"]["required_additional_quantity_kg"] for i in range(len(잡힌))]
    assert 실린량 == [400.0, 900.0], "회차가 바뀌었는데 다시 안 물었다"


# ── ⑧ 자기 생존 ───────────────────────────────────────────────────────────


def test_이_파일이_실제로_매입을_부르고_있다():
    """🔴 **자기 생존 검사.** 위 검사 대부분이 *"이 봉투가 이렇게 실렸다"* 를 재는데,
    라우팅이 닫히거나 후보 모양이 바뀌어 **매입이 한 번도 안 불리면** `잡힌` 이 비고
    그 검사들은 조용히 초록이 될 자리가 생긴다.

    ★ 여기서 한 번 **불렸다는 사실 자체**를 못 박는다 — 이 파일 전체가 빈 껍데기로
      도는 날 여기가 먼저 빨개진다.
    """
    잡힌: list[dict[str, Any]] = []
    흐름([[후보("SCN-1")]], 잡힌, 경계=읽은경계()).run()

    assert len(잡힌) == 1
    assert 잡힌[0]["mode"] == "SUPPLY_CAPACITY_QUERY"
    assert 잡힌[0]["payload"], "봉투가 비어 있다 — 실은 것이 없다"
