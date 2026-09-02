# Finance Agent

## Finance LLM 아키텍처

Finance의 Master 연동 경로는 다음과 같다. **밖에서 보이는 계약은 그대로다** —
LangChain 과 Harness 는 재무 내부 구현이다.

```text
Master
  ↓
finance_port
  ↓
FinanceAgentController
  ↓
Finance Harness            ← 지금 무엇이 합법인가를 정한다
  ↓
LangChain Tool-calling Planner (LLM)
  ↓  선택 = 실행 요청
Finance Harness 재검증      ← Registry 직전에 한 번 더
  ↓
FinanceToolRegistry → capability (결정론)
  ↓
Finance Rules (verdict)
  ↓
Finance Finalizer (설명) → 설명 guard
```

책임은 다음과 같이 갈린다.

```text
LangChain   Tool 계약 표현과 tool calling 전송 계층. 업무 로직을 갖지 않는다.
Harness     capability 상태 · 실행 가능 Tool · 권한 · 의존 · 예산 · 중복을 정하고 강제한다.
LLM         (1) 지금 실행 가능한 Tool 중 하나를 고른다 (2) 확정된 결과를 설명한다
Tool        재무 사실을 계산한다 (금액 공식의 유일한 주인)
Rule        재무 verdict 를 정한다
Finalizer   검증된 Evidence 로 고정 문장을 고른다 — 결과를 바꾸지 못한다
```

★ **LLM 의 Tool 호출은 실행 요청이지 실행 권한이 아니다.** LangChain 에 바인딩되는
Tool 객체와 실제로 실행되는 Tool 객체는 같지만, 그 사이에 Harness 승인이 있다.
그래서 `AgentExecutor` 류의 자동 실행 루프를 쓰지 않는다 — 승인 자리가 사라진다.

### capability 소유와 의존

정본은 `capability_graph.py` 하나다. 소유는 **1:1** 이다.

```text
finance_position              → assess_finance_position
cashflow_projection           → project_cashflow
finance_cap                   → calculate_purchase_finance_cap
payment_pressure              → analyze_payment_pressure
scenario_evaluation           → evaluate_purchase_scenario
amount_adjustment_validation  → validate_amount_adjustment
```

선행 조건은 **Tool 이름 순서가 아니라 capability 조건**이다.

```text
calculate_purchase_finance_cap  requires cashflow_projection
analyze_payment_pressure        requires cashflow_projection
validate_amount_adjustment      requires scenario_evaluation
```

조건만 맞으면 어느 것을 먼저 골라도 된다. 그래서 PRE_PURCHASE 는 고정 파이프라인이
아니다 — 투영이 끝난 뒤에는 위치조사·cap·압박도 셋이 모두 합법이고, 그중 무엇을
고를지는 Planner 몫이다.

🔴 **숨은 선행 호출을 없앴다.** 예전에는 투영이 없으면 `calculate_purchase_finance_cap`
과 `analyze_payment_pressure` 가 안에서 `project_cashflow` 를 몰래 돌렸다. 값은 맞았지만
**이력이 실행을 말하지 않았다** — 현금흐름을 만든 적이 없는 실행으로 남았다. 지금은
선행 capability 를 Harness 가 드러내 놓고 강제하고, 그래도 새어 들어온 호출은
`FinancePreconditionMissing` 으로 실패한다. 이것은 **실행 순서 오류이지
`RUNTIME_NOT_READY` 가 아니다** — 재무 데이터는 멀쩡히 있다.

### 반려 사유

Harness 가 막은 이유는 값으로 남는다 (`finance_harness_trace` observation).

```text
TOOL_NOT_EXECUTABLE            지금 필요하지 않거나 이미 채워진 capability
DEPENDENCY_NOT_SATISFIED       선행 capability 가 없다
TOOL_PERMISSION_DENIED         이 mode 의 Tool 이 아니다
TOOL_BUDGET_EXHAUSTED          Tool 호출 상한
DUPLICATE_UNRESOLVED_TOOL_CALL 같은 요청 반복
```

앞의 셋은 회복 가능해 상한 안에서 되묻고(bounded replan), 뒤의 둘은 되물어도 같아서
실행을 접는다.

### Trace

`ExecutionMetadata.observations` 에 `finance_harness_trace` 가 한 덩어리로 남는다.
단계마다 **LLM 이 요청한 것 · Harness 가 허락한 것 · 실제로 돈 것**을 함께 적는다.

```text
step / branch_id
completed_capabilities · missing_capabilities · executable_tools · dependency_status
requested_tool · finalize_requested · selected_tool · executed_tool
denied_tool · denied_reason
tool_calls · llm_calls · replans
```

DB 테이블을 새로 만들지 않는다 — 기존 실행 metadata 를 넓혔을 뿐이다.

### 상한

```text
FINANCE_MAX_TOOL_CALLS   기본 8
FINANCE_MAX_REPLANS      기본 2
```

한 실행 전체에서 공유한다(분기가 늘어도 예산은 늘지 않는다). `0` 은 기본값으로
덮이지 않는다 — **한 번도 부르지 말라**는 뜻이다.

### 사용자 문장과 기계 계약

나누는 기준은 **누가 읽는가** 다. 정본은 `messages.py` 하나다.

```text
사람이 읽는 것   reasoning · payload.verdicts[].reason · HTTP 404 본문
                 → 한국어 · 업무 언어 · 구현 용어 없음

기계가 읽는 것   READY · RUNTIME_NOT_READY · ERROR
                 ok · conditional · reject · FIN-BASE-STRESS
                 Tool 이름 · capability id · payload 키 · missing_data · Trace
                 → 그대로 둔다. 번역하면 프론트·Critic·마스터가 대상을 못 찾는다.
```

읽는 사람은 Tool 도 Capability 도 Harness 도 Planner 도 모른다. 그래서 사용자 문장에는
그 낱말이 없다 — *"무엇이 어떻게 됐고 왜 그런가"* 와 **다음에 할 일**만 있다.

🔴 **기술적 사유를 회신에 싣지 않는다. 대신 잃지도 않는다.** 예전에는 실패한 실행의
`reasoning` 이 예외 문자열 그대로였다 — 사용자가 `Finance tool call limit exceeded` 를
받았다. 지금 그 문자열은 `finance_harness_trace` 의 `failure_reason` · `failure_kind` 로
가고, 사용자에게는 갈래에 맞는 한국어 문장이 나간다.

```text
failure_kind = INVALID_REQUEST  보내 주신 내용을 고쳐야 한다
failure_kind = NOT_READY        자료가 준비되면 다시 보면 된다   (RUNTIME_NOT_READY)
failure_kind = INTERNAL         우리 쪽 사정이다. 잠시 후 다시   (ERROR)
```

`ok` · `conditional` · `reject` 는 **각각 다른 문장**을 받는다. 사용자가 할 일이
다르기 때문이다 — 그대로 진행 / 조정한 뒤 재검토 / 이 조건으로는 어려움.

★ **LLM 경로와 결정론 대체 경로가 같은 문장을 쓴다.** 한쪽만 다듬으면 모델이 죽은
날에만 말투가 달라진다 — 설명이 가장 필요한 날에 설명이 제일 나빠진다.

★ 숫자 비소유는 그대로다. 설명은 여전히 **고정 문장을 고르는** 구조이고
`_validate_ready_reasoning` 이 숫자를 막는다. 금액은 payload 와 Evidence 가 든다.

## 패키지 구조

책임이 어디 사는지가 파일 위치로 보이게 정리했다. 전체 디렉터리 이동보다 **책임 분리와
기존 import 호환**을 우선했으므로, 밖에서 쓰는 모듈은 원래 자리를 지킨다.

```text
app/finance/
├─ adapter.py         Master 경계 번역 (finance_port)      ← main.py 가 import
├─ router.py          HTTP 진입점 · 실행이력/판매 조회      ← main.py 가 import
├─ db.py              영속 계층: 연결·조회 헬퍼 · 데이터 경계 계약 · as-of DataPort 구현
│                                                          ← master · orchestrator 도 import
├─ schemas.py         요청·응답 계약 전체 (어휘·현금흐름·정책·상태·매입·판매·이력)
├─ state.py           한 실행 동안 살아 있는 값
├─ tools.py           결정론 재무 계산 (공식의 유일한 주인)
├─ rules.py           결정론 판정 (verdict 소유)
├─ execution.py       Evidence · DeptMeta(Critic 사이드카) · 실행이력 저장/조회
├─ messages.py        사용자에게 보이는 한국어 문장 (정본)  ← 기계 계약은 담지 않는다
├─ interpretation.py  공개 표면 (legacy/* 재수출)          ← 재무 밖 테스트가 import
├─ application/       Agent 실행 계층
│  ├─ harness.py          합법 행동공간: capability 정책 · Tool 선언/디스패치 ·
│  │                      승인 · 예산 · 중복 차단 · 실행 계약 guard · Trace
│  └─ orchestration.py    수명주기: 분기 · Planner 루프 · 결과 확정 · 설명 · 회신 · 이력
├─ capabilities/      결정론 업무 (Harness 가 부르고, 여기서 계산한다)
│  ├─ procurement.py      컨텍스트 적재 · 위치 조사 · 투영 · Finance Cap · 지급 압박도
│  └─ scenario.py         지급 일정 재구성 · BASE/STRESS overlay · 판정 · 금액 대안 검증
├─ llm/               Provider 통합 (업무 로직 없음)
│  ├─ client.py           설정 · Gemini/Ollama HTTP · 가용성 실패 판별 ·
│  │                      Gemini 전송 형식 낮추기(const → enum, null union → nullable)
│  ├─ planner.py          Planner 계약 · 프롬프트 · 사후 검증 · ChatModel ·
│  │                      LangChain tool-calling Planner · 결정론 Planner · 가용성 대체
│  ├─ finalizer.py        검증된 Evidence 에서 설명 키 선택
│  ├─ runtime.py          레거시 해석 계층 (`/finance/sales` 전용) ← 재무 밖 테스트가 import
│  └─ schemas.py          레거시 해석 계약                      ← 재무 밖 테스트가 import
└─ legacy/            Agent 이전의 결정론 경로 (입구는 `/finance/sales` 하나)
   ├─ deterministic_service.py  Finance A/B 실행
   ├─ scenario_engine.py        결정론 Scenario 실행
   └─ interpretation.py         응답 해설 보강
```

★ **한 응집 영역 = 한 모듈**이다. 늘 같이 열리는 것들을 한 파일에 둔다 — *"재무
Agent 는 어떻게 실행되는가"* 는 `application/orchestration.py`, *"이 호출이 합법인가"* 는
`application/harness.py` 하나면 된다. 파일 경계와 신뢰 경계는 다른 것이고, 후자는
클래스·절·이름으로 지킨다.

책임 경계는 다음과 같다.

```text
harness.py 무엇을 부를 수 있는지 정하고 강제한다 (숫자를 만들지 않는다)
Planner    그중 무엇을 부를지 고른다             (숫자를 만들지 않는다)
capability 허용된 재무 작업을 수행한다
tools.py   금액·현금흐름을 계산한다              (공식의 유일한 주인)
rules.py   PASS/FAIL·verdict 를 정한다
Finalizer  검증된 Evidence 를 설명한다           (고정 문장을 고를 뿐이다)
```

`tool_registry.py` 는 이름을 capability 로 넘기는 일만 한다. 예전에는 이 파일 하나가
디스패치·컨텍스트 적재·두 mode 업무·지급 일정 재구성·Evidence 조립을 모두 들고 있었다.

## 지원 Provider

- Ollama / Gemma
- Gemini API

Finance의 Primary Provider는 `gemini / gemini-3.5-flash-lite`다. Gemini를 사용할 수 없는
일부 가용성 장애에서는 `ollama / gemma3:4b`를 availability fallback으로 사용한다. 다른
Agent가 사용하는 전역 LLM 기본값은 변경하지 않는다.

## 환경 설정

Finance만 Gemini를 사용하려면 다음과 같이 설정한다.

```env
FINANCE_LLM_PROVIDER=gemini
FINANCE_LLM_MODEL=gemini-3.5-flash-lite
FINANCE_GEMINI_API_KEY=<secret>
```

Provider는 FINANCE_LLM_PROVIDER가 있으면 해당 값을 사용하고,
없으면 Finance 기본 Provider인 gemini를 사용한다.
전역 LLM_PROVIDER는 Finance Provider 선택에 상속하지 않는다.

키는 `FINANCE_GEMINI_API_KEY`를 먼저 읽고, 비어 있으면 `GEMINI_API_KEY`를 사용한다.
Finance는 `MASTER_GEMINI_API_KEY`를 읽지 않는다. Finance에서만 Gemini를 활성화하기 위해
전역 `LLM_PROVIDER`를 변경해서는 안 된다.

환경 파일은 절대경로로 `backend/.env`를 먼저 읽고 project-root `.env`를 선택적으로 읽는다.
`override=False`이므로 이미 설정된 process 환경변수가 `.env`보다 우선한다. API 키는 설정
객체, 로그 또는 Repository에 저장하지 않으며 실제 키를 Git에 커밋해서는 안 된다.

### Provider별 기본 모델과 상속 규칙

- Primary Gemini 모델: `gemini-3.5-flash-lite`
- Availability fallback Ollama 모델: `gemma3:4b`
- 명시적인 `FINANCE_LLM_MODEL`이 항상 우선한다.

모델은 Provider에 종속된다. 다음 설정처럼 전역 Provider와 Finance Provider가 다를 때
Finance는 전역 Ollama 모델을 상속하지 않고 Gemini 기본 모델을 사용한다.

```env
LLM_PROVIDER=ollama
LLM_MODEL=gemma3:4b
FINANCE_LLM_PROVIDER=gemini
```

따라서 위 설정의 Finance 모델은 `gemini-3.5-flash-lite`이며, Gemini API에
`gemma3:4b`를 요청해 404가 발생하는 구성을 방지한다.

### Availability fallback

Gemini에서 다음 가용성 오류가 발생하면 같은 Finance 실행의 남은 Planner와 Finalizer는
Ollama/Gemma를 사용한다.

- Gemini API 키 누락
- HTTP 429
- timeout
- network 또는 `URLError`
- HTTP 5xx

다음 오류는 구성이나 계약 결함일 수 있으므로 Ollama로 조용히 우회하지 않는다.

- HTTP 400
- HTTP 401/403
- HTTP 404
- schema 또는 contract 오류
- invalid JSON 및 structured output 오류

Gemini 가용성 오류 후 Gemma가 성공하면 Finance 계산 결과는 그대로 유지되고
`runtime_status=READY`, `llm_status=SUCCESS`가 된다. 이때 실제 사용 모델은
`llm_model=gemma3:4b`이며 `llm_fallback_used=False`다. Provider 전환과 deterministic
finalization fallback을 같은 상태로 취급하지 않는다.

Provider 전환 이력은 Finance가 `ExecutionMetadata.observations`에 추가하는
`finance_llm_provider` observation으로 구분한다.

```json
{
  "observation_type": "finance_llm_provider",
  "primary_provider": "gemini",
  "effective_provider": "ollama",
  "provider_fallback_used": true,
  "provider_fallback_reason": "HTTP_429"
}
```

`provider_fallback_reason`은 `API_KEY_MISSING`, `HTTP_429`, `TIMEOUT`, `NETWORK_ERROR`,
`HTTP_5XX` 중 하나이며 fallback이 없으면 `null`이다. 따라서 명시적으로 Ollama를 선택한
실행은 `primary_provider=ollama`, `effective_provider=ollama`,
`provider_fallback_used=false`로 자동 fallback과 구분된다. 공유 `ExecutionMetadata`
계약에는 Finance 전용 필드를 추가하지 않는다.

## Gemini 응답 처리

Gemini Planner와 Finalizer는 `/v1beta/models/...:generateContent` 및 structured JSON
`responseSchema`를 사용한다. 응답은 첫 번째 part만 읽지 않고 사용할 수 있는 text를
검색한다.

- `thought=true`인 part만 건너뛴다.
- `thoughtSignature`만 존재하는 part는 thought block으로 취급하지 않는다.
- 사용할 수 있는 text가 없으면 명시적으로 실패한다.
- boolean과 Tool 선택 사이의 행동 제약은 JSON 파싱 후 검증한다.
- `HTTPError`를 감싸지 않아 400/401/403/404/429/5xx 상태 코드를 보존한다.

Planner가 반환한 Tool은 현재 mode의 허용 목록과 대조한다. 필요한 capability가 남아 있으면
Planner는 하나의 허용 Tool을 선택해야 하고, 모두 충족되면 Tool 없이 finalize해야 한다.

## LLM 메타데이터 의미

- `DISABLED`: LLM이 비활성화되어 호출되지 않음
- `SUCCESS`: LLM이 호출되고 유효한 결과를 반환함
- `FALLBACK`: LLM이 호출됐지만 실패했거나 사용할 수 없는 결과를 반환함

`runtime_status`와 `llm_status`는 서로 다른 축이다. Planner가 실제로 호출된 뒤 실패하면
성공 실행으로 바꾸지 않는다.

```text
Planner invoked and failed
→ runtime_status=ERROR
→ llm_status=FALLBACK
→ llm_fallback_used=True
```

Finalizer 실패는 기존 deterministic fallback을 사용할 수 있으며, 이때도 실제 Controller의
`used_tools`, `tool_order`, `replans`, `llm_attempts`, `llm_fallback_used`, `elapsed_ms`를
보존한다.

Availability fallback으로 Ollama가 정상 결과를 반환한 경우는 LLM 실행 자체가 성공한
것이므로 `SUCCESS`다. 반면 deterministic fallback은 LLM 결과를 사용하지 못한 상태이므로
`FALLBACK`이며 `llm_fallback_used=True`다.

## Gemma vs Gemini — initial smoke benchmark

2026-08-31에 동일한 deterministic Finance 입력으로 실제 smoke test를 수행했다.

| Case | Gemini 3.5 Flash-Lite | Gemma3:4b |
| --- | ---: | ---: |
| PRE_PURCHASE | 6,905 ms | 27,827 ms |
| Accepted scenario | 3,625 ms | 4,359 ms |
| Rejected scenario | 5,014 ms | 9,015 ms |
| Total | 15,544 ms | 41,201 ms |

Gemini 측 관측 결과는 다음과 같다.

```text
Gemini real API requests: 12
Gemini Planner failures: 0
Gemini Finalizer failures: 0
Gemini structured-output failures: 0
Gemini FALLBACK: 0
Gemini ERROR: 0
Replans: 0
```

Hard invariant도 모두 유지됐다.

- Finance numerical output이 Gemma baseline과 일치했다.
- Evidence가 일치했다.
- verdict/business semantics가 일치했다.
- 선택된 Tools와 순서가 일치했다.
- LLM이 재무 수치를 생성하지 않았다.
- Finance contract validation이 통과했다.

Gemini의 잠정 분류는 `STRONG_CANDIDATE`다. Finance 정책상 Gemini를 Primary로 사용하지만,
이는 실제 Gemini Finance 실행 세 건만을 대상으로 한 초기 smoke benchmark다. benchmark
하나만으로 모든 Production 상황에서의 최종 우승 모델을 단정할 수는 없다. quota 초기화
이후 더 다양한 입력과 반복 안정성을 별도로 평가해야 한다.

## Gemini quota와 자동 테스트

현재 사용한 Gemini free-tier 환경은 하루 약 20회의 요청으로 제한된다. Finance Agent 한 번의
실행에서도 Planner의 반복 호출과 별도 Finalizer 호출 때문에 여러 API 요청을 사용할 수 있다.

```text
PRE_PURCHASE → 5
accepted validation → 3
rejected validation → 4
```

따라서 실제 Gemini API 회귀 테스트를 일반 pytest에 포함하지 않는다. 정상 자동 테스트는
`urllib.request.urlopen`을 mock하여 네트워크와 실제 API 키를 사용하지 않아야 한다.

## 알려진 별도 Evidence 이슈

다음 오류는 PurchaseProposal 호환 경로에서 이전에 관측됐다.

```text
E-EVIDENCE-MISSING
payment_schedule[0].amount_krw
```

이 오류는 최소 Controller smoke 사례에서는 재현되지 않았고 현재 Gemini에 기인한 것으로
판단하지 않는다. Gemini Provider 작업과 분리하여 별도로 조사해야 한다.
