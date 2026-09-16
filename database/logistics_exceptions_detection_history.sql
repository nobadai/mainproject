-- logistics_exceptions 에 detection_history_json 한 칸을 더한다 (2026-09-16 · 물류 · LOG-AGENT-005)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **같은 스키마 변경이 두 곳에 있다.**
--
--     신규 구축  →  database/40_logistics_agent_schema.sql §3 (같은 칸을 만든다)
--     운영 중 DB →  이 파일 (ADD COLUMN)
--
--    어느 하나만 고치면 두 스키마가 조용히 갈린다 (README §2). 칸 정의를 바꾸면
--    반드시 둘 다 고친다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 왜 만드는가 — **과거 기준일의 severity(우선도)를 복원할 근거가 없었다.**
--
--   `touch_exception` 이 같은 문제를 다시 감지할 때 행의 `severity` 를 **덮어쓴다**
--   (`SET severity = …`). 그래서 화면이 과거 `as_of` 를 보면 지금 행의 `severity` 는
--   그 뒤(`last_detected_as_of > 기준일`)에 갱신된 «지금» 값이라, 그날 우선도를
--   증명할 수 없어 `—`("기준일 당시 우선도 확인 불가")로 감춘다.
--
--   ⇒ 감지마다 «그날 · severity» 한 벌을 이 칸에 쌓는다. 그러면
--
--     history 원소 = {"as_of": 감지날짜, "severity": …}
--     Historical 조회 = as_of <= 기준일 중 **가장 늦은 감지**의 severity
--
--   가 되어, 지금 값을 과거로 흘리지 않고 그날 우선도를 그대로 복원한다.
--
-- 🔴 **`severity` 를 걷지 않는다.** 그 칸은 Current 축(최신 감지값 캐시)의 정본으로
--    계속 쓰인다 — 이 칸이 세우는 것은 «날짜별 이력» 하나뿐이다.
--    (정상 흐름에서 `severity` == 최신 이력 원소의 severity 다.)
--
-- 🔴 **하루 1원소.** 걷기는 하루에 AFTER_INBOUND · AFTER_OUTBOUND 두 번 감지할 수
--    있다. 같은 날짜 원소는 **마지막 감지값**으로 교체한다(최고값이 아니다) —
--    HIGH→MEDIUM 이면 그날 최종값은 MEDIUM 이다. writer 가 같은 tx 에서 같은 날
--    원소를 지우고 새로 얹는다.
--
-- 🔴 **CHECK 를 걸지 않는다.** *"배열이어야 한다"* 같은 제약은 매력적이지만, 이 칸이
--    생기기 전에 있던 행은 DEFAULT `[]` 로 남고 그 상태가 정상이다.
--    ⚠️ JSONB · NOT NULL · DEFAULT 는 **JSON 타입과 NULL 여부만** 보장한다 — 배열이라는
--       것도, 원소가 `{as_of, severity}` 라는 것도 DB 는 강제하지 않는다. 그 shape 는
--       production writer 가 보장한다(항상 배열·원소를 쓴다).
--
-- 🔴 **INDEX 를 만들지 않는다.** 이 칸만으로 거르는 질의가 없다 — Historical 은 늘
--    `sim_run_id` 로 먼저 좁히고, severity 선택은 파이썬이 배열에서 한다.
--
-- ⚠️ **Backfill 이 없다.** 과거 감지의 severity 를 되살릴 근거가 어디에도 없다 —
--    지금 `severity`(덮어쓴 최신값)를 과거 날짜로 복제하면 이 칸을 만든 이유를
--    그대로 배반한다. 기존 행은 `[]`(= 기록 없음)로 남고, 화면은 그런 행에 기존
--    fallback(`—`)을 그대로 쓴다. 이 기능이 세우는 것은 «적용 이후 실제 감지 이력»뿐이다.
--
-- ★ **두 번 돌려도 안전하다** (`IF NOT EXISTS`). **additive 다** — 기존 칸을 바꾸지도
--    지우지도 않고, 기존 데이터를 UPDATE/DELETE 하지 않는다. FK/constraint 를 건드리지
--    않고 CASCADE 도 없다.
--
-- 🔴 **배포 순서 — migration 이 먼저, 코드가 나중이다.**
--
--   이 칸을 읽는 production 경로:
--
--     agent/exceptions.py  live_exceptions · live_exceptions_at · resolved_exceptions_on
--     api/logistics/query.py  _severity_at (화면 우선도)
--
--   칸이 없는 DB 에 새 코드를 먼저 올리면 그 경로가 `column does not exist` 로 깨진다.
--   반대 순서(칸 먼저)는 안전하다 — 옛 코드는 이 칸을 안 읽으므로 아무 영향이 없다.

BEGIN;

ALTER TABLE haetdeul.logistics_exceptions
    ADD COLUMN IF NOT EXISTS detection_history_json JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN haetdeul.logistics_exceptions.detection_history_json IS
    '[{as_of, severity}] 감지 이력 (LOG-AGENT-005). 하루 1원소 · 같은 날은 마지막 감지값으로 교체. 🔴 Historical 은 as_of <= 기준일 중 max(as_of) 원소의 severity 로 그날 우선도를 복원한다(배열 순서 비의존). severity 칸은 최신값 캐시로 그대로 두고, 이 칸이 «날짜별 이력» 을 든다. 기존 행은 [] 이며 backfill 하지 않는다 — 그런 행은 화면 fallback(—)을 쓴다.';

COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════
-- 적용 뒤 검증
-- ═══════════════════════════════════════════════════════════════════════════
--
--   ① 칸이 생겼나 — 1행 · jsonb · NOT NULL · DEFAULT '[]'::jsonb 여야 한다
--     SELECT column_name, data_type, is_nullable, column_default
--       FROM information_schema.columns
--      WHERE table_schema = 'haetdeul' AND table_name = 'logistics_exceptions'
--        AND column_name = 'detection_history_json';
--
--   ② 기존 행이 안 바뀌었나 — 전부 '[]' 여야 한다(backfill 없음)
--     SELECT count(*) AS 전체, count(*) FILTER (WHERE detection_history_json = '[]'::jsonb) AS 빈배열
--       FROM haetdeul.logistics_exceptions;
