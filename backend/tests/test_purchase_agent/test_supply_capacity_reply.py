"""추가공급 회신이 **봉투와 판매 계약을 둘 다 통과하는가** (E4-7).

`test_supply_capacity.py` 는 계산을 잰다. 이 파일은 **회신 모양**을 잰다::

    · 봉투 `validate_reply` findings 가 0 인가 (근거 규칙을 우리가 재구현하지 않는다)
    · 판매 `PurchaseAdditionalSupplyResult` 로 그대로 파싱되는가
    · 7노드를 안 도는가 — 경계만 내겠다고 답한 것이 지켜지는가
    · 못 받은 것을 `0` 으로 채우지 않는가

★★ **봉투가 열렸다** (마스터 2026-09-10). 전에는 `_AGENT_MODES["purchase"]` 에 그
  mode 가 없어 정상 경로로는 요청을 만들 수조차 없었고, 그래서 `opened` 픽스처가
  **그날을 미리 살아 보는** 자리였다. 이제 실제로 열려 있어 그 픽스처는 아무것도
  안 바꾼다 — 남겨 두어도 결과가 같아서 이 판에서는 안 건드린다.

  ⚠️ 같이 있던 `test_봉투는_아직_이_mode_를_안_연다` 는 **이 파일이 «그날 지운다» 고
    적어 둔 검사**라 지웠다. 봉투가 그 mode 를 받는다는 사실은 이제
    `tests/master/test_sales_additional_supply.py` 가 잰다.
"""

from datetime import date

import pytest

from app.master import envelope
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply
from app.purchase_agent.adapter import purchase_port
from app.sales.schemas import PurchaseAdditionalSupplyResult

#: mock 앵커여야 시세가 나온다 (`mocks/scenarios.json`).
AS_OF = date(2026, 9, 11)
ITEM = "배추"


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """마스터가 `_AGENT_MODES` 를 여는 날을 미리 살아 본다."""
    monkeypatch.setitem(
        envelope._AGENT_MODES,
        "purchase",
        envelope._AGENT_MODES["purchase"] | {"SUPPLY_CAPACITY_QUERY"},
    )


def _request(**payload) -> AgentRequest:
    body = {"item": ITEM, **payload}
    return AgentRequest(
        context=ExecutionContext("R-SUPPLY", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="SUPPLY_CAPACITY_QUERY",
        payload=body,
    )


# ---------------------------------------------------------------------------
# 1. 🔴 봉투 검증 — 규칙을 재구현하지 않고 실제로 돌린다
# ---------------------------------------------------------------------------


def test_재료가_없는_회신도_봉투를_통과한다(opened):
    """지금 실제로 오는 모양이다 — 마스터가 경계를 아직 안 싣는다."""
    reply, metadata = purchase_port(_request())

    findings = validate_reply(_request(), reply, metadata)

    assert [f.code for f in findings] == [], [f"{f.code} {f.where} {f.detail}" for f in findings]
    assert reply.runtime_status == "READY"


def test_재료가_다_있는_회신도_봉투를_통과한다(opened):
    payload = {"warehouse_free_kg": 5000, "finance_cap_amount_krw": 3_000_000}
    request = _request(**payload)

    reply, metadata = purchase_port(request)
    findings = validate_reply(request, reply, metadata)

    assert [f.code for f in findings] == [], [f"{f.code} {f.where} {f.detail}" for f in findings]
    assert reply.payload["procurable_quantity_kg"] is not None


def test_부족량까지_실린_회신도_봉투를_통과한다(opened):
    request = _request(
        warehouse_free_kg=5000,
        finance_cap_amount_krw=3_000_000,
        required_additional_quantity_kg=1200,
    )

    reply, metadata = purchase_port(request)
    findings = validate_reply(request, reply, metadata)

    assert [f.code for f in findings] == [], [f"{f.code} {f.where} {f.detail}" for f in findings]
    assert reply.payload["requested_quantity_kg"] == 1200


# ---------------------------------------------------------------------------
# 2. 판매 계약 — 우리가 낸 것을 판매가 읽을 수 있나
# ---------------------------------------------------------------------------


def test_판매가_우리_회신을_그대로_읽는다(opened):
    """`extra="ignore"` 라 우리가 더 실은 칸(`basis`·`unit_price_grade`)은 무시된다."""
    reply, _ = purchase_port(_request(warehouse_free_kg=800, finance_cap_amount_krw=9_000_000))

    parsed = PurchaseAdditionalSupplyResult.model_validate(dict(reply.payload))

    assert parsed.procurable_quantity_kg == 800
    assert parsed.available_date is None
    assert parsed.risks  # 🔴 비면 판매가 「위험 없음」으로 읽는다


def test_못_받은_날에도_판매_계약을_지킨다(opened):
    reply, _ = purchase_port(_request())

    parsed = PurchaseAdditionalSupplyResult.model_validate(dict(reply.payload))

    assert parsed.procurable_quantity_kg is None  # 0 이 아니다 (규칙 3)
    assert any("둘 다 받지 못했다" in r for r in parsed.risks)


def test_available_date_가_null_인_이유를_한_줄로_말한다(opened):
    """*"값 칸만 null 로 정직하고 위험 칸이 거짓말을 한다"* 를 막는다 (마스터 §4.2)."""
    reply, _ = purchase_port(_request(warehouse_free_kg=5000, finance_cap_amount_krw=3_000_000))

    assert reply.payload["available_date"] is None
    assert any("언제 댈 수 있는지" in r for r in reply.payload["risks"])


# ---------------------------------------------------------------------------
# 3. 경계만 낸다 — 7노드를 안 돈다
# ---------------------------------------------------------------------------


def test_그래프를_한_번도_안_돈다(opened, monkeypatch: pytest.MonkeyPatch):
    """🔴 우리가 약속한 것이다 — 완주는 평균 11.2초 · 최대 136.6초다.

    판매 사이클이 그 시간을 기다린다. 그래프를 부르면 여기서 터진다.
    """
    from app.purchase_agent import adapter

    def _boom(*args, **kwargs):
        raise AssertionError("경계만 내는 경로가 7노드 그래프를 불렀다")

    monkeypatch.setattr(adapter, "build_graph", _boom)

    reply, metadata = purchase_port(
        _request(warehouse_free_kg=5000, finance_cap_amount_krw=3_000_000)
    )

    assert reply.runtime_status == "READY"
    assert "scenarios" not in reply.payload
    # 실행 계획이 **노드 이름이 아니다** — 그래프를 안 돌았다는 증거이기도 하다.
    assert metadata.used_tools == ("get_market_quotes", "compute_supply_capacity")


def test_안을_만들지_않는다(opened):
    """판매 사이클 안에서 매입안을 만들지 않는다 — 셋의 합의다."""
    reply, _ = purchase_port(_request(warehouse_free_kg=5000, finance_cap_amount_krw=3_000_000))

    assert "scenarios" not in reply.payload
    assert "split_plan" not in reply.payload
    assert "sourcing_plan" not in reply.payload


# ---------------------------------------------------------------------------
# 4. 무엇을 묻는지 모를 때
# ---------------------------------------------------------------------------


def test_품목이_없으면_안_돌았다고_답한다(opened):
    request = AgentRequest(
        context=ExecutionContext("R-SUPPLY", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="SUPPLY_CAPACITY_QUERY",
        payload={},
    )

    reply, _ = purchase_port(request)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("item",)
    assert reply.payload == {}  # 반쪽짜리 결과를 안 싣는다


def test_품목_하나짜리_목록은_받아_준다(opened):
    """부르는 쪽 모양이 아직 안 정해졌다. 하나는 뜻이 같으므로 막지 않는다."""
    request = AgentRequest(
        context=ExecutionContext("R-SUPPLY", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="SUPPLY_CAPACITY_QUERY",
        payload={"items": [ITEM]},
    )

    reply, _ = purchase_port(request)

    assert reply.runtime_status == "READY"
    assert reply.payload["item"] == ITEM


def test_여러_품목이_오면_한_품목만_답한다고_말한다(opened):
    """🔴 판매 계약이 최상위 하나라 어느 것을 둘지 우리가 못 정한다 — 숨기지 않는다."""
    request = AgentRequest(
        context=ExecutionContext("R-SUPPLY", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="SUPPLY_CAPACITY_QUERY",
        payload={"items": ["배추", "무"]},
    )

    reply, _ = purchase_port(request)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("item",)


# ---------------------------------------------------------------------------
# 5. 규칙 3 — 회신 층에서도 `0` 과 `None` 이 안 섞인다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"warehouse_free_kg": 0, "finance_cap_amount_krw": 3_000_000}, 0),
        ({"warehouse_free_kg": 5000, "finance_cap_amount_krw": 0}, 0),
        ({"warehouse_free_kg": 0, "finance_cap_amount_krw": 0}, 0),
    ],
)
def test_0은_읽은_값이라_0kg_을_답한다(opened, payload, expected):
    reply, _ = purchase_port(_request(**payload))

    assert reply.payload["procurable_quantity_kg"] == expected
    assert reply.payload["basis"] != "unknown"


@pytest.mark.parametrize("key", ["warehouse_free_kg", "finance_cap_amount_krw"])
def test_한쪽만_와도_확정하지_않는다(opened, key):
    reply, _ = purchase_port(_request(**{key: 1000}))

    assert reply.payload["procurable_quantity_kg"] is None
    assert reply.payload["basis"] == "unknown"


def test_참이나_거짓은_숫자로_안_읽는다(opened):
    """`True` 를 `1` 로 읽으면 창고 여유 1kg 이 된다 — 다른 파트에서 실제로 난 사고다."""
    reply, _ = purchase_port(_request(warehouse_free_kg=True, finance_cap_amount_krw=3_000_000))

    assert reply.payload["procurable_quantity_kg"] is None


# ---------------------------------------------------------------------------
# 6. 단가 — 나눗셈과 회신이 같은 수
# ---------------------------------------------------------------------------


def test_회신_단가로_나눈_값이_회신_수량과_같다(opened):
    """🔴 마스터 조건이다 — 두 값이 갈리면 어느 쪽이 참인지 아무도 말해 주지 않는다."""
    budget = 3_000_000
    reply, _ = purchase_port(_request(warehouse_free_kg=10**9, finance_cap_amount_krw=budget))

    unit = reply.payload["expected_unit_price_krw"]
    assert reply.payload["procurable_quantity_kg"] == budget // unit


def test_어느_등급에서_온_단가인지_밝힌다(opened):
    """값으로 골랐다는 사실이 숫자만 봐서는 안 보인다."""
    reply, _ = purchase_port(_request(warehouse_free_kg=5000, finance_cap_amount_krw=3_000_000))

    assert reply.payload["unit_price_grade"] in {"특", "상", "중", "하"}
