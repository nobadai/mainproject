"""`E3_REJECTED` 사유는 **지적이 매입에 갔는지**를 말한다.

🔴 **실측 2026-09-01 — 화면이 안 한 일을 한 것처럼 말하고 있었다.**

```text
화면            "매입 재호출 2 회에도 통과안 없음"
사람이 읽는 것   "고쳐 보라고 두 번 시켰는데 못 고쳤구나"
실제            같은 입력으로 두 번 돌렸다
```

`_purchase_input` 이 루프 밖 값만 읽어(`flow.py`) 2회차 payload 가 1회차와 같다.
검증 지적도 부서 기각 사유도 매입에 가지 않는다.

★ **되먹임을 배선하지 않은 것은 선택이다. 안 한 것을 한 것처럼 읽히게 두는 것은
  선택이 아니다.** 배선(이슈 ②-ㄷ)이 끝나면 이 문장은 전달 건수와 반영 건수를 적는
  쪽으로 바뀐다.
"""

from __future__ import annotations

from app.master.verifier import VerificationResult
from tests.master.test_flow import advisor, happy


def test_검증_지적이_전달되지_않았다고_적는다():
    out = happy(verifier=lambda s, c, v, p, ctx=None: VerificationResult(("E-IDENTITY",))).run()

    assert out.end_code == "E3_REJECTED"
    assert "매입 재호출 2 회에도 통과안 없음" in out.reason
    assert "매입에 전달되지 않은 것" in out.reason
    assert "검증 지적 1건" in out.reason
    assert "되먹임 미배선" in out.reason


def test_부서_기각_사유가_전달되지_않았다고_적는다():
    """부서 이름을 적는다 — *"누가 기각했는지"* 가 화면에서 사라지지 않게."""
    out = happy(finance=advisor(validation_status="reject")).run()

    assert out.end_code == "E3_REJECTED"
    assert "재무 기각 사유" in out.reason
    assert "되먹임 미배선" in out.reason


def test_둘_다면_둘_다_적는다():
    out = happy(
        finance=advisor(validation_status="reject"),
        verifier=lambda s, c, v, p, ctx=None: VerificationResult(("E-IDENTITY", "E-TIMING")),
    ).run()

    assert "검증 지적 2건" in out.reason
    assert "재무 기각 사유" in out.reason


def test_건수를_센다_문장을_옮기지_않는다():
    """★ **지적 원문은 안 싣는다.**

    원문은 `findings` 가 이미 응답에 싣고 있다. 사유 문장이 그것을 한 번 더 요약하면
    **같은 사실의 주인이 둘**이 되고, 한쪽만 바뀌는 자리가 생긴다.
    이 문장이 소유하는 것은 *"그 지적이 매입에 갔는가"* 하나다.
    """
    out = happy(
        verifier=lambda s, c, v, p, ctx=None: VerificationResult(("E-IDENTITY", "E-TIMING"))
    ).run()

    assert "검증 지적 2건" in out.reason
    assert "E-IDENTITY" not in out.reason
    assert out.findings == ("E-IDENTITY", "E-TIMING")


def test_다른_E3_사유는_안_건드린다():
    """예산 소진은 재호출을 다 쓴 것이 아니다 — 되먹임 이야기를 붙이지 않는다."""
    out = happy(budget=3).run()

    assert out.end_code == "E3_REJECTED"
    assert "예산" in out.reason
    assert "되먹임" not in out.reason


def test_통과하는_날은_이_문장이_안_나온다():
    out = happy(finance=advisor(validation_status="conditional")).run()

    assert out.end_code == "E1_APPROVED"
    assert "되먹임" not in out.reason
