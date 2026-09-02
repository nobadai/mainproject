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
from app.master.plan import ExecutionPlan
from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse
from app.master.status_flow import StatusOutcome

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


# ── 조회(STATUS) 적재 ───────────────────────────────────────────────────────


def _status_outcome(status_code: str = "S1_ANSWERED") -> StatusOutcome:
    return StatusOutcome(
        status_code=status_code,
        reason="",
        plan=ExecutionPlan(request_id="REQ-20251231-0001", as_of=date(2025, 12, 31)),
        answers={"finance": {"cash": 1}},
    )


def test_조회도_이력에_남는다(monkeypatch):
    """🔴 **조회는 안을 안 만들지만 예산을 쓰고 부서를 부른다.**

    안 남기면 그 호출이 이력에서 사라진다 - M-16 이 막으려는 "안 보이는 호출" 이다.
    조회만 계속 돌린 날과 아무것도 안 한 날이 이력에서 같아 보이면 안 된다.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: captured.update(kw))
    persistence.record_status(
        request_id="REQ-20251231-0001",
        as_of=date(2025, 12, 31),
        policy_version="v1.3",
        intent={"action": "STATUS_QUERY", "agents": ["finance"]},
        outcome=_status_outcome(),
    )

    assert captured["cycle"] == "STATUS"
    assert captured["end_code"] == "S1_ANSWERED"
    assert captured["request_id"] == "REQ-20251231-0001"


def test_조회는_품목_축이_아니다(monkeypatch):
    """★ 조회는 부서에 묻는다. 무엇을 물었는지는 request_payload 의 agents 에 남는다.

    품목 칸을 억지로 채우면 "이 조회는 피마늘에 대한 것" 이라는 없는 사실이 생긴다.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: captured.update(kw))
    persistence.record_status(
        request_id="REQ-20251231-0001",
        as_of=date(2025, 12, 31),
        policy_version="v1.3",
        intent={"action": "STATUS_QUERY", "agents": ["finance"]},
        outcome=_status_outcome(),
    )

    assert captured.get("item") is None
    assert captured["request_payload"]["intent"]["agents"] == ["finance"]


def test_아무도_못_답하면_미가동이다(monkeypatch):
    """`S3` 만 미가동이다 - 일부라도 답했으면 돌긴 돈 날이다.

    매입에서 `E4` 만 `RUNTIME_NOT_READY` 인 것과 같은 구분이다.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: captured.update(kw))
    for code, expected in (
        ("S1_ANSWERED", "READY"),
        ("S2_PARTIAL", "READY"),
        ("S3_UNAVAILABLE", "RUNTIME_NOT_READY"),
    ):
        persistence.record_status(
            request_id="REQ-20251231-0001",
            as_of=date(2025, 12, 31),
            policy_version="v1.3",
            intent={},
            outcome=_status_outcome(code),
        )
        assert captured["runtime_status"] == expected, code


def test_읽는_쪽이_사이클을_밝힌다():
    """🔴 **조회와 매입이 같은 업무 키를 쓴다.**

    둘 다 `make_request_id(as_of)` 로 REQ-20251231-0001 을 만든다 - 순번 관리가
    호출자 몫이라 화면이 안 주면 같아진다.

    조회를 적재하기 시작했으므로, 사이클을 안 밝히면 조회가 최신 행이 되는 날

    ```text
    결정 경로   승인할 실행을 찾다가 조회를 집는다 - 조회는 승인 대상이 아니다
    이력 화면   매입 실행 자리에 조회가 뜬다
    보고서      안이 없는 실행으로 매입안 보고서를 만들려 한다
    ```

    셋 다 예외가 안 나고 화면만 조용히 틀린다. 그래서 호출자를 고정한다.
    """
    import inspect as _inspect

    from app.master import decision_service, service

    for module in (service, decision_service):
        source = _inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "get_run_by_request_id(" not in stripped:
                continue
            if "def " in stripped or "import" in stripped:
                continue
            assert "cycle=" in stripped, (
                f"{module.__name__} 이 사이클 없이 실행을 찾는다: {stripped}"
            )


def test_조회_경로가_적재를_부른다(monkeypatch):
    """🔴 **적재 함수가 있어도 아무도 안 부르면 이력은 안 남는다.**

    앞의 테스트들은 `record_status` 를 직접 부른다 - 그것만으로는 `_run_status` 가
    실제로 그 함수를 부르는지 알 수 없다. 변이 테스트에서 호출을 통째로 지웠는데
    한 건도 안 걸렸다 (2026-09-02). 그 구멍을 메운다.
    """
    from app.master import ask_service
    from app.master.llm.schemas import Intent

    called: dict[str, object] = {}
    monkeypatch.setattr(ask_service.persistence, "record_status", lambda **kw: called.update(kw))
    monkeypatch.setattr(ask_service.wiring, "missing", lambda: ("finance", "inventory", "purchase"))

    ask_service._run_status(
        request_id="REQ-20251231-0001",
        as_of=date(2025, 12, 31),
        policy_version="v1.3",
        budget=12,
        intent=Intent(action="STATUS_QUERY", agents=["finance"], confidence="HIGH"),
    )

    assert called, "_run_status 가 record_status 를 부르지 않았다"
    assert called["request_id"] == "REQ-20251231-0001"
    assert called["outcome"].status_code == "S3_UNAVAILABLE"


def test_어댑터가_없어_못_물어본_날도_남는다(monkeypatch):
    """★ **안 부른 것과 못 부른 것은 다르다.**

    미등록으로 접히는 경로에서 먼저 돌려주면 그 날이 이력에서 사라진다.
    "조회를 안 했다" 와 "조회했는데 아무도 없었다" 가 같아 보이면 안 된다.
    """
    from app.master import ask_service
    from app.master.llm.schemas import Intent

    called: dict[str, object] = {}
    monkeypatch.setattr(ask_service.persistence, "record_status", lambda **kw: called.update(kw))
    monkeypatch.setattr(ask_service.wiring, "missing", lambda: ("inventory",))
    monkeypatch.setattr(ask_service.wiring, "registry", dict)

    ask_service._run_status(
        request_id="REQ-20251231-0001",
        as_of=date(2025, 12, 31),
        policy_version="v1.3",
        budget=12,
        intent=Intent(action="STATUS_QUERY", agents=["inventory"], confidence="HIGH"),
    )

    outcome = called["outcome"]
    assert outcome.status_code == "S3_UNAVAILABLE"
    assert "inventory" in outcome.unavailable
