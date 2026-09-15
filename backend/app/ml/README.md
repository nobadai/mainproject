# 마스터 ↔ ML 배선 — 저쪽이 고칠 세 자리

**2026-09-15 · ML 파트**

우리 쪽 구현은 끝났습니다. **마스터가 「ml」이라는 이름을 알기만 하면 바로 돕니다.**
아래 세 자리가 저희가 손댈 수 없는 곳입니다 (저희는 `app/ml/` 만 고칩니다).

---

## 1. 고칠 세 자리

### ① `app/master/envelope.py:58` — 이름 더하기

```python
AgentName = Literal["finance", "inventory", "purchase", "sales", "ml"]
```

### ② `app/master/envelope.py:194` — 받을 모드

```python
_AGENT_MODES: dict[AgentName, frozenset[Mode]] = {
    ...
    "ml": frozenset({"STATUS_QUERY"}),
}
```

**새 모드를 만들지 않았습니다.** `STATUS_QUERY` 는 *"묻기만 하는 요청"* 이고 저희가
하는 일이 정확히 그것입니다.

`_AGENT_DEPT` 에는 **넣지 마세요.** 저희는 조언자가 아니라 답하는 쪽이라 축 조정을
제안하지 않습니다. 넣으면 저희가 안 쓰는 권한이 열립니다.

### ③ `app/master/bootstrap.py` — 등록 한 줄

```python
from app.ml.wiring import register_ml_agent
register_ml_agent()
```

포트 이름이나 준비 단계가 바뀌어도 **이 한 줄은 그대로**입니다. 저희 쪽에서
흡수합니다. ①②가 안 된 상태에서 불러도 **예외를 안 던지고 `False`** 를 돌려줍니다 —
저희 배선 때문에 마스터 부팅이 죽으면 안 되기 때문입니다.

---

## 2. 저희가 돌려주는 것

```text
포트      app/ml/adapter.py::ml_port
서명      (AgentRequest) -> (AgentReply, ExecutionMetadata)
모드      STATUS_QUERY
```

`payload` 는 이렇게 채웁니다.

| 키 | 뜻 |
|---|---|
| `answer_markdown` | **사람에게 그대로 보여줄 글** (표·굵은 글씨 포함) |
| `qa_status` | `OK` · `PARTIAL` · `NEED_CLARIFY` · `OUT_OF_SCOPE` 등 |
| `item` · `target_kind` | 배추·무·양파 / AUC·WHSL·RTL |
| `as_of` | 예측 기준일 |
| `target_dates` | 답한 대상일 목록 |
| `model_version` · `forecast_source` | 어느 모델·어느 표에서 읽었나 |
| `use_recommended` | 판단에 써도 되는 조합인가 |
| `filled_count` | 복사값이 섞인 행 수 (있을 때만) |
| `out_of_range_dates` | 예측 범위 밖이라 못 답한 날 (있을 때만) |

---

## 3. 부탁 하나 — `answer_markdown` 은 펼치지 말고 그대로 써 주세요

`app/master/answer.py::facts_from_status` 가 payload 의 **키마다 한 줄**을 만드는데,
그 규칙을 그대로 태우면 저희 마크다운 표가 **한 줄로 뭉개집니다.**

```text
지금 규칙   facts.append(Fact(label=..., value=_format(key, value)))
바라는 것   answer_markdown 은 사실 줄로 만들지 말고 본문에 그대로 붙이기
```

**안 해 주셔도 답은 성립합니다.** 그래서 마크다운과 별개로 `item` · `forecasts[]` ·
`model_version` 같은 기계용 칸을 같이 싣습니다. 다만 사람이 읽기에는 마크다운 쪽이
훨씬 낫습니다 — 표·구간·오차율·출처가 한 덩어리로 들어 있습니다.

### 3-1. 화면 쪽 — **새로 만드실 것이 없습니다**

지금 채팅창은 답을 평문으로 그립니다.

```tsx
// frontend/src/components/console/MasterConsole.tsx
<div className="whitespace-pre-wrap text-sm leading-relaxed">{turn.text}</div>
```

그래서 우리 마크다운을 그대로 실어도 **표 대신 막대기와 별표가 보입니다.**

**꾸러미를 늘리실 필요는 없습니다.** 프런트 의존성이 넷뿐이고(`next` · `react` ·
`react-dom` · `recharts`) 마크다운 라이브러리가 없는데, **이미 만들어 둔 것이
있습니다.**

```
frontend/src/components/console/ml/Markdownish.tsx
```

제목 · **표** · 굵은 글씨 · 코드칸 · 목록 · 인용을 그립니다. 우리 답이 쓰는 문법이
정확히 그만큼입니다. `dangerouslySetInnerHTML` 을 쓰지 않습니다 — AI 가 쓴 글이
화면으로 들어오는 통로라, 글자는 전부 React 가 글자로 넣습니다.

```tsx
import { Markdownish } from "./ml/Markdownish";

<Markdownish text={turn.text} />
```

⚠️ **다른 부서 답의 모양이 바뀝니다.** `render_answer` 가 만든 `- 재무 가용 현금 …`
같은 줄을 `Markdownish` 는 **목록으로** 그립니다. 나빠지지는 않지만 달라지므로
확인이 필요합니다. 우리 탭에만 쓰고 싶으시면 봇 말풍선에서 **ML 답일 때만**
갈아 끼우는 것도 됩니다.

🔴 **§3 과 이 절은 둘 다 돼야 뜻이 있습니다.** payload 를 펼치기만 하면 표가 한 줄로
뭉개지고, 화면만 고치면 뭉개진 한 줄을 예쁘게 그릴 뿐입니다.

---

## 4. 지금 구조에서 하나 막혀 있는 것 — 질문 문장이 안 옵니다

`app/master/status_flow.py:110` 이 이렇게 부릅니다.

```python
reply = self.runner.call(agent, "STATUS_QUERY")
```

**`payload` 가 비어서 옵니다.** 그래서 저희는 «무엇을 물었는지» 를 알 수 없습니다.

지금은 이렇게 동작합니다.

```text
질문이 오면    해석해서 값·구간·오차·출처를 마크다운으로 답한다
안 오면        오늘 예측을 갖고 있는지와 무엇을 물으면 되는지를 답한다
```

**빈 요청에 되묻지 않습니다.** 그러면 조회할 때마다 *"ML 이 답하지 못했다"* 가 떠서
진짜 고장과 구분이 안 됩니다.

### 부탁 — 사람 말을 그대로 넘겨주세요

```python
reply = self.runner.call(agent, "STATUS_QUERY", payload={"question": utterance})
```

저희는 `question` · `utterance` · `q` 세 이름을 다 받습니다. **어느 것으로 정하실지
알려주시면 하나로 줄이겠습니다** — 셋을 열어 둔 채로 두면 나중에 어느 것이 정본인지
아무도 모르게 됩니다.

`Intent.item` 이 있으면 `payload["item"]` 으로 같이 주셔도 됩니다. 어휘가 같습니다
(`ItemName` = 배추·무·양파 = 저희 `ITEMS`).

---

## 5. 저희가 답할 수 있는 범위

```text
품목   배추 · 무 · 양파
가격   경락가(AUC) · 중도매가(WHSL) · 소매가(RTL)
날짜   오늘 ~ 18일 뒤
질문   값이 얼마인가 · 얼마나 맞는가 · 믿고 써도 되는가
```

**못 하는 것도 적습니다.** 마늘·대파 같은 다른 품목, 19일 뒤 이상, 과거,
평균·합계 같은 집계, 「왜 오르나」, 사라 말라 판단. 범위 밖이면 `qa_status` 에
`OUT_OF_SCOPE` 로 적고 **`READY` 로 답합니다** — 소관이 아닌 것은 고장이 아닙니다.

---

## 6. Evidence 등급은 `ASSUMED` 입니다

`HARD_ALLOWED_GRADES` 는 `OFFICIAL · VENDOR · SIM_FIXED` 인데, **예측은 관측이
아닙니다.** 저희 값으로 하드 제약을 세우면 *모델이 틀리면 제약도 틀리는* 제약이
됩니다. 그래서 일부러 하드 제약에 못 쓰는 등급으로 내보냅니다.

각 근거의 `ref_ids` 에는 읽은 표와 키를 그대로 적습니다.

```text
ml_price_forecasts:base_dt=2026-09-15,item=배추,kind=AUC,target_dt=2026-09-16
```

---

## 7. HTTP API 는 연결에 쓰지 않습니다

`/ml/qa` (GET·POST)는 **시험용 입구**로 남겨 둡니다. 연결은 같은 프로세스 안에서
함수로 부르는 쪽입니다 — 다른 파트와 같고, 그래야 호출 예산·이력·봉투 검증이 한
줄기로 이어집니다.
