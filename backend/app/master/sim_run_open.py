"""
sim_run_open.py — **새 실행을 여는 절차.**

```text
BaselineLineage                    어느 실행의 어느 상태에서 출발하나  (가리키기만 한다)
seed_opening_finance_state         새 실행의 **시작 재무 상태**를 만든다
seed_opening_logistics_fixture     새 실행의 **시작 물류 fixture** 를 만든다
reset_sim_run_ledger               다시 열 때 그 실행의 **장부를 지운다**
delete_sim_run_row                 다시 열 때 그 실행의 **행 자체를 지운다**
```

★ 실행 행 자체를 만드는 것은 `sim_run.create_sim_run` 이다 (`#531`). 이 파일은
  그 위에 얹히는 ② · ③ 이고, **셋을 하나로 묶는 진입점은 여기서 안 만든다** —
  돌리는 문은 별도 판이다.

---

## 🔴 `config_json` 은 **lineage 만** 담는다

```json
{"baseline": {"from_sim_run_id": "...", "finance_state_id": "..."}}
```

⚠️ **숫자를 여기 박지 않는다.** 현금 잔액을 설정에 복사해 두면 원본 행과 복사본이
  **두 진실**이 되고, 원본이 고쳐지는 날 갈린다 — 그때 어느 쪽이 맞는지 답할
  방법이 없다. 설정은 **가리키기만** 하고, 값은 늘 가리켜진 행에서 읽는다.

---

## 🔴 한 값을 보고 다른 값을 **추측하지 않는다**

```text
❌ financing_mode 를 보고 baseline 을 고른다
❌ finance_state_id 에 담긴 글자를 읽어 어느 쪽인지 판정한다
🟢 호출자가 financing_mode 와 baseline 을 **함께** 명시한다
```

★★ 실제 상태 이름들은 사람이 읽으라고 지은 것이고 같은 날짜·같은 구간을 가리킨다.
  이름으로 판정하는 순간 이름을 못 바꾸게 되고, 이름을 바꾸는 날 **말없이 다른
  출발점**에서 걷는다.

그 대신 **정합성 셋**을 확인한다 (재무와 합의 · 재무가 마감에서 같은 셋을 다시
확인한다 — 양쪽이 같은 것을 재는 것이 의도다).

```text
source = finance_state_id 가 가리키는 행
  ① source 가 있는가
  ② source.sim_run_id     == baseline.from_sim_run_id
  ③ source.financing_mode == 새 실행의 financing_mode
```

🔴 **하나라도 안 맞으면 터뜨린다.** 다른 baseline 을 추측하지도, `0` 으로 보정하지도
  않는다 — 보정하면 *"출발점을 못 찾았다"* 가 *"빈손에서 시작했다"* 로 둔갑한다.

---

## 🔴 identity 는 새로, 값만 이관

```text
finance_state_id · sim_run_id · state_date · state_type   **새로 정한다**
나머지 재무 값                                             source 행에서 **이관**
```

⚠️ **source 행을 그대로 복제하지 않는다.** 복제하면 새 실행의 첫 행에 **남의 실행
  id 가 들어오고**, 그 뒤로 이 행이 누구 것인지 되가를 방법이 없다.

★ **어느 칸을 나를지 손으로 고르지 않는다.** `finance_states` 는 `NOT NULL` 칸이
  열셋이고, 일부만 고르면 나머지를 어디서 채울지가 또 생긴다. 표의 칸 목록은
  `information_schema` 가 주인이고, 여기서는 **읽어서 통째로** 나른다.

---

## 🔴 물류 씨앗도 **같은 규율**이다

★★ 재무 시작 상태만 놓고 물류를 안 놓으면, 새 실행에 물류 행이 **한 행도 없다.**
  물류는 자기 개장 여부를 `logistics_runtime_fixture` 에서 읽으므로
  (`app/logistics/day_open.py` `is_open`), 마스터가 상한만큼 거슬러도 anchor 를
  못 찾고 **행을 만들지 않고 거절**한다.

```text
source 를 찾는 열쇠   (baseline_run_id, as_of, usage_scope)
```

★ 그 셋이 정확히 `uq_log_runtime_fixture` 이고, `is_open` 이 묻는 열쇠와 **같다.**
  최대 한 행이다.

🔴 **없으면 막는다.** 다른 날짜로 물러서지도, 다른 `usage_scope` 를 뒤지지도 않는다 —
  재무 씨앗이 baseline 을 못 찾을 때와 같은 태도다.

```text
새로 정한다   fixture_id · sim_run_id
그대로 이관    나머지 값 전부
note          이관 사실과 **source 의 fixture_id** 를 적는다
```

★★ **`as_of` 는 새로 정하지 않는다** — source 를 찾은 그 날짜 그대로다. 같은 날의
  같은 사실을 다른 실행 축에 앉히는 것이라 날짜가 바뀔 이유가 없다.

🔴 **`evidence_grade` · `source_ref` · `approved_by` 를 마스터가 새로 쓰지 않는다.**
  그 셋은 *"이 사실이 어디서 왔고 누가 승인했나"* 이고 **물류 소유**다. 같은 사실이니
  그대로 따라가고, **되짚을 수 있게** `note` 가 출처를 단다.

🔴 **`usage_scope` 를 여기 박지 않는다** (물류 상수를 import 하는 것도 아니다) —
  그 어휘의 주인은 물류이고, 마스터가 제 코드에 박으면 물류가 값을 바꾸는 날
  말없이 갈린다. **부르는 쪽이 눈에 보이게 준다.**

---

## 🔴 지우는 절차 — 표 목록도 순서도 **DB 가 주인이다**

```text
DELETE FROM <축을 가진 표> WHERE sim_run_id = <그 실행>
```

★★ **표 목록을 손으로 안 적는다.** 축을 가진 표는 마이그레이션이 늘리고 줄인다 —
  손으로 적은 목록은 표가 하나 늘어난 날 **조용히 그 표만 안 지우고**, 다시 연
  실행에 남의 행이 섞인 채로 179일이 걸린다. 목록은 `information_schema` 에서
  읽는다 (`sim_run_id` 칸을 가진 `BASE TABLE`).

★★ **순서도 손으로 안 적는다.** 자식부터 안 지우면 FK 가 막는다. 그 자식-부모
  관계의 주인은 `pg_constraint` 이므로 거기서 읽어 **자식 먼저**로 세운다.

🔴 **`sim_runs` 는 안 지운다.** 지우는 것은 그 실행의 **장부**이지 실행 자체가
   아니다 — 실행 행까지 지우면 *"다시 여는 것"* 이 아니라 *"없애는 것"* 이 된다.

🔴 **번인 실행에는 거부한다.** 번인에 **기초 상태가 있다** — 지우면 모든 실행의
   출발점이 사라지고, 그것을 되살릴 곳이 저장소에 없다.

🔴 **지운 것을 세어서 돌려준다.** 안 세면 *"지웠다"* 와 *"지울 것이 없었다"* 가
   같아지고, 다시 연 실행이 정말 빈 장부에서 출발했는지 아무도 못 답한다.

---

## 🔴 실행 행을 지우는 것은 **`reset_sim_run_ledger` 밖의 별개 일**이다

```text
--reset 일 때   ① 장부를 지운다   reset_sim_run_ledger
                ② 실행 행을 지운다 delete_sim_run_row     ← 이 파일의 새 자리
                ③ 실행 행을 넣는다 create_sim_run         (ON CONFLICT 없이 그대로)
```

★★ **왜 `reset_sim_run_ledger` 안이 아닌가.** 그 함수의 규율은 *"다시 여는 것이지
  없애는 것이 아니다"* 이고, **장부 비우기에 대해서는 지금도 맞다** — 장부만 비우고
  싶은 자리에서 실행 행까지 날아가면 그 말이 거짓이 된다. 실행 행을 지우는 것은
  `--reset` 이 정하는 일이지 장부 비우기가 정하는 일이 아니다.

★ **그런데도 같은 파일이다.** 지우는 것의 주인이 한 파일에 모여야 *"무엇을 지우면
  무엇이 사라지나"* 를 한 자리에서 읽는다.

🔴 **`create_sim_run` 에 `ON CONFLICT` 를 붙여 푸는 길로 가지 않는다.** 그러면
   **다른 설정으로 만들려던 실행**이 옛 행 위에 조용히 앉고, 그 뒤의 179일이 어느
   설정으로 걸린 것인지 아무도 못 답한다 — `create_sim_run` 이 막으려던 바로 그
   사고다. 지우고 다시 넣으면 새 설정이 **새 행으로** 선다.

### ★★ 이 삭제가 **자기 검사**다

`sim_runs.sim_run_id` 를 가리키는 FK 가 여럿이다. 장부를 다 안 지웠으면 이 삭제가
**FK 로 막힌다** — 그것이 좋은 성질이다. 지우기를 빠뜨린 표가 있으면 **시끄럽게**
드러나고, 조용히 반쪽 실행이 서는 것보다 낫다.

🔴 **`ON DELETE CASCADE` 로 풀지 않는다** (FK 를 끄는 것도 아니다) — 그러면 이
   자기 검사가 사라지고, 빠뜨린 표가 있어도 아무 소리 없이 다 지워진다.

⚠️ **막히면 「어느 표가 남았나」를 사유에 적는다.** 예외가 제약 이름과 남은 표를
  들고 오므로, 그것을 버리고 *"못 지웠다"* 만 남기면 사람이 다시 찾아 헤맨다.

🔴 **번인 가드를 여기서 다시 만들지 않는다.** `--reset` 은 ① 을 먼저 부르고,
   `reset_sim_run_ledger` 가 번인이면 거기서 이미 터진다 — 두 곳에서 막으면
   언젠가 한쪽만 고쳐지고, 그때 어느 쪽이 진짜 규칙인지 아무도 못 답한다.

---

🔴 **commit 하지 않는다.** 커밋은 부르는 쪽이 한다 — `create_sim_run` 과 같다.
   여기서 커밋하면 장부만 먼저 지워지고, 뒤이어 시작 상태 적재가 터졌을 때
   **출발점 없는 빈 실행**이 남는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from psycopg import sql
from psycopg.errors import ForeignKeyViolation

from app.finance.db import get_db_schema
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

__all__ = [
    "AXIS_COLUMN",
    "BASELINE_CONFIG_KEY",
    "FINANCE_STATE_TABLE",
    "LOGISTICS_FIXTURE_TABLE",
    "RUN_TABLE",
    "BaselineLineage",
    "ForeignKey",
    "LedgerReset",
    "delete_sim_run_row",
    "reset_sim_run_ledger",
    "seed_opening_finance_state",
    "seed_opening_logistics_fixture",
]

#: 실행 축이 실리는 칸 이름. 이 칸을 가진 표가 곧 **지울 대상**이다.
AXIS_COLUMN = "sim_run_id"

#: 실행 자체가 사는 표.
#:
#: 🔴 **장부를 지울 때는 대상이 아니다** — 다시 여는 것이지 없애는 것이 아니다.
#:   `reset_sim_run_ledger` 가 이 표를 목록에서 뺀다.
#:
#: ★ 다시 열 때 이 행을 지우고 새로 넣는 것은 `delete_sim_run_row` 가 따로 한다
#:   (모듈 docstring) — `--reset` 이 정하는 일이지 장부 비우기가 정하는 일이 아니다.
RUN_TABLE = "sim_runs"

#: 시작 재무 상태가 사는 표.
FINANCE_STATE_TABLE = "finance_states"

#: 시작 물류 fixture 가 사는 표. 🔴 **물류가 자기 개장 여부를 읽는 그 표다.**
LOGISTICS_FIXTURE_TABLE = "logistics_runtime_fixture"

#: 🔴 **이관 대상이 아닌 칸.** 이 행이 **언제 쓰였나** 이지 사실이 언제 생겼나가 아니다.
#:
#: ★★ 축이 다르다. `evidence_grade` · `source_ref` · `approved_by` 는 *"이 사실이 어디서
#:   왔나"* 라 같은 사실이면 그대로 따라가는 것이 맞다. 그런데 이 둘은 **행에 대한 기록**
#:   이고, 이관본은 **지금** 쓰인 새 행이다. source 값을 나르면 새 행이 두 달 전에
#:   만들어졌다고 말하게 된다 — 없는 사실이고, 에러 없이 틀린 값이다.
#:
#: 🔴 **`is_active` 는 여기 없다.** 그것도 기본값이 있지만 *"이 사실이 살아 있나"* 라
#:   물류의 판정이다. 마스터가 살려 놓으면 물류가 내린 판정을 뒤집는 것이 된다.
ROW_WRITTEN_COLUMNS = ("created_at", "updated_at")

#: `config_json` 안에서 lineage 가 앉는 자리.
BASELINE_CONFIG_KEY = "baseline"


@dataclass(frozen=True)
class BaselineLineage:
    """어느 실행의 어느 재무 상태에서 출발하는가. **가리키기만 한다.**

    🔴 **숫자를 안 담는다.** 값은 늘 `finance_state_id` 가 가리키는 행에서 읽는다
      (모듈 docstring).
    """

    from_sim_run_id: str
    finance_state_id: str

    def __post_init__(self) -> None:
        for 이름, 값 in (
            ("from_sim_run_id", self.from_sim_run_id),
            ("finance_state_id", self.finance_state_id),
        ):
            if not 값 or not 값.strip():
                raise ValueError(f"{이름} 없이 출발점을 가리킬 수 없다 — 지어내지 않는다")

    def as_config(self) -> dict[str, dict[str, str]]:
        """`create_sim_run(config_json=...)` 에 실을 조각.

        ★ **여기 담기는 것은 두 개의 이름뿐이다.** 잔액도 한도도 안 담는다.
        """
        return {
            BASELINE_CONFIG_KEY: {
                "from_sim_run_id": self.from_sim_run_id,
                "finance_state_id": self.finance_state_id,
            }
        }


@dataclass(frozen=True)
class ForeignKey:
    """FK 하나의 **모양**. `pg_constraint` 에서 읽은 그대로다.

    🔴 **칸이 여럿일 수 있다.** `master_decisions` 는 `(run_id, request_id)` 두 칸으로
      `master_agent_runs` 에 매달린다. 한 칸만 맞춰 좁히면 **다른 실행의 같은 run_id**
      까지 걸려 남의 장부가 날아간다.
    """

    child: str
    child_columns: tuple[str, ...]
    parent: str
    parent_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.child_columns) != len(self.parent_columns):
            raise ValueError(
                f"{self.child} → {self.parent} 의 칸 수가 안 맞는다:"
                f" {self.child_columns} / {self.parent_columns}"
                " — 짝이 안 맞는 열쇠로 지울 행을 좁히지 않는다"
            )
        if not self.child_columns:
            raise ValueError(f"{self.child} → {self.parent} 에 칸이 하나도 없다")


@dataclass(frozen=True)
class LedgerReset:
    """지운 결과. **표마다 몇 행인지**를 값으로 낸다."""

    sim_run_id: str
    #: 실제로 DELETE 를 던진 순서. **자식 먼저**다.
    #:
    #: 🔴 **축 없는 자식도 여기 든다.** 지운 것이 성적표에 안 남으면 무엇이 얼마나
    #:   지워졌는지를 되짚을 데가 없다.
    order: tuple[str, ...]
    #: 표 이름 → 지운 행 수. 🔴 **0 도 담는다** — 안 담으면 *"지웠다"* 와
    #: *"지울 것이 없었다"* 가 같아진다.
    deleted: Mapping[str, int]

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


# ── ② 시작 재무 상태 ───────────────────────────────────────────────────


def seed_opening_finance_state(
    conn: Any,
    *,
    sim_run_id: str,
    financing_mode: str,
    baseline: BaselineLineage,
    finance_state_id: str,
    state_date: date,
    state_type: str,
    note: str | None = None,
) -> str:
    """새 실행의 **시작 재무 상태 한 행**을 만든다. 만든 행의 이름을 돌려준다.

    :param financing_mode: 🔴 **새 실행의 것**이다. 호출자가 `baseline` 과 **함께**
        명시한다 — 한쪽을 보고 다른 쪽을 고르지 않는다 (모듈 docstring).
    :param finance_state_id: 새 행의 이름. 🔴 **source 것을 물려받지 않는다.**
    :raises LookupError: `baseline.finance_state_id` 가 가리키는 행이 없을 때.
    :raises ValueError: source 의 실행 축이나 조달 방식이 새 실행과 안 맞을 때.
        **다른 baseline 을 추측하지도 `0` 으로 보정하지도 않는다.**
    """
    for 이름, 값 in (
        (AXIS_COLUMN, sim_run_id),
        ("financing_mode", financing_mode),
        ("finance_state_id", finance_state_id),
        ("state_type", state_type),
    ):
        if not 값 or not 값.strip():
            raise ValueError(f"{이름} 없이 시작 상태를 만들 수 없다 — 없는 값을 지어내지 않는다")

    schema = get_db_schema()
    with conn.cursor() as cursor:
        칸들 = _insertable_columns(cursor, schema=schema, table=FINANCE_STATE_TABLE)
        source = _read_finance_state(
            cursor, schema=schema, columns=칸들, finance_state_id=baseline.finance_state_id
        )
        _assert_baseline_matches(source, baseline=baseline, financing_mode=financing_mode)

        # 🔴 **identity 는 새로, 값만 이관.** source 를 통째로 깐 위에 새로 정하는
        #    칸만 덮는다 — 덮는 칸 하나를 빠뜨리면 남의 실행 id 가 그대로 들어온다.
        #
        # ★ **덮는 칸의 주인은 이 dict 하나다.** 같은 목록을 상수로 한 벌 더 두면
        #   한쪽만 고치는 날 둘이 갈리고, 그때 어느 쪽이 실제로 실리는지가 흐려진다.
        #
        # ★ `financing_mode` 는 identity 가 아니다 — 정합성 셋 ③ 이 이미 *같음* 을
        #   쟀다. 그래도 **새 실행의 것을 적는다**: 출발점에서 나르면 이 칸의 주인이
        #   출발점이 되고, 셋 ③ 을 걷는 날 말없이 갈린다.
        새로정한다: dict[str, Any] = {
            "finance_state_id": finance_state_id,
            AXIS_COLUMN: sim_run_id,
            "state_date": state_date,
            "state_type": state_type,
            "financing_mode": financing_mode,
        }
        실을것 = {**dict(source), **새로정한다}
        if note is not None:
            실을것["note"] = note

        cursor.execute(
            sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
                sql.Identifier(schema),
                sql.Identifier(FINANCE_STATE_TABLE),
                sql.SQL(", ").join(sql.Identifier(one) for one in 칸들),
                sql.SQL(", ").join(sql.Placeholder() for _ in 칸들),
            ),
            [실을것[one] for one in 칸들],
        )
    return finance_state_id


def _assert_baseline_matches(
    source: Mapping[str, Any] | None,
    *,
    baseline: BaselineLineage,
    financing_mode: str,
) -> None:
    """🔴 **정합성 셋.** 하나라도 안 맞으면 터진다 (재무 청함 · 재무가 마감에서 다시 잰다)."""
    # ① source 가 있는가
    if source is None:
        raise LookupError(
            f"출발점으로 가리킨 재무 상태가 없다: {baseline.finance_state_id}"
            " — 다른 baseline 을 여기서 고르지 않는다"
        )
    # ② source 가 lineage 가 말한 그 실행의 것인가
    if source[AXIS_COLUMN] != baseline.from_sim_run_id:
        raise ValueError(
            f"출발점의 실행 축이 lineage 와 다르다:"
            f" {baseline.finance_state_id} 는 {source[AXIS_COLUMN]!r} 것인데"
            f" lineage 는 {baseline.from_sim_run_id!r} 이라고 한다"
        )
    # ③ source 의 조달 방식이 새 실행의 것과 같은가
    if source["financing_mode"] != financing_mode:
        raise ValueError(
            f"출발점의 조달 방식이 새 실행과 다르다:"
            f" 출발점은 {source['financing_mode']!r} 인데 새 실행은 {financing_mode!r} 이다"
            " — 한쪽을 보고 다른 쪽을 고치지 않는다"
        )


def _insertable_columns(cursor: Any, *, schema: str, table: str) -> tuple[str, ...]:
    """표에 **실을 수 있는 칸**을 순서대로 읽는다.

    ★ **손으로 안 고른다** (모듈 docstring). 생성 칸(`financial_limit_krw` 같은
      `GENERATED ALWAYS`)은 실으면 DB 가 막으므로 뺀다.

    🔴 **`ROW_WRITTEN_COLUMNS` 도 뺀다** — 안 실으면 DB 기본값(`now()`)이 선다.
       그 둘은 이관할 사실이 아니라 이 행이 언제 쓰였나이고, 이관본은 지금 쓰인다.
    """
    cursor.execute(
        """
        SELECT column_name, is_generated, identity_generation
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        [schema, table],
    )
    칸들 = tuple(
        row["column_name"]
        for row in cursor.fetchall()
        if row["is_generated"] != "ALWAYS"
        and row["identity_generation"] != "ALWAYS"
        and row["column_name"] not in ROW_WRITTEN_COLUMNS
    )
    if not 칸들:
        raise LookupError(f"{schema}.{table} 의 칸을 못 읽었다 — 빈 목록으로 행을 만들지 않는다")
    return 칸들


# ── ②' 시작 물류 fixture ───────────────────────────────────────────────


def seed_opening_logistics_fixture(
    conn: Any,
    *,
    sim_run_id: str,
    baseline_run_id: str,
    fixture_id: str,
    as_of: date,
    usage_scope: str,
) -> str:
    """새 실행의 **시작 물류 fixture 한 행**을 만든다. 만든 행의 이름을 돌려준다.

    ★★ 재무 씨앗만 놓으면 새 실행에 물류 행이 한 행도 없고, 그때 마스터 개장은
      anchor 를 못 찾아 `REJECTED_GAP` 으로 거절한다. **그 거절이 옳다** — 여기서
      막을 일은 물류 없는 하루가 열리는 것이지 거절 자체가 아니다.

    :param baseline_run_id: 어느 실행의 물류 사실을 이관하나. 🔴 **호출자가 준다.**
    :param fixture_id: 새 행의 이름. 🔴 **source 것을 물려받지 않는다.**
    :param as_of: source 를 찾는 날짜이자 **새 행이 앉는 날짜**다 — 같은 날의 같은
        사실을 다른 실행 축에 앉히는 것이라 날짜를 새로 정하지 않는다.
    :param usage_scope: 🔴 **어휘의 주인은 물류다.** 여기 박지 않고 눈에 보이게 받는다.
    :raises LookupError: `(baseline_run_id, as_of, usage_scope)` 로 찾은 행이 없을 때.
        **다른 날짜로 물러서지도 다른 scope 를 뒤지지도 않는다.**
    """
    for 이름, 값 in (
        (AXIS_COLUMN, sim_run_id),
        ("baseline_run_id", baseline_run_id),
        ("fixture_id", fixture_id),
        ("usage_scope", usage_scope),
    ):
        if not 값 or not 값.strip():
            raise ValueError(f"{이름} 없이 시작 물류 fixture 를 만들 수 없다 — 지어내지 않는다")

    schema = get_db_schema()
    with conn.cursor() as cursor:
        # ⚠️ **칸 목록을 손으로 안 적는다** — 물류가 칸을 더하는 날 손으로 적은
        #   목록은 조용히 뒤처진다. 목록의 주인은 `information_schema` 다.
        칸들 = _insertable_columns(cursor, schema=schema, table=LOGISTICS_FIXTURE_TABLE)
        source = _read_logistics_fixture(
            cursor,
            schema=schema,
            columns=칸들,
            baseline_run_id=baseline_run_id,
            as_of=as_of,
            usage_scope=usage_scope,
        )
        if source is None:
            raise LookupError(
                f"이관할 물류 fixture 가 없다:"
                f" 실행 {baseline_run_id!r} · as_of {as_of.isoformat()}"
                f" · usage_scope {usage_scope!r}"
                " — 다른 날짜로 물러서지도 다른 scope 를 뒤지지도 않는다"
            )

        # 🔴 **identity 는 새로, 값만 이관.** `evidence_grade` · `source_ref` ·
        #    `approved_by` 는 **물류 소유**라 마스터가 새로 쓰지 않는다.
        #
        # ★ `as_of` 는 여기 없다 — source 를 찾은 그 날짜가 그대로 실린다.
        새로정한다: dict[str, Any] = {
            "fixture_id": fixture_id,
            AXIS_COLUMN: sim_run_id,
        }
        실을것 = {**dict(source), **새로정한다}
        # 🔴 **되짚을 수 있게 출처를 단다.** 근거 셋을 그대로 따라가는 대신
        #    *"어느 행에서 왔나"* 를 적는 것이 마스터가 남길 몫이다.
        실을것["note"] = (
            f"{baseline_run_id} 의 {source['fixture_id']} 에서 이관"
            f" (as_of={as_of.isoformat()}, usage_scope={usage_scope})"
        )

        cursor.execute(
            sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
                sql.Identifier(schema),
                sql.Identifier(LOGISTICS_FIXTURE_TABLE),
                sql.SQL(", ").join(sql.Identifier(one) for one in 칸들),
                sql.SQL(", ").join(sql.Placeholder() for _ in 칸들),
            ),
            [실을것[one] for one in 칸들],
        )
    return fixture_id


def _read_logistics_fixture(
    cursor: Any,
    *,
    schema: str,
    columns: Sequence[str],
    baseline_run_id: str,
    as_of: date,
    usage_scope: str,
) -> Mapping[str, Any] | None:
    """이관할 물류 fixture 한 행을 **읽은 칸 그대로** 가져온다.

    ★ 좁히는 셋이 곧 `uq_log_runtime_fixture` 다 — 물류 `is_open` 이 묻는 열쇠와
      **같은 셋**이고, 그래서 최대 한 행이다.
    """
    cursor.execute(
        sql.SQL(
            "SELECT {} FROM {}.{} WHERE {} = %s AND as_of = %s AND usage_scope = %s"
        ).format(
            sql.SQL(", ").join(sql.Identifier(one) for one in columns),
            sql.Identifier(schema),
            sql.Identifier(LOGISTICS_FIXTURE_TABLE),
            sql.Identifier(AXIS_COLUMN),
        ),
        [baseline_run_id, as_of, usage_scope],
    )
    return cursor.fetchone()


def _read_finance_state(
    cursor: Any, *, schema: str, columns: Sequence[str], finance_state_id: str
) -> Mapping[str, Any] | None:
    """출발점 한 행을 **읽은 칸 그대로** 가져온다."""
    cursor.execute(
        sql.SQL("SELECT {} FROM {}.{} WHERE finance_state_id = %s").format(
            sql.SQL(", ").join(sql.Identifier(one) for one in columns),
            sql.Identifier(schema),
            sql.Identifier(FINANCE_STATE_TABLE),
        ),
        [finance_state_id],
    )
    return cursor.fetchone()


# ── ③ 장부를 지운다 ────────────────────────────────────────────────────


def reset_sim_run_ledger(conn: Any, *, sim_run_id: str) -> LedgerReset:
    """그 실행의 **장부를 지운다**. 표마다 몇 행을 지웠는지 돌려준다.

    🔴 **축 없는 자식까지 지운다.** 축을 가진 표만 비우면 그 표에 매달린 축 없는
      자식이 남고, 부모를 지우는 순간 FK 가 막는다 — 실제로 `master_decisions` 가
      `master_agent_runs` 를 붙잡아 `--reset` 이 거기서 섰다.

    ★ 지우는 것은 *"이번에 지우는 부모에 매달린 행"* 이다. 자식 표를 통째로 비우지
      않는다 — 비우면 다른 실행의 행까지 날아간다 (`_narrowing`).

    :raises ValueError: 축이 비었거나 **번인 실행**일 때. 🔴 번인에 기초 상태가
        있어서, 지우면 모든 실행의 출발점이 사라진다.
    :raises RuntimeError: 자식-부모 관계가 고리를 이뤄 순서를 못 세울 때.
        **아무 순서로나 던지지 않는다.**
    """
    if not sim_run_id or not sim_run_id.strip():
        raise ValueError("sim_run_id 없이 장부를 지울 수 없다 — 어느 실행인지를 지어내지 않는다")
    if sim_run_id == BURN_IN_SIM_RUN_ID:
        raise ValueError(
            f"번인 실행({sim_run_id})의 장부는 지우지 않는다"
            " — 여기에 기초 상태가 있고, 지우면 모든 실행의 출발점이 사라진다"
        )

    schema = get_db_schema()
    with conn.cursor() as cursor:
        축표들 = _axis_tables(cursor, schema=schema)
        관계 = _foreign_keys(cursor, schema=schema)
        # 🔴 **축 있는 표만으로는 부족하다.** 축이 없으면서 축 있는 표에 매달린
        #    자식이 있고, 그 행이 남아 있으면 부모를 지울 때 FK 가 막는다.
        대상들 = (*축표들, *_axisless_children(축표들, 관계))
        순서 = _child_first(대상들, tuple((fk.child, fk.parent) for fk in 관계))
        지운수: dict[str, int] = {}
        for 표 in 순서:
            조건, 값들 = _narrowing(
                표,
                schema=schema,
                axis_tables=축표들,
                foreign_keys=관계,
                targets=대상들,
                sim_run_id=sim_run_id,
            )
            cursor.execute(
                sql.SQL("DELETE FROM {}.{} WHERE {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(표),
                    조건,
                ),
                값들,
            )
            # 🔴 **세어서 담는다.** 0 도 담는다 — 안 담으면 *"지웠다"* 와
            #    *"지울 것이 없었다"* 가 같아진다.
            지운수[표] = cursor.rowcount
    return LedgerReset(sim_run_id=sim_run_id, order=순서, deleted=지운수)


# ── ③' 실행 행을 지운다 ────────────────────────────────────────────────


def delete_sim_run_row(conn: Any, *, sim_run_id: str) -> int:
    """다시 열 때 그 실행의 **행 자체를 지운다**. 지운 행 수를 돌려준다.

    ★★ **`reset_sim_run_ledger` 와 별개다.** 그 함수는 장부만 비우고 이 표에는
      손대지 않는다 — 실행 행을 지우는 것은 `--reset` 이 정하는 일이다
      (모듈 docstring).

    🔴 **`create_sim_run` 에 `ON CONFLICT` 를 붙이는 대신 이것을 쓴다.** 조용히
       덮으면 다른 설정으로 만들려던 실행이 옛 행 위에 앉는다.

    ★ **0 행도 정상이다.** 같은 이름으로 처음 여는 자리에서는 지울 행이 없고,
      그때 *"못 지웠다"* 고 말하면 여는 것이 막힌다. 몇 행이었는지는 돌려준다.

    ★ **번인을 여기서 다시 막지 않는다** — `--reset` 이 부르는 ① 이
      `reset_sim_run_ledger` 이고 거기서 이미 터진다.

    :raises ValueError: 축이 비었을 때. **어느 실행인지를 지어내지 않는다.**
    :raises RuntimeError: 장부가 남아 FK 가 막을 때. 🟢 **그 막힘이 자기 검사다** —
        사유에 **어느 표가 남았는지**와 제약 이름을 적는다.
    """
    if not sim_run_id or not sim_run_id.strip():
        raise ValueError("sim_run_id 없이 실행 행을 지울 수 없다 — 어느 실행인지를 지어내지 않는다")

    schema = get_db_schema()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DELETE FROM {}.{} WHERE {} = %s").format(
                    sql.Identifier(schema),
                    sql.Identifier(RUN_TABLE),
                    sql.Identifier(AXIS_COLUMN),
                ),
                [sim_run_id],
            )
            지운수 = cursor.rowcount
    except ForeignKeyViolation as exc:
        # 🟢 **여기서 막히는 것이 옳다.** 장부를 다 안 지웠다는 뜻이고, 그것을
        #    `ON DELETE CASCADE` 로 풀면 이 자기 검사가 사라진다.
        #
        # 🔴 **남은 표를 사유에 적는다.** 버리고 *"못 지웠다"* 만 남기면 사람이
        #    어느 표가 남았는지를 다시 찾아 헤맨다.
        진단 = getattr(exc, "diag", None)
        raise RuntimeError(
            f"실행 {sim_run_id!r} 의 행을 못 지웠다 — 그 실행의 장부가 남아 있다:"
            f" 표 {getattr(진단, 'table_name', None)!r}"
            f" · 제약 {getattr(진단, 'constraint_name', None)!r}"
            f" ({exc})"
            " — 남은 표를 지우는 것이 먼저다. FK 를 끄거나 CASCADE 를 달지 않는다"
        ) from exc
    return int(지운수)


def _axis_tables(cursor: Any, *, schema: str) -> tuple[str, ...]:
    """**축을 가진 표**를 읽는다 — 목록의 주인은 `information_schema` 다.

    ★ 뷰는 대상이 아니다(`BASE TABLE` 만). 실행 자체가 사는 `sim_runs` 도 뺀다 —
      다시 여는 것이지 없애는 것이 아니다.
    """
    cursor.execute(
        """
        SELECT col.table_name
        FROM information_schema.columns AS col
        JOIN information_schema.tables AS tab
          ON tab.table_schema = col.table_schema AND tab.table_name = col.table_name
        WHERE col.table_schema = %s
          AND col.column_name = %s
          AND tab.table_type = 'BASE TABLE'
        ORDER BY col.table_name
        """,
        [schema, AXIS_COLUMN],
    )
    표들 = tuple(row["table_name"] for row in cursor.fetchall() if row["table_name"] != RUN_TABLE)
    if not 표들:
        raise LookupError(
            f"{schema} 에서 축을 가진 표를 하나도 못 읽었다"
            " — 빈 목록으로 *'다 지웠다'* 고 답하지 않는다"
        )
    return 표들


def _foreign_keys(cursor: Any, *, schema: str) -> tuple[ForeignKey, ...]:
    """FK 를 **모양째** 읽는다 — 관계의 주인은 `pg_constraint` 다.

    🔴 **칸 목록까지 읽는다.** `(자식, 부모)` 이름만으로는 *"자식의 어느 칸이 부모의
      어느 칸을 가리키나"* 를 모르고, 그것을 모르면 축 없는 자식에서 **지울 행을
      못 좁힌다**. 그 자리에서 손으로 칸을 적으면 `master_decisions` 처럼 두 칸으로
      매달린 복합 FK 에서 틀린다.

    ★ `conkey` 와 `confkey` 는 **짝이 맞는 순서**로 들어 있다. `WITH ORDINALITY` 로
      그 순서를 지켜서 풀지 않으면 두 칸이 뒤바뀐 채 짝지어진다.
    """
    cursor.execute(
        """
        SELECT child.relname AS child_table,
               parent.relname AS parent_table,
               (SELECT array_agg(att.attname ORDER BY child_key.ord)
                  FROM unnest(fk.conkey) WITH ORDINALITY AS child_key(attnum, ord)
                  JOIN pg_attribute AS att
                    ON att.attrelid = fk.conrelid AND att.attnum = child_key.attnum
               ) AS child_columns,
               (SELECT array_agg(att.attname ORDER BY parent_key.ord)
                  FROM unnest(fk.confkey) WITH ORDINALITY AS parent_key(attnum, ord)
                  JOIN pg_attribute AS att
                    ON att.attrelid = fk.confrelid AND att.attnum = parent_key.attnum
               ) AS parent_columns
        FROM pg_constraint AS fk
        JOIN pg_class AS child ON child.oid = fk.conrelid
        JOIN pg_class AS parent ON parent.oid = fk.confrelid
        JOIN pg_namespace AS child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_namespace AS parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE fk.contype = 'f'
          AND child_ns.nspname = %s
          AND parent_ns.nspname = %s
        """,
        [schema, schema],
    )
    return tuple(
        ForeignKey(
            child=row["child_table"],
            child_columns=tuple(row["child_columns"]),
            parent=row["parent_table"],
            parent_columns=tuple(row["parent_columns"]),
        )
        for row in cursor.fetchall()
    )


def _axisless_children(
    axis_tables: Iterable[str], foreign_keys: Iterable[ForeignKey]
) -> tuple[str, ...]:
    """지우는 표에 매달린 **축 없는 자식**을 훑어 모은다.

    ★★ **손으로 안 적는다.** 축 없는 자식은 지금 열둘이지만, 열셋째가 생기는 날
      손으로 적은 목록은 조용히 뒤처진다 — 그리고 그 실패는 `--reset` 을 돌리는
      사람 앞에서 FK 위반으로 터진다.

    🔴 **따라가는 기준은 *"그 부모를 이번에 지우는가"* 다.** *"그 부모에 축이
      있는가"* 가 아니다. 지우지 않는 부모(품목·거래처 같은 기준 정보)로 따라가면
      이번 실행과 아무 상관 없는 행을 지우게 된다.

    ★ **한 번으로 안 끝난다.** 축 없는 자식이 또 축 없는 자식을 가질 수 있다
      (`pallet_events → pallets → inventory_lots`). 더 안 늘 때까지 훑는다.
    """
    대상 = set(axis_tables)
    관계 = tuple(foreign_keys)
    while True:
        더할것 = {
            fk.child
            for fk in 관계
            if fk.parent in 대상 and fk.child not in 대상 and fk.child != fk.parent
        }
        if not 더할것:
            return tuple(sorted(대상 - set(axis_tables)))
        대상 |= 더할것


def _narrowing(
    table: str,
    *,
    schema: str,
    axis_tables: Iterable[str],
    foreign_keys: Iterable[ForeignKey],
    targets: Iterable[str],
    sim_run_id: str,
) -> tuple[Any, list[str]]:
    """그 표에서 **지울 행만** 고르는 조건과, 거기 들어갈 값을 만든다.

    ```text
    축이 있는 표      sim_run_id = %s
    축이 없는 자식    (매다는 칸들) IN (SELECT 부모 칸들 FROM 부모 WHERE <부모의 조건>)
    ```

    🔴 **자식 표를 통째로 안 비운다.** `DELETE FROM purchase_items` 로 뭉뚱그리면
      다른 실행의 품목까지 날아간다 — 지울 것은 *"이번에 지우는 부모에 매달린 행"*
      이지 *"그 표의 모든 행"* 이 아니다.

    ★ 부모가 여럿이면 **OR** 다. 어느 한 부모라도 지워지면 그 자식 행은 못 남는다.

    ★ 조건이 부모의 조건을 다시 물고 들어간다 — 그래서 손자도 제 축 있는 조상까지
      되짚어 좁혀진다. 자식을 부모보다 **먼저** 지우므로, 이 조건을 재는 시점에
      부모 행은 아직 살아 있다.
    """
    if table in set(axis_tables):
        return sql.SQL("{} = %s").format(sql.Identifier(AXIS_COLUMN)), [sim_run_id]

    조각들: list[Any] = []
    값들: list[str] = []
    for fk in sorted(
        (one for one in foreign_keys if one.child == table and one.parent in set(targets)),
        key=lambda one: (one.parent, one.child_columns),
    ):
        if fk.parent == fk.child:
            continue
        부모조건, 부모값 = _narrowing(
            fk.parent,
            schema=schema,
            axis_tables=axis_tables,
            foreign_keys=foreign_keys,
            targets=targets,
            sim_run_id=sim_run_id,
        )
        조각들.append(
            sql.SQL("({}) IN (SELECT {} FROM {}.{} WHERE {})").format(
                sql.SQL(", ").join(sql.Identifier(one) for one in fk.child_columns),
                sql.SQL(", ").join(sql.Identifier(one) for one in fk.parent_columns),
                sql.Identifier(schema),
                sql.Identifier(fk.parent),
                부모조건,
            )
        )
        값들.extend(부모값)

    if not 조각들:
        # 지우는 부모가 하나도 없는데 목록에 들었다면 위상 계산이 어긋난 것이다.
        # 🔴 **조건 없이 DELETE 를 던지지 않는다** — 표 하나가 통째로 날아간다.
        raise RuntimeError(
            f"{table} 을 지울 근거가 없다 — 이번에 지우는 부모가 하나도 없다."
            " 조건 없이 DELETE 를 던지지 않는다"
        )
    return sql.SQL(" OR ").join(조각들), 값들


def _child_first(tables: Sequence[str], foreign_keys: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """**자식 먼저**로 순서를 세운다.

    ⚠️ 자식부터 안 지우면 FK 가 막는다. 부모를 먼저 지우려 하면 `ForeignKeyViolation`
      이 나고, 그때는 **일부만 지워진 장부**가 남는다.

    ★ 같은 층에서는 이름순으로 고정한다 — 같은 스키마에서 두 번 부르면 **같은
      순서**가 나와야 순서를 재는 검사가 성립한다.
    """
    남은 = set(tables)
    자식들: dict[str, set[str]] = {one: set() for one in 남은}
    for 자식, 부모 in foreign_keys:
        # 자기 자신을 가리키는 FK 는 순서를 못 정하는 근거가 아니다.
        if 자식 == 부모:
            continue
        if 자식 in 남은 and 부모 in 남은:
            자식들[부모].add(자식)

    순서: list[str] = []
    while 남은:
        # 아직 안 지운 자식이 하나도 안 남은 표부터 나간다.
        갈수있다 = sorted(one for one in 남은 if not (자식들[one] & 남은))
        if not 갈수있다:
            raise RuntimeError(
                f"자식-부모 관계가 고리를 이뤄 지우는 순서를 못 세운다: {sorted(남은)}"
                " — 아무 순서로나 던지지 않는다"
            )
        순서.extend(갈수있다)
        남은 -= set(갈수있다)
    return tuple(순서)
