"""판매 확정이 **그 자리에서 재고를 잡는다** — 같은 재고를 두 번 팔았다 (2026-09-12).

완주한 걷기 `SIM-CHAIN-V4` 실측이다.

```text
판단일    물류가 준 가용량   판매가 판 양   결과
01-06        1,435           1,435      출고완료
01-07        3,587           3,587      🔴 미출고   ← 01-06 확정분 1,435 이 아직 안
                                                      나갔는데 전량 가용으로 보고됐다
02-09        2,869           2,869      출고완료
02-10        2,870           2,870      🔴 미출고
```

🟢 **판매는 잘못이 없다.** 물류가 준 값을 여덟 건 전부 한 자리도 안 틀리고 썼다.

🔴 **확정됐지만 아직 안 나간 판매가 아무것도 안 잡았다.**

```text
확정 (D일)   sales 행이 CONFIRMED 로 선다 · 예약이 없었다
출고 (D+1)   ship_due_sales 가 그때서야 reserve 를 불렀다
그 사이      available_qty_kg = remaining_qty_kg − held_qty_kg 인데 held 가 안 올라
             **같은 재고가 다음 날 또 팔렸다**
```

★★ **물류가 이 위험을 이미 적어 뒀다** — `app/logistics/outbound.py` 의
  `item_free_stock_qty` 문서화 문자열: *"아직 Lot 을 안 고른 예약은 … Lot 가용량에서
  안 빠진다 — 그것만 보면 같은 재고를 두 번 예약하게 된다."*

피해: `SIM-CHAIN-V4` 에서 확정 34건 중 **14건 15,474kg 미출고**(중량의 59%).

🔴 **이 파일이 잡으려는 넷.**

```text
① 확정 뒤 예약이 걸린다        확정이 서면 물류의 예약 함수가 불린다
② 예약 번호가 출고 것과 같다    reservation_id_for_sale_item 을 쓴다 — 새로 지으면
                              번호가 갈려 **정확히 이중 예약**이 난다
③ 모자라도 확정은 CONFIRMED    모자란 사실은 값으로 남는다 — BLOCKED 로 안 되돌린다
④ 확정이 실패하면 예약을 안 건다  순서가 전부다
```

★ **DB 를 치지 않는다.** `confirm_sale` · 예약 함수 · 커넥션을 전부 대역으로 바꾸고
  *"불렸는가 · 무엇을 들고 불렸는가"* 만 본다 (`test_sales_approval.py` 와 같은 결).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.master import outbound_flow, sales_approval

RUN_UUID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
REQ = "REQ-DAILY-SALES-SIM-CHAIN-V4-20260106-배추"
SCN = "SALES-001-A"

#: 🔴 실측의 그날. 01-06 확정분이 01-07 에 다시 가용으로 잡혔다.
원_실행일 = date(2026, 1, 6)
납품일 = date(2026, 1, 7)
실행축 = "SIM-CHAIN-V4"

#: 🔴 실측의 그 양. 01-06 에 판 1,435kg 이다.
요구량 = Decimal(1435)

재무요약 = {"contribution_margin_krw": "128235.00", "contribution_margin_rate": "0.664"}

#: 판매가 짓는 품목 행 이름. 🔴 **손으로 조립하지 않는다** — 예약 이름이 여기서 나온다.
SALE_ITEM_ID = "SI-SALE-1-1"


class 확정_대역:
    """`confirm_sale` 대역. **불렸는가 · 무엇을 들고 불렸는가**만 남긴다."""

    def __init__(self, 터뜨린다: Exception | None = None) -> None:
        self.호출: list[Any] = []
        self.터뜨린다 = 터뜨린다

    def __call__(self, conn: Any, request: Any) -> Any:
        self.호출.append(request)
        if self.터뜨린다 is not None:
            raise self.터뜨린다

        class _결과:
            sale_id = "SALE-1"
            sale_item_id = SALE_ITEM_ID
            item_id = "ITEM-BAECHU"
            quantity_kg = 요구량

        return _결과()


class 예약_대역:
    """`reserve_confirmed_sale_available` 대역.

    :param 확보: 물류가 실제로 잡은 양. 안 주면 요구량 전부.
    """

    def __init__(self, 확보: Decimal | None = None) -> None:
        self.호출: list[Any] = []
        self.커넥션: list[Any] = []
        self.확보 = 확보

    def __call__(self, conn: Any, request: Any) -> Any:
        self.호출.append(request)
        self.커넥션.append(conn)
        요구 = Decimal(str(request.quantity_kg))

        class _예약:
            applied = True
            reservation_id = request.reservation_id
            status = "ACTIVE"
            required_qty_kg = 요구

        _예약.reserved_qty_kg = 요구 if self.확보 is None else self.확보
        return _예약()


class 커넥션_대역:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def _scenario() -> dict[str, Any]:
    return {
        "scenario_id": SCN,
        "scenario_type": "BALANCED",
        "objective": "BALANCE",
        "business_mode": "SPOT_SALES",
        "item": "배추",
        "partner_id": "KIMCHI_FACTORY_001",
        "quantity_kg": str(요구량),
        "unit_price_krw": "625",
        "sales_amount_krw": "896875",
        "delivery_date": 납품일.isoformat(),
        "payment_days": 30,
        "payment_terms_type": "SINGLE",
        "supply": {"confirmed_quantity_kg": str(요구량)},
        "required_validations": [],
    }


def _확정(확정: 확정_대역, 예약: 예약_대역, conn: 커넥션_대역 | None = None):
    return sales_approval.confirm_approved_sale(
        request_id=REQ,
        run_id=str(RUN_UUID),
        as_of=원_실행일,
        policy_version="v1.3",
        scenario=_scenario(),
        revalidation_outcome="PASSED",
        financial_summary=재무요약,
        sim_run_id=실행축,
        confirm=확정,
        reserve=예약,
        connect=(커넥션_대역 if conn is None else (lambda: conn)),
    )


# ---------------------------------------------------------------------------
# ① 확정 뒤 예약이 걸린다
# ---------------------------------------------------------------------------


def test_확정이_서면_그_자리에서_예약을_건다() -> None:
    """🔴 **잠금 ①.** 예약을 출고 날까지 미루면 그 하루 동안 같은 재고가 또 팔린다."""
    확정, 예약 = 확정_대역(), 예약_대역()

    결과 = _확정(확정, 예약)

    assert 확정.호출, "자기 생존: confirm_sale 이 아예 안 불렸다 — 아래가 공짜로 통과한다"
    assert len(예약.호출) == 1, "확정이 섰는데 물류 예약이 안 불렸다"
    assert 결과.status == "CONFIRMED"
    assert 결과.reservation_outcome == "RESERVED"


def test_예약이_확정과_같은_커넥션으로_간다() -> None:
    """★ 다른 커넥션이면 예약이 언제 보이는지가 달라진다 — 확정과 같이 서야 한다."""
    conn = 커넥션_대역()
    확정, 예약 = 확정_대역(), 예약_대역()

    _확정(확정, 예약, conn)

    assert 예약.커넥션 == [conn], "예약이 확정과 다른 커넥션으로 갔다"


def test_예약_요청이_판매가_정한_양_그대로다() -> None:
    """🟡 **수량을 마스터가 안 고친다.** 판매가 정한 양이 그대로 요구량이다."""
    확정, 예약 = 확정_대역(), 예약_대역()

    _확정(확정, 예약)

    요청 = 예약.호출[0]
    assert Decimal(str(요청.quantity_kg)) == 요구량
    assert 요청.sim_run_id == 실행축
    assert 요청.as_of == 원_실행일
    assert 요청.sale_id == "SALE-1"
    assert 요청.item_id == "ITEM-BAECHU"


# ---------------------------------------------------------------------------
# ② 예약 번호가 출고가 쓰는 것과 같다
# ---------------------------------------------------------------------------


def test_예약_번호가_출고가_계산하는_것과_같다() -> None:
    """🔴 **잠금 ② — 이 판의 핵심.**

    출고는 예약 이름을 저장해 두지 않고 `reservation_id_for_sale_item` 으로 **계산**
    한다. 확정이 다른 이름으로 잡으면 출고가 잡은 적 없는 이름을 찾게 되고, 그러면
    **고치려던 이중 예약을 정확히 만들어 낸다.**

    ★ **두 자리의 번호를 실제로 맞대 본다** — 문자열 비교다.
    """
    확정, 예약 = 확정_대역(), 예약_대역()

    결과 = _확정(확정, 예약)

    출고가_계산하는_이름 = outbound_flow.reservation_id_for_sale_item(SALE_ITEM_ID)
    assert 예약.호출[0].reservation_id == 출고가_계산하는_이름, (
        "확정이 건 예약 이름이 출고가 계산하는 이름과 다르다 — 같은 재고를 두 번 잡는다"
    )
    assert 결과.reservation_id == 출고가_계산하는_이름


def test_마스터가_예약_이름을_손으로_짓지_않는다() -> None:
    """★ 이름을 코드에서 조립하면 규칙이 갈리는 날이 온다 — 주인이 둘이 된다."""
    원문 = (
        __import__("pathlib").Path(sales_approval.__file__).read_text(encoding="utf-8")
    )
    코드 = "\n".join(
        줄 for 줄 in 원문.splitlines() if not 줄.lstrip().startswith(("#", "#:"))
    )

    assert '"RSV-' not in 코드, "마스터가 예약 이름 접두사를 직접 박았다"
    assert "'RSV-" not in 코드


# ---------------------------------------------------------------------------
# ③ 예약이 모자라도 확정은 선다
# ---------------------------------------------------------------------------


def test_예약이_모자라도_확정은_CONFIRMED_다() -> None:
    """🔴 **잠금 ③.** 확정은 이미 일어난 사실이고 장부에 섰다 — 되돌리지 않는다.

    ★ 물류도 같은 태도다: 못 잡은 몫은 `reserved_qty_kg` 로 **보이게** 남긴다.
    """
    확정, 예약 = 확정_대역(), 예약_대역(확보=Decimal(900))

    결과 = _확정(확정, 예약)

    assert 결과.status == "CONFIRMED", "예약이 모자라다고 확정을 되돌렸다"


def test_모자랐다는_사실이_결과에_실린다() -> None:
    """★★ **안 보이면 오늘 밤과 같은 일이 반복된다.** 걷기 요약이 이것을 센다."""
    확정, 예약 = 확정_대역(), 예약_대역(확보=Decimal(900))

    결과 = _확정(확정, 예약)

    assert 결과.reservation_outcome == "SHORT"
    assert 결과.required_qty_kg == 요구량
    assert 결과.reserved_qty_kg == Decimal(900)
    assert "900" in 결과.reason, f"모자란 사실이 사유에 없다: {결과.reason}"


def test_걷기_요약에_예약어휘_한_줄이_선다() -> None:
    """★★ **값이 있는데 성적표가 안 읽으면 없는 것과 같다** (`확정어휘` 와 같은 규율).

    `SIM-CHAIN-V4` 에서 확정 34건을 보고 재고가 잡힌 줄 알았다 — 이 줄이 없으면
    *"팔렸다"* 와 *"그만큼 잡아 뒀다"* 를 성적표가 구별하지 못한다.
    """
    from app.master.backfill import BackfilledRun, BackfillOut
    from app.master.backtest_runner import WalkResult, format_summary
    from app.master.scheduler import DayRunOutcome

    def _행(예약어휘: str | None) -> BackfilledRun:
        return BackfilledRun(
            as_of=원_실행일,
            run_id="RUN-1",
            request_id=REQ,
            outcome="RECORDED",
            revalidation_outcome="PASSED",
            confirmation_status="CONFIRMED",
            reservation_outcome=예약어휘,  # type: ignore[arg-type]
        )

    승인 = BackfillOut(
        sim_run_id=실행축,
        start=원_실행일,
        end=원_실행일,
        status="RAN",
        runs=(_행("RESERVED"), _행("SHORT"), _행("SHORT")),
    )
    결과 = WalkResult(
        start=원_실행일,
        end=원_실행일,
        days=(
            DayRunOutcome(as_of=원_실행일, action="RUN_NOW", reason="", sales_approval=승인),
        ),
    )

    요약 = format_summary(결과)

    assert dict(결과.reservation_outcomes) == {"RESERVED": 1, "SHORT": 2}
    assert "예약어휘  " in 요약, "요약에 예약 줄 자체가 없다"
    assert "SHORT" in 요약, "모자란 건수가 성적표에 안 보인다"


# ---------------------------------------------------------------------------
# ④ 확정이 실패하면 예약을 안 건다
# ---------------------------------------------------------------------------


def test_확정이_터지면_예약을_안_건다() -> None:
    """🔴 **잠금 ④.** 순서가 전부다 — 안 선 확정의 재고를 잡으면 재고가 사라진다."""
    확정 = 확정_대역(터뜨린다=RuntimeError("원장이 안 받았다"))
    예약 = 예약_대역()

    결과 = _확정(확정, 예약)

    assert 확정.호출, "자기 생존: confirm_sale 이 아예 안 불렸다"
    assert 예약.호출 == [], "확정이 터졌는데 예약을 걸었다"
    assert 결과.status == "FAILED"
    assert 결과.reservation_outcome is None


def test_재검증이_막히면_예약까지_안_간다() -> None:
    """★ 확정을 안 부르는 길에서는 예약도 없다 — 예약이라는 사건 자체가 없다."""
    확정, 예약 = 확정_대역(), 예약_대역()

    결과 = sales_approval.confirm_approved_sale(
        request_id=REQ,
        run_id=str(RUN_UUID),
        as_of=원_실행일,
        policy_version="v1.3",
        scenario=_scenario(),
        revalidation_outcome="CONDITIONAL",
        financial_summary=재무요약,
        sim_run_id=실행축,
        confirm=확정,
        reserve=예약,
        connect=커넥션_대역,
    )

    assert 결과.status == "BLOCKED"
    assert 확정.호출 == []
    assert 예약.호출 == []
    assert 결과.reservation_outcome is None


def test_예약이_터지면_확정까지_물러난다() -> None:
    """🔴 확정만 서고 예약이 없는 상태가 **바로 그 버그의 자리**다.

    그 상태로 커밋하느니 안 선 것이 낫다 — 사유가 어느 단계에서 터졌는지를 말한다.
    """
    conn = 커넥션_대역()

    class 터지는_예약:
        def __call__(self, conn: Any, request: Any) -> Any:
            raise RuntimeError("물류가 안 받았다")

    결과 = _확정(확정_대역(), 터지는_예약(), conn)

    assert 결과.status == "FAILED"
    assert "예약" in 결과.reason
    assert conn.rollbacks == 1, "예약이 터졌는데 확정을 롤백 안 했다"
    assert conn.commits == 0


# ---------------------------------------------------------------------------
# ⑤ 기본값이 진짜 물류 함수다
# ---------------------------------------------------------------------------


def test_기본_예약_함수가_시뮬레이션_경로의_것이다() -> None:
    """🔴 `reserve_confirmed_sale` 이 아니다 — 저쪽은 전량 아니면 멈춘다."""
    import inspect

    기본값 = inspect.signature(sales_approval.confirm_approved_sale).parameters["reserve"].default

    assert 기본값 is None, "기본값을 시그니처에 박으면 monkeypatch 가 안 닿는다"
    assert (
        sales_approval.reserve_confirmed_sale_available.__name__
        == "reserve_confirmed_sale_available"
    )


@pytest.mark.parametrize("칸", ["reservation_id", "required_qty_kg", "reserved_qty_kg"])
def test_확정이_안_서면_예약_칸은_비어_있다(칸: str) -> None:
    """★ 없는 사실을 0 으로 채우지 않는다 — 0kg 잡은 것과 안 잡은 것은 다르다."""
    결과 = sales_approval.confirm_approved_sale(
        request_id="",
        run_id=str(RUN_UUID),
        as_of=원_실행일,
        policy_version="v1.3",
        scenario=_scenario(),
        revalidation_outcome="PASSED",
        financial_summary=재무요약,
        sim_run_id=실행축,
        confirm=확정_대역(),
        reserve=예약_대역(),
        connect=커넥션_대역,
    )

    assert 결과.status == "BLOCKED"
    assert getattr(결과, 칸) is None
