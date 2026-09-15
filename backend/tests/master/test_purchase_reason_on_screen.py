"""부서가 낸 `no_proposal_reason` 이 **문구 그대로 화면에 오른다** (2026-09-03).

🔴 **전에는 마스터 사유만 화면에 있었습니다.**

```text
마스터   유효한 안이 없어 제안을 내지 못했다      ← 무엇이 없어서인지는 안 말한다
매입     (응답에는 있는데 화면에는 없었다)
```

`report.py` 는 매입 사유를 쓰는데 `answer.py`(화면)는 안 썼습니다 — 마크다운
리포트를 여는 사람만 볼 수 있었습니다.

★ **이 파일이 재는 것은 통과(passthrough) 하나입니다.**

```text
재는 것      judgment.no_proposal_reason 이 무슨 문구든 화면에 그대로 오른다
안 재는 것   그 문구가 무엇인지 · 어느 단어가 들어 있는지
```

🔴 **매입 어휘를 여기서 잠그지 않습니다.** 전에는 픽스처가 매입의 실제 문장
(`max_price` 가 든 컷 사유)이었고 그 단어가 화면에 있는지를 단언했습니다. 그래서

```text
① 매입이 그 단어를 그만 쓰자 (매입 #632 · 이제 「상한」)  저장소에서 그 문구를
   찍는 곳이 이 검사 하나만 남았다 — 남의 어휘를 마스터 검사가 잠갔다
② 픽스처를 자기가 만들어 자기에게 먹이므로 계약 검사가 아닌데 계약 검사처럼 읽혔다
```

그래서 픽스처를 **누가 봐도 자리표시자**로 바꾸고, **서로 다른 두 문구로 각각
돌립니다** — 통과가 내용에 안 기대는 것이 그것으로 보입니다.
"""

from __future__ import annotations

import unicodedata

import pytest

from app.master.answer import facts_from_procurement, render_answer
from app.master.schemas import ProcurementRunResponse

#: 자리표시자 둘. **매입의 실제 문장처럼 생기지 않게 둔다** — 여기 적힌 글자는
#: 아무 뜻이 없고, 아무 뜻이 없어도 화면에 올라가야 한다는 것이 이 파일의 주장이다.
_자리표시자_가 = "PLACEHOLDER-REASON-A · 부서가 낸 문구 하나 (뜻 없음)"
_자리표시자_나 = "ZZZ-다른-자리표시자-나 // 내용이 달라도 결과는 같아야 한다"

_자리표시자들 = (_자리표시자_가, _자리표시자_나)

_사유줄이름 = "매입 사유"


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


def _response(**kw) -> ProcurementRunResponse:
    base = {
        "request_id": "REQ-1",
        "as_of": "2025-12-31",
        "end_code": "E2_HELD",
        "reason": "유효한 안이 없어 제안을 내지 못했다.",
        "scenarios": [],
        "judgment": {"no_proposal_reason": _자리표시자_가},
    }
    return ProcurementRunResponse(**{**base, **kw})


@pytest.mark.parametrize("사유", _자리표시자들)
def test_부서가_낸_사유가_문구_그대로_화면에_오른다(사유: str):
    """🔴 이 파일의 주장이다. **두 문구로 각각 돈다** — 통과가 내용에 안 기댄다."""
    응답 = _response(judgment={"no_proposal_reason": 사유})
    text = _NFC(render_answer(facts_from_procurement(응답)))

    assert _NFC(사유) in text, f"부서가 낸 사유가 화면에 그대로 안 올라갔다: {text}"


def test_두_자리표시자가_서로_다르다():
    """★ **자기 생존 검사다.** 둘이 같아지면 위 검사는 한 번을 두 번 돈 것뿐이다."""
    assert _자리표시자_가 != _자리표시자_나


def test_마스터_사유도_같이_나온다():
    """★ **둘은 층이 다르다.** 하나만 두면 읽는 사람이 다음에 무엇을 볼지 모른다.

    ```text
    마스터   왜 보류하나
    매입     왜 안을 안 냈나
    ```
    """
    text = _NFC(render_answer(facts_from_procurement(_response())))

    assert _NFC("유효한 안이 없어 제안을 내지 못했다") in text
    assert _NFC(_자리표시자_가) in text


def test_안이_있으면_부서_사유를_안_싣는다():
    """⚠️ 안이 있는 날은 `no_proposal_reason` 이 계약상 없다.

    그래도 누가 실어 보내면 화면이 *"안도 있고 못 냈다"* 를 같이 말하게 된다.
    """
    text = _NFC(
        render_answer(
            facts_from_procurement(
                _response(
                    scenarios=[{"label": "보수"}, {"label": "기본"}],
                    reason="사용자 선택 대기",
                )
            )
        )
    )

    assert _NFC(_자리표시자_가) not in text
    assert "2개" in text


def test_부서가_사유를_안_내면_그_줄이_없다():
    """빈 줄을 만들지 않는다 — 없는 것을 있는 것처럼 보이게 하지 않는다."""
    text = _NFC(render_answer(facts_from_procurement(_response(judgment={}))))

    assert _NFC(_사유줄이름) not in text
