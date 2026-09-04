"""주말에는 안 돈다 — **실행일만 평일**, 그런데 **경과일수는 달력일 그대로**.

2026-09-04.

🔴 시뮬레이션이 토·일에도 판단을 만들고 있었다. 그런데 토·일에는 시장이 안 서서
**ML 예측이 없다.**

실측(2026-09-04):

```text
ML 예측 기준일 12개        전부 평일        주말 기준일 0건
주말 대상일 값             전부 is_filled=True  (직전 개장일 값 복사)
주말인데 실측값인 행        0건
```

없는 값을 복사본으로 채워 판단하면, 그 판단은 시장을 본 것이 아니라 **금요일을 두 번
본 것**이다.

★ **그런데 주말이 사라지는 것은 아니다.** 판단을 안 할 뿐이다. 재고는 토·일에도
  늙고 도착일·지급일도 달력일로 온다. 그래서 금요일 다음 실행일은 월요일이고
  **그 사이는 3일**이다.

  이 파일에서 **일수 차를 명시적으로 단언하는 검사들**(`test_금요일_다음은_사흘_뒤_월요일`
  등)이 그 사실을 고정한다. 거기가 무너지면 "다음 실행일" 이 "하루 뒤" 로 읽히기
  시작하고, 재고 나이와 지급일이 조용히 이틀씩 어긋난다.

⚠️ **공휴일은 못 거른다.** 설·추석에도 이 모듈은 평일이라고 답한다. 같은 한계가
  `app/purchase_agent/constraints.yaml:62` 에 이미 적혀 있고, 이 판에서 넓히지
  않았다.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, timedelta
from typing import Any

import pytest

from app.master import execution_day
from app.master.execution_day import is_execution_day, next_execution_day

# 2026-09-07(월) ~ 2026-09-13(일). 한 주를 통째로 돈다.
_MONDAY = date(2026, 9, 7)
_WEEK = [_MONDAY + timedelta(days=n) for n in range(7)]

_THURSDAY = date(2026, 9, 10)
_FRIDAY = date(2026, 9, 11)
_SATURDAY = date(2026, 9, 12)
_SUNDAY = date(2026, 9, 13)
_NEXT_MONDAY = date(2026, 9, 14)


# ── ① 실행일은 평일만 ──────────────────────────────────────────────────────


def test_한_주를_돌면_평일_다섯_날만_실행일이다():
    """🔴 **이 파일의 첫 주장이다.** 토·일에는 시장이 안 선다."""
    verdict = {day: is_execution_day(day) for day in _WEEK}

    assert [day.isoformat() for day, ok in verdict.items() if ok] == [
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
    ], f"평일 판정이 흔들린다: {verdict}"
    assert sum(verdict.values()) == 5
    assert not verdict[_SATURDAY] and not verdict[_SUNDAY]


# ── ② 다음 실행일 — **일수 차를 못 박는다** ─────────────────────────────────
#
# 🔴 여기가 "경과일수는 달력일" 을 고정하는 자리다. 날짜만 재고 일수를 안 재면,
#   `next_execution_day` 를 "하루 더하기" 로 바꿔도 초록불이 될 수 있다.


def test_목요일_다음은_하루_뒤_금요일():
    following = next_execution_day(_THURSDAY)

    assert following == _FRIDAY
    assert (following - _THURSDAY).days == 1


def test_금요일_다음은_사흘_뒤_월요일():
    """🔴 **3일이다. 1일이 아니다.**

    주말에 판단을 안 할 뿐 주말이 사라지지 않는다 — 재고는 토·일에도 늙는다.
    """
    following = next_execution_day(_FRIDAY)

    assert following == _NEXT_MONDAY
    assert (following - _FRIDAY).days == 3, (
        "금요일 다음 실행일까지를 1일로 세면 재고 나이와 지급일이 이틀씩 어긋난다"
    )


def test_토요일_다음은_이틀_뒤_월요일():
    following = next_execution_day(_SATURDAY)

    assert following == _NEXT_MONDAY
    assert (following - _SATURDAY).days == 2


def test_일요일_다음은_하루_뒤_월요일():
    following = next_execution_day(_SUNDAY)

    assert following == _NEXT_MONDAY
    assert (following - _SUNDAY).days == 1


def test_금토일_셋의_다음_실행일이_같은_월요일이다():
    """★ 세 날의 답이 갈리면 주말 중 하루가 실행일로 새어 들어간 것이다."""
    answers = {day: next_execution_day(day) for day in (_FRIDAY, _SATURDAY, _SUNDAY)}

    assert set(answers.values()) == {_NEXT_MONDAY}, f"답이 갈린다: {answers}"


def test_자기_자신은_세지_않는다():
    """평일에 물어도 그날이 아니라 **다음** 날이 나온다."""
    assert next_execution_day(_THURSDAY) != _THURSDAY
    assert next_execution_day(_FRIDAY) != _FRIDAY


# ── ③ 🔴 이 모듈은 경과일수를 세지 않는다 (원문 검사) ───────────────────────


def test_원문에_경과일수를_세는_코드가_없다():
    """🔴 **같은 사실의 주인이 둘이 되면 안 된다.**

    경과일수는 `app/master/verifier.py` 의 `_day_gap` 이 소유한다 (calendar day,
    영업일 보정 없음). 여기서 날짜 차를 세기 시작하면 언젠가 둘이 갈리고, 갈린 날
    아무도 어느 쪽이 맞는지 말해 주지 않는다.

    ★ **주석·docstring 은 세지 않는다.** 문서로 `_day_gap` 을 가리키는 것은
      권장이지 위반이 아니다 — `ast` 로 실행되는 코드만 본다.
    """
    tree = ast.parse(inspect.getsource(execution_day))

    subtractions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
    ]
    assert not subtractions, (
        "날짜를 빼고 있다 — 경과일수는 verifier._day_gap 이 소유한다"
    )

    day_counts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "days"
    ]
    assert not day_counts, "`.days` 로 일수를 세고 있다 — 이 모듈의 일이 아니다"


def test_경과일수의_주인을_문서가_가리킨다():
    """★ 주인이 어디인지 **읽는 사람에게** 적혀 있어야 한다."""
    doc = execution_day.__doc__ or ""

    assert "verifier.py" in doc and "_day_gap" in doc, (
        "경과일수의 주인을 안 가리킨다 — 다음 사람이 여기서 세기 시작한다"
    )
    assert "공휴일" in doc, "공휴일을 못 거른다는 한계가 안 적혀 있다"
    assert "constraints.yaml" in doc, "같은 한계가 이미 적힌 자리를 안 가리킨다"


# ── ④ 서비스 — 주말에는 Flow 를 시작하지 않는다 ────────────────────────────


def _port(payload: dict[str, Any] | None = None):
    from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload=payload or {"cap": 1},
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    return port


def _wire_all() -> list[str]:
    """세 부서를 다 등록하고, **불린 부서 이름**을 모으는 목록을 돌려준다."""
    from app.master import wiring
    from app.master.envelope import AgentRequest

    called: list[str] = []

    def watching(request: AgentRequest):
        called.append(request.agent)
        if request.agent == "purchase":
            return _port({"scenarios": [{"scenario_id": "SCN-1"}]})(request)
        return _port()(request)

    wiring.reset()
    wiring.register("finance", watching)
    wiring.register("inventory", watching)
    wiring.register("purchase", watching)
    return called


def _loaded_inputs():
    from app.master.inputs import MasterInputs, SourcedInput

    return MasterInputs(
        forecast=SourcedInput("forecast", {"horizon_days": 18}, "MEASURED", "뷰", ""),
        confirmed_orders=SourcedInput("confirmed_orders", {"total_kg": 1.0}, "DERIVED", "뷰", ""),
        policy_values=SourcedInput("policy_values", {"contract_price_krw": 1}, "DERIVED", "표", ""),
    )


@pytest.fixture
def 적재를_지켜본다(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    recorded: list[tuple[Any, Any]] = []

    def record(request, response, **kw):
        recorded.append((request, response))
        return "RUN-FAKE-1"

    monkeypatch.setattr("app.master.service.persistence.record", record)
    monkeypatch.setattr("app.master.service.collect_inputs", lambda *a, **k: _loaded_inputs())
    return recorded


def _run(as_of: date, request_id: str):
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    return run_procurement(
        ProcurementRunRequest(
            as_of=as_of, policy_version="v1.3", item="배추", request_id=request_id
        ),
        verifier=None,
    )


def test_주말_요청은_E4_로_접히고_안이_없다(적재를_지켜본다):
    """🔴 **주말은 오류가 아니라 안 도는 날이다** — 어댑터 미등록과 같은 태도다."""
    _wire_all()

    response = _run(_SATURDAY, "REQ-WEEKEND-1")

    assert response.end_code == "E4_NOT_STARTED"
    assert response.scenarios == [], "주말인데 안을 만들었다"


def test_주말_사유가_요일과_다음_실행일을_말한다(적재를_지켜본다):
    """★ 사람이 읽고 **"언제 다시 도나"** 를 알 수 있어야 한다."""
    _wire_all()

    reason = _run(_SUNDAY, "REQ-WEEKEND-2").reason

    assert "실행일이 아니다" in reason, f"왜 안 돌았는지 안 말한다: {reason}"
    assert "일)" in reason, f"그날의 요일이 없다: {reason}"
    assert _NEXT_MONDAY.isoformat() in reason, f"다음 실행일이 없다: {reason}"
    assert "월)" in reason, f"다음 실행일의 요일이 없다: {reason}"


def test_주말에는_부서를_한_번도_안_부른다(적재를_지켜본다):
    """★ 한 번이라도 부르면 그 회신이 이력에 남고 *"돌긴 돌았다"* 로 읽힌다."""
    called = _wire_all()

    _run(_SATURDAY, "REQ-WEEKEND-3")

    assert called == [], f"주말인데 부서를 불렀다: {called}"


def test_안_돈_날도_이력에_남는다(적재를_지켜본다):
    """🔴 **안 부른 것과 안 도는 날인 것은 다르다.** 이력이 비면 둘이 같아 보인다."""
    _wire_all()

    response = _run(_SATURDAY, "REQ-WEEKEND-4")

    assert len(적재를_지켜본다) == 1, "주말 실행이 이력에 안 남았다"
    assert 적재를_지켜본다[0][1].end_code == "E4_NOT_STARTED"
    assert response.history_run_id == "RUN-FAKE-1", "적재가 돌려준 행 id 를 응답에 안 실었다"


def test_주말에도_사람이_읽을_문장이_붙는다(적재를_지켜본다):
    """★ 빈 응답을 그대로 내보내면 화면이 침묵한다 — 어댑터 갈래와 같은 이유다."""
    _wire_all()

    response = _run(_SUNDAY, "REQ-WEEKEND-5")

    assert response.report_text, "주말 응답에 읽을 문장이 없다"


def test_주말_가드가_어댑터_검사보다_먼저다(적재를_지켜본다):
    """🔴 **순서가 뒤바뀌면 사유가 거짓말을 한다.**

    어댑터가 미등록인 채로 토요일에 부르면, 뒤에 있는 가드가 이기고 사유가
    *"어댑터 미등록"* 이 된다. 그러면 어댑터를 다 붙인 뒤에야 *"주말이었다"* 를
    알게 된다 — 안 도는 진짜 이유가 한 겹 뒤에 숨는다.
    """
    from app.master import wiring

    wiring.reset()  # 아무도 등록하지 않는다 — `wiring.missing()` 이 셋을 다 낸다
    assert wiring.missing(), "이 검사가 의미 있으려면 어댑터가 미등록이어야 한다"

    response = _run(_SATURDAY, "REQ-WEEKEND-6")

    assert "어댑터" not in response.reason, (
        f"주말인데 어댑터를 사유로 댄다 — 가드가 뒤에 있다: {response.reason}"
    )
    assert "실행일이 아니다" in response.reason
    assert response.missing_adapters == [], "주말 응답이 어댑터 문제를 지어냈다"
    assert response.skipped_checks and "어댑터" not in response.skipped_checks[0], (
        f"건너뛴 검사 사유가 어댑터라고 말한다: {response.skipped_checks}"
    )


# ── ⑤ 회귀 — 평일은 그대로 돈다 ────────────────────────────────────────────


def test_평일은_그대로_Flow_를_탄다(적재를_지켜본다):
    """★ 주말을 막느라 평일까지 막으면 아무 날도 안 돈다."""
    called = _wire_all()

    response = _run(_THURSDAY, "REQ-WEEKDAY-1")

    assert "purchase" in called, f"평일인데 매입을 안 불렀다: {called}"
    assert "실행일이 아니다" not in response.reason, (
        f"평일을 주말로 접었다: {response.reason}"
    )
