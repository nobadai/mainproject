"""**실행 축이 요청에서 봉투를 지나 판단 행까지 가나** (`#531` 후속 · 2026-09-10).

`#531` 로 `walk()` 이 축을 받고 승인 경로가 실행 행의 축을 읽게 됐다. **그런데 축이
봉투까지 안 갔다.**

```text
service.run_procurement:114   sim_run_id=BURN_IN_SIM_RUN_ID   ← 여기서 끊겼다
service.run_sales:288         sim_run_id=BURN_IN_SIM_RUN_ID   ← 같은 자리
```

그 아래는 전부 `context.sim_run_id` 를 쓴다. 그래서 **걷기가 축을 줘도 판단 행이
번인으로 저장되고, 승인 경로가 그 행을 읽으니 원장도 번인으로 돌아왔다.**

이 파일이 잠그는 것은 다섯이다.

```text
①  요청이 축을 주면 **봉투까지** 그 값이 간다
②  요청이 축을 주면 **판단 행(master_agent_runs.sim_run_id)** 에 그 값이 저장된다
③  요청이 안 주면 기본값으로 가되 **출처표에 "DEFAULT:…" 가 적힌다** (키를 안 뺀다)
④  walk 이 준 축이 **판단 요청까지** 간다 — 중간에서 안 바뀐다
⑤  등록소가 든 축과 걷기 축이 **갈리지 않는다**
```

🔴 **DB 를 안 탄다.** 개장 관문도 적재도 커넥션도 전부 대역이다 — 이 판은 자리를
   잇는 것까지고, 실제로 걷거나 행을 쓰는 것은 이 판이 하지 않는다.

⚠️ **`_input_sources` 는 함수로 직접 잰다.** 그 표는 모든 관문을 지난 날에만 서므로,
  전 경로를 태우면 이 검사가 개장·실행일·배선까지 다 떠안게 된다. 재는 것은
  *"기본값으로 떨어진 사실을 적는가"* 하나다.
"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

import app.main  # 임포트 시점에 배선이 선다 · ⑤ 의 `실제_배선` 이 이 모듈을 다시 실행한다
from app.master import persistence, service
from app.master.day_gate import DayGate
from app.master.envelope import ExecutionContext
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.scheduler import ScheduledAction, run_scheduled_day
from app.master.schemas import ProcurementRunRequest, SalesRunRequest
from app.master.sim_run_binding import SimRunBound, bind_sim_run

AS_OF = date(2026, 1, 7)

#: 이 검사가 요청에 싣는 실행 축. 🔴 **운영값(`BURN_IN_SIM_RUN_ID`)과 다르다** —
#:   같으면 봉투가 상수를 다시 읽는 뮤턴트가 전부 살아남는다.
실행축 = "SIM-ENVELOPE-202601"


# ---------------------------------------------------------------------------
# 대역
# ---------------------------------------------------------------------------


def _매입요청(**kw: Any) -> ProcurementRunRequest:
    return ProcurementRunRequest(as_of=AS_OF, policy_version="v1.3", item="배추", **kw)


def _판매요청(**kw: Any) -> SalesRunRequest:
    return SalesRunRequest(
        as_of=AS_OF,
        policy_version="v1.3",
        item="배추",
        business_mode="SPOT_SALES",
        **kw,
    )


@pytest.fixture
def 봉투를_잡는다(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`ExecutionContext` 가 **무엇을 받고 서는지** 그대로 잡는다.

    ★ 응답에서 되읽지 않는다. 봉투의 `sim_run_id` 는 응답 칸이 아니라서, 되읽으려면
      적재까지 태워야 한다 — 재려는 것은 **봉투에 실렸나** 하나다.
    """
    잡힌: dict[str, Any] = {}
    진짜 = service.ExecutionContext

    def _간첩(**kw: Any) -> ExecutionContext:
        잡힌.update(kw)
        return 진짜(**kw)

    monkeypatch.setattr(service, "ExecutionContext", _간첩)
    return 잡힌


@pytest.fixture
def 문앞에서_접는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """개장 관문을 **대역으로 막는다.** DB 를 안 타고 적재 자리까지만 간다.

    🔴 **막힌 날도 이력에 남는다** — `service` 가 그렇게 만들어져 있고, 그래서 이
       갈래에서도 `persistence.record(..., sim_run_id=context.sim_run_id)` 가 불린다.
       축이 봉투까지 갔는지를 **가장 짧은 경로로** 잴 수 있는 자리다.
    """
    막힘 = DayGate(as_of=AS_OF, gate="BLOCKED", result="NEVER_OPENED", reason="대역: 안 열린 날")
    monkeypatch.setattr(service, "check_day_gate", lambda *a, **k: 막힘)


@pytest.fixture
def 적재를_잡는다(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`try_save_run` 이 실제로 받는 것을 잡는다 — `master_agent_runs` 에 앉는 값이다.

    ★ `persistence.record` 를 통째로 갈아 끼우지 않는다. 그러면 *"진입점이 봉투에서
      꺼내 넘겼나"* 까지만 재고, **적재 함수가 그 값을 칸으로 들고 가는가**는 안 잰다.
    """
    잡힌: dict[str, Any] = {}
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: 잡힌.update(kw) or "RUN-1")
    return 잡힌


# ---------------------------------------------------------------------------
# ① 요청이 준 축이 봉투까지 간다
# ---------------------------------------------------------------------------


def test_매입_요청이_준_축이_봉투에_실린다(봉투를_잡는다, 문앞에서_접는다, 적재를_잡는다) -> None:
    """🔴 전에는 진입점이 상수를 다시 적어 **걷기가 축을 줘도 여기서 끊겼다.**"""
    service.run_procurement(_매입요청(sim_run_id=실행축), verifier=lambda *a, **k: None)

    assert 봉투를_잡는다["sim_run_id"] == 실행축
    assert 봉투를_잡는다["sim_run_id"] != BURN_IN_SIM_RUN_ID


def test_판매_요청이_준_축이_봉투에_실린다(봉투를_잡는다, 문앞에서_접는다, 적재를_잡는다) -> None:
    """★ **매입과 같은 자리다.** 여기만 상수로 남으면 같은 날 두 판단이 갈린다."""
    service.run_sales(_판매요청(sim_run_id=실행축), verifier=lambda *a, **k: None)

    assert 봉투를_잡는다["sim_run_id"] == 실행축
    assert 봉투를_잡는다["sim_run_id"] != BURN_IN_SIM_RUN_ID


def test_안_주면_번인으로_간다(봉투를_잡는다, 문앞에서_접는다, 적재를_잡는다) -> None:
    """🟢 **라우터·화면 경로는 동작이 안 바뀐다.** 기본값이 그대로 돈다.

    ⚠️ 기본값을 없애면 라우터가 깨진다 — 이번 판은 운영 동작을 안 바꾼다.
    """
    service.run_procurement(_매입요청(), verifier=lambda *a, **k: None)

    assert 봉투를_잡는다["sim_run_id"] == BURN_IN_SIM_RUN_ID


@pytest.mark.parametrize("빈축", ["", "   "])
def test_빈_축은_안_준_것으로_본다(빈축, 봉투를_잡는다, 문앞에서_접는다, 적재를_잡는다) -> None:
    """★ 공백만 든 축을 봉투에 그대로 실으면 `ledger.sim_run_id_for` 가 **뒤에서**
    터지고, 그 실패가 *"요청이 이상했다"* 가 아니라 *"원장이 터졌다"* 로 읽힌다.
    """
    service.run_procurement(_매입요청(sim_run_id=빈축), verifier=lambda *a, **k: None)

    assert 봉투를_잡는다["sim_run_id"] == BURN_IN_SIM_RUN_ID


# ---------------------------------------------------------------------------
# ② 그 축이 판단 행에 저장된다
# ---------------------------------------------------------------------------


def test_매입_판단_행에_요청이_준_축이_앉는다(문앞에서_접는다, 적재를_잡는다) -> None:
    """🔴 **`master_agent_runs.sim_run_id` 다.** 승인 경로가 그 행에서 축을 읽으므로
    (`decision_service._sim_run_id_of`), 여기가 번인이면 **원장까지 번인으로 돌아온다.**
    """
    service.run_procurement(_매입요청(sim_run_id=실행축), verifier=lambda *a, **k: None)

    assert 적재를_잡는다["sim_run_id"] == 실행축


def test_판매_판단_행에_요청이_준_축이_앉는다(문앞에서_접는다, 적재를_잡는다) -> None:
    service.run_sales(_판매요청(sim_run_id=실행축), verifier=lambda *a, **k: None)

    assert 적재를_잡는다["sim_run_id"] == 실행축


# ---------------------------------------------------------------------------
# ③ 기본값으로 떨어진 사실을 **적는다**
# ---------------------------------------------------------------------------


def test_기본값이면_출처표가_그렇게_적는다() -> None:
    """🔴 **적히는 것 자체를 잰다.** 기본값을 조용히 두면 *"말 안 하고 번인에 쌓는"*
    길이 그대로 남는다 — `#524` 가 봉투에 대해 낸 결론과 같은 자리다.
    """
    표 = service._input_sources(_매입요청(), None)

    assert 표["sim_run_id"] == "DEFAULT:burn_in"


def test_요청이_주면_출처표가_요청이라고_적는다() -> None:
    """★ **어휘를 새로 만들지 않았다.** `REQUEST` 는 이미 있던 것이다."""
    표 = service._input_sources(_매입요청(sim_run_id=실행축), None)

    assert 표["sim_run_id"] == "REQUEST:sim_run_id"


def test_출처표가_축_키를_통째로_빼지_않는다() -> None:
    """🔴 **「없다」와 「모른다」가 섞이면 안 된다.**

    키를 빼면 읽는 쪽이 *"이 실행이 왜 번인에 앉았는지"* 를 확인할 표가 없어
    **없는 표를 다시 뒤진다** — 봉투(`execution_calendar`)에서 이미 한 번 겪은 일이다.
    """
    for 요청 in (_매입요청(), _매입요청(sim_run_id=실행축)):
        assert "sim_run_id" in service._input_sources(요청, None), (
            "출처표에서 축 키가 통째로 빠졌다 — 「기본값이었다」가 「모른다」와 섞인다"
        )


def test_축_출처를_값과_같은_판정으로_고른다() -> None:
    """★ **한 자리에서만 판정한다.** 값(`_sim_run_id_of`)과 출처(`_sim_run_source`)가
    서로 다른 규칙으로 가르면, 요청이 공백을 준 날 *"요청이 줬다"* 고 적으면서 값은
    번인으로 앉는다.
    """
    공백요청 = _매입요청(sim_run_id="   ")

    assert service._sim_run_id_of(공백요청) == BURN_IN_SIM_RUN_ID
    assert service._input_sources(공백요청, None)["sim_run_id"] == "DEFAULT:burn_in"


# ---------------------------------------------------------------------------
# ④ 걷기가 준 축이 판단 요청까지 간다
# ---------------------------------------------------------------------------


def _하루를_돈다(**대역: Any) -> dict[str, Any]:
    """`run_scheduled_day` 를 **전부 대역으로** 한 번 돌린다. DB 를 안 탄다."""

    class _열림:
        status = "OPENED"
        reason = ""

    class _통과:
        status = "NOTHING_DUE"
        reason = ""

    기본 = {
        "open_day_fn": lambda *a, **k: _열림(),
        "receive_fn": lambda *a, **k: _통과(),
        "issue_fn": lambda *a, **k: _통과(),
        "collect_fn": lambda *a, **k: _통과(),
        "procure_fn": lambda req: None,
        "sales_fn": lambda req: None,
        "outbound_fn": lambda *a, **k: _통과(),
        "close_fn": lambda *a, **k: _통과(),
    }
    기본.update(대역)
    지금 = datetime(2026, 1, 7, 9, 0, tzinfo=UTC)
    run_scheduled_day(
        ScheduledAction(
            as_of=AS_OF,
            now=지금,
            action="RUN_NOW",
            reason="검사",
            deadline=지금 + timedelta(hours=1),
        ),
        sim_run_id=실행축,
        items=("배추",),
        **기본,
    )
    return 기본


def test_걷기_축이_매입_판단_요청까지_간다() -> None:
    """★★ **여기가 `#531` 이 남긴 자리다.** 걷기는 축을 들고 있었는데 요청에 안 실었다."""
    실린: list[str | None] = []
    _하루를_돈다(procure_fn=lambda req: 실린.append(req.sim_run_id))

    assert 실린 == [실행축]


def test_걷기_축이_판매_판단_요청까지_간다() -> None:
    """🔴 **매입만 이으면 같은 날 판매 행이 번인에 앉는다** — 그러면 판매가 읽는
    매입 경계(`service._procurement_boundary`)가 남의 실행 것이 된다.
    """
    실린: list[str | None] = []
    _하루를_돈다(sales_fn=lambda req: 실린.append(req.sim_run_id))

    assert 실린 == [실행축]


@pytest.mark.parametrize("단계", ["open_day_fn", "receive_fn", "issue_fn", "collect_fn"])
def test_걷기_축이_장부_네_단계까지_간다(단계) -> None:
    """🔴 **판단만 이으면 반쪽이다.** 개장·입고·채권·수금은 등록소 어댑터를 부르고,
    그 어댑터가 프로세스 시작 때 든 상수를 쓰면 **매입 원장만 새 실행에 앉는다.**
    """
    실린: list[str | None] = []

    class _답:
        status = "OPENED" if 단계 == "open_day_fn" else "NOTHING_DUE"
        reason = ""

    def _잡는다(as_of, **kw):
        실린.append(kw.get("sim_run_id"))
        return _답()

    _하루를_돈다(**{단계: _잡는다})

    assert 실린 == [실행축]


# ---------------------------------------------------------------------------
# ⑤ 등록소가 든 축과 걷기 축이 갈리지 않는다
# ---------------------------------------------------------------------------


def test_배선이_축을_상수로_들지_않는다() -> None:
    """🔴 **원문을 읽어 잠근다.** 배선에 상수가 다시 박히면 그날부터 등록소만
    번인에 남고, 그 갈림은 **아무 오류도 안 낸다.**

    ★ **주석을 걷어내고 잰다** — 이 파일도 배선 파일도 근거를 길게 적는다.
      안 걷으면 코드가 아니라 문장을 재게 된다.
    """
    import pathlib

    from app.master import bootstrap

    원문 = pathlib.Path(bootstrap.__file__).read_text(encoding="utf-8")
    코드 = "\n".join(줄 for 줄 in 원문.splitlines() if not 줄.lstrip().startswith("#"))

    assert "BURN_IN_SIM_RUN_ID" not in 코드, (
        "배선이 축을 상수로 든다 — 걷기가 번인 아닌 실행을 타는 날 등록소만 번인에 남는다"
    )


def test_감싼_등록은_부를_때_축을_받는다() -> None:
    """★ **등록 시점에 어댑터를 안 만든다.** 만들면 그 순간 축이 굳는다."""
    만든: list[str] = []
    감쌈 = SimRunBound(lambda axis: 만든.append(axis) or f"어댑터({axis})")

    assert 만든 == [], "등록만 했는데 어댑터가 이미 섰다 — 축이 그 시점에 굳는다"
    assert bind_sim_run(감쌈, 실행축) == f"어댑터({실행축})"
    assert 만든 == [실행축]


def test_축을_안_쓰는_등록은_그대로_돌려준다() -> None:
    """★ 재무 전이·하루넘김은 생성자에 축이 없다. 그런 등록까지 감싸면 아무 뜻 없는
    껍데기가 여섯 자리에 는다.
    """
    그냥 = object()

    assert bind_sim_run(그냥, 실행축) is 그냥


@pytest.mark.parametrize("빈축", [None, "", "   "])
def test_빈_축으로는_등록소를_못_묶는다(빈축) -> None:
    """🔴 **상수로 안 메운다.** 조용히 번인으로 떨어지면 그 갈림이 아무 오류도 안 낸다 —
    `ledger.sim_run_id_for` 와 같은 태도다.
    """
    with pytest.raises(ValueError, match="sim_run_id"):
        bind_sim_run(SimRunBound(lambda axis: axis), 빈축)


@pytest.fixture
def 실제_배선() -> Any:
    """`bootstrap.wire_registries` 를 **이 검사 안에서 다시 실행**한다.

    🔴 **`import app.main` 만으로는 부족하다.** 모듈 캐시 때문에 등록은 프로세스에서
       딱 한 번 일어나고, 그 뒤 누군가 등록소를 비우면 다시 채워지지 않는다 —
       그러면 아래 검사가 *"실제 배선"* 이 아니라 **앞 검사가 남긴 것**을 잰다.

    ⚠️ **실제로 새는 자리가 있다** (`tests/logistics/test_logistics_day_open.py` 의
      같은 fixture 가 그 실측을 적어 뒀다). 루트 `conftest.py` 는 `wiring` ·
      `transition` · `day_open` 만 되돌리고 **`cancellation` 은 안 되돌린다.**

    ★ **값을 검사가 지어내지 않는다.** 상수를 들고 다시 등록하는 것이 아니라 배선
      함수를 다시 돌린다 — 검사가 production 사실을 복제하면 배선이 틀려도 초록불이다.
    """
    from app.master import cancellation as master_cancellation

    이전_취소 = dict(master_cancellation.registered_cancellations())
    try:
        importlib.reload(app.main)
        yield
    finally:
        master_cancellation.reset()
        for part, impl in 이전_취소.items():
            master_cancellation.register_cancellation(part, impl)


def test_여섯_등록소가_같은_축_하나를_받는다(실제_배선) -> None:
    """★★ **이 한 줄이 「조용한 갈림」을 잰다.** 반쪽이면 매입 원장은 새 실행에,
    물류 장부는 번인에 앉는다 — `ledger.py` 가 경고해 둔 그 모양이다.

    ```text
    전이 · 하루넘김 · 취소 · 입고    물류 어댑터가 축을 든다
    수금 · 채권                      재무 어댑터가 축을 든다
    ```

    🔴 **`registered()` 를 지나서 잰다.** 배선 원문만 읽으면 등록소가 감싼 것을
       **풀지 않고 그대로 쓰는** 날을 못 잡는다.
    """
    from app.master import cancellation, collection, day_open, inbound, receivable, transition

    등록들 = [
        transition.registered()["logistics"],
        day_open.registered()["logistics"],
        cancellation.registered_cancellations()["logistics"],
        inbound.registered()["logistics"],
        collection.registered()["finance"],
        receivable.registered()["finance"],
    ]

    for 등록 in 등록들:
        assert isinstance(등록, SimRunBound), (
            f"등록소가 축을 호출 때 안 받는다: {type(등록).__name__}"
        )

    축들 = {
        getattr(어댑터, "sim_run_id", None) or getattr(어댑터, "_sim_run_id", None)
        for 어댑터 in (bind_sim_run(등록, 실행축) for 등록 in 등록들)
    }
    assert 축들 == {실행축}, f"등록소들이 서로 다른 축에 앉았다: {축들}"


def test_감싼_등록도_미등록_판정을_안_바꾼다() -> None:
    """★ **감싼다고 미등록 판정이 바뀌면 안 된다.** `missing()` 은 키만 본다 —
    바뀌었으면 *"오늘 그 부서가 안 돈다"* 가 갑자기 오류가 된다.

    ★ **전역 등록을 읽지 않는다.** 다른 검사가 `reset()` 을 하면 순서에 따라 답이
      달라진다 — 여기서 직접 채우고, 되돌리기는 루트 `conftest.py` 가 한다.
    """
    from app.master import transition

    transition.reset()
    for part in transition.PARTS:
        transition.register_transition(part, SimRunBound(lambda axis: axis))

    assert transition.missing() == ()
