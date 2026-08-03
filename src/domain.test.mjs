import test from 'node:test';
import assert from 'node:assert/strict';
import { createDatabase } from './db.mjs';
import {
  login, authenticate, listEmployees, createEmployee, updateEmployee,
  createRequest, listRequests, decideRequest, generatePlan, executePlan,
  listShifts, punch, closeEvent, accountReport, payrollReport, auditReport,
  understandEmployeeCommand, DomainError
} from './domain.mjs';

function setup() {
  const db = createDatabase(':memory:');
  const managerLogin = login(db, 'DEMO', 'manager', 'Demo@2026');
  const employeeLogin = login(db, 'DEMO', 'employee', 'Demo@2026');
  return { db, manager: managerLogin.user, employee: employeeLogin.user, managerLogin, employeeLogin };
}

test('登录、会话认证和角色数据隔离真实生效', () => {
  const { db, managerLogin, employeeLogin, employee } = setup();
  assert.equal(authenticate(db, managerLogin.token).role, 'manager');
  assert.equal(authenticate(db, employeeLogin.token).employeeId, 'emp-linxiao');
  assert.throws(() => login(db, 'DEMO', 'manager', 'wrong'), /用户名或密码错误/);
  assert.throws(() => listEmployees(db, employee), error => error instanceof DomainError && error.status === 403);
  assert.equal(listShifts(db, employee).every(shift => shift.employeeNo === 'E001'), true);
});

test('员工新增和可支援状态更新持久化到数据库', () => {
  const { db, manager } = setup();
  const result = createEmployee(db, manager, {
    employeeNo:'E009', name:'测试员工', storeId:'store-north', department:'零售运营',
    position:'导购', employmentType:'full_time', hourlyRate:99, skills:['导购'],
    annualLeaveHours:24, availableForSupport:true
  });
  updateEmployee(db, manager, result.id, { availableForSupport:false, hourlyRate:108 });
  const row = db.prepare('SELECT hourly_rate,available_for_support FROM employees WHERE id=?').get(result.id);
  assert.equal(row.hourly_rate, 108);
  assert.equal(row.available_for_support, 0);
  assert.equal(db.prepare('SELECT balance_hours FROM leave_balances WHERE employee_id=?').get(result.id).balance_hours, 24);
});

test('请假申请审批扣减真实余额并保留合规和审计记录', () => {
  const { db, manager, employee } = setup();
  const request = createRequest(db, employee, {
    requestType:'leave', requestDate:'2026-08-09', hours:8, reason:'家庭事务'
  });
  assert.equal(listRequests(db, employee)[0].status, 'pending');
  decideRequest(db, manager, request.id, 'approved', '余额及覆盖校验通过');
  assert.equal(db.prepare("SELECT balance_hours FROM leave_balances WHERE employee_id='emp-linxiao'").get().balance_hours, 32);
  assert.equal(db.prepare('SELECT status FROM employee_requests WHERE id=?').get(request.id).status, 'approved');
  assert.ok(db.prepare("SELECT COUNT(*) AS count FROM compliance_checks WHERE subject_id=?").get(request.id).count > 0);
});

test('AI 提案经合规校验和人工确认后创建真实班次', () => {
  const { db, manager } = setup();
  const plan = generatePlan(db, manager, '会员日客流增长35%，预算不超2000元，覆盖率达到95%，全程合规', 'event-member-day');
  assert.equal(plan.option.coverage, 100);
  assert.ok(plan.option.cost <= 2000);
  assert.equal(plan.option.checks.every(check => check.passed), true);
  assert.equal(plan.alternatives.length, 3);
  assert.equal(plan.modules.length, 9);
  assert.equal(plan.stateTrace.at(-1).state, 'AWAITING_CONFIRMATION');
  const stored = db.prepare('SELECT alternatives_json,state_trace_json,modules_json FROM workforce_plans WHERE id=?').get(plan.id);
  assert.equal(JSON.parse(stored.alternatives_json).length, 3);
  const result = executePlan(db, manager, plan.id);
  assert.equal(result.addedShifts, 4);
  assert.equal(listShifts(db, manager).length, 8);
  assert.equal(db.prepare('SELECT status FROM workforce_plans WHERE id=?').get(plan.id).status, 'executed');
});

test('员工自然语言在后端结合本人排班生成下一步动作', () => {
  const { db, employee } = setup();
  const query = understandEmployeeCommand(db, employee, '帮我看看本周排班');
  assert.equal(query.intent.action, 'query_schedule');
  assert.equal(query.nextAction, 'return_schedule');
  assert.ok(query.context.length > 0);
  const correction = understandEmployeeCommand(db, employee, '昨天忘记打下班卡，帮我补卡');
  assert.equal(correction.nextAction, 'create_attendance_correction_request');
  assert.equal(correction.requiresConfirmation, true);
});

test('打卡、工时结算、工时银行、账户分摊、薪资和数据反哺端到端可计算', () => {
  const { db, manager, employee } = setup();
  const plan = generatePlan(db, manager, '会员日客流增长35%，预算不超2000元，覆盖率达到95%，全程合规', 'event-member-day');
  executePlan(db, manager, plan.id);
  punch(db, employee, { eventType:'clock_in', occurredAt:'2026-08-08T09:00:00+08:00' });
  punch(db, employee, { eventType:'clock_out', occurredAt:'2026-08-08T18:00:00+08:00' });
  const result = closeEvent(db, manager, 'event-member-day', { actualTraffic:1420, actualSales:180000 });
  assert.equal(result.processed, 8);
  assert.equal(result.totalHours, 52);
  assert.ok(result.totalCost > 0);
  assert.equal(db.prepare('SELECT overtime_hours FROM time_results WHERE employee_id=?').get(employee.employeeId).overtime_hours, 1);
  assert.equal(db.prepare('SELECT hours FROM time_bank_entries WHERE employee_id=?').get(employee.employeeId).hours, 1);
  const account = accountReport(db, manager).find(item => item.id === 'account-member-day');
  assert.equal(account.allocatedHours, 52);
  assert.ok(account.allocatedCost > 0);
  assert.equal(payrollReport(db, manager).length, 8);
  assert.equal(db.prepare("SELECT COUNT(*) AS count FROM feedback_metrics WHERE event_id='event-member-day'").get().count, 3);
  assert.ok(auditReport(db, manager).some(row => row.action === 'event.closed'));
  assert.equal(closeEvent(db, manager, 'event-member-day').processed, 0);
});

test('活动结算拒绝缺失或无效的实际经营数据',()=>{
  const {db,manager}=setup();
  assert.throws(()=>closeEvent(db,manager,'event-member-day',{}),error=>error.code==='INVALID_EVENT_OUTCOME');
  assert.throws(()=>closeEvent(db,manager,'event-member-day',{actualTraffic:-1,actualSales:100}),error=>error.code==='INVALID_EVENT_OUTCOME');
});
