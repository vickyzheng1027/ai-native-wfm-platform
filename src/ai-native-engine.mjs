export const AI_STATES = [
  'UNDERSTANDING', 'COLLECTING_CONTEXT', 'CHECKING_COMPLIANCE',
  'GENERATING_OPTIONS', 'SIMULATING_IMPACT', 'AWAITING_CONFIRMATION',
  'EXECUTING', 'COMPLETED', 'LEARNING'
];

export const WFM_MODULES = [
  { id:'hr', name:'人事管理' }, { id:'schedule', name:'智能排班' },
  { id:'attendance', name:'考勤管理' }, { id:'timecalc', name:'工时计算' },
  { id:'leave', name:'假期计算' }, { id:'timebank', name:'工时银行' },
  { id:'payroll', name:'薪资模块' }, { id:'account', name:'劳动力账户' },
  { id:'analytics', name:'人效报表' }
];

const DAY_MAP = {
  周一:1, 星期一:1, 周二:2, 星期二:2, 周三:3, 星期三:3,
  周四:4, 星期四:4, 周五:5, 星期五:5, 周六:6, 星期六:6,
  周日:0, 周天:0, 星期日:0, 星期天:0
};

export function parseManagementIntent(text) {
  const raw = String(text || '').trim();
  const traffic = raw.match(/(?:客流|业务量)[^\d]{0,8}(?:增长|增加|提升)?\s*(\d{1,3}(?:\.\d+)?)\s*%/);
  const budget = raw.match(/(?:预算|不超|控制在)[^\d]{0,8}(\d{3,8})/);
  const coverage = raw.match(/覆盖率[^\d]{0,8}(\d{1,3}(?:\.\d+)?)\s*%/);
  const store = raw.match(/([\u4e00-\u9fa5A-Za-z0-9]{1,10}(?:店|门店))/);
  const leaveCount = raw.match(/(\d+)\s*名?员工请假/);
  return {
    raw,
    action:'optimize_workforce',
    store:store?.[1] || null,
    trafficIncreasePct:traffic ? Number(traffic[1]) : 35,
    budgetCeiling:budget ? Number(budget[1]) : 2000,
    minimumCoveragePct:coverage ? Number(coverage[1]) : 95,
    leaveCount:leaveCount ? Number(leaveCount[1]) : 0,
    laborAccountRequired:/账户|归属|分摊/.test(raw),
    complianceRequired:/合规|法规|工时|不超/.test(raw),
    recognized:Boolean(store || traffic || budget || coverage || /排班|人力|支援|加班/.test(raw))
  };
}

export function parseEmployeeIntent(text) {
  const raw = String(text || '').trim();
  let action = 'unknown';
  if (/打卡|上班卡|下班卡/.test(raw)) action = 'punch';
  if (/补卡|忘记打|漏打卡|没打卡/.test(raw)) action = 'attendance_correction';
  else if (/加班|延长工作|多留/.test(raw)) action = 'overtime';
  else if (/请假|休假|病假|事假|不能来/.test(raw)) action = 'leave';
  else if (/换班|调班|对调/.test(raw)) action = 'swap';
  else if (/偏好|周末.*休|不排班/.test(raw)) action = 'preference';
  else if (/排班|我的班|班表|几点/.test(raw)) action = 'query_schedule';
  const day = Object.keys(DAY_MAP).find(key => raw.includes(key));
  return {
    raw, action, dayOfWeek:day ? DAY_MAP[day] : null,
    shift:/早班|上午/.test(raw) ? 'morning' : (/晚班|下午|夜班/.test(raw) ? 'evening' : null),
    punchType:/下班/.test(raw) ? 'clock_out' : 'clock_in',
    reason:raw.match(/(生病|发烧|感冒|家里有事|个人原因)/)?.[1] || null
  };
}

export function createScenario(context, intent) {
  const required = Math.max(1, Number(context.requiredHeadcount));
  const beforeCount = Math.max(0, Number(context.currentHeadcount));
  const afterLeaveCount = Math.max(0, beforeCount - intent.leaveCount);
  return {
    event:{
      id:context.eventId, name:context.eventName, date:context.eventDate,
      budget:Math.min(Number(context.budgetRemaining), intent.budgetCeiling),
      trafficIncreasePct:intent.trafficIncreasePct,
      laborAccountId:context.laborAccountId
    },
    coverage:{
      required,
      baselineBeforeLeave:Math.round(beforeCount / required * 100),
      baselineAfterLeave:Math.round(afterLeaveCount / required * 100)
    },
    units:{
      overtime:{ coverage:Math.round(100 / required), cost:Number(context.averageHourlyRate) * 2, label:'本店加班', hours:2 },
      crossStore:{ coverage:Math.round(100 / required), cost:Number(context.averageSupportRate) * 4, label:'跨店支援', hours:4 },
      extendShift:{ coverage:Math.round(100 / required / 2), cost:Number(context.averageHourlyRate) * 2, label:'延长班次', hours:2 }
    },
    rules:{ maxOvertimeHeadcount:3, minBudgetMargin:200, minimumCoveragePct:intent.minimumCoveragePct },
    actuals:null
  };
}

function simulateImpact(scenario, definition) {
  let coverageGain = 0;
  let addedCost = 0;
  let affectedEmployees = 0;
  let addedHours = 0;
  const breakdown = definition.actions.map(action => {
    const unit = scenario.units[action.type];
    coverageGain += unit.coverage * action.count;
    addedCost += unit.cost * action.count;
    affectedEmployees += action.count;
    addedHours += unit.hours * action.count;
    return { type:action.type, label:unit.label, count:action.count, hours:unit.hours * action.count, cost:unit.cost * action.count };
  });
  return {
    coverageBefore:scenario.coverage.baselineAfterLeave,
    coverageAfter:Math.min(100, scenario.coverage.baselineAfterLeave + coverageGain),
    addedCost:Number(addedCost.toFixed(2)),
    budget:scenario.event.budget,
    budgetRemaining:Number((scenario.event.budget - addedCost).toFixed(2)),
    affectedEmployees, addedHours, breakdown
  };
}

function checkPlan(scenario, definition, impact) {
  const violations = [];
  const highRisks = [];
  const overtime = definition.actions.find(action => action.type === 'overtime');
  if (overtime?.count > scenario.rules.maxOvertimeHeadcount) violations.push({ rule:'加班上限', detail:`本店加班 ${overtime.count} 人，超过阈值 ${scenario.rules.maxOvertimeHeadcount} 人` });
  if (impact.budgetRemaining < 0) violations.push({ rule:'预算护栏', detail:`新增成本 ¥${impact.addedCost} 超出可用预算 ¥${scenario.event.budget}` });
  else if (impact.budgetRemaining < scenario.rules.minBudgetMargin) highRisks.push({ rule:'预算余量', detail:`预算余量 ¥${impact.budgetRemaining}，需人工复核` });
  if (definition.actions.some(action => action.type === 'crossStore')) highRisks.push({ rule:'员工同意', detail:'跨店支援须取得员工本人确认' });
  if (impact.coverageAfter < scenario.rules.minimumCoveragePct) highRisks.push({ rule:'覆盖达标', detail:`预计覆盖率 ${impact.coverageAfter}%，未达目标 ${scenario.rules.minimumCoveragePct}%` });
  return { passed:violations.length === 0, needManualReview:highRisks.length > 0, violations, highRisks };
}

export function generateCandidatePlans(scenario) {
  const gap = Math.max(0, scenario.coverage.required - Math.round(scenario.coverage.baselineAfterLeave * scenario.coverage.required / 100));
  const definitions = [
    { id:'PLAN-OT', name:'仅本店加班', actions:[{ type:'overtime', count:Math.max(1, gap) }] },
    { id:'PLAN-CS', name:'仅跨店支援', actions:[{ type:'crossStore', count:Math.max(1, gap) }] },
    { id:'PLAN-MIX', name:'跨店支援 + 延长班次（组合）', actions:[{ type:'crossStore', count:Math.max(1, Math.ceil(gap / 2)) }, { type:'extendShift', count:Math.max(1, Math.floor(gap / 2)) }] }
  ];
  const plans = definitions.map(definition => {
    const impact = simulateImpact(scenario, definition);
    return { ...definition, impact, compliance:checkPlan(scenario, definition, impact), recommended:false };
  });
  const eligible = plans.filter(plan => plan.compliance.passed && plan.impact.coverageAfter >= scenario.rules.minimumCoveragePct);
  const recommended = [...(eligible.length ? eligible : plans.filter(plan => plan.compliance.passed))].sort((a,b) => a.impact.addedCost - b.impact.addedCost)[0];
  if (recommended) recommended.recommended = true;
  return plans;
}

export function buildStateTrace() {
  return AI_STATES.slice(0, 6).map((state, index) => ({ state, sequence:index + 1 }));
}

export function computeTimeBank(plan) {
  const overtimeHours = plan.actions.filter(action => ['overtime','extendShift'].includes(action.type)).reduce((sum, action) => sum + action.count * 2, 0);
  const carryOverHours = Math.round(overtimeHours * 0.5 * 100) / 100;
  return { overtimeHours, carryOverHours, payrollHours:overtimeHours - carryOverHours };
}

export function computeEfficiency({ actualSales, baselineSales, totalHours, laborCost, coveragePct }) {
  return {
    coveragePct,
    salesPerHour:totalHours ? Math.round(actualSales / totalHours) : 0,
    laborCostRate:actualSales ? Math.round(laborCost / actualSales * 1000) / 10 : 0,
    roi:laborCost ? Math.round((actualSales - baselineSales) / laborCost * 10) / 10 : 0
  };
}

export function buildThreeLayerFeedback({ plannedTraffic, actualTraffic, employeeName = '陈晨' }) {
  const before = 1.28;
  const variance = plannedTraffic ? (actualTraffic - plannedTraffic) / plannedTraffic : 0;
  return [
    { type:'business', key:'traffic_factor', before, after:Math.round(before * (1 + variance) * 100) / 100, evidence:`预测客流 ${plannedTraffic}，实际 ${actualTraffic}` },
    { type:'employee', key:'support_reliability', before:82, after:88, evidence:`${employeeName}完成跨店支援任务` },
    { type:'strategy', key:'voluntary_support_weight', before:0.60, after:0.72, evidence:'组合方案经管理者确认并顺利执行' }
  ];
}
