-- sales.source_order_id NOT NULL 해제 (2026-09-07 · Sales)
--
-- 신규 구축 DB는 `database/10_domain_schema.sql` 이 정본이다.
-- 이 파일은 기존 dev/shared DB 에만 적용한다.
-- 행 데이터는 바꾸지 않고 nullable 제약만 완화한다.
-- PostgreSQL 에서는 이미 nullable 인 칸에 대해 반복 실행해도 안전하다.

BEGIN;

ALTER TABLE haetdeul.sales
    ALTER COLUMN source_order_id DROP NOT NULL;

COMMIT;
