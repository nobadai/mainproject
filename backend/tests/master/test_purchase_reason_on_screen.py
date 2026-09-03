"""안이 없는 날 **매입 사유가 화면에 나온다** (2026-09-03).

🔴 **전에는 마스터 사유만 화면에 있었습니다.**

```text
마스터   유효한 안이 없어 제안을 내지 못했다      ← 무엇이 없어서인지는 안 말한다
매입     (응답에는 있는데 화면에는 없었다)
```

`report.py` 는 매입 사유를 쓰는데 `answer.py`(화면)는 안 썼습니다 — 마크다운
리포트를 여는 사람만 볼 수 있었습니다.

⚠️ **문서 부재는 더는 0안의 사유가 아닙니다** (2026-09-04 · 마스터 결정 — 문서 없으면
없이 진행). 그래서 예시 사유를 실제로 나올 수 있는 **가격 컷**으로 바꿨습니다 —
`no_proposal_reason` 이 무슨 문구든 화면에 그대로 올린다는 것을 재는 검사입니다.
"""

from __future__ import annotations

from app.master.answer import facts_from_procurement, render_answer
from app.master.schemas import ProcurementRunResponse

_PURCHASE_REASON = (
    "모든 안이 self_check에서 컷됨: 보수(매입단가 1,650원이 max_price 992원 초과); "
    "기본(매입단가 1,650원이 max_price 1,007원 초과)"
)


def _response(**kw) -> ProcurementRunResponse:
    base = {
        "request_id": "REQ-1",
        "as_of": "2025-12-31",
        "end_code": "E2_HELD",
        "reason": "유효한 안이 없어 제안을 내지 못했다.",
        "scenarios": [],
        "judgment": {"no_proposal_reason": _PURCHASE_REASON},
    }
    return ProcurementRunResponse(**{**base, **kw})


def test_매입_사유가_화면에_나온다():
    """🔴 이 파일의 주장이다."""
    text = render_answer(facts_from_procurement(_response()))

    assert "self_check에서 컷됨" in text
    assert "max_price" in text, "무엇 때문에 컷됐는지가 화면에 없다"


def test_마스터_사유도_같이_나온다():
    """★ **둘은 층이 다르다.** 하나만 두면 읽는 사람이 다음에 무엇을 볼지 모른다.

    ```text
    마스터   왜 보류하나
    매입     왜 안을 안 냈나
    ```
    """
    text = render_answer(facts_from_procurement(_response()))

    assert "유효한 안이 없어 제안을 내지 못했다" in text
    assert "self_check에서 컷됨" in text


def test_안이_있으면_매입_사유를_안_싣는다():
    """⚠️ 안이 있는 날은 `no_proposal_reason` 이 계약상 없다.

    그래도 누가 실어 보내면 화면이 *"안도 있고 못 냈다"* 를 같이 말하게 된다.
    """
    text = render_answer(
        facts_from_procurement(
            _response(
                scenarios=[{"label": "보수"}, {"label": "기본"}],
                reason="사용자 선택 대기",
            )
        )
    )

    assert "self_check에서 컷됨" not in text
    assert "2개" in text


def test_매입이_사유를_안_내면_그_줄이_없다():
    """빈 줄을 만들지 않는다 — 없는 것을 있는 것처럼 보이게 하지 않는다."""
    text = render_answer(facts_from_procurement(_response(judgment={})))

    assert "매입 사유" not in text
