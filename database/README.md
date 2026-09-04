# `database/` — 스키마 파일과 실행 순서

> 마지막 갱신 2026-08-30 · 검증: 빈 PostgreSQL 17 컨테이너에 아래 순서로 세워
> **38표 · 8뷰**가 서는 것을 확인했습니다.

---

## 1. 새 DB 를 세울 때 — 이 순서로

```bash
psql "$DSN" -v ON_ERROR_STOP=1 \
  -f database/00_init_schema.sql \
  -f database/10_domain_schema.sql \
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
orchestrator_agent_runs     ← master_decisions 보다 **먼저**
master_decisions            run_id 가 orchestrator_agent_runs 를 참조한다 (복합 FK)
```

`*_agent_runs` 끼리는 서로를 참조하지 않아 순서가 상관없습니다.

> **시드 데이터는 여기 없습니다.** 이 파일들은 스키마만 만듭니다.

---

## 2. 이미 데이터가 있는 DB 를 옮길 때 — 위 파일을 쓰지 않습니다

`CREATE TABLE IF NOT EXISTS` 는 **이미 있는 표를 고치지 않습니다.** 그래서 운영
중인 DB 에는 본 DDL 이 아니라 **ALTER 판**을 씁니다.

| 이관 스크립트 | 무엇을 바꾸나 | 언제 |
|---|---|---|
| `master_runs_migration.sql` | `orchestrator_agent_runs` 에 `agent='master'` · `request_id` · `plan` | 2026-08-27 |
| `master_decisions_run_id.sql` | `master_decisions.run_id` + 복합 FK + 인덱스 | 2026-08-30 · **팀 승인 대기** |
| `ml_forecast_view_gate_reason.sql` | `v_ml_price_forecast` 의 `daily[]` 에 `gate_reason` 추가 | 2026-09-03 · **실 DB 적용 대기** |

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
| `sales_agent_runs.sql` | 판매 | |
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
