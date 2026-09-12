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
from app.logistics.agent.tools import (
    ACTION_UNSUPPORTED,
    IMPACT_INPUT_MISSING,
    SUPPORTED_ACTIONS,
    InboundScheduleFact,
    LotFact,
    _allocated_by_lot,
    _as_of_snapshot,
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
    monkeypatch.setattr(agent_tools, "load_schedule_views", lambda *a, **k: ())

    result = get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF, days=requested_days)

    assert result.days == expected_days


def test_schedules_arriving_after_the_window_are_dropped(monkeypatch):
    inside = _schedule_view("INB-IN", arrives=AS_OF + timedelta(days=2), created=AS_OF)
    outside = _schedule_view("INB-OUT", arrives=AS_OF + timedelta(days=40), created=AS_OF)
    monkeypatch.setattr(agent_tools, "load_schedule_views", lambda *a, **k: (inside, outside))

    result = get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF)

    assert [one.inbound_id for one in result.schedules] == ["INB-IN"]
    assert isinstance(result.schedules[0], InboundScheduleFact)


def test_inbound_schedule_carries_a_real_business_observed_at(monkeypatch):
    """✅ **장부에 선 날**(`created_as_of`)이 있다 — 정책·예약 축과 다른 자리다."""
    monkeypatch.setattr(
        agent_tools,
        "load_schedule_views",
        lambda *a, **k: (
            _schedule_view("INB-1", arrives=AS_OF + timedelta(days=2), created=date(2026, 1, 10)),
            _schedule_view("INB-2", arrives=AS_OF + timedelta(days=3), created=date(2026, 1, 14)),
        ),
    )

    result = get_inbound_schedule(None, sim_run_id=SIM, as_of=AS_OF)

    assert result.observed_as_of == date(2026, 1, 14)
    assert result.observed_as_of <= AS_OF


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
