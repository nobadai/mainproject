"""**장부 관문 행을 업무 키로 알아본다** (2026-09-09).

🔴 **왜 바꿨나.** 어제까지는 관문 행을 **모양**으로 알아봤다.

```python
row["item"] is None and row["end_code"] == "E4_NOT_STARTED"
```

그런데 **그 모양은 관문 행만의 것이 아니다** (실측).

```text
PROCUREMENT · end_code='E4_NOT_STARTED' · item IS NULL      14행
  품목을 정하기 전에 죽은 옛 매입 실행이다
  ("경계를 내지 못한 에이전트: finance" · "어댑터 미등록: inventory, purchase")
  그 14행 전부 sim_run_id 가 NULL 이라 오늘은 안 샌다
```

★★ **축이 막고 있었던 것이지 모양이 스스로를 증명한 것이 아니다.** 축이 실린 채로
  품목 전에 죽는 실행이 한 번만 나오면 그날이 *"장부가 막았다"* 로 잘못 읽힌다.
  화면에서는 정상 동작으로, 성적표에서는 `gate_blocked` 로 보인다.

🟢 **관문 행에는 그 행만의 업무 키가 있다** — `ledger_gap_request_id(as_of)` 가
  짓고 `record_ledger_gap` 이 `request_id` 로 적는다. 이 파일이 잠그는 것은 넷이다.

```text
① 업무 키의 주인이 하나다 (모양도 하나다)
② 관문 행을 그 키로 알아본다
③ 품목 없는 옛 E4 행은 관문이 아니다      ← 이 판의 핵심
④ 적는 쪽과 되찾는 쪽이 같은 키를 본다
⑤ SQL 과 파이썬이 같은 행에서 같은 답을 낸다
```

⚠️ **DB 를 안 탄다.** 적재도 조회도 전부 대역이고, 행은 검사가 손으로 세운다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

import pytest

from app.master import persistence, procurement_boundary, run_repository, scheduler
from app.master.clock import SEOUL
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.procurement_boundary import read_procurement_boundary
from app.master.run_repository import (
    LEDGER_GAP_END_CODE,
    LEDGER_GAP_REQUEST_LIKE,
    count_runs_by_day,
    is_ledger_gap_request_id,
    ledger_gap_request_id,
)

#: 실행일(금요일). 실행일이라야 *"행이 없다"* 와 *"관문이 막았다"* 가 갈린다.
평일 = date(2026, 1, 23)

축 = "SIM-BURNIN-202512"
ITEMS = ("무", "배추", "양파")


# ── 행 대역 ─────────────────────────────────────────────────────────────


def _row(
    *,
    request_id: str,
    item: str | None,
    end_code: str,
    as_of: date = 평일,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """표의 행 하나. **경계는 안 든다** — 관문 판정까지 내려가야 한다."""
    return {
        "run_id": UUID("11111111-1111-1111-1111-111111111111"),
        "request_id": request_id,
        "as_of": as_of,
        "cycle": "PROCUREMENT",
        "run_seq": 1,
        "item": item,
        "end_code": end_code,
        "runtime_status": "RUNTIME_NOT_READY",
        "coverage_ran": None,
        "coverage_total": None,
        "elapsed_ms": 1,
        "plan": [],
        "request_payload": {},
        "response_payload": {"constraints": constraints or {}},
        "sim_run_id": 축,
        "created_at": datetime(2026, 1, 23, 9, 0, tzinfo=SEOUL),
    }


def _관문행(**kwargs: Any) -> dict[str, Any]:
    """진짜 관문 행. **키를 손으로 안 적는다** — 적는 쪽과 같은 함수를 부른다."""
    as_of = kwargs.pop("as_of", 평일)
    return _row(
        request_id=ledger_gap_request_id(as_of),
        item=None,
        end_code=LEDGER_GAP_END_CODE,
        as_of=as_of,
        **kwargs,
    )


def _옛_매입행(**kwargs: Any) -> dict[str, Any]:
    """🔴 **실측 14행의 모양.** 품목을 정하기 전에 죽은 옛 매입 실행이다.

    관문 행과 `item` · `end_code` 가 같다. **다른 것은 업무 키 하나뿐**이고, 그것이
    이 판이 판정을 옮긴 이유다.
    """
    as_of = kwargs.pop("as_of", 평일)
    return _row(
        request_id=f"REQ-DAILY-{as_of:%Y%m%d}-배추",
        item=None,
        end_code=LEDGER_GAP_END_CODE,
        as_of=as_of,
        **kwargs,
    )


@pytest.fixture
def 표(monkeypatch):
    """`list_runs` 대역. **축과 날짜를 실제로 존중한다.**"""

    def _놓기(*rows: dict[str, Any]):
        def _fake(**kwargs: Any) -> list[dict[str, Any]]:
            return [
                row
                for row in rows
                if (kwargs.get("as_of") is None or row["as_of"] == kwargs["as_of"])
                and (kwargs.get("sim_run_id") is None or row["sim_run_id"] == kwargs["sim_run_id"])
            ]

        monkeypatch.setattr(procurement_boundary, "list_runs", _fake)

    return _놓기


# ══════════════════════════════════════════════════════════════════════
#  ① 업무 키의 주인이 하나다
# ══════════════════════════════════════════════════════════════════════


def test_업무_키의_모양이_날짜를_품는다():
    """🔴 **날짜가 키 안에 있어야 하루에 한 벌이 된다.**

    `record_ledger_gap` 이 *"그날 게이트 행이 이미 있나"* 를 이 키로 묻는다
    (`_ledger_gap_already_recorded`). 형식에서 날짜가 흐려지면 다른 날이 같은 키를
    갖고, 그러면 둘째 날의 행이 *"이미 있다"* 로 조용히 사라진다.
    """
    assert ledger_gap_request_id(date(2026, 9, 8)) == "REQ-DAILY-20260908-LEDGER-GAP"
    assert ledger_gap_request_id(date(2026, 9, 18)) == "REQ-DAILY-20260918-LEDGER-GAP"
    assert ledger_gap_request_id(date(2026, 9, 8)) != ledger_gap_request_id(date(2026, 9, 18))


def test_스케줄러가_저장소의_키를_그대로_쓴다():
    """🔴 **주인은 `run_repository` 하나다** (2026-09-09 에 옮겼다).

    되찾는 쪽이 저장소라 저장소가 키를 소유해야 한다 — 반대로 하면
    `run_repository → scheduler → persistence → run_repository` 고리가 생긴다.
    이름은 `scheduler` 에 그대로 살아 있고, **같은 함수여야** 두 벌이 아니다.
    """
    assert scheduler.ledger_gap_request_id is ledger_gap_request_id
    assert "ledger_gap_request_id" in scheduler.__all__


def test_키를_알아보는_꼬리가_키와_같은_데서_나온다():
    """★ 짓는 쪽과 알아보는 쪽이 갈리면 아무것도 안 잰다.

    ★ **자기 생존.** 아무 문자열이나 참이 되는 판정이면 먼저 실패한다.
    """
    assert is_ledger_gap_request_id(ledger_gap_request_id(평일)) is True
    assert is_ledger_gap_request_id("REQ-DAILY-20260123-배추") is False
    assert is_ledger_gap_request_id(None) is False
    assert is_ledger_gap_request_id("") is False


# ══════════════════════════════════════════════════════════════════════
#  ② ③ 되찾는 쪽 — 화면이 읽는 자리
# ══════════════════════════════════════════════════════════════════════


def test_관문_행을_업무_키로_알아본다(표):
    """🟢 진짜 관문 행은 `LEDGER_GAP` 이다 — 화면이 *"장부가 막았다"* 를 그린다."""
    표(_관문행())

    assert read_procurement_boundary(as_of=평일, sim_run_id=축).absent_reason == "LEDGER_GAP"


def test_품목_없는_옛_E4_행은_관문이_아니다(표):
    """🔴🔴 **이 판의 핵심이다.**

    실측 14행이 관문 행과 `item` · `end_code` 가 같다. 모양으로 잡으면 그중 하나만
    축을 달고 나타나도 그날이 *"장부가 막았다"* 가 되고, 그것은 없는 사실이다.
    실행일에 행이 있었지만 경계를 못 냈다면 답은 `NO_PROCUREMENT_RUN` 이다.

    ★ **자기 생존.** 같은 검사가 진짜 관문 행에서는 `LEDGER_GAP` 이 나오는 것까지
      확인한다 — 관문을 영영 못 잡는 구현으로도 초록이 되지 않는다.
    """
    표(_옛_매입행())
    옛행 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 옛행.absent_reason == "NO_PROCUREMENT_RUN", "옛 매입 행을 관문으로 읽었다"

    표(_관문행())
    진짜 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 진짜.absent_reason == "LEDGER_GAP", "진짜 관문 행도 못 잡는다"


def test_옛_행과_관문_행이_같이_있어도_관문만_잡는다(표):
    """★ 둘이 한 날에 같이 앉을 수 있다 — 그때도 답이 흔들리면 안 된다."""
    표(_옛_매입행(), _관문행())

    assert read_procurement_boundary(as_of=평일, sim_run_id=축).absent_reason == "LEDGER_GAP"


# ══════════════════════════════════════════════════════════════════════
#  ④ 적는 쪽과 되찾는 쪽이 **같은 키**를 본다
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ 검사가 키를 손으로 적으면 *"내가 적은 값과 같은가"* 가 되어 아무것도 안 잰다.
#    진짜 걷기를 돌려 **표에 적히려던 값**을 받아내고, 그 값으로 행을 세워 되찾는다.


@dataclass
class _Out:
    status: str
    reason: str = ""


class _Spy:
    def __init__(self, out: object = None) -> None:
        self.out = out
        self.calls: list[object] = []

    def __call__(self, arg, *args, **kwargs):
        self.calls.append(arg)
        return self.out


class _Calendar:
    def is_market_open(self, as_of: date) -> bool:
        return True


def _적힌_관문행(monkeypatch) -> dict[str, Any]:
    """장부가 막힌 하루를 **진짜로 걸어서** 표에 적히려던 칸을 그대로 받아낸다.

    `run_scheduled_day → record_ledger_gap → try_save_run` 이 전부 진짜다 — 대역은
    부서 함수와 표 한 줄뿐이다.
    """
    적힌것: dict[str, Any] = {}
    monkeypatch.setattr(persistence, "history_enabled", lambda: True)
    monkeypatch.setattr(persistence, "list_runs", lambda **kw: [])
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: 적힌것.update(kw) or "RUN-1")

    준비 = DayForecastReadiness(
        as_of=평일,
        readiness="ALL_READY",  # type: ignore[arg-type]
        items=tuple(
            ItemForecastGate(item=item, as_of=평일, readiness="READY", grade="MEASURED")  # type: ignore[arg-type]
            for item in ITEMS
        ),
    )
    action = scheduler.plan_next_action(
        now=datetime(평일.year, 평일.month, 평일.day, 9, 30, tzinfo=SEOUL),
        as_of=평일,
        calendar=_Calendar(),
        gate_result=준비,
    )
    scheduler.run_scheduled_day(
        action,
        open_day_fn=_Spy(_Out("OPENED")),
        receive_fn=_Spy(_Out("BLOCKED")),
        issue_fn=_Spy(_Out("ISSUED")),
        collect_fn=_Spy(_Out("COLLECTED")),
        procure_fn=_Spy(_Out("RAN")),
        outbound_fn=_Spy(_Out("NOTHING_DUE")),
        items=ITEMS,
    )
    return 적힌것


def test_적는_쪽이_되찾는_키를_적는다(monkeypatch):
    """🔴 **적는 쪽과 되찾는 쪽이 갈리면 이 판 전체가 무의미하다.**

    ★ **자기 생존.** 적히려던 칸을 못 받아냈으면 먼저 실패한다 — 빈 dict 로 통과하는
      비교를 만들지 않는다.
    """
    적힌것 = _적힌_관문행(monkeypatch)

    assert 적힌것, "관문 행이 표로 안 갔다 — 아무것도 안 쟀다"
    assert is_ledger_gap_request_id(적힌것["request_id"]), f"적은 키를 못 알아본다: {적힌것}"


def test_적힌_그_행을_화면이_관문으로_읽는다(monkeypatch):
    """🟢 **적힌 칸 그대로** 되찾는다 — 검사가 행을 지어내지 않는다."""
    적힌것 = _적힌_관문행(monkeypatch)
    행 = _row(
        request_id=적힌것["request_id"],
        item=적힌것.get("item"),
        end_code=적힌것["end_code"],
        as_of=적힌것["as_of"],
    )
    monkeypatch.setattr(procurement_boundary, "list_runs", lambda **kw: [행])

    답 = read_procurement_boundary(as_of=평일, sim_run_id=축)

    assert 답.absent_reason == "LEDGER_GAP"


# ══════════════════════════════════════════════════════════════════════
#  ⑤ SQL 과 파이썬이 **같은 행에서 같은 답**을 낸다
# ══════════════════════════════════════════════════════════════════════


def _asked(monkeypatch) -> tuple[str, tuple]:
    잡힘: dict[str, Any] = {}

    def _fake(query, params):
        잡힘["query"] = query.as_string(None)
        잡힘["params"] = params
        return []

    monkeypatch.setattr(run_repository, "fetch_all", _fake)
    count_runs_by_day(sim_run_id=축, start=평일, end=평일)
    return 잡힘["query"], 잡힘["params"]


def _LIKE(pattern: str, value: str) -> bool:
    """`LIKE` 를 흉내 낸다. **패턴을 검사가 짓지 않는다** — SQL 에 실린 것을 받는다.

    ⚠️ 여기서 다루는 패턴은 꼬리 하나(`%...`)뿐이라 `%` 만 옮긴다. `_` 나 이스케이프를
      쓰기 시작하면 이 흉내가 더는 못 따라가고, 그때는 진짜 DB 로 재야 한다.
    """
    assert "_" not in pattern and pattern.count("%") == 1 and pattern.startswith("%"), (
        f"이 흉내가 못 따라가는 패턴이다: {pattern!r}"
    )
    return value.endswith(pattern[1:])


def test_SQL_도_업무_키로_묻는다(monkeypatch):
    """🔴 **날짜 형식을 SQL 에 다시 적지 않는다.** 두 벌이 되면 한쪽만 바뀌는 날이 온다."""
    query, params = _asked(monkeypatch)

    assert "request_id LIKE %s" in query, f"업무 키로 안 묻는다: {query}"
    assert "%-LEDGER-GAP" in params, f"꼬리를 안 넘긴다: {params}"
    assert LEDGER_GAP_REQUEST_LIKE in params
    assert "REQ-DAILY" not in query, "날짜 형식이 SQL 에도 적혔다 — 두 벌이다"


def test_성적표와_화면이_같은_행에서_같은_답을_낸다(monkeypatch):
    """🔴 **두 쪽이 갈리면 화면은 정상 동작이라 하고 성적표는 막혔다고 한다.**

    SQL 쪽 답은 표에 실려 나간 패턴으로, 파이썬 쪽 답은 판정 함수로 각각 낸다 —
    한쪽만 고치는 변이가 여기서 빨간불이 된다.

    ★ **자기 생존.** 표본에 참·거짓이 둘 다 없으면 먼저 실패한다 — 전부 거짓인
      표본으로 초록이 되는 비교를 만들지 않는다.
    """
    query, params = _asked(monkeypatch)
    assert "request_id LIKE %s" in query, f"SQL 이 업무 키를 안 본다: {query}"
    [패턴] = [값 for 값 in params if isinstance(값, str) and 값.startswith("%")]

    표본 = (
        ledger_gap_request_id(평일),
        ledger_gap_request_id(date(2026, 1, 30)),
        "REQ-DAILY-20260123-배추",
        "REQ-DAILY-20260123-무",
        "REQ-AXIS-1",
    )
    SQL쪽 = [_LIKE(패턴, 키) for 키 in 표본]
    파이썬쪽 = [is_ledger_gap_request_id(키) for 키 in 표본]

    assert True in 파이썬쪽 and False in 파이썬쪽, "표본이 한쪽뿐이다 — 아무것도 안 갈랐다"
    assert SQL쪽 == 파이썬쪽, f"같은 행에서 답이 갈린다: {list(zip(표본, SQL쪽, 파이썬쪽))}"
