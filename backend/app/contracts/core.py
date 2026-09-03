# ─────────────────────────────────────────────────────────────────────────────
# STATUS: 공용 계약 (2026-09-03 이전 완료)
#   마스터·검증·재무·물류·매입 어댑터가 전부 여기서 타입을 가져온다.
#   `app/master/envelope.py` 가 Evidence·Verdict·RuntimeStatus·SuggestedAdjustment 를 쓴다.
#
#   🟢 **`app/orchestrator/contracts_core.py` 에서 여기로 옮겼다** (2026-09-03).
#      전에는 공용 계약이 한 파트 폴더에 있어 네 파트가 "오케를 임포트하는" 모양이었다.
#      다섯 파트 동의: 매입·물류·재무 (판매는 참조 0건이라 대상 아님).
#
#   ⚠️ **옛 자리는 재수출 shim 으로 남아 있다** — 아직 지우지 말 것.
#      순서 ① 이동 → ② shim 유지 → ③ 파트별 import 교체 → ④ shim 제거.
#      지금은 ②다. ④ 전에 각 파트에 통보한다 (마스터 약속).
#
#   🔴 **④ 는 `app/master/envelope.py` 를 먼저 옮긴 뒤에 친다.** 물류·판매가 봉투를
#      경유해 이 모듈을 두 번째 경로로 읽는다 — 자기 import 를 다 고쳐도 봉투가
#      옛 자리를 가리키면 같이 깨진다 (2026-09-03 물류 지적).
#
#   ★ **이 판에서 타입·필드 정의는 하나도 안 건드렸다.** 위치와 import 만이다.
#      섞으면 무엇이 깨뜨렸는지 못 가린다 (재무 합의).
# ─────────────────────────────────────────────────────────────────────────────
"""
contracts_core.py v1.2.8 — 햇들농산 공용 계약 타입 (오케스트레이터/Critic 소유: 이현서)

v0.2 → v0.3 (정의서 v0.13 반영)
  ⑧ Cycle 신설 — 사이클 A(조달) / B(판매) 분리. 타입·로그·Critic 전부 사이클 인지
  ⑨ cap_by_date 벡터 — 재고 cap 이 날짜별 (N15). 창고 점유 검사 신설
  ⑩ N12 확정 반영 — T3 는 단가 환산을 하지 않는다. 시나리오가 낸 총액으로만 대조
  ⑪ Severity 3단계 → verdict 판정 규칙 (§3.4.5-⑥)
  ⑫ E5 · single_option_reason · has_unmet_obligation (§5.0)
  ⑬ 부서 reject 사유 → E2/E3 매핑 (§5.1). base_state_violated 신설
  ⑭ price_basis 기본값 AUCTION (N11 종결)
  ⑮ 판매 배분 타입 (SaleAllocation) · T0 on_hand/in_transit 분리

v0.3 → v1.1 (정의서 v1.1 반영)
  ⑯ ApprovedPurchaseCommitment — 사이클 간 상태 전달 계약 (§3.2.3) 🔴
  ⑰ 0 / NULL 엄격 구분 — require_value() 로 계산 자체를 차단 (§1.2-10)
  ⑱ cash_priority 2종 분리 — base_(T0) / sales_(S2 산출) (§7.4.1)
  ⑲ cash_before_outflow / cash_after_outflow 분리 (§3.4.5-①)
  ⑳ usable_remaining_borrowing_capacity — 정책한도 ≠ 가용액
  ㉑ INVALID_FOR_HARD 상태 — N2 창고 capacity 하드 제약 사용 차단 (§7.2)

v1.1 → v1.2 (정의서 v1.2 · 유저플로우 v1.4 · UI v1.3)
  ㉒ 🔴 strategy_type 의미 충돌 해소 — 축은 strategy_type, 성향은 stance
  ㉓ 🔴 cycle_log 1일 1행 · a_/b_ 컬럼 구조로 교체 (유저플로우 §⑧)
  ㉔ 🔴 has_unmet_obligation 산출 주체 T0 → S3(오케스트레이터) 이동 (§5.0 v1.2)
  ㉕ 🔴 파산선 = minimum_cash_balance (0원 아님)
  ㉖ expected_arrival_date 는 매입이 회차별로 계산 (N4 3자 공유, §3.4.5-③)
  ㉗ collapse_type AXIS | QUANTITY 구분
  ㉘ 승인 규약 — recommended_id · override_reason (UI §5 approval 테이블)

v0.1 → v0.2 변경 (`매입파트_답변서_v0.9_검토의견` 반영)
  ① SplitLeg · SourcingLot 신설 — C-1 (a) 총량 스칼라 + split_plan 확정에 대응
  ② ClipResult 확장 — 클리핑 시 하위 계획도 함께 축소해 삼중 일치를 유지 (B1)
  ③ PurchaseScenario 프로토콜 4 → 8 필드
  ④ T0Snapshot 8필드 추가 (안건 9.5 취합분)
  ⑤ EvidenceGrade 에 SIM_FIXED 신설 (안건 12)
  ⑥ 영업 축 price → channel_mix 개명 (이름 충돌 해소)
  ⑦ PriceBasis 신설 — 경락가/중도매가 혼선 방지 (확인 4번)

v1.2 → v1.2.1 (버그 수정 · 계약은 하위 호환)
  ㉙ 🔴 ArrivalLeg 신설 + ApprovedPurchaseCommitment.arrival_schedule
       도착일이 단수라 분할 매입이 사이클 B overlay 에서 하루로 뭉쳤다 (§3.6.5)
  ㉚ 🔴 CycleBState.in_transit · cap_by_date_overlay 를 회차별 누적으로 교정
  ㉛ EndCode · BacktestEnd · resolve_end_code 중복 정의 제거 (한쪽만 고치는 함정)

v1.2.1 → v1.2.2 (사이클 B 골격 · 계약은 하위 호환)
  ㉜ OutboundBand 신설 — 사이클 B 밴드 (cap 만 있고 floor 는 없다, §3.7.4)
  ㉝ PipelineState.outbound_band 추가

v1.2.2 → v1.2.3 (영업 IO 명세 v0.6 반영)
  ㉞ 🔴 Verdict 에 skipped 신설 — 미결 입력으로 못 돈 검사를 ok 로 내지 않는다 (§1)
  ㉟ 🔴 HOLD 채널 — is_outbound · outbound_qty_by_item. 보유는 출고가 아니다 (§5)
  ㊱ 🔴 OutboundLeg · outbound_by_date — S3 공용 출고 결합 검사의 정본 입력 (§5)
  ㊲ 🔴 SalesFacts — confirmed_obligation_kg · coverable_kg. E5 판정 입력 (§5)
  ㊳ T2Reply.skipped_checks · PipelineState.sales_facts

v1.2.3 → v1.2.4 (재무·물류 문서 v1.2 · 품목별 T2 결합)
  ㊴ 🔴 T2Reply.item — 품목별 회신 (영업 IO 명세 §0 run_floor_reply(item, ...))
  ㊵ 🔴 RuntimeStatus + T2Reply.runtime_status + Band.not_ready
       READY 가 아닌 부서를 조용히 건너뛰면 그 cap 이 무한대가 되어
       **부서가 죽은 날 무제한 매입이 통과한다**. T3 가 E4 로 끊는다.
  ㊶ combine_band 가 부서당 회신 여럿을 받는다 (하위 호환 유지)

  ※ 신규 필드·타입은 전부 기본값이 있어 기존 호출부는 그대로 동작한다.
    다만 **전원 배포 대상**이므로 재배포가 필요하다.

정의서 v0.9 §1.2 / §3.4 / §3.4.5-④ / §7.3, 계약서 v0.3 §8 준수.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

# ---------------------------------------------------------------------------
# 0. 기본 타입
# ---------------------------------------------------------------------------

ItemCode = str  # "배추" | "무" | "양파" | "피마늘"  — v0.9 4품목 체제
Grade = str  # "특" | "상" | "중"

ITEMS: tuple[ItemCode, ...] = ("배추", "무", "양파", "피마늘")

Dept = Literal["sales", "inventory", "finance"]

# ★ v1.2.3 — skipped 신설. 영업 IO 명세 v0.6 §1 이 요구한다.
#   "하드 제약의 입력값이 null 이면 그 검사는 회사 공통 CheckResult 규약의
#    skipped 로 회신된다. 값을 임의로 채워 평가된 것처럼 보이게 하지 않는다."
#
#   v1.2.2 까지는 이 값이 없어서 부서가 ok(통과)로 낼 수밖에 없었다.
#   **미결값 때문에 못 돈 검사와 돌아서 통과한 검사가 구분되지 않는다.**
#   §1.2-10(0 과 NULL 을 섞지 않는다)의 CheckResult 판 이다.
Verdict = Literal["ok", "conditional", "reject", "skipped"]
CheckKind = Literal["hard", "soft"]

# ★ v0.2 — SIM_FIXED 신설 (안건 12 / 재무 검토 메모 F4)
#   "독립의 정확한 의미는 정적(static)이다"(N2 논의 결론)를 근거 등급에도 적용한다.
#   팀이 명시적으로 확정하고 백테스트 내내 변하지 않는 값은, 출처가 가정이더라도
#   제약으로서는 OFFICIAL 과 성질이 같다. 흔들리는 것은 ASSUMED 뿐이다.
#   단, SIM_FIXED 를 붙이려면 evidence_detail 에 승인 회차를 기록해야 한다.
EvidenceGrade = Literal["OFFICIAL", "VENDOR", "SIM_FIXED", "ASSUMED", "INVALID_FOR_HARD"]

HARD_ALLOWED_GRADES: frozenset[str] = frozenset({"OFFICIAL", "VENDOR", "SIM_FIXED"})

# ★ v1.1 — INVALID_FOR_HARD 는 ASSUMED 보다 아래다.
#   ASSUMED 는 "출처가 약하다"이고, INVALID_FOR_HARD 는 "이 값으로 하드 제약을
#   만들면 제약이 무력화된다"이다 (§7.1 제약 독립성 위반).
#   현재 해당: N2 창고 capacity — 수요 역산으로 산출돼 가동률 5.9% 로 영원히
#   바인딩되지 않는다. allow_assumed_hard 완화 대상이 **아니다.**
INVALID_FOR_HARD = "INVALID_FOR_HARD"

# ★ v0.2 — 매입 기준 시장. 확인 4번(경락가 vs 중도매가) 혼선 방지용.
#   (가)/(나) 어느 쪽으로 결정되든 오케스트레이터 로직은 동일하고 이 상수만 바뀐다.
PriceBasis = Literal["AUCTION", "WHOLESALE"]  # 경락가 / 중도매가
DEFAULT_PRICE_BASIS: PriceBasis = "AUCTION"  # N11 종결 (v0.11) — 경락가 확정

# ★ v0.3 — 사이클 분리 (정의서 v0.13 §3.1)
#   A: 조달 (제안자=매입)  T1 → T2 → T3 → Critic → H1
#   B: 판매 (제안자=영업)  S1 → S2 → S3 → Critic → H2
#   두 사이클은 같은 형태다 — 제안 → 제약 → 조정 → 검증 → 승인.
#   구현 패턴을 재사용하되 **사이클을 넘나드는 회송은 없다** (§3.6.3).
Cycle = Literal["A", "B"]

# ★ v0.3 — 소프트 경고 severity (§3.4.5-⑥)
#   verdict = conditional 의 조건을 "소프트 경고 존재"로 두면
#   구조적 상시 경고(회수 D+30, 미수금 집중) 때문에 verdict 가 정보를 잃는다.
Severity = Literal["HIGH", "MEDIUM", "LOW"]

# ★ v1.2.4 — 재무·물류 통합 문서 v1.2 가 A 사이클의 verdict 를 runtime_status 로 바꿨다.
#   둘은 다른 축이므로 **대체가 아니라 병존**한다.
#     runtime_status  이 에이전트가 오늘 돌 수 있었는가        (실행 가능성)
#     verdict         회신 내용의 상태                          (판정)
#   READY 가 아니면 밴드 기여가 없다. 그때 밴드를 그냥 비우면 cap 이 무한대가 되어
#   **부서가 죽은 날에 무제한 매입이 통과한다.** Band.not_ready 로 명시적으로 막는다.
RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]

# ★ 2026-09-03 — 안이 무엇 때문에 깎였는가. **소유는 마스터**다 (매입 P-4 답).
#
#   매입 `draft_plan` 이 `clipped_by[].constraint` 로 내고, 앞으로 `applied_adjustments`
#   의 `binding` 에도 같은 어휘가 실린다. 공용 계약이고 Critic 도 검사해야 하므로
#   **한 파트가 정하면 그 파트 화면 문구에 맞는 값**이 된다.
#
# 🔴 **한글을 쓰지 않는 이유가 있다.** 지금 매입은 `{"창고", "현금", "신선도"}` 를
#   쓰는데, `"현금"` 이 **다른 축에도 있다**.
#
#   ```text
#   자원 축     clipped_by[].constraint     창고 · 현금 · 신선도
#   근거 출처 축 RationaleSource            예측 · 시세관측 · 재고 · 주문 · 현금 · 문서ID
#                (app/purchase_agent/schemas.py:91)
#   ```
#
#   **같은 문자열인데 뜻이 다르다.** 표시 문구를 값으로 쓰면 축이 섞인다.
#
# ⚠️ `"현금"` 이 아니라 `자금` 인 것도 그래서다 (매입·판매 정리) — 재무 제약은
#   차입여력·예정 유출입까지 포함해서 *"현금"* 은 좁게 읽힌다.
BindingConstraint = Literal["WAREHOUSE", "FINANCE", "FRESHNESS"]

BINDING_CONSTRAINT_LABELS: Mapping[str, str] = {
    "WAREHOUSE": "창고",
    "FINANCE": "자금",
    "FRESHNESS": "신선도",
}
"""사람이 읽는 문구. **어휘는 마스터가, 표시는 부서가 정한다** — 뜻을 아는 쪽이
   문구를 정하는 것이 맞다 (매입 제공 2026-09-03).

⚠️ **표시 문구로 판정하지 않는다.** 값 비교는 위 `BindingConstraint` 로만 한다."""


class ContractViolation(Exception):
    """계약 위반. 문서 규칙이 아니라 타입 레벨에서 즉시 터뜨린다."""


class UnresolvedValueError(Exception):
    """미결값(NULL)을 계산에 넣으려 했다. §1.2-10 — NULL 은 계산을 막는 장치다."""


def require_value(name: str, value, blocker: str = ""):
    """
    §1.2-10 (v1.1 신설) — **0 과 NULL 을 엄격히 구분한다.**

        0    = 값이 확정되었고 그 값이 0        예) initial_debt = 0 (무차입 BASE)
        NULL = 아직 결정되지 않았다              예) purchase_payment_days (N5)

    ★ NULL 을 0 으로 읽으면 **에러 없이 손익만 달라진다.**
      purchase_payment_days 를 0 으로 처리하면 D+0 즉시 지급이 되어
      운전자금이 과대 계상된다. 그래서 기본값을 주지 않고 계산 자체를 차단한다.

    게이트 ①(계약·골격)에서는 이 함수를 호출하는 지점까지만 만들고,
    게이트 ②(Seed 생성)에서 값이 채워지면 그대로 통과한다 (§9.1).
    """
    if value is None:
        raise UnresolvedValueError(
            f"'{name}' 는 아직 미결(NULL)이다. 0 으로 대체하지 말 것 — "
            f"계산이 조용히 틀린다." + (f" 차단 원인: {blocker}" if blocker else "")
        )
    return value


# ---------------------------------------------------------------------------
# 1. Evidence — §1.2-5
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    claim: str
    source: Literal["inventory", "sales", "finance", "documents", "tool_calc", "persona"]
    ref_ids: tuple[str, ...]
    value: float
    unit: str
    evidence_grade: EvidenceGrade = "ASSUMED"
    evidence_detail: str = ""  # SIM_FIXED 는 여기에 승인 회차를 적는다

    def __post_init__(self) -> None:
        if not self.ref_ids:
            raise ContractViolation(
                f"Evidence '{self.claim}' 에 ref_ids 가 없다. §1.2-5 위반 — Critic FAIL 대상."
            )
        if self.evidence_grade == "SIM_FIXED" and not self.evidence_detail:
            raise ContractViolation(
                f"Evidence '{self.claim}': SIM_FIXED 는 확정 기록이 필요하다. "
                f"evidence_detail 에 승인 회차를 적을 것 (예: '5회차 승인')."
            )


# ---------------------------------------------------------------------------
# 2. CheckResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """
    ┌─ 밴드 기여 방향 (정의서 §3.4.5-④) ────────────────────────────┐
    │  영업     → floor_kg          (FAIL 상황 = "너무 적게 산다")   │
    │  재고·물류 → cap_kg / cap_total_kg                            │
    │  재무     → cap_amount_krw    (§4.3 금액 축)                  │
    └───────────────────────────────────────────────────────────────┘

    ★ floor 는 품목별이다. 총량 floor 는 존재하지 않는다.
      확정주문이 품목별이므로 총량으로 뭉개면 "배추 납기는 못 지키고
      무를 초과 매입"해도 통과한다. (검토의견 §3-3 정정 사항)
    """

    check_id: str
    dept: Dept
    verdict: Verdict
    kind: CheckKind

    reason: str
    evidences: tuple[Evidence, ...]

    floor_kg: Mapping[ItemCode, float] | None = None  # 영업 전용 (품목별)
    cap_kg: Mapping[ItemCode, float] | None = None  # 재고 전용 (+영업 loose)
    cap_total_kg: float | None = None  # 재고 전용 (집계)
    cap_amount_krw: float | None = None  # 재무 전용

    evidence_grade: EvidenceGrade = "ASSUMED"
    source_ref: str = ""
    allow_loose_cap: bool = False

    # ── v0.3 신설 ────────────────────────────────────────────────
    cycle: Cycle = "A"

    severity: Severity = "MEDIUM"
    """소프트 경고 전용. HIGH 만 verdict 를 conditional 로 올린다 (§3.4.5-⑥)."""

    cap_by_date_kg: Mapping[date, float] | None = None
    """재고 전용 · N15. 날짜별 창고 점유 상한. **확정분만 반영** (§3.4.5-④ v0.13).
       전략적 판매·사이클 B 후보는 반영하지 않는다 — 체결 불확실한 물량을
       창고 여유로 계산하면 판매가 안 됐을 때 창고가 넘친다.
       사이클 A 는 전략 판매 = 0 을 가정하고 안전한 방향으로만 틀린다."""

    base_state_violated: bool = False
    """★ §5.1 — reject 가 '오늘은 못 산다'(E2)인지 '회사가 위험하다'(E3)인지 구분.
       True = BASE 상태 자체가 하드 제약 위반 → E3 반려 + 백테스트 종료 검토.
       이 플래그가 없으면 파산/교착 종료 판정이 성립하지 않는다."""

    def __post_init__(self) -> None:
        allowed = {
            "sales": ("floor_kg",),
            "inventory": ("cap_kg", "cap_total_kg", "cap_by_date_kg"),
            "finance": ("cap_amount_krw",),
        }[self.dept]
        if self.dept == "sales" and self.allow_loose_cap:
            allowed = allowed + ("cap_kg",)

        for fname in ("floor_kg", "cap_kg", "cap_total_kg", "cap_amount_krw", "cap_by_date_kg"):
            if getattr(self, fname) is not None and fname not in allowed:
                raise ContractViolation(
                    f"[{self.check_id}] dept={self.dept} 는 {fname} 를 채울 수 없다. "
                    f"허용: {allowed}. 정의서 §3.4.5-④ 밴드 기여 방향 위반."
                )

        if self.kind == "soft" and self.verdict == "reject":
            raise ContractViolation(
                f"[{self.check_id}] soft 검사는 reject 를 낼 수 없다. "
                f"소프트 경고는 하드 제약을 이기지 못한다 (N8)."
            )
        if self.verdict != "ok" and not self.reason:
            raise ContractViolation(f"[{self.check_id}] ok 가 아니면 reason 필수.")
        if self.kind == "hard" and self.evidence_grade == INVALID_FOR_HARD:
            raise ContractViolation(
                f"[{self.check_id}] INVALID_FOR_HARD 등급 값으로 하드 제약을 만들 수 없다 "
                f"(§7.2). 소프트 경고로 내리거나 독립적 재산정 후 사용할 것."
            )
        if self.kind == "hard" and self.severity != "MEDIUM":
            raise ContractViolation(
                f"[{self.check_id}] severity 는 소프트 경고 전용이다 (§3.4.5-⑥)."
            )

    @property
    def is_binding(self) -> bool:
        return self.kind == "hard" and self.verdict in ("conditional", "reject")

    @property
    def grade_allows_hard(self) -> bool:
        return self.evidence_grade in HARD_ALLOWED_GRADES


# ---------------------------------------------------------------------------
# 3. suggested_adjustment — 부서별 축 제한 (§3.4.2)
# ---------------------------------------------------------------------------

# ★ v0.2 — 영업 축 price → channel_mix 개명.
#   variant_axis 의 price(매입 상한가, V-2에서 폐기)와 문자열이 겹쳐
#   constraints.yaml / cycle_log / Critic L3 에서 구분이 안 됐다.
#   §3.7.4 채널 포트폴리오에서 영업의 실제 결정 변수가 채널 배분이므로
#   이름이 실체에 더 가까워지는 부수 효과도 있다.
AdjustAxis = Literal["quantity", "timing", "channel_mix", "amount"]

_DEPT_AXES: dict[Dept, tuple[AdjustAxis, ...]] = {
    "inventory": ("quantity", "timing"),
    "sales": ("channel_mix", "quantity"),
    "finance": ("amount",),
}

# ★ v1.2 — 필드명 개명 전파 (정의서 §3.5.1)
#   variant_axis → strategy_type. 값은 quantity | timing | mix 그대로다.
#   variant_collapsed 는 이름을 유지한다 (축이 아니라 수렴 '현상'을 가리키므로).
#
#   🔴 v0.3 은 strategy_type 에 "보수/기준/공격"을 담았는데 이는 V-3 의 다른 논의였다.
#     UI v1.3 §2 가 PurchaseCandidate.strategy_type: quantity|timing|mix 로 확정했으므로
#     성향 라벨은 stance 로 분리한다. 두 개념을 한 필드에 담으면 화면과 계약이 어긋난다.
StrategyType = Literal["quantity", "timing", "mix"]
VariantAxis = StrategyType  # 하위 호환 별칭. 신규 코드는 StrategyType 사용.

Stance = Literal["보수", "기준", "공격"]  # 사람이 읽는 성향 라벨 (H1 화면 탭)

# mix 축 게이팅 임계 — 금액 기준 최대 품목 비중 (constraints.yaml 등재)
ITEM_CONCENTRATION_THRESHOLD = 0.70


@dataclass(frozen=True)
class SuggestedAdjustment:
    dept: Dept
    axis: AdjustAxis
    target_value: float
    unit: str
    reason: str
    ref_ids: tuple[str, ...]

    #: 🆕 **이 조정이 어느 시나리오를 대상으로 하는가** (2026-09-02 · 되먹임 계약 v0.2).
    #:
    #: 전에는 시나리오 축이 아예 없어 `reason` 문장 안에만 남았다. 받는 쪽이 그것을
    #: 쓰려면 **부서 문장을 파싱해야 했다.**
    #:
    #: 🔴 부서 하나의 사정이 아니다 — 재무도 걸린다. 상한 2,000만에 보수 1,500만 ·
    #:   기본 2,100만 · 공격 2,800만이면 **기본·공격만 재조정 대상**인데, 그 사실을
    #:   기계가 읽을 자리가 없었다.
    #:
    #: ★ **여럿을 담는다.** 물류는 축·회차·목표값이 같으면 시나리오를 가로질러
    #:   한 건으로 합치는데, 그때 합쳐진 라벨을 다 담는다 — 건수를 안 늘리면서
    #:   "이 조정은 세 안 모두에 해당" 이 값으로 드러난다.
    #:
    #: ★ **비어 있어도 된다.** 안 채운 것과 해당 없는 것을 여기서 가르지 않는다.
    scenario_labels: tuple[str, ...] = ()

    #: 🆕 **어느 회차의 상한인가** (2026-09-02).
    #:
    #: 물류 내부 `ScenarioAdjustment` 는 `split_date` 로 회차를 식별하는데 그 값이
    #: 표준형으로 안 옮겨졌다. **값이 갈리느냐와 무관하게** 받는 쪽이 *"이 상한을
    #: 어느 회차에 적용할지"* 를 알아야 한다.
    #:
    #: ★ **번호가 아니라 날짜다** (물류 지정). 물류에는 회차 번호가 없다 —
    #:   번호 칸을 두면 **없는 값을 만들게 된다.** 받는 쪽이 번호를 원하면 자기
    #:   회차 목록에서 날짜로 찾는다.
    #:
    #: ★ 회차 개념이 없는 축(재무 `amount`)은 `None` 이다.
    split_date: date | None = None

    def __post_init__(self) -> None:
        if self.axis not in _DEPT_AXES[self.dept]:
            raise ContractViolation(
                f"{self.dept} 는 {self.axis} 축을 제안할 수 없다. "
                f"허용: {_DEPT_AXES[self.dept]} (§3.4.2 축 침범)."
            )
        if not self.ref_ids:
            raise ContractViolation("suggested_adjustment 에도 ref_ids 필수 (§1.2-5).")


# ---------------------------------------------------------------------------
# 4. T2Reply (§8.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class T2Reply:
    dept: Dept
    as_of: date
    checks: tuple[CheckResult, ...]
    suggested_adjustments: tuple[SuggestedAdjustment, ...] = ()
    reasoning: str = ""

    item: ItemCode | None = None
    """★ v1.2.4 — 품목별 회신이면 그 품목. 전사 단일 회신이면 None.

    영업 IO 명세 §0 이 `run_floor_reply(item, ...)` 로 **품목마다 한 번씩** 호출된다고
    정했다. 반면 재무 A 는 `scope = ALL_ITEMS_TOTAL` 로 전사 총액 하나를 낸다.
    두 축이 한 밴드에서 만나므로, 어느 쪽인지 회신 자신이 밝혀야 결합이 성립한다."""

    runtime_status: RuntimeStatus = "READY"
    """READY 가 아니면 이 회신은 밴드에 기여하지 않고 Band.not_ready 에 기록된다."""

    def __post_init__(self) -> None:
        for c in self.checks:
            if c.dept != self.dept:
                raise ContractViolation(f"{self.dept} 회신에 {c.dept} 검사가 섞였다.")

    @property
    def verdict(self) -> Verdict:
        """
        §3.4.5-⑥ 판정 규칙 (v0.3)

            reject       ⟸ 하드 제약 위반
            conditional  ⟸ 하드 통과 + 밴드 유효 + HIGH 경고 ≥ 1
            ok           ⟸ 하드 통과 + 밴드 유효 + HIGH 경고 없음

        ★ verdict 는 사람 승인 화면과 cycle_log 기록용이다.
          T3 클리핑은 밴드 값만으로 수행하며 verdict 를 참조하지 않는다 (§5.1 말미).

        ★ v1.2.3 — skipped 는 verdict 를 움직이지 않는다.
          영업 IO 명세 §3 이 verdict 를 `ok | conditional` 두 값으로 정의하고
          소프트 경고 심각도로만 정한다. skipped 는 "이 검사가 못 돌았다"는 사실이며
          판정이 아니다. 대신 `skipped_checks` 로 드러내 T3·Critic 이 보게 한다.
        """
        hard = [c.verdict for c in self.checks if c.kind == "hard"]
        if "reject" in hard:
            return "reject"
        if any(
            c.kind == "soft" and c.severity == "HIGH" and c.verdict not in ("ok", "skipped")
            for c in self.checks
        ):
            return "conditional"
        if "conditional" in hard:
            return "conditional"
        return "ok"

    @property
    def skipped_checks(self) -> tuple[CheckResult, ...]:
        """
        미결 입력 때문에 돌지 못한 검사들 (v1.2.3).

        ★ 통과와 구분해서 흘려보내야 한다. 이것이 비어 있지 않은 날은
          "전 항목 통과"가 아니라 "일부는 아직 검사 자체를 못 했다"이다.
          Critic 의 coverage 와 같은 취지다.
        """
        return tuple(c for c in self.checks if c.verdict == "skipped")

    @property
    def base_state_violated(self) -> bool:
        """§5.1 — True 면 E3(회사 상태 위험), False 면 E2(오늘 못 산다)."""
        return any(c.base_state_violated for c in self.checks if c.kind == "hard")

    @property
    def hard_checks(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.kind == "hard")

    @property
    def soft_warnings(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.kind == "soft" and c.verdict != "ok")


# ---------------------------------------------------------------------------
# 5. T0 스냅샷 (§8.1) — v0.2 에서 8필드 추가 (안건 9.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Forecast:
    item: ItemCode
    horizon_days: int
    q10: float
    q50: float
    q90: float
    confidence: float
    model_version: str


@dataclass(frozen=True)
class CashFlowLeg:
    """확정 유출입 1건. 재무 T0 Tool 산출물 (재무 검토 메모 F5)."""

    due_date: date
    amount_krw: float
    kind: Literal["salary", "debt_service", "purchase_payable", "receivable"]
    ref_id: str


@dataclass(frozen=True)
class FinanceSnapshot:
    cash_balance_krw: float
    salary_due_krw: float
    debt_service_due_krw: float
    minimum_operating_cash_krw: float
    receivable_incoming_krw: float
    projected_cash_min_krw: float
    credit_headroom_krw: float
    horizon_days: int = 30

    # ── v1.1 §3.4.5-① 판정 시점 분리 ────────────────────────
    cash_before_outflow: Mapping[date, float] = field(default_factory=dict)
    """그날 의무를 지급하기 **직전** 잔액. 급여·원리금 커버 판정에 쓴다."""

    cash_after_outflow: Mapping[date, float] = field(default_factory=dict)
    """그날 모든 확정 유출 **후** 잔액. 최소현금 방어선 판정에 쓴다.
       projected_cash_min = MIN(cash_after_outflow[d])"""

    usable_remaining_borrowing_capacity_krw: float | None = None
    """★ 정책한도 − 기실행액 ≠ 실제 조달 가능액 (v1.1).
       정책한도는 '빌려도 된다고 정한 상한'이고, 이 값은 '실제로 빌릴 수 있는 액수'다.
       N9 에서 별도 결정되므로 그 전까지 None(미결) — require_value() 로 차단된다."""
    # ★ v0.2 — ports 재호출 금지(검토의견 §4)에 따라 스케줄을 T0 에서 확정 배포
    obligation_schedule: tuple[CashFlowLeg, ...] = ()
    receivable_schedule: tuple[CashFlowLeg, ...] = ()

    @property
    def purchasable_krw(self) -> float:
        return (
            self.cash_balance_krw
            - self.salary_due_krw
            - self.debt_service_due_krw
            - self.minimum_operating_cash_krw
            + self.receivable_incoming_krw
        )


@dataclass(frozen=True)
class InventoryLot:
    lot_id: str
    item: ItemCode
    qty_kg: float
    freshness_days_left: int
    in_transit: bool = False  # True = 아직 도착 안 함 → 오늘 판매 후보 아님
    eta: date | None = None


@dataclass(frozen=True)
class T0Snapshot:
    """§3.1.1 필드 계약 (v0.10 확정 · v0.13 유지).
    ★ 여기 없는 값을 개별 조회하면 §1.2-9 위반이다."""

    as_of: date
    run_seq: int
    forecasts: tuple[Forecast, ...]
    spot_price_krw_per_kg: Mapping[ItemCode, float]
    inventory_available_kg: Mapping[ItemCode, float]
    warehouse_free_kg: float
    confirmed_orders_kg: Mapping[ItemCode, float]
    finance: FinanceSnapshot
    budget_envelope_krw: float

    # ── v0.2 신설 8필드 ─────────────────────────────────────────
    price_basis: PriceBasis = DEFAULT_PRICE_BASIS
    """매입 기준 시장. N11 종결로 **경락가 확정**(v0.11 §3.7.5).
       계약단가 산정 기준과 반드시 일치해야 한다 —
       Critic check_price_basis_consistency() 가 검사한다."""

    contract_price_basis: PriceBasis = DEFAULT_PRICE_BASIS
    """계약단가 분자도 경락가 평균으로 전환됨 (v0.11). price_basis 와 달라지면 FAIL."""

    item_mix_ratio_amount: Mapping[ItemCode, float] = field(default_factory=dict)
    """품목 편중 — **금액 기준**. mix 축 게이팅의 정본.
       금액 기준인 이유: 재무 cap 이 금액이고, mix 축의 실효성은
       '자금을 어떻게 나눠 쓰는가'에서 나온다. 수량이 고르더라도 금액이
       한 품목에 쏠리면 재무 cap 이 그 품목 하나에 걸린다.
       ※ 건고추가 수량 3.2% / 금액 33.4% 로 10배 갈렸던 사례가 근거다."""

    item_mix_ratio_qty: Mapping[ItemCode, float] = field(default_factory=dict)
    """품목 편중 — 수량 기준. 창고 cap 해석용. 게이팅에는 쓰지 않는다."""

    allowed_variant_axes: tuple[VariantAxis, ...] = ("quantity", "timing")
    """★ 게이팅 결과를 T0 에서 확정 배포한다.
       매입이 T1 에서 자체 계산하면 T3 의 회송 지시와 어긋난다 —
       §8.1('여기 없는 값을 개별 조회하면 에이전트마다 다른 숫자를 본다')의 직접 적용."""

    contract_price_krw_per_kg: Mapping[ItemCode, float] = field(default_factory=dict)
    """매입이 **참조값으로만** 읽는다. 하드 컷 아님 (검토의견 §3-5)."""

    margin_defense_floor_rate: float = 0.0
    """as_of 로 거치(0.267)/상환기(0.284) 선택된 값. 판정은 영업 T2 소관."""

    grade_unit_price: Mapping[tuple[ItemCode, Grade], float] = field(default_factory=dict)
    """등급별 예측 단가. 금액 항등식의 입력 (§3.4.5-④)."""

    # ── v0.3 신설 (정의서 v0.13 §3.1.1) ─────────────────────────
    snapshot_id: str = ""
    horizon_end: date | None = None
    horizon_basis: str = ""  # 투영 기간을 그 길이로 정한 이유 (재무)

    lots: tuple[InventoryLot, ...] = ()
    """로트 단위 필수. on_hand / in_transit 구분이 여기서 나온다."""

    base_cash_priority: str = "MEDIUM"
    """★ v1.1 §7.4.1 — T0 시점 BASE Projection 기반. **참조·대시보드 전용.**
       영업이 쓰는 것은 이 값이 아니라 S2 에서 산출되는 sales_cash_priority 다.
       T0 값을 영업에 주면 방금 승인한 조달 의무를 모른 채 판매를 결정하게 된다."""

    policy_version: str = ""
    """정책 테이블 버전 (§7.4). 이익률 3종·탐색 허용량·잔존가치 계수 등."""

    inbound_lead_days: int | None = None
    """N4. today_floor 계산과 유효 예측 구간 하한을 동시에 정한다. 미확정이면 None."""

    confirmed_occupancy_by_date: Mapping[date, float] = field(default_factory=dict)
    """날짜별 확정 창고 점유 (현재 로트 + 확정 입고 − 확정주문 납품)."""

    cap_by_date_window_days: int | None = None
    """`cap_by_date` 를 계산한 창의 길이 (물류 IO Contract §6 · #183).

    ★ **`inbound_lead_days` 와 짝이다.** 창의 시작이 `as_of + inbound_lead_days`,
      길이가 이 값이다. 둘 다 물류가 준다 (`logistics/tools.build_cap_window`).

    🔴 **이 값이 없으면 "키가 없는 날짜" 를 읽을 수 없다.** 물류가 정한 규약이
      셋으로 갈리는데, 창을 모르면 뒤의 둘을 못 가른다.

        키 존재 + 값 0    입고 가능량이 0 이다
        창 안인데 키 없음  계산 누락 또는 미결
        창 밖             계산 대상이 아니다   ← 0 도 무한대도 아니다

      가르지 못하면 마스터는 **무한대 쪽으로 틀린다** — 창 밖 도착이 어느 비교에도
      안 걸리고 조용히 통과한다."""

    # ★ v1.2 정정 — has_unmet_obligation 은 T0 필드가 아니다.
    #   부서는 자기 도메인 사실만 알 뿐 납품 의무 충족 여부를 판정할 위치가 아니다.
    #     매입 → no_proposal_reason      (유효 시나리오 없음 + 사유)
    #     영업 → today_floor · 부족분
    #     재고 → on_hand · 신선도·납기 제약
    #     S3   → has_unmet_obligation 산출 + E5 판정   ← 오케스트레이터
    #   매입의 no_proposal 이 곧 E5 가 아니다. 재고에 여유가 있으면 매입 0 이어도 납품 가능.

    @property
    def on_hand(self) -> tuple[InventoryLot, ...]:
        """오늘 팔 수 있는 재고. 사이클 B 판매 후보의 유일한 원천 (§3.1)."""
        return tuple(l for l in self.lots if not l.in_transit)

    @property
    def in_transit(self) -> tuple[InventoryLot, ...]:
        return tuple(l for l in self.lots if l.in_transit)


def gate_variant_axes(
    item_mix_ratio_amount: Mapping[ItemCode, float],
    split_entry_ok: bool = True,
    threshold: float = ITEM_CONCENTRATION_THRESHOLD,
) -> tuple[VariantAxis, ...]:
    """
    허용 축 게이팅 — T0 에서 한 번 계산해 전원에게 배포한다 (§3.5.1).

        quantity : 항상 허용
        timing   : 분할 진입 조건 충족 시 (총량 임계 초과 또는 지속 상승 궤적)
        mix      : item_mix_ratio 최대값 < item_concentration_threshold

    ⚠️ **v0.13 에서 timing 이 조건부가 되었다.** v0.2 는 항상 허용으로 두었다.
      mix 가 게이팅으로 빠진 상태에서 timing 까지 빠지면 허용 축이 quantity 하나뿐이고,
      그러면 붕괴 시 회송할 축이 없어 **그날은 무조건 단일안**이 된다.
      → build_feedback() 이 이 경우를 감지해 회송을 생략한다 (§3.5.2 연동).
    """
    axes: list[VariantAxis] = ["quantity"]
    if split_entry_ok:
        axes.append("timing")
    if item_mix_ratio_amount and max(item_mix_ratio_amount.values()) < threshold:
        axes.append("mix")
    return tuple(axes)


# ---------------------------------------------------------------------------
# 6. 시나리오 — v0.2: 4 → 8 필드
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitLeg:
    """
    분할 입고 1회분. C-1 (a) 총량 스칼라 + split_plan 별도 필드.

    ★ v1.2 §3.4.5-③ — expected_arrival_date 는 **매입이 회차별로 계산**한다.
      계산은 매입, 값(inbound_lead_days)은 재고가 공급한다 (N4 3자 공유).
      총량 기준 단일 도착일로 뭉치면 분할 매입의 창고 부담 분산 효과가 사라진다.
      N4 가 NULL 인 동안은 계산하지 않는다 — 0 으로 대체하면 "오늘 승인분이 오늘 도착"이
      되어 §3.2.3 의 on_hand 전환 금지가 무의미해진다 (§1.2-10).
    """

    offset_days: int
    qty_kg: Mapping[ItemCode, float]
    expected_arrival_date: date | None = None


@dataclass(frozen=True)
class SourcingLot:
    """등급별 조달 1건. 삼중 일치의 세 번째 축."""

    item: ItemCode
    grade: Grade
    qty_kg: float
    unit_price_krw_per_kg: float
    ref_ids: tuple[str, ...] = ()
    min_lot_kg: float | None = None
    """등급별 최소 거래 단위. None 이면 연속량으로 간주해 비례 축소한다.
       값이 있으면 T3 축소가 로트 배수 내림이 되고 금액 항등식에 잔차가 생긴다."""

    @property
    def amount_krw(self) -> float:
        return self.qty_kg * self.unit_price_krw_per_kg


class PurchaseScenario(Protocol):
    """
    ★ v0.2 정정 — 요구사항명세서 v0.1 §4.3 에서 "오케스트레이터에는 총량만
      넘어온다"고 적었으나 이는 틀렸다. 붕괴 판정이 split_plan 을 봐야 하기
      때문이다 (총량이 같고 분할만 다른 두 안을 붕괴로 오판하면 timing 축이
      무력화된다).
    """

    scenario_id: str
    strategy_type: str  # quantity | timing | mix (v1.2)
    stance: str  # 보수 | 기준 | 공격 (화면 라벨)
    qty_kg: Mapping[ItemCode, float]
    unit_price_krw_per_kg: Mapping[ItemCode, float]  # sourcing 가중평균 (파생)
    split_plan: tuple[SplitLeg, ...]
    sourcing_plan: tuple[SourcingLot, ...]
    price_basis: str
    total_amount_krw: float
    """★ v1.2 §3.4.5-④ — 재무 cap 과 대조되는 **유일한** 값.
       재무의 max_feasible_amount_krw 는 등급가를 가정하지 않는 순수 금액 상한이므로,
       등급 구성은 그 상한 안에서 매입이 짠다. 재무는 어떤 단가도 가정하지 않는다."""


@dataclass(frozen=True)
class MinimalScenario:
    scenario_id: str
    strategy_type: str  # quantity | timing | mix
    stance: str  # 보수 | 기준 | 공격
    qty_kg: Mapping[ItemCode, float]
    unit_price_krw_per_kg: Mapping[ItemCode, float]
    split_plan: tuple[SplitLeg, ...] = ()
    sourcing_plan: tuple[SourcingLot, ...] = ()
    price_basis: PriceBasis = DEFAULT_PRICE_BASIS
    rationale: str = ""
    evidences: tuple[Evidence, ...] = ()
    exceeds_budget: bool = False
    exceed_reason: str = ""

    @property
    def amount_krw(self) -> float:
        return sum(self.qty_kg[i] * self.unit_price_krw_per_kg[i] for i in self.qty_kg)


# ---------------------------------------------------------------------------
# 6.5 판매 배분 후보 — 사이클 B (v0.3 신설, 정의서 v0.13 §3.1)
# ---------------------------------------------------------------------------


# ★ v1.2.3 — 영업 IO 명세 v0.6 §5 의 채널 enum.
#   HOLD 는 **출고가 아니다.** 오늘 안 내보내고 들고 가기로 한 물량이다.
SALES_CHANNELS: tuple[str, ...] = ("KIMCHI_FACTORY", "SCHOOL_MEAL", "SPOT", "HOLD")
HOLD_CHANNEL = "HOLD"


@dataclass(frozen=True)
class OutboundLeg:
    """
    날짜별 출고 예정 1건 (v1.2.3 · 영업 IO 명세 §5 `outbound_by_date`).

    ★ **확정 납품분을 포함한다.** 전략 배분(legs)과 범위가 다르다.
      HOLD 는 출고가 아니므로 들어가지 않는다.
    """

    date: date
    qty_kg: float


# ★ v1.2.8 — 사이클 B 조언자(S2) 출력 계약. 갱신 PDF 물류 B / 재무 B 로 확정.
#   S3 가 판매 후보를 대조할 때 읽는다. 부서가 후보를 직접 판정하지는 않는다.

LotStatus = Literal["AVAILABLE", "RESERVED", "EXPIRED"]


@dataclass(frozen=True)
class LotConstraint:
    """
    물류 B 출력의 로트 1건 (PDF 물류 B OUTPUT `lot_constraints`).

    ★ S3 의 판매 후보 검증 입력이다 (04 §6 `validate_sales_candidate`).
      Critic B 의 L4-9(on_hand 초과)·L4-10(신선도·납기)이 이 값으로 재검산한다.
    """

    lot_id: str
    item: ItemCode
    available_qty_kg: float
    remaining_freshness_days: int
    status: LotStatus = "AVAILABLE"


@dataclass(frozen=True)
class CollectionPreference:
    """
    재무 B 출력의 채널 회수 선호 1건 (PDF 재무 B OUTPUT `collection_preferences`).

    ★ **배분 지시가 아니라 순위 신호다.** liquidity_rank 가 낮을수록 회수가 빠르다.
      재무는 특정 후보를 PASS/FAIL 하지 않는다 (통합 문서 §3). S3 가 참고한다.
    """

    channel_type: str
    partner_id: str | None
    settlement_days: int | None
    liquidity_rank: int


@dataclass(frozen=True)
class SalesFacts:
    """
    S1 이 제출하는 **사실 보고** (v1.2.3 · 영업 IO 명세 §5).

    ★ 영업은 판정하지 않는다. 의무량과 충당 가능량이라는 사실만 낸다.
      `has_unmet_obligation` 과 E5 판정은 매입·판매 전체를 보는 S3 의 몫이다
      (§5.0). 영업이 그 플래그를 내면 Critic 의 `E-AUTHORITY` 위반이다.
    """

    confirmed_obligation_kg: Mapping[ItemCode, float] = field(default_factory=dict)
    coverable_kg: Mapping[ItemCode, float] = field(default_factory=dict)
    no_feasible_reason: str | None = None


@dataclass(frozen=True)
class ChannelLeg:
    """채널 1곳에 대한 배분."""

    channel: str  # KIMCHI_FACTORY | SCHOOL_MEAL | SPOT | HOLD
    item: ItemCode
    qty_kg: float
    unit_price_krw_per_kg: float
    lot_ids: tuple[str, ...] = ()
    due_date: date | None = None

    @property
    def amount_krw(self) -> float:
        return self.qty_kg * self.unit_price_krw_per_kg

    @property
    def is_outbound(self) -> bool:
        """
        ★ v1.2.3 — HOLD 는 출고량에 넣지 않는다 (영업 IO 명세 §5).

          "보유(HOLD)는 출고가 아니라 제외"

          이 구분이 없으면 공용 출고 능력 결합 검사가 **보유하기로 한 물량까지
          출고로 세어** 실제보다 좁게 클리핑한다. 오늘 안 내보내기로 한 결정이
          출고 능력을 잡아먹는 셈이 된다.
        """
        return self.channel != HOLD_CHANNEL


class SaleAllocation(Protocol):
    """
    S1 산출물. 오케스트레이터가 의존하는 최소 인터페이스.

    ★ on_hand 만 쓴다. 오늘 승인된 매입은 보지 않는다 (§3.4.5-⑦).
      오늘 도착분은 T0 입고 처리에서 이미 반영됐다.
    """

    allocation_id: str
    strategy_type: str  # 소진 | 균형 | 보유
    legs: tuple[ChannelLeg, ...]
    expected_contribution_krw: float
    outbound_by_date: tuple[OutboundLeg, ...]
    """★ v1.2.3 — S3 공용 출고 결합 검사의 **정본 입력** (영업 IO 명세 §5).

    legs 를 합산해 유추하지 않는다. 두 값의 범위가 다르기 때문이다.

        legs             전략 가능 재고의 배분. **확정 주문분을 포함하지 않는다**
        outbound_by_date 그날의 총 출고량. **확정 납품분을 포함한다**. HOLD 는 제외

    legs 합으로 결합 검사를 하면 확정 납품분이 빠져 출고 능력을 과소 계상한다."""


@dataclass(frozen=True)
class MinimalAllocation:
    allocation_id: str
    strategy_type: str
    legs: tuple[ChannelLeg, ...]
    expected_contribution_krw: float = 0.0
    rationale: str = ""
    outbound_by_date: tuple[OutboundLeg, ...] = ()
    estimation_confidence: str = ""  # HIGH | MEDIUM | LOW — 반응 추정 신뢰도

    @property
    def qty_by_item(self) -> dict[ItemCode, float]:
        """배분 총량. **HOLD 포함** — 전략 가능 재고와 대조할 때 쓴다."""
        out: dict[ItemCode, float] = {}
        for leg in self.legs:
            out[leg.item] = out.get(leg.item, 0.0) + leg.qty_kg
        return out

    @property
    def outbound_qty_by_item(self) -> dict[ItemCode, float]:
        """
        실제 출고량. **HOLD 제외** (v1.2.3).

        ★ 클리핑과 출고 능력 검사는 반드시 이쪽을 쓴다.
          qty_by_item 을 쓰면 오늘 안 내보내기로 한 물량이 출고 능력을 잡아먹는다.
        """
        out: dict[ItemCode, float] = {}
        for leg in self.legs:
            if not leg.is_outbound:
                continue
            out[leg.item] = out.get(leg.item, 0.0) + leg.qty_kg
        return out

    @property
    def total_kg(self) -> float:
        return sum(l.qty_kg for l in self.legs)


# ---------------------------------------------------------------------------
# 6.6 사이클 간 상태 전달 계약 (v1.1 신설 · §3.2.3) 🔴
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrivalLeg:
    """
    승인 약정의 도착 1회분 (v1.2.1 신설). `CashFlowLeg` 의 입고 버전이다.

    split_index 는 원안 split_plan 의 회차 번호다. 승인 후 실제 입고와 대조할 때
    "몇 회차가 늦었는가"를 추적하려면 순번이 남아 있어야 한다.
    """

    date: date
    qty_kg: float
    split_index: int


@dataclass(frozen=True)
class ApprovedPurchaseCommitment:
    """
    §1.2-9 는 "T0 스냅샷이 유일한 데이터 출처"라고 규정한다.
    그런데 **H1 승인 결과는 T0 이후에 생긴 새 사실이다.**
    사이클 B 가 이를 어떻게 반영하는지가 v0.13 까지 비어 있었다.

        T0 Snapshot    = Base State 정본. 절대 수정하지 않는다
        H1 Commitment  = 같은 날 새로 확정된 Delta
        사이클 B State = T0 Snapshot + H1 Commitment overlay

    ★ **DB 재조회가 아니라 Delta 합성**이므로 §1.2-9 를 위반하지 않는다.
      H1 승인분을 반영하려고 DB 를 다시 읽으면 위반이다.
    """

    approval_id: str
    as_of: date
    total_amount_krw: float
    total_qty_kg: float
    payment_date: date | None  # as_of + purchase_payment_days (N5 미결 → None)
    expected_arrival_date: date | None  # **최초** 도착일. 상세는 arrival_schedule (v1.2.1)
    source_scenario_id: str
    ref_ids: tuple[str, ...] = ()
    payment_schedule: tuple[CashFlowLeg, ...] = ()

    arrival_schedule: tuple[ArrivalLeg, ...] = ()
    """★ v1.2.1 신설 — payment_schedule 과 대칭.

    v1.2 는 도착일이 **단수**였다. 지급은 복수 스케줄인데 도착은 하나였다.
    timing 축(분할 매입) 안이 승인되면 도착이 여러 날에 나뉘는데 스키마가 담지 못해,
    사이클 B overlay 에서 **전량이 최초 도착일 하루에 들어온 것으로 계산**됐다.
    분할 매입이 창고 부담을 분산시키는 효과가 통째로 사라진다 (§3.6.5 위반).

    expected_arrival_date 는 하위 호환을 위해 남기되 **최초 도착일**로 정의한다."""

    def __post_init__(self) -> None:
        if not self.ref_ids:
            raise ContractViolation("Commitment 에도 ref_ids 필수 (§1.2-5).")
        if self.arrival_schedule:
            total = sum(a.qty_kg for a in self.arrival_schedule)
            if abs(total - self.total_qty_kg) > IDENTITY_TOL_KG:
                raise ContractViolation(
                    f"arrival_schedule 수량 합 {total:,.1f}kg ≠ total_qty_kg "
                    f"{self.total_qty_kg:,.1f}kg — 회차별 도착이 총량과 어긋난다."
                )


@dataclass(frozen=True)
class CycleBState:
    """
    사이클 B 가 읽는 상태. **스냅샷은 불변이고 commitment 가 겹쳐진다.**

    부서별 overlay 범위 (§3.2.3) — 무엇을 반영하고 무엇을 제외하는가

    | 부서 | overlay 한다 | 하지 않는다 |
    |---|---|---|
    | 재무 | payment_date 현금 유출 · post_h1_projected_cash_min | 예상 판매액·매출채권 |
    | 재고 | expected_arrival 기준 in_transit 추가 · cap_by_date 갱신 | **on_hand 전환** |
    | 영업 | — | 전부 (오늘 판매 후보는 on_hand 만) |

    ★ **재고의 on_hand 전환 금지가 핵심이다.** H1 에서 승인해도 물건은
      inbound_lead_days 뒤에 도착한다. 이를 on_hand 에 넣으면 아직 오지 않은
      물량이 오늘의 판매 후보가 되어 납기 위반으로 이어진다.
    """

    snapshot: T0Snapshot
    commitment: ApprovedPurchaseCommitment | None = None

    @property
    def on_hand(self) -> tuple[InventoryLot, ...]:
        """★ commitment 와 무관하다. 오늘 승인분은 오늘 도착하지 않는다."""
        return self.snapshot.on_hand

    @property
    def in_transit(self) -> tuple[InventoryLot, ...]:
        """★ v1.2.1 — arrival_schedule 이 있으면 **회차별로** 로트를 만든다.

        총량 하나로 뭉치면 2회차(D+5) 도착분까지 최초 도착일에 들어온 것이 되어
        분할 매입의 창고 부담 분산 효과가 사라진다."""
        base = list(self.snapshot.in_transit)
        c = self.commitment
        if c is None:
            return tuple(base)
        if c.arrival_schedule:
            for leg in c.arrival_schedule:
                base.append(
                    InventoryLot(
                        lot_id=f"commit_{c.approval_id}_{leg.split_index}",
                        item="(승인분)",
                        qty_kg=leg.qty_kg,
                        freshness_days_left=0,
                        in_transit=True,
                        eta=leg.date,
                    )
                )
        elif c.expected_arrival_date is not None:
            base.append(
                InventoryLot(
                    lot_id=f"commit_{c.approval_id}",
                    item="(승인분)",
                    qty_kg=c.total_qty_kg,
                    freshness_days_left=0,
                    in_transit=True,
                    eta=c.expected_arrival_date,
                )
            )
        return tuple(base)

    @property
    def committed_outflow_krw(self) -> float:
        """재무 overlay — 승인된 것만 확정 유출로 본다."""
        return self.commitment.total_amount_krw if self.commitment else 0.0

    def cap_by_date_overlay(self, base: Mapping[date, float]) -> dict[date, float]:
        """재고 overlay — 도착 예정일 이후 날짜의 여유를 줄인다.
           S2 에서 보유(HOLD) 후보의 창고 여유를 판단할 때 필요하다.

        ★ v1.2.1 — 회차별 누적으로 차감한다. D+2 에 절반, D+5 에 나머지가 도착하면
          D+2~D+4 는 절반만 차감되어야 한다. 전량을 최초 도착일부터 빼면
          **실제보다 창고를 좁게 보고 보유 판단이 과도하게 보수적이 된다.**"""
        c = self.commitment
        if c is None:
            return dict(base)
        if c.arrival_schedule:
            return {
                d: v - sum(a.qty_kg for a in c.arrival_schedule if a.date <= d)
                for d, v in base.items()
            }
        if c.expected_arrival_date is None:
            return dict(base)
        return {
            d: (v - c.total_qty_kg if d >= c.expected_arrival_date else v) for d, v in base.items()
        }

    def provenance(self) -> dict[str, str | None]:
        """§3.2.3 추적성 — 어느 스냅샷과 어느 승인에 기반했는지 전 산출물에 기록."""
        return {
            "snapshot_id": self.snapshot.snapshot_id,
            "approval_id": self.commitment.approval_id if self.commitment else None,
            "policy_version": self.snapshot.policy_version,
        }


def compute_sales_cash_priority(state: CycleBState, base_priority: str) -> dict[str, str]:
    """
    §7.4.1 — 영업이 쓰는 것은 `sales_cash_priority` 다.

    H1 에서 대규모 선매입이 승인되면 그만큼 미래 현금이 묶이므로 판매 시점의
    현금 시급도가 T0 시점과 달라진다. **두 값을 같은 이름으로 덮어쓰지 않고**
    basis 를 함께 담는다. H1 승인 매입이 없으면 두 값이 같을 수 있지만,
    그래도 basis 는 정확히 기록한다.
    """
    basis = "POST_H1_COMMITMENT" if state.commitment else "BASE_T0"
    priority = base_priority
    if state.commitment:
        fin = state.snapshot.finance
        remaining = fin.purchasable_krw - state.committed_outflow_krw
        if remaining < fin.minimum_operating_cash_krw:
            priority = "HIGH"
    out = {"cash_priority": priority, "basis": basis}
    out.update({k: v for k, v in state.provenance().items() if v})
    return out


# ---------------------------------------------------------------------------
# 7. 삼중 일치 불변조건 (검토의견 §3-2)
# ---------------------------------------------------------------------------

IDENTITY_TOL_KG = 0.5
IDENTITY_TOL_KRW = 1.0


def check_triple_identity(
    qty_kg: Mapping[ItemCode, float],
    split_plan: Sequence[SplitLeg],
    sourcing_plan: Sequence[SourcingLot],
    amount_krw: float | None = None,
    tol_kg: float = IDENTITY_TOL_KG,
    tol_krw: float = IDENTITY_TOL_KRW,
) -> list[str]:
    """
        수량:  total == Σ split == Σ sourcing
        금액:  total_amount == Σ(sourcing.qty × grade_unit_price)   ← v0.2 신설

    위반 사유 리스트를 반환한다. 빈 리스트면 통과.

    ★ 이 함수는 self_check(매입 주장값)와 Critic(DB 재조회값)이 **같이** 쓴다.
      계약서 §6.4 — 룰은 공유, 입력만 분리.
    ★ Critic 은 **원안이 아니라 클리핑된 값**에 대해 호출해야 한다.
      원안에 대고 검사하면 클리핑되는 날마다 FAIL 이 난다 (B1).
    """
    problems: list[str] = []

    for i, q in qty_kg.items():
        if split_plan:
            s = sum(leg.qty_kg.get(i, 0.0) for leg in split_plan)
            if abs(s - q) > tol_kg:
                problems.append(f"수량 항등식 위반 [{i}]: total {q:,.1f} ≠ Σsplit {s:,.1f}")
        if sourcing_plan:
            g = sum(lot.qty_kg for lot in sourcing_plan if lot.item == i)
            if abs(g - q) > tol_kg:
                problems.append(f"수량 항등식 위반 [{i}]: total {q:,.1f} ≠ Σsourcing {g:,.1f}")

    if amount_krw is not None and sourcing_plan:
        a = sum(lot.amount_krw for lot in sourcing_plan)
        if abs(a - amount_krw) > tol_krw:
            problems.append(f"금액 항등식 위반: total {amount_krw:,.0f}원 ≠ Σsourcing {a:,.0f}원")

    return problems


# ---------------------------------------------------------------------------
# 8. 밴드 / 클리핑 결과
# ---------------------------------------------------------------------------

DeadlockCode = Literal["DEADLOCK_ITEM", "DEADLOCK_SPACE", "DEADLOCK_CASH"]


@dataclass(frozen=True)
class Band:
    floor_kg: Mapping[ItemCode, float]
    cap_kg: Mapping[ItemCode, float]
    cap_total_kg: float
    cap_amount_krw: float
    contributors: Mapping[str, str]
    cap_by_date_kg: Mapping[date, float] = field(default_factory=dict)  # v0.3 · N15

    not_ready: tuple[Dept, ...] = ()
    """★ v1.2.4 — runtime_status 가 READY 가 아니었던 부서.

    비어 있지 않으면 **이 밴드로 클리핑하면 안 된다.** 그 부서의 상한이 통째로
    빠진 상태이므로 cap 이 실제보다 넓다. 재고가 죽은 날 무제한 매입이 통과하는
    것을 막는 장치다. T3 는 이를 교착이 아니라 E4(미시작)로 다룬다 —
    회사 상태 문제가 아니라 실행 환경 문제다."""

    @property
    def usable(self) -> bool:
        """클리핑에 쓸 수 있는 밴드인가."""
        return not self.not_ready

    def width_kg(self, item: ItemCode) -> float:
        return self.cap_kg[item] - self.floor_kg[item]

    @property
    def floor_total_kg(self) -> float:
        return sum(self.floor_kg.values())

    @property
    def aggregate_slack_kg(self) -> float:
        """집계 여유. 이 값이 작으면 재생성해도 다양성이 나올 수 없다 (B5)."""
        return self.cap_total_kg - self.floor_total_kg


@dataclass(frozen=True)
class OutboundBand:
    """
    사이클 B 의 밴드 (v1.2.2 신설 · §3.7.4). **cap 만 있고 floor 는 없다.**

    ★ Band 를 재사용하지 않는 이유. 축의 모양이 다르다.

        A(T3)  floor(kg) ≤ 수량 ≤ cap(kg) · 금액 ≤ cap(원)   ← 단위가 섞여 환산이 난점
        B(S3)  Σ배분 ≤ 하루 공용 출고 능력 · 배분[i] ≤ on_hand[i]  ← 전부 kg

      Band 에는 cap_amount_krw · cap_by_date_kg 처럼 B 에서 의미 없는 축이 있고,
      억지로 끼우면 "이 필드는 B 에서 안 쓴다"는 주석이 계약 전체에 번진다.

    ★ 왜 floor 가 없는가.
      "오늘 안 파는 것도 정상 결정"이다 (§5.0 — 매입 정상 + 판매 0 → E1).
      확정 납품 의무는 floor 처럼 보이지만 성격이 다르다. 못 채우면 클리핑으로
      끌어올릴 수 있는 것이 아니라 **E5(계약 위반 선언)** 로 가는 신호다.
      밴드에 넣으면 "조금 더 팔면 되는 문제"로 잘못 다뤄진다.
    """

    cap_kg: Mapping[ItemCode, float]
    """품목별 상한. on_hand + 신선도 창 (재고 S2)."""

    cap_total_kg: float
    """하루 공용 출고 능력. **사이클 B 의 유일한 결합 제약** (§3.7.4 검사 3번)."""

    contributors: Mapping[str, str] = field(default_factory=dict)

    soft_notes: tuple[str, ...] = ()
    """재무 S2 의 회수 시급도·채널 조건 경고. 밴드를 움직이지 않고 H2 표시로만 흐른다."""

    @property
    def cap_total_effective_kg(self) -> float:
        """공용 출고 능력과 품목별 합 중 작은 쪽. 실제로 나갈 수 있는 최대치."""
        return min(self.cap_total_kg, sum(self.cap_kg.values()))


@dataclass(frozen=True)
class Deadlock:
    code: DeadlockCode
    detail: str
    item: ItemCode | None
    shortfall: float
    unit: str
    responsible_checks: tuple[str, ...]


# constraints.yaml 등재 대상 임계 3종
OVER_CLIP_RATIO = 0.30  # C-4 과도 클리핑 경고
VARIANT_SPREAD_MIN = 0.15  # 검토의견 §3-4 붕괴 판정
STRUCTURAL_SLACK_MIN = 0.15  # B5 구조적 협소 밴드


@dataclass(frozen=True)
class ClipResult:
    """
    ★ v0.2 핵심 변경 — 하위 계획도 함께 축소한다.

      v0.1 은 total_qty 만 잘랐다. 그러면 split_plan · sourcing_plan 이
      원안 그대로 남아 삼중 일치가 깨지고, Critic L1 이 FAIL 을 내며,
      **클리핑이 발생하는 모든 날이 보류로 끝난다.**

      축소는 상수 비율 곱셈이므로 §1.2-3(LLM 숫자 생성 금지) 대상이 아니다.
      검토의견 §1-3 이 C-3(클리핑 후 마진 재계산)에 대해 인정한 논리와 동일하며,
      ref_id 도 그대로 유지된다.
    """

    scenario_id: str
    qty_kg: Mapping[ItemCode, float]  # 원안 — 반드시 보존
    clipped_qty_kg: Mapping[ItemCode, float]
    clipped_split_plan: tuple[SplitLeg, ...] = ()
    clipped_sourcing_plan: tuple[SourcingLot, ...] = ()
    clipped_amount_krw: float = 0.0
    binding_constraints: tuple[str, ...] = ()
    identity_problems: tuple[str, ...] = ()
    lot_residual_kg: float = 0.0  # min_lot_kg 내림에서 생긴 잔차
    floor_broken: tuple[ItemCode, ...] = ()  # 로트 내림으로 floor 를 못 지킨 품목
    infeasible: bool = False

    @property
    def clipped(self) -> bool:
        return any(abs(self.qty_kg[i] - self.clipped_qty_kg[i]) > 1e-6 for i in self.qty_kg)

    @property
    def total_kg(self) -> float:
        return sum(self.clipped_qty_kg.values())

    @property
    def original_total_kg(self) -> float:
        return sum(self.qty_kg.values())

    @property
    def clip_ratio(self) -> float:
        o = self.original_total_kg
        return 1.0 if o <= 0 else self.total_kg / o

    @property
    def over_clipped(self) -> bool:
        """C-4 — 드랍하지 않고 경고만 낸다 (§1.2-7 충돌 회피)."""
        return self.clip_ratio < OVER_CLIP_RATIO

    def signature(self) -> tuple:
        """붕괴 판정용 지문. 수량 벡터 + 분할 구조를 함께 본다."""
        qty = tuple(sorted((i, round(v, 1)) for i, v in self.clipped_qty_kg.items()))
        split = tuple(
            (leg.offset_days, tuple(sorted((i, round(v, 1)) for i, v in leg.qty_kg.items())))
            for leg in self.clipped_split_plan
        )
        return (qty, split)


# ---------------------------------------------------------------------------
# 9. 사전 feedback 힌트
# ---------------------------------------------------------------------------

FeedbackReason = Literal["ALL_CLIPPED", "VARIANT_COLLAPSED", "ALL_REJECTED", "STRUCTURAL_NARROW"]


@dataclass(frozen=True)
class FeedbackHint:
    reason_code: FeedbackReason
    target_axis: str
    band_floor_kg: Mapping[ItemCode, float]
    band_cap_kg: Mapping[ItemCode, float]
    violated: tuple[str, ...]
    shortfall: float
    unit: str
    message: str = ""


# ---------------------------------------------------------------------------
# 10. Critic
# ---------------------------------------------------------------------------

CriticLayer = Literal["L1_hard", "L2_evidence", "L3_band_axis", "L3_5_grade", "L4_rationale"]
FailRoute = Literal["T1_purchase", "T2_dept", "T3_combine", "T3_rationale_only"]

FAIL_ROUTING: dict[CriticLayer, FailRoute] = {
    "L1_hard": "T1_purchase",
    "L2_evidence": "T2_dept",
    "L3_band_axis": "T3_combine",
    "L3_5_grade": "T2_dept",
    "L4_rationale": "T3_rationale_only",  # ★ 숫자는 건드리지 않는다
}


@dataclass(frozen=True)
class CriticFinding:
    layer: CriticLayer
    check_id: str
    detail: str
    ref_ids: tuple[str, ...] = ()
    dept: Dept | None = None

    @property
    def route(self) -> FailRoute:
        return FAIL_ROUTING[self.layer]


@dataclass(frozen=True)
class CriticVerdict:
    as_of: date
    run_seq: int
    scenario_id: str
    passed: bool
    findings: tuple[CriticFinding, ...]
    llm_note: str = ""
    decided_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def route(self) -> FailRoute | None:
        order: Sequence[CriticLayer] = (
            "L1_hard",
            "L2_evidence",
            "L3_band_axis",
            "L3_5_grade",
            "L4_rationale",
        )
        for layer in order:
            for f in self.findings:
                if f.layer == layer:
                    return f.route
        return None


# ---------------------------------------------------------------------------
# 10.5 하루 종료 판정 (§5.0 · §5.1)
#
# ★ v1.2.1 — 이 블록(EndCode · BacktestEnd · resolve_end_code)이 아래 '11. cycle_log'
#   절에 통째로 한 번 더 정의돼 있었다. 본문 로직은 같고 docstring 만 달라
#   동작에는 영향이 없었으나, **한쪽만 고치면 반영되지 않는 함정**이라 중복을 걷어냈다.
# ---------------------------------------------------------------------------

EndCode = Literal[
    "E1_APPROVED",
    "E2_HELD",
    "E3_REJECTED",
    "E4_NOT_STARTED",
    "E5_NO_FEASIBLE_PLAN",  # v0.3 — 계약 납품 의무를 채울 방법이 0 (§5.0)
]
BacktestEnd = Literal["COMPLETED", "BANKRUPT", "DEADLOCKED", "MANUAL_STOP"]


def compute_has_unmet_obligation(
    confirmed_obligation_kg: Mapping[ItemCode, float],
    fulfilled_kg: Mapping[ItemCode, float],
) -> bool:
    """
    ★ v1.2 정정 — **산출 주체는 오케스트레이터(S3)다.**

        has_unmet_obligation = (확정 납품 의무 > 0) AND (승인된 A·B로 충족 불가)

    부서는 자기 도메인 사실만 반환한다. 매입의 `no_proposal` 이 곧 E5 를 뜻하지
    않는다 — 재고에 여유가 있으면 매입 0 이어도 납품은 가능하다.
    """
    if not confirmed_obligation_kg:
        return False
    return any(
        fulfilled_kg.get(i, 0.0) < q - 1e-6 for i, q in confirmed_obligation_kg.items() if q > 0
    )


def is_bankrupt(projected_cash_min_krw: float, minimum_cash_balance_krw: float) -> bool:
    """
    ★ v1.2 — **파산선은 0원이 아니다** (유저플로우 §⑥-5).
      FIN-H01 이 projected_cash_min ≥ minimum_cash_balance 이므로,
      현재 정책안(1개월 급여 Reserve) 기준 12,941,280원이 판정선이다.
      0 원으로 잡으면 급여를 못 주는 상태를 정상으로 통과시킨다.
    """
    return projected_cash_min_krw < minimum_cash_balance_krw


def resolve_end_code(
    *,
    approved: bool,
    base_state_violated: bool,
    has_unmet_obligation: bool,
    both_cycles_empty: bool,
) -> EndCode:
    """
    §5.0 + §5.1 판정. **E5 판정 주체는 오케스트레이터(S3)다.**

    | 상황                                   | 코드 |
    |---|---|
    | 매입 0 + 판매 정상                      | E1 — 사지 않는 것도 정상 결정 |
    | 매입 정상 + 판매 0                      | E1 — 오늘 안 파는 것도 정상 결정 |
    | 매입 0 + 판매 0 + 확정 납품 의무 없음   | E2 보류 |
    | 매입 0 + 판매 0 + 확정 납품 의무 있음   | **E5** |
    | BASE 상태 자체가 하드 제약 위반         | E3 (+ 백테스트 종료 검토) |

    ★ E5 는 AI 에게 넘기지 않는다. 계약 의무를 지킬 수 없는 상황을 골라 달라고
      요청하는 것은 하드 제약의 예외를 LLM 이 만들게 하는 것과 같다.
    """
    if base_state_violated:
        return "E3_REJECTED"
    if approved:
        return "E1_APPROVED"
    if both_cycles_empty:
        return "E5_NO_FEASIBLE_PLAN" if has_unmet_obligation else "E2_HELD"
    return "E1_APPROVED"


# ---------------------------------------------------------------------------
# 11. cycle_log
# ---------------------------------------------------------------------------


CollapseType = Literal["AXIS", "QUANTITY"]
EndStage = Literal["T1", "T2", "T3", "H1", "S1", "S2", "S3", "H2"]


@dataclass(frozen=True)
class AdjustAttempt:
    """
    조정 1회분의 기록 (v1.2.7 신설).

    ★ 왜 밴드가 아니라 시도를 기록하는가.
      v1.2.6 에서 부서 회신이 하루 한 번으로 고정되면서 **밴드는 하루의 상수**가 됐다.
      `combine_band` 는 replies 만의 순수 함수이므로 회송해도 같은 값이 나온다.
      회송마다 달라지는 것은 밴드가 아니라 **매입 시나리오와 그 클리핑 결과**다.

      그래서 밴드는 한 번만 결합해 정본으로 두고(`SubcycleLog.band`),
      회송 이력은 여기에 남긴다. 밴드를 회송 횟수만큼 덮어쓰면
      "그날의 제약이 무엇이었나"가 마지막 값 하나로 뭉개진다.
    """

    seq: int
    trigger: Literal["INITIAL", "PRE_LOOP", "POST_CRITIC"]
    scenario_ids: tuple[str, ...] = ()
    total_kg_by_id: Mapping[str, float] = field(default_factory=dict)
    binding: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class SubcycleLog:
    """한 사이클의 실행 기록. cycle_log 의 a_ / b_ 컬럼군에 대응한다."""

    pre_loop_used: int = 0
    post_loop_used: int = 0
    approved_id: str | None = None
    candidate_count: int = 0
    allowed_axes: tuple[str, ...] = ()
    allowed_axes_excluded: Mapping[str, str] = field(default_factory=dict)
    """축이 제외된 이유. UI A-10 이 '왜 품목 분산 안이 없는가'를 보여주려면 필요하다.
       예: {"mix": "item_mix_ratio 최대 81.2% > 70%"}"""
    variant_collapsed: bool = False
    collapse_type: CollapseType | None = None
    """AXIS = 전 안이 같은 축 / QUANTITY = 클리핑 후 수량 수렴 (유저플로우 §⑧)"""
    band: Band | None = None
    deadlock: Deadlock | None = None
    clip_results: tuple[ClipResult, ...] = ()
    """★ v1.2.7 — **완료된(최종) 클리핑 결과**만 담는다. 중간 시도는 attempts 에 있다."""

    attempts: tuple[AdjustAttempt, ...] = ()
    """조정 회차별 이력. 회송이 몇 번 돌았고 무엇이 달라졌는지 남는다."""
    critic_verdicts: tuple[CriticVerdict, ...] = ()
    over_clipped_ids: tuple[str, ...] = ()
    structural_narrow: bool = False
    llm_calls: int = 0
    recommended_id: str | None = None
    override_reason: str | None = None
    """★ UI §5 approval — 추천 ≠ 선택일 때 **필수**. 없으면 승인 자체를 거부한다.
       사람이 시스템을 뒤집은 이유가 남아야 백테스트 후 개입 패턴을 분석할 수 있다."""
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


@dataclass
class CycleLog:
    """
    ★ v1.2 구조 변경 — **하루에 한 행**이다 (유저플로우 v1.4 §⑧).
      v1.1 은 사이클마다 한 행씩 두 행을 썼는데, 그러면 "그날 최종 어떻게 끝났나"가
      두 행에 흩어져 end_code 가 두 개가 된다. a_ / b_ 컬럼군으로 합친다.
    """

    as_of: date  # PK
    run_seq: int = 1  # TR-2 재실행 대비 (§10.3-17)
    end_code: EndCode | None = None
    end_reason: str = ""
    end_cycle: Literal["A", "B", "NONE"] = "NONE"
    """★ 같은 E2 라도 조달에서 막힌 날과 판매에서 막힌 날은 원인도 대응도 완전히 다르다.
       이 구분이 없으면 blocking_agent 분포가 무의미해진다."""
    end_stage: EndStage | None = None

    a: SubcycleLog = field(default_factory=SubcycleLog)
    b: SubcycleLog = field(default_factory=SubcycleLog)

    progress_halted: bool = False
    blocking_agent: str | None = None
    blocking_axis: str | None = None
    single_option_reason: str | None = None
    base_state_violated: bool = False
    has_unmet_obligation: bool = False  # ★ S3 가 산출한다 (v1.2 정정)
    policy_version: str = ""
    snapshot_id: str = ""
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None

    def upsert_key(self) -> tuple[date, int]:
        return (self.as_of, self.run_seq)

    def side(self, cycle: Cycle) -> SubcycleLog:
        return self.a if cycle == "A" else self.b

    @property
    def llm_calls(self) -> int:
        return self.a.llm_calls + self.b.llm_calls

    def note(self, msg: str) -> None:
        self.a.note(msg)


# ---------------------------------------------------------------------------
# 11.5 승인 규약 — H1 · H2 공통 (UI v1.3 §5 approval 테이블)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalDecision:
    """
        사람이 추천과 다른 안을 고르면
          추천안 · 실제 선택 · 뒤집은 이유를 나란히 기록한다

    ★ H1·H2 의 승인 규약이 동일하므로 같은 타입을 두 사이클에 쓴다.
      approval 테이블에 cycle 컬럼이 있는 이유도 같다 (UI §5).
    """

    cycle: Cycle
    recommended_id: str | None
    selected_id: str | None  # None = 전안 반려
    override_reason: str | None = None
    approved_by: str = ""

    def __post_init__(self) -> None:
        overridden = (
            self.selected_id is not None
            and self.recommended_id is not None
            and self.selected_id != self.recommended_id
        )
        if overridden and not self.override_reason:
            raise ContractViolation(
                f"추천안({self.recommended_id}) 과 다른 안({self.selected_id}) 을 선택했으면 "
                f"override_reason 이 필수다 (UI §5). 사람이 시스템을 뒤집은 이유가 남아야 "
                f"백테스트 후 개입 패턴을 분석할 수 있다."
            )


# ---------------------------------------------------------------------------
# 12. 파이프라인 상태 — 노드 시그니처에 db 인자가 없는 것이 §5.1 의 구현체다
# ---------------------------------------------------------------------------


@dataclass
class PipelineState:
    snapshot: T0Snapshot
    scenarios: list[Any] = field(default_factory=list)
    replies: dict[Dept, T2Reply] = field(default_factory=dict)
    band: Band | None = None
    outbound_band: OutboundBand | None = None  # v1.2.2 — 사이클 B 전용 (S3)
    deadlock: Deadlock | None = None
    clip_results: list[ClipResult] = field(default_factory=list)
    variant_collapsed: bool = False
    structural_narrow: bool = False
    feedback: FeedbackHint | None = None
    ranked_ids: list[str] = field(default_factory=list)
    critic: CriticVerdict | None = None
    approved_scenario_id: str | None = None
    cycle_b_state: CycleBState | None = None  # v1.1 — B 사이클에서만 채워짐
    sales_facts: SalesFacts | None = None  # v1.2.3 — S1 사실 보고 (E5 입력)
    log: CycleLog = None  # type: ignore

    def __post_init__(self) -> None:
        if self.log is None:
            self.log = CycleLog(as_of=self.snapshot.as_of, run_seq=self.snapshot.run_seq)


__all__ = [n for n in dir() if not n.startswith("_")]
