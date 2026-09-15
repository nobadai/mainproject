"""화면이 읽는 실행은 `app/api/shown_run.py` 한 자리다.

🔴 **실 DB 에 닿지 않는다.** 서비스 함수와 커넥션을 대역으로 바꿔 «무엇을 넘기는가» 만 본다.

```text
① app/api 아래 파이썬 소스에 BURN_IN_SIM_RUN_ID 이름이 한 번도 안 쓰인다 (AST)
② 재무 · 판매 · 물류 build 가 서비스에 SHOWN_SIM_RUN_ID 를 넘긴다
③ 대시보드가 매입 build 에 sim_run_id=SHOWN_SIM_RUN_ID 를 넘긴다
④ 매입 라우터: 쿼리 없음 → SHOWN · 쿼리 있음 → 준 값
⑤ 프론트 기준일 코드값 == SHOWN_AS_OF
```
"""

from __future__ import annotations

import ast
import re
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import shown_run
from app.api.dashboard import query as dashboard_query
from app.api.finance import query as finance_query
from app.api.logistics import query as logistics_query
from app.api.purchase import routes as purchase_routes
from app.api.sales import query as sales_query
from app.api.shown_run import SHOWN_AS_OF, SHOWN_SIM_RUN_ID

_BACKEND = Path(__file__).resolve().parents[2]
_API_DIR = _BACKEND / "app" / "api"
_DEMO_AS_OF = _BACKEND.parent / "frontend" / "src" / "lib" / "demo_as_of.ts"
_BURN_IN = "BURN_IN_SIM_RUN_ID"

AS_OF = date(2026, 1, 26)


class _멈춤(Exception):
    """넘긴 값을 잡은 뒤 계산을 더 진행하지 않으려고 던진다."""


# ── ① 스캔 잠금 ─────────────────────────────────────────────────────────


def test_화면_API_소스에_번인_상수_이름이_없다():
    """🔴 `import` · 이름 · 속성 어디로도 번인 상수를 쓰지 않는다. 문자열은 안 본다."""
    files = sorted(p for p in _API_DIR.rglob("*.py") if "__pycache__" not in p.parts)
    assert files, f"스캔한 파일이 0개다 — 경로가 틀렸다: {_API_DIR}"

    found: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            hit = (
                (isinstance(node, ast.Name) and node.id == _BURN_IN)
                or (isinstance(node, ast.Attribute) and node.attr == _BURN_IN)
                or (
                    isinstance(node, ast.ImportFrom)
                    and any(alias.name == _BURN_IN for alias in node.names)
                )
            )
            if hit:
                found.append(f"{path.relative_to(_BACKEND)}:{node.lineno}")
    assert not found, f"화면 API 가 번인 상수를 쓴다: {found}"


# ── ② 재무 · 판매 · 물류 ────────────────────────────────────────────────


def test_재무_build_가_보는_실행을_넘긴다(monkeypatch):
    잡은: list[tuple[str, str]] = []

    def 대시보드(*, sim_run_id: str, as_of: date) -> Any:
        잡은.append(("dashboard", sim_run_id))
        return object()

    def 현금흐름(*, sim_run_id: str, as_of: date, days: int) -> Any:
        잡은.append(("cashflow", sim_run_id))
        raise _멈춤

    monkeypatch.setattr(finance_query, "get_finance_dashboard", 대시보드)
    monkeypatch.setattr(finance_query, "get_finance_cashflow", 현금흐름)
    with pytest.raises(_멈춤):
        finance_query.build(AS_OF, "base")
    assert 잡은 == [("dashboard", SHOWN_SIM_RUN_ID), ("cashflow", SHOWN_SIM_RUN_ID)]


def test_판매_build_가_보는_실행을_넘긴다(monkeypatch):
    잡은: list[str] = []

    def 대시보드(*, sim_run_id: str, as_of: date) -> Any:
        잡은.append(sim_run_id)
        raise _멈춤

    monkeypatch.setattr(sales_query, "get_sales_dashboard", 대시보드)
    with pytest.raises(_멈춤):
        sales_query.build(AS_OF)
    assert 잡은 == [SHOWN_SIM_RUN_ID]


@contextmanager
def _가짜_커넥션():
    yield object()


def _물류_대역(monkeypatch) -> list[tuple[str, str]]:
    잡은: list[tuple[str, str]] = []

    def 기록(name: str, result: Any):
        def 대역(*args: Any, sim_run_id: str, **kwargs: Any) -> Any:
            잡은.append((name, sim_run_id))
            return result

        return 대역

    열림 = SimpleNamespace(has_snapshot=True, first_as_of=AS_OF, last_as_of=AS_OF)
    monkeypatch.setattr(logistics_query, "get_connection", _가짜_커넥션)
    monkeypatch.setattr(logistics_query, "runtime_coverage_at", 기록("coverage", 열림))
    monkeypatch.setattr(logistics_query, "onhand_total_by_day", 기록("onhand", {}))
    monkeypatch.setattr(logistics_query, "snapshot_days_between", 기록("days", set()))
    #  ★ Runtime 읽기는 **한 판에 한 번** 이고 두 콘솔이 나눠 쓴다 (2026-09-15).
    monkeypatch.setattr(logistics_query, "load_console_runtime", 기록("runtime", None))
    monkeypatch.setattr(
        logistics_query, "get_inbound_console", 기록("inbound", SimpleNamespace(in_transit=[]))
    )
    monkeypatch.setattr(logistics_query, "get_inventory_console", 기록("inventory", None))
    monkeypatch.setattr(logistics_query, "get_outbound_console", 기록("outbound", None))
    #  ★ **Exception 두 조회도 같은 축에 선다** (#675). 창고 배치(`warehouse`) 는
    #    발표 화면에서 빠져 부르지 않는다.
    monkeypatch.setattr(logistics_query, "live_exceptions_at", 기록("live_exceptions", None))
    monkeypatch.setattr(
        logistics_query, "resolved_exceptions_on", 기록("resolved_exceptions", ())
    )
    return 잡은


def test_물류_build_가_보는_실행을_넘기고_출처에_적는다(monkeypatch):
    잡은 = _물류_대역(monkeypatch)
    #  대역 콘솔이 빈 값이라 판 조립은 실패한다. 넘긴 축과 출처 글만 본다.
    result = logistics_query.build_result(AS_OF, "summary")
    names = [name for name, _ in 잡은]
    assert names == [
        "coverage",
        "runtime",
        "inventory",
        "inbound",
        "outbound",
        "live_exceptions",
        "resolved_exceptions",
    ]
    assert {run for _, run in 잡은} == {SHOWN_SIM_RUN_ID}
    assert f"보고 있는 실행: {SHOWN_SIM_RUN_ID} · 기준일: {AS_OF}" in (result.tab.source.note or "")


def test_물류_재고그래프가_보는_실행을_넘긴다(monkeypatch):
    잡은 = _물류_대역(monkeypatch)
    logistics_query.dashboard_stock(10, 5, AS_OF)
    names = [name for name, _ in 잡은]
    assert names == ["coverage", "onhand", "days", "runtime", "inbound"]
    assert {run for _, run in 잡은} == {SHOWN_SIM_RUN_ID}


# ── ③ 대시보드 → 매입 ───────────────────────────────────────────────────


def test_대시보드가_매입_build_에_보는_실행을_넘긴다(monkeypatch):
    잡은: list[dict[str, Any]] = []

    def 매입(as_of: date, *args: Any, **kwargs: Any) -> Any:
        잡은.append({"args": args, "kwargs": kwargs})
        raise _멈춤

    monkeypatch.setattr(dashboard_query.forecast_q, "build", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(dashboard_query.purchase_q, "build", 매입)
    with pytest.raises(_멈춤):
        dashboard_query.build(AS_OF)
    assert 잡은 == [{"args": (), "kwargs": {"sim_run_id": SHOWN_SIM_RUN_ID}}]


# ── ④ 매입 라우터 ───────────────────────────────────────────────────────


@pytest.fixture
def 매입_라우터(monkeypatch):
    잡은: list[Any] = []

    def 매입(as_of: date, sim_run_id: str | None = None) -> Any:
        잡은.append(sim_run_id)
        raise _멈춤

    monkeypatch.setattr(purchase_routes, "build", 매입)
    app = FastAPI()
    app.include_router(purchase_routes.router)
    return TestClient(app, raise_server_exceptions=False), 잡은


def test_매입_라우터는_축이_없으면_보는_실행을_쓴다(매입_라우터):
    client, 잡은 = 매입_라우터
    client.get("/purchase", params={"as_of": AS_OF.isoformat()})
    assert 잡은 == [SHOWN_SIM_RUN_ID]


def test_매입_라우터는_축을_주면_준_값이_이긴다(매입_라우터):
    client, 잡은 = 매입_라우터
    client.get("/purchase", params={"as_of": AS_OF.isoformat(), "sim_run_id": "SIM-OTHER"})
    assert 잡은 == ["SIM-OTHER"]


# ── ⑤ 프론트 기준일 ─────────────────────────────────────────────────────


def test_프론트_기준일이_SHOWN_AS_OF_와_같다():
    text = _DEMO_AS_OF.read_text(encoding="utf-8")
    values = re.findall(r'\?\?\s*"(\d{4}-\d{2}-\d{2})"', text)
    assert len(values) == 1, f"demo_as_of.ts 에서 `?? \"YYYY-MM-DD\"` 를 하나로 못 찾았다: {values}"
    assert values[0] == SHOWN_AS_OF.isoformat(), (
        f"프론트 기준일 {values[0]} 이 백엔드 SHOWN_AS_OF {SHOWN_AS_OF} 와 갈렸다"
    )


def test_설정_파일은_두_값만_둔다():
    """🔴 환경변수 덮어쓰기 같은 두 번째 주인을 만들지 않는다."""
    public = {name for name in vars(shown_run) if name.isupper()}
    assert public == {"SHOWN_SIM_RUN_ID", "SHOWN_AS_OF"}
    assert "environ" not in Path(shown_run.__file__).read_text(encoding="utf-8")
