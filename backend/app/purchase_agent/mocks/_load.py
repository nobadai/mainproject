"""mock JSON을 IO명세 §1의 반환 형태로 materialize한다.

**as_of가 시나리오 키다.** 포트 시그니처에 ``scenario`` 인자를 넣을 수 없으므로
(계약으로 확정됨) 4개 앵커일에 4개 단위 테스트 시나리오를 배정했다 — ``scenarios.json``.
전역 스위치나 환경변수를 쓰지 않으므로 상태가 없고, 테스트 간 오염도 없다.

**날짜는 저장하지 않고 오프셋으로 저장한다.** 재고의 ``stocked_at``, 주문의 ``due_date``,
예측의 ``daily[].date``는 전부 ``as_of`` 상대값이다. 리터럴 날짜를 쓰면 9/11 시나리오에서
"8/24 납품"이 과거가 되어 등급-신선도 매칭이 무의미해진다. 여기서 ``as_of``를 더해 실날짜를
만든다 — 그래서 이 모듈에도 벽시계(현재 시각)를 읽는 코드가 없다 (규칙 1).

**캐시하지 않는다.** 매 호출마다 파일을 다시 읽고 새 dict를 만든다. 로드한 객체를 재사용하면
호출자가 반환값을 만졌을 때 다음 호출자가 오염된 데이터를 받는다 — mock은 read-only 경계를
흉내 내는 물건이라 그 성질이 특히 중요하다. 파일은 전부 100줄 안팎이다.
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent

#: 취급 품목 (상세설계 §3). 이 밖의 품목은 mock이 없다 — 조용히 빈 값을 주지 않는다.
ITEMS: tuple[str, ...] = ("배추", "무", "피마늘", "양파")


def _read(name: str) -> dict[str, Any]:
    with (_HERE / name).open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise TypeError(f"{name} must contain a JSON object, got {type(loaded).__name__}")
    return loaded


def _pick(block: dict[str, Any], key: str, where: str) -> Any:
    """키를 꺼내되, 없으면 **어느 파일의 어느 블록인지** 말해준다.

    ``block[key]``로 바로 읽으면 ``KeyError('무')``만 남아 어느 mock 파일이 깨졌는지
    알 수 없다. mock은 사람이 손으로 고치는 파일이라 이 문맥이 특히 값싸게 유용하다.
    """
    if key not in block:
        available = sorted(k for k in block if not k.startswith("_"))
        raise KeyError(f"{where} has no {key!r}; available: {available}")
    return block[key]


def _require_item(item: str) -> None:
    if item not in ITEMS:
        raise ValueError(f"unknown item {item!r}; mock covers {list(ITEMS)}")


def _require_int(value: object, name: str) -> int:
    """정수만 받는다. ``bool``을 먼저 거르는 이유: ``isinstance(True, int)``가 참이라
    ``days=True``가 ``days=1``로 둔갑해 조용히 빈 주문 목록을 돌려준다.
    ``3.5``도 비교 연산만으로는 통과해버린다 — 타입이 계약이면 런타임에서도 계약이다.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {value!r} ({type(value).__name__})")
    return value


def scenario_for(as_of: date) -> dict[str, Any]:
    """``as_of``에 배정된 시나리오. 앵커일이 아니면 어떤 날이 있는지 알려주며 터진다."""
    anchors = _read("scenarios.json")["anchors"]
    key = as_of.isoformat()
    if key not in anchors:
        raise KeyError(f"no mock scenario for as_of={key}; anchors are {sorted(anchors)}")
    return anchors[key]


def load_forecast(item: str, as_of: date) -> dict[str, Any]:
    """IO명세 §1-① 형태. ``daily``는 D+1 ~ D+18 총 18건."""
    _require_item(item)
    name = f"forecast_{scenario_for(as_of)['forecast']}.json"
    data = _read(name)
    block = _pick(_pick(data, "items", name), item, f"{name}.items")
    return {
        # 예측 배치는 당일 아침 06:00에 생성된 것으로 둔다 (generated_at <= as_of).
        "generated_at": f"{as_of.isoformat()}T06:00:00+09:00",
        "item": item,
        "unit": data["unit"],
        "current_price": block["current_price"],
        "horizon_days": data["horizon_days"],
        "daily": [
            {
                "date": (as_of + timedelta(days=row["offset_days"])).isoformat(),
                "predicted": row["predicted"],
                "lower": row["lower"],
                "upper": row["upper"],
            }
            for row in block["daily"]
        ],
        "model_version": data["model_version"],
    }


def load_quotes(item: str, as_of: date) -> list[dict[str, Any]]:
    """가락 등급별 당일 시세. 포트 시그니처대로 **quotes 배열만** 돌려준다 (IO명세 §1-②)."""
    _require_item(item)
    name = f"quotes_{scenario_for(as_of)['quotes']}.json"
    data = _read(name)
    return [dict(quote) for quote in _pick(_pick(data, "items", name), item, f"{name}.items")]


def load_inventory(item: str, as_of: date) -> dict[str, Any]:
    """IO명세 §1-③ 형태. ``stocked_at``은 오프셋에서 만든다."""
    _require_item(item)
    scenario_for(as_of)  # 앵커일 검증 — 6개 포트가 같은 날짜 규칙을 따르게 한다
    block = _pick(_pick(_read("inventory.json"), "items", "inventory.json"), item, "inventory.json.items")
    return {
        "as_of": as_of.isoformat(),
        "item": item,
        "lots": [
            {
                "lot_id": lot["lot_id"],
                "grade": lot["grade"],
                "stocked_at": (as_of + timedelta(days=lot["stocked_at_offset_days"])).isoformat(),
                "remaining_kg": lot["remaining_kg"],
                # 이 로트의 유통기한. constraints.yaml의 품목 보관한계와 다른 개념이다.
                "shelf_life_days": lot["shelf_life_days"],
            }
            for lot in block["lots"]
        ],
        "warehouse_free_kg": block["warehouse_free_kg"],
        "rental_cap_kg": block["rental_cap_kg"],
    }


def load_orders(item: str, as_of: date, days: int) -> dict[str, Any]:
    """IO명세 §1-④ 형태. ``days``로 **실제로 거른다** — ``total_kg``도 거른 뒤 합산한다."""
    _require_item(item)
    scenario_for(as_of)
    _require_int(days, "days")
    if days < 0:
        raise ValueError(f"days must be non-negative, got {days}")
    kept = [
        {
            "sale_id": order["sale_id"],
            "qty_kg": order["qty_kg"],
            "due_date": (as_of + timedelta(days=order["due_date_offset_days"])).isoformat(),
        }
        for order in _pick(_pick(_read("orders.json"), "items", "orders.json"), item, "orders.json.items")
        if 0 <= order["due_date_offset_days"] <= days
    ]
    return {
        "as_of": as_of.isoformat(),
        "item": item,
        "orders": kept,
        "total_kg": sum(order["qty_kg"] for order in kept),
    }


def load_cash(as_of: date, horizon_days: int) -> int:
    """향후 ``horizon_days``일 최저 예상 현금. 포트 시그니처대로 **정수 하나만** 돌려준다."""
    scenario_for(as_of)
    _require_int(horizon_days, "horizon_days")
    table = _pick(_read("cash.json"), "by_horizon_days", "cash.json")
    key = str(horizon_days)
    if key not in table:
        # 30일치 숫자를 60일 질문에 조용히 돌려주면 운전자본 갭을 놓친다 (IO명세 §1-⑤).
        raise KeyError(f"no mock cash for horizon_days={horizon_days}; have {sorted(table)}")
    # int()로 감싸지 않는다 — JSON에 "5347696"이나 5347696.0이 들어와도 통과시켜버린다.
    return _require_int(table[key], f"cash.json.by_horizon_days[{key}]")


def filter_by_published_at(records: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    """``published_at <= as_of``만 남긴다. 발행일 없는 레코드는 **적재 자체를 거부**한다.

    look-ahead 방어의 생명선이라 조용히 건너뛰지 않는다 (IO명세 §1-⑥) — 건너뛰면 발행일이
    빠진 문서가 코퍼스에서 통째로 사라지고, 그 사실을 아무도 모른 채 근거가 비어버린다.
    """
    kept = []
    for record in records:
        published_at = record.get("published_at")
        if not published_at:
            raise ValueError(f"document {record.get('doc_id')!r} has no published_at; refusing load")
        if date.fromisoformat(published_at) <= as_of:
            kept.append(record)
    return kept


def load_documents(item: str, as_of: date, doc_types: list[str]) -> list[dict[str, Any]]:
    """IO명세 §1-⑥ 형태. item · doc_type · published_at 세 필터를 모두 통과한 것만."""
    _require_item(item)
    scenario_for(as_of)  # 6개 포트가 같은 날짜 규칙을 따른다 — 문서만 예외를 두지 않는다
    corpus = _pick(_read("documents.json"), "documents", "documents.json")

    known = sorted({record["doc_type"] for record in corpus})
    if not doc_types:
        raise ValueError(f"doc_types must not be empty; choose from {known}")
    unknown = [doc_type for doc_type in doc_types if doc_type not in known]
    if unknown:
        raise ValueError(f"unknown doc_types {unknown}; corpus has {known}")

    matched = [
        record for record in corpus if record["item"] == item and record["doc_type"] in doc_types
    ]
    return [
        {
            "doc_id": record["doc_id"],
            "source": record["source"],
            "doc_type": record["doc_type"],
            "item": record["item"],
            "title": record["title"],
            "published_at": record["published_at"],
            "content": record["content"],
        }
        for record in filter_by_published_at(matched, as_of)
    ]
