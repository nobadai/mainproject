"""ML 예측 계약 검사.

각 검사는 "계약의 어느 조항이 코드로 지켜지는가" 에 1:1 로 대응한다.
정상 픽스처 하나를 만들고 조항별로 한 필드씩 깨뜨려 거부되는지 확인한다.

계약의 출처는 ``purchase_agent/ports.py::get_forecast`` 의 반환 형태
(IO명세 §1-①) 다. 그쪽 mock 과 같은 값을 쓰면 다음 단계에서 그대로
가져다 쓸 수 있다.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.ml.schemas import HORIZON_DAYS, ITEMS, SPEC, DailyPoint, Forecast

KST = timezone(timedelta(hours=9))
AS_OF = date(2026, 8, 26)


def _daily(n: int = HORIZON_DAYS, *, start: int = 1) -> list[DailyPoint]:
    return [
        DailyPoint(
            date=AS_OF + timedelta(days=offset),
            predicted=610 + offset,
            lower=395 + offset,
            upper=831 + offset,
        )
        for offset in range(start, start + n)
    ]


def _forecast(**overrides) -> Forecast:
    payload = {
        "as_of": AS_OF,
        "item": "배추",
        "target_kind": "AUC",
        "unit": "원/kg",
        "current_price": 610,
        "horizon_days": HORIZON_DAYS,
        "model_version": "ops_auc",
        "generated_at": datetime(2026, 8, 26, 6, 0, tzinfo=KST),
        "daily": _daily(),
    }
    payload.update(overrides)
    return Forecast(**payload)


def test_정상_픽스처가_통과한다():
    forecast = _forecast()
    assert len(forecast.daily) == HORIZON_DAYS
    assert forecast.daily[0].date == AS_OF + timedelta(days=1)
    assert forecast.daily[-1].date == AS_OF + timedelta(days=HORIZON_DAYS)


def test_daily_는_연속_달력일이어야_한다():
    """우리 내부는 개장일 축이다. 변환을 빠뜨리면 날짜가 띄엄띄엄해진다.

    그러면 판정 기준일(D+14)이 조용히 밀린다 — 가장 위험한 실패다.
    """
    broken = _daily()
    broken[5] = DailyPoint(
        date=AS_OF + timedelta(days=9),  # 6 이어야 하는데 9
        predicted=610, lower=395, upper=831,
    )
    with pytest.raises(ValidationError, match="연속 달력일"):
        _forecast(daily=broken)


def test_daily_는_18건이어야_한다():
    with pytest.raises(ValidationError, match="18건"):
        _forecast(daily=_daily(17))


def test_구간이_뒤집히면_거부한다():
    with pytest.raises(ValidationError, match="구간이 뒤집"):
        DailyPoint(date=AS_OF + timedelta(days=1), predicted=610, lower=900, upper=831)


def test_예측가가_구간_밖이면_거부한다():
    with pytest.raises(ValidationError, match="구간이 뒤집"):
        DailyPoint(date=AS_OF + timedelta(days=1), predicted=1200, lower=395, upper=831)


def test_음수_가격을_거부한다():
    with pytest.raises(ValidationError):
        DailyPoint(date=AS_OF + timedelta(days=1), predicted=-1, lower=1, upper=2)


def test_미래에_만든_예측을_거부한다():
    """generated_at 이 as_of 보다 나중이면 미래 정보를 쓴 것이다.

    백테스트 성적을 부풀리는 가장 흔한 실수라 계약이 직접 막는다.
    """
    with pytest.raises(ValidationError, match="나중입니다"):
        _forecast(generated_at=datetime(2026, 8, 27, 6, 0, tzinfo=KST))


def test_같은_날_만든_예측은_허용한다():
    assert _forecast(generated_at=datetime(2026, 8, 26, 23, 59, tzinfo=KST))


def test_계약에_없는_필드를_거부한다():
    with pytest.raises(ValidationError):
        _forecast(unknown_field="x")


def test_가격종류는_셋뿐이다():
    for kind in ("AUC", "WHSL", "RTL"):
        assert _forecast(target_kind=kind)
    with pytest.raises(ValidationError):
        _forecast(target_kind="UNKNOWN")


def test_규격_정보가_세_가격종류에_모두_있다():
    """매입 파트가 "어느 규격·등급으로 환산했는지 명시해달라" 고 요청했다."""
    for kind in ("AUC", "WHSL", "RTL"):
        assert SPEC[kind]["market"]
        assert SPEC[kind]["grade"]


def test_경락가_규격이_품목마다_지정돼_있다():
    """경락가는 포장 규격마다 가격이 크게 다르다.

    배추 특등급 하루치에 그물망 10kg(711원/kg)부터 1kg 소포장(11,224원/kg)
    까지 섞여 있었다. 규격을 고정하고서야 예측이 가능해졌다.
    """
    for item in ITEMS:
        assert SPEC["AUC"]["desc"][item], f"{item} 규격 설명이 없습니다"
        assert SPEC["AUC"]["kg"][item] > 0, f"{item} 규격 중량이 없습니다"


def test_지원_품목은_셋이다():
    """마늘은 원자료 품질 문제로 아직 제외다 (경락가 결측 11%)."""
    assert ITEMS == ("배추", "무", "양파")
