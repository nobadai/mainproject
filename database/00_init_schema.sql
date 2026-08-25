-- 최초 DB 기동 시 가장 먼저 실행된다(파일명 정렬 기준 00_ 이 앞선다).
-- 나머지 *_agent_runs.sql 은 haetdeul 스키마가 있어야 테이블을 만들 수 있다.
CREATE SCHEMA IF NOT EXISTS haetdeul;
