"""개장 관문이 **실행 축을 받는가** (`#555` · `#539` 후속).

🔴 **걷기 아흐레가 판단 한 건도 못 세운 자리다** (실측 `SIM-WALK-2026-FULL` ·
   2026-01-01 ~ 2026-01-09).

```text
날      6일이 열렸다 · 휴장 3일 · 사고 0건
판단    E4_NOT_STARTED 18   사유 "개장 여부를 못 읽었다: AttributeError"
채권    NOT_OPENED 6
```

실 DB 에는 그 아흐레가 **열려 있었다** — `finance_states` 10행 ·
`logistics_runtime_fixture` 10행. 열렸는데 관문이 그것을 못 읽었다.

---

🔴 **원인 — 관문이 표시 객체에 대고 물었다.**

`#539` 뒤로 등록소에 앉는 것은 **어댑터가 아니라 `SimRunBound`(공장을 든 표시)** 다.
실제 어댑터는 `bind_sim_run(impl, sim_run_id)` 로 **부를 때** 선다.

```text
day_open.open_day        🟢 bind_sim_run 을 거친다
day_gate.check_day_gate  🔴 registered() 를 그대로 썼다 → 'SimRunBound' has no attribute 'is_open'
```

★★ **이 결함이 에러를 안 내고 사유 문자열로만 나타났다는 것이 핵심이다.** 관문은
  예외를 값으로 바꾸는 것이 계약이라(*"못 물어본 것과 안 열린 것은 다르다"*)
  여섯 날 내내 같은 사유가 조용히 쌓였다. **그래서 여기서는 사유까지 잰다.**

---

★ **전부 대역이다.** 실 DB 를 타지 않는다 — 등록소에 가짜 `SimRunBound` 를 직접 꽂는다.

★ **`check_day_gate` 를 모듈 최상단에서 가져온다.** `conftest` 의
  `개장_관문을_통과시킨다` 가 `app.master.*` 의 그 이름을 전부 대역으로 바꾸는데,
  이 파일은 fixture 보다 먼저 import 되므로 **진짜를 잰다**
  (`test_day_gate.py` 와 같은 자리·같은 이유).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import unicodedata
from datetime import date, timedelta
from typing import Any

import pytest

from app.master import closing, collection, day_open
from app.master import day_gate as 관문모듈
from app.master.day_gate import check_day_gate
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.sim_run_binding import SimRunBound

AS_OF = date(2026, 1, 7)

#: 실측에서 막힌 그 실행이다.
걷기축 = "SIM-WALK-2026-FULL"

#: 🔴 **운영값(`BURN_IN_SIM_RUN_ID`)과 다르다** — 같으면 축을 상수에서 다시 읽는
#:   뮤턴트가 전부 살아남는다.
남의축 = "SIM-SOMEONE-ELSE"


def _NFC(값: str) -> str:
    """한글 잠금은 정규화해서 비교한다 — 자모 분리 형태로 저장되면 `in` 이 조용히 어긋난다."""
    return unicodedata.normalize("NFC", 값)


class _가짜커넥션:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _축따라_열리는_파트:
    """**`걷기축` 으로 묶였을 때만** `through` 날까지 열려 있는 파트.

    ★ 축이 실제로 실려 갔는지는 이렇게만 보인다. 축을 무시하는 대역을 쓰면
      *"남의 축으로 물었다"* 는 뮤턴트가 살아남는다.
    """

    def __init__(self, axis: str, through: date | None) -> None:
        self.axis = axis
        self._through = through

    def is_open(self, conn: Any, *, as_of: date) -> bool:
        if self.axis != 걷기축:
            return False
        return self._through is not None and as_of <= self._through

    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:  # pragma: no cover
        raise AssertionError("관문은 열지 않는다 — 물어보기만 한다")


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    before = dict(day_open.registered())
    day_open.reset()
    yield
    day_open.reset()
    for part, impl in before.items():
        day_open.register_day_opening(part, impl)


def _등록(through: date | None) -> list[str]:
    """등록소에 **표시 객체**를 꽂는다. 묶인 축을 순서대로 모아 돌려준다."""
    만든축: list[str] = []

    def 공장(axis: str) -> _축따라_열리는_파트:
        만든축.append(axis)
        return _축따라_열리는_파트(axis, through)

    for part in day_open.PARTS:
        day_open.register_day_opening(part, SimRunBound(공장))
    return 만든축


# ── ① 관문이 표시가 아니라 묶인 어댑터에 묻는다 ────────────────────────────


def test_관문이_묶인_어댑터에_묻는다() -> None:
    """🔴 **오늘 걷기를 막은 그 상태를 문다.**

    `registered()` 를 그대로 쓰면 `SimRunBound` 에 `is_open` 을 물어 `AttributeError`
    가 나고, 관문이 그것을 값으로 바꿔 **여섯 날 내내 같은 사유가 조용히 쌓였다.**
    """
    _등록(AS_OF)

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=걷기축)

    assert gate.gate == "PASS", f"열려 있는 날이 막혔다: {gate.result} · {gate.reason!r}"
    assert gate.result == "ALREADY_OPENED"


def test_묶지_않은_실패가_사유로만_숨지_않는다() -> None:
    """★★ **에러가 안 나고 사유 문자열로만 나타난 것이 이 결함의 핵심이다.**

    실측 사유가 그대로 *"개장 여부를 못 읽었다: AttributeError"* 였다.
    """
    _등록(AS_OF)

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=걷기축)

    assert "AttributeError" not in gate.reason, (
        f"표시 객체에 어댑터 메서드를 불렀다: {gate.reason!r}"
    )
    assert _NFC("못 읽었다") not in _NFC(gate.reason)


def test_관문은_여전히_열지_않는다() -> None:
    """★ 묶고 나서도 여는 것은 `open_day` 다 — `_축따라_열리는_파트.open_day` 가 터진다."""
    _등록(AS_OF)

    check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=걷기축)


def test_커넥션을_닫는다() -> None:
    conn = _가짜커넥션()
    _등록(AS_OF)

    check_day_gate(AS_OF, connect=lambda: conn, sim_run_id=걷기축)

    assert conn.closed == 1


# ── ② 넘긴 축의 어댑터가 선다 (남의 축 것이 아니다) ─────────────────────────


def test_넘긴_축으로_어댑터를_묶는다() -> None:
    """🔴 **등록소가 든 상수가 아니라 이번 물음의 축이다.**

    ⚠️ 상수로 묶으면 매입 원장만 새 실행에 앉고 관문은 번인에 남는다 — 그리고
      **아무 오류도 안 난다.**
    """
    만든축 = _등록(AS_OF)

    check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=걷기축)

    assert 만든축 == [걷기축] * len(day_open.PARTS), f"묶인 축이 다르다: {만든축}"
    assert BURN_IN_SIM_RUN_ID not in 만든축


def test_남의_축으로_물으면_안_열린_것으로_보인다() -> None:
    """★ **대역이 축을 실제로 가른다는 것을 여기서 잰다.**

    이 줄이 없으면 위 검사가 *"축이 실렸다"* 가 아니라 *"아무 축이나 됐다"* 를 잰다.
    """
    _등록(AS_OF)

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=남의축)

    assert gate.gate == "BLOCKED"


# ── ③ `_last_opened` 도 묶인 것에 묻는다 ────────────────────────────────────


def test_뒤로_걷는_자리도_묶인_것에_묻는다() -> None:
    """🔴 **막힌 날에만 도는 경로다** — 통과하는 날에는 아무 티도 안 난다.

    ★ 여기가 안 묶이면 *"마지막으로 열린 날"* 을 못 찾고, `NOT_OPENED` 가
      `CONTACT_OPERATOR` 로 올라가 **재시도 한 번 없이 사람을 부른다.**
    """
    _등록(AS_OF - timedelta(days=5))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=걷기축)

    assert gate.gate == "BLOCKED"
    assert gate.result == "NOT_OPENED"
    assert gate.last_opened_date == AS_OF - timedelta(days=5), (
        f"마지막 개장을 못 찾았다: {gate.last_opened_date!r} · {gate.reason!r}"
    )
    assert gate.gap_days == 5
    assert gate.next_action == "RETRY_OPEN_DAY"
    assert "AttributeError" not in gate.reason


def test_뒤로_걷는_자리가_등록소를_다시_읽지_않는다() -> None:
    """★ **원문으로 잠근다.** 여기서 `registered()` 를 다시 부르면 묶는 자리가 둘이 된다."""
    tree = ast.parse(inspect.getsource(관문모듈._last_opened).lstrip())
    불린것 = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "registered" not in 불린것, "뒤로 걷는 자리가 등록소를 다시 읽는다 — 묶인 것을 받는다"


# ── ④ 묶지 않고 쓰는 경로가 하나도 없다 (원문) ──────────────────────────────


def test_원문에_묶지_않고_쓰는_경로가_없다() -> None:
    """🔴 **한 자리라도 남으면 같은 일이 다시 조용히 일어난다.**

    `registered()` 가 돌려준 것을 쓸 수 있는 자리는 둘뿐이다.

    ```text
    part in registered()                         있는지만 묻는다 — 어댑터를 안 부른다
    bind_sim_run(impl, …) for … in registered()  묶어서 쓴다
    ```

    ★ 나머지는 전부 **표시 객체를 어댑터인 척 쓰는 것**이다.
    """
    tree = ast.parse(pathlib.Path(관문모듈.__file__).read_text(encoding="utf-8"))

    def _등록소_호출(node: ast.AST) -> set[int]:
        return {
            id(n)
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "registered"
        }

    전체 = _등록소_호출(tree)

    허용: set[int] = set()
    for node in ast.walk(tree):
        # ① `part in registered()` — 있는지만 묻는다.
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops):
            허용 |= _등록소_호출(node)
        # ② `bind_sim_run` 을 품은 컴프리헨션 — 묶어서 쓴다.
        if isinstance(node, ast.DictComp | ast.ListComp | ast.SetComp | ast.GeneratorExp) and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "bind_sim_run"
            for n in ast.walk(node)
        ):
            허용 |= _등록소_호출(node)

    남은 = 전체 - 허용
    assert not 남은, (
        f"day_gate.py 에 등록소를 묶지 않고 쓰는 자리가 {len(남은)}곳 남았다 — "
        "표시 객체에 어댑터 메서드를 부르면 에러 없이 사유 문자열로만 나타난다"
    )
    assert 전체, "등록소를 아예 안 읽는다 — 이 검사가 아무것도 안 재고 있다"


# ── ⑤ 부르는 자리가 자기 축을 넘긴다 ────────────────────────────────────────


def _관문기록(받은: dict[str, Any]) -> Any:
    from app.master.day_gate import DayGate

    def 관문(as_of: date, **kw: Any) -> DayGate:
        받은.update(kw)
        return DayGate(
            as_of=as_of,
            gate="BLOCKED",
            result="NOT_OPENED",
            reason="대역이 막았다",
            next_action="RETRY_OPEN_DAY",
        )

    return 관문


def test_수금이_자기_축을_관문에_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 안 넘기면 관문이 번인 축으로 묶고, 걷기 실행의 열린 날을 **안 열린 날**로 읽는다."""
    받은: dict[str, Any] = {}
    monkeypatch.setattr(collection, "check_day_gate", _관문기록(받은))

    out = collection.collect_receipts(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=걷기축)

    assert out.status == "NOT_OPENED", "대역 관문이 안 불렸다 — 이 검사가 아무것도 안 잰다"
    assert 받은.get("sim_run_id") == 걷기축, f"수금이 관문에 넘긴 축: {받은!r}"


def test_마감이_자기_축을_관문에_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 마감은 이미 `sim_run_id` 를 인자로 받는다 — 관문에만 안 넘기면 축이 갈린다."""
    받은: dict[str, Any] = {}
    monkeypatch.setattr(closing, "check_day_gate", _관문기록(받은))

    out = closing.close_day(AS_OF, sim_run_id=걷기축, connect=lambda: _가짜커넥션())

    assert out.status == "NOT_OPENED", "대역 관문이 안 불렸다 — 이 검사가 아무것도 안 잰다"
    assert 받은.get("sim_run_id") == 걷기축, f"마감이 관문에 넘긴 축: {받은!r}"


# ── ⑥ 안 바뀐 것 — 예외를 삼키는 태도 · 기본값 ──────────────────────────────


def test_못_물어보면_여전히_사람을_부른다() -> None:
    """🟢 **계약 그대로다.** 못 물어본 것과 안 열린 것은 다르다 — 500 을 내지 않는다."""

    class _터지는파트:
        def is_open(self, conn: Any, *, as_of: date) -> bool:
            raise RuntimeError("연결 없음")

    day_open.register_day_opening("finance", SimRunBound(lambda axis: _터지는파트()))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id=걷기축)

    assert gate.gate == "BLOCKED"
    assert gate.next_action == "CONTACT_OPERATOR"
    assert _NFC("못 읽었다") in _NFC(gate.reason)


def test_축이_비면_막고_사유를_낸다() -> None:
    """🔴 **빈 축으로는 안 묶는다** (`bind_sim_run` 계약).

    ★ **그 실패도 값으로 내려온다.** 묶는 줄이 `try` 밖에 있으면 여기서 `ValueError`
      가 밖으로 나가고, 관문이 500 을 내지 않는다는 계약이 깨진다.
    """
    _등록(AS_OF)

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id="")

    assert gate.gate == "BLOCKED"
    assert gate.next_action == "CONTACT_OPERATOR"
    assert "ValueError" in gate.reason


def test_기본값이_있어_라우터_동작이_안_바뀐다() -> None:
    """⚠️ **`open_day` · `seed_day` 와 같은 태도다.** 라우터가 이 칸을 안 준다.

    ★ 이번 판은 운영 라우터 동작을 안 바꾼다 — 안 주면 번인으로 묶인다.
    """
    기본 = inspect.signature(check_day_gate).parameters["sim_run_id"].default
    assert 기본 == BURN_IN_SIM_RUN_ID

    만든축 = _등록(AS_OF)
    check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert 만든축 == [BURN_IN_SIM_RUN_ID] * len(day_open.PARTS)


def test_등록이_0건이면_축이_없어도_통과한다() -> None:
    """⚠️ **미등록은 통과다.** 묶기 전에 돌아서므로 빈 축으로도 안 터진다."""
    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션(), sim_run_id="")

    assert gate.gate == "PASS"


# ── 부르는 자리가 축을 넘기는가 ─────────────────────────────────────────


#: 🔴 **축을 안 넘기는 것이 알려진 자리.** 여기 이름이 있다는 것은
#:   *"기본값으로 떨어지는 것을 안다"* 는 뜻이지 *"괜찮다"* 는 뜻이 아니다.
#:
#: 🟢 **이제 비었다** (2026-09-11). 마지막 한 자리였던
#:   `revalidation.revalidate_scenario` 가 봉투와 관문에 같은 축을 넘긴다 — 축은 원
#:   실행 행에서 온다. 그 자리를 따로 재는 잠금이
#:   `test_revalidation_carries_run_axis.py` 에 있다.
_축을_안_넘기는_알려진_자리: set[tuple[str, str]] = set()


def test_관문을_부르는_자리가_전부_축을_넘긴다() -> None:
    """★★ **`grep | head` 로 세었다가 다섯을 놓친 자리다.**

    🔴 오늘 관문 호출부를 눈으로 세어 *"둘"* 이라고 적었는데 실제로는 **일곱**이었다.
       잘린 출력을 전부로 읽은 것이고, 그 다섯 중 둘(`service.py` 의 매입·판매)이
       **판단을 막고 있던 바로 그 자리**였다.

    ★ 그래서 사람이 세지 않는다. `app/master/` 전체를 AST 로 읽어
      `check_day_gate(...)` 를 전부 모으고, `sim_run_id` 를 안 넘기는 것이 있으면
      빨개진다 — 새 호출부가 생기는 날 **그 자리에서** 걸린다.
    """
    뿌리 = pathlib.Path(관문모듈.__file__).parent
    샌것: list[tuple[str, int]] = []
    센_것 = 0
    for 파일 in sorted(뿌리.glob("*.py")):
        나무 = ast.parse(파일.read_text(encoding="utf-8"))
        for 마디 in ast.walk(나무):
            if not isinstance(마디, ast.Call):
                continue
            이름 = 마디.func.id if isinstance(마디.func, ast.Name) else None
            if 이름 != "check_day_gate":
                continue
            센_것 += 1
            if (파일.name, 이름) in _축을_안_넘기는_알려진_자리:
                continue
            if not any(kw.arg == "sim_run_id" for kw in 마디.keywords):
                샌것.append((파일.name, 마디.lineno))

    # 🔴 **호출부가 하나도 안 잡히면 이 검사는 아무것도 안 잰다.**
    assert 센_것 >= 6, f"관문 호출부를 {센_것}건밖에 못 찾았다 — 검사가 헛돌고 있다"
    assert not 샌것, f"관문을 부르면서 축을 안 넘기는 자리가 있다: {샌것}"
