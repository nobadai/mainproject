"""*"이 낱말을 코드가 쓰는가"* 를 구문으로 재는 헬퍼.

⚠️ **이 필터가 두 검사 파일에 복제돼 있었다** (2026-09-07).

``ast`` 는 docstring 을 ``Constant`` 로 파싱한다. 그래서 *"이 낱말을 코드가 쓰는가"* 를
재려면 **``Expr`` 자리에 홀로 선 ``Constant`` 를 걷어내야** 한다.

🔴 **어제 `#335` 가 그것을 안 해서 깨졌다** — `#332` docstring 의 *"approved_commitments
  도 안 온다"* 를 *"읽는다"* 로 셌다. 두 브랜치가 각각은 통과하는데 합치니 `dev` 가
  16분간 빨간불이었다.

★ **복제해 두면 한쪽만 고치는 날이 온다.** 그때 다른 쪽이 어제와 같은 방식으로 깨진다.

⚠️ **주석은 애초에 안 보인다.** ``ast`` 가 주석을 노드로 만들지 않기 때문이고, 그래서
  걷어낼 필요가 없다 — 걷어내는 것은 docstring 뿐이다. 둘을 같은 말로 뭉치면 *"주석도
  막고 있다"* 고 오해하게 되므로 갈라 적는다.
"""

import ast
from pathlib import Path


def prose_node_ids(tree: ast.AST) -> set[int]:
    """docstring 노드의 ``id`` 집합. **문(statement) 자리에 홀로 선 문자열**이 그것이다.

    ★ ``ast.get_docstring`` 을 안 쓰는 이유: 그것은 모듈·클래스·함수의 **첫** 문자열만
      돌려준다. 설명을 위해 중간에 둔 문자열 문(예: 상수 아래 붙인 설명)도 코드가 아니라
      산문이므로 같이 걷어내야 한다.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def code_string_literals(path: Path) -> list[str]:
    """이 파일에서 **코드가 실제로 쓰는** 문자열 리터럴 전부 (docstring 제외).

    돌려주는 것이 ``bool`` 이 아니라 목록인 이유는, 부르는 쪽이 *"있는가"* 와 *"무엇이
    남았는가"* 를 둘 다 물어보기 때문이다 — 검사 실패 문장에 그 값을 실어야 다음 사람이
    어디를 볼지 안다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = prose_node_ids(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
    ]


def references(path: Path, word: str) -> bool:
    """이 파일의 **코드가** 그 낱말을 쓰는가. docstring 에만 있으면 ``False``."""
    return word in code_string_literals(path)


def called_names(path: Path) -> set[str]:
    """이 파일이 **이름으로 부르는** 함수들 (``foo(...)`` 의 ``foo``).

    ★ *"그 함수를 실제로 부르는가"* 를 재는 자리다. import 문만 보면 **쓰지 않고
      import 만 해도** 통과하고, 문자열로 훑으면 주석에 이름을 적어도 통과한다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
