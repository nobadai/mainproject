"""transport.py — 계약 baseline 기반 고정 운송 견적 (3-E2). **읽기와 계산만 한다.**

```text
출고 확정 수량
   → resolve_fixed_route      계약이 정해 둔 거리·차량·운임을 읽는다
   → select_vehicle           실을 수 있는 가장 작은 차량
   → plan_fixed_route_transport   trip 수 · 운임 합계
```

🔴 **여기서 "Route" 는 지도 위의 경로가 아니다.** 출발지·목적지·경유지를 가진 운송
   Route entity 가 이 시스템에 **없다**(아래 실측). 이 파일이 말하는 *"고정"* 은
   **물류계약이 미리 고정해 둔 거리와 운임**이라는 뜻이고, 그 위에서 수량만 보고
   차량과 횟수를 정하는 **결정론적 견적**이다.

```text
이 파일이 하는 것      계약 거리 30km + 수량 → 차량 · trip · 운임
이 파일이 아닌 것      A 지점에서 B 지점까지 어느 길로 갈 것인가
```

🔴 **이 파일은 아무것도 쓰지 않는다.** 운송계획은 *"이렇게 실으면 이만큼 든다"* 는
   계산이지 사실의 기록이 아니다.

```text
remaining_qty_kg 변경   ❌
Inventory Move          ❌
Allocation 상태 변경     ❌
```

  ★ 실제 재고 감소는 이미 `outbound.ship_allocated_stock` → 원장 OUT 이 한다.
    운송이 그 일을 다시 하지 않는다.

🔴 **Shipment 표를 새로 만들지 않는다.** 실출고 사실은 여전히
   *할당 `SHIPPED` + 원장 OUT* 이다. 기존 `deliveries` 표가 배송 실행단위를 갖고
   있지만 그쪽은 `sale_id` · `customer_partner_id` 를 FK 로 요구하는 **판매 쪽 표**라,
   물류가 임의로 줄을 만들지 않는다 (아래 연계 메모).

★ **스키마 실측 (2026-09-05 · 실 PostgreSQL 카탈로그).** 세 표가 각각 다른 것의 정본이다.

```text
logistics_contracts   거리·계약 baseline 의 정본   1행 (LOGI-BASE-5PL)
    delivery_distance_km 30.000 · vehicle_class '2.5t 냉장/냉동'
    transport_cost_per_delivery_krw 130,000 · contract_status 'BASELINE_ONLY'

vehicle_specs         차량 제원의 정본             3행 (PK vehicle_class)
    1t   REEFER  max 1000 · 운영 800  · floor 2
    1.4t REEFER  max 1400 · 운영 1200 · floor 2
    2.5t REEFER  max 2500 · 운영 2000 · floor 3

vehicle_rate_table    거리구간 운임의 정본         12행
    (vehicle_class, body_type, distance_from_km, distance_to_km) → base_rate_krw
    구간은 from 초과 ~ to 이하다 (DDL 주석: "(0,11] = 문서의 ~11km")
```

  🔴 **`routes` 표는 없다.** 저장소 DDL 에도 실 DB 카탈로그에도 `route` · `origin` ·
     `destination` · `standard_minutes` 이름의 표나 칸이 하나도 없다(실측). 그래서
     이 판의 *"고정 Route"* 는 **계약이 고정해 둔 거리**를 뜻한다 — 없는 표를
     지어내지 않는다.

  ⚠️ **운송 소요시간의 정본이 없다.** `standard_minutes` 에 해당하는 칸이 어디에도
     없어서 `standard_minutes=None`(UNRESOLVED)로 돌려준다. 🔴 지도 API·평균속도·
     거리÷속도 같은 것으로 **분을 지어내지 않는다.**

  ⚠️ **차량 어휘가 두 표에서 갈린다.**

  ```text
  logistics_contracts.vehicle_class  '2.5t 냉장/냉동'   ← 한글 표시문자열
  vehicle_specs.vehicle_class        '2.5t'            ← 제원 PK
  ```

     둘 사이에 FK 도 매핑 표도 **없다**(실측). 그래서 계약의 차량 문자열을
     `vehicle_specs` 로 **번역하지 않는다** — 계약값은 `contract_vehicle_class` 로
     날것 그대로 돌려주고, 실을 차량은 `vehicle_specs` 에서 수량으로 고른다.

  ⚠️ **계약 baseline 운임과 구간 운임표가 서로 다른 답을 준다.**

  ```text
  계약     거리 30km · 130,000원/회
  운임표   2.5t · 30km → (26,36] 구간 → 140,000원/회
  ```

     실측된 불일치다. 조용히 한쪽으로 맞추지 않는다 — 계산은 **운임표**로 하고
     계약값은 `contract_baseline_cost_krw` 로 함께 돌려주어 호출자가 본다.

🔴 **하지 않는 것.** 지도 API · 실시간 교통 · GPS · 최단경로 · 다중 Stop 최적화 ·
   기사 배차 · 유류비 · 톨게이트. 거리도 단가도 시간도 **코드에 숫자를 박지 않는다** —
   전부 위 세 표에서 읽는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "AmbiguousRate",
    "AmbiguousRoute",
    "FixedRoute",
    "InvalidTransportRequest",
    "RateNotFound",
    "RouteNotFound",
    "TransportError",
    "TransportPlan",
    "VehicleSpec",
    "VehicleTooLargeToSplit",
    "load_vehicle_specs",
    "plan_fixed_route_transport",
    "resolve_fixed_route",
    "select_vehicle",
    "trip_count_for",
]

_AMBIGUITY_PROBE_LIMIT = 2


class TransportError(RuntimeError):
    """이 모듈이 내는 실패의 조상."""


class InvalidTransportRequest(TransportError, ValueError):
    """요청이 계약을 어긴다. **DB 를 보기 전에 막는다.**"""


class RouteNotFound(TransportError, LookupError):
    """이 조건에 맞는 고정 Route 계약이 **없다.**"""


class AmbiguousRoute(TransportError, LookupError):
    """고정 Route 계약이 **둘 이상이다.** 🔴 자동으로 하나를 고르지 않는다."""


class RateNotFound(TransportError, LookupError):
    """이 차량·거리에 해당하는 운임 구간이 **없다.** 값을 지어내지 않는다."""


class AmbiguousRate(TransportError, LookupError):
    """운임 구간이 **겹친다.** 🔴 싼 쪽·비싼 쪽을 임의로 고르지 않는다."""


class VehicleTooLargeToSplit(TransportError, ValueError):
    """가장 큰 차량으로도 못 싣는데 나눠 실을 수도 없다."""


@dataclass(frozen=True)
class VehicleSpec:
    """`vehicle_specs` 한 줄. **제원 그대로다.**"""

    vehicle_class: str
    body_type: str
    max_payload_kg: Decimal
    #: 🔴 차량 선택은 **이 값**으로 한다. 명목 최대적재량이 아니라 보수적 운영 Payload 다
    #: (DDL 주석: *"명목 최대적재량이 아니라 보수적인 내부 운영 Payload"*).
    operational_payload_kg: Decimal
    max_pallet_floor_count: int | None


@dataclass(frozen=True)
class FixedRoute:
    """계약이 고정해 둔 운송 조건. **지도 경로가 아니라 계약 거리다.**"""

    logistics_contract_id: str
    distance_km: Decimal
    #: 계약의 차량 문자열. 🔴 `vehicle_specs` 로 번역하지 않은 **날것**이다.
    contract_vehicle_class: str
    #: 계약이 적어 둔 회당 운임. 계산에는 안 쓰고 대조용으로만 돌려준다.
    contract_baseline_cost_krw: Decimal
    contract_status: str
    provisional: bool


@dataclass(frozen=True)
class TransportPlan:
    logistics_contract_id: str
    distance_km: Decimal
    vehicle_class: str
    body_type: str
    vehicle_operational_payload_kg: Decimal
    shipment_qty_kg: Decimal
    trip_count: int
    #: 회당 운임 (`vehicle_rate_table.base_rate_krw`).
    fixed_fee_per_trip_krw: Decimal
    estimated_cost_krw: Decimal
    #: 🔴 **정본이 없다.** 스키마 어디에도 소요시간 칸이 없어 항상 `None` 이다.
    standard_minutes: int | None
    #: 계약이 적어 둔 회당 운임. 운임표와 다를 수 있다 (모듈 docstring 참고).
    contract_baseline_cost_krw: Decimal
    contract_vehicle_class: str


# ── 순수 계산 ───────────────────────────────────────────────────────────


def trip_count_for(*, shipment_qty_kg: Decimal, payload_kg: Decimal) -> int:
    """몇 번 실어야 하나. 순수 계산이다.

    ```text
    ceil(shipment_qty_kg / payload_kg)
    ```

    ★ 올림이다 — 남은 100kg 을 두고 갈 수 없다.
    """
    수량 = _quantity(shipment_qty_kg, 칸="shipment_qty_kg")
    적재 = _quantity(payload_kg, 칸="payload_kg")
    return math.ceil(수량 / 적재)


def select_vehicle(
    specs: Any, *, shipment_qty_kg: Decimal, body_type: str | None = None
) -> tuple[VehicleSpec, int]:
    """**실을 수 있는 가장 작은 차량**과 필요한 trip 수. 순수 계산이다.

    ```text
    운영 Payload 800 · 1200 · 2000 일 때
    qty  900  → 1200 짜리 · 1 trip
    qty 1200  → 1200 짜리 · 1 trip      ← 경계는 "이하" 다
    qty 1201  → 2000 짜리 · 1 trip
    qty 5000  → 2000 짜리 · 3 trip      ← 가장 큰 차로 나눠 싣는다
    ```

    🔴 **한 대로 되면 큰 차를 부르지 않는다.** 운임이 차량 등급마다 다르므로 과대
       배차는 그대로 비용이다.

    ⚠️ **`operational_payload_kg` 로 고른다.** `max_payload_kg` 는 명목값이라 그걸로
       고르면 운영 한도를 넘겨 싣는 계획이 나온다.

    :param body_type: 주면 그 차체만 본다. 안 주면 전부 본다 — 🔴 냉장이 필요한지는
        물류가 정하지 않는다. 호출자가 안 정했으면 좁히지 않는다.
    """
    수량 = _quantity(shipment_qty_kg, 칸="shipment_qty_kg")
    후보 = [s for s in specs if body_type is None or s.body_type == body_type]
    if not 후보:
        raise RouteNotFound(
            f"차량 제원이 없다 (body_type={body_type!r})."
            " vehicle_specs 에 줄이 없으면 계획을 세우지 않는다."
        )
    # ★ 작은 순서로 본다. 동률이면 `vehicle_class` 로 갈라 **결정론**을 지킨다.
    순서 = sorted(후보, key=lambda s: (s.operational_payload_kg, s.vehicle_class))
    for spec in 순서:
        if spec.operational_payload_kg >= 수량:
            return spec, 1
    가장큰 = 순서[-1]
    trips = trip_count_for(shipment_qty_kg=수량, payload_kg=가장큰.operational_payload_kg)
    if trips < 1:
        raise VehicleTooLargeToSplit(
            f"나눠 실을 횟수를 셀 수 없다 (수량 {수량} · 최대 {가장큰.operational_payload_kg})."
        )
    return 가장큰, trips


def _quantity(값: Any, *, 칸: str) -> Decimal:
    """수량을 `Decimal` 로 좁힌다. **float 도 비유한값도 받지 않는다.**

    ★ `ledger._quantity` · `outbound._quantity` · `warehouse._quantity` 와 같은 규율이다.
    """
    if isinstance(값, bool) or not isinstance(값, Decimal):
        raise InvalidTransportRequest(
            f"{칸} 은 Decimal 이어야 한다 (받은 것: {값!r} · {type(값).__name__})."
        )
    if not 값.is_finite():
        raise InvalidTransportRequest(f"{칸} 이 유한한 수가 아니다: {값!r}")
    if 값 <= 0:
        raise InvalidTransportRequest(f"{칸} 은 0보다 커야 한다 (받은 것: {값})")
    return 값


def _require_text(값: Any, *, 칸: str) -> str:
    if not isinstance(값, str) or not 값.strip():
        raise InvalidTransportRequest(f"{칸} 가 비었다: {값!r}")
    return 값


def _cell(row: Any, index: int, name: str) -> Any:
    return row[name] if isinstance(row, dict) else row[index]


# ── 읽기 ────────────────────────────────────────────────────────────────


def load_vehicle_specs(conn: Any, *, body_type: str | None = None) -> tuple[VehicleSpec, ...]:
    """차량 제원을 읽는다. **정본은 `vehicle_specs` 하나다.**"""
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT vehicle_class, body_type, max_payload_kg,
                       operational_payload_kg, max_pallet_floor_count
                FROM {}.vehicle_specs
                WHERE (%s::text IS NULL OR body_type = %s)
                ORDER BY operational_payload_kg, vehicle_class
                """
            ).format(schema),
            (body_type, body_type),
        )
        행들 = list(cursor.fetchall())
    return tuple(
        VehicleSpec(
            vehicle_class=str(_cell(행, 0, "vehicle_class")),
            body_type=str(_cell(행, 1, "body_type")),
            max_payload_kg=Decimal(str(_cell(행, 2, "max_payload_kg"))),
            operational_payload_kg=Decimal(str(_cell(행, 3, "operational_payload_kg"))),
            max_pallet_floor_count=(
                None
                if _cell(행, 4, "max_pallet_floor_count") is None
                else int(_cell(행, 4, "max_pallet_floor_count"))
            ),
        )
        for 행 in 행들
    )


def resolve_fixed_route(conn: Any, *, logistics_contract_id: str | None = None) -> FixedRoute:
    """운송 조건을 고정해 둔 계약 하나를 확정한다. **0 / 1 / 2+ 를 셋 다 다르게 다룬다.**

    ```text
    0개    → RouteNotFound
    1개    → 그것을 쓴다
    2개+   → AmbiguousRoute       🔴 자동으로 고르지 않는다
    ```

    ★ `logistics_contract_id` 를 주면 그 줄만 본다 — 계약이 여럿이 되는 날, 어느 것을
      쓸지는 **호출자가 정한다.**

    ⚠️ **거리가 비어 있으면 계획을 세우지 않는다.** `delivery_distance_km` 는
       nullable 이고, 없는 거리로는 운임 구간을 고를 수 없다. 0 으로 보정하면 가장
       싼 구간이 조용히 선택된다.
    """
    schema = sql.Identifier(get_db_schema())
    if logistics_contract_id is not None:
        _require_text(logistics_contract_id, 칸="logistics_contract_id")
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT logistics_contract_id, delivery_distance_km, vehicle_class,
                       transport_cost_per_delivery_krw, contract_status, provisional
                FROM {}.logistics_contracts
                WHERE (%s::text IS NULL OR logistics_contract_id = %s)
                ORDER BY logistics_contract_id
                LIMIT %s
                """
            ).format(schema),
            (logistics_contract_id, logistics_contract_id, _AMBIGUITY_PROBE_LIMIT),
        )
        행들 = list(cursor.fetchall())
    if not 행들:
        raise RouteNotFound(
            f"고정 Route 계약이 없다 (logistics_contract_id={logistics_contract_id!r})."
            " 거리·운임의 정본이 없으면 계획을 세우지 않는다."
        )
    if len(행들) > 1:
        raise AmbiguousRoute(
            f"고정 Route 계약이 둘 이상이다 ({len(행들)}건 이상)."
            " 🔴 하나를 임의로 고르지 않는다 — 호출자가 logistics_contract_id 를 준다."
        )
    행 = 행들[0]
    거리 = _cell(행, 1, "delivery_distance_km")
    계약id = str(_cell(행, 0, "logistics_contract_id"))
    if 거리 is None:
        raise RouteNotFound(
            f"계약에 거리가 없다 (logistics_contract_id={계약id!r})."
            " 🔴 0 으로 보정하지 않는다 — 가장 싼 운임 구간이 조용히 잡힌다."
        )
    return FixedRoute(
        logistics_contract_id=계약id,
        distance_km=Decimal(str(거리)),
        contract_vehicle_class=str(_cell(행, 2, "vehicle_class")),
        contract_baseline_cost_krw=Decimal(str(_cell(행, 3, "transport_cost_per_delivery_krw"))),
        contract_status=str(_cell(행, 4, "contract_status")),
        provisional=bool(_cell(행, 5, "provisional")),
    )


def _fixed_fee(conn: Any, *, vehicle_class: str, body_type: str, distance_km: Decimal) -> Decimal:
    """이 차량·거리의 회당 운임. **구간표에서 읽는다.**

    ```text
    distance_from_km < distance_km <= distance_to_km
    ```

    ★ 경계가 *"초과 ~ 이하"* 인 것은 DDL 주석이 못박은 계약이다
      (*"(0,11] = 문서의 ~11km"*). 양쪽을 이하로 잡으면 경계 거리에서 두 구간이 겹친다.

    ⚠️ **`is_active` 인 구간만 본다.** 내린 운임표로 견적을 내지 않는다.

    🔴 거리×단가 같은 새 모델을 만들지 않는다.
    """
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT rate_id, base_rate_krw
                FROM {}.vehicle_rate_table
                WHERE vehicle_class = %s AND body_type = %s AND is_active
                  AND distance_from_km < %s AND %s <= distance_to_km
                ORDER BY rate_id
                LIMIT %s
                """
            ).format(schema),
            (vehicle_class, body_type, distance_km, distance_km, _AMBIGUITY_PROBE_LIMIT),
        )
        행들 = list(cursor.fetchall())
    if not 행들:
        raise RateNotFound(
            f"운임 구간이 없다 (vehicle_class={vehicle_class!r} · body_type={body_type!r}"
            f" · distance_km={distance_km})."
            " 🔴 가장 가까운 구간으로 대체하지 않는다 — 없는 값을 지어내는 것과 같다."
        )
    if len(행들) > 1:
        겹침 = [str(_cell(행, 0, "rate_id")) for 행 in 행들]
        raise AmbiguousRate(
            f"운임 구간이 겹친다 (vehicle_class={vehicle_class!r} · distance_km={distance_km}):"
            f" {겹침} …. 🔴 싼 쪽·비싼 쪽을 임의로 고르지 않는다."
        )
    return Decimal(str(_cell(행들[0], 1, "base_rate_krw")))


def plan_fixed_route_transport(
    conn: Any,
    *,
    shipment_qty_kg: Decimal,
    logistics_contract_id: str | None = None,
    body_type: str | None = None,
) -> TransportPlan:
    """계약 baseline 기반 운송 견적. **읽기와 계산만 한다 — 아무것도 쓰지 않는다.**

    ```text
    ① 계약 확정        거리 · 계약 baseline
    ② 차량 선택        실을 수 있는 가장 작은 차 · 넘치면 가장 큰 차로 나눈다
    ③ 운임 구간 조회    (차량, 차체, 거리) → 회당 운임
    ④ 비용 = 회당 운임 × trip 수
    ```

    🔴 **재고를 건드리지 않는다.** `remaining_qty_kg` · `inventory_moves` ·
       할당 상태 — 셋 다 그대로다. 실제 감소는 `outbound.ship_allocated_stock` 이 한다.

    ★ **결정론이다.** 같은 입력·같은 표면 같은 답이 나온다. 시계도 난수도 안 쓴다.

    ⚠️ `standard_minutes` 는 항상 `None` 이다 — 소요시간의 정본이 스키마에 없다.
       거리÷속도로 지어내지 않는다.

    :param body_type: 냉장이 필요한지는 **호출자가 정한다.** 안 주면 차체로 좁히지 않는다.
    :raises RouteNotFound: 계약이 없거나 계약에 거리가 없을 때.
    :raises AmbiguousRoute: 계약이 둘 이상일 때.
    :raises RateNotFound: 그 차량·거리의 운임 구간이 없을 때.
    :raises AmbiguousRate: 운임 구간이 겹칠 때.
    """
    수량 = _quantity(shipment_qty_kg, 칸="shipment_qty_kg")
    route = resolve_fixed_route(conn, logistics_contract_id=logistics_contract_id)
    specs = load_vehicle_specs(conn, body_type=body_type)
    if not specs:
        raise RouteNotFound(
            f"차량 제원이 없다 (body_type={body_type!r}). vehicle_specs 가 비어 있다."
        )
    vehicle, trips = select_vehicle(specs, shipment_qty_kg=수량, body_type=body_type)
    회당 = _fixed_fee(
        conn,
        vehicle_class=vehicle.vehicle_class,
        body_type=vehicle.body_type,
        distance_km=route.distance_km,
    )
    return TransportPlan(
        logistics_contract_id=route.logistics_contract_id,
        distance_km=route.distance_km,
        vehicle_class=vehicle.vehicle_class,
        body_type=vehicle.body_type,
        vehicle_operational_payload_kg=vehicle.operational_payload_kg,
        shipment_qty_kg=수량,
        trip_count=trips,
        fixed_fee_per_trip_krw=회당,
        estimated_cost_krw=회당 * trips,
        standard_minutes=None,
        contract_baseline_cost_krw=route.contract_baseline_cost_krw,
        contract_vehicle_class=route.contract_vehicle_class,
    )
