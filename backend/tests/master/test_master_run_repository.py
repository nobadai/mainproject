"""마스터 실행이력 저장소 — **표를 옮긴 것이 실제로 지켜지는가.**

2026-09-02 에 마스터가 `orchestrator_agent_runs` 에서 `master_agent_runs` 로
나왔다. 그 이전에서 **조용히 되돌아갈 수 있는 자리 셋**을 잠근다.

```text
① 옛 표 이름으로 되돌아감        SQL 문자열이라 타입이 안 막는다
② pytest 가 공용 DB 를 오염시킴   history_enabled 를 새 모듈에 안 옮겼으면 그렇게 된다
③ 새 컬럼이 안 채워짐            item · end_code 를 적재가 안 넘기면 늘 NULL 이다
```

★ **실제 INSERT 는 돌지 않는다.** DB 를 세우지 않고 경계에서 가로챈다 —
  이 테스트가 확인하는 것은 "무엇을 어느 표에 넣으려 했는가" 이지 DB 동작이 아니다.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest

from app.master import persistence, run_repository
from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse

# ── ① 표 이름 ───────────────────────────────────────────────────────────────


def test_마스터_소유_표를_쓴다():
    """★ 표 이름은 SQL 문자열이라 타입이 안 막는다. 값으로 고정한다."""
    assert run_repository._TABLE == "master_agent_runs"


def _value_strings(module) -> list[str]:
    """모듈에서 **값으로 쓰이는 문자열**만. docstring 과 주석은 뺀다.

    🔴 **이 구분이 없으면 오탐이 난다.** 이 모듈의 헤더는 "왜 옛 표를 안 쓰는가" 를
      설명하느라 옛 표 이름을 적고 있다. 문서에 적힌 이름을 코드로 세면, 이유를
      친절히 적어 둘수록 테스트가 빨간불이 된다.

    같은 함정을 재시도 상한 대조 테스트에서 두 번 밟았다 (2026-09-01).
    규칙은 그때 정한 것 그대로다 — **값으로 안 쓰이는 문자열은 문서다.**
    """
    tree = ast.parse(inspect.getsource(module))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_옛_공용_표로_되돌아가지_않는다():
    """🔴 되돌아가면 실행이력이 두 표로 갈리고, 결정의 FK 가 어느 표를 가리키는지
    흐려진다. 옛 표는 Critic 이 계속 쓰므로 **살아 있어서** 되돌려도 에러가 안 난다 —
    그래서 이름으로 잠근다.
    """
    offenders = [s for s in _value_strings(run_repository) if "orchestrator_agent_runs" in s]
    assert not offenders, f"옛 표 이름을 값으로 쓴다: {offenders}"


def test_agent_축이_없다():
    """마스터 전용 표라 `agent` 는 늘 같은 값이었다.

    ★ 상수를 컬럼으로 두면 "언젠가 다른 값이 들어올 수 있다" 로 읽힌다.
      옛 저장소에는 `agent` 인자가 필수였다 — 그것이 안 남아 있어야 한다.
    """
    params = inspect.signature(run_repository.save_run).parameters
    assert "agent" not in params
    assert "item" in params
    assert "end_code" in params


# ── ② 공용 DB 오염 방지 ─────────────────────────────────────────────────────


def test_pytest_안에서는_적재하지_않는다():
    """🔴 **옛 저장소에서 실측으로 나온 규칙이다** — 표가 팀 공용 DB 에 있어
    테스트를 돌릴 때마다 가짜 실행이 쌓였다 (12행 중 10행이 테스트 산물).

    새 모듈로 옮기면서 이 가드를 빠뜨리면 **아무도 모르는 채로** 다시 쌓인다.
    """
    assert run_repository.history_enabled() is False


def test_적재가_실제로_건너뛴다(monkeypatch):
    """가드가 있어도 `try_save_run` 이 안 보면 소용이 없다."""
    called = False

    def _boom(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("pytest 안에서 save_run 이 불렸다")

    monkeypatch.setattr(run_repository, "save_run", _boom)
    assert run_repository.try_save_run(cycle="PROCUREMENT") is None
    assert called is False


# ── ③ 새 컬럼이 실제로 실리는가 ─────────────────────────────────────────────


def _response(end_code: str = "E1_APPROVED") -> ProcurementRunResponse:
    return ProcurementRunResponse(
        request_id="REQ-20251231-0001",
        as_of=date(2025, 12, 31),
        end_code=end_code,
        reason="",
        scenarios=[],
        judgment={},
        plan=[],
    )


def _request(item: str | None = "피마늘") -> ProcurementRunRequest:
    return ProcurementRunRequest(
        as_of=date(2025, 12, 31),
        policy_version="v1.3",
        item=item,
    )


def test_품목과_종료코드가_컬럼으로_넘어간다(monkeypatch):
    """🔴 **컬럼을 만들어 두고 안 채우면 늘 NULL 이다.**

    두 값은 payload 안에도 있어서, 안 넘겨도 아무 에러가 안 나고 응답도 멀쩡하다.
    "배추가 며칠째 E2 인가" 를 못 보게 되는 것만 조용히 남는다.
    """
    captured: dict[str, object] = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(persistence, "try_save_run", _capture)
    persistence.record(_request(), _response())

    assert captured["item"] == "피마늘"
    assert captured["end_code"] == "E1_APPROVED"
    assert captured["cycle"] == "PROCUREMENT"


def test_품목이_없으면_없는_대로_넘긴다(monkeypatch):
    """★ 없는 것을 지어내지 않는다. 품목 없이 도는 실행이 있고 그것도 이력이다."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: captured.update(kw))
    persistence.record(_request(item=None), _response())

    assert captured["item"] is None


@pytest.mark.parametrize(
    ("end_code", "expected"),
    [
        ("E1_APPROVED", "READY"),
        ("E2_HELD", "READY"),
        ("E4_NOT_STARTED", "RUNTIME_NOT_READY"),
    ],
)
def test_종료코드는_그대로_런타임상태는_접힌다(monkeypatch, end_code, expected):
    """두 값이 **다른 축**이라는 것을 고정한다.

    `end_code` 는 회사 상태(안이 없다·반려됐다)이고 `runtime_status` 는 실행 환경이
    섰는가다. 컬럼이 둘 다 생겼으니 섞이지 않는 것을 잠근다.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: captured.update(kw))
    persistence.record(_request(), _response(end_code))

    assert captured["end_code"] == end_code
    assert captured["runtime_status"] == expected


# ── 어휘 ────────────────────────────────────────────────────────────────────


def test_조회_사이클을_적을_수_있다():
    """★ **표를 나눈 이유가 이것이다.**

    옛 표의 `cycle` CHECK 에 `STATUS` 가 없어서 조회를 이력에 못 남겼다.
    어휘를 고치려면 오케·Critic 행의 뜻까지 건드려야 했기 때문이다.

    ⚠️ 배선은 아직이다 — 이 테스트가 확인하는 것은 **DDL 이 받는다**는 것뿐이다.
      `ask_service` 가 조회를 적재하기 시작하면 그때 관통으로 확인한다.
    """
    ddl = (
        __import__("pathlib").Path(__file__).parents[3] / "database" / "master_agent_runs.sql"
    ).read_text(encoding="utf-8")

    assert "'STATUS'" in ddl, "cycle 어휘에 STATUS 가 없다 — 표를 나눈 이유가 사라진다"
    assert "'A'" not in ddl, "오케 어휘(A·B)를 가져오면 안 된다"
