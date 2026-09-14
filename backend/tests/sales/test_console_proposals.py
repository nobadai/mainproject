"""금일 판매안 — 저장된 안을 펴서 보여 주되, 판정은 재무 것을 읽는다."""

from datetime import date
from decimal import Decimal

from app.sales.console_proposals import get_console_sales_proposals

RUN = "SIM-CHAIN-V13"
AS_OF = date(2026, 3, 10)
REQUEST = "REQ-DAILY-SALES-SIM-CHAIN-V13-20260310-무"


class _Reader:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[str, list]] = []

    def __call__(self, query, params=None):
        self.calls.append((str(query), list(params or [])))
        return [dict(row) for row in self.rows]


def _scenario(**over) -> dict:
    return {
        "scenario_id": "SALES-001-A",
        "scenario_type": "CONSERVATIVE",
        "objective": "RISK_DEFENSE",
        "item": "무",
        "partner_id": "KIMCHI_FACTORY_001",
        "quantity_kg": "463.0",
        "unit_price_krw": "1033.0",
        "reported_sales_amount_krw": "478279.0",
        "payment_days": 30,
        "delivery_date": "2026-03-11",
        "status": "UNRESOLVED",
        "rationale": ["전달된 계약만 사용해 구성했습니다."],
        "risks": [],
        "uncertainties": [],
        **over,
    }


def _row(**over) -> dict:
    return {
        "request_id": REQUEST,
        "payload": {"recommended_scenario_id": None, "scenarios": []},
        "scenario": _scenario(),
        "finance_verdict": "PASS",
        "finance_status": "EVALUATED",
        "rule_results": [
            {
                "rule_id": "FIN-SALES-MARGIN",
                "verdict": "PASS",
                "reason_codes": ["SALES_MARGIN_MEETS_WARNING"],
            },
        ],
        "financial_summary": {"contribution_margin_rate": "0.3349"},
        **over,
    }


def _patch(monkeypatch, reader: _Reader) -> None:
    monkeypatch.setattr("app.sales.console_proposals.get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr("app.sales.console_proposals.fetch_all", reader)


def test_a_stored_proposal_comes_back_as_stored(monkeypatch):
    _patch(monkeypatch, _Reader([_row()]))

    result = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    assert result.sim_run_id == RUN
    assert result.as_of == AS_OF
    assert result.request_count == 1
    row = result.rows[0]
    assert row.item == "무"
    assert row.scenario_type == "CONSERVATIVE"
    assert row.quantity_kg == Decimal("463.0")
    assert row.unit_price_krw == Decimal("1033.0")
    assert row.reported_sales_amount_krw == Decimal("478279.0")
    assert row.payment_days == 30
    assert row.delivery_date == date(2026, 3, 11)
    assert row.rationale == ["전달된 계약만 사용해 구성했습니다."]


def test_the_amount_is_read_not_recomputed(monkeypatch):
    """🔴 수량×단가로 다시 만들지 않는다.

    저장된 매출액과 곱셈이 어긋나면 그것은 화면이 고칠 일이 아니라 드러날 일이다.
    """
    _patch(monkeypatch, _Reader([_row(scenario=_scenario(reported_sales_amount_krw="1"))]))

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.reported_sales_amount_krw == Decimal(1)
    assert row.quantity_kg == Decimal("463.0")


def test_the_finance_verdict_is_read_rather_than_judged(monkeypatch):
    """🔴 판매가 마진이나 여신으로 판정을 흉내 내지 않는다."""
    _patch(monkeypatch, _Reader([_row(finance_verdict="FAIL")]))

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.finance_verdict == "FAIL"
    assert row.finance_status == "EVALUATED"


def test_a_missing_finance_verdict_stays_missing(monkeypatch):
    """⚠️ 재무가 아직 안 봤으면 «없음» 이다. 통과도 거절도 아니다."""
    _patch(monkeypatch, _Reader([_row(finance_verdict=None, finance_status=None)]))

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.finance_verdict is None
    assert row.finance_status is None


def test_a_request_that_made_no_proposal_still_counts_as_a_request(monkeypatch):
    """그날 돌았지만 안을 못 만든 요청도 «돌았다» 는 사실이다."""
    _patch(monkeypatch, _Reader([_row(scenario=None)]))

    result = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    assert result.request_count == 1
    assert result.rows == []


def test_a_day_with_no_run_is_empty_rather_than_zero(monkeypatch):
    _patch(monkeypatch, _Reader([]))

    result = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    assert result.request_count == 0
    assert result.rows == []


def test_the_recommended_scenario_is_marked_only_when_it_matches(monkeypatch):
    _patch(
        monkeypatch,
        _Reader(
            [
                _row(payload={"recommended_scenario_id": "SALES-001-A", "scenarios": []}),
                _row(
                    payload={"recommended_scenario_id": "SALES-001-A", "scenarios": []},
                    scenario=_scenario(scenario_id="SALES-001-B"),
                ),
            ]
        ),
    )

    rows = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows

    assert [row.recommended for row in rows] == [True, False]


def test_nothing_is_recommended_when_the_run_named_none(monkeypatch):
    """🔴 추천이 없으면 첫 안을 추천으로 만들지 않는다."""
    _patch(monkeypatch, _Reader([_row()]))

    assert get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0].recommended is False


def test_an_unreadable_amount_is_missing_rather_than_zero(monkeypatch):
    """⚠️ 0원 제안과 «못 읽었다» 는 다른 사실이다."""
    _patch(
        monkeypatch,
        _Reader([_row(scenario=_scenario(reported_sales_amount_krw="알 수 없음"))]),
    )

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.reported_sales_amount_krw is None


def test_a_real_zero_amount_is_kept(monkeypatch):
    """매출액 0 은 사실이므로 그대로 둔다. 숨기는 기준은 **파는 양**이다."""
    _patch(monkeypatch, _Reader([_row(scenario=_scenario(reported_sales_amount_krw="0"))]))

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.reported_sales_amount_krw == Decimal(0)


def test_a_proposal_with_nothing_to_sell_is_hidden_and_counted(monkeypatch):
    """🔴 팔 물량이 0이면 안이 선 것이 아니다.

    재무도 검토할 것이 없어 판정이 영원히 안 붙고, 화면에서는 «재무 검토 전» 으로 남아
    실제로 밀린 안처럼 보인다. 실측에서 판정 없는 안 287건이 **전부** 수량 0이었다.
    """
    _patch(
        monkeypatch,
        _Reader(
            [
                _row(scenario=_scenario(quantity_kg="0.0"), finance_verdict=None),
                _row(scenario=_scenario(scenario_id="SALES-001-B")),
            ]
        ),
    )

    result = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    assert result.hidden_zero_quantity == 1
    assert [row.scenario_id for row in result.rows] == ["SALES-001-B"]


def test_only_the_failing_rules_become_reasons(monkeypatch):
    """🔴 통과 사유를 거절 사유로 적지 않는다."""
    _patch(
        monkeypatch,
        _Reader(
            [
                _row(
                    finance_verdict="FAIL",
                    rule_results=[
                        {
                            "rule_id": "FIN-SALES-AMOUNT",
                            "verdict": "PASS",
                            "reason_codes": ["SALES_AMOUNT_MATCH"],
                        },
                        {
                            "rule_id": "FIN-SALES-MARGIN",
                            "verdict": "FAIL",
                            "reason_codes": ["SALES_MARGIN_BELOW_MINIMUM"],
                        },
                    ],
                )
            ]
        ),
    )

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.finance_reason_codes == ["SALES_MARGIN_BELOW_MINIMUM"]


def test_a_review_required_rule_also_carries_its_reason(monkeypatch):
    """«확인 필요» 도 왜 그런지 사유가 있어야 한다."""
    _patch(
        monkeypatch,
        _Reader(
            [
                _row(
                    finance_verdict="REVIEW_REQUIRED",
                    rule_results=[
                        {
                            "rule_id": "FIN-SALES-MARGIN",
                            "verdict": "REVIEW_REQUIRED",
                            "reason_codes": ["SALES_MARGIN_BELOW_WARNING"],
                        },
                    ],
                )
            ]
        ),
    )

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.finance_reason_codes == ["SALES_MARGIN_BELOW_WARNING"]


def test_the_evidence_the_proposal_leaned_on_comes_through(monkeypatch):
    """근거를 펴 보려면 참조가 화면까지 와야 한다."""
    _patch(
        monkeypatch,
        _Reader(
            [
                _row(
                    scenario=_scenario(
                        evidence_refs=[
                            "DB:inventory_lots/sim_run_id=X",
                            "MASTER-DAY-OPEN:2026-03-10",
                        ],
                        inventory_cost_basis={
                            "amount_krw": "318081.0",
                            "quantity_kg": "463.0",
                            "cost_method": "ACTUAL",
                            "source_refs": ["LOT-A", "LOT-B"],
                        },
                        supply={
                            "confirmed_quantity_kg": "463.0",
                            "conditional_quantity_kg": "0",
                            "additional_supply_required": False,
                        },
                    )
                )
            ]
        ),
    )

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert len(row.evidence_refs) == 2
    assert row.cost_basis_amount_krw == Decimal("318081.0")
    assert row.cost_basis_method == "ACTUAL"
    assert row.cost_basis_refs == ["LOT-A", "LOT-B"]
    assert row.confirmed_quantity_kg == Decimal("463.0")
    assert row.additional_supply_required is False


def test_why_finance_has_not_judged_is_carried(monkeypatch):
    """⚠️ «검토 전» 이면 왜 검토가 안 됐는지도 함께 와야 한다."""
    _patch(
        monkeypatch,
        _Reader(
            [
                _row(
                    finance_verdict=None,
                    finance_status=None,
                    rule_results=None,
                    financial_summary=None,
                    payload={
                        "recommended_scenario_id": None,
                        "scenarios": [],
                        "missing_capabilities": ["FINANCIAL_VALIDATION"],
                    },
                )
            ]
        ),
    )

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.finance_verdict is None
    assert row.missing_capabilities == ["FINANCIAL_VALIDATION"]


def test_an_unknown_supply_flag_stays_unknown(monkeypatch):
    """🔴 `None` 은 «모른다» 다. `False` 로 바꾸면 «확인했고 아니다» 가 된다."""
    _patch(monkeypatch, _Reader([_row(scenario=_scenario(supply={}))]))

    row = get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert row.additional_supply_required is None
    assert row.confirmed_quantity_kg is None


def test_the_run_axis_and_the_day_are_both_carried(monkeypatch):
    """🔴 실행 축이 빠지면 다른 실행의 판매안이 오늘 화면에 섞인다."""
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    query, params = reader.calls[0]
    assert params == [RUN, AS_OF, RUN]
    assert "context'->>'sim_run_id' = %s" in query
    assert "run.as_of = %s" in query


def test_only_the_latest_run_of_each_request_is_shown(monkeypatch):
    """되먹임이 돌면 같은 요청이 여러 번 저장된다. 그중 마지막이 그날의 답이다."""
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    query = reader.calls[0][0]
    assert "DISTINCT ON (run.response_payload->>'request_id')" in query
    assert "run.created_at DESC" in query


def test_a_finance_verdict_from_another_run_never_reaches_this_proposal(monkeypatch):
    """🔴 **같은 요청 키가 여러 실행에 걸쳐 있다.**

    `REQ-DAILY-SALES-20260107-배추` 는 축을 담지 않는 키라 실측에서 네 실행에 걸쳐
    있었다. 키만으로 이으면 남의 실행에서 내려진 판정이 이 안에 붙는다 — 같은 안이
    한쪽 화면에서는 진행 가능, 다른 쪽에서는 진행 어려움이 된다.

    ★ 축은 마스터가 안다. 재무 실행 행에는 `sim_run_id` 칸이 없으므로, 그 연결을 적어
      둔 `master_agent_runs` 에 물어 **판정 쪽에도 같은 축을 건다.**
    """
    shared_request = "REQ-DAILY-SALES-20260107-배추"
    shared_scenario = "SALES-001-A"

    class _AxisAwareReader:
        """실행 축까지 보는 가짜 저장소. **축이 맞는 판정만 돌려준다.**"""

        def __init__(self) -> None:
            self.verdicts = {
                ("SIM-CHAIN-V13", shared_request, shared_scenario): "PASS",
                ("SIM-WALK-2026-FINAL", shared_request, shared_scenario): "FAIL",
            }
            self.calls: list[tuple[str, list]] = []

        def __call__(self, query, params=None):
            params = list(params or [])
            self.calls.append((str(query), params))
            #  쿼리가 축을 두 번 실어야 여기서 고를 수 있다 — 하나는 판매, 하나는 재무다.
            assert len(params) == 3, params
            sales_axis, _as_of, finance_axis = params
            assert sales_axis == finance_axis
            verdict = self.verdicts.get((finance_axis, shared_request, shared_scenario))
            return [
                {
                    "request_id": shared_request,
                    "payload": {"recommended_scenario_id": None, "scenarios": []},
                    "scenario": _scenario(scenario_id=shared_scenario),
                    "finance_verdict": verdict,
                    "finance_status": None if verdict is None else "EVALUATED",
                    "rule_results": None
                    if verdict != "FAIL"
                    else [
                        {
                            "rule_id": "FIN-SALES-MARGIN",
                            "verdict": "FAIL",
                            "reason_codes": ["SALES_MARGIN_BELOW_MINIMUM"],
                        }
                    ],
                    "financial_summary": None,
                }
            ]

    reader = _AxisAwareReader()
    _patch(monkeypatch, reader)

    mine = get_console_sales_proposals(sim_run_id="SIM-CHAIN-V13", as_of=AS_OF).rows[0]
    theirs = get_console_sales_proposals(
        sim_run_id="SIM-WALK-2026-FINAL", as_of=AS_OF
    ).rows[0]

    #  같은 요청 키·같은 안인데 실행이 다르면 판정도 다르다.
    assert mine.scenario_id == theirs.scenario_id == shared_scenario
    assert mine.finance_verdict == "PASS"
    assert theirs.finance_verdict == "FAIL"
    assert mine.finance_reason_codes == []
    assert theirs.finance_reason_codes == ["SALES_MARGIN_BELOW_MINIMUM"]


def test_the_finance_verdict_lookup_carries_the_run_axis(monkeypatch):
    """축을 거는 자리가 SQL 안에 실제로 있는지 본다."""
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    query, params = reader.calls[0]
    #  🔴 키 문자열을 파싱하지 않는다 — 축은 마스터가 적어 둔 연결에서 온다.
    assert "master_agent_runs" in query
    assert "axis.sim_run_id = %s" in query
    assert params == [RUN, AS_OF, RUN]


def test_the_newest_finance_reply_wins(monkeypatch):
    """재검증이 돌면 같은 키에 회신이 쌓인다."""
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    query = reader.calls[0][0]
    assert "check_run.created_at DESC" in query
    assert "mode = 'SALES_VALIDATION'" in query


def test_proposals_are_never_written_back(monkeypatch):
    """🔴 화면이 안을 다시 만들지 않는다 — 읽기 전용이다."""
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_proposals(sim_run_id=RUN, as_of=AS_OF)

    query = reader.calls[0][0].upper()
    for word in ("INSERT", "UPDATE", "DELETE"):
        assert word not in query
