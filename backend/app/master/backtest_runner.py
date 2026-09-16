"""
backtest_runner.py — **범위를 하루씩 걸으며 하루 실행을 부른다.**

```text
walk(sim_run_id=..., start=..., end=..., now=...)   start..end 를 하루씩 걷는다
                                                    개장일마다 run_scheduled_day 를 부른다
```

🔴 **실행 축이 인자다. 기본값이 없다** (2026-09-10). 179일을 **어느 실행에 쌓는지**가
   곧 그 곡선의 정체다 — 기본값을 두면 말 안 하고 번인(`SIM-BURNIN-202512`)에 쌓을 수
   있고, 사람이 심어 둔 30일 위에 179일이 겹쳐 앉는다.

🔴 **여기에 판단이 없다. 개장도 입고도 채권도 수금도 출고도 여기서 다시 짜지 않는다.**

  그 순서는 `scheduler.run_scheduled_day` 가 이미 안다. 이 파일은 **날짜 축**만
  진다 — 어느 날을 걷고 어느 날을 건너뛰고 어디서 멈추는가.

★ **왜 저장소에 세우나.** 179 영업일 걷기를 임시 스크립트로 돌려 성적을 냈는데,
  그 스크립트가 저장소 밖이라 다른 사람이 같은 걸음을 못 걷는다. 성적만 남고
  **그 성적을 낸 걸음이 안 남는 것**이 문제다.

⚠️ **그 임시 스크립트는 `run_procurement` 을 직접 불렀다.** 그래서 개장 · 입고 ·
  채권 · 수금 · 장부 관문이 한 번도 안 돌았고, *"사고 0건"* 은 **그 다섯 단계를 안 탄
  채로** 나온 숫자였다. 이 파일이 고치는 것이 정확히 그것이다.

```text
❌ run_procurement(ProcurementRunRequest(as_of=d, ...))   개장·입고·채권·수금·장부게이트를 건너뛴다
🟢 run_scheduled_day(action, ...)                          오늘 선 순서 전부를 안다
```

---

🔴 **승인은 명시로만 켠다** (2026-09-11).

```text
--auto-approve 를 **안 주면**   승인 함수가 이름조차 안 불린다 · 한 건도 안 선다
--auto-approve 를 주면          각 판단 **바로 뒤**에 규칙대로 승인이 선다
```

★★ **설정에 규칙이 있다고 켜지지 않는다.** *"있으니까 한다"* 는 암묵 스위치이고,
  그러면 설정을 실험하려고 넣은 사람이 **승인까지 하게 된다.**

⚠️ **반대로, 켰는데 규칙이 없으면 걷기 전에 막는다.** 조용히 걸으면 179일 뒤에
  0건이 나오고 사람은 그것을 *"돌았는데 해당이 없었구나"* 로 읽는다.

---

🔴 **물류 유지보수도 명시로만 켠다** (2026-09-11).

```text
--auto-maintain 를 **안 주면**   유지보수 함수가 이름조차 안 불린다 · 한 Lot 도 안 없앤다
--auto-maintain 를 주면          **개장 바로 뒤**에 그날 자리를 비운다
```

★★ **승인보다 더 조심할 자리다.** 승인은 `master_decisions` 에 한 줄이 남는
  것이지만 폐기는 **물건이 없어진다** — 물류가 *"되돌릴 경로가 없다(`ADJUST_IN`
  없음 · 실사 제외)"* 고 못박았다.

⚠️ **켰는데 버릴 것이 없어도 막지 않는다.** `--auto-approve` 와 축이 다르다 —
  저쪽은 규칙 파일이 있어야 성립하고, 이쪽은 확인할 설정이 없다. 버릴 것이 없는
  날은 사고가 아니라 정상이고, 그 사실은 `NOTHING_DUE` 가 말한다.

---

🔴 **`now` 를 인자로 받는다. 시계를 안 읽는다.**

  `plan_next_action` 이 마감(10:30)과 비교하는 값이 `now` 다. 이 파일이 시계를
  읽으면 *"마감 뒤"* 상태를 검사가 만들 수 없고, 걷기 결과가 **돌린 시각에 따라
  갈린다.**

  ★ **받은 시각에서 시각만 떼어 그날에 붙인다.** 걷는 날마다 마감 비교가 그날의
    10:30 을 봐야 하기 때문이다. 받은 `now` 를 그대로 179일에 다 쓰면 첫날 말고는
    전부 *"마감이 한참 지난 미래"* 가 된다.

  ⚠️ 시간대는 받은 값의 것을 그대로 나른다 (`timetz()`). 여기서 `ZoneInfo` 를
    새로 만들지 않는다 — 시간대의 주인도 `clock.py` 하나다.

  🔴 **받은 `--now` 를 요약에 찍고 원장에 남긴다** (2026-09-13).

```text
요약    기준시각  <받은 문자열> · 날마다 HH:MM · 마감 10:30 뒤 / 🔴 전
원장    sim_runs.config_json.provenance.walked_now   ← 걷기 첫 날 전에 · 받은 문자열 그대로
```

  ★★ V8(16:00) 과 V9①(09:00) 이 같은 코드로 통째로 갈렸는데 그 값이 어디에도 안
    남아 있었다. **적는 것은 걷기다** — 진짜 값을 아는 것은 걷기뿐이다
    (`walk_provenance`). 이미 **다른** 값이 있으면 걷기 전에 막는다.

---

🔴 **달력을 못 읽으면 멈춘다. 건너뛰지 않는다.**

```text
is_market_open(day) == False   → 건너뛴다 (skipped_days 에 남는다)
CalendarNotCovered             → 🔴 멈추고 사유를 낸다
```

★ 그 예외는 *"장이 서는지 아닌지를 이 표로는 말할 수 없다"* 는 뜻이다. 건너뛰면
  **모르는 날을 안 서는 날로** 바꾸는 것이고, 그러면 성적표의 분모가 조용히 줄어든다.

---

🔴 **하루가 터져도 다음 날을 계속 걷는다. 다만 상한을 둔다.**

  ★ 하루가 터졌다고 걷기를 멈추면 179일 중 3일째에서 끝나고 나머지를 못 본다.

  ⚠️ **그런데 상한이 없으면 반대로 망가진다.** 장부가 깨진 채 179일을 걸으면 사고
    목록이 179줄이고 아무도 안 읽는다. 그리고 그 179줄은 **한 가지 사실**이다 —
    첫날 깨진 것이 안 고쳐졌다는 것. 연속 실패가 상한에 닿으면 멈추고 사유를 낸다.

---

🔴 **현금 축을 읽는다. 세지 않는다** (2026-09-12).

```text
현금        {매입유출: n · 물류유출: n · 인건이자: n · 수금: n · 순현금: n · 기말잔액: n}
현금항등식  Δ잔액 n · Σ순현금 n · 차이 n · 어긋난 날 n일 → 🟢 성립 / 🔴 깨짐
```

★★ **걷기가 현금 축을 아예 안 보고 있었다.** 칸은 `daily_closings` 에 처음부터
  있었고, V7 에서 매입 현금유출 27,122,228 원이 **흐름에는 잡히는데 잔액에서 안
  빠지는데도** 179일 동안 아무 줄도 그 사실을 말하지 않았다.

  ★ **고치는 것은 재무 몫이다** (recognition 과 settlement 사이 지급 전이). 이
    파일이 하는 것은 **걷기가 그 불일치를 스스로 말하게** 하는 것뿐이다.

🔴 **깨져도 `incidents` 에 안 넣는다.** 지급 전이가 서기 전에는 매일 깨지고, 사고로
  세면 `MAX_CONSECUTIVE_FAILURES` 에 걸려 **걷기가 못 끝난다** — 그러면 정본 판을
  못 돌린다. 🟢 센다 · 찍는다 · 판정을 낸다 / 🔴 멈추지 않는다 · `사고` 줄을 안
  건드린다.

⚠️ **어휘를 새로 만들지 않았다.** 세는 값은 전부 `scheduler` 가 낸 것 그대로다
  (`DayRunOutcome.action` · `ItemRunOutcome.end_code` · `procurement_status` 의
  `NOT_ATTEMPTED` · `sales_status` 의 세 값 · `outbound_status` 의 네 값 ·
  `RetryOut.outcomes` 의 네 값 · `MaintenanceOut.outcomes` 가 나르는
  `MaintenanceOutcome` 의 네 값 · `failed_items`). 그 네 값은 **접지
  않고 그대로 센다** — `NOTHING_DUE`(없다)와 `FAILED`(못 했다)와
  `NOT_ATTEMPTED`(안 했다)를 묶으면 손익 곡선이 왜 평평한지를 성적표가 못 답한다.
  이 파일이 새로 만든 말은 걷기 자체에 관한 것
  뿐이다 (`skipped_days` · `incidents` · `stopped_reason`) — 그것은 `scheduler` 가
  모르는 사실이다. 스케줄러는 하루만 알지 **범위를 모른다.**
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, get_args

from app.master.backfill import (
    BackfillRuleMissing,
    BackfillRules,
    SalesTermsRule,
    read_run_rules,
)
from app.master.bootstrap import wire_registries

# 🔴 **마감 어휘도 주인에서 읽는다** (2026-09-16). `ClosingOut.status` 의 다섯 값을 여기서
#   손으로 적으면 어휘가 느는 날 요약만 옛말을 하고 새 값의 0 이 안 찍힌다.
from app.master.closing import ClosingOut

# 🔴 **어휘의 주인에서 들여온다. 여기서 네 이름을 안 적는다** (2026-09-12).
#   손으로 적으면 어휘가 느는 날 요약만 옛말을 하고, 새로 든 값이 성적표에서
#   조용히 사라진다 — `envelope` 가 `get_args` 로 한 벌만 만드는 이유 그대로다.
from app.master.envelope import LLM_STATUSES
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, day_forecast_readiness

# 🔴 **점검 칸 이름과 어휘도 주인에서 들여온다** (2026-09-14). 위 `LLM_STATUSES` 와 같은
#   결이다 — 손으로 적으면 어휘가 느는 날 요약만 옛말을 하고 새 값의 0 이 안 찍힌다.
from app.master.inspection import AFTER_INBOUND, AFTER_OUTBOUND, INSPECTION_STATUSES

# 🔴 **막는 갈래 이름의 주인에서 들여온다. 여기서 안 짓는다** (2026-09-16).
#   `INSPECTION_STATUSES` · `LLM_STATUSES` 와 같은 결이다 — 세는 쪽이 자기 이름을
#   지으면 갈래가 둘이 되고, 그때 나는 것은 오류가 아니라 **조용한 0** 이다.
from app.master.ledger import LEDGER_BLOCK_KINDS, PERMANENT_BLOCK_KINDS

# 🔴 **마감 칸 이름의 주인에서 들여온다. 여기서 칸 이름을 안 적는다** (2026-09-12).
#   손으로 적으면 표가 바뀌는 날 요약만 옛 이름을 말하고, 그때 나는 것은 오류가
#   아니라 **조용한 0** 이다 — 위 `LLM_STATUSES` 와 같은 결이다.
#
# ⚠️ **`LOAN_CASH_BALANCE` 는 안 들여온다.** 대출 포함 곡선은 이 항등식의 축이
#   아니다 (아래 `cash_identity`).
from app.master.ledger_repository import (
    BASE_CASH_BALANCE,
    COLLECTION_CASH_IN,
    LOGISTICS_CASH_OUT,
    NET_CASH,
    OPERATING_EXPENSE_CASH_OUT,
    PAYROLL_INTEREST_CASH_OUT,
    PURCHASE_CASH_OUT,
    read_walk_closings,
)
from app.master.market_calendar import MarketCalendar, get_market_calendar
from app.master.ml_batch_calendar import MlBatchCalendar, get_ml_batch_calendar
from app.master.sales_terms import read_run_sales_terms
from app.master.scheduler import (
    DAILY_POLICY_VERSION,
    DayRunOutcome,
    DayScope,
    # 🔴 **마감의 주인에서 읽는다. 여기서 10:30 을 다시 적지 않는다** (2026-09-13).
    #   손으로 적으면 마감이 바뀌는 날 요약만 옛 마감을 말하고, 그 줄이 막으려던
    #   「마감 전인데 모른다」 가 반대 방향으로 다시 선다.
    deadline_at,
    plan_next_action,
    run_scheduled_day,
)

# 🔴 **승인 하나를 가르는 키를 여기서 짓지 않는다** (2026-09-16). 그 파일이
#   *"여기까지가 승인 하나를 가리킨다"* 고 적어 둔 앞머리가 그대로 동일성 키다 —
#   원장 행 ID 를 짓는 규칙과 **같은 함수**라 둘이 갈릴 수가 없다.
from app.master.transition import purchase_id_prefix_for
from app.master.walk_provenance import WalkedNowStamp, record_walked_now

__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "CashIdentity",
    "WalkIncident",
    "WalkResult",
    "format_summary",
    "main",
    "walk",
]

#: 같다고 보는 한계. `numeric(18,6)` 이라 소수점 찌꺼기가 남는다 — 그것을 사고로
#: 세면 매일이 사고가 되고, 사고 목록이 아무것도 안 가리킨다.
_ONE_WON = Decimal(1)

_ZERO = Decimal(0)

#: 연속 사고 상한. **닿으면 멈추고 사유를 낸다.**
#:
#: ★ **왜 5인가.** 영업일 닷새다. 한 주를 통째로 못 걸었으면 그 다음 날의 장부는
#:   이미 못 믿는다 — 안 받은 입고와 안 받은 수금이 닷새치 쌓인 위에서 판단이 돌고,
#:   거기서 나온 성적은 걷기의 성적이 아니라 **깨진 장부의 성적**이다.
#:
#: ⚠️ `calendar_walk.MAX_WALK_DAYS` 를 안 쓴다. 그 31 은 *"다음 실행일을 찾으러 앞으로
#:   걷는 상한"* 이고 축이 다르다 — 179일 걷기는 그 수를 당연히 넘긴다.
MAX_CONSECUTIVE_FAILURES = 5

#: 관측 기준시점을 세는 **두 칸의 이름** (2026-09-12). 🔴 **여기가 유일한 주인이다.**
#:
#: 요약 줄과 세는 자리가 각자 문자열을 적으면 한쪽만 고치는 날 이름이 갈리고,
#: 성적표는 제가 안 세는 칸을 찍는다 — `llm_outcomes` 가 `envelope.LLM_STATUSES`
#: 하나를 보는 것과 같은 규율이다.
#:
#: 🔴 **두 칸뿐이다.** 세 번째 칸(「미래를 봤다」)은 `observed_at > as_of` 를 막는
#:   검사를 걸 때 생긴다 — 지금 만들면 아무도 안 채운 상태에서 전부 막힌다.
#:
#: ⚠️ **순서가 뜻이다.** 「실었다」가 먼저다 — 그 숫자가 늘어나는 것이 진도이고,
#:   읽는 사람이 먼저 볼 자리다. 그래서 이 줄만 `sorted` 를 안 쓴다.
_OBSERVED_AT_LABELS: tuple[str, str] = ("실었다", "안쟀다")

#: 마감 줄이 찍는 어휘 (2026-09-16). 🔴 **여기서 이름을 안 적는다** — `ClosingOut.status`
#: 의 다섯 값과, 단계를 안 탄 날 `DayRunOutcome` 이 두는 기본값 그대로다
#: (`inspection_statuses` 가 `DayRunOutcome` 기본값을 읽는 것과 같은 결).
_CLOSING_STATUSES: tuple[str, ...] = (
    *get_args(ClosingOut.model_fields["status"].annotation),
    DayRunOutcome.closing_status,
)


@dataclass(frozen=True)
class WalkIncident:
    """걷다 만난 사고 하나. **날짜와 사유만 든다.**

    ★ 사유 문장은 `scheduler` 가 낸 값을 그대로 옮긴다. 여기서 다시 이름 붙이면
      같은 사실에 이름이 둘이 된다.
    """

    as_of: date
    reason: str


@dataclass(frozen=True)
class CashIdentity:
    """현금 항등식 한 판 (2026-09-12). **잔액이 흐름만큼 움직였는가.**

    ```text
    기초잔액 = 첫 마감행의 잔액 − 첫 마감행의 순현금
    Δ잔액    = 마지막 마감행의 잔액 − 기초잔액
    Σ순현금  = 순현금 합
    성립     = |Δ잔액 − Σ순현금| < 1원
    ```

    ★★ **V7 걷기에서 매입 현금유출 27,122,228 원이 흐름에는 잡히고 잔액에서는
      안 빠졌다** (실측 2026-09-12). 재무가 *"recognition 과 settlement 사이에 빠진
      계층"* 으로 정리했고 지급 전이를 세우는 중이다 — **그 수정은 재무 몫이고**
      이 값이 하는 일은 그 불일치를 걷기가 **스스로 말하게** 하는 것뿐이다.

    🔴 **여기서 아무것도 고치지 않는다.** 잔액을 다시 세지도, 맞춰 주지도 않는다.
      마스터가 부서 값을 고치기 시작하면 같은 사실의 주인이 둘이 된다.
    """

    #: 걷기 앞에 서 있던 잔액. **첫 행이 말한다** — 표는 그 앞을 말하지 않는다.
    opening_balance_krw: Decimal
    #: 기초에서 기말까지 잔액이 움직인 폭.
    balance_delta_krw: Decimal
    #: 그 구간 순현금 합.
    net_cash_krw: Decimal
    #: 하루 단위로 어긋난 날 수. 🔴 **첫날은 안 센다** — 앞 잔액이 없다.
    #:
    #: ★ 합만 맞고 날마다 어긋나는 판이 있다. 그 둘은 다른 사실이라 따로 센다.
    mismatched_days: int

    @property
    def gap_krw(self) -> Decimal:
        """Δ잔액 − Σ순현금. **0 이면 성립이다.**"""
        return self.balance_delta_krw - self.net_cash_krw

    @property
    def holds(self) -> bool:
        """성립하는가. `|차이| < 1원`."""
        return abs(self.gap_krw) < _ONE_WON


@dataclass(frozen=True)
class LedgerBlocks:
    """승인은 났는데 **매입 원장에 한 행도 안 남은** 것들 (2026-09-16).

    🔴 **크기와 소음을 한 수로 접지 않는다.**

    ```text
    unique          고유 미기록 승인 건수 — 같은 승인은 한 번만 센다   ← 크기
    retries         그 건들이 다음 날 재시도에서 다시 막힌 횟수        ← 소음
    permanent       그중 영영 안 될 것. **고유 건수로 센다**           ← 크기
    unique_by_kind  갈래별 고유 건수. 0 인 갈래도 든다
    ```

    ★★ **접으면 한 건이 며칠치로 부푼다.** `retry_pending_transitions` 가 같은 약정을
      날마다 다시 세우고 같은 사유로 또 막히기 때문이다 — 실측에서 `NOT_APPLIED 12`
      였는데 복수 등급 승인안은 **실제로 1건**이었다.

    ⚠️ **`unique_by_kind` 의 합이 `unique` 보다 클 수 있다.** 한 승인이 날을 달리해
      다른 갈래로 막히면 양쪽에 다 든다 — 그 승인을 **한쪽에서 빼면** 그 갈래가
      실제보다 적어 보인다. `unique` 는 그때도 승인 수를 말한다.
    """

    unique_by_kind: Mapping[str, int]
    unique: int
    retries: int
    permanent: int


def _approval_key(request_id: str | None, decision_seq: int | None, run_id: str) -> str:
    """승인 하나를 가르는 키. 🔴 **여기서 규칙을 짓지 않는다.**

    ★ 주인은 `transition.purchase_id_prefix_for` 다 — 그 함수가 *"여기까지가 승인
      하나를 가리킨다"* 고 적어 둔 앞머리이고, **원장 행 ID 를 짓는 규칙과 같은
      함수**라 둘이 갈릴 수가 없다 (`PUR-{request_id}-D{decision_seq}-S`).

    🔴 **`request_id` 하나로 접지 않는다.** 같은 업무 키에 결정이 여러 번 붙을 수
       있고 (`decision_seq` 가 그래서 있다), 접으면 서로 다른 승인 둘이 한 건이 된다.

    ⚠️ **키를 못 만들면 조용히 빼지 않는다.** 실행 행 하나는 승인 하나를 넘지 않으므로
      `run_id` 로 떨어뜨린다 — 같은 승인을 두 건으로 셀지언정 **안 센 것으로 만들지는
      않는다.** (막힌 행은 승인 문을 지난 행이라 여기 오는 일이 없어야 한다.)
    """
    if request_id and decision_seq is not None:
        return purchase_id_prefix_for(request_id, decision_seq)
    return f"RUN:{run_id}"


@dataclass(frozen=True)
class WalkResult:
    """걷기 한 번의 결과.

    ⚠️ **`days` 는 실제로 하루 실행을 부른 날만 든다.** 휴장일은 `skipped_days` 로
      간다 — 둘을 섞으면 *"179일 중 몇 날을 돌았나"* 를 못 센다.
    """

    start: date
    end: date
    #: 하루 실행을 부른 날의 결과. **부른 순서 그대로.**
    days: tuple[DayRunOutcome, ...] = ()
    #: 달력이 *"안 선다"* 고 한 날.
    skipped_days: tuple[date, ...] = ()
    #: 사고 목록. **터진 날도 걷기는 이어졌다** (상한에 닿기 전까지).
    incidents: tuple[WalkIncident, ...] = ()
    #: 걷기가 끝까지 못 갔으면 멈춘 날. 끝까지 갔으면 `None`.
    stopped_at: date | None = None
    #: 멈춘 사유. 🔴 **`stopped_at` 과 짝이다** — 하나만 있으면 안 된다.
    stopped_reason: str | None = None
    #: 소요 시간(초). 벽시계가 아니라 단조 시계로 잰다 (아래 `ticks` 주석).
    elapsed_seconds: float = 0.0
    #: 그 구간의 마감행 (2026-09-12). 🔴 **재무가 적은 값을 그대로 든다.**
    #:
    #: ★ **여기서 다시 세지 않는다.** 마스터가 부서 값을 재계산하면 같은 사실의
    #:   주인이 둘이 되고, 재무가 세는 값과 갈리는 날 **에러 없이 손익만 틀린다.**
    #:
    #: ⚠️ **한 행도 없으면 빈 튜플이고 그것이 답이다** — 0 으로 메운 행을 지어내지
    #:   않는다. *"마감이 안 돌았다"* 와 *"돌았는데 0 이다"* 는 다른 사실이다.
    closings: tuple[Mapping[str, Any], ...] = ()
    #: 마감행을 **못 읽었으면** 그 사유 (2026-09-12). 읽었으면 `None`.
    #:
    #: 🔴 **「0행」과 접지 않는다.** *"못 읽었다"* 를 *"없다"* 로 적으면 DB 가 죽은
    #:   판과 마감이 한 번도 안 돈 판이 화면에서 같아진다.
    closings_reason: str | None = None
    #: 걷기가 받은 `--now` **문자열 그대로** (2026-09-13). 안 받았으면 `None`.
    #:
    #: 🔴 **정규화하지 않는다.** 사람이 준 것과 코드가 쓰는 것(그 시각 부분)이 둘 다
    #:   보여야 같은 코드로 건 두 판이 왜 갈렸는지가 읽힌다 — V8(16:00) 과
    #:   V9①(09:00) 이 그 한 값으로 통째로 갈렸다.
    walked_now: str | None = None

    @property
    def completed(self) -> bool:
        """끝까지 걸었는가."""
        return self.stopped_reason is None

    @property
    def actions(self) -> Mapping[str, int]:
        """판단 분포. **`SchedulerAction` 값을 센다** — 새 이름을 안 붙인다."""
        return Counter(one.action for one in self.days)

    @property
    def end_codes(self) -> Mapping[str, int]:
        """품목 종료 코드 분포. **못 돈 품목은 `FAILED` 로 센다.**

        ★ `end_code` 가 `None` 인 것은 *"코드가 없었다"* 이고, 그 자리는 `status` 가
          이미 `FAILED` 라고 말한다 (`ItemRunOutcome` 의 어휘 그대로).
        """
        return Counter(
            one.end_code if one.end_code is not None else one.status
            for day in self.days
            for one in day.items
        )

    @property
    def sales_end_codes(self) -> Mapping[str, int]:
        """판매 판단의 품목 종료 코드 분포 (2026-09-10). 🔴 **매입과 한 칸에 안 담는다.**

        ★ **왜 `end_codes` 와 가르나.** 어휘가 다르다 — 매입은 `E1`·`E4`, 판매는
          `SL1`·`SL4` 다. 한 Counter 에 담으면 *"오늘 어느 사이클이 어떻게 끝났나"*
          가 두 어휘가 섞인 한 표가 되고, 어느 쪽 수가 는 것인지를 못 읽는다.

        🔴 **접지 않는다.** `SL1_PRESENTED` 를 *"돌았다"* 로 묶으면 걷기 179일에
          **후보가 실제로 나온 날이 며칠인지**를 성적표가 못 답한다 — 이 판이
          존재하는 이유가 그 숫자다.
        """
        return Counter(
            one.end_code if one.end_code is not None else one.status
            for day in self.days
            for one in day.sales_items
        )

    @property
    def llm_outcomes(self) -> Mapping[str, int]:
        """그 걷기에서 **LLM 이 실제로 돌았나** (2026-09-12). 🔴 **넷을 접지 않는다.**

        ```text
        DISABLED           설정으로 껐다 — 🟢 정상이다
        SKIPPED_TEMPLATE   켜져 있는데 부를 조건이 아니었다 — 🟢 정상이다
        SUCCESS            불렀고 쓸 수 있는 답을 받았다
        FALLBACK           🔴 불렀는데 실패했다 — 규칙이 대신 답했다
        ```

        ★★ **이 줄이 없어서 71영업일을 `SUCCESS` 0건으로 걷고도 아무도 몰랐다**
          (`SIM-CHAIN-V6` 실측 2026-09-12). 재무가 918건 전부 `FALLBACK` 이었고
          2026-09-11 19시대 뒤로는 성공이 한 건도 없었는데, 원장에는 처음부터 다
          적혀 있었고 **요약에 안 나와서 아무도 안 봤다.** `plan.py:62` 가 이미
          적어 둔 위험 그대로다 — *"규칙 경로로 떨어져도 산출물은 멀쩡해 보인다."*

        🔴 **0 인 어휘를 빼지 않는다.** 여기가 다른 어휘 줄과 갈리는 자리다.
          저쪽은 *"그 사건이 없었다"* 를 빈 칸으로 말하지만, 이쪽에서 `SUCCESS` 가
          없다는 것은 **그 자체가 사고**다 — 빼고 찍으면 오늘 이 사태의 모양이
          그대로 다시 선다. 네 값은 늘 닫힌 집합이라 채울 수 있고, 채워야 한다.

        ★ **부서별로 안 가른다.** 지금 필요한 것은 *"돌았나 안 돌았나"* 이고,
          합계에서 `FALLBACK` 이 0 이 아니면 그때 파고들면 된다.

        ★ **이름의 주인은 `envelope.LLMStatus` 다.** 여기서 새 이름을 안 붙이고
          세기만 한다 — `end_codes` 가 `scheduler` 의 값을 그대로 세는 것과 같다.
        """
        total: Counter[str] = Counter(dict.fromkeys(LLM_STATUSES, 0))
        for day in self.days:
            for one in (*day.items, *day.sales_items):
                total.update(one.llm_statuses)
        return total

    @property
    def observation_coverage(self) -> Mapping[str, int]:
        """그 걷기에서 **부서가 관측 기준시점을 실었나** (2026-09-12). 두 칸뿐이다.

        ```text
        실었다   부서가 `AgentReply.observed_at` 을 채워 보냈다
        안쟀다   🔴 안 채워 보냈다 — 그 사실을 **언제부터 알 수 있었는지 모른다**
        ```

        🔴 **0 이어도 찍는다.** `llm_outcomes` 와 같은 규율이다 — 처음에는
          「실었다」가 0 이고, **그 숫자가 늘어나는 것이 이 일의 진도**다. 0 이라
          빼면 진도가 안 보이고, 재무·물류가 연결한 날에도 성적표가 아무 말을
          안 한다.

        🔴 **세 번째 칸(「미래를 봤다」)이 없다.** 그것은 `observed_at > as_of` 를
          막는 검사를 걸 때 생기는 칸이고, 아무도 안 채운 지금 걸면 전부 막힌다.
          이 판은 **칸을 여는 데까지**다.

        🔴 **「안 쟀다」를 「미래를 봤다」와 섞지 않는다.** 앞은 *"모른다"* 이고
          뒤는 *"틀렸다"* 다 — 다음에 할 일이 다르다. 앞은 부서에 연결을 요청하는
          일이고 뒤는 그 호출을 막는 일이다.

        ★ **마스터가 값을 지어내지 않는다.** 여기서 `as_of` 로 메우면 이 줄은
          첫날부터 「실었다」가 만 건이라고 말하고, 그 숫자는 한 건도 사실이 아니다.

        ⚠️ **못 돈 품목은 안 세어진다.** 계획이 없으면 `observed_ats` 가 비고,
          그 자리는 `status=FAILED` 가 이미 말한다 — 여기서 `None` 한 개로
          채우면 **안 돈 품목이 안 잰 품목으로** 세어진다.
        """
        실었다, 안쟀다 = _OBSERVED_AT_LABELS
        total: Counter[str] = Counter(dict.fromkeys(_OBSERVED_AT_LABELS, 0))
        for day in self.days:
            for one in (*day.items, *day.sales_items):
                for 관측 in one.observed_ats:
                    total[안쟀다 if 관측 is None else 실었다] += 1
        return total

    @property
    def procurement_statuses(self) -> Mapping[str, int]:
        """매입 판단 단계 분포 (2026-09-13). 🔴 **값을 접지 않고 그대로 센다.**

        ```text
        RAN            돌았다
        NOT_ATTEMPTED  거기까지 못 갔다 — 개장 실패 · 장부 관문   ← 사고다
        NO_ML_BATCH    배치가 원래 없는 날이라 안 돌렸다           ← 사고가 아니다
        ```

        ★★ **이 줄이 없으면 배치 없는 날이 매입 쪽에서 안 보인다.** 그 날은 품목이
          없어 `종료코드` 줄에서 빠진다 — 종전 `E4` 42건이 0 이 되는데 그 42건이
          어디 갔는지를 이 줄이 말한다.

        ★ `sales_statuses` 와 같은 모양이다 — `scheduler` 가 낸 값을 세기만 한다.
        """
        return Counter(one.procurement_status for one in self.days)

    @property
    def sales_statuses(self) -> Mapping[str, int]:
        """판매 판단 단계 분포. 🔴 **세 값을 접지 않고 그대로 센다.**

        ```text
        RAN            돌았다
        FAILED         해 보고 터졌다 — 돈 품목이 하나도 없다   ← "못 했다"
        NOT_ATTEMPTED  거기까지 못 갔다                        ← "안 했다"
        NO_ML_BATCH    배치가 원래 없는 날이라 안 돌렸다         ← 2026-09-13
        ```

        ⚠️ **값이 있는데 성적표가 안 읽으면 없는 것과 같다** — `outbound_status` 를
          성적표에 태울 때(`#446`) 배운 그것이다.
        """
        return Counter(one.sales_status for one in self.days)

    @property
    def receivable_statuses(self) -> Mapping[str, int]:
        """채권 발행 단계 분포. 🔴 **다섯 값을 접지 않고 그대로 센다.**

        ```text
        ISSUED         대상이 있었고 채권이 서 있다
        NOTHING_DUE    그날 확정된 판매가 없었다     ← "없다"
        BLOCKED        대상은 있는데 못 세웠다        ← "못 했다"
        NOT_OPENED     하루가 안 열려서 안 했다       ← "안 했다"
        FAILED         세워 보다 터졌다
        ```

        ⚠️ **값이 있는데 성적표가 안 읽으면 없는 것과 같다.** `outbound_status` 를
          성적표에 태울 때(`#446`) 배운 것이 그것이다 — 단계는 도는데 화면이 그
          단계를 말하지 않으면 아무도 그 단계가 막힌 것을 모른다.
        """
        return Counter(one.receivable_status for one in self.days)

    @property
    def outbound_statuses(self) -> Mapping[str, int]:
        """출고 단계 분포. 🔴 **네 값을 접지 않고 그대로 센다.**

        ```text
        RAN            나갔다
        NOTHING_DUE    나갈 것이 없었다        ← "없다"
        FAILED         나가려다 못 나갔다      ← "못 했다"
        NOT_ATTEMPTED  거기까지 못 갔다        ← "안 했다"
        ```

        ⚠️ **`RAN` 만 세고 나머지를 묶으면 안 된다.** 손익 곡선이 평평할 때 그것이
          *"나갈 것이 없어서"* 인지 *"나가려다 못 나가서"* 인지를 성적표가 답해야
          하고, 묶는 순간 그 답이 사라진다.

        ★ `end_codes` 와 같은 모양이다 — `scheduler` 가 낸 값을 세기만 한다.
        """
        return Counter(one.outbound_status for one in self.days)

    @property
    def closing_statuses(self) -> Mapping[str, int]:
        """재무 일마감 단계 분포 (2026-09-16). 🔴 **여섯 값을 접지 않는다 · 0 도 든다.**

        ```text
        CLOSED         그날을 닫았다
        NOTHING_DUE    닫을 움직임이 없었다 · 또는 미등록이다
        BLOCKED        장부가 안 서서 못 닫았다
        NOT_OPENED     하루가 안 열려서 안 물었다
        FAILED         닫아 보다 터졌다        ← 🔴 사고다 (`_incident_reason`)
        NOT_ATTEMPTED  단계를 안 탔다
        ```

        ★★ **이 줄이 없어서 3월 초부터 마감이 멈춘 것을 아무도 못 봤다** (실측 2026-09-16).
          `SIM-CHAIN-CHECK-0916` 요약은 「사고 0건 · 현금항등식 🟢」 이었는데
          `daily_closings` 는 03-06 뒤로 0행이었다. 값은 `DayRunOutcome.closing_status` 에
          안 접힌 채 있었고 **재는 줄만 없었다** (`maintenance_statuses` 때와 같은 모양).

        🔴 **`FAILED` 0 을 빼지 않는다.** 키가 안 보이면 *"없었다"* 와 *"안 셌다"* 가 같아진다.
        """
        total: Counter[str] = Counter(dict.fromkeys(_CLOSING_STATUSES, 0))
        for day in self.days:
            total[day.closing_status] += 1
        return total

    @property
    def last_closed_on(self) -> date | None:
        """마지막으로 `CLOSED` 가 선 날. 한 번도 안 섰으면 `None`.

        ★ **분포만으로는 언제 멈췄는지를 못 읽는다.** `CLOSED 45` 는 1월부터 45일인지
          3월까지 45일인지를 말하지 않는다.
        """
        닫은날 = [day.as_of for day in self.days if day.closing_status == "CLOSED"]
        return max(닫은날) if 닫은날 else None

    @property
    def first_closing_failure(self) -> tuple[date, str] | None:
        """처음으로 마감이 `FAILED` 인 날과 그 사유. 없으면 `None`.

        🔴 **사유를 짓지 않는다.** 주인은 `ClosingOut.reason` 이고, 낸 값이 없으면
          (마감이 예외로 터졌으면) `모름` 이다 — 그날 사고 줄이 note 전체를 나른다.
        """
        for day in self.days:
            if day.closing_status == "FAILED":
                사유 = day.closing.reason if day.closing is not None else None
                return day.as_of, _or_unknown(사유 or None)
        return None

    @property
    def approval_statuses(self) -> Mapping[str, int]:
        """자동 승인 **단계** 분포 (2026-09-11). 🔴 **네 값을 접지 않고 그대로 센다.**

        ```text
        NOT_ATTEMPTED  안 켰다 — --auto-approve 를 안 줬다        ← "안 했다"
        RAN            승인 문까지 돌았다
        NO_RULE        켰는데 그 실행이 규칙을 안 들었다           ← "못 했다"
        FAILED         돌리다 터졌다
        ```

        ⚠️ **매입과 판매를 한 통에 센다.** 어느 사이클이 안 섰는지는 `days` 의 두
          칸이 그대로 들고 있고, 여기서 묻는 것은 *"며칠에 승인 단계가 돌았나"* 다.

        ⚠️ **값이 있는데 성적표가 안 읽으면 없는 것과 같다** — `outbound_status` 를
          성적표에 태울 때(`#446`) 배운 그것이다.
        """
        return Counter(
            status
            for day in self.days
            for status in (day.procurement_approval_status, day.sales_approval_status)
        )

    @property
    def transition_outcomes(self) -> Mapping[str, int]:
        """미적용 전이 재시도의 **승인별** 결과 분포 (2026-09-11). 🔴 **넷을 접지 않는다.**

        ```text
        APPLIED         원장에 닿았다
        NOT_APPLIED     아직 쓸 것이 없다      ← "없다"
        FAILED          쓰려다 터졌다          ← "못 했다"
        NOT_BUILDABLE   약정을 못 만들었다     ← 전이 앞에서 끝났다
        ```

        ★★ **이 줄이 없어서 「승인 15건 RECORDED」 를 보고 원장에 닿은 줄 알았다**
          (실측 2026-09-11). `approval_outcomes` 는 *"승인을 적었나"* 까지만 답한다 —
          그 승인이 **장부에 닿았나**는 축이 하나 더 뒤다.

        🔴 **`approval_outcomes` 와 한 칸에 담지 않는다.** 어휘가 다르고 축이 다르다 —
          담으면 *"적었다"* 와 *"닿았다"* 가 한 표에 섞여 어느 쪽 수가 는 것인지를
          못 읽는다 (`end_codes` 와 `sales_end_codes` 를 가른 것과 같은 이유).

        ★ **이름의 주인은 `pending_transition.py` 다.** 여기서 새 이름을 안 붙이고
          세기만 한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            if day.pending_transition is not None:
                total.update(day.pending_transition.outcomes)
        return total

    @property
    def ledger_blocks(self) -> LedgerBlocks:
        """승인은 났는데 **매입 원장에 한 행도 안 남은** 것들 (2026-09-16).

        ```text
        등급 둘        purchase_items 는 품목당 한 줄인데 등급이 둘이다
        회차금액 없음   회차가 둘 이상인데 어느 회차 금액이 비었다
        지급일 없음     purchases.payment_due_date 를 만들 값이 없다
        도착분 없음     목표 상태일에 앞으로 올 도착분이 하나도 없다
        ```

        ★★ **이 줄이 없어서 6건 726kg 255,287원(수량 0.76% · 금액 0.46%)이 조용히
          사라졌다** (확인 걷기 CHECK-0916 · 매입 파트가 A/B 실험 중 발견). 승인은
          `RECORDED` 로 찍히고 전이는 `NOT_APPLIED` 한 값에 묻혀, **요약만 봐서는
          원장이 비었다는 사실이 아무 데도 안 보였다.**

        🔴 **막히는 경로가 둘이라 둘 다 본다.**

        ```text
        당일 전이      backfill 이 승인하며 부른 apply_approval  (BackfilledRun)
        다음 날 재시도  retry_pending_transitions                 (RetriedTransition)
        ```

        한쪽만 보면 수가 **조용히 작아진다** — 오류가 안 나고 그냥 적게 나온다.

        🔴 **크기와 소음을 따로 센다** (매입 파트 회신 2026-09-16). `retry_pending_
           transitions` 가 같은 약정을 **날마다** 다시 세우고 같은 사유로 또 막힌다 —
           날짜별로 세면 한 건이 며칠치로 부푼다. 실측에서 `NOT_APPLIED 12` 였는데
           복수 등급 승인안은 **실제로 1건**이었다. 12 와 1 이 이만큼 벌어진다.

        🔴 **사유 문장이 아니라 갈래로 센다.** 문장에는 등급 이름과 회차 번호가
           박혀 있어 (`등급이 2개인데 매입 줄이 하나다 (특/상)`) 약정마다 다른 키가
           되고, 그러면 세는 뜻이 없어진다. 이름의 주인은 `ledger` 다.

        🔴 **0 인 갈래도 든다.** 키가 빠지면 *"없었다"* 와 *"안 셌다"* 가 같아진다
           (`inspection_statuses` · `observation_coverage` 와 같은 규율).
        """
        갈래별: dict[str, set[str]] = {갈래: set() for 갈래 in LEDGER_BLOCK_KINDS}
        모두: set[str] = set()
        재시도 = 0

        def 담는다(키: str, 갈래: str) -> None:
            갈래별.setdefault(갈래, set()).add(키)
            모두.add(키)

        for day in self.days:
            # ① 당일 전이 — 승인 문이 그날 바로 부른 자리. **첫 시도라 소음이 아니다.**
            for approval in (day.procurement_approval, day.sales_approval):
                if approval is None:
                    continue
                for one in approval.runs:
                    if one.transition_block_kind:
                        담는다(
                            _approval_key(one.request_id, one.decision_seq, one.run_id),
                            one.transition_block_kind,
                        )
            # ② 다음 날 재시도 — 원장에 안 닿은 것을 다시 세우는 자리. **여기가 소음이다.**
            if day.pending_transition is not None:
                for retried in day.pending_transition.retried:
                    if retried.block_kind:
                        재시도 += 1
                        담는다(
                            purchase_id_prefix_for(retried.request_id, retried.decision_seq),
                            retried.block_kind,
                        )

        # 🔴 **영영 안 될 것도 고유로 센다.** 이 수가 곧 발표에서 말할 크기다 —
        #    날짜별로 세면 재시도가 도는 날수만큼 부풀고, 그러면 *"복수 등급 1건"* 이
        #    *"12건"* 으로 나간다.
        영영: set[str] = set()
        for 갈래 in PERMANENT_BLOCK_KINDS:
            영영 |= 갈래별.get(갈래, set())
        return LedgerBlocks(
            unique_by_kind={갈래: len(키들) for 갈래, 키들 in 갈래별.items()},
            unique=len(모두),
            retries=재시도,
            permanent=len(영영),
        )

    @property
    def maintenance_statuses(self) -> Mapping[str, int]:
        """물류 유지보수 **단계** 분포 (2026-09-11). `approval_statuses` 와 같은 자리다.

        🔴 **Lot 축 한 줄로는 「안 켰다」와 「켰는데 0 Lot」이 안 갈린다.** 둘 다 `{}`
           로 나오고, 그러면 성적표를 보는 사람이 *"폐기할 것이 없었구나"* 로 읽는다 —
           실제로는 안 켠 것일 수 있다.

        ★★ 구현이 이 구멍을 보고했고 값은 이미 `DayRunOutcome.maintenance_status` 에
          안 접힌 채 있었다. **재는 줄만 없었다.** 승인이 `승인`·`승인어휘` 두 줄인
          것과 같은 이유로 여기도 둘이다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            total[day.maintenance_status] += 1
        return total

    @property
    def inspection_statuses(self) -> Mapping[str, Mapping[str, int]]:
        """물류 점검 **칸별** 상태 분포 (2026-09-14). 🔴 **넷을 접지 않는다 · 0 도 든다.**

        ```text
        NOT_ATTEMPTED  칸을 안 탔다 — 개장 실패 · 안 도는 날     ← "안 했다"
        RAN            문제를 열었거나 갱신했거나 닫았다
        NOTHING_DUE    확인했고 손댈 것이 없었다 — 🟢 정상이다
        FAILED         보려다 터졌다 — 🔴 **그래도 하루는 계속 간다**  ← "못 했다"
        ```

        ★★ **이 줄이 없어서 걷기 끝에 「점검이 실패한 날이 있었나」 를 증명할 수
          없었다.** 값은 `DayRunOutcome.inspection_*_status` 에 안 접힌 채 있었고
          **재는 줄만 없었다** (`maintenance_statuses` 때와 같은 모양).

        🔴 **`FAILED` 0 을 빼지 않는다.** 여기서 묻는 것은 *"실패가 없었다"* 이고, 키가
          안 보이면 *"없었다"* 와 *"안 셌다"* 가 같아진다 (`llm_outcomes` 와 같은 규율).

        🔴 **두 칸을 한 통에 담지 않는다.** 닫는 자리는 `AFTER_OUTBOUND` 하나라 둘을
          합치면 *"입고 뒤는 늘 돌고 출고 뒤만 터진다"* 가 안 읽힌다.

        ⚠️ **사고가 아니다.** `_incident_reason` 은 이 값을 안 본다 — 점검이 터진 날도
          하루는 끝까지 갔고, 그것은 요약에 보이는 사실이다.

        ★ **이름의 주인은 `inspection.py` 다** (`INSPECTION_STATUSES` · 칸 이름).
          `NOT_ATTEMPTED` 는 `DayRunOutcome` 의 기본값을 그대로 읽는다.
        """
        어휘 = (*INSPECTION_STATUSES, DayRunOutcome.inspection_inbound_status)
        칸별: dict[str, Counter[str]] = {
            AFTER_INBOUND: Counter(dict.fromkeys(어휘, 0)),
            AFTER_OUTBOUND: Counter(dict.fromkeys(어휘, 0)),
        }
        for day in self.days:
            칸별[AFTER_INBOUND][day.inspection_inbound_status] += 1
            칸별[AFTER_OUTBOUND][day.inspection_outbound_status] += 1
        return 칸별

    @property
    def inspection_counts(self) -> Mapping[str, int]:
        """물류 점검이 **연 · 갱신한 · 닫은** 문제 수 합계 (2026-09-14). 두 칸을 더한다.

        ★ **여기서 다시 세지 않는다.** 주인은 `DetectOut.counts` 이고 이쪽은 날마다
          `InspectionOut.counts` 를 더하기만 한다.

        ⚠️ **결과가 한 번도 없었으면 빈 칸이다.** 전부 `FAILED` 거나 칸을 안 탄 걷기에서
          `opened 0` 을 채우면 *"봤는데 없었다"* 로 읽힌다 — 실제로는 **못 봤다.**
          그 사실은 `inspection_statuses` 가 말한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            for out in (day.inspection_inbound, day.inspection_outbound):
                if out is not None:
                    total.update(out.counts)
        return total

    @property
    def maintenance_outcomes(self) -> Mapping[str, int]:
        """물류 유지보수의 **Lot 별** 결과 분포 (2026-09-11). 🔴 **넷을 접지 않는다.**

        ```text
        DISPOSED                 전량 폐기했다 · 자리도 돌려줬다
        SKIPPED_HELD_ALLOCATION  살아있는 할당이 있어 **손대지 않았다** ← 사람 몫이다
        PALLETS_EMPTIED          잔량이 이미 0 이라 자리만 돌려줬다
        FAILED                   도메인이 거절했다                     ← "못 했다"
        ```

        ★★ **`SKIPPED_HELD_ALLOCATION` 이 안 보이면 자동화가 왜 덜 했는지를 성적표가
          못 답한다.** 물류가 *"자동 부분 폐기를 하지 않는다 — 남은 판단은 사람
          몫이다"* 로 일부러 남긴 줄이고, 창고가 안 비는 날 **거기부터 봐야** 한다.

        ★ **이름의 주인은 `logistics/auto_maintenance.py` 다.** 여기서 새 이름을
          안 붙이고 세기만 한다 (`transition_outcomes` 와 같은 모양).

        🔴 **`approval_outcomes` 와 한 칸에 담지 않는다.** 어휘가 다르고 축이 다르다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            if day.maintenance is not None:
                total.update(day.maintenance.outcomes)
        return total

    @property
    def approval_outcomes(self) -> Mapping[str, int]:
        """승인 **행별** 어휘 분포 (2026-09-11). 🔴 **여덟을 접지 않는다.**

        ```text
        RECORDED · ALREADY_DECIDED · NOT_APPROVABLE · NO_RULE_FOR_CYCLE
        LABEL_NOT_OFFERED · AMBIGUOUS_TYPE · BLOCKED_BY_BOUNDARY · FAILED
        ```

        ★ **이름의 주인은 `backfill.py` 다.** 여기서 새 이름을 안 붙이고 세기만
          한다 — `end_codes` 가 `scheduler` 의 값을 그대로 세는 것과 같은 모양이다.

        🔴 **`approval_statuses` 와 축이 다르다.** 저쪽은 하루의 단계이고 이쪽은
          실행 이력 한 행이다 — 묶으면 *"승인이 왜 0건인가"* 를 성적표가 못 답한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            for approval in (day.procurement_approval, day.sales_approval):
                if approval is not None:
                    total.update(approval.outcomes)
        return total

    @property
    def confirmation_outcomes(self) -> Mapping[str, int]:
        """판매 확정 어휘 분포 (2026-09-11). 🔴 **셋을 접지 않는다.**

        ```text
        CONFIRMED  sales · sale_items 가 섰다
        BLOCKED    확정할 수 없었다 — 아무것도 안 썼다
        FAILED     쓰려다 실패했다 — 롤백했다
        ```

        ★★ **이 줄이 없어서 「승인 7건 RECORDED · 재검증 PASSED 7건」 을 보고 판매가
          선 줄 알았다** (실측 2026-09-11). `sales` 는 0행이었고, 확정이 매번
          `BLOCKED` 였는데 그 사실이 성적표 어디에도 안 남아 사람이 손으로 재현해서야
          찾았다.

        🔴 **`approval_outcomes` 와 한 칸에 담지 않는다.** 어휘가 다르고 축이 다르다 —
          `RECORDED` 는 *"승인이 적혔다"* 일 뿐 *"판매가 섰다"* 가 아니다
          (`transition_outcomes` 를 가른 것과 같은 이유).

        ★ **이름의 주인은 `backfill.py` 다.** 여기서 새 이름을 안 붙이고 세기만 한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            for approval in (day.procurement_approval, day.sales_approval):
                if approval is not None:
                    total.update(approval.confirmation_outcomes)
        return total

    @property
    def reservation_outcomes(self) -> Mapping[str, int]:
        """확정분 예약 어휘 분포 (2026-09-12). 🔴 **둘을 접지 않는다.**

        ```text
        RESERVED  요구량만큼 잡았다
        SHORT     모자랐다 — 확정은 CONFIRMED 인데 재고는 그만큼 없었다
        ```

        ★★ **이 줄이 없어서 「확정 34건」 을 보고 재고가 잡힌 줄 알았다**
          (`SIM-CHAIN-V4` 실측). 확정 뒤 예약이 안 걸려 물류가 다음 날 같은 재고를
          다시 가용으로 보고했고, 14건 15,474kg 이 미출고로 남았다.

        ★ **이름의 주인은 `sales_approval.SaleConfirmationOut` 이다.** 여기서 새
          이름을 안 붙이고 세기만 한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            for approval in (day.procurement_approval, day.sales_approval):
                if approval is not None:
                    total.update(approval.reservation_outcomes)
        return total

    @property
    def outbound_failure_lines(self) -> tuple[str, ...]:
        """출고가 **터진** 판매 품목마다 한 줄 (2026-09-15 · 물류 문서 24 §5-㉣).

        ```text
        as_of · sale_id · sale_item_id · reservation_id · item_id ·
        required · reserved · 후보 n · 후보합 · error_type · message · release
        ```

        🔴 **`FAILED` 만.** `SHORT` 는 사업 결과이지 터진 것이 아니다 (`OutboundOut.failed_items`
          와 같은 선).

        ★ **값을 새로 만들지 않는다.** 칸의 주인은 `SaleItemOutcome` 이고 여기는 옮겨
          적는다. 모르는 칸(`None`)은 `모름` 으로 적는다 — 0 으로 접으면 «후보 0건» 과
          «후보를 못 읽었다» 가 같아진다. `message` 는 사유 문장 그대로다.
        """
        out: list[str] = []
        for day in self.days:
            출고 = day.outbound
            if 출고 is None:
                continue
            for one in 출고.items:
                if one.status != "FAILED":
                    continue
                칸 = [
                    출고.as_of.isoformat(),
                    one.sale_id,
                    one.sale_item_id,
                    one.reservation_id,
                    _or_unknown(one.item_id),
                    f"required {one.required_qty_kg}",
                    f"reserved {_or_unknown(one.reserved_qty_kg)}",
                    f"후보 {_or_unknown(one.candidate_lot_count)}",
                    f"후보합 {_or_unknown(one.candidate_available_kg)}",
                    _or_unknown(one.error_type),
                    one.reason,
                    f"release {_or_unknown(one.release_outcome)}",
                ]
                out.append(" · ".join(칸))
        return tuple(out)

    @property
    def confirmation_reasons(self) -> tuple[str, ...]:
        """확정이 못 선 이유들. **`CONFIRMED` 가 아닌 것만.**

        ⚠️ **코드만 나르면 오늘 밤이 반복된다.** `{BLOCKED: 7}` 만 보고는 무엇이
          막았는지를 못 읽는다 — 그날의 이유는 `ValidationError:
          reported_sales_amount_krw` 였고 그것은 문장을 봐야 보인다.

        ★ **같은 문장을 한 번만 싣는다.** 이레 내내 같은 이유면 줄이 일곱이 아니라
          하나여야 읽힌다. **본 순서는 지킨다** — 무엇이 먼저 막았는지가 순서다.
        """
        out: list[str] = []
        for day in self.days:
            for approval in (day.procurement_approval, day.sales_approval):
                if approval is None:
                    continue
                for one in approval.runs:
                    사유 = one.confirmation_reason
                    if (
                        one.confirmation_status not in (None, "CONFIRMED")
                        and 사유
                        and 사유 not in out
                    ):
                        out.append(사유)
        return tuple(out)

    @property
    def label_outcomes(self) -> Mapping[str, int]:
        """**어느 라벨이 실제로 섰나** (2026-09-11). 🔴 **접지 않는다.**

        ★★ **규칙에 순서가 생긴 순간 이 줄이 필요해졌다** (`backfill.FIRST_OFFERED`).
          규칙 파일에 순서가 적혀 있어도 **그날 무엇이 섰는지**는 그날 제시된 안이
          정한다 — 이 줄이 없으면 **곡선이 한 규칙의 것이 아니게 되고**, 사람이
          날마다 결정 행을 되짚어야 한다.

        🔴 **`approval_outcomes` 와 한 칸에 담지 않는다.** 어휘가 다르고 축이 다르다 —
          저쪽은 *"승인을 적었나"* 이고 이쪽은 *"무엇을 골랐나"* 다
          (`confirmation_outcomes` · `transition_outcomes` 와 같은 규율).

        ★ **이름의 주인은 매입이다.** 여기서 새 이름을 안 붙이고 세기만 한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            for approval in (day.procurement_approval, day.sales_approval):
                if approval is not None:
                    total.update(approval.label_outcomes)
        return total

    @property
    def cash(self) -> Mapping[str, Decimal | None] | None:
        """그 구간의 현금 축 (2026-09-12). **마감행이 0행이면 `None`.**

        ```text
        네 유출·유입    그 구간 합
        순현금          그 구간 합
        기말잔액        🔴 합이 아니라 **마지막 날의 잔액**이다
        ```

        ★★ **걷기가 현금 축을 아예 안 보고 있었다.** 칸은 `daily_closings` 에
          처음부터 있었고 아무도 안 봤다 — `llm_outcomes` 가 서기 전과 같은 모양이다.

        🔴 **0 인 칸을 빼지 않는다.** 네 유출이 전부 0 인 채로 V4~V6 세 판이
          「성립」을 통과했다. 그 0 이 안 보이면 그 성립이 **무엇을 통과시킨
          것인지**를 성적표가 못 답한다.

        ★ **이름의 주인은 `ledger_repository` 다.** 여기서 새 이름을 안 붙이고
          나르기만 한다 — `end_codes` 가 `scheduler` 의 값을 그대로 세는 것과 같다.

        🔴 **`_NULLABLE_CASH_COLUMNS` 의 칸은 하루라도 `None` 이면 합이 `None` 이다**
          (2026-09-16). 그 `None` 은 「0원이었다」가 아니라 **「그날 이 축을 안 셌다」**다
          (재무 `schemas.py` 의 뜻).

        🔴 **기록된 날만 더해서 합으로 내지 않는다.** 그러면 구간 합인 척하는
          **부분합**이 찍히고, 읽는 사람은 그 수가 며칠치인지 알 길이 없다 —
          `SIM-CHAIN-CHECK-0916` 에서 매입유출이 조용히 작아졌던 그 모양이다.
          모르는 것은 **모른다고 적는다.**
        """
        if not self.closings:
            return None
        total: dict[str, Decimal | None] = {}
        for _, column in _CASH_FLOWS:
            if column not in _NULLABLE_CASH_COLUMNS:
                total[column] = sum((_won(row, column) for row in self.closings), _ZERO)
                continue
            값들 = [_won_or_none(row, column) for row in self.closings]
            total[column] = None if any(one is None for one in 값들) else sum(값들, _ZERO)
        # ★ **기말잔액만 합이 아니다.** 잔액은 그날의 상태이지 그날의 움직임이
        #   아니다 — 더하면 179일치 잔액을 합한 뜻 없는 수가 나온다.
        total[BASE_CASH_BALANCE] = _won(self.closings[-1], BASE_CASH_BALANCE)
        return total

    @property
    def cash_identity(self) -> CashIdentity | None:
        """현금 항등식 (2026-09-12). **마감행이 0행이면 `None`.**

        🔴 **대출 포함 곡선(`loan_cash_balance_krw`)을 여기 안 넣는다.** 차입과
          상환이 들어가 축이 다르다 — 대출이 실행된 날은 **잔액이 순현금과 달라야
          맞다.** 지금 네 판 다 대출이 0 이라 두 곡선이 안 갈리지만, **안 갈린다고
          한 축으로 접으면** 차입이 한 번 서는 날 항등식이 조용히 거짓말을 한다
          (`end_codes` 와 `sales_end_codes` 를 가른 것과 같은 규율).

        🔴 **깨져도 `incidents` 에 안 넣는다.** 재무 지급 전이가 서기 전에는 **매일
          깨지고**, 사고로 세면 `max_consecutive_failures` 에 걸려 걷기가 못 끝난다 —
          그러면 정본 판을 못 돌린다. 🟢 세고 찍고 판정은 낸다 · 🔴 걷기를 멈추지
          않고 `사고` 줄 숫자를 안 건드린다. `사고` 는 자기 축을 그대로 지키고
          현금항등식은 **자기 줄**을 갖는다.

        ★ **여기는 `_won` 을 그대로 쓴다** (2026-09-16). 이 항등식이 읽는 칸은
          `BASE_CASH_BALANCE` 와 `NET_CASH` 둘뿐이고 **둘 다 `NOT NULL` 이다** —
          `_NULLABLE_CASH_COLUMNS` 에 없다. 그래서 운영비 칸이 `None` 이 되어도
          이 줄은 종전과 같은 값을 낸다. **다음 사람이 같은 걱정을 다시 하지 않게
          여기 적어 둔다.**
        """
        rows = self.closings
        if not rows:
            return None
        opening = _won(rows[0], BASE_CASH_BALANCE) - _won(rows[0], NET_CASH)
        return CashIdentity(
            opening_balance_krw=opening,
            balance_delta_krw=_won(rows[-1], BASE_CASH_BALANCE) - opening,
            net_cash_krw=sum((_won(row, NET_CASH) for row in rows), _ZERO),
            # ★ **첫날은 안 센다** — 앞 잔액이 없다. 세면 기초를 아는 판마다
            #   하루가 늘 어긋난 것으로 나온다.
            mismatched_days=sum(
                1
                for 앞, 뒤 in pairwise(rows)
                if abs(
                    (_won(뒤, BASE_CASH_BALANCE) - _won(앞, BASE_CASH_BALANCE)) - _won(뒤, NET_CASH)
                )
                >= _ONE_WON
            ),
        )


#: 현금 줄이 찍는 칸. **왼쪽은 사람이 읽는 이름 · 오른쪽은 재무의 칸이다.**
#:
#: 🔴 **여섯이 전부 합이고 기말잔액만 여기 없다** — 그쪽은 마지막 날의 값이라
#:   같은 자리에 두면 합으로 읽힌다.
#:
#: 🔴 **운영비 칸이 빠져 있으면 찍힌 유출의 합이 순현금과 안 맞는다.** 재무가 순현금에서
#:   이미 뺀 값이라 순현금은 맞는데, 그 차이를 설명하는 칸이 표에 없어서 읽는 사람이
#:   «어디서 샜지» 를 되짚을 수가 없다.
_CASH_FLOWS = (
    ("매입유출", PURCHASE_CASH_OUT),
    ("물류유출", LOGISTICS_CASH_OUT),
    ("인건이자", PAYROLL_INTEREST_CASH_OUT),
    ("운영비유출", OPERATING_EXPENSE_CASH_OUT),
    ("수금", COLLECTION_CASH_IN),
    ("순현금", NET_CASH),
)

#: 🔴 **값이 `None` 으로 올 수 있는 현금 칸** (2026-09-16). 나머지 칸은 `NOT NULL` 이다.
#:
#: ★ **왜 이 칸만 다른가.** 운영비 유출은 **나중에 생긴 축**이다. 이 칸이 서기 전에
#:   돈 실행들(SIM-CHAIN-V2~V13 · WALK-* · PREFINAL)이 DB 에 그대로 남아 있고,
#:   **그 실행들은 이 축을 한 번도 안 셌다.** 그래서 그쪽의 빈 값은 「0원이 나갔다」가
#:   아니라 **「안 셌다」**다 — 재무가 `finance/schemas.py` 에
#:   `operating_expense_cash_out_krw: Decimal | None` 로, `api/finance/query.py` 에
#:   `"기록 없음" if ... is None` 으로 적어 둔 그 뜻이다.
#:
#: ⚠️ **지금 이 갈래는 실제로 안 탄다.** `haetdeul.daily_closings` 의 이 칸은 아직
#:   `NOT NULL DEFAULT 0` 이라 DB 가 `None` 을 못 준다 (실측 2026-09-16).
#:   **그런데도 미리 세운다** — 칸의 주인은 재무이고, 재무가 코드 뜻대로 칸을
#:   바로잡는 날 이쪽이 준비돼 있지 않으면 **그날 걷기 요약이
#:   `decimal.InvalidOperation` 으로 통째로 죽는다.** 179일을 다 걷고 마지막 줄에서
#:   죽으면 성적을 통째로 잃는다 — 우리는 그 자리를 이미 한 번 밟았다
#:   (`_use_utf8_output`). 🔴 **여기는 「DB 가 언제 바뀌어도 안 죽는다」를 세우는 자리다.**
#:
#: 🔴 **여기에 칸을 늘리는 것은 «그 칸의 `None` 을 0 으로 안 읽겠다» 는 선언이다.**
#:   NOT NULL 로 남을 칸을 넣으면 안 된다 — 넣는 순간 「안 셌다」가 없는 자리에 생긴다.
_NULLABLE_CASH_COLUMNS = frozenset({OPERATING_EXPENSE_CASH_OUT})


def _won(row: Mapping[str, Any], column: str) -> Decimal:
    """마감행 한 칸을 원으로. **없는 칸은 터진다 — 0 으로 안 메운다.**

    ⚠️ `numeric` 은 `Decimal` 로 온다. `float` 로 낮추면 179일을 더하는 동안
      원 단위가 조용히 어긋나고, 그 어긋남이 **항등식의 판정**이 된다.

    🔴 **`NOT NULL` 칸 전용이다.** 값이 `None` 이면 `Decimal("None")` 을 만들려다
      `InvalidOperation` 으로 터진다 — 그래야 맞다. `None` 이 올 수 있는 칸은
      `_won_or_none` 을 쓴다 (`_NULLABLE_CASH_COLUMNS`).
    """
    value = row[column]
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _won_or_none(row: Mapping[str, Any], column: str) -> Decimal | None:
    """마감행 한 칸을 원으로. **값이 `None` 이면 `None` 이다 — 0 으로 안 메운다.**

    ```text
    칸이 없다        터진다        ← `_won` 과 같다. 표가 바뀐 것을 조용히 못 넘긴다
    값이 None 이다   None          ← 「안 셌다」. 0 이 아니다
    ```

    🔴 **`row.get(column, 0)` 으로 바꾸지 않는다.** 그러면 칸이 사라진 날과
      값이 0 인 날이 화면에서 같아진다 — `_won` 이 지키던 규율 그대로다.
    """
    value = row[column]
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def walk(
    *,
    sim_run_id: str,
    start: date,
    end: date,
    now: datetime,
    calendar: Callable[[], MarketCalendar] = get_market_calendar,
    ml_batch: Callable[[], MlBatchCalendar] = get_ml_batch_calendar,
    readiness: Callable[[date], DayForecastReadiness] = lambda as_of: day_forecast_readiness(
        as_of=as_of
    ),
    run_day_fn: Callable[..., DayRunOutcome] = run_scheduled_day,
    policy_version: str = DAILY_POLICY_VERSION,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ticks: Callable[[], float] = time.monotonic,
    auto_approve: bool = False,
    rules_of: Callable[[str], BackfillRules] = read_run_rules,
    terms_of: Callable[[str], SalesTermsRule | None] = read_run_sales_terms,
    auto_maintain: bool = False,
    closings_of: Callable[..., Sequence[Mapping[str, Any]]] = read_walk_closings,
    walked_now: str | None = None,
    record_now: Callable[..., WalkedNowStamp] = record_walked_now,
) -> WalkResult:
    """`start` 부터 `end` 까지 하루씩 걷는다. **개장일마다 하루 실행을 부른다.**

    🔴 **`run_scheduled_day` 를 부른다.** `run_procurement` 을 직접 부르지 않는다 —
      그러면 개장 · 입고 · 채권 · 수금 · 장부 관문을 통째로 건너뛰고, 그 위에서 나온
      *"사고 0건"* 은 아무것도 증명하지 않는다.

    :param sim_run_id: 어느 실행에 이 걸음을 쌓는가. 🔴 **기본값이 없다 — 안 주면
        터진다.** 179일을 어느 실행에 쌓는지가 곧 그 곡선의 정체이고, 기본값을 두면
        **말 안 하고 번인에 쌓을 수 있다.** 그러면 사람이 심어 둔 30일 위에 179일이
        겹쳐 앉고, 어느 행이 번인이고 어느 행이 걷기인지 되가를 방법이 없다.

        ★ **여기서 실행을 만들지 않는다.** 행을 세우는 것은 `sim_run.create_sim_run`
          이고, 이 파일은 **받은 축을 나르기만** 한다 — 걷기가 실행을 만들면 같은
          범위를 두 번 걸을 때마다 실행이 하나씩 늘어난다.

    :param now: 걷는 동안 쓸 시각. 🔴 **인자다 — 이 파일은 시계를 안 읽는다.**
        날짜는 안 쓰고 **시각만** 떼어 걷는 날마다 붙인다 (모듈 docstring).
    :param calendar: 개장 축. `is_market_open` 하나만 부른다.
    :param ml_batch: 배치 축 (2026-09-13). `has_ml_batch` 하나만 부른다 — 하루마다
        `scheduler.plan_next_action` 에 넘긴다. 🔴 **여기서 판정하지 않는다.** 걷기가
        배치 없는 날을 제 손으로 건너뛰면 그 날 장부(입고 · 수금 · 출고 · 마감)가
        통째로 빠진다 — 무엇을 돌지는 하루 실행이 `scope` 로 안다.
    :param readiness: 그날 예측 게이트. `wake_up` 과 같은 모양으로 받는다.
    :param run_day_fn: 하루 실행. 🔴 **기본값이 `run_scheduled_day` 자체다** —
        `None` 을 안 받는다 (`clock.py` · `verifier.py` 와 같은 규율).
    :param ticks: 소요 시간을 재는 단조 시계. 🔴 **벽시계가 아니다** — 날짜도
        시간대도 안 만들고 *"얼마나 걸렸나"* 만 답한다. 검사가 고정값을 꽂는다.
    :param auto_approve: 🔴 **기본이 거짓이다. 안 주면 승인 함수가 이름조차 안
        불린다** (2026-09-11). 켜면 각 판단 **바로 뒤**에 승인이 선다.

        ★★ **설정에 규칙이 있다고 켜지지 않는다.** *"있으니까 한다"* 는 암묵
          스위치이고, 그러면 설정을 실험하려고 넣은 사람이 **승인까지 하게 된다.**
          켜는 것은 명시로만이고, 그 명시가 이 인자 하나다.
    :param rules_of: 그 실행이 정한 **승인** 규칙을 읽는 자리. 🔴 **`auto_approve` 가
        거짓이면 한 번도 안 불린다** — 안 켠 걷기가 승인 규칙을 물을 이유가 없다.
    :param terms_of: 그 실행이 정한 **판매 상업 조건**을 읽는 자리 (2026-09-11).

        🔴 **`rules_of` 와 한 인자로 묶지 않는다.** 축이 다르다 — 저쪽을 부르는
          것은 *"안 켜고 걸으면 막는다"* 는 관문이라 **안 켜면 안 부르는 것이
          잠겨 있다.** 이쪽은 관문이 아니라 요청에 실릴 값이고, 승인을 안 켜도
          조건은 실려야 한다. 하나로 묶으면 *"승인을 켜야 조건이 실린다"* 가 되고
          그 사실은 아무 데도 안 적혀 있다.

        ★ **실행당 한 번 부른다.** 규칙은 실행에 속하지 날에 속하지 않는다.
    :param auto_maintain: 🔴 **기본이 거짓이다. 안 주면 유지보수 함수가 이름조차
        안 불린다** (2026-09-11). 켜면 **개장 바로 뒤**에 그날 자리를 비운다.

        ★★ **승인보다 더 조심할 자리다.** 폐기는 되돌릴 경로가 없다 —
          `ADJUST_IN` 도 실사도 없다고 물류가 못박았다.

        ⚠️ **`auto_approve` 처럼 걷기 전에 막는 관문이 없다.** 확인할 규칙 파일이
          없기 때문이다 — 버릴 것이 없는 날은 사고가 아니라 `NOTHING_DUE` 다.
    :param closings_of: 그 구간의 마감행을 읽는 자리 (2026-09-12). 🔴 **걷기 한
        판에 한 번, 다 걷고 나서 부른다** — 날마다 읽으면 같은 표를 179번 다시
        읽고, 그 중 하루만 다른 답이 오는 날이 오면 왜인지를 못 읽는다.

        🔴 **여기서 숫자를 만들지 않는다.** 읽은 값을 `WalkResult` 가 그대로 든다.

        ⚠️ **못 읽어도 걷기를 안 터뜨린다.** 179일을 다 걷고 마지막 조회에서 죽으면
          **성적을 통째로 잃는다** (`_use_utf8_output` 이 막은 그 모양). 못 읽은
          사실은 `closings_reason` 이 든다 — *"없다"* 로 접지 않는다.
    :param walked_now: 사람이 준 `--now` **문자열 그대로** (2026-09-13). 주면
        `record_now` 가 그 실행의 `config_json.provenance.walked_now` 에 남긴다.

        🔴 **`now` 와 같은 시각이어야 한다.** 적은 값과 실제로 건 값이 갈리면 그
          칸은 없는 것보다 나쁘다 — 갈리면 걷기 전에 막는다.

        ⚠️ **안 주면 기록을 안 부른다.** 적을 문자열이 없다. 그 사실은 요약이
          「안 받았다」로 말한다 — 진입점(`main`)은 늘 준다.
    :param record_now: 기준 시각을 **재고 · 없으면 적는** 자리. 🔴 **걷기 첫 날
        전에 한 번 부른다** — 다 걷고 나서 부르면 연속 사고 상한에 걸려 멈춘 판에
        무엇으로 걸었는지가 안 남는다. 이미 **다른** 값이 있으면 그 자리가
        `WalkedNowConflict` 로 막고, 걷기는 한 날도 안 걷는다.
    :raises ValueError: 범위가 거꾸로거나 `now` 에 시간대가 없거나 `sim_run_id` 가
        빈 문자열일 때. **막고 사유를 낸다** — 조용히 바로잡지 않는다.

        🔴 **`auto_approve` 인데 그 실행이 규칙을 안 들었을 때도 막는다.** 조용히
          걸으면 179일 뒤에 승인 0건이 나오고, 사람은 그것을 *"승인이 돌았는데
          해당이 없었구나"* 로 읽는다 — 그때는 하루도 되돌릴 수 없다.
    """
    if not sim_run_id.strip():
        # 🔴 **상수로 메우지 않는다.** 조용히 번인으로 떨어지면 재무 채무와 매입
        #    원장이 서로 다른 실행에 앉고, 그때는 아무 오류도 안 난다.
        raise ValueError(
            "sim_run_id 없이는 걸을 수 없다 — 어느 실행에 쌓는지가 곡선의 정체다."
            " 실행을 먼저 만들고(`sim_run.create_sim_run`) 그 이름을 넘겨라"
        )
    if start > end:
        raise ValueError(
            f"걷기 범위가 거꾸로다: {start.isoformat()} ~ {end.isoformat()}"
            " — 어느 쪽이 시작인지를 여기서 정하지 않는다"
        )
    if now.tzinfo is None:
        raise ValueError(
            "시간대 없는 시각으로는 마감을 못 잰다 — 어느 지역의 10:30 인지가 없다."
            " walk 는 시계를 안 읽으므로 부르는 쪽이 시간대를 붙여 줘야 한다"
        )
    if max_consecutive_failures < 1:
        raise ValueError(
            f"연속 사고 상한이 {max_consecutive_failures} 다 — 1 보다 작으면 한 날도 못 걷는다"
        )
    if walked_now is not None and not _same_moment(walked_now, now):
        # 🔴 **칸이 거짓말을 하게 두지 않는다.** 원장에 적는 문자열과 날마다 붙여
        #    쓰는 시각이 갈리면, 그 칸을 믿고 읽은 사람이 오늘 같은 판을 또 버린다.
        raise ValueError(
            f"기준 시각 문자열 {walked_now!r} 과 걷는 시각 {now.isoformat()} 이 다르다"
            " — 원장에 적는 값과 실제로 건 값이 갈리면 그 칸이 거짓말을 한다"
        )

    # ── 🔴 **켰으면 걷기 전에 규칙을 확인한다** (2026-09-11) ────────────
    #
    # ⚠️ **조용히 아무것도 안 하면 사람이 「승인이 돌았는데 0건이구나」 로 읽는다.**
    #    그래서 첫날을 걷기도 전에 막는다 — 179일을 다 걷고 나서 알면 늦다.
    #
    # 🔴 **여기서 규칙을 지어내지 않는다.** 기본 규칙은 곧 업무 규칙이고, 그러면
    #    아무도 안 정한 규칙으로 곡선이 선다 (`backfill.py` 의 그 규율 그대로).
    if auto_approve:
        try:
            rules_of(sim_run_id)
        except BackfillRuleMissing as exc:
            raise ValueError(
                f"--auto-approve 인데 실행 {sim_run_id!r} 이 백필 규칙을 안 들었다: {exc}"
                " — 규칙을 실은 실행을 먼저 열어라(`sim_run_runner --backfill-rules`)."
                " 조용히 0건으로 걷지 않는다"
            ) from exc

    # 🔴 **상업 조건은 걷기 전에 한 번 읽는다** (2026-09-11). 규칙은 실행에 속하지
    #    날에 속하지 않는다 — 날마다 읽으면 같은 설정을 179번 다시 읽고, 그러다
    #    하루만 다른 조건으로 도는 날이 오면 **왜 그런지를 설정만 보고는 못 읽는다.**
    #
    # ⚠️ **여기서 막지 않는다.** 조건이 없는 것은 사고가 아니라 종전 동작이다 —
    #    `auto_approve` 관문과 축이 다르다 (`terms_of` 설명).
    sales_terms = terms_of(sim_run_id)

    market = calendar()
    # ★ 개장 축과 같은 자리 · 같은 횟수로 한 번 만든다. 표를 읽는 것은 첫 물음 때다.
    batch = ml_batch()

    # ── 🔴 **기준 시각을 걷기 첫 날 전에 남긴다** (2026-09-13) ─────────────
    #
    # ★ 재는 것(이미 다른 값이면 막는다)과 쓰는 것을 **한 자리에서 같이** 한다.
    #   다 걷고 나서 쓰면 179일을 걷다 연속 사고 상한에 멈춘 판에 무엇으로 걸었는지가
    #   안 남는다 — 그리고 그 판이 **가장 먼저 그 질문을 받는 판**이다.
    #
    # 🔴 **막히면 여기서 나간다.** 한 실행을 두 기준 시각으로 이어 걸으면 절반은 마감
    #    전 · 절반은 마감 뒤인 판이 되고, 그 판은 아무것도 증명하지 않는다.
    if walked_now is not None:
        record_now(sim_run_id=sim_run_id, walked_now=walked_now)

    started_ticks = ticks()

    days: list[DayRunOutcome] = []
    skipped: list[date] = []
    incidents: list[WalkIncident] = []
    stopped_at: date | None = None
    stopped_reason: str | None = None
    in_a_row = 0

    day = start
    while day <= end:
        # ── ① 달력 ──────────────────────────────────────────────────
        try:
            is_open = market.is_market_open(day)
        except CalendarNotCovered as exc:
            # 🔴 **건너뛰지 않는다.** 못 읽은 날을 안 선 날로 바꾸면 분모가 줄어든다.
            stopped_at = day
            stopped_reason = f"개장 달력을 못 읽어서 멈춘다: {exc}"
            break
        if not is_open:
            skipped.append(day)
            day += timedelta(days=1)
            continue

        # ── ② 하루 ──────────────────────────────────────────────────
        action = plan_next_action(
            now=_moment_on(day, now),
            as_of=day,
            calendar=market,
            ml_batch=batch,
            gate_result=readiness(day),
        )
        try:
            # 🔴 **받은 축을 그대로 넘긴다.** 여기서 상수를 다시 읽거나 이름을
            #    고쳐 짓지 않는다 — 그러면 걷기가 부른 하루와 걷기가 말한 실행이
            #    갈리고, 성적표가 자기가 무엇을 쟀는지 모르게 된다.
            outcome = run_day_fn(
                action,
                policy_version=policy_version,
                sim_run_id=sim_run_id,
                # 🔴 **받은 스위치를 그대로 넘긴다.** 여기서 규칙의 유무를 보고
                #    다시 정하지 않는다 — 그러면 스위치가 둘이 된다.
                auto_approve=auto_approve,
                # 🔴 **읽은 조건을 그대로 넘긴다.** 하루가 제 손으로 다시 읽지
                #    않는다 — 그러면 같은 설정의 주인이 둘이 되고, 하루를 부르는
                #    모든 검사가 조용히 실 DB 를 친다.
                sales_terms=sales_terms,
                # 🔴 **받은 스위치를 그대로 넘긴다.** 여기서 다시 정하지 않는다 —
                #    그러면 폐기를 켜고 끄는 자리가 둘이 된다.
                auto_maintain=auto_maintain,
            )
        except Exception as exc:  # noqa: BLE001 - 하루가 터져도 다음 날은 걷는다.
            # ★ **터진 날도 사고로 남고 걷기는 이어진다.** 여기서 raise 하면 나머지
            #   날을 통째로 못 본다.
            incidents.append(
                WalkIncident(as_of=day, reason=f"하루 실행이 터졌다: {type(exc).__name__}: {exc}")
            )
            in_a_row += 1
        else:
            days.append(outcome)
            reason = _incident_reason(outcome, scope=action.scope)
            if reason is None:
                in_a_row = 0
            else:
                incidents.append(WalkIncident(as_of=day, reason=reason))
                in_a_row += 1

        # ── ③ 연속 사고 상한 ────────────────────────────────────────
        if in_a_row >= max_consecutive_failures:
            stopped_at = day
            stopped_reason = (
                f"사고가 {in_a_row}일 연속이라 멈춘다 (상한 {max_consecutive_failures})"
                " — 이 뒤를 더 걸어도 같은 사실이 줄 수만 늘어난다"
            )
            break

        day += timedelta(days=1)

    # ── ④ 🔴 **현금 축을 읽는다. 여기서 세지 않는다** (2026-09-12) ─────────
    #
    # ★ 다 걷고 한 번 읽는다 — 걷는 동안 읽으면 그날 마감이 아직 안 선 날의 행을
    #   보게 되고, 그 빈 자리가 「마감이 안 돌았다」로 보인다.
    #
    # 🔴 **읽다 터져도 걷기를 안 터뜨린다.** 그리고 그 실패를 `incidents` 에도
    #    안 넣는다 — 사고 줄은 **걸음의 축**이고, 조회 실패는 걸음이 아니다.
    #    못 읽은 사실은 `closings_reason` 이 따로 든다.
    closings: tuple[Mapping[str, Any], ...] = ()
    closings_reason: str | None = None
    try:
        closings = tuple(closings_of(sim_run_id=sim_run_id, start=start, end=end))
    except Exception as exc:  # noqa: BLE001 - 성적표를 통째로 잃는 것이 더 나쁘다.
        closings_reason = f"{type(exc).__name__}: {exc}"

    return WalkResult(
        start=start,
        end=end,
        days=tuple(days),
        skipped_days=tuple(skipped),
        incidents=tuple(incidents),
        stopped_at=stopped_at,
        stopped_reason=stopped_reason,
        elapsed_seconds=ticks() - started_ticks,
        closings=closings,
        closings_reason=closings_reason,
        walked_now=walked_now,
    )


def _same_moment(walked_now: str, now: datetime) -> bool:
    """받은 문자열이 **걷는 시각과 같은 시각**을 말하는가.

    ★ **시각과 시간대 오프셋을 둘 다 본다.** 같은 순간이라도 오프셋이 다르면
      `timetz()` 가 다른 시각을 날마다 붙인다 — 그러면 걷기가 쓰는 시각이 갈린다.

    ⚠️ **읽지 못하는 문자열은 다르다고 본다.** 적을 값이 시각이 아니면 칸이 거짓말을 한다.
    """
    try:
        받은 = datetime.fromisoformat(walked_now)
    except ValueError:
        return False
    return (
        받은.tzinfo is not None
        and 받은.replace(tzinfo=None) == now.replace(tzinfo=None)
        and 받은.utcoffset() == now.utcoffset()
    )


def _moment_on(day: date, now: datetime) -> datetime:
    """받은 시각의 **시각 부분**을 그날에 붙인다.

    🔴 **왜 필요한가.** `plan_next_action` 은 `now` 와 그날 10:30 을 비교한다. 받은
      `now` 를 179일에 그대로 쓰면 첫날 말고는 전부 마감이 한참 지난 것으로 읽히고,
      `NONE_READY` 인 날이 전부 `RUN_AND_RECORD` 가 된다.

    ★ **시간대를 새로 만들지 않는다.** `timetz()` 가 받은 값의 것을 그대로 나른다.
    """
    return datetime.combine(day, now.timetz())


def _or_unknown(value: Any) -> str:
    """모르는 칸은 `모름`. 🔴 **0 으로 접지 않는다** — 없는 것과 0 은 다른 사실이다."""
    return "모름" if value is None else str(value)


def _incident_reason(outcome: DayRunOutcome, *, scope: DayScope) -> str | None:
    """이 날이 사고인가. 사고면 사유, 아니면 `None`.

    🔴 **`scheduler` 가 낸 값만 읽는다.** 여기서 상태 목록을 다시 세지 않는다 —
      세면 `_LEDGER_GAP_STATUSES` 가 두 곳에 생기고, 한쪽만 바뀌는 날이 온다.

    ```text
    action == BLOCKED                       달력이든 배치 축이든 게이트든 못 읽었다
    돌기로 했는데 procurement_status 가
      NOT_ATTEMPTED                         개장이 막혔거나 장부 관문이 돌아섰다
                                            🔴 scope 가 FULL 이든 LEDGER_ONLY 든 같다
    failed_items 가 비지 않았다              품목이 터졌다 (나머지는 돌았다)
    outbound_status == FAILED               나가려다 못 나갔다
    closing_status == FAILED                마감이 닫아 보다 터졌다 (2026-09-16)
    ```

    🔴 **마감 `FAILED` 는 사고다** (2026-09-16). `SIM-CHAIN-CHECK-0916` 에서 03-09 부터
      마감이 매일 `FAILED` 였는데 요약은 「사고 0건」 이었다. 그날 장부가 안 닫혔으면
      다음 날 판단은 안 닫힌 장부 위에서 돈다 — 조용히 계속 가는 것보다 연속 사고
      상한에 걸려 멈추는 쪽이 낫다.

    ⚠️ **마감 `BLOCKED` · `NOT_OPENED` 는 여기서 안 센다.** 그 둘은 앞 단계(개장 · 장부
      관문)가 이미 막힌 날의 결과이고, 그 사실은 위 줄이 이미 사고로 잡았다.

    ⚠️ **`WAIT` 은 사고가 아니다.** *"아직"* 이지 *"못"* 이 아니다. 그 구분이
      `scheduler` 가 다섯 어휘를 가른 이유이고, 여기서 접으면 그게 무의미해진다.

    ⚠️ **`NO_ML_BATCH` 로 판단을 안 돈 것은 사고가 아니다** (2026-09-13). 그 날
      `procurement_status` 는 `NOT_ATTEMPTED` 가 아니라 `NO_ML_BATCH` 이고, 그래서
      아래 줄에 안 걸린다.

      🔴 **그렇다고 `LEDGER_ONLY` 를 판정에서 통째로 빼지 않는다.** 그 날도 개장이
        막히거나 장부 관문이 돌아서면 `NOT_ATTEMPTED` 로 남고, **그것은 사고다** —
        빼면 배치 없는 토요일에 난 장부 사고가 안 세진다.

    ⚠️ **`NOT_A_MARKET_DAY` 도 사고가 아니다.** 달력 검사가 먼저 걸러서 여기까지
      오지도 않지만, 온다 해도 *"안 서는 날"* 은 정상이다.

    ⚠️ **출고 `NOTHING_DUE` 도 같은 결로 사고가 아니다.** *"나갈 것이 없다"* 는
      *"못 나갔다"* 가 아니다 — 예약이 아직 0행인 지금 그것을 사고로 세면 **매일이
      사고**가 되고, 사고 목록이 아무것도 안 가리킨다.

    🔴 **출고 `NOT_ATTEMPTED` 를 여기서 다시 세지 않는다.** 거기까지 못 간 날은
      판단 단계를 안 탄 날이고, 그것은 **위 줄이 이미 사고로 잡았다** — 겹쳐 적으면
      한 사실이 사고 둘로 세진다.
    """
    if outcome.action == "BLOCKED":
        return f"BLOCKED — {outcome.reason}"
    if scope != "NONE" and outcome.procurement_status == "NOT_ATTEMPTED":
        # ★ 개장 실패와 장부 관문을 한 값이 이미 가른다 — 둘 다 판단 단계를 안 탄다.
        return (
            f"판단 단계를 안 탔다 (개장: {outcome.day_open_status} ·"
            f" 입고: {outcome.inbound_status} · 채권: {outcome.receivable_status}"
            f" · 수금: {outcome.collection_status})"
            + (f" — {'; '.join(outcome.notes)}" if outcome.notes else "")
        )
    if outcome.failed_items:
        return f"품목이 터졌다: {', '.join(outcome.failed_items)}"
    if outcome.outbound_status == "FAILED":
        # ★ **사유를 여기서 짓지 않는다.** 무엇이 못 나갔는지는 `OutboundOut.reason`
        #   이 알고, `_stage` 가 그것을 note 로 실어 보냈다 — 그 값을 그대로 나른다.
        return "출고가 못 나갔다" + (f" — {'; '.join(outcome.notes)}" if outcome.notes else "")
    if outcome.closing_status == "FAILED":
        # ★ **사유를 여기서 짓지 않는다.** `_stage` 가 `ClosingOut.reason` 을 note 로 실었다.
        return "마감이 못 섰다" + (f" — {'; '.join(outcome.notes)}" if outcome.notes else "")
    return None


# ── 진입점 — 인자만 받는다 ─────────────────────────────────────────────
#
# 🔴 **로직이 여기 없다.** `walk()` 가 다 안다. 이 아래는 문자열을 날짜로 바꾸고
#    결과를 사람이 읽게 찍는 것뿐이다.


def _parser() -> argparse.ArgumentParser:
    """인자 정의. 🔴 **날짜에 기본값이 없다.**

    ⚠️ 기본 범위를 두면 **그 범위가 곧 업무 규칙이 된다** — 아무도 그것을 정한 적이
      없는데 성적표마다 그 범위가 찍히고, 나중에 *"왜 2월 7일부터인가"* 에 답할
      사람이 없다. 안 주면 막고 사유를 낸다.

    ⚠️ `--now` 도 기본값이 없다. 여기서 시계를 읽으면 **돌린 시각에 따라 성적이
      갈리고**, 그 사실이 성적표 어디에도 안 남는다.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.master.backtest_runner",
        description="범위를 하루씩 걸으며 개장일마다 하루 실행(run_scheduled_day)을 부른다",
    )
    parser.add_argument(
        "--sim-run-id",
        required=True,
        help="어느 실행에 쌓는가 (예: SIM-WALK-202601) · 🔴 기본값 없음 — 안 주면 막는다",
    )
    parser.add_argument("--start", required=True, help="걷기 시작일 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="걷기 종료일 (YYYY-MM-DD · 포함)")
    parser.add_argument(
        "--now",
        required=True,
        help="걷는 동안 쓸 시각 (ISO 8601 · 시간대 필수 · 예: 2026-09-07T10:35+09:00)",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=MAX_CONSECUTIVE_FAILURES,
        help=f"연속 사고 상한 (기본 {MAX_CONSECUTIVE_FAILURES})",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help=(
            "🔴 각 판단 바로 뒤에 규칙대로 승인한다 · master_decisions 에 행이 쓰인다"
            " · 안 주면 한 건도 승인하지 않는다"
        ),
    )
    parser.add_argument(
        "--auto-maintain",
        action="store_true",
        default=False,
        help=(
            "🔴 개장 바로 뒤에 그날 창고 자리를 비운다 · 폐기대기 Lot 이 없어진다"
            " (되돌릴 경로 없음) · 안 주면 한 Lot 도 건드리지 않는다"
        ),
    )
    return parser


def _krw(value: Decimal) -> str:
    """금액 한 칸. **원 단위로 자리를 끊어 찍는다.** `Decimal` 전용이다."""
    return f"{value:,.0f}"


def _krw_or_none(value: Decimal | None, *, recorded: int, total: int) -> str:
    """금액 한 칸 — **`None` 은 「기록 없음」이다. 🔴 0 이 아니다** (2026-09-16).

    ```text
    값이 있다              1,234,567
    한 날도 안 기록됐다    기록 없음
    일부만 기록됐다        기록 없음 (179일 중 120일)
    ```

    🔴 **0 과 「기록 없음」을 한 글자로 접지 않는다.** *"0원이 나갔다"* 와
      *"이 축을 안 셌다"* 를 같은 0 으로 적으면, 고칠 것이 있는 판과 없는 판이
      화면에서 같아진다 — 이 함수가 있는 이유가 그것 하나다.

    ★ **일부만 기록된 판은 몇 날인지까지 찍는다.** 「기록 없음」만 찍으면 «한 날도
      안 셌다» 로 읽히는데, 사실은 **섞여 있다** 는 것이 그 판의 사실이다.
    """
    if value is not None:
        return _krw(value)
    if recorded == 0:
        return "기록 없음"
    return f"기록 없음 ({total}일 중 {recorded}일)"


def _기록된_날수(rows: Sequence[Mapping[str, Any]], column: str) -> int:
    """그 칸을 **실제로 기록한** 마감행이 몇 날인가. 🔴 **없는 칸은 터진다.**"""
    return sum(1 for row in rows if row[column] is not None)


def _cash_lines(result: WalkResult) -> list[str]:
    """현금 두 줄 (2026-09-12). 🔴 **맞아도 찍고 · 0 도 찍고 · 없으면 「없음」이다.**

    ```text
    현금        {매입유출: n · 물류유출: n · 인건이자: n · 수금: n · 순현금: n · 기말잔액: n}
    현금항등식  Δ잔액 n · Σ순현금 n · 차이 n · 어긋난 날 n일 → 🔴 깨짐
    ```

    🔴 **네 상태를 접지 않는다.**

    ```text
    마감행이 있다      숫자와 판정을 찍는다 (성립이어도 찍는다)
    0행이다            「없음」 — 마감이 한 번도 안 돌았다
    못 읽었다          「못 읽음」 — 0행과 다른 사실이다
    칸을 안 셌다       「기록 없음」 — 마감은 섰는데 그 축을 안 센 것이다 (2026-09-16)
    ```

    ⚠️ **0 으로 메우지 않는다.** *"마감이 안 돌았다"* 와 *"돌았는데 0 이다"* 를
      같은 0 으로 적으면, 고칠 것이 있는 판과 없는 판이 화면에서 같아진다.
      **「안 셌다」도 마찬가지다** — `_krw_or_none` 이 그 자리를 지킨다.
    """
    if result.closings_reason is not None:
        못읽음 = f"못 읽음 — {result.closings_reason}"
        return [f"현금        {못읽음}", f"현금항등식  {못읽음}"]

    현금 = result.cash
    항등식 = result.cash_identity
    # 🔴 **현금 합은 마감이 선 날만의 합이다** (2026-09-16). 그 사실을 줄에 드러낸다 —
    #    `SIM-CHAIN-CHECK-0916` 에서 매입유출이 1,625만 으로 찍혔는데 purchases 는 5,586만
    #    이었다. 03-06 뒤로 마감이 안 서서 합이 조용히 작아진 것이다.
    일수 = f"마감이 선 날 {len(result.closings)}일 / 돈 날 {len(result.days)}일"
    if 현금 is None or 항등식 is None:
        없음 = "없음 — 그 구간에 마감행이 0행이다"
        return [f"현금        {없음} · {일수}", f"현금항등식  {없음}"]

    # 🔴 **0 인 칸도 그대로 찍는다.** 빼면 V4~V6 세 판을 통과시킨 그 0 이 사라진다.
    # 🔴 **그리고 「기록 없음」을 0 으로 접지 않는다** (2026-09-16). 운영비 축은 나중에
    #    생겨서 그 축을 안 센 실행이 DB 에 남아 있다 — 그쪽의 빈 값은 「0원」이 아니다.
    #    ⚠️ 칸이 아직 `NOT NULL DEFAULT 0` 이라 이 갈래는 **오늘은 안 탄다.** 칸의 주인인
    #       재무가 코드 뜻대로 바로잡는 날 탄다 — 그때 안 죽으려고 미리 세운다.
    def _칸값(column: str) -> str:
        return _krw_or_none(
            현금[column],
            recorded=_기록된_날수(result.closings, column),
            total=len(result.closings),
        )

    칸 = " · ".join(f"{이름}: {_칸값(column)}" for 이름, column in _CASH_FLOWS)
    판정 = "🟢 성립" if 항등식.holds else "🔴 깨짐"
    항등식줄 = (
        f"Δ잔액 {_krw(항등식.balance_delta_krw)}"
        f" · Σ순현금 {_krw(항등식.net_cash_krw)}"
        f" · 차이 {_krw(항등식.gap_krw)}"
        f" · 어긋난 날 {항등식.mismatched_days}일 → {판정}"
    )
    # ★ **기말잔액은 `_krw` 그대로다** — `base_cash_balance_krw` 는 `NOT NULL` 이라
    #   `_NULLABLE_CASH_COLUMNS` 에 없고, 「기록 없음」이 설 수 없는 칸이다.
    return [
        f"현금        {{{칸} · 기말잔액: {_krw(현금[BASE_CASH_BALANCE])}}} · {일수}",
        f"현금항등식  {항등식줄}",
    ]


def _moment_line(result: WalkResult) -> str:
    """기준 시각 한 줄 (2026-09-13). 🔴 **마감 전이면 그 사실을 찍는다.**

    ```text
    기준시각  2026-09-13T16:00+09:00 · 날마다 16:00 · 마감 10:30 뒤
    기준시각  2026-09-13T09:00+09:00 · 날마다 09:00 · 🔴 마감 10:30 전
              — 예측이 늦는 날이 통째로 안 돈다      (실제로는 한 줄이다)
    ```

    ★★ **이 한 줄이 없어서 한 판을 버렸다.** V9① 을 09:00 으로 걸었더니 ML 배치가
      없는 날이 전부 `WAIT` 이 되어 14일이 영영 안 돌았고, 매입 셀 213 → 171 ·
      `E4` 42 → 0 · 폐기 16 → 59 로 통째로 갈렸다. **코드가 아니라 입력 하나였다.**

    🔴 **경고 문장이 「예측이 늦는 날」이다 · 「배치 없는 날」이 아니다** (2026-09-13).
      배치 없는 날은 `NO_ML_BATCH` 가 되어 마감과 무관하게 장부가 돈다
      (`scheduler.plan_next_action` 이 게이트 **앞**에서 가른다). 마감 전 시각에 안
      도는 것은 **배치가 도는 날인데 예측이 아직 안 온 날**뿐이다 — 옛 문장을 두면
      V9① 을 고친 판이 그 사실을 거꾸로 말한다.

    ★ **받은 문자열을 먼저 찍는다.** 날짜 부분은 안 쓰이지만 사람이 준 것과 코드가
      쓰는 것(`날마다`)이 둘 다 보여야 왜 갈렸는지 읽힌다.

    🔴 **전/뒤를 여기서 따로 판정하지 않는다.** 걷기가 쓰는 그대로(`_moment_on`)
      붙인 시각을 `scheduler.deadline_at` 과 비교한다 — `plan_next_action` 이
      `now >= deadline` 을 뒤로 보는 그 경계 그대로다.
    """
    if result.walked_now is None:
        return "기준시각  🟡 안 받았다 — 이 걷기가 어느 시각으로 걸렸는지 원장에 안 남는다"
    날 = result.start
    날마다 = _moment_on(날, datetime.fromisoformat(result.walked_now))
    마감 = deadline_at(날)
    시각 = f"{날마다:%H:%M}" if not (날마다.second or 날마다.microsecond) else f"{날마다:%H:%M:%S}"
    머리 = f"기준시각  {result.walked_now} · 날마다 {시각} · "
    if 날마다 < 마감:
        return 머리 + f"🔴 마감 {마감:%H:%M} 전 — 예측이 늦는 날이 통째로 안 돈다"
    return 머리 + f"마감 {마감:%H:%M} 뒤"


def _closing_line(result: WalkResult) -> str:
    """재무 일마감 한 줄 (2026-09-16). 🔴 **0 인 칸도 찍는다.**

    ⚠️ **빈 자리를 「없음」 으로 안 적는다.** 그 말은 현금 줄이 「마감행 0행」 에 쓰고, 「못
      읽음」 과 안 섞이는지를 검사가 요약 전체에서 잰다 — 여기는 「안 섰다」·「안 났다」 다.

    ```text
    마감      {'BLOCKED': 0, 'CLOSED': 45, 'FAILED': 3, ...} · 마지막 마감일 2026-03-06
              · 첫 실패일 2026-03-09 (마감 실패: ...)      (실제로는 한 줄이다)
    ```

    ★★ **이 줄이 없어서 마감이 3월 초에 멈춘 것을 아무도 못 봤다.**
    """
    마지막 = result.last_closed_on
    첫실패 = result.first_closing_failure
    return (
        f"마감      {dict(sorted(result.closing_statuses.items()))}"
        f" · 마지막 마감일 {마지막.isoformat() if 마지막 is not None else '안 섰다'}"
        + (
            f" · 첫 실패일 {첫실패[0].isoformat()} ({첫실패[1]})"
            if 첫실패 is not None
            else " · 첫 실패일 안 났다"
        )
    )


def _ledger_block_line(result: WalkResult) -> str:
    """원장못씀 한 줄 (2026-09-16). 🔴 **0 건이어도 찍고 · 0 인 갈래도 찍는다.**

    ```text
    원장못씀  고유 2건 {등급 둘: 1 · 회차금액 없음: 1 · 지급일 없음: 0 · 도착분 없음: 0}
              · 재시도 12회 · 영영 안 될 것 1건       ← 실제로는 한 줄이다
    ```

    🔴 **세 수를 낸다.** 「고유」가 크기이고 「재시도」가 소음이다 — 한 수로 접으면
       같은 승인이 날마다 다시 막히는 것이 건수로 읽혀 한 건이 며칠치로 부푼다.
       **「영영 안 될 것」도 고유 건수다** — 그 수가 곧 발표에서 말할 크기다.

    ★ **갈래 순서는 `LEDGER_BLOCK_KINDS` 그대로다** — 가나다순으로 세우지 않는다.
      순서가 뜻이라 `관측시점` 줄과 같은 규율이다.

    🔴 **세고 찍기만 한다.** 이 함수도 이 줄도 걷기가 고르는 안·재시도·분류 결과를
       바꾸지 않는다.
    """
    센것 = result.ledger_blocks
    갈래 = " · ".join(f"{이름}: {수}" for 이름, 수 in 센것.unique_by_kind.items())
    return (
        f"원장못씀  고유 {센것.unique}건 {{{갈래}}}"
        f" · 재시도 {센것.retries}회 · 영영 안 될 것 {센것.permanent}건"
    )


def _inspection_line(result: WalkResult) -> str:
    """물류 점검 한 줄. **칸 순서는 하루 안의 순서 그대로**, 칸 안은 가나다순이다."""
    칸별 = {칸: dict(sorted(분포.items())) for 칸, 분포 in result.inspection_statuses.items()}
    return f"물류점검  {칸별} · 문제 {dict(sorted(result.inspection_counts.items()))}"


def format_summary(result: WalkResult) -> str:
    """걷기 결과를 사람이 읽을 줄로. **값을 새로 만들지 않는다.**"""
    lines = [
        f"범위      {result.start.isoformat()} ~ {result.end.isoformat()}",
        # 🔴 **기준시각 줄을 지우지 않는다** (2026-09-13). 「이 판이 무엇 위에
        #    섰나」 자리다 — 걷기 요약에는 기준커밋 줄이 없어 범위 바로 아래다.
        #    이 줄이 없어서 같은 코드로 건 두 판이 왜 갈렸는지 아무도 못 읽었다.
        _moment_line(result),
        f"돈 날     {len(result.days)}일 · 휴장 {len(result.skipped_days)}일",
        f"판단      {dict(sorted(result.actions.items()))}",
        # 🔴 **매입 줄을 판단 줄에 접지 않는다** (2026-09-13). 배치 없는 날은 품목이
        #    없어 `종료코드` 줄에서 빠진다 — 그 날들이 어디 갔는지를 이 줄이 말한다.
        f"매입      {dict(sorted(result.procurement_statuses.items()))}",
        # 🔴 **원장못씀 줄을 매입 줄에 접지 않는다** (2026-09-16). *"매입 판단이
        #    돌았나"* 와 *"그 승인이 매입 원장에 닿았나"* 는 축이 다르다 — 이 줄이
        #    없어서 확인 걷기에서 6건 726kg 255,287원(수량 0.76% · 금액 0.46%)이
        #    승인은 났는데 원장에 한 행도 안 남은 채 요약 어디에도 안 보였다.
        _ledger_block_line(result),
        f"종료코드  {dict(sorted(result.end_codes.items()))}",
        f"채권      {dict(sorted(result.receivable_statuses.items()))}",
        f"판매      {dict(sorted(result.sales_statuses.items()))}",
        f"판매코드  {dict(sorted(result.sales_end_codes.items()))}",
        f"출고      {dict(sorted(result.outbound_statuses.items()))}",
        # 🔴 **승인 줄을 접지 않는다** (2026-09-11). 단계와 어휘가 축이 다르므로
        #    두 줄이다 — 한 줄로 묶으면 *"안 켰다"* 와 *"켰는데 0건"* 이 같아 보인다.
        f"승인      {dict(sorted(result.approval_statuses.items()))}",
        f"승인어휘  {dict(sorted(result.approval_outcomes.items()))}",
        # 🔴 **확정 줄을 접지 않는다** (2026-09-11). *"승인을 적었다"* 와 *"판매가
        #    섰다"* 는 축이 다르다 — 이 줄이 없어서 `RECORDED 7 · 재검증 PASSED 7` 을
        #    보고 판매가 선 줄 알았고, `sales` 는 0행이었다.
        f"확정어휘  {dict(sorted(result.confirmation_outcomes.items()))}",
        # 🔴 **예약 줄을 확정 줄에 접지 않는다** (2026-09-12). *"판매가 섰다"* 와
        #    *"그만큼 재고를 잡았다"* 는 축이 다르다 — 이 줄이 없어서 확정 34건을
        #    보고 재고가 잡힌 줄 알았고, 같은 재고가 다음 날 또 팔렸다.
        f"예약어휘  {dict(sorted(result.reservation_outcomes.items()))}",
        # 🔴 **라벨 줄을 접지 않는다** (2026-09-11). 규칙이 순서를 갖게 되면서
        #    *"규칙이 무엇을 적었나"* 와 *"그날 무엇이 섰나"* 가 갈릴 수 있다 —
        #    이 줄이 그 둘을 잇는 유일한 자리다.
        f"라벨어휘  {dict(sorted(result.label_outcomes.items()))}",
        # 🔴 **전이 줄을 접지 않는다** (2026-09-11). *"승인을 적었다"* 와 *"그 승인이
        #    원장에 닿았다"* 는 축이 다르다 — 이 줄이 없어서 `RECORDED 15` 를 보고
        #    원장에 닿은 줄 알았고, `purchases` 는 0행이었다.
        f"전이      {dict(sorted(result.transition_outcomes.items()))}",
        # 🔴 **유지보수 줄을 접지 않는다** (2026-09-11). 몇 Lot 이 없어졌고 몇이
        #    **사람 몫으로 남았는지**가 보여야 한다 — 창고가 안 비는 날 봐야 할
        #    자리가 `SKIPPED_HELD_ALLOCATION` 이고, 접으면 그 줄이 사라진다.
        f"유지보수  {dict(sorted(result.maintenance_statuses.items()))}",
        f"유지어휘  {dict(sorted(result.maintenance_outcomes.items()))}",
        # 🔴 **물류점검 줄을 접지 않는다 · 0 도 찍는다** (2026-09-14). 이 줄이 없어서
        #    걷기 끝에 「점검이 실패한 날이 있었나」 를 증명할 수 없었다. `FAILED` 0 이
        #    안 보이면 *"없었다"* 와 *"안 셌다"* 가 같아진다.
        _inspection_line(result),
        # 🔴 **LLM 줄은 0 인 어휘도 찍는다** (2026-09-12). 다른 어휘 줄과 여기서
        #    갈린다 — 저쪽의 0 은 *"그 사건이 없었다"* 이고 이쪽의 `SUCCESS` 0 은
        #    **그 자체가 사고**다. 71영업일을 `SUCCESS` 0건으로 걷고도 아무도
        #    모른 것이 「0이라 안 보임」의 모양이었다 (`SIM-CHAIN-V6`).
        f"LLM어휘   {dict(sorted(result.llm_outcomes.items()))}",
        # 🔴 **관측 줄도 0 인 칸을 찍는다** (2026-09-12). `LLM어휘` 와 같은 규율이다 —
        #    처음에는 「실었다」가 0 이고 **그 숫자가 늘어나는 것이 이 일의 진도**다.
        #    0 이라 빼면 부서가 연결한 날에도 성적표가 아무 말을 안 한다.
        #
        # ⚠️ **이 줄만 `sorted` 를 안 쓴다.** 순서가 뜻이라 `_OBSERVED_AT_LABELS` 가
        #    정한 그대로 찍는다 — 가나다순으로 세우면 「안쟀다」가 앞에 온다.
        f"관측시점  {dict(result.observation_coverage)}",
        # 🔴 **현금 두 줄을 접지 않는다** (2026-09-12). *"현금이 얼마 움직였나"* 와
        #    *"그만큼 잔액이 움직였나"* 는 축이 다르다 — 이 줄이 없어서 V7 에서
        #    매입 유출 27,122,228 원이 잔액에서 안 빠진 것을 179일 동안 아무도
        #    못 봤다. 🔴 **맞아도 찍는다** — 0 이라 안 보이면 아무도 안 본다.
        # 🔴 **마감 줄을 현금 줄 바로 위에 둔다** (2026-09-16). 현금 합이 어느 날들의
        #    합인지가 이 줄에 있다 — 이 줄이 없어서 03-06 뒤로 마감이 0행인 판을
        #    「사고 0건 · 현금항등식 🟢」 으로 읽었다.
        _closing_line(result),
        *_cash_lines(result),
        f"사고      {len(result.incidents)}건",
        f"소요      {result.elapsed_seconds:.1f}초",
    ]
    # ⚠️ **사유를 코드 밑에 붙인다.** `{BLOCKED: 7}` 만으로는 무엇이 막았는지를
    #    못 읽고, 그것이 오늘 밤 사람이 손으로 재현해야 했던 이유다.
    lines += [f"  확정막힘  {사유}" for 사유 in result.confirmation_reasons]
    # 🔴 **출고실패 줄을 사고 줄에 접지 않는다** (2026-09-15). 사고 줄은 날 단위이고
    #    이 줄은 품목 단위다 — 고아 예약 미설명 6건을 되짚으려면 터진 순간의 후보가
    #    몇 개 · 몇 kg 이었는지가 한 줄에 있어야 한다.
    lines += [f"  출고실패  {줄}" for 줄 in result.outbound_failure_lines]
    lines += [f"  사고 {one.as_of.isoformat()}  {one.reason}" for one in result.incidents]
    if result.stopped_reason is not None:
        lines.append(f"멈춤      {result.stopped_at} — {result.stopped_reason}")
    return "\n".join(lines)


def _use_utf8_output() -> None:
    """요약을 찍다 죽지 않게 출력 스트림을 UTF-8 로 맞춘다.

    🔴 **걷기를 다 마치고 `print` 에서 죽었다** (2026-09-09 실측).

    ```text
    UnicodeEncodeError: 'cp949' codec can't encode character '\\u2014'
      File "app/master/backtest_runner.py", line 401, in main
        print(format_summary(result))
    ```

      `format_summary` 는 한국어와 `—` 로 적는다. 윈도우 기본 인코딩이 cp949 라
      그 한 글자에서 터졌고, **결과는 다 계산해 놓고 성적표만 잃었다** — 다시
      보려면 걷기를 통째로 또 돌려야 한다.

    ★ **요약 문장을 ASCII 로 낮추지 않는다.** 사람이 읽으라고 쓴 한국어이고,
      바꿔야 할 것은 문장이 아니라 그 문장을 내보내는 통로다.

    ⚠️ **여기서만 바꾼다 — 라이브러리 코드가 아니라 진입점이다.** `walk()` 나
      `format_summary()` 가 프로세스 전역 스트림을 건드리면, 그것을 부르는 쪽의
      출력 설정까지 이 모듈이 정하는 셈이 된다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # ★ 감싸인 스트림(파이프 대역·캡처)이면 그쪽 규칙을 따른다. 여기서
            #   억지로 바꾸려다 진입점이 터지면 고치려던 것과 같은 일이 난다.
            continue
        reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: Sequence[str]) -> int:
    """진입점. **인자만 받아 `walk()` 에 넘긴다.**

    :returns: 끝까지 걸었고 사고가 없으면 0. 아니면 1 — **조용히 0 을 내지 않는다.**
    """
    args = _parser().parse_args(argv)
    _use_utf8_output()
    # 🔴 **걷기 전에 등록소를 채운다.** 이 진입점은 `app/main.py` 를 안 거치므로,
    #    이 줄이 없으면 등록소가 전부 빈 채로 걷는다 — 걷는 날마다 *"하루 넘김
    #    미등록: finance, logistics"* 로 돌아서고 5일이 5일 다 사고였다 (2026-09-09).
    #
    # ★ **여기서 무엇을 등록할지 정하지 않는다.** 목록의 주인은 `bootstrap` 하나이고
    #   FastAPI 진입점도 같은 함수를 부른다. 두 진입점이 다른 세상을 보면 CLI 로 낸
    #   성적이 앱의 성적이 아니다.
    #
    # ⚠️ **`walk()` 안이 아니라 여기다.** `walk` 는 검사가 대역을 꽂아 부르는 함수이고,
    #   거기서 전역 등록소를 채우면 검사가 만든 세상을 조립 뿌리가 덮어쓴다.
    wire_registries()
    result = walk(
        sim_run_id=args.sim_run_id,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        now=datetime.fromisoformat(args.now),
        max_consecutive_failures=args.max_consecutive_failures,
        auto_approve=args.auto_approve,
        auto_maintain=args.auto_maintain,
        # 🔴 **받은 문자열 그대로 넘긴다** (2026-09-13). 위 `now` 는 파싱한 값이라
        #    `16:00` 이 `16:00:00` 이 되고, 사람이 준 것이 원장에서 사라진다.
        walked_now=args.now,
    )
    print(format_summary(result))
    return 0 if result.completed and not result.incidents else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
