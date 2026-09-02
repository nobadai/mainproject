"""같은 입력에 같은 결과가 나오는가 — **결정론을 잠근다.**

멘토 지적 2026-09-01: *"매입에서 시나리오 근거가 여러 번 실행했을 때도 얼마나
똑같은 안이 올 것인가 고민해보자."*

```text
층이 둘이다
  ① 마스터 층    부서를 고정하면 마스터는 완전히 같아야 한다   ← 이 파일이 잠근다
  ② 부서 층      부서 안에서 LLM 이 돌면 흔들릴 수 있다        ← 실측으로 잰다
```

★ **여기서 잠그는 것은 ①뿐이다.** 부서를 stub 으로 고정했을 때 마스터가 흔들리면
  그건 마스터 버그다 - 백테스트가 성립하지 않고, 되먹임을 붙여도 "달라진 것이
  되먹임 때문인지 우연인지" 를 영영 못 가른다.

★ **②는 테스트로 못 잡는다.** 실 DB 와 Gemini 가 필요하고, 그건 CI 에서 못 돈다.
  대신 실측 결과를 문서로 남긴다 ([[260902_재현성_측정]]).

⚠️ **완전히 같은지를 본다.** "거의 같다" 는 기준을 두면 무엇이 흔들려도 통과한다 -
  흔들려도 되는 값은 애초에 결과에 안 실려야 한다 (시각이 그래서 빠졌다).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner
from app.master.service import _evidences_out
from app.orchestrator.contracts_core import Evidence

AS_OF = date(2025, 12, 31)
ROUNDS = 5


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _evidence(claim: str, value: float = 100.0) -> Evidence:
    return Evidence(
        claim=claim,
        source="finance",
        ref_ids=(f"REF-{claim}",),
        value=value,
        unit="krw",
        evidence_grade="OFFICIAL",
    )


def _port(payload: dict[str, Any], evidences: tuple[Evidence, ...] = ()):
    """**고정된** 부서. 같은 요청에 늘 같은 답을 준다."""

    def port(request: AgentRequest):
        run_id = f"{request.agent.upper()}-{request.mode}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload=payload,
            evidences=evidences,
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


_SCENARIOS = {
    "situation": "stable",
    "allowed_axes": ["quantity"],
    "scenarios": [
        {
            "label": "보수",
            "total_qty_kg": 100,
            "total_amount_krw": 250000,
            "split_plan": [{"seq": 1, "qty_kg": 100, "date": "2025-12-31"}],
        },
        {
            "label": "기본",
            "total_qty_kg": 200,
            "total_amount_krw": 500000,
            "split_plan": [{"seq": 1, "qty_kg": 200, "date": "2025-12-31"}],
        },
    ],
}


def _run_once():
    registry = AgentRegistry()
    registry.register("finance", _port({"cap": 1}, (_evidence("finance_cap_amount_krw", 3.1e7),)))
    registry.register("inventory", _port({"free": 2}, (_evidence("warehouse_free_kg", 7637.0),)))
    registry.register("purchase", _port(_SCENARIOS))
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    return ProcurementFlow(runner, verifier=None, item="피마늘").run()


# ── ① 실행 계획 ─────────────────────────────────────────────────────────────


def test_실행_계획이_N회_모두_같다():
    """누구를 어떤 목적으로 몇 번째로 불렀나 — **백테스트의 전제다.**

    이것이 흔들리면 "어제 결과와 오늘 결과가 다른 이유" 를 영영 설명할 수 없다.
    """
    signatures = {tuple(_run_once().plan.signature) for _ in range(ROUNDS)}
    assert len(signatures) == 1, f"{ROUNDS}회 중 실행 계획이 {len(signatures)}가지로 갈렸다"


# ── ② 안 ────────────────────────────────────────────────────────────────────


def test_안이_N회_모두_같다():
    """부서를 고정하면 안도 완전히 같아야 한다.

    ★ 라벨만 보지 않는다 - 같은 '기본' 이 다른 수량이면 그것이 정확히 위험한 경우다
      (DECISION-COLLISION 이 막으려는 것과 같은 종류).
    """
    seen = {
        tuple(
            (s.get("label"), s.get("total_qty_kg"), s.get("total_amount_krw"))
            for s in _run_once().scenarios
        )
        for _ in range(ROUNDS)
    }
    assert len(seen) == 1, f"{ROUNDS}회 중 안이 {len(seen)}가지로 갈렸다: {seen}"


def test_종료코드가_N회_모두_같다():
    codes = {_run_once().end_code for _ in range(ROUNDS)}
    assert len(codes) == 1, f"종료 코드가 갈렸다: {codes}"


# ── ③ 근거 ──────────────────────────────────────────────────────────────────


def test_근거가_N회_모두_같다():
    """🔴 **근거가 흔들리면 안이 같아도 설명이 달라진다.**

    사람이 어제 본 근거와 오늘 본 근거가 다르면 "왜 이 숫자인가" 에 대한 답이
    실행마다 바뀐다 - 값이 같아도 신뢰가 무너진다. 순서까지 본다.
    """
    seen = {
        tuple((e.agent, e.mode, e.claim, e.value, e.unit) for e in _evidences_out(_run_once()))
        for _ in range(ROUNDS)
    }
    assert len(seen) == 1, f"{ROUNDS}회 중 근거가 {len(seen)}가지로 갈렸다"


# ── ④ 무엇이 흔들려도 되는가 ────────────────────────────────────────────────


def test_실행_계획의_모든_값이_N회_같다():
    """★ **시계가 끼어들면 여기서 걸린다 — 이름을 추측하지 않고.**

    처음에는 필드 이름에 `time` 이 들어가는지 봤는데 `runtime_status` 가 걸렸다.
    **이름으로 재는 검사의 전형적 오탐**이고, 이번 주에 두 번 겪은 것과 같다
    (오케 차단 AST · 옛 표 이름 docstring).

    이름을 안 보고 **값이 흔들리는지**를 본다. 시각이든 난수든 실행마다 달라지는
    것이 끼어들면 N회 비교에서 잡힌다. `ExecutionStep` 의 필드 **집합**은
    `test_계획에_실행_시각이_없다` 가 따로 고정하므로 둘이 겹치지 않는다.
    """
    def steps_snapshot() -> str:
        return repr([sorted(vars(s).items()) for s in _run_once().plan.steps])

    seen = {steps_snapshot() for _ in range(ROUNDS)}
    assert len(seen) == 1, f"{ROUNDS}회 중 실행 계획 값이 {len(seen)}가지로 갈렸다"


def test_두_실행의_결과_객체가_완전히_같다():
    """★ **위 넷을 한 번에 본다.**

    개별 검사는 내가 고른 필드만 본다 - 새 필드가 늘면 그것은 안 본다.
    결과 전체를 비교하면 **모르는 사이에 흔들리는 값이 생기는 것**을 잡는다.

    ⚠️ `plan` 은 뺀다. `ExecutionPlan` 이 dataclass 가 아니라 동등 비교가
      객체 동일성이 된다 - 계획 자체는 `signature` 로 위에서 이미 봤다.
    """

    def snapshot(o) -> str:
        # ★ repr 로 통째로 찍는다. 필드마다 해시 가능한 모양을 만들면 **내가 고른
        #   필드만** 보게 되고, 새 필드가 늘 때 그것은 안 본다 - 이 검사의 목적과 반대다.
        return repr(
            (
                o.end_code,
                o.reason,
                o.scenarios,
                o.judgment,
                o.constraints,
                o.verdicts,
                o.evidences,
                o.findings,
                o.concerns,
                o.skipped_checks,
                o.purchase_attempts,
            )
        )

    seen = {snapshot(_run_once()) for _ in range(ROUNDS)}
    assert len(seen) == 1, f"{ROUNDS}회 중 결과가 {len(seen)}가지로 갈렸다"
