"""마스터 테스트 공통 — **네트워크를 타지 않게 막는다.**

★ 여기 있는 fixture 하나가 지키는 것: `/master/ask` 경로가 **⑥(사용자 응답 생성)
  때문에 조용히 실 LLM 을 부르는 일**이 없게 한다.

  ①(의도 분류)은 `ask(request, service=...)` 로 갈아 끼울 수 있어 각 테스트가
  막아 왔는데, ⑥은 **API 를 통해 들어오면 갈아 끼울 자리가 없다** (`TestClient` 가
  라우터를 부르므로 인자를 넣을 수 없다). 그래서 모듈 경계에서 막는다.

  실 모델 채점은 `-m llm` 테스트가 서비스를 **직접** 부르므로 이 fixture 의 영향을
  받지 않는다.
"""

from __future__ import annotations

from datetime import date

import pytest
from 개장정본_격리 import (
    _개장_관문_함수,
    개장_관문_이름을_가져간_모듈들,
    개장_정본_이름을_가져간_모듈들,
)

from app.master.inputs import MasterInputs, SourcedInput
from app.master.llm.answer_runtime import NarrativeService
from app.master.llm.runtime import LLMSettings

_OFFLINE = LLMSettings(
    enabled=False,  # 꺼 두면 프로바이더를 만들지도, 부르지도 않는다
    provider="disabled",
    model="",
    base_url="",
    timeout_seconds=1.0,
    max_retries=0,
    max_output_tokens=256,
    effort=None,
)


class _NeverCalled:
    def generate(self, system: str, user: str, schema: dict) -> str:
        raise AssertionError("테스트에서 실 LLM 을 불렀다 — fixture 가 뚫렸다")


@pytest.fixture(autouse=True)
def 응답_생성_LLM_을_끈다(monkeypatch: pytest.MonkeyPatch) -> None:
    """⑥을 꺼 둔다. **답은 그대로 나온다** — 숫자는 규칙이 만들기 때문이다.

    문장이 붙는지 보고 싶은 테스트는 `NarrativeService` 를 직접 만들어 쓴다
    (`test_answer.py`).
    """
    monkeypatch.setattr(
        "app.master.ask_service.get_narrative_service",
        lambda: NarrativeService(_OFFLINE, _NeverCalled()),
    )


_NOT_LOADED = MasterInputs(
    forecast=SourcedInput("forecast", None, "MISSING", "-", "테스트에서는 적재하지 않는다"),
    confirmed_orders=SourcedInput("confirmed_orders", None, "MISSING", "-", ""),
    policy_values=SourcedInput("policy_values", None, "MISSING", "-", ""),
)


@pytest.fixture(autouse=True)
def 입력_적재를_끈다(monkeypatch: pytest.MonkeyPatch) -> None:
    """마스터가 실어 주는 입력 3종(§3.2.5)을 **DB 대신 빈 값으로** 준다.

    ★ **공용 DB 를 건드리지 않기 위해서다.** `.env` 의 `DB_HOST` 가 팀 공용 서버라
      `run_procurement` 을 그냥 부르면 실제 조회가 나간다.

    ★ 빈 값이어도 기존 테스트는 그대로 돈다 — 셋이 없으면 매입이 `missing_data` 로
      답하는 것이 **원래 계약**이고, 그 경로를 테스트가 이미 검사하고 있다.
      실 적재는 `test_inputs.py` 가 따로 본다.

    🔴 **판매 경로는 `load_forecast` 를 따로 부른다** (M-1). 판매가 나르는 것은 예측
      하나뿐이라 셋을 모으지 않는다 — 그래서 `collect_inputs` 만 막으면 `run_sales` 가
      **조용히 실 DB 를 친다.** 두 자리를 같이 막는다.
    """
    monkeypatch.setattr("app.master.service.collect_inputs", lambda *a, **k: _NOT_LOADED)
    monkeypatch.setattr("app.master.service.load_forecast", lambda *a, **k: _NOT_LOADED.forecast)


class _공휴일이_없는_달력:
    """모든 날을 덮고 공휴일은 하나도 없는 달력.

    ★ **오늘까지의 동작과 정확히 같다** — 주말만 걸린다. 그래서 이 fixture 는 기존
      검사들의 답을 하나도 바꾸지 않는다.

    ★ **공휴일 축이 실제로 붙는 경로는 `test_holiday_calendar.py` 가 본다.** 거기서는
      가짜 달력을 직접 꽂아 공휴일과 *"달력에 없는 날"* 을 둘 다 재현한다.
    """

    def is_holiday(self, day: object) -> bool:
        return False


class _주말만_쉬는_시장:
    """토·일에 장이 안 서고 평일에는 다 서는 **개장 축** 가짜 (`MarketCalendar`).

    ⚠️ **실제 달력과 다르다** — 실 표에서는 토요일 대부분이 개장이다 (2026년 45일).
      여기서 토요일을 닫는 것은 **기존 검사들의 답을 안 바꾸려는 것**이고, 개장 축이
      실제로 붙는 경로는 `test_execution_calendar*.py` 가 가짜를 직접 꽂아 본다.

    ★ 이 fixture 로는 *"봉투가 실렸는가"* 만 재고, *"어느 날이 실렸는가"* 는 못 잰다.
    """

    def is_market_open(self, day: date) -> bool:
        return day.weekday() < 5


@pytest.fixture(autouse=True)
def 개장_정본_적재를_막는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """개장 정본(`master_day_openings`) 조회·적재를 **DB 대신 가짜로** 받는다.

    🔴 **`record_day_opening` 이 예외를 삼킨다** (이력이 없는 것보다 하루를 못 여는
       것이 나쁘므로). 그래서 안 막으면 검사가 **조용히 실 DB 를 치고도 초록**이다.

    🔴 **조회(`read_day_opening`)도 같이 막는다.** 이쪽은 예외를 삼키지도 않고 값을
       돌려주므로, 안 막으면 검사의 답이 **그날 실 DB 에 행이 있느냐로 갈린다** —
       `day_gate` 의 근사 분기가 정확히 그렇게 무너져 있었다.

    ★ 이름을 가져간 모듈까지 훑어 막는다 — `개장_정본_이름을_가져간_모듈들` 참조.

    ★ 정본 자체를 재는 검사는 `test_day_opening_repository.py` 가 가짜 커넥션을 직접
      꽂고, **격리가 실제로 섰는지는 `test_db_isolation.py` 가 잰다.**
    """
    가짜 = {
        "read_day_opening": lambda **kw: None,
        "record_day_opening": lambda **kw: True,
    }
    for 이름, 대체 in 가짜.items():
        for 모듈 in 개장_정본_이름을_가져간_모듈들(이름):
            monkeypatch.setattr(모듈, 이름, 대체)


@pytest.fixture(autouse=True)
def 개장_관문을_통과시킨다(monkeypatch: pytest.MonkeyPatch) -> None:
    """개장 관문을 **DB 없이 통과**시킨다.

    🔴 **`run_procurement` 이 첫 관문에서 `is_open` 을 묻는다** (2026-09-06 · 계약).
       등록소가 전역이라 다른 검사가 하루 넘김을 등록해 두면, 그 뒤 실행이 **실 DB 로
       개장 여부를 물으러 나간다.**

    🔴 **이름을 가져간 모듈을 전부 막는다.** 진입점이 셋이 됐다 (매입 · 판매 ·
       재검증). 손으로 한 줄씩 적으면 넷째가 생긴 날 또 조용히 샌다 —
       **격리가 실제로 섰는지는 `test_db_isolation.py` 가 잰다.**

    ★ **관문 자체를 재는 검사는 `test_day_gate.py` 가 가짜를 직접 꽂는다.** 그 파일은
      `from app.master.day_gate import check_day_gate` 로 **자기 네임스페이스에** 이미
      복사해 두었으므로 여기서 원본을 바꿔도 진짜를 잰다. 여기서는 *"관문 때문에 다른
      검사가 막히지 않는다"* 만 보장한다 — 공휴일 달력을 가짜로 주는 것과 같은 이유다.
    """
    from app.master.day_gate import DayGate

    def 통과(as_of: date, **kw: object) -> DayGate:
        return DayGate(as_of=as_of, gate="PASS", result="ALREADY_OPENED", last_opened_date=as_of)

    for 모듈 in 개장_관문_이름을_가져간_모듈들():
        monkeypatch.setattr(모듈, _개장_관문_함수, 통과)


@pytest.fixture(autouse=True)
def 공휴일_달력을_가짜로_준다(monkeypatch: pytest.MonkeyPatch) -> None:
    """문 앞의 공휴일 축과 봉투의 개장 축을 **DB 대신 가짜로** 준다.

    ★ **입력 적재를 끄는 것과 같은 이유다.** `.env` 의 `DB_HOST` 가 팀 공용 서버라
      `run_procurement` 을 그냥 부르면 `ml_calendar_days` 로 실제 조회가 나간다.

    ⚠️ **둘 다 꽂는다.** 축이 둘이라 한쪽만 막으면 나머지가 조용히 실 DB 를 친다.
    """
    monkeypatch.setattr("app.master.service.get_calendar", lambda: _공휴일이_없는_달력())
    monkeypatch.setattr("app.master.service.get_market_calendar", lambda: _주말만_쉬는_시장())


class _배치가_늘_도는_달력:
    """**배치 축** 가짜 (`MlBatchCalendar`). 모든 날에 배치가 돈다고 답한다.

    ⚠️ **실제 달력과 다르다** — 실 표에서는 토요일 대부분이 `is_survey=f` 다. 여기서
      늘 참으로 두는 것은 **기존 검사들의 답을 안 바꾸려는 것**이고, 배치 축이 실제로
      판단을 가르는 경로는 `test_no_ml_batch_day.py` 가 가짜를 직접 꽂아 본다.
    """

    def has_ml_batch(self, day: date) -> bool:
        return True


@pytest.fixture(autouse=True)
def 배치_달력을_가짜로_준다(monkeypatch: pytest.MonkeyPatch) -> None:
    """스케줄러 · 걷기의 **배치 축**을 DB 대신 가짜로 준다 (2026-09-13).

    ★ **`wake_up` · `walk` 의 기본값이 `get_ml_batch_calendar` 자체다.** 기본 인자로
      이미 묶여 있어 모듈 속성으로는 못 바꾼다 — 그 함수가 부를 때마다 찾아가는
      **프로세스 캐시(`_BATCH`)** 가 실제 문이고, 거기에 가짜를 앉힌다.

    🔴 **안 막으면 배치 축을 안 넘긴 검사가 조용히 `ml_calendar_days` 를 친다.** `.env`
      가 없는 자리에서는 `BLOCKED` 로 빨개지고, 있는 자리에서는 **그날 표에 따라** 답이
      갈린다 — 둘 다 다른 사람 손에서 재현되지 않는다.
    """
    monkeypatch.setattr("app.master.ml_batch_calendar._BATCH", _배치가_늘_도는_달력())


#: 미적용 전이를 찾는 두 조회가 **DB 로 나가는 문**. 🔴 **여기 하나만 막으면 된다.**
#:
#: ★ **`approved_decisions` 를 갈아 끼우지 않는 이유.** 그 이름은
#:   `retry_pending_transitions` 의 **기본 인자로 이미 묶여 있어** 모듈 속성을 바꿔도
#:   안 바뀐다 (기본값이 `None` 이 아니라 함수 자체인 규율의 대가다). 진짜 함수가
#:   호출 때 찾아가는 이름은 `fetch_all` 이고, 그것이 실제 문이다.
미적용_조회_문 = "app.master.pending_transition_repository.fetch_all"


@pytest.fixture(autouse=True)
def 미적용_전이_조회를_막는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """미적용 전이를 찾는 조회를 **DB 대신 빈 목록으로** 받는다 (2026-09-11).

    🔴 **여기가 안 막히면 검사가 실 DB 에 「쓴다」.** 하루 순서의 재시도 단계는 찾은
       미적용마다 `apply_approval` 을 부르고, 그 함수는 `purchases` · 재무 · 물류에
       **행을 쓰고 커밋한다.** 다른 fixture 들이 막는 것은 조회인데 이 자리는
       **쓰기까지** 가므로, 새면 팀 공용 DB 에 검사가 만든 행이 남는다.

    ★ **빈 목록이면 재시도 단계는 `NOTHING_DUE` 로 돌아선다** — 진짜 코드가 돌되
      `apply_approval` 은 이름조차 안 불린다. 입력 적재를 빈 값으로 주는 것과 같은
      태도다.

    ★ **재시도 자체를 재는 검사는 `retry_fn` 을 직접 꽂거나
      `retry_pending_transitions` 에 대역을 넘긴다** (`test_pending_transition.py`) —
      여기서는 *"재시도 때문에 다른 검사가 실 DB 를 치지 않는다"* 만 보장한다.

    🔴 **격리가 실제로 섰는지는 `test_db_isolation.py` 가 잰다.**
    """
    def 아무것도_없다(*args: object, **kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(미적용_조회_문, 아무것도_없다)


# ══════════════════════════════════════════════════════════════════════
#  `db` 마크가 없는 검사는 실 DB 없이 돈다 (2026-09-14)
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 **아래 셋은 `@pytest.mark.db` 검사에는 안 걸린다.** 그 검사는 실제 표를 재는 것이
#    목적이라, 여기서 막으면 초록인데 아무것도 안 잰 검사가 된다.
#
# ★ **`.env` 가 없는 자리에서 `tests/master` 만 돌리면 183건이 빨갰다** (측정 2026-09-14).
#   전체 스위트에서는 41건으로 보였는데, `tests/finance` · `tests/sales` 의 어떤 모듈이
#   **수집 때** `os.environ.setdefault("DB_SCHEMA", ...)` 를 불러 주기 때문이다. 남의
#   폴더가 먼저 수집되느냐로 마스터 검사의 색이 갈리면 안 된다.


def _실_DB_검사다(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("db") is not None


#: `tests/master` 가 이미 쓰는 스키마 이름 (`test_outbound_carries_run_axis.py` 와 같은 값).
검사용_스키마 = "haetdeul"


@pytest.fixture(autouse=True)
def 스키마_이름을_환경에_둔다(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`get_db_schema()` 가 읽는 `DB_SCHEMA` 를 **환경에 둔다. 연결은 안 연다.**

    ★ 이 이름으로 막히던 검사는 전부 가짜 커넥션에 실린 SQL 문장을 잰다. 스키마 이름은
      문장에 붙는 글자일 뿐이고, 기대값도 같은 `get_db_schema()` 로 짓는다.
    """
    if _실_DB_검사다(request):
        return
    monkeypatch.setenv("DB_SCHEMA", 검사용_스키마)


#: 매입 경계를 읽는 조회가 **DB 로 나가는 문**.
매입_경계_조회_문 = "app.master.procurement_boundary.list_runs"


@pytest.fixture(autouse=True)
def 매입_경계_조회를_막는다(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """판매 진입점이 읽는 매입 경계를 **DB 대신 「그날 그 축에 행이 없다」로** 받는다.

    🔴 **`run_sales` 가 `_procurement_boundary` 로 `master_agent_runs` 를 읽는다**
       (2026-09-10 · 라우팅 개방). 안 막으면 판매 검사 41건이 조용히 실 DB 를 친다.

    ★ **표 접근 하나만 막는다.** 판정(`①` 행 → `②` 관문 → `③` 실행일 → `④` 그 밖)은
      진짜 코드가 돈다. 그래서 평일은 `NO_PROCUREMENT_RUN`, 토요일은
      `NOT_EXECUTION_DAY` 로 그대로 나온다.

    ★ **경계 판정 자체를 재는 검사는 `list_runs` 대역을 직접 꽂는다**
      (`test_procurement_boundary.py` · `test_ledger_gap_key.py`) — 이 fixture 뒤에
      꽂으므로 그쪽 대역이 이긴다.

    🔴 **격리가 실제로 섰는지는 `test_db_isolation.py` 가 잰다.**
    """
    if _실_DB_검사다(request):
        return

    def 행이_없다(**kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(매입_경계_조회_문, 행이_없다)


class 실_DB_연결을_열었다(AssertionError):
    """`db` 마크가 없는 검사가 `psycopg.connect` 까지 갔다."""


@pytest.fixture(autouse=True)
def 실_DB_연결을_막는다(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **`db` 마크가 없는 검사가 연결을 열려 하면 그 자리에서 예외를 던진다.**

    ★ **문이 하나다.** 재무 · 물류 · 매입 · 판매 · ML 의 `get_connection` 이 전부
      `psycopg.connect` 를 부른다. 이름을 복사해 간 모듈이 많아도 끝은 여기라, 여기
      하나를 막으면 새 경로가 생겨도 실 DB 까지는 안 간다.

    ★ **`.env` 가 있는 자리를 없는 자리와 같게 만든다.** 없는 자리에서는 환경변수
      확인이 먼저 터져 여기까지 안 온다. 있는 자리에서는 이 가드가 없으면 새는 검사가
      **팀 공용 DB 를 조용히 치고** 답이 그날 표에 따라 갈린다.

    ⚠️ **예외를 삼키는 경로는 이 가드로 빨개지지 않는다.** 검사 뒤에 「불렸다」로
      실패시키면 기존 검사 204건이 빨개져서(측정 2026-09-14 · 가짜 접속 환경변수)
      그 판정은 넣지 않았다. 삼키는 경로도 **실 DB 에는 닿지 않는다.**
    """
    if _실_DB_검사다(request):
        return

    import psycopg

    def 막는다(*args: object, **kwargs: object) -> object:
        raise 실_DB_연결을_열었다(
            f"db 마크가 없는 검사가 실 DB 연결을 열었다: {request.node.nodeid}"
        )

    monkeypatch.setattr(psycopg, "connect", 막는다)
