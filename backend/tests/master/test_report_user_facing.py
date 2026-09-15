"""매입안 보고서가 **사용자 문서**인가 (2026-09-15 결정).

잠그는 것:

```text
검증 섹션 없음        지적·확인 필요·못 돈 검사·검사 커버리지를 싣지 않는다
코드 노출 0          종료 코드·요청 번호·입력 출처·근거 등급·참조 번호·영문 코드
다른 품목 없음        이 실행 품목만 제목·파일 이름에
안 못 낸 실행         「오늘은 매입안을 내지 않았습니다 — 사유」 한 줄
```

★ DB 를 안 쓴다. 실제 응답 모양을 줄인 표본을 손으로 만든다.
"""

from __future__ import annotations

from app.master.report import render_report, report_filename

_FORBIDDEN = (
    "검증",
    "지적",
    "못 돈 검사",
    "Critic",
    "E1_APPROVED",
    "E2_HELD",
    "REQ-",
    "MEASURED",
    "DERIVED",
    "burn_in",
    "sim_run_id",
    "forecast",
    "quantity",
    "OFFICIAL",
    "SIM_FIXED",
    "시뮬 고정값",
    "FC-",
    "MQ-",
    "SO-",
    "CAP-",
    "LOG-H",
    "mock",
    "산출하지 못함",
    "`",
)


def _run() -> dict:
    return {
        "request_id": "REQ-20260113-0001",
        "as_of": "2026-01-13",
        "end_code": "E1_APPROVED",
        "reason": "사용자 선택 대기",
        "findings": ["지적 하나"],
        "concerns": ["EVIDENCE-VALUE-NOT-NUMERIC: 확인 필요 문장"],
        "skipped_checks": ["Critic 커버리지 21/56 — L0 6/6"],
        "input_sources": {
            "forecast": "MEASURED:v_ml_price_forecast(as_of=2026-01-13, AUC)",
            "sim_run_id": "DEFAULT:burn_in",
        },
        "mocked_inputs": ["inventory_lots"],
        "judgment": {"meta": {"item": "배추", "as_of": "2026-01-13"}},
        "constraints": {
            "inventory": {
                "item_storage_policies": [
                    {"item": "양파", "operational_limit_days": 30},
                    {"item": "배추", "operational_limit_days": 10},
                ]
            }
        },
        "verdicts": {
            "finance": {
                "business_status": "ok",
                "runtime_status": "READY",
                "reasoning": "그대로 진행하실 수 있습니다.",
                "payload": {
                    "verdicts": [
                        {
                            "scenario_id": "보수",
                            "verdict": "ok",
                            "rule_id": "FIN-BASE-STRESS",
                            "reason": "운영 자금이 부족해지지 않습니다.",
                            "finance_cap_amount_krw": 6206629,
                            "scenario_projected_cash_min": 17920984.6,
                            "stress_projected_cash_min": 17652639.6,
                            "payment_schedule": [
                                {
                                    "payment_date": "2026-01-13",
                                    "amount_krw": 1226925,
                                    "basis": "non_split_policy_reconstruction",
                                }
                            ],
                        }
                    ]
                },
            },
            "inventory": {
                "business_status": "conditional",
                "runtime_status": "READY",
                "reasoning": "매입 시나리오를 물류 관점에서 판정했다.",
                "payload": {
                    "verdict": "conditional",
                    "soft_warnings": ["SNAPSHOT_ID_UNRESOLVED"],
                    "hard_constraints": [
                        {"code": "LOG-H01", "status": "PASS"},
                        {"code": "LOG-H02", "status": "UNRESOLVED"},
                    ],
                    "scenario_results": [{"label": "보수", "verdict": "ok"}],
                    "expected_arrival_dates": ["2026-01-14"],
                    "cap_by_date": {"2026-01-14": 8000.0},
                },
            },
        },
        "scenarios": [
            {
                "label": "보수",
                "strategy_type": "quantity",
                "coverage_days": 2,
                "total_qty_kg": 1435,
                "total_amount_krw": 1226925,
                "max_price": 1042,
                "expected_margin_rate": None,
                "split_plan": [{"seq": 1, "date": "2026-01-13", "qty_kg": 1435}],
                "sourcing_plan": [
                    {"market": "가락", "grade": "특", "qty_kg": 1435, "grade_unit_price": 855}
                ],
                "rationale": [
                    {
                        "source": "예측",
                        "claim": "D+14 예측 -6.0%, 신뢰구간 폭 63.9%",
                        "evidence_grade": "SIM_FIXED",
                        "ref_id": "FC-ops_auc-2026-01-13",
                    },
                    {
                        "source": "시세관측",
                        "claim": "가락 2026-01-12 경락가 855원/kg 등 1개 등급",
                        "evidence_grade": "OFFICIAL",
                        "ref_id": "MQ-가락-2026-01-12",
                    },
                    {
                        "source": "재고",
                        "claim": "날짜별 입고 여유 8,000kg (전량 8,000kg 중 0kg 이 예정)",
                        "evidence_grade": "SIM_FIXED",
                        "ref_id": "CAP-2026-01-13",
                    },
                ],
                "risks": [
                    "문서 1종을 요청했으나 읽지 못했다 — 그날 발간물이 0건이었다는 뜻이 아니다",
                    "기준등급 '상'이 당일 시세에 없어 '특'(855원/kg)으로 배정했다",
                    "판단 재료(관측월보·기상·작년동기)를 읽지 못해 문서 보강 없이 판단했다",
                    "물량이 적어 SO-2026-01-13 기준 `unit_price` 가 흔들린다 (thin market)",
                ],
            }
        ],
    }


def test_검증_섹션을_싣지_않는다():
    markdown = render_report(_run())

    assert "## 검증" not in markdown
    assert "확인 필요" not in markdown
    assert "입력 출처" not in markdown


def test_코드와_참조_번호가_하나도_나가지_않는다():
    markdown = render_report(_run())

    leaked = [word for word in _FORBIDDEN if word in markdown]
    assert leaked == []


def test_사람에게_필요한_것은_사람_말로_남는다():
    markdown = render_report(_run())

    assert markdown.startswith("# 배추 매입안 — 2026년 1월 13일")
    assert "| 보수안 | 1,435kg | 123만 원 | 2일 | 1,042원/kg |" in markdown
    assert "상태: 선택 대기" in markdown
    assert "- 재무 · 통과 — 그대로 진행하실 수 있습니다." in markdown
    assert "안에는 문제가 없고, 구역별 용량을 확인하지 못해 조건부입니다." in markdown
    assert "매입량 1,435kg · 금액 1,226,925원 · 2일치" in markdown
    assert "2주 뒤 가격이 지금보다 6.0% 낮을 것으로 예측" in markdown
    assert "기준 등급인 상품이 당일 시세에 없어 특품으로 샀습니다." in markdown


def test_늘_뜨는_개발용_문장은_빼고_모르는_문장은_코드만_벗긴다():
    markdown = render_report(_run())

    assert "읽지 못했다" not in markdown
    assert "읽지 못해" not in markdown
    assert "  - 물량이 적어 기준 가 흔들린다\n" in markdown


def test_다른_품목을_싣지_않는다():
    markdown = render_report(_run())

    assert "양파" not in markdown
    assert report_filename(_run()) == "2026-01-13_배추_매입안.md"


def test_파일_이름에_요청_번호_원문이_없고_같은_날_두번째는_구분한다():
    run = {**_run(), "request_id": "REQ-20260113-0002"}

    assert report_filename(run) == "2026-01-13_배추_매입안_2.md"


def test_회신에_품목이_없으면_요청_품목을_쓴다():
    run = {**_run(), "judgment": {}}

    assert render_report(run, item="양파").startswith("# 양파 매입안")
    assert report_filename(run, item="양파") == "2026-01-13_양파_매입안.md"


def test_결정이_있으면_상태에_적는다():
    markdown = render_report(_run(), decision={"decision": "APPROVE", "scenario_label": "보수"})

    assert "상태: 보수안 승인됨" in markdown


def test_안을_못_낸_실행은_사유를_한_줄로_말한다():
    run = {
        "request_id": "REQ-20260113-0001",
        "as_of": "2026-01-13",
        "end_code": "E2_HELD",
        "reason": "유효한 안이 없어 제안을 내지 못했다.",
        "judgment": {
            "meta": {"item": "배추"},
            "no_proposal_reason": "창고 여유가 없어 `LOG-H01` 기준 안을 낼 수 없다",
            "rejected_reasons": [{"label": "기본", "reason": "창고를 넘친다 (CAP-2026-01-13)"}],
        },
        "skipped_checks": ["Critic 커버리지 0/56"],
    }

    markdown = render_report(run)

    assert "오늘은 매입안을 내지 않았습니다 — 창고 여유가 없어 기준 안을 낼 수 없다" in markdown
    assert "- 기본안: 창고를 넘친다" in markdown
    assert [word for word in _FORBIDDEN if word in markdown] == []


def test_mock_으로_멈춘_실행도_코드_없이_말한다():
    run = {
        "request_id": "REQ-20260113-0001",
        "as_of": "2026-01-13",
        "end_code": "E4_NOT_STARTED",
        "reason": "mock 입력으로는 판단하지 않는다: forecast. 실 데이터를 못 읽은 것이므로",
    }

    markdown = render_report(run, item="배추")

    expected = "필요한 자료를 읽지 못해 판단을 시작하지 않았습니다."
    assert f"오늘은 매입안을 내지 않았습니다 — {expected}" in markdown
    assert [word for word in _FORBIDDEN if word in markdown] == []
