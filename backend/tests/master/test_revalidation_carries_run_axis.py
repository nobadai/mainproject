"""재검증이 **실행 축을 나른다** — 2026-09-11.

`revalidation.revalidate_scenario` 는 봉투의 축을 `BURN_IN_SIM_RUN_ID` 로 **박아**
두었다. 어느 실행을 재검증하든 언제나 번인 장부였다.

```text
전  sim_run_id=BURN_IN_SIM_RUN_ID      상수 — 원 실행이 무엇이든 같은 값
후  sim_run_id=sim_run_id              원 실행 행이 실은 축 (기본값 없음)
```

🔴 **실측된 피해** (실행 `SIM-SALESCHAIN-20260911`).

```text
재무가 채권·현금을 번인 장부에서 읽었다
  → 판매를 한 번도 안 한 실행의 재검증이
    SALES_CREDIT_LIMIT_EXCEEDED · BASE_MINIMUM_CASH_VIOLATED 로 떨어졌다
  → 승인 일곱 건이 전부 FAILED 이고 sales 는 0행
  → 번인 채권 합과 재검증이 본 AR 이 소수점까지 같았다 (15,752,100.13535)
```

그리고 업무 키 `REV-20260106-0001` 하나에 **여러 실행의 행 20건**이 쌓여 있었다 —
키가 축을 안 나르면 *"어느 실행의 재검증인가"* 를 아무도 못 푼다.

🔴 **이 파일이 잡으려는 넷.**

```text
① 봉투의 축이 그 실행이다        두 다른 실행으로 부르면 각각 그 실행이어야 한다
② 업무 키가 축을 나른다          같은 날 같은 회차라도 실행이 다르면 키가 다르다
③ 번인이라는 이름이 안 남는다    revalidation.py 에 BURN_IN_SIM_RUN_ID 가 한 번도 없다
④ 관문도 같은 축을 받는다        check_day_gate 호출이 전부 sim_run_id 를 넘긴다
```

★★ **③④ 는 AST 로 센다. 그리고 「0건을 세면 그것도 막는다」** — 파일을 못 읽거나
  구조가 바뀌어 센 것이 0이면 그 검사는 아무것도 안 재면서 초록이 된다. 두 검사
  모두 **센 총수가 0이면 실패**시킨다 (`#320` 의 변이가 안 울었던 모양이 정확히
  그것이었다).

★ **DB 를 치지 않는다.** 부서는 대역으로 등록하고, 개장 관문은 `conftest.py` 가
  이미 통과시킨다.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import unicodedata
from datetime import date
from typing import Any

import pytest

from app.master import revalidation, wiring
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata

#: 재검증이 서는 날. 두 실행이 **같은 날 같은 회차**로 돌아야 ② 가 축만 잰다.
고른_날 = date(2026, 1, 6)

#: 🔴 **둘 다 번인이 아니다.** 번인으로 떨어지는 길이 남아 있으면 그 자리에서 값이
#:   갈린다 — 둘 중 하나라도 번인이면 이 검사가 그 길을 못 본다.
실행_가 = "SIM-WALK-2026-V4"
실행_나 = "SIM-SALESCHAIN-20260911"

#: 검사 대상 파일. 🔴 **`__file__` 에서 얻는다** — 경로를 손으로 적으면 파일이
#:   옮겨간 날 `FileNotFoundError` 가 아니라 조용한 빈 통과가 될 길이 생긴다.
_대상 = pathlib.Path(revalidation.__file__)


class 부서:
    """등록된 어댑터 대역. **어떤 축으로 물었는지만 남긴다.**"""

    def __init__(self) -> None:
        self.호출: list[tuple[str, str]] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.호출.append((request.context.sim_run_id, request.context.request_id))
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
        "as_of": 고른_날,
    }
    base.update(kw)
    return revalidation.revalidate_scenario(**base)


def _파싱() -> ast.Module:
    """대상 파일의 AST. 🔴 **못 읽으면 그 자리에서 터진다** — 빈 통과를 안 만든다."""
    return ast.parse(_대상.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ① 봉투의 축이 그 실행이다
# ---------------------------------------------------------------------------


def test_두_다른_실행이_각각_자기_축으로_돈다(부서들):
    """🔴 **이것이 버그의 본체다.**

    전에는 두 번 다 `BURN_IN` 이 나왔다. 그래서 판매를 한 번도 안 한 실행의 재무
    검증이 **번인 장부의 채권**을 읽고 한도 초과로 떨어뜨렸다.

    ★ **두 번 부른다.** 한 번만 부르면 *"넘긴 값이 실렸다"* 와 *"상수가 마침 그
      값이었다"* 를 못 가른다.
    """
    _재검증(sim_run_id=실행_가)
    _재검증(sim_run_id=실행_나)

    잰_축 = [축 for 부 in 부서들.values() for (축, _rid) in 부.호출]

    assert 잰_축, "부서를 한 번도 안 불렀다 — 이 검사가 아무것도 안 재고 있다"
    assert set(잰_축) == {실행_가, 실행_나}, f"넘긴 축으로 안 물었다: {sorted(set(잰_축))}"
    assert 잰_축.count(실행_가) == 잰_축.count(실행_나) == len(부서들), (
        f"두 실행이 같은 횟수로 안 돌았다: {잰_축}"
    )


def test_축은_기본값_없는_키워드다():
    """★ `as_of` 와 **같은 규율이다** — 기본값은 곧 업무 규칙이 된다.

    🔴 기본값이 생기면 안 넘긴 자리가 조용히 그 값(번인)으로 돌고, 아무 데도 안
      적힌다. 안 넘기면 **터져야** 그 자리를 그날 안다.
    """
    param = inspect.signature(revalidation.revalidate_scenario).parameters["sim_run_id"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"sim_run_id 가 키워드 전용이 아니다: {param}"
    )
    assert param.default is inspect.Parameter.empty, (
        f"sim_run_id 에 기본값이 생겼다({param.default!r}) — 기본값은 곧 업무 규칙이다"
    )


def test_축_없이_부르면_터진다(부서들):
    """★ 위 검사의 짝 — 서명뿐 아니라 **실제로 안 도는지**까지 본다."""
    with pytest.raises(TypeError):
        _재검증()

    assert not 부서들["finance"].호출, "축 없이도 부서를 불렀다"


def test_못_읽은_축은_메우지_않고_ERROR_다(부서들):
    """🔴 **`or BURN_IN_SIM_RUN_ID` 로 메우는 길을 막는다.**

    원 실행 행이 축을 안 실었으면 *"모른다"* 다. 번인으로 메우면 축이 안 실린 옛
    실행의 승인이 조용히 남의 장부에 앉는다 — 그것이 지금 고치는 바로 그 병이다.

    ★ 어휘는 `as_of` · `policy_version` 과 **같은 모양**이다 (같은 함수 안 바로 위).

    ★★ **부서를 등록해 두고 잰다.** 안 등록하면 메우는 변이를 넣어도 *"어댑터가
      없다"* 로 `ERROR` 가 나서 `outcome` 만으로는 못 가른다 — 등록해 두면 메우는
      순간 `PASSED` 가 되어 그 자리에서 빨개진다.
    """
    from app.master.decision import DecisionIn
    from app.master.decision_service import _revalidation_for

    응답 = {
        "end_code": "E1_APPROVED",
        "as_of": 고른_날.isoformat(),
        "scenarios": [{"label": "기본"}],
        "adjustments": [],
    }
    결과 = _revalidation_for(
        {"request_payload": {"policy_version": "v1.3"}},
        응답,
        DecisionIn(decision="APPROVE", scenario_label="기본", decided_by="이현서"),
        1,
        # 🔴 축을 못 읽은 것 — `record_decision` 의 `_sim_run_id_of(row)` 가 낸 `None` 이다.
        sim_run_id=None,
    )

    assert 결과 is not None
    assert 결과.outcome == "ERROR", f"축을 못 읽었는데 {결과.outcome} 로 돌았다"
    assert not [부.호출 for 부 in 부서들.values() if 부.호출], (
        "축을 못 읽었는데 부서를 불렀다 — 어느 실행의 장부로 물었는지 아무도 모른다"
    )
    assert unicodedata.normalize("NFC", 결과.reason) == unicodedata.normalize(
        "NFC", "원 실행의 sim_run_id 를 못 읽어 재검증 봉투를 만들 수 없다."
    ), f"사유 문장이 다르다: {결과.reason}"


# ---------------------------------------------------------------------------
# ② 업무 키가 축을 나른다
# ---------------------------------------------------------------------------


def test_같은_날_같은_회차라도_실행이_다르면_키가_다르다(부서들):
    """🔴 **실측에서 `REV-20260106-0001` 한 키에 여러 실행의 행 20건이 앉아 있었다.**

    ★ 날짜와 회차를 **고정**하고 축만 바꾼다 — 키가 갈리는 이유가 축임을 못 박는다.
    """
    가 = _재검증(sim_run_id=실행_가)
    나 = _재검증(sim_run_id=실행_나)

    assert 가.request_id == f"REV-{실행_가}-20260106-0001"
    assert 나.request_id == f"REV-{실행_나}-20260106-0001"
    assert 가.request_id != 나.request_id, "실행이 달라도 업무 키가 같다"

    쓴_키 = {rid for 부 in 부서들.values() for (_축, rid) in 부.호출}
    assert 쓴_키 == {가.request_id, 나.request_id}, f"부서가 받은 키가 다르다: {쓴_키}"


def test_키_만드는_함수가_축을_첫_위치_인자로_받는다():
    """★ **인자가 하나 늘면 옛 호출부가 조용히 통과하지 않고 터진다.**

    🔴 축을 뒤에 기본값으로 붙였다면 옛 두 인자 호출이 그대로 살고, 그 자리는
      여전히 축 없는 키를 짓는다 — 그것이 못 잡는 모양이다.
    """
    params = list(inspect.signature(revalidation.make_revalidation_request_id).parameters)

    assert params[0] == "sim_run_id", f"축이 첫 인자가 아니다: {params}"
    assert params == ["sim_run_id", "as_of", "decision_seq"], (
        f"`{{머리}}-{{실행}}-{{날짜}}-{{꼬리}}` 차례가 아니다: {params}"
    )
    assert (
        revalidation.make_revalidation_request_id(실행_가, 고른_날, 1)
        == f"REV-{실행_가}-20260106-0001"
    )


# ---------------------------------------------------------------------------
# ③ AST — 번인이라는 이름이 그 파일에 안 남는다
# ---------------------------------------------------------------------------


def test_재검증_모듈에_번인이라는_이름이_없다():
    """🔴 **이름이 남아 있으면 되돌아갈 길이 남아 있는 것이다.**

    문자열 `in` 으로 세면 주석·문서화 문자열까지 걸려 *"왜 번인을 쓰면 안 되는가"*
    를 적을 수가 없다. 그래서 **AST 로 실제 이름 참조만** 센다.

    ★★ **0건을 세면 그것도 막는다.** 파일을 못 읽거나 구조가 바뀌어 `Name` /
      `Attribute` 마디가 하나도 안 잡히면 이 검사는 아무것도 안 재면서 초록이 된다.
    """
    나무 = _파싱()

    센_마디 = 0
    걸린것: list[int] = []
    for 마디 in ast.walk(나무):
        if isinstance(마디, ast.Name):
            센_마디 += 1
            if 마디.id == "BURN_IN_SIM_RUN_ID":
                걸린것.append(마디.lineno)
        elif isinstance(마디, ast.Attribute):
            센_마디 += 1
            if 마디.attr == "BURN_IN_SIM_RUN_ID":
                걸린것.append(마디.lineno)

    assert 센_마디 > 0, f"{_대상.name} 에서 이름 마디를 하나도 못 셌다 — 검사가 헛돌고 있다"
    assert not 걸린것, (
        f"{_대상.name} 이 아직 번인 축을 이름으로 들고 있다 (줄 {걸린것}) — "
        f"축은 원 실행 행에서 온다"
    )


# ---------------------------------------------------------------------------
# ④ AST — 관문 호출이 전부 축을 넘긴다
# ---------------------------------------------------------------------------


def test_재검증_모듈의_관문_호출이_전부_축을_넘긴다():
    """🔴 **봉투와 관문이 다른 실행을 보면 한 함수 안에서 축이 갈린다.**

    이 자리는 저장소의 관문 호출부 **일곱 중 유일하게** 축을 안 넘기던 곳이었다.

    ★★ **0건을 세면 그것도 막는다.** 호출을 하나도 못 찾으면 아래 `not 샌것` 은
      공짜로 참이다.
    """
    나무 = _파싱()

    센_호출 = 0
    샌것: list[int] = []
    for 마디 in ast.walk(나무):
        if not isinstance(마디, ast.Call):
            continue
        이름 = (
            마디.func.attr
            if isinstance(마디.func, ast.Attribute)
            else getattr(마디.func, "id", None)
        )
        if 이름 != "check_day_gate":
            continue
        센_호출 += 1
        if not any(kw.arg == "sim_run_id" for kw in 마디.keywords):
            샌것.append(마디.lineno)

    assert 센_호출 >= 1, (
        f"{_대상.name} 에서 check_day_gate 호출을 한 건도 못 찾았다 — 검사가 헛돌고 있다"
    )
    assert not 샌것, f"관문을 부르면서 축을 안 넘기는 자리가 있다 (줄 {샌것})"
