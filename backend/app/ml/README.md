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
| `answer_status` | `ok` · `partial` · `need_clarify` · `out_of_scope` 등 (**소문자**) |
| `answer_routes` | 무엇을 답했나 — `forecast` · `batch` · `perf` (쉼표로 이음) |
| `item` | 배추·무·양파 (가격 갈래일 때) |
| `as_of` | 예측 기준일 |
| `forecasts[]` | 답한 행 — `target_dt` · `predicted` · `lower` · `upper` · `item` · `kind` |
| `model_version` · `forecast_source` | 어느 모델·어느 표에서 읽었나 |
| `use_recommended` | **답에 든 조합을 전부 써도 되나** — 하나라도 막혔으면 `false` (없으면 «모른다») |
| `out_of_range_note` | 예측 범위 밖이라 못 답한 날 (있을 때만) |
| `batch` | 그날 배치 — `status`(사람 말) · `run_id` · `n_ok` · `n_fail` |
| `report` | 그날 AI 점검 보고서의 `ran_at` |
| `performance[]` | 봉인 개봉 성능 아홉 칸 — `item` · `kind` · `avg_price` · `avg_error` · `pct` |
| `models[]` | **지금 도는 모델 셋** — `kind` · `model_ver` · `created_at` · `train_end` · `last_swapped_at` |

🔴 **대문자 라벨을 최상위에 두지 않습니다.** 봉투가 최상위 숫자·대문자 라벨에 근거를
요구하는데(`required_claims`), `"OK"` · `"AUC"` 같은 값에는 댈 수치가 없습니다.
같은 사실은 `forecasts[]` · `performance[]` 안으로 넣습니다.

근거(`Evidence`)의 주소는 payload 를 **그대로** 가리킵니다 —
`forecasts[0].predicted` · `batch.n_ok` · `performance[3].pct`.

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

🟢 **`question` 하나로 정해졌습니다** (마스터 확정 · 2026-09-15). `utterance` · `q` 는
**닫았습니다** — 여러 이름이 열려 있으면 나중에 어느 것이 정본인지 못 정하고, 두
이름으로 다른 값이 오는 날 조용히 한쪽만 읽힙니다.

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

### 5-1. 갈래가 셋입니다 ★ (2026-09-16)

한 질문이 **여럿을 물을 수 있습니다.** 답은 물어본 차례대로 이어 붙입니다.

| 갈래 | 이런 질문 | 답하는 것 |
|---|---|---|
| `forecast` | 「5일 뒤 배추 경락가?」 | 예측 표 · 예상 구간 |
| `batch` | 「오늘 데이터 처리 잘 됐어?」 | 그날 배치 한 줄(상태·시각·단계 수) + 실패한 단계 + **그날 AI 점검 보고서 본문 그대로** |
| `perf` | 「모델 성능 어때?」 | **현재 모델 셋** + 봉인 개봉 성능 아홉 칸 + 그날 재학습 보고서 전부 |

```text
「오늘 상태 어때? 성능도」       → batch · perf
「5일 뒤 배추 경락가랑 배치 상태」 → forecast · batch
```

★ **품목·가격 종류를 안 말하면 되묻지 않고 전부 답합니다** (2026-09-16). 품목이 없으면
배추·무·양파, 가격 종류가 없으면 경락가·중도매가·소매가, 둘 다 없으면 아홉 조합입니다.
날짜가 없으면 예전처럼 오늘 하나입니다. **무엇을 채웠는지 답 첫 줄에 한 줄로 밝힙니다.**
질문 문장으로 오든 `item`·`kind` 값으로 오든 **같은 규칙**입니다 — 값으로 부르는 쪽은
프로그램이라 되물어도 다시 답할 수가 없습니다. 되묻기(`need_clarify`)는 이제 해석기가
**갈래조차 못 고른 때**(`clarify`) 하나뿐입니다. 짝 물음(`asks`)이 오면 채우지 않습니다.
범위 밖 품목(마늘·대파)도 전부로 바꾸지 않고 그대로 거절합니다.

★ **가격 답은 표만 냅니다** (2026-09-16). 「이 조합은 판단에 쓰지 마세요」 경고를
**문장에서 뺐습니다.** 🔴 **값은 그대로입니다** — `use_recommended` · `quality_note` ·
`is_gated` 가 `meta` 와 payload 에 실려 나갑니다. 판단은 마스터가 그 값으로 합니다.
그래서 `use_recommended` 의 뜻을 **«답에 든 조합을 전부 써도 되나»** 로 넓혔습니다 —
하나라도 막혔으면 `false` 입니다. 「가격 알려줘」에 아홉 조합이 나가는데 **첫 조합만**
보고 있어, 막힌 조합(양파 중도매가)이 조용히 묻혔습니다. **답 문장은 안 바뀝니다.**

🔴 **해석기를 하나 더 두지 않았습니다.** LLM 호출은 예전처럼 **한 번**이고, 그 한 번이
`routes` 목록을 돌려줍니다. 갈래를 나눠 읽는 것은 코드가 합니다.

🔴 **한 갈래가 터져도 나머지는 나갑니다.** 못 읽은 갈래만 «…을 읽지 못했습니다» 한 줄로
말하고, 전체 상태는 `partial` 이 됩니다. 전부 못 읽었을 때만 `source_unavailable` 입니다.

**읽는 표** (전부 원본 창고 · `SELECT` 만): `batch_run` · `batch_run_stage` ·
`agent_report` · `prediction_log` · `model_cutover`. 성능 아홉 칸은 표가 아니라
**상수**입니다 (`qa_tools.SEALED_ACCURACY`) — `prediction_log` 로 다시 재지
않습니다 (실험용 모델이 섞여 있습니다).

★ **«현재 모델» 의 출처는 둘입니다** (`qa_tools.current_models()`). 이름·만든 날은
`prediction_log` 의 **최신 기준일 행**(`model_ver` · `model_created_at`), 학습 끝·최근
교체는 `model_cutover` 의 `kind` 별 최신 행(`new_train_end` · `swapped_at` · `note`)
입니다. **`model_cutover` 가 아직 없어도 죽지 않습니다** — 그때 «최근 교체» 칸에
«교체 이력 없음» 이라고 적습니다 (2026-09-16 실측: 두 창고 다 그 표가 없습니다).

🔴 **이름으로는 교체를 알 수 없습니다.** `ops_auc` · `ops_whsl` · `ops_rtl` 은 모델을
갈아 끼워도 그대로 둡니다 — 매입 파트 필터가 이름 정확히 일치라 바꾸면 에러 없이
0건이 됩니다. 그래서 **만든 날**을 같이 적습니다.

★ **교체 시각을 모를 수 있습니다.** `model_cutover.time_known` 이 `false` 면 «최근 교체»
를 **날짜만 + «(시각 미상)»** 으로 적습니다. 되짚어 적은 기록은 백업 폴더 **이름**에서
날짜만 건진 것이 있어 시각이 `00:00` 으로 앉아 있고, 그대로 보이면 «한밤중에 바꿨나» 로
읽힙니다. **칸이 아직 없으면(`None`) 예전처럼 날짜·시각**입니다 — «모른다» 와
«안 알려준다» 는 다릅니다. 긴 `note` 는 **답에 안 적습니다** (원문은 DB 에 그대로).

🔴 **재학습 보고서는 그날 것을 전부 읽습니다** (`agent_reports()`). 하루에 판정 3건 ·
검증 2건이 남는 날이 있고, 마지막 하나만 읽었더니 경락가 검증의 «후보가 나쁩니다» 가
답에서 통째로 빠졌습니다. 판정은 한 줄 요약표, 검증은 비교표 전부입니다.
**가격 종류는 `payload.kind` → 제목 맨 앞 낱말 → «(가격 종류 미상)» 순으로** 가립니다.

★ **시간대가 표마다 다릅니다.** `batch_run.started_at` 은 UTC 라
`AT TIME ZONE 'Asia/Seoul'` 로 돌려 날짜를 자르고, `agent_report.ran_at` 은 이미
한국 시간이라 그대로 자릅니다. 한국 09:00 이 UTC 자정이라 안 돌리면 **09:00 배치가
전날 것으로 세어집니다.**

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
