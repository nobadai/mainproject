"""2026-09-14 신규 거래처 — **거래처가 날짜를 갖는다.**

시나리오: 기존 단일 김치공장 거래 구조에서 2026-09-14 경기권 반찬제조공장이 신규
거래처로 유입되어, 기존 거래처의 약 30% 규모의 추가 수요가 발생한 상황.

```text
① 파생 수요   창의 날 d 마다 active_from <= d · effective_from <= d 인 거래처 일수요 합
② 주문 주기   거래처마다 제 order_cycle_days (전 판: 뷰 LIMIT 1)
③ 판매 규칙   sales_terms 객체 또는 목록 · 목록이면 그날 유효한 거래처마다 적힌 순서로
④ 업무 키     목록 규칙의 판매 키 꼬리에 거래처가 붙는다 · 객체 규칙은 종전 그대로
```

⚠️ **실 DB 를 타지 않는다.** `fetch_all` · `get_db_schema` 와 하루의 함수들을 대역으로 갈아
  끼운다. 대역은 SQL 의 날짜 조건을 **흉내내지 않는다** — 날짜 판정이 파이썬(`_in_force`)
  에서 일어나는지를 값으로 재기 위해서다.

⚠️ **LLM 경로를 안 탄다.** 매입·판매는 전부 대역이다.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.master import inputs, scheduler
from app.master.backfill import (
    ALWAYS_FIXED_TYPE,
    ML_CURRENT_PRICE,
    BackfillOut,
    BackfillRuleMissing,
    SalesTermsRule,
    read_rules,
)
from app.master.clock import SEOUL
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.scheduler import plan_next_action, run_scheduled_day

기존 = "KIMCHI_FACTORY_001"
신규 = "BANCHAN_FACTORY_001"
번인_시작 = date(2025, 12, 1)
유입일 = date(2026, 9, 14)
창 = inputs._ORDER_WINDOW_DAYS


# ══════════════════════════════════════════════════════════════════════
#  대역 — 파생 수요 조회
# ══════════════════════════════════════════════════════════════════════


def _행(
    partner_id: str,
    daily: str,
    *,
    cycle: int = 2,
    active_from: date = 번인_시작,
    effective_from: date | None = None,
) -> dict[str, Any]:
    return {
        "partner_id": partner_id,
        "order_cycle_days": cycle,
        "active_from": active_from,
        "effective_from": effective_from or active_from,
        "daily_demand_kg": Decimal(daily),
        "demand_basis": "통합 Persona v1.2 적용 일수요",
        "provisional": True,
    }


def _DB(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    """확정 주문 조회는 0건, 파생 수요 조회는 `rows` 전부.

    🔴 **params 의 날짜로 거르지 않는다.** SQL 이 창 끝으로 걸러 주는 것에 기대면
      창 안의 날짜별 판정이 빠져도 값이 안 바뀐다.
    """

    def fetch_all(query: Any, params: Any = None) -> list[dict[str, Any]]:
        if "partner_item_demands" in query.as_string(None):
            return list(rows)
        return []

    def fetch_one(*_a: Any) -> Any:
        raise AssertionError("파생 경로가 fetch_one 을 불렀다")

    monkeypatch.setattr(inputs, "fetch_all", fetch_all)
    monkeypatch.setattr(inputs, "fetch_one", fetch_one)
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")


def _전판(daily: float, cycle: int, as_of: date, item: str = "배추") -> dict[str, Any]:
    """**변경 전 `_orders_from_demand` 의 payload 식 그대로** (커밋 83b328b)."""
    orders = [
        {
            "sale_id": None,
            "qty_kg": round(daily * cycle, 1),
            "due_date": (as_of + timedelta(days=offset)).isoformat(),
        }
        for offset in range(cycle, 창 + 1, cycle)
    ]
    return {
        "as_of": as_of.isoformat(),
        "item": item,
        "orders": orders,
        "total_kg": round(sum(o["qty_kg"] for o in orders), 1),
    }


def _파생(item: str, as_of: date):
    return inputs.load_confirmed_orders(item, as_of, sim_run_id="SIM-TEST-PARTNER-DATES")


def _바이트(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=False)


# ══════════════════════════════════════════════════════════════════════
#  ① 거래처 1곳 · 기본 날짜 → 전 판과 같다 (V13 1~3월 대표 날짜)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("as_of", [date(2026, 1, 5), date(2026, 2, 16), date(2026, 3, 20)])
@pytest.mark.parametrize("daily", ["717.300", "154.400", "14.300"])
def test_거래처_1곳_기본_날짜면_파생_수요가_전_판과_바이트까지_같다(monkeypatch, as_of, daily):
    _DB(monkeypatch, [_행(기존, daily)])

    got = _파생("배추", as_of)

    assert got.grade == "DERIVED"
    assert _바이트(got.payload) == _바이트(_전판(float(Decimal(daily)), 2, as_of)), got.note
    assert "거래처 1곳 합산" in got.note


# ══════════════════════════════════════════════════════════════════════
#  ② 두 번째 거래처 active_from 2026-09-14
# ══════════════════════════════════════════════════════════════════════


def _두_거래처(cycle: int = 2) -> list[dict[str, Any]]:
    return [
        _행(기존, "717.300"),
        _행(신규, "215.200", cycle=cycle, active_from=유입일),
    ]


def test_창이_유입일_전에_끝나면_새_거래처가_안_보인다(monkeypatch):
    """🔴 **변이 대상.** `_in_force` 의 날짜 조건을 빼면 여기가 빨개진다.

    ★ `2026-08-30` 의 창은 `08-31 ~ 09-13` 이다 — 창의 모든 날이 유입일 전이다.
    """
    as_of = date(2026, 8, 30)
    _DB(monkeypatch, _두_거래처())

    got = _파생("배추", as_of)

    assert _바이트(got.payload) == _바이트(_전판(717.3, 2, as_of)), got.note
    assert "거래처 1곳 합산" in got.note


def test_창이_유입일을_걸치면_유입일_이후_날만_더해진다(monkeypatch):
    """★ `2026-09-10` 의 창은 `09-11 ~ 09-24` 다. 새 거래처는 `09-14 ~ 09-24` 11일만 더한다.

    ```text
    due 09-12  09-11·09-12        새 거래처 0일   주문 없음
    due 09-14  09-13·09-14        새 거래처 1일   215.2
    due 09-16 ~ 09-24             새 거래처 2일씩 430.4
    ```
    """
    as_of = date(2026, 9, 10)
    _DB(monkeypatch, _두_거래처())

    got = _파생("배추", as_of)

    기존분 = _전판(717.3, 2, as_of)["orders"]
    새분 = [o for o in got.payload["orders"] if o not in 기존분]
    남은_기존분 = [o for o in got.payload["orders"] if o in 기존분]
    assert 남은_기존분 == 기존분, "기존 거래처 주문이 바뀌었다"
    assert 새분 == [
        {"sale_id": None, "qty_kg": 215.2, "due_date": "2026-09-14"},
        *(
            {"sale_id": None, "qty_kg": 430.4, "due_date": f"2026-09-{day}"}
            for day in (16, 18, 20, 22, 24)
        ),
    ]
    assert got.payload["total_kg"] == round(717.3 * 14 + 215.2 * 11, 1)
    assert "거래처 2곳 합산" in got.note
    assert "2026-09-14 부터" in got.note


def test_창_전체가_유입일_이후면_창_전체만큼_더해진다(monkeypatch):
    """★ `2026-09-13` 의 창은 `09-14 ~ 09-27` 이라 새 거래처가 14일 전부 더한다.

    ⚠️ 이 날 **매입 입력은 바뀐다.** 9/13 의 판매는 안 바뀐다(아래 ④).
    """
    as_of = date(2026, 9, 13)
    _DB(monkeypatch, _두_거래처())

    got = _파생("배추", as_of)

    assert got.payload["total_kg"] == round(717.3 * 14 + 215.2 * 14, 1)
    assert "거래처 2곳 합산" in got.note


def test_유입일_이후_창은_두_거래처_합이다(monkeypatch):
    as_of = date(2026, 9, 20)
    _DB(monkeypatch, _두_거래처())

    got = _파생("배추", as_of)

    assert got.payload["total_kg"] == round(717.3 * 14 + 215.2 * 14, 1)
    dates = [o["due_date"] for o in got.payload["orders"]]
    assert dates == sorted(dates), "납품일 순서가 아니다"


# ══════════════════════════════════════════════════════════════════════
#  ③ 주문 주기 — LIMIT 1 을 걷었다
# ══════════════════════════════════════════════════════════════════════


def test_거래처마다_제_주기로_쪼갠다(monkeypatch):
    """🔴 전 판은 `v_current_partner_demand LIMIT 1` 이라 거래처가 둘이면 어느 주기인지 몰랐다."""
    as_of = date(2026, 9, 20)
    _DB(monkeypatch, _두_거래처(cycle=3))

    got = _파생("배추", as_of)

    기존분 = _전판(717.3, 2, as_of)["orders"]
    새분 = [o for o in got.payload["orders"] if o not in 기존분]
    assert [o["due_date"] for o in 새분] == [
        (as_of + timedelta(days=offset)).isoformat() for offset in (3, 6, 9, 12)
    ]
    assert all(o["qty_kg"] == round(215.2 * 3, 1) for o in 새분)


def test_파생_수요가_뷰의_LIMIT_1_을_안_읽는다():
    """★ 설명 문장은 전 판을 말하므로 docstring 을 걷고 **코드 문자열만** 본다."""
    문자열: list[str] = []
    for fn in (inputs._orders_from_demand, inputs._demand_rows):
        body = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        doc = ast.get_docstring(body, clean=False)  # type: ignore[arg-type]
        문자열 += [
            node.value
            for node in ast.walk(body)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != doc
        ]
    코드 = "\n".join(문자열)
    assert "partner_item_demands" in 코드, "스캐너가 아무것도 안 재고 있다"
    assert "LIMIT 1" not in 코드
    assert "v_current_partner_demand" not in 코드


def test_품목_비중은_그날_유효한_거래처를_품목별로_더한다(monkeypatch):
    """🔴 전 판은 행마다 표에 넣어 같은 품목 둘째 행이 첫째를 덮었다."""
    rows = [
        {"item_name": "배추", "partner_id": 기존, "active_from": 번인_시작,
         "effective_from": 번인_시작, "daily_demand_kg": Decimal("717.300")},
        {"item_name": "무", "partner_id": 기존, "active_from": 번인_시작,
         "effective_from": 번인_시작, "daily_demand_kg": Decimal("154.400")},
        {"item_name": "배추", "partner_id": 신규, "active_from": 유입일,
         "effective_from": 유입일, "daily_demand_kg": Decimal("215.200")},
        {"item_name": "무", "partner_id": 신규, "active_from": 유입일,
         "effective_from": 유입일, "daily_demand_kg": Decimal("46.300")},
    ]  # fmt: skip
    monkeypatch.setattr(inputs, "fetch_all", lambda *a: list(rows))
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")

    전 = inputs._mix_ratio_from_demand(date(2026, 9, 13))
    후 = inputs._mix_ratio_from_demand(유입일)

    assert 전 == {"배추": round(717.3 / 871.7, 4), "무": round(154.4 / 871.7, 4)}
    assert 후 == {
        "배추": round((717.3 + 215.2) / (871.7 + 261.5), 4),
        "무": round((154.4 + 46.3) / (871.7 + 261.5), 4),
    }


# ══════════════════════════════════════════════════════════════════════
#  ④ 판매 규칙 — 객체 또는 목록
# ══════════════════════════════════════════════════════════════════════

#: 🟢 **FINAL 규칙 파일 예시** (재무·판매 확정 2026-09-14). `sim_run_runner --backfill-rules`
#: 가 `config_json.backfill` 에 그대로 싣는 모양이다. 매입 규칙은 이 판의 결정 밖이라 뺐다.
FINAL_규칙 = {
    "sales": {"rule": ALWAYS_FIXED_TYPE, "scenario_type": "CONSERVATIVE"},
    "sales_terms": [
        {
            "partner_id": 기존,
            "payment_terms_type": "SINGLE",
            "payment_days": 30,
            "unit_price_source": ML_CURRENT_PRICE,
        },
        {
            "partner_id": 신규,
            "payment_terms_type": "SINGLE",
            "payment_days": 30,
            "unit_price_source": ML_CURRENT_PRICE,
        },
    ],
}


def test_객체_규칙은_종전_그대로_하나로_읽힌다():
    """★ 기존 실행(V13 등)은 객체다. 모양도 타입도 안 바뀐다."""
    읽은 = read_rules({"backfill": {"sales_terms": FINAL_규칙["sales_terms"][0]}}).sales_terms

    assert 읽은 == SalesTermsRule(
        partner_id=기존,
        payment_terms_type="SINGLE",
        payment_days=30,
        unit_price_source=ML_CURRENT_PRICE,
    )


def test_목록_규칙은_적힌_순서_그대로_읽힌다():
    읽은 = read_rules({"backfill": FINAL_규칙}).sales_terms

    assert isinstance(읽은, tuple)
    assert [one.partner_id for one in 읽은] == [기존, 신규]


def test_빈_목록은_터진다():
    with pytest.raises(BackfillRuleMissing, match="비었다"):
        read_rules({"backfill": {"sales_terms": []}})


def test_거래처가_겹치는_목록은_터진다():
    겹침 = [FINAL_규칙["sales_terms"][0], FINAL_규칙["sales_terms"][0]]
    with pytest.raises(BackfillRuleMissing, match="겹친다"):
        read_rules({"backfill": {"sales_terms": 겹침}})


def test_목록_원소도_네_칸이_전부_서야_한다():
    빠짐 = [FINAL_규칙["sales_terms"][0], {"partner_id": 신규}]
    with pytest.raises(BackfillRuleMissing):
        read_rules({"backfill": {"sales_terms": 빠짐}})


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 하루 — 그날 유효한 거래처마다 적힌 순서로 판단 → 승인
# ══════════════════════════════════════════════════════════════════════

ITEMS = ("배추", "무")
실행 = "SIM-TEST-PARTNER-DATES"
유효일 = {기존: 번인_시작, 신규: 유입일}


@dataclass
class _Out:
    status: str
    reason: str = ""
    end_code: str | None = None


def _터짐(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("대역: 부르지 않는 단계")


class _기록:
    """판매·승인이 불린 순서를 한 줄로 모은다."""

    def __init__(self) -> None:
        self.사건: list[tuple[str, Any]] = []

    def sales(self, request, verifier=None, **_k: Any):
        self.사건.append(("판매", (request.partner_id, request.item, request.request_id)))
        return _Out(status="RAN", end_code="SL1_PRESENTED")

    def approve(self, *, sim_run_id: str, start: date, end: date, runs_on: Any) -> BackfillOut:
        self.사건.append(("승인", start))
        return BackfillOut(sim_run_id=sim_run_id, start=start, end=end, status="RAN")

    @property
    def 판매(self) -> list[tuple[str | None, str, str]]:
        return [값 for 종류, 값 in self.사건 if 종류 == "판매"]


def _유효_거래처(as_of: date, partner_ids: Any) -> frozenset[str]:
    return frozenset(p for p in partner_ids if 유효일.get(p, date.max) <= as_of)


def _준비(as_of: date) -> DayForecastReadiness:
    return DayForecastReadiness(
        as_of=as_of,
        readiness="ALL_READY",  # type: ignore[arg-type]
        items=tuple(
            ItemForecastGate(item=item, as_of=as_of, readiness="READY", grade="MEASURED")  # type: ignore[arg-type]
            for item in ITEMS
        ),
    )


class _달력:
    def is_market_open(self, as_of: date) -> bool:
        return True


class _배치:
    def has_ml_batch(self, day: date) -> bool:
        return True


def _하루(as_of: date, **over: Any):
    기록 = _기록()
    action = plan_next_action(
        now=datetime(as_of.year, as_of.month, as_of.day, 9, 30, tzinfo=SEOUL),
        as_of=as_of,
        calendar=_달력(),
        ml_batch=_배치(),
        gate_result=_준비(as_of),
    )
    인자: dict[str, Any] = {
        "open_day_fn": lambda *a, **k: _Out("OPENED"),
        "retry_fn": _터짐,
        "receive_fn": lambda *a, **k: _Out("RECEIVED"),
        "inspect_fn": _터짐,
        "issue_fn": lambda *a, **k: _Out("ISSUED"),
        "collect_fn": lambda *a, **k: _Out("COLLECTED"),
        "procure_fn": lambda *a, **k: _Out("RAN"),
        "sales_fn": 기록.sales,
        "outbound_fn": lambda *a, **k: _Out("NOTHING_DUE"),
        "close_fn": lambda *a, **k: _Out("CLOSED"),
        "sim_run_id": 실행,
        "items": ITEMS,
        "approve_fn": 기록.approve,
        "partners_active_fn": _유효_거래처,
    }
    인자.update(over)
    outcome = run_scheduled_day(action, **인자)
    return 기록, outcome


def _목록() -> tuple[SalesTermsRule, ...]:
    읽은 = read_rules({"backfill": FINAL_규칙}).sales_terms
    assert isinstance(읽은, tuple)
    return 읽은


def test_유입일_전날에는_새_거래처_판매_호출이_0이다():
    기록, outcome = _하루(date(2026, 9, 13), sales_terms=_목록())

    assert [p for p, _, _ in 기록.판매] == [기존, 기존]
    assert outcome.sales_status == "RAN"


def test_유입일부터_목록_순서대로_판매를_묻는다():
    """🔴 **변이 대상.** 목록 순서를 뒤집으면 여기가 빨개진다.

    ★ 재고를 먼저 잡는 쪽이 목록 앞(기존 거래처)이다.
    """
    기록, _ = _하루(유입일, sales_terms=_목록())

    assert [(p, item) for p, item, _ in 기록.판매] == [
        (기존, "배추"),
        (기존, "무"),
        (신규, "배추"),
        (신규, "무"),
    ]


def test_앞_거래처_승인이_뒤_거래처_판단보다_먼저다():
    """★★ 뒤 거래처의 판단이 앞 거래처가 잡은 재고를 봐야 한다."""
    기록, outcome = _하루(유입일, sales_terms=_목록(), auto_approve=True)

    종류 = [kind for kind, _ in 기록.사건]
    # 매입 승인 → 기존 판매 둘 → 기존 승인 → 신규 판매 둘 → 신규 승인
    assert 종류 == ["승인", "판매", "판매", "승인", "판매", "판매", "승인"]
    assert outcome.sales_approval_status == "RAN"
    assert any(f"판매 승인({기존})" in note for note in outcome.notes)
    assert any(f"판매 승인({신규})" in note for note in outcome.notes)


def test_거래처별_업무_키가_안_겹친다():
    기록, _ = _하루(유입일, sales_terms=_목록())

    keys = [key for _, _, key in 기록.판매]
    assert len(set(keys)) == len(keys)
    assert all(key.endswith(f"-{p}") for p, _, key in 기록.판매)


def test_객체_규칙은_거래처_날짜를_안_묻고_업무_키도_종전_그대로다():
    """★ 기존 실행 호환. 조회 함수가 불리면 터진다."""
    객체 = _목록()[0]

    def 묻지마(*_a: Any) -> Any:
        raise AssertionError("객체 규칙인데 유효 거래처를 물었다")

    기록, outcome = _하루(유입일, sales_terms=객체, partners_active_fn=묻지마)

    assert [key for _, _, key in 기록.판매] == [
        scheduler.daily_sales_request_id(유입일, item, sim_run_id=실행) for item in ITEMS
    ]
    assert all(p == 기존 for p, _, _ in 기록.판매)
    assert "판매: RAN (2품목)" in outcome.notes


def test_유효_거래처_조회가_터지면_아무에게도_안_판다():
    """🔴 fail-closed. 조용히 0건이 아니라 거래처 × 품목마다 FAILED 로 남는다."""

    def 터진다(*_a: Any) -> Any:
        raise RuntimeError("커넥션 없음")

    기록, outcome = _하루(유입일, sales_terms=_목록(), partners_active_fn=터진다)

    assert 기록.판매 == []
    assert outcome.sales_status == "FAILED"
    assert len(outcome.sales_items) == 2 * len(ITEMS)
    assert all("커넥션 없음" in (one.reason or "") for one in outcome.sales_items)
