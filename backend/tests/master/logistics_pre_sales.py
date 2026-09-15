"""물류 `PRE_SALES` 회신 payload 의 **정본 모양** 최소본 — 마스터 검사들이 함께 쓴다.

🔴 **모양이 이제 실제로 쓰인다** (2026-09-10 · 물류 PR #484 · 판매 `#509`).

  그 전까지 이 자리에는 `{"sellable": "yes"}` 같은 아무 매핑이나 들어 있었다.
  마스터가 그 값을 `supply_context` 라는 **판매가 모르는 이름**으로 실었고 어댑터가
  조용히 버렸기 때문에, 모양이 틀려도 아무 일도 안 났다.

  지금은 `sales_flow._proposal_input` 이 이것을 `logistics_context` 로 넘기고
  `SalesLogisticsContext(extra="forbid")` 가 문 앞에서 읽는다 — **모양이 틀리면
  판매 회신이 통째로 선다.** 그래서 검사용 대역도 정본 모양이어야 한다.

★ **최소본이다.** 계약이 필수로 요구하는 칸만 채운다 — `query_scope` ·
  `delivery_feasibility` 는 `None` 이 정상값이라 안 만든다 (§1.2-10 — 안 낸 것과
  빈 것을 구별할 수 있게 둔다).

⚠️ **여기 숫자에 근거를 안 붙여도 된다.** `envelope.required_claims` 는 중첩 Mapping
  안의 숫자를 요구하지 않는다 — 물류가 `§2.3` 에서 그 전역 재귀를 명시적으로 금지했다.
"""

from __future__ import annotations

from typing import Any

#: 물류가 답한 날의 최소 정본 payload.
PRE_SALES_PAYLOAD: dict[str, Any] = {
    "sellable_supply": {
        "status": "READY",
        "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000.0}],
    },
}
