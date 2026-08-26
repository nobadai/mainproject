/**
 * 기능 테스트용 요청 프리셋.
 *
 * 전부 실제로 200 이 나오는 것을 확인한 payload 다. 화면에서 직접 편집할 수 있으므로
 * 여기 값은 "시작점"이지 고정이 아니다.
 */

export type EndpointKey =
  | "orchestrator/procurement"
  | "orchestrator/sales"
  | "orchestrator/day"
  | "critic/procurement"
  | "critic/sales";

export const AS_OF = "2026-08-25";

const scenarios = [
  { scenario_id: "SCN-1", stance: "보수", qty_kg: { 배추: 3000 }, unit_price_krw_per_kg: { 배추: 1200 } },
  { scenario_id: "SCN-2", stance: "기본", qty_kg: { 배추: 6000 }, unit_price_krw_per_kg: { 배추: 1200 } },
  { scenario_id: "SCN-3", stance: "공격", qty_kg: { 배추: 9000 }, unit_price_krw_per_kg: { 배추: 1200 } },
];

const evidence = (claim: string, ref: string, value: number, unit = "kg") => ({
  claim,
  ref_ids: [ref],
  value,
  unit,
  evidence_grade: "OFFICIAL",
});

/** 오케 입력용 부서 회신 — evidence 없이 밴드 기여만. */
const orchReplies = [
  {
    dept: "sales",
    item: "배추",
    reasoning: "김치공장 계약 물량을 맞춰야 합니다.",
    checks: [{ check_id: "sales.floor", floor_kg: { 배추: 2000 }, reason: "계약 최소" }],
  },
  {
    dept: "inventory",
    reasoning: "창고 여유가 부족합니다.",
    checks: [{ check_id: "inv.cap", cap_kg: { 배추: 8000 }, cap_total_kg: 8000 }],
  },
  {
    dept: "finance",
    reasoning: "지급이 한 주에 몰립니다.",
    checks: [
      { check_id: "fin.cap", cap_amount_krw: 8400000 },
      { check_id: "fin.warn", kind: "soft", verdict: "conditional", reason: "지급 집중" },
    ],
  },
];

/** Critic 입력용 — 하드 검사마다 evidence 가 필요하다 (§1.2-5). */
const criticReplies = [
  {
    dept: "sales",
    item: "배추",
    reasoning: "김치공장 계약 물량을 맞춰야 합니다.",
    checks: [
      {
        check_id: "sales.floor",
        floor_kg: { 배추: 2000 },
        reason: "계약 최소",
        evidences: [evidence("계약최소", "SO-1", 2000)],
      },
    ],
  },
  {
    dept: "inventory",
    reasoning: "창고 여유가 부족합니다.",
    checks: [
      {
        check_id: "inv.cap",
        cap_kg: { 배추: 8000 },
        cap_total_kg: 8000,
        evidences: [evidence("가용", "WH-1", 8000)],
      },
    ],
  },
  {
    dept: "finance",
    reasoning: "지급이 한 주에 몰립니다.",
    checks: [
      {
        check_id: "fin.cap",
        cap_amount_krw: 8400000,
        evidences: [evidence("한도", "FIN-1", 8400000, "KRW")],
      },
    ],
  },
];

const salesReplies = [
  {
    dept: "inventory",
    reasoning: "공용 출고 능력에 한계가 있습니다.",
    checks: [{ check_id: "inv.out", cap_kg: { 배추: 5000 }, cap_total_kg: 5000 }],
  },
  {
    dept: "finance",
    reasoning: "단가가 낮은 채널 비중이 큽니다.",
    checks: [
      { check_id: "fin.soft", kind: "soft", verdict: "conditional", reason: "기여 저하 우려" },
    ],
  },
];

const criticSalesReplies = [
  {
    dept: "inventory",
    reasoning: "공용 출고 능력에 한계가 있습니다.",
    checks: [
      {
        check_id: "inv.out",
        cap_kg: { 배추: 5000 },
        cap_total_kg: 5000,
        evidences: [evidence("출고능력", "WH-2", 5000)],
      },
    ],
  },
];

const allocations = [
  {
    allocation_id: "ALLOC-1",
    strategy_type: "균형",
    expected_contribution_krw: 3000000,
    legs: [
      {
        channel: "KIMCHI_FACTORY",
        item: "배추",
        qty_kg: 3000,
        unit_price_krw_per_kg: 1500,
        lot_ids: ["LOT-A"],
        due_date: "2026-08-28",
      },
    ],
    outbound_by_date: [{ date: "2026-08-26", qty_kg: 3000 }],
  },
  {
    allocation_id: "ALLOC-2",
    strategy_type: "공격",
    expected_contribution_krw: 5200000,
    legs: [
      {
        channel: "KIMCHI_FACTORY",
        item: "배추",
        qty_kg: 4000,
        unit_price_krw_per_kg: 1500,
        lot_ids: ["LOT-A"],
        due_date: "2026-08-28",
      },
      {
        channel: "SPOT",
        item: "배추",
        qty_kg: 2500,
        unit_price_krw_per_kg: 1300,
        lot_ids: ["LOT-B"],
        due_date: "2026-08-27",
      },
    ],
    outbound_by_date: [{ date: "2026-08-26", qty_kg: 6500 }],
  },
];

export type Preset = { label: string; note: string; body: unknown };

export const PRESETS: Record<EndpointKey, Preset[]> = {
  "orchestrator/procurement": [
    {
      label: "정상 — 클리핑 발생",
      note: "SCN-3(9,000kg)이 재고 상한·금액 한도에 걸려 7,000kg 으로 잘린다.",
      body: { as_of: AS_OF, replies: orchReplies, scenarios },
    },
    {
      label: "교착 — 영업 하한 > 재고 상한",
      note: "부서 제약이 서로 맞물려 실행 가능한 구간이 없다. deadlock 이 뜬다.",
      body: {
        as_of: AS_OF,
        scenarios,
        replies: [
          {
            dept: "sales",
            item: "배추",
            reasoning: "계약 물량이 매우 큽니다.",
            checks: [{ check_id: "sales.floor", floor_kg: { 배추: 9000 }, reason: "계약 최소" }],
          },
          {
            dept: "inventory",
            reasoning: "창고가 거의 찼습니다.",
            checks: [{ check_id: "inv.cap", cap_kg: { 배추: 2000 }, cap_total_kg: 2000 }],
          },
        ],
      },
    },
    {
      label: "부서 미가동 — RUNTIME_NOT_READY",
      note: "재고가 못 돌면 밴드가 형성되지 않는다. LLM 도 호출하지 않는다.",
      body: {
        as_of: AS_OF,
        scenarios,
        replies: [
          {
            dept: "inventory",
            runtime_status: "RUNTIME_NOT_READY",
            reasoning: "재고 시스템 점검 중입니다.",
            checks: [],
          },
        ],
      },
    },
    {
      label: "단일 후보 — SKIPPED_TEMPLATE",
      note: "후보가 하나면 정렬할 것이 없어 LLM 을 부르지 않는다.",
      body: { as_of: AS_OF, replies: orchReplies, scenarios: [scenarios[0]] },
    },
  ],
  "orchestrator/sales": [
    {
      label: "정상 — 공용 출고 클리핑",
      note: "ALLOC-2(6,500kg)가 출고 능력 5,000kg 에 걸린다.",
      body: { as_of: AS_OF, replies: salesReplies, allocations },
    },
  ],
  "orchestrator/day": [
    {
      label: "하루 전체 — 매입 → 판매",
      note: "두 코어를 순차로 돌리고 end_code 를 낸다.",
      body: {
        procurement: { as_of: AS_OF, replies: orchReplies, scenarios },
        sales: { as_of: AS_OF, replies: salesReplies, allocations },
      },
    },
  ],
  "critic/procurement": [
    {
      label: "정상 — L5 판정 포함",
      note: "rationale 은 오케 실행 후 자동으로 채워진다(아래 연계 버튼).",
      body: {
        as_of: AS_OF,
        replies: criticReplies,
        scenarios,
        target_scenario_id: "SCN-3",
        rationale: "재고 상한과 금액 한도에 함께 걸려 물량을 줄였습니다.",
      },
    },
    {
      label: "모순 설명문 — L5 가 잡아야 한다",
      note: "제약과 정반대 문장. judge 가 FAIL → E-LOGIC CONCERN 을 내야 정상이다.",
      body: {
        as_of: AS_OF,
        replies: criticReplies,
        scenarios,
        target_scenario_id: "SCN-3",
        rationale: "재고 여유가 충분해 상한 없이 물량을 늘렸습니다. 창고 공간에 여유가 많습니다.",
      },
    },
    {
      label: "rationale 미제출 — L5 skipped",
      note: "검사할 문장이 없으면 coverage L5 가 0/6 으로 드러난다 (감추지 않는다).",
      body: { as_of: AS_OF, replies: criticReplies, scenarios, target_scenario_id: "SCN-3" },
    },
  ],
  "critic/sales": [
    {
      label: "정상 — L4-B 재검산",
      note: "로트 제약을 주면 on_hand·신선도 검사가 돈다. 약정 미제출분은 skipped.",
      body: {
        as_of: AS_OF,
        replies: criticSalesReplies,
        allocations,
        target_allocation_id: "ALLOC-2",
        rationale: "공용 출고 능력에 걸려 배분을 줄였습니다.",
        lot_constraints: [
          { lot_id: "LOT-A", item: "배추", available_qty_kg: 4000, remaining_freshness_days: 7 },
          { lot_id: "LOT-B", item: "배추", available_qty_kg: 2500, remaining_freshness_days: 5 },
        ],
      },
    },
  ],
};

export const ENDPOINTS: { key: EndpointKey; label: string; agent: "오케" | "Critic" }[] = [
  { key: "orchestrator/procurement", label: "매입 T3 (사이클 A)", agent: "오케" },
  { key: "orchestrator/sales", label: "판매 S3 (사이클 B)", agent: "오케" },
  { key: "orchestrator/day", label: "하루 전체", agent: "오케" },
  { key: "critic/procurement", label: "Critic A — 매입 검증", agent: "Critic" },
  { key: "critic/sales", label: "Critic B — 판매 검증", agent: "Critic" },
];
