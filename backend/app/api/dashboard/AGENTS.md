<!-- 이 파일은 `app/api/_agent_docs.py` 가 만듭니다. 직접 고치지 말고
     그 파일을 고친 뒤 다시 돌리세요:  uv run python -m app.api._agent_docs -->

# 대시보드 화면 API — 작업 지시서

**읽는 대상: 마스터 파트와 그 파트의 코딩 도우미(CLI AI).**
이 문서 하나로 끝까지 갈 수 있게 썼습니다. 다른 문서를 안 봐도 됩니다.

---

## 할 일 한 줄

`backend/app/api/dashboard/query.py` 의 `build()` 안쪽을 **실제 DB 값으로** 채우고
`Source(filled=True)` 로 바꾼다. **그 파일 하나만 고친다.**

---

## 지금 상태

이 폴더는 이미 다 돌아갑니다. 주소도 화면도 있습니다.

```
API     GET /api/dashboard?as_of=2026-01-06
화면    http://localhost:3000/console
```

지금은 `build()` 가 **예시값**(데모 화면에서 옮겨 적은 숫자)을 돌려주고,
`Source(filled=False)` 라서 화면 위에 **주황색 「예시값」 띠**가 뜹니다.

> **예시값** — 마스터 파트가 아직 실제 값에 붙이지 않았습니다.
> 이 화면의 숫자는 화면 구성을 보이기 위한 것입니다.

**★ 채우고 나면 반드시 `filled=True` 로 바꾸세요.** 안 바꾸면 진짜 값인데
«예시» 라고 붙어 아무도 안 믿습니다. 반대로 안 채웠는데 `True` 로 두면
**데모 숫자를 실적으로 읽습니다.** 이쪽이 훨씬 위험합니다.

---

## 이 폴더의 파일

```
app/api/dashboard/
  AGENTS.md    이 문서
  schema.py    응답 모양 — 바꾸려면 화면(frontend/src/lib/screen.ts)도 같이 고쳐야 함
  query.py  ★  여기만 고친다
  routes.py    주소 — 안 고쳐도 된다
```

고칠 함수는 이것 하나입니다.

```python
# backend/app/api/dashboard/query.py
def build(as_of: date) -> DashboardTab:
```

---

## 어디서 값을 읽나

**이 탭은 DB 를 직접 읽지 않습니다.** 다른 다섯 파트의 `build()` 를 부릅니다.

```python
from app.api.finance import query as finance_q
from app.api.forecast import query as forecast_q
from app.api.logistics import query as logistics_q
from app.api.purchase import query as purchase_q
from app.api.sales import query as sales_q
```

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
from app.finance.db import fetch_one, fetch_all, get_db_schema
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
rows = fetch_all(f'SELECT * FROM {schema}.daily_closings WHERE as_of = %s', (as_of,))
```

값은 `%s` 자리표시자로 넘기세요. **f-string 으로 이어붙이지 마세요** (SQL 주입).

---

## 돌려줄 모양 — 계약

**이 표는 `schema.py` 에서 뽑은 것입니다.** 칸 이름을 임의로 바꾸면
화면이 조용히 빈 칸이 됩니다 — 오류가 안 나서 아무도 모릅니다.

칸을 **더하는** 것은 안전합니다. **이름을 바꾸거나 지우는** 것이 위험하고,
그때는 `frontend/src/lib/screen.ts` 도 같이 고쳐야 합니다 (ML 파트에 연락).

### `DashboardTab` — 이 함수가 돌려줄 것

| 칸 | 타입 | 필수 | 무엇 |
|---|---|---|---|
| `axis` | `CalendarAxis` | 필수 | 여섯 탭이 함께 쓰는 날짜축. 그래프 계열 길이의 기준 |
| `badges` | `list[Badge]` | 필수 | 상단 알약 — 오늘 상태 |
| `stats` | `list[Stat]` | 필수 | 맨 위 요약 다섯 칸. **다섯 파트에서 하나씩 가져온다** |
| `forecast_cards` | `list[ItemCard]` | 필수 | 가격 예측 탭과 **같은 값** |
| `purchase` | `Table` | 필수 | 승인을 기다리는 안 |
| `purchase_note` | `Note` | 필수 | 상한가가 안마다 다르다는 안내 |
| `cash_chart` | `Chart` | 필수 | 재무의 dashboard_cash() 가 만든 것. 여기서 만들지 말 것 |
| `stock_chart` | `Chart` | 필수 | 물류의 dashboard_stock() 가 만든 것. 여기서 만들지 말 것 |
| `sources` | `list[Source]` | 필수 | 탭마다 채워졌나 — 하나라도 예시면 화면이 알린다 |

### `ItemCard` — 품목 하나의 다음 값 — 대시보드 위쪽 카드에도 같은 값을 쓴다.

| 칸 | 타입 | 필수 | 무엇 |
|---|---|---|---|
| `item` | `str` | 필수 | 배추 · 무 · 양파 |
| `grade` | `str` | 필수 | 예측한 등급. 경락가는 특등급 |
| `spec` | `str &#124; None` | 선택 | 포장 규격. 경락가만 있다 |
| `target_date` | `str` | 필수 | 언제 값인가 |
| `predicted` | `int` | 필수 | 가운데 값. **이것만 보고 사면 안 된다** |
| `lower` | `int` | 필수 | 구간 아래끝 |
| `upper` | `int` | 필수 | 구간 위끝. 최악을 잡을 때 이 값을 쓴다 |
| `unit` | `str` | 선택 | 값의 단위 |
| `ci_width` | `float` | 필수 | 구간 폭 ÷ 가운데 값. 클수록 덜 확실하다 |
| `review` | `bool` | 필수 | 폭이 넓어 사람이 한 번 볼 것을 권하나 |
| `use_recommended` | `bool` | 필수 | False 면 이 조합은 '어제 가격 그대로' 가 우리 모델보다 낫다 |
| `gated` | `bool` | 선택 | 이 값이 **모델이 아니라 어제값 그대로**인가. 리드타임 3 미만이면 모델을 안 쓴다 — 어제 가격이 이미 정답에 가까워 모델이 낄 자리가 없다 |

---

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

---

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

---

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

---

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
curl "http://127.0.0.1:8000/api/dashboard?as_of=2026-01-06"
```

브라우저로는 `http://127.0.0.1:8000/docs` 에서 눌러 볼 수 있습니다.

### 4. 화면으로 보기

```bash
cd frontend && npm install && npm run dev
```

`http://localhost:3000/console` 을 엽니다. 로그인은 아무 사번·이름이나 됩니다.

**★ `localhost` 로 여세요. `127.0.0.1` 은 흰 화면이 뜹니다.**
Next 개발 서버가 `127.0.0.1` 을 다른 사이트로 보고 막습니다.

**★ 화면 위의 주황색 「예시값」 띠가 없어졌는지 보세요.** 안 없어졌으면
`Source(filled=True)` 를 안 바꾼 것입니다.

---

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

---

## 끝났는지 스스로 확인하는 목록

하나라도 «아니오» 면 아직 안 끝났습니다.

- [ ] `backend/app/api/dashboard/query.py` **만** 고쳤다 (`git status` 로 확인)
- [ ] `build()` 가 예시값이 아니라 DB 에서 읽은 값을 돌려준다
- [ ] 조회가 비었을 때 0 이 아니라 `None`/공란으로 나가고, 이유를 `Note` 에 적었다
- [ ] `Source(filled=True, owner=..., note="어느 표에서 읽었는지")` 로 바꿨다
- [ ] `uv run pytest tests/api -q` 가 전부 통과한다
- [ ] `uv run ruff check app/api` 가 조용하다
- [ ] 화면(`/console`)을 열어 「예시값」 띠가 사라진 것을 눈으로 봤다
- [ ] 표의 모든 `columns[].key` 가 모든 행에 있다
- [ ] 그래프를 넣었다면 계열 길이가 `axis.days` 와 같다
- [ ] 새 POST 를 만들지 않았다

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
