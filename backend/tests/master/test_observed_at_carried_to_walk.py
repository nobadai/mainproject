"""봉투가 **관측 기준시점**을 나르고, 걷기가 **몇 건이 아직 안 쟀는지** 세는가 — 2026-09-12.

`observed_at` 은 *"이 사실을 **언제부터 알 수 있었나**"* 다. DB 에 언제 적혔나
(`created_at`)와 다르다 — 9월 1일에 일어난 일을 9월 10일에 적재했으면
`created_at` 은 9월 10일이고 `observed_at` 은 9월 1일이다.

```text
이름     observed_at
타입     date | None      🔴 datetime 이 아니다 — ExecutionContext.as_of 와 같아야
                            비교 규칙이 하나다
기본값   None
파생값   입력들의 observed_at 중 **가장 늦은 것** · 하나라도 미지면 None
None     🟡 「안 쟀다」이고 「미래를 봤다」와 **다른 사실**이다
```

🔴 **이 판은 칸을 여는 데까지다.** `observed_at > as_of` 를 막는 검사는 여기 없다 —
  아무도 안 채운 상태에서 걸면 전부 막힌다.

🔴 **이 파일이 잡으려는 다섯.**

```text
㉠ 마스터가 None 을 as_of 로 안 메운다     ← 제일 중요하다. 메우면 안 잰 것이 잰 것이 된다
㉡ 타입이 date 다                          datetime 으로 넓히면 비교 규칙이 둘로 갈린다
㉢ 요약이 0 인 칸도 찍는다                  「실었다」0 이 늘어나는 것이 이 일의 진도다
㉣ AgentReply 에서 읽는다                   ExecutionMetadata 는 실행 흔적이지 업무 결과가 아니다
㉤ 요약 줄 자체가 선다                      값이 있는데 성적표가 안 읽으면 없는 것과 같다
```

★ **DB 를 안 탄다.** 봉투를 손으로 세워 `ExecutionPlan.record` 에 넣고, 걷기 결과는
  `WalkResult` 를 직접 만든다.
"""

from __future__ import annotations

import ast
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.master import backtest_runner
from app.master.backtest_runner import WalkResult, format_summary
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.plan import ExecutionPlan
from app.master.scheduler import DayRunOutcome, ItemRunOutcome, _observed_ats
from app.master.service import _steps

AS_OF = date(2026, 9, 12)

#: 부서가 실제로 실어 보낸 관측 기준시점. **`AS_OF` 와 다른 날이어야** 마스터가
#: `as_of` 를 베껴 넣었는지 아닌지가 갈린다.
관측일 = date(2026, 9, 1)

#: 실측이 말한 그 분포. 「실었다」가 0 이라는 사실이 이 판의 출발점이다.
실측_안쟀다 = 519


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------------------
# ㉠㉣ 봉투 → 실행 계획 — **마스터가 값을 지어내지 않는다**
# ---------------------------------------------------------------------------


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20260912-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _request() -> AgentRequest:
    return AgentRequest(context=_ctx(), agent="finance", mode="PRE_PURCHASE")


def _reply(observed_at: date | None) -> AgentReply:
    return AgentReply(
        request_id="REQ-20260912-0001",
        as_of=AS_OF,
        agent="finance",
        mode="PRE_PURCHASE",
        run_id="FIN-1",
        runtime_status="READY",
        business_status="pass",
        observed_at=observed_at,
    )


def _metadata() -> ExecutionMetadata:
    return ExecutionMetadata(run_id="FIN-1", request_id="REQ-20260912-0001", agent="finance")


@dataclass(frozen=True)
class _관측을_든_실행흔적:
    """`ExecutionMetadata` 대역. **업무 결과에 없는 값을 흔적 쪽에 심어 둔다.**

    🔴 여기서 읽으면 *"언제부터 알 수 있었나"* 가 *"언제 돌렸나"* 로 바뀐다 —
      `observed_at` 은 사실의 성질이지 실행의 흔적이 아니다. 흔적에서 끌어오는
      변이가 이 대역 때문에 **에러가 아니라 틀린 값**으로 드러난다.
    """

    run_id: str = "FIN-1"
    request_id: str = "REQ-20260912-0001"
    agent: str = "finance"
    used_tools: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    replans: int = 0
    llm_status: str = "DISABLED"
    llm_model: str = ""
    llm_attempts: int = 0
    llm_fallback_used: bool = False
    #: 흔적 쪽에만 있는 값. **업무 결과의 `None` 을 이걸로 메우면 안 된다.**
    observed_at: date = date(2026, 9, 30)


def _plan() -> ExecutionPlan:
    return ExecutionPlan(request_id="REQ-20260912-0001", as_of=AS_OF)


def test_부서가_실은_관측시점이_실행계획까지_그대로_간다() -> None:
    """★ 나르기만 한다. 자르지도 미루지도 않는다."""
    step = _plan().record(_request(), _reply(관측일), _metadata())

    assert step.observed_at == 관측일


def test_안_실은_부서는_None_그대로_남는다() -> None:
    """🔴 **이것이 이 파일의 제일 중요한 단언이다.**

    ★★ `ExecutionPlan.as_of` 가 바로 옆에 있어서 메우기 쉬운 자리다. 메우는 순간
      **「안 쟀다」가 「쟀다」로 세어지고**, 걷기 요약은 첫날부터 「실었다」가 만
      건이라고 말한다 — 그 숫자는 한 건도 사실이 아니다.
    """
    plan = _plan()
    step = plan.record(_request(), _reply(None), _metadata())

    assert step.observed_at is None, "마스터가 None 을 무언가로 메웠다"
    assert step.observed_at != plan.as_of, "as_of 를 베껴 넣었다 — 안 잰 것이 잰 것이 됐다"
    assert step.observed_at != date.today(), "오늘 날짜로 메웠다"


def test_실행흔적에_관측시점이_있어도_업무결과의_None_을_안_메운다() -> None:
    """🔴 **출처는 `AgentReply` 다** — `llm_status` 와 길은 같고 출처가 다르다.

    저쪽은 `ExecutionMetadata`(실행 흔적)에서 오고 이쪽은 **업무 결과**에서 온다.
    """
    step = _plan().record(_request(), _reply(None), _관측을_든_실행흔적())

    assert step.observed_at is None, "실행 흔적에서 끌어왔다 — 출처가 틀렸다"


def test_실행흔적의_관측시점이_업무결과를_덮지_않는다() -> None:
    """★ 둘 다 값이 있을 때 어느 쪽을 따르는지까지 못 박는다."""
    step = _plan().record(_request(), _reply(관측일), _관측을_든_실행흔적())

    assert step.observed_at == 관측일


def test_응답_스키마까지_None_이_None_으로_나간다() -> None:
    """★ 화면이 메우면 **아직 안 실은 부서가 실은 부서처럼 보인다.**"""
    plan = _plan()
    plan.record(_request(), _reply(None), _metadata())
    plan.record(_request(), _reply(관측일), _metadata())

    나간것 = [one.observed_at for one in _steps(plan)]

    assert 나간것 == [None, 관측일]


# ---------------------------------------------------------------------------
# ㉡ 타입 — **`date` 다.** `ExecutionContext.as_of` 와 같아야 비교 규칙이 하나다
# ---------------------------------------------------------------------------


_봉투파일 = Path(backtest_runner.__file__).with_name("envelope.py")


def _필드주석(source: str, 클래스: str, 필드: str) -> str:
    """그 클래스가 그 필드에 적어 둔 **타입 주석의 원문**."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == 클래스:
            for one in node.body:
                if (
                    isinstance(one, ast.AnnAssign)
                    and isinstance(one.target, ast.Name)
                    and one.target.id == 필드
                ):
                    return ast.unparse(one.annotation)
    raise AssertionError(f"{클래스}.{필드} 를 못 찾았다")


def test_관측시점은_date_다() -> None:
    """🔴 **`datetime` 으로 넓히면 비교 규칙이 둘로 갈린다.**

    한쪽은 날짜로 자르고 한쪽은 시각으로 잘라서, 같은 사실이 어느 자리를
    지나느냐에 따라 룩어헤드가 됐다 안 됐다 한다.
    """
    source = _봉투파일.read_text(encoding="utf-8")

    assert _필드주석(source, "AgentReply", "observed_at") == "date | None"


def test_기준시점과_같은_타입이다() -> None:
    """★ 비교할 두 값의 타입이 같은지를 한 줄로 붙잡는다."""
    source = _봉투파일.read_text(encoding="utf-8")
    기준 = _필드주석(source, "ExecutionContext", "as_of")
    관측 = _필드주석(source, "AgentReply", "observed_at")

    assert 관측 == f"{기준} | None", f"as_of 는 {기준} 인데 observed_at 은 {관측} 이다"


def test_기본값은_None_이다() -> None:
    """★ **선택 칸이다.** 부서가 아직 안 실어도 봉투가 선다 — 검사는 이 판에 없다."""
    reply = AgentReply(
        request_id="REQ-20260912-0001",
        as_of=AS_OF,
        agent="finance",
        mode="PRE_PURCHASE",
        run_id="FIN-1",
        runtime_status="READY",
        business_status="pass",
    )

    assert reply.observed_at is None


def test_미래_관측시점도_봉투가_안_막는다() -> None:
    """🔴 **이 판은 칸을 여는 데까지다.**

    아무도 안 채운 상태에서 `observed_at > as_of` 를 막으면 전부 막힌다 —
    막는 일은 부서가 싣기 시작한 뒤의 판이다.
    """
    미래 = _reply(date(2026, 12, 31))

    assert 미래.observed_at == date(2026, 12, 31)


# ---------------------------------------------------------------------------
# 걷기가 값을 손에 쥐는가 — **`None` 을 걸러 내지 않는다**
# ---------------------------------------------------------------------------


class _계획단계:
    """부서 응답의 실행 계획 한 걸음 대역. **걷기가 읽는 칸만 든다.**"""

    def __init__(self, observed_at: date | None) -> None:
        self.observed_at = observed_at


class _응답:
    """`run_procurement` 응답 대역. `plan` 이 실려 오는 그 모양이다."""

    def __init__(self, *관측들: date | None) -> None:
        self.end_code = "E1_PRESENTED"
        self.plan = [_계획단계(one) for one in 관측들]


def test_None_을_걸러_내지_않는다() -> None:
    """🔴 **여기서 버리면 「몇 건이 아직 안 쟀는지」가 통째로 사라진다.**

    안 실은 호출이 집계에서 빠지고, 걷기는 **실은 것만 세어 100% 라고 말한다.**
    """
    assert _observed_ats(_응답(관측일, None, 관측일)) == (관측일, None, 관측일)


def test_계획이_없는_응답은_빈_튜플이다() -> None:
    """⚠️ *"안 쟀다"* 가 아니라 **"셀 것이 없었다"** 다.

    빈 자리를 `None` 한 개로 채우면 **안 돈 품목이 안 잰 품목으로** 세어진다.
    """

    class _계획없음:
        end_code = "E4_NOT_STARTED"

    assert _observed_ats(_계획없음()) == ()
    assert _observed_ats(None) == ()


def test_칸_자체가_없는_옛_단계는_안_쟀다로_읽힌다() -> None:
    """★ 부서가 아직 안 실은 지금이 바로 이 모양이다."""

    class _옛단계:
        llm_status = "DISABLED"

    class _옛응답:
        plan = [_옛단계()]

    assert _observed_ats(_옛응답()) == (None,)


def test_못_돈_품목은_관측시점을_안_나른다() -> None:
    """★ 응답이 없으면 계획도 없다 — 그 자리는 `status` 가 `FAILED` 라고 말한다."""
    터진것 = ItemRunOutcome(item="배추", request_id="REQ-1", status="FAILED", reason="boom")

    assert 터진것.observed_ats == ()


# ---------------------------------------------------------------------------
# ㉢㉤ 걷기 요약 — **0 이어도 찍는다**
# ---------------------------------------------------------------------------


def _품목(*관측들: date | None, item: str = "배추") -> ItemRunOutcome:
    return ItemRunOutcome(
        item=item,
        request_id=f"REQ-{item}",
        status="RAN",
        end_code="E1_PRESENTED",
        observed_ats=tuple(관측들),
    )


def _하루(*품목들: ItemRunOutcome) -> DayRunOutcome:
    return DayRunOutcome(as_of=AS_OF, action="RUN_NOW", reason="", items=tuple(품목들))


def _걷기(*날들: DayRunOutcome) -> WalkResult:
    return WalkResult(start=AS_OF, end=AS_OF, days=tuple(날들))


def test_실었다와_안쟀다가_각자_세어진다() -> None:
    """⚠️ 접으면 *"안 쟀다"* 가 *"쟀다"* 에 섞인다 — 다음에 할 일이 다르다."""
    센것 = dict(_걷기(_하루(_품목(관측일, None, None))).observation_coverage)

    assert 센것 == {"실었다": 1, "안쟀다": 2}


def test_매입과_판매를_합계_한_줄로_센다() -> None:
    """★ 지금 필요한 것은 *"쟀나 안 쟀나"* 다 — 부서별로 가르면 줄이 넷이 된다."""
    하루 = DayRunOutcome(
        as_of=AS_OF,
        action="RUN_NOW",
        reason="",
        items=(_품목(None),),
        sales_items=(_품목(관측일, item="무"),),
    )

    assert dict(_걷기(하루).observation_coverage) == {"실었다": 1, "안쟀다": 1}


def test_실었다가_0_이어도_요약에_0_으로_찍힌다() -> None:
    """🔴 **「0이라 안 보임」을 막는다** — `LLM어휘` 와 같은 규율이다.

    ★★ 처음에는 「실었다」가 0 이고, **그 숫자가 늘어나는 것이 이 일의 진도**다.
      0 이라 빼면 재무·물류가 연결한 날에도 성적표가 아무 말을 안 한다.
    """
    결과 = _걷기(_하루(_품목(*([None] * 실측_안쟀다))))

    센것 = dict(결과.observation_coverage)
    요약 = _NFC(format_summary(결과))

    assert 센것 == {"실었다": 0, "안쟀다": 실측_안쟀다}
    assert _NFC("'실었다': 0") in 요약, (
        f"요약에서 「실었다」 0 이 사라졌다 — 그것이 이 판이 막으려는 실패다: {요약}"
    )
    assert _NFC(f"'안쟀다': {실측_안쟀다}") in 요약


def test_한_번도_안_돈_걷기도_두_칸이_다_보인다() -> None:
    """★ 빈 걷기의 `{}` 는 *"다 쟀다"* 와 화면에서 같아 보인다."""
    센것 = dict(_걷기().observation_coverage)

    assert 센것 == {"실었다": 0, "안쟀다": 0}
    assert _NFC("'실었다': 0") in _NFC(format_summary(_걷기()))


def test_요약에_관측시점_줄_자체가_선다() -> None:
    """★★ 값이 있는데 성적표가 안 읽으면 **없는 것과 같다.**"""
    요약 = _NFC(format_summary(_걷기(_하루(_품목(None)))))

    assert _NFC("관측시점  ") in 요약, "요약에 관측시점 줄 자체가 없다"
    assert _NFC("관측시점  {'실었다': 0, '안쟀다': 1}") in 요약, (
        f"「실었다」가 먼저 오는 그 모양이 아니다: {요약}"
    )


def test_세_번째_칸을_지금_만들지_않는다() -> None:
    """🔴 **「미래를 봤다」는 검사를 걸 때 생기는 칸이다.**

    지금 만들면 아무도 안 채운 상태에서 전부 막힌다 — 이 판은 칸을 여는 데까지다.
    """
    미래 = date(2026, 12, 31)
    센것 = dict(_걷기(_하루(_품목(미래))).observation_coverage)

    assert 센것 == {"실었다": 1, "안쟀다": 0}, "미래 관측을 따로 세고 있다 — 이 판이 아니다"


# ---------------------------------------------------------------------------
# 이름의 주인은 하나다 — **0건을 세면 그것도 막는다**
# ---------------------------------------------------------------------------


_요약파일 = Path(backtest_runner.__file__)


def _문자열들(source: str) -> list[str]:
    """그 파일이 코드에 적어 둔 문자열 상수 전부. **docstring 도 여기 든다.**"""
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_두_칸의_이름을_요약이_두_번_적지_않는다() -> None:
    """🔴 **주인은 `_OBSERVED_AT_LABELS` 하나다.**

    요약 줄과 세는 자리가 각자 문자열을 적으면 한쪽만 고치는 날 이름이 갈리고,
    성적표는 **제가 안 세는 칸을 찍는다.**

    ★ **자기 생존 검사를 같이 둔다.** 스캐너가 문자열을 실제로 찾는지부터 본다 —
      안 그러면 0건을 세는 날 공짜 초록이 난다.
    """
    문자열 = [_NFC(one) for one in _문자열들(_요약파일.read_text(encoding="utf-8"))]

    assert 문자열, "스캐너가 문자열을 한 개도 못 찾았다 — 아래 단언은 공짜 초록이다"
    assert any(_NFC("관측시점") in one for one in 문자열), (
        "스캐너가 요약의 관측시점 줄을 못 찾았다 — 스캐너가 망가졌거나 줄이 사라졌다"
    )

    for 이름 in backtest_runner._OBSERVED_AT_LABELS:
        적힌수 = sum(1 for one in 문자열 if one.strip() == _NFC(이름))
        assert 적힌수 == 1, (
            f"요약 모듈이 '{이름}' 를 {적힌수} 번 적는다 — 주인은 _OBSERVED_AT_LABELS 하나다"
        )


def test_두_칸뿐이다() -> None:
    """🔴 세 번째 칸을 늘리려면 이 줄부터 고쳐야 한다 — 조용히 안 는다."""
    assert backtest_runner._OBSERVED_AT_LABELS == ("실었다", "안쟀다")
