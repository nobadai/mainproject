"""탐지기 둘과 관측일 규칙 — **DB 없이 재는 것들** (#628 Commit 2).

```text
경계        ratio == 임계 · usage == 임계 에서 실제로 서는가
안 서는 것  이미 팔린 Lot · 만료된 Lot · 기준 없는 날
급한 정도   CRITICAL / HIGH / MEDIUM 이 무엇으로 갈리는가
관측일      사실마다 다르다 · max · 하나라도 None 이면 None
            🔴 as_of · 오늘 · created_at · 입고일을 편의로 대입하지 않는가
```

🔴 **표에 쓰는 일은 여기서 안 잰다.** 중복 방지 · 해소 · 재발 연결은 실제 제약이
   있어야 의미가 있어 `test_logistics_agent_exceptions_db.py` 가 실 DB 에서 잰다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.logistics.agent.detect import (
    CAPACITY_HIGH_RATIO,
    COMMITTED,
    ESCALATED_FRESHNESS_EXPIRED,
    LOT_EMPTY,
    REDETECT,
    _resolution,
    detect_capacity_pressure,
    detect_freshness_pressure,
)
from app.logistics.agent.observe import (
    CAPACITY_WINDOW_USAGE_UNRESOLVED,
    LEDGER_MOVE_UNRESOLVED,
    OBSERVATION_INCONSISTENT,
    _quantity_observed_as_of,
    _status_observed_as_of,
)
from app.logistics.agent.schemas import (
    CAPACITY_PRESSURE,
    COMMITMENT_OBSERVED_AS_OF,
    FRESHNESS_PRESSURE,
    POLICY_OBSERVED_AS_OF,
    WAREHOUSE_SUBJECT_ID,
    ExceptionEvidence,
    ExceptionRow,
    ObservedCapacity,
    ObservedLot,
    ObservedPolicy,
    WarehouseObservation,
    derive_observed_as_of,
    snapshot_observed_as_of,
)
from app.logistics.historical_repository import LedgerLotState
from app.logistics.rules import (
    CAPACITY_TIGHT_POLICY_UNRESOLVED,
    FRESHNESS_PRESSURE_POLICY_UNRESOLVED,
)
from app.logistics.schemas import InventoryLotSnapshot

AS_OF = date(2026, 1, 20)
SIM = "SIM-DETECT-TEST"
#: 시뮬레이션 달력의 시간대 (`historical_repository._KST` 와 같다).
_KST = ZoneInfo("Asia/Seoul")
#: 실 DB 정책값 그대로 (`agent_policy_config`) — 검사에서 다른 숫자를 쓰면 경계를
#: 재는 의미가 없다.
FRESHNESS_RATIO = Decimal("0.30")
CAPACITY_RATIO = Decimal("0.90")


def _lot(
    *,
    lot_id: str = "LOT-1",
    item: str = "배추",
    status: str = "ACTIVE",
    remaining: int | None = 3,
    limit: int | None = 10,
    qty: str = "500",
    uncommitted: str | None = "500",
    priority_days: int | None = 3,
    received: date | None = None,
    moved: date | None = None,
    moved_unknown: bool = False,
) -> ObservedLot:
    """검사용 Lot 하나.

    ★ `received` 는 **입고일**(불변), `moved` 는 **마지막 원장 이동일**(가변 잔량의
      관측일)이다. 기본값이 서로 다른 날인 것이 중요하다 — 같게 두면 둘을 뭉갠
      구현이 검사를 통과한다.
    """
    받은날 = received if received is not None else AS_OF - timedelta(days=7)
    옮긴날 = None if moved_unknown else (moved if moved is not None else AS_OF - timedelta(days=2))
    return ObservedLot(
        lot_id=lot_id,
        item=item,
        item_id=f"ITEM-{item}",
        status=status,
        received_at=받은날,
        remaining_qty_kg=Decimal(qty),
        uncommitted_kg=None if uncommitted is None else Decimal(uncommitted),
        remaining_freshness_days=remaining,
        effective_freshness_limit_days=limit,
        sell_priority_remaining_days=priority_days,
        storage_zone="COLD_HUMID_0_3",
        remaining_qty_observed_as_of=옮긴날,
        status_observed_as_of=받은날 if status == "ACTIVE" else None,
    )


def _관측(
    *lots: ObservedLot,
    usage: str | None = None,
    freshness_ratio: Decimal | None = FRESHNESS_RATIO,
    capacity_ratio: Decimal | None = CAPACITY_RATIO,
    open_exceptions: tuple[ExceptionRow, ...] = (),
) -> WarehouseObservation:
    return WarehouseObservation(
        sim_run_id=SIM,
        as_of=AS_OF,
        inventory_observed_as_of=derive_observed_as_of(
            [one.remaining_qty_observed_as_of for one in lots]
        ),
        lots=lots,
        capacity=ObservedCapacity(
            used_kg=Decimal(7200),
            guaranteed_kg=Decimal(8000),
            burst_kg=Decimal(9600),
            window_usage_ratio=None if usage is None else Decimal(usage),
        ),
        policy=ObservedPolicy(
            freshness_pressure_ratio=freshness_ratio,
            capacity_tight_ratio=capacity_ratio,
        ),
        open_exceptions=open_exceptions,
    )


# ===========================================================================
# A. FRESHNESS_PRESSURE — Core 메인
# ===========================================================================


def test_비율이_임계와_같으면_탐지한다():
    """🔴 **경계가 `<=` 다.** `count_freshness_risk_lots` 와 같은 자리에서 서야
    *"위험 Lot 1건"* 이라고 답한 회신과 Exception 이 같은 수를 센다."""
    out = detect_freshness_pressure(_관측(_lot(remaining=3, limit=10)))

    assert out.ran
    assert [one.code for one in out.conditions] == [FRESHNESS_PRESSURE]
    assert out.conditions[0].subject_type == "LOT"
    assert out.conditions[0].subject_id == "LOT-1"


def test_임계를_넘으면_탐지하지_않는다():
    """0.4 > 0.3 — 아직 여유가 있다."""
    assert detect_freshness_pressure(_관측(_lot(remaining=4, limit=10))).conditions == ()


def test_이미_다_잡힌_Lot_은_탐지하지_않는다():
    """🔴 **이 조건이 탐지기의 핵심이다.** 이미 판매 확정·할당된 재고까지
    *"빨리 파세요"* 로 올리면 사람이 매일 같은 제안을 지운다."""
    assert detect_freshness_pressure(_관측(_lot(uncommitted="0"))).conditions == ()


def test_예약_축을_못_읽은_Lot_은_탐지하지_않는다():
    """`None` 은 0 이 아니라 **셈을 못 한 것**이다 — 못 읽은 축으로 문제를 만들지 않는다."""
    assert detect_freshness_pressure(_관측(_lot(uncommitted=None))).conditions == ()


def test_신선도가_다한_Lot_은_압박이_아니다():
    """🔴 `remaining <= 0` 은 **다른 사실**이다 (§7.1 E). 압박 Exception 으로 계속 들고
    있으면 *"빨리 팔면 된다"* 와 *"이제 못 판다"* 가 한 줄에 섞인다."""
    assert detect_freshness_pressure(_관측(_lot(remaining=0))).conditions == ()
    assert detect_freshness_pressure(_관측(_lot(remaining=-2))).conditions == ()


@pytest.mark.parametrize("status", ["HOLD", "DISPOSED", "DEPLETED"])
def test_ACTIVE_가_아닌_Lot_은_탐지하지_않는다(status):
    """판매 가용 기준(`tools._AVAILABLE_LOT_STATUS`)과 **같은 눈**으로 본다."""
    assert detect_freshness_pressure(_관측(_lot(status=status))).conditions == ()


def test_보관한계를_모르면_비율을_지어내지_않는다():
    """한계가 없으면 분모가 없다 — 0 으로도 평균으로도 메우지 않는다."""
    assert detect_freshness_pressure(_관측(_lot(remaining=None, limit=None))).conditions == ()


def test_임계_정책이_없으면_탐지기가_아예_안_돈다():
    """🔴 **«확인했고 문제 없음» 이 아니라 «기준이 없어 못 쟀다» 다.** `ran=False` 여야
    해소(RESOLVED)도 안 일어난다 — 정책 미등재가 *"해결됐다"* 로 장부에 남으면 안 된다."""
    out = detect_freshness_pressure(_관측(_lot(), freshness_ratio=None))

    assert out.ran is False
    assert out.skipped == FRESHNESS_PRESSURE_POLICY_UNRESOLVED
    assert out.conditions == ()


@pytest.mark.parametrize(
    ("remaining", "limit", "priority_days", "기대"),
    [
        (1, 10, 3, "CRITICAL"),  # 내일이면 못 판다
        (3, 10, 3, "HIGH"),  # 회전 정책이 «우선 팔라» 고 한 구간
        (3, 10, 2, "MEDIUM"),  # 압박이지만 그 구간은 아직 아니다
        (3, 10, None, "MEDIUM"),  # 🔴 정책이 없는 품목을 더 급하다고 말하지 않는다
        (1, 10, None, "CRITICAL"),  # 잔여 경계는 정책과 무관하다
    ],
)
def test_얼마나_급한지가_잔여와_회전정책으로_갈린다(remaining, limit, priority_days, 기대):
    out = detect_freshness_pressure(
        _관측(_lot(remaining=remaining, limit=limit, priority_days=priority_days))
    )

    assert [one.severity for one in out.conditions] == [기대]


def test_근거가_값과_출처와_관측일을_함께_싣는다():
    """🔴 **근거 없는 Exception 을 만들지 않는다.** 그리고 근거는 *"무슨 값이 · 어디서
    왔고 · 언제부터 알 수 있었나"* 셋을 다 말해야 한다."""
    받은날, 옮긴날 = date(2026, 1, 13), date(2026, 1, 18)
    조건 = detect_freshness_pressure(
        _관측(_lot(received=받은날, moved=옮긴날))
    ).conditions[0]
    사실 = {one.fact: one for one in 조건.evidence}

    assert {
        "remaining_freshness_days",
        "effective_freshness_limit_days",
        "freshness_remaining_ratio",
        "freshness_pressure_ratio",
        "remaining_qty_kg",
        "uncommitted_kg",
        "sell_priority_remaining_days",
    } <= set(사실)
    assert 사실["freshness_pressure_ratio"].value == FRESHNESS_RATIO
    assert 사실["freshness_pressure_ratio"].source == "agent_policy_config"
    # 가변 잔량은 **마지막 이동일**이고 …
    assert 사실["remaining_qty_kg"].observed_as_of == 옮긴날
    # 🔴 … 입고일이 아니다. 입고 뒤 출고·폐기가 잔량을 바꾼다.
    assert 사실["remaining_qty_kg"].observed_as_of != 받은날
    # … 정책 축은 유효일 칸이 없어 `None` 이다.
    assert 사실["freshness_pressure_ratio"].observed_as_of is None


def test_정책값이_섞이면_Exception_의_관측일이_None_이다():
    """🔴 **§18 그대로다.** 하나라도 관측일이 없으면 파생값의 관측일은 `None` 이고,
    그것이 *"안 쟀다"* 라는 사실이다 — `as_of` 로 메우면 그 사실이 사라진다."""
    조건 = detect_freshness_pressure(_관측(_lot())).conditions[0]

    assert 조건.observed_as_of is None
    assert 조건.observed_as_of != AS_OF


def test_근거는_소수를_문자열로_적는다():
    """JSON 수치로 적으면 읽을 때 float 을 지나 `0.30` 이 흔들린다."""
    조건 = detect_freshness_pressure(_관측(_lot())).conditions[0]
    실린값 = {one["fact"]: one["value"] for one in (e.as_json() for e in 조건.evidence)}

    assert 실린값["freshness_pressure_ratio"] == "0.30"
    assert ExceptionEvidence.from_json(조건.evidence[0].as_json()) == 조건.evidence[0]


# ===========================================================================
# B. CAPACITY_PRESSURE — Core 보조
# ===========================================================================


def test_사용률이_임계와_같으면_탐지한다():
    """기존 `CAPACITY_TIGHT` 와 **같은 비교**(`>=`)다."""
    out = detect_capacity_pressure(_관측(_lot(), usage="0.90"))

    assert out.ran
    assert [one.code for one in out.conditions] == [CAPACITY_PRESSURE]
    assert out.conditions[0].subject_type == "WAREHOUSE"
    assert out.conditions[0].subject_id == WAREHOUSE_SUBJECT_ID


def test_임계_아래면_탐지하지_않고_그래도_돈_것이다():
    """🔴 **`ran=True` 가 중요하다** — 재 봤는데 괜찮았어야 기존 행을 닫을 수 있다."""
    out = detect_capacity_pressure(_관측(_lot(), usage="0.89"))

    assert out.ran is True
    assert out.conditions == ()


@pytest.mark.parametrize(
    ("usage", "기대"),
    [
        ("0.90", "MEDIUM"),
        ("0.94", "MEDIUM"),
        ("0.95", "HIGH"),
        ("0.99", "HIGH"),
        ("1.00", "CRITICAL"),
    ],
)
def test_사용률_구간이_급한_정도를_가른다(usage, 기대):
    out = detect_capacity_pressure(_관측(_lot(), usage=usage))

    assert [one.severity for one in out.conditions] == [기대]
    assert CAPACITY_HIGH_RATIO == Decimal("0.95")


def test_사용률을_못_셈하면_탐지기가_안_돈다():
    """리드타임·보장 용량이 없으면 창을 못 만든다 — 0 으로 놓으면 «여유 있음» 이 된다."""
    out = detect_capacity_pressure(_관측(_lot(), usage=None))

    assert out.ran is False
    assert out.skipped == CAPACITY_WINDOW_USAGE_UNRESOLVED


def test_용량_임계_정책이_없으면_탐지기가_안_돈다():
    out = detect_capacity_pressure(_관측(_lot(), usage="0.99", capacity_ratio=None))

    assert out.ran is False
    assert out.skipped == CAPACITY_TIGHT_POLICY_UNRESOLVED


def test_용량_근거의_점유는_잔량_축_관측일을_따라간다():
    """물리 점유는 **그 Lot 들의 잔량 합**이다 — 입고일이 아니라 마지막 이동일이 정한다.

    🔴 입고일 최댓값을 쓰면, 입고 뒤 출고로 점유가 줄어든 날이 통째로 안 보인다.
    """
    관측 = _관측(
        _lot(received=date(2026, 1, 2), moved=date(2026, 1, 10)),
        _lot(lot_id="LOT-2", received=date(2026, 1, 19), moved=date(2026, 1, 16)),
        usage="0.95",
    )
    조건 = detect_capacity_pressure(관측).conditions[0]
    사실 = {one.fact: one for one in 조건.evidence}

    assert 사실["used_capacity_kg"].observed_as_of == date(2026, 1, 16)
    # 🔴 입고일 최댓값(1/19)이 아니다.
    assert 사실["used_capacity_kg"].observed_as_of != date(2026, 1, 19)
    assert 사실["capacity_tight_ratio"].observed_as_of is None
    assert 사실["capacity_window_usage_ratio"].observed_as_of is None


def test_한_Lot_의_잔량_관측일을_못_대면_점유_전체가_None_이다():
    """🔴 **합계의 관측일은 가장 약한 고리를 따른다.** 한 Lot 을 못 쟀는데 나머지로
    날짜를 내면, 그 합계가 *"다 재 봤다"* 로 읽힌다."""
    관측 = _관측(
        _lot(moved=date(2026, 1, 10)),
        _lot(lot_id="LOT-2", moved_unknown=True),
        usage="0.95",
    )
    조건 = detect_capacity_pressure(관측).conditions[0]
    사실 = {one.fact: one for one in 조건.evidence}

    assert 사실["used_capacity_kg"].observed_as_of is None


# ===========================================================================
# C. 해소 갈래 — 무엇이 닫았나
# ===========================================================================


def _열린행(code: str = FRESHNESS_PRESSURE, subject_id: str = "LOT-1") -> ExceptionRow:
    return ExceptionRow(
        exception_id="EX-TEST",
        sim_run_id=SIM,
        code=code,
        subject_type="LOT" if code == FRESHNESS_PRESSURE else "WAREHOUSE",
        subject_id=subject_id,
        severity="MEDIUM",
        status="OPEN",
        opened_as_of=AS_OF - timedelta(days=2),
        last_detected_as_of=AS_OF - timedelta(days=1),
        observed_as_of=None,
        evidence=(),
        detector_version="v1",
    )


def test_Lot_이_사라졌으면_비었다고_적는다():
    사유, _ = _resolution(_열린행(), _관측())

    assert 사유 == LOT_EMPTY


def test_잔량이_남았는데_신선도가_다했으면_넘어갔다고_적는다():
    """§7.1 E. 🔴 **후속 Exception 을 만들지 않는다** — 그 탐지기는 Commit 2 에 없다."""
    사유, 비고 = _resolution(_열린행(), _관측(_lot(remaining=0)))

    assert 사유 == ESCALATED_FRESHNESS_EXPIRED
    assert 비고 and "잔량" in 비고


def test_할당이_잔량을_다_덮었으면_확정으로_닫는다():
    """§7.1 C — «해결» 이 아니라 «위험 관리 상태» 다."""
    사유, _ = _resolution(_열린행(), _관측(_lot(remaining=8, uncommitted="0")))

    assert 사유 == COMMITTED


def test_용량은_그냥_회복이다():
    """용량은 되돌아온다 — Lot 처럼 갈래를 나눌 사실이 없다."""
    사유, _ = _resolution(_열린행(code=CAPACITY_PRESSURE, subject_id=WAREHOUSE_SUBJECT_ID), _관측())

    assert 사유 == REDETECT


# ===========================================================================
# D. 관측일 규칙 (§18)
# ===========================================================================


def test_모든_입력의_날짜를_알면_가장_늦은_것이다():
    """🔴 **가장 이른 것을 고르면 늦게 온 입력이 조용히 숨는다.**"""
    assert derive_observed_as_of([date(2026, 1, 10), date(2026, 1, 18)]) == date(2026, 1, 18)


def test_하나라도_모르면_None_이다():
    assert derive_observed_as_of([date(2026, 1, 10), None]) is None


def test_입력이_없으면_None_이다():
    """«잴 것이 없었다» 도 «안 쟀다» 다."""
    assert derive_observed_as_of([]) is None


def test_명시적_관측일_하나면_그_날짜다():
    assert derive_observed_as_of([date(2026, 1, 13)]) == date(2026, 1, 13)


def test_회신의_관측일은_정책값_때문에_None_이고_as_of_가_아니다(complete_logistics_snapshot):
    """🔴 **기본값을 둔 것이 아니라 재 봤더니 못 쟀다.** 네 Mode 는 전부 정책값을
    계산에 넣는데 그 표에 유효일 칸이 없다 — `as_of` 로 메우지 않는다."""
    잰값 = snapshot_observed_as_of(complete_logistics_snapshot)

    assert 잰값 is None
    assert 잰값 != complete_logistics_snapshot.as_of
    assert snapshot_observed_as_of(None) is None


# ===========================================================================
# E. 사실마다 다른 관측일 (§18.2) — **Lot 하나에 날짜 하나가 아니다**
# ===========================================================================


def _원장Lot(lot_id: str = "LOT-1", qty: str = "500") -> InventoryLotSnapshot:
    """원장 대조에 들어가는 스냅샷 Lot. **실제 계약 타입을 그대로 쓴다.**"""
    return InventoryLotSnapshot(
        lot_id=lot_id, item="배추", available_qty_kg=Decimal(qty), status="ACTIVE"
    )


def test_가변_잔량의_관측일은_마지막_이동일이지_입고일이_아니다():
    """```text
    D1  입고 1,000kg
    D5  OUT  300kg
    D8  OUT  200kg
    현재 잔량 500kg
    ```

    🔴 500kg 이라는 **현재** 사실은 D8 의 출고까지 반영된 뒤에야 알 수 있다.
    """
    D1, D8 = date(2026, 1, 1), date(2026, 1, 8)
    원장 = {"LOT-1": LedgerLotState(balance_kg=Decimal(500), last_moved_at=D8)}

    잰날, 사유 = _quantity_observed_as_of(원장, lot=_원장Lot())

    assert 잰날 == D8
    assert 잰날 != D1
    assert 사유 == []


def test_원장에_이동이_없는_Lot_은_잔량_관측일이_None_이다():
    """production 경로면 날 수 없는 일이다 — 그래도 **지어내지 않고 사실로 적는다.**"""
    잰날, 사유 = _quantity_observed_as_of({}, lot=_원장Lot())

    assert 잰날 is None
    assert 사유 == [f"{LEDGER_MOVE_UNRESOLVED}:LOT-1"]


def test_캐시와_원장이_갈리면_잔량_관측일을_비운다():
    """🔴 **갈린 값의 «언제부터» 는 못 댄다.** 원장 날짜를 그대로 붙이면 *"틀린 값을
    이 날부터 알고 있었다"* 가 된다."""
    원장 = {"LOT-1": LedgerLotState(balance_kg=Decimal(480), last_moved_at=date(2026, 1, 8))}

    잰날, 사유 = _quantity_observed_as_of(원장, lot=_원장Lot(qty="500"))

    assert 잰날 is None
    assert 사유 == [f"{OBSERVATION_INCONSISTENT}:LOT-1"]


def test_원장_자체를_못_읽은_날은_모든_잔량_관측일이_None_이다():
    """`ADJUST` 가 섞인 날이다 — 대조도 날짜도 같은 원장에서 나온다."""
    잰날, 사유 = _quantity_observed_as_of(None, lot=_원장Lot())

    assert 잰날 is None
    assert 사유 == []


@pytest.mark.parametrize(
    ("status", "기대"),
    [
        # Lot INSERT 가 적는 값이고, ACTIVE 로 되돌리는 writer 가 없다.
        ("ACTIVE", date(2026, 1, 1)),
        # 잔량을 0 으로 만든 DISPOSE 의 날 (`disposal._mark_disposed`).
        ("DISPOSED", date(2026, 1, 8)),
        # 🔴 production writer 가 하나도 없는 어휘 — 되살릴 사건이 없다.
        ("DEPLETED", None),
        ("HOLD", None),
    ],
)
def test_상태의_관측일은_어휘마다_근거가_다르다(status, 기대):
    잰날 = _status_observed_as_of(
        status, received_at=date(2026, 1, 1), last_moved_at=date(2026, 1, 8)
    )

    assert 잰날 == 기대


def test_불변_사실인_입고일은_그대로_남는다():
    """🔴 **가변 사실을 고치느라 불변 사실까지 None 으로 만들지 않는다.**"""
    받은날 = date(2026, 1, 3)
    lot = _lot(received=받은날, moved=date(2026, 1, 11))

    assert lot.received_at == 받은날
    assert lot.status_observed_as_of == 받은날
    assert lot.remaining_qty_observed_as_of != 받은날


def test_미확정_물량은_예약_축을_못_대서_지금은_언제나_None_이다():
    """`uncommitted_kg = 잔량 − 살아 있는 할당` 이라 **두 축이 다 서야** 날짜가 선다.

    🔴 지금은 예약·할당 축이 `None` 이다 — `outbound.cancel_allocation` 이 할당을
       내리면서 업무 날짜를 하나도 안 남긴다. 붙은 날(`decided_at`)만 모으면 **실제보다
       이른 날**이 적힌다.
    """
    lot = _lot(moved=date(2026, 1, 11))

    assert lot.remaining_qty_observed_as_of == date(2026, 1, 11)
    assert COMMITMENT_OBSERVED_AS_OF is None
    assert lot.uncommitted_observed_as_of is None


def test_미확정_물량의_규칙_자체는_늦은_쪽이다():
    """예약 축이 날짜를 얻는 날 **저절로** 서야 하므로 규칙을 따로 잰다 (§16)."""
    잔량, 할당 = date(2026, 1, 8), date(2026, 1, 7)

    assert derive_observed_as_of([잔량, 할당]) == 잔량
    assert derive_observed_as_of([잔량, None]) is None


def test_잔여_신선도는_보관정책을_지나므로_None_이다():
    """🔴 **입고일로 적지 않는다.** «한계 − 경과» 이고 그 한계가 정책에서 온다 —
    정책이 언제부터 그 값이었는지를 모르면 잔여 일수도 모른다."""
    lot = _lot(received=date(2026, 1, 3))

    assert POLICY_OBSERVED_AS_OF is None
    assert lot.freshness_observed_as_of is None


def test_관측일에_as_of_도_오늘도_안_들어간다():
    """🔴 **§18 의 금지 목록을 통째로 잰다.** 편의 대입은 «안 쟀다» 를 «쟀다» 로
    둔갑시키고, 그 거짓은 값이 아니라 **날짜**에 남아 아무도 못 본다."""
    조건 = detect_freshness_pressure(_관측(_lot())).conditions[0]
    날짜들 = [one.observed_as_of for one in 조건.evidence] + [조건.observed_as_of]

    assert AS_OF not in 날짜들
    assert datetime.now(tz=_KST).date() not in 날짜들
    # 🔴 **미래를 본 관측일이 없다.** 마스터가 게이트를 거는 날 막히면 안 되는 쪽이다.
    assert [one for one in 날짜들 if one is not None and one > AS_OF] == []


#: 관측일 규칙의 주인이 되는 파일들. 🔴 여기서 편의 대입이 한 번이라도 서면 규칙이
#: 무너진다 — 값 검사만으로는 «지금 그 경로를 안 탔을 뿐» 인 것과 구별이 안 된다.
#:
#: ★ `adapter.py` 가 들어 있는 것이 중요하다 — 마스터에게 나가는
#:   `AgentReply.observed_at` 이 서는 자리가 거기 넷이다.
_관측일_모듈 = (
    "agent/schemas.py",
    "agent/observe.py",
    "agent/detect.py",
    "agent/exceptions.py",
    "adapter.py",
)

#: 🔴 `observed_*` 칸에 **대입하면 안 되는 것들** (§18 금지 목록).
#:
#: ⚠️ 낱말 경계를 둔다 — `remaining_qty_observed_as_of` 같은 **정당한 이름** 안의
#:    `as_of` 를 금지로 세면 규칙이 아니라 이름을 막는 것이 된다.
_금지_대입 = (
    r"(?<![\w])as_of\b",
    r"date\.today\(",
    r"datetime\.now\(",
    r"utcnow\(",
    r"(?<![\w])created_at\b",
    r"(?<![\w])updated_at\b",
    r"(?<![\w.])now\(",
)


def _코드만(본문: str) -> str:
    """주석과 문서화 문자열을 걷어낸다 — **금지 목록을 적어 둔 설명이 걸리지 않게.**"""
    본문 = re.sub(r'"""(?:.|\n)*?"""', "", 본문)
    return re.sub(r"(?m)#.*$", "", 본문)


def test_관측일에_편의_대입이_코드에_아예_없다():
    """🔴 **§18 금지 목록을 소스에서 잠근다.**

    ```text
    observed_at    = as_of · 오늘 · created_at      ← 하나라도 있으면 안 된다
    observed_as_of = 같은 것들
    ```

    ★ 값 검사(`test_관측일에_as_of_도_오늘도_안_들어간다`)와 **둘 다** 필요하다.
      저쪽은 지금 도는 경로를 재고, 이쪽은 **아직 안 도는 경로까지** 잠근다.
    """
    농장 = Path(__file__).resolve().parents[2] / "app" / "logistics"
    무늬 = re.compile(r"observed_(?:at|as_of)\s*=\s*([^,\n)]+)")

    걸린것 = [
        f"{이름}: observed_… = {맞은것.strip()}"
        for 이름 in _관측일_모듈
        for 맞은것 in 무늬.findall(_코드만((농장 / 이름).read_text(encoding="utf-8")))
        for 금지 in _금지_대입
        if re.search(금지, 맞은것)
    ]

    assert 걸린것 == []
