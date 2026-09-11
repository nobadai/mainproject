"""확정이 **재무가 낸 기여이익을 나르는가** — 2026-09-11.

🔴 **고리가 닫혀 있었다.**

```text
app/sales/persistence.py:380  _line_profit
  ① line.contribution_profit_krw        ← 마스터가 비워 뒀다
  ② scenario.contribution_margin_krw    ← **비어 있다**
  ③ 없으면 SalesPersistenceConflict("missing contribution profit")
```

②가 왜 비는가.

```text
app/sales/proposal.py:216   판매는 **되먹임 회신에서** 마진을 받아 적는다
app/master/sales_flow.py    🔴 통과 후보가 있으면 되먹임하지 않고 제시한다 (계약 C-1)
실측                        feedback_attempts 전건 0 · 후보 revision 전건 0
```

★★ **되먹임을 받아야 마진이 적히는데, 통과한 안은 되먹임을 안 받는다** (설계대로).
  그래서 **통과한 안은 영원히 확정될 수 없었다** — `sales` 0행.

🔴 **`C-1` 을 안 건드렸다.** 그 계약은 옳다 — 통과한 안에 되먹임을 걸면 사용자가 볼
  수 있던 안이 바뀐다. 고리는 **마스터가 값을 날라서** 푼다.

🔴 **그런데 나를 값이 마스터에게 없었다.** `revalidation._verdict_of` 가
  `reply.payload` 를 버리고 있었고, 두 파일이 서로를 가리키며 *"같은 모양이다"* 라고
  적어 두는 동안 그 칸이 비어 있었다 (`sales_flow._verdict_of` 에는 있다).
  그래서 이 판은 **그 칸을 먼저 열고** 값을 나른다.

🔴 **이 파일이 잡으려는 다섯.**

```text
① 재검증 판정이 payload 를 나른다               ← 칸을 열지 않으면 나를 것이 없다
② 재무 판정의 마진이 확정 입력의 line 에 실린다   값 일치
③ 🔴 **재검증 값이 실린다** — 첫 검증이 아니다   이 판의 핵심
④ 마진이 없으면 BLOCKED 이고 이름을 부른다       0 이 안 실린다
⑤ 마스터가 마진을 계산하지 않는다                AST · 「0건을 세면 그것도 막는다」
```

★ **DB 를 안 탄다.** 부서는 대역으로 등록하고 `confirm_sale` 도 대역이다.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.master import decision_service, revalidation, sales_approval, wiring
from app.master.decision import DecisionIn, DecisionOut, mark_current
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata

REQ = "REQ-20260910-0001"
RUN_UUID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
SCN = "SALES-001-A-R1"

원_실행일 = date(2026, 1, 30)
납품일 = date(2026, 2, 6)

#: 🔴 **재검증이 내는 값.** 이것이 장부에 실려야 한다.
재검증_마진 = "128235.00"
재검증_마진율 = "0.664"

#: 🔴 **첫 검증이 낸 값.** 같은 안의 **제안 시점** 사실이라 다른 수다.
#:
#: ★★ 둘을 다르게 두는 것이 ③ 의 전부다. 같은 값으로 두면 *"재검증 것을 썼다"* 와
#:   *"첫 검증 것을 썼다"* 를 못 가른다.
첫검증_마진 = "999999.00"
첫검증_마진율 = "0.111"


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _요약(마진: str, 마진율: str) -> dict[str, str]:
    return {"contribution_margin_krw": 마진, "contribution_margin_rate": 마진율}


# ---------------------------------------------------------------------------
# 대역
# ---------------------------------------------------------------------------


class 부서:
    """등록된 어댑터 대역. **재검증이 부르는 자리다.**"""

    def __init__(self, 요약: dict[str, str] | None) -> None:
        self.요약 = 요약

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status="ok",
            reasoning="대역",
            payload=(
                {}
                if request.agent != "finance" or self.요약 is None
                else {"financial_summary": dict(self.요약)}
            ),
        )
        return reply, ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("tool_a",),
            tool_order=(1,),
        )


class 확정_대역:
    """`confirm_sale` 대역. **무엇을 들고 불렸는가**만 남긴다."""

    def __init__(self) -> None:
        self.호출: list[Any] = []

    def __call__(self, conn: Any, request: Any) -> Any:
        self.호출.append(request)

        class _결과:
            sale_id = "SALE-1"
            sale_item_id = "SI-SALE-1-1"

        return _결과()


class 커넥션_대역:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class 결정_저장소:
    def __init__(self) -> None:
        self.rows: list[DecisionOut] = []

    def list_decisions(self, request_id: str) -> list[DecisionOut]:
        return mark_current([r for r in self.rows if r.request_id == request_id])

    def save_decision(self, **kw: Any) -> DecisionOut:
        row = DecisionOut(
            decision_id=uuid4(),
            created_at=datetime.now(UTC),
            is_current=True,
            **{k: v for k, v in kw.items() if k != "note"},
            note=kw.get("note"),
        )
        self.rows.append(row)
        return row


def _scenario() -> dict[str, Any]:
    """판매 후보 하나 — `SalesScenario` 가 받는 모양 그대로 (`extra="forbid"`).

    🔴 **`contribution_margin_krw` 가 없다.** 통과한 안의 실제 모양이다 — 되먹임을
      안 받았으므로 판매가 그 칸을 채울 기회가 없었다 (계약 `C-1`).
    """
    return {
        "scenario_id": SCN,
        "scenario_type": "BALANCED",
        "objective": "BALANCE",
        "business_mode": "SPOT_SALES",
        "item": "배추",
        "partner_id": "P-1",
        "quantity_kg": "2000",
        "unit_price_krw": "1200",
        "sales_amount_krw": "2400000",
        "delivery_date": 납품일.isoformat(),
        "payment_days": 30,
        "payment_terms_type": "SINGLE",
        "supply": {"confirmed_quantity_kg": "2000"},
        "required_validations": [],
    }


def _판매_실행() -> dict[str, Any]:
    """`master_agent_runs` 판매 한 행의 대역.

    ★ **후보 판정에 첫 검증 마진을 실어 둔다.** ③ 이 재려는 것이 *"이 값을 쓰면
      안 된다"* 이므로, 이 값이 실제로 거기 있어야 검사가 뜻을 갖는다.
    """
    return {
        "run_id": RUN_UUID,
        "request_id": REQ,
        "cycle": "SALES",
        "item": "배추",
        "as_of": 원_실행일,
        "request_payload": {"policy_version": "v1.3", "item": "배추"},
        "response_payload": {
            "end_code": "SL1_PRESENTED",
            "as_of": 원_실행일.isoformat(),
            "candidates": [
                {
                    "scenario": _scenario(),
                    "validations": {
                        "FINANCIAL_VALIDATION": {
                            "agent": "finance",
                            "mode": "SALES_VALIDATION",
                            "business_status": "ok",
                            "runtime_status": "READY",
                            "payload": {"financial_summary": _요약(첫검증_마진, 첫검증_마진율)},
                        }
                    },
                }
            ],
            "adjustments": [],
        },
    }


@pytest.fixture
def 확정(monkeypatch) -> 확정_대역:
    대역 = 확정_대역()
    monkeypatch.setattr(sales_approval, "confirm_sale", 대역)
    monkeypatch.setattr(sales_approval, "_open", lambda connect: 커넥션_대역())
    return 대역


def _승인한다(monkeypatch, 요약: dict[str, str] | None) -> DecisionOut:
    """재무가 그 요약을 내는 세상에서 승인 한 번을 끝까지 돌린다."""
    wiring.reset()  # 루트 conftest 가 스냅샷을 떠 두므로 이 파일 밖으로 안 샌다
    wiring.register("inventory", 부서(None))
    wiring.register("finance", 부서(요약))

    저장소 = 결정_저장소()
    monkeypatch.setattr(decision_service, "list_decisions", 저장소.list_decisions)
    monkeypatch.setattr(decision_service, "save_decision", 저장소.save_decision)
    행 = _판매_실행()
    monkeypatch.setattr(
        decision_service, "get_run_by_request_id", lambda request_id, **kw: dict(행)
    )
    monkeypatch.setattr(decision_service, "get_run", lambda run_id: dict(행))

    return decision_service.record_decision(
        REQ,
        DecisionIn(decision="APPROVE", scenario_label=SCN, decided_by="이현서"),
    )


# ---------------------------------------------------------------------------
# ① 🔴 재검증 판정이 payload 를 나른다 — 칸을 열지 않으면 나를 것이 없다
# ---------------------------------------------------------------------------


def test_재검증_판정이_부서_payload_를_나른다() -> None:
    """🔴 **`revalidation._verdict_of` 가 이 칸을 버리고 있었다.**

    ★★ 두 파일이 서로를 가리키며 *"같은 모양이다"* 라고 적어 두었는데 두 칸이
      달랐다 — 매입 `#588` 과 같은 병이다. 이 검사가 그 약속을 코드로 만든다.
    """
    reply = AgentReply(
        request_id="REV-1",
        as_of=원_실행일,
        agent="finance",
        mode="SALES_VALIDATION",
        run_id="FIN-1",
        runtime_status="READY",
        business_status="ok",
        payload={"financial_summary": _요약(재검증_마진, 재검증_마진율)},
    )

    판정 = revalidation._verdict_of(reply)

    assert "payload" in 판정, "재검증 판정이 payload 를 버린다 — 나를 값이 없다"
    assert 판정["payload"]["financial_summary"]["contribution_margin_krw"] == 재검증_마진


def test_두_경로의_판정_모양이_run_id_말고는_같다() -> None:
    """★ **모양을 상대에게서 읽지 않고 여기서 맞댄다.**

    🔴 `run_id` 만 다르다. 저쪽의 그것은 `replies_by_ref` 로 회신 원본을 찾는
      **포인터**이고 재검증에는 그 등록소가 없다 — 없는 것을 가리키는 포인터를
      만들면 다음 사람이 그것을 쓰려다 빈손이 된다.
    """
    from app.master import sales_flow

    reply = AgentReply(
        request_id="R",
        as_of=원_실행일,
        agent="finance",
        mode="SALES_VALIDATION",
        run_id="FIN-1",
        runtime_status="READY",
        business_status="ok",
    )

    재검증칸 = set(revalidation._verdict_of(reply))
    첫검증칸 = set(sales_flow._verdict_of(reply))

    assert 첫검증칸 - 재검증칸 == {"run_id"}, (
        f"두 판정의 차이가 run_id 하나가 아니다: 첫검증만 {sorted(첫검증칸 - 재검증칸)}"
    )
    assert 재검증칸 - 첫검증칸 == set(), (
        f"재검증에만 있는 칸이 생겼다: {sorted(재검증칸 - 첫검증칸)}"
    )


def test_payload_를_더해도_조건_집합이_안_바뀐다() -> None:
    """🔴 **누가 `conditions_of` 에 칸을 더하면 사용자가 같은 안을 계속 다시 승인한다.**

    `conditions_of` 는 `business_status` 하나만 본다 — 그 문서화 문자열이
    *"판정은 닫힌 어휘 하나만 쓴다. `reasoning` 은 설명이지 조건이 아니라 넣지
    않는다"* 고 적었고, `payload` 도 같은 쪽이다.
    """
    없는것 = {
        "FINANCIAL_VALIDATION": {
            "agent": "finance",
            "mode": "SALES_VALIDATION",
            "business_status": "conditional",
            "runtime_status": "READY",
            "reasoning": "한도 근처",
            "missing_data": [],
        }
    }
    있는것 = {
        "FINANCIAL_VALIDATION": {
            **없는것["FINANCIAL_VALIDATION"],
            "payload": {"financial_summary": _요약(재검증_마진, 재검증_마진율)},
        }
    }

    assert revalidation.conditions_of(없는것, ()) == revalidation.conditions_of(있는것, ()), (
        "payload 가 조건 집합을 바꿨다 — 사용자가 같은 안을 다시 승인하게 된다"
    )


# ---------------------------------------------------------------------------
# ② 마진이 확정 입력의 line 에 그대로 실린다
# ---------------------------------------------------------------------------


def test_재무가_낸_마진이_확정_입력에_그대로_실린다(monkeypatch, 확정) -> None:
    """🔴 **이것이 `sales` 를 0행으로 만든 고리의 출구다.**

    ★ 후보에는 `contribution_margin_krw` 가 **없다** (`_scenario`). 되먹임을 안
      받았기 때문이고, 그래서 마스터가 안 나르면 `_line_profit` 이 터진다.
    """
    saved = _승인한다(monkeypatch, _요약(재검증_마진, 재검증_마진율))

    assert saved.sale is not None and saved.sale.status == "CONFIRMED", (
        f"확정이 안 섰다: {None if saved.sale is None else saved.sale.reason}"
    )
    assert 확정.호출, "confirm_sale 이 한 번도 안 불렸다 — 이 검사가 아무것도 안 재고 있다"

    line = 확정.호출[0].line
    assert line.contribution_profit_krw == Decimal(재검증_마진)
    assert line.contribution_margin_rate == Decimal(재검증_마진율)


# ---------------------------------------------------------------------------
# ③ 🔴 **재검증 값이 실린다 — 첫 검증이 아니다**
# ---------------------------------------------------------------------------


def test_첫_검증이_아니라_재검증_값이_실린다(monkeypatch, 확정) -> None:
    """★★ **이 판의 핵심 잠금이다.**

    확정은 재검증 **뒤에** 선다. 재검증이 그날 사실로 다시 센 값이 정본이고, 첫
    검증 값을 쓰면 **「제안 시점 사실」로 장부가 서서** 그 사이 재고·원가가 움직인
    것이 사라진다.

    ★ 원 실행 행의 후보 판정에는 첫 검증 마진(`999999.00`)이 실려 있고, 재검증
      부서는 다른 값(`128235.00`)을 낸다 — **둘 중 어느 것이 장부에 갔는지**를
      그 수로 가른다.
    """
    saved = _승인한다(monkeypatch, _요약(재검증_마진, 재검증_마진율))

    assert saved.sale is not None and saved.sale.status == "CONFIRMED"
    line = 확정.호출[0].line

    assert line.contribution_profit_krw == Decimal(재검증_마진), (
        f"재검증 값이 안 실렸다: {line.contribution_profit_krw}"
    )
    assert line.contribution_profit_krw != Decimal(첫검증_마진), (
        "첫 검증 값이 실렸다 — 제안 시점 사실로 장부가 섰다"
    )
    assert line.contribution_margin_rate != Decimal(첫검증_마진율)


def test_첫_검증_마진이_원_실행_행에_실제로_있다() -> None:
    """🔴 **먼저 이것부터.** 원 실행에 그 값이 없으면 위 검사가 공짜로 초록이 된다.

    *"첫 검증 것이 아니다"* 를 재려면 첫 검증 것이 **거기 있어야** 한다.
    """
    후보 = _판매_실행()["response_payload"]["candidates"][0]
    실린것 = 후보["validations"]["FINANCIAL_VALIDATION"]["payload"]["financial_summary"]

    assert 실린것["contribution_margin_krw"] == 첫검증_마진
    assert 첫검증_마진 != 재검증_마진, "두 값이 같으면 ③ 이 아무것도 안 가른다"


# ---------------------------------------------------------------------------
# ④ 없으면 지어내지 않는다 — BLOCKED 이고 이름을 부른다
# ---------------------------------------------------------------------------


def test_마진이_없으면_BLOCKED_이고_이름을_부른다(monkeypatch, 확정) -> None:
    """🔴 **0 으로 채우면 그날 손익이 거짓이 되고, 그 거짓은 안 터진다.**"""
    saved = _승인한다(monkeypatch, None)

    assert saved.sale is not None
    assert saved.sale.status == "BLOCKED", f"마진이 없는데 {saved.sale.status} 로 갔다"
    assert 확정.호출 == [], "마진이 없는데 confirm_sale 을 불렀다"

    사유 = _NFC(saved.sale.reason)
    for 이름 in sales_approval.REQUIRED_FINANCIAL_SUMMARY_FIELDS:
        assert 이름 in 사유, f"사유가 '{이름}' 을 안 부른다: {사유}"
    assert _NFC("재무") in 사유, "사유가 누가 채우는지를 안 가리킨다"


def test_마진이_0_이면_그것은_있는_값이다(monkeypatch, 확정) -> None:
    """★ `0` 은 없는 값이 아니다 — *"기여이익이 0 으로 확인됐다"* 는 정해진 사실이다.

    🔴 `falsy` 로 세면 그 사실이 조용히 *"재무가 안 냈다"* 가 된다
      (`missing_commercial_terms` 가 `payment_days=0` 에 대해 적어 둔 그것).
    """
    saved = _승인한다(monkeypatch, _요약("0", "0"))

    assert saved.sale is not None and saved.sale.status == "CONFIRMED", (
        f"0 을 「없다」로 셌다: {saved.sale.reason if saved.sale else None}"
    )
    assert 확정.호출[0].line.contribution_profit_krw == Decimal(0)


@pytest.mark.parametrize(
    "요약",
    [None, {}, {"contribution_margin_krw": "1"}, {"contribution_margin_rate": "0.1"}],
    ids=["없음", "빈칸", "마진만", "마진율만"],
)
def test_한_칸이라도_비면_확정하지_않는다(요약) -> None:
    """★ 두 칸이 짝이다. 한쪽만 싣고 다른 쪽을 `None` 으로 두면 장부가 반만 선다."""
    없는것 = sales_approval.missing_financial_summary_fields(요약)

    assert 없는것, f"비어 있는데 「다 있다」로 셌다: {요약}"


# ---------------------------------------------------------------------------
# ⑤ 🔴 AST — 마스터가 마진을 **계산하지 않는다**
# ---------------------------------------------------------------------------


#: 검사 대상. 🔴 **`__file__` 에서 얻는다** — 경로를 손으로 적으면 파일이 옮겨간 날
#:   조용한 빈 통과가 될 길이 생긴다.
_대상 = pathlib.Path(sales_approval.__file__)

#: 마스터가 재무가 되는 연산들. `수량 × 단가 − 원가` 가 그 모양이다.
_금지연산 = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)


def _함수마디(이름: str) -> ast.FunctionDef:
    나무 = ast.parse(_대상.read_text(encoding="utf-8"))
    for 마디 in ast.walk(나무):
        if isinstance(마디, ast.FunctionDef) and 마디.name == 이름:
            return 마디
    raise AssertionError(f"{_대상.name} 에서 {이름} 를 못 찾았다 — 검사가 헛돌고 있다")


@pytest.mark.parametrize("함수이름", ["_confirmation_input", "_decimal_of"])
def test_마스터가_마진을_계산하지_않는다(함수이름: str) -> None:
    """🔴 **수량 × 단가 − 원가 를 여기서 세면 그 순간 마스터가 재무가 된다.**

    ★ 값의 주인은 재무 하나다. 마스터가 한 번이라도 다시 세면 두 수가 갈리는 날이
      오고, 그때 어느 쪽이 맞는지를 아무도 못 푼다.

    ★★ **「0건을 세면 그것도 막는다」.** 함수를 못 찾으면 `_함수마디` 가 그 자리에서
      터지고, 마디를 하나도 못 세면 아래 `센_마디 > 0` 이 빨개진다 — 재는 줄만
      있고 재는 대상이 없는 것을 막는다.
    """
    마디 = _함수마디(함수이름)

    센_마디 = 0
    걸린것: list[int] = []
    for 안쪽 in ast.walk(마디):
        센_마디 += 1
        if isinstance(안쪽, ast.BinOp) and isinstance(안쪽.op, _금지연산):
            걸린것.append(안쪽.lineno)
        if isinstance(안쪽, ast.AugAssign) and isinstance(안쪽.op, _금지연산):
            걸린것.append(안쪽.lineno)

    assert 센_마디 > 0, f"{함수이름} 안에서 마디를 하나도 못 셌다 — 검사가 헛돌고 있다"
    assert not 걸린것, f"{함수이름} 이 산술 연산을 한다 (줄 {걸린것}) — 마스터가 재무가 됐다"


def test_금지연산_목록이_실제로_걸린다() -> None:
    """★★ **잠금 자체의 생존 검사.** 이 목록이 비면 위 검사가 늘 초록이다.

    🔴 `#320` 의 변이가 안 울었던 이유가 그 모양이었다 — 재는 줄은 있는데 재는
      대상이 없었다.
    """
    가짜 = ast.parse("def f(a, b):\n    return a * b - 1\n")
    걸린것 = [
        n.lineno for n in ast.walk(가짜) if isinstance(n, ast.BinOp) and isinstance(n.op, _금지연산)
    ]

    assert len(걸린것) == 2, f"산술 둘을 못 잡는다 — 금지연산 목록이 고장 났다: {걸린것}"


def test_기여이익_인자가_기본값_없는_키워드다() -> None:
    """★ `as_of` · `sim_run_id` 와 같은 규율이다 — 기본값은 곧 업무 규칙이 된다.

    🔴 기본값이 생기면 안 넘긴 자리가 조용히 그 값으로 돌고 아무 데도 안 적힌다.
    """
    param = inspect.signature(sales_approval.confirm_approved_sale).parameters["financial_summary"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        f"financial_summary 에 기본값이 생겼다({param.default!r})"
    )
