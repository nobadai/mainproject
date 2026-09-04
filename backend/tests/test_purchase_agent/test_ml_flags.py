"""ML 신뢰도 플래그 넷을 **읽는다** (#213 · ML 회신 2026-08-27).

넷이 같은 층이 아니라 처리도 넷이다::

    gate_reason       quality 계열이면 그 행을 뺀다 · lead_time·None 이면 쓴다
    use_recommended   False 면 판정 자체를 안 한다 (어댑터가 앞에서 막는다)
    is_gated          🔴 **안 본다** — ML 이 "다른 축"(ⓒ)이라 확정했다
    is_filled         판정에 안 쓰고 max_price 창에 섞이면 고지만 붙인다

🔴 **지금 AUC 에는 ``quality`` 가 0건이라 이 검사들은 합성 입력으로 잰다.** 실데이터로
는 아무것도 안 걸리는데(3품목 × 7배치 = 21조합 전부 통과), 그건 *"검사가 무의미하다"* 가
아니라 *"오늘의 데이터가 그렇다"* 다. WHSL 에는 ``quality`` 가 101건 있고 계열이 느는
날 걸린다 — 그날 검사가 없으면 아무도 모른다.

⚠️ **``is_gated`` 로 걸렀으면 터졌다는 것을 검사로 남긴다** (아래 변이 시험).
  실측상 보수(D=2) 창은 21조합 전부 100% gated 라, 표시만 보고 걸렀다면
  ``max_price`` 가 통째로 사라졌다. 지나간 실수가 아니라 **다음 사람이 하기 쉬운
  실수**라 잠근다.
"""

from datetime import date, timedelta

import pytest

from app.purchase_agent.adapter import validate_forecast
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.classify_situation import is_gate_excluded
from app.purchase_agent.nodes.package_scenarios import (
    _forecast_risks,
    compute_max_price,
    usable_forecast_window,
)

AS_OF = date(2026, 8, 21)


def _daily(rows: dict[int, dict] | None = None, count: int = 18) -> list[dict]:
    """D+1~D+N 예측. ``rows`` 는 ``{offset: {덮어쓸 키}}`` — 나머지는 평범한 행이다."""
    rows = rows or {}
    out = []
    for offset in range(1, count + 1):
        row = {
            "date": (AS_OF + timedelta(days=offset)).isoformat(),
            "predicted": 700 + offset,
            "lower": 600,
            "upper": 800 + offset,  # offset 이 클수록 상단이 높다 — 최댓값 자리를 안다
        }
        row.update(rows.get(offset, {}))
        out.append(row)
    return out


def _forecast(**over) -> dict:
    base = {
        "generated_at": f"{AS_OF.isoformat()}T06:00:00+09:00",
        "item": "배추",
        "unit": "원/kg",
        "current_price": 700,
        "horizon_days": 18,
        "daily": _daily(),
        "model_version": "ops_auc",
    }
    base.update(over)
    return base


# ── ① gate_reason — quality 는 빼고 lead_time 은 쓴다 ──────────────────────


@pytest.mark.parametrize(
    "reason, excluded",
    [
        ("quality", True),
        ("lead_time+quality", True),  # 🔴 복합값. == 비교로는 못 잡는다
        ("lead_time", False),  # ML: "값이 나쁜 게 아니다"
        (None, False),
        ("", False),
    ],
)
def test_only_quality_reasons_are_excluded(reason: str | None, excluded: bool) -> None:
    assert is_gate_excluded({"gate_reason": reason}) is excluded


def test_a_row_with_no_gate_column_at_all_is_kept() -> None:
    """mock 예측에는 이 칸이 아예 없다 — 없는 것을 제외 사유로 읽으면 4앵커가 죽는다."""
    assert is_gate_excluded({"predicted": 700}) is False


def test_is_gated_alone_never_excludes_a_row() -> None:
    """🔴 ML 이 "다른 축"이라 했다. 표시만 보고 빼면 lead_time 구간이 통째로 날아간다."""
    assert is_gate_excluded({"is_gated": True, "gate_reason": "lead_time"}) is False


def test_quality_rows_drop_out_of_the_max_price_window() -> None:
    forecast = _forecast(daily=_daily({1: {"upper": 9_999, "gate_reason": "quality"}}))
    assert compute_max_price(forecast, 5) == 805  # offset 5. 9,999 는 빠졌다
    assert all(row["upper"] != 9_999 for row in usable_forecast_window(forecast, 5))


def test_a_compound_reason_drops_out_too() -> None:
    """``== "quality"`` 로 짜면 여기서 운다 — 표에 이 값이 25건 있다."""
    forecast = _forecast(daily=_daily({1: {"upper": 9_999, "gate_reason": "lead_time+quality"}}))
    assert compute_max_price(forecast, 5) == 805


def test_lead_time_rows_stay_in_the_window() -> None:
    """🔴 **여기가 핵심이다.** lead_time 을 빼면 실데이터에서 max_price 가 사라진다."""
    forecast = _forecast(daily=_daily({1: {"upper": 9_999, "gate_reason": "lead_time"}}))
    assert compute_max_price(forecast, 5) == 9_999


def test_the_window_can_be_emptied_and_says_so() -> None:
    """창 전체가 quality 면 상한을 못 정한다 — 조용한 ``max() arg is empty`` 를 막는다."""
    gated = {offset: {"gate_reason": "quality"} for offset in (1, 2)}
    with pytest.raises(ValueError, match="품질 게이트"):
        compute_max_price(_forecast(daily=_daily(gated)), 2)


# ── ② use_recommended=False → 판정 자체를 안 한다 ──────────────────────────


def test_use_recommended_false_blocks_the_run() -> None:
    missing = validate_forecast(_forecast(use_recommended=False), AS_OF)
    assert "forecast.use_recommended" in missing


def test_use_recommended_true_or_absent_passes() -> None:
    """🔴 ``None`` 은 안 건다 (규칙 3) — mock 에는 이 칸이 아예 없다."""
    assert validate_forecast(_forecast(use_recommended=True), AS_OF) == []
    assert validate_forecast(_forecast(), AS_OF) == []
    assert validate_forecast(_forecast(use_recommended=None), AS_OF) == []


def test_a_quality_gated_judgment_row_blocks_the_run() -> None:
    """판정일 한 줄로 ci_width 를 재므로, 그 줄이 quality 면 판정이 성립하지 않는다."""
    day = load_constraints()["situation"]["ci_judgment_day"]
    forecast = _forecast(daily=_daily({day: {"gate_reason": "quality"}}))
    assert "forecast.daily.gate_reason" in validate_forecast(forecast, AS_OF)


def test_a_lead_time_judgment_row_does_not_block() -> None:
    day = load_constraints()["situation"]["ci_judgment_day"]
    forecast = _forecast(daily=_daily({day: {"gate_reason": "lead_time"}}))
    assert validate_forecast(forecast, AS_OF) == []


def test_the_shortest_coverage_window_is_guarded_at_the_door() -> None:
    """가장 짧은 창이 전부 quality 면 어느 안도 상한을 못 정한다 — 노드 앞에서 막는다."""
    shortest = min(load_constraints()["coverage_days"]["by_label"].values())
    gated = {offset: {"gate_reason": "quality"} for offset in range(1, shortest + 1)}
    forecast = _forecast(daily=_daily(gated))
    assert "forecast.daily.gate_reason" in validate_forecast(forecast, AS_OF)


def test_the_guard_reads_the_declaration_not_a_hard_coded_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 규칙 8 — 선언을 바꾸면 판정이 따라 바뀌는가.

    창을 하나 넓히면 **더 많은 행이 quality 여야** 막힌다. 코드가 ``2`` 를 박고 있으면
    넓힌 창의 세 번째 행을 안 보므로 여기서 운다.
    """
    from app.purchase_agent import adapter

    real = load_constraints()
    widened = {
        **real,
        "coverage_days": {
            **real["coverage_days"],
            "by_label": {**real["coverage_days"]["by_label"], "보수": 3},
        },
    }
    monkeypatch.setattr(adapter, "load_constraints", lambda: widened)

    two_gated = {offset: {"gate_reason": "quality"} for offset in (1, 2)}
    assert validate_forecast(_forecast(daily=_daily(two_gated)), AS_OF) == []

    three_gated = {offset: {"gate_reason": "quality"} for offset in (1, 2, 3)}
    assert "forecast.daily.gate_reason" in validate_forecast(
        _forecast(daily=_daily(three_gated)), AS_OF
    )


# ── ③ is_filled — 고지만, 그것도 최댓값 행일 때만 ──────────────────────────


def test_a_copied_row_that_sets_the_ceiling_gets_a_notice() -> None:
    forecast = _forecast(daily=_daily({2: {"upper": 9_999, "is_filled": True}}))
    risks = _forecast_risks(forecast, 5)
    assert len(risks) == 1
    assert "9999원" in risks[0] and "장이 서지 않아" in risks[0]


def test_a_copied_row_that_does_not_set_the_ceiling_stays_quiet() -> None:
    """🔴 창 전체를 세면 실측 48/63 에 붙는다 — 매일 붙는 줄은 신호가 아니다."""
    forecast = _forecast(daily=_daily({1: {"upper": 100, "is_filled": True}}))
    assert _forecast_risks(forecast, 5) == []


def test_no_copy_column_means_no_notice() -> None:
    """mock 4앵커가 이 경로다 — 없는 칸을 ``false`` 로 읽지 않는다 (규칙 3)."""
    assert _forecast_risks(_forecast(), 5) == []


def test_the_notice_never_cuts_a_scenario() -> None:
    """컷이 아니라 고지다 — ``is_filled`` 는 틀린 값이 아니라 그날 장이 안 선 사실이다."""
    forecast = _forecast(daily=_daily({2: {"upper": 9_999, "is_filled": True}}))
    assert compute_max_price(forecast, 5) == 9_999


# ── 변이 시험 — 틀리게 짰으면 우는가 ──────────────────────────────────────


def test_filtering_by_is_gated_would_wipe_out_the_ceiling() -> None:
    """🔴 실측 재현: AUC 보수(D=2) 창은 전부 lead_time gated 다.

    ``is_gated`` 로 걸렀다면 이 창에 남는 행이 0개가 된다. 우리 기준(``gate_reason``)
    으로는 그대로 산다 — **두 방식이 갈리는 지점을 검사가 들고 있는다.**
    """
    lead_gated = {
        offset: {"is_gated": True, "gate_reason": "lead_time"} for offset in (1, 2)
    }
    forecast = _forecast(daily=_daily(lead_gated))

    assert [row for row in forecast["daily"][:2] if not row["is_gated"]] == []
    assert compute_max_price(forecast, 2) == 802


def test_an_equality_comparison_would_miss_the_compound_reason() -> None:
    """``reason == "quality"`` 로 짜면 이 값이 통과한다 — 표에 25건 있다."""
    row = {"gate_reason": "lead_time+quality"}
    assert row["gate_reason"] != "quality"
    assert is_gate_excluded(row) is True
