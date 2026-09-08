"""판단 기록의 실행 축 — `master_agent_runs.sim_run_id` (2026-09-08 · `Refs #150`).

`sim_run_id` 를 가진 표가 **22개**인데 판단 기록에만 칸이 없었다. 그래서 E2E 검증으로
만든 상태와 장기 걷기 상태가 한 통에 섞이고, 판단 1,206행을 실행별로 못 갈랐다.

이 파일이 잠그는 것은 **칸이 세워졌고 값이 흐르는가** 넷이다.

```text
①  _COLUMNS 와 MasterAgentRun 이 어긋나지 않는다   손으로 맞춰져 있어 조용히 갈린다
②  record_* 넷이 전부 축을 넘긴다                  하나만 빠져도 오류가 안 난다
③  빈 문자열은 NULL 로 접힌다                      모르는 것이 두 모양으로 앉으면 안 된다
④  list_runs 가 축으로 좁힌다                      좁힘이 빠져도 결과가 나오긴 한다
```

🔴 **기존 1,206행은 NULL 이다.** 「그 실행이었을 것」으로 채우지 않았다 —
  **없는 것과 모르는 것은 다르다.** 그래서 이 파일에도 백필을 확인하는 검사가 없다.

⚠️ **이 판은 칸을 세울 뿐, 검증 상태와 장기 상태를 가르지 않는다.** 가르는 것은
  새 `sim_run_id` 로 다시 걷는 별건이다.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest

from app.master import ask_service, persistence, run_repository, service
from app.master.envelope import ExecutionContext
from app.master.plan import ExecutionPlan
from app.master.schemas import (
    ProcurementRunRequest,
    ProcurementRunResponse,
    SalesRunRequest,
    SalesRunResponse,
)
from app.master.status_flow import StatusOutcome

_AS_OF = date(2025, 12, 31)
_REQUEST_ID = "REQ-20251231-0001"
_SIM = "SIM-TEST-0001"


# ── ① `_COLUMNS` 와 `MasterAgentRun` ────────────────────────────────────────


def _columns_from_source() -> list[str]:
    """`run_repository._COLUMNS` 를 **소스에서** 읽는다.

    ★ 값(`run_repository._COLUMNS`)을 그냥 쓰지 않는 이유는 아래 짝 검사가 소스에서
      읽은 TypedDict 키와 맞추는 것이라, 한쪽만 값이면 비교가 비대칭이 되기 때문이다.
    """
    tree = ast.parse(inspect.getsource(run_repository))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_COLUMNS" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Tuple):
            continue
        return [
            e.value
            for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


def _typeddict_keys_from_source() -> list[str]:
    """`MasterAgentRun` 의 키를 소스에서 읽는다."""
    tree = ast.parse(inspect.getsource(run_repository))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MasterAgentRun":
            return [
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            ]
    return []


def test_스캐너가_실제로_무언가를_센다():
    """🔴 **자기 생존 검사다. 반드시 첫째로 둔다.**

    아래 짝 검사는 두 목록을 비교한다 — 둘 다 빈 목록이면 **비교가 통과한다.**
    `_COLUMNS` 의 이름이 바뀌거나 TypedDict 가 다른 모양이 되면 스캐너가 0개를 세고,
    그 순간 짝 검사는 아무것도 안 보면서 초록이 된다. **공짜 초록**이 가장 나쁘다.
    """
    columns = _columns_from_source()
    keys = _typeddict_keys_from_source()

    assert len(columns) >= 15, f"_COLUMNS 를 못 찾았다 (센 개수 {len(columns)}) — 스캐너가 죽었다"
    assert len(keys) >= 15, f"MasterAgentRun 을 못 찾았다 (센 개수 {len(keys)}) — 스캐너가 죽었다"
    assert "run_id" in columns and "run_id" in keys, "가장 기본인 칸조차 안 잡힌다"


def test_컬럼과_타입딕트가_어긋나지_않는다():
    """🔴 **둘이 손으로 맞춰져 있다.** 한쪽만 늘면 조용히 갈린다.

    ```text
    _COLUMNS 만 늘면      SELECT 는 그 칸을 가져오는데 타입에는 없다 — 읽는 쪽이 모른다
    TypedDict 만 늘면     타입은 있다고 하는데 행에 그 키가 없다 — KeyError 가 런타임에
    ```

    둘 다 문자열/식별자라 타입 검사기도 안 잡아 준다. 그래서 여기서 잠근다.
    """
    columns = _columns_from_source()
    keys = _typeddict_keys_from_source()

    assert columns == keys, (
        "_COLUMNS 와 MasterAgentRun 이 어긋난다.\n"
        f"  _COLUMNS 에만: {[c for c in columns if c not in keys]}\n"
        f"  TypedDict 에만: {[k for k in keys if k not in columns]}"
    )


def test_실행_축이_컬럼과_타입딕트에_있다():
    """축이 22개 표에 있는데 판단 기록에만 없었다 — 그 칸이 실제로 섰는지."""
    assert "sim_run_id" in _columns_from_source()
    assert "sim_run_id" in _typeddict_keys_from_source()


# ── ② `record_*` 넷이 전부 축을 넘긴다 ──────────────────────────────────────


def _procurement_request() -> ProcurementRunRequest:
    return ProcurementRunRequest(as_of=_AS_OF, policy_version="v1.3", item="배추")


def _procurement_response() -> ProcurementRunResponse:
    return ProcurementRunResponse(
        request_id=_REQUEST_ID,
        as_of=_AS_OF,
        end_code="E1_APPROVED",
        reason="",
        scenarios=[],
        judgment={},
        plan=[],
    )


def _sales_request() -> SalesRunRequest:
    return SalesRunRequest(
        as_of=_AS_OF, policy_version="v1.3", item="배추", business_mode="SPOT_SALES"
    )


def _sales_response() -> SalesRunResponse:
    return SalesRunResponse(
        request_id=_REQUEST_ID,
        as_of=_AS_OF,
        end_code="SL1_PRESENTED",
        reason="",
        candidates=[],
        plan=[],
    )


def _status_outcome() -> StatusOutcome:
    return StatusOutcome(
        status_code="S1_ANSWERED",
        reason="",
        plan=ExecutionPlan(request_id=_REQUEST_ID, as_of=_AS_OF),
        answers={"finance": {"cash": 1}},
    )


def _context(sim_run_id: str = _SIM) -> ExecutionContext:
    return ExecutionContext(
        request_id=_REQUEST_ID,
        as_of=_AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
        sim_run_id=sim_run_id,
    )


def _capture(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: captured.update(kw))
    return captured


def test_매입_적재가_축을_넘긴다(monkeypatch):
    captured = _capture(monkeypatch)
    persistence.record(_procurement_request(), _procurement_response(), sim_run_id=_SIM)
    assert captured["sim_run_id"] == _SIM


def test_판매_적재가_축을_넘긴다(monkeypatch):
    captured = _capture(monkeypatch)
    persistence.record_sales(_sales_request(), _sales_response(), sim_run_id=_SIM)
    assert captured["sim_run_id"] == _SIM


def test_조회_적재가_축을_넘긴다(monkeypatch):
    captured = _capture(monkeypatch)
    persistence.record_status(
        request_id=_REQUEST_ID,
        as_of=_AS_OF,
        policy_version="v1.3",
        intent={"action": "STATUS_QUERY", "agents": ["finance"]},
        outcome=_status_outcome(),
        sim_run_id=_SIM,
    )
    assert captured["sim_run_id"] == _SIM


def test_재검증_적재가_축을_넘긴다(monkeypatch):
    """★ 여기만 인자가 아니라 **봉투에서** 온다 — `record_revalidation` 은 이미
    `ExecutionContext` 를 받는다. 같은 사실의 주인은 하나다.
    """
    captured = _capture(monkeypatch)
    persistence.record_revalidation(
        _context(),
        outcome="PASSED",
        reason="",
        validations={},
        unroutable=(),
        plan=ExecutionPlan(request_id=_REQUEST_ID, as_of=_AS_OF),
    )
    assert captured["sim_run_id"] == _SIM


# ── 넷 다인가 — 하나만 빠져도 그 사이클에 축이 없다 ─────────────────────────


def _record_functions() -> list[str]:
    """`persistence` 의 적재 함수 이름 전부."""
    tree = ast.parse(inspect.getsource(persistence))
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and (node.name == "record" or node.name.startswith("record_"))
    ]


def test_적재_함수_전부가_축을_넘긴다():
    """🔴 **넷을 손으로 세지 않는다.** 다섯째 사이클이 붙는 날 위의 개별 검사는
    그대로 초록이고, 그 사이클만 축이 없다.

    ★ 자기 생존: 함수를 하나도 못 찾으면 먼저 실패한다.
    """
    names = _record_functions()
    assert len(names) >= 4, f"적재 함수를 못 찾았다: {names} — 스캐너가 죽었다"

    source = inspect.getsource(persistence)
    tree = ast.parse(source)
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in names:
            continue
        passes = any(
            isinstance(call, ast.Call) and any(kw.arg == "sim_run_id" for kw in call.keywords)
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call))
        )
        if not passes:
            offenders.append(node.name)

    assert not offenders, f"축을 안 넘기는 적재 함수: {offenders}"


def test_진입점이_봉투에서_축을_꺼낸다():
    """🔴 **적재 함수가 받을 줄 알아도 아무도 안 주면 늘 NULL 이다.**

    값의 주인은 `ExecutionContext.sim_run_id` 하나다. 진입점이 상수를 다시 적거나
    아예 안 주면, 부서로 나간 값과 표에 적힌 값이 갈린다.

    ★ 자기 생존: 적재 호출 줄을 하나도 못 찾으면 먼저 실패한다.
    """
    checked = 0
    offenders = []
    for module, callees in (
        (service, ("persistence.record(", "persistence.record_sales(")),
        (ask_service, ("persistence.record_status(",)),
    ):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            snippet = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            if not any(c in snippet for c in callees):
                continue
            checked += 1
            if not any(
                kw.arg == "sim_run_id"
                and isinstance(kw.value, ast.Attribute)
                and kw.value.attr == "sim_run_id"
                for kw in node.keywords
            ):
                offenders.append(f"{module.__name__}:{node.lineno}")

    assert checked >= 8, f"적재 호출을 {checked} 개만 찾았다 — 스캐너가 죽었다"
    assert not offenders, f"봉투에서 축을 안 꺼내는 적재 호출: {offenders}"


# ── ③ 빈 문자열은 NULL 이다 ────────────────────────────────────────────────


def _saved_params(monkeypatch, sim_run_id) -> tuple:
    captured: dict[str, tuple] = {}

    def _fake(_query, params):
        captured["params"] = params
        return {"run_id": "x"}

    monkeypatch.setattr(run_repository, "execute_returning_one", _fake)
    run_repository.save_run(
        cycle="PROCUREMENT",
        as_of=_AS_OF,
        request_payload={},
        response_payload={},
        sim_run_id=sim_run_id,
    )
    return captured["params"]


@pytest.mark.parametrize("blank", ["", None])
def test_빈_값은_널로_적힌다(monkeypatch, blank):
    """🔴 **`ExecutionContext.sim_run_id` 의 기본값이 `""` 이고, 그건 값이 아니라
    *"아직 안 실렸다"* 는 뜻이다** (`envelope.py` 의 ①②③).

    그대로 적으면 표 안에 **모르는 것이 두 모양**으로 앉는다 — 옛 1,206행은 NULL,
    안 실린 새 행은 `''`. 그러면 *"축이 없는 실행"* 을 세는 질문에 `IS NULL` 만으로는
    답이 안 나오고, `= ''` 를 잊은 조회가 조용히 틀린 수를 낸다.
    """
    assert _saved_params(monkeypatch, blank)[-1] is None


def test_실린_값은_그대로_적힌다(monkeypatch):
    """★ 접는 것은 **빈 값뿐**이다. 값이 있으면 손대지 않는다."""
    assert _saved_params(monkeypatch, _SIM)[-1] == _SIM


# ── ④ `list_runs` 가 축으로 좁힌다 ─────────────────────────────────────────


def _listed(monkeypatch, **kwargs) -> tuple[str, tuple]:
    captured: dict[str, object] = {}

    def _fake(query, params):
        captured["query"] = query.as_string(None)
        captured["params"] = params
        return []

    monkeypatch.setattr(run_repository, "fetch_all", _fake)
    run_repository.list_runs(**kwargs)
    return captured["query"], captured["params"]  # type: ignore[return-value]


def test_축으로_좁힌다(monkeypatch):
    """🔴 **좁힘이 빠져도 결과는 나온다** — 다른 실행의 판단까지 섞인 채로.

    E2E 검증으로 만든 상태와 장기 걷기 상태가 한 통에 있다는 것이 이 판의 출발점이다.
    좁히는 조건이 조용히 사라지면 그 통을 다시 못 가른다.
    """
    query, params = _listed(monkeypatch, sim_run_id=_SIM)

    where = query.split(" WHERE ", 1)[-1]
    assert '"sim_run_id" = %s' in where, f"좁히는 조건이 SQL 에 없다: {query}"
    assert _SIM in params


def test_안_주면_안_좁힌다(monkeypatch):
    """★ 기본값이 없다. 안 주면 전체다 — 기존 호출을 안 깨뜨린다.

    🔴 **여기가 백필을 안 한 자리와 이어진다.** 축이 NULL 인 1,206행은 어느 값으로도
      안 걸린다. 기본값을 박았으면 그 행들이 조용히 사라졌을 것이고, 그건
      *"모른다"* 를 감추는 일이다.
    """
    query, params = _listed(monkeypatch, item="배추")

    where = query.split(" WHERE ", 1)[-1]
    assert "sim_run_id" not in where, f"안 준 조건이 붙었다: {query}"
    assert "배추" in params
