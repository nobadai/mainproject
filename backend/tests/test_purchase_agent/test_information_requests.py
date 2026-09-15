"""부족 정보 요청 (E3-11) — **정본이 하나인가**를 잠근다.

🔴 이 파일이 재는 것은 값이 아니라 **정본의 개수**다. 같은 사실이 「사람이 읽는 문장」과
「마스터가 읽는 구조」 둘로 나가는데, 판정이 두 벌이면 한쪽만 고치는 날 조용히 갈린다.

★ 그래서 짝 검사와 변이가 핵심이다 — 표에서 한 사유를 지우면 **둘이 같이 사라져야**
한다. 하나만 사라지면 그건 정본이 둘이라는 뜻이다.
"""

from datetime import date

import pytest

from app.purchase_agent import features
from app.purchase_agent import information_requests as ir
from app.purchase_agent.graph import build_graph
from app.purchase_agent.state import build_initial_state

ITEM = "배추"
AS_OF = date(2026, 8, 21)

#: 🔴 **이 판 이전에 나가던 문장 그대로다.** 문면을 안 바꾸는 것이 이 기능의 조건이라
#: 값으로 못박는다 — 여기는 설정을 읽는지 보는 자리가 아니라 **과거 산출물과의 대조**다
#: (규칙 8 이 금지하는 "값 비교"는 선언을 읽는지 재는 자리의 이야기고, 이쪽은 다르다).
옛문장 = {
    "INBOUND_LEAD_NOT_DECLARED": (
        (
            "입고일 기준 창고 점유 검사 보류 — 물류 입고 소요일이 미확정이라 "
            "회차별 도착일을 계산하지 않는다"
        ),
        (
            "보유 재고를 뺀 창과 매입이 덮는 창이 맞는지 확인 보류 — 물류 입고 "
            "소요일이 미확정이라 매입분 도착일을 놓지 못한다"
        ),
    ),
    "PAYMENT_LEAD_NOT_DECLARED": (
        (
            "지급일 기준 현금 검사 보류 — 재무 대금 지급 소요일이 미확정이라 "
            "회차별 지급일을 계산하지 않는다"
        ),
    ),
}


def _모든사유() -> tuple[ir.MissingInfo, ...]:
    return ir.missing_information(
        split_entry_unknown="분할 진입을 판정하지 못했다",
        free_stock_unknown="가용재고를 못 받았다",
        inbound_lead_missing=True,
        holdings_deducted=True,
        payment_lead_missing=True,
    )


def test_문면이_이_판_이전과_바이트가_같다() -> None:
    """🔴 **문면을 안 바꾼다** — 화면에 뜨는 문장이고 걷기 대조의 기준이다."""
    for info in _모든사유():
        if info.reason_code in 옛문장:
            assert info.risk_sentences == 옛문장[info.reason_code]


def test_받은_사유는_그대로_실어_보낸다() -> None:
    """분할 진입·가용재고는 **부르는 쪽이 만든 문장**이라 여기서 다시 짓지 않는다.

    ⚠️ 여기서 문장을 새로 지으면 같은 사실의 문면이 두 곳에서 나오게 된다.
    """
    사유 = {i.reason_code: i.risk_sentences for i in _모든사유()}
    assert 사유["ARRIVAL_CAPACITY_NOT_CHECKED"] == ("분할 진입을 판정하지 못했다",)
    assert 사유["FREE_STOCK_NOT_RECEIVED"] == ("가용재고를 못 받았다",)


def test_짝_검사_요청이_선_사유는_문장도_선다() -> None:
    """🔴 **정본이 하나인가** — 둘이 같은 판정에서 나오는지 본다."""
    사유 = _모든사유()
    요청코드 = {r["reason_code"] for r in ir.to_requests(사유)}
    문장코드 = {i.reason_code for i in 사유 if i.risk_sentences}
    assert 요청코드 == 문장코드


@pytest.mark.parametrize("지울_코드", list(ir.ROUTING))
def test_변이_표에서_한_사유를_빼면_요청이_같이_사라진다(
    monkeypatch: pytest.MonkeyPatch, 지울_코드: str
) -> None:
    """🔴 **변이로 잰다** (규칙 8). 값을 대조하면 코드가 같은 값을 들고 있어도 통과한다.

    표에서 한 줄을 지웠는데 요청이 그대로 서면, 그 요청은 **표를 안 읽고 있다.**
    """
    남긴_표 = {k: v for k, v in ir.ROUTING.items() if k != 지울_코드}
    monkeypatch.setattr(ir, "ROUTING", 남긴_표)
    요청 = ir.to_requests(_모든사유())
    assert 지울_코드 not in {r["reason_code"] for r in 요청}
    assert len(요청) == len(남긴_표)  # 🔴 ir.ROUTING 은 이미 줄어든 표다 — 그걸로 세면 안 는다


def test_같은_누락_하나가_요청_하나에_막힌_검사_둘로_나간다() -> None:
    """``cap_by_date`` 가 없으면 ①의 분할 진입과 ⑦의 도착일 수용량이 **같이** 막힌다.

    ★ 사유를 둘로 쪼개면 받는 쪽이 **두 번 청해야 하는 줄로 읽는다.**
    """
    요청 = ir.to_requests(
        ir.missing_information(
            split_entry_unknown="여유를 못 봤다",
            free_stock_unknown=None,
            inbound_lead_missing=False,
            holdings_deducted=False,
            payment_lead_missing=False,
        )
    )
    assert len(요청) == 1
    assert 요청[0]["blocked_checks"] == ["ARRIVAL_CAPACITY", "SPLIT_ENTRY"]
    assert 요청[0]["required_field"] == "constraints.inventory.cap_by_date"


def test_정렬과_중복제거가_결정적이다() -> None:
    """같은 입력이면 **같은 순서**다 — 집합 순회나 해시 순서에 기대면 실행마다 달라진다."""
    사유 = _모든사유()
    assert ir.to_requests(사유) == ir.to_requests(사유)
    assert [r["reason_code"] for r in ir.to_requests(사유)] == list(ir.ROUTING)


def test_요청은_받는_쪽이_있는_것만_낸다() -> None:
    """🔴 자기에게 청하는 요청은 요청이 아니다 — ``purchase`` 가 어휘에 없다."""
    받는쪽 = {길.requested_from for 길 in ir.ROUTING.values()}
    assert "purchase" not in 받는쪽
    assert 받는쪽 <= {"logistics", "finance", "sales", "ml", "master"}


def test_못_하는_동작을_계약에_안_적는다() -> None:
    """마스터는 부분 재진입을 안 한다 — 값이 오면 에이전트를 처음부터 다시 부른다.

    ``resume_from`` 을 적으면 받는 쪽이 **있는 기능으로 읽는다.**
    """
    assert ir.RERUN_SCOPE == "FULL_AGENT"
    for 요청 in ir.to_requests(_모든사유()):
        assert "resume_from" not in 요청
        assert 요청["rerun_scope"] == "FULL_AGENT"


def test_종료된_협의는_요청_어휘에_없다() -> None:
    """🔴 **`#390` 은 닫혔다.** 신선도 보관한계를 요청으로 내보내면 **닫은 협의가 매일
    되살아난다.** 물류가 «추가 변경하지 않겠다» 로 종료했고 우리도 청하지 않기로 정했다.

    ⚠️ 고지(``risks``)로는 계속 나간다 — 「안 청한다」와 「안 말한다」는 다르다.
    """
    어휘 = " ".join(ir.ReasonCode.__args__) + " ".join(
        길.required_field for 길 in ir.ROUTING.values()
    )
    assert "SHELF" not in 어휘
    assert "보관한계" not in 어휘


def test_시장의_사실은_요청이_아니다() -> None:
    """*"그날 시세에 상·중 등급이 없었다"* 는 **아무도 줄 수 없다.**

    요청으로 내보내면 못 받을 것을 매일 청하게 된다 — 「누가 주면 풀리는 것」만 요청이다.
    """
    어휘 = " ".join(ir.ReasonCode.__args__)
    assert "GRADE" not in 어휘


def _제안(*, 켬: bool, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(
        features, "enabled", lambda key, default=False: 켬 and key == features.INFORMATION_REQUESTS
    )
    from app.purchase_agent.nodes import self_check as sc

    monkeypatch.setattr(sc, "enabled", lambda key, default=False: 켬)
    state = build_initial_state(ITEM, AS_OF)
    return build_graph().invoke(state)["proposal"]


def test_플래그를_꺼도_고지는_그대로_나간다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **착수 조건 ⑤ — 플래그가 끄는 것은 구조화 출력뿐이다.**

    플래그로 고지를 없애면 「못 판정했다」가 조용히 사라지고, 규칙 3(*"0으로 채우지
    않는다"*)이 **출력 층에서** 깨진다.
    """
    껐을_때 = _제안(켬=False, monkeypatch=monkeypatch)
    켰을_때 = _제안(켬=True, monkeypatch=monkeypatch)
    끈_risks = [안["risks"] for 안 in 껐을_때["scenarios"]]
    켠_risks = [안["risks"] for 안 in 켰을_때["scenarios"]]
    assert 끈_risks == 켠_risks


def test_꺼진_실행에는_키가_아예_없다(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 키만 생겨도 **이 기능을 넣기 전과 바이트가 달라진다** — 회귀 게이트가 죽는다."""
    assert "information_requests" not in _제안(켬=False, monkeypatch=monkeypatch)


def test_한_모델에_직렬화기가_하나뿐이다() -> None:
    """🔴 **둘 달면 뒤엣것이 이기고 앞엣것이 조용히 죽는다.**

    이 판에서 실제로 밟았다 — ``information_requests`` 를 빼는 직렬화기를 따로 달았더니
    ``_omit_null_no_proposal_reason`` 이 이겨서 **키가 안 빠졌다.** 그런데
    ``hasattr`` 로 보면 「직렬화기가 있다」로 보이고, pydantic 도 등록은 해 준다.

    ⚠️ 다음에 또 «빼고 싶은 키» 가 생기면 **기존 하나에 줄을 더한다.** 새로 달면
    지금 빠지는 것이 그날부터 안 빠지고, 그 사실은 산출물을 바이트로 대조해야 보인다.
    """
    from app.purchase_agent.schemas import PurchaseProposal, Scenario

    for 모델 in (PurchaseProposal, Scenario):
        직렬화기 = list(모델.__pydantic_decorators__.model_serializers)
        assert len(직렬화기) <= 1, f"{모델.__name__} 에 직렬화기가 {직렬화기}"
