"""화면 API 매입 탭 — **갈라 둔 것이 화면에서 다시 붙지 않는가.**

★ `tests/api/test_screen_api.py` 는 여섯 탭의 **모양**을 봅니다 (지환님 소유 ·
  고치지 않습니다). 여기서는 매입 탭만의 **뜻**을 봅니다.

🔴 이 파일이 막으려는 변이 넷:

.. code-block:: text

    ① Plan 에서 cut_unit_price 를 지운다
    ② cut_unit_price 자리에 max_price 를 넣는다
    ③ cut_unit_price 가 None 일 때 max_price 로 메운다
    ④ DB 를 못 읽었는데 filled=True 로 내보낸다

⚠️ **DB 를 안 붙입니다.** ``_read`` 를 갈아끼워 씁니다 — 화면 층의 판단만
  검사하고, 값이 실제로 있는지는 검사 대상이 아닙니다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.api.purchase import query as tab
from app.api.purchase.schema import Plan

AS_OF = date(2026, 1, 6)


def _scenario(label: str, **over: object) -> dict:
    scenario = {
        "label": label,
        "coverage_days": 2,
        "total_qty_kg": 1435,
        "total_amount_krw": 1327375,
        "max_price": 1095,
        "strategy_type": "quantity",
        "sourcing_plan": [
            {"grade": "특", "market": "가락", "qty_kg": 1435, "grade_unit_price": 925},
        ],
        "split_plan": [
            {"seq": 1, "date": "2026-01-06", "qty_kg": 1435,
             "amount_krw": 1327375, "expected_arrival_date": "2026-01-08"},
        ],
        "rationale": [{"source": "예측", "claim": "D+14 −2.8%", "ref_id": "FC-2026-01-06"}],
        "risks": ["등급 배분 보류"],
    }
    scenario.update(over)
    return scenario


def _run(request_id: str, *scenarios: dict, **over: object) -> dict:
    run = {
        "request_id": request_id,
        "item": "배추",
        "end_code": "E1_APPROVED",
        "runtime_status": "READY",
        "created_at": f"2026-09-05 10:0{len(request_id) % 10}",
        "payload": {"scenarios": list(scenarios), "judgment": {"rejected_reasons": []}},
    }
    run.update(over)
    return run


def _data(**over: object) -> dict:
    data = {
        "runs": [_run("REQ-20260106-0001", _scenario("보수"))],
        "buys": [],
        "decisions": [],
        "items": {"ITEM-BAECHU": "배추"},
        "arrivals": [],
    }
    data.update(over)
    return data


@pytest.fixture
def read(monkeypatch):
    """``_read`` 를 갈아끼운다. 돌려줄 값을 넣으면 그대로 읽힌다."""

    def install(data: dict | Exception) -> None:
        def fake(_as_of: date) -> dict:
            if isinstance(data, Exception):
                raise data
            return data

        monkeypatch.setattr(tab, "_read", fake)

    return install


# ══════════════════════════════════════════════════════════════════════════
#  🔴 상한 둘 — 09-08 에 갈랐다 (#398)
# ══════════════════════════════════════════════════════════════════════════

def test_컷_기준과_재무_기준은_서로_다른_칸이다():
    """변이 ① — ``Plan`` 에서 ``cut_unit_price`` 를 지우면 여기서 운다.

    ⚠️ 값을 견주지 않는다. 지금 두 값이 **같아서** 값 비교로는 갈렸는지 못 잰다
    (CLAUDE.md 규칙 8). 칸이 둘인지를 본다.
    """
    assert "cut_unit_price" in Plan.model_fields
    assert "max_price" in Plan.model_fields
    assert Plan.model_fields["cut_unit_price"] is not Plan.model_fields["max_price"]


def test_컷_기준이_오면_재무_기준과_따로_실린다(read):
    """변이 ② — 컷 자리에 ``max_price`` 를 넣으면 여기서 운다.

    ★ 둘이 **다른 값인 시나리오**를 넣는다. 같은 값으로 검사하면 자리를 바꿔도
      통과한다 — `09-17` 에 밴드가 바뀌면 실제로 갈라질 상황을 미리 만든다.
    """
    read(_data(runs=[_run("REQ-A", _scenario("보수", max_price=1095, cut_unit_price=980))]))
    plan = tab.build(AS_OF).plans[0]
    assert plan.max_price == 1095
    assert plan.cut_unit_price == 980


def test_컷_기준_칸이_없으면_재무_기준으로_메우지_않는다(read):
    """변이 ③ — ``or max_price`` 로 메우면 여기서 운다.

    🔴 저장된 실행 대부분에 이 칸이 없다. 그 칸이 `09-08` 에 생겼기 때문이고,
    메우는 순간 갈라 둔 둘이 화면에서 **다시 하나**가 된다.
    """
    read(_data(runs=[_run("REQ-A", _scenario("보수", max_price=1095))]))
    result = tab.build(AS_OF)
    assert result.plans[0].cut_unit_price is None
    #  ★ 비운 것으로 끝내지 않는다 — 왜 비었는지 화면이 말해야 한다 (규칙 3).
    assert "컷 기준" in result.plans_note.text


# ══════════════════════════════════════════════════════════════════════════
#  🔴 예시값 딱지
# ══════════════════════════════════════════════════════════════════════════

def test_DB_를_못_읽으면_예시값_딱지가_붙는다(read):
    """변이 ④ — 못 읽었는데 ``filled=True`` 면 보는 사람이 데모 숫자를 실적으로 읽는다."""
    read(RuntimeError("연결 실패"))
    result = tab.build(AS_OF)
    assert result.source.filled is False
    assert "예시값" in result.plans_note.text


def test_읽었는데_비면_왜_비었는지_적는다(read):
    """읽고 나서 비는 것과 못 읽어서 비는 것은 **다르다**.

    읽었으면 ``filled=True`` 가 맞고, 대신 «몇 건을 봤는데 없더라» 를 적는다.
    """
    read(_data(runs=[]))
    result = tab.build(AS_OF)
    assert result.source.filled is True
    assert result.plans == []
    assert result.plans_note.text.strip()
    assert result.committed.empty_text.strip()


# ══════════════════════════════════════════════════════════════════════════
#  🔴 데모 브랜치에서 부딪힌 셋
# ══════════════════════════════════════════════════════════════════════════

def test_같은_날_실행이_여럿이면_무엇을_골랐는지_적는다(read):
    """레슨 ① — `2026-01-06` 배추는 실행이 아홉이다.

    아무 말 없이 하나를 고르면 다음 사람이 다른 행을 보고 «값이 다르다» 고 한다.
    """
    read(_data(runs=[
        _run("REQ-NEW", _scenario("보수")),
        _run("REQ-OLD", _scenario("보수"), created_at="2026-09-01 09:00"),
        _run("REQ-DEAD", runtime_status="RUNTIME_NOT_READY", end_code="E4_NOT_STARTED"),
    ]))
    result = tab.build(AS_OF)
    assert len(result.plans) == 1  # 품목마다 하나
    assert "REQ-NEW" in result.plans_note.text
    assert "3건" in result.plans_note.text  # 몇 개 중에 골랐는지


def test_계약_밖_품목은_화면에_안_올린다(read):
    """🔴 저장된 실행에 **피마늘**이 194건 남아 있다 (2026-09-09 실측).

    `#216` 으로 계약에서 뺐지만 기록은 일부러 안 고쳤다 (`e63f990` — *"고쳐 쓰면
    기록이 거짓이 된다"*). 그러니 **보일 때 거른다.** 계약이 그렇게 적어 두었다 —

    ★ **이 검사가 「피마늘」이라는 낱말을 쓰는 마지막 자리 중 하나다** (2026-09-09).
      선언·mock·다른 검사에서는 다 걷었지만 **여기는 남긴다** — DB 에 그 행이
      실제로 있고, 거르는 기능이 도는지 재려면 그 이름으로 넣어 봐야 한다.
    제안 축은 ``ITEMS`` 로 거르고 재고 축은 안 좁힌다 (`contracts/core.py` 의 ``ITEMS`` 각주).

    ⚠️ 조용히 없애지 않는다. 몇 건을 왜 뺐는지 화면이 말해야 한다.
    """
    read(_data(runs=[
        _run("REQ-BAECHU", _scenario("보수")),
        _run("REQ-PIMANUL", _scenario("보수"), item="피마늘"),
        _run("REQ-NOITEM", _scenario("보수"), item=None),
    ]))
    result = tab.build(AS_OF)
    assert [p.key for p in result.plans] == ["배추 · 보수"]
    assert "피마늘" in result.plans_note.text
    assert "품목 미상" in result.plans_note.text


def test_어휘를_여기서_다시_세지_않는다():
    """품목 목록의 주인은 계약이다 (규칙 7).

    ⚠️ 값 비교가 아니라 **같은 객체를 보고 있는가**를 본다 — 사본을 만들어 두면
    계약이 바뀌어도 화면만 옛 목록으로 남는다. `#281` 이 그 병이었다.
    """
    from app.contracts.core import ITEMS

    assert tab.ITEMS is ITEMS


def test_계약_품목_실행이_하나도_없으면_그렇게_적는다(read):
    """피마늘만 있는 날은 «안 0개» 이고, 그 이유가 화면에 있어야 한다."""
    read(_data(runs=[_run("REQ-PIMANUL", _scenario("보수"), item="피마늘")]))
    result = tab.build(AS_OF)
    assert result.plans == []
    assert "피마늘" in result.plans_note.text


def test_안이_없으면_안별_컷_사유를_적는다(read):
    """레슨 ② — ``no_proposal_reason`` 이라는 칸은 **없다.**

    ``reason`` 은 한 줄 요약이라 «어느 안이 왜 죽었나» 를 못 말한다.
    안별 사유는 ``judgment.rejected_reasons[]`` 에 있다.
    """
    dead = _run("REQ-HELD", end_code="E2_HELD")
    dead["payload"] = {
        "scenarios": [],
        "reason": "유효한 안이 없어 제안을 내지 못했다.",
        "judgment": {"rejected_reasons": [
            {"label": "보수",
             "reason": "하드 제약(창고, 현금)으로 수량이 0까지 축소되어 제안 불가"},
        ]},
    }
    read(_data(runs=[dead]))
    note = tab.build(AS_OF).plans_note.text
    assert "보수" in note
    assert "창고" in note


#: 실제 저장된 모양에서 그대로 옮겼다 (`2025-12-31` 배추 공격안). **지어내지 않는다** —
#: 키를 하나라도 틀리게 적으면 검사는 통과하는데 화면만 빈다.
_PAYMENT_SCHEDULE = [
    {"seq": 1, "basis": "as_of_unit_price", "qty_kg": 3818, "amount_krw": 6_299_700,
     "payment_date": "2026-01-07", "purchase_date": "2025-12-31",
     "amount_max_krw": 7_258_018},
    {"seq": 2, "basis": "as_of_unit_price", "qty_kg": 3818, "amount_krw": 6_299_700,
     "payment_date": "2026-01-13", "purchase_date": "2026-01-06",
     "amount_max_krw": 7_258_018},
]


def test_지급_계획이_있으면_그대로_편다(read):
    """★ 지급 계획은 **분할 안에서만** 실린다 (2026-09-08 실측).

    ::

        split_plan 1회차   719건   payment_schedule 없음
        split_plan 2회차   228건   payment_schedule 배열     ← 228 = 228

    저장된 분할 안이 전부 `2025-12-31` 이고 그날 최신 실행은 1회차라, 이 갈래는
    **실 데이터로 아직 안 탄다.** 그래서 검사로 세운다 — 안 세우면 분할 안이
    최신이 되는 날 화면이 조용히 빈다.
    """
    read(_data(runs=[_run("REQ-A", _scenario("공격", payment_schedule=_PAYMENT_SCHEDULE))]))
    table = tab.build(AS_OF).plans[0].payments

    assert [row["leg"] for row in table.rows] == [1, 2]
    assert [row["buy"] for row in table.rows] == ["2025-12-31", "2026-01-06"]
    assert [row["pay"] for row in table.rows] == ["2026-01-07", "2026-01-13"]
    assert [row["amount"] for row in table.rows] == ["6,299,700 원", "6,299,700 원"]
    #  ★ 머리에 있는 칸이 행에 다 있어야 한다 — 없으면 그 칸이 통째로 빈다.
    for row in table.rows:
        assert not {c.key for c in tab._PAY_COLS} - set(row)


def test_지급_표에_재무_스트레스_금액을_안_싣는다(read):
    """🔴 `amount_max_krw` 는 **상한가로 샀을 때의 금액**이다 (재무 STRESS).

    지급 표에 두면 «실제로 낼 돈» 으로 읽힌다. 이 PR 이 `max_price` 를 컷 자리에서
    빼낸 것과 같은 이유다 — 자리가 뜻을 만든다.
    """
    read(_data(runs=[_run("REQ-A", _scenario("공격", payment_schedule=_PAYMENT_SCHEDULE))]))
    rendered = str(tab.build(AS_OF).plans[0].payments.rows)

    assert "7,258,018" not in rendered
    assert "as_of_unit_price" not in rendered


def test_지급_계획이_없으면_빈_표에_이유를_적는다(read):
    """한 번에 사는 안은 지급이 한 건이라 계획을 따로 안 만든다.

    ⚠️ **빈 표가 흔한 것이 정상**이다. 매입일로 메우면 «그날 냈다» 는 거짓이 된다.
    """
    read(_data(runs=[_run("REQ-A", _scenario("보수"))]))
    table = tab.build(AS_OF).plans[0].payments

    assert table.rows == []
    assert table.empty_text.strip()


def test_줄_금액은_원장_합계가_아니라_줄_금액이다(read):
    """레슨 ③ — ``purchases`` 와 ``purchase_items`` 를 조인하면 합계가 줄마다 반복된다.

    `PUR-KIMCHI-015` 는 다섯 줄이고 다섯 다 `3,370,487` 이 찍혔었다.
    """
    read(_data(buys=[
        {"purchase_id": "PUR-X", "purchase_date": date(2026, 1, 5),
         "payment_due_date": date(2026, 1, 5), "settlement_status": "OPEN",
         "item_id": "ITEM-BAECHU", "grade": None, "quantity_kg": 1000,
         "unit_price_krw_per_kg": 900, "line_amount_krw": 900_000},
        {"purchase_id": "PUR-X", "purchase_date": date(2026, 1, 5),
         "payment_due_date": date(2026, 1, 5), "settlement_status": "OPEN",
         "item_id": "ITEM-MU", "grade": None, "quantity_kg": 500,
         "unit_price_krw_per_kg": 600, "line_amount_krw": 300_000},
    ], items={"ITEM-BAECHU": "배추", "ITEM-MU": "무"}))
    result = tab.build(AS_OF)
    amounts = [row["amount"] for row in result.committed.rows]
    assert amounts == ["900,000", "300,000"]  # 같은 값이 반복되면 조인 실수다
    week = next(s for s in result.stats if "매입액" in s.label)
    assert week.raw == 1_200_000


def test_원장에_없는_값은_0_이_아니라_공란이다(read):
    """등급이 `NULL` 이고 도착일을 못 맞추면 **공란**이다 (규칙 3)."""
    read(_data(buys=[
        {"purchase_id": "PUR-X", "purchase_date": date(2026, 1, 5),
         "payment_due_date": date(2026, 1, 5), "settlement_status": "OPEN",
         "item_id": "ITEM-BAECHU", "grade": None, "quantity_kg": 1000,
         "unit_price_krw_per_kg": 900, "line_amount_krw": 900_000},
    ]))
    row = tab.build(AS_OF).committed.rows[0]
    assert row["grade"] is None
    assert row["arrive"] is None
    #  ★ 표 머리의 칸이 행에 다 있어야 한다 — 없으면 그 칸이 통째로 빈다.
    assert {c.key for c in tab._COMMITTED_COLS} <= set(row)


def test_도착일은_금액으로_맞춘다(read):
    """확정 매입 원장에 도착일이 없다. 그날 실행의 시나리오에서 **금액으로** 가져온다.

    ★ `purchase_id` 문자열을 쪼개지 않는다 — 이름 규칙이 바뀌는 날 조용히 끊긴다.
    """
    read(_data(
        buys=[{"purchase_id": "PUR-X", "purchase_date": date(2026, 1, 5),
               "payment_due_date": date(2026, 1, 5), "settlement_status": "OPEN",
               "item_id": "ITEM-BAECHU", "grade": None, "quantity_kg": 3587,
               "unit_price_krw_per_kg": 854, "line_amount_krw": 3_063_298}],
        arrivals=[{"as_of": date(2026, 1, 5), "item": "배추", "scenarios": [
            _scenario("기본", total_amount_krw=3_063_298, split_plan=[
                {"seq": 1, "date": "2026-01-05", "qty_kg": 3587,
                 "expected_arrival_date": "2026-01-07"}]),
        ]}],
    ))
    result = tab.build(AS_OF)
    assert result.committed.rows[0]["arrive"] == "2026-01-07"
    inbound = next(s for s in result.stats if "입고" in s.label)
    assert inbound.raw == 3587  # as_of 뒤에 오는 것만 «예정» 이다


def test_등급이_여럿이면_평균으로_접지_않는다(read):
    """판매가 `§15-6` 에서 요청한 것 — 가중평균으로 접지 않는다.

    ★ 컷은 줄마다 걸리므로 화면에 견줄 값은 **최고가**다. 그 사실을 등급 글자에
      적는다 — 안 적으면 평균으로 읽힌다.
    """
    read(_data(runs=[_run("REQ-A", _scenario("보수", sourcing_plan=[
        {"grade": "특", "market": "가락", "qty_kg": 1000, "grade_unit_price": 925},
        {"grade": "상", "market": "가락", "qty_kg": 435, "grade_unit_price": 880},
    ]))]))
    plan = tab.build(AS_OF).plans[0]
    assert plan.unit_price == 925
    assert "특" in plan.grade and "상" in plan.grade
