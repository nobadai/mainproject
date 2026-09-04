"""실운영 등록이 실 경락가를 꽂는지 (2026-09-03).

🔴 **mock 으로 두면 안이 하나도 안 나온다.** 실측으로 확인했다.

```text
같은 payload · as_of=2025-12-31
  mock      배추 0안 · 무 0안   self_check 가 전부 컷
  실 경락가  배추 2안 · 무 2안   business=ok
```

`max_price` 는 실 ML 예측 q90 에서 오고 `grade_unit_price` 는 시세에서 온다.
한쪽만 mock 이면 **출처가 다른 두 값을 비교**하게 된다.

```text
max_price          실 ML 예측 q90      배추 992 · 무 795
grade_unit_price   mock                배추 1,650 · 무 1,100
```

그래서 `self_check` 가 전부 컷하고 `no_proposal_reason` 만 남았다 — 마스터가 보기에는
*"매입이 돌긴 했는데 안이 없다"* 이고, 사용자가 보기에는 *"매입안이 안 나온다"* 였다.

⚠️ **이 파일은 DB 를 안 읽는다.** 배선만 본다 — 실제 조회는 `-m db` 쪽이 잰다.
"""

from __future__ import annotations

import ast
import inspect
from functools import partial
from pathlib import Path

import app.main
from app.master import wiring
from app.purchase_agent.adapter import purchase_port


def _registered_purchase():
    return wiring.registry().get("purchase")


def test_등록된_매입이_시세를_주입받았다():
    """🔴 이 판의 주장이다. `purchase_port` 를 맨몸으로 등록하면 mock 이 된다."""
    port = _registered_purchase()

    assert isinstance(port, partial), (
        "매입이 partial 이 아니다 — 시세를 안 꽂고 등록하면 mock 기본값이 쓰인다"
    )
    assert port.func is purchase_port
    assert "quotes" in port.keywords, f"quotes 를 안 넘긴다: {sorted(port.keywords)}"
    assert port.keywords["quotes"] is not None, "quotes=None 이면 mock 으로 떨어진다"


def test_꽂힌_시세가_경락가_공급자다():
    """★ 아무 공급자나 꽂혀도 되는 것이 아니다. **DB 를 읽는 그것**이어야 한다."""
    quotes = _registered_purchase().keywords["quotes"]

    source_file = inspect.getsourcefile(quotes)
    assert source_file is not None
    assert Path(source_file).name == "quotes.py", (
        f"시세 공급자가 quotes.py 에서 안 왔다: {source_file}"
    )


def test_인자_기본값은_여전히_mock_이다():
    """★ **기본값을 바꾸지 않는다.** 결정론 스위트가 DB 없이 돌아야 한다.

    실운영 기본값은 등록 자리(`app/main.py`)에서 정하고, 함수 기본값은
    테스트가 쓰는 것으로 남긴다. 둘을 같게 만들면 스위트가 DB 에 묶인다.
    """
    signature = inspect.signature(purchase_port)

    assert signature.parameters["quotes"].default is None, (
        "purchase_port 의 quotes 기본값이 바뀌었다 — 결정론 스위트가 DB 를 타게 된다"
    )


def test_등록_한_줄이_mock_으로_되돌아가지_않았다():
    """AST 로 등록 자리를 직접 본다.

    ⚠️ 위 검사들은 **import 된 결과**를 보므로, 누가 다른 자리에서 다시 등록하면
      그 자리가 이겨도 여기는 통과할 수 있다. 등록 문장 자체를 못 박는다.
    """
    main_py = Path(app.main.__file__)
    tree = ast.parse(main_py.read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_agent"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "purchase"
    ]
    assert len(calls) == 1, f"매입 등록이 {len(calls)} 곳이다"

    second = calls[0].args[1]
    assert isinstance(second, ast.Call), (
        "매입을 맨몸 함수로 등록한다 — partial(..., quotes=...) 이어야 한다"
    )
    assert "quotes" in [kw.arg for kw in second.keywords], "등록 자리에 quotes 가 없다"
