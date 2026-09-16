"""
revalidation.py — **최종 승인 시점 재검증** (설계 2026-09-07 · M-4)

승인 클릭 하나가 부서를 다시 부른다. 순서가 한 칸 늘어난 것이 이 조각이다.

```text
지금까지   읽고 → 검사하고 → 적재한다
이제부터   읽고 → 검사하고 → 🔴 재검증하고 → 적재한다
```

★ **왜 자기 모듈인가.** `decision_service.py` 는 *"사람이 무엇을 눌렀나"* 를 적는
  자리이고 여기는 *"그 사이 바뀌었는가"* 를 묻는 자리다. 한 파일에 두면 결정 적재가
  부서 호출·개장 관문·호출 예산까지 들고 있게 된다.

  🔴 **`service.py` 에 넣을 수도 없다.** 저쪽은 이미 `decision_service` 를 임포트하는데
    결정 적재가 재검증을 부르므로, 같은 파일에 두면 import 가 원을 그린다.

🔴 **S-1(기여 호출 재사용)을 쓰지 않는다.** 판매 Flow 의 `_judge` 는 라우팅이 ②와
  같으면 그 회신을 다시 쓰는데(같은 `as_of`·같은 요청 안이므로), **여기서는 그것이
  틀린 규칙이다.** 목적이 *"그 사이 바뀌었는가"* 라 재사용하면 바뀐 것을 못 보고
  통과시킨다 — 재검증을 하는 이유 자체가 없어진다 (설계 §1 · `sales_flow._judge` 의
  경고 그대로).

  ★ 그래서 이 모듈은 **원 실행의 회신을 결과로 쓰지 않는다.** 원 실행에서 읽는 것은
    *"무엇을 검증받아야 하는가"* 와 *"그때 조건이 무엇이었나"* 둘뿐이고, **판정은
    전부 이번 호출에서 나온다.**

🔴 **`as_of` 는 원 실행의 날이 아니라 지금 고르는 날이다.** 그것이 *"그 사이
  바뀌었는가"* 의 뜻이다. 그래서 **개장 Gate 를 또 지난다**: 그날이 안 열렸으면
  재검증을 못 한다.

  ★ **그 날짜를 이 파일이 만들지 않는다** (2026-09-09 · `#452`). 진입점이 정해서
    넘기고 여기는 받은 것을 흘린다 — 아래 `revalidate_scenario` 가 왜 그렇게
    바뀌었는지를 적어 둔다.

★ **`PASSED` 가 나와도 아무 일도 안 일어난다.** 승인의 효력을 도메인 Write 로 흘리는
  것은 M-5 이고, 그 앞에 실행 원장(saga) 문제가 있다 (설계 §5).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.master import wiring
from app.master.answer import agent_label
from app.master.budget import BudgetExhausted, CallBudget
from app.master.day_gate import check_day_gate
from app.master.decision import PROCUREMENT_CYCLE, SALES_CYCLE, RevalidationOutcome
from app.master.envelope import (
    PASSING_VERDICTS,
    AgentName,
    AgentReply,
    Capability,
    ExecutionContext,
    Mode,
    route_capability,
    wire_adjustment,
    wire_payload,
)
from app.master.flow import ADVISORS
from app.master.persistence import record_revalidation
from app.master.ports import AgentNotRegistered
from app.master.runner import MasterRunner

__all__ = [
    "REQUIRED_CAPABILITIES",
    "REVALIDATION_BUDGET",
    "Revalidation",
    "conditions_of",
    "conditions_of_original",
    "find_scenario",
    "make_revalidation_request_id",
    "procurement_validation_payload",
    "revalidate_procurement_scenario",
    "revalidate_scenario",
]


#: 🔴 **라우팅은 열렸지만 재검증에서는 안 부르는 capability.**
#:
#: `ADDITIONAL_SUPPLY_CONTEXT` 가 그 자리다. 판매 Flow 는 부족량과 매입용 경계를
#: 골라 담아 보내는데(`sales_flow._supply_capacity_input`), 여기는 **후보를 그대로**
#: 보낸다(`:280`). 그대로 보내면 매입이 부족량도 경계도 못 읽어 `basis=unknown` 으로
#: 답한다.
#:
#: ⚠️ **그 답은 화면에서 *"못 물어봤다"* 로 보이는데 실제로는 *"잘못 물어봤다"* 다.**
#:   호출 예산을 한 번 쓰고 어휘가 거짓말을 한다 — 둘 다 손해다.
#:
#: 🟢 그래서 **안 부르고 `unroutable` 로 남긴다.** 라우팅이 열리기 전과 화면이 같고,
#:   *"이 검증은 안 왔다"* 는 사실 그대로다.
#:
#: 🔴 **이것을 지우려면 같은 배선을 여기에도 옮겨야 한다** — 부족량과 경계를 골라
#:   담는 자리를 만들고 나서다. 지금 지우면 위 문단의 거짓말이 그대로 돌아온다.
_NOT_REVALIDATED: frozenset[str] = frozenset({"ADDITIONAL_SUPPLY_CONTEXT"})

REQUIRED_CAPABILITIES: tuple[Capability, ...] = (
    "SELLABLE_SUPPLY_CONTEXT",
    "FINANCIAL_VALIDATION",
)
"""재검증이 **반드시** 받아야 하는 검증 (설계 §1 · 2026-09-04 판매 합의).

```text
SELLABLE_SUPPLY_CONTEXT   아직 팔 수 있는 물건이 있는가
FINANCIAL_VALIDATION      아직 돈이 되는가
```

★ **후보가 요구했든 안 했든 부른다.** 후보의 `required_validations` 는 *"이 안을
  제시하려면 무엇이 필요한가"* 이고, 이 둘은 *"승인 직전에 무엇을 다시 봐야 하는가"*
  다 — 물음이 다르므로 목록도 다르다.

⚠️ **`REQUIRED_FOR_SALES`(어댑터 목록)와 다른 것이다.** 저쪽은 `(sales, finance)` 로
  **제안자**를 포함한다. 재검증은 후보를 다시 만들지 않으므로 제안자를 안 부른다 —
  부르면 사용자가 고른 안이 아닌 다른 안이 나온다.
"""

REVALIDATION_BUDGET = 4
"""재검증 한 번의 호출 예산.

```text
필수  SELLABLE_SUPPLY_CONTEXT · FINANCIAL_VALIDATION      2
조건부 후보가 요구한 나머지 (어휘가 넷이라 최대 2 가 더 붙는다)  2
──────────────────────────────────────────────────────────
                                                          4
```

🔴 **`SALES_BUDGET`(25) 을 그대로 쓰지 않는다.** 저쪽은 *"후보 3 · 되먹임 2회"* 를
  전제로 센 값이고, 재검증은 **후보 하나에 되먹임이 없다.** 남의 예산을 빌려 쓰면
  여기서 몇 번을 부르는지가 아무 데도 안 적히고, 그 사이 판매 예산이 바뀌면 재검증의
  상한이 이유 없이 따라 움직인다.

★ **소진은 `ERROR` 다.** *"다 봤는데 안 된다"* 가 아니라 *"다 못 봤다"* 이므로
  `FAILED` 와 갈라 둔다 (판매가 `SL5` 를 `SL3` 으로 안 접는 것과 같은 판단).
"""


_REVALIDATION_KEY_PREFIX = "REV"
"""재검증 실행의 업무 키 접두. **`REQ` 와 다른 글자여야 한다.**

🔴 **`make_request_id` 를 그대로 쓰면 원 실행과 같은 키가 나온다.** 저쪽은
  `REQ-{날짜}-{순번}` 인데, 같은 날 첫 실행을 승인하면 `REQ-20260907-0001` 이 되어
  **재검증 키가 원 실행을 가리킨다.** 그러면 `revalidation_request_id` 가 무엇을
  가리키는지 아무도 못 푼다.

★ 조회와 매입이 같은 업무 키를 써서 `cycle` 로 갈라야 했던 자리
  (`persistence.record_status` 의 ⚠️)를 되풀이하지 않는다 — 여기는 애초에 안 겹치게
  둔다.
"""


def make_revalidation_request_id(sim_run_id: str, as_of: date, decision_seq: int) -> str:
    """`REV-SIM-WALK-2026-V4-20260907-0001`. **실행 축 + 날짜 + 결정 회차다.**

    ★ `make_request_id` 와 같은 규율이다 (§1.2-11) — 같은 날 재검증을 구분하되
      **재현 가능해야 한다.** 순번을 결정 회차로 두면 번복(`decision_seq` 2, 3 …)이
      각자 다른 키를 받고, 같은 승인을 두 번 처리해도 같은 키가 나온다.

    🔴 **축이 키에 실린다** (2026-09-11). 전에는 `REV-20260907-0001` 이라 **실행이
      달라도 같은 날 같은 회차면 키가 겹쳤다.** 실측에서 업무 키
      `REV-20260106-0001` 하나에 **여러 실행의 행 20건**이 쌓여 있었다 — 그 키로는
      *"어느 실행의 재검증인가"* 를 아무도 못 푼다.

    ★ **축을 첫 위치 인자로 둔다.** 인자가 하나 늘었으므로 옛 호출부가 조용히
      통과하지 않고 그 자리에서 터진다.

    ★ **자리는 머리 쪽(접두 바로 뒤)이다.** 고를 수 있었던 이유가 실측이다 —
      저장소 전체에 `REV-` 키의 **접두 조회도 꼬리 조회도 한 곳도 없다**
      (2026-09-11 전수 확인). 그래서 `run_repository.build_request_id` 의
      `{머리}-{실행}-{날짜}-{꼬리}` 와 **같은 모양**으로 맞췄다. 🔴 다음 사람이
      `REV-` 조회를 더한다면 이 자리가 이미 정해져 있다는 것을 먼저 보라.

    ⚠️ `master_decisions.revalidation_request_id` 는 `text` 라 길이 제한이 없다
      (2026-09-11 실 DB 확인) — 키가 길어져도 마이그레이션이 필요 없다.
    """
    return f"{_REVALIDATION_KEY_PREFIX}-{sim_run_id}-{as_of.strftime('%Y%m%d')}-{decision_seq:04d}"


@dataclass(frozen=True)
class Revalidation:
    """재검증 한 번의 결과. **결정 행에 그대로 실린다.**

    ★ **두 칸을 한 객체로 낸다.** `revalidation_request_id` 와
      `revalidation_outcome` 은 DB 의 짝 CHECK(`master_decisions_revalidation_pairing`)
      가 묶어 둔 한 사실이라, 따로 돌려주면 한쪽만 채워지는 날이 온다.
    """

    outcome: RevalidationOutcome

    #: 재검증이 **실제로 돈** 실행의 업무 키. 🔴 못 돌린 `ERROR` 에서는 `None` 이다 —
    #: 짝 CHECK 가 그 조합만 예외로 열어 두었고, **가짜 키를 지어 넣지 않기 위해서다.**
    request_id: str | None = None

    #: 사람이 읽는 한 줄. 부서가 쓴 문장을 옮기거나 못 돈 이유를 적는다.
    reason: str = ""

    #: 🔴 **원 실행에 없던 조건의 표지 원문** (`conditions_of` 가 만든 그대로).
    #:
    #: ★ **`reason` 과 다투는 칸이 아니다.** 둘은 묻는 사람이 다르다.
    #:
    #:   ```text
    #:   reason      사람이 읽는다     「물류: 수량을 7,470kg 로 조정 제안」
    #:   conditions  기계가 되만든다   adjust:{dept·axis·target_value·unit·…}
    #:   ```
    #:
    #: 🔴 **이 칸이 없으면 표지 원문이 영영 사라진다** (2026-09-16). 전에는 표지를
    #:   `reason` 문장에 이어 붙여 **그 문자열이 유일한 사본**이었는데, 그 문장을
    #:   사람 말로 고치는 순간 원문이 어디에도 안 남는다:
    #:
    #:   ```text
    #:   validations[cap]   `_verdict_of` 가 담는 칸에 suggested_adjustments 가 없다
    #:                      (봉투에서 payload 의 **형제**라 payload 에도 안 들어온다)
    #:   plan(ExecutionStep) 조정 제안 칸 자체가 없다
    #:   master_decisions   revalidation_request_id · revalidation_outcome 뿐이다
    #:   로그                이 모듈에도 `runner` 에도 로거가 없다
    #:   ```
    #:
    #: 🔴 **발표 뒤 개발이 없다. 지금 안 남기면 영영 못 되만든다.**
    #:
    #: 🔴 **빈 자리가 아니다** — 30회 실행에 조정 45건이 실측됐다 (충환님 2026-09-16).
    #:   살아 있는 데이터를 사람 말로 덮는 것이라 잃으면 티가 난다.
    #:
    #: ★ **`verdict:` 표지도 같이 싣는다.** `validations[cap].business_status` 로
    #:   되만들 수는 있지만 **되만들 수 있다는 것과 남아 있다는 것은 다르다** —
    #:   되만드는 규칙(`conditions_of`)이 바뀌는 날 두 값이 갈린다.
    #:
    #: ★ **오늘 세 파트가 각자 세운 같은 선이다.** 매입은 「확정 입고 예정」을 `0` 이
    #:   아니라 `—` + `raw=None` 으로 냈고(`#740`), 재무는 운영비 축을 `None` =
    #:   「기록 없음」으로 두었다. **사람 말과 정본을 나란히 둔다** — 사람 말이
    #:   정본을 덮지 않는다.
    conditions: tuple[str, ...] = ()

    #: capability → 이번 호출의 판정. **원 실행 회신이 아니다** (S-1 금지).
    validations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    #: 🔴 부를 대상이 없어 못 물어본 요구. 조용히 버리지 않는다 (§1.2-10).
    unroutable: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def revalidate_scenario(
    *,
    scenario: Mapping[str, Any],
    original_conditions: frozenset[str],
    decision_seq: int,
    policy_version: str,
    as_of: date,
    sim_run_id: str,
    item: str | None = None,
) -> Revalidation:
    """선택된 **1안만** 고르는 그날로 다시 검증한다.

    ```text
    ① 개장 Gate      오늘이 안 열렸으면 못 돈다        → ERROR
    ② 어댑터 점검     필수 capability 를 부를 수 없다  → ERROR
    ③ 호출           필수 + 후보가 요구했던 나머지
    ④ 매핑           PASSED · CONDITIONAL · FAILED
    ⑤ 이력 적재       master_agent_runs (cycle=SALES)
    ```

    🔴 **판매 안 전용이다** (2026-09-16). 매입 안은 `revalidate_procurement_scenario` 로
      간다. 매입 안을 여기 넣으면 재무 `SALES_VALIDATION` 이 판매 사실을 못 찾아
      `INPUT_INCOMPLETE`(READY/skipped) 로 답하고, 그 skipped 가 늘 `FAILED` 로 접힌다.

    🔴 **전체 후보를 다시 돌리지 않는다.** 사용자는 하나를 골랐고, 나머지는 이미 그
      시점의 판단으로 화면에 나갔다 (설계 §1).

    🔴 **개장 Gate 를 지난다.** `ExecutionContext` 를 만드는 자리는 전부 그렇다 —
      안 열린 날 판단이 서면 **막힌 것이 아니라 안 막힌 것**이라 아무 오류도 안 난다.
      `tests/master/test_entrypoint_day_gate.py` 의 스캐너가 이 모듈까지 훑는다.

    🔴 **`as_of` 를 필수 인자로 받는다** (2026-09-09 · `#452`). 전에는 이 자리에서
      `today()` 로 벽시계를 읽었고, 옛 주석이 그 이유를 이렇게 적어 두었다.

      ```text
      ⚠️ `as_of` 를 인자로 받지 않는다. 재검증은 "오늘 어떤가" 를 묻는 사건이라
         날짜가 인자가 되면 부르는 쪽이 원 실행의 날을 넣을 수 있고, 그 순간
         재검증이 아무것도 안 재게 된다.
      ```

      ★ **그 걱정은 옳았다.** 없앤 것이 아니라 **막는 자리를 옮겼다** — 벽시계를
        진입점 하나로 올렸으므로, 이 깊은 자리에서는 아무도 날짜를 지어내지 못하고
        받은 것을 그대로 쓴다.

      ```text
      운영     사람이 고르는 날 = 오늘        router.master_decide 가 clock 을 읽어 넘긴다
               말로 고르는 날                 ask_service 가 그 요청의 as_of 를 넘긴다
      백테스트  정책이 고르는 날 = 걷는 그날    walk 가 그날을 넘긴다
      ```

      🔴 **여기서 시계를 읽으면 백테스트가 무효가 된다.** `2026-03-10` 을 걷는 실행이
        승인 경로를 타는 순간 재검증만 오늘로 답하고, 곡선에 벽시계가 섞인다.
        `tests/master/test_clock_is_the_only_wall_clock.py` 가 이 파일이 `clock` 을
        다시 임포트하지 않는지 지킨다.

      ⚠️ **기본값을 두지 않는다.** 기본값은 곧 업무 규칙이 되고, 안 넘긴 자리가
        조용히 오늘로 답한다. 안 넘기면 터져야 한다.

    ⚠️ **백테스트에서는 제안한 날과 고른 날이 같다.** 그래도 재검증은 돈다 — 그날
      안에 입고 · 수금 · 출고가 지나갔을 수 있고, 재검증이 재는 것은 *"그 사이"* 이지
      *"며칠 지났는가"* 가 아니다.

    🔴 **`sim_run_id` 도 필수 인자로 받는다** (2026-09-11). 전에는 이 자리에서
      `BURN_IN_SIM_RUN_ID` 를 박았고, 그래서 **어느 실행을 재검증하든 늘 번인
      장부**를 읽었다.

      ★ **실측된 피해** (실행 `SIM-SALESCHAIN-20260911`): 재무가 채권·현금을 번인
        장부에서 읽어, 판매를 한 번도 안 한 실행의 재검증이
        `SALES_CREDIT_LIMIT_EXCEEDED` · `BASE_MINIMUM_CASH_VIOLATED` 로 떨어졌다.
        승인 일곱 건이 전부 `FAILED` 이고 `sales` 는 0행인데, 번인 채권 합과
        재검증이 본 AR 이 소수점까지 같았다 (15,752,100.13535).

      ⚠️ **`as_of` 와 같은 규율이다 — 기본값을 두지 않는다.** 기본값은 곧 업무
        규칙이 되고, 안 넘긴 자리가 조용히 번인으로 답한다. 안 넘기면 터져야 한다.

    :param as_of: 이 재검증이 서는 날. 🔴 **원 실행의 날이 아니라 지금 고르는 날이다.**
    :param sim_run_id: 어느 실행의 장부를 읽는가. 🔴 **원 실행 행이 정본이고 여기서
        짓지 않는다** (`decision_service._sim_run_id_of`).
    :param original_conditions: 원 실행에서 그 후보에 붙어 있던 **조건 표지 집합**
        (`conditions_of` 가 만든다). 이번 결과가 이보다 늘면 `CONDITIONAL` 이다.
    """
    request_id = make_revalidation_request_id(sim_run_id, as_of, decision_seq)
    context = ExecutionContext(
        request_id=request_id,
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version=policy_version,
        # ★ 어느 실행의 장부인가는 마스터가 정한다 (물류 `#325`). 🔴 **다만 상수가
        #   아니라 원 실행 행에서 온다** — 부르는 쪽이 읽어 넘긴 값을 그대로 흘린다.
        sim_run_id=sim_run_id,
    )

    # ① 🔴 **첫 관문은 개장이다.** 막히면 재검증이 **실패한** 것이 아니라 **돌리지 못한**
    #    것이라 `FAILED` 와 갈라 `ERROR` 로 적는다.
    #
    #    ★ **관문에도 같은 축을 넘긴다.** 봉투와 관문이 다른 실행을 보면 *"안 열린 날에
    #      판단이 서는"* 자리가 한 함수 안에서 생긴다.
    day_gate = check_day_gate(as_of, sim_run_id=sim_run_id)
    if day_gate.gate == "BLOCKED":
        return Revalidation(
            outcome="ERROR",
            reason=f"재검증할 날({as_of.isoformat()})이 안 열려 재검증을 못 돌렸다: "
            f"{day_gate.reason or day_gate.result}",
        )

    capabilities = _capabilities_for(scenario)
    routes = {capability: route_capability(capability) for capability in capabilities}

    # ② 필수 capability 를 부를 대상이 **등록조차 안 돼 있으면** 못 돈 것이다.
    missing = _missing_for(routes)
    if missing:
        return Revalidation(
            outcome="ERROR",
            reason=f"필수 검증을 부를 어댑터가 없어 재검증을 못 돌렸다: {', '.join(missing)}",
        )

    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=REVALIDATION_BUDGET))
    validations: dict[str, Mapping[str, Any]] = {}
    adjustments: list[Mapping[str, Any]] = []
    unroutable: list[str] = []

    try:
        for capability, route in routes.items():
            if route is None or capability in _NOT_REVALIDATED:
                # 🔴 **조용히 건너뛰지 않는다.** 건너뛰면 *"검증됐다"* 로 읽힌다.
                unroutable.append(capability)
                continue
            agent, mode = route
            # ★ 후보를 **그대로** 보낸다 — `sales_flow._judge` 와 같은 규칙이다.
            #   마스터가 골라 담으면 판매가 필드를 늘린 날 조용히 빠진다.
            reply = runner.call(agent, mode, dict(scenario))
            validations[capability] = _verdict_of(reply)
            adjustments.extend(wire_adjustment(a) for a in reply.suggested_adjustments)
    except BudgetExhausted as exc:
        return _recorded(
            context,
            Revalidation(
                outcome="ERROR",
                request_id=request_id,
                reason=f"호출 예산 소진으로 재검증이 끝나지 않았다: {exc}",
                validations=validations,
                unroutable=tuple(unroutable),
            ),
            runner=runner,
            item=item,
            cycle=SALES_CYCLE,
        )
    except AgentNotRegistered as exc:
        # ★ ②에서 필수는 걸렀지만 **조건부 대상이 빠질 수 있다.** 그때도 못 돈 것이다.
        return _recorded(
            context,
            Revalidation(
                outcome="ERROR",
                request_id=request_id,
                reason=f"에이전트 미등록으로 재검증을 끝내지 못했다: {exc}",
                validations=validations,
                unroutable=tuple(unroutable),
            ),
            runner=runner,
            item=item,
            cycle=SALES_CYCLE,
        )

    outcome, reason, conditions = _verdict(
        validations, tuple(unroutable), adjustments, original_conditions
    )
    return _recorded(
        context,
        Revalidation(
            outcome=outcome,
            request_id=request_id,
            reason=reason,
            conditions=conditions,
            validations=validations,
            unroutable=tuple(unroutable),
        ),
        runner=runner,
        item=item,
        cycle=SALES_CYCLE,
    )


PROCUREMENT_REVALIDATION_MODE: Mode = "SCENARIO_VALIDATION"
"""매입 안 재검증이 조언자에게 묻는 mode. **매입 Flow ④ 와 같은 물음이다** (`flow._validate`)."""


def procurement_validation_payload(
    proposal: Mapping[str, Any], scenario: Mapping[str, Any]
) -> dict[str, Any]:
    """조언자에게 보내는 **매입 제안 한 벌.** 제안 최상위 + 고른 안 하나다.

    ★ **두 부서가 이것을 `PurchaseProposal` 로 되살린다** (`finance/adapter._purchase_proposal`
      · `logistics/adapter._as_proposal`). 그래서 `meta` · `situation` · `confidence`
      같은 최상위 칸이 빠지면 판정 대신 입력 오류가 온다.

    🔴 **이름이 붙은 이유는 검사다** (2026-09-16). 실매입 기록값 사본이 이 모양으로
      안 갈 때 두 부서가 동시에 떨어졌는데, 검사가 이 조립을 손으로 다시 적으면
      **검사와 운영이 다른 모양을 볼 수 있다.** 주인을 하나 둔다.
    """
    return {**proposal, "scenarios": [dict(scenario)]}


def revalidate_procurement_scenario(
    *,
    scenario: Mapping[str, Any],
    proposal: Mapping[str, Any],
    original_conditions: frozenset[str],
    decision_seq: int,
    policy_version: str,
    as_of: date,
    sim_run_id: str,
    item: str | None = None,
) -> Revalidation:
    """매입 안 **1안만** 그 실행의 날로 다시 검증한다 (매입 승인 · 실매입 기록 공용).

    ```text
    ① 개장 Gate      안 열렸으면 못 돈다                  → ERROR
    ② 조언자 점검     재무 · 물류 중 등록 안 된 쪽이 있다    → ERROR
    ③ 호출           조언자마다 SCENARIO_VALIDATION 한 번
    ④ 매핑           PASSED · CONDITIONAL · FAILED (`_verdict` 공용)
    ⑤ 이력 적재       master_agent_runs (cycle=PROCUREMENT)
    ```

    🔴 **판매 capability 로 묻지 않는다** (2026-09-16 실측).
      전에는 매입 승인도 `revalidate_scenario` 를 탔다. 거기서 `FINANCIAL_VALIDATION` 은
      재무 `SALES_VALIDATION` 으로 가는데, 매입 안에는 판매 사실이 없어 재무가
      `INPUT_INCOMPLETE` → `READY/skipped` 로 답했다. 그 skipped 가 허용목록 밖이라
      **매입 재검증은 늘 `FAILED`** 였고 이력 행은 `cycle=SALES` 로 남았다.

    ★ **묻는 모양은 매입 Flow 가 정한 그대로다** (`flow._validate`). 제안 최상위
      (응답 `judgment` · `meta.as_of` · `meta.item` 이 여기 있다)에 `scenarios` 를
      고른 안 하나로 얹는다. 🔴 **안을 골라 담지 않는다** — 매입이 칸을 늘린 날
      조용히 빠진다.

    ★ **부를 조언자의 주인은 `flow.ADVISORS` 다.** 여기서 이름을 다시 적지 않는다.

    ★ **skipped 는 통과가 아니다** (`_verdict`). 규칙 판정은 LLM 이 꺼져도 돈다 —
      원 실행이 `E1_APPROVED` 로 올라온 것 자체가 두 조언자가 LLM 없이 판정을 냈다는
      뜻이다. 그러니 재검증에서 skipped 가 오면 *"못 봤다"* 이지 *"원래 그렇다"* 가 아니다.

    :param proposal: 원 실행 응답의 `judgment` (매입 제안에서 `scenarios` 를 뺀 최상위).
    """
    request_id = make_revalidation_request_id(sim_run_id, as_of, decision_seq)
    context = ExecutionContext(
        request_id=request_id,
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version=policy_version,
        sim_run_id=sim_run_id,
    )

    day_gate = check_day_gate(as_of, sim_run_id=sim_run_id)
    if day_gate.gate == "BLOCKED":
        return Revalidation(
            outcome="ERROR",
            reason=f"재검증할 날({as_of.isoformat()})이 안 열려 재검증을 못 돌렸다: "
            f"{day_gate.reason or day_gate.result}",
        )

    missing = wiring.missing(ADVISORS)
    if missing:
        return Revalidation(
            outcome="ERROR",
            reason=f"매입 안을 검증할 조언자가 등록되지 않아 재검증을 못 돌렸다: "
            f"{', '.join(missing)}",
        )

    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=REVALIDATION_BUDGET))
    payload = procurement_validation_payload(proposal, scenario)
    validations: dict[str, Mapping[str, Any]] = {}
    adjustments: list[Mapping[str, Any]] = []

    try:
        for agent in ADVISORS:
            reply = runner.call(agent, PROCUREMENT_REVALIDATION_MODE, payload)
            validations[agent] = _verdict_of(reply)
            adjustments.extend(wire_adjustment(a) for a in reply.suggested_adjustments)
    except (BudgetExhausted, AgentNotRegistered) as exc:
        return _recorded(
            context,
            Revalidation(
                outcome="ERROR",
                request_id=request_id,
                reason=f"매입 안 재검증이 끝나지 않았다: {exc}",
                validations=validations,
            ),
            runner=runner,
            item=item,
            cycle=PROCUREMENT_CYCLE,
        )

    outcome, reason, conditions = _verdict(validations, (), adjustments, original_conditions)
    return _recorded(
        context,
        Revalidation(
            outcome=outcome,
            request_id=request_id,
            reason=reason,
            conditions=conditions,
            validations=validations,
        ),
        runner=runner,
        item=item,
        cycle=PROCUREMENT_CYCLE,
    )


def _recorded(
    context: ExecutionContext,
    result: Revalidation,
    *,
    runner: MasterRunner,
    item: str | None,
    cycle: str,
) -> Revalidation:
    """재검증 실행 1건을 이력에 남긴다 (설계 §4).

    ★ **적재 실패가 재검증을 죽이지 않는다** (`persistence.record` 와 같은 태도).
      그때 `revalidation_request_id` 는 `master_agent_runs` 에 없는 키를 가리키는데,
      그것이 곧 *"적재가 실패했다"* 이고 설계 §4 가 숨기지 말라고 적은 자리다.
    """
    record_revalidation(
        context,
        cycle=cycle,
        outcome=result.outcome,
        reason=result.reason,
        # 🔴 **사람 말(`reason`)과 표지 원문(`conditions`)을 같은 행에 나란히 남긴다.**
        #   `reason` 만 남기면 조정 표지가 이 표에서 사라진다 — 근거는
        #   `Revalidation.conditions` 에 적어 두었다.
        conditions=result.conditions,
        validations=result.validations,
        unroutable=result.unroutable,
        plan=runner.plan,
        item=item,
    )
    return result


# ---------------------------------------------------------------------------
# 무엇을 부를 것인가
# ---------------------------------------------------------------------------


def _capabilities_for(scenario: Mapping[str, Any]) -> tuple[str, ...]:
    """이번에 물어볼 검증. **필수 둘이 먼저, 그다음 후보가 요구했던 나머지.**

    ★ **중복을 지운다.** 후보가 `FINANCIAL_VALIDATION` 을 요구했어도 필수로 이미
      들어 있으므로 한 번만 부른다 — 두 번 부르면 예산만 태우고 답은 같다.

    ★ **순서를 지킨다.** 판매가 적은 차례를 뒤에 그대로 붙인다
      (`sales_flow._required_validations` 와 같은 규율).
    """
    out: list[str] = list(REQUIRED_CAPABILITIES)
    raw = scenario.get("required_validations")
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        return tuple(out)
    for item in raw:
        if isinstance(item, str) and item not in out:
            out.append(item)
    return tuple(out)


def _missing_for(routes: Mapping[str, tuple[AgentName, Mode] | None]) -> tuple[str, ...]:
    """필수 검증을 부를 수 없는 이유들. **필수만 본다.**

    ```text
    라우팅이 없다   capability → (agent, mode) 표에 값이 None 이다
    등록이 없다     그 에이전트 어댑터가 프로세스에 없다
    ```

    🔴 **조건부는 여기서 안 막는다.** `ADDITIONAL_SUPPLY_CONTEXT` 는 라우팅이 아직
      `None` 이라 필수로 세면 **모든 재검증이 `ERROR`** 가 된다 (설계 §5: 조건부는
      *"안 왔다"* 로 둔다).
    """
    out: list[str] = []
    for capability in REQUIRED_CAPABILITIES:
        route = routes.get(capability)
        if route is None:
            out.append(f"{capability}(부를 대상 없음)")
            continue
        agent, _mode = route
        if wiring.missing((agent,)):
            out.append(f"{capability}({agent} 미등록)")
    return tuple(out)


def _verdict_of(reply: AgentReply) -> dict[str, Any]:
    """회신 하나를 판정 칸에 담는 모양으로.

    ★ `sales_flow._verdict_of` 와 **무엇이 같고 무엇이 왜 다른지** (2026-09-11).

      ```text
      같다   agent · mode · business_status · runtime_status · payload
             · reasoning · missing_data
      다르다 run_id — sales_flow 에만 있다
      ```

      🔴 **`run_id` 를 따라 넣지 않는다.** 저쪽의 `run_id` 는 되먹임에 실을 때
        `SalesFlow.replies_by_ref` 에서 회신 원본을 찾는 **포인터**다. 재검증에는 그
        등록소가 없다 — 없는 것을 가리키는 포인터를 만들면 다음 사람이 그것을
        쓰려다 빈손이 된다.

    🔴 **`payload` 는 2026-09-11 에 열었다. 그전에는 버렸다.**

      ```text
      전  agent · mode · business_status · runtime_status · reasoning · missing_data
      후  + payload
      ```

      ★★ **두 파일이 서로를 가리키며 「같은 모양」이라고 적어 두었는데 두 칸이
        달랐다.** 이 문단의 옛 문장이 *"`sales_flow._verdict_of` 와 같은 모양이다"*
        였고, 저쪽은 *"`revalidation._verdict_of` 가 이미 목록으로 적고 있어 두
        경로가 같아진다"* 였다 — **둘 다 상대를 근거로 대며 같다고 주장했다.**
        매입이 `#588` 에서 고친 것과 같은 병이다: 우리가 소유하지 않은 파일의 사실을
        베껴 와 근거로 삼았다.

      🔴 **실측된 피해.** 재검증이 받은 재무 회신의 `financial_summary` 가 여기서
        사라져, 확정이 기여이익을 못 찾아 `sales` 가 0행이었다. 통과한 안은 되먹임을
        안 받으므로(계약 `C-1`) 후보에도 그 값이 없었고, **고리가 닫혀 있었다.**

      ⚠️ `ExecutionPlan.record` 도 `reply.payload` 를 안 담는다. 그래서 이 칸을 열기
        전에는 회신 내용이 **어디에도** 안 남았다.

    ★ **`wire_payload` 로 편다** (#175 · `sales_flow` 와 같은 규율). payload 는
      마스터가 모양을 모르는 중첩 dict 라, 튜플이 하나라도 있으면 JSON 왕복 전후로
      같은 칸이 두 모양이 된다.

    ★ **조건 비교는 안 바뀐다.** `conditions_of` 는 `business_status` 하나만 보고,
      그 문서화 문자열이 *"판정은 닫힌 어휘 하나만 쓴다 — `reasoning` 은 설명이지
      조건이 아니라 넣지 않는다"* 고 적어 두었다. `payload` 도 같은 쪽이다.
      `tests/master/test_finance_margin_carried.py` 가 그것을 잠근다.
    """
    return {
        "agent": reply.agent,
        "mode": reply.mode,
        "business_status": reply.business_status,
        "runtime_status": reply.runtime_status,
        "payload": wire_payload(dict(reply.payload)),
        "reasoning": reply.reasoning,
        "missing_data": list(reply.missing_data),
    }


# ---------------------------------------------------------------------------
# 조건 비교 — 🔴 M-4 에서 가장 틀리기 쉬운 자리
# ---------------------------------------------------------------------------


def conditions_of(
    validations: Mapping[str, Mapping[str, Any]],
    adjustments: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """그 안에 붙은 **조건 표지 집합.** 두 종류를 한 집합에 담는다.

    ```text
    verdict:{capability}={business_status}   그 검증이 `conditional` 로 답했다
    adjust:{표준형 JSON}                     부서가 낸 조정 제안 하나
    ```

    🔴 **`business_status == "conditional"` 만 보면 틀린다.** *"원래도 조건부였던 안이
      같은 조건으로 다시 통과한 것"* 과 *"새 조건이 생긴 것"* 은 다르다. 그래서 상태
      하나가 아니라 **집합**을 만들어 원 실행 것과 견준다 (설계 §3).

    ★ **비교 단위를 이렇게 고른 근거.**

      ```text
      판정(validations)   닫힌 어휘 하나만 쓴다 — (capability, business_status)
      조건(adjustments)   부서 표준형 **전부** 를 쓴다 — 마스터가 안에서 고르지 않는다
      ```

      판정은 `Verdict` 네 값으로 닫힌 어휘라 그 값 자체가 비교 단위가 된다. `reasoning`
      은 **설명**이지 조건이 아니라 넣지 않는다 — 넣으면 문장만 다듬어도 `CONDITIONAL`
      이 되어 사용자가 같은 안을 계속 다시 승인하게 된다.

      반대로 조정 제안은 `SuggestedAdjustment` **표준형이 곧 조건**이다. 그 안에서
      *"무엇이 중요한 칸인가"* 를 마스터가 고르면 그것이 판단이 된다 (§3.2.2) — 골라
      담지 않고 `wire_adjustment` 가 편 것을 통째로 표지로 쓴다. `reason` 문장까지
      들어가므로, 같은 숫자에 다른 문장이면 **조건이 바뀐 것으로 본다.**

    ⚠️ **판매가 조건을 어떤 모양으로 내는지 아직 확인 전이다** (설계 §6-①). 그래서
      **보수적으로 기울였다** — 비교 단위가 확실히 같지 않으면 `CONDITIONAL` 이다.
      통과 쪽으로 기울면 *사용자가 본 적 없는 조건이 사용자 승인으로 기록된다.*
    """
    out: set[str] = set()
    for capability, verdict in validations.items():
        business = str(verdict.get("business_status") or "")
        if business != "ok":
            # `ok` 만 "조건 없음" 이다. `conditional` 은 물론 `reject` · `skipped` 도
            # 표지로 남긴다 — 판정은 아래 `_verdict` 가 따로 하고, 여기는 **원 실행보다
            # 나빠졌는가**만 잰다.
            out.add(f"verdict:{capability}={business}")
    for adjustment in adjustments:
        out.add(f"adjust:{json.dumps(adjustment, sort_keys=True, ensure_ascii=False)}")
    return frozenset(out)


def conditions_of_original(
    response_payload: Mapping[str, Any], scenario_label: str
) -> frozenset[str]:
    """원 실행에서 **그 후보에 붙어 있던** 조건 표지 집합.

    ```text
    판정   candidates[].validations   판매 응답
           verdicts                   매입 응답 (후보가 없을 때만 본다)
    조건   adjustments[]              🔴 그 안의 라벨을 밝힌 것만 센다
    ```

    🔴 **라벨을 안 밝힌 조정은 그 안의 조건으로 세지 않는다.** `scenario_labels` 는
      *"비어 있어도 된다 — 안 채운 것과 해당 없는 것을 여기서 가르지 않는다"*
      (`SuggestedAdjustment`). 뜻이 둘인 값을 **있는 쪽으로 읽으면** 원 조건 집합이
      커지고, 커진 만큼 재검증 결과가 `PASSED` 로 접힌다.

      **그 방향으로는 기울이지 않는다.** 안 센다 → 원 조건 집합이 작다 →
      `CONDITIONAL` 쪽으로 기운다 → 사용자에게 되돌아간다. 되돌아가는 것은 되돌릴 수
      있지만, 없던 조건이 승인으로 기록되는 것은 되돌릴 수 없다.

    ⚠️ **재검증 쪽은 라벨로 거르지 않는다** (`revalidate_scenario`). 그쪽은 후보 하나만
      돌리므로 나온 조정이 전부 그 안의 것이다. 이 비대칭도 보수적인 방향이다 — 재검증
      집합이 더 크게 잡히므로 `PASSED` 로 접히기 어렵다.

    ★ **못 읽으면 빈 집합이다.** 판정을 실은 칸이 아예 없는 모양이면 원 조건을 0 으로
      두고, 그러면 재검증에 조건이 하나라도 있는 순간 `CONDITIONAL` 이다.
      *"모르면 통과"* 가 아니라 *"모르면 되돌린다"* 로 둔다.

    🔴 **매입 응답의 판정은 최상위 `verdicts` 에 있다** (2026-09-16). 전에는 이 함수가
      `candidates[].validations` **하나만** 읽어서, 매입에서는 원 조건 집합이 **항상 빈
      집합**이었다. *"모르면 되돌린다"* 가 매입에서는 *"매번 되돌린다"* 가 된 것이다.

      실측 (`dev@983c85b` · 실행 `SIM-CHECK-HOLIDAY-0916` · 2026-04-13 배추):

      ```text
      원 실행  verdicts.inventory.business_status = "conditional"   (ZONE_CAPACITY_UNRESOLVED)
      재검증   validations.inventory.business_status = "conditional"  ← 원 실행과 똑같다
      결과     added = {"verdict:inventory=conditional"} → CONDITIONAL
      ```

      **그 조건은 원 실행에도 똑같이 있었다.** 재고 판정은 구조적으로 매일
      `conditional` 이라, `POST /master/runs/{id}/purchase-record` 가 기록값이 선정안과
      하나라도 다르면 **항상 422** 로 막혔다 (`_revalidate_or_reject` 는 `PASSED` 만
      받는다).

    ⚠️ **매입의 `verdicts` 는 안 라벨로 거르지 않는다.** 그 칸은 안별로 갈라져 있지 않고
      **그 실행 전체의 판정**이다. 사람이 승인할 때 화면에서 본 것이 바로 그 판정이라,
      그대로 견주는 것이 맞다. 라벨로 거르면 셀 것이 하나도 안 남아 고치기 전과 같아진다.
    """
    candidates = [
        candidate
        for candidate in response_payload.get("candidates") or ()
        if isinstance(candidate, Mapping)
    ]
    validations: Mapping[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        scenario = candidate.get("scenario")
        if not isinstance(scenario, Mapping) or not _labels_match(scenario, scenario_label):
            continue
        raw = candidate.get("validations")
        if isinstance(raw, Mapping):
            validations = {k: v for k, v in raw.items() if isinstance(v, Mapping)}
        break

    if not candidates:
        # 매입 응답. 후보가 있는 응답(판매)에서는 이 칸을 보지 않는다 — 판매의 판정은
        # 후보마다 갈라져 있어, 최상위로 올라가면 다른 안의 조건까지 세게 된다.
        raw = response_payload.get("verdicts")
        if isinstance(raw, Mapping):
            validations = {k: v for k, v in raw.items() if isinstance(v, Mapping)}

    adjustments = [
        adjustment
        for adjustment in response_payload.get("adjustments") or ()
        if isinstance(adjustment, Mapping)
        and scenario_label in (adjustment.get("scenario_labels") or ())
    ]
    return conditions_of(validations, adjustments)


def find_scenario(
    response_payload: Mapping[str, Any], scenario_label: str
) -> Mapping[str, Any] | None:
    """사용자가 고른 **그 안 하나.** 두 응답 모양을 다 본다.

    ```text
    매입 응답   scenarios[]              label 로 찾는다
    판매 응답   candidates[].scenario    scenario_id 로 찾는다 (label 이 없다)
    ```

    ★ **`decision_service._scenarios_of` 와 다른 물음이다.** 저쪽은 *"약정을 만들
      매입 안"* 을 찾아 `build_commitment` 에 넘기므로 매입 응답 모양만 본다. 여기는
      *"오늘 다시 검증할 안"* 이라 판매 후보도 찾아야 한다 — 같은 함수로 묶으면 매입
      약정 조립이 판매 후보를 받는 날이 온다.

    🔴 **라벨이 겹치면 `None` 이다.** 첫 것을 조용히 고르면 **어느 안을 재검증했는지가
      운에 걸린다** (`_commitment_parts` 가 같은 이유로 그렇게 한다).

    ⚠️ **두 모양을 한 목록으로 합치지 않는다.** 합치면 같은 안이 두 칸에 다 실린 응답에서
      *"라벨이 둘"* 로 읽혀 재검증이 통째로 못 돈다. **`scenarios` 가 먼저다** —
      승인 검사(`check_scenario_exists`)가 보는 칸이 그쪽이라, 그 칸이 있으면 사용자가
      승인한 안은 정의상 거기 있는 것이다.
    """
    for scenarios in (_top_level(response_payload), _candidate_scenarios(response_payload)):
        matches = [s for s in scenarios if _labels_match(s, scenario_label)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None  # 그 칸 안에서 겹쳤다 — 뒤 칸으로 넘어가 다른 안을 고르지 않는다
    return None


def _top_level(response_payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """매입 응답의 안 목록 (`scenarios[]`)."""
    return [
        scenario
        for scenario in response_payload.get("scenarios") or ()
        if isinstance(scenario, Mapping)
    ]


def _candidate_scenarios(response_payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """판매 응답의 안 목록 (`candidates[].scenario`)."""
    out: list[Mapping[str, Any]] = []
    for candidate in response_payload.get("candidates") or ():
        if not isinstance(candidate, Mapping):
            continue
        scenario = candidate.get("scenario")
        if isinstance(scenario, Mapping):
            out.append(scenario)
    return out


def _labels_match(scenario: Mapping[str, Any], scenario_label: str) -> bool:
    """후보가 그 라벨의 것인가.

    ★ **두 칸을 다 본다.** 매입 응답의 안은 `label` 이고 판매 후보는 `scenario_id` 다
      (`app/sales/schemas.py` `SalesScenario`). 승인 요청이 실어 보내는 것은 한
      칸(`scenario_label`)뿐이라, 받는 쪽이 두 이름을 다 알아야 한다.
    """
    return scenario_label in {
        str(scenario.get("label") or ""),
        str(scenario.get("scenario_id") or ""),
    }


# ---------------------------------------------------------------------------
# 조건 표지를 사람 말로 — 🔴 **비교는 표지로, 문장은 사람 말로** (2026-09-16)
# ---------------------------------------------------------------------------
#
# 🔴 **실측된 피해** (2026-09-16 · 실 서버 · `POST /master/runs/{id}/purchase-record`
#   에 130kg → 50,000kg 를 넣음). 거부 사유가 이렇게 나갔다:
#
#   ```text
#   기록값으로 다시 검증했더니 통과하지 못해 기록하지 않았습니다 (CONDITIONAL):
#   통과했으나 원 실행에 없던 조건이 1건 붙었다:
#   adjust:{"axis": "quantity", "dept": "inventory", "reason": "수량을 7470kg 로 조정 제안",
#   "ref_ids": [...], "scenario_labels": ["기본"], "split_date": "2026-09-14",
#   "target_value": 7470.0, "unit": "kg"}
#   ```
#
#   **조정 제안 JSON 이 통째로 화면에 뜬다.** 그런데 그 JSON 안에 「수량을 7470kg 로
#   조정 제안」이라는 **부서가 쓴 좋은 한국어**가 이미 있다 — 꺼내 쓰면 된다.
#
# 🔴 **`conditions_of` 는 한 줄도 안 바꿨다.** 표지 집합이 비교의 정본이고, 그것을
#   건드리면 *"원래도 있던 조건"* 과 *"새로 붙은 조건"* 을 가르는 기준이 흔들린다.
#   여기서 바뀌는 것은 `_verdict` 가 만드는 **문장뿐**이고, 판정 값
#   (`PASSED`·`CONDITIONAL`·`FAILED`)과 `added` 집합은 그대로다.

_VERDICT_PREFIX = "verdict:"
_ADJUST_PREFIX = "adjust:"

#: 판정 어휘 한국어. **닫힌 어휘 넷**(`Verdict`)이고 `skipped` 는 표에 없다 —
#: `answer._VERDICT_LABEL` · `report._VERDICT_LABEL` 과 **같은 세 낱말**이다.
#:
#: ★ 두 곳 다 비공개라 임포트할 공개 주인이 없다. 어휘를 늘리지 않고 그 셋을 그대로
#:   쓰고, 표 밖은 `report._verdict_line` 과 같은 말(「판정 없음」)로 떨어뜨린다.
_VERDICT_LABEL: dict[str, str] = {"ok": "통과", "conditional": "조건부", "reject": "거절"}

#: 조정 축 한국어. 어휘의 주인은 `contracts.core.AdjustAxis` 이고, 한국어는 화면
#: `frontend/src/lib/vocab.ts` 의 `DEPT_AXIS_LABEL` 과 **같은 낱말**이다.
_AXIS_LABEL: dict[str, str] = {
    "quantity": "수량",
    "timing": "시점",
    "channel_mix": "채널 배분",
    "amount": "금액",
}


def _spoken(marker: str) -> str:
    """조건 표지 **하나**를 사람이 읽는 한 조각으로.

    ```text
    verdict:{capability}={business_status}   →  「재무 판정이 조건부」
    adjust:{부서 표준형 JSON}                 →  「물류: 수량을 7,470kg 로 조정 제안」
    ```

    🔴 **모르는 표지는 그대로 흘린다.** 여기서 지어내면 *"무엇이 조건이었나"* 가
      사라진다 — 지금 표지는 둘뿐이고, 셋째가 생기면 그 낱말이 그대로 화면에 뜬다.
    """
    if marker.startswith(_VERDICT_PREFIX):
        return _spoken_verdict(marker[len(_VERDICT_PREFIX) :])
    if marker.startswith(_ADJUST_PREFIX):
        return _spoken_adjust(marker[len(_ADJUST_PREFIX) :])
    return marker


def _spoken_verdict(body: str) -> str:
    """`{capability}={business_status}` 를 「{부서} 판정이 {상태}」로.

    ★ **capability 이름을 화면에 안 쓴다.** 재검증이 담는 키는 경로에 따라 두 어휘다 —
      판매 안은 capability(`FINANCIAL_VALIDATION`), 매입 안은 조언자 이름(`finance`).
      **둘 다 부서로 옮긴다**: capability 는 라우팅 표(`route_capability`)가 이미 부서를
      알고, 조언자 이름은 그 자체가 부서다. 여기서 표를 새로 적지 않는다.
    """
    name, _, status = body.partition("=")
    label = _VERDICT_LABEL.get(status)
    dept = _dept_label(name)
    # ⚠️ 여기 올 수 있는 상태는 사실상 `conditional` 하나다 — 그 밖은 위에서
    #   `FAILED` 로 떨어진다 (`PASSING_VERDICTS`). 그래도 표 밖을 말로 받는다.
    return f"{dept} 판정이 {label}" if label else f"{dept} 판정이 없다"


def _dept_label(name: str) -> str:
    """capability 든 조언자 이름이든 **부서 한국어**로. 모르면 받은 이름 그대로."""
    route = route_capability(name)
    return agent_label(route[0] if route is not None else name)


def _spoken_adjust(body: str) -> str:
    """부서 표준형 JSON 을 「{부서}: {무엇}」으로. **`dept` 와 `reason` 만 쓴다.**

    🔴 **`reason` 이 비면 지어내지 않는다.** `axis` · `target_value` · `unit` 로 짓고,
      셋 다 없으면 「부서가 대안을 냈다」까지만 말한다. 조정의 나머지 칸(`ref_ids` ·
      `split_date` · `scenario_labels`)은 **비교에는 쓰이지만 문장에는 안 나간다** —
      사람이 읽을 것이 아니다.

    ⚠️ **표지는 언제나 `json.dumps` 가 만든 것이다** (`conditions_of`). 그래도 못 읽는
      경우를 값으로 받는다 — 여기서 터지면 거부 사유 대신 스택이 화면에 뜬다.
    """
    try:
        raw = json.loads(body)
    except ValueError:
        raw = None
    adjustment: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

    dept = agent_label(str(adjustment.get("dept") or "")) or "부서"
    reason = str(adjustment.get("reason") or "").strip()
    무엇 = _grouped(reason, adjustment) if reason else _what_from_fields(adjustment)
    if not 무엇:
        return f"{dept}{_particle(dept, '이', '가')} 대안을 냈다"
    return f"{dept}: {무엇}"


def _what_from_fields(adjustment: Mapping[str, Any]) -> str:
    """`reason` 이 빈 조정을 **칸으로만** 편다. 🔴 없는 값을 지어내지 않는다."""
    axis = _AXIS_LABEL.get(str(adjustment.get("axis") or ""), "")
    값 = _grouped_amount(adjustment.get("target_value"), str(adjustment.get("unit") or ""))
    if axis and 값:
        return f"{axis}{_particle(axis, '을', '를')} {값} 로 조정 제안"
    if axis:
        return f"{axis} 조정 제안"
    if 값:
        return f"{값} 로 조정 제안"
    return ""


def _grouped_amount(value: Any, unit: str) -> str:
    """수량·금액을 **자리를 끊어** 찍는다 (`report._kg` · `report._won` 과 같은 방식)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    return f"{round(value):,}{unit}"


def _grouped(reason: str, adjustment: Mapping[str, Any]) -> str:
    """부서 문장 안 **그 숫자 하나만** 자리를 끊어 찍는다 (`7470kg` → `7,470kg`).

    🔴 **문장을 훑어 숫자를 고치지 않는다.** 훑으면 물류의 시점 조정 문장
      「도착일을 2026-09-14 로 조정 제안」의 연도가 `2,026` 이 된다. 그래서 **칸이
      말해 주는 값과 단위로 만든 토큰**(`{target_value:g}{unit}`)이 문장에 그대로
      있을 때만 바꾼다 — 시점 조정은 단위가 `d`(D+N)라 그 토큰이 문장에 없고,
      따라서 날짜는 손대지 않는다.

    ★ **못 찾으면 부서 문장 그대로다.** 부서가 표기를 바꾸는 날 이 자리는 조용히
      아무것도 안 한다 — 문장이 안 예뻐질 뿐 **틀린 숫자가 나가지 않는다.**
    """
    target = adjustment.get("target_value")
    unit = str(adjustment.get("unit") or "")
    if not isinstance(target, (int, float)) or isinstance(target, bool) or not unit:
        return reason
    토큰 = f"{float(target):g}{unit}"
    끊은_것 = f"{round(target):,}{unit}"
    if 토큰 == 끊은_것:
        return reason
    # 앞자리가 숫자면 다른 수의 꼬리를 자르는 것이라 안 바꾼다.
    return re.sub(rf"(?<!\d){re.escape(토큰)}", 끊은_것, reason)


def _particle(word: str, 받침: str, 모음: str) -> str:
    """받침 유무로 조사를 고른다 (`report._object_particle` 과 같은 규칙)."""
    if not word:
        return 받침
    code = ord(word[-1]) - 0xAC00
    if code < 0 or code > 11171:
        return 받침
    return 모음 if code % 28 == 0 else 받침


def _verdict(
    validations: Mapping[str, Mapping[str, Any]],
    unroutable: tuple[str, ...],
    adjustments: Sequence[Mapping[str, Any]],
    original_conditions: frozenset[str],
) -> tuple[RevalidationOutcome, str, tuple[str, ...]]:
    """네 값 중 무엇인가 (설계 §3).

    ```text
    FAILED       필수 중 하나라도 통과 어휘 밖이다
    CONDITIONAL  통과했으나 조건이 원 실행보다 늘었다
    PASSED       통과했고 조건이 같거나 줄었다
    ```

    🔴 **셋째 칸이 표지 원문이다** (2026-09-16 · `Revalidation.conditions`).
      **`reason` 문장이 접은 바로 그 표지**를 정렬 그대로 돌려준다 — 문장과 원문이
      같은 것을 가리켜야 한 행 안에서 서로를 풀 수 있다. 여기서 한 번 만든 것을
      두 곳이 나눠 쓰는 것이라, 부르는 쪽이 `conditions_of` 를 다시 돌리지 않는다.

    ★ **통과 판정은 허용목록으로 한다** (`PASSING_VERDICTS`). *"reject 가 아니면
      통과"* 로 정하면 봉투 어휘가 늘 때마다 새 값이 통과 쪽으로 샌다 (#173).
      그래서 `skipped`(판정을 안 낸 것)도 여기서는 못 통과한 것이다 — 필수 검증에서
      *"판정을 안 냈다"* 는 통과가 아니다.

    🔴 **`unroutable` 로는 `FAILED` 를 내지 않는다.** 지금 그 자리는
      `ADDITIONAL_SUPPLY_CONTEXT` 하나이고 **원 실행에서도 똑같이 못 불렀다** — 그
      사이 나빠진 것이 아니다 (설계 §5). 못 물어봤다는 사실은 결과에 그대로 실린다.
    """
    blocked = [
        f"{capability}({verdict.get('runtime_status')}/{verdict.get('business_status')})"
        for capability, verdict in validations.items()
        if str(verdict.get("business_status") or "") not in PASSING_VERDICTS
    ]
    if blocked:
        # ★ **여기는 조건을 비교하지도 않았다** — 표지가 없는 것이 사실 그대로다.
        return "FAILED", f"재검증에서 막혔다: {', '.join(blocked)}", ()

    now = conditions_of(validations, adjustments)
    added = sorted(now - original_conditions)
    if added:
        # 🔴 **비교는 표지로, 문장은 사람 말로** (2026-09-16). `added` 는 그대로 표지
        #   집합이고 세는 방식도 그대로다 — 펴는 것은 이 한 줄뿐이고, **편 것과 원문을
        #   나란히 돌려준다** (`Revalidation.conditions`).
        return (
            "CONDITIONAL",
            (
                f"통과했으나 원 실행에 없던 조건이 {len(added)}건 붙었다: "
                f"{'; '.join(_spoken(표지) for 표지 in added)}"
            ),
            tuple(added),
        )

    꼬리 = f" (못 물어본 요구: {', '.join(unroutable)})" if unroutable else ""
    return "PASSED", f"재검증 통과 — 조건이 원 실행보다 나빠지지 않았다{꼬리}", ()
