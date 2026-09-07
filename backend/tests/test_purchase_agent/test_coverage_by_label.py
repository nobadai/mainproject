"""①과 ③이 **같은 라벨 목록**을 쓴다 (`#340`).

①은 timing 축을 열지 말지 정하려고 *"그날 최대 D"* 로 추정 총량을 만들고, ③은 같은
목록으로 안을 만든다. 두 곳이 각자 판단하면 **축은 열렸는데 그 안이 없는 모순**이 난다.

실제로 그랬다::

    ① max(by_label) = 12          717.3 × 12 = 8,608kg   ← uncertain 인 날에도
    ③ uncertain 이면 공격 제외      717.3 ×  5 = 3,587kg   ← 실제로 만드는 안

🔴 **2.4배 차이였고, 실 경로 81조합이 전부 uncertain 이라 매일 그랬다.**

⚠️ 지금 임계(20,000)에서는 산출물이 안 바뀐다 — ``by_volume`` 이 어디서도 안 서기
  때문이다. 임계를 내리는 순간(`#308`) 갈린다: mock 15조합 중 2개가 닫힌다.
"""

import ast
from datetime import date
from pathlib import Path

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.classify_situation import compute_allowed_axes, coverage_by_label
from app.purchase_agent.state import build_initial_state

_NODES = Path(__file__).resolve().parents[2] / "app" / "purchase_agent" / "nodes"


def test_uncertain_이면_공격을_뺀다() -> None:
    """🔴 **이 판의 본문이다.** 구간이 넓은 날엔 선매입을 제안하지 않는다 (규칙 4)."""
    labels = coverage_by_label("uncertain", load_constraints())

    assert "공격" not in labels, labels
    assert list(labels) == ["보수", "기본"]


def test_stable_이면_셋_다_남긴다() -> None:
    """반대 방향 — 공격안이 있는 날까지 빼면 3안이 영영 안 나온다."""
    labels = coverage_by_label("stable", load_constraints())

    assert list(labels) == ["보수", "기본", "공격"]


def test_선언_순서를_보존한다() -> None:
    """⚠️ ``by_label`` 의 선언 순서가 곧 안이 나가는 순서다.

    ⑥ ``assign_axes`` 가 ``labels[-1]`` 로 마지막 안을 집으므로, 순서가 흔들리면
    **어느 안이 timing 을 가져가는지**가 조용히 바뀐다.
    """
    declared = list(load_constraints()["coverage_days"]["by_label"])

    for situation in ("stable", "uncertain"):
        kept = list(coverage_by_label(situation, load_constraints()))
        assert kept == [label for label in declared if label in kept], kept


def test_값은_선언에서_온다() -> None:
    """🔴 커버일수를 코드가 들고 있지 않다 (규칙 7·8).

    **선언을 바꾸면 결과가 따라 바뀌는지** 본다 — 사본을 덮어써서 잰다.
    """
    edited = load_constraints()
    edited["coverage_days"]["by_label"] = {"보수": 3, "기본": 7, "공격": 21}

    assert coverage_by_label("stable", edited) == {"보수": 3, "기본": 7, "공격": 21}
    assert coverage_by_label("uncertain", edited) == {"보수": 3, "기본": 7}


@pytest.mark.parametrize(
    ("situation", "expected_max"),
    [("stable", 12), ("uncertain", 5)],
)
def test_추정_총량이_그날_최대_D_를_쓴다(situation: str, expected_max: int) -> None:
    """🔴 **①이 실제로 그 목록으로 재는가.**

    ``compute_allowed_axes`` 를 직접 부르고, 임계를 추정 총량 바로 아래·위로 옮겨
    **경계가 어디인지** 확인한다 — 값 비교가 아니라 판정이 따라 바뀌는지를 본다.
    """
    state = build_initial_state("배추", date(2026, 9, 4))
    window = load_constraints()["demand"]["order_window_days"]
    daily = state["confirmed_orders"]["total_kg"] / window
    estimated = daily * expected_max

    just_under = load_constraints()
    just_under["triggers"]["split_entry_qty_kg"] = int(estimated) - 1
    assert "timing" in compute_allowed_axes(state, situation, just_under)

    just_over = load_constraints()
    just_over["triggers"]["split_entry_qty_kg"] = int(estimated) + 100
    assert "timing" not in compute_allowed_axes(state, situation, just_over), (
        f"{situation} 인데 최대 D 가 {expected_max} 가 아니다 — 추정 총량 {estimated:,.0f}kg"
    )


def test_두_노드가_같은_함수를_쓴다() -> None:
    """🔴 **두 곳이 같은 규칙을 쓰는지 잰다** (규칙 8) — 이 검사가 핵심이다.

    값을 대조하면 양쪽이 같은 조건을 **각자 하드코딩해도** 통과한다. 그건 *"한 곳에서
    읽는다"* 를 증명하지 못한다 — 오늘 여러 번 만난 유형이다.

    ★ 그래서 **③이 그 함수를 실제로 부르는지**를 구문으로 본다. ③이 자기 필터를 다시
      쓰면 여기가 운다.
    """
    source = (_NODES / "draft_plan.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "coverage_by_label" in calls, "③이 ①의 라벨 규칙을 안 부른다"

    # 🔴 ③ 안에 "공격" 을 직접 거르는 조건이 남아 있으면 두 곳이 다시 갈린 것이다.
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "공격" and id(node) not in prose
    ]
    assert not literals, "③에 «공격» 리터럴이 남았다 — 라벨 규칙이 두 곳에 있다"


def test_두_노드의_목록이_실제로_같다() -> None:
    """★ 위 검사가 *"부르는가"* 라면 이쪽은 *"결과가 같은가"* 다.

    ③을 통째로 돌려 나온 안의 라벨이 ①이 쓴 목록과 일치하는지 본다 — 그 사이에
    누가 라벨을 더하거나 빼면 여기가 운다.
    """
    from app.purchase_agent.nodes.draft_plan import draft_plan

    for as_of, expected in ((date(2026, 8, 21), 3), (date(2026, 9, 4), 2)):
        state = build_initial_state("배추", as_of)
        state["situation"] = "stable" if expected == 3 else "uncertain"
        drafts = draft_plan(state)["base_plan"]["drafts"]

        labels_from_draft = [row["label"] for row in drafts]
        labels_from_axes = list(coverage_by_label(state["situation"], load_constraints()))
        assert labels_from_draft == labels_from_axes, (
            f"{as_of}: {labels_from_draft} vs {labels_from_axes}"
        )
