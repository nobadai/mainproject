"""물류 행동 계약 테스트 — 감사 P0 2건 + Adapter↔Service parity (#121).

1단계에서 결함의 현재 동작을 PIN 으로 고정했고, 2·3단계 수정이 반영되면서 전부
**계약 테스트**로 전환됐다 (PIN 잔존 없음).

★ (a) P0-1 — business_status 는 시나리오 집계 ⊕ 하드 제약의 최악값 결합이다
  (2026-09-01 마스터 확정: any reject → reject · 조정안은 판정을 무르지 않음 ·
  하드 제약은 낮출 수만 있음). 독립 API 최상위 verdict 도 같은 규칙이다.

★ (b) P0-2 — reject 안의 adjustment 는 scenario_results 에 진단으로만 남고,
  preferred·suggested·needs_followup 으로 승격되지 않는다.

★ (c) parity 는 재현이 아니라 상시 안전망이다 — Core 에 값이 추가되고 한쪽 조립에만
  반영되는 드리프트(PR #116 에서 실제로 일어난 일)를 자동으로 잡는다.
  **응답 전체를 비교하지 않는다.** 두 경로는 계약이 다르다(Evidence · missing_data
  어휘 · LLM 필드) — 공유돼야 하는 결정론 값만 대조한다.

★ (d) P1-1 — 스냅샷 로드 실패의 분류: 데이터 부재(LookupError)는 RUNTIME_NOT_READY,
  실행 오류(무결성 위반·DB 장애 등)는 ERROR. 예외 원문은 reasoning 으로 새지 않는다.

★ DB 를 타지 않는다. (a)~(c)의 스냅샷은 양쪽에 동일 객체를 직접 주입하고,
  (d)만 로드 실패 자체를 대상으로 repository 함수를 갈아 끼운다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, get_args

import pytest

from app.logistics import adapter
from app.logistics.interpretation import _MISSING_DATA_NAMES
from app.logistics.llm.runtime import InterpretationService, LLMSettings, UnavailableProvider
from app.logistics.repository import LogisticsRead
from app.logistics.rules import (
    BUSINESS_SIGNALS,
    UNRESOLVED_WARNING_CODES,
    derive_procurement_verdict,
)
from app.logistics.scenario_engine import (
    derive_preferred_adjustment,
    validate_purchase_scenarios,
)
from app.logistics.schemas import (
    POLICY_VERSION,
    ConstraintCode,
    ConstraintResult,
    InventoryLogisticsSnapshot,
    LogisticsPolicy,
    PurchaseAgentOutput,
    ScenarioAdjustment,
    ScenarioValidationResult,
)
from app.logistics.service import run_logistics_procurement_with_snapshot
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply

AS_OF = date(2026, 8, 21)


#: 테스트 전용 실행 축 (#345) — 운영값(`BURN_IN_SIM_RUN_ID`)을 쓰지 않는다.
SIM_RUN_ID = "SIM-T-PIN-0001"


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-PIN-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
        sim_run_id=SIM_RUN_ID,
    )


def _request(payload: dict[str, Any], mode: str = "SCENARIO_VALIDATION") -> AgentRequest:
    return AgentRequest(context=_ctx(), agent="inventory", mode=mode, payload=payload)


def _snapshot(**overrides: Any) -> InventoryLogisticsSnapshot:
    base: dict[str, Any] = {
        "snapshot_id": "PIN-SNAP-1",
        "as_of": AS_OF,
        "on_hand_by_lot": [],
        "in_transit": [],
        "confirmed_inbound_schedule": [],
        "confirmed_outbound_schedule": [],
        "outbound_commitments": [],
        "used_capacity_kg": Decimal(1000),
        "guaranteed_capacity_kg": Decimal(8000),
        "burst_capacity_kg": Decimal(9600),
        "guaranteed_capacity_by_zone_kg": None,
        "inbound_lead_days": 2,
        "daily_inbound_capacity_kg": Decimal(5000),
        "inbound_transport_capacity_kg": Decimal(5000),
        "shared_daily_outbound_capacity_kg": Decimal(5000),
        "evidence_refs": ["FIXTURE:PIN-SNAP-1"],
    }
    return InventoryLogisticsSnapshot(**{**base, **overrides})


_UNIT_PRICE = 1650


def _scenario_block(
    label: str,
    split_plan: list[dict[str, Any]],
    total_qty: int,
) -> dict[str, Any]:
    """사중 일치(수량 3축 + 금액)와 seq·날짜 규칙을 지키는 시나리오 블록."""
    return {
        "label": label,
        "strategy_type": "quantity",
        "coverage_days": 5,
        "total_qty_kg": total_qty,
        "total_amount_krw": total_qty * _UNIT_PRICE,
        "max_price": 1750,
        "margin_warning": False,
        "split_plan": split_plan,
        "sourcing_plan": [
            {
                "market": "가락",
                "grade": "상",
                "qty_kg": total_qty,
                "grade_unit_price": _UNIT_PRICE,
            }
        ],
        "expected_margin_rate": 0.3,
        "rationale": [
            {
                "source": "예측",
                "claim": "행동 고정 테스트 근거",
                "ref_id": "TEST-PIN-001",
                "evidence_grade": "OFFICIAL",
                "evidence_detail": "테스트 fixture",
            }
        ],
        "risks": [],
    }


def _payload_of(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "meta": {
            "as_of": AS_OF.isoformat(),
            "item": "배추",
            "agent_version": "v1.1",
            "is_refeed": False,
            "feedback_attempt": 0,
        },
        "scenarios": scenarios,
        "confidence": "high",
        "situation": "stable",
        "context_docs_used": [],
        "rejected_reasons": [],
    }


def _proposal_payload(
    split_plan: list[dict[str, Any]],
    sourcing_qty: int,
) -> dict[str, Any]:
    return _payload_of([_scenario_block("기본", split_plan, sourcing_qty)])


def _policy() -> LogisticsPolicy:
    """실물 모델로 만든다 — 가짜를 쓰면 물류가 계약을 넓힐 때 이 테스트가 안 깨진다."""
    return LogisticsPolicy(
        guaranteed_capacity_kg=Decimal(8000),
        burst_capacity_kg=Decimal(9600),
        inbound_lead_days=2,
        daily_inbound_capacity_kg=Decimal(5000),
        inbound_transport_capacity_kg=Decimal(5000),
        shared_daily_outbound_capacity_kg=Decimal(5000),
        cap_by_date_policy="CONFIRMED_ONLY",
        policy_version=POLICY_VERSION,
        usage_scope="AGENT_MVP_DEMO",
        source_refs={},
    )


def _wire(monkeypatch: pytest.MonkeyPatch, snapshot: InventoryLogisticsSnapshot) -> None:
    monkeypatch.setattr(
        adapter,
        "_load_read",
        lambda *, as_of, sim_run_id: LogisticsRead(snapshot=snapshot, policy=_policy()),
    )


def _disabled_llm() -> InterpretationService:
    settings = LLMSettings(
        enabled=False,
        provider="ollama",
        model="test",
        base_url="http://127.0.0.1:9",
        timeout_seconds=0.1,
        max_retries=0,
    )
    return InterpretationService(settings, UnavailableProvider())


# ---------------------------------------------------------------------------
# (a) P0-1 — business_status 는 시나리오 집계 ⊕ 하드 제약 결합이다 (3단계 수정 반영)
# ---------------------------------------------------------------------------


def test_전_시나리오_reject_면_business_status_도_reject_다(monkeypatch):
    """✅ 계약 (#121 3단계 수정 반영 · 2026-09-01 마스터 확정) — 1단계 PIN 의 반전.

    하드 제약이 전부 PASS 여도(창고 정책값 전부 존재 + zone 존재) 시나리오가 전부
    reject 면 business_status 는 reject 다 — 하드 제약은 판정을 낮출 수만 있고
    올릴 수 없다. 종전에는 하드만 집계해 `ok` 가 나갔고, 마스터 `_acceptable` 이
    이 값만 보므로 "물류상 실행 불가능한 안이 통과"였다 (P0-1).

    전부-PASS 스냅샷을 쓰는 이유는 1단계와 같다 — 실환경의 zone=None(LOG-H02
    UNRESOLVED) 노이즈를 걷어내고 시나리오 축이 판정을 끌어내리는 것만 분리해 본다.
    """
    snapshot = _snapshot(
        used_capacity_kg=Decimal(8000),  # 보장치와 같음 — 여유 0, 출고 없음 → 창 전체 0
        guaranteed_capacity_by_zone_kg={"MAIN": Decimal(8000)},  # LOG-H02 도 PASS
    )
    _wire(monkeypatch, snapshot)
    payload = _proposal_payload(
        split_plan=[{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 1000}],
        sourcing_qty=1000,
    )

    reply, _meta = adapter.logistics_port(_request(payload))

    assert reply.runtime_status == "READY"
    # 원인을 결과와 함께 고정한다 — "ok" 가 집계 단절에서 왔음을 증명하려면
    # 하드 제약 5종이 실제로 전부 PASS 였다는 사실이 같이 박혀 있어야 한다.
    # (하드 제약이 실수로 사라져 빈 집합이 PASS 가 되는 회귀도 여기서 걸린다)
    constraints = {row["code"]: row["status"] for row in reply.payload["hard_constraints"]}
    assert constraints == {
        "LOG-H01": "PASS",
        "LOG-H02": "PASS",
        "LOG-H03": "PASS",
        "LOG-H04": "PASS",
        "LOG-H05": "PASS",
    }
    # 도착일의 수용량이 실제로 0 — reject 가 다른 경로가 아니라 만석에서 왔다.
    assert reply.payload["cap_by_date"] == {"2026-08-23": 0.0}
    results = reply.payload["scenario_results"]
    assert [row["verdict"] for row in results] == ["reject"]
    assert "NO_FEASIBLE_ARRIVAL_DATE" in results[0]["reason_codes"]

    # ✅ 반전 완료: 시나리오 전부 reject → 최상위도 reject. 마스터가 재호출한다.
    assert reply.payload["verdict"] == "reject"
    assert reply.business_status == "reject"
    # 행동 제안 채널은 침묵 — reject 안에는 승격할 조정이 없다 (2단계 계약과 정합).
    assert reply.suggested_adjustments == ()
    assert reply.needs_followup is False

    # verdict Evidence 도 결합 근거를 싣는다 — 되돌리면 unit/value/문구가 갈린다
    # (종전: failed_check_count=0). 3자 검증 지적 반영.
    evidence = next(e for e in reply.evidences if e.claim == "verdict")
    assert evidence.unit == "non_ok_input_count"
    assert evidence.value == 1.0  # 비통과 하드 0건 + reject 1안 + conditional 0안
    assert "reject 1안" in evidence.evidence_detail

    # 독립 API 도 같은 결합 규칙이다 (#121 3단계의 나머지 절반) — 이 픽스처에서
    # 하드만 보면 PASS, 결합이면 FAIL 이라 service 쪽 되돌리기가 여기서 잡힌다.
    service_response = run_logistics_procurement_with_snapshot(
        PurchaseAgentOutput.model_validate(payload),
        snapshot,
        interpretation_service=_disabled_llm(),
    )
    assert service_response.verdict == "FAIL"


def _rule_result(*, runtime: str = "READY", statuses: tuple[str, ...] = ("PASS",)) -> dict:
    """결합 함수 단위 테스트용 최소 LogisticsRuleResult."""
    return {
        "runtime_status": runtime,
        "hard_constraints": [
            ConstraintResult(code="LOG-H01", status=status, skip_reason=None) for status in statuses
        ],
        "soft_warnings": [],
        "calculation_ready": runtime == "READY",
    }


def _scenario_verdicts(*verdicts: str) -> list[ScenarioValidationResult]:
    return [
        ScenarioValidationResult(label=f"안{i}", verdict=v, reason_codes=[], adjustments=[])
        for i, v in enumerate(verdicts, start=1)
    ]


@pytest.mark.parametrize(
    ("runtime", "statuses", "verdicts", "expected"),
    [
        # 시나리오 집계 — any reject > any conditional > 전부 ok
        ("READY", ("PASS",), ("ok", "ok"), "PASS"),
        ("READY", ("PASS",), ("ok", "conditional"), "REVIEW_REQUIRED"),
        ("READY", ("PASS",), ("ok", "conditional", "reject"), "FAIL"),
        # 하드는 낮출 수만 있다 — UNRESOLVED 가 전-ok 를 끌어내리고,
        # 전부 PASS 가 reject 를 되살리지 못한다
        ("READY", ("UNRESOLVED",), ("ok",), "REVIEW_REQUIRED"),
        ("READY", ("PASS",), ("reject",), "FAIL"),
        ("READY", ("FAIL",), ("ok",), "FAIL"),
        # skipped 는 집계 불참 — 올리지도 낮추지도 않는다
        ("READY", ("PASS",), ("skipped",), "PASS"),
        ("READY", ("UNRESOLVED",), ("skipped", "ok"), "REVIEW_REQUIRED"),
        # 시나리오 0개 — 하드 판정만으로 떨어진다 (집계항 부재)
        ("READY", ("PASS",), (), "PASS"),
        # runtime 비-READY — 시나리오와 무관하게 판정 없음
        ("RUNTIME_NOT_READY", ("PASS",), ("reject",), None),
    ],
)
def test_결합_판정_truth_table(runtime, statuses, verdicts, expected):
    """`derive_procurement_verdict` 단위 고정 — 확정 문구의 성질 전부 (검증 지적 반영).

    `skipped` 불참과 시나리오 0개(하드만)는 통합 경로로는 닿기 어려운 경계라
    함수 단위로 박는다. 형제 `derive_logistics_verdict` 의 단위 테스트와 대칭.
    """
    result = derive_procurement_verdict(
        _rule_result(runtime=runtime, statuses=statuses),
        _scenario_verdicts(*verdicts),
    )
    assert result == expected


# ---------------------------------------------------------------------------
# (b) P0-2 — reject 의 adjustment 는 진단으로만 남는다 (2단계 수정 반영)
# ---------------------------------------------------------------------------


def _multi_split_case() -> tuple[InventoryLogisticsSnapshot, dict[str, Any]]:
    """1회차는 수량 조정으로 살 수 있고(가용 1,000), 2회차는 창 전체에서 불가.

    1회차 제안 원안(3,000kg)이 도착일부터 창을 점유하므로 2회차 시점의 가용은
    max(0, 1000 − 3000) = 0 이고, 창(window) 안의 모든 날이 같은 상태다.
    """
    snapshot = _snapshot(used_capacity_kg=Decimal(7000))  # 여유 1,000
    payload = _proposal_payload(
        split_plan=[
            {"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 3000},
            {"seq": 2, "date": date(2026, 8, 24).isoformat(), "qty_kg": 2000},
        ],
        sourcing_qty=5000,
    )
    return snapshot, payload


def test_reject_의_adjustment_는_진단으로_남고_preferred_에서는_빠진다():
    """✅ 계약 (#121 2단계 수정 반영) — 1단계 PIN 의 기대값 반전.

    multi-split 에서 앞 회차의 adjustment 가 쌓인 채 뒤 회차 불가로 reject 가 될
    수 있다. 그 adjustment 는 "어디까지는 됐는지"의 **진단 기록으로 유지**되지만,
    구제 불가 판정한 안이 우선 조정 축을 정하면 안 되므로 preferred 집계에서는
    제외된다.
    """
    snapshot, payload = _multi_split_case()
    proposal = PurchaseAgentOutput.model_validate(payload)

    results = validate_purchase_scenarios(proposal, snapshot)

    assert len(results) == 1
    result = results[0]
    assert result.verdict == "reject"
    assert "NO_FEASIBLE_ARRIVAL_DATE" in result.reason_codes

    # 진단 기록은 유지된다 — 사실을 지우는 수정이 아니다.
    assert len(result.adjustments) == 1
    assert result.adjustments[0].axis == "quantity"
    assert result.adjustments[0].suggested_qty_kg == Decimal(1000)
    # ✅ 반전 완료: reject 만 있는 집계에서 preferred 는 없다.
    assert derive_preferred_adjustment(results) is None


def test_reject_시나리오의_조정은_M1_행동_제안_채널로_나가지_않는다(monkeypatch):
    """✅ 계약 (#121 2단계 수정 반영) — 1단계 PIN 의 기대값 반전.

    reject 안의 조정은 payload.scenario_results(진단)에는 남지만, M-1 행동 제안
    채널(`suggested_adjustments`)·`preferred_adjustment`·`needs_followup` 으로는
    승격되지 않는다 — "reject 는 조정으로 구제 불가"와 채널 의미를 일치시킨다.
    """
    snapshot, payload = _multi_split_case()
    _wire(monkeypatch, snapshot)

    reply, _meta = adapter.logistics_port(_request(payload))

    results = reply.payload["scenario_results"]
    assert [row["verdict"] for row in results] == ["reject"]
    # 진단 정보는 유지된다 — 사실이 사라지는 것이 아니라 제안으로 격상되지 않을 뿐.
    assert results[0]["adjustments"][0]["suggested_qty_kg"] == 1000.0

    # ✅ 반전 완료: 행동 제안 채널 전부 침묵. preferred 는 None 이므로 키 자체가 없다.
    assert "preferred_adjustment" not in reply.payload
    assert reply.suggested_adjustments == ()
    assert reply.needs_followup is False


def test_혼합_집계에서_preferred_는_비reject_축을_따른다():
    """✅ 계약 — 2단계 수정의 의미 변화를 고정한다 (검증 지적 반영).

    reject(quantity 잔존) + conditional(timing) 혼합에서 수정 전에는 축 혼재로
    None 이었지만, reject 의 조정은 행동 제안의 모집단이 아니므로 **행동 가능한
    안의 고유 축인 timing 이 우선 조정**이 맞다. 구제 불가 판정의 진단 잔재가
    유효한 추천을 막던 것이 종전의 결함이다.
    """
    results = [
        ScenarioValidationResult(
            label="보수",
            verdict="reject",
            reason_codes=["CAPACITY_EXCEEDED", "NO_FEASIBLE_ARRIVAL_DATE"],
            adjustments=[
                ScenarioAdjustment(
                    axis="quantity", split_date=AS_OF, suggested_qty_kg=Decimal(1000)
                )
            ],
        ),
        ScenarioValidationResult(
            label="기본",
            verdict="conditional",
            reason_codes=["CAPACITY_EXCEEDED"],
            adjustments=[
                ScenarioAdjustment(
                    axis="timing",
                    split_date=AS_OF,
                    suggested_arrival_date=date(2026, 8, 26),
                )
            ],
        ),
    ]

    assert derive_preferred_adjustment(results) == "timing"


def _mixed_verdict_case() -> tuple[InventoryLogisticsSnapshot, dict[str, Any]]:
    """reject(보수)와 conditional(기본)이 한 제안에 공존하는 실계산 케이스.

    창고 만석(8,000/8,000)에 확정 출고가 1,000kg(8/25)·1,000kg(8/27) 있어
    8/26부터 1,000 · 8/28부터 2,000 이 열린다. lead=2. (매입 계약상 각 시나리오
    seq 1 의 날짜는 `meta.as_of` 여야 하므로 둘 다 8/21 시작이다.)

    보수: split1 2,000@8/21 → 도착 8/23 cap 0 → timing 조정(8/28, 처음으로 2,000
          이 들어가는 날). 원안 2,000 이 8/23부터 점유 → split2 3,000@8/23 은
          8/28에도 2000−2000=0 이라 창 전체 불가 → **reject + timing 조정 잔존**.
    기본: split 1,000@8/21 → 도착 8/23 cap 0 → 8/26 가능 → **conditional + timing**.

    두 조정의 dedup 키가 다르다(target 8/28→7.0 vs 8/26→5.0) — 필터가 없으면
    suggested 가 2건이 되므로 필터 제거 뮤턴트가 확실히 잡힌다.
    """
    snapshot = _snapshot(
        used_capacity_kg=Decimal(8000),
        on_hand_by_lot=[
            {
                "lot_id": "LOT-MIX-1",
                "item": "배추",
                "available_qty_kg": Decimal(2000),
                "remaining_freshness_days": 5,
                "effective_freshness_limit_days": 10,
                "status": "ACTIVE",
            }
        ],
        confirmed_outbound_schedule=[
            {"date": date(2026, 8, 25).isoformat(), "quantity_kg": 1000, "item": "배추"},
            {"date": date(2026, 8, 27).isoformat(), "quantity_kg": 1000, "item": "배추"},
        ],
        outbound_commitments=[],
    )
    payload = _payload_of(
        [
            _scenario_block(
                "보수",
                [
                    {"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 2000},
                    {"seq": 2, "date": date(2026, 8, 23).isoformat(), "qty_kg": 3000},
                ],
                5000,
            ),
            _scenario_block(
                "기본",
                [{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 1000}],
                1000,
            ),
        ]
    )
    return snapshot, payload


def test_혼합_케이스에서_어댑터는_비reject_조정만_승격한다(monkeypatch):
    """✅ 계약 — suggested 선별 승격과 preferred Evidence 건수 모집단을 고정한다.

    reject(보수)의 timing 조정은 진단으로만 남고, conditional(기본)의 timing 조정만
    행동 제안 채널로 나간다. preferred Evidence 의 건수도 비-reject 모집단만 센다 —
    이 필터는 혼합 케이스에서만 발화하므로 여기서만 잡힌다 (검증 지적 반영).
    """
    snapshot, payload = _mixed_verdict_case()
    _wire(monkeypatch, snapshot)

    reply, _meta = adapter.logistics_port(_request(payload))

    results = reply.payload["scenario_results"]
    assert [(row["label"], row["verdict"]) for row in results] == [
        ("보수", "reject"),
        ("기본", "conditional"),
    ]
    # reject 의 timing 조정은 진단으로 유지된다.
    assert results[0]["adjustments"][0]["axis"] == "timing"
    assert results[0]["adjustments"][0]["suggested_arrival_date"] == "2026-08-28"

    # preferred 는 비-reject 축(timing)을 따른다.
    assert reply.payload["preferred_adjustment"] == "timing"
    assert "preferred_adjustment" in reply.judgment_fields

    # 3단계 확정 별표 ①: 조정안이 있어도 판정을 무르지 않는다 — any reject → reject.
    assert reply.business_status == "reject"

    # 승격은 conditional(기본)의 조정 1건뿐 — 필터가 없으면 보수 것까지 2건이 된다.
    assert len(reply.suggested_adjustments) == 1
    suggested = reply.suggested_adjustments[0]
    assert suggested.axis == "timing"
    assert suggested.target_value == 5.0  # 8/26 − as_of(8/21)
    # 🔴 라벨은 **칸**으로 간다 (#209 · 미결 §0-6 갈래 ㄱ). 전에는 reason 문자열에만
    #   있어 받는 쪽이 부서 문장을 파싱해야 했다.
    assert suggested.scenario_labels == ("기본",)
    assert suggested.split_date == date(2026, 8, 21)
    # 문장에서는 라벨·회차 앞머리가 빠졌다. 목표 도착일은 남는다 — 절대 날짜 칸이
    # 아직 없어 지금 빼면 그 값이 어디에도 없다.
    assert suggested.reason == "도착일을 2026-08-26 로 조정 제안"
    assert "기본" not in suggested.reason
    assert reply.needs_followup is True

    # preferred Evidence 건수 = 비-reject 모집단의 timing 조정 수 = 1 (전체로 세면 2).
    evidence = next(e for e in reply.evidences if e.claim == "preferred_adjustment")
    assert evidence.value == 1.0

    # 독립 API 정렬의 두 번째 대조점 — 이 픽스처는 하드만 보면 REVIEW_REQUIRED,
    # 결합이면 FAIL 이다 ((a)의 PASS→FAIL 과 다른 갈림이라 함께 고정한다).
    service_response = run_logistics_procurement_with_snapshot(
        PurchaseAgentOutput.model_validate(payload),
        snapshot,
        interpretation_service=_disabled_llm(),
    )
    assert service_response.verdict == "FAIL"


# ---------------------------------------------------------------------------
# (c) Adapter↔Service parity — 공유 결정론 값만 대조하는 상시 안전망
# ---------------------------------------------------------------------------


def _parity_case() -> tuple[InventoryLogisticsSnapshot, dict[str, Any]]:
    """세 signal(CAPACITY_TIGHT·신선도 압박·조정 필요)이 전부 서는 시나리오.

    여유 1,000 에 3,000 제안 → conditional(수량 조정) → SCENARIO_ADJUSTMENT_REQUIRED.
    창 사용률 1 − 1000/8000 = 0.875 ≥ 0.8 → CAPACITY_TIGHT.
    Lot 잔여 비율 2/10 = 0.2 ≤ 0.3 → INVENTORY_FRESHNESS_PRESSURE.
    """
    snapshot = _snapshot(
        used_capacity_kg=Decimal(7000),
        on_hand_by_lot=[
            {
                "lot_id": "LOT-PIN-1",
                "item": "배추",
                "available_qty_kg": Decimal(600),
                "remaining_freshness_days": 2,
                "effective_freshness_limit_days": 10,
                "status": "ACTIVE",
            }
        ],
        capacity_tight_ratio=Decimal("0.8"),
        freshness_pressure_ratio=Decimal("0.3"),
    )
    payload = _proposal_payload(
        split_plan=[{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 3000}],
        sourcing_qty=3000,
    )
    return snapshot, payload


def _norm_scenario_results_from_models(results: Any) -> list[tuple]:
    return [
        (
            r.label,
            r.verdict,
            tuple(r.reason_codes),
            tuple(
                (
                    a.axis,
                    a.split_date.isoformat(),
                    None if a.suggested_qty_kg is None else float(a.suggested_qty_kg),
                    None
                    if a.suggested_arrival_date is None
                    else a.suggested_arrival_date.isoformat(),
                )
                for a in r.adjustments
            ),
        )
        for r in results
    ]


def _norm_scenario_results_from_payload(rows: list[dict[str, Any]]) -> list[tuple]:
    return [
        (
            row["label"],
            row["verdict"],
            tuple(row["reason_codes"]),
            tuple(
                (
                    a["axis"],
                    a["split_date"],
                    a.get("suggested_qty_kg"),
                    a.get("suggested_arrival_date"),
                )
                for a in row["adjustments"]
            ),
        )
        for row in rows
    ]


def _business_signals(warnings: list[str]) -> set[str]:
    return {w for w in warnings if w in BUSINESS_SIGNALS}


def _norm_constraints_from_models(constraints: Any) -> list[tuple]:
    return [(c.code, c.status, c.skip_reason) for c in constraints]


def _norm_constraints_from_payload(rows: list[dict[str, Any]]) -> list[tuple]:
    return [(row["code"], row["status"], row["skip_reason"]) for row in rows]


def test_어댑터와_독립_경로는_공유_결정론_값이_같다(monkeypatch):
    """같은 스냅샷 + 같은 제안 → 두 조립의 공유값이 일치해야 한다.

    PR #116 이 수동으로 맞춘 정합을 자동 감시로 바꾼다. 감시 범위는 **아래에
    열거된 공유값**이다 — 여기 없는 새 공유값이 Core 에 생기면 이 목록에도
    추가해야 감시가 미친다(자동 확장이 아니다). 대조 대상: runtime_status ·
    inventory_by_item · scenario_results · cap_by_date · preferred_adjustment ·
    soft_warnings(같은 merge_business_warnings 출력이라 전체 대조) ·
    hard_constraints(같은 rules 출력). 계약이 다른 필드(Evidence · missing_data
    어휘 · LLM)는 비교하지 않는다.
    """
    snapshot, payload = _parity_case()
    proposal = PurchaseAgentOutput.model_validate(payload)

    service_response = run_logistics_procurement_with_snapshot(
        proposal, snapshot, interpretation_service=_disabled_llm()
    )

    _wire(monkeypatch, snapshot)
    reply, _meta = adapter.logistics_port(_request(payload))

    # 대조가 무의미하지 않은지 먼저 — 세 signal 이 실제로 섰고 판정은 conditional 이다.
    assert service_response.runtime_status == "READY"
    assert _business_signals(service_response.soft_warnings) == {
        "CAPACITY_TIGHT",
        "INVENTORY_FRESHNESS_PRESSURE",
        "SCENARIO_ADJUSTMENT_REQUIRED",
    }

    # ① runtime·최상위 판정 — 같은 rules 결과·같은 결합 규칙을 쓴다 (#121 3단계:
    #    독립 API verdict 와 M-1 business_status 는 같은 집계의 두 표기다)
    assert reply.runtime_status == service_response.runtime_status
    assert service_response.verdict is not None
    assert reply.business_status == adapter._VERDICT_MAP[service_response.verdict]

    # ② inventory_by_item
    assert service_response.inventory_by_item is not None
    service_inventory = [
        (entry.item, float(entry.available_qty_kg)) for entry in service_response.inventory_by_item
    ]
    adapter_inventory = [
        (row["item"], row["available_qty_kg"]) for row in reply.payload["inventory_by_item"]
    ]
    assert adapter_inventory == service_inventory

    # ③ scenario_results (판정·사유·조정 전부)
    assert service_response.scenario_results is not None
    assert _norm_scenario_results_from_payload(
        reply.payload["scenario_results"]
    ) == _norm_scenario_results_from_models(service_response.scenario_results)

    # ④ cap_by_date
    service_cap = {
        day.isoformat(): float(value) for day, value in service_response.band.cap_by_date.items()
    }
    assert reply.payload["cap_by_date"] == service_cap

    # ⑤ preferred_adjustment — 어댑터는 None 이면 키를 뺀다
    assert reply.payload.get("preferred_adjustment") == service_response.preferred_adjustment

    # ⑥ 경고 채널 — 두 경로가 같은 merge_business_warnings 를 쓰므로 전체가 같아야
    #    한다 (signal 부분집합만 보면 POLICY_UNRESOLVED 계열의 한쪽 누락을 놓친다)
    assert reply.payload["soft_warnings"] == service_response.soft_warnings
    assert _business_signals(reply.payload["soft_warnings"]) == {
        "CAPACITY_TIGHT",
        "INVENTORY_FRESHNESS_PRESSURE",
        "SCENARIO_ADJUSTMENT_REQUIRED",
    }

    # ⑦ hard_constraints — 같은 evaluate_procurement_rules 출력을 양쪽이 싣는다
    assert _norm_constraints_from_payload(
        reply.payload["hard_constraints"]
    ) == _norm_constraints_from_models(service_response.hard_constraints)


def test_parity_는_시나리오가_전부_통과인_날도_성립한다(monkeypatch):
    """조정·signal 이 없는 조용한 날에도 두 조립이 같은 것을 실어야 한다.

    (풍부한 케이스만 대조하면 "없음"을 한쪽만 싣는 드리프트를 놓친다 —
    preferred 키 생략 규칙이 정확히 그런 자리다.)
    """
    snapshot = _snapshot()  # 여유 7,000 — 1,000kg 제안은 그대로 통과
    payload = _proposal_payload(
        split_plan=[{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 1000}],
        sourcing_qty=1000,
    )
    proposal = PurchaseAgentOutput.model_validate(payload)

    service_response = run_logistics_procurement_with_snapshot(
        proposal, snapshot, interpretation_service=_disabled_llm()
    )
    _wire(monkeypatch, snapshot)
    reply, _meta = adapter.logistics_port(_request(payload))

    assert service_response.preferred_adjustment is None
    # 조용한 날도 최상위 판정 결합 규칙은 동일하다.
    assert service_response.verdict is not None
    assert reply.business_status == adapter._VERDICT_MAP[service_response.verdict]
    # 키 생략까지 고정한다 — `.get() is None` 은 "키 없음"과 "명시적 null 탑재"를
    # 구분하지 못한다 (§1.2-10). 어댑터는 preferred 가 없으면 키 자체를 빼야 한다.
    assert "preferred_adjustment" not in reply.payload
    # 빈 집계도 양쪽이 같은 모양이어야 한다 — []("0건 확인")를 한쪽만 싣는 드리프트 방지.
    assert service_response.inventory_by_item == []
    assert reply.payload["inventory_by_item"] == []
    assert _norm_scenario_results_from_payload(
        reply.payload["scenario_results"]
    ) == _norm_scenario_results_from_models(service_response.scenario_results or [])
    # 조용한 날의 경고는 signal 이 아니라 POLICY_UNRESOLVED 계열뿐 — 전체 대조로 고정.
    assert reply.payload["soft_warnings"] == service_response.soft_warnings
    assert _business_signals(reply.payload["soft_warnings"]) == set()


# ---------------------------------------------------------------------------
# (d) P1-1 — 스냅샷 로드 실패 분류: 부재는 NOT_READY, 실행 오류는 ERROR (#121 4단계)
# ---------------------------------------------------------------------------


def _raising_loader(error: Exception):
    # ★ 실물 `get_current_logistics_read` 와 **같은 시그니처**여야 한다 (#345).
    #   `sim_run_id` 를 안 받으면 어댑터가 넘기는 순간 `TypeError` 가 나고, 그것을
    #   `_load_read` 의 `except Exception` 이 삼켜 **부재 검사가 ERROR 로 뒤집힌다** —
    #   가짜의 모양이 틀려서 난 실패가 *"어댑터가 부재를 ERROR 로 뭉갠다"* 로 읽힌다.
    def _fn(*, as_of, sim_run_id):
        raise error

    return _fn


def _validation_payload() -> dict[str, Any]:
    return _proposal_payload(
        split_plan=[{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 1000}],
        sourcing_qty=1000,
    )


_MODE_PAYLOADS = [
    pytest.param("PRE_PURCHASE", dict, id="PRE_PURCHASE"),
    pytest.param("STATUS_QUERY", dict, id="STATUS_QUERY"),
    pytest.param("SCENARIO_VALIDATION", _validation_payload, id="SCENARIO_VALIDATION"),
]


@pytest.mark.parametrize(("mode", "payload_factory"), _MODE_PAYLOADS)
def test_데이터_부재는_NOT_READY_다(monkeypatch, mode, payload_factory):
    """Repository 예외 계약의 부재 쪽 — LookupError(fixture 0건 · 필수 정책 미등재).

    다시 불러도 같은 답이므로 재시도 대상이 아니고(M-1 §5.1), 마스터가 사용자에게
    무엇을 달라고 할지 알도록 missing_data 에 **이름**이 남는다.
    """
    monkeypatch.setattr(
        adapter,
        "get_current_logistics_read",
        _raising_loader(LookupError("No active Logistics runtime fixture")),
    )

    request = _request(payload_factory(), mode=mode)
    reply, meta = adapter.logistics_port(request)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    # 이름 없는 NOT_READY 는 계약 위반이다 (M-1 §5.1) — 봉투가 던진다
    assert "logistics_snapshot" in reply.missing_data
    assert validate_reply(request, reply, meta) == ()


@pytest.mark.parametrize(("mode", "payload_factory"), _MODE_PAYLOADS)
@pytest.mark.parametrize(
    "error",
    [
        # DB 장애 — 다시 부르면 성공할 수 있다
        pytest.param(RuntimeError("connection refused: 127.0.0.1:5432"), id="db"),
        # 무결성 위반 ValueError — 활성 fixture 중복 등 (repository 가 부재와 가른다)
        pytest.param(
            ValueError("Expected exactly one active Logistics runtime fixture, found 2"),
            id="duplicate",
        ),
        # 무결성 위반 TypeError — 깨진 행
        pytest.param(
            TypeError("Inventory lot remaining_qty_kg must be a Decimal"), id="broken-row"
        ),
    ],
)
def test_실행_오류는_ERROR_다(monkeypatch, mode, payload_factory, error):
    """✅ 계약 (#121 4단계) — 종전에는 모든 예외가 NOT_READY 로 뭉개졌다.

    DB 장애·env 부재·무결성 위반(ValueError/TypeError)은 회사 상태가 아니라 실행
    실패다 — ERROR 로 나가야 마스터가 재시도할 수 있다. 예외 원문(숫자 포함)은
    reasoning 으로 새지 않는다 — E-REASONING-NUMERIC 함정 방지(재무 400 실측).

    세 예외를 다 도는 이유는 무결성 계열 하나만 대표로 두면 "ValueError 만 부재로
    되돌리는" 뮤턴트가 살아남기 때문이다 (2026-09-01 교차검증 지적).
    """
    monkeypatch.setattr(adapter, "get_current_logistics_read", _raising_loader(error))

    request = _request(payload_factory(), mode=mode)
    reply, meta = adapter.logistics_port(request)

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.payload == {"failed_operation": "load_logistics_snapshot"}
    # 예외 원문이 새지 않는다 — reasoning 에 숫자가 한 자리도 없어야 한다.
    assert not any(character.isdigit() for character in reply.reasoning)
    # ERROR 봉투도 계약을 지킨다 — missing_data 가 비어도 위반이 아니다
    assert validate_reply(request, reply, meta) == ()


# ---------------------------------------------------------------------------
# (e) 어휘 완전성 — missing 코드 발행처와 번역표가 갈리지 않는가 (#121 ⑤)
# ---------------------------------------------------------------------------


def test_모든_미확정_코드에_번역명이_있다():
    """코드 발행처(schemas Literal · rules 경고 상수)와 번역표는 세 파일에 흩어져 있다.

    수동 동기화라 새 코드가 생겨도 아무도 모르고, 번역표에 없으면 사람 화면에는
    generic 이름(`unrecognized_missing_information`)이 나가 **무엇이 없는지가
    사라진다**. 발행처를 기준으로 표를 훑어 그 순간 CI 가 울게 한다.

    ★ 한계 — `UNRESOLVED_WARNING_CODES` 자체가 손으로 유지되는 목록이라, Rule 이
      목록에 없는 새 문자열을 직접 append 하면 여기서도 안 잡힌다. 그 경우 목록에
      추가하는 것이 계약이다.
    """
    published = {*get_args(ConstraintCode), *UNRESOLVED_WARNING_CODES}

    unmapped = sorted(code for code in published if code not in _MISSING_DATA_NAMES)

    assert unmapped == []


def test_번역표에_죽은_이름이_없다():
    """반대 방향 — 아무도 내지 않는 코드가 표에 남아 있으면 그것도 드리프트다.

    (H1_FUTURE_OCCUPANCY_UNRESOLVED 는 Sales rules 가 내므로 발행처에 포함된다.)
    """
    published = {*get_args(ConstraintCode), *UNRESOLVED_WARNING_CODES}

    stale = sorted(code for code in _MISSING_DATA_NAMES if code not in published)

    assert stale == []


def _two_axis_case() -> tuple[InventoryLogisticsSnapshot, dict[str, Any]]:
    """한 시나리오에서 **축이 다른 조정 둘**이 서는 케이스 (#221 · #209 M7).

    창고 만석(8,000/8,000)에 확정 출고 1,000kg(8/25)·1,000kg(8/27) 이 있어
    8/26부터 1,000 · 8/28부터 2,000 이 열린다. lead=2.

        split1  500@8/21  → 도착 8/23 cap 0        → timing 조정 (8/26 에 500 가능)
        split2  800@8/24  → 도착 8/26 여유 1,000 − 원안 500 = 500
                            800 > 500 이고 여유가 0 이 아니다  → quantity 조정 (500)

    ★ 조정안이 **2건 나오는 유일한 픽스처**다. 다른 픽스처는 전부 1건이라
      리스트 순서를 뒤집어도 결과가 같았다.
    """
    snapshot = _snapshot(
        used_capacity_kg=Decimal(8000),
        on_hand_by_lot=[
            {
                "lot_id": "LOT-TWO-AXIS",
                "item": "배추",
                "available_qty_kg": Decimal(2000),
                "remaining_freshness_days": 5,
                "effective_freshness_limit_days": 10,
                "status": "ACTIVE",
            }
        ],
        confirmed_outbound_schedule=[
            {"date": date(2026, 8, 25).isoformat(), "quantity_kg": 1000, "item": "배추"},
            {"date": date(2026, 8, 27).isoformat(), "quantity_kg": 1000, "item": "배추"},
        ],
        outbound_commitments=[],
    )
    payload = _payload_of(
        [
            _scenario_block(
                "기본",
                [
                    {"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 500},
                    {"seq": 2, "date": date(2026, 8, 24).isoformat(), "qty_kg": 800},
                ],
                1300,
            )
        ]
    )
    return snapshot, payload


def test_조정안이_둘이면_등장_순서를_지킨다(monkeypatch):
    """✅ 계약 — 조정안 **리스트의 순서**를 고정한다 (#221 · `#209` 가 남긴 구멍).

    🔴 `#209` 변이 7건 중 `M7`(조정안 리스트 순서 뒤집기)만 안 잡혔다. 조정안이
    1건 나오는 픽스처뿐이라 뒤집어도 결과가 같았기 때문이다. 라벨 순서(`M5`)는
    잡혔지만 **리스트 순서는 재는 수단이 없었다.**

    받는 쪽이 순서에 뜻을 둔다 — 마스터 화면이 첫 조정안을 먼저 읽고,
    `preferred_adjustment` 는 별도 축이라 리스트가 뒤집히면 화면의 우선순위와
    엇갈린다. 조용히 바뀌면 아무도 모른다.

    ⚠️ 운영 코드는 이 판에서 안 고쳤다. 지금 동작이 이미 맞다(수집 `dict` 의
    삽입 순서 보존). 재는 검사만 없었다.
    """
    snapshot, payload = _two_axis_case()
    _wire(monkeypatch, snapshot)

    reply, _meta = adapter.logistics_port(_request(payload))

    assert reply.payload["scenario_results"][0]["verdict"] == "conditional"

    # 🔴 축이 다른 조정 둘이 서고, split_plan 등장 순서를 그대로 지킨다.
    assert [row.axis for row in reply.suggested_adjustments] == ["timing", "quantity"]
    assert [row.split_date for row in reply.suggested_adjustments] == [
        AS_OF,
        date(2026, 8, 24),
    ]

    # 두 조정이 합쳐지지 않는다 — dedup 키가 다르다.
    assert len(reply.suggested_adjustments) == 2
    assert all(row.scenario_labels == ("기본",) for row in reply.suggested_adjustments)
