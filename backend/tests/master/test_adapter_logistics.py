"""물류 어댑터 — 번역이 계약을 지키는가.

★ DB 를 타지 않는다. `_load_snapshot` · `_load_policy` 를 갈아 끼워 **번역만** 시험한다.
  값의 정확성은 `app.logistics.tools` 의 테스트가 본다.

★ 여기서 특히 보는 것은 **없는 값을 지어내지 않는가**다.
  `rental_cap_kg` 을 `burst − guaranteed` 로 채우면 숫자는 나오고 에러도 안 나며
  봉투도 통과한다. 그 조용한 통과를 막는 것이 이 파일의 절반이다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.logistics.schemas import InventoryLogisticsSnapshot, LogisticsPolicy
from app.master.adapters import logistics as adapter
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply

AS_OF = date(2025, 12, 31)


def ctx(as_of: date = AS_OF) -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-T-0001",
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
    )


def req(mode="PRE_PURCHASE", as_of: date = AS_OF, payload=None) -> AgentRequest:
    return AgentRequest(context=ctx(as_of), agent="inventory", mode=mode, payload=payload or {})


class _Lot:
    """`build_lot_constraints` 가 돌려주는 모양만 흉내 낸다 — 어댑터가 읽는 6필드."""

    def __init__(self, lot_id: str, qty: str, freshness: int | None, grade: str | None = None):
        self.lot_id = lot_id
        self.item = "배추"
        self.available_qty_kg = Decimal(qty)
        self.remaining_freshness_days = freshness
        self.grade = grade
        self.status = "ACTIVE"


def _policy() -> LogisticsPolicy:
    """★ 진짜 모델로 만든다.

    필드를 흉내 낸 가짜를 쓰면 **물류가 계약을 넓힐 때 이 테스트가 안 깨진다** —
    어댑터가 없는 필드를 읽어도 통과해 버린다.
    """
    return LogisticsPolicy(
        guaranteed_capacity_kg=Decimal(8000),
        burst_capacity_kg=Decimal(9600),
        inbound_lead_days=2,
        daily_inbound_capacity_kg=Decimal(5000),
        inbound_transport_capacity_kg=Decimal(5000),
        shared_daily_outbound_capacity_kg=Decimal(5000),
        cap_by_date_policy="CONFIRMED_ONLY",
        policy_version="v1.3-PROVISIONAL",
        usage_scope="AGENT_MVP_DEMO",
        source_refs={
            "guaranteed_capacity_kg": "MVP-DECISION-20260825:N2-INDEPENDENT-SLA",
            "burst_capacity_kg": "MVP-DECISION-20260825:N2-INDEPENDENT-SLA",
            "inbound_lead_days": "MVP-DECISION-20260825:N4",
            "daily_inbound_capacity_kg": "MVP-DECISION-20260825:L-INBOUND",
            "inbound_transport_capacity_kg": "MVP-DECISION-20260825:N4-TRANSPORT",
            "shared_daily_outbound_capacity_kg": "MVP-DECISION-20260825:N17",
            "cap_by_date_policy": "PROJECT-DEFINITION-V1.2:N15",
        },
    )


def _snapshot(**overrides) -> InventoryLogisticsSnapshot:
    base: dict = {
        "snapshot_id": "LOG-SNAP-1",
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
        "evidence_refs": [
            "DB:logistics_runtime_fixture/LOG-RUNTIME-1",
            "DB:inventory_lots/sim_run_id=SIM-1",
        ],
    }
    return InventoryLogisticsSnapshot(**{**base, **overrides})


#: LOT-B 는 등급·신선도가 **확인되지 않은** Lot 이다 — 둘 다 None 으로 나가야 한다.
_LOTS = [_Lot("LOT-A", "300.5", 10, grade="상"), _Lot("LOT-B", "200", None)]


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(adapter, "_load_snapshot", lambda as_of: _snapshot())
    monkeypatch.setattr(adapter, "_load_policy", _policy)
    monkeypatch.setattr(adapter, "build_lot_constraints", lambda snapshot: list(_LOTS))


# ---------------------------------------------------------------------------
# PRE_PURCHASE
# ---------------------------------------------------------------------------


def test_봉투_검증을_통과한다(wired):
    """어댑터가 findings 를 내면 남 탓할 자리가 없다 — 우리가 만든 것이다."""
    request = req()
    reply, meta = adapter.logistics_port(request)
    assert reply.runtime_status == "READY"
    assert validate_reply(request, reply, meta) == ()


def test_창고_여유는_보장치에서_점유를_뺀_값이다(wired):
    """★ 기준이 `guaranteed`(8,000)지 `burst`(9,600)가 아니다.

    물류 자신의 `calculate_cap_by_date` 가 `guaranteed − projected_occupancy` 로 쓴다.
    burst 를 기준으로 삼으면 **살 수 있는 양이 1,600kg 늘어난 채로 조용히 돈다.**
    """
    reply, _ = adapter.logistics_port(req())
    assert reply.payload["warehouse_free_kg"] == 7000.0


def test_rental_cap_kg_는_burst_에서_파생하지_않는다(wired):
    """🔴 **이 파일에서 가장 중요한 검사.**

    `burst − guaranteed = 1,600` 은 그럴듯하지만 다른 개념이다 — burst 는 3PL 의
    순간 초과 허용이고 rental 은 창고 임대다. 채웠으면 숫자도 나오고 봉투도 통과했다.

    비워 두고 물었더니 **물류가 `0` 으로 확정**했다 (2026-08-27 회신 §1).
    추측했더라면 1,600kg 을 더 살 수 있다고 매입에 알렸을 것이다.
    """
    reply, _ = adapter.logistics_port(req())
    assert reply.payload["rental_cap_kg"] == 0.0
    assert reply.payload["burst_capacity_kg"] == 9600.0  # 값은 싣되 파생하지 않는다


def test_rental_cap_0_은_미확정이_아니다(wired):
    """★ `0` 과 *"모른다"* 는 다르다 (물류 회신 §7).

    매입은 이 값을 창고 상한에 더한다 — 모르는 값을 0 으로 쓰면 **살 수 있는 양을
    실제보다 적게** 잡고, 확정 0 을 미확정으로 두면 **매입이 아예 못 돈다.**
    """
    reply, _ = adapter.logistics_port(req())
    assert "rental_cap_kg" not in reply.missing_data
    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    evidence = next(e for e in reply.evidences if e.claim == "rental_cap_kg")
    assert evidence.evidence_grade == "SIM_FIXED"
    assert "확정" in evidence.evidence_detail


def test_DB_에_없는_정책값은_출처_부재를_밝힌다(wired):
    """🔴 재무 `payroll_date` 가 Schema default 로 조용히 쓰이던 것과 같은 자리다.

    값은 쓰되 **DB 에서 온 것이 아니라는 사실**이 남는다. 등록되면 저절로 사라진다.
    """
    reply, _ = adapter.logistics_port(req())
    assert "rental_cap_kg@policy_source_ref" in reply.missing_data


def test_cap_by_date_는_리드타임_다음날부터_창_길이만큼이다(wired):
    reply, _ = adapter.logistics_port(req())
    cap = reply.payload["cap_by_date"]
    assert len(cap) == adapter._CAP_WINDOW_DAYS
    assert min(cap) == "2026-01-02"  # as_of + inbound_lead_days(2)
    assert reply.payload["cap_by_date_window_days"] == adapter._CAP_WINDOW_DAYS


def test_조회_창을_payload_에_밝힌다(wired):
    """창 밖의 날짜를 받는 쪽이 **0 으로 읽지 않게** 한다 (§1.2-10)."""
    reply, _ = adapter.logistics_port(req())
    assert "cap_by_date_window_days" in reply.payload


def test_리드타임이_없으면_cap_by_date_를_비우지_않고_밝힌다(wired, monkeypatch):
    """빈 dict 를 실으면 *못 받은 것* 과 *받았는데 빈 것* 이 구분되지 않는다."""

    monkeypatch.setattr(adapter, "_load_snapshot", lambda as_of: _snapshot(inbound_lead_days=None))
    reply, _ = adapter.logistics_port(req())
    assert "cap_by_date" not in reply.payload
    assert "cap_by_date" in reply.missing_data
    assert "inbound_lead_days" in reply.missing_data


def test_Lot_근거는_번호가_아니라_lot_id_로_가리킨다(wired):
    """★ 번호로 가리키면 **Lot 순서가 바뀌는 날 다른 Lot 을 가리킨다.**"""
    reply, _ = adapter.logistics_port(req())
    claims = {e.claim for e in reply.evidences}
    assert "lots[LOT-A].available_qty_kg" in claims
    assert "lots[LOT-A].remaining_freshness_days" in claims


def test_신선도가_없는_Lot_은_근거를_만들지_않는다(wired):
    """0 으로 채우지 않는다 — 없는 값에 근거를 다는 것은 지어내는 것이다."""
    reply, _ = adapter.logistics_port(req())
    claims = {e.claim for e in reply.evidences}
    assert "lots[LOT-B].available_qty_kg" in claims
    assert "lots[LOT-B].remaining_freshness_days" not in claims
    lot_b = next(lot for lot in reply.payload["lots"] if lot["lot_id"] == "LOT-B")
    assert lot_b["remaining_freshness_days"] is None


def test_Lot_근거는_Lot_을_담은_참조를_가리킨다(wired):
    """runtime fixture 를 가리키면 *"이 수량이 어디서 왔나"* 가 엉뚱한 곳에 닿는다."""
    reply, _ = adapter.logistics_port(req())
    lot_ev = next(e for e in reply.evidences if e.claim.startswith("lots[LOT-A]"))
    assert "inventory_lots" in lot_ev.ref_ids[0]


def test_Lot_등급은_물류가_준_값_그대로_실린다(wired):
    """등급은 DB 에서 오는 값이라 **물류가 실어야** 매입 등급 배분이 볼 수 있다.

    `None` 이어도 키를 빼지 않는다 — *"등급 축이 없다"* 와 *"등급을 모른다"* 는
    다르다. 어댑터는 값을 만들지 않고 그대로 옮기기만 한다.
    """
    reply, _ = adapter.logistics_port(req())
    lots = {lot["lot_id"]: lot for lot in reply.payload["lots"]}

    assert lots["LOT-A"]["grade"] == "상"
    assert "grade" in lots["LOT-B"]
    assert lots["LOT-B"]["grade"] is None
    # 매입이 stocked_at/shelf_life_days 로 다시 계산하지 않도록 파생 원재료는 안 싣는다.
    assert not {"stocked_at", "shelf_life_days", "remaining_kg"} & lots["LOT-A"].keys()


def test_스냅샷_기준일이_다르면_판단하지_않는다(wired):
    """다른 날의 재고는 그날의 사실이 아니다 (§1.2-6)."""
    request = req(as_of=date(2026, 8, 21))
    reply, _ = adapter.logistics_port(request)
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("logistics_snapshot@2026-08-21",)


def test_스냅샷이_없으면_ERROR_가_아니라_NOT_READY(wired, monkeypatch):
    """다시 불러도 같다 — 재시도 가치가 다르다 (M-1 §5.1)."""
    monkeypatch.setattr(adapter, "_load_snapshot", lambda as_of: None)
    reply, _ = adapter.logistics_port(req())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"


def test_LLM_을_타지_않는다(wired):
    """해석 서비스를 부르는 `enrich_logistics_response` 를 지나지 않는다."""
    _, meta = adapter.logistics_port(req())
    assert meta.llm_status == "DISABLED"


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION
# ---------------------------------------------------------------------------


def _proposal_payload() -> dict:
    """매입 실물 스키마와 같은 모양 + 어댑터가 얹는 `allowed_axes`."""
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
                "total_qty_kg": 1000,
                "total_amount_krw": 1650000,
                "max_price": 1750,
                "margin_warning": False,
                "split_plan": [{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 1000}],
                "sourcing_plan": [
                    {"market": "가락", "grade": "상", "qty_kg": 1000, "grade_unit_price": 1650}
                ],
                "expected_margin_rate": 0.30,
                "rationale": [
                    {
                        "source": "예측",
                        "claim": "2주 후 +14%",
                        "ref_id": "FC-1",
                        "evidence_grade": "OFFICIAL",
                        "evidence_detail": "ML q50",
                    }
                ],
                "risks": [],
            }
        ],
        "confidence": "high",
        "situation": "stable",
        "context_docs_used": ["DOC-3"],
        "rejected_reasons": [],
        # ★ 매입 어댑터가 얹는 키다. `PurchaseProposal` 은 extra="forbid" 라
        #   그대로 넣으면 통째로 실패한다 — 걸러 내는지 본다.
        "allowed_axes": ["quantity", "timing"],
    }


def test_제안을_매입_실물_스키마로_되살린다(wired):
    """★ 물류는 `PurchaseAgentOutput = PurchaseProposal` 로 매입 스키마를 그대로 쓴다.

    이름을 손으로 맞추는 자리가 없으므로 **조용히 틀릴 자리도 없다.**
    """
    request = req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    reply, meta = adapter.logistics_port(request)
    assert reply.runtime_status == "READY"
    assert reply.payload["verdict"] in {"ok", "conditional", "reject", "skipped"}
    assert validate_reply(request, reply, meta) == ()


def test_어댑터가_얹은_키는_걸러_낸다(wired):
    """`allowed_axes` 가 들어 있어도 되살리기가 실패하지 않는다."""
    payload = _proposal_payload()
    assert "allowed_axes" in payload
    proposal = adapter._as_proposal(payload)
    assert proposal is not None
    assert proposal.meta.item == "배추"


def test_제안을_못_읽으면_NOT_READY(wired):
    request = req(mode="SCENARIO_VALIDATION", payload={"scenarios": []})
    reply, _ = adapter.logistics_port(request)
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("purchase_proposal",)


def test_도착일은_리드타임을_더한_날이다(wired):
    request = req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    reply, _ = adapter.logistics_port(request)
    # split_plan 이 as_of 하루 + lead 2일
    assert reply.payload["expected_arrival_dates"] == ["2026-01-02"]


def test_REVIEW_REQUIRED_는_conditional_로_옮긴다():
    """재무와 같은 매핑이다 (정의서 §7.1)."""
    assert adapter._VERDICT_MAP["REVIEW_REQUIRED"] == "conditional"
    assert adapter._VERDICT_MAP["PASS"] == "ok"
    assert adapter._VERDICT_MAP["FAIL"] == "reject"


def test_모르는_mode_는_능력_없음으로_답한다(wired):
    request = req(mode="STATUS_QUERY")
    reply, _ = adapter.logistics_port(request)
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_capability == ("STATUS_QUERY 번역",)


def test_물류가_NOT_READY_면_반드시_이름이_남는다(wired, monkeypatch):
    """🔴 `rules` 는 물류가 정하고 `missing` 은 어댑터가 따로 모은다 — 어긋날 수 있다.

    물류 Rule 이 막았는데 어댑터가 읽은 값이 다 멀쩡하면 `missing_data` 가 빈 채로
    `RUNTIME_NOT_READY` 가 나가고, **봉투가 ContractViolation 을 던진다**(M-1 §5.1).

    지금은 `rental_cap_kg@policy_source_ref` 가 늘 들어 있어 우연히 안 비어 있다.
    **DB 에 그 키가 등록되는 날 터진다.** 그때를 미리 재현한다.
    """
    monkeypatch.setattr(
        adapter,
        "_load_policy",
        lambda: _policy().model_copy(
            update={"source_refs": {**_policy().source_refs, "rental_cap_kg": "MVP:RENTAL"}}
        ),
    )
    monkeypatch.setattr(
        adapter,
        "evaluate_procurement_rules",
        lambda **kw: {
            "runtime_status": "RUNTIME_NOT_READY",
            "calculation_ready": True,  # 계산은 됐는데 Rule 이 막은 경우
            "hard_constraints": [_Check("IN_TRANSIT_SCHEDULE_UNRESOLVED", "UNRESOLVED")],
            "soft_warnings": [],
        },
    )
    reply, _ = adapter.logistics_port(req())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data  # 비어 있으면 봉투가 던진다
    assert "logistics_rule/IN_TRANSIT_SCHEDULE_UNRESOLVED" in reply.missing_data


class _Check:
    """`ConstraintResult` 의 어댑터가 읽는 두 필드만 흉내 낸다."""

    def __init__(self, code: str, status: str):
        self.code = code
        self.status = status
