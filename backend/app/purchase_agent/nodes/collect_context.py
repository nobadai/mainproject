"""② collect_context — 문서 선택 로드 루프 (상세설계 §4-② · 백로그 E3-4).

**uncertain일 때만 돈다.** 그래프의 조건부 분기가 stable한 날은 이 노드를 통째로 건너뛴다
(§4-②: "문서를 읽을지 말지부터가 판단이다").

검색 엔진은 없다. `doc_type`을 골라 **선택 로드**한다 — 코퍼스가 작아 전문을 통째로
주입한다(§2). 그래도 **"상황에 따라 탐색 경로가 달라지는" agentic 구조는 루프가 유지한다.**

**포트 호출 위치의 유일한 예외가 여기다.** ①~⑤는 T0(``build_initial_state``)에서 한 번씩
불리지만 ⑥ ``get_context_docs``는 이 노드가 **런타임에** 부른다 (정의서 §3.1.1 · 팀 확인
2026-08-25 · IO명세 §0). 문서는 ``published_at <= as_of``로 고정된 불변 발행물이라 사이클
중에 값이 변하지 않기 때문이다. 근거 전문은 ``ports.get_context_docs`` docstring에 있다.

**LLM 자리는 두 곳뿐이고 지금은 규칙이 대신한다** (규칙 6):

1. "다음에 어떤 문서를 읽을까" → 우선순위 목록 순서
2. "판단에 충분한가?"       → 판정하지 않는다. 목록 소진 또는 ``loop_max``까지 계속

2번을 "1건 찾으면 충분"으로 바꾸면 안 된다 — 규칙은 충분성을 판정할 수 없고, 조기 종료는
**판정하지 않은 것을 판정한 것처럼** 만든다. 그 사실은 ⑥이 risks에 고지한다.
"""

from datetime import date
from typing import Any

from app.purchase_agent import ports
from app.purchase_agent.config import load_constraints
from app.purchase_agent.ports import MockNotAllowed
from app.purchase_agent.state import PurchaseAgentState

#: 절단 표시. **발췌가 문장 경계가 아니라 글자 수로 잘렸다는 사실을 발췌 자체에 남긴다.**
#: 현서님 2차 피드백의 반례가 이 상수의 존재 이유다 — "상승하지 않았다"가 상한에 걸려
#: "상승"으로 잘리면 **부정이 사라진 완결된 주장**이 되어 원문과 정반대로 읽힌다.
#: "상승…"이면 읽는 쪽이 문장이 끝나지 않았음을 안다.
TRUNCATION_MARK = "…"


def leading_excerpt(content: str, max_chars: int) -> tuple[str, bool]:
    """인용 발췌 — 규칙 단계에서는 **본문 서두**를 그대로 뜬다.

    현서님 합의 8/25 (IO명세 §0 P2): 문서를 근거로 쓰면 ``ref_id`` + 해당 구절을 출력에
    동봉한다. Critic은 DB 조회가 금지라 발췌 없이는 근거 대조 자체가 성립하지 않는다.

    **어느 구절이 관련 있는가는 LLM 판단이다.** 규칙은 그걸 못 하므로 하는 척하지 않고
    서두를 뜬다 — 원문 훼손이 없어 Critic 대조는 성립하고, "선별은 아직 없다"는 사실은
    ⑥이 risks에 적는다. LLM이 붙으면 **이 함수 본문만** 바뀐다.

    **반환값 두 번째는 "글자 수로 잘렸는가"다.** 접미사를 보고 되짚지 않는 이유는 원문이
    ``…``로 끝나는 경우와 구분할 수 없기 때문이다 — 잘랐다는 사실은 자른 쪽만 안다.
    ⑦의 발췌 대조가 이 값으로 표시를 떼고 원문과 맞춘다.

    ⚠️ **문장 경계 파서가 아니다.** 한국어 종결이 "…다."라 그 첫 등장에서 자르지만,
    ``그는 '끝이다.'라고 말했다`` 같은 문장은 중간에서 잘린다. 못 찾으면 ``max_chars``까지
    자르는데 그건 문장이 아니라 그냥 앞부분이다. 그래서 함수 이름도 ``first_sentence``가
    아니고, ⑥의 risks 문구도 "첫 문장"이라고 주장하지 않는다 (Codex 교차검증 지적).
    **잘라내기만 하므로 원문 문자는 변조되지 않는다** — 대조는 어느 경우든 성립한다.

    빈 발췌는 돌려주지 않는다. 발췌 없는 인용은 Critic이 대조할 수 없어 "근거를 동봉했다"가
    거짓이 되므로, 만든 쪽에서 터뜨린다 — ``published_at`` 없는 문서를 로더가 적재 거부하는
    것과 같은 자리다 (IO명세 §1-⑥).
    """
    head, terminator, _ = content.partition("다.")
    if terminator:
        excerpt, truncated = head + terminator, False
    else:
        # 종결을 못 찾은 경로. **상한에 실제로 걸렸을 때만** 잘렸다고 표시한다 —
        # 본문이 상한보다 짧으면 전문이 그대로 발췌이고, 거기 표시를 붙이면
        # 잘리지 않은 것을 잘렸다고 말하는 게 된다.
        excerpt, truncated = content[:max_chars], len(content) > max_chars
    # 빈 검사는 **표시를 붙이기 전에** 한다. 붙인 뒤에 보면 공백뿐인 본문도
    # 표시 한 글자 때문에 non-empty가 되어 이 방어가 통째로 무력해진다.
    if not excerpt.strip():
        raise ValueError("cannot build a citation excerpt from empty content; refusing load")
    return (excerpt + TRUNCATION_MARK if truncated else excerpt), truncated


def select_doc_types(constraints: dict) -> list[str]:
    """읽을 순서. **constraints가 소유한다** (규칙 7) — 코드에 박지 않는다.

    ← **LLM 자리 ①**: "지금 상황에 어떤 문서가 필요한가"를 고르는 지점이다.
    규칙 단계는 §4-②의 고정 우선순위(관측월보 → 기상 → 작년 동기)를 그대로 쓴다.
    """
    return list(constraints["context"]["doc_type_priority"])


def is_enough(docs: list[dict]) -> bool:
    """"판단에 충분한가?" — **규칙 단계는 판정하지 않는다.**

    ← **LLM 자리 ②**: §4-②의 "부족 시 다른 doc_type 추가 로드"를 결정하는 지점이다.

    항상 ``False``를 돌려 목록이 소진되거나 ``loop_max``에 닿을 때까지 계속 읽는다.
    ``docs``를 받지만 쓰지 않는 건 의도다 — 시그니처를 미리 맞춰두면 LLM이 붙을 때
    호출부가 그대로다. 조기 종료 규칙을 임의로 만들면(예: "1건이면 충분") 판정한 적 없는
    것을 판정한 것처럼 만들고, 9/4에서 DOC-4·5가 조용히 사라진다.
    """
    return False


def collect_context(state: PurchaseAgentState) -> dict[str, Any]:
    """우선순위 순으로 문서를 선택 로드한다. 루프 상한은 ``context.loop_max``(3).

    탈출 조건 두 가지 — **둘 다 for 경계로 표현한다.** ``while``로 쓰면 "언젠가 끝난다"가
    조건문 안에 숨고, 조건이 틀리면 그래프가 멈춰 선다:

    - ``range(loop_max)``      : 재진입 상한
    - 목록 소진 시 ``break``   : 더 읽을 유형이 없다

    **같은 문서를 두 번 담지 않는다.** 유형은 목록에서 소비하고(``pop``), 그래도 ``doc_id``로
    한 번 더 거른다. 한 유형에 여러 문서가 걸리는 날이 있어(9/11 배추 관측월보 = DOC-3·6)
    유형 소비만으로는 부족하다. 중복이 들어가면 ``context_docs_used``에 같은 DOC이 두 번
    실리고 rationale도 두 벌이 된다.

    **``published_at`` 필터를 여기서 다시 하지 않는다.** 포트가 이미 한다
    (``mocks._load.filter_by_published_at``). 두 곳에 두면 한쪽만 바뀐다.

    빈 목록으로는 포트를 **부르지 않는다** — ``doc_types=[]``는 ``ValueError``다.

    **``loop_max``는 누적 상한이다.** ``state["context_loop_count"]``에서 이어 세고 남은
    예산만큼만 돈다 — §3이 이 필드를 "max 3"으로 규정하므로, 재진입해도 그 값을 넘으면
    안 된다. 지금 그래프엔 ②로 돌아오는 간선이 없어 도달 불가한 경로지만, 필드가 약속한
    불변을 코드가 아니라 배선이 지키게 두면 간선 하나 추가에 조용히 깨진다.
    """
    constraints = load_constraints()
    loop_max = constraints["context"]["loop_max"]
    excerpt_max_chars = constraints["context"]["excerpt_max_chars"]
    remaining = select_doc_types(constraints)

    # 벽시계를 읽지 않는다 (규칙 1). State에 실려 온 as_of만 포트에 넘긴다.
    as_of = date.fromisoformat(state["date"])
    collected: list[dict] = []
    seen: set[object] = set()
    loops = state["context_loop_count"]

    for _ in range(max(0, loop_max - loops)):
        if not remaining or is_enough(collected):
            break
        doc_type = remaining.pop(0)
        loops += 1
        # 요청한 유형에 문서가 없는 날도 정상이다 (무·양파엔 기상·작년동기 문서가
        # 없다). 빈 회차도 한 번의 시도로 세어야 "찾아봤지만 없었다"가 루프 수에 남는다.
        #
        # 🔴 **"없다" 와 "못 읽는다" 는 다르다** (2026-09-03).
        #
        #   앞엣것은 그날의 사실이고, 뒤엣것은 **판단 재료를 아예 못 구한 상태**다.
        #   둘을 같이 빈 목록으로 두면 화면에 *"문서를 찾아봤지만 없었다"* 로 나가는데,
        #   실제로는 **연습 데이터를 쓰려다 막힌 것**이다 (§1.2-10).
        #
        # ★ 여기서 멈추지 않고 사실만 담는다. 안을 낼지 말지는 ⑦ self_check 이 정한다 —
        #   판정은 한 자리에 모여 있어야 사유가 갈리지 않는다.
        try:
            found = ports.get_context_docs(state["item"], as_of, [doc_type])
        except MockNotAllowed as blocked:
            return {
                "context_docs": collected,
                "context_loop_count": loops,
                "context_unavailable": str(blocked),
            }
        for doc in found:
            if doc["doc_id"] in seen:
                continue
            seen.add(doc["doc_id"])
            excerpt, truncated = leading_excerpt(doc["content"], excerpt_max_chars)
            collected.append(
                {**doc, "excerpt": excerpt, "excerpt_truncated": truncated}
            )

    return {"context_docs": collected, "context_loop_count": loops}
