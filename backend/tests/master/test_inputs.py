"""마스터가 실어 주는 입력 3종 — **출처가 값과 함께 다니는지**를 잠근다.

★ **DB 를 타지 않는다.** 조회 함수를 갈아 끼운다.

이 파일이 지키는 명제는 하나다 — **mock 에서 온 값이 실측처럼 보이면 안 된다.**
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.master import inputs

AS_OF = date(2025, 12, 31)

FORECAST_ROW = {
    "as_of": date(2026, 8, 27),
    "item": "배추",
    "target_kind": "AUC",
    "generated_at": "2026-08-27T06:00:00+09:00",
    "unit": "원/kg",
    "current_price": 645,
    "horizon_days": 18,
    "daily": [{"date": "2026-08-28", "predicted": 645}],
    "model_version": "ops_auc",
    "quality_note": "세 구간 모두 양수",
}

DEMAND_ROWS = [
    {"item_name": "배추", "daily_demand_kg": Decimal("717.300")},
    {"item_name": "무", "daily_demand_kg": Decimal("154.400")},
]


def patch(monkeypatch, *, one=None, many=None):
    monkeypatch.setattr(inputs, "fetch_one", one or (lambda *a: None))
    monkeypatch.setattr(inputs, "fetch_all", many or (lambda *a: []))
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")


# ── forecast ────────────────────────────────────────────────────────────


def test_실_DB_에서_읽으면_MEASURED_다(monkeypatch):
    patch(monkeypatch, one=lambda *a: FORECAST_ROW)
    got = inputs.load_forecast("배추", AS_OF)

    assert got.grade == "MEASURED"
    assert got.payload["current_price"] == 645
    assert "v_ml_price_forecast" in got.source
    # 뷰 행을 그대로 옮기고 값은 손대지 않는다
    assert got.payload["daily"] == FORECAST_ROW["daily"]


def test_예측_배치가_없으면_비운다_mock_으로_안_메운다(monkeypatch):
    """🟢 **뒤집힌 검사다** (2026-09-03).

    전에는 `MOCK` 으로 떨어지고 *"그래도 관통은 시킨다"* 였다. 그 갈래가 ML DB
    장애를 정상 실행으로 보이게 했고, 마스터 실측을 두 번 오염시켰다.

    ★ 이제 비운다. 매입이 `missing_data: ["forecast"]` 로 답한다.
    """
    patch(monkeypatch)  # fetch_one → None
    got = inputs.load_forecast("배추", AS_OF)

    assert got.grade == "MISSING"
    assert got.payload is None, "못 읽었는데 값이 있다 — mock 다리가 다시 생겼다"
    assert "예측 배치가 없다" in got.note


def test_DB_가_터져도_Flow_를_죽이지_않는다(monkeypatch):
    def boom(*a):
        raise RuntimeError("커넥션 없음")

    patch(monkeypatch, one=boom)
    got = inputs.load_forecast("배추", AS_OF)

    # ⚠️ 전에는 {"MOCK", "MISSING"} 둘 다 받았다. 그 느슨함이 mock 다리를 가렸다.
    assert got.grade == "MISSING"
    assert got.payload is None
    assert "커넥션 없음" in got.note, "왜 못 읽었는지가 안 남는다"


# ── confirmed_orders ────────────────────────────────────────────────────


def test_실제_납품_예정이_있으면_MEASURED_다(monkeypatch):
    rows = [{"sale_id": 7, "sale_date": date(2026, 1, 3), "quantity_kg": 12000}]
    patch(monkeypatch, many=lambda *a: rows)
    got = inputs.load_confirmed_orders("배추", AS_OF)

    assert got.grade == "MEASURED"
    assert got.payload["total_kg"] == 12000
    assert got.payload["orders"][0]["due_date"] == "2026-01-03"


def test_납품_예정이_없으면_수요에서_파생하되_확정이라_부르지_않는다(monkeypatch):
    """🔴 **여기가 이 모듈의 핵심이다.**

    파생값을 "확정 주문" 으로 넘기면 매입도 사람도 확정으로 읽는다. 등급과 파생식을
    함께 실어 리포트에 드러낸다.
    """
    demand = {
        "daily_demand_kg": Decimal("717.300"),
        "demand_basis": "통합 Persona v1.2 적용 일수요",
        "provisional": True,
    }
    calls = iter([demand, {"order_cycle_days": 2}])
    patch(monkeypatch, one=lambda *a: next(calls), many=lambda *a: [])
    got = inputs.load_confirmed_orders("배추", AS_OF)

    assert got.grade == "DERIVED"
    assert "확정 주문이 아니다" in got.note
    assert "잠정값" in got.note
    assert got.payload["total_kg"] == pytest.approx(717.3 * 14, rel=1e-3)
    # 총량 한 덩어리로 주면 "전량 첫날 납품" 으로 읽힌다 — 주기로 쪼갠다
    assert len(got.payload["orders"]) == 7
    assert all(o["sale_id"] is None for o in got.payload["orders"])  # id 를 지어내지 않는다


def test_수요도_없으면_지어내지_않고_비운다(monkeypatch):
    patch(monkeypatch)
    got = inputs.load_confirmed_orders("배추", AS_OF)

    assert got.grade == "MISSING"
    assert got.payload is None


# ── policy_values ───────────────────────────────────────────────────────


def test_품목_비중은_수요에서_파생한다(monkeypatch):
    patch(monkeypatch, many=lambda *a: DEMAND_ROWS)
    got = inputs.load_policy_values("배추", AS_OF)

    assert got.grade == "DERIVED"
    ratios = got.payload["item_mix_ratio"]
    assert ratios["배추"] == pytest.approx(717.3 / (717.3 + 154.4), abs=1e-4)
    assert sum(ratios.values()) == pytest.approx(1.0, abs=1e-3)


def test_계약_단가는_평균으로_메우지_않는다(monkeypatch):
    """없으면 `margin_warning` 이 `null` 로 나가는 것이 **정상 경로**다."""
    patch(monkeypatch, many=lambda *a: DEMAND_ROWS)
    got = inputs.load_policy_values("배추", AS_OF)

    assert "contract_price_krw" not in got.payload
    assert "DB 에 없어 비움" in got.note


def test_수요_행이_없으면_비중을_지어내지_않는다(monkeypatch):
    patch(monkeypatch)
    got = inputs.load_policy_values("배추", AS_OF)

    assert got.grade == "MISSING"


# ── 출처표 ──────────────────────────────────────────────────────────────


def test_이제_mock_에서_오는_것이_없다(monkeypatch):
    """🔴 **만드는 곳이 사라졌다** (2026-09-03).

    `MOCK` 은 어휘에 남지만 마스터 적재층이 그것을 만들지 않는다.
    새 다리가 생기면 여기가 운다 — 그리고 `ProcurementFlow` 가 실행을 세운다.
    """
    patch(monkeypatch, many=lambda *a: DEMAND_ROWS)
    got = inputs.collect_inputs("배추", AS_OF)

    assert got.mocked == (), f"mock 다리가 다시 생겼다: {got.mocked}"
    assert got.sources()["forecast"].startswith("MISSING:")
    assert got.sources()["policy_values"].startswith("DERIVED:")


def test_Decimal_은_정수면_정수로_남는다():
    """매입 계약이 정수를 받는 자리가 있어, 무조건 float 로 바꾸면 거기서 터진다."""
    assert inputs._plain(Decimal(8000)) == 8000
    assert isinstance(inputs._plain(Decimal(8000)), int)
    assert inputs._plain(Decimal("717.3")) == pytest.approx(717.3)
