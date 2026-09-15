"""봉투의 **관측 기준시점**(`AgentReply.observed_at`) — 우리가 싣는 규칙 (`#393` · `#626`).

마스터가 2026-09-12 에 칸을 열었고(`#626`) 우리가 **첫 번째로 싣는 파트**다. 걷기 요약이
「실었다 / 안쟀다」 두 칸을 세는데, 이 파일이 지키는 것은 **그 두 칸이 서로를 삼키지 않게
하는 것**이다 — `None` 은 「안 쟀다」이고 값으로 메우면 안 잰 호출이 잰 호출로 세어진다.

🟡 **mock 시세에는 관측일 표기가 0건이다** (`observed_at` 가 *"mock 처럼 표기가 없으면
None"* 으로 스스로 규정한다). 그래서 **회귀 경로 전부가 그대로 `None`** 이고, 값이 나는
것은 실 DB 직독뿐이다 — 이 판이 작은 이유가 그것이다. 아래 검사들은 관측일을 **주입해서**
그 경로를 억지로 만든 것이다.

🔴 **값 비교로 가르지 않는다** (규칙 8). 「같은 값이 나온다」는 코드가 그 값을 하드코딩해도
통과한다. 그래서 **주입을 바꾸면 판정이 따라 바뀌는지**를 본다.
"""

from datetime import date, timedelta
from typing import Any

import pytest

from app.master.envelope import AgentRequest, ExecutionContext
from app.purchase_agent import ports
from app.purchase_agent.adapter import purchase_port
from app.purchase_agent.config import load_constraints
from app.purchase_agent.quotes import observed_at

# 통합 시연 앵커 — 재무·물류 mock 이 이 날에만 다 서 있다 (`#73`).
INTEGRATION = date(2025, 12, 31)
ITEMS = ("배추", "무", "양파")


def _payload(item: str, as_of: date) -> dict:
    extras = ports.get_snapshot_extras(item, as_of)
    return {
        "item": item,
        "constraints": {
            "finance": {
                "base_projected_cash_min": ports.get_projected_cash_min(as_of, 30),
                "margin_defense_floor_rate": 0.267,
                "finance_cap_amount_krw": 9_000_000,
                "purchase_payment_days": 7,
                "critical_payment_dates": [],
            },
            "inventory": ports.get_inventory(item, as_of),
        },
        "forecast": ports.get_forecast(item, as_of),
        "confirmed_orders": ports.get_confirmed_orders(item, as_of, days=14),
        "policy_values": {
            "contract_price_krw": extras["contract_price"],
            "item_mix_ratio": extras["item_mix_ratio"],
        },
    }


def _request(item: str, as_of: date, *, mode: str = "GENERATE_SCENARIOS", **over) -> AgentRequest:
    payload = over.pop("payload", None)
    return AgentRequest(
        context=ExecutionContext(f"REQ-{as_of.isoformat()}-{item}", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode=mode,  # type: ignore[arg-type]
        payload=payload if payload is not None else _payload(item, as_of),
    )


def _quote(observed: str, **over: Any) -> dict:
    """관측 표기가 **온전한** 시세 한 줄. 규격과 관측일은 한 묶음이다."""
    base = {
        "market": "가락",
        "grade": "특",
        "price": 824,
        "spec": "그물망·파렛트 10kg",
        "observed_at": observed,
    }
    return {**base, **over}


def _source(rows: list[dict]):
    return lambda item, as_of: rows


# ── ① 주입 축 — 주입을 바꾸면 판정이 따라 바뀐다 ─────────────────────────


@pytest.mark.parametrize("observed", ["2025-12-29", "2025-12-30"])
def test_the_observation_date_we_load_follows_the_injected_quote(observed: str) -> None:
    """🔴 **주입한 날짜가 그대로 봉투에 선다** — 서로 다른 두 날짜로 두 번 돈다.

    ★ 이것이 값 비교가 아닌 이유: 코드가 날짜를 하드코딩했으면 **둘 중 하나에서 깨진다.**
      한 날짜로만 보면 하드코딩과 구분이 안 된다 (규칙 8).
    """
    request = _request("배추", INTEGRATION)
    reply, _ = purchase_port(request, quotes=_source([_quote(observed)]))

    assert reply.observed_at == date.fromisoformat(observed)


def test_the_observation_date_is_a_date_not_a_string() -> None:
    """봉투가 ``ExecutionContext.as_of`` 와 **같은 ``date``** 로 규정한 칸이다.

    ``datetime`` 이나 문자열로 넓히면 비교 규칙이 둘로 갈려, 같은 사실이 어느 자리를
    지나느냐에 따라 룩어헤드가 됐다 안 됐다 한다 (봉투 주석).
    """
    reply, _ = purchase_port(
        _request("배추", INTEGRATION), quotes=_source([_quote("2025-12-30")])
    )

    assert isinstance(reply.observed_at, date)
    assert not isinstance(reply.observed_at, bool)


# ── ② 메움 금지 축 — 시세를 안 읽은 호출은 `None` 이다 ────────────────────


def test_a_status_query_did_not_observe_anything() -> None:
    """``STATUS_QUERY`` 는 시세를 **한 줄도 안 읽는다** — 「안 쟀다」가 사실이다.

    🔴 ``as_of`` 로 메우면 *"살아 있느냐"* 에 답한 호출이 **관측을 한 호출로** 세어진다.
    """
    request = _request("배추", INTEGRATION, mode="STATUS_QUERY", payload={})
    reply, _ = purchase_port(request)

    assert reply.observed_at is None
    assert reply.observed_at != request.context.as_of


def test_an_early_return_did_not_observe_anything() -> None:
    """필수 입력이 없어 **그래프를 돌기 전에** 돌아선 호출.

    시세를 읽는 자리(``build_state``)가 이 반환보다 **뒤**에 있어 관측이 없다.
    🔴 그날 시세가 실제로 존재했는지와 무관하다 — **우리가 안 봤다**.
    """
    request = _request("배추", INTEGRATION, payload={"item": "배추"})
    reply, _ = purchase_port(request, quotes=_source([_quote("2025-12-30")]))

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.observed_at is None
    assert reply.observed_at != request.context.as_of


def test_a_market_holiday_is_not_measured_either() -> None:
    """시세가 0건인 날(휴장). 죽지 않고 **`None` 으로 답한다** (규칙 3)."""
    reply, _ = purchase_port(_request("배추", INTEGRATION), quotes=_source([]))

    assert reply.observed_at is None


def test_the_mock_path_reports_nothing_measured() -> None:
    """🟡 **mock 은 표기가 없어 전부 `None` 이다** — 회귀가 안 움직이는 이유.

    이 검사가 있어야 *"값이 안 실린다"* 가 **고장이 아니라 mock 의 성질**이라는 것이
    기록으로 남는다. mock 에 관측일을 심는 날 이 검사가 먼저 운다.
    """
    for item in ITEMS:
        reply, _ = purchase_port(_request(item, INTEGRATION))
        assert reply.observed_at is None, item


# ── ③ 막힘 축 — 안 쓴 시세의 날짜는 싣지 않는다 (규칙 3) ──────────────────


def test_quotes_we_refused_do_not_leave_an_observation_date() -> None:
    """🔴 **관측일이 두 날 섞인 시세**는 우리가 판단에 쓰지 않는다 — 계보에도 안 싣는다.

    ★★ 여기가 이 파일의 핵심이다. ``observed_at`` 는 ``max(dates)`` 라 **그 상태에서도
      조용히 값을 낸다.** 그 값을 실으면 *"우리가 이 시점 기준으로 판단했다"* 가 되는데,
      그날 우리는 0안을 냈다 — 「모른다」를 날짜로 메우는 것이다.

    ⚠️ 그래서 같은 검사 안에서 **두 함수가 서로 다른 답을 낸다**는 것을 못 박는다.
      한쪽을 다른 쪽으로 갈아 끼우면 이 줄이 먼저 운다.
    """
    mixed = [
        _quote("2025-12-30", grade="상", price=800),
        _quote("2025-11-12", grade="중", price=600),
    ]
    reply, _ = purchase_port(_request("배추", INTEGRATION), quotes=_source(mixed))

    assert observed_at(mixed) == "2025-12-30"  # 그 함수는 값을 낸다
    assert reply.observed_at is None  # 🔴 우리는 안 싣는다


def test_half_written_provenance_leaves_no_observation_date() -> None:
    """규격은 있는데 관측일이 없는 시세 — 우리가 거부하는 상태다.

    ``observed_at`` 는 표기 없는 줄을 건너뛰어 ``None`` 을 내므로 두 축이 같은 답이다.
    """
    reply, _ = purchase_port(
        _request("배추", INTEGRATION),
        quotes=_source([{"market": "가락", "grade": "특", "price": 824,
                         "spec": "그물망·파렛트 10kg"}]),
    )

    assert reply.observed_at is None


def test_an_unparseable_observation_date_leaves_no_observation_date() -> None:
    """날짜로 읽을 수 없는 표기. 🔴 **죽지 않는다** — 그것도 「모른다」의 하나다."""
    reply, _ = purchase_port(
        _request("배추", INTEGRATION), quotes=_source([_quote("작년 말")])
    )

    assert reply.observed_at is None


# ── ④ 경계 축 — 우리 경계가 계약보다 하루 엄격하다 ────────────────────────


@pytest.mark.parametrize("shift", [0, 1])
def test_an_observation_on_or_after_as_of_is_never_loaded(shift: int) -> None:
    """★★ **계약은 ``<= as_of`` 를 허용하지만 우리는 ``== as_of`` 도 안 싣는다.**

    우리는 아침에 판정하고 경매는 저녁에 끝난다 — ``as_of`` 당일 관측은 우리가 돌던
    시각에 **존재하지 않았다**.

    🔴 남의 계약에 맞추려고 이 검사를 느슨하게 하지 않는다. ⇒ 그 덕에 마스터가 나중에
      ``observed_at > as_of`` 게이트를 열어도 **우리는 한 건도 안 걸린다.**
    """
    ahead = (INTEGRATION + timedelta(days=shift)).isoformat()
    reply, _ = purchase_port(_request("배추", INTEGRATION), quotes=_source([_quote(ahead)]))

    assert reply.observed_at is None


def test_what_we_load_is_always_strictly_before_as_of() -> None:
    """싣는 값은 **항상 ``< as_of``** 다 — 위 두 축을 한 문장으로 묶은 불변조건."""
    request = _request("배추", INTEGRATION)
    reply, _ = purchase_port(request, quotes=_source([_quote("2025-12-30")]))

    assert reply.observed_at is not None
    assert reply.observed_at < request.context.as_of


# ── ⑤ 계약 축 — 지금 모수가 하나다 (`#393` 이 기다리는 넷) ────────────────


def test_the_population_is_the_quote_alone_for_now() -> None:
    """🔴 봉투는 *"가장 늦은 것 · 하나라도 미지면 None"* 인데 **우리 모수가 하나**다.

    우리 입력 여섯 중 우리가 직접 관측하는 것은 시세뿐이고, 나머지 다섯은 봉투로 오는데
    **그 다섯에 ``observed_at`` 이 아직 없다** (`#393`).

    ★ 이 검사가 그 사실을 붙잡아 둔다 — 봉투 payload 에 남의 관측일을 심어도 우리가
      싣는 값은 **시세 것 그대로**다. 다섯이 오는 날 이 검사가 먼저 울고, 그때
      ``_observed_at`` 이 ``max(...)`` 로 바뀐다.
    """
    payload = _payload("배추", INTEGRATION)
    payload["constraints"]["inventory"]["observed_at"] = "2025-12-20"
    payload["constraints"]["finance"]["observed_at"] = "2025-12-15"

    reply, _ = purchase_port(
        _request("배추", INTEGRATION, payload=payload),
        quotes=_source([_quote("2025-12-30")]),
    )

    assert reply.observed_at == date(2025, 12, 30)


def test_the_supply_capacity_path_loads_it_too() -> None:
    """``SUPPLY_CAPACITY_QUERY`` 도 시세를 읽으므로 싣는다 — 경로가 둘인 것을 못 박는다.

    ⚠️ 그래프를 안 도는 경로라 **깔때기(`_reply`) 하나만 고쳐도 자동으로 되지 않는다.**
      호출부에서 넘겨야 하고, 그 사실을 이 검사가 잡는다.
    """
    request = _request(
        "배추",
        INTEGRATION,
        mode="SUPPLY_CAPACITY_QUERY",
        payload={"item": "배추", "required_additional_quantity_kg": 500},
    )
    reply, _ = purchase_port(request, quotes=_source([_quote("2025-12-30")]))

    assert reply.runtime_status == "READY"
    assert reply.observed_at == date(2025, 12, 30)


def test_the_same_quote_source_answers_both_the_gate_and_the_lineage() -> None:
    """🔴 **경계와 관측일이 같은 조회에서 나온다.**

    시세를 두 번 읽으면 그 사이 적재가 들어와, 막힘 판정은 옛 조회로 하고 관측일은 새
    조회에서 나올 수 있다. 그러면 *"쓰지 않기로 한 시세의 날짜"* 가 계보에 실린다.

    ★ 부르는 횟수를 세어 본다 — 임계는 ``constraints`` 가 정하므로 여기서 값을 안 적는다.
    """
    calls: list[tuple[str, date]] = []

    def counting(item: str, as_of: date) -> list[dict]:
        calls.append((item, as_of))
        return [_quote("2025-12-30")]

    request = _request(
        "배추",
        INTEGRATION,
        mode="SUPPLY_CAPACITY_QUERY",
        payload={"item": "배추", "required_additional_quantity_kg": 500},
    )
    reply, _ = purchase_port(request, quotes=counting)

    assert reply.observed_at == date(2025, 12, 30)
    assert len(calls) == 1, f"시세를 {len(calls)}번 읽었다 — 한 번이어야 한다"
    assert load_constraints()["market_quotes"], "임계 섹션이 선언에 있어야 한다"
