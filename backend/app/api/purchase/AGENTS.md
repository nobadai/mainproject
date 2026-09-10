<!-- 이 파일은 `app/api/_agent_docs.py` 가 만듭니다. 직접 고치지 말고
     그 파일을 고친 뒤 다시 돌리세요:  uv run python -m app.api._agent_docs -->

# 매입 화면 API — 작업 지시서

**읽는 대상: 매입 파트와 그 파트의 코딩 도우미(CLI AI).**
이 문서 하나로 끝까지 갈 수 있게 썼습니다. 다른 문서를 안 봐도 됩니다.

---

## 할 일 한 줄

`backend/app/api/purchase/query.py` 의 `build()` 안쪽을 **실제 DB 값으로** 채우고
`Source(filled=True)` 로 바꾼다. **그 파일 하나만 고친다.**

---

## 지금 상태

이 폴더는 이미 다 돌아갑니다. 주소도 화면도 있습니다.

```
API     GET /api/purchase?as_of=2026-01-22
화면    http://localhost:3000/console/purchase
```

지금은 `build()` 가 **예시값**(데모 화면에서 옮겨 적은 숫자)을 돌려주고,
`Source(filled=False)` 라서 화면 위에 **주황색 「예시값」 띠**가 뜹니다.

> **예시값** — 매입 파트가 아직 실제 값에 붙이지 않았습니다.
> 이 화면의 숫자는 화면 구성을 보이기 위한 것입니다.

**★ 채우고 나면 반드시 `filled=True` 로 바꾸세요.** 안 바꾸면 진짜 값인데
«예시» 라고 붙어 아무도 안 믿습니다. 반대로 안 채웠는데 `True` 로 두면
**데모 숫자를 실적으로 읽습니다.** 이쪽이 훨씬 위험합니다.

---

## 이 폴더의 파일

```
app/api/purchase/
  AGENTS.md    이 문서
  schema.py    응답 모양 — 바꾸려면 화면(frontend/src/lib/screen.ts)도 같이 고쳐야 함
  query.py  ★  여기만 고친다
  routes.py    주소 — 안 고쳐도 된다
```

고칠 함수는 이것 하나입니다.

```python
# backend/app/api/purchase/query.py
def build(as_of: date, sim_run_id: str | None = None) -> PurchaseTab:
```

---

## 어디서 값을 읽나

읽을 곳: `master_agent_runs`(저장된 실행) · `master_decisions`(사람의 결정) ·
`purchases` + `purchase_items`(확정 매입 원장) · `items`(품목 이름).

제안(후보 안)은 `app/purchase_agent/` 가 만들고, 승인·확정은
`app/master/` 가 씁니다. **여기서 에이전트를 돌리지 마세요** — 저장된
결과만 읽습니다 (화면을 열 때마다 LLM 이 돌면 안 됩니다).

🔴 **DB 헬퍼는 `app.finance.db` 입니다.** `app.purchase_agent.db` 에는
`get_db_schema` 가 **없습니다** — 일부러 뺐고 (그 파일 머리말) 이유는
*"`.env` 가 어느 시세 테이블을 읽을지 정하면 안 된다"* 입니다. 그건
에이전트 경로의 사정이고, 화면은 `haetdeul` 도메인 표를 읽으므로 스키마를
`.env` 가 정하는 것이 맞습니다. 마스터 `ledger_repository.py` 가 같은
이유로 같은 선택을 했습니다. ⚠️ 쓰기 헬퍼는 가져오지 않습니다.

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
rows = fetch_all(f'SELECT * FROM {schema}.purchases WHERE as_of = %s', (as_of,))
```

값은 `%s` 자리표시자로 넘기세요. **f-string 으로 이어붙이지 마세요** (SQL 주입).

---

## 돌려줄 모양 — 계약

**이 표는 `schema.py` 에서 뽑은 것입니다.** 칸 이름을 임의로 바꾸면
화면이 조용히 빈 칸이 됩니다 — 오류가 안 나서 아무도 모릅니다.

칸을 **더하는** 것은 안전합니다. **이름을 바꾸거나 지우는** 것이 위험하고,
그때는 `frontend/src/lib/screen.ts` 도 같이 고쳐야 합니다 (ML 파트에 연락).

### `PurchaseTab` — 이 함수가 돌려줄 것

| 칸 | 타입 | 필수 | 무엇 |
|---|---|---|---|
| `stats` | `list[Stat]` | 필수 | 오늘 제안 · 승인 대기 · 확정 매입액 · 입고 예정 |
| `plans` | `list[Plan]` | 필수 | 오늘 낸 안들. 비면 화면이 «안이 없다» 고 적는다 |
| `plans_note` | `Note` | 필수 | 안이 왜 이 개수인가 |
| `committed` | `Table` | 필수 | 승인을 거친 뒤에 생기는 확정 매입 |
| `committed_note` | `Note` | 필수 | 승인 전에는 표가 빈다는 안내 |
| `source` | `Source` | 필수 | 예시값인지 실제 값인지 |

### `Plan` — 매입안 하나.

| 칸 | 타입 | 필수 | 무엇 |
|---|---|---|---|
| `key` | `str` | 필수 | 보수 · 기본 · 공격 |
| `coverage` | `str` | 필수 | 며칠치인가 |
| `knob` | `str` | 필수 | 무엇으로 조절한 안인가 |
| `qty_kg` | `float` | 필수 | 사는 양 (kg) |
| `amount_krw` | `int` | 필수 | 예상 금액 (원) |
| `unit_price` | `int` | 필수 | 등급 단가 (원/kg) |
| `grade` | `str` | 필수 | 배정된 등급 |
| `max_price` | `int` | 필수 | 재무 스트레스 기준 (amount_max_krw = qty × 이것) |
| `cut_unit_price` | `int &#124; None` | 선택 | 🔴 컷 기준 — 이보다 비싸면 안 산다. 없으면 그 실행에 칸이 없던 것이다 |
| `legs` | `Table` | 필수 | 회차 — 언제 사서 언제 오나 |
| `payments` | `Table` | 필수 | 언제 얼마 내나 |
| `reasons` | `list[Reason]` | 필수 | 이 안을 왜 냈나. 여섯 갈래를 다 채운다 |
| `risks` | `list[str]` | 필수 | 걸리는 것. 비어 있으면 안 적는다 |
| `pending` | `bool` | 필수 | 아직 사람이 안 고른 안인가 |
| `approved` | `bool` | 선택 | 이미 승인된 안인가 |
| `sim_run_id` | `str &#124; None` | 선택 | 어느 걷기의 실행인가. None 이면 걷기 밖(손 실행·축이 생기기 전)이다 |

### `Reason` — 이 안을 왜 냈나 — 한 줄.

| 칸 | 타입 | 필수 | 무엇 |
|---|---|---|---|
| `source` | `str` | 필수 | 예측 · 시세 · 주문 · 재고 · 현금 · 창고 |
| `text` | `str` | 필수 | 근거 한 줄. 숫자를 그대로 적는다 |
| `ref` | `str &#124; None` | 선택 | 근거 꼬리표 |
| `carried` | `bool` | 선택 | 어제 판단에서 이어받은 것인가 |

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

## ★ 이 파트만의 규칙

**승인 버튼은 이 화면에 없습니다.** 승인은 화면 아래 서랍(마스터)에서
`POST /master/runs/{request_id}/decision` 으로 합니다.
이 화면은 **무엇을 고를지 판단할 근거**를 보이는 자리입니다.

**`Reason.ref` 를 반드시 채우세요.** 근거에 꼬리표(`FC-2026-01-06` ·
`MQ-가락-0105`)가 없으면 나중에 되짚을 수 없습니다. 이미 그 형태로
쓰고 있으니 그대로 실으면 됩니다.

**🔴 상한이 둘입니다. 섞지 마세요** (`#398` · `dev@a615aa6` · 2026-09-08).

```text
max_price       재무 STRESS 로 나간다 — 남이 등식을 검사한다
                finance/capabilities/scenario.py   amount_max_krw 등식
                master/verifier.py                 검사 이름 L-PAYSCHED-MAX
cut_unit_price  우리 컷 (self_check.check_max_price)
```

⚠️ **줄 번호를 안 적습니다** — 이름으로 가리킵니다. 2026-09-08 에 `:711` 이
`:734` 로 밀렸고 그 뒤로 또 움직였습니다.

지금은 **같은 값**이고 **컷 산식을 바꾸는 날** 갈라집니다. 화면이
「이보다 비싸면 안 산다」 자리에 `max_price` 를 보이면 그 뒤로 **조용히 틀린
값**이 뜹니다 — 그 자리는 `cut_unit_price` 입니다.

🔴 ~~`09-17` 에 밴드가 바뀌면~~ 은 **낡았습니다** (ML 회신 2026-09-10).
밴드 교체는 `09-03` 에 끝났고 `09-17` 은 «그림자 기록 2주가 차는 날» 입니다.

⚠️ `cut_unit_price` 가 `None` 이면 **`max_price` 로 메우지 마세요.** 그 칸이
생기기 전에 저장된 실행이라는 뜻이고, 메우는 순간 갈라 둔 둘이 화면에서
다시 하나가 됩니다.

**`risks`(걸리는 것)를 지우지 마세요.** 비어 있으면 화면이 그 자리를
안 그립니다. 있는데 안 실으면 위험을 숨기는 것이 됩니다.

**확정 매입이 없으면 빈 표**로 두고 `empty_text` 로 이유를 적습니다 —
"아직 확정된 매입이 없습니다 — 위에서 안을 고르면 여기에 생깁니다".
0건과 «아직 안 골랐다» 는 다릅니다.

## ★ `sim_run_id` — 매입은 **㉮ 로 정했습니다** (2026-09-10)

⚠️ 위 「아직 안 정한 것」은 공용 안내입니다. **매입은 정했습니다.**

```
GET /api/purchase?as_of=2026-01-22&sim_run_id=SIM-BURNIN-202512
```

마스터가 축 이름 규칙(`SIM-{RUN_TYPE}-{YYYYMM}[-{구분}]`)을 통보하며
*"상수로 박지 말고 **받아서 그대로 흘려** 달라"* 고 했습니다. 그래서
주소 파라미터로 받습니다.

🔴 **안 주면 안 거릅니다.** 축이 붙기 전 실행이 1,202건 있어서(실측)
무조건 걸러 버리면 스무 날이 통째로 빕니다.

⚠️ 다른 탭 셋은 아직 상수(`ledger_repository.BURN_IN_SIM_RUN_ID`)를 씁니다.
그 상수 주석이 *"여러 개가 되면 요청 파라미터로 올린다"* 이므로 매입이
**먼저 그 자리에 간 것**입니다.

**🔴 거르는 자리는 SQL 이 아니라 파이썬입니다.** `_read` 는 전부 읽고
`_pick` · `_committed` 가 고릅니다. 이유 둘입니다.

```
① 화면이 «전체 몇 건 중 이 걷기 몇 건» 을 말하려면 전체를 봐야 합니다.
   WHERE 로 걸러 오면 뺀 수를 셀 수 없고, 그러면 조용히 없애는 것이 됩니다
② 검사가 `_read` 를 대신 세워 상황을 주입합니다. WHERE 에 두면 그 주입이
   필터를 건너뛰어 축이 도는지를 못 잽니다
```

**🔴 이름을 쪼개 뜻을 읽지 마세요.** `SIM-WALK-202601-BASE` 의 `BASE` 는
사람이 목록에서 고를 때 쓰는 꼬리표이고, 뜻은 `sim_runs` 행
(`financing_mode` · `config_json`)이 답합니다. 여기서는 **같은지만** 봅니다.

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
curl "http://127.0.0.1:8000/api/purchase?as_of=2026-01-22"
```

브라우저로는 `http://127.0.0.1:8000/docs` 에서 눌러 볼 수 있습니다.

### 4. 화면으로 보기

```bash
cd frontend && npm install && npm run dev
```

`http://localhost:3000/console/purchase` 을 엽니다. 로그인은 아무 사번·이름이나 됩니다.

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

- [ ] `backend/app/api/purchase/query.py` **만** 고쳤다 (`git status` 로 확인)
- [ ] `build()` 가 예시값이 아니라 DB 에서 읽은 값을 돌려준다
- [ ] 조회가 비었을 때 0 이 아니라 `None`/공란으로 나가고, 이유를 `Note` 에 적었다
- [ ] `Source(filled=True, owner=..., note="어느 표에서 읽었는지")` 로 바꿨다
- [ ] `uv run pytest tests/api -q` 가 전부 통과한다
- [ ] `uv run ruff check app/api` 가 조용하다
- [ ] 화면(`/console/purchase`)을 열어 「예시값」 띠가 사라진 것을 눈으로 봤다
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
