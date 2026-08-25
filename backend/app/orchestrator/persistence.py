"""라우터가 계산 뒤에 실행이력을 적재하는 얇은 층.

★ 계산과 적재를 섞지 않는다. `service.py` 는 여전히 순수하고, 여기서만 DB 를 만진다.
  적재 실패는 응답을 막지 않는다 (`try_save_run` 이 삼킨다).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.orchestrator.run_repository import Agent, RunCycle, try_save_run

# 응답 모델 → (agent, cycle) 을 무엇으로 적을지. 응답이 스스로 밝히는 값만 쓴다.
_CYCLE_BY_AGENT: dict[str, str] = {
    "PROCUREMENT": "PROCUREMENT",
    "SALES": "SALES",
}


def _coverage(response: Any) -> tuple[int | None, int | None]:
    ratio = getattr(response, "coverage_ratio", None)
    if not ratio or len(tuple(ratio)) != 2:
        return None, None
    ran, total = tuple(ratio)
    return int(ran), int(total)


def record(
    func: Callable[[Any], BaseModel],
    request: BaseModel,
    *,
    agent: Agent,
    cycle: RunCycle,
) -> BaseModel:
    """계산을 돌리고, 끝난 뒤 요청·응답을 적재한다.

    소요 시간을 함께 남긴다 — LLM 이 붙은 뒤로 지연이 관측 대상이 됐다.
    """
    started = time.perf_counter()
    response = func(request)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    ran, total = _coverage(response)
    # `/day` 는 자기 llm_status 가 없다. 매입 하위 응답의 것을 대표로 적는다.
    llm_source = getattr(response, "procurement", None) or response

    try_save_run(
        agent=agent,
        cycle=cycle,
        as_of=getattr(response, "as_of", None) or getattr(request, "as_of", None),
        run_seq=getattr(request, "run_seq", 1) or 1,
        snapshot_id=getattr(response, "snapshot_id", None),
        runtime_status=getattr(response, "runtime_status", "READY") or "READY",
        critic_status=getattr(response, "status", None) if agent == "critic" else None,
        coverage_ran=ran,
        coverage_total=total,
        llm_status=getattr(llm_source, "llm_status", None),
        llm_model=getattr(llm_source, "llm_model", None),
        llm_attempts=getattr(llm_source, "llm_attempts", None),
        llm_fallback_used=getattr(llm_source, "llm_fallback_used", None),
        elapsed_ms=elapsed_ms,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response
