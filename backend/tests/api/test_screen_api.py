"""화면용 API — 여섯 탭이 계약대로 값을 내는가.

★ **이 테스트가 있는 이유.** 부서가 자기 `query.py` 를 채울 때, 응답 모양을
  같이 바꿔 버리면 화면이 **조용히 빈 칸**이 됩니다. 오류가 안 나서 아무도
  모릅니다. 그래서 모양만 여기서 잡아 둡니다.

★ **값은 안 봅니다. 모양만 봅니다.** 부서가 예시값을 실제 값으로 바꾸면
  숫자는 당연히 달라집니다. 숫자를 잡으면 채우는 사람이 테스트를 지우게
  되고, 그러면 검사가 사라집니다.

★ 라우터만 격리해 띄웁니다 — `app.main` 전체는 psycopg 를 요구합니다
  (`tests/master/test_master_api.py` 와 같은 이유).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import router

AS_OF = "2026-01-06"
FIN_AS_OF = "2025-12-31"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


#: (주소, 파라미터). 부서가 탭을 늘리면 여기에 한 줄 더합니다.
TABS = [
    ("/api/dashboard", {"as_of": AS_OF}),
    ("/api/forecast", {"as_of": AS_OF, "item": "배추"}),
    ("/api/purchase", {"as_of": AS_OF}),
    ("/api/finance", {"as_of": FIN_AS_OF, "state": "base"}),
    ("/api/logistics", {"as_of": AS_OF, "pane": "stock"}),
    ("/api/sales", {"as_of": FIN_AS_OF}),
]


@pytest.mark.parametrize(("path", "params"), TABS)
def test_탭이_열린다(client, path, params):
    """여섯 탭 모두 200 이어야 한다. 하나라도 500 이면 그 화면은 통째로 빈다."""
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(("path", "params"), TABS)
def test_쓰기는_막혀_있다(client, path, params):
    """`/api` 아래는 **읽기만** 한다.

    쓰기는 기존 경로(`POST /master/…/decision`)가 한다. 여기에 POST 가 생기면
    같은 일을 두 군데서 하게 되고, «어느 쪽으로 승인했나» 가 기록에서 갈린다.
    """
    assert client.post(path, params=params).status_code == 405


def test_없는_값은_400_이지_500_이_아니다(client):
    """무엇을 잘못 넣었는지 **문장으로** 알려줘야 한다."""
    for path, params, bad in [
        ("/api/forecast", {"as_of": AS_OF}, {"item": "딸기"}),
        ("/api/finance", {"as_of": FIN_AS_OF}, {"state": "없는상태"}),
        ("/api/logistics", {"as_of": AS_OF}, {"pane": "없는화면"}),
    ]:
        response = client.get(path, params={**params, **bad})
        assert response.status_code == 400, response.text
        assert "가능:" in response.json()["detail"]


def test_예시값인지_아닌지를_반드시_밝힌다(client):
    """`Source.filled` 가 없으면 화면이 「예시값」 딱지를 못 붙인다.

    딱지가 없으면 보는 사람이 **데모 숫자를 실적으로 읽습니다.**
    """
    for path, params in TABS:
        body = client.get(path, params=params).json()
        sources = body.get("sources") or [body.get("source")]
        assert sources and all(s is not None for s in sources), path
        for source in sources:
            assert isinstance(source["filled"], bool), path
            assert source["owner"], path


def test_그래프_계열은_날짜축과_길이가_같다(client):
    """★ 길이가 어긋나면 선이 **엉뚱한 날짜에 그려진다.**

    화면은 칸 번호로만 위치를 잡는다. 계열이 짧으면 조용히 왼쪽으로 밀린다 —
    오류가 안 나서 «그날 값이 그랬구나» 로 읽히는 것이 제일 위험하다.
    """
    body = client.get("/api/dashboard", params={"as_of": AS_OF}).json()
    n = len(body["axis"]["days"])
    for key in ("cash_chart", "stock_chart"):
        for series in body[key]["series"]:
            assert len(series["data"]) == n, f"{key} · {series['name']}"


def test_눈금_글자를_주면_눈금_수와_같아야_한다(client):
    """`y_labels` 는 `y_ticks` 와 짝이다. 짧으면 아래쪽 눈금이 글자를 잃는다."""
    body = client.get("/api/dashboard", params={"as_of": AS_OF}).json()
    for key in ("cash_chart", "stock_chart"):
        chart = body[key]
        if chart["y_labels"]:
            assert len(chart["y_labels"]) == len(chart["y_ticks"]), key


def test_대시보드는_숫자를_만들지_않는다(client):
    """대시보드 값은 **부서 탭의 값과 같아야** 한다.

    같은 값을 두 군데서 계산하면 언젠가 갈라진다. 실제로 갈라졌었다 —
    재고 요약이 4,550kg 인데 그래프 끝은 14,600kg 이었다.
    """
    dash = client.get("/api/dashboard", params={"as_of": AS_OF}).json()
    stock_stat = next(s for s in dash["stats"] if "재고" in s["label"])
    last_actual = [v for v in dash["stock_chart"]["series"][0]["data"] if v is not None][-1]
    assert stock_stat["raw"] == last_actual

    logistics = client.get("/api/logistics", params={"as_of": AS_OF, "pane": "stock"}).json()
    pane = next(p for p in logistics["panes"] if p["key"] == "stock")
    assert stock_stat["raw"] == pane["stats"][0]["raw"]

    forecast = client.get("/api/forecast", params={"as_of": AS_OF, "item": "배추"}).json()
    assert dash["forecast_cards"] == forecast["cards"]


def test_표의_칸_이름이_행에_있다(client):
    """머리에 있는데 행에 없으면 그 칸은 **통째로 공란**이 된다.

    부서가 `columns` 만 고치고 `rows` 를 안 고치는 실수를 여기서 잡는다.
    """
    checks = [
        ("/api/purchase", {"as_of": AS_OF}, ["committed"]),
        ("/api/finance", {"as_of": FIN_AS_OF, "state": "base"}, ["closings"]),
        ("/api/forecast", {"as_of": AS_OF, "item": "배추"}, ["accuracy", "quality"]),
    ]
    for path, params, keys in checks:
        body = client.get(path, params=params).json()
        for key in keys:
            table = body[key]
            names = {c["key"] for c in table["columns"]}
            for row in table["rows"]:
                missing = names - set(row)
                assert not missing, f"{path} · {key} · 빠진 칸 {missing}"
