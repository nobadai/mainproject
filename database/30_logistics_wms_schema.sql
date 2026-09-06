-- 재고·물류 WMS 스키마 — 실 DB 에만 있던 것을 저장소로 회수한다 (2026-09-05)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **새 설계가 아니다. 회수다.**
--
--   여기 있는 표 21 · 뷰 2 는 **이미 실 DB(test/haetdeul)에 서 있다.**
--   `pallets.note` 가 `WMS Cutover 2026-09-04` 로 적고 있고,
--   `item_packaging_specs.created_at` 이 그날 06:21 UTC 다.
--
--   그런데 `database/` 어디에도 그 DDL 이 없었다. 빈 PostgreSQL 에
--   `README.md` §1 순서대로 넣으면 **이 23 개가 서지 않는다.**
--   `database/README.md` §5 가 경고한 *"본 DB 를 이 저장소만으로 세울 수 없다"*
--   가 그때보다 더 벌어진 상태였다.
--
--   ⇒ 이 파일은 실 DB 의 pg_catalog 에서 그대로 옮겨 적은 것이다.
--     컬럼 · 타입 · NULL · DEFAULT · PK · FK · UNIQUE · CHECK · INDEX ·
--     COMMENT 를 **하나도 바꾸지 않았다.** 고칠 것이 보여도 여기서 고치지 않는다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 실행 위치 — `10_domain_schema.sql` **다음**이다.
--
--   ```text
--   00_init_schema           스키마
--   10_domain_schema         items · partners · sim_runs · purchase_items ·
--                            sales · sale_items · inventory_lots · inventory_moves
--   30_logistics_wms_schema  ← 이 파일. 위의 표들을 FK 로 참조한다
--   ```
--
-- 🔴 **다른 파트 표를 고치지 않는다.** 이 파일이 `sales` · `sale_items` ·
--    `purchases` · `purchase_items` · `partners` · `sim_runs` · `items` ·
--    finance · master 표에 하는 일은 **FK 로 가리키는 것뿐**이다.
--    그쪽 컬럼 · 제약 · 인덱스 · 데이터는 건드리지 않는다.
--
-- ⚠️ **본 DDL 과 ALTER 판을 나누지 않았다** (README §2 의 통상 규칙과 다르다).
--
--   ```text
--   나누면 순서가 안 잡힌다
--     uq_inventory_moves_id_lot (기존 표 ALTER)  →  inventory_move_lines (신규) 가 참조
--     inbound_receipts (신규)                    →  inventory_lots FK (기존 표 ALTER) 가 참조
--     두 방향이 얽혀 "신규 먼저" 도 "ALTER 먼저" 도 성립하지 않는다
--   ```
--
--   ⇒ **한 파일에 의존 순서대로 담고, 모든 문장을 멱등·가산으로만 썼다.**
--     `CREATE TABLE IF NOT EXISTS` · `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` ·
--     `CREATE INDEX IF NOT EXISTS` · 제약은 `pg_constraint` 조회로 감쌌다.
--     그래서 **신규 구축 DB 와 운영 DB 에 같은 파일을 그대로 돌린다.**
--     README §2 의 규칙이 막으려던 것(두 판이 조용히 갈리는 것)은 판이 하나라
--     성립하지 않는다.
--
-- 🔴 **DROP 이 한 줄도 없다.** 기존 표를 다시 만들지 않고, 기존 데이터를 지우지
--    않으며, 기존 값을 바꾸지 않는다. 운영 DB 에 돌려도 이미 있는 것은 건너뛴다.
--
-- ⚠️ **이번 회수에서 고치지 않은 것 둘** (알고 남긴다):
--
--   ① Zone 어휘 불일치
--      `item_storage_policies.storage_zone` = 'COLD_HUMID_0_3' 계열
--      `warehouse_zones.zone_id`            = 'HIGH_HUMIDITY_COLD' 계열
--      둘이 안 맞는다. 임의 Mapping 을 여기서 만들지 않는다 — 어느 쪽으로
--      통일할지가 정해지지 않았고, 지금 정하면 그 임의값이 사실로 굳는다.
--
--   ② 거리구간 운임표(`vehicle_rate_table`)
--      고정 Route 정책(route_code · fixed_fee_krw · standard_minutes)은 후속이다.
--      **기존 km 구간 12 행을 지우거나 바꾸지 않는다** — `deliveries` 15 행이
--      이 체계로 적혀 있어 과거 데이터의 근거가 된다.

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════════
-- §1  창고 물리 골격 — Warehouse → Zone → Location
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.warehouses (
    warehouse_id                            TEXT NOT NULL,
    warehouse_name                          TEXT NOT NULL,
    network_type                            TEXT NOT NULL,
    operation_model                         TEXT NOT NULL,
    contract_type                           TEXT NOT NULL,
    region                                  TEXT,
    area_pyeong                             NUMERIC(10,2),
    area_m2                                 NUMERIC(12,2),
    clear_height_m                          NUMERIC(6,2),
    top_airflow_clearance_m                 NUMERIC(6,2),
    usable_storage_envelope_m               NUMERIC(6,2),
    pallet_standard                         TEXT,
    pallet_length_mm                        INTEGER,
    pallet_width_mm                         INTEGER,
    storage_equipment                       TEXT,
    storage_levels                          INTEGER,
    operational_max_loaded_pallet_height_mm INTEGER,
    handling_equipment                      TEXT,
    handling_equipment_capacity_ton         NUMERIC(6,2),
    working_aisle_mm                        INTEGER,
    geometry_basis                          TEXT NOT NULL,
    source_ref                              TEXT NOT NULL,
    note                                    TEXT,
    created_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT warehouses_pkey PRIMARY KEY (warehouse_id),
    CONSTRAINT ck_warehouses_network_type
        CHECK (network_type IN ('SINGLE_HUB_MVP', 'MULTI_HUB')),
    CONSTRAINT ck_warehouses_operation_model
        CHECK (operation_model IN ('LEASED_SELF_OPERATED', 'OUTSOURCED_3PL')),
    CONSTRAINT ck_warehouses_contract_type
        CHECK (contract_type IN ('LEASE', 'OWNED')),
    CONSTRAINT ck_warehouses_geometry_basis
        CHECK (geometry_basis IN ('SIMULATION_GEOMETRY', 'MEASURED'))
);

COMMENT ON TABLE haetdeul.warehouses IS
    '창고 물리 기준정보. 치수·높이는 실측이 아니라 Simulation Geometry다 (Persona 01 §1).';
COMMENT ON COLUMN haetdeul.warehouses.operational_max_loaded_pallet_height_mm IS
    '적재 Pallet 운영 상한(mm). 기하학적 최대 1650이 아니라 보수적 운영한계 1500이다 (01 §4).';
COMMENT ON COLUMN haetdeul.warehouses.geometry_basis IS
    'SIMULATION_GEOMETRY = 비교매물·면적에서 만든 가정 형상. 실측 전에는 이 값을 바꾸지 않는다.';


CREATE TABLE IF NOT EXISTS haetdeul.warehouse_zones (
    zone_id           TEXT NOT NULL,
    warehouse_id      TEXT NOT NULL,
    zone_code         TEXT NOT NULL,
    zone_name         TEXT NOT NULL,
    zone_kind         TEXT NOT NULL,
    purpose           TEXT NOT NULL,
    temp_min_c        NUMERIC(6,2),
    temp_max_c        NUMERIC(6,2),
    rh_min_pct        NUMERIC(6,2),
    rh_max_pct        NUMERIC(6,2),
    environment_basis TEXT NOT NULL,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    source_ref        TEXT NOT NULL,
    note              TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT warehouse_zones_pkey PRIMARY KEY (zone_id),
    CONSTRAINT uq_warehouse_zones_code UNIQUE (warehouse_id, zone_code),
    CONSTRAINT warehouse_zones_warehouse_id_fkey
        FOREIGN KEY (warehouse_id) REFERENCES haetdeul.warehouses(warehouse_id),
    CONSTRAINT ck_warehouse_zones_kind
        CHECK (zone_kind IN ('STORAGE_RACK', 'WORK_FLOOR')),
    CONSTRAINT ck_warehouse_zones_purpose
        CHECK (purpose IN ('NORMAL_STORAGE', 'RECEIVING_INSPECTION',
                           'HOLD_QUARANTINE', 'OUTBOUND_STAGING')),
    CONSTRAINT ck_warehouse_zones_environment_basis
        CHECK (environment_basis IN ('SIMULATION_ASSUMPTION', 'MEASURED'))
);

COMMENT ON TABLE haetdeul.warehouse_zones IS
    '보관 Zone 과 작업 Floor Area. 온습도는 MVP 가정값이며 센서 실측이 아니다 (Persona 05 §5).';
COMMENT ON COLUMN haetdeul.warehouse_zones.zone_kind IS
    'STORAGE_RACK = 정상재고 Rack Zone / WORK_FLOOR = 검수·HOLD·출고대기 작업 Floor. Capacity 축이 다르다 (01 §7).';
COMMENT ON COLUMN haetdeul.warehouse_zones.environment_basis IS
    'SIMULATION_ASSUMPTION = 온습도 센서 없이 정상 유지된다고 가정한 값 (05 §5).';


-- ★ `UNIQUE NULLS NOT DISTINCT` 는 PostgreSQL 15+ 다. 실 DB 는 17.10 이다.
--   FLOOR_POSITION 은 rack/bay/level 이 NULL 이라, NULL 을 서로 다른 값으로 보는
--   기본 규칙이면 같은 자리를 여러 번 등록해도 안 막힌다.
CREATE TABLE IF NOT EXISTS haetdeul.storage_locations (
    location_id   TEXT NOT NULL,
    warehouse_id  TEXT NOT NULL,
    zone_id       TEXT NOT NULL,
    lane_code     TEXT,
    rack_code     TEXT,
    bay_code      TEXT,
    level_no      INTEGER,
    position_no   INTEGER NOT NULL,
    location_kind TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT storage_locations_pkey PRIMARY KEY (location_id),
    CONSTRAINT uq_storage_locations_address UNIQUE NULLS NOT DISTINCT
        (warehouse_id, zone_id, lane_code, rack_code, bay_code, level_no, position_no),
    CONSTRAINT storage_locations_warehouse_id_fkey
        FOREIGN KEY (warehouse_id) REFERENCES haetdeul.warehouses(warehouse_id),
    CONSTRAINT storage_locations_zone_id_fkey
        FOREIGN KEY (zone_id) REFERENCES haetdeul.warehouse_zones(zone_id),
    CONSTRAINT ck_storage_locations_kind
        CHECK (location_kind IN ('RACK_POSITION', 'FLOOR_POSITION')),
    CONSTRAINT ck_storage_locations_position_no CHECK (position_no > 0),
    CONSTRAINT ck_storage_locations_rack_addressed
        CHECK (location_kind <> 'RACK_POSITION'
               OR (rack_code IS NOT NULL AND bay_code IS NOT NULL AND level_no IS NOT NULL)),
    CONSTRAINT ck_storage_locations_floor_addressed
        CHECK (location_kind <> 'FLOOR_POSITION'
               OR (rack_code IS NULL AND bay_code IS NULL AND level_no IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_storage_locations_zone
    ON haetdeul.storage_locations (zone_id, is_active);

COMMENT ON TABLE haetdeul.storage_locations IS
    'Warehouse→Zone→Lane→Rack→Bay→Level→Position 물리 위치 (02 §6). 한 행 = Pallet 한 자리다 — Zone 물리정본은 kg 가 아니라 Pallet Position 이다 (07 §6).';
COMMENT ON COLUMN haetdeul.storage_locations.location_kind IS
    'RACK_POSITION = Selective Pallet Rack 한 자리 / FLOOR_POSITION = 검수·HOLD·출고대기 Floor 한 자리. 둘 다 Pallet 한 장이다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §2  품목 기준정보 — 포장 · 회전 · Zone 배정
--     🔴 `items` 를 FK 로 가리키기만 한다. `items` 자체는 건드리지 않는다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.item_packaging_specs (
    packaging_spec_id           TEXT NOT NULL,
    item_id                     TEXT NOT NULL,
    package_type                TEXT NOT NULL,
    nominal_unit_weight_kg      NUMERIC(10,3) NOT NULL,
    length_mm                   INTEGER,
    width_mm                    INTEGER,
    height_mm                   INTEGER,
    default_units_per_pallet    INTEGER,
    default_kg_per_pallet       NUMERIC(12,3) NOT NULL,
    max_loaded_pallet_height_mm INTEGER,
    source_ref                  TEXT NOT NULL,
    evidence_grade              TEXT NOT NULL,
    is_default                  BOOLEAN NOT NULL DEFAULT FALSE,
    note                        TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT item_packaging_specs_pkey PRIMARY KEY (packaging_spec_id),
    CONSTRAINT item_packaging_specs_item_id_fkey
        FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id),
    CONSTRAINT ck_item_packaging_specs_type
        CHECK (package_type IN ('NET', 'BOX', 'BULK')),
    CONSTRAINT ck_item_packaging_specs_weight CHECK (nominal_unit_weight_kg > 0),
    CONSTRAINT ck_item_packaging_specs_kg_plt CHECK (default_kg_per_pallet > 0),
    CONSTRAINT ck_item_packaging_specs_grade
        CHECK (evidence_grade IN ('OFFICIAL', 'VENDOR', 'SIM_FIXED', 'ASSUMED'))
);

-- 품목당 기본 규격은 하나다. 부분 UNIQUE 라 제약이 아니라 인덱스로 선다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_item_packaging_specs_default
    ON haetdeul.item_packaging_specs (item_id) WHERE is_default;

COMMENT ON TABLE haetdeul.item_packaging_specs IS
    '품목별 포장·Pallet 환산 기준정보 (02 §8). kg → Pallet Position 환산의 정본이다.';
COMMENT ON COLUMN haetdeul.item_packaging_specs.length_mm IS
    '그물망은 고정형상이 아니므로 NULL 허용 (02 §8). 없는 치수를 지어내지 않는다.';
COMMENT ON COLUMN haetdeul.item_packaging_specs.default_kg_per_pallet IS
    '보수적 Simulation 값이다. 실제 Pallet 적재시험 결과가 아니다 (01 §8).';


CREATE TABLE IF NOT EXISTS haetdeul.item_turnover_policies (
    item_id                          TEXT NOT NULL,
    operational_turnover_target_days INTEGER NOT NULL,
    sell_priority_remaining_days     INTEGER NOT NULL,
    turnover_clock_start             TEXT NOT NULL DEFAULT 'received_at',
    physical_storage_limit_days      INTEGER,
    physical_storage_limit_status    TEXT NOT NULL DEFAULT 'NOT_FIXED',
    policy_status                    TEXT NOT NULL,
    evidence_grade                   TEXT NOT NULL,
    source_ref                       TEXT NOT NULL,
    note                             TEXT,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT item_turnover_policies_pkey PRIMARY KEY (item_id),
    CONSTRAINT item_turnover_policies_item_id_fkey
        FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id),
    CONSTRAINT ck_item_turnover_target CHECK (operational_turnover_target_days > 0),
    CONSTRAINT ck_item_turnover_sell_priority
        CHECK (sell_priority_remaining_days >= 0
               AND sell_priority_remaining_days <= operational_turnover_target_days),
    CONSTRAINT ck_item_turnover_clock_start
        CHECK (turnover_clock_start IN ('received_at', 'harvest_date')),
    -- 미확정을 숫자로 지어내지 못하게 상태와 값을 한 쌍으로 묶는다.
    CONSTRAINT ck_item_turnover_physical_limit
        CHECK ((physical_storage_limit_status = 'NOT_FIXED' AND physical_storage_limit_days IS NULL)
               OR (physical_storage_limit_status = 'FIXED' AND physical_storage_limit_days > 0)),
    CONSTRAINT ck_item_turnover_policy_status
        CHECK (policy_status IN ('SIMULATION_POLICY', 'CONFIRMED_POLICY')),
    CONSTRAINT ck_item_turnover_grade
        CHECK (evidence_grade IN ('OFFICIAL', 'VENDOR', 'SIM_FIXED', 'ASSUMED'))
);

COMMENT ON TABLE haetdeul.item_turnover_policies IS
    '신규 회전목표 계약 (Persona 05). Legacy item_storage_policies 를 대체하지 않고 병행한다 — 판매·매입 전환 완료 전 Legacy 제거 금지 (07 §8). 🔴 3품목만 있다 — Lot 조회에서 INNER JOIN 하면 계약 밖 품목 재고가 사라진다 (아래 주석).';
COMMENT ON COLUMN haetdeul.item_turnover_policies.operational_turnover_target_days IS
    '재고회전 목표일수. 실제 부패기한도 판매불가기한도 아니다 (05 §1·§2).';
COMMENT ON COLUMN haetdeul.item_turnover_policies.sell_priority_remaining_days IS
    '이 잔여일 이하에서 SELL_PRIORITY Signal 을 낸다. 자동 할인·자동 가격조정이 아니다 (05 §7.1).';
COMMENT ON COLUMN haetdeul.item_turnover_policies.physical_storage_limit_days IS
    '물리 저장한계(실제 Shelf-Life). 회전목표와 숫자가 같아도 뜻이 다르다 — 아직 미확정이다 (05 §8.1).';


CREATE TABLE IF NOT EXISTS haetdeul.item_zone_assignments (
    item_id    TEXT NOT NULL,
    zone_id    TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    allowed    BOOLEAN NOT NULL DEFAULT TRUE,
    source_ref TEXT NOT NULL,
    note       TEXT,
    CONSTRAINT item_zone_assignments_pkey PRIMARY KEY (item_id, zone_id),
    CONSTRAINT item_zone_assignments_item_id_fkey
        FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id),
    CONSTRAINT item_zone_assignments_zone_id_fkey
        FOREIGN KEY (zone_id) REFERENCES haetdeul.warehouse_zones(zone_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_item_zone_assignments_default
    ON haetdeul.item_zone_assignments (item_id) WHERE is_default;

COMMENT ON TABLE haetdeul.item_zone_assignments IS
    '품목별 허용 보관 Zone (03 §7). 기본 Zone 을 벗어나려면 zone_override_approvals 가 있어야 한다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §3  입고 — 도착 · 검수
--     🔴 `purchase_items` · `sim_runs` 를 FK 로 가리키기만 한다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.inbound_receipts (
    receipt_id             TEXT NOT NULL,
    sim_run_id             TEXT NOT NULL,
    inbound_id             TEXT,
    purchase_item_id       TEXT,
    item_id                TEXT NOT NULL,
    arrived_at             DATE NOT NULL,
    receiving_location_id  TEXT,
    ordered_qty_kg         NUMERIC(18,6),
    accepted_qty_kg        NUMERIC(18,6),
    hold_qty_kg            NUMERIC(18,6),
    rejected_qty_kg        NUMERIC(18,6),
    estimated_pallet_count INTEGER,
    actual_pallet_count    INTEGER,
    receipt_status         TEXT NOT NULL,
    fact_source            TEXT NOT NULL,
    received_by            TEXT,
    note                   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inbound_receipts_pkey PRIMARY KEY (receipt_id),
    -- B-1 대조 키가 한 실행 안에서 두 번 서지 못하게 막는다.
    CONSTRAINT uq_inbound_receipts_inbound_id UNIQUE (sim_run_id, inbound_id),
    CONSTRAINT inbound_receipts_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    CONSTRAINT inbound_receipts_purchase_item_id_fkey
        FOREIGN KEY (purchase_item_id) REFERENCES haetdeul.purchase_items(purchase_item_id),
    CONSTRAINT inbound_receipts_item_id_fkey
        FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id),
    CONSTRAINT inbound_receipts_receiving_location_id_fkey
        FOREIGN KEY (receiving_location_id) REFERENCES haetdeul.storage_locations(location_id),
    CONSTRAINT ck_inbound_receipts_status
        CHECK (receipt_status IN ('ARRIVED', 'INSPECTING', 'INSPECTED', 'PUTAWAY_DONE', 'CLOSED')),
    CONSTRAINT ck_inbound_receipts_fact_source
        CHECK (fact_source IN ('HUMAN_RECORDED', 'SCENARIO_SIMULATED')),
    -- 미입력(NULL)은 0 으로 보지 않는다 — COALESCE 는 비교용이고 값을 만들지 않는다.
    CONSTRAINT ck_inbound_receipts_qty
        CHECK (COALESCE(ordered_qty_kg, 0) >= 0
               AND COALESCE(accepted_qty_kg, 0) >= 0
               AND COALESCE(hold_qty_kg, 0) >= 0
               AND COALESCE(rejected_qty_kg, 0) >= 0)
);

CREATE INDEX IF NOT EXISTS idx_inbound_receipts_arrival
    ON haetdeul.inbound_receipts (sim_run_id, arrived_at);

COMMENT ON TABLE haetdeul.inbound_receipts IS
    '입고 도착~검수~PUTAWAY 헤더 (03 §1). 재고 IN 수량은 주문수량이 아니라 accepted_qty_kg 다 (02 §2 · 03 §3).';
COMMENT ON COLUMN haetdeul.inbound_receipts.inbound_id IS
    'in_transit / confirmed_inbound_schedule 대조 키 (B-1). 같은 회차가 두 행이면 점유가 이중 계상된다.';
COMMENT ON COLUMN haetdeul.inbound_receipts.estimated_pallet_count IS
    '입고 전 kg/PLT 로 추정한 값. 확정은 검수 후 actual_pallet_count 다 (02 §9).';


CREATE TABLE IF NOT EXISTS haetdeul.inbound_inspections (
    inspection_id    TEXT NOT NULL,
    receipt_id       TEXT NOT NULL,
    inspected_at     TIMESTAMPTZ NOT NULL,
    inspector        TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    inspected_qty_kg NUMERIC(18,6) NOT NULL,
    accepted_qty_kg  NUMERIC(18,6) NOT NULL DEFAULT 0,
    hold_qty_kg      NUMERIC(18,6) NOT NULL DEFAULT 0,
    reject_qty_kg    NUMERIC(18,6) NOT NULL DEFAULT 0,
    fact_source      TEXT NOT NULL,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inbound_inspections_pkey PRIMARY KEY (inspection_id),
    CONSTRAINT inbound_inspections_receipt_id_fkey
        FOREIGN KEY (receipt_id) REFERENCES haetdeul.inbound_receipts(receipt_id),
    CONSTRAINT ck_inbound_inspections_verdict
        CHECK (verdict IN ('PASS', 'HOLD', 'REJECT')),
    CONSTRAINT ck_inbound_inspections_fact_source
        CHECK (fact_source IN ('HUMAN_RECORDED', 'SCENARIO_SIMULATED')),
    -- 항등식이다. 셋의 합이 검수량과 다르면 어느 쪽이 맞는지 아무도 모른다.
    CONSTRAINT ck_inbound_inspections_qty
        CHECK (inspected_qty_kg > 0
               AND accepted_qty_kg >= 0 AND hold_qty_kg >= 0 AND reject_qty_kg >= 0
               AND accepted_qty_kg + hold_qty_kg + reject_qty_kg = inspected_qty_kg),
    -- 판정과 수량이 서로를 배반하지 못하게 한다.
    CONSTRAINT ck_inbound_inspections_verdict_qty
        CHECK ((verdict = 'PASS' AND hold_qty_kg = 0 AND reject_qty_kg = 0)
               OR (verdict = 'HOLD' AND hold_qty_kg > 0)
               OR (verdict = 'REJECT' AND accepted_qty_kg = 0 AND reject_qty_kg > 0))
);

COMMENT ON TABLE haetdeul.inbound_inspections IS
    'MVP 공통 품질검수 결과 (03 §4). 품목별 전문 판정은 고도화 대상이다.';
COMMENT ON COLUMN haetdeul.inbound_inspections.inspected_qty_kg IS
    'accepted + hold + reject 와 정확히 같아야 한다. 도착량과 검수량의 차이는 inbound_receipts 쪽 축이다.';


CREATE TABLE IF NOT EXISTS haetdeul.inbound_inspection_checks (
    check_id      BIGSERIAL NOT NULL,
    inspection_id TEXT NOT NULL,
    check_code    TEXT NOT NULL,
    observed      BOOLEAN NOT NULL,
    severity      TEXT,
    note          TEXT,
    CONSTRAINT inbound_inspection_checks_pkey PRIMARY KEY (check_id),
    CONSTRAINT uq_inbound_inspection_checks UNIQUE (inspection_id, check_code),
    CONSTRAINT inbound_inspection_checks_inspection_id_fkey
        FOREIGN KEY (inspection_id) REFERENCES haetdeul.inbound_inspections(inspection_id),
    CONSTRAINT ck_inbound_inspection_checks_code
        CHECK (check_code IN ('MOLD', 'ROT', 'ODOR', 'APPEARANCE_DAMAGE',
                              'CONTAMINATION', 'PACKAGING_DAMAGE')),
    CONSTRAINT ck_inbound_inspection_checks_severity
        CHECK (severity IS NULL OR severity IN ('LOW', 'MEDIUM', 'HIGH'))
);

COMMENT ON TABLE haetdeul.inbound_inspection_checks IS
    '검수 항목별 기록 — 곰팡이·무름/부패·이상냄새·외관/압상/파손·오염·포장손상 (03 §4). 사람이 웹 Form 으로 넣는다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §4  기존 물류 표의 회수분 — `inventory_lots` · `inventory_moves`
--
--     🔴 **두 표는 물류 소유이고 `10_domain_schema.sql` 에 이미 있다.**
--        실 DB 에서 자란 칸·제약이 그 파일에 안 담겨 있어 여기서 더한다.
--        `10_domain_schema.sql` 은 손대지 않는다 — 그 파일은 pg_dump 스냅샷이고
--        여러 파트의 표가 한 덩어리로 들어 있다. 물류 변경을 거기 섞으면
--        "물류가 남의 파일을 고쳤다" 가 되고, 같은 변경이 두 곳으로 갈린다.
--
--     ★ 순서가 여기인 이유: 아래 FK 가 §2·§3 의 신규 표를 가리킨다.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE haetdeul.inventory_lots
    ADD COLUMN IF NOT EXISTS packaging_spec_id  TEXT,
    ADD COLUMN IF NOT EXISTS harvest_date       DATE,
    ADD COLUMN IF NOT EXISTS inbound_receipt_id TEXT,
    ADD COLUMN IF NOT EXISTS source_partner_id  TEXT,
    ADD COLUMN IF NOT EXISTS inspection_status  TEXT,
    ADD COLUMN IF NOT EXISTS lot_note           TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'fk_inventory_lots_packaging_spec'
                     AND conrelid = 'haetdeul.inventory_lots'::regclass) THEN
        ALTER TABLE haetdeul.inventory_lots
            ADD CONSTRAINT fk_inventory_lots_packaging_spec
            FOREIGN KEY (packaging_spec_id)
            REFERENCES haetdeul.item_packaging_specs(packaging_spec_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'fk_inventory_lots_inbound_receipt'
                     AND conrelid = 'haetdeul.inventory_lots'::regclass) THEN
        ALTER TABLE haetdeul.inventory_lots
            ADD CONSTRAINT fk_inventory_lots_inbound_receipt
            FOREIGN KEY (inbound_receipt_id)
            REFERENCES haetdeul.inbound_receipts(receipt_id);
    END IF;

    -- ⚠️ 다른 도메인(`partners`)을 **가리키기만** 한다. 그쪽 표는 안 바뀐다.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'fk_inventory_lots_source_partner'
                     AND conrelid = 'haetdeul.inventory_lots'::regclass) THEN
        ALTER TABLE haetdeul.inventory_lots
            ADD CONSTRAINT fk_inventory_lots_source_partner
            FOREIGN KEY (source_partner_id)
            REFERENCES haetdeul.partners(partner_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'ck_inventory_lots_inspection_status'
                     AND conrelid = 'haetdeul.inventory_lots'::regclass) THEN
        ALTER TABLE haetdeul.inventory_lots
            ADD CONSTRAINT ck_inventory_lots_inspection_status
            CHECK (inspection_status IS NULL
                   OR inspection_status IN ('PASS', 'HOLD', 'REJECT'));
    END IF;

    -- 검수에서 걸린 물량이 ACTIVE 로 앉아 가용재고에 섞이지 못하게 막는다.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'ck_inventory_lots_hold_not_available'
                     AND conrelid = 'haetdeul.inventory_lots'::regclass) THEN
        ALTER TABLE haetdeul.inventory_lots
            ADD CONSTRAINT ck_inventory_lots_hold_not_available
            CHECK (inspection_status IS NULL
                   OR inspection_status = 'PASS'
                   OR status <> 'ACTIVE');
    END IF;
END
$$;

COMMENT ON COLUMN haetdeul.inventory_lots.harvest_date IS
    '공급자가 준 수확일. 없으면 NULL 이다 — 추정해 만들지 않는다 (05 §3).';
COMMENT ON COLUMN haetdeul.inventory_lots.inspection_status IS
    '입고검수 판정 (PASS/HOLD/REJECT). 검수결과가 다른 물량은 같은 Lot 에 섞지 않는다 (03 §5).';

-- 🔴 **이 주석은 실 DB 의 현재 문구를 그대로 옮긴 것이다** — 물류가 이번 단계에서
--    정본을 옮기기로 정한 것이 아니다. 코드(`app/logistics/repository.py`)는 아직
--    Lot 잔량을 읽고 `inventory_moves` 를 읽지 않는다. 문구와 코드가 갈려 있다는
--    사실 자체가 회수 대상이라 원문 그대로 둔다 (판단은 다음 단계 몫이다).
--    ⚠️ 문구가 가리키는 `23_inventory_move_type_split.sql` 은 저장소에 없다.
COMMENT ON COLUMN haetdeul.inventory_lots.remaining_qty_kg IS
    'DERIVED_CACHE — inventory_moves 집계의 현재 잔량이다. 정본은 원장이다: IN - OUT - DISPOSE + ADJUST_IN - ADJUST_OUT (02 §3·§14). ADJUST_IN/ADJUST_OUT 어휘는 23_inventory_move_type_split.sql 적용 후에 쓸 수 있다. 직접 UPDATE 로 뜻을 만들지 않는다.';


-- `inventory_move_lines` 의 복합 FK 가 이 UNIQUE 를 필요로 한다.
-- Move 한 건의 Line 이 **다른 Lot** 을 가리키지 못하게 하는 장치다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'uq_inventory_moves_id_lot'
                     AND conrelid = 'haetdeul.inventory_moves'::regclass) THEN
        ALTER TABLE haetdeul.inventory_moves
            ADD CONSTRAINT uq_inventory_moves_id_lot UNIQUE (move_id, lot_id);
    END IF;
END
$$;


-- ═══════════════════════════════════════════════════════════════════════════
-- §5  Pallet — 물리 취급단위
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.pallets (
    pallet_id           TEXT NOT NULL,
    lot_id              TEXT NOT NULL,
    packaging_spec_id   TEXT,
    current_location_id TEXT,
    status              TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    emptied_at          TIMESTAMPTZ,
    note                TEXT,
    CONSTRAINT pallets_pkey PRIMARY KEY (pallet_id),
    -- 한 자리에 Pallet 은 하나다.
    CONSTRAINT uq_pallets_location UNIQUE (current_location_id),
    -- `inventory_move_lines` 의 복합 FK 대상 — Line 의 Pallet 과 Lot 이 갈리지 못한다.
    CONSTRAINT uq_pallets_id_lot UNIQUE (pallet_id, lot_id),
    CONSTRAINT pallets_lot_id_fkey
        FOREIGN KEY (lot_id) REFERENCES haetdeul.inventory_lots(lot_id),
    CONSTRAINT pallets_packaging_spec_id_fkey
        FOREIGN KEY (packaging_spec_id) REFERENCES haetdeul.item_packaging_specs(packaging_spec_id),
    CONSTRAINT pallets_current_location_id_fkey
        FOREIGN KEY (current_location_id) REFERENCES haetdeul.storage_locations(location_id),
    CONSTRAINT ck_pallets_status
        CHECK (status IN ('ACTIVE', 'HOLD', 'EMPTIED', 'DISPOSED')),
    -- 살아 있는 Pallet 은 자리가 있고, 비운 Pallet 은 자리를 차지하지 않는다.
    CONSTRAINT ck_pallets_location_matches_status
        CHECK ((current_location_id IS NOT NULL) = (status IN ('ACTIVE', 'HOLD'))),
    CONSTRAINT ck_pallets_emptied_at_required
        CHECK (status <> 'EMPTIED' OR emptied_at IS NOT NULL),
    CONSTRAINT ck_pallets_emptied_at_allowed
        CHECK (emptied_at IS NULL OR status IN ('EMPTIED', 'DISPOSED'))
);

CREATE INDEX IF NOT EXISTS idx_pallets_lot ON haetdeul.pallets (lot_id);

COMMENT ON TABLE haetdeul.pallets IS
    '물리 취급단위. 1 Lot : N Pallet 허용, 1 Pallet : 1 Lot (02 §5). Pallet 별 현재 수량은 저장하지 않고 Move Line 에서 계산한다.';


CREATE TABLE IF NOT EXISTS haetdeul.pallet_events (
    pallet_event_id  BIGSERIAL NOT NULL,
    pallet_id        TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    from_location_id TEXT,
    to_location_id   TEXT,
    occurred_at      TIMESTAMPTZ NOT NULL,
    recorded_by      TEXT NOT NULL,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pallet_events_pkey PRIMARY KEY (pallet_event_id),
    CONSTRAINT pallet_events_pallet_id_fkey
        FOREIGN KEY (pallet_id) REFERENCES haetdeul.pallets(pallet_id),
    CONSTRAINT pallet_events_from_location_id_fkey
        FOREIGN KEY (from_location_id) REFERENCES haetdeul.storage_locations(location_id),
    CONSTRAINT pallet_events_to_location_id_fkey
        FOREIGN KEY (to_location_id) REFERENCES haetdeul.storage_locations(location_id),
    CONSTRAINT ck_pallet_events_type
        CHECK (event_type IN ('CREATED', 'PUTAWAY', 'RELOCATED', 'HOLD_MOVED', 'EMPTIED'))
);

CREATE INDEX IF NOT EXISTS idx_pallet_events_pallet
    ON haetdeul.pallet_events (pallet_id, occurred_at DESC);

COMMENT ON TABLE haetdeul.pallet_events IS
    '수량변동 없는 Pallet 위치이동 이력 (02 §7). 수량이 바뀌는 것은 inventory_moves 쪽이다.';


CREATE TABLE IF NOT EXISTS haetdeul.zone_override_approvals (
    override_id      TEXT NOT NULL,
    pallet_id        TEXT,
    lot_id           TEXT,
    expected_zone_id TEXT,
    target_zone_id   TEXT NOT NULL,
    override_reason  TEXT NOT NULL,
    approved_by      TEXT NOT NULL,
    approved_at      TIMESTAMPTZ NOT NULL,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT zone_override_approvals_pkey PRIMARY KEY (override_id),
    CONSTRAINT zone_override_approvals_pallet_id_fkey
        FOREIGN KEY (pallet_id) REFERENCES haetdeul.pallets(pallet_id),
    CONSTRAINT zone_override_approvals_lot_id_fkey
        FOREIGN KEY (lot_id) REFERENCES haetdeul.inventory_lots(lot_id),
    CONSTRAINT zone_override_approvals_expected_zone_id_fkey
        FOREIGN KEY (expected_zone_id) REFERENCES haetdeul.warehouse_zones(zone_id),
    CONSTRAINT zone_override_approvals_target_zone_id_fkey
        FOREIGN KEY (target_zone_id) REFERENCES haetdeul.warehouse_zones(zone_id),
    -- 무엇에 대한 예외인지 없는 승인은 두지 않는다.
    CONSTRAINT ck_zone_override_target
        CHECK (pallet_id IS NOT NULL OR lot_id IS NOT NULL)
);

COMMENT ON TABLE haetdeul.zone_override_approvals IS
    '기본 Zone 을 벗어난 배치의 승인 기록 (03 §7). 사유·승인자·승인시각 없이는 예외 배치를 두지 않는다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §6  출고 준비 — Reservation · Allocation
--     🔴 `sales` 를 FK 로 가리키기만 한다. 판매 표는 안 바뀐다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.inventory_reservations (
    reservation_id  TEXT NOT NULL,
    sim_run_id      TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    sale_id         TEXT,
    required_qty_kg NUMERIC(18,6) NOT NULL,
    reserved_qty_kg NUMERIC(18,6) NOT NULL DEFAULT 0,
    due_date        DATE,
    status          TEXT NOT NULL,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inventory_reservations_pkey PRIMARY KEY (reservation_id),
    CONSTRAINT inventory_reservations_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    CONSTRAINT inventory_reservations_item_id_fkey
        FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id),
    CONSTRAINT inventory_reservations_sale_id_fkey
        FOREIGN KEY (sale_id) REFERENCES haetdeul.sales(sale_id),
    CONSTRAINT ck_inventory_reservations_status
        CHECK (status IN ('RESERVED', 'PARTIALLY_ALLOCATED', 'ALLOCATED', 'RELEASED', 'CANCELLED')),
    -- 확보량이 요구량을 넘을 수 없다 — 넘으면 남의 재고를 잡은 것이다.
    CONSTRAINT ck_inventory_reservations_qty
        CHECK (required_qty_kg > 0 AND reserved_qty_kg >= 0 AND reserved_qty_kg <= required_qty_kg)
);

COMMENT ON TABLE haetdeul.inventory_reservations IS
    '주문 CONFIRMED 시 품목 총량 확보 (02 §11). Lot/Pallet 지정은 Allocation 쪽이다.';


CREATE TABLE IF NOT EXISTS haetdeul.inventory_allocations (
    allocation_id    TEXT NOT NULL,
    reservation_id   TEXT NOT NULL,
    lot_id           TEXT NOT NULL,
    pallet_id        TEXT,
    allocated_qty_kg NUMERIC(18,6) NOT NULL,
    allocation_basis TEXT NOT NULL,
    decided_by       TEXT NOT NULL,
    decided_at       TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inventory_allocations_pkey PRIMARY KEY (allocation_id),
    CONSTRAINT inventory_allocations_reservation_id_fkey
        FOREIGN KEY (reservation_id) REFERENCES haetdeul.inventory_reservations(reservation_id),
    CONSTRAINT inventory_allocations_lot_id_fkey
        FOREIGN KEY (lot_id) REFERENCES haetdeul.inventory_lots(lot_id),
    CONSTRAINT inventory_allocations_pallet_id_fkey
        FOREIGN KEY (pallet_id) REFERENCES haetdeul.pallets(pallet_id),
    CONSTRAINT ck_inventory_allocations_basis
        CHECK (allocation_basis IN ('FEFO_TOOL_CONFIRMED', 'HUMAN_OVERRIDE')),
    CONSTRAINT ck_inventory_allocations_status
        CHECK (status IN ('ALLOCATED', 'PICKED', 'SHIPPED', 'CANCELLED')),
    CONSTRAINT ck_inventory_allocations_qty CHECK (allocated_qty_kg > 0)
);

CREATE INDEX IF NOT EXISTS idx_inventory_allocations_reservation
    ON haetdeul.inventory_allocations (reservation_id);
CREATE INDEX IF NOT EXISTS idx_inventory_allocations_lot
    ON haetdeul.inventory_allocations (lot_id);

COMMENT ON TABLE haetdeul.inventory_allocations IS
    '출고 준비 시 실제 Lot/Pallet 지정 (02 §11). 한 주문이 여러 Lot/Pallet 에서 충당될 수 있다.';
COMMENT ON COLUMN haetdeul.inventory_allocations.allocation_basis IS
    'FEFO_TOOL_CONFIRMED = Tool 후보를 사람이 그대로 확정 / HUMAN_OVERRIDE = 사람이 다르게 정함. 자동 Allocation 은 후속이다 (02 §12).';


-- ═══════════════════════════════════════════════════════════════════════════
-- §7  원장 상세 — Move Line
--     ★ `inventory_moves` 헤더는 `10_domain_schema.sql` 소유다. 여기는 상세다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.inventory_move_lines (
    move_line_id BIGSERIAL NOT NULL,
    move_id      TEXT NOT NULL,
    lot_id       TEXT NOT NULL,
    pallet_id    TEXT,
    location_id  TEXT,
    quantity_kg  NUMERIC(18,6) NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inventory_move_lines_pkey PRIMARY KEY (move_line_id),
    -- 🔴 복합 FK 둘이 "Line 의 Lot" 을 헤더·Pallet 과 **강제로 일치**시킨다.
    --    단일 FK 셋으로 나누면 Line 이 다른 Lot 을 가리켜도 DB 가 안 막는다.
    CONSTRAINT fk_move_lines_move_lot
        FOREIGN KEY (move_id, lot_id) REFERENCES haetdeul.inventory_moves(move_id, lot_id),
    CONSTRAINT fk_move_lines_pallet_lot
        FOREIGN KEY (pallet_id, lot_id) REFERENCES haetdeul.pallets(pallet_id, lot_id),
    CONSTRAINT inventory_move_lines_lot_id_fkey
        FOREIGN KEY (lot_id) REFERENCES haetdeul.inventory_lots(lot_id),
    CONSTRAINT inventory_move_lines_location_id_fkey
        FOREIGN KEY (location_id) REFERENCES haetdeul.storage_locations(location_id),
    CONSTRAINT ck_inventory_move_lines_qty CHECK (quantity_kg > 0)
);

CREATE INDEX IF NOT EXISTS idx_inventory_move_lines_move
    ON haetdeul.inventory_move_lines (move_id);
CREATE INDEX IF NOT EXISTS idx_inventory_move_lines_pallet
    ON haetdeul.inventory_move_lines (pallet_id);

COMMENT ON TABLE haetdeul.inventory_move_lines IS
    '수량 원장의 Pallet 단위 내역 (02 §3·§10). Lot 수량과 Pallet 수량을 같은 원장에서 계산한다 (02 §14).';
COMMENT ON COLUMN haetdeul.inventory_move_lines.lot_id IS
    'Move 헤더·Pallet 과 **같은 Lot 이어야 한다** — 복합 FK 두 개가 강제한다.';
COMMENT ON COLUMN haetdeul.inventory_move_lines.pallet_id IS
    'Pallet 확정 전 입고 등에서는 NULL 이다. 없는 Pallet 을 지어내지 않는다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §8  재고실사
--     ⚠️ 표만 회수한다. 실사 Workflow 구현은 이번 단계 밖이다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.inventory_count_sessions (
    count_session_id TEXT NOT NULL,
    sim_run_id       TEXT NOT NULL,
    count_type       TEXT NOT NULL,
    scope_zone_id    TEXT,
    blind_count      BOOLEAN NOT NULL DEFAULT TRUE,
    as_of            DATE NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    counted_by       TEXT NOT NULL,
    status           TEXT NOT NULL,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inventory_count_sessions_pkey PRIMARY KEY (count_session_id),
    CONSTRAINT inventory_count_sessions_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    CONSTRAINT inventory_count_sessions_scope_zone_id_fkey
        FOREIGN KEY (scope_zone_id) REFERENCES haetdeul.warehouse_zones(zone_id),
    CONSTRAINT ck_inventory_count_sessions_type
        CHECK (count_type IN ('CYCLE', 'FULL', 'AD_HOC')),
    CONSTRAINT ck_inventory_count_sessions_status
        CHECK (status IN ('OPEN', 'COUNTED', 'REVIEWED', 'CLOSED'))
);

COMMENT ON TABLE haetdeul.inventory_count_sessions IS
    '재고실사 회차 (04 §1·§7). 주 1회 순환·월 1회 전체는 법정기준이 아니라 내부 운영가정이다.';
COMMENT ON COLUMN haetdeul.inventory_count_sessions.blind_count IS
    'TRUE = 장부 수량을 먼저 보여주지 않고 실제 확인값을 먼저 입력한다 (04 §4).';


CREATE TABLE IF NOT EXISTS haetdeul.inventory_count_lines (
    count_line_id     BIGSERIAL NOT NULL,
    count_session_id  TEXT NOT NULL,
    location_id       TEXT,
    pallet_id         TEXT,
    lot_id            TEXT,
    physical_presence BOOLEAN NOT NULL,
    physical_qty_kg   NUMERIC(18,6),
    system_qty_kg     NUMERIC(18,6),
    abnormal_flag     BOOLEAN NOT NULL DEFAULT FALSE,
    counted_by        TEXT NOT NULL,
    counted_at        TIMESTAMPTZ NOT NULL,
    note              TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inventory_count_lines_pkey PRIMARY KEY (count_line_id),
    CONSTRAINT inventory_count_lines_count_session_id_fkey
        FOREIGN KEY (count_session_id) REFERENCES haetdeul.inventory_count_sessions(count_session_id),
    CONSTRAINT inventory_count_lines_location_id_fkey
        FOREIGN KEY (location_id) REFERENCES haetdeul.storage_locations(location_id),
    CONSTRAINT inventory_count_lines_pallet_id_fkey
        FOREIGN KEY (pallet_id) REFERENCES haetdeul.pallets(pallet_id),
    CONSTRAINT inventory_count_lines_lot_id_fkey
        FOREIGN KEY (lot_id) REFERENCES haetdeul.inventory_lots(lot_id),
    CONSTRAINT ck_inventory_count_lines_qty
        CHECK (physical_qty_kg IS NULL OR physical_qty_kg >= 0)
);

COMMENT ON TABLE haetdeul.inventory_count_lines IS
    '실사 입력 한 줄 (04 §3). system_qty_kg 는 Blind Count 라 입력 후에 채운다 — 미입력(NULL)과 0 을 섞지 않는다.';


CREATE TABLE IF NOT EXISTS haetdeul.inventory_count_discrepancies (
    discrepancy_id     TEXT NOT NULL,
    count_line_id      BIGINT NOT NULL,
    discrepancy_type   TEXT NOT NULL,
    system_qty_kg      NUMERIC(18,6),
    physical_qty_kg    NUMERIC(18,6),
    variance_qty_kg    NUMERIC(18,6),
    cause_candidates   JSONB,
    recommended_action TEXT NOT NULL,
    resolution_status  TEXT NOT NULL,
    resolved_move_id   TEXT,
    approved_by        TEXT,
    approved_at        TIMESTAMPTZ,
    note               TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inventory_count_discrepancies_pkey PRIMARY KEY (discrepancy_id),
    CONSTRAINT inventory_count_discrepancies_count_line_id_fkey
        FOREIGN KEY (count_line_id) REFERENCES haetdeul.inventory_count_lines(count_line_id),
    CONSTRAINT inventory_count_discrepancies_resolved_move_id_fkey
        FOREIGN KEY (resolved_move_id) REFERENCES haetdeul.inventory_moves(move_id),
    CONSTRAINT ck_count_discrepancy_type
        CHECK (discrepancy_type IN ('QTY_MISMATCH', 'PALLET_NOT_FOUND',
                                    'UNEXPECTED_PALLET', 'LOCATION_MISMATCH')),
    CONSTRAINT ck_count_discrepancy_action
        CHECK (recommended_action IN ('RECOUNT', 'ADJUST_IN', 'ADJUST_OUT',
                                      'HOLD', 'RELOCATE', 'NONE')),
    CONSTRAINT ck_count_discrepancy_status
        CHECK (resolution_status IN ('OPEN', 'RECOUNT_REQUESTED', 'APPROVED', 'REJECTED', 'CLOSED')),
    -- 승인 없는 장부수정은 없다 — 조정 Move 가 붙으려면 승인자·승인시각이 있어야 한다.
    CONSTRAINT ck_count_discrepancy_approval
        CHECK (resolved_move_id IS NULL
               OR (approved_by IS NOT NULL AND approved_at IS NOT NULL
                   AND resolution_status = 'APPROVED'))
);

CREATE INDEX IF NOT EXISTS idx_count_discrepancies_open
    ON haetdeul.inventory_count_discrepancies (resolution_status)
    WHERE resolution_status IN ('OPEN', 'RECOUNT_REQUESTED');

COMMENT ON TABLE haetdeul.inventory_count_discrepancies IS
    '장부/실사 불일치와 처리 (04 §5·§6). AI 는 원인 후보와 조치를 추천만 하고, 승인 없는 장부수정은 없다 (04 §8).';
COMMENT ON COLUMN haetdeul.inventory_count_discrepancies.cause_candidates IS
    'Agent 가 제시한 원인 후보. 추천이지 확정 사실이 아니다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §9  운송 기준정보 — 차량 · 거리구간 운임 · 비용 근거
--
--     🔴 **여기 있는 것은 회수일 뿐이다.** 고정 Route 정책
--        (route_code · direction · origin/destination_code · vehicle_class ·
--         max_load_kg · fixed_fee_krw · standard_minutes)은 **후속 단계**다.
--        `vehicle_rate_table` 의 km 구간 12 행은 지우지도 바꾸지도 않는다 —
--        `deliveries` 15 행이 이 체계로 적혀 있다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.vehicle_specs (
    vehicle_class          TEXT NOT NULL,
    body_type              TEXT NOT NULL,
    max_payload_kg         NUMERIC(12,3) NOT NULL,
    operational_payload_kg NUMERIC(12,3) NOT NULL,
    inner_width_mm         INTEGER,
    inner_length_mm        INTEGER,
    inner_height_mm        INTEGER,
    max_pallet_floor_count INTEGER,
    source_ref             TEXT NOT NULL,
    evidence_grade         TEXT NOT NULL,
    note                   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vehicle_specs_pkey PRIMARY KEY (vehicle_class),
    CONSTRAINT ck_vehicle_specs_body_type
        CHECK (body_type IN ('REEFER', 'DRY', 'OPEN')),
    CONSTRAINT ck_vehicle_specs_payload
        CHECK (max_payload_kg > 0 AND operational_payload_kg > 0
               AND operational_payload_kg <= max_payload_kg),
    CONSTRAINT ck_vehicle_specs_grade
        CHECK (evidence_grade IN ('OFFICIAL', 'VENDOR', 'ASSUMED'))
);

COMMENT ON TABLE haetdeul.vehicle_specs IS
    '대표 차량 제원 (06 §3). 차량 선택은 중량 하나가 아니라 kg + Pallet + 높이를 함께 본다 (06 §2).';
COMMENT ON COLUMN haetdeul.vehicle_specs.operational_payload_kg IS
    '명목 최대적재량이 아니라 보수적인 내부 운영 Payload (06 §8).';


CREATE TABLE IF NOT EXISTS haetdeul.vehicle_rate_table (
    rate_id          TEXT NOT NULL,
    vehicle_class    TEXT NOT NULL,
    body_type        TEXT NOT NULL,
    distance_from_km NUMERIC(10,3) NOT NULL,
    distance_to_km   NUMERIC(10,3) NOT NULL,
    base_rate_krw    NUMERIC(18,2) NOT NULL,
    rate_type        TEXT NOT NULL,
    evidence_grade   TEXT NOT NULL,
    source_ref       TEXT NOT NULL,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vehicle_rate_table_pkey PRIMARY KEY (rate_id),
    CONSTRAINT uq_vehicle_rate_band
        UNIQUE (vehicle_class, body_type, distance_from_km, distance_to_km, rate_type),
    CONSTRAINT vehicle_rate_table_vehicle_class_fkey
        FOREIGN KEY (vehicle_class) REFERENCES haetdeul.vehicle_specs(vehicle_class),
    CONSTRAINT ck_vehicle_rate_distance
        CHECK (distance_from_km >= 0 AND distance_to_km > distance_from_km),
    CONSTRAINT ck_vehicle_rate_amount CHECK (base_rate_krw > 0),
    CONSTRAINT ck_vehicle_rate_type
        CHECK (rate_type IN ('PUBLIC_REFERENCE', 'SIMULATION_BASELINE',
                             'VENDOR_QUOTE', 'SETTLEMENT_RATE')),
    CONSTRAINT ck_vehicle_rate_grade
        CHECK (evidence_grade IN ('OFFICIAL', 'VENDOR', 'ASSUMED'))
);

COMMENT ON TABLE haetdeul.vehicle_rate_table IS
    '거리구간 운임표 (06 §5). 단일 won_per_km 를 정책 정본으로 쓰지 않는다.';
COMMENT ON COLUMN haetdeul.vehicle_rate_table.distance_to_km IS
    '구간은 distance_from_km 초과 ~ distance_to_km 이하다. 예: (0,11] = 문서의 "~11km".';


CREATE TABLE IF NOT EXISTS haetdeul.logistics_cost_references (
    cost_ref_id    TEXT NOT NULL,
    cost_category  TEXT NOT NULL,
    cost_label     TEXT NOT NULL,
    amount_krw     NUMERIC(18,2),
    amount_basis   TEXT NOT NULL,
    value_status   TEXT NOT NULL,
    evidence_grade TEXT,
    source_ref     TEXT NOT NULL,
    excludes       TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT logistics_cost_references_pkey PRIMARY KEY (cost_ref_id),
    CONSTRAINT ck_logistics_cost_category
        CHECK (cost_category IN ('WAREHOUSE_BASE_COST', 'EQUIPMENT_CAPEX', 'RACK_CAPEX',
                                 'EQUIPMENT_DEPRECIATION', 'MAINTENANCE', 'ENERGY',
                                 'PALLET_COST', 'RACK_DEPRECIATION', 'RACK_INSPECTION_COST',
                                 'TRANSPORT')),
    CONSTRAINT ck_logistics_cost_basis
        CHECK (amount_basis IN ('ONE_TIME', 'MONTHLY', 'PER_DELIVERY')),
    CONSTRAINT ck_logistics_cost_grade
        CHECK (evidence_grade IS NULL
               OR evidence_grade IN ('OFFICIAL', 'VENDOR', 'ASSUMED')),
    -- 🔴 미확정을 금액으로 지어내지 못하게 한다. NOT_FIXED 면 금액도 등급도 NULL 이다.
    CONSTRAINT ck_logistics_cost_value_status
        CHECK ((value_status = 'FIXED' AND amount_krw IS NOT NULL AND evidence_grade IS NOT NULL)
               OR (value_status = 'NOT_FIXED' AND amount_krw IS NULL AND evidence_grade IS NULL))
);

COMMENT ON TABLE haetdeul.logistics_cost_references IS
    '물류 비용 근거 (01 §14·§16 · 07 §10). Finance 확정 CAPEX/OPEX 가 아니다 — 금액과 evidence_grade 를 항상 함께 전달한다.';
COMMENT ON COLUMN haetdeul.logistics_cost_references.evidence_grade IS
    'MVP 고정값으로 채택되어도 근거가 내부 파생이면 ASSUMED 를 유지한다 (2026-09-03 재무 요청 · 00 §9.1).';
COMMENT ON COLUMN haetdeul.logistics_cost_references.excludes IS
    '이 금액에 포함되지 않은 항목. Rack 재료비 1,550,000원을 총구축비로 전달하지 않기 위한 칸이다 (01 §14).';


-- ═══════════════════════════════════════════════════════════════════════════
-- §10  물류 전용 View
-- ═══════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW haetdeul.v_zone_position_occupancy AS
 SELECT z.zone_id,
    z.zone_code,
    z.zone_kind,
    z.purpose,
    COALESCE(cap.max_pallet_positions, 0::bigint) AS max_pallet_positions,
    COALESCE(occ.occupied_pallet_count, 0::bigint) AS occupied_pallet_count,
    COALESCE(cap.max_pallet_positions, 0::bigint) - COALESCE(occ.occupied_pallet_count, 0::bigint) AS free_pallet_positions_now
   FROM haetdeul.warehouse_zones z
     LEFT JOIN ( SELECT storage_locations.zone_id,
            count(*) AS max_pallet_positions
           FROM haetdeul.storage_locations
          WHERE storage_locations.is_active
          GROUP BY storage_locations.zone_id) cap ON cap.zone_id = z.zone_id
     LEFT JOIN ( SELECT l.zone_id,
            count(*) AS occupied_pallet_count
           FROM haetdeul.pallets p
             JOIN haetdeul.storage_locations l ON l.location_id = p.current_location_id
          WHERE p.status = ANY (ARRAY['ACTIVE'::text, 'HOLD'::text])
          GROUP BY l.zone_id) occ ON occ.zone_id = z.zone_id
  WHERE z.is_active;

COMMENT ON VIEW haetdeul.v_zone_position_occupancy IS
    'Zone 별 Pallet Position 정원과 현재 점유. 날짜별 free_positions 투영은 코드 몫이다 (01 §11~§12).';


CREATE OR REPLACE VIEW haetdeul.v_move_line_integrity AS
 SELECT m.move_id,
    m.sim_run_id,
    m.lot_id,
    m.move_type,
    m.moved_at,
    m.quantity_kg AS header_qty_kg,
    sum(l.quantity_kg) AS line_total_kg,
    sum(l.quantity_kg) - m.quantity_kg AS diff_kg,
    count(*) AS line_count
   FROM haetdeul.inventory_moves m
     JOIN haetdeul.inventory_move_lines l ON l.move_id = m.move_id
  GROUP BY m.move_id, m.sim_run_id, m.lot_id, m.move_type, m.moved_at, m.quantity_kg
 HAVING sum(l.quantity_kg) <> m.quantity_kg;

COMMENT ON VIEW haetdeul.v_move_line_integrity IS
    '원장 정합성 검출 — 비어 있어야 정상이다. Header 수량과 Move Line 합계가 갈린 Move 를 낸다 (02 §14 INVENTORY_INTEGRITY_ERROR). Line 이 0건인 Move 는 대상이 아니다 — Pallet 확정 전 입고(02 §9)가 그 상태다. 🔴 DB 는 이것을 막지 않는다. Service 가 검사한다.';

COMMIT;
