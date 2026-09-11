"""업무 키가 **실행 축**을 단다 (2026-09-11).

🔴🔴 **새 실행이 옛 실행의 승인을 물려받아 자기 원장을 못 만들던 자리다.**

```text
실측  새 실행을 열고 2주를 걸었더니
      종료코드  E1_APPROVED 24
      승인어휘  ALREADY_DECIDED 24 · RECORDED 0
      전이      {}                      ← 원장에 한 건도 안 닿았다

      업무키 `REQ-DAILY-20260105-무`  행 16건 · 실행 5개에 걸쳐 있음
      그 업무키의 master_decisions 행: 1건
```

`decision_repository.list_decisions` 는 `WHERE request_id = %s` 뿐이라 **축을 안
본다.** 업무 키에 축이 없으니 A 실행의 승인이 B 실행에서 `ALREADY_DECIDED` 로 읽혔다.

---

무엇을 재나.

```text
① 실행이 다르면 업무 키가 **다르다**
② 같은 실행·같은 날·같은 품목이면 업무 키가 **같다**  (멱등 유지)
③ 축을 안 넘기면 **문법이 막는다**  (기본값 없음 · 빈 축도 막는다)
④ 부르는 자리가 **전부** 넘긴다  (원문을 AST 로 센다)
⑤ 꼬리로 찾는 SQL 이 **여전히 찾는다**  (`LEDGER_GAP_REQUEST_LIKE`)
```

★ 전부 **대역**이다. 실 DB 를 안 탄다 — 여기서 DB 를 타면 검사가 환경을 잰다.

⚠️ **한글 잠금은 NFC 로 맞춘다.** 파일 인코딩이 어떻든 같은 글자를 재야 한다.
"""

from __future__ import annotations

import ast
import inspect
import unicodedata
from datetime import date
from pathlib import Path

import pytest

from app.master import scheduler
from app.master.run_repository import (
    DAILY_REQUEST_HEAD,
    LEDGER_GAP_REQUEST_LIKE,
    build_request_id,
    is_ledger_gap_request_id,
    ledger_gap_request_id,
)
from app.master.scheduler import daily_request_id, daily_sales_request_id

_APP = Path(__file__).resolve().parents[2] / "app"

평일 = date(2026, 1, 5)
품목 = unicodedata.normalize("NFC", "무")

실행A = "SIM-WALK-2026-FULL"
실행B = "SIM-WALK-2026-FINAL"

#: 업무 키를 짓는 **공개 함수 셋**. 부르는 자리를 셀 때 이 이름을 본다.
키함수 = ("daily_request_id", "daily_sales_request_id", "ledger_gap_request_id")


def _NFC(값: str) -> str:
    return unicodedata.normalize("NFC", 값)


# ══════════════════════════════════════════════════════════════════════
#  ① 실행이 다르면 키가 다르다
# ══════════════════════════════════════════════════════════════════════


def test_실행이_다르면_매입_업무_키가_다르다() -> None:
    """🔴🔴 **이것이 이 판의 전부다.**

    같으면 B 실행의 승인 문이 A 실행의 결정을 보고 `ALREADY_DECIDED` 로 돌아선다.
    """
    A = daily_request_id(평일, 품목, sim_run_id=실행A)
    B = daily_request_id(평일, 품목, sim_run_id=실행B)

    assert A != B, f"실행이 달라도 키가 같다 — 승인이 물려받는다: {A!r}"
    assert 실행A in A and 실행B in B, f"키가 축을 안 품는다: {A!r} · {B!r}"


def test_실행이_다르면_판매_업무_키가_다르다() -> None:
    """🔴 판매도 같은 자리다 — 판매 승인도 남의 실행 것을 물려받는다."""
    A = daily_sales_request_id(평일, 품목, sim_run_id=실행A)
    B = daily_sales_request_id(평일, 품목, sim_run_id=실행B)

    assert A != B, f"실행이 달라도 판매 키가 같다: {A!r}"


def test_실행이_다르면_장부_관문_키가_다르다() -> None:
    """🔴 관문 키도 같은 자리다 (`run_repository`).

    ⚠️ 안 갈리면 *"그날 게이트 행이 이미 있나"* 가 **남의 실행 행**에 참이 되고,
      오늘 실행의 관문 행이 조용히 안 남는다.
    """
    A = ledger_gap_request_id(평일, sim_run_id=실행A)
    B = ledger_gap_request_id(평일, sim_run_id=실행B)

    assert A != B, f"실행이 달라도 관문 키가 같다: {A!r}"


def test_매입_판매_관문_셋이_서로_다르다() -> None:
    """🔴 같은 실행·같은 날인데 셋이 겹치면 유일 인덱스가 뒤엣것을 막는다."""
    셋 = {
        daily_request_id(평일, 품목, sim_run_id=실행A),
        daily_sales_request_id(평일, 품목, sim_run_id=실행A),
        ledger_gap_request_id(평일, sim_run_id=실행A),
    }

    assert len(셋) == 3, f"셋 중 둘이 같은 키다: {sorted(셋)}"


# ══════════════════════════════════════════════════════════════════════
#  ② 멱등 — 축은 시각이 아니다
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("지은것", "기댄것"),
    [
        (daily_request_id(평일, 품목, sim_run_id=실행A), f"REQ-DAILY-{실행A}-20260105-{품목}"),
        (
            daily_sales_request_id(평일, 품목, sim_run_id=실행A),
            f"REQ-DAILY-SALES-{실행A}-20260105-{품목}",
        ),
        (
            ledger_gap_request_id(평일, sim_run_id=실행A),
            f"REQ-DAILY-{실행A}-20260105-LEDGER-GAP",
        ),
    ],
)
def test_키에_네_조각_말고는_아무것도_안_들어간다(지은것: str, 기댄것: str) -> None:
    """🔴🔴 **멱등 규율을 지키는 유일한 잠금이다** (2026-09-11 변이로 확인).

    ⚠️ *"두 번 불러서 같은가"* 로는 **시각이 낀 키를 못 잡는다.** 윈도우 시계 눈금이
      15.6ms 라 붙어 있는 두 호출이 같은 마이크로초를 읽고, `%H%M%S%f` 를 끼워 넣은
      변이가 그 비교를 **초록으로 통과했다.** 그래서 모양 전체를 문자로 잠근다.

    ★ 여기 적힌 네 조각(머리·축·날짜·꼬리)이 전부다. 무엇이 하나라도 더 끼면 같은 날
      두 번 깨어날 때 키가 갈리고, `master_agent_runs_run_request_unique` 가 두 번째를
      못 막는다 — 멱등이 인덱스가 아니라 *"두 번 안 깨우기"* 에 걸리게 된다.
    """
    assert _NFC(지은것) == _NFC(기댄것)


@pytest.mark.parametrize("짓기", [daily_request_id, daily_sales_request_id])
def test_같은_실행_같은_날_같은_품목이면_키가_같다(짓기) -> None:
    """🔴 **멱등 규율은 그대로다.** 축이 시각처럼 굴면 안 된다.

    ⚠️ **이 비교만으로는 부족하다** — 위 모양 잠금이 진짜 잡는 자리다. 여기는
      *"같은 입력이면 같은 답"* 이라는 뜻을 남겨 둘 뿐이다.
    """
    assert 짓기(평일, 품목, sim_run_id=실행A) == 짓기(평일, 품목, sim_run_id=실행A)


def test_같은_실행이면_관문_키도_같다() -> None:
    """🔴 하루에 한 벌이어야 *"그날 게이트 행이 이미 있나"* 를 물을 수 있다."""
    assert ledger_gap_request_id(평일, sim_run_id=실행A) == ledger_gap_request_id(
        평일, sim_run_id=실행A
    )


def test_날짜와_품목은_여전히_키를_가른다() -> None:
    """★ **자기 생존.** 축만 보고 날짜·품목을 흘리는 키면 여기서 먼저 실패한다."""
    기준 = daily_request_id(평일, 품목, sim_run_id=실행A)

    assert 기준 != daily_request_id(date(2026, 1, 6), 품목, sim_run_id=실행A), "날짜를 흘린다"
    assert 기준 != daily_request_id(평일, _NFC("배추"), sim_run_id=실행A), "품목을 흘린다"


# ══════════════════════════════════════════════════════════════════════
#  ③ 축을 안 넘기면 문법이 막는다
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("짓기", "인자"),
    [
        (daily_request_id, (평일, 품목)),
        (daily_sales_request_id, (평일, 품목)),
        (ledger_gap_request_id, (평일,)),
    ],
)
def test_축을_안_넘기면_부를_수가_없다(짓기, 인자) -> None:
    """🔴 **기본값을 두지 않는다.**

    기본값이 있으면 축을 빠뜨린 호출이 조용히 옛 모양으로 떨어지고, 그 실패는
    `ALREADY_DECIDED` 로만 나타나 **에러가 안 난다.** 문법이 막게 한다.
    """
    with pytest.raises(TypeError):
        짓기(*인자)


@pytest.mark.parametrize("짓기", 키함수)
def test_축이_키워드_전용이고_기본값이_없다(짓기: str) -> None:
    """🔴 **서명 자체를 잠근다.** 위치 인자로 받으면 순서가 바뀌는 날 조용히 밀린다."""
    파라 = inspect.signature(getattr(scheduler, 짓기)).parameters

    assert "sim_run_id" in 파라, f"{짓기} 가 축을 아예 안 받는다"
    축 = 파라["sim_run_id"]
    assert 축.kind is inspect.Parameter.KEYWORD_ONLY, f"{짓기} 의 축이 키워드 전용이 아니다"
    assert 축.default is inspect.Parameter.empty, f"{짓기} 의 축에 기본값이 있다"


@pytest.mark.parametrize("빈축", ["", "   "])
@pytest.mark.parametrize(
    ("짓기", "인자"),
    [
        (daily_request_id, (평일, 품목)),
        (daily_sales_request_id, (평일, 품목)),
        (ledger_gap_request_id, (평일,)),
    ],
)
def test_빈_축은_거절한다(짓기, 인자, 빈축: str) -> None:
    """🔴 **빈 축을 메우지 않는다.**

    `REQ-DAILY--20260105-무` 는 축을 안 실은 **모든** 호출자에게서 같은 문자열이라,
    실행이 달라도 키가 겹친다 — 이 판이 없애려는 바로 그 모양이다.
    """
    with pytest.raises(ValueError):
        짓기(*인자, sim_run_id=빈축)


# ══════════════════════════════════════════════════════════════════════
#  ④ 부르는 자리가 전부 넘긴다 — 원문을 AST 로 센다
# ══════════════════════════════════════════════════════════════════════


def _키를_짓는_호출() -> list[tuple[str, int, str, bool]]:
    """운영 코드에서 키 함수를 부르는 자리 전부. **문자열 검색이 아니라 AST 다.**

    ⚠️ 문자열로 세면 주석과 docstring 이 같이 걸린다 — 이 파일들은 주석이 길다.
    """
    나온것: list[tuple[str, int, str, bool]] = []
    for 파일 in sorted(_APP.rglob("*.py")):
        나무 = ast.parse(파일.read_text(encoding="utf-8"))
        for 마디 in ast.walk(나무):
            if not isinstance(마디, ast.Call):
                continue
            함수 = 마디.func
            이름 = (
                함수.id
                if isinstance(함수, ast.Name)
                else (함수.attr if isinstance(함수, ast.Attribute) else None)
            )
            if 이름 not in 키함수:
                continue
            넘겼나 = any(kw.arg == "sim_run_id" for kw in 마디.keywords)
            나온것.append((파일.name, 마디.lineno, 이름, 넘겼나))
    return 나온것


def test_운영_코드의_모든_호출이_축을_넘긴다() -> None:
    """🔴 **한 자리만 빠져도 그 사이클이 통째로 남의 실행 결정을 물려받는다.**

    ★ **자기 생존.** 호출을 하나도 못 찾으면 먼저 실패한다 — 0건을 세고 초록이 되는
      검사를 만들지 않는다.
    """
    호출 = _키를_짓는_호출()

    assert 호출, "운영 코드에서 키 함수 호출을 하나도 못 찾았다 — 스캐너가 죽었다"
    빠진것 = [(f, n, 이름) for f, n, 이름, 넘겼나 in 호출 if not 넘겼나]
    assert 빠진것 == [], f"축을 안 넘기는 호출이 있다: {빠진것}"


def test_세_사이클이_다_불리고_있다() -> None:
    """★ **자기 생존.** 위 검사가 *"매입 하나만 세고 초록"* 이 되지 않게 잠근다.

    매입·판매·관문 셋 다 부르는 자리가 있어야 `#` 이 고친 자리가 전부 덮인다.
    """
    불린이름 = {이름 for _, _, 이름, _ in _키를_짓는_호출()}

    안불린것 = set(키함수) - 불린이름
    assert 안불린것 == set(), f"운영 코드가 안 부르는 키 함수가 있다: {안불린것}"


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 꼬리로 찾는 SQL 이 여전히 찾는다
# ══════════════════════════════════════════════════════════════════════


def _LIKE(패턴: str, 값: str) -> bool:
    """`LIKE '%꼬리'` 흉내. **이 모양만 따라간다** — 다른 패턴이면 먼저 실패한다."""
    assert 패턴.startswith("%") and 패턴.count("%") == 1 and "_" not in 패턴, (
        f"이 흉내가 못 따라가는 패턴이다: {패턴!r}"
    )
    return 값.endswith(패턴[1:])


def test_축이_붙어도_꼬리_조회가_관문_행을_찾는다() -> None:
    """🔴 **축을 꼬리에 붙이면 이 조회가 에러 없이 늘 거짓이 된다.**

    성적표의 `gate_blocked`(`count_runs_by_day` 의 `request_id LIKE %s`)와 화면의
    `procurement_boundary` 가 같은 꼬리를 문다. 축이 꼬리 뒤로 가면 둘 다 한 행도
    못 집고, **관문이 막은 날이 통째로 안 보인다.**
    """
    관문키 = ledger_gap_request_id(평일, sim_run_id=실행A)

    assert _LIKE(LEDGER_GAP_REQUEST_LIKE, 관문키), f"SQL 이 관문 키를 못 찾는다: {관문키!r}"
    assert is_ledger_gap_request_id(관문키), f"파이썬이 관문 키를 못 알아본다: {관문키!r}"


def test_축이_붙은_품목_키는_관문으로_안_읽힌다() -> None:
    """🔴 **자기 생존.** 아무거나 참이 되는 꼬리 조회면 여기서 먼저 실패한다."""
    품목키 = daily_request_id(평일, 품목, sim_run_id=실행A)
    판매키 = daily_sales_request_id(평일, 품목, sim_run_id=실행A)

    for 키 in (품목키, 판매키):
        assert not _LIKE(LEDGER_GAP_REQUEST_LIKE, 키), f"품목 키가 관문으로 읽힌다: {키!r}"
        assert not is_ledger_gap_request_id(키), f"품목 키가 관문으로 읽힌다: {키!r}"


def test_SQL쪽과_파이썬쪽이_같은_행에서_같은_답을_낸다() -> None:
    """🔴 한쪽만 갈리면 화면은 정상 동작이라 하고 성적표는 막혔다고 한다.

    ★ **자기 생존.** 표본에 참·거짓이 둘 다 없으면 먼저 실패한다.
    """
    표본 = (
        ledger_gap_request_id(평일, sim_run_id=실행A),
        ledger_gap_request_id(평일, sim_run_id=실행B),
        daily_request_id(평일, 품목, sim_run_id=실행A),
        daily_sales_request_id(평일, 품목, sim_run_id=실행A),
    )
    SQL쪽 = [_LIKE(LEDGER_GAP_REQUEST_LIKE, 키) for 키 in 표본]
    파이썬쪽 = [is_ledger_gap_request_id(키) for 키 in 표본]

    assert True in 파이썬쪽 and False in 파이썬쪽, "표본이 한쪽뿐이다 — 아무것도 안 갈랐다"
    assert SQL쪽 == 파이썬쪽, f"같은 행에서 답이 갈린다: {list(zip(표본, SQL쪽, 파이썬쪽))}"


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 자리 배치의 주인이 하나다
# ══════════════════════════════════════════════════════════════════════


def test_축이_꼬리_앞에_붙는다() -> None:
    """🔴 **꼬리가 그대로 남아야 ⑤ 의 조회가 산다.**

    키의 머리는 `REQ-DAILY` 이고 꼬리는 품목(또는 `LEDGER-GAP`)이다. 축은 그 사이에
    들어간다 — 뒤로 가면 꼬리 조회가 죽고, 머리를 밀어내면 이 키가 무엇인지 사람이
    못 읽는다.
    """
    키 = daily_request_id(평일, 품목, sim_run_id=실행A)

    assert 키.startswith(f"{DAILY_REQUEST_HEAD}-"), f"머리가 밀렸다: {키!r}"
    assert _NFC(키).endswith(f"-{품목}"), f"품목이 꼬리가 아니다: {키!r}"
    assert 키.index(실행A) < 키.index("20260105"), f"축이 날짜 뒤에 붙었다: {키!r}"


def test_세_키가_자리_배치를_한_함수에서_받는다() -> None:
    """★ **두 벌이 되면 한쪽만 축을 옮기는 날 ⑤ 가 조용히 죽는다.**

    셋 다 `build_request_id` 가 낸 모양이어야 한다 — 각자 f-string 을 쓰면 여기서
    갈린다.
    """
    기대 = {
        daily_request_id(평일, 품목, sim_run_id=실행A): build_request_id(
            head=DAILY_REQUEST_HEAD, as_of=평일, sim_run_id=실행A, tail=품목
        ),
        daily_sales_request_id(평일, 품목, sim_run_id=실행A): build_request_id(
            head=f"{DAILY_REQUEST_HEAD}-SALES", as_of=평일, sim_run_id=실행A, tail=품목
        ),
        ledger_gap_request_id(평일, sim_run_id=실행A): build_request_id(
            head=DAILY_REQUEST_HEAD, as_of=평일, sim_run_id=실행A, tail="LEDGER-GAP"
        ),
    }

    갈린것 = [(지은것, 기댄것) for 지은것, 기댄것 in 기대.items() if _NFC(지은것) != _NFC(기댄것)]
    assert 갈린것 == [], f"자리 배치가 두 벌이다: {갈린것}"
