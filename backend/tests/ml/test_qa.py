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
    #   ★ 출발점은 **문장에서 뺐다** (2026-09-15 · 화면을 깨끗이). meta 로 옮겼다 —
    #     없앤 것이 아니다. 실제 거래가가 아니라는 사실은 여전히 전해져야 한다.
    assert out.meta.current_price == 884
    assert "출발점" not in out.markdown


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
    assert "오늘 2026-09-14" in out.markdown
    #   ★ 표 이름·모델 이름 같은 코드 낱말은 **문장에 안 나간다** (마스터 요청).
    #     기계가 읽을 값은 meta 로 간다 — 화면은 사람 말만 본다.
    for code_word in ("prediction_log", "ml_price_forecasts", "ops_auc"):
        assert code_word not in out.markdown
    #   어느 창고에서 읽었는지도 문장에 안 적는다 — 사람에게 알 바가 아니다.
    assert "내부 기록" not in out.markdown


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
    #   ★ 문장에서는 뺐고 meta 로 옮겼다. **값과 조건이 늘 같이 간다** —
    #     조건 없는 수치는 어디에도 안 남긴다 (CLAUDE.md §11).
    assert out.meta.accuracy_pct == "19.7"          # 화면 _ACCURACY 와 같은 값
    assert "2026-09-01" in (out.meta.accuracy_note or "")
    assert "486일치" in (out.meta.accuracy_note or "")
    assert "19.7%" not in out.markdown


def test_설명_줄을_문장에서_빼고_meta_로_옮겼다(도구를_갈아_끼운다):
    """★ 화면은 깨끗하게, 값은 잃지 않게 (2026-09-15 지시).

    🔴 **빼는 것이 아니라 옮기는 것이다.** 출발점·오차·규격이 통째로 사라지면
       19.7% 틀리는 값을 확정값처럼 읽게 된다.
    """
    도구를_갈아_끼운다(rows=[_row(1)], acc=qa_tools.SEALED_ACCURACY[("AUC", "배추")])
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    for gone in ("출발점", "평균 오차", "값의 정체", "486일치"):
        assert gone not in out.markdown, gone
    assert out.meta.current_price == 884
    assert out.meta.accuracy_pct == "19.7"
    assert out.meta.market_name == "서울가락"
    assert out.meta.grade_name == "특"
    assert out.meta.spec_desc == "그물망·파렛트 10kg"


def test_당일_값의_Decimal_출발점을_받아_낸다(도구를_갈아_끼운다):
    """🔴 **자료형이 창고마다 다르다** (2026-09-15 실측).

    전달표 행은 정수인데 원본 창고의 당일 행은 `Decimal('1001.090')` 이다.
    meta 를 `int` 로 좁혀 뒀더니 당일 값을 물을 때마다 500 이 났고, 검사는
    도구를 갈아 끼워 정수만 넣어서 **안 걸렸다.** 진짜 자료형으로 재현해 둔다.
    """
    from decimal import Decimal

    today = {
        "base_dt": BASE, "target_dt": BASE, "lead_biz_d": 0,
        "predicted": Decimal("962.400"), "lower": Decimal("716.0"),
        "upper": Decimal("1342.0"), "current_price": Decimal("1001.090"),
        "unit": "원/kg", "is_gated": False, "gate_reason": None,
        "band_method": "quantile", "model_version": "ops_auc",
    }
    도구를_갈아_끼운다(rows=[], today=today)
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC", dates=[BASE]))
    assert out.meta.status == "OK"
    assert out.meta.current_price == 1001                    # 반올림해 받는다
    assert "962" in out.markdown


def test_당일_값은_고른_기준일로_읽는다_미래를_안_본다(도구를_갈아_끼운다, monkeypatch):
    """🔴 **룩어헤드였다** (2026-09-15 · 화면에서 발견).

    화면(3000)이 2026-07-01 을 걷는데 「오늘 2026-09-15 · 962원」이 나갔다.
    당일 값이 as_of 를 안 받고 **원본 창고 전체 최신**을 읽었기 때문이다.
    에러 없이 두 달 반 뒤의 값이 섞였다.
    """
    화면_기준일 = date(2026, 7, 1)
    받은_기준일: list = []

    def 당일(item, kind, base_dt=None):
        받은_기준일.append(base_dt)
        return {
            "base_dt": base_dt, "target_dt": base_dt, "lead_biz_d": 0,
            "predicted": 389, "lower": 277, "upper": 657, "current_price": 400,
            "unit": "원/kg", "is_gated": False, "gate_reason": None,
            "band_method": "quantile", "model_version": "ops_auc",
        }

    도구를_갈아_끼운다(rows=[], base=화면_기준일)
    monkeypatch.setattr(qa_graph.qa_tools, "today_row", 당일)
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC", as_of=화면_기준일))
    assert 받은_기준일 == [화면_기준일]                      # 전체 최신이 아니라 그날
    assert "오늘 2026-07-01" in out.markdown
    assert "2026-09-15" not in out.markdown


def test_오늘은_화면_기준일로_센다(도구를_갈아_끼운다, monkeypatch):
    """★ 「오늘」·「내일」은 **화면의 기준일**로 센다. 벽시계가 아니다."""
    화면_기준일 = date(2026, 7, 1)
    받은_오늘: list = []

    def 해석(question, today):
        받은_오늘.append(today)
        return {"route": "forecast", "items": ["배추"], "kinds": ["AUC"],
                "item": "배추", "kind": "AUC", "dates": [], "asks": []}

    도구를_갈아_끼운다(rows=[_row(1)], base=화면_기준일)
    monkeypatch.setattr(qa_graph.qa_llm, "interpret", 해석)
    qa_graph.answer(QaRequest(question="오늘 배추 경락가", as_of=화면_기준일))
    assert 받은_오늘 == [화면_기준일]


def test_쓰지_말라는_경고는_문장에_남는다(도구를_갈아_끼운다):
    """🔴 이건 설명이 아니라 **판정**이다. 못 보면 그대로 쓰게 된다."""
    도구를_갈아_끼운다(
        rows=[_row(1)],
        usab={"use_recommended": False, "quality_note": "앵커가 거의 완벽"},
    )
    out = qa_graph.answer(QaRequest(item="양파", kind="WHSL"))
    assert "쓰지 마세요" in out.markdown


def test_상수표는_아홉_칸이_다_있다():
    """중도매가·소매가도 답해야 한다. 화면에는 경락가 세 칸만 적혀 있다."""
    assert len(qa_tools.SEALED_ACCURACY) == 9
    assert qa_tools.SEALED_ACCURACY[("RTL", "배추")]["pct"] == "12.7"


def _llm(monkeypatch, answer):
    """LLM 을 갈아 끼운다. `None` 이면 «못 불렀다» 는 뜻이다.

    ★ 계약이 `items`·`kinds` **배열**로 바뀌었다 (2026-09-15). 검사는 예전처럼
      `item`·`kind` 한 칸으로 적어도 되게, 여기서 배열로 감싸 준다 —
      **읽기 쉬운 검사와 진짜 계약을 한 자리에서 잇는다.**
    """
    if isinstance(answer, dict):
        answer = {
            **answer,
            "items": answer.get("items") or ([answer["item"]] if answer.get("item") else []),
            "kinds": answer.get("kinds") or ([answer["kind"]] if answer.get("kind") else []),
        }
    monkeypatch.setattr(qa_graph.qa_llm, "interpret", lambda q, base: answer)


def test_질문만_줘도_해석해서_답한다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": "AUC",
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="내일 배추 경락가 얼마야?"))
    assert out.meta.status == "OK"
    assert out.meta.item == "배추" and out.meta.kind == "AUC"
    assert "867" in out.markdown


def test_가격_종류가_여럿이면_표를_여러_개_준다(도구를_갈아_끼운다, monkeypatch):
    """★ 「배추 경락가랑 도매가」에 **중도매가만** 나가고 경락가는 조용히 버려졌다.

    한 칸짜리 계약(item·kind)의 한계였다 (2026-09-15 실측).
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "items": ["배추"],
                       "kinds": ["AUC", "WHSL"], "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="배추 경락가랑 도매가 알려줘"))
    assert out.meta.status == "OK"
    assert out.meta.items == ["배추", "배추"]
    assert out.meta.kinds == ["AUC", "WHSL"]
    assert out.markdown.count("| 날짜 | 예측 |") == 2       # 표가 둘
    assert "경락가" in out.markdown and "중도매가" in out.markdown


def test_짝지어_물으면_안_물어본_조합은_안_나온다(도구를_갈아_끼운다, monkeypatch):
    """🔴 「5일 뒤 배추 경락가와 7일 뒤 무 도매가」를 품목 x 가격으로 곱하면
    **배추 중도매가·무 경락가**가 따라 나가고 날짜도 뒤섞인다 (2026-09-15 실측).

    묶음이 오면 그 묶음만 답한다.
    """
    seen: list[tuple] = []

    def 읽은_것을_적는다(item, kind, base_dt, targets):
        seen.append((item, kind, tuple(str(d) for d in targets)))
        return [_row(5)] if targets else []

    도구를_갈아_끼운다(rows=[_row(5)])
    monkeypatch.setattr(qa_graph.qa_tools, "forecast_rows", 읽은_것을_적는다)
    _llm(monkeypatch, {
        "route": "forecast", "items": [], "kinds": [], "dates": [],
        "asks": [
            {"item": "배추", "kind": "AUC", "dates": [BASE + timedelta(days=5)]},
            {"item": "무", "kind": "WHSL", "dates": [BASE + timedelta(days=7)]},
        ],
    })
    out = qa_graph.answer(
        QaRequest(question="5일뒤의 배추 경락가와 7일 뒤의 무 도매가를 알려줘")
    )
    assert out.meta.items == ["배추", "무"]
    assert out.meta.kinds == ["AUC", "WHSL"]
    assert out.markdown.count("| 날짜 | 예측 |") == 2       # 넷이 아니라 둘
    #   ★ 묶음마다 **자기 날짜만** 읽는다 — 날짜가 섞이면 안 물어본 날이 나간다.
    assert seen == [
        ("배추", "AUC", ("2026-09-19",)),
        ("무", "WHSL", ("2026-09-21",)),
    ]


def test_짝_물음이_오면_품목가격_목록은_안_쓴다(도구를_갈아_끼운다, monkeypatch):
    """🔴 **규칙이 막는 자리다** (2026-09-15).

    해석기가 `kinds` 를 넉넉히 고르는 버릇이 있다 — 「배추 도매가」 하나를 물어도
    `WHSL · RTL` 을 내놓는다. 지시문을 두 번 고쳐도 그대로였다.

    그런데 `asks` 는 정확히 갈린다. 그래서 **짝 물음이 오면 그것만 쓴다** —
    말로 부탁해서 안 되는 것은 규칙으로 막는다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {
        "route": "forecast",
        #   해석기가 넉넉히 고른 목록 — 이대로 곱하면 표가 여섯 개가 된다
        "items": ["배추", "무"], "kinds": ["AUC", "WHSL", "RTL"], "dates": [],
        "asks": [{"item": "배추", "kind": "AUC", "dates": [BASE + timedelta(days=1)]}],
    })
    out = qa_graph.answer(QaRequest(question="내일 배추 경락가"))
    assert out.meta.items == ["배추"]
    assert out.meta.kinds == ["AUC"]
    assert out.markdown.count("| 날짜 | 예측 |") == 1       # 여섯이 아니라 하나


def test_품목이_여럿이면_품목마다_표를_준다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "items": ["배추", "무"],
                       "kinds": ["AUC"], "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="배추랑 무 내일 경락가"))
    assert out.meta.items == ["배추", "무"]
    assert out.markdown.count("| 날짜 | 예측 |") == 2
    #   여러 조합이면 무엇을 답했는지 맨 앞에 밝힌다 — 표가 길어 눈에 안 들어온다.
    assert "모두 보여드립니다" in out.markdown


def test_조합마다_근거가_그_조합을_가리킨다(도구를_갈아_끼운다, monkeypatch):
    """🔴 하나로 뭉뚱그리면 배추 경락가 근거가 배추 중도매가를 가리키게 된다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "items": ["배추"],
                       "kinds": ["AUC", "WHSL"], "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="배추 경락가랑 도매가"))
    kinds = {row["kind"] for row in out.rows_for_evidence}
    assert kinds == {"AUC", "WHSL"}                          # 행마다 조합이 붙는다


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


def test_날짜를_안_말하면_오늘_값과_그_이유를_준다(도구를_갈아_끼운다):
    """★ 전에는 말없이 «내일 하루» 였다 (2026-09-15 고침).

    값은 맞지만 왜 하루뿐인지 안 밝히면 «원래 하루치만 있나 보다» 로 읽힌다.
    """
    today = {
        "base_dt": BASE, "target_dt": BASE, "lead_biz_d": 0,
        "predicted": 962, "lower": 700, "upper": 1300, "current_price": 1001,
        "unit": "원/kg", "is_gated": False, "gate_reason": None,
        "band_method": "quantile", "model_version": "ops_auc",
    }
    도구를_갈아_끼운다(rows=[], today=today)
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    assert out.meta.status == "OK"
    assert out.meta.targets == [BASE]                        # 내일이 아니라 오늘
    assert "날짜를 따로 말씀하지 않으셔서" in out.markdown
    assert "전부" in out.markdown                            # 더 볼 수 있다고 알려준다


def test_날짜를_말하면_그_줄이_안_나온다(도구를_갈아_끼운다):
    """물어본 대로 답했으면 설명할 것이 없다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    out = qa_graph.answer(
        QaRequest(item="배추", kind="AUC", dates=[BASE + timedelta(days=1)])
    )
    assert "날짜를 따로 말씀하지" not in out.markdown


def test_오늘_값이_없으면_내일로_물러서고_그렇게_말한다(도구를_갈아_끼운다, monkeypatch):
    """🔴 빈 답을 주느니 물러선다. 다만 **물러섰다고 적는다.**

    ★ 첫 조회는 빈손이어야 한다 — 날짜를 안 말했을 때 읽을 것은 «오늘» 뿐이고
      그건 전달표에 없다. 물러선 **뒤의** 조회에서만 내일 행이 나온다.
    """
    도구를_갈아_끼운다(rows=[], today=None)
    calls: list[list] = []

    def 두_번째부터_행이_나온다(item, kind, base_dt, targets):
        calls.append(list(targets))
        return [_row(1)] if len(calls) > 1 else []

    monkeypatch.setattr(qa_graph.qa_tools, "forecast_rows", 두_번째부터_행이_나온다)
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    assert out.meta.status == "OK"
    assert "오늘 값이 아직 없어" in out.markdown and "내일" in out.markdown
    assert calls[0] == []                                    # 첫 조회는 읽을 날이 없었다
    assert calls[1] == [BASE + timedelta(days=1)]            # 물러선 뒤엔 내일을 읽는다


def test_빠진_것만_묻는다(도구를_갈아_끼운다, monkeypatch):
    """★ 품목이 없는데 «가격 종류» 만 물으면, 답해도 또 되묻게 된다 (2026-09-15).

    한 번에 알려줬어야 할 것을 두 번에 나눠 묻는 셈이다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])

    #   품목만 없다 — 가격 종류는 다시 안 묻는다
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": "AUC", "dates": []})
    out = qa_graph.answer(QaRequest(question="경락가 알려줘"))
    assert out.meta.status == "NEED_CLARIFY"
    assert "어느 품목인지" in out.markdown
    assert "배추 · 무 · 양파" in out.markdown
    assert "WHSL" not in out.markdown                        # 이미 안 것은 안 묻는다

    #   둘 다 없다 — 한 번에 둘 다 묻는다
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": None, "dates": []})
    out = qa_graph.answer(QaRequest(question="가격 알려줘"))
    assert "어느 품목의 어느 가격인지" in out.markdown
    assert "배추 · 무 · 양파" in out.markdown and "경락가(AUC)" in out.markdown


def test_되물을_때_알아들은_날짜를_밝힌다(도구를_갈아_끼운다, monkeypatch):
    """🔴 날짜를 제대로 골라 놓고 되묻기로 빠지면 그 값이 조용히 사라진다.

    그러면 사람이 「오늘부터 8일」을 또 적어야 한다 — 알아들은 것은 말해 줘야 한다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {
        "route": "forecast", "item": None, "kind": None,
        "dates": [BASE + timedelta(days=d) for d in range(8)],
    })
    out = qa_graph.answer(QaRequest(question="오늘부터 8일동안의 가격을 알려줘"))
    assert out.meta.status == "NEED_CLARIFY"
    assert "알아들은 것" in out.markdown
    assert "8일" in out.markdown
    assert str(BASE) in out.markdown                         # 시작일을 그대로 적는다


def test_LLM_을_못_부르면_해석하지_못했다고_답한다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, None)
    out = qa_graph.answer(QaRequest(question="내일 배추 얼마야?"))
    assert out.meta.status == "LLM_UNAVAILABLE"


def test_스위치를_끄면_해석기가_아예_안_부른다(monkeypatch):
    """★ `ML_LLM_ENABLED=0` 이면 호출 자체가 없어야 한다.

    발표 전에 할당량을 아끼려고 끄는 스위치다. 껐는데 부르면 «껐다» 고 믿은 채로
    429 를 맞는다. 실제로 `enabled()` 를 만들어 두고 아무도 안 부르고 있었다.
    """
    def _절대_안_불려야_한다(*_a, **_k):
        raise AssertionError("스위치를 껐는데 호출했다")

    monkeypatch.setenv("ML_LLM_ENABLED", "0")
    monkeypatch.setenv("ML_GEMINI_API_KEY", "있는-척-하는-키")
    monkeypatch.setattr(qa_llm.urllib.request, "urlopen", _절대_안_불려야_한다)
    assert qa_llm.interpret("내일 배추 얼마야?", BASE) is None


def test_넘치게_고른_가격_종류를_규칙이_자른다():
    """🔴 **지시문으로 두 번 실패한 자리다** (2026-09-15).

    「내일 배추 경락가 얼마야?」 하나를 물어도 해석기가 `AUC·WHSL·RTL` 셋을
    내놓았다. 같은 질문에 세 번 물으면 셋·셋·하나로 흔들렸다. 낱말 풀이를 넣고
    「나온 것만」이라고 적어도 그대로였다.

    **말로 부탁해서 안 되는 것은 규칙이 자른다.**
    """
    셋 = ["AUC", "WHSL", "RTL"]
    assert qa_llm._trim_kinds(셋, "내일 배추 경락가 얼마야?") == ["AUC"]
    assert qa_llm._trim_kinds(셋, "배추 소매가 내일") == ["RTL"]
    #   둘을 물었으면 둘 다 남는다
    assert qa_llm._trim_kinds(셋, "배추 경락가랑 도매가 내일") == ["AUC", "WHSL"]


def test_전부_라고_하면_자르지_않는다():
    """「가격 전부」는 정말 다 달라는 말이다. 그때 자르면 물어본 것을 못 준다."""
    셋 = ["AUC", "WHSL", "RTL"]
    for 문장 in ("배추 가격 전부 다", "배추 가격 모두", "배추 모든 가격"):
        assert qa_llm._trim_kinds(셋, 문장) == 셋, 문장


def test_모르는_표현이면_자르지_않는다():
    """★ 규칙이 답을 **없애면** 안 된다.

    우리가 모르는 말로 물었을 수 있다. 잘라서 빈손이 되면 해석기 쪽이 옳다.
    """
    assert qa_llm._trim_kinds(["AUC", "WHSL"], "배추 값 알려줘") == ["AUC", "WHSL"]


def test_응답_스키마는_칸을_전부_꼭_쓰게_한다():
    """🔴 **화면에서 발견한 것** (2026-09-15).

    `route` 하나만 필수였을 때, `asks` 칸을 더한 뒤로 모델이 `items`·`kinds` 까지만
    쓰고 **`dates`·`asks` 를 통째로 빼먹었다.** 「5일뒤」·「전체기간」·「일주일치」가
    전부 «날짜를 말씀하지 않으셨다» 로 떨어졌다. 뜻은 알아듣고 있었는데 칸을 안 썼다.

    선택 칸이 늘면 모델은 뒤쪽 칸을 건너뛴다 — 비어도 되지만 칸은 반드시 쓰게 한다.
    """
    required = set(qa_llm._RESPONSE_SCHEMA["required"])
    assert {"route", "items", "kinds", "dates", "asks"} <= required
    #   날짜를 목록으로 묶는 판도 같은 필수 목록을 물려받는다
    enum_schema = qa_llm._schema(BASE)
    assert {"dates", "asks"} <= set(enum_schema["required"])


def test_해석기는_틀린_날짜를_고쳐_쓰지_않고_버린다():
    assert qa_llm._parse_dates(["2026-09-15", "내일", None, "2026-13-40"]) == [
        date(2026, 9, 15)
    ]


def test_Swagger_기본값이_와도_질문으로_답한다(도구를_갈아_끼운다, monkeypatch):
    """★ 2026-09-14 실측 — Swagger «Try it out» 이 item 에 "string" 을 넣어 준다.

    그것을 품목으로 읽고 거절하면 질문 문장을 쳐다보지도 않는다. 그리고 같이 온
    **유효한 kind 는 살려야** 한다 — 버리면 답할 수 있는 질문에 되묻게 된다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": None, "dates": []})
    out = qa_graph.answer(
        QaRequest(question="배추값 알려줘", item="string", kind="AUC")
    )
    assert out.meta.status == "OK"
    assert out.meta.item == "배추" and out.meta.kind == "AUC"   # 유효한 kind 는 살린다
    assert "«string»" in out.markdown                          # 무시한 것을 밝힌다


def test_질문이_없으면_잘못된_품목은_그대로_거절한다(도구를_갈아_끼운다):
    """질문이 없으면 대신 읽을 것이 없다. 조용히 넘어가지 않는다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    out = qa_graph.answer(QaRequest(item="string", kind="AUC"))
    assert out.meta.status == "OUT_OF_SCOPE"


def test_질문_한_칸짜리_입구가_돈다(도구를_갈아_끼운다, monkeypatch):
    """★ 프롬프트처럼 쓰는 입구다 — GET /ml/qa?q=... 에 질문만 넣는다."""
    from fastapi.testclient import TestClient

    from app.main import app

    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": "AUC",
                       "dates": [BASE + timedelta(days=1)]})
    client = TestClient(app)
    got = client.get("/ml/qa", params={"q": "내일 배추 경락가 얼마야?"})
    assert got.status_code == 200
    assert got.json()["meta"]["status"] == "OK"
    assert "867" in got.json()["markdown"]


def test_질문이_없으면_입구가_막는다():
    """빈 질문을 받아 «해석 못 했다» 로 답하느니, 아예 안 받는 쪽이 낫다."""
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).get("/ml/qa").status_code == 422
