"""재고·물류 화면 계약을 잠근다 (#675 · 발표용 종합 화면).

```text
한눈에 보기   기준일에 열린 · 지속되는 · 해소된 물류 문제 + 창고 여유 + 품목별
재고 · 신선도  Lot 별 신선도
입고 · 검수   도착 · 검수 · 재고 반영
예약 · 출고   예약 · FEFO 후보
```

🔴 **이 파일이 지키는 것은 «무엇을 보여 주는가» 가 아니라 «무엇을 안 지어내는가» 다.**

  ① 다른 실행의 문제를 섞지 않는다
  ② 기준일 뒤에 열린 문제를 섞지 않는다
  ③ 기준일 뒤에 갱신된 우선도를 그날 값처럼 적지 않는다
  ④ `None` · `NO_DATA` · `ERROR` 를 0 으로 바꾸지 않는다
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.logistics import query as logistics_query
from app.api.logistics import routes as logistics_routes
from app.api.shown_run import SHOWN_SIM_RUN_ID
from app.logistics.monitoring.schemas import ExceptionEvidence, ExceptionRow

AS_OF = date(2026, 3, 10)
ITEM_ON_SCREEN = "배추"
ITEM_OFF_SCREEN = "피마늘"

_EVIDENCE = (
    ExceptionEvidence(
        fact="remaining_freshness_days",
        value="2",
        unit="일",
        source="inventory_lots",
        source_id="LOT-A",
        observed_as_of=AS_OF,
    ),
)


def 문제(
    exception_id: str,
    *,
    opened: date,
    detected: date,
    code: str = "FRESHNESS_PRESSURE",
    severity: str = "HIGH",
    status: str = "OPEN",
    subject_id: str = "LOT-A",
    resolved: date | None = None,
) -> ExceptionRow:
    return ExceptionRow(
        exception_id=exception_id,
        sim_run_id=SHOWN_SIM_RUN_ID,
        code=code,
        subject_type="LOT",
        subject_id=subject_id,
        severity=severity,
        status=status,
        opened_as_of=opened,
        last_detected_as_of=detected,
        observed_as_of=opened,
        evidence=_EVIDENCE,
        detector_version="v1",
        resolved_as_of=resolved,
    )


def 품목(name: str, *, available: Decimal | None = Decimal(10)) -> Any:
    return SimpleNamespace(
        item_id=name,
        item_name=name,
        on_hand_qty_kg=Decimal(100),
        available_qty_kg=available,
        reserved_qty_kg=Decimal(5),
        allocated_qty_kg=Decimal(0),
        unallocated_reserved_qty_kg=Decimal(5),
        active_reservation_count=1,
        sell_priority_lot_count=1,
        expired_lot_count=1,
        expired_qty_kg=Decimal(3),
        disposal_candidate_lot_count=1,
    )


def 로트(lot_id: str = "LOT-A", item_name: str = ITEM_ON_SCREEN) -> Any:
    return SimpleNamespace(
        lot_id=lot_id,
        item_id=item_name,
        item_name=item_name,
        grade="상",
        remaining_qty_kg=Decimal(100),
        received_at=AS_OF,
        status="ACTIVE",
        storage_zone=None,
        remaining_freshness_days=2,
        remaining_turnover_days=3,
        turnover_status="SELL_PRIORITY",
        sell_priority=True,
        disposal_candidate=False,
    )


def 재고(items: list[Any]) -> Any:
    return SimpleNamespace(
        items=items,
        lots=[로트()],
        available_qty_unresolved_reason=None,
        capacity=SimpleNamespace(
            used_capacity_kg=Decimal(450),
            guaranteed_capacity_kg=Decimal(1000),
            burst_capacity_kg=Decimal(1200),
        ),
    )


def 한눈에(pane_list: list[Any]) -> Any:
    return next(p for p in pane_list if p.key == "summary")


def 카드(pane: Any, key: str) -> Any:
    return next(c for c in pane.cards if c.key == key)


def 통계(pane: Any, label: str) -> Any:
    return next(s for s in pane.stats if s.label == label)


@pytest.fixture
def 화면(monkeypatch):
    """`build_result` 를 대역으로 돌리는 한 판. **DB 를 안 탄다.**"""

    잡은: dict[str, Any] = {}

    def 세우기(
        *,
        live: tuple[ExceptionRow, ...],
        resolved: tuple[ExceptionRow, ...],
        items: list[Any] | None = None,
        uncertainties: tuple[str, ...] = (),
    ) -> Any:
        inv = 재고(items if items is not None else [품목(ITEM_ON_SCREEN)])
        monkeypatch.setattr(logistics_query, "get_connection", lambda: _커넥션())
        monkeypatch.setattr(
            logistics_query,
            "runtime_coverage_at",
            lambda *a, **k: SimpleNamespace(has_snapshot=True, first_as_of=AS_OF, last_as_of=AS_OF),
        )
        #  ★ Runtime 읽기는 한 판에 한 번 — 대역은 «그날 스냅샷 없음» 으로 둔다.
        monkeypatch.setattr(logistics_query, "load_console_runtime", lambda **k: None)
        #  그날 예약을 한 판에 한 번 읽는다 (#760) — 콘솔 대역이 값을 무시하므로 빈 축.
        monkeypatch.setattr(logistics_query, "reservation_state_at", lambda *a, **k: ())
        monkeypatch.setattr(logistics_query, "get_inventory_console", lambda **k: inv)
        monkeypatch.setattr(
            logistics_query,
            "get_inbound_console",
            lambda **k: SimpleNamespace(
                in_transit=[],
                in_transit_status="OK",
                receipts=[],
                arrival_summary=_도착요약(),
            ),
        )
        monkeypatch.setattr(
            logistics_query, "get_outbound_console", lambda **k: SimpleNamespace(reservations=[])
        )

        def 살아있는(conn: Any, *, sim_run_id: str, as_of: date) -> Any:
            잡은["live"] = (sim_run_id, as_of)
            return SimpleNamespace(rows=live, membership_dates=(), uncertainties=uncertainties)

        def 닫힌(conn: Any, *, sim_run_id: str, as_of: date) -> Any:
            잡은["resolved"] = (sim_run_id, as_of)
            return resolved

        monkeypatch.setattr(logistics_query, "live_exceptions_at", 살아있는)
        monkeypatch.setattr(logistics_query, "resolved_exceptions_on", 닫힌)
        return logistics_query.build_result(AS_OF, "summary")

    세우기.잡은 = 잡은  # type: ignore[attr-defined]
    return 세우기


class _커넥션:
    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def _도착요약() -> Any:
    return SimpleNamespace(
        due_count=0,
        overdue_count=0,
        blocked_count=0,
        unresolved_count=0,
        source_status="OK",
    )


#  ── 화면 계약 ────────────────────────────────────────────────────────────


def test_화면_탭은_넷이고_창고_배치는_없다():
    assert logistics_query.PANES == ("summary", "stock", "inbound", "outbound")
    assert "warehouse" not in logistics_query.PANES


def test_기본_탭은_한눈에_보기다():
    기본 = logistics_routes.logistics_tab.__defaults__
    assert 기본 is not None and "summary" in 기본


def test_그날이_없는_날도_한눈에_보기로_연다(monkeypatch):
    monkeypatch.setattr(logistics_query, "get_connection", lambda: _커넥션())
    monkeypatch.setattr(
        logistics_query,
        "runtime_coverage_at",
        lambda *a, **k: SimpleNamespace(has_snapshot=False, first_as_of=AS_OF, last_as_of=AS_OF),
    )
    tab = logistics_query.build_result(AS_OF, "summary").tab
    assert tab.selected == "summary"
    assert [p.key for p in tab.panes] == ["summary", "stock", "inbound", "outbound"]


#  ── 실행 축 ──────────────────────────────────────────────────────────────


def test_문제_조회도_보는_실행과_요청_기준일을_쓴다(화면):
    화면(live=(), resolved=())
    assert 화면.잡은["live"] == (SHOWN_SIM_RUN_ID, AS_OF)
    assert 화면.잡은["resolved"] == (SHOWN_SIM_RUN_ID, AS_OF)


#  ── 신규 · 지속 · 해소 ───────────────────────────────────────────────────


def test_기준일에_열린_문제는_신규다(화면):
    result = 화면(live=(문제("E1", opened=AS_OF, detected=AS_OF),), resolved=())
    pane = 한눈에(result.tab.panes)
    assert 통계(pane, "신규").value == "1"
    assert 통계(pane, "지속 중").value == "0"
    행 = 카드(pane, "exceptions").table.rows
    assert [r["state"] for r in 행] == ["신규"]


def test_그전에_열려_살아_있으면_지속_중이다(화면):
    result = 화면(live=(문제("E1", opened=date(2026, 3, 1), detected=AS_OF),), resolved=())
    pane = 한눈에(result.tab.panes)
    assert 통계(pane, "신규").value == "0"
    assert 통계(pane, "지속 중").value == "1"
    assert [r["state"] for r in 카드(pane, "exceptions").table.rows] == ["지속 중"]


def test_기준일에_닫힌_문제는_해소로_세고_확인_필요에_안_섞인다(화면):
    result = 화면(
        live=(문제("E1", opened=date(2026, 3, 1), detected=AS_OF),),
        resolved=(
            문제(
                "E2",
                opened=date(2026, 2, 20),
                detected=AS_OF,
                status="RESOLVED",
                resolved=AS_OF,
            ),
        ),
    )
    pane = 한눈에(result.tab.panes)
    assert (통계(pane, "신규").value, 통계(pane, "지속 중").value, 통계(pane, "해소").value) == (
        "0",
        "1",
        "1",
    )
    상태 = [r["state"] for r in 카드(pane, "exceptions").table.rows]
    assert 상태 == ["지속 중", "해소됨"]


def test_용량_압박은_따로_센다(화면):
    result = 화면(
        live=(
            문제("E1", opened=AS_OF, detected=AS_OF),
            문제("E2", opened=AS_OF, detected=AS_OF, code="CAPACITY_PRESSURE", subject_id="WH-1"),
        ),
        resolved=(),
    )
    pane = 한눈에(result.tab.panes)
    assert 통계(pane, "용량 압박").value == "1"
    종류 = {r["kind"] for r in 카드(pane, "exceptions").table.rows}
    assert 종류 == {"신선도 압박", "용량 압박"}


#  ── 시간축이 새지 않는가 ─────────────────────────────────────────────────


def test_기준일_뒤에_갱신된_우선도를_그날_값으로_적지_않는다(화면):
    """`touch_exception` 이 `severity` 를 덮어쓴다 — 미래 값이 새면 안 된다."""
    result = 화면(
        live=(
            문제("E1", opened=date(2026, 3, 1), detected=date(2026, 3, 20), severity="CRITICAL"),
        ),
        resolved=(),
    )
    행 = 카드(한눈에(result.tab.panes), "exceptions").table.rows[0]
    assert "매우 높음" not in str(행["sev"])
    assert "확인 불가" in str(행["sev"])
    assert 행["seen"] == "—"


def test_기준일_이전에_마지막으로_본_문제는_우선도를_그대로_쓴다(화면):
    result = 화면(
        live=(문제("E1", opened=date(2026, 3, 1), detected=date(2026, 3, 5), severity="CRITICAL"),),
        resolved=(),
    )
    행 = 카드(한눈에(result.tab.panes), "exceptions").table.rows[0]
    assert 행["sev"] == "매우 높음"
    assert 행["seen"] == "03-05"


#  ── 품목 표시 범위 ───────────────────────────────────────────────────────


def test_발표_화면은_운영_품목만_그린다(화면):
    result = 화면(
        live=(),
        resolved=(),
        items=[품목(ITEM_ON_SCREEN), 품목(ITEM_OFF_SCREEN)],
    )
    표 = 카드(한눈에(result.tab.panes), "items").table
    assert [r["item"] for r in 표.rows] == [ITEM_ON_SCREEN]


def test_창고_사용량은_화면_품목_필터보다_앞선다(화면):
    """범위 밖 품목의 실물도 창고를 차지한다 — 사용량에서 빼지 않는다."""
    result = 화면(live=(), resolved=(), items=[품목(ITEM_ON_SCREEN), 품목(ITEM_OFF_SCREEN)])
    사용량 = next(
        s for s in 카드(한눈에(result.tab.panes), "capacity").stats if s.label == "현재 사용량"
    )
    assert 사용량.raw == 450.0


#  ── None 을 0 으로 바꾸지 않는가 ─────────────────────────────────────────


def test_판매가능량이_없으면_공란이고_0_이_아니다(화면):
    result = 화면(live=(), resolved=(), items=[품목(ITEM_ON_SCREEN, available=None)])
    행 = 카드(한눈에(result.tab.panes), "items").table.rows[0]
    assert 행["avail"] is None


def test_목록이_확정되지_않으면_그_사실을_먼저_적는다(화면):
    result = 화면(live=(), resolved=(), uncertainties=("CLOSE_DATE_UNRESOLVED:E9",))
    lead = 카드(한눈에(result.tab.panes), "exceptions").lead
    assert lead is not None and "확정되지 않았" in lead.text


#  ── 예약 · 출고: 끝난 예약은 안 그리고, FEFO 는 미할당이 남은 예약에만 묻는다 (2026-09-15) ──


def _resv(rid: str, *, status: str, allocated: str, unallocated: str, shipped: bool) -> Any:
    return SimpleNamespace(
        reservation_id=rid, item_id=ITEM_ON_SCREEN, item_name=ITEM_ON_SCREEN, sale_id="S",
        required_qty_kg=Decimal(100), reserved_qty_kg=Decimal(100),
        allocated_qty_kg=Decimal(allocated), unallocated_qty_kg=Decimal(unallocated),
        due_date=None, status=status,
        allocations=[SimpleNamespace(status="SHIPPED")] if shipped else [],
    )


def _가짜_후보(물은것: list[tuple[str, ...]]):
    """`get_fefo_candidates_by_item` 대역 — 어느 품목을 물었는지 적고 품목마다 후보 하나."""

    def 대역(*, conn: Any, sim_run_id: str, item_ids: Any, as_of: date):
        품목 = sorted(set(item_ids))
        물은것.append(tuple(품목))
        후보 = SimpleNamespace(
            lot_id="LOT-1", grade="상", available_qty_kg=Decimal(10), remaining_freshness_days=3
        )
        return {item_id: [후보] for item_id in 품목}

    return 대역


def test_FEFO_는_미할당이_남은_예약에만_그리고_끝난_예약은_표에서_뺀다(monkeypatch):
    물은것: list[tuple[str, ...]] = []
    monkeypatch.setattr(logistics_query, "get_fefo_candidates_by_item", _가짜_후보(물은것))
    ob = SimpleNamespace(reservations=[
        #  전량 출고 · Lot 아직 안 고름 · 배정됐지만 미출고 · 놓아줌
        _resv("R-DONE", status="ALLOCATED", allocated="0", unallocated="0", shipped=True),
        _resv("R-WAIT", status="RESERVED", allocated="0", unallocated="100", shipped=False),
        _resv("R-HOLD", status="ALLOCATED", allocated="100", unallocated="0", shipped=False),
        _resv("R-GONE", status="RELEASED", allocated="0", unallocated="0", shipped=False),
    ])
    pane = logistics_query._outbound_pane(ob, AS_OF, conn=None, sim_run_id="SIM")
    #  🔴 품목마다 한 번 묻고(164번 묻던 자리), 표에는 미할당이 남은 예약만 오른다.
    assert 물은것 == [(ITEM_ON_SCREEN,)]
    fefo = 카드(pane, "fefo").table
    assert fefo is not None and [row["resv"] for row in fefo.rows] == ["R-WAIT"]
    예약 = next(s for s in pane.stats if s.label == "예약")
    assert 예약.value == "2" and "2건은 뺐습니다" in (예약.detail or "")  # 숨기지 않고 적는다


def test_FEFO_는_같은_품목_예약_여럿에_한_번만_묻고_예약마다_순서를_다시_센다(monkeypatch):
    물은것: list[tuple[str, ...]] = []
    monkeypatch.setattr(logistics_query, "get_fefo_candidates_by_item", _가짜_후보(물은것))
    ob = SimpleNamespace(reservations=[
        _resv("R-1", status="RESERVED", allocated="0", unallocated="100", shipped=False),
        _resv("R-2", status="RESERVED", allocated="0", unallocated="50", shipped=False),
    ])
    pane = logistics_query._outbound_pane(ob, AS_OF, conn=None, sim_run_id="SIM")
    assert 물은것 == [(ITEM_ON_SCREEN,)]
    fefo = 카드(pane, "fefo").table
    assert fefo is not None
    assert [(row["resv"], row["rank"]) for row in fefo.rows] == [("R-1", 1), ("R-2", 1)]


def test_그릴_예약이_없으면_FEFO_를_묻지도_않는다(monkeypatch):
    물은것: list[tuple[str, ...]] = []
    monkeypatch.setattr(logistics_query, "get_fefo_candidates_by_item", _가짜_후보(물은것))
    ob = SimpleNamespace(reservations=[
        _resv("R-HOLD", status="ALLOCATED", allocated="100", unallocated="0", shipped=False),
    ])
    pane = logistics_query._outbound_pane(ob, AS_OF, conn=None, sim_run_id="SIM")
    assert 물은것 == []
    assert next(s for s in pane.stats if s.label == "FEFO 후보").value == "0"


def test_화면_한_판은_커넥션_하나로_읽는다(화면, monkeypatch):
    """🔴 조회마다 커넥션을 새로 열던 구조(한 판 23개)를 잠근다."""
    열린것: list[int] = []

    def 세는_커넥션():
        열린것.append(1)
        return _커넥션()

    result = 화면(live=(), resolved=())
    assert result.http_status == 200
    monkeypatch.setattr(logistics_query, "get_connection", 세는_커넥션)
    logistics_query.build_result(AS_OF, "summary")
    assert len(열린것) == 1


def _뿌리_이름(노드: ast.expr) -> str | None:
    """`psycopg.sql.SQL` 같은 속성 사슬의 맨 앞 이름을 돌려준다."""
    while isinstance(노드, ast.Attribute):
        노드 = 노드.value
    return 노드.id if isinstance(노드, ast.Name) else None


def _커넥션을_여는_자리(소스: str) -> list[str]:
    """소스에서 «커넥션을 여는 자리» 만 골라낸다 — 글자가 아니라 구문 노드로 본다.

    🔴 **글자 훑기(`"get_connection" not in 코드`)는 독스트링에 걸려 빨개졌다.**
       `git log -S` 로 보면 단언은 `2b36b33`(perf(logistics): improve console query
       performance · #719)이 넣었고, 같은 PR 뒤 커밋 `7d4ccfa`(docs(logistics): update
       console usage docs and comments · #719)가 `console_service` **독스트링**에
       `get_connection` 이라는 낱말을 써서 그 단언에 걸렸다. 커넥션을 여는 코드는 없다.

    그래서 보는 것을 **부름과 들임**으로 좁혔다. 지키려는 뜻은 그대로다 —
    «커넥션의 주인은 `build_result` 다». 독스트링이 그 낱말을 설명에 쓰는 것과
    코드가 그것을 부르는 것은 다른 일이고, 이 함수는 뒤엣것만 잡는다.

      · `ast.Call`      — `get_connection()` · `x.get_connection()` · `psycopg.connect()`
      · `ast.Import`    — `import psycopg` (다음 줄에서 `psycopg.connect()` 할 수 있다)
      · `ast.ImportFrom`— `from ... import get_connection` · `from psycopg import connect`

    독스트링 · 주석 · 문자열 리터럴 안의 낱말은 세지 않는다.
    `from psycopg import sql` 은 SQL 조립이라 걸리지 않는다.
    """
    걸린것: list[str] = []
    for 노드 in ast.walk(ast.parse(소스)):
        if isinstance(노드, ast.Call):
            부름 = 노드.func
            if isinstance(부름, ast.Name) and 부름.id == "get_connection":
                걸린것.append(f"부름 {부름.id}()")
            elif isinstance(부름, ast.Attribute) and (
                부름.attr == "get_connection"
                or (부름.attr == "connect" and _뿌리_이름(부름.value) == "psycopg")
            ):
                걸린것.append(f"부름 {ast.unparse(부름)}()")
        elif isinstance(노드, ast.Import):
            for 이름 in 노드.names:
                조각 = 이름.name.split(".")
                if 조각[0] == "psycopg" or 조각[-1] == "get_connection":
                    걸린것.append(f"들임 import {이름.name}")
        elif isinstance(노드, ast.ImportFrom):
            뿌리 = (노드.module or "").split(".")[0]
            for 이름 in 노드.names:
                if 이름.name == "get_connection" or (
                    뿌리 == "psycopg" and 이름.name == "connect"
                ):
                    걸린것.append(f"들임 from {노드.module} import {이름.name}")
    return 걸린것


@pytest.mark.parametrize(
    ("이름", "소스", "잡아야_하나"),
    [
        ("독스트링에만", '"""get_connection 은 build_result 가 부른다."""\n', False),
        ("주석에만", "# get_connection · psycopg.connect 는 여기서 안 쓴다\nx = 1\n", False),
        ("문자열에만", '메시지 = "get_connection 이 없다"\n', False),
        ("SQL 조립만", "from psycopg import sql\n\nq = sql.SQL('select 1')\n", False),
        ("이름을_부른다", "conn = get_connection()\n", True),
        ("속성을_부른다", "conn = psycopg.connect()\n", True),
        ("빌려서_부른다", "conn = db.get_connection()\n", True),
        ("들이기만_한다", "from app.logistics.db import get_connection\n", True),
        ("모듈을_들인다", "import psycopg\n", True),
    ],
)
def test_커넥션_검사는_낱말이_아니라_부름을_본다(
    이름: str, 소스: str, 잡아야_하나: bool
) -> None:
    """★ 좁힌 단언이 진짜 위반은 그대로 잡는지 잰다 — `console_service` 는 안 건드린다."""
    걸린것 = _커넥션을_여는_자리(소스)
    assert bool(걸린것) is 잡아야_하나, f"{이름}: {걸린것}"


def test_console_service_는_커넥션을_열지_않는다() -> None:
    """★ 커넥션의 주인은 `build_result` 다 — 조회 계층이 자기 것을 열면 다시 늘어난다."""
    import inspect

    from app.logistics import console_service

    assert _커넥션을_여는_자리(inspect.getsource(console_service)) == []
