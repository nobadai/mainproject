"""판매 확정이 **업무 키를 원본 주문으로 싣는다** (2026-09-11).

판매 확정이 장부 쓰기에서 터졌다. 한 칸이 비어 있었다.

```text
NotNullViolation
  null value in column "source_order_id" of relation "sales"
```

🔴 **계약과 표가 서로 다른 말을 하고 있었다.**

```text
app/sales/schemas.py   source_order_id: str | None = Field(default=None, ...)   ← 선택
haetdeul.sales         source_order_id  NOT NULL · "원본 주문 ID."              ← 필수
```

마스터는 **계약만 보고 안 실었다.** 그 칸 말고는 전부 차 있었다 — 터지는 곳이
장부라 *"판매가 거부했다"* 로 읽혔지만, 실제로는 **마스터가 안 실은 것**이다.

🟢 **`source_order_id` 는 마스터 업무 키다** (사람이 정했다).

```text
REQ-DAILY-SALES-{실행}-{날짜}-{품목}
```

★★ **지어낸 값이 아니다.** 이 시뮬레이션에서 판매를 낳은 것은 **승인된 마스터
  판단**이고, 그 판단의 이름이 업무 키다. 「원본 주문 ID」가 가리키는 것이 실제로
  그것이다.

🔴 **이 파일이 잠그는 셋.**

```text
① 확정 입력의 source_order_id 가 마스터 업무 키와 같다        (값 일치)
② 업무 키가 비면 BLOCKED 이고 사유가 이름을 부른다            (FAILED 가 아니다)
③ sale_id 와 source_order_id 가 서로 다른 값이다              (두 축이 다르다)
```

★ **③ 이 이 판의 핵심이다.** 두 칸이 같은 값이 되는 순간 **한 칸이 거짓말**이 된다.

```text
sale_id          SALE-{run_id}-{scenario_id}          ← 실행 축 (몇 번째로 돈 것인가)
source_order_id  REQ-DAILY-SALES-{실행}-{날짜}-{품목}   ← 업무 축 (무엇을 판단한 것인가)
```

  같은 업무 키가 여러 `run_id` 를 가질 수 있다(재시도). 그래서 두 칸이 같은 것을
  가리키지 않는다 — 잠그지 않으면 다음 사람이 *"둘 다 그 판매를 가리키는데 왜
  둘인가"* 로 하나를 지운다.

★ **DB 를 치지 않는다.** `confirm_sale` 은 대역이고 커넥션도 대역이다 — 무엇을 들고
  불렸는지만 본다 (`test_sales_approval.py` 와 같은 결).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from app.master import sales_approval
from app.sales.persistence import sale_id_for

#: 마스터 업무 키. 🔴 **`scheduler.daily_sales_request_id` 가 짓는 모양 그대로다** —
#: `REQ-DAILY-SALES-{실행}-{날짜}-{품목}`.
업무키 = "REQ-DAILY-SALES-SIM-CHAIN-V2-20260106-배추"

#: 실행 축. ★ **업무 키의 실행 조각과 같은 값이 아니어도 된다** — 재시도하면 갈린다.
RUN_UUID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

SCN = "SALES-001-A"
원_실행일 = date(2026, 1, 6)
납품일 = date(2026, 2, 6)
실행축 = "SIM-CHAIN-V2"
재무요약 = {"contribution_margin_krw": "128235.00", "contribution_margin_rate": "0.664"}


class 확정_대역:
    """`confirm_sale` 대역. **불렸는가 · 무엇을 들고 불렸는가**만 남긴다."""

    def __init__(self) -> None:
        self.호출: list[Any] = []

    def __call__(self, conn: Any, request: Any) -> Any:
        self.호출.append(request)

        class _결과:
            sale_id = "SALE-1"
            sale_item_id = "SI-SALE-1-1"

        return _결과()


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
        "quantity_kg": "309",
        "unit_price_krw": "625",
        "sales_amount_krw": "193125",
        "delivery_date": 납품일.isoformat(),
        "payment_days": 30,
        "payment_terms_type": "SINGLE",
        "supply": {"confirmed_quantity_kg": "309"},
        "required_validations": [],
    }


def _확정(대역: 확정_대역, *, request_id: str = 업무키):
    return sales_approval.confirm_approved_sale(
        request_id=request_id,
        run_id=str(RUN_UUID),
        as_of=원_실행일,
        policy_version="v1.3",
        scenario=_scenario(),
        revalidation_outcome="PASSED",
        financial_summary=재무요약,
        sim_run_id=실행축,
        confirm=대역,
        connect=커넥션_대역,
    )


# ---------------------------------------------------------------------------
# ① 업무 키가 원본 주문으로 실린다
# ---------------------------------------------------------------------------


def test_확정_입력의_원본주문은_마스터_업무_키다():
    """🔴 **비어 있으면 장부가 `NotNullViolation` 으로 터진다** — 실측에서 터졌다.

    ★ **계약이 「선택」이라 계약만 보면 안 실어도 통과한다.** 그래서 마스터 쪽에서
      잠근다 — 판매 계약은 판매가 정할 자리다.
    """
    대역 = 확정_대역()

    결과 = _확정(대역)

    assert 결과.status == "CONFIRMED", 결과.reason
    assert 대역.호출, "확정이 한 번도 안 불려 이 검사가 아무것도 안 재고 있다"
    보낸것 = 대역.호출[0]
    assert 보낸것.source_order_id == 업무키, (
        f"원본 주문에 마스터 업무 키가 안 실렸다: {보낸것.source_order_id!r} != {업무키!r}"
    )


# ---------------------------------------------------------------------------
# ② 업무 키가 비면 BLOCKED — FAILED 가 아니다
# ---------------------------------------------------------------------------


def test_업무_키가_비면_BLOCKED_이고_이름을_부른다():
    """🔴 **지어내지 않는다.** `or "UNKNOWN"` 으로 메우면 아무도 안 내린 판단이
    원본 주문으로 장부에 선다 — 터지지 않고 숫자와 이력만 틀린다.

    ★ **`FAILED` 가 아니다.** 실패는 *"쓰려다 실패했다"* 이고, 이것은 **마스터가
      값을 못 만든 것**이다. 둘은 다른 사실이라 한 칸에 접으면 사람이 장부를 보러
      간다 — 볼 곳은 마스터다.
    """
    대역 = 확정_대역()

    결과 = _확정(대역, request_id="")

    assert 결과.status == "BLOCKED", f"업무 키가 없는데 {결과.status} 다: {결과.reason}"
    assert 결과.status != "FAILED"
    assert "request_id" in 결과.reason and "source_order_id" in 결과.reason, (
        f"무엇이 없는지 이름을 안 부른다: {결과.reason}"
    )
    assert 대역.호출 == [], "업무 키가 없는데 확정을 불렀다"


# ---------------------------------------------------------------------------
# ③ 두 축이 다르다 — 값으로 잠근다
# ---------------------------------------------------------------------------


def test_sale_id_와_원본주문은_서로_다른_값이다():
    """🔴 **이 판의 핵심 잠금이다.**

    ★★ 두 칸이 같은 값이 되는 순간 **한 칸이 거짓말**이 된다. `sale_id` 는 *"그날
      어느 실행 행이 낳았나"* 이고 `source_order_id` 는 *"무엇에 대한 판단이었나"*
      다 — 같은 업무 키가 여러 `run_id` 를 가질 수 있어(재시도) 한 칸으로 안 접힌다.

    ★ **`sale_id` 를 여기서 짓지 않는다.** 판매가 쓰는 `sale_id_for` 에게 우리가
      보낸 입력을 그대로 물어본다 — 마스터가 공식을 베끼면 판매가 공식을 바꿔도
      이 검사가 모른다.
    """
    대역 = 확정_대역()

    _확정(대역)

    assert 대역.호출, "확정이 한 번도 안 불려 이 검사가 아무것도 안 재고 있다"
    보낸것 = 대역.호출[0]
    파생된_sale_id = sale_id_for(보낸것.execution_identity, 보낸것.selected_scenario)
    assert 보낸것.source_order_id is not None, "원본 주문이 비었다 — 잠글 값이 없다"
    assert 보낸것.source_order_id != 파생된_sale_id, (
        f"실행 축과 업무 축이 같은 값이 됐다: {파생된_sale_id!r} — "
        f"두 칸 중 하나가 거짓말이다"
    )
