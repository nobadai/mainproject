# 화면용 API — 파트별 연결법

운영 콘솔 여섯 탭에 값을 대는 자리입니다.
**자기 파트 폴더의 `query.py` 안쪽만 고치면 됩니다.** 화면은 안 건드립니다.

> ## ★ 실제로 작업할 때는 이 문서 말고 자기 폴더의 `AGENTS.md` 를 보세요
>
> ```
> app/api/dashboard/AGENTS.md    마스터
> app/api/forecast/AGENTS.md     ML
> app/api/purchase/AGENTS.md     매입
> app/api/finance/AGENTS.md      재무
> app/api/logistics/AGENTS.md    물류
> app/api/sales/AGENTS.md        판매
> ```
>
> 파트마다 **그 파트만의 작업 지시서**입니다. 응답 계약 · 읽을 표 ·
> 그 파트만의 함정 · 검증 명령 · 완료 확인 목록이 다 들어 있어서,
> **다른 문서를 안 봐도 끝까지 갈 수 있습니다.**
>
> `AGENTS.md` 라는 이름이라 **Claude Code · Cursor 가 그 폴더에서 일할 때
> 묻지 않아도 읽습니다.** 코딩 도우미에게 시킬 때 따로 붙여 줄 필요가 없습니다.
>
> 이 README 는 **사람이 전체를 훑는 개요**입니다.
>
> 여섯 `AGENTS.md` 는 `_agent_docs.py` 가 만듭니다 — 계약 표를 실제
> `schema.py` 에서 뽑으므로 **문서가 코드와 어긋날 수 없습니다.**
> 스키마를 고쳤으면 다시 돌리세요:
> `uv run python -m app.api._agent_docs`

---

## 0. 5분 요약

```
① backend/app/api/<내파트>/query.py 를 연다
② build() 안에서 예시값을 지우고 내 DB 에서 읽은 값을 넣는다
③ 맨 아래 Source(filled=False) 를 filled=True 로 바꾼다
④ pytest tests/api 를 돌린다
```

**끝입니다.** 주소도 화면도 안 만듭니다. 이미 있습니다.

| 파트 | 고칠 파일 | 화면 주소 |
|---|---|---|
| 마스터 | `app/api/dashboard/query.py` | `/console` |
| ML | `app/api/forecast/query.py` | `/console/forecast` |
| 매입 | `app/api/purchase/query.py` | `/console/purchase` |
| 재무 | `app/api/finance/query.py` | `/console/finance` |
| 물류 | `app/api/logistics/query.py` | `/console/inventory` |
| 판매 | `app/api/sales/query.py` | `/console/sales` |

---

## 1. 이게 왜 따로 있나

기존 부서 라우터는 **에이전트를 돌리는 문**입니다. 화면이 쓸 수가 없습니다.

```
POST /finance/agent            에이전트를 돌린다
GET  /finance/runs/{run_id}    "그 실행" 의 이력
```

**화면은 `run_id` 를 모릅니다. 날짜 하나만 압니다.**
"1월 6일 현금 잔액 얼마?" 를 물을 자리가 기존 36개 경로에 하나도 없었습니다.

그리고 `/finance/agent` 를 화면이 부르면 **화면을 열 때마다 에이전트가 돕니다.**
느리고, 비싸고, 볼 때마다 답이 달라집니다.

그래서 주소로 갈랐습니다.

```
/finance/agent     에이전트를 돌린다   (기존 · 아무도 안 건드림)
/api/finance       화면에 값을 준다    (여기)
```

**`/api` 로 시작하면 화면용입니다. 규칙은 이 하나뿐입니다.**

---

## 2. 폴더 하나 = 파트 하나

```
backend/app/api/
  primitives.py     여섯 탭이 함께 쓰는 부품 — 표 · 그래프 · 요약칸
  calendar.py       공용 날짜축
  router.py         주소를 모아 붙이는 곳
  <파트>/
    schema.py       응답 모양      (바꾸려면 화면 담당과 같이 본다)
    query.py    ★   여기만 고친다
    routes.py       주소           (거의 안 건드린다)
```

**파일이 안 겹칩니다.** 여섯 명이 동시에 작업해도 충돌하지 않습니다.

---

## 3. 지금은 예시값입니다

`query.py` 마다 맨 아래에 이게 있습니다.

```python
source=Source(
    filled=False,          # ← 아직 안 채웠다
    owner="재무",
    note="app/api/finance/query.py 의 build() 를 채우면 실제 값이 됩니다",
),
```

`filled=False` 면 화면 위에 **주황색 「예시값」 띠**가 뜹니다.

> **예시값** — 재무 파트가 아직 실제 값에 붙이지 않았습니다.
> 이 화면의 숫자는 화면 구성을 보이기 위한 것입니다.

**★ 채우고 나면 반드시 `filled=True` 로 바꾸세요.**
안 바꾸면 진짜 값인데 «예시» 라고 붙어 아무도 안 믿습니다.
반대로 안 채웠는데 `True` 로 두면 **데모 숫자를 실적으로 읽습니다.** 이쪽이 훨씬 위험합니다.

---

## 4. 어떻게 채우나

### 4-1. 지금 모습

```python
# app/api/finance/query.py

def build(as_of: date, state: str) -> FinanceTab:
    return FinanceTab(
        stats=[
            Stat(label="현금 잔액", value="-1,328", unit="만원",
                 detail="12/31 BASE_NO_LOAN 기준", tone="bad"),
            ...
        ],
        ...
        source=Source(filled=False, owner="재무"),
    )
```

### 4-2. 채운 모습

```python
from app.finance.db import fetch_one          # ← 이미 있는 자기 파트 DB 모듈

def build(as_of: date, state: str) -> FinanceTab:
    row = fetch_one(
        "SELECT cash_balance FROM finance_states WHERE as_of = %s AND scenario = %s",
        (as_of, state),
    )
    #  ★ fetch_one 은 **None 을 돌려줄 수 있습니다.** 그날 행이 없으면
    #    0 이 아니라 «없음» 으로 적습니다 — 둘은 뜻이 다릅니다 (6-② 규칙).
    cash = None if row is None else row["cash_balance"] / 10_000   # 원 → 만원

    return FinanceTab(
        stats=[
            Stat(label="현금 잔액",
                 value="—" if cash is None else f"{cash:,.0f}",
                 unit=None if cash is None else "만원",
                 detail=(f"{as_of} 마감이 아직 안 들어왔습니다" if cash is None
                         else f"{as_of} {state.upper()} 기준"),
                 tone="neutral" if cash is None else ("bad" if cash < 0 else "good"),
                 raw=cash),                      # ← 계산용 수도 같이
            ...
        ],
        ...
        source=Source(filled=True, owner="재무",
                      note="finance_states · daily_closings 에서 읽었습니다"),
    )
```

**바뀐 것은 `build()` 안쪽뿐입니다.** 주소도 화면도 그대로입니다.

### 4-3. DB 는 이미 있는 것을 쓰세요

```
app/finance/db.py         재무   (마스터도 이걸 씁니다)
app/logistics/db.py       물류
app/sales/db.py           판매
app/purchase_agent/db.py  매입
app/ml/db.py              ML
```

다섯 다 `fetch_one` · `fetch_all` · `get_db_schema` 를 갖고 있습니다.
**새로 만들지 마세요.** 접속 정보가 두 군데로 갈라집니다.

스키마 이름은 하드코딩하지 말고 `get_db_schema()` 로 받으세요.

---

## 5. 부품 — 무엇으로 그리나

전부 `app/api/primitives.py` 에 있습니다. **화면은 이것만 그릴 줄 압니다.**

### `Stat` — 큰 숫자 한 칸

```python
Stat(label="받을 돈", value="7,305", unit="만원",
     detail="매출채권 15건 · 아직 수금 0원",
     tone="warn",      # neutral · good · warn · bad · info · sim
     raw=7305)         # 계산·정렬에 쓸 수. 글자와 따로 담는다
```

**`value` 는 사람이 읽을 글자, `raw` 는 계산용 수입니다.**
자릿점·단위(만원)는 파트마다 다르므로 글자를 그대로 받습니다.

### `Table` — 표

```python
Table(
    columns=[
        Column(key="d",    label="날짜",   mono=True),
        Column(key="cash", label="현금", align="right", mono=True),
    ],
    rows=[
        {"d": "2025-12-31", "cash": "-1,328만원"},
        {"d": "2025-12-30", "cash": None},        # ← 공란. 0 이 아니다
    ],
    empty_text="이 기간에 값이 없습니다",           # 행이 0개일 때 대신 적을 말
)
```

- `columns[].key` 와 `rows` 의 키 이름이 **같아야** 합니다. 다르면 그 칸이 통째로 빕니다
- `align="right"` 는 수치 칸에, `mono=True` 는 자릿수를 맞춰 보고 싶을 때
- **`empty_text` 를 꼭 쓰세요.** "없음" 과 "아직 안 들어옴" 은 다릅니다

### `Chart` — 그래프

```python
Chart(
    label="12월 일별 현금",       # 읽어주는 도구가 쓸 이름
    y_min=-20, y_max=110,
    y_ticks=[0, 50, 100],
    y_unit="M",                  # 눈금 뒤에 붙일 글자
    series=[
        Series(name="실적", data=[58.1, 54.0, None, 49.8], tone="info"),
        Series(name="추정", data=[None, None, 49.8, 45.6], dashed=True),
    ],
    x_labels=["12/02", "", "", "12/31"],   # 공용 날짜축을 안 쓸 때만
)
```

**★ 값의 단위와 눈금 글자가 다르면 `y_labels` 를 쓰세요.**

```python
# 재고는 kg 로 그리는데 눈금은 톤으로 적어야 한다
y_ticks=[10000, 20000],
y_labels=["10t", "20t"],     # 없으면 "10,000t" 이 되어 틀린다
```

실제로 이렇게 틀렸었습니다.

### `Note` — 설명 상자

```python
Note(tone="warn",
     text="★ 둘째 칸은 **0 이 아니라 공란**입니다 — 그날 보고가 없었습니다.")
```

`**굵게**` 만 알아듣습니다. 다른 표시는 글자 그대로 나옵니다.

### `Card` · `Pane` — 카드가 많을 때

재고·물류와 판매가 씁니다. 지금은 물류 7장 · 판매 4장인데, 데모에는 물류가
15장이라 앞으로 늘어납니다 — **늘려도 화면은 안 고칩니다.**

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
**카드를 하나 더 넣어도 화면은 안 고칩니다** — 목록이라 그냥 늘어납니다.

---

## 6. 지켜야 할 것 다섯

### ① `/api` 아래는 읽기만 합니다

쓰기는 기존 경로가 합니다 (`POST /master/runs/{id}/decision`).
여기에 POST 를 만들면 같은 일을 두 군데서 하게 되고,
**«어느 쪽으로 승인했나» 가 기록에서 갈립니다.**

테스트가 막습니다 — `test_쓰기는_막혀_있다`.

### ② `None` 은 0 이 아닙니다

```python
data=[21400, None, 19800]     # 가운데는 그날 보고가 없었다
data=[21400, 0,    19800]     # 가운데는 재고가 없었다      ← 뜻이 다르다
```

값이 없으면 `None` 을 넣으세요. 화면이 **선을 끊고** 표에 **`—`** 를 그립니다.
0 으로 채우면 «그날 재고가 없었다» 는 **거짓말**이 됩니다.

그리고 왜 없는지를 `Note` 에 적으세요. 안 적으면 보는 사람이 0 으로 읽습니다.

### ③ 그래프 계열 길이는 날짜축과 같아야 합니다

화면은 **칸 번호로만** 위치를 잡습니다.
계열이 짧으면 오류 없이 **조용히 왼쪽으로 밀립니다.**
"그날 값이 그랬구나" 로 읽히는 것이 제일 위험합니다.

날짜축 길이는 응답 안 `axis.days` 로 옵니다.

### ④ 대시보드는 숫자를 만들지 않습니다

```
마스터는 숫자를 만들지 않는다.
부서 값을 날짜 축에 놓고, 없으면 공란으로 둔다.
```

`app/api/dashboard/query.py` 는 다른 파트의 `build()` 를 불러 **골라 담기만** 합니다.
대시보드에 자기 파트 값을 얹고 싶으면 **자기 `query.py` 에 함수를 만들고**
대시보드가 그걸 부르게 하세요. 재무·물류가 이렇게 하고 있습니다.

```python
# app/api/logistics/query.py
def dashboard_stock(n: int, at: int) -> Chart: ...

# app/api/dashboard/query.py
stock = logistics_q.dashboard_stock(n, at)
```

**안 그러면 갈라집니다.** 실제로 갈라졌었습니다 —
재고 요약은 4,550kg 인데 그래프 끝은 14,600kg 이었습니다.

### ⑤ 백엔드를 고치면 서버를 다시 올리세요

`--reload` 없이 띄우면 코드를 고쳐도 안 바뀝니다.
새 칸을 더하고 서버를 안 올려서 **화면이 통째로 죽은 적**이 있습니다.

---

## 7. 확인법

### 값이 나오나

```bash
curl "http://127.0.0.1:8000/api/finance?as_of=2025-12-31&state=base"
```

브라우저로 보려면 `http://127.0.0.1:8000/docs` 에서 눌러 보면 됩니다.

### 모양이 맞나

```bash
cd backend
uv run pytest tests/api -q
```

18개가 돕니다. **값은 안 봅니다 — 모양만 봅니다.**
채우면서 숫자가 바뀌는 건 당연하니, 숫자를 잡으면 테스트를 지우게 되고
그러면 검사가 사라집니다.

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

### 화면으로 보나

```bash
# 백엔드
cd backend && uv run uvicorn app.main:app --port 8000

# 프론트 (다른 창)
cd frontend && npm run dev
```

**★ `localhost` 로 여세요. `127.0.0.1` 은 흰 화면이 뜹니다.**
Next 개발 서버가 `127.0.0.1` 을 다른 사이트로 보고 막습니다.

---

## 8. 파트별 — 어디에 무엇이 들어가나

### 마스터 · 대시보드

`app/api/dashboard/query.py` → `/console`

| 자리 | 무엇 |
|---|---|
| `badges` | 상단 알약 — 장 열림 · 승인 대기 |
| `stats` | 요약 다섯 칸 — **다섯 파트에서 하나씩** |
| `forecast_cards` | 세 품목 내일 예측 (ML 것을 그대로) |
| `purchase` | 승인 기다리는 안 |
| `cash_chart` / `stock_chart` | 재무 · 물류가 만들어 준 것 |
| `sources` | 다섯 파트의 채움 여부 |

**여기서 계산하지 마세요.** 4-⑥ 규칙이 여기 걸립니다.

### ML · 가격 예측

`app/api/forecast/query.py` → `/console/forecast`

| 자리 | 무엇 |
|---|---|
| `cards` | 세 품목 내일 예측 · 구간 · 폭 |
| `chart` | 실측 · 전일 예측 · 내일 예측 구간 |
| `accuracy` | **우리가 얼마나 틀리나** |
| `quality` | 어느 조합을 써도 되나 |
| `caveat` | 가운데 값만 보고 사면 안 된다는 경고 |

**★ `accuracy` 와 `caveat` 를 지우지 마세요.** 예측선만 그리면 정답처럼 보입니다.
1,000원짜리를 배추는 197원 틀립니다.

여섯 중 **여기만 실제 값에 붙어 있습니다.** 못 읽으면 예시값으로 떨어집니다 —
화면이 통째로 죽는 것보다 낫습니다.

### 매입

`app/api/purchase/query.py` → `/console/purchase`

| 자리 | 무엇 |
|---|---|
| `stats` | 오늘 제안 · 승인 대기 · 확정 매입액 · 입고 예정 |
| `plans[]` | 안 하나 — 수량 · 금액 · 상한가 · 회차 · 지급 · 근거 · 걸리는 것 |
| `plans_note` | 안이 왜 이 개수인가 |
| `committed` | 승인을 거친 뒤에 생기는 확정 매입 |

**★ `Reason.ref` 를 꼭 채우세요.** 근거에 꼬리표(`FC-2026-01-06`)가 없으면
나중에 되짚을 수 없습니다.

**★ 승인 버튼은 이 화면에 없습니다.** 승인은 아래 서랍(마스터)에서
`POST /master/runs/{request_id}/decision` 으로 합니다.
이 화면은 **무엇을 고를지 판단할 근거**를 보이는 자리입니다.

### 재무

`app/api/finance/query.py` → `/console/finance`

| 자리 | 무엇 |
|---|---|
| `states` / `selected` | 저장된 기준 상태 (BASE_NO_LOAN · LOAN_BASELINE) |
| `stats` | 현금 · 최소 운영자금 · 받을 돈 · 부채 |
| `cash_chart` | 한 달 일별 현금 |
| `flows` | 돈이 어디로 나가고 들어왔나 (원장 용어 대신 사람 말로) |
| `balances` | 받을 돈 · 줄 돈 |
| `closings` | 최근 일별 마감 |
| `tables_read` | 어느 표를 읽었나 — 화면 아래에 적힙니다 |

**★ 조회 전용입니다.** 상태 전이는 승인 트랜잭션(`apply_approval`) 안에서만 일어납니다.

**★ `StateOption` 은 «대출 승인 결과» 가 아닙니다.** DB 에 저장된 기준 상태입니다.
데모에도 그렇게 적혀 있습니다.

### 물류 · 재고

`app/api/logistics/query.py` → `/console/inventory`

안에서 넷으로 나뉩니다.

| 작은 탭 | `key` |
|---|---|
| 재고 · 예약 | `stock` |
| 입고 처리 | `inbound` |
| 창고 배치 | `warehouse` |
| 출고 · 운송 | `outbound` |

각 `Pane` 은 `stats` 와 `cards[]` 를 가집니다.
**카드를 더해도 화면은 안 고칩니다.**

대시보드에 얹는 재고 그래프는 `dashboard_stock(n, at)` 이 만듭니다.
요약 숫자와 **같은 곳(`ONHAND_NOW`)에서 나와야** 둘이 안 갈라집니다.

### 판매

`app/api/sales/query.py` → `/console/sales`

| 자리 | 무엇 |
|---|---|
| `stats` | 총 판매금액 · 판매량 · 공헌이익 · 아직 받을 돈 |
| `cards[]` | 한눈에 · 최근 내역 · 언제 돈이 들어오나 · 알기 쉬운 해석 |

**★ 조회 전용입니다.** 시나리오를 돌리지 않고 저장된 결과만 봅니다.

---

## 9. 모양을 바꾸고 싶으면

`schema.py` 를 고치면 됩니다. 다만 **화면도 같이 고쳐야 합니다.**

```
backend/app/api/<파트>/schema.py     ←→     frontend/src/lib/screen.ts
```

**한쪽만 고치면 그 칸이 조용히 빕니다.** 오류가 안 납니다.
화면 담당(ML 파트)에게 말해 주세요.

칸을 **더하는** 것은 안전합니다. **이름을 바꾸거나 지우는** 것이 위험합니다.

---

## 10. 막히면

| 증상 | 볼 곳 |
|---|---|
| 화면이 하얗다 | `127.0.0.1` 말고 `localhost` 로 열었나 |
| "백엔드에 닿지 못했습니다" | 백엔드가 떠 있나 · 포트가 맞나 |
| 값이 안 바뀐다 | 백엔드를 다시 올렸나 (`--reload` 없이 띄웠으면 안 바뀝니다) |
| 표의 한 칸이 통째로 비었다 | `columns[].key` 와 `rows` 키 이름이 같나 |
| 선이 엉뚱한 날짜에 있다 | 계열 길이가 `axis.days` 와 같나 |
| 숫자가 두 군데서 다르다 | 대시보드가 값을 직접 만들고 있지 않나 |
| 「예시값」 띠가 안 없어진다 | `Source(filled=True)` 로 바꿨나 |
