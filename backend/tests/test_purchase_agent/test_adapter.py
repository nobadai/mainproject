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
    build_reasoning,
    purchase_port,
    validate_payload,
)
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import run_purchase_agent

ANCHORS = ["2026-08-21", "2026-08-28", "2026-09-04", "2026-09-11"]
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


def _payload(item: str, as_of: date, **over) -> dict:
    extras = ports.get_snapshot_extras(item, as_of)
    payload = {
        "item": item,
        "constraints": {
            "finance": _finance(as_of, **over.pop("finance", {})),
            "inventory": ports.get_inventory(item, as_of),
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
    assert reply.payload["scenarios"] == []


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
    # E-PLAN-EMPTY를 피하려면 READY 회신에 Tool이 하나는 있어야 한다 (임시값)
    assert metadata.used_tools == ("status_query",)
    assert validate_reply(request, reply, metadata) == ()
