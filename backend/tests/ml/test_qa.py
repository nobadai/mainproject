"""예측 질의응답 — 경로별 검사.

★ **DB 를 안 쓴다.** 도구 넷을 갈아 끼워 그래프의 판단만 잰다. 값이 맞는지는 표가
  답할 일이고, 여기서 잴 것은 *«어떤 상황에 어떤 문구가 나가는가»* 다.

🔴 검사의 핵심은 **못 한 것이 한 것처럼 보이지 않는가** 다.
   창고를 못 읽었을 때 예시값이 나가거나, 해석 못 한 질문에 그럴듯한 답이 나가면
   그 순간 이 기능은 위험물이 된다.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.ml import qa_graph, qa_llm, qa_tools
from app.ml.qa_schemas import QaRequest

BASE = date(2026, 9, 14)


def _row(offset: int, *, filled: bool = False) -> dict:
    return {
        "base_dt": BASE,
        "target_dt": BASE + timedelta(days=offset),
        "offset_days": offset,
        "src_lead_biz_d": offset,
        "predicted": 867,
        "lower": 640,
        "upper": 1213,
        "current_price": 884,
        "unit": "원/kg",
        "is_filled": filled,
        "is_gated": False,
        "gate_reason": None,
        "band_method": "quantile",
        "use_recommended": True,
        "quality_note": None,
        "market_name": "서울가락",
        "grade_name": "특",
        "spec_desc": "그물망·파렛트 10kg",
        "model_version": "ops_auc",
        "generated_at": "2026-09-14 06:00:00+09:00",
    }


@pytest.fixture
def 도구를_갈아_끼운다(monkeypatch: pytest.MonkeyPatch):
    """기본은 «정상» 이다. 각 검사가 필요한 것만 다시 덮어쓴다."""

    def install(*, rows=None, today=None, acc=None, usab=None, base=BASE, boom=False):
        def _raise(*_a, **_k):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            qa_graph.qa_tools, "latest_base_date",
            _raise if boom else (lambda as_of=None: base),
        )
        monkeypatch.setattr(
            qa_graph.qa_tools, "forecast_rows", lambda *a, **k: list(rows or [])
        )
        monkeypatch.setattr(qa_graph.qa_tools, "today_row", lambda *a, **k: today)
        monkeypatch.setattr(qa_graph.qa_tools, "accuracy", lambda *a, **k: acc)
        monkeypatch.setattr(qa_graph.qa_tools, "usability", lambda *a, **k: usab or {})

    return install


def test_범위_밖_품목은_소관이_아니라고_답한다(도구를_갈아_끼운다):
    도구를_갈아_끼운다()
    out = qa_graph.answer(QaRequest(item="마늘", kind="AUC"))
    assert out.meta.status == "OUT_OF_SCOPE"
    assert "마늘" in out.markdown and "배추" in out.markdown


def test_창고를_못_읽으면_예시값_대신_못_읽었다고_답한다(도구를_갈아_끼운다):
    도구를_갈아_끼운다(boom=True)
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    assert out.meta.status == "SOURCE_UNAVAILABLE"
    assert "읽지 못했습니다" in out.markdown


def test_기본은_내일_하루를_답한다(도구를_갈아_끼운다):
    도구를_갈아_끼운다(rows=[_row(1)])
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    assert out.meta.status == "OK"
    assert out.meta.targets == [BASE + timedelta(days=1)]
    assert "867" in out.markdown
    assert "출발점" in out.markdown          # 앵커가 거래가가 아니라는 경고


def test_범위_밖_날짜가_섞이면_나머지를_답하고_PARTIAL_로_적는다(도구를_갈아_끼운다):
    도구를_갈아_끼운다(rows=[_row(1), _row(10, filled=True)])
    out = qa_graph.answer(
        QaRequest(
            item="배추", kind="AUC",
            dates=[BASE + timedelta(days=d) for d in (1, 10, 20)],
        )
    )
    assert out.meta.status == "PARTIAL"
    assert out.meta.out_of_range == [BASE + timedelta(days=20)]
    assert "예측 범위 밖" in out.markdown
    assert "복사값" in out.markdown          # is_filled 는 답에 드러난다


def test_당일을_물으면_원본_창고에서_읽고_출처를_밝힌다(도구를_갈아_끼운다):
    today = {
        "base_dt": BASE, "target_dt": BASE, "lead_biz_d": 0,
        "predicted": 864, "lower": 621, "upper": 1190, "current_price": 884,
        "unit": "원/kg", "is_gated": False, "gate_reason": None,
        "band_method": "quantile", "model_version": "ops_auc",
    }
    도구를_갈아_끼운다(rows=[], today=today)
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC", dates=[BASE]))
    assert out.meta.status == "OK"
    assert out.meta.source == "prediction_log"
    assert "내부 기록" in out.markdown


def test_쓰지_말라는_조합은_경고가_먼저_나간다(도구를_갈아_끼운다):
    도구를_갈아_끼운다(
        rows=[_row(1)],
        usab={"use_recommended": False, "quality_note": "앵커가 거의 완벽",
              "band_method": "fixed_table"},
    )
    out = qa_graph.answer(QaRequest(item="양파", kind="WHSL"))
    assert out.meta.use_recommended is False
    assert "쓰지 마세요" in out.markdown


def test_평균_오차는_화면과_같은_값을_쓴다(도구를_갈아_끼운다):
    """★ prediction_log 를 다시 집계하지 않는다 — 한 사실에 두 숫자가 돌면 안 된다."""
    도구를_갈아_끼운다(rows=[_row(1)], acc=qa_tools.SEALED_ACCURACY[("AUC", "배추")])
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    assert "19.7%" in out.markdown          # 화면 _ACCURACY 와 같은 값
    assert "봉인 개봉" in out.markdown       # 조건 없는 수치는 안 적는다


def test_상수표는_아홉_칸이_다_있다():
    """중도매가·소매가도 답해야 한다. 화면에는 경락가 세 칸만 적혀 있다."""
    assert len(qa_tools.SEALED_ACCURACY) == 9
    assert qa_tools.SEALED_ACCURACY[("RTL", "배추")]["pct"] == "12.7"


def _llm(monkeypatch, answer):
    """LLM 을 갈아 끼운다. `None` 이면 «못 불렀다» 는 뜻이다."""
    monkeypatch.setattr(qa_graph.qa_llm, "interpret", lambda q, base: answer)


def test_질문만_줘도_해석해서_답한다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": "AUC",
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="내일 배추 경락가 얼마야?"))
    assert out.meta.status == "OK"
    assert out.meta.item == "배추" and out.meta.kind == "AUC"
    assert "867" in out.markdown


def test_LLM_이_범위_밖_값을_골라도_gate_가_막는다(도구를_갈아_끼운다, monkeypatch):
    """★ 스키마로 묶어도 «범위 안» 까지 보장되지는 않는다. 그래서 규칙이 다시 본다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "대파", "kind": "AUC", "dates": []})
    out = qa_graph.answer(QaRequest(question="대파 내일 얼마야?"))
    assert out.meta.status == "OUT_OF_SCOPE"


def test_가격_종류를_못_고르면_되묻는다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": None, "dates": []})
    out = qa_graph.answer(QaRequest(question="배추 가격 알려줘"))
    assert out.meta.status == "NEED_CLARIFY"
    assert "경락가" in out.markdown and "소매가" in out.markdown


def test_LLM_을_못_부르면_해석하지_못했다고_답한다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, None)
    out = qa_graph.answer(QaRequest(question="내일 배추 얼마야?"))
    assert out.meta.status == "LLM_UNAVAILABLE"


def test_해석기는_틀린_날짜를_고쳐_쓰지_않고_버린다():
    assert qa_llm._parse_dates(["2026-09-15", "내일", None, "2026-13-40"]) == [
        date(2026, 9, 15)
    ]
