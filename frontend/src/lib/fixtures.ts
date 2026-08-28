/**
 * 시연 고정 표본 — **손으로 옮겨 적은 값이 하나도 없다.**
 * `backend/app/purchase_agent/adapter.py:purchase_port` 를 실제로 돌려 받은 응답을
 * 그대로 직렬화했다 (dev 934dd6f · 2026-08-28).
 *
 * 두 가지가 섞여 있으니 구분해서 쓴다:
 *
 *   input     호출자가 `POST /master/request` 본문에 **실어 보내는 것**.
 *             마스터는 ML·영업·정책 테이블을 직접 읽지 않으므로(정의서 §3.2.5 예외)
 *             예측·확정주문·정책값은 요청에 담는 것이 계약이다. fallback 이 아니다.
 *
 *   fallback  API 가 실패하거나 시나리오를 0건으로 돌려줄 때 **화면에 띄우는 표본**.
 *             화면 어딘가에 출처를 반드시 밝힌다 — 실측과 표본이 섞이면 안 된다.
 *
 * 갱신하려면 scratchpad/gen_fixtures.py 를 다시 돌린다. 이 파일은 직접 고치지 않는다.
 */
import type { CallerInput, FallbackProposal } from "./types";

export interface Scene {
  input: CallerInput;
  fallback: FallbackProposal;
  boundary: { finance: Record<string, unknown>; inventory: Record<string, unknown> };
  financeCap: number;
  blurb: string;
}

export const SCENES: Record<string, Scene> = {
  "2025-12-31": {
    "input": {
      "item": "배추",
      "forecast": {
        "generated_at": "2025-12-31T06:00:00+09:00",
        "item": "배추",
        "unit": "원/kg",
        "current_price": 1650,
        "horizon_days": 18,
        "daily": [
          {
            "date": "2026-01-01",
            "predicted": 1666,
            "lower": 1616,
            "upper": 1716
          },
          {
            "date": "2026-01-02",
            "predicted": 1681,
            "lower": 1631,
            "upper": 1731
          },
          {
            "date": "2026-01-03",
            "predicted": 1697,
            "lower": 1646,
            "upper": 1748
          },
          {
            "date": "2026-01-04",
            "predicted": 1713,
            "lower": 1662,
            "upper": 1764
          },
          {
            "date": "2026-01-05",
            "predicted": 1729,
            "lower": 1677,
            "upper": 1781
          },
          {
            "date": "2026-01-06",
            "predicted": 1745,
            "lower": 1693,
            "upper": 1797
          },
          {
            "date": "2026-01-07",
            "predicted": 1762,
            "lower": 1709,
            "upper": 1815
          },
          {
            "date": "2026-01-08",
            "predicted": 1778,
            "lower": 1725,
            "upper": 1831
          },
          {
            "date": "2026-01-09",
            "predicted": 1795,
            "lower": 1741,
            "upper": 1849
          },
          {
            "date": "2026-01-10",
            "predicted": 1812,
            "lower": 1758,
            "upper": 1866
          },
          {
            "date": "2026-01-11",
            "predicted": 1829,
            "lower": 1774,
            "upper": 1884
          },
          {
            "date": "2026-01-12",
            "predicted": 1846,
            "lower": 1791,
            "upper": 1901
          },
          {
            "date": "2026-01-13",
            "predicted": 1863,
            "lower": 1807,
            "upper": 1919
          },
          {
            "date": "2026-01-14",
            "predicted": 1881,
            "lower": 1825,
            "upper": 1937
          },
          {
            "date": "2026-01-15",
            "predicted": 1899,
            "lower": 1842,
            "upper": 1956
          },
          {
            "date": "2026-01-16",
            "predicted": 1917,
            "lower": 1859,
            "upper": 1975
          },
          {
            "date": "2026-01-17",
            "predicted": 1935,
            "lower": 1877,
            "upper": 1993
          },
          {
            "date": "2026-01-18",
            "predicted": 1953,
            "lower": 1894,
            "upper": 2012
          }
        ],
        "model_version": "mock-v0"
      },
      "confirmed_orders": {
        "as_of": "2025-12-31",
        "item": "배추",
        "orders": [
          {
            "sale_id": 7,
            "qty_kg": 12000,
            "due_date": "2026-01-03"
          },
          {
            "sale_id": 9,
            "qty_kg": 6000,
            "due_date": "2026-01-08"
          }
        ],
        "total_kg": 18000
      },
      "policy_values": {
        "contract_price_krw": 2293,
        "item_mix_ratio": {
          "배추": 0.812,
          "무": 0.081,
          "양파": 0.068,
          "피마늘": 0.039
        }
      }
    },
    "fallback": {
      "situation": "stable",
      "allowed_axes": [
        "quantity",
        "timing"
      ],
      "confidence": "high",
      "context_docs_used": [],
      "meta": {
        "as_of": "2025-12-31",
        "item": "배추",
        "agent_version": "v1.1",
        "is_refeed": false,
        "feedback_attempt": 0
      },
      "scenarios": [
        {
          "label": "보수",
          "strategy_type": "quantity",
          "coverage_days": 2,
          "total_qty_kg": 2571,
          "total_amount_krw": 4242150,
          "max_price": 1731,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2025-12-31",
              "qty_kg": 2571
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 2571,
              "grade_unit_price": 1650
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +14.0%, 신뢰구간 폭 6.0%",
              "ref_id": "FC-mock-v0-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=2",
              "ref_id": "SO-2025-12-31",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요"
          ]
        },
        {
          "label": "기본",
          "strategy_type": "quantity",
          "coverage_days": 5,
          "total_qty_kg": 6429,
          "total_amount_krw": 10607850,
          "max_price": 1781,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2025-12-31",
              "qty_kg": 6429
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 6429,
              "grade_unit_price": 1650
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +14.0%, 신뢰구간 폭 6.0%",
              "ref_id": "FC-mock-v0-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=5",
              "ref_id": "SO-2025-12-31",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요"
          ]
        },
        {
          "label": "공격",
          "strategy_type": "timing",
          "coverage_days": 12,
          "total_qty_kg": 12121,
          "total_amount_krw": 19999650,
          "max_price": 1901,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2025-12-31",
              "qty_kg": 6060
            },
            {
              "seq": 2,
              "date": "2026-01-06",
              "qty_kg": 6061
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 12121,
              "grade_unit_price": 1650
            }
          ],
          "payment_schedule": [
            {
              "seq": 1,
              "purchase_date": "2025-12-31",
              "payment_date": "2026-01-07",
              "qty_kg": 6060,
              "amount_krw": 9999000,
              "amount_max_krw": 11520060,
              "basis": "as_of_unit_price"
            },
            {
              "seq": 2,
              "purchase_date": "2026-01-06",
              "payment_date": "2026-01-13",
              "qty_kg": 6061,
              "amount_krw": 10000650,
              "amount_max_krw": 11521961,
              "basis": "as_of_unit_price"
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +14.0%, 신뢰구간 폭 6.0%",
              "ref_id": "FC-mock-v0-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=12",
              "ref_id": "SO-2025-12-31",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            },
            {
              "source": "예측",
              "claim": "판정일까지 지속 상승 궤적 → 2회 분할로 로트 나이 분산",
              "ref_id": "FC-mock-v0-2025-12-31",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "상승장 분할은 평균단가에 불리하고 로트 나이 분산에 유리하다 — 그 트레이드오프 판단은 LLM 몫이라 지금은 균등 배분이다 (상세설계 §4-④)"
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "현금 제약으로 원안 15,429kg에서 12,121kg으로 축소",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요",
            "2회 분할 — 회차별 도착일(= 회차 date + N4) 기준 cap_by_date 검사는 inbound_lead_days(N4) 미확정으로 보류 (상세설계 §5.5 · 규칙 3). 총량 단일 도착일로 뭉치면 분할의 창고 부담 분산 효과가 검증되지 않는다"
          ]
        }
      ],
      "reasoning": "보수·기본·공격 안을 냈다. 예측 구간이 안정 범위다. 열린 전략축은 quantity·timing이다.",
      "evidences": [
        {
          "claim": "situation",
          "source": "tool_calc",
          "ref_ids": [
            "배추-CI-2025-12-31"
          ],
          "value": 0.059543,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "D+14 구간폭을 임계 0.08와 비교해 stable 판정"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-CI-2025-12-31"
          ],
          "value": 0.059543,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "구간폭 0.060 < 0.08 → stable → 선매입 궤적 허용"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-VOL-2025-12-31"
          ],
          "value": 15429.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "추정 총량 15,429kg < 임계 20,000kg → 총량 트리거 미달"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-MIX-2025-12-31"
          ],
          "value": 0.812,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "품목 편중 최대 0.812 ≥ 0.7 → mix 제외"
        },
        {
          "claim": "scenarios",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SCEN-2025-12-31"
          ],
          "value": 3.0,
          "unit": "count",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "stable 판정에서 파생 — 불확실이면 공격안을 만들지 않아 두 안이 된다"
        },
        {
          "claim": "scenarios[0].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2025-12-31"
          ],
          "value": 2.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[0].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2025-12-31"
          ],
          "value": 2571.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[0].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2025-12-31"
          ],
          "value": 4242150.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[0].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2025-12-31"
          ],
          "value": 1731.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[0].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2025-12-31"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — (contract_price − 가중 매입단가) ÷ contract_price"
        },
        {
          "claim": "scenarios[1].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2025-12-31"
          ],
          "value": 5.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[1].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2025-12-31"
          ],
          "value": 6429.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[1].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2025-12-31"
          ],
          "value": 10607850.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[1].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2025-12-31"
          ],
          "value": 1781.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[1].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2025-12-31"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — (contract_price − 가중 매입단가) ÷ contract_price"
        },
        {
          "claim": "scenarios[2].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2025-12-31"
          ],
          "value": 12.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[2].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2025-12-31"
          ],
          "value": 12121.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[2].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2025-12-31"
          ],
          "value": 19999650.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[2].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2025-12-31"
          ],
          "value": 1901.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[2].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2025-12-31"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — (contract_price − 가중 매입단가) ÷ contract_price"
        }
      ],
      "run_id": "PUR-RUN-REQ-2025-12-31-배추-1",
      "used_tools": [
        "assess_market_situation",
        "draft_purchase_quantities",
        "plan_split_purchase",
        "allocate_grade_mix",
        "compose_and_verify_scenarios"
      ],
      "missing_data": []
    },
    "boundary": {
      "finance": {
        "base_projected_cash_min": 24000000,
        "margin_defense_floor_rate": 0.267,
        "finance_cap_amount_krw": 20000000,
        "purchase_payment_days": 7,
        "critical_payment_dates": []
      },
      "inventory": {
        "as_of": "2025-12-31",
        "item": "배추",
        "warehouse_free_kg": 12000,
        "rental_cap_kg": 3600,
        "lot_count": 1,
        "first_lot": {
          "lot_id": 12,
          "grade": "상",
          "stocked_at": "2025-12-27",
          "remaining_kg": 3000,
          "shelf_life_days": 10
        }
      }
    },
    "financeCap": 20000000,
    "blurb": "통합 · 3안"
  },
  "2026-08-21": {
    "input": {
      "item": "배추",
      "forecast": {
        "generated_at": "2026-08-21T06:00:00+09:00",
        "item": "배추",
        "unit": "원/kg",
        "current_price": 1650,
        "horizon_days": 18,
        "daily": [
          {
            "date": "2026-08-22",
            "predicted": 1666,
            "lower": 1616,
            "upper": 1716
          },
          {
            "date": "2026-08-23",
            "predicted": 1681,
            "lower": 1631,
            "upper": 1731
          },
          {
            "date": "2026-08-24",
            "predicted": 1697,
            "lower": 1646,
            "upper": 1748
          },
          {
            "date": "2026-08-25",
            "predicted": 1713,
            "lower": 1662,
            "upper": 1764
          },
          {
            "date": "2026-08-26",
            "predicted": 1729,
            "lower": 1677,
            "upper": 1781
          },
          {
            "date": "2026-08-27",
            "predicted": 1745,
            "lower": 1693,
            "upper": 1797
          },
          {
            "date": "2026-08-28",
            "predicted": 1762,
            "lower": 1709,
            "upper": 1815
          },
          {
            "date": "2026-08-29",
            "predicted": 1778,
            "lower": 1725,
            "upper": 1831
          },
          {
            "date": "2026-08-30",
            "predicted": 1795,
            "lower": 1741,
            "upper": 1849
          },
          {
            "date": "2026-08-31",
            "predicted": 1812,
            "lower": 1758,
            "upper": 1866
          },
          {
            "date": "2026-09-01",
            "predicted": 1829,
            "lower": 1774,
            "upper": 1884
          },
          {
            "date": "2026-09-02",
            "predicted": 1846,
            "lower": 1791,
            "upper": 1901
          },
          {
            "date": "2026-09-03",
            "predicted": 1863,
            "lower": 1807,
            "upper": 1919
          },
          {
            "date": "2026-09-04",
            "predicted": 1881,
            "lower": 1825,
            "upper": 1937
          },
          {
            "date": "2026-09-05",
            "predicted": 1899,
            "lower": 1842,
            "upper": 1956
          },
          {
            "date": "2026-09-06",
            "predicted": 1917,
            "lower": 1859,
            "upper": 1975
          },
          {
            "date": "2026-09-07",
            "predicted": 1935,
            "lower": 1877,
            "upper": 1993
          },
          {
            "date": "2026-09-08",
            "predicted": 1953,
            "lower": 1894,
            "upper": 2012
          }
        ],
        "model_version": "mock-v0"
      },
      "confirmed_orders": {
        "as_of": "2026-08-21",
        "item": "배추",
        "orders": [
          {
            "sale_id": 7,
            "qty_kg": 12000,
            "due_date": "2026-08-24"
          },
          {
            "sale_id": 9,
            "qty_kg": 6000,
            "due_date": "2026-08-29"
          }
        ],
        "total_kg": 18000
      },
      "policy_values": {
        "contract_price_krw": 2293,
        "item_mix_ratio": {
          "배추": 0.812,
          "무": 0.081,
          "양파": 0.068,
          "피마늘": 0.039
        }
      }
    },
    "fallback": {
      "situation": "stable",
      "allowed_axes": [
        "quantity",
        "timing"
      ],
      "confidence": "high",
      "context_docs_used": [],
      "meta": {
        "as_of": "2026-08-21",
        "item": "배추",
        "agent_version": "v1.1",
        "is_refeed": false,
        "feedback_attempt": 0
      },
      "scenarios": [
        {
          "label": "보수",
          "strategy_type": "quantity",
          "coverage_days": 2,
          "total_qty_kg": 2571,
          "total_amount_krw": 4242150,
          "max_price": 1731,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2026-08-21",
              "qty_kg": 2571
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 2571,
              "grade_unit_price": 1650
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +14.0%, 신뢰구간 폭 6.0%",
              "ref_id": "FC-mock-v0-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=2",
              "ref_id": "SO-2026-08-21",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요"
          ]
        },
        {
          "label": "기본",
          "strategy_type": "quantity",
          "coverage_days": 5,
          "total_qty_kg": 6429,
          "total_amount_krw": 10607850,
          "max_price": 1781,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2026-08-21",
              "qty_kg": 6429
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 6429,
              "grade_unit_price": 1650
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +14.0%, 신뢰구간 폭 6.0%",
              "ref_id": "FC-mock-v0-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=5",
              "ref_id": "SO-2026-08-21",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요"
          ]
        },
        {
          "label": "공격",
          "strategy_type": "timing",
          "coverage_days": 12,
          "total_qty_kg": 12121,
          "total_amount_krw": 19999650,
          "max_price": 1901,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2026-08-21",
              "qty_kg": 6060
            },
            {
              "seq": 2,
              "date": "2026-08-27",
              "qty_kg": 6061
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 12121,
              "grade_unit_price": 1650
            }
          ],
          "payment_schedule": [
            {
              "seq": 1,
              "purchase_date": "2026-08-21",
              "payment_date": "2026-08-28",
              "qty_kg": 6060,
              "amount_krw": 9999000,
              "amount_max_krw": 11520060,
              "basis": "as_of_unit_price"
            },
            {
              "seq": 2,
              "purchase_date": "2026-08-27",
              "payment_date": "2026-09-03",
              "qty_kg": 6061,
              "amount_krw": 10000650,
              "amount_max_krw": 11521961,
              "basis": "as_of_unit_price"
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +14.0%, 신뢰구간 폭 6.0%",
              "ref_id": "FC-mock-v0-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=12",
              "ref_id": "SO-2026-08-21",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            },
            {
              "source": "예측",
              "claim": "판정일까지 지속 상승 궤적 → 2회 분할로 로트 나이 분산",
              "ref_id": "FC-mock-v0-2026-08-21",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "상승장 분할은 평균단가에 불리하고 로트 나이 분산에 유리하다 — 그 트레이드오프 판단은 LLM 몫이라 지금은 균등 배분이다 (상세설계 §4-④)"
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "현금 제약으로 원안 15,429kg에서 12,121kg으로 축소",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요",
            "2회 분할 — 회차별 도착일(= 회차 date + N4) 기준 cap_by_date 검사는 inbound_lead_days(N4) 미확정으로 보류 (상세설계 §5.5 · 규칙 3). 총량 단일 도착일로 뭉치면 분할의 창고 부담 분산 효과가 검증되지 않는다"
          ]
        }
      ],
      "reasoning": "보수·기본·공격 안을 냈다. 예측 구간이 안정 범위다. 열린 전략축은 quantity·timing이다.",
      "evidences": [
        {
          "claim": "situation",
          "source": "tool_calc",
          "ref_ids": [
            "배추-CI-2026-08-21"
          ],
          "value": 0.059543,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "D+14 구간폭을 임계 0.08와 비교해 stable 판정"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-CI-2026-08-21"
          ],
          "value": 0.059543,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "구간폭 0.060 < 0.08 → stable → 선매입 궤적 허용"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-VOL-2026-08-21"
          ],
          "value": 15429.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "추정 총량 15,429kg < 임계 20,000kg → 총량 트리거 미달"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-MIX-2026-08-21"
          ],
          "value": 0.812,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "품목 편중 최대 0.812 ≥ 0.7 → mix 제외"
        },
        {
          "claim": "scenarios",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SCEN-2026-08-21"
          ],
          "value": 3.0,
          "unit": "count",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "stable 판정에서 파생 — 불확실이면 공격안을 만들지 않아 두 안이 된다"
        },
        {
          "claim": "scenarios[0].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-08-21"
          ],
          "value": 2.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[0].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-08-21"
          ],
          "value": 2571.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[0].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-08-21"
          ],
          "value": 4242150.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[0].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-08-21"
          ],
          "value": 1731.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[0].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-08-21"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — (contract_price − 가중 매입단가) ÷ contract_price"
        },
        {
          "claim": "scenarios[1].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-08-21"
          ],
          "value": 5.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[1].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-08-21"
          ],
          "value": 6429.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[1].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-08-21"
          ],
          "value": 10607850.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[1].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-08-21"
          ],
          "value": 1781.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[1].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-08-21"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — (contract_price − 가중 매입단가) ÷ contract_price"
        },
        {
          "claim": "scenarios[2].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2026-08-21"
          ],
          "value": 12.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[2].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2026-08-21"
          ],
          "value": 12121.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[2].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2026-08-21"
          ],
          "value": 19999650.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[2].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2026-08-21"
          ],
          "value": 1901.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[2].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC2-2026-08-21"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "공격안 — (contract_price − 가중 매입단가) ÷ contract_price"
        }
      ],
      "run_id": "PUR-RUN-REQ-2026-08-21-배추-1",
      "used_tools": [
        "assess_market_situation",
        "draft_purchase_quantities",
        "plan_split_purchase",
        "allocate_grade_mix",
        "compose_and_verify_scenarios"
      ],
      "missing_data": []
    },
    "boundary": {
      "finance": {
        "base_projected_cash_min": 24000000,
        "margin_defense_floor_rate": 0.267,
        "finance_cap_amount_krw": 20000000,
        "purchase_payment_days": 7,
        "critical_payment_dates": []
      },
      "inventory": {
        "as_of": "2026-08-21",
        "item": "배추",
        "warehouse_free_kg": 12000,
        "rental_cap_kg": 3600,
        "lot_count": 1,
        "first_lot": {
          "lot_id": 12,
          "grade": "상",
          "stocked_at": "2026-08-17",
          "remaining_kg": 3000,
          "shelf_life_days": 10
        }
      }
    },
    "financeCap": 20000000,
    "blurb": "안정 · 3안"
  },
  "2026-09-04": {
    "input": {
      "item": "배추",
      "forecast": {
        "generated_at": "2026-09-04T06:00:00+09:00",
        "item": "배추",
        "unit": "원/kg",
        "current_price": 1650,
        "horizon_days": 18,
        "daily": [
          {
            "date": "2026-09-05",
            "predicted": 1671,
            "lower": 1571,
            "upper": 1771
          },
          {
            "date": "2026-09-06",
            "predicted": 1692,
            "lower": 1590,
            "upper": 1794
          },
          {
            "date": "2026-09-07",
            "predicted": 1714,
            "lower": 1611,
            "upper": 1817
          },
          {
            "date": "2026-09-08",
            "predicted": 1735,
            "lower": 1631,
            "upper": 1839
          },
          {
            "date": "2026-09-09",
            "predicted": 1756,
            "lower": 1651,
            "upper": 1861
          },
          {
            "date": "2026-09-10",
            "predicted": 1777,
            "lower": 1670,
            "upper": 1884
          },
          {
            "date": "2026-09-11",
            "predicted": 1799,
            "lower": 1691,
            "upper": 1907
          },
          {
            "date": "2026-09-12",
            "predicted": 1787,
            "lower": 1680,
            "upper": 1894
          },
          {
            "date": "2026-09-13",
            "predicted": 1775,
            "lower": 1669,
            "upper": 1882
          },
          {
            "date": "2026-09-14",
            "predicted": 1763,
            "lower": 1657,
            "upper": 1869
          },
          {
            "date": "2026-09-15",
            "predicted": 1751,
            "lower": 1646,
            "upper": 1856
          },
          {
            "date": "2026-09-16",
            "predicted": 1740,
            "lower": 1636,
            "upper": 1844
          },
          {
            "date": "2026-09-17",
            "predicted": 1728,
            "lower": 1624,
            "upper": 1832
          },
          {
            "date": "2026-09-18",
            "predicted": 1716,
            "lower": 1613,
            "upper": 1819
          },
          {
            "date": "2026-09-19",
            "predicted": 1704,
            "lower": 1602,
            "upper": 1806
          },
          {
            "date": "2026-09-20",
            "predicted": 1691,
            "lower": 1590,
            "upper": 1792
          },
          {
            "date": "2026-09-21",
            "predicted": 1679,
            "lower": 1578,
            "upper": 1780
          },
          {
            "date": "2026-09-22",
            "predicted": 1667,
            "lower": 1567,
            "upper": 1767
          }
        ],
        "model_version": "mock-v0"
      },
      "confirmed_orders": {
        "as_of": "2026-09-04",
        "item": "배추",
        "orders": [
          {
            "sale_id": 7,
            "qty_kg": 12000,
            "due_date": "2026-09-07"
          },
          {
            "sale_id": 9,
            "qty_kg": 6000,
            "due_date": "2026-09-12"
          }
        ],
        "total_kg": 18000
      },
      "policy_values": {
        "contract_price_krw": 2293,
        "item_mix_ratio": {
          "배추": 0.812,
          "무": 0.081,
          "양파": 0.068,
          "피마늘": 0.039
        }
      }
    },
    "fallback": {
      "situation": "uncertain",
      "allowed_axes": [
        "quantity"
      ],
      "confidence": "medium",
      "context_docs_used": [
        "DOC-3",
        "DOC-4",
        "DOC-5"
      ],
      "meta": {
        "as_of": "2026-09-04",
        "item": "배추",
        "agent_version": "v1.1",
        "is_refeed": false,
        "feedback_attempt": 0
      },
      "scenarios": [
        {
          "label": "보수",
          "strategy_type": "quantity",
          "coverage_days": 2,
          "total_qty_kg": 2571,
          "total_amount_krw": 4242150,
          "max_price": 1794,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2026-09-04",
              "qty_kg": 2571
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 2571,
              "grade_unit_price": 1650
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +4.0%, 신뢰구간 폭 12.0%",
              "ref_id": "FC-mock-v0-2026-09-04",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2026-09-04",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=2",
              "ref_id": "SO-2026-09-04",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2026-09-04",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            },
            {
              "source": "문서ID",
              "claim": "KREI 관측월보 — 농업관측 8월호 — 배추",
              "ref_id": "DOC-3",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "2026-08-05 발행 · 발췌: \"고랭지 배추 정식면적은 전년 대비 6% 감소한 것으로 조사됐다.\""
            },
            {
              "source": "문서ID",
              "claim": "기상청 기상 — 1개월 전망 — 8월 하순~9월 상순",
              "ref_id": "DOC-4",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "2026-08-10 발행 · 발췌: \"8월 하순 강수량은 평년보다 많겠다.\""
            },
            {
              "source": "문서ID",
              "claim": "aT 작년동기 — 작년 동기 가락시장 배추 경락가 요약",
              "ref_id": "DOC-5",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "2026-08-01 발행 · 발췌: \"작년 8월 하순 가락시장 배추 상품 경락가는 kg당 1,480원으로 8월 상순 대비 9% 상승했다.\""
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요",
            "문서 3건 참조 — 규칙 기반 수집이라 문서 선별·충분성 판단은 미적용(우선순위 순서대로 로드). 발췌는 관련 구절 선별 없이 각 문서 서두에서 기계적으로 뜬 것이다"
          ]
        },
        {
          "label": "기본",
          "strategy_type": "quantity",
          "coverage_days": 5,
          "total_qty_kg": 6429,
          "total_amount_krw": 10607850,
          "max_price": 1861,
          "margin_warning": false,
          "split_plan": [
            {
              "seq": 1,
              "date": "2026-09-04",
              "qty_kg": 6429
            }
          ],
          "sourcing_plan": [
            {
              "market": "가락",
              "grade": "상",
              "qty_kg": 6429,
              "grade_unit_price": 1650
            }
          ],
          "expected_margin_rate": 0.2804186655037069,
          "rationale": [
            {
              "source": "예측",
              "claim": "D+14 예측 +4.0%, 신뢰구간 폭 12.0%",
              "ref_id": "FC-mock-v0-2026-09-04",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "mock-v0 경락가 예측 (지평 18일)"
            },
            {
              "source": "시세관측",
              "claim": "가락 당일 경락가 1,850원/kg 등 3개 등급",
              "ref_id": "MQ-가락-2026-09-04",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "가락시장 등급별 당일 실측 (mock)"
            },
            {
              "source": "주문",
              "claim": "확정주문 18,000kg → 일평균 1,286kg × D=5",
              "ref_id": "SO-2026-09-04",
              "evidence_grade": "ASSUMED",
              "evidence_detail": "확정주문에서 파생한 일평균 — 수요 파생값이라 SIM_FIXED 자격 없음"
            },
            {
              "source": "재고",
              "claim": "가용 3,000kg (로트 12)",
              "ref_id": "INV-12",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "inventory_lots 스냅샷 (mock)"
            },
            {
              "source": "현금",
              "claim": "재무 매입 상한 20,000,000원까지 매입 가능",
              "ref_id": "CASH-2026-09-04",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)"
            },
            {
              "source": "문서ID",
              "claim": "KREI 관측월보 — 농업관측 8월호 — 배추",
              "ref_id": "DOC-3",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "2026-08-05 발행 · 발췌: \"고랭지 배추 정식면적은 전년 대비 6% 감소한 것으로 조사됐다.\""
            },
            {
              "source": "문서ID",
              "claim": "기상청 기상 — 1개월 전망 — 8월 하순~9월 상순",
              "ref_id": "DOC-4",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "2026-08-10 발행 · 발췌: \"8월 하순 강수량은 평년보다 많겠다.\""
            },
            {
              "source": "문서ID",
              "claim": "aT 작년동기 — 작년 동기 가락시장 배추 경락가 요약",
              "ref_id": "DOC-5",
              "evidence_grade": "SIM_FIXED",
              "evidence_detail": "2026-08-01 발행 · 발췌: \"작년 8월 하순 가락시장 배추 상품 경락가는 kg당 1,480원으로 8월 상순 대비 9% 상승했다.\""
            }
          ],
          "risks": [
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)",
            "기존 로트 12 잔여신선도 6일 — 신규 매입분이 이 로트를 밀어내지 않는지 확인 필요",
            "문서 3건 참조 — 규칙 기반 수집이라 문서 선별·충분성 판단은 미적용(우선순위 순서대로 로드). 발췌는 관련 구절 선별 없이 각 문서 서두에서 기계적으로 뜬 것이다"
          ]
        }
      ],
      "reasoning": "보수·기본 안을 냈다. 예측 구간이 넓어 공격안은 만들지 않았다. 열린 전략축은 quantity이다.",
      "evidences": [
        {
          "claim": "situation",
          "source": "tool_calc",
          "ref_ids": [
            "배추-CI-2026-09-04"
          ],
          "value": 0.120047,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "D+14 구간폭을 임계 0.08와 비교해 uncertain 판정"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-CI-2026-09-04"
          ],
          "value": 0.120047,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "구간폭 0.120 ≥ 0.08 → uncertain → 선매입 궤적 차단"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-VOL-2026-09-04"
          ],
          "value": 15429.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "추정 총량 15,429kg < 임계 20,000kg → 총량 트리거 미달"
        },
        {
          "claim": "allowed_axes",
          "source": "tool_calc",
          "ref_ids": [
            "배추-MIX-2026-09-04"
          ],
          "value": 0.812,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "품목 편중 최대 0.812 ≥ 0.7 → mix 제외"
        },
        {
          "claim": "scenarios",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SCEN-2026-09-04"
          ],
          "value": 2.0,
          "unit": "count",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "uncertain 판정에서 파생 — 불확실이면 공격안을 만들지 않아 두 안이 된다"
        },
        {
          "claim": "scenarios[0].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-09-04"
          ],
          "value": 2.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[0].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-09-04"
          ],
          "value": 2571.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[0].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-09-04"
          ],
          "value": 4242150.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[0].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-09-04"
          ],
          "value": 1794.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[0].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC0-2026-09-04"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "보수안 — (contract_price − 가중 매입단가) ÷ contract_price"
        },
        {
          "claim": "scenarios[1].coverage_days",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-09-04"
          ],
          "value": 5.0,
          "unit": "days",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — constraints.coverage_days.by_label — 안별 커버일수 매핑"
        },
        {
          "claim": "scenarios[1].total_qty_kg",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-09-04"
          ],
          "value": 6429.0,
          "unit": "kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — 일평균 확정수요 × 커버일수, 하드 제약(창고·현금·신선도)으로 클립"
        },
        {
          "claim": "scenarios[1].total_amount_krw",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-09-04"
          ],
          "value": 10607850.0,
          "unit": "KRW",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — Σ(sourcing_plan[].qty_kg × grade_unit_price) — 등급 배분에서 파생"
        },
        {
          "claim": "scenarios[1].max_price",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-09-04"
          ],
          "value": 1861.0,
          "unit": "KRW/kg",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — 커버 구간 예측 상단(q90)의 최대값"
        },
        {
          "claim": "scenarios[1].expected_margin_rate",
          "source": "tool_calc",
          "ref_ids": [
            "배추-SC1-2026-09-04"
          ],
          "value": 0.2804186655037069,
          "unit": "ratio",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "기본안 — (contract_price − 가중 매입단가) ÷ contract_price"
        },
        {
          "claim": "context_docs_used",
          "source": "documents",
          "ref_ids": [
            "DOC-3",
            "DOC-4",
            "DOC-5"
          ],
          "value": 3.0,
          "unit": "count",
          "evidence_grade": "SIM_FIXED",
          "evidence_detail": "우선순위 목록을 소진할 때까지 읽었다 — 문서 선별·충분성 판단은 적용되지 않았다"
        }
      ],
      "run_id": "PUR-RUN-REQ-2026-09-04-배추-1",
      "used_tools": [
        "assess_market_situation",
        "collect_market_context",
        "draft_purchase_quantities",
        "plan_split_purchase",
        "allocate_grade_mix",
        "compose_and_verify_scenarios"
      ],
      "missing_data": []
    },
    "boundary": {
      "finance": {
        "base_projected_cash_min": 24000000,
        "margin_defense_floor_rate": 0.267,
        "finance_cap_amount_krw": 20000000,
        "purchase_payment_days": 7,
        "critical_payment_dates": []
      },
      "inventory": {
        "as_of": "2026-09-04",
        "item": "배추",
        "warehouse_free_kg": 12000,
        "rental_cap_kg": 3600,
        "lot_count": 1,
        "first_lot": {
          "lot_id": 12,
          "grade": "상",
          "stocked_at": "2026-08-31",
          "remaining_kg": 3000,
          "shelf_life_days": 10
        }
      }
    },
    "financeCap": 20000000,
    "blurb": "불확실 · 2안"
  }
} as const;

/** 시연에서 오갈 기준일. 12-31 앵커는 mock 에 아직 없다 (이슈 #73). */
export const DEMO_DATES = Object.keys(SCENES);
