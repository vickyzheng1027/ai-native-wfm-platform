import test from 'node:test';
import assert from 'node:assert/strict';
import {
  WFM_MODULES, parseManagementIntent, parseEmployeeIntent, createScenario,
  generateCandidatePlans, buildStateTrace, computeTimeBank,
  computeEfficiency, buildThreeLayerFeedback
} from './ai-native-engine.mjs';

test('管理端自然语言可解析为目标、约束和账户要求', () => {
  const intent = parseManagementIntent('旗舰店会员日客流增长35%，预算不超2000元，覆盖率达到95%，新增工时计入活动账户并全程合规');
  assert.equal(intent.store, '旗舰店');
  assert.equal(intent.trafficIncreasePct, 35);
  assert.equal(intent.budgetCeiling, 2000);
  assert.equal(intent.minimumCoveragePct, 95);
  assert.equal(intent.laborAccountRequired, true);
  assert.equal(intent.complianceRequired, true);
});

test('员工自然语言覆盖查班、请假、换班、补卡和加班意图', () => {
  assert.equal(parseEmployeeIntent('我周六生病了，要请假').action, 'leave');
  assert.equal(parseEmployeeIntent('周三晚班想和同事换班').action, 'swap');
  assert.equal(parseEmployeeIntent('昨天忘记打下班卡').action, 'attendance_correction');
  assert.equal(parseEmployeeIntent('会员日多留两小时加班').action, 'overtime');
  assert.equal(parseEmployeeIntent('看看我的班表').action, 'query_schedule');
});

test('多目标引擎生成三方案并推荐覆盖达标且成本较低的合规方案', () => {
  const intent = parseManagementIntent('旗舰店客流增长35%，预算2000元，覆盖率95%，全程合规');
  const scenario = createScenario({
    eventId:'event-1', eventName:'会员日', eventDate:'2026-08-08', requiredHeadcount:8,
    currentHeadcount:4, budgetRemaining:2000, laborAccountId:'account-1',
    averageHourlyRate:130, averageSupportRate:108
  }, intent);
  const plans = generateCandidatePlans(scenario);
  assert.equal(plans.length, 3);
  assert.equal(plans.filter(plan => plan.recommended).length, 1);
  assert.equal(plans.find(plan => plan.id === 'PLAN-OT').compliance.passed, false);
  assert.ok(plans.find(plan => plan.recommended).impact.coverageAfter >= 95);
  assert.equal(buildStateTrace().at(-1).state, 'AWAITING_CONFIRMATION');
  assert.equal(WFM_MODULES.length, 9);
});

test('工时银行、人效与三层反哺由输入数据计算', () => {
  assert.deepEqual(computeTimeBank({ actions:[{ type:'overtime', count:2 }, { type:'extendShift', count:1 }] }), {
    overtimeHours:6, carryOverHours:3, payrollHours:3
  });
  const efficiency = computeEfficiency({ actualSales:180000, baselineSales:160000, totalHours:100, laborCost:5000, coveragePct:98 });
  assert.equal(efficiency.salesPerHour, 1800);
  assert.equal(efficiency.roi, 4);
  const feedback = buildThreeLayerFeedback({ plannedTraffic:1000, actualTraffic:1100 });
  assert.deepEqual(feedback.map(item => item.type), ['business','employee','strategy']);
  assert.equal(feedback[0].after, 1.41);
});
