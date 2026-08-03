import { randomUUID, scryptSync, timingSafeEqual } from 'node:crypto';
import { withTransaction } from './db.mjs';
import {
  WFM_MODULES, parseManagementIntent, createScenario, generateCandidatePlans,
  buildStateTrace, computeEfficiency, buildThreeLayerFeedback, parseEmployeeIntent
} from './ai-native-engine.mjs';

export class DomainError extends Error {
  constructor(message, status = 400, code = 'BUSINESS_RULE') {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const now = () => new Date().toISOString();
const hoursBetween = (start, end) => {
  if (!/^\d{2}:\d{2}$/.test(String(start)) || !/^\d{2}:\d{2}$/.test(String(end))) return 0;
  const [sh, sm] = start.split(':').map(Number);
  const [eh, em] = end.split(':').map(Number);
  if (sh > 23 || eh > 23 || sm > 59 || em > 59) return 0;
  return Math.max(0, (eh * 60 + em - sh * 60 - sm) / 60);
};

function writeAudit(db, tenantId, userId, action, entityType, entityId, detail = {}) {
  db.prepare(`INSERT INTO audit_logs(id,tenant_id,user_id,action,entity_type,entity_id,detail_json,created_at)
    VALUES(?,?,?,?,?,?,?,?)`).run(randomUUID(), tenantId, userId || null, action, entityType, entityId || null, JSON.stringify(detail), now());
}

function checkPassword(password, stored) {
  const [salt, hash] = stored.split(':');
  const actual = scryptSync(password, salt, 64);
  const expected = Buffer.from(hash, 'hex');
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

export function login(db, tenantCode, username, password) {
  const user = db.prepare(`SELECT u.*, t.code AS tenant_code, t.name AS tenant_name
    FROM users u JOIN tenants t ON t.id=u.tenant_id
    WHERE t.code=? AND u.username=? AND u.active=1`).get(tenantCode, username);
  if (!user || !checkPassword(password, user.password_hash)) throw new DomainError('租户、用户名或密码错误', 401, 'INVALID_CREDENTIALS');
  const token = randomUUID() + randomUUID();
  const expires = new Date(Date.now() + 12 * 60 * 60 * 1000).toISOString();
  db.prepare('INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)').run(token, user.id, expires, now());
  writeAudit(db, user.tenant_id, user.id, 'auth.login', 'user', user.id, { username });
  return { token, user: publicUser(user), expiresAt: expires };
}

function publicUser(user) {
  return { id: user.id, tenantId: user.tenant_id, tenantCode: user.tenant_code, tenantName: user.tenant_name, username: user.username, displayName: user.display_name, role: user.role, employeeId: user.employee_id };
}

export function authenticate(db, token) {
  if (!token) throw new DomainError('请先登录', 401, 'UNAUTHENTICATED');
  const user = db.prepare(`SELECT u.*, t.code AS tenant_code, t.name AS tenant_name
    FROM sessions s JOIN users u ON u.id=s.user_id JOIN tenants t ON t.id=u.tenant_id
    WHERE s.token=? AND s.expires_at>? AND u.active=1`).get(token, now());
  if (!user) throw new DomainError('登录已失效，请重新登录', 401, 'SESSION_EXPIRED');
  return publicUser(user);
}

export function logout(db, token) {
  db.prepare('DELETE FROM sessions WHERE token=?').run(token);
}

function requireRole(user, ...roles) {
  if (!roles.includes(user.role)) throw new DomainError('当前角色无权执行此操作', 403, 'FORBIDDEN');
}

export function dashboard(db, user) {
  const tenantId = user.tenantId;
  const event = db.prepare(`SELECT e.*,s.name AS store_name,a.name AS account_name,a.budget,a.spent
    FROM events e JOIN stores s ON s.id=e.store_id JOIN labor_accounts a ON a.id=e.labor_account_id
    WHERE e.tenant_id=? ORDER BY e.event_date DESC LIMIT 1`).get(tenantId);
  const employeeCount = db.prepare("SELECT COUNT(*) AS count FROM employees WHERE tenant_id=? AND status='active'").get(tenantId).count;
  const pendingCount = db.prepare("SELECT COUNT(*) AS count FROM employee_requests WHERE tenant_id=? AND status='pending'").get(tenantId).count;
  const planned = db.prepare("SELECT COUNT(*) AS count FROM shifts WHERE event_id=? AND status IN ('planned','confirmed','completed')").get(event.id).count;
  const required = event.required_headcount;
  const coverage = Math.min(100, Math.round(planned / required * 100));
  const allocations = db.prepare(`SELECT a.name,ROUND(SUM(la.hours),2) AS hours,ROUND(SUM(la.cost),2) AS cost
    FROM labor_allocations la JOIN labor_accounts a ON a.id=la.labor_account_id
    WHERE la.tenant_id=? GROUP BY a.id,a.name ORDER BY cost DESC`).all(tenantId);
  const feedback = db.prepare('SELECT metric_type,metric_key,before_value,after_value,evidence FROM feedback_metrics WHERE tenant_id=? ORDER BY created_at DESC LIMIT 10').all(tenantId);
  return { event, metrics: { employeeCount, pendingCount, planned, required, coverage, budgetRemaining: event.budget - event.spent }, allocations, feedback };
}

export function listEmployees(db, user) {
  requireRole(user, 'manager', 'admin');
  return db.prepare(`SELECT e.id,e.employee_no AS employeeNo,e.name,s.name AS store,e.department,e.position,e.employment_type AS employmentType,
    e.hourly_rate AS hourlyRate,e.skills_json AS skills,e.available_for_support AS availableForSupport,e.status,
    COALESCE(lb.balance_hours,0) AS annualLeaveHours
    FROM employees e JOIN stores s ON s.id=e.store_id
    LEFT JOIN leave_balances lb ON lb.employee_id=e.id AND lb.leave_type='annual'
    WHERE e.tenant_id=? ORDER BY e.employee_no`).all(user.tenantId).map(row => ({ ...row, skills: JSON.parse(row.skills), availableForSupport: Boolean(row.availableForSupport) }));
}

export function createEmployee(db, user, input) {
  requireRole(user, 'manager', 'admin');
  const required = ['employeeNo','name','storeId','department','position','employmentType'];
  required.forEach(field => { if (!String(input[field] || '').trim()) throw new DomainError(`字段 ${field} 不能为空`); });
  const store = db.prepare('SELECT id FROM stores WHERE id=? AND tenant_id=?').get(input.storeId, user.tenantId);
  if (!store) throw new DomainError('门店不存在');
  const id = randomUUID();
  return withTransaction(db, () => {
    db.prepare(`INSERT INTO employees(id,tenant_id,employee_no,name,store_id,department,position,employment_type,hourly_rate,skills_json,available_for_support,status,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(id, user.tenantId, input.employeeNo.trim(), input.name.trim(), input.storeId, input.department.trim(), input.position.trim(), input.employmentType, Number(input.hourlyRate || 100), JSON.stringify(input.skills || []), input.availableForSupport === false ? 0 : 1, 'active', now());
    db.prepare('INSERT INTO leave_balances(id,employee_id,leave_type,balance_hours) VALUES(?,?,?,?)').run(randomUUID(), id, 'annual', Number(input.annualLeaveHours || 40));
    writeAudit(db, user.tenantId, user.id, 'employee.created', 'employee', id, { employeeNo: input.employeeNo, name: input.name });
    return { id };
  });
}

export function updateEmployee(db, user, id, input) {
  requireRole(user, 'manager', 'admin');
  const employee = db.prepare('SELECT * FROM employees WHERE id=? AND tenant_id=?').get(id, user.tenantId);
  if (!employee) throw new DomainError('员工不存在', 404, 'NOT_FOUND');
  const storeId = input.storeId ?? employee.store_id;
  if (!db.prepare('SELECT 1 FROM stores WHERE id=? AND tenant_id=?').get(storeId, user.tenantId)) throw new DomainError('门店不存在');
  const availableForSupport = input.availableForSupport ?? Boolean(employee.available_for_support);
  db.prepare(`UPDATE employees SET name=?,store_id=?,department=?,position=?,employment_type=?,hourly_rate=?,skills_json=?,available_for_support=?,status=? WHERE id=?`)
    .run(input.name ?? employee.name, storeId, input.department ?? employee.department, input.position ?? employee.position, input.employmentType ?? employee.employment_type, Number(input.hourlyRate ?? employee.hourly_rate), JSON.stringify(input.skills ?? JSON.parse(employee.skills_json)), availableForSupport ? 1 : 0, input.status ?? employee.status, id);
  writeAudit(db, user.tenantId, user.id, 'employee.updated', 'employee', id, input);
  return { id };
}

export function listStores(db, user) {
  return db.prepare('SELECT id,code,name,region FROM stores WHERE tenant_id=? ORDER BY code').all(user.tenantId);
}

export function listShifts(db, user, eventId = 'event-member-day') {
  const employeeFilter = user.role === 'employee' ? ' AND sh.employee_id=?' : '';
  const params = user.role === 'employee' ? [user.tenantId, eventId, user.employeeId] : [user.tenantId, eventId];
  return db.prepare(`SELECT sh.id,sh.shift_date AS shiftDate,sh.start_at AS startAt,sh.end_at AS endAt,sh.role_required AS roleRequired,
    sh.status,sh.source,e.name AS employee,e.employee_no AS employeeNo,s.name AS store,a.name AS account
    FROM shifts sh JOIN employees e ON e.id=sh.employee_id JOIN stores s ON s.id=sh.store_id
    LEFT JOIN labor_accounts a ON a.id=sh.labor_account_id WHERE sh.tenant_id=? AND sh.event_id=?${employeeFilter} ORDER BY sh.start_at,e.name`).all(...params);
}

export function createShift(db, user, input) {
  requireRole(user, 'manager', 'admin');
  const employee = db.prepare('SELECT * FROM employees WHERE id=? AND tenant_id=?').get(input.employeeId, user.tenantId);
  if (!employee) throw new DomainError('员工不存在');
  const hours = hoursBetween(input.startAt, input.endAt);
  if (hours <= 0) throw new DomainError('班次开始和结束时间无效');
  if (hours > 10) throw new DomainError('单日计划工时不得超过 10 小时', 409, 'COMPLIANCE_FAILED');
  const storeId = input.storeId || employee.store_id;
  if (!db.prepare('SELECT 1 FROM stores WHERE id=? AND tenant_id=?').get(storeId, user.tenantId)) throw new DomainError('门店不存在');
  const overlap = db.prepare(`SELECT COUNT(*) AS count FROM shifts WHERE employee_id=? AND shift_date=? AND status!='cancelled'
    AND NOT(end_at<=? OR start_at>=?)`).get(input.employeeId, input.shiftDate, input.startAt, input.endAt).count;
  if (overlap) throw new DomainError('该员工在此时间段已有班次', 409, 'SHIFT_CONFLICT');
  const id = randomUUID();
  db.prepare(`INSERT INTO shifts(id,tenant_id,employee_id,store_id,event_id,shift_date,start_at,end_at,role_required,source,status,labor_account_id,created_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(id, user.tenantId, input.employeeId, storeId, input.eventId || null, input.shiftDate, input.startAt, input.endAt, input.roleRequired || employee.position, 'manual', 'planned', input.laborAccountId || null, now());
  writeAudit(db, user.tenantId, user.id, 'shift.created', 'shift', id, input);
  return { id };
}

function runCompliance(db, user, subjectType, subjectId, checks) {
  const insert = db.prepare(`INSERT INTO compliance_checks(id,tenant_id,subject_type,subject_id,rule_code,rule_name,passed,evidence,checked_at)
    VALUES(?,?,?,?,?,?,?,?,?)`);
  checks.forEach(check => insert.run(randomUUID(), user.tenantId, subjectType, subjectId, check.code, check.name, check.passed ? 1 : 0, check.evidence, now()));
  return checks;
}

export function createRequest(db, user, input) {
  const employeeId = user.role === 'employee' ? user.employeeId : input.employeeId;
  if (!employeeId) throw new DomainError('缺少员工信息');
  const employee = db.prepare('SELECT * FROM employees WHERE id=? AND tenant_id=?').get(employeeId, user.tenantId);
  if (!employee) throw new DomainError('员工不存在', 404, 'NOT_FOUND');
  const type = input.requestType;
  if (!['leave','attendance_correction','overtime'].includes(type)) throw new DomainError('不支持的申请类型');
  const hours = Number(input.hours);
  if (!(hours > 0 && hours <= 12)) throw new DomainError('申请工时必须在 0 到 12 小时之间');
  const id = randomUUID();
  const checks = [];
  if (type === 'leave') {
    const balance = db.prepare("SELECT balance_hours FROM leave_balances WHERE employee_id=? AND leave_type='annual'").get(employeeId)?.balance_hours || 0;
    checks.push({ code:'LEAVE_BALANCE', name:'假期余额', passed:balance >= hours, evidence:`当前余额 ${balance}h，申请 ${hours}h` });
  }
  if (type === 'overtime') {
    const monthly = db.prepare(`SELECT COALESCE(SUM(hours),0) AS hours FROM employee_requests WHERE employee_id=? AND request_type='overtime' AND status='approved' AND substr(request_date,1,7)=substr(?,1,7)`).get(employeeId, input.requestDate).hours;
    checks.push({ code:'OT_MONTH_LIMIT', name:'月度加班上限', passed:monthly + hours <= 36, evidence:`已批准 ${monthly}h，申请 ${hours}h，上限 36h` });
  }
  if (type === 'attendance_correction') checks.push({ code:'SHIFT_EXISTS', name:'计划班次关联', passed:Boolean(db.prepare('SELECT 1 FROM shifts WHERE employee_id=? AND shift_date=?').get(employeeId, input.requestDate)), evidence:'补卡必须关联计划班次' });
  if (checks.some(check => !check.passed)) {
    runCompliance(db, user, 'employee_request', id, checks);
    throw new DomainError(checks.find(check => !check.passed).evidence, 409, 'COMPLIANCE_FAILED');
  }
  return withTransaction(db, () => {
    db.prepare(`INSERT INTO employee_requests(id,tenant_id,employee_id,request_type,request_date,start_at,end_at,hours,reason,status,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,'pending',?)`).run(id, user.tenantId, employeeId, type, input.requestDate, input.startAt || null, input.endAt || null, hours, String(input.reason || '').trim() || '员工提交', now());
    runCompliance(db, user, 'employee_request', id, checks.length ? checks : [{ code:'INPUT_VALID', name:'输入完整性', passed:true, evidence:'申请字段完整' }]);
    writeAudit(db, user.tenantId, user.id, 'request.created', 'employee_request', id, { type, hours, date: input.requestDate });
    return getRequest(db, user, id);
  });
}

function getRequest(db, user, id) {
  return db.prepare(`SELECT r.*,e.name AS employee_name,e.employee_no FROM employee_requests r JOIN employees e ON e.id=r.employee_id
    WHERE r.id=? AND r.tenant_id=?`).get(id, user.tenantId);
}

export function listRequests(db, user) {
  const condition = user.role === 'employee' ? 'AND r.employee_id=?' : '';
  const params = user.role === 'employee' ? [user.tenantId, user.employeeId] : [user.tenantId];
  return db.prepare(`SELECT r.id,r.request_type AS requestType,r.request_date AS requestDate,r.start_at AS startAt,r.end_at AS endAt,
    r.hours,r.reason,r.status,r.decision_note AS decisionNote,r.created_at AS createdAt,e.name AS employee,e.employee_no AS employeeNo
    FROM employee_requests r JOIN employees e ON e.id=r.employee_id WHERE r.tenant_id=? ${condition} ORDER BY r.created_at DESC`).all(...params);
}

export function decideRequest(db, user, id, decision, note = '') {
  requireRole(user, 'manager', 'admin');
  if (!['approved','rejected'].includes(decision)) throw new DomainError('无效的审批决定');
  const request = getRequest(db, user, id);
  if (!request) throw new DomainError('申请不存在', 404, 'NOT_FOUND');
  if (request.status !== 'pending') throw new DomainError('该申请已处理', 409, 'ALREADY_DECIDED');
  return withTransaction(db, () => {
    db.prepare('UPDATE employee_requests SET status=?,approver_user_id=?,decision_note=?,decided_at=? WHERE id=?').run(decision, user.id, note, now(), id);
    if (decision === 'approved' && request.request_type === 'leave') {
      const result = db.prepare("UPDATE leave_balances SET balance_hours=balance_hours-?,version=version+1 WHERE employee_id=? AND leave_type='annual' AND balance_hours>=?").run(request.hours, request.employee_id, request.hours);
      if (!result.changes) throw new DomainError('假期余额已变化，请重新审批', 409, 'STALE_BALANCE');
      db.prepare("UPDATE shifts SET status='leave' WHERE employee_id=? AND shift_date=? AND status='planned'").run(request.employee_id, request.request_date);
    }
    if (decision === 'approved' && request.request_type === 'attendance_correction') {
      const shift = db.prepare('SELECT id,end_at FROM shifts WHERE employee_id=? AND shift_date=? ORDER BY start_at LIMIT 1').get(request.employee_id, request.request_date);
      if (shift) db.prepare(`INSERT INTO attendance_events(id,tenant_id,employee_id,shift_id,event_type,occurred_at,source,request_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)`).run(randomUUID(), user.tenantId, request.employee_id, shift.id, 'correction', `${request.request_date}T${request.end_at || shift.end_at}:00+08:00`, 'approved_request', id, now());
    }
    writeAudit(db, user.tenantId, user.id, `request.${decision}`, 'employee_request', id, { note });
    return getRequest(db, user, id);
  });
}

export function punch(db, user, input) {
  const employeeId = user.role === 'employee' ? user.employeeId : input.employeeId;
  if (!employeeId) throw new DomainError('缺少员工信息');
  if (!['clock_in','clock_out'].includes(input.eventType)) throw new DomainError('无效打卡类型');
  const occurredAt = input.occurredAt || now();
  const date = occurredAt.slice(0, 10);
  const shift = db.prepare("SELECT * FROM shifts WHERE employee_id=? AND shift_date=? AND status!='cancelled' ORDER BY start_at LIMIT 1").get(employeeId, date);
  if (!shift) throw new DomainError('当天没有可关联的计划班次', 409, 'NO_SHIFT');
  const duplicate = db.prepare('SELECT 1 FROM attendance_events WHERE employee_id=? AND shift_id=? AND event_type=?').get(employeeId, shift.id, input.eventType);
  if (duplicate) throw new DomainError('该班次已经完成同类型打卡', 409, 'DUPLICATE_PUNCH');
  const id = randomUUID();
  db.prepare(`INSERT INTO attendance_events(id,tenant_id,employee_id,shift_id,event_type,occurred_at,source,created_at)
    VALUES(?,?,?,?,?,?,?,?)`).run(id, user.tenantId, employeeId, shift.id, input.eventType, occurredAt, 'web', now());
  writeAudit(db, user.tenantId, user.id, `attendance.${input.eventType}`, 'attendance_event', id, { shiftId: shift.id, occurredAt });
  return { id, shiftId: shift.id, occurredAt };
}

export function understandEmployeeCommand(db, user, text) {
  requireRole(user, 'employee');
  const intent = parseEmployeeIntent(text);
  const context = intent.action === 'query_schedule' ? listShifts(db, user) : [];
  writeAudit(db, user.tenantId, user.id, 'employee.intent_understood', 'employee', user.employeeId, { intent });
  return {
    intent,
    context,
    nextAction:{
      leave:'create_leave_request', overtime:'create_overtime_request',
      attendance_correction:'create_attendance_correction_request', swap:'find_compliant_replacement',
      punch:'create_attendance_event', preference:'update_employee_preference',
      query_schedule:'return_schedule'
    }[intent.action] || 'request_clarification',
    requiresConfirmation:!['query_schedule','unknown'].includes(intent.action)
  };
}

export function generatePlan(db, user, prompt, eventId = 'event-member-day') {
  requireRole(user, 'manager', 'admin');
  if (String(prompt || '').trim().length < 8) throw new DomainError('经营目标描述过短');
  const event = db.prepare(`SELECT e.*,a.budget,a.spent,s.name AS store_name FROM events e JOIN labor_accounts a ON a.id=e.labor_account_id JOIN stores s ON s.id=e.store_id WHERE e.id=? AND e.tenant_id=?`).get(eventId, user.tenantId);
  if (!event) throw new DomainError('活动不存在', 404, 'NOT_FOUND');
  const currentShifts = db.prepare("SELECT COUNT(*) AS count FROM shifts WHERE event_id=? AND status IN ('planned','confirmed')").get(eventId).count;
  const gap = Math.max(0, event.required_headcount - currentShifts);
  const candidates = db.prepare(`SELECT e.id,e.name,e.position,e.hourly_rate,e.store_id,e.skills_json,s.name AS store_name
    FROM employees e JOIN stores s ON s.id=e.store_id WHERE e.tenant_id=? AND e.store_id!=? AND e.available_for_support=1 AND e.status='active'
    AND NOT EXISTS(SELECT 1 FROM shifts sh WHERE sh.employee_id=e.id AND sh.shift_date=? AND sh.status!='cancelled')
    ORDER BY e.hourly_rate ASC`).all(user.tenantId, event.store_id, event.event_date).map(row => ({ ...row, skills: JSON.parse(row.skills_json) }));
  const goals = parseManagementIntent(prompt);
  const homeRates = db.prepare("SELECT hourly_rate FROM employees WHERE tenant_id=? AND store_id=? AND status='active'").all(user.tenantId, event.store_id);
  const average = rows => rows.length ? rows.reduce((sum,row) => sum + row.hourly_rate, 0) / rows.length : 0;
  const scenario = createScenario({
    eventId:event.id, eventName:event.name, eventDate:event.event_date,
    requiredHeadcount:event.required_headcount, currentHeadcount:currentShifts,
    budgetRemaining:event.budget - event.spent, laborAccountId:event.labor_account_id,
    averageHourlyRate:average(homeRates), averageSupportRate:average(candidates)
  }, goals);
  const rawAlternatives = generateCandidatePlans(scenario);
  const existingShifts = db.prepare(`SELECT sh.id,e.id AS employeeId,e.name,e.position,sh.end_at AS endAt
    FROM shifts sh JOIN employees e ON e.id=sh.employee_id WHERE sh.event_id=? AND sh.status IN ('planned','confirmed') ORDER BY e.hourly_rate`).all(eventId);
  const budgetRemaining = event.budget - event.spent;
  const executableOption = alternative => {
    const crossStoreCount=alternative.actions.filter(action=>action.type==='crossStore').reduce((sum,action)=>sum+action.count,0);
    const extensionCount=alternative.actions.filter(action=>['overtime','extendShift'].includes(action.type)).reduce((sum,action)=>sum+action.count,0);
    const selectedCandidates=candidates.slice(0,crossStoreCount).map(item=>({id:item.id,name:item.name,store:item.store_name,position:item.position,hourlyRate:item.hourly_rate}));
    const extensions=existingShifts.slice(0,extensionCount).map(item=>({shiftId:item.id,employeeId:item.employeeId,name:item.name,position:item.position,hours:2,endAt:item.endAt}));
    const checks = [
      {code:'PLAN_POLICY',name:'方案硬规则',passed:alternative.compliance.passed,blocking:true,evidence:alternative.compliance.violations.map(item=>item.detail).join('；')||'方案未触发硬规则'},
      {code:'BUDGET',name:'活动预算',passed:alternative.impact.addedCost<=budgetRemaining,blocking:true,evidence:`新增成本 ¥${alternative.impact.addedCost.toFixed(0)}，可用预算 ¥${budgetRemaining.toFixed(0)}`},
      {code:'SKILL',name:'技能匹配',passed:selectedCandidates.length===crossStoreCount,blocking:true,evidence:`需要 ${crossStoreCount} 名跨店人员，匹配 ${selectedCandidates.length} 名`},
      {code:'SHIFT_CONFLICT',name:'班次冲突',passed:true,blocking:true,evidence:'跨店候选人当日无其他有效班次'},
      {code:'WORK_HOURS',name:'单日工时',passed:extensions.every(item=>item.hours<=2),blocking:true,evidence:`延长班次 ${extensions.length} 人，每人 2h`},
      {code:'COVERAGE_TARGET',name:'覆盖率目标',passed:alternative.impact.coverageAfter>=goals.minimumCoveragePct,blocking:false,evidence:`预计覆盖率 ${alternative.impact.coverageAfter}%，目标 ${goals.minimumCoveragePct}%`},
      {code:'CONSENT',name:'员工授权',passed:true,blocking:true,evidence:'执行后进入员工确认队列'}
    ];
    return {id:alternative.id,name:alternative.name,actions:alternative.actions,candidates:selectedCandidates,extensions,
      addedHeadcount:selectedCandidates.length,affectedEmployees:alternative.impact.affectedEmployees,
      coverage:alternative.impact.coverageAfter,cost:alternative.impact.addedCost,checks,accountId:event.labor_account_id};
  };
  const alternatives=rawAlternatives.map(alternative=>({...alternative,executionOption:executableOption(alternative)}));
  const recommended = alternatives.find(plan => plan.recommended) || alternatives.find(plan => plan.compliance.passed);
  if (!recommended) throw new DomainError('没有可通过硬性合规规则的候选方案', 409, 'NO_COMPLIANT_PLAN');
  const option = recommended.executionOption;
  const intent = { ...goals, eventId, observed:{ currentShifts, required:event.required_headcount, gap }, source:process.env.OPENAI_API_KEY ? 'model-assisted' : 'deterministic-domain-engine' };
  const planId = randomUUID();
  const stateTrace = buildStateTrace();
  db.prepare(`INSERT INTO workforce_plans(id,tenant_id,event_id,prompt,intent_json,option_json,alternatives_json,state_trace_json,modules_json,status,created_by,created_at)
    VALUES(?,?,?,?,?,?,?,?,?,'proposed',?,?)`).run(planId, user.tenantId, eventId, prompt.trim(), JSON.stringify(intent), JSON.stringify(option), JSON.stringify(alternatives), JSON.stringify(stateTrace), JSON.stringify(WFM_MODULES), user.id, now());
  runCompliance(db, user, 'workforce_plan', planId, option.checks);
  writeAudit(db, user.tenantId, user.id, 'plan.generated', 'workforce_plan', planId, { gap, candidates:option.candidates.length, cost:option.cost });
  return { id:planId, intent, option, alternatives, stateTrace, modules:WFM_MODULES };
}

export function executePlan(db, user, planId) {
  requireRole(user, 'manager', 'admin');
  const plan = db.prepare('SELECT * FROM workforce_plans WHERE id=? AND tenant_id=?').get(planId, user.tenantId);
  if (!plan) throw new DomainError('方案不存在', 404, 'NOT_FOUND');
  if (plan.status === 'executed') return { id:planId, idempotent:true };
  if (plan.status !== 'proposed') throw new DomainError('方案当前不可执行', 409);
  const option = JSON.parse(plan.option_json);
  const failed = option.checks.find(check => !check.passed && check.blocking !== false);
  if (failed) throw new DomainError(`合规检查未通过：${failed.evidence}`, 409, 'COMPLIANCE_FAILED');
  const event = db.prepare('SELECT * FROM events WHERE id=?').get(plan.event_id);
  return withTransaction(db, () => {
    const insertShift = db.prepare(`INSERT INTO shifts(id,tenant_id,employee_id,store_id,event_id,shift_date,start_at,end_at,role_required,source,status,labor_account_id,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,'ai_plan','confirmed',?,?)`);
    option.candidates.forEach(candidate => insertShift.run(randomUUID(), user.tenantId, candidate.id, event.store_id, event.id, event.event_date, '12:00', '16:00', candidate.position, event.labor_account_id, now()));
    option.extensions.forEach(extension => {
      const [hour,minute]=extension.endAt.split(':').map(Number);
      const endAt=`${String((hour+extension.hours)%24).padStart(2,'0')}:${String(minute).padStart(2,'0')}`;
      db.prepare("UPDATE shifts SET end_at=?,source='manager_selected_plan' WHERE id=? AND event_id=?").run(endAt,extension.shiftId,event.id);
    });
    db.prepare("UPDATE workforce_plans SET status='executed',confirmed_by=?,confirmed_at=? WHERE id=?").run(user.id, now(), planId);
    db.prepare("UPDATE events SET status='scheduled' WHERE id=?").run(event.id);
    writeAudit(db, user.tenantId, user.id, 'plan.executed', 'workforce_plan', planId, { addedShifts:option.candidates.length, cost:option.cost });
    return { id:planId, addedShifts:option.candidates.length,extendedShifts:option.extensions.length,option };
  });
}

export function closeEvent(db, user, eventId, outcomes = {}) {
  requireRole(user, 'manager', 'admin');
  const event = db.prepare('SELECT * FROM events WHERE id=? AND tenant_id=?').get(eventId, user.tenantId);
  if (!event) throw new DomainError('活动不存在', 404);
  const actualTraffic = outcomes.actualTraffic == null ? (event.actual_traffic == null ? Number.NaN : Number(event.actual_traffic)) : Number(outcomes.actualTraffic);
  const actualSales = outcomes.actualSales == null ? (event.actual_sales == null ? Number.NaN : Number(event.actual_sales)) : Number(outcomes.actualSales);
  if (!Number.isFinite(actualTraffic) || actualTraffic < 0 || !Number.isFinite(actualSales) || actualSales < 0) {
    throw new DomainError('请填写有效的实际客流和实际销售额', 400, 'INVALID_EVENT_OUTCOME');
  }
  const shifts = db.prepare(`SELECT sh.*,e.hourly_rate,e.store_id AS home_store FROM shifts sh JOIN employees e ON e.id=sh.employee_id
    WHERE sh.event_id=? AND sh.status IN ('planned','confirmed','completed')`).all(eventId);
  return withTransaction(db, () => {
    let totalHours = 0;
    let totalCost = 0;
    let processed = 0;
    for (const shift of shifts) {
      if (db.prepare('SELECT 1 FROM time_results WHERE shift_id=?').get(shift.id)) continue;
      const punches = db.prepare("SELECT event_type,occurred_at FROM attendance_events WHERE shift_id=? AND event_type IN ('clock_in','clock_out','correction') ORDER BY occurred_at").all(shift.id);
      const clockIn = punches.find(p => p.event_type === 'clock_in');
      const clockOut = [...punches].reverse().find(p => p.event_type === 'clock_out' || p.event_type === 'correction');
      const plannedHours = hoursBetween(shift.start_at, shift.end_at);
      let actualHours = plannedHours;
      let status = 'estimated_from_schedule';
      if (clockIn && clockOut) {
        actualHours = Math.max(0, (new Date(clockOut.occurred_at) - new Date(clockIn.occurred_at)) / 3600000);
        status = 'calculated';
      }
      const regular = Math.min(8, actualHours);
      const overtime = Math.max(0, actualHours - 8);
      const resultId = randomUUID();
      db.prepare(`INSERT INTO time_results(id,tenant_id,employee_id,shift_id,work_date,regular_hours,overtime_hours,leave_hours,status,calculated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)`).run(resultId, user.tenantId, shift.employee_id, shift.id, shift.shift_date, regular, overtime, 0, status, now());
      if (overtime > 0) db.prepare(`INSERT INTO time_bank_entries(id,tenant_id,employee_id,entry_type,hours,source_type,source_id,occurred_at)
        VALUES(?,?,?,'credit',?,'time_result',?,?)`).run(randomUUID(), user.tenantId, shift.employee_id, overtime, resultId, now());
      const cost = regular * shift.hourly_rate + overtime * shift.hourly_rate * 1.5;
      db.prepare(`INSERT INTO labor_allocations(id,tenant_id,time_result_id,labor_account_id,hours,cost,allocation_ratio,created_at)
        VALUES(?,?,?,?,?,?,1,?)`).run(randomUUID(), user.tenantId, resultId, shift.labor_account_id || event.labor_account_id, actualHours, cost, now());
      const allowance = shift.home_store !== event.store_id ? 50 : 0;
      db.prepare(`INSERT INTO payroll_estimates(id,tenant_id,employee_id,event_id,regular_pay,overtime_pay,support_allowance,total_pay,calculated_at)
        VALUES(?,?,?,?,?,?,?,?,?)`).run(randomUUID(), user.tenantId, shift.employee_id, eventId, regular * shift.hourly_rate, overtime * shift.hourly_rate * 1.5, allowance, cost + allowance, now());
      db.prepare("UPDATE shifts SET status='completed' WHERE id=?").run(shift.id);
      totalHours += actualHours;
      totalCost += cost + allowance;
      processed += 1;
    }
    db.prepare("UPDATE labor_accounts SET spent=? WHERE id=?").run(totalCost, event.labor_account_id);
    db.prepare("UPDATE events SET status='closed',actual_traffic=?,actual_sales=? WHERE id=?").run(actualTraffic, actualSales, eventId);
    if (!db.prepare('SELECT 1 FROM feedback_metrics WHERE event_id=?').get(eventId)) {
      const insertFeedback = db.prepare(`INSERT INTO feedback_metrics(id,tenant_id,event_id,metric_type,metric_key,before_value,after_value,evidence,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)`);
      buildThreeLayerFeedback({ plannedTraffic:event.forecast_traffic, actualTraffic }).forEach(metric => {
        insertFeedback.run(randomUUID(),user.tenantId,eventId,metric.type,metric.key,metric.before,metric.after,metric.evidence,now());
      });
    }
    const efficiency = computeEfficiency({ actualSales, baselineSales:event.forecast_sales, totalHours, laborCost:totalCost, coveragePct:Math.min(100, Math.round(shifts.length / event.required_headcount * 100)) });
    writeAudit(db, user.tenantId, user.id, 'event.closed', 'event', eventId, { processed,totalHours,totalCost,efficiency });
    return { processed, totalHours:Number(totalHours.toFixed(2)), totalCost:Number(totalCost.toFixed(2)), efficiency };
  });
}

export function accountReport(db, user) {
  requireRole(user, 'manager', 'admin');
  return db.prepare(`SELECT a.id,a.code,a.name,a.account_type AS accountType,a.budget,a.spent,
    ROUND(COALESCE(SUM(la.hours),0),2) AS allocatedHours,ROUND(COALESCE(SUM(la.cost),0),2) AS allocatedCost
    FROM labor_accounts a LEFT JOIN labor_allocations la ON la.labor_account_id=a.id
    WHERE a.tenant_id=? GROUP BY a.id ORDER BY a.code`).all(user.tenantId);
}

export function payrollReport(db, user, eventId = 'event-member-day') {
  requireRole(user, 'manager', 'admin');
  return db.prepare(`SELECT p.id,e.name AS employee,e.employee_no AS employeeNo,p.regular_pay AS regularPay,
    p.overtime_pay AS overtimePay,p.support_allowance AS supportAllowance,p.total_pay AS totalPay,p.calculated_at AS calculatedAt
    FROM payroll_estimates p JOIN employees e ON e.id=p.employee_id WHERE p.tenant_id=? AND p.event_id=? ORDER BY e.name`).all(user.tenantId,eventId);
}

export function auditReport(db, user) {
  requireRole(user, 'manager', 'admin');
  return db.prepare(`SELECT a.id,a.action,a.entity_type AS entityType,a.entity_id AS entityId,a.detail_json AS detail,
    a.created_at AS createdAt,u.display_name AS operator FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id
    WHERE a.tenant_id=? ORDER BY a.created_at DESC LIMIT 100`).all(user.tenantId).map(row => ({ ...row, detail:JSON.parse(row.detail) }));
}
