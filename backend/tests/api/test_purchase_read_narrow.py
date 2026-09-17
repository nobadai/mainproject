"""매입 탭 `_read` 의 두 조회를 좁힌다 — 결정은 그날 요청으로 · 실행 본문은 쓰는 칸만 (2026-09-17).

🔴 **결과가 같아야 하는 변경이라 값으로는 못 잠근다** (규칙 8). 좁혀도 안 좁혀도 화면은
같다. 그래서 여기서 잠그는 것은 셋이다::

    ① 결정 조회에 좁힌 조건이 실리나 — 문면 · 파라미터를 보고, 실행을 바꿔 따라오나
    ② 실행 조회가 본문을 통째로 안 끌어오나 — 문면
    ③ 🔴 **쓰는 칸을 빠뜨리지 않았나** — 이 모듈 소스에서 `payload` 로 읽는 `.get("…")` 을
       전부 모아 `_RUN_PAYLOAD` 와 **양쪽으로** 맞춘다

★ ③ 이 가장 위험한 자리다. 칸을 빠뜨리면 예외 없이 `None` 이 오고, 안별 컷 사유 · 사유
  문장이 「사유를 남긴 실행이 없습니다」로 **조용히** 바뀐다.

실측 (REH-0914 08-31) — 결정 11,427행 · 1.6MB 전부 → 그날 요청만 · 실행 본문 1.6MB → 96KB.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date
from typing import Any

import pytest

from app.api.purchase import query as purchase_query

AS_OF = date(2026, 8, 31)


class _Recorder:
    """`fetch_all` 대역. 실행 조회에는 **넣어 준 실행**을 돌려주고 나머지는 빈 목록."""

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = runs
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, query: Any, params: Any = None) -> list[dict[str, Any]]:
        text = query.as_string(None)
        self.calls.append((text, params))
        if "run_id, request_id" in text:
            return self.runs
        return []

    def only(self, needle: str) -> list[tuple[str, Any]]:
        return [(t, p) for t, p in self.calls if needle in t]


def _run(request_id: str | None) -> dict[str, Any]:
    return {"request_id": request_id, "sim_run_id": "SIM-X", "payload": {}}


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch):
    import app.finance.db as finance_db

    monkeypatch.setenv("DB_SCHEMA", "haetdeul")

    def _install(runs: list[dict[str, Any]]) -> _Recorder:
        rec = _Recorder(runs)
        monkeypatch.setattr(finance_db, "fetch_all", rec)
        return rec

    return _install


# ══════════════════════════════════════════════════════════════════════════
#  ① 결정 조회 — 그날 실행의 요청 ID 로
# ══════════════════════════════════════════════════════════════════════════

def test_결정_조회가_그날_실행의_요청으로_좁혀_나간다(install) -> None:
    """★ 규칙 8 — **실행을 바꿔** 조건이 따라오는지 본다. 상수와 대 보지 않는다."""
    앞 = install([_run("REQ-B"), _run("REQ-A"), _run("REQ-A"), _run(None)])
    purchase_query._read(AS_OF)
    뒤 = install([_run("REQ-C")])
    purchase_query._read(AS_OF)

    ((text_a, params_a),) = 앞.only("master_decisions")
    ((_text_b, params_b),) = 뒤.only("master_decisions")
    assert "request_id = ANY(%(request_ids)s)" in text_a
    #  ★ 겹친 요청은 한 번 · 요청 ID 가 없는 실행은 안 싣는다
    assert params_a["request_ids"] == ["REQ-A", "REQ-B"]
    assert params_b["request_ids"] == ["REQ-C"]


def test_그날_실행이_없으면_결정_조회를_안_낸다(install) -> None:
    """🔴 빈 목록으로 `ANY` 를 부르는 대신 **아예 안 낸다** — 조건을 빼고 전부 읽는 길이 없다."""
    rec = install([])

    data = purchase_query._read(AS_OF)

    assert rec.only("master_decisions") == []
    assert data["decisions"] == []


# ══════════════════════════════════════════════════════════════════════════
#  ② 실행 조회 — 본문을 통째로 안 끌어온다
# ══════════════════════════════════════════════════════════════════════════

def test_실행_조회가_본문을_통째로_안_끌어온다(install) -> None:
    rec = install([])

    purchase_query._read(AS_OF)

    ((text, _params),) = rec.only("run_id, request_id")
    assert "response_payload AS payload" not in text
    assert "jsonb_build_object" in text
    for key, sub in purchase_query._RUN_PAYLOAD.items():
        assert f"response_payload->'{key}'" in text, key
        for inner in sub:
            assert f"response_payload->'{key}'->'{inner}'" in text, (key, inner)


# ══════════════════════════════════════════════════════════════════════════
#  ③ 🔴 쓰는 칸을 빠뜨리지 않았나 — 소스에서 모아 양쪽으로 맞춘다
# ══════════════════════════════════════════════════════════════════════════

def _strip(node: ast.expr) -> ast.expr:
    """`(x or {})` 의 `x` 를 꺼낸다."""
    while isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        node = node.values[0]
    return node


def _is_payload(node: ast.expr) -> bool:
    """`payload` 이름이거나 `run["payload"]` · `r["payload"]` 같은 첨자인가."""
    node = _strip(node)
    if isinstance(node, ast.Name):
        return node.id == "payload"
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "payload"
    )


def _get_key(node: ast.AST) -> tuple[ast.expr, str] | None:
    """`<받는 쪽>.get("키")` 이면 (받는 쪽, 키)."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.func.value, node.args[0].value
    return None


def _payload_reads() -> dict[str, set[str]]:
    """이 모듈이 `payload` 에서 읽는 칸 — `{최상위 칸: {그 안에서 읽는 칸}}`."""
    tree = ast.parse(inspect.getsource(purchase_query))
    reads: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        got = _get_key(node)
        if got is None:
            continue
        receiver, key = got
        if _is_payload(receiver):
            reads.setdefault(key, set())
            continue
        inner = _get_key(_strip(receiver))
        if inner is not None and _is_payload(inner[0]):
            reads.setdefault(inner[1], set()).add(key)
    return reads


def test_payload_에서_읽는_칸이_전부_뽑혀_온다() -> None:
    """🔴 **빠뜨린 칸이 있으면 여기서 운다.** 새 칸을 읽으면 `_RUN_PAYLOAD` 에 먼저 적는다."""
    reads = _payload_reads()
    spec = {k: set(v) for k, v in purchase_query._RUN_PAYLOAD.items()}

    #  ★ 모으는 쪽이 고장 나 빈 집합이면 아래 비교가 거짓으로 초록이 된다 — 먼저 막는다
    assert reads, "소스에서 payload 읽기를 하나도 못 찾았다 — 스캐너가 낡았다"
    for key, inner in reads.items():
        assert key in spec, f"payload 에서 「{key}」를 읽는데 조회가 안 뽑는다"
        if spec[key]:
            assert inner <= spec[key], f"「{key}」 안에서 {inner - spec[key]} 를 읽는데 안 뽑는다"


def test_뽑아_오는_칸은_전부_실제로_쓰인다() -> None:
    """반대 방향 — 안 쓰는 칸을 뽑으면 좁힌 뜻이 조용히 흐려진다."""
    reads = _payload_reads()

    assert set(purchase_query._RUN_PAYLOAD) <= set(reads)
    for key, sub in purchase_query._RUN_PAYLOAD.items():
        assert set(sub) <= reads[key], (key, sub, reads[key])
