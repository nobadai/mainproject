"""마스터 포트 어댑터 (IO명세 §2-B · M-1 §5~§8).

**제1원칙: 기존 회귀는 한 건도 건드리지 않는다.** 어댑터는 payload를 State로 펴서 기존
그래프를 부르는 층이고, 어댑터를 거치지 않는 호출은 이 층 자체를 만나지 않는다.

**봉투 규칙을 재구현하지 않는다.** ``validate_reply``를 직접 돌려 findings가 0인지 본다 —
우리가 규칙을 베껴 쓰면 그 사본이 드리프트 지점이 되고, 규약이 바뀌었을 때 조용히 어긋난다.
"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.master.envelope import (
    AgentRequest,
    ExecutionContext,
    check_reasoning,
    validate_reply,
)
from app.purchase_agent import ports
from app.purchase_agent.adapter import (
    absorb_inventory,
    build_reasoning,
    purchase_port,
    validate_payload,
)
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.nodes.package_scenarios import split_quantities

# "2025-12-31" 은 통합 시연 앵커 (#73) — 재무·물류 DB 데이터가 이 날에만 있다.
ANCHORS = ["2025-12-31", "2026-08-21", "2026-08-28", "2026-09-04", "2026-09-11"]
ITEMS = ["배추", "무", "양파", "피마늘"]
UNCERTAIN = date(2026, 9, 4)
SPREAD_WIDE = date(2026, 9, 11)


def _finance(as_of: date, **over) -> dict:
    base = {
        "base_projected_cash_min": ports.get_projected_cash_min(as_of, 30),
        "margin_defense_floor_rate": 0.267,
        "finance_cap_amount_krw": 9_000_000,
        "purchase_payment_days": 7,  # N5 = 7 확정 (8/27 재무)
        "critical_payment_dates": [],
    }
    base.update(over)
    return base


def _inventory(item: str, as_of: date, **over) -> dict:
    """물류 payload. 어댑터 경로에서만 오는 키(``inbound_lead_days``·``cap_by_date``)를
    얹어 시험할 수 있게 finance와 같은 방식으로 연다 — mock에는 그 키들이 없다."""
    return {**ports.get_inventory(item, as_of), **over}


def _payload(item: str, as_of: date, **over) -> dict:
    extras = ports.get_snapshot_extras(item, as_of)
    payload = {
        "item": item,
        "constraints": {
            "finance": _finance(as_of, **over.pop("finance", {})),
            "inventory": _inventory(item, as_of, **over.pop("inventory", {})),
        },
        "forecast": ports.get_forecast(item, as_of),
        "confirmed_orders": ports.get_confirmed_orders(item, as_of, days=14),
        "policy_values": {
            "contract_price_krw": extras["contract_price"],
            "item_mix_ratio": extras["item_mix_ratio"],
        },
    }
    payload.update(over)
    return payload


def _request(item: str, as_of: date, *, mode: str = "GENERATE_SCENARIOS", **over) -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(f"REQ-{as_of.isoformat()}-{item}", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode=mode,  # type: ignore[arg-type]
        payload=_payload(item, as_of, **over),
    )


# ── 봉투 계약 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("as_of", ANCHORS)
@pytest.mark.parametrize("item", ITEMS)
def test_envelope_validation_is_clean_on_every_anchor(item: str, as_of: str) -> None:
    """**봉투 자신의 검증기**로 전 앵커·전 품목을 통과시킨다.

    E-BIND 4종 · run_id 일치 · E-PLAN-EMPTY · Evidence 양방향(누락·고아) · reasoning
    두 규칙이 **한 번에** 걸린다. 우리가 규칙을 베껴 쓰지 않고 이 함수를 부르는 이유다.
    """
    request = _request(item, date.fromisoformat(as_of))
    reply, metadata = purchase_port(request)
    assert validate_reply(request, reply, metadata) == ()


def test_binding_fields_round_trip() -> None:
    """E-BIND 4종은 요청 값을 **그대로** 되돌려준다."""
    request = _request("배추", SPREAD_WIDE)
    reply, metadata = purchase_port(request)
    assert reply.request_id == request.context.request_id
    assert reply.as_of == request.context.as_of
    assert reply.agent == "purchase"
    assert reply.mode == request.mode
    assert reply.run_id and reply.run_id == metadata.run_id  # E-BIND-RUN-ID


def test_run_id_is_deterministic_and_varies_by_call_seq() -> None:
    """같은 요청은 같은 ``run_id``다 — 난수·벽시계를 쓰지 않는다 (규칙 1).

    재호출(최대 2회)은 **다른 실행**이라 ``call_seq``로 갈린다.
    """
    first = purchase_port(_request("배추", SPREAD_WIDE))[0]
    again = purchase_port(_request("배추", SPREAD_WIDE))[0]
    assert first.run_id == again.run_id

    request = _request("배추", SPREAD_WIDE)
    second_call = AgentRequest(
        context=request.context, agent="purchase", mode=request.mode,
        call_seq=2, payload=request.payload,
    )
    assert purchase_port(second_call)[0].run_id != first.run_id


def test_scenarios_is_always_a_list_never_a_single_dict() -> None:
    """🔴 마스터는 ``Mapping``을 **빈 것으로 조용히 취급한다** (필요데이터 §2.2).

    단일 dict를 돌려주면 에러가 아니라 "안이 없는 날"이 되고, 미충족 납품이 있으면
    ``E5_NO_FEASIBLE_PLAN``까지 간다. 1안이어도 배열이어야 한다.
    """
    payload = purchase_port(_request("배추", SPREAD_WIDE))[0].payload
    assert isinstance(payload["scenarios"], list)
    assert not isinstance(payload["scenarios"], dict)
    assert payload["scenarios"], "정상 날에는 비어 있으면 안 된다"


def test_suggested_adjustments_stays_empty() -> None:
    """매입은 축 조정을 제안할 권한이 없다 — 하나라도 담으면 ``ContractViolation``."""
    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    assert reply.suggested_adjustments == ()


# ── allowed_axes Evidence — 게이트별 2건 ──────────────────────────────────


def _axes_evidence(reply) -> list:
    return [e for e in reply.evidences if e.claim == "allowed_axes"]


def test_allowed_axes_carries_one_evidence_per_gate() -> None:
    """축을 여닫는 게이트가 둘이라 **근거도 둘**이다 (현서님 회신 8/27).

    처음엔 "열린 축 개수"(2.0)를 실었는데 그건 **답의 길이를 세어 답이라고 적은 것**이라
    감사 가치가 없다. 나중에 *"왜 그날 timing이 열렸나"*를 보는 사람에게 2.0은 아무것도
    말하지 않는다. ``Evidence.value``의 용도는 **판정을 만든 근거 수치**다.
    """
    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    axes_ev = _axes_evidence(reply)
    # 신뢰도(CI) · 총량(VOL) · 편중(MIX) — 축을 여닫는 조건이 셋이라 근거도 셋이다.
    assert {e.ref_ids[0].split("-")[1] for e in axes_ev} == {"CI", "VOL", "MIX"}
    assert not any(e.unit == "count" for e in axes_ev), "개수(count) 방식은 폐기됐다"
    assert not any(e.value == float(len(reply.payload["allowed_axes"])) for e in axes_ev)


def test_confidence_gate_evidence_shares_the_situation_ref_id() -> None:
    """**판정 하나 = 근거 하나** (정의서 §4.2.2).

    하나의 신뢰도 판정이 개수·허용 축·분할 진입 셋을 동시에 결정하므로, ``situation``과
    ``allowed_axes``의 신뢰도 게이트는 같은 ``ref_id``를 가리킨다 — 추적하면 한 곳으로
    모인다.
    """
    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    situation = next(e for e in reply.evidences if e.claim == "situation")
    gate = next(e for e in _axes_evidence(reply) if e.ref_ids == situation.ref_ids)
    assert gate.value == situation.value, "같은 판정이면 같은 수치여야 한다"
    assert "구간폭" in gate.evidence_detail


def test_concentration_gate_evidence_uses_a_separate_ref_id() -> None:
    """편중은 **다른 게이트**라 ``ref_id``도 다르다.

    신뢰도와 같은 id를 쓰면 서로 다른 두 판정이 한 근거를 가리키게 된다.
    """
    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    situation = next(e for e in reply.evidences if e.claim == "situation")
    mix_gate = next(e for e in _axes_evidence(reply) if "MIX" in e.ref_ids[0])
    assert mix_gate.ref_ids != situation.ref_ids
    # 호출 품목이 아니라 **전 품목의 최대비**다 — mix 게이팅이 max()를 보기 때문
    assert mix_gate.value == pytest.approx(0.812)
    assert "mix 제외" in mix_gate.evidence_detail


def test_concentration_gate_uses_the_maximum_not_the_called_item() -> None:
    """편중 값은 **전 품목의 최대비**다 — 호출 품목의 비중이 아니다.

    ⚠️ 배추로만 시험하면 이 구분이 안 드러난다. mock에서 배추가 **호출 품목이자
    최대비**(0.812)라 두 계산이 같은 값을 내기 때문이다 — 변이를 넣어도 초록불이었다.
    무(0.081)로 부르면 갈린다: mix 게이팅이 *"어느 품목이든 편중됐나"*를 묻기 때문에
    무를 사는 날에도 배추의 편중이 축을 닫는다.
    """
    reply = purchase_port(_request("무", SPREAD_WIDE))[0]
    mix_gate = next(e for e in _axes_evidence(reply) if "MIX" in e.ref_ids[0])
    assert mix_gate.value == pytest.approx(0.812), "호출 품목(무 0.081)이 아니라 최대비"
    assert "mix" not in reply.payload["allowed_axes"]


def test_concentration_gate_evidence_is_present_when_mix_opens_too() -> None:
    """**열린 날에도 싣는다.**

    닫힘만 기록하면 *"왜 열렸나"*의 근거가 없어지고, 편중이 완화돼 mix가 부활한 날을
    설명할 수 없다. 값은 그대로 최대비이고 문장만 갈린다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    # 편중을 임계 아래로 낮춘 합성 입력 — mock에서는 배추가 늘 0.812라 이 경로가 안 밟힌다
    payload["policy_values"] = {
        **payload["policy_values"],
        "item_mix_ratio": {"배추": 0.30, "무": 0.25, "양파": 0.25, "피마늘": 0.20},
    }
    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    assert "mix" in reply.payload["allowed_axes"]
    mix_gate = next(e for e in _axes_evidence(reply) if "MIX" in e.ref_ids[0])
    assert mix_gate.value == pytest.approx(0.30)
    assert "mix 개방" in mix_gate.evidence_detail


@pytest.mark.parametrize(
    ("value", "threshold", "comparison", "expected"),
    [
        (0.08, 0.08, ">=", "≥"),  # 경계 — 예전엔 ">"를 찍어 거짓 문장이 됐다
        (0.09, 0.08, ">=", "≥"),
        (0.07, 0.08, ">=", "<"),
        (0.08, 0.08, ">", "≤"),  # 설정이 ">"면 경계는 성립하지 않는다
        (0.09, 0.08, ">", ">"),
    ],
)
def test_evidence_relation_never_states_a_false_comparison(
    value: float, threshold: float, comparison: str, expected: str
) -> None:
    """근거 문장의 부등호가 **경계에서도 참**이어야 한다.

    예전 구현은 ``'>' if value >= threshold``였다 — 정확히 임계와 같은 날 "0.080 > 0.08"
    이라고 적었는데 그건 거짓이다. 판정은 맞고 문장만 틀린 상태라 아무도 안 잡는다.

    방향을 하드코딩하지 않는 것도 같은 이유다 — ①이 ``ci_width_comparison``을 설정에서
    읽으므로(규칙 7), 문장이 방향을 따로 적으면 설정을 바꾼 날 문장만 옛 방향으로 남는다.
    """
    from app.purchase_agent.adapter import _relation

    assert _relation(value, threshold, comparison) == expected


def test_axes_evidence_reads_the_comparison_from_constraints() -> None:
    """실제 산출물의 부등호가 설정값 방향과 맞는가 (하드코딩 회귀)."""
    from app.purchase_agent.config import load_constraints

    constraints = load_constraints()
    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    situation = next(e for e in reply.evidences if e.claim == "situation")
    gate = next(e for e in _axes_evidence(reply) if e.ref_ids == situation.ref_ids)
    threshold = constraints["situation"]["ci_width_threshold"]
    # 9/11 배추는 stable(0.060 < 0.08)이라 "<"가 나와야 한다
    assert gate.value < threshold
    assert f"{gate.value:.3f} < {threshold}" in gate.evidence_detail


def test_concentration_detail_matches_the_gate_condition_at_the_boundary() -> None:
    """편중 게이트는 ``max < threshold``면 개방이다 — 경계는 **제외**다.

    임계와 정확히 같은 편중을 합성해 문장이 ``≥``인지 본다.
    """
    as_of = SPREAD_WIDE
    threshold = load_constraints()["concentration"]["item_threshold"]
    payload = _payload("배추", as_of)
    payload["policy_values"] = {
        **payload["policy_values"],
        "item_mix_ratio": {"배추": threshold, "무": 0.1, "양파": 0.1, "피마늘": 0.1},
    }
    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    assert "mix" not in reply.payload["allowed_axes"], "경계값은 제외다"
    mix_gate = next(e for e in _axes_evidence(reply) if "MIX" in e.ref_ids[0])
    assert f"≥ {threshold}" in mix_gate.evidence_detail
    assert "mix 제외" in mix_gate.evidence_detail


def test_volume_gate_evidence_explains_timing_opened_without_a_stable_situation() -> None:
    """🔴 **timing은 ``situation``과 무관하게도 열린다** (Codex 교차검증 P1).

    ``by_volume OR by_trend``인데 ``by_volume``은 추정 총량만 본다. 그래서 uncertain인
    날에도 물량이 크면 timing이 열린다 — CI 근거만 있으면 *"uncertain → timing 열림"*
    이라는 **없는 인과**를 주장하게 된다.

    현서님 회신이 물은 *"축을 닫는 다른 게이트가 있습니까?"*의 답이 이것이다.
    """
    as_of = UNCERTAIN
    payload = _payload("배추", as_of)
    payload["confirmed_orders"] = {**payload["confirmed_orders"], "total_kg": 300_000}
    payload["constraints"]["finance"] = {
        **payload["constraints"]["finance"],
        "finance_cap_amount_krw": 50_000_000,
    }
    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    assert reply.payload["situation"] == "uncertain"
    assert "timing" in reply.payload["allowed_axes"], "총량으로 열린다"

    ci_gate = next(e for e in _axes_evidence(reply) if "CI" in e.ref_ids[0])
    assert "선매입 궤적 차단" in ci_gate.evidence_detail
    assert "허용 축" not in ci_gate.evidence_detail, "구간폭이 축을 열었다고 주장하면 안 된다"

    vol_gate = next(e for e in _axes_evidence(reply) if "VOL" in e.ref_ids[0])
    assert vol_gate.unit == "kg"
    assert "총량 트리거 충족" in vol_gate.evidence_detail


def test_empty_item_mix_ratio_is_refused_not_recorded_as_zero() -> None:
    """빈 매핑을 통과시키면 **관측된 적 없는 최대비를 0.0으로 적는다** (규칙 3 위반).

    게이트는 빈 입력이면 mix를 안 열지만, 근거는 "0.000 < 0.7 → mix 제외"라는 **스스로
    모순된 문장**을 냈다 — 부등호는 개방 조건이 성립한다고 말하는데 판정은 제외다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["policy_values"] = {**payload["policy_values"], "item_mix_ratio": {}}
    assert "policy_values.item_mix_ratio" in validate_payload(payload, as_of)

    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    assert reply.runtime_status == "RUNTIME_NOT_READY"


def test_concentration_gate_survives_a_max_that_is_neither_first_nor_called() -> None:
    """``max()``가 아니라 **첫 항목**을 읽어도 통과하던 사각을 막는다 (Codex 교차검증 P2).

    앞선 테스트들은 배추가 늘 첫 항목이자 최대비라 ``next(iter(ratios))`` 변이를
    잡지 못했다. 최대비를 **마지막 항목의 다른 품목**에 두고, 호출은 배추로 한다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["policy_values"] = {
        **payload["policy_values"],
        "item_mix_ratio": {"배추": 0.10, "무": 0.15, "양파": 0.20, "피마늘": 0.55},
    }
    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    mix_gate = next(e for e in _axes_evidence(reply) if "MIX" in e.ref_ids[0])
    assert mix_gate.value == pytest.approx(0.55), "첫 항목(0.10)도 호출 품목(0.10)도 아니다"
    assert "mix" in reply.payload["allowed_axes"], "0.55 < 0.70 이므로 개방"


def test_null_margin_gets_no_fabricated_evidence() -> None:
    """``contract_price`` 미수령이면 마진이 ``null``이고 **근거도 만들지 않는다** (규칙 3).

    ⚠️ **봉투는 이걸 막지 않는다.** ``canonical_claim``은 값이 ``None``이어도 필드가
    존재하면 경로를 인정하므로, ``0.0``을 지어내 붙여도 ``validate_reply``는 깨끗하다
    (Codex 교차검증 P2). 미결을 0으로 채우지 않는 것은 **우리 쪽 한 줄이 유일한 방어**라
    여기서 직접 잠근다.
    """
    from dataclasses import replace as dc_replace

    from app.orchestrator.contracts_core import Evidence

    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["policy_values"] = {**payload["policy_values"], "contract_price_krw": None}
    request = AgentRequest(
        context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=payload,
    )
    reply, metadata = purchase_port(request)

    scenario = reply.payload["scenarios"][0]
    assert scenario["expected_margin_rate"] is None
    assert scenario["margin_warning"] is None, "두 값은 함께 null이다 (IO명세 §2)"
    assert not [e for e in reply.evidences if e.claim.endswith(".expected_margin_rate")]
    assert validate_reply(request, reply, metadata) == ()

    # 봉투가 못 잡는다는 사실 자체를 고정한다 — 이 단언이 깨지면 봉투가 강화된 것이고,
    # 그때는 위 방어를 봉투에 맡길 수 있다.
    fabricated = dc_replace(
        reply,
        evidences=(
            *reply.evidences,
            Evidence(
                claim="scenarios[0].expected_margin_rate",
                source="tool_calc",
                ref_ids=("X",),
                value=0.0,
                unit="ratio",
                evidence_grade="SIM_FIXED",
                evidence_detail="지어낸 0.0",
            ),
        ),
    )
    assert validate_reply(request, fabricated, metadata) == (), (
        "봉투가 잡기 시작했다면 adapter 주석과 이 테스트를 갱신할 것"
    )


def test_envelope_stays_clean_when_every_scenario_is_cut() -> None:
    """**안이 0개인 날도 봉투를 통과한다** (Codex 교차검증 P3).

    빈 배열에 stale 경로 근거가 남으면 `E-EVIDENCE-ORPHAN`이다 — 안이 사라졌는데
    ``scenarios[0].*`` 근거만 남는 상태를 막는다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of, finance={"finance_cap_amount_krw": 0})
    request = AgentRequest(
        context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=payload,
    )
    reply, metadata = purchase_port(request)
    assert reply.payload["scenarios"] == []
    assert not [e for e in reply.evidences if e.claim.startswith("scenarios[")]
    assert validate_reply(request, reply, metadata) == ()


def test_envelope_stays_clean_with_a_single_scenario() -> None:
    """안이 **1개**인 날의 인덱스 처리를 고정한다 (Codex 교차검증 P3).

    ⚠️ **재무 상한으로는 1안을 만들 수 없다.** 상한은 안을 없애는 게 아니라 **수량을
    깎으므로** 셋 다 살아남는다(cap 300만~1,100만 전부 3안, 0원일 때만 0안). 그래서
    실제 산출물의 안 목록을 하나로 줄여 **근거 생성과 봉투 검증만** 시험한다 —
    마스터는 1안이어도 배열로 받아 ``single_option``으로 표시한다.
    """
    from dataclasses import replace as dc_replace

    from app.purchase_agent.adapter import build_evidences, build_reasoning

    request = _request("배추", SPREAD_WIDE)
    reply, metadata = purchase_port(request)
    final_state = {"forecast": request.payload["forecast"], "item_mix_ratio":
                   request.payload["policy_values"]["item_mix_ratio"],
                   "confirmed_orders": request.payload["confirmed_orders"]}

    single = {**reply.payload, "scenarios": reply.payload["scenarios"][:1]}
    trimmed = dc_replace(
        reply,
        payload=single,
        evidences=build_evidences(final_state, single),
        reasoning=build_reasoning(single),
    )
    assert isinstance(trimmed.payload["scenarios"], list)
    paths = {e.claim for e in trimmed.evidences if e.claim.startswith("scenarios[")}
    assert paths and all(c.startswith("scenarios[0].") for c in paths), (
        "1안이면 인덱스는 0뿐이다 — stale한 [1]·[2] 경로가 남으면 고아 근거다"
    )
    assert validate_reply(request, trimmed, metadata) == ()

# ── used_tools ────────────────────────────────────────────────────────────


def test_used_tools_includes_document_loading_only_on_uncertain_days() -> None:
    """조건부 노드가 **실행된 날만** 담긴다 (전달_2차 §2).

    ``collect_market_context``가 빠진 날이 *"불확실한 날에만 문서를 읽는다"*를 이력으로
    보여 주는 값이다. 그래프의 조건부 간선이 그 사실을 만든다.
    """
    uncertain = purchase_port(_request("배추", UNCERTAIN))[1]
    stable = purchase_port(_request("배추", SPREAD_WIDE))[1]
    assert "collect_market_context" in uncertain.used_tools
    assert "collect_market_context" not in stable.used_tools


def test_used_tools_order_matches_and_never_repeats_a_tool() -> None:
    """``tool_order``는 ``used_tools``와 **길이가 같아야 한다**.

    ⑥ 조립과 ⑦ 자기검증은 같은 Tool이라 노드로는 둘인데 Tool로는 하나다 — 중복이
    남으면 길이가 어긋나 ``ContractViolation``이 난다.
    """
    metadata = purchase_port(_request("배추", SPREAD_WIDE))[1]
    assert len(metadata.tool_order) == len(metadata.used_tools)
    assert len(set(metadata.used_tools)) == len(metadata.used_tools)
    assert metadata.used_tools[0] == "assess_market_situation"
    assert metadata.used_tools[-1] == "compose_and_verify_scenarios"


def test_recorder_is_absent_on_the_direct_path() -> None:
    """어댑터를 거치지 않는 호출은 **기록기 층을 만나지 않는다** — 949건 불변의 근거."""
    from app.purchase_agent.graph import build_graph
    from app.purchase_agent.tracing import wrap

    node = object()
    assert wrap(node, "x", None) is node  # 감싸지 않고 원본 그대로
    assert build_graph() is not None  # recorder 없이도 조립된다


# ── 수신 검증 ─────────────────────────────────────────────────────────────


def test_missing_forecast_key_reports_runtime_not_ready() -> None:
    """마스터가 오염 판정으로 **키를 아예 싣지 않은** 경우 (필요데이터 §1.3-②)."""
    request = _request("배추", SPREAD_WIDE)
    stripped = {k: v for k, v in request.payload.items() if k != "forecast"}
    request = AgentRequest(
        context=request.context, agent="purchase", mode=request.mode, payload=stripped
    )
    reply, _ = purchase_port(request)
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert "forecast" in reply.missing_data


@pytest.mark.parametrize(
    "drop", ["item", "forecast", "constraints", "confirmed_orders", "policy_values"]
)
def test_a_not_ready_reply_carries_no_payload_at_all(drop: str) -> None:
    """🔴 **"안 돌았다"에는 제안 형태를 싣지 않는다.**

    전에는 ``{"scenarios": []}``였다. 반쪽짜리 제안이라 ``PurchaseProposal``로 파싱하면
    깨지고(``meta``·``no_proposal_reason`` 부재), 온전히 채우면 이번엔 **"돌았는데 안이
    없다"와 구분되지 않는다** — 그쪽은 ``READY`` + ``no_proposal_reason``으로 이미 따로
    있다(아래 검사가 그 상태를 잠근다). ``runtime_status``를 안 보는 소비자가 하나라도
    생기면 두 상태가 같아진다.

    재무·물류도 이 자리에 payload를 안 싣는다(둘 다 ``_not_ready()``). 무엇이 없는지는
    ``missing_data``가, 왜인지는 ``reasoning``이 말한다.
    """
    request = _request("배추", SPREAD_WIDE)
    stripped = {k: v for k, v in request.payload.items() if k != drop}
    reply, _ = purchase_port(
        AgentRequest(
            context=request.context, agent="purchase", mode=request.mode, payload=stripped
        )
    )

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.payload == {}, f"안 돌았는데 뭔가 실렸다: {reply.payload}"
    assert reply.missing_data, "무엇이 없는지는 이름으로 남아야 한다"
    assert reply.reasoning


def test_the_two_empty_states_are_told_apart_by_payload_alone() -> None:
    """🔴 **payload만 봐도 두 상태가 갈린다** — ``runtime_status``를 안 봐도.

    ``READY`` + 0안은 *"돌았는데 안이 없다"* 라서 왜인지를 싣는다.
    ``RUNTIME_NOT_READY``는 *"안 돌았다"* 라서 아무것도 안 싣는다.
    """
    as_of = SPREAD_WIDE
    ran, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=_payload("배추", as_of, finance={"finance_cap_amount_krw": 0}),
        )
    )
    request = _request("배추", as_of)
    did_not_run, _ = purchase_port(
        AgentRequest(
            context=request.context,
            agent="purchase",
            mode=request.mode,
            payload={k: v for k, v in request.payload.items() if k != "forecast"},
        )
    )

    assert ran.payload["scenarios"] == [] and ran.payload["no_proposal_reason"]
    assert did_not_run.payload == {}


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        ({"generated_at": None}, "forecast.generated_at"),
        ({"generated_at": "2026-09-99"}, "forecast.generated_at"),
        ({"daily": []}, "forecast.daily"),
    ],
)
def test_forecast_receipt_checks_catch_what_master_lets_through(
    mutate: dict, expected: str
) -> None:
    """마스터는 ``generated_at``이 **없으면 판단하지 않고 그대로 싣는다.**

    그 구멍이 여기서 막힌다. 누수는 에러를 내지 않고 손익만 좋아지므로 양쪽에서 본다.
    """
    as_of = SPREAD_WIDE
    forecast = {**ports.get_forecast("배추", as_of), **mutate}
    missing = validate_payload({**_payload("배추", as_of), "forecast": forecast}, as_of)
    assert expected in missing


def test_forecast_day_axis_must_start_at_the_day_after_as_of() -> None:
    """🔴 판정 기준일이 ``daily[13]``이라 축이 하루만 밀려도 **다른 날을 본다.**

    에러가 나지 않아 아무도 모르는 종류의 오류다.
    """
    as_of = SPREAD_WIDE
    forecast = ports.get_forecast("배추", as_of)
    shifted = [{**row, "date": (date.fromisoformat(row["date"]) + timedelta(days=1)).isoformat()}
               for row in forecast["daily"]]
    missing = validate_payload(
        {**_payload("배추", as_of), "forecast": {**forecast, "daily": shifted}}, as_of
    )
    assert "forecast.daily" in missing


def test_scalar_item_mix_ratio_is_refused() -> None:
    """스칼라로 오면 mix 게이팅의 ``max()``가 성립하지 않는다 (답변 §4-4)."""
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["policy_values"] = {**payload["policy_values"], "item_mix_ratio": 0.812}
    assert "policy_values.item_mix_ratio" in validate_payload(payload, as_of)


@pytest.mark.parametrize(
    ("drop", "expected"),
    [
        ("base_projected_cash_min", "constraints.finance.base_projected_cash_min"),
        # cap이 없으면 mock 폴백(60% 비율)을 타는데, 어댑터 경로에서 그 길로 가면
        # B6("같은 목적 60% 재적용 금지")가 조용히 되살아난다 (Codex 교차검증 P1).
        ("finance_cap_amount_krw", "constraints.finance.finance_cap_amount_krw"),
    ],
)
def test_finance_leaf_keys_are_required_not_just_the_container(drop: str, expected: str) -> None:
    """**컨테이너 모양만 보면 안 된다.**

    ``constraints.finance``가 dict이기만 하면 통과시켰더니 ``build_state``가
    ``KeyError``로 죽었다. 죽으면 *"무엇이 없는지"*가 ``missing_data``에 남지 않아
    마스터가 사용자에게 요청할 대상을 모른다 — 계약이 막으려던 상태가 그대로 된다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["constraints"]["finance"] = {
        k: v for k, v in payload["constraints"]["finance"].items() if k != drop
    }
    assert expected in validate_payload(payload, as_of)

    request = AgentRequest(
        context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=payload,
    )
    reply, _ = purchase_port(request)  # 터지지 않고 이름을 담아 돌아온다
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert expected in reply.missing_data


@pytest.mark.parametrize("key", ["warehouse_free_kg", "rental_cap_kg"])
def test_inventory_leaf_keys_are_required(key: str) -> None:
    """물류 **필드명이 미확정**이라 이름이 어긋난 payload가 실제로 올 수 있다.

    그때 ``warehouse_cap_kg``가 ``KeyError``로 죽으면 마스터는 *"물류 이름이 다르다"*를
    알 길이 없다. ``lots``는 빠져도 돌아가므로(단일 등급으로 강등) 필수가 아니다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["constraints"]["inventory"] = {
        k: v for k, v in payload["constraints"]["inventory"].items() if k != key
    }
    assert f"constraints.inventory.{key}" in validate_payload(payload, as_of)


@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        (True, "@수량이어야 한다"),
        ("1000", "@수량이어야 한다"),
        ([1], "@수량이어야 한다"),
        (float("nan"), "@유한한 수여야 한다"),
        (-500, "@음수일 수 없다"),
    ],
    ids=["bool", "문자열", "리스트", "NaN", "음수"],
)
@pytest.mark.parametrize("key", ["warehouse_free_kg", "rental_cap_kg"])
def test_capacity_of_wrong_shape_answers_with_a_reason(
    key: str, bad: object, expected: str
) -> None:
    """🔴 **값이 와도 수가 아니면 사유를 내고 멈춘다** — 죽지 않는다.

    부재만 보던 자리다. 값이 있으면 그대로 ``warehouse_cap_kg``까지 흘러갔고 거기서
    ``TypeError``로 죽었다 — 죽으면 ``missing_data``가 비어 마스터는 *"무엇을 다시
    달라고 해야 하는지"*를 모른다. ``True``는 더 나쁘다: 죽지도 않고 **창고 상한
    1kg**이 되어 전 안이 눌린다 (2026-08-31 확인).

    로트 ``shelf_life_days``·``inbound_lead_days``와 같은 종류의 값이라 같은 자리에서
    같은 모양으로 막는다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["constraints"]["inventory"][key] = bad

    request = AgentRequest(
        context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=payload,
    )
    reply, _ = purchase_port(request)  # 터지지 않는다

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    named = [m for m in reply.missing_data if m.startswith(f"constraints.inventory.{key}")]
    assert named, reply.missing_data
    assert expected in named[0], named


@pytest.mark.parametrize("key", ["warehouse_free_kg", "rental_cap_kg"])
def test_confirmed_zero_capacity_is_not_a_shape_problem(key: str) -> None:
    """**확정된 0은 통과한다** (규칙 3).

    ``rental_cap_kg``는 2026-08-27 물류 회신 §1로 0 확정이다. 0을 모양 문제로 잡으면
    정상 payload가 ``RUNTIME_NOT_READY``로 막힌다 — 창고 상한이 그만큼 작다는 **사실**을
    값이 안 온 것으로 바꿔 읽는 셈이다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["constraints"]["inventory"][key] = 0
    assert not [
        m for m in validate_payload(payload, as_of) if m.startswith(f"constraints.inventory.{key}")
    ]


def test_empty_inventory_reports_names_instead_of_crashing() -> None:
    """빈 dict가 와도 터지지 않고 **무엇이 없는지**를 담아 돌아온다."""
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["constraints"]["inventory"] = {}
    request = AgentRequest(
        context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=payload,
    )
    reply, _ = purchase_port(request)
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert "constraints.inventory.warehouse_free_kg" in reply.missing_data


def test_confirmed_zero_is_honoured_not_treated_as_missing() -> None:
    """**확정된 0은 미결이 아니다** (규칙 3) — 폴백하지 않고 그대로 상한 0으로 쓴다.

    재무 cap이 0이면 살 수 있는 금액이 없다는 **사실**이므로 전 안이 컷되고, 왜 없는지는
    ``rejected_reasons``에 남는다. ``RUNTIME_NOT_READY``가 아니다 — 값이 안 온 것이
    아니라 온 값이 0이다.
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of, finance={"finance_cap_amount_krw": 0})
    assert not [m for m in validate_payload(payload, as_of) if "finance" in m]
    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    assert reply.runtime_status == "READY"
    assert reply.payload["scenarios"] == []
    assert reply.payload["rejected_reasons"], "왜 안이 없는지는 남아야 한다"


def test_finance_cap_makes_base_cash_irrelevant_to_the_ceiling() -> None:
    """cap이 오면 상한은 **cap 하나로** 정해진다 — ``base_projected_cash_min``은 안 쓰인다.

    이게 B6("같은 목적 60% 재적용 금지")의 실제 모습이다. 처음 이 테스트를 쓸 때
    *"base가 0이면 안이 없어지겠지"*라고 가정했는데, cap이 살아 있으면 그렇지 않다.
    """
    zero_base = purchase_port(
        _request("배추", SPREAD_WIDE, finance={"base_projected_cash_min": 0})
    )[0].payload
    assert zero_base["scenarios"], "cap이 있으면 base가 0이어도 안이 나온다"


@pytest.mark.parametrize(
    ("path", "key"),
    [("finance", "margin_defense_floor_rate"), ("policy", "contract_price_krw")],
)
def test_nullable_contract_values_are_not_treated_as_missing(path: str, key: str) -> None:
    """**null이 정상인 값을 필수로 걸면 정상 요청이 막힌다** (Codex 교차검증 P1).

    - ``margin_defense_floor_rate``: 재무가 ``READY``인 채로 null을 줄 수 있고, 어느
      노드도 이 값을 쓰지 않는다 — 참조값으로 실려만 간다.
    - ``contract_price_krw``: 미수령이면 ``None``이고, 그때 ``margin_warning``·
      ``expected_margin_rate``가 **함께 null**로 나가는 것이 계약이다 (IO명세 §2).
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    if path == "finance":
        payload["constraints"]["finance"] = {**payload["constraints"]["finance"], key: None}
    else:
        payload["policy_values"] = {**payload["policy_values"], key: None}
    assert not [m for m in validate_payload(payload, as_of) if key in m]

    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    assert reply.runtime_status == "READY"


def test_missing_item_is_reported_instead_of_crashing() -> None:
    """품목 없이 오면 ``KeyError``가 아니라 ``missing_data``다."""
    as_of = SPREAD_WIDE
    payload = {k: v for k, v in _payload("배추", as_of).items() if k != "item"}
    assert "item" in validate_payload(payload, as_of)


def test_missing_advisor_constraints_report_their_names() -> None:
    """조언자가 ``READY``를 못 내면 **키 자체가 없다** — 빈 dict가 아니다."""
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload["constraints"] = {"finance": payload["constraints"]["finance"]}
    assert "constraints.inventory" in validate_payload(payload, as_of)


# ── Codex 교차검증 회귀 ───────────────────────────────────────────────────


def test_forecast_axis_shifted_from_the_middle_is_caught() -> None:
    """**첫 행만 보면 안 된다** (Codex 교차검증 P1).

    ``daily[0]``만 D+1로 맞춰 두고 이후를 하루씩 밀면 예전 검사는 통과했다. 판정 기준일을
    **배열 인덱스**로 고르므로 중간부터 밀리면 D+15를 D+14로 착각한 채 조용히 돈다.
    """
    as_of = SPREAD_WIDE
    forecast = ports.get_forecast("배추", as_of)
    rows = [dict(row) for row in forecast["daily"]]
    for row in rows[1:]:  # 첫 행은 그대로 두고 나머지만 민다
        row["date"] = (date.fromisoformat(row["date"]) + timedelta(days=1)).isoformat()
    payload = {**_payload("배추", as_of), "forecast": {**forecast, "daily": rows}}
    assert "forecast.daily" in validate_payload(payload, as_of)


@pytest.mark.parametrize(
    ("drop_from", "key", "expected"),
    [
        ("forecast", "model_version", "forecast.model_version"),
        ("forecast", "current_price", "forecast.current_price"),
        ("confirmed_orders", "total_kg", "confirmed_orders.total_kg"),
        ("confirmed_orders", "orders", "confirmed_orders.orders"),
    ],
)
def test_graph_consumed_leaf_keys_are_validated(drop_from: str, key: str, expected: str) -> None:
    """검증을 통과한 뒤 **그래프 안에서 KeyError로 죽는** 하위 키를 막는다.

    죽으면 ``missing_data``가 비어 마스터가 무엇을 요청해야 할지 모른다 — 계약이
    막으려던 상태가 그대로 된다 (Codex 교차검증 P1).
    """
    as_of = SPREAD_WIDE
    payload = _payload("배추", as_of)
    payload[drop_from] = {k: v for k, v in payload[drop_from].items() if k != key}
    assert expected in validate_payload(payload, as_of)

    reply, _ = purchase_port(
        AgentRequest(
            context=ExecutionContext("R", as_of, "ML_COMPLETE", "v2.3"),
            agent="purchase",
            mode="GENERATE_SCENARIOS",
            payload=payload,
        )
    )
    assert reply.runtime_status == "RUNTIME_NOT_READY"  # 예외가 아니라 이름을 담아 돌아온다


def test_cash_rationale_describes_the_path_that_actually_set_the_ceiling() -> None:
    """**근거가 산출물과 어긋나면 안 된다** (Codex 교차검증 P1).

    재무 cap을 받는 날에도 "최저 현금의 60%"라고 쓰고 있었는데, 그 문장이 주장하는
    금액보다 실제 안이 클 수 있었다. ``evidence_detail``도 mock이라고 기록했다.
    """
    received = purchase_port(
        _request("배추", SPREAD_WIDE, finance={"finance_cap_amount_krw": 30_000_000})
    )[0].payload
    cash = next(r for r in received["scenarios"][-1]["rationale"] if r["source"] == "현금")
    assert "재무 매입 상한" in cash["claim"]
    assert "finance_cap_amount_krw" in cash["evidence_detail"]
    assert "mock" not in cash["evidence_detail"]
    assert received["scenarios"][-1]["total_amount_krw"] <= 30_000_000

    # cap이 없는 mock 경로는 종전 문장 그대로
    direct = run_purchase_agent("배추", SPREAD_WIDE)
    cash = next(r for r in direct["scenarios"][-1]["rationale"] if r["source"] == "현금")
    assert "60%" in cash["claim"]


def test_execution_metadata_carries_the_real_llm_state() -> None:
    """risks와 메타데이터가 **서로를 부정하면 안 된다** (Codex 교차검증 P1).

    LLM이 fallback했는데 메타데이터가 ``DISABLED``·fallback ``false``로 나가면 실행
    재현 계약이 깨진다. ``llm_*``는 payload가 아니라 ``ExecutionMetadata``다 (M-1 §6).
    """
    from app.purchase_agent.adapter import _metadata
    from app.purchase_agent.llm.mix import MixDecision

    # 회귀 스위트는 conftest가 LLM을 꺼 두므로(``PURCHASE_LLM_ENABLED=false`` — API 키
    # 없이 전건이 돌아야 한다) 실행 경로의 값은 DISABLED다.
    _, metadata = purchase_port(_request("배추", SPREAD_WIDE))
    assert metadata.llm_status == "DISABLED"
    assert metadata.llm_fallback_used is False

    # **매핑 자체를 따로 시험한다.** ⑤가 fallback을 실어 보냈는데 메타데이터가 DISABLED로
    # 나가면 risks("판단자 응답 실패")와 서로를 부정한다.
    request = _request("배추", SPREAD_WIDE)
    state = {
        "sourcing_plan": [
            {
                "decision": {
                    "mix": MixDecision(
                        candidate_id="MID_HALF",
                        reason="규칙 기본안",
                        llm_status="FALLBACK",
                        llm_model="claude-haiku-4-5-20251001",
                        llm_fallback_used=True,
                    )
                }
            }
        ]
    }
    carried = _metadata(request, None, tools=("assess_market_situation",), state=state)
    assert carried.llm_status == "FALLBACK"
    assert carried.llm_fallback_used is True
    assert carried.llm_model == "claude-haiku-4-5-20251001"


def test_reasoning_reports_the_axes_that_are_actually_open() -> None:
    """**서술문이 산출물과 어긋나면 안 된다** (Codex 교차검증 P2).

    stable이면 무조건 "세 축을 모두 열었다"고 썼는데, 배추는 편중 게이팅으로 mix가
    닫혀 두 축뿐이다. 봉투의 숫자·문장 검사는 이 종류의 불일치를 잡지 못한다.
    """
    payload = purchase_port(_request("배추", SPREAD_WIDE))[0].payload
    text = build_reasoning(payload)
    assert payload["allowed_axes"] == ["quantity", "timing"]
    assert "mix" not in text
    for axis in payload["allowed_axes"]:
        assert axis in text


# ── N5 이중 경로 ──────────────────────────────────────────────────────────


def _n5_notes(risks: list[str]) -> list[str]:
    return [note for note in risks if "N5" in note]


def test_n5_deferred_on_the_mock_path_and_restored_on_the_adapter_path() -> None:
    """**같은 값이 경로마다 다르다** — 수신값이 설정값을 이긴다.

    mock 경로는 ``constraints.yaml``의 ``null``을 그대로 보고 지급일 계산을 보류하고,
    어댑터 경로는 재무가 준 7을 받아 계산한다. B6(재무 cap)과 같은 이중 경로 패턴이다.
    """
    direct = run_purchase_agent("배추", SPREAD_WIDE)
    assert _n5_notes(direct["scenarios"][0]["risks"]), "mock 경로는 보류 고지가 남는다"

    received = purchase_port(_request("배추", SPREAD_WIDE))[0].payload
    assert not _n5_notes(received["scenarios"][0]["risks"]), "N5를 받으면 보류가 풀린다"


def _n4_notes(risks: list[str]) -> list[str]:
    return [note for note in risks if "N4" in note]


def test_n4_deferred_on_the_mock_path_and_restored_when_logistics_sends_it() -> None:
    """N5와 같은 이중 경로다. **다만 N4는 물류 payload에서 온다** (재무가 아니다).

    이 배선이 없으면 값이 ``state["inventory"]`` 안에는 들어와 있는데
    ``pending_value``가 State **최상위**를 봐서 못 찾고, 실연동에서도 "N4 미확정"을
    고지한다 — 화면에 사실이 아닌 문장이 나가던 자리다 (#58 선행).
    """
    direct = run_purchase_agent("배추", SPREAD_WIDE)
    assert _n4_notes(direct["scenarios"][0]["risks"]), "mock 경로는 보류 고지가 남는다"

    received = purchase_port(
        _request("배추", SPREAD_WIDE, inventory={"inbound_lead_days": 2})
    )[0].payload
    assert not _n4_notes(received["scenarios"][0]["risks"]), "N4를 받으면 보류가 풀린다"


def test_n4_zero_is_a_received_value_not_a_missing_one() -> None:
    """``0``은 "당일 도착"이라는 **확정된 값**이다 (규칙 3).

    ``or``로 폴백하면 0이 falsy라 미결로 되돌아가고, "받았는데 못 받은 것으로 친다"가 된다.
    """
    received = purchase_port(
        _request("배추", SPREAD_WIDE, inventory={"inbound_lead_days": 0})
    )[0].payload
    assert not _n4_notes(received["scenarios"][0]["risks"])


def test_split_quantities_and_the_risk_note_never_disagree() -> None:
    """⑥은 수량을 재배분하고 ``_split_risks``는 **같은 계산을 다시** 한다.

    두 곳이 갈라지면 화면에 "재배분했다"고 적힌 옆에 균등 수량이 뜬다 — 어느 쪽이
    사실인지 소비자가 알 수 없다. 순수 함수라 갈라질 수 없다는 전제를 못박는다.
    """
    # 공격안(D=12·2회차)의 실제 도착일이다 — 매입일 09-11·09-17 에 N4 2 를 더한 값.
    # 1회차 상한을 낮게 걸어 **재배분이 실제로 일어나게** 한다.
    cap = {"2026-09-13": 1_000, "2026-09-19": 100_000}
    received = purchase_port(
        _request("배추", SPREAD_WIDE, inventory={"inbound_lead_days": 2, "cap_by_date": cap})
    )[0].payload

    split_scenarios = [s for s in received["scenarios"] if len(s["split_plan"]) > 1]
    assert split_scenarios, "분할 안이 없으면 이 검사가 아무것도 안 본다"
    reapportioned = 0
    for scenario in split_scenarios:
        quantities = [line["qty_kg"] for line in scenario["split_plan"]]
        assert sum(quantities) == scenario["total_qty_kg"], "사중 일치"
        assert all(qty > 0 for qty in quantities), "0kg 회차는 스키마가 죽인다"
        notes = [note for note in scenario["risks"] if "회 분할" in note]
        assert len(notes) == 1

        # 균등이었다면 나왔을 수량. **양방향으로** 대조한다 — 한 방향만 보면
        # 고지가 "보류"로 빠지는 변이를 못 잡는다.
        rounds = len(quantities)
        equal = split_quantities(scenario["total_qty_kg"], [{"ratio": 1 / rounds}] * rounds)
        if quantities != equal:
            reapportioned += 1
            assert "맞춰 옮겼다" in notes[0], "조정했는데 고지는 다른 말을 한다"
            for line in scenario["split_plan"]:
                assert f"{line['seq']}회 " in notes[0]
                assert f"{line['qty_kg']:,}kg" in notes[0]
        else:
            assert "맞춰 옮겼다" not in notes[0], "조정 안 했는데 했다고 적는다"

    assert reapportioned, "재배분이 한 번도 안 일어나면 이 검사가 헛돈다"


def test_cap_by_date_absence_is_the_normal_path() -> None:
    """mock 재고에는 ``cap_by_date``가 없다 — 회귀 픽스처 전량이 이 길로 간다."""
    received = purchase_port(_request("배추", SPREAD_WIDE))[0].payload
    notes = [
        note
        for scenario in received["scenarios"]
        for note in scenario["risks"]
        if "회 분할" in note
    ]
    assert notes, "분할 고지는 남는다"
    assert all("검사를 하지 않았다" in note for note in notes), "부재는 미검사로 고지된다"


@pytest.mark.parametrize(
    ("bad", "hint"),
    [
        ({"inbound_lead_days": 2.5}, "일 단위 정수"),
        ({"inbound_lead_days": True}, "정수여야"),
        ({"inbound_lead_days": -1}, "음수"),
        ({"cap_by_date": [1, 2]}, "매핑이어야"),
        ({"cap_by_date": {date(2026, 1, 2): 30}}, "ISO 날짜 문자열"),
        ({"cap_by_date": {"2026-01-02": float("nan")}}, "유한한 수"),
        ({"cap_by_date": {"2026-01-02": True}}, "수량이어야"),
    ],
)
def test_arrival_inputs_are_shape_checked(bad: dict, hint: str) -> None:
    """도착일 계산 입력의 **모양**이 어긋나면 어댑터에서 세운다.

    특히 ``2.5``: 도착일은 ``timedelta(days=2.5)``에서 2일로 잘리는데 ⑤의 소진 창은
    2.5를 그대로 쓴다. 두 계산이 다른 리드타임을 보고도 결과는 멀쩡해 보인다 —
    조용히 반올림하지 않고 여기서 막는다 (Codex 교차검증).
    """
    payload = _payload("배추", SPREAD_WIDE, inventory=bad)
    problems = [m for m in validate_payload(payload, SPREAD_WIDE) if "inventory" in m]
    assert any(hint in m for m in problems), problems


def test_arrival_inputs_absent_is_not_a_problem() -> None:
    """둘 다 선택 필드다 — 부재는 잡지 않는다 (mock 경로가 그 길이다)."""
    payload = _payload("배추", SPREAD_WIDE)
    assert not [
        m
        for m in validate_payload(payload, SPREAD_WIDE)
        if "inbound_lead_days" in m or "cap_by_date" in m
    ]


def test_rationale_does_not_claim_equal_split_after_reapportioning() -> None:
    """근거와 수량이 서로를 부정하면 안 된다 (Codex 교차검증).

    회차 **비율**은 균등이지만 **물량**은 창고 여유로 옮겨질 수 있다. 근거가
    "균등"이라고만 적으면 화면에서 [30, 70] 옆에 균등이라는 문장이 뜬다.
    """
    cap = {"2026-09-13": 1_000, "2026-09-19": 100_000}
    received = purchase_port(
        _request("배추", SPREAD_WIDE, inventory={"inbound_lead_days": 2, "cap_by_date": cap})
    )[0].payload
    for scenario in received["scenarios"]:
        quantities = [line["qty_kg"] for line in scenario["split_plan"]]
        trend = [r for r in scenario["rationale"] if "지속 상승 궤적" in r["claim"]]
        if not trend or len(set(quantities)) <= 1:
            continue
        detail = trend[0]["evidence_detail"]
        assert "회차 물량도 균등하다" not in detail, "옮겨졌는데 균등이라고 적는다"
        for line in scenario["split_plan"]:
            assert f"{line['qty_kg']:,}kg" in detail


def test_payment_date_overlap_raises_a_warning_not_a_cut() -> None:
    """지급일이 재무 집중일과 겹치면 **경고만** 남긴다.

    🔴 날짜별 잔액은 재계산하지 않는다 — 재무 ``SCENARIO_VALIDATION`` 소관이다.
    컷하면 우리가 판정한 것이 되고, 재무 판정과 갈렸을 때 정본이 불분명해진다.
    """
    plain = purchase_port(_request("배추", SPREAD_WIDE))[0].payload
    first_round = plain["scenarios"][0]["split_plan"][0]["date"]
    payment_date = (date.fromisoformat(first_round) + timedelta(days=7)).isoformat()

    hit = purchase_port(
        _request("배추", SPREAD_WIDE, finance={"critical_payment_dates": [payment_date]})
    )[0].payload
    warnings = [note for note in hit["scenarios"][0]["risks"] if "집중일" in note]
    assert warnings and payment_date in warnings[0]
    # 경고일 뿐 컷이 아니다 — 안 수가 그대로다
    assert len(hit["scenarios"]) == len(plain["scenarios"])


def test_payment_overlap_is_silent_without_n5() -> None:
    """N5가 없으면 지급일 자체를 계산하지 않는다 (규칙 3) — 집중일이 와도 경고가 없다."""
    hit = purchase_port(
        _request(
            "배추",
            SPREAD_WIDE,
            finance={"purchase_payment_days": None, "critical_payment_dates": ["2026-09-18"]},
        )
    )[0].payload
    assert not [note for note in hit["scenarios"][0]["risks"] if "집중일" in note]


# ── B6 — 재무 cap 이중 적용 해소 ──────────────────────────────────────────


def test_finance_cap_replaces_the_ratio_instead_of_stacking() -> None:
    """재무 cap을 받으면 ``max_purchase_ratio``를 **곱하지 않는다** (재무 회신 v2.2.1).

    같은 목적으로 두 번 조이면 상한이 두 겹이 되고 "왜 이만큼밖에 못 사나"의 근거가
    흐려진다. cap을 낮추면 총액이 따라 내려가는 것으로 실제 적용을 확인한다.
    """
    low = purchase_port(
        _request("배추", SPREAD_WIDE, finance={"finance_cap_amount_krw": 3_000_000})
    )[0].payload
    high = purchase_port(
        _request("배추", SPREAD_WIDE, finance={"finance_cap_amount_krw": 30_000_000})
    )[0].payload
    assert low["scenarios"][-1]["total_amount_krw"] < high["scenarios"][-1]["total_amount_krw"]
    for scenario in low["scenarios"]:
        assert scenario["total_amount_krw"] <= 3_000_000


def test_mock_path_keeps_the_ratio_fallback() -> None:
    """cap이 없는 경로는 **종전 그대로** — 949건이 매일 이 길을 밟는다."""
    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.draft_plan import purchase_budget_krw

    constraints = load_constraints()
    state = {"projected_cash_min": 10_000_000}
    assert purchase_budget_krw(state, constraints) == 10_000_000 * 0.60  # type: ignore[arg-type]
    with_cap = {**state, "finance_cap_amount_krw": 4_000_000}
    assert purchase_budget_krw(with_cap, constraints) == 4_000_000  # type: ignore[arg-type]


# ── reasoning ─────────────────────────────────────────────────────────────


def test_reasoning_passes_the_envelope_rule_on_every_anchor() -> None:
    """3자리 이상 연속 숫자 금지 · 문장 3개 이하 (``E-REASONING-*``).

    ⚠️ **규칙을 여기서 다시 쓰지 않는다.** 처음엔 ``chunk.isdigit()``으로 근사했는데
    그건 봉투의 ``\\d[\\d,]{2,}``보다 **약하다** — ``"1,250"``(쉼표)이나 ``"D+123"``처럼
    숫자가 다른 문자와 붙은 경우를 놓친다. 근사한 검사가 통과시킨 문자열을 봉투가
    거부하면 어긋난 곳은 코드가 아니라 **우리 테스트**가 된다.

    그래서 봉투의 ``check_reasoning``을 그대로 부른다.
    """
    for anchor in ANCHORS:
        reply = purchase_port(_request("배추", date.fromisoformat(anchor)))[0]
        probe = replace(reply, reasoning=build_reasoning(reply.payload))
        assert check_reasoning(probe) == []


def test_the_envelope_rule_is_stricter_than_a_naive_digit_check() -> None:
    """위 테스트가 봉투 함수를 쓰는 **이유**를 고정한다.

    ``"1,250"``은 ``isdigit()``으로는 숫자가 아니지만 봉투는 잡는다. 근사 검사로
    되돌리면 이 문장이 통과해 버린다.
    """
    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    leaked = replace(reply, reasoning="총액은 1,250만원이다.")
    assert [f.code for f in check_reasoning(leaked)] == ["E-REASONING-NUMERIC"]


def test_reasoning_reports_the_absence_of_scenarios() -> None:
    """안이 없는 날도 **사실만** 적는다 — E5 판정은 마스터 몫이다."""
    assert "제안을 내지 못했다" in build_reasoning({"scenarios": [], "situation": "uncertain"})


# ── STATUS_QUERY ──────────────────────────────────────────────────────────


def test_status_query_answers_without_building_scenarios() -> None:
    """살아 있는지와 무엇을 받을 수 있는지만 답한다."""
    request = _request("배추", SPREAD_WIDE, mode="STATUS_QUERY")
    reply, metadata = purchase_port(request)
    assert reply.runtime_status == "READY"
    assert "scenarios" not in reply.payload
    assert reply.payload["capabilities"]["supported_modes"] == [
        "GENERATE_SCENARIOS",
        "STATUS_QUERY",
    ]
    # **``used_tools``는 비어 있다.** 봉투가 STATUS_QUERY를 ``E-PLAN-EMPTY`` 예외로
    # 뺐으므로(``_PLAN_EXEMPT_MODES``) 가짜 Tool 이름을 넣을 이유가 없다 — 검사를
    # 피하려고 넣은 이름은 M-16이 읽는 실행 계획을 그대로 오염시킨다.
    assert metadata.used_tools == ()
    assert validate_reply(request, reply, metadata) == ()


def test_generate_scenarios_still_requires_used_tools() -> None:
    """면제는 ``STATUS_QUERY`` **하나뿐**이다 — 판단하는 mode는 재현할 대상이 있다."""
    from app.master.envelope import ExecutionMetadata, validate_reply

    request = _request("배추", SPREAD_WIDE)
    reply, metadata = purchase_port(request)
    assert metadata.used_tools, "정상 경로는 Tool을 담는다"

    stripped = ExecutionMetadata(
        run_id=metadata.run_id, request_id=metadata.request_id, agent="purchase"
    )
    codes = [f.code for f in validate_reply(request, reply, stripped)]
    assert "E-PLAN-EMPTY" in codes


def test_judgment_fields_are_declared_and_resolve_in_payload() -> None:
    """선언한 이름이 payload에 없으면 ``E-JUDGMENT-UNKNOWN``이다.

    오타를 조용히 넘기면 *"표기와 무관하게 근거를 요구하라"*는 그 검사가 통째로 빈다.
    ``allowed_axes``는 스키마에 없어 어댑터가 얹는 값이라 특히 깨지기 쉬운 자리다.
    """
    from app.purchase_agent.adapter import JUDGMENT_FIELDS

    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    assert reply.judgment_fields == JUDGMENT_FIELDS == ("situation", "allowed_axes")
    for name in reply.judgment_fields:
        assert name in reply.payload, f"{name}이 payload에 없으면 E-JUDGMENT-UNKNOWN"


def test_every_scenario_number_carries_a_path_evidence() -> None:
    """봉투 v0.4가 배열을 **한 겹 파고들어** 안 안쪽 숫자에도 근거를 요구한다.

    같은 이름의 필드가 안마다 2~3벌이라 위치가 필요하다 — 매입 요청으로 신설된 규칙이다
    (M-1 §7.1). 라벨은 면제이므로 ``label``·``strategy_type`` 근거는 만들지 않는다.
    """
    from app.master.envelope import required_claims

    reply = purchase_port(_request("배추", SPREAD_WIDE))[0]
    required = required_claims(reply.payload, reply.judgment_fields)
    claims = {e.claim for e in reply.evidences}
    assert required <= claims, f"근거 없는 경로: {sorted(required - claims)}"
    assert any(c.startswith("scenarios[0].") for c in claims)
    assert not any(c.endswith((".label", ".strategy_type")) for c in claims)


# ── #76 물류 lots 흡수 ────────────────────────────────────────────────────
#
# 이 절이 막으려는 사고: 전 스위트가 green 인데 실연동이 `KeyError: 'remaining_kg'` 로
# 죽었다 (2026-08-28 첫 통합 실행). mock 이 물류와 다른 이름을 쓰고 있었고, `lots` 가
# 선택 항목이라 수신 검증도 지나쳤다. 그래서 **mock 모양이 아니라 물류 모양**으로 검사한다.

#: 물류가 실제로 싣는 로트 (2026-08-28 실측). 값·타입을 그대로 옮겼다.
_LOGISTICS_LOT = {
    "lot_id": "LOT-KIMCHI-015-BAECHU",
    "item": "배추",
    "available_qty_kg": 286.92,
    "remaining_freshness_days": 10,
    "grade": None,
    "status": "ACTIVE",
}
_OTHER_ITEM_LOT = {**_LOGISTICS_LOT, "lot_id": "LOT-KIMCHI-015-MU", "item": "무"}


def test_lots_of_other_items_are_filtered_out() -> None:
    """물류는 4품목을 한 목록에 담아 보낸다 — 매입은 품목 하나씩 돈다.

    거르지 않고 ``lots[0]`` 을 집으면 **다른 품목의 로트를 근거로 삼는다.** 에러가 나지
    않아 아무도 모른다. mock 은 품목별로 나뉘어 있어 이 구멍이 보이지 않던 자리다.
    """
    absorbed = absorb_inventory({"lots": [_OTHER_ITEM_LOT, _LOGISTICS_LOT]}, "배추")
    assert [lot["lot_id"] for lot in absorbed["lots"]] == ["LOT-KIMCHI-015-BAECHU"]


def test_lots_without_an_item_key_are_kept_not_dropped() -> None:
    """품목 축을 **못 밝힌 것**과 **다른 품목**은 다르다 — 버리면 있는 재고가 없어진다."""
    unlabeled = {k: v for k, v in _LOGISTICS_LOT.items() if k != "item"}
    absorbed = absorb_inventory({"lots": [unlabeled]}, "배추")
    assert absorbed["lots"] == [unlabeled]


def test_absorb_does_not_invent_values() -> None:
    """없는 키를 기본값으로 채우지 않는다 (규칙 3). 옮기기만 한다."""
    absorbed = absorb_inventory({"warehouse_free_kg": 10, "lots": [_LOGISTICS_LOT]}, "배추")
    assert absorbed["lots"][0] == _LOGISTICS_LOT
    assert absorbed["warehouse_free_kg"] == 10


def test_absorb_passes_through_when_lots_are_absent() -> None:
    """``lots`` 는 선택 항목이다 — 없으면 그대로 둔다 (M-1 제출 §5)."""
    assert absorb_inventory({"warehouse_free_kg": 10}, "배추") == {"warehouse_free_kg": 10}


def test_old_shape_lots_are_caught_by_receive_validation() -> None:
    """**있는데 모양이 다른** 경우를 어댑터가 잡는다.

    옛 이름(``remaining_kg``)만 실린 로트가 오면, 노드 안에서 ``KeyError`` 로 죽는 대신
    ``missing_data`` 로 **무엇이 어긋났는지** 마스터에게 전달된다.
    """
    stale = {"lot_id": 12, "grade": "상", "remaining_kg": 3000, "shelf_life_days": 10}
    payload = _payload("배추", date(2026, 8, 21))
    payload["constraints"]["inventory"] = {**payload["constraints"]["inventory"], "lots": [stale]}
    missing = validate_payload(payload, date(2026, 8, 21))
    assert "constraints.inventory.lots[0].available_qty_kg" in missing


def test_absent_lots_are_not_reported_as_missing() -> None:
    """부재는 잡지 않는다 — 선택 항목이라 빠져도 단일 등급으로 돌아간다."""
    payload = _payload("배추", date(2026, 8, 21))
    inventory = {k: v for k, v in payload["constraints"]["inventory"].items() if k != "lots"}
    payload["constraints"]["inventory"] = inventory
    missing = validate_payload(payload, date(2026, 8, 21))
    assert not [name for name in missing if "lots" in name]


def test_real_logistics_lot_shape_runs_end_to_end() -> None:
    """물류 실물 모양으로 어댑터가 **끝까지 돈다.**

    이 테스트가 없어서 첫 통합 실행이 죽었다. mock 을 물류 모양에 맞췄으므로 이제
    같은 계약을 두 경로가 공유하지만, **실물 값을 직접 넣어** 한 번 더 못 박는다.
    """
    as_of = date(2026, 8, 21)
    payload = _payload("배추", as_of)
    payload["constraints"]["inventory"] = {
        **payload["constraints"]["inventory"],
        "lots": [_LOGISTICS_LOT, _OTHER_ITEM_LOT],
    }
    request = AgentRequest(
        context=ExecutionContext(f"REQ-{as_of}-배추", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=payload,
    )
    reply, _ = purchase_port(request)
    assert reply.runtime_status == "READY"
    assert reply.payload["scenarios"]
    # 근거는 **이 품목** 로트를 가리켜야 한다 — 무 로트를 집으면 여기서 걸린다
    inventory_refs = [
        item["ref_id"]
        for scenario in reply.payload["scenarios"]
        for item in scenario["rationale"]
        if item["source"] == "재고"
    ]
    assert inventory_refs and all(ref == "INV-LOT-KIMCHI-015-BAECHU" for ref in inventory_refs)


def test_fractional_warehouse_capacity_still_yields_integer_quantities() -> None:
    """물류가 보내는 창고 여유는 **소수**다 (실측 7,636.72kg).

    mock 이 우연히 정수라(12,000 + 3,600) 오래 드러나지 않았다. 소수가 그대로 수량 상한이
    되면 ``total_qty_kg`` 가 소수가 되고 출력 스키마(``int``)가 막는다 — 실연동이 거기서
    죽었다 (2026-08-28).

    **내림**인 것도 함께 못 박는다: 창고 수용량은 상한이라 올리면 못 넣는 양을 계획하게 된다.
    """
    as_of = date(2026, 8, 21)
    payload = _payload("배추", as_of)
    payload["constraints"]["inventory"] = {
        **payload["constraints"]["inventory"],
        "warehouse_free_kg": 7636.72,
        "rental_cap_kg": 0.0,
    }
    request = AgentRequest(
        context=ExecutionContext(f"REQ-{as_of}-배추", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=payload,
    )
    reply, _ = purchase_port(request)
    assert reply.runtime_status == "READY", reply.reasoning
    scenarios = reply.payload["scenarios"]
    assert scenarios
    for scenario in scenarios:
        assert isinstance(scenario["total_qty_kg"], int)
        assert all(isinstance(r["qty_kg"], int) for r in scenario["split_plan"])
        assert all(isinstance(r["qty_kg"], int) for r in scenario["sourcing_plan"])
        # 상한을 **넘지 않는다** — 내림이라 7,636 이 최대다
        # (이 픽스처는 현금이 먼저 묶어 창고 상한까지 안 간다. 내림 자체는
        #  test_draft_plan 의 warehouse_cap_kg 단위 검사가 잠근다.)
        assert scenario["total_qty_kg"] <= 7636


# ── llm_status — DISABLED 와 SKIPPED_TEMPLATE 를 가른다 (2026-08-31) ────────


def test_llm_status_separates_switched_off_from_not_called_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 **"안 켰다"와 "켰는데 이번엔 안 썼다"는 다른 사실이다.**

    전에는 판단자를 안 부른 실행이 무조건 ``DISABLED`` 였다. 2025-12-31 실행이 그랬는데
    설정은 켜져 있었다 — 등급이 미상이라 ⑤가 후보를 만들기 전에 막힌 것이다.
    **앞은 설정 문제라 고칠 수 있고 뒤는 그날의 사실이라 고칠 것이 없다.** 한 값으로
    내면 사람이 없는 문제를 찾는다.

    봉투가 뜻을 규정한다 (``master/envelope.py`` ``LLMStatus``) — 새로 정한 규칙이 아니라
    마스터 ``IntentService``·Critic ``JudgeService`` 가 이미 쓰는 서열이다.
    """
    from app.purchase_agent.adapter import _uncalled_status

    monkeypatch.setenv("PURCHASE_LLM_ENABLED", "false")
    assert _uncalled_status() == "DISABLED"

    monkeypatch.setenv("PURCHASE_LLM_ENABLED", "true")
    assert _uncalled_status() == "SKIPPED_TEMPLATE"


def test_a_status_query_says_skipped_not_disabled_when_the_llm_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """판단 단계가 **애초에 없는** 실행도 ``SKIPPED_TEMPLATE`` 이다.

    Critic 이 *"이 Flow 에는 그 문장을 쓰는 단계가 없다"* 를 같은 값으로 적는 것과 같다
    (``critic/critic_v0_4.py``).
    """
    monkeypatch.setenv("PURCHASE_LLM_ENABLED", "true")
    _, metadata = purchase_port(_request("배추", SPREAD_WIDE, mode="STATUS_QUERY"))

    assert metadata.llm_status == "SKIPPED_TEMPLATE"
    assert metadata.llm_fallback_used is False


def test_our_vocabulary_is_the_envelope_vocabulary() -> None:
    """어휘를 우리가 새로 만들지 않는다 — 봉투 계약의 네 값을 그대로 쓴다."""
    from typing import get_args

    from app.master.envelope import LLMStatus as EnvelopeStatus
    from app.purchase_agent.llm.schemas import LLMStatus as OurStatus

    assert set(get_args(OurStatus)) == set(get_args(EnvelopeStatus))
