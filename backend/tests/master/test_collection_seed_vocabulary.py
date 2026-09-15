"""🔴 **같은 사실에 두 낱말을 쓰지 않는다** — 수금 두 경로의 어휘를 맞물려 둔다.

```text
app/master/collection_seed.py       seed_day        개장할 때 사건을 **만든다**
app/master/finance_collection.py    collect         그 사건을 **실행한다**
```

  ★ 둘은 같은 자리에서 같은 것을 막는다 — 재무 축을 못 읽었을 때와 축이 마스터
    것과 다를 때다. **사유 문장은 글자까지 같다.** 그런데 상태 낱말이 갈리면 한
    저장소 안에서 같은 사실이 두 낱말로 나가고, 그 위에서 결정이 뒤집힌다.

---

⚠️ **지금은 두 파일이 손으로 맞춰져 있다.** 잠그지 않으면 한쪽만 바뀌는 날이 온다 —
  이 파일이 그 날을 잡는다.

```text
축 불일치      seed_day BLOCKED     ==  collect BLOCKED     ← 같은 낱말이어야 한다
축 조회 실패    seed_day UNREADABLE  !=  collect BLOCKED     ← 아래를 보라
```

🔴 **축 조회 실패에서 둘이 갈리는 것은 고른 결과가 아니다.**
  `CollectionPartOut.status` 는 `COLLECTED · NOTHING_DUE · BLOCKED` 셋뿐이고 그 표에
  **`UNREADABLE` 이라는 낱말이 아예 없다.** `collect` 는 쓸 수 있는 낱말 중 가장 넓은
  것을 쓴 것이다. 그래서 이 파일은 **그 셋임을 같이 못 박는다** — 그 표에 낱말이
  생기는 날 이 검사가 red 가 되고, 그때 둘을 다시 맞춰야 한다.

---

★★ **자기 생존 검사를 같이 둔다.** 대역이 두 경로를 실제로 태웠는지 먼저 센다.
  한쪽만 태우고도 초록이 나면 이 파일은 **아무것도 재지 않는 초록**이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, get_args, get_origin

from app.finance.db import FinanceDataNotReady, FinanceRuntimeAxis
from app.master.collection import CollectionPartOut
from app.master.collection_seed import SeedStatus, seed_day
from app.master.finance_collection import FinanceCollectionAdapter
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

AS_OF = date(2026, 1, 10)
축_모드 = "LOAN_BASELINE"
남의_실행 = "SIM-SOMEONE-ELSE"
모호_사유 = "finance_runtime_axis_ambiguous"

축_조회_실패 = "축 조회 실패"
축_불일치 = "축 불일치"


@dataclass
class _센다:
    """축을 읽어 간 횟수. **0 건을 세면 공짜 초록이 난다.**"""

    호출: int = 0


def _못_읽는_축(센다: _센다):
    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        센다.호출 += 1
        raise FinanceDataNotReady(모호_사유)

    return read_axis


def _남의_축(센다: _센다):
    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        센다.호출 += 1
        return FinanceRuntimeAxis(sim_run_id=남의_실행, financing_mode=축_모드)

    return read_axis


대역들 = ((축_조회_실패, _못_읽는_축), (축_불일치, _남의_축))


def _안_불린다(*_: Any, **__: Any) -> Any:
    """축 검사에서 걸리므로 **여기까지 오면 안 된다.**"""
    raise AssertionError("축이 안 맞는데 DB 로 갔다")


@dataclass(frozen=True)
class _관측:
    대역: str
    seed_status: str
    seed_reason: str
    part_status: str
    part_reason: str


def _두_경로를_태운다(대역: str, 축을_만든다: Any) -> _관측:
    """한 대역으로 **두 함수를 다 몰고** 나온 답을 적는다."""
    센다 = _센다()

    seed = seed_day(
        AS_OF,
        sim_run_id=BURN_IN_SIM_RUN_ID,
        connect=_안_불린다,
        read_axis=축을_만든다(센다),
    )
    part = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=축을_만든다(센다),
        load_events=_안_불린다,
    ).collect(object(), as_of=AS_OF)

    # ★★ **자기 생존 검사.** 두 경로가 실제로 축을 읽으러 갔는가.
    assert 센다.호출 == 2, (
        f"{대역}: 두 경로를 다 태우지 않았다 (축을 읽어 간 횟수 {센다.호출}회) —"
        " 한쪽만 태우면 이 파일은 아무것도 재지 않는다"
    )
    return _관측(
        대역=대역,
        seed_status=seed.status,
        seed_reason=seed.reason,
        part_status=part.status,
        part_reason=part.reason,
    )


def _잰다() -> dict[str, _관측]:
    관측들 = [_두_경로를_태운다(대역, 축을_만든다) for 대역, 축을_만든다 in 대역들]
    # ★★ **자기 생존 검사.** 대역이 실제로 둘 걸렸는가.
    assert len(관측들) == 2, f"대역을 다 안 태웠다: {[관측.대역 for 관측 in 관측들]}"
    return {관측.대역: 관측 for 관측 in 관측들}


def test_같은_사유_문장이_두_파일에서_나온다() -> None:
    """🔴 **두 파일이 같은 사실을 말하고 있다는 근거가 먼저다.**

    사유 문장이 갈리면 아래 낱말 대조는 **다른 두 사실**을 비교하는 셈이 된다.
    """
    관측 = _잰다()

    for 대역, 하나 in 관측.items():
        assert 하나.seed_reason, f"{대역}: seed_day 가 사유를 안 남겼다"
        assert 하나.part_reason, f"{대역}: collect 가 사유를 안 남겼다"
        assert 하나.seed_reason == 하나.part_reason, (
            f"{대역}: 같은 자리인데 사유가 갈렸다\n"
            f"  seed_day: {하나.seed_reason!r}\n"
            f"  collect : {하나.part_reason!r}"
        )

    assert 모호_사유 in 관측[축_조회_실패].seed_reason
    assert 남의_실행 in 관측[축_불일치].seed_reason


def test_축_불일치는_두_파일에서_같은_낱말이다() -> None:
    """🔴 **`BLOCKED` 하나다.** 한쪽이 `NOT_ATTEMPTED` 로 답하면 *"시도할 이유가
    없었다"* 로 읽히고, **막은 사고가 안 일어난 일이 된다.**
    """
    하나 = _잰다()[축_불일치]

    assert 하나.seed_status == 하나.part_status, (
        "같은 사실에 두 낱말이 나갔다:"
        f" seed_day={하나.seed_status} · collect={하나.part_status}"
    )
    assert 하나.seed_status == "BLOCKED", f"막은 것이 {하나.seed_status} 로 나갔다"


def test_축_조회_실패가_갈리는_이유는_낱말이_없어서다() -> None:
    """⚠️ **이 갈림은 고른 것이 아니다.** `CollectionPartOut` 에 `UNREADABLE` 이 없다.

    🔴 **그 표에 낱말이 생기는 날 이 검사가 red 가 된다.** 그때 둘을 다시 맞춰야 하고,
      맞추지 않으면 같은 사실이 두 낱말로 나간다.
    """
    하나 = _잰다()[축_조회_실패]

    part_낱말 = get_args(CollectionPartOut.model_fields["status"].annotation)
    assert "UNREADABLE" not in part_낱말, (
        f"`CollectionPartOut.status` 에 UNREADABLE 이 생겼다: {part_낱말} —"
        " 이제 축 조회 실패도 두 파일이 같은 낱말로 답해야 한다"
    )
    assert "UNREADABLE" in get_args(SeedStatus)

    assert 하나.seed_status == "UNREADABLE", (
        f"조회 실패가 {하나.seed_status} 로 접혔다 — 재무 축 조회도 조회다"
    )
    assert 하나.part_status == "BLOCKED", (
        f"쓸 수 있는 낱말 중 가장 넓은 것이 아니다: {하나.part_status}"
    )


def test_NOT_ATTEMPTED_는_두_대역_어디에도_없다() -> None:
    """🔴 **`NOT_ATTEMPTED` 는 「하루가 안 열렸다」 하나에만 남는다.**

    셋을 한 칸에 접으면 209일을 걷고 나서 *"시드 안 된 날"* 을 셀 때 개장 안 한 날과
    축이 깨진 날이 같이 잡힌다 — 무엇을 고쳐야 할지 못 본다.
    """
    관측 = _잰다()

    for 대역, 하나 in 관측.items():
        assert 하나.seed_status != "NOT_ATTEMPTED", (
            f"{대역}: 축 문제가 *'시도할 이유가 없었다'* 로 접혔다"
        )
        assert 하나.seed_status != "NOTHING_DUE", f"{대역}: 축 문제가 0 건으로 접혔다"


def test_어휘는_다섯이다() -> None:
    """★ 낱말의 주인은 `SeedStatus` 하나다. 늘거나 줄면 위 대조표를 다시 써야 한다."""
    assert get_args(SeedStatus) == (
        "SEEDED",
        "NOTHING_DUE",
        "UNREADABLE",
        "BLOCKED",
        "NOT_ATTEMPTED",
    )
    assert get_origin(SeedStatus) is Literal
