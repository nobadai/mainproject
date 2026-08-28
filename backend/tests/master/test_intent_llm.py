"""의도 분류 — **실 모델** 채점 (기본 실행에서 제외 · `-m llm` 으로 돈다).

정답을 아는 발화문으로 분류기를 채점한다. 프롬프트를 고칠 때 **성적이 떨어지지
않는지** 보는 것이 목적이다.

```bash
uv run pytest tests/master/test_intent_llm.py -m llm -v
```

★ **왜 이 테스트가 필요한가** — 프롬프트는 고칠 때마다 한쪽이 좋아지고 다른 쪽이
  나빠진다. 실제로 "창고/재고" 어휘를 넣었더니 `SELECT_SCENARIO` 가 깨졌고,
  라벨 규칙을 더해 되돌렸다. **점수가 없으면 그 교환을 못 본다.**

★ 기본 실행에서 빼는 이유는 모델·서버가 있어야 돌기 때문이다
  (`pyproject.toml` 의 `addopts = "-m 'not llm'"`).
"""

from __future__ import annotations

import pytest

from app.master.llm.runtime import get_intent_service

pytestmark = pytest.mark.llm

# (발화문, 기대 action, 반드시 포함해야 하는 agents)
CASES = [
    ("지금 자금 상황 알려줘", "STATUS_QUERY", {"finance"}),
    ("돈 얼마나 있어?", "STATUS_QUERY", {"finance"}),
    ("창고에 얼마나 남았어?", "STATUS_QUERY", {"inventory"}),
    ("재고 어때?", "STATUS_QUERY", {"inventory"}),
    ("지금 자금이랑 창고 상황 둘 다 알려줘", "STATUS_QUERY", {"finance", "inventory"}),
    ("오늘 배추 얼마나 사야 해?", "PROCUREMENT_RUN", set()),
    ("무 매입안 뽑아줘", "PROCUREMENT_RUN", set()),
    ("예산 2천만원으로 낮춰서 다시 해줘", "RERUN_WITH_CONDITION", set()),
    ("기본안으로 진행해", "SELECT_SCENARIO", set()),
    ("보수안 선택할게", "SELECT_SCENARIO", set()),
    ("그거 있잖아 그거", "UNKNOWN", set()),
]


@pytest.fixture(scope="module")
def service():
    return get_intent_service()


@pytest.mark.parametrize(
    ("utterance", "want_action", "want_agents"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_분류가_정답과_맞는다(service, utterance, want_action, want_agents):
    result = service.classify(utterance)
    intent = result.intent

    assert result.llm_status in {"SUCCESS", "FALLBACK"}, (
        f"모델을 부르지 못했다 ({result.llm_status}) — 프로바이더 설정을 확인하라"
    )
    assert intent.action == want_action, (
        f"'{utterance}' → {intent.action} (기대 {want_action}) · "
        f"시도 {result.llm_attempts}회 · {result.llm_model}"
    )
    if want_agents:
        assert want_agents <= set(intent.agents), (
            f"'{utterance}' 의 부서가 {intent.agents} — {sorted(want_agents)} 를 포함해야 한다"
        )


def test_고른_안은_라벨을_달고_온다(service):
    """`SELECT_SCENARIO` 인데 라벨이 없으면 검증에 걸려 `UNKNOWN` 으로 떨어진다.

    실제로 그렇게 깨졌던 적이 있어 별도로 잠근다.
    """
    intent = service.classify("기본안으로 진행해").intent
    assert intent.action == "SELECT_SCENARIO"
    assert intent.scenario_label, "고른 안의 이름이 비어 있다"
