"""판매 어댑터가 **실제로 등록되는가** — 그리고 그 등록이 무엇을 바꿨는가.

🔴 **`register_agent("sales", …)` 호출이 0건이었다.** 판매 Flow 골격(`sales_flow.py`)
   도 진입점(`run_sales`)도 어댑터(`app/sales/adapter.py` · `#364`)도 다 서 있었는데
   등록하는 줄이 어디에도 없어서 `POST /master/sales/run` 이 **부르기 전에** 섰다 —
   `REQUIRED_FOR_SALES` 를 문 앞에서 보는데 `sales` 가 없어 매번 `SL4_NOT_STARTED`
   였다. `register_inbound` 가 `#337` 까지 0건이던 것과 **같은 모양**이고,
   `test_inbound_registration.py` 가 잠근 자리와 같은 종류다.

★ **가짜 어댑터로는 이 자리를 못 잰다.** `test_sales_entrypoint.py` · `test_sales_flow.py`
  는 대역을 직접 등록해 골격을 재므로 배선이 통째로 빠져 있어도 초록불이다. 여기서는
  `app.main` 이 import 시점에 등록한 **실제 판매 어댑터**로 잰다.

---

🔴 **두 번째로 잠그는 것 — 그 경로가 정말 도는가.**

등록 한 줄은 쉽고 **정말 도는지가 어렵다.** 지금까지 판매 경로는 어댑터 없이 `SL4` 로만
검증됐다 — *"시작하지 못했다"* 는 사실만 재고 **시작한 뒤**는 아무도 안 봤다.

```text
등록 전   SL4_NOT_STARTED   어댑터 미등록: sales   ← 부서를 한 번도 안 부른다
등록 후   SL2_NO_CANDIDATE  판매가 답을 냈다        ← 실 어댑터가 실제로 돈다
```

★ **가르는 사실은 `SL4` 가 사라지는 것이다.** 어느 `SL` 로 끝나는지는 그날 후보
  사정이지만, `SL4` 는 *"아무도 못 불렀다"* 라 배선이 있으면 나올 수 없다.

⚠️ **물류·재무는 봉투를 말하는 대역이다.** 둘 다 실 DB 를 읽는데 이 검사는 판매 배선을
  재는 자리다 (`tests/master/conftest.py` 가 막는 범위 밖이다). 대역이어도 **봉투는
  진짜다** — `AgentRequest`/`AgentReply` 를 우회하면 이 검사가 증명하는 것이 없어진다.

---

🔴 **세 번째로 잠그는 것 — 지금 어디까지 가는가.**

`#373` 이 이 블록에 *"경로는 `finance / SALES_VALIDATION` 앞에서 멈춘다"* 를 못 박고
**이어지는 날 빨간불이 나게** 적어 두었다. `#377` 이 정확히 그것을 이었다 —
`business_mode` 를 최상위로 나르고 `requested_quantity_kg` 를 넓혔다. 예고대로 빨개졌고,
아래 ③ 이 **새 도달점**으로 늘어난 자리다.

```text
#337 이전   SL4_NOT_STARTED   어댑터 미등록 — 부서를 한 번도 안 부른다
#373 시점   SL2_NO_CANDIDATE  validation_errors=['business_mode'] — 문 앞에서 거부
지금(#377)  수량 없이         SL2_NO_CANDIDATE · PROPOSAL_QUANTITY_REQUIRED
            수량과 함께       SL1_PRESENTED · 후보 3안 · finance 를 후보마다 부른다
```

★ **갈래가 둘이다.** 앞은 *"판매가 무엇이 없어서 못 냈는지 말한다"* 를, 뒤는 *"경로가
  재무까지 간다"* 를 증명한다. 뒤엣것이 이 파이프라인이 **처음 끝까지 도는 자리**다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main  # import 시점에 판매 어댑터를 등록한다. 이 검사의 전제다
from app.master import persistence, wiring
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.sales.adapter import sales_port

평일 = date(2026, 9, 10)
"""개장 관문이 열려 있고 실행일 관문이 없는 날 — 판매는 주말에도 돌지만 굳이 안 섞는다."""


# ---------------------------------------------------------------------------
# 1. 배선 — `REQUIRED_FOR_SALES` 가 요구하는 것이 전부 등록되는가
# ---------------------------------------------------------------------------


def test_판매_필수_어댑터가_전부_등록된다() -> None:
    """★ **미등록과 후보 0건은 다른 사실이다.** 이 줄이 없으면 앞으로 나간다.

    🔴 **이름을 손으로 적지 않는다.** `wiring.REQUIRED_FOR_SALES` 를 읽는다 — 목록이
      늘면 검사가 따라와야 한다. 여기에 `("sales", "finance")` 를 베껴 두면 셋째가
      필수가 되는 날 **그 셋째는 아무도 안 본다.**
    """
    미등록 = wiring.missing(wiring.REQUIRED_FOR_SALES)

    assert 미등록 == (), (
        f"판매 필수 어댑터가 미등록이다: {미등록}. app/main.py 의 register_agent 를 확인한다"
    )


def test_등록된_것이_판매의_실제_어댑터다() -> None:
    """🔴 **대역이 등록돼 있으면 배선이 있는 것처럼 보이지만 판매가 답하지 않는다.**

    `test_inbound_registration.py` 가 물류 구현체를 확인하는 것과 같은 자리다.
    """
    등록된 = wiring.registry().get("sales")

    assert 등록된 is sales_port, (
        f"등록된 것이 판매 어댑터가 아니다: {getattr(등록된, '__module__', 등록된)}"
    )


# ---------------------------------------------------------------------------
# 2. 🔴 판매 경로가 **실제로 돈다**
# ---------------------------------------------------------------------------


def _reply(request: AgentRequest, **kw: Any) -> AgentReply:
    base: dict[str, Any] = {
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


def _대역(payload: dict[str, Any], 부른_것: list[tuple[str, str]]):
    """봉투를 말하는 대역. **회신을 지어내는 것이 아니라 봉투에 담아 돌려준다.**"""

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        부른_것.append((request.agent, request.mode))
        reply = _reply(request, payload=payload)
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


@pytest.fixture
def 부른_부서(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """물류·재무만 대역으로 갈아 끼운다. **판매는 `app.main` 이 등록한 실물 그대로다.**

    🔴 **판매 이력 적재를 막는다.** `save_sales_agent_run` 은 실 DB 에 INSERT 한다.
       지금은 판매가 계약 오류로 돌아서 그 줄까지 안 가지만, ③ 이 풀리는 날 **조용히
       공용 DB 를 친다** — `tests/master/conftest.py` 가 마스터 쪽 적재를 막는 것과
       같은 이유로 여기서 판매 쪽을 막는다.
    """
    부른_것: list[tuple[str, str]] = []
    monkeypatch.setattr("app.sales.adapter.save_sales_agent_run", lambda **kw: None)
    wiring.register("inventory", _대역({"sellable": "yes"}, 부른_것))
    wiring.register("finance", _대역({"verdict": "ok"}, 부른_것))
    return 부른_것


@pytest.fixture
def client() -> TestClient:
    """🔴 **실 앱이다.** 라우터만 새 `FastAPI` 에 꽂으면 `app/main.py` 를 안 거치고,
    그러면 **등록이 빠져 있어도** 이 검사가 초록이다 — 재는 대상이 바로 그 파일이다.
    """
    return TestClient(app.main.app)


def _본문(client: TestClient, **추가: Any) -> dict[str, Any]:
    """기본 요청은 **수량 없이** 던진다 — 자유 문장만 실린 날이 기본값이다.

    ★ `requested_quantity_kg=...` 를 얹으면 재무까지 가는 갈래가 된다 (아래 ③).
    """
    r = client.post(
        "/master/sales/run",
        json={
            "as_of": 평일.isoformat(),
            "policy_version": "v1.3",
            "business_mode": "SPOT_SALES",
            "item": "배추",
            "user_request": "배추 2톤 다음 주에",
            "partner_id": "P-1",
            **추가,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_등록_뒤에는_SL4_로_서지_않는다(client: TestClient, 부른_부서) -> None:
    """🔴 **이 한 줄이 등록 전과 후를 가른다.**

    `SL4_NOT_STARTED` 는 *"부르기 전에 못 섰다"* 다 (`sales_flow.SalesEndCode`).
    배선이 있으면 나올 수 없는 코드이고, `app/main.py` 의 등록이 리팩터로 사라지면
    **조용히 여기로 돌아온다.**
    """
    본문 = _본문(client)

    assert 본문["end_code"] != "SL4_NOT_STARTED", (
        f"판매가 시작조차 못 했다: {본문['reason']}. app/main.py 의 등록을 확인한다"
    )
    assert "미등록" not in 본문["reason"], f"미등록 사유가 남아 있다: {본문['reason']}"


def test_물류와_판매를_실제로_부른다(client: TestClient, 부른_부서) -> None:
    """★ **계획이 곧 *"누구를 몇 번째로 불렀나"* 다** (M-16).

    ② `inventory / PRE_SALES` → ③ `sales / GENERATE_SALES_PROPOSAL` 순서가 그대로
    남는다. 등록 전에는 이 목록이 **통째로 비어 있었다.**
    """
    본문 = _본문(client)

    걸음 = [(step["agent"], step["mode"]) for step in 본문["plan"]]
    assert 걸음[:2] == [
        ("inventory", "PRE_SALES"),
        ("sales", "GENERATE_SALES_PROPOSAL"),
    ], f"판매 경로가 설계 순서대로 안 돌았다: {걸음}"


def test_판매_회신이_실_어댑터에서_왔다(client: TestClient, 부른_부서) -> None:
    """🔴 **대역이면 이 자리가 조용하다.** 회신의 출처를 회신 자체로 확인한다.

    ★ 대역은 `run_id` 를 `"SALES-2"` 처럼 만들고 실 어댑터는 `uuid4()` 를 쓴다
      (`app/sales/adapter.py` `_run_id`). 값을 비교하지 않고 **모양이 갈리는 것**만
      본다 — 값을 박으면 판매가 id 규칙을 바꾸는 날 마스터 검사가 깨진다.
    """
    본문 = _본문(client)

    판매_걸음 = next(step for step in 본문["plan"] if step["agent"] == "sales")
    assert 판매_걸음["run_id"] != "SALES-2", "판매 자리에 대역이 앉아 있다"
    assert len(판매_걸음["run_id"]) == 36, (
        f"실 어댑터가 만드는 uuid4 모양이 아니다: {판매_걸음['run_id']}"
    )


def test_이력에_돌긴_돈_날로_적힌다(client: TestClient, 부른_부서) -> None:
    """★ **못 시작한 날과 돌긴 돈 날은 런타임 상태가 다르다** (`sales_runtime_status_of`).

    `SL4` 만 `RUNTIME_NOT_READY` 로 적히고 나머지는 `READY` 다. 등록이 사라지면
    이력이 통째로 *"미가동"* 으로 바뀌는데, `end_code` 만 보는 검사는 그 자리를
    안 본다 — 이력을 읽는 사람이 보는 칸은 이쪽이다.
    """
    본문 = _본문(client)

    assert persistence.sales_runtime_status_of(본문["end_code"]) == "READY", (
        f"판매가 돈 날인데 이력에는 미가동으로 적힌다: {본문['end_code']}"
    )


# ---------------------------------------------------------------------------
# 3. 🔴 도달점 — **수량 없이 어디서 멈추고, 수량과 함께 어디까지 가는가**
# ---------------------------------------------------------------------------


def test_수량이_없으면_판매가_무엇이_없는지_말한다(client: TestClient, 부른_부서) -> None:
    """🔴 **못 냈다는 사실이 아니라 *무엇이 없어서* 못 냈는지가 잠글 값이다** (실측 #377).

    자유 문장(`user_request`)만 실린 요청은 판매가 수량을 못 읽는다 — `raw_text` 를
    해석해 수량을 뽑지 않기 때문이다 (`service._sales_user_request` 가 그 사실을
    적어 두었다). 그래서 후보가 0이고 `finance / SALES_VALIDATION` 까지 안 간다.

    ```text
    end_code   SL2_NO_CANDIDATE
    judgment   status=INPUT_INCOMPLETE · missing_data=['PROPOSAL_QUANTITY_REQUIRED']
    후보       0안
    부른 부서   inventory / PRE_SALES 하나 (재무는 안 부른다)
    ```

    ★ **`missing_capabilities` 는 안 잠근다.** 셋(`SELLABLE_SUPPLY_CONTEXT` ·
      `DELIVERY_FEASIBILITY_CONTEXT` · `FINANCIAL_VALIDATION`)은 **두 갈래 모두**에
      실려 나와 갈래를 가르지 못한다 — 갈래를 가르는 칸은 `missing_data` 다.

    ⚠️ **판매가 `raw_text` 에서 수량을 뽑기 시작하면 이 검사가 빨개진다.** 그날은
      `missing_data` 가 비고 아래 재무 갈래와 같은 도달점이 된다 — 그러면 이 검사를
      *"수량을 아예 말하지 않은 요청"* 으로 다시 세우거나 지운다.
    """
    본문 = _본문(client)

    assert 본문["end_code"] == "SL2_NO_CANDIDATE", (
        f"수량 없는 요청이 여기서 안 멈춘다: {본문['end_code']}. "
        "판매가 자유 문장에서 수량을 뽑기 시작했다면 이 검사를 다시 세우고, "
        "SL4 라면 app/main.py 의 등록이 사라진 것이다"
    )
    assert 본문["judgment"]["status"] == "INPUT_INCOMPLETE", (
        f"판매가 '입력이 모자란다' 고 말하지 않는다: {본문['judgment'].get('status')}"
    )
    assert 본문["judgment"]["missing_data"] == ["PROPOSAL_QUANTITY_REQUIRED"], (
        f"판매가 없다고 말한 것이 수량이 아니다: {본문['judgment']['missing_data']}. "
        "판매가 요구하는 칸이 늘었다면 마스터가 나를 칸도 늘어야 한다 "
        "(SalesRunRequest · service._sales_user_request)"
    )
    assert 본문["candidates"] == [], f"입력이 모자란데 후보가 나왔다: {len(본문['candidates'])}안"
    assert ("finance", "SALES_VALIDATION") not in 부른_부서, (
        "후보가 0인데 재무를 불렀다 — 마스터가 없는 후보를 지어냈다"
    )


def test_수량을_실으면_재무_최종검증까지_간다(client: TestClient, 부른_부서) -> None:
    """🔴 **이 파이프라인이 처음 끝까지 도는 자리다** (실측 #377).

    `requested_quantity_kg` 가 실리면 판매가 후보를 내고, 마스터가 **후보마다 한 번씩**
    `finance / SALES_VALIDATION` 을 부른다 (설계 정정 ② · `test_sales_flow.py`).

    ```text
    end_code   SL1_PRESENTED · reason="사용자 선택 대기"
    judgment   status=SCENARIOS_GENERATED · missing_data=[]
    후보       3안 (SALES-001-A · B · C)
    부른 부서   inventory / PRE_SALES → finance / SALES_VALIDATION × 후보 수
    ```

    ★ **후보 수를 숫자로 박지 않는다.** 실측은 3안이지만 몇 안이 나오는지는 **판매
      소유의 사실**이라 판매가 변형 축을 늘리는 날 마스터 검사가 깨진다. 여기서
      잠그는 마스터 소유의 사실은 **순서**와 *"후보 하나에 재무 호출 하나"* 다.

    ⚠️ **재무가 후보를 배열로 받게 바뀌면 이 검사가 빨개진다.** 그날은 재무 호출이
      후보 수만큼이 아니라 한 번이므로, 아래 목록을 `× 1` 로 줄이고 위 블록도 같이
      고친다. `SL3` 으로 바뀌었다면 재무 대역이 후보를 떨어뜨린 것이고,
      `SL4` 라면 `app/main.py` 의 등록이 사라진 것이다.
    """
    본문 = _본문(client, requested_quantity_kg=2000)

    후보 = 본문["candidates"]
    assert 본문["end_code"] == "SL1_PRESENTED", (
        f"수량을 실었는데 제시까지 못 갔다: {본문['end_code']} / {본문['reason']}. "
        "SL2 라면 판매가 요구하는 칸이 또 늘었고 (judgment.missing_data 를 본다), "
        "SL4 라면 app/main.py 의 등록이 사라진 것이다"
    )
    assert 본문["judgment"]["status"] == "SCENARIOS_GENERATED", (
        f"판매가 후보를 냈다고 말하지 않는다: {본문['judgment'].get('status')}"
    )
    assert 본문["judgment"]["missing_data"] == [], (
        f"수량을 실었는데 아직 모자란 입력이 있다: {본문['judgment']['missing_data']}"
    )
    assert 후보, "후보가 0안이다 — 재무까지 갈 것이 없다"

    # 🔴 **순서까지 잠근다.** 판매는 실물이라 이 목록에 안 남는다 (대역만 기록한다) —
    #    판매 자리는 위 `test_물류와_판매를_실제로_부른다` 가 `plan` 으로 본다.
    기대 = [("inventory", "PRE_SALES")] + [("finance", "SALES_VALIDATION")] * len(후보)
    assert 부른_부서 == 기대, (
        f"재무까지 가는 순서가 설계와 다르다: {부른_부서} (후보 {len(후보)}안)"
    )
