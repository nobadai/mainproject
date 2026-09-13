"""재고·물류 AI Agent Core MVP (#628) — **외부 계약 동결** (Commit 1).

★ 목적은 검사를 늘리는 것이 아니라 **Agent 를 붙이기 전에 지금 실제로 소비되는 경계를
  잠그는 것**이다. 여기 적힌 값은 전부 `origin/dev`(`fd94216`) Production 이 **지금** 내는
  것이고, 소비자 코드(매입 `draft_plan` · 판매 `SalesLogisticsContext` · 마스터 봉투)가
  **지금** 읽는 그대로다. 설계 문서(`docs/logistics-agent-mvp/19`)가 아니라 Production
  과 소비자를 먼저 보고 적었다.

🔴 **이 파일은 회귀 검증 수단이지 Production 설계의 정답이 아니다.**

   Agent 구현 뒤 이 파일이 실패하면 **먼저** 판정한다:

   ```text
   A. 실제 외부 계약이 깨졌는가        → Production 을 고친다
   B. 검사가 내부 구현까지 잠갔는가     → 검사를 고친다
   C. 계약이 의도적으로 바뀐 것인가     → 소비자와 합의한 뒤 검사를 갱신한다
   D. 기존 검사가 낡은 것인가          → 검사를 고친다
   ```

   Production 코드와 실제 소비자의 계약이 유지되고 있다면 **검사가 수정 대상**일 수 있다.
   검사 실패만을 근거로 Production 코드를 바꾸지 않는다.

★ **외부 계약은 얼리고 내부 구현은 얼리지 않는다.** helper 분리·통합, 모듈 재배치, 계산
  pipeline 정리, Agent 용 read model 추가는 이 파일을 건드리지 않고 할 수 있어야 한다.
  그래서 Tool 이름·호출 순서·내부 상수·payload 의 **호환 가능한 키 추가**는 잠그지 않는다.
  잠그는 것은 소비자가 실제로 읽는 키·타입·값의 업무 뜻, 그리고 `extra="forbid"` 처럼
  소비자가 **추가를 거부**하는 자리뿐이다.

동결 범위:

```text
A. 4 모드 공통   mode 집합 · 봉투 바인딩 · runtime_status · 봉투 검증 · 전선 직렬화 · sim_run_id 축
B. PRE_PURCHASE  소비자가 읽는 키 · 숫자 타입 · cap_by_date 뜻 · 매입 문 앞 검사 통과
C. PRE_SALES     최상위 7키 (extra="forbid") · 중첩 키 · 판매 모델 파싱 · 키 추가 거부 · 원가 기준
D. SCENARIO_VALIDATION  소비자가 읽는 키 · verdict 어휘 · 판정 선언 · 도착일 · 기준일 불일치
E. STATUS_QUERY  상태만 싣고 경계는 안 싣는다 · 모집단 차이 · int 유지
F. 뜻            lots[].available_qty_kg = 물리 잔량 (status 무관) ≠ inventory_by_item = 가용 정본
G. observed_at   칸의 타입과 기본값 · 현재 기준선(None)
```

★ DB 를 타지 않는다. `adapter._load_read` 한 곳만 갈아 끼우고 **Tool 은 전부 진짜**를
  돌린다 — 뜻(F)은 Tool 이 만들므로 Tool 을 대역으로 바꾸면 뜻을 재지 못한다.

★ 재료의 숫자(1,000 · 700 · 250 · 300)는 계약이 아니다. **두 값의 뜻이 다르다**는 것을
  증명하려고 어느 Lot 수량도 정답과 같지 않게 고른 것뿐이다.

★ `request_id` · 근거 순서 · 벽시계 · run 식별자 · Tool 이름은 잠그지 않는다 — 계약이
  아니라 실행의 흔적이다.
"""

from __future__ import annotations

import dataclasses
import json
import typing
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

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
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
    agent_allowed_modes,
    validate_reply,
    wire_payload,
)

# ---------------------------------------------------------------------------
# 재료 — 뜻(F)이 갈리도록 고른 수
# ---------------------------------------------------------------------------

AS_OF = date(2026, 1, 15)
SIM_RUN_ID = "SIM-FREEZE-0001"
LEAD_DAYS = 2
GUARANTEED_KG = Decimal(8000)
USED_KG = Decimal(2500)  # Lot 합 2,250 + 품목 미귀속 250

#: 물류 4 모드 — `envelope._AGENT_MODES["inventory"]` 와 같아야 한다.
MODES = ("PRE_PURCHASE", "PRE_SALES", "SCENARIO_VALIDATION", "STATUS_QUERY")

#: 배추 Lot 셋 + 무 Lot 하나. **어느 Lot 수량도 정답(가용 500 · 300)과 같지 않다** —
#: 차감·제외를 빼먹은 회신이 우연히 통과하지 못하게.
#:
#: ```text
#: LOT-ACTIVE   배추 1,000  잔여 10   ACTIVE   할당 400 (이 Lot 지정)
#: LOT-EXPIRED  배추   700  잔여 -3   ACTIVE   ← 신선도 만료 · 가용에서 빠진다
#: LOT-HOLD     배추   250  잔여 12   HOLD     ← 비-ACTIVE · 가용에서 빠진다 (DB CHECK 어휘)
#: LOT-MU       무     300  잔여  7   ACTIVE
#:                                  + 배추 미할당 예약 100 (Lot 미지정)
#:
#: 배추 물리 합 = 1,950        배추 가용 = (1,000 − 400) − 100 = 500       무 가용 = 300
#: ```
_LOTS = [
    InventoryLotSnapshot(
        lot_id="LOT-ACTIVE",
        item="배추",
        available_qty_kg=Decimal(1000),
        received_at=AS_OF - timedelta(days=5),
        unit_cost_krw_per_kg=Decimal(1500),
        remaining_freshness_days=10,
        effective_freshness_limit_days=15,
        status="ACTIVE",
    ),
    InventoryLotSnapshot(
        lot_id="LOT-EXPIRED",
        item="배추",
        available_qty_kg=Decimal(700),
        received_at=AS_OF - timedelta(days=18),
        unit_cost_krw_per_kg=Decimal(1400),
        remaining_freshness_days=-3,
        effective_freshness_limit_days=15,
        status="ACTIVE",
    ),
    InventoryLotSnapshot(
        lot_id="LOT-HOLD",
        item="배추",
        available_qty_kg=Decimal(250),
        received_at=AS_OF - timedelta(days=3),
        unit_cost_krw_per_kg=Decimal(1600),
        remaining_freshness_days=12,
        effective_freshness_limit_days=15,
        status="HOLD",
    ),
    InventoryLotSnapshot(
        lot_id="LOT-MU",
        item="무",
        available_qty_kg=Decimal(300),
        received_at=AS_OF - timedelta(days=7),
        unit_cost_krw_per_kg=Decimal(900),
        remaining_freshness_days=7,
        effective_freshness_limit_days=14,
        status="ACTIVE",
    ),
]

_COMMITMENTS = [
    OutboundCommitment(item="배추", lot_id="LOT-ACTIVE", quantity_kg=Decimal(400)),
    OutboundCommitment(item="배추", lot_id=None, quantity_kg=Decimal(100)),
]

#: 확정 출고 하나 — `cap_by_date` 가 **D+1 부터** 공간을 여는 규칙을 재는 재료.
_OUTBOUND_DATE = AS_OF + timedelta(days=LEAD_DAYS)
_OUTBOUND_KG = Decimal(200)


def _policy() -> LogisticsPolicy:
    return LogisticsPolicy(
        guaranteed_capacity_kg=GUARANTEED_KG,
        burst_capacity_kg=Decimal(9600),
        inbound_lead_days=LEAD_DAYS,
        daily_inbound_capacity_kg=Decimal(5000),
        inbound_transport_capacity_kg=Decimal(5000),
        shared_daily_outbound_capacity_kg=Decimal(5000),
        cap_by_date_policy="CONFIRMED_ONLY",
        outbound_prep_lead_days=1,
        capacity_tight_ratio=Decimal("0.90"),
        freshness_pressure_ratio=Decimal("0.30"),
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
            "outbound_prep_lead_days": "MVP-DECISION-20260909:OUTBOUND-PREP",
            "capacity_tight_ratio": "MVP-DECISION-20260830:LLM-CAPACITY-TIGHT",
            "freshness_pressure_ratio": "MVP-DECISION-20260830:LLM-FRESHNESS",
        },
    )


def _snapshot(**overrides) -> InventoryLogisticsSnapshot:
    base: dict = {
        "snapshot_id": "LOG-SNAP-FREEZE",
        "as_of": AS_OF,
        "on_hand_by_lot": list(_LOTS),
        "item_storage_policies": [
            ItemStoragePolicyFact(
                item="배추", operational_limit_days=15, medium_grade_factor=Decimal("0.8")
            ),
            ItemStoragePolicyFact(
                item="무", operational_limit_days=14, medium_grade_factor=Decimal("0.8")
            ),
        ],
        "in_transit": [],
        "confirmed_inbound_schedule": [],
        "confirmed_outbound_schedule": [
            ScheduledQuantity(date=_OUTBOUND_DATE, quantity_kg=_OUTBOUND_KG, item="무")
        ],
        "outbound_commitments": list(_COMMITMENTS),
        "used_capacity_kg": USED_KG,
        "guaranteed_capacity_kg": GUARANTEED_KG,
        "burst_capacity_kg": Decimal(9600),
        "guaranteed_capacity_by_zone_kg": None,
        "inbound_lead_days": LEAD_DAYS,
        "daily_inbound_capacity_kg": Decimal(5000),
        "inbound_transport_capacity_kg": Decimal(5000),
        "shared_daily_outbound_capacity_kg": Decimal(5000),
        "capacity_tight_ratio": Decimal("0.90"),
        "freshness_pressure_ratio": Decimal("0.30"),
        "evidence_refs": [
            "DB:logistics_runtime_fixture/LOG-RUNTIME-FREEZE",
            f"DB:inventory_lots/sim_run_id={SIM_RUN_ID}",
            "DB:item_storage_policies",
        ],
    }
    return InventoryLogisticsSnapshot(**{**base, **overrides})


def _proposal_payload() -> dict:
    """매입 실물 스키마(`PurchaseProposal`) 모양의 최소 제안 — SCENARIO_VALIDATION 입력."""
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
        "context_docs_used": [],
        "rejected_reasons": [],
    }


#: 모드별 입력 — 마스터가 실제로 싣는 것과 같은 모양이다 (`flow._boundary_input` 은
#: 물류에 빈 payload · `sales_flow._context_input` 은 `user_request` · 매입 판단 ④ 는 제안).
_PAYLOADS: dict[str, dict] = {
    "PRE_PURCHASE": {},
    "PRE_SALES": {"user_request": {"item": "배추", "requested_quantity_kg": 300}},
    "SCENARIO_VALIDATION": _proposal_payload(),
    "STATUS_QUERY": {},
}


def _request(
    mode: str, payload: dict | None = None, *, sim_run_id: str = SIM_RUN_ID
) -> AgentRequest:
    context = ExecutionContext(
        request_id="REQ-FREEZE-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
        sim_run_id=sim_run_id,
    )
    return AgentRequest(
        context=context,
        agent="inventory",
        mode=mode,
        payload=_PAYLOADS[mode] if payload is None else payload,
    )


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    """읽기 seam 하나만 갈아 끼운다. Tool · Rule · Scenario Engine 은 진짜다."""
    monkeypatch.setenv("LOGISTICS_MASTER_LLM_ENABLED", "false")
    read = LogisticsRead(snapshot=_snapshot(), policy=_policy(), delivery_route="LOGI-BASE-5PL")
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: read)


def _call(mode: str, payload: dict | None = None):
    request = _request(mode, payload)
    reply, meta = adapter.logistics_port(request)
    return request, reply, meta


# ===========================================================================
# A. 4 모드 공통
# ===========================================================================


def test_물류_4모드_집합은_봉투가_정한_넷이다():
    """마스터 봉투가 물류에 허용하는 mode 는 넷이다 (`envelope._AGENT_MODES["inventory"]`).

    Agent 를 위해 다섯째 mode 를 열면 여기서 선다. 어댑터 쪽 실행 축 문은 아래
    `test_실행_축이_비면_어느_모드도_읽지_않는다` 가 **행동으로** 잰다 — 상수 이름을 잠그지
    않는다.
    """
    assert agent_allowed_modes("inventory") == frozenset(MODES)


@pytest.mark.parametrize("mode", MODES)
def test_모드마다_READY_회신의_바인딩이_고정이다(mode):
    """봉투 `check_binding` · `E-BIND-RUN-ID` 가 요구하는 것 — 요청과 회신·흔적이 한 줄로 묶인다."""
    request, reply, meta = _call(mode)

    assert reply.runtime_status == "READY"
    assert (reply.request_id, reply.as_of, reply.agent, reply.mode) == (
        request.context.request_id,
        AS_OF,
        "inventory",
        mode,
    )
    assert reply.run_id and meta.run_id == reply.run_id


@pytest.mark.parametrize("mode", MODES)
def test_모드마다_실행_흔적은_마스터가_요구하는_만큼만_잠근다(mode):
    """마스터가 `ExecutionMetadata` 에 실제로 의존하는 것은 셋뿐이다 (`envelope.py`).

    ```text
    E-PLAN-EMPTY        READY 인데 used_tools 가 비면 안 된다 (STATUS_QUERY 면제)
    tool_order 길이      used_tools 와 같아야 한다 (`ExecutionMetadata.__post_init__`)
    llm_status 어휘      LLM_STATUSES 안 (validate_reply 가 잰다)
    ```

    🔴 **Tool 이름과 순서는 잠그지 않는다.** `plan.py` · `service.py` 는 이름을 그대로 적을
       뿐 읽지 않는다 — helper 를 나누거나 합쳐도 외부 결과가 같으면 계약은 그대로다.
    """
    _, reply, meta = _call(mode)
    assert reply.runtime_status == "READY"
    if mode != "STATUS_QUERY":
        assert meta.used_tools, "READY 회신인데 실행 계획을 재현할 Tool 이 없다"
    assert len(meta.tool_order) == len(meta.used_tools)


@pytest.mark.parametrize("mode", MODES)
def test_모드마다_봉투_검증을_통과한다(mode):
    """`validate_reply` 의 finding 은 **알려진 하나**(PRE_SALES 중첩 주소 고아)뿐이다.

    ⚠️ `E-EVIDENCE-ORPHAN` 은 봉투 `_CLAIM_PATH` 가 점 있는 키를 못 읽어 생기는
      **마스터 소유** 어긋남이다 (`test_logistics_adapter.py` L 절이 같은 사실을 적는다).
      여기서 payload 를 평탄화해 초록을 만들지 않는다.
    """
    request, reply, meta = _call(mode)
    findings = validate_reply(request, reply, meta)
    codes = {finding.code for finding in findings}
    if mode == "PRE_SALES":
        assert codes <= {"E-EVIDENCE-ORPHAN"}, findings
    else:
        assert findings == (), findings


@pytest.mark.parametrize("mode", MODES)
def test_모드마다_payload_는_전선에_실을_수_있다(mode):
    """마스터는 `wire_payload` 로 편 뒤 `json.dumps` 로 이력에 적는다 (`flow` · `sales_flow`).

    `Decimal` · `date` · `set` 이 payload 에 섞이면 여기서 죽는다 — 물류는 숫자를
    `float` · `int`, 날짜를 ISO 문자열로 내보낸다.
    """
    _, reply, _ = _call(mode)
    text = json.dumps(wire_payload(dict(reply.payload)))
    assert json.loads(text) == wire_payload(dict(reply.payload))


@pytest.mark.parametrize("mode", MODES)
def test_실행_축이_비면_어느_모드도_읽지_않는다(mode):
    """`sim_run_id` 가 비면 `RUNTIME_NOT_READY` · `missing_data=("sim_run_id",)` 다. 예외도
    `ERROR` 도 아니다 — 값을 못 받은 것은 다시 불러도 같다 (#345)."""
    reply, _ = adapter.logistics_port(_request(mode, sim_run_id=""))
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("sim_run_id",)
    assert reply.missing_capability == ()  # 번역 부재가 아니라 값 부재다


# ===========================================================================
# B. PRE_PURCHASE — 매입 경계
# ===========================================================================

#: READY 회신이 **반드시** 싣는 최상위 키 — 매입(`absorb_inventory` · `free_stock_for` ·
#: `classify_situation` · `self_check` · `package_scenarios` · `allocate_sourcing`) · 마스터
#: (`critic_bridge` · `decision_service._lead_days_of` · `procurement_boundary`)가 읽는 이름이다.
#:
#: ★ **상한을 안 건다.** 매입은 payload 를 dict 째 나르고 모르는 키를 거부하지 않는다 —
#:   호환 키 추가는 계약 위반이 아니다 (PRE_SALES 와 다른 점).
_PRE_PURCHASE_REQUIRED_KEYS = frozenset(
    {
        "warehouse_free_kg",
        "rental_cap_kg",
        "used_capacity_kg",
        "cap_by_date_policy",
        "cap_by_date_window_days",
        "guaranteed_capacity_kg",
        "burst_capacity_kg",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
        "inbound_lead_days",
        "cap_by_date",
        "lots",
        "inventory_by_item",
        "item_storage_policies",
        "policy_version_used",
    }
)
_LOT_KEYS = frozenset(
    {"lot_id", "item", "available_qty_kg", "remaining_freshness_days", "grade", "status"}
)
_INVENTORY_BY_ITEM_KEYS = frozenset({"item", "available_qty_kg"})
_STORAGE_POLICY_KEYS = frozenset({"item", "operational_limit_days", "medium_grade_factor"})


def test_PRE_PURCHASE_소비자가_읽는_키와_판정_선언이_고정이다():
    _, reply, _ = _call("PRE_PURCHASE")

    missing = _PRE_PURCHASE_REQUIRED_KEYS - set(reply.payload)
    assert not missing, f"소비자가 읽는 키가 빠졌다: {sorted(missing)}"
    # 경계 해석 하나만 판정으로 선언한다 — 봉투가 이 이름으로 근거를 요구한다
    assert reply.judgment_fields == ("cap_by_date_policy",)
    assert reply.business_status == "ok"
    assert reply.payload["cap_by_date_policy"] == "CONFIRMED_ONLY"
    assert reply.payload["rental_cap_kg"] == 0.0  # 2026-08-27 물류 회신 §1 — 확정 0


def test_PRE_PURCHASE_숫자_타입이_소비자가_읽는_대로다():
    """`inbound_lead_days` 만 `int` 고 kg 값은 `float` 다 (#221).

    매입 `_arrival_input_problems` 가 `lead != int(lead)` 로 세우고, 마스터
    `critic_bridge._int_of` · `decision_service._lead_days_of` 가 같은 값을 나른다.
    """
    _, reply, _ = _call("PRE_PURCHASE")
    payload = reply.payload

    assert type(payload["inbound_lead_days"]) is int
    assert payload["inbound_lead_days"] == LEAD_DAYS
    assert type(payload["cap_by_date_window_days"]) is int
    # kg 값은 "수량" 이면 된다 — 매입 검사가 int·float·Decimal 을 다 받고 bool 만 막는다.
    # `float` 로 못 박지 않는다 (직렬화 가능 여부는 전선 검사가 따로 잰다).
    for key in (
        "warehouse_free_kg",
        "rental_cap_kg",
        "used_capacity_kg",
        "guaranteed_capacity_kg",
        "burst_capacity_kg",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
    ):
        value = payload[key]
        assert isinstance(value, int | float) and not isinstance(value, bool), key


def test_PRE_PURCHASE_행마다_소비자가_읽는_칸이_있다():
    """`lots[]` 6칸은 매입 `absorb_inventory`(item) · `usable_holdings_kg`(available_qty_kg ·
    remaining_freshness_days) · 등급 배분(grade) 이 읽고, `LOT_REQUIRED_KEYS` 가 문 앞에서 센다.
    `inventory_by_item[]` 은 `free_stock_for` 가 품목으로 **첫 행**을 집으므로 품목당 한 행이어야
    한다.
    """
    _, reply, _ = _call("PRE_PURCHASE")
    payload = reply.payload

    assert payload["lots"], "Lot 이 하나도 안 실렸다"
    assert all(_LOT_KEYS <= set(row) for row in payload["lots"])
    assert all(_INVENTORY_BY_ITEM_KEYS <= set(row) for row in payload["inventory_by_item"])
    assert all(_STORAGE_POLICY_KEYS <= set(row) for row in payload["item_storage_policies"])
    items = [row["item"] for row in payload["inventory_by_item"]]
    assert len(items) == len(set(items)), "품목당 한 행이어야 매입이 첫 행을 집어도 맞다"


def test_PRE_PURCHASE_cap_by_date_는_리드타임부터_18일_창이고_보장치에서_투영_점유를_뺀_값이다():
    """`cap_by_date[d] = guaranteed − 그날 투영 점유`. 확정 출고는 **D+1 부터** 공간을 연다.

    ```text
    창       as_of + lead 부터 payload 가 밝힌 `cap_by_date_window_days` 일 · ISO 문자열 키
    출고일   8,000 − 2,500          = 5,500   (당일은 아직 안 나갔다)
    다음날   8,000 − (2,500 − 200)  = 5,700   (무 200 이 나갔다)
    ```

    매입 `classify_situation` 은 `cap_by_date[as_of + N4]` 를(그래서 창의 **시작일**이 계약이다),
    `self_check` 는 `cap_by_date_window_days` 로 *"이 날짜까지밖에 안 왔다"* 를 읽는다(그래서
    밝힌 길이와 실제 길이가 같아야 한다). 창의 길이 자체(18)는 Tool 상수라 잠그지 않는다.
    """
    _, reply, _ = _call("PRE_PURCHASE")
    cap = reply.payload["cap_by_date"]

    start = AS_OF + timedelta(days=LEAD_DAYS)
    window = range(reply.payload["cap_by_date_window_days"])
    assert list(cap) == [(start + timedelta(days=n)).isoformat() for n in window]
    assert all(isinstance(value, float) for value in cap.values())

    assert cap[_OUTBOUND_DATE.isoformat()] == float(GUARANTEED_KG - USED_KG)
    assert cap[(_OUTBOUND_DATE + timedelta(days=1)).isoformat()] == float(
        GUARANTEED_KG - USED_KG + _OUTBOUND_KG
    )
    # 창고 여유는 **현재** 점유만 뺀 값이라 cap_by_date 와 다른 계산이다
    assert reply.payload["warehouse_free_kg"] == float(GUARANTEED_KG - USED_KG)


def test_PRE_PURCHASE_payload_는_매입이_문_앞에서_거는_검사를_그대로_통과한다():
    """매입 어댑터가 `constraints.inventory` 에 거는 모양 검사 셋과 흡수 함수를 **그대로** 돌린다.

    ★ 소비자 코드를 검사에서 읽는 것은 허용이고 고치는 것은 금지다 — 여기서 재는 것은
      *"물류가 낸 것을 매입이 지금 그대로 읽을 수 있는가"* 하나다.
    """
    from app.purchase_agent import adapter as purchase_adapter
    from app.purchase_agent.nodes.draft_plan import free_stock_for

    _, reply, _ = _call("PRE_PURCHASE")
    payload = dict(reply.payload)

    assert purchase_adapter._capacity_input_problems(payload) == []
    assert purchase_adapter._arrival_input_problems(payload) == []
    assert purchase_adapter._lot_shape_problems(payload["lots"]) == []

    absorbed = purchase_adapter.absorb_inventory(payload, "배추")
    assert {row["lot_id"] for row in absorbed["lots"]} == {"LOT-ACTIVE", "LOT-EXPIRED", "LOT-HOLD"}
    # 매입이 수요에서 빼는 가용재고는 `inventory_by_item` 에서 온다 — Lot 합이 아니다
    assert free_stock_for(absorbed, "배추").kg == 500.0
    assert free_stock_for(absorbed, "무").kg == 300.0


# ===========================================================================
# C. PRE_SALES — 판매 컨텍스트 (extra="forbid")
# ===========================================================================

_PRE_SALES_KEYS = frozenset(
    {
        "query_scope",
        "sellable_supply",
        "delivery_feasibility",
        "hard_constraints",
        "soft_warnings",
        "missing_data",
        "evidence_refs",
    }
)
_SELLABLE_SUPPLY_KEYS = frozenset(
    {
        "status",
        "inventory_by_item",
        "lot_constraints",
        "supply_capacity_by_date",
        "inventory_cost_basis",
        "uncertainties",
    }
)
_DELIVERY_KEYS = frozenset(
    {
        "status",
        "daily_outbound_capacity_kg",
        "delivery_route",
        "transport_lead_time",
        "earliest_delivery_date",
        "reason_codes",
        "uncertainties",
    }
)
_LOT_CONSTRAINT_KEYS = _LOT_KEYS | {"effective_freshness_limit_days"}
_COST_BASIS_KEYS = frozenset(
    {
        "item",
        "quantity_kg",
        "amount_krw",
        "allocation_method",
        "cost_method",
        "included_components",
        "source_ref",
        "source_refs",
        "evidence_grade",
    }
)


def test_PRE_SALES_최상위_7키와_중첩_키가_정확히_고정이다():
    """판매 `SalesLogisticsContext(extra="forbid")` 가 문 앞에서 읽는 그 일곱이다.

    ★ 최상위는 **정확히** 일곱이다 — 선택 키가 없다. `PRE_PURCHASE` 와 달리 부족한
      것도 넘치는 것도 판매 회신을 통째로 세운다.
    """
    _, reply, _ = _call("PRE_SALES")
    payload = reply.payload

    assert set(payload) == _PRE_SALES_KEYS
    assert set(payload["sellable_supply"]) == _SELLABLE_SUPPLY_KEYS
    assert set(payload["delivery_feasibility"]) == _DELIVERY_KEYS
    supply = payload["sellable_supply"]
    assert all(set(row) == _LOT_CONSTRAINT_KEYS for row in supply["lot_constraints"])
    assert all(set(row) == _INVENTORY_BY_ITEM_KEYS for row in supply["inventory_by_item"])
    assert reply.judgment_fields == ()  # 판매 판정을 내지 않는다
    assert reply.business_status == "ok"
    assert payload["hard_constraints"] == []  # 승인 매입 Overlay 없는 자리 — 판정을 지어내지 않는다
    assert payload["sellable_supply"]["status"] == "READY"
    assert payload["query_scope"] == {"as_of": AS_OF.isoformat(), "item": "배추"}


def test_PRE_SALES_payload_는_판매_모델이_그대로_파싱한다():
    """마스터 `sales_flow._proposal_input` 이 payload 를 벗겨 `logistics_context` 로 싣고,
    판매가 `SalesLogisticsContext.model_validate` 로 읽는다. 재조립은 없다."""
    from app.sales.schemas import SalesLogisticsContext

    _, reply, _ = _call("PRE_SALES")
    context = SalesLogisticsContext.model_validate(wire_payload(dict(reply.payload)))

    assert context.sellable_supply is not None
    by_item = {row.item: row.available_qty_kg for row in context.sellable_supply.inventory_by_item}
    assert by_item == {"무": Decimal(300), "배추": Decimal(500)}
    assert context.delivery_feasibility is not None
    assert context.delivery_feasibility.status in {"READY", "UNRESOLVED", "FAIL"}


@pytest.mark.parametrize(
    "where",
    ["top", "sellable_supply", "delivery_feasibility", "lot_constraints", "inventory_by_item"],
)
def test_PRE_SALES_에_키를_하나라도_더하면_판매가_거부한다(where):
    """🔴 Agent 때문에 `PRE_SALES` 에 새 키(예: `priority_sell_requests`)를 넣지 않는다.

    판매 모델 다섯 겹이 전부 `extra="forbid"` 다 — 어느 겹에 넣어도 판매 회신이 통째로
    선다. 설계 §15 가 그 경로를 **기각** 한 근거가 이 검사다.
    """
    from app.sales.schemas import SalesLogisticsContext

    _, reply, _ = _call("PRE_SALES")
    payload = json.loads(json.dumps(wire_payload(dict(reply.payload))))

    if where == "top":
        payload["priority_sell_requests"] = []
    elif where in ("sellable_supply", "delivery_feasibility"):
        payload[where]["agent_note"] = "x"
    else:
        payload["sellable_supply"][where][0]["agent_note"] = "x"

    with pytest.raises(ValidationError):
        SalesLogisticsContext.model_validate(payload)


def test_PRE_SALES_원가_기준은_물은_수량에_FEFO_로_배부한_값이고_없으면_None_이다():
    """재무는 물류를 직접 부르지 않는다 — 판매 후보의 `inventory_cost_basis` 를
    `compose_sales_cost_basis` 로 받고, `None` 이면 `RUNTIME_NOT_READY` 로 멈춘다.

    ```text
    물은 수량 300 ≤ 확정 가용 500  →  FEFO 첫 Lot(LOT-ACTIVE, 단가 1,500) 에서 300kg
                                      = 450,000원 · ACTUAL · SIM_FIXED
    수량을 안 물음                  →  확정 가용 전체(500) 가 대상
    품목을 안 물음                  →  None (0 원이 아니다)
    ```
    """
    _, reply, _ = _call("PRE_SALES")
    basis = reply.payload["sellable_supply"]["inventory_cost_basis"]

    assert basis is not None and set(basis) == _COST_BASIS_KEYS
    assert basis["item"] == "배추"
    assert basis["quantity_kg"] == 300.0
    assert basis["amount_krw"] == 450000.0
    assert basis["allocation_method"] == "FEFO"
    assert basis["cost_method"] == "ACTUAL"
    assert basis["source_refs"] == ["LOT-ACTIVE"]
    assert basis["source_ref"] == "LOT-ACTIVE"

    _, reply_all, _ = _call("PRE_SALES", {"user_request": {"item": "배추"}})
    assert reply_all.payload["sellable_supply"]["inventory_cost_basis"]["quantity_kg"] == 500.0

    _, reply_none, _ = _call("PRE_SALES", {})
    assert reply_none.payload["sellable_supply"]["inventory_cost_basis"] is None
    assert reply_none.payload["query_scope"] == {"as_of": AS_OF.isoformat()}


# ===========================================================================
# D. SCENARIO_VALIDATION — 시나리오 판정
# ===========================================================================

_SCENARIO_REQUIRED_KEYS = frozenset(
    {
        "verdict",
        "expected_arrival_dates",
        "cap_by_date",
        "hard_constraints",
        "soft_warnings",
        "scenario_results",
        "inventory_by_item",
        "interpretation",
    }
)
_SCENARIO_RESULT_KEYS = frozenset({"label", "verdict", "reason_codes", "adjustments"})
_VERDICTS = frozenset({"ok", "conditional", "reject", "skipped"})


def test_SCENARIO_VALIDATION_소비자가_읽는_키와_판정_어휘가_고정이다():
    """마스터 `verdicts` · Critic dept_meta · 2차 시도 `adjustments` · 프론트가 읽는 이름과 어휘.

    ★ 상한을 안 건다 — `preferred_adjustment` 처럼 있을 때만 실리는 키가 이미 있고, 마스터는
      payload 를 dict 째 나른다.
    """
    _, reply, _ = _call("SCENARIO_VALIDATION")

    missing = _SCENARIO_REQUIRED_KEYS - set(reply.payload)
    assert not missing, f"소비자가 읽는 키가 빠졌다: {sorted(missing)}"
    assert reply.payload["verdict"] in _VERDICTS
    # 회신의 업무 상태는 payload 의 verdict 그대로다 — 둘이 갈리면 마스터가 어느 것을 볼지 갈린다
    assert reply.business_status == reply.payload["verdict"]
    assert "verdict" in reply.judgment_fields
    assert all(_SCENARIO_RESULT_KEYS <= set(row) for row in reply.payload["scenario_results"])
    assert {row["verdict"] for row in reply.payload["scenario_results"]} <= _VERDICTS
    # 해석 블록은 결정론 값 옆에 **따로** 앉는다 — LLM 이 꺼져 있어도 칸은 있다
    assert set(reply.payload["interpretation"]) >= {"summary", "risks", "suggested_adjustment"}


def test_SCENARIO_VALIDATION_도착일은_분할_실행일에_리드타임을_더한_날이다():
    _, reply, _ = _call("SCENARIO_VALIDATION")
    arrival = (AS_OF + timedelta(days=LEAD_DAYS)).isoformat()
    assert reply.payload["expected_arrival_dates"] == [arrival]
    assert set(reply.payload["cap_by_date"]) == {arrival}


def test_SCENARIO_VALIDATION_기준일이_다른_제안은_ERROR_로_돌려보낸다():
    """`proposal.meta.as_of != as_of` 는 데이터 부재가 아니라 **호출 오류**다.

    ★ 분할 실행일도 같이 옮긴다 — 매입 스키마가 `meta.as_of` 앞의 실행일을 거부하므로,
      안 옮기면 되살리기 자체가 실패해 `RUNTIME_NOT_READY`(제안 부재)로 떨어진다.
    """
    yesterday = (AS_OF - timedelta(days=1)).isoformat()
    payload = _proposal_payload()
    payload["meta"]["as_of"] = yesterday
    payload["scenarios"][0]["split_plan"][0]["date"] = yesterday
    _, reply, _ = _call("SCENARIO_VALIDATION", payload)

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.payload["validation_errors"] == ["proposal.meta.as_of"]


# ===========================================================================
# E. STATUS_QUERY — 상태만, 경계는 안 싣는다
# ===========================================================================

#: 지금 READY 회신이 싣는 키. 🔴 **부분집합으로 잰다** — 조회 답은 원문 키를 그대로
#: 출력하므로(`master/answer.facts_from_status` · `_LABEL.get(key, key)`) 호환 추가는
#: 계약을 안 깨지만, 아래 키의 **제거·개명·타입 변경**은 깬다.
_STATUS_KEYS_NOW = frozenset(
    {
        "as_of",
        "used_capacity_kg",
        "lot_count",
        "warehouse_free_kg",
        "guaranteed_capacity_kg",
        "min_remaining_freshness_days",
        "min_freshness_lot_id",
        "capacity_window_usage_ratio",
        "capacity_tight_ratio",
        "freshness_unresolved_lot_count",
        "freshness_expired_lot_count",
        "freshness_min_remaining_ratio",
        "freshness_risk_lot_count",
        "freshness_pressure_ratio",
        "policy_version_used",
    }
)
#: 매입 분할 경계 — 조회에는 **없어야** 한다 (`_status_query` docstring).
_STATUS_BOUNDARY_KEYS = frozenset(
    {
        "cap_by_date",
        "cap_by_date_window_days",
        "inbound_lead_days",
        "daily_inbound_capacity_kg",
        "lots",
    }
)


def test_STATUS_QUERY_는_상태_키를_싣고_경계_키는_안_싣는다():
    _, reply, _ = _call("STATUS_QUERY")
    keys = set(reply.payload)

    assert _STATUS_KEYS_NOW <= keys, sorted(_STATUS_KEYS_NOW - keys)
    assert not (keys & _STATUS_BOUNDARY_KEYS), sorted(keys & _STATUS_BOUNDARY_KEYS)
    assert reply.judgment_fields == ()  # 재기만 하고 판정하지 않는다
    assert reply.business_status == "ok"
    assert reply.payload["as_of"] == AS_OF.isoformat()


def test_STATUS_QUERY_건수는_int_고_모집단이_다르다():
    """`lot_count` 는 창고에 남은 Lot **전부**(HOLD 포함)고, 신선도 건수 넷은 **ACTIVE 만**이다.

    ```text
    lot_count                       4   (ACTIVE 3 + HOLD 1)
    freshness_expired_lot_count     1   (LOT-EXPIRED · ACTIVE 인데 잔여 -3)
    freshness_unresolved_lot_count  0
    min_remaining_freshness_days   -3   (전 Lot 중 최솟값 · 0 으로 접지 않는다)
    ```
    """
    _, reply, _ = _call("STATUS_QUERY")
    payload = reply.payload

    for key in (
        "lot_count",
        "freshness_unresolved_lot_count",
        "freshness_expired_lot_count",
        "freshness_risk_lot_count",
        "min_remaining_freshness_days",
    ):
        assert type(payload[key]) is int, key
    assert payload["lot_count"] == 4
    assert payload["freshness_expired_lot_count"] == 1
    assert payload["freshness_unresolved_lot_count"] == 0
    assert (payload["min_remaining_freshness_days"], payload["min_freshness_lot_id"]) == (
        -3,
        "LOT-EXPIRED",
    )
    # 정책 원값은 비교 없이 나란히 실린다
    assert payload["capacity_tight_ratio"] == 0.90
    assert payload["freshness_pressure_ratio"] == 0.30


# ===========================================================================
# F. 뜻 — lots[].available_qty_kg (물리 잔량) ≠ inventory_by_item (가용 정본)
# ===========================================================================


def _lots_and_inventory(mode: str) -> tuple[list[dict], list[dict]]:
    _, reply, _ = _call(mode)
    if mode == "PRE_SALES":
        supply = reply.payload["sellable_supply"]
        return supply["lot_constraints"], supply["inventory_by_item"]
    return reply.payload["lots"], reply.payload["inventory_by_item"]


@pytest.mark.parametrize("mode", ["PRE_PURCHASE", "PRE_SALES"])
def test_lots_의_available_qty_kg_는_status_와_무관한_물리_잔량이다(mode):
    """🔴 **이 파일에서 가장 중요한 뜻.** 두 칸의 이름이 같지만 정의가 다르다.

    ```text
    lots[].available_qty_kg              repository 가 `remaining_qty_kg` 를 그대로 싣는다
                                         (`_inventory_lot_from_row`) · `build_lot_constraints`
                                         는 status 로 거르지 않는다 → HOLD · 만료 Lot 도 전량
    inventory_by_item[].available_qty_kg 비-ACTIVE 제외 · 만료(<= 0) 제외 · 예약·할당 차감
                                         (`build_inventory_by_item` → `_sellable_lot_contributions`)
    ```

    매입 `usable_holdings_kg` docstring 이 *"이름이 같아서 못 봤다"* 로 적어 둔 그 자리다.
    설계 §14 는 이 뜻을 **유지**(안 B)하기로 했다 — 물류가 비-ACTIVE 를 0 으로 투영하면
    Critic · 판매가 받는 "물리 잔량" 의 뜻이 바뀐다.
    """
    lots, inventory = _lots_and_inventory(mode)
    by_lot = {row["lot_id"]: row for row in lots}

    # 물리: 넷 다 실리고, 수량은 잔량 그대로, status 는 원문 그대로
    assert set(by_lot) == {"LOT-ACTIVE", "LOT-EXPIRED", "LOT-HOLD", "LOT-MU"}
    assert by_lot["LOT-HOLD"]["available_qty_kg"] == 250.0
    assert by_lot["LOT-HOLD"]["status"] == "HOLD"
    assert by_lot["LOT-EXPIRED"]["available_qty_kg"] == 700.0
    assert by_lot["LOT-EXPIRED"]["remaining_freshness_days"] == -3  # 0 으로 접지 않는다
    assert by_lot["LOT-ACTIVE"]["available_qty_kg"] == 1000.0  # 할당 400 을 여기서 빼지 않는다
    physical_baechu = sum(row["available_qty_kg"] for row in lots if row["item"] == "배추")
    assert physical_baechu == 1950.0

    # 가용: 품목당 한 행 · 품목명 순 · 차감·제외가 전부 반영된 값
    assert inventory == [
        {"item": "무", "available_qty_kg": 300.0},
        {"item": "배추", "available_qty_kg": 500.0},
    ]
    assert physical_baechu != 500.0


def test_lots_에는_grade_칸이_있고_근거_없으면_None_이다():
    """정규화 근거가 없는 등급은 `None` 으로 드러난다 — 키를 빼거나 임의 등급으로 채우지 않는다."""
    lots, _ = _lots_and_inventory("PRE_PURCHASE")
    assert all("grade" in row and row["grade"] is None for row in lots)


def test_PRE_PURCHASE_와_PRE_SALES_의_가용재고는_같은_함수의_같은_답이다():
    """두 모드가 `build_inventory_by_item` 하나를 부른다 — 매입이 보는 가용과 판매가 보는
    가용이 갈리면 매입은 팔 수 있다고 보고 판매는 못 잡는 상태가 된다."""
    _, purchase_inventory = _lots_and_inventory("PRE_PURCHASE")
    _, sales_inventory = _lots_and_inventory("PRE_SALES")
    assert purchase_inventory == sales_inventory


def test_출고_귀속_불명이면_가용재고를_지어내지_않는다(monkeypatch):
    """예약·할당 축(`outbound_commitments`)을 못 읽으면 `inventory_by_item` 은 **없다**.

    ```text
    PRE_PURCHASE   READY 유지 · 키 생략 · missing_data 에 이름
    PRE_SALES      RUNTIME_NOT_READY · Lot 합계로 대신 답하지 않는다
    ```
    """
    read = LogisticsRead(
        snapshot=_snapshot(outbound_commitments=None),
        policy=_policy(),
        delivery_route="LOGI-BASE-5PL",
    )
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: read)

    _, purchase_reply, _ = _call("PRE_PURCHASE")
    assert purchase_reply.runtime_status == "READY"
    assert "inventory_by_item" not in purchase_reply.payload
    assert "inventory_by_item" in purchase_reply.missing_data
    assert purchase_reply.payload["lots"]  # 물리 잔량은 여전히 나간다

    _, sales_reply, _ = _call("PRE_SALES")
    assert sales_reply.runtime_status == "RUNTIME_NOT_READY"
    assert sales_reply.missing_data == ("inventory_by_item",)


# ===========================================================================
# G. observed_at — 칸의 계약과 현재 기준선
# ===========================================================================


def test_observed_at_칸은_date_또는_None_이고_기본값은_None_이다():
    """PR #626 이 연 칸. **`None` 은 「안 쟀다」** 이고 마스터가 `as_of` · 오늘 · `created_at`
    으로 메우지 않는다 (`scheduler._observed_ats` ·
    `tests/master/test_observed_at_carried_to_walk.py`).

    ★ 실행 흔적(`ExecutionMetadata`)에는 이 칸이 **없다** — 관측 기준시점은 사실의 성질이지
      실행의 흔적이 아니다. 흔적에서 끌어오면 *"언제부터 알 수 있었나"* 가 *"언제 돌렸나"* 가 된다.
    """
    hints = typing.get_type_hints(AgentReply)
    assert hints["observed_at"] == (date | None)
    field = {f.name: f for f in dataclasses.fields(AgentReply)}["observed_at"]
    assert field.default is None
    assert "observed_at" not in {f.name for f in dataclasses.fields(ExecutionMetadata)}


@pytest.mark.parametrize("mode", MODES)
def test_현재_물류_4모드는_observed_at_을_싣지_않는다(mode):
    """**기준선.** 지금 물류는 네 모드 전부 `None` 을 낸다 — 걷기 요약이 「안 쟀다」로 센다.

    ⚠️ 이 파일에서 유일하게 **Phase 1 이 의도적으로 갱신할** 검사다. 그때 규칙은:
       원천에 관측일 칸이 있는 입력만 `date`, 정책 표·상수가 섞이면 `None`, 파생은 `max`,
       `as_of` 를 편의상 대입하지 않는다 (설계 §18). `as_of` 로 채운 값은 여기서 잡혀야 한다.
    """
    _, reply, _ = _call(mode)
    assert reply.observed_at is None
    assert reply.observed_at != reply.as_of
