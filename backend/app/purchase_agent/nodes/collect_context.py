"""② collect_context — 통과 스텁 (상세설계 §4-②, 구현은 Epic 3 / 백로그 E3-3)."""

from typing import Any

from app.purchase_agent.state import PurchaseAgentState


def collect_context(state: PurchaseAgentState) -> dict[str, Any]:
    """uncertain일 때만 진입하는 문서 선택 로드 루프. 지금은 아무것도 읽지 않는다.

    구현하면: LLM이 읽을 doc_type을 고르고(관측월보 → 기상 → 작년 동기) ``get_context_docs``로
    전문을 주입한 뒤 "판단에 충분한가?"를 자문하며 ``context.loop_max``(3)까지 반복한다.
    검색 엔진은 없지만 **상황에 따라 탐색 경로가 달라지는 agentic 구조**는 이 루프가 유지한다.

    스텁이 빈 목록을 반환하므로 ⑥의 ``context_docs_used``도 비고, 그 사실이 출력에 그대로
    드러난다 — "문서를 읽었는데 근거에 안 썼다"와 "아직 안 읽는다"가 구분된다.
    """
    return {"context_docs": [], "context_loop_count": state["context_loop_count"]}
