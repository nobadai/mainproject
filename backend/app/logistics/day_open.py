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

🔴 **실행 축을 여기서도 본다 (2026-09-06).** 종전에는 `is_open` 이 `as_of +
   usage_scope` 만 물었다. `uq_log_runtime_fixture` 가 `(sim_run_id, as_of,
   usage_scope)` 라 **다른 실행의 행이 같은 날에 공존할 수 있고**, 그러면 하루 넘김이
   *"남의 실행이 열려 있으니 내 실행도 열려 있다"* 로 답했다.

   ```text
   SIM-A / 2026-09-06 / AGENT_MVP_DEMO   있다
   SIM-B / 2026-09-06 / AGENT_MVP_DEMO   없다

   종전  SIM-B is_open → True     ← SIM-A 를 보고 답했다. 그리고 SIM-B 행은 영영 안 선다
   지금  SIM-B is_open → False    ← 자기 실행만 본다
   ```

   ★ **열렸는지 판정하는 눈과 실제 상태를 읽는 눈이 같아야 한다.** 그래서
     `repository.get_active_logistics_runtime_fixture` 도 같은 축을 받도록 함께 넓혔다
     — 한쪽만 넓히면 *"열렸다고 본 행"* 과 *"읽은 행"* 이 다시 갈린다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.transition import USAGE_SCOPE, LogisticsFixtureMissing

__all__ = ["LogisticsDayOpening", "LogisticsRunAmbiguous"]

# ── 실행 축 조건 ────────────────────────────────────────────────────────
#
# 🔴 **두 질의가 같은 조건을 쓴다. 한 곳에 적는다.** `is_open` 과 carry-forward INSERT
#    가 각자 조건을 적으면 언젠가 한쪽만 바뀌고, 그 순간 *"열렸다고 본 행"* 과
#    *"물려받은 행"* 이 갈린다 — 이 파일이 고치려는 그 버그 그대로다.
#
# ★ 별칭 때문에 둘로 나뉘어 있다 (`is_open` 은 단일 표, carry-forward 는 `base`).
#   조각을 다시 조립하지 않고 통짜로 적는 이유는 **읽을 때 SQL 한 줄로 보여야** 하기
#   때문이다 (`sql.SQL` 을 f-string 으로 짓지 않는 규율도 함께 지킨다).
_NO_RUN_PREDICATE = sql.SQL("")
_RUN_PREDICATE = sql.SQL("AND sim_run_id = %(sim_run_id)s")
_BASE_RUN_PREDICATE = sql.SQL("AND base.sim_run_id = %(sim_run_id)s")


class LogisticsRunAmbiguous(ValueError):
    """같은 `(as_of, usage_scope)` 에 **서로 다른 실행의 활성 행**이 둘 이상인데,
    이 하루 넘김이 어느 실행인지 듣지 못했다.

    🔴 **여기서 하나를 고르지 않는다.** 첫 행도 최신도 고르는 것이고, 고르면 그
       순간 다른 실행의 어제가 이 실행의 오늘이 된다 —
       `repository.get_active_logistics_runtime_fixture` 가 활성 fixture 2건에서
       하나를 고르지 않는 것과 **같은 규율**이다.

    ⚠️ **`LogisticsFixtureMissing`(부재)과 다른 사실이다.** 저쪽은 *"물려받을 곳이
       없다"* 이고 여기는 *"물려받을 곳이 여럿이라 못 고른다"* 다. 부재로 접으면
       무결성 위반이 *"데이터를 주세요"* 로 나간다.

    ★ **이 예외가 뜨는 것은 `sim_run_id` 를 안 받은 배선뿐이다.** 받았으면 조회가 그
      실행으로 좁혀져 있어 애초에 둘이 보이지 않는다 (`app/main.py` 에서
      `LogisticsTransitionAdapter` · `LogisticsCancellationAdapter` 와 같은 모양으로
      주입하면 된다).
    """


class LogisticsDayOpening:
    """`app.master.day_open.DayOpening` 의 물류 구현.

    🔴 **commit 도 rollback 도 하지 않고 커넥션을 새로 열지도 않는다.** 커넥션은
       마스터가 주고 커밋은 두 파트가 모두 끝난 뒤 마스터가 한 번 한다. 여기서
       커밋하면 물류만 먼저 확정되고 뒤이어 다른 파트가 터졌을 때 **한쪽 날만 열린
       장부**가 남는다 (`persist_inventory` 와 같은 규율이다).

    🔴 **생성 인자 `sim_run_id` 는 `LogisticsTransitionAdapter` ·
       `LogisticsCancellationAdapter` 와 같은 자리다.** *"어느 실행의 장부인가"* 는
       물류 사실이 아니라 실행 정체성이고, 그 값의 주인은 마스터다. 모듈 상수로 박으면
       실행이 둘이 되는 날 물류 코드를 고쳐야 하므로 배선 자리(`app/main.py`)에서
       눈에 보이게 받는다.

       ⚠️ **종전에는 인자가 없었다** — *"하루 넘김은 정하는 것이 아니라 전날 행에서
          물려받는다"* 가 근거였다. 그 말은 **INSERT 의 `sim_run_id` 칸**에 대해서는
          지금도 맞다(`base.sim_run_id`). 틀렸던 것은 **어느 전날 행을 물려받을지**를
          고르는 자리다 — 그건 물려받는 것이 아니라 알고 있어야 하는 값이다.

    ⚠️ **`None` 을 받을 수 있다 — 다만 조용히 넘어가지 않는다.**

       ```text
       받았다     세 조회 모두 (sim_run_id, as_of, usage_scope) 로 좁힌다
       못 받았다  실행이 하나뿐이면 종전 그대로 · 둘 이상 보이면 LogisticsRunAmbiguous
       ```

       🔴 **`None` 은 "모든 실행 허용" 이 아니다.** 실행이 둘 보이는 순간 답하지 않고
          멈춘다. 기본값을 둔 이유는 배선 파일이 물류 소유가 아니라서다 — 필수 인자로
          만들면 주입 줄이 서기 전까지 앱이 import 시점에 죽는다.

    :param sim_run_id: 이 하루 넘김이 앉을 시뮬레이션 실행. **마스터가 소유한 값**이다.
    """

    def __init__(self, *, sim_run_id: str | None = None) -> None:
        # ★ 빈 문자열은 `None` 과 다른 실수다 — 주입은 했는데 값이 안 실린 것이라
        #   조용히 미주입으로 접으면 그 배선 실수가 안 보인다.
        if sim_run_id is not None and not sim_run_id.strip():
            raise ValueError(
                f"하루 넘김에 쓸 수 없는 sim_run_id 다: {sim_run_id!r}."
                " 주입하지 않을 것이면 None 이어야 한다 — 빈 문자열은 조회를 0건으로"
                " 만들고 그 0건은 '그날 행이 없다' 로 읽힌다."
            )
        self._sim_run_id = sim_run_id

    def is_open(self, conn: Any, *, as_of: date) -> bool:
        """그날 **내 실행의** 물류 fixture 행이 이미 있는가.

        ```text
        persist_inventory   sim_run_id + as_of + usage_scope   마스터가 값을 준다
        repository 조회      sim_run_id + as_of + usage_scope   같은 축으로 넓혔다
        is_open             sim_run_id + as_of + usage_scope   ← 생성 인자로 받는다
        ```

        🔴 **`sim_run_id` 를 받았으면 조건에 넣는다.** Protocol 이 `as_of` 만 주므로
           종전에는 넣을 자리가 없었는데, 그 값은 호출마다 달라지는 값이 아니라 이
           배선이 어느 실행인지라 **생성 인자**가 맞는 자리다.

        ⚠️ **못 받았으면 실행 수를 세어 본다.** 하나면 종전과 같은 답을 내고, 둘 이상
           보이면 `LogisticsRunAmbiguous` 로 멈춘다 — 어느 실행의 *"열림"* 인지 모르는
           채로 True 를 내면 **다른 실행의 행 하나가 내 실행의 모든 날을 열린 것으로
           만든다** (그리고 내 행은 영영 안 선다: 마스터는 `is_open` 이 참인 날을
           anchor 로 잡고 그 뒤만 만든다).

        ★ **`SELECT DISTINCT sim_run_id … LIMIT 2` 다.** 세려는 것은 행 수가 아니라
          **실행 수**이고, 둘까지만 보면 가릴 수 있다.
        """
        schema = sql.Identifier(get_db_schema())
        pinned = self._sim_run_id is not None
        query = sql.SQL(
            """
            SELECT DISTINCT sim_run_id
            FROM {}.logistics_runtime_fixture
            WHERE as_of = %(as_of)s
              AND usage_scope = %(usage_scope)s
              AND is_active
              {}
            LIMIT 2
            """
        ).format(schema, _RUN_PREDICATE if pinned else _NO_RUN_PREDICATE)

        with conn.cursor() as cursor:
            cursor.execute(query, self._params(as_of=as_of))
            실행들 = cursor.fetchall()

        if pinned:
            # ★ 조회가 이미 내 실행으로 좁혀져 있다 — 나온 것이 있으면 내 행이다.
            return bool(실행들)
        if len(실행들) > 1:
            raise LogisticsRunAmbiguous(
                f"같은 날에 활성 실행이 둘 이상이다 (as_of={as_of},"
                f" usage_scope={USAGE_SCOPE}, 실행 {len(실행들)}개 이상)."
                " 어느 실행의 하루 넘김인지 모르는 채로 열렸다고 답하지 않는다 —"
                " LogisticsDayOpening(sim_run_id=...) 로 주입해야 한다."
            )
        return bool(실행들)

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

        🔴 **물려받을 행도 내 실행에서만 고른다.** `sim_run_id` 를 받았으면 가드
           (`is_open`)와 INSERT 의 `WHERE` 가 **둘 다** 그 실행으로 좁는다. 한쪽만
           좁히면 *"SIM-A 가 열려 있으니 통과, 그런데 만들 행은 SIM-B 것"* 처럼
           **본 행과 만든 행이 갈린다.**
        """
        # ★ 먼저 물려받을 행이 있는지 본다. `is_open` 과 같은 질문이라 같은 함수를
        #   쓴다 — 조건이 갈리면 "열렸다고 본 날" 과 "물려받을 수 있는 날" 이 달라진다.
        if not self.is_open(conn, as_of=carry_from):
            raise LogisticsFixtureMissing(
                # ★ 무엇이 없는지 보이게 적는다. `as_of` 만으로는 만들려던 날이 없다는
                #   것인지 물려받을 날이 없다는 것인지 가릴 수 없다.
                # ★ 실행도 적는다 — 다른 실행에는 그날 행이 있는데 내 실행에만 없는
                #   경우가 이제 정상적으로 존재하고, 그 둘을 메시지가 갈라야 한다.
                f"물려받을 물류 runtime fixture 행이 없다 (sim_run_id={self._sim_run_id},"
                f" carry_from={carry_from},"
                f" usage_scope={USAGE_SCOPE}). {as_of} 행을 만들지 않는다 —"
                " evidence_grade · approved_by · 나머지 status 는 물류 판단이다."
            )

        schema = sql.Identifier(get_db_schema())
        with conn.cursor() as cursor:
            cursor.execute(
                self._carry_forward_query(schema, pinned=self._sim_run_id is not None),
                self._params(
                    as_of=as_of,
                    carry_from=carry_from,
                    # ⚠️ **승인이 만든 것처럼 보이면 안 된다.** 01-02 씨앗이
                    #    `MASTER-APPROVAL:RT-1` 이라 없는 승인을 가리키는 문제가 있었다
                    #    (2026-09-04 실측). 이 행을 만든 것은 승인이 아니라 하루 넘김이다.
                    source_ref=f"MASTER-DAY-OPEN:{as_of}",
                    note=(
                        f"하루 넘김이 {carry_from} 행에서 물려받아 세운 행이다."
                        " 승인이 만든 행이 아니다 - 그날 승인이 나면"
                        " persist_inventory 가 in_transit 두 칸을 덮는다."
                    ),
                ),
            )

    def _params(self, **값: object) -> dict[str, object]:
        """공통 파라미터에 `sim_run_id` 를 **받았을 때만** 얹는다.

        ★ **조건과 파라미터가 같은 값 하나(`self._sim_run_id is not None`)로 갈린다.**
          그래서 *"조건은 걸렸는데 값이 없다"* 도 *"값은 실렸는데 조건이 없다"* 도
          성립할 수 없다.

        ⚠️ 안 받았는데 `None` 을 실으면 조건이 없는 질의에 남는 열쇠가 되고, 나중에
           조건을 더할 때 `sim_run_id = NULL` 이 조용히 0건을 만든다.
        """
        params: dict[str, object] = {"usage_scope": USAGE_SCOPE, **값}
        if self._sim_run_id is not None:
            params["sim_run_id"] = self._sim_run_id
        return params

    @staticmethod
    def _carry_forward_query(schema: sql.Identifier, *, pinned: bool) -> sql.Composed:
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

        🔴 **`sim_run_id` 칸은 여전히 `base.sim_run_id` 다.** 마스터 상수를 가져다 쓰지
           않는다 — 쓰는 값은 물려받는 것이지 정하는 것이 아니다 (씨앗 SQL 머리말 §3 과
           같은 이유: 손으로 적으면 실행이 여럿이 되는 날 이 코드만 옛 값을 들고 남는다).

        ⚠️ **`WHERE` 에 얹는 `sim_run_id` 는 다른 이야기다.** 그것은 *"어느 전날 행을
           고를 것인가"* 이고, 안 좁히면 같은 날에 선 다른 실행의 행까지 함께 물려받아
           **한 번의 하루 넘김이 남의 실행 행도 만든다.**

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
              {}
            ON CONFLICT DO NOTHING
            """
        ).format(schema, schema, _BASE_RUN_PREDICATE if pinned else _NO_RUN_PREDICATE)
