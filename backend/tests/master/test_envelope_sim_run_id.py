"""마스터가 만드는 봉투는 **어느 실행의 장부인지를 반드시 싣는다.**

물류 회신 `#325` (2026-09-06) — 물류가 `get_current_logistics_read` 를
`(sim_run_id, as_of, usage_scope)` 로 좁히려면 그 값이 필요한데, 물류는 그 값을
**박지도 추론하지도 않겠다**고 했다. 맞는 말이다: *"어느 실행의 장부인가"* 는 물류
사실이 아니다.

★ **왜 생성자 주입이 아닌가.** 전이·취소·하루넘김 셋은 어댑터가 **객체**라
  `app/main.py` 에서 `LogisticsTransitionAdapter(sim_run_id=…)` 로 넣었다. 그런데
  에이전트 경로는 `register_agent("inventory", logistics_port)` 로 **평범한 함수**를
  등록한다 — 넣을 생성자가 없다. 봉투 말고 길이 없다.

🔴 **`ExecutionContext.sim_run_id` 는 아직 기본값이 있다.** 생성 지점이 64곳이고
  62곳이 다섯 파트의 테스트라 한 판에 필수로 만들면 깨졌을 때 누구 것인지 못 가린다
  (`contracts_core` 이전에서 배운 그대로). 그래서 ①②③ 으로 나눴고, **이 파일이
  ① 을 지킨다** — 기본값이 있는 동안 마스터가 그 기본값으로 새는 것을 막는다.

⚠️ **왜 값을 실제로 돌려 보지 않고 AST 로 보나.** 두 경로 다 부서 LLM 을 부르고 DB 를
  읽는다. 그 비용을 매 CI 마다 치르면 이 검사는 곧 skip 으로 꺼진다. 그리고 여기서
  지키려는 것은 *"어떤 값이 나갔나"* 가 아니라 **"채우는 줄이 있나"** 다 — 그것은
  구문에 다 드러난다.

★ **나중에 세 번째 생성 지점이 생겨도 걸린다.** 이름을 열거하지 않고
  `app/master/` 전체를 훑기 때문이다. `#320` 에서 *"정상 경로를 안 쟀다"* 로 변이가
  안 울었던 자리와 같은 실수를 안 하려고 이렇게 썼다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.master
from app.master.envelope import ExecutionContext
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

_MASTER_DIR = Path(app.master.__file__).parent


def _execution_context_calls() -> list[tuple[Path, ast.Call]]:
    """`app/master/` 안의 모든 `ExecutionContext(...)` 호출."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(_MASTER_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "ExecutionContext":
                found.append((path, node))
    return found


def test_마스터가_봉투를_만드는_자리를_실제로_찾는다() -> None:
    """🔴 **먼저 이것부터.** 스캐너가 0건을 세면 아래 검사가 전부 공짜로 초록이 된다.

    `#320` 의 변이가 안 울었던 이유가 정확히 그 모양이었다 — 재는 줄은 있는데 재는
    대상이 없었다.
    """
    calls = _execution_context_calls()
    files = {path.name for path, _ in calls}
    assert calls, "app/master/ 안에서 ExecutionContext 생성 지점을 하나도 못 찾았다 — 스캐너가 고장 났다"
    assert {"service.py", "ask_service.py"} <= files, (
        f"판단·조회 두 경로가 다 잡혀야 한다. 잡힌 파일: {sorted(files)}"
    )


def test_마스터가_만드는_봉투는_전부_sim_run_id_를_채운다() -> None:
    """★ 하나라도 빠지면 그 경로만 조용히 빈 장부를 본다."""
    빠진_곳 = [
        f"{path.relative_to(_MASTER_DIR.parent.parent)}:{node.lineno}"
        for path, node in _execution_context_calls()
        if not any(kw.arg == "sim_run_id" for kw in node.keywords)
    ]
    assert not 빠진_곳, (
        "마스터가 만드는 ExecutionContext 는 sim_run_id 를 채워야 한다 "
        f"(물류 #325 · 봉투 ①). 안 채운 자리: {빠진_곳}"
    )


def test_채우되_빈_값으로_채우지_않는다() -> None:
    """⚠️ 인자만 있고 값이 `""` 면 안 채운 것과 같은데 위 검사는 통과한다."""
    빈_곳 = [
        f"{path.name}:{node.lineno}"
        for path, node in _execution_context_calls()
        for kw in node.keywords
        if kw.arg == "sim_run_id"
        and isinstance(kw.value, ast.Constant)
        and not str(kw.value.value or "").strip()
    ]
    assert not 빈_곳, f"sim_run_id 를 빈 값으로 채운 자리: {빈_곳}"


def test_기본값은_틀린_답조차_아니다() -> None:
    """🔴 **여기가 ③ 이 오게 만드는 자리다.**

    기본값을 `BURN_IN_SIM_RUN_ID` 로 박았으면 안 채운 자리가 **조용히 맞는 답**을
    받는다. 그러면 아무도 안 아프고 ③ 은 영영 안 온다. 빈 문자열은 쓰는 쪽에서
    반드시 걸리므로 파트들이 옮길 이유가 생긴다.
    """
    기본 = ExecutionContext(
        request_id="REQ-DEFAULT-PROBE", as_of=__import__("datetime").date(2026, 1, 5),
        trigger="ML_COMPLETE", policy_version="v1.3",
    )
    assert 기본.sim_run_id == ""
    assert 기본.sim_run_id != BURN_IN_SIM_RUN_ID, (
        "기본값이 실제로 도는 값이면 안 채운 자리가 아프지 않다 — ③ 이 안 온다"
    )


def test_실으면_그대로_간다() -> None:
    """봉투는 값을 바꾸지 않는다 — 아는 쪽이 공급하고 쓰는 쪽이 쓴다."""
    실린 = ExecutionContext(
        request_id="REQ-CARRY-PROBE", as_of=__import__("datetime").date(2026, 1, 5),
        trigger="ML_COMPLETE", policy_version="v1.3", sim_run_id=BURN_IN_SIM_RUN_ID,
    )
    assert 실린.sim_run_id == BURN_IN_SIM_RUN_ID
