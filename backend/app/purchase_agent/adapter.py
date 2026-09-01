"""마스터 포트 — ``AgentRequest`` → 그래프 → ``(AgentReply, ExecutionMetadata)``.

**그래프를 바꾸지 않는다.** payload를 State로 펴서 기존 ``build_graph()``를 부르고, 나온
제안을 봉투에 담는 것이 전부다. 노드·스키마·불변조건은 그대로이고, 회귀 테스트 전량이
어댑터를 거치지 않는 경로로 계속 돈다 (IO명세 §2-B).

계약 근거: 정의서 v2.3 §3.2.2·§3.2.5 · M-1 §5~§8 · `매입Agent_필요데이터_260827.md`.

**시점은 요청이 준다** (규칙 1). ``context.as_of``만 보고 벽시계를 읽지 않으므로 과거
날짜로도 그대로 돈다 — 백테스트가 성립하는 근거다.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from math import isfinite
from typing import Any

from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionMetadata,
    LLMStatus,
)
from app.orchestrator.contracts_core import Evidence
from app.purchase_agent import AGENT_VERSION, mocks
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import build_graph
from app.purchase_agent.llm.runtime import get_llm_settings
from app.purchase_agent.nodes.classify_situation import (
    compute_ci_width,
    estimate_daily_demand,
)
from app.purchase_agent.state import PurchaseAgentState
from app.purchase_agent.tracing import ToolRecorder

AGENT_NAME = "purchase"

#: 표기와 무관하게 근거를 요구할 판정 필드 (M-1 §7.2 · 전달_2차 §1).
#: 봉투의 라벨 휴리스틱은 **대문자만** 보므로 재무의 ``MEDIUM``은 걸리지만 매입의
#: ``stable``·``["quantity","timing"]``은 빠진다. 선언하면 표기와 무관하게 걸린다.
#: **payload에 없는 이름을 적으면 ``E-JUDGMENT-UNKNOWN``이다** — 오타를 조용히 넘기면
#: 그 검사가 통째로 빈다.
JUDGMENT_FIELDS: tuple[str, ...] = ("situation", "allowed_axes")

def _run_id(request: AgentRequest) -> str:
    """``request_id``에서 **결정적으로** 만든다.

    난수·벽시계를 쓰지 않는다 (규칙 1) — 같은 요청을 다시 돌렸을 때 같은 ``run_id``가
    나와야 실행 이력이 대조된다. ``call_seq``를 넣는 이유는 재호출(최대 2회)이 서로 다른
    실행이기 때문이다.
    """
    return f"PUR-RUN-{request.context.request_id}-{request.call_seq}"


def _reply(
    request: AgentRequest,
    *,
    runtime_status: str,
    business_status: str,
    payload: Mapping[str, Any] | None = None,
    evidences: tuple[Evidence, ...] = (),
    reasoning: str = "",
    missing_data: tuple[str, ...] = (),
    judgment_fields: tuple[str, ...] = (),
) -> AgentReply:
    """봉투 4종(E-BIND)을 **한 곳에서** 채운다.

    ``request_id``·``as_of``·``agent``·``mode``를 호출부마다 적으면 한 경로만 어긋나도
    검증이 잡는데 원인은 흩어진다. 왕복 일치를 이 함수가 유일하게 책임진다.

    ``suggested_adjustments``는 **넘기지 않는다** — 매입은 축 조정을 제안할 권한이 없고
    (제안자 ≠ 조언자), 하나라도 담으면 봉투 생성 시점에 ``ContractViolation``이다.
    """
    return AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,
        mode=request.mode,
        run_id=_run_id(request),
        runtime_status=runtime_status,  # type: ignore[arg-type]
        business_status=business_status,  # type: ignore[arg-type]
        payload=dict(payload or {}),
        evidences=evidences,
        reasoning=reasoning,
        missing_data=missing_data,
        judgment_fields=judgment_fields,
    )


def _metadata(
    request: AgentRequest,
    recorder: ToolRecorder | None,
    *,
    tools: tuple[str, ...] = (),
    state: Mapping[str, Any] | None = None,
) -> ExecutionMetadata:
    """``run_id``는 회신과 **같아야 한다** (E-BIND-RUN-ID).

    **LLM 실행 상태를 여기 담는다** — ``provenance``가 아니라 ``ExecutionMetadata``다
    (전달_2차 §2 · 회신 §5-4). *"모델·fallback 상태"*는 업무 결과가 아니라 **실행 흔적**이라
    Business Reply와 섞지 않는다는 M-1 §6 원칙이다.

    담지 않으면 risks에는 *"판단자 응답 실패"*가 남는데 메타데이터는 ``DISABLED``·
    fallback ``false``로 나가 **두 값이 서로를 부정한다** (Codex 교차검증 P1).
    """
    used = recorder.used_tools if recorder is not None else tools
    mix = _mix_decision(state)
    return ExecutionMetadata(
        run_id=_run_id(request),
        request_id=request.context.request_id,
        agent=AGENT_NAME,
        used_tools=used,
        tool_order=tuple(range(1, len(used) + 1)),
        llm_status=mix.llm_status if mix is not None else _uncalled_status(),
        llm_model=(mix.llm_model or "") if mix is not None else "",
        llm_fallback_used=mix.llm_fallback_used if mix is not None else False,
    )


def _uncalled_status() -> LLMStatus:
    """판단자를 **한 번도 안 부른** 실행의 상태. 설정이 갈림길이다.

    🔴 전에는 무조건 ``DISABLED`` 였다. 그러면 *"LLM 을 안 켰네"* 와 *"켰는데 이번엔 안
      썼네"* 가 한 값이 되고, **사람이 없는 문제를 찾는다** — 2025-12-31 실행이 그랬다.
      등급이 미상이라 ⑤가 후보를 만들기 전에 막혔는데, 설정은 켜져 있었다.

    봉투가 네 값의 뜻을 규정한다 (``master/envelope.py`` ``LLMStatus``)::

        DISABLED           설정이 꺼져 있다
        SKIPPED_TEMPLATE   켜져 있는데 이번 실행에서는 안 불렀다 — 부를 조건이 아니었다

    **새로 정한 규칙이 아니다.** 마스터 ``IntentService``·Critic ``JudgeService``·우리
    ``MixSelectionService`` 가 이미 ``DISABLED → SKIPPED_TEMPLATE → SUCCESS → FALLBACK``
    순서를 쓴다. 서비스 **안**은 맞았는데, 서비스에 **닿기 전에** 막히는 경로만 이 함수를
    거치면서 뭉개지고 있었다.

    STATUS_QUERY 처럼 애초에 판단 단계가 없는 실행도 ``SKIPPED_TEMPLATE`` 이다 —
    Critic 이 *"이 Flow 에는 그 문장을 쓰는 단계가 없다"* 를 같은 값으로 적는 것과 같다.
    """
    return "SKIPPED_TEMPLATE" if get_llm_settings().enabled else "DISABLED"


def _mix_decision(state: Mapping[str, Any] | None) -> Any:
    """⑤의 LLM 판단. ⑤가 비율 목록 **첫 줄에 얹어** 보낸다 (``_sourcing_decision``과 같은 자리).

    ``None``인 경우가 둘이다 — ⑤가 아예 안 돈 경로(수신 검증 실패·STATUS_QUERY)와,
    돌았지만 게이팅에 걸려 LLM을 부르지 않은 날. **둘 다 실패가 아니고**, 어느 쪽이든
    "이번 실행에서 안 불렀다"는 같은 사실이라 ``_uncalled_status()`` 가 설정으로 가른다.
    """
    if not state:
        return None
    ratios = state.get("sourcing_plan") or []
    decision = ratios[0].get("decision", {}) if ratios else {}
    return decision.get("mix")


# ── 수신 검증 ─────────────────────────────────────────────────────────────


def validate_forecast(forecast: Any, as_of: date) -> list[str]:
    """받은 예측이 **as_of 이전에 만들어진 것인가**, **날짜 축이 맞는가**.

    마스터가 이미 한 겹 건다 — ``generated_at > as_of``면 아예 싣지 않는다. 그런데
    **시점 필드가 없으면 판단하지 않고 그대로 싣는다**(필요데이터 §1.3-②). 그 구멍이
    여기서 막힌다. 누수는 에러를 내지 않고 손익만 좋아지므로 양쪽에서 본다.

    ``daily`` 축을 보는 이유: 판정 기준일이 **D+14 = ``daily[13]``**이라(``ci_judgment_day``)
    축이 하루만 밀려도 **다른 날을 보게 된다.** 에러가 나지 않아 아무도 모른다.

    돌려주는 것은 ``missing_data``에 실을 이름 목록이다 — 비어 있으면 통과.
    """
    if not isinstance(forecast, Mapping):
        return ["forecast"]

    missing: list[str] = []
    generated_at = forecast.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        missing.append("forecast.generated_at")
    elif generated_at[:10] > as_of.isoformat():
        # 마스터가 걸렀어야 하는 값이 왔다 — 통과시키면 look-ahead가 성립한다.
        missing.append("forecast.generated_at")

    # 그래프가 실제로 꺼내는 최상위 키. 없으면 노드 안에서 KeyError가 난다.
    for key in ("current_price", "horizon_days", "model_version"):
        if forecast.get(key) is None:
            missing.append(f"forecast.{key}")

    daily = forecast.get("daily")
    horizon = forecast.get("horizon_days")
    if not isinstance(daily, list) or not daily:
        missing.append("forecast.daily")
        return sorted(set(missing))

    if isinstance(horizon, int) and len(daily) != horizon:
        missing.append("forecast.daily")
    # **축 전체를 본다.** 처음엔 ``daily[0]``만 D+1인지 봤는데, 첫 행만 맞춰 두고 이후를
    # 하루씩 밀면 그대로 통과했다 (Codex 교차검증 P1). 판정 기준일을 **배열 인덱스**로
    # 고르므로(``classify_situation.judgment_row``) 중간부터 밀리면 D+15를 D+14로
    # 착각한 채 조용히 돈다 — 에러가 나지 않는 look-ahead다.
    for index, row in enumerate(daily):
        if not isinstance(row, Mapping):
            missing.append("forecast.daily")
            break
        if row.get("date") != (as_of + timedelta(days=index + 1)).isoformat():
            missing.append("forecast.daily")
            break
        if any(row.get(key) is None for key in ("predicted", "lower", "upper")):
            missing.append("forecast.daily")
            break
    return sorted(set(missing))


#: 물류가 로트마다 싣는 키 (`master/adapters/logistics.py` · 2026-08-28 실측).
#: **이름을 바꾸지 않는다** — 물류가 alias 를 만들지 않기로 했고(#78 §6), 매입이
#: 물류 어휘를 그대로 읽기로 합의했다(#76). 매핑 표를 두면 어긋날 자리가 하나 더 생긴다.
LOT_REQUIRED_KEYS = ("lot_id", "available_qty_kg")

#: 로트가 어느 품목 것인지 밝히는 키. 없으면 품목을 가려낼 수 없다.
LOT_ITEM_KEY = "item"


def _lot_shape_problems(lots: Any) -> list[str]:
    """``lots``가 **있을 때** 모양을 본다. 없는 것과 모양이 다른 것은 다르다.

    ``lots``는 여전히 **선택 항목**이다 — 빠지면 등급 배분이 단일 등급으로 내려갈 뿐
    돌아간다(M-1 제출 §5). 그래서 부재는 잡지 않는다.

    ⚠️ **막으려는 것은 "있는데 모양이 다른" 경우다.** 그 구멍으로 실연동이
    ``KeyError: 'remaining_kg'`` 로 죽었는데(2026-08-28), 전 스위트는 green 이었다 —
    선택 항목이라 검사 자체가 없었고 노드 안에서야 터졌다. 어댑터에서 잡으면
    마스터가 ``missing_data`` 로 **무엇이 어긋났는지** 받는다 (#76).
    """
    if lots is None:
        return []
    if not isinstance(lots, list):
        return ["constraints.inventory.lots"]
    problems: list[str] = []
    for index, lot in enumerate(lots):
        if not isinstance(lot, Mapping):
            problems.append(f"constraints.inventory.lots[{index}]")
            continue
        # 값이 ``None``인 것은 통과시킨다 — 물류가 "모른다"를 그렇게 표현한다(§1.2-10).
        # 여기서 막는 것은 **키 자체가 없는** 경우다.
        problems.extend(
            f"constraints.inventory.lots[{index}].{key}"
            for key in LOT_REQUIRED_KEYS
            if key not in lot
        )
    return sorted(set(problems))


def validate_payload(payload: Mapping[str, Any], as_of: date) -> list[str]:
    """필수 4키와 그 하위 계약. 없는 것의 **이름**을 돌려준다.

    ``missing_data``가 비면 ``RUNTIME_NOT_READY``를 낼 수 없다(봉투가 ``ContractViolation``).
    무엇이 없는지 이름이 있어야 마스터가 사용자에게 요청할 수 있기 때문이다.

    ⚠️ **조언자가 ``READY``를 못 내면 키 자체가 없다** — 빈 dict가 아니다
    (필요데이터 §1.3-①). 그래서 ``in`` 으로 존재를 먼저 본다.
    """
    missing: list[str] = []
    if not payload.get("item"):
        missing.append("item")

    constraints = payload.get("constraints")
    if not isinstance(constraints, Mapping):
        missing.append("constraints")
    else:
        for dept in ("finance", "inventory"):
            if not isinstance(constraints.get(dept), Mapping):
                missing.append(f"constraints.{dept}")
        # **컨테이너 모양만 보면 안 된다.** ``constraints.finance``가 dict이기만 하면
        # 통과시켰더니, 안이 비어 있을 때 ``build_state``가 ``KeyError``로 죽었다.
        # 죽으면 *"무엇이 없는지"*가 ``missing_data``에 남지 않아 마스터가 사용자에게
        # 요청할 대상을 모른다 — 계약이 막으려는 상태가 그대로 된다.
        # **여기서 꺼내 쓰는 키만** 적는다. 목록이 실제 참조보다 길면 안 쓰는 값을
        # 요구하게 되고, 짧으면 다시 KeyError가 난다.
        finance = constraints.get("finance")
        if isinstance(finance, Mapping):
            # ⚠️ **``margin_defense_floor_rate``는 넣지 않는다.** 재무가 ``READY``인 채로
            # null을 줄 수 있고(Codex 교차검증 P1), 어느 노드도 그 값을 쓰지 않는다 —
            # 참조값으로 실려만 간다. 필수로 걸면 정상 요청이 어댑터에서 막힌다.
            #
            # ``finance_cap_amount_krw``는 **필수다.** 없으면 ``purchase_budget_krw``가
            # mock 폴백(60% 비율)을 타는데, 어댑터 경로에서 그 길로 가면 B6("같은 목적
            # 60% 재적용 금지")가 조용히 되살아난다. 실운영에서 재무 경계 미수신은
            # 애초에 ``RUNTIME_NOT_READY``이므로(M-1 제출 §4) 필수로 두는 것이 맞다.
            for key in ("base_projected_cash_min", "finance_cap_amount_krw"):
                if finance.get(key) is None:
                    missing.append(f"constraints.finance.{key}")

        # 물류도 같다. **필드명이 아직 미확정이라**(물류 미제출 — 필요데이터 §1.3-①)
        # 이름이 어긋난 payload가 실제로 올 수 있는데, 그때 ``warehouse_cap_kg``가
        # ``KeyError``로 죽으면 마스터는 *"물류 이름이 다르다"*를 알 길이 없다.
        # ``lots``는 빠져도 돌아간다 — 등급 배분이 단일 등급으로 내려갈 뿐이라
        # 필수가 아니다 (M-1 제출 §5).
        inventory = constraints.get("inventory")
        if isinstance(inventory, Mapping):
            missing.extend(_capacity_input_problems(inventory))
            missing.extend(_lot_shape_problems(inventory.get("lots")))
            missing.extend(_arrival_input_problems(inventory))

    if "forecast" not in payload:
        # 마스터가 오염 판정으로 싣지 않은 경우가 여기다 (필요데이터 §1.3-②).
        missing.append("forecast")
    else:
        missing.extend(validate_forecast(payload["forecast"], as_of))

    orders = payload.get("confirmed_orders")
    if not isinstance(orders, Mapping):
        missing.append("confirmed_orders")
    else:
        # ③이 ``total_kg``으로 일평균 수요를, ⑤가 ``orders[]``로 납품일 매칭을 한다.
        if orders.get("total_kg") is None:
            missing.append("confirmed_orders.total_kg")
        if not isinstance(orders.get("orders"), list):
            missing.append("confirmed_orders.orders")

    policy = payload.get("policy_values")
    if not isinstance(policy, Mapping):
        missing.append("policy_values")
    elif not policy.get("item_mix_ratio") or not isinstance(policy["item_mix_ratio"], Mapping):
        # 스칼라로 오면 mix 게이팅의 max()가 성립하지 않는다 (답변 §4-4).
        # **빈 dict도 거부한다.** 통과시키면 근거가 관측된 적 없는 최대비를 ``0.0``으로
        # 적고 "0.000 < 0.7 → mix 제외"라는 **스스로 모순된 문장**을 낸다 — 미결을 0으로
        # 채우지 않는다는 규칙 3 위반이다 (Codex 교차검증 P1).
        missing.append("policy_values.item_mix_ratio")
    # ⚠️ ``contract_price_krw``는 **필수가 아니다.** 미수령이면 ``None``이고, 그때
    # ``margin_warning``·``expected_margin_rate``가 함께 null로 나가는 것이 계약이다
    # (state.py · IO명세 §2 동기화 규칙). 필수로 걸면 정상 경로가 막힌다
    # (Codex 교차검증 P1).
    return sorted(set(missing))


def _capacity_input_problems(inventory: Mapping[str, Any]) -> list[str]:
    """창고 상한 입력의 **부재와 모양을 함께** 본다.

    부재만 보면 ``warehouse_cap_kg``가 값을 받고도 죽거나 조용히 틀린다. 실측:

        True          → 창고 상한 1kg. 전 안이 창고에 눌려 죽는데 사유가 안 남는다
        '1000' · [1]  → 더하는 자리에서 ``TypeError``. 노드가 죽으면 **사유를 못 낸다**
        -500          → 상한이 음수. 수량이 음수로 클립된다

    죽으면 마스터는 *"무엇을 다시 달라고 해야 하는지"*를 모른다 — ``RUNTIME_NOT_READY``에
    ``missing_data``가 있어야 요청이 성립한다. 로트 ``shelf_life_days``·
    ``inbound_lead_days``와 **같은 종류의 값이라 같은 자리에서 막는다**.

    ``0``은 통과시킨다. ``rental_cap_kg``는 2026-08-27 물류 회신 §1로 **0 확정**이라
    미결이 아니다 (규칙 3).
    """
    problems: list[str] = []
    for key in ("warehouse_free_kg", "rental_cap_kg"):
        base = f"constraints.inventory.{key}"
        value = inventory.get(key)
        if value is None:
            problems.append(base)
        elif isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            problems.append(f"{base}@수량이어야 한다")
        elif not isfinite(float(value)):  # NaN · ±Inf
            problems.append(f"{base}@유한한 수여야 한다")
        elif float(value) < 0:
            problems.append(f"{base}@음수일 수 없다 (받은 값 {value})")
    return problems


def _arrival_input_problems(inventory: Mapping[str, Any]) -> list[str]:
    """도착일 계산 입력의 **모양**을 본다. 부재는 잡지 않는다 — 둘 다 선택 필드다.

    ⚠️ ``inbound_lead_days``를 정수로 강제하는 이유: 2.5가 오면 도착일은
    ``date + timedelta(days=2.5)``에서 **2일로 잘리는데** ⑤의 소진 창 계산은 2.5를
    그대로 쓴다. 두 계산이 다른 리드타임을 보게 되고, 결과는 멀쩡해 보인다.
    조용히 반올림하지 않고 여기서 세운다 — 계약이 "일" 단위이기 때문이다.

    ``bool``을 따로 막는 것은 ``True``가 ``1일``로 통과하기 때문이다
    (``schemas.py``의 ``_reject_boolean``과 같은 이유).
    """
    problems: list[str] = []
    lead = inventory.get("inbound_lead_days")
    if lead is not None:
        base = "constraints.inventory.inbound_lead_days"
        if isinstance(lead, bool) or not isinstance(lead, int | float):
            problems.append(f"{base}@정수여야 한다")
        elif lead != int(lead):
            problems.append(f"{base}@일 단위 정수여야 한다 (받은 값 {lead})")
        elif lead < 0:
            problems.append(f"{base}@음수일 수 없다 (받은 값 {lead})")

    cap = inventory.get("cap_by_date")
    if cap is not None:
        base = "constraints.inventory.cap_by_date"
        if not isinstance(cap, Mapping):
            problems.append(f"{base}@날짜→수용량 매핑이어야 한다")
        else:
            for day, value in cap.items():
                if not isinstance(day, str):
                    # 날짜 객체로 오면 ISO 문자열 조회가 **전부 미스**가 되고,
                    # 값이 와 있는데도 "받지 못했다"로 고지된다.
                    problems.append(f"{base}@키가 ISO 날짜 문자열이어야 한다")
                    break
            for day, value in cap.items():
                if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
                    problems.append(f"{base}[{day}]@수량이어야 한다")
                    break
                if not isfinite(value):  # NaN · ±Inf
                    problems.append(f"{base}[{day}]@유한한 수여야 한다")
                    break
    return problems


# ── 재고 흡수 ─────────────────────────────────────────────────────────────

def absorb_inventory(inventory: Mapping[str, Any], item: str) -> dict[str, Any]:
    """물류 payload 의 재고를 **이 품목 것만** 남겨 넘긴다.

    ★ **품목 필터가 핵심이다.** 물류는 4품목 로트를 한 목록에 담아 보내는데
      (`LOT-…-BAECHU` · `-MU` · `-PIMANUL` · `-YANGPA`, 2026-08-28 실측),
      매입은 품목 하나씩 돈다. 거르지 않고 ``lots[0]`` 을 집으면 **다른 품목의
      로트를 근거로 삼는다** — 에러가 나지 않아 아무도 모른다. mock 은 품목별로
      나뉘어 있어 이 구멍이 보이지 않던 자리다.

    ★ ``item`` 키가 없는 로트는 **버리지 않고 남긴다.** 품목 축을 못 밝힌 것과
      "다른 품목"은 다르다 — 버리면 있는 재고를 없는 것으로 만든다. 판단은
      노드가 하고, 여기서는 가려낼 수 있는 것만 가려낸다.

    ★ **값을 만들지 않는다.** 없는 키를 기본값으로 채우면 미결이 사실이 된다
      (규칙 3). 모양 검사는 `validate_payload` 가 하고, 여기서는 옮기기만 한다.
    """
    out = dict(inventory)
    lots = inventory.get("lots")
    if not isinstance(lots, list):
        return out
    out["lots"] = [
        dict(lot)
        for lot in lots
        if isinstance(lot, Mapping) and lot.get(LOT_ITEM_KEY, item) == item
    ]
    return out


# ── payload → State ───────────────────────────────────────────────────────


def build_state(request: AgentRequest) -> PurchaseAgentState:
    """수신 payload를 그래프가 아는 State로 편다 (IO명세 §2-B).

    ``build_initial_state``와 **다른 경로**다 — 그쪽은 포트를 호출해 값을 당겨오고,
    이쪽은 이미 받은 값을 배치한다. 매입 자기 도메인(당일 시세·시장 문서)만 포트가
    그대로 담당하므로, 그 둘은 여기서도 ``ports``를 거친다.
    """
    from app.purchase_agent import ports

    payload = request.payload
    as_of = request.context.as_of
    finance: Mapping[str, Any] = payload["constraints"]["finance"]
    inventory: Mapping[str, Any] = payload["constraints"]["inventory"]
    policy: Mapping[str, Any] = payload["policy_values"]
    item = payload["item"]

    return {  # type: ignore[return-value]  # 중간·출력 필드는 노드가 채운다
        "date": as_of.isoformat(),
        "item": item,
        "forecast": dict(payload["forecast"]),
        # 자기 도메인 — 마스터를 거치지 않는다 (정의서 §4.1)
        "market_quotes": ports.get_market_quotes(item, as_of),
        "inventory": absorb_inventory(inventory, item),
        "confirmed_orders": dict(payload["confirmed_orders"]),
        "item_mix_ratio": dict(policy["item_mix_ratio"]),
        "contract_price": policy.get("contract_price_krw"),
        # 마진 방어선은 **재무 Policy 소유**라 policy_values가 아니라 재무 payload에 있다
        # (v2.3 M-19 해소 · 재무 회신 v2.2.1).
        # 참조값이라 없어도 돈다 — 어느 노드도 이 값을 쓰지 않는다.
        "margin_defense_floor_rate": finance.get("margin_defense_floor_rate"),
        "projected_cash_min": finance["base_projected_cash_min"],
        "finance_cap_amount_krw": finance.get("finance_cap_amount_krw"),
        "purchase_payment_days": finance.get("purchase_payment_days"),
        # N4는 **물류** payload에 있다 (재무가 아니다). ``absorb_inventory``가 통째로
        # 복사해 ``state["inventory"]`` 안에도 들어가지만, ``pending_value``는 State
        # 최상위를 보므로 여기서 한 번 더 올려야 값이 실제로 쓰인다.
        # ``or``를 쓰지 않는다 — 0은 "당일 도착"이라는 확정된 값이라 폴백 대상이 아니다 (규칙 3).
        "inbound_lead_days": inventory.get("inbound_lead_days"),
        "critical_payment_dates": list(finance.get("critical_payment_dates") or []),
        "feedback": dict(payload.get("prior_feedback") or {}) or None,
        "context_docs": [],
        "context_loop_count": 0,
        "rejected_reasons": [],
        "proposal": None,
    }


# ── 출력 ──────────────────────────────────────────────────────────────────


def build_reasoning(proposal: Mapping[str, Any]) -> str:
    """문장 3개 이하 · **3자리 이상 연속 숫자 금지** (M-1 §5.4).

    숫자를 **아예 넣지 않는 쪽**을 택했다. 봉투의 검사가 ``\\d[\\d,]{2,}``라 세 자리부터
    걸리는데, 안 개수·커버일수 같은 값은 한두 자리라 통과한다 — 그래도 서술문에 숫자를
    실으면 **출처를 붙일 수 없다.** 숫자는 payload와 Evidence가 싣는다.

    ``"2안"``·``"D+7"``은 통과하지만, 그 통과에 기대지 않고 라벨만 쓴다.
    """
    labels = [s["label"] for s in proposal.get("scenarios", [])]
    if not labels:
        return "제약 조합 하에 유효한 안이 없어 제안을 내지 못했다."
    head = "·".join(labels)
    # **열린 축을 실제 값에서 읽는다.** 처음엔 stable이면 무조건 "세 축을 모두 열었다"고
    # 썼는데, 배추는 편중 게이팅으로 mix가 닫혀 있어 두 축뿐이었다 — 서술문이 산출물과
    # 어긋났다 (Codex 교차검증 P2). 봉투의 숫자·문장 검사는 이 종류의 불일치를 못 잡는다.
    axes = "·".join(proposal.get("allowed_axes") or []) or "없음"
    tail = (
        "예측 구간이 넓어 공격안은 만들지 않았다."
        if proposal.get("situation") == "uncertain"
        else "예측 구간이 안정 범위다."
    )
    return f"{head} 안을 냈다. {tail} 열린 전략축은 {axes}이다."


def build_payload(state: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    """봉투에 실을 payload. **제안에 ``allowed_axes``를 얹는다.**

    🔴 ``PurchaseProposal``(우리 출력 스키마)에는 ``allowed_axes``가 없다. 그런데 마스터가
    ``judgment_fields``로 선언하라고 한 두 이름 중 하나가 그것이고, **선언한 이름이
    payload에 없으면 ``E-JUDGMENT-UNKNOWN``**이다 (전달_2차 §1). 검증 Tool이
    *"타이밍이 닫혔는데 분할이 있다"*를 잡으려면 그 값이 실려 나가야 한다 (답변 §4-3).

    **스키마를 고치지 않고 어댑터에서 얹는 이유**: ``PurchaseProposal``은 매입 내부의
    출력 계약이고 봉투 payload는 v2.2 계약이다. 둘을 잇는 것이 어댑터의 일이라, 여기서
    합치면 스키마가 안 바뀌고 949건이 그대로 돈다. 스키마에 넣는 편이 낫다고 팀이 정하면
    그때 옮긴다 (프로세스 기준 8/26 — 스키마에 닿으면 합의 후).
    """
    return {**proposal, "allowed_axes": list(state.get("allowed_axes") or [])}


def _relation(value: float, threshold: float, comparison: str) -> str:
    """근거 문장에 쓸 부등호. **판정에 쓴 연산과 같은 방향**을 돌려준다.

    ①이 ``ci_width_comparison``(설정값)으로 임계를 비교하므로 여기서도 그 문자열을 받아
    쓴다. 경계값에서 "0.080 > 0.08"처럼 **거짓인 문장**이 나오지 않게, 성립하지 않을 때는
    반대 방향을 적는다.
    """
    holds = value >= threshold if comparison == ">=" else value > threshold
    if holds:
        return "≥" if comparison == ">=" else ">"
    return "<" if comparison == ">=" else "≤"


def build_evidences(state: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[Evidence, ...]:
    """payload의 **숫자·판정·비어 있지 않은 배열**에 근거를 붙인다 (정의서 §1.2-5).

    봉투는 최상위 값 중 ``_needs_evidence``인 것 전부에 ``claim``이 같은 Evidence를
    요구하고, 반대로 payload에 없는 claim은 ``E-EVIDENCE-ORPHAN``으로 잡는다. 즉
    **양방향**이라 넘쳐도 모자라도 걸린다.

    ⚠️ **그 규칙을 여기서 재구현하지 않는다.** 어느 값이 근거를 요구하는지는 봉투가
    정하고, 우리는 아는 키에 대해 만든다. 규칙이 바뀌면 재구현이 조용히 어긋나므로,
    대신 **실제 ``validate_reply``를 돌려 findings가 0인지 보는 테스트**로 잠근다.

    ``allowed_axes``는 **게이트마다 한 건**이다 (신뢰도·총량·편중 셋). 축 목록 하나에
    근거가 여럿인 이유는 축을 여닫는 조건이 여럿이기 때문이고, 하나로 합치면 나머지
    게이트의 수치가 사라진다.
    """
    constraints = load_constraints()
    threshold = constraints["situation"]["ci_width_threshold"]
    judgment_day = constraints["situation"]["ci_judgment_day"]
    item = payload["meta"]["item"]
    as_of = payload["meta"]["as_of"]
    # ① 노드가 ``situation``·``allowed_axes``만 돌려주고 판정 **수치**는 남기지 않는다.
    # 여기서 같은 함수로 다시 구한다 — 값을 State에 얹으면 노드 계약이 바뀌고 949건이
    # 그 변화를 받는다. 같은 입력·같은 함수라 두 값이 갈릴 수 없다.
    ci_width = compute_ci_width(state["forecast"], judgment_day)
    axes = list(payload.get("allowed_axes") or [])
    # 편중 게이트가 보는 값 — **호출 품목이 아니라 전 품목의 최대비**다.
    # mix 축은 품목을 조합하는 전략이라 "어느 품목이든 편중됐나"를 묻는다
    # (``classify_situation.compute_allowed_axes``와 같은 계산).
    ratios = (state.get("item_mix_ratio") or {}).values()
    top_mix_ratio = max(ratios) if ratios else 0.0
    mix_threshold = constraints["concentration"]["item_threshold"]
    # **부등호를 하드코딩하지 않는다** (규칙 7). ①이 임계와 **비교 방향을 둘 다** 파일에서
    # 읽으므로(``ci_width_comparison``), 근거 문장이 방향을 따로 적으면 설정을 ``>``로
    # 바꾼 날 문장만 옛 방향으로 남는다.
    comparison = constraints["situation"]["ci_width_comparison"]
    # 총량 게이트가 보는 값 — ①과 **같은 계산**이다. 여기서 따로 세면 근거가 실제 판정과
    # 다른 수치를 주장하게 된다.
    estimated_total_kg = estimate_daily_demand(state["confirmed_orders"], constraints) * max(
        constraints["coverage_days"]["by_label"].values()
    )
    volume_threshold = constraints["triggers"]["split_entry_qty_kg"]

    def ref(kind: str) -> tuple[str, ...]:
        return (f"{item}-{kind}-{as_of}",)

    out = [
        Evidence(
            claim="situation",
            source="tool_calc",
            ref_ids=ref("CI"),
            value=round(ci_width, 6),
            unit="ratio",
            evidence_grade="SIM_FIXED",
            evidence_detail=(
                f"D+{judgment_day} 구간폭을 임계 {threshold}와 비교해 "
                f"{payload.get('situation')} 판정"
            ),
        ),
        # ── allowed_axes는 **게이트마다 한 건**이다 (현서님 회신 8/27) ──────────
        #
        # 처음엔 "열린 축 개수"(2.0)를 실었는데, 그건 **답의 길이를 세어 답이라고 적은
        # 것**이라 감사 가치가 없다. 나중에 *"왜 그날 timing이 열렸나"*를 보는 사람에게
        # 2.0은 아무것도 말하지 않는다. ``Evidence.value``의 용도는 **판정을 만든 근거
        # 수치**다.
        #
        # 축을 여닫는 게이트가 둘이라 근거도 둘이다. 하나로 합치면 한쪽 수치가 사라진다.
        Evidence(
            claim="allowed_axes",
            source="tool_calc",
            # **``situation``과 같은 ``ref_id``다.** §4.2.2가 "하나의 신뢰도 판정이
            # 개수·허용 축·분할 진입 셋을 동시에 결정한다"로 정했으므로 판정이 하나면
            # 근거도 하나다 — 추적하면 한 곳으로 모인다.
            ref_ids=ref("CI"),
            value=round(ci_width, 6),
            unit="ratio",
            evidence_grade="SIM_FIXED",
            # ⚠️ **"→ 허용 축 [...]"이라고 쓰지 않는다.** 구간폭이 정하는 것은 ``situation``
            # 이고, 축에 대해서는 **선매입 궤적 조건(by_trend)만** 연다·닫는다.
            # timing은 총량 게이트로도 열리므로(아래 VOL 근거), uncertain인데 timing이
            # 열린 날이 실재한다 — 그때 이 문장이 "uncertain → timing 열림"으로 읽히면
            # **없는 인과를 주장하게 된다** (Codex 교차검증 P1, 합성 입력으로 재현).
            evidence_detail=(
                f"구간폭 {ci_width:.3f} {_relation(ci_width, threshold, comparison)} {threshold}"
                f" → {payload.get('situation')} → 선매입 궤적 "
                f"{'차단' if payload.get('situation') == 'uncertain' else '허용'}"
            ),
        ),
        Evidence(
            claim="allowed_axes",
            source="tool_calc",
            # **세 번째 게이트다.** 현서님 회신이 *"축을 닫는 다른 게이트가 있다면 그
            # 게이트의 값을 쓰는 게 맞다 — 그런 게이트가 있습니까?"*라고 물었는데,
            # 있다: timing은 ``by_volume OR by_trend``로 열리고 **by_volume은 situation과
            # 무관하다**. 이 근거가 없으면 uncertain인데 timing이 열린 날을 설명할 수 없다.
            ref_ids=ref("VOL"),
            value=float(round(estimated_total_kg)),
            unit="kg",
            evidence_grade="SIM_FIXED",
            evidence_detail=(
                f"추정 총량 {round(estimated_total_kg):,}kg "
                f"{'≥' if estimated_total_kg >= volume_threshold else '<'} "
                f"임계 {volume_threshold:,}kg → 총량 트리거 "
                f"{'충족' if estimated_total_kg >= volume_threshold else '미달'}"
            ),
        ),
        Evidence(
            claim="allowed_axes",
            source="tool_calc",
            # 편중은 **다른 게이트**라 ref_id도 다르다. 신뢰도와 같은 id를 쓰면
            # 서로 다른 두 판정이 한 근거를 가리키게 된다.
            ref_ids=ref("MIX"),
            value=round(top_mix_ratio, 6),
            unit="ratio",
            evidence_grade="SIM_FIXED",
            # **열린 날에도 싣는다.** 닫힘만 기록하면 "왜 열렸나"의 근거가 없어지고,
            # 편중이 완화돼 mix가 부활한 날을 설명할 수 없다.
            # ⑤ 게이트 조건은 ``max(ratios) < threshold``면 개방이다. 부등호를 그 조건에서
            # 이끌어내 **경계값에서도 참인 문장**이 되게 한다 — 예전엔 0.70에서 "0.700 > 0.7"
            # 이라고 적었는데 그건 거짓이었다(판정은 맞고 문장만 틀린 상태).
            evidence_detail=(
                f"품목 편중 최대 {top_mix_ratio:.3f} "
                f"{'<' if top_mix_ratio < mix_threshold else '≥'} {mix_threshold} → "
                f"mix {'개방' if 'mix' in axes else '제외'}"
            ),
        ),
        Evidence(
            claim="scenarios",
            source="tool_calc",
            ref_ids=ref("SCEN"),
            value=float(len(payload.get("scenarios") or [])),
            unit="count",
            evidence_grade="SIM_FIXED",
            # 개수는 독립 파라미터가 아니라 **상황 판정의 파생값**이다 (변경요청 1).
            evidence_detail=(
                f"{payload.get('situation')} 판정에서 파생 — "
                "불확실이면 공격안을 만들지 않아 두 안이 된다"
            ),
        ),
        *_scenario_evidences(payload, ref),
    ]


    # 아래 둘은 **비어 있으면 근거를 요구받지 않는다** (빈 Sequence는 대상 밖).
    # 그런데도 붙이면 넘치는 쪽이라 ORPHAN은 아니지만 의미 없는 근거가 된다.
    if payload.get("context_docs_used"):
        out.append(
            Evidence(
                claim="context_docs_used",
                source="documents",
                ref_ids=tuple(payload["context_docs_used"]),
                value=float(len(payload["context_docs_used"])),
                unit="count",
                evidence_grade="SIM_FIXED",
                evidence_detail=(
                    "우선순위 목록을 소진할 때까지 읽었다 — 문서 선별·충분성 판단은 적용되지 않았다"
                ),
            )
        )
    if payload.get("rejected_reasons"):
        out.append(
            Evidence(
                claim="rejected_reasons",
                source="tool_calc",
                ref_ids=ref("CUT"),
                value=float(len(payload["rejected_reasons"])),
                unit="count",
                evidence_grade="SIM_FIXED",
                evidence_detail="자기 검증에서 컷된 안의 수 — 사유는 항목마다 실려 있다",
            )
        )
    return tuple(out)


#: 안 안쪽에서 **근거를 요구받는 숫자**와 그 값이 어디서 왔는지.
#: 봉투 v0.4가 배열을 **한 겹 파고들어** ``scenarios[i].<필드>`` 경로로 요구한다
#: (M-1 §7.1 — 매입 요청으로 신설된 규칙이다. 재무 payload는 평면이라 1:1이 성립하지만
#: 우리는 같은 이름의 필드가 안마다 2~3벌이라 위치가 필요하다).
#:
#: **라벨은 면제다** — ``label``·``strategy_type``까지 요구하면 안마다 근거를 만들어야
#: 해서 과하다. 숫자만 다르다: 어디서 왔는지 없으면 **LLM이 만든 값과 구분되지 않는다.**
_SCENARIO_NUMERIC_SOURCES: dict[str, str] = {
    "coverage_days": "constraints.coverage_days.by_label — 안별 커버일수 매핑",
    "total_qty_kg": "일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립",
    "total_amount_krw": "Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생",
    "max_price": "커버 구간 예측 상단(q90)의 최대값",
    "expected_margin_rate": "(contract_price − 가중 매입단가) ÷ contract_price",
}


def _scenario_evidences(payload: Mapping[str, Any], ref: Any) -> list[Evidence]:
    """안별 숫자 근거. **경로 표기**로 어느 안의 값인지 가리킨다.

    ``scenarios[0].total_amount_krw`` 형태다. 번호 대신 ``scenarios[공격]``처럼 이름으로도
    가리킬 수 있지만(봉투 ``canonical_claim``), **번호를 쓴다** — 라벨은 안 구성이 바뀌면
    사라질 수 있고 번호는 배열이 있는 한 항상 유효하다.

    ``None``인 값은 건너뛴다. ``expected_margin_rate``는 ``contract_price`` 미수령이면
    ``null``로 나가는데(IO명세 §2 동기화 규칙), 그때 봉투는 근거를 **요구하지 않는다.**

    ⚠️ **다만 봉투가 막아 주지는 않는다.** 처음엔 *"없는 값에 근거를 붙이면 고아 근거가
    된다"*고 적었는데 **틀렸다** — ``canonical_claim``은 값이 ``None``이어도 **필드가
    존재하면** 경로를 인정하므로, 여기서 ``0.0``을 지어내 붙여도 ``validate_reply``는
    깨끗하다 (Codex 교차검증 P2, 강제 삽입으로 재현). 즉 **미결을 0으로 채우지 않는 것은
    이 ``continue`` 한 줄이 유일한 방어**이고, 그래서 테스트로 따로 잠근다.
    """
    out: list[Evidence] = []
    for index, scenario in enumerate(payload.get("scenarios") or []):
        for field, origin in _SCENARIO_NUMERIC_SOURCES.items():
            value = scenario.get(field)
            if value is None:
                continue
            out.append(
                Evidence(
                    claim=f"scenarios[{index}].{field}",
                    source="tool_calc",
                    ref_ids=ref(f"SC{index}"),
                    value=float(value),
                    unit=_SCENARIO_UNITS[field],
                    evidence_grade="SIM_FIXED",
                    evidence_detail=f"{scenario.get('label')}안 — {origin}",
                )
            )
    return out


_SCENARIO_UNITS: dict[str, str] = {
    "coverage_days": "days",
    "total_qty_kg": "kg",
    "total_amount_krw": "KRW",
    "max_price": "KRW/kg",
    "expected_margin_rate": "ratio",
}


def purchase_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """마스터가 부르는 유일한 진입점.

    ``mode``는 둘뿐이다 — ``GENERATE_SCENARIOS``·``STATUS_QUERY``. 다른 mode는 봉투가
    **보내기 전에** 막으므로 여기서 다시 검사하지 않는다 (``_AGENT_MODES``).
    """
    if request.mode == "STATUS_QUERY":
        return _status_query(request)
    return _generate_scenarios(request)


def _status_query(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """살아 있는지와 **무엇을 받을 수 있는지**를 답한다. 시나리오를 만들지 않는다."""
    reply = _reply(
        request,
        runtime_status="READY",
        business_status="ok",
        # **중첩 Mapping 하나로 담는다.** 봉투는 최상위 값 중 비어 있지 않은 배열마다
        # Evidence를 요구하는데(``_needs_evidence``), 능력 목록에 근거를 붙이는 것은
        # 의미가 없다 — "이 mode를 받을 수 있다"는 계산 결과가 아니라 **정적 사실**이다.
        # 봉투 자신이 *"중첩 구조의 근거 규칙은 도메인이 정한다"*고 밝히므로, 규칙을
        # 우회하는 것이 아니라 그 설계를 그대로 쓰는 것이다.
        payload={
            "capabilities": {
                "agent_version": AGENT_VERSION,
                "supported_modes": ["GENERATE_SCENARIOS", "STATUS_QUERY"],
                "items": list(mocks.ITEMS),
            }
        },
        reasoning="매입 에이전트는 요청을 받을 수 있는 상태다.",
    )
    # ``used_tools``를 비운다. 봉투가 ``STATUS_QUERY``를 ``E-PLAN-EMPTY`` 예외로
    # 뺐으므로(``_PLAN_EXEMPT_MODES``) 가짜 Tool 이름을 넣을 이유가 사라졌다.
    # 검사를 피하려고 넣은 이름은 **M-16이 읽는 실행 계획을 그대로 오염시킨다.**
    return reply, _metadata(request, None)


def _generate_scenarios(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    as_of = request.context.as_of
    missing = validate_payload(request.payload, as_of)
    if missing:
        # 제약이 하나라도 빠진 시나리오는 만들지 않는다 (M-1 §11-6 · 제출 §4).
        # 재시도해도 같은 결과이므로 ERROR가 아니라 RUNTIME_NOT_READY다.
        #
        # 🔴 **payload를 싣지 않는다.** 전에는 ``{"scenarios": []}``였다.
        #   ``RUNTIME_NOT_READY``는 *"안 돌았다"*인데 그 dict는 **반쪽짜리 제안 형태**라
        #   ``PurchaseProposal``로 파싱하면 깨진다(``meta``·``no_proposal_reason`` 부재).
        #
        #   온전한 제안 형태로 채우는 것도 답이 아니다. *"돌았는데 안이 없다"*는
        #   **``READY`` + ``no_proposal_reason``**으로 이미 따로 있어서(12-31 피마늘이
        #   그 모양), payload를 같게 만들면 **두 상태가 payload만 봐서는 구분되지 않는다.**
        #   ``runtime_status``를 안 보는 소비자가 하나라도 생기면 "안 돌았다"가
        #   "돌았는데 안이 없다"로 읽힌다.
        #
        #   재무·물류도 이 자리에 payload를 안 싣는다(둘 다 ``_not_ready()``). 무엇이
        #   없는지는 ``missing_data``가, 왜인지는 ``reasoning``이 말한다 — 봉투는
        #   ``RUNTIME_NOT_READY``에 ``missing_data``가 비지 않을 것만 요구한다.
        reply = _reply(
            request,
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            reasoning="필수 입력이 없어 시나리오를 만들지 못했다.",
            missing_data=tuple(missing),
        )
        return reply, _metadata(request, None)

    recorder = ToolRecorder()
    state = build_state(request)
    final = build_graph(recorder=recorder).invoke(state)
    proposal = final["proposal"]

    payload = build_payload(final, proposal)
    reply = _reply(
        request,
        runtime_status="READY",
        # 안이 없는 것은 **사실**이지 오류가 아니다. E5 판정은 마스터가 한다.
        business_status="ok" if payload.get("scenarios") else "skipped",
        payload=payload,
        evidences=build_evidences(final, payload),
        reasoning=build_reasoning(payload),
        judgment_fields=JUDGMENT_FIELDS,
    )
    return reply, _metadata(request, recorder, state=final)
