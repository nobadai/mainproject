"""
test_critic_v0_4.py — 오케스트레이터 · Critic 기능 테스트

두 가지를 한다.

  A. 오케스트레이터  — 기존 6케이스가 계속 완주하는가 (회귀)
  B. Critic v0.4     — 설계서가 요구하는 검사가 실제로 잡는가 / 놓치지 않는가

실행:
    python -m haetdeul.tests.test_critic_v0_4
"""

from __future__ import annotations

from datetime import timedelta

from fixtures import AS_OF, DEMAND, ITEMS4, PRICE_BASE, make_snapshot, sales_reply

from app.critic.critic import run_l3 as legacy_run_l3
from app.critic.critic_v0_4 import (
    CONTRACT_AMENDMENTS,
    DeptMeta,
    check_arrival_decomposition,
    check_axis_intrusion,
    check_finance_cap_grade_neutrality,
    check_obligation_authority,
    classify_collapse,
    run_critic_v04,
    run_l0,
)
from app.orchestrator.contracts_core import (
    Band,
    CheckResult,
    ClipResult,
    Evidence,
    MinimalScenario,
    SourcingLot,
    SplitLeg,
    SuggestedAdjustment,
    T2Reply,
)

PASS, FAIL = "✅", "❌"
_results: list[tuple[bool, str]] = []


def ok(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  {PASS if cond else FAIL} {label}")


def section(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------

SNAP = make_snapshot()


def _ev(claim, ref, value, unit, grade="OFFICIAL") -> Evidence:
    return Evidence(claim, "tool_calc", (ref,), value, unit, grade)


def _scenario(**kw) -> MinimalScenario:
    qty = kw.pop("qty", {i: DEMAND[i] * 5 for i in ITEMS4})
    item = next(iter(qty))
    base = {
        "scenario_id": "SCN-TEST",
        "strategy_type": "quantity",
        "stance": "기준",
        "qty_kg": qty,
        "unit_price_krw_per_kg": {i: PRICE_BASE.get(i, 1000.0) for i in qty},
        "split_plan": (SplitLeg(0, dict(qty), AS_OF + timedelta(days=2)),),
        "sourcing_plan": (
            SourcingLot(item, "상", qty[item], PRICE_BASE.get(item, 1000.0), ("SRC-1",)),
        ),
        "price_basis": "AUCTION",
    }
    base.update(kw)
    return MinimalScenario(**base)


def _clip(scn: MinimalScenario, clipped=None) -> ClipResult:
    c = clipped or dict(scn.qty_kg)
    return ClipResult(
        scenario_id=scn.scenario_id,
        qty_kg=dict(scn.qty_kg),
        clipped_qty_kg=c,
        clipped_split_plan=scn.split_plan,
        clipped_sourcing_plan=scn.sourcing_plan,
        clipped_amount_krw=sum(c[i] * PRICE_BASE.get(i, 1000.0) for i in c),
    )


def _wide_band() -> Band:
    return Band(
        floor_kg={i: 0.0 for i in ITEMS4},
        cap_kg={i: 1e9 for i in ITEMS4},
        cap_total_kg=1e9,
        cap_amount_krw=1e12,
        contributors={},
        cap_by_date_kg={},
    )


def _finance_reply(cap: float = 30_000_000.0) -> T2Reply:
    return T2Reply(
        "finance",
        AS_OF,
        (
            CheckResult(
                "check_projected_cash_min",
                "finance",
                "conditional",
                "hard",
                "가용자금",
                (_ev("가용자금", "SRC-FIN-001", cap, "krw"),),
                cap_amount_krw=cap,
                evidence_grade="OFFICIAL",
            ),
        ),
    )


def _resolver(mapping):
    return lambda rid: mapping.get(rid)


_REPLIES = {"sales": sales_reply(), "finance": _finance_reply()}
_REFS = {"SRC-SALES-001": sum(round(DEMAND[i], 1) for i in ITEMS4), "SRC-FIN-001": 30_000_000.0}


def _run(scn, clip, replies=None, meta=None, band=None, **kw):
    replies = replies if replies is not None else _REPLIES
    return run_critic_v04(
        as_of=AS_OF,
        run_seq=1,
        clip=clip,
        band=band or _wide_band(),
        snapshot=SNAP,
        scenario=scn,
        replies=replies,
        unit_price=dict(PRICE_BASE),
        verify_ctx={},
        check_fns={},
        resolve_evidence=_resolver(_REFS),
        dept_meta=meta,
        **kw,
    )


# ===========================================================================
# A. 오케스트레이터 회귀 — 기존 6케이스가 계속 완주하는가
# ===========================================================================

section("[A] 오케스트레이터 — 기존 파이프라인 회귀")

from fixtures import CASES

from app.orchestrator.band import clip_all, combine_band

for name, case in CASES.items():
    replies = case["replies"]
    band = combine_band(replies)
    clips = clip_all(case["scenarios"], band)
    live = [c for c in clips if not c.infeasible]
    ok(
        len(clips) == len(case["scenarios"]),
        f"{name}: 시나리오 {len(case['scenarios'])}안 전부 클리핑 결과 산출",
    )
    ok(
        all(c.total_kg <= band.cap_total_kg + 1e-6 for c in live),
        f"{name}: 클리핑 후 전 안이 창고 상한 이내",
    )


# ===========================================================================
# B. Critic v0.4
# ===========================================================================

section("\n[B-1] L0 형식 · 구조")

f = run_l0(_scenario())
ok(not f, "정상 시나리오는 L0 통과")

f = run_l0(_scenario(strategy_type="price"))
ok(
    any(x.check_id == "E-STRATEGY-GATE" for x in f),
    "strategy_type='price' → E-STRATEGY-GATE (구 E-AXIS-GATE 개명)",
)

f = run_l0(_scenario(stance="공격적"))
ok(any(x.check_id == "E-STANCE" for x in f), "stance 오타 → E-STANCE (축과 성향을 섞지 않는다)")

f = run_l0(_scenario(qty={"당근": 100.0}))
ok(any(x.check_id == "E-UNKNOWN-ITEM" for x in f), "미등록 품목 → E-UNKNOWN-ITEM")


section("\n[B-2] L1 신설 — E-AUTHORITY (S3 전속 판정 침범)")

ok(
    not check_obligation_authority("sales", ["today_floor", "shortage_kg"]),
    "영업이 자기 도메인 사실만 반환 → 통과",
)

f = check_obligation_authority("sales", ["today_floor", "has_unmet_obligation"])
ok(any(x.check_id == "E-AUTHORITY" for x in f), "영업이 has_unmet_obligation 산출 → E-AUTHORITY")

f = check_obligation_authority("inventory", ["on_hand", "has_unmet_obligation"])
ok(f and f[0].dept == "inventory", "재고가 산출해도 동일하게 차단 (부서 무관)")


section("\n[B-3] L1 신설 — E-GRADE-LEAK (재무 cap 등급 개입)")

fin_chk = _finance_reply().checks[0]

ok(
    not check_finance_cap_grade_neutrality(fin_chk, ["cash_balance", "salary_due"]),
    "재무가 순수 금액만으로 cap 산출 → 통과",
)

f = check_finance_cap_grade_neutrality(fin_chk, ["cash_balance", "grade_unit_price"])
ok(any(x.check_id == "E-GRADE-LEAK" for x in f), "재무 cap 에 grade_unit_price 개입 → E-GRADE-LEAK")

f = check_finance_cap_grade_neutrality(fin_chk, ["avg_unit_price", "total_qty_kg"])
ok(
    len(f) == 1 and "avg_unit_price" in f[0].detail and "total_qty_kg" in f[0].detail,
    "복수 누출 키를 한 건으로 묶어 보고",
)

sales_chk = sales_reply().checks[0]
ok(
    not check_finance_cap_grade_neutrality(sales_chk, ["grade_unit_price"]),
    "영업 검사에는 적용하지 않는다 (재무 금액축 전용)",
)


section("\n[B-4] L3 축 침범 — 계약(_DEPT_AXES) 단일 출처 교정")

adj = SuggestedAdjustment("sales", "channel_mix", 100.0, "kg", "채널 재배분", ("SRC-S-1",))
r = T2Reply("sales", AS_OF, sales_reply().checks, (adj,))

legacy = legacy_run_l3(_clip(_scenario()), _wide_band(), {"sales": r}, dict(PRICE_BASE))
ok(
    not any("channel_mix" in x.detail for x in legacy),
    "critic.run_l3 이 계약상 정당한 channel_mix 를 통과시킨다 (v1.2.1 수정)",
)

ok(
    not check_axis_intrusion({"sales": r}),
    "v0.4 check_axis_intrusion 도 동일 — 두 경로가 계약이라는 단일 출처를 읽는다",
)

bogus = SuggestedAdjustment("inventory", "timing", 1.0, "d", "이연", ("SRC-I-1",))
r_inv = T2Reply("inventory", AS_OF, (), (bogus,))
ok(
    not legacy_run_l3(_clip(_scenario()), _wide_band(), {"inventory": r_inv}, dict(PRICE_BASE))
    and not check_axis_intrusion({"inventory": r_inv}),
    "재고 timing 축도 두 경로에서 동일하게 통과",
)

bad = SuggestedAdjustment("finance", "amount", 1.0, "krw", "한도", ("SRC-F-1",))
r_bad = T2Reply("finance", AS_OF, _finance_reply().checks, (bad,))
ok(not check_axis_intrusion({"finance": r_bad}), "재무 amount 축은 정당 → 통과")


section("\n[B-5] L4 신설 — 회차별 도착일 분해")

scn = _scenario()
qty = dict(scn.qty_kg)
half = {i: v / 2 for i, v in qty.items()}
split_ok = (
    SplitLeg(0, dict(half), AS_OF + timedelta(days=2)),
    SplitLeg(3, dict(half), AS_OF + timedelta(days=5)),
)
scn2 = _scenario(split_plan=split_ok)
f, s = check_arrival_decomposition(_clip(scn2), 2)
ok(not f and not s, "회차별 도착일이 전부 있고 수량 합이 맞으면 통과")

# 회차 합 ≠ 총량
scn3 = _scenario(split_plan=(SplitLeg(0, dict(half), AS_OF + timedelta(days=2)),))
f, s = check_arrival_decomposition(_clip(scn3), 2)
ok(any(x.check_id == "E-ARRIVAL-SUM" for x in f), "회차별 수량 합 ≠ 총량 → E-ARRIVAL-SUM")

# N4 미결 + 도착일 부재 → FAIL 이 아니라 skipped
scn4 = _scenario(split_plan=(SplitLeg(0, dict(half), None), SplitLeg(3, dict(half), None)))
f, s = check_arrival_decomposition(_clip(scn4), None)
ok(not f and s, "N4 미결 + 도착일 부재 → FAIL 아님, skipped 로 기록 (§1.2-10)")

f, s = check_arrival_decomposition(_clip(_scenario(split_plan=())), 2)
ok(not f and not s, "분할이 없으면 검사 대상 아님")


section("\n[B-6] L4 — 날짜별 창고 점유 (cap_by_date, T1·T3·Critic 공용 함수)")

d_arrive = AS_OF + timedelta(days=2)
tight = Band(
    floor_kg={i: 0.0 for i in ITEMS4},
    cap_kg={i: 1e9 for i in ITEMS4},
    cap_total_kg=1e9,
    cap_amount_krw=1e12,
    contributors={},
    cap_by_date_kg={d_arrive: 3_100.0},  # 확정 점유 3,000 + 여유 100kg
)
v = _run(_scenario(), _clip(_scenario()), band=tight)
ok(
    v.status == "FAIL" and any("occupancy" in x.check_id for x in v.findings),
    "도착일 점유가 cap_by_date 초과 → Critic 이 감사 지점에서 잡는다",
)

v = _run(_scenario(), _clip(_scenario()))
ok(
    "check_occupancy_by_date" in " ".join(v.skipped),
    "cap_by_date 미제공(N15) 이면 통과가 아니라 skipped 로 남긴다",
)


section("\n[B-7] L4 — 붕괴 유형 분류 (AXIS vs QUANTITY)")

a1, a2 = _scenario(scenario_id="A1"), _scenario(scenario_id="A2")
collapsed, ctype = classify_collapse([_clip(a1), _clip(a2)], {"A1": a1, "A2": a2})
ok(collapsed and ctype == "AXIS", "전 안이 같은 strategy_type + 같은 지문 → AXIS (T1 문제)")

t1 = _scenario(scenario_id="T1", strategy_type="timing")
t2 = _scenario(scenario_id="T2", strategy_type="quantity")
c1, c2 = _clip(t1), _clip(t2)
collapsed, ctype = classify_collapse([c1, c2], {"T1": t1, "T2": t2})
ok(
    collapsed and ctype == "QUANTITY",
    "축은 달랐는데 클리핑 후 지문이 같음 → QUANTITY (회사 상태 문제)",
)

t3 = _scenario(scenario_id="T3", strategy_type="timing", split_plan=split_ok)
collapsed, _ = classify_collapse([_clip(t2), _clip(t3)], {"T2": t2, "T3": t3})
ok(not collapsed, "총량 동일 + 분할 상이는 붕괴가 아니다 (B2 유지)")


section("\n[B-8] L5 — CONCERN 은 FAIL 이 아니다 (§4.3 LLM 은 결정을 죽일 수 없다)")

scn = _scenario()
clip = _clip(scn)

v = _run(scn, clip, judge=lambda p: (True, "일관성 OK"))
ok(v.status == "PASS", "judge 통과 → PASS")

v = _run(scn, clip, judge=lambda p: (False, "근거와 결론이 어긋난다"))
ok(v.status == "CONCERN", "judge 지적 → FAIL 이 아니라 CONCERN")
ok(v.passed is True, "CONCERN 은 passed=True — 파이프라인을 막지 않는다")
ok(v.route is None, "CONCERN 은 회송 없음")
ok(
    len(v.concerns) == 1 and v.concerns[0].code == "E-LOGIC",
    "CONCERN 내용이 사람 승인 화면용으로 보존된다",
)

v = _run(scn, clip, judge=lambda p: (False, "x"), unattended=True)
ok(
    "UNATTENDED_CONCERN_APPROVED" in v.llm_note,
    "무인 구간: 승인하되 별도 카운트 (자동승인도 자동반려도 아니다)",
)


section("\n[B-9] 커버리지 · end_stage")

v = _run(scn, clip, judge=lambda p: (True, ""))
ran, total = v.coverage_ratio
ok(total == 56, f"설계서 §6 총 검사 수 56 과 일치 (현재 {ran}/{total} 실행)")
ok(ran < total, "미결(N-계열)로 돌지 않은 검사가 있음을 커버리지가 드러낸다")
ok(v.badge().startswith("PASS ("), f"화면 배지 형식: {v.badge()}")

v = _run(_scenario(strategy_type="price"), clip)
ok(
    v.status == "FAIL" and v.end_stage == "CRITIC_A",
    "Critic FAIL 로 끝난 날의 end_stage 는 CRITIC_A (T3 로 뭉치지 않는다)",
)

v = _run(
    scn,
    clip,
    meta={
        "finance": DeptMeta(
            inputs_used={"check_projected_cash_min": ["cash_balance"]},
            produced_fields=["max_feasible_amount_krw"],
        )
    },
)
ok(
    not any("DeptMeta 미제출" in s for s in v.skipped if "finance" in s),
    "DeptMeta 를 낸 부서는 권한·누출 검사가 실제로 돈다",
)

v = _run(scn, clip)
ok(
    any("DeptMeta 미제출" in s for s in v.skipped),
    "DeptMeta 미제출 부서는 조용히 통과가 아니라 skipped 로 드러난다",
)


section("\n[B-11] v1.2.1 패치 회귀 — 사이클 A·B 결함 4건")

from dataclasses import replace as _dc_replace

from fixtures import _std_replies, make_scenarios, make_split_variants

from app.orchestrator.band import check_occupancy_detailed
from app.orchestrator.contracts_core import (
    ApprovedPurchaseCommitment,
    ArrivalLeg,
    ContractViolation,
    CycleBState,
    PipelineState,
)
from app.orchestrator.cycle import CycleHooks, build_commitment, run_day

_REPLIES_STD = _std_replies()

# ── 결함 2: 클리핑이 회차별 도착일을 버렸다 ────────────────────
_scns = make_split_variants()
_band = combine_band(_REPLIES_STD)
_t2 = next(c for c in clip_all(_scns, _band) if c.scenario_id == "SCN-T2")
_src = next(s for s in _scns if s.scenario_id == "SCN-T2")
ok(
    [l.expected_arrival_date for l in _t2.clipped_split_plan]
    == [l.expected_arrival_date for l in _src.split_plan],
    "클리핑이 회차별 도착일을 보존한다 (원안과 동일)",
)

# ── 결함 3: 빈 결과가 '통과'로 읽히지 않는다 ───────────────────
_snap_null = _dc_replace(SNAP, inbound_lead_days=None)
_no_date = ClipResult(
    scenario_id="X",
    qty_kg={"배추": 100.0},
    clipped_qty_kg={"배추": 100.0},
    clipped_split_plan=(SplitLeg(0, {"배추": 100.0}, None),),
)
_tight = Band(
    {i: 0.0 for i in ITEMS4},
    {i: 1e9 for i in ITEMS4},
    1e9,
    1e12,
    {},
    {AS_OF + timedelta(days=2): 3_100.0},
)
_res = check_occupancy_detailed(_no_date, _tight, _snap_null)
ok(
    not _res.problems and not _res.ran,
    "도착일 부재 + N4 미결 → 위반 0건이지만 ran=False (미검사와 통과를 구분)",
)
ok(_res.skipped and "N4" in _res.skipped[0], f"스킵 사유가 남는다: {_res.skipped[0]}")

_res2 = check_occupancy_detailed(_t2, _tight, SNAP)
ok(_res2.ran and _res2.legs_dated == 2, "회차 2건 전부 도착일로 검사됨 (ran=True)")

# ── 결함 4: 승인 약정이 회차별 도착을 보존한다 ─────────────────
_st = PipelineState(snapshot=SNAP)
_st.clip_results = [_t2]
_st.approved_scenario_id = "SCN-T2"
_c = build_commitment(_st)
ok(len(_c.arrival_schedule) == 2, "arrival_schedule 이 회차 수만큼 생성된다")
ok(
    abs(sum(a.qty_kg for a in _c.arrival_schedule) - _c.total_qty_kg) < 0.5,
    "회차별 수량 합 == 총량 (계약이 __post_init__ 에서 강제)",
)
ok(
    _c.expected_arrival_date == min(a.date for a in _c.arrival_schedule),
    "expected_arrival_date 는 최초 도착일",
)

try:
    ApprovedPurchaseCommitment(
        approval_id="X",
        as_of=AS_OF,
        total_amount_krw=1.0,
        total_qty_kg=100.0,
        payment_date=None,
        expected_arrival_date=None,
        source_scenario_id="X",
        ref_ids=("r",),
        arrival_schedule=(ArrivalLeg(AS_OF, 40.0, 1),),
    )
    ok(False, "회차 합 ≠ 총량이면 계약이 거부한다")
except ContractViolation:
    ok(True, "회차 합 ≠ 총량이면 계약이 거부한다")

# overlay 가 회차별 누적으로 차감하는가
_cb = CycleBState(snapshot=SNAP, commitment=_c)
_base = {a.date: 10_000.0 for a in _c.arrival_schedule}
_ov = _cb.cap_by_date_overlay(_base)
_d1, _d2 = sorted(_base)
ok(
    _ov[_d1] > _ov[_d2],
    f"1회차일({_d1}) 여유 {_ov[_d1]:,.0f} > 2회차일({_d2}) 여유 {_ov[_d2]:,.0f} — 누적 차감",
)
ok(
    len([l for l in _cb.in_transit if l.item == "(승인분)"]) == 2,
    "in_transit 이 회차별 로트 2건으로 들어간다 (총량 1건이 아니다)",
)

# ── 결함 1: 사이클 B 훅이 overlay 를 본다 ──────────────────────
_seen = {}


def _mk(cycle, scenarios):
    def propose(s):
        s.scenarios = scenarios
        return s

    def advise(s):
        s.replies = _REPLIES_STD
        return s

    def adjust(s):
        s.band = combine_band(s.replies)
        s.clip_results = clip_all(s.scenarios, s.band)
        s.ranked_ids = [r.scenario_id for r in s.clip_results]
        _seen[cycle] = s.cycle_b_state  # ★ 훅 실행 시점의 값
        return s

    return CycleHooks(
        cycle,
        propose,
        advise,
        adjust,
        lambda s: (s, False),
        lambda s: (s, False),
        lambda s: s.ranked_ids[0] if s.ranked_ids else None,
    )


run_day(SNAP, _mk("A", make_scenarios()), _mk("B", make_scenarios()))
ok(_seen["A"] is None, "사이클 A 훅은 overlay 를 받지 않는다 (정상)")
ok(_seen["B"] is not None, "사이클 B 훅이 **실행 중에** overlay 를 본다")
ok(_seen["B"].commitment is not None, "overlay 에 H1 승인 약정이 실려 있다")


section("\n[B-12] 사이클 B 골격 배선")

from fixtures_cycle_b import (
    CASES_B,
    finance_reply_b,
    inventory_reply_b,
    make_allocations,
)
from run_day_stub import _hooks_a, _hooks_b

from app.orchestrator.graph import node_t3_combine
from app.orchestrator.graph_b import build_cycle_b_hooks
from app.orchestrator.outbound import (
    clip_allocations,
    combine_outbound_band,
    detect_allocation_collapse,
)

# ── S3 결합 ────────────────────────────────────────────────────
_allocs = make_allocations()
_bb = combine_outbound_band({"inventory": inventory_reply_b(shared_outbound_kg=400.0)})
ok(_bb.cap_total_kg == 400.0, "공용 출고 능력이 cap_total_kg 로 결합된다")
ok(
    set(_bb.cap_kg) == set(ITEMS4) and all(v == 200.0 for v in _bb.cap_kg.values()),
    "품목별 상한이 cap_kg 로 결합된다",
)
ok(_bb.cap_total_effective_kg == 400.0, "실효 상한 = min(공용 능력 400, 재고 합 800)")

# ── 공용 자원이 결합 제약으로 실제 작동하는가 (§3.7.4 검사 3번) ──
_cl = clip_allocations(_allocs, _bb)
_full = next(c for c in _cl if c.scenario_id == "ALO-100")
ok(
    all(v <= 200.0 + 1e-6 for v in _full.qty_kg.values()),
    "품목별로는 전부 on_hand 이내 — 개별 검사로는 안 잡힌다",
)
ok(
    abs(sum(_full.clipped_qty_kg.values()) - 400.0) < 1e-6,
    "합치면 공용 능력 초과 → 400kg 로 클리핑 (품목별 검사가 못 잡는 것을 잡는다)",
)
ok("cap_total_kg" in _full.binding_constraints, "구속 제약이 기록된다")

_ratios = {i: _full.clipped_qty_kg[i] / _full.qty_kg[i] for i in _full.qty_kg}
ok(
    max(_ratios.values()) - min(_ratios.values()) < 1e-9,
    "비례 축소 — 품목 간 축소율이 동일 (선착순 아님, §3.7.3)",
)

# ── 금액은 단가 재계산 없이 비율만 적용 ─────────────────────────
_src = next(a for a in _allocs if a.allocation_id == "ALO-100")
_orig_amt = sum(l.qty_kg * l.unit_price_krw_per_kg for l in _src.legs)
ok(
    abs(_full.clipped_amount_krw - _orig_amt * 0.5) < 1.0,
    "클리핑 금액 = 원금액 × 축소비율 (오케스트레이터가 단가를 만들지 않는다, §5.1)",
)

# ── 수렴 판정 ──────────────────────────────────────────────────
_tight = combine_outbound_band({"inventory": inventory_reply_b(shared_outbound_kg=100.0)})
ok(
    detect_allocation_collapse(clip_allocations(_allocs, _tight)),
    "전 후보가 같은 값으로 수렴 → 붕괴",
)
ok(not detect_allocation_collapse(_cl), "400kg 에서는 후보가 갈린다 → 붕괴 아님")

# ── 하루 전체 완주 (T0 → A → H1 → B → H2 → T4) ─────────────────
_A = _hooks_a(make_scenarios(), _REPLIES_STD)
for _name, _case in CASES_B.items():
    _r = run_day(SNAP, _A, _hooks_b(_case["allocations"], _case["shared_outbound_kg"]))
    ok(_r.cycle_b is not None, f"{_name}: 사이클 B 가 실행된다")
    ok(
        _r.end_code in ("E1_APPROVED", "E2_HELD", "E5_NO_FEASIBLE_PLAN"),
        f"{_name}: end_code={_r.end_code}",
    )

# ── 배선 불변식 ────────────────────────────────────────────────
_r = run_day(SNAP, _A, _hooks_b(CASES_B["B_NORMAL"]["allocations"], 2_500.0))
ok(_r.cycle_b.approved_scenario_id is not None, "H2 승인이 오케스트레이터를 통해 나온다")
ok(_r.cycle_a.log.b.candidate_count > 0, "B 기록이 하루 한 행의 b_ 컬럼군에 남는다 (a_ 가 아니다)")
ok(_r.cycle_b.outbound_band is not None, "OutboundBand 가 state 에 실린다")
ok(
    _r.cycle_b.cycle_b_state.commitment is not None,
    "S2 가 overlay 를 본다 — H1 승인 약정이 실려 있다",
)
ok(
    _r.cycle_a.log.has_unmet_obligation is not None,
    "의무 충족 판정은 S3(run_day)가 산출한다 — 부서가 아니다 (§5.0)",
)

_r0 = run_day(SNAP, _A, _hooks_b(CASES_B["B_NO_CAPACITY"]["allocations"], 0.0))
ok(_r0.cycle_b.approved_scenario_id is None, "출고 능력 0 → 판매 0 으로 마감")
ok(_r0.cycle_a.log.b.pre_loop_used == 2, "S3→S1 회송이 예산(2)까지 돌고 멈춘다")
ok(_r0.end_code == "E1_APPROVED", "매입 정상 + 판매 0 → E1 (오늘 안 파는 것도 정상 결정, §5.0)")


section("\n[B-13] 영업 IO 명세 v0.6 반영")

from fixtures_cycle_b import make_sales_facts

from app.orchestrator.contracts_core import (
    HOLD_CHANNEL,
    ChannelLeg,
    MinimalAllocation,
    OutboundLeg,
    SalesFacts,
)
from app.orchestrator.outbound import clip_allocation

# ── HOLD 는 출고가 아니다 (§5) ──────────────────────────────────
_legs = (
    ChannelLeg("KIMCHI_FACTORY", "배추", 250.0, 2293.0, ("L1",)),
    ChannelLeg("SPOT", "배추", 100.0, 2580.0, ("L4",)),
    ChannelLeg(HOLD_CHANNEL, "배추", 150.0, 0.0, ("L1",)),
)
_a = MinimalAllocation("S-A", "균형", _legs, outbound_by_date=(OutboundLeg(AS_OF, 350.0),))
ok(_a.qty_by_item["배추"] == 500.0, "qty_by_item 은 배분 총량 (HOLD 포함)")
ok(_a.outbound_qty_by_item["배추"] == 350.0, "outbound_qty_by_item 은 HOLD 제외")

_wide = Band({i: 0.0 for i in ITEMS4}, {"배추": 1e9}, 1e9, 1e12, {}, {})
_c = clip_allocation(_a, _wide)
ok(
    _c.qty_kg["배추"] == 350.0,
    "클리핑 대상이 출고량 350kg — 보유 150kg 이 출고 능력을 잡아먹지 않는다",
)
ok(
    abs(_c.clipped_amount_krw - (250 * 2293 + 100 * 2580)) < 1.0,
    "금액에도 HOLD 가 빠진다 — 팔지 않은 물량에는 매출이 없다",
)

# ── outbound_by_date 가 결합 검사의 정본 (§5) ───────────────────
_a2 = MinimalAllocation(
    "S-B",
    "균형",
    (ChannelLeg("KIMCHI_FACTORY", "배추", 100.0, 2293.0, ("L1",)),),
    outbound_by_date=(OutboundLeg(AS_OF, 300.0),),
)
_tight2 = Band({i: 0.0 for i in ITEMS4}, {"배추": 1e9}, 250.0, 1e12, {}, {})
_c2 = clip_allocation(_a2, _tight2)
ok(
    "cap_total_kg" in _c2.binding_constraints,
    "전략 배분 100kg 이지만 총 출고 300kg(확정 납품 포함) > 250kg → 구속",
)
ok(
    _c2.clipped_qty_kg["배추"] < 100.0,
    "초과분을 전략 배분에서 깎는다 — 확정 납품분은 의무라 줄이지 않는다",
)

# ── skipped verdict (§1) ───────────────────────────────────────
_sk = CheckResult(
    "check_delivery_deadline",
    "sales",
    "skipped",
    "hard",
    "inbound_lead_days 가 null — 납기 실행 가능성 검사 불가",
    (_ev("리드타임", "SRC-N4", 0.0, "d"),),
)
_r_sk = T2Reply("sales", AS_OF, sales_reply().checks + (_sk,))
ok(_r_sk.verdict == "ok", "skipped 는 verdict 를 움직이지 않는다 (§3 — ok|conditional)")
ok(len(_r_sk.skipped_checks) == 1, "skipped_checks 로 드러난다 — 통과와 구분된다")
ok(not _sk.is_binding, "skipped 는 밴드를 구속하지 않는다")

# ── 영업 사실 보고로 E5 를 판정한다 (§5) ────────────────────────
_facts = make_sales_facts(coverable_ratio=1.0)
ok(
    isinstance(_facts, SalesFacts) and _facts.coverable_kg,
    "영업은 confirmed_obligation_kg · coverable_kg 라는 사실만 낸다",
)
ok(
    not hasattr(_facts, "has_unmet_obligation"),
    "영업 출력에 has_unmet_obligation 이 없다 — S3 전속 (§5.0)",
)

_full = run_day(SNAP, _A, _hooks_b(CASES_B["B_NORMAL"]["allocations"], 2_500.0))
ok(
    _full.cycle_a.log.has_unmet_obligation is False,
    "충당 가능하면 unmet=False — v1.2.2 는 전략 배분만 세어 매일 True 였다",
)

_short = _hooks_b(CASES_B["B_NORMAL"]["allocations"], 2_500.0)
_short_hooks = build_cycle_b_hooks(
    sales_agent=lambda snap, retry: (
        CASES_B["B_NORMAL"]["allocations"],
        make_sales_facts(coverable_ratio=0.5),
    ),
    dept_agents={
        "inventory": lambda s, st, a: inventory_reply_b(shared_outbound_kg=2_500.0),
        "finance": lambda s, st, a: finance_reply_b(st, s.base_cash_priority),
    },
    selector=lambda st: [r.scenario_id for r in st.clip_results if not r.infeasible],
    approver=lambda st: st.ranked_ids[0] if st.ranked_ids else None,
)
_part = run_day(SNAP, _A, _short_hooks)
ok(_part.cycle_a.log.has_unmet_obligation is True, "충당 가능량이 의무의 절반이면 unmet=True")


section("\n[B-14] combine_band 품목별 수용 · runtime_status")

from fixtures import _ev, finance_reply, inventory_reply

from app.orchestrator.contracts_core import ContractViolation


def _sales_for(item, floor=None):
    """영업 IO 명세 §0 — run_floor_reply(item, ...) 는 품목마다 호출된다."""
    v = DEMAND[item] if floor is None else floor
    return T2Reply(
        "sales",
        AS_OF,
        (
            CheckResult(
                "check_confirmed_demand_total",
                "sales",
                "ok",
                "hard",
                "확정수요",
                (_ev("확정수요", f"SRC-S-{item}", v, "kg"),),
                floor_kg={item: v},
            ),
        ),
        item=item,
    )


_per_item = combine_band(
    {
        "sales": [_sales_for(i) for i in ITEMS4],
        "inventory": inventory_reply(),
        "finance": finance_reply(30_000_000.0),
    }
)
ok(
    all(abs(_per_item.floor_kg[i] - DEMAND[i]) < 0.1 for i in ITEMS4),
    "품목별 영업 회신 4개가 하나의 floor 벡터로 결합된다",
)
ok(
    _per_item.cap_total_kg == 17_600.0 and _per_item.cap_amount_krw == 30_000_000.0,
    "전사 단일 회신(재무 ALL_ITEMS_TOTAL · 재고 집계)은 그대로 결합된다",
)
ok(_per_item.usable, "전 부서 READY → 밴드 사용 가능")

_single = combine_band(
    {"sales": sales_reply(), "inventory": inventory_reply(), "finance": finance_reply(30_000_000.0)}
)
ok(
    _single.cap_total_kg == _per_item.cap_total_kg,
    "부서당 단일 회신 형태도 그대로 동작 (하위 호환)",
)

# 품목별 회신이 남의 품목을 채우면 계약 위반
try:
    combine_band(
        {
            "sales": [
                T2Reply(
                    "sales",
                    AS_OF,
                    (
                        CheckResult(
                            "x",
                            "sales",
                            "ok",
                            "hard",
                            "r",
                            (_ev("c", "SRC-1", 1.0, "kg"),),
                            floor_kg={"배추": 100.0, "무": 50.0},
                        ),
                    ),
                    item="배추",
                )
            ]
        }
    )
    ok(False, "배추 회신이 무의 floor 를 채우면 거부한다")
except ContractViolation:
    ok(True, "배추 회신이 무의 floor 를 채우면 거부한다")

# runtime_status 가 READY 가 아니면 밴드에 넣지 않는다
_down = T2Reply("inventory", AS_OF, inventory_reply().checks, runtime_status="RUNTIME_NOT_READY")
_b_down = combine_band(
    {"sales": [_sales_for(i) for i in ITEMS4], "inventory": _down, "finance": finance_reply(3e7)}
)
ok(_b_down.not_ready == ("inventory",), "미가동 부서가 not_ready 에 기록된다")
ok(not _b_down.usable, "not_ready 가 있으면 밴드를 쓸 수 없다")
ok(
    _b_down.cap_total_kg == float("inf"),
    "미가동 부서의 cap 은 무한대로 남는다 — 그래서 조용히 통과시키면 안 된다",
)

_st = PipelineState(snapshot=SNAP)
_st.replies = {
    "sales": [_sales_for(i) for i in ITEMS4],
    "inventory": _down,
    "finance": finance_reply(3e7),
}
_st.scenarios = make_scenarios()
node_t3_combine(_st)
ok(
    _st.log.end_code == "E4_NOT_STARTED",
    "T3 가 미가동을 E4 로 끊는다 — 교착(E3)이 아니라 실행 환경 문제",
)
ok(not _st.clip_results, "클리핑을 수행하지 않는다 — 무제한 매입 방지")


section("\n[B-15] 시나리오 독립성 — 부서 회신은 매입안을 읽지 않는다 (§3.6.1)")

from app.critic.critic_v0_4 import check_scenario_independence

_inv_chk = inventory_reply().checks[0]
_fin_chk = _finance_reply().checks[0]
_sal_chk = sales_reply().checks[0]

ok(
    not check_scenario_independence(
        "inventory", _inv_chk, ["cap_by_date", "confirmed_inbound_schedule"]
    ),
    "재고가 확정분만 읽으면 통과 (§3.6.7 — cap_by_date 는 확정분만)",
)

f = check_scenario_independence("inventory", _inv_chk, ["cap_by_date", "purchase_output"])
ok(
    any(x.check_id == "E-SCENARIO-LEAK" for x in f),
    "재고가 purchase_output 을 읽으면 → E-SCENARIO-LEAK",
)

f = check_scenario_independence("finance", _fin_chk, ["current_cash", "sourcing_plan"])
ok(
    any(x.check_id == "E-SCENARIO-LEAK" for x in f),
    "재무가 sourcing_plan 을 읽어도 동일하게 차단 (부서 무관)",
)

f = check_scenario_independence("sales", _sal_chk, ["confirmed_orders", "total_amount_krw"])
ok(
    any(x.check_id == "E-SCENARIO-LEAK" for x in f),
    "영업 T2 도 동일 — 영업 IO 명세 §1 '매입안을 받지 않는다'",
)

ok(
    not check_scenario_independence(
        "finance",
        _fin_chk,
        ["current_cash", "payroll_schedule", "usable_remaining_borrowing_capacity"],
    ),
    "재무가 현금흐름 축만으로 이분탐색하면 통과 (§3.6.8)",
)

# 러너에서도 실제로 도는가
_v = _run(
    _scenario(),
    _clip(_scenario()),
    meta={
        "finance": DeptMeta(
            inputs_used={"check_projected_cash_min": ["current_cash", "purchase_output"]},
            produced_fields=["max_feasible_amount_krw"],
        )
    },
    replies={"finance": _finance_reply()},
)
ok(
    _v.status == "FAIL" and any(x.check_id == "E-SCENARIO-LEAK" for x in _v.findings),
    "러너 L1 에서 E-SCENARIO-LEAK 이 실제로 발화한다",
)


section("\n[B-16] 부서당 1회 회신 — 회송은 T1 만 다시 돈다 (§3.1 · §3.6.1)")

from app.orchestrator.cycle import run_subcycle


def _count_calls(n_retry: int, critic_route=None):
    calls = {"T1": 0, "T2": 0, "T2_fix": 0}

    def propose(s):
        calls["T1"] += 1
        s.scenarios = make_scenarios()
        return s

    def advise(s):
        calls["T2"] += 1
        s.replies = _REPLIES_STD
        return s

    def re_advise(s):
        calls["T2_fix"] += 1
        return s

    def adjust(s):
        s.band = combine_band(s.replies)
        s.clip_results = clip_all(s.scenarios, s.band)
        s.ranked_ids = [r.scenario_id for r in s.clip_results]
        return s

    seen = {"gate": 0, "verify": 0}

    def gate(s):
        seen["gate"] += 1
        return (s, seen["gate"] <= n_retry)

    def verify(s):
        seen["verify"] += 1
        if critic_route and seen["verify"] == 1:
            s.critic = type("V", (), {"route": critic_route, "passed": False})()
            return (s, True)
        return (s, False)

    run_subcycle(
        SNAP,
        CycleHooks(
            "A",
            propose,
            advise,
            adjust,
            gate,
            verify,
            lambda s: s.ranked_ids[0] if s.ranked_ids else None,
            re_advise=re_advise,
        ),
    )
    return calls


_c0 = _count_calls(0)
ok(_c0["T1"] == 1 and _c0["T2"] == 1, "회송 없으면 T1 1회 · T2 1회")

_c2 = _count_calls(2)
ok(_c2["T2"] == 1, f"회송 2회여도 T2 는 1회 — 부서당 1회 (§3.1). 실제 {_c2['T2']}회")
ok(_c2["T1"] == 3, "T1 은 회송마다 다시 돈다 (시나리오 재생성이 회송의 목적)")

_cr = _count_calls(0, critic_route="T1_purchase")
ok(_cr["T1"] == 2 and _cr["T2"] == 1, "Critic L1 FAIL(T1_purchase) → T1 재생성, 부서는 그대로")

_cd = _count_calls(0, critic_route="T3_combine")
ok(_cd["T1"] == 1 and _cd["T2"] == 1, "Critic L3 FAIL(T3_combine) → 재조정만, T1·T2 재호출 없음")

_cf = _count_calls(0, critic_route="T2_dept")
ok(
    _cf["T2"] == 1 and _cf["T2_fix"] == 1,
    "Critic L2 FAIL(T2_dept) → re_advise 로 정정 (밴드 재계산이 아니라 회신 정정)",
)


section("\n[B-17] 밴드는 하루 한 번 결합 · 회송은 이력으로 (§3.6.1)")


_bst = PipelineState(snapshot=SNAP)
_bst.replies = _REPLIES_STD
_bst.scenarios = make_scenarios()

node_t3_combine(_bst)
_band1 = _bst.band
ok(_band1 is not None and len(_bst.log.a.attempts) == 1, "1회차 — 밴드 결합 + 시도 1건 기록")
ok(_bst.log.a.attempts[0].trigger == "INITIAL", "첫 회차 trigger 는 INITIAL")

# 회송을 흉내 내 다시 조정
_bst.scenarios = make_scenarios(cover_days=(2, 5))
node_t3_combine(_bst)
ok(_bst.band is _band1, "밴드는 **같은 객체** — 재결합하지 않는다")
ok(_bst.log.a.band is _band1, "로그의 밴드도 덮이지 않는다 (그날의 제약 정본)")
ok(len(_bst.log.a.attempts) == 2, "회송 이력이 2건으로 쌓인다")
ok(_bst.log.a.attempts[1].trigger == "PRE_LOOP", "2회차 trigger 는 PRE_LOOP")
ok(
    _bst.log.a.attempts[0].scenario_ids != _bst.log.a.attempts[1].scenario_ids,
    "회차마다 달라지는 것은 시나리오다 — 밴드가 아니다",
)
ok(
    len(_bst.log.a.clip_results) == 2,
    "clip_results 는 **완료된(최신) 결과**만 — 중간 시도는 attempts 에",
)

# 밴드는 replies 만의 함수이므로 재결합해도 같은 값이 나온다 (불변 확인)
ok(
    combine_band(_REPLIES_STD).cap_total_kg == _band1.cap_total_kg,
    "재결합해도 값은 동일 — 그래서 한 번만 하는 것이 손실이 없다",
)

# 사이클 B 도 같은 규약
_r = run_day(SNAP, _A, _hooks_b(CASES_B["B_NORMAL"]["allocations"], 2_500.0))
ok(len(_r.cycle_a.log.b.attempts) >= 1, "사이클 B 도 attempts 를 남긴다")
ok(_r.cycle_b.outbound_band is not None, "OutboundBand 도 하루 한 번 결합")


section("\n[B-18] 갱신 PDF — 시나리오 독립성 범위 축소 (밴드만) · S2 출력 타입")

from app.orchestrator.contracts_core import CollectionPreference, LotConstraint

# 밴드를 채우는 검사가 매입안을 읽으면 여전히 FAIL
_band_chk = _finance_reply().checks[0]  # cap_amount_krw 를 채운다
f = check_scenario_independence("finance", _band_chk, ["current_cash", "sourcing_plan"])
ok(
    any(x.check_id == "E-SCENARIO-LEAK" for x in f),
    "밴드 검사(cap_amount_krw)가 sourcing_plan 을 읽으면 → E-SCENARIO-LEAK 유지",
)

# 자문 검사(밴드 필드 없음)가 매입안을 읽는 것은 이제 허용 — PDF 재무A/물류A
_advisory = CheckResult(
    "FRESHNESS_RISK",
    "inventory",
    "conditional",
    "soft",
    "중품 입고 시 신선도 확인 필요",
    (_ev("중품", "SRC-ADV", 1.0, "flag"),),
    severity="MEDIUM",
)
ok(
    not check_scenario_independence("inventory", _advisory, ["purchase_output", "sourcing_plan"]),
    "자문 검사(soft·밴드 없음)가 purchase_output 을 읽는 것은 허용 (v1.2.8 — PDF 물류A FRESHNESS_RISK)",
)

# 물류 B lot_constraints 타입
_lot = LotConstraint("LOT-001", "배추", 500.0, 5, "AVAILABLE")
ok(
    _lot.status == "AVAILABLE" and _lot.available_qty_kg == 500.0,
    "LotConstraint — 물류 B 출력, S3·Critic B 의 on_hand·신선도 재검산 입력",
)

# 재무 B collection_preferences 타입
_cp = CollectionPreference("DIRECT_B2B", "KIMCHI_FACTORY_001", 30, 1)
ok(_cp.liquidity_rank == 1, "CollectionPreference — 재무 B 출력, 순위 신호이지 배분 지시가 아니다")


section("\n[B-10] 계약 개정 요청 목록")

ok(
    len(CONTRACT_AMENDMENTS) == 6,
    f"계약 개정 필요 항목 {len(CONTRACT_AMENDMENTS)}건이 코드에 명시됨",
)
for name, why in CONTRACT_AMENDMENTS:
    print(f"     · {name}")


# ---------------------------------------------------------------------------
# pytest 브리지 — 위 모든 ok() 는 임포트 시 _results 에 쌓인다.
# 각 항목을 개별 pytest 케이스로 노출한다 (오케 코드와 연계 검증).

import pytest


@pytest.mark.parametrize(
    ("passed", "label"),
    _results,
    ids=[label for _, label in _results],
)
def test_critic_v0_4(passed: bool, label: str) -> None:
    assert passed, label


if __name__ == "__main__":  # 자체 러너: python tests/critic/test_critic_v0_4.py
    _ok = sum(1 for c, _ in _results if c)
    print("\n" + "=" * 62)
    print(f"  {_ok} / {len(_results)} 통과")
    print("=" * 62)
    if _ok != len(_results):
        print("\n실패 항목:")
        for _c, _label in _results:
            if not _c:
                print(f"  {FAIL} {_label}")
        raise SystemExit(1)
