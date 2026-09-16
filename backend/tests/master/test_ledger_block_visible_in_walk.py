"""걷기 요약이 **승인은 났는데 원장에 한 행도 안 남은 것**을 말하는가 — 2026-09-16.

★★ **확인 걷기 CHECK-0916 에서 6건 · 726kg · 255,287원(수량 0.76% · 금액 0.46%)이
  그렇게 사라졌고, 요약만 봐서는 안 보여 사람이 못 봤다.** 매입 파트가 A/B 실험을
  하다 발견했다. 승인은 `RECORDED` 로 찍히고 전이는 `NOT_APPLIED` 한 값에 묻힌다.

🔴 **이 파일이 잡으려는 것.**

```text
① 막힌 승인이 있으면 원장못씀 줄에 건수와 갈래가 찍힌다
② 🔴 「영영 안 될 것」이 등급 둘만 센다      회차금액 없음은 그 수에 안 든다
③ 막힌 것이 없으면 0 건으로 찍힌다           줄이 사라지지 않는다
④ 🔴 당일 전이와 다음 날 재시도 양쪽 다 센다  한쪽만 세면 수가 조용히 작아진다
⑤ 회귀: procurement_statuses · transition_outcomes 가 안 변한다
⑨ 🔴 같은 승인이 사흘 막히면 고유 1건 · 재시도 2회  날짜별로 세면 부푼다
⑩ 🔴 「영영 안 될 것」도 고유로 센다          닷새 막혀도 1건
⑪ 갈래 이름과 사유 문장의 주인이 `ledger` 하나다
```

🔴 **세고 찍기만 한다.** 이 파일은 걷기가 고르는 안도 재시도 동작도 안 본다 —
   바뀌면 안 되는 것이라 애초에 건드리지 않는다.

★ **DB 를 안 탄다.** 하루 결과를 손으로 세우고 `WalkResult` 를 직접 만든다
  (`test_inspection_visible_in_walk.py` 와 같은 모양).
"""

from __future__ import annotations

import unicodedata
from datetime import date

from app.master.backfill import BackfilledRun, BackfillOut
from app.master.backtest_runner import WalkResult, format_summary
from app.master.ledger import (
    BLOCK_GRADES,
    BLOCK_LEG_AMOUNT,
    BLOCK_NO_ARRIVAL,
    BLOCK_PAYMENT_DUE,
    LEDGER_BLOCK_KINDS,
    PERMANENT_BLOCK_KINDS,
)
from app.master.pending_transition import RetriedTransition, RetryOut
from app.master.scheduler import DayRunOutcome
from app.master.transition import purchase_id_prefix_for

오늘 = date(2026, 1, 7)
SIM = "SIM-LEDGER-BLOCK-SUMMARY-TEST"


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------------------
# 하루 세우기
# ---------------------------------------------------------------------------


def _당일막힘(
    *막힌것: tuple[str, int, str],
    as_of: date = 오늘,
) -> BackfillOut:
    """당일 전이에서 막힌 승인들. `(업무키, 결정회차, 갈래)` 로 준다."""
    return BackfillOut(
        sim_run_id=SIM,
        start=as_of,
        end=as_of,
        status="RAN",
        runs=tuple(
            BackfilledRun(
                as_of=as_of,
                run_id=f"RUN-{request_id}-{seq}",
                request_id=request_id,
                decision_seq=seq,
                outcome="RECORDED",
                transition_block_kind=갈래,
            )
            for request_id, seq, 갈래 in 막힌것
        ),
    )


def _재시도막힘(*막힌것: tuple[str, int, str], as_of: date = 오늘) -> RetryOut:
    """다음 날 재시도에서 막힌 승인들. `(업무키, 결정회차, 갈래)` 로 준다."""
    return RetryOut(
        status="RAN",
        reason=f"미적용 {len(막힌것)}건을 다시 세웠다",
        retried=tuple(
            RetriedTransition(
                request_id=request_id,
                decision_seq=seq,
                as_of=as_of,
                outcome="NOT_APPLIED",
                reason="사유 문장은 여기서 안 짓는다",
                block_kind=갈래,
            )
            for request_id, seq, 갈래 in 막힌것
        ),
    )


def _하루(
    *,
    당일: BackfillOut | None = None,
    재시도: RetryOut | None = None,
    as_of: date = 오늘,
) -> DayRunOutcome:
    return DayRunOutcome(
        as_of=as_of,
        action="RUN_NOW",
        reason="",
        procurement_approval_status="RAN" if 당일 is not None else "NOT_ATTEMPTED",
        procurement_approval=당일,
        pending_transition_status="RAN" if 재시도 is not None else "NOT_ATTEMPTED",
        pending_transition=재시도,
    )


def _걷기(*날들: DayRunOutcome) -> WalkResult:
    return WalkResult(start=오늘, end=오늘, days=tuple(날들))


def _원장못씀줄(result: WalkResult) -> str:
    """요약에서 원장못씀 줄 하나를 뗀다. 🔴 **없거나 둘이면 여기서 터진다.**"""
    요약 = _NFC(format_summary(result)).splitlines()
    줄들 = [줄 for 줄 in 요약 if 줄.startswith(_NFC("원장못씀"))]
    assert len(줄들) == 1, f"원장못씀 줄이 {len(줄들)}개다 — 요약: {요약}"
    return 줄들[0]


# ---------------------------------------------------------------------------
# ① 막힌 승인이 있으면 건수와 갈래가 찍힌다
# ---------------------------------------------------------------------------


def test_등급_둘로_막히면_건수와_갈래가_찍힌다() -> None:
    """🔴 **승인은 RECORDED 인데 원장은 0행이다.** 그 사실이 요약에 있어야 한다."""
    결과 = _걷기(_하루(당일=_당일막힘(("REQ-A", 1, BLOCK_GRADES), ("REQ-B", 1, BLOCK_GRADES))))

    센것 = 결과.ledger_blocks

    assert 센것.unique == 2
    assert 센것.unique_by_kind[BLOCK_GRADES] == 2
    줄 = _원장못씀줄(결과)
    assert _NFC("고유 2건") in 줄, 줄
    assert _NFC(f"{BLOCK_GRADES}: 2") in 줄, 줄


def test_갈래를_사유_문장이_아니라_이름으로_접는다() -> None:
    """🔴 **문장에는 등급 이름이 박혀 있다** — 키로 쓰면 약정마다 다른 키가 된다."""
    결과 = _걷기(
        _하루(
            당일=_당일막힘(
                ("REQ-A", 1, BLOCK_GRADES),
                ("REQ-B", 1, BLOCK_LEG_AMOUNT),
                ("REQ-C", 1, BLOCK_PAYMENT_DUE),
                ("REQ-D", 1, BLOCK_NO_ARRIVAL),
            )
        )
    )

    센것 = 결과.ledger_blocks

    assert dict(센것.unique_by_kind) == {
        BLOCK_GRADES: 1,
        BLOCK_LEG_AMOUNT: 1,
        BLOCK_PAYMENT_DUE: 1,
        BLOCK_NO_ARRIVAL: 1,
    }
    assert 센것.unique == 4


# ---------------------------------------------------------------------------
# ② 🔴 「영영 안 될 것」이 등급 둘만 센다
# ---------------------------------------------------------------------------


def test_영영_안_될_것은_등급_둘만_센다() -> None:
    """🔴 **이 줄이 이 판의 핵심이다.**

    ```text
    등급이 둘        영영 안 된다 — 며칠을 재시도해도 담을 칸이 없다
    그 밖 셋          매입·재무·물류가 값을 보내면 다음 날 풀린다
    ```

    섞으면 *"재시도가 도니 곧 풀리겠지"* 로 읽히는데, 등급 둘은 몇 달을 돌아도
    안 풀린다 — **다음에 할 일이 다르다.**
    """
    결과 = _걷기(
        _하루(
            당일=_당일막힘(
                ("REQ-A", 1, BLOCK_GRADES),
                ("REQ-B", 1, BLOCK_GRADES),
                ("REQ-C", 1, BLOCK_LEG_AMOUNT),
                ("REQ-D", 1, BLOCK_PAYMENT_DUE),
                ("REQ-E", 1, BLOCK_NO_ARRIVAL),
            )
        )
    )

    센것 = 결과.ledger_blocks

    assert 센것.unique == 5, "막힌 승인은 다섯이다"
    assert 센것.permanent == 2, (
        "영영 안 될 것은 등급 둘 2건뿐이다 — 회차금액·지급일·도착분은 값이 오면 풀린다"
    )
    assert _NFC("영영 안 될 것 2건") in _원장못씀줄(결과)


def test_회차금액_없음은_영영에_안_든다() -> None:
    """🔴 **값이 오면 다음 날 풀리는 것을 영영으로 세면 크기가 부푼다.**"""
    결과 = _걷기(_하루(당일=_당일막힘(("REQ-A", 1, BLOCK_LEG_AMOUNT))))

    센것 = 결과.ledger_blocks

    assert 센것.unique == 1
    assert 센것.permanent == 0


# ---------------------------------------------------------------------------
# ③ 막힌 것이 없으면 0 건으로 찍힌다
# ---------------------------------------------------------------------------


def test_막힌_것이_없어도_줄이_찍힌다() -> None:
    """🔴 **줄이 사라지면 「없었다」와 「안 셌다」가 같아진다** (`_cash_lines` 규율)."""
    줄 = _원장못씀줄(_걷기(_하루()))

    assert _NFC("고유 0건") in 줄, 줄
    assert _NFC("영영 안 될 것 0건") in 줄, 줄


def test_0_인_갈래도_줄에_남는다() -> None:
    """🔴 **키가 빠지면 「그 갈래가 없었다」를 아무도 못 읽는다.**"""
    줄 = _원장못씀줄(_걷기(_하루(당일=_당일막힘(("REQ-A", 1, BLOCK_GRADES)))))

    for 갈래 in LEDGER_BLOCK_KINDS:
        assert _NFC(f"{갈래}: ") in 줄, f"{갈래} 칸이 줄에 없다 — {줄}"


def test_갈래_순서는_주인이_정한_그대로다() -> None:
    """★ 가나다순으로 세우지 않는다 — 순서가 뜻이다 (`관측시점` 줄과 같은 규율)."""
    센것 = _걷기(_하루()).ledger_blocks

    assert tuple(센것.unique_by_kind) == LEDGER_BLOCK_KINDS


# ---------------------------------------------------------------------------
# ④ 🔴 당일 전이와 다음 날 재시도 양쪽 다 센다
# ---------------------------------------------------------------------------


def test_당일_전이만_막혀도_세어진다() -> None:
    """🔴 **당일 경로를 빼면 수가 조용히 작아진다** — 오류가 안 나고 그냥 적게 나온다."""
    센것 = _걷기(_하루(당일=_당일막힘(("REQ-A", 1, BLOCK_GRADES)))).ledger_blocks

    assert 센것.unique == 1
    assert 센것.unique_by_kind[BLOCK_GRADES] == 1


def test_다음_날_재시도만_막혀도_세어진다() -> None:
    """🔴 **재시도 경로를 빼도 수가 조용히 작아진다.**"""
    센것 = _걷기(_하루(재시도=_재시도막힘(("REQ-A", 1, BLOCK_GRADES)))).ledger_blocks

    assert 센것.unique == 1
    assert 센것.unique_by_kind[BLOCK_GRADES] == 1


def test_두_경로가_서로_다른_승인이면_둘_다_센다() -> None:
    """🔴 **한쪽만 세는 구현이면 여기가 red 다.**"""
    결과 = _걷기(
        _하루(
            당일=_당일막힘(("REQ-당일", 1, BLOCK_GRADES)),
            재시도=_재시도막힘(("REQ-재시도", 1, BLOCK_LEG_AMOUNT)),
        )
    )

    센것 = 결과.ledger_blocks

    assert 센것.unique == 2, "당일 1건 · 재시도 1건 — 한쪽만 세면 1 이 된다"
    assert 센것.unique_by_kind[BLOCK_GRADES] == 1
    assert 센것.unique_by_kind[BLOCK_LEG_AMOUNT] == 1


# ---------------------------------------------------------------------------
# ⑨⑩ 🔴 고유로 센다 — 같은 승인이 날마다 다시 막힌다
# ---------------------------------------------------------------------------


def test_같은_승인이_사흘_막히면_고유_1건_재시도_2회() -> None:
    """🔴 **날짜별로 세면 한 건이 며칠치로 부푼다.**

    ★★ 실측에서 `NOT_APPLIED 12` 였는데 복수 등급 승인안은 **실제로 1건**이었다.
      12 와 1 이 이만큼 벌어진다 (매입 파트 회신 2026-09-16).

    ★ 첫날은 당일 전이이고 그 뒤 이틀이 재시도다 — **첫 시도는 소음이 아니다.**
    """
    결과 = _걷기(
        _하루(당일=_당일막힘(("REQ-A", 1, BLOCK_GRADES))),
        _하루(재시도=_재시도막힘(("REQ-A", 1, BLOCK_GRADES)), as_of=date(2026, 1, 8)),
        _하루(재시도=_재시도막힘(("REQ-A", 1, BLOCK_GRADES)), as_of=date(2026, 1, 9)),
    )

    센것 = 결과.ledger_blocks

    assert 센것.unique == 1, "같은 승인이다 — 3 이 나오면 날짜별로 센 것이다"
    assert 센것.unique_by_kind[BLOCK_GRADES] == 1
    assert 센것.retries == 2, "재시도는 두 번 돌았다 — 소음은 소음대로 보인다"
    줄 = _원장못씀줄(결과)
    assert _NFC("고유 1건") in 줄, 줄
    assert _NFC("재시도 2회") in 줄, 줄


def test_영영_안_될_것도_고유로_센다() -> None:
    """🔴 **이 수가 곧 발표에서 말할 크기다.** 닷새 막혀도 1건이다."""
    닷새 = (오늘, date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 12), date(2026, 1, 13))
    결과 = _걷기(
        *(
            _하루(재시도=_재시도막힘(("REQ-A", 1, BLOCK_GRADES), as_of=날), as_of=날)
            for 날 in 닷새
        )
    )

    센것 = 결과.ledger_blocks

    assert 센것.permanent == 1, "등급 둘인 한 건이다 — 5 가 나오면 날짜별로 센 것이다"
    assert 센것.retries == 5
    assert _NFC("영영 안 될 것 1건") in _원장못씀줄(결과)


def test_같은_업무_키라도_결정_회차가_다르면_다른_승인이다() -> None:
    """🔴 **`request_id` 하나로 접으면 서로 다른 승인 둘이 한 건이 된다.**

    ★ 동일성 키의 주인은 `transition.purchase_id_prefix_for` 다 — 원장 행 ID 를
      짓는 규칙과 **같은 함수**라 둘이 갈릴 수가 없다.
    """
    결과 = _걷기(_하루(재시도=_재시도막힘(("REQ-A", 1, BLOCK_GRADES), ("REQ-A", 2, BLOCK_GRADES))))

    assert 결과.ledger_blocks.unique == 2
    assert purchase_id_prefix_for("REQ-A", 1) != purchase_id_prefix_for("REQ-A", 2)


def test_안_막힌_승인은_안_센다() -> None:
    """★ `RECORDED` 이면서 원장에 닿은 행은 이 줄과 상관이 없다."""
    닿은것 = BackfillOut(
        sim_run_id=SIM,
        start=오늘,
        end=오늘,
        status="RAN",
        runs=(
            BackfilledRun(
                as_of=오늘,
                run_id="RUN-OK",
                request_id="REQ-OK",
                decision_seq=1,
                outcome="RECORDED",
            ),
        ),
    )

    센것 = _걷기(_하루(당일=닿은것)).ledger_blocks

    assert 센것.unique == 0
    assert 센것.retries == 0


# ---------------------------------------------------------------------------
# ⑤ 회귀 — 기존 값이 안 변한다
# ---------------------------------------------------------------------------


def test_기존_두_줄의_값이_안_변한다() -> None:
    """🔴 **이 판은 세고 찍기만 한다.** 종전 값이 갈리면 걷기 숫자가 갈린 것이다."""
    결과 = _걷기(
        _하루(
            당일=_당일막힘(("REQ-A", 1, BLOCK_GRADES)),
            재시도=_재시도막힘(("REQ-A", 1, BLOCK_GRADES)),
        )
    )

    assert dict(결과.procurement_statuses) == {"NOT_ATTEMPTED": 1}, (
        "매입 판단 단계는 이 판이 안 건드린다"
    )
    assert dict(결과.transition_outcomes) == {"NOT_APPLIED": 1}, (
        "전이 어휘는 이 판이 안 건드린다 — 접히거나 늘면 안 된다"
    )
    assert dict(결과.approval_outcomes) == {"RECORDED": 1}, "승인 어휘도 그대로다"


def test_원장못씀_줄이_매입_줄_바로_뒤에_선다() -> None:
    """★ 자리도 사실이다 — 기존 줄 순서를 바꾸지 않고 한 줄만 끼운다."""
    요약 = _NFC(format_summary(_걷기(_하루()))).splitlines()
    매입 = next(i for i, 줄 in enumerate(요약) if 줄.startswith(_NFC("매입 ")))

    assert 요약[매입 + 1].startswith(_NFC("원장못씀")), 요약[매입 : 매입 + 3]
    assert 요약[매입 + 2].startswith(_NFC("종료코드")), "기존 줄이 밀리기만 해야 한다"


# ---------------------------------------------------------------------------
# ⑪ 갈래 이름의 주인이 `ledger` 하나다
#
# ⚠️ **갈래와 사유 문장이 짝인지는 `test_commitment_grade.py` 가 잠근다** — 약정을
#   실제로 조립해 보는 자리가 거기이고, `ledger_block_reason` 문장을 이미 그 파일이
#   본다. 같은 사실을 두 파일이 각자 재지 않는다.
# ---------------------------------------------------------------------------


def test_영영_안_될_갈래의_주인이_ledger_다() -> None:
    """★ 세는 쪽이 자기 목록을 들면 갈래가 둘이 된다."""
    assert PERMANENT_BLOCK_KINDS == (BLOCK_GRADES,)
    assert set(PERMANENT_BLOCK_KINDS) <= set(LEDGER_BLOCK_KINDS)
