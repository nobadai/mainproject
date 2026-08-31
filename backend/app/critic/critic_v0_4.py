# ─────────────────────────────────────────────────────────────────────────────
# STATUS: TOOL 후보 — 유지 (2026-08-26)
#   6레이어 56검사. 정의서 §3.7.1 — **마스터가 직접 가진 검증 Tool** 이 된다.
#   → `app/master/flow.py` 의 `VerifierPort` 에 주입될 구현. 파일은 그대로 둔다.
#   ⚠️ 삭제 대상이 아니다. 마스터 구조에서 오히려 호출 지점이 늘어난다(④ 실행 계획 온전성).
# ─────────────────────────────────────────────────────────────────────────────
"""
critic_v0_4.py — Critic 설계서 v0.4 구현 (기준: 프로젝트_정의서_v1.2 · 유저플로우_v1.4 · UI_v1.3)

기존 `critic.py`(v0.3 계열, L1~L4)를 **대체하지 않고 감싼다.**
v0.4 설계서가 요구하는 6레이어(L0~L5)로 재배치하고, 설계서 §3 의 신설 3종과
§1 의 불일치 대응(CONCERN · CRITIC_A/B · 회차별 도착일)을 얹는다.

  L0  형식 · 구조             코드  6
  L1  바인딩 · 출처 · 권한     코드 13   ← E-AUTHORITY · E-GRADE-LEAK 신설
  L2  as_of 룩어헤드          코드  4
  L3  하드 제약               코드 17   ← 기존 critic.run_l1 재사용
  L4  결합 재검산             코드 10   ← 회차별 도착일 분해 신설
  L5  논리 일관성             LLM   6   ← 기존 critic.run_l4. FAIL 대신 CONCERN

────────────────────────────────────────────────────────────────────────────
★ contracts_core.py 는 FROZEN 이므로 건드리지 않는다.

  설계서가 요구하는 `CheckResult.inputs_used` 와 부서 산출 필드 목록은
  계약에 없다. 계약을 고치는 대신 **사이드카**(`DeptMeta`)로 받는다.
  계약 개정(v1.3)이 이뤄지면 DeptMeta 를 걷어내고 CheckResult 를 직접 읽으면 된다.
  개정 대상 목록은 이 파일 맨 아래 `CONTRACT_AMENDMENTS` 에 있다.
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.critic.critic import (
    EvidenceResolver,
    RationaleJudge,
    check_constraint_independence,
    check_evidence_grade,
    check_identity_on_clipped,
    check_price_basis_consistency,
)
from app.critic.critic import (
    run_l1 as _run_hard_recheck,
)
from app.critic.critic import (
    run_l2 as _run_evidence_match,
)
from app.critic.critic import (
    run_l4 as _run_llm_rationale,
)
from app.orchestrator.band import check_occupancy_by_date, detect_collapse_type
from app.orchestrator.contracts_core import (
    _DEPT_AXES,
    ITEMS,
    ApprovedPurchaseCommitment,
    Band,
    CheckResult,
    ClipResult,
    CriticFinding,
    Dept,
    ItemCode,
    LotConstraint,
    OutboundBand,
    PurchaseScenario,
    SaleAllocation,
    T0Snapshot,
    T2Reply,
)

EPS = 1e-6

# ---------------------------------------------------------------------------
# 0. v0.4 확장 타입 — 계약 개정 전까지의 사이드카
# ---------------------------------------------------------------------------

CriticStatus = Literal["PASS", "CONCERN", "FAIL"]
"""★ 설계서 §1 불일치 ③ — UI v1.3 은 PASS 만 정의한다.
   L5(LLM)는 결정을 죽일 수 없으므로(§4.3) FAIL 이 아니라 CONCERN 으로 올린다.
   화면에 자리가 없으면 사람은 Critic 이 무엇을 걸었는지 모른 채 승인하게 된다."""

# ★ 설계서 §1 불일치 ② — EndStage 값 집합에 Critic 이 없다.
#   "조정이 안 됐다"(T3)와 "검증에서 걸렸다"(Critic)는 원인도 대응도 다르다.
#   T3 로 뭉치면 blocking_agent 분포가 흐려진다.
CriticEndStage = Literal["CRITIC_A", "CRITIC_B"]

# 설계서 §3.2 — 재무 금액 cap 산출에 끼어들면 안 되는 입력
FORBIDDEN_IN_FINANCE_CAP: frozenset[str] = frozenset(
    {
        "grade_unit_price",
        "qty_kg",
        "total_qty_kg",
        "avg_unit_price",
        "sourcing_plan",
    }
)

# 설계서 §3.1 — S3(오케스트레이터) 전속 판정. 부서가 산출하면 위반이다.
S3_EXCLUSIVE_FIELDS: frozenset[str] = frozenset({"has_unmet_obligation"})

# ★ v1.2.5 — 정의서 §3.6.1 "각 부서는 **시나리오와 무관하게** 하루에 한 번만 회신한다"
#   T2 조언자가 매입 시나리오를 입력으로 읽으면 위반이다. 부서 무관하게 적용된다.
#
#   왜 전 부서인가. v1.2.4 까지는 재무만 막았는데(E-GRADE-LEAK), 조항은 부서를
#   가리지 않는다. 재고가 매입안을 보고 cap_by_date 를 내면 §3.6.7 의
#   "확정분만 반영"이 깨지고, 영업이 보면 명세 §1 의 구조 계약 위반이다.
FORBIDDEN_SCENARIO_INPUTS: frozenset[str] = frozenset(
    {
        "purchase_output",
        "scenarios",
        "sourcing_plan",
        "split_plan",
        "total_amount_krw",
        "total_quantity_ton",
        "coverage_days",
        "strategy_type",
    }
)

_STRATEGY_TYPES: frozenset[str] = frozenset({"quantity", "timing", "mix"})
# "기본" = 매입 명세 v1.1 label 어휘. 계약 stance("기준")와 동의어로 함께 수용한다.
_STANCES: frozenset[str] = frozenset({"보수", "기준", "기본", "공격"})


@dataclass(frozen=True)
class Issue:
    """CONCERN 으로 사람 승인 화면(A-10 · A-30)에 올라가는 항목."""

    code: str
    detail: str
    layer: str = "L5_logic"
    dept: Dept | None = None


@dataclass(frozen=True)
class DeptMeta:
    """
    계약(CheckResult)에 없는 두 가지를 부서가 별도로 제출한다.

    inputs_used    : {check_id: [입력 키...]}  — L1-7 재무 cap 등급 개입 탐지용
    produced_fields: 이 부서가 회신에 실제로 담은 필드 이름들 — L1-6 권한 침범 탐지용
    """

    inputs_used: Mapping[str, Sequence[str]] = field(default_factory=dict)
    produced_fields: Sequence[str] = ()


@dataclass(frozen=True)
class CriticVerdictV04:
    as_of: date
    run_seq: int
    scenario_id: str
    status: CriticStatus
    findings: tuple[CriticFinding, ...] = ()
    concerns: tuple[Issue, ...] = ()
    coverage: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    """레이어별 (실행된 검사 수, 정의된 검사 수). 화면 배지 'PASS (2/13)' 의 출처.

    ★ 설계서 §8 — 커버리지를 감추면 사람은 전 항목 통과로 읽는다.
      실제로는 미결(N-계열) 때문에 돌지 않은 검사가 대부분인 날이 있다."""
    skipped: tuple[str, ...] = ()
    llm_note: str = ""
    end_stage: CriticEndStage | None = None

    @property
    def passed(self) -> bool:
        """기존 graph.py 의 `verdict.passed` 와 호환. CONCERN 은 통과로 본다."""
        return self.status != "FAIL"

    @property
    def route(self):
        """기존 CriticVerdict.route 와 동일 규약. FAIL 이 아니면 회송 없음."""
        if self.status != "FAIL" or not self.findings:
            return None
        return self.findings[0].route

    @property
    def coverage_ratio(self) -> tuple[int, int]:
        ran = sum(r for r, _ in self.coverage.values())
        total = sum(t for _, t in self.coverage.values())
        return (ran, total)

    def badge(self) -> str:
        ran, total = self.coverage_ratio
        return f"{self.status} ({ran}/{total})"


# ---------------------------------------------------------------------------
# L0 — 형식 · 구조 (6)
# ---------------------------------------------------------------------------


def run_l0(scenario: PurchaseScenario) -> list[CriticFinding]:
    """
    시나리오가 계약 형태를 갖췄는가. **여기서 걸리면 뒤 계층은 의미가 없다.**

    L0-2 가 설계서 §2 의 개명 대상이다 — `variant_axis` → `strategy_type`,
    FAIL 코드도 `E-AXIS-GATE` → `E-STRATEGY-GATE`.
    """
    out: list[CriticFinding] = []

    def bad(code: str, detail: str) -> None:
        out.append(CriticFinding("L1_hard", code, detail))

    # L0-1 식별자
    if not getattr(scenario, "scenario_id", ""):
        bad("E-NO-ID", "scenario_id 가 비어 있다")

    # L0-2 strategy_type 허용 목록 (구 E-AXIS-GATE)
    st = getattr(scenario, "strategy_type", None)
    if st not in _STRATEGY_TYPES:
        bad(
            "E-STRATEGY-GATE",
            f"strategy_type='{st}' 가 허용 목록 밖이다. 허용: {sorted(_STRATEGY_TYPES)} (§3.5)",
        )

    # L0-3 stance 는 축이 아니라 성향 라벨이다 (v1.2 개념 분리)
    stance = getattr(scenario, "stance", None)
    if stance not in _STANCES:
        bad("E-STANCE", f"stance='{stance}' 가 허용 목록 밖이다. 허용: {sorted(_STANCES)}")

    # L0-4 품목 코드
    unknown = set(getattr(scenario, "qty_kg", {})) - set(ITEMS)
    if unknown:
        bad("E-UNKNOWN-ITEM", f"미등록 품목: {sorted(unknown)}")

    # L0-5 음수 수량
    neg = [i for i, v in getattr(scenario, "qty_kg", {}).items() if v < -EPS]
    if neg:
        bad("E-NEGATIVE-QTY", f"음수 수량: {sorted(neg)}")

    # L0-6 price_basis 존재
    if not getattr(scenario, "price_basis", ""):
        bad("E-NO-PRICE-BASIS", "price_basis 가 비어 있다 — 시장 기준 대조가 불가능하다")

    return out


# ---------------------------------------------------------------------------
# L1 신설 ① — has_unmet_obligation 권한 침범 (설계서 §3.1)
# ---------------------------------------------------------------------------


def check_obligation_authority(
    dept: Dept,
    produced_fields: Sequence[str],
) -> list[CriticFinding]:
    """
    E5 판정 권한 침범 탐지 — 축 침범(§3.4.2)과 같은 성격이다.

    ★ 왜 하드하게 막는가.
      부서가 이 플래그를 세팅하면 매입의 `no_proposal` 이 곧 E5 로 오독된다.
      재고에 여유가 있으면 매입 0 이어도 납품은 가능하다 — 부서는 자기 도메인
      사실만 알 뿐 **전체 충족 여부를 판정할 위치가 아니다.**
      E5 는 계약 위반 선언이므로 오판 비용이 크다.
    """
    leaked = S3_EXCLUSIVE_FIELDS & set(produced_fields)
    if not leaked:
        return []
    return [
        CriticFinding(
            "L2_evidence",
            "E-AUTHORITY",
            f"{dept} 가 {sorted(leaked)} 를 산출했다. S3 전속 판정이다(§5.0). "
            f"부서는 no_proposal_reason·today_floor 등 자기 도메인 사실만 반환한다.",
            (),
            dept,
        )
    ]


# ---------------------------------------------------------------------------
# L1 신설 ② — 재무 cap 의 등급 · 수량 개입 (설계서 §3.2)
# ---------------------------------------------------------------------------


def check_finance_cap_grade_neutrality(
    check: CheckResult,
    inputs_used: Sequence[str],
) -> list[CriticFinding]:
    """
    max_feasible_amount_krw = 등급 무관 · 순수 금액 상한.
    **재무는 어떤 단가도 가정하지 않는다** (§3.4.5-④ v1.2).

    ★ 이 검사가 없으면 조용히 틀린다.
      재무가 평균가를 가정해 cap 을 냈는데 매입이 특급 위주로 배분하면
      두 값의 기준이 달라 대조 자체가 무의미해진다.
      그런데 **숫자는 멀쩡히 나오므로 아무 에러도 발생하지 않는다.**
    """
    if check.dept != "finance" or check.cap_amount_krw is None:
        return []
    leaked = FORBIDDEN_IN_FINANCE_CAP & set(inputs_used)
    if not leaked:
        return []
    return [
        CriticFinding(
            "L2_evidence",
            "E-GRADE-LEAK",
            f"재무 금액 cap '{check.check_id}' 산출에 {sorted(leaked)} 가 개입했다. "
            f"등급·수량 가정 없이 순수 금액으로만 산출한다(§3.4.5-④). "
            f"대조 대상은 오직 total_amount_krw 다.",
            (),
            "finance",
        )
    ]


# ---------------------------------------------------------------------------
# L1 — 바인딩 (스냅샷 · 정책 버전)
# ---------------------------------------------------------------------------


def check_snapshot_binding(
    replies: Mapping[Dept, T2Reply],
    snapshot: T0Snapshot,
    reply_snapshot_ids: Mapping[Dept, str] | None = None,
) -> list[CriticFinding]:
    """
    부서 회신이 **이 스냅샷**을 보고 답했는가.

    ★ v1.2.3 — `snapshot_id` 대조를 추가한다 (영업 IO 명세 §1 · §3).

      as_of 만 보면 같은 날 재실행(run_seq 2)에서 만든 다른 스냅샷의 회신이
      그대로 통과한다. 명세가 "입력과 같은 스냅샷인지 크리틱이 대조한다"고
      명시적으로 Critic 에 이 검사를 배정했다.

      `reply_snapshot_ids` 는 T2Reply 에 필드가 없어 사이드카로 받는다
      (계약 개정 목록 참조).
    """
    out: list[CriticFinding] = []
    ids = reply_snapshot_ids or {}
    for dept, reply in replies.items():
        if reply.as_of != snapshot.as_of:
            out.append(
                CriticFinding(
                    "L2_evidence",
                    "E-SNAPSHOT-BIND",
                    f"{dept} 회신 as_of={reply.as_of} ≠ 스냅샷 as_of={snapshot.as_of} — "
                    f"다른 날 데이터로 답했다",
                    (),
                    dept,
                )
            )
            continue

        sid = ids.get(dept)
        if sid is None:
            continue  # 미제출 — 커버리지에서 skipped 로 드러난다
        if snapshot.snapshot_id and sid != snapshot.snapshot_id:
            out.append(
                CriticFinding(
                    "L2_evidence",
                    "E-SNAPSHOT-BIND",
                    f"{dept} 회신 snapshot_id={sid} ≠ {snapshot.snapshot_id} — "
                    f"같은 날이지만 다른 스냅샷(재실행)의 회신이다",
                    (),
                    dept,
                )
            )
    return out


_BAND_FIELDS = ("floor_kg", "cap_kg", "cap_total_kg", "cap_amount_krw", "cap_by_date_kg")


def _fills_band(check: CheckResult) -> bool:
    """이 검사가 밴드 값을 채우는가. 채우면 시나리오와 무관해야 한다."""
    return any(getattr(check, f, None) is not None for f in _BAND_FIELDS)


def check_scenario_independence(
    dept: Dept,
    check: CheckResult,
    inputs_used: Sequence[str],
) -> list[CriticFinding]:
    """
    **밴드를 채우는 검사**가 매입 시나리오를 읽었는가 (v1.2.5 · v1.2.8 범위 축소).

    정의서 §3.6.1 — "각 부서는 시나리오와 무관하게 하루에 한 번만 회신한다."

    ★ v1.2.8 — 갱신된 「각_에이전트_필요_산출물」 PDF 가 재무 A · 물류 A INPUT 에
      `purchase_output` 을 **그대로 두고**, 영업 A 만 "재무·물류와 달리 없다"고
      명시했다. v1.2.5 의 전 부서 차단은 이 계약과 충돌한다.

      조정: §3.6.1 이 막는 것은 **밴드가 후보에 의존하는 것**이지 매입안을
      받는 것 자체가 아니다. 재무/물류는 자문 주석(soft_warnings·evidences —
      물류의 FRESHNESS_RISK 는 중품 입고를 참조한다)에 매입안을 쓸 수 있다.
      **금지는 밴드 값을 채우는 검사에만** 건다.

        밴드 검사(floor_kg·cap_*·cap_by_date_kg) + 시나리오 입력  → FAIL
        자문 검사(soft warning·evidence)      + 시나리오 입력  → 허용

    ★ 재무 cap 은 이 위에 `E-GRADE-LEAK` 이 한 겹 더 있다 — 시나리오를 안 읽어도
      등급·수량을 가정하면 안 된다 (§3.6.8).
    """
    if not _fills_band(check):
        return []  # 자문 검사는 매입안 참조 허용 (v1.2.8)
    leaked = FORBIDDEN_SCENARIO_INPUTS & set(inputs_used)
    if not leaked:
        return []
    return [
        CriticFinding(
            "L2_evidence",
            "E-SCENARIO-LEAK",
            f"{dept} 의 밴드 검사 '{check.check_id}' 가 매입 시나리오 {sorted(leaked)} 를 읽었다. "
            f"밴드는 시나리오와 무관해야 한다(§3.6.1). "
            f"밴드가 후보에 의존하면 T3 가 어느 밴드로 클리핑할지 정할 수 없다. "
            f"자문(soft_warnings)에서 매입안을 참조하는 것은 허용된다.",
            (),
            dept,
        )
    ]


def check_sales_authority(facts: Any | None) -> list[CriticFinding]:
    """
    영업 사실 보고에 판정 필드가 섞였는가 (v1.2.3).

    ★ 영업 IO 명세 §5 — "영업은 판정하지 않는다. 빈 후보 목록과 사유, 확정 납품
      의무량, 충당 가능 물량이라는 사실만 제출한다."
      `has_unmet_obligation` 은 S3 전속이므로 `E-AUTHORITY` 와 같은 계열이다.
    """
    if facts is None:
        return []
    leaked = S3_EXCLUSIVE_FIELDS & {k for k in dir(facts) if not k.startswith("_")}
    if not leaked:
        return []
    return [
        CriticFinding(
            "L2_evidence",
            "E-AUTHORITY",
            f"영업 사실 보고에 {sorted(leaked)} 가 들어 있다. S3 전속 판정이다(§5.0).",
            (),
            "sales",
        )
    ]


# ---------------------------------------------------------------------------
# L2 — as_of 룩어헤드
# ---------------------------------------------------------------------------


def check_lookahead(
    replies: Mapping[Dept, T2Reply],
    snapshot: T0Snapshot,
) -> list[CriticFinding]:
    """
    미래정보 차단(§1.2-6). `AsOfSession` 이 조회 시점에 막지만,
    Critic 은 **결과물에 남은 흔적**으로 한 번 더 본다.
    """
    out: list[CriticFinding] = []
    for dept, reply in replies.items():
        if reply.as_of > snapshot.as_of:
            out.append(
                CriticFinding(
                    "L2_evidence",
                    "E-LOOKAHEAD",
                    f"{dept} 회신 as_of={reply.as_of} > 기준일 {snapshot.as_of} — 미래정보",
                    (),
                    dept,
                )
            )
    return out


# ---------------------------------------------------------------------------
# L3 — 축 침범 (기존 run_l3 의 하드코딩 화이트리스트를 계약에서 읽도록 교정)
# ---------------------------------------------------------------------------


def check_axis_intrusion(replies: Mapping[Dept, T2Reply]) -> list[CriticFinding]:
    """
    ★ 기존 `critic.run_l3` 은 허용 축을 함수 안에 하드코딩한다.

          {"sales": {"price", "quantity"}, ...}

      그런데 계약(`_DEPT_AXES`)은 v0.2 에서 영업 축을 price → channel_mix 로
      개명했다. 두 곳이 어긋나 있어서 **계약상 정당한 channel_mix 제안이
      축 침범으로 FAIL 난다.** L3 FAIL 은 T3 로 회송되므로 사후 루프 예산을
      태우고 결국 E2 보류로 끝난다.

      여기서는 계약을 단일 출처로 삼는다. 룰을 두 벌 짜지 않는다(§6.4).
    """
    out: list[CriticFinding] = []
    for dept, reply in replies.items():
        allowed = set(_DEPT_AXES[dept])
        for adj in reply.suggested_adjustments:
            if adj.axis not in allowed:
                out.append(
                    CriticFinding(
                        "L3_band_axis",
                        f"{dept}.suggested_adjustment",
                        f"{dept} 가 {adj.axis} 축을 침범했다. 허용: {sorted(allowed)} (§3.4.2)",
                        (),
                        dept,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# L4 신설 — 회차별 도착일 분해 (설계서 §3.3)
# ---------------------------------------------------------------------------


def check_arrival_decomposition(
    clip: ClipResult,
    inbound_lead_days: int | None,
) -> tuple[list[CriticFinding], list[str]]:
    """
        날짜 d 점유 = 확정 점유[d] + Σ(매입안 중 d까지 도착분) ≤ cap_by_date[d]
                                      ↑ split_plan 회차별로 각각

    ★ 총량 단일 도착일로 뭉치면 분할 매입의 창고 부담 분산 효과가 사라진다.
      전량이 하루에 도착한 것으로 계산되어 cap_by_date 검사가 무의미해진다.

    반환: (findings, skipped)  — N4 미결이면 FAIL 이 아니라 skipped 다.
      0 으로 대체하면 '오늘 승인분이 오늘 도착'이 되어 §3.2.3 on_hand 전환 금지가
      무의미해진다(§1.2-10).
    """
    legs = clip.clipped_split_plan
    if not legs:
        return [], []  # 분할 없으면 대상 아님

    missing = [i for i, leg in enumerate(legs) if leg.expected_arrival_date is None]
    if missing and inbound_lead_days is None:
        return [], [
            f"check_arrival_decomposition: N4 미확정 + 회차 {missing} 도착일 부재 — 계산 불가"
        ]
        # 리드타임으로 파생 가능하므로 검사는 계속한다 (band.check_occupancy_by_date 와 동일 규약)

    # 회차 수와 도착 스케줄 수가 맞는가
    dated = [leg for leg in legs if leg.expected_arrival_date is not None]
    if dated and len(dated) != len(legs) and inbound_lead_days is None:
        return [
            CriticFinding(
                "L3_band_axis",
                "E-ARRIVAL-COLLAPSE",
                f"split_plan 회차 {len(legs)}건 중 도착일이 있는 것은 {len(dated)}건뿐이다. "
                f"총량 단일 도착일로 뭉치면 분산 효과가 사라진다(§3.4.5-③).",
            )
        ], []

    # 수량 항등식 — 회차별 합 == 클리핑 총량
    leg_total = sum(sum(leg.qty_kg.values()) for leg in legs)
    if abs(leg_total - clip.total_kg) > 0.5:
        return [
            CriticFinding(
                "L3_band_axis",
                "E-ARRIVAL-SUM",
                f"회차별 도착 수량 합 {leg_total:,.1f}kg ≠ 총량 {clip.total_kg:,.1f}kg",
            )
        ], []

    return [], []


# ---------------------------------------------------------------------------
# L4 — variant_collapsed 유형 분류 (설계서 §5)
# ---------------------------------------------------------------------------


def classify_collapse(
    clips: Sequence[ClipResult],
    scenarios: Mapping[str, PurchaseScenario],
) -> tuple[bool, str | None]:
    """
    AXIS     — 전 안이 같은 strategy_type. 애초에 차별화가 없었다 → **T1 문제**
    QUANTITY — 클리핑 후 수량이 수렴했다. 밴드가 좁다 → **회사 상태 문제**

    ★ 판정은 `band.detect_collapse_type()` 에 위임한다. 룰을 두 벌 짜지 않는다(§6.4).
      Critic 이 자체 구현하면 T3 와 미세하게 달라져 "T3 는 붕괴 아님 / Critic 은 붕괴"가
      반복되고, 그 차이를 메우는 데 사후 루프 예산이 소진된다.
      여기서 하는 일은 **축 목록을 시나리오에서 뽑아 넘기는 것**뿐이다.
    """
    live = [c for c in clips if not c.infeasible]
    axes = [getattr(scenarios.get(c.scenario_id), "strategy_type", "") for c in live]
    ctype = detect_collapse_type(list(clips), axes)
    return (ctype is not None), ctype


# ---------------------------------------------------------------------------
# 러너
# ---------------------------------------------------------------------------

_LAYER_TOTALS = {"L0": 6, "L1": 13, "L2": 4, "L3": 17, "L4": 10, "L5": 6}


def run_critic_v04(
    *,
    as_of: date,
    run_seq: int,
    clip: ClipResult,
    band: Band,
    snapshot: T0Snapshot,
    scenario: PurchaseScenario,
    replies: Mapping[Dept, T2Reply],
    unit_price: Mapping[ItemCode, float],
    verify_ctx: Mapping[str, Any],
    check_fns: Mapping[str, Any],
    resolve_evidence: EvidenceResolver,
    dept_meta: Mapping[Dept, DeptMeta] | None = None,
    all_clips: Sequence[ClipResult] = (),
    all_scenarios: Mapping[str, PurchaseScenario] | None = None,
    rationale: str = "",
    judge: RationaleJudge | None = None,
    governed_axis_refs: Mapping[str, set[str]] | None = None,
    allow_assumed_hard: bool = True,
    cycle: Literal["A", "B"] = "A",
    unattended: bool = False,
) -> CriticVerdictV04:
    """
    앞 계층이 FAIL 이면 뒤는 돌리지 않는다 — L1 이 깨졌는데 L5 LLM 을 호출하는 것은
    비용 낭비다. 다만 **커버리지는 돌지 않은 계층도 기록한다** (설계서 §8).

    unattended : 백테스트 무인 구간. L5 CONCERN 을 **승인하되 별도 카운트**한다(설계서 §10).
      자동 승인으로 처리하면 L5 가 무력화되고, 자동 반려하면 LLM 이 결정을 죽이게 되어
      §4.3 과 충돌한다. 판정을 유보하고 관측으로 돌린다.
    """
    meta = dept_meta or {}
    findings: list[CriticFinding] = []
    concerns: list[Issue] = []
    skipped: list[str] = []
    coverage: dict[str, tuple[int, int]] = {}

    # ── L0 형식 · 구조 ────────────────────────────────────────────
    findings += run_l0(scenario)
    coverage["L0"] = (6, _LAYER_TOTALS["L0"])

    # ── L1 바인딩 · 출처 · 권한 ───────────────────────────────────
    l1_ran = 0
    if not findings:
        findings += check_snapshot_binding(replies, snapshot)
        l1_ran += 1
        for dept, reply in replies.items():
            dm = meta.get(dept)
            if dm is not None:
                findings += check_obligation_authority(dept, dm.produced_fields)
                for chk in reply.checks:
                    used = dm.inputs_used.get(chk.check_id, ())
                    # 전 부서 공통 — 시나리오 독립성 (§3.6.1)
                    findings += check_scenario_independence(dept, chk, used)
                    # 재무 전용 — 등급·수량 무관 (§3.6.8)
                    findings += check_finance_cap_grade_neutrality(chk, used)
            else:
                skipped.append(f"{dept}: DeptMeta 미제출 — E-AUTHORITY·E-GRADE-LEAK 생략")
        l1_ran += 2 if meta else 0
        findings += _run_evidence_match(replies, resolve_evidence)
        l1_ran += 1
        findings += check_evidence_grade(replies, allow_assumed_hard)
        l1_ran += 1
        findings += check_constraint_independence(replies, governed_axis_refs or {})
        l1_ran += 1
        findings += check_price_basis_consistency(snapshot, scenario.price_basis)
        l1_ran += 1
    coverage["L1"] = (l1_ran, _LAYER_TOTALS["L1"])

    # ── L2 as_of 룩어헤드 ─────────────────────────────────────────
    l2_ran = 0
    if not findings:
        findings += check_lookahead(replies, snapshot)
        l2_ran += 1
    coverage["L2"] = (l2_ran, _LAYER_TOTALS["L2"])

    # ── L3 하드 제약 재검사 (부서 함수를 DB 재조회 값으로 재실행) ──
    l3_ran = 0
    if not findings:
        findings += check_identity_on_clipped(clip)
        l3_ran += 1
        findings += _run_hard_recheck(clip.clipped_qty_kg, verify_ctx, check_fns)
        l3_ran += len(check_fns)
        findings += check_axis_intrusion(replies)
        l3_ran += 1
    coverage["L3"] = (l3_ran, _LAYER_TOTALS["L3"])

    # ── L4 결합 재검산 ────────────────────────────────────────────
    l4_ran = 0
    if not findings:
        # 밴드 준수
        for i, v in clip.clipped_qty_kg.items():
            f, c = band.floor_kg.get(i, 0.0), band.cap_kg.get(i, float("inf"))
            if v < f - EPS:
                findings.append(
                    CriticFinding(
                        "L3_band_axis",
                        f"band.floor.{i}",
                        f"{i} 결정 {v:,.0f}kg < floor {f:,.0f}kg — 납기 미달",
                    )
                )
            if v > c + EPS:
                findings.append(
                    CriticFinding(
                        "L3_band_axis", f"band.cap.{i}", f"{i} 결정 {v:,.0f}kg > cap {c:,.0f}kg"
                    )
                )
        l4_ran += 1

        total = sum(clip.clipped_qty_kg.values())
        if total > band.cap_total_kg + EPS:
            findings.append(
                CriticFinding(
                    "L3_band_axis",
                    "band.cap_total_kg",
                    f"총 {total:,.0f}kg > 창고 상한 {band.cap_total_kg:,.0f}kg",
                )
            )
        l4_ran += 1

        # 금액 항등식 — 재무 cap 과 대조하는 값은 오직 total_amount_krw (§3.4.5-④)
        amount = clip.clipped_amount_krw or sum(
            clip.clipped_qty_kg[i] * unit_price.get(i, 0.0) for i in clip.clipped_qty_kg
        )
        if amount > band.cap_amount_krw + EPS:
            findings.append(
                CriticFinding(
                    "L3_band_axis",
                    "band.cap_amount_krw",
                    f"총 {amount:,.0f}원 > 가용 {band.cap_amount_krw:,.0f}원",
                )
            )
        l4_ran += 1

        # 회차별 도착일 분해 🆕
        arr_f, arr_s = check_arrival_decomposition(clip, snapshot.inbound_lead_days)
        findings += arr_f
        skipped += arr_s
        l4_ran += 1 if not arr_s else 0

        # 날짜별 창고 점유 — T1 · T3 와 **같은 함수**를 쓴다 (설계서 §4)
        occ = check_occupancy_by_date(clip, band, snapshot)
        findings += [CriticFinding("L3_band_axis", "check_occupancy_by_date", p) for p in occ]
        l4_ran += 1 if band.cap_by_date_kg else 0
        if not band.cap_by_date_kg:
            skipped.append("check_occupancy_by_date: cap_by_date 미제공 (N15) — 생략")
    coverage["L4"] = (l4_ran, _LAYER_TOTALS["L4"])

    # ── L5 논리 일관성 (LLM) — FAIL 이 아니라 CONCERN ─────────────
    note = ""
    l5_ran = 0
    if not findings:
        l5, note = _run_llm_rationale(clip, band, replies, rationale, judge)
        if judge is None:
            skipped.append("L5: judge 미주입 — 논리 일관성 검증 생략")
        elif not _judge_ran(judge):
            # judge 는 붙었지만 LLM 이 실제로 판정하지 못했다. 검사한 척하지 않는다 —
            # coverage 를 0 으로 두고 skipped 에 드러낸다 (설계서 §8).
            #
            # 🔴 **왜 못 했는지를 가른다.** 전에는 미가동·장애·호출 불필요를 한 문구로
            #    냈는데, 셋은 **해야 할 일이 다르다** — 앞 둘은 고칠 것이 있고 뒤는
            #    없다. 한 줄로 내면 사람이 **없는 문제를 찾는다** (매입 8/31 지적과
            #    같은 종류다).
            skipped.append(f"L5: {_l5_skip_reason(judge)} — 논리 일관성 검증 생략")
        else:
            l5_ran = 6
            for f in l5:
                concerns.append(Issue("E-LOGIC", f.detail, "L5_logic", f.dept))
    coverage["L5"] = (l5_ran, _LAYER_TOTALS["L5"])

    # ── 붕괴 유형 (참고 산출 — FAIL 사유가 아니다) ────────────────
    if all_clips and all_scenarios:
        collapsed, ctype = classify_collapse(all_clips, all_scenarios)
        if collapsed:
            concerns.append(Issue("E-COLLAPSE", f"시나리오 붕괴 — 유형 {ctype}", "L4_combine"))

    if findings:
        status: CriticStatus = "FAIL"
    elif concerns:
        status = "CONCERN"
    else:
        status = "PASS"

    if unattended and status == "CONCERN":
        # 승인하되 별도 카운트한다. 판정을 유보하고 관측으로 돌린다 (설계서 §10).
        note = (note + " | " if note else "") + "UNATTENDED_CONCERN_APPROVED"

    return CriticVerdictV04(
        as_of=as_of,
        run_seq=run_seq,
        scenario_id=clip.scenario_id,
        status=status,
        findings=tuple(findings),
        concerns=tuple(concerns),
        coverage=coverage,
        skipped=tuple(skipped),
        llm_note=note,
        end_stage=("CRITIC_A" if cycle == "A" else "CRITIC_B") if status == "FAIL" else None,
    )


# ===========================================================================
# 사이클 B 러너 — L4-7 ~ L4-10 (설계서 §6 검사 7~10)
# ===========================================================================
#
#   7  overlay 후 cap_by_date 재검산   E-OVERLAY-CAPDATE
#   8  공용 출고 능력                  E-OUTBOUND-CAP
#   9  on_hand 초과                    E-ONHAND-EXCEED
#   10 신선도·납기                     E-FRESHNESS
#
# Critic A 의 L4 가 매입 밴드(kg·원)를 재검산하듯, B 의 L4 는 출고 밴드(전부 kg)와
# overlay·로트를 재검산한다. 미결값(N15 cap_by_date·N17 공용 출고·로트 규격)으로
# 돌 수 없는 검사는 통과가 아니라 skipped 로 남긴다 (설계서 §8).

_LAYER_TOTALS_B = {"L4_B": 4}


def check_outbound_capacity(
    clip: ClipResult, band: OutboundBand
) -> tuple[list[CriticFinding], list[str]]:
    """L4-8 — 총 출고가 공용 출고 능력을 넘지 않는가 (§3.7.4)."""
    cap = band.cap_total_kg
    if cap == float("inf"):
        return [], ["L4-8 공용 출고 능력: N17 미결 — 미검사"]
    total = sum(clip.clipped_qty_kg.values())
    if total > cap + EPS:
        return [
            CriticFinding(
                "L3_band_axis",
                "outbound.cap_total_kg",
                f"총 출고 {total:,.0f}kg > 공용 출고 능력 {cap:,.0f}kg",
            )
        ], []
    return [], []


def check_onhand_exceed(
    clip: ClipResult, lots: Sequence[LotConstraint]
) -> tuple[list[CriticFinding], list[str]]:
    """L4-9 — 품목별 출고가 on_hand(가용 로트 합)를 넘지 않는가 (§3.4.5-⑦)."""
    if not lots:
        return [], ["L4-9 on_hand 초과: 로트 제약 미제출 — 미검사"]
    avail: dict[ItemCode, float] = {}
    for lot in lots:
        if lot.status == "AVAILABLE":
            avail[lot.item] = avail.get(lot.item, 0.0) + lot.available_qty_kg
    out: list[CriticFinding] = []
    for item, qty in clip.clipped_qty_kg.items():
        cap = avail.get(item, 0.0)
        if qty > cap + EPS:
            out.append(
                CriticFinding(
                    "L3_band_axis",
                    f"onhand.{item}",
                    f"{item} 출고 {qty:,.0f}kg > 가용 로트 {cap:,.0f}kg — on_hand 초과",
                )
            )
    return out, []


def check_freshness_delivery(
    allocation: SaleAllocation | None,
    lots: Sequence[LotConstraint],
    as_of: date,
) -> tuple[list[CriticFinding], list[str]]:
    """L4-10 — 배정 로트의 잔여 신선도가 납기까지 버티는가.

    각 채널 배분(due_date 있는 것)이 쓰는 로트의 remaining_freshness_days 가
    (due_date - as_of) 보다 짧으면 납기 전에 상한다.
    """
    if allocation is None or not lots:
        return [], ["L4-10 신선도·납기: 배분/로트 미제출 — 미검사"]
    fresh = {lot.lot_id: lot.remaining_freshness_days for lot in lots}
    checked = False
    out: list[CriticFinding] = []
    for leg in allocation.legs:
        if leg.due_date is None or not leg.lot_ids:
            continue
        need_days = (leg.due_date - as_of).days
        for lot_id in leg.lot_ids:
            if lot_id not in fresh:
                continue
            checked = True
            if fresh[lot_id] < need_days:
                out.append(
                    CriticFinding(
                        "L3_band_axis",
                        f"freshness.{lot_id}",
                        f"로트 {lot_id} 잔여 {fresh[lot_id]}일 < 납기 {need_days}일 "
                        f"({leg.item} → {leg.channel} {leg.due_date}) — 납기 전 신선도 소진",
                    )
                )
    if not checked:
        return [], ["L4-10 신선도·납기: 납기·로트 매칭 없음 — 미검사"]
    return out, []


def check_overlay_cap_by_date(
    commitment: ApprovedPurchaseCommitment | None,
    snapshot: T0Snapshot,
) -> tuple[list[CriticFinding], list[str]]:
    """L4-7 — H1 승인분 overlay 후 날짜별 창고 점유가 상한을 넘지 않는가.

    승인 매입의 회차별 도착(arrival_schedule)을 확정 점유에 겹쳐, 어느 날짜든
    창고 여유(warehouse_free_kg)를 초과하면 오늘 판매를 그만큼 늦추거나 늘려야 한다.
    N15(confirmed_occupancy_by_date)·N2(창고 상한)가 미결이면 검사할 수 없다.
    """
    occ = snapshot.confirmed_occupancy_by_date
    if commitment is None or not commitment.arrival_schedule:
        return [], ["L4-7 overlay cap_by_date: 승인 약정 없음 — 미검사"]
    if not occ or snapshot.warehouse_free_kg <= 0:
        return [], ["L4-7 overlay cap_by_date: N15/N2 미결 — 미검사"]
    capacity = max(occ.values()) + snapshot.warehouse_free_kg
    overlaid: dict[date, float] = dict(occ)
    for leg in commitment.arrival_schedule:
        overlaid[leg.date] = overlaid.get(leg.date, 0.0) + leg.qty_kg
    out: list[CriticFinding] = []
    for d, load in sorted(overlaid.items()):
        if load > capacity + EPS:
            out.append(
                CriticFinding(
                    "L3_band_axis",
                    f"overlay.cap_by_date.{d}",
                    f"{d} 점유 {load:,.0f}kg > 상한 {capacity:,.0f}kg — 승인분 overlay 후 초과",
                )
            )
    return out, []


#: `judge` 가 판정하지 못한 이유. `JudgeRunner` 가 남긴 `llm_status` 로 가른다.
#:
#: 🔴 `SKIPPED_TEMPLATE` 이 이 Flow 의 기본 상태다 — **판정할 문장이 없다.**
#:   L5 는 *"클리핑 후에 쓰인 결정 근거"* 를 본다. 1차 Flow 에는 그 문장을 쓰는
#:   단계가 없어서(오케 selector 가 하던 일), `rationale` 이 빈 채로 온다.
#:   **이건 장애가 아니라 아직 없는 단계다.** "미수행" 이라고만 적으면 읽는 사람이
#:   서버를 뒤진다.
_L5_SKIP_REASON: dict[str, str] = {
    "SKIPPED_TEMPLATE": "판정할 결정 근거가 없다 (이 Flow 에는 그 문장을 쓰는 단계가 없다)",
    "FALLBACK": "LLM 을 불렀으나 쓸 수 있는 판정을 못 받았다",
    "DISABLED": "LLM 설정이 꺼져 있다",
}


def _l5_skip_reason(judge: RationaleJudge) -> str:
    result = getattr(judge, "result", None)
    status = getattr(result, "llm_status", None)
    return _L5_SKIP_REASON.get(str(status), "LLM 판정 미수행")


def _judge_ran(judge: RationaleJudge) -> bool:
    """judge 가 실제로 판정했는지.

    LLM 어댑터(`JudgeRunner`)는 `ran` 으로 알려 준다 — 호출 불필요·장애면 False 다.
    `ran` 이 없는 평범한 콜러블(테스트 스텁 등)은 돌았다고 본다.
    """
    return bool(getattr(judge, "ran", True))


@dataclass(frozen=True)
class _BandView:
    """OutboundBand 를 L5 payload 가 기대하는 Band 모양으로만 비춰 준다.

    B 에는 매입 밴드의 floor·금액축이 없다 — 없는 축은 비운다. 값을 지어내지 않는다.
    """

    floor_kg: Mapping[str, float]
    cap_kg: Mapping[str, float]
    cap_total_kg: float | None
    cap_amount_krw: float | None


def _band_view(outbound_band: OutboundBand) -> _BandView:
    return _BandView(
        floor_kg={},
        cap_kg=dict(outbound_band.cap_kg),
        cap_total_kg=outbound_band.cap_total_kg,
        cap_amount_krw=None,
    )


def run_critic_b(
    *,
    as_of: date,
    run_seq: int,
    clip: ClipResult,
    outbound_band: OutboundBand,
    snapshot: T0Snapshot,
    replies: Mapping[Dept, T2Reply],
    allocation: SaleAllocation | None = None,
    commitment: ApprovedPurchaseCommitment | None = None,
    lot_constraints: Sequence[LotConstraint] = (),
    dept_meta: Mapping[Dept, DeptMeta] | None = None,
    sales_facts: Any | None = None,
    judge: RationaleJudge | None = None,
    rationale: str = "",
    unattended: bool = False,
) -> CriticVerdictV04:
    """사이클 B(판매) 검증. 앞 계층이 FAIL 이면 뒤는 돌리지 않는다.

    A 와 달리 L0(매입 시나리오 형식)·L3(매입 축)·금액 항등식은 없다.
    B 는 스냅샷 바인딩·룩어헤드·권한(L1~L2) + 출고 결합 재검산(L4-7~10)이다.
    """
    meta = dept_meta or {}
    findings: list[CriticFinding] = []
    skipped: list[str] = []
    coverage: dict[str, tuple[int, int]] = {}

    # ── L1 바인딩 · 권한 ──────────────────────────────────────────
    l1_ran = 0
    findings += check_snapshot_binding(replies, snapshot)
    l1_ran += 1
    for dept, dm in meta.items():
        findings += check_obligation_authority(dept, dm.produced_fields)
        l1_ran += 1
    findings += check_sales_authority(sales_facts)
    l1_ran += 1
    coverage["L1"] = (l1_ran, _LAYER_TOTALS["L1"])

    # ── L2 룩어헤드 ───────────────────────────────────────────────
    coverage["L2"] = (0, _LAYER_TOTALS["L2"])
    if not findings:
        findings += check_lookahead(replies, snapshot)
        coverage["L2"] = (1, _LAYER_TOTALS["L2"])

    # ── L4-B 결합 재검산 (7~10) ───────────────────────────────────
    l4_ran = 0
    if not findings:
        for check, args in (
            (check_overlay_cap_by_date, (commitment, snapshot)),
            (check_outbound_capacity, (clip, outbound_band)),
            (check_onhand_exceed, (clip, lot_constraints)),
            (check_freshness_delivery, (allocation, lot_constraints, as_of)),
        ):
            f, s = check(*args)
            findings += f
            skipped += s
            l4_ran += 1 if not s else 0
    coverage["L4_B"] = (l4_ran, _LAYER_TOTALS_B["L4_B"])

    # ── L5 논리 일관성 (LLM) — A 와 같이 FAIL 이 아니라 CONCERN ────
    concerns: list[Issue] = []
    note = ""
    l5_ran = 0
    if not findings:
        l5, note = _run_llm_rationale(clip, _band_view(outbound_band), replies, rationale, judge)
        if judge is None:
            skipped.append("L5: judge 미주입 — 논리 일관성 검증 생략")
        elif not _judge_ran(judge):
            skipped.append("L5: LLM 판정 미수행 — 논리 일관성 검증 생략")
        else:
            l5_ran = 6
            for f in l5:
                concerns.append(Issue("E-LOGIC", f.detail, "L5_logic", f.dept))
    coverage["L5"] = (l5_ran, _LAYER_TOTALS["L5"])

    if findings:
        status: CriticStatus = "FAIL"
    elif concerns:
        status = "CONCERN"
    else:
        status = "PASS"

    if unattended and status == "CONCERN":
        # A 와 동일 — 승인하되 별도 카운트한다 (설계서 §10).
        note = (note + " | " if note else "") + "UNATTENDED_CONCERN_APPROVED"

    return CriticVerdictV04(
        as_of=as_of,
        run_seq=run_seq,
        scenario_id=getattr(allocation, "allocation_id", "(sales)"),
        status=status,
        findings=tuple(findings),
        concerns=tuple(concerns),
        coverage=coverage,
        skipped=tuple(skipped),
        llm_note=note,
        end_stage="CRITIC_B" if status == "FAIL" else None,
    )


# ---------------------------------------------------------------------------
# 계약 개정 요청 목록 — contracts_core.py 가 FROZEN 이라 여기 적어 둔다
# ---------------------------------------------------------------------------

CONTRACT_AMENDMENTS: tuple[tuple[str, str], ...] = (
    (
        "CheckResult.inputs_used",
        "L1-7 재무 cap 등급 개입 탐지에 필요. 현재 DeptMeta 사이드카로 우회 중.",
    ),
    (
        "T2Reply.produced_fields",
        "L1-6 has_unmet_obligation 권한 침범 탐지에 필요. 현재 DeptMeta 사이드카.",
    ),
    (
        "CriticLayer 에 L0_format · L4_combine · L5_logic 추가",
        "설계서 v0.4 는 6레이어인데 계약은 L1~L4 5값이다. FAIL_ROUTING 도 함께 확장 필요.",
    ),
    (
        "EndStage 에 CRITIC_A · CRITIC_B 추가",
        "설계서 §1 불일치 ②. 지금은 Critic FAIL 로 끝난 날이 T3 로 뭉쳐 기록된다.",
    ),
    (
        "CriticVerdict.status: PASS|CONCERN|FAIL",
        "설계서 §1 불일치 ③. UI v1.3 에도 critic_verdict·critic_issues 영역이 필요.",
    ),
    (
        "ApprovedPurchaseCommitment.arrival_schedule",
        (
            "설계서 §1 불일치 ①. SplitLeg 는 회차별 도착일을 갖는데 Commitment 는 단수라 "
            "사이클 B overlay 에서 날짜 분포가 소실된다."
        ),
    ),
)
