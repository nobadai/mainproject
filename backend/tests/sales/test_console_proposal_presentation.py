"""판매안이 **사용자 앞에 어떤 자리로 서는가.**

이 파일이 지키는 경계는 하나다.

```text
UNRESOLVED  ≠  REJECTED
```

🔴 **탈락은 «다 봤는데 안 된다» 이고 미판정은 «아직 안 봤다» 다.** 둘을 같은 줄로
   보여주면 사용자는 재무 자료를 채워야 할 날에 판매 조건을 바꾸고, 같은 자리에서 또
   막힌다. 마스터가 `SL3_ALL_REJECTED` 와 `SL6_VALIDATION_UNRESOLVED` 를 따로 둔
   이유와 같다.

★ **후보를 보여주는 것과 확정으로 보내는 것은 다른 사실이다.** 미판정 후보도 화면에
  설 수 있지만 승인 경계는 닫혀 있어야 한다.
"""

from datetime import date

from app.sales.console_proposals import get_console_sales_proposals

RUN = "SIM-CHAIN-V13"
AS_OF = date(2026, 3, 10)
REQUEST = "REQ-DAILY-SALES-SIM-CHAIN-V13-20260310-무"


class _Reader:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []

    def __call__(self, query, params=None):
        if "source_order_id" in str(query):
            return []
        return [dict(row) for row in self.rows]


def _scenario(**over) -> dict:
    return {
        "scenario_id": "SALES-001-A",
        "scenario_type": "CONSERVATIVE",
        "item": "무",
        "partner_id": "KIMCHI_FACTORY_001",
        "quantity_kg": "463.0",
        "unit_price_krw": "1033.0",
        "reported_sales_amount_krw": "478279.0",
        "payment_days": 30,
        "delivery_date": "2026-03-11",
        "status": "UNRESOLVED",
        "rationale": [],
        "risks": [],
        "uncertainties": [],
        **over,
    }


def _payload(**over) -> dict:
    return {"recommended_scenario_id": None, "scenarios": [], **over}


def _row(**over) -> dict:
    base = {
        "request_id": REQUEST,
        "history_run_id": "RUN-1",
        "payload": _payload(),
        "scenario": _scenario(),
        "finance_verdict": None,
        "finance_status": None,
        "rule_results": [],
        "financial_summary": {},
    }
    base.update(over)
    return base


def _load(monkeypatch, rows):
    monkeypatch.setattr("app.sales.console_proposals.get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr("app.sales.console_proposals.fetch_all", _Reader(rows))
    return get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)


# ── 미판정은 탈락이 아니다 ────────────────────────────────────────────────


def test_a_candidate_without_a_finance_verdict_is_unresolved_not_rejected(monkeypatch):
    """🔴 아무도 탈락시키지 않았는데 탈락이라고 적지 않는다."""
    response = _load(monkeypatch, [_row()])

    row = response.rows[0]
    assert row.presentation_state == "UNRESOLVED"
    assert row.presentation_state != "REJECTED"
    assert response.state == "UNRESOLVED"
    assert response.rejected_count == 0
    assert response.unresolved_count == 1


def test_an_unresolved_candidate_is_still_shown_to_the_user(monkeypatch):
    """★ 판정이 없다고 후보를 숨기지 않는다 — 무엇이 기다리고 있는지 보여야 한다."""
    response = _load(monkeypatch, [_row()])

    assert len(response.rows) == 1
    assert response.rows[0].scenario_id == "SALES-001-A"
    assert response.rows[0].quantity_kg is not None


def test_an_unresolved_candidate_can_never_be_approved(monkeypatch):
    """🔴 보여주는 것과 확정으로 보내는 것은 다른 사실이다."""
    response = _load(monkeypatch, [_row()])

    assert response.rows[0].approval_blocked is True


def test_an_unresolved_candidate_names_what_is_missing(monkeypatch):
    """무엇을 채워야 하는지 말하지 않으면 사용자가 할 수 있는 일이 없다."""
    response = _load(
        monkeypatch,
        [_row(payload=_payload(missing_capabilities=["FINANCIAL_VALIDATION"]))],
    )

    assert response.rows[0].unresolved_reason_codes == ["FINANCIAL_VALIDATION"]


def test_an_unresolved_candidate_without_a_stated_cause_still_says_why(monkeypatch):
    """★ 판매가 아무것도 안 적었어도 «재무 판정 대기» 라고는 말할 수 있다."""
    response = _load(monkeypatch, [_row()])

    assert response.rows[0].unresolved_reason_codes == ["FINANCIAL_VALIDATION_PENDING"]


# ── 판정이 난 안 ──────────────────────────────────────────────────────────


def test_a_passing_candidate_is_presentable_and_approvable(monkeypatch):
    response = _load(
        monkeypatch,
        [_row(scenario=_scenario(status="EXECUTABLE"), finance_verdict="PASS")],
    )

    row = response.rows[0]
    assert row.presentation_state == "PRESENTABLE"
    assert row.approval_blocked is False
    assert row.unresolved_reason_codes == []
    assert response.state == "PRESENTABLE"
    assert response.presentable_count == 1


def test_a_failing_candidate_is_rejected_not_unresolved(monkeypatch):
    """판정이 났고 그 판정이 «안 된다» 다 — 조건을 바꿔야 한다."""
    response = _load(
        monkeypatch,
        [_row(scenario=_scenario(status="EXECUTABLE"), finance_verdict="FAIL")],
    )

    row = response.rows[0]
    assert row.presentation_state == "REJECTED"
    assert row.approval_blocked is True
    assert row.unresolved_reason_codes == []
    assert response.state == "REJECTED"
    assert response.rejected_count == 1


def test_a_review_required_candidate_is_its_own_state_and_still_blocked(monkeypatch):
    """«사람이 봐야 한다» 는 아직 승인이 아니다. 그렇다고 탈락도 아니다."""
    response = _load(
        monkeypatch,
        [
            _row(
                scenario=_scenario(status="EXECUTABLE"),
                finance_verdict="REVIEW_REQUIRED",
            )
        ],
    )

    row = response.rows[0]
    assert row.presentation_state == "REVIEW_REQUIRED"
    assert row.approval_blocked is True
    assert response.review_required_count == 1


def test_an_authoritative_fail_beats_what_sales_said_about_itself(monkeypatch):
    """★ 권위 있는 판정이 이긴다 — 판매가 스스로 실행 가능이라 적었어도 탈락이다."""
    response = _load(
        monkeypatch,
        [_row(scenario=_scenario(status="EXECUTABLE"), finance_verdict="FAIL")],
    )

    assert response.rows[0].presentation_state == "REJECTED"


def test_an_unknown_verdict_is_never_folded_into_pass(monkeypatch):
    """⚠️ 모르는 어휘를 통과로 접으면 재무가 막았을 거래가 승인 가능으로 보인다."""
    response = _load(
        monkeypatch,
        [_row(scenario=_scenario(status="EXECUTABLE"), finance_verdict="MAYBE")],
    )

    row = response.rows[0]
    assert row.presentation_state == "UNRESOLVED"
    assert row.approval_blocked is True
    assert row.unresolved_reason_codes == ["UNKNOWN_FINANCE_VERDICT_MAYBE"]


def test_a_candidate_sales_itself_called_infeasible_is_rejected(monkeypatch):
    response = _load(monkeypatch, [_row(scenario=_scenario(status="INFEASIBLE"))])

    assert response.rows[0].presentation_state == "REJECTED"


# ── 화면 전체 상태 ────────────────────────────────────────────────────────


def test_no_candidate_at_all_is_empty_not_unresolved(monkeypatch):
    """🔴 «안을 못 만들었다» 와 «판정이 안 났다» 는 다른 일이고 할 일도 다르다."""
    response = _load(monkeypatch, [_row(scenario=None)])

    assert response.rows == []
    assert response.state == "EMPTY"


def test_one_unresolved_among_rejected_keeps_the_whole_screen_unresolved(monkeypatch):
    """🔴 섞여 있을 때 «전부 탈락» 이라고 적으면 기다리는 판정이 있다는 사실이 사라진다."""
    response = _load(
        monkeypatch,
        [
            _row(scenario=_scenario(status="EXECUTABLE"), finance_verdict="FAIL"),
            _row(scenario=_scenario(scenario_id="SALES-001-B")),
        ],
    )

    assert response.state == "UNRESOLVED"
    assert response.rejected_count == 1
    assert response.unresolved_count == 1


def test_one_passing_candidate_makes_the_screen_presentable(monkeypatch):
    response = _load(
        monkeypatch,
        [
            _row(scenario=_scenario(status="EXECUTABLE"), finance_verdict="PASS"),
            _row(scenario=_scenario(scenario_id="SALES-001-B")),
        ],
    )

    assert response.state == "PRESENTABLE"
    assert response.presentable_count == 1
    assert response.unresolved_count == 1


# ── 전략 라벨 ─────────────────────────────────────────────────────────────


def test_the_stored_strategy_labels_reach_the_screen(monkeypatch):
    """🔴 이 칸이 없던 동안 «모델이 실패했다» 뒤에 우리 스키마 버그가 숨어 있었다."""
    response = _load(
        monkeypatch,
        [
            _row(
                payload=_payload(
                    strategy_source="TEMPLATE_FALLBACK",
                    strategy_llm_status="FALLBACK",
                    strategy_llm_failure_reason="HTTP_400",
                    strategy_clamped_reason_codes=["MARGIN_FLOOR"],
                    strategy_collapsed=True,
                    strategy_collapse_reason_codes=["MARGIN_FLOOR"],
                )
            )
        ],
    )

    strategy = response.rows[0].strategy
    assert strategy is not None
    assert strategy.source == "TEMPLATE_FALLBACK"
    assert strategy.llm_status == "FALLBACK"
    assert strategy.llm_failure_reason == "HTTP_400"
    assert strategy.clamped_reason_codes == ["MARGIN_FLOOR"]
    assert strategy.collapsed is True
    assert strategy.collapse_reason_codes == ["MARGIN_FLOOR"]


def test_a_run_without_strategy_labels_reports_nothing_rather_than_empty_labels(
    monkeypatch,
):
    """★ 없는 칸을 빈 값으로 만들면 «모델을 안 썼다» 가 «모델이 실패했다» 로 읽힌다."""
    response = _load(monkeypatch, [_row()])

    assert response.rows[0].strategy is None


def test_the_strategy_block_carries_only_stored_labels(monkeypatch):
    """🔴 HTTP 원문이나 provider 응답 본문이 화면까지 나가지 않는다."""
    from app.sales.console_proposals import ConsoleSalesStrategy

    assert set(ConsoleSalesStrategy.model_fields) == {
        "source",
        "llm_status",
        "llm_failure_reason",
        "clamped_reason_codes",
        "collapsed",
        "collapse_reason_codes",
    }
