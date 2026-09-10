"""
collection_seed.py — 개장할 때 그날 결제기일인 채권을 **수금 사건으로 옮긴다**.

🔴 **`SIM_FIXED` 시뮬레이션 가정이다. 실제 입금 사실이 아니다.**

  재무·판매가 조건 여섯으로 승인한 규칙이고, 여섯 중 첫째가 그것이다 — *"실제 입금
  사실로 취급하지 않는다"*. 그래서 모든 행의 `note` 가 `SIM_FIXED` 로 시작한다.

```text
① 실제 입금 사실로 취급 안 함   note 에 근거를 적고 SIM_FIXED 로 구분한다
② Finance 자동수금은 계속 금지   재무는 **이 표에 적힌 것만** 실행한다
③ target 은 **누적** target      원금을 그대로 적는다 (증분이 아니다)
④ COLLECTED 재수금 금지          target = original 이라 delta 가 0 이다
⑤ PARTIAL 은 잔여만 · OPEN 전액   같은 한 줄이 둘 다 만든다
⑥ 신규 Receivable 에도 멱등      개장 때마다 다시 훑고, 없는 건만 만든다
```

---

★ **`due_date` 는 지어낸 값이 아니다.**

```text
sales/persistence.py:106   due_date = sale_date + scenario.payment_days
finance/receivables.py:94  sale 행과 receivable 요청의 due_date 를 **대조**한다
```

  🔴 **가정인 것은 「그날 전액 들어온다」 하나뿐이다.** 날짜 자체는 계약 결제기일이고
    두 표가 서로 대조하는 사실이다. 그래서 `note` 에 파생식을 적는다 — 값만 남기면
    사람도 매입 판단도 그것을 **확정 입금**으로 읽는다.

---

## ★★ `target = original_amount_krw` **한 줄로 `④⑤` 가 성립한다**

```text
OPEN       target 300 · current   0  →  delta 300   (전액)
PARTIAL    target 300 · current 150  →  delta 150   (잔여만)
COLLECTED  target 300 · current 300  →  delta   0   (재수금 아님)
```

🔴 **`④⑤` 를 여기서 따로 계산하지 않는다.** 재무 `build_collection_transition` 이
  이미 역행(`cumulative collection cannot regress`) · 초과(`cannot exceed original
  amount`) · 항등식(`receivable amount identity is inconsistent`)을 막는다. 같은
  규칙을 두 곳에 앉히면 **둘이 갈리는 날이 온다** — 그때 어느 쪽이 정본인지 아무도
  모른다.

⚠️ **그래도 `outstanding_amount_krw > 0` 인 채권만 사건을 만든다.**

  `COLLECTED` 는 delta 가 0 이라 넣어도 아무 일이 안 일어나지만, **낼 것이 없는 사건을
  적지 않는다** — *"없는 것과 안 한 것은 다르다"* 는 규율의 다른 쪽이다. 행이 있으면
  사람은 *"그날 뭔가 들어왔다"* 로 읽는다.

---

🟢 **멱등은 PK 가 잡는다** — `(sim_run_id, financing_mode, collection_date,
  receivable_id)`. `ON CONFLICT DO NOTHING` 이라 두 번 불러도 행이 안 늘어난다.

  ⚠️ **그래도 몇 건이 실제로 들어갔는지는 센다.** 넣은 건수와 이미 있던 건수를 안
    가르면 조건 `⑥`(*"신규 Receivable 에도 멱등 적용"*)이 지켜지는지 밖에서 못 본다.

---

🔴 **다섯 값을 섞지 않는다** (`transition.carried_forward_status` 와 같은 규율).

```text
SEEDED         n 건 만들었다
NOTHING_DUE    **확인했고** 낼 것이 없었다 (0 건)
UNREADABLE     **못 했다** — 조회나 쓰기가 실패했다
BLOCKED        **막았다** — 실행 축이 안 맞아 fail-closed 했다
NOT_ATTEMPTED  시도할 **이유가 없었다** — 하루가 안 열렸다
```

  ⚠️ **왜 다섯인가.** 없는 것 ≠ 안 한 것 ≠ 못 읽은 것 ≠ 막은 것. 접으면 209일을 걷고
    나서 *"시드 안 된 날"* 을 셀 때 개장 안 한 날과 축이 깨진 날이 같이 잡힌다 —
    무엇을 고쳐야 할지 못 본다.

  ★ **`BLOCKED` 는 `finance_collection.py` 와 같은 낱말이다.** 축 불일치는 거기서도
    `BLOCKED` 다 (`finance_collection.py` 의 `collect`). **같은 사실에 두 낱말을 쓰지
    않는다** — 한 저장소에서 같은 사유 문장이 두 낱말로 나가면 결정이 뒤집힌다.

  ⚠️ `UNREADABLE` 을 `NOTHING_DUE` 로 접으면 표가 안 서 있거나 DB 가 끊긴 날이
    *"확인했고 없었다"* 로 조용히 지나가고, 들어왔어야 할 현금이 장부에 없는 채로
    매입 판단이 돈다.

  ⚠️ **재무 축 조회 실패는 `UNREADABLE` 이다.** 조회도 조회다 — 위 정의가 그렇다.
    `finance_collection.py` 는 같은 자리에서 `BLOCKED` 로 답하는데, 그것은 고른 것이
    아니라 `CollectionPartOut.status` 에 `UNREADABLE` 이라는 낱말이 아예 없기
    때문이다. 그 표에 낱말이 생기는 날 둘을 다시 맞춰야 하고, 그 날을 잡는 검사가
    `tests/master/test_collection_seed_vocabulary.py` 다.

🔴 **개장을 실패시키지 않는다.** 사건 생성이 터져도 하루는 열려야 한다 —
  `seed_day` 는 어떤 예외도 밖으로 내보내지 않고 `UNREADABLE` 로 답한다.

---

⚠️ **`financing_mode` 는 마스터가 고르지 않는다.** `finance_collection.py` 와 같다 —
  `get_finance_runtime_axis()` 가 재무 축의 주인이고, 축의 `sim_run_id` 가 마스터 것과
  다르면 **fail-closed** 한다. 남의 실행 장부에 수금 사건을 적는 것은 에러 없이 남의
  현금을 늘린다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from psycopg import sql

from app.finance.db import (
    FinanceDataNotReady,
    FinanceRuntimeAxis,
    get_connection,
    get_db_schema,
    get_finance_runtime_axis,
)
from app.master.collection_events import TABLE

__all__ = [
    "CollectionSeedOutcome",
    "CollectionSeedResult",
    "SeedStatus",
    "seed_collection_events",
    "seed_day",
]

#: 🔴 **가정임을 행마다 표시하는 말머리.** 이 문자열이 없으면 나중에 아무도 이 행이
#:   실측 입금인지 시뮬레이션 가정인지 못 가른다.
SIM_FIXED = "SIM_FIXED"

SeedStatus = Literal["SEEDED", "NOTHING_DUE", "UNREADABLE", "BLOCKED", "NOT_ATTEMPTED"]


@dataclass(frozen=True)
class CollectionSeedResult:
    """사건 생성 1회의 셈. **만든 것과 이미 있던 것을 가른다.**

    :param created: 이번에 실제로 들어간 행 수.
    :param skipped: PK 충돌로 건너뛴 행 수 — **이미 있던 사건**이다.
    """

    created: int
    skipped: int


@dataclass(frozen=True)
class CollectionSeedOutcome:
    """개장 응답에 실리는 결과. **못 한 것을 0 건으로 접지 않는다.**"""

    status: SeedStatus
    created: int = 0
    skipped: int = 0
    reason: str = ""


def _table() -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(TABLE))


def _receivables() -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier("receivables"))


#: 조회 컬럼 순서. 아래 SELECT 와 **같아야 한다.**
_COLUMNS = (
    "receivable_id",
    "due_date",
    "original_amount_krw",
    "received_amount_krw",
)


def _note(*, due_date: Any, original: Any, received: Any) -> str:
    """*"이 사건을 왜 사실로 두었나"* 를 적는다. **파생식과 성격이 둘 다 들어간다.**

    🔴 **숫자를 넣는다.** 값만 넘기면 사람도 매입 판단도 확정으로 읽는다
      (`app/master/inputs.py` 가 파생분에 파생식을 실어 내보내는 것과 같은 규율).
    """
    delta = original - received
    return (
        f"{SIM_FIXED}: 계약 결제기일 당일 전액 회수 가정. 실제 입금 사실이 아니다."
        f" 근거 due_date={due_date} (sales.collection_due_date = sale_date + payment_days)."
        f" 원금 {original} · 기왕수금 {received} → 이 사건의 delta {delta}."
    )


def seed_collection_events(
    conn: Any,
    *,
    sim_run_id: str,
    financing_mode: str,
    as_of: date,
) -> CollectionSeedResult:
    """`as_of` 가 결제기일인 미수 채권을 `master_collection_events` 에 옮긴다.

    🔴 **커밋하지 않는다. 커넥션도 열지 않는다.** 트랜잭션 경계는 부르는 쪽 것이다
      (`DayOpening` Protocol 과 같은 분담).

    ⚠️ **`receivables` 에는 `financing_mode` 칸이 없다.** 그 표의 축은 `sim_run_id`
      하나이고, `financing_mode` 는 **만들 사건의 축**으로만 쓴다 — 재무가 준 값을
      그대로 싣는다.

    🔴 **`outstanding_amount_krw > 0` 만 고른다.** `COLLECTED` 는 낼 것이 없어 행을
      만들지 않는다.

    🔴 **`target_received_total_krw` 는 `original_amount_krw` 다.** `outstanding` 이
      아니다 — 이 칸은 **누적** target 이라 잔액을 적으면 두 번째 분할 수금에서
      역행으로 거부된다. 그리고 이 한 줄이 조건 `④⑤` 를 동시에 만든다.
    """
    query = sql.SQL(
        "SELECT receivable_id, due_date, original_amount_krw, received_amount_krw"
        " FROM {} WHERE sim_run_id = %s AND due_date = %s AND outstanding_amount_krw > 0"
        " ORDER BY receivable_id"
    ).format(_receivables())

    insert = sql.SQL(
        "INSERT INTO {} (sim_run_id, financing_mode, collection_date, receivable_id,"
        " target_received_total_krw, note)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING"
    ).format(_table())

    created = 0
    skipped = 0
    with conn.cursor() as cursor:
        cursor.execute(query, (sim_run_id, as_of))
        rows = cursor.fetchall()
        for row in rows:
            values = (
                [row[name] for name in _COLUMNS] if isinstance(row, Mapping) else list(row)
            )
            receivable_id, due_date, original, received = values
            cursor.execute(
                insert,
                (
                    sim_run_id,
                    financing_mode,
                    as_of,
                    receivable_id,
                    original,
                    _note(due_date=due_date, original=original, received=received),
                ),
            )
            if cursor.rowcount == 1:
                created += 1
            else:
                # ★ **이미 있던 사건이다.** PK 가 멱등을 잡는 자리가 여기다.
                skipped += 1

    return CollectionSeedResult(created=created, skipped=skipped)


def seed_day(
    as_of: date,
    *,
    sim_run_id: str,
    connect: Callable[[], Any] | None = None,
    read_axis: Callable[..., FinanceRuntimeAxis] = get_finance_runtime_axis,
    seed: Callable[..., CollectionSeedResult] = seed_collection_events,
) -> CollectionSeedOutcome:
    """개장 뒤에 부르는 자리. **어떤 예외도 밖으로 내보내지 않는다.**

    🔴 **개장을 실패시키지 않는다.** 사건 생성이 터져도 하루는 열려야 한다 — 다만
      *"못 했다"* 가 응답에 실려야 하고, `0 건` 으로 접히면 안 된다.

    ★ **`financing_mode` 를 고르지 않는다.** 재무 축을 물어보고 그 답을 그대로 쓴다.
      실측으로 `finance_states` 에 `LOAN_BASELINE` 252행과 `BASE_NO_LOAN` 2행이
      공존하므로, 상수를 박으면 무차입 장부의 수금이 대출 장부에 조용히 들어간다.

    🔴 **축의 `sim_run_id` 가 마스터 것과 다르면 안 만든다** (fail-closed) — `BLOCKED`
      다. 덮어 쓰면 남의 실행 장부에 수금 사건을 적는다.

    ⚠️ **`NOT_ATTEMPTED` 는 이 함수가 내지 않는다.** *"하루가 안 열렸다"* 뿐이고,
      그것은 `day_open` 이 여기 오기 전에 판단한다.
    """
    try:
        axis = read_axis(sim_run_id=sim_run_id)
    except (FinanceDataNotReady, LookupError, ValueError) as exc:
        # ★ **조회 실패는 `UNREADABLE` 이다.** 재무 축 조회도 조회다 — *"시도할 이유가
        #   없었다"* 가 아니라 *"못 했다"* 다.
        # ★ **사유를 그대로 옮긴다.** `finance_runtime_axis_ambiguous` 가 여기서
        #   사라지면 *"못 했다"* 만 남고 무엇이 모호했는지가 없어진다.
        return CollectionSeedOutcome(
            status="UNREADABLE", reason=f"재무 축을 읽지 못했다: {exc}"
        )

    if axis["sim_run_id"] != sim_run_id:
        # 🔴 **막은 것이다.** `finance_collection.py` 가 같은 상황에 쓰는 낱말과
        #   같아야 한다 — 같은 사실에 두 낱말을 쓰지 않는다.
        return CollectionSeedOutcome(
            status="BLOCKED",
            reason=(
                "실행 축이 다르다: 마스터 sim_run_id="
                f"{sim_run_id!r}, 재무 축 sim_run_id={axis['sim_run_id']!r}"
            ),
        )

    open_connection = get_connection if connect is None else connect
    try:
        conn = open_connection()
    except Exception as exc:  # noqa: BLE001 - 커넥션을 못 여는 것도 **못 한 것**이다.
        return CollectionSeedOutcome(
            status="UNREADABLE",
            reason=f"수금 사건을 만들지 못했다: {type(exc).__name__}: {exc}",
        )

    try:
        result = seed(
            conn,
            sim_run_id=sim_run_id,
            financing_mode=axis["financing_mode"],
            as_of=as_of,
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 실패를 `0 건` 으로 접지 않는다.
        # 🔴 **`NOTHING_DUE` 로 접으면 *"확인했고 없었다"* 로 조용히 지나간다.**
        # ⚠️ **되돌리기 실패가 사유를 덮으면 안 된다.** 무엇이 터졌는지가 먼저다.
        with suppress(Exception):
            conn.rollback()
        return CollectionSeedOutcome(
            status="UNREADABLE",
            reason=f"수금 사건을 만들지 못했다: {type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()

    if result.created == 0:
        # ★ **확인했고 만들 것이 없었다.** 낼 것이 없었거나 이미 다 있었다 —
        #   어느 쪽인지는 `skipped` 가 나른다.
        return CollectionSeedOutcome(
            status="NOTHING_DUE", created=0, skipped=result.skipped
        )
    return CollectionSeedOutcome(
        status="SEEDED", created=result.created, skipped=result.skipped
    )
