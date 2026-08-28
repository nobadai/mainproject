"""E3-4 검사 — ② collect_context 문서 선택 로드 루프 (백로그 E3-4 · 상세설계 §4-②).

백로그 E3-4 DoD: **"uncertain에서 루프·조기종료, look-ahead 차단"**.

⚠️ **`loop_max`(3)와 우선순위 목록 길이(3)가 우연히 같다.** mock만 돌리면 "상한에서 멈췄다"와
"목록을 다 썼다"가 구분되지 않는다 — 둘 다 3회다. E3-3의 수량 트리거와 같은 상황이라
**둘을 갈라놓는 합성 입력**으로 따로 시험한다.

4품목 × 4앵커 전횡단을 기본으로 깐다 — E3-1에서 배추만 돌려 양파·피마늘 크래시를 놓친 교훈이다.
9/4에 배추만 문서 3건이고 나머지 3품목은 관측월보 1건뿐이라, **빈 회차 경로가 전횡단으로만
드러난다.**
"""

from datetime import date

import pytest

from app.purchase_agent import ports
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import build_graph, run_purchase_agent
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.collect_context import (
    TRUNCATION_MARK,
    collect_context,
    is_enough,
    leading_excerpt,
    select_doc_types,
)
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import (
    _context_rationale,
    _context_risks,
    package_scenarios,
)
from app.purchase_agent.nodes.self_check import (
    check_document_publication,
    check_document_refs,
    check_excerpt_fidelity,
    self_check,
)
from app.purchase_agent.nodes.split_plan import split_plan
from app.purchase_agent.state import build_initial_state

RISING = date(2026, 8, 21)
FALLING = date(2026, 8, 28)
UNCERTAIN = date(2026, 9, 4)
SPREAD_WIDE = date(2026, 9, 11)
#: 통합 시연 앵커 (#73). 성격은 rising 과 같고 날짜만 다르다 — 여기서는 ② 진입 잠금
#: (stable 이면 문서 포트를 0회 부른다)을 한 앵커 더 걸어 두는 값이 있다.
INTEGRATION = date(2025, 12, 31)
ANCHORS = (INTEGRATION, RISING, FALLING, UNCERTAIN, SPREAD_WIDE)
ITEMS = ("배추", "무", "피마늘", "양파")

ITEM = "배추"


def _classified(item: str = ITEM, as_of: date = UNCERTAIN) -> dict:
    """①까지 돌린 상태 — ②는 situation 분기 뒤에 오므로 ①이 선행해야 한다."""
    state = build_initial_state(item, as_of)
    state.update(classify_situation(state))
    return state


@pytest.fixture(scope="module")
def proposals() -> dict[date, dict]:
    return {as_of: run_purchase_agent(ITEM, as_of) for as_of in ANCHORS}


# ── E3-4 DoD: 9/4에서 DOC-3~5 로드 ──────────────────────────────────────────


def test_uncertain_day_loads_the_whole_priority_list(proposals: dict) -> None:
    """9/4 배추 = DOC-3(관측월보) · DOC-4(기상) · DOC-5(작년동기).

    세 건이 다 나오려면 우선순위 3종을 **전부** 돌아야 한다. "1건 찾으면 충분"으로
    조기 종료하면 DOC-4·5가 조용히 사라진다 — 규칙은 충분성을 판정할 수 없으므로
    판정한 척하지 않는다 (``is_enough``).
    """
    assert proposals[UNCERTAIN]["context_docs_used"] == ["DOC-3", "DOC-4", "DOC-5"]


def test_stable_days_never_enter_the_node(proposals: dict) -> None:
    """stable한 날은 ②를 통째로 건너뛴다 (§4-②). 빈 목록이 그 사실을 그대로 보여준다."""
    for as_of in (RISING, FALLING, SPREAD_WIDE):
        assert proposals[as_of]["context_docs_used"] == []


def test_loop_count_records_every_attempt() -> None:
    """빈 회차도 한 번의 시도로 센다 — "찾아봤지만 없었다"가 루프 수에 남아야 한다.

    ``context_loop_count``가 안 오르면 ``loop_max``가 아무것도 막지 못하고, 그 사실을
    출력만 봐서는 알 수 없다.
    """
    for item in ITEMS:
        result = collect_context(_classified(item))
        assert result["context_loop_count"] == 3


# ── look-ahead 차단 ─────────────────────────────────────────────────────────


def test_future_document_is_invisible_on_the_uncertain_day(proposals: dict) -> None:
    """DOC-6은 배추 관측월보인데 **9/5 발행**이라 9/4에는 보이면 안 된다.

    필터가 없으면 같은 유형·같은 품목이라 DOC-3과 함께 딸려 나온다. 코퍼스에 "보이면 안 되는
    문서"를 넣어둔 이유가 이 한 줄이다 (``documents.json._published_at``).
    """
    assert "DOC-6" not in proposals[UNCERTAIN]["context_docs_used"]
    # 9/11이면 보이는 문서다 — 안 보이는 게 as_of 때문이지 코퍼스 누락이 아님을 확인한다
    later = {doc["doc_id"] for doc in ports.get_context_docs(ITEM, SPREAD_WIDE, ["관측월보"])}
    assert 6 in later


def test_node_does_not_reimplement_the_published_at_filter(monkeypatch) -> None:
    """②는 포트가 돌려준 것을 **그대로** 담는다 — 필터를 두 곳에 두면 한쪽만 바뀐다.

    포트가 미래 문서를 흘리면 ②도 함께 흘려야 정상이다. 여기서 ②가 막아버리면
    "필터가 ②에도 있다"는 뜻이고, 진짜 필터가 고장 나도 아무도 모른다.
    """
    future = {
        "doc_id": 99,
        "source": "KREI",
        "doc_type": "관측월보",
        "item": ITEM,
        "title": "미래 문서",
        "published_at": "2099-01-01",
        "content": "이 문서는 포트가 걸렀어야 한다.",
    }
    monkeypatch.setattr(ports, "get_context_docs", lambda *_args, **_kwargs: [future])
    collected = collect_context(_classified())["context_docs"]
    assert [doc["doc_id"] for doc in collected] == [99]


# ── 실패 모드 (a) 루프가 안 끝남 / (c) 빈 목록 호출 ────────────────────────


def test_loop_stops_at_loop_max_even_with_a_longer_priority_list(monkeypatch) -> None:
    """상한이 실제로 무는가 — **목록을 4종으로 늘려도 3회에서 멈춘다**.

    mock에서는 목록 길이와 ``loop_max``가 둘 다 3이라 구분되지 않는다. 목록만 늘려
    "소진해서 멈춘 게 아니다"를 갈라낸다.
    """
    calls: list[list[str]] = []
    original = ports.get_context_docs

    def counted(item, as_of, doc_types):
        calls.append(doc_types)
        return original(item, as_of, doc_types) if doc_types != ["없는유형"] else []

    monkeypatch.setattr(ports, "get_context_docs", counted)
    monkeypatch.setattr(
        "app.purchase_agent.nodes.collect_context.select_doc_types",
        lambda _c: ["관측월보", "기상", "작년동기", "없는유형"],
    )
    result = collect_context(_classified())
    assert len(calls) == 3
    assert ["없는유형"] not in calls
    assert result["context_loop_count"] == 3


def test_loop_stops_when_the_list_runs_out_before_loop_max(monkeypatch) -> None:
    """반대 방향 — 목록이 짧으면 ``loop_max``보다 **먼저** 끝난다.

    이게 없으면 위 테스트가 "항상 3회 돈다"만 잠그게 되고, 소진 탈출이 지워져도 통과한다.
    """
    monkeypatch.setattr(
        "app.purchase_agent.nodes.collect_context.select_doc_types", lambda _c: ["관측월보"]
    )
    result = collect_context(_classified())
    assert result["context_loop_count"] == 1
    assert [doc["doc_id"] for doc in result["context_docs"]] == [3]


def test_loop_max_is_a_cumulative_budget_across_re_entry() -> None:
    """``loop_max``는 **누적 상한**이다 — 재진입해도 ``context_loop_count``가 3을 못 넘는다.

    §3이 이 필드를 "max 3"으로 규정한다. 지금 그래프엔 ②로 돌아오는 간선이 없어 도달 불가한
    경로지만(Codex도 "현 배선에서는 도달 불가"라 봤다), 필드가 약속한 불변을 **배선이**
    지키게 두면 간선 하나 추가에 조용히 깨진다. 코드가 지키게 한다.
    """
    state = _classified()
    state["context_loop_count"] = 2
    assert collect_context(state)["context_loop_count"] == 3

    state["context_loop_count"] = 3  # 이미 소진 — 한 번도 더 돌지 않는다
    exhausted = collect_context(state)
    assert exhausted["context_loop_count"] == 3
    assert exhausted["context_docs"] == []


def test_empty_priority_list_never_calls_the_port(monkeypatch) -> None:
    """``doc_types=[]``는 포트가 ``ValueError``다 — 루프 진입 전에 막아야 한다."""
    monkeypatch.setattr(
        "app.purchase_agent.nodes.collect_context.select_doc_types", lambda _c: []
    )

    def explode(*_args, **_kwargs):
        raise AssertionError("빈 목록으로 포트를 부르면 안 된다")

    monkeypatch.setattr(ports, "get_context_docs", explode)
    assert collect_context(_classified()) == {"context_docs": [], "context_loop_count": 0}


def test_port_rejects_an_empty_doc_type_list() -> None:
    """위 방어가 필요한 이유를 포트 쪽에서 못 박는다 — 실제로 터진다."""
    with pytest.raises(ValueError, match="must not be empty"):
        ports.get_context_docs(ITEM, UNCERTAIN, [])


# ── 실패 모드 (b) 문서 중복 ────────────────────────────────────────────────


def test_repeated_doc_type_does_not_duplicate_documents(monkeypatch) -> None:
    """같은 유형이 목록에 두 번 있어도 문서는 한 번만 담긴다.

    중복이 들어가면 ``context_docs_used``에 같은 DOC이 두 번 실리고 rationale도 두 벌이 된다 —
    Critic 입장에서는 근거가 두 배로 부풀어 보인다.
    """
    monkeypatch.setattr(
        "app.purchase_agent.nodes.collect_context.select_doc_types",
        lambda _c: ["관측월보", "관측월보", "기상"],
    )
    collected = collect_context(_classified())["context_docs"]
    assert [doc["doc_id"] for doc in collected] == [3, 4]


def test_multiple_documents_of_one_type_are_all_kept() -> None:
    """유형 소비만으로는 부족한 반례 — 한 유형에 문서가 여럿인 날이 있다.

    9/11 배추 관측월보 = DOC-3 · DOC-6. dedupe를 ``doc_type``으로만 하면 둘 중 하나가
    사라진다. 그래서 ``doc_id``로 거른다.
    """
    docs = ports.get_context_docs(ITEM, SPREAD_WIDE, ["관측월보"])
    assert sorted(doc["doc_id"] for doc in docs) == [3, 6]


# ── 발췌 (인용 요건) ───────────────────────────────────────────────────────


def test_excerpt_is_the_verbatim_first_sentence() -> None:
    """발췌는 **원문 그대로**여야 한다 — Critic이 대조하는 값이라 훼손되면 대조가 거짓이 된다."""
    collected = collect_context(_classified())["context_docs"]
    for doc in collected:
        assert doc["excerpt"] in doc["content"]
        assert doc["content"].startswith(doc["excerpt"])


def test_excerpt_falls_back_to_a_length_cap_without_a_sentence_end() -> None:
    """종결("…다.")이 없으면 본문 전체가 인용으로 둔갑한다 — 상한이 그걸 막는다.

    **잘린 발췌에는 절단 표시가 붙는다** (현서님 2차 피드백). 표시가 없으면 "상승하지
    않았다"가 "상승"으로 잘렸을 때 **부정이 사라진 완결된 주장**으로 읽힌다.
    """
    assert leading_excerpt("종결 없는 긴 본문" * 50, 10) == ("종결 없는 긴 본문…", True)
    assert leading_excerpt("첫 문장이다. 둘째 문장이다.", 999) == ("첫 문장이다.", False)


def test_untruncated_content_gets_no_truncation_mark() -> None:
    """상한에 **안 걸린** 본문에는 표시를 붙이지 않는다.

    종결이 없다는 것과 잘렸다는 것은 다른 사실이다. 짧은 본문 전문이 발췌인데 표시를
    붙이면 **잘리지 않은 것을 잘렸다고 말하는 것**이라 표시 자체가 신뢰를 잃는다.
    """
    assert leading_excerpt("종결 없는 짧은 본문", 999) == ("종결 없는 짧은 본문", False)


def test_truncation_mark_does_not_rescue_empty_content() -> None:
    """빈 검사는 **표시를 붙이기 전에** 한다.

    붙인 뒤에 보면 공백뿐인 본문도 표시 한 글자 때문에 non-empty가 되어, 빈 발췌를
    거부하는 방어(Codex P1 회귀)가 통째로 무력해진다.
    """
    with pytest.raises(ValueError, match="excerpt"):
        leading_excerpt(" " * 500, 10)


def test_truncated_excerpt_reads_as_incomplete() -> None:
    """현서님 반례 그대로 — 부정이 잘려나가도 **완결 주장으로 읽히지 않는다.**"""
    # 종결("다.")이 없어 상한 경로로 간다. 상한이 "상승" 직후에 떨어지도록 잡았다 —
    # 표시가 없으면 "배추 가격은 상승"이 되어 **원문과 정반대**로 읽히는 자리다.
    excerpt, truncated = leading_excerpt("배추 가격은 상승하지 않았음", 9)
    assert truncated
    assert excerpt == "배추 가격은 상승…"
    assert excerpt.endswith(TRUNCATION_MARK)
    assert not excerpt.removesuffix(TRUNCATION_MARK).endswith("상승…")


def test_empty_content_is_refused_instead_of_quoting_nothing() -> None:
    """**Codex 교차검증 P1 회귀 테스트.**

    빈 발췌를 그냥 실으면 ``evidence_detail``이 ``'발췌: ""'``가 되어 스키마의 non-empty
    검사는 통과하는데 **인용은 아무것도 없다.** "근거를 동봉했다"가 거짓이 되고 Critic은
    대조할 게 없다. ``published_at`` 없는 문서를 로더가 적재 거부하는 것과 같은 자리라
    조용히 넘기지 않고 터뜨린다.
    """
    for content in ("", "   ", "\n"):
        with pytest.raises(ValueError, match="excerpt"):
            leading_excerpt(content, 120)


def test_excerpt_never_alters_the_source_text() -> None:
    """서두 잘라내기라 **원문 문자는 변조되지 않는다** — 문장 경계를 틀려도 대조는 성립한다.

    ``그는 '끝이다.'라고 말했다``처럼 종결 어미가 문장 중간에 나오면 거기서 잘린다.
    문장 경계 파서가 아니라는 뜻이고, 그래서 risks 문구도 "첫 문장"이라 주장하지 않는다.
    """
    tricky = "그는 '끝이다.'라고 말했지만 설명은 이어졌다."
    excerpt, truncated = leading_excerpt(tricky, 120)
    assert excerpt == "그는 '끝이다."
    assert not truncated  # 종결에서 잘렸지 글자 수에 걸린 게 아니다
    assert tricky.startswith(excerpt)  # 잘렸을 뿐 고쳐지지 않았다


def test_document_rationale_carries_ref_id_and_excerpt(proposals: dict) -> None:
    """현서님 합의 8/25: 문서를 근거로 쓰면 **ref_id + 해당 구절**을 출력에 동봉한다.

    Critic은 DB 조회가 금지라(계약서 §0) 발췌 없이는 근거 대조 자체가 성립하지 않는다.
    """
    loaded = {doc["doc_id"]: doc for doc in collect_context(_classified())["context_docs"]}
    scenarios = proposals[UNCERTAIN]["scenarios"]
    assert len(scenarios) == 2  # uncertain은 보수·기본 2안 (공격 금지)
    for scenario in scenarios:  # **양쪽 안 모두**에 붙는다 — 문서 근거는 안 공통이다
        items = [r for r in scenario["rationale"] if r["source"] == "문서ID"]
        assert [r["ref_id"] for r in items] == ["DOC-3", "DOC-4", "DOC-5"]
        for item in items:
            doc = loaded[int(item["ref_id"].removeprefix("DOC-"))]
            assert doc["excerpt"] in item["evidence_detail"]
            assert doc["published_at"] in item["evidence_detail"]
            # mock은 형식만 빌린 가상 문서다 — 실제 KREI 발간물이 아니므로 OFFICIAL이 아니다
            assert item["evidence_grade"] == "SIM_FIXED"


def test_stable_day_gets_no_document_rationale(proposals: dict) -> None:
    """다른 3앵커 불변 — ②가 안 돌았으니 문서 근거도 없어야 한다."""
    for as_of in (RISING, FALLING, SPREAD_WIDE):
        for scenario in proposals[as_of]["scenarios"]:
            assert not [r for r in scenario["rationale"] if r["source"] == "문서ID"]
            assert not [risk for risk in scenario["risks"] if "문서" in risk]


# ── risks 고지 ─────────────────────────────────────────────────────────────


def test_uncertain_day_discloses_that_no_sufficiency_judgment_was_made(proposals: dict) -> None:
    """"이만하면 충분한가"를 아무도 묻지 않았다는 사실이 보여야 한다.

    고지가 없으면 소비자는 "검토를 거친 근거"로 읽는다 — E3-3의 일괄 fallback 고지와 같은
    라벨/행동 불일치다. 문구에 내부 단계 이름을 쓰지 않는다(H1 화면·Critic이 읽는다).
    """
    for scenario in proposals[UNCERTAIN]["scenarios"]:
        notes = [risk for risk in scenario["risks"] if "충분성" in risk]
        assert len(notes) == 1
        assert "rule_only" not in notes[0]


def test_no_documents_found_is_a_different_fact_from_never_looking() -> None:
    """세 상태가 구분돼야 한다 — 안 찾아봄 / 찾았는데 없음 / 찾아서 있음.

    판정 기준이 ``situation``이 아니라 **루프 수**인 이유가 여기다 (Codex 교차검증 지적).
    "그날이 uncertain인가"가 아니라 "문서를 실제로 찾아봤는가"를 직접 묻는다 — situation으로
    물으면 ②의 실행 여부를 그래프 배선을 통해 간접 추론하게 된다.
    """
    assert _context_risks(0, []) == []  # ② 미실행
    assert "0건" in _context_risks(3, [])[0]  # 찾았는데 없음
    assert "3건 참조" in _context_risks(3, [{"doc_id": 3}] * 3)[0]  # 찾아서 있음


def test_risk_note_does_not_claim_more_than_the_rule_does() -> None:
    """출력 문구가 하지 않은 일을 한 것처럼 적으면 안 된다.

    발췌는 문장 경계 파서가 아니라 서두 잘라내기다 — "첫 문장"이라 쓰면 소비자가 발췌 범위를
    잘못 믿는다. 내부 단계 이름도 쓰지 않는다 (H1 화면·Critic이 읽는다).
    """
    note = _context_risks(3, [{"doc_id": 3}])[0]
    assert "첫 문장" not in note
    assert "서두" in note
    assert "rule_only" not in note


# ── 실패 모드 (h) 환각 / (i) 읽고 안 씀 ────────────────────────────────────


def test_citing_an_unloaded_document_is_cut() -> None:
    """읽지 않은 ``DOC-``을 인용하면 그 안이 컷된다 — 지어낸 문서를 막는 유일한 검사다."""
    scenario = {
        "rationale": [
            {"source": "문서ID", "ref_id": "DOC-99"},
            {"source": "예측", "ref_id": "FC-mock-v0-2026-09-04"},
        ]
    }
    docs = [{"doc_id": 3}]
    assert "DOC-99" in check_document_refs(scenario, docs)
    # 문서가 아닌 근거는 그대로 통과한다 — 이 검사가 다른 축을 건드리면 안 된다
    assert check_document_refs({"rationale": scenario["rationale"][1:]}, docs) is None


def _cited(ref_id: str, excerpt: str) -> dict:
    """문서 근거 한 줄. ⑥ ``_context_rationale``이 만드는 형태와 같다."""
    return {
        "source": "문서ID",
        "ref_id": ref_id,
        "evidence_detail": f'2026-08-05 발행 · 발췌: "{excerpt}"',
    }


def _doc(**kwargs) -> dict:
    base = {
        "doc_id": 3,
        "content": "고랭지 배추 정식면적은 전년 대비 6% 감소했다. 이어지는 문장이다.",
        "excerpt": "고랭지 배추 정식면적은 전년 대비 6% 감소했다.",
        "excerpt_truncated": False,
        "published_at": "2026-08-05",
    }
    return {**base, **kwargs}


# ── 신설 불변식 ① 발췌가 원문의 literal substring인가 ──────────────────────


def test_excerpt_absent_from_the_source_is_cut() -> None:
    """원문에 없는 문자열을 발췌라고 실으면 컷된다 — **환각 인용**의 정면 방어다.

    오늘은 ②가 서두를 잘라내기만 해서 이 상태가 나올 수 없다. ②·⑥에 LLM이 붙어
    "구절 선별"이 생기는 순간(E3-5 이후) 열리는 구멍을 미리 막는 것이고, 검사를 나중에
    만들면 그 사이 산출물은 검증된 적이 없는 채로 남는다.
    """
    fabricated = "정식면적이 전년 대비 30% 증가했다."
    scenario = {"rationale": [_cited("DOC-3", fabricated)]}
    docs = [_doc(excerpt=fabricated)]
    assert "원문에 없다" in check_excerpt_fidelity(scenario, docs)


def test_rationale_altering_the_excerpt_is_cut() -> None:
    """⑥이 옮기며 문구를 고치면 컷된다 — ②의 발췌와 출력의 인용이 같아야 한다.

    ②가 원문에서 제대로 떴어도 ⑥이 다듬으면 Critic이 대조하는 값은 원문이 아니게 된다.
    두 지점을 따로 보는 이유다.
    """
    doc = _doc()
    scenario = {"rationale": [_cited("DOC-3", "정식면적이 감소했다")]}  # 요약해 실음
    assert "로드한 발췌와 다르다" in check_excerpt_fidelity(scenario, [doc])
    # 그대로 실으면 통과한다
    assert check_excerpt_fidelity({"rationale": [_cited("DOC-3", doc["excerpt"])]}, [doc]) is None


def test_truncation_mark_is_stripped_before_matching() -> None:
    """절단 표시는 ②가 붙인 표식이지 원문 문자가 아니다 — 떼고 맞춘다.

    떼지 않으면 **잘린 발췌가 전부 컷된다.** 표시를 도입하면서 이 검사가 같이 안 오면
    9/4처럼 문서를 인용하는 날의 안이 통째로 사라진다.
    """
    doc = _doc(excerpt="고랭지 배추 정식면적은…", excerpt_truncated=True)
    scenario = {"rationale": [_cited("DOC-3", doc["excerpt"])]}
    assert check_excerpt_fidelity(scenario, [doc]) is None


def test_fidelity_ignores_non_document_rationale() -> None:
    """문서가 아닌 근거는 건드리지 않는다 — 검사가 다른 축을 침범하면 안 된다."""
    scenario = {"rationale": [{"source": "예측", "ref_id": "FC-mock-v0", "evidence_detail": "x"}]}
    assert check_excerpt_fidelity(scenario, []) is None


# ── 신설 불변식 ② ref_id 실재 + published_at <= as_of ─────────────────────


def test_citing_a_document_published_after_as_of_is_cut() -> None:
    """as_of 이후 발행 문서를 인용하면 컷된다 (규칙 1 look-ahead).

    포트가 이미 거르지만, **필터를 통과하는 것과 출력이 지키는 것은 다른 약속**이다.
    9/4에 9/5 발행 DOC-6을 인용하면 백테스트 성적이 무효가 되는데 그건 버그다.
    """
    scenario = {"rationale": [_cited("DOC-6", "x")]}
    docs = [_doc(doc_id=6, published_at="2026-09-05")]
    reason = check_document_publication(scenario, docs, "2026-09-04")
    assert "look-ahead" in reason and "DOC-6" in reason
    # 같은 문서라도 발행일 이후 시점이면 통과한다
    assert check_document_publication(scenario, docs, "2026-09-05") is None


def test_citing_a_document_without_a_publication_date_is_cut() -> None:
    """발행일이 없으면 비교 자체가 불가능하다 — 조용히 넘기면 "검사했다"가 거짓이 된다."""
    scenario = {"rationale": [_cited("DOC-3", "x")]}
    for missing in (None, ""):
        reason = check_document_publication(scenario, [_doc(published_at=missing)], "2026-09-04")
        assert "발행일 없는" in reason


def test_document_checks_do_not_lean_on_chain_order() -> None:
    """앞 검사가 통과했다고 가정하지 않는다 — 체인 순서가 바뀌어도 조용히 안 뚫린다."""
    scenario = {"rationale": [_cited("DOC-99", "x")]}
    assert "찾을 수 없어" in check_document_publication(scenario, [_doc()], "2026-09-04")
    assert "찾을 수 없어" in check_excerpt_fidelity(scenario, [_doc()])


def test_disguising_a_document_ref_as_another_source_is_cut() -> None:
    """**Codex 교차검증 P1 회귀 테스트.**

    처음엔 ``source == "문서ID"``인 항목만 골랐다. 그래서 출처를 "예측"으로 적고 ``ref_id``에
    ``"DOC-999"``를 넣으면 환각 대조를 통째로 빠져나갔다 — 스키마도 둘의 정합을 요구하지
    않아 최종 출력까지 갔다. 재현해 확인한 뒤 **둘 중 하나라도 문서를 가리키면 걸리게** 했다.
    """
    disguised = {
        "rationale": [
            {"source": "예측", "claim": "위장", "ref_id": "DOC-999"},
        ]
    }
    reason = check_document_refs(disguised, [{"doc_id": 3}])
    assert reason is not None
    assert "DOC-999" in reason

    # 반대 방향도 사각지대였다 — source는 문서인데 ref_id가 문서 표기가 아닌 경우
    mislabeled = {"rationale": [{"source": "문서ID", "claim": "위장", "ref_id": "FC-mock-v0"}]}
    assert "표기 불일치" in check_document_refs(mislabeled, [{"doc_id": 3}])


def test_loading_a_document_without_citing_it_is_not_a_violation() -> None:
    """"읽었는데 근거에 안 썼다"는 **컷하지 않는다.**

    ② 스텁 시절부터 그 상태를 의도적으로 구분 가능하게 두었다. 컷하면
    ``context_docs_used``와 rationale이 항상 같아져 두 필드 중 하나가 무의미해진다.
    """
    assert check_document_refs({"rationale": []}, [{"doc_id": 3}, {"doc_id": 4}]) is None


def test_hallucinated_document_reaches_rejected_reasons() -> None:
    """검사가 그래프 안에서 실제로 컷하는가 — 함수 단위 통과와 배선은 별개다."""
    state = _classified()
    state.update(collect_context(state))
    state.update(draft_plan(state))
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    state["scenarios_final"][0]["rationale"].append(
        {
            "source": "문서ID",
            "claim": "읽은 적 없는 문서",
            "ref_id": "DOC-99",
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": "지어낸 근거",
        }
    )
    result = self_check(state)
    assert any("DOC-99" in item["reason"] for item in result["rejected_reasons"])


def _built_state() -> dict:
    """⑦ 직전까지 그래프를 돌린 state. 체인 레벨 검사에 쓴다."""
    state = _classified()
    state.update(collect_context(state))
    state.update(draft_plan(state))
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    return state


def test_altered_excerpt_reaches_rejected_reasons() -> None:
    """발췌 대조가 **그래프 안에서** 컷하는가 — 함수 단위 통과와 배선은 별개다.

    이 테스트가 없으면 ``check_excerpt_fidelity``를 검사 체인에서 통째로 빼도 단위
    테스트가 전부 초록불이다. 신설 검사를 배선하는 커밋에는 배선을 잠그는 검사가 같이
    와야 한다.
    """
    state = _built_state()
    cited = next(
        item
        for item in state["scenarios_final"][0]["rationale"]
        if item["source"] == "문서ID"
    )
    cited["evidence_detail"] = '2026-08-05 발행 · 발췌: "정식면적이 크게 늘었다."'
    result = self_check(state)
    assert any("로드한 발췌와 다르다" in item["reason"] for item in result["rejected_reasons"])


def test_look_ahead_document_reaches_rejected_reasons() -> None:
    """발행일 대조도 그래프 안에서 컷한다 — 포트 필터와 **별개의 약속**이다."""
    state = _built_state()
    # 로드된 문서의 발행일을 as_of 이후로 조작한다. 포트는 이미 통과한 뒤라
    # 출력 경계 검사만이 이걸 잡을 수 있다.
    state["context_docs"][0]["published_at"] = "2026-12-31"
    result = self_check(state)
    assert any("look-ahead" in item["reason"] for item in result["rejected_reasons"])


# ── 포트 호출 위치 잠금 (실패 모드 e·f) ────────────────────────────────────


def test_documents_are_loaded_at_runtime_only_on_uncertain_days(monkeypatch) -> None:
    """호출 **위치**의 런타임 쪽을 잠근다 — T0 잠금(``test_contracts``)의 짝이다.

    ①~⑤는 T0 only이고 ⑥ 문서만 ② 노드의 런타임 예외다 (정의서 §3.1.1 · IO명세 §0).
    T0 쪽은 "``build_initial_state``가 문서를 안 부른다"를 잠그지만, **stable한 날 ②가
    돌아버리는 것**은 아무도 안 보고 있었다. 문서를 불필요하게 당겨오면 예외의 안전 근거
    ("uncertain일 때만")가 사라진다.
    """
    calls: dict[date, int] = {}
    original = ports.get_context_docs

    for as_of in ANCHORS:

        def wrapper(item, when, doc_types, *, _as_of=as_of):
            calls[_as_of] = calls.get(_as_of, 0) + 1
            return original(item, when, doc_types)

        monkeypatch.setattr(ports, "get_context_docs", wrapper)
        build_graph().invoke(build_initial_state(ITEM, as_of))

    assert calls.get(UNCERTAIN, 0) == 3
    for as_of in (RISING, FALLING, SPREAD_WIDE):
        assert calls.get(as_of, 0) == 0


# ── 4품목 전횡단 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("item", ITEMS)
@pytest.mark.parametrize("as_of", ANCHORS)
def test_every_item_and_anchor_survives_the_document_loop(item: str, as_of: date) -> None:
    """전횡단. 무·양파·피마늘은 기상·작년동기 문서가 없어 **빈 회차**를 지난다.

    빈 회차가 크래시를 내거나 ``None``을 담으면 여기서 드러난다 — E3-1에서 배추만 돌려
    양파·피마늘 크래시를 놓친 자리와 같다.
    """
    proposal = run_purchase_agent(item, as_of)
    cited = {
        r["ref_id"]
        for scenario in proposal["scenarios"]
        for r in scenario["rationale"]
        if r["source"] == "문서ID"
    }
    assert cited <= set(proposal["context_docs_used"])
    if as_of == UNCERTAIN:
        assert proposal["context_docs_used"]  # uncertain엔 최소 관측월보 1건이 있다
    else:
        assert proposal["context_docs_used"] == []


# ── 순수 함수 / 계약 ───────────────────────────────────────────────────────


def test_priority_list_comes_from_constraints() -> None:
    """우선순위는 constraints가 소유한다 (규칙 7) — 코드에 박으면 두 곳이 된다."""
    constraints = load_constraints()
    assert select_doc_types(constraints) == constraints["context"]["doc_type_priority"]
    # 반환은 **복사본**이어야 한다. 루프가 pop으로 소비하므로 원본을 돌려주면
    # load_constraints가 캐시를 도입하는 순간 두 번째 실행이 빈 목록으로 시작한다.
    select_doc_types(constraints).pop()
    assert len(select_doc_types(constraints)) == len(constraints["context"]["doc_type_priority"])


def test_rule_stage_never_declares_the_context_sufficient() -> None:
    """``is_enough``는 LLM 자리다 — 규칙 단계에서 참을 돌려주면 조기 종료가 생긴다."""
    assert is_enough([]) is False
    assert is_enough([{"doc_id": 3}, {"doc_id": 4}, {"doc_id": 5}]) is False


def test_document_rationale_is_empty_without_documents() -> None:
    """안 읽었으면 아무것도 안 붙는다 — 없는 근거를 적지 않는다."""
    assert _context_rationale([]) == []
