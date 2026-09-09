"""
procurement_boundary.py — **그날 매입 판단이 받아 둔 경계를 읽어 온다** (2026-09-09).

판매 후보가 물건이 모자랄 때 매입에 *"얼마를 언제 얼마에 댈 수 있나"* 를 묻기로
계약이 섰다 (매입 확정 2026-09-09 · `batch` · `SUPPLY_CAPACITY_QUERY`). 그런데
매입이 알려 온 것이 하나 더 있다.

```text
procurable_quantity_kg = min( 창고 여유(물류) , 매입 가능액(재무) ÷ 단가(매입) )
```

🔴 **「가능량」이 매입 값이 아니다.** 재료가 물류·재무 봉투다. 그래서 마스터가 그
  둘을 실어 줘야 한다.

★★ **아무도 새로 부르지 않는다.** 값은 **이미 표에 있다** — 매입 판단이 `PRE_PURCHASE`
  로 받은 경계를 실행 이력에 통째로 적어 둔다.

```text
master_agent_runs · cycle='PROCUREMENT' · response_payload.constraints
    inventory.warehouse_free_kg · inventory.rental_cap_kg
    inventory.inbound_lead_days · finance.finance_cap_amount_krw
```

  물류·재무 호출이 **0회**라 판매 예산이 안 늘어난다.

---

🔴 **`procurable_quantity_kg` 를 계산하지 않는다.** 나눗셈에 쓰는 단가가 **매입
  것**이다. 우리가 계산하면 단가가 바뀌는 날 두 값이 갈리고, 그때 어느 쪽이 참인지
  아무도 말해 주지 않는다. **재료만 준다.**

🔴 **없는 값을 `0` 으로 채우지 않는다.** `None` 이 *"못 읽었다"* 이고 `0.0` 은
  *"자리가 없다"* 다 — 매입이 `null ≠ 0` 을 계약으로 청했다.

  ⚠️ `rental_cap_kg` 는 실측이 실제로 `0.0` 이다 (물류 확정값). **그건 읽은 값이라
    `0.0` 이 맞다.** 못 읽은 것과 반드시 구별되어야 한다.

---

🔴 **`CAPABILITY_ROUTING["ADDITIONAL_SUPPLY_CONTEXT"]` 은 이 판에서 안 채운다.**

  회신은 왔지만 **매입 어댑터가 아직 그 mode 를 모른다** — `purchase_port` 가
  `STATUS_QUERY` 말고는 전부 `_generate_scenarios` 로 떨어뜨린다. 지금 채우면 판매
  사이클이 매입을 불러 **조용히 매입안을 만든다.** 근거는 `envelope.py` 의 그 자리에
  적혀 있다. 이 모듈은 **경계를 읽어 오는 자리**와 **못 읽을 때의 사유 어휘**까지다.

★ **봉투에 싣는 배선(`sales_flow.py`)도 이 판이 아니다.** 라우팅이 열리는 날 같이
  한다 — 지금 실어도 아무도 안 읽는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, get_args

from app.master.execution_day import HolidayCalendar, is_execution_day
from app.master.run_repository import MasterAgentRun, is_ledger_gap_request_id, list_runs

__all__ = [
    "ABSENT_REASONS",
    "AbsentReason",
    "ProcurementBoundary",
    "read_procurement_boundary",
]

#: 경계를 읽어 오는 축. 매입 판단 행이 앉는 사이클이다 (`persistence._CYCLE`).
_CYCLE = "PROCUREMENT"

#: 하루치 행을 읽는 상한. 실측(2026-09-09)으로 한 축·하루 최대가 21행이다
#: (같은 날을 여러 번 다시 걸으면 품목 셋이 그만큼 쌓인다).
#:
#: ⚠️ `list_runs` 의 기본값 50 에 기대지 않는다. 그 기본값은 저쪽 사정이라 바뀔 수
#:   있고, 바뀌는 날 이 모듈은 **오류 없이 옛 행을 최신으로 읽는다.**
_DAY_ROW_LIMIT = 200

AbsentReason = Literal[
    "NOT_EXECUTION_DAY",
    "LEDGER_GAP",
    "NO_PROCUREMENT_RUN",
]
"""경계를 **못 읽은 사유**. 셋을 가르는 것이 이 모듈의 값이다.

```text
NOT_EXECUTION_DAY    그날은 실행일이 아니라 매입 판단이 안 돈다 (토·일·공휴일)
LEDGER_GAP           장부 관문이 막아서 판단을 안 돌렸다
NO_PROCUREMENT_RUN   실행일이고 관문도 안 막았는데 그날 행이 없다
```

★★ **매입 `basis` 의 `unknown` 을 푸는 자리다.** 그 값은 *"마스터가 안 실었다"* 와
  *"실렸는데 계산이 안 됐다"* 를 뭉갠다. 이 사유가 옆에 있으면 화면이 **"토요일이라
  못 물어봤다"** 까지 말한다 — 셋을 한 낱말로 접으면 그 문장이 사라진다.

🔴 **`NO_PROCUREMENT_RUN` 이 나머지 둘의 쓰레기통이 되면 안 된다.** 그래서 판정
  순서를 `read_procurement_boundary` 가 못 박고, 그 순서를 검사가 잠근다."""

ABSENT_REASONS: frozenset[str] = frozenset(get_args(AbsentReason))
"""닫힌 집합. **주인은 위 `Literal` 하나다** — `get_args` 로 읽어 두 벌로 만들지
않는다 (봉투가 `TRIGGERS` 를 만든 것과 같은 자리)."""


@dataclass(frozen=True)
class ProcurementBoundary:
    """그날 매입 판단이 받아 둔 경계 — **재료 넷과 그 출처.**

    🔴 **`present` 와 값 넷이 어긋나면 성립하지 않는다.** 못 읽었다면서 값이 실려
      있으면 받는 쪽이 그 값을 쓴다. 아래 `__post_init__` 이 그것을 봉투처럼
      **만들 수 없게** 막는다 — 받아 보고 판정할 것이 아니라 애초에 나가면 안 되는
      모양이다 (`envelope.py` 의 두 층 중 앞쪽).
    """

    #: 경계를 읽었나.
    present: bool

    #: 못 읽었으면 왜. 읽었으면 `None`.
    absent_reason: AbsentReason | None = None

    #: 읽어 온 실행. **`run_id` 와 `as_of` 를 담는다** (아래 `_source_ref`).
    source_ref: str | None = None

    warehouse_free_kg: float | None = None
    rental_cap_kg: float | None = None
    finance_cap_amount_krw: int | None = None
    inbound_lead_days: int | None = None

    def __post_init__(self) -> None:
        if self.present:
            if self.absent_reason is not None:
                raise ValueError("경계를 읽었는데 못 읽은 사유가 붙었다 — 둘 중 하나가 거짓이다.")
            if not self.source_ref:
                raise ValueError(
                    "경계를 읽었는데 출처가 없다 — 그 경계가 어느 실행의 언제 것인지는"
                    " 숨기지 않는다 (§3.2)."
                )
            return
        if self.absent_reason not in ABSENT_REASONS:
            raise ValueError(
                f"absent_reason={self.absent_reason!r} 는 사유 어휘가 아니다."
                f" 허용: {sorted(ABSENT_REASONS)}"
            )
        # 🔴 **못 읽은 날에 값이 하나라도 실리면 그 값이 쓰인다.** `0` 으로 채우지
        #   않는 규율이 여기서 한 번 더 잠긴다.
        실린값 = {
            "warehouse_free_kg": self.warehouse_free_kg,
            "rental_cap_kg": self.rental_cap_kg,
            "finance_cap_amount_krw": self.finance_cap_amount_krw,
            "inbound_lead_days": self.inbound_lead_days,
        }
        남은것 = sorted(name for name, value in 실린값.items() if value is not None)
        if 남은것:
            raise ValueError(f"경계를 못 읽었는데 값이 실렸다: {', '.join(남은것)}")


def read_procurement_boundary(
    *,
    as_of: date,
    sim_run_id: str,
    calendar: HolidayCalendar | None = None,
) -> ProcurementBoundary:
    """그날 그 축의 매입 경계를 읽어 온다. **못 읽으면 사유를 낸다.**

    판정 순서 — **이 순서가 계약이다.**

    ```text
    ① 그날 그 축에 constraints 를 든 PROCUREMENT 행이 있나  → 있으면 읽고 끝
    ② 관문 행이 있나                                        → LEDGER_GAP
    ③ is_execution_day 가 거짓인가                          → NOT_EXECUTION_DAY
    ④ 그 밖                                                 → NO_PROCUREMENT_RUN
    ```

    🔴 **`①` 이 맨 앞이다.** 행이 있으면 왜 없는지 물을 필요가 없다. 뒤로 밀면
      토요일에 손으로 돌린 판단이 *"실행일이 아니라 못 읽었다"* 로 접힌다.

    ⚠️ **`③` 은 `execution_day.is_execution_day` 를 부른다.** 요일 판정을 여기서
      다시 짓지 않는다 — 같은 사실의 주인이 둘이 되면 갈린 날 아무도 어느 쪽이
      맞는지 말해 주지 않는다.

    ★ **`calendar` 를 안 주면 주말만 가른다** (`is_execution_day` 의 태도 그대로).
      공휴일에 물으면 그날은 `NO_PROCUREMENT_RUN` 으로 나온다 — 달력을 준 호출만
      `NOT_EXECUTION_DAY` 를 받는다.

    ★ **그 경계는 「그날 매입 판단 시점」의 것이다.** 하루 순서가 … → 판단(매입) →
      출고 라, 판단 뒤 출고가 나가면 창고가 바뀐다. 낮에 판매가 물으면 **아침
      값**이고, 실제 매입은 다음 실행일이다. 그래서 `source_ref` 를 반드시 채운다.

    :raises ValueError: `sim_run_id` 가 비었을 때. **빈 축을 조용히 전체로 바꾸지
        않는다** — 그러면 남의 걷기 경계가 이 판매 후보의 답으로 실린다
        (`run_repository.check_walk_scope` 와 같은 태도).
    :raises CalendarNotCovered: `calendar` 를 줬는데 그 날을 안 덮을 때. **평일로
        단정하지 않는다** — 잡아서 넘기면 달력이 끊긴 것과 실행일인 것이 같아진다.
        `execution_day` 가 *"부르는 쪽이 정한다"* 로 남긴 자리이고, `①` `②` 가 앞에
        있으므로 이 예외는 **답이 정말 달력에 걸릴 때만** 난다.
    """
    if not sim_run_id.strip():
        raise ValueError(
            "sim_run_id 없이 경계를 읽을 수 없다 — 어느 실행의 경계인지 없으면"
            " 물음이 성립하지 않는다"
        )

    rows = list_runs(
        cycle=_CYCLE,
        as_of=as_of,
        sim_run_id=sim_run_id,
        limit=_DAY_ROW_LIMIT,
    )

    # ★ **최신부터 본다 — 여기서 직접 세운다.** `list_runs` 가 이미 `created_at DESC`
    #   로 주지만, 그 정렬은 저쪽 사정이라 바뀔 수 있다. 바뀌는 날 이 모듈은 오류
    #   없이 **옛 경계를 오늘 답으로** 내보낸다.
    최신부터 = sorted(rows, key=lambda row: row["created_at"], reverse=True)

    # ① 그날 그 축에 constraints 를 든 행이 있나.
    #
    # ⚠️ **품목별로 값이 다를 수 있다** (실측 2026-09-09 · 축이 실린 15개 날 묶음 중
    #   5개에서 `warehouse_free_kg` 가 갈렸다). 갈리는 자리는 품목이 아니라 **같은 날을
    #   다시 걸은 회차**다 — 한 회차 안의 배추·무·양파는 실측 전부가 같은 값이다.
    #   그러니 최신 회차가 오늘의 사실이고, **어느 실행에서 왔는지는 `source_ref` 가
    #   숨김없이 말한다.**
    for row in 최신부터:
        constraints = _constraints_of(row)
        if constraints:
            return _boundary_from(row, constraints)

    # ② 관문 행이 있나 — **모양이 아니라 업무 키로 잡는다.**
    #
    # 🔴 `01-24` · `01-31` 의 `E4_NOT_STARTED` 여섯 행은 품목이 실린 **매입 행**이고
    #   (실측), 품목이 없는 채로 죽은 옛 매입 행도 14행 있다. 모양으로 잡으면 그것들이
    #   관문으로 읽혀 없는 *"장부가 막았다"* 가 생긴다. 관문 행에는 그 행만의 업무 키가
    #   있고 (`run_repository.ledger_gap_request_id`), 적는 쪽이 그 키를 적는다.
    if any(_is_ledger_gap_row(row) for row in 최신부터):
        return ProcurementBoundary(present=False, absent_reason="LEDGER_GAP")

    # ③ 실행일이 아닌가. **요일 판정을 다시 짓지 않는다.**
    if not is_execution_day(as_of, calendar=calendar):
        return ProcurementBoundary(present=False, absent_reason="NOT_EXECUTION_DAY")

    # ④ 실행일이고 관문도 안 막았는데 행이 없다. **운영 공백이다.**
    return ProcurementBoundary(present=False, absent_reason="NO_PROCUREMENT_RUN")


# ---------------------------------------------------------------------------
# 안쪽 — 행 하나에서 무엇을 꺼내나
# ---------------------------------------------------------------------------


def _constraints_of(row: MasterAgentRun) -> Mapping[str, Any]:
    """그 행이 든 부서별 경계. **없으면 빈 매핑이다.**

    ★ 빈 매핑이 *"경계를 안 든 행"* 이다 — 관문 행·조회 행이 여기로 떨어진다.
    """
    payload = row.get("response_payload") or {}
    if not isinstance(payload, Mapping):
        return {}
    constraints = payload.get("constraints")
    return constraints if isinstance(constraints, Mapping) and constraints else {}


def _is_ledger_gap_row(row: MasterAgentRun) -> bool:
    """장부 관문 행인가 — **업무 키로 알아본다** (`#465` · 2026-09-09 에 키로 바꿨다).

    ⚠️ **옛 판정은 모양이었다** — `item IS NULL AND end_code == 'E4_NOT_STARTED'`.
      그런데 **그 모양은 관문 행만의 것이 아니다** (실측 2026-09-09).

      ```text
      PROCUREMENT           1,315행
        E4_NOT_STARTED        404행
          item IS NULL          14행   ← 품목을 정하기 전에 죽은 옛 매입 실행이다
                                        (*"경계를 내지 못한 에이전트: finance"*)
      ```

      그 14행이 안 샜던 이유는 **전부 `sim_run_id` 가 `NULL`** 이라 축을 좁히는 이
      함수에 한 행도 안 걸렸기 때문이다. **축이 막고 있었던 것이지 모양이 스스로를
      증명한 것이 아니다.** 축이 실린 채로 품목 전에 죽는 실행이 한 번만 나오면
      그날이 *"장부가 막았다"* 로 잘못 읽힌다.

    🟢 **그래서 업무 키로 바꿨다.** 관문 행에는 그 행만의 키가 있다 —
      `run_repository.ledger_gap_request_id(as_of)` 가 짓고
      `persistence.record_ledger_gap` 이 `request_id` 로 적는다. **적는 쪽과
      되찾는 쪽이 같은 함수를 본다.**

    ★ **꼬리 문자열을 여기 다시 적지 않는다.** 주인은 `run_repository` 하나이고,
      `count_runs_by_day` 의 `gate_blocked` 도 같은 꼬리로 그 행을 되찾는다 —
      두 벌로 적으면 한쪽만 바뀌는 날 화면과 성적표가 조용히 갈린다.

    ★ **`end_code` 를 같이 보지 않는다.** 종료 코드는 *"시작 못 했다"* 이고 그것은
      관문 행만의 사실이 아니다. 같은 사실을 두 칸으로 물으면 한 칸이 바뀌는 날
      판정이 이유 없이 조용해진다.
    """
    return is_ledger_gap_request_id(row.get("request_id"))


def _boundary_from(row: MasterAgentRun, constraints: Mapping[str, Any]) -> ProcurementBoundary:
    """읽은 행 하나를 경계로. **부서 칸을 그대로 옮긴다 — 계산하지 않는다.**"""
    inventory = _dept(constraints, "inventory")
    finance = _dept(constraints, "finance")
    return ProcurementBoundary(
        present=True,
        source_ref=_source_ref(row),
        warehouse_free_kg=_read_float(inventory, "warehouse_free_kg"),
        rental_cap_kg=_read_float(inventory, "rental_cap_kg"),
        finance_cap_amount_krw=_read_int(finance, "finance_cap_amount_krw"),
        inbound_lead_days=_read_int(inventory, "inbound_lead_days"),
    )


def _dept(constraints: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = constraints.get(name)
    return value if isinstance(value, Mapping) else {}


def _source_ref(row: MasterAgentRun) -> str:
    """*"이 경계는 어느 실행의 언제 것"* 한 줄. **숨기지 않는다.**

    🔴 **`run_id` 만으로는 부족하다.** 그 경계는 그날 **매입 판단 시점**의 것이고,
      판단 뒤 출고가 나가면 창고가 바뀐다 — 언제 것인지가 답에 따라가야 낮에 받은
      값이 아침 값이라는 것을 사람이 안다.
    """
    return f"master_agent_runs/{row['run_id']}@{row['as_of'].isoformat()}"


def _read_float(payload: Mapping[str, Any], key: str) -> float | None:
    """숫자 칸 하나. **없으면 `None` — `0.0` 으로 안 채운다.**

    🔴 `0.0` 은 *"자리가 없다"* 라는 **읽은 값**이다 (물류 `rental_cap_kg` 이 실제로
      그렇다). 못 읽은 것을 그 값으로 채우면 두 사실이 화면에서 같아진다.

    ★ `bool` 을 숫자로 안 센다 — 파이썬에서 `True` 는 `int` 라 그냥 두면 `1.0` 이 된다.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _read_int(payload: Mapping[str, Any], key: str) -> int | None:
    """정수 칸 하나. **없으면 `None`.**

    ⚠️ 실측에 `finance_cap_amount_krw` 가 `168012`(int) 로도 `31854627.0`(float) 로도
      온다 — 같은 칸이 경로에 따라 두 모양이다. 원 단위라 실측 전부가 정수라서
      `round` 가 값을 움직이지 않는다. **모양만 맞추고 값은 안 고친다.**
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value)
