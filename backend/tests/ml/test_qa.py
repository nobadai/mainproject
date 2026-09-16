"""예측 질의응답 — 경로별 검사.

★ **DB 를 안 쓴다.** 도구 넷을 갈아 끼워 그래프의 판단만 잰다. 값이 맞는지는 표가
  답할 일이고, 여기서 잴 것은 *«어떤 상황에 어떤 문구가 나가는가»* 다.

🔴 검사의 핵심은 **못 한 것이 한 것처럼 보이지 않는가** 다.
   창고를 못 읽었을 때 예시값이 나가거나, 해석 못 한 질문에 그럴듯한 답이 나가면
   그 순간 이 기능은 위험물이 된다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

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
        #   ★ 현재 모델도 DB 를 읽는다. **기본은 «교체 이력 표가 없다»** 다 —
        #     2026-09-16 실측에서 두 창고 다 `model_cutover` 가 없었다.
        #     따로 잴 검사는 `모델도구를_갈아_끼운다` 로 덮어쓴다.
        monkeypatch.setattr(
            qa_graph.qa_tools, "current_models",
            lambda: _models(cutover=False, read="absent"),
        )

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
    #   is_filled 는 답에 드러난다 — 문구는 사용자 지시로 바꿨다 (2026-09-15)
    assert "직전 예측값을 사용합니다" in out.markdown


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


def test_쓰지_말라는_판정은_meta_로_간다(도구를_갈아_끼운다):
    """★ 답 문장에서는 뺐다 (2026-09-16 · 사용자 결정 ⑦). 가격 답은 표만 둔다.

    🔴 **값은 안 지웠다.** `use_recommended` 가 `meta` 로 그대로 가고, 마스터 payload
      에도 실린다 — 판단하는 쪽이 저기다. 문장에서 빼는 것과 값에서 지우는 것은
      전혀 다른 일이고, 값까지 지우면 저쪽이 조용히 못 쓰는 조합을 쓰게 된다.
    """
    도구를_갈아_끼운다(
        rows=[_row(1)],
        usab={"use_recommended": False, "quality_note": "앵커가 거의 완벽",
              "band_method": "fixed_table"},
    )
    out = qa_graph.answer(QaRequest(item="양파", kind="WHSL"))
    assert out.meta.use_recommended is False
    assert "쓰지 마세요" not in out.markdown
    assert "앵커가 거의 완벽" not in out.markdown


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


def test_게이트_행에도_출발점_문구를_안_적는다(도구를_갈아_끼운다):
    """★ 표 아래 «출발점» 줄을 뺄 때 **비고 칸의 같은 말을 놓쳤다** (2026-09-15 화면).

    「모델 대신 출발점을 그대로 씀」이 비고에만 남아, 출발점이라는 말을 없앤
    화면에서 뜻 모를 문구가 됐다. 정보는 meta.is_gated 로 옮긴다.
    """
    gated = {**_row(1), "is_gated": True}
    도구를_갈아_끼운다(rows=[gated])
    out = qa_graph.answer(QaRequest(item="무", kind="RTL"))
    assert "출발점" not in out.markdown
    assert out.meta.is_gated == [True]


def test_쓰지_말라는_경고는_문장에서_빠지고_표만_남는다(도구를_갈아_끼운다):
    """★ 2026-09-15 에는 «문장에 남긴다» 였고, 2026-09-16 에 **빼기로** 바꿨다.

    두 판단이 갈린 자리라 둘 다 적어 둔다 — 「못 보면 그대로 쓴다」 는 걱정은
    여전히 맞지만, 그 걱정은 **사람이 아니라 마스터가** 받는다 (`use_recommended`).
    """
    도구를_갈아_끼운다(
        rows=[_row(1)],
        usab={"use_recommended": False, "quality_note": "앵커가 거의 완벽"},
    )
    out = qa_graph.answer(QaRequest(item="양파", kind="WHSL"))
    assert "쓰지 마세요" not in out.markdown
    #   표는 그대로 나간다 — 값을 감추는 것이 아니다
    assert "| 날짜 | 예측 | 예상 구간 | 비고 |" in out.markdown
    assert out.meta.use_recommended is False


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


def test_가격_종류를_못_고르면_셋_다_답한다(도구를_갈아_끼운다, monkeypatch):
    """★ 전에는 되물었다 (2026-09-16 · 사용자 지시로 바꿈).

    되묻는 대신 **셋을 다 보여준다** — 무엇을 기본값으로 채웠는지는 한 줄로 밝힌다.
    한 번 더 묻게 하는 것보다 한 번에 다 주는 쪽이 사람이 덜 친다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": None,
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="배추 가격 알려줘"))
    assert out.meta.status == "OK"
    assert out.meta.kinds == ["AUC", "WHSL", "RTL"]
    assert out.meta.items == ["배추", "배추", "배추"]
    assert "경락가" in out.markdown and "소매가" in out.markdown
    assert "가격 종류를 말씀하지 않으셔서" in out.markdown


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
    #   ★ 더 볼 수 있다고 알려준다. **「전부」라고 권하지 않는다** (2026-09-16 ·
    #     사용자 지시) — 그 말은 우리 해석기만 알아듣고 마스터를 거쳐 오면
    #     못 알아듣는다. 되는 말을 예로 보인다
    assert qa_graph.ASK_DATE_HINT in out.markdown
    assert "「전부」라고 하시면" not in out.markdown
    #   🔴 **꼬리 한 줄을 글자 그대로** 잰다. 조각으로만 재면 문장이 어디서
    #     끊기거나 띄어쓰기가 달라져도 통과한다 — 사람이 보는 것은 줄 전체다
    assert (
        "> 날짜를 따로 말씀하지 않으셔서 **오늘** 값을 보여드립니다. "
        "원하는 날짜나 기간을 말씀해주시면 해당 기간에 대한 가격을 보여드립니다. "
        "예) 일주일치의 가격 / 3일 뒤 가격"
    ) in out.markdown


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
    #   ★ 물러선 경우에도 **뒷문장은 같다** — 날짜를 말하면 그 날을 준다는 안내는
    #     오늘을 줬든 내일로 물러섰든 똑같이 필요하다
    assert (
        "> 날짜를 따로 말씀하지 않으셔서 오늘 값이 아직 없어 **내일** 값을 보여드립니다. "
        f"{qa_graph.ASK_DATE_HINT}"
    ) in out.markdown
    assert calls[0] == []                                    # 첫 조회는 읽을 날이 없었다
    assert calls[1] == [BASE + timedelta(days=1)]            # 물러선 뒤엔 내일을 읽는다


def test_빠진_것은_되묻지_않고_전부로_채운다(도구를_갈아_끼운다, monkeypatch):
    """★ 전에는 «빠진 것만» 되물었다 (2026-09-16 · 사용자 지시로 바꿈).

    되물으면 사람이 한 번 더 쳐야 한다. 이제는 **빠진 자리를 전부로 채우고**
    무엇을 채웠는지만 한 줄로 밝힌다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])

    #   품목만 없다 — 가격 종류는 고른 것을 그대로 쓴다
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": "AUC", "dates": []})
    out = qa_graph.answer(QaRequest(question="경락가 알려줘"))
    assert out.meta.status == "OK"
    assert out.meta.items == ["배추", "무", "양파"]
    assert set(out.meta.kinds) == {"AUC"}                    # 안 채운 자리는 안 건드린다
    assert "품목을 말씀하지 않으셔서" in out.markdown

    #   둘 다 없다 — 아홉 조합
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": None, "dates": []})
    out = qa_graph.answer(QaRequest(question="가격 알려줘"))
    assert out.meta.status == "OK"
    assert len(out.meta.items) == 9
    assert "품목과 가격 종류를 말씀하지 않으셔서" in out.markdown


def test_알아들은_날짜는_되묻기_없이_그대로_쓴다(도구를_갈아_끼운다, monkeypatch):
    """🔴 날짜를 제대로 골라 놓고 되묻기로 빠지면 그 값이 조용히 사라진다.

    그러면 사람이 「오늘부터 8일」을 또 적어야 한다. 2026-09-15 에는 «알아들은 것»
    을 밝히는 것으로 막았고, 2026-09-16 부터는 **아예 안 되묻고** 품목·가격만
    전부로 채운다. 날짜는 고른 그대로 쓴다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {
        "route": "forecast", "item": None, "kind": None,
        "dates": [BASE + timedelta(days=d) for d in range(8)],
    })
    out = qa_graph.answer(QaRequest(question="오늘부터 8일동안의 가격을 알려줘"))
    assert out.meta.status != "NEED_CLARIFY"
    assert "품목과 가격 종류를 말씀하지 않으셔서" in out.markdown
    #   날짜를 버리지 않았다 — 기본값(오늘) 줄이 안 나온다
    assert "날짜를 따로 말씀하지" not in out.markdown
    assert str(BASE + timedelta(days=1)) in out.markdown


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

    ★ `route` 한 칸이 **`routes` 목록으로 넓어졌다** (2026-09-16). 스키마에는 목록
      하나만 둔다 — 두 칸을 다 두면 값이 어긋나는 날 어느 쪽이 정본인지 못 정한다.
      **돌려주는 값에는 `route` 를 남긴다** (`test_옛_route_한_칸만_와도_그대로_읽는다`).
    """
    required = set(qa_llm._RESPONSE_SCHEMA["required"])
    assert {"routes", "items", "kinds", "dates", "asks"} <= required
    #   날짜를 목록으로 묶는 판도 같은 필수 목록을 물려받는다
    enum_schema = qa_llm._schema(BASE)
    assert {"dates", "asks"} <= set(enum_schema["required"])


def test_범위_밖만_물으면_오늘_값을_주지_않고_안내만_한다(도구를_갈아_끼운다, monkeypatch):
    """🔴 **화면에서 발견한 것** (2026-09-15).

    「30일 뒤의 배추 경락가」에 오늘 값과 «날짜를 따로 말씀하지 않으셔서» 가 나갔다.
    날짜를 19개 목록에서만 고르게 한 뒤로 30일 뒤는 보기에 없어 모델이 날짜를 비웠고,
    코드가 그걸 «안 물었다» 로 읽었다. 물은 날과 **다른 날**을 답한 것이다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": "AUC",
                       "dates": [BASE + timedelta(days=30)], "asks": []})
    out = qa_graph.answer(QaRequest(question="30일 뒤의 배추 경락가를 알려줘", as_of=BASE))
    assert out.meta.status == "OUT_OF_SCOPE"
    assert "예측 범위 밖" in out.markdown
    assert "날짜를 따로 말씀하지" not in out.markdown
    assert "| 날짜 | 예측 |" not in out.markdown                 # 표가 없다
    assert out.meta.out_of_range == [BASE + timedelta(days=30)]


def test_목록_밖_날은_정수로_받아_날짜로_바꾼다():
    """보기에 없는 날을 적을 칸이다. 정수가 아닌 것은 고쳐 쓰지 않고 버린다."""
    오늘 = date(2026, 9, 15)
    assert qa_llm._far_dates([30, -1], 오늘) == [date(2026, 10, 15), date(2026, 9, 14)]
    assert qa_llm._far_dates(["30", 1.5, True, None], 오늘) == []


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


# ── 갈래 둘을 더한다 — 배치 결과 · 모델 성능 ─────────────────────────────


def _batch(status: str = "ok", n_ok: int = 11, n_fail: int = 0) -> dict:
    """`batch_run` 한 행. **시작 시각은 UTC 다** — 화면에는 한국 시간으로 적는다."""
    return {
        "run_id": 75,
        "started_at": datetime(2026, 9, 16, 0, 0, 2, tzinfo=UTC),
        "finished_at": datetime(2026, 9, 16, 0, 10, 6, tzinfo=UTC),
        "status": status,
        "host": "DESKTOP-KEDJ5F3",
        "n_ok": n_ok,
        "n_fail": n_fail,
        "note": None,
    }


#: 실제 보고서 본문의 앞부분 (2026-09-16 09:25 · id 246) 을 줄여서 옮긴 것.
CHECK_BODY = (
    "# 일별 배치 점검 — 2026-09-16 (09:23에 점검함)\n\n"
    "**요약: 배치가 잘 돌았습니다. 데이터 품질은 정상입니다.**\n\n"
    "## 1. 배치\n\n- 75번째 실행이 2026-09-16 09:00:03에 정상적으로 끝났습니다."
)


def _check_report() -> dict:
    return {
        "id": 246,
        "name": "claude_check",
        "verdict": None,
        #   ★ 시간대가 **없는** 값이다 — 원본이 한국 시간으로 앉아 있다.
        "ran_at": datetime(2026, 9, 16, 9, 25, 0),  # noqa: DTZ001
        "payload": None,
        "body": CHECK_BODY,
    }


def _verify_report() -> dict:
    """실제 `재학습검증` payload (2026-09-16 09:09:24 · id 217) 를 줄여서 옮긴 것."""
    return {
        "id": 217,
        "name": "재학습검증",
        "verdict": "주의",
        "ran_at": datetime(2026, 9, 16, 9, 9, 24),  # noqa: DTZ001
        "payload": {
            "at": "2026-09-16 09:09:24",
            "name": "재학습검증",
            "verdict": "주의",
            "findings": [
                {
                    "level": "정상", "title": "견주는 창", "advice": "",
                    "detail": "둘 다 이 구간을 학습에 안 썼습니다.",
                    #   ★ 학습 끝 두 칸을 되살렸다 (2026-09-16). **원본에 있는 값이고**,
                    #     둘이 같다는 것이 «판정 불가» 셋의 이유다 — 빼 놓으면
                    #     교체 직후라 견줄 것이 없다는 사실을 잴 수 없다.
                    "numbers": [["구간", "2026-01-01 ~ 2026-09-15"],
                                ["기준일", "173개"],
                                ["행수", "9,348행 (LT>=0)"],
                                ["현행 학습 끝", "2025-12-31"],
                                ["후보 학습 끝", "2025-12-31"]],
                },
                {
                    "level": "주의", "title": "배추 — 판정 불가", "advice": "",
                    "detail": "차이가 시드 흔들림 안입니다.",
                    "numbers": [["행수", "3,116행"],
                                ["현행 WMAPE", "0.0725"],
                                ["후보 WMAPE", "0.0725"],
                                ["앵커 WMAPE", "0.0849"],
                                ["차이 (현행−후보)", "+0.0000"],
                                ["시드 편차×2", "0.0020"]],
                },
                {
                    "level": "주의",
                    "title": "채택하지 않습니다 — 나아진 것을 증명 못 했습니다",
                    "advice": "", "detail": "", "numbers": [],
                },
            ],
        },
        "body": "재학습검증 · 2026-09-16 09:09:24",
    }


@pytest.fixture
def 배치도구를_갈아_끼운다(monkeypatch: pytest.MonkeyPatch):
    """배치·보고서 도구를 갈아 끼운다. **기본은 «아무것도 없다» 다.**"""

    def install(*, run=None, fails=None, reports=None, pending=None,
                boom_batch=False, boom_report=False, boom_pending=False):
        def _raise(*_a, **_k):
            raise RuntimeError("connection refused")

        #   ★ 값은 **한 건이든 여럿이든** 받는다 (2026-09-16). 재학습 보고서는
        #     그날 여러 건이 남는다 — 한 건만 담는 모양으로는 그 사고를 못 잰다.
        found = {
            name: (list(value) if isinstance(value, list) else [value])
            for name, value in (reports or {}).items()
        }
        monkeypatch.setattr(
            qa_graph.qa_tools, "batch_run",
            _raise if boom_batch else (lambda on: run),
        )
        monkeypatch.setattr(
            qa_graph.qa_tools, "failed_stages", lambda run_id: list(fails or [])
        )
        monkeypatch.setattr(
            qa_graph.qa_tools, "agent_report",
            _raise if boom_report else (lambda name, on: (found.get(name) or [None])[-1]),
        )
        monkeypatch.setattr(
            qa_graph.qa_tools, "agent_reports",
            _raise if boom_report else (lambda name, on: list(found.get(name) or [])),
        )
        #   🔴 **이건 HTTP 다.** 안 갈아 끼우면 검사가 진짜 8102 를 부른다 —
        #     그 서버가 떠 있느냐에 따라 검사 결과가 달라진다. 기본은 «후보 없음».
        monkeypatch.setattr(
            qa_graph.qa_tools, "retrain_pending",
            _raise if boom_pending else (lambda: list(pending or [])),
        )

    return install


def _routes(monkeypatch, routes, **extra):
    """해석기가 갈래를 골랐다고 해 둔다. **LLM 은 안 부른다.**"""
    chosen = {
        "routes": routes,
        "route": routes[0],
        "items": extra.get("items", []),
        "kinds": extra.get("kinds", []),
        "item": (extra.get("items") or [None])[0],
        "kind": (extra.get("kinds") or [None])[0],
        "dates": extra.get("dates", []),
        "asks": extra.get("asks", []),
    }
    monkeypatch.setattr(qa_graph.qa_llm, "interpret", lambda q, base: chosen)


def test_배치를_물으면_그날_결과와_점검_보고서를_그대로_붙인다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """★ 점검 보고서는 **요약하지 않는다** — 그대로 붙인다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(run=_batch(), reports={"claude_check": _check_report()})
    _routes(monkeypatch, ["batch"])
    out = qa_graph.answer(
        QaRequest(question="오늘 데이터 처리 잘 됐어?", as_of=date(2026, 9, 16))
    )
    assert out.meta.status == "OK"
    assert out.meta.routes == ["batch"]
    assert "정상" in out.markdown                        # ok 를 사람 말로
    assert "09:00" in out.markdown and "09:10" in out.markdown   # UTC 가 아니다
    assert CHECK_BODY in out.markdown                    # 보고서 본문 그대로
    #   코드 이름·enum 값을 답 문장에 안 적는다
    for code_word in ("batch_run", "agent_report", "claude_check", "n_ok"):
        assert code_word not in out.markdown


def test_배치_행이_없으면_기록이_없다고_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(run=None, reports={})
    _routes(monkeypatch, ["batch"])
    out = qa_graph.answer(QaRequest(question="오늘 배치 어때?", as_of=date(2026, 9, 16)))
    #   ★ 문구를 바꿨다 (2026-09-16 · 결정 ①) — «기준일(…)에 대한» 을 앞에 붙인다.
    #     어느 날 이야기인지가 문장 안에 있어야 마스터가 그대로 실어도 말이 된다.
    assert "기준일(2026-09-16)에 대한 배치 기록이 없습니다" in out.markdown
    assert "기준일(2026-09-16)에 대한 점검 보고서도 아직 없습니다" in out.markdown


def test_실패한_단계가_있으면_사람_말로_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 실패를 숨기지 않는다. 단계 이름은 코드 이름이 아니라 사람 말로 적는다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(
        run=_batch(status="partial", n_ok=9, n_fail=2),
        fails=[{"seq": 2, "stage": "collect_price", "duration_s": 4.7,
                "message": "HTTP 500\n재시도 3회 실패"}],
        reports={},
    )
    _routes(monkeypatch, ["batch"])
    out = qa_graph.answer(QaRequest(question="오늘 배치 어때?", as_of=date(2026, 9, 16)))
    assert "일부 실패" in out.markdown                    # partial 을 사람 말로
    assert "중도매·소매가 수집" in out.markdown            # collect_price 가 아니라
    assert "collect_price" not in out.markdown
    assert "HTTP 500" in out.markdown


def test_배치를_못_읽어도_한_줄로_말하고_끝낸다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(boom_batch=True, boom_report=True)
    _routes(monkeypatch, ["batch"])
    out = qa_graph.answer(QaRequest(question="오늘 배치 어때?", as_of=date(2026, 9, 16)))
    assert "읽지 못했습니다" in out.markdown
    assert out.meta.status == "SOURCE_UNAVAILABLE"


def test_성능을_물으면_봉인_아홉칸과_조건을_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """★ 조건 없는 수치는 안 적는다 — 출처 한 줄이 늘 같이 간다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))
    assert out.meta.routes == ["perf"]
    assert qa_tools.SEALED_SOURCE in out.markdown
    for 숫자 in ("19.7", "9.0", "12.7", "8.3"):
        assert 숫자 in out.markdown
    #   ★ «후보 없음» 이 아니라 «기록이 없다» 로 바꿨다 (2026-09-16 · 결정 ①).
    #     배치가 아예 안 돈 날을 «검사해 봤더니 바꿀 게 없다» 로 읽으면 안 된다.
    assert "기준일(2026-09-16)에 대한 재학습 판정 기록이 없습니다" in out.markdown
    assert len(out.performance_for_evidence) == 9


def test_재학습_후보가_있으면_판정_문구를_그대로_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 «판정 불가» 를 «문제 없음» 으로 바꾸지 않는다. 그대로 옮긴다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={"재학습검증": _verify_report()})
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="재학습 후보 있어?", as_of=date(2026, 9, 16)))
    assert "판정 불가" in out.markdown
    assert "채택하지 않습니다 — 나아진 것을 증명 못 했습니다" in out.markdown
    assert "주의" in out.markdown                          # verdict 그대로
    for 수치 in ("0.0725", "0.0849", "0.0020", "2026-01-01 ~ 2026-09-15"):
        assert 수치 in out.markdown
    assert "재학습 후보 없음" not in out.markdown
    assert "문제 없음" not in out.markdown


def _pending(kind: str = "rtl") -> dict:
    """실제 `/retrain/pending` 의 `pending[]` 한 줄 모양 (`agent/retrain_auto.py`).

    🔴 **학습 끝 칸이 없다.** 후보 이름의 `20260916` 은 **만든 날**이지 학습 끝이
      아니다 — 답은 그걸로 되짚지 않고, 검증 보고서에 적혀 있을 때만 적는다.
    """
    return {
        "kind": kind,
        "state": "pending",
        "sec": 18.4,
        "candidate": f"ops_{kind}_cand_20260916",
        "items": [],
        "verify": "",
    }


def test_바꿀_후보가_있으면_한_줄과_버튼이_같이_나간다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """★ 버튼은 `action:` 스킴 링크로 싣는다 — 채팅까지 가는 것은 마크다운뿐이다.

    ★ 학습 끝은 **검증 보고서가 가격 종류를 밝혔을 때만** 붙는다. 여기서는
      `payload.kind` 를 넣어 «붙는 쪽» 을 잰다 — 안 붙는 쪽은 다음 검사다.
    """
    보고서 = _verify_report()
    보고서["payload"] = dict(보고서["payload"], kind="rtl")
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={"재학습검증": 보고서}, pending=[_pending("rtl")])
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))

    한_줄 = "소매가 후보 (학습 끝 2025-12-31) — 현행보다 나음 · 업데이트할 수 있습니다"
    버튼 = "[모델 업데이트 — 소매가](action:retrain-apply?kind=rtl)"
    assert 한_줄 in out.markdown
    assert 버튼 in out.markdown
    assert qa_graph.UPDATE_UNREADABLE not in out.markdown

    #   ★ **맨 아래다** (2026-09-16 · 사용자 지시). 누를지 정할 근거(검증 표)를
    #     다 보인 뒤라야 한다 — 표보다 먼저 나오면 «보기 전에 누르라» 가 된다.
    assert out.markdown.rstrip().endswith(버튼)
    assert out.markdown.index(한_줄) > out.markdown.index("**현재 모델**")
    assert out.markdown.index(한_줄) > out.markdown.index("**모델 성능 — ")
    assert out.markdown.index(한_줄) > out.markdown.index("현행 WMAPE")
    assert out.markdown.index(버튼) > out.markdown.index(한_줄)


def test_학습_끝을_모르면_괄호째_뺀다_지어내지_않는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 후보 이름의 날짜(`…_20260916`)로 학습 끝을 되짚지 않는다.

    🔴 **오늘 실제 자료가 이 쪽이다** (2026-09-16 실측). 그날 검증 보고서 둘은
      제목이 «견주는 창» · «배추 — …» 로 시작해 가격 종류가 어디에도 없고,
      `payload.kind` 도 아직 안 붙었다 — `_report_kind` 가 «(가격 종류 미상)»
      을 돌려준다. 그러면 후보와 보고서를 짝지을 수 없으므로 **괄호째 뺀다.**
    """
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={"재학습검증": _verify_report()},
                    pending=[_pending("auc")])
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))

    assert "경락가 후보 — 현행보다 나음 · 업데이트할 수 있습니다" in out.markdown
    #   «(학습 끝 …)» 괄호가 통째로 없다. 표 머리의 «학습 끝» 칸과 섞지 않는다
    assert "(학습 끝" not in out.markdown
    assert "20260916" not in out.markdown
    assert "[모델 업데이트 — 경락가](action:retrain-apply?kind=auc)" in out.markdown


def test_재학습_기록을_못_읽어도_버튼은_그_줄_뒤에_붙는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 나가는 문이 하나라야 한다.

    보고서를 못 읽는 것과 «바꿀 후보가 있나» 는 **다른 창구**다 (앞은 DB, 뒤는
    ML 콘솔). 중간에서 빠져나가면 보고서가 안 읽히는 날에만 버튼이 통째로
    사라진다 — 정작 그날도 바꿀 것은 있을 수 있다.
    """
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(boom_report=True, pending=[_pending("whsl")])
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))

    버튼 = "[모델 업데이트 — 중도매가](action:retrain-apply?kind=whsl)"
    assert "재학습 기록을 읽지 못했습니다." in out.markdown
    assert 버튼 in out.markdown
    assert out.markdown.index(버튼) > out.markdown.index("재학습 기록을 읽지 못했습니다.")
    assert out.markdown.rstrip().endswith(버튼)


def test_바꿀_후보가_없으면_버튼도_문장도_없다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """★ 없는 것이 정상이다. 그때 버튼을 그리면 누를 것이 없는 버튼이 나간다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={}, pending=[])
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))

    assert "action:" not in out.markdown
    assert "모델 업데이트" not in out.markdown
    assert "업데이트할 수 있습니다" not in out.markdown
    assert qa_graph.UPDATE_UNREADABLE not in out.markdown


def test_콘솔을_못_읽으면_확인_불가라고_적고_버튼은_안_그린다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 «못 읽었다» 를 «없다» 로 적지 않는다 — 앞은 고장이고 뒤는 정상이다.

    ★ 그리고 **성능표는 그대로 나간다.** 콘솔 하나가 죽었다고 답을 비우지 않는다.
    """
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={}, boom_pending=True)
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))

    assert qa_graph.UPDATE_UNREADABLE in out.markdown
    assert "action:" not in out.markdown
    assert "업데이트할 수 있습니다" not in out.markdown
    #   성능표는 살아 있다
    assert qa_tools.SEALED_SOURCE in out.markdown
    assert "19.7" in out.markdown


def test_보고서_제목의_가격종류_코드는_사람_말로_바꾼다():
    """★ 제목 맨 앞의 «rtl» 은 코드 이름이다. **그 한 낱말만** 바꾼다.

    🔴 판정 문구는 손대지 않는다 — «1주 연속 앵커에 밀렸습니다» 는 그대로다.
    """
    assert qa_graph._kindly("rtl 무 — 1주 연속 앵커에 밀렸습니다 · 재학습 후보") == (
        "소매가 무 — 1주 연속 앵커에 밀렸습니다 · 재학습 후보"
    )
    assert qa_graph._kindly("auc — 학습이 990일 전에서 멈춰 있습니다") == (
        "경락가 — 학습이 990일 전에서 멈춰 있습니다"
    )
    #   가격 종류로 시작하지 않으면 한 글자도 안 바꾼다
    assert qa_graph._kindly("배추 — 판정 불가") == "배추 — 판정 불가"


def test_갈래_둘을_고르면_둘_다_답한다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(run=_batch(), reports={"claude_check": _check_report()})
    _routes(monkeypatch, ["batch", "perf"])
    out = qa_graph.answer(
        QaRequest(question="오늘 상태 어때? 성능도", as_of=date(2026, 9, 16))
    )
    assert out.meta.routes == ["batch", "perf"]
    assert CHECK_BODY in out.markdown
    assert qa_tools.SEALED_SOURCE in out.markdown


def test_가격과_배치를_같이_물으면_표와_배치가_같이_나온다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    도구를_갈아_끼운다(rows=[_row(5)], base=BASE)
    배치도구를_갈아_끼운다(run=_batch(), reports={})
    #   ★ 날짜는 **짝 물음 안에만** 들어온다 (2026-09-16 · 실제 해석기 응답으로 확인).
    #     맨 위 `dates` 는 비어 있어서 배치 쪽은 «날짜를 안 말한 것» 이 된다.
    _routes(monkeypatch, ["forecast", "batch"], items=["배추"], kinds=["AUC"],
            dates=[], asks=[{"item": "배추", "kind": "AUC",
                             "dates": [BASE + timedelta(days=5)]}])
    out = qa_graph.answer(QaRequest(question="5일 뒤 배추 경락가랑 배치 상태"))
    assert out.meta.routes == ["forecast", "batch"]
    assert "| 날짜 | 예측 |" in out.markdown               # 가격 표
    assert "867" in out.markdown
    assert "정상" in out.markdown                          # 배치 상태
    assert out.rows_for_evidence                           # 예측도 읽었다


def test_한_갈래가_실패해도_다른_갈래_답은_나간다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 하나가 터졌다고 답을 통째로 버리지 않는다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(boom_batch=True, boom_report=True)
    _routes(monkeypatch, ["batch", "perf"])
    out = qa_graph.answer(QaRequest(question="오늘 상태랑 성능", as_of=date(2026, 9, 16)))
    assert "읽지 못했습니다" in out.markdown                # 배치는 실패
    assert qa_tools.SEALED_SOURCE in out.markdown           # 성능은 나간다
    assert out.meta.status == "PARTIAL"


def test_옛_route_한_칸만_와도_그대로_읽는다(도구를_갈아_끼운다, monkeypatch):
    """★ 계약을 넓혔지만 **옛 이름을 끊지 않는다.** `route` 한 칸만 와도 돈다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "배추", "kind": "AUC",
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="내일 배추 경락가 얼마야?"))
    assert out.meta.status == "OK"
    assert out.meta.routes == ["forecast"]
    assert "867" in out.markdown


def test_어느_갈래도_아니면_무엇을_답할_수_있는지_알려준다(도구를_갈아_끼운다, monkeypatch):
    """★ 되묻되 **세 갈래를 다 알려준다** — 가격만 있는 줄 알면 다시 안 묻는다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "clarify", "item": None, "kind": None, "dates": []})
    out = qa_graph.answer(QaRequest(question="뭐 좀 알려줘"))
    assert out.meta.status == "NEED_CLARIFY"
    assert "배추 · 무 · 양파" in out.markdown
    assert "배치" in out.markdown and "성능" in out.markdown


def test_응답_스키마와_지시문에_세_갈래가_들어_있다():
    """★ 해석기를 하나 더 두지 않는다 — 한 번 부르고 **갈래를 여럿** 받는다."""
    required = set(qa_llm._RESPONSE_SCHEMA["required"])
    assert "routes" in required
    갈래 = set(qa_llm._RESPONSE_SCHEMA["properties"]["routes"]["items"]["enum"])
    assert {"forecast", "batch", "perf"} <= 갈래
    for 낱말 in ("배치", "성능", "재학습", "점검"):
        assert 낱말 in qa_llm.SYSTEM_PROMPT_KO, 낱말


def test_배치_갈래는_예측표가_없어도_답한다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 예측 창고가 죽어도 **배치 답은 나가야 한다** — 서로 다른 표다."""
    도구를_갈아_끼운다(boom=True)
    배치도구를_갈아_끼운다(run=_batch(), reports={"claude_check": _check_report()})
    _routes(monkeypatch, ["batch"])
    out = qa_graph.answer(QaRequest(question="오늘 배치 어때?", as_of=date(2026, 9, 16)))
    assert out.meta.status == "OK"
    assert CHECK_BODY in out.markdown


# ── 현재 모델 · 그날 보고서 전부 (2026-09-16) ────────────────────────────


def _models(*, cutover: bool = True, read: str = "ok") -> dict:
    """`qa_tools.current_models()` 가 돌려주는 모양. **실측 값 그대로** (2026-09-16).

    ★ 만든 날은 `prediction_log` 의 최신 기준일 행에서 읽은 것이다.
      소매가만 2026-09-12 인 것이 09-15 저녁 교체의 흔적이다.
    """
    made = {
        "AUC": datetime(2026, 9, 8, 19, 5, 50),              # noqa: DTZ001
        "WHSL": datetime(2026, 9, 8, 19, 7, 37),             # noqa: DTZ001
        "RTL": datetime(2026, 9, 12, 9, 11, 51),             # noqa: DTZ001
    }
    return {
        "cutover_read": read,
        "models": [
            {
                "kind": kind,
                "model_ver": qa_tools.OPS_MODEL[kind],
                "created_at": made[kind],
                "base_dt": date(2026, 9, 16),
                "train_end": date(2025, 12, 31) if cutover else None,
                "last_swapped_at": (
                    datetime(2026, 9, 15, 20, 31, 0) if cutover else None  # noqa: DTZ001
                ),
                "last_swap_note": "사람이 눌러 바꿈" if cutover else None,
                #   ★ 시각을 아는가 (2026-09-16 · 결정 ⑥). 백필한 행은 폴더 이름에서
                #     되짚어 **날짜만** 확실한 것이 있다. 칸 자체가 없을 수도 있다.
                "last_swap_time_known": True if cutover else None,
            }
            for kind in ("AUC", "WHSL", "RTL")
        ],
    }


@pytest.fixture
def 모델도구를_갈아_끼운다(monkeypatch: pytest.MonkeyPatch):
    """`current_models()` 를 갈아 끼운다. **기본은 «교체 이력이 없다» 다.**"""

    def install(*, found=None, boom=False):
        def _raise(*_a, **_k):
            raise RuntimeError("connection refused")

        def _give():
            return found if found is not None else _models(cutover=False, read="absent")

        monkeypatch.setattr(
            qa_graph.qa_tools, "current_models", _raise if boom else _give
        )

    return install


def _judge_report(rid: int, kind_head: str, candidates: int, verdict: str = "이상") -> dict:
    """실제 `재학습판정` payload (2026-09-16 · id 213·214·216) 를 줄여서 옮긴 것.

    🔴 **payload 에 `kind` 칸이 없다** — 가격 종류는 제목 맨 앞 낱말에만 있다.
    """
    return {
        "id": rid,
        "name": "재학습판정",
        "verdict": verdict,
        "ran_at": datetime(2026, 9, 16, 9, 8, 9),            # noqa: DTZ001
        "payload": {
            "at": "2026-09-16 09:08:09",
            "name": "재학습판정",
            "verdict": verdict,
            "findings": [
                {
                    "level": "주의",
                    "title": f"{kind_head} — 학습이 990일 전에서 멈춰 있습니다",
                    "advice": "", "detail": "",
                    "numbers": [["학습 끝난 날", "2023-12-31"], ["지난 날수", "990일"]],
                },
                {
                    "level": "이상",
                    "title": f"{kind_head} 무 — 1주 연속 앵커에 밀렸습니다 · 재학습 후보",
                    "advice": "", "detail": "",
                    "numbers": [["2026-09-07", "모델 12.3% · 앵커 9.2%"]],
                },
                {
                    "level": "이상",
                    "title": f"재학습 후보 {candidates}개",
                    "advice": "", "detail": "", "numbers": [],
                },
            ],
        },
        "body": f"재학습판정 · {kind_head}",
    }


def _verify_auc_report() -> dict:
    """실제 `재학습검증` payload (2026-09-16 09:09:05 · id 215) 를 줄여서 옮긴 것.

    🔴 이 보고서의 «배추 — 후보가 **나쁩니다**» 가 답에서 빠지고 있었다 —
      그날 검증이 둘인데 최신 하나만 읽었기 때문이다.
    """
    return {
        "id": 215,
        "name": "재학습검증",
        "verdict": "이상",
        "ran_at": datetime(2026, 9, 16, 9, 9, 5),            # noqa: DTZ001
        "payload": {
            "at": "2026-09-16 09:09:05",
            "name": "재학습검증",
            "verdict": "이상",
            "findings": [
                {
                    "level": "정상", "title": "견주는 창", "advice": "",
                    "detail": "둘 다 이 구간을 학습에 안 썼습니다.",
                    "numbers": [["구간", "2026-01-01 ~ 2026-09-15"],
                                ["현행 학습 끝", "2023-12-31"],
                                ["후보 학습 끝", "2025-12-31"]],
                },
                {
                    "level": "이상", "title": "배추 — 후보가 **나쁩니다**",
                    "advice": "", "detail": "편차x2 를 넘어 나쁩니다.",
                    "numbers": [["행수", "3,039행"],
                                ["현행 WMAPE", "0.1419"],
                                ["후보 WMAPE", "0.1516"],
                                ["앵커 WMAPE", "0.1619"],
                                ["차이 (현행−후보)", "-0.0098"],
                                ["시드 편차x2", "0.0029"]],
                },
                {
                    "level": "이상",
                    "title": "채택하지 않습니다 — 나빠지는 품목이 있습니다",
                    "advice": "", "detail": "", "numbers": [],
                },
            ],
        },
        "body": "재학습검증 · 2026-09-16 09:09:05",
    }


def test_성능_답_맨_위에_현재_모델_표가_온다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """★ 09-15 저녁 소매가 모델이 바뀌었는데 답에 그 사실이 없었다.

    이름은 교체해도 그대로라(매입 필터가 이름 일치) **이름만으로는 알 수 없다** —
    만든 날과 학습 끝이 같이 있어야 «지금 무엇이 도는가» 가 보인다.
    """
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    모델도구를_갈아_끼운다(found=_models())
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))

    assert "**현재 모델**" in out.markdown
    #   ★ 모델 이름은 **사용자가 보여 달라고 한 것**이다 — 코드 이름 금지에서 뺀다
    for 이름 in ("ops_auc", "ops_whsl", "ops_rtl"):
        assert 이름 in out.markdown
    assert "2026-09-12" in out.markdown                      # 소매가를 만든 날
    assert "2025-12-31" in out.markdown                      # 학습 끝
    assert "2026-09-15 20:31" in out.markdown                # 최근 교체
    #   ★ 꼬리말(`note`)은 답에 안 적는다 (2026-09-16 · 결정 ⑥). 원문은 DB 에 있고,
    #     그것이 말하려던 «시각이 추정이다» 는 «(시각 미상)» 표시가 대신한다.
    assert "사람이 눌러 바꿈" not in out.markdown
    #   맨 위다 — 봉인 개봉 표보다 앞선다
    assert out.markdown.index("현재 모델") < out.markdown.index(qa_tools.SEALED_SOURCE)


def test_교체_이력_표가_없어도_안_죽고_없다고_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """🔴 `model_cutover` 는 아직 없을 수 있다 (2026-09-16 실측: 두 창고 다 없음)."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    모델도구를_갈아_끼운다()
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))
    assert "교체 이력 없음" in out.markdown
    assert "ops_rtl" in out.markdown                         # 이름·만든 날은 그대로 나간다
    assert "2026-09-12" in out.markdown


def test_현재_모델을_못_읽어도_성능표는_나간다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """🔴 한 갈래가 터져도 나머지는 나간다 — 같은 규칙을 표 하나에도 건다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    모델도구를_갈아_끼운다(boom=True)
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))
    assert "현재 모델을 읽지 못했습니다" in out.markdown
    assert qa_tools.SEALED_SOURCE in out.markdown


def test_그날_재학습_보고서를_전부_보여준다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """🔴 그날 판정 3건 · 검증 2건인데 각 1건만 보였다.

    그래서 **경락가 검증의 «후보가 나쁩니다» 가 답에서 통째로 빠졌다.**
    """
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={
        "재학습판정": [_judge_report(213, "auc", 1), _judge_report(214, "auc", 2),
                   _judge_report(216, "rtl", 2)],
        "재학습검증": [_verify_auc_report(), _verify_report()],
    })
    모델도구를_갈아_끼운다()
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="재학습 후보 있어?", as_of=date(2026, 9, 16)))

    #   빠져 있던 것 — 경락가 검증
    assert "후보가 **나쁩니다**" in out.markdown
    assert "0.1419" in out.markdown and "0.1516" in out.markdown
    assert "채택하지 않습니다 — 나빠지는 품목이 있습니다" in out.markdown
    #   소매가 검증도 그대로
    assert "0.0725" in out.markdown
    assert "채택하지 않습니다 — 나아진 것을 증명 못 했습니다" in out.markdown
    #   판정 3건이 한 줄씩
    assert out.markdown.count("재학습 후보 1개") == 1
    assert out.markdown.count("재학습 후보 2개") == 2


def test_판정은_한_줄_요약표로_적고_문구를_그대로_쓴다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """★ 판정은 길어서 요약표다 — 다만 **판정 문구는 글자 그대로** 옮긴다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={"재학습판정": [_judge_report(216, "rtl", 2)]})
    모델도구를_갈아_끼운다()
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="재학습 판정 어때?", as_of=date(2026, 9, 16)))
    assert "| 가격 | 시각 | 판정 | 후보 |" in out.markdown
    assert "| 소매가 |" in out.markdown                      # 제목 맨 앞 낱말로 가렸다
    assert "이상" in out.markdown
    assert "재학습 후보 2개" in out.markdown
    #   판정은 요약이라 내부 문구를 안 펼친다
    assert "1주 연속 앵커에 밀렸습니다" not in out.markdown


def test_학습_끝이_같으면_견줄_새_후보가_없다고_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """★ 09-16 소매가 검증이 그 경우다 — 현행도 후보도 2025-12-31 이고 WMAPE 가 같다.

    🔴 그래도 «판정 불가» 를 «문제 없음» 으로 바꾸지 않는다.
    """
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={"재학습검증": [_verify_report()]})
    모델도구를_갈아_끼운다()
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="재학습 검증 어때?", as_of=date(2026, 9, 16)))
    assert "현행과 후보의 학습 끝이 같습니다" in out.markdown
    assert "교체 직후라 견줄 새 후보가 없습니다" in out.markdown
    assert "판정 불가" in out.markdown
    assert "문제 없음" not in out.markdown


def test_학습_끝이_다르면_그_줄이_안_붙는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """경락가 검증은 현행 2023-12-31 · 후보 2025-12-31 이라 견줄 것이 있다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={"재학습검증": [_verify_auc_report()]})
    모델도구를_갈아_끼운다()
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="재학습 검증 어때?", as_of=date(2026, 9, 16)))
    assert "현행과 후보의 학습 끝이 같습니다" not in out.markdown


def test_보고서_가격_종류는_payload_kind_를_먼저_본다():
    """★ 다른 일꾼이 `kind` 칸을 붙이면 그것을, 아니면 제목 맨 앞 낱말을 본다."""
    붙은_것 = {"payload": {"kind": "WHSL", "findings": [{"title": "배추 — 판정 불가"}]}}
    assert qa_graph._report_kind(붙은_것) == "중도매가"

    제목만 = {"payload": {"findings": [{"title": "rtl 무 — 재학습 후보"}]}}
    assert qa_graph._report_kind(제목만) == "소매가"

    #   🔴 둘 다 없으면 **지어내지 않는다** (검증 보고서가 실제로 이렇다)
    아무것도 = {"payload": {"findings": [{"title": "배추 — 판정 불가"}]}}
    assert qa_graph._report_kind(아무것도) == "(가격 종류 미상)"


# ── 품목·가격 종류를 안 말하면 전부 답한다 (2026-09-16 · 사용자 지시) ──────


def test_품목을_안_말하면_배추_무_양파를_전부_답한다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": "AUC",
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="경락가 알려줘"))
    assert out.meta.status == "OK"
    assert out.meta.items == ["배추", "무", "양파"]
    assert out.meta.kinds == ["AUC", "AUC", "AUC"]
    assert "품목을 말씀하지 않으셔서" in out.markdown


def test_둘_다_없으면_아홉_조합을_답하고_한_줄로_밝힌다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": None,
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="가격 알려줘"))
    assert out.meta.status == "OK"
    assert len(out.meta.items) == 9 and len(out.meta.kinds) == 9
    assert "품목과 가격 종류를 말씀하지 않으셔서" in out.markdown
    #   군더더기 설명은 그 한 줄뿐 — 조합을 늘어놓는 줄은 안 쓴다
    assert "를 모두 보여드립니다" not in out.markdown


def test_짝_물음이_있으면_기본값을_안_쓴다(도구를_갈아_끼운다, monkeypatch):
    """★ 짝에 이미 품목·가격이 있다. 거기에 전부를 끼얹으면 안 물어본 것이 나간다."""
    도구를_갈아_끼운다(rows=[_row(5)])
    _llm(monkeypatch, {
        "route": "forecast", "item": None, "kind": None, "dates": [],
        "asks": [{"item": "배추", "kind": "AUC", "dates": [BASE + timedelta(days=5)]}],
    })
    out = qa_graph.answer(QaRequest(question="5일 뒤 배추 경락가"))
    assert out.meta.items == ["배추"] and out.meta.kinds == ["AUC"]
    assert "말씀하지 않으셔서" not in out.markdown


def test_범위_밖_품목은_전부로_안_바꾼다(도구를_갈아_끼운다, monkeypatch):
    """🔴 「대파」를 물었는데 배추·무·양파 아홉 표가 나가면 물은 것과 다른 답이다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    _llm(monkeypatch, {"route": "forecast", "item": "대파", "kind": None, "dates": []})
    out = qa_graph.answer(QaRequest(question="대파 가격 알려줘"))
    assert out.meta.status == "OUT_OF_SCOPE"
    assert "대파" in out.markdown
    assert "| 날짜 | 예측 |" not in out.markdown




def test_긴_교체_꼬리말은_답에_안_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """🔴 2026-09-16 실측 — 실제 `note` 가 300자가 넘어 칸 하나가 표를 늘려 버렸다.

    ★ **두 번 고쳤다.** 처음에는 표 밖 한 줄로 내렸는데(«> 경락가 교체 — …»),
      그래도 답이 꼬리말 세 줄로 덮였다. 지금은 **아예 안 적는다** —
      꼬리말이 말하려던 «시각이 추정이다» 는 «(시각 미상)» 표시가 대신하고,
      원문은 `model_cutover.note` 에 그대로 있다. **지운 것이 아니라 옮긴 것이다.**
    """
    긴_꼬리말 = (
        "★ 추정 — 폴더 이름의 날짜는 확실하나 시각은 모름. st_ctime 이 이름의 날짜와 "
        "달라 쓸 수 없음 (이름 바꾸기로 만든 백업은 옛 번들의 만든 시각을 물려받음) · "
        "백필 · 새 번들 출처=지금 꽂혀 있는 번들 · 폴더 st_ctime=2026-09-03 10:42:38"
    )
    found = _models()
    for row in found["models"]:
        row["last_swap_note"] = 긴_꼬리말
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    모델도구를_갈아_끼운다(found=found)
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))

    assert 긴_꼬리말 not in out.markdown
    assert "> 경락가 교체 —" not in out.markdown
    #   표는 그대로다 — 날짜·시각은 여전히 보인다
    표_줄 = [line for line in out.markdown.splitlines() if line.startswith("| 경락가 |")]
    assert len(표_줄) == 1
    assert "2026-09-15 20:31" in out.markdown


# ── 질문 없이 부르는 입구도 같은 규칙 (2026-09-16 · 사용자 결정 ④) ──────────


def test_질문_없이_품목만_줘도_가격_셋을_다_답한다(도구를_갈아_끼운다):
    """★ 채팅 입구와 **같은 규칙**이다. 입구가 다르다고 답이 달라지면 안 된다.

    전에는 «가격 종류를 알 수 없습니다» 로 되물었다. 되묻는 쪽이 부르는 코드에는
    더 나쁘다 — 사람이 아니라 프로그램이라 다시 물어볼 수가 없다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    out = qa_graph.answer(QaRequest(item="배추", dates=[BASE + timedelta(days=1)]))
    assert out.meta.status == "OK"
    assert out.meta.items == ["배추", "배추", "배추"]
    assert out.meta.kinds == ["AUC", "WHSL", "RTL"]
    assert "가격 종류를 말씀하지 않으셔서" in out.markdown


def test_질문_없이_가격만_줘도_품목_셋을_다_답한다(도구를_갈아_끼운다):
    도구를_갈아_끼운다(rows=[_row(1)])
    out = qa_graph.answer(QaRequest(kind="AUC", dates=[BASE + timedelta(days=1)]))
    assert out.meta.status == "OK"
    assert out.meta.items == ["배추", "무", "양파"]
    assert set(out.meta.kinds) == {"AUC"}
    assert "품목을 말씀하지 않으셔서" in out.markdown


def test_질문도_품목도_없으면_아홉_조합을_답한다(도구를_갈아_끼운다):
    """★ 「아무것도 못 정한 경우」는 **해석기가 clarify 를 낸 때**뿐이다.

    값을 하나도 안 주고 부른 것은 «아무 조건 없이 오늘 값 전부» 로 읽는다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])
    out = qa_graph.answer(QaRequest(dates=[BASE + timedelta(days=1)]))
    assert out.meta.status == "OK"
    assert len(out.meta.items) == 9 and len(out.meta.kinds) == 9
    assert "품목과 가격 종류를 말씀하지 않으셔서" in out.markdown


def test_질문_없이_준_품목이_틀리면_여전히_거절한다(도구를_갈아_끼운다):
    """🔴 **범위 밖은 전부로 안 바꾼다.** 「대파」를 물었는데 배추가 나가면 다른 답이다."""
    도구를_갈아_끼운다(rows=[_row(1)])
    out = qa_graph.answer(QaRequest(item="대파"))
    assert out.meta.status == "OUT_OF_SCOPE"
    assert "대파" in out.markdown
    assert "| 날짜 | 예측 |" not in out.markdown


# ── 해석기 지시문 (2026-09-16 · 사용자 결정 ⑤) ──────────────────────────


def test_지시문이_품목_가격_날짜를_비워_두라고_말한다():
    """★ 규칙이 바뀌었으면 **해석기에게도 말해야** 한다.

    코드가 «없으면 전부» 로 바뀌었는데 지시문이 그대로면, 해석기는 계속 clarify 를
    내거나 안 물어본 품목을 채운다. 두 벌(한국어·영어)의 **뜻이 같아야** 한다 —
    지시문 언어만 다른 짝이라야 `qa_prompt_bench` 로 견줄 수 있다.
    """
    for 지시문 in (qa_llm.SYSTEM_PROMPT_KO, qa_llm.SYSTEM_PROMPT_EN):
        assert "되묻지" in 지시문 or "ask back" in 지시문
        assert "비워" in 지시문 or "leave" in 지시문.lower()
    #   한국어판에는 우리 말로 못 박아 둔다
    assert "되묻지 마라" in qa_llm.SYSTEM_PROMPT_KO
    assert "clarify 를 쓰지 마라" in qa_llm.SYSTEM_PROMPT_KO
    #   ★ 스키마의 clarify 값은 **남긴다** — 정말 아무것도 못 알아들은 질문이 있다
    assert "clarify" in qa_llm._RESPONSE_SCHEMA["properties"]["routes"]["items"]["enum"]


# ── 교체 시각 미상 (2026-09-16 · 사용자 결정 ⑥) ─────────────────────────


def test_교체_시각이_미상이면_날짜만_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """🔴 백필한 교체 시각은 **폴더 이름에서 되짚은 것**이라 시각이 없는 행이 있다.

    `00:00` 을 그대로 보이면 «한밤중에 바꿨나» 로 읽힌다 — 실제로는 «모른다» 다.
    """
    found = _models()
    for row in found["models"]:
        row["last_swap_time_known"] = False
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    모델도구를_갈아_끼운다(found=found)
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))
    assert "2026-09-15 (시각 미상)" in out.markdown
    assert "20:31" not in out.markdown


def test_교체_시각_칸이_없으면_지금처럼_날짜와_시각을_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, 모델도구를_갈아_끼운다, monkeypatch
):
    """★ 루트 쪽이 `time_known` 을 아직 안 붙였을 수 있다 (2026-09-16 실측: 없다).

    `SELECT *` 로 읽으니 칸이 없으면 열쇠가 아예 안 온다 — 그때는 예전 그대로다.
    """
    found = _models()
    for row in found["models"]:
        row.pop("last_swap_time_known", None)
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    모델도구를_갈아_끼운다(found=found)
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=date(2026, 9, 16)))
    assert "2026-09-15 20:31" in out.markdown
    assert "시각 미상" not in out.markdown


# ── «전부 써도 되나» 한 칸 (2026-09-16 · 되물음 1 결정 «가») ────────────────


def test_한_조합이라도_막히면_전부_쓰지_말라고_적는다(도구를_갈아_끼운다, monkeypatch):
    """🔴 「가격 알려줘」에 아홉 조합이 나가는데 **첫 조합만** 보고 있었다.

    양파 중도매가는 막힌 조합인데 `meta.use_recommended` 는 첫 조합(배추 경락가)의
    `True` 를 내보냈다. 답 문장에서 «쓰지 마세요» 를 뺀 뒤로는 그 사실이
    **어디에도 안 남았다** — 마스터가 못 쓰는 값을 쓰게 되는 자리다.

    ★ **답 문장은 한 글자도 안 바뀐다.** 바뀌는 것은 이 칸 하나의 뜻이다.
    """
    도구를_갈아_끼운다(rows=[_row(1)])

    def 양파_중도매가만_막혔다(item, kind):
        if (item, kind) == ("양파", "WHSL"):
            return {"use_recommended": False, "quality_note": "세 구간 중 1개만 통과"}
        return {"use_recommended": True}

    monkeypatch.setattr(qa_graph.qa_tools, "usability", 양파_중도매가만_막혔다)
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": None,
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="가격 알려줘"))

    assert len(out.meta.items) == 9
    assert out.meta.use_recommended is False              # 하나라도 막혔으면 False
    #   🔴 답 문장은 그대로다 — 경고 줄도, 막힌 조합의 표도 예전과 같다
    assert "쓰지 마세요" not in out.markdown
    assert "세 구간 중 1개만 통과" not in out.markdown
    assert out.markdown.count("| 날짜 | 예측 | 예상 구간 | 비고 |") == 9


def test_전부_써도_되면_참으로_적는다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[_row(1)], usab={"use_recommended": True})
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": None,
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="가격 알려줘"))
    assert len(out.meta.items) == 9
    assert out.meta.use_recommended is True


def test_조합이_하나면_그_조합_값을_그대로_쓴다(도구를_갈아_끼운다):
    """★ 예전과 같아야 한다 — 뜻을 넓혔다고 이미 나가던 답이 달라지면 안 된다."""
    도구를_갈아_끼운다(rows=[_row(1)], usab={"use_recommended": False})
    out = qa_graph.answer(QaRequest(item="양파", kind="WHSL"))
    assert out.meta.use_recommended is False

    도구를_갈아_끼운다(rows=[_row(1)], usab={"use_recommended": True})
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    assert out.meta.use_recommended is True

    #   🔴 **«모른다» 를 «안 막혔다» 로 바꾸지 않는다.** 행이 없으면 None 그대로다
    도구를_갈아_끼운다(rows=[_row(1)], usab={})
    out = qa_graph.answer(QaRequest(item="배추", kind="AUC"))
    assert out.meta.use_recommended is None


def test_막힌_것을_모르는_조합이_섞이면_단정하지_않는다(도구를_갈아_끼운다, monkeypatch):
    """★ 하나는 «써도 된다» 이고 하나는 «모른다» 면 **«전부 써도 된다» 가 아니다.**"""
    도구를_갈아_끼운다(rows=[_row(1)])

    def 양파만_모른다(item, kind):
        return {} if item == "양파" else {"use_recommended": True}

    monkeypatch.setattr(qa_graph.qa_tools, "usability", 양파만_모른다)
    _llm(monkeypatch, {"route": "forecast", "item": None, "kind": "AUC",
                       "dates": [BASE + timedelta(days=1)]})
    out = qa_graph.answer(QaRequest(question="경락가 알려줘"))
    assert out.meta.use_recommended is None


# ── 지난 날을 묻는다 · 기록이 없는 날은 «고장» 이 아니다 (2026-09-16) ──────────
#
# 🔴 두 가지가 한 사고에서 나왔다. 화면 기준일 2026-08-03 에 «오늘 배치 상태» 를
#    물으니 답은 «그날 배치 기록이 없습니다» 로 맞았는데, 상태가 `NO_DATA` 라
#    어댑터가 «창고를 못 쓴다» 로 올렸고 마스터가 그 답을 버렸다.
#    **기록이 없는 날은 고장이 아니다.**


AS_OF = date(2026, 9, 16)


def _날짜를_적어_둔다(monkeypatch, *, runs=None, reports=None, retrains=None):
    """배치·보고서 도구가 **어느 날을 읽었는지** 적어 둔다. 값은 날짜별로 준다."""
    seen: dict[str, list[date]] = {"batch": [], "report": [], "retrain": []}
    runs = runs or {}
    reports = reports or {}
    retrains = retrains or {}

    def _batch_run(on):
        seen["batch"].append(on)
        return runs.get(on)

    def _agent_report(name, on):
        seen["report"].append(on)
        return reports.get(on)

    def _agent_reports(name, on):
        seen["retrain"].append(on)
        return list(retrains.get(on) or [])

    monkeypatch.setattr(qa_graph.qa_tools, "batch_run", _batch_run)
    monkeypatch.setattr(qa_graph.qa_tools, "failed_stages", lambda run_id: [])
    monkeypatch.setattr(qa_graph.qa_tools, "agent_report", _agent_report)
    monkeypatch.setattr(qa_graph.qa_tools, "agent_reports", _agent_reports)
    return seen


def test_기록이_없는_날은_기준일을_적어_말한다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """★ 사용자 결정 ① — 이 문장이 화면에 **그대로** 나가야 한다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(run=None, reports={})
    _routes(monkeypatch, ["batch"])
    out = qa_graph.answer(
        QaRequest(question="오늘 배치 상태 알려줘", as_of=date(2026, 8, 3))
    )
    assert "기준일(2026-08-03)에 대한 배치 기록이 없습니다" in out.markdown
    assert "기준일(2026-08-03)에 대한 점검 보고서도 아직 없습니다" in out.markdown
    #   ★ 어댑터가 갈래별로 가릴 수 있게 **읽은 결과를 표 이름으로** 넘긴다
    assert out.reads == {"batch_run": "empty", "agent_report": "empty"}


def test_재학습_기록이_없는_날도_같은_꼴로_적는다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """🔴 봉인 성능표·현재 모델 표는 **그대로 나간다** — 없는 것은 그날 판정뿐이다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    _routes(monkeypatch, ["perf"])
    out = qa_graph.answer(QaRequest(question="모델 성능 어때?", as_of=AS_OF))
    assert "기준일(2026-09-16)에 대한 재학습 판정 기록이 없습니다" in out.markdown
    assert qa_tools.SEALED_SOURCE in out.markdown
    assert "19.7" in out.markdown


def test_어제_배치를_물으면_어제_기록을_읽는다(도구를_갈아_끼운다, monkeypatch):
    """★ 사용자 결정 ② — 배치·성능은 **지나간 날**을 받는다."""
    도구를_갈아_끼운다(rows=[])
    어제 = AS_OF - timedelta(days=1)
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["batch"], dates=[어제])
    out = qa_graph.answer(QaRequest(question="어제 배치 상태 알려줘", as_of=AS_OF))
    assert seen["batch"] == [어제]
    assert "배치 — 2026-09-15" in out.markdown
    assert "2026-09-16" not in out.markdown


def test_가격_날짜만_있는_배치는_오늘을_본다(도구를_갈아_끼운다, monkeypatch):
    """🔴 «5일 뒤 배추 경락가랑 배치 상태» 의 배치는 **오늘**이다 — 날짜를 안 말했다.

    ★ **해석기가 둘을 갈라 준다** (2026-09-16 · 실제 응답 2건으로 확인).
      같은 모델·같은 지시문에 두 질문을 넣어 받은 것이 이랬다 —

      ```text
      「내일 배치 알려줘」            dates=["2026-09-17"] · asks=[]
      「5일 뒤 배추 경락가랑 배치 상태」 dates=[]             ·
                                     asks=[{배추, AUC, ["2026-09-21"]}]
      ```

      **가격에 붙은 날은 `asks` 안에만 들어온다.** 배치·성능이 읽는 `asked` 는
      맨 위 `dates` 뿐이라(`supervise`), 짝 물음의 날짜는 여기까지 오지 않는다.
      그래서 이 질문의 배치 쪽은 «날짜를 안 말한 것» 이고 오늘을 본다.
    """
    도구를_갈아_끼운다(rows=[_row(5)], base=BASE)
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["forecast", "batch"], items=["배추"], kinds=["AUC"],
            dates=[], asks=[{"item": "배추", "kind": "AUC",
                             "dates": [BASE + timedelta(days=5)]}])
    out = qa_graph.answer(
        QaRequest(question="5일 뒤 배추 경락가랑 배치 상태", as_of=AS_OF)
    )
    assert seen["batch"] == [AS_OF]
    assert qa_graph.AHEAD_BATCH not in out.markdown


# ── 앞날을 콕 집어 물으면 기준일 뒤는 안 보여준다 (2026-09-16) ────────────────
#
# 🔴 화면에서 «내일 배치 알려줘» 에 **오늘 배치와 오늘 점검 보고서**가 나왔다.
#    `_report_days` 가 지난 날만 남기고, 남는 게 없으면 «오늘 하루» 로 돌아갔다.
#    그 규칙은 **날짜를 안 말한 질문**을 위한 것인데 콕 집은 앞날까지 오늘로 바꿨다.
#
# ★ «아직 안 돌았다» 고 적지 않는다 (사용자 결정). 화면 기준일은 진짜 오늘보다
#   과거일 수 있다 — 기준일을 09-14 로 두면 09-15 기록은 **DB 에 있는데** 앞날이다.
#   그러면 «아직 안 돌았다» 는 거짓이 된다. 맞는 말은 «기준일 뒤는 안 보여준다» 다.


def test_앞날_배치는_기준일_뒤는_못_본다고_한_줄로_말한다(도구를_갈아_끼운다, monkeypatch):
    """🔴 오늘 것을 **대신 보여주지 않는다.** 조용히 다른 날을 내미는 것이다."""
    도구를_갈아_끼운다(rows=[])
    내일 = AS_OF + timedelta(days=1)
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["batch"], dates=[내일])
    out = qa_graph.answer(QaRequest(question="내일 배치 알려줘", as_of=AS_OF))

    assert out.markdown.strip() == qa_graph.AHEAD_BATCH
    assert seen["batch"] == [] and seen["report"] == []       # 읽으러 가지도 않는다
    assert "배치 — " not in out.markdown                      # 오늘 표가 없다
    assert "점검 보고서" not in out.markdown
    #   ★ 상태는 «기록 없음» 과 같은 길이다 — 어댑터가 READY·skipped 로 낸다
    assert out.meta.status == "NO_DATA"
    assert out.reads == {"batch_run": "empty", "agent_report": "empty"}


def test_앞날_배치도_어댑터는_고장이_아니라고_낸다(도구를_갈아_끼운다, monkeypatch):
    """🔴 `RUNTIME_NOT_READY` 로 올리면 마스터가 이 한 줄을 **버린다.**"""
    from app.master import envelope as E
    from app.ml import adapter

    도구를_갈아_끼운다(rows=[])
    _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["batch"], dates=[AS_OF + timedelta(days=1)])
    답 = qa_graph.answer(QaRequest(question="내일 배치 알려줘", as_of=AS_OF))
    monkeypatch.setattr(adapter, "qa_answer", lambda _request: 답)

    modes = dict(E._AGENT_MODES)
    modes["ml"] = frozenset({"STATUS_QUERY"})
    monkeypatch.setattr(E, "_AGENT_MODES", modes)
    request = E.AgentRequest(
        context=E.ExecutionContext(request_id="req-1", as_of=AS_OF,
                                   trigger="USER_REQUEST", policy_version="v1"),
        agent="ml",                                          # type: ignore[arg-type]
        mode="STATUS_QUERY",                                 # type: ignore[arg-type]
        payload={"question": "내일 배치 알려줘"},
    )
    reply, meta = adapter.ml_port(request)
    assert reply.runtime_status == "READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ()
    assert reply.payload["answer_markdown"] == 답.markdown
    assert E.validate_reply(request, reply, meta) == ()


def test_어제랑_내일을_같이_물으면_어제는_답하고_앞날은_한_줄로_밝힌다(
    도구를_갈아_끼운다, monkeypatch
):
    """★ 날마다 따로 본다. 앞날 안내는 **맨 끝에 한 번만**."""
    도구를_갈아_끼운다(rows=[])
    어제, 내일 = AS_OF - timedelta(days=1), AS_OF + timedelta(days=1)
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["batch"], dates=[어제, 내일])
    out = qa_graph.answer(QaRequest(question="어제랑 내일 배치 알려줘", as_of=AS_OF))

    assert seen["batch"] == [어제]
    assert "배치 — 2026-09-15" in out.markdown
    assert "배치 — 2026-09-17" not in out.markdown
    assert out.markdown.count(qa_graph.AHEAD_BATCH) == 1
    assert out.markdown.rstrip().endswith(qa_graph.AHEAD_BATCH)


def test_앞날_재학습도_같은_규칙이고_봉인_성능표는_그대로다(
    도구를_갈아_끼운다, 배치도구를_갈아_끼운다, monkeypatch
):
    """★ 봉인 성능표·현재 모델은 **날짜와 무관하다** — 그건 늘 지금 것이다."""
    도구를_갈아_끼운다(rows=[])
    배치도구를_갈아_끼운다(reports={})
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["perf"], dates=[AS_OF + timedelta(days=1)])
    out = qa_graph.answer(QaRequest(question="내일 재학습 어때?", as_of=AS_OF))

    assert seen["retrain"] == []
    assert qa_graph.AHEAD_PERF in out.markdown
    assert "재학습 판정 기록이 없습니다" not in out.markdown
    assert qa_tools.SEALED_SOURCE in out.markdown            # 봉인 성능표는 그대로
    assert "19.7" in out.markdown


def test_날짜가_여럿이면_날마다_블록을_만든다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[])
    날들 = [AS_OF - timedelta(days=n) for n in (2, 1, 0)]
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["batch"], dates=list(reversed(날들)))
    out = qa_graph.answer(QaRequest(question="사흘치 배치 어땠어", as_of=AS_OF))
    assert seen["batch"] == 날들
    for 날 in ("2026-09-14", "2026-09-15", "2026-09-16"):
        assert f"배치 — {날}" in out.markdown


def test_이레를_넘기면_최근_이레만_보여주고_밝힌다(도구를_갈아_끼운다, monkeypatch):
    """★ 열흘치를 다 펼치면 답이 배치 표로 덮인다. **자르되 잘랐다고 말한다.**"""
    도구를_갈아_끼운다(rows=[])
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["batch"],
            dates=[AS_OF - timedelta(days=n) for n in range(10)])
    out = qa_graph.answer(QaRequest(question="열흘치 배치", as_of=AS_OF))
    assert len(seen["batch"]) == 7
    assert seen["batch"][-1] == AS_OF
    assert seen["batch"][0] == AS_OF - timedelta(days=6)
    assert "최근 7일" in out.markdown


def test_성능도_같은_날을_읽는다(도구를_갈아_끼운다, monkeypatch):
    도구를_갈아_끼운다(rows=[])
    어제 = AS_OF - timedelta(days=1)
    seen = _날짜를_적어_둔다(monkeypatch)
    _routes(monkeypatch, ["perf"], dates=[어제])
    out = qa_graph.answer(QaRequest(question="어제 재학습 어땠어", as_of=AS_OF))
    assert set(seen["retrain"]) == {어제}
    assert "기준일(2026-09-15)에 대한 재학습 판정 기록이 없습니다" in out.markdown


def test_지난_날의_가격은_범위_밖이라고_말한다(도구를_갈아_끼운다, monkeypatch):
    """🔴 가격 갈래는 **앞날만** 답한다. 지난 날을 주면 예전처럼 «범위 밖» 이다."""
    도구를_갈아_끼운다(rows=[])
    _routes(monkeypatch, ["forecast"], items=["배추"], kinds=["AUC"],
            dates=[BASE - timedelta(days=1)])
    out = qa_graph.answer(QaRequest(question="어제 배추 경락가 얼마였어?"))
    assert out.meta.status == "OUT_OF_SCOPE"
    assert "예측 범위 밖" in out.markdown


def test_해석기가_지난_날도_고를_수_있다():
    """★ 목록에 지난 30일을 더한다 — «9월 10일» 을 오프셋으로 환산하게 두지 않는다."""
    schema = qa_llm._schema(BASE)
    days = schema["properties"]["dates"]["items"]["enum"]
    assert (BASE - timedelta(days=1)).isoformat() in days
    assert (BASE - timedelta(days=30)).isoformat() in days
    assert (BASE + timedelta(days=18)).isoformat() in days
    assert (BASE - timedelta(days=31)).isoformat() not in days
    #   🔴 짝 물음(asks)은 **가격**이라 앞날만 고른다
    ask_days = schema["properties"]["asks"]["items"]["properties"]["dates"]["items"]["enum"]
    assert (BASE - timedelta(days=1)).isoformat() not in ask_days
    assert BASE.isoformat() in ask_days


def test_지시문이_지난_날을_어떻게_고를지_말해_준다():
    prompt = qa_llm._prompt(BASE)
    for 낱말 in ("어제", "그저께", "지난 날"):
        assert 낱말 in prompt, 낱말
