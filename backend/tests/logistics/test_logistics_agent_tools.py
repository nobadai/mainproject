"""공용 Read-only Tool 8개 — **DB 없이 재는 것들** (#628 Commit 3).

```text
읽기 전용   쓰기 함수·쓰기 SQL 이 **소스에 아예 없는가** (AST · 문자열 둘 다)
축          sim_run_id · as_of 가 **필수 인자인가** (이름만 보고 추론하지 않는다)
창          days 가 cap_by_date 창을 넘지 않는가
영향        카탈로그 밖 행동에 숫자를 붙이지 않는가 · 모르는 값을 0 으로 적지 않는가
관측일      기존 규칙 재사용 · as_of 로 메우지 않는가
```

🔴 **실제 과거 재현은 여기서 안 잰다.** look-ahead·실행 격리·숫자 정합은 실 DB 가
   있어야 의미가 있어 `test_logistics_agent_tools_db.py` 가 잰다.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.logistics.agent import tools as agent_tools
from app.logistics.agent.schemas import ExceptionEvidence, ExceptionRow
from app.logistics.agent.tools import (
    ACTION_UNSUPPORTED,
    IMPACT_INPUT_MISSING,
    MUTABLE_EXCEPTION_DETAILS,
    SUPPORTED_ACTIONS,
    InboundScheduleFact,
    LotFact,
    _allocated_by_lot,
    _as_of_snapshot,
    _exception_fact,
    _quantity,
    _state_observed_as_of,
    estimate_action_impact,
    get_inbound_schedule,
)
from app.logistics.historical_repository import HistoricalAllocation, HistoricalReservation
from app.logistics.inbound_schedules import InboundScheduleView
from app.logistics.tools import CAP_BY_DATE_WINDOW_DAYS

AS_OF = date(2026, 1, 20)
SIM = "SIM-TOOLS-TEST"

#: Tool 8개. 🔴 **숫자가 계약이다** — 하나가 사라지거나 늘면 §9 와 갈린다.
TOOL_NAMES = (
    "get_open_exceptions",
    "get_lot",
    "get_item_lots",
    "get_sales_commitments",
    "get_policy",
    "get_capacity_context",
    "get_inbound_schedule",
    "estimate_action_impact",
)

SOURCE = Path(agent_tools.__file__).read_text(encoding="utf-8")


def _tool(name: str):
    return getattr(agent_tools, name)


# ===========================================================================
# A. 계층 계약 — 8개 · 읽기 전용 · 축 필수
# ===========================================================================


def test_exactly_eight_tools_are_exported():
    """§9 의 목록 그대로다. 🔴 Registry 도 동적 탐색도 만들지 않는다."""
    exported = {name for name in agent_tools.__all__ if name in TOOL_NAMES}

    assert exported == set(TOOL_NAMES)
    assert all(callable(_tool(name)) for name in TOOL_NAMES)


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_requires_run_and_as_of(name):
    """🔴 **이름만 보고 «현재 실행» · «오늘» 을 추론하지 않는다.**

    기본값이 붙는 순간 부르는 쪽이 축을 안 넘겨도 답이 나오고, 그 답은 **아무 실행의
    아무 날**이 된다.
    """
    parameters = inspect.signature(_tool(name)).parameters

    for axis in ("sim_run_id", "as_of"):
        assert axis in parameters, f"{name} 에 {axis} 이 없다"
        parameter = parameters[axis]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}.{axis} 은 키워드 전용"
        assert parameter.default is inspect.Parameter.empty, f"{name}.{axis} 에 기본값이 있다"


#: 🔴 이 층에서 한 번이라도 부르면 read-only 가 깨진다.
FORBIDDEN_CALLS = {
    "commit",
    "rollback",
    "open_exception",
    "touch_exception",
    "resolve_exception",
    "record_inventory_move",
    "record_schedule",
    "cancel_schedule",
    "cancel_allocation",
    "release_reservation",
    "reserve_available_stock",
    "allocate_stock",
    "ship_allocated_stock",
    "confirm_disposal",
    "executemany",
}


def test_no_write_function_is_ever_called():
    """**AST 로 본다.** 🔴 문자열 검색만으로는 주석·문서와 실제 호출을 못 가른다."""
    called: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            called.add(target.id)
        elif isinstance(target, ast.Attribute):
            called.add(target.attr)

    assert called & FORBIDDEN_CALLS == set()


def test_no_write_function_is_even_imported():
    """부르지 않아도 **가져다 두면** 다음 사람이 부른다. 문을 아예 안 연다."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)

    assert imported & FORBIDDEN_CALLS == set()


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """문서화 문자열 노드의 id. ⚠️ **설명문의 «UPDATE» 를 쓰기로 세지 않는다.**"""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def test_no_write_sql_in_source():
    """AST 가 못 보는 축 하나 — **문자열 안의 SQL** 이다. 둘 다 봐야 닫힌다."""
    tree = ast.parse(SOURCE)
    docstrings = _docstring_nodes(tree)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    offenders = [
        literal
        for literal in literals
        for keyword in ("INSERT", "UPDATE", "DELETE", "UPSERT", "TRUNCATE")
        if re.search(rf"\b{keyword}\b", literal, re.IGNORECASE)
    ]

    assert offenders == []


def test_this_layer_holds_no_sql_at_all():
    """★ **질의의 주인은 기존 Reader 다.** Tool 이 자기 SQL 을 들면 같은 사실을 두 곳이
    소유하게 되고, 한쪽만 고쳐지는 날 조사와 화면이 다른 값을 말한다."""
    assert "SELECT" not in SOURCE
    assert "sql.SQL" not in SOURCE


# ===========================================================================
# B. 관측일 — 기존 규칙을 재사용한다 (§18.2)
# ===========================================================================


def test_observed_at_constants_are_not_redefined():
    """🔴 `agent.schemas` 가 규칙의 주인이다 — Tool 이 자기 상수를 들면 두 벌이 된다."""
    assert "OBSERVED_AS_OF: date" not in SOURCE
    assert "from app.logistics.agent.schemas import" in SOURCE


def test_observed_at_has_no_convenience_fallback():
    """§18 금지 목록 그대로다 — `as_of` · 오늘 · `created_at` 으로 메우지 않는다."""
    code = re.sub(r'"""(?:.|\n)*?"""', "", SOURCE)
    code = re.sub(r"(?m)#.*$", "", code)
    pattern = re.compile(r"observed_(?:at|as_of)\s*=\s*([^,\n)]+)")
    forbidden = (
        r"(?<![\w])as_of\b",
        r"date\.today\(",
        r"datetime\.now\(",
        r"(?<![\w])created_at\b",
        r"(?<![\w])updated_at\b",
    )

    offenders = [
        assignment.strip()
        for assignment in pattern.findall(code)
        for rule in forbidden
        if re.search(rule, assignment)
    ]

    assert offenders == []


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("ACTIVE", date(2026, 1, 1)),
        ("DISPOSED", date(2026, 1, 8)),
        # 🔴 **여기만 `observe` 와 다르다.** 저쪽 `DEPLETED` 는 캐시라 writer 가 없어
        #    날짜를 못 대지만, 이쪽은 **원장 잔량 0** 이라는 유도 결과다.
        ("DEPLETED", date(2026, 1, 8)),
    ],
)
def test_derived_state_observed_at_follows_its_evidence(state, expected):
    observed = _state_observed_as_of(
        state, received_at=date(2026, 1, 1), last_moved_at=date(2026, 1, 8)
    )

    assert observed == expected


def test_state_observed_at_is_none_without_the_ledger():
    observed = _state_observed_as_of("DEPLETED", received_at=date(2026, 1, 1), last_moved_at=None)

    assert observed is None


def test_lot_observed_at_is_not_the_quantity_date_alone():
    """🔴 **한 줄 전체를 잔량 날짜로 대표하지 않는다** (v0.8 보정).

    한 줄에 잔량(원장 · 날짜 있음) · 신선도/회전(정책 · 날짜 없음) · 예약(날짜 없음)이
    함께 실려 있다. 잔량 날짜만 내면 *"정책·예약까지 그날 기준으로 다 쟀다"* 가 된다.
    """
    lot = _lot()

    assert lot.remaining_qty_observed_as_of == AS_OF - timedelta(days=2)
    assert lot.freshness_observed_as_of is None, "정책 축은 유효일이 없다"
    assert lot.uncommitted_observed_as_of is None, "예약 축은 빠진 날을 못 댄다"
    assert lot.observed_as_of is None
    assert lot.observed_as_of != lot.remaining_qty_observed_as_of


def test_lot_facts_keep_their_own_dates_even_when_the_composite_is_none():
    """★ **사실별 날짜는 그대로 남는다** — 합성이 `None` 이라고 축을 지우지 않는다."""
    lot = _lot()

    assert lot.remaining_qty_observed_as_of is not None
    assert lot.status_observed_as_of is not None
    assert lot.received_at is not None


# ===========================================================================
# B'. Exception 투영 — 그날 값과 지금 값을 가른다 (v0.8 보정)
# ===========================================================================


def _exception_row(
    *,
    severity: str = "HIGH",
    status: str = "OPEN",
    opened: date = date(2026, 1, 1),
    last_detected: date,
    observed: date | None = None,
) -> ExceptionRow:
    return ExceptionRow(
        exception_id="EX-1",
        sim_run_id=SIM,
        code="FRESHNESS_PRESSURE",
        subject_type="LOT",
        subject_id="LOT-1",
        severity=severity,
        status=status,
        opened_as_of=opened,
        last_detected_as_of=last_detected,
        observed_as_of=observed,
        evidence=(
            ExceptionEvidence(
                fact="remaining_qty_kg",
                value=Decimal(500),
                unit="kg",
                source="inventory_lots",
                source_id="LOT-1",
            ),
        ),
        detector_version="v1",
        note="지금 값",
    )


def test_detail_touched_after_as_of_is_never_shown():
    """```text
    D1 OPEN · D5 severity=MEDIUM · D8 severity=HIGH + 새 근거
    as_of=D5 조회에 HIGH 나 D8 근거가 실리면 look-ahead 다
    ```

    🔴 표가 과거 severity·근거를 안 들고 있다 — **지어내지 않고 비운다.**
    """
    row = _exception_row(severity="HIGH", last_detected=date(2026, 1, 8))

    fact = _exception_fact(row, as_of=date(2026, 1, 5))

    assert fact.detail_known is False
    assert fact.severity is None
    assert fact.evidence is None
    assert fact.last_detected_as_of is None
    assert fact.note is None
    assert fact.evidence_observed_as_of is None
    assert set(MUTABLE_EXCEPTION_DETAILS) <= set(fact.unresolved_details)


def test_lifecycle_facts_survive_even_when_detail_is_unknown():
    """★ *"그날 살아 있었다"* 와 *"그날 severity 가 무엇이었다"* 는 **다른 문제다.**"""
    row = _exception_row(last_detected=date(2026, 1, 8))

    fact = _exception_fact(row, as_of=date(2026, 1, 5))

    assert fact.exception_id == "EX-1" and fact.code == "FRESHNESS_PRESSURE"
    assert fact.opened_as_of == date(2026, 1, 1)
    assert fact.open_days == 5, "D1 에 열렸으면 D5 는 닷새째다"
    assert fact.status == "OPEN", "상태는 앞으로만 가므로 지금 OPEN 이면 그날에도 OPEN"
    assert fact.detector_version == "v1"


def test_detail_is_shown_when_nothing_touched_it_after_as_of():
    """`last_detected_as_of <= as_of` 는 *"그 뒤로 손댄 적이 없다"* 는 **증명**이다."""
    row = _exception_row(
        severity="MEDIUM", last_detected=date(2026, 1, 5), observed=date(2026, 1, 4)
    )

    fact = _exception_fact(row, as_of=date(2026, 1, 5))

    assert fact.detail_known is True
    assert fact.severity == "MEDIUM"
    assert fact.evidence is not None and len(fact.evidence) == 1
    assert fact.last_detected_as_of == date(2026, 1, 5)
    assert fact.evidence_observed_as_of == date(2026, 1, 4)
    assert fact.unresolved_details == ()


def test_status_is_unknown_when_the_row_already_moved_on():
    """🔴 `PROPOSED` 로 넘어간 날을 적는 칸이 없다 (§26) — 그날 무엇이었는지 못 댄다."""
    row = _exception_row(status="RESOLVED", last_detected=date(2026, 1, 3))

    fact = _exception_fact(row, as_of=date(2026, 1, 5))

    assert fact.status is None
    assert "status" in fact.unresolved_details


# ===========================================================================
# C. 할당 축 — 무엇을 세고 무엇을 빼는가
# ===========================================================================


def _allocation(lot_id: str, qty: str, state: str) -> HistoricalAllocation:
    return HistoricalAllocation(
        allocation_id=f"ALC-{lot_id}-{state}",
        reservation_id="RSV-1",
        lot_id=lot_id,
        pallet_id=None,
        allocated_qty_kg=Decimal(qty),
        allocation_basis="FEFO_AUTO_SELECTED",
        decided_by="TEST",
        decided_at=None,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        shipped_at=None,
        note=None,
    )


def _reservation(*allocations: HistoricalAllocation) -> HistoricalReservation:
    return HistoricalReservation(
        reservation_id="RSV-1",
        sim_run_id=SIM,
        item_id="ITEM-BAECHU",
        item_name="배추",
        sale_id="SALE-1",
        sale_date=AS_OF,
        required_qty_kg=Decimal(500),
        reserved_qty_kg=Decimal(500),
        due_date=AS_OF,
        state="HOLDING",
        status="ALLOCATED",
        released_as_of=None,
        allocated_qty_kg=Decimal(0),
        shipped_qty_kg=Decimal(0),
        unallocated_qty_kg=Decimal(0),
        allocations=allocations,
    )


def test_only_live_allocations_are_counted():
    """🔴 `SHIPPED` 는 원장 OUT 이 잔량에서 이미 뺐다 — 또 빼면 없는 재고가 생긴다.
    `RELEASED` 는 돌려준 몫이라 잡고 있는 것이 아니다."""
    totals = _allocated_by_lot(
        [
            _reservation(
                _allocation("LOT-1", "100", "ALLOCATED"),
                _allocation("LOT-1", "200", "SHIPPED"),
                _allocation("LOT-2", "300", "RELEASED"),
            )
        ]
    )

    assert totals == {"LOT-1": Decimal(100)}


# ===========================================================================
# D. 용량 — 점유 축만 갈아 끼운다
# ===========================================================================


def _lot(lot_id: str = "LOT-1", qty: str = "500", status: str = "ACTIVE") -> LotFact:
    return LotFact(
        lot_id=lot_id,
        item_id="ITEM-BAECHU",
        item="배추",
        grade=None,
        storage_zone="COLD_HUMID_0_3",
        status=status,
        received_at=AS_OF - timedelta(days=7),
        remaining_qty_kg=Decimal(qty),
        unit_cost_krw_per_kg=Decimal(1000),
        remaining_freshness_days=3,
        effective_freshness_limit_days=10,
        turnover_status="SELL_PRIORITY",
        sell_priority=True,
        sell_priority_remaining_days=3,
        disposal_candidate=False,
        committed_kg=Decimal(0),
        uncommitted_kg=Decimal(qty),
        remaining_qty_observed_as_of=AS_OF - timedelta(days=2),
        status_observed_as_of=AS_OF - timedelta(days=7),
    )


def test_as_of_snapshot_swaps_only_the_occupancy_axis(complete_logistics_snapshot):
    """🔴 **정책·리드타임·예정 목록을 건드리지 않는다.** 그 축들은 이미 `as_of` 로
    잘려 왔고(`repository._schedule_lists`), 여기서 다시 만들면 두 주인이 된다."""
    original = complete_logistics_snapshot
    swapped = _as_of_snapshot(original, lots=[_lot(qty="700")], used_kg=Decimal(700))

    assert len(swapped.on_hand_by_lot) == 1
    assert swapped.used_capacity_kg == Decimal(700)
    assert swapped.on_hand_by_lot[0].available_qty_kg == Decimal(700)
    # 나머지 축은 원본 그대로다.
    assert swapped.guaranteed_capacity_kg == original.guaranteed_capacity_kg
    assert swapped.inbound_lead_days == original.inbound_lead_days
    assert swapped.confirmed_inbound_schedule == original.confirmed_inbound_schedule
    assert swapped.capacity_tight_ratio == original.capacity_tight_ratio


def test_empty_lots_do_not_occupy_space(complete_logistics_snapshot):
    """스냅샷의 `on_hand_by_lot` 은 **실물이 있는 Lot** 축이다 (`repository` 와 같은 눈)."""
    swapped = _as_of_snapshot(
        complete_logistics_snapshot,
        lots=[_lot(qty="0", status="DEPLETED"), _lot(lot_id="LOT-2", qty="400")],
        used_kg=Decimal(400),
    )

    assert [lot.lot_id for lot in swapped.on_hand_by_lot] == ["LOT-2"]


# ===========================================================================
# E. 입고 창 — cap_by_date 창을 넘지 않는다
# ===========================================================================


def _stub_schedules(monkeypatch, *, views: tuple, change_dates: tuple) -> None:
    """일정 조회 둘을 함께 갈아 끼운다.

    ★ **둘이 짝이다** — 목록을 내는 쪽과 «그 목록을 그렇게 만든 날» 을 내는 쪽이라,
      하나만 바꾸면 검사가 실제 계약을 안 재게 된다.
    """
    monkeypatch.setattr(agent_tools, "load_schedule_views", lambda *a, **k: views)
    monkeypatch.setattr(agent_tools, "schedule_fact_dates_at", lambda *a, **k: change_dates)


def _schedule_view(inbound_id: str, *, arrives: date, created: date) -> InboundScheduleView:
    return InboundScheduleView(
        inbound_id=inbound_id,
        sim_run_id=SIM,
        purchase_item_id="PI-1",
        purchase_id="PO-1",
        item_id="ITEM-BAECHU",
        item_name="배추",
        quantity_kg=Decimal(1000),
        expected_arrival_date=arrives,
        created_as_of=created,
        has_receipt=False,
        stock_applied=False,
    )


@pytest.mark.parametrize(
    ("requested_days", "expected_days"),
    [
        (None, CAP_BY_DATE_WINDOW_DAYS),
        (3, 3),
        # 🔴 **판정 창보다 멀리 보지 않는다** — 용량 판정에 안 들어간 입고가 조사에 섞인다.
        (60, CAP_BY_DATE_WINDOW_DAYS),
        (-5, 0),
    ],
)
def test_inbound_window_never_exceeds_the_capacity_window(
    monkeypatch, requested_days, expected_days
):
    _stub_schedules(monkeypatch, views=(), change_dates=())

    result = get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF, days=requested_days)

    assert result.days == expected_days


def test_schedules_arriving_after_the_window_are_dropped(monkeypatch):
    inside = _schedule_view("INB-IN", arrives=AS_OF + timedelta(days=2), created=AS_OF)
    outside = _schedule_view("INB-OUT", arrives=AS_OF + timedelta(days=40), created=AS_OF)
    _stub_schedules(monkeypatch, views=(inside, outside), change_dates=(AS_OF,))

    result = get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF)

    assert [one.inbound_id for one in result.schedules] == ["INB-IN"]
    assert isinstance(result.schedules[0], InboundScheduleFact)


def test_inbound_schedule_carries_a_real_business_observed_at(monkeypatch):
    """✅ **장부를 바꾼 날**이 있다 — 정책·예약 축과 다른 자리다."""
    _stub_schedules(
        monkeypatch,
        views=(
            _schedule_view("INB-1", arrives=AS_OF + timedelta(days=2), created=date(2026, 1, 10)),
            _schedule_view("INB-2", arrives=AS_OF + timedelta(days=3), created=date(2026, 1, 14)),
        ),
        change_dates=(date(2026, 1, 10), date(2026, 1, 14)),
    )

    result = get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF)

    assert result.observed_as_of == date(2026, 1, 14)
    assert result.observed_as_of <= AS_OF


def test_a_cancellation_survives_in_the_collection_observed_at(monkeypatch):
    """🔴 **취소가 관측일에서 사라지면 안 된다.**

    ```text
    D10  A 생성 · D12  B 생성 · D17  B 취소
    답 = [A]      ← 이 답은 **D17 부터** 참이다. D10 이라고 하면 거짓이다
    ```

    살아남은 `A` 의 `created_as_of` 만 모으면 D10 이 나온다 — 그것이 종전 버그다.
    """
    survivor = _schedule_view("INB-A", arrives=AS_OF + timedelta(days=2), created=date(2026, 1, 10))
    _stub_schedules(
        monkeypatch,
        views=(survivor,),
        # 생성 둘 · 취소 하나 — 취소가 가장 늦다.
        change_dates=(date(2026, 1, 10), date(2026, 1, 12), date(2026, 1, 17)),
    )

    result = get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF)

    assert [one.inbound_id for one in result.schedules] == ["INB-A"]
    assert result.observed_as_of == date(2026, 1, 17)
    assert result.observed_as_of != survivor.created_as_of


def test_collection_observed_at_is_none_without_any_change_date(monkeypatch):
    """잴 것이 없었던 날도 «안 쟀다» 다 — 빈 입력은 `None` 이다."""
    _stub_schedules(monkeypatch, views=(), change_dates=())

    assert get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF).observed_as_of is None


# ===========================================================================
# F. 영향 추정 — 모르는 것에 숫자를 붙이지 않는다
# ===========================================================================


def test_supported_actions_match_the_catalogue():
    """§10.1 그대로다. 🔴 Proposal 을 여기서 만들지 않는다 — 이름만 공유한다."""
    assert SUPPORTED_ACTIONS == (
        "SALES_PRIORITY_REQUEST",
        "PURCHASE_ADJUST_REQUEST",
        "ACCEPT_RISK",
        "DISPOSAL_REQUEST",
    )


def test_unknown_action_gets_no_numbers():
    """🔴 **모르는 행동에 그럴듯한 숫자를 붙이면 그 숫자가 제안의 근거가 된다.**"""
    result = estimate_action_impact(
        None, sim_run_id=SIM, as_of=AS_OF, action="ZONE_MOVE", parameters={"lot_id": "LOT-1"}
    )

    assert result.feasibility == "UNSUPPORTED"
    assert result.affected_kg is None and result.capacity_delta_kg is None
    assert result.estimated_loss_krw is None
    assert f"{ACTION_UNSUPPORTED}:ZONE_MOVE" in result.uncertainties


def test_missing_lot_id_is_reported_as_unresolved():
    result = estimate_action_impact(
        None, sim_run_id=SIM, as_of=AS_OF, action="DISPOSAL_REQUEST", parameters={}
    )

    assert result.feasibility == "UNRESOLVED"
    assert f"{IMPACT_INPUT_MISSING}:lot_id" in result.uncertainties


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("10.5"), Decimal("10.5")),
        (10, Decimal(10)),
        ("10.5", Decimal("10.5")),
        (0, None),
        (-3, None),
        (True, None),  # 🔴 bool 은 int 가 아니다 — 수량으로 읽지 않는다
        (None, None),
        ("십", None),
        (1.5, None),  # float 은 안 받는다 — 통화·수량이 조용히 흔들린다
    ],
)
def test_quantity_accepts_only_exact_values(value, expected):
    assert _quantity(value) == expected


def test_negative_delta_is_allowed_only_when_asked():
    """매입 조정은 **줄이는 쪽**도 있다 — 그때 음수가 정상 입력이다."""
    assert _quantity(Decimal(-200), allow_negative=True) == Decimal(-200)
    assert _quantity(Decimal(-200)) is None
