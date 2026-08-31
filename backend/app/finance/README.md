# Finance Agent

## Finance LLM 아키텍처

Finance의 Master 연동 경로는 다음과 같다.

```text
Master
  ↓
finance_port
  ↓
FinanceAgentController
  ↓
Finance Planner
  ↓
Finance Tools / Rules
  ↓
Finance Finalizer
```

Planner는 현재 mode에서 허용된 Finance Tool만 선택한다. 모든 재무 수치, Policy 적용,
Evidence, verdict와 adjustment는 deterministic Finance Tools/Rules가 계산한다. Finalizer는
검증된 Evidence를 설명할 뿐이며 재무 수치나 Policy 값을 생성할 수 없다.

## 지원 Provider

- Ollama / Gemma
- Gemini API

Production 기본값은 `ollama / gemma3:4b`다. Gemini는 선택적으로 활성화할 수 있는 후보이며
아직 Production 기본 Provider가 아니다.

## 환경 설정

Finance만 Gemini를 사용하려면 다음과 같이 설정한다.

```env
FINANCE_LLM_PROVIDER=gemini
FINANCE_LLM_MODEL=gemini-3.5-flash-lite
FINANCE_GEMINI_API_KEY=<secret>
```

Provider는 `FINANCE_LLM_PROVIDER → LLM_PROVIDER → ollama` 순서로 결정한다. Gemini API
키는 `FINANCE_GEMINI_API_KEY`를 먼저 읽고, 비어 있으면 `GEMINI_API_KEY`를 사용한다.
Finance는 `MASTER_GEMINI_API_KEY`를 읽지 않는다. Finance에서만 Gemini를 활성화하기 위해
전역 `LLM_PROVIDER`를 변경해서는 안 된다.

환경 파일은 절대경로로 `backend/.env`를 먼저 읽고 project-root `.env`를 선택적으로 읽는다.
`override=False`이므로 이미 설정된 process 환경변수가 `.env`보다 우선한다. API 키는 설정
객체, 로그 또는 Repository에 저장하지 않으며 실제 키를 Git에 커밋해서는 안 된다.

### Provider별 기본 모델과 상속 규칙

- Ollama 기본 모델: `gemma3:4b`
- Gemini 기본 모델: `gemini-3.5-flash-lite`
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

Gemini의 잠정 분류는 `STRONG_CANDIDATE`다. 다만 이는 실제 Gemini Finance 실행 세 건만을
대상으로 한 초기 smoke benchmark이며, 최종 Production 모델을 선택하기에 충분한 근거가
아니다. quota 초기화 이후 더 다양한 입력과 반복 안정성을 별도로 평가해야 한다.

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
