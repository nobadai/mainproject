"""물류 행동 고정 테스트 — 감사 P0 2건의 현재 동작 박제 + Adapter↔Service parity (#121 1단계).

★ 이 파일의 (a)·(b) 테스트는 **버그를 승인하는 것이 아니라 고정하는 것**이다.
  수정 이슈(#121 2·3단계)에서 기대값을 반전하는 것이 곧 수정 완료의 판정이 된다.
  `PIN(현재 동작)` 표시가 붙은 assert 가 반전 대상이다.

★ (c) parity 는 재현이 아니라 상시 안전망이다 — Core 에 값이 추가되고 한쪽 조립에만
  반영되는 드리프트(PR #116 에서 실제로 일어난 일)를 자동으로 잡는다.
  **응답 전체를 비교하지 않는다.** 두 경로는 계약이 다르다(Evidence · missing_data
  어휘 · LLM 필드) — 공유돼야 하는 결정론 값만 대조한다.

★ DB 를 타지 않는다. 스냅샷은 양쪽에 동일 객체를 직접 주입한다 — 로드 실패 처리
  차이(#121 4단계 건)를 여기에 섞지 않는다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.logistics import adapter
from app.logistics.llm.runtime import InterpretationService, LLMSettings, UnavailableProvider
from app.logistics.rules import BUSINESS_SIGNALS
from app.logistics.scenario_engine import (
    derive_preferred_adjustment,
    validate_purchase_scenarios,
)
from app.logistics.schemas import InventoryLogisticsSnapshot, PurchaseAgentOutput
from app.logistics.service import run_logistics_procurement_with_snapshot
from app.master.envelope import AgentRequest, ExecutionContext

AS_OF = date(2026, 8, 21)


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-PIN-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
    )


def _request(payload: dict[str, Any]) -> AgentRequest:
    return AgentRequest(
        context=_ctx(), agent="inventory", mode="SCENARIO_VALIDATION", payload=payload
    )


def _snapshot(**overrides: Any) -> InventoryLogisticsSnapshot:
    base: dict[str, Any] = {
        "snapshot_id": "PIN-SNAP-1",
        "as_of": AS_OF,
        "on_hand_by_lot": [],
        "in_transit": [],
        "confirmed_inbound_schedule": [],
        "confirmed_outbound_schedule": [],
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


def _proposal_payload(
    split_plan: list[dict[str, Any]],
    sourcing_qty: int,
) -> dict[str, Any]:
    """사중 일치(수량 3축 + 금액)와 seq·날짜 규칙을 지키는 최소 제안."""
    unit_price = 1650
    return {
        "meta": {
            "as_of": AS_OF.isoformat(),
            "item": "배추",
            "agent_version": "v1.1",
            "is_refeed": False,
            "feedback_attempt": 0,
        },
        "scenarios": [
            {
                "label": "기본",
                "strategy_type": "quantity",
                "coverage_days": 5,
                "total_qty_kg": sourcing_qty,
                "total_amount_krw": sourcing_qty * unit_price,
                "max_price": 1750,
                "margin_warning": False,
                "split_plan": split_plan,
                "sourcing_plan": [
                    {
                        "market": "가락",
                        "grade": "상",
                        "qty_kg": sourcing_qty,
                        "grade_unit_price": unit_price,
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
        ],
        "confidence": "high",
        "situation": "stable",
        "context_docs_used": [],
        "rejected_reasons": [],
    }


def _wire(monkeypatch: pytest.MonkeyPatch, snapshot: InventoryLogisticsSnapshot) -> None:
    monkeypatch.setattr(adapter, "_load_snapshot", lambda as_of: snapshot)
    monkeypatch.setattr(adapter, "_load_policy", lambda: None)


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
# (a) P0-1 — 최상위 판정이 시나리오 판정을 집계하지 않는다
# ---------------------------------------------------------------------------


def test_전_시나리오_reject_인데_business_status_는_ok_다(monkeypatch):
    """🔴 PIN(현재 동작) — #121 3단계에서 기대값 반전 대상.

    하드 제약을 전부 PASS 로 만들면(창고 정책값 전부 존재 + zone 존재) 최상위 판정은
    `derive_logistics_verdict` 가 hard_constraints 만 보고 PASS → `ok` 를 낸다.
    같은 응답의 `scenario_results` 는 전부 reject 다 — 창고가 꽉 차 어느 날짜로도
    수용 불가한 제안이기 때문이다.

    마스터 `_acceptable` 은 business_status 만 보고 재호출을 판단하므로, 이 조합은
    "물류상 실행 불가능한 안이 통과" 로 이어진다. 실환경은 zone=None(LOG-H02
    UNRESOLVED)이라 같은 이유로 `conditional` 이 나온다(PR #119 실측) — 여기서는
    집계 단절만 분리해 보기 위해 전부 PASS 로 둔다.
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

    # 🔴 PIN(현재 동작): 시나리오가 전부 reject 인데 최상위는 ok 다.
    #    집계 규칙 확정(#121 3단계) 후 이 두 줄의 기대값을 반전한다.
    assert reply.payload["verdict"] == "ok"
    assert reply.business_status == "ok"
    # 아무 신호도 마스터로 가지 않는다 — 조정 제안이 없어 followup 도 서지 않는다.
    assert reply.suggested_adjustments == ()
    assert reply.needs_followup is False


# ---------------------------------------------------------------------------
# (b) P0-2 — reject 시나리오가 앞 회차 adjustment 를 유지한 채 새어 나간다
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


def test_reject_시나리오에_앞_회차_adjustment_가_남는다():
    """🔴 PIN(현재 동작) — #121 2단계에서 기대값 반전 대상.

    `derive_preferred_adjustment` docstring 은 "reject 시나리오는 adjustments 가
    비므로 자연히 집계에서 빠진다" 고 전제하지만, multi-split 에서는 거짓이다 —
    뒤 회차의 불가 판정이 앞 회차에서 이미 쌓인 adjustment 를 지우지 않는다.
    """
    snapshot, payload = _multi_split_case()
    proposal = PurchaseAgentOutput.model_validate(payload)

    results = validate_purchase_scenarios(proposal, snapshot)

    assert len(results) == 1
    result = results[0]
    assert result.verdict == "reject"
    assert "NO_FEASIBLE_ARRIVAL_DATE" in result.reason_codes

    # 🔴 PIN(현재 동작): 불가 판정된 안에 1회차 수량 조정이 그대로 남아 있고,
    #    preferred 집계가 그 축을 우선 조정으로 뽑는다. 수정 후에는
    #    파생 채널(preferred)에서 reject 시나리오가 제외되어야 한다.
    assert len(result.adjustments) == 1
    assert result.adjustments[0].axis == "quantity"
    assert result.adjustments[0].suggested_qty_kg == Decimal(1000)
    assert derive_preferred_adjustment(results) == "quantity"


def test_reject_시나리오의_조정이_M1_행동_제안_채널로_나간다(monkeypatch):
    """🔴 PIN(현재 동작) — #121 2단계에서 기대값 반전 대상.

    어댑터의 suggested 조립 루프에 verdict 필터가 없어, 불가 판정된 안의 조정이
    M-1 전용 채널(`suggested_adjustments`)과 `needs_followup=True` 로 승격된다.
    수정 후: scenario_results.adjustments(진단 정보)는 유지하되
    suggested/needs_followup 은 reject 만으로 서지 않아야 한다.
    """
    snapshot, payload = _multi_split_case()
    _wire(monkeypatch, snapshot)

    reply, _meta = adapter.logistics_port(_request(payload))

    results = reply.payload["scenario_results"]
    assert [row["verdict"] for row in results] == ["reject"]
    # 진단 정보 자체는 수정 후에도 유지된다 — 반전 대상이 아니다.
    assert results[0]["adjustments"][0]["suggested_qty_kg"] == 1000.0

    # 🔴 PIN(현재 동작): 반전 대상 세 줄.
    assert reply.payload["preferred_adjustment"] == "quantity"
    assert len(reply.suggested_adjustments) == 1
    assert reply.suggested_adjustments[0].axis == "quantity"
    assert reply.needs_followup is True


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

    # ① runtime — 같은 rules 결과를 쓴다
    assert reply.runtime_status == service_response.runtime_status

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
