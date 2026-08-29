"""부서 간 이름 번역 — **🔴 한시 조치. 합의되면 이 파일은 사라진다.**

같은 값을 재무·물류·매입이 **다른 이름으로 부른다.** 붙여 보기 전에는 없던 문제이고,
셋 다 **합의로 풀리지 구현으로 풀리지 않는다.**

★ **조용히 바꾸지 않는다.** 이 모듈은 바꾼 것을 함께 돌려주고, 그것이 실행 결과의
  `concerns` 로 올라가 리포트에 그대로 나온다. 마스터가 이름을 맞춰 덮어 버리면
  **같은 문제가 더 깊은 곳에서 다시 난다** — 합의가 안 됐다는 사실이 사라지기 때문이다.

★ **이름이 같은 뜻일 때만 바꾼다.** 뜻이 다르면 **싣지 않는다.**
  `shelf_life_days` 가 그 경우다 — 물류는 *기존 로트의 잔여* 를 주고 매입은 *새로 살
  물건의 전체 유통기한* 을 기대한다. 잔여를 그 칸에 넣으면 숫자는 들어가고 뜻은
  틀린다. 안 실으면 매입이 미결로 처리한다(규칙 3) — **그쪽이 맞다.**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 물류 → 매입. **뜻이 같고 이름만 다른 것만** 여기 둔다.
_LOT_RENAMES = {"available_qty_kg": "remaining_kg"}

#: 매입이 읽지만 물류가 주지 않는 칸. **채우지 않고 사유를 남긴다.**
_LOT_UNSUPPLIED = {
    "shelf_life_days": (
        "물류는 기존 로트의 잔여 신선도(remaining_freshness_days)를 주고, 매입은 "
        "새로 살 물건의 전체 유통기한을 기대한다 — 뜻이 다르므로 싣지 않았다. "
        "매입의 중품 소진 한계가 미결로 남는다"
    ),
    "stocked_at": "물류가 입고일을 payload 에 싣지 않는다 — as_of 로 채우면 거짓 입고일이 된다",
}


def translate_lots(
    lots: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """물류 `lots` 를 매입이 읽는 이름으로. **바꾼 것을 함께 돌려준다.**

    :returns: (번역된 로트, 사람이 읽는 변경 내역)
    """
    if not lots:
        return [], []

    out: list[dict[str, Any]] = []
    renamed: set[str] = set()
    for lot in lots:
        row = dict(lot)
        for old, new in _LOT_RENAMES.items():
            if old in row and new not in row:
                row[new] = row.pop(old)
                renamed.add(f"{old} → {new}")
        out.append(row)

    notes = [
        f"로트 필드 이름을 마스터가 맞췄다: {name} (합의 전 한시 조치)" for name in sorted(renamed)
    ]
    notes.extend(
        f"로트 {field} 를 싣지 못했다 — {why}"
        for field, why in _LOT_UNSUPPLIED.items()
        if not any(field in lot for lot in lots)
    )
    return out, notes


def floor_kg(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """🔴 **kg 값을 정수로 내린다 — 이건 이름이 아니라 값을 바꾸는 일이다.**

    물류는 `7636.72kg` 처럼 소수로 답하고, 매입의 출력 계약(`PurchaseProposal`)은
    수량·금액을 **정수만** 받는다. 매입이 입력 정밀도를 출력에 그대로 흘려서,
    `total_qty_kg=7636.72` 가 자기 스키마 검증에 걸려 **매입 전체가 터진다.**

    ★ **근본 해결은 여기가 아니다.** 물류가 정수로 주거나, 매입이 자기 안에서
      정수화하거나 둘 중 하나이고 **합의 항목**이다. 마스터가 입구에서 내리는 것은
      관통을 한 번 뚫어 보기 위한 우회다.

    ★ **내림(floor)이지 반올림이 아니다.** 여유·상한을 과소평가하는 쪽이라 **덜 사는**
      방향이다 — 창고를 넘겨 사는 것보다 안전하다. 방향을 고른 것 자체를 기록에 남긴다.

    ★ 금액(`_krw`)은 건드리지 않는다. 매입이 단가 × 수량으로 만드는 값이라 수량이
      정수가 되면 따라 정수가 된다 — 여기서 함께 내리면 **두 번 깎인다.**
    """
    out = dict(payload)
    changed: list[str] = []

    for key, value in list(out.items()):
        if key.endswith("_kg") and isinstance(value, float) and not value.is_integer():
            out[key] = int(value)
            changed.append(f"{key} {value} → {out[key]}")

    caps = out.get("cap_by_date")
    if isinstance(caps, Mapping):
        floored = {
            day: (int(v) if isinstance(v, float) and not v.is_integer() else v)
            for day, v in caps.items()
        }
        if floored != dict(caps):
            out["cap_by_date"] = floored
            changed.append("cap_by_date 의 소수 값")

    lots = out.get("lots")
    if isinstance(lots, list):
        new_lots, lot_changed = [], False
        for lot in lots:
            row = dict(lot)
            for key, value in list(row.items()):
                if key.endswith("_kg") and isinstance(value, float) and not value.is_integer():
                    row[key] = int(value)
                    lot_changed = True
            new_lots.append(row)
        if lot_changed:
            out["lots"] = new_lots
            changed.append("lots 의 kg 소수 값")

    if not changed:
        return out, []
    note = (
        "🔴 마스터가 kg 을 정수로 **내렸다**(합의 전 한시 조치) — "
        "물류는 소수로 답하고 매입 출력 계약은 정수만 받는다. "
        f"바꾼 것: {', '.join(changed)}. 내림이라 덜 사는 방향이다"
    )
    return out, [note]


def translate_inventory(
    payload: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """물류 `PRE_PURCHASE` payload 전체를 매입이 읽는 모양으로.

    ★ `lots` 밖은 손대지 않는다. 지금 어긋난 것이 로트 필드뿐이고, **미리 넓게 잡으면
      나중에 진짜 불일치가 생겼을 때 이 층이 그것까지 조용히 덮는다.**
    """
    if not payload:
        return {}, []
    out = dict(payload)
    notes: list[str] = []
    if "lots" in out:
        out["lots"], lot_notes = translate_lots(out["lots"])
        notes.extend(lot_notes)
    out, kg_notes = floor_kg(out)
    notes.extend(kg_notes)
    return out, notes
