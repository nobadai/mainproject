-- 매입대금이 현금곡선에 **한 번** 실렸다는 사실 — finance_payable_closing_events (2026-09-12)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 실측 근거 (dev@4e74ec7 · DB_SCHEMA=haetdeul)
--
--   haetdeul.payables            484행 · 전부 issued_date = due_date
--   SIM-CHAIN-V5 마감 결과
--     payables 73건 27,484,900원이 daily_closings.purchase_cash_out_krw = 0 인
--     날짜에 매달려 있다 — 현금유출로 **한 번도** 안 잡혔다
--
--   왜 빠지나
--     승인 D일    payable 행이 아직 없다 (pending transition 이 D+1 에 적용된다)
--     마감 D일    조회해도 없으니 0
--     적용 D+1    payable 이 생긴다. issued_date = due_date = D
--     마감 D+1    조건이 effective_cash_date(due_date) == as_of 라 D != D+1 → 0
--
--   그래서 그 payable 은 **영원히** 현금유출에 안 잡힌다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- 🔴 **`== as_of` 를 `<= as_of` 로 바꾸는 것으로 풀지 않는다.**
--
--   OPEN/PARTIAL 은 *"아직 갚지 않았다"* 는 **채무 상태**이지
--   *"아직 현금곡선에 안 실었다"* 가 아니다. 부등호만 열면 기일이 지난 payable 이
--   갚을 때까지 **매일** purchase_cash_out 에 다시 실린다 — 실측 73건이 날마다
--   중복 계상된다.
--
-- ★ 그래서 이 표가 하는 일은 «얼마를 갚았나» 가 아니라
--   **«이 payable 을 어느 날 현금곡선에 실었나»** 하나다.
--
-- 🔴 **실제 지급 처리와 섞지 않는다.** `paid_amount_krw` · `outstanding_amount_krw`
--    · `status` 는 이 판에서 한 칸도 안 건드린다. 그 계약(원금 − 지급 − 취소 =
--    미지급)은 그대로 살아 있고, 여기는 현금곡선 귀속만 적는다.
--
-- ★ **멱등이 DB 에 있다.** `(sim_run_id, payable_id)` PK 가 최종 방어선이고,
--   애플리케이션은 `ON CONFLICT DO NOTHING` 으로 그것에 기댄다. «먼저 SELECT 해서
--   없으면 INSERT» 로는 같은 날 두 번 닫는 경합을 못 막는다.
--
-- ★ **판을 나누지 않는다.** 새 표라 신규 구축과 이관이 같은 문장이다. 두 번 돌려도
--   안전하다 (ALTER 가 한 줄도 없다).

BEGIN;

CREATE TABLE IF NOT EXISTS haetdeul.finance_payable_closing_events (
    -- 실행 축. 이 칸이 있어야 sim-run reset 이 이 표를 **자동으로** 집는다
    -- (`app/master/sim_run_open.py` 가 information_schema 에서 축 있는 표를 읽는다).
    sim_run_id             text           NOT NULL,

    -- haetdeul.payables 의 그 채무.
    payable_id             text           NOT NULL,

    -- 🔴 **현금곡선에 실은 날이다. 계약 기일이 아니다.**
    --
    --   payable 이 늦게 생기면 이 날짜가 `due_date` 보다 뒤다. 그 차이를 없애려고
    --   `due_date` 를 고쳐 쓰면 계약 사실이 사라진다 — 그래서 둘 다 적는다.
    recognized_date        date           NOT NULL,

    -- 그날 현금유출로 실은 금액. 마감 시점의 `outstanding_amount_krw` 다.
    --
    -- ⚠️ `original_amount_krw` 가 아니다. PARTIAL 은 이미 일부가 나갔고, 원금을
    --   실으면 그만큼 과대계상된다.
    recognized_amount_krw  numeric(18,6)  NOT NULL,

    -- 계약 기일. 원장 그대로이고 여기서 고치지 않는다 — 왜 이 날 실렸는지를
    -- 되짚을 때 `recognized_date` 와 나란히 봐야 한다.
    due_date               date           NOT NULL,

    created_at             timestamptz    NOT NULL DEFAULT now(),

    -- 🔴 **한 실행에서 한 payable 은 한 번이다.** 이것이 멱등의 정본이다.
    CONSTRAINT finance_payable_closing_events_pkey
        PRIMARY KEY (sim_run_id, payable_id),

    -- 없는 채무에 귀속이 서지 않는다.
    CONSTRAINT finance_payable_closing_events_payable_fkey
        FOREIGN KEY (payable_id) REFERENCES haetdeul.payables(payable_id),

    -- 음수 현금유출은 이 표가 적을 사실이 아니다.
    CONSTRAINT finance_payable_closing_events_amount_check
        CHECK (recognized_amount_krw >= 0)
);

-- 마감이 매번 도는 질의다: 이 실행에서 **이 날** 실린 것의 합.
CREATE INDEX IF NOT EXISTS finance_payable_closing_events_run_date_idx
    ON haetdeul.finance_payable_closing_events (sim_run_id, recognized_date);

COMMENT ON TABLE haetdeul.finance_payable_closing_events IS
    '매입대금이 일마감 현금곡선에 실린 사실. 한 실행에서 한 payable 은 한 번이다. 실제 지급(paid_amount_krw·status)과 다른 축이고, 여기는 현금곡선 귀속만 적는다.';

COMMENT ON COLUMN haetdeul.finance_payable_closing_events.sim_run_id IS
    '어느 실행의 장부인가. 이 칸이 있어야 sim-run reset 이 이 표를 자동으로 비운다.';

COMMENT ON COLUMN haetdeul.finance_payable_closing_events.recognized_date IS
    '🔴 현금곡선에 실은 날이다. 계약 기일이 아니다 — payable 이 늦게 생기면 due_date 보다 뒤다.';

COMMENT ON COLUMN haetdeul.finance_payable_closing_events.recognized_amount_krw IS
    '실은 금액. 마감 시점의 outstanding_amount_krw 다. original_amount_krw 를 쓰면 PARTIAL 에서 이미 나간 몫까지 다시 센다.';

COMMENT ON COLUMN haetdeul.finance_payable_closing_events.due_date IS
    '계약 기일. 원장 그대로다. recognized_date 와 나란히 봐야 왜 이 날 실렸는지 되짚을 수 있다.';

COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 확인 — 적용 후 이 넷을 돌려 본다
-- ══════════════════════════════════════════════════════════════════════════
--
-- ① 표와 제약이 섰나
--
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint
--    WHERE conrelid='haetdeul.finance_payable_closing_events'::regclass;
--
-- ② 🔴 **비어 있나** — 이 판은 자리를 만들 뿐 사건을 만들지 않는다
--
--   SELECT count(*) FROM haetdeul.finance_payable_closing_events;   -- 기대: 0
--
-- ③ 🔴 **채무 원장이 안 바뀌었나**
--
--   SELECT status, count(*) FROM haetdeul.payables GROUP BY status;
--   -- 기대: 적용 전과 같다. 이 파일은 payables 를 한 행도 안 건드린다.
--
-- ④ 같은 payable 을 두 번 실을 수 없나
--
--   INSERT INTO haetdeul.finance_payable_closing_events
--       (sim_run_id, payable_id, recognized_date, recognized_amount_krw, due_date)
--   VALUES ('X', '<payable>', DATE '2026-01-02', 1, DATE '2026-01-01');
--   -- 같은 문장을 두 번 돌리면 두 번째가 PK 로 막힌다.


-- ══════════════════════════════════════════════════════════════════════════
-- 되돌리기
-- ══════════════════════════════════════════════════════════════════════════
--
--   DROP TABLE IF EXISTS haetdeul.finance_payable_closing_events;
--
-- ⚠️ 지우면 «이 payable 을 어느 날 현금곡선에 실었나» 가 같이 사라진다. 그 뒤 마감을
--   다시 돌리면 이미 실린 payable 이 **그날 다시** 실린다.
