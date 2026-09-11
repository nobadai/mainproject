"""판매 **확정 결과가 성적표에 보이는가** — 2026-09-11.

승인 문은 `RECORDED` 를 냈고 재검증은 `PASSED` 7건이었다. 그런데 `sales` 는 **0행**
이었다. 확정이 매번 막혔는데 **그 사실이 어디에도 안 남았다.**

```text
승인어휘   {RECORDED: 7}     🟢 적혔다
재검증     PASSED 7건        🟢 통과했다
확정       ??                🔴 아무 줄도 없다
sales      0행               🔴
```

★★ **그래서 사람이 손으로 재현해서야 원인을 찾았다.** 원인은
  `ValidationError: reported_sales_amount_krw` 였고, 코드(`BLOCKED`)만으로는 그것을
  못 읽는다 — **사유 문장을 봐야** 보인다.

🔴 **이 파일이 잡으려는 넷.**

```text
① BackfilledRun 이 확정 결과를 나른다        안 나르면 위 계층이 셀 것이 없다
② 확정 어휘 셋이 안 접힌다                    BLOCKED 와 FAILED 가 같아 보이면 안 된다
③ 걷기 요약에 「확정어휘」 한 줄이 선다        값이 있는데 성적표가 안 읽으면 없는 것과 같다
④ 막힌 사유 문장이 요약에 실린다              코드만 있으면 오늘 밤이 반복된다
```

★ **`승인어휘` 와 한 칸에 담지 않는다.** 어휘가 다르고 축이 다르다 — `RECORDED` 는
  *"승인이 적혔다"* 일 뿐 *"판매가 섰다"* 가 아니다 (`전이` 줄을 가른 것과 같은 이유).

★ **DB 를 안 탄다.** `BackfilledRun` 을 손으로 세워 `BackfillOut` 에 담고,
  걷기 결과는 `WalkResult` 를 직접 만든다.
"""

from __future__ import annotations

import unicodedata
from datetime import date

import pytest

from app.master.backfill import BackfilledRun, BackfillOut
from app.master.backtest_runner import WalkResult, format_summary
from app.master.scheduler import DayRunOutcome

오늘 = date(2026, 1, 6)

#: 확정 어휘 셋. 🔴 **주인은 `sales_approval.SaleConfirmationOut` 이다** — 여기서
#:   새 이름을 안 붙인다.
셋 = ("CONFIRMED", "BLOCKED", "FAILED")

#: 그날 실제로 막은 문장. 🔴 **코드만으로는 이것을 못 읽는다.**
실측사유 = "판매 확정 입력을 만들 수 없다: 1 validation error for SalesScenario"


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


def _행(
    상태: str | None,
    *,
    사유: str | None = None,
    request_id: str = "REQ-1",
) -> BackfilledRun:
    return BackfilledRun(
        as_of=오늘,
        run_id="RUN-1",
        request_id=request_id,
        outcome="RECORDED",
        revalidation_outcome="PASSED",
        confirmation_status=상태,  # type: ignore[arg-type]
        confirmation_reason=사유,
    )


def _백필(*행들: BackfilledRun) -> BackfillOut:
    return BackfillOut(
        sim_run_id="SIM-SALESCHAIN-20260911",
        start=오늘,
        end=오늘,
        status="RAN",
        runs=행들,
    )


def _하루(승인: BackfillOut | None) -> DayRunOutcome:
    return DayRunOutcome(as_of=오늘, action="RUN_NOW", reason="", sales_approval=승인)


# ---------------------------------------------------------------------------
# ① 행이 확정 결과를 나른다
# ---------------------------------------------------------------------------


def test_행이_확정_결과와_사유를_같이_나른다() -> None:
    """🔴 **`RECORDED` 옆에 이 둘이 없으면 위 계층이 셀 것이 없다.**"""
    행 = _행("BLOCKED", 사유=실측사유)

    assert 행.outcome == "RECORDED", "승인은 적혔다 — 그 사실이 바뀌면 안 된다"
    assert 행.confirmation_status == "BLOCKED"
    assert _NFC(행.confirmation_reason or "") == _NFC(실측사유)


def test_확정을_안_탄_행은_None_이다() -> None:
    """★ 매입 행에는 확정이라는 사건 자체가 없다.

    그 `None` 은 *"확정에 실패했다"* 가 아니라 **"확정할 것이 없었다"** 다.
    """
    행 = _행(None)

    assert 행.confirmation_status is None
    assert dict(_백필(행).confirmation_outcomes) == {}, (
        "확정을 안 탄 행을 셌다 — 매입 행 수만큼 「못 했다」가 부풀어 오른다"
    )


# ---------------------------------------------------------------------------
# ② 어휘 셋이 안 접힌다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("어휘", 셋)
def test_어휘_셋이_각자_세어진다(어휘: str) -> None:
    """⚠️ 접으면 *"확정할 수 없었다"* 와 *"쓰려다 터졌다"* 가 같아 보인다.

    앞의 것은 아무것도 안 썼고 뒤의 것은 쓰려다 롤백했다 — 다음에 볼 자리가 다르다.
    """
    assert dict(_백필(_행(어휘)).confirmation_outcomes) == {어휘: 1}


def test_승인_어휘와_한_칸에_안_담긴다() -> None:
    """🔴 *"승인을 적었다"* 와 *"판매가 섰다"* 는 축이 하나 다르다."""
    나온것 = _백필(_행("BLOCKED"), _행("CONFIRMED"))

    assert dict(나온것.confirmation_outcomes) == {"BLOCKED": 1, "CONFIRMED": 1}
    assert dict(나온것.outcomes) == {"RECORDED": 2}, "승인 줄에 확정 결과가 섞였다"


# ---------------------------------------------------------------------------
# ③ 걷기 요약에 한 줄이 선다
# ---------------------------------------------------------------------------


def test_확정_어휘_셋이_요약에_접히지_않고_올라온다() -> None:
    """🔴 **이 줄이 없어서 `RECORDED 7 · PASSED 7` 을 보고 판매가 선 줄 알았다.**

    ★★ 값이 있는데 성적표가 안 읽으면 **없는 것과 같다** (`outbound_status` 를 태울
      때 배운 그것이다).
    """
    결과 = WalkResult(
        start=오늘,
        end=오늘,
        days=(_하루(_백필(*(_행(어휘) for 어휘 in 셋))),),
    )

    센것 = dict(결과.confirmation_outcomes)
    요약 = _NFC(format_summary(결과))

    assert 센것 == {어휘: 1 for 어휘 in 셋}
    for 어휘 in 셋:
        assert 어휘 in 요약, f"요약에 확정 어휘 '{어휘}' 가 없다"
    assert _NFC("확정어휘  ") in 요약, "요약에 확정 줄 자체가 없다"


def test_확정을_안_탄_날은_요약이_세지_않는다() -> None:
    """★ 안 탄 날은 `{}` 다 — 0 으로 채우면 *"확정된 것이 없다"* 와 같아 보인다."""
    결과 = WalkResult(start=오늘, end=오늘, days=(_하루(None),))

    assert dict(결과.confirmation_outcomes) == {}
    assert _NFC("확정어휘  {}") in _NFC(format_summary(결과))


# ---------------------------------------------------------------------------
# ④ 🔴 사유 문장이 같이 올라온다 — 코드만으로는 오늘 밤이 반복된다
# ---------------------------------------------------------------------------


def test_막힌_사유가_요약에_실린다() -> None:
    """🔴 **`{BLOCKED: 7}` 만 보고는 무엇이 막았는지를 못 읽는다.**

    그날의 이유는 `ValidationError: reported_sales_amount_krw` 였고, 그것은 문장을
    봐야 보인다. 코드만 나르면 사람이 또 손으로 재현해야 한다.
    """
    결과 = WalkResult(
        start=오늘,
        end=오늘,
        days=(_하루(_백필(_행("BLOCKED", 사유=실측사유))),),
    )

    assert 결과.confirmation_reasons == (실측사유,)
    assert _NFC(실측사유) in _NFC(format_summary(결과)), "막힌 사유가 요약에 없다"


def test_통과한_확정의_사유는_안_싣는다() -> None:
    """★ `CONFIRMED` 의 문장은 *"확정했다"* 라 새로 알려 주는 것이 없다.

    ⚠️ 다 실으면 막힌 줄이 통과한 줄에 묻힌다 — 봐야 할 것만 남긴다.
    """
    결과 = WalkResult(
        start=오늘,
        end=오늘,
        days=(_하루(_백필(_행("CONFIRMED", 사유="판매를 확정했다 (CONFIRMED)."))),),
    )

    assert 결과.confirmation_reasons == ()


def test_같은_사유가_되풀이되면_한_번만_싣는다() -> None:
    """★ 이레 내내 같은 이유면 줄이 일곱이 아니라 **하나**여야 읽힌다.

    ⚠️ 그렇다고 사유 자체를 버리지 않는다 — 되풀이를 접는 것과 안 싣는 것은 다르다.
    """
    다른사유 = "재검증이 통과하지 않아 판매를 확정하지 않았다 (재검증 결과: FAILED)."
    하루치 = _백필(
        _행("BLOCKED", 사유=실측사유, request_id="REQ-1"),
        _행("BLOCKED", 사유=실측사유, request_id="REQ-2"),
        _행("BLOCKED", 사유=다른사유, request_id="REQ-3"),
    )
    결과 = WalkResult(start=오늘, end=오늘, days=(_하루(하루치),))

    # ★ 본 순서를 지킨다 — 무엇이 먼저 막았는지가 순서다.
    assert 결과.confirmation_reasons == (실측사유, 다른사유)
