# `database/` — 스키마 파일과 실행 순서

> 마지막 갱신 2026-09-05

**검증 결과는 시점마다 다릅니다. 셋을 섞어 읽지 마십시오.**

| 시점 | 무엇을 세웠나 | 실측 결과 |
|---|---|---|
| 2026-08-30 | 빈 PostgreSQL 17 컨테이너 · 당시 §1 순서(`30_` **없음**) | **38표 · 8뷰** |
| 2026-09-05 | `30_logistics_wms_schema.sql` **하나가 만드는 물류 객체** | **21표 · 2뷰** |
| 2026-09-05 | 현재 §1 순서 전체(`30_` **포함**) | **59표 · 10뷰** |

- **38표 · 8뷰는 2026-08-30 의 옛 결과입니다.** `30_` 추가 후의 총수가 아닙니다.
- 59표 · 10뷰는 §1 에 적힌 9개 파일을 임시 스키마에 실제로 세워 센 값입니다
  (2026-09-05 · 방법은 §6). §1 에 없는 파일(`master_agent_runs.sql` ·
  `ml_calendar_days.sql` 등)은 여기 포함되지 않습니다 — 그래서 공유 DB 의
  62표 · 11뷰와 다릅니다(아래 §1 끝 주석).
- `30_logistics_wms_schema.sql` 이 만드는 21표 · 2뷰는 실 DB 카탈로그와
  컬럼·타입·NULL·DEFAULT·제약·인덱스·주석·뷰정의를 1:1 대조해 **완전히 같음**을
  확인했습니다.

🔴 **`30_logistics_wms_schema.sql` 은 공유 외부 DB(`haetdeul`)에 이미 존재하는 WMS
구조를 저장소로 회수한 파일이며, 그 DB 에 다시 적용하는 것이 이 파일의 목적이
아닙니다.** 목적은 *저장소만으로 같은 스키마를 세울 수 있게 하는 것*입니다 —
공유 DB 는 이미 이 모양이라 돌릴 이유가 없고, 검증도 임시 스키마에서만 했습니다.

---

## 1. 새 DB 를 세울 때 — 이 순서로

```bash
psql "$DSN" -v ON_ERROR_STOP=1 \
  -f database/00_init_schema.sql \
  -f database/10_domain_schema.sql \
  -f database/30_logistics_wms_schema.sql \
  -f database/orchestrator_agent_runs.sql \
  -f database/finance_agent_runs.sql \
  -f database/finance_agent_runs_v22.sql \
  -f database/logistics_agent_runs.sql \
  -f database/sales_agent_runs.sql \
  -f database/master_decisions.sql
```

**순서가 자유롭지 않습니다.**

```text
00_init_schema              스키마를 만든다 — 나머지가 전부 이것을 필요로 한다
10_domain_schema            도메인 표·뷰
30_logistics_wms_schema     ← 10_domain 보다 **뒤**. items · sim_runs ·
                              purchase_items · sales · inventory_lots 를 FK 로 참조한다
orchestrator_agent_runs     ← master_decisions 보다 **먼저**
master_decisions            run_id 가 orchestrator_agent_runs 를 참조한다 (복합 FK)
```

`*_agent_runs` 끼리는 서로를 참조하지 않아 순서가 상관없습니다.

> **시드 데이터는 여기 없습니다.** 이 파일들은 스키마만 만듭니다.

> ⚠️ **이 목록이 공유 DB 전체를 만들지는 않습니다.** 위 9개로 세우면 59표 · 10뷰이고
> 공유 DB 는 62표 · 11뷰입니다(2026-09-05 실측). 차이 4개는 **저장소에 파일은 있으나
> §1 목록에 없는** 것들입니다 — `master_agent_runs` · `ml_calendar_days` ·
> `ml_batch_day_status` · `v_ml_batch_days`(`master_agent_runs.sql` ·
> `ml_calendar_days.sql`). 목록에 넣을지는 **마스터·ML 파트 판단**이라 물류가
> 여기서 고치지 않았습니다.

---

## 2. 이미 데이터가 있는 DB 를 옮길 때 — 위 파일을 쓰지 않습니다

`CREATE TABLE IF NOT EXISTS` 는 **이미 있는 표를 고치지 않습니다.** 그래서 운영
중인 DB 에는 본 DDL 이 아니라 **ALTER 판**을 씁니다.

| 이관 스크립트 | 무엇을 바꾸나 | 언제 |
|---|---|---|
| `master_runs_migration.sql` | `orchestrator_agent_runs` 에 `agent='master'` · `request_id` · `plan` | 2026-08-27 |
| `master_decisions_run_id.sql` | `master_decisions.run_id` + 복합 FK + 인덱스 | 2026-08-30 · **팀 승인 대기** |
| `ml_forecast_view_gate_reason.sql` | `v_ml_price_forecast` 의 `daily[]` 에 `gate_reason` 추가 | 2026-09-03 · **실 DB 적용 대기** |
| `finance/finance_state_daily_unique.sql` | `finance_states` 의 `UNIQUE(sim_run_id, financing_mode, state_date)` · 적용 전 duplicate preflight · 기존 데이터 자동 정리 없음 | 2026-09-05 · **실 DB 적용 대기** |
| `finance/payable_cancellation.sql` | `payables` 의 `CANCELLED` · 취소금액/취소일 · 지급/취소/미지급 금액 항등식. 기존 행은 취소금액 0, 자동 상태 변경·삭제 없음 | 2026-09-05 · **실 DB 적용 대기** |
| `30_logistics_wms_schema.sql` | 물류 WMS 표 21 · 뷰 2 회수 + `inventory_lots` 컬럼 6·제약 5 · `inventory_moves` UNIQUE 1 | 2026-09-05 · **실 DB 에는 이미 있음**(회수) · 신규 구축 DB 에는 필수 |
| `logistics_inventory_lots_nullable.sql` | `inventory_lots.grade` · `derivation_status` NOT NULL 해제. **기존 행 값 변경 없음.** 정상 실입고가 미확정 등급과 비-Burn-in 상태를 NULL 로 표현할 수 있게 함 | 2026-09-05 · **실 DB 적용 대기** |

### ⚠️ `30_logistics_wms_schema.sql` 은 판을 나누지 않았습니다

이 파일 하나가 **신규 구축과 기존 DB 양쪽**을 겸합니다. 나눌 수 없어서입니다 —
두 방향의 의존이 얽혀 있습니다.

```text
uq_inventory_moves_id_lot (기존 표 ALTER)  →  inventory_move_lines (신규) 가 참조
inbound_receipts (신규)                    →  inventory_lots FK (기존 표 ALTER) 가 참조
```

그래서 **의존 순서대로 한 파일에 담고, 모든 문장을 멱등·가산으로만** 썼습니다
(`CREATE TABLE IF NOT EXISTS` · `ADD COLUMN IF NOT EXISTS` ·
`CREATE INDEX IF NOT EXISTS` · 제약은 `pg_constraint` 조회로 감쌈). `DROP` 이
한 줄도 없습니다. 위 §2 규칙이 막으려던 것(두 판이 조용히 갈리는 것)은 **판이
하나라 성립하지 않습니다.**

🔴 **`10_domain_schema.sql` 을 고치지 않았습니다.** `inventory_lots` ·
`inventory_moves` 는 물류 소유지만 그 파일은 여러 파트의 표가 한 덩어리인 pg_dump
스냅샷이라, 물류 변경을 거기 섞으면 같은 변경이 두 곳으로 갈립니다. 대신 30\_ 이
`10_domain` 뒤에서 ALTER 로 더합니다 — 신규 구축도 이 경로를 지납니다.

**같은 변경이 두 곳에 있습니다** — 본 DDL(신규 구축용)과 ALTER 판(이관용).
어느 하나만 고치면 갈립니다. **둘 다 고칩니다.**

🔴 **적어 두는 것만으로는 안 지켜집니다.** 갈려도 어느 쪽도 에러를 안 내고
**서로 다른 스키마가 조용히 생깁니다.** 그래서 검사로 겁니다 —
`backend/tests/master/test_schema_files_agree.py` 가 두 파일의 뷰 본문을 대조합니다.

---

## 3. 파일별 소유

| 파일 | 소유 | 비고 |
|---|---|---|
| `00_init_schema.sql` | 공통 | 스키마 생성만 |
| `10_domain_schema.sql` | ⚠️ **주인 없음** | 아래 §5 |
| `orchestrator_agent_runs.sql` | 마스터 | 오케·Critic·마스터가 **공유**하는 실행이력 |
| `master_decisions.sql` | 마스터 | 사람의 결정. append-only |
| `master_decisions_run_id.sql` | 마스터 | 위의 ALTER 판 |
| `master_runs_migration.sql` | 마스터 | 2026-08-27 ALTER 판 |
| `finance_agent_runs*.sql` | 재무 | |
| `logistics_agent_runs.sql` | 물류 | |
| `30_logistics_wms_schema.sql` | **물류** | WMS 표 21 · 뷰 2 회수 (2026-09-04 cutover 분). 다른 파트 표는 FK 로 가리키기만 한다 |
| `logistics_inventory_lots_nullable.sql` | **물류** | 재고·물류 동작을 바꾸는 변경이라 물류가 낸다. 대상 표(`inventory_lots`)의 본 DDL 은 `10_domain_schema.sql` 안에 있고 그 파일은 여전히 주인이 없다(§5) |
| `25_logistics_runtime_fixture_20260102.sql` | **물류** | 물류가 만들고 물류가 채운다. 런타임 fixture 씨앗 행. 다른 파트는 읽기만 |
| `27_logistics_runtime_fixture_20260105_20260106.sql` | **물류** | 물류가 만들고 물류가 채운다. 런타임 fixture 씨앗 행 · 관통 실행일 쌍. 다른 파트는 읽기만 |
| `sales_agent_runs.sql` | 판매 | |
| `ml_calendar_days.sql` | **ML** | ML 이 만들고 ML 이 채운다. 조사일·경매일·공휴일 달력 + `v_ml_batch_days`. 다른 파트는 읽기만 |
| `mvp_demo_remove_dried_pepper.sql` | 데모 | 스키마가 아니라 데이터 정리 |
| `mvp_demo_remove_pimanul.sql` | 데모 | 스키마가 아니라 데이터 정리 · #216 피마늘 제외 · 실 DB 적용 대기 |

---

## 4. `orchestrator_agent_runs` 를 왜 안 나누나

**오케스트레이터가 마스터 에이전트가 됐지만 표는 그대로 둡니다.** 2026-08-27
DDL 에 이미 적힌 판단이고, 지금도 유효합니다.

```text
agent=master        121건    ← 지금 쓰는 것
agent=orchestrator   21건    ← 8/25 까지의 과거 실행
agent=critic          7건
```

**나누면 안 되는 이유 셋.**

**① 모양이 같습니다.** 마스터의 한 실행도 *"API 한 번 · 요청/응답 원문"* 입니다.
표를 나누면 *"그날 무슨 일이 있었나"* 를 두 곳에서 합쳐 봐야 합니다.

**② 과거를 다시 쓰게 됩니다.** `agent='orchestrator'` 21행은 **그때 실제로 있었던
일**입니다. 표를 옮기거나 이름을 바꾸면 과거 행의 판정을 나중에 바꾸는 셈입니다.

**③ 축이 이미 있습니다.** `agent` 컬럼이 셋을 구분합니다. 나눌 이유가 컬럼 하나로
이미 해결돼 있습니다.

### 이름은 바꾸지 않기를 권합니다

`orchestrator_agent_runs` 라는 이름이 이제 안 맞아 보이지만, 실제로는 **"에이전트
실행 공용 로그"** 입니다. 이름을 바꾸면 145행·모듈 4개·이 파일들이 전부 따라오고,
얻는 것은 이름뿐입니다. 대신 표 COMMENT 로 무엇인지 밝혀 두는 쪽이 쌉니다.

**남는 것은 코드 위치 하나입니다** — `app/master/persistence.py` 가
`app/orchestrator/run_repository.py` 를 임포트합니다. 이건 **표 문제가 아니라
모듈 배치 문제**라, 옮기고 싶으면 DB 를 건드리지 않고 옮길 수 있습니다.

---

## 5. 🔴 `10_domain_schema.sql` 은 사람이 쓴 것이 아닙니다

2026-08-30 이전까지 `database/` 가 덮는 것은 **6개뿐**이었습니다. `items` ·
`partners` · `sales` · `inventory_lots` · `item_storage_policies` ·
`ml_price_forecasts` … **32표와 뷰 8개는 저장소에 없었습니다.** 본 DB 를 이
저장소만으로 세울 수 없는 상태였습니다.

그 빈자리를 메우려고 **살아 있는 test DB 에서 `pg_dump` 로 떴습니다.**

**본 DB 를 세우기 전에 각 파트가 자기 표를 봐야 합니다.**

- 지금 안 쓰는 표가 섞여 있을 수 있습니다 — `agent_runs` · `sim_runs` ·
  `constraint_reviews` · `finance_agent_runs_v22` 는 이름이 겹치거나 옛것으로
  보입니다. **판단은 각 파트 몫입니다.**
- test DB 에서 손으로 바꾼 `CHECK` · `DEFAULT` 가 있으면 그대로 따라옵니다.
- 설계 의도가 주석에 없습니다. 다른 파일들과 달리 **왜 그런지가 안 적혀 있습니다.**

---

## 6. 검증하는 법

빈 컨테이너에 세워 보는 것이 가장 확실합니다. 공용 DB 를 건드리지 않습니다.

```bash
docker run -d --name ddl-check -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:17
```

```bash
for f in 00_init_schema 10_domain_schema orchestrator_agent_runs \
         finance_agent_runs finance_agent_runs_v22 logistics_agent_runs \
         sales_agent_runs master_decisions; do
  docker exec -i ddl-check psql -U postgres -v ON_ERROR_STOP=1 < "database/$f.sql" > /dev/null || echo "실패: $f"
done
docker exec ddl-check psql -U postgres -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='haetdeul'"
```

```bash
docker rm -f ddl-check
```

### 컨테이너를 못 쓸 때 — 임시 스키마에 세워 보고 되돌립니다

`30_logistics_wms_schema.sql` 은 이 방법으로 검증했습니다(2026-09-05). 스크립트의
`haetdeul.` 을 임시 스키마 이름으로 바꿔 한 트랜잭션 안에서 세우고, 실 DB 카탈로그와
대조한 뒤 `ROLLBACK` 합니다.

```text
① 임시 스키마 + 다른 도메인 PK-only stub (items · partners · sim_runs ·
   purchase_items · sales · sale_items)
② inventory_lots · inventory_moves 는 **저장소 10_domain 원문**으로 세운다
   → 30_ 의 ALTER 가 실제로 실행되고 "신규 구축 = 실 DB" 가 증명된다
③ 대상 스크립트 실행 → 실 DB 와 컬럼·타입·NULL·DEFAULT·제약·인덱스·주석·뷰 대조
④ 한 번 더 실행해 멱등 확인
⑤ ROLLBACK
```

🔴 **`haetdeul` 을 건드리지 않습니다** — 이름을 바꿔 돌리므로 FK 가 `haetdeul` 표를
가리키지 않고, 따라서 잠금도 걸리지 않습니다. `haetdeul` 은 **세는 데만** 읽습니다.

**총수(59표 · 10뷰)도 같은 방법으로 쟀습니다** — §1 의 9개 파일을 순서대로 임시
스키마에 세우고 `pg_class` 를 센 뒤 `ROLLBACK` 했습니다. 상단 표의 숫자는 전부
이렇게 실측한 값이며, 계산으로 더한 값이 아닙니다.
