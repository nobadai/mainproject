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
from typing import Any

from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionMetadata,
)
from app.orchestrator.contracts_core import Evidence
from app.purchase_agent import AGENT_VERSION, mocks
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import build_graph
from app.purchase_agent.nodes.classify_situation import compute_ci_width
from app.purchase_agent.state import PurchaseAgentState
from app.purchase_agent.tracing import ToolRecorder

AGENT_NAME = "purchase"

#: ``STATUS_QUERY``가 담는 Tool 이름.
#: ⚠️ **임시값이다.** 봉투 검증은 ``runtime_status == "READY"``면 ``used_tools``가 비는 것을
#: ``E-PLAN-EMPTY``로 막는데(``envelope.py``), STATUS_QUERY는 업무 Tool을 하나도 쓰지 않는다.
#: 이 이름은 M-1 §10에 제출한 **6종 Registry에 없다** — 마스터 쪽에 STATUS_QUERY를
#: ``E-PLAN-EMPTY`` 예외로 두는 것을 제안 중이고, 정해지면 이 상수가 사라지거나
#: Registry에 정식 등재된다.
_STATUS_QUERY_TOOL = "status_query"


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
        llm_status=mix.llm_status if mix is not None else "DISABLED",
        llm_model=(mix.llm_model or "") if mix is not None else "",
        llm_fallback_used=mix.llm_fallback_used if mix is not None else False,
    )


def _mix_decision(state: Mapping[str, Any] | None) -> Any:
    """⑤의 LLM 판단. ⑤가 비율 목록 **첫 줄에 얹어** 보낸다 (``_sourcing_decision``과 같은 자리).

    ``None``인 경우가 둘이다 — ⑤가 아예 안 돈 경로(수신 검증 실패·STATUS_QUERY)와,
    돌았지만 게이팅에 걸려 LLM을 부르지 않은 날. 둘 다 ``DISABLED``가 맞다:
    전자는 실행이 없었고 후자는 **판단자를 쓰지 않기로 규칙이 정한 것**이라 실패가 아니다.
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
            for key in ("warehouse_free_kg", "rental_cap_kg"):
                if inventory.get(key) is None:
                    missing.append(f"constraints.inventory.{key}")

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
    elif not isinstance(policy.get("item_mix_ratio"), Mapping):
        # 스칼라로 오면 mix 게이팅의 max()가 성립하지 않는다 (답변 §4-4).
        missing.append("policy_values.item_mix_ratio")
    # ⚠️ ``contract_price_krw``는 **필수가 아니다.** 미수령이면 ``None``이고, 그때
    # ``margin_warning``·``expected_margin_rate``가 함께 null로 나가는 것이 계약이다
    # (state.py · IO명세 §2 동기화 규칙). 필수로 걸면 정상 경로가 막힌다
    # (Codex 교차검증 P1).
    return sorted(set(missing))


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
        "inventory": dict(inventory),
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


def build_evidences(state: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[Evidence, ...]:
    """payload의 **숫자·판정·비어 있지 않은 배열**에 근거를 붙인다 (정의서 §1.2-5).

    봉투는 최상위 값 중 ``_needs_evidence``인 것 전부에 ``claim``이 같은 Evidence를
    요구하고, 반대로 payload에 없는 claim은 ``E-EVIDENCE-ORPHAN``으로 잡는다. 즉
    **양방향**이라 넘쳐도 모자라도 걸린다.

    ⚠️ **그 규칙을 여기서 재구현하지 않는다.** 어느 값이 근거를 요구하는지는 봉투가
    정하고, 우리는 아는 키에 대해 만든다. 규칙이 바뀌면 재구현이 조용히 어긋나므로,
    대신 **실제 ``validate_reply``를 돌려 findings가 0인지 보는 테스트**로 잠근다.

    ⚠️ ``allowed_axes``의 ``value``는 **열린 축 개수**다. ``Evidence.value``가 ``float``
    필수인데 축 목록에 대응하는 수치가 없어 택한 값이고 **현서님 확인 항목**이다.
    실제 근거는 ``evidence_detail``이 문장으로 싣는다.
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
        Evidence(
            claim="allowed_axes",
            source="tool_calc",
            ref_ids=ref("AXES"),
            value=float(len(axes)),
            unit="count",
            evidence_grade="SIM_FIXED",
            evidence_detail=f"허용 축 {axes} — 축별 개폐는 상황 판정과 편중·트리거가 정한다",
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
    return reply, _metadata(request, None, tools=(_STATUS_QUERY_TOOL,))


def _generate_scenarios(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    as_of = request.context.as_of
    missing = validate_payload(request.payload, as_of)
    if missing:
        # 제약이 하나라도 빠진 시나리오는 만들지 않는다 (M-1 §11-6 · 제출 §4).
        # 재시도해도 같은 결과이므로 ERROR가 아니라 RUNTIME_NOT_READY다.
        reply = _reply(
            request,
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            payload={"scenarios": []},
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
    )
    return reply, _metadata(request, recorder, state=final)
