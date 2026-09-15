"""마스터가 만드는 예측 payload 가 **ML 계약을 그대로 통과하는가** (2026-09-11).

🔴 **걷기 206일에서 판매 537건이 이 자리에서 죽었다** (`SIM-WALK-2026-APPROVED`).

```text
판매는 `app.ml.schemas.Forecast` 를 **그대로** 쓴다 (`sales/schemas.py:459`)
그 모델은 `as_of` · `target_kind` 를 **필수**로 두고 `extra="forbid"` 다
마스터의 `_forecast_payload` 는 그 둘을 **버리고** 있었다
→ 봉투가 통째로 거부되고 `SL2_NO_CANDIDATE`(안을 못 만들었다)로 적혔다
```

★★ **매입은 안 아팠다.** 그 둘을 안 읽기 때문이다. 같은 값이 한 파트는 통과하고
  한 파트는 막히는데, 막힌 쪽 사유가 *"안을 못 만들었다"* 라 **원인이 안 보였다.**

★ `use_recommended` 를 더했던 `#192`(2026-09-03)와 **같은 모양**이다 — 뷰는 주는데
  고르는 자리에서 버렸다.
"""

from __future__ import annotations

import datetime
import unicodedata

import pytest

from app.master.inputs import _forecast_payload
from app.ml.schemas import Forecast

#: 뷰가 실제로 돌려주는 행. `v_ml_price_forecast` 실측(2026-09-11)을 본뜬다.
_뷰행 = {
    "as_of": datetime.date(2025, 12, 31),
    "item": "무",
    "target_kind": "AUC",
    "unit": "원/kg",
    "current_price": 650,
    "horizon_days": 2,
    "model_version": "v1",
    "generated_at": datetime.datetime(2025, 12, 31, 9, 0, tzinfo=datetime.UTC),
    "daily": [
        {
            "date": datetime.date(2026, 1, 1),
            "predicted": 650,
            "lower": 528,
            "upper": 795,
            "is_filled": True,
            "is_gated": True,
            "gate_reason": "lead_time",
        },
        {
            "date": datetime.date(2026, 1, 2),
            "predicted": 655,
            "lower": 530,
            "upper": 800,
            "is_filled": False,
            "is_gated": False,
            "gate_reason": None,
        },
    ],
    "use_recommended": True,
}


def test_ML_계약이_요구하는_두_칸을_나른다() -> None:
    """🔴 **이 둘이 537건을 죽인 값이다.**"""
    실린것 = _forecast_payload(dict(_뷰행))

    assert 실린것["as_of"] == _뷰행["as_of"]
    assert 실린것["target_kind"] == _뷰행["target_kind"]


def test_만든_payload_가_ML_계약을_그대로_통과한다() -> None:
    """★★ **칸 이름을 세는 것으로는 부족하다.**

    판매는 이 payload 를 `Forecast` 로 **파싱**한다. 칸이 있어도 모양이 다르면
    거기서 막히고, 그 실패는 *"안을 못 만들었다"* 로 적혀 **원인이 안 보인다.**
    그래서 여기서 실제로 파싱한다.
    """
    통과한것 = Forecast.model_validate(_forecast_payload(dict(_뷰행)))

    assert 통과한것.as_of == _뷰행["as_of"]
    assert 통과한것.target_kind == "AUC"
    assert len(통과한것.daily) == 2


def test_daily_의_신뢰도_셋이_붙어도_막히지_않는다() -> None:
    """🔴 뷰가 `is_filled` · `is_gated` · `gate_reason` 을 **이미 싣는다.**

    ★ 마스터는 `daily` 를 **손대지 않고 그대로 나른다** — 풀어 다시 조립하면 ML 이
      준 모양이 바뀐다. 그러니 계약 쪽에 칸이 있어야 한다.
    """
    통과한것 = Forecast.model_validate(_forecast_payload(dict(_뷰행)))

    assert 통과한것.daily[0].is_gated is True
    assert 통과한것.daily[0].gate_reason == "lead_time"
    assert 통과한것.daily[1].is_gated is False


def test_신뢰도_셋이_없어도_통과한다() -> None:
    """⚠️ **「안 온 것」과 「거짓인 것」은 다르다.**

    셋을 필수로 만들면 이 셋을 안 싣는 옛 경로가 거꾸로 막힌다.
    """
    행 = dict(_뷰행)
    행["daily"] = [
        {k: v for k, v in point.items() if k not in ("is_filled", "is_gated", "gate_reason")}
        for point in _뷰행["daily"]
    ]

    통과한것 = Forecast.model_validate(_forecast_payload(행))

    assert 통과한것.daily[0].is_gated is None
    assert 통과한것.daily[0].gate_reason is None


def test_두_칸_중_하나라도_빠지면_계약이_막는다() -> None:
    """★ 이 검사가 **없으면** 위 검사들이 「칸을 지웠는데도 통과」를 못 잡는다."""
    for 뺄칸 in ("as_of", "target_kind"):
        실린것 = _forecast_payload(dict(_뷰행))
        del 실린것[뺄칸]

        with pytest.raises(Exception) as 터진것:
            Forecast.model_validate(실린것)

        assert 뺄칸 in unicodedata.normalize("NFC", str(터진것.value))
