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
from app.logistics.llm import runtime as llm_runtime
from app.logistics.llm.runtime import (
    InterpretationService,
    LLMSettings,
    ProviderResult,
    ProviderUsage,
    UnavailableProvider,
    build_template_interpretation,
    needs_llm,
)
from app.logistics.llm.schemas import SanitizedLLMContext
from app.logistics.repository import LogisticsRead
from app.logistics.rules import (
    BUSINESS_SIGNALS,
    CAPACITY_TIGHT,
    FRESHNESS_QUALITY_RISK,
    INVENTORY_FRESHNESS_PRESSURE,
    SCENARIO_ADJUSTMENT_REQUIRED,
)
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


#: 대역이 내는 고정 운송 계약. **문자열을 여기서 짓지 않는다** — `logistics_contracts`
#: 표의 실제 값과 같은 모양이고, 정본 Reader 는 `transport.resolve_fixed_route` 다.
_ROUTE = "LOGI-BASE-5PL"


def _read(snapshot=None, policy=None, *, route=_ROUTE, route_error=False):
    """어댑터의 단일 읽기 seam — Snapshot · Policy · 운송 계약을 함께 준다 (#121 ⑤).

    🔴 **운송 계약도 이 한 벌에 들어온다.** 어댑터가 자기 커넥션을 열어 따로 읽으면
       한 회신 안에서 읽기가 두 시점으로 갈리고, 검사도 실 DB 에 매인다.
    """
    return LogisticsRead(
        snapshot=snapshot or _snapshot(),
        policy=policy or _policy(),
        delivery_route=route,
        delivery_route_error=route_error,
    )


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read())
    monkeypatch.setattr(adapter, "build_lot_constraints", lambda snapshot: list(_LOTS))


@pytest.fixture(autouse=True)
def master_llm_off(monkeypatch):
    """★ Master-facing 해석은 opt-in 이다 (#385) — 이 파일의 기본은 **꺼짐**이다.

    개발자 `.env` 에 켜 둔 값이 `load_dotenv` 로 새어 들어와 실 Provider 를 부르는 일이
    없게 여기서 고정한다 (`load_dotenv` 는 이미 있는 환경변수를 덮지 않는다). LLM 을
    켜서 재는 테스트는 팩토리를 가짜 서비스로 갈아 끼운다 — env 로 켜지 않는다.
    """
    monkeypatch.setenv("LOGISTICS_MASTER_LLM_ENABLED", "false")


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
    """PRE_PURCHASE 에는 해석 경로가 없다 — `DISABLED` 가 사실이다.

    #385 는 SCENARIO_VALIDATION 만 잇는다.
    """
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

    배추 300(가용) + 배추 100(신선도 만료 — 제외) + 무 50.
    기대: 무 50 · 배추 300.

    🔴 **확정 출고는 안 뺀다 (WP-3).** 그 축은 예약·할당으로 이미 한 번 빠진다 —
       둘을 다 빼면 같은 판매를 두 번 차감한다.
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
    정의(비-ACTIVE·만료 제외, 예약·할당 차감)를 남의 도메인에서 재구현하게 된다.

    🔴 **차감 축이 한 벌이다 (WP-3).** 종전에는 예약·할당에 더해
       `confirmed_outbound_schedule` 도 뺐고 배추가 180 이었다. 확정 판매는 그날
       마스터 출고 흐름이 **예약으로 내려보내는 바로 그 사실**이라, 둘을 다 빼면
       같은 판매를 두 번 차감한다.
    """
    reply, _ = adapter.logistics_port(req())
    assert reply.payload["inventory_by_item"] == [
        {"item": "무", "available_qty_kg": 50.0},
        {"item": "배추", "available_qty_kg": 300.0},
    ]


def test_가용재고_근거는_번호가_아니라_품목명으로_가리킨다(stocked):
    """Lot·보관정책과 같은 이름 선택자다 — 번호로 쓰면 품목 순서가 바뀌는 날
    근거가 다른 품목을 가리킨다."""
    reply, _ = adapter.logistics_port(req())
    claims = {evidence.claim: evidence.value for evidence in reply.evidences}
    assert claims["inventory_by_item[배추].available_qty_kg"] == 300.0
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
    monkeypatch.setattr(
        adapter,
        "_load_read",
        lambda *, as_of, sim_run_id: _read(_stocked_snapshot(outbound_commitments=None)),
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
        {"item": "배추", "available_qty_kg": 300.0},
    ]
    assert validate_reply(request, reply, meta) == ()


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — 해석 Harness 연결 (#385)
#
# ★ LLM 은 결정론 결과가 다 선 뒤에만 돌고, 어느 상태에서도 업무 결과를 바꾸지 않는다.
#   Provider 는 여기서 전부 가짜다 — 실 Gemini · Ollama · HTTP 는 한 번도 열리지 않는다.
#   주입 seam 은 `adapter.master_interpretation_service` 팩토리다 (`logistics_port` 는 그대로).
# ---------------------------------------------------------------------------


def _signal_payload() -> dict:
    """조정 제안이 나오는 제안 — 창고 여유로 20,000kg 을 못 받아 `conditional` 이 되고
    `SCENARIO_ADJUSTMENT_REQUIRED`(질적 signal → 게이트 통과)가 선다."""
    payload = _proposal_payload()
    scenario = payload["scenarios"][0]
    scenario["total_qty_kg"] = 20000
    scenario["total_amount_krw"] = 33000000
    scenario["split_plan"] = [{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 20000}]
    scenario["sourcing_plan"] = [
        {"market": "가락", "grade": "상", "qty_kg": 20000, "grade_unit_price": 1650}
    ]
    return payload


class _Provider:
    """가짜 Provider — 받은 Context 를 기록하고, 정해진 방식으로 답하거나 실패한다.

    `echo` 는 받은 Context 에서만 답을 만든다 — signals 를 그대로 risks 로, preferred 를
    그대로 suggested 로. 검증기를 통과하는 유일한 방법이 Context 인용뿐이라는 뜻이다.
    """

    def __init__(self, behaviour: str = "echo", *, usage: ProviderUsage | None = None):
        self.behaviour = behaviour
        self.usage = usage
        self.contexts: list = []
        self.guidance: list = []

    def generate(self, context, *, retry_guidance=None):
        self.contexts.append(context)
        self.guidance.append(retry_guidance)
        if self.behaviour == "timeout":
            raise TimeoutError()
        if self.behaviour == "invalid":
            return ProviderResult(text="not json", usage=self.usage)
        return ProviderResult(
            text=json.dumps(
                {
                    "summary": "매입안이 물류 경계에 걸려 조정 검토가 필요합니다.",
                    "risks": list(context.signals),
                    "suggested_adjustment": context.preferred_adjustment,
                },
                ensure_ascii=False,
            ),
            # 기본은 `None` 이다 — usage 를 보고하지 않는 Provider 응답이 공식 계약상
            # 정상이고, 기존 검사들이 그 경로를 그대로 재현해야 한다 (#406).
            usage=self.usage,
        )

    @property
    def calls(self) -> int:
        return len(self.contexts)


def _llm(provider: _Provider, *, enabled: bool = True) -> InterpretationService:
    return InterpretationService(
        LLMSettings(
            enabled=enabled,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=0,
        ),
        provider,
    )


def _inject(monkeypatch, service: InterpretationService) -> None:
    """주입 seam — `logistics_port` 시그니처는 그대로고 팩토리만 갈아 끼운다."""
    monkeypatch.setattr(adapter, "master_interpretation_service", lambda: service)


def _business_view(reply) -> dict:
    """LLM 이 건드리면 안 되는 것 전부 — 구조 비교 대상이다. 해석 칸 하나만 뺀다."""
    return {
        "runtime_status": reply.runtime_status,
        "business_status": reply.business_status,
        "payload": {key: value for key, value in reply.payload.items() if key != "interpretation"},
        "evidences": reply.evidences,
        "suggested_adjustments": reply.suggested_adjustments,
        "needs_followup": reply.needs_followup,
        "judgment_fields": reply.judgment_fields,
        "missing_data": reply.missing_data,
        "reasoning": reply.reasoning,
    }


def _trace_view(meta) -> dict:
    """LLM 과 무관한 실행 흔적 — Tool 순서와 DeptMeta 관측도 상태 따라 흔들리면 안 된다.

    🔴 **`inventory_llm_trace` 하나만 뺀다** (#402). observations 비교를 통째로 지우거나
      "`inventory_dept_meta` 만 본다" 로 좁히지 않는다 — 전자는 DeptMeta drift 를 무검사로
      만들고, 후자는 **앞으로 추가될 관측이 조용히 비교에서 빠진다**(fail-open). 이름 하나를
      제외하면 나머지 전부가 결정론 비교에 남는다 (fail-closed).

    ★ 문자열을 베끼지 않고 `adapter._LLM_TRACE_OBSERVATION` 을 참조한다 — 이름이 바뀌는 날
      필터만 조용히 빗나가면 이 비교가 LLM 관측까지 삼켜 매번 깨진다.
    ★ 제외가 관측을 **전부** 지워 버리는 변이는
      `test_결정론_비교에서_제외되는_것은_LLM_관측_하나뿐이다` 가 따로 잡는다.
    """
    return {
        "used_tools": meta.used_tools,
        "tool_order": meta.tool_order,
        "observations": tuple(
            item
            for item in meta.observations
            if json.loads(item).get("observation_type") != adapter._LLM_TRACE_OBSERVATION
        ),
    }


def _business_signals(reply) -> list[str]:
    return [code for code in reply.payload["soft_warnings"] if code in BUSINESS_SIGNALS]


def test_opt_in_이_없으면_DISABLED_이고_Provider_클라이언트를_만들지_않는다(stocked):
    """★ 설정 부재 = 꺼짐. 실 팩토리를 그대로 탄다 — autouse 가 opt-in 을 끈 상태다."""
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    assert reply.runtime_status == "READY"
    assert _business_signals(reply), "signal 이 서야 게이트를 재는 뜻이 있다"
    assert meta.llm_status == "DISABLED"
    assert meta.llm_attempts == 0
    assert meta.llm_fallback_used is False
    # 해석 칸은 있되 무숫자 Template 다 — signal 코드는 그대로 보존된다
    assert reply.payload["interpretation"]["risks"] == _business_signals(reply)
    assert not any(ch.isdigit() for ch in reply.payload["interpretation"]["summary"])
    service = adapter.master_interpretation_service()
    assert service.settings.enabled is False
    assert isinstance(service.provider, UnavailableProvider)
    assert validate_reply(request, reply, meta) == ()


def test_opt_in_이_있어도_signal_이_없으면_SKIPPED_TEMPLATE(monkeypatch, stocked):
    """★ 게이트 — 켜져 있는데 부를 이유가 없다. Provider 호출 0회."""
    provider = _Provider()
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_proposal_payload())
    reply, meta = adapter.logistics_port(request)

    assert reply.runtime_status == "READY"
    assert _business_signals(reply) == []
    assert meta.llm_status == "SKIPPED_TEMPLATE"
    assert meta.llm_attempts == 0
    assert provider.calls == 0
    assert "interpretation" in reply.payload
    assert validate_reply(request, reply, meta) == ()


def test_SUCCESS_는_해석을_payload_중첩_칸에_상태를_metadata_에_적는다(monkeypatch, stocked):
    provider = _Provider()
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    assert provider.calls == 1
    assert meta.llm_status == "SUCCESS"
    assert meta.llm_model == "fake-model"
    assert meta.llm_attempts == 1
    assert meta.llm_fallback_used is False
    context = provider.contexts[0]
    assert reply.payload["interpretation"] == {
        "summary": "매입안이 물류 경계에 걸려 조정 검토가 필요합니다.",
        "risks": list(context.signals),
        "suggested_adjustment": context.preferred_adjustment,
    }
    # 판정 칸은 LLM 이 아니라 Rule 의 것이다 — 그대로다
    assert reply.payload["verdict"] == "conditional"
    assert reply.business_status == "conditional"
    assert validate_reply(request, reply, meta) == ()


def test_timeout_은_FALLBACK_이고_업무_실패로_승격되지_않는다(monkeypatch, stocked):
    provider = _Provider("timeout")
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    assert provider.calls == 1  # max_retries=0 — 전송 재시도 없이 접는다
    assert reply.runtime_status == "READY"
    assert reply.business_status == "conditional"
    assert meta.llm_status == "FALLBACK"
    assert meta.llm_fallback_used is True
    assert meta.llm_attempts == 1
    template = build_template_interpretation(provider.contexts[0])
    assert reply.payload["interpretation"] == template.model_dump(mode="json")
    assert validate_reply(request, reply, meta) == ()


def test_검증_탈락은_correction_한_번_뒤_FALLBACK(monkeypatch, stocked):
    provider = _Provider("invalid")
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    assert provider.calls == 2
    assert provider.guidance[0] is None
    assert provider.guidance[1], "두 번째 호출에는 correction 이 붙는다"
    assert meta.llm_status == "FALLBACK"
    assert meta.llm_attempts == 2
    assert not any(ch.isdigit() for ch in reply.payload["interpretation"]["summary"])
    assert validate_reply(request, reply, meta) == ()


def test_결정론_보존_LLM_상태가_달라도_업무_결과는_구조적으로_같다(monkeypatch, stocked):
    """🔴 이 파일의 핵심이다. "거의 같다" 가 아니라 **구조 비교**다.

    같은 요청을 다섯 상태로 돌린다 — DISABLED · SKIPPED_TEMPLATE · SUCCESS ·
    FALLBACK(timeout) · FALLBACK(검증 탈락). 해석 칸 하나를 뺀 reply 와, LLM 칸을 뺀
    metadata 가 전부 같아야 한다.
    """
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    real_builder = adapter.build_sanitized_context

    def incomplete_builder(**kwargs):
        # facts 조립 실패를 흉내 내 게이트를 닫는다 — 같은 요청으로 SKIPPED 를 만든다
        context, _ = real_builder(**kwargs)
        return context, True

    cases = {
        "DISABLED": (_llm(_Provider(), enabled=False), real_builder),
        "SKIPPED_TEMPLATE": (_llm(_Provider()), incomplete_builder),
        "SUCCESS": (_llm(_Provider()), real_builder),
        "FALLBACK/timeout": (_llm(_Provider("timeout")), real_builder),
        "FALLBACK/invalid": (_llm(_Provider("invalid")), real_builder),
    }
    views: dict[str, dict] = {}
    traces: dict[str, dict] = {}
    for name, (service, builder) in cases.items():
        _inject(monkeypatch, service)
        monkeypatch.setattr(adapter, "build_sanitized_context", builder)
        reply, meta = adapter.logistics_port(request)
        assert meta.llm_status == name.split("/")[0], name
        assert validate_reply(request, reply, meta) == (), name
        views[name] = _business_view(reply)
        traces[name] = _trace_view(meta)

    for name in cases:
        assert views[name] == views["DISABLED"], name
        assert traces[name] == traces["DISABLED"], name


def test_Provider_가_받는_것은_SanitizedLLMContext_뿐이다(monkeypatch, stocked):
    """★ 전송 경계 — Lot · 날짜 · 원본 수량 · 단가 · 식별자 · payload 는 넘어가지 않는다."""
    provider = _Provider()
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, _ = adapter.logistics_port(request)

    assert len(provider.contexts) == 1
    context = provider.contexts[0]
    assert isinstance(context, SanitizedLLMContext)
    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)
    forbidden = ("LOT-", AS_OF.isoformat(), "20000", "33000000", "1650", "REQ-T", SIM_RUN_ID, "kg")
    for token in forbidden:
        assert token not in serialized, token
    # payload 의 결정론 키가 통째로 넘어가지 않는다
    for key in ("cap_by_date", "scenario_results", "inventory_by_item", "expected_arrival_dates"):
        assert key not in serialized, key
    # 미확정 이름은 번역돼 있다 — `logistics_rule/LOG-H02` 같은 raw 코드가 아니다
    assert context.missing_data
    assert not any(ch.isdigit() for name in context.missing_data for ch in name)
    assert not any("/" in name or "@" in name for name in context.missing_data)
    # facts 는 판정 수치의 확정 표기뿐이다
    assert [fact.fact_id for fact in context.facts] == ["scenario_conditional_count"]
    assert reply.payload["interpretation"]["risks"] == list(context.signals)


@pytest.mark.parametrize(("enabled", "expected"), [(True, "SKIPPED_TEMPLATE"), (False, "DISABLED")])
def test_못_낸_회신도_상태_어휘가_사실이다(monkeypatch, stocked, enabled, expected):
    """★ 켜져 있었는데 부를 자리에 못 갔다 = SKIPPED_TEMPLATE, 꺼져 있었다 = DISABLED.

    어느 쪽도 Provider 를 부르지 않고, 해석 칸도 만들지 않는다 (못 낸 회신이다).
    """
    provider = _Provider()
    _inject(monkeypatch, _llm(provider, enabled=enabled))
    request = req(mode="SCENARIO_VALIDATION", payload={"scenarios": []})
    reply, meta = adapter.logistics_port(request)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert "interpretation" not in reply.payload
    assert meta.llm_status == expected
    assert meta.llm_attempts == 0
    assert provider.calls == 0
    assert validate_reply(request, reply, meta) == ()


def test_기준일_불일치_ERROR_에도_상태_어휘가_사실이다(monkeypatch, stocked):
    provider = _Provider()
    _inject(monkeypatch, _llm(provider))
    payload = _proposal_payload()
    payload["meta"]["as_of"] = "2026-01-01"
    payload["scenarios"][0]["split_plan"][0]["date"] = "2026-01-01"
    reply, meta = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=payload))

    assert reply.runtime_status == "ERROR"
    assert "interpretation" not in reply.payload
    assert meta.llm_status == "SKIPPED_TEMPLATE"
    assert provider.calls == 0


@pytest.mark.parametrize("mode", ["PRE_PURCHASE", "PRE_SALES", "STATUS_QUERY"])
def test_다른_mode_는_opt_in_이_있어도_LLM_경로가_없다(monkeypatch, stocked, mode):
    """#385 는 SCENARIO_VALIDATION 만 잇는다 — 나머지는 `DISABLED` 가 여전히 사실이다."""
    provider = _Provider()
    _inject(monkeypatch, _llm(provider))
    reply, meta = adapter.logistics_port(req(mode=mode))

    assert provider.calls == 0
    assert meta.llm_status == "DISABLED"
    assert "interpretation" not in reply.payload


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
    from app.master.critic.critic_v0_4 import FORBIDDEN_SCENARIO_INPUTS

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
    from app.master.critic.critic_v0_4 import FORBIDDEN_SCENARIO_INPUTS

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
    LOT-B  배추   700kg  신선도 -3   ← 신선도 만료 · 가용에서 빠진다
    LOT-C  무     300kg  신선도  7
                          + 배추 미할당 예약 100 (Lot 미지정)

    배추 가용   = (1,000 − 400) − 100 = 500      무 가용 = 300
    Lot 물리 합계(배추) = 1,700                  ← 500 과 다르다. 이것이 요점이다
    ```

    🔴 **어느 Lot 수량도 정답(500·300)과 같지 않게 골랐다.** 종전에 LOT-B 를 500 으로
       뒀더니, 차감을 빼먹고 Lot 을 그대로 싣는 **뮤턴트가 살아남았다** — 검사가
       `{item: qty}` 로 접는 순간 배추 두 행 중 뒤엣것(500)만 남아 우연히 정답이 됐다.
       숫자가 겹치면 검사는 조용히 통과한다.
    """
    base: dict = {
        "on_hand_by_lot": [
            _sales_lot("LOT-A", "배추", "1000", 10),
            _sales_lot("LOT-B", "배추", "700", -3),
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

    # 🔴 **배열 그대로 잰다.** `{item: qty}` 로 접으면 같은 품목의 여러 행이 뒤엣것
    #    하나로 뭉개져, Lot 을 차감 없이 그대로 실은 회신도 통과한다 (뮤턴트 실측).
    #    품목당 한 행이라는 것도 이 계약의 일부다 — 합계는 물류가 이미 냈다.
    assert reply.payload["sellable_supply"]["inventory_by_item"] == [
        {"item": "무", "available_qty_kg": 300.0},
        {"item": "배추", "available_qty_kg": 500.0},
    ]


def test_Lot_수량을_합산해도_판매가능량이_되지_않는다(wired_sales):
    """E — 두 배열은 **다른 뜻**이다. 같은 수가 나오면 한쪽이 잘못된 것이다.

    `lot_constraints` 는 예약·할당 차감 **전** 물리 잔량이고 만료 Lot 도 들어 있다.
    받는 쪽이 이것을 합산해 판매가능량을 다시 만들면 안 된다는 것을 수로 잠근다.
    """
    payload = _pre_sales_reply()[1].payload

    supply = payload["sellable_supply"]
    lot_total = sum(
        row["available_qty_kg"] for row in supply["lot_constraints"] if row["item"] == "배추"
    )
    confirmed = next(
        row["available_qty_kg"] for row in supply["inventory_by_item"] if row["item"] == "배추"
    )
    assert lot_total == 1700.0  # 1,000 + 700(만료분 포함)
    assert confirmed == 500.0
    assert lot_total != confirmed


def test_신선도가_만료된_Lot_은_판매가능량에서_빠지되_근거로는_남는다(wired_sales):
    """만료 Lot 을 **숨기지 않는다.** 가용에서 빠지는 것과 없던 일이 되는 것은 다르다."""
    payload = _pre_sales_reply()[1].payload

    assert "LOT-B" in {row["lot_id"] for row in payload["sellable_supply"]["lot_constraints"]}


# ── F. freshness ────────────────────────────────────────────────


def test_음수_신선도를_그대로_나른다(wired_sales):
    """F — 🔴 **0 으로 접지 않는다.**

    `-3` 은 *"신선도 기준을 지난 지 사흘"* 이라는 사실이다. 0 으로 보정하면 **기준일
    당일**과 **사흘 지난 Lot** 이 같은 값이 되고, 받는 쪽은 그 차이를 영영 못 본다.
    """
    payload = _pre_sales_reply()[1].payload
    by_lot = {
        row["lot_id"]: row["remaining_freshness_days"]
        for row in payload["sellable_supply"]["lot_constraints"]
    }

    assert by_lot["LOT-B"] == -3
    assert by_lot["LOT-A"] == 10  # 다른 Lot 도 손대지 않았다


def test_신선도_분모를_다시_계산하지_않고_그대로_나른다(wired_sales):
    """잔여 신선도를 낸 그 유효 한계를 함께 싣는다 — 받는 쪽이 역산하지 않도록.

    `중` 등급은 유효 한계가 `operational_limit × medium_grade_factor` 라, 품목 정책
    원값으로 역산하면 **갓 입고된 Lot 이 임박으로 보인다.**
    """
    payload = _pre_sales_reply()[1].payload

    lot_a = next(
        row for row in payload["sellable_supply"]["lot_constraints"] if row["lot_id"] == "LOT-A"
    )
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

    lot = payload["sellable_supply"]["lot_constraints"][0]
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

    ★ **창을 물류가 만들지 않는다 (WP-4).** 사용자가 물은 납기일 하나가 답할 날짜이고,
      안 물었으면 답할 날짜가 없다. 여기서 임의 창을 만들면 **묻지도 않은 날짜의
      공급량**이 판매 근거로 나간다.

    🔴 **빈 목록에 «못 냈다» 이름을 안 단다.** 계산에 실패한 것과 물어본 날짜가 없는
       것은 다른 사실이다 (§1.2-10) — 종전에는 둘을 한 이름으로 뭉갰다.
    """
    payload = _pre_sales_reply()[1].payload

    assert payload["sellable_supply"]["supply_capacity_by_date"] == []
    assert payload["sellable_supply"]["uncertainties"] == []
    assert "supply_capacity_by_date" not in payload["missing_data"]


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
    delivery = _pre_sales_reply()[1].payload["delivery_feasibility"]

    # ★ **숫자와 판정이 한 블록에 산다 (WP-4B).** 판매 계약
    #   (`sales.schemas.LogisticsDeliveryFeasibility`)의 모양 그대로다.
    assert delivery == {
        "status": "UNRESOLVED",
        "daily_outbound_capacity_kg": 5000.0,
        "delivery_route": "LOGI-BASE-5PL",
        "transport_lead_time": 0,
        "earliest_delivery_date": None,
        "reason_codes": [],
        "uncertainties": ["OUTBOUND_PREP_LEAD_DAYS_UNRESOLVED"],
    }


def test_같은_숫자를_두_자리에_두지_않는다(wired_sales):
    """🔴 **한 값이 두 자리에 있으면 받는 쪽이 어느 것을 볼지 갈린다.**

    ⚠️ 종전에는 반대였다 — 숫자 셋을 payload **최상위로 끌어올려** 중복시켰다.
       봉투가 중첩 안의 숫자를 주소지정하지 못해서였는데(`envelope._CLAIM_PATH`),
       그 회피가 판매 계약 모양을 깨뜨렸다. WP-4B 가 중첩 한 벌로 되돌렸고,
       주소지정은 봉투 쪽에서 풀 일로 남겼다 (Master HANDOFF).
    """
    payload = _pre_sales_reply()[1].payload

    for 올라오면_안_되는_키 in (
        "inventory_by_item",
        "lot_constraints",
        "shared_daily_outbound_capacity_kg",
        "daily_outbound_capacity_kg",
        "as_of",
        "policy_version_used",
    ):
        assert 올라오면_안_되는_키 not in payload
    assert payload["delivery_feasibility"]["daily_outbound_capacity_kg"] == 5000.0


def test_준비일_정책이_없으면_납기일을_지어내지_않는다(wired_sales):
    """🔴 **정책이 없으면 코드 상수로 메우지 않는다 (WP-4 M4).**

    `outbound_prep_lead_days` 가 없으면 가장 이른 납기일을 못 내고, 못 내는 것을
    `READY` 로 답하면 **DB 에 정책이 없는데도 납기가 확정된 것처럼** 나간다.
    """
    payload = _pre_sales_reply()[1].payload
    delivery = payload["delivery_feasibility"]

    assert delivery["status"] == "UNRESOLVED"
    assert delivery["earliest_delivery_date"] is None
    assert "OUTBOUND_PREP_LEAD_DAYS_UNRESOLVED" in delivery["uncertainties"]
    # ★ 물류가 짓지 않는 이름들 — 있는 이름을 다른 이름으로 또 내지 않는다.
    금지 = {"delivery_feasible", "delivery_date", "transport_lead_days"}
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

    ★ #385 이후 `app.logistics.interpretation` 은 **허용**이다 — 어댑터가 재사용하는 것은
      순수 Harness 조립기(`build_sanitized_context`)와 opt-in 팩토리뿐이다. Service 응답
      타입에 묶인 `enrich_logistics_response`, Provider 층(`app.logistics.llm.runtime`),
      쓰기 경로, 마스터의 `cycle_llm` 은 여전히 들여오지 않는다.
    """
    modules, names = _adapter_imports()

    금지_모듈 = {
        "app.logistics.service",
        "app.logistics.run_repository",
        "app.logistics.db",
        "app.logistics.outbound",
        "app.logistics.llm.runtime",
        "app.master.cycle_llm",
    }
    금지_이름 = {
        "run_logistics_sales",
        "run_logistics_sales_with_snapshot",
        "run_logistics_sales_scenario",
        "evaluate_sales_rules",
        "enrich_logistics_response",
        "get_interpretation_service",
        "save_logistics_agent_run",
        "get_connection",
        "execute_returning_one",
    }
    assert {
        module
        for module in modules
        for banned in 금지_모듈
        if module == banned or module.startswith(banned + ".")
    } == set()
    assert 금지_이름 & names == set()
    # 허용된 것은 조립기 · opt-in 팩토리 · 미호출 상태뿐이다 — 어댑터의 LLM 표면 전부다
    assert {
        "build_sanitized_context",
        "master_interpretation_service",
        "uncalled_interpretation",
    } <= names


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
    """L — 남는 finding 은 **중첩 주소 해석 하나뿐**이어야 한다.

    ⚠️ **이것이 지금 열려 있는 유일한 어긋남이다 (Master HANDOFF).**
       `envelope._CLAIM_PATH` 의 `key` 가 점을 안 받아
       `sellable_supply.inventory_by_item[배추].available_qty_kg` 가 매치에 실패하고
       `E-EVIDENCE-ORPHAN` 이 된다. 흐름은 안 막는다 —
       `verifier` 가 `M16-ENVELOPE` 로 보고할 뿐이다.

    🔴 **여기를 초록으로 만들려고 payload 를 다시 평탄화하지 않는다.** 업무 계약을
       검증기 모양에 맞추는 것이 아니라 검증기가 업무 계약의 실제 주소를 읽게 만든다.
       그래서 이 검사는 *"ORPHAN 말고 다른 finding 은 없다"* 를 잠근다 —
       `E-PLAN-EMPTY` 나 `E-EVIDENCE-MISSING` 이 새로 생기면 여기서 걸린다.
    """
    request, reply, meta = _pre_sales_reply()

    findings = validate_reply(request, reply, meta)
    코드 = {finding.code for finding in findings}

    assert 코드 <= {"E-EVIDENCE-ORPHAN"}, f"중첩 주소 말고 다른 finding 이 있다: {findings}"
    고아 = {finding.where for finding in findings}
    assert all("sellable_supply." in 곳 or "delivery_feasibility." in 곳 for 곳 in 고아), (
        "중첩 주소가 아닌 근거가 고아가 됐다"
    )


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

    assert "sellable_supply.inventory_by_item[배추].available_qty_kg" in claims
    assert "sellable_supply.lot_constraints[LOT-A].available_qty_kg" in claims
    assert "sellable_supply.lot_constraints[LOT-B].remaining_freshness_days" in claims
    assert "sellable_supply.lot_constraints[LOT-A].effective_freshness_limit_days" in claims
    # 🔴 **정확히 그 숫자를 가리킨다.** 조상 블록(`delivery_feasibility`)에 달면 판정
    #    이름에 kg 값이 붙어 근거와 대상의 뜻이 어긋난다.
    assert "delivery_feasibility.daily_outbound_capacity_kg" in claims
    assert "delivery_feasibility" not in claims


def test_정책값_근거는_정책_출처를_가리킨다(wired_sales):
    """출고 여력은 물류가 계산한 값이 아니라 **정책 원값을 옮긴 것**이다."""
    _, reply, _ = _pre_sales_reply()
    주소 = "delivery_feasibility.daily_outbound_capacity_kg"
    evidence = next(e for e in reply.evidences if e.claim == 주소)

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

    # ★ **낸 것은 여기서 뺀다 (WP-4).** `supply_capacity_by_date` ·`delivery_route` ·
    #   `transport_lead_time` 은 이제 실제로 계산한다 — 값을 냈는데 *"모른다"* 로도
    #   적으면 계약이 스스로 모순된다.
    assert reply.missing_data == ("earliest_delivery_date",)
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

    축 = {"earliest_delivery_date": "OUTBOUND_PREP_LEAD_DAYS_UNRESOLVED"}
    uncertainties = reply.payload["delivery_feasibility"]["uncertainties"]

    assert set(축.values()) == set(uncertainties)
    assert set(축) == set(reply.missing_data)
    # ★ 낸 축은 어느 쪽에도 안 적힌다 — 값을 내고 «모른다» 로도 적지 않는다.
    assert reply.payload["sellable_supply"]["uncertainties"] == []


# ── 판매 계약 호환 ──────────────────────────────────────────────


def test_payload_가_판매_계약으로_그대로_읽힌다(wired_sales):
    """🔴 **어댑터는 `app.sales.schemas` 를 import 하지 않는다 — 여기서만 읽는다.**

    런타임에 물류가 판매 스키마에 묶이면 판매가 자기 파일을 고치는 날 물류가 같이
    깨진다. 그렇다고 *"모양을 맞췄다"* 를 말로만 두면 어느 날 조용히 갈린다 —
    마스터가 `Capability` 어휘를 베껴 두고 테스트로만 대조하는 것과 **같은 자리**다
    (`master/envelope.py` `Capability` docstring · `tests/master/test_sales_flow.py`).

    ★ **받는 쪽이 옮길 것이 없다 (WP-4B).** payload 가 판매 계약 모양 **그대로**다 —
      종전에는 숫자 셋을 최상위로 끌어올려 두고 받는 쪽이 제자리로 옮겨야 했다.

    🔴 **아직 한 자리가 열려 있다 — 판매 HANDOFF.**

      ```text
      delivery_route · transport_lead_time · earliest_delivery_date
      → LogisticsDeliveryFeasibility(extra="forbid") 가 아직 이 셋을 안 받는다
      ```

      물류가 이 셋을 빼서 맞추지 않는다 — 값은 실제로 계산한 사실이고, 계약을
      좁히면 판매가 납기를 못 읽는다. 그래서 **어긋난 칸이 정확히 그 셋인지**를
      여기서 잠근다. 판매가 칸을 열면 아래 `대기중` 이 비고 이 검사가 알려 준다.
    """
    from pydantic import ValidationError

    from app.sales.schemas import SalesLogisticsContext

    payload = _pre_sales_reply({"user_request": {"item": "배추"}})[1].payload

    대기중 = {"delivery_route", "transport_lead_time", "earliest_delivery_date"}
    try:
        SalesLogisticsContext.model_validate(payload)
        어긋난칸: set[str] = set()
    except ValidationError as 오류:
        어긋난칸 = {str(e["loc"][-1]) for e in 오류.errors()}
    assert 어긋난칸 == 대기중, (
        f"판매 계약과 어긋난 칸이 달라졌다: {sorted(어긋난칸)}"
        " — 셋 말고 다른 것이 갈렸으면 물류가 계약을 깬 것이다"
    )

    context = SalesLogisticsContext.model_validate(
        {
            **payload,
            "delivery_feasibility": {
                칸: 값
                for 칸, 값 in payload["delivery_feasibility"].items()
                if 칸 not in 대기중
            },
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


# ── 보관한계 부재 Lot (#366) ────────────────────────────────────


def test_보관한계가_없는_Lot_은_신선도를_지어내지_않는다(monkeypatch):
    """🔴 **부재는 `null` 로 나간다 — 0 도 생략도 아니다** (#366).

    Repository 가 보관한계 NULL 을 `remaining_freshness_days=None` 으로 내게 되면서
    이 Lot 이 PRE_SALES 까지 도달한다. 여기서 0 으로 접거나 키를 빼면 받는 쪽이
    *"신선도를 확인했다"* 나 *"물류가 칸을 안 보냈다"* 로 읽는다 — 둘 다 틀렸다.

    ★ **칸은 있고 값이 없다.** `§1.2-10` 이 요구하는 모양이다.
    """
    _with_read(
        monkeypatch,
        _sales_snapshot(
            on_hand_by_lot=[
                _sales_lot("LOT-A", "배추", "1000", 10),
                _sales_lot("LOT-N", "배추", "700", None, limit=None),
                _sales_lot("LOT-C", "무", "300", 7),
            ]
        ),
    )

    _, reply, _ = _pre_sales_reply()
    payload = reply.payload
    row = next(r for r in payload["sellable_supply"]["lot_constraints"] if r["lot_id"] == "LOT-N")

    # 🔴 키는 있고 값은 None 이다 — `is None` 으로 재야 0 치환 뮤턴트가 잡힌다
    assert "remaining_freshness_days" in row
    assert row["remaining_freshness_days"] is None
    assert "effective_freshness_limit_days" in row
    assert row["effective_freshness_limit_days"] is None

    # 나머지 Fact 는 그대로 실린다 — 신선도를 못 냈다고 재고가 사라지지 않는다
    assert row["item"] == "배추"
    assert row["available_qty_kg"] == 700.0
    assert row["status"] == "ACTIVE"
    assert row["grade"] is None

    # 정상 Lot 두 개는 값이 그대로다 (회귀 방어)
    others = {r["lot_id"]: r for r in payload["sellable_supply"]["lot_constraints"]}
    assert others["LOT-A"]["remaining_freshness_days"] == 10
    assert others["LOT-A"]["effective_freshness_limit_days"] == 15
    assert others["LOT-C"]["remaining_freshness_days"] == 7


def test_보관한계가_없는_Lot_도_판매가능량에서_빠지지_않는다(monkeypatch):
    """🔴 **만료 확인(<= 0)과 확인 불가(None)는 다르다.**

    None 을 만료로 접으면 창고에 실물이 있는 재고가 판매가능량에서 조용히 사라진다.
    `build_inventory_by_item` 이 이미 그렇게 적어 두었고(*"0 != null"*), 이 검사는
    그 규율이 PRE_SALES 경계를 건너서도 살아 있는지를 잰다.

    ```text
    배추  (1,000 − 할당 400) + 700(한계 미등록) − 미할당 예약 100 = 1,200
    ```
    """
    _with_read(
        monkeypatch,
        _sales_snapshot(
            on_hand_by_lot=[
                _sales_lot("LOT-A", "배추", "1000", 10),
                _sales_lot("LOT-N", "배추", "700", None, limit=None),
                _sales_lot("LOT-C", "무", "300", 7),
            ]
        ),
    )

    _, reply, _ = _pre_sales_reply()

    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    qty = {
        row["item"]: row["available_qty_kg"]
        for row in reply.payload["sellable_supply"]["inventory_by_item"]
    }
    # 🔴 700 이 빠지면 500 이 된다 — 그 뮤턴트를 이 숫자가 잡는다
    assert qty == {"배추": 1200.0, "무": 300.0}


def test_보관한계_부재는_신선도_근거를_만들지_않는다(monkeypatch):
    """근거는 **값이 있는 칸에만** 붙는다.

    🔴 `null` 에 Evidence 를 달면 *"신선도를 이만큼 확인했다"* 가 되고, 안 달면서
       값을 실으면 `E-EVIDENCE-ORPHAN` 이 된다. 값을 안 싣는 것이 답이라 근거도 없다.
    """
    _with_read(
        monkeypatch,
        _sales_snapshot(
            on_hand_by_lot=[
                _sales_lot("LOT-A", "배추", "1000", 10),
                _sales_lot("LOT-N", "배추", "700", None, limit=None),
            ]
        ),
    )

    _, reply, _ = _pre_sales_reply()
    claims = {ev.claim for ev in reply.evidences}

    assert "sellable_supply.lot_constraints[LOT-N].remaining_freshness_days" not in claims
    assert "sellable_supply.lot_constraints[LOT-N].effective_freshness_limit_days" not in claims
    # 물리 잔량은 여전히 근거가 붙는다 — 그 값은 실제로 냈다
    assert "sellable_supply.lot_constraints[LOT-N].available_qty_kg" in claims
    # 정상 Lot 은 셋 다 그대로 (회귀 방어)
    assert "sellable_supply.lot_constraints[LOT-A].remaining_freshness_days" in claims
    assert "sellable_supply.lot_constraints[LOT-A].effective_freshness_limit_days" in claims


def test_보관한계_부재_Lot_이_판매_계약으로도_읽힌다(monkeypatch):
    """🔴 판매 DTO 가 `null` 신선도를 거부하면 여기가 빨간불이다.

    `LogisticsLotConstraint` 는 두 칸을 `int | None` 으로 열어 두었다 — 그 계약이
    좁아지는 날 물류가 낼 수 있는 사실이 경계에서 막힌다.
    """
    from app.sales.schemas import SalesLogisticsContext

    _with_read(
        monkeypatch,
        _sales_snapshot(on_hand_by_lot=[_sales_lot("LOT-N", "배추", "700", None, limit=None)]),
    )

    payload = _pre_sales_reply()[1].payload
    context = SalesLogisticsContext.model_validate(
        {
            "sellable_supply": {
                **payload["sellable_supply"],
                "inventory_by_item": payload["sellable_supply"]["inventory_by_item"],
                "lot_constraints": payload["sellable_supply"]["lot_constraints"],
            },
        }
    )

    lot = context.sellable_supply.lot_constraints[0]
    assert lot.lot_id == "LOT-N"
    assert lot.remaining_freshness_days is None
    assert lot.effective_freshness_limit_days is None
    assert lot.available_qty_kg == Decimal("700.0")


# ---------------------------------------------------------------------------
# STATUS_QUERY — 운영 Fact (#396)
#
# ★ **조회는 재기만 한다.** 측정치와 임계를 나란히 싣되 **비교하지 않는다** — 비교는
#   Rule 소유이고 그 결과(signal)는 조회에 실리지 않는다. 이 절의 절반이 그 경계다.
#
# ★ 이 절은 `_load_read` 만 갈아 끼우고 **Tool 은 진짜를 돌린다**. 가짜 측정값을 넣으면
#   어댑터가 값을 *나르는지* 아니면 *스스로 세는지* 를 구별할 수 없다.
# ---------------------------------------------------------------------------

#: 실 DB seed (`database/logistics_llm_policy_seed.sql`) 와 같은 선택 정책 2종.
_CAPACITY_TIGHT_RATIO = Decimal("0.90")
_FRESHNESS_PRESSURE_RATIO = Decimal("0.30")


def _fact_policy(**overrides) -> LogisticsPolicy:
    """선택 정책 2종이 **등록된** Policy — 값도 출처도 seed 와 같은 모양이다."""
    base = _policy()
    update: dict = {
        "capacity_tight_ratio": _CAPACITY_TIGHT_RATIO,
        "freshness_pressure_ratio": _FRESHNESS_PRESSURE_RATIO,
        "source_refs": {
            **base.source_refs,
            "capacity_tight_ratio": "MVP-DECISION-20260830:LLM-CAPACITY-TIGHT",
            "freshness_pressure_ratio": "MVP-DECISION-20260830:LLM-FRESHNESS-PRESSURE",
        },
    }
    return base.model_copy(update={**update, **overrides})


def _fact_snapshot(**overrides) -> InventoryLogisticsSnapshot:
    """신선도 네 갈래를 **한 스냅샷에** 담는다 — 갈래가 섞여도 서로 안 침범해야 한다.

    ```text
    LOT-OK    ACTIVE  잔여 12 / 한계 15  → 비율 0.8   비율 계산 대상
    LOT-RISK  ACTIVE  잔여  3 / 한계 15  → 비율 0.2   임계 0.30 이하
    LOT-NONE  ACTIVE  잔여 None          → 미확인 (0 이 아니다)
    LOT-GONE  ACTIVE  잔여 -2            → 만료 확인
    LOT-HOLD  HOLD    잔여  1            → 모집단 밖 — ACTIVE 가 아니다
    ```

    🔴 **`lot_count` 는 5 이고 신선도 모집단은 4 다.** 두 수가 다른 것이 정상이며,
       그 사실 자체를 아래 테스트가 고정한다.
    """
    base: dict = {
        "on_hand_by_lot": [
            _sales_lot("LOT-OK", "배추", "100", 12),
            _sales_lot("LOT-RISK", "배추", "100", 3),
            _sales_lot("LOT-NONE", "배추", "100", None, limit=None),
            _sales_lot("LOT-GONE", "배추", "100", -2),
            _sales_lot("LOT-HOLD", "배추", "100", 1, status="HOLD"),
        ],
        "capacity_tight_ratio": _CAPACITY_TIGHT_RATIO,
        "freshness_pressure_ratio": _FRESHNESS_PRESSURE_RATIO,
    }
    return _snapshot(**{**base, **overrides})


def _status_reply(monkeypatch, snapshot=None, policy=None):
    """운영 Fact 를 실은 STATUS_QUERY 회신 — 요청까지 함께 돌려준다 (봉투 검증용)."""
    monkeypatch.setattr(
        adapter,
        "_load_read",
        lambda *, as_of, sim_run_id: _read(snapshot or _fact_snapshot(), policy or _fact_policy()),
    )
    request = req(mode="STATUS_QUERY")
    reply, meta = adapter.logistics_port(request)
    return request, reply, meta


# -- 1. 정상 정책 + 계산 가능 -------------------------------------


def test_운영_Fact_일곱을_측정값과_임계로_함께_싣는다(monkeypatch):
    """★ 값이 **나란히** 있어야 사람이 스스로 판단한다.

    사용률만 싣고 임계를 빼면 `0.125` 가 큰 건지 작은 건지 알 수 없다 —
    `warehouse_free_kg` 옆에 `guaranteed_capacity_kg` 를 두는 것과 같은 이유다.
    """
    _, reply, _ = _status_reply(monkeypatch)

    assert reply.payload["capacity_window_usage_ratio"] == 0.125  # 1 − (7,000 ÷ 8,000)
    assert reply.payload["capacity_tight_ratio"] == 0.9
    assert reply.payload["freshness_min_remaining_ratio"] == 0.2  # LOT-RISK 3/15
    assert reply.payload["freshness_pressure_ratio"] == 0.3
    assert reply.payload["freshness_risk_lot_count"] == 1  # 0.2 <= 0.30
    assert reply.payload["freshness_unresolved_lot_count"] == 1  # LOT-NONE
    assert reply.payload["freshness_expired_lot_count"] == 1  # LOT-GONE
    assert reply.runtime_status == "READY"


def test_신선도_모집단은_ACTIVE_라_lot_count_와_다르다(monkeypatch):
    """🔴 **합이 안 맞는 것이 정상이다.**

    `lot_count` 는 창고에 남아 있는 Lot 전부이고(비-ACTIVE 도 공간을 차지한다),
    신선도 넷은 `ACTIVE` 만 본다. 이것을 같게 만들려고 어느 한쪽을 고치면
    *"격리 재고가 사라지거나"* *"격리 재고의 신선도가 압박 신호에 섞이거나"* 둘 중
    하나가 된다.
    """
    _, reply, _ = _status_reply(monkeypatch)

    assert reply.payload["lot_count"] == 5  # HOLD 포함
    셈한_ACTIVE = (
        reply.payload["freshness_unresolved_lot_count"]
        + reply.payload["freshness_expired_lot_count"]
        + 2  # 비율을 셈한 LOT-OK · LOT-RISK
    )
    assert 셈한_ACTIVE == 4  # HOLD 는 빠진다


def test_조회는_측정하고_판정하지_않는다(monkeypatch):
    """🔴 **이 절에서 가장 중요한 검사** — Fact 를 늘리면서 판정기가 되지 않았는가.

    사용률·비율을 실으면서 signal 이나 verdict 가 따라 나오면 조회의 계약
    (`judgment_fields=()` · "위험 여부는 사람이 본다")이 무너진다.
    """
    _, reply, _ = _status_reply(monkeypatch)

    assert reply.judgment_fields == ()
    assert reply.business_status == "ok"
    assert reply.suggested_adjustments == ()
    assert reply.needs_followup is False
    # 업무 위험 signal 은 조회 payload 어디에도 없다 — 중첩 안까지 훑는다
    assert BUSINESS_SIGNALS & _all_keys(reply.payload) == set()
    실린_문자열 = {value for value in reply.payload.values() if isinstance(value, str)}
    assert BUSINESS_SIGNALS & 실린_문자열 == set()
    assert "soft_warnings" not in reply.payload


def test_임계를_넘어도_조회는_같은_모양으로_답한다(monkeypatch):
    """★ 사용률 0.95 ≥ 임계 0.90 — 그래도 **달라지는 것은 숫자 하나뿐**이다.

    임계를 넘는 날 signal 이 붙거나 business_status 가 바뀌면, 그것이 곧 조회가
    판정을 시작했다는 뜻이다.
    """
    _, 낮음, _ = _status_reply(monkeypatch)
    _, 높음, _ = _status_reply(monkeypatch, snapshot=_fact_snapshot(used_capacity_kg=Decimal(7600)))

    assert 높음.payload["capacity_window_usage_ratio"] == 0.95  # 1 − (400 ÷ 8,000)
    assert 높음.payload["capacity_tight_ratio"] == 0.9
    assert 높음.business_status == 낮음.business_status == "ok"
    assert 높음.judgment_fields == ()
    assert set(높음.payload) == set(낮음.payload)  # 키 구성이 임계에 따라 흔들리지 않는다


# -- 2. 정책 미등재 -----------------------------------------------


def test_임계_정책이_없으면_값을_지어내지_않고_이름을_밝힌다(monkeypatch):
    """🔴 *"기준이 없어 못 쟀다"* 와 *"재 봤더니 안전하다"* 는 다르다.

    임계가 없을 때 위험 Lot 수를 `0` 으로 채우면 **정책 미등재가 안전 신호로
    둔갑한다.** 반대로 조용히 빼면 마스터가 무엇을 달라고 할지 모른다 (§1.2-10).
    """
    _, reply, _ = _status_reply(
        monkeypatch,
        snapshot=_fact_snapshot(capacity_tight_ratio=None, freshness_pressure_ratio=None),
        policy=_policy(),  # 선택 정책 2종이 없는 기본 Policy
    )

    for key in ("capacity_tight_ratio", "freshness_pressure_ratio", "freshness_risk_lot_count"):
        assert key not in reply.payload, key
        assert key in reply.missing_data, key

    # 임계와 무관한 측정치는 그대로 답한다 — 못 한 것만 못 했다고 적는다
    assert reply.payload["capacity_window_usage_ratio"] == 0.125
    assert reply.payload["freshness_min_remaining_ratio"] == 0.2
    assert reply.payload["freshness_unresolved_lot_count"] == 1
    assert reply.payload["freshness_expired_lot_count"] == 1
    assert reply.runtime_status == "READY"  # 선택 정책 부재가 조회를 막지 않는다


def test_정책_출처가_없으면_값은_쓰되_출처_부재를_밝힌다(monkeypatch):
    """★ `guaranteed_capacity_kg@policy_source_ref` 와 같은 자리다.

    값이 아니라 **출처**의 문제라 READY 는 유지한다.
    """
    policy = _fact_policy(source_refs=_policy().source_refs)  # 선택 정책 2종의 출처만 없다

    _, reply, _ = _status_reply(monkeypatch, policy=policy)

    assert reply.payload["capacity_tight_ratio"] == 0.9
    assert "capacity_tight_ratio@policy_source_ref" in reply.missing_data
    assert "freshness_pressure_ratio@policy_source_ref" in reply.missing_data
    assert reply.runtime_status == "READY"


def test_창을_못_세우면_사용률_대신_이름을_남긴다(monkeypatch):
    """★ 조회에는 `hard_constraints` 칸이 없다 — 여기서 안 밝히면 사실이 사라진다.

    `PRE_PURCHASE` 는 같은 사실을 `LOG-H05`(리드타임 미확정)로 나르지만 조회는 그
    채널이 없어 `missing_data` 가 유일한 자리다.
    """
    _, reply, _ = _status_reply(monkeypatch, snapshot=_fact_snapshot(inbound_lead_days=None))

    assert "capacity_window_usage_ratio" not in reply.payload
    assert "capacity_window_usage_ratio" in reply.missing_data
    assert reply.runtime_status == "READY"


# -- 3. 신선도 갈래 -----------------------------------------------


def test_잔여_미확인_Lot_은_0_이_아니라_미확인으로_센다(monkeypatch):
    """🔴 `None` 을 `0` 으로 읽으면 **한계를 모르는 재고가 만료로 둔갑한다.**"""
    snapshot = _fact_snapshot(on_hand_by_lot=[_sales_lot("LOT-N", "배추", "100", None, limit=None)])

    _, reply, _ = _status_reply(monkeypatch, snapshot=snapshot)

    assert reply.payload["freshness_unresolved_lot_count"] == 1
    assert reply.payload["freshness_expired_lot_count"] == 0
    assert reply.payload["freshness_risk_lot_count"] == 0  # 셈할 Lot 이 없다 — 임계는 있다
    # 비율을 셈할 Lot 이 없으므로 최솟값은 **없는 것**이지 0 이 아니다
    assert "freshness_min_remaining_ratio" not in reply.payload
    assert "freshness_min_remaining_ratio" in reply.missing_data


def test_잔여가_0_이하인_Lot_은_만료로_따로_센다(monkeypatch):
    """★ 종전에는 이 Lot 이 비율에서도 미확인에서도 **조용히 빠졌다** (#396 이 연 자리).

    `collect_freshness_pressure_inputs` 가 `continue` 로 건너뛰던 갈래라, 만료 재고가
    있어도 어느 수에도 안 잡혔다.
    """
    snapshot = _fact_snapshot(
        on_hand_by_lot=[
            _sales_lot("LOT-GONE", "배추", "100", 0),  # 0 도 만료다 (<= 0)
            _sales_lot("LOT-PAST", "배추", "100", -5),
        ]
    )

    _, reply, _ = _status_reply(monkeypatch, snapshot=snapshot)

    assert reply.payload["freshness_expired_lot_count"] == 2
    assert reply.payload["freshness_unresolved_lot_count"] == 0


def test_만료_건수를_폐기_어휘로_부르지_않는다(monkeypatch):
    """🔴 **폐기는 turnover · disposal 소유이고 사람이 확정한다.**

    어댑터가 `disposal_candidate` 라는 이름을 내면 그 순간 폐기 판정의 주인이 둘이
    되고, 화면은 *"폐기 대상 2건"* 으로 읽는다 — `confirm_disposal` 은 그런 근거로
    돌지 않는다.
    """
    _, reply, meta = _status_reply(monkeypatch)

    금지 = {"disposal_candidate", "disposal_candidate_count", "disposal_target_lot_count"}
    assert 금지 & _all_keys(reply.payload) == set()
    # 어휘뿐 아니라 **경로**도 막는다 — 회전 모듈은 조회가 부르지 않는다
    assert "load_lot_turnover" not in meta.used_tools
    근거 = next(e for e in reply.evidences if e.claim == "freshness_expired_lot_count")
    assert "폐기 판정도 폐기 대상 수도 아니" in 근거.evidence_detail


# -- 4. 근거 · 봉투 -----------------------------------------------


def test_새_숫자_Fact_에는_전부_근거가_붙는다(monkeypatch):
    """🔴 근거 없는 최상위 숫자는 **LLM 이 만든 값과 구분되지 않는다** (§1.2-5).

    봉투가 `E-EVIDENCE-MISSING` 으로 잡지만, 무엇이 요구되는지를 여기서 한 번 더
    이름으로 고정한다 — 검사만 믿으면 키를 늘릴 때 이유를 잊는다.
    """
    request, reply, meta = _status_reply(monkeypatch)

    새_Fact = {
        "capacity_window_usage_ratio",
        "capacity_tight_ratio",
        "freshness_min_remaining_ratio",
        "freshness_pressure_ratio",
        "freshness_risk_lot_count",
        "freshness_unresolved_lot_count",
        "freshness_expired_lot_count",
    }
    assert 새_Fact <= set(reply.payload)
    assert 새_Fact <= {e.claim for e in reply.evidences}
    assert validate_reply(request, reply, meta) == ()


def test_정책_원값과_측정값의_근거_등급을_가른다(monkeypatch):
    """★ 읽는 사람이 *"잰 값"* 과 *"기준"* 을 구별해야 한다.

    임계는 시뮬레이션 검증용 PROVISIONAL 값이라 그 사실이 근거에 남아야 하고
    (`SIM_FIXED`), 측정값은 Tool 산출이라 `tool_calc` 다.
    """
    _, reply, _ = _status_reply(monkeypatch)
    근거 = {e.claim: e for e in reply.evidences}

    for claim in ("capacity_tight_ratio", "freshness_pressure_ratio"):
        assert 근거[claim].evidence_grade == "SIM_FIXED", claim
        assert "PROVISIONAL" in 근거[claim].evidence_detail, claim

    for claim in (
        "capacity_window_usage_ratio",
        "freshness_min_remaining_ratio",
        "freshness_risk_lot_count",
        "freshness_unresolved_lot_count",
        "freshness_expired_lot_count",
    ):
        assert 근거[claim].source == "tool_calc", claim


def test_신선도_비율_근거는_분모의_출처도_가리킨다(monkeypatch):
    """★ 분모(유효 보관한계)는 품목 정책에서 왔다 — Lot 참조만 달면 출처가 반쪽이다."""
    _, reply, _ = _status_reply(monkeypatch)

    근거 = next(e for e in reply.evidences if e.claim == "freshness_min_remaining_ratio")
    assert "DB:inventory_lots/sim_run_id=SIM-1" in 근거.ref_ids
    assert "DB:item_storage_policies" in 근거.ref_ids


def test_건수는_int_로_나간다(monkeypatch):
    """🔴 `inbound_lead_days` 가 `2.0` 으로 새어 나간 경로를 되풀이하지 않는다 (#221).

    `_num()`(= `float()`) 을 건수에 태우면 `1` 이 `1.0` 이 된다. 값 비교로는 안 잡히므로
    **타입을 직접 잰다.**
    """
    _, reply, _ = _status_reply(monkeypatch)

    for key in (
        "freshness_risk_lot_count",
        "freshness_unresolved_lot_count",
        "freshness_expired_lot_count",
    ):
        value = reply.payload[key]
        assert isinstance(value, int), key
        assert not isinstance(value, bool), key


def test_측정_Tool_을_실행_계획에_정직하게_적는다(monkeypatch):
    """안 돈 것을 돈 것처럼 적지 않는다 — 그 반대도 마찬가지다."""
    _, _, meta = _status_reply(monkeypatch)

    assert meta.used_tools == (
        "build_lot_constraints",
        "calculate_window_capacity_usage",
        "measure_freshness_facts",
    )
    assert meta.tool_order == (1, 2, 3)


def test_조회는_여전히_LLM_을_타지_않는다(monkeypatch):
    """#396 은 Fact 확장이다 — opt-in 을 켜도 조회에는 LLM 경로가 없다."""
    provider = _Provider()
    _inject(monkeypatch, _llm(provider))

    _, reply, meta = _status_reply(monkeypatch)

    assert provider.calls == 0
    assert meta.llm_status == "DISABLED"
    assert "interpretation" not in reply.payload


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — LLM Gate Golden Case (#399)
#
# ★ **이 절은 안전망이다.** Gate 를 넓히지도 좁히지도 않고, 지금 무엇이 불리고 무엇이
#   Template 으로 남는지를 **도달 가능한 조합 전수**로 고정한다.
#
# ★ **`needs_llm` 단위 테스트와 겹치지 않는다.** 저쪽(`tests/llm/`)은 손으로 만든
#   Context 로 게이트 산수를 재고, 여기서는 **실 스냅샷과 실 매입 제안**으로 그 signal
#   조합이 애초에 성립하는지까지 잰다 — 도달 불가능한 조합을 고정하면 안전망이 아니라
#   허구다.
#
# ★ Provider 는 전부 가짜다. 실 Gemini · Ollama · HTTP 는 한 번도 열리지 않는다.
# ---------------------------------------------------------------------------

#: 선택 정책 2종 — 실 DB seed (`database/logistics_llm_policy_seed.sql`) 와 같은 값이다.
_GOLDEN_TIGHT_RATIO = Decimal("0.90")
_GOLDEN_FRESHNESS_RATIO = Decimal("0.30")

#: 창고 점유. 8,000 보장 기준 7,200 이면 사용률이 정확히 0.90 이라 **경계 포함**으로 선다.
_GOLDEN_TIGHT_USED_KG = Decimal(7200)
_GOLDEN_ROOMY_USED_KG = Decimal(1000)

#: 신선도 잔여 비율. 한계 15 일 기준 3/15 = 0.20 (임계 이하) · 12/15 = 0.80 (정상).
_GOLDEN_RISK_FRESHNESS_DAYS = 3
_GOLDEN_SAFE_FRESHNESS_DAYS = 12
_GOLDEN_FRESHNESS_LIMIT_DAYS = 15


def _golden_snapshot(
    *, capacity_tight: bool, freshness_pressure: bool, **overrides
) -> InventoryLogisticsSnapshot:
    """signal 두 개를 **입력으로** 켜고 끈다 — 판정은 Rule 이 한다.

    ```text
    capacity_tight      used_capacity_kg 로 사용률을 임계 위/아래로 옮긴다
    freshness_pressure  Lot 의 잔여 신선도로 비율을 임계 이하/위로 옮긴다
    ```

    ★ 두 축이 서로 간섭하지 않는다. Lot 100kg 은 `used_capacity_kg` 안에 들어가므로
      (`tools._initial_occupancy_by_item` 의 미귀속 버킷) 신선도를 바꿔도 사용률은
      그대로다. 조합을 독립적으로 켤 수 있는 근거가 이것이다.
    """
    lot = InventoryLotSnapshot(
        lot_id="LOT-GOLDEN",
        item="배추",
        available_qty_kg=Decimal(100),
        remaining_freshness_days=(
            _GOLDEN_RISK_FRESHNESS_DAYS if freshness_pressure else _GOLDEN_SAFE_FRESHNESS_DAYS
        ),
        effective_freshness_limit_days=_GOLDEN_FRESHNESS_LIMIT_DAYS,
        status="ACTIVE",
    )
    return _snapshot(
        on_hand_by_lot=[lot],
        used_capacity_kg=_GOLDEN_TIGHT_USED_KG if capacity_tight else _GOLDEN_ROOMY_USED_KG,
        capacity_tight_ratio=_GOLDEN_TIGHT_RATIO,
        freshness_pressure_ratio=_GOLDEN_FRESHNESS_RATIO,
        **overrides,
    )


def _golden_payload(*, adjustment_required: bool) -> dict:
    """`SCENARIO_ADJUSTMENT_REQUIRED` 를 켜고 끈다.

    ★ 20,000kg 은 어느 여유에서도 `conditional` 이고(기존 `_signal_payload`),
      500kg 은 가장 빡빡한 경우(여유 800kg)에도 `ok` 다.
      제안 크기를 여유에 맞춰 고른 것이라, 조정 signal 이 사용률과 얽히지 않는다.
    """
    if adjustment_required:
        return _signal_payload()
    payload = _proposal_payload()
    scenario = payload["scenarios"][0]
    scenario["total_qty_kg"] = 500
    scenario["total_amount_krw"] = 825000
    scenario["split_plan"] = [{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 500}]
    scenario["sourcing_plan"] = [
        {"market": "가락", "grade": "상", "qty_kg": 500, "grade_unit_price": 1650}
    ]
    return payload


def _golden_run(monkeypatch, *, capacity_tight, freshness_pressure, adjustment_required, **kwargs):
    """Golden Case 한 건 실행 — 요청·회신·메타·Provider 를 함께 돌려준다.

    ★ `_load_read` 만 갈아 끼우고 **Tool · Rule · Scenario Engine 은 진짜를 돌린다.**
      signal 을 손으로 넣으면 "그 조합이 성립하는가" 를 못 재고 게이트 산수만 남는다.
    """
    provider = _Provider(kwargs.pop("behaviour", "echo"))
    _inject(monkeypatch, _llm(provider, enabled=kwargs.pop("enabled", True)))
    snapshot = _golden_snapshot(
        capacity_tight=capacity_tight,
        freshness_pressure=freshness_pressure,
        **kwargs.pop("snapshot_overrides", {}),
    )
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read(snapshot))
    monkeypatch.setattr(adapter, "build_lot_constraints", real_build_lot_constraints)
    request = req(
        mode="SCENARIO_VALIDATION",
        payload=_golden_payload(adjustment_required=adjustment_required),
    )
    reply, meta = adapter.logistics_port(request)
    return request, reply, meta, provider


#: 도달 가능한 signal 부분집합 8개와 **현재** Gate 결과.
#:
#: 🔴 이 표는 기대를 적은 것이 아니라 **실측을 고정한 것이다.** 값을 바꾸려면 먼저
#:    production Gate 가 바뀌어야 하고, 그것은 이 이슈의 범위 밖이다 (#399 금지 목록).
#:
#: ★ `FRESHNESS_QUALITY_RISK` 는 여기 없다 — SALES 사이클 signal 이라
#:   `evaluate_sales_business_signals` 만 낸다. SCENARIO_VALIDATION 은 PROCUREMENT
#:   사이클이므로 이 표에 넣으면 **성립하지 않는 상태를 고정하는** 테스트가 된다.
_GOLDEN_GATE_CASES = [
    pytest.param((False, False, False), set(), "SKIPPED_TEMPLATE", 0, id="signals_none__SKIPPED"),
    pytest.param((True, False, False), {CAPACITY_TIGHT}, "SKIPPED_TEMPLATE", 0, id="CT__SKIPPED"),
    pytest.param(
        (False, True, False), {INVENTORY_FRESHNESS_PRESSURE}, "SUCCESS", 1, id="IFP__CALL"
    ),
    pytest.param(
        (False, False, True), {SCENARIO_ADJUSTMENT_REQUIRED}, "SUCCESS", 1, id="SAR__CALL"
    ),
    pytest.param(
        (True, True, False),
        {CAPACITY_TIGHT, INVENTORY_FRESHNESS_PRESSURE},
        "SUCCESS",
        1,
        id="CT_IFP__CALL",
    ),
    pytest.param(
        (True, False, True),
        {CAPACITY_TIGHT, SCENARIO_ADJUSTMENT_REQUIRED},
        "SUCCESS",
        1,
        id="CT_SAR__CALL",
    ),
    pytest.param(
        (False, True, True),
        {INVENTORY_FRESHNESS_PRESSURE, SCENARIO_ADJUSTMENT_REQUIRED},
        "SUCCESS",
        1,
        id="IFP_SAR__CALL",
    ),
    pytest.param(
        (True, True, True),
        {CAPACITY_TIGHT, INVENTORY_FRESHNESS_PRESSURE, SCENARIO_ADJUSTMENT_REQUIRED},
        "SUCCESS",
        1,
        id="CT_IFP_SAR__CALL",
    ),
]


@pytest.mark.parametrize(
    ("inputs", "expected_signals", "expected_status", "expected_calls"), _GOLDEN_GATE_CASES
)
def test_golden_gate(monkeypatch, inputs, expected_signals, expected_status, expected_calls):
    """🔴 **이 절의 중심.** 도달 가능한 8조합의 signal 성립과 Gate 결과를 함께 고정한다.

    두 가지를 한 번에 재는 것이 요점이다.

    ```text
    signal 이 실제로 서는가   실 스냅샷·실 제안으로 만든 조합인지
    그래서 불렸는가           Provider 호출 횟수가 곧 답이다
    ```

    앞을 안 재면 도달 불가능한 조합을 고정할 수 있고, 뒤를 안 재면 게이트가 아니라
    signal 판정만 재게 된다.
    """
    capacity_tight, freshness_pressure, adjustment_required = inputs
    request, reply, meta, provider = _golden_run(
        monkeypatch,
        capacity_tight=capacity_tight,
        freshness_pressure=freshness_pressure,
        adjustment_required=adjustment_required,
    )

    assert reply.runtime_status == "READY"
    assert set(_business_signals(reply)) == expected_signals
    assert meta.llm_status == expected_status
    assert provider.calls == expected_calls
    assert meta.llm_attempts == expected_calls
    # 🔴 하드 제약 FAIL 은 정상 운전에서 성립하지 않는다 — 아래 차단 절이 그 유일한
    #    도달 경로(기준일 불일치)를 따로 고정한다.
    assert [c for c in reply.payload["hard_constraints"] if c["status"] == "FAIL"] == []
    assert validate_reply(request, reply, meta) == ()


@pytest.mark.parametrize(
    ("inputs", "expected_signals", "expected_status", "expected_calls"), _GOLDEN_GATE_CASES
)
def test_golden_gate_는_deterministic_결과를_바꾸지_않는다(
    monkeypatch, inputs, expected_signals, expected_status, expected_calls
):
    """★ 8조합 **각각에서** LLM 을 끈 회신과 켠 회신의 업무 결과가 같다.

    기존 결정론 보존 테스트는 signal 하나(조정 필요)에서 다섯 상태를 비교한다.
    여기서는 반대 축으로 넓힌다 — signal 조합을 전수로 돌면서 **켬/끔 두 상태**를 비교해,
    Context 가 가장 두꺼운 조합(신선도 fact 둘 + 사용률 + 시나리오)에서도 결정론 결과가
    흔들리지 않는지 본다.

    ★ 비교 helper 를 새로 만들지 않는다 — `_business_view` · `_trace_view` 가 이미
      "LLM 이 건드리면 안 되는 것" 의 정본이다. 범위를 좁혀 통과시키는 일이 없도록
      그대로 쓴다.
    """
    del expected_signals, expected_status, expected_calls
    capacity_tight, freshness_pressure, adjustment_required = inputs
    kwargs = {
        "capacity_tight": capacity_tight,
        "freshness_pressure": freshness_pressure,
        "adjustment_required": adjustment_required,
    }

    _, 켬_reply, 켬_meta, _ = _golden_run(monkeypatch, **kwargs)
    _, 끔_reply, 끔_meta, 끔_provider = _golden_run(monkeypatch, enabled=False, **kwargs)

    assert 끔_provider.calls == 0
    assert 끔_meta.llm_status == "DISABLED"
    assert _business_view(켬_reply) == _business_view(끔_reply)
    assert _trace_view(켬_meta) == _trace_view(끔_meta)


def test_composite_분기는_도달_가능한_조합_어디서도_판정을_바꾸지_않는다(monkeypatch):
    """★ `_COMPOSITE_SIGNALS` 휴면을 **주석이 아니라 실행으로** 고정한다.

    비워도 8조합의 게이트 결과가 전부 같으면, 그 분기는 어느 조합에서도 판정을 내리지
    않는다는 뜻이다 — `{CAPACITY_TIGHT, INVENTORY_FRESHNESS_PRESSURE}` 조합조차
    질적 signal 분기가 먼저 잡는다.

    🔴 **휴면이라고 production 코드를 지우지 않는다.** 단독 호출 대상이 아닌 업무 위험이
      추가되는 날 살아나는 확장 자리이고, 이 이슈는 Gate 를 건드리지 않는다 (#399).
      여기서 하는 일은 *"오늘은 안 쓰인다"* 를 사실로 남기는 것뿐이다.
    """
    조합 = [
        set(),
        {CAPACITY_TIGHT},
        {INVENTORY_FRESHNESS_PRESSURE},
        {SCENARIO_ADJUSTMENT_REQUIRED},
        {CAPACITY_TIGHT, INVENTORY_FRESHNESS_PRESSURE},
        {CAPACITY_TIGHT, SCENARIO_ADJUSTMENT_REQUIRED},
        {INVENTORY_FRESHNESS_PRESSURE, SCENARIO_ADJUSTMENT_REQUIRED},
        {CAPACITY_TIGHT, INVENTORY_FRESHNESS_PRESSURE, SCENARIO_ADJUSTMENT_REQUIRED},
    ]

    def gate(signals):
        context = SanitizedLLMContext(signals=sorted(signals), facts=[], allowed_adjustments=[])
        return needs_llm(context, runtime_ready=True, has_blocking_constraints=False)

    원본 = [gate(signals) for signals in 조합]
    monkeypatch.setattr(llm_runtime, "_COMPOSITE_SIGNALS", frozenset())
    비운_뒤 = [gate(signals) for signals in 조합]

    assert 비운_뒤 == 원본
    # 그리고 그 결과는 "질적 signal 이 하나라도 있는가" 와 같다 — CAPACITY_TIGHT 만
    # 단독으로 남는 조합이 유일한 SKIP 이다.
    assert 원본 == [bool(signals - {CAPACITY_TIGHT}) for signals in 조합]


def test_SALES_signal_은_SCENARIO_VALIDATION_에서_성립하지_않는다(monkeypatch):
    """★ `FRESHNESS_QUALITY_RISK` 를 Golden Case 에 넣지 않은 **이유를 고정한다.**

    같은 신선도 비율에서 PROCUREMENT 는 `INVENTORY_FRESHNESS_PRESSURE` 를 내고
    SALES 만 `FRESHNESS_QUALITY_RISK` 를 낸다 (`rules.evaluate_*_business_signals`).
    SCENARIO_VALIDATION 은 PROCUREMENT 사이클이므로 그 signal 은 여기 올 수 없다.

    억지 fixture 로 만들면 성립하지 않는 상태를 고정하게 된다 — 안전망이 아니라 허구다.
    """
    _, reply, _, _ = _golden_run(
        monkeypatch, capacity_tight=False, freshness_pressure=True, adjustment_required=False
    )

    signals = set(_business_signals(reply))
    assert INVENTORY_FRESHNESS_PRESSURE in signals
    assert FRESHNESS_QUALITY_RISK not in signals
    assert FRESHNESS_QUALITY_RISK not in _all_keys(reply.payload)


# ── Gate 차단 조건 — Provider 호출 0건 ──────────────────────────


def test_차단_LLM_이_꺼져_있으면_부르지_않는다(monkeypatch):
    """설정 부재 = 꺼짐. 질적 signal 이 서 있어도 호출은 0건이다."""
    _, reply, meta, provider = _golden_run(
        monkeypatch,
        capacity_tight=False,
        freshness_pressure=True,
        adjustment_required=True,
        enabled=False,
    )

    assert INVENTORY_FRESHNESS_PRESSURE in _business_signals(reply)
    assert provider.calls == 0
    assert meta.llm_status == "DISABLED"
    assert meta.llm_attempts == 0


def test_차단_runtime_이_준비되지_않으면_부르지_않는다(monkeypatch):
    """★ 확정 출고 일정 미조회 하나로 `calculation_ready` 가 꺼진다.

    🔴 이 경로가 **하드 제약 FAIL 없이** runtime 만 막는 유일한 자리다 — 아래
      기준일 불일치 테스트와 짝이고, 둘을 나눠 두어야 어느 조건이 막았는지 알 수 있다.
    """
    _, reply, meta, provider = _golden_run(
        monkeypatch,
        capacity_tight=False,
        freshness_pressure=True,
        adjustment_required=False,
        snapshot_overrides={"confirmed_outbound_schedule": None},
    )

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert [c for c in reply.payload["hard_constraints"] if c["status"] == "FAIL"] == []
    assert INVENTORY_FRESHNESS_PRESSURE in _business_signals(reply)
    assert provider.calls == 0
    assert meta.llm_status == "SKIPPED_TEMPLATE"


def test_차단_하드_제약_FAIL_은_기준일_불일치로만_성립한다(monkeypatch):
    """🔴 **FAIL 을 단독으로 만들 수 없다는 사실 자체가 계약이다.**

    PROCUREMENT 하드 제약은 `_known_constraint` 가 만들고 그것은 `PASS` 아니면
    `UNRESOLVED` 다 (`rules.py`). FAIL 이 나오는 자리는 `_snapshot_boundary` 둘뿐인데,
    스냅샷 부재는 어댑터가 그 앞에서 `_not_ready` 로 접고, 남는 것은 기준일 불일치다.
    그리고 그 경로는 `runtime_status` 도 함께 내린다.

    ★ 그래서 어댑터 층에서는 두 차단 조건을 **분리할 수 없다.** 분리 검증은
      `tests/llm/test_logistics_interpretation_runtime.py::test_fail_blocks_the_call_even_with_signals`
      가 `needs_llm` 층에서 이미 한다 — 여기서 같은 것을 다시 만들지 않는다.
    """
    _, reply, meta, provider = _golden_run(
        monkeypatch,
        capacity_tight=False,
        freshness_pressure=True,
        adjustment_required=False,
        snapshot_overrides={"as_of": date(2025, 12, 30)},
    )

    실패 = [c["code"] for c in reply.payload["hard_constraints"] if c["status"] == "FAIL"]
    assert 실패 == ["AS_OF_MISMATCH"]
    assert reply.runtime_status == "RUNTIME_NOT_READY"  # 둘이 함께 선다
    assert provider.calls == 0
    assert meta.llm_status == "SKIPPED_TEMPLATE"


class _RecordingService(InterpretationService):
    """`interpret()` 이 받은 게이트 인자를 기록한다. 판정은 실물 그대로 돈다."""

    def __init__(self, settings, provider):
        super().__init__(settings, provider)
        self.gate_args: list[dict] = []

    def interpret(self, context, **kwargs):
        self.gate_args.append(dict(kwargs))
        return super().interpret(context, **kwargs)


@pytest.mark.parametrize(
    ("snapshot_overrides", "expected"),
    [
        pytest.param({}, False, id="정상__blocking_False"),
        pytest.param({"as_of": date(2025, 12, 30)}, True, id="기준일_불일치__blocking_True"),
    ],
)
def test_어댑터가_넘기는_게이트_인자를_직접_고정한다(monkeypatch, snapshot_overrides, expected):
    """🔴 **이 인자는 결과로 관측되지 않는다 — 그래서 인자를 직접 잰다.**

    어댑터가 `has_blocking_constraints` 를 통째로 `False` 로 바꿔도 회신은 하나도 달라지지
    않는다. 유일하게 FAIL 이 서는 경로(기준일 불일치)가 `runtime_ready=False` 도 함께
    내려서 게이트가 그쪽에서 먼저 닫히기 때문이다.

    즉 이 배선은 **지워도 아무 테스트가 안 깨지는 자리**였다. 결과가 못 보는 것을
    인자로 본다 — `_TOOL_INPUTS` 계약을 실행이 아니라 선언으로 잠그는 것과 같은 태도다.
    """
    provider = _Provider()
    service = _RecordingService(_llm(provider).settings, provider)
    monkeypatch.setattr(adapter, "master_interpretation_service", lambda: service)
    snapshot = _golden_snapshot(capacity_tight=False, freshness_pressure=True, **snapshot_overrides)
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read(snapshot))
    monkeypatch.setattr(adapter, "build_lot_constraints", real_build_lot_constraints)
    request = req(mode="SCENARIO_VALIDATION", payload=_golden_payload(adjustment_required=False))
    adapter.logistics_port(request)

    assert len(service.gate_args) == 1
    assert service.gate_args[0]["has_blocking_constraints"] is expected
    # 짝이 되는 두 인자도 함께 고정한다 — 셋이 한 자리에서 정해진다
    assert service.gate_args[0]["runtime_ready"] is not expected
    assert service.gate_args[0]["facts_incomplete"] is False


def test_차단_facts_조립이_불완전하면_부르지_않는다(monkeypatch):
    """★ fact 상한 초과·조립 실패는 조용한 절단이 아니라 **미호출**이다.

    signal 은 섰는데 판정 수치가 전달되지 않은 상태는 Rule to Service 배선이 깨진
    것이라, 확인된 fact 없이 해석시키지 않고 무숫자 Template 으로 남긴다.
    """
    real_builder = adapter.build_sanitized_context

    def incomplete_builder(**kwargs):
        context, _ = real_builder(**kwargs)
        return context, True

    monkeypatch.setattr(adapter, "build_sanitized_context", incomplete_builder)
    _, reply, meta, provider = _golden_run(
        monkeypatch,
        capacity_tight=False,
        freshness_pressure=True,
        adjustment_required=True,
    )

    assert reply.runtime_status == "READY"
    assert _business_signals(reply)  # signal 은 그대로 선다
    assert provider.calls == 0
    assert meta.llm_status == "SKIPPED_TEMPLATE"


# ── Context leakage · 정보 증분 진단 ────────────────────────────


def test_가장_두꺼운_Context_에서도_원본_업무값이_새지_않는다(monkeypatch):
    """★ 기존 leakage 테스트는 signal 하나(조정 필요)의 Context 를 본다.

    여기서는 **세 signal 이 다 선 조합**을 본다. 신선도 fact 둘은 Lot 에서 나오고
    사용률 fact 는 창고 점유에서 나오므로, 그쪽으로 lot_id 나 kg 이 새는지는 이 조합에서만
    드러난다.
    """
    _, _, _, provider = _golden_run(
        monkeypatch, capacity_tight=True, freshness_pressure=True, adjustment_required=True
    )

    assert len(provider.contexts) == 1
    context = provider.contexts[0]
    assert isinstance(context, SanitizedLLMContext)
    # 세 signal 의 fact 넷 — 판정에 실제 쓰인 수치의 확정 표기뿐이다
    assert [fact.fact_id for fact in context.facts] == [
        "capacity_window_usage",
        "freshness_risk_lot_count",
        "freshness_min_remaining_ratio",
        "scenario_conditional_count",
    ]
    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)
    금지 = (
        "LOT-GOLDEN",  # lot_id
        AS_OF.isoformat(),  # 원본 날짜
        SIM_RUN_ID,  # 실행 축
        "REQ-T",  # request_id
        "7200",  # 원본 창고 점유 kg
        "20000",  # 원본 제안 수량 kg
        "33000000",  # 원본 금액
        "1650",  # 단가
        "배추",  # 품목
    )
    for token in 금지:
        assert token not in serialized, token


def test_복수_signal_이_해석까지_하나도_빠지지_않고_간다(monkeypatch):
    """★ 다중 signal 진단 — **배선**을 잰다.

    Rule 이 낸 signal 셋이 Context 를 지나 `payload["interpretation"].risks` 까지
    그대로 도착하는지를 본다. 중간에서 하나가 떨어지면 사람이 보는 위험 목록이
    결정론 판정보다 짧아진다.

    ⚠️ 이것은 **모델 품질 점수가 아니다.** 출력이 signal 집합과 일치해야 한다는 것은
      검증기(`SIGNAL_MISSING` · `UNSUPPORTED_RISK`)가 이미 강제하므로 여기서 같은
      규칙을 다시 만들지 않는다 — 여기서 새로 재는 것은 어댑터 배선이다.
    """
    _, reply, _, provider = _golden_run(
        monkeypatch, capacity_tight=True, freshness_pressure=True, adjustment_required=True
    )

    context = provider.contexts[0]
    assert set(context.signals) == {
        CAPACITY_TIGHT,
        INVENTORY_FRESHNESS_PRESSURE,
        SCENARIO_ADJUSTMENT_REQUIRED,
    }
    assert reply.payload["interpretation"]["risks"] == list(context.signals)
    assert set(_business_signals(reply)) == set(context.signals)


def test_summary_는_fact_표기를_인용해_Template_이_못_내는_문장을_낼_수_있다(monkeypatch):
    """★ 정보 증분 **가능성** 진단 — 품질 점수가 아니다.

    Template 은 계약상 무숫자다 (`_TEMPLATE_SIGNAL_PHRASES`). 그래서 "측정값을 인용한
    문장" 은 Template 이 구조적으로 못 내는 유일한 종류이고, 그것이 현재 스키마에서
    가능한 정보 증분의 상한이다. 그 상한이 어댑터 경로 끝까지 살아 있는지 본다.

    ⚠️ **모델이 실제로 인용하는지는 재지 않는다.** 여기 Provider 는 가짜라 그것을 재면
      가짜를 재는 것이 된다. 검증기 층의 인용 허용·거부는
      `tests/llm/test_logistics_context_facts.py` 가 이미 고정한다.
    """

    class _CitingProvider(_Provider):
        """허용된 fact 표기를 그대로 인용하는 가짜 Provider."""

        def generate(self, context, *, retry_guidance=None):
            self.contexts.append(context)
            self.guidance.append(retry_guidance)
            인용 = context.facts[0].display_value
            return ProviderResult(
                text=json.dumps(
                    {
                        "summary": f"판정 창 최대 창고 사용률은 {인용} 입니다.",
                        "risks": list(context.signals),
                        "suggested_adjustment": context.preferred_adjustment,
                    },
                    ensure_ascii=False,
                )
            )

    provider = _CitingProvider()
    _inject(monkeypatch, _llm(provider))
    snapshot = _golden_snapshot(capacity_tight=True, freshness_pressure=False)
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read(snapshot))
    monkeypatch.setattr(adapter, "build_lot_constraints", real_build_lot_constraints)
    request = req(mode="SCENARIO_VALIDATION", payload=_golden_payload(adjustment_required=True))
    reply, meta = adapter.logistics_port(request)

    assert meta.llm_status == "SUCCESS"  # 인용은 검증기를 통과한다
    인용 = provider.contexts[0].facts[0].display_value
    assert 인용 in reply.payload["interpretation"]["summary"]
    # Template 은 같은 자리에서 숫자를 내지 못한다 — 그것이 증분의 정체다
    템플릿 = build_template_interpretation(provider.contexts[0])
    assert not any(ch.isdigit() for ch in 템플릿.summary)
    assert validate_reply(request, reply, meta) == ()


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — Master-facing LLM 실행 관측 (#402)
#
# ★ 결정론 관측(`inventory_dept_meta`)과 **책임을 나눈다.** 저쪽은 LLM 상태와 무관하게
#   같아야 하고(#399 가 잠근다), 이쪽은 상태에 따라 달라지는 것이 정상이다.
# ★ 공통 `ExecutionMetadata` 를 넓히지 않았다 — 기존 `observations` 확장 채널만 쓴다.
#   재무가 `finance_llm_provider` 를 싣는 자리와 같고, Critic 은 `<dept>_dept_meta` 가
#   아닌 관측을 조용히 건너뛴다 (`critic_bridge._dept_meta_in`).
# ---------------------------------------------------------------------------

#: 관측에 허용된 key 전부. **정확히 이 일곱이다** — `<=` 로 재면 필드가 하나 더 새도
#: 통과한다. `display_value` 한 줄이 늘어나는 것이 곧 누출이므로 `==` 로 잠근다.
#:
#: ★ #406 에서 다섯 → 일곱이 됐다. **느슨하게 푸는 것이 아니라 새 정확한 집합으로
#:   다시 잠그는 것이다** — 늘어난 둘은 검증된 usage 숫자뿐이고, Provider 원본 필드명
#:   (`promptTokenCount` · `prompt_eval_count`)이나 raw 응답은 여전히 들어올 수 없다.
_LLM_TRACE_KEYS = {
    "observation_type",
    "provider",
    "error_kind",
    "provider_elapsed_ms",
    "observed_input_tokens",
    "observed_output_tokens",
    "context_fact_ids",
}


def _llm_trace(meta) -> dict | None:
    """ExecutionMetadata 의 observations 에서 LLM 실행 관측 하나를 꺼낸다."""
    found = [
        json.loads(item)
        for item in meta.observations
        if json.loads(item).get("observation_type") == adapter._LLM_TRACE_OBSERVATION
    ]
    return found[0] if found else None


def _observation_types(meta) -> list[str]:
    return [json.loads(item).get("observation_type") for item in meta.observations]


def test_SUCCESS_는_LLM_실행_관측을_남긴다(monkeypatch, stocked):
    provider = _Provider()
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    assert provider.calls == 1
    trace = _llm_trace(meta)
    assert trace is not None
    assert set(trace) == _LLM_TRACE_KEYS
    assert trace["provider"] == "fake"
    assert trace["error_kind"] is None, "성공으로 끝나면 최종 실패 원인이 없다"
    assert isinstance(trace["provider_elapsed_ms"], int)
    assert trace["provider_elapsed_ms"] >= 0
    assert trace["context_fact_ids"] == ["scenario_conditional_count"]
    # 결정론 관측은 그대로 남는다 — 새 관측이 기존 것을 밀어내지 않는다
    assert _dept_meta(meta) is not None
    assert validate_reply(request, reply, meta) == ()


def test_FALLBACK_timeout_은_최종_실패_원인을_관측에_남긴다(monkeypatch, stocked):
    """🔴 `llm_error_kind` 는 `_meta()` 경계에서 통째로 유실되던 값이다 (#402 M3)."""
    provider = _Provider("timeout")
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    trace = _llm_trace(meta)
    assert trace is not None
    assert trace["error_kind"] == "TIMEOUT"
    assert isinstance(trace["provider_elapsed_ms"], int)
    # 업무 결과는 그대로다 — 관측은 판정이 아니다
    assert reply.runtime_status == "READY"
    assert reply.business_status == "conditional"
    assert validate_reply(request, reply, meta) == ()


def test_FALLBACK_검증탈락은_VALIDATION_FAILED_를_남긴다(monkeypatch, stocked):
    provider = _Provider("invalid")
    _inject(monkeypatch, _llm(provider))
    reply, meta = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_signal_payload()))

    assert provider.calls == 2  # correction 한 번 뒤 접는다
    trace = _llm_trace(meta)
    assert trace is not None
    assert trace["error_kind"] == "VALIDATION_FAILED"
    assert reply.runtime_status == "READY"


@pytest.mark.parametrize(
    ("enabled", "builder_incomplete", "expected_status"),
    [
        pytest.param(False, False, "DISABLED", id="DISABLED"),
        pytest.param(True, True, "SKIPPED_TEMPLATE", id="SKIPPED_TEMPLATE"),
    ],
)
def test_부르지_않은_실행에는_LLM_관측을_남기지_않는다(
    monkeypatch, stocked, enabled, builder_incomplete, expected_status
):
    """🔴 **관측이 있다 ⇔ Provider 를 불렀다.**

    미호출 실행에 provider 이름을 적으면 *"이 Provider 를 썼다"* 로 읽힌다. 그 사실은
    이미 `llm_status` + `llm_attempts=0` 이 정확히 말하므로 관측은 중복이자 오독의
    씨앗이다. 결정론 관측(`inventory_dept_meta`)은 영향받지 않는다.
    """
    real_builder = adapter.build_sanitized_context
    if builder_incomplete:
        monkeypatch.setattr(
            adapter,
            "build_sanitized_context",
            lambda **kwargs: (real_builder(**kwargs)[0], True),
        )
    provider = _Provider()
    _inject(monkeypatch, _llm(provider, enabled=enabled))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    assert meta.llm_status == expected_status
    assert meta.llm_attempts == 0
    assert provider.calls == 0
    assert _llm_trace(meta) is None
    assert _observation_types(meta) == ["inventory_dept_meta"]
    assert validate_reply(request, reply, meta) == ()


def test_LLM_경로가_없는_mode_는_관측도_없다(wired, stocked):
    """PRE_PURCHASE · STATUS_QUERY 는 `_meta` 에 llm 을 주지 않는다."""
    for request in (req(), req(mode="STATUS_QUERY")):
        _, meta = adapter.logistics_port(request)
        assert _llm_trace(meta) is None, request.mode


def test_LLM_관측은_공통_봉투_필드를_늘리지_않는다(monkeypatch, stocked):
    """🔴 `elapsed_ms` 는 **Agent 전체 실행시간**이다 (재무가 채운다).

    LLM latency 를 그 칸에 넣으면 한 컬럼에 비교 불가능한 두 값이 섞인다. 물류는
    그 칸을 채우지 않고, Provider latency 는 관측으로만 나른다.
    """
    _inject(monkeypatch, _llm(_Provider()))
    _, meta = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_signal_payload()))

    assert meta.llm_status == "SUCCESS"
    assert meta.elapsed_ms == 0, "물류는 이 칸을 채우지 않는다"
    assert not hasattr(meta, "llm_provider_elapsed_ms")
    assert _llm_trace(meta)["provider_elapsed_ms"] is not None


def test_LLM_관측에는_원본_업무값이_없다(monkeypatch):
    """★ #399 의 가장 두꺼운 Context(3 signal · fact 4개)를 그대로 재사용한다.

    Provider 전송 경계에서 막은 값이 **실행이력으로 우회해 나가는지**를 본다 — 관측은
    마스터 run history 와 화면까지 원문 그대로 실리므로 Context 보다 더 엄하게 본다.
    """
    _, _, meta, provider = _golden_run(
        monkeypatch, capacity_tight=True, freshness_pressure=True, adjustment_required=True
    )

    trace = _llm_trace(meta)
    assert trace is not None
    assert set(trace) == _LLM_TRACE_KEYS
    # fact 는 **id 만** 간다 — label · display_value 는 오지 않는다
    assert trace["context_fact_ids"] == [
        "capacity_window_usage",
        "freshness_risk_lot_count",
        "freshness_min_remaining_ratio",
        "scenario_conditional_count",
    ]
    assert all(isinstance(fact_id, str) for fact_id in trace["context_fact_ids"])
    assert [fact.fact_id for fact in provider.contexts[0].facts] == trace["context_fact_ids"]

    serialized = json.dumps(trace, ensure_ascii=False)
    금지 = (
        "LOT-GOLDEN",  # lot_id
        AS_OF.isoformat(),  # 원본 날짜
        SIM_RUN_ID,  # 실행 축
        "REQ-T",  # request_id
        "7200",  # 원본 창고 점유 kg
        "20000",  # 원본 제안 수량 kg
        "33000000",  # 원본 금액
        "1650",  # 단가
        "배추",  # 품목
        "임계",  # display_value 의 관계 표기
        "%",  # display_value 의 비율 단위
        "개",  # display_value 의 건수 단위
        "label",
        "display_value",
    )
    for token in 금지:
        assert token not in serialized, token


def test_결정론_비교에서_제외되는_것은_LLM_관측_하나뿐이다(monkeypatch, stocked):
    """🔴 **안전핀** — `_trace_view` 가 관측 비교를 통째로 잃는 변이를 잡는다 (#402 M6).

    LLM trace 를 제외하되 `inventory_dept_meta` 는 **반드시 남아야** 한다. 제외 필터가
    넓어지거나 observations 자체가 빠지면 결정론 관측 drift 가 무검사가 된다 —
    그것이 #399 가 세운 안전망의 핵심이다.
    """
    _inject(monkeypatch, _llm(_Provider()))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    _, meta = adapter.logistics_port(request)

    assert _observation_types(meta) == ["inventory_dept_meta", "inventory_llm_trace"]
    남은_것 = [json.loads(item) for item in _trace_view(meta)["observations"]]
    assert [o["observation_type"] for o in 남은_것] == ["inventory_dept_meta"]
    assert 남은_것, "관측 비교가 통째로 비면 DeptMeta drift 를 아무도 못 본다"


def test_관측_이름을_문자열로_베끼지_않는다():
    """이름이 바뀌는 날 `_trace_view` 의 제외 필터만 조용히 빗나가면 안 된다."""
    assert adapter._LLM_TRACE_OBSERVATION == "inventory_llm_trace"


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — Provider token usage 를 실행 관측으로 나른다 (#406)
#
# ★ 새 observation 을 만들지 않았다. `inventory_llm_trace` 는 이미 *"실제로 일어난
#   Provider 호출 하나의 실행 관측"* 이고 토큰 사용량은 정확히 그 축의 사실이다.
#   이름을 하나 더 만들면 `_trace_view` 의 "한 이름만 제외" fail-closed 설계가 무너진다.
# ---------------------------------------------------------------------------

#: 🔴 input · output 에 서로 다른 값 (뒤바꿈 변이 방어 · #406 M1·M2).
_TRACE_USAGE = ProviderUsage(input_tokens=137, output_tokens=24)


def test_SUCCESS_는_관측한_토큰_사용량을_남긴다(monkeypatch, stocked):
    provider = _Provider(usage=_TRACE_USAGE)
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    trace = _llm_trace(meta)
    assert trace is not None
    assert set(trace) == _LLM_TRACE_KEYS, "key 집합은 정확히 일곱이다"
    assert trace["observed_input_tokens"] == 137
    assert trace["observed_output_tokens"] == 24
    # 기존 #402 값은 그대로 산다 — 새 필드가 옆자리를 밀어내지 않는다
    assert trace["provider"] == "fake"
    assert trace["error_kind"] is None
    assert isinstance(trace["provider_elapsed_ms"], int)
    assert trace["context_fact_ids"] == ["scenario_conditional_count"]
    assert validate_reply(request, reply, meta) == ()


def test_usage_를_보고하지_않은_SUCCESS_는_key_는_두되_null_이다(monkeypatch, stocked):
    """🔴 **키를 빼지 않는다.** 관측의 key 집합이 실행마다 달라지면 읽는 쪽이
    *"필드가 없다"* 와 *"값이 없다"* 를 구별하지 못하고, `_LLM_TRACE_KEYS` 의 `==`
    잠금도 성립하지 않는다. 그리고 `0` 으로 채우지도 않는다 — 미관측과 실제 0 은
    다른 사실이다 (#406 M3).
    """
    provider = _Provider()  # usage 기본값 None — 보고하지 않는 Provider 응답
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    trace = _llm_trace(meta)
    assert set(trace) == _LLM_TRACE_KEYS
    assert trace["observed_input_tokens"] is None
    assert trace["observed_output_tokens"] is None
    assert meta.llm_status == "SUCCESS", "usage 부재는 업무 결과를 바꾸지 않는다"
    assert validate_reply(request, reply, meta) == ()


def test_실제_0_은_0_으로_남는다(monkeypatch, stocked):
    provider = _Provider(usage=ProviderUsage(input_tokens=0, output_tokens=0))
    _inject(monkeypatch, _llm(provider))
    _, meta = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_signal_payload()))

    trace = _llm_trace(meta)
    assert trace["observed_input_tokens"] == 0
    assert trace["observed_output_tokens"] == 0
    assert trace["observed_input_tokens"] is not None


def test_FALLBACK_도_앞선_호출에서_관측한_사용량을_보존한다(monkeypatch, stocked):
    """검증 탈락으로 끝난 실행이야말로 *"토큰을 얼마나 쓰고 실패했나"* 가 필요하다.

    `_Provider("invalid")` 는 두 호출 모두 usage 를 보고하고 두 번 다 검증에 떨어진다 —
    두 호출분이 합산되어야 한다 (137·24 의 두 배).
    """
    provider = _Provider("invalid", usage=_TRACE_USAGE)
    _inject(monkeypatch, _llm(provider))
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())
    reply, meta = adapter.logistics_port(request)

    assert provider.calls == 2
    trace = _llm_trace(meta)
    assert trace["error_kind"] == "VALIDATION_FAILED"
    assert trace["observed_input_tokens"] == 274
    assert trace["observed_output_tokens"] == 48
    # 업무 결과는 그대로다 — 관측은 판정이 아니다
    assert reply.runtime_status == "READY"
    assert reply.business_status == "conditional"
    assert validate_reply(request, reply, meta) == ()


def test_timeout_FALLBACK_은_토큰을_지어내지_않는다(monkeypatch, stocked):
    """응답 본문을 못 받은 호출의 사용량은 **모르는 값**이다 — `0` 으로 채우지 않는다."""
    provider = _Provider("timeout", usage=_TRACE_USAGE)
    _inject(monkeypatch, _llm(provider))
    _, meta = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_signal_payload()))

    trace = _llm_trace(meta)
    assert trace["error_kind"] == "TIMEOUT"
    assert trace["observed_input_tokens"] is None
    assert trace["observed_output_tokens"] is None


@pytest.mark.parametrize(
    ("enabled", "builder_incomplete"),
    [
        pytest.param(False, False, id="DISABLED"),
        pytest.param(True, True, id="SKIPPED_TEMPLATE"),
    ],
)
def test_미호출_실행에는_usage_를_이유로_관측을_만들지_않는다(
    monkeypatch, stocked, enabled, builder_incomplete
):
    """#402 계약 그대로 — **관측이 있다 ⇔ Provider 를 불렀다.**

    usage 를 적을 자리가 생겼다고 부르지 않은 실행에 관측을 만들면, 그 실행이 이
    Provider 를 *"썼다"* 로 읽힌다.
    """
    real_builder = adapter.build_sanitized_context
    if builder_incomplete:
        monkeypatch.setattr(
            adapter,
            "build_sanitized_context",
            lambda **kwargs: (real_builder(**kwargs)[0], True),
        )
    provider = _Provider(usage=_TRACE_USAGE)
    _inject(monkeypatch, _llm(provider, enabled=enabled))
    _, meta = adapter.logistics_port(req(mode="SCENARIO_VALIDATION", payload=_signal_payload()))

    assert provider.calls == 0
    assert _llm_trace(meta) is None
    assert _observation_types(meta) == ["inventory_dept_meta"]


def test_usage_관측에는_Provider_원본_필드명이_오지_않는다(monkeypatch):
    """🔴 #399 의 가장 두꺼운 Context 에 usage 까지 실어 **누출 검사를 넓힌다** (M7).

    숫자 둘만 나르고 그 숫자가 어디서 왔는지는 나르지 않는다. 원본 필드명이 실행이력에
    남으면 다음 사람이 *"그럼 옆 필드도 실을 수 있겠네"* 로 읽고, 그 옆에는 raw 응답이
    있다.
    """
    provider = _Provider(usage=_TRACE_USAGE)
    _inject(monkeypatch, _llm(provider))
    snapshot = _golden_snapshot(capacity_tight=True, freshness_pressure=True)
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: _read(snapshot))
    monkeypatch.setattr(adapter, "build_lot_constraints", real_build_lot_constraints)
    _, meta = adapter.logistics_port(
        req(mode="SCENARIO_VALIDATION", payload=_golden_payload(adjustment_required=True))
    )

    trace = _llm_trace(meta)
    assert set(trace) == _LLM_TRACE_KEYS
    serialized = json.dumps(trace, ensure_ascii=False)
    금지 = (
        "usageMetadata",  # Gemini 응답 봉투
        "promptTokenCount",
        "candidatesTokenCount",
        "totalTokenCount",
        "prompt_eval_count",  # Ollama 응답 필드
        "eval_count",
        "total_duration",
        "message",  # raw 응답의 본문 자리
        "content",
        "candidates",
    )
    for token in 금지:
        assert token not in serialized, token
    # 값은 스칼라와 문자열 리스트뿐이다 — dict 가 실리면 raw 응답이 들어온 것이다
    assert all(value is None or isinstance(value, (str, int, list)) for value in trace.values())


def test_usage_는_결정론_결과를_바꾸지_않는다(monkeypatch, stocked):
    """usage 를 보고하는 Provider 와 보고하지 않는 Provider 의 업무 결과가 같다.

    ★ #399 의 켬/끔 비교와 같은 축이다 — 여기서는 **LLM 을 켠 두 상태** 사이를 본다.
      `_trace_view` 가 LLM 관측을 빼므로 실행 흔적도 같아야 한다.
    """
    request = req(mode="SCENARIO_VALIDATION", payload=_signal_payload())

    _inject(monkeypatch, _llm(_Provider(usage=_TRACE_USAGE)))
    보고_reply, 보고_meta = adapter.logistics_port(request)
    _inject(monkeypatch, _llm(_Provider()))
    무보고_reply, 무보고_meta = adapter.logistics_port(request)

    assert _business_view(보고_reply) == _business_view(무보고_reply)
    assert _trace_view(보고_meta) == _trace_view(무보고_meta)
    # 그런데 관측에서는 갈린다 — 그것이 이 필드의 존재 이유다
    assert _llm_trace(보고_meta)["observed_input_tokens"] == 137
    assert _llm_trace(무보고_meta)["observed_input_tokens"] is None
