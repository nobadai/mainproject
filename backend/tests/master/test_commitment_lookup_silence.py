"""약정을 **못 읽은 것**과 **승인이 없었던 것**을 가른다.

2026-09-03 · `#185` 후속.

🔴 `_approved_commitments` 가 `list[dict]` 하나를 돌려줬다. 그래서 **셋이 전부 빈
목록**이었다.

```text
승인이 없었다              정상
조회가 깨졌다              사고   ← 조용히 빈 목록
승인은 있는데 약정을 못 만들었다  사고   ← 조용히 걸러짐
```

받는 쪽에서 셋이 구별되지 않으면 *"어제 승인이 없었나 보다"* 로 읽힌다.
§1.2-10 의 **0 과 모름은 다르다**가 그대로 걸리는 자리다.

★ **막지 않고 드러낸다.** 이력 DB 는 없어도 Flow 가 도는 것이 계약이라
  (`history_enabled`) 예외를 올리지 않는다. `concerns` 로 남긴다 — 매입을 몇 번
  다시 불러도 DB 가 안 읽히는 사실은 그대로라 `findings` 가 아니다.

⚠️ 원래 docstring 에 *"그 사실이 응답에 남는 자리가 아직 없다 — #185 후속으로
  둔다"* 라고 적어 두고 사흘을 뒀다. 이 파일이 그것을 닫는다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.master.decision import ArrivalLegOut, CommitmentOut
from app.master.schemas import ProcurementRunRequest
from app.master.service import _approved_commitments

AS_OF = date(2026, 1, 2)


def _request(item: str | None = "배추") -> ProcurementRunRequest:
    return ProcurementRunRequest(as_of=AS_OF, policy_version="v1.3", item=item)


def _built(**kw) -> CommitmentOut:
    base = {
        "approval_id": "H1-REQ-20260101-0001-1",
        "item": "배추",
        "scenario_label": "기본",
        "arrival_schedule": [
            ArrivalLegOut(
                item="배추",
                qty_kg=500.0,
                arrival_date=date(2026, 1, 4),
                purchase_date=date(2026, 1, 2),
                seq=1,
            )
        ],
    }
    base.update(kw)
    return CommitmentOut(**base)


def _patch(monkeypatch: pytest.MonkeyPatch, result) -> None:
    def fake(item, as_of, **kw):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("app.master.service.commitments_before", fake)


# ── ① 핵심 — 못 읽은 것을 없는 것으로 만들지 않는다 ──────────────────────────


def test_조회가_깨지면_그_사실이_남는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **이 파일의 주장이다.** 전에는 `except: return []` 하나였다."""
    _patch(monkeypatch, RuntimeError("connection refused"))

    lookup = _approved_commitments(_request())

    assert lookup.carried == []
    assert len(lookup.concerns) == 1, f"조회 실패가 조용히 넘어갔다: {lookup.concerns}"
    note = lookup.concerns[0]
    assert "못 읽었다" in note
    assert "승인이 없었다" in note, "빈 목록과 다르다는 것을 안 적으면 같아 보인다"


def test_승인이_없으면_아무것도_안_적는다(monkeypatch: pytest.MonkeyPatch):
    """대조군. 이것이 없으면 위 검사가 **항상 concern 을 내는 코드**로도 통과한다."""
    _patch(monkeypatch, [])

    lookup = _approved_commitments(_request())

    assert lookup.carried == []
    assert lookup.concerns == (), f"정상 상태를 사고로 적었다: {lookup.concerns}"


def test_조회가_깨져도_실행을_막지_않는다(monkeypatch: pytest.MonkeyPatch):
    """이력 DB 가 없어도 Flow 는 돈다 (`history_enabled` 와 같은 규약).

    예외를 올리면 어제 이력이 없는 날 매입이 통째로 못 돈다.
    """
    _patch(monkeypatch, RuntimeError("connection refused"))

    _approved_commitments(_request())  # 예외가 올라오면 여기서 죽는다


# ── ② 승인은 있는데 못 실은 것 ──────────────────────────────────────────────


def test_약정을_못_만든_승인은_사유까지_남는다(monkeypatch: pytest.MonkeyPatch):
    """`buildable=False` 는 조용히 걸러졌다.

    `CommitmentOut` 이 `reason` 을 이미 들고 있었는데 **아무도 안 봤다.**
    """
    _patch(monkeypatch, [_built(buildable=False, reason="라벨 '기본' 이 2개다")])

    lookup = _approved_commitments(_request())

    assert lookup.carried == [], "못 만든 약정을 경계 호출에 실으면 안 된다"
    assert len(lookup.concerns) == 1
    assert "라벨 '기본' 이 2개다" in lookup.concerns[0], "사유를 안 나르면 조사할 것이 없다"


def test_도착_일정이_빈_약정은_실리되_드러난다(monkeypatch: pytest.MonkeyPatch):
    """⚠️ **이쪽이 더 위험하다.**

    `buildable=True` 라 경계 호출에 실린다. 물류는 받기는 받고 **"입고 예정이
    없다"** 로 읽는다 — `CommitmentOut.notes` 가 사유를 들고 있는데 안 봤다.
    """
    _patch(monkeypatch, [_built(arrival_schedule=[], notes=["리드타임이 없어 도착일을 못 걸었다"])])

    lookup = _approved_commitments(_request())

    assert len(lookup.carried) == 1, "약정 자체는 섰으므로 싣는다"
    assert len(lookup.concerns) == 1
    assert "리드타임이 없어" in lookup.concerns[0]


def test_온전한_약정은_조용히_실린다(monkeypatch: pytest.MonkeyPatch):
    """대조군 둘째. 정상 경로가 시끄러워지지 않았는지 본다."""
    _patch(monkeypatch, [_built()])

    lookup = _approved_commitments(_request())

    assert len(lookup.carried) == 1
    assert lookup.concerns == (), f"정상 약정에 concern 이 붙었다: {lookup.concerns}"


def test_어느_승인인지_적는다(monkeypatch: pytest.MonkeyPatch):
    """여러 건이 걸릴 수 있다. **어느 것이 문제인지** 없으면 사람이 못 찾는다."""
    _patch(
        monkeypatch,
        [
            _built(approval_id="H1-A", buildable=False, reason="첫째 사유"),
            _built(approval_id="H1-B", buildable=False, reason="둘째 사유"),
        ],
    )

    lookup = _approved_commitments(_request())

    joined = " ".join(lookup.concerns)
    assert "H1-A" in joined and "H1-B" in joined, f"어느 승인인지 안 적혔다: {lookup.concerns}"


# ── ③ 품목이 없으면 묻지 않는다 ─────────────────────────────────────────────


def test_품목이_없으면_조회_자체를_안_한다(monkeypatch: pytest.MonkeyPatch):
    """약정은 품목별이라 물을 대상이 없다. **없는 조회의 실패를 적으면 오탐이다.**"""
    called: list[str] = []

    def fake(item, as_of, **kw):
        called.append(item)
        raise AssertionError("품목이 없는데 조회했다")

    monkeypatch.setattr("app.master.service.commitments_before", fake)

    lookup = _approved_commitments(_request(item=None))

    assert called == []
    assert lookup.carried == [] and lookup.concerns == ()
