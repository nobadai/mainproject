"""마스터가 실어 주는 입력 3종 — **어디서 왔는지를 값과 함께 들고 다닌다.**

정의서 §3.2.5 의 명시적 예외다. 이 셋은 *"해당 에이전트에게 요청"* 이 성립하지 않아
마스터가 직접 싣는다.

```text
forecast          ML 은 호출 구조 밖 독립 실행이라 부를 대상이 없다
confirmed_orders  1차 판매는 에이전트가 아니라 마스터 관할 Rule 이다
policy_values     정책 테이블 — 운반 주체 미결 (M-19)
```

★ **값만 싣지 않고 `SourcedInput` 으로 싣는다.** 같은 `forecast` 라도 오늘 실제 DB 에서
  읽은 것과 mock 파일에서 온 것은 **의사결정의 무게가 다르다.** 값만 넘기면 그 차이가
  사라지고, 리포트를 읽는 사람은 전부 실측으로 읽는다. §3.7.6("못 한 것을 한 척하지
  않는다")이 검증 커버리지에 대해 말하는 것과 같은 이야기다.

★ **비어 있으면 지어내지 않는다.** 못 읽으면 `MISSING` 으로 두고 매입이
  `missing_data` 로 답하게 한다 — 0 이나 평균값으로 메우면 **그럴듯하게 틀린 계획**이
  나온다.

🟢 **`MOCK` 다리를 걷었다** (2026-09-03).

  앵커가 어긋나 있던 동안(`M-24`) `forecast` 가 mock 으로 떨어졌다. `D-2` 가
  `2025-12-31` 로 확정되면서 세 품목 전부 실 예측이 선다 — 실측으로 확인했다.

  .. code-block:: text

      배추 · 무 · 양파   grade=MEASURED   v_ml_price_forecast(as_of=2025-12-31, AUC)

🔴 **그리고 다시 놓지 않는다.** ML DB 가 죽었는데 mock 으로 돌면 **장애가 정상으로
  보인다.** 그 갈래가 마스터 실측을 두 번 오염시켰다 (2026-08-31 · 09-03 피마늘).

  이제 못 읽으면 `MISSING` 이고, 매입이 `missing_data: ["forecast"]` 로
  `RUNTIME_NOT_READY` 를 낸다. **못 한 것이 한 것으로 안 보인다.**

★ `MOCK` 은 어휘에 남긴다. 만드는 곳이 지금은 없지만, 새 다리가 생기면 그것이
  스스로 `MOCK` 이라고 말할 자리가 있어야 하고 그때 `ProcurementFlow` 가 세운다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from psycopg import sql

from app.finance.db import fetch_all, fetch_one, get_db_schema

#: 값 하나의 출처 등급. **리포트에 그대로 나간다.**
Grade = Literal[
    "MEASURED",  # 실제 운영 DB 에서 그대로 읽었다
    "DERIVED",  # 실제 DB 값에서 규칙으로 파생했다 — 원식을 함께 남긴다
    "MOCK",  # 🔴 mock 파일에서 왔다 — 한시 조치
    "MISSING",  # 못 구했다. 지어내지 않고 비운다
]

#: 확정 주문을 내다볼 기간. 매입 ③이 `total_kg ÷ order_window_days` 로 일수요를 낸다.
_ORDER_WINDOW_DAYS = 14


@dataclass(frozen=True)
class SourcedInput:
    """값 + 출처. **둘을 떼어 놓지 않는다.**"""

    key: str
    payload: Any | None
    grade: Grade
    source: str
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.payload is not None and self.grade != "MISSING"

    def line(self) -> str:
        tail = f" — {self.note}" if self.note else ""
        return f"{self.key} [{self.grade}] {self.source}{tail}"


@dataclass(frozen=True)
class MasterInputs:
    """한 실행이 실어 주는 것 전부."""

    forecast: SourcedInput
    confirmed_orders: SourcedInput
    policy_values: SourcedInput

    def all(self) -> tuple[SourcedInput, ...]:
        return (self.forecast, self.confirmed_orders, self.policy_values)

    def sources(self) -> dict[str, str]:
        """리포트·응답에 싣는 출처표."""
        return {s.key: f"{s.grade}:{s.source}" for s in self.all()}

    @property
    def mocked(self) -> tuple[str, ...]:
        """🔴 mock 에서 온 것. **감추지 않고 위로 올린다.**"""
        return tuple(s.key for s in self.all() if s.grade == "MOCK")


def collect_inputs(item: str, as_of: date) -> MasterInputs:
    """세 입력을 모은다. **하나가 실패해도 나머지는 싣는다.**"""
    return MasterInputs(
        forecast=load_forecast(item, as_of),
        confirmed_orders=load_confirmed_orders(item, as_of),
        policy_values=load_policy_values(item, as_of),
    )


# ── forecast ────────────────────────────────────────────────────────────


def load_forecast(item: str, as_of: date) -> SourcedInput:
    """ML 예측. **`generated_at <= as_of` 인 최신 배치만 본다.**

    ★ 미래 배치를 집으면 백테스트 성적이 통째로 무효가 된다 (look-ahead).
      뷰가 `as_of` 컬럼을 갖고 있으므로 그 이하만 고른다.
    """
    try:
        row = _forecast_from_db(item, as_of)
    except Exception as error:  # noqa: BLE001 — 적재 실패가 Flow 를 죽이면 안 된다
        return _forecast_missing(f"DB 조회 실패 ({error})")
    if row is None:
        return _forecast_missing(f"{as_of} 이전 예측 배치가 없다")
    return SourcedInput(
        key="forecast",
        payload=_forecast_payload(row),
        grade="MEASURED",
        source=f"v_ml_price_forecast(as_of={row['as_of']}, {row['target_kind']})",
        note=str(row.get("quality_note") or ""),
    )


def _forecast_from_db(item: str, as_of: date) -> dict[str, Any] | None:
    query = sql.SQL("""
        SELECT * FROM {}.v_ml_price_forecast
         WHERE item = %s AND as_of <= %s AND target_kind = 'AUC'
         ORDER BY as_of DESC
         LIMIT 1
    """).format(sql.Identifier(get_db_schema()))
    row = fetch_one(query, (item, as_of))
    return dict(row) if row else None


def _forecast_payload(row: dict[str, Any]) -> dict[str, Any]:
    """뷰 행을 매입이 받는 형태로. **키를 고르기만 하고 값은 손대지 않는다.**

    🔴 **`use_recommended` 를 더했다** (2026-09-03 · 매입 `#192`).

      ML 이 신뢰도 플래그 셋을 붙여 보내는데 매입이 하나도 안 읽고 있었다.
      매입은 *"payload 에 칸이 없어서 못 읽는다"* 로 진단했는데 **절반만 맞았다.**

      ```text
      is_filled · is_gated   행별   뷰가 daily[] 안에 넣어 이미 간다
      use_recommended        조합별  여기서 버리고 있었다
      ```

    ★ **`daily` 안의 둘은 손대지 않는다.** 뷰가 `jsonb_build_object` 로 넣은
      그대로 나른다 — 마스터가 풀어 다시 조립하면 ML 이 준 모양이 바뀐다.

    ⚠️ 아직 안 나르는 것이 셋 있다 — `has_filled_rows` · `filled_count` ·
      `quality_note`. 앞 둘은 `daily` 에서 셀 수 있는 파생이고, `quality_note` 는
      사람이 읽는 문장이라 `SourcedInput.note` 로 이미 화면에 간다.
      **읽겠다는 파트가 생기면 그때 더한다.**
    """
    return {
        "generated_at": row["generated_at"],
        "item": row["item"],
        "unit": row["unit"],
        "current_price": _plain(row["current_price"]),
        "horizon_days": _plain(row["horizon_days"]),
        "daily": row["daily"],
        "model_version": row["model_version"],
        "use_recommended": row.get("use_recommended"),
    }


def _forecast_missing(why: str) -> SourcedInput:
    """🔴 **못 읽으면 비운다. mock 으로 메우지 않는다** (2026-09-03).

    전에는 여기서 `app.purchase_agent.mocks` 를 집어 왔다. 그러면 ML DB 장애가
    **정상 실행처럼** 보인다 — 매입이 안을 만들고 세 부서가 판정하고 `E1_APPROVED`
    까지 간다. 사람이 `input_sources` 를 읽지 않으면 아무도 모른다.

    ★ 비우면 매입이 `missing_data: ["forecast"]` 로 `RUNTIME_NOT_READY` 를 낸다.
      **없는 것과 못 만든 것을 가르는 것**이 이 프로젝트의 §1.2-10 이다.
    """
    return SourcedInput(key="forecast", payload=None, grade="MISSING", source="-", note=why)


# ── confirmed_orders ────────────────────────────────────────────────────


def load_confirmed_orders(item: str, as_of: date) -> SourcedInput:
    """향후 납품 예정. **실제 주문이 있으면 그것을, 없으면 파트너 수요에서 파생한다.**

    🔴 **파생분을 "확정 주문" 이라 부르지 않는다.** `sales` 에 앞으로 납품할 건이
      0건이라(전부 `DELIVERED`) 파트너 일수요로 메우는데, 그건 **예상 수요이지 확정이
      아니다.** 등급을 `DERIVED` 로 두고 파생식을 `note` 에 적어 리포트에 내보낸다 —
      값만 넘기면 매입도 사람도 확정으로 읽는다.
    """
    try:
        booked = _orders_from_db(item, as_of)
    except Exception as error:  # noqa: BLE001
        booked = None
        why = f"DB 조회 실패 ({error})"
    else:
        why = "앞으로 납품할 확정 건이 없다"

    if booked:
        return SourcedInput(
            key="confirmed_orders",
            payload=booked,
            grade="MEASURED",
            source="sales + sale_items",
            note=f"{as_of} 이후 {_ORDER_WINDOW_DAYS}일 납품 예정",
        )

    try:
        return _orders_from_demand(item, as_of, why)
    except Exception as error:  # noqa: BLE001
        return SourcedInput(
            key="confirmed_orders",
            payload=None,
            grade="MISSING",
            source="-",
            note=f"{why} · 파생도 실패 ({error})",
        )


def _orders_from_db(item: str, as_of: date) -> dict[str, Any] | None:
    query = sql.SQL("""
        SELECT s.sale_id, s.sale_date, si.quantity_kg
          FROM {sch}.sales s
          JOIN {sch}.sale_items si ON si.sale_id = s.sale_id
          JOIN {sch}.items i ON i.item_id = si.item_id
         WHERE i.item_name = %s
           AND s.sale_date > %s
           AND s.sale_date <= %s
         ORDER BY s.sale_date
    """).format(sch=sql.Identifier(get_db_schema()))
    rows = fetch_all(query, (item, as_of, as_of + timedelta(days=_ORDER_WINDOW_DAYS)))
    if not rows:
        return None
    orders = [
        {
            "sale_id": r["sale_id"],
            "qty_kg": _plain(r["quantity_kg"]),
            "due_date": r["sale_date"].isoformat(),
        }
        for r in rows
    ]
    return {
        "as_of": as_of.isoformat(),
        "item": item,
        "orders": orders,
        "total_kg": sum(o["qty_kg"] for o in orders),
    }


def _orders_from_demand(item: str, as_of: date, why: str) -> SourcedInput:
    """파트너 일수요 × 기간. **주문 주기 간격으로 쪼갠다.**

    ⑤ 노드가 `due_date` 별 분포로 등급-신선도를 맞추므로 총량 한 덩어리로 주면
    "전량을 첫날 납품" 으로 읽힌다. 주기(`order_cycle_days`)를 그대로 쓴다.
    """
    schema = sql.Identifier(get_db_schema())
    demand = fetch_one(
        sql.SQL("""
            SELECT d.daily_demand_kg, d.demand_basis, d.provisional
              FROM {sch}.partner_item_demands d
              JOIN {sch}.items i ON i.item_id = d.item_id
             WHERE i.item_name = %s
        """).format(sch=schema),
        (item,),
    )
    if demand is None:
        raise LookupError(f"{item} 파트너 일수요가 없다")

    cycle_row = fetch_one(
        sql.SQL("SELECT order_cycle_days FROM {sch}.v_current_partner_demand LIMIT 1").format(
            sch=schema
        ),
        (),
    )
    cycle = int(cycle_row["order_cycle_days"]) if cycle_row else 1
    cycle = max(1, cycle)

    daily = _plain(demand["daily_demand_kg"])
    orders = [
        {
            "sale_id": None,  # 실제 주문이 아니다 — id 를 지어내지 않는다
            "qty_kg": round(daily * cycle, 1),
            "due_date": (as_of + timedelta(days=offset)).isoformat(),
        }
        for offset in range(cycle, _ORDER_WINDOW_DAYS + 1, cycle)
    ]
    return SourcedInput(
        key="confirmed_orders",
        payload={
            "as_of": as_of.isoformat(),
            "item": item,
            "orders": orders,
            "total_kg": round(sum(o["qty_kg"] for o in orders), 1),
        },
        grade="DERIVED",
        source="partner_item_demands · v_current_partner_demand",
        note=(
            f"{why} → 일수요 {daily}kg × {_ORDER_WINDOW_DAYS}일, 주기 {cycle}일로 분할 "
            f"({demand['demand_basis']}"
            f"{', 잠정값' if demand['provisional'] else ''}) · 확정 주문이 아니다"
        ),
    )


# ── policy_values ───────────────────────────────────────────────────────


def load_policy_values(item: str, as_of: date) -> SourcedInput:
    """매입이 쓰는 정책값.

    ★ `item_mix_ratio` 는 **파트너 일수요에서 파생**한다. 정책 테이블에 그 키가 없고,
      쓰임이 *"한 품목이 임계 이상을 차지하면 mix 축을 닫는다"* 라 **품목별 수요
      비중**이 바로 그 뜻이다. 파생식을 `note` 에 남긴다.

    ⚠️ `contract_price_krw` 는 **비운다.** 계약 단가가 DB 에 없고, 매입 계약상
      필수가 아니다 — 없으면 `margin_warning` 이 `null` 로 나가는 것이 정상 경로다.
      **평균값으로 메우면 마진 경고가 조용히 틀린다.**
    """
    del as_of  # 정책은 현재 유효분 하나뿐이다 (버전 축은 policy_version 이 갖는다)
    try:
        ratios = _mix_ratio_from_demand()
    except Exception as error:  # noqa: BLE001
        return SourcedInput(
            key="policy_values", payload=None, grade="MISSING", source="-", note=str(error)
        )
    if not ratios:
        return SourcedInput(
            key="policy_values",
            payload=None,
            grade="MISSING",
            source="partner_item_demands",
            note="품목별 일수요가 없어 비중을 낼 수 없다",
        )

    payload: dict[str, Any] = {"item_mix_ratio": ratios}
    return SourcedInput(
        key="policy_values",
        payload=payload,
        grade="DERIVED",
        source="partner_item_demands",
        note=(
            f"item_mix_ratio = 품목 일수요 ÷ 전체 일수요 "
            f"({item} {ratios.get(item, 0):.3f}) · contract_price 는 DB 에 없어 비움"
        ),
    )


def _mix_ratio_from_demand() -> dict[str, float]:
    rows = fetch_all(
        sql.SQL("""
            SELECT i.item_name, d.daily_demand_kg
              FROM {sch}.partner_item_demands d
              JOIN {sch}.items i ON i.item_id = d.item_id
        """).format(sch=sql.Identifier(get_db_schema())),
        (),
    )
    total = sum(_plain(r["daily_demand_kg"]) for r in rows)
    if not total:
        return {}
    return {r["item_name"]: round(_plain(r["daily_demand_kg"]) / total, 4) for r in rows}


# ── 값 정리 ─────────────────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    """`Decimal` 을 파이썬 수로. **정수는 정수로 남긴다.**

    매입 계약이 `qty_kg` 를 정수로 받는 자리가 있어, 무조건 `float` 로 바꾸면
    소수/정수 불일치가 거기서 터진다.
    """
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    return value
