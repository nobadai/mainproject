-- logistics_runtime_fixture 에서 죽은 입고 JSON 두 칸을 걷는다 (2026-09-09 · 물류 · W3-4)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **이 파일은 아직 실 DB 에 적용하지 않았다.** 적용 시점은 따로 정한다.
--
--     신규 구축  →  README §1 순서에서 `30_logistics_wms_schema.sql` **뒤**에 둔다.
--                   `10_domain_schema.sql` 이 두 칸을 만들고 이 파일이 걷는다.
--     운영 중 DB →  이 파일 그대로.
--
-- ⚠️ **`10_domain_schema.sql` 을 고치지 않았다.** 그 파일은 여러 파트의 표가 한
--    덩어리인 pg_dump 스냅샷이고 주인이 없다(README §5). 물류 변경을 거기 섞으면
--    같은 변경이 두 곳으로 갈린다 — `30_` 이 `10_domain` 뒤에서 ALTER 로 더하는 것과
--    같은 규율이고, 걷는 것도 같은 자리에서 한다.
--
-- ⚠️ **`30_logistics_wms_schema.sql` 에 넣지 않았다.** 그 파일은 *"DROP 이 한 줄도
--    없다"* 를 계약으로 적은 파일이라, 거기 DROP 을 넣으면 그 계약이 깨진다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 무엇이 죽었나 — **입고 예정의 정본이 옮겨 갔다.**
--
--   ```text
--   ~W3-1   업무 사실       logistics_runtime_fixture.in_transit_json / confirmed_inbound_json
--   W3-2~   업무 사실       inbound_schedules            ← Reader 전환
--   W3-3~   Writer 도 이동                                ← 두 칸에 아무도 안 쓴다
--   W3-3b   Arrival 판정도 status 로                      ← 두 칸을 아무도 안 읽는다
--   ```
--
--   두 칸을 그대로 두면 *"어느 쪽이 진짜인가"* 가 코드를 읽는 사람에게 계속 남는다.
--   비어 있는 채로 남은 옛 정본이 가장 헷갈리는 상태다.
--
-- 🔴 **적용 전 확인 넷.** 하나라도 어긋나면 걷지 않는다 (2026-09-09 실측값 병기).
--
--   ① 업무 사실이 전부 옮겨 갔나 — JSON 에만 있는 입고가 없어야 한다
--
--     WITH j AS (
--       SELECT DISTINCT x ->> 'inbound_id' AS inbound_id
--         FROM haetdeul.logistics_runtime_fixture f,
--              jsonb_array_elements(f.in_transit_json) x
--        WHERE x ->> 'inbound_id' IS NOT NULL
--       UNION
--       SELECT DISTINCT x ->> 'inbound_id'
--         FROM haetdeul.logistics_runtime_fixture f,
--              jsonb_array_elements(f.confirmed_inbound_json) x
--        WHERE x ->> 'inbound_id' IS NOT NULL)
--     SELECT count(*) FROM j
--      WHERE inbound_id NOT IN (SELECT inbound_id FROM haetdeul.inbound_schedules);
--     -- 0 이어야 한다   (실측 0 · JSON 5건 · schedule 5건 · 값 5/5 일치)
--
--   ② 이 두 칸을 참조하는 DB 객체가 없나 (VIEW · MATERIALIZED VIEW · RULE)
--
--     SELECT dv.relname
--       FROM pg_depend d
--       JOIN pg_rewrite r ON r.oid = d.objid
--       JOIN pg_class dv ON dv.oid = r.ev_class
--       JOIN pg_class src ON src.oid = d.refobjid
--       JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
--      WHERE src.relname = 'logistics_runtime_fixture'
--        AND a.attname IN ('in_transit_json', 'confirmed_inbound_json');
--     -- 0행이어야 한다   (실측 0행)
--
--   ③ 트리거 · 함수 본문 참조
--
--     SELECT tgname FROM pg_trigger
--      WHERE tgrelid = 'haetdeul.logistics_runtime_fixture'::regclass AND NOT tgisinternal;
--     SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--      WHERE n.nspname = 'haetdeul'
--        AND (p.prosrc LIKE '%in_transit_json%' OR p.prosrc LIKE '%confirmed_inbound_json%');
--     -- 둘 다 0행이어야 한다   (실측 0행 · 0행)
--
--   ④ 코드에 실행 참조가 없나 — 주석 말고 SQL 문
--
--     grep -rn "in_transit_json\|confirmed_inbound_json" backend/app database \
--       | grep -vE ":\s*(#|--)"
--     -- database/10_domain_schema.sql 의 컬럼 정의 둘만 남아야 한다   (실측 그대로)
--
-- 🔴 **status 두 칸은 걷지 않는다.**
--
--   ```text
--   in_transit_status · confirmed_inbound_status   살아 있다
--   ```
--
--   그 값은 *"그 축을 확인한 적이 있나"* 를 말하고, 두 Reader 가 실제로 읽는다 —
--   `repository._schedule_source`(화면 축) · `inbound_stock._fixture_row`(도착 축).
--   `UNRESOLVED` 면 목록을 숨기고, 그 밖에는 `inbound_schedules` 가 목록을 정한다.
--   JSON 이 죽었다고 status 까지 걷으면 *"확인 못 한 날"* 을 표현할 자리가 사라진다.
--
-- 🔴 **`confirmed_outbound_json` · `confirmed_outbound_status` 는 안 건드린다.**
--    출고 정규화는 별도 단계(WP-3)다.
--
-- ⚠️ **되돌리기.** `ALTER TABLE ... ADD COLUMN` 으로 칸은 되살릴 수 있지만 **값은
--    못 되살린다** — 그래서 위 ① 이 필수다. 되살려야 할 값이 `inbound_schedules` 에
--    다 있다는 것이 걷어도 되는 유일한 근거다.
--
--     ALTER TABLE haetdeul.logistics_runtime_fixture
--         ADD COLUMN IF NOT EXISTS in_transit_json        JSONB,
--         ADD COLUMN IF NOT EXISTS confirmed_inbound_json JSONB;
--
-- 🔴 **`logistics_inbound_schedules.sql` 보다 반드시 뒤에 돌린다.** 그 파일의
--    Backfill 이 `in_transit_json` 을 읽어 일정을 만든다 — 먼저 걷으면 그 이관이
--    영영 못 돈다. README §1 순서가 그 둘을 이 순서로 둔다.
--
-- ★ **두 번 돌려도 안전하다** (`IF EXISTS`).

BEGIN;

ALTER TABLE haetdeul.logistics_runtime_fixture
    DROP COLUMN IF EXISTS in_transit_json,
    DROP COLUMN IF EXISTS confirmed_inbound_json;

COMMENT ON COLUMN haetdeul.logistics_runtime_fixture.in_transit_status IS
    '운송 중 축을 확인했나. UNRESOLVED = 확인한 적 없음(Reader 가 목록을 숨긴다) · 그 밖 = inbound_schedules 가 목록을 정한다. 🔴 이 칸이 목록을 들지 않는다.';
COMMENT ON COLUMN haetdeul.logistics_runtime_fixture.confirmed_inbound_status IS
    '확정 입고 축을 확인했나. 의미는 in_transit_status 와 같다 — 목록의 정본은 inbound_schedules 다.';

COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════
-- 적용 뒤 검증
-- ═══════════════════════════════════════════════════════════════════════════
--
--   ① 칸이 사라졌나 — 0행이어야 한다
--     SELECT column_name FROM information_schema.columns
--      WHERE table_schema = 'haetdeul' AND table_name = 'logistics_runtime_fixture'
--        AND column_name IN ('in_transit_json', 'confirmed_inbound_json');
--
--   ② 남은 칸이 그대로인가 — 254행 · status 값 분포 불변
--     SELECT in_transit_status, confirmed_inbound_status, count(*)
--       FROM haetdeul.logistics_runtime_fixture GROUP BY 1, 2 ORDER BY 3 DESC;
--
--   ③ 입고 일정이 그대로인가 — 5행 · FIRSTINB 살아 있음
--     SELECT count(*) FROM haetdeul.inbound_schedules;
--     SELECT inbound_id, created_as_of, expected_arrival_date, cancelled_as_of
--       FROM haetdeul.inbound_schedules
--      WHERE inbound_id = 'INB-H1-REQ-FIRSTINB-20260113-1-1';
