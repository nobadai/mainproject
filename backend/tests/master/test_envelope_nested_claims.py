"""봉투가 **중첩 근거 주소를 읽는가** — 물류 PR #484 수신요청 §2 (2026-09-10).

물류 `PRE_SALES` 가 의미 단위 중첩이 되면서 Evidence claim 도 **값이 실제로 앉은
자리**를 가리킨다.

```text
sellable_supply.inventory_by_item[배추].available_qty_kg        점 + 셀렉터
sellable_supply.lot_constraints[LOT-001].remaining_freshness_days
sellable_supply.supply_capacity_by_date[2026-01-05].confirmed_sellable_quantity_kg
delivery_feasibility.daily_outbound_capacity_kg                 점만 (스칼라)
delivery_feasibility.transport_lead_time                        점만 (스칼라)
missing_data                                                    최상위
```

`_CLAIM_PATH` 의 `key` 가 점을 안 받던 동안 이 여섯 중 넷이 `canonical_claim` 에서
`None` 이 되어 **값이 payload 에 있는데도 `E-EVIDENCE-ORPHAN`** 이 떴다.

🔴 **여기서 잠그는 것 셋.**

```text
① 중첩 주소를 읽는다 — 점 + 셀렉터 · 점만 · 예전 평면/셀렉터 모두
② 요구는 안 넓힌다 — 중첩 Mapping 안의 숫자를 required 로 만들지 않는다 (§2.3)
③ 없는 주소는 여전히 None 이다 — fail-closed 가 안 풀렸다
```

★ ②가 이 파일에서 제일 중요하다. WP-4A 때 전역 재귀가 한번 들어가 **매입 required
  2→4 · 판매 8→10** 의 공용 회귀가 났다. 그래서 dotted canonical 은 `covered` 에는
  들어가도 `required` 에는 **영원히 안 들어간다** — 그 비대칭이 의도다.
"""

from __future__ import annotations

from datetime import date

from app.contracts.core import Evidence
from app.master.envelope import (
    AgentReply,
    canonical_claim,
    check_evidence_coverage,
    required_claims,
)

AS_OF = date(2026, 9, 10)

#: 물류 `PRE_SALES` 정본 payload 의 축소본. **주소를 바꾸지 않았다.**
PRE_SALES: dict = {
    "query_scope": {"as_of": "2026-01-02", "items": ["배추"]},
    "sellable_supply": {
        "status": "READY",
        "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000.0}],
        "lot_constraints": [
            {
                "lot_id": "LOT-001",
                "item": "배추",
                "available_qty_kg": 3000.0,
                "remaining_freshness_days": 9,
                "status": "AVAILABLE",
            }
        ],
        "supply_capacity_by_date": [
            {"date": "2026-01-05", "confirmed_sellable_quantity_kg": 2500.0}
        ],
    },
    "delivery_feasibility": {
        "status": "READY",
        "daily_outbound_capacity_kg": 7636.72,
        "transport_lead_time": 1,
    },
    "missing_data": [],
    "evidence_refs": ["DB:inventory_lots/2026-01-02"],
}

#: 물류가 실제로 내는 주소들 (`app/logistics/adapter.py` — 실측 2026-09-10).
물류_주소 = (
    "sellable_supply.inventory_by_item[배추].available_qty_kg",
    "sellable_supply.lot_constraints[LOT-001].available_qty_kg",
    "sellable_supply.lot_constraints[LOT-001].remaining_freshness_days",
    "sellable_supply.supply_capacity_by_date[2026-01-05].confirmed_sellable_quantity_kg",
    "delivery_feasibility.daily_outbound_capacity_kg",
    "delivery_feasibility.transport_lead_time",
)


def ev(claim: str, value: float = 1.0, unit: str = "kg") -> Evidence:
    return Evidence(
        claim=claim,
        source="tool_calc",
        ref_ids=(f"REF-{claim}",),
        value=value,
        unit=unit,
        evidence_grade="SIM_FIXED",
        evidence_detail="MVP 확정 가정",
    )


def reply(payload: dict, evidences: tuple[Evidence, ...] = ()) -> AgentReply:
    return AgentReply(
        request_id="REQ-1",
        as_of=AS_OF,
        agent="inventory",
        mode="PRE_SALES",
        run_id="LOG-RUN-1",
        runtime_status="READY",
        business_status="ok",
        payload=payload,
        evidences=evidences,
    )


# ---------------------------------------------------------------------------
# ① 중첩 주소를 읽는다
# ---------------------------------------------------------------------------


def test_점과_셀렉터가_함께_있는_주소를_읽는다():
    """`sellable_supply.inventory_by_item[배추].available_qty_kg`."""
    got = canonical_claim(PRE_SALES, "sellable_supply.inventory_by_item[배추].available_qty_kg")

    assert got == "sellable_supply.inventory_by_item[0].available_qty_kg"


def test_canonical_은_점_접두사를_유지한다():
    """🔴 접두사를 떼면 두 블록에 같은 이름의 배열이 있는 날 **서로 다른 사실이 한
    주소로 접힌다.**"""
    got = canonical_claim(PRE_SALES, "sellable_supply.lot_constraints[LOT-001].available_qty_kg")

    assert got is not None
    assert got.startswith("sellable_supply.")
    assert got == "sellable_supply.lot_constraints[0].available_qty_kg"


def test_셀렉터가_없는_중첩_스칼라도_읽는다():
    """`delivery_feasibility.daily_outbound_capacity_kg` — 점만 있고 배열이 없다.

    전에는 `claim if claim in payload else None` 이라 점 있는 주소가 **통째로**
    실패했다.
    """
    assert (
        canonical_claim(PRE_SALES, "delivery_feasibility.daily_outbound_capacity_kg")
        == "delivery_feasibility.daily_outbound_capacity_kg"
    )
    assert (
        canonical_claim(PRE_SALES, "delivery_feasibility.transport_lead_time")
        == "delivery_feasibility.transport_lead_time"
    )


def test_물류가_내는_여섯_주소가_전부_고아가_아니다():
    """★ **이 파일의 본론이다.** 물류 실측에서 6건 중 4건이 ORPHAN 이었다."""
    evidences = tuple(ev(claim) for claim in 물류_주소) + (ev("evidence_refs"),)
    found = check_evidence_coverage(reply(PRE_SALES, evidences))

    assert [f.code for f in found] == [], f"고아가 남았다: {[f.where for f in found]}"


def test_날짜_셀렉터도_문자열_필드로_찾는다():
    """`supply_capacity_by_date[2026-01-05]` 는 항목의 `date` 칸과 맞는다 —
    ★ **`_select_index` 를 안 바꿨다.** 항목의 문자열 필드 아무거나와 맞으면 된다."""
    got = canonical_claim(
        PRE_SALES,
        "sellable_supply.supply_capacity_by_date[2026-01-05].confirmed_sellable_quantity_kg",
    )

    assert got == "sellable_supply.supply_capacity_by_date[0].confirmed_sellable_quantity_kg"


# ---------------------------------------------------------------------------
# 예전 계약 — 한 줄도 안 죽었다
# ---------------------------------------------------------------------------


def test_평면_주소는_그대로_읽는다():
    assert canonical_claim({"total_amount_krw": 42}, "total_amount_krw") == "total_amount_krw"


def test_최상위_셀렉터_주소는_그대로_읽는다():
    payload = {"scenarios": [{"label": "보수", "total_amount_krw": 1}, {"label": "공격"}]}

    assert canonical_claim(payload, "scenarios[공격].label") == "scenarios[1].label"
    assert (
        canonical_claim(payload, "scenarios[0].total_amount_krw") == "scenarios[0].total_amount_krw"
    )


# ---------------------------------------------------------------------------
# ② 요구는 안 넓힌다 (§2.3)
# ---------------------------------------------------------------------------


def test_중첩_숫자를_필수_근거로_만들지_않는다():
    """🔴 **읽는 기능만 넓혔다.** 전역 재귀가 들어가면 매입 2→4 · 판매 8→10 이 도로 난다.

    ★ `evidence_refs` 는 **예전부터** 요구된다 — 최상위 스칼라 배열이라 *"통째로 하나의
      근거"* 규칙에 걸린다. 중첩과 무관한 기존 계약이라 여기서 안 건드린다.
    """
    required = required_claims(PRE_SALES)

    assert required == {"evidence_refs"}, f"중첩 숫자가 요구로 새어 나왔다: {sorted(required)}"
    assert not any("." in path for path in required), "점 있는 주소는 요구가 되면 안 된다"


def test_중첩_숫자에_근거를_아예_안_붙여도_통과한다():
    """★ 위 검사의 짝. 요구가 안 늘었다는 것을 **결과 쪽에서도** 잰다.

    `sellable_supply` · `delivery_feasibility` 안의 숫자 다섯에 근거를 하나도 안 붙여도
    `E-EVIDENCE-MISSING` 이 안 난다 — 최상위 `evidence_refs` 하나만 대면 된다.
    """
    assert check_evidence_coverage(reply(PRE_SALES, (ev("evidence_refs"),))) == []


def test_meta_안의_숫자는_통째로_하나만_요구된다():
    """⚠️ WP-4A 회귀가 났던 바로 그 모양이다.

    ```text
    회귀 때      meta.feedback_attempt · meta.received_adjustments   ← 잎마다 요구
    지금 (정상)  meta                                               ← 한 규칙이 만든 한 벌
    ```

    ★ `meta` 는 값이 전부 숫자라 `_is_number_map` 이 **통째로 하나**로 요구한다.
      그것은 중첩 재귀가 아니라 예전부터 있던 규칙이다.
    """
    payload = {"meta": {"feedback_attempt": 1, "received_adjustments": 2}, "total_amount_krw": 100}
    required = required_claims(payload)

    assert required == {"meta", "total_amount_krw"}
    assert "meta.feedback_attempt" not in required
    assert "meta.received_adjustments" not in required


def test_dotted_근거는_요구_목록을_안_늘린다():
    """★ **그 비대칭이 의도다.** 근거로 인정은 되지만 요구가 늘지는 않는다."""
    payload = {"sellable_supply": {"status": "READY", "daily": {"kg": 3000.0}}}

    assert canonical_claim(payload, "sellable_supply.daily.kg") == "sellable_supply.daily.kg"
    assert required_claims(payload) == set()


# ---------------------------------------------------------------------------
# ③ 없는 주소는 여전히 None — fail-closed
# ---------------------------------------------------------------------------


def test_없는_중첩_칸을_가리키면_고아다():
    assert canonical_claim(PRE_SALES, "sellable_supply.ghost_block.qty") is None


def test_없는_잎을_가리키면_고아다():
    assert canonical_claim(PRE_SALES, "delivery_feasibility.ghost_kg") is None


def test_중간이_Mapping_이_아니면_고아다():
    """🔴 **배열을 점으로 뚫지 않는다.** 뚫게 두면 `lots.0.qty` 와 `lots[0].qty` 두 표기가
    같은 자리를 가리켜 **한 사실에 주소가 둘**이 된다."""
    배열_잎 = "sellable_supply.inventory_by_item.0.available_qty_kg"

    assert canonical_claim(PRE_SALES, 배열_잎) is None
    assert canonical_claim(PRE_SALES, "evidence_refs.0") is None


def test_없는_중첩_주소는_ORPHAN_으로_걸린다():
    """★ **자기 생존 검사다.** 위 검사들이 초록인 이유가 *"아무것도 안 세서"* 가
    아니라는 것을 잰다 — 여기서는 반드시 빨개져야 한다."""
    found = check_evidence_coverage(
        reply(PRE_SALES, (ev("evidence_refs"), ev("sellable_supply.ghost.qty")))
    )

    assert [f.code for f in found] == ["E-EVIDENCE-ORPHAN"]
    assert found[0].where == "evidences[sellable_supply.ghost.qty]"


def test_점_있는_주소여도_배열이_아니면_셀렉터가_안_먹는다():
    assert canonical_claim(PRE_SALES, "delivery_feasibility[0].transport_lead_time") is None
