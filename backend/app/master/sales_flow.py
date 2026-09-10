"""
sales_flow.py — 판매 의사결정 Flow **골격** (판매 v1.7 · 마스터 설계 2026-09-06)

    ① mock 입력 검사                        섞였으면 아무것도 시작하지 않는다
    ② inventory / PRE_SALES                 판매가능·납기 초기 컨텍스트
    ③ sales / GENERATE_SALES_PROPOSAL       후보 여럿
    ④ 후보마다 required_validations 라우팅   CAPABILITY_ROUTING 으로 (agent, mode)
    ⑤ 통과 후보 ≥ 1 → 제시 · == 0 → 되먹임(최대 2회) → 그래도 0 이면 탈락

★ **매입 `flow.py` 를 고치지 않는다.** 뼈대는 닮았지만 층이 다르다 (D-3 합의).
  종료 코드도 예산도 되먹임 규칙도 따로 둔다 — 합치면 한쪽 사이클을 고칠 때마다
  다른 쪽이 흔들린다.

★ **판매와 매입이 갈리는 세 자리** (설계 §1).

  ```text
  누가 검증 대상을 정하나   매입: 마스터가 조언자를 정한다
                            판매: 후보가 capability 로 요구하고 마스터가 라우팅한다
  밴드                      매입: 조언자가 하나라도 빠지면 시작하지 않는다
                            판매: 밴드가 없다 — 컨텍스트가 없어도 시작은 한다
  부분 통과                 매입: 안이 전부 잘리면 끝
                            판매: 후보가 여럿이고 **부분 통과가 정상이다** (C-1)
  ```

🔴 **이번 조각에 없는 것.** 어댑터 배선·승인 후 Write. 랭킹/추천 재계산은 **판매
  소유라 영원히 여기 없다** — 마스터는 순위를 다시 매기지 않는다 (판매 v1.7 §18).

  ★ **최종 재검증은 `revalidation.py` 로 나갔다** (M-4 · 2026-09-07). 여기 두지
    않는 이유가 있다 — 그쪽은 **후보를 만들지 않고 하나만 다시 본다.** 이 Flow 를
    재사용하면 제안자를 다시 불러 **사용자가 고른 안이 아닌 다른 안이 나온다.**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.contracts.core import SuggestedAdjustment
from app.master.budget import BudgetExhausted, CallBudget

# 🔴 **넷은 봉투에 있다 — 매입 Flow 에서 가져오지 않는다.**
#
#   `PASSING_VERDICTS` · `SourcedEvidence` · `AgentFailure` · `wire_adjustment` 는
#   **사이클에 매인 것이 아니라 봉투 수준의 것**이다. 부서 회신 하나에서 무엇을 뽑아
#   나르고 무엇을 전선에 싣는가는 매입이든 판매든 같은 질문이라 주인이 하나여야 한다.
#
#   `forecast_is_clean` 도 같은 자리다 — 매입 Flow 의 private 메서드였던 것을 M-1 에서
#   봉투로 올렸다. *"오늘 이후에 생성된 예측을 싣지 않는다"* 는 사이클이 아니라 **예측을
#   나르는 모든 경로**의 규칙이라, 베끼면 한쪽만 고쳐지는 날 판매가 미래를 본다.
#
#   베끼면 `#173` 이 고친 허용목록과 `#175` 가 고친 ISO 변환이 두 벌이 된다 —
#   어휘가 늘어난 날 한쪽만 늘고, JSON 왕복 성질
#   (`test_전선에_실은_것은_왕복해도_같다`)이 판매 경로에서만 조용히 깨진다.
#
# ★ 처음에는 `flow.py` 에서 private 로 읽어 왔다. **판매가 매입 모듈에 매인 것**이라
#   매입을 손대면 판매가 깨졌고, 그래서 넷을 봉투로 올렸다 (순수 이동).
from app.master.envelope import (
    PASSING_VERDICTS,
    AgentFailure,
    AgentName,
    AgentReply,
    Mode,
    SourcedEvidence,
    forecast_is_clean,
    route_capability,
    wire_adjustment,
    wire_payload,
)
from app.master.plan import ExecutionPlan
from app.master.ports import AgentNotRegistered
from app.master.procurement_boundary import ProcurementBoundary
from app.master.runner import MasterRunner
from app.master.sales_approval import missing_term_origins, missing_terms_reason

SalesEndCode = Literal[
    "SL1_PRESENTED",
    "SL2_NO_CANDIDATE",
    "SL3_ALL_REJECTED",
    "SL4_NOT_STARTED",
    "SL5_BUDGET_EXHAUSTED",
    "SL6_VALIDATION_UNRESOLVED",
]
"""판매 사이클 종료 코드.

```text
SL1_PRESENTED             통과 후보 ≥ 1 — 사용자에게 제시한다 (탈락안 사유 동봉)
SL2_NO_CANDIDATE          판매가 안을 만들지 못했다 (missing_data / missing_capability)
SL3_ALL_REJECTED          안은 있었으나 전부 탈락 — 되먹임까지 끝났다
SL4_NOT_STARTED           시작하지 못했다 (어댑터 미등록 · mock 입력)
SL5_BUDGET_EXHAUSTED      예산 소진 — 판단이 끝나지 않았다
SL6_VALIDATION_UNRESOLVED 안은 있는데 **판정이 끝나지 않았다** — 탈락이 아니다
```

🔴 **`SL6` 은 `SL3` 의 거짓말을 걷어내려고 생겼다.** 부서가 자료·정책이 없어 판정을
  못 낸 날(`RUNTIME_NOT_READY` · `INPUT_INCOMPLETE` → `skipped`)에도 종료 코드는
  *"전부 탈락"* 이었다. 아무도 탈락시키지 않았는데 탈락이라고 적은 것이라, 읽는
  사람은 **후보를 다시 만들어야 한다**고 읽는다 — 실제로 할 일은 재무 자료를 채우는
  것이고, 후보를 다시 만들어 봐야 같은 자리에서 또 막힌다.

  ```text
  SL3   판정이 났고 그 판정이 "안 된다" 다      → 조건을 바꿔야 한다
  SL6   판정 자체가 안 났다                     → 없는 자료를 채워야 한다
  ```

★ **`SL6` 도 승인 코드가 아니다.** `decision.approve_end_codes` 는 `SL1` 하나만
  승인으로 받는다 — 후보가 살아 있는 것과 승인 가능한 것은 다른 문제다.

★ **`SL5` 와 다르다.** 저쪽은 *"예산이 다해서 더 못 물었다"* 이고 여기는 **물어봤고
  답도 받았는데 그 답이 판정이 아니었다** 이다. 다음에 할 일이 다르다.

🔴 **매입 `EndCode`(E1~E5) 에 값을 더하지 않는다** (D-3 합의). 층이 다르다. 한 어휘에
  두 사이클을 담으면 `E2_HELD` 가 *"매입 보류"* 와 *"판매 보류"* 를 동시에 뜻하게 되고,
  화면과 이력이 어느 사이클의 종료인지를 payload 로 되짚어야 한다.

🔴 **예산 소진을 `SL3` 으로 접지 않는다 — 매입과 일부러 다르다.**

  매입은 `BudgetExhausted` 를 `E3_REJECTED` 로 바꾼다 (`flow.py:378-380`). 그러면
  **"다 봤는데 안 된다"** 와 **"다 못 봤다"** 가 **같은 코드**가 된다.

  매입 쪽을 지금 고칠 일은 아니지만, **새로 만드는 어휘를 같은 모양으로 만들 이유는
  없다.** 판매는 사용자에게 후보를 직접 보여주는 경로라, 못 본 것을 거절로 적으면
  화면이 거짓말을 한다 — 사용자는 *"이 조건으로는 안 된다"* 로 읽고 조건을 바꾸는데,
  실제로는 **판단이 끝나지 않은 것**이라 같은 조건으로 다시 돌리는 것이 맞다.
"""

#: 부서가 **실제로 판정을 낸** 업무 상태. 통과 여부는 묻지 않는다.
#:
#: 🔴 **`PASSING_VERDICTS` 의 반대가 아니다.** *"통과가 아니다"* 안에는 두 가지가
#:   섞여 있다 — **판정이 났는데 안 된다**(`reject`)와 **판정 자체가 안 났다**
#:   (`skipped`). 둘을 한 덩어리로 다루면 자료가 없어 못 본 안이 거절당한 안과
#:   같은 자리에 놓이고, 사용자는 고칠 수 없는 것을 고치러 간다.
#:
#: ★ 매입 `flow._JUDGED_VERDICTS` 와 같은 뜻이다. 두 Flow 가 각자 들고 있는 이유는
#:   `PASSING_VERDICTS` 를 봉투로 올릴 때와 같다 — 판매가 매입 모듈에 매이지 않는다.
_CONCLUDED_VERDICTS: frozenset[str] = PASSING_VERDICTS | {"reject"}


def _validation_concluded(verdict: Mapping[str, Any]) -> bool:
    """이 검증이 **판정까지 갔는가.**

    두 축을 같이 본다. `runtime_status` 가 `READY` 가 아니면 부서가 실행을 끝내지
    못한 것이고(`RUNTIME_NOT_READY` · `ERROR`), `READY` 라도 업무 상태가 판정 어휘
    밖이면(`skipped`) 부서가 **판정을 안 낸 것**이다 — 재무 `INPUT_INCOMPLETE` 가
    그 자리다.
    """
    return (
        str(verdict.get("runtime_status") or "") == "READY"
        and str(verdict.get("business_status") or "") in _CONCLUDED_VERDICTS
    )


MAX_FEEDBACK_ATTEMPTS = 2
"""되먹임 상한. **이 값의 소유자는 마스터다** (매입 `MAX_PURCHASE_ATTEMPTS` 와 같은 이유).

되먹임은 조정 행위이므로 조정자가 소유한다. 제안자는 자기 안을 몇 번 다시 만들지
정하지 않는다 — 그건 예산과 종료 코드를 쥔 쪽의 판단이다 (§1.2-12).

★ **되먹임 회차(`feedback_attempt`)는 호출 순번(`call_seq`)과 다르다** (C-5).
  `S-2`(ERROR 1회 재시도)가 끼면 `call_seq` 만 오르고 회차는 그대로다. 두 수를 한
  칸에 담으면 *"두 번 고쳐 보라고 시켰다"* 와 *"한 번 시켰는데 어댑터가 한 번
  터졌다"* 가 이력에서 같아 보인다.

★ 2 인 근거는 매입과 같다 — 상한이 아니라 **호출 예산이 실제 제동**이고, 회차는
  *"고칠 기회를 몇 번 주나"* 다. 더 나은 안은 사용자가 `RERUN_WITH_CONDITION` 으로
  요청한다 (C-2).
"""

SALES_BUDGET = 25
"""판매 사이클 기본 호출 예산.

```text
후보 3 · 되먹임 2회 최악 경우
  inventory PRE_SALES              1
  sales GENERATE_SALES_PROPOSAL    3   (최초 1 + 되먹임 2)
  finance SALES_VALIDATION         9   (후보 3 × 회차 3)
  purchase SUPPLY_CAPACITY_QUERY   9   (품목 3 × 회차 3)
  ────────────────────────────────────
                                  22
+2  후보 범위·날짜가 바뀌어 물류를 다시 부르는 경우 (판매 v1.7 §5)
+1  S-2 (ERROR 1회 재시도) 여유
────
                                  25
```

🔴 **매입 줄이 「후보 3」이 아니라 「품목 3」인 이유** (2026-09-10 · 라우팅 개방).

  매입 호출은 **품목으로 묶는다** (`_additional_supply_requests`). 후보가 셋이어도
  품목이 하나면 호출은 1이다. 그래서 **회차당 상한은 「서로 다른 품목 수」**이고,
  후보가 셋이면 품목도 최대 셋이라 회차당 3이 상한이다.

  ```text
  후보 셋이 다 배추            회차당 1   →  3회차 3
  후보 셋이 배추·무·양파       회차당 3   →  3회차 9   ← 이 값을 쓴다
  ```

  ⚠️ **후보 수를 늘리면 이 줄이 두 번 늘어난다** — 재무 줄과 매입 줄이 같이 는다.

🔴 **매입 기본값 12 (`schemas.py` `ProcurementRunRequest.budget`) 를 건드리지
  않는다.** 사이클이 다르면 예산도 다르다. 매입 값을 16 으로 올리면 매입이 안 쓰는
  4 회분 상한이 매입 쪽에서 풀린다.

🔴 **재무 `SALES_VALIDATION` 이 한 번에 한 후보만 받는다** (`app/finance/capabilities/
  sales.py` `parse_sales_validation_input` — `scenario_id`·`quantity_kg` 가 전부 단수).
  배열을 안 받으므로 **후보 3개면 재무를 3번 부른다.** 매입에 물어 둔 batch /
  ONE_BY_ONE 질문이 판매 쪽에서는 이미 ONE_BY_ONE 으로 정해진 셈이고, 재무가 그렇게
  구현한 것을 마스터가 바꿀 수 없다.

⚠️ **예산이 후보 수에 끌려간다.** 판매가 후보를 하나 더 내면 회차당 1 씩 더 붙는다.
  매입 라우팅(`ADDITIONAL_SUPPLY_CONTEXT`)이 열리면 후보당 1 이 또 붙으므로 **그때
  다시 센다.**

★ **`+2` 와 `+1` 은 아직 쓰이지 않는다.** 물류 재조회(S-1 범위 이탈)도 ERROR 재시도
  (S-2)도 이 골격에는 배선이 없다 — 예산에 자리를 잡아 둔 것과 배선한 것은 다르다.
"""


def sales_call_budget(limit: int = SALES_BUDGET) -> CallBudget:
    """판매 사이클 `CallBudget`. **진입점이 여기를 거친다.**

    ★ 상수를 각자 읽어 `CallBudget(limit=16)` 을 만들면 그 순간 주인이 여럿이 된다.
      아직 진입점(`run_sales()`)이 없어 호출자는 테스트뿐이지만, **자리를 먼저 하나로
      둔다** — 나중에 만드는 쪽이 상수를 다시 옮겨 적지 않게.
    """
    return CallBudget(limit=limit)


INITIAL_CONTEXT_ROUTE: tuple[AgentName, Mode] = ("inventory", "PRE_SALES")
"""②에서 부르는 초기 컨텍스트. **라우팅표와 같은 값을 손으로 적지 않는다.**

S-1(기여 호출 재사용)은 *"판매가 요구한 capability 의 라우팅이 ②와 같은 곳을 가리키면
그 회신을 다시 쓴다"* 로 판정한다. capability 이름 목록을 따로 두면
`CAPABILITY_ROUTING` 이 바뀐 날 한쪽만 바뀐다 — 여기는 **경로 하나**만 안다.
"""

ADDITIONAL_SUPPLY_CAPABILITY = "ADDITIONAL_SUPPLY_CONTEXT"
"""🔴 **후보를 그대로 보내지 않는 유일한 capability** (2026-09-10 · 라우팅 개방).

나머지 요구는 후보 전체를 payload 로 싣는다 — 재무 `parse_sales_validation_input` 이
`scenario_id`·`quantity_kg`·`supply` 를 후보 최상위에서 읽으므로 그 모양이 맞다.
매입이 읽는 것은 **다른 모양**이다.

```text
매입이 payload 에서 읽는 것 (`purchase_agent/adapter.py` `_supply_capacity_query`)
    item                             어느 품목을 묻나
    required_additional_quantity_kg  얼마가 모자라나
    warehouse_free_kg                창고 여유    ← 마스터가 실어 주는 물류 값
    finance_cap_amount_krw           매입 가능액  ← 마스터가 실어 주는 재무 값
```

⚠️ **후보를 통째로 보내면 조용히 반쪽이 된다.** 후보에 `item` 은 최상위에 있지만
  부족량은 `supply.required_additional_quantity_kg` 라 한 겹 안이고, 경계 재료는
  아예 없다. 매입은 오류를 내지 않고 `procurable_quantity_kg=null` ·
  `basis=unknown` 으로 답한다 — **물어본 값이 안 실렸다는 사실이 안 보인다.**
"""

#: 매입에 실어 보내는 경계 재료 칸. **`ProcurementBoundary` 의 필드 이름 그대로다** —
#: 옮겨 적으면 그쪽이 칸을 바꾸는 날 한쪽만 바뀐다.
#:
#: 🔴 **넷을 늘 싣는다. 못 읽었으면 `None` 으로 싣는다** (매입 계약 · 규칙 3).
#:   칸을 빼면 *"안 물어봤다"* 와 *"물어봤는데 못 읽었다"* 가 같아진다.
BOUNDARY_FIELDS: tuple[str, ...] = (
    "warehouse_free_kg",
    "rental_cap_kg",
    "finance_cap_amount_krw",
    "inbound_lead_days",
)

FEEDBACK_SOURCE_AGENT: dict[AgentName, str] = {"inventory": "logistics"}
"""마스터 `AgentName` → 판매 `SalesDomainReply.source_agent`.

🔴 **같은 부서를 두 어휘가 다르게 부른다.** 마스터는 물류 어댑터를 `"inventory"` 로
  등록해 두었고 (`AgentName` · `CAPABILITY_ROUTING`), 판매 계약은 같은 자리를
  `"logistics"` 로 적었다 (`SalesDomainReply.source_agent` Literal).

  ```text
  마스터   inventory   호출 대상의 등록 이름
  판매     logistics   회신을 보낸 부서의 이름
  ```

★ **이름 매핑이지 의미 이동이 아니다.** 바꾸는 것은 부서를 부르는 낱말 하나뿐이고,
  `runtime_status`·`business_status`·`payload` 는 부서가 보낸 그대로 나간다. 값을
  다시 쓰기 시작하면 그 순간 마스터가 부서 회신의 주인이 된다 (§3.2.2).

★ **여기서만 바꾼다 — 나가는 자리다.** 마스터 안에서 `"logistics"` 로 부르기
  시작하면 `CAPABILITY_ROUTING`·`wiring`·계획 이력까지 전부 두 이름을 갖게 된다.

🔴 **표에 없는 이름은 그대로 내보낸다.** 판매 Literal 에 없으면 문 앞에서 거부되는
  것이 맞다 — 마스터가 아는 이름으로 몰래 갈아 끼우면 **어휘가 갈린 사실 자체가
  사라진다.** 그 사실은 `test_sales_vocabulary.py` 가 잰다.
"""


@dataclass(frozen=True)
class CandidateVerdict:
    """후보 하나에 대한 판정 — **무엇을 물었고 무엇이 왔는가.**

    ★ **후보 단위다** (설계 정정 ① · 2026-09-06). 판매 v1.7 §4 는
      `required_validations` 를 최상위 배열로 적었지만 구현은 시나리오별이다
      (`app/sales/schemas.py` `SalesScenario.required_validations`). 1안은 재무만,
      2안은 재무+매입 식으로 **후보마다 요구가 다를 수 있다.**

    ★ **통과/탈락을 필드로 들지 않는다.** `passed` 는 `validations` 와 `unroutable`
      에서 나오는 값이라 필드로 두면 같은 사실의 주인이 둘이 된다.
    """

    #: 판매가 낸 후보 그대로. **마스터는 고르지도 재계산하지도 않는다** (§3.2.2).
    scenario: Mapping[str, Any]

    #: capability → 그 검증의 회신. 키는 판매가 요구한 이름 그대로다.
    validations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    #: 🔴 **부를 대상이 없어 못 물어본 요구.** 조용히 버리지 않는다 (설계 §3).
    #: 이 칸이 비어 있지 않으면 그 후보는 **통과로 치지 않는다** — 미해결로 둔다.
    unroutable: tuple[str, ...] = ()

    #: 사용자가 말한 조건 그대로 (`SalesFlow.user_request`). **누락의 위치를 가르는 데만
    #: 쓴다** — 후보를 고르거나 값을 채우는 데 쓰지 않는다 (§3.2.2).
    #:
    #: 🔴 **`SalesFlow` 를 통째로 들지 않는다.** 판정 하나가 실행기를 참조하면 이
    #:   판정을 재는 검사가 실행기를 세워야 한다 — 그래서 필요한 두 값만 들고 온다.
    user_request: Mapping[str, Any] | None = None

    #: 무슨 판매인가 (`SalesProposalInput.business_mode`). **`CONTRACT_FULFILLMENT` 을
    #: 가르는 데 쓴다** — 그 경로는 계약값을 쓰므로 `preferred_*` 가 안 실리는 것이
    #: 정상이고, 거기서 `REQUEST_MISSING` 을 내면 거짓말이 된다.
    business_mode: str | None = None

    @property
    def scenario_id(self) -> str:
        return str(self.scenario.get("scenario_id") or "")

    @property
    def unvalidated(self) -> bool:
        """요구한 검증이 **하나도 없었다.**

        🔴 이 후보는 통과로 나가지만 **아무도 안 본 안이다.** 마스터가 요구를 지어내지
          않기 때문이다 — 무엇이 필요한지는 제안자가 정한다 (§3.2.2). 대신 그 사실이
          여기 남아 화면이 *"검증 0건"* 을 말할 수 있다.
          *"검사하지 못한 것을 검사했다고 말하지 않는다"* (설계서 §8).
        """
        return not self.validations and not self.unroutable

    @property
    def missing_terms(self) -> tuple[str, ...]:
        """🔴 **확정에 필요한데 비어 있는 상업조건 — 어디서 끊겼는지까지.** 없으면 빈 튜플이다.

        ```text
        REQUEST_MISSING_<FIELD>     부르는 쪽이 안 보냈다   → 고칠 사람: 화면 · 걷기 · API 호출자
        TERMS_UNRESOLVED_<FIELD>    정말 조건이 없다        → 고칠 사람: 판매 · 계약
        ```

        ★ **이 구분의 값은 "누가 고쳐야 하나" 가 이름에서 보이는 것이다.** 전에는
          `delivery_date` 하나만 말해서, 화면에 채울 칸이 있는데 안 채운 것인지 판매가
          못 만든 것인지 사람이 코드를 읽어야 알았다.

        ★ **부서 판정과 다른 칸이다.** `validations` 에 가짜 항목을 밀어 넣지 않는다 —
          탈락 사유가 *"재무가 반려"* 처럼 보이면 사람이 재무를 본다. *"납품일이
          없다"* 로 보여야 판매를 본다.

        ★ **목록의 주인은 `sales_approval.REQUIRED_COMMERCIAL_TERMS` 다.** 승인 뒤
          `confirm_sale` 이 막는 것과 **같은 목록**을 여기서 미리 읽는다 — 베껴 두면
          *"올려도 되는 안"* 과 *"확정할 수 있는 안"* 이 갈린다.
        """
        return missing_term_origins(
            self.scenario,
            user_request=self.user_request,
            business_mode=self.business_mode,
        )

    @property
    def passed(self) -> bool:
        """사용자에게 올려도 되는가.

        ★ **허용목록으로 정한다** (`PASSING_VERDICTS`). *"reject 가 아니면 통과"* 로
          정하면 봉투 어휘가 늘 때마다 새 값이 통과 쪽으로 샌다 (#173).

        🔴 **상업조건 필수값도 여기서 본다** (2026-09-08 계약). `delivery_date` 나
          `payment_days` 가 없는 안은 사용자가 골라도 확정할 수 없다 — 값이 없는
          안을 고른 **뒤에야** 막는 것보다 **승인 가능한 후보의 필수조건**으로 두는
          편이 계약상 명확하다.

          ⚠️ 이 줄을 지우면 값 없는 안이 화면에 오르고, 사용자가 그것을 고른 뒤에야
            `sales_approval` 이 `BLOCKED` 를 낸다.
        """
        if self.unroutable:
            return False
        if self.missing_terms:
            return False
        return all(
            str(v.get("business_status") or "") in PASSING_VERDICTS
            for v in self.validations.values()
        )

    @property
    def unresolved_validations(self) -> tuple[str, ...]:
        """판정이 **나지 않은** 검증의 이름. 탈락과 다른 칸이다.

        🔴 **후보를 살려 두는 근거가 이 칸이다.** `passed` 는 여전히 거짓이고
          그래야 한다 — 판정 못 낸 안을 통과시키면 보지 않은 것을 통과시킨 것이다.
          하지만 *"통과가 아니다"* 와 *"거절당했다"* 는 다르고, 그 차이를 담을 자리가
          없어서 지금까지 둘이 같은 모양으로 나갔다.

        ★ **`skipped` 를 통과로 만들지 않는다.** 이 칸은 통과 여부를 바꾸지 않고
          *"왜 통과가 아닌가"* 만 가른다 — `PASSING_VERDICTS` 는 그대로다.

        ★ `unroutable`(아예 못 물어본 요구)은 **여기 넣지 않는다.** 그것은 이미 자기
          칸이 있고, 재검증도 그것으로는 실패를 내지 않는다 (`revalidation._verdict`).
        """
        return tuple(
            capability
            for capability, verdict in self.validations.items()
            if not _validation_concluded(verdict)
        )

    @property
    def rejected_validations(self) -> tuple[str, ...]:
        """부서가 **판정해서 막은** 검증. 자료가 없어 못 본 것과 섞지 않는다."""
        return tuple(
            capability
            for capability, verdict in self.validations.items()
            if _validation_concluded(verdict)
            and str(verdict.get("business_status") or "") not in PASSING_VERDICTS
        )

    @property
    def detail(self) -> str:
        """왜 탈락했나 — **사람이 읽는 한 줄. 여기서만 만든다.**

        ★ 마스터가 사유를 새로 쓰지 않는다. 부서가 보낸 `reasoning` 을 그대로 옮기고,
          없으면 상태값만 적는다 (§3.2.2 · `AgentFailure.detail` 과 같은 자리).
        """
        parts: list[str] = []
        if self.unroutable:
            parts.append(f"부를 대상이 없는 요구: {', '.join(self.unroutable)}")
        # 🔴 **부서 판정 줄과 섞이지 않게 따로 적는다.** 아래 줄은
        #   `capability(runtime/business)` 모양이고 이 줄은 칸 이름을 부른다.
        if self.missing_terms:
            parts.append(missing_terms_reason(self.missing_terms))
        for capability, verdict in self.validations.items():
            business = str(verdict.get("business_status") or "?")
            if business in PASSING_VERDICTS:
                continue
            runtime = str(verdict.get("runtime_status") or "?")
            head = f"{capability}({runtime}/{business})"
            why = str(verdict.get("reasoning") or "").strip()
            parts.append(f"{head}: {why}" if why else head)
        return " / ".join(parts) if parts else "통과"


@dataclass(frozen=True)
class SalesOutcome:
    """판매 Flow 한 번의 결과. **무엇을 못 했는지도 담는다.**

    ★ 매입 `ProcurementOutcome` 과 모양이 닮았지만 담는 것이 다르다. 매입은
      *"시나리오 배열 + 부서별 판정"* 이고 판매는 **후보마다 자기 판정을 들고 있다** —
      부분 통과가 정상이라 부서 축으로 접으면 어느 후보가 왜 떨어졌는지가 사라진다.
    """

    end_code: SalesEndCode
    reason: str
    plan: ExecutionPlan

    #: 통과·탈락을 **한 칸에** 담는다. 가르는 것은 아래 property 다 — 두 칸으로 두면
    #: 같은 후보가 양쪽에 들어가는 날을 아무도 못 막는다.
    candidates: tuple[CandidateVerdict, ...] = ()

    #: `scenarios` 를 뺀 제안 최상위 — 판매의 `situation`·`business_mode`·`self_check`.
    #: 매입 `_judgment_of` 와 같은 자리이고, **키를 고르지 않는다.**
    judgment: Mapping[str, Any] = field(default_factory=dict)

    #: ②에서 받은 초기 물류 컨텍스트. **못 받았으면 비어 있고** 그 사유는 아래 칸에 있다.
    supply_context: Mapping[str, Any] = field(default_factory=dict)

    #: 🔴 **물류가 컨텍스트를 못 냈다는 사실.** 판매는 밴드가 없어 여기서 멈추지 않지만,
    #: 멈추지 않는 것과 없던 일로 하는 것은 다르다 — 후보의 질이 왜 떨어졌는지를
    #: 나중에 읽는 사람이 볼 수 있어야 한다.
    context_failure: AgentFailure | None = None

    #: 🔴 **ML 예측을 못 실은 이유. 실었으면 빈 문자열이다** (M-1).
    #:
    #:   판매 v1.7 은 *"ML missing 은 전체 Sales 실패가 아니다"* 라고 적었다. 그래서
    #:   여기서 Flow 를 세우지 않는다 — `context_failure` 와 같은 태도다.
    #:
    #: ★ **멈추지 않는 것과 없던 일로 하는 것은 다르다.** 예측 없이 만든 후보와 예측을
    #:   보고 만든 후보는 무게가 다른데, 조용히 빠지면 화면이 둘을 같게 보여준다.
    #:
    #: ⚠️ **`AgentFailure` 가 아니다.** ML 은 호출 대상이 아니라 **입력**이라
    #:   (`inputs.py` 머리말) 부서 실패 모양에 담으면 없는 에이전트를 지어내게 된다.
    ml_context_note: str = ""

    evidences: tuple[SourcedEvidence, ...] = ()
    adjustments: tuple[SuggestedAdjustment, ...] = ()

    #: 실제로 돈 되먹임 회차. **0 이면 되먹임하지 않았다** (통과 후보가 있었거나,
    #: 권위 있는 대안이 없어 다시 물어도 같았거나).
    feedback_attempts: int = 0

    @property
    def presented(self) -> tuple[CandidateVerdict, ...]:
        return tuple(c for c in self.candidates if c.passed)

    @property
    def rejected(self) -> tuple[CandidateVerdict, ...]:
        """탈락 후보. **SL1 에서도 비어 있지 않을 수 있다** — 사유를 동봉해 함께 낸다.

        ⚠️ **이 칸에는 판정이 안 난 후보도 들어 있다** — `passed` 의 여집합이기
          때문이다. 둘을 갈라 보려면 `unresolved` 를 쓴다. 이름을 바꾸지 않는 이유는
          화면·이력이 이 칸을 쓰고 있어서다.
        """
        return tuple(c for c in self.candidates if not c.passed)

    @property
    def unresolved(self) -> tuple[CandidateVerdict, ...]:
        """**판정이 끝나지 않은** 후보. 후보는 살아 있고 승인만 못 한다.

        ★ `rejected` 의 부분집합이다 — 통과가 아니라는 점은 같고, **왜** 통과가
          아닌지가 다르다. 화면이 둘을 같은 줄로 보여 주면 사용자는 자료를 채워야
          할 날에 조건을 바꾼다.
        """
        return tuple(c for c in self.candidates if not c.passed and c.unresolved_validations)

    @property
    def unroutable_capabilities(self) -> tuple[str, ...]:
        """이번 실행에서 **못 불러 본 요구**의 전부. 후보들 것을 모아 이름만 남긴다."""
        return tuple(sorted({cap for c in self.candidates for cap in c.unroutable}))

    @property
    def presentable(self) -> bool:
        return self.end_code == "SL1_PRESENTED" and bool(self.presented)


class SalesFlow:
    """판매 Flow 실행기.

    ★ 요청마다 새로 만든다 — `MasterRunner` 가 요청 단위이기 때문이다.
    """

    def __init__(
        self,
        runner: MasterRunner,
        user_request: Mapping[str, Any] | None = None,
        business_mode: str | None = None,
        mocked_inputs: Sequence[str] = (),
        max_feedback_attempts: int = MAX_FEEDBACK_ATTEMPTS,
        forecast: Mapping[str, Any] | None = None,
        forecast_note: str = "",
        procurement_boundary: ProcurementBoundary | None = None,
    ) -> None:
        self.runner = runner
        #: 사용자가 말한 조건 그대로. **숫자로 바꿔 제약에 꽂지 않는다** — 해석은
        #: 판매가 한다 (§3.2.2 · 매입 `prior_feedback` 과 같은 자리).
        self.user_request = user_request

        #: 🔴 **무슨 판매인가 — `SalesProposalInput` 최상위 필수 칸이다.**
        #:
        #:   `SalesUserRequest` 안에 넣지 않는다. 저쪽은 `extra="forbid"` 이라 넣는
        #:   순간 문 앞에서 통째로 거부되고, 그 칸의 자리는 **한 층 위**다.
        #:
        #: ★ **어휘의 주인은 판매다** (`SalesBusinessMode`). 여기서 값을 검사하지
        #:   않는다 — 문 앞 판정은 `SalesRunRequest.business_mode` 가 이미 했고,
        #:   Flow 가 다시 세면 같은 판정의 주인이 둘이 된다.
        #:
        #: ★ **없으면 칸을 안 만든다** (§1.2-10). 진입점은 늘 들고 오지만 Flow 를
        #:   직접 만드는 자리(검사·재검증)까지 값을 강제하지는 않는다.
        self.business_mode = business_mode
        #: 🔴 **mock 에서 온 입력. 하나라도 있으면 실행을 세운다** (매입과 같은 태도).
        #: 경고와 차단은 다르다 — mock 으로 내린 결론은 실측으로 읽히면 안 되는 정도가
        #: 아니라 **아예 내리면 안 되는** 것이다.
        self.mocked_inputs: tuple[str, ...] = tuple(mocked_inputs)
        self.max_feedback_attempts = max_feedback_attempts

        #: 🔴 **ML 예측은 마스터가 실어 나른다 — 판매가 직접 안 부른다** (판매 v1.7 §11).
        #:
        #:   ML 은 호출 구조 밖의 독립 실행이라 부를 대상이 없다 (`inputs.py` 머리말).
        #:   매입이 `forecast` 로 받는 것과 **같은 값 같은 자리**이고, 이름만 받는 쪽
        #:   낱말(`ml_context`)로 바뀌어 나간다.
        #:
        #: ★ **실행 시작에 정해지고 안 바뀐다.** `as_of` 도 예측도 회차마다 안 바뀌므로
        #:   여기서 한 번 판정한다 — 회차마다 다시 재면 같은 실행 안에서 답이 갈릴
        #:   자리를 만드는 것이다 (§3.4 재현성).
        #:
        #: ⚠️ **받은 원본을 따로 들지 않는다.** `self.forecast` 와 `self.ml_context` 를
        #:   같이 두면 *"받은 것"* 과 *"실은 것"* 이 두 칸이 되고, 읽는 쪽이 어느 것을
        #:   봐야 하는지 알 수 없다. 나가는 값 하나와 못 나간 사유 하나면 족하다.
        self.ml_context, self.ml_context_note = _carriable_forecast(
            forecast, forecast_note, runner.context.as_of
        )

        #: 🔴 **매입에 실어 줄 경계 재료 — 마스터가 읽어 나른다** (`ml_context` 와 같은
        #:   자리). 매입이 낼 「가능량」의 재료가 물류·재무 봉투인데 그 값은 이미 실행
        #:   이력에 있으므로 **부서 호출이 0회**다.
        #:
        #: ★ **Flow 가 직접 조회하지 않는다.** 진입점(`service.run_sales`)이
        #:   `read_procurement_boundary` 로 읽어 넘긴다 — Flow 가 DB 를 읽으면 조립기가
        #:   적재층을 겸하게 되고, 백테스트가 그날 값을 꽂아 넣을 자리도 사라진다
        #:   (`forecast` 를 그렇게 나르는 것과 같은 이유).
        #:
        #: ★ **`None` 은 못 읽은 것이 아니라 「읽어 보지도 않았다」이다.** 못 읽은 것은
        #:   `ProcurementBoundary(present=False, absent_reason=...)` 로 온다 — 그쪽은
        #:   사유가 매입 봉투에 실린다.
        self.procurement_boundary = procurement_boundary

        #: ②의 회신을 담아 둔다. **S-1 재사용의 원본이다** — 같은 회신을 두 번 부르지
        #: 않는다는 것을 이 한 칸이 보증한다.
        self.supply_context: Mapping[str, Any] | None = None
        self.context_failure: AgentFailure | None = None

        #: 🔴 **품목 → 그 회차의 매입 회신.** 회차마다 비운다.
        #:
        #:   `_judge` 는 후보 단위인데 매입 호출은 **품목 단위**라, 후보를 돌기 전에
        #:   품목으로 묶어 부르고 그 답을 여기서 나눠 쓴다. 후보마다 부르면 같은 품목에
        #:   **같은 답이 여러 번** 오고 예산만 탄다.
        self.supply_capacity_replies: dict[str, AgentReply] = {}

        #: 🔴 **최초 판매 제안을 만든 run.** 되먹임이 여러 번 돌아도 계보를 잃지 않게
        #:   `SalesFeedback.original_run_id` 로 나간다 (판매 회신 2026-09-07).
        #:
        #: ★ **첫 회차에 정해지고 안 바뀐다.** 회차마다 덮으면 *"최초"* 가 *"직전"* 이
        #:   되고, 2차 되먹임에서 판매가 자기 1회차 안을 원본으로 읽는다.
        self.original_run_id: str | None = None

        #: 회신 원본을 `run_id` 로 찾는 자리. **회신을 베껴 두는 칸이 아니다** —
        #: 되먹임에 실을 때 `domain_replies` 가 원본을 가리키기 위한 색인이다.
        #: 판정 칸(`_verdict_of`)은 `run_id` 만 들고 있고 내용의 주인은 여기 하나다.
        self.replies_by_ref: dict[str, AgentReply] = {}

        self.sourced_evidences: list[SourcedEvidence] = []
        self.suggested_adjustments: list[SuggestedAdjustment] = []

    # ── 진입점 ──────────────────────────────────────────────────

    def run(self) -> SalesOutcome:
        """끝까지 돌린다.

        🔴 **예산 소진을 탈락으로 접지 않는다** (`SalesEndCode` 참조). 매입이
          `E3_REJECTED` 로 접는 자리에서 판매는 `SL5_BUDGET_EXHAUSTED` 로 남긴다.

        ★ **어댑터 미등록은 오류가 아니라 상태다** (§5.3). `AgentNotRegistered` 는
          `MasterRunner._invoke` 가 값으로 안 바꾸고 올리는 유일한 실패인데, 그것을
          여기서 종료 코드로 받는다 — 배선 전에도 이 Flow 가 *"시작하지 못했다"* 를
          정확히 말할 수 있어야 하기 때문이다.
        """
        try:
            return self._run()
        except BudgetExhausted as exc:
            return self._outcome("SL5_BUDGET_EXHAUSTED", f"호출 예산 소진: {exc}")
        except AgentNotRegistered as exc:
            return self._outcome("SL4_NOT_STARTED", f"에이전트 미등록: {exc}")

    def _run(self) -> SalesOutcome:
        # ① mock 이 섞이면 아무것도 시작하지 않는다 — 부르기 전에 선다.
        #   한 번이라도 부르면 그 회신이 이력에 남고, 나중에 읽는 사람이
        #   **"돌긴 돌았다"** 로 읽는다.
        if self.mocked_inputs:
            keys = " · ".join(self.mocked_inputs)
            return self._outcome(
                "SL4_NOT_STARTED",
                f"mock 입력으로는 판단하지 않는다: {keys}. "
                f"실 데이터를 못 읽은 것이므로 그 조회부터 고쳐야 한다",
            )

        # ② 초기 물류 컨텍스트.
        self._collect_supply_context()

        candidates: tuple[CandidateVerdict, ...] = ()
        judgment: Mapping[str, Any] = {}
        feedback: Mapping[str, Any] | None = None
        attempt = 0

        while True:
            # ③ 판매에게 후보를 받는다.
            proposal = self.runner.call(
                "sales", "GENERATE_SALES_PROPOSAL", self._proposal_input(attempt, feedback)
            )
            if self.original_run_id is None:
                # ★ **최초 회차에서만 잡는다.** 되먹임 회차의 run 으로 덮으면
                #   `original_run_id` 가 *"직전 회차"* 를 뜻하게 된다.
                self.original_run_id = proposal.run_id
            judgment = _judgment_of(proposal)
            self.sourced_evidences.extend(
                SourcedEvidence("sales", "GENERATE_SALES_PROPOSAL", ev) for ev in proposal.evidences
            )

            if not proposal.contributes_to_band:
                # 🔴 **`SL4` 가 아니라 `SL2` 다.** `SL4` 는 *"부르기 전에 못 섰다"* 이고,
                #   여기는 부른 뒤다. 사용자가 보는 사실은 **후보가 없다**는 것이고,
                #   왜인지는 부서가 쓴 문장 그대로 사유에 실린다.
                failure = self._failure_of("sales", "GENERATE_SALES_PROPOSAL")
                return self._outcome(
                    "SL2_NO_CANDIDATE",
                    f"판매 에이전트 미가동: {failure.detail}",
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            scenarios = _scenarios_of(proposal)
            if not scenarios:
                # ★ **되먹임 회차에서 후보가 사라져도 여기로 온다.** 그 회차가 소유하는
                #   사실은 *"안을 못 만들었다"* 이고, 앞 회차의 탈락안은 사유와 함께
                #   `candidates` 에 그대로 실려 나간다 — 둘 다 화면에 남는다.
                return self._outcome(
                    "SL2_NO_CANDIDATE",
                    _no_candidate_reason(proposal, attempt),
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            # ④ 후보마다 라우팅해 판정을 받는다.
            before = len(self.suggested_adjustments)
            # 🔴 **매입만 후보 앞에서 부른다 — 호출 단위가 품목이기 때문이다.**
            #   `_judge` 안에서 부르면 배추 후보가 셋일 때 배추를 세 번 묻는다.
            self._ask_supply_capacity(scenarios)
            candidates = tuple(self._judge(scenario) for scenario in scenarios)
            fresh_adjustments = len(self.suggested_adjustments) - before

            # ⑤ 통과 후보가 하나라도 있으면 **되먹임하지 않고 제시한다** (C-1).
            #
            #   🔴 전체 되먹임을 걸면 **통과했던 안까지 바뀌어** 사용자가 볼 수 있던
            #     안이 사라진다. 더 나은 안은 사용자가 `RERUN_WITH_CONDITION` 으로
            #     요청한다 (C-2) — 마스터가 최적안을 찾아 주는 자리가 아니다.
            if any(c.passed for c in candidates):
                return self._outcome(
                    "SL1_PRESENTED",
                    "사용자 선택 대기",
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            if attempt >= self.max_feedback_attempts:
                end_code, reason = _unpassed_outcome(
                    candidates, f"되먹임 {attempt} 회에도 통과 후보 없음"
                )
                return self._outcome(
                    end_code,
                    reason,
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            if not fresh_adjustments:
                # 🔴 **권위 있는 대안이 없으면 다시 물어도 같다** (C-2).
                #   되먹임에 실을 것이 없는데 부르면 호출 예산과 LLM 만 태운다
                #   (§1.2-12 · 매입 `_dept_blocked` 와 같은 판단).
                #
                # ★ 판정을 못 낸 후보에는 이 판단이 **더 강하게** 맞는다. 재무가
                #   여신한도를 못 읽은 것은 같은 후보를 다시 만들어서 풀리는 문제가
                #   아니다 — 그래서 여기서 접는 것은 그대로 두고, 접힌 결과에
                #   **무엇이 끝나지 않았는지**만 정확히 적는다.
                end_code, reason = _unpassed_outcome(
                    candidates, "통과 후보가 없고 부서가 낸 대안도 없다 — 다시 물어도 같다"
                )
                return self._outcome(
                    end_code,
                    reason,
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            attempt += 1
            # ★ **회차 값을 여기서 안 적는다.** `attempt` 를 봉투에 찍는 자리는
            #   `_proposal_input` 하나다 — 최상위 `feedback_attempt` 와
            #   `feedback.attempt` 가 **같은 값이어야** 판매가 받아 주기 때문이다.
            feedback = self._feedback(candidates)

    # ── 단계 ────────────────────────────────────────────────────

    def _collect_supply_context(self) -> None:
        """② 물류에게 판매가능·납기 컨텍스트를 받는다.

        🔴 **밴드를 세우지 않는다.** 매입은 조언자가 하나라도 빠지면
          `band_is_formed` 로 시작조차 안 하지만, 판매는 물류가 못 답해도 **시작은
          한다** — 시나리오의 질이 떨어질 뿐이다 (설계 §1-2).

          매입의 `band_is_formed` 를 여기 쓰면 두 가지가 동시에 틀린다. 그 함수는
          `PRE_PURCHASE` 만 보므로 `PRE_SALES` 회신은 **영원히 안 선 것으로 읽히고**,
          설령 고쳐 쓰더라도 판매에 없는 게이트를 만들게 된다.

        ★ **못 받았으면 그 사실을 남긴다.** 조용히 건너뛰면 나중에 읽는 사람이
          *"물류가 괜찮다고 했다"* 로 읽는다 (§1.2-10).
        """
        agent, mode = INITIAL_CONTEXT_ROUTE
        reply = self.runner.call(agent, mode, self._context_input())
        self.replies_by_ref[reply.run_id] = reply
        self.sourced_evidences.extend(SourcedEvidence(agent, mode, ev) for ev in reply.evidences)
        self.suggested_adjustments.extend(reply.suggested_adjustments)
        if reply.contributes_to_band:
            self.supply_context = _verdict_of(reply)
        else:
            # 🔴 회신 자체는 버리지 않는다. 후보가 `SELLABLE_SUPPLY_CONTEXT` 를
            #   요구하면 **이 못 받은 회신이 그대로 그 자리에 들어가** 탈락 사유가
            #   된다 — 다시 부르지 않는다 (`RUNTIME_NOT_READY` 는 다시 불러도 같다).
            self.supply_context = _verdict_of(reply)
            self.context_failure = self._failure_of(agent, mode)

    def _context_input(self) -> dict[str, Any]:
        """② 에 실어 보내는 것. **사용자 조건을 그대로 나른다.**

        ★ **없으면 칸을 안 만든다.** 빈 매핑을 보내면 받는 쪽이 *"조건이 없었다"* 와
          *"마스터가 안 보낸다"* 를 구별할 수 없다 (§1.2-10).
        """
        payload: dict[str, Any] = {}
        if self.user_request is not None:
            payload["user_request"] = dict(self.user_request)
        return payload

    def _proposal_input(self, attempt: int, feedback: Mapping[str, Any] | None) -> dict[str, Any]:
        """③ 에 실어 보내는 것. **묶기만 한다** (§3.2.2).

        🔴 **회차를 찍는 자리는 여기 하나다** (판매·물류 회신 2026-09-07).

          ```text
          feedback_attempt          최상위. 마스터가 소유하는 회차
          feedback.attempt          같은 값. 다르면 판매가 계약 오류로 닫는다
          ```

          같은 값을 두 곳에 적는 자리라 **두 곳을 한 문장이 쓴다.** `_feedback` 이
          자기 몫을 따로 찍으면 두 값이 갈리는 날이 오고, 그날 되먹임이 통째로
          거부되는데 마스터 쪽에서는 아무 소리가 안 난다.

        🔴 **`is_refeed` 를 싣지 않는다.** 판매가 `feedback_attempt > 0` 으로 정한다
          (판매 회신). 같이 실으면 *"되먹임인가"* 의 주인이 둘이 되고, 한쪽만
          채워지는 날 판매가 1회차를 최초 호출로 읽는다.

        ★ **물류가 못 답한 회차에는 물류 컨텍스트 칸을 안 만든다.** 빈 값을 실으면
          판매가 *"물류가 팔 수 있는 게 없다고 했다"* 로 읽는다. 안 실으면 판매가
          `missing_capabilities` 로 그 사실을 낸다 — 그것이 §1.2-10 이 원하는 모양이다.

        🔴 **`ml_context` 도 같은 규칙이다** (M-1). 못 읽었거나 look-ahead 로 걸렸으면
          **칸을 아예 안 만든다.** `None` 을 실으면 판매가 *"예측이 없었다"* 와
          *"마스터가 안 보냈다"* 를 구별할 수 없다 — 매입 `_purchase_input` 이 같은
          이유로 그렇게 한다. 못 실은 사실은 `ml_context_note` 로 결과에 남는다.
        """
        payload: dict[str, Any] = {}
        # 🔴 **최상위다 — `feedback` 안이 아니다** (`SalesProposalInput.feedback_attempt`).
        #
        #   전에는 `feedback_context["feedback_attempt"]` 안에 있었다. 어댑터는
        #   `request.payload["feedback_attempt"]` 를 최상위에서 읽으므로 **되먹임
        #   회차에도 0 이 나갔고**, `is_refeed` 가 영원히 `False` 였다 —
        #   되먹임 상한 2 를 정해 두고 회차가 한 번도 전달되지 않았다.
        #
        # ★ **최초 호출에도 싣는다** (`0`). 없는 것과 0 인 것을 판매가 구별할 필요가
        #   없는 자리라 (기본값이 0 이다) 늘 같은 모양으로 나가는 편이 낫다 — 칸이
        #   회차마다 생겼다 사라지면 이력에서 두 모양을 비교해야 한다.
        if self.business_mode is not None:
            # 🔴 **최상위다 — `user_request` 안이 아니다** (`SalesProposalInput`).
            payload["business_mode"] = self.business_mode
        if self.user_request is not None:
            payload["user_request"] = dict(self.user_request)
        if self.context_failure is None and self.supply_context is not None:
            # 🔴 **칸 이름은 `logistics_context` 다** (`sales.schemas.SalesProposalInput`).
            #
            #   막고 있던 둘이 다 풀렸다 (2026-09-10 · 물류 PR #484 수신요청 §3).
            #
            #   ```text
            #   ① 마스터가 싣던 것이 물류 payload 가 아니라 `_verdict_of` 봉투 래퍼였다
            #      → 마스터 소유. 여기서 payload 만 벗겨 낸다
            #   ② SalesLogisticsContext(extra="forbid") 가 다섯 키를 거부했다
            #      → #509 로 해소. 지금은 받는 칸 일곱과 물류 PRE_SALES 최상위 일곱이
            #        정확히 같다 (query_scope · sellable_supply · delivery_feasibility
            #        · hard_constraints · soft_warnings · missing_data · evidence_refs)
            #   ```
            #
            # 🔴 **payload 안을 재조립하지 않는다** (물류 §3 금지 목록).
            #   `sellable_supply` 를 풀어 최상위 `inventory_by_item` 을 다시 만들거나,
            #   구·신 경로를 둘 다 채우거나, 호환용 중복키를 만들지 않는다 — 그러면
            #   같은 사실의 주인이 둘이 되고 물류가 주소를 바꾸는 날 둘이 갈린다.
            #
            # ★ **래퍼에 payload 매핑이 없으면 칸을 안 만든다.** 빈 칸을 실으면 판매가
            #   *"물류가 팔 수 있는 게 없다고 했다"* 로 읽는다 (§1.2-10) — 위 두 가드와
            #   같은 규율이다.
            context_payload = self.supply_context.get("payload")
            if isinstance(context_payload, Mapping):
                payload["logistics_context"] = dict(context_payload)
        if self.ml_context is not None:
            # ★ **칸 이름은 판매 것이다** (`app/sales/schemas.py` `SalesProposalInput`).
            #   매입은 같은 값을 `forecast` 로 받는다 — 받는 쪽 낱말에 맞춘다
            #   (`feedback_attempt` 와 같은 자리).
            payload["ml_context"] = dict(self.ml_context)
        if feedback is not None:
            # 🔴 **칸 이름은 `feedback` 이다** (`SalesProposalInput.feedback`).
            #   `feedback_context` 는 판매 스키마에 없는 이름이라 `extra="forbid"`
            #   문 앞에서가 아니라 **어댑터의 키 거르기에서 조용히 버려졌다.**
            #
            # 🔴 **`adjustments` 최상위 칸을 만들지 않는다** (판매 회신 2026-09-07).
            #   `SuggestedAdjustment` 는 그 부서 회신의 일부이므로
            #   `domain_replies[n].payload` 안에 보존한다 — 최상위로 따로 빼면 어느
            #   회신이 낸 대안인지가 사라지고, 같은 사실이 두 곳에 앉는다.
            payload["feedback"] = {**dict(feedback), "attempt": attempt}

        # 🔴 **회차는 최상위에도 싣는다** (판매 회신 2026-09-07).
        #
        #   판매 어댑터가 `request.payload["feedback_attempt"]` 를 **최상위에서** 읽고,
        #   그 값으로 `is_refeed` 를 정한다. 되먹임 안에만 넣어 두던 동안에는 회차가
        #   0 으로 고정돼 **되먹임이 한 번도 안 돈 것으로 처리됐다.**
        #
        # ★ **`is_refeed` 는 안 싣는다.** 판매가 `feedback_attempt > 0` 으로 정한다 —
        #   같은 사실의 주인을 둘로 만들지 않는다 (판매 회신).
        #
        # ⚠️ **`feedback.attempt` 와 같은 값이어야 한다.** 다르면 판매가 조용히 하나를
        #   고르지 않고 계약 오류로 닫는다. 같은 값을 두 곳에 적는 자리라 **한 인자
        #   (`attempt`)에서 둘 다 나오게** 둔다 — 두 곳에서 따로 세면 언젠가 갈린다.
        payload["feedback_attempt"] = attempt
        return payload

    def _ask_supply_capacity(self, scenarios: Sequence[Mapping[str, Any]]) -> None:
        """④ 앞 — 부족 품목마다 **한 번씩** 매입에 경계를 묻는다.

        ```text
        후보 셋이 다 배추 400·700·550   →  배추 1회 · 요청량 700
        후보 셋이 배추·무·양파          →  3회
        부족량이 없는 후보              →  안 부른다
        ```

        🔴 **같은 품목을 여러 번 묻지 않는다.** 매입은 품목 하나를 받아 하나를 답하므로
          (`sales.schemas.PurchaseAdditionalSupplyResult` 가 최상위 단수) 같은 품목을
          두 번 물으면 **같은 답이 두 번** 온다 — 예산만 타고 사실은 안 는다.

        🔴 **요청량은 그 품목 후보 중 가장 큰 것이다.** 묻는 것이 *"얼마까지 되나"* 라
          작은 쪽으로 물으면 답이 그만큼 잘린다. 700 이 되는지 물어야 400 후보도 같이
          판정할 수 있고, 400 으로 물으면 700 후보는 다시 물어야 한다.

        🔴 **경계를 못 읽어도 부른다** (매입 `#485` §1.2). 재료가 없으면 매입이
          `procurable_quantity_kg=null` · `basis=unknown` · `risks` 에 사유로 답하는
          것이 계약이다. 여기서 건너뛰면 그 계약이 쓰이지 않고, 화면에는 *"안 왔다"* 만
          남아 **매입에 물어봤는지조차** 안 보인다.

        ★ **회차마다 다시 묻는다.** 되먹임으로 후보가 바뀌면 부족량도 바뀐다 — 앞
          회차 답을 재사용하면 새 부족량에 옛 경계를 붙이게 된다 (S-1 재사용이
          ②에만 걸리는 것과 같은 이유).

        ⚠️ **미등록은 이 사이클을 세우지 않는다.** 매입은 부족량이 있는 후보에만
          필요한 **조건부**라 `wiring.REQUIRED_FOR_SALES` 에 없다 (그 docstring).
          여기서 `AgentNotRegistered` 를 올리면 `run()` 이 `SL4_NOT_STARTED` 로 받아
          *"시작하지 못했다"* 가 되는데, 실제로는 **후보까지 다 받은 뒤**다.
          못 물어봤다는 사실은 `unroutable` 로 후보에 남는다.
        """
        self.supply_capacity_replies = {}
        # 🔴 **경로를 손으로 적지 않는다** (`INITIAL_CONTEXT_ROUTE` 와 같은 규율).
        #   `("purchase", "SUPPLY_CAPACITY_QUERY")` 를 여기 박으면 **라우팅표가 이
        #   호출의 주인이 아니게 된다** — 표를 `None` 으로 되돌려도 여기서는 그대로
        #   부르고, `_judge` 만 *"부를 대상이 없다"* 로 답한다. 한 capability 에 대해
        #   **부르는 쪽과 판정하는 쪽이 서로 다른 답**을 갖는 상태다.
        route = route_capability(ADDITIONAL_SUPPLY_CAPABILITY)
        if route is None:
            return
        agent, mode = route
        for item, requested in _additional_supply_requests(scenarios).items():
            try:
                reply = self.runner.call(
                    agent, mode, self._supply_capacity_input(item, requested)
                )
            except AgentNotRegistered:
                continue
            self.replies_by_ref[reply.run_id] = reply
            self.supply_capacity_replies[item] = reply
            self.sourced_evidences.extend(
                SourcedEvidence(agent, mode, ev) for ev in reply.evidences
            )
            self.suggested_adjustments.extend(reply.suggested_adjustments)

    def _supply_capacity_input(self, item: str, requested: float) -> dict[str, Any]:
        """매입에 나가는 봉투 하나 — **부족량과 경계 재료.**

        🔴 **`procurable_quantity_kg` 를 여기서 계산하지 않는다.** 그 나눗셈에 쓰는
          단가가 매입 것이라, 마스터가 계산하면 단가가 바뀌는 날 두 값이 갈리고
          **그때 어느 쪽이 참인지 아무도 말해 주지 않는다.** 재료만 준다.

        🔴 **못 읽은 값을 `0` 으로 채우지 않는다.** `None` 이 *"못 읽었다"* 이고
          `0.0` 은 *"자리가 없다"* 다 — `rental_cap_kg` 는 실측이 실제로 `0.0` 인데
          **그건 읽은 값이다.** 둘을 뭉개면 매입이 자리가 없다고 답한다.

        ★ **`source_ref` 를 같이 싣는다.** 그 경계는 「그날 매입 판단 시점」의 값이라
          판단 뒤 출고가 나가면 창고가 바뀐다 — 어느 실행의 언제 값인지를 숨기지
          않는다 (§3.2).
        """
        payload: dict[str, Any] = {
            "item": item,
            # ★ **매입이 읽는 칸 이름 그대로다** (`_supply_capacity_query`). 매입은 이
            #   값을 회신에 `requested_quantity_kg` 로 되싣는데, **읽는 이름과 되싣는
            #   이름이 다르다** — 되싣는 쪽 이름으로 보내면 조용히 안 실린다.
            "required_additional_quantity_kg": requested,
        }
        boundary = self.procurement_boundary
        for field_name in BOUNDARY_FIELDS:
            payload[field_name] = getattr(boundary, field_name, None)
        if boundary is not None and boundary.source_ref:
            payload["source_ref"] = boundary.source_ref
        if boundary is not None and boundary.absent_reason is not None:
            # ★★ **매입 `basis="unknown"` 을 푸는 값이다.** 그 값만으로는 *"마스터가
            #   안 실었다"* 와 *"실렸는데 계산이 안 됐다"* 가 뭉개진다. 사유가 옆에
            #   있으면 화면이 **"토요일이라 못 물어봤다"** 까지 말한다.
            payload["supply_context_absent"] = boundary.absent_reason
        return payload

    def _judge(self, scenario: Mapping[str, Any]) -> CandidateVerdict:
        """④ 후보 하나의 `required_validations` 를 라우팅해 판정을 모은다.

        🔴 **요청 단위가 아니라 후보 단위다** (설계 정정 ①). 재무 `SALES_VALIDATION`
          은 배열을 안 받으므로 **후보 3개면 재무를 3번 부른다** (설계 정정 ②).

        ★ **S-1 재사용.** 라우팅이 ②와 같은 곳을 가리키면 그 회신을 다시 쓴다 — 같은
          `as_of`, 같은 요청 안이라 다시 불러도 같다.

          ⚠️ **최종 재검증에서는 재사용이 금지다** (C-3). 그쪽의 목적은 *"그 사이
            바뀌었는가"* 라 재사용이 규칙 위반이다. 최종 재검증은 `revalidation.py` 에
            있고 **이 줄을 옮겨 가지 않았다** — capability 마다 오늘 다시 부른다.
            그 자국은 `tests/master/test_final_revalidation.py` 가 잰다.

          ⚠️ 후보의 수량·날짜 범위가 초기 조회 범위를 벗어나면 재사용하지 않고 다시
            불러야 한다 (판매 v1.7 §5). **그 범위 대조는 아직 배선하지 않았다** —
            `SALES_BUDGET` 이 +2 로 자리만 잡아 두었다.
        """
        validations: dict[str, Mapping[str, Any]] = {}
        unroutable: list[str] = []

        for capability in _required_validations(scenario):
            route = route_capability(capability)
            if route is None:
                # 🔴 **조용히 건너뛰지 않는다.** 건너뛰면 *"검증됐다"* 로 읽힌다.
                unroutable.append(capability)
                continue
            if route == INITIAL_CONTEXT_ROUTE and self.supply_context is not None:
                validations[capability] = self.supply_context
                continue
            if capability == ADDITIONAL_SUPPLY_CAPABILITY:
                # ★ **이미 물었다.** 호출 단위가 품목이라 `_ask_supply_capacity` 가
                #   후보를 돌기 전에 부르고, 여기서는 그 답을 나눠 쓴다.
                reply = self.supply_capacity_replies.get(_item_of(scenario))
                if reply is None:
                    # 🔴 **조용히 건너뛰지 않는다** — 건너뛰면 *"검증됐다"* 로 읽힌다.
                    #   여기 오는 길은 둘이고 **둘 다 "못 물어봤다"** 이다.
                    #     ① 매입 어댑터가 등록돼 있지 않다
                    #     ② 후보가 이 검증을 요구했는데 부족량이 안 실렸다
                    #   ②는 판매 계약상 안 나온다 (`additional_supply_required` 가
                    #   부족량 > 0 과 한 몸이다). 그래도 조용히 통과시키지 않는다.
                    unroutable.append(capability)
                    continue
                validations[capability] = _verdict_of(reply)
                continue
            agent, mode = route
            # ★ 후보를 **그대로** 보낸다. 재무 `parse_sales_validation_input` 이 읽는
            #   `scenario_id`·`quantity_kg`·`supply` 가 전부 후보 최상위에 있다 —
            #   마스터가 골라 담으면 판매가 필드를 늘린 날 조용히 빠진다.
            reply = self.runner.call(agent, mode, dict(scenario))
            self.replies_by_ref[reply.run_id] = reply
            validations[capability] = _verdict_of(reply)
            self.sourced_evidences.extend(
                SourcedEvidence(agent, mode, ev) for ev in reply.evidences
            )
            self.suggested_adjustments.extend(reply.suggested_adjustments)

        return CandidateVerdict(
            scenario=dict(scenario),
            validations=validations,
            unroutable=tuple(unroutable),
            # 🔴 **누락의 위치를 가르는 데만 쓴다** (`missing_terms`). 이 둘이 안 실리면
            #   계약 이행 경로까지 *"호출에서 안 실렸다"* 로 읽힌다 — 거짓말이 된다.
            user_request=self.user_request,
            business_mode=self.business_mode,
        )

    def _feedback(self, candidates: Sequence[CandidateVerdict]) -> dict[str, Any]:
        """다음 회차에 실을 되먹임 — **판매 `SalesFeedback` 모양 그대로.**

        ```text
        original_run_id   최초 판매 제안을 만든 run
        domain_replies[]  부서 회신 원본 (source_agent · capability · reply_ref
                          · runtime_status · business_status · payload)
        scenario_feedback[]  scenario_id · reply_refs — **연결만 한다**
        ```

        🔴 **`attempt` 는 여기서 안 찍는다.** 최상위 `feedback_attempt` 와 같은 값이어야
          하므로 두 칸을 한 자리(`_proposal_input`)에서 쓴다.

        🔴 **원본을 나른다. 재작성하지 않는다** (판매 회신 2026-09-07).

          전에는 마스터가 `reason`·`rejected[].detail` 로 **자기 문장을 지어** 보냈다.
          그 문장은 부서 회신에서 마스터가 고른 것이고, 고르는 것이 곧 판단이다
          (§3.2.2). 이제는 부서 회신을 통째로 실어 **판매가 직접 읽는다.**

        🔴 **scenario 별 payload 를 만들지 않는다** (판매 회신 명시). `scenario_feedback`
          은 *"이 후보에 이 회신들이 왔다"* 만 말한다. 요약을 끼워 넣으면 판매가 읽는
          사실의 주인이 마스터가 된다.

        🔴 **`scenario_id` 를 `SalesDomainReply` 에 새로 채우지 않는다** (D-2 합의).
          그 칸은 deprecated 이고, 후보 ↔ 회신 연결의 주인은 `scenario_feedback` 이다.

        ★ **회신 하나는 한 번만 싣는다.** S-1 재사용으로 같은 물류 회신이 후보 셋에
          걸리면 목록에 셋이 아니라 하나가 실리고, 셋은 `reply_refs` 로 그것을
          가리킨다 — 그래서 이 두 칸이 나뉘어 있다.

        ★ **시각·난수·외부조회를 넣지 않는다** (§3.4). 되먹임이 앞 회차 산출물에서만
          나와야 같은 입력에 같은 다음 회차가 나온다.
        """
        domain_replies: dict[tuple[str, str], dict[str, Any]] = {}
        scenario_feedback: list[dict[str, Any]] = []

        for candidate in candidates:
            reply_refs: list[str] = []
            for capability, verdict in candidate.validations.items():
                ref = str(verdict.get("run_id") or "")
                reply = self.replies_by_ref.get(ref)
                if reply is None:
                    # 회신 원본을 못 찾으면 **가리키지 않는다.** 없는 것을 가리키는
                    # `reply_ref` 는 판매 쪽에서 빈 회신으로 읽힌다.
                    continue
                if ref not in reply_refs:
                    reply_refs.append(ref)
                domain_replies.setdefault((capability, ref), _domain_reply(capability, reply))
            scenario_feedback.append(
                {"scenario_id": candidate.scenario_id, "reply_refs": reply_refs}
            )

        return {
            "original_run_id": self.original_run_id,
            "domain_replies": list(domain_replies.values()),
            "scenario_feedback": scenario_feedback,
        }

    def _failure_of(self, agent: AgentName, mode: Mode) -> AgentFailure:
        """기여하지 못한 부서의 **사유를 실행 계획에서 꺼낸다.**

        ★ **회신이 아니라 계획을 본다** — 매입 `_failures_of` 와 같은 이유다.
          *"무슨 일이 일어났나"* 의 단일 출처를 하나로 둔다.
        """
        step = self.runner.plan.last(agent, mode)
        if step is None:
            # 회신 자체가 없다. `RuntimeStatus` 로는 적을 수 없는 상태다.
            return AgentFailure(agent, "NOT_CALLED")
        return AgentFailure(
            agent,
            step.runtime_status,
            reasoning=step.reasoning,
            missing_data=tuple(step.missing_data),
        )

    def _outcome(self, end_code: SalesEndCode, reason: str, **kw: Any) -> SalesOutcome:
        return SalesOutcome(
            end_code=end_code,
            reason=reason,
            plan=self.runner.plan,
            # ★ **모든 종료 코드에서 싣는다.** 후보가 안 나온 날이야말로 *"무슨 근거로
            #   그렇게 됐나"* 가 필요하다 — 성공한 날만 근거를 보여주면 정작 설명이
            #   필요한 날에 화면이 침묵한다 (매입과 같은 자리).
            evidences=tuple(self.sourced_evidences),
            adjustments=tuple(self.suggested_adjustments),
            supply_context=dict(self.supply_context or {}),
            context_failure=self.context_failure,
            # ★ **모든 종료 코드에서 싣는다** — 근거와 같은 이유다. 후보가 안 나온 날
            #   *"예측을 못 실었다"* 가 그 이유의 일부일 수 있다.
            ml_context_note=self.ml_context_note,
            **kw,
        )


# ---------------------------------------------------------------------------
# 회신 읽기 — 마스터는 **꺼내기만** 한다
# ---------------------------------------------------------------------------


def _carriable_forecast(
    forecast: Mapping[str, Any] | None, note: str, as_of: date
) -> tuple[dict[str, Any] | None, str]:
    """실어도 되는 예측인가. **실을 것과 못 실은 이유를 같이 돌려준다.**

    🔴 **둘을 따로 돌려주면 같은 사실의 주인이 둘이 된다.** *"안 실었다"* 와 *"왜 안
      실었나"* 가 갈리면 한쪽만 채워지는 날이 오고, 그날 화면은 예측이 빠진 것을
      모른 채 후보를 보여준다.

    ★ **look-ahead 대조는 봉투 것을 그대로 부른다** (`forecast_is_clean`). 매입과 같은
      규칙이어야 하므로 여기서 다시 쓰지 않는다 — 베끼면 어휘가 갈리는 날 판매만
      미래를 본다.

    ★ **진입점이 준 사유가 있으면 그것을 쓴다.** 못 읽은 이유(`SourcedInput.note`)를
      아는 곳은 적재층이고, Flow 는 자기가 아는 이유(look-ahead)만 쓴다.
    """
    if forecast is None:
        return None, note or "ML 예측을 못 받아 싣지 않았다"
    if not forecast_is_clean(forecast, as_of):
        return None, (
            f"ML 예측 `generated_at` 이 {as_of.isoformat()} 이후이거나 타임존이 없어 "
            "싣지 않았다 — 오늘 이후에 생성된 예측으로 오늘을 판단할 수 없다"
        )
    return dict(forecast), ""


def _domain_reply(capability: str, reply: AgentReply) -> dict[str, Any]:
    """부서 회신 하나를 판매 `SalesDomainReply` 모양으로. **원본을 나른다.**

    🔴 **`extra="forbid"` 이라 여기 적힌 칸 말고는 못 보낸다.** 칸을 하나 더 실으면
      되먹임이 통째로 거부된다.

    🔴 **`source_agent` 만 이름을 바꾼다** (`FEEDBACK_SOURCE_AGENT`). 나머지는 부서가
      보낸 값 그대로다.

    ⚠️ **조정안을 `payload` 안 `suggested_adjustments` 키로 붙인다.**

      봉투는 `suggested_adjustments` 를 `payload` 의 **형제**로 두는데
      (`AgentReply.suggested_adjustments`) 판매 계약에는 그 형제 칸이 없다. 그래서
      나가는 자리에서 합쳐야 하는데, **부서 payload 는 한 글자도 안 고치고** 표준형
      조정안을 옆에 한 칸으로 얹는 것까지만 한다 — 값을 골라 다시 쓰면 그 순간
      마스터가 부서 회신의 주인이 된다.

      붙이는 이름은 **봉투에서 그 칸이 갖던 이름 그대로**다. 다른 이름을 지어내면
      받는 쪽이 그것이 무엇인지 마스터 코드를 읽어야 알 수 있다.

      🔴 **낼 것이 없으면 칸을 안 만든다** (§1.2-10). 빈 목록을 실으면 판매가
        *"부서가 대안이 없다고 했다"* 로 읽는다.
    """
    payload = wire_payload(dict(reply.payload))
    if reply.suggested_adjustments:
        payload["suggested_adjustments"] = [wire_adjustment(a) for a in reply.suggested_adjustments]
    return {
        "source_agent": FEEDBACK_SOURCE_AGENT.get(reply.agent, reply.agent),
        "capability": capability,
        # ★ **`AgentReply.run_id` 다** (C-4 합의). 회신 한 번을 가리키는 유일한 키다.
        "reply_ref": reply.run_id,
        "runtime_status": reply.runtime_status,
        "business_status": reply.business_status,
        "payload": payload,
    }


def _unpassed_outcome(
    candidates: Sequence[CandidateVerdict], rejected_reason: str
) -> tuple[SalesEndCode, str]:
    """통과 후보가 하나도 없을 때 **무엇이라고 적을 것인가.**

    ```text
    판정이 안 난 후보가 하나라도 있다  → SL6_VALIDATION_UNRESOLVED
    전부 판정이 났고 전부 안 된다      → SL3_ALL_REJECTED
    ```

    🔴 **하나라도 미판정이면 `SL6` 다.** 섞여 있을 때 `SL3` 으로 적으면 *"전부
      탈락"* 이 되는데, 판정을 안 받은 안은 탈락한 적이 없다. 반대로 전부 탈락한
      날까지 `SL6` 으로 적으면 이번에는 **끝난 판단을 안 끝났다**고 적는 것이라,
      사용자가 오지 않을 답을 기다린다.

    ★ **사유에 부서 이름을 새로 쓰지 않는다.** 무엇이 왜 끝나지 않았는지는 이미
      후보의 `detail` 과 `validations` 에 부서가 쓴 문장 그대로 있다 — 여기서는
      **어느 후보의 어느 검증**이 안 끝났는지 이름만 부른다.
    """
    unresolved = {
        capability
        for candidate in candidates
        for capability in candidate.unresolved_validations
    }
    if not unresolved:
        return "SL3_ALL_REJECTED", rejected_reason
    return "SL6_VALIDATION_UNRESOLVED", (
        f"통과 후보가 없지만 탈락도 아니다 — 판정이 끝나지 않은 검증: "
        f"{', '.join(sorted(unresolved))}"
    )


def _verdict_of(reply: AgentReply) -> dict[str, Any]:
    """회신 하나를 후보 판정 칸에 담는 모양으로.

    ★ **`agent`·`mode` 를 같이 담는다.** capability → 부서 매핑은 마스터만 아는
      사실이라, 담지 않으면 화면이 *"FINANCIAL_VALIDATION 이 reject"* 까지만 알고
      **누가 그렇게 말했는지**를 모른다.

    ★ **`run_id` 는 원본을 가리키는 포인터다.** 되먹임에 실을 때 이 값으로
      `SalesFlow.replies_by_ref` 에서 회신 원본을 찾는다 — 판정 칸이 회신 내용을
      베껴 두면 같은 사실의 주인이 둘이 된다.

    ★ **`missing_data` 를 목록으로 편다.** 이 dict 는 화면·이력까지 나가는데 튜플은
      JSON 을 한 번 왕복하면 목록이 된다 (#175 · `wire_payload` 와 같은 규율).
      `revalidation._verdict_of` 가 이미 목록으로 적고 있어 **두 경로가 같아진다.**
    """
    return {
        "agent": reply.agent,
        "mode": reply.mode,
        "run_id": reply.run_id,
        "business_status": reply.business_status,
        "runtime_status": reply.runtime_status,
        "payload": wire_payload(dict(reply.payload)),
        "reasoning": reply.reasoning,
        "missing_data": list(reply.missing_data),
    }


def _required_validations(scenario: Mapping[str, Any]) -> tuple[str, ...]:
    """후보가 요구한 capability. **순서를 지킨다** — 판매가 적은 차례 그대로 부른다.

    ★ 문자열이 아닌 것은 버린다. 어휘 밖 문자열은 **안 버린다** — 그것은
      `route_capability` 가 `None` 으로 받아 `unroutable` 에 남는다.
    """
    raw = scenario.get("required_validations")
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _item_of(scenario: Mapping[str, Any]) -> str:
    """후보의 품목. **단수다** (`sales.schemas.SalesScenario.item` — 실측 `:496`)."""
    item = scenario.get("item")
    return item if isinstance(item, str) else ""


def _shortage_of(scenario: Mapping[str, Any]) -> float | None:
    """그 후보의 부족량. 없거나 숫자가 아니면 `None`.

    🔴 **`supply` 한 겹 안이다** (`sales.schemas.ScenarioSupply`). 최상위에서 찾으면
      늘 `None` 이 나오고, 그러면 **아무 후보도 매입에 안 물어보는데 오류는 안 난다.**

    ⚠️ **`confirmed_quantity_kg` 도 `conditional_quantity_kg` 도 아니다.** 셋은 서로
      다른 사실이고 그 파일이 *"섞거나 합산하지 않는다"* 로 못 박았다 — 확보 가능량을
      부족량 자리에 넣으면 이미 확보된 만큼을 다시 사 달라고 묻게 된다.
    """
    supply = scenario.get("supply")
    if not isinstance(supply, Mapping):
        return None
    value = supply.get("required_additional_quantity_kg")
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return float(value)


def _additional_supply_requests(
    scenarios: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    """품목 → 그 품목에 물을 요청량. **묶는 자리가 여기 하나다.**

    ```text
    요구 안 함            건너뛴다 — 마스터가 요구를 지어내지 않는다 (§3.2.2)
    부족량 없음 · 0 이하   건너뛴다 — 모자라지 않은데 더 대 달라고 묻지 않는다
    같은 품목 여럿        가장 큰 부족량 하나로 묶는다
    ```

    ★ **입력 순서를 지킨다.** 판매가 낸 후보 차례대로 묻는다 — `dict` 가 삽입 순서를
      지키므로 같은 후보 목록에 늘 같은 호출 순서가 나온다 (§3.4 재현성).
    """
    wanted: dict[str, float] = {}
    for scenario in scenarios:
        if ADDITIONAL_SUPPLY_CAPABILITY not in _required_validations(scenario):
            continue
        item = _item_of(scenario)
        shortage = _shortage_of(scenario)
        if not item or shortage is None or shortage <= 0:
            continue
        if shortage > wanted.get(item, 0.0):
            wanted[item] = shortage
    return wanted


def _scenarios_of(reply: AgentReply) -> tuple[Mapping[str, Any], ...]:
    raw = reply.payload.get("scenarios", ())
    if isinstance(raw, Mapping) or not isinstance(raw, Sequence):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _judgment_of(reply: AgentReply) -> Mapping[str, Any]:
    """`scenarios` 를 뺀 제안 최상위. **키를 고르지 않는다.**

    화이트리스트로 뽑으면 판매가 판정 필드를 추가할 때마다 마스터를 고쳐야 하고,
    빠뜨린 키는 §3.7.6 의 *"커버리지를 감춘"* 상태가 된다 (매입 `_judgment_of` 와 같다).
    """
    return {k: v for k, v in reply.payload.items() if k != "scenarios"}


def _no_candidate_reason(reply: AgentReply, attempt: int) -> str:
    """`SL2` 사유 한 줄. **판매가 쓴 문장을 그대로 쓰고, 없으면 봉투 칸을 적는다.**

    ★ 봉투에 이미 자리가 있는 두 칸(`missing_data`·`missing_capability`)만 읽는다.
      판매 payload 의 `missing_capabilities` 를 파면 마스터가 남의 스키마를 해석하는
      것이 된다 — 그 값은 `judgment` 에 통째로 실려 화면까지 간다.
    """
    parts: list[str] = [reply.reasoning.strip() or "실행 가능한 판매안이 없다"]
    if reply.missing_data:
        parts.append(f"없는 입력: {', '.join(reply.missing_data)}")
    if reply.missing_capability:
        parts.append(f"없는 capability: {', '.join(reply.missing_capability)}")
    if attempt:
        parts.append(f"되먹임 {attempt} 회차")
    return " / ".join(parts)


__all__ = [
    "INITIAL_CONTEXT_ROUTE",
    "MAX_FEEDBACK_ATTEMPTS",
    "SALES_BUDGET",
    "CandidateVerdict",
    "SalesEndCode",
    "SalesFlow",
    "SalesOutcome",
    "sales_call_budget",
]
