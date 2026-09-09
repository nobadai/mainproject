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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.master import wiring
from app.master.budget import BudgetExhausted, CallBudget
from app.master.day_gate import check_day_gate
from app.master.decision import RevalidationOutcome
from app.master.envelope import (
    PASSING_VERDICTS,
    AgentName,
    AgentReply,
    Capability,
    ExecutionContext,
    Mode,
    route_capability,
    wire_adjustment,
)
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
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
    "revalidate_scenario",
]


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

🔴 **`SALES_BUDGET`(16) 을 그대로 쓰지 않는다.** 저쪽은 *"후보 3 · 되먹임 2회"* 를
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


def make_revalidation_request_id(as_of: date, decision_seq: int) -> str:
    """`REV-20260907-0001`. **시각이 아니라 날짜 + 결정 회차다.**

    ★ `make_request_id` 와 같은 규율이다 (§1.2-11) — 같은 날 재검증을 구분하되
      **재현 가능해야 한다.** 순번을 결정 회차로 두면 번복(`decision_seq` 2, 3 …)이
      각자 다른 키를 받고, 같은 승인을 두 번 처리해도 같은 키가 나온다.
    """
    return f"{_REVALIDATION_KEY_PREFIX}-{as_of.strftime('%Y%m%d')}-{decision_seq:04d}"


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

    :param as_of: 이 재검증이 서는 날. 🔴 **원 실행의 날이 아니라 지금 고르는 날이다.**
    :param original_conditions: 원 실행에서 그 후보에 붙어 있던 **조건 표지 집합**
        (`conditions_of` 가 만든다). 이번 결과가 이보다 늘면 `CONDITIONAL` 이다.
    """
    request_id = make_revalidation_request_id(as_of, decision_seq)
    context = ExecutionContext(
        request_id=request_id,
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version=policy_version,
        # ★ 어느 실행의 장부인가는 마스터가 정한다 (물류 `#325`) — 두 진입점과 같은 값.
        sim_run_id=BURN_IN_SIM_RUN_ID,
    )

    # ① 🔴 **첫 관문은 개장이다.** 막히면 재검증이 **실패한** 것이 아니라 **돌리지 못한**
    #    것이라 `FAILED` 와 갈라 `ERROR` 로 적는다.
    day_gate = check_day_gate(as_of)
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
            if route is None:
                # 🔴 **조용히 건너뛰지 않는다.** 건너뛰면 *"검증됐다"* 로 읽힌다.
                #    `ADDITIONAL_SUPPLY_CONTEXT` 가 지금 그 자리다 (설계 §5).
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
        )

    outcome, reason = _verdict(validations, tuple(unroutable), adjustments, original_conditions)
    return _recorded(
        context,
        Revalidation(
            outcome=outcome,
            request_id=request_id,
            reason=reason,
            validations=validations,
            unroutable=tuple(unroutable),
        ),
        runner=runner,
        item=item,
    )


def _recorded(
    context: ExecutionContext,
    result: Revalidation,
    *,
    runner: MasterRunner,
    item: str | None,
) -> Revalidation:
    """재검증 실행 1건을 이력에 남긴다 (설계 §4).

    ★ **적재 실패가 재검증을 죽이지 않는다** (`persistence.record` 와 같은 태도).
      그때 `revalidation_request_id` 는 `master_agent_runs` 에 없는 키를 가리키는데,
      그것이 곧 *"적재가 실패했다"* 이고 설계 §4 가 숨기지 말라고 적은 자리다.
    """
    record_revalidation(
        context,
        outcome=result.outcome,
        reason=result.reason,
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

    ★ `sales_flow._verdict_of` 와 **같은 모양이다** — 화면과 이력이 두 경로에서 다른
      모양을 받으면 읽는 쪽이 어느 경로에서 왔는지를 먼저 알아야 한다.
    """
    return {
        "agent": reply.agent,
        "mode": reply.mode,
        "business_status": reply.business_status,
        "runtime_status": reply.runtime_status,
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
    판정   candidates[].validations   판매 응답에만 있다 (매입 응답에는 없다)
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

    ★ **못 읽으면 빈 집합이다.** 매입 응답처럼 후보 판정이 아예 없는 모양이면 원
      조건을 0 으로 두고, 그러면 재검증에 조건이 하나라도 있는 순간 `CONDITIONAL` 이다.
      *"모르면 통과"* 가 아니라 *"모르면 되돌린다"* 로 둔다.
    """
    validations: Mapping[str, Mapping[str, Any]] = {}
    for candidate in response_payload.get("candidates") or ():
        if not isinstance(candidate, Mapping):
            continue
        scenario = candidate.get("scenario")
        if not isinstance(scenario, Mapping) or not _labels_match(scenario, scenario_label):
            continue
        raw = candidate.get("validations")
        if isinstance(raw, Mapping):
            validations = {k: v for k, v in raw.items() if isinstance(v, Mapping)}
        break

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


def _verdict(
    validations: Mapping[str, Mapping[str, Any]],
    unroutable: tuple[str, ...],
    adjustments: Sequence[Mapping[str, Any]],
    original_conditions: frozenset[str],
) -> tuple[RevalidationOutcome, str]:
    """네 값 중 무엇인가 (설계 §3).

    ```text
    FAILED       필수 중 하나라도 통과 어휘 밖이다
    CONDITIONAL  통과했으나 조건이 원 실행보다 늘었다
    PASSED       통과했고 조건이 같거나 줄었다
    ```

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
        return "FAILED", f"재검증에서 막혔다: {', '.join(blocked)}"

    now = conditions_of(validations, adjustments)
    added = sorted(now - original_conditions)
    if added:
        return "CONDITIONAL", (
            f"통과했으나 원 실행에 없던 조건이 {len(added)}건 붙었다: {'; '.join(added)}"
        )

    꼬리 = f" (못 물어본 요구: {', '.join(unroutable)})" if unroutable else ""
    return "PASSED", f"재검증 통과 — 조건이 원 실행보다 나빠지지 않았다{꼬리}"
