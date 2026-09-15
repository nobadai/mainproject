# 현재 backend pytest 실패 분석

기준: GitHub Actions run `94631858172` (`dev → main` PR #703).

이 문서는 pytest를 non-blocking으로 두는 빠른 배포 기간에, 실패를 숨기지 않고
후속 안정화 작업의 범위와 위험을 기록한다. `ruff`, frontend lint/build, image build는
계속 blocking check다.

## 요약

| 담당 | 실패 | 성격 | 운영 영향 가능성 | 다음 조치 |
|---|---:|---|---|---|
| Logistics 화면/API | 2 | 테스트 mock 누락으로 실제 DB 조회 | DB 연결·권한·schema가 깨지면 화면 오류 | read service stub 또는 통합 DB test 분리 |
| Logistics repository | 19 | 새 schedule reader가 기존 DB mock을 우회 | 환경변수 누락 자체는 CI 한정 | `_schedule_lists` fixture/mock 추가 |
| Finance SALES_VALIDATION | 20 | response payload/trace 계약 단절 | 높음: 판매 재무검증의 안별 결과·부족 사유가 사라질 수 있음 | Controller/harness contract 복구 |
| Sales Console | 2 | 새 status 조회의 mock 누락 | DB 연결 실패 시 제안 조회 오류 | `load_sale_statuses` fixture 추가 |
| ML adapter | 1 | no-question 경로의 payload 계약/DB mock 누락 | 질문 누락 요청의 오류 응답 계약 | `latest_base_date` stub 또는 계약 확정 |
| Sales → Finance 통합 | 1 | Finance batch payload 계약 단절의 연쇄 | Finance 항목과 동일 | Finance 안정화 PR에서 재검증 |

총 45건이 실패했고 8,003건은 통과했다. 이 목록은 CI run의 기록이며, 이후 dev
커밋에서 재현 여부를 다시 확인해야 한다.

## Finance 담당 분석

대상은 `test_finance_sales_runtime_status.py` 5건,
`test_finance_sales_validation_batch.py` 11건,
`test_finance_sales_validation_roundtrip.py` 4건이다.

`SALES_VALIDATION`은 `RUNTIME_NOT_READY`로 회신하지만 payload가 비어 있어 단일
경로의 `status`, `scenario_id`, `financial_summary`, `data_quality`, `missing_data`와
batch 경로의 `scenario_results`가 사라진다. trace에도 branch ID가 남지 않는다.

이는 더미 DB를 넣어 해결할 종류가 아니다. Finance controller/harness에서
`build_sales_validation_payload()`의 결과를 단일·batch reply와 trace에 보존하는
계약을 별도 PR로 복구해야 한다. 운영 Sales 재무검증 경로에 영향을 줄 수 있으므로
배포 후 read-only 확인이 필요하다.

## Logistics 담당 분석

화면 API test fixture는 Finance와 Sales만 stub하고 `/api/logistics`는 실제
`build_result()`로 들어간다. CI에는 DB 환경변수가 없어 500 응답이 되고, dashboard
test는 빈 chart를 읽어 후속 실패한다.

repository tests는 기존 `fetch_all`과 `get_db_schema`만 mock한다. 새
`_schedule_lists()`가 별도로 `get_connection()`을 열어 mock을 우회한다. 각 테스트가
이미 메모리 fixture row를 보유하므로 production DB나 seed가 필요한 것이 아니라 새
조회 seam에 대한 mock 결과가 필요하다.

## Sales 및 ML 담당 분석

Sales Console tests는 `load_proposal_rows()`만 stub하고 `load_sale_statuses()`가 실제
DB 연결을 연다. 빈 status fixture를 제공하면 된다.

ML adapter test는 질문이 없는 입력에서 `latest_base_date()`가 실제 DB를 조회해
`forecast_available` payload key가 없는 오류 reply를 받는다. test에서 이 경계를
stub하거나 no-question DB-unavailable 응답 계약을 명시해야 한다.

`test_sales_finance_purchase_runtime_paths.py`의 `scenario_results` KeyError는 Finance
payload 계약 단절의 연쇄다.

## 빠른 배포 중 운영 확인

- `GET /health`
- `GET /api/health`
- read-only Logistics 화면 API
- read-only Sales Console 조회
- Finance SALES_VALIDATION의 단일·batch 응답에서 `missing_data`와 안별 결과 확인

쓰기, agent execution, DB migration 또는 seed 적용은 이 확인 절차에 포함하지 않는다.
