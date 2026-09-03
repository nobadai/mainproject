"""되먹임 payload 는 **전선에 실을 수 있어야 한다.**

2026-09-03 · #175 · 매입 회신 지적.

🔴 `asdict` 는 `date` 를 그대로 둔다. 그 dict 를 `json.dumps` 에 넣으면 죽는다.

```
TypeError: Object of type date is not JSON serializable
```

⚠️ **지금 안 터지는 이유가 더 나쁘다.** 물류 어댑터가 `split_date` 를 표준형에
안 옮겨서(`logistics/adapter.py:1122`) 늘 `None` 이라 통과한다. **물류가 칸을
채우는 순간 터진다** — 지금 그 작업 중이고 매입은 오늘 받는 자리를 만든다.

★ 매입은 *"받아서 바꾸는 쪽이 자연스럽다"* 고 했지만 **보내는 쪽이 한다.**
  `asdict` 로 편 것이 마스터라 마스터가 책임지고, 받는 쪽이 여럿이 되면 각자
  변환해 **같은 사실의 주인이 여럿**이 된다.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Any

from app.contracts.core import SuggestedAdjustment
from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow, _wire
from app.master.runner import AgentRegistry, MasterRunner

AS_OF = date(2025, 12, 31)
SPLIT = date(2026, 9, 11)

SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]


def _adj(*, split_date: date | None = SPLIT, labels: tuple[str, ...] = ("보수", "기본")):
    return SuggestedAdjustment(
        dept="inventory",
        axis="quantity",
        target_value=500.0,
        unit="kg",
        reason="창고 여유",
        ref_ids=("REF-1",),
        scenario_labels=labels,
        split_date=split_date,
    )


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _advisor(*, verdict: str = "ok", adjustments: tuple[SuggestedAdjustment, ...] = ()):
    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        pre = request.mode == "PRE_PURCHASE"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok" if pre else verdict,  # type: ignore[arg-type]
            payload={"cap": 1} if pre else {},
            suggested_adjustments=() if pre else adjustments,
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


class _Purchaser:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.payloads.append(dict(request.payload))
        run_id = f"PURCHASE-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="purchase",
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload={"scenarios": list(SCN)},
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent="purchase"
        )
        return reply, meta


def _run(adjustments: tuple[SuggestedAdjustment, ...]):
    """조언자가 `reject` 를 내야 2회차가 돌고 그때 조정안이 실린다."""
    purchaser = _Purchaser()
    registry = AgentRegistry()
    registry.register("finance", _advisor())
    registry.register("inventory", _advisor(verdict="reject", adjustments=adjustments))
    registry.register("purchase", purchaser)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    ProcurementFlow(runner, item="피마늘").run()
    return purchaser


# ── ① 병을 먼저 고정한다 ────────────────────────────────────────────────────


def test_asdict_만으로는_json_이_안_된다():
    """🔴 **이것이 병이다.** 고쳐야 할 이유를 검사 안에 남긴다.

    `_wire` 를 안 쓰고 `asdict` 를 그대로 쓰면 어떻게 되는지 고정한다 — 나중에
    누가 `_wire` 를 지우고 `asdict` 로 되돌리면 무엇이 깨지는지 여기서 보인다.
    """
    import pytest

    with pytest.raises(TypeError, match="date is not JSON serializable"):
        json.dumps(asdict(_adj()))


# ── ② 편 값이 전선에 실린다 ─────────────────────────────────────────────────


def test_split_date_가_ISO_문자열로_나간다():
    assert _wire(_adj())["split_date"] == "2026-09-11"


def test_없으면_None_그대로다():
    """**없는 것을 만들지 않는다** — 안 채운 부서의 빈 칸을 날짜로 지어내지 않는다."""
    assert _wire(_adj(split_date=None))["split_date"] is None


def test_라벨은_배열로_나간다():
    assert _wire(_adj())["scenario_labels"] == ["보수", "기본"]


def test_나머지_칸은_그대로다():
    """마스터는 **모양만 편다.** 값을 고치지 않는다 (§3.2.2)."""
    wired = _wire(_adj())

    assert wired["dept"] == "inventory"
    assert wired["axis"] == "quantity"
    assert wired["target_value"] == 500.0
    assert wired["unit"] == "kg"
    assert wired["reason"] == "창고 여유"
    assert wired["ref_ids"] == ["REF-1"]


def test_전선에_실은_것은_왕복해도_같다():
    """🔴 **이 파일의 기준이다.** 칸마다 세는 대신 성질 하나로 잠근다.

    `asdict` 는 튜플을 그대로 두는데 JSON 을 한 번 왕복하면 목록이 된다 —
    **같은 칸이 경로에 따라 두 모양**이 되고, 받는 쪽이 `== [...]` 로 비교하면
    in-process 에서만 조용히 어긋난다.

    ★ 새 칸이 늘어도 이 검사는 그대로 유효하다. `split_date` 만 검사했으면
      다음 `date` 칸에서 같은 병이 다시 난다.
    """
    wired = _wire(_adj())

    assert json.loads(json.dumps(wired)) == wired


def test_dataclass_는_여전히_date_객체다():
    """🔴 **계약 타입은 안 바꾼다.** 객체 안에서는 비교·연산이 되는 것이 맞다."""
    adjustment = _adj()

    _wire(adjustment)

    assert adjustment.split_date == SPLIT
    assert isinstance(adjustment.split_date, date)


# ── ③ 실경로 전체가 실린다 ──────────────────────────────────────────────────


def test_매입에_가는_payload_전체가_json_을_통과한다():
    """🔴 **칸 하나만 재면 다른 칸이 같은 병에 걸릴 때 못 잡는다.**

    `split_date` 만 검사하면 나중에 누가 `date` 를 쓰는 칸을 하나 더 넣었을 때
    똑같이 조용히 통과한다. **payload 통째로** 재는 검사가 그것을 막는다.
    """
    purchaser = _run((_adj(),))

    assert len(purchaser.payloads) == 2, "2회차가 돌아야 조정안이 실린다"
    json.dumps(purchaser.payloads[1])  # 죽으면 여기서 죽는다


def test_2회차_payload_의_조정안이_편_모양이다():
    """실제로 마스터가 보내는 값이 문자열인지 본다 — `_wire` 단위 검사와 별개다."""
    purchaser = _run((_adj(),))
    sent = purchaser.payloads[1]["adjustments"]

    assert sent[0]["split_date"] == "2026-09-11"
    assert sent[0]["scenario_labels"] == ["보수", "기본"]


def test_안_채운_부서의_조정안도_그대로_간다():
    """🔴 **지금 물류가 이 상태다** — 여섯 칸만 채우고 둘은 비운다.

    마스터가 정규화해도 **빈 값을 정규화할 뿐**이다. 그 사실이 payload 에 그대로
    드러나야 매입이 *"안 왔다"* 를 실측할 수 있다.
    """
    purchaser = _run((_adj(split_date=None, labels=()),))
    sent = purchaser.payloads[1]["adjustments"]

    assert sent[0]["split_date"] is None
    assert sent[0]["scenario_labels"] == []
    json.dumps(purchaser.payloads[1])
