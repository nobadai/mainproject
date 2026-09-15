"""미적용 전이를 찾는 **조회 둘**. 🔴 **쓰기가 없다.**

*"승인은 났는데 매입 원장에 안 닿은 것"* 을 찾으려면 두 사실을 맞대야 한다.

```text
승인   master_decisions (+ 어느 실행인가는 master_agent_runs 가 든다)
원장   purchases
```

★★ **표를 새로 만들지 않는다.** 미적용 목록을 어딘가에 들고 있으면 **같은 사실의
  주인이 둘**이 되고, 한쪽만 고치는 날 갈린다. 둘 다 이미 있는 표이고, 맞대면 답이
  나온다 — `walk_report` 가 성적표를 새 표 없이 낸 것과 같은 자리다.

---

🔴 **ID 규칙을 SQL 이 알지 못하게 한다.**

  `purchase_id` 는 `PUR-{request_id}-D{decision_seq}-S{seq}` 다. 이것을 SQL 안에서
  `'PUR-' || d.request_id || '-D' || d.decision_seq || '-S'` 로 이어 붙이면 **ID
  규칙의 주인이 둘**(파이썬의 `transition.purchase_id_prefix_for` 와 이 SQL)이 되고,
  한쪽만 바뀌는 날 이 조회가 **에러 없이 늘 0건**을 돌려준다.

  ★ 그래서 이 파일은 **두 목록을 그냥 가져오기만** 한다.

  ```text
  ① approved_decisions(sim_run_id)   승인 행 (request_id · decision_seq · as_of)
  ② ledger_purchase_ids(sim_run_id)  그 실행의 purchase_id 전부
  ```

  맞대는 것은 `pending_transition.pending_approvals` 이고, 거기서 앞머리를 만드는
  것은 **ID 규칙의 주인 하나**다.

⚠️ **② 를 통째로 가져오는 것이 맞나.** 실행 하나의 매입 원장은 걷기 179일 × 품목
  셋이라 크기가 안 는다. 회차별 `LIKE` 를 SQL 로 밀면 위의 규칙 하나를 SQL 이
  알아야 하고, 그 대가가 이 편의보다 크다.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql

from app.finance.db import fetch_all, get_db_schema
from app.master.decision import PROCUREMENT_CYCLE

__all__ = [
    "approved_decisions",
    "ledger_purchase_ids",
]


def _table(name: str) -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(name))


def approved_decisions(*, sim_run_id: str) -> list[dict[str, Any]]:
    """이 실행 축에서 **지금 유효한 결정이 승인인** 업무 키 전부. 오래된 날부터.

    🔴 **`DISTINCT ON` 으로 업무 키마다 최신 회차 하나만 본다.** `master_decisions`
       는 append-only 라 번복도 새 행이고 **최대 회차가 유효하다**
       (`decision.mark_current` 가 같은 규칙을 파이썬에서 쓴다). 회차를 안 좁히면
       취소된 승인이 영영 미적용으로 남아 날마다 다시 서게 된다.

    🔴 **매입 사이클만 본다.** 판매 승인은 `sales` 표로 흘러 `purchases` 에 영영
       안 앉는다 — 안 좁히면 *"승인됐는데 원장에 없다"* 가 판매 행에 늘 참이 되고,
       재시도가 판매 승인을 매일 헛돌린다. 어휘의 주인은 `decision.PROCUREMENT_CYCLE`
       이고 여기서 문자열을 다시 적지 않는다.

    🔴 **실행 축이 필수다.** 안 좁히면 남의 걷기와 번인 30일의 승인까지 같이 끌고
       와서, 오늘 걷는 실행이 남의 장부를 고치려 든다.

    ★ **축의 주인은 실행 이력 행이다** (`master_agent_runs.sim_run_id` ·
      `decision_service._sim_run_id_of` 와 같은 자리). 결정 표에는 그 칸이 없다.

    :returns: `request_id` · `decision_seq` · `as_of` · `sim_run_id` 를 든 행들.
        승인이 없으면 **빈 목록** — 조회를 못 한 것과는 다르고, 그쪽은 예외로 오른다.
    """
    query = sql.SQL(
        """
        SELECT request_id, decision_seq, as_of, sim_run_id
        FROM (
            SELECT DISTINCT ON (d.request_id)
                   d.request_id  AS request_id,
                   d.decision_seq AS decision_seq,
                   d.decision     AS decision,
                   r.as_of        AS as_of,
                   r.sim_run_id   AS sim_run_id
            FROM {} AS d
            JOIN {} AS r
              ON r.run_id = d.run_id
             AND r.request_id = d.request_id
            WHERE r.sim_run_id = %s
              AND r.cycle = %s
            ORDER BY d.request_id, d.decision_seq DESC
        ) AS current_decision
        WHERE decision = %s
        ORDER BY as_of, request_id
        """
    ).format(_table("master_decisions"), _table("master_agent_runs"))
    rows = fetch_all(query, (sim_run_id, PROCUREMENT_CYCLE, "APPROVE"))
    return [dict(row) for row in rows]


def ledger_purchase_ids(*, sim_run_id: str) -> list[str]:
    """이 실행 축의 매입 원장에 **실제로 서 있는** Header ID 전부.

    ★ **행의 유무만 묻는다.** 무슨 값이 어느 칸에 들었는지는 `ledger.py` 의 일이고,
      여기서 알아야 하는 것은 *"이 승인이 원장에 닿았나"* 하나다.

    🔴 **`settlement_status` 로 안 거른다.** `CANCELLED` 인 행도 **닿은 것**이다 —
       거르면 취소된 매입이 미적용으로 돌아와 같은 승인이 두 번 앉는다.
    """
    query = sql.SQL("SELECT purchase_id FROM {} WHERE sim_run_id = %s").format(
        _table("purchases")
    )
    return [row["purchase_id"] for row in fetch_all(query, (sim_run_id,))]
