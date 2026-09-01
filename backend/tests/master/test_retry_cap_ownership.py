"""재시도 상한 `2` 의 **소유자는 마스터**이고, 인용이 갈리면 여기서 잡는다.

🔴 **실측 2026-09-01 — 같은 수가 두 곳에 있고 잇는 검사가 없었다.**

```text
마스터   app/master/flow.py                    MAX_PURCHASE_ATTEMPTS = 2   원본
매입     app/purchase_agent/constraints.yaml   feedback.attempt_max = 2    인용
```

매입이 그 YAML 주석에 **먼저** 적어 두었다.

> *"우리 코드는 이 값을 아직 읽지 않는다. 재시도 상한을 실제로 집행하는 쪽은
>  마스터다 … 두 값이 갈라지면 '2회까지' 라는 말이 두 뜻을 갖게 된다"*

**저쪽이 적었는데 마스터는 소유한다고 아무 데도 안 적었다.** 선언이 한쪽에만 있으면
그건 합의가 아니라 추정이다.

★ **마스터는 이 YAML 을 런타임에 읽지 않는다.** 읽으면 마스터가 부서 설정을 배우는
  것이 된다. 대조는 **테스트가** 한다 — 앱이 남의 설정 파일을 읽는 것과 다르다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

import app.master
from app.master.flow import MAX_PURCHASE_ATTEMPTS
from app.purchase_agent.config import CONSTRAINTS_PATH, load_constraints

_MASTER_DIR = Path(app.master.__file__).parent


def _quoted(path: Path | None = None) -> int:
    """매입이 인용해 둔 값. **키가 없으면 `KeyError` 로 터진다** — 조용히 넘기지 않는다."""
    return int(load_constraints(path)["feedback"]["attempt_max"])


def test_매입_인용값이_마스터_원본과_같다():
    """둘 중 **어느 쪽을 바꿔도** 빨간불이 된다."""
    quoted = _quoted()
    assert quoted == MAX_PURCHASE_ATTEMPTS, (
        f"재시도 상한이 갈렸다 — 마스터 {MAX_PURCHASE_ATTEMPTS} · 매입 인용 {quoted}. "
        "소유자는 마스터이므로 매입 constraints.yaml 의 feedback.attempt_max 를 맞춘다."
    )


def test_대조가_갈린_값을_실제로_잡는다(tmp_path: Path):
    """★ **검사가 공허하지 않다는 증명.**

    지금은 둘 다 2 라서 위 단언은 그냥 통과한다. **갈린 값을 실제로 만들어 봐야**
    *"대조가 물린다"* 를 보인 것이 된다 — 값 비교만으로는 검사가 죽어 있어도 초록이다.
    """
    drifted = tmp_path / "constraints.yaml"
    original = CONSTRAINTS_PATH.read_text(encoding="utf-8")
    assert "attempt_max: 2" in original, "치환 대상이 없다 — 이 테스트가 무의미해진다"
    drifted.write_text(
        original.replace("attempt_max: 2", f"attempt_max: {MAX_PURCHASE_ATTEMPTS + 1}", 1),
        encoding="utf-8",
    )

    assert _quoted(drifted) != MAX_PURCHASE_ATTEMPTS, "갈린 값을 대조가 못 잡는다"


def test_인용_선언이_사라지면_KeyError_로_터진다(tmp_path: Path):
    """키를 지우는 것도 갈림이다 — *"선언이 없다"* 가 조용히 통과하면 안 된다."""
    gutted = tmp_path / "constraints.yaml"
    gutted.write_text(
        CONSTRAINTS_PATH.read_text(encoding="utf-8").replace("  attempt_max: 2", "  other: 1", 1),
        encoding="utf-8",
    )

    try:
        _quoted(gutted)
    except KeyError:
        return
    raise AssertionError("선언이 없는데 대조가 통과했다")


def test_서명_기본값이_리터럴이_아니라_상수다():
    """★ **값 비교로는 못 잡는다.** 둘 다 2 라서 그냥 통과한다 — 선언을 본다.

    서명에 리터럴 `2` 를 남기면 `MAX_PURCHASE_ATTEMPTS` 를 바꿔도 기본값이 안 따라오고,
    **소유자를 선언한 의미가 사라진다.**
    """
    source = (_MASTER_DIR / "flow.py").read_text(encoding="utf-8")
    assert "max_purchase_attempts: int = MAX_PURCHASE_ATTEMPTS," in source


def _code_strings(tree: ast.AST) -> list[str]:
    """docstring 을 뺀 **코드가 쓰는 문자열**만.

    🔴 줄 단위로 `#` 만 걸러서는 못 가른다. `MAX_PURCHASE_ATTEMPTS` 의 docstring 이
      `constraints.yaml` 을 **일부러** 가리키고 있어서, 그것까지 위반으로 잡혔다
      (이 테스트를 처음 돌렸을 때 실제로 그랬다). **가리키는 것과 읽는 것은 다르다.**

    🔴 `body[0]` 만 docstring 으로 치는 것도 부족했다. 상수 밑에 붙는 **속성
      docstring** 은 AST 로는 그냥 표현식 statement 라 걸러지지 않는다.
      **값으로 안 쓰이는 문자열은 전부 문서다** — 실행돼도 아무 일이 없다.
    """
    documentation = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    ]


def test_마스터는_매입_설정을_런타임에_안_읽는다():
    """대조는 테스트가 한다. **앱은 남의 설정 파일을 안 읽는다.**

    물류 `scenario_results` 를 마스터가 펴지 않는 것과 같은 자리다 — 남의 스키마를
    배우기 시작하면 그쪽이 바꿀 때마다 마스터가 깨진다.

    ⚠️ **한계.** import 와 코드 문자열만 본다. 경로를 조각내 만들어 읽으면 못 잡는다.
      그 경우를 막는 것은 이 테스트가 아니라 리뷰다.
    """
    offenders: list[str] = []
    for path in sorted(_MASTER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("app.purchase_agent.config")
            or isinstance(node, ast.Import)
            and any(alias.name.startswith("app.purchase_agent.config") for alias in node.names)
            for node in ast.walk(tree)
        )
        reads_yaml = any("constraints.yaml" in text for text in _code_strings(tree))

        if imported or reads_yaml:
            offenders.append(path.name)

    assert not offenders, f"마스터가 매입 설정을 읽는다: {offenders}"


def test_로더를_거치지_않고_읽어도_같다():
    """`load_constraints` 가 값을 변형하지 않는 것까지 본다."""
    raw = yaml.safe_load(CONSTRAINTS_PATH.read_text(encoding="utf-8"))
    assert raw["feedback"]["attempt_max"] == MAX_PURCHASE_ATTEMPTS
