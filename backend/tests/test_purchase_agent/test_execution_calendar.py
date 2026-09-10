"""⑦ 회차일 개장 검사 (`#300` · `#303`).

마스터가 `09-05` 부터 실행일 봉투를 싣고 있었는데 **매입이 한 곳도 안 읽었다** (2026-09-10
실측 · `purchase_agent` 전수 0곳). 그래서 「회차일이 주말에 떨어진다」는 증상이 이슈로만
남아 있었다.

★ **축이 「살 수 있는 날」이다.** `is_open` 이고 문 앞 게이트의 「예측이 있어 도는 날」과
  다르다 — 2026년 토요일 45일에 가락이 서므로, 문 앞 축으로 밀면 **살 수 있는 날에 못
  산다고 계획한다.**

🔴 **도착일은 여기서 안 본다.** 도착일 축은 `arrival_capacity` 가 `cap_by_date` 로 보고,
  물류는 주말 칸에도 여유를 준다 (실측: 주말 칸 3,050건 중 0 이 아닌 것 1,774건).

⚠️ **이 검사는 지금 한 번도 안 걸린다.** 회차가 155건 전부 하나뿐이고 그 하나가 `as_of`
  이며, `as_of` 는 문 앞 게이트가 실행일만 통과시킨다. 그래서 이 파일의 절반은 **분할이
  서는 날**(`#308`) 걸릴 것을 미리 시험하는 합성 입력이고, 나머지 절반은 **지금 안 걸리는
  이유를 구조로 잠근다** (`test_single_leg_is_always_as_of`).
"""

from datetime import date

from app.master.envelope import AgentRequest, ExecutionContext
from app.purchase_agent import ports
from app.purchase_agent.nodes.self_check import (
    MARKET_SKIP_REASONS,
    market_open_days,
)

AS_OF = date(2025, 12, 31)
ITEM = "배추"

#: 재무 상한을 올려 **개장 축만 남긴다.** 그러지 않으면 현금이 먼저 컷해 이 검사가 한 번도
#: 안 밟힌다 — `test_arrival_capacity` 가 같은 이유로 같은 장치를 쓴다.
HUGE_CASH = 10**12


def _scenario(*dates: str) -> dict:
    """단위 검사용 최소 안. 개장 검사는 ``split_plan[].date`` 만 본다."""
    return {
        "label": "기본",
        "risks": [],
        "split_plan": [
            {"seq": seq, "date": day, "qty_kg": 100, "expected_arrival_date": day}
            for seq, day in enumerate(dates, start=1)
        ],
    }


def _state(calendar: dict | None) -> dict:
    return {"date": AS_OF.isoformat(), "execution_calendar": calendar}  # type: ignore[return-value]


def _calendar(*closed: str, horizon_end: str = "2026-01-31") -> dict:
    return {"non_execution_days": list(closed), "horizon_end": horizon_end}


# ── 컷이 실제로 난다 ──────────────────────────────────────────────────────


def test_leg_on_closed_day_is_cut() -> None:
    """🔴 회차일이 장이 안 서는 날이면 **그 안은 죽는다.**"""
    scenario = _scenario("2025-12-31", "2026-01-04")
    verdict = market_open_days(scenario, _state(_calendar("2026-01-04")))  # type: ignore[arg-type]
    assert verdict.violation is not None
    assert "2026-01-04" in verdict.violation, verdict.violation
    assert verdict.skipped is None, "컷과 미검사를 같이 내지 않는다"


def test_open_days_pass_clean() -> None:
    """장이 서는 날만이면 컷도 고지도 없다."""
    scenario = _scenario("2025-12-31", "2026-01-05")
    verdict = market_open_days(scenario, _state(_calendar("2026-01-04")))  # type: ignore[arg-type]
    assert verdict == (None, None)


# ── 🔴 규칙 3 — ``None`` 과 ``[]`` 는 다른 사실이다 ────────────────────────


def test_no_envelope_is_skipped_not_passed() -> None:
    """🔴 달력이 없으면 **통과가 아니라 미검사**다.

    ⚠️ 이 검사가 잡는 변이는 ``state.get("execution_calendar")`` 를 ``or {}`` 로 접는
      것이다. 접으면 빈 목록이 되어 «안 서는 날이 없다» 로 조용히 지난다.
    """
    verdict = market_open_days(_scenario("2025-12-31"), _state(None))  # type: ignore[arg-type]
    assert verdict.violation is None, "달력이 없다고 안을 죽이지 않는다"
    assert verdict.skipped == MARKET_SKIP_REASONS["no_envelope"]


def test_empty_list_is_a_confirmed_answer() -> None:
    """🟢 빈 목록은 **«그 지평에 안 서는 날이 없다» 는 확정**이다 — 미검사가 아니다.

    ★ 이것이 ``None`` 과 갈리는 자리다. 둘을 같이 접으면 «확인했고 없었다» 와 «확인을
      못 했다» 가 한 상태가 되고, 그때부터 `risks` 가 거짓말을 한다.
    """
    verdict = market_open_days(_scenario("2025-12-31"), _state(_calendar()))  # type: ignore[arg-type]
    assert verdict == (None, None), "빈 목록을 «못 받았다» 로 읽지 않는다"


# ── 지평 밖은 «안 선다» 가 아니라 «모른다» ────────────────────────────────


def test_beyond_horizon_is_skipped_not_cut() -> None:
    """지평을 넘은 회차일은 컷하지 않고 **날짜를 이름으로** 고지한다."""
    verdict = market_open_days(
        _scenario("2025-12-31", "2026-02-09"), _state(_calendar(horizon_end="2026-01-31"))  # type: ignore[arg-type]
    )
    assert verdict.violation is None, "달력이 못 답하는 날을 «안 선다» 로 읽지 않는다"
    assert verdict.skipped is not None
    assert "2026-02-09" in verdict.skipped and "2026-01-31" in verdict.skipped


def test_closed_day_wins_over_horizon() -> None:
    """둘이 같이 걸리면 **컷이 먼저다** — 아는 위반을 모르는 것 때문에 미루지 않는다."""
    verdict = market_open_days(
        _scenario("2026-01-04", "2026-02-09"),
        _state(_calendar("2026-01-04", horizon_end="2026-01-31")),  # type: ignore[arg-type]
    )
    assert verdict.violation is not None and verdict.skipped is None


# ── 어댑터가 봉투에서 읽는다 ──────────────────────────────────────────────


def _payload(calendar: dict | None) -> dict:
    extras = ports.get_snapshot_extras(ITEM, AS_OF)
    payload: dict = {
        "item": ITEM,
        "constraints": {
            "finance": {
                "base_projected_cash_min": ports.get_projected_cash_min(AS_OF, 30),
                "margin_defense_floor_rate": 0.267,
                "finance_cap_amount_krw": HUGE_CASH,
                "critical_payment_dates": [],
            },
            "inventory": dict(ports.get_inventory(ITEM, AS_OF)),
        },
        "forecast": ports.get_forecast(ITEM, AS_OF),
        "confirmed_orders": ports.get_confirmed_orders(ITEM, AS_OF, days=14),
        "policy_values": {
            "contract_price_krw": extras["contract_price"],
            "item_mix_ratio": extras["item_mix_ratio"],
        },
    }
    if calendar is not None:
        payload["execution_calendar"] = calendar
    return payload


def _built(calendar: dict | None) -> dict:
    from app.purchase_agent.adapter import build_state

    request = AgentRequest(
        context=ExecutionContext("REQ-300", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=_payload(calendar),
    )
    return build_state(request)  # type: ignore[no-any-return]


def test_adapter_carries_the_envelope() -> None:
    """🟢 봉투에 실려 오면 State 로 그대로 온다 — `#300` 이 여기서 닫힌다."""
    calendar = _calendar("2026-01-04")
    assert _built(calendar)["execution_calendar"] == calendar


def test_adapter_leaves_none_when_absent() -> None:
    """🔴 안 오면 ``None`` 이다. 빈 dict 로 메우지 않는다 (규칙 3)."""
    assert _built(None).get("execution_calendar") is None


# ── ⚠️ 지금 안 걸리는 이유를 구조로 잠근다 ────────────────────────────────


def test_single_leg_is_always_as_of() -> None:
    """⚠️ **회차가 하나면 그 날짜는 `as_of` 다** — 그래서 이 검사가 지금 안 걸린다.

    실측(2026년 실행 전수): 회차 155건이 **전부 1회차뿐**이고 날짜가 전부 `as_of` 였다.
    `as_of` 는 마스터 문 앞 게이트가 실행일만 통과시키므로 비영업일일 수 없다.

    ★★ **이 검사가 깨지는 날이 곧 개장 검사가 의미를 갖는 날이다** — `#308`(분할 임계)이
      풀려 2·3회차가 `as_of + k` 로 가면 여기서 먼저 알려 준다. `check_split_dates` 가
      *"회차가 하나뿐이던 동안에는 순서를 어길 방법이 없었다"* 로 적어 둔 것과 같은 모양이다.
    """
    from app.purchase_agent.adapter import purchase_port

    request = AgentRequest(
        context=ExecutionContext("REQ-300B", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=_payload(None),
    )
    proposal = purchase_port(request)[0].payload
    singles = [
        s for s in proposal.get("scenarios", []) if len(s.get("split_plan") or []) == 1
    ]
    assert singles, "일괄 안이 하나도 없다 — 이 잠금의 전제가 사라졌다"
    for scenario in singles:
        assert scenario["split_plan"][0]["date"] == AS_OF.isoformat()


def test_violation_actually_kills_the_plan() -> None:
    """🔴 **체인에 걸려 있는가** — 컷 사유가 실제로 안을 죽이는지 관통으로 본다.

    ⚠️ 이 검사는 변이 시험으로 **필요해서 생겼다.** 단위 함수만 시험했더니 `self_check`
      체인에서 ``market.violation`` 을 통째로 빼도 10건이 다 통과했다 — 검사는 있는데
      **아무것도 안 죽이는** 상태를 못 잡았다.

    ★★ `#93` 에서 겪은 것과 같은 모양이다: *"컷을 분할 안에만 넣으면 회차를 안 나눌수록
      검사를 안 받는 구조가 된다."* 있다는 것과 **물린다는 것**은 다른 사실이다.

    ★ 여기서 `as_of` 를 닫는 것은 인위적이 아니다. 두 축이 다르므로 **예측은 있는데 장이
      안 서는 날**이 실재한다 — 문 앞 게이트는 통과시키고 봉투 달력은 «안 선다» 를 말한다.
      그날은 살 수 없으니 컷이 맞다.
    """
    from app.purchase_agent.adapter import purchase_port

    request = AgentRequest(
        context=ExecutionContext("REQ-300D", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=_payload(_calendar(AS_OF.isoformat())),
    )
    proposal = purchase_port(request)[0].payload
    assert proposal.get("scenarios") == [], "장이 안 서는 날인데 살아남은 안이 있다"

    reasons = [r["reason"] for r in proposal["rejected_reasons"]]
    assert reasons, "컷됐는데 사유가 없다"
    assert any("장이 안 서는 날" in reason for reason in reasons), reasons


def test_skip_notice_reaches_risks() -> None:
    """🔴 미검사 고지가 **안의 `risks` 까지** 간다 — ⑦ 안에서만 알고 끝나지 않는다."""
    from app.purchase_agent.adapter import purchase_port

    request = AgentRequest(
        context=ExecutionContext("REQ-300C", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=_payload(None),
    )
    proposal = purchase_port(request)[0].payload
    scenarios = proposal.get("scenarios") or []
    assert scenarios, "안이 0개라 고지가 실릴 자리가 없다"
    for scenario in scenarios:
        assert MARKET_SKIP_REASONS["no_envelope"] in scenario["risks"], scenario["label"]
