"""재무 어댑터가 **봉투의 실행 축을 읽는가.**

🔴 이 파일이 생긴 이유 (2026-09-11 · 매입 판단 513건).

    번인과 걷기가 함께 서 있는 날, 어댑터는 축 없이 전역 Current State 를 물었다.

    ```text
    _load_context(as_of)                       ← 축 인자가 없다
      → get_current_finance_runtime_context(as_of)
      → _get_current_finance_state_row(as_of, sim_run_id=None)
      → 🔴 FinanceDataNotReady("finance_runtime_axis_ambiguous")
    ```

    `db.py` 의 fail-closed 는 **옳았다.** 축을 안 주면 번인과 걷기 둘이 보이니
    정확히 막은 것이다. 틀린 것은 **질문**이었다 — 어댑터가 축을 물어보지 않았다.

★★ 그리고 그 실패가 `except Exception: return None` 에 삼켜져
   `missing=("finance_state", "finance_policy")` 로 나갔다. 삼키는 태도는 옳다
   (*"없는 것은 예외가 아니라 상태다"*). **어휘가 틀렸다** — 「못 물어봤다」가
   「자료가 없다」로 적혀 나가서, 읽는 사람은 멀쩡히 있는 재무 자료를 하루 종일
   찾으러 갔다.

---

재무 파트가 문서로 확정해 준 기준 여섯 (2026-09-11) — **이 파일이 그것을 잠근다.**

```text
①  Finance Adapter 는 봉투의 sim_run_id 를 그대로 사용
②  _load_context 에서 그 sim_run_id 기준으로 Finance Runtime Context 조회
③  Finance 가 별도로 실행축을 추측하거나 전역 Current State 에서 하나를 고르지 않음
④  sim_run_id 가 빈 상태로 Finance 까지 들어오면 번인으로 임의 fallback 하지 않음
⑤  라우터·화면의 번인 호환은 마스터가 봉투 생성 단계에서 축을 채우는 계약으로 유지
⑥  그럼에도 빈 축이 닿으면 RUNTIME_NOT_READY / skipped 로 두고 sim_run_id 누락을 명시
```

★ 전부 **대역**이다. 실 DB 를 타지 않는다.
"""

from __future__ import annotations

import ast
import inspect
import unicodedata
from datetime import date
from pathlib import Path

import pytest

from app.finance import adapter
from app.finance import user_messages as messages
from app.finance.application.orchestration import FinanceAgentController
from app.master.envelope import AgentRequest, ExecutionContext
from tests.finance.test_finance_adapter import AS_OF, _AdapterPlanner, _Context

#: 실 장애가 난 조합 그대로. 번인과 걷기가 **함께** 서 있다.
BURN_IN = "SIM-BURNIN-202512"
WALK = "SIM-WALK-202601-LOAN"

#: `_load_context` 가 닿는 mode 전부. `_controller_boundary` 로 들어오는 셋과
#: `_status_query` 하나다.
_AXIS_MODES = ("PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION", "STATUS_QUERY")

#: 공개 진입점으로 payload 없이 끝까지 갈 수 있는 mode.
#: (`SCENARIO_VALIDATION` · `SALES_VALIDATION` 은 payload 를 먼저 읽어 경계에 못 닿는다.)
_PAYLOAD_FREE_MODES = ("PRE_PURCHASE", "STATUS_QUERY")


def _req(*, sim_run_id: str, mode: str = "PRE_PURCHASE") -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(
            request_id="REQ-AXIS-0001",
            as_of=AS_OF,
            trigger="USER_REQUEST",
            policy_version="POLICY-V1",
            sim_run_id=sim_run_id,
        ),
        agent="finance",
        mode=mode,
        payload={},
    )


class _축_기록기:
    """`_load_context` 대역. **무엇을 물었는지**를 든다."""

    def __init__(self, context=None):
        self.context = context
        self.calls: list[tuple[date, str]] = []

    def __call__(self, as_of, *, sim_run_id):
        self.calls.append((as_of, sim_run_id))
        return self.context


class _조회_기록기:
    """`get_current_finance_runtime_context` 대역 — `_load_context` **안쪽**을 본다."""

    def __init__(self, context=None):
        self.context = context
        self.calls: list[dict[str, object]] = []

    def __call__(self, as_of=None, *, sim_run_id=None):
        self.calls.append({"as_of": as_of, "sim_run_id": sim_run_id})
        if self.context is None:
            raise LookupError("Current Finance State was not found")
        return self.context


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    """Controller 와 이력 저장만 격리한다. **축 경로는 실물 그대로 둔다.**"""
    monkeypatch.setattr(
        adapter,
        "FinanceAgentController",
        lambda port: FinanceAgentController(port, _AdapterPlanner()),
    )
    monkeypatch.setattr(adapter, "save_finance_execution", lambda **_kwargs: None)
    monkeypatch.setattr("app.finance.execution.save_finance_execution", lambda **_kwargs: None)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------------------
# ①② 봉투에 실린 축이 `_load_context` 까지 **그대로** 간다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _AXIS_MODES)
def test_봉투의_축이_그대로_load_context_까지_간다(monkeypatch, mode):
    """🔴 오늘 513건을 막은 자리. 축이 여기까지 안 오면 전역 조회가 된다."""
    기록기 = _축_기록기(_Context())
    monkeypatch.setattr(adapter, "_load_context", 기록기)

    if mode == "STATUS_QUERY":
        adapter._status_query(_req(sim_run_id=WALK, mode=mode))
    else:
        adapter._controller_boundary(_req(sim_run_id=WALK, mode=mode))

    assert 기록기.calls, "_load_context 를 부르지 않았다"
    assert [축 for _as_of, 축 in 기록기.calls] == [WALK]


@pytest.mark.parametrize("mode", _PAYLOAD_FREE_MODES)
@pytest.mark.parametrize("축", (BURN_IN, WALK))
def test_공개_진입점도_봉투의_축을_그대로_넘긴다(monkeypatch, mode, 축):
    """★ 값이 박혀 있지 않다 — 봉투가 무엇을 싣든 그것이 간다."""
    기록기 = _축_기록기(_Context())
    monkeypatch.setattr(adapter, "_load_context", 기록기)

    adapter.finance_port(_req(sim_run_id=축, mode=mode))

    assert [축_값 for _as_of, 축_값 in 기록기.calls] == [축]


def test_as_of_도_함께_간다(monkeypatch):
    """★ 축이 생겼다고 `as_of` 계약이 사라지지 않는다 (§1.2-6)."""
    기록기 = _축_기록기(_Context())
    monkeypatch.setattr(adapter, "_load_context", 기록기)

    adapter.finance_port(_req(sim_run_id=WALK))

    assert 기록기.calls[0][0] == AS_OF


# ---------------------------------------------------------------------------
# ②③ 그 축으로 조회한다 — 전역에서 하나를 고르지 않는다
# ---------------------------------------------------------------------------


def test_load_context_는_받은_축으로_조회한다(monkeypatch):
    """🔴 `sim_run_id=None` 으로 물으면 번인과 걷기가 둘 다 보여 막힌다."""
    기록기 = _조회_기록기(_Context())
    monkeypatch.setattr(adapter, "get_current_finance_runtime_context", 기록기)

    context = adapter._load_context(AS_OF, sim_run_id=WALK)

    assert context is not None
    assert 기록기.calls == [{"as_of": AS_OF, "sim_run_id": WALK}]


def test_load_context_는_축_없이_묻지_않는다(monkeypatch):
    """★ 축을 받고도 전역으로 묻는 순간 남의 실행 잔액이 섞인다."""
    기록기 = _조회_기록기(_Context())
    monkeypatch.setattr(adapter, "get_current_finance_runtime_context", 기록기)

    adapter._load_context(AS_OF, sim_run_id=WALK)

    assert all(call["sim_run_id"] for call in 기록기.calls), "축 없는 조회가 있다"


def test_다른_실행의_축으로는_조회하지_않는다(monkeypatch):
    """번인이 함께 서 있어도 걷기 봉투는 걷기만 묻는다."""
    기록기 = _조회_기록기(_Context())
    monkeypatch.setattr(adapter, "get_current_finance_runtime_context", 기록기)
    monkeypatch.setattr(adapter, "_load_context", adapter._load_context)

    adapter.finance_port(_req(sim_run_id=WALK))

    assert [call["sim_run_id"] for call in 기록기.calls] == [WALK]
    assert BURN_IN not in [call["sim_run_id"] for call in 기록기.calls]


def test_load_context_는_sim_run_id_에_기본값을_두지_않는다():
    """🔴 기본값이 있으면 축을 빠뜨린 호출이 **조용히** 전역으로 떨어진다.

    ★ 그래서 키워드 전용 · 기본값 없음이다. 빠뜨리면 문법이 막는다.
    """
    파라미터 = inspect.signature(adapter._load_context).parameters["sim_run_id"]

    assert 파라미터.kind is inspect.Parameter.KEYWORD_ONLY
    assert 파라미터.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# ④ 빈 축이면 **번인으로 안 떨어진다**
# ---------------------------------------------------------------------------


def test_빈_축이면_DB_에_아예_묻지_않는다(monkeypatch):
    """🔴 물어보면 그 실패가 아래 `except` 에서 **자료 없음**으로 바뀐다.

    ★ 「못 물어봤다」와 「자료가 없다」는 고칠 자리가 완전히 다르다.
    """
    기록기 = _조회_기록기(_Context())
    monkeypatch.setattr(adapter, "get_current_finance_runtime_context", 기록기)

    assert adapter._load_context(AS_OF, sim_run_id="") is None
    assert 기록기.calls == [], "빈 축으로 조회했다"


@pytest.mark.parametrize("빈_축", ("", "   "))
@pytest.mark.parametrize("mode", _PAYLOAD_FREE_MODES)
def test_빈_축이면_어떤_실행으로도_떨어지지_않는다(monkeypatch, 빈_축, mode):
    """★ 하나뿐이라고 그것을 집으면 **오류 없이 숫자만** 남의 것이 된다."""
    기록기 = _조회_기록기(_Context())
    monkeypatch.setattr(adapter, "get_current_finance_runtime_context", 기록기)

    reply, _meta = adapter.finance_port(_req(sim_run_id=빈_축, mode=mode))

    assert 기록기.calls == [], "빈 축인데 재무 상태를 읽었다"
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"


# ---------------------------------------------------------------------------
# ⑥ 빈 축일 때 사유·`missing` 이 **축 누락이라고 말한다**
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _PAYLOAD_FREE_MODES)
def test_빈_축은_missing_data_에_sim_run_id_라고_적힌다(mode):
    """🔴 `finance_state` · `finance_policy` 로 적으면 **없는 자료를 찾으러 간다.**"""
    reply, _meta = adapter.finance_port(_req(sim_run_id="", mode=mode))

    assert reply.missing_data == ("sim_run_id",)
    assert "finance_state" not in reply.missing_data
    assert "finance_policy" not in reply.missing_data


@pytest.mark.parametrize("mode", _PAYLOAD_FREE_MODES)
def test_빈_축_사유는_자료가_없다고_말하지_않는다(mode):
    """★★ 이 문장이 저를 하루 헤매게 했습니다 (재무 기준 ⑥ · §3.1)."""
    reply, _meta = adapter.finance_port(_req(sim_run_id="", mode=mode))

    사유 = _nfc(reply.reasoning)

    assert 사유 == _nfc(messages.EXECUTION_AXIS_MISSING)
    assert 사유 != _nfc(messages.CONTEXT_UNAVAILABLE)
    assert _nfc("자금 현황 자료를 확인하지 못해") not in 사유
    assert _nfc("자료가 준비된 뒤에") not in 사유


def test_빈_축_사유는_요청에_실행이_없다고_말한다():
    """★ 읽는 사람이 **자료를 찾으러 가지 않게** 한다 — 그것이 이 판의 요점이다."""
    사유 = _nfc(messages.EXECUTION_AXIS_MISSING)

    assert _nfc("어느 실행의 자금 상황을 봐야 하는지가 요청에 실려 있지 않아") in 사유
    assert _nfc("자금 자료가 없는 것이 아니라") in 사유
    assert _nfc("요청이 실행을 지정하지 않은 것입니다") in 사유


def test_빈_축_사유에는_구현_용어가_없다():
    """★ `user_messages` 규칙 그대로 — 식별자는 `missing_data` 가 나른다."""
    assert "sim_run_id" not in messages.EXECUTION_AXIS_MISSING
    assert "finance" not in messages.EXECUTION_AXIS_MISSING.lower()


# ---------------------------------------------------------------------------
# 🟢 회귀 — 축을 주면 예전 동작 그대로다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _PAYLOAD_FREE_MODES)
def test_축이_있고_자료가_없으면_예전_어휘_그대로다(monkeypatch, mode):
    """★ 축 누락과 **자료 없음**은 다른 사건이다. 자료 없음은 그대로 남는다."""
    monkeypatch.setattr(adapter, "_load_context", _축_기록기(None))

    reply, _meta = adapter.finance_port(_req(sim_run_id=WALK, mode=mode))

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert set(reply.missing_data) == {"finance_state", "finance_policy"}
    assert _nfc(reply.reasoning) == _nfc(messages.CONTEXT_UNAVAILABLE)


def test_축이_있는_단일_실행은_READY_까지_간다(monkeypatch):
    """🟢 번인 단일 실행 회귀 — 축을 채운 봉투는 예전처럼 끝까지 간다."""
    monkeypatch.setattr(adapter, "get_current_finance_runtime_context", _조회_기록기(_Context()))

    reply, _meta = adapter.finance_port(_req(sim_run_id=BURN_IN))

    assert reply.runtime_status == "READY"


# ---------------------------------------------------------------------------
# 부르는 자리가 **전부** 넘긴다 — 원문으로 센다
# ---------------------------------------------------------------------------


def _adapter_tree() -> ast.Module:
    source = Path(inspect.getsourcefile(adapter)).read_text(encoding="utf-8")
    return ast.parse(source)


def _load_context_calls() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_adapter_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_load_context"
    ]


def test_load_context_를_부르는_자리가_하나도_안_빠진다():
    """🔴 **사람이 세지 않는다.** `grep | head` 로 세다 다섯을 놓친 적이 있다.

    ★ 원문 전체를 AST 로 훑어 호출을 전부 찾고, 그 전부가 축을 넘기는지 본다.
    """
    calls = _load_context_calls()

    assert calls, "_load_context 호출을 하나도 못 찾았다 — 검사가 헛돈다"
    축_없는_호출 = [
        call
        for call in calls
        if not any(keyword.arg == "sim_run_id" for keyword in call.keywords)
    ]
    assert 축_없는_호출 == [], (
        f"{len(축_없는_호출)}곳이 축을 안 넘긴다 "
        f"(줄 {[call.lineno for call in 축_없는_호출]})"
    )


def test_축을_읽는_자리와_부르는_자리의_개수가_같다():
    """★ 호출은 늘었는데 빈 축 방어가 안 붙는 것을 막는다."""
    calls = _load_context_calls()
    방어 = [
        node
        for node in ast.walk(_adapter_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_axis_not_ready"
    ]

    assert len(방어) == len(calls)


def test_어댑터는_축_없는_전역_조회를_남겨_두지_않는다():
    """🔴 `get_current_finance_runtime_context(as_of)` 가 되살아나면 여기서 걸린다."""
    조회 = [
        node
        for node in ast.walk(_adapter_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_current_finance_runtime_context"
    ]

    assert 조회, "조회 자체가 사라졌다 — 검사가 헛돈다"
    assert all(
        any(keyword.arg == "sim_run_id" for keyword in call.keywords) for call in 조회
    )
