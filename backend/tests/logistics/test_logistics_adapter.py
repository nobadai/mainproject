"""물류 어댑터 — 번역이 계약을 지키는가.

★ DB 를 타지 않는다. `_load_read`(Snapshot + Policy 한 벌)를 갈아 끼워 **번역만** 시험한다.
  값의 정확성은 `app.logistics.tools` 의 테스트가 본다.

★ 여기서 특히 보는 것은 **없는 값을 지어내지 않는가**다.
  `rental_cap_kg` 을 `burst − guaranteed` 로 채우면 숫자는 나오고 에러도 안 나며
  봉투도 통과한다. 그 조용한 통과를 막는 것이 이 파일의 절반이다.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.logistics import adapter
from app.logistics.repository import LogisticsRead
from app.logistics.schemas import (
    InventoryLogisticsSnapshot,
    InventoryLotSnapshot,
    ItemStoragePolicyFact,
    LogisticsPolicy,
    OutboundCommitment,
    ScheduledQuantity,
)
from app.logistics.tools import build_lot_constraints as real_build_lot_constraints
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply

AS_OF = date(2025, 12, 31)

#: 이 파일이 쓰는 **테스트 전용** 실행 축 (#345).
#
# ★ `BURN_IN_SIM_RUN_ID` 를 쓰지 않는다. 운영값을 넣으면 어댑터가 값을 **나르는지**
#   아니면 어딘가에서 **주워 오는지** 구별이 안 된다 — 봉투가 준 값이 그대로
#   Repository 로 가는 것을 보려면 여기서만 나오는 값이어야 한다.
SIM_RUN_ID = "SIM-T-0001"


def ctx(as_of: date = AS_OF, sim_run_id: str = SIM_RUN_ID) -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-T-0001",
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
        sim_run_id=sim_run_id,
    )


def req(
    mode="PRE_PURCHASE", as_of: date = AS_OF, payload=None, sim_run_id: str = SIM_RUN_ID
) -> AgentRequest:
    return AgentRequest(
        context=ctx(as_of, sim_run_id), agent="inventory", mode=mode, payload=payload or {}
    )


class _Lot:
    """`build_lot_constraints` 가 돌려주는 모양만 흉내 낸다 — 어댑터가 읽는 6필드.

    ★ `grade` 는 #77 로 `LotConstraint` 에 생겼다. 기본값을 `None` 으로 둔 것은
      실물이 그렇기 때문이다 — raw `'상품'` 은 정규화 근거가 없어 `None` 으로 온다.
    """

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
        "outbound_commitments": [],
        "used_capacity_kg": Decimal(1000),
        "guaranteed_capacity_kg": Decimal(8000),
        "burst_capacity_kg": Decimal(9600),
        "guaranteed_capacity_by_zone_kg": None,
        "inbound_lead_days": 2,
        "daily_inbound_capacity_kg": Decimal(5000),
        "inbound_transport_capacity_kg": Decimal(5000),
        "shared_daily_outbound_capacity_kg": Decimal(5000),
        # 실물 Repository 가 늘 싣는다 — 재고가 0kg 인 품목의 보관 한계도 매입은 알아야
        # 해서 Lot 목록과 별개로 읽는다. `대파` 는 DB 값이 비어 있는 경우다.
        "item_storage_policies": [
            ItemStoragePolicyFact(
                item="배추",
                operational_limit_days=15,
                medium_grade_factor=Decimal("0.8"),
            ),
            ItemStoragePolicyFact(item="대파"),
        ],
        "evidence_refs": [
            "DB:logistics_runtime_fixture/LOG-RUNTIME-1",
            "DB:inventory_lots/sim_run_id=SIM-1",
            "DB:item_storage_policies",
        ],
    }
    return InventoryLogisticsSnapshot(**{**base, **overrides})


_LOTS = [_Lot("LOT-A", "300.5", 10), _Lot("LOT-B", "200", None)]


def _read(snapshot=None, policy=None):
    """어댑터의 단일 읽기 seam — Snapshot 과 그것을 만든 Policy 를 함께 준다 (#121 ⑤)."""
    return LogisticsRead(snapshot=snapshot or _snapshot(), policy=policy or _policy())


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read())
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

    monkeypatch.setattr(
        adapter, "_load_read", lambda *, as_of, sim_run_id: _read(_snapshot(inbound_lead_days=None))
    )
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


# ---------------------------------------------------------------------------
# cap_by_date — Evidence 가 실제 계산을 설명하는가
# ---------------------------------------------------------------------------


def test_cap_by_date_근거는_실제_계산을_설명한다(wired):
    """🔴 근거가 **실제 계산과 다른 말**을 하면 검증은 통과하고 사람만 속는다.

    1차 MVP 의 Hard Capacity 는 `guaranteed_capacity_kg` 하나다
    (`calculate_cap_by_date` — burst·일일입고·운송은 판정에 개입하지 않는다).
    `min(창고여유, 일일입고, 운송)` 은 그 정책 이전의 설명이라 남아 있으면 안 된다.
    """
    reply, _ = adapter.logistics_port(req())
    detail = next(e for e in reply.evidences if e.claim == "cap_by_date").evidence_detail
    assert "min(" not in detail
    assert "guaranteed_capacity_kg" in detail


def test_시나리오_판정의_cap_by_date_근거도_같은_계산을_설명한다(wired):
    """두 mode 가 같은 Tool 을 부르므로 설명도 같아야 한다."""
    request = req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    reply, _ = adapter.logistics_port(request)
    detail = next(e for e in reply.evidences if e.claim == "cap_by_date").evidence_detail
    assert "min(" not in detail
    assert "guaranteed_capacity_kg" in detail


def test_창고여유_근거는_cap_by_date_와_같은_값이라고_말하지_않는다(wired):
    """★ `warehouse_free_kg` 는 as_of 점유 기준이고 `cap_by_date` 는 도착일별이다.

    뺄셈의 모양이 같아 헷갈리지만 **같은 값이 아니다.** 같다고 쓰면 받는 쪽이
    하루치 여유를 18일 내내 쓸 수 있는 것으로 읽는다.
    """
    reply, _ = adapter.logistics_port(req())
    detail = next(e for e in reply.evidences if e.claim == "warehouse_free_kg").evidence_detail
    # 이제는 존재하지 않는 이름을 가리키지 않는다
    assert "free_capacity" not in detail
    assert "일치하지 않는다" in detail


# ---------------------------------------------------------------------------
# item_storage_policies — 품목 단위 보관 정책
# ---------------------------------------------------------------------------


def test_품목_보관정책을_PRE_payload_에_싣는다(wired):
    """Repository→Snapshot 까지 온 값이 매입에 닿지 않으면 조회한 의미가 없다."""
    reply, _ = adapter.logistics_port(req())
    policies = reply.payload["item_storage_policies"]
    baechu = next(row for row in policies if row["item"] == "배추")
    assert baechu["operational_limit_days"] == 15
    assert baechu["medium_grade_factor"] == 0.8


def test_보관한계는_Lot_잔여_신선도와_다른_값이다(wired):
    """🔴 **개념이 다르다.**

    `lots[].remaining_freshness_days` 는 *이미 있는 그 Lot* 이 앞으로 며칠 쓸 수 있나이고,
    `operational_limit_days` 는 *그 품목을 새로 들일 때* 적용할 보관 한계다.
    새 매입의 기준은 후자라 Lot 잔여일수에서 역산하면 **살 수 있는 양이 조용히 줄어든다.**
    """
    reply, _ = adapter.logistics_port(req())
    lot_a = next(lot for lot in reply.payload["lots"] if lot["lot_id"] == "LOT-A")
    baechu = next(row for row in reply.payload["item_storage_policies"] if row["item"] == "배추")
    assert lot_a["item"] == baechu["item"]  # 같은 품목인데
    assert lot_a["remaining_freshness_days"] == 10  # 값이 다르다 — 서로 다른 사실이다
    assert baechu["operational_limit_days"] == 15
    # 어느 한쪽이 다른 쪽을 대체하지 않는다 — 둘 다 payload 에 남는다
    assert "remaining_freshness_days" not in baechu
    assert "operational_limit_days" not in lot_a


def test_보관정책_근거는_번호가_아니라_품목명으로_가리킨다(wired):
    """Lot 과 같은 이유다 — 번호로 쓰면 품목 순서가 바뀌는 날 다른 품목을 가리킨다."""
    reply, _ = adapter.logistics_port(req())
    claims = {e.claim for e in reply.evidences}
    assert "item_storage_policies[배추].operational_limit_days" in claims
    assert "item_storage_policies[배추].medium_grade_factor" in claims


def test_보관정책_근거는_정책_테이블을_가리킨다(wired):
    """ref_id 를 지어내지 않는다 — Repository 가 실은 DB 참조를 그대로 쓴다."""
    reply, _ = adapter.logistics_port(req())
    evidence = next(e for e in reply.evidences if e.claim.startswith("item_storage_policies[배추]"))
    assert evidence.ref_ids == ("DB:item_storage_policies",)


def test_정책값이_없는_품목은_근거를_만들지_않는다(wired):
    """없는 값에 근거를 붙이면 *"확인했다"* 는 거짓이 된다 — 0 으로도 채우지 않는다."""
    reply, _ = adapter.logistics_port(req())
    claims = {e.claim for e in reply.evidences}
    assert "item_storage_policies[대파].operational_limit_days" not in claims
    assert "item_storage_policies[대파].medium_grade_factor" not in claims
    daepa = next(row for row in reply.payload["item_storage_policies"] if row["item"] == "대파")
    assert daepa["operational_limit_days"] is None
    assert daepa["medium_grade_factor"] is None


def test_보관정책이_미조회면_빈_배열로_덮지_않는다(wired, monkeypatch):
    """`None`(미조회)과 `[]`(정책 0 건 확인)은 다르다 (§1.2-10)."""
    monkeypatch.setattr(
        adapter,
        "_load_read",
        lambda *, as_of, sim_run_id: _read(_snapshot(item_storage_policies=None)),
    )
    reply, _ = adapter.logistics_port(req())
    assert "item_storage_policies" not in reply.payload
    assert "item_storage_policies" in reply.missing_data


def test_보관정책을_실어도_봉투_검증을_통과한다(wired):
    """🔴 배열 항목 안의 숫자에는 봉투가 **항목마다 Evidence 를 요구한다.**

    근거 없이 payload 에만 넣으면 `E-EVIDENCE-MISSING` 으로 물류 회신이 통째로 막힌다.
    """
    request = req()
    reply, meta = adapter.logistics_port(request)
    assert "item_storage_policies" in reply.payload
    assert validate_reply(request, reply, meta) == ()


def test_스냅샷_기준일이_다르면_판단하지_않는다(wired):
    """다른 날의 재고는 그날의 사실이 아니다 (§1.2-6)."""
    request = req(as_of=date(2026, 8, 21))
    reply, _ = adapter.logistics_port(request)
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("logistics_snapshot@2026-08-21",)


def test_스냅샷이_없으면_ERROR_가_아니라_NOT_READY(wired, monkeypatch):
    """다시 불러도 같다 — 재시도 가치가 다르다 (M-1 §5.1)."""
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: None)
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


def test_모르는_mode_는_능력_없음으로_답한다():
    """🔴 이제 **공개 경로로는 도달할 수 없다.**

    봉투가 `AgentRequest` 에서 mode 를 검증하는데(`ContractViolation`), inventory 의
    허용 mode 셋이 전부 구현됐다. 그래서 `logistics_port()` 를 통해서는 이 분기에
    닿지 못하고, 함수를 직접 부른다.

    지워도 되는 코드처럼 보이지만 남긴다 — 봉투가 mode 를 하나 더 여는 날
    **구현 전까지 이 분기가 받는다.** 그때 조용히 빈 답을 내는 대신
    `missing_capability` 로 이름을 남기는 것이 이 함수의 일이다.
    """
    request = req(mode="SCENARIO_VALIDATION")  # 봉투가 허용하는 아무 mode
    reply, _ = adapter._not_implemented(request)
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_capability == ("SCENARIO_VALIDATION 번역",)
    # RUNTIME_NOT_READY 는 이름이 비면 ContractViolation 이다 (M-1 §5.1)
    assert reply.missing_data


# ---------------------------------------------------------------------------
# STATUS_QUERY — 조회는 경계가 아니라 상태를 답한다
# ---------------------------------------------------------------------------


def test_조회는_봉투_검증을_통과한다(wired):
    request = req(mode="STATUS_QUERY")
    reply, meta = adapter.logistics_port(request)
    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert validate_reply(request, reply, meta) == ()


def test_봉투의_inbound_lead_days_는_int_다(wired):
    """🔴 일수를 kg 변환기에 태우지 않는다 (#221 · 매입 지적 2026-09-03).

    정책값 여섯을 한 루프로 묶어 `_num()` = `float()` 을 태우고 있었는데, 다섯은
    kg(`Decimal`)이고 **이것 하나가 일수(`int`)** 였다. 그래서 `2` 가 `2.0` 으로
    나갔다 — 물류 내부(`schemas.py`)도 IO Contract §3 도 `int` 인데 봉투만 달랐다.

    받는 쪽 셋이 전부 방어를 만들어 뒀다(`critic_bridge.py` `_int_of` ·
    `commitment.py` · `purchase_agent/adapter.py` 의 `lead != int(lead)`).
    생산자가 맞게 보내면 그 방어들이 무해해진다.

    ⚠️ `2.0 == 2` 가 참이라 값 비교로는 안 잡힌다. **타입을 직접 잰다.**
    """
    reply, _ = adapter.logistics_port(req())

    lead = reply.payload["inbound_lead_days"]
    assert isinstance(lead, int)
    assert not isinstance(lead, bool)  # True 가 1일로 통과하는 자리를 막는다
    assert lead == 2

    # kg 축은 그대로 float 다 — 루프에서 하나만 갈라 낸 것이지 전부 바꾼 것이 아니다.
    assert isinstance(reply.payload["guaranteed_capacity_kg"], float)
    assert isinstance(reply.payload["daily_inbound_capacity_kg"], float)


def test_조회는_상태를_싣고_경계는_안_싣는다(wired):
    """★ `PRE_PURCHASE` 와 읽는 것은 같고 **싣는 것이 다르다.**

    `cap_by_date` 는 매입이 분할 계획을 짤 때 쓰는 경계다. "지금 창고 어떠냐" 에
    D+18 Band 를 실으면 사람이 읽을 것이 아닌 표가 답을 덮는다.
    """
    reply, _ = adapter.logistics_port(req(mode="STATUS_QUERY"))
    assert reply.payload["used_capacity_kg"] == 1000.0
    assert reply.payload["warehouse_free_kg"] == 7000.0
    assert reply.payload["guaranteed_capacity_kg"] == 8000.0
    assert reply.payload["lot_count"] == 2
    for boundary in ("cap_by_date", "inbound_lead_days", "daily_inbound_capacity_kg", "lots"):
        assert boundary not in reply.payload


def test_조회의_창고여유_근거는_현재_점유만_설명한다(wired):
    """★ 조회에는 `cap_by_date` 가 없다 — 그 계산을 근거로 끌어오면 안 된다.

    `warehouse_free_kg` 는 as_of 의 `used_capacity_kg` 기준이고
    `calculate_cap_by_date()` 는 도착일별 예상 점유 기준이라 **다른 계산**이다.
    """
    reply, _ = adapter.logistics_port(req(mode="STATUS_QUERY"))
    detail = next(e for e in reply.evidences if e.claim == "warehouse_free_kg").evidence_detail
    assert "cap_by_date" not in detail
    assert "guaranteed_capacity_kg" in detail


def test_조회는_가장_짧은_신선도만_밝힌다(wired):
    """★ 임계를 **지어내지 않는다** — "며칠 이하가 임박인가" 는 물류 정책이다.

    여기서 3일·5일 같은 수를 고르면 §1.2-8(하드 제약값 파생 금지)이 된다.
    최솟값과 그 Lot 만 밝히고 위험 여부는 사람이 본다.
    """
    reply, _ = adapter.logistics_port(req(mode="STATUS_QUERY"))
    # LOT-A 10일 · LOT-B 는 None — None 을 0 으로 읽으면 최솟값이 뒤집힌다
    assert reply.payload["min_remaining_freshness_days"] == 10
    assert reply.payload["min_freshness_lot_id"] == "LOT-A"


def test_신선도가_하나도_없으면_이름을_남긴다(wired, monkeypatch):
    """Lot 은 있는데 신선도가 안 실린 것은 **"0 일 남았다" 가 아니다** (§1.2-10)."""
    monkeypatch.setattr(
        adapter, "build_lot_constraints", lambda snapshot: [_Lot("LOT-C", "100", None)]
    )
    reply, _ = adapter.logistics_port(req(mode="STATUS_QUERY"))
    assert "min_remaining_freshness_days" not in reply.payload
    assert "lots[].remaining_freshness_days" in reply.missing_data
    assert reply.runtime_status == "READY"  # 못 채운 값이 조회 자체를 막지 않는다


def test_조회도_as_of_가_어긋나면_안_답한다(wired):
    """다른 날의 재고는 그날의 사실이 아니다 (§1.2-6) — 조회라고 느슨하지 않다."""
    reply, _ = adapter.logistics_port(req(mode="STATUS_QUERY", as_of=date(2026, 1, 1)))
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("logistics_snapshot@2026-01-01",)


def test_물류가_NOT_READY_면_반드시_이름이_남는다(wired, monkeypatch):
    """🔴 `rules` 는 물류가 정하고 `missing` 은 어댑터가 따로 모은다 — 어긋날 수 있다.

    물류 Rule 이 막았는데 어댑터가 읽은 값이 다 멀쩡하면 `missing_data` 가 빈 채로
    `RUNTIME_NOT_READY` 가 나가고, **봉투가 ContractViolation 을 던진다**(M-1 §5.1).

    지금은 `rental_cap_kg@policy_source_ref` 가 늘 들어 있어 우연히 안 비어 있다.
    **DB 에 그 키가 등록되는 날 터진다.** 그때를 미리 재현한다.
    """
    monkeypatch.setattr(
        adapter,
        "_load_read",
        lambda *, as_of, sim_run_id: _read(
            policy=_policy().model_copy(
                update={"source_refs": {**_policy().source_refs, "rental_cap_kg": "MVP:RENTAL"}}
            )
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
    # 🔴 이 테스트의 전제 — DB 에 키가 등록되면 그 이름이 **사라진다.** 우연히 남아
    #    있는 이름 덕에 통과하는 것이 아님을 함께 고정한다.
    assert "rental_cap_kg@policy_source_ref" not in reply.missing_data


class _Check:
    """`ConstraintResult` 의 어댑터가 읽는 두 필드만 흉내 낸다."""

    def __init__(self, code: str, status: str):
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# lots[].grade — 물류 #77 로 열린 축
# ---------------------------------------------------------------------------


def test_lots_에_grade_를_실어_나른다(wired, monkeypatch):
    """매입 등급 배분이 이 값을 본다 — 없으면 필터가 **에러 없이 전부 미스**다.

    8/28 `lots` 필드 매핑 회신에서 짚은 것으로, 물류가 `LotConstraint.grade` 를
    나르게 되면서(#77) 마스터도 payload 로 옮긴다.
    """
    monkeypatch.setattr(
        adapter, "build_lot_constraints", lambda snapshot: [_Lot("LOT-G", "100", 5, grade="특")]
    )
    reply, _ = adapter.logistics_port(req())
    assert reply.payload["lots"][0]["grade"] == "특"


def test_grade_가_없으면_None_으로_드러낸다(wired):
    """🔴 임의 등급으로 채우지 않는다.

    `_RAW_GRADE_NORMALIZATION` 이 비어 있어 raw `'상품'` 은 `None` 으로 온다.
    **키를 빼면** *"물류가 안 준 것"* 과 *"근거가 없어 못 정한 것"* 이 구분되지
    않는다 (§1.2-10) — 키는 두고 값을 `None` 으로 드러낸다.
    """
    reply, _ = adapter.logistics_port(req())
    for lot in reply.payload["lots"]:
        assert "grade" in lot
        assert lot["grade"] is None


# ---------------------------------------------------------------------------
# 품목별 가용재고 — #111 A1
# ---------------------------------------------------------------------------


def _stocked_snapshot(**overrides) -> InventoryLogisticsSnapshot:
    """가용재고 집계가 실제로 도는 스냅샷.

    배추 300(가용) + 배추 100(신선도 만료 — 제외) + 무 50, 확정 출고 배추 120 차감.
    기대: 무 50 · 배추 180.
    """
    lots = [
        InventoryLotSnapshot(
            lot_id="LOT-B1",
            item="배추",
            available_qty_kg=Decimal(300),
            remaining_freshness_days=5,
            effective_freshness_limit_days=10,
            status="ACTIVE",
        ),
        InventoryLotSnapshot(
            lot_id="LOT-B2",
            item="배추",
            available_qty_kg=Decimal(100),
            remaining_freshness_days=0,
            effective_freshness_limit_days=10,
            status="ACTIVE",
        ),
        InventoryLotSnapshot(
            lot_id="LOT-M1",
            item="무",
            available_qty_kg=Decimal(50),
            remaining_freshness_days=7,
            effective_freshness_limit_days=14,
            status="ACTIVE",
        ),
    ]
    outbound = [ScheduledQuantity(date=AS_OF, quantity_kg=Decimal(120), item="배추")]
    merged: dict = {"on_hand_by_lot": lots, "confirmed_outbound_schedule": outbound, **overrides}
    return _snapshot(**merged)


@pytest.fixture
def stocked(wired, monkeypatch):
    """`_stocked_snapshot` 기반 배선.

    ★ `build_lot_constraints` 를 실물로 되돌린다 — `wired` 의 `_LOTS` 패치를 그대로
      두면 payload 의 `lots`(배추 500.5)와 `inventory_by_item`(배추 180)이 **서로 다른
      재고**에서 나와, 두 필드의 정합을 보려는 후속 테스트가 헛돈다 (검증 발견 7).
    """
    monkeypatch.setattr(
        adapter, "_load_read", lambda *, as_of, sim_run_id: _read(_stocked_snapshot())
    )
    monkeypatch.setattr(adapter, "build_lot_constraints", real_build_lot_constraints)


def test_품목별_가용재고를_PRE_payload_에_싣는다(stocked):
    """Lot 목록과 별개의 **집계값**이다 — 매입/마스터가 Lot 을 재합산하면 가용재고
    정의(비-ACTIVE·만료 제외, 확정 출고 차감)를 남의 도메인에서 재구현하게 된다."""
    reply, _ = adapter.logistics_port(req())
    assert reply.payload["inventory_by_item"] == [
        {"item": "무", "available_qty_kg": 50.0},
        {"item": "배추", "available_qty_kg": 180.0},
    ]


def test_가용재고_근거는_번호가_아니라_품목명으로_가리킨다(stocked):
    """Lot·보관정책과 같은 이름 선택자다 — 번호로 쓰면 품목 순서가 바뀌는 날
    근거가 다른 품목을 가리킨다."""
    reply, _ = adapter.logistics_port(req())
    claims = {evidence.claim: evidence.value for evidence in reply.evidences}
    assert claims["inventory_by_item[배추].available_qty_kg"] == 180.0
    assert claims["inventory_by_item[무].available_qty_kg"] == 50.0


def test_가용재고_근거는_Lot_출처를_가리킨다(stocked):
    """🔴 이 kg 은 Lot 행 합산이다 — `_ref()`(첫 참조 = runtime fixture)를 쓰면
    *"이 수량이 어디서 왔나"* 를 따라갈 때 엉뚱한 곳에 닿는다 (`_lots_ref` docstring,
    검증 발견 3). 확정 출고 출처는 보조 ref 로 함께 싣는다."""
    reply, _ = adapter.logistics_port(req())
    inventory_evidences = [
        evidence for evidence in reply.evidences if evidence.claim.startswith("inventory_by_item[")
    ]
    assert inventory_evidences
    for evidence in inventory_evidences:
        assert "inventory_lots" in evidence.ref_ids[0], evidence.ref_ids


def test_출고_귀속_불명이면_가용재고를_지어내지_않는다(stocked, monkeypatch):
    """🔴 확정 출고에 item 없는 행이 있으면 어느 품목의 재고가 줄었는지 모른다.

    임의 배분 대신 키를 생략하고 이름을 남긴다 — `[]`(품목 0건 확인)로 위장하면
    *"재고가 없다"* 로 읽힌다 (§1.2-10).
    """
    unattributed = [ScheduledQuantity(date=AS_OF, quantity_kg=Decimal(120), item=None)]
    monkeypatch.setattr(
        adapter,
        "_load_read",
        lambda *, as_of, sim_run_id: _read(
            _stocked_snapshot(confirmed_outbound_schedule=unattributed)
        ),
    )
    reply, _ = adapter.logistics_port(req())
    assert "inventory_by_item" not in reply.payload
    assert "inventory_by_item" in reply.missing_data


def test_가용재고를_실어도_봉투_검증을_통과한다(stocked):
    """배열 항목 안의 숫자마다 근거가 있어야 한다 (`required_claims`) — 커버리지 검증."""
    request = req()
    reply, meta = adapter.logistics_port(request)
    assert reply.payload["inventory_by_item"]
    assert validate_reply(request, reply, meta) == ()


# ---------------------------------------------------------------------------
# 시나리오 상세·업무 위험 signal·우선 조정 축 — #111 A2·A3·A4
# ---------------------------------------------------------------------------


def test_시나리오별_판정_상세를_봉투에_싣는다(wired):
    """총평(verdict)만으로는 *"어떤 시나리오가 왜 conditional 인지"* 를 마스터가
    받지 못한다 — 독립 응답과 같은 상세를 나른다."""
    reply, _ = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_proposal_payload()))
    results = reply.payload["scenario_results"]
    assert len(results) == 1
    assert results[0]["label"] == "기본"
    assert results[0]["verdict"] in {"ok", "conditional", "reject", "skipped"}
    assert isinstance(results[0]["reason_codes"], list)
    assert isinstance(results[0]["adjustments"], list)


def test_업무_위험_signal_이_soft_warnings_로_합류한다(wired, monkeypatch):
    """CAPACITY_TIGHT 계열은 판정을 바꾸지 않지만 Critic 과 사람이 봐야 한다.

    독립 경로와 같은 병합(`merge_business_warnings`)이다 — 잔여 신선도 비율
    2/10 = 0.2 ≤ 임계 0.30 이면 `INVENTORY_FRESHNESS_PRESSURE` 가 나간다.
    """
    pressured = _stocked_snapshot(freshness_pressure_ratio=Decimal("0.30"))
    pressured = pressured.model_copy(
        update={
            "on_hand_by_lot": [
                InventoryLotSnapshot(
                    lot_id="LOT-P1",
                    item="배추",
                    available_qty_kg=Decimal(100),
                    remaining_freshness_days=2,
                    effective_freshness_limit_days=10,
                    status="ACTIVE",
                )
            ]
        }
    )
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read(pressured))
    reply, _ = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_proposal_payload()))
    assert "INVENTORY_FRESHNESS_PRESSURE" in reply.payload["soft_warnings"]


def test_우선_조정_축은_있을_때만_실린다(wired, monkeypatch):
    """축 값의 정확성은 `derive_preferred_adjustment` 테스트가 본다 — 여기는 **번역**만.

    `None`(혼재·0건)이면 키를 싣지 않는다 — 근거 없이 하나를 고르지 않는다.
    """
    monkeypatch.setattr(adapter, "derive_preferred_adjustment", lambda results: "quantity")
    reply, _ = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_proposal_payload()))
    assert reply.payload["preferred_adjustment"] == "quantity"

    monkeypatch.setattr(adapter, "derive_preferred_adjustment", lambda results: None)
    reply, _ = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_proposal_payload()))
    assert "preferred_adjustment" not in reply.payload


def test_시나리오_상세를_실어도_봉투_검증을_통과한다(wired, monkeypatch):
    """signal·상세·근거가 다 실린 상태로 봉투 규칙 전체를 통과해야 한다."""
    pressured = _stocked_snapshot(freshness_pressure_ratio=Decimal("0.30"))
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read(pressured))
    request = req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    reply, meta = adapter.logistics_port(request)
    assert reply.payload["scenario_results"]
    assert validate_reply(request, reply, meta) == ()


def test_기준일이_다른_제안은_판정하지_않는다(wired):
    """🔴 재무 어댑터와 같은 fail-closed (§1.2-6).

    스냅샷·Rule 은 요청 `as_of` 로 읽는데 시나리오만 다른 날짜로 계산하면 기준일이
    섞인 판정이 READY 로 나간다 — Codex 교차검증에서 실제 재현된 케이스다.
    """
    payload = _proposal_payload()
    payload["meta"]["as_of"] = "2026-01-01"
    payload["scenarios"][0]["split_plan"][0]["date"] = "2026-01-01"
    reply, _ = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=payload))
    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.payload["validation_errors"] == ["proposal.meta.as_of"]


def test_조정_제안은_전용_채널에도_실린다(stocked):
    """🔴 payload 안에만 두면 마스터 flow 가 세는 `reply.suggested_adjustments` 는
    0건이고, 사람 화면("물류가 조정을 제안했습니다 N건")과 Critic 축 침범 검사가
    전부 빈 튜플을 본다 (검증 발견 1).

    창고 여유(무 50 + 배추 180 시나리오와 무관하게 cap 은 guaranteed−점유)로는
    20,000kg 제안을 못 받으므로 조정 제안이 나온다.
    """
    payload = _proposal_payload()
    scenario = payload["scenarios"][0]
    scenario["total_qty_kg"] = 20000
    scenario["total_amount_krw"] = 33000000
    scenario["split_plan"] = [{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 20000}]
    scenario["sourcing_plan"] = [
        {"market": "가락", "grade": "상", "qty_kg": 20000, "grade_unit_price": 1650}
    ]
    request = req(mode="SCENARIO_VALIDATION", payload=payload)
    reply, meta = adapter.logistics_port(request)

    assert reply.suggested_adjustments, "payload 에는 있는 조정이 전용 채널에 없다"
    for adjustment in reply.suggested_adjustments:
        assert adjustment.dept == "inventory"
        assert adjustment.axis in {"quantity", "timing"}
        assert adjustment.ref_ids
    assert reply.needs_followup is True
    assert validate_reply(request, reply, meta) == ()


def test_같은_조정이_여러_안에서_나오면_라벨이_합쳐진다(stocked):
    """🔴 전에는 중복 키를 만나면 그 자리에서 `continue` 해 **두 번째 안의 라벨이
    사라졌다** (#209 · 되먹임 ④).

    같은 회차·같은 목표값이면 조정안은 하나로 합치는 것이 맞다. 다만 그 하나가
    **어느 안들에서 나왔는지**는 잃으면 안 된다 — 마스터 화면(`answer.py:295`)이
    `scenario_labels` 를 읽어 "보수·기본안" 을 조립한다.

    같은 split_plan 을 가진 안 둘을 넣으면 조정도 같은 key 로 나온다.
    """
    payload = _proposal_payload()
    scenario = payload["scenarios"][0]
    scenario["total_qty_kg"] = 20000
    scenario["total_amount_krw"] = 33000000
    scenario["split_plan"] = [{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 20000}]
    scenario["sourcing_plan"] = [
        {"market": "가락", "grade": "상", "qty_kg": 20000, "grade_unit_price": 1650}
    ]
    payload["scenarios"] = [scenario, {**scenario, "label": "공격"}]

    request = req(mode="SCENARIO_VALIDATION", payload=payload)
    reply, meta = adapter.logistics_port(request)

    # 중복 제거의 뜻은 그대로 — 같은 key 는 하나다.
    assert len(reply.suggested_adjustments) == 1
    suggested = reply.suggested_adjustments[0]

    # 🔴 라벨은 둘 다 남고 시나리오 등장 순서를 지킨다.
    assert suggested.scenario_labels == ("기본", "공격")
    # 대상 회차도 칸으로 간다 — reason 문자열을 파싱하지 않아도 된다.
    assert suggested.split_date == AS_OF
    # 문장에는 라벨·회차가 없다 (미결 §0-6 갈래 ㄱ).
    assert "기본" not in suggested.reason
    assert "회차" not in suggested.reason

    assert validate_reply(request, reply, meta) == ()


def test_판정_스킵_사실은_soft_warnings_에만_남는다(wired):
    """🔴 업무 경고를 M-1 `missing_data` 로 옮기지 않는다.

    독립 응답의 `missing_data` 는 무숫자 번역 채널이라 판정 스킵 사실이 들어가지만,
    M-1 의 `missing_data` 는 **마스터가 사용자에게 무엇을 달라고 할지**의 이름이다.
    형식도 `logistics_rule/LOG-H02` · `rental_cap_kg@policy_source_ref` 처럼 네임스페이스
    붙은 필드명이라 맨 경고 코드를 섞으면 어휘가 갈라진다. NOT_READY 로 떨어지는 날에는
    *"CAPACITY_TIGHT_POLICY_UNRESOLVED 가 없어 답하지 못했습니다"* 라는 이중부정 문장이
    나간다 (`master/answer.py`).

    사실은 사라지지 않는다 — 같은 코드가 `soft_warnings` 로 나간다. 기본 픽스처는
    임계 정책이 등록돼 있지 않아 판정 스킵 2건이 실제로 발생하는 상태다.
    """
    reply, _ = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_proposal_payload()))
    assert "CAPACITY_TIGHT_POLICY_UNRESOLVED" in reply.payload["soft_warnings"]
    assert "FRESHNESS_PRESSURE_POLICY_UNRESOLVED" in reply.payload["soft_warnings"]
    assert "CAPACITY_TIGHT_POLICY_UNRESOLVED" not in reply.missing_data
    assert "FRESHNESS_PRESSURE_POLICY_UNRESOLVED" not in reply.missing_data


def test_우선_조정_축은_판정으로_선언되고_근거가_붙는다(wired, monkeypatch):
    """`quantity` 는 소문자라 봉투의 대문자 라벨 휴리스틱을 지나친다 — 직접 선언하지
    않으면 매입 행동을 바꾸는 판정이 근거 없이 나간다 (검증 발견 2)."""
    monkeypatch.setattr(adapter, "derive_preferred_adjustment", lambda results: "quantity")
    request = req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    reply, meta = adapter.logistics_port(request)
    assert "preferred_adjustment" in reply.judgment_fields
    assert any(evidence.claim == "preferred_adjustment" for evidence in reply.evidences)
    assert validate_reply(request, reply, meta) == ()


def test_시나리오_판정에도_품목별_가용재고를_싣는다(stocked):
    """Scenario 엔진이 이미 계산한 값이다 — 버리면 마스터가 판정 회신에서 재고 맥락을
    잃는다 (검증 발견 5). PRE 와 같은 근거(이름 선택자·Lot 출처)가 붙는다."""
    request = req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    reply, meta = adapter.logistics_port(request)
    assert reply.payload["inventory_by_item"] == [
        {"item": "무", "available_qty_kg": 50.0},
        {"item": "배추", "available_qty_kg": 180.0},
    ]
    assert validate_reply(request, reply, meta) == ()


# ---------------------------------------------------------------------------
# Critic DeptMeta (#134)
#
# ★ 이 관측은 **틀리게 적어도 에러가 안 난다.** Critic 은
#   `inputs_used.get(check_id, ())` 로 읽고 못 찾으면 빈 튜플이며, 빈 튜플은
#   "금지 입력이 없다" 로 읽혀 통과다. 그래서 여기서 보는 것은 값의 정확성이 아니라
#   **조용한 통과가 성립하지 않는가**다.
# ---------------------------------------------------------------------------


def _dept_meta(meta):
    """ExecutionMetadata 의 observations 에서 물류 DeptMeta 하나를 꺼낸다."""
    found = [
        json.loads(item)
        for item in meta.observations
        if json.loads(item).get("observation_type") == "inventory_dept_meta"
    ]
    return found[0] if found else None


def test_PRE_회신에_DeptMeta_관측이_실린다(wired):
    """안 실으면 Critic 의 두 검사가 통과가 아니라 **생략**된다."""
    request = req()
    reply, meta = adapter.logistics_port(request)
    assert reply.runtime_status == "READY"
    dept_meta = _dept_meta(meta)
    assert dept_meta is not None
    assert validate_reply(request, reply, meta) == ()


def test_inputs_used_키는_마스터가_합성하는_check_id_다(wired):
    """🔴 한 글자만 달라도 Critic 이 빈 튜플을 받고 **조용히 통과**한다.

    이름의 주인은 마스터(`critic_bridge.DEPT_CAP_CHECK_ID`)이고, 물류는 거기에 맞출
    뿐이다. 물류는 문자열을 베끼지 않고 그 상수를 참조한다 (#137).
    """
    from app.master.critic_bridge import DEPT_CAP_CHECK_ID

    _, meta = adapter.logistics_port(req())
    assert list(_dept_meta(meta)["inputs_used"]) == [DEPT_CAP_CHECK_ID["inventory"]]


def test_마스터가_실제로_합성한_check_와_키가_맞는다(wired):
    """상수 비교보다 한 걸음 더 — **마스터가 이 회신 payload 로 만든 check** 의
    `check_id` 가 우리 `inputs_used` 의 키여야 한다.

    Critic 은 `inputs_used.get(chk.check_id, ())` 로 대조하므로, 여기서 어긋나면
    검사가 돌면서 빈 튜플을 보고 **조용히 통과**한다 (`critic_v0_4.py:643`).
    """
    from app.master.critic_bridge import _replies_in

    reply, meta = adapter.logistics_port(req())
    synthesized = _replies_in({"inventory": reply.payload}, {"inventory": reply.evidences})
    checks = [chk for dept in synthesized for chk in dept["checks"]]
    assert checks, "마스터가 물류 check 를 합성하지 못했다 — payload 전제가 바뀐 것이다"
    declared = _dept_meta(meta)["inputs_used"]
    for chk in checks:
        assert chk["check_id"] in declared


def test_check_id_문자열을_물류가_들고_있지_않다():
    """🔴 **이 이슈(#137)의 실질 수용 기준 — 이름이 사는 곳은 한 곳이다.**

    값이 같은지를 보는 것으로는 부족하다. 베낀 문자열도 값은 같기 때문이다.
    그래서 **물류 소스에 그 리터럴이 없다**는 것을 본다.

    이 테스트가 잡는 회귀는 이렇다 — 누가 순환 import 를 피하려고, 또는 마스터
    의존을 줄이려고 문자열을 다시 박아 넣는 경우다. 그 순간 마스터가 이름을 바꾸면
    물류 검사만 조용히 죽고, 마스터 대조 테스트는 초록불이다.
    """
    import inspect

    from app.master.critic_bridge import DEPT_CAP_CHECK_ID

    source = inspect.getsource(adapter)
    literal = DEPT_CAP_CHECK_ID["inventory"]
    assert f'"{literal}"' not in source, (
        f"물류 소스에 {literal!r} 리터럴이 있다 — 마스터 상수를 참조해야 한다"
    )
    assert adapter._CAP_CHECK_ID == literal


def test_선언한_입력에_매입_시나리오_이름이_없다(wired):
    """`E-SCENARIO-LEAK` 의 자기 검증 — 밴드는 후보와 무관해야 한다 (§3.6.1).

    PRE_PURCHASE 는 제안이 생기기 **전에** 도는 경로라 구조적으로 성립하지만
    (`master/flow.py::_collect_constraints` 가 PRE 회신만 모은다), 그 사실이 관측에도
    유지되는지는 따로 봐야 한다.
    """
    from app.critic.critic_v0_4 import FORBIDDEN_SCENARIO_INPUTS

    _, meta = adapter.logistics_port(req())
    declared = set(_dept_meta(meta)["inputs_used"][adapter._CAP_CHECK_ID])
    assert FORBIDDEN_SCENARIO_INPUTS & declared == set()


def test_안_돈_Tool_의_입력은_실리지_않는다():
    """선언이 아니라 **관측**이다. 정적 목록을 그대로 내면 실행과 갈리고, 갈린 목록을
    Critic 은 사실로 검사한다.

    같은 mode 를 Tool 목록만 바꿔 부르면 선언이 따라 바뀌어야 한다.
    """
    only_lots = adapter._inventory_dept_meta("PRE_PURCHASE", {}, [adapter._T_LOTS])
    with_rules = adapter._inventory_dept_meta(
        "PRE_PURCHASE", {}, [adapter._T_LOTS, adapter._T_RULES]
    )
    band = set(adapter._ADAPTER_BAND_INPUTS)
    assert set(only_lots["inputs_used"][adapter._CAP_CHECK_ID]) == band | set(
        adapter._TOOL_INPUTS[adapter._T_LOTS]
    )
    # Rule **만** 읽는 입력(어댑터 밴드·Lots 와 겹치지 않는 것)은 Rule 이 돌기
    # 전에는 실리지 않는다. `on_hand_by_lot` 처럼 공유되는 입력은 돈 Tool 이 데려오므로
    # 배타 입력으로 봐야 실행 의존이 실제로 검증된다.
    rule_only = (
        set(adapter._TOOL_INPUTS[adapter._T_RULES])
        - band
        - set(adapter._TOOL_INPUTS[adapter._T_LOTS])
    )
    assert rule_only
    assert rule_only.isdisjoint(only_lots["inputs_used"][adapter._CAP_CHECK_ID])
    assert rule_only <= set(with_rules["inputs_used"][adapter._CAP_CHECK_ID])


def test_produced_fields_는_실제로_실린_필드다(wired):
    """산출하지 않은 필드를 산출했다고 적으면 권한 검사가 엉뚱한 것을 본다."""
    reply, meta = adapter.logistics_port(req())
    assert _dept_meta(meta)["produced_fields"] == sorted(
        key for key, value in reply.payload.items() if value is not None
    )


def test_시나리오_판정은_inputs_used_를_비우고_산출만_낸다(stocked):
    """그 mode 에는 대응하는 cap 검사 축이 없다 — 마스터가 밴드 check 를 합성하는
    입력(`constraints`)은 PRE 회신만 모은다. 없는 검사에 가짜 입력을 지어내지 않고,
    `E-AUTHORITY` 가 볼 수 있게 실제 산출 필드만 낸다."""
    reply, meta = adapter.logistics_port(
        req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    )
    dept_meta = _dept_meta(meta)
    assert dept_meta["inputs_used"] == {}
    assert dept_meta["produced_fields"] == sorted(
        key for key, value in reply.payload.items() if value is not None
    )


def test_빈_inputs_used_가_경계_관측을_덮지_않는다(wired, stocked):
    """마스터가 두 mode 의 관측을 **합쳐서** 나른다 (`critic_bridge._dept_meta_in`).
    시나리오 관측의 빈 `inputs_used` 가 마지막이라 경계 것을 덮으면, 검사가 돌면서
    아무것도 안 보게 된다."""
    from app.master.critic_bridge import _dept_meta_in

    _, pre_meta = adapter.logistics_port(req())
    _, sv_meta = adapter.logistics_port(
        req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    )
    merged = _dept_meta_in({"inventory": [*pre_meta.observations, *sv_meta.observations]})
    assert merged["inventory"]["inputs_used"][adapter._CAP_CHECK_ID]


def test_못_낸_회신에는_관측을_달지_않는다(monkeypatch):
    """ "안 돌았는데 무엇을 읽었다" 가 되면 안 된다."""
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: None)
    reply, meta = adapter.logistics_port(req())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert _dept_meta(meta) is None


def test_스냅샷_실행오류_회신에도_관측을_달지_않는다(monkeypatch):
    def _boom(*, as_of, sim_run_id):
        raise adapter._SnapshotLoadError("db down")

    monkeypatch.setattr(adapter, "_load_read", _boom)
    reply, meta = adapter.logistics_port(req())
    assert reply.runtime_status == "ERROR"
    assert _dept_meta(meta) is None


def test_모든_Tool_이_입력_계약을_가진다():
    """Tool 을 새로 만들고 계약을 안 적으면 조용한 누락이 아니라 **import 실패**여야
    한다. 빈 `inputs_used` 는 Critic 이 통과로 읽으므로 크게 실패하는 편이 낫다."""
    declared = set(adapter._TOOL_INPUTS)
    assert {
        adapter._T_RULES,
        adapter._T_CAP,
        adapter._T_ARRIVAL,
        adapter._T_LOTS,
        adapter._T_INVENTORY,
        adapter._T_SIGNALS,
    } <= declared


def test_계약_없는_Tool_은_조용히_0개가_아니라_예외다():
    with pytest.raises(adapter._ToolInputContractMissing):
        adapter._inventory_dept_meta("PRE_PURCHASE", {}, ["nonexistent_tool"])


def test_시나리오_Tool_은_금지_이름을_정직하게_선언한다():
    """지금은 SCENARIO_VALIDATION 에서만 돌아 `inputs_used` 에 실리지 않는다. 언젠가
    경계 경로로 새면 Critic 이 잡아야 하므로 계약을 비워 두지 않는다."""
    from app.critic.critic_v0_4 import FORBIDDEN_SCENARIO_INPUTS

    assert FORBIDDEN_SCENARIO_INPUTS & set(adapter._TOOL_INPUTS[adapter._T_ARRIVAL])


# ---------------------------------------------------------------------------
# 실행 축 — 마스터가 준 sim_run_id 가 그대로 Repository 까지 간다 (#345)
# ---------------------------------------------------------------------------
#
# ★ **`_load_read` 를 갈아 끼우지 않는다.** 이번에 고친 자리가 바로 그 함수라, 그것을
#   가짜로 덮으면 전달 여부를 볼 수 없다. 한 단계 아래(`get_current_logistics_read`)를
#   잡아 **봉투에서 나온 값이 Repository 인자로 도착하는지**를 본다.
#
# ★ 실행 둘을 실제로 세우지 않는다. 어댑터는 DB 를 안 타고, *"남의 실행을 안 읽는다"*
#   의 검사는 이미 Repository 쪽에 있다
#   (`test_logistics_service_repository.py::test_runtime_fixture_reads_only_the_requested_run`).
#   여기서 볼 것은 **축을 나르는가** 하나다.

#: 실행 축을 실제로 읽는 네 mode — payload 가 있어야 하는 쪽은 만들어 준다.
#:
#: ★ `PRE_SALES` 는 #346 이 번역을 구현하면서 들어왔다. **구현이 먼저고 문이 나중이다** —
#:   그 순서는 아래 `test_실행_축_문은_구현된_mode_에만_선다` 가 구조로 잠근다.
_AXIS_MODES = [
    pytest.param("PRE_PURCHASE", dict, id="PRE_PURCHASE"),
    pytest.param("PRE_SALES", dict, id="PRE_SALES"),
    pytest.param("STATUS_QUERY", dict, id="STATUS_QUERY"),
    pytest.param("SCENARIO_VALIDATION", _proposal_payload, id="SCENARIO_VALIDATION"),
]


def _recorder(calls: list[dict]):
    def _fn(*, as_of, sim_run_id):
        calls.append({"as_of": as_of, "sim_run_id": sim_run_id})
        return _read()

    return _fn


@pytest.mark.parametrize(("mode", "payload_factory"), _AXIS_MODES)
@pytest.mark.parametrize("실행", ["SIM-A", "SIM-B"])
def test_봉투가_준_실행_축을_그대로_조회에_넘긴다(monkeypatch, mode, payload_factory, 실행):
    """🔴 **물류는 이 값을 지어내지 않는다** — 봉투가 준 것을 그대로 쓴다 (#345).

    두 값을 다 도는 이유는 *"어쩌다 맞는"* 을 막기 위해서다. 하나만 재면
    `BURN_IN_SIM_RUN_ID` 를 박아 넣은 뮤턴트가 살아남을 수 있다.
    """
    calls: list[dict] = []
    monkeypatch.setattr(adapter, "get_current_logistics_read", _recorder(calls))

    request = req(mode=mode, payload=payload_factory(), sim_run_id=실행)
    adapter.logistics_port(request)

    assert calls == [{"as_of": AS_OF, "sim_run_id": 실행}]


@pytest.mark.parametrize(("mode", "payload_factory"), _AXIS_MODES)
def test_실행_축이_비면_조회하지_않고_이름을_남긴다(monkeypatch, mode, payload_factory):
    """🔴 **읽지 않는 것이 답이다.** 축이 없으면 어느 실행의 장부인지 모르고, 모르는 채로
    아무 행이나 고르는 것이 fail-open 이다 (`repository` 가 지키는 규율과 같은 편).

    ★ **`ERROR` 가 아니다.** 다시 불러도 같으므로 재시도 가치가 없다 (M-1 §5.1) —
      `ERROR` 로 내면 마스터가 호출 예산만 태운다.
    """
    # ★ 여기서 예외를 던지지 않는다. 던지면 `_load_read` 의 `except Exception` 이
    #   삼켜 ERROR 로 나오고, 문이 사라진 날 실패 문구가 *"어댑터가 뭉갠다"* 로
    #   읽힌다 — 세고 나서 비었는지 묻는 편이 무엇이 깨졌는지 곧바로 말한다.
    calls: list[dict] = []
    monkeypatch.setattr(adapter, "get_current_logistics_read", _recorder(calls))

    request = req(mode=mode, payload=payload_factory(), sim_run_id="")
    reply, meta = adapter.logistics_port(request)

    assert calls == [], "실행 축이 없는데 Repository 를 불렀다"
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("sim_run_id",)
    # 아무 Tool 도 안 돌았다 — 안 돈 것을 돈 것처럼 적지 않는다
    assert meta.used_tools == ()
    # 예외 원문이 새지 않는다 — reasoning 에 숫자가 한 자리도 없어야 한다
    assert not any(character.isdigit() for character in reply.reasoning)
    assert validate_reply(request, reply, meta) == ()


def test_실행_축_문은_구현된_mode_에만_선다():
    """🔴 **#345 의 규율을 #346 이후에도 지키는 자리다.**

    종전 이 자리에는 *"`PRE_SALES` 는 미구현이므로 빈 축보다 `PRE_SALES_translation`
    을 먼저 낸다"* 가 있었다. #346 이 그 번역을 구현했으므로 **그 문장은 더 이상
    사실이 아니다.** 지우지 않고 뜻을 바꾼 이유는 규율 자체가 그대로이기 때문이다.

    ```text
    지키는 것   축이 비었다고 "번역이 없다" 를 "값이 안 왔다" 로 바꾸지 않는다
    바뀐 것     PRE_SALES 가 이제 진짜로 runtime 을 읽는 mode 다
    ```

    ★ **행동으로 재던 것을 구조로 잰다.** 종전 형태(미구현 mode 를 실제로 불러 본다)는
      이제 만들 수 없다 — 물류가 받는 네 mode 가 전부 구현됐고, 그 밖의 이름은
      `AgentRequest.__post_init__` 이 `ContractViolation` 으로 막아 **요청 자체를 만들 수
      없다** (`_AGENT_MODES`). 봉투 내부를 뒤집어 가짜 mode 를 밀어 넣으면 그때부터
      이 검사는 남의 계약을 시험하는 것이 된다.

    ★ 그래서 **불변식**을 잰다: 문 뒤에 선 mode 는 전부 실제 handler 가 있다.
      누군가 구현보다 문을 먼저 세우는 날 여기가 빨간불이다.
    """
    구현된_mode = {"PRE_PURCHASE", "PRE_SALES", "SCENARIO_VALIDATION", "STATUS_QUERY"}

    # 문 뒤에 미구현 mode 가 서 있으면 그 mode 의 "번역이 없다" 가 축 탓으로 바뀐다
    assert adapter._RUNTIME_AXIS_MODES <= 구현된_mode

    # 그리고 그 목록이 낡지 않았는지 — 진짜로 handler 를 타는지 실행으로 확인한다
    for mode in sorted(구현된_mode):
        reply, _ = adapter.logistics_port(req(mode=mode, sim_run_id=""))
        assert reply.missing_data == ("sim_run_id",), mode
        assert reply.missing_capability == (), mode


def test_미구현_mode_는_여전히_번역이_없다고_답한다():
    """★ `_not_implemented` 의 의미는 살아 있다 — **지금 물류에 미구현 mode 가 없을 뿐이다.**

    이 경로가 죽은 코드가 아님을 남긴다. 봉투가 물류에 새 mode 를 열고 어댑터가 아직
    그것을 번역하지 못하는 날, 답은 *"값이 안 왔다"* 가 아니라 *"번역이 없다"* 여야 한다.

    ★ **`logistics_port` 를 거치지 않고 handler 를 직접 부른다.** 그 앞의
      `AgentRequest` 가 어휘를 막기 때문이고, 여기서 보려는 것은 라우팅이 아니라
      **그 handler 가 무엇을 말하는가**다.
    """
    request = req(mode="STATUS_QUERY")
    reply, meta = adapter._not_implemented(request)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("STATUS_QUERY_translation",)
    assert reply.missing_capability == ("STATUS_QUERY 번역",)
    # 안 돈 것을 돈 것처럼 적지 않는다
    assert meta.used_tools == ()


def test_읽는_함수는_실행_축을_이름으로_받는다():
    """★ 구조로 잠근다 — `test_logistics_day_open.py` 가 Repository 쪽에 건 것과 같은 검사.

    위치인자로 새면 호출자가 `as_of` 와 축을 뒤바꿔 넣어도 조용히 돈다. 그리고
    **선택 인자가 아니어야** *"안 주면 넓어지는"* 자리가 안 생긴다.
    """
    import inspect

    축 = inspect.signature(adapter._load_read).parameters["sim_run_id"]
    assert 축.kind is inspect.Parameter.KEYWORD_ONLY
    assert 축.default is inspect.Parameter.empty, "선택 인자로 두면 fail-open 이 돌아온다"


# ---------------------------------------------------------------------------
# PRE_SALES — 판매 제안 전 컨텍스트 (#346)
# ---------------------------------------------------------------------------
#
# ★ **`build_inventory_by_item` 를 갈아 끼우지 않는다.** 이 mode 가 답하는 confirmed
#   sellable 의 정본이 그 함수라, 가짜로 덮으면 *"차감했는가"* 를 볼 수 없다.
#   진짜 Snapshot 을 세우고 진짜 Tool 을 돌린다.


def _sales_lot(
    lot_id: str,
    item: str,
    qty: str,
    freshness: int | None,
    *,
    limit: int | None = 15,
    status: str = "ACTIVE",
) -> InventoryLotSnapshot:
    return InventoryLotSnapshot(
        lot_id=lot_id,
        item=item,
        available_qty_kg=Decimal(qty),
        remaining_freshness_days=freshness,
        effective_freshness_limit_days=limit,
        status=status,
    )


def _sales_snapshot(**overrides) -> InventoryLogisticsSnapshot:
    """예약·할당과 만료 Lot 이 **실제로 들어 있는** 스냅샷.

    ```text
    LOT-A  배추 1,000kg  신선도 10   할당 400 (LOT-A 지정)
    LOT-B  배추   500kg  신선도 -3   ← 신선도 만료 · 가용에서 빠진다
    LOT-C  무     300kg  신선도  7
                          + 배추 미할당 예약 100 (Lot 미지정)

    배추 가용   = (1,000 − 400) − 100 = 500      무 가용 = 300
    Lot 물리 합계(배추) = 1,500                  ← 500 과 다르다. 이것이 요점이다
    ```
    """
    base: dict = {
        "on_hand_by_lot": [
            _sales_lot("LOT-A", "배추", "1000", 10),
            _sales_lot("LOT-B", "배추", "500", -3),
            _sales_lot("LOT-C", "무", "300", 7),
        ],
        "outbound_commitments": [
            OutboundCommitment(item="배추", lot_id="LOT-A", quantity_kg=Decimal(400)),
            OutboundCommitment(item="배추", lot_id=None, quantity_kg=Decimal(100)),
        ],
        "used_capacity_kg": Decimal(1800),
    }
    return _snapshot(**{**base, **overrides})


def _with_read(monkeypatch, snapshot):
    """`_load_read` 만 갈아 끼운다 — Tool 은 진짜를 돌린다."""
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read(snapshot))


@pytest.fixture
def wired_sales(monkeypatch):
    _with_read(monkeypatch, _sales_snapshot())


def _pre_sales_reply(payload=None):
    request = req(mode="PRE_SALES", payload=payload)
    reply, meta = adapter.logistics_port(request)
    return request, reply, meta


def _all_keys(value) -> set[str]:
    """payload 어디에 있든 키 이름을 전부 모은다 — 중첩 안에 숨는 것을 막는다."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


def _adapter_imports() -> tuple[set[str], set[str]]:
    """어댑터가 **실제로 들여온** (모듈, 이름).

    ★ 소스 문자열로 재지 않는다 — docstring 이 금지 함수 이름을 설명으로 적고 있어
      문자열 검색은 그것까지 잡는다. import 만 보면 *"부를 수 있는가"* 를 정확히 잰다.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(adapter))
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
    return modules, names


# ── A. routing ──────────────────────────────────────────────────


def test_PRE_SALES_는_더_이상_미구현이_아니다(wired_sales):
    """A — `_not_implemented` 로 가지 않는다.

    ★ *"오류가 아니다"* 로 재지 않는다. `_not_implemented` 가 내던 **바로 그 이름**이
      사라졌는지를 본다 — 다른 이유로 NOT_READY 가 나도 통과하는 검사는 검사가 아니다.
    """
    _, reply, _ = _pre_sales_reply()

    assert "PRE_SALES_translation" not in reply.missing_data
    assert reply.missing_capability == ()
    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"


def test_PRE_SALES_는_판정을_내지_않는다(wired_sales):
    """판매 승인·거절은 판매와 재무가 한다 — 물류는 사실만 낸다 (`_status_query` 와 같다)."""
    _, reply, _ = _pre_sales_reply()

    assert reply.judgment_fields == ()
    assert reply.needs_followup is False
    assert reply.suggested_adjustments == ()


# ── D · E. confirmed sellable vs Lot 근거 ───────────────────────


def test_판매가능량은_예약과_할당을_차감한_값이다(wired_sales):
    """D — 🔴 **이 절에서 가장 중요한 검사.**

    이미 잡아 둔 몫을 다시 팔 수 있다고 답하면 매입은 팔 수 있다고 보고 판매는 못 잡는
    상태가 된다. 차감이 빠져도 숫자는 나오고 봉투도 통과한다 — 그 조용한 통과를 막는다.

    ```text
    배추  (1,000 − 할당 400) − 미할당 예약 100 = 500
    무     300 (차감 없음)
    ```
    """
    _, reply, _ = _pre_sales_reply()

    by_item = {row["item"]: row["available_qty_kg"] for row in reply.payload["inventory_by_item"]}
    assert by_item == {"배추": 500.0, "무": 300.0}


def test_Lot_수량을_합산해도_판매가능량이_되지_않는다(wired_sales):
    """E — 두 배열은 **다른 뜻**이다. 같은 수가 나오면 한쪽이 잘못된 것이다.

    `lot_constraints` 는 예약·할당 차감 **전** 물리 잔량이고 만료 Lot 도 들어 있다.
    받는 쪽이 이것을 합산해 판매가능량을 다시 만들면 안 된다는 것을 수로 잠근다.
    """
    payload = _pre_sales_reply()[1].payload

    lot_total = sum(
        row["available_qty_kg"] for row in payload["lot_constraints"] if row["item"] == "배추"
    )
    confirmed = next(
        row["available_qty_kg"] for row in payload["inventory_by_item"] if row["item"] == "배추"
    )
    assert lot_total == 1500.0  # 1,000 + 500(만료분 포함)
    assert confirmed == 500.0
    assert lot_total != confirmed


def test_신선도가_만료된_Lot_은_판매가능량에서_빠지되_근거로는_남는다(wired_sales):
    """만료 Lot 을 **숨기지 않는다.** 가용에서 빠지는 것과 없던 일이 되는 것은 다르다."""
    payload = _pre_sales_reply()[1].payload

    assert "LOT-B" in {row["lot_id"] for row in payload["lot_constraints"]}


# ── F. freshness ────────────────────────────────────────────────


def test_음수_신선도를_그대로_나른다(wired_sales):
    """F — 🔴 **0 으로 접지 않는다.**

    `-3` 은 *"신선도 기준을 지난 지 사흘"* 이라는 사실이다. 0 으로 보정하면 **기준일
    당일**과 **사흘 지난 Lot** 이 같은 값이 되고, 받는 쪽은 그 차이를 영영 못 본다.
    """
    payload = _pre_sales_reply()[1].payload
    by_lot = {row["lot_id"]: row["remaining_freshness_days"] for row in payload["lot_constraints"]}

    assert by_lot["LOT-B"] == -3
    assert by_lot["LOT-A"] == 10  # 다른 Lot 도 손대지 않았다


def test_신선도_분모를_다시_계산하지_않고_그대로_나른다(wired_sales):
    """잔여 신선도를 낸 그 유효 한계를 함께 싣는다 — 받는 쪽이 역산하지 않도록.

    `중` 등급은 유효 한계가 `operational_limit × medium_grade_factor` 라, 품목 정책
    원값으로 역산하면 **갓 입고된 Lot 이 임박으로 보인다.**
    """
    payload = _pre_sales_reply()[1].payload

    lot_a = next(row for row in payload["lot_constraints"] if row["lot_id"] == "LOT-A")
    assert lot_a["effective_freshness_limit_days"] == 15


def test_신선도_한계를_모르면_지어내지_않는다(monkeypatch):
    """`None` 은 `0` 이 아니다 — 모르는 분모를 0 으로 채우면 비율이 무한이 된다."""
    _with_read(
        monkeypatch,
        _sales_snapshot(
            on_hand_by_lot=[_sales_lot("LOT-A", "배추", "100", None, limit=None)],
            outbound_commitments=[],
            used_capacity_kg=Decimal(100),
        ),
    )
    payload = _pre_sales_reply()[1].payload

    lot = payload["lot_constraints"][0]
    assert lot["remaining_freshness_days"] is None
    assert lot["effective_freshness_limit_days"] is None


# ── G. 날짜별 공급 ──────────────────────────────────────────────


def test_날짜별_공급량을_지어내지_않는다(wired_sales):
    """G — 🔴 특정 납기일의 판매가능량을 내는 권위 계산이 물류에 **없다.**

    금지 셋을 한꺼번에 막는다.

    ```text
    조회 구간 최대값               → 그 납기일 공급량   ❌
    현재 재고가 미래에도 그대로 남는다                   ❌
    future_occupancy_by_date(점유량) → 공급량            ❌
    ```

    ★ **빈 배열이 "0건 확인" 으로 읽히지 않게** 그 사실을 이름으로 함께 낸다.
    """
    payload = _pre_sales_reply()[1].payload

    assert payload["sellable_supply"]["supply_capacity_by_date"] == []
    assert payload["sellable_supply"]["uncertainties"] == ["SUPPLY_CAPACITY_BY_DATE_UNRESOLVED"]
    assert "supply_capacity_by_date" in payload["missing_data"]


def test_못_낸_날짜_공급의_이름에는_숫자가_없다(wired_sales):
    """미확정 이름에 숫자를 넣지 않는다 (`rules` 의 명명 규칙과 같다)."""
    payload = _pre_sales_reply()[1].payload

    for name in payload["sellable_supply"]["uncertainties"]:
        assert not any(character.isdigit() for character in name), name


# ── H. delivery feasibility ─────────────────────────────────────


def test_출고_여력_숫자가_있어도_납기_가능성은_UNRESOLVED_다(wired_sales):
    """H — 두 질문이 다르다.

    ```text
    하루 출고 총량   "얼마나 내보낼 수 있나"   ← 정책값으로 답한다
    납기 가능성      "그날 그 고객에게 닿나"   ← Route·운송시간 정본이 없다
    ```

    숫자가 있다고 `READY` 로 올리면 **답하지 않은 질문에 답한 것**이 된다.
    """
    payload = _pre_sales_reply()[1].payload
    delivery = payload["delivery_feasibility"]

    # 숫자는 정책 이름 그대로 최상위에 있다 — 근거를 정확히 붙일 수 있는 자리다
    assert payload["shared_daily_outbound_capacity_kg"] == 5000.0

    # 판정 블록에는 판정만 남는다
    assert delivery == {
        "status": "UNRESOLVED",
        "reason_codes": [],
        "uncertainties": [
            "DELIVERY_ROUTE_UNRESOLVED",
            "TRANSPORT_LEAD_TIME_UNRESOLVED",
            "EARLIEST_DELIVERY_DATE_UNRESOLVED",
        ],
    }


def test_납기_판정_블록에는_숫자를_두지_않는다(wired_sales):
    """🔴 **한 값이 두 자리에 있으면 받는 쪽이 어느 것을 볼지 갈린다.**

    그리고 중첩 안의 숫자는 봉투가 주소지정을 못 해(`envelope._CLAIM_PATH`) 근거를
    조상 블록에 달 수밖에 없는데, 그러면 *"`delivery_feasibility` 라는 판정의 값이
    5,000kg"* 으로 읽힌다 — 그 판정은 `UNRESOLVED` 라 **근거와 대상의 뜻이 어긋난다.**
    """
    delivery = _pre_sales_reply()[1].payload["delivery_feasibility"]

    assert not [value for value in delivery.values() if isinstance(value, (int, float))]
    assert "daily_outbound_capacity_kg" not in delivery


def test_납기일이나_배송_가능_여부를_만들지_않는다(wired_sales):
    """없는 Route·운송시간에서 날짜를 역산하지 않는다."""
    payload = _pre_sales_reply()[1].payload

    금지 = {"earliest_delivery_date", "delivery_feasible", "delivery_date", "transport_lead_days"}
    assert 금지 & _all_keys(payload) == set()


# ── I. 수량 역할 분리 ───────────────────────────────────────────


def test_남의_수량_축을_회신에_섞지_않는다(wired_sales):
    """I — 🔴 세 수량은 주인이 다르다.

    ```text
    confirmed            물류   지금 확정할 수 있는 판매가능량
    required_additional  판매   요청량 − confirmed
    conditional          매입   조건부 추가 확보 가능량
    ```

    물류가 뒤 둘을 내면 판매의 `_supply()` 가 그것을 확정 재고로 빼서 **부족량이
    사라진다.** 칸을 아예 만들지 않는 것이 방어다.
    """
    payload = _pre_sales_reply()[1].payload

    금지 = {
        "required_additional_quantity_kg",
        "additional_supply_required",
        "conditional_quantity_kg",
        "procurable_quantity_kg",
        "requested_quantity_kg",
    }
    assert 금지 & _all_keys(payload) == set()


# ── J. SELL_PRIORITY 범위 고정 ──────────────────────────────────


def test_회전관리_축을_이번_판에서_읽지_않는다():
    """J — `SELL_PRIORITY` 는 #346 범위 밖이다. **경계를 코드로 고정한다.**

    🔴 `FRESHNESS_QUALITY_RISK`(물리 신선도)를 `SELL_PRIORITY`(회전관리)로 이름만
       바꿔 내보내면 두 축이 한 이름이 되고, `turnover.py` 가 *"동시에 다른 답을 낼 수
       있어야 한다"* 고 못박은 구분이 무너진다.
    """
    modules, names = _adapter_imports()

    assert "app.logistics.turnover" not in modules
    assert not {"load_lot_turnover", "sell_priority_of", "derive_turnover_status"} & names


def test_신선도_위험을_판매우선으로_고쳐_부르지_않는다(monkeypatch):
    """기존 코드명을 그대로 보존한다 — 새 어휘를 만들지 않는다."""
    _with_read(monkeypatch, _sales_snapshot(freshness_pressure_ratio=Decimal("0.9")))
    payload = _pre_sales_reply()[1].payload

    codes = {row["code"] for row in payload["soft_warnings"]}
    assert "FRESHNESS_QUALITY_RISK" in codes
    assert "SELL_PRIORITY" not in codes
    assert "sell_priority" not in _all_keys(payload)


# ── K. 읽기 전용 · LLM 없음 ─────────────────────────────────────


def test_판매_Service_와_LLM_경로를_아예_들여오지_않는다():
    """K — 🔴 **부를 수 없게 해 둔다.**

    *"안 불렀다"* 를 실행으로 재면 경로 하나를 놓치는 날 조용히 통과하지만,
    **import 가 없으면 부를 방법이 없다.**

    ```text
    run_logistics_sales()                sim_run_id 축 소실 + save_logistics_agent_run DB write
    run_logistics_sales_with_snapshot()  enrich_logistics_response → LLM
    run_logistics_sales_scenario()       approved_purchase 필수
    ```
    """
    modules, names = _adapter_imports()

    금지_모듈 = {
        "app.logistics.service",
        "app.logistics.interpretation",
        "app.logistics.run_repository",
        "app.logistics.db",
        "app.logistics.outbound",
    }
    금지_이름 = {
        "run_logistics_sales",
        "run_logistics_sales_with_snapshot",
        "run_logistics_sales_scenario",
        "evaluate_sales_rules",
        "enrich_logistics_response",
        "save_logistics_agent_run",
        "get_connection",
        "execute_returning_one",
    }
    assert 금지_모듈 & modules == set()
    assert 금지_이름 & names == set()


def test_승인_매입을_지어내지_않는다():
    """🔴 `LogisticsApprovedPurchaseCommitment` 이 어댑터에 **들어오지도 않는다.**

    그 모델은 `total_qty_kg > 0` · `arrival_schedule` 최소 1건이라 빈 값을 못 넣는다 —
    쓰려면 없는 입고를 지어내야 하고, 그 입고가 `LOG-H01` 판정을 그대로 바꾼다.
    """
    _, names = _adapter_imports()

    assert (
        not {
            "LogisticsApprovedPurchaseCommitment",
            "LogisticsSalesRequest",
            "overlay_approved_purchase",
            "calculate_future_occupancy_by_date",
        }
        & names
    )


def test_LLM_을_안_썼다는_말이_사실이다(wired_sales):
    """`llm_status="DISABLED"` 가 실제 실행과 일치해야 한다."""
    _, _, meta = _pre_sales_reply()

    assert meta.llm_status == "DISABLED"


# ── L. 봉투 ─────────────────────────────────────────────────────


def test_PRE_SALES_회신이_봉투_검증을_통과한다(wired_sales):
    """L — 어댑터가 findings 를 내면 남 탓할 자리가 없다 — 우리가 만든 것이다.

    ★ 특히 `E-PLAN-EMPTY`(정상 회신인데 돌린 Tool 이 없다)와
      `E-EVIDENCE-MISSING`·`E-EVIDENCE-ORPHAN` 을 본다. 뒤 둘이 이 mode 의 payload 모양을
      정했다 — 봉투는 중첩 안의 숫자를 주소지정하지 못해(`_CLAIM_PATH`) 근거를 붙일 수
      있는 두 배열만 최상위에 있다.
    """
    request, reply, meta = _pre_sales_reply()

    assert validate_reply(request, reply, meta) == ()


def test_돌린_Tool_만_기록한다(wired_sales):
    """안 돈 것을 돈 것처럼 적지 않는다 — 그 반대도 마찬가지다."""
    _, _, meta = _pre_sales_reply()

    assert meta.used_tools == (
        "build_inventory_by_item",
        "build_lot_constraints",
        "evaluate_sales_business_signals",
    )
    assert meta.tool_order == (1, 2, 3)


def test_판매_경계에는_매입_Band_관측을_붙이지_않는다(wired_sales):
    """🔴 `_CAP_CHECK_ID` 는 **매입 밴드 전용 이름**이다.

    판매 Flow 에는 Critic 경로가 아예 없는데(`master/sales_flow.py`) 그 이름으로
    관측을 내면 매입의 밴드 검사가 판매 입력을 읽는다. 새 check_id 를 지어내는 것도
    금지다 — 마스터가 만들지 않은 계약을 물류가 먼저 만드는 것이 된다.
    """
    _, _, meta = _pre_sales_reply()

    assert meta.observations == ()


def test_숫자마다_근거가_붙는다(wired_sales):
    """근거 없는 숫자를 내보내지 않는다 — 어느 DB 행에서 왔는지가 이름에 남는다."""
    claims = {evidence.claim for evidence in _pre_sales_reply()[1].evidences}

    assert "inventory_by_item[배추].available_qty_kg" in claims
    assert "lot_constraints[LOT-A].available_qty_kg" in claims
    assert "lot_constraints[LOT-B].remaining_freshness_days" in claims
    assert "lot_constraints[LOT-A].effective_freshness_limit_days" in claims
    # 🔴 **정확히 그 숫자를 가리킨다.** 조상 블록(`delivery_feasibility`)에 달면 판정
    #    이름에 kg 값이 붙어 근거와 대상의 뜻이 어긋난다.
    assert "shared_daily_outbound_capacity_kg" in claims
    assert "delivery_feasibility" not in claims


def test_정책값_근거는_정책_출처를_가리킨다(wired_sales):
    """출고 여력은 물류가 계산한 값이 아니라 **정책 원값을 옮긴 것**이다."""
    _, reply, _ = _pre_sales_reply()
    evidence = next(e for e in reply.evidences if e.claim == "shared_daily_outbound_capacity_kg")

    assert evidence.value == 5000.0
    assert evidence.unit == "kg"
    assert evidence.evidence_grade == "SIM_FIXED"
    assert evidence.ref_ids == ("MVP-DECISION-20260825:N17",)


def test_근거_ref_를_발명하지_않는다(wired_sales):
    """payload 의 `evidence_refs` 는 Repository 가 스냅샷에 실어 둔 것 그대로다."""
    _, reply, _ = _pre_sales_reply()

    assert reply.payload["evidence_refs"] == _sales_snapshot().evidence_refs


def test_설명문에_숫자를_싣지_않는다(wired_sales):
    """숫자가 필요하면 Evidence 를 쓴다 (§1.2-3 · `E-REASONING-NUMERIC`)."""
    _, reply, _ = _pre_sales_reply()

    assert not any(character.isdigit() for character in reply.reasoning)


# ── query_scope ─────────────────────────────────────────────────


def test_마스터가_준_품목만_조회_범위에_적는다(wired_sales):
    """마스터가 ②에 싣는 것은 사용자 조건 그대로다 — 거기 없는 것은 물류도 모른다."""
    _, reply, _ = _pre_sales_reply({"user_request": {"item": "배추"}})

    assert reply.payload["query_scope"] == {"as_of": AS_OF.isoformat(), "item": "배추"}


def test_품목이_없으면_칸을_만들지_않는다(wired_sales):
    """`item: None` 을 실으면 *"지정이 없었다"* 와 *"안 읽었다"* 가 구별되지 않는다."""
    _, reply, _ = _pre_sales_reply()

    assert reply.payload["query_scope"] == {"as_of": AS_OF.isoformat()}


def test_조회_범위를_추론해_넓히지_않는다(wired_sales):
    """🔴 판매가 **쓰지 않기로 못박은** 값을 물류가 만들어 보내지 않는다.

    (`tests/sales/test_sales_proposal_core.py`
    `test_delivery_date_uses_exact_logistics_vector_not_query_scope_max`)
    """
    _, reply, _ = _pre_sales_reply({"user_request": {"item": "배추"}})

    금지 = {"delivery_window_start", "delivery_window_end", "max_confirmed_sellable_quantity_kg"}
    assert 금지 & set(reply.payload["query_scope"]) == set()


# ── runtime 미충족 ──────────────────────────────────────────────


def test_스냅샷을_못_읽으면_이름을_밝힌다(monkeypatch):
    """RUNTIME_NOT_READY 에 이름이 없으면 마스터가 무엇을 요청할지 모른다 (M-1 §5.1)."""
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: None)
    request, reply, meta = _pre_sales_reply()

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("logistics_snapshot", "logistics_runtime_fixture")
    assert validate_reply(request, reply, meta) == ()


def test_판매가능량을_확정_못_하면_Lot_합계로_대신_답하지_않는다(monkeypatch):
    """🔴 **fail-closed 다.**

    예약·할당 축을 못 읽었는데 Lot 물리 잔량으로 답하면 **이미 팔린 재고를 다시 팔 수
    있다고** 말하게 된다. 판매는 밴드가 없어 이 회신 없이도 시작하지만
    (`sales_flow._collect_supply_context`), 시작하는 것과 틀린 수량을 주는 것은 다르다.
    """
    _with_read(monkeypatch, _sales_snapshot(outbound_commitments=None))
    request, reply, meta = _pre_sales_reply()

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("inventory_by_item",)
    assert reply.payload == {}
    assert validate_reply(request, reply, meta) == ()


def test_출고_여력_정책이_없으면_READY_를_내지_않는다(monkeypatch):
    """판매 사이클의 기존 Rule 이 같은 기준이다 — `evaluate_sales_rules` 의 `N17`."""
    _with_read(monkeypatch, _sales_snapshot(shared_daily_outbound_capacity_kg=None))
    _, reply, _ = _pre_sales_reply()

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("shared_daily_outbound_capacity_kg",)


def test_다른_날의_재고를_그날의_사실로_답하지_않는다(monkeypatch):
    """★ 사유에 날짜를 적지 않는다 — `E-REASONING-NUMERIC` 이 잡는다."""
    _with_read(monkeypatch, _sales_snapshot(as_of=date(2025, 12, 30)))
    request, reply, meta = _pre_sales_reply()

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == (f"logistics_snapshot@{AS_OF.isoformat()}",)
    assert validate_reply(request, reply, meta) == ()


def test_조회가_실행_오류로_실패하면_ERROR_다(monkeypatch):
    """부재(다시 불러도 같다)와 실행 실패(재시도 가치가 있다)를 가른다 (M-1 §5.1)."""

    def _boom(*, as_of, sim_run_id):
        raise adapter._SnapshotLoadError

    monkeypatch.setattr(adapter, "_load_read", _boom)
    _, reply, _ = _pre_sales_reply()

    assert reply.runtime_status == "ERROR"
    assert reply.payload == {"failed_operation": "load_logistics_snapshot"}


# ── 못 낸 것의 이름 ─────────────────────────────────────────────


def test_구조적으로_못_내는_것을_READY_안에서도_밝힌다(wired_sales):
    """★ 현재 재고와 현재 물류 상태는 권위 있게 답했다 — 그것이 이 판의 READY 다.

    못 낸 것(날짜별 공급 · 납기 축 셋)은 READY 를 막지 않지만 **조용히 빠지지도 않는다.**
    봉투의 `missing_data` 와 payload 의 그것이 같은 사실이어야 화면과 마스터가 갈리지 않는다.
    """
    _, reply, _ = _pre_sales_reply()

    assert reply.missing_data == (
        "supply_capacity_by_date",
        "delivery_route",
        "transport_lead_time",
        "earliest_delivery_date",
    )
    assert list(reply.missing_data) == reply.payload["missing_data"]
    assert reply.runtime_status == "READY"


def test_있는_블록을_없다고_적지_않는다(wired_sales):
    """🔴 `delivery_feasibility` 는 **있다** — 판정이 `UNRESOLVED` 일 뿐이다.

    있는 것을 `missing_data` 에 적으면 마스터가 *"물류가 납기 블록을 안 보냈다"* 로 읽고
    사용자에게 엉뚱한 것을 달라고 한다 (M-1 §5.1). 없는 것은 블록이 아니라 **그 안을
    채울 Fact** 다.
    """
    _, reply, _ = _pre_sales_reply()

    assert "delivery_feasibility" not in reply.missing_data
    assert reply.payload["delivery_feasibility"]["status"] == "UNRESOLVED"


def test_못_낸_납기_축_이름이_uncertainties_와_같은_사실을_가리킨다(wired_sales):
    """★ 두 자리가 **같은 세 축**을 말한다 — 하나가 사라지면 다른 하나도 사라져야 한다.

    ```text
    payload.delivery_feasibility.uncertainties   물류 내부 코드 어휘 (*_UNRESOLVED)
    missing_data                                 마스터가 읽는 이름
    ```

    `interpretation._MISSING_DATA_NAMES` 가 코드를 사람용 이름으로 옮기는 것과 같은 층
    구분이라, 한쪽만 늘거나 줄면 *"무엇이 없는지"* 가 두 이름으로 갈린다.
    """
    _, reply, _ = _pre_sales_reply()

    축 = {
        "delivery_route": "DELIVERY_ROUTE_UNRESOLVED",
        "transport_lead_time": "TRANSPORT_LEAD_TIME_UNRESOLVED",
        "earliest_delivery_date": "EARLIEST_DELIVERY_DATE_UNRESOLVED",
    }
    uncertainties = reply.payload["delivery_feasibility"]["uncertainties"]

    assert set(축.values()) == set(uncertainties)
    assert set(축) <= set(reply.missing_data)
    # 날짜별 공급도 같은 짝을 이룬다
    assert reply.payload["sellable_supply"]["uncertainties"] == [
        "SUPPLY_CAPACITY_BY_DATE_UNRESOLVED"
    ]
    assert "supply_capacity_by_date" in reply.missing_data


# ── 판매 계약 호환 ──────────────────────────────────────────────


def test_payload_가_판매_계약으로_그대로_읽힌다(wired_sales):
    """🔴 **어댑터는 `app.sales.schemas` 를 import 하지 않는다 — 여기서만 읽는다.**

    런타임에 물류가 판매 스키마에 묶이면 판매가 자기 파일을 고치는 날 물류가 같이
    깨진다. 그렇다고 *"모양을 맞췄다"* 를 말로만 두면 어느 날 조용히 갈린다 —
    마스터가 `Capability` 어휘를 베껴 두고 테스트로만 대조하는 것과 **같은 자리**다
    (`master/envelope.py` `Capability` docstring · `tests/master/test_sales_flow.py`).

    ★ **받는 쪽이 하는 일이 무엇인지도 함께 고정한다.** 아래 매핑이 전부다 —
      최상위 셋을 제자리로 옮기는 **키 이동뿐이고 재계산이 없다.**

      ```text
      inventory_by_item                  → sellable_supply.inventory_by_item
      lot_constraints                    → sellable_supply.lot_constraints
      shared_daily_outbound_capacity_kg  → delivery_feasibility.daily_outbound_capacity_kg
      ```

      셋이 최상위에 있는 이유는 봉투가 중첩 안의 숫자에 근거를 못 달기
      때문이지(`envelope._CLAIM_PATH`) 판매 계약과 달라서가 아니다.
    """
    from app.sales.schemas import SalesLogisticsContext

    payload = _pre_sales_reply({"user_request": {"item": "배추"}})[1].payload

    context = SalesLogisticsContext.model_validate(
        {
            "query_scope": payload["query_scope"],
            "sellable_supply": {
                **payload["sellable_supply"],
                "inventory_by_item": payload["inventory_by_item"],
                "lot_constraints": payload["lot_constraints"],
            },
            "delivery_feasibility": {
                **payload["delivery_feasibility"],
                "daily_outbound_capacity_kg": payload["shared_daily_outbound_capacity_kg"],
            },
            "hard_constraints": payload["hard_constraints"],
            "soft_warnings": payload["soft_warnings"],
            "missing_data": payload["missing_data"],
            "evidence_refs": payload["evidence_refs"],
        }
    )

    # 수량은 물류가 낸 그대로다 — 판매가 다시 합산하지 않는다
    assert {
        row.item: row.available_qty_kg for row in context.sellable_supply.inventory_by_item
    } == {
        "배추": Decimal("500.0"),
        "무": Decimal("300.0"),
    }
    # 🔴 **음수가 경계를 건너서도 살아남는다.** 판매 DTO 가 거부하면 여기가 빨간불이다
    assert [lot.remaining_freshness_days for lot in context.sellable_supply.lot_constraints] == [
        10,
        -3,
        7,
    ]
    assert context.sellable_supply.supply_capacity_by_date == []
    assert context.delivery_feasibility.status == "UNRESOLVED"
    # 키만 옮겼고 값은 정책 원값 그대로다 — mapper 가 계산하지 않는다
    assert context.delivery_feasibility.daily_outbound_capacity_kg == Decimal("5000.0")
    assert context.missing_data == list(payload["missing_data"])
