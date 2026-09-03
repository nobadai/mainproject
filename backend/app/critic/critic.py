# ─────────────────────────────────────────────────────────────────────────────
# STATUS: TOOL 후보 — 유지 (2026-08-26)
#   L1~L4 base. `critic_v0_4` 가 이것을 감싸 6레이어로 재배치한다. 함께 Tool 로 전환.
# ─────────────────────────────────────────────────────────────────────────────
"""
critic.py — Critic 5계층 러너 (담당: 이현서)

계약서 §6.2 "Critic 의 90%는 LLM 이 아니라 코드다" 를 구현한다.

  L1   하드 제약 재검사      ← 12종 check_* 를 **DB 재조회 값**으로 실행     [코드]
  L2   Evidence 숫자 대조    ← ref_id → 원본 조회 → 값 비교 (허용오차 0)     [코드]
  L3   밴드 준수 · 축 침범   ← 최종 결정이 밴드 안인가                       [코드]
  L3.5 근거 등급 · 독립성    ← §7.3 check_evidence_grade / §7.1 source_ref  [코드]
  L4   rationale 논리 일관성 ← 여기만 LLM. temp 0, 생성 모델과 완전 분리     [LLM]

§6.4 핵심 — L1 은 self_check 와 **같은 함수**를 쓰되 **입력 데이터만 다르다**.
매입은 자기가 읽어온 capacity=12000 을 넣고, Critic 은 DB 에서 다시 조회한 값을 넣는다.
룰을 두 벌 짜지 않는다.

★ 이 모듈만이 원본 세션(verify_session)을 직접 받는다.
  오케스트레이터 노드는 세션을 손에 넣을 수 없다(§5.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.contracts.core import (
    _DEPT_AXES,
    HARD_ALLOWED_GRADES,
    Band,
    CheckResult,
    ClipResult,
    CriticFinding,
    CriticVerdict,
    Dept,
    ItemCode,
    T0Snapshot,
    T2Reply,
)

EPS = 1e-6


# ---------------------------------------------------------------------------
# 부서 제약 함수의 호출 규약
# ---------------------------------------------------------------------------


class CheckFn(Protocol):
    """
    각 부서가 제출하는 함수의 시그니처.

        def check_xxx(decision, ctx) -> CheckResult

    decision : {item: kg}   최종(클리핑된) 매입 수량
    ctx      : 제약 계산에 필요한 값들. **누가 채웠느냐가 핵심이다.**
               - self_check 경로 : 에이전트가 주장한 값
               - Critic  경로    : DB 에서 재조회한 값
               같은 함수, 다른 입력. 이것이 이중 방어선의 실질이다(§6.4).
    """

    def __call__(
        self, decision: Mapping[ItemCode, float], ctx: Mapping[str, Any]
    ) -> CheckResult: ...


class EvidenceResolver(Protocol):
    """(ref_id, claim) → 원본 실값. Critic 전용. as_of 로 잘린 세션을 내부에서 쓴다.

    🔴 **claim 이 키에 반드시 들어간다.** ref_id 하나가 여러 주장을 뒷받침하는 것이
    정상이기 때문이다 — DB 한 행이 *창고 여유* 와 *창고 점유* 를 동시에 뒷받침한다.
    ref_id 만으로 대조하면 **여유를 점유와 비교**하게 되고, 그 불일치는 실제 오류가
    아니라 **키가 부족해서 생긴 거짓 양성**이다 (실측 2026-08-29: 재무 1건 · 물류 2건이
    이 이유로 떴고, 통과안 3개가 그 때문에 반려됐다).
    """

    def __call__(self, ref_id: str, claim: str) -> float | None: ...


class RationaleJudge(Protocol):
    """L4 전용 LLM. 반드시 temp=0, 생성 모델과 다른 프롬프트/모델."""

    def __call__(self, payload: Mapping[str, Any]) -> tuple[bool, str]: ...


# ---------------------------------------------------------------------------
# L1 — 하드 제약 재검사
# ---------------------------------------------------------------------------


def run_l1(
    decision: Mapping[ItemCode, float],
    verify_ctx: Mapping[str, Any],
    check_fns: Mapping[str, CheckFn],
) -> list[CriticFinding]:
    findings: list[CriticFinding] = []
    for name, fn in check_fns.items():
        res = fn(decision, verify_ctx)  # ← ctx 가 DB 재조회 값이라는 점이 전부다
        if res.kind == "hard" and res.verdict == "reject":
            findings.append(
                CriticFinding(
                    layer="L1_hard",
                    check_id=name,
                    detail=res.reason,
                    ref_ids=tuple(r for e in res.evidences for r in e.ref_ids),
                    dept=res.dept,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# L2 — Evidence 숫자 대조
# ---------------------------------------------------------------------------


def run_l2(
    replies: Mapping[Dept, T2Reply],
    resolve: EvidenceResolver,
    tolerance: float = 0.0,
) -> list[CriticFinding]:
    """LLM 이 만드는 오류는 "없는 숫자를 근거로 든다"이다.
    이건 룰이 아니라 **입력이 달라야** 잡힌다(§6.4). 허용오차는 기본 0.

    ⚠️ **이 검사가 무엇을 잡고 무엇을 못 잡는지 분명히 해 둔다.**

    ```text
    잡는다   evidences 가 비어 있다
    잡는다   ref_id 를 원본에서 찾을 수 없다 — 지어낸 근거
    잡는다   같은 (ref_id, claim) 을 두 값으로 주장한다
    못 잡는다 주장값이 실제와 다르다  ← resolver 가 독립 원본일 때만 가능하다
    ```

    마지막 줄이 핵심이다. `service._evidence_resolver` 는 **회신 자신에서** 대조표를
    만들므로 독립 원본이 아니다. 그 배선에서는 값 대조가 성립하지 않으며, **그 사실을
    통과로 세지 않도록** `critic_bridge` 가 `skipped` 에 적는다.
    """
    findings: list[CriticFinding] = []
    for dept, reply in replies.items():
        for chk in reply.checks:
            if not chk.evidences:
                findings.append(
                    CriticFinding(
                        "L2_evidence", chk.check_id, "evidences 가 비어 있다 (§1.2-5)", (), dept
                    )
                )
                continue
            for ev in chk.evidences:
                for rid in ev.ref_ids:
                    actual = resolve(rid, ev.claim)
                    if actual is None:
                        findings.append(
                            CriticFinding(
                                "L2_evidence",
                                chk.check_id,
                                f"ref_id '{rid}' 를 원본에서 찾을 수 없다 — 존재하지 않는 근거",
                                (rid,),
                                dept,
                            )
                        )
                    elif abs(actual - ev.value) > tolerance + EPS:
                        findings.append(
                            CriticFinding(
                                "L2_evidence",
                                chk.check_id,
                                f"'{ev.claim}' 주장값 {ev.value:,.2f}{ev.unit} "
                                f"vs 원본 {actual:,.2f}{ev.unit}",
                                (rid,),
                                dept,
                            )
                        )
    return findings


# ---------------------------------------------------------------------------
# L3 — 밴드 준수 · 축 침범
# ---------------------------------------------------------------------------


def run_l3(
    clip: ClipResult,
    band: Band,
    replies: Mapping[Dept, T2Reply],
    unit_price: Mapping[ItemCode, float],
) -> list[CriticFinding]:
    findings: list[CriticFinding] = []
    q = clip.clipped_qty_kg

    for i, v in q.items():
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

    total = sum(q.values())
    if total > band.cap_total_kg + EPS:
        findings.append(
            CriticFinding(
                "L3_band_axis",
                "band.cap_total_kg",
                f"총 {total:,.0f}kg > 창고 상한 {band.cap_total_kg:,.0f}kg",
            )
        )

    amount = sum(q[i] * unit_price.get(i, 0.0) for i in q)
    if amount > band.cap_amount_krw + EPS:
        findings.append(
            CriticFinding(
                "L3_band_axis",
                "band.cap_amount_krw",
                f"총 {amount:,.0f}원 > 가용 {band.cap_amount_krw:,.0f}원",
            )
        )

    # 축 침범 — SuggestedAdjustment 생성자가 이미 막지만, 우회 경로를 이중으로 잡는다
    #
    # ★ v1.2.1 — 허용 축을 계약(_DEPT_AXES)에서 읽는다. 하드코딩하지 않는다.
    #   v1.2 는 여기에 {"sales": {"price", "quantity"}} 를 박아 뒀는데, 계약은 이미
    #   v0.2 에서 영업 축을 price → channel_mix 로 개명한 상태였다. 두 곳이 어긋나
    #   **계약상 정당한 channel_mix 제안이 축 침범으로 FAIL** 났다.
    #   L3 FAIL 은 T3 로 회송되므로 사후 루프 예산을 태우고 E2 보류로 끝난다.
    #   룰을 두 벌 짜지 않는다 (§6.4).
    for dept, reply in replies.items():
        for adj in reply.suggested_adjustments:
            allowed = set(_DEPT_AXES[dept])
            if adj.axis not in allowed:
                findings.append(
                    CriticFinding(
                        "L3_band_axis",
                        f"{dept}.suggested_adjustment",
                        f"{dept} 가 {adj.axis} 축을 침범했다. 허용: {sorted(allowed)} (§3.4.2)",
                        (),
                        dept,
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# L3.5 — 근거 등급(§7.3) · 제약 독립성(§7.1)
# ---------------------------------------------------------------------------


def check_evidence_grade(
    replies: Mapping[Dept, T2Reply],
    allow_assumed_hard: bool = True,  # ← 초기엔 True. 경고만 내고 단계적으로 조인다.
) -> list[CriticFinding]:
    """
    OFFICIAL/VENDOR → 하드 제약 허용, ASSUMED → 소프트 경고만 (§7.3).

    ⚠ 통합 페르소나 v1.2 값 대부분이 ASSUMED 이므로 처음 돌리면 경고가 대량 발생한다.
      allow_assumed_hard=True 로 시작해 FAIL 로 승격하는 시점을 팀이 정한다.
    """
    findings: list[CriticFinding] = []
    for dept, reply in replies.items():
        for chk in reply.checks:
            if chk.kind == "hard" and chk.evidence_grade not in HARD_ALLOWED_GRADES:
                if allow_assumed_hard:
                    continue  # 경고 단계 — findings 에 넣지 않는다
                findings.append(
                    CriticFinding(
                        "L3_5_grade",
                        chk.check_id,
                        f"하드 제약 '{chk.check_id}' 의 근거 등급이 {chk.evidence_grade} (§7.3). "
                        f"허용: {sorted(HARD_ALLOWED_GRADES)}",
                        (),
                        dept,
                    )
                )
    return findings


def check_constraint_independence(
    replies: Mapping[Dept, T2Reply],
    governed_axis_refs: Mapping[str, set[str]],
) -> list[CriticFinding]:
    """
    §7.1 — 하드 제약값이 그 제약이 규율하는 축의 값에서 파생되면 경고한다.

    통합 페르소나 v1.2 의 창고 capacity 가 위반 사례:
        SKU별 2일 수요 × 1.2 ÷ 800kg → 8 PLT → 다시 매입 수량을 제약
    수요가 창고 크기를 정하고 그 창고가 다시 매입을 제약하므로 영원히 바인딩되지 않는다.

    governed_axis_refs 예: {"check_warehouse_capacity": {"daily_demand_kg", "confirmed_orders"}}
    """
    findings: list[CriticFinding] = []
    for dept, reply in replies.items():
        for chk in reply.checks:
            banned = governed_axis_refs.get(chk.check_id, set())
            if chk.source_ref and any(b in chk.source_ref for b in banned):
                findings.append(
                    CriticFinding(
                        "L3_5_grade",
                        chk.check_id,
                        f"제약 독립성 위반(§7.1): source_ref='{chk.source_ref}' 가 "
                        f"이 제약이 규율하는 축({', '.join(sorted(banned))})을 참조한다",
                        (),
                        dept,
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# L3.5 추가 — 시장 기준 일치 (v0.2, 확인 4번)
# ---------------------------------------------------------------------------


def check_price_basis_consistency(
    snapshot: T0Snapshot,
    scenario_price_basis: str,
) -> list[CriticFinding]:
    """
    매입 기준 시장 = 판매 계약단가 산정 기준 시장.

    검토의견 §2 는 이것을 "§3.7.5 에 한 줄로 못 박자"고 제안했으나,
    문서 한 줄보다 코드 검사가 낫다. 매입을 경락가로 하면서 계약단가를
    중도매가 기준으로 산정하면 그 차이(중도매 마진)가 통째로 마진에 들어가
    **의사결정 효과와 시장 간 스프레드가 분리되지 않는다.**
    사후에 발견하면 손익 전체를 다시 돌려야 하는 종류의 오류다.
    """
    if scenario_price_basis != snapshot.contract_price_basis:
        return [
            CriticFinding(
                "L3_5_grade",
                "check_price_basis_consistency",
                f"매입 기준 시장({scenario_price_basis}) ≠ 계약단가 기준 시장"
                f"({snapshot.contract_price_basis}) — 시장 간 스프레드가 손익에 섞인다",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# L1 추가 — 삼중 일치 (v0.2, 검토의견 §3-2)
# ---------------------------------------------------------------------------


def check_identity_on_clipped(clip: ClipResult) -> list[CriticFinding]:
    """
    ★ 반드시 **클리핑된 값**에 대해 검사한다.
      원안에 대고 검사하면 T3 가 총량을 자를 때마다 FAIL 이 나고,
      클리핑이 발생하는 모든 날이 보류로 끝난다.
      T3 가 split_plan · sourcing_plan 을 함께 축소하므로 항등식은 유지된다.
    """
    problems = list(clip.identity_problems)
    problems += [f"min_lot 내림으로 floor 미달: {i}" for i in clip.floor_broken]
    return [CriticFinding("L1_hard", "check_triple_identity", p) for p in problems]


# ---------------------------------------------------------------------------
# L4 — rationale 논리 일관성 (유일한 LLM 지점)
# ---------------------------------------------------------------------------


def run_l4(
    clip: ClipResult,
    band: Band,
    replies: Mapping[Dept, T2Reply],
    rationale: str,
    judge: RationaleJudge | None,
) -> tuple[list[CriticFinding], str]:
    """
    ★ L4 FAIL 은 숫자를 바꾸지 않는다.
      L4 로 수량을 바꾸면 LLM 이 숫자를 만든 것이 되어 §1.2-3 위반이다.
      FAIL_ROUTING["L4_rationale"] = "T3_rationale_only" 인 이유.
    """
    if judge is None:
        return [], "(L4 skipped — judge 미주입)"

    payload = {
        "decision_kg": dict(clip.clipped_qty_kg),
        "original_kg": dict(clip.qty_kg),
        "binding_constraints": list(clip.binding_constraints),
        "band": {
            "floor": dict(band.floor_kg),
            "cap": dict(band.cap_kg),
            "cap_total_kg": band.cap_total_kg,
            "cap_amount_krw": band.cap_amount_krw,
        },
        "dept_reasons": {d: r.reasoning for d, r in replies.items()},
        "rationale": rationale,
    }
    ok, note = judge(payload)
    if ok:
        return [], note
    return [CriticFinding("L4_rationale", "rationale_consistency", note)], note


# ---------------------------------------------------------------------------
# 러너
# ---------------------------------------------------------------------------


def run_critic(
    *,
    as_of,
    run_seq: int,
    clip: ClipResult,
    band: Band,
    replies: Mapping[Dept, T2Reply],
    unit_price: Mapping[ItemCode, float],
    verify_ctx: Mapping[str, Any],
    check_fns: Mapping[str, CheckFn],
    resolve_evidence: EvidenceResolver,
    rationale: str = "",
    judge: RationaleJudge | None = None,
    governed_axis_refs: Mapping[str, set[str]] | None = None,
    allow_assumed_hard: bool = True,
    snapshot: T0Snapshot | None = None,
    scenario_price_basis: str | None = None,
) -> CriticVerdict:
    """
    계층 순서대로 돌린다. **앞 계층이 FAIL 이면 뒤는 돌리지 않는다** —
    L1 이 깨졌는데 L4 LLM 을 호출하는 것은 비용 낭비다.
    """
    findings: list[CriticFinding] = []

    findings += check_identity_on_clipped(clip)  # v0.2 — B1
    findings += run_l1(clip.clipped_qty_kg, verify_ctx, check_fns)
    if not findings:
        findings += run_l2(replies, resolve_evidence)
    if not findings:
        findings += run_l3(clip, band, replies, unit_price)
    if not findings:
        findings += check_evidence_grade(replies, allow_assumed_hard)
        findings += check_constraint_independence(replies, governed_axis_refs or {})
        if snapshot is not None and scenario_price_basis is not None:
            findings += check_price_basis_consistency(snapshot, scenario_price_basis)

    note = ""
    if not findings:
        l4, note = run_l4(clip, band, replies, rationale, judge)
        findings += l4

    return CriticVerdict(
        as_of=as_of,
        run_seq=run_seq,
        scenario_id=clip.scenario_id,
        passed=not findings,
        findings=tuple(findings),
        llm_note=note,
    )
