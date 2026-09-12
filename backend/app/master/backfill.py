"""자동 백필 승인 — **과거 구간을 규칙 하나로 재현해 장부를 만든다.**

```text
백필 승인           과거 구간을 재현해 장부를 만드는 것   = 데이터 생성   ← 이 파일
에이전트 자율 승인   오늘의 안을 스스로 정하는 것          = 판단 위임    🔴 팀이 금지했다
```

★★ **어느 안이 나은지 판단하는 코드가 여기 없다.** 규칙이 가리키는 안을 그대로
  고르거나, 그 안이 없으면 **안 고른다.** 두 갈래뿐이라 고를 것이 없다.

★ **`backtest_runner.walk` 과 같은 모양이다** — 범위를 받고, 하루가 막혀도 밖으로
  예외를 안 내고, 무엇을 했는지를 값으로 돌려준다.

🔴 **이 파일에 CLI 진입점이 없다 — 일부러 없다.** 판단이 사는 파일에 문까지 두면
  실수로 돌아갈 길이 그만큼 짧아진다 (`tests/master/test_backfill.py` 가 이것도
  잠근다). 문은 `backfill_runner.py` 에 가드와 함께 서 있고, **기본이 「안 쓴다」**
  라 `--commit` 을 줘야 한 행이라도 적힌다.

## 규칙의 집

```json
{"backfill": {
  "procurement": {"rule": "ALWAYS_BASE",       "scenario_label":  "..."},
  "sales":       {"rule": "ALWAYS_FIXED_TYPE", "scenario_type":   "..."},
  "sales_terms": {"partner_id": "...", "payment_terms_type": "...",
                  "payment_days": 0,   "unit_price_source":  "..."}
}}
```

매입 규칙은 **둘 중 하나**다 (2026-09-11).

```json
{"procurement": {"rule": "ALWAYS_BASE",   "scenario_label":  "1순위"}}
{"procurement": {"rule": "FIRST_OFFERED", "scenario_labels": ["1순위", "2순위"]}}
```

★★ **왜 순서가 생겼나.** 매입 `#584`(보유 차감)가 들어오면서 **한 라벨의 안이 자주
  안 선다.** 실측에서 같은 열흘이 15건 → 3건으로 줄었고, 그 라벨 하나를 가리키던
  걷기가 `LABEL_NOT_OFFERED` 를 51번 냈다 — 곡선에 승인이 거의 안 남았다.

🟢 **대체가 아니다.** 사람이 *"이것 먼저, 없으면 저것"* 이라고 **정해서 파일에
  적는다.** 코드는 그 순서를 읽기만 하고, 안 적힌 라벨은 여전히 안 고른다.

`sim_runs.config_json` 이다. **한 실행 = 사이클마다 규칙 하나**이고, 규칙은 행이
아니라 실행에 속한다.

🔴 **`sales_terms` 는 승인 규칙이 아니다** (2026-09-11). 승인할 안을 고르는 둘과
  달리, 이 칸은 **판매에 물어볼 때 요청에 실리는 조건**이다 — 자동 걷기에 사람이
  없어 거래처도 지급조건도 단가도 아무도 안 정하던 자리다. `SALES_TERMS_KEY` 가
  왜 `sales` 안에 안 들어갔는지를 적어 뒀다.

🔴 **사이클별로 가른 모양만 읽는다.** 옛 평면 모양(`{"backfill": {"rule": ...}}`)은
  터진다 — 조용히 매입으로 접으면 **어느 규칙으로 돌았는지가 갈린다.** 지금 그
  모양을 쓴 실행이 하나도 없어 호환을 만들 이유도 없다 (2026-09-10 확인).

🔴 **코드에 안 이름을 박지 않는다.** `decision.scenario_labels_of` 가 이미 그 규율을
  적어 뒀다 — *"'보수·기본·공격' 은 매입의 계약이다. 여기에 복제하면 매입이 라벨을
  바꿀 때 조용히 어긋난다."* 판매 축 이름도 같다. 그래서 코드가 아는 것은 **「고정
  라벨 규칙」·「고정 축 규칙」이라는 모양**뿐이고, 어느 이름인지는 **설정이 말한다.**

🔴 **기본 규칙을 지어내지 않는다.** 규칙 칸이 없으면 `NO_RULE` 이고 한 행도 안 쓴다 —
  기본값은 곧 업무 규칙이고, 그러면 아무도 안 정한 규칙으로 곡선이 선다.

## 축이 둘로 갈리는 자리

```text
매입   설정의 scenario_label 로 고른다
판매   설정의 scenario_type 으로 후보를 **찾아**, 그 후보의 scenario_id 를 싣는다
```

★ 판매 후보에는 `label` 이 없다. `DecisionIn.scenario_label` 칸에 `scenario_id` 를
  싣는 것은 **판매가 이미 정한 계약**이고 (`decision.DecisionIn` 참고), 여기서 새로
  만드는 것이 아니다.

🔴 **일반 Runtime 은 그대로 `HUMAN` 이다.** 이 판은 **과거 장부 재현**뿐이라,
  추천·랭킹을 승인 권한으로 쓰는 것과 다르다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.master.decision import (
    SALES_CYCLE,
    DecisionIn,
    DecisionOut,
    RevalidationOutcome,
    approve_end_codes,
    available_scenario_names,
    scenario_ids_of_type,
)
from app.master.decision_repository import list_decisions
from app.master.decision_service import record_decision
from app.master.ledger_repository import get_burn_in
from app.master.run_repository import list_runs

AUTO_BACKFILL = "AUTO-BACKFILL"
"""자동으로 채운 승인의 `decided_by`.

🔴 **사람 이름을 안 쓴다.** `master_decisions.decided_by` 는 지금 전부 사람 이름이라,
  자동으로 채우면서 거기 사람 이름을 적으면 **사람이 안 눌렀는데 눌렀다고 기록**되고
  그 표는 append-only 라 못 지운다.

★ `ask_service` 가 적어 둔 *"승인자가 없는 승인은 승인이 아니다"* 를 지키는 길이
  이것이다 — 자동일 때도 **「누가」를 정직하게** 적는다.
"""

BACKFILL_BOUNDARY_AS_OF = date(2026, 9, 9)
"""자동으로 채울 수 있는 마지막 날. **이 날까지 포함이다.**

```text
as_of <= 2026-09-09   자동으로 채운다
as_of >= 2026-09-10   🔴 사람만. 한 행도 안 쓴다
```

🔴 **`config_json` 으로 빼지 않는다.** 이건 규칙이 아니라 **가드**라, 옮기려면
  diff 에 보여야 한다. 설정으로 내리면 오늘 이후를 자동 승인하는 것이 **행 하나
  고치는 일**이 된다.
"""

ALWAYS_BASE = "ALWAYS_BASE"
"""**매입이 아는 규칙 이름.** 「그 실행이 정한 라벨 하나를 늘 고른다」는 모양.

⚠️ 이름이 가리키는 라벨은 여기 없다 — `scenario_label` 이 설정에 있다.

🟢 **`FIRST_OFFERED` 가 생겨도 이 규칙은 그대로 산다** (2026-09-11). 한 라벨만 고르는
  것은 여전히 옳은 규칙이고, **그것으로 돈 실행들이 이미 있다.** 새 규칙은 이것을
  갈아끼우는 것이 아니라 **더하는 것**이다.
"""

FIRST_OFFERED = "FIRST_OFFERED"
"""**매입이 아는 두 번째 규칙 이름.** 「적어 둔 순서대로 찾아 먼저 있는 것을 고른다」.

```json
{"procurement": {"rule": "FIRST_OFFERED", "scenario_labels": ["1순위", "2순위"]}}
```

⚠️ **여기 적힌 두 낱말은 자리 표시다.** 실제 라벨 이름은 이 파일 어디에도 없다 —
  `ALWAYS_BASE` 가 자기 라벨을 안 들고 있는 것과 같은 규율이고,
  `test_코드에_시나리오_안_이름이_박혀_있지_않다` 가 그것을 지킨다.

★★ **왜 순서가 필요해졌나 — 실측 2026-09-11.** 매입 `#584`(보유 차감)가 들어오면서
  같은 열흘·같은 규칙·같은 출발점에서 한 라벨의 안이 **15건에서 3건으로** 줄었다.
  그 라벨 하나를 가리키던 걷기가 `LABEL_NOT_OFFERED` 를 **51번** 냈고, 곡선에 승인이
  거의 안 남았다.

🟢 **걷기가 대체하지 않은 것은 맞다.** *"없으면 다른 안으로 대체하지 않는다"* 는
  규율이 일했고 그 규율은 지금도 그대로다 (`_approve_one` 의 🔴).

🔴 **그래서 조용한 대체가 아니라 「순서를 밝힌 한 규칙」으로 만든다.** 사람이
  *"이것 먼저, 없으면 저것"* 으로 정했고, 그 순서가 **설정 파일에 적혀 리뷰에
  남는다.** 코드가 고르는 것이 아니다.

🔴 **순서를 코드에 박지 않는다.** 배열 순서가 정본이다 — 배열을 뒤집어 쓰면 그대로
  뒤집혀 돌아야 한다. 코드가 순서를 알면 **규칙의 주인이 설정이 아니게 되고**, 그
  순간 «어느 안을 우선하나» 가 배포로 바뀐다.

⚠️ **하나도 없으면 여전히 `LABEL_NOT_OFFERED` 다.** 순서는 *"찾아볼 곳"* 을 늘릴 뿐
  *"없는 것을 만들어"* 주지 않는다.
"""

_PROCUREMENT_RULE_NAMES: tuple[str, ...] = (ALWAYS_BASE, FIRST_OFFERED)
"""매입이 아는 규칙 이름 **전부.**

🔴 **손으로 나열한 문장을 두 벌로 두지 않는다.** 사유 문장이 이 튜플에서 나오므로,
  규칙을 더하면서 안내 문장만 옛 하나를 가리키는 날이 없다.
"""

ALWAYS_FIXED_TYPE = "ALWAYS_FIXED_TYPE"
"""**판매가 아는 규칙 이름.** 「그 실행이 정한 축 하나를 늘 고른다」는 모양.

⚠️ 이름이 가리키는 축은 여기 없다 — `scenario_type` 이 설정에 있다.

🔴 **이름에 축 이름을 넣지 않았다.** 넣으면 판매가 축을 바꾸는 날 규칙 이름이 조용히
  낡고, 「코드에 축 이름을 안 박는다」는 잠금도 그 자리에서 뚫린다.
"""

BACKFILL_CONFIG_KEY = "backfill"
"""`sim_runs.config_json` 안에서 규칙이 앉는 칸 이름.

🔴 **읽는 쪽과 쓰는 쪽이 같은 이름을 두 벌로 들지 않게 상수로 둔다** (2026-09-11).
  `read_rules` 가 읽고 `sim_run_runner` 가 쓴다 — 문자열을 양쪽에 적으면 한쪽만
  고치는 날 **규칙을 실은 실행이 규칙이 없는 실행으로 읽힌다.**

⚠️ **칸 이름이지 규칙 이름이 아니다.** `ALWAYS_BASE` 같은 규칙 이름과 축이 다르다.
"""

PROCUREMENT_RULES_KEY = "procurement"
"""매입 규칙이 앉는 칸 이름."""

SALES_RULES_KEY = "sales"
"""판매 규칙이 앉는 칸 이름."""

SALES_TERMS_KEY = "sales_terms"
"""**자동 걷기의 판매 요청이 실을 상업 조건**이 앉는 칸 이름 (2026-09-11).

```text
sales        그날 난 판매 안 중 **어느 것을 승인하나**   ← 승인 규칙
sales_terms  판매를 **무슨 조건으로 물어보나**          ← 요청 조건   ← 이 칸
```

🔴 **`sales` 안에 넣지 않는다.** 두 칸은 축이 다르다 — 앞엣것은 이미 난 안을 고르고
  뒤엣것은 안이 나기 전에 요청에 실린다. 한 칸에 담으면 *"승인 규칙을 안 적고
  조건만 적는다"* 가 모양으로 표현이 안 되고, `for_cycle` 이 조건까지 들고 다닌다.

🔴 **`BackfillOutcome` 여덟에 이 칸의 사유가 안 섞인다.** 조건이 없는 것은 승인
  결과가 아니라 **요청에 아무것도 안 실린다**는 사실이다.
"""

_RULES_KEYS: tuple[str, ...] = (PROCUREMENT_RULES_KEY, SALES_RULES_KEY)

_SECTION_KEYS: tuple[str, ...] = (*_RULES_KEYS, SALES_TERMS_KEY)
"""`backfill` 칸이 **아는 칸 전부.** 하나라도 있으면 새 모양으로 본다.

🔴 **`_RULES_KEYS` 와 따로 둔다.** 저쪽은 *"사이클 규칙이 사는 칸"* 이라
  `for_cycle` 이 쓰고, 이쪽은 *"이 칸이 새 모양인가"* 를 가르는 데만 쓴다. 하나로
  묶으면 `sales_terms` 가 사이클 규칙처럼 읽히고, 조건만 적은 규칙 파일이
  **옛 평면 모양으로 오인돼 터진다.**
"""

ML_CURRENT_PRICE = "ML_CURRENT_PRICE"
"""단가를 **ML 이 그날 예측과 같은 행에 동봉한 시세**에서 가져온다.

🔴 **마스터가 시세를 계산하지 않는다.** `inputs.load_forecast` 가 실어 준
  `current_price` 를 **그대로 옮긴다** — 곱하지도 반올림하지도 않는다.

⚠️ 그날 예측이 없으면 단가가 **안 실린다.** 옛 배치로 메우지 않는다 —
  `load_forecast` 가 이미 *"하루만 밀려도 안 쓴다"* 로 정해 뒀다.
"""

FIXED_UNIT_PRICE = "FIXED"
"""단가를 **규칙 파일이 적은 고정값**에서 가져온다.

⚠️ 그 숫자는 규칙 파일에 있다 — `unit_price_krw` 다. **코드에 없다.**
"""

_UNIT_PRICE_SOURCES: tuple[str, ...] = (ML_CURRENT_PRICE, FIXED_UNIT_PRICE)


BackfillOutcome = Literal[
    "RECORDED",
    "ALREADY_DECIDED",
    "NOT_APPROVABLE",
    "NO_RULE_FOR_CYCLE",
    "LABEL_NOT_OFFERED",
    "AMBIGUOUS_TYPE",
    "BLOCKED_BY_BOUNDARY",
    "FAILED",
]
"""실행 이력 한 행에 무슨 일이 있었나. **여덟을 접지 않는다.**

```text
RECORDED              승인을 적었다
ALREADY_DECIDED       이미 결정이 있다 — 덮지 않는다
NOT_APPROVABLE        승인이 성립하는 종료 코드가 아니다
NO_RULE_FOR_CYCLE     그 사이클 규칙을 설정이 안 정했다
LABEL_NOT_OFFERED     규칙이 가리키는 안이 그날 없다
AMBIGUOUS_TYPE        규칙이 가리키는 축의 후보가 둘 이상이라 안 골랐다
BLOCKED_BY_BOUNDARY   as_of 가 경계 밖이다
FAILED                해 봤는데 터졌다 (사유를 같이 적는다)
```

🔴 **`NO_RULE_FOR_CYCLE` 을 `NO_RULE` 로 접지 않는다.** *"규칙을 아무것도 안 정했다"*
  와 *"매입만 정하고 판매는 안 정했다"* 는 다른 사실이고, 뒤엣것은 **의도일 수 있다.**

🔴 **`AMBIGUOUS_TYPE` 을 `LABEL_NOT_OFFERED` 로 접지 않는다.** *"그 축이 그날 없다"*
  와 *"그 축이 둘이라 어느 것인지 규칙이 못 정한다"* 는 고칠 곳이 서로 다르다 —
  앞엣것은 그날의 사실이고 뒤엣것은 규칙이 덜 정해졌다는 뜻이다.

⚠️ *"없다"* 와 *"안 했다"* 와 *"못 했다"* 를 묶으면 **왜 곡선이 평평한지를 결과가
  못 답한다.**
"""

BackfillStatus = Literal["RAN", "NO_RULE"]
"""실행 전체가 돌았나. 🔴 **`BackfillOutcome` 과 축이 다르다** — 저쪽은 행 하나이고
이쪽은 걷기 한 번이다. `NO_RULE` 이면 행을 아예 안 본다.
"""


class BackfillRuleMissing(Exception):
    """`sim_runs.config_json` 이 백필 규칙을 말하지 않는다.

    ★ **밖으로 안 나간다.** `backfill_decisions` 가 잡아 `NO_RULE` 로 값에 담는다 —
      이 사실도 결과의 일부라, 예외로 던지면 부르는 쪽이 *"안 돌았다"* 와 *"돌았는데
      할 것이 없었다"* 를 못 가른다.
    """


@dataclass(frozen=True)
class BackfillRule:
    """그 실행이 정한 **매입** 백필 규칙 — 「라벨 하나를 늘 고른다」."""

    #: 규칙의 이름. `ALWAYS_BASE` 다.
    name: str
    #: 그 규칙이 늘 고르는 안의 라벨. 🔴 **설정에서 온다 — 코드에 없다.**
    scenario_label: str

    @property
    def labels_in_order(self) -> tuple[str, ...]:
        """찾아볼 라벨을 **순서대로.** 이 규칙은 언제나 하나다.

        ★ **`OrderedLabelRule` 과 이 이름을 공유한다.** 고르는 자리(`_approve_one`)가
          규칙의 종류를 다시 가르지 않게 하려는 것이다 — 거기서 `isinstance` 로
          갈래를 늘리면 규칙을 더할 때마다 그 함수가 같이 자란다.
        """
        return (self.scenario_label,)


@dataclass(frozen=True)
class OrderedLabelRule:
    """그 실행이 정한 **매입** 백필 규칙 — 「적어 둔 순서대로 먼저 있는 것」.

    🔴 **`BackfillRule` 과 한 모양으로 합치지 않는다.** 앞엣것은 라벨 하나를
      **가리키고** 이쪽은 여럿을 **순서대로 찾는다** — 고르는 방법이 다른 둘을 한
      칸에 담으면 어느 규칙으로 돌았는지가 값의 길이로만 읽힌다
      (`SalesBackfillRule` 을 따로 둔 것과 **같은 이유**다).

    ⚠️ **하나만 적어도 된다.** 그때 동작은 `ALWAYS_BASE` 와 같지만 **적힌 규칙 이름이
      다르다** — 실행 이력에서 *"그때 무슨 규칙이었나"* 가 이름으로 남는다.
    """

    #: 규칙의 이름. `FIRST_OFFERED` 다.
    name: str

    #: 찾아볼 라벨을 **순서대로.** 🔴 **배열 순서가 정본이다 — 코드가 안 정한다.**
    scenario_labels: tuple[str, ...]

    @property
    def labels_in_order(self) -> tuple[str, ...]:
        """적힌 그대로. **여기서 정렬하지 않는다.**

        🔴 정렬하면 설정이 말한 우선순위가 사라지고 **코드가 순서를 정한 것**이 된다.
        """
        return self.scenario_labels


@dataclass(frozen=True)
class SalesBackfillRule:
    """그 실행이 정한 **판매** 백필 규칙.

    🔴 **`BackfillRule` 과 한 모양으로 합치지 않는다.** 매입은 라벨로 **가리키고**
      판매는 축으로 **찾는다** — 고르는 방법이 다른 둘을 한 칸에 담으면 어느 축의
      규칙이었는지가 값의 모양으로만 읽힌다 (`decision` 이 `commitment` 와 `sale` 을
      가른 것과 같은 이유).
    """

    #: 규칙의 이름. 판매가 아는 것은 `ALWAYS_FIXED_TYPE` 하나다.
    name: str
    #: 그 규칙이 늘 찾는 후보의 축. 🔴 **설정에서 온다 — 코드에 없다.**
    scenario_type: str


#: 매입 규칙의 두 모양. ★ **이름이 하나 있어야 부르는 쪽이 「매입 규칙」을 한 낱말로
#:   말할 수 있다** — 안 두면 `BackfillRule | OrderedLabelRule` 이 여섯 자리에 퍼진다.
ProcurementRule = BackfillRule | OrderedLabelRule


@dataclass(frozen=True)
class SalesTermsRule:
    """그 실행의 **자동 걷기가 판매에 물어볼 상업 조건** (2026-09-11).

    ★★ **왜 있나.** 자동 걷기에는 사람이 없다. 수량 하나만 물류에서 오고 거래처 ·
      지급조건 · 단가는 아무도 안 정해서, 재무가 `SALES_INPUT_INCOMPLETE` 로
      판정을 못 냈다 (실측: `missing_fields` 다섯).

    🔴 **값이 여기 하나도 없다.** 거래처 id 도 지급조건 이름도 일수도 **규칙 파일이
      말한다.** 코드에 박으면 조건을 바꾸는 날 diff 가 아니라 배포가 되고,
      `--opening-usage-scope` 를 문에 안 박은 것과 같은 자리에서 무너진다.

    🔴 **네 칸이 전부 필수다.** 재무가 요구하는 다섯 중 넷이 여기서 서고 나머지
      하나(`reported_sales_amount_krw`)는 **판매가 수량 × 단가로 자기 안에서 센다.**
      한 칸만 비워 둘 수 있게 하면 재무 판정이 **왜 또 안 났는지**를 규칙 파일만
      보고는 못 읽는다.
    """

    #: 어느 거래처에 파는가. 🔴 **설정에서 온다 — 코드에 없다.**
    partner_id: str
    #: 지급조건 종류. ⚠️ **어휘의 주인은 판매다** — 마스터는 값을 검사하지 않고
    #: 나르기만 한다. 아는 이름인지는 판매 문 앞(`SalesUserRequest`)이 판정한다.
    payment_terms_type: str
    #: 며칠 뒤에 받는가. ★ **0 도 값이다** — *"당일 수금"* 이라는 정해진 조건이다.
    payment_days: int
    #: 단가를 **어디서** 가져오나. `ML_CURRENT_PRICE` · `FIXED` 둘뿐이다.
    unit_price_source: str
    #: `FIXED` 일 때 그 고정값. `ML_CURRENT_PRICE` 면 `None` 이다.
    unit_price_krw: Decimal | None = None


@dataclass(frozen=True)
class BackfillRules:
    """그 실행이 **사이클별로** 정한 규칙.

    ★ 한 칸이 비어 있는 것은 사고가 아니라 사실이다 — *"그 사이클은 안 정했다"* 를
      그대로 담고, 행에 닿을 때 `NO_RULE_FOR_CYCLE` 로 남긴다.

    ⚠️ **`sales_terms` 는 사이클 규칙이 아니다.** `for_cycle` 이 안 돌려준다 —
      승인할 안을 고르는 것과 요청에 조건을 싣는 것은 축이 다르다.
    """

    #: 🔴 **두 모양 중 하나다** — 라벨 하나를 가리키거나(`BackfillRule`) 순서대로
    #: 찾거나(`OrderedLabelRule`). 고르는 자리는 `labels_in_order` 하나만 본다.
    procurement: ProcurementRule | None = None
    sales: SalesBackfillRule | None = None
    sales_terms: SalesTermsRule | None = None

    def for_cycle(self, cycle: str) -> ProcurementRule | SalesBackfillRule | None:
        """그 실행 행의 `cycle` 에 걸리는 규칙.

        ★ **`approve_end_codes` 와 같은 모양이다** — 어느 어휘를 볼지는 실행 행의
          `cycle` 이 정한다. 부르는 쪽이 정하면 축을 나눈 뜻이 없어진다.
        """
        return self.sales if cycle == SALES_CYCLE else self.procurement


@dataclass(frozen=True)
class BackfilledRun:
    """실행 이력 한 행의 처리 결과."""

    as_of: date
    run_id: str
    request_id: str | None
    outcome: BackfillOutcome
    #: 왜 그 결과가 됐나. `RECORDED` 에는 없다.
    reason: str | None = None
    #: 승인 문이 돌린 재검증 결과. 🔴 **`RECORDED` 여도 `FAILED` 일 수 있다** —
    #: 결정 행은 쓰이고 효력은 재검증이 정한다 (`record_decision` 의 규율 그대로).
    revalidation_outcome: RevalidationOutcome | None = None

    #: 판매 확정 결과 (2026-09-11). `CONFIRMED` · `BLOCKED` · `FAILED` · `None`.
    #:
    #: 🔴 **`revalidation_outcome` 의 다음 축이다.** 재검증이 `PASSED` 여도 확정은
    #:   막힐 수 있고, 그때 `sales` 에는 한 행도 안 선다 — 그 사실이 `RECORDED` 에
    #:   가려 있었다 (실측 2026-09-11: 재검증 `PASSED` 7건인데 `sales` 0행).
    #:
    #: ★ **매입 행에서는 `None` 이다.** 판매 확정이 도는 것은 판매 사이클뿐이라,
    #:   그 `None` 은 *"확정에 실패했다"* 가 아니라 **"확정할 것이 없었다"** 다.
    confirmation_status: Literal["CONFIRMED", "BLOCKED", "FAILED"] | None = None

    #: 확정이 막히거나 터진 이유 한 줄.
    #:
    #: ⚠️ **코드만 나르면 오늘 밤이 반복된다** (2026-09-11). `BLOCKED` 만 보고는
    #:   *"상업조건이 없나"* 인지 *"입력 계약이 안 맞나"* 인지를 못 가른다 — 실제로
    #:   그날의 이유는 `ValidationError: reported_sales_amount_krw` 였고, 그것은
    #:   문장을 봐야 보인다.
    confirmation_reason: str | None = None

    #: 확정분 예약 결과 (2026-09-12). `RESERVED` · `SHORT` · `None`.
    #:
    #: 🔴 **`confirmation_status` 의 다음 축이다.** 확정이 `CONFIRMED` 여도 재고를
    #:   못 잡을 수 있고, 그때 그 판매는 **팔렸는데 잡은 것이 없는** 상태다 — 그
    #:   사실이 `CONFIRMED` 에 가려 `SIM-CHAIN-V4` 에서 같은 재고가 두 번 팔렸다.
    #:
    #: ★ **확정이 안 선 행에서는 `None` 이다** — *"예약에 실패했다"* 가 아니라
    #:   **"예약할 것이 없었다"** 다.
    reservation_outcome: Literal["RESERVED", "SHORT"] | None = None

    #: 매입 규칙이 **순서에서 실제로 고른 라벨** (2026-09-11). 못 골랐으면 `None`.
    #:
    #: ★★ **`FIRST_OFFERED` 를 들이면서 같이 연 칸이다.** 순서가 생긴 순간 *"그날
    #:   어느 라벨이 돌았나"* 가 더 이상 규칙 파일만 보고는 안 풀린다 — 이 칸이
    #:   없으면 **곡선이 한 규칙의 것이 아니게 되고**, 사람이 날마다 되짚어야 한다.
    #:
    #: 🔴 **판매는 `None` 이다.** 저쪽은 라벨이 아니라 축으로 찾고 그 축은 실행마다
    #:   하나라 셀 것이 없다. 후보 `scenario_id` 를 여기 담으면 날마다 다른 값이라
    #:   요약 줄이 못 읽히는 목록이 된다.
    #:
    #: ⚠️ `master_decisions.scenario_label` 에도 같은 값이 적힌다 (승인 문이
    #:   `DecisionIn.scenario_label` 로 받는다). **그쪽이 장부의 정본이고** 이 칸은
    #:   걷기 한 번을 요약하려고 든 것이다 — 요약이 DB 를 다시 읽지 않게.
    picked_label: str | None = None


@dataclass(frozen=True)
class BackfillOut:
    """백필 한 번의 결과. **예외 대신 이것을 돌려준다.**"""

    sim_run_id: str
    start: date
    end: date
    status: BackfillStatus
    #: 그 실행이 사이클별로 정한 규칙. `NO_RULE` 이면 `None`.
    rules: BackfillRules | None = None
    #: `NO_RULE` 의 사유. 🔴 **`status` 와 짝이다.**
    reason: str | None = None
    #: 본 실행 이력 행마다 하나씩. **본 순서 그대로.**
    runs: tuple[BackfilledRun, ...] = ()
    #: 범위 안이지만 경계를 넘어 **자동으로는 못 채우는 날.**
    #:
    #: 🔴 **조용히 자르지 않는다.** 자르면 부르는 쪽이 *"179일을 채웠다"* 고 믿는다 —
    #:   그날에 행이 하나도 없어도 이 목록에는 남는다.
    blocked_days: tuple[date, ...] = ()

    @property
    def outcomes(self) -> Mapping[str, int]:
        """결과 분포. **여덟 값을 그대로 센다** — 새 이름을 안 붙인다."""
        return Counter(one.outcome for one in self.runs)

    @property
    def confirmation_outcomes(self) -> Mapping[str, int]:
        """판매 확정 어휘 분포 (2026-09-11). 🔴 **셋을 접지 않는다.**

        ```text
        CONFIRMED  sales · sale_items 가 섰다
        BLOCKED    확정할 수 없었다 — 아무것도 안 썼다
        FAILED     쓰려다 실패했다 — 롤백했다
        ```

        ★★ **`outcomes` 와 한 칸에 담지 않는다.** 축이 다르다 — 저쪽은 *"승인을
          적었나"* 이고 이쪽은 *"판매가 섰나"* 다. `RECORDED` 가 곧 판매가 선 것이
          아니라는 사실이 이 줄로 보여야 한다 (`transition_outcomes` 와 같은 규율).

        ★ **`None` 은 안 센다.** 매입 행에는 확정이라는 사건 자체가 없어, 세면
          *"확정을 못 했다"* 가 매입 행 수만큼 부풀어 오른다.

        ★ **이름의 주인은 `sales_approval.SaleConfirmationOut` 이다.** 여기서 새
          이름을 안 붙이고 세기만 한다.
        """
        return Counter(
            one.confirmation_status
            for one in self.runs
            if one.confirmation_status is not None
        )

    @property
    def reservation_outcomes(self) -> Mapping[str, int]:
        """확정분 예약 어휘 분포 (2026-09-12). 🔴 **둘을 접지 않는다.**

        ```text
        RESERVED  요구량만큼 잡았다
        SHORT     모자랐다 — 확정은 CONFIRMED 인데 재고는 그만큼 없었다
        ```

        ★★ **`confirmation_outcomes` 와 한 칸에 담지 않는다.** 축이 다르다 — 저쪽은
          *"판매가 섰나"* 이고 이쪽은 *"그만큼 잡았나"* 다. `CONFIRMED` 가 곧 재고를
          잡은 것이 아니라는 사실이 이 줄로 보여야 한다.

        ★ **`None` 은 안 센다.** 확정이 안 선 행에는 예약이라는 사건 자체가 없다.

        ★ **이름의 주인은 `sales_approval.SaleConfirmationOut` 이다.**
        """
        return Counter(
            one.reservation_outcome
            for one in self.runs
            if one.reservation_outcome is not None
        )

    @property
    def label_outcomes(self) -> Mapping[str, int]:
        """**어느 라벨이 실제로 섰나** (2026-09-11). 🔴 **접지 않는다.**

        ★★ **`FIRST_OFFERED` 가 순서를 갖는 순간 이 줄이 필요해졌다.** 규칙 파일이
          `["보수", "기본"]` 이라고 적혀 있어도 **그날 무엇이 섰는지**는 그날
          제시된 안이 정한다 — 이 줄이 없으면 곡선이 한 규칙의 것이 아니게 되고,
          사람이 날마다 결정 행을 되짚어야 한다.

        ★ **`outcomes` 와 한 칸에 담지 않는다.** 축이 다르다 — 저쪽은 *"승인을
          적었나"* 이고 이쪽은 *"무엇을 골랐나"* 다 (`confirmation_outcomes` 를 가른
          것과 같은 규율).

        ★ **`None` 은 안 센다.** 못 고른 행과 판매 행에는 라벨이라는 사실 자체가
          없어, 세면 *"아무것도 안 골랐다"* 가 그 수만큼 부풀어 오른다.

        ★ **이름의 주인은 매입이다.** 여기서 새 이름을 안 붙이고 세기만 한다.
        """
        return Counter(
            one.picked_label for one in self.runs if one.picked_label is not None
        )


def read_rules(config_json: Mapping[str, Any]) -> BackfillRules:
    """`config_json` 이 말하는 **사이클별** 백필 규칙.

    🔴 **없으면 지어내지 않고 막는다.** 모르는 규칙 이름도 마찬가지다 — 아는 모양이
      아닌 것을 아는 모양으로 접으면, 설정이 시킨 적 없는 규칙으로 장부가 선다.

    🔴 **적어 둔 사이클 칸은 전부 그 자리에서 검사한다.** 말이 안 되는 규칙을 적어
      놓고 그 사이클 행이 그날 없었다는 이유로 초록이 되면, 설정이 틀렸다는 사실이
      **행의 유무에 따라** 보였다 안 보였다 한다.

    ⚠️ **없는 칸은 여기서 안 막는다.** *"안 적었다"* 는 사고가 아니라 의도일 수
      있어, 행에 닿을 때 `NO_RULE_FOR_CYCLE` 로 남는다.

    :raises BackfillRuleMissing: `backfill` 칸이 없거나, 사이클별로 가르지 않은
        모양이거나, 적어 둔 사이클 칸이 아는 규칙이 아닐 때.
    """
    section = config_json.get(BACKFILL_CONFIG_KEY)
    if section is None:
        # ⚠️ 사유 문장에 안 이름을 쓰지 않는다 — `test_backfill` 이 원문을 읽어
        #   잠그므로, 안 이름과 겹치는 낱말은 설명에서도 피한다.
        raise BackfillRuleMissing(
            "sim_runs.config_json 에 backfill 칸이 없다 — 없는 규칙을 지어내지 않는다"
        )
    if not isinstance(section, Mapping):
        raise BackfillRuleMissing(
            f"backfill 칸이 객체가 아니다: {type(section).__name__} — 규칙을 읽을 수 없다"
        )
    if not any(key in section for key in _SECTION_KEYS):
        # 🔴 옛 평면 모양이 여기서 터진다. 조용히 매입으로 접으면 **둘 중 어느
        #   규칙으로 돌았는지가 갈린다.**
        raise BackfillRuleMissing(
            "backfill 칸이 사이클별로 가른 모양이어야 한다"
            f" — 아는 칸은 {', '.join(_SECTION_KEYS)} 이고 받은 칸은 {sorted(section)} 다"
        )
    return BackfillRules(
        procurement=_read_procurement_rule(section.get(PROCUREMENT_RULES_KEY))
        if PROCUREMENT_RULES_KEY in section
        else None,
        sales=_read_sales_rule(section.get(SALES_RULES_KEY))
        if SALES_RULES_KEY in section
        else None,
        sales_terms=_read_sales_terms(section.get(SALES_TERMS_KEY))
        if SALES_TERMS_KEY in section
        else None,
    )


def _cycle_section(raw: Any, key: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise BackfillRuleMissing(
            f"backfill.{key} 칸이 객체가 아니다: {type(raw).__name__} — 규칙을 읽을 수 없다"
        )
    return raw


def _fixed_name(section: Mapping[str, Any], key: str, known: str) -> str:
    name = section.get("rule")
    if name != known:
        raise BackfillRuleMissing(
            f"모르는 백필 규칙이다: {name!r} (backfill.{key}) — 아는 규칙은 {known} 하나다"
        )
    return name


def _fixed_pick(section: Mapping[str, Any], field: str, rule_name: str) -> str:
    """그 규칙이 늘 고르는 이름. 🔴 **비면 코드가 채울 자리가 생긴다.**"""
    picked = section.get(field)
    if not isinstance(picked, str) or not picked.strip():
        raise BackfillRuleMissing(
            f"{rule_name} 은 고를 것을 설정이 말해야 한다 — {field} 이(가) 비었다"
        )
    return picked


def _read_procurement_rule(raw: Any) -> ProcurementRule:
    """매입 규칙 한 칸. **아는 이름이 둘이고, 이름이 모양을 정한다.**

    ```text
    ALWAYS_BASE     scenario_label  한 라벨을 늘 고른다
    FIRST_OFFERED   scenario_labels 적어 둔 순서대로 먼저 있는 것
    ```

    🔴 **이름으로 가른다 — 칸의 유무로 짐작하지 않는다.** `scenario_labels` 가 있으면
      새 규칙으로 읽는 식이면, 이름은 옛것인데 모양은 새것인 파일이 조용히 돌고
      **실행 이력에 적히는 규칙 이름이 실제로 돈 규칙과 갈린다.**
    """
    section = _cycle_section(raw, PROCUREMENT_RULES_KEY)
    name = section.get("rule")
    if name == ALWAYS_BASE:
        return BackfillRule(
            name=name, scenario_label=_fixed_pick(section, "scenario_label", ALWAYS_BASE)
        )
    if name == FIRST_OFFERED:
        return OrderedLabelRule(
            name=name, scenario_labels=_ordered_labels(section)
        )
    raise BackfillRuleMissing(
        f"모르는 백필 규칙이다: {name!r} (backfill.{PROCUREMENT_RULES_KEY}) —"
        f" 아는 규칙은 {', '.join(_PROCUREMENT_RULE_NAMES)} 다"
    )


def _ordered_labels(section: Mapping[str, Any]) -> tuple[str, ...]:
    """`FIRST_OFFERED` 가 찾아볼 라벨을 **적힌 순서 그대로.**

    🔴 **여기서 정렬하지도 중복을 지우지도 않는다.** 순서의 주인은 설정 파일이고,
      코드가 손대면 `["기본", "보수"]` 로 뒤집어 쓴 사람이 **안 뒤집힌 결과**를 받는다.

    🔴 **비면 코드가 채울 자리가 생긴다** (`_fixed_pick` 과 같은 규율). 빈 배열은
      *"아무 라벨이나"* 가 아니라 **규칙이 안 선 것**이다.

    ⚠️ **문자열 하나를 배열로 안 접는다.** `"보수"` 를 적으면 파이썬이 글자 하나씩
      도는 순회 가능 객체라 `("보", "수")` 가 되고, 그러면 **있지도 않은 라벨 둘**을
      찾다가 `LABEL_NOT_OFFERED` 로 끝난다 — 터지는 편이 낫다.
    """
    raw = section.get("scenario_labels")
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise BackfillRuleMissing(
            f"{FIRST_OFFERED} 은 찾을 순서를 배열로 말해야 한다 —"
            f" scenario_labels 가 {type(raw).__name__} 다"
        )
    labels = tuple(one for one in raw if isinstance(one, str) and one.strip())
    if len(labels) != len(raw) or not labels:
        raise BackfillRuleMissing(
            f"{FIRST_OFFERED} 은 고를 것을 설정이 말해야 한다 —"
            f" scenario_labels 에 빈 값이 있거나 비었다: {raw!r}"
        )
    return labels


def _read_sales_rule(raw: Any) -> SalesBackfillRule:
    section = _cycle_section(raw, SALES_RULES_KEY)
    name = _fixed_name(section, SALES_RULES_KEY, ALWAYS_FIXED_TYPE)
    return SalesBackfillRule(
        name=name, scenario_type=_fixed_pick(section, "scenario_type", ALWAYS_FIXED_TYPE)
    )


def _terms_text(section: Mapping[str, Any], field: str) -> str:
    """조건 한 칸의 문자열. 🔴 **비면 코드가 채울 자리가 생긴다.**"""
    value = section.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BackfillRuleMissing(
            f"{SALES_TERMS_KEY} 은 {field} 을(를) 설정이 말해야 한다 — 코드가 채우지 않는다"
        )
    return value


def _terms_days(section: Mapping[str, Any], field: str) -> int:
    """지급일수 한 칸. ★ **0 을 안 접는다** — *"당일 수금"* 은 정해진 조건이다.

    🔴 **`bool` 을 숫자로 안 센다.** 파이썬에서 `True` 는 `int` 라 그냥 두면 1일이
      되고, 규칙 파일에 `true` 를 적은 사람이 **하루 유예**를 받는다.
    """
    value = section.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackfillRuleMissing(
            f"{SALES_TERMS_KEY}.{field} 은 0 이상의 정수여야 한다: {value!r}"
        )
    return value


def _read_sales_terms(raw: Any) -> SalesTermsRule:
    """`backfill.sales_terms` 칸. **적어 뒀으면 네 칸이 전부 서야 한다.**

    🔴 **여기서 기본값을 지어내지 않는다.** 거래처를 안 적었는데 코드가 하나
      고르면, 규칙을 안 적은 사람이 **모르는 거래처에 판다.** 그래서 빈 칸은
      기본값이 아니라 **오류**이고, 그 오류는 실행을 여는 자리에서 난다
      (`sim_run_runner._config_json` 이 `read_rules` 를 부른다) — 179일을 걷고 나서
      알면 늦다.

    ⚠️ **칸을 아예 안 적은 것은 여기 안 온다.** 그것은 *"조건을 안 싣는다"* 이고
      종전 동작 그대로다 (`read_rules` 가 `None` 으로 둔다).
    """
    section = _cycle_section(raw, SALES_TERMS_KEY)
    source = _terms_text(section, "unit_price_source")
    if source not in _UNIT_PRICE_SOURCES:
        raise BackfillRuleMissing(
            f"모르는 단가 출처다: {source!r}"
            f" ({SALES_TERMS_KEY}) — 아는 것은 {', '.join(_UNIT_PRICE_SOURCES)} 둘이다"
        )
    if source != FIXED_UNIT_PRICE and "unit_price_krw" in section:
        # 🔴 단가가 둘이면 어느 쪽을 썼는지가 `source_ref` 와 갈린다.
        raise BackfillRuleMissing(
            f"{source} 인데 unit_price_krw 도 적혀 있다 — 단가의 주인이 둘이 된다"
        )
    return SalesTermsRule(
        partner_id=_terms_text(section, "partner_id"),
        payment_terms_type=_terms_text(section, "payment_terms_type"),
        payment_days=_terms_days(section, "payment_days"),
        unit_price_source=source,
        unit_price_krw=_fixed_unit_price(section) if source == FIXED_UNIT_PRICE else None,
    )


def _fixed_unit_price(section: Mapping[str, Any]) -> Decimal:
    """`FIXED` 가 쓸 고정 단가.

    🔴 **`ML_CURRENT_PRICE` 와 같이 적지 못하게 한다** — 단가가 둘이면 어느 쪽을
      썼는지가 `source_ref` 와 갈린다.
    """
    raw = section.get("unit_price_krw")
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise BackfillRuleMissing(
            f"{FIXED_UNIT_PRICE} 은 unit_price_krw 를 설정이 말해야 한다: {raw!r}"
        )
    try:
        price = Decimal(str(raw))
    except InvalidOperation as exc:
        raise BackfillRuleMissing(
            f"{SALES_TERMS_KEY}.unit_price_krw 를 숫자로 못 읽는다: {raw!r}"
        ) from exc
    if price <= 0:
        raise BackfillRuleMissing(
            f"{SALES_TERMS_KEY}.unit_price_krw 는 0 보다 커야 한다: {raw!r}"
        )
    return price


def _config_of(sim_run_id: str) -> Mapping[str, Any]:
    """그 실행의 `config_json`.

    ★ **`sim_runs` 를 읽는 주인을 하나 더 만들지 않는다.** `ledger_repository` 가
      이미 그 행을 읽는다 — 여기에 SELECT 를 또 적으면 컬럼이 바뀌는 날 두 곳이
      어긋난다.
    """
    run = get_burn_in(sim_run_id).get("run")
    if not isinstance(run, Mapping):
        return {}
    config = run.get("config_json")
    return config if isinstance(config, Mapping) else {}


def read_run_rules(
    sim_run_id: str,
    *,
    load_config: Callable[[str], Mapping[str, Any]] = _config_of,
) -> BackfillRules:
    """그 실행이 정한 규칙을 **행을 하나도 안 보고** 읽는다 (2026-09-11).

    ★ **왜 있나.** 걷기가 `--auto-approve` 를 받으면 **걷기 전에** 규칙이 있는지
      물어야 한다 — 없는 채로 179일을 걸으면 사람이 *"승인이 돌았는데 0건이구나"*
      로 읽고, 그때는 이미 하루도 되돌릴 수 없다.

    🔴 **읽는 방법의 주인을 둘로 만들지 않으려고 여기 둔다.** 부르는 쪽이
      `get_burn_in(...)["run"]["config_json"]` 을 제 손으로 파면 컬럼이 바뀌는 날
      백필과 걷기가 서로 다른 자리를 보게 된다.

    :raises BackfillRuleMissing: 규칙 칸이 없거나 아는 모양이 아닐 때.
        🔴 **여기서는 값으로 안 접는다** — `backfill_decisions` 는 이것을 잡아
        `NO_RULE` 로 담지만, 그쪽은 *"걸었는데 할 것이 없었다"* 를 말해야 하고
        이쪽은 *"걷기 전에 막는다"* 를 말해야 한다.
    """
    return read_rules(load_config(sim_run_id))


def backfill_decisions(
    *,
    sim_run_id: str,
    start: date,
    end: date,
    load_config: Callable[[str], Mapping[str, Any]] = _config_of,
    runs_on: Callable[..., Sequence[Mapping[str, Any]]] = list_runs,
    decisions_of: Callable[[str], Sequence[DecisionOut]] = list_decisions,
    decide: Callable[[str, DecisionIn], DecisionOut] = record_decision,
    limit_per_day: int = 500,
) -> BackfillOut:
    """`start` 부터 `end` 까지, 규칙이 가리키는 안을 **승인 문으로** 승인한다.

    ★★ **승인 문을 우회하지 않는다.** `record_decision` 을 그대로 부른다 —
      `save_decision` 을 직접 부르거나 재검증을 건너뛰면 그 순간 **「승인」이 두
      종류**가 된다. 백필 승인도 사람 승인과 같은 검사 · 같은 재검증 · 같은 이력을
      지나야 한다. 느려도 그것이 맞다.

    ⚠️ 재검증이 벽시계로 돌지 않는다 — `record_decision` 안쪽의 `_revalidation_for`
      가 **그 실행 행의 `as_of`** 를 쓴다. 그래서 과거 구간을 오늘 재현해도 오늘로
      개장을 묻지 않는다.

    :param load_config: 규칙을 읽는 자리. 기본은 `sim_runs.config_json`.
    :param runs_on: 하루치 실행 이력. `list_runs` 와 같은 키워드로 부른다.
    :param decisions_of: 그 업무 키에 이미 붙은 결정.
    :param decide: 🔴 **승인 문.** 기본값이 `record_decision` 자체다 — `None` 을
        안 받는다 (`backtest_runner.walk` 의 `run_day_fn` 과 같은 규율).
    :raises ValueError: 범위가 거꾸로일 때. **막고 사유를 낸다.**
    :raises LookupError: 그 실행을 못 찾아 규칙을 **읽지도 못했을** 때.
        🔴 *"규칙이 없다"* 와 섞지 않는다 — 저쪽은 값(`NO_RULE`)이고 이쪽은 사고다.
    """
    if start > end:
        raise ValueError(
            f"백필 범위가 거꾸로다: {start.isoformat()} ~ {end.isoformat()}"
            " — 어느 쪽이 시작인지를 여기서 정하지 않는다"
        )

    try:
        rules = read_rules(load_config(sim_run_id))
    except BackfillRuleMissing as exc:
        # 🔴 **한 행도 안 쓰고 돌아간다.** `decide` 를 한 번도 안 부른다.
        return BackfillOut(
            sim_run_id=sim_run_id, start=start, end=end, status="NO_RULE", reason=str(exc)
        )

    results: list[BackfilledRun] = []
    blocked: list[date] = []

    day = start
    while day <= end:
        if day > BACKFILL_BOUNDARY_AS_OF:
            blocked.append(day)
        for row in runs_on(sim_run_id=sim_run_id, as_of=day, limit=limit_per_day):
            results.append(_backfill_one(row, rules, decisions_of=decisions_of, decide=decide))
        day += timedelta(days=1)

    return BackfillOut(
        sim_run_id=sim_run_id,
        start=start,
        end=end,
        status="RAN",
        rules=rules,
        runs=tuple(results),
        blocked_days=tuple(blocked),
    )


def _backfill_one(
    row: Mapping[str, Any],
    rules: BackfillRules,
    *,
    decisions_of: Callable[[str], Sequence[DecisionOut]],
    decide: Callable[[str, DecisionIn], DecisionOut],
) -> BackfilledRun:
    """실행 이력 한 행을 규칙대로 처리한다. **어느 안이 나은지 안 따진다.**"""
    as_of = row["as_of"]
    run_id = str(row["run_id"])
    request_id = row.get("request_id")
    cycle = row.get("cycle") if isinstance(row.get("cycle"), str) else ""

    def 결과(outcome: BackfillOutcome, reason: str | None = None) -> BackfilledRun:
        return BackfilledRun(
            as_of=as_of,
            run_id=run_id,
            request_id=request_id,
            outcome=outcome,
            reason=reason,
        )

    # ── ① 경계 ──────────────────────────────────────────────────────
    # 🔴 **가장 먼저 본다.** 뒤로 밀면 그 앞 검사가 하나 바뀌는 날 경계가 새 나간다.
    #    루프가 아니라 **행의 `as_of`** 로 잰다 — 조회 필터가 무엇을 걸었든 가드는
    #    자기 눈으로 본다.
    if as_of > BACKFILL_BOUNDARY_AS_OF:
        return 결과(
            "BLOCKED_BY_BOUNDARY",
            f"{as_of.isoformat()} 은 백필 경계({BACKFILL_BOUNDARY_AS_OF.isoformat()}) 밖이다"
            " — 그 뒤는 사람만 승인한다",
        )

    # ── ② 승인이 성립하는 실행인가 ──────────────────────────────────
    # ★ 어느 종료 코드에 승인이 서는지는 `decision.approve_end_codes` 가 주인이다.
    #   여기에 코드를 베끼면 그쪽이 바뀌는 날 백필만 옛 규칙으로 돈다.
    #
    # 🔴 **사이클을 여기서 다시 가르지 않는다.** `approve_end_codes` 가 이미 사이클별로
    #   답한다 — 그 앞에 사이클 조건을 하나 더 놓으면 어느 종료 코드에 승인이 서는지의
    #   주인이 둘이 된다.
    end_code = row.get("end_code")
    if end_code not in approve_end_codes(cycle):
        return 결과("NOT_APPROVABLE", f"종료 코드가 {end_code!r} 다 (cycle={cycle!r})")

    # ── ③ 그 사이클 규칙을 설정이 정했나 ────────────────────────────
    # 🔴 **`NO_RULE` 로 접지 않는다.** 실행 전체가 규칙 없이 돈 것과, 이 사이클만
    #   안 정한 것은 다른 사실이다 — 뒤엣것은 의도일 수 있다.
    rule = rules.for_cycle(cycle)
    if rule is None:
        return 결과("NO_RULE_FOR_CYCLE", f"{cycle!r} 사이클 백필 규칙을 설정이 안 정했다")

    if not isinstance(request_id, str) or not request_id:
        return 결과("FAILED", "실행 행에 업무 키가 없어 승인 문을 부를 수 없다")

    # ── ④ 이미 결정이 있으면 덮지 않는다 ────────────────────────────
    if decisions_of(request_id):
        return 결과("ALREADY_DECIDED", "이미 붙은 결정이 있다 — 자동이 사람 결정을 덮지 않는다")

    # ── ⑤ 규칙이 가리키는 안이 그날 있었나 ──────────────────────────
    response_payload = row.get("response_payload") or {}

    # 🔴 **승인 문과 같은 눈으로 본다.** `check_scenario_exists` 가 이 목록으로
    #   검사하므로, 여기서 안 맞추면 승인 문이 터져 `FAILED` 로 남고 *"그날 그 안이
    #   없었다"* 는 사실이 사고로 뭉개진다.
    available = available_scenario_names(response_payload, cycle)
    찾은순서: tuple[str, ...] = ()

    if isinstance(rule, SalesBackfillRule):
        # 🔴 **축으로 찾되, 둘 이상이면 안 고른다.** 첫 번째를 고르면 배열 순서가
        #   선택 규칙이 되고, 판매가 그것을 쓰지 않기로 명시했다.
        matched = scenario_ids_of_type(response_payload, rule.scenario_type)
        if len(matched) > 1:
            return 결과(
                "AMBIGUOUS_TYPE",
                f"규칙이 가리키는 축의 후보가 {len(matched)} 개다 — 규칙이 어느 것인지"
                " 말하지 않아 안 고른다",
            )
        picked = matched[0] if matched else None
    else:
        # 🟢 **적힌 순서대로 찾아 먼저 있는 것** (2026-09-11 · `FIRST_OFFERED`).
        #    `ALWAYS_BASE` 는 그 순서가 하나뿐이라 전과 똑같이 돈다.
        #
        # 🔴 **순서를 여기서 정하지 않는다.** `labels_in_order` 가 설정이 적은 그대로를
        #    낸다 — 이 줄이 정렬하거나 뒤집으면 규칙의 주인이 코드가 된다.
        #
        # 🔴 **그래도 대체가 아니다.** 사람이 *"보수 우선, 없으면 기본"* 이라고 **적어
        #    둔 것**을 따르는 것이고, 안 적힌 라벨은 여전히 안 고른다.
        찾은순서 = rule.labels_in_order
        picked = next((label for label in 찾은순서 if label in available), None)

    # 🔴 **없으면 다른 안으로 대체하지 않는다.** 대체하면 곡선이 규칙과 다른 것을
    #   재현하고, *"규칙이 정한 안을 늘 고른 곡선"* 이라는 발표 문장이 거짓이 된다.
    if picked is None or picked not in available:
        shown = ", ".join(available) if available else "(없음)"
        # ★ **「하나를 못 찾았다」와 「둘 다 못 찾았다」는 다른 사실이다.** 찾아본
        #   순서를 사유에 적어 두면, 규칙을 늘렸는데도 안 섰다는 것이 그 줄로 보인다.
        #
        # ⚠️ **판매에는 이 절이 없다.** 저쪽은 라벨이 아니라 **축**으로 찾으므로
        #   *"찾은 순서"* 라는 말 자체가 없다 — 없는 개념을 빈 값으로 적지 않는다.
        순서절 = f"찾은 순서: {' → '.join(찾은순서)} · " if 찾은순서 else ""
        return 결과(
            "LABEL_NOT_OFFERED",
            f"규칙이 가리키는 안이 그날 없다 ({순서절}제시된 안: {shown})",
        )

    # ── ⑥ 승인 문 ───────────────────────────────────────────────────
    try:
        saved = decide(
            request_id,
            DecisionIn(
                decision="APPROVE",
                # ⚠️ 판매에서는 이 칸에 `scenario_id` 가 실린다 — 칸 이름과 값이
                #    어긋나는 자리이고, 그렇게 하기로 판매가 정했다.
                scenario_label=picked,
                decided_by=AUTO_BACKFILL,
                history_run_id=run_id,
                # 🟡 되짚을 때 눈으로 보라는 것뿐이다. ⚠️ **파싱하지 않는다** —
                #    자유 텍스트라 규칙 이름의 주인이 아니다. 주인은 `config_json` 이다.
                note=f"{AUTO_BACKFILL} rule={rule.name}",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 한 날이 막혀도 나머지 날은 채운다.
        return 결과("FAILED", f"{type(exc).__name__}: {exc}")

    return BackfilledRun(
        as_of=as_of,
        run_id=run_id,
        request_id=request_id,
        outcome="RECORDED",
        revalidation_outcome=saved.revalidation_outcome,
        # 🔴 **확정 결과를 여기서 버리지 않는다** (2026-09-11). 전에는 `saved.sale`
        #    을 안 읽어서, 확정이 `BLOCKED` 로 막혀도 성적표에는 `RECORDED` 하나만
        #    남았다 — *"승인이 적혔다"* 가 *"판매가 섰다"* 로 읽혔다.
        confirmation_status=None if saved.sale is None else saved.sale.status,
        confirmation_reason=None if saved.sale is None else (saved.sale.reason or None),
        # 🔴 **확정이 재고를 잡았는지를 여기서 버리지 않는다** (2026-09-12).
        #    `CONFIRMED` 만 세면 *"팔렸다"* 가 *"잡아 뒀다"* 로 읽히고, 그 사이에서
        #    같은 재고가 다음 날 또 팔린다.
        reservation_outcome=None if saved.sale is None else saved.sale.reservation_outcome,
        # 🔴 **순서에서 실제로 고른 것을 남긴다.** 규칙 파일에 적힌 순서와 그날 선
        #    라벨은 다른 사실이다 — 규칙만 보고 곡선을 읽으면 틀린다.
        #
        # ★ **판매는 `None` 이다** (`찾은순서` 가 비어 있다). 저쪽 `picked` 는
        #   라벨이 아니라 후보 `scenario_id` 라 라벨 어휘에 섞으면 안 된다.
        picked_label=picked if 찾은순서 else None,
    )
