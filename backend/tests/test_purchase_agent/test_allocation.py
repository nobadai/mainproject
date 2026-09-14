"""``allocation.py`` — 순수 함수층의 **경계**를 잠근다.

🔴 이 파일이 재는 것은 산식이 아니라 **의존 방향**이다. 산식 자체는 옮기기 전부터
``test_split.py``·``test_adapter.py`` 가 재고 있었고, 이 판은 그 함수들을 **한 글자도
안 고치고 자리만 옮겼다**.

★ 왜 경계를 검사로 잠그나 — 노드 사이 의존이 한쪽이라(④ → ⑥ → ⑦) ``allocation`` 이
노드를 되짚어 import 하는 순간 **순환**이 된다. 사람이 무심코 한 줄 더하면 그날
``graph.py`` 가 import 단계에서 죽고, 원인은 여기가 아니라 부르는 쪽에서 터진다.
"""

import ast
from pathlib import Path

from app.purchase_agent import allocation
from app.purchase_agent.nodes import package_scenarios, split_plan

SOURCE = Path(allocation.__file__)


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def test_순수층은_노드를_import_하지_않는다() -> None:
    """🔴 **이 한 줄이 순환을 막는다.**

    ``split_plan`` 이 이 모듈을 쓰고, ``package_scenarios``·``self_check`` 도 쓴다.
    여기서 그중 아무나 되짚으면 import 가 고리를 이룬다.
    """
    노드를_가리키는_것 = [m for m in _imported_modules(SOURCE) if ".nodes" in m]
    assert 노드를_가리키는_것 == []


def test_순수층은_아무것도_import_하지_않는다() -> None:
    """지금은 **완전히 비어 있다** — 설정도 스키마도 안 읽는다.

    ⚠️ 이 검사는 «영원히 0» 을 요구하는 것이 아니다. 뒤에 선언(``constraints.yaml``)을
    읽는 함수가 들어오면 이 줄을 **의도적으로** 고치게 되고, 그때 «무엇이 늘었나» 가
    리뷰에 뜬다. 조용히 늘어나는 것을 막는 자리다.
    """
    assert _imported_modules(SOURCE) == []


def test_옮긴_이름이_원래_자리에서도_그대로_불린다() -> None:
    """검사와 남의 코드가 쓰던 경로가 **그대로 산다**.

    ``tests/test_split.py`` 는 ``nodes.split_plan`` 에서 ``equal_ratios`` 를,
    ``nodes.package_scenarios`` 에서 ``split_offsets`` 를 가져온다. 옮기면서 그 경로가
    끊기면 「옮기기만 했다」가 아니게 된다.
    """
    assert split_plan.equal_ratios is allocation.equal_ratios
    assert package_scenarios.split_offsets is allocation.split_offsets
    assert package_scenarios.split_quantities is allocation.split_quantities


def test_마지막_회차가_잔량을_흡수해_총량이_안_흔들린다() -> None:
    """사중 일치의 한 축 — ``Σ 회차수량 == total_qty_kg``.

    ★ 반올림을 마지막에 몰지 않으면 ⑦이 컷한다. 옮긴 뒤에도 그대로인지 본다.
    """
    for total in (0, 1, 29, 1_435, 4_286, 12_345):
        for rounds in (1, 2, 3):
            비율 = [{"ratio": r} for r in allocation.equal_ratios(rounds)]
            수량 = allocation.split_quantities(total, 비율)
            assert sum(수량) == total
            assert len(수량) == rounds


def test_균등_비율의_합이_정확히_1이다() -> None:
    """⑥의 합계 검사가 ``1e-9`` 라 부동소수점 잔차가 남으면 걸린다."""
    for rounds in range(1, 8):
        assert abs(sum(allocation.equal_ratios(rounds)) - 1.0) <= 1e-9


def test_첫_회차는_늘_as_of_다() -> None:
    """IO명세 §2 *"seq 1의 date = as_of"* — 오프셋 0 이 그 뜻이다."""
    for coverage in range(1, 19):
        for rounds in (1, 2, 3):
            assert allocation.split_offsets(coverage, rounds)[0] == 0
