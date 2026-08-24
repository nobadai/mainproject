"""⑥ package_scenarios — 보수/기본/공격 묶기 + 근거 작성 (상세설계 §4-⑥).

Epic 2는 rule_only 경로다. LLM 몫은 **문장을 다듬는 것**이고 숫자는 계산이 소유한다
(규칙 6). Epic 3에서 붙일 때도 이 함수를 다시 쓰는 게 아니라, 아래가 만든 결과를
입력으로 받아 rationale·risks의 서술만 손본다.
"""

from datetime import date
from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes._guards import require_positive
from app.purchase_agent.nodes.classify_situation import compute_ci_width, compute_rise_rate_2w
from app.purchase_agent.state import PurchaseAgentState


def assign_axes(labels: list[str], allowed_axes: list[str], aggressive_axis: str) -> dict[str, str]:
    """안별 ``strategy_type``을 허용 축 안에서 고른다 (정의서 §3.5.1-2).

    축이 하나뿐인 날은 전 안이 같은 축을 쓴다 — 그게 정상이고, ⑦의 중복 검사도 그날은
    면제한다. 축이 여럿이면 겹치지 않게 배분해 "3안인데 사실 한 안"을 피한다.
    """
    if len(allowed_axes) == 1:
        return dict.fromkeys(labels, allowed_axes[0])
    axes = dict.fromkeys(labels, "quantity")
    if aggressive_axis in allowed_axes and "공격" in labels:
        axes["공격"] = aggressive_axis
    else:
        axes[labels[-1]] = next(axis for axis in allowed_axes if axis != "quantity")
    return axes


def materialize_split(as_of: str, total_qty_kg: int, chosen: list[dict] | None) -> list[dict]:
    """회차별 입고 계획. ④가 유형만 정하므로 안별 총량을 여기서 나눈다.

    ``None``(일괄)이면 단일 회차다. seq 1의 date는 ``as_of``여야 한다 (IO명세 §2).
    """
    if not chosen:
        return [{"seq": 1, "date": as_of, "qty_kg": total_qty_kg}]
    _validate_ratios(chosen, "split_plan")
    remaining = total_qty_kg
    rounds = []
    for index, part in enumerate(chosen, start=1):
        # 마지막 회차가 잔량을 흡수한다 — 반올림이 총량을 흔들면 사중 일치가 깨진다.
        qty = remaining if index == len(chosen) else round(total_qty_kg * part["ratio"])
        rounds.append({"seq": index, "date": part["date"], "qty_kg": qty})
        remaining -= qty
    return rounds


def _validate_ratios(ratios: list[dict], name: str) -> list[dict]:
    """비율이 양수이고 합이 1인지 확인한다.

    검증하지 않으면 작은 비율은 ``round()``로 0이 되고, 합이 1을 넘으면 마지막 항목이
    **음수 수량**이 된다. 마지막이 잔량을 흡수하므로 사중 일치는 통과하고, 스키마 검증에서야
    제안 전체가 터진다 — 조용히 지나가는 구간을 만들지 않는다.
    """
    if not ratios:
        raise ValueError(f"{name} is empty")
    for line in ratios:
        require_positive(line["ratio"], f"{name}.ratio")
    total = sum(line["ratio"] for line in ratios)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"{name} ratios must sum to 1, got {total}")
    return ratios


def materialize_sourcing(total_qty_kg: int, ratios: list[dict]) -> list[dict]:
    """등급별 절대 수량. ⑤가 비율만 정하므로 안별 총량을 여기서 곱한다.

    마지막 줄이 잔량을 흡수한다 — ``Σ sourcing.qty_kg == total_qty_kg``가 사중 일치의
    한 축이라 반올림 오차를 남기면 ⑦이 컷한다.
    """
    _validate_ratios(ratios, "sourcing_plan")
    remaining = total_qty_kg
    lines = []
    for index, line in enumerate(ratios, start=1):
        qty = remaining if index == len(ratios) else round(total_qty_kg * line["ratio"])
        lines.append(
            {
                "market": line["market"],
                "grade": line["grade"],
                "qty_kg": qty,
                "grade_unit_price": line["grade_unit_price"],
            }
        )
        remaining -= qty
    return lines


def compute_max_price(forecast: dict, coverage_days: int) -> int:
    """max_price = 커버 구간 안 예측 상단의 최대값 (규칙 5 · §4-⑦ "예측 q90 기반").

    **마진 방어선과 무관하다** (규칙 5). 혼동하기 쉬운 두 값을 구분해둔다:

    - ``max_price`` 초과 → ⑦이 **컷한다** (예측 q90 기반 하드 상한)
    - ``contract_price`` 초과 → 컷이 아니라 ``margin_warning=true`` 표시만

    커버 구간으로 자르는 이유: 그 안에서만 실제로 사기 때문이다.
    """
    return max(row["upper"] for row in forecast["daily"][:coverage_days])


def _weighted_unit_price(sourcing: list[dict], total_qty_kg: int) -> float:
    amount = sum(line["qty_kg"] * line["grade_unit_price"] for line in sourcing)
    return amount / require_positive(total_qty_kg, "total_qty_kg")


def _inventory_claim(lots: list[dict] | None) -> tuple[str, str, str]:
    """재고 근거 문장 · ref_id · 등급. **NULL과 확정 0을 구분한다** (규칙 3).

    ``lots``가 ``None``이면 재고를 아직 못 받은 것이고, 빈 목록이면 "가용 재고 없음"이
    확정된 것이다. 둘을 똑같이 "가용 0kg"으로 적으면 미결이 사실처럼 나간다.
    """
    if lots is None:
        return "재고 정보 미수신 — 가용량 미결", "INV-미수신", "ASSUMED"
    if not lots:
        return "가용 재고 없음 (확정)", "INV-없음", "SIM_FIXED"
    lot = lots[0]
    return (
        f"가용 {lot['remaining_kg']:,}kg (로트 {lot['lot_id']})",
        f"INV-{lot['lot_id']}",
        "SIM_FIXED",
    )


def _rationale(state: PurchaseAgentState, draft: dict, constraints: dict) -> list[dict]:
    """근거. **모든 항목에 ref_id가 필수**다 (규칙 4) — 실제 데이터에서 뽑는다.

    ``evidence_grade``는 정직하게 붙인다. mock은 팀이 시뮬 조건으로 선언한 값이라
    ``SIM_FIXED``이고, 확정주문에서 파생한 일평균 수요는 IO명세 §5대로 ``ASSUMED``다
    ("수요에서 파생된 것은 SIM_FIXED 자격을 잃는다" — 제약 독립성 요건).
    """
    as_of = state["date"]
    forecast = state["forecast"]
    day = constraints["situation"]["ci_judgment_day"]
    claim, ref_id, grade = _inventory_claim(state["inventory"].get("lots"))
    return [
        {
            "source": "예측",
            "claim": (
                f"D+{day} 예측 {compute_rise_rate_2w(forecast, day):+.1%}, "
                f"신뢰구간 폭 {compute_ci_width(forecast, day):.1%}"
            ),
            "ref_id": f"FC-{forecast['model_version']}-{as_of}",
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": (
                f"{forecast['model_version']} 경락가 예측 (지평 {forecast['horizon_days']}일)"
            ),
        },
        {
            "source": "시세관측",
            "claim": (
                f"가락 당일 경락가 {state['market_quotes'][0]['price']:,}원/kg 등 "
                f"{len(state['market_quotes'])}개 등급"
            ),
            "ref_id": f"MQ-가락-{as_of}",
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": "가락시장 등급별 당일 실측 (mock)",
        },
        {
            "source": "주문",
            "claim": (
                f"확정주문 {state['confirmed_orders']['total_kg']:,}kg "
                f"→ 일평균 {draft['daily_demand_kg']:,.0f}kg × D={draft['coverage_days']}"
            ),
            "ref_id": f"SO-{as_of}",
            "evidence_grade": "ASSUMED",
            "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음",
        },
        {
            "source": "재고",
            "claim": claim,
            "ref_id": ref_id,
            "evidence_grade": grade,
            "evidence_detail": "inventory_lots 스냅샷 (mock)",
        },
        {
            "source": "현금",
            "claim": (
                f"향후 최저 현금 {state['projected_cash_min']:,}원의 "
                f"{constraints['cash']['max_purchase_ratio']:.0%}까지 매입 가능"
            ),
            "ref_id": f"CASH-{as_of}",
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": "base_projected_cash_min (정산 산출, mock)",
        },
    ]


def _risks(draft: dict, deferred: list[str], lots: list[dict] | None, as_of: str) -> list[str]:
    """위험·유의사항. **미결값 때문에 건너뛴 검사도 여기 싣는다.**

    ``rejected_reasons``가 아니라 risks인 이유: 소비자는 rejected_reasons를 "컷된 안의
    이력"으로 읽는다. "검사를 하지 않았다"는 다른 의미라 그 필드에 섞으면 계약이 오염된다.
    """
    risks = list(deferred)
    for clip in draft["clipped_by"]:
        risks.append(
            f"{clip['constraint']} 제약으로 원안 {clip['raw_qty_kg']:,}kg에서 "
            f"{clip['cap_kg']:,}kg으로 축소"
        )
    lot = lots[0] if lots else {}
    if lot.get("shelf_life_days") is not None and lot.get("stocked_at"):
        # 잔여신선도 = shelf_life − (as_of − stocked_at). as_of는 주입된 값이라
        # 벽시계를 읽지 않는다 (규칙 1).
        elapsed = (date.fromisoformat(as_of) - date.fromisoformat(lot["stocked_at"])).days
        risks.append(
            f"기존 로트 {lot['lot_id']} 잔여신선도 {lot['shelf_life_days'] - elapsed}일 — "
            "신규 매입분이 이 로트를 밀어내지 않는지 확인 필요"
        )
    return risks


def package_scenarios(state: PurchaseAgentState) -> dict[str, Any]:
    """안별로 split·sourcing을 묶고 근거를 붙여 시나리오를 완성한다."""
    constraints = load_constraints()
    base = state["base_plan"]
    drafts = base["drafts"]
    axes = assign_axes(
        [d["label"] for d in drafts],
        state["allowed_axes"],
        constraints["allocation"]["aggressive_axis"],
    )
    lots = state["inventory"].get("lots")
    # 계약단가는 마진 표시의 분모다. 없거나 0이면 마진을 지어내지 않고 멈춘다 (규칙 3).
    contract_price = require_positive(state["contract_price"], "contract_price")

    scenarios = []
    dropped = []
    for draft in drafts:
        total = draft["total_qty_kg"]
        if total <= 0:
            # 하드 제약이 전량을 깎아낸 안. 스키마가 total_qty_kg > 0을 요구하므로 제안이
            # 될 수 없다. 조용히 사라지지 않게 사유를 남긴다 — 안이 왜 줄었는지가 소비자에게
            # 보여야 한다.
            binding = ", ".join(clip["constraint"] for clip in draft["clipped_by"]) or "미상"
            dropped.append(
                {
                    "label": draft["label"],
                    "reason": f"하드 제약({binding})으로 수량이 0까지 축소되어 제안 불가",
                }
            )
            continue
        sourcing = materialize_sourcing(total, state["sourcing_plan"])
        rationale_input = {**draft, "daily_demand_kg": base["daily_demand_kg"]}
        unit_price = _weighted_unit_price(sourcing, total)
        scenarios.append(
            {
                "label": draft["label"],
                "strategy_type": axes[draft["label"]],
                "coverage_days": draft["coverage_days"],
                "total_qty_kg": total,
                "total_amount_krw": sum(
                    line["qty_kg"] * line["grade_unit_price"] for line in sourcing
                ),
                "max_price": compute_max_price(state["forecast"], draft["coverage_days"]),
                # 규칙 5 — 계약단가 초과는 컷이 아니라 표시다.
                "margin_warning": unit_price > contract_price,
                "split_plan": materialize_split(state["date"], total, state["split_plan"]),
                "sourcing_plan": sourcing,
                "expected_margin_rate": max(0.0, (contract_price - unit_price) / contract_price),
                "rationale": _rationale(state, rationale_input, constraints),
                "risks": _risks(draft, base["deferred_checks"], lots, state["date"]),
            }
        )

    return {
        "scenarios_final": scenarios,
        "rejected_reasons": [*state["rejected_reasons"], *dropped],
        "confidence": constraints["situation"]["confidence_by_situation"][state["situation"]],
    }
