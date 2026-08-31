"""번인 조회 — **에이전트가 판단하기 전 30일**.

★ **DB 를 타지 않는다.** 저장소만 갈아끼운다 — DB 가 있어야 도는 검사는 아무도
  안 돌린다 (매입 8/31 회신 §5 와 같은 이유다).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.master import service
from app.master.schemas import BurnInOut


def _raw(**over):
    base = {
        "run": {
            "sim_run_id": "SIM-BURNIN-202512",
            "run_type": "BURN_IN",
            "period_start": date(2025, 12, 2),
            "period_end": date(2025, 12, 31),
            "as_of": date(2025, 12, 31),
            "status": "SEEDED",
            "financing_mode": "LOAN_BASELINE",
            "note": "Agent 실행 전 30일 Persona 이력",
        },
        "closings": [
            {
                "close_date": date(2025, 12, 2),
                "day_no": 1,
                "base_cash_balance_krw": 58198631.47,
                "loan_cash_balance_krw": None,
                "receivables_balance_krw": 0.0,
                "inventory_qty_kg": 375.4,
                "sales_recognized_krw": 0.0,
                "collection_cash_in_krw": 0.0,
                "purchase_cash_out_krw": 0.0,
                "closed": True,
            },
            {
                "close_date": date(2025, 12, 31),
                "day_no": 30,
                "base_cash_balance_krw": -13278190.42,
                "loan_cash_balance_krw": None,
                "receivables_balance_krw": 73051531.0,
                "inventory_qty_kg": 375.4,
                "sales_recognized_krw": 4459888.0,
                "collection_cash_in_krw": 0.0,
                "purchase_cash_out_krw": 0.0,
                "closed": True,
            },
        ],
    }
    base.update(over)
    return base


def test_번인을_화면_형태로_옮긴다(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "get_burn_in", lambda: _raw())

    out = service.get_burn_in_history()

    assert isinstance(out, BurnInOut)
    assert out.as_of == date(2025, 12, 31), "에이전트가 처음 판단하는 날"
    assert len(out.closings) == 2
    assert out.closings[-1].base_cash_balance_krw == -13278190.42


def test_값을_만들지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 합계·증감을 여기서 계산하면 **재무가 내는 숫자와 갈릴 자리**가 생긴다.

    받은 것을 모양만 바꾼다.
    """
    monkeypatch.setattr(service, "get_burn_in", lambda: _raw())

    out = service.get_burn_in_history()

    assert not hasattr(out, "total"), "합계 칸을 두지 않는다"
    assert not hasattr(out, "delta"), "증감 칸을 두지 않는다"
    assert [c.day_no for c in out.closings] == [1, 30], "순서를 바꾸거나 채우지 않는다"


def test_마감되지_않은_날을_지우지_않는다(monkeypatch: pytest.MonkeyPatch):
    """섞여 있으면 **그 사실이 답의 일부**다 — 화면이 적을 수 있어야 한다."""
    raw = _raw()
    raw["closings"][1]["closed"] = False
    monkeypatch.setattr(service, "get_burn_in", lambda: raw)

    out = service.get_burn_in_history()

    assert [c.closed for c in out.closings] == [True, False]


def test_없으면_LookupError_가_그대로_올라간다(monkeypatch: pytest.MonkeyPatch):
    """라우터가 404 로 옮긴다 — 여기서 빈 값으로 덮으면 **없는 것이 0 으로 보인다.**"""

    def missing():
        raise LookupError("시뮬레이션을 찾을 수 없습니다: SIM-NOPE")

    monkeypatch.setattr(service, "get_burn_in", missing)

    with pytest.raises(LookupError, match="SIM-NOPE"):
        service.get_burn_in_history()
