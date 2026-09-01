"""②③ 배선 — 물류 ``item_storage_policies`` 를 등급 배분에 쓴다 (#79).

사전 조사에서 나온 함정 셋을 그대로 잠근다.

1. **품목 필터** — policies 는 4품목이 아니라 5품목이 오고 **첫 항목이 무**다.
2. **이중 소스** — 물류 ``medium_grade_factor`` 와 yaml 계수가 같은 개념이었다.
3. **사유 순서** — 값이 온 뒤에도 *"안 왔다"* 고 적으면 거짓 사유가 나간다.
"""

from datetime import date

import pytest

from app.master.verifier import supplied_but_unresolved, unresolved_supplied_keys
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
    # 마스터가 중첩 한 겹까지 보게 되어(dev 42795c6) **못 읽은 값의 이름을 그대로** 쓴다.
    assert "operational_limit_days" in reason
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


def _keys(risk: str, inventory: dict) -> list[str]:
    """마스터가 **지목한 키**. 문장이 아니라 사실을 받는다.

    🔴 전에는 concern 문장의 머리말 따옴표를 정규식으로 열었다. 그것도 결국
      *"마스터가 문구를 안 바꾼다"* 에 기대는 것이라, 현서님이 문구를 다듬는 날 다시
      깨진다 — 그 전에는 **문장이 사유 원문을 통째로 되싣어** ``"키" in concern`` 이
      다른 키가 지목된 경우에도 참이 됐다(반례가 엉뚱하게 통과). 두 번 같은 자리에서
      깨진 셈이라 현서님이 ``unresolved_supplied_keys`` 를 여셨다 (verifier.py).

    ★ 사람이 읽을 문장이 필요하면 ``supplied_but_unresolved`` 를 쓴다. 둘은 **같은
      검사**를 부르므로 갈릴 자리가 없다.
    """
    return unresolved_supplied_keys([{"label": "보수", "risks": [risk]}], {"inventory": inventory})


def test_master_actually_flags_our_reason() -> None:
    """🔴 **마스터가 이 사유를 SUPPLIED-BUT-UNRESOLVED 로 잡는가** — 실제로 불러서 본다.

    ⚠️ **규칙을 재현하지 않는다.** 전에는 마스터의 정규식을 import 해 *"우리가 재현한
      규칙에 걸리는가"* 를 봤다. 검사는 통과했는데 실물 ``concerns`` 는 **0건**이었다 —
      관문이 둘인데 하나만 재현했기 때문이다 (2026-08-31 실측).

      현서님이 그래서 공개 함수를 열었다 (dev `4137bc9`). docstring 이 그 이유를 적고 있다:
      *"규칙을 재현하지 마십시오. 재현한 것이 진짜와 갈리는 순간, 검사는 통과하는데 실물은
      조용해집니다. 이 함수를 부르면 재현할 것이 없습니다."*

    이 검사는 **마스터 관통이 쓰는 것과 같은 코드**를 부른다. 재현할 것이 없다.
    """
    inventory = {"item_storage_policies": REAL_POLICIES, "lots": REAL_LOTS}
    reason = shelf_days_block_reason(inventory, "상", ITEM)

    # 사람이 읽을 문장이 필요한 자리 — 실패 메시지에 마스터가 쓴 말을 그대로 싣는다.
    concerns = supplied_but_unresolved(
        [{"label": "보수", "risks": [reason]}], {"inventory": inventory}
    )
    assert concerns, f"마스터가 못 잡는다: {reason!r}"

    # 단언은 키로 한다 — 문구가 바뀌어도 이 줄은 그대로다.
    assert "operational_limit_days" in _keys(reason, inventory)


def test_the_reason_names_the_value_we_actually_could_not_use() -> None:
    """🔴 **못 읽은 값의 이름을 그대로 쓴다.**

    전에는 봉투 최상위 키(``item_storage_policies``)로 적었다 — 마스터의 ``supplied`` 가
    최상위만 봐서 중첩 키로는 안 울렸기 때문이다. **증상에 문구를 맞춘 것**이지 사실을
    말한 것이 아니었다. 현서님이 ``_supplied_keys`` 를 중첩 한 겹까지 보게 고쳐서
    (dev `42795c6`) 이제 정확한 이름을 쓸 수 있다.
    """
    reason = shelf_days_block_reason(
        {"item_storage_policies": REAL_POLICIES, "lots": REAL_LOTS}, "상", ITEM
    )

    assert "operational_limit_days" in reason
    assert "item_storage_policies" not in reason


def test_the_sentence_length_no_longer_decides_whether_it_rings() -> None:
    """🔴 판정이 **글자 수로 재지 않는다** (dev `42795c6`).

    전에는 키 뒤 12자 안에 미결 어휘가 있어야 울렸다. 우리 문구가 그 창을 넘어서
    *"멀어지면 안 걸린다"* 를 오히려 잠그고 있었다 — 검사가 남의 문장 길이를 정하는
    상태였다. 새 규칙은 거리 대신 **사이에 다른 실린 키가 끼었는가** 를 본다.
    """
    inventory = {"item_storage_policies": REAL_POLICIES, "lots": REAL_LOTS}
    far = (
        "operational_limit_days 는 받았고 로트도 받았으나 등급 어휘가 아직 정해지지 "
        "않아 기준등급을 특정할 수 없어 미확정이다"
    )
    assert far.index("미확정") - far.index("operational_limit_days") > 12

    assert "operational_limit_days" in _keys(far, inventory)


def test_another_supplied_key_between_the_name_and_the_word_silences_it() -> None:
    """새 규칙의 반대편 — 사이에 **다른 실린 키**가 있으면 그 키 얘기라 안 울린다.

    이 반례가 없으면 위 검사가 "무엇이든 울린다"를 잠그는 것과 구분되지 않는다.

    🔴 **여기가 키로 받아야 하는 자리다.** 문장으로 보면 이 반례가 반례가 아니다 —
      concern 이 사유 원문을 되싣기 때문에 ``medium_grade_factor`` 가 지목된 경우에도
      문장 안에 ``operational_limit_days`` 라는 글자가 그대로 있다. 그래서 "안 울렸다"
      를 확인할 방법이 없었다.
    """
    inventory = {"item_storage_policies": REAL_POLICIES, "lots": REAL_LOTS}
    other = "operational_limit_days 검사는 medium_grade_factor 미확정으로 보류"

    flagged = _keys(other, inventory)

    assert "operational_limit_days" not in flagged, f"다른 키 얘기인데 울렸다: {flagged}"
    assert "medium_grade_factor" in flagged, "정작 그 키는 울려야 한다"


# ── Codex 교차검증 회귀 (2026-08-31) ────────────────────────────────────


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        (10.9, "소수는 조용히 10으로 잘렸다"),
        ("10", "문자열이 통과했다"),
        (True, "bool 이 1일로 통과했다"),
        (0, "0일이면 중품을 아예 못 쓴다 — 확정된 0이 아니라 잘못 온 값이다"),
        (-5, "음수 보관한계"),
        ("oops", "int() 가 ValueError 로 노드를 죽였다"),
    ],
)
def test_bad_operational_limit_days_is_not_silently_coerced(bad: object, why: str) -> None:
    """🟡 **Codex 지적 재현.** 이상한 보관한계를 고쳐 쓰지도, 터지지도 않는다.

    잘린 값은 에러가 나지 않아 아무도 모르고, ``ValueError`` 는 봉투 하나가 그래프
    전체를 멈춘다. 둘 다 안 되므로 **못 쓰는 값으로 보고 폴백**한다.
    """
    inventory = {
        "item_storage_policies": [{"item": ITEM, "operational_limit_days": bad}],
        "lots": [{"lot_id": "L1", "grade": "상", "available_qty_kg": 100}],
    }
    assert top_grade_shelf_days(inventory, "상", ITEM) is None, why


@pytest.mark.parametrize("bad", [0, -0.5, 5.0, True, "x", float("nan")])
def test_bad_medium_grade_factor_falls_back_instead_of_distorting(bad: object) -> None:
    """🟡 **Codex 지적 재현.** 범위 밖 계수는 폴백으로 보내고 **고지한다.**

    ``0``·음수는 폴백도 고지도 없이 중품 배분을 0으로 만들었고, ``1`` 초과는 중품
    소진 한계를 상품 한계일보다 길게 만들어 개념을 뒤집었다.
    """
    constraints = load_constraints()
    inventory = {"item_storage_policies": [
        {"item": ITEM, "operational_limit_days": 10, "medium_grade_factor": bad}
    ]}
    ratio, fell_back = mid_grade_shelf_ratio(inventory, ITEM, constraints)
    assert fell_back is True
    assert ratio == constraints["grade"]["mid_grade_shelf_ratio_fallback"]


@pytest.mark.parametrize(
    ("lots", "forbidden"),
    [
        ([{"lot_id": "L1", "grade": "상", "shelf_life_days": None}], "등급 로트가 없어"),
        (
            [
                {"lot_id": "L1", "grade": "상"},
                {"lot_id": "L2", "grade": "중", "shelf_life_days": 10},
            ],
            "등급 로트가 없어",
        ),
    ],
    ids=["상등급_값이_None", "중등급에만_키가_있음"],
)
def test_reason_does_not_deny_a_lot_that_exists(lots: list, forbidden: str) -> None:
    """🟡 **Codex 지적 재현.** 있는 로트를 없다고 말하지 않는다.

    사유 판정이 "키가 있는가"를 보고 ``top_grade_shelf_days`` 는 "값이 있는가"를 봤다.
    두 기준이 갈리자 **상 등급 로트가 있는데도** *"상 등급 로트가 없어"* 라고 답했다 —
    재고에 없는 사실을 만들어 내는 것이라, 읽는 사람이 창고를 잘못 이해한다.
    """
    inventory = {"lots": lots}
    reason = shelf_days_block_reason(inventory, "상", ITEM)
    assert forbidden not in reason, f"있는 로트를 부정한다: {reason!r}"
    assert "받지 못했다" in reason or "읽지 못했다" in reason


# ── 타입 강제 — 폴백 경로도 같은 검사를 받는다 (2026-08-31) ─────────────────


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ("10", "문자열이 그대로 통과해 뒤에서 '10' * 0.6 으로 죽었다"),
        (10.9, "소진 한계가 6.54일이 된다 — 에러 없이 다른 값"),
        (True, "bool 이 1일로 통과했다"),
        (0, "신선도 리스크가 1.0 으로 굳어 중품이 조용히 막힌다"),
        (-5, "음수 유통기한"),
    ],
)
def test_lot_shelf_life_gets_the_same_type_check_as_the_policy(bad: object, why: str) -> None:
    """🔴 ``operational_limit_days`` 는 막아 두고 **로트 폴백은 그냥 읽고 있었다.**

    같은 종류의 값인데 한쪽만 지킨 상태였다 — 물류 값이 없는 날에만 타는 경로라
    실측에서 안 드러났다.
    """
    inventory = {
        "item_storage_policies": [],
        "lots": [{"lot_id": "L", "item": ITEM, "grade": "상", "shelf_life_days": bad}],
    }

    assert top_grade_shelf_days(inventory, "상", ITEM) is None, why


def test_mixed_lot_types_do_not_crash_the_comparison() -> None:
    """혼합 타입이면 ``min()`` 이 str 과 int 를 비교하다 죽었다.

    못 읽는 값은 **버린다** — 0으로 채우지 않는다 (규칙 3). 읽을 수 있는 값이 남으면
    그것으로 판단하고, 전부 버려지면 None 이라 사유가 나간다.
    """
    inventory = {
        "item_storage_policies": [],
        "lots": [
            {"lot_id": "A", "item": ITEM, "grade": "상", "shelf_life_days": 10},
            {"lot_id": "B", "item": ITEM, "grade": "상", "shelf_life_days": "8"},
        ],
    }

    assert top_grade_shelf_days(inventory, "상", ITEM) == 10


def test_the_policy_still_wins_over_a_readable_lot_value() -> None:
    """검사를 붙이면서 우선순위가 바뀌지 않았는지 — 물류 값이 있으면 그것을 쓴다."""
    inventory = {
        "item_storage_policies": [{"item": ITEM, "operational_limit_days": 14}],
        "lots": [{"lot_id": "A", "item": ITEM, "grade": "상", "shelf_life_days": 10}],
    }

    assert top_grade_shelf_days(inventory, "상", ITEM) == 14
