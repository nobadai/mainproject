"""
scheduler.py — **깨어났을 때 무엇을 할지 정하고, 그 답을 따른다.**

ML 이 매일 아침 09:23 쯤 예측을 적재한다. 이 모듈은 09:30 부터 5분 간격으로 깨어나
그것을 받고, 10:30 까지 안 오면 **한 번 돌려 `E4_NOT_STARTED` 로 확정 기록**한다.

```text
시작   09:30 KST      SCHEDULE_START
간격   5분             SCHEDULE_INTERVAL
마감   10:30 KST      SCHEDULE_DEADLINE
```

🔴 **여기에 데몬이 없다. 잠자는 루프도 cron 파일도 없다.**

  이 판은 **"깨어났을 때 무엇을 할지"**(`plan_next_action`)와 **"그 답을 따르는
  실행부"**(`run_scheduled_day`)까지다. 실제로 깨우는 것 — OS 스케줄러든 컨테이너
  진입점이든 — 은 **다음 판**이고, 이 파일에 들어오지 않는다.

  ★ **왜 가르나.** 잠자는 루프를 여기 두면 검사가 그것을 못 지난다. 09:30 부터
    10:35 까지 전 구간을 재려면 검사가 **시각을 주입**할 수 있어야 하는데, 루프
    안에 시계가 박혀 있으면 검사는 진짜로 한 시간을 기다리거나 아무것도 못 잰다.
    `plan_next_action` 이 순수 함수라 `now` 를 인자로 받고, 그래서 전 구간이
    한 스위트 안에서 돈다.

★ **상태를 저장하지 않는다.** 재시도 횟수를 어디에도 안 적는다 — `now` 와 마감만
  비교하면 몇 번째 깨어났든 같은 답이 나온다. 적어 두면 그 값이 두 번째 정본이
  되고, 프로세스가 죽는 날 그것부터 틀린다.

---

🔴 **두 축을 둘 다 본다.**

```text
① 달력   market_calendar.is_market_open(as_of)
           False               → NOT_A_MARKET_DAY
           CalendarNotCovered  → BLOCKED (fail-closed · 달력이 이미 그렇게 던진다)

② 게이트  forecast_gate.day_forecast_readiness(...)
           ALL_READY / SOME_READY → RUN_NOW
           NONE_READY             → 마감 전 WAIT · 마감 뒤 RUN_AND_RECORD
           UNREADABLE             → BLOCKED
```

⚠️ **왜 달력도 보나.** 게이트만 보면 *"ML 이 늦은 날"* 과 *"오늘은 배치가 아예 없는
  날"* 이 **둘 다 `NONE_READY`** 라 구별이 안 된다. 앞은 기다려야 하고 뒤는 기다릴
  이유가 없다. 휴장일에 열두 번 깨어나 열두 번 `NONE_READY` 를 보고 마지막에
  `E4` 를 적으면, 나중에 *"몇 날을 못 돌았나"* 를 셀 때 휴장일이 실패로 섞인다.

★ **그래도 1년에 여섯 날은 헛기다린다.** 달력은 열렸는데(`is_open=true`) ML 배치가
  없는 날이다 (노동절 · 어린이날 등 · 매입 실측). 두 축으로도 그 날은 못 가른다 —
  달력이 *"장은 선다"* 고 하고 예측만 없기 때문이다. 받아들이되
  **`RUN_AND_RECORD` 의 사유에 그 사실을 적는다** (`_NO_ML_BATCH`). 그래야 그
  여섯 날이 나중에 눈에 띄고, 사람이 달력 쪽을 볼지 ML 쪽을 볼지 안다.

---

🔴 **다섯 어휘를 섞지 않는다.**

```text
RUN_NOW            예측이 왔다 → 지금 돈다
WAIT               아직 안 왔고 **마감 전** → 5분 뒤 다시 · **판단을 안 돌린다**
RUN_AND_RECORD     마감이 지났는데도 안 왔다 → 한 번 돌려 E4 로 확정 기록
NOT_A_MARKET_DAY   달력이 "오늘 안 선다" → 아무것도 안 한다
BLOCKED            달력이나 게이트를 **못 읽었다** → 재시도로 안 풀린다
```

🔴 **`WAIT` 중에는 판단을 절대 안 돌린다.** 돌리면 09:30 부터 10:30 까지 열두 번
  깨어나며 `E4_NOT_STARTED` 가 **열두 건** 쌓이고, *"몇 날을 못 돌았나"* 가 거짓이
  된다. 확정 기록은 마감 뒤 **한 번**이다.

🔴 **`BLOCKED` 를 `WAIT` 로 접지 않는다.** 접으면 DB 가 죽은 날 스케줄러가 영원히
  재시도하고 **그 사실이 아무 데도 안 남는다.** 없는 것과 못 읽은 것을 가르는
  §1.2-10 이 여기서도 그대로다 (`forecast_gate` · `day_gate` 와 같은 규율).

---

🔴 **서비스 함수를 직접 부른다. HTTP 를 태우지 않는다.**

```text
open_day(as_of)           day_open.py
run_auto_maintenance(as_of)
                          maintenance.py         ← 🔴 개장 바로 뒤다 · 기본이 꺼짐
retry_pending_transitions(as_of)
                          pending_transition.py  ← 🔴 유지보수 뒤 · 입고 앞이다
receive_arrivals(as_of)   inbound.py
issue_receivables(as_of)  receivable.py  ← 🔴 수금보다 앞이다
collect_receipts(as_of)   collection.py
run_procurement(...)      service.py     ← 품목마다
run_sales(...)            service.py     ← 🔴 품목마다 · 매입 뒤 · 출고 앞
ship_due_sales(as_of)     outbound_flow.py
close_day(as_of, …)       closing.py     ← 🔴 하루의 맨 끝이다
```

  ★ 179 영업일 걷기가 `run_procurement` 을 직접 부른다. 스케줄러도 **같은 자리**를
    불러야 백테스트와 운영이 같은 코드다. 여기서 `TestClient` 나 `httpx` 를 태우면
    운영만 라우터를 한 겹 더 지나고, 그 겹에서 갈리는 날 백테스트가 못 잡는다.

🔴 **벽시계는 `clock` 에서만 읽는다.** 이 모듈은 `clock.seoul_now` ·
  `clock.today_in_seoul` 을 **부르는 쪽**이지 새로 읽는 쪽이 아니다
  (`tests/master/test_clock_is_the_only_wall_clock.py` 가 AST 로 지킨다).

---

🔴 **장부가 안 선 날에는 판단을 안 돌린다. 그 결정이 여기서 났다.**

  `inbound.py` 의 `receive_arrivals` 가 이렇게 적어 두고 답을 미뤄 뒀다.

  > ⚠️ **입고가 `FAILED` · `BLOCKED` 인데 매입 판단을 계속하면 현재고와 capacity 가
  >   실제보다 적게 반영된 상태로 판단한다.** 받았어야 할 물건이 장부에 없는 채로
  >   *"창고가 비었으니 더 사자"* 가 나온다.
  >
  > ★ **부르는 쪽이 정한다.** 이 함수는 상태를 값으로 돌려주고, `run_procurement`
  >   진행 여부는 그것을 본 orchestration 의 결정이다.

  ★★ **그 "부르는 쪽" 이 이 파일이다** (`collection.py` 의 `CollectionOut` 도 같은
    말을 같은 이유로 적어 뒀다 — *"들어왔어야 할 현금이 장부에 없는 채로 매입 판단이
    돈다"*). 그래서 `run_scheduled_day` 가 **개장과 판단 사이에서** 막는다. 두 모듈은
    그대로 두고 답만 여기서 낸다 — 상태의 주인은 여전히 입고·수금이다.

```text
입고(InboundOut.status) 또는 수금(CollectionOut.status) 이
  BLOCKED · FAILED   →  🔴 그날 판단을 안 돌린다 · **출고도 안 돌린다**
```

🔴 **출고는 장부 관문 뒤, 판단 뒤다.**

```text
개장 → 입고 → 수금 → [장부 관문] → 판단 → **출고**
```

🔴 **마감은 그 뒤, 하루의 맨 끝이다** (2026-09-10).

```text
개장 → 입고 → 채권 → 수금 → [장부 관문] → 판단 → 출고 → **마감**
```

🔴 **판매 판단은 매입 뒤, 출고 앞이다** (2026-09-10).

```text
개장 → 입고 → 채권 → 수금 → [장부 관문] → 매입 → **판매** → 출고 → 마감
```

  ★★ **`ship_due_sales` 는 판매 판단이 아니다.** 이미 확정된 판매를 내보내는
    단계다 — 이름 때문에 판매가 서 있는 것처럼 보였고, 그래서 걷기 179일에
    판매 판단이 **0건**이었다. 없던 것은 로직이 아니라 **부르는 자리 하나**다.

  ★ **왜 출고 앞인가.** 뒤에 두면 그날 확정된 안이 다음 날에야 나갈 자리가 생긴다.

  🔴 **예측 게이트 안이다.** `WAIT` 인 날은 판매도 안 돈다 — 그 한 줄을 안 지키면
    매입이 피한 문제(열두 번 깨어나며 미완 실행이 열두 건)를 판매가 다시 짓는다.

🔴 **승인은 각 판단 바로 뒤다** (2026-09-11).

```text
개장 → 입고 → 채권 → 수금 → [장부 관문]
     → 매입 판단 → **매입 승인** → 판매 판단 → **판매 승인** → 출고 → 마감
```

🔴 **미적용 전이 재시도는 개장 바로 뒤, 입고 앞이다** (2026-09-11).

```text
개장 → **미적용 전이 재시도** → 입고 → 채권 → 수금 → [장부 관문]
     → 매입 판단 → 매입 승인 → 판매 판단 → 판매 승인 → 출고 → 마감
```

  ★★ **승인 15건이 원장에 한 건도 안 닿던 자리다** (실측 2026-09-11 ·
    `SIM-WALK-2026-APPROVED`). 승인일이 `D` 면 상태가 설 날은 `D+1` 인데
    (`transition._target_state_date`), 걷기는 날짜 순으로 돌아 그 행은 **다음
    차례에** 열린다 — 전이는 **언제나 하루 앞을 본다.** 왜 그런지와 왜 물류가
    그 행을 미리 안 만드는 것이 옳은지는 `pending_transition.py` 가 적는다.

  ★ **왜 개장 바로 뒤인가.** 그날 도착 행이 방금 열렸고, **입고가 그 도착을
    잡아야** 한다. 뒤로 가면 그날 도착이 하루 더 밀린다.

  🔴 **재시도가 하루를 죽이지 않는다.** 또 실패하면 세어서 요약에 올리고 하루는
    계속 간다 — `apply_approval` 이 예외 대신 값을 돌려주는 그 태도 그대로다.

  🔴 **미적용 목록을 새 표에 안 들고 있다.** 승인은 `master_decisions` 에 있고
    원장은 `purchases` 에 있으니 **둘을 맞대면 답이 나온다.**

🔴 **물류 유지보수는 개장 바로 뒤, 그 모든 것의 앞이다** (2026-09-11).

```text
개장 → **물류 유지보수** → 미적용 전이 재시도 → 입고 → 채권 → 수금 → [장부 관문]
     → 매입 판단 → 매입 승인 → 판매 판단 → 판매 승인 → 출고 → 마감
```

  ★★ **창고가 차서 매입이 1월 12일부터 멈추던 자리다** (실측 2026-09-11 · 보수안
    71일 걷기). 매입 15건이 전부 01-05 ~ 01-09 닷새에 몰렸고 나머지 156건이
    *"하드 제약(창고)으로 수량이 0까지 축소되어 제안 불가"* 로 보류였다. 그날
    물류는 `warehouse_free_kg 0` · 이후 `cap_by_date` 전부 `0.0` 을 보냈다 —
    **부서는 경계를 냈고**(`blocked_by` 는 비어 있었다) 재고가 나갈 길이 없었다.

  ★★ **폐기 경로가 통째로 안 불렸다.** 자동 폐기도 자리 반환도
    `logistics.auto_maintenance` 에 이미 있었고, `app/master/` 어디에도 **부르는
    줄 하나**가 없었다 — 판매 판단 0건 · 매입 승인 0건 때와 같은 모양이다.

  ★ **왜 개장 바로 뒤인가.** 그날 자리를 비워야 **그날 입고와 그날 매입 판단**이
    들어갈 자리가 생긴다. 뒤로 가면 비운 자리를 그날이 못 쓰고 하루씩 밀린다.

  ★ **왜 전이 재시도 앞인가 — 재서 정했다** (2026-09-11). 두 단계는 같은 날
    안에서 서로의 결과를 안 본다: 전이(`logistics/transition.py`)는
    `logistics_runtime_fixture` 한 행만 UPDATE 하고 `pallets` 도 `inventory_lots`
    도 안 건드리며, 유지보수(`turnover.load_lot_turnover`)는 그 fixture 를 안
    읽는다. 앞뒤로 갈리는 사실이 없으므로 **자리를 먼저 비운다** — 재고가
    막힌 것이 이 판이 푸는 문제라 그 단계를 앞에 세우는 편이 읽기 쉽다.

  🔴 **기본이 꺼짐이다.** `--auto-maintain` 을 명시로 줄 때만 선다. 승인보다 **더**
    조심할 자리다 — 물류가 *"되돌릴 경로가 없다(`ADJUST_IN` 없음 · 실사 제외)"*
    고 못박았다. 승인은 append-only 표에 한 줄이 남는 것이고, 폐기는 **물건이
    없어진다.**

  🔴 **유지보수가 하루를 죽이지 않는다.** 터지면 세어서 요약에 올리고 하루는
    계속 간다 — 수금 씨앗 · 전이 재시도와 같은 태도다.

  🔴 **사유 · 행위자 · 시각을 마스터가 정해 넘긴다.** 물류가 셋 다 기본값을 두지
    않았고(*"물류가 지어내지 않는다"*), 그 빈칸을 채우는 것이 부르는 쪽의 일이다 —
    어휘의 주인은 `maintenance.py` 다.

  ★★ **끝에 몰지 않는다.** 몰면 판매 판단이 그날의 매입 결과를 못 보고, 다음 날이
    어제 산 것을 못 본다 — 그러면 179일을 걸어도 재고가 영영 안 쌓인다.

  🔴 **기본이 꺼짐이다.** `auto_approve` 를 명시로 켤 때만 선다. 설정에 규칙이
    있다고 켜지지 않는다 — *"있으니까 한다"* 는 암묵 스위치다.

  🔴 **승인 문을 우회하지 않는다.** `backfill.backfill_decisions` 를 그대로 부르고,
    `record_decision` 은 그쪽이 부른다. 여기서 직접 부르면 경계 가드도 규칙도 이
    자리만 안 지난다.

  ★ **왜 출고 뒤인가.** 출고가 재고를 움직인다. 출고 앞에서 닫으면 그날 재고가
    **마감 뒤에 바뀌고**, `daily_closings.inventory_qty_kg` 가 그날 장부와 안 맞는다.
    에러는 안 난다.

  ★★ **왜 관문이 막은 날에도 부르나.** 그 날은 **닫지 않는다** — 그런데
    `NOT_ATTEMPTED` 로 두면 *«마감이 없다»* 와 *«마감이 막혔다»* 가 같아 보인다.
    그래서 `close_day` 에 관문 사유를 넘겨 `BLOCKED` 를 받아 적는다. 어댑터는
    부르지 않는다 — 판정만 어휘로 남는다.

  🔴 **마감이 터져도 그날 걷기 결과를 안 바꾼다.** `_stage` 가 예외를 값으로
    옮기고, 판단·출고 결과는 그대로 나간다 — `try_save_run` 이 `try_` 인 이유와
    같다.

  ★ **왜 판단 뒤인가.** 오늘 산 것이 오늘 나가지 않는다 — 도착이 며칠 뒤다. 그래서
    출고가 보는 재고는 판단이 만든 매입과 무관하고, 순서를 바꿔도 결과가 같아야
    하는데 **같지 않게 보이는 날**이 생긴다. 판단 뒤에 두면 그 물음이 없다.

  ★★ **왜 관문 뒤인가.** 장부가 안 선 날에 출고까지 하면 **재고가 두 번 틀린다** —
    입고가 안 들어온 채로 물건이 나가고, 그 위에서 다음 날 판단이 선다. 판단을
    막는 이유가 그대로 출고를 막는 이유다.

🔴 **무엇을 안 막는지가 더 중요하다.**

```text
RECEIVED / COLLECTED   정상
NOTHING_DUE            **확인했고 낼 것이 없다** — 정상이다. 절대 막지 않는다
NOT_OPENED             하루가 안 열렸다 — 그러면 개장에서 이미 멈춰 여기까지 안 온다
NOT_ATTEMPTED          단계를 안 탔다 — 앞 단계에서 이미 멈춘 것이다
```

  ★★ `NOTHING_DUE` 를 막으면 **대부분의 날이 멈춘다.** 그 어휘를 만든 이유가 정확히
    *"없는 것과 못 한 것은 다르다"* 이고, 여기서 접으면 그 구분이 통째로 무의미해진다.

⚠️ **개장은 그대로다.** 개장이 실패하면 그 뒤를 안 하고, 판단이 실패해도 개장을
  되돌리지 않는다. 이 관문은 **개장과 판단 사이**에만 선다.

🔴 **관문이 막은 날에는 행 하나를 남긴다** (2026-09-09). 나머지는 여전히 값뿐이다.

```text
관문이 막았다        🟢 master_agent_runs 에 행이 있다 (persistence.record_ledger_gap)
그날을 안 돌렸다      🔴 여전히 행이 없다
돌렸는데 적재가 실패   🔴 여전히 행이 없다
```

  ★ **왜 관문만 먼저인가.** 매입 화면이 셋을 *"사유를 남긴 실행이 없습니다"* 한
    문구로 그린다. 관문이 막은 것은 **정상 동작**이고 나머지 둘은 아니라, 정상을
    먼저 갈라내야 남은 둘이 눈에 띈다.

  ⚠️ **세는 것은 아직 안 된다.** `end_code='E4_NOT_STARTED'` 한 값에 예측 미도착 ·
    어댑터 미등록 · 장부 관문이 같이 앉는다. 그것은 `Master 상세 19.0` 이 풀 자리다.

  🔴 **그 행에 실행 축(`sim_run_id`)을 같이 싣는다.** 값의 주인은
    `ledger_repository.BURN_IN_SIM_RUN_ID` 하나이고, 같은 날 판단 행이 싣는 값과 같다.
    안 실으면 축으로 훑는 모든 조회에서 게이트 행만 빠져 **막힌 날이 도로 안 보인다.**
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from app.master import clock, persistence
from app.master.backfill import BackfillOut, SalesTermsRule, backfill_decisions
from app.master.clock import SCHEDULE_DEADLINE, SCHEDULE_INTERVAL, SCHEDULE_START
from app.master.closing import close_day
from app.master.collection import collect_receipts
from app.master.commitment import ITEM_CODES
from app.master.day_open import open_day
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, day_forecast_readiness
from app.master.inbound import receive_arrivals
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.maintenance import MaintenanceOut, run_auto_maintenance
from app.master.market_calendar import MarketCalendar, get_market_calendar
from app.master.outbound_flow import ship_due_sales
from app.master.pending_transition import RetryOut, retry_pending_transitions
from app.master.receivable import issue_receivables
from app.master.run_repository import (
    DAILY_REQUEST_HEAD,
    build_request_id,
    ledger_gap_request_id,
    list_runs,
)
from app.master.sales_terms import apply_sales_terms, read_run_sales_terms
from app.master.schemas import ProcurementRunRequest, SalesBusinessMode, SalesRunRequest
from app.master.service import run_procurement, run_sales

__all__ = [
    "DAILY_POLICY_VERSION",
    "SCHEDULE_DEADLINE",
    "SCHEDULE_INTERVAL",
    "SCHEDULE_START",
    "WALK_BUSINESS_MODE",
    "DayRunOutcome",
    "ItemRunOutcome",
    "ScheduledAction",
    "SchedulerAction",
    "daily_request_id",
    "daily_sales_request_id",
    "deadline_at",
    "ledger_gap_request_id",
    "plan_next_action",
    "run_scheduled_day",
    "scheduled_items",
    "wake_up",
]

logger = logging.getLogger(__name__)

# ── 시각 상수 ───────────────────────────────────────────────────────────
#
# 🔴 **선언은 `clock.py` 에 있다** (2026-09-08 에 옮겼다). 여기서 다시 세지 않고
#    이름만 다시 내보낸다 — `sim_time` 이 기준점을 가져갈 때 `scheduler` 를 통과하면
#    `sim_time → scheduler → service` 고리가 생기기 때문이다.
#
# ★ 값의 **뜻**(언제 깨우고 언제 마감하나)은 그대로 이 파일의 것이다. `clock.py` 는
#   표준 라이브러리만 들이는 leaf 라 그 숫자를 두는 자리일 뿐이다.

#: 하루 실행이 싣는 정책 판. **부르는 쪽이 바꿀 수 있게 인자로도 열어 둔다.**
DAILY_POLICY_VERSION = "v1.3-PROVISIONAL"

#: `RUN_AND_RECORD` 사유에 반드시 들어가는 문장. **1년에 여섯 날을 눈에 띄게 한다.**
#:
#: ★ 문자열을 상수로 둔 이유는 검사가 이 문장을 찾기 때문이다. 사유를 손으로 다시
#:   쓰면 철자가 갈리고, 그러면 그 여섯 날을 나중에 못 센다.
_NO_ML_BATCH = "달력은 열렸는데 ML 배치가 없었다"

#: 🔴 **하루 순서가 판매에 싣는 영업 모드. 한 곳에서만 바꾼다** (2026-09-10).
#:
#: ★ **마스터가 임시로 정한 값이다. 어휘의 주인은 판매이고, 판매가 정하면 여기를
#:   바꾼다 (2026-09-10).**
#:
#: 🔴 **왜 `SPOT_SALES` 인가.** 나머지 셋(`CONTRACT_FULFILLMENT` ·
#:   `CONTRACT_PROPOSAL_NEW` · `CONTRACT_PROPOSAL_RENEWAL`)은 **거래처가 있어야**
#:   성립한다. 하루 순서가 거래처를 고르면 **그것이 곧 영업 정책**이 되고, 정책의
#:   주인이 판매에서 스케줄러로 조용히 옮겨 온다.
#:
#: ⚠️ **마스터는 거래처를 고르지 않는다.** 실행 규칙 파일이 `sales_terms.partner_id`
#:   를 적으면 그 값이 실리고 (`sales_terms.apply_sales_terms`), 안 적으면 종전처럼
#:   아무것도 안 실린다 — **고르는 것은 여전히 코드가 아니다** (2026-09-11).
WALK_BUSINESS_MODE: SalesBusinessMode = "SPOT_SALES"

#: 🔴 **이 상태면 그날 판단을 안 돌린다.** 입고·수금 둘 다 같은 표를 쓴다.
#:
#: ```text
#: BLOCKED   받을(들어올) 대상이 있는데 못 했다 — 장부가 실제보다 적다
#: FAILED    하려다 터졌다 — 아무것도 안 바뀌었다
#: ```
#:
#: 🔴 **`NOTHING_DUE` 는 여기 없다. 넣으면 대부분의 날이 멈춘다.**
#:   *"확인했고 낼 것이 없다"* 는 정상이고, 그 어휘를 만든 이유가 정확히
#:   *"없는 것과 못 한 것은 다르다"* 이다.
#:
#: ⚠️ **`NOT_OPENED` · `NOT_ATTEMPTED` 도 여기 없다.** 둘 다 *"앞에서 이미 멈췄다"*
#:   이고, 개장 관문이 먼저 돌아서서 여기까지 오지도 않는다.
_LEDGER_GAP_STATUSES: frozenset[str] = frozenset({"BLOCKED", "FAILED"})

#: 판단을 안 돌린 이유의 앞머리. **검사가 이 문장을 찾는다** (`_NO_ML_BATCH` 와 같은
#: 이유다 — 손으로 다시 쓰면 철자가 갈리고 그러면 막힌 날을 나중에 못 센다).
_LEDGER_GAP = "장부가 안 서서 판단을 안 돌린다"


#: 스케줄러가 답할 수 있는 **전부**. 여섯 번째를 만들지 않는다.
SchedulerAction = Literal[
    "RUN_NOW",
    "WAIT",
    "RUN_AND_RECORD",
    "NOT_A_MARKET_DAY",
    "BLOCKED",
]


def deadline_at(as_of: date) -> datetime:
    """그날의 마감 시각. **서울 시각으로 붙인다.**

    🔴 **시간대를 여기서 새로 만들지 않는다.** `clock.SEOUL` 을 가져다 쓴다 —
      `timezone(timedelta(hours=9))` 를 따로 들면 값이 같아서 아무도 못 보다가
      규칙이 바뀌는 날 조용히 갈린다 (`revalidation.py` 가 그랬다 · 2026-09-08).
    """
    return datetime.combine(as_of, SCHEDULE_DEADLINE, tzinfo=clock.SEOUL)


def scheduled_items() -> tuple[str, ...]:
    """오늘 돌 품목. **`ITEM_CODES` 를 정렬해서 낸다. 목록을 다시 세지 않는다.**

    ★ `ITEM_CODES` 는 `frozenset` 이라 순서가 없다. 순서를 정해 두지 않으면 같은 날
      두 번 돌 때 품목 순서가 갈리고, 실패한 품목을 비교하기가 어려워진다.
    """
    return tuple(sorted(ITEM_CODES))


#: 판매 키의 머리. **매입 머리에서 갈라 나온다 — 문자열을 다시 적지 않는다.**
_SALES_REQUEST_HEAD = f"{DAILY_REQUEST_HEAD}-SALES"


def daily_request_id(as_of: date, item: str, *, sim_run_id: str) -> str:
    """`REQ-DAILY-SIM-WALK-202601-20260908-배추`. **실행·날짜·품목으로 정해진다.**

    🔴🔴 **실행 축이 필수다** (2026-09-11 · `#`). 없으면 **새 실행이 옛 실행의 승인을
      물려받는다.**

      ```text
      실측  업무 키 `REQ-DAILY-20260105-무`  행 16건 · 실행 5개에 걸쳐 있음
            그 업무 키의 master_decisions 행    1건
      ```

      `decision_repository.list_decisions` 는 `WHERE request_id = %s` 뿐이라 축을 안
      본다. 그래서 A 실행에서 난 승인이 B 실행에서 `ALREADY_DECIDED` 로 읽히고,
      **B 는 자기 원장을 영영 못 만든다** — 새 실행을 열고 2주를 걸었더니
      `ALREADY_DECIDED 24 · RECORDED 0 · 전이 {}` 였다.

    🔴 **기본값을 두지 않는다.** 기본값이 있으면 축을 빠뜨린 호출이 조용히 옛 모양으로
      떨어지고, 그 실패는 `ALREADY_DECIDED` 로만 나타나 **에러가 안 난다.** 문법이
      막게 한다.

    🔴 **시각을 넣지 않는다. 축은 시각이 아니다.** 넣으면 같은 날 두 번 깨어날 때 id 가
      갈리고, **같은 업무가 두 업무로 읽힌다.** 축은 같은 실행 안에서 안 변하므로 그
      읽기를 안 건드린다.

      ⚠️ **인덱스가 그 둘째 행을 막아 주지는 않는다** — 이유는
        `run_repository.build_request_id` 가 적는다 (`run_id` 가 PK 라 그 복합
        유니크는 언제나 통과한다). 종전 이 자리의 *"멱등이 인덱스에 걸린다"* 는
        **거짓이었다.**

    ★ **자리 배치는 `run_repository.build_request_id` 가 정한다** — 축이 꼬리 앞에
      붙는 이유가 거기 적혀 있다 (`LEDGER_GAP_REQUEST_LIKE` 가 꼬리를 문다).
    """
    return build_request_id(
        head=DAILY_REQUEST_HEAD, as_of=as_of, sim_run_id=sim_run_id, tail=item
    )


def daily_sales_request_id(as_of: date, item: str, *, sim_run_id: str) -> str:
    """`REQ-DAILY-SALES-SIM-WALK-202601-20260908-배추`.

    🔴 **매입 키와 갈라야 한다** (2026-09-10).

      **판매가 `daily_request_id` 를 그대로 쓰면 안 된다.** 같은 날 같은 품목이면
      문자열이 같아지고, 그러면 `get_run_by_request_id` 가 **어느 사이클의 실행인지
      못 가른다.**

      ⚠️ 종전 이 자리는 *"인덱스가 두 번째 사이클을 막는다"* 고 적었는데 **거짓이었다**
        (`run_repository.build_request_id` 참고). 막는 것이 아니라 **두 사이클이
        한 업무 키에 뒤섞이는 것**이 문제다 — 막혀서 안 남는 것이 아니라 남는데
        구별이 안 된다.

    🔴 **실행 축이 필수다** (2026-09-11). 이유는 `daily_request_id` 가 적어 둔 그대로다 —
      축이 없으면 판매 승인도 남의 실행 것을 물려받는다.

    🔴 **시각을 넣지 않는다.** 이유는 `daily_request_id` 가 적어 둔 그대로다 —
      넣으면 같은 날 두 번 깨어날 때 id 가 갈리고, 멱등이 인덱스가 아니라
      *"두 번 안 깨우기"* 에 걸리게 된다.
    """
    return build_request_id(
        head=_SALES_REQUEST_HEAD, as_of=as_of, sim_run_id=sim_run_id, tail=item
    )


# ★ `ledger_gap_request_id` 는 여기서 안 짓는다 — **주인이 `run_repository` 다**
#   (2026-09-09 에 옮겼다). 그 키로 관문 행을 **되찾는** 쪽이 저장소이고, 저장소가
#   여기를 import 하면 `scheduler → persistence → run_repository` 와 고리가 된다.
#   이름은 그대로 살려 둔다 — 위 `__all__` 이 내보내고 검사가 이 이름으로 부른다.


# ── ① 결정 — 순수 함수 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ScheduledAction:
    """이번에 깨어나서 **무엇을 할지**. 부작용 없이 만들어진다.

    ★ **품목 내역을 같이 담는다.** `SOME_READY` 를 `RUN_NOW` 로 접어도 어느 품목이
      빠졌는지는 남아야 한다 — 빠진 품목은 그 자리에서 `MISSING → E4` 로 정직하게
      남고, 그 사실을 나중에 세려면 여기 있어야 한다.
    """

    as_of: date
    now: datetime
    action: SchedulerAction
    reason: str
    #: 마감 시각. **답과 함께 낸다** — 왜 `WAIT` 인지가 이 값과의 비교이기 때문이다.
    deadline: datetime
    #: 예측이 온 품목.
    ready_items: tuple[str, ...] = ()
    #: 확인했고 아직 안 온 품목. 🔴 `unreadable_items` 와 섞지 않는다.
    not_yet_items: tuple[str, ...] = ()
    #: 못 물어본 품목.
    unreadable_items: tuple[str, ...] = ()
    #: 다음 깨어남까지. **`WAIT` 일 때만 값이 있다.**
    retry_after: timedelta | None = None

    @property
    def should_run(self) -> bool:
        """이번에 판단을 돌리는가.

        🔴 **`WAIT` · `NOT_A_MARKET_DAY` · `BLOCKED` 는 안 돈다.** 특히 `WAIT` 은
          *"아직"* 이지 *"못"* 이 아니라, 여기서 돌면 `E4` 가 열두 건 쌓인다.
        """
        return self.action in ("RUN_NOW", "RUN_AND_RECORD")


def plan_next_action(
    *,
    now: datetime,
    as_of: date,
    calendar: MarketCalendar,
    gate_result: DayForecastReadiness,
) -> ScheduledAction:
    """이번에 깨어나서 무엇을 할지. **순수 함수다 — 아무것도 안 돌린다.**

    ★ **`now` 를 인자로 받는다.** 그래서 검사가 09:29 · 09:30 · 10:29 · 10:30 ·
      10:35 를 한 스위트 안에서 전부 지날 수 있다. 여기서 시계를 읽으면 검사는
      *"지금 몇 시인가"* 에 답이 끌려가고, CI 가 도는 시각마다 결과가 달라진다.

    ★ **두 축을 다 받는다.** 달력은 물어봐야 알아서 객체로 받고(`CalendarNotCovered`
      가 여기서 튄다), 게이트는 이미 답이 나와 있어 값으로 받는다.

    :param calendar: 개장 축. `is_market_open` 하나만 부른다.
    :param gate_result: `day_forecast_readiness` 의 답. **미리 계산해서 넘긴다** —
        이 함수가 DB 를 타면 순수 함수가 아니게 되고, 검사가 대역을 끼울 자리가
        인자가 아니라 monkeypatch 가 된다.
    """
    if now.tzinfo is None:
        raise ValueError(
            "시간대 없는 시각으로는 마감을 못 잰다 — 어느 지역의 10:30 인지가 없다."
            " clock.seoul_now() 가 주는 값을 그대로 넘겨야 한다"
        )
    deadline = deadline_at(as_of)

    # ── ① 달력 ──────────────────────────────────────────────────────
    #
    # 🔴 **달력을 먼저 본다.** 휴장일이면 예측이 없는 것이 정상이고, 그 날 `NONE_READY`
    #    를 보고 기다리면 한 시간을 헛 깨어난 뒤 `E4` 까지 적는다.
    try:
        is_open = calendar.is_market_open(as_of)
    except CalendarNotCovered as exc:
        # 🔴 **fail-closed.** 달력을 못 읽은 것을 *"장이 선다"* 로도 *"안 선다"* 로도
        #    만들지 않는다. 재시도로 안 풀리므로 `WAIT` 이 아니다.
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="BLOCKED",
            reason=f"개장 달력을 못 읽었다: {exc}",
            deadline=deadline,
        )
    if not is_open:
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="NOT_A_MARKET_DAY",
            reason=f"{as_of.isoformat()} 은 장이 서지 않는다 — 기다릴 예측이 없다",
            deadline=deadline,
        )

    ready = gate_result.ready_items
    not_yet = gate_result.not_yet_items
    unreadable = gate_result.unreadable_items

    # ── ② 게이트 ────────────────────────────────────────────────────
    if gate_result.readiness == "UNREADABLE":
        # 🔴 **`WAIT` 으로 접지 않는다.** DB 가 죽은 날 영원히 재시도하고 그 사실이
        #    아무 데도 안 남는 것이 정확히 이 한 줄에 달려 있다.
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="BLOCKED",
            reason=f"예측 게이트를 못 읽었다: {', '.join(unreadable)}",
            deadline=deadline,
            ready_items=ready,
            not_yet_items=not_yet,
            unreadable_items=unreadable,
        )

    if gate_result.readiness in ("ALL_READY", "SOME_READY"):
        # ★ **`SOME_READY` 도 돈다.** 빠진 품목은 `load_forecast` 가 `MISSING` 을 내고
        #   매입이 `RUNTIME_NOT_READY` → `E4` 로 정직하게 남긴다. 그 품목이 무엇인지는
        #   `not_yet_items` 가 나른다.
        missing_note = f" (빠진 품목: {', '.join(not_yet)})" if not_yet else ""
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="RUN_NOW",
            reason=f"예측이 왔다: {', '.join(ready)}{missing_note}",
            deadline=deadline,
            ready_items=ready,
            not_yet_items=not_yet,
            unreadable_items=unreadable,
        )

    # ── ③ NONE_READY — 마감 하나만 본다 ────────────────────────────
    #
    # ★ **상태를 안 본다.** 몇 번째 깨어남인지, 앞서 몇 번 기다렸는지를 안 세고
    #   `now` 와 마감만 비교한다. 그래서 프로세스가 죽었다 살아나도 답이 같다.
    if now >= deadline:
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="RUN_AND_RECORD",
            reason=(
                f"{_NO_ML_BATCH} — 마감({deadline:%H:%M})까지 예측이 안 와서"
                " 한 번 돌려 E4_NOT_STARTED 로 확정 기록한다"
            ),
            deadline=deadline,
            ready_items=ready,
            not_yet_items=not_yet,
            unreadable_items=unreadable,
        )
    return ScheduledAction(
        as_of=as_of,
        now=now,
        action="WAIT",
        reason=f"아직 예측이 안 왔다 — 마감({deadline:%H:%M}) 전이라 기다린다",
        deadline=deadline,
        ready_items=ready,
        not_yet_items=not_yet,
        unreadable_items=unreadable,
        retry_after=SCHEDULE_INTERVAL,
    )


# ── ② 실행 — 답을 따르기만 한다 ────────────────────────────────────────


@dataclass(frozen=True)
class ItemRunOutcome:
    """품목 하나의 실행 결과. **터진 것도 값으로 남는다.**"""

    item: str
    request_id: str
    #: `RAN` 은 판단이 끝까지 돌았다는 뜻이다. **좋은 답이었다는 뜻이 아니다** —
    #: `E4_NOT_STARTED` 도 `RAN` 이다. 못 돈 것은 `FAILED` 다.
    status: Literal["RAN", "FAILED"]
    #: 그 실행의 종료 코드. **못 돌았으면 `None`** — 코드가 없었다는 뜻이다.
    end_code: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class DayRunOutcome:
    """하루 실행 한 번의 결과. **어느 단계에서 무엇이 됐는지가 다 남는다.**

    ⚠️ 단계 상태의 `NOT_ATTEMPTED` 는 *"안 했다"* 다. `FAILED` 와 섞지 않는다 —
      개장이 막혀 안 한 것과 해 보고 터진 것은 다른 사실이다 (`DayOpenOut` 의
      `collection_seed_status` 와 같은 어휘).
    """

    as_of: date
    action: SchedulerAction
    reason: str
    day_open_status: str = "NOT_ATTEMPTED"
    #: 물류 유지보수 단계 (2026-09-11). 🔴 **개장 바로 뒤다 — 그날 자리를 비운다.**
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `RAN` · `NOTHING_DUE` · `FAILED` 는
    #:   `MaintenanceOut.status` 그대로이고 (`closing_status` 가 `ClosingOut.status`
    #:   를 그대로 싣는 것과 같다), 단계를 안 탄 날은 이 클래스가 이미 쓰는
    #:   `NOT_ATTEMPTED` 다.
    #:
    #: ```text
    #: NOT_ATTEMPTED   안 켰다 — auto_maintain 이 거짓이었다 · 거기까지 못 갔다
    #: RAN             손댔거나 일부러 건너뛴 Lot 이 있었다 — 전부 성공이 아니다
    #: NOTHING_DUE     확인했고 할 것이 없었다 — 🟢 정상이다
    #: FAILED          하려다 터졌다 — 🔴 **그래도 하루는 계속 간다**
    #: ```
    #:
    #: 🔴 **`NOT_ATTEMPTED` 와 `NOTHING_DUE` 를 접지 않는다.** 앞은 *"안 켰다"* 이고
    #:   뒤는 *"켰는데 버릴 것이 없었다"* 다 — 폐기 0건의 이유가 그 둘로 갈린다.
    maintenance_status: str = "NOT_ATTEMPTED"
    #: 미적용 전이 재시도 단계 (2026-09-11). 🔴 **유지보수 뒤 · 입고 앞이다.**
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `RAN` · `NOTHING_DUE` · `FAILED` 는
    #:   `RetryStatus` 그대로이고 (`closing_status` 가 `ClosingOut.status` 를 그대로
    #:   싣는 것과 같다), 단계를 안 탄 날은 이 클래스가 이미 쓰는 `NOT_ATTEMPTED` 다.
    #:
    #: ```text
    #: NOT_ATTEMPTED   거기까지 못 갔다 — WAIT · 휴장 · 개장 실패
    #: RAN             미적용을 찾아 다시 세웠다 — 전부 닿았다는 뜻이 아니다
    #: NOTHING_DUE     확인했고 미적용이 없었다 — 🟢 정상이다
    #: FAILED          찾다가 터졌다 — 🔴 **그래도 하루는 계속 간다**
    #: ```
    pending_transition_status: str = "NOT_ATTEMPTED"
    inbound_status: str = "NOT_ATTEMPTED"
    #: 채권 발행 단계. 🔴 **수금보다 앞이다** — 채권이 서야 수금할 것이 있다.
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `ReceivableOut.status` 의 다섯 값을 그대로
    #:   싣고, 단계를 안 탄 날은 이 클래스가 이미 쓰는 `NOT_ATTEMPTED` 다.
    receivable_status: str = "NOT_ATTEMPTED"
    collection_status: str = "NOT_ATTEMPTED"
    #: 판단 단계를 **탔는가**. 🔴 좋은 답이 나왔다는 뜻이 아니다 — 품목별 결과는
    #: `items` 가 나른다 (`ItemRunOutcome.status` 와 같은 어휘를 쓴다).
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `NOT_ATTEMPTED` 는 이 클래스가 이미 세 단계에
    #:   쓰는 말이고 `RAN` 은 `ItemRunOutcome` 이 이미 쓰는 말이다. 단계를 안 탄
    #:   사실을 `items == ()` 으로만 두면 *"품목 목록이 비었다"* 와 구별이 안 된다.
    procurement_status: str = "NOT_ATTEMPTED"
    #: 판매 판단 단계를 **탔는가** (2026-09-10). 🔴 **매입 뒤 · 출고 앞이다.**
    #:
    #: ★ **어휘를 새로 만들지 않았다.** 셋 다 이 클래스가 이미 쓰는 말이다.
    #:
    #: ```text
    #: NOT_ATTEMPTED   안 했다 — 게이트가 WAIT 였다 · 관문이 막았다 · 품목이 없었다
    #: RAN             돌았다 — 좋은 답이었다는 뜻이 아니다
    #: FAILED          해 보고 터졌다 — 돈 품목이 **하나도** 없다
    #: ```
    #:
    #: 🔴 **`FAILED` 는 「전부 터졌다」다.** 한 품목이 터진 날은 `RAN` 이고, 터진
    #:   품목은 `sales_items` 에 `FAILED` 로 남는다 — 매입 루프와 같은 규율이다.
    sales_status: str = "NOT_ATTEMPTED"
    #: 출고 단계를 **탔는가**. 🔴 `procurement_status` 와 **같은 모양·같은 어휘**다
    #: (`RAN` · `NOTHING_DUE` · `FAILED` · `NOT_ATTEMPTED`).
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `NOTHING_DUE` 는 입고·수금이 이미 쓰는 말이고
    #:   나머지 셋은 이 클래스가 이미 쓴다. 판매 품목별 결과는 `OutboundOut.items` 가
    #:   나르고, 여기 다시 담지 않는다 — 같은 사실의 주인은 하나다.
    outbound_status: str = "NOT_ATTEMPTED"
    #: 마감 단계. 🔴 **하루의 맨 끝이다 — 출고 뒤다.**
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `ClosingOut.status` 의 다섯 값
    #: (`CLOSED` · `NOTHING_DUE` · `BLOCKED` · `NOT_OPENED` · `FAILED`)을 그대로
    #: 싣고, 단계를 안 탄 날은 이 클래스가 이미 쓰는 `NOT_ATTEMPTED` 다.
    #:
    #: 🔴 **`NOT_ATTEMPTED` 와 `BLOCKED` 를 접지 않는다.** 앞은 *"그날을 아예 안
    #: 돌았다"* (휴장·`WAIT`·개장 실패)이고 뒤는 *"돌았는데 장부가 안 서서 못
    #: 닫았다"* 이다. 손익 곡선에는 둘 다 빈 칸으로 보이므로, **이 값이 아니면
    #: 둘을 가를 데가 없다.**
    closing_status: str = "NOT_ATTEMPTED"
    items: tuple[ItemRunOutcome, ...] = ()
    #: 판매 판단의 품목별 결과 (2026-09-10). 🔴 **`items` 와 섞지 않는다.**
    #:
    #: ★ **모양은 같고 축이 다르다.** `ItemRunOutcome` 을 그대로 쓰되 한 칸에 담지
    #:   않는다 — 섞으면 `failed_items` 가 *"어느 사이클이 터졌나"* 를 못 말하고,
    #:   같은 품목이 두 번 앉아 품목 수를 세는 모든 자리가 두 배로 읽힌다.
    #:
    #: ★ `end_code` 는 판매 어휘 그대로다 (`SL1_PRESENTED` 등). 🔴 **매입 어휘로
    #:   접지 않는다** — `backtest_runner` 가 `end_codes` 를 세는 자리와 같은 규율이다.
    sales_items: tuple[ItemRunOutcome, ...] = ()
    #: 매입 판단 **바로 뒤** 자동 승인 단계를 **탔는가** (2026-09-11).
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `NOT_ATTEMPTED` · `FAILED` 는 이 클래스가
    #:   이미 쓰는 말이고, `RAN` · `NO_RULE` 은 `BackfillOut.status` 의 두 값
    #:   그대로다 (`closing_status` 가 `ClosingOut.status` 를 그대로 싣는 것과 같다).
    #:
    #: ```text
    #: NOT_ATTEMPTED   안 켰다 — auto_approve 가 거짓이었다 · 거기까지 못 갔다
    #: RAN             승인 문까지 돌았다 — 몇 건이 적혔는지는 outcomes 가 말한다
    #: NO_RULE         켰는데 그 실행이 규칙을 안 들었다
    #: FAILED          돌리다 터졌다 — 🔴 **그래도 하루는 계속 간다**
    #: ```
    #:
    #: 🔴 **`NOT_ATTEMPTED` 와 `NO_RULE` 을 접지 않는다.** 앞은 *"안 켰다"* 이고
    #:   뒤는 *"켰는데 규칙이 없었다"* 다 — 승인 0건의 이유가 그 둘로 갈린다.
    procurement_approval_status: str = "NOT_ATTEMPTED"
    #: 판매 판단 바로 뒤 자동 승인 단계. 🔴 **매입과 같은 모양·같은 어휘다.**
    sales_approval_status: str = "NOT_ATTEMPTED"
    #: 매입 승인이 낸 값 그대로. 🔴 **여기서 다시 세지 않는다** — 어휘 여덟의
    #: 주인은 `BackfillOut.outcomes` 하나다. 안 켠 날은 `None`.
    #: 미적용 전이 재시도가 낸 값 그대로 (2026-09-11). 🔴 **여기서 다시 세지 않는다** —
    #: 어휘 넷의 주인은 `RetryOut.outcomes` 하나다. 단계를 안 탄 날은 `None`.
    pending_transition: RetryOut | None = None
    #: 유지보수가 낸 값 그대로 (2026-09-11). 🔴 **접지 않는다** — 몇 Lot 을 봤고
    #: 무엇을 버렸고 무엇을 사람에게 남겼는지의 주인은 `AutoMaintenanceResult` 다.
    #: 안 켠 날은 `None`.
    maintenance: MaintenanceOut | None = None
    procurement_approval: BackfillOut | None = None
    #: 판매 승인이 낸 값 그대로. ⚠️ **매입 것과 한 칸에 안 담는다** — 섞으면
    #: 어느 사이클의 승인이 안 섰는지를 요약이 못 말한다 (`items` 와 `sales_items`
    #: 를 가른 것과 같은 이유).
    sales_approval: BackfillOut | None = None
    #: 단계별 사유. 사람이 읽을 자리다.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed_items(self) -> tuple[str, ...]:
        """터진 품목. **나머지는 계속 돌았다.**

        ⚠️ **매입 축이다.** 판매가 터진 품목은 `failed_sales_items` 가 나른다 —
          한 property 로 합치면 어느 사이클이 터졌는지가 사라진다.
        """
        return tuple(one.item for one in self.items if one.status == "FAILED")

    @property
    def failed_sales_items(self) -> tuple[str, ...]:
        """판매 판단이 터진 품목. **나머지는 계속 돌았다.**"""
        return tuple(one.item for one in self.sales_items if one.status == "FAILED")


def run_scheduled_day(
    action: ScheduledAction,
    *,
    policy_version: str = DAILY_POLICY_VERSION,
    open_day_fn: Callable[..., Any] = open_day,
    maintain_fn: Callable[..., MaintenanceOut] = run_auto_maintenance,
    retry_fn: Callable[..., RetryOut] = retry_pending_transitions,
    receive_fn: Callable[..., Any] = receive_arrivals,
    issue_fn: Callable[..., Any] = issue_receivables,
    collect_fn: Callable[..., Any] = collect_receipts,
    procure_fn: Callable[..., Any] = run_procurement,
    sales_fn: Callable[..., Any] = run_sales,
    outbound_fn: Callable[..., Any] = ship_due_sales,
    close_fn: Callable[..., Any] = close_day,
    sim_run_id: str = BURN_IN_SIM_RUN_ID,
    items: Sequence[str] | None = None,
    auto_approve: bool = False,
    approve_fn: Callable[..., BackfillOut] = backfill_decisions,
    sales_terms: SalesTermsRule | None = None,
    auto_maintain: bool = False,
) -> DayRunOutcome:
    """결정을 따른다. **여기에는 판단이 없다.**

    ```text
    개장 → 물류 유지보수 → 미적용 전이 재시도 → 입고 → 채권 → 수금 → [장부 관문]
         → 매입 판단 → 매입 승인 → 판매 판단 → 판매 승인 → 출고 → 마감
    ```

    🔴 **물류 유지보수가 개장 바로 뒤다** (2026-09-11).

      ★★ **창고가 차서 매입이 1월 12일부터 멈추던 자리다.** 폐기도 자리 반환도
        `logistics.auto_maintenance` 에 이미 있었고 **부르는 자리 하나**가 없었다.

      ★ **왜 개장 바로 뒤인가.** 그날 자리를 비워야 **그날 입고와 그날 매입 판단**이
        들어갈 자리가 생긴다. 뒤로 가면 하루씩 밀린다.

      🔴 **기본이 꺼짐이다** (`auto_maintain` 참고). 승인보다 더 조심할 자리다 —
        폐기는 되돌릴 경로가 없다.

      🔴 **터져도 하루는 계속 간다.** `_stage` 와 같은 태도다.

    🔴 **미적용 전이 재시도가 유지보수 뒤 · 입고 앞이다** (2026-09-11).

      ★★ **승인 15건이 원장에 한 건도 안 닿던 자리다.** 승인일이 `D` 면 상태가 설
        날은 `D+1` 이고 그 행은 다음 차례에 열린다 — 전이는 늘 하루 앞을 본다.
        도착일이 열린 날 다시 세우면 닿는다 (`pending_transition.py`).

      ★ **왜 입고 앞인가.** 그날 도착 행이 방금 열렸고 **입고가 그 도착을 잡아야**
        한다. 뒤로 가면 그날 도착이 하루 더 밀린다.

      🔴 **또 실패해도 하루는 계속 간다.** 세어서 요약에 올리는 것이 전부다.

    🔴 **채권이 수금보다 앞이다.** 채권이 서야 수금할 것이 있다. 지금 데이터는
      결제조건이 30일이라 같은 날 수금될 일이 없지만, **순서가 계약**이다.

    🔴 **출고가 판단 뒤이고 관문 뒤다.** 오늘 산 것은 오늘 안 나가고(도착이 며칠
      뒤다), 장부가 안 선 날에 물건을 내보내면 재고가 두 번 틀린다. 관문에서
      돌아서면 `outbound_status` 는 `NOT_ATTEMPTED` 로 남는다.

    🔴 **판매 판단은 매입 뒤 · 출고 앞이다** (2026-09-10).

      ★ **왜 출고 앞인가.** `ship_due_sales` 는 **이미 확정된 판매를 내보내는 것**이지
        판매 안을 내는 것이 아니다. 판매 판단을 출고 뒤에 두면 그날 확정된 안이
        **다음 날에야** 나갈 자리가 생기고, 순서를 읽는 사람이 *"판매가 왜 출고 뒤에
        서나"* 를 매번 물어야 한다.

      ★ **왜 매입 뒤인가.** 판매가 재고를 보고 안을 낸다. 매입은 오늘 사도 며칠 뒤
        도착이라 오늘 재고를 안 움직이지만, 둘의 순서를 매입-판매로 두면 *"하루의
        판단"* 이 한 덩어리로 읽히고 그 사이에 아무 단계도 안 끼어든다.

      🔴 **예측 게이트 **안**이다.** `WAIT` 인 날은 판매도 안 돈다 — `should_run`
        한 줄이 매입과 판매를 같이 막는다. `WAIT` 중에 판매를 부르면 매입이 피한
        그 문제(열두 번 깨어나며 미완 실행이 열두 건 쌓인다)를 판매가 그대로 다시 짓는다.

      ⚠️ **거래처를 안 고른다.** `partner_id` 도 `user_request` 도 안 싣는다.
        영업 모드는 `WALK_BUSINESS_MODE` 하나이고, 그 값의 뜻은 거기 적혀 있다.

    🔴 **승인은 각 판단 「바로 뒤」다. 끝에 몰지 않는다** (2026-09-11).

      ```text
      매입 판단 → **매입 승인** → 판매 판단 → **판매 승인**
      ```

      ★★ **판매가 그날의 매입 결과를 봐야 한다.** 둘을 끝에 몰면 판매 판단이 도는
        시점에 그날 매입은 아직 승인 전이고, 그러면 다음 날이 **어제 산 것을 못
        본다** — 179일을 걸어도 재고가 영영 안 쌓인다.

      🔴 **기본이 꺼짐이다** (`auto_approve` 참고). 설정에 규칙이 있다고 켜지지
        않는다 — *"있으니까 한다"* 는 암묵 스위치이고, 그러면 설정을 실험하려고
        넣은 사람이 승인까지 하게 된다.

      🔴 **새 승인 경로를 만들지 않는다.** `backfill.backfill_decisions` 를 그대로
        부르고 그것이 `record_decision` 을 부른다 — 여기서 `record_decision` 을
        직접 부르면 그 순간 **「승인」이 두 종류**가 되고, 경계 가드
        (`BACKFILL_BOUNDARY_AS_OF`)도 규칙도 이 자리만 안 지나게 된다.

    :param auto_approve: 🔴 **기본이 거짓이다. 거짓이면 승인 함수가 이름조차 안
        불린다.** 켜는 것은 **명시로만** — `--auto-approve` 를 준 걷기 하나다.
    :param approve_fn: 🔴 **승인 문.** 기본이 `backfill_decisions` 자체다 —
        `None` 을 안 받는다 (`run_day_fn` · `verifier` 와 같은 규율).
        `auto_approve` 가 거짓이면 이 값은 **한 번도 안 쓰인다.**
    :param sales_terms: 판매 요청에 실을 상업 조건. 🔴 **읽지 않고 받는다** —
        기본이 `None` 이고 그 뜻은 *"아무것도 안 싣는다"* 이다.
    :param auto_maintain: 🔴 **기본이 거짓이다. 거짓이면 유지보수 함수가 이름조차
        안 불린다.** 켜는 것은 **명시로만** — `--auto-maintain` 을 준 걷기 하나다.

        ★★ **승인보다 더 조심할 자리다.** 승인은 append-only 표에 한 줄이 남고,
          폐기는 **물건이 없어진다** — 물류가 *"되돌릴 경로가 없다(`ADJUST_IN`
          없음 · 실사 제외)"* 고 못박았다.
    :param maintain_fn: 🔴 **물류 경계.** 기본이 `run_auto_maintenance` 자체다 —
        `None` 을 안 받는다. `auto_maintain` 이 거짓이면 **한 번도 안 쓰인다.**

    🔴 **판매 상업 조건은 규칙 파일이 말한다** (2026-09-11 · 걷기 실측).

      ★★ **자동 걷기에는 사람이 없다.** 수량 하나만 물류에서 오고 거래처·지급조건·
        단가는 아무도 안 정해서, 재무가 `SALES_INPUT_INCOMPLETE` 로 판정을 못 냈다.

      🔴 **하루가 설정을 제 손으로 읽지 않는다.** 읽는 자리는 `wake_up` 과
        `backtest_runner.walk` 이고 (`sales_terms.read_run_sales_terms`), 실행당
        한 번이다. 하루가 읽으면 이 함수를 부르는 모든 검사가 조용히 실 DB 를
        치고, 규칙은 날이 아니라 실행에 속하므로 179번 다시 읽을 이유도 없다.

      🔴 **코드에 기본값을 두지 않는다.** 규칙 파일에 `sales_terms` 가 없으면
        **종전 그대로 아무것도 안 싣는다** — 두면 규칙을 안 적은 사람도 모르는
        거래처에 팔게 된다 (`sales_terms.apply_sales_terms`).

    🔴 **`should_run` 이 아니면 아무것도 안 부른다.** `WAIT` 중에 판단을 돌리면
      `E4_NOT_STARTED` 가 열두 건 쌓인다 — 이 한 줄이 그것을 막는다.

    🔴 **개장이 실패하면 그 뒤를 안 한다.** 상태 행이 없으면 입고 · 수금 · 판단이
      적을 자리가 없고, 그런데도 부르면 `NOTHING_DUE` 가 나가 *"오늘은 올 게
      없었다"* 로 읽힌다.

    🔴 **판단이 실패해도 개장을 되돌리지 않는다.** 하루가 열린 것은 사실이고, 판단이
      실패한 것은 별개 사실이다 (`#400` 에서 같은 판단을 했다). 되돌리면 다음 날이
      *"어제가 안 열렸다"* 위에 서고, 그게 판단 실패보다 크다.

    🔴 **한 품목이 터져도 나머지는 계속 돈다.** 배추가 터졌다고 무와 양파를 안 돌면
      하루가 통째로 빈다. 터진 것은 `items` 에 `FAILED` 로 남는다.

    🔴 **입고 · 채권 · 수금이 `BLOCKED` · `FAILED` 면 판단을 안 돌린다.** `inbound.py` 가
      *"부르는 쪽이 정한다"* 로 넘겨 둔 답을 이 파일이 낸다 (모듈 docstring 에 원문을
      인용해 뒀다). 장부가 실제보다 적은 채로 판단하면 **과매입이 나는데 에러는 안
      난다** — 재고가 적게 보이면 더 사고, 현금이 적게 보이면 `projected_cash_min`
      이 틀린다.

      ★ **조용히 건너뛰지 않는다.** 무엇이 막았는지가 `notes` 에 남고, 판단 단계를
        안 탔다는 사실은 `procurement_status` 가 든다.

      🔴 **`NOTHING_DUE` 는 막지 않는다.** *"확인했고 낼 것이 없다"* 는 정상이고,
        막으면 대부분의 날이 멈춘다.

    🔴 **마감이 맨 끝이다. 마감이 터져도 그날 결과를 안 바꾼다.**

      ```text
      관문이 통과한 날   출고 뒤에 닫는다                → CLOSED · NOTHING_DUE · FAILED
      관문이 막은 날     닫지 않고 **BLOCKED 로 적는다**  → 어댑터를 부르지 않는다
      단계를 안 탄 날    NOT_ATTEMPTED                   → 휴장 · WAIT · 개장 실패
      ```

      ★ **관문이 막은 날에 `NOT_ATTEMPTED` 로 두면** *«마감이 없다»* 와 *«마감이
        막혔다»* 가 같아 보인다. 손익 곡선에는 둘 다 빈 칸이라, 이 값이 아니면
        가를 데가 없다.

      🔴 **마감 결과를 보고 무엇을 되돌리지 않는다.** 판단도 출고도 이미 끝났고,
        그것은 사실이다 — `try_save_run` 이 `try_` 인 이유와 같다.

    :param items: 돌 품목. 안 주면 `scheduled_items()` — **목록을 다시 세지 않는다.**
    :param sim_run_id: 어느 실행의 장부인가. 🔴 **인자로 받아 흘린다** — 마감이
        `daily_closings` 의 PK 절반으로 쓴다. 기본값의 주인은
        `ledger_repository.BURN_IN_SIM_RUN_ID` 하나이고, 관문 행이 싣는 값과 같다.
    """
    if not action.should_run:
        # 🔴 여기서 돌아선다. **서비스 함수를 하나도 안 부른다.**
        return DayRunOutcome(as_of=action.as_of, action=action.action, reason=action.reason)

    as_of = action.as_of
    notes: list[str] = []

    # ── 개장 ────────────────────────────────────────────────────────
    #
    # 🔴 **네 단계에 축을 실어 준다** (`#531` 후속 · 2026-09-10). 등록소가 프로세스
    #    시작 때 든 상수를 쓰면 **매입 원장만 새 실행에 앉고 물류 장부·재무 수금은
    #    번인에 남는다** — 아무 오류도 안 나는 갈림이라 `ledger.py` 가 경고해 둔
    #    그 모양 그대로다. 마감이 이미 `sim_run_id` 를 받는 것과 같은 자리다.
    try:
        opened = open_day_fn(as_of, sim_run_id=sim_run_id)
    except Exception as exc:  # noqa: BLE001 - 개장 실패가 500 으로 올라가면 안 된다.
        return DayRunOutcome(
            as_of=as_of,
            action=action.action,
            reason=action.reason,
            day_open_status="FAILED",
            notes=(f"개장이 터졌다: {type(exc).__name__}: {exc}",),
        )
    day_open_status = str(getattr(opened, "status", "FAILED"))
    if day_open_status not in ("OPENED", "ALREADY_OPENED"):
        # ★ **`ALREADY_OPENED` 는 통과다** — *"할 일이 없었다"* 이지 *"못 했다"* 가
        #   아니다. 같은 날 두 번 깨어나면 두 번째가 여기로 온다.
        return DayRunOutcome(
            as_of=as_of,
            action=action.action,
            reason=action.reason,
            day_open_status=day_open_status,
            notes=(f"하루가 안 열려서 뒤를 안 한다: {getattr(opened, 'reason', '')}",),
        )

    # ── 물류 유지보수 — 🔴 **개장 바로 뒤** (2026-09-11) ─────────────
    #
    # ★★ **여기가 없어서 매입이 1월 12일부터 멈췄다.** 폐기 경로도 자리 반환도
    #   `logistics.auto_maintenance` 에 이미 있었고, **부르는 자리 하나**가 없었다 —
    #   걷기 실행 넷의 Lot 이 전부 `ACTIVE` 였다 (실측 2026-09-11).
    #
    # 🔴 **개장 바로 뒤여야 한다.** 그날 자리를 비워야 **그날 입고와 그날 매입
    #    판단**이 들어갈 자리가 생긴다. 뒤로 가면 비운 자리를 그날이 못 쓴다.
    #
    # 🔴 **`auto_maintain` 이 거짓이면 이 블록이 통째로 안 돈다.**
    #
    # 🔴 **터져도 하루는 계속 간다.** `_stage` 와 같은 태도다.
    maintenance_status, maintenance, note = _maintain(
        as_of=as_of,
        sim_run_id=sim_run_id,
        # 🔴 **걷기의 시간축을 그대로 넘긴다. 벽시계를 여기서 안 읽는다.**
        #    `action.now` 는 `backtest_runner --now` 가 그날에 붙여 준 값이고
        #    (`_moment_on`), `wake_up` 에서는 진입점이 한 번 읽은 그 값이다 —
        #    시계를 읽는 자리는 여전히 하나다.
        occurred_at=action.now,
        maintain_fn=maintain_fn,
        enabled=auto_maintain,
    )
    if note is not None:
        notes.append(note)

    # ── 미적용 전이 재시도 — 🔴 **유지보수 뒤 · 입고 앞** (2026-09-11) ──
    #
    # ★★ **여기가 없어서 걷기 아흐레에 승인 15건이 원장에 0건이었다.** 전이 로직은
    #   `transition.apply_approval` 에 이미 있었고, **다시 부르는 자리 하나**가
    #   없었다 (판매 판단·매입 승인 때와 같은 모양이다).
    #
    # 🔴 **개장 뒤여야 한다.** 그날 도착 행을 세우는 것이 개장이다 — 앞에 두면
    #    재시도가 어제와 똑같이 *"갱신할 물류 runtime fixture 행이 없다"* 로 터진다.
    #
    # 🔴 **입고 앞이어야 한다.** 방금 선 도착 예정을 **그날 입고가 잡아야** 한다 —
    #    뒤로 밀면 그날 도착이 하루 더 밀리고, 그 하루가 날마다 쌓인다.
    #
    # 🔴 **터져도 하루는 계속 간다.** `_stage` 와 같은 태도다.
    pending_transition_status, pending_transition, note = _retry_pending(
        as_of=as_of, sim_run_id=sim_run_id, retry_fn=retry_fn
    )
    notes.append(note)

    # ── 입고 ────────────────────────────────────────────────────────
    inbound_status, note = _stage("입고", lambda: receive_fn(as_of, sim_run_id=sim_run_id))
    notes.append(note)

    # ── 채권 — 🔴 **수금보다 앞이다** ───────────────────────────────
    #
    # ★ 채권이 서야 수금할 것이 있다. 순서를 뒤집으면 같은 날 발생·수금되는 계약이
    #   생기는 순간 **수금할 채권이 아직 없는 상태**에서 수금이 돈다.
    receivable_status, note = _stage("채권", lambda: issue_fn(as_of, sim_run_id=sim_run_id))
    notes.append(note)

    # ── 수금 ────────────────────────────────────────────────────────
    collection_status, note = _stage("수금", lambda: collect_fn(as_of, sim_run_id=sim_run_id))
    notes.append(note)

    # ── 장부 관문 — 개장과 판단 **사이** ────────────────────────────
    #
    # 🔴 여기서 돌아서면 `procure_fn` 을 **한 번도 안 부른다.** 개장은 그대로 둔다 —
    #    하루가 열린 것은 사실이고, 판단을 안 돌린 것은 별개 사실이다.
    if _ledger_gap(inbound_status, receivable_status, collection_status):
        gap_reason = _ledger_gap_note(inbound_status, receivable_status, collection_status)
        notes.append(gap_reason)
        # ── 🔴 **행 하나를 남긴다. 판단은 여전히 안 돌린다** (2026-09-09) ──
        #
        # ★ **왜 남기나.** 안 남기면 이 날이 화면에서 *"사유를 남긴 실행이
        #   없습니다"* 로 보이고, 그 문구는 **안 돌린 날**과 **적재가 실패한 날**도
        #   똑같이 낸다. 관문이 막은 것은 정상 동작이고 나머지 둘은 아니다.
        #
        # ★ **문장을 다시 짓지 않는다.** 위에서 만든 `gap_reason` 을 그대로 넘긴다 —
        #   두 벌이 되면 한쪽만 고치는 날 화면과 이력이 갈린다.
        #
        # 🔴 **여기서도 `procure_fn` 은 안 부른다.** 남기는 것은 행이지 판단이 아니다.
        #
        # 🔴 **실행 축을 같이 싣는다** (2026-09-09). 인자만 있고 값을 안 주면 이 행의
        #    `sim_run_id` 가 늘 NULL 로 앉는다. 그러면 같은 날 판단 행은 축이 있고 게이트
        #    행만 없어서, **모두가 쓰는 축으로 훑을 때 막힌 날이 도로 안 보인다** — 이
        #    판이 존재하는 이유가 바로 그것이라 그 자리에서 무너진다.
        #
        # ★ **값의 주인은 `ledger_repository.BURN_IN_SIM_RUN_ID` 하나다.** 여기서 문자열을
        #   다시 적거나 새 상수를 만들지 않는다 — `service.run_procurement` 가 같은 날
        #   판단 행에 싣는 값도 그 상수이고, 두 벌이 되면 한쪽만 고치는 날 두 행이 갈린다.
        try:
            persistence.record_ledger_gap(
                request_id=ledger_gap_request_id(as_of, sim_run_id=sim_run_id),
                as_of=as_of,
                policy_version=policy_version,
                reason=gap_reason,
                inbound_status=inbound_status,
                receivable_status=receivable_status,
                collection_status=collection_status,
                sim_run_id=sim_run_id,
            )
        except Exception:  # 이력 때문에 걷기가 멈추면 안 된다.
            # ★ `try_save_run` 이 이미 삼키지만 여기서 한 번 더 잡는다 — 그날 결과가
            #   적재의 약속에 걸리면 안 된다 (`_stage` 와 같은 태도).
            logger.exception("장부 관문 행 적재가 터졌다 - 그날 결과는 그대로 나간다")
        # ── 마감 — 🔴 **닫지 않는다. 막혔다고 적는다** (2026-09-10) ──────
        #
        # ★ **왜 그래도 부르나.** 안 부르면 이 날의 `closing_status` 가
        #   `NOT_ATTEMPTED` 로 남고, 그것은 **휴장일·`WAIT`·개장 실패와 같은 값**이다.
        #   손익 곡선에는 넷 다 빈 칸이라 이 값이 아니면 가를 데가 없다.
        #
        # 🔴 **어댑터는 안 불린다.** `close_day` 가 `ledger_gap` 을 받으면 그 자리에서
        #    `BLOCKED` 로 돌아선다 — 장부가 실제보다 적은 채로 그날을 닫으면
        #    **그 틀린 숫자가 손익 곡선의 확정값으로 앉는다.**
        #
        # ★ **사유를 다시 짓지 않는다.** 위에서 만든 `gap_reason` 을 그대로 넘긴다 —
        #   관문 행에 적은 문장과 같아야 화면과 이력이 안 갈린다.
        closing_status, note = _stage(
            "마감", lambda: close_fn(as_of, sim_run_id=sim_run_id, ledger_gap=gap_reason)
        )
        notes.append(note)
        return DayRunOutcome(
            as_of=as_of,
            action=action.action,
            reason=action.reason,
            day_open_status=day_open_status,
            # 🔴 **관문이 막아도 유지보수와 재시도는 이미 돌았다.** 그 사실을 여기서
            #    지우지 않는다 — 지우면 *"안 했다"* 와 *"했는데 관문에서 돌아섰다"*
            #    가 같아진다. 폐기는 되돌릴 경로가 없으므로 특히 그렇다.
            maintenance_status=maintenance_status,
            maintenance=maintenance,
            pending_transition_status=pending_transition_status,
            pending_transition=pending_transition,
            inbound_status=inbound_status,
            receivable_status=receivable_status,
            collection_status=collection_status,
            closing_status=closing_status,
            notes=tuple(notes),
        )

    # ── 판단 ────────────────────────────────────────────────────────
    #
    # ★ **품목 축을 한 번만 정한다.** 매입과 판매가 같은 튜플을 돈다 — 각자 세면
    #   두 사이클의 품목 축이 갈리고, 그 날 무엇을 팔 수 있었나가 무엇을 샀나와
    #   다른 목록 위에 서게 된다.
    day_items = scheduled_items() if items is None else tuple(items)

    results: list[ItemRunOutcome] = []
    for item in day_items:
        request_id = daily_request_id(as_of, item, sim_run_id=sim_run_id)
        try:
            response = procure_fn(
                ProcurementRunRequest(
                    as_of=as_of,
                    policy_version=policy_version,
                    request_id=request_id,
                    item=item,
                    # 🔴 **걷기가 받은 축을 봉투까지 잇는다** (`#531` 후속 · 2026-09-10).
                    #    안 실으면 `run_procurement` 이 번인 상수로 떨어지고, 그 판단
                    #    행을 승인이 읽으니 **원장까지 번인으로 돌아온다.** 마감은
                    #    `sim_run_id` 를 받는데 판단만 안 받아서, 같은 날 두 행이 서로
                    #    다른 실행에 앉았다.
                    sim_run_id=sim_run_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 한 품목이 하루를 세우면 안 된다.
            results.append(
                ItemRunOutcome(
                    item=item,
                    request_id=request_id,
                    status="FAILED",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        results.append(
            ItemRunOutcome(
                item=item,
                request_id=request_id,
                status="RAN",
                end_code=str(getattr(response, "end_code", "")) or None,
            )
        )

    # ── 매입 승인 — 🔴 **매입 판단 바로 뒤. 판매 판단 앞** (2026-09-11) ──
    #
    # ★★ **여기가 없어서 걷기 206일에 승인이 0건이었다.** 승인 로직은 `backfill.py`
    #   에 이미 있었고, **부르는 자리 하나**가 없었다 (판매 판단 때와 같은 모양이다).
    #
    # 🔴 **끝으로 밀지 않는다.** 여기서 승인이 서야 판매 판단이 그날 매입을 보고,
    #    다음 날이 어제 산 것 위에 선다.
    #
    # 🔴 **`auto_approve` 가 거짓이면 이 블록이 통째로 안 돈다.**
    procurement_approval_status, procurement_approval, note = _approve(
        "매입 승인",
        as_of=as_of,
        sim_run_id=sim_run_id,
        request_ids=[one.request_id for one in results],
        approve_fn=approve_fn,
        enabled=auto_approve,
    )
    if note is not None:
        notes.append(note)

    # ── 판매 판단 — 🔴 **매입 뒤 · 출고 앞** (2026-09-10) ───────────
    #
    # ★★ **여기가 없어서 걷기 179일에 판매 판단이 0건이었다.** 판매 판단 로직은
    #   `service.run_sales` 에 이미 있었고, **부르는 자리 하나**가 없었다.
    #
    # 🔴 **`ship_due_sales` 가 이 자리를 대신하지 못한다.** 그것은 이미 확정된 판매를
    #    내보내는 단계다 — 이름 때문에 판매가 서 있는 것처럼 보였을 뿐이다.
    #
    # 🔴 **request_id 가 매입 것과 다르다.** 같으면 유일 인덱스가 두 번째 사이클을
    #    막는다 (`daily_sales_request_id` 가 그 이유를 적는다).
    #
    # 🔴 **한 품목이 터져도 하루를 안 세운다.** 매입 루프와 같은 모양이다 — 터진
    #    것은 `sales_items` 에 `FAILED` 로 남고 출고·마감은 그대로 돈다.
    sales_results: list[ItemRunOutcome] = []
    for item in day_items:
        sales_request_id = daily_sales_request_id(as_of, item, sim_run_id=sim_run_id)
        try:
            # ★ `budget` 과 `verifier` 를 안 준다. 판매 기본값 25 가 계약이고
            #   (매입 12 를 복사하면 요청이 골격의 `SALES_BUDGET` 을 이긴다),
            #   `verifier` 를 안 주면 기본 검증 Tool 이 붙는다 — 매입과 같은 규율이다.
            #
            # 🔴 **상업 조건을 여기서 적지 않는다.** 거래처도 지급조건도 단가도
            #    `apply_sales_terms` 가 **규칙 파일이 말한 대로만** 얹는다 —
            #    규칙이 없으면 요청이 그대로 지나가고 종전과 한 글자도 안 다르다.
            sales_response = sales_fn(
                apply_sales_terms(
                    SalesRunRequest(
                        as_of=as_of,
                        policy_version=policy_version,
                        request_id=sales_request_id,
                        item=item,
                        business_mode=WALK_BUSINESS_MODE,
                        # 🔴 **매입과 같은 축이다.** 여기만 빠지면 같은 날 매입 판단은
                        #    걷기 축에, 판매 판단은 번인에 앉는다 — 그러면 판매가 읽는
                        #    매입 경계(`_procurement_boundary`)가 **남의 실행 것**이 된다.
                        sim_run_id=sim_run_id,
                    ),
                    sales_terms,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 한 품목이 하루를 세우면 안 된다.
            sales_results.append(
                ItemRunOutcome(
                    item=item,
                    request_id=sales_request_id,
                    status="FAILED",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        sales_results.append(
            ItemRunOutcome(
                item=item,
                request_id=sales_request_id,
                status="RAN",
                # 🔴 **판매 어휘 그대로 싣는다.** `SL1_PRESENTED` 를 매입 어휘로
                #    접으면 그 날 무슨 답이 났는지를 세는 자리가 통째로 거짓이 된다.
                end_code=str(getattr(sales_response, "end_code", "")) or None,
            )
        )
    sales_status = _fold_item_statuses(sales_results)
    notes.append(f"판매: {sales_status} ({len(sales_results)}품목)")

    # ── 판매 승인 — 🔴 **판매 판단 바로 뒤. 출고 앞** (2026-09-11) ──
    #
    # ★ **매입 승인과 같은 자리·같은 모양이다.** 제 사이클이 낸 행만 본다 —
    #   그래서 매입 행이 여기서 `ALREADY_DECIDED` 로 다시 세지지 않는다.
    sales_approval_status, sales_approval, note = _approve(
        "판매 승인",
        as_of=as_of,
        sim_run_id=sim_run_id,
        request_ids=[one.request_id for one in sales_results],
        approve_fn=approve_fn,
        enabled=auto_approve,
    )
    if note is not None:
        notes.append(note)

    # ── 출고 — 🔴 **장부 관문 뒤 · 판단 뒤** ────────────────────────
    #
    # ★ 여기 오기 전에 관문이 이미 돌아섰을 수 있고, 그러면 이 줄에 아예 안 온다 —
    #   그것이 *"장부가 안 선 날에는 출고도 안 한다"* 이다.
    #
    # 🔴 **`sim_run_id` 를 흘려 준다** (2026-09-11). 출고 조회가 그 값으로 그날
    #    판매를 거른다 — 안 넘기면 조회가 **모든 실행**의 그 날짜 판매를 보고,
    #    남의 실행 판매가 내 창고에서 나간다. 채권·수금 두 줄과 같은 모양이다.
    outbound_status, note = _stage("출고", lambda: outbound_fn(as_of, sim_run_id=sim_run_id))
    notes.append(note)

    # ── 마감 — 🔴 **하루의 맨 끝. 출고 뒤다** ───────────────────────
    #
    # ★ **왜 출고 뒤인가.** 출고가 재고를 움직인다. 앞에서 닫으면 그날 재고가
    #   마감 뒤에 바뀌고 `inventory_qty_kg` 가 그날 장부와 안 맞는다 — 에러는 안 난다.
    #
    # 🔴 **마감이 터져도 위 결과를 안 바꾼다.** `_stage` 가 예외를 값으로 옮기고,
    #    `procurement_status` 도 `items` 도 `outbound_status` 도 그대로 나간다.
    #    이력 때문에 그날 걷기 결과가 달라지면 안 된다.
    #
    # 🔴 **`sim_run_id` 를 흘려 준다.** 마감이 그 값을 `daily_closings` 의 PK 절반
    #    (`(sim_run_id, close_date)`)으로 쓴다 — 여기서 상수를 다시 적지 않는다.
    closing_status, note = _stage("마감", lambda: close_fn(as_of, sim_run_id=sim_run_id))
    notes.append(note)

    return DayRunOutcome(
        as_of=as_of,
        action=action.action,
        reason=action.reason,
        day_open_status=day_open_status,
        maintenance_status=maintenance_status,
        maintenance=maintenance,
        pending_transition_status=pending_transition_status,
        pending_transition=pending_transition,
        inbound_status=inbound_status,
        receivable_status=receivable_status,
        collection_status=collection_status,
        procurement_status="RAN",
        sales_status=sales_status,
        outbound_status=outbound_status,
        closing_status=closing_status,
        items=tuple(results),
        sales_items=tuple(sales_results),
        procurement_approval_status=procurement_approval_status,
        sales_approval_status=sales_approval_status,
        procurement_approval=procurement_approval,
        sales_approval=sales_approval,
        notes=tuple(notes),
    )


def _maintain(
    *,
    as_of: date,
    sim_run_id: str,
    occurred_at: datetime,
    maintain_fn: Callable[..., MaintenanceOut],
    enabled: bool,
) -> tuple[str, MaintenanceOut | None, str | None]:
    """그날 창고 자리를 비우는 단계 하나 (2026-09-11). **예외를 값으로 옮긴다.**

    🔴 **`enabled` 가 거짓이면 `maintain_fn` 이 이름조차 안 불린다.** 이 한 줄이
      「비운다 / 안 비운다」가 갈리는 **유일한 자리**다 — `_approve` 와 같은 모양이고
      **더 센 이유**가 있다. 승인은 append-only 표에 한 줄이 남는 것이지만 폐기는
      **물건이 없어지고**, 물류가 *"되돌릴 경로가 없다(`ADJUST_IN` 없음 · 실사
      제외)"* 고 못박았다.

    🔴 **사유 · 행위자 · 시각을 여기서 짓지 않는다.** 어휘의 주인은
      `maintenance.py` 이고 (`FRESHNESS_EXPIRED` · `AUTO_MAINTENANCE`), 시각은
      **부르는 쪽이 나른 걷기의 시간축**이다 — 이 함수는 받은 값을 흘려보낸다.

    🔴 **`_stage` 를 그대로 못 쓴다.** 저쪽은 `(상태, 사유)` 만 돌려주는데, 요약이
      *"몇 Lot 을 버렸고 몇을 사람에게 남겼나"* 를 세려면 **낸 값 자체**가 하루
      결과에 실려야 한다 (`_retry_pending` · `_approve` 와 같은 모양).

    🔴 **터져도 하루는 계속 간다.** `run_auto_maintenance` 가 예외를 안 내겠다고
      적어 뒀지만 여기서 한 번 더 잡는다 — 하루의 진행이 그 약속에 걸리면 안 된다.

    :returns: `(단계 상태, 낸 값, 사유 한 줄)`. 안 켠 날은
        `("NOT_ATTEMPTED", None, None)` — 🔴 **note 도 안 남긴다.** 안 켠 것은
        사건이 아니라 기본값이고, 매일 한 줄씩 남기면 진짜 사유가 안 읽힌다
        (`_approve` 와 같은 규율).
    """
    if not enabled:
        return "NOT_ATTEMPTED", None, None
    try:
        out = maintain_fn(
            as_of,
            sim_run_id=sim_run_id,
            occurred_at=occurred_at,
        )
    except Exception as exc:  # noqa: BLE001 - 유지보수가 터져도 하루는 계속 간다.
        return "FAILED", None, f"물류 유지보수가 터졌다: {type(exc).__name__}: {exc}"
    status = str(getattr(out, "status", "FAILED"))
    # ⚠️ **어휘를 접지 않고 그대로 적는다** — 무엇을 버렸고 무엇을 남겼는지가 이 줄이다.
    return status, out, f"물류 유지보수: {status} {dict(sorted(out.outcomes.items()))}"


def _retry_pending(
    *,
    as_of: date,
    sim_run_id: str,
    retry_fn: Callable[..., RetryOut],
) -> tuple[str, RetryOut | None, str]:
    """미적용 전이를 다시 세우는 단계 하나 (2026-09-11). **예외를 값으로 옮긴다.**

    🔴 **`_stage` 를 그대로 못 쓴다.** 저쪽은 `(상태, 사유)` 만 돌려주는데, 요약이
       *"어느 승인이 원장에 안 닿았나"* 를 세려면 **낸 값 자체**가 하루 결과에 실려야
       한다 — 여기서 다시 세면 어휘 넷의 주인이 둘이 된다 (`_approve` 와 같은 모양).

    🔴 **터져도 하루는 계속 간다.** `retry_pending_transitions` 가 예외를 안 내겠다고
       적어 뒀지만 여기서 한 번 더 잡는다 — 하루의 진행이 그 약속에 걸리면 안 된다.

    ★ **스위치가 없다.** `auto_approve` 와 다르다 — 이 단계는 **이미 난 승인**을
      장부에 잇는 것뿐이라, 켜고 끄는 것이 곧 *"승인을 장부에 안 옮긴다"* 가 된다.

    :returns: `(단계 상태, 낸 값, 사유 한 줄)`.
    """
    try:
        out = retry_fn(as_of, sim_run_id=sim_run_id)
    except Exception as exc:  # noqa: BLE001 - 재시도가 터져도 하루는 계속 간다.
        return "FAILED", None, f"미적용 전이 재시도가 터졌다: {type(exc).__name__}: {exc}"
    status = str(getattr(out, "status", "FAILED"))
    # ⚠️ **어휘를 접지 않고 그대로 적는다** — 무엇이 왜 안 닿았는지가 이 줄이다.
    return status, out, f"미적용 전이 재시도: {status} {dict(sorted(out.outcomes.items()))}"


def _approve(
    name: str,
    *,
    as_of: date,
    sim_run_id: str,
    request_ids: Sequence[str],
    approve_fn: Callable[..., BackfillOut],
    enabled: bool,
) -> tuple[str, BackfillOut | None, str | None]:
    """판단 하나가 낸 행을 **그 자리에서** 승인한다 (2026-09-11).

    🔴 **`enabled` 가 거짓이면 `approve_fn` 이 이름조차 안 불린다.** 이 한 줄이
      「승인한다 / 안 한다」가 갈리는 **유일한 자리**다 — `backfill_runner` 의
      `--commit` 과 같은 모양이고, 같은 이유다. `master_decisions` 는 append-only 라
      한 번 들어간 승인은 못 지운다.

    🔴 **설정에 규칙이 있다고 켜지지 않는다.** 이 함수는 `enabled` 만 본다 —
      규칙의 유무를 스위치로 읽으면 설정을 실험하려고 넣은 사람이 승인까지 한다.

    🔴 **제 사이클이 방금 낸 행만 본다.** `request_ids` 로 좁힌다 — 안 좁히면
      판매 승인이 그날 매입 행을 다시 훑어 `ALREADY_DECIDED` 를 품목 수만큼 더
      쌓고, 그 어휘가 뜻하던 *"사람이 이미 정했다"* 가 성적표에서 안 읽힌다.

      ★ **사이클 이름으로 안 좁힌다.** `'PROCUREMENT'` 를 여기 적으면 그 어휘가
        두 곳에 살게 된다 — 업무 키는 이 파일이 방금 지은 것이라 주인이 여기다.

    🔴 **터져도 하루는 계속 간다.** `_stage` 와 같은 태도다 — 승인은 그날 판단·출고·
      마감의 앞을 막지 않는다 (`open_day` 가 수금 씨앗에 대해 정해 둔 그것).

    :returns: `(단계 상태, 백필이 낸 값, 사유 한 줄)`. 안 켠 날은
        `("NOT_ATTEMPTED", None, None)` — 🔴 **note 도 안 남긴다.** 안 켠 것은
        사건이 아니라 기본값이고, 매일 한 줄씩 남기면 진짜 사유가 안 읽힌다.
    """
    if not enabled:
        return "NOT_ATTEMPTED", None, None
    try:
        out = approve_fn(
            sim_run_id=sim_run_id,
            start=as_of,
            end=as_of,
            # 🔴 **축을 그대로 넘긴다.** 승인도 이번 실행의 축으로 앉아야 한다 —
            #    여기서 상수를 다시 읽으면 판단은 걷기 축에, 승인은 번인에 앉는다.
            runs_on=_runs_of(request_ids),
        )
    except Exception as exc:  # noqa: BLE001 - 승인이 터져도 하루는 계속 간다.
        return "FAILED", None, f"{name}이 터졌다: {type(exc).__name__}: {exc}"
    status = str(getattr(out, "status", "FAILED"))
    # ⚠️ **어휘를 접지 않고 그대로 적는다** — 무엇이 왜 안 채워졌는지가 이 줄이다.
    return status, out, f"{name}: {status} {dict(sorted(out.outcomes.items()))}"


def _runs_of(request_ids: Sequence[str]) -> Callable[..., list[Any]]:
    """백필이 하루치 행을 묻는 자리. **방금 그 판단이 낸 업무 키만 답한다.**

    ★ **조회를 새로 짜지 않는다.** `list_runs` 를 그대로 부르고 걸러 내기만 한다 —
      `backfill_decisions` 가 `runs_on` 을 *"`list_runs` 와 같은 키워드로 부른다"*
      고 적어 뒀고, 받은 키워드를 그대로 흘려보내는 것이 그 계약을 지키는 길이다.

    ⚠️ **조회 함수를 인자로 안 받는다.** 갈아 끼울 자리는 `approve_fn` 하나로
      족하고, 여기에 하나 더 두면 *"어느 조회로 걸렀나"* 가 두 곳에서 갈린다.
    """
    wanted = frozenset(request_ids)

    def runs_on(**kwargs: Any) -> list[Any]:
        return [row for row in list_runs(**kwargs) if row.get("request_id") in wanted]

    return runs_on


def _fold_item_statuses(results: Sequence[ItemRunOutcome]) -> str:
    """품목별 결과를 단계 하나의 어휘로 접는다. **세 값뿐이다.**

    ```text
    품목이 없었다        NOT_ATTEMPTED   — 부를 것이 없었으면 단계를 안 탄 것이다
    전부 터졌다          FAILED          — 해 보고 터졌다
    하나라도 돌았다      RAN             — 좋은 답이었다는 뜻이 아니다
    ```

    🔴 **`RAN` 은 「끝까지 돌았다」다.** `SL4_NOT_STARTED` 도 `RAN` 이다 — 못 돈
      것만 `FAILED` 다 (`ItemRunOutcome.status` 와 같은 어휘).

    ⚠️ **빈 목록을 `RAN` 으로 접지 않는다.** 접으면 *"품목이 없었다"* 와 *"세 품목이
      다 돌았다"* 가 같은 값이 되고, 화면이 둘을 못 가른다.
    """
    if not results:
        return "NOT_ATTEMPTED"
    if all(one.status == "FAILED" for one in results):
        return "FAILED"
    return "RAN"


def _ledger_gap(inbound_status: str, receivable_status: str, collection_status: str) -> bool:
    """장부가 안 섰는가. **입고 · 채권 · 수금을 다 본다.**

    🔴 **한쪽만 보면 다른 쪽 구멍이 그대로 열려 있다.** 입고가 막히면 재고와 capacity
      가 적게 반영되고, 수금이 막히면 현금이 적게 반영되고, **채권이 안 서면
      `receivables_krw` 가 적게 잡힌다** — 셋 다 매입 판단이 보는 값이고, 어느 쪽이
      틀려도 에러 없이 틀린 답이 나온다.
    """
    return any(
        status in _LEDGER_GAP_STATUSES
        for status in (inbound_status, receivable_status, collection_status)
    )


def _ledger_gap_note(inbound_status: str, receivable_status: str, collection_status: str) -> str:
    """막은 이유. 🔴 **입고·채권·수금 상태를 다 적는다 — 어느 쪽이 막았는지 보이게.**

    ⚠️ 하나만 적으면 화면이 *"장부가 안 섰다"* 까지만 말하고, 사람이 물류를 볼지
      판매를 볼지 재무를 볼지 모른 채 세 곳을 다 뒤진다.
    """
    return (
        f"{_LEDGER_GAP} (입고: {inbound_status} · 채권: {receivable_status}"
        f" · 수금: {collection_status})"
    )


def _stage(name: str, call: Callable[[], Any]) -> tuple[str, str]:
    """입고 · 채권 · 수금 · 출고 · 마감 한 단계. **예외를 값으로 옮긴다.**

    ★ 다섯 함수 다 예외를 안 내보낸다고 적어 뒀지만 여기서 한 번 더 잡는다 —
      판단의 진행 여부가 그 약속에 걸리면 안 된다 (`_seed_collection` 과 같은 태도).

    🔴 **마감에는 이유가 하나 더 있다.** `close_day` 는 `sim_run_id` 가 비면 일부러
      예외를 낸다 (빈 축을 조용히 전체로 바꾸지 않으려고). 그 배선 사고가 여기서
      `FAILED` 로 옮겨져 **그날 걷기 결과는 그대로 나간다.**
    """
    try:
        out = call()
    except Exception as exc:  # noqa: BLE001
        return "FAILED", f"{name}이 터졌다: {type(exc).__name__}: {exc}"
    status = str(getattr(out, "status", "FAILED"))
    return status, f"{name}: {status} {getattr(out, 'reason', '')}".strip()


# ── ③ 진입점 — 시계를 읽는 유일한 자리 ─────────────────────────────────


def wake_up(
    *,
    now: Callable[[], datetime] = clock.seoul_now,
    calendar: Callable[[], MarketCalendar] = get_market_calendar,
    readiness: Callable[[date], DayForecastReadiness] = lambda as_of: day_forecast_readiness(
        as_of=as_of
    ),
    policy_version: str = DAILY_POLICY_VERSION,
    open_day_fn: Callable[..., Any] = open_day,
    maintain_fn: Callable[..., MaintenanceOut] = run_auto_maintenance,
    retry_fn: Callable[..., RetryOut] = retry_pending_transitions,
    receive_fn: Callable[..., Any] = receive_arrivals,
    issue_fn: Callable[..., Any] = issue_receivables,
    collect_fn: Callable[..., Any] = collect_receipts,
    procure_fn: Callable[..., Any] = run_procurement,
    sales_fn: Callable[..., Any] = run_sales,
    outbound_fn: Callable[..., Any] = ship_due_sales,
    close_fn: Callable[..., Any] = close_day,
    sim_run_id: str = BURN_IN_SIM_RUN_ID,
    auto_approve: bool = False,
    approve_fn: Callable[..., BackfillOut] = backfill_decisions,
    terms_of: Callable[[str], SalesTermsRule | None] = read_run_sales_terms,
    auto_maintain: bool = False,
) -> DayRunOutcome:
    """한 번 깨어났다. **결정하고, 그 답을 따른다.**

    🔴 **`auto_maintain` 도 여기서 기본이 거짓이다.** 깨어난 것만으로 창고가
      비워지면 폐기를 명시로만 켠다는 규율이 **깨어남 한 번으로 뚫린다** —
      `auto_approve` 와 같은 이유이고, 되돌릴 경로가 없어 더 센 이유다.

    🔴 **판매 상업 조건은 여기서 읽어 하루에 넘긴다** (2026-09-11).

      ★ **`auto_approve` 와 축이 다르다.** 승인은 명시로 켜야 돌지만 조건은
        **규칙 파일에 있으면 실린다** — 조건을 승인 스위치 뒤에 두면 *"규칙은
        적었는데 왜 또 재무가 판정을 못 내나"* 가 생기고, 그 답이 승인 스위치라는
        것은 아무 데도 안 적혀 있다.

      ⚠️ **하루가 제 손으로 안 읽는 이유**는 `run_scheduled_day` 의 `sales_terms`
        에 적어 뒀다.

    🔴 **`auto_approve` 는 여기서도 기본이 거짓이다.** 깨어난 것만으로 승인이
      서면 자동 승인을 명시로만 켠다는 규율이 **깨어남 한 번으로 뚫린다.**

    🔴 **이 함수는 안 잔다.** 5분 뒤 다시 부르는 것은 밖의 일이고 (`retry_after` 가
      그 간격을 말해 준다), 여기에 `sleep` 이 들어오면 검사가 못 지난다.

    ★ **시계를 여기서 한 번만 읽는다.** 읽은 시각으로 `as_of` 와 마감 비교를 둘 다
      한다 — 두 번 읽으면 자정을 넘기는 순간 날짜와 시각이 서로 다른 날을 가리킨다.

    ⚠️ **기본값이 실제 함수 자체다. `None` 이 아니다** (`clock.py` · `verifier.py` 와
      같은 규율). `None` 을 허용하면 *"안 줬다"* 와 *"기본을 줬다"* 가 같은 값이 된다.
    """
    moment = now()
    as_of = clock.today_in_seoul(lambda: moment)
    action = plan_next_action(
        now=moment,
        as_of=as_of,
        calendar=calendar(),
        gate_result=readiness(as_of),
    )
    return run_scheduled_day(
        action,
        policy_version=policy_version,
        open_day_fn=open_day_fn,
        maintain_fn=maintain_fn,
        retry_fn=retry_fn,
        receive_fn=receive_fn,
        issue_fn=issue_fn,
        collect_fn=collect_fn,
        procure_fn=procure_fn,
        sales_fn=sales_fn,
        outbound_fn=outbound_fn,
        close_fn=close_fn,
        sim_run_id=sim_run_id,
        auto_approve=auto_approve,
        approve_fn=approve_fn,
        sales_terms=terms_of(sim_run_id),
        auto_maintain=auto_maintain,
    )
