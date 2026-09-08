"""
forecast_gate.py — **오늘 ML 예측이 왔는가.** 묻기만 한다.

ML 이 매일 아침 09:23 쯤 예측을 적재하고, 스케줄러가 09:30 에 깨어난다. 그 사이가
좁아서 *"아직 안 왔다"* 가 정상적으로 일어난다. 이 모듈은 그 사실을 **답으로만**
낸다.

★ **`day_gate.check_day_gate` 와 같은 규율이다.**

```text
day_gate        "그날 장부가 열렸는가"   묻기만 하고 **열지 않는다**
forecast_gate   "오늘 예측이 왔는가"     묻기만 하고 **판단을 안 돌린다**
```

  여는 것이 `open_day` 의 일인 것처럼, 기다릴지 · 언제까지 · 몇 번 물을지는 다음 판의
  스케줄러가 정한다.

🔴 **재시도 판단은 이 모듈이 하지 않는다.** 마감 시각 · 재시도 간격 · 횟수 상한은
  여기에 없고, 앞으로도 여기 두지 않는다. 그것을 여기 넣으면 *"물어보기"* 와
  *"기다리기"* 가 한 함수가 되어, 백테스트가 예측 유무를 물을 때마다 재시도 정책까지
  끌고 오게 된다.

🔴 **`load_forecast` 를 다시 구현하지 않는다.**

  게이트가 *"왔다"* 고 했는데 `load_forecast` 가 `MISSING` 을 내면 **최악**이다 —
  스케줄러는 돌리고 매입은 `RUNTIME_NOT_READY` 를 내며, 화면에는 *"예측이 왔는데
  못 썼다"* 가 뜬다. 그래서 **새 쿼리를 쓰지 않고 같은 함수를 불러 그 등급을 본다.**

```text
load_forecast(item, as_of) 의 grade
  "MEASURED"  → READY       그날 배치가 있다
  "MISSING"   → NOT_YET     아직 안 왔다
  예외        → UNREADABLE  못 물었다
```

  ★★ 그러면 **둘이 어긋날 수가 없다.** 게이트가 통과시킨 날은 정의상 매입이 쓸 수
    있는 날이다.

★ 이 게이트가 기대는 사실은 `load_forecast` 의 이 줄이다 (`inputs.py`).

    *"집은 행의 `as_of` 가 요청 `as_of` 와 **같을 때만** `MEASURED` 다.
    하루만 밀려도 안 쓴다."*

  그래서 *"예측이 왔다"* 는 곧 *"그날 배치가 있다"* 다. 210일 전 배치를 집고 있던
  동안(`#227` 후속 · 2026-09-04 실측)에는 이 게이트를 세울 수가 없었다.

🔴 **세 값을 섞지 않는다.**

```text
READY       왔다
NOT_YET     **확인했고** 아직 안 왔다        ← 재시도할 자리
UNREADABLE  **못 물었다** (DB 가 죽었다 등)  ← 재시도로 안 풀린다
```

  `UNREADABLE` 을 `NOT_YET` 으로 접으면 **DB 가 죽은 날 스케줄러가 영원히
  재시도하고 그 사실이 아무 데도 안 남는다.** 없는 것과 못 읽은 것을 가르는
  §1.2-10 이 여기서도 그대로다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.contracts.core import ITEMS, ItemCode
from app.master.inputs import SourcedInput, load_forecast

__all__ = [
    "DayForecastReadiness",
    "DayReadiness",
    "ItemForecastGate",
    "Readiness",
    "check_forecast_gate",
    "day_forecast_readiness",
]

#: 품목 하나의 답.
Readiness = Literal["READY", "NOT_YET", "UNREADABLE"]

#: 날 하나의 답.
DayReadiness = Literal["ALL_READY", "SOME_READY", "NONE_READY", "UNREADABLE"]

#: `load_forecast` 를 갈아 끼우는 자리. 검사는 이 자리에 대역을 준다.
#:
#: ⚠️ **기본값이 `load_forecast` 자체다.** `None` 으로 두면 *"안 물었다"* 와
#:   *"기본 적재로 물었다"* 가 같은 값이 된다 (`clock.py` · `verifier.py` 와 같은 규율).
Loader = Callable[[str, date], SourcedInput]


@dataclass(frozen=True)
class ItemForecastGate:
    """품목 하나의 게이트 답. **등급을 지우지 않고 같이 들고 다닌다.**

    ★ `grade` 를 남기는 이유는 `readiness` 가 접은 값이기 때문이다. 접힌 값만 남으면
      *"왜 그렇게 접혔는가"* 를 다시 물을 데가 없고, 그때 사람은 새 쿼리를 짠다.
    """

    item: str
    as_of: date
    readiness: Readiness
    #: `load_forecast` 가 낸 등급. **못 물었으면 `None`** — 등급이 없었다는 뜻이다.
    grade: str | None
    #: 사람이 읽는 사유. `load_forecast` 의 `note` 를 그대로 옮긴다.
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.readiness == "READY"


@dataclass(frozen=True)
class DayForecastReadiness:
    """날 하나로 접은 답. **품목별 내역을 반드시 같이 담는다.**

    🔴 `SOME_READY` 를 `ALL_READY` 나 `NONE_READY` 로 접으면 *"배추만 왔는데 전부
      왔다"* 로 읽히거나 그 반대가 된다. 그래서 `items` 가 비어 있을 수 없다.
    """

    as_of: date
    readiness: DayReadiness
    items: tuple[ItemForecastGate, ...]

    def _of(self, readiness: Readiness) -> tuple[str, ...]:
        return tuple(gate.item for gate in self.items if gate.readiness == readiness)

    @property
    def ready_items(self) -> tuple[str, ...]:
        """예측이 온 품목."""
        return self._of("READY")

    @property
    def not_yet_items(self) -> tuple[str, ...]:
        """확인했고 아직 안 온 품목."""
        return self._of("NOT_YET")

    @property
    def unreadable_items(self) -> tuple[str, ...]:
        """못 물어본 품목. 🔴 **`not_yet_items` 와 섞지 않는다.**"""
        return self._of("UNREADABLE")


def check_forecast_gate(
    item: str,
    as_of: date,
    *,
    load: Loader = load_forecast,
) -> ItemForecastGate:
    """그 품목의 **그날 예측이 왔는지만** 묻는다. 판단을 돌리지 않는다.

    ★ **`load_forecast` 의 등급 하나만 본다.** payload 를 뜯어 보지도, 새 조회를
      하지도 않는다 — 그 둘 중 하나라도 하면 게이트와 적재가 다른 사실을 보게 된다.

    ⚠️ `load_forecast` 는 지금 `MEASURED` 와 `MISSING` 만 낸다. 그래도 **`MEASURED`
      만 `READY`** 로 두고 나머지는 `NOT_YET` 이다 — 언젠가 `MOCK` 다리가 다시
      생기면 그것을 *"예측이 왔다"* 로 읽어서는 안 되기 때문이다. 접힌 뒤에도
      실제 등급은 `grade` 에 그대로 남는다.

    ⚠️ **예외를 밖으로 내지 않는다.** 관문이 터지면 스케줄러가 아예 못 도는데,
      못 물어본 것과 안 온 것은 다르다 (`check_day_gate` 와 같은 태도).
    """
    try:
        sourced = load(item, as_of)
    except Exception as exc:  # noqa: BLE001 — 못 물어본 것과 안 온 것은 다르다.
        return ItemForecastGate(
            item=item,
            as_of=as_of,
            readiness="UNREADABLE",
            grade=None,
            reason=f"예측 적재를 못 읽었다: {type(exc).__name__}: {exc}",
        )
    readiness: Readiness = "READY" if sourced.grade == "MEASURED" else "NOT_YET"
    return ItemForecastGate(
        item=item,
        as_of=as_of,
        readiness=readiness,
        grade=sourced.grade,
        reason=sourced.note if readiness == "NOT_YET" else "",
    )


def day_forecast_readiness(
    items: Iterable[ItemCode] = ITEMS,
    as_of: date | None = None,
    *,
    load: Loader = load_forecast,
) -> DayForecastReadiness:
    """그날 전체가 준비됐는지. **품목별 답을 접되 내역을 버리지 않는다.**

    ```text
    ALL_READY    전부 왔다
    SOME_READY   일부만 왔다      ← 어느 품목인지 `ready_items` · `not_yet_items` 에
    NONE_READY   하나도 안 왔다
    UNREADABLE   하나라도 못 물었다
    ```

    🔴 **`UNREADABLE` 이 가장 세다.** 하나라도 못 물었으면 나머지가 다 왔어도
      `ALL_READY` 가 아니다 — *"전부 왔다"* 고 답하려면 전부를 물어봤어야 한다.

    🔴 **재시도는 여기서 안 정한다.** `NONE_READY` 가 *"기다려라"* 라는 뜻이 아니라
      *"아직 없다"* 라는 사실일 뿐이다. 마감 시각과 간격은 스케줄러의 몫이다.

    :param as_of: 물어볼 날. 🔴 **벽시계를 여기서 읽지 않는다** — 안 주면 터진다.
        오늘이 며칠인지는 `clock.today_in_seoul()` 하나만 안다.
    """
    if as_of is None:
        raise ValueError(
            "as_of 가 없다 — 예측 게이트는 시계를 안 읽는다."
            " 스케줄러가 clock.today_in_seoul() 로 정해서 넘겨야 한다"
        )
    gates = tuple(check_forecast_gate(item, as_of, load=load) for item in items)
    if not gates:
        raise ValueError(
            "품목이 하나도 없다 — 빈 목록을 NONE_READY 로 접으면"
            " 스케줄러가 오지 않을 예측을 영원히 기다린다"
        )
    return DayForecastReadiness(as_of=as_of, readiness=_fold(gates), items=gates)


def _fold(gates: tuple[ItemForecastGate, ...]) -> DayReadiness:
    """품목별 답을 날 하나로. **접는 규칙이 여기 한 곳에만 있다.**"""
    if any(gate.readiness == "UNREADABLE" for gate in gates):
        return "UNREADABLE"
    ready = sum(1 for gate in gates if gate.readiness == "READY")
    if ready == len(gates):
        return "ALL_READY"
    if ready == 0:
        return "NONE_READY"
    return "SOME_READY"
