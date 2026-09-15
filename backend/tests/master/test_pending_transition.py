"""**도착일이 열릴 때 미적용 전이가 다시 선다** (`#563` · 2026-09-11).

```text
개장 → **미적용 전이 재시도** → 입고 → 채권 → 수금 → [장부 관문] → …
```

★★ **왜 이 판이 있나 — 승인 15건이 원장에 한 건도 안 닿았다.**

```text
실측 SIM-WALK-2026-APPROVED · 2026-01-01 ~ 01-09
  종료코드  E1_APPROVED 15 · 승인어휘 RECORDED 15
  🔴 원장    purchases 0행 · purchase_items 0행 · inventory_lots 0행
  전이      FAILED — "갱신할 물류 runtime fixture 행이 없다 (as_of=2026-01-10)"
```

승인일은 01-09 이고 상태가 설 날은 01-10 인데, 걷기는 날짜 순으로 돌아 **01-10 행은
다음 차례에** 열린다. 전이는 **언제나 하루 앞을 본다.**

🔴 **물류가 그 행을 미리 안 만드는 것은 옳다.** `evidence_grade` · `approved_by` 는
   물류의 주장이고 전이가 지어내면 없는 근거가 물류 표에 앉는다. 그래서 고칠 자리는
   물류도 승인도 아니라 **「언제 다시 부르나」** 다.

⚠️ **DB 를 안 탄다.** 조회도 전이도 전부 대역이다 — 이 판이 잠그는 것은 **찾는 식과
  부르는 자리와 그 순서**이지, 전이가 무엇을 적는가가 아니다.

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가
  안 같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import unicodedata
from datetime import date, timedelta
from typing import Any

import pytest

from app.master.backtest_runner import WalkResult, format_summary
from app.master.commitment import ApprovedCommitment, ArrivalLeg
from app.master.pending_transition import (
    RetriedTransition,
    RetryOut,
    pending_approvals,
    retry_pending_transitions,
)
from app.master.scheduler import DayRunOutcome
from app.master.transition import TransitionOut, purchase_id_for, purchase_id_prefix_for

오늘 = date(2026, 1, 10)
실행 = "SIM-WALK-2026-APPROVED"


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# ── 대역 ────────────────────────────────────────────────────────────────


def _승인행(request_id: str, *, seq: int = 1, as_of: date = date(2026, 1, 9)) -> dict[str, Any]:
    """`approved_decisions` 가 내는 행의 모양 그대로."""
    return {
        "request_id": request_id,
        "decision_seq": seq,
        "as_of": as_of,
        "sim_run_id": 실행,
    }


def _약정(request_id: str, *, seq: int = 1, as_of: date = date(2026, 1, 9)) -> ApprovedCommitment:
    """실제 `ApprovedCommitment` 다. 🔴 **모양을 흉내 내지 않는다** — `purchase_id_for`
    가 `approval_id` 를 파싱하므로 가짜 객체로는 ID 짝을 못 잰다."""
    return ApprovedCommitment(
        approval_id=f"H1-{request_id}-{seq}",
        request_id=request_id,
        as_of=as_of,
        item="배추",
        scenario_label="기본",
        total_qty_kg=1000.0,
        total_amount_krw=6182450.0,
        inbound_lead_days=1,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=1000.0,
                arrival_date=as_of + timedelta(days=1),
                purchase_date=as_of,
                seq=1,
                amount_krw=6182450.0,
                payment_due_date=as_of + timedelta(days=30),
            ),
        ),
    )


class _전이:
    """`apply_approval` 대역. **부른 약정을 모으고 정해진 답을 준다.**"""

    def __init__(self, out: TransitionOut | None = None, boom: Exception | None = None) -> None:
        self.out = out or TransitionOut(status="APPLIED", parts=["finance", "logistics"])
        self.boom = boom
        self.calls: list[tuple[ApprovedCommitment, str | None]] = []

    def __call__(self, commitment: ApprovedCommitment, *, sim_run_id: str | None) -> TransitionOut:
        self.calls.append((commitment, sim_run_id))
        if self.boom is not None:
            raise self.boom
        return self.out


def _재시도(
    *,
    decisions: list[dict[str, Any]] | None = None,
    purchase_ids: list[str] | None = None,
    commitment_of: Any = None,
    전이: _전이 | None = None,
    as_of: date = 오늘,
) -> tuple[RetryOut, _전이]:
    문 = 전이 or _전이()
    out = retry_pending_transitions(
        as_of,
        sim_run_id=실행,
        decisions_of=lambda **kw: list(decisions or []),
        purchase_ids_of=lambda **kw: list(purchase_ids or []),
        commitment_of=commitment_of or (lambda request_id: _약정(request_id)),
        apply_fn=문,
    )
    return out, 문


# ══════════════════════════════════════════════════════════════════════
#  ① 🔴 찾는 식이 **헛돌지 않는다**
# ══════════════════════════════════════════════════════════════════════


def test_원장에_안_닿은_승인을_실제로_찾아낸다() -> None:
    """🔴 **0건을 찾고 통과하면 이 단계는 있으나 마나다.**

    ★★ 찾는 식이 늘 빈 목록을 돌려주도록 바뀌면 여기가 빨개진다 — 이 판 전체가
      *"미적용을 찾을 수 있다"* 위에 서 있다.
    """
    미적용 = pending_approvals(
        decisions=[_승인행("REQ-DAILY-20260109-배추")],
        purchase_ids=[],
        before=오늘,
    )

    assert len(미적용) == 1, "승인이 있고 원장이 비었는데 미적용을 못 찾았다"
    assert 미적용[0].request_id == "REQ-DAILY-20260109-배추"
    assert 미적용[0].decision_seq == 1
    assert 미적용[0].sim_run_id == 실행


def test_찾는_식이_쓰는_앞머리가_실제_매입ID의_앞머리다() -> None:
    """🔴 **여기가 갈리면 찾는 식이 에러 없이 늘 0건을 돌려준다.**

    ★★ `purchase_id_prefix_for` 와 `purchase_id_for` 가 **같은 규칙**을 쓰는지를
      실제 약정으로 잰다. 문자열을 한쪽에만 고치는 날 이 검사가 먼저 빨개진다.
    """
    약정 = _약정("REQ-DAILY-20260109-배추", seq=3)

    앞머리 = purchase_id_prefix_for("REQ-DAILY-20260109-배추", 3)
    실제 = purchase_id_for(약정, 1)

    assert 실제.startswith(앞머리), f"{실제!r} 가 {앞머리!r} 로 시작하지 않는다"


def test_실제_매입ID_가_서_있으면_미적용이_아니다() -> None:
    """★ 위 검사와 짝이다 — **진짜 ID 로** 걸러지는지를 잰다."""
    약정 = _약정("REQ-DAILY-20260109-배추")

    미적용 = pending_approvals(
        decisions=[_승인행("REQ-DAILY-20260109-배추")],
        purchase_ids=[purchase_id_for(약정, 1)],
        before=오늘,
    )

    assert 미적용 == ()


# ══════════════════════════════════════════════════════════════════════
#  ② 🔴 **멱등** — 이미 닿은 것은 다시 안 한다
# ══════════════════════════════════════════════════════════════════════


def test_이미_원장에_닿은_승인은_다시_안_한다() -> None:
    """🔴 **되돌리는 경로가 저장소에 없다.** 두 번 앉으면 지울 방법이 없다."""
    약정 = _약정("REQ-A")
    out, 전이 = _재시도(
        decisions=[_승인행("REQ-A")],
        purchase_ids=[purchase_id_for(약정, 1)],
    )

    assert 전이.calls == [], "이미 원장에 닿은 승인을 다시 세웠다"
    assert out.status == "NOTHING_DUE"


def test_회차가_하나라도_서_있으면_닿은_것이다() -> None:
    """★ 회차 번호를 안 본다 — **앞머리까지만으로** 판정한다.

    ⚠️ 반쪽만 선 승인을 막는 것은 이 단계가 아니라 `apply_approval` 의 한 트랜잭션이다.
    """
    미적용 = pending_approvals(
        decisions=[_승인행("REQ-A", seq=2)],
        purchase_ids=["PUR-REQ-A-D2-S2"],
        before=오늘,
    )

    assert 미적용 == ()


def test_다른_회차의_원장은_이_승인을_안_덮는다() -> None:
    """🔴 번복은 `decision_seq` 를 올린다 — **앞 회차 원장이 뒤 회차를 가리면 안 된다.**"""
    미적용 = pending_approvals(
        decisions=[_승인행("REQ-A", seq=2)],
        purchase_ids=["PUR-REQ-A-D1-S1"],
        before=오늘,
    )

    assert len(미적용) == 1
    assert 미적용[0].decision_seq == 2


# ══════════════════════════════════════════════════════════════════════
#  ③ 🔴 **오늘 승인은 안 본다** — 상태가 설 날이 아직 안 왔다
# ══════════════════════════════════════════════════════════════════════


def test_오늘_승인은_안_본다() -> None:
    """🔴 상태가 설 날은 승인일 **다음 날**이라 오늘 세울 자리가 없다.

    ★★ 그리고 같은 범위를 **다시 걸을 때** 이 줄이 없으면 첫날 재시도가 아직 오지
      않은 날의 승인까지 장부에 밀어 넣는다.
    """
    미적용 = pending_approvals(
        decisions=[_승인행("REQ-A", as_of=오늘)],
        purchase_ids=[],
        before=오늘,
    )

    assert 미적용 == ()


def test_어제_승인은_오늘_본다() -> None:
    """🟢 **도착일이 열린 날이 바로 오늘이다.** 이 한 줄이 이 판의 값이다."""
    미적용 = pending_approvals(
        decisions=[_승인행("REQ-A", as_of=date(2026, 1, 9))],
        purchase_ids=[],
        before=오늘,
    )

    assert len(미적용) == 1


def test_내일_승인은_안_본다() -> None:
    """🔴 같은 범위를 다시 걸어도 **미래를 장부에 밀어 넣지 않는다.**"""
    미적용 = pending_approvals(
        decisions=[_승인행("REQ-A", as_of=date(2026, 1, 11))],
        purchase_ids=[],
        before=오늘,
    )

    assert 미적용 == ()


# ══════════════════════════════════════════════════════════════════════
#  ④ 🔴 **또 실패해도 하루는 계속 간다**
# ══════════════════════════════════════════════════════════════════════


def test_전이가_또_실패해도_값으로_돌려준다() -> None:
    """🔴 **예외를 밖으로 내지 않는다.** `apply_approval` 의 태도 그대로다."""
    out, _ = _재시도(
        decisions=[_승인행("REQ-A")],
        전이=_전이(TransitionOut(status="FAILED", reason="전이 적재 실패: 행이 없다")),
    )

    assert out.status == "RAN"
    assert dict(out.outcomes) == {"FAILED": 1}
    assert "행이 없다" in _NFC(out.retried[0].reason)


def test_전이가_터져도_나머지를_계속_세운다() -> None:
    """🔴 하나 때문에 멈추면 그 뒤 승인이 전부 미적용으로 남고 다음 날 또 멈춘다."""

    class _하나만_터진다(_전이):
        def __call__(self, commitment, *, sim_run_id):  # type: ignore[no-untyped-def]
            self.calls.append((commitment, sim_run_id))
            if commitment.request_id == "REQ-A":
                raise RuntimeError("커넥션이 끊겼다")
            return TransitionOut(status="APPLIED", parts=["finance", "logistics"])

    out, 전이 = _재시도(
        decisions=[_승인행("REQ-A"), _승인행("REQ-B")],
        전이=_하나만_터진다(),
    )

    assert len(전이.calls) == 2, "하나가 터져서 나머지를 안 세웠다"
    assert dict(out.outcomes) == {"FAILED": 1, "APPLIED": 1}


def test_조회가_터지면_FAILED_이지_NOTHING_DUE_가_아니다() -> None:
    """🔴 *"미적용이 없다"* 와 *"있었는지 못 물어봤다"* 는 다르다."""

    def 터진다(**kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("DB 가 죽었다")

    out = retry_pending_transitions(
        오늘,
        sim_run_id=실행,
        decisions_of=터진다,
        purchase_ids_of=lambda **kw: [],
        commitment_of=lambda request_id: None,
        apply_fn=_전이(),
    )

    assert out.status == "FAILED"
    assert "DB 가 죽었다" in _NFC(out.reason)


def test_약정을_못_만들면_FAILED_로_접지_않는다() -> None:
    """🔴 전이 **앞에서** 끝난 일과 **쓰려다** 터진 일은 고칠 곳이 다르다."""
    out, 전이 = _재시도(decisions=[_승인행("REQ-A")], commitment_of=lambda request_id: None)

    assert dict(out.outcomes) == {"NOT_BUILDABLE": 1}
    assert 전이.calls == [], "약정이 없는데 전이를 불렀다"


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 🔴 **축을 지어내지 않는다**
# ══════════════════════════════════════════════════════════════════════


def test_승인_행이_든_실행_축을_그대로_넘긴다() -> None:
    """🔴 여기서 상수를 읽으면 판단은 걷기 축에, 전이는 번인에 앉는다."""
    out, 전이 = _재시도(decisions=[_승인행("REQ-A")])

    assert out.status == "RAN"
    assert 전이.calls[0][1] == 실행


def test_미적용이_없으면_전이를_이름조차_안_부른다() -> None:
    """🟢 **정상이다.** 승인이 다 닿았거나 아직 승인이 없다."""
    out, 전이 = _재시도(decisions=[])

    assert out.status == "NOTHING_DUE"
    assert 전이.calls == []
    assert out.retried == ()


@pytest.mark.parametrize(
    ("어휘", "안"),
    [
        ("APPLIED", TransitionOut(status="APPLIED", parts=["finance", "logistics"])),
        ("NOT_APPLIED", TransitionOut(status="NOT_APPLIED", reason="상태전이 미등록: finance")),
        ("FAILED", TransitionOut(status="FAILED", reason="전이 적재 실패")),
    ],
)
def test_전이_어휘를_접지_않고_그대로_싣는다(어휘: str, 안: TransitionOut) -> None:
    """⚠️ 접으면 *"아직 쓸 것이 없다"* 와 *"쓰려다 터졌다"* 가 같아 보인다."""
    out, _ = _재시도(decisions=[_승인행("REQ-A")], 전이=_전이(안))

    assert dict(out.outcomes) == {어휘: 1}


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 🟢 요약에 **접히지 않고** 올라온다
# ══════════════════════════════════════════════════════════════════════

넷 = ("APPLIED", "NOT_APPLIED", "FAILED", "NOT_BUILDABLE")


def _하루(outcomes: dict[str, int]) -> DayRunOutcome:
    return DayRunOutcome(
        as_of=오늘,
        action="RUN_NOW",
        reason="",
        pending_transition_status="RAN",
        pending_transition=RetryOut(
            status="RAN",
            retried=tuple(
                RetriedTransition(
                    request_id=f"REQ-{이름}-{i}",
                    decision_seq=1,
                    as_of=date(2026, 1, 9),
                    outcome=이름,  # type: ignore[arg-type]
                )
                for 이름, 수 in outcomes.items()
                for i in range(수)
            ),
        ),
    )


def test_전이_어휘_넷이_요약에_접히지_않고_올라온다() -> None:
    """🔴 **이 줄이 없어서 「승인 15건 RECORDED」 를 보고 원장에 닿은 줄 알았다.**

    ★★ 어느 승인이 원장에 안 닿았는지를 **요약만 보고** 알아야 한다.
    """
    결과 = WalkResult(start=오늘, end=오늘, days=(_하루({이름: 1 for 이름 in 넷}),))

    센것 = dict(결과.transition_outcomes)
    요약 = _NFC(format_summary(결과))

    assert 센것 == {이름: 1 for 이름 in 넷}
    for 이름 in 넷:
        assert 이름 in 요약, f"요약에 전이 어휘 '{이름}' 이 없다"
    assert _NFC("전이      ") in 요약


def test_전이_줄을_승인_줄과_한_칸에_담지_않는다() -> None:
    """🔴 *"승인을 적었다"* 와 *"그 승인이 원장에 닿았다"* 는 축이 하나 다르다."""
    결과 = WalkResult(start=오늘, end=오늘, days=(_하루({"APPLIED": 2}),))

    assert dict(결과.transition_outcomes) == {"APPLIED": 2}
    assert dict(결과.approval_outcomes) == {}, "승인 줄에 전이 결과가 섞였다"


def test_재시도를_안_탄_날은_요약이_세지_않는다() -> None:
    """★ 안 탄 날은 `None` 이다 — 0 으로 채우면 *"닿은 것이 없다"* 와 같아 보인다."""
    안탄날 = DayRunOutcome(as_of=오늘, action="WAIT", reason="")
    결과 = WalkResult(start=오늘, end=오늘, days=(안탄날,))

    assert dict(결과.transition_outcomes) == {}


# ══════════════════════════════════════════════════════════════════════
#  ⑦ 🔴 조회가 **무엇으로 좁히는가**
# ══════════════════════════════════════════════════════════════════════


def _잡아둔_조회(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    """`fetch_all` 이 받은 SQL 원문과 인자를 모은다. **DB 를 안 탄다.**"""
    잡힌: list[tuple[str, tuple[Any, ...]]] = []

    def 가짜(query: Any, params: tuple[Any, ...] = ()) -> list[Any]:
        잡힌.append((query.as_string(None), params))
        return []

    monkeypatch.setattr("app.master.pending_transition_repository.fetch_all", 가짜)
    return 잡힌


def test_승인_조회가_실행_축과_매입_사이클로_좁힌다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **축을 안 좁히면 남의 걷기와 번인 30일의 승인까지 끌고 온다.**

    🔴 **매입 사이클을 안 좁히면 판매 승인이 영영 미적용으로 남는다** — 판매 승인은
      `sales` 표로 흘러 `purchases` 에 안 앉기 때문이다.
    """
    from app.master.decision import PROCUREMENT_CYCLE
    from app.master.pending_transition_repository import approved_decisions

    잡힌 = _잡아둔_조회(monkeypatch)
    approved_decisions(sim_run_id=실행)

    원문, 인자 = 잡힌[0]
    assert 인자 == (실행, PROCUREMENT_CYCLE, "APPROVE")
    assert "DISTINCT ON" in 원문, "회차를 안 좁히면 취소된 승인이 영영 미적용으로 남는다"


def test_원장_조회도_실행_축으로_좁힌다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 남의 실행 원장을 *"닿았다"* 로 읽으면 이 승인이 영영 안 선다."""
    from app.master.pending_transition_repository import ledger_purchase_ids

    잡힌 = _잡아둔_조회(monkeypatch)
    ledger_purchase_ids(sim_run_id=실행)

    원문, 인자 = 잡힌[0]
    assert 인자 == (실행,)
    assert "settlement_status" not in 원문, (
        "정산 상태로 거르면 취소된 매입이 미적용으로 돌아와 같은 승인이 두 번 앉는다"
    )
