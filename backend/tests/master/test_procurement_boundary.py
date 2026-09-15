"""**그날 매입 판단이 받아 둔 경계를 읽어 온다** — 그리고 못 읽으면 왜인지 말한다.

🔴 **이 판이 가르는 것 셋.** 매입 `basis` 의 `unknown` 하나로 접혀 있던 것들이다.

```text
NOT_EXECUTION_DAY    토요일이라 매입 판단이 안 돌았다        → "못 물어봤다"
LEDGER_GAP           장부 관문이 막아서 판단을 안 돌렸다      → "장부가 안 섰다"
NO_PROCUREMENT_RUN   실행일인데 그날 행이 없다               → 운영 공백
```

⚠️ **DB 를 안 탄다.** 표 접근(`list_runs`)을 대역으로 준다 — 실 DB 는 팀 공용이라
  값이 바뀌고, 그 위에서 재면 검사가 오늘만 초록이다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.master import procurement_boundary
from app.master.clock import SEOUL
from app.master.execution_day import CalendarNotCovered
from app.master.procurement_boundary import (
    ABSENT_REASONS,
    ProcurementBoundary,
    read_procurement_boundary,
)
from app.master.run_repository import LEDGER_GAP_END_CODE, ledger_gap_request_id

#: 실행일(금요일). 실측으로 이 날 매입 판단 행이 서 있다.
평일 = date(2026, 1, 23)

#: 실행일이 아닌 날(토요일). 실측으로 이 날 `E4_NOT_STARTED` 행이 **품목을 달고** 섰다 —
#: 관문 행이 아니라 매입 행이다. 종료코드만 보면 여기서 틀린다 (`#469` 와 같은 함정).
토요일 = date(2026, 1, 24)

축 = "SIM-BURNIN-202512"
남의축 = "SIM-OTHER-0001"

#: 실측값 (2026-09-09 · `master_agent_runs` · `cycle='PROCUREMENT'`).
창고여유 = 5134.6
임차상한 = 0.0  # ★ 물류 확정값. **읽은 `0.0` 이다** — 못 읽은 것과 구별되어야 한다.
재무상한 = 168012
납기일수 = 2


def _row(
    *,
    as_of: date = 평일,
    sim_run_id: str = 축,
    item: str | None = "배추",
    end_code: str = "E1_APPROVED",
    created_at: datetime | None = None,
    inventory: dict[str, Any] | None = None,
    finance: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    run_id: str = "11111111-1111-1111-1111-111111111111",
    request_id: str | None = None,
) -> dict[str, Any]:
    """표의 행 하나. **실측 payload 모양을 그대로 쓴다.**"""
    if constraints is None:
        constraints = {}
        if inventory is not None:
            constraints["inventory"] = inventory
        if finance is not None:
            constraints["finance"] = finance
    return {
        "run_id": UUID(run_id),
        "request_id": (
            f"REQ-DAILY-{sim_run_id}-{as_of:%Y%m%d}-{item}" if request_id is None else request_id
        ),
        "as_of": as_of,
        "cycle": "PROCUREMENT",
        "run_seq": 1,
        "item": item,
        "end_code": end_code,
        "runtime_status": "READY",
        "coverage_ran": None,
        "coverage_total": None,
        "elapsed_ms": 1,
        "plan": [],
        "request_payload": {},
        "response_payload": {"constraints": constraints},
        "sim_run_id": sim_run_id,
        "created_at": created_at or datetime(2026, 9, 9, 9, 0, tzinfo=SEOUL),
    }


def _경계행(**kwargs: Any) -> dict[str, Any]:
    """네 값을 다 든 정상 행."""
    kwargs.setdefault(
        "inventory",
        {
            "warehouse_free_kg": 창고여유,
            "rental_cap_kg": 임차상한,
            "inbound_lead_days": 납기일수,
            "guaranteed_capacity_kg": 8000.0,
            "burst_capacity_kg": 9600.0,
        },
    )
    kwargs.setdefault("finance", {"finance_cap_amount_krw": 재무상한})
    return _row(**kwargs)


def _관문행(*, as_of: date = 평일, **kwargs: Any) -> dict[str, Any]:
    """장부 관문 행 (`#465`). **업무 키가 이 행의 정체다** — 품목이 없고 경계를 안 든다.

    ★ **키를 손으로 안 적는다.** `record_ledger_gap` 이 표에 적는 값과 같은 함수를
      부른다 — 여기서 문자열을 지어내면 되찾는 쪽만 초록인 검사가 된다.
    """
    kwargs.setdefault(
        "request_id", ledger_gap_request_id(as_of, sim_run_id=kwargs.get("sim_run_id", 축))
    )
    return _row(
        as_of=as_of,
        item=None,
        end_code=LEDGER_GAP_END_CODE,
        constraints={},
        **kwargs,
    )


class _표:
    """`list_runs` 대역. **받은 조건대로 거른다** — 진짜와 같은 태도다.

    🔴 걸러 주지 않으면 *"축을 안 좁혀도 초록"* 인 검사가 된다. 필터를 뺀 변이가
      빨간불이 되려면 대역이 필터를 실제로 존중해야 한다.
    """

    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows = list(rows)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        out = [
            row
            for row in self.rows
            if all(
                kwargs.get(column) is None or row[column] == kwargs[column]
                for column in ("cycle", "as_of", "sim_run_id", "item", "request_id")
            )
        ]
        out.sort(key=lambda row: row["created_at"], reverse=True)
        return out[: kwargs.get("limit", 50)]


@pytest.fixture
def 표(monkeypatch):
    """대역을 꽂는다. 검사마다 행을 넣어 쓴다."""

    def _꽂기(*rows: dict[str, Any]) -> _표:
        대역 = _표(*rows)
        monkeypatch.setattr(procurement_boundary, "list_runs", 대역)
        return 대역

    return _꽂기


# ══════════════════════════════════════════════════════════════════════
#  ① 그날 행이 있으면 네 값을 읽어 온다
# ══════════════════════════════════════════════════════════════════════


def test_네_값을_읽어_온다(표):
    """🔴 **재료 넷이 이 판의 결과물이다.** 하나라도 안 오면 매입이 가능량을 못 낸다."""
    대역 = 표(_경계행())

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 대역.calls, "표를 한 번도 안 읽었다 — 아무것도 안 쟀다"
    assert 경계.present is True
    assert 경계.absent_reason is None
    assert 경계.warehouse_free_kg == 창고여유
    assert 경계.rental_cap_kg == 임차상한
    assert 경계.finance_cap_amount_krw == 재무상한
    assert 경계.inbound_lead_days == 납기일수


def test_가능량을_계산하지_않는다(표):
    """🔴 **나눗셈의 단가가 매입 것이다.** 우리가 세면 단가가 바뀌는 날 두 값이 갈린다.

    재료 넷 말고 *"가능량"* 을 닮은 칸이 결과에 생기면 그 순간 주인이 둘이 된다.
    """
    표(_경계행())

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    가능량비슷 = [name for name in vars(경계) if "procurable" in name or "quantity" in name]
    assert 가능량비슷 == [], f"마스터가 가능량을 계산했다: {가능량비슷}"


def test_임차상한_0은_읽은_값이다(표):
    """🔴 **`0.0` 과 `None` 을 가른다.** 물류가 확정한 `0.0` 은 *"자리가 없다"* 이고
    `None` 은 *"못 읽었다"* 다 — 매입이 `null ≠ 0` 을 계약으로 청했다.

    ★ **자기 생존.** 같은 검사가 *"칸이 없으면 `None`"* 까지 확인한다 — 늘 `None` 을
      내는 구현으로도 초록이 되는 비교를 만들지 않는다.
    """
    표(_경계행())
    읽은0 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 읽은0.rental_cap_kg == 0.0
    assert 읽은0.rental_cap_kg is not None

    표(_경계행(inventory={"warehouse_free_kg": 창고여유}, finance={}))
    못읽음 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 못읽음.rental_cap_kg is None, "못 읽은 칸을 0 으로 채웠다"
    assert 못읽음.inbound_lead_days is None
    assert 못읽음.finance_cap_amount_krw is None
    assert 못읽음.warehouse_free_kg == 창고여유, "읽을 수 있는 칸까지 버렸다"


def test_없는_칸을_0으로_안_채운다(표):
    """🔴 **경계를 든 행인데 칸 하나가 비었을 때가 제일 위험하다.**

    `0` 으로 채우면 *"창고가 꽉 찼다"* 라는 **없는 사실**이 서고, 판매가 그걸 보고
    후보를 접는다. 오류는 안 난다.
    """
    표(_경계행(inventory={"rental_cap_kg": 임차상한}, finance={}))

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 경계.present is True
    assert 경계.warehouse_free_kg is None
    assert 경계.inbound_lead_days is None
    assert 경계.finance_cap_amount_krw is None


# ══════════════════════════════════════════════════════════════════════
#  ② 출처 — 그 경계가 어느 실행의 언제 것인가
# ══════════════════════════════════════════════════════════════════════


def test_출처에_실행과_시점이_담긴다(표):
    """★ 그 경계는 **그날 매입 판단 시점**의 것이다. 판단 뒤 출고가 나가면 창고가
    바뀌므로, 낮에 판매가 물으면 아침 값이다. **숨기지 않는다.**
    """
    행 = _경계행()
    표(행)

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 경계.source_ref is not None
    assert str(행["run_id"]) in 경계.source_ref, "어느 실행에서 왔는지가 없다"
    assert 평일.isoformat() in 경계.source_ref, "언제 것인지가 없다"


def test_출처_없이는_읽었다고_말할_수_없다():
    """🔴 **성립하지 않는 모양은 만들 수 없게 막는다** (봉투의 앞쪽 층과 같은 자리)."""
    with pytest.raises(ValueError):
        ProcurementBoundary(present=True, warehouse_free_kg=창고여유)


# ══════════════════════════════════════════════════════════════════════
#  ③ 못 읽을 때 — 사유 셋과 판정 순서
# ══════════════════════════════════════════════════════════════════════


def test_실행일이_아니면_그렇게_말한다(표):
    """🔴 화면이 **"토요일이라 못 물어봤다"** 까지 말할 수 있어야 한다."""
    표()

    경계 = read_procurement_boundary(as_of=토요일, sim_run_id=축)

    assert 경계.present is False
    assert 경계.absent_reason == "NOT_EXECUTION_DAY"


def test_실행일인데_행이_없으면_운영_공백이다(표):
    """★ 이것은 정상 동작이 아니라 **공백**이다. 토요일과 한 낱말로 접으면 안 된다."""
    표()

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 경계.absent_reason == "NO_PROCUREMENT_RUN"


def test_관문이_막은_날은_장부_공백이다(표):
    """🟢 **이제 잴 수 있다** — `#465`·`#467` 이 관문 행을 축과 함께 남긴다."""
    표(_관문행())

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 경계.absent_reason == "LEDGER_GAP"


def test_사유_셋이_다_나온다(표):
    """★ **자기 생존.** 셋 중 하나만 나오는 구현으로는 초록이 안 된다."""
    나온것 = set()

    표()
    나온것.add(read_procurement_boundary(as_of=토요일, sim_run_id=축).absent_reason)
    표()
    나온것.add(read_procurement_boundary(as_of=평일, sim_run_id=축).absent_reason)
    표(_관문행())
    나온것.add(read_procurement_boundary(as_of=평일, sim_run_id=축).absent_reason)

    assert 나온것 == ABSENT_REASONS, f"사유 어휘를 다 못 냈다: {sorted(나온것)}"


def test_행이_있으면_왜_없는지_묻지_않는다(표):
    """🔴 **판정 순서 `①` 이 맨 앞이다.**

    토요일에 손으로 돌린 판단이 있으면 그 경계가 답이다 — 뒤로 밀면 실제로 읽은
    값을 두고 *"실행일이 아니라 못 읽었다"* 가 나간다.
    """
    표(_경계행(as_of=토요일))

    경계 = read_procurement_boundary(as_of=토요일, sim_run_id=축)

    assert 경계.present is True, "행이 있는데 없다고 했다"
    assert 경계.warehouse_free_kg == 창고여유


def test_관문이_실행일_판정보다_앞선다(표):
    """🔴 **판정 순서 `②` 가 `③` 보다 앞이다.**

    관문 행이 선 토요일에 *"실행일이 아니다"* 로 답하면, 장부가 안 선 사실이 사라진다.
    """
    표(_관문행(as_of=토요일))

    경계 = read_procurement_boundary(as_of=토요일, sim_run_id=축)

    assert 경계.absent_reason == "LEDGER_GAP"


def test_관문_행을_품목_없음으로_가른다(표):
    """🔴 **종료코드만으로 잡으면 틀린다** (`#469` 에서 만난 함정).

    실측 `01-24`·`01-31` 의 `E4_NOT_STARTED` 여섯 행은 **품목이 실린 매입 행**이다.
    관문은 하루를 통째로 돌려세우므로 품목이 없다 — 그 칸이 가른다.

    ★ **자기 생존.** 같은 검사가 진짜 관문 행에서는 `LEDGER_GAP` 이 나오는 것까지
      확인한다 — 관문을 영영 못 잡는 구현으로도 초록이 되지 않는다.
    """
    표(_row(as_of=토요일, item="배추", end_code=LEDGER_GAP_END_CODE, constraints={}))
    품목있는E4 = read_procurement_boundary(as_of=토요일, sim_run_id=축)

    assert 품목있는E4.absent_reason == "NOT_EXECUTION_DAY", "품목이 실린 E4 를 관문으로 읽었다"

    표(_관문행(as_of=토요일))
    진짜관문 = read_procurement_boundary(as_of=토요일, sim_run_id=축)

    assert 진짜관문.absent_reason == "LEDGER_GAP", "진짜 관문 행도 못 잡는다"


def test_못_읽으면_값_넷이_전부_없다(표):
    """🔴 못 읽었다면서 값이 실려 있으면 **받는 쪽이 그 값을 쓴다.**"""
    표()

    for 날 in (평일, 토요일):
        경계 = read_procurement_boundary(as_of=날, sim_run_id=축)
        assert 경계.present is False
        assert 경계.source_ref is None
        assert 경계.warehouse_free_kg is None
        assert 경계.rental_cap_kg is None
        assert 경계.finance_cap_amount_krw is None
        assert 경계.inbound_lead_days is None


def test_사유_어휘_밖의_값은_만들_수_없다():
    """★ 닫힌 집합인데 아무도 안 보면 새 낱말이 조용히 는다 (봉투 `Trigger` 의 교훈)."""
    with pytest.raises(ValueError):
        ProcurementBoundary(present=False, absent_reason="MYSTERY")  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════
#  ④ 축 — 남의 걷기가 안 섞인다
# ══════════════════════════════════════════════════════════════════════


def test_다른_축의_행이_안_섞인다(표):
    """🔴 섞이면 **남의 걷기 경계**가 이 판매 후보의 답으로 실린다.

    ★ **자기 생존.** 남의 축에 실제로 행이 있고, 그 축으로 물으면 그 값이 나오는
      것까지 같은 검사가 확인한다 — 대역이 통째로 비어 있어도 초록이 되지 않는다.
    """
    남의값 = 9999.9
    표(
        _경계행(sim_run_id=남의축, inventory={"warehouse_free_kg": 남의값}),
    )

    내경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)
    assert 내경계.present is False, f"남의 축 행을 읽었다: {내경계.warehouse_free_kg}"
    assert 내경계.absent_reason == "NO_PROCUREMENT_RUN"

    남의경계 = read_procurement_boundary(as_of=평일, sim_run_id=남의축)
    assert 남의경계.warehouse_free_kg == 남의값, "남의 축에도 행이 없다 — 아무것도 안 쟀다"


def test_다른_날의_행이_안_섞인다(표):
    """★ 경계는 **그날** 것이다. 어제 창고로 오늘을 판단하면 오류 없이 틀린다."""
    표(_경계행(as_of=평일 - timedelta(days=1)))

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 경계.present is False


def test_빈_축으로는_물을_수_없다(표):
    """🔴 **빈 값을 조용히 전체로 바꾸지 않는다** (`check_walk_scope` 와 같은 태도).

    `ExecutionContext.sim_run_id` 의 기본값이 `""` 이고 그것은 *"아직 안 실렸다"* 다.
    그대로 물어 `NO_PROCUREMENT_RUN` 을 받으면 **안 물어본 것이 공백으로 읽힌다.**
    """
    표(_경계행())

    with pytest.raises(ValueError):
        read_procurement_boundary(as_of=평일, sim_run_id="")
    with pytest.raises(ValueError):
        read_procurement_boundary(as_of=평일, sim_run_id="   ")


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 같은 날 행이 여럿이면 — **최신 회차가 오늘의 사실이다**
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ **스펙(§3.5)은 *"품목별로 값이 다르면 사고"* 라고 적었는데 실측이 다르다.**
#   축이 실린 15개 (축·날) 묶음 중 5개에서 `warehouse_free_kg` 가 갈린다. 갈리는
#   자리는 품목이 아니라 **같은 날을 다시 걸은 회차**이고 — 한 회차 안의 배추·무·양파는
#   실측 전부가 같은 값이다. 그러니 갈린 것은 사고가 아니라 세상이 바뀐 것이고,
#   여기서 막으면 실제 걷기의 3분의 1에서 경계를 못 읽는다.
#
# 🟢 대신 **최신 회차를 고르고, 어느 실행에서 왔는지를 `source_ref` 가 말한다.**


def test_같은_날_행이_여럿이면_최신을_읽는다(표):
    """🔴 옛 회차를 읽으면 **오늘 답이 어제 창고**가 된다 — 오류는 안 난다."""
    이른 = datetime(2026, 9, 9, 9, 15, tzinfo=SEOUL)
    늦은 = datetime(2026, 9, 9, 10, 1, tzinfo=SEOUL)
    표(
        _경계행(
            created_at=늦은,
            run_id="22222222-2222-2222-2222-222222222222",
            inventory={"warehouse_free_kg": 7932.52},
        ),
        _경계행(created_at=이른, inventory={"warehouse_free_kg": 4058.6}),
    )

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 경계.warehouse_free_kg == 7932.52
    assert "22222222-2222-2222-2222-222222222222" in (경계.source_ref or "")


def test_경계를_안_든_행을_건너뛴다(표):
    """★ 관문 행이 판단 행보다 **나중에** 설 수 있다. 그때도 경계는 읽힌다."""
    표(
        _관문행(created_at=datetime(2026, 9, 9, 11, 0, tzinfo=SEOUL)),
        _경계행(created_at=datetime(2026, 9, 9, 9, 0, tzinfo=SEOUL)),
    )

    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 경계.present is True
    assert 경계.warehouse_free_kg == 창고여유


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 실행일 판정을 다시 짓지 않는다
# ══════════════════════════════════════════════════════════════════════


class _달력:
    def __init__(self, *, 공휴일: set[date] | None = None, 덮는날: set[date] | None = None) -> None:
        self.공휴일 = 공휴일 or set()
        self.덮는날 = 덮는날

    def is_holiday(self, day: date) -> bool:
        if self.덮는날 is not None and day not in self.덮는날:
            raise CalendarNotCovered(day.isoformat())
        return day in self.공휴일


def test_공휴일도_실행일이_아니다(표):
    """⚠️ **요일 판정을 여기서 다시 짓지 않는다** — `is_execution_day` 를 부른다.

    ★ **자기 생존.** 달력을 안 준 같은 날은 `NO_PROCUREMENT_RUN` 이 나오는 것까지
      확인한다 — 달력을 안 보고도 초록이 되는 비교를 만들지 않는다.
    """
    표()

    달력있음 = read_procurement_boundary(as_of=평일, sim_run_id=축, calendar=_달력(공휴일={평일}))
    assert 달력있음.absent_reason == "NOT_EXECUTION_DAY"

    달력없음 = read_procurement_boundary(as_of=평일, sim_run_id=축)
    assert 달력없음.absent_reason == "NO_PROCUREMENT_RUN", "달력을 안 줬는데 공휴일을 안다"


def test_달력이_못_덮는_날을_평일로_단정하지_않는다(표):
    """🔴 잡아서 넘기면 **달력이 끊긴 것과 실행일인 것이 같아진다.**

    `execution_day` 가 *"부르는 쪽이 정한다"* 로 남긴 자리다. 우리는 정하지 않고
    그대로 올린다 — 행이 있는 날은 `①` 에서 이미 끝나므로 이 예외는 **답이 정말
    달력에 걸릴 때만** 난다.
    """
    표()
    with pytest.raises(CalendarNotCovered):
        read_procurement_boundary(as_of=평일, sim_run_id=축, calendar=_달력(덮는날=set()))

    표(_경계행())
    경계 = read_procurement_boundary(as_of=평일, sim_run_id=축, calendar=_달력(덮는날=set()))
    assert 경계.present is True, "행이 있는데도 달력을 물으러 갔다"
