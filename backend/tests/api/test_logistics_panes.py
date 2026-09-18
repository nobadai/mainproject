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


def 용량(
    *,
    used: Decimal = Decimal(450),
    guaranteed: Decimal | None = Decimal(1000),
    burst: Decimal | None = Decimal(1200),
) -> Any:
    """기본값은 «여유가 있는 창고» 다 — 넘긴 값만 바꿔 쓴다."""
    return SimpleNamespace(
        used_capacity_kg=used,
        guaranteed_capacity_kg=guaranteed,
        burst_capacity_kg=burst,
    )


def 재고(items: list[Any], capacity: Any = None, lots: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        items=items,
        lots=lots if lots is not None else [로트()],
        available_qty_unresolved_reason=None,
        capacity=capacity if capacity is not None else 용량(),
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
        capacity: Any = None,
        lots: list[Any] | None = None,
    ) -> Any:
        inv = 재고(items if items is not None else [품목(ITEM_ON_SCREEN)], capacity, lots)
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
    확인할문제 = 통계(pane, "확인할 문제")
    assert 확인할문제.value == "1"
    assert "새로 열림 1건" in (확인할문제.detail or "")
    assert "이어짐 0건" in (확인할문제.detail or "")
    행 = 카드(pane, "exceptions").table.rows
    assert [r["state"] for r in 행] == ["신규"]


def test_그전에_열려_살아_있으면_지속_중이다(화면):
    result = 화면(live=(문제("E1", opened=date(2026, 3, 1), detected=AS_OF),), resolved=())
    pane = 한눈에(result.tab.panes)
    확인할문제 = 통계(pane, "확인할 문제")
    assert 확인할문제.value == "1"
    assert "새로 열림 0건" in (확인할문제.detail or "")
    assert "이어짐 1건" in (확인할문제.detail or "")
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
    확인할문제 = 통계(pane, "확인할 문제")
    #  🔴 해소는 «확인할 문제» 에 안 섞인다 — 이어진 1건만 센다.
    assert 확인할문제.value == "1"
    assert "새로 열림 0건" in (확인할문제.detail or "")
    assert "이어짐 1건" in (확인할문제.detail or "")
    해소 = next(s for s in 카드(pane, "progress").stats if s.label == "기준일에 해소")
    assert 해소.value == "1"
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
    assert "창고 여유 1건" in (통계(pane, "확인할 문제").detail or "")
    종류 = {r["kind"] for r in 카드(pane, "exceptions").table.rows}
    #  ★ 사용자 표시명이다 — raw 코드(`FRESHNESS_PRESSURE`)를 화면에 싣지 않는다.
    assert 종류 == {"신선도 확인 필요", "창고 여유 확인 필요"}


#  ── 시간축이 새지 않는가 ─────────────────────────────────────────────────


def test_기준일_뒤에_갱신된_우선도를_그날_값으로_적지_않는다(화면):
    """`touch_exception` 이 `severity` 를 덮어쓴다 — 미래 값이 새면 안 된다."""
    result = 화면(
        live=(
            문제("E1", opened=date(2026, 3, 1), detected=date(2026, 3, 20), severity="CRITICAL"),
        ),
        resolved=(),
    )
    카드_ = 카드(한눈에(result.tab.panes), "exceptions")
    행 = 카드_.table.rows[0]
    assert "매우 높음" not in str(행["sev"])
    #  🔴 칸에는 **결과만** 적고 구현 사정을 쓰지 않는다 (#812) — 종전 문구는
    #     「기준일 당시 우선도 확인 불가」였다. 이유는 카드 footer 가 한 번 말한다.
    assert 행["sev"] == "우선도 정보 없음"
    assert "그날 값으로 쓰지 않아" in (카드_.footer or "")
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
        s for s in 카드(한눈에(result.tab.panes), "capacity").stats if s.label == "창고 사용량"
    )
    assert 사용량.raw == 450.0


def test_보장_용량을_넘어도_여유가_음수로_안_내려간다(화면):
    """🔴 **「추가 수용 가능량 -463 kg」 은 뜻이 성립하지 않는다.**

    정본이 `max(0, 보장 − 점유)` 다 (`04_창고_Capacity관리.md` C-2). 실측 250일 중
    14일이 보장 용량을 넘었고(최대 8,463kg · 105.8%) 화면이 뺄셈 결과를 그대로 적었다.

    ★ 깎되 **버리지 않는다** — 초과분은 설명 문구가 자기 숫자로 말한다. 0 으로 접어
      «꽉 차지 않았다» 로 읽히면 그것대로 사실과 다르다.
    """
    #  보장 1,000 인 창고에 1,200 이 들어찼다 — 200 초과다.
    result = 화면(live=(), resolved=(), capacity=용량(used=Decimal(1200)))
    pane = 한눈에(result.tab.panes)
    for 칸 in (통계(pane, "창고 여유"),
               next(s for s in 카드(pane, "capacity").stats if s.label == "추가 수용 가능량")):
        assert 칸.value == "0" and 칸.raw == 0.0
        assert 칸.detail == "보장 용량 200 kg 초과 (120.0% 사용)"
        assert 칸.tone == "warn"


def test_우선출고와_폐기검토를_겸한_Lot_을_두_번_세지_않는다(화면):
    """🔴 **두 축은 독립이다** (`turnover.sell_priority_of` ↔ `is_disposal_candidate`).

    더하면 둘 다 참인 Lot 이 두 번 세어진다 — 실측 250일 중 21일이 부풀었고 최악은
    2026-03-08 의 「12 Lot」(실제 8 Lot)이었다.
    """
    겸한Lot = 로트("LOT-BOTH")
    겸한Lot.sell_priority = True
    겸한Lot.disposal_candidate = True
    우선만 = 로트("LOT-SELL")
    우선만.sell_priority = True
    우선만.disposal_candidate = False
    pane = 한눈에(화면(live=(), resolved=(), lots=[겸한Lot, 우선만]).tab.panes)
    #  Lot 은 둘뿐이다 — 합으로 세면 3 이 된다.
    assert 통계(카드(pane, "progress"), "신선도 관리 대상").value == "2"
    assert 카드(pane, "items").table.rows[0]["risk"] == 2


def test_활성_예약_수량은_요구량이_아니라_잡고_있는_양이다(화면):
    """🔴 `ConsoleInventoryItem.reserved_qty_kg` 는 allocated + unallocated 다.

    「요구량」은 아래 표가 그리는 `required_qty_kg` 라는 **다른 칸**이다 — 한 화면에서
    같은 말이 두 값을 가리키면 안 된다.
    """
    pane = next(p for p in 화면(live=(), resolved=()).tab.panes if p.key == "stock")
    assert "지금 잡고 있는 양" in (통계(pane, "활성 예약 수량").detail or "")
    assert "요구" not in (통계(pane, "활성 예약 수량").detail or "")


def test_보장_용량을_못_읽으면_초과분을_지어내지_않는다(화면):
    """★ `None` 은 «—» 다 — 0 으로 메우면 «여유가 없다» 가 되어 뜻이 바뀐다."""
    result = 화면(live=(), resolved=(), capacity=용량(guaranteed=None, burst=None))
    여유 = 통계(한눈에(result.tab.panes), "창고 여유")
    assert 여유.value == "—" and 여유.raw is None
    assert 여유.detail == "보장 용량을 못 읽어 계산하지 않습니다"


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


#: 그날 살아 있던 Lot 하나 (`ConsoleInventoryLot` 계약). 표시명은 이 칸들로 만든다.
def _lot(lot_id: str = "LOT-1", *, item: str = ITEM_ON_SCREEN, 받은날=date(2026, 8, 20)) -> Any:
    return SimpleNamespace(
        lot_id=lot_id, item_id=item, item_name=item, grade="상",
        remaining_qty_kg=Decimal(10), received_at=받은날,
        remaining_freshness_days=3, disposal_candidate=False,
    )


_INV = SimpleNamespace(lots=[_lot()])


def _가짜_후보(물은것: list[tuple[str, ...]]):
    """`get_fefo_candidates_by_item` 대역 — 어느 품목을 물었는지 적고 품목마다 후보 하나.

    🔴 **대역도 새 계약을 받는다** (#812). 후보는 이제 그날 Lot 목록과 그날 예약으로
       만들어지므로 `conn` · `sim_run_id` · `as_of` 를 받지 않는다 — 날짜를 안 받는 것이
       «두 시간축이 섞일 자리를 없앤다» 는 그 계약이다.
    """

    def 대역(*, lots: Any, reservations: Any, item_ids: Any):
        품목 = sorted(set(item_ids))
        물은것.append(tuple(품목))
        #  ★ `ConsoleFefoCandidate` 계약 그대로.
        후보 = SimpleNamespace(
            lot_id="LOT-1",
            grade="상",
            available_qty_kg=Decimal(10),
            remaining_freshness_days=3,
            received_at=date(2026, 8, 20),
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
    pane = logistics_query._outbound_pane(ob, _INV)
    #  🔴 품목마다 한 번 묻고(164번 묻던 자리), 표에는 미할당이 남은 예약만 오른다.
    assert 물은것 == [(ITEM_ON_SCREEN,)]
    #  🔴 raw Reservation ID 를 싣지 않는다 — **카드 제목이 품목**이다.
    assert [c.title for c in pane.cards] == [f"{ITEM_ON_SCREEN} 출고 후보"]
    fefo = 카드(pane, f"fefo-{ITEM_ON_SCREEN}").table
    assert fefo is not None and len(fefo.rows) == 1
    예약 = next(s for s in pane.stats if s.label == "예약")
    assert 예약.value == "2" and "2건은 뺐습니다" in (예약.detail or "")  # 숨기지 않고 적는다


def test_FEFO_후보는_품목당_한_벌이고_예약마다_복제되지_않는다(monkeypatch):
    """🔴 **같은 Lot 을 예약 수만큼 그리면 읽는 사람이 더한다** (#812).

    종전에는 예약마다 그 품목의 후보 전부를 다시 적었다 — 무 예약 다섯이면 같은 후보
    일곱 줄이 다섯 번 나와 56줄이 됐고, 실제 사실은 19개뿐이었다. 가용량은 예약마다
    따로 있는 값이 아니라 **그 Lot 하나에 남은 몫**이라 그렇게 읽히면 안 된다.
    """
    물은것: list[tuple[str, ...]] = []
    monkeypatch.setattr(logistics_query, "get_fefo_candidates_by_item", _가짜_후보(물은것))
    ob = SimpleNamespace(reservations=[
        _resv("R-1", status="RESERVED", allocated="0", unallocated="100", shipped=False),
        _resv("R-2", status="RESERVED", allocated="0", unallocated="50", shipped=False),
    ])
    pane = logistics_query._outbound_pane(ob, _INV)
    assert 물은것 == [(ITEM_ON_SCREEN,)]
    #  ★ 카드가 품목마다 하나다 — 한 표에 몰면 품목 칸이 줄마다 되풀이된다.
    카드하나 = 카드(pane, f"fefo-{ITEM_ON_SCREEN}")
    fefo = 카드하나.table
    assert fefo is not None
    #  예약 둘인데 후보는 **한 줄**이다 (대역이 품목당 후보 하나를 준다).
    assert [row["rank"] for row in fefo.rows] == [1]
    #  예약 축 칸도 품목 칸도 표에 없다 — 있으면 그 자리에서 복제가 생긴다.
    칸 = [c.key for c in fefo.columns]
    assert "resv" not in 칸 and "need" not in 칸 and "item" not in 칸
    #  배정해야 할 양은 **카드 머리에** 한 번 적는다 (100 + 50).
    assert "Lot 미배정 150 kg" in (카드하나.subtitle or "")
    #  🔴 «자동 배정» 처럼 보이면 안 된다 — 카드가 여럿이라 pane 통계가 한 번 말한다.
    assert "자동으로 배정되지 않습니다" in (통계(pane, "출고 후보 Lot").detail or "")


def test_그릴_예약이_없으면_FEFO_를_묻지도_않는다(monkeypatch):
    물은것: list[tuple[str, ...]] = []
    monkeypatch.setattr(logistics_query, "get_fefo_candidates_by_item", _가짜_후보(물은것))
    ob = SimpleNamespace(reservations=[
        _resv("R-HOLD", status="ALLOCATED", allocated="100", unallocated="0", shipped=False),
    ])
    pane = logistics_query._outbound_pane(ob, _INV)
    assert 물은것 == []
    assert 통계(pane, "출고 후보 Lot").value == "0"
    assert 카드(pane, "fefo").lead is None


#  ── #812: 화면에서 뺀 것들 ───────────────────────────────────────────────


def test_입고_탭에_운송_칸이_없다(화면):
    """🔴 **보여 줄 수 없는 칸을 «0 건» 이라고 적지 않는다** (#812).

    걷기가 일정 행을 도착일에 만들어(`created_as_of == expected_arrival_date`)
    Receipt 없는 일정이 하루도 없다 — 「입고 예정」 목록은 어느 기준일에도 비어 있다.
    빈 표에 «이 기준일에 들어올 예정인 입고가 없습니다» 라고 적으면 **0 건을
    확인했다는 주장**이 되는데, 사실은 «이 실행은 도착 전 상태를 안 남긴다» 이다.
    """
    result = 화면(live=(), resolved=())
    입고 = next(p for p in result.tab.panes if p.key == "inbound")
    #  ★ 남는 것은 실제로 도착한 물량의 표 하나다. 「입고 예정」 카드도, 그 단계를
    #    그리던 「입고 처리 흐름」 소개 카드도 없앴다.
    assert [c.key for c in 입고.cards] == ["by_item", "receipt"]
    #  요약 탭에도 남지 않는다.
    진행 = 카드(한눈에(result.tab.panes), "progress")
    assert all(s.label != "입고 예정" for s in 진행.stats)


def test_입고_요약은_항상_0_인_칸을_세우지_않는다(화면):
    """🔴 **자리만 차지하는 0 을 안 그린다** (#812).

    종전 넷(도착 예정 · 도착 지연 · 처리 보류 · 확인 필요)은 도착 전 상태 목록에서
    세는데 그 목록이 늘 비어 있어 어느 날을 열어도 `0건` 넷이었다. 대신 그날 실제로
    도착한 건을 아래 표와 **같은 판정**으로 센다.
    """
    result = 화면(live=(), resolved=())
    입고 = next(p for p in result.tab.panes if p.key == "inbound")
    이름 = [s.label for s in 입고.stats]
    assert "도착 예정" not in 이름 and "도착 지연" not in 이름
    #  경보 둘은 값이 0 이면 안 뜬다 (대역 요약이 전부 0 이다).
    assert "처리 보류" not in 이름 and "확인 필요" not in 이름
    assert "기준일 도착" in 이름 and "처리 중" in 이름


def test_입고는_기준일_하루만_세고_날짜를_적는다(화면):
    """🔴 **누계도 월 단위도 «도착 건수» 가 아니다.**

    `receipt_state_at` 은 아래쪽 경계가 없어 실행 첫날부터 다 실어 준다 — 실측
    291건 · 151,921kg 가 2026-01-06~09-01 8개월 누계였다(#812). 그 뒤 한동안 그 달
    1일부터 셌으나, 달력 경계가 업무 경계가 아니라 창고에 아무 일도 없는 8/31 → 9/1
    사이에 38건이 2건으로 떨어졌다. **지금은 기준일 하루만 세고 그 날짜를 적는다.**
    """
    어제 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    어제.arrived_at = date(2026, 3, 9)
    오늘 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    오늘.arrived_at = AS_OF
    inb = SimpleNamespace(
        in_transit=[], in_transit_status="CONFIRMED_ZERO",
        receipts=[오늘, 어제], arrival_summary=_도착요약(),
    )
    pane = logistics_query._inbound_pane(inb, AS_OF)  # AS_OF = 2026-03-10
    도착 = 통계(pane, "기준일 도착")
    assert 도착.value == "1"  # 어제 건은 안 센다 — 같은 달이어도
    assert 도착.detail == "03-10 하루"
    #  둘 다 끝난 건이라 표에도 그날 것만 남는다.
    assert len(카드(pane, "receipt").table.rows) == 1


def test_안_끝난_입고는_그날_것이_아니어도_표와_처리중에_남는다(화면):
    """🔴 **막힌 건은 막힌 그날 날짜에 속한다** — 하루로 자르면 다음 날 사라진다.

    찾아내라고 세운 칸이 못 찾게 되므로, 표와 「처리 중」만은 날짜로 안 자른다.
    """
    막힌것 = _입고행(state="ARRIVED", stock_applied=False, settled=False, verdict=None)
    막힌것.arrived_at = date(2026, 3, 2)  # 여드레 전에 막혔다
    inb = SimpleNamespace(
        in_transit=[], in_transit_status="CONFIRMED_ZERO",
        receipts=[막힌것], arrival_summary=_도착요약(),
    )
    pane = logistics_query._inbound_pane(inb, AS_OF)
    assert 통계(pane, "기준일 도착").value == "0"  # 오늘 도착은 없다
    assert 통계(pane, "처리 중").value == "1"      # 그래도 할 일은 남아 있다
    행 = 카드(pane, "receipt").table.rows
    assert [r["arrive"] for r in 행] == ["03-02"]


def test_마지막_입고는_맨_뒤에_서고_당일이면_오늘이라고_적는다(화면):
    """★ 앞의 칸들은 «오늘 들어온 것이 어디까지 갔나» 한 줄기이고, 이 칸만 «마지막이
    언제였나» 라는 다른 질문에 답한다 — 가운데 두면 흐름이 끊긴다.

    🔴 당일이면 «0일 전» 이 아니라 **«오늘»** 이다.
    """
    오늘도착 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    오늘도착.arrived_at = AS_OF
    inb = SimpleNamespace(
        in_transit=[], in_transit_status="CONFIRMED_ZERO",
        receipts=[오늘도착], arrival_summary=_도착요약(),
    )
    pane = logistics_query._inbound_pane(inb, AS_OF)
    assert [s.label for s in pane.stats][-1] == "마지막 입고"
    assert 통계(pane, "마지막 입고").detail == "오늘"


def test_그날_입고가_없어도_마지막_입고는_말한다(화면):
    """★ 「0건」만 남으면 화면이 «왜 비었는지» 를 안 말한다 — 기간 밖에서 찾아 적는다."""
    지난달 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    지난달.arrived_at = date(2026, 2, 25)
    inb = SimpleNamespace(
        in_transit=[], in_transit_status="CONFIRMED_ZERO",
        receipts=[지난달], arrival_summary=_도착요약(),
    )
    pane = logistics_query._inbound_pane(inb, AS_OF)
    assert 통계(pane, "기준일 도착").value == "0"
    마지막 = 통계(pane, "마지막 입고")
    assert 마지막.value == "02-25" and 마지막.detail == "13일 전"
    assert 마지막.tone == "warn"  # 7일 넘게 입고가 없다


def test_재고_탭에_회전_칸이_없다(화면):
    """🔴 **회전은 화면에 안 적는다** (#812) — 계산·정책·Agent 판단은 그대로다.

    신선도 잔여 옆에 비슷한 숫자를 하나 더 세우면 사용자는 둘 중 무엇을 봐야 하는지
    알 수 없다. 회전에서 나온 **업무 신호**(우선 출고 · 폐기 검토)는 남는다.
    """
    result = 화면(live=(), resolved=())
    재고탭 = next(p for p in result.tab.panes if p.key == "stock")
    표 = 카드(재고탭, "lots").table
    칸 = [c.label for c in 표.columns]
    assert "회전 잔여" not in 칸 and "회전 상태" not in 칸
    assert "신선도 잔여" in 칸 and "필요한 조치" in 칸
    #  ★ 회전에서 나온 업무 신호는 살아 있다 — 지운 것은 회전 «어휘» 뿐이다.
    assert 표.rows[0]["action"] == "우선 출고 대상"
    #  설명 카드를 통째로 없앴다 — 표가 스스로 읽혀야 한다.
    assert all(c.key != "principle" for c in 재고탭.cards)


def test_FEFO_에는_그날_예약을_전부_넘긴다(monkeypatch):
    """🔴 **화면이 그리는 예약만 넘기면 남의 할당이 안 빠져 가용량이 부푼다** (#812).

    후보의 가용량은 «이 Lot 에서 아직 아무 할당에도 안 묶인 몫» 이라, 끝난 예약이
    잡고 있는 할당도 빼야 한다 — 그 예약은 표에 안 그려도 재고는 잡고 있다.
    """
    받은예약: list[Any] = []

    def 대역(*, lots: Any, reservations: Any, item_ids: Any):
        받은예약.extend(reservations)
        return {}

    monkeypatch.setattr(logistics_query, "get_fefo_candidates_by_item", 대역)
    ob = SimpleNamespace(reservations=[
        _resv("R-WAIT", status="RESERVED", allocated="0", unallocated="100", shipped=False),
        _resv("R-HOLD", status="ALLOCATED", allocated="100", unallocated="0", shipped=False),
    ])
    logistics_query._outbound_pane(ob, _INV)
    #  표에 오르는 것은 R-WAIT 하나지만, 넘기는 것은 둘 다여야 한다.
    assert [r.reservation_id for r in 받은예약] == ["R-WAIT", "R-HOLD"]


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


#  ── #805 입고 처리 완료 != 재고 반영 완료 ────────────────────────────────


def _입고행(
    *,
    state: str,
    stock_applied: bool,
    settled: bool | None,
    verdict: str | None = "PASS",
) -> Any:
    """`ConsoleInboundReceipt` 계약 그대로의 한 줄."""
    return SimpleNamespace(
        inbound_id="INB-1", receipt_id="RCPT-1", item_id=ITEM_ON_SCREEN,
        item_name=ITEM_ON_SCREEN, arrived_at=AS_OF,
        ordered_qty_kg=Decimal(100), accepted_qty_kg=Decimal(100),
        hold_qty_kg=Decimal(0), rejected_qty_kg=Decimal(0),
        receipt_status=state, fact_source="inbound_receipts",
        inspection_id="INS-1", inspection_verdict=verdict, inspected_qty_kg=Decimal(100),
        lot_id="LOT-1" if stock_applied else None,
        in_move_id="MOVE-1" if stock_applied else None,
        stock_applied=stock_applied, settled_without_stock=settled,
    )


def _입고_카드(rows: list[Any]) -> Any:
    inb = SimpleNamespace(
        in_transit=[], in_transit_status="CONFIRMED_ZERO",
        receipts=rows, arrival_summary=_도착요약(),
    )
    return 카드(logistics_query._inbound_pane(inb, AS_OF), "receipt").table


#  ── #812: 전 줄이 같은 값인 칸은 세우지 않는다 ───────────────────────────


def test_보류도_거절도_없으면_그_칸을_세우지_않는다():
    """🔴 전 줄 `0 kg` 인 칸은 아무것도 안 가른다 — 실측 아홉 칸 중 다섯이 상수였다."""
    표 = _입고_카드([_입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)])
    칸 = [c.label for c in 표.columns]
    assert "보류" not in 칸 and "거절" not in 칸
    #  주문 == 수용 이면 같은 숫자를 두 번 적지 않는다.
    assert "주문" not in 칸
    #  전 줄 「합격」·「처리 완료」면 그 둘도 안 가른다 — 재고 처리만 남는다.
    assert "검수 결과" not in 칸 and "처리 상태" not in 칸
    assert 칸 == ["도착일", "품목", "수용", "재고 처리"]


def test_보류가_한_줄이라도_있으면_칸이_선다():
    """🔴 **숨기는 것이 아니다.** 값이 생기면 그날 칸이 그대로 돌아온다."""
    보류행 = _입고행(state="INSPECTED", stock_applied=False, settled=False, verdict="HOLD")
    보류행.hold_qty_kg = Decimal(30)
    보류행.accepted_qty_kg = Decimal(70)
    표 = _입고_카드([보류행])
    칸 = [c.label for c in 표.columns]
    assert "보류" in 칸 and "검수 결과" in 칸 and "처리 상태" in 칸
    #  주문(100) != 수용(70) 이라 주문 칸도 선다.
    assert "주문" in 칸


def test_품목별_입고는_수용량을_더한다():
    """★ 새 판정이 아니라 **합계**다 — 아래 표의 수용량을 품목 축으로 묶기만 한다."""
    배추 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    양파 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    양파.item_name = "양파"
    양파.accepted_qty_kg = Decimal(30)
    rows = logistics_query._receipt_by_item([배추, 양파, 배추])
    #  많이 들어온 품목이 위에 온다.
    assert [r["item"] for r in rows] == [ITEM_ON_SCREEN, "양파"]
    assert [r["count"] for r in rows] == [2, 1]
    assert [r["acc"] for r in rows] == ["200 kg", "30 kg"]


def test_품목별_입고는_못_읽은_건이_섞이면_합계를_안_낸다():
    """🔴 아는 것만 더해서 아는 척하지 않는다 — `None` 은 0 이 아니다."""
    아는것 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    모름 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    모름.accepted_qty_kg = None
    rows = logistics_query._receipt_by_item([아는것, 모름])
    assert rows[0]["acc"] is None


def test_못_읽은_수량은_0_으로_접지_않는다():
    """🔴 `None` 은 0 이 아니다 — 모르는 값이 있으면 그 칸을 세워 보이게 둔다."""
    모름행 = _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=None)
    모름행.rejected_qty_kg = None
    표 = _입고_카드([모름행])
    assert "거절" in [c.label for c in 표.columns]


def test_재고가_선_입고는_처리도_재고도_완료로_적는다():
    표 = _입고_카드([_입고행(state="PUTAWAY_DONE", stock_applied=True, settled=False)])
    assert (표.rows[0]["state"], 표.rows[0]["applied"]) == ("처리 완료", "재고 반영 완료")


def test_수용_0_으로_끝난_입고는_아직이_아니라_반영할_재고_없음이다():
    """🔴 #805 — 재고가 없다고 «아직» 으로 적으면 영영 밀린 것처럼 보인다."""
    표 = _입고_카드([_입고행(state="INSPECTED", stock_applied=False, settled=True)])
    상태, 재고 = 표.rows[0]["state"], 표.rows[0]["applied"]
    assert (상태, 재고) == ("처리 완료", "반영할 재고 없음")
    #  ★ 실패라고 적지 않는다 — 만들 재고가 없던 것이지 처리가 실패한 것이 아니다.
    assert "실패" not in str(재고) and "아직" not in str(재고)


def test_검수_전_입고는_창고_도착_검수_대기다():
    표 = _입고_카드([_입고행(state="ARRIVED", stock_applied=False, settled=False, verdict=None)])
    assert (표.rows[0]["state"], 표.rows[0]["applied"]) == ("창고 도착", "검수 대기")


def test_검수는_끝났고_재고_전이면_반영_대기다():
    표 = _입고_카드([_입고행(state="INSPECTED", stock_applied=False, settled=False)])
    assert (표.rows[0]["state"], 표.rows[0]["applied"]) == ("검수 완료", "반영 대기")


def test_일정을_못_읽으면_반영_대기라고_넘겨짚지_않는다():
    """🔴 `None` 은 «아니다» 가 아니라 «모른다» 다 — 둘을 가릴 수 없으면 «—» 다."""
    표 = _입고_카드([_입고행(state="INSPECTED", stock_applied=False, settled=None)])
    assert 표.rows[0]["applied"] == "—"


def test_아직_안_끝난_입고가_완료된_입고보다_위에_온다():
    """잘라내도 «할 일» 은 안 잘린다 — #805 완료 둘을 같이 뒤로 보낸다."""
    행 = [
        _입고행(state="PUTAWAY_DONE", stock_applied=True, settled=False),
        _입고행(state="INSPECTED", stock_applied=False, settled=True),
        _입고행(state="ARRIVED", stock_applied=False, settled=False, verdict=None),
    ]
    assert [r["state"] for r in _입고_카드(행).rows] == ["창고 도착", "처리 완료", "처리 완료"]
