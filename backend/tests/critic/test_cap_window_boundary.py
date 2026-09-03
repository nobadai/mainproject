"""창 밖 도착일은 **통과가 아니라 미검사**다.

2026-09-03 · #183 · 물류 IO Contract §6.

🔴 `check_occupancy_detailed` 는 **cap 이 있는 날짜만** 돌았다. 그래서 마지막 cap
날짜보다 뒤에 도착하는 회차는 **어느 비교에도 안 걸렸다** — 빈 problems 가 돌아와
*"검사했고 깨끗하다"* 로 읽혔다. 창 밖을 `0` 이 아니라 **무한대로** 읽은 셈이다.

물류가 정한 규약은 셋이다.

```text
키 존재 + 값 0    입고 가능량이 0 이다        → 누적 비교가 잡는다
창 안인데 키 없음  계산 누락 또는 미결
창 밖             계산 대상이 아니다          ← 0 도 무한대도 아니다
```

뒤의 둘을 가르려면 **창을 알아야 한다.** 창의 두 조각(`inbound_lead_days` ·
`cap_by_date_window_days`) 중 앞만 Critic 으로 갔고 뒤는 안 갔다.

⚠️ **지금 실 데이터에서는 안 걸린다 — 잠복이다.** 공격안 D=12·3회차면 도착일이
  D+10 이라 창(D+2~D+19) 안이다. 커버일수를 30 으로 올리면 D+22 라 넘는다.
  **왜 안 걸리는지가 우연**이라 이 파일이 필요하다.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import timedelta
from types import SimpleNamespace

from fixtures import AS_OF, ITEMS4, make_snapshot

from app.contracts.core import (
    Band,
    ClipResult,
    MinimalScenario,
    SourcingLot,
    SplitLeg,
)
from app.critic.critic_v0_4 import run_critic_v04
from app.logistics.tools import CAP_BY_DATE_WINDOW_DAYS, build_cap_window
from app.orchestrator.band import check_occupancy_detailed

LEAD = 2

#: 물류가 실제로 만드는 창. **여기서 상수를 다시 적지 않는다** — 다시 적으면
#: 물류가 창을 바꾼 날 이 파일만 옛 값을 들고 초록불로 남는다.
WINDOW = build_cap_window(SimpleNamespace(inbound_lead_days=LEAD), AS_OF)
WINDOW_END = WINDOW[-1]

SNAP = dc_replace(
    make_snapshot(),
    inbound_lead_days=LEAD,
    cap_by_date_window_days=CAP_BY_DATE_WINDOW_DAYS,
    confirmed_occupancy_by_date={},
)

D_FIRST = WINDOW[0]


def _scenario_out(eta) -> MinimalScenario:
    """도착일 하나짜리 매입안. cap 을 넉넉히 두어 **수량 위반은 안 나게** 한다."""
    qty = {"배추": 500.0}
    return MinimalScenario(
        scenario_id="SCN-WINDOW",
        strategy_type="quantity",
        stance="기준",
        qty_kg=qty,
        unit_price_krw_per_kg={"배추": 1_500.0},
        split_plan=(SplitLeg((eta - AS_OF).days - LEAD, dict(qty), eta),),
        sourcing_plan=(SourcingLot("배추", "상", 500.0, 1_500.0, ("SRC-1",)),),
        price_basis="AUCTION",
    )


def _clip_of(scn: MinimalScenario) -> ClipResult:
    return ClipResult(
        scenario_id=scn.scenario_id,
        qty_kg=dict(scn.qty_kg),
        clipped_qty_kg=dict(scn.qty_kg),
        clipped_split_plan=scn.split_plan,
        clipped_sourcing_plan=scn.sourcing_plan,
        clipped_amount_krw=750_000.0,
    )


def _run_critic(scn: MinimalScenario, clip: ClipResult, band: Band):
    return run_critic_v04(
        as_of=AS_OF,
        run_seq=1,
        clip=clip,
        band=band,
        snapshot=SNAP,
        scenario=scn,
        replies={},
        unit_price={"배추": 1_500.0},
        verify_ctx={},
        check_fns={},
        resolve_evidence=lambda rid, claim=None: None,
    )


def _band(cap_by_date: dict) -> Band:
    return Band({i: 0.0 for i in ITEMS4}, {i: 1e9 for i in ITEMS4}, 1e9, 1e12, {}, cap_by_date)


def _clip(*legs: tuple[int, float, object]) -> ClipResult:
    plan = tuple(SplitLeg(off, {"배추": qty}, eta) for off, qty, eta in legs)
    total = sum(qty for _, qty, _ in legs)
    return ClipResult(
        scenario_id="SCN-WINDOW",
        qty_kg={"배추": total},
        clipped_qty_kg={"배추": total},
        clipped_split_plan=plan,
    )


# ── ① 핵심 — 창 밖 도착은 조용히 통과하지 않는다 ────────────────────────────


def test_창_밖_도착은_통과가_아니라_미검사로_남는다():
    """🔴 **이 파일의 주장이다.**

    cap 은 창 첫날 하나뿐이고 2회차가 창 밖(`WINDOW_END + 1`)에 도착한다.
    전 코드는 `problems == ()` 만 돌려줘서 **두 회차 다 통과**로 읽혔다.
    """
    outside = WINDOW_END + timedelta(days=1)
    result = check_occupancy_detailed(
        _clip((0, 100.0, D_FIRST), (30, 9_999.0, outside)), _band({D_FIRST: 1_000.0}), SNAP
    )

    assert result.problems == (), "창 안 100kg 은 여유 1,000kg 안이라 위반이 아니다"
    assert result.skipped, "🔴 창 밖 9,999kg 이 아무 흔적 없이 사라졌다 — 통과로 읽힌다"
    note = " ".join(result.skipped)
    assert str(outside) in note, f"어느 날짜를 못 봤는지 안 적혔다: {result.skipped}"
    assert "창" in note and "밖" in note, f"창 밖이라고 안 적혔다: {result.skipped}"


def test_창_안_도착만_있으면_아무것도_안_적는다():
    """대조군. 이것이 없으면 위 검사가 **항상 skipped 를 내는 코드**로도 통과한다."""
    result = check_occupancy_detailed(
        _clip((0, 100.0, D_FIRST)), _band({D_FIRST: 1_000.0}), SNAP
    )

    assert result.ran
    assert result.skipped == (), f"검사가 시끄러워졌다: {result.skipped}"


# ── ② 경계 하루 — build_cap_window 와 대조해 잠근다 ─────────────────────────


def test_창의_마지막_날은_창_안이다():
    """⚠️ **off-by-one 이면 경계 하루가 통째로 갈래를 바꾼다** (2026-09-03 매입 조언).

    `build_cap_window` 의 마지막 offset 은 `window_days - 1` 이다. `-1` 을 빠뜨리면
    창이 하루 길어져, 진짜 창 밖 도착 하나가 *"창 안 누락"* 으로 읽힌다.
    """
    assert WINDOW_END == AS_OF + timedelta(days=LEAD + CAP_BY_DATE_WINDOW_DAYS - 1), (
        "이 파일이 가정한 창 끝이 물류 build_cap_window 와 다르다"
    )

    result = check_occupancy_detailed(
        _clip((0, 500.0, WINDOW_END)), _band({D_FIRST: 1_000.0}), SNAP
    )

    note = " ".join(result.skipped)
    assert "키 없음" in note, f"창의 마지막 날을 창 밖으로 읽었다: {result.skipped}"
    assert "밖" not in note, f"창의 마지막 날을 창 밖으로 읽었다: {result.skipped}"


def test_창_바로_다음날은_창_밖이다():
    """위 검사의 짝. 둘이 붙어 있어야 경계가 잠긴다."""
    result = check_occupancy_detailed(
        _clip((0, 500.0, WINDOW_END + timedelta(days=1))), _band({D_FIRST: 1_000.0}), SNAP
    )

    note = " ".join(result.skipped)
    assert "밖" in note, f"창 밖을 창 안 누락으로 읽었다: {result.skipped}"
    assert "누락" not in note, f"창 밖을 창 안 누락으로 읽었다: {result.skipped}"


# ── ③ 창을 모르면 **모른다고** 적는다 ───────────────────────────────────────


def test_창_길이가_없으면_창_밖인지_키_누락인지_못_가린다고_적는다():
    """🔴 **모르는 것을 아는 척하지 않는다.**

    물류가 창 길이를 안 실어 보냈거나 마스터가 안 날랐으면, 마지막 cap 날짜 뒤의
    도착은 창 밖일 수도 계산 누락일 수도 있다. 한쪽으로 단정하면 사람이 **없는
    문제를 찾거나 있는 문제를 놓친다.**
    """
    snap = dc_replace(SNAP, cap_by_date_window_days=None)
    result = check_occupancy_detailed(
        _clip((0, 500.0, WINDOW_END)), _band({D_FIRST: 1_000.0}), snap
    )

    note = " ".join(result.skipped)
    assert "cap_by_date_window_days" in note, f"무엇이 없어서 못 가리는지 안 적혔다: {note}"
    assert "못 가린다" in note, f"단정해 버렸다: {note}"


def test_리드타임이_없으면_창을_짓지_않는다():
    """N4 미결이면 창의 시작을 모른다. 0 으로 대체하지 않는다 (§1.2-10)."""
    snap = dc_replace(SNAP, inbound_lead_days=None)
    result = check_occupancy_detailed(
        _clip((0, 500.0, WINDOW_END)), _band({D_FIRST: 1_000.0}), snap
    )

    assert "못 가린다" in " ".join(result.skipped)


def test_리드타임이_float_로_와도_창을_만든다():
    """⚠️ 실 payload 의 `inbound_lead_days` 는 `2.0`(float) 이다 (2026-09-03 매입 실측).

    `isinstance(x, int)` 로 막으면 창을 **한 번도 못 만든다** — 그러면 위 갈래가
    영영 *"못 가린다"* 로만 가고, 이 판이 없던 것과 같아진다.
    """
    snap = dc_replace(SNAP, inbound_lead_days=2.0)
    result = check_occupancy_detailed(
        _clip((0, 500.0, WINDOW_END + timedelta(days=1))), _band({D_FIRST: 1_000.0}), snap
    )

    assert "밖" in " ".join(result.skipped), f"float lead 로 창을 못 만들었다: {result.skipped}"


# ── ④ 창 안의 키 누락은 누적 비교가 이미 흡수한다 ───────────────────────────


def test_창_안_중간_날짜의_키_누락은_다음_cap_날짜에서_세어진다():
    """**여기까지 skipped 로 만들면 시끄럽기만 하다.**

    `arrived` 가 `a <= d` 누적이라, 키 없는 날의 도착분은 그 다음 cap 날짜에서
    같이 세어진다. 통째로 새는 것은 **마지막 cap 날짜보다 뒤인 도착뿐**이라
    거기서만 가른다.
    """
    d_mid = D_FIRST + timedelta(days=3)
    d_last = D_FIRST + timedelta(days=6)
    result = check_occupancy_detailed(
        _clip((0, 800.0, d_mid)), _band({D_FIRST: 1_000.0, d_last: 500.0}), SNAP
    )

    assert result.skipped == (), f"창 안 누락을 두 번 적었다: {result.skipped}"
    assert result.problems, "키 없는 날의 도착분이 다음 cap 날짜에서 안 세어졌다"


# ── ⑤ 관통 — Critic 이 그 사실을 버리지 않는다 ──────────────────────────────


def test_Critic_이_창_밖_미검사를_결과에_낸다():
    """🔴 **band 가 적어도 Critic 이 버리면 소용없다.**

    Critic 은 `check_occupancy_by_date`(짧은 쪽)를 불렀다 — problems 만 돌려주는
    함수라 `skipped` 가 **호출부에서 통째로 사라졌다.** band 를 아무리 정직하게
    고쳐도 화면까지 안 온다.
    """
    outside = WINDOW_END + timedelta(days=1)
    scn = _scenario_out(outside)
    verdict = _run_critic(scn, _clip_of(scn), _band({D_FIRST: 1_000_000.0}))

    note = " ".join(verdict.skipped)
    assert "check_occupancy_by_date" in note, f"Critic 이 미검사를 버렸다: {verdict.skipped}"
    assert str(outside) in note, f"어느 날짜인지 안 왔다: {verdict.skipped}"


def test_못_본_회차가_있으면_L4_커버리지에_안_센다():
    """*"검사하지 못한 것을 검사했다고 말하지 않는다"* (설계서 §8).

    커버리지는 **실제로 돈 것만** 센다. 창 밖 회차를 남겨 두고 1 을 더하면
    `19/56` 같은 숫자가 조용히 부풀어 오른다.
    """
    inside = _scenario_out(WINDOW[3])
    outside = _scenario_out(WINDOW_END + timedelta(days=1))
    band = _band({D_FIRST: 1_000_000.0, WINDOW[3]: 1_000_000.0})

    ran_inside = _run_critic(inside, _clip_of(inside), band).coverage["L4"][0]
    ran_outside = _run_critic(outside, _clip_of(outside), band).coverage["L4"][0]

    assert ran_inside > ran_outside, (
        f"창 밖 회차를 남겨 두고도 같은 커버리지를 냈다: {ran_inside} vs {ran_outside}"
    )
