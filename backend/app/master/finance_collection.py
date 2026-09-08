"""
finance_collection.py — 마스터 `CollectionSource` 를 재무 수금 구현체에 잇는 배선.

🔴 **이 파일이 하는 일은 축을 맞춰 주는 것뿐이다.**

```text
마스터 Protocol        collect(conn, *, as_of)
재무 구현체            FinanceCollectionSource(sim_run_id, financing_mode, source)
                       .collect(conn, *, as_of)
```

  Protocol 은 `as_of` 만 나르는데 재무 구현체는 **생성 인자로 축 둘**을 받는다
  (`app/master/collection.py` 의 `CollectionSource` 가 그렇게 적어 뒀다 — *"어느
  실행의 장부인가는 실행 정체성이라 어댑터 생성 인자로 온다"*). 그 둘을 어디서
  가져오는가가 이 파일의 전부다.

---

🔴 **`sim_run_id` 는 마스터가 정하고 `financing_mode` 는 마스터가 고르지 않는다.**

```text
sim_run_id       🟢 **마스터가 정한다** — 값의 주인은 ledger_repository.BURN_IN_SIM_RUN_ID 하나
financing_mode   🔴 **마스터 축이 아니다** — 재무 축 (sim_run_id, as_of, financing_mode) 의 것
```

  ⚠️ **여기에 `financing_mode` 를 상수로 박으면 안 된다.** 실측으로 `finance_states`
    에 `LOAN_BASELINE` 252행과 `BASE_NO_LOAN` 2행이 **실제로 공존한다**. 마스터가
    하나를 골라 박으면 *"무차입 상태가 대출 baseline 자리에 조용히 들어오는"* 사고가
    나고, 그 사고는 에러 없이 숫자만 바꾼다 — `app/finance/db.py` 의
    `get_finance_runtime_axis` 가 그 문장을 이미 적어 뒀다.

★ **그래서 물어본다.** `get_finance_runtime_axis()` 가 재무 축의 주인이고, 이
  어댑터는 그 답을 **그대로 실어 보낸다.** 고르지 않는다.

🔴 **임포트 시점에 DB 를 읽지 않는다.** `app/main.py` 의 등록소 넷이 다 그렇다 —
  축 조회는 `collect()` **안에서** 일어난다. 배선이 DB 를 요구하기 시작하면 앱이
  뜨는 조건이 조용히 늘어난다.

---

🔴 **축의 `sim_run_id` 가 마스터 것과 다르면 막는다 (fail-closed).**

  재무가 읽은 축이 다른 실행을 가리키는데 마스터 값으로 덮어 쓰고 진행하면 **남의
  실행 장부에 수금을 적는다.** 그것은 에러 없이 남의 현금을 늘린다.

  ⚠️ **덮어 쓰는 것이 더 나빠 보이지 않는 것이 함정이다.** `FinanceCollectionSource`
    는 자기가 받은 축으로만 사건을 고르므로 아무 예외 없이 조용히 돈다 — 그래서
    여기서 세운다.

★ **`BLOCKED` 다.** `NOTHING_DUE` 로 접으면 *"오늘은 들어올 게 없었다"* 로 읽히고,
  들어왔어야 할 현금이 장부에 없는 채로 매입 판단이 돈다
  (`app/master/collection.py` 의 다섯 갈래 표에서 축 불일치가 `BLOCKED` 다).

---

⚠️ **축이 모호하면 삼키지 않는다.** `get_finance_runtime_axis()` 는 실행이 여럿이면
  `FinanceDataNotReady("finance_runtime_axis_ambiguous")` 를 던진다. 그 사유를
  `BLOCKED` 로 접되 **무엇이 모호했는지를 문장에 남긴다** — 접기만 하고 사유를 버리면
  화면에는 *"막혔다"* 만 남고 고칠 곳이 사라진다.

---

🔴 **사건은 호출 시점에 표에서 읽는다** (2026-09-08 · `master_collection_events`).

  전에는 이 어댑터가 `source: DeterministicCollectionFixtureSource` 를 **필드로**
  들고 있었다. 그러면 사건 목록이 **배선 시점에 고정**되고, 표에 한 줄 넣어도 앱을
  다시 띄우기 전까지는 아무 일도 안 일어난다.

```text
① 재무 축을 읽는다                     ← 지금 그대로
② sim_run_id 불일치면 BLOCKED          ← 지금 그대로
③ 그 축의 사건을 표에서 읽는다          ← 여기
④ 재무 fixture 원천으로 감싸 위임한다
```

  ⚠️ **③ 이 실패하면 `BLOCKED` 다.** `()` 로 접으면 표가 안 서 있거나 DB 가 끊긴 날이
    *"오늘은 들어올 게 없었다"* 로 읽힌다 — `collection_events.read_collection_events`
    가 예외를 그대로 올리는 이유가 그것이고, 접는 자리는 여기다.

🔴 **표가 비어 있다** (2026-09-08 실측 0행). **이 판은 자리를 만들 뿐 사건을 만들지
  않는다.** 무엇을 사실로 둘지는 팀 결정이고, 재무가 *"due_date 경과를 수금으로 읽지
  않는다"* 로 그은 선이 그 이유다. **낸 것과 도는 것은 다르다.**
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.finance.collection import CollectionEvent
from app.finance.collection_fixture import DeterministicCollectionFixtureSource
from app.finance.collection_source import FinanceCollectionSource
from app.finance.db import FinanceDataNotReady, FinanceRuntimeAxis, get_finance_runtime_axis
from app.master.collection import CollectionPartOut
from app.master.collection_events import read_collection_events

__all__ = ["FinanceCollectionAdapter"]

#: 사건을 읽는 방법의 모양. 축 둘을 받아 그 축의 사건 전부를 준다.
LoadEvents = Callable[..., tuple[CollectionEvent, ...]]


@dataclass
class FinanceCollectionAdapter:
    """`CollectionSource` 구현. **재무 축을 물어보고 그 축의 사건을 실어 넘긴다.**

    :param sim_run_id: 마스터가 정한 실행. `BURN_IN_SIM_RUN_ID` 하나가 주인이다.
    :param read_axis: 재무 축을 읽는 방법. 기본값이 재무 함수 그대로이고, 검사가
        대역을 끼울 자리다. **마스터가 축을 계산하는 자리가 아니다.**
    :param load_events: 수금 사건을 읽는 방법. 기본값이 `master_collection_events`
        조회이고, 검사가 대역을 끼울 자리다. **호출마다 다시 읽는다** — 배선 시점에
        고정하면 표에 한 줄 넣어도 앱을 다시 띄우기 전까지 아무 일도 안 일어난다.
    """

    sim_run_id: str
    read_axis: Callable[[], FinanceRuntimeAxis] = field(default=get_finance_runtime_axis)
    load_events: LoadEvents = field(default=read_collection_events)

    def collect(self, conn: Any, *, as_of: date) -> CollectionPartOut:
        """재무 축을 읽어 `FinanceCollectionSource` 를 세우고 위임한다."""
        try:
            axis = self.read_axis()
        except (FinanceDataNotReady, LookupError, ValueError) as exc:
            # ★ **사유를 그대로 옮긴다.** `finance_runtime_axis_ambiguous` 가 여기서
            #   사라지면 *"막혔다"* 만 남고 무엇이 모호했는지가 없어진다.
            return CollectionPartOut(
                part="finance",
                status="BLOCKED",
                reason=f"재무 축을 읽지 못했다: {exc}",
            )

        if axis["sim_run_id"] != self.sim_run_id:
            # 🔴 **덮어 쓰지 않는다.** 남의 실행 장부에 조용히 수금을 적는 자리다.
            return CollectionPartOut(
                part="finance",
                status="BLOCKED",
                reason=(
                    "실행 축이 다르다: 마스터 sim_run_id="
                    f"{self.sim_run_id!r}, 재무 축 sim_run_id={axis['sim_run_id']!r}"
                ),
            )

        try:
            events = self.load_events(
                sim_run_id=self.sim_run_id,
                financing_mode=axis["financing_mode"],
            )
        except Exception as exc:  # noqa: BLE001 - 조회 실패를 `()` 로 접지 않는다.
            # 🔴 **없는 것과 못 읽은 것은 다르다.** `()` 로 접으면 표가 안 서 있거나
            #   DB 가 끊긴 날이 *"오늘은 들어올 게 없었다"* 로 읽히고, 들어왔어야 할
            #   현금이 장부에 없는 채로 매입 판단이 돈다.
            return CollectionPartOut(
                part="finance",
                status="BLOCKED",
                reason=f"수금 사건을 읽지 못했다: {type(exc).__name__}: {exc}",
            )

        return FinanceCollectionSource(
            sim_run_id=self.sim_run_id,
            # 🔴 **고르지 않는다. 재무가 읽은 값 그대로다.**
            financing_mode=axis["financing_mode"],
            # ★ 사건 원천의 모양은 **재무 것**이다. 마스터는 읽어 온 사건을 그 그릇에
            #   담아 넘길 뿐이고, 그날 것을 고르는 일은 그쪽이 한다.
            source=DeterministicCollectionFixtureSource.from_events(
                events,
                source_ref="master_collection_events",
            ),
        ).collect(conn, as_of=as_of)
