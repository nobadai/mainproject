"""마스터 검증 Tool — M-16 실행 계획 온전성 · timing 게이트.

★ 픽스처는 **매입 실측 스키마**를 따른다 (2026-08-27 매입 §4 답변).
  `allowed_axes` 는 제안 최상위, `split_plan` 은 시나리오 안이다. 초안은 둘 다
  시나리오에서 찾다가 **검사가 영영 발화하지 않았다** — 그런 검사는 skipped 로도
  안 잡히고 "봤는데 문제없음"으로 읽힌다.
"""

from __future__ import annotations

from datetime import date

from app.master.plan import ExecutionPlan
from app.master.verifier import MasterVerifier

AS_OF = date(2026, 9, 4)


def plan() -> ExecutionPlan:
    return ExecutionPlan(request_id="REQ-1", as_of=AS_OF)


QTY_PER_ROUND = 1_000
UNIT_PRICE = 1_650
MAX_PRICE = 1_750


def scenario(rounds: int = 1, **over) -> dict:
    """★ 항등식이 성립하는 픽스처.

    처음엔 `total_qty_kg` 를 상수로 박아 두고 `split_plan` 만 늘렸다가
    **새로 붙인 L-IDENTITY-QTY 가 그 불일치를 바로 잡아냈다.** 검사가 도는 증거이기도
    하고, 픽스처가 계약을 어기면 안 된다는 뜻이기도 하다.
    """
    qty = QTY_PER_ROUND * rounds
    base = {
        "label": "보수",
        "strategy_type": "quantity",
        "total_qty_kg": qty,
        "total_amount_krw": qty * UNIT_PRICE,
        "max_price": MAX_PRICE,
        # 매입 스키마상 split_plan 은 **최소 1** — 미적용이 빈 배열이 아니다
        "split_plan": [
            {"seq": i + 1, "date": f"2026-09-{5 + i:02d}", "qty_kg": QTY_PER_ROUND}
            for i in range(rounds)
        ],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": qty, "grade_unit_price": UNIT_PRICE}
        ],
    }
    base.update(over)
    return base


def payment_rows(rounds: int, **over) -> list[dict]:
    """매입 §3.2 형태. `purchase_date` 는 `split_plan` 과 seq 대응한다."""
    rows = [
        {
            "seq": i + 1,
            "purchase_date": f"2026-09-{5 + i:02d}",
            "payment_date": f"2026-09-{12 + i:02d}",  # +7 (N5)
            "qty_kg": QTY_PER_ROUND,
            "amount_krw": QTY_PER_ROUND * UNIT_PRICE,
            "amount_max_krw": QTY_PER_ROUND * MAX_PRICE,
            "basis": "as_of_unit_price",
        }
        for i in range(rounds)
    ]
    for row in rows:
        row.update(over)
    return rows


FINANCE = {"finance": {"purchase_payment_days": 7}}


def proposal(axes: list[str] | None = None, rounds: int = 1) -> dict:
    out: dict = {"situation": "uncertain", "confidence": "medium", "scenarios": [scenario(rounds)]}
    if axes is not None:
        out["allowed_axes"] = axes
    return out


def run(p: dict, constraints: dict | None = None):
    return MasterVerifier()(p, constraints if constraints is not None else FINANCE, {}, plan())


def gate(result) -> list[str]:
    return [f for f in result.findings if f.startswith("L-TIMING-GATE")]


# ---------------------------------------------------------------------------
# ③ timing 게이트
# ---------------------------------------------------------------------------


def test_timing_닫혔는데_분할이면_발화한다():
    assert gate(run(proposal(["quantity"], rounds=3)))


def test_분할이_1회차면_통과한다():
    """`split_plan` 은 최소 1이라 **경계가 `> 1`** 이다. 1을 잡으면 전부 걸린다."""
    assert not gate(run(proposal(["quantity"], rounds=1)))


def test_timing_이_열려_있으면_분할은_정상이다():
    assert not gate(run(proposal(["quantity", "timing"], rounds=3)))


def test_allowed_axes_가_없으면_통과가_아니라_미검사다():
    """★ 못 본 것을 통과로 치지 않는다 (§3.7.6)."""
    result = run(proposal(None, rounds=3))
    assert not gate(result)
    assert any(s.startswith("L-TIMING-GATE") for s in result.skipped)


def test_split_plan_이_없으면_그_시나리오만_미검사다():
    p = proposal(["quantity"], rounds=3)
    p["scenarios"].append({"label": "기본"})  # split_plan 없음
    result = run(p)
    assert gate(result)  # 0번은 여전히 걸린다
    assert any("scenarios[1]" in s for s in result.skipped)


# ---------------------------------------------------------------------------
# ④ 실행 계획 온전성 (M-16)
# ---------------------------------------------------------------------------


def test_조언자를_안_부르면_concern_이다():
    """★ finding 이 아니다 — 매입을 다시 불러도 마스터가 물류를 안 부른 사실은 안 바뀐다."""
    result = run(proposal(["quantity", "timing"]))
    assert not result.findings
    assert any("M16-AGENT-MISSING" in c for c in result.concerns)


def test_커버리지를_감추지_않는다():
    """`findings: []` 를 "56검사 통과"로 읽지 않게 못 본 것을 함께 낸다."""
    assert any("56검사" in s for s in run(proposal(["quantity", "timing"])).skipped)


# ---------------------------------------------------------------------------
# ② 시나리오 층 항등식 (매입 §4-2)
# ---------------------------------------------------------------------------


def ident(result) -> list[str]:
    return [f for f in result.findings if f.startswith("L-IDENTITY")]


def test_정합한_시나리오는_통과한다():
    assert not ident(run(proposal(["quantity", "timing"], rounds=2)))


def test_분할_수량_합이_총량과_다르면_잡는다():
    p = proposal(["quantity", "timing"], rounds=2)
    p["scenarios"][0]["split_plan"][0]["qty_kg"] += 1
    assert any("split_plan 수량 합" in f for f in ident(run(p)))


def test_등급_수량_합이_총량과_다르면_잡는다():
    p = proposal(["quantity", "timing"], rounds=2)
    p["scenarios"][0]["sourcing_plan"][0]["qty_kg"] -= 500
    assert any("sourcing_plan 수량 합" in f for f in ident(run(p)))


def test_총액이_등급_가중합과_다르면_잡는다():
    """★ 매입도 같은 항등식을 강제한다고 했지만, 믿는 것과 확인하는 것은 다르다."""
    p = proposal(["quantity", "timing"], rounds=2)
    p["scenarios"][0]["total_amount_krw"] += 10_000
    assert any("L-IDENTITY-AMOUNT" in f for f in ident(run(p)))


def test_셀_수_없으면_통과가_아니라_미검사다():
    p = proposal(["quantity", "timing"], rounds=2)
    del p["scenarios"][0]["sourcing_plan"]
    result = run(p)
    assert any("L-IDENTITY" in s for s in result.skipped)


# ---------------------------------------------------------------------------
# ③ 분할 지급 일정 (매입 §3.2 · 재무 확정)
# ---------------------------------------------------------------------------


def sched(result) -> list[str]:
    return [f for f in result.findings if f.startswith("L-PAYSCHED")]


def with_schedule(rounds: int = 2, **over):
    p = proposal(["quantity", "timing"], rounds=rounds)
    p["scenarios"][0]["payment_schedule"] = payment_rows(rounds, **over)
    return p


def test_정합한_지급일정은_통과한다():
    assert not sched(run(with_schedule()))


def test_회차_수량_합이_다르면_잡는다():
    p = with_schedule()
    p["scenarios"][0]["payment_schedule"][0]["qty_kg"] += 100
    assert any("L-PAYSCHED-QTY" in f for f in sched(run(p)))


def test_회차_금액_합이_총액과_다르면_잡는다():
    p = with_schedule()
    p["scenarios"][0]["payment_schedule"][0]["amount_krw"] += 1
    assert any("L-PAYSCHED-AMOUNT" in f for f in sched(run(p)))


def test_매입일이_split_plan_과_어긋나면_잡는다():
    p = with_schedule()
    p["scenarios"][0]["payment_schedule"][1]["purchase_date"] = "2026-09-09"
    assert any("L-PAYSCHED-DATE" in f for f in sched(run(p)))


def test_지급_간격이_N5_와_다르면_잡는다():
    p = with_schedule()
    p["scenarios"][0]["payment_schedule"][0]["payment_date"] = "2026-09-10"  # +5
    assert any("L-PAYSCHED-N5" in f for f in sched(run(p)))


def test_N5_를_상수로_박지_않는다():
    """★ 7 을 박아 두면 정책이 바뀌어도 검사가 옛 값으로 통과시킨다.

    재무가 N5 를 5 로 바꾸면 +7 이 **오류**가 되어야 한다.
    """
    p = with_schedule()
    result = run(p, {"finance": {"purchase_payment_days": 5}})
    assert any("L-PAYSCHED-N5" in f for f in sched(result))


def test_재무_N5_가_없으면_미검사다():
    result = run(with_schedule(), {})
    assert not any("L-PAYSCHED-N5" in f for f in sched(result))
    assert any("L-PAYSCHED-N5" in s for s in result.skipped)


def test_H1_확정_지급일은_N5_검사를_건너뛴다():
    """재무: "H1 에 확정 payment_date 가 있으면 그 값이 authoritative"."""
    p = with_schedule(payment_date="2026-09-30", h1_payment_date=True)
    result = run(p)
    assert not any("L-PAYSCHED-N5" in f for f in sched(result))
    assert any("H1 확정 지급일" in s for s in result.skipped)


def test_상한이_수량x상한가와_다르면_잡는다():
    """재무가 STRESS Cashflow 로 쓰는 값이다."""
    p = with_schedule()
    p["scenarios"][0]["payment_schedule"][0]["amount_max_krw"] += 1000
    assert any("L-PAYSCHED-MAX" in f for f in sched(run(p)))


def test_분할이_아닌데_지급일정이_있으면_잡는다():
    """⑤ 분할이 아닌 시나리오에는 이 키가 없다 (매입 §3.3)."""
    p = proposal(["quantity"], rounds=1)
    p["scenarios"][0]["payment_schedule"] = payment_rows(1)
    assert any("L-PAYSCHED-UNEXPECTED" in f for f in sched(run(p)))


def test_분할인데_지급일정이_없으면_미검사다():
    """아직 안 실려 오는 신설 필드다. **통과로 치지 않는다.**"""
    result = run(proposal(["quantity", "timing"], rounds=2))
    assert not sched(result)
    assert any("payment_schedule 가 없어 미검사" in s for s in result.skipped)
