"""걷기 요약이 **물류 점검 두 칸의 결과**를 말하는가 — 2026-09-14.

`run_logistics_inspection` 은 하루 두 번(`AFTER_INBOUND` · `AFTER_OUTBOUND`) 돌고
`RAN` · `NOTHING_DUE` · `FAILED` 를 낸다. 칸을 안 탄 날은 `NOT_ATTEMPTED` 다.

★★ 값은 `DayRunOutcome.inspection_*_status` 에 이미 있었는데 **요약에 세는 줄이
  없었다.** 그래서 걷기 끝에 *"점검이 실패한 날이 있었나"* 를 증명할 수 없었다.

🔴 **이 파일이 잡으려는 것.**

```text
① 두 칸이 각자 세어진다              입고 뒤와 출고 뒤를 한 통에 담지 않는다
② 🔴 FAILED 0 이 보인다              키가 빠지면 "없었다" 와 "안 셌다" 가 같아진다
③ 칸을 안 탄 날도 세어진다            NOT_ATTEMPTED 로 · 배치 없는 날은 점검을 탄다
④ 문제 수는 주인이 낸 값을 더하기만   DetectOut.counts · 결과가 없으면 빈 칸
⑤ 점검 FAILED 는 사고가 아니다         터져도 하루는 계속 간다
⑥ 기존 줄 순서를 안 바꾼다            유지어휘 뒤 · LLM어휘 앞에 한 줄 끼운다
```

★ **DB 를 안 탄다.** 하루 결과는 손으로 세우거나 `run_scheduled_day` 에 대역을 꽂아
  만들고, 걷기 결과는 `WalkResult` 를 직접 만든다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from app.logistics.monitoring.schemas import DetectOut
from app.master.backtest_runner import WalkResult, _incident_reason, format_summary
from app.master.inspection import (
    AFTER_INBOUND,
    AFTER_OUTBOUND,
    INSPECTION_STATUSES,
    InspectionOut,
)
from app.master.scheduler import DayRunOutcome, ScheduledAction, run_scheduled_day

SEOUL = ZoneInfo("Asia/Seoul")
오늘 = date(2026, 1, 7)
SIM = "SIM-INSPECT-SUMMARY-TEST"

#: 칸을 안 탄 날의 이름. 🔴 **주인은 `DayRunOutcome` 의 기본값이다** — 여기서 안 짓는다.
안탐 = DayRunOutcome.inspection_inbound_status

#: 요약 한 칸이 늘 들어야 할 넷.
넷 = frozenset({*INSPECTION_STATUSES, 안탐})


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


def _하루(
    입고뒤: str,
    출고뒤: str,
    *,
    입고뒤_값: InspectionOut | None = None,
    출고뒤_값: InspectionOut | None = None,
    **칸: Any,
) -> DayRunOutcome:
    return DayRunOutcome(
        as_of=오늘,
        action="RUN_NOW",
        reason="",
        inspection_inbound_status=입고뒤,
        inspection_outbound_status=출고뒤,
        inspection_inbound=입고뒤_값,
        inspection_outbound=출고뒤_값,
        **칸,
    )


def _걷기(*날들: DayRunOutcome) -> WalkResult:
    return WalkResult(start=오늘, end=오늘, days=tuple(날들))


def _점검줄(result: WalkResult) -> str:
    """요약에서 물류점검 줄 하나를 뗀다. 🔴 **없거나 둘이면 여기서 터진다.**"""
    요약 = _NFC(format_summary(result)).splitlines()
    줄들 = [줄 for 줄 in 요약 if 줄.startswith(_NFC("물류점검  "))]
    assert len(줄들) == 1, f"물류점검 줄이 {len(줄들)}개다 — 요약: {요약}"
    return 줄들[0]


def _결과(phase: str, *, opened: int = 0, updated: int = 0, resolved: int = 0) -> InspectionOut:
    탐지 = DetectOut(
        as_of=오늘,
        phase=phase,  # type: ignore[arg-type]
        status="RAN" if opened or updated or resolved else "NOTHING_DUE",
        opened=tuple(f"EX-O{i}" for i in range(opened)),
        updated=tuple(f"EX-U{i}" for i in range(updated)),
        resolved=tuple(f"EX-R{i}" for i in range(resolved)),
    )
    return InspectionOut(as_of=오늘, phase=phase, status=탐지.status, result=탐지)


# ---------------------------------------------------------------------------
# ① 두 칸이 각자 세어진다
# ---------------------------------------------------------------------------


def test_두_칸을_각자_센다() -> None:
    """🔴 **입고 뒤와 출고 뒤를 한 통에 담지 않는다.** 닫는 자리는 출고 뒤 하나다."""
    결과 = _걷기(
        _하루("RAN", "FAILED"),
        _하루("NOTHING_DUE", "FAILED"),
        _하루("NOTHING_DUE", "RAN"),
    )

    센것 = 결과.inspection_statuses

    assert set(센것) == {AFTER_INBOUND, AFTER_OUTBOUND}
    assert dict(센것[AFTER_INBOUND]) == {"RAN": 1, "NOTHING_DUE": 2, "FAILED": 0, 안탐: 0}
    assert dict(센것[AFTER_OUTBOUND]) == {"RAN": 1, "NOTHING_DUE": 0, "FAILED": 2, 안탐: 0}


def test_요약_줄이_두_칸의_분포를_그대로_찍는다() -> None:
    """값이 있는데 요약이 안 읽으면 없는 것과 같다 — `outbound_status` 때(`#446`) 배운 것."""
    줄 = _점검줄(_걷기(_하루("RAN", "FAILED"), _하루("NOTHING_DUE", "FAILED")))

    assert "'AFTER_INBOUND': {'FAILED': 0, 'NOTHING_DUE': 1, 'NOT_ATTEMPTED': 0, 'RAN': 1}" in 줄
    assert "'AFTER_OUTBOUND': {'FAILED': 2, 'NOTHING_DUE': 0, 'NOT_ATTEMPTED': 0, 'RAN': 0}" in 줄


# ---------------------------------------------------------------------------
# ② 🔴 0 도 찍는다
# ---------------------------------------------------------------------------


def test_실패가_없어도_FAILED_0_이_보인다() -> None:
    """🔴 **이 줄이 증명하려는 것이 *"실패가 없었다"* 다.** 키가 빠지면 그 증명이 사라진다."""
    줄 = _점검줄(_걷기(_하루("NOTHING_DUE", "NOTHING_DUE")))

    for 칸 in (AFTER_INBOUND, AFTER_OUTBOUND):
        assert f"'{칸}': {{'FAILED': 0, 'NOTHING_DUE': 1, 'NOT_ATTEMPTED': 0, 'RAN': 0}}" in 줄


def test_하루도_안_돈_걷기에서도_네_칸이_다_보인다() -> None:
    """🔴 **0건을 세는 잠금이다.** 빈 걷기에서 `{}` 가 찍히면 이 검사가 빨갛다."""
    센것 = _걷기().inspection_statuses

    for 칸 in (AFTER_INBOUND, AFTER_OUTBOUND):
        assert dict(센것[칸]) == dict.fromkeys(넷, 0)

    줄 = _점검줄(_걷기())
    for 칸 in (AFTER_INBOUND, AFTER_OUTBOUND):
        assert f"'{칸}': {{'FAILED': 0, 'NOTHING_DUE': 0, 'NOT_ATTEMPTED': 0, 'RAN': 0}}" in 줄


def test_어휘_이름을_마스터가_새로_안_짓는다() -> None:
    """★ **주인은 `inspection.INSPECTION_STATUSES` 와 `DayRunOutcome` 기본값이다.**"""
    assert INSPECTION_STATUSES == {"RAN", "NOTHING_DUE", "FAILED"}
    assert 안탐 == "NOT_ATTEMPTED"
    for 분포 in _걷기().inspection_statuses.values():
        assert set(분포) == 넷


# ---------------------------------------------------------------------------
# ③ 칸을 안 탄 날 · 배치 없는 날
# ---------------------------------------------------------------------------


@dataclass
class _Out:
    status: str
    reason: str = ""


class _Spy:
    def __init__(self, out: Any) -> None:
        self.out = out

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.out


class _Retry:
    status = "NOTHING_DUE"
    reason = "미적용 없음"
    outcomes: ClassVar[dict[str, int]] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> _Retry:
        return self


class _Inspect:
    def __init__(self, status: str) -> None:
        self.status = status

    def __call__(self, as_of: date, *, sim_run_id: str, phase: str) -> InspectionOut:
        return InspectionOut(as_of=as_of, phase=phase, status=self.status)  # type: ignore[arg-type]


def _행동(action: str) -> ScheduledAction:
    return ScheduledAction(
        as_of=오늘,
        now=datetime(2026, 1, 7, 9, 30, tzinfo=SEOUL),
        action=action,  # type: ignore[arg-type]
        reason="",
        deadline=datetime(2026, 1, 7, 10, 30, tzinfo=SEOUL),
    )


def test_칸을_안_탄_날은_NOT_ATTEMPTED_로_세어진다() -> None:
    """🔴 **하루가 안 열린 날도 요약에서 사라지지 않는다.** 빼면 *"몇 날을 봤나"* 가 틀린다."""
    안열림 = run_scheduled_day(
        _행동("RUN_NOW"),
        open_day_fn=_Spy(_Out("FAILED", "개장 실패")),
        inspect_fn=_Inspect("FAILED"),
        items=("배추",),
        sim_run_id=SIM,
    )
    assert 안열림.day_open_status == "FAILED", "전제가 깨졌다"

    센것 = _걷기(안열림).inspection_statuses

    assert 센것[AFTER_INBOUND][안탐] == 1
    assert 센것[AFTER_OUTBOUND][안탐] == 1
    assert "'NOT_ATTEMPTED': 1" in _점검줄(_걷기(안열림))


def test_배치_없는_날도_점검은_타고_그대로_세어진다() -> None:
    """⚠️ **`NO_ML_BATCH` 는 판단 넷만 건너뛴다.** 점검 두 칸은 판단부 밖이라 돈다.

    그 날을 `NOT_ATTEMPTED` 로 세면 **창고를 본 날이 안 본 날로** 세어진다.
    """
    배치없음 = run_scheduled_day(
        _행동("NO_ML_BATCH"),
        open_day_fn=_Spy(_Out("OPENED")),
        retry_fn=_Retry(),
        receive_fn=_Spy(_Out("RECEIVED")),
        inspect_fn=_Inspect("FAILED"),
        issue_fn=_Spy(_Out("ISSUED")),
        collect_fn=_Spy(_Out("COLLECTED")),
        outbound_fn=_Spy(_Out("NOTHING_DUE")),
        close_fn=_Spy(_Out("CLOSED")),
        items=("배추",),
        sim_run_id=SIM,
    )
    assert 배치없음.procurement_status == "NO_ML_BATCH", "전제가 깨졌다"

    센것 = _걷기(배치없음).inspection_statuses

    assert 센것[AFTER_INBOUND]["FAILED"] == 1
    assert 센것[AFTER_OUTBOUND]["FAILED"] == 1
    assert 센것[AFTER_INBOUND][안탐] == 0


# ---------------------------------------------------------------------------
# ④ 문제 수
# ---------------------------------------------------------------------------


def test_문제_수는_두_칸의_counts_를_더하기만_한다() -> None:
    """★ **주인은 `DetectOut.counts` 다.** 여기서 다시 세지 않는다."""
    결과 = _걷기(
        _하루(
            "RAN",
            "RAN",
            입고뒤_값=_결과(AFTER_INBOUND, opened=2, updated=1),
            출고뒤_값=_결과(AFTER_OUTBOUND, updated=1, resolved=3),
        ),
        _하루("NOTHING_DUE", "FAILED", 입고뒤_값=_결과(AFTER_INBOUND)),
    )

    assert dict(결과.inspection_counts) == {"opened": 2, "updated": 2, "resolved": 3}
    assert "· 문제 {'opened': 2, 'resolved': 3, 'updated': 2}" in _점검줄(결과)


def test_결과가_한_번도_없으면_문제_칸은_비어_있다() -> None:
    """⚠️ **전부 못 본 걷기에서 `opened 0` 을 채우면 *"봤는데 없었다"* 로 읽힌다.**"""
    결과 = _걷기(_하루("FAILED", "FAILED"), _하루(안탐, 안탐))

    assert dict(결과.inspection_counts) == {}
    assert _점검줄(결과).endswith("· 문제 {}")


# ---------------------------------------------------------------------------
# ⑤ 사고가 아니다
# ---------------------------------------------------------------------------


def test_점검_FAILED_는_사고가_아니다() -> None:
    """🔴 **터져도 하루는 계속 간다** (`inspection.py`). 요약에 보이는 사실이지 사고가 아니다."""
    하루 = _하루("FAILED", "FAILED", procurement_status="RAN", outbound_status="NOTHING_DUE")

    assert _incident_reason(하루, scope="FULL") is None


# ---------------------------------------------------------------------------
# ⑥ 줄 자리
# ---------------------------------------------------------------------------


def test_유지어휘_뒤_LLM어휘_앞에_한_줄로_선다() -> None:
    """🔴 **기존 줄 순서를 안 바꾼다.** 한 줄을 끼우기만 한다."""
    머리들 = [_NFC(줄).split("{", 1)[0].strip() for 줄 in format_summary(_걷기()).splitlines()]
    유지 = 머리들.index(_NFC("유지어휘"))

    assert 머리들[유지 + 1] == _NFC("물류점검")
    assert 머리들[유지 + 2] == _NFC("LLM어휘")
