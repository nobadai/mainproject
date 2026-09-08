"""파트별 `AGENTS.md` 를 만든다.

    cd backend && uv run python -m app.api._agent_docs

★ **왜 손으로 안 쓰고 만드나.**

  여섯 파트가 각자 CLI AI(Claude Code · Cursor 등)에게 읽히는 문서입니다.
  거기 적힌 칸 이름이 실제 `schema.py` 와 **하나라도 다르면** AI 가 그대로
  만들고, 화면은 조용히 빈 칸이 됩니다 — 오류가 안 나서 아무도 모릅니다.

  그래서 **계약 부분은 스키마에서 뽑습니다.** 스키마를 고치면 이 파일을
  다시 돌리기만 하면 되고, 문서가 코드와 어긋날 길이 없습니다.

  공통 규칙(§부품 · §규칙 · §검증)도 여기 한 벌만 두어, 여섯 문서가
  서로 갈라지지 않게 합니다.

★ **`AGENTS.md` 라는 이름인 이유.** Claude Code · Cursor · Codex 가 그
  폴더에서 일할 때 **묻지 않아도 읽는** 이름입니다. 사람이 읽을 개요는
  `app/api/README.md` 에 따로 있습니다.
"""

from __future__ import annotations

import inspect
import types
import typing
from pathlib import Path

from pydantic import BaseModel

from app.api.dashboard.schema import DashboardTab
from app.api.finance.schema import FinanceTab
from app.api.forecast.schema import ForecastTab
from app.api.logistics.schema import LogisticsTab
from app.api.purchase.schema import PurchaseTab
from app.api.sales.schema import SalesTab

HERE = Path(__file__).parent


# ══════════════════════════════════════════════════════════════════════════
#  스키마 → 표
# ══════════════════════════════════════════════════════════════════════════

def _type_name(annotation: object) -> str:
    """타입을 사람이 읽을 한 줄로. `list[Stat] | None` 같은 모양."""
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        return " | ".join(
            _type_name(a) for a in typing.get_args(annotation) if a is not type(None)
        ) + (" | None" if type(None) in typing.get_args(annotation) else "")
    if origin in (list, set, tuple):
        inner = ", ".join(_type_name(a) for a in typing.get_args(annotation))
        return f"{origin.__name__}[{inner}]"
    if origin is dict:
        k, v = typing.get_args(annotation)
        return f"dict[{_type_name(k)}, {_type_name(v)}]"
    if annotation is type(None):
        return "None"
    if inspect.isclass(annotation):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """이 타입 안에 들어 있는 우리 모델들."""
    found: list[type[BaseModel]] = []
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for arg in typing.get_args(annotation):
        found.extend(_models_in(arg))
    return found


def _required(field) -> str:
    return "필수" if field.is_required() else "선택"


def field_table(model: type[BaseModel]) -> str:
    rows = ["| 칸 | 타입 | 필수 | 무엇 |", "|---|---|---|---|"]
    for name, field in model.model_fields.items():
        desc = (field.description or "").replace("\n", " ").replace("|", "·")
        #  ★ 마크다운 표에서 `|` 는 칸 구분자다. `str | None` 을 그대로 넣으면
        #    표가 깨진다. HTML 엔티티로 바꾼다 — 역슬래시 이스케이프는
        #    파이썬 소스에서 또 한 번 새기 쉬워 실제로 두 번 틀렸다.
        kind = _type_name(field.annotation).replace("|", "&#124;")
        rows.append(f"| `{name}` | `{kind}` | {_required(field)} | {desc} |")
    return "\n".join(rows)


def contract(root: type[BaseModel], skip: set[str]) -> str:
    """탭 모델과, 그 안에서 처음 나오는 우리 모델들을 차례로 편다.

    `skip` 은 §부품 절에 이미 적힌 공용 모델이라 여기서 또 안 적는다.
    """
    out = [f"### `{root.__name__}` — 이 함수가 돌려줄 것\n", field_table(root)]
    seen: set[str] = {root.__name__} | skip
    queue = [
        m
        for f in root.model_fields.values()
        for m in _models_in(f.annotation)
        if m.__name__ not in seen and not seen.add(m.__name__)
    ]
    while queue:
        model = queue.pop(0)
        doc = (model.__doc__ or "").strip().split("\n")[0]
        out.append(f"\n### `{model.__name__}`{' — ' + doc if doc else ''}\n")
        out.append(field_table(model))
        for f in model.model_fields.values():
            for m in _models_in(f.annotation):
                if m.__name__ not in seen:
                    seen.add(m.__name__)
                    queue.append(m)
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════
#  공통 — 여섯 문서가 똑같이 받는 부분
# ══════════════════════════════════════════════════════════════════════════

PRIMITIVES = """\
## 부품 — 이것만 씁니다

전부 `app/api/primitives.py` 에 있습니다. **화면은 이것만 그릴 줄 압니다.**
여기 없는 모양을 만들면 화면이 못 그립니다.

`tone` 은 여섯 중 하나입니다 — `neutral` `good` `warn` `bad` `info` `sim`.
**색이 아니라 뜻입니다.** 색은 화면이 정합니다.

```python
Stat(label="받을 돈", value="7,305", unit="만원",
     detail="매출채권 15건 · 아직 수금 0원",
     tone="warn",
     raw=7305)          # 계산·정렬용 수. 글자(value)와 따로 담는다
```

`value` 는 **사람이 읽을 글자**(자릿점·부호 포함), `raw` 는 **수**입니다.
자릿점과 단위(만원·톤)는 파트마다 다르므로 글자를 그대로 받습니다.

```python
Table(
    columns=[
        Column(key="d",    label="날짜", mono=True),
        Column(key="cash", label="현금", align="right", mono=True),
    ],
    rows=[
        {"d": "2025-12-31", "cash": "-1,328만원"},
        {"d": "2025-12-30", "cash": None},        # 공란. 0 이 아니다
    ],
    empty_text="이 기간에 값이 없습니다",
)
```

- `columns[].key` 와 `rows` 의 키가 **글자까지 같아야** 합니다. 다르면 그 칸이 통째로 빕니다
- `align` 은 `left` `right` `center`. 수치는 `right`
- `mono=True` 는 자릿수를 세로로 맞춰 보고 싶을 때
- `empty_text` 를 꼭 채우세요 — "없음" 과 "아직 안 들어옴" 은 다릅니다

```python
Chart(
    label="12월 일별 현금",           # 읽어주는 도구가 쓸 이름
    y_min=-20, y_max=110,
    y_ticks=[0, 50, 100],
    y_unit="M",                      # 눈금 숫자 뒤에 붙일 글자
    y_labels=[],                     # 눈금 글자를 직접 줄 때 (아래 ★)
    series=[
        Series(name="실적", data=[58.1, 54.0, None, 49.8], tone="info"),
        Series(name="추정", data=[None, None, 49.8, 45.6], dashed=True),
    ],
    bands=[],                        # 예측 구간 (위·아래 두 선 사이를 칠함)
    markers=[],                      # 그날 일어난 일 (지급 · 폐기)
    x_labels=[],                     # 공용 날짜축을 안 쓸 때만
    note=Note(...),
)
```

**★ 값의 단위와 눈금 글자가 다르면 `y_labels` 를 쓰세요.**

```python
y_ticks=[10000, 20000],
y_labels=["10t", "20t"]      # 없으면 "10,000t" 이 되어 틀린다
```

재고는 kg 로 그리는데 눈금은 톤으로 적어야 합니다. **실제로 이렇게 틀렸었습니다.**

```python
Note(tone="warn", text="★ 둘째 칸은 **0 이 아니라 공란**입니다.")
```

`**굵게**` 만 알아듣습니다. 다른 마크다운 표시는 글자 그대로 나옵니다.

```python
Card(
    key="lots",                      # 파트 안에서 안 겹치게
    title="Lot 상태",
    subtitle="Snapshot + turnover 계산 결과",
    source_ref="inventory_lots",     # 오른쪽 위에 작게 — 어느 표를 읽었나
    lead=Note(...),                  # 표보다 먼저 읽을 안내
    flow=["현재고", "예약", "할당"],   # 화살표로 잇는 단계
    stats=[...], table=..., chart=...,
    bullets=["원칙 한 줄", "또 한 줄"],
    footer="표 아래 작은 글",
)
```

**채운 것만 그립니다.** 다 채울 필요 없습니다.
카드를 더 넣어도 **화면은 안 고칩니다** — 목록이라 그냥 늘어납니다.
"""

RULES = """\
## 지켜야 할 것 다섯

번호는 코드 주석이 참조합니다(「6-② 규칙」). **번호를 바꾸지 마세요.**

### ① `/api` 아래는 읽기만 합니다

`routes.py` 에 `@router.post` 를 만들지 마세요. 쓰기는 기존 경로가 합니다
(`POST /master/runs/{id}/decision`). 두 군데서 쓰면 **«어느 쪽으로 승인했나»
가 기록에서 갈립니다.** 테스트가 막습니다 — `test_쓰기는_막혀_있다`.

### ② `None` 은 0 이 아닙니다

```python
data=[21400, None, 19800]   # 가운데는 그날 보고가 없었다
data=[21400, 0,    19800]   # 가운데는 재고가 없었다      ← 뜻이 다르다
```

값이 없으면 `None` 을 넣으세요. 화면이 **선을 끊고** 표에 `—` 를 그립니다.
0 으로 채우면 «그날 값이 0이었다» 는 **거짓말**이 됩니다.
DB 조회가 `None` 을 돌려주는 경우를 반드시 다루세요.

그리고 **왜 없는지를 `Note` 에 적으세요.** 안 적으면 보는 사람이 0 으로 읽습니다.

### ③ 그래프 계열 길이는 날짜축과 같아야 합니다

화면은 **칸 번호로만** 위치를 잡습니다. 계열이 짧으면 오류 없이
**조용히 왼쪽으로 밀립니다.** "그날 값이 그랬구나" 로 읽히는 것이 제일
위험합니다. 날짜축 길이는 응답 안 `axis.days` 로 옵니다.

### ④ 대시보드는 숫자를 만들지 않습니다

> 마스터는 숫자를 만들지 않는다. 부서 값을 날짜 축에 놓고, 없으면 공란으로 둔다.

대시보드에 자기 파트 값을 얹고 싶으면 **자기 `query.py` 에 함수를 만들고**
대시보드가 그걸 부르게 하세요. 재무·물류가 이렇게 합니다.

```python
# app/api/logistics/query.py
def dashboard_stock(n: int, at: int) -> Chart: ...
```

**안 그러면 갈라집니다.** 실제로 갈라졌습니다 — 재고 요약은 4,550kg 인데
그래프 끝은 14,600kg 이었습니다.

### ⑤ 백엔드를 고치면 서버를 다시 올리세요

`--reload` 없이 띄우면 코드를 고쳐도 안 바뀝니다. 새 칸을 더하고 서버를
안 올려서 **화면이 통째로 죽은 적**이 있습니다.
"""

DB_HELP = """\
## ★ 아직 안 정한 것 — `sim_run_id`

부서 서비스들이 `sim_run_id` 를 받습니다.

```python
get_finance_dashboard(sim_run_id=..., as_of=as_of)
```

그런데 **화면 API 는 지금 `as_of` 만 받습니다.** 어디서 얻을지 아직
안 정했습니다. 세 가지가 있습니다.

```
㉮ 주소에 파라미터를 더한다      /api/finance?as_of=…&sim_run_id=…
㉯ 서버가 그날의 기본 실행을 고른다
㉰ 설정값으로 하나 못 박는다
```

**혼자 정하지 말고 물어보세요.** 잘못 고르면 다른 실행의 값을 화면에
띄우게 되고, 그건 **틀린 줄도 모르는** 오류입니다.
급하면 ㉰ 로 두고 `Note` 에 «어느 실행을 보고 있는지» 를 적으세요.

---

## DB 는 이미 있는 것을 쓰세요

부서 서비스로 안 되는 값만 직접 읽습니다. **먼저 위를 보세요.**

```python
from {db_module} import fetch_one, fetch_all, get_db_schema
```

```python
fetch_one(query, params) -> dict | None      # 없으면 None. 반드시 다룰 것
fetch_all(query, params) -> list[dict]       # 없으면 빈 목록
get_db_schema()          -> str              # 스키마 이름. 하드코딩 금지
```

**새 DB 모듈을 만들지 마세요.** 접속 정보가 두 군데로 갈라집니다.
접속 정보는 `.env` 에 있습니다 — **코드나 문서에 절대 쓰지 마세요.**

스키마 이름은 문자열로 박지 말고 `get_db_schema()` 로 받아 씁니다.

```python
schema = get_db_schema()
rows = fetch_all(f'SELECT * FROM {{schema}}.{table} WHERE as_of = %s', (as_of,))
```

값은 `%s` 자리표시자로 넘기세요. **f-string 으로 이어붙이지 마세요** (SQL 주입).
"""

VERIFY = """\
## 다 됐는지 확인하기

### 1. 모양 검사 — 반드시 통과해야 합니다

```bash
cd backend
uv run pytest tests/api -q
```

18개가 돕니다. **값은 안 봅니다 — 모양만 봅니다.**
숫자가 바뀌는 건 당연하므로, 이 테스트를 고치거나 지우지 마세요.

무엇을 잡는가:

```
탭이 열리나 (200)
쓰기가 막혀 있나 (405)
없는 값에 400 을 내나 · 무엇이 가능한지 문장으로 알려주나
Source.filled 가 있나
그래프 계열 길이가 날짜축과 같나
y_labels 가 y_ticks 와 짝인가
대시보드 값이 부서 값과 같은가
표 머리에 있는 칸이 행에도 있나
```

### 2. 문법 검사

```bash
cd backend
uv run ruff check app/api
```

**`All checks passed!` 가 나와야 합니다.** 줄 길이는 100 입니다.

`ruff format` 은 저장소 전체가 아직 적용 전이라 **돌리지 마세요** —
남의 파일까지 바뀌어 리뷰가 못 볼 만큼 커집니다.

### 3. 값이 실제로 나오나

```bash
cd backend && uv run uvicorn app.main:app --port 8000
curl "http://127.0.0.1:8000{route}"
```

브라우저로는 `http://127.0.0.1:8000/docs` 에서 눌러 볼 수 있습니다.

### 4. 화면으로 보기

```bash
cd frontend && npm install && npm run dev
```

`http://localhost:3000{screen}` 을 엽니다. 로그인은 아무 사번·이름이나 됩니다.

**★ `localhost` 로 여세요. `127.0.0.1` 은 흰 화면이 뜹니다.**
Next 개발 서버가 `127.0.0.1` 을 다른 사이트로 보고 막습니다.

**★ 화면 위의 주황색 「예시값」 띠가 없어졌는지 보세요.** 안 없어졌으면
`Source(filled=True)` 를 안 바꾼 것입니다.
"""

DONT = """\
## 하지 말 것

| 하지 말 것 | 왜 |
|---|---|
| 다른 파트 폴더를 고치기 | 그 파트 사람이 동시에 작업 중입니다 |
| `primitives.py` 를 고치기 | 여섯 탭이 같이 씁니다. 필요하면 ML 파트에 말하세요 |
| `frontend/` 를 고치기 | 계약(`schema.py`)만 지키면 화면은 안 고쳐도 됩니다 |
| `app/main.py` 를 고치기 | 이미 붙어 있습니다 |
| `tests/api/` 를 고치거나 지우기 | 그게 유일한 안전망입니다 |
| `@router.post` 를 만들기 | 6-① 규칙 |
| `.env` 값을 코드·문서에 쓰기 | 접속 정보는 코드에 안 남깁니다 |
| 값이 없을 때 0 으로 채우기 | 6-② 규칙. 거짓말이 됩니다 |
| 기존 부서 라우터(`/finance/agent` 등) 건드리기 | 에이전트가 씁니다. 이 일과 무관합니다 |
"""

CHECKLIST = """\
## 끝났는지 스스로 확인하는 목록

하나라도 «아니오» 면 아직 안 끝났습니다.

- [ ] `{query_path}` **만** 고쳤다 (`git status` 로 확인)
- [ ] `build()` 가 예시값이 아니라 DB 에서 읽은 값을 돌려준다
- [ ] 조회가 비었을 때 0 이 아니라 `None`/공란으로 나가고, 이유를 `Note` 에 적었다
- [ ] `Source(filled=True, owner=..., note="어느 표에서 읽었는지")` 로 바꿨다
- [ ] `uv run pytest tests/api -q` 가 전부 통과한다
- [ ] `uv run ruff check app/api` 가 조용하다
- [ ] 화면(`{screen}`)을 열어 「예시값」 띠가 사라진 것을 눈으로 봤다
- [ ] 표의 모든 `columns[].key` 가 모든 행에 있다
- [ ] 그래프를 넣었다면 계열 길이가 `axis.days` 와 같다
- [ ] 새 POST 를 만들지 않았다
"""


# ══════════════════════════════════════════════════════════════════════════
#  파트별 — 손으로 쓰는 부분
# ══════════════════════════════════════════════════════════════════════════

class Part(typing.NamedTuple):
    key: str
    owner: str
    title: str
    route: str
    screen: str
    db_module: str
    table: str
    model: type[BaseModel]
    signature: str
    tables: str
    notes: str


PARTS = [
    Part(
        key="dashboard", owner="마스터", title="대시보드",
        route="/api/dashboard?as_of=2026-01-06", screen="/console",
        db_module="app.finance.db", table="daily_closings",
        model=DashboardTab,
        signature="def build(as_of: date) -> DashboardTab:",
        tables="""\
**이 탭은 DB 를 직접 읽지 않습니다.** 다른 다섯 파트의 `build()` 를 부릅니다.

```python
from app.api.finance import query as finance_q
from app.api.forecast import query as forecast_q
from app.api.logistics import query as logistics_q
from app.api.purchase import query as purchase_q
from app.api.sales import query as sales_q
```
""",
        notes="""\
## ★ 이 파트만의 규칙 — 숫자를 만들지 마세요

사이드바에 적혀 있는 그대로입니다.

> 마스터는 숫자를 만들지 않는다.
> 부서 값을 날짜 축에 놓고, 없으면 공란으로 둔다.

`build()` 안에서 **계산하지 마세요.** 다른 파트의 `build()` 를 불러
골라 담기만 합니다. 그래프도 마찬가지입니다 — 주인 부서가 만든 것을 받습니다.

```python
cash = finance_q.dashboard_cash(n, at)        # 재무가 만든다
stock = logistics_q.dashboard_stock(n, at)    # 물류가 만든다
```

**어긴 적이 있고, 실제로 갈라졌습니다** — 요약은 재고 4,550kg 인데
그래프 끝은 14,600kg 이었습니다. 테스트가 이걸 잡습니다
(`test_대시보드는_숫자를_만들지_않는다`).

한 부서 값이 없으면 **그 칸만 공란**으로 두고 나머지는 그립니다.
대시보드 전체가 죽으면 안 됩니다.

`sources` 에는 다섯 파트의 `Source` 를 그대로 실으세요. 하나라도
`filled=False` 면 화면이 「예시값」 띠를 띄웁니다.
""",
    ),
    Part(
        key="forecast", owner="ML", title="가격 예측",
        route="/api/forecast?as_of=2026-01-06&item=배추", screen="/console/forecast",
        db_module="app.ml.db", table="ml_price_forecasts",
        model=ForecastTab,
        signature="def build(as_of: date, item: str) -> ForecastTab:",
        tables="""\
`haetdeul.ml_price_forecasts` 를 읽습니다. 직접 SQL 을 쓰지 말고
**이미 있는 서비스 함수**를 쓰세요 — 개장일 축을 달력일 축으로 펴는 일과
품질 게이트가 거기 들어 있습니다.

```python
from app.ml.service import get_forecast
fc = get_forecast(item, as_of, "AUC")   # LookupError · RuntimeError 를 낸다
```
""",
        notes="""\
## ★ 이 파트만의 규칙

**여섯 중 유일하게 실제 값에 붙어 있습니다.** 이미 `filled=True` 로 갈 수
있는 상태이고, 못 읽으면 예시값으로 떨어집니다 —
**화면이 통째로 죽는 것보다 낫습니다.** 이 폴백을 지우지 마세요.

**`accuracy` 와 `caveat` 를 지우지 마세요.** 예측선만 그리면 정답처럼
보입니다. 실측으로 1,000원짜리를 배추는 197원(19.7%), 무는 186원(18.6%)
틀립니다. 먼 날짜일수록 더 틀립니다 — D+5 13.0% · D+14 17.8% · D+18 19.8%.

**`use_recommended=False` 인 조합은 화면이 「쓰지 말 것」으로 표시합니다.**
그 조합은 우리 모델이 «어제 가격 그대로» 보다 나쁩니다. 임의로 `True` 로
두지 마세요.

**품목은 배추·무·양파 셋뿐입니다** (`ITEMS`). 마늘은 원자료 품질 문제로
빠져 있습니다. 없는 품목은 400 으로 막고 **가능한 목록을 문장으로** 알려줍니다.

**`ci_width` 는 (상한 − 하한) ÷ 가운데 값**입니다. 0.15 이상이면
`review=True` 로 두어 사람이 한 번 보게 합니다.
""",
    ),
    Part(
        key="purchase", owner="매입", title="매입",
        route="/api/purchase?as_of=2026-01-06", screen="/console/purchase",
        db_module="app.purchase_agent.db", table="purchases",
        model=PurchaseTab,
        signature="def build(as_of: date) -> PurchaseTab:",
        tables="""\
읽을 곳: `purchases` · `arrival_schedule` · 그리고 마스터의 실행/결정 기록.

제안(후보 안)은 `app/purchase_agent/` 가 만들고, 승인·확정은
`app/master/` 가 씁니다. **여기서 에이전트를 돌리지 마세요** — 저장된
결과만 읽습니다 (화면을 열 때마다 LLM 이 돌면 안 됩니다).
""",
        notes="""\
## ★ 이 파트만의 규칙

**승인 버튼은 이 화면에 없습니다.** 승인은 화면 아래 서랍(마스터)에서
`POST /master/runs/{request_id}/decision` 으로 합니다.
이 화면은 **무엇을 고를지 판단할 근거**를 보이는 자리입니다.

**`Reason.ref` 를 반드시 채우세요.** 근거에 꼬리표(`FC-2026-01-06` ·
`MQ-가락-0105`)가 없으면 나중에 되짚을 수 없습니다. 이미 그 형태로
쓰고 있으니 그대로 실으면 됩니다.

**`max_price`(이보다 비싸면 안 산다)를 빠뜨리지 마세요.** 안마다 다릅니다.
화면에서 제일 눈에 띄는 칸이고, 이게 없으면 안을 고를 수 없습니다.

**`risks`(걸리는 것)를 지우지 마세요.** 비어 있으면 화면이 그 자리를
안 그립니다. 있는데 안 실으면 위험을 숨기는 것이 됩니다.

**확정 매입이 없으면 빈 표**로 두고 `empty_text` 로 이유를 적습니다 —
"아직 확정된 매입이 없습니다 — 위에서 안을 고르면 여기에 생깁니다".
0건과 «아직 안 골랐다» 는 다릅니다.
""",
    ),
    Part(
        key="finance", owner="재무", title="재무",
        route="/api/finance?as_of=2025-12-31&state=base", screen="/console/finance",
        db_module="app.finance.db", table="finance_states",
        model=FinanceTab,
        signature="def build(as_of: date, state: str) -> FinanceTab:",
        tables="""\
**★ SQL 을 새로 쓰지 마세요. 이미 만들어 둔 것을 부르세요.**

```python
from app.finance.dashboard_service import get_finance_dashboard, get_finance_cashflow
dash = get_finance_dashboard(sim_run_id=..., as_of=as_of)
flow = get_finance_cashflow(sim_run_id=..., as_of=as_of)
```

`GET /finance/dashboard` · `/finance/dashboard/cashflow` 가 쓰는 함수입니다.
**같은 쿼리를 두 벌 두면 언젠가 값이 갈라집니다.**

이 `query.py` 가 할 일은 **읽는 것이 아니라 옮기는 것**입니다 —
저쪽이 준 업무 값을 화면 부품(`Stat` · `Table` · `Chart`)에 담습니다.

원래 표: `finance_states` · `daily_closings` · `receivables` · `payables` ·
`expenses`. 읽은 표 이름을 `tables_read` 에 실으세요 — 화면 아래에 적힙니다.
""",
        notes="""\
## ★ 이 파트만의 규칙

**조회 전용입니다.** 에이전트(`POST /finance/agent`)를 부르지 마세요.
상태 전이는 승인 트랜잭션(`apply_approval`) 안에서만 일어납니다.

**`state` 는 «대출 승인 결과» 가 아닙니다.** DB 에 저장된 기준 상태입니다
(`BASE_NO_LOAN` · `LOAN_BASELINE`). 화면 글에도 그렇게 적혀 있으니
말을 바꾸지 마세요. 없는 상태는 400 으로 막고 가능한 목록을 알려줍니다.

**현금 그래프의 가로축은 공용 날짜축이 아닙니다** — 12월 한 달(30칸)입니다.
그래서 `x_labels` 로 자기 눈금을 같이 보냅니다. 30개를 다 적으면 글자가
겹치므로 다섯 개만 적고 나머지는 빈 문자열로 둡니다.

**`flows` 는 원장 용어 대신 사람 말로 적는 자리**입니다.
`label` 에 «상품 매입으로 나간 돈», `term` 에 `purchase cash out`.
둘 다 채우세요 — 원래 용어를 지우면 회계 담당이 대조를 못 합니다.

**대시보드에 얹는 현금 그래프는 `dashboard_cash(n, at)` 이 만듭니다.**
요약 숫자와 같은 곳에서 나와야 둘이 안 갈라집니다 (6-④ 규칙).
""",
    ),
    Part(
        key="logistics", owner="물류", title="재고 · 물류",
        route="/api/logistics?as_of=2026-01-06&pane=stock", screen="/console/inventory",
        db_module="app.logistics.db", table="inventory_lots",
        model=LogisticsTab,
        signature="def build(as_of: date, pane: str) -> LogisticsTab:",
        tables="""\
**★ SQL 을 새로 쓰지 마세요. 이미 만들어 둔 것을 부르세요** (#415).

```python
from app.logistics.console_service import get_inventory_console
snap = get_inventory_console(sim_run_id=..., as_of=as_of)
```

`app/logistics/console_service.py` 에 `/logistics/inventory` · `/inbound` ·
`/warehouse` · `/outbound` 가 쓰는 함수가 다 있습니다. **같은 쿼리를 두 벌
두면 언젠가 값이 갈라집니다.**

이 `query.py` 가 할 일은 **읽는 것이 아니라 옮기는 것**입니다 —
저쪽이 준 업무 값을 화면 부품(`Stat` · `Table` · `Card`)에 담습니다.

원래 표를 직접 봐야 하면: `inventory_lots` · `inventory_reservations` ·
`inventory_allocations` · `arrival_schedule` · `zone_capacity`.

**`panes` 는 넷 다 채워서 보냅니다** (`stock` `inbound` `warehouse`
`outbound`). `selected` 가 지금 보고 있는 것이고, 화면이 나머지를
미리 들고 있어 탭을 눌러도 깜빡이지 않습니다.
""",
        notes="""\
## ★ 이 파트만의 규칙

**보고가 없는 날은 `None` 입니다. 0 이 아닙니다** (6-② 규칙).
이 파트가 이 규칙에 제일 많이 걸립니다 — 재고를 0 으로 그리면
«그날 재고가 없었다» 는 거짓말이 됩니다. 왜 없는지 `Note` 에 적으세요.

**판매가능량은 화면이 계산하지 않습니다.** 서버가 예약·할당·Lot 상태·
신선도를 반영해 낸 값을 그대로 실으세요. 여기서 다시 빼거나 더하지 마세요.

**예약과 할당은 재고를 줄이지 않습니다.** 실제 감소는 실출고 때입니다.
이 사실을 `lead` 노트에 적어 두었습니다 — 지우지 마세요.

**등급이 없으면 «등급 미확정» 으로 적습니다.** 특으로 넘겨짚지 마세요.

**대시보드에 얹는 재고 그래프는 `dashboard_stock(n, at)` 이 만듭니다.**
요약 숫자와 **같은 상수(`ONHAND_NOW`)에서** 나와야 둘이 안 갈라집니다.
실제로 갈라졌던 자리입니다 (6-④ 규칙).

**카드를 더 넣어도 화면은 안 고칩니다.** 지금 7장인데 데모에는 15장입니다.
`Card(...)` 를 목록에 더하기만 하세요.
""",
    ),
    Part(
        key="sales", owner="판매", title="판매",
        route="/api/sales?as_of=2025-12-31", screen="/console/sales",
        db_module="app.sales.db", table="sales",
        model=SalesTab,
        signature="def build(as_of: date) -> SalesTab:",
        tables="""\
**★ SQL 을 새로 쓰지 마세요. 이미 만들어 둔 것을 부르세요.**

```python
from app.sales.dashboard_service import get_sales_dashboard
dash = get_sales_dashboard(sim_run_id=..., as_of=as_of)
```

`GET /sales/dashboard` 가 쓰는 함수입니다. **같은 쿼리를 두 벌 두면
언젠가 값이 갈라집니다.**

이 `query.py` 가 할 일은 **읽는 것이 아니라 옮기는 것**입니다.

원래 표: `sales` · `sale_items` · `receivables`.
""",
        notes="""\
## ★ 이 파트만의 규칙

**조회 전용입니다.** 시나리오(`POST /sales/proposal` 등)를 돌리지 마세요.
저장된 판매·수금 결과만 읽습니다.

**«판 것» 과 «받은 것» 을 반드시 갈라서 적으세요.** 지금 화면이 그렇게
되어 있습니다 — 판매 7,305만원인데 실제로 들어온 수금은 0원입니다
(D+30 조건). 둘을 한 숫자로 합치면 현금이 있는 것처럼 보입니다.

**공헌이익은 «매출에서 변동비를 뺀 것»** 이고 고정비는 아직 안 뺐습니다.
`detail` 에 그 말을 적어 두었습니다 — 지우면 이익으로 오해합니다.

**수금 그래프의 가로축은 공용 날짜축이 아닙니다.** `x_labels` 로 자기
눈금을 같이 보냅니다.

**카드를 더 넣어도 화면은 안 고칩니다.**
""",
    ),
]


# ══════════════════════════════════════════════════════════════════════════
#  조립
# ══════════════════════════════════════════════════════════════════════════

#: §부품 에 이미 적은 공용 모델. 계약 표에서 또 펴지 않는다.
SHARED = {"Stat", "Badge", "Note", "Column", "Table", "Series", "Band",
          "Marker", "Chart", "Day", "CalendarAxis", "Source", "Card", "Pane"}

TEMPLATE = """\
<!-- 이 파일은 `app/api/_agent_docs.py` 가 만듭니다. 직접 고치지 말고
     그 파일을 고친 뒤 다시 돌리세요:  uv run python -m app.api._agent_docs -->

# {title} 화면 API — 작업 지시서

**읽는 대상: {owner} 파트와 그 파트의 코딩 도우미(CLI AI).**
이 문서 하나로 끝까지 갈 수 있게 썼습니다. 다른 문서를 안 봐도 됩니다.

---

## 할 일 한 줄

`{query_path}` 의 `build()` 안쪽을 **실제 DB 값으로** 채우고
`Source(filled=True)` 로 바꾼다. **그 파일 하나만 고친다.**

---

## 지금 상태

이 폴더는 이미 다 돌아갑니다. 주소도 화면도 있습니다.

```
{route_line}
{screen_line}
```

지금은 `build()` 가 **예시값**(데모 화면에서 옮겨 적은 숫자)을 돌려주고,
`Source(filled=False)` 라서 화면 위에 **주황색 「예시값」 띠**가 뜹니다.

> **예시값** — {owner} 파트가 아직 실제 값에 붙이지 않았습니다.
> 이 화면의 숫자는 화면 구성을 보이기 위한 것입니다.

**★ 채우고 나면 반드시 `filled=True` 로 바꾸세요.** 안 바꾸면 진짜 값인데
«예시» 라고 붙어 아무도 안 믿습니다. 반대로 안 채웠는데 `True` 로 두면
**데모 숫자를 실적으로 읽습니다.** 이쪽이 훨씬 위험합니다.

---

## 이 폴더의 파일

```
app/api/{key}/
  AGENTS.md    이 문서
  schema.py    응답 모양 — 바꾸려면 화면(frontend/src/lib/screen.ts)도 같이 고쳐야 함
  query.py  ★  여기만 고친다
  routes.py    주소 — 안 고쳐도 된다
```

고칠 함수는 이것 하나입니다.

```python
# {query_path}
{signature}
```

---

## 어디서 값을 읽나

{tables}
{db_help}
---

## 돌려줄 모양 — 계약

**이 표는 `schema.py` 에서 뽑은 것입니다.** 칸 이름을 임의로 바꾸면
화면이 조용히 빈 칸이 됩니다 — 오류가 안 나서 아무도 모릅니다.

칸을 **더하는** 것은 안전합니다. **이름을 바꾸거나 지우는** 것이 위험하고,
그때는 `frontend/src/lib/screen.ts` 도 같이 고쳐야 합니다 (ML 파트에 연락).

{contract}

---

{primitives}
---

{notes}
---

{rules}
---

{verify}
---

{dont}
---

{checklist}
---

## 막히면

| 증상 | 볼 곳 |
|---|---|
| 화면이 하얗다 | `127.0.0.1` 말고 `localhost` 로 열었나 |
| “백엔드에 닿지 못했습니다” | 백엔드가 떠 있나 · 포트가 맞나 |
| 값이 안 바뀐다 | 백엔드를 다시 올렸나 (6-⑤ 규칙) |
| 표의 한 칸이 통째로 비었다 | `columns[].key` 와 `rows` 키 이름이 같나 |
| 선이 엉뚱한 날짜에 있다 | 계열 길이가 `axis.days` 와 같나 (6-③ 규칙) |
| 숫자가 두 군데서 다르다 | 대시보드가 값을 직접 만들고 있지 않나 (6-④ 규칙) |
| 「예시값」 띠가 안 없어진다 | `Source(filled=True)` 로 바꿨나 |
| `pytest` 가 표 칸 오류를 낸다 | 머리에 있는 `key` 가 모든 행에 있나 |

사람이 읽을 개요는 `app/api/README.md` 에 있습니다.
화면 쪽 코드는 `frontend/src/app/console/` 와
`frontend/src/components/console/Blocks.tsx` 입니다.
"""


def render(part: Part) -> str:
    query_path = f"backend/app/api/{part.key}/query.py"
    return TEMPLATE.format(
        title=part.title,
        owner=part.owner,
        key=part.key,
        query_path=query_path,
        signature=part.signature,
        route_line=f"API     GET {part.route}",
        screen_line=f"화면    http://localhost:3000{part.screen}",
        tables=part.tables,
        db_help=DB_HELP.format(db_module=part.db_module, table=part.table),
        contract=contract(part.model, SHARED),
        primitives=PRIMITIVES,
        notes=part.notes,
        rules=RULES,
        verify=VERIFY.format(route=part.route, screen=part.screen),
        dont=DONT,
        checklist=CHECKLIST.format(query_path=query_path, screen=part.screen),
    )


def main() -> None:
    for part in PARTS:
        target = HERE / part.key / "AGENTS.md"
        target.write_text(render(part), encoding="utf-8", newline="\n")
        print(f"  {target.relative_to(HERE.parent.parent)}  {len(render(part).splitlines()):>4}줄")


if __name__ == "__main__":
    print("파트별 작업 지시서를 만듭니다.")
    main()
