"""재검증의 `as_of` 는 **넘겨받는 값이다** — `#452` (2026-09-09).

`revalidation.revalidate_scenario` 는 그 자리에서 `today()` 로 벽시계를 읽었다.
부르는 곳이 승인 경로 하나뿐이라 아무 검사도 안 울었고, 걷기가 아무도 안 골랐으므로
아직 안 터졌다. 🔴 **걷기가 정책에 따라 안을 고르기 시작하면 그날 터진다** —
`2026-03-10` 을 걷는 실행에서 재검증만 오늘로 답하고, 곡선에 벽시계가 섞인다.

```text
운영     사람이 고르는 날 = 오늘        router.master_decide 가 clock 을 읽어 넘긴다
         말로 고르는 날                 ask_service 가 그 요청의 as_of 를 넘긴다
백테스트  정책이 고르는 날 = 걷는 그날    walk 가 그날을 넘긴다
```

⚠️ **옛 주석의 걱정을 지운 것이 아니다.**

```text
옛 주석  날짜가 인자가 되면 부르는 쪽이 원 실행의 날을 넣을 수 있고,
        그 순간 재검증이 아무것도 안 재게 된다
지금    막는 자리를 옮겼다 — 벽시계는 진입점 하나에만 있고,
        깊은 자리에서는 아무도 날짜를 지어내지 못한다
```

🔴 **이 파일이 잡으려는 넷.**

```text
① 기본값이 생긴다        안 넘겨도 도는 순간 그 기본값이 업무 규칙이 된다
② 날짜가 안 흐른다       넘긴 값이 봉투·업무 키에 안 실리면 넘긴 뜻이 없다
③ 진입점이 시계를 안 읽는다  운영 승인이 엉뚱한 날로 돈다
④ 발화문 경로가 갈린다    화면 승인과 말 승인이 다른 날로 돌면 안 된다
```

★ **DB 를 치지 않는다.** 부서는 대역으로 등록하고, 개장 관문은 `conftest.py` 가
  이미 통과시킨다.
"""

from __future__ import annotations

import inspect
from datetime import date
from typing import Any

import pytest

from app.master import ask_service, revalidation, router, wiring
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata

#: 이 검사가 고르는 "고른 날". 🔴 **오늘일 리 없는 값으로 둔다** — 벽시계가 어딘가에
#: 남아 있으면 그 자리에서 값이 갈린다.
고른_날 = date(2026, 3, 10)


class 부서:
    """등록된 어댑터 대역. **어떤 as_of 와 업무 키로 물었는지만 남긴다.**"""

    def __init__(self) -> None:
        self.호출: list[tuple[date, str]] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.호출.append((request.context.as_of, request.context.request_id))
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status="ok",
            reasoning="대역",
        )
        return reply, ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("tool_a",),
            tool_order=(1,),
        )


@pytest.fixture
def 부서들() -> dict[str, 부서]:
    """필수 capability 둘이 라우팅되는 물류·재무를 등록한다."""
    wiring.reset()  # 루트 conftest 가 스냅샷을 떠 두므로 이 파일 밖으로 안 샌다
    등록 = {"inventory": 부서(), "finance": 부서()}
    for 이름, 포트 in 등록.items():
        wiring.register(이름, 포트)
    return 등록


def _재검증(**kw: Any) -> revalidation.Revalidation:
    base: dict[str, Any] = {
        "scenario": {"label": "기본"},
        "original_conditions": frozenset(),
        "decision_seq": 1,
        "policy_version": "v1.3-PROVISIONAL",
    }
    base.update(kw)
    return revalidation.revalidate_scenario(**base)


# ---------------------------------------------------------------------------
# ① 기본값이 없다 — 안 넘기면 터져야 한다
# ---------------------------------------------------------------------------


def test_as_of_없이_부르면_터진다(부서들):
    """🔴 **기본값이 곧 업무 규칙이 된다.**

    기본값이 생기면 안 넘긴 자리가 조용히 그 값으로 돌고, 아무 데도 안 적힌다.
    안 넘기면 **터져야** 그 자리를 그날 안다.
    """
    with pytest.raises(TypeError):
        _재검증()

    assert not 부서들["finance"].호출, "as_of 없이도 부서를 불렀다"


@pytest.mark.parametrize(
    "함수",
    #: 🔴 **`record_decision` 은 여기 없다** (2026-09-09). 그 함수는 as_of 를 **안 받고**
    #: 실행 이력 행에서 읽는다 — 받으면 부르는 쪽이 아무 날이나 넣을 수 있다.
    [revalidation.revalidate_scenario],
    ids=["revalidate_scenario"],
)
def test_as_of_는_기본값_없는_키워드다(함수):
    """★ 위 검사의 짝 — **왜 터지는지**까지 못 박는다.

    `TypeError` 만 재면 인자 이름이 바뀌어도 초록이다. 여기서는 그 이름이
    `as_of` 이고, **키워드 전용이며, 기본값이 없다**는 셋을 다 본다.

    ⚠️ **위치 인자로 두지 않는다.** 날짜 하나가 위치로 들어가면 부르는 쪽에서
      `decision_seq` 와 자리를 바꿔 넣어도 안 터진다.
    """
    param = inspect.signature(함수).parameters["as_of"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"as_of 가 키워드 전용이 아니다: {param}"
    assert param.default is inspect.Parameter.empty, (
        f"as_of 에 기본값이 생겼다({param.default!r}) — 기본값은 곧 업무 규칙이다"
    )


# ---------------------------------------------------------------------------
# ② 넘긴 날이 봉투와 업무 키에 실린다
# ---------------------------------------------------------------------------


def test_넘긴_날로_부서를_부른다(부서들):
    """🔴 **받아 놓고 안 쓰면 넘긴 뜻이 없다.**

    봉투(`ExecutionContext.as_of`)가 부서로 나가는 값이므로, 그 값이 넘긴 날인지를
    **부서가 받은 자국**으로 잰다.
    """
    _재검증(as_of=고른_날)

    잰_날 = {as_of for 부 in 부서들.values() for (as_of, _rid) in 부.호출}
    assert 잰_날 == {고른_날}, f"넘긴 날({고른_날})로 안 물었다: {잰_날}"


def test_넘긴_날이_재검증_업무_키를_만든다(부서들):
    """★ `make_revalidation_request_id` 가 **그 날짜로** 키를 만든다.

    🔴 키가 다른 날짜로 서면 이력에서 *"언제 재검증했나"* 가 봉투와 갈린다 — 같은
      사실이 두 곳에 다르게 적히는 것이다.
    """
    결과 = _재검증(as_of=고른_날)

    assert 결과.request_id == "REV-20260310-0001"
    assert 결과.request_id == revalidation.make_revalidation_request_id(고른_날, 1)

    쓴_키 = {rid for 부 in 부서들.values() for (_as_of, rid) in 부.호출}
    assert 쓴_키 == {결과.request_id}, "부서가 받은 업무 키가 결과의 키와 다르다"


# ---------------------------------------------------------------------------
# ③④ 진입점이 그 날을 정한다
# ---------------------------------------------------------------------------


def test_승인_라우터는_날짜를_안_정한다(monkeypatch):
    """🔴 **진입점이 재검증할 날을 고르지 않는다** (2026-09-09).

    ⚠️ **이 검사는 뜻이 뒤집혔다.** 전에는 *"라우터가 clock 을 읽어 넘긴다"* 를 지켰다.
      그런데 실측에서 그 구조가 **승인을 막았다** — 재검증의 첫 관문이 개장이고,
      화면이 오늘 누르면 **오늘은 안 열린 날**이라 `ERROR` 가 난다.

    ★ 그래서 그 날은 **실행 이력 행**이 정한다. 라우터는 시계를 아예 안 든다.
    """
    assert not hasattr(router, "today_in_seoul"), (
        "라우터가 다시 시계를 들었다 — 재검증할 날은 실행 이력 행이 정한다"
    )

    받은: list[Any] = []
    monkeypatch.setattr(
        router, "record_decision", lambda request_id, body, **kw: 받은.append(kw) or "OK"
    )

    router.master_decide("REQ-20260901-0001", object())  # type: ignore[arg-type]

    assert 받은 == [{}], f"진입점이 날짜를 넘겼다: {받은}"


def test_발화문_승인도_날짜를_안_넘긴다(monkeypatch):
    """🔴 **말로 한 승인과 화면에서 누른 승인이 다른 날로 돌면 안 된다.**

    ★ 발화문 요청에는 `as_of` 가 실려 있지만 **그것도 안 넘긴다.** 재검증이 설 날의
      주인은 실행 이력 행 하나다 — 요청이 말한 날과 실행 행의 날이 갈릴 수 있고,
      그때 어느 쪽이 맞는지를 부르는 쪽이 정하게 되기 때문이다.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.master.ask_schemas import AskExecuteRequest
    from app.master.decision import DecisionOut
    from app.master.llm.schemas import Intent

    받은: list[Any] = []

    def _대역(request_id: str, body: Any, **kw: Any) -> DecisionOut:
        받은.append(kw)
        return DecisionOut(
            decision_id=uuid4(),
            request_id=request_id,
            decision_seq=1,
            decision=body.decision,
            scenario_label=body.scenario_label,
            decided_by=body.decided_by,
            end_code_at_decision="E1_APPROVED",
            revalidation_outcome="PASSED",
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(ask_service, "record_decision", _대역)

    ask_service._record_selection(
        AskExecuteRequest(
            intent=Intent(action="SELECT_SCENARIO", scenario_label="기본", confidence="HIGH"),
            as_of=고른_날,
            policy_version="v1.3-PROVISIONAL",
            target_request_id="REQ-20260901-0001",
            decided_by="이현서",
        )
    )

    assert 받은 == [{}], f"발화문 경로가 날짜를 넘겼다: {받은}"
