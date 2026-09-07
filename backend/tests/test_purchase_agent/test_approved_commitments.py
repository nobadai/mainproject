"""어제 승인분을 봉투에서 받는다 — **받기만 하고, 받았다는 것을 잰다** (`#310` · `#312`).

매입이 *"어제 승인 때문에 창고 여유가 줄었다"* 를 쓸 근거가 없었다. 숫자는 이어지는데
(``cap_by_date`` 7,645.6 → 4,058.6) **무엇이** 그 3,587kg 인지가 봉투에 없었다.
마스터가 `#312` 로 실어 주면서 받는 쪽이 생겼다.

🔴 **``None`` 과 ``[]`` 가 다른 사실이다.** 마스터가 *"없으면 칸을 안 만든다"* 로 보내므로
  (`flow.py._commitments_block`), 받는 쪽에서 ``or []`` 로 접으면 **보내는 쪽이 지킨
  구분이 여기서 사라진다** (규칙 3).

⚠️ **문장을 넓히는 것은 이 판이 아니다.** *"어제 승인분 3,587kg 이 01-07 에 온다"* 는
  ⑥ ``_warehouse_rationale`` 의 몫이고 그 함수는 `#332` 에 있다. 여기까지는 **받는
  자리**뿐이다.
"""

import ast
from datetime import date
from pathlib import Path

import pytest

from app.purchase_agent.adapter import build_state, validate_payload

# ★ **봉투를 다시 짓지 않는다.** ``test_adapter.py`` 가 이미 mock 포트로 정상 payload 를
#   만든다 — 여기서 또 지으면 필수 키 목록이 두 곳이 되고, 어댑터가 요구 사항을 늘리는
#   날 이 파일만 조용히 낡는다.
from tests.test_purchase_agent.test_adapter import _payload, _request

#: 마스터 실측 (`#310` 회신 · ``as_of=2026-01-08`` 배추). 이 모양이 실제로 온다.
COMMITMENT = {
    "approval_id": "H1-THRU-20260105-BAECHU-1",
    "item": "배추",
    "scenario_label": "기본",
    "total_qty_kg": 3587.0,
    "total_amount_krw": 3063298.0,
    "inbound_lead_days": 2.0,
    "first_arrival": "2026-01-07",
    "arrival_schedule": [
        {
            "item": "배추",
            "qty_kg": 3587.0,
            "arrival_date": "2026-01-07",
            "purchase_date": "2026-01-05",
            "seq": 1,
        }
    ],
}

_NODES = Path(__file__).resolve().parents[2] / "app" / "purchase_agent" / "nodes"

#: 이 검사가 쓰는 앵커. mock 포트가 이 날짜로 3품목을 다 낸다.
AS_OF = date(2026, 8, 21)


def test_승인_약정이_state_에_실린다() -> None:
    """🔴 **`#310` 의 본문이다.** 전에는 봉투에 와도 어댑터가 버렸다."""
    state = build_state(_request("배추", AS_OF, approved_commitments=[COMMITMENT]))

    assert state.get("approved_commitments") == [COMMITMENT]


def test_안_오면_None_이지_빈_목록이_아니다() -> None:
    """🔴 *"마스터가 안 보냈다"* 와 *"어제 승인이 없었다"* 는 다른 사실이다 (규칙 3).

    ``or []`` 로 접으면 둘이 같아지고, 그러면 나중에 근거 문장이 **승인이 없었다고
    단정**하게 된다.
    """
    state = build_state(_request("배추", AS_OF))

    assert state.get("approved_commitments") is None, "안 온 것을 빈 목록으로 접었다"


def test_빈_목록이_오면_빈_목록이다() -> None:
    """반대 방향 — 마스터가 ``[]`` 를 보내면 그것도 사실이라 ``None`` 으로 접지 않는다.

    ⚠️ 지금 마스터는 ``[]`` 를 보내지 않는다 (`_commitments_block` 이 칸을 안 만든다).
      그 규칙이 바뀌는 날 **여기가 조용히 틀리지 않게** 두 갈래를 다 잠근다.
    """
    state = build_state(_request("배추", AS_OF, approved_commitments=[]))

    assert state.get("approved_commitments") == []


def test_온_그대로_나른다() -> None:
    """★ 마스터가 승인 이력을 해석하지 않고 싣듯, 우리도 고르거나 줄이지 않는다.

    ``arrival_schedule`` 이 통째로 남아야 *"N kg 이 D 에 온다"* 를 나중에 쓸 수 있다.
    """
    state = build_state(_request("배추", AS_OF, approved_commitments=[COMMITMENT]))
    carried = state["approved_commitments"][0]  # type: ignore[index]

    assert carried == COMMITMENT
    assert carried["arrival_schedule"][0]["arrival_date"] == "2026-01-07"


def test_사본이라_봉투를_건드리지_않는다() -> None:
    """State 를 고쳐도 원본 payload 가 안 바뀐다 — 마스터가 준 값은 우리 것이 아니다."""
    request = _request("배추", AS_OF, approved_commitments=[COMMITMENT])
    state = build_state(request)
    state["approved_commitments"][0]["total_qty_kg"] = 1.0  # type: ignore[index]

    assert request.payload["approved_commitments"][0]["total_qty_kg"] == 3587.0


def test_없어도_missing_data_에_안_들어간다() -> None:
    """★ 필수 입력이 아니다.

    필수로 걸면 **어제가 없는 첫날**이 통째로 ``RUNTIME_NOT_READY`` 가 된다. 없으면
    근거 문장 하나가 안 넓어질 뿐이라, 안은 그대로 만들어져야 한다.
    """
    missing = validate_payload(_payload("배추", AS_OF), AS_OF)

    assert not [name for name in missing if "approved_commitments" in name], missing


def test_아직_어느_노드도_안_읽는다() -> None:
    """🔴 **주석이 틀리는 순간을 여기서 잡는다** (규칙 8).

    ``state.py`` 와 ``adapter.py`` 가 *"지금은 받기만 한다"* 라고 적어 두었다. 그
    문장은 **읽기 시작하는 순간 틀린다.**

    ``adjustments`` 가 정확히 그렇게 틀렸다 — *"어느 노드도 아직 안 읽는다"* 를 적은
    바로 그 판에서 이미 두 곳이 읽고 있었고, 문장은 **쓴 순간부터** 틀린 채 남았다
    (2026-09-03 정정).

    ⚠️ **이 검사가 우는 것은 결함이 아니다.** ⑥이 *"어제 승인분 N kg 이 D 에 온다"*
      를 쓰기 시작하면 울고, 그때 **이 검사와 두 주석을 같이 고치는 것**이 맞다
      (`#332` 뒤). 지우지 말고 갱신할 것.
    """
    readers = [
        path.name
        for path in sorted(_NODES.glob("*.py"))
        if "approved_commitments" in path.read_text(encoding="utf-8")
    ]

    assert readers == [], (
        f"노드가 approved_commitments 를 읽기 시작했다: {readers}. "
        "state.py·adapter.py 의 «지금은 받기만 한다» 주석과 이 검사를 같이 고칠 것"
    )


def test_받는_줄이_adjustments_옆에_있다() -> None:
    """★ 두 값이 **같은 성격**이라 같은 자리에 둔다 — 마스터가 실어 주고 우리가 아직
    안 쓰는 값이다.

    🔴 **문자열이 아니라 구문으로 본다** (규칙 8). 주석이나 docstring 에 이름이
      적혀 있는 것으로는 *"실제로 그 dict 에 담기는가"* 를 증명하지 못한다.
    """
    source = (_NODES.parent / "adapter.py").read_text(encoding="utf-8")
    keys = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "approved_commitments" in keys
    assert "adjustments" in keys


@pytest.mark.parametrize("bad", ["3587kg", 3587, [{"approval_id": "X"}, "낱말"]])
def test_목록이_아닌_값은_그대로_터진다(bad: object) -> None:
    """⚠️ **모양을 검사하지 않는다** — 조용히 삼키면 봉투가 틀린 것을 아무도 모른다.

    ``missing_data`` 로 세우지 않는 이유는 이것이 *"사용자가 채울 수 있는 값"* 이
    아니기 때문이다. 그 목록은 **마스터가 다시 보내면 풀리는 것**들인데, 약속과 다른
    모양은 다시 보내도 같은 모양으로 온다 — 계약 위반이라 **터지는 편이 낫다.**

    ⚠️ 예외 종류를 좁히지 않는다. ``dict("3")`` 은 ``ValueError`` 고 ``dict(3587)`` 은
      ``TypeError`` 라, 하나로 못 박으면 **입력에 따라 검사가 통과했다 말았다** 한다.
      우리가 잠그는 것은 *"조용히 넘어가지 않는다"* 이지 예외 이름이 아니다.
    """
    with pytest.raises((TypeError, ValueError)):
        build_state(_request("배추", AS_OF, approved_commitments=bad))
