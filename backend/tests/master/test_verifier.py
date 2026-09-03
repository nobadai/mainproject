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


# ---------------------------------------------------------------------------
# ⑤ 실어 준 값을 미결이라 답하는가
# ---------------------------------------------------------------------------


INVENTORY = {"inventory": {"inbound_lead_days": 2.0, "cap_by_date": {"2026-01-02": 5000}}}


def supplied(result) -> list[str]:
    return [c for c in result.concerns if c.startswith("SUPPLIED-BUT-UNRESOLVED")]


def with_risk(text: str) -> dict:
    p = proposal()
    p["scenarios"][0]["risks"] = [text]
    return p


def test_실어_준_값을_미결이라_답하면_잡는다():
    """🔴 **조정자만 볼 수 있는 종류다.**

    물류는 자기가 보낸 것을 알고 매입은 자기가 못 읽은 것을 아는데, **둘을 나란히
    놓는 것은 마스터뿐**이다. 실측(2026-08-29)에서 `inbound_lead_days` 가 봉투에
    `2.0` 으로 실려 있는데 매입이 *"미확정"* 으로 도착일 계산을 보류했다.
    """
    result = run(
        with_risk("입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 …"),
        INVENTORY,
    )

    assert len(supplied(result)) == 1
    assert "inbound_lead_days" in supplied(result)[0]
    # 재호출로 안 고쳐진다 — 배선을 고쳐야 하는 일이라 findings 가 아니다
    assert not any("SUPPLIED" in f for f in result.findings)


def test_같은_원인의_파생은_두_번_보고하지_않는다():
    """`cap_by_date` 도 봉투에 있지만 **보류 사유는 뒤의 키 하나**다.

    처음엔 문장 어디에 있든 잡아 둘 다 보고했다 — 같은 원인을 두 번 낸 셈이었다.
    """
    result = run(
        with_risk("cap_by_date 검사는 inbound_lead_days(N4) 미확정으로 보류"),
        INVENTORY,
    )

    assert len(supplied(result)) == 1
    assert "inbound_lead_days" in supplied(result)[0]


def test_봉투에_없는_이름은_잡지_않는다():
    """이름이 다른 불일치는 **여기서 못 잡는다.**

    물류 `item_storage_policies[].operational_limit_days` 와 매입
    `lots[].shelf_life_days` 가 그 경우다. 별칭 표를 두면 어긋날 자리가 하나 더
    생기고, 그건 8/29 에 걷어낸 층이다 — **이름 합의는 팀이 할 일이다.**
    """
    result = run(with_risk("품목 보관한계(shelf_life_days)를 아무 로트도 싣지 않았다"), INVENTORY)

    assert supplied(result) == []


def test_정상적인_위험_고지는_잡지_않는다():
    """넓게 잡으면 **부서가 성실히 남긴 위험까지** 배선 문제로 읽힌다."""
    result = run(
        with_risk("기존 로트 잔여신선도 10일 — 신규 매입분이 밀어내지 않는지 확인"), INVENTORY
    )

    assert supplied(result) == []


def test_짧은_키가_긴_키_안에_걸리지_않는다():
    """🔴 **이름 전체로만 건다.** 실측 2026-08-31 (피마늘 관통 · 당시 4품목) — 중첩 한 겹을 보게
    되면서 `item` 이 `supplied` 에 들어왔고, 그 순간 매입 문장 **하나가 지적 두 줄**이
    됐다. 화면에 같은 사유가 두 번 떴다.

    ```text
    "item_storage_policies 반영했으나 결론 미결"
      item_storage_policies → 울린다 (맞다)
      item                  → 그 이름 안에 걸려 또 울린다 (오탐)
    ```

    ★ **끼어든 키 규칙으로는 못 막았다.** `item` 의 match 가 이름 중간에서 끝나므로
      뒤에 남는 것이 `_storage_policies …` 라 온전한 이름이 없다.
    """
    nested = {
        "inventory": {
            "item": "배추",
            "item_storage_policies": [{"item": "배추", "operational_limit_days": 7}],
        }
    }

    result = run(
        with_risk("등급 배분 보류 — item_storage_policies 반영했으나 결론 미결"), nested
    )

    assert len(supplied(result)) == 1, f"오탐 포함: {supplied(result)}"
    assert "item_storage_policies" in supplied(result)[0]


def test_한글_조사가_붙어도_잡는다():
    """`operational_limit_days는` — 조사는 식별자 글자가 아니라 경계로 친다."""
    nested = {"inventory": {"item_storage_policies": [{"operational_limit_days": 7}]}}

    result = run(with_risk("operational_limit_days는 미확정이라 보류"), nested)

    assert len(supplied(result)) == 1
    assert "operational_limit_days" in supplied(result)[0]
