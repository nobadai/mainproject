"""🔴 마스터 코드가 번인 실행 축을 **기본값으로도, 박은 인자로도** 쓰지 않는다 (소스 스캔).

2026-09-14 · 규칙상 `SIM-BURNIN-202512` 는 baseline 시드 전용이고 걷지 않는데, 그 실행에
2026년 날짜 재고 이동과 예외 행이 쌓였다.

```text
실행 축 사고는 지금까지 전부 같은 모양이었다
  축이 빠졌다                    → 받는 쪽이 번인으로 메웠다
  함수 기본값 = BURN_IN_SIM_RUN_ID → 안 준 호출이 조용히 번인 장부에 썼다
  sim_run_id=BURN_IN_SIM_RUN_ID   → 받은 축을 무시하고 번인을 읽었다 (day_gate._blocked)
```

★ **자리마다 기억에 맡기면 다음 자리에서 또 빠진다.** 그래서 `app/master/` 를 AST 로
  읽어 두 모양을 전부 찾는다. 주석 · docstring · 메시지 문자열은 AST 에서 이름이 아니라
  안 걸리고, 번인을 **거부하는 비교문**(`== BURN_IN_SIM_RUN_ID`)도 인자나 기본값이 아니라
  안 걸린다.

🔴 **0건을 세는 잠금은 스캔이 눈을 감으면 거짓으로 초록이다.** 그래서 스캔한 파일이
  0개면 실패하고, 허용 목록의 자리가 실제로 안 걸려도 실패한다.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

BACKEND = Path(__file__).resolve().parents[2]

#: 스캔 대상. **마스터 소유 코드 전부**다.
SCAN_ROOT = BACKEND / "app" / "master"

_NAME = "BURN_IN_SIM_RUN_ID"

#: 🔴 **명시적으로 남기기로 한 자리.** `(파일, 모양, 이름)` → 남기는 이유.
#:   여기 있는 것은 *"번인으로 떨어지는 것을 안다"* 이지 *"괜찮다"* 가 아니다.
ALLOWED: dict[tuple[str, str, str], str] = {
    ("ledger_repository.py", "default", "get_burn_in"): (
        "이름부터 번인 전용 읽기다. `sim_runs` · `daily_closings` SELECT 둘뿐이고 쓰지 않는다."
        " 기본값을 쓰는 호출자는 번인 화면(`service.get_burn_in_history`) 하나다"
    ),
    ("ask_service.py", "call", "ExecutionContext"): (
        "발화문 조회 흐름(`_run_status`)이다. 부서에 묻기만 하고 장부를 안 바꾼다."
        " 적는 것은 실행 이력 한 행이다. 요청(`AskRequest`)에 축 칸이 없어 따로 판단한다"
    ),
}


def _is_burn_in(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Name):
        return node.id == _NAME
    if isinstance(node, ast.Attribute):
        return node.attr == _NAME
    return False


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return "<call>"


def _scan() -> tuple[int, list[tuple[str, str, str, int]]]:
    """`(스캔한 파일 수, [(파일, 모양, 이름, 줄)])`. 모양은 `default` · `call` 둘이다."""
    files = sorted(SCAN_ROOT.rglob("*.py"))
    found: list[tuple[str, str, str, int]] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                name = getattr(node, "name", "<lambda>")
                defaults = [*node.args.defaults, *node.args.kw_defaults]
                if any(_is_burn_in(d) for d in defaults):
                    found.append((path.name, "default", name, node.lineno))
            elif isinstance(node, ast.Call):
                if any(_is_burn_in(kw.value) for kw in node.keywords) or any(
                    _is_burn_in(arg) for arg in node.args
                ):
                    found.append((path.name, "call", _call_name(node), node.lineno))
    return len(files), found


def test_스캔이_마스터_파일을_실제로_읽는다() -> None:
    """🔴 **0개면 실패한다.** 경로가 어긋나 아무것도 안 읽으면 *"없다"* 가 아니라 *"안 봤다"* 다."""
    count, found = _scan()

    assert count > 0, f"{SCAN_ROOT} 에서 파이썬 파일을 하나도 못 읽었다 — 스캔이 눈을 감았다"
    seen = {(f, shape, name) for f, shape, name, _ in found}
    stale = sorted(set(ALLOWED) - seen)
    assert not stale, f"허용 목록의 자리가 안 걸렸다 — 스캔이 헛돌거나 목록이 낡았다: {stale}"


def test_번인_축을_기본값이나_박은_인자로_쓰는_자리가_허용_목록_밖에_없다() -> None:
    """🔴 **새 자리가 생기는 날 그 자리에서 걸린다.**"""
    count, found = _scan()
    assert count > 0, "스캔 대상이 0개다"

    leaked = [
        f"{f}:{line} {shape} {name}"
        for f, shape, name, line in found
        if (f, shape, name) not in ALLOWED
    ]
    assert not leaked, (
        "번인 실행 축을 기본값이나 박은 인자로 쓰는 자리가 있다."
        " 부르는 쪽이 자기 축을 넘기게 하라: " + ", ".join(leaked)
    )


# ── 손으로 부르는 하루 엔드포인트 ─────────────────────────────────────────

#: `(경로 끝, 라우터가 부르는 이름)`. 사건 다섯이 전부 장부를 바꾼다.
_DAY_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("open", "run_open_day"),
    ("receive", "run_receive_arrivals"),
    ("issue-receivables", "run_issue_receivables"),
    ("collect", "run_collect_receipts"),
    ("close", "run_close_day"),
)

_WALK = "SIM-TEST-HAND-CALL"


class _Spy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, as_of: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"sentinel": True}


@pytest.fixture
def client() -> TestClient:
    import app.main

    return TestClient(app.main.app)


def _spy_on(monkeypatch: pytest.MonkeyPatch, attr: str) -> _Spy:
    from app.master import router as router_module

    spy = _Spy()
    monkeypatch.setattr(router_module, attr, spy)
    return spy


@pytest.mark.parametrize("tail, attr", _DAY_ENDPOINTS)
def test_하루_엔드포인트가_번인_축을_거부한다(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, tail: str, attr: str
) -> None:
    """🔴 번인은 기초 상태 시드 전용이다. 손으로 부른 하루가 거기 쌓이면 안 된다."""
    spy = _spy_on(monkeypatch, attr)

    resp = client.post(f"/master/days/2026-01-07/{tail}", params={"sim_run_id": BURN_IN_SIM_RUN_ID})

    assert resp.status_code == 400, resp.text
    assert BURN_IN_SIM_RUN_ID in resp.json()["detail"]
    assert spy.calls == [], "거부했는데 장부 함수를 불렀다"


@pytest.mark.parametrize("tail, attr", _DAY_ENDPOINTS)
def test_하루_엔드포인트가_축_없는_요청을_받지_않는다(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, tail: str, attr: str
) -> None:
    """🔴 축을 안 주면 번인으로 메우지 않고 요청 자체를 거절한다."""
    spy = _spy_on(monkeypatch, attr)

    없음 = client.post(f"/master/days/2026-01-07/{tail}")
    빈값 = client.post(f"/master/days/2026-01-07/{tail}", params={"sim_run_id": "  "})

    assert 없음.status_code == 422, 없음.text
    assert 빈값.status_code == 400, 빈값.text
    assert spy.calls == []


@pytest.mark.parametrize("tail, attr", _DAY_ENDPOINTS)
def test_하루_엔드포인트가_받은_축을_그대로_넘긴다(
    monkeypatch: pytest.MonkeyPatch, tail: str, attr: str
) -> None:
    """★ 요청이 준 축이 장부 함수까지 간다. 라우터가 다시 고르지 않는다."""
    from app.master import router as router_module

    spy = _spy_on(monkeypatch, attr)
    handler = {
        "open": router_module.master_open_day,
        "receive": router_module.master_receive_arrivals,
        "issue-receivables": router_module.master_issue_receivables,
        "collect": router_module.master_collect_receipts,
        "close": router_module.master_close_day,
    }[tail]

    handler(date(2026, 1, 7), sim_run_id=_WALK)

    assert spy.calls == [{"sim_run_id": _WALK}]
