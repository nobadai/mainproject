"""Epic 1 계약 검사 — schemas / constraints.yaml / ports.

각 테스트는 "설계 문서의 어느 조항이 코드로 지켜지는가"에 1:1로 대응한다.
정상 픽스처 하나를 만들고 조항별로 한 필드씩 깨뜨려 거부되는지 확인한다.

픽스처 숫자는 IO명세 §2 / 상세설계 §5의 예시 JSON과 일치시켰다 — 예시 JSON은 mock 스펙이자
실제 산출물 계약이므로(IO명세 §3 "삼위일체") 같은 값을 쓰면 다음 단계의 mock을 여기서 그대로
가져갈 수 있다.

(이력: ① 두 문서의 예시가 한때 ``total_amount_krw = 10,318,995`` 로 적혀 있었으나 같은
예시의 sourcing 합계는 7,125,000이라 사중 일치를 위반했다 — 문서 수정으로 해결.
② 수량 단위가 ton에서 kg로 통일되면서 금액 공식의 ``× 1000``이 사라졌다. 총액 7,125,000은
그대로다.)
"""

import inspect
import json
from datetime import date

import pytest
from _fixtures import AS_OF, _proposal
from pydantic import ValidationError

from app.purchase_agent import ports
from app.purchase_agent.config import CONSTRAINTS_PATH, load_constraints
from app.purchase_agent.schemas import PurchaseProposal, revalidate_for_output
from app.purchase_agent.state import build_initial_state

#: IO명세 §1이 규정한 계약 포트 6개. 이 목록이 곧 외부 입력 경계다.
CONTRACT_PORTS = (
    "get_forecast",
    "get_market_quotes",
    "get_inventory",
    "get_confirmed_orders",
    "get_projected_cash_min",
    "get_context_docs",
)

#: 계약 밖 잠정 포트. T0 스냅샷 형식 확정 전까지의 임시 경계다 (상세설계 §11 선행확인).
PROVISIONAL_PORTS = ("get_snapshot_extras",)

PORT_FUNCTIONS = (
    ports.get_forecast,
    ports.get_market_quotes,
    ports.get_inventory,
    ports.get_confirmed_orders,
    ports.get_projected_cash_min,
    ports.get_context_docs,
    ports.get_snapshot_extras,
)




# --------------------------------------------------------------------------- schemas


def test_valid_proposal_passes() -> None:
    proposal = PurchaseProposal.model_validate(_proposal())
    assert proposal.scenarios[0].total_qty_kg == 4500
    assert proposal.scenarios[0].total_amount_krw == 7125000
    assert proposal.meta.as_of == date(2026, 8, 21)


def test_quantity_must_match_split_plan() -> None:
    """사중 일치 수량 축 — total != Σsplit (규칙 4)."""
    data = _proposal()
    data["scenarios"][0]["split_plan"][0]["qty_kg"] = 4000
    with pytest.raises(ValidationError, match="split_plan quantity total"):
        PurchaseProposal.model_validate(data)


def test_quantity_must_match_sourcing_plan() -> None:
    """사중 일치 수량 축 — total != Σsourcing (규칙 4)."""
    data = _proposal()
    data["scenarios"][0]["sourcing_plan"][1]["qty_kg"] = 1000
    with pytest.raises(ValidationError, match="sourcing_plan quantity total"):
        PurchaseProposal.model_validate(data)


def test_amount_must_match_sourcing_plan() -> None:
    """사중 일치 금액 축 — total_amount != Σ(qty_kg x 등급단가) (규칙 4)."""
    data = _proposal()
    data["scenarios"][0]["total_amount_krw"] = 10318995  # sourcing 합계 7,125,000과 다른 값
    with pytest.raises(ValidationError, match="sourcing_plan amount total"):
        PurchaseProposal.model_validate(data)


def test_rationale_requires_ref_id() -> None:
    """근거에 ref_id가 없으면 근거가 아니다 (규칙 4 · 정의서 §1.2-5)."""
    data = _proposal()
    del data["scenarios"][0]["rationale"][0]["ref_id"]
    with pytest.raises(ValidationError, match="ref_id"):
        PurchaseProposal.model_validate(data)


def test_rationale_rejects_empty_ref_id() -> None:
    data = _proposal()
    data["scenarios"][0]["rationale"][0]["ref_id"] = ""
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


def test_evidence_grade_accepts_all_four_levels() -> None:
    """정의서 §7.3 4단계 전부 유효값이다."""
    for grade in ("OFFICIAL", "VENDOR", "SIM_FIXED", "ASSUMED"):
        data = _proposal()
        data["scenarios"][0]["rationale"][0]["evidence_grade"] = grade
        proposal = PurchaseProposal.model_validate(data)
        assert proposal.scenarios[0].rationale[0].evidence_grade == grade


def test_evidence_grade_rejects_unknown_level() -> None:
    data = _proposal()
    data["scenarios"][0]["rationale"][0]["evidence_grade"] = "GUESSED"
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


def test_uncertain_forbids_aggressive_scenario() -> None:
    """uncertain이면 공격안 금지 — 보수/기본 2안만 (규칙 4)."""
    data = _proposal()
    data["situation"] = "uncertain"
    data["scenarios"][0]["label"] = "공격"
    with pytest.raises(ValidationError, match="공격"):
        PurchaseProposal.model_validate(data)


def test_uncertain_allows_at_most_two_scenarios() -> None:
    """uncertain이면 최대 2안 (규칙 4).

    label 어휘가 3개뿐이라 3안이면 "공격"이 반드시 끼는데, 개수 검사가 먼저 걸리는지를
    match로 고정한다 — 3안이 거부된 이유가 "공격 포함"이 아니라 "개수 초과"여야 한다.
    """
    data = _proposal()
    data["situation"] = "uncertain"
    base = data["scenarios"][0]
    data["scenarios"] = [
        {**base, "label": "보수"},
        {**base, "label": "기본"},
        {**base, "label": "공격"},
    ]
    with pytest.raises(ValidationError, match="at most 2"):
        PurchaseProposal.model_validate(data)


def test_scenario_labels_must_be_unique() -> None:
    data = _proposal()
    data["scenarios"].append({**data["scenarios"][0]})
    with pytest.raises(ValidationError, match="unique"):
        PurchaseProposal.model_validate(data)


def test_empty_scenarios_require_no_proposal_reason() -> None:
    """제안 불가 응답은 사유가 있어야 한다 (IO명세 §2)."""
    data = _proposal()
    data["scenarios"] = []
    with pytest.raises(ValidationError, match="no_proposal_reason"):
        PurchaseProposal.model_validate(data)


def test_no_proposal_response_is_valid() -> None:
    proposal = PurchaseProposal.model_validate(
        {
            "meta": {"as_of": AS_OF, "item": "배추", "agent_version": "v1.1"},
            "scenarios": [],
            "no_proposal_reason": "재조정 2회 초과 — 제약 조합 하에 유효 시나리오 없음",
        }
    )
    assert proposal.scenarios == []


def test_market_is_fixed_to_garak() -> None:
    """가락시장 단일 (IO명세 §1-②)."""
    data = _proposal()
    data["scenarios"][0]["sourcing_plan"][0]["market"] = "구리"
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


def test_first_split_date_must_equal_as_of() -> None:
    """seq 1의 date = as_of (IO명세 §2)."""
    data = _proposal()
    data["scenarios"][0]["split_plan"][0]["date"] = "2026-08-22"
    with pytest.raises(ValidationError, match="seq 1 date"):
        PurchaseProposal.model_validate(data)


def test_split_seq_must_be_sequential() -> None:
    data = _proposal()
    scenario = data["scenarios"][0]
    scenario["split_plan"] = [
        {"seq": 1, "date": AS_OF, "qty_kg": 2500},
        {"seq": 3, "date": "2026-08-25", "qty_kg": 2000},
    ]
    with pytest.raises(ValidationError, match="seq must start at 1"):
        PurchaseProposal.model_validate(data)


def test_split_plan_supports_multiple_rounds() -> None:
    """분할이면 회차가 여러 개 — 합계만 맞으면 통과한다."""
    data = _proposal()
    data["scenarios"][0]["strategy_type"] = "timing"
    data["scenarios"][0]["split_plan"] = [
        {"seq": 1, "date": AS_OF, "qty_kg": 2500},
        {"seq": 2, "date": "2026-08-25", "qty_kg": 2000},
    ]
    assert len(PurchaseProposal.model_validate(data).scenarios[0].split_plan) == 2


def test_unknown_field_is_rejected() -> None:
    """extra=forbid — 계약에 없는 필드를 실어 보내지 않는다."""
    data = _proposal()
    data["scenarios"][0]["variant_axis"] = "quantity"  # v0.3 구 필드명
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


def test_boolean_is_rejected_for_numeric_field() -> None:
    """bool은 int의 서브클래스라 ge/gt를 통과한다 — 숫자 자리에서 막는다."""
    data = _proposal()
    data["scenarios"][0]["coverage_days"] = True
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


# --------------------------- Codex 교차 검증에서 드러난 우회 경로 (회귀 방지) ---


def test_json_serialization_emits_numbers_not_strings() -> None:
    """IO명세 §2 규약이 ``integer``/``number``다 — 문자열이 아니라 숫자로 나가야 한다."""
    payload = json.loads(PurchaseProposal.model_validate(_proposal()).model_dump_json())
    scenario = payload["scenarios"][0]

    assert isinstance(scenario["total_qty_kg"], int)
    assert scenario["total_qty_kg"] == 4500
    assert isinstance(scenario["total_amount_krw"], int)
    assert scenario["total_amount_krw"] == 7125000
    assert isinstance(scenario["split_plan"][0]["qty_kg"], int)
    assert isinstance(scenario["sourcing_plan"][0]["qty_kg"], int)


@pytest.mark.parametrize("value", [4500.5, "4500.5"], ids=["float", "str"])
def test_fractional_quantity_is_rejected(value: object) -> None:
    """**정수 kg — 소수 불허** (IO명세 §2 ``integer``).

    도매 매입 단위가 정수 kg이기도 하지만, 더 중요하게는 float 직렬화 오차의 원천을
    차단한다. 소수를 허용하면 ``0.3kg x 3원 = 0.9원``이 JSON을 거쳐 소비자에게서
    ``0.3 * 3 != 0.9``가 되어 사중 일치가 직렬화 경계 뒤에서 깨진다.
    """
    data = _proposal()
    data["scenarios"][0]["total_qty_kg"] = value
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


def test_python_dump_keeps_integers() -> None:
    """``model_dump()``도 int다 — Decimal 시절과 달리 숫자 변환 계층이 아예 없다."""
    dumped = PurchaseProposal.model_validate(_proposal()).model_dump()
    assert isinstance(dumped["scenarios"][0]["total_qty_kg"], int)
    assert isinstance(dumped["scenarios"][0]["total_amount_krw"], int)


@pytest.mark.parametrize("blank", ["   ", "\t", "\n "], ids=["spaces", "tab", "newline"])
def test_whitespace_only_ref_id_is_rejected(blank: str) -> None:
    """``min_length=1``은 공백을 통과시킨다 — ref_id 필수 조항이 우회되면 안 된다."""
    data = _proposal()
    data["scenarios"][0]["rationale"][0]["ref_id"] = blank
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


def test_ref_id_is_trimmed() -> None:
    data = _proposal()
    data["scenarios"][0]["rationale"][0]["ref_id"] = "  FC-K-0821  "
    proposal = PurchaseProposal.model_validate(data)
    assert proposal.scenarios[0].rationale[0].ref_id == "FC-K-0821"








def test_frozen_blocks_field_reassignment() -> None:
    """(a) 필드 재대입 — 예외뿐 아니라 **값이 오염되지 않았는지**까지 확인한다.

    이전 구현(``validate_assignment=True``)은 예외를 던지면서도 대입값을 남겼다.
    예외를 삼키는 호출자가 하나만 있어도 오염된 객체가 그대로 직렬화됐다. 예외만 보던
    테스트는 그 사실을 놓쳤고, "70건 통과"가 이 영역에서는 아무 보증이 아니었다.
    """
    proposal = PurchaseProposal.model_validate(_proposal())
    with pytest.raises(ValidationError):
        proposal.scenarios[0].total_qty_kg = 999

    assert proposal.scenarios[0].total_qty_kg == 4500
    assert json.loads(proposal.model_dump_json())["scenarios"][0]["total_qty_kg"] == 4500


def test_frozen_blocks_child_field_reassignment() -> None:
    """(b) 자식 필드 변경 — 부모 validator가 아예 재실행되지 않던 경로다."""
    proposal = PurchaseProposal.model_validate(_proposal())
    with pytest.raises(ValidationError):
        proposal.scenarios[0].sourcing_plan[0].qty_kg = 2999

    serialized = json.loads(proposal.model_dump_json())["scenarios"][0]
    assert serialized["total_qty_kg"] == sum(item["qty_kg"] for item in serialized["sourcing_plan"])


def test_list_mutation_is_caught_by_output_revalidation() -> None:
    """(c) 리스트 append는 frozen이 막지 못한다 — 출력 경계 재검증이 잡는다.

    ``frozen=True``는 필드 재대입만 막는다. 리스트 객체 자체는 여전히 가변이라
    ``rationale.append({...})``는 어떤 validator도 거치지 않는다.
    """
    proposal = PurchaseProposal.model_validate(_proposal())
    proposal.scenarios[0].rationale.append({"source": "예측", "claim": "ref_id 없는 근거"})

    with pytest.raises(ValidationError):
        revalidate_for_output(proposal)


def test_label_mutation_cannot_bypass_uncertain_rule() -> None:
    """(d) 검증 후 label을 바꿔 uncertain 규칙을 우회할 수 없다."""
    data = _proposal()
    data["situation"] = "uncertain"
    proposal = PurchaseProposal.model_validate(data)

    with pytest.raises(ValidationError):
        proposal.scenarios[0].label = "공격"
    assert proposal.scenarios[0].label == "기본"


def test_revalidation_passes_for_untouched_proposal() -> None:
    """정상 제안은 재검증을 그대로 통과한다 — 재검증이 과잉 차단하지 않는지 확인."""
    proposal = PurchaseProposal.model_validate(_proposal())
    assert revalidate_for_output(proposal).scenarios[0].total_qty_kg == 4500


def _without_margin(data: dict) -> dict:
    """계약단가를 못 받은 상태 — 마진 두 값이 **함께** 빠진다."""
    scenario = data["scenarios"][0]
    del scenario["margin_warning"]
    del scenario["expected_margin_rate"]
    return data


def test_margin_fields_default_to_none_together() -> None:
    """규칙 3 — 미계산(None)과 "확인했더니 문제없음"(False / 0.0)을 구분한다.

    ``margin_warning``은 bool 판, ``expected_margin_rate``는 float 판이다. 둘 다
    contract_price 파생이라 기본값도 함께 None이어야 한다 (IO명세 §2 v1.1 개정).
    """
    scenario = PurchaseProposal.model_validate(_without_margin(_proposal())).scenarios[0]
    assert scenario.margin_warning is None
    assert scenario.expected_margin_rate is None


def test_both_margin_fields_set_is_accepted() -> None:
    """계약단가를 받았으면 둘 다 값이 있다 — 정상 경로."""
    scenario = PurchaseProposal.model_validate(_proposal()).scenarios[0]
    assert scenario.margin_warning is False
    assert scenario.expected_margin_rate == 0.30


@pytest.mark.parametrize(
    ("dropped", "kept"),
    [("margin_warning", "expected_margin_rate"), ("expected_margin_rate", "margin_warning")],
)
def test_half_null_margin_pair_is_rejected(dropped: str, kept: str) -> None:
    """한쪽만 null이면 모순이다 — 소비자가 어느 쪽을 믿어야 할지 알 수 없다.

    둘 다 contract_price에서 나오므로 "계약단가를 받았는데 한쪽만 계산했다"는 상태는 없다
    (IO명세 §2 "동기화 규칙: 한쪽만 null이면 스키마 validator가 반려").
    """
    data = _proposal()
    del data["scenarios"][0][dropped]
    with pytest.raises(ValidationError, match="both be null or both set"):
        PurchaseProposal.model_validate(data)
    assert kept in data["scenarios"][0]


def test_half_null_margin_pair_is_rejected_when_written_as_explicit_null() -> None:
    """키를 지우는 것과 ``null``을 명시하는 것이 같은 판정을 받아야 한다."""
    data = _proposal()
    data["scenarios"][0]["expected_margin_rate"] = None
    with pytest.raises(ValidationError, match="both be null or both set"):
        PurchaseProposal.model_validate(data)


def test_normal_proposal_omits_no_proposal_reason_key() -> None:
    """정상 제안에는 키 자체가 없다 (IO명세 §2 정상 예시). null도 싣지 않는다."""
    payload = json.loads(PurchaseProposal.model_validate(_proposal()).model_dump_json())
    assert "no_proposal_reason" not in payload


def test_null_margin_pair_survives_serialization() -> None:
    """두 null은 "미계산"이라는 **정보**다 — 직렬화에서 사라지면 안 된다.

    키가 빠지면 소비자 쪽에서 "미계산"과 "필드 자체가 없는 구버전"이 구분되지 않는다.
    (``no_proposal_reason``은 반대로 null일 때 키를 지운다 — 그쪽은 없는 게 정상 상태다.)
    """
    payload = json.loads(
        PurchaseProposal.model_validate(_without_margin(_proposal())).model_dump_json()
    )
    scenario = payload["scenarios"][0]
    assert "margin_warning" in scenario
    assert scenario["margin_warning"] is None
    assert "expected_margin_rate" in scenario
    assert scenario["expected_margin_rate"] is None


def test_computed_margin_pair_survives_serialization() -> None:
    """값이 있을 때도 두 필드가 그대로 나간다 — 반쪽 직렬화가 없다."""
    payload = json.loads(PurchaseProposal.model_validate(_proposal()).model_dump_json())
    scenario = payload["scenarios"][0]
    assert scenario["margin_warning"] is False
    assert scenario["expected_margin_rate"] == 0.30


def test_no_proposal_reason_cannot_coexist_with_scenarios() -> None:
    """"안이 있는데 제안 불가"는 모순이다 — 상호 배타."""
    data = _proposal()
    data["no_proposal_reason"] = "제안 불가"
    with pytest.raises(ValidationError, match="must be absent"):
        PurchaseProposal.model_validate(data)


def test_blank_no_proposal_reason_is_rejected() -> None:
    data = _proposal()
    data["scenarios"] = []
    data["no_proposal_reason"] = "   "
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(data)


# --------------------------------------------------------------------------- constraints.yaml


def _constraints() -> dict:
    """로더를 거쳐 읽는다 — 노드가 쓸 경로와 테스트가 볼 경로를 하나로 묶는다."""
    return load_constraints()


def test_constraints_file_parses() -> None:
    assert isinstance(_constraints(), dict)


@pytest.mark.parametrize(
    "key",
    [
        "situation",
        "coverage_days",
        "triggers",
        "concentration",
        "variant",
        "costs",
        "grade",
        "demand",
        "warehouse",
        "context",
        "allocation",
        "shelf_life_days",
        "feedback",
        "pending",
    ],
)
def test_constraints_has_required_section(key: str) -> None:
    assert key in _constraints()


def test_coverage_days_mapping_matches_design() -> None:
    """상세설계 §7 — 보수 2 / 기본(기준) 5 / 공격 12, 범위 [2, 18]."""
    coverage = _constraints()["coverage_days"]
    assert coverage["min"] == 2
    assert coverage["max"] == 18
    assert coverage["by_label"] == {"보수": 2, "기본": 5, "공격": 12}


@pytest.mark.parametrize("key", ["inbound_lead_days", "purchase_payment_days"])
def test_pending_values_are_null_not_zero(key: str) -> None:
    """규칙 3 — 미결값은 NULL. 0으로 채우면 계산이 조용히 틀어진다."""
    assert _constraints()["pending"][key] is None


def test_unconfirmed_shelf_life_is_null() -> None:
    """단일 확정값이 있는 품목만 채운다."""
    shelf_life = _constraints()["shelf_life_days"]
    assert shelf_life["배추"] == 135
    assert shelf_life["양파"] is None
    assert shelf_life["무"] is None
    assert shelf_life["피마늘"] is None


def test_feedback_attempt_max_is_declared() -> None:
    """재시도 상한은 임계다 — 코드가 아니라 constraints.yaml에서 읽는다 (규칙 7)."""
    assert _constraints()["feedback"]["attempt_max"] == 2


def test_ci_width_boundary_is_explicit() -> None:
    """상세설계 §4-①: ``< 0.08`` stable / ``>= 0.08`` uncertain — 경계값을 못박는다."""
    situation = _constraints()["situation"]
    assert situation["ci_width_threshold"] == 0.08
    assert situation["ci_width_comparison"] == ">="


def test_ci_judgment_day_is_declared() -> None:
    """상세설계 §4-①: 판정 기준일 = D+14 단일. 코드에 박지 않는다 (규칙 7).

    "어느 날의 ci_width로 판정하는가"가 오래 미정이었고, 그동안 mock이 전 구간 밴드를
    고르게 유지해 우회했다. 이제 값이 있으므로 계약으로 고정한다.
    """
    situation = _constraints()["situation"]
    assert situation["ci_judgment_day"] == 14


def test_input_values_are_not_stored_as_constants() -> None:
    """계약단가·방어선은 재무·영업이 주는 **입력값**이지 매입 상수가 아니다 (§7 각주)."""
    text = CONSTRAINTS_PATH.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "contract_price:" not in body
    assert "margin_defense_floor_rate:" not in body


def test_baseline_spread_matches_the_normal_day_mock() -> None:
    """평시 기준선(SIM_FIXED 선언값)이 **실제 평시 mock 스프레드와 같은가**.

    과거 시세 이력 포트가 계약에 없어 상수로 선언했는데(§4-⑤ Epic 3 확정 1), 그 상수가
    자기가 대표한다고 주장하는 데이터와 어긋나면 "평시 대비 확대" 판정이 통째로 거짓이 된다.
    두 파일이 따로 놀 수 있는 유일한 지점이라 여기서 묶어둔다.
    """
    constraints = _constraints()
    mid_grade = constraints["grade"]["mid_grade"]
    top_grade = constraints["allocation"]["reference_grade"]
    normal_day = date(2026, 8, 21)  # quotes_normal이 붙은 앵커일 (scenarios.json)

    for item, baseline in constraints["grade"]["baseline_grade_spread"].items():
        prices = {q["grade"]: q["price"] for q in ports.get_market_quotes(item, normal_day)}
        observed = (prices[top_grade] - prices[mid_grade]) / prices[top_grade]
        assert observed == pytest.approx(baseline, rel=0.01), item


def test_mid_grade_scoring_weights_are_declared() -> None:
    """스코어 가중치는 코드가 아니라 파일이 갖는다 (규칙 7). 값 자체는 튜닝 대상이다."""
    weights = _constraints()["grade"]["score_weights"]
    assert set(weights) == {"price_gain", "freshness_risk"}
    assert all(value > 0 for value in weights.values())


# --------------------------------------------------------------------------- ports


def test_ports_module_exposes_the_contract_six_and_nothing_unlabelled() -> None:
    """IO명세 §1의 6개는 반드시 있고, 그 밖의 포트는 **잠정임이 명시**돼야 한다.

    개수만 세면 계약 포트가 하나 사라지고 다른 게 생겨도 통과한다. 이름으로 못 박는다.
    잠정 포트를 허용하는 이유: T0 스냅짓 형식이 아직 팀 미확정이라(§11 선행확인)
    ``get_snapshot_extras``가 §1 밖에 있다. 대신 docstring이 그 사실을 밝혀야 한다.
    """
    public = {
        name
        for name, value in vars(ports).items()
        if inspect.isfunction(value) and not name.startswith("_")
    }
    assert set(CONTRACT_PORTS) <= public
    assert public - set(CONTRACT_PORTS) == set(PROVISIONAL_PORTS), public


@pytest.mark.parametrize("name", PROVISIONAL_PORTS)
def test_provisional_port_declares_that_it_is_outside_the_contract(name: str) -> None:
    """잠정 포트는 "IO명세 §1의 계약 포트가 아니다"를 docstring에 적어야 한다."""
    doc = getattr(ports, name).__doc__ or ""
    assert "IO명세 §1" in doc
    assert "계약 포트가 아니다" in doc


@pytest.mark.parametrize("port", PORT_FUNCTIONS, ids=lambda fn: fn.__name__)
def test_every_port_requires_as_of(port: object) -> None:
    """규칙 1 — as_of 주입 없이는 어떤 외부 입력도 받지 않는다 (look-ahead 방어)."""
    parameters = inspect.signature(port).parameters
    assert "as_of" in parameters
    assert parameters["as_of"].annotation is date


def test_t0_snapshot_calls_ports_one_to_five_once_and_never_loads_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """호출 **위치**를 잠근다 — ①~⑤는 T0 only, ⑥ 문서만 ② 노드의 런타임 예외.

    ``ports.py``·``state.py`` docstring이 선언만 하고 아무도 검사하지 않던 경계다
    (정의서 §3.1.1 · 팀 확인 2026-08-25 · IO명세 §0). 실제로 한 번 어긋나 있었다 —
    docstring이 "6개 포트를 각각 한 번씩"이라고 적혀 있었는데 호출은 5개였다.

    이게 있어야 ② ``collect_context``를 구현하다 실수로 T0에서 문서를 당겨오는 걸 막는다.
    문서를 T0로 옮기면 "발행 시점 고정"이라는 예외의 안전 근거가 사라진다.
    """
    calls: dict[str, int] = {}

    def counted(name: str):
        original = getattr(ports, name)

        def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return original(*args, **kwargs)

        return wrapper

    for name in (*CONTRACT_PORTS, *PROVISIONAL_PORTS):
        monkeypatch.setattr(ports, name, counted(name))

    state = build_initial_state("배추", date(2026, 8, 21))

    assert "get_context_docs" not in calls  # ⑥은 T0에서 부르지 않는다
    t0_ports = [name for name in CONTRACT_PORTS if name != "get_context_docs"]
    assert {name: calls.get(name) for name in t0_ports} == dict.fromkeys(t0_ports, 1)
    # 계약 밖 잠정 포트도 T0 1회다 — 스냅샷 형식이 확정되면 이 줄이 함께 바뀐다
    assert calls.get("get_snapshot_extras") == 1
    # ② 노드가 아직 안 돌았으므로 문서 자리는 비어 있어야 한다 (빈 목록 = "아직 안 읽음")
    assert state["context_docs"] == []
