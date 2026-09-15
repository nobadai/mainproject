"""마스터 포트 검사 — **마스터 없이, DB 없이 잰다.**

★ 마스터 어휘에 아직 `"ml"` 이 없다 (`AgentName` 은 닫힌 목록이고 그 파일은 우리
  것이 아니다). 그래서 **저쪽이 할 수정 두 줄을 여기서 흉내낸다.**

```text
envelope._AGENT_MODES["ml"] = frozenset({"STATUS_QUERY"})
```

🔴 이 흉내가 **검사의 전제 그 자체**다. 저쪽이 다른 모드를 열거나 이름을 다르게
   잡으면 여기가 먼저 깨져야 한다 — 조용히 통과하면 «붙었다고 믿는데 안 붙은»
   상태가 된다.

검사의 핵심은 하나다 — **못 한 것이 한 것처럼 보이지 않는가.**
창고를 못 읽었는데 `READY` 로 답하거나, 예측을 안 읽고 `observed_at` 을 채우면
마스터 이력에 «쟀다» 로 남는다.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.master import envelope as E
from app.ml import adapter
from app.ml.qa_schemas import QaAnswer, QaMeta

BASE = date(2026, 9, 15)


@pytest.fixture(autouse=True)
def 마스터가_우리_이름을_안다(monkeypatch: pytest.MonkeyPatch):
    """저쪽이 넣을 `_AGENT_MODES` 한 줄을 흉내낸다. 원본은 건드리지 않는다."""
    modes = dict(E._AGENT_MODES)
    modes["ml"] = frozenset({"STATUS_QUERY"})
    monkeypatch.setattr(E, "_AGENT_MODES", modes)


def req(*, mode: str = "STATUS_QUERY", payload: dict | None = None) -> E.AgentRequest:
    return E.AgentRequest(
        context=E.ExecutionContext(
            request_id="req-1",
            as_of=BASE,
            trigger="USER_REQUEST",
            policy_version="v1",
        ),
        agent="ml",                                          # type: ignore[arg-type]
        mode=mode,                                           # type: ignore[arg-type]
        payload=payload or {},
    )


def _row(offset: int) -> dict:
    return {
        "target_dt": BASE + timedelta(days=offset),
        "predicted": 962,
        "lower": 712,
        "upper": 1348,
        "unit": "원/kg",
        "is_filled": False,
    }


def _answer(status: str = "OK", *, rows: list[dict] | None = None) -> QaAnswer:
    """Q&A 결과를 흉내낸다. 어댑터가 **그것을 어떻게 봉투에 담나**만 잰다."""
    return QaAnswer(
        markdown="**배추 · 경락가**\n\n| 날짜 | 예측 |\n|---|---|\n| 09-16 | 962원 |",
        meta=QaMeta(
            status=status,                                   # type: ignore[arg-type]
            item="배추",
            kind="AUC",
            base_dt=BASE,
            targets=[BASE + timedelta(days=1)],
            model_version="ops_auc",
            source="ml_price_forecasts",
            is_filled=[False],
            use_recommended=True,
        ),
        rows_for_evidence=rows if rows is not None else [_row(1)],
    )


def _qa(monkeypatch, out):
    """질의응답을 갈아 끼운다. 예외를 내고 싶으면 `out` 에 예외를 준다."""

    def fake(_request):
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(adapter, "qa_answer", fake)


# ── 질문이 왔을 때 ──────────────────────────────────────────────────────


def test_질문을_받으면_마크다운과_기계용_값을_같이_싣는다(monkeypatch):
    """★ 둘 다 싣는 이유 — 마스터가 마크다운을 그대로 못 써도 답이 성립해야 한다."""
    _qa(monkeypatch, _answer())
    reply, meta = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))

    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert "962원" in reply.payload["answer_markdown"]
    assert reply.payload["item"] == "배추"
    assert reply.payload["forecasts"][0]["target_dt"] == "2026-09-16"
    assert reply.payload["forecasts"][0]["kind"] == "AUC"
    assert meta.run_id == reply.run_id                       # E-BIND-RUN-ID


def test_봉투_검증이_비어야_한다(monkeypatch):
    """🔴 **이 검사가 이 파일의 핵심이다.**

    마스터는 회신을 받고 `validate_reply()` 를 돌린다. 지적이 나와도 예외는 아니지만
    **이력에 «근거 없는 값을 실은 부서» 로 남는다.**

    2026-09-15 실측에서 네 건이 났었다 — `qa_status` · `target_kind` ·
    `target_dates` 에 근거가 없고, 근거의 `claim` 을 사람 말로 적어 **고아**가 됐다.
    payload 모양을 봉투 규칙에 맞춰 고쳤고, 그 결과를 여기에 못 박는다.
    """
    _qa(monkeypatch, _answer())
    request = req(payload={"question": "내일 배추 경락가?"})
    reply, meta = adapter.ml_port(request)
    assert E.validate_reply(request, reply, meta) == ()


def test_근거를_못_다는_라벨을_최상위에_안_싣는다(monkeypatch):
    """★ 억지 근거를 만드느니 자리를 옮긴다.

    대문자 라벨(`"OK"` · `"AUC"`)은 최상위에 있으면 근거가 필요한데, 그 값에는
    댈 수치가 없다. 세어 본 것을 근거라고 적는 대신 `forecasts[]` 안으로 넣는다.
    """
    _qa(monkeypatch, _answer())
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    for banned in ("qa_status", "target_kind", "target_dates", "filled_count"):
        assert banned not in reply.payload
    assert reply.payload["answer_status"] == "ok"            # 소문자라 라벨이 아니다


def test_숫자_칸마다_근거가_하나씩_붙는다(monkeypatch):
    """봉투가 배열 항목 **안의 숫자**에 근거를 요구한다 — 행 하나에 셋이다."""
    _qa(monkeypatch, _answer(rows=[_row(1), _row(2)]))
    reply, _ = adapter.ml_port(req(payload={"question": "내일, 모레 배추 경락가?"}))
    claims = {evidence.claim for evidence in reply.evidences}
    assert claims == {
        "forecasts[0].predicted", "forecasts[0].lower", "forecasts[0].upper",
        "forecasts[1].predicted", "forecasts[1].lower", "forecasts[1].upper",
    }


def test_질문은_question_한_이름으로만_받는다(monkeypatch):
    """★ 마스터가 `question` 으로 정했다 (2026-09-15). **나머지는 닫는다.**

    여러 이름을 열어 두면 나중에 어느 것이 정본인지 아무도 못 정하고, 두 이름으로
    다른 값이 오는 날 조용히 한쪽만 읽힌다.
    """
    _qa(monkeypatch, _answer())
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert reply.payload["item"] == "배추"

    #   닫은 이름으로 오면 «질문이 안 온 것» 과 같다 — 조용히 읽지 않는다.
    for closed in ("utterance", "q"):
        _qa(monkeypatch, _answer())
        reply, _ = adapter.ml_port(req(payload={closed: "내일 배추 경락가?"}))
        assert "item" not in reply.payload, closed
        assert reply.payload["forecast_available"] is True, closed


def test_예측을_읽었을_때만_근거와_관측시점을_단다(monkeypatch):
    """🔴 안 읽고 `observed_at` 을 채우면 **안 잰 호출이 잰 호출로** 세어진다."""
    _qa(monkeypatch, _answer())
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert reply.observed_at == BASE
    #   행 하나에 근거 셋 — 봉투가 배열 항목 안의 **숫자마다** 근거를 요구한다.
    assert len(reply.evidences) == 3
    by_claim = {evidence.claim: evidence for evidence in reply.evidences}
    assert by_claim["forecasts[0].predicted"].value == 962.0
    assert by_claim["forecasts[0].lower"].value == 712.0
    assert by_claim["forecasts[0].upper"].value == 1348.0
    #   ref 는 읽은 표·키를 그대로 적는다. 지어낸 주소가 없다.
    assert by_claim["forecasts[0].predicted"].ref_ids[0].startswith(
        "ml_price_forecasts:base_dt=2026-09-15"
    )
    assert by_claim["forecasts[0].predicted"].ref_ids[0].endswith("column=predicted")

    _qa(monkeypatch, _answer(rows=[]))
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert reply.observed_at is None
    assert reply.evidences == ()


def test_근거는_하드제약에_못_쓰는_등급이다(monkeypatch):
    """예측은 관측이 아니다. `HARD_ALLOWED_GRADES` 에 드는 등급을 쓰면 안 된다."""
    from app.contracts.core import HARD_ALLOWED_GRADES

    _qa(monkeypatch, _answer())
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert reply.evidences[0].evidence_grade not in HARD_ALLOWED_GRADES


def test_축_조정을_제안하지_않는다(monkeypatch):
    """조언자가 아니다. 하나라도 담으면 봉투가 ContractViolation 을 낸다."""
    _qa(monkeypatch, _answer())
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert reply.suggested_adjustments == ()


# ── 답을 못 낸 경우 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "missing"),
    [
        ("SOURCE_UNAVAILABLE", "ml_price_forecasts"),
        ("NO_DATA", "ml_price_forecasts"),
        ("LLM_UNAVAILABLE", "ml_question_interpretation"),
    ],
)
def test_못_답한_것은_이름을_밝힌다(monkeypatch, status, missing):
    """`RUNTIME_NOT_READY` 는 **무엇이 없는지** 밝혀야 봉투가 성립한다."""
    _qa(monkeypatch, _answer(status))
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert missing in reply.missing_data


def test_소관이_아닌_것은_고장이_아니다(monkeypatch):
    """★ `OUT_OF_SCOPE` 를 RUNTIME_NOT_READY 로 내면 이력에 «ML 이 못 답했다» 로 남는다."""
    _qa(monkeypatch, _answer("OUT_OF_SCOPE"))
    reply, _ = adapter.ml_port(req(payload={"question": "마늘 얼마야?"}))
    assert reply.runtime_status == "READY"
    assert reply.business_status == "skipped"


def test_일부만_답하면_조건부로_적는다(monkeypatch):
    _qa(monkeypatch, _answer("PARTIAL"))
    reply, _ = adapter.ml_port(req(payload={"question": "내일과 30일 뒤 배추?"}))
    assert reply.business_status == "conditional"


def test_터져도_예외를_위로_안_던진다(monkeypatch):
    """🔴 우리 하나가 터져서 마스터 사이클이 죽으면 안 된다 (ports.py §7.1)."""
    _qa(monkeypatch, RuntimeError("connection refused"))
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert reply.runtime_status == "ERROR"
    assert reply.payload == {}


def test_오류_문구를_밖으로_안_흘린다(monkeypatch):
    """접속 정보가 오류에 실려 나온 적이 있다. 원문을 그대로 올리지 않는다."""
    _qa(monkeypatch, RuntimeError("password=secret host=10.0.0.5"))
    reply, _ = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert "secret" not in reply.reasoning
    assert "10.0.0.5" not in reply.reasoning


# ── 질문이 안 왔을 때 ───────────────────────────────────────────────────


def test_질문이_없으면_되묻지_않고_보유_상태를_답한다(monkeypatch):
    """★ 지금 마스터는 payload 를 안 보낸다 (`status_flow.py:110`).

    여기서 되물으면 **조회할 때마다** «ML 이 답하지 못했다» 가 뜬다.
    """
    monkeypatch.setattr(adapter.qa_tools, "latest_base_date", lambda as_of=None: BASE)
    reply, _ = adapter.ml_port(req())
    assert reply.runtime_status == "READY"
    assert reply.payload["forecast_available"] is True
    assert "배추" in reply.payload["answerable"]
    assert reply.observed_at == BASE


def test_예측이_아직_없으면_없다고_말한다(monkeypatch):
    monkeypatch.setattr(adapter.qa_tools, "latest_base_date", lambda as_of=None: None)
    reply, _ = adapter.ml_port(req())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert "ml_price_forecasts" in reply.missing_data


def test_창고를_못_읽으면_ERROR_다(monkeypatch):
    """`ERROR` 만 재시도 가치가 있다 — 값이 없어서 못 낸 답과 갈라 적는다."""

    def _boom(as_of=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(adapter.qa_tools, "latest_base_date", _boom)
    reply, _ = adapter.ml_port(req())
    assert reply.runtime_status == "ERROR"
    assert reply.worth_retry is True


def test_안_받는_모드는_거절한다():
    """계약에 없는 모드는 받지 않는다. 봉투가 먼저 막지만 우리도 접는다."""
    request = req()
    object.__setattr__(request, "mode", "PRE_PURCHASE")
    reply, _ = adapter.ml_port(request)
    assert reply.runtime_status == "ERROR"


# ── 실행 흔적 ───────────────────────────────────────────────────────────


def test_LLM_을_껐으면_DISABLED_로_적는다(monkeypatch):
    """«안 켰다» 와 «켰는데 이번엔 안 썼다» 를 한 값으로 적으면 없는 문제를 찾는다."""
    monkeypatch.setenv("ML_LLM_ENABLED", "0")
    monkeypatch.setattr(adapter.qa_tools, "latest_base_date", lambda as_of=None: BASE)
    _, meta = adapter.ml_port(req())
    assert meta.llm_status == "DISABLED"
    assert meta.llm_model == ""


def test_쓴_도구와_순서의_길이가_같다(monkeypatch):
    """길이가 다르면 실행 계획을 재현할 수 없다 (봉투가 ContractViolation 을 낸다)."""
    _qa(monkeypatch, _answer())
    _, meta = adapter.ml_port(req(payload={"question": "내일 배추 경락가?"}))
    assert len(meta.used_tools) == len(meta.tool_order)
    assert meta.agent == "ml"


# ── 등록 ────────────────────────────────────────────────────────────────


def test_마스터가_이름을_모르면_조용히_안_붙는다(monkeypatch):
    """🔴 예외를 던지면 저쪽 부팅이 죽는다. 우리 배선 때문에 서버가 안 뜨면 안 된다."""
    from app.ml import wiring

    modes = {k: v for k, v in E._AGENT_MODES.items() if k != "ml"}
    monkeypatch.setattr(E, "_AGENT_MODES", modes)
    assert wiring.master_knows_us() is False
    assert wiring.register_ml_agent() is False


def test_이름을_알면_등록된다():
    """이름만 있으면 바로 붙는다 — 저쪽이 더 할 일은 한 줄뿐이다."""
    from app.master import wiring as master_wiring
    from app.ml import wiring

    assert wiring.register_ml_agent() is True
    assert master_wiring.registry().has("ml")                # type: ignore[arg-type]
