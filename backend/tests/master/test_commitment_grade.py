"""승인 약정이 **등급을 나르는가** (`sourcing_plan` → `purchase_items.grade`).

🔴 **끊긴 자리는 승인 약정 하나였다** (실측 2026-09-10 · `#69`).

```text
매입 sourcing_plan[].grade   특·상·중·하
  → 승인 약정                   🔴 **여기서 끊겨 있었다**
  → master/ledger.py            🟢 칸은 있는데 NULL 을 명시로 썼다
  → logistics/purchase_detail   🟢 읽는다
  → logistics/inbound_stock     🟢 그대로 내린다
  → inventory_lots.grade        🔴 그래서 NULL
```

★★ **등급과 회차는 다른 축이다.** 한 회차가 여러 등급을 담을 수 있고 한 등급이 여러
  회차에 걸칠 수 있다. 오늘 둘 다 하나뿐인 것은 `#308` 이 분할을 못 세우기
  때문이지 계약이라서가 아니다.

```text
2026년 안 155건 전수 (매입 실측)
  안 하나당 sourcing_plan 줄 수   {1: 155}   ← 전부 한 줄
  안 하나당 split_plan 회차 수     {1: 155}   ← 전부 1회차
  등급 값   특 137 · 상 4 · 중 12 · 하 2
```

🔴 **그래서 등급을 `ArrivalLeg` 에 붙이지 않는다.** 붙이면 *"회차 하나 = 등급 하나"*
   가 계약으로 굳고, `#308` 이 풀리는 날 **말없이 틀린다.** 목록으로 나르면 지금은
   한 줄이라 결과가 같고, 여럿이 되는 날 재료가 이미 거기 있다.

⚠️ **`purchase_items` 는 품목당 한 줄이다** — `grade` 는 그 한 줄에 한 칸이라 등급이
   둘 이상이면 담을 자리가 없다. 그때는 **막는다.** 줄을 등급별로 가르는 것은
   `purchase_item_id` 규칙이 바뀌고 `inventory_lots` · `inbound_schedules` 두 FK 가
   걸리는 일이라 매입·물류와 함께 정해야 한다.

🔴 **실 DB 를 부르지 않는다.** 가짜 커넥션으로 나간 SQL 과 파라미터만 본다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import fields
from datetime import date, timedelta
from typing import Any, Self

import pytest

from app.master import ledger, transition
from app.master.commitment import (
    ApprovedCommitment,
    ArrivalLeg,
    SourcingLine,
    build_commitment,
)

AS_OF = date(2025, 12, 31)

#: 이 검사가 쓰는 실행 축. 🔴 운영 상수(`BURN_IN_SIM_RUN_ID`)를 안 쓴다.
실행축 = "SIM-GRADE-AXIS"


def N(값: str) -> str:
    """한글 잠금은 **NFC 로 재고 NFC 로 비교한다.**

    🔴 조합형 `특` 과 분해형 `특` 은 눈으로 같고 `==` 로 다르다. 잠금 문자열을
       정규화하지 않으면 이 검사가 **파일 인코딩에 걸린다** — 오늘 두 파트가
       그것에 데었다 (2026-09-10).
    """
    return unicodedata.normalize("NFC", 값)


# ── 대역 ────────────────────────────────────────────────────────────────


class 가짜커서:
    """나간 SQL 과 파라미터를 그대로 들고 있는다. `items` 조회에만 답한다."""

    def __init__(self, log: list[tuple[str, list[Any]]]) -> None:
        self.log = log
        self.rowcount = 1
        self._row: dict[str, str] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self.log.append((text, list(params or [])))
        self._row = {"item_id": "ITEM-BAECHU"} if ("FROM" in text and "items" in text) else None

    def fetchone(self) -> dict[str, str] | None:
        return self._row


class 가짜커넥션:
    def __init__(self) -> None:
        self.log: list[tuple[str, list[Any]]] = []

    def cursor(self) -> 가짜커서:
        return 가짜커서(self.log)


def _안(*, sourcing_plan: Any = None, **over: Any) -> dict[str, Any]:
    """매입이 보내는 안 하나. 🟢 3,587kg × 854원 = 3,063,298원 (실측 예)."""
    base: dict[str, Any] = {
        "label": "보수",
        "total_qty_kg": 3587.0,
        "total_amount_krw": 3063298.0,
        "split_plan": [{"seq": 1, "date": "2025-12-31", "qty_kg": 3587.0}],
    }
    if sourcing_plan is not None:
        base["sourcing_plan"] = sourcing_plan
    base.update(over)
    return base


def _약정(**over: Any) -> ApprovedCommitment:
    kw: dict[str, Any] = {
        "request_id": "REQ-1",
        "as_of": AS_OF,
        "item": "배추",
        "scenario": _안(),
        "inbound_lead_days": 2.0,
        "decision_seq": 1,
        # ★ N5 를 실어야 `payment_due_date` 가 서고, 그래야 원장이 **등급 말고 다른
        #   이유로** 막히지 않는다. 이 파일이 재는 것은 등급이다.
        "purchase_payment_days": 0.0,
    }
    kw.update(over)
    return build_commitment(**kw)


def _한줄(*, grade: str = "특") -> list[dict[str, Any]]:
    """매입 실측 모양 그대로 — 155건이 전부 이 한 줄이다."""
    return [{"grade": grade, "market": "가락", "qty_kg": 3587, "grade_unit_price": 854}]


def _원장행(commitment: ApprovedCommitment) -> tuple[ledger.PurchaseWrite, ...]:
    purchase_ids = {
        leg.seq: transition.purchase_id_for(commitment, leg.seq)
        for leg in commitment.arrival_schedule
    }
    return ledger.build_purchase_rows(commitment, purchase_ids=purchase_ids, sim_run_id=실행축)


def _품목줄_파라미터(commitment: ApprovedCommitment) -> list[Any]:
    """`purchase_items` 로 나간 `INSERT` 의 파라미터."""
    conn = 가짜커넥션()
    ledger.persist_purchases(conn, _원장행(commitment))
    return next(
        params for text, params in conn.log if "INSERT INTO" in text and "purchase_items" in text
    )


# ── ① 약정이 sourcing_plan 을 목록 그대로 나른다 ────────────────────────


def test_약정이_등급_조달을_목록으로_나른다() -> None:
    """🔴 **한 줄이어도 목록이다.** 스칼라로 접으면 여럿이 되는 날 펴야 한다."""
    commitment = _약정(scenario=_안(sourcing_plan=_한줄()))

    assert isinstance(commitment.sourcing_plan, tuple)
    assert len(commitment.sourcing_plan) == 1
    assert N(commitment.sourcing_plan[0].grade) == N("특")


def test_매입이_보낸_값을_고쳐_쓰지_않는다() -> None:
    """🔴 수량·단가·시장 **이름도 값도** 그대로다.

    ★ `grade_unit_price` 를 `unit_price_krw_per_kg` 로 고쳐 부르지 않는다 —
      그 순간 한 사실이 두 이름이 된다. `amount_krw` 를 마스터가 안 만드는 것과
      같은 규율이다.
    """
    줄 = _약정(scenario=_안(sourcing_plan=_한줄())).sourcing_plan[0]

    assert 줄.qty_kg == 3587.0
    assert 줄.grade_unit_price == 854.0
    assert N(줄.market or "") == N("가락")
    이름 = {f.name for f in fields(SourcingLine)}
    assert "grade_unit_price" in 이름, "매입이 부르는 이름을 그대로 쓴다"
    assert "unit_price_krw_per_kg" not in 이름, "이름을 바꾸면 한 사실이 두 이름이 된다"
    # 🔴 금액을 마스터가 만들지 않는다 — 매입이 안 보낸 값이다.
    assert "amount_krw" not in 이름


def test_줄이_여럿이면_여럿_그대로_들고_온다() -> None:
    """🔴 **접지 않는다.** 합치면 *"등급이 여럿이었다"* 는 사실이 사라진다."""
    두줄 = [
        {"grade": "특", "market": "가락", "qty_kg": 2000, "grade_unit_price": 900},
        {"grade": "상", "market": "가락", "qty_kg": 1587, "grade_unit_price": 780},
    ]
    commitment = _약정(scenario=_안(sourcing_plan=두줄))

    assert len(commitment.sourcing_plan) == 2
    assert [N(g) for g in commitment.grades] == [N("특"), N("상")]


def test_같은_등급_두_줄은_등급_하나다() -> None:
    """★ 시장이 달라 줄이 갈린 것뿐이고 등급 칸에 담길 값은 하나다.

    🔴 겹침 판정은 NFC 로 접고 **돌려주는 값은 받은 그대로**다. 조합형·분해형을
       다른 등급으로 세면 등급이 하나인 안이 *"둘"* 로 읽혀 원장이 엉뚱하게 멈춘다.
    """
    분해형 = unicodedata.normalize("NFD", "특")
    두줄 = [
        {"grade": "특", "market": "가락", "qty_kg": 2000, "grade_unit_price": 900},
        {"grade": 분해형, "market": "구리", "qty_kg": 1587, "grade_unit_price": 900},
    ]
    commitment = _약정(scenario=_안(sourcing_plan=두줄))

    assert len(commitment.sourcing_plan) == 2, "줄은 둘 그대로다"
    assert [N(g) for g in commitment.grades] == [N("특")], "등급은 하나다"
    assert ledger.ledger_block_reason(commitment) == "", "등급이 하나면 막지 않는다"


# ── ② 그 등급이 purchase_items.grade 에 적힌다 ──────────────────────────


def test_등급이_purchase_items_에_적힌다() -> None:
    commitment = _약정(scenario=_안(sourcing_plan=_한줄()))

    행 = _원장행(commitment)[0]
    assert 행.grade is not None
    assert N(행.grade) == N("특")
    assert N(str(_품목줄_파라미터(commitment)[3])) == N("특"), "grade 는 네 번째 칸이다"


def test_등급이_시장이나_근거_칸으로_새지_않는다() -> None:
    """★ `market_name` · `source_quote_id` 는 승인이 여전히 모르는 사실이라 `NULL` 이다.

    🔴 **칸 순서를 함께 잠근다.** `grade` 만 `%s` 로 열고 그 뒤 칸이 한 칸 밀리면
       수량이 `market_name` 에 들어간다 — DB 가 타입으로 막아 주기 전까지 여기서만
       보인다.
    """
    conn = 가짜커넥션()
    ledger.persist_purchases(conn, _원장행(_약정(scenario=_안(sourcing_plan=_한줄()))))
    text, params = next((t, p) for t, p in conn.log if "INSERT INTO" in t and "purchase_items" in t)

    assert "VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, NULL)" in " ".join(text.split())
    assert len(params) == 7, "열린 칸은 일곱이다 — market_name·source_quote_id 는 NULL 고정"
    assert N(str(params[3])) == N("특")


def test_등급_어휘를_변환하지_않는다() -> None:
    """🔴 번인은 `상품`, 새 것은 `특/상/중/하` 다. **매핑표를 만들지 않는다.**

    ★ 둘이 한 칸에 섞이는 것을 **아는 채로 둔다.** 매입이 *"매핑표를 만들 수 없다"*
      고 했고 그 판단이 맞다 — 근거 없는 대응표가 원장의 사실이 되는 것보다 낫다.
    """
    for 원문 in ("특", "상", "중", "하", "상품"):
        행 = _원장행(_약정(scenario=_안(sourcing_plan=_한줄(grade=원문))))[0]
        assert 행.grade is not None
        assert N(행.grade) == N(원문), f"{원문!r} 이 그대로 적혀야 한다"


# ── ③ 안 오면 여전히 NULL ───────────────────────────────────────────────


def test_등급이_안_오면_NULL_이다() -> None:
    """🔴 **지어내지 않는다.** 없는 것과 빈 것은 다르다 (§1.2-10)."""
    commitment = _약정(scenario=_안())

    assert commitment.sourcing_plan == ()
    assert commitment.grades == ()
    행 = _원장행(commitment)[0]
    assert 행.grade is None, "빈 문자열로 채우지 않는다"
    assert _품목줄_파라미터(commitment)[3] is None


@pytest.mark.parametrize(
    "sourcing_plan",
    [
        [],
        [{"market": "가락", "qty_kg": 3587, "grade_unit_price": 854}],
        [{"grade": "", "market": "가락", "qty_kg": 3587, "grade_unit_price": 854}],
        [{"grade": None, "market": "가락", "qty_kg": 3587, "grade_unit_price": 854}],
        "특",
    ],
    ids=["빈목록", "등급칸없음", "빈문자열", "None", "목록이아님"],
)
def test_등급으로_읽히지_않으면_NULL_이다(sourcing_plan: Any) -> None:
    """★ 매입 `SourcingPlanItem.grade` 는 `NonEmptyStr` 라 여기 오는 값은 계약 밖이다.

    🔴 계약 밖 값을 **등급으로 승격시키지 않는다.** `""` 를 원장에 적으면
       *"등급이 비어 있다"* 라는 없는 사실이 남는다.
    """
    행 = _원장행(_약정(scenario=_안(sourcing_plan=sourcing_plan)))[0]

    assert 행.grade is None


def test_원장은_등급이_없어도_멈추지_않는다() -> None:
    """★ 등급이 안 온 것은 **정상 상태**다 — 오늘 지나가는 길이 그것이다."""
    assert ledger.ledger_block_reason(_약정(scenario=_안())) == ""


# ── ④ 등급이 둘 이상이면 막는다 ─────────────────────────────────────────


def _두등급() -> list[dict[str, Any]]:
    return [
        {"grade": "특", "market": "가락", "qty_kg": 2000, "grade_unit_price": 900},
        {"grade": "상", "market": "가락", "qty_kg": 1587, "grade_unit_price": 780},
    ]


def test_등급이_둘_이상이면_원장을_막는다() -> None:
    """🔴 `purchase_items` 는 품목당 한 줄이고 `grade` 는 한 칸이다 — 담을 자리가 없다."""
    commitment = _약정(scenario=_안(sourcing_plan=_두등급()))

    사유 = ledger.ledger_block_reason(commitment)
    assert 사유, "막아야 한다"
    assert N("등급이 2개인데 매입 줄이 하나다") in N(사유), "왜 못 쓰는지가 사유에 있어야 한다"


def test_등급이_둘이면_행을_만들지_않는다() -> None:
    """🔴 **아무거나 고르지 않는다.** 고르면 어느 등급이 남는지가 줄 순서에 걸린다."""
    commitment = _약정(scenario=_안(sourcing_plan=_두등급()))

    with pytest.raises(ledger.PurchaseLedgerNotWritable) as 걸림:
        _원장행(commitment)

    assert N("등급이 2개인데 매입 줄이 하나다") in N(str(걸림.value))


def test_등급이_둘이면_커넥션도_안_연다() -> None:
    """★ 막힌 약정은 SQL 이 한 줄도 안 나간다 — 🔴 DB 에 한 행도 안 쓴다."""
    conn = 가짜커넥션()
    commitment = _약정(scenario=_안(sourcing_plan=_두등급()))

    with pytest.raises(ledger.PurchaseLedgerNotWritable):
        ledger.persist_purchases(conn, _원장행(commitment))

    assert conn.log == []


def test_막을_때_등급을_고르지도_합치지도_않는다() -> None:
    """🔴 사유에 **둘 다** 적힌다 — 하나만 적으면 그것을 골랐다는 뜻으로 읽힌다."""
    사유 = ledger.ledger_block_reason(_약정(scenario=_안(sourcing_plan=_두등급())))

    assert N("특") in N(사유) and N("상") in N(사유)
    assert N("특상") not in N(사유), "붙여 합친 등급을 만들지 않는다"


def test_등급이_셋이면_셋이라고_적는다() -> None:
    """★ *"등급이 여럿"* 처럼 뭉뚱그리지 않는다 — 몇 개인지가 사유에 있어야 한다."""
    셋 = _두등급() + [{"grade": "중", "market": "가락", "qty_kg": 0, "grade_unit_price": 600}]
    셋[0]["qty_kg"] = 2000
    사유 = ledger.ledger_block_reason(_약정(scenario=_안(sourcing_plan=셋)))

    assert N("등급이 3개인데 매입 줄이 하나다") in N(사유)


# ── ⑤ 원문 잠금 — 넣지 않기로 한 것이 안 들어갔다 ───────────────────────


def test_ArrivalLeg_에_등급이_없다() -> None:
    """🔴 **회차에 등급을 붙이지 않는다.**

    붙이면 *"회차 하나 = 등급 하나"* 가 계약으로 굳는다. 오늘은 `#308` 때문에 둘 다
    하나뿐이라 아무 일도 안 나지만, 분할이 서는 날 한 회차가 여러 등급을 담게 되고
    그때 **에러 없이 틀린 등급**이 원장으로 간다.
    """
    이름 = {f.name for f in fields(ArrivalLeg)}

    assert "grade" not in 이름, "등급은 sourcing_plan 이 목록으로 나른다"
    assert not any("grade" in n for n in 이름), "등급이 다른 이름으로도 붙지 않는다"


def test_등급은_약정_축에서_집는다() -> None:
    """★ 회차가 둘이어도 등급은 하나고, 두 회차가 **같은 등급**을 받는다."""
    두회차 = [
        {"seq": 1, "date": "2025-12-31", "qty_kg": 2000.0, "amount_krw": 1708000.0},
        {"seq": 2, "date": "2026-01-03", "qty_kg": 1587.0, "amount_krw": 1355298.0},
    ]
    commitment = _약정(
        scenario=_안(sourcing_plan=_한줄(), split_plan=두회차),
        purchase_payment_days=0.0,
    )

    행들 = _원장행(commitment)
    assert len(행들) == 2
    assert [N(행.grade or "") for 행 in 행들] == [N("특"), N("특")]


def test_purchase_item_id_규칙이_안_바뀌었다() -> None:
    """🟢 등급을 실었다고 **줄을 가르지 않았다** — 키 규칙이 그대로다.

    ⚠️ 등급별로 줄을 가르면 이 규칙이 바뀌고 `inventory_lots` ·
      `inbound_schedules` 두 FK 가 걸린다. 이 판에서 하는 일이 아니다.
    """
    commitment = _약정(scenario=_안(sourcing_plan=_한줄()))

    params = _품목줄_파라미터(commitment)
    assert params[0] == "PITEM-REQ-1-D1-S1-BAECHU", "등급이 키에 안 섞인다"
    assert params[1] == "PUR-REQ-1-D1-S1"
    assert transition.purchase_item_id_for("PUR-REQ-1-D1-S1", "BAECHU") == params[0]


def test_등급이_있어도_품목_줄은_하나다() -> None:
    """🔴 줄 수는 **회차 수**로 정해진다 — 등급 수가 아니다."""
    commitment = _약정(scenario=_안(sourcing_plan=_한줄()))
    conn = 가짜커넥션()

    written = ledger.persist_purchases(conn, _원장행(commitment))

    assert written == {"purchases": 1, "purchase_items": 1}


def test_등급이_수량과_단가를_건드리지_않는다() -> None:
    """🟢 등급별 단가(854원)가 원장 단가로 새지 않는다 — 원장 단가는 회차에서 나온다."""
    없음 = _원장행(_약정(scenario=_안()))[0]
    있음 = _원장행(_약정(scenario=_안(sourcing_plan=_한줄(grade="중"))))[0]

    assert 없음.quantity_kg == 있음.quantity_kg
    assert 없음.unit_price_krw_per_kg == 있음.unit_price_krw_per_kg
    assert 없음.line_amount_krw == 있음.line_amount_krw


def test_기존_약정은_등급_없이도_그대로_선다() -> None:
    """★ `sourcing_plan` 을 모르는 옛 입력이 깨지지 않는다."""
    commitment = ApprovedCommitment(
        approval_id="H1-REQ-1-1",
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario_label="보수",
        total_qty_kg=3587.0,
        total_amount_krw=3063298.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=3587.0,
                arrival_date=AS_OF + timedelta(days=2),
                purchase_date=AS_OF,
                seq=1,
                payment_due_date=AS_OF,
            ),
        ),
        inbound_lead_days=2.0,
    )

    assert commitment.sourcing_plan == ()
    assert _원장행(commitment)[0].grade is None
