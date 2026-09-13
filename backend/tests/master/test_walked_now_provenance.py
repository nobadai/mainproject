"""**걷기가 받은 `--now` 를 요약에 찍고 원장에 남긴다** (2026-09-13).

```text
V8    --now 2026-09-13T16:00+09:00  마감(10:30) 뒤 → 그날이 끝난 상태를 본다
V9①   --now 2026-09-13T09:00+09:00  마감 전       → 14일이 `WAIT` 로 영영 안 돌았다
```

★★ **코드가 아니라 입력 하나 때문이었다.** 걷기는 `--now` 의 시각 부분만 날마다 붙여
  쓰고, 그 값이 요약에도 원장에도 없어서 두 판이 왜 갈렸는지 아무도 못 읽었다.

```text
① 요약      기준시각 줄 · 마감 전이면 🔴 로 그 사실을 찍는다 · 마감은 scheduler 에서 읽는다
② 원장      sim_runs.config_json.provenance.walked_now · 받은 문자열 그대로
③ 막는다    이미 다른 값이 있으면 걷기 전에 멈춘다 · 같으면 간다 · 없으면 쓴다
④ 언제      걷기 첫 날 **전에** ③ 과 ② 를 같이 한다
```

🔴 **DB 를 안 탄다.** 커넥션은 대역이고, 걷기의 기록 자리도 대역이다 — 이 판은 쓰는
   길을 세우는 것까지고 실제로 한 행도 쓰지 않는다.

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가 안
  같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import ast
import unicodedata
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Self

import pytest

from app.master import backtest_runner, scheduler, sim_run_runner, walk_provenance
from app.master.backtest_runner import WalkResult, format_summary, walk
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.scheduler import DayRunOutcome, ItemRunOutcome, plan_next_action
from app.master.sim_run_runner import PROVENANCE_CONFIG_KEY
from app.master.walk_provenance import (
    WALKED_NOW_KEY,
    WalkedNowConflict,
    record_walked_now,
    stamp_walked_now,
)

_MASTER = Path(__file__).resolve().parents[2] / "app" / "master"

실행축 = "SIM-TEST-WALKED-NOW"
#: V8 이 건 모양 그대로. 🔴 **초가 없다** — 정규화하면 `16:00:00` 이 되어 갈린다.
마감뒤 = "2026-09-13T16:00+09:00"
#: V9① 이 건 모양 그대로.
마감전 = "2026-09-13T09:00+09:00"
기준커밋 = "0e635f8"


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _벗긴_원문(path: Path) -> str:
    """주석과 docstring 을 걷어낸 원문.

    🔴 **왜 걷어내나.** 이 저장소는 근거를 길게 적는다 — 금지어가 설명 문장 안에
      있으면 원문 잠금은 코드가 아니라 문장을 재게 된다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return _NFC(ast.unparse(tree))


# ── ① 요약 ─────────────────────────────────────────────────────────────


def _요약(walked_now: str | None) -> list[str]:
    결과 = WalkResult(start=date(2026, 2, 7), end=date(2026, 2, 9), walked_now=walked_now)
    return _NFC(format_summary(결과)).splitlines()


def _기준시각줄(줄들: list[str]) -> str:
    """🔴 **정확히 한 줄이어야 한다.** 0 줄을 세고 초록이 되는 길을 막는다."""
    찾음 = [줄 for 줄 in 줄들 if 줄.startswith(_NFC("기준시각"))]
    assert len(찾음) == 1, f"기준시각 줄이 {len(찾음)}줄이다: {줄들}"
    return 찾음[0]


def test_마감_전이면_그_사실을_찍는다() -> None:
    """🔴 **이 한 줄이면 V9① 한 판을 안 버렸다.**

    09:00 을 주면 ML 배치가 없는 날이 전부 `WAIT` 이 되고, 걷기는 하루에 한 번만
    물으니 그 날들이 아예 안 돈다. 요약이 그것을 말하지 않으면 사람은 성적표만 보고
    코드를 의심한다.
    """
    줄 = _기준시각줄(_요약(마감전))

    assert _NFC("🔴") in 줄, f"마감 전인데 경고가 없다: {줄}"
    assert _NFC("마감 10:30 전") in 줄, 줄
    assert _NFC("배치 없는 날이 통째로 안 돈다") in 줄, 줄


def test_마감_뒤면_경고_없이_찍는다() -> None:
    """★ **경고가 늘 붙으면 경고가 아니다.** 뒤인 판에는 🔴 가 없어야 한다."""
    줄 = _기준시각줄(_요약(마감뒤))

    assert 줄 == _NFC(f"기준시각  {마감뒤} · 날마다 16:00 · 마감 10:30 뒤"), 줄
    assert _NFC("🔴") not in 줄


def test_받은_문자열을_그대로_먼저_찍는다() -> None:
    """★ **날짜 부분은 안 쓰이는데도 받은 값을 먼저 찍는다.** 사람이 준 것과 코드가
    쓰는 것이 둘 다 보여야 왜 갈렸는지 읽힌다.

    🔴 `isoformat()` 으로 다시 쓰면 `16:00:00` 이 되어 **사람이 준 것이 사라진다.**
    """
    줄 = _기준시각줄(_요약(마감뒤))

    assert 줄.startswith(_NFC(f"기준시각  {마감뒤} · ")), 줄
    assert "16:00:00" not in 줄, f"받은 문자열을 정규화했다: {줄}"


def test_기준시각_줄은_범위_바로_아래다() -> None:
    """★ **「이 판이 무엇 위에 섰나」 자리다.** 걷기 요약에는 기준커밋 줄이 없고
    (그 줄은 문의 요약에 있다), 걷기 요약의 첫 줄은 범위다.
    """
    줄들 = _요약(마감뒤)
    기준 = 줄들.index(_기준시각줄(줄들))

    assert 줄들[기준 - 1].startswith(_NFC("범위")), 줄들


def test_안_받았으면_그렇게_적는다() -> None:
    """🟡 **줄이 통째로 빠지지 않는다.** 안 받은 것을 조용히 넘기면 없는 것과 같다."""
    줄 = _기준시각줄(_요약(None))

    assert _NFC("안 받았다") in 줄, 줄


def test_마감_시각은_scheduler_에서_읽는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **숫자를 여기 다시 적지 않는다.** 마감이 바뀌는 날 요약만 옛 마감을 말하면
    이 줄이 오늘 막으려던 것과 똑같이 거짓말을 한다.

    ★ 원문을 안 읽는다 — **주인의 값을 바꿨을 때 요약이 따라오는지**를 잰다.
    """
    monkeypatch.setattr(scheduler, "SCHEDULE_DEADLINE", time(8, 0))
    뒤로바뀜 = _기준시각줄(_요약(마감전))

    monkeypatch.setattr(scheduler, "SCHEDULE_DEADLINE", time(17, 0))
    전으로바뀜 = _기준시각줄(_요약(마감뒤))

    assert 뒤로바뀜.endswith(_NFC("마감 08:00 뒤")), 뒤로바뀜
    assert _NFC("마감 17:00 전") in 전으로바뀜, 전으로바뀜


class _아무날도_안_온_게이트:
    """`NONE_READY` 날. **마감 전후로 판단이 갈리는 유일한 날이다.**"""

    def __call__(self, as_of: date) -> DayForecastReadiness:
        return DayForecastReadiness(
            as_of=as_of,
            readiness="NONE_READY",
            items=(ItemForecastGate(item="무", as_of=as_of, readiness="NOT_YET", grade="MISSING"),),
        )


class _늘_서는_시장:
    def is_market_open(self, day: date) -> bool:
        return True


@pytest.mark.parametrize(("받은", "기다리나"), [(마감전, True), (마감뒤, False)])
def test_요약의_전_뒤가_걷기가_실제로_기다린_것과_같다(받은: str, 기다리나: bool) -> None:
    """🔴 **요약이 제 판정을 따로 만들면 걷기와 갈린다.** 같은 시각을 걷기가 쓰는
    그대로 붙여 `plan_next_action` 에 물었을 때의 답과 요약의 전/뒤가 같아야 한다.
    """
    하루 = date(2026, 2, 9)
    판단 = plan_next_action(
        now=backtest_runner._moment_on(하루, datetime.fromisoformat(받은)),
        as_of=하루,
        calendar=_늘_서는_시장(),
        gate_result=_아무날도_안_온_게이트()(하루),
    )
    줄 = _기준시각줄(_요약(받은))

    assert (판단.action == "WAIT") is 기다리나, 판단
    assert (_NFC("🔴") in 줄) is 기다리나, 줄


# ── ④ 언제 — 걷기 첫 날 전에 ─────────────────────────────────────────────


def _돈_하루(as_of: date) -> DayRunOutcome:
    return DayRunOutcome(
        as_of=as_of,
        action="RUN_NOW",
        reason="예측이 왔다",
        day_open_status="OPENED",
        inbound_status="NOTHING_DUE",
        collection_status="NOTHING_DUE",
        procurement_status="RAN",
        outbound_status="NOTHING_DUE",
        items=(ItemRunOutcome(item="무", request_id="REQ-무", status="RAN", end_code="E1_OK"),),
        notes=("출고: NOTHING_DUE",),
    )


def _다_온_게이트(as_of: date) -> DayForecastReadiness:
    return DayForecastReadiness(
        as_of=as_of,
        readiness="ALL_READY",
        items=(ItemForecastGate(item="무", as_of=as_of, readiness="READY", grade="MEASURED"),),
    )


class _순서:
    """기록과 하루 실행이 **어느 순서로** 불렸는지를 한 목록에 모은다."""

    def __init__(self, *, 기록답: Any = "WRITTEN", 터지는날: frozenset[date] = frozenset()) -> None:
        self.log: list[tuple[str, Any]] = []
        self.기록답 = 기록답
        self.터지는날 = 터지는날

    def 기록(self, **kwargs: Any) -> str:
        self.log.append(("기록", kwargs))
        if isinstance(self.기록답, BaseException):
            raise self.기록답
        return self.기록답

    def 하루(self, action: Any, **_: Any) -> DayRunOutcome:
        self.log.append(("하루", action.as_of))
        if action.as_of in self.터지는날:
            raise RuntimeError("장부가 깨졌다")
        return _돈_하루(action.as_of)


def _걷는다(순서: _순서, **over: Any) -> WalkResult:
    인자: dict[str, Any] = {
        "sim_run_id": 실행축,
        "start": date(2026, 2, 9),
        "end": date(2026, 2, 11),
        "now": datetime.fromisoformat(마감뒤),
        "walked_now": 마감뒤,
        "calendar": _늘_서는_시장,
        "readiness": _다_온_게이트,
        "run_day_fn": 순서.하루,
        "ticks": lambda: 0.0,
        "terms_of": lambda _sim: None,
        "closings_of": lambda **_: (),
        "record_now": 순서.기록,
    }
    인자.update(over)
    return walk(**인자)


def test_걷기_첫_날_전에_적는다() -> None:
    """🔴 **다 걷고 나서 쓰지 않는다.** 179일을 걷다 중간에 멈추면 그 판에 무엇으로
    걸었는지가 안 남는다.
    """
    순서 = _순서()
    _걷는다(순서)

    이름들 = [이름 for 이름, _ in 순서.log]
    assert "하루" in 이름들, f"하루가 한 번도 안 돌았다 — 이 검사가 아무것도 안 쟀다: {순서.log}"
    assert 이름들[0] == "기록", f"첫 날보다 기록이 뒤다: {이름들}"
    assert 이름들.count("기록") == 1, f"기록을 여러 번 했다: {이름들}"


def test_중간에_멈춘_판에도_기록이_남아_있다() -> None:
    """★ 연속 사고 상한에 걸려 멈춘 판이 **정확히 그 판**이다 — 무엇으로 걸었는지를
    가장 먼저 물어야 하는 판이 끝까지 못 간 판이다.
    """
    순서 = _순서(터지는날=frozenset({date(2026, 2, 9), date(2026, 2, 10)}))
    결과 = _걷는다(순서, max_consecutive_failures=2)

    assert not 결과.completed, "멈추지 않았다 — 이 검사의 전제가 안 섰다"
    assert ("기록", {"sim_run_id": 실행축, "walked_now": 마감뒤}) in 순서.log
    assert 결과.walked_now == 마감뒤


def test_받은_문자열을_그대로_기록에_넘긴다() -> None:
    """🔴 **정규화하지 않는다.** `now.isoformat()` 을 넘기면 `16:00:00` 이 된다."""
    순서 = _순서()
    _걷는다(순서)

    기록들 = [값 for 이름, 값 in 순서.log if 이름 == "기록"]
    assert 기록들 == [{"sim_run_id": 실행축, "walked_now": 마감뒤}], 기록들


def test_이미_다른_값이면_한_날도_안_걷는다() -> None:
    """🔴 **걷기 전에 멈추고 사유를 낸다.** 절반은 마감 전 · 절반은 마감 뒤인 판은
    아무것도 증명하지 않는다.
    """
    순서 = _순서(기록답=WalkedNowConflict("이미 다른 기준 시각으로 걸렸다"))

    with pytest.raises(WalkedNowConflict):
        _걷는다(순서)

    assert [이름 for 이름, _ in 순서.log] == ["기록"], f"막혔는데 걸었다: {순서.log}"


def test_안_주면_기록을_부르지_않는다() -> None:
    """★ **기존 걷기 검사가 기록 자리를 안 꽂아도 실 DB 로 안 나간다.** 안 받은 값은
    적을 것이 없다 — 그 사실은 요약이 「안 받았다」로 말한다.
    """
    순서 = _순서()
    결과 = _걷는다(순서, walked_now=None)

    assert "하루" in [이름 for 이름, _ in 순서.log], "하루가 안 돌았다 — 이 검사가 아무것도 안 쟀다"
    assert "기록" not in [이름 for 이름, _ in 순서.log]
    assert 결과.walked_now is None


def test_받은_문자열과_걷는_시각이_다르면_막는다() -> None:
    """🔴 **칸이 거짓말을 하게 두지 않는다.** 적은 값과 실제로 건 값이 갈리면 그 칸은
    없는 것보다 나쁘다.
    """
    순서 = _순서()

    with pytest.raises(ValueError, match="기준 시각"):
        _걷는다(순서, now=datetime.fromisoformat(마감전), walked_now=마감뒤)

    assert 순서.log == [], f"갈렸는데 기록하거나 걸었다: {순서.log}"


def test_기록_자리의_기본값이_원장에_쓰는_함수_자체다() -> None:
    """★ **`None` 이 아니다.** 진입점이 대역 없이 부르면 실제로 원장에 남아야 한다."""
    import inspect

    기본값 = inspect.signature(walk).parameters["record_now"].default
    assert 기본값 is record_walked_now


def test_진입점이_받은_문자열을_그대로_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **`--now` 를 파싱한 값만 넘기면 받은 문자열이 사라진다.**"""
    받음: dict[str, Any] = {}

    def _대역(**kwargs: Any) -> WalkResult:
        받음.update(kwargs)
        return WalkResult(start=kwargs["start"], end=kwargs["end"], walked_now=kwargs["walked_now"])

    monkeypatch.setattr(backtest_runner, "walk", _대역)
    monkeypatch.setattr(backtest_runner, "wire_registries", lambda: None)

    backtest_runner.main(
        ["--sim-run-id", 실행축, "--start", "2026-01-05", "--end", "2026-01-09", "--now", 마감전]
    )

    assert 받음["walked_now"] == 마감전
    assert 받음["now"] == datetime.fromisoformat(마감전)


# ── ② ③ 원장 — 대역 커넥션 ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _스키마만_환경에서_끊는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_db_schema()` 는 `DB_SCHEMA` 를 읽는다 — 여기서 끊는다. **연결은 안 연다.**

    ★ 안 끊으면 이 파일의 답이 **앞서 돈 검사가 환경을 채웠느냐로** 갈린다
      (`test_outbound_carries_run_axis.py` 와 같은 모양).
    """
    monkeypatch.setenv("DB_SCHEMA", "haetdeul")


class _대역커서:
    def __init__(self, 대장: _대역커넥션) -> None:
        self.대장 = 대장

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        문장 = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.대장.log.append((문장, list(params or [])))

    def fetchone(self) -> dict[str, Any] | None:
        return None if self.대장.설정 is None else {"config_json": self.대장.설정}


class _대역커넥션:
    def __init__(self, 설정: dict[str, Any] | None) -> None:
        self.설정 = 설정
        self.log: list[tuple[str, list[Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> _대역커서:
        return _대역커서(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1

    @property
    def 쓴것(self) -> list[tuple[str, list[Any]]]:
        return [(문장, 값) for 문장, 값 in self.log if 문장.lstrip().upper().startswith("UPDATE")]


def _연_설정(**자취: str) -> dict[str, Any]:
    설정: dict[str, Any] = {"baseline": {"from_sim_run_id": "SIM-BURNIN-202512"}}
    if 자취:
        설정[PROVENANCE_CONFIG_KEY] = dict(자취)
    return 설정


def test_없으면_받은_문자열_그대로_쓴다() -> None:
    conn = _대역커넥션(_연_설정(commit=기준커밋))

    답 = stamp_walked_now(conn, sim_run_id=실행축, walked_now=마감뒤)

    assert 답 == "WRITTEN"
    assert len(conn.쓴것) == 1, conn.log
    _, 값 = conn.쓴것[0]
    assert 마감뒤 in 값, f"받은 문자열 그대로가 아니다: {값}"
    assert WALKED_NOW_KEY in 값 and PROVENANCE_CONFIG_KEY in 값, 값
    assert 실행축 in 값


def test_commit_을_안_건드린다() -> None:
    """🔴 **칸을 통째로 덮지 않는다.** 여는 자리가 적은 커밋이 사라진다.

    ★ 한 키만 붙이는 모양인지를 잰다 — 옛 `provenance` 에 새 키를 **합친다**.
    """
    conn = _대역커넥션(_연_설정(commit=기준커밋))

    stamp_walked_now(conn, sim_run_id=실행축, walked_now=마감뒤)

    문장, 값 = conn.쓴것[0]
    assert "jsonb_set" in 문장 and "||" in 문장, f"칸을 합치지 않고 덮는다: {문장}"
    assert "commit" not in 문장 and 기준커밋 not in 값, f"커밋을 건드린다: {문장} {값}"


def test_같은_값이면_그대로_간다() -> None:
    """★ **이어 걷기는 정상이다.** 같은 값을 다시 쓰지도 않는다."""
    conn = _대역커넥션(_연_설정(commit=기준커밋, walked_now=마감뒤))

    답 = stamp_walked_now(conn, sim_run_id=실행축, walked_now=마감뒤)

    assert 답 == "ALREADY_SAME"
    assert len(conn.log) == 1, "잰 것이 없다 — 이 검사가 아무것도 안 쟀다"
    assert conn.쓴것 == []


def test_이미_다른_값이면_막고_덮지_않는다() -> None:
    """🔴 **덮어쓰기 옵션을 만들지 않는다.** 다른 시각으로 걸려면 `--reset` 으로 새로 연다."""
    conn = _대역커넥션(_연_설정(commit=기준커밋, walked_now=마감뒤))

    with pytest.raises(WalkedNowConflict) as 막힘:
        stamp_walked_now(conn, sim_run_id=실행축, walked_now=마감전)

    assert len(conn.log) == 1, "잰 것이 없다 — 이 검사가 아무것도 안 쟀다"
    assert conn.쓴것 == [], f"다른 값 위에 덮었다: {conn.log}"
    사유 = _NFC(str(막힘.value))
    assert 마감뒤 in 사유 and 마감전 in 사유, 사유
    assert "--reset" in 사유, 사유


def test_정규화하면_같은_시각도_다른_값이다() -> None:
    """★ **문자열로 잰다.** `16:00` 과 `16:00:00` 을 같다고 보면 여기서 정규화가 선다."""
    conn = _대역커넥션(_연_설정(walked_now=마감뒤))

    with pytest.raises(WalkedNowConflict):
        stamp_walked_now(conn, sim_run_id=실행축, walked_now="2026-09-13T16:00:00+09:00")


def test_잴_때_행을_잠근다() -> None:
    """★ 두 걷기가 동시에 들어오면 둘 다 「없다」를 보고 각자 적을 수 있다."""
    conn = _대역커넥션(_연_설정())

    stamp_walked_now(conn, sim_run_id=실행축, walked_now=마감뒤)

    assert "FOR UPDATE" in conn.log[0][0], conn.log[0][0]


def test_실행이_없으면_지어내지_않는다() -> None:
    conn = _대역커넥션(None)

    with pytest.raises(LookupError):
        stamp_walked_now(conn, sim_run_id=실행축, walked_now=마감뒤)

    assert conn.쓴것 == []


def test_재고_쓰는_자리는_커밋하지_않는다() -> None:
    """🔴 **커밋은 부르는 쪽이 한다** (`sim_run.create_sim_run` 과 같은 규율)."""
    conn = _대역커넥션(_연_설정())

    stamp_walked_now(conn, sim_run_id=실행축, walked_now=마감뒤)

    assert conn.commits == 0


def test_기본_기록은_한_번_커밋하고_닫는다(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _대역커넥션(_연_설정())
    monkeypatch.setattr(walk_provenance, "get_connection", lambda: conn)

    답 = record_walked_now(sim_run_id=실행축, walked_now=마감뒤)

    assert 답 == "WRITTEN"
    assert (conn.commits, conn.rollbacks, conn.closed) == (1, 0, 1)


def test_기본_기록이_막히면_되돌리고_닫는다(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _대역커넥션(_연_설정(walked_now=마감뒤))
    monkeypatch.setattr(walk_provenance, "get_connection", lambda: conn)

    with pytest.raises(WalkedNowConflict):
        record_walked_now(sim_run_id=실행축, walked_now=마감전)

    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 1, 1)


# ── 문은 안 받는다 ───────────────────────────────────────────────────────


def test_문이_기준_시각을_안_받는다() -> None:
    """🔴 **걷기가 쓴다 — 문이 안 받는다.** 여는 자리에서 받으면 실제로 건 값과 갈린다."""
    문 = _벗긴_원문(_MASTER / "sim_run_runner.py")
    쓰는자리 = _벗긴_원문(_MASTER / "walk_provenance.py")

    assert "WALKED_NOW_KEY" in 쓰는자리, "쓰는 자리에서도 못 찾는다 — 이 검사가 아무것도 안 쟀다"
    assert WALKED_NOW_KEY not in 문 and "WALKED_NOW_KEY" not in 문, "문이 기준 시각을 안다"

    받는칸 = {action.dest for action in sim_run_runner._parser()._actions}
    assert "sim_run_id" in 받는칸, "문의 인자를 못 읽었다 — 이 검사가 아무것도 안 쟀다"
    assert not {"now", "walked_now"} & 받는칸, f"문이 기준 시각을 인자로 받는다: {받는칸}"
