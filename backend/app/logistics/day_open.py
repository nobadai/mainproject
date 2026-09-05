"""day_open.py — 하루가 넘어갈 때 **물류 runtime fixture 의 그날 행을 세운다.**

마스터가 `app/master/day_open.py` 에서 달력과 트랜잭션 경계를 쥐고, **무엇을
물려받고 무엇을 새로 둘지는 물류가 소유한다.** 이 파일이 그 물류 몫이다.

```text
마스터  언제 · 어느 날까지 · 한 커넥션 · 한 커밋      달력과 경계는 마스터 것이다
물류    logistics_runtime_fixture 의 그날 행 하나     무엇을 물려받을지는 물류가 안다
```

🟢 **새로 짠 SQL 이 아니다.** `database/27_logistics_runtime_fixture_20260105_20260106.sql`
   이 이미 정확히 이 carry-forward INSERT 이고, 여기서는 그 모양과 그 값을 그대로
   옮겼다. 어느 칸을 물려받고 어느 칸을 새로 두는지는 **그 파일이 정한 그대로다.**

⚠️ **씨앗 SQL 과 다른 자리가 딱 하나 있다 — `in_transit` 이다.**

   ```text
   씨앗 SQL    리터럴로 새로 뒀다      관통 Day1/Day2 를 세우려던 파일이라 그랬다
   open_day    물려받는다              in_transit 은 여러 날에 걸쳐 유지되는 상태다
   ```

   `in_transit` 은 매입 승인 ~ 창고 도착 ~ 검수 완료까지 여러 날에 걸쳐 유지된다.
   하루가 넘어갔다고 어제 떠 있던 물건이 사라지지 않는다. 여기서 `CONFIRMED_ZERO`
   로 새로 두면 **어제 승인된 입고 예정이 다음 날 조용히 없어진다.**

🔴 **`in_transit` 과 `confirmed_inbound` 는 짝이다. 한쪽만 물려받으면 안 된다.**

   한쪽만 물려받으면 B-1(`tools.py` `find_in_transit_schedule_gap`)이
   `IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE` 로 다음 날을 세운다.

   ★ **실측으로 겪은 자리다 (2026-09-04).** 승인 전이가 `in_transit` 만 채웠더니 다음
     날 물류가 경계를 못 냈고 `#275` 로 `confirmed_inbound` 를 병합해 풀었다.
     `app/master/day_open.DayOpening` docstring 이 물류 구현자에게 남긴 한 줄이 이것이다.

🔴 **`transition.py` 를 손대지 않았다.** `build_next_inventory` · `persist_inventory`
   는 승인이 부르는 경로이고 이 파일은 하루 넘김이 부르는 경로다. 두 경로가 같은 표의
   같은 행을 건드리지만 **쓰는 칸도 시점도 다르다** — 하루 넘김이 행을 세우고, 그날
   승인이 나면 `persist_inventory` 가 그 행의 `in_transit` 두 칸에 승인분을 **더한다**
   (2026-09-05 부터 덮어쓰기가 아니라 `inbound_id` 기준 누적이다).

⚠️ **물류가 자기 판단으로 바꿀 수 있는 자리다.** 팀 리드 지시로 마스터 파트가 옮겨
   적었을 뿐, 어느 칸을 물려받을지는 물류 소유다. 바꿀 때 `in_transit` 과
   `confirmed_inbound` 를 함께 다루는 것만 지키면 된다 (위 B-1).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.transition import USAGE_SCOPE, LogisticsFixtureMissing

__all__ = ["LogisticsDayOpening"]


class LogisticsDayOpening:
    """`app.master.day_open.DayOpening` 의 물류 구현.

    🔴 **commit 도 rollback 도 하지 않고 커넥션을 새로 열지도 않는다.** 커넥션은
       마스터가 주고 커밋은 두 파트가 모두 끝난 뒤 마스터가 한 번 한다. 여기서
       커밋하면 물류만 먼저 확정되고 뒤이어 다른 파트가 터졌을 때 **한쪽 날만 열린
       장부**가 남는다 (`persist_inventory` 와 같은 규율이다).

    ★ **생성 인자가 없다.** `LogisticsTransitionAdapter` 는 `sim_run_id` 를 마스터에게
      받는데 여기는 받지 않는다 — 하루 넘김은 그 값을 **정하는 것이 아니라 전날 행에서
      물려받기** 때문이다 (`_CARRY_FORWARD` 의 `base.sim_run_id`).
    """

    def is_open(self, conn: Any, *, as_of: date) -> bool:
        """그날 물류 fixture 행이 이미 있는가.

        ⚠️ **조건을 `usage_scope + as_of` 로 잡는다. `sim_run_id` 를 안 넣는다.**

        ```text
        persist_inventory   sim_run_id + as_of + usage_scope   마스터가 값을 준다
        repository 조회      usage_scope + as_of               읽는 쪽은 실행을 모른다
        is_open             usage_scope + as_of               ← 인자가 as_of 뿐이다
        ```

        `is_open` 은 `sim_run_id` 를 **받을 자리가 없다** (Protocol 이 `as_of` 만 준다).
        모듈 상수로 박으면 실행이 둘이 되는 날 물류 코드를 고쳐야 하고, 마스터에게
        받으면 *"어느 실행의 장부인가"* 가 하루 넘김 판단으로 올라온다.

        ★ **그리고 읽는 쪽과 같은 눈으로 보는 것이 맞다.** 그날 행이 있는지를 묻는
          이유는 어댑터가 그 행을 실제로 읽을 수 있는지이고, 어댑터가 지나는 길은
          `repository.get_active_logistics_runtime_fixture` 다. 그쪽이
          `usage_scope + as_of` 로 고르므로 여기서도 같은 조건으로 본다.

        ⚠️ 활성 행이 둘 이상인 것(`uq_log_runtime_fixture` 가
           `(sim_run_id, as_of, usage_scope)` 라 `sim_run_id` 가 다르면 공존한다)은
           **여기서 판정하지 않는다.** 무결성 위반은 `repository` 가 `ValueError` 로
           가른다 — 하루 넘김은 *"열려 있나"* 만 묻는다.
        """
        schema = sql.Identifier(get_db_schema())
        query = sql.SQL(
            """
            SELECT 1
            FROM {}.logistics_runtime_fixture
            WHERE as_of = %(as_of)s
              AND usage_scope = %(usage_scope)s
              AND is_active
            LIMIT 1
            """
        ).format(schema)

        with conn.cursor() as cursor:
            cursor.execute(query, {"as_of": as_of, "usage_scope": USAGE_SCOPE})
            return cursor.fetchone() is not None

    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:
        """`carry_from` 날 행을 물려받아 `as_of` 날 행을 만든다.

        🔴 **`carry_from` 행이 없으면 만들지 않고 예외를 던진다.** 물려받을 곳이 없는데
           행을 세우면 `evidence_grade` · `approved_by` · 세 status 를 기본값으로
           지어내게 되고, **지어낸 값이 그날의 사실로 남는다**
           (`LogisticsFixtureMissing` docstring 과 같은 이유다).

        ★ **INSERT 하나로 한다.** 값을 파이썬으로 읽어 와 다시 쓰면 그 사이에 값이
          모양을 바꾼다 (`jsonb` 왕복 · `Decimal` 왕복). `INSERT ... SELECT` 는 DB 안에서
          칸을 그대로 옮기므로 **물려받은 값이 어제 값과 같다는 것이 자명해진다.**

        ⚠️ **`ON CONFLICT DO NOTHING` 을 한 겹 더 둔다.** 마스터가 `is_open` 으로 이미
           거르지만, 두 번 열려도 두 번째가 아무 일도 안 하는 것이 여기서 보장된다.
           대상을 적지 않은 이유는 막을 것이 둘이라서다 — PK `fixture_id` 와 UNIQUE
           `uq_log_runtime_fixture (sim_run_id, as_of, usage_scope)`.
        """
        # ★ 먼저 물려받을 행이 있는지 본다. `is_open` 과 같은 질문이라 같은 함수를
        #   쓴다 — 조건이 갈리면 "열렸다고 본 날" 과 "물려받을 수 있는 날" 이 달라진다.
        if not self.is_open(conn, as_of=carry_from):
            raise LogisticsFixtureMissing(
                # ★ 무엇이 없는지 보이게 적는다. `as_of` 만으로는 만들려던 날이 없다는
                #   것인지 물려받을 날이 없다는 것인지 가릴 수 없다.
                f"물려받을 물류 runtime fixture 행이 없다 (carry_from={carry_from},"
                f" usage_scope={USAGE_SCOPE}). {as_of} 행을 만들지 않는다 —"
                " evidence_grade · approved_by · 나머지 status 는 물류 판단이다."
            )

        schema = sql.Identifier(get_db_schema())
        with conn.cursor() as cursor:
            cursor.execute(
                self._carry_forward_query(schema),
                {
                    "as_of": as_of,
                    "carry_from": carry_from,
                    "usage_scope": USAGE_SCOPE,
                    # ⚠️ **승인이 만든 것처럼 보이면 안 된다.** 01-02 씨앗이
                    #    `MASTER-APPROVAL:RT-1` 이라 없는 승인을 가리키는 문제가 있었다
                    #    (2026-09-04 실측). 이 행을 만든 것은 승인이 아니라 하루 넘김이다.
                    "source_ref": f"MASTER-DAY-OPEN:{as_of}",
                    "note": (
                        f"하루 넘김이 {carry_from} 행에서 물려받아 세운 행이다."
                        " 승인이 만든 행이 아니다 - 그날 승인이 나면"
                        " persist_inventory 가 in_transit 두 칸을 덮는다."
                    ),
                },
            )

    @staticmethod
    def _carry_forward_query(schema: sql.Identifier) -> sql.Composed:
        """전날 행에서 물려받는 INSERT.

        ```text
        물려받는다     in_transit · confirmed_inbound · confirmed_outbound
                       zone_capacity · usage_scope · evidence_grade · approved_by
                       sim_run_id
        새로 둔다      as_of · fixture_id · source_ref · note · is_active
        안 물려받는다  lot_priority (CONFIRMED_ZERO · [])
        ```

        🔴 **`lot_priority` 는 판단이라 물려받지 않는다.** 씨앗 SQL 이 그렇게 적었고
           (`database/27_...sql` 124행) 그대로 옮긴다. 어제 어느 로트를 먼저 내보내기로
           했는지는 어제의 판단이지 오늘의 사실이 아니다.

        🔴 **`sim_run_id` 는 `base.sim_run_id` 다.** 마스터 상수를 가져다 쓰지 않는다 —
           물려받는 것이지 정하는 것이 아니다 (씨앗 SQL 머리말 §3 과 같은 이유: 손으로
           적으면 실행이 여럿이 되는 날 이 코드만 옛 값을 들고 남는다).

        ★ `fixture_id` 도 씨앗 SQL 과 같은 식으로 만든다 —
          `LOG-RUNTIME-{sim_run_id}-{YYYYMMDD}`. 같은 날을 두 번 열어도 같은 id 가
          나와야 `ON CONFLICT` 가 걸린다.
        """
        return sql.SQL(
            """
            INSERT INTO {}.logistics_runtime_fixture (
                fixture_id, sim_run_id, as_of,
                in_transit_status,         in_transit_json,
                confirmed_inbound_status,  confirmed_inbound_json,
                confirmed_outbound_status, confirmed_outbound_json,
                lot_priority_status,       lot_priority_json,
                zone_capacity_status,      guaranteed_capacity_by_zone_json,
                usage_scope, evidence_grade, approved_by, source_ref, is_active, note
            )
            SELECT
                'LOG-RUNTIME-' || base.sim_run_id || '-' || to_char(%(as_of)s::date, 'YYYYMMDD'),
                base.sim_run_id,
                %(as_of)s::date,
                base.in_transit_status,         base.in_transit_json,
                base.confirmed_inbound_status,  base.confirmed_inbound_json,
                base.confirmed_outbound_status, base.confirmed_outbound_json,
                'CONFIRMED_ZERO',               '[]'::JSONB,
                base.zone_capacity_status,      base.guaranteed_capacity_by_zone_json,
                base.usage_scope,
                base.evidence_grade,            base.approved_by,
                %(source_ref)s,
                TRUE,
                %(note)s
            FROM {}.logistics_runtime_fixture base
            WHERE base.as_of = %(carry_from)s::date
              AND base.usage_scope = %(usage_scope)s
              AND base.is_active
            ON CONFLICT DO NOTHING
            """
        ).format(schema, schema)
