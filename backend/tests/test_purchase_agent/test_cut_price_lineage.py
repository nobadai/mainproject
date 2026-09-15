"""컷 기준과 재무 STRESS 상한이 **다른 경로**를 탄다 — 지금은 값이 같다 (2026-09-08).

::

    max_price       재무 STRESS 로 나간다 (amount_max_krw = qty × 이것)
                    🔴 재무·마스터가 그 등식을 검사한다
                       finance/capabilities/scenario.py  amount_max_krw 등식
                       master/verifier.py                검사 이름 L-PAYSCHED-MAX
    cut_unit_price  우리 컷 기준 (self_check.check_max_price)

🔴 **왜 갈랐나** — 하나였을 때 밴드가 좁아지면 컷이 엄격해지고 재무 STRESS 는
느슨해졌다. **방향이 반대인데 값이 하나였다.**

⚠️ **값이 같으므로 값 비교로는 못 잰다** (규칙 8). 그래서 셋으로 나눠 잰다.

.. code-block:: text

    주입   컷 기준만 움직여 STRESS 가 안 따라오는지 본다
    구문   어느 함수가 어느 이름을 읽는지 본다 (파일 단위로는 둘 다 참이다)
    계약   amount_max_krw == qty × max_price 가 여전히 성립하는지 본다

★ 이 판은 **"고정"이 아니라 "갈라놓기"** 다. 밴드가 바뀌면 STRESS 는 여전히 따라간다.

🔴 ~~`09-17` 에 밴드가 바뀐다~~ — **낡았다** (ML 회신 2026-09-10). 밴드 교체는 `09-03`
에 끝났고 `09-17` 은 «그림자 기록 2주가 차는 날» 이다. 둘이 갈라지는 계기는 날짜가
아니라 **우리가 `compute_cut_unit_price` 를 바꾸는 것**이다.

🟢 **재무가 답했다** (2026-09-08) — *"별도 새 기준이 오기 전까지는 **현재값을 고정해서
진행하셔도 됩니다**"*. 🔴 다만 *"현재값"* 이 **값인지 산식인지**가 갈린다.

.. code-block:: text

    값을 박는다   품목 3 × 커버 2/5/12 = 여섯~아홉 개를 손으로 적어야 하고,
                  실측상 매일 최대 11% 움직인다
    산식을 둔다   지금 상태 — 밴드가 바뀌면 따라간다

⚠️ **이 판은 산식을 둔 쪽이다. 되물었고 답이 왔다** (2026-09-10 · q80 · 커버 2일은
`LT2` — 배추 9.6% · 무 15.0% · 양파 8.8%). 🟡 아직 안 바꿨다.
"""

import ast
import inspect
from datetime import date
from pathlib import Path

import pytest

from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.nodes import package_scenarios
from app.purchase_agent.nodes.package_scenarios import build_payment_schedule
from app.purchase_agent.nodes.self_check import check_max_price
from tests.test_purchase_agent._ast_helpers import references, references_in

ANCHOR = date(2025, 12, 31)
ITEMS = ("배추", "무", "양파")

_PACKAGE = Path(package_scenarios.__file__)
_SELF_CHECK = Path(package_scenarios.__file__).with_name("self_check.py")


# ── 지금 상태 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("item", ITEMS)
def test_both_ceilings_are_present_and_equal_today(item: str) -> None:
    """둘 다 실리고, **지금은 같은 값이다.**

    ⚠️ 이 검사는 *"같다"* 를 지키려는 것이 아니라 **언제 갈렸는지 알려는 것**이다.
    ML 여유율 표가 와서 컷 산식이 바뀌면 여기가 먼저 운다 — 그때 이 검사를
    *"다르다"* 로 뒤집으면 된다.
    """
    proposal = run_purchase_agent(item, ANCHOR)
    assert proposal["scenarios"], "안이 0개면 아무것도 못 잰다"
    for scenario in proposal["scenarios"]:
        assert scenario["cut_unit_price"] is not None, "컷 기준이 비면 ⑦이 컷을 못 한다"
        assert scenario["cut_unit_price"] == scenario["max_price"]


# ── ① 주입 — 컷만 움직인다 ────────────────────────────────────────────────


def test_moving_the_cut_ceiling_never_moves_the_stress_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """컷 기준을 통째로 갈아끼워도 ``max_price`` 는 **한 원도 안 움직인다.**

    ★ **아주 큰 값으로 민다.** 낮추면 안이 전부 컷되어 비교할 대상이 사라진다 —
      재려는 것은 *"컷이 걸리는가"* 가 아니라 *"STRESS 가 따라오는가"* 다.

    🔴 이것이 이 판의 본체다. 두 값이 같으므로 **값을 비교해서는 갈렸는지 알 수 없고**,
      한쪽만 흔들어 다른 쪽이 가만히 있는지를 봐야 한다 (규칙 8).
    """
    before = {item: run_purchase_agent(item, ANCHOR) for item in ITEMS}

    monkeypatch.setattr(package_scenarios, "compute_cut_unit_price", lambda *_: 9_999_999)
    after = {item: run_purchase_agent(item, ANCHOR) for item in ITEMS}

    for item in ITEMS:
        old, new = before[item]["scenarios"], after[item]["scenarios"]
        assert len(old) == len(new), "컷 기준을 올렸는데 안 개수가 바뀌었다"
        for was, now in zip(old, new, strict=True):
            assert now["cut_unit_price"] == 9_999_999, "주입이 안 먹었다 — 검사가 헛돈다"
            assert now["max_price"] == was["max_price"]
            assert now["total_amount_krw"] == was["total_amount_krw"]


def test_the_cut_still_fires_when_only_the_cut_ceiling_is_lowered() -> None:
    """반대 방향 — **컷 기준만** 낮추면 컷이 걸리고, ``max_price`` 만 낮추면 안 걸린다.

    ⚠️ 앞 검사만으로는 부족하다. 그것은 *"STRESS 가 안 따라온다"* 만 보고,
      **컷이 실제로 새 값을 본다**는 것은 안 본다. 둘 다 있어야 경로가 하나로
      되돌아가는 변이가 잡힌다.
    """
    line = {"market": "가락", "grade": "특", "qty_kg": 100, "grade_unit_price": 2_000}

    only_cut_low = {"sourcing_plan": [line], "cut_unit_price": 1_900, "max_price": 9_999}
    assert check_max_price(only_cut_low) is not None

    only_stress_low = {"sourcing_plan": [line], "cut_unit_price": 9_999, "max_price": 1_900}
    assert check_max_price(only_stress_low) is None, "⑦이 STRESS 상한을 보고 있다"


def test_a_missing_cut_ceiling_is_reported_not_passed() -> None:
    """컷 기준이 없으면 **통과가 아니라 컷 사유다** (규칙 3).

    스키마가 ``None`` 을 허용하는 것은 남의 픽스처 때문이고
    (``schemas.Scenario.cut_unit_price``), 없는 것을 통과로 읽으면 **상한 검사가
    조용히 꺼진다.** 그때는 아무도 모른다 — 에러가 안 나고 안이 더 많이 살아남는다.
    """
    line = {"market": "가락", "grade": "특", "qty_kg": 100, "grade_unit_price": 2_000}
    assert check_max_price({"sourcing_plan": [line], "max_price": 1_900}) is not None


# ── ② 구문 — 어느 함수가 어느 이름을 읽나 ─────────────────────────────────


def test_the_file_alone_cannot_tell_the_two_apart() -> None:
    """⚠️ **파일 단위로는 둘 다 참이다** — 그래서 함수 단위로 재야 한다.

    이 검사는 아래 두 검사가 *"왜 함수 단위인가"* 를 남기려고 있다. 파일 단위
    ``references`` 로 물으면 ``self_check.py`` 는 두 이름을 다 쓴다 — 컷에서 하나,
    STRESS 검산에서 다른 하나.
    """
    assert references(_SELF_CHECK, "max_price")
    assert references(_SELF_CHECK, "cut_unit_price")


def test_the_cut_reads_only_the_cut_ceiling() -> None:
    """``check_max_price`` 는 ``cut_unit_price`` 만 읽는다."""
    assert references_in(_SELF_CHECK, "check_max_price", "cut_unit_price")
    assert not references_in(_SELF_CHECK, "check_max_price", "max_price")


def test_the_payment_schedule_cannot_see_the_cut_ceiling() -> None:
    """지급 일정은 컷 기준을 **받을 자리가 없다.**

    ★ 구문보다 시그니처가 강하다 — 인자에 없으면 실수로도 못 읽는다.

    ⚠️ **이것만으로는 부족하다.** 이 함수는 넘겨받을 뿐이고 *"무엇을 넘기는가"* 는
      호출부가 정한다 — 아래 ``test_the_payment_schedule_is_fed_the_stress_ceiling``
      이 그 자리를 잠근다. 실제로 변이(호출 인자를 ``cut_unit_price`` 로 교체)가
      **이 검사를 그대로 통과했다** (2026-09-08).
    """
    params = inspect.signature(build_payment_schedule).parameters
    assert "max_price" in params
    assert "cut_unit_price" not in params
    assert not references_in(_PACKAGE, "build_payment_schedule", "cut_unit_price")


def test_the_payment_schedule_is_fed_the_stress_ceiling() -> None:
    """조립부가 지급 일정에 넘기는 것이 ``max_price`` **변수 그 자체**인가.

    🔴 **두 값이 같은 동안은 이 자리를 값으로 못 잰다.** 컷 기준을 넘겨도
      ``amount_max_krw`` 가 똑같이 나오기 때문이다 — 그러면 재무·마스터 검사도
      통과하고, 갈렸다고 믿은 채 **STRESS 가 컷 기준을 따라 움직이게 된다.**

    ★ 그래서 **호출 인자의 이름**을 본다. 값이 아니라 배선을 재는 자리다.
    """
    tree = ast.parse(_PACKAGE.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_payment_schedule_field"
    ]
    assert calls, "_payment_schedule_field 호출을 못 찾았다 — 이름이 바뀌었는가"
    for call in calls:
        ceiling = call.args[1]
        assert isinstance(ceiling, ast.Name), "상한을 변수로 넘겨야 이 검사가 읽는다"
        assert ceiling.id == "max_price", f"지급 일정에 {ceiling.id} 를 넘기고 있다"


# ── ③ 계약 — 재무·마스터가 검사하는 등식 ──────────────────────────────────


def test_the_stress_identity_still_holds() -> None:
    """``amount_max_krw == qty × max_price``.

    🔴 **우리만의 규칙이 아니다.** 재무(``capabilities/scenario.py`` 의 ``amount_max_krw``
    등식)와 마스터(``verifier.py`` 의 ``L-PAYSCHED-MAX``)가 같은 등식을 검사한다 —
    이 판이 그 등식을 건드리지
    않는 것이 *"남의 코드 0줄"* 의 근거다.
    """
    max_price = 1_800
    rounds = [
        {"seq": 1, "date": "2026-08-21", "qty_kg": 100, "amount_krw": 165_000},
        {"seq": 2, "date": "2026-08-24", "qty_kg": 200, "amount_krw": 330_000},
    ]
    schedule = build_payment_schedule(rounds, max_price, 7)
    assert schedule is not None
    for row in schedule:
        assert row["amount_max_krw"] == row["qty_kg"] * max_price
