"""②③ 배선 — 물류 ``item_storage_policies`` 를 등급 배분에 쓴다 (#79).

사전 조사에서 나온 함정 셋을 그대로 잠근다.

1. **품목 필터** — policies 는 4품목이 아니라 5품목이 오고 **첫 항목이 무**다.
2. **이중 소스** — 물류 ``medium_grade_factor`` 와 yaml 계수가 같은 개념이었다.
3. **사유 순서** — 값이 온 뒤에도 *"안 왔다"* 고 적으면 거짓 사유가 나간다.
"""

import re
from datetime import date

from app.master.verifier import MasterVerifier
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.allocate_sourcing import (
    evaluate_mid_grade,
    item_storage_policy,
    mid_grade_shelf_ratio,
    shelf_days_block_reason,
    top_grade_shelf_days,
)
from app.purchase_agent.state import build_initial_state

SPREAD_WIDE = date(2026, 9, 11)
ITEM = "배추"

#: 물류가 실제로 보내는 모양 (2026-08-31 실측 · `REQ-B1-20251231`).
#:
#: 🔴 **순서를 바꾸지 않는다.** 첫 항목이 무(14)이고 배추는 두 번째인 것이 이 픽스처의
#:   요점이다. 정렬해서 넣으면 `policies[0]` 함정이 재현되지 않는다.
REAL_POLICIES = [
    {"item": "무", "operational_limit_days": 14, "medium_grade_factor": 0.6},
    {"item": "배추", "operational_limit_days": 10, "medium_grade_factor": 0.6},
    {"item": "양파", "operational_limit_days": 30, "medium_grade_factor": 0.6},
    {"item": "건고추", "operational_limit_days": 90, "medium_grade_factor": 0.6},
    {"item": "피마늘", "operational_limit_days": 30, "medium_grade_factor": 0.6},
]

#: 물류가 실제로 보내는 로트 — **grade 가 전부 None** 이다 (#69 등급 어휘 미확정).
#: `shelf_life_days` 도 없다. 이 둘이 실물의 핵심 성질이다.
REAL_LOTS = [
    {"lot_id": "LOT-KIMCHI-015-BAECHU", "item": "배추", "available_qty_kg": 286.92,
     "remaining_freshness_days": 10, "grade": None, "status": "ACTIVE"},
]


# ── 함정 1: 품목 필터 ────────────────────────────────────────────────────


def test_policy_is_picked_by_item_not_by_position() -> None:
    """🔴 **첫 항목이 무(14)** 인 목록에서 배추(10)를 골라야 한다.

    ``policies[0]`` 을 집으면 14가 나온다. 에러가 아니라 **다른 품목의 보관한계로 중품
    소진 창을 계산하는 상태**라 아무도 모른다 — 실제로 이 함정을 밟은 사례가 있다(#79).
    """
    inventory = {"item_storage_policies": REAL_POLICIES}
    picked = item_storage_policy(inventory, ITEM)
    assert picked is not None
    assert picked["operational_limit_days"] == 10, "무(14)를 집었다면 위치로 고른 것이다"


def test_items_outside_our_scope_are_ignored() -> None:
    """건고추(90)는 목록에 있어도 매입 품목이 아니다 — 정책을 읽지 않는다."""
    inventory = {"item_storage_policies": REAL_POLICIES}
    assert item_storage_policy(inventory, "건고추") is None


def test_shelf_days_use_the_policy_of_this_item() -> None:
    """상품 한계일이 **이 품목의** ``operational_limit_days`` 로 온다.

    등급을 특정할 수 있어야 하므로 로트에 ``grade`` 를 준다 — 실물은 None 이라 여기까지
    오지 못하고, 그 상태는 아래 사유 검사가 따로 잠근다.
    """
    inventory = {
        "item_storage_policies": REAL_POLICIES,
        "lots": [{"lot_id": "L1", "item": ITEM, "grade": "상", "available_qty_kg": 100}],
    }
    assert top_grade_shelf_days(inventory, "상", ITEM) == 10
    assert top_grade_shelf_days(inventory, "상", "무") == 14, "품목이 바뀌면 값도 바뀐다"


def test_policy_wins_over_lot_shelf_life() -> None:
    """두 소스가 다 있으면 **물류 정책이 정본**이다 — 실측값이 로트 추론을 이긴다."""
    inventory = {
        "item_storage_policies": REAL_POLICIES,
        "lots": [{"lot_id": "L1", "grade": "상", "shelf_life_days": 99}],
    }
    assert top_grade_shelf_days(inventory, "상", ITEM) == 10


# ── 함정 2: 이중 소스 ────────────────────────────────────────────────────


def test_mid_grade_factor_comes_from_logistics_when_supplied() -> None:
    """물류가 보내면 그 값을 쓰고, 폴백 깃발은 서지 않는다."""
    inventory = {"item_storage_policies": [
        {"item": ITEM, "operational_limit_days": 10, "medium_grade_factor": 0.4}
    ]}
    ratio, fell_back = mid_grade_shelf_ratio(inventory, ITEM, load_constraints())
    assert ratio == 0.4
    assert fell_back is False


def test_mid_grade_factor_falls_back_and_says_so() -> None:
    """물류가 안 보내면 설계 기본값으로 떨어지되 **그 사실을 돌려준다** (규칙 3)."""
    constraints = load_constraints()
    ratio, fell_back = mid_grade_shelf_ratio({}, ITEM, constraints)
    assert ratio == constraints["grade"]["mid_grade_shelf_ratio_fallback"]
    assert fell_back is True


def test_the_old_double_source_key_is_gone() -> None:
    """yaml 에 옛 키가 남아 있으면 **두 곳을 각자 읽는 상태로 되돌아간다.**

    값이 같아(0.6) 갈라져도 티가 나지 않으므로, 키의 부재를 명시적으로 잠근다.
    """
    grade = load_constraints()["grade"]
    assert "mid_grade_shelf_ratio" not in grade
    assert "mid_grade_shelf_ratio_fallback" in grade


def test_fallback_is_disclosed_in_risks() -> None:
    """폴백으로 계산했으면 화면에 남는다 — mock 은 policies 를 싣지 않는다."""
    from app.purchase_agent.graph import run_purchase_agent

    proposal = run_purchase_agent(ITEM, SPREAD_WIDE)
    for scenario in proposal["scenarios"]:
        assert any("medium_grade_factor" in risk for risk in scenario["risks"]), scenario["risks"]


# ── 함정 3: 사유 순서 ────────────────────────────────────────────────────


def test_real_shape_reports_grade_not_missing_value() -> None:
    """🔴 **실물에서 "값이 안 왔다"고 적으면 거짓이다.**

    물류는 ``operational_limit_days`` 를 보냈다. 못 쓰는 이유는 로트 ``grade`` 가 전부
    None 인 것(#69)이고, 사유는 그렇게 적혀야 한다 — 아니면 물류에 잘못된 문의가 간다.
    """
    inventory = {"item_storage_policies": REAL_POLICIES, "lots": REAL_LOTS}
    reason = shelf_days_block_reason(inventory, "상", ITEM)
    assert "등급" in reason and "#69" in reason
    assert "item_storage_policies" in reason, "봉투 최상위 키로 말해야 마스터가 잡는다"
    assert "받지 못했다" not in reason, "값은 왔다 — 안 왔다고 적으면 안 된다"


def test_missing_reason_needs_both_sources_absent() -> None:
    """두 소스가 **다 없을 때만** "못 받았다"가 참이다."""
    inventory = {"lots": [{"lot_id": "L1", "grade": "상", "available_qty_kg": 100}]}
    assert "어느 쪽으로도 받지 못했다" in shelf_days_block_reason(inventory, "상", ITEM)


def test_policy_alone_does_not_unblock_the_allocation() -> None:
    """값이 와도 등급을 모르면 배분은 보류된다 — 정책값을 등급 자리에 끼우지 않는다."""
    state = build_initial_state(ITEM, SPREAD_WIDE)
    state["inventory"]["lots"] = REAL_LOTS
    state["inventory"]["item_storage_policies"] = REAL_POLICIES
    decision = evaluate_mid_grade(state, load_constraints())
    assert decision["blocked_by"] is not None
    assert decision.get("ratio", 0) == 0


# ── 🔴 마스터 검사와의 접점 ──────────────────────────────────────────────


#: 물류가 봉투에 싣는 **최상위** 키 (2026-08-31 실측 `constraints.inventory`).
#: 마스터의 `supplied` 집합이 이 층에서만 만들어진다 — 중첩 필드는 들어가지 않는다.
ENVELOPE_TOP_LEVEL_KEYS = (
    "warehouse_free_kg", "rental_cap_kg", "used_capacity_kg", "guaranteed_capacity_kg",
    "burst_capacity_kg", "inbound_lead_days", "cap_by_date", "lots",
    "item_storage_policies", "soft_warnings", "policy_version_used",
)


def test_master_actually_flags_our_reason() -> None:
    """🔴 **마스터가 이 사유를 SUPPLIED-BUT-UNRESOLVED 로 잡는가** — 관문 둘을 다 본다.

    ``_check_supplied_but_unused`` 는 두 조건이 **모두** 맞아야 울린다.

    1. 키가 ``supplied`` 에 있을 것. 그 집합은 **봉투 payload 의 최상위 키**로만 만들어진다
       (``for key, value in payload.items()``). 중첩 필드는 들어가지 않는다.
    2. ``re.escape(key) + _UNRESOLVED_NEAR`` 에 걸릴 것 — 키 뒤 **12자 안**에 미결 어휘.

    ⚠️ 처음에는 2번만 검사했고 통과했다. 그런데 실물에서 concerns 가 **0건**이었다 —
    사유에 적은 ``operational_limit_days`` 가 ``item_storage_policies[]`` **안에** 있어
    1번을 못 넘었기 때문이다 (2026-08-31 실측). **정규식에 걸리는 것과 마스터가 잡는 것은
    다르다.** 그래서 이 검사는 두 관문을 함께 재현한다.

    패턴은 문자열로 베끼지 않고 마스터에서 import 한다 — 복제하면 그쪽이 바뀔 때
    거짓 안심을 준다.
    """
    inventory = {"item_storage_policies": REAL_POLICIES, "lots": REAL_LOTS}
    reason = shelf_days_block_reason(inventory, "상", ITEM)

    hit = [
        key
        for key in ENVELOPE_TOP_LEVEL_KEYS
        if re.compile(re.escape(key) + MasterVerifier._UNRESOLVED_NEAR).search(reason)
    ]
    assert hit, f"봉투 최상위 키 중 어느 것도 안 걸린다 — 마스터가 못 잡는다: {reason!r}"
    assert "item_storage_policies" in hit


def test_a_nested_field_name_alone_would_not_be_flagged() -> None:
    """중첩 필드 이름만으로는 안 잡힌다 — 위 검사가 무엇을 막고 있는지 못박는다.

    이 반례가 없으면 사유를 ``operational_limit_days`` 로 되돌려도 통과하는 줄 알기 쉽다.
    """
    assert "operational_limit_days" not in ENVELOPE_TOP_LEVEL_KEYS


def test_the_distance_constraint_actually_bites() -> None:
    """거리 제약이 **실제로 좁다**는 것 — 멀어지면 안 걸린다는 사실을 함께 잠근다."""
    pattern = re.compile(re.escape("item_storage_policies") + MasterVerifier._UNRESOLVED_NEAR)
    assert not pattern.search("item_storage_policies 는 반영했으나 등급 어휘가 미확정이다")
