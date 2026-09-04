"""조정안이 **어느 안 · 어느 회차** 대상인지가 값으로 실린다.

2026-09-02 · 되먹임 계약 v0.2 §5.1 · §5.2 · 배선 2단계.

🔴 **전에는 시나리오 축이 아예 없었다.** 그 사실이 `reason` 문장 안에만 남아,
받는 쪽이 쓰려면 **부서 문장을 파싱해야 했다.**

```text
reason = "보수 시나리오 2026-01-03 회차 — 수량을 7120kg 로 조정 제안"
                        └─ 유일한 회차 정보
```

★ **부서 하나의 사정이 아니다.** 재무도 걸린다 — 상한 2,000만에 보수 1,500만 ·
  기본 2,100만 · 공격 2,800만이면 **기본·공격만 재조정 대상**인데 기계가 못 읽었다.

★ **회차는 번호가 아니라 날짜다** (물류 지정). 물류에 회차 번호가 없어서,
  번호 칸을 두면 **없는 값을 만들게 된다.**

★ **둘 다 선택이다.** 부서가 안 채우면 빈 목록·`None` 그대로 나간다 —
  안 채운 것과 해당 없는 것을 마스터가 갈라 주지 않는다.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date
from typing import Any

from app.contracts.core import SuggestedAdjustment
from app.master.answer import facts_from_procurement
from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner
from app.master.service import _adjustments_out, _to_response

AS_OF = date(2025, 12, 31)

SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _adj(**over: Any) -> SuggestedAdjustment:
    base: dict[str, Any] = {
        "dept": "inventory",
        "axis": "quantity",
        "target_value": 7120.0,
        "unit": "kg",
        "reason": "사유",
        "ref_ids": ("REF-1",),
    }
    base.update(over)
    return SuggestedAdjustment(**base)


def _advisor(adjustments: tuple[SuggestedAdjustment, ...] = (), verdict: str = "ok"):
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


def _purchaser(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
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
    meta = ExecutionMetadata(run_id=run_id, request_id=request.context.request_id, agent="purchase")
    return reply, meta


def _run(adjustments: tuple[SuggestedAdjustment, ...]):
    registry = AgentRegistry()
    registry.register("finance", _advisor())
    registry.register("inventory", _advisor(adjustments, verdict="conditional"))
    registry.register("purchase", _purchaser)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    return ProcurementFlow(runner, verifier=None, item="배추").run()


# ── ① 칸이 계약에 있다 ──────────────────────────────────────────────────────


def test_표준형에_시나리오_축과_회차가_있다():
    """🔴 이 둘이 없어서 사실이 사유 문장 안에만 남았다."""
    이름 = {f.name for f in fields(SuggestedAdjustment)}

    assert "scenario_labels" in 이름
    assert "split_date" in 이름


def test_회차는_번호가_아니라_날짜다():
    """물류에 회차 번호가 없다 — 번호 칸을 두면 **없는 값을 만들게 된다.**"""
    이름 = {f.name for f in fields(SuggestedAdjustment)}

    assert "split_seq" not in 이름, "번호 칸을 만들면 부서가 없는 값을 지어내야 한다"
    칸 = next(f for f in fields(SuggestedAdjustment) if f.name == "split_date")
    assert "date" in str(칸.type)


def test_안_채워도_만들어진다():
    """🔴 다섯 파트가 쓰는 계약이다 — 기존 호출부가 안 깨져야 한다.

    재무·물류가 지금 이 둘 없이 만들고 있다.
    """
    조정 = _adj()

    assert 조정.scenario_labels == ()
    assert 조정.split_date is None


# ── ② 마스터가 나른다 ───────────────────────────────────────────────────────


def test_결과에_그대로_남는다():
    원본 = _adj(scenario_labels=("보수", "기본"), split_date=date(2026, 1, 3))
    run = _run((원본,))

    assert run.adjustments[0].scenario_labels == ("보수", "기본")
    assert run.adjustments[0].split_date == date(2026, 1, 3)


def test_응답_변환까지_간다():
    """🔴 결과만 검사하면 `_adjustments_out` 에서 버려도 초록불이다.

    이번 주에 네 번 밟은 함정이다 (#154 · #157 · #164 · #168).
    """
    run = _run((_adj(scenario_labels=("공격",), split_date=date(2026, 1, 3)),))
    실린_것 = _adjustments_out(run)[0]

    assert 실린_것.scenario_labels == ["공격"]
    assert 실린_것.split_date == date(2026, 1, 3)


def test_응답_모델까지_간다():
    run = _run((_adj(scenario_labels=("보수",), split_date=date(2026, 1, 3)),))
    response = _to_response(_ctx(), run)

    assert response.adjustments[0].scenario_labels == ["보수"]


def test_이력에_적재되는_모양에_들어간다():
    """`master_agent_runs.response_payload` 는 이 덤프 그대로다."""
    run = _run((_adj(scenario_labels=("보수", "기본", "공격"), split_date=date(2026, 1, 3)),))
    dumped = _to_response(_ctx(), run).model_dump(mode="json")

    실린_것 = dumped["adjustments"][0]
    assert 실린_것["scenario_labels"] == ["보수", "기본", "공격"]
    assert 실린_것["split_date"] == "2026-01-03"


# ── ③ 마스터가 손대지 않는다 ────────────────────────────────────────────────


def test_라벨_순서를_바꾸지_않는다():
    """부서가 낸 차례가 그 부서의 설명 차례다.

    ★ 정렬하면 뒤집히는 순서를 쓴다 — 정렬해도 같은 순서면 변이를 못 잡는다.

    🔴 **처음 쓸 때 또 틀렸다.** `("공격","기본","보수")` 를 골랐는데 그게 마침
      정렬 순서였다. 이번 주 다섯 번째인데, **아래 단언이 먼저 걸렀다.**
      전제를 테스트 안에 적어 두면 데이터 고르기 실수를 코드가 잡는다.
    """
    라벨 = ("보수", "공격", "기본")
    assert sorted(라벨) != list(라벨), "정렬해도 같으면 이 검사가 무의미하다"

    run = _run((_adj(scenario_labels=라벨),))

    assert _adjustments_out(run)[0].scenario_labels == ["보수", "공격", "기본"]


def test_안_채운_것을_마스터가_채우지_않는다():
    """빈 목록은 **부서가 안 채운 것**이지 "해당 없음" 이 아니다.

    마스터가 시나리오 이름을 유추해 넣으면 그 순간 없는 사실이 생긴다.
    """
    run = _run((_adj(),))
    실린_것 = _adjustments_out(run)[0]

    assert 실린_것.scenario_labels == []
    assert 실린_것.split_date is None


# ── ④ 읽는 자리도 같이 고쳤다 ───────────────────────────────────────────────


def test_발화문이_어느_안_어느_회차인지_말한다():
    """🔴 **`reason` 에서 라벨·회차를 빼기로 하면서 생긴 자리다** (계약 v0.2 §5.4).

    같은 사실을 문장과 칸 두 곳에 두면 한쪽만 고쳐지는 날이 오므로 칸으로 옮기는데,
    **읽는 쪽을 같이 안 고치면 그 사실이 발화문에서 사라진다** — 값을 옮기면서
    읽는 자리를 빠뜨리는 것이 이번 주에 네 번 고친 그 모양이다.
    """
    run = _run(
        (
            _adj(
                scenario_labels=("보수", "기본"),
                split_date=date(2026, 1, 3),
                reason="수량을 7120kg 로 조정 제안",
            ),
        )
    )
    facts = facts_from_procurement(_to_response(_ctx(), run))
    적힌_것 = " ".join(facts.gaps)

    assert "보수·기본안" in 적힌_것
    assert "2026-01-03 회차" in 적힌_것
    assert "수량을 7120kg 로 조정 제안" in 적힌_것, "부서 문장은 그대로 남는다"


def test_안_채운_조정은_범위를_안_붙인다():
    """빈 목록은 *"안 채운 것"* 이지 *"해당 없음"* 이 아니다 — 지어내 가르지 않는다."""
    run = _run((_adj(reason="수량 조정"),))
    facts = facts_from_procurement(_to_response(_ctx(), run))
    적힌_것 = " ".join(facts.gaps)

    assert "수량 조정" in 적힌_것
    assert "안 ·" not in 적힌_것 and "회차" not in 적힌_것


def test_회차_개념이_없는_축은_None_이다():
    """재무 `amount` 는 회차가 없다 — 없는 것을 지어내지 않는다.

    ★ 재무 조정안은 **재무 포트로** 낸다. 봉투가 축 침범을 막아
      (`_AGENT_DEPT`) 물류 회신에 재무 조정안을 섞으면 계약 위반이다.
    """
    registry = AgentRegistry()
    registry.register(
        "finance",
        _advisor(
            (_adj(dept="finance", axis="amount", target_value=1.8e7, unit="krw"),),
            verdict="conditional",
        ),
    )
    registry.register("inventory", _advisor())
    registry.register("purchase", _purchaser)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    run = ProcurementFlow(runner, verifier=None, item="배추").run()

    실린_것 = _adjustments_out(run)
    assert len(실린_것) == 1, "재무 조정안이 안 실렸다"
    assert 실린_것[0].split_date is None
