"""매입 입력 `confirmed_orders` 는 **이 실행의 판매만** 읽는다.

2026-09-13 · 걷기끼리 새는 문제의 원인.

```text
같은 코드로 다시 걸면 무 매입이 판마다 11~14kg 늘었다 (3월 19일부터 · 무에서만)
```

🔴 `_orders_from_db` 조회에 `sim_run_id` 조건이 없었다. 공유 DB 에는 걷기마다 마지막 날
  확정하고 구간 밖에서 출고될 주문이 `CONFIRMED` 로 남는다. 창(14일)이 그 날을 덮는
  순간부터 **앞 판들의 잔여 주문이 이번 판의 확정 수요로 읽혔다.**

★ **대역 DB 는 SQL 을 실제로 해석한다** (sqlite 메모리). 조회 인자만 보는 대역은
  조건을 지워도 같은 행을 돌려주므로 이 결함을 못 잰다. 그리고 **다른 실행의 판매를
  같이 심는다** — 한 실행만 심으면 축을 지워도 초록이다.

⚠️ **실 DB 를 타지 않는다.** `fetch_all` · `fetch_one` 을 갈아 끼운다.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import date
from typing import Any

import pytest

from app.master import inputs, service
from app.master.inputs import MasterInputs, SourcedInput

ITEM = "무"
AS_OF = date(2026, 3, 19)
MINE = "SIM-CHAIN-V12"
OTHER = "SIM-CHAIN-V11"


# ── 대역 DB ─────────────────────────────────────────────────────────────


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ':memory:' AS haetdeul")
    conn.executescript(
        """
        CREATE TABLE haetdeul.items (item_id INTEGER PRIMARY KEY, item_name TEXT);
        CREATE TABLE haetdeul.sales (
            sale_id TEXT PRIMARY KEY, sim_run_id TEXT, sale_date TEXT, order_status TEXT
        );
        CREATE TABLE haetdeul.sale_items (
            sale_item_id INTEGER PRIMARY KEY, sale_id TEXT, item_id INTEGER, quantity_kg REAL
        );
        INSERT INTO haetdeul.items VALUES (1, '무'), (2, '배추');
        """
    )
    sales = [
        # 이 실행의 확정 주문 — 이것만 수요다
        ("S-MINE-1", MINE, "2026-03-25", "CONFIRMED", 1, 30.0),
        ("S-MINE-2", MINE, "2026-03-27", "READY", 1, 12.0),
        # 🔴 다른 실행이 구간 끝에 남긴 잔여 주문 — 새면 안 된다
        ("S-OTHER-1", OTHER, "2026-04-01", "CONFIRMED", 1, 21.0),
        ("S-OTHER-2", OTHER, "2026-03-25", "READY", 1, 772.0),
        # 이 실행이어도 걸러지는 것들 (상태 · 창 · 품목)
        ("S-MINE-DONE", MINE, "2026-03-26", "DELIVERED", 1, 99.0),
        ("S-MINE-FAR", MINE, "2026-04-10", "CONFIRMED", 1, 99.0),
        ("S-MINE-CAB", MINE, "2026-03-26", "CONFIRMED", 2, 99.0),
    ]
    for n, (sale_id, run, day, status, item_id, kg) in enumerate(sales, start=1):
        conn.execute("INSERT INTO haetdeul.sales VALUES (?, ?, ?, ?)", (sale_id, run, day, status))
        conn.execute(
            "INSERT INTO haetdeul.sale_items VALUES (?, ?, ?, ?)", (n, sale_id, item_id, kg)
        )
    return conn


def _param(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


@pytest.fixture
def 대역_DB(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = _db()
    conn.row_factory = sqlite3.Row

    def fetch_all(query: Any, params: Any = ()) -> list[dict[str, Any]]:
        text = query.as_string(None).replace("%s", "?")
        rows = conn.execute(text, [_param(p) for p in params]).fetchall()
        return [
            {**dict(r), "sale_date": date.fromisoformat(r["sale_date"])}
            if "sale_date" in dict(r)
            else dict(r)
            for r in rows
        ]

    def fetch_one(*_a: Any, **_k: Any) -> None:
        raise AssertionError("실제 주문을 읽어야 할 자리에서 파생 경로로 떨어졌다")

    monkeypatch.setattr(inputs, "fetch_all", fetch_all)
    monkeypatch.setattr(inputs, "fetch_one", fetch_one)
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")
    return conn


# ── ① 🔴 남의 실행 판매가 새지 않는다 ────────────────────────────────────


def test_다른_실행의_확정_주문은_이번_실행의_수요가_아니다(대역_DB):
    """★★ **이 판의 핵심.** 창 안에 다른 실행의 `CONFIRMED` 가 있어도 안 읽는다."""
    got = inputs.load_confirmed_orders(ITEM, AS_OF, sim_run_id=MINE)

    assert got.grade == "MEASURED", got.note
    ids = [o["sale_id"] for o in got.payload["orders"]]
    assert ids == ["S-MINE-1", "S-MINE-2"], f"다른 실행의 판매가 샜다: {ids}"
    assert got.payload["total_kg"] == pytest.approx(42.0)


def test_실행이_바뀌면_읽는_주문도_바뀐다(대역_DB):
    """같은 날 같은 품목이어도 축이 다르면 답이 갈린다 — 축이 조회에 실제로 걸렸다는 증거."""
    mine = inputs.load_confirmed_orders(ITEM, AS_OF, sim_run_id=MINE)
    other = inputs.load_confirmed_orders(ITEM, AS_OF, sim_run_id=OTHER)

    assert {o["sale_id"] for o in other.payload["orders"]} == {"S-OTHER-1", "S-OTHER-2"}
    assert mine.payload["total_kg"] != other.payload["total_kg"]


# ── ② 🔴 기본값이 없다 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fn",
    [
        inputs.collect_inputs,
        inputs.load_confirmed_orders,
        inputs._orders_from_db,
        service._inputs_for,
    ],
    ids=lambda f: f.__name__,
)
def test_실행_축에_기본값이_없다(fn):
    """🔴 **오늘까지 축 결함 여덟이 전부 기본값을 박았거나 빠뜨린 자리였다.**

    안 넘기면 호출 자리에서 터져야 한다. 기본값이 있으면 조용히 그 실행을 읽는다.
    """
    param = inspect.signature(fn).parameters.get("sim_run_id")
    assert param is not None, f"{fn.__name__} 이 실행 축을 안 받는다"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{fn.__name__} 축이 키워드 전용이 아니다"
    assert param.default is inspect.Parameter.empty, (
        f"{fn.__name__} 의 sim_run_id 에 기본값이 있다: {param.default!r}"
    )


# ── ③ 🔴 부르는 자리가 축을 그대로 넘긴다 ────────────────────────────────


def test_collect_inputs_가_받은_축을_확정_주문에_넘긴다(monkeypatch):
    seen: list[str] = []

    def load(item: str, as_of: date, *, sim_run_id: str) -> SourcedInput:
        seen.append(sim_run_id)
        return SourcedInput("confirmed_orders", None, "MISSING", "-", "")

    monkeypatch.setattr(inputs, "load_confirmed_orders", load)
    monkeypatch.setattr(
        inputs, "load_forecast", lambda *a, **k: SourcedInput("forecast", None, "MISSING", "-", "")
    )
    monkeypatch.setattr(
        inputs,
        "load_policy_values",
        lambda *a, **k: SourcedInput("policy_values", None, "MISSING", "-", ""),
    )

    inputs.collect_inputs(ITEM, AS_OF, sim_run_id=MINE)

    assert seen == [MINE]


def test_run_procurement_이_봉투의_축으로_입력을_모은다(monkeypatch):
    """🔴 **요청이 준 축이 적재층까지 간다.** 번인 상수로 끊기면 걷기가 남의 판매를 읽는다."""
    from app.master import wiring
    from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
    from app.master.schemas import ProcurementRunRequest

    seen: list[dict[str, Any]] = []
    missing = MasterInputs(
        forecast=SourcedInput("forecast", None, "MISSING", "-", ""),
        confirmed_orders=SourcedInput("confirmed_orders", None, "MISSING", "-", ""),
        policy_values=SourcedInput("policy_values", None, "MISSING", "-", ""),
    )

    def collect(item: str, as_of: date, **kwargs: Any) -> MasterInputs:
        seen.append(kwargs)
        return missing

    def port(request: AgentRequest):
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload={"cap": 1},
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    monkeypatch.setattr("app.master.service.collect_inputs", collect)
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: None)
    wiring.reset()
    for agent in ("finance", "inventory", "purchase"):
        wiring.register(agent, port)

    service.run_procurement(
        ProcurementRunRequest(
            as_of=date(2025, 12, 31),
            policy_version="v1.3",
            item="배추",
            request_id="REQ-AXIS-1",
            sim_run_id=MINE,
        ),
        verifier=None,
    )

    assert seen, "입력을 모으지 않았다 — 이 검사가 의미 없다"
    assert seen[0].get("sim_run_id") == MINE, f"봉투의 축이 적재층에 안 갔다: {seen[0]}"
