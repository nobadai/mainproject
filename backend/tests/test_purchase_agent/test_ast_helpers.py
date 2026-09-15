"""`_ast_helpers` 자체를 잰다 — **필터를 지우면 여기가 운다.**

⚠️ 그 필터는 두 검사 파일에서 쓰이는데, **지금 통과하는 것이 필터 덕분인지 우연인지를
  아무도 안 재고 있었다** (2026-09-07).

    test_approved_commitments.py   ⑥ docstring 에 "approved_commitments" 가 있다
    test_coverage_by_label.py      ③ docstring·주석에 "공격" 이 셋 있다

두 검사 다 **대상 파일의 산문에 그 낱말이 이미 있는데** 통과한다. 필터가 걷어내기
때문인데, 그 사실을 잠근 검사가 없으면 필터를 지워도 아무도 모른다 — 지운 뒤에는
*"코드가 쓴다"* 와 *"설명에 적혀 있다"* 가 같아지고, 그게 `#335` 를 깨뜨린 그 상태다.

🔴 **여기서 쓰는 표본은 이 파일 안에서 만든다.** 실제 소스 파일에 기대면, 남이 그
  파일의 docstring 을 고치는 날 이 검사가 **이유 없이** 색을 바꾼다.
"""

import ast
from pathlib import Path

import pytest

from tests.test_purchase_agent._ast_helpers import (
    called_names,
    code_string_literals,
    prose_node_ids,
    references,
)

#: docstring · 주석 · 코드에 같은 낱말을 넣은 표본. **셋의 처지가 달라야 한다.**
#:
#: 🔴 **docstring 값이 낱말 «그 자체» 여야 한다.** ``references`` 는 완전 일치로 보므로,
#:   *"이 함수는 공격 을 설명한다"* 처럼 낱말이 문장에 섞여 있으면 **필터가 없어도**
#:   안 잡힌다 — 그런 표본으로는 필터가 일하는지 증명하지 못한다 (2026-09-07 정정:
#:   처음 쓴 표본이 그랬고, 변이를 넣어도 검사가 안 울었다).
SAMPLE = '''
"""공격"""

CONSTANT = 1
"""공격"""


def only_prose():
    """공격"""
    # 주석에도 공격 이 있다
    return 1


def actually_uses():
    return {"쓰는말": "실제코드"}
'''


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_docstring_에만_있으면_안_센다(sample: Path) -> None:
    """🔴 **이 판의 본문이다.** `#335` 가 이것을 안 해서 깨졌다.

    표본의 *"공격"* 은 모듈 docstring · 상수 아래 설명 · 함수 docstring · 주석에만
    있고 **코드에는 없다.** 넷 다 값이 낱말 그 자체라, 필터가 없으면 앞 셋이 잡힌다.
    """
    assert not references(sample, "공격")
    assert "공격" in sample.read_text(encoding="utf-8"), "표본 전제가 깨졌다 — 낱말이 없다"


def test_코드가_쓰면_센다(sample: Path) -> None:
    """반대 방향 — 걷어내기만 하면 아무것도 못 잡는 필터가 된다."""
    assert references(sample, "쓰는말")
    assert references(sample, "실제코드")


def test_주석은_애초에_보이지_않는다(sample: Path) -> None:
    """⚠️ 주석은 필터가 걷어내는 게 아니라 **``ast`` 가 노드로 만들지 않는다.**

    둘을 뭉쳐 *"주석도 막고 있다"* 고 적으면, 나중에 필터를 손볼 때 주석까지 지켜야
    한다고 오해하게 된다.
    """
    tree = ast.parse(SAMPLE)
    literals = [
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
    ]

    assert "# 주석에도 공격 이 있다" not in literals
    assert any("공격" in text for text in literals if isinstance(text, str)), (
        "docstring 은 Constant 로 남아 있어야 한다 — 그래야 걷어낼 것이 있다"
    )


def test_걷어내는_것이_실제로_docstring_이다(sample: Path) -> None:
    """★ 필터가 **무엇을** 걷어내는지 직접 확인한다.

    ``prose_node_ids`` 가 비면 위 검사들이 *"우연히"* 통과하게 된다 — 걷어낼 것이
    없는데 통과하는 것과, 걷어내서 통과하는 것은 다른 상태다.
    """
    tree = ast.parse(sample.read_text(encoding="utf-8"))

    assert prose_node_ids(tree), "걷어낸 docstring 이 하나도 없다"


def test_필터를_지우면_결과가_뒤집힌다(sample: Path) -> None:
    """🔴 **이 검사가 «지우면 운다» 를 보장한다.**

    필터를 안 쓴 판을 여기서 재현해, 같은 표본이 **반대 답**을 내는지 본다. 둘이 같은
    답을 내면 필터가 아무 일도 안 하는 것이고, 그때 이 검사가 운다.
    """
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    without_filter = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    assert "공격" in " ".join(without_filter), "필터 없이도 안 잡히면 표본이 무의미하다"
    assert not references(sample, "공격"), "필터가 걷어내지 못했다"


def test_부르는_이름만_센다(tmp_path: Path) -> None:
    """``called_names`` — import 만 하고 안 부르는 것은 세지 않는다.

    ★ *"그 함수를 실제로 부르는가"* 가 물음이라, ``from x import foo`` 만으로 통과하면
      검사가 뜻을 잃는다.
    """
    path = tmp_path / "calls.py"
    path.write_text(
        "from somewhere import imported_only, actually_called\n"
        "def run():\n"
        "    return actually_called(1)\n",
        encoding="utf-8",
    )

    names = called_names(path)
    assert "actually_called" in names
    assert "imported_only" not in names


def test_두_검사가_같은_헬퍼를_쓴다() -> None:
    """⚠️ **복제가 다시 생기면 여기가 운다** (2026-09-07).

    이 필터는 두 파일에 복제돼 있었고, 한쪽만 고치는 날 다른 쪽이 `#335` 와 같은
    방식으로 깨진다. 그래서 *"각자 ``ast.Expr`` 를 걷어내는 코드를 다시 쓰지
    않는다"* 를 잠근다.
    """
    here = Path(__file__).parent
    users = ["test_approved_commitments.py", "test_coverage_by_label.py"]

    for name in users:
        source = (here / name).read_text(encoding="utf-8")
        assert "_ast_helpers" in source, f"{name} 이 공용 헬퍼를 안 쓴다"
        assert "ast.Expr" not in source, f"{name} 에 필터가 다시 복제됐다"


def test_공용_헬퍼_말고는_ast_Expr_를_쓰지_않는다() -> None:
    """★ 위 검사의 넓은 판 — 이 디렉터리 전체에서 그 필터는 한 곳에만 있어야 한다."""
    here = Path(__file__).parent
    copies = [
        path.name
        for path in sorted(here.glob("*.py"))
        if path.name not in {"_ast_helpers.py", Path(__file__).name}
        and "ast.Expr" in path.read_text(encoding="utf-8")
    ]

    assert copies == [], f"필터가 복제된 파일: {copies}"


def test_이_파일이_실제로_구문을_읽는다(sample: Path) -> None:
    """⚠️ 표본이 파싱조차 안 되면 위 검사들이 **전부 빈손으로** 통과한다.

    ``code_string_literals`` 가 무엇이든 돌려주는지 한 번 확인해, 표본이 살아 있음을
    잠근다.
    """
    assert code_string_literals(sample), "표본에서 코드 문자열을 하나도 못 읽었다"
