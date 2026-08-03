import { randomUUID } from 'node:crypto';
import { dashboard, generatePlan, executePlan, listShifts, createRequest, punch } from './domain.mjs';
import { forecastDemand, latestForecast } from './forecast.mjs';
import { runWorkforceAgent, interpretEmployeeCommand, DEFAULT_MODEL } from './openai-agent.mjs';
import { WFM_MODULES, parseManagementIntent, parseEmployeeIntent } from './ai-native-engine.mjs';

const now = () => new Date().toISOString();
const FALLBACK_MODEL = 'deterministic-wfm-v1';
const hasOpenAIKey = () => /^sk-/.test(String(process.env.OPENAI_API_KEY || ''));

function requireManager(user) {
  if (!['manager','admin'].includes(user.role)) throw Object.assign(new Error('当前角色无权执行此操作'),{status:403,code:'FORBIDDEN'});
}

export function operationsContext(db,user,eventId='event-member-day') {
  requireManager(user);
  const data = dashboard(db,user);
  const employeeRequests = db.prepare(`SELECT r.request_type AS requestType,r.status,r.hours,r.request_date AS requestDate,e.name AS employee
    FROM employee_requests r JOIN employees e ON e.id=r.employee_id WHERE r.tenant_id=? ORDER BY r.created_at DESC LIMIT 10`).all(user.tenantId);
  const swapRequests = db.prepare(`SELECT 'swap' AS requestType,r.status,0 AS hours,s.shift_date AS requestDate,e.name AS employee
    FROM shift_swap_requests r JOIN employees e ON e.id=r.employee_id JOIN shifts s ON s.id=r.shift_id
    WHERE r.tenant_id=? ORDER BY r.created_at DESC LIMIT 10`).all(user.tenantId);
  const requests = [...employeeRequests,...swapRequests].sort((a,b)=>String(b.requestDate).localeCompare(String(a.requestDate))).slice(0,10);
  const latestRun = db.prepare(`SELECT id,status,model,prompt,plan_id AS planId,error_message AS errorMessage,created_at AS createdAt,completed_at AS completedAt
    FROM agent_runs WHERE tenant_id=? AND event_id=? ORDER BY created_at DESC LIMIT 1`).get(user.tenantId,eventId) || null;
  const rules = db.prepare(`SELECT rule_code AS ruleCode,source,severity,version,effective_from AS effectiveFrom,is_demo_rule AS isDemoRule
    FROM compliance_rules WHERE tenant_id=? AND status='active' ORDER BY rule_code`).all(user.tenantId);
  const configured=hasOpenAIKey();
  return { ...data, requests, latestRun, forecast:latestForecast(db,user,eventId), rules, modules:WFM_MODULES, ai:{ configured, mode:configured?'openai':'deterministic', model:configured?DEFAULT_MODEL:FALLBACK_MODEL } };
}

function workforceToolContext(db,user,eventId) {
  const event = db.prepare(`SELECT e.id,e.name,e.event_date AS eventDate,e.required_headcount AS requiredHeadcount,
    e.forecast_traffic AS storedForecastTraffic,a.budget,a.spent,s.name AS store
    FROM events e JOIN labor_accounts a ON a.id=e.labor_account_id JOIN stores s ON s.id=e.store_id
    WHERE e.id=? AND e.tenant_id=?`).get(eventId,user.tenantId);
  const employees = db.prepare(`SELECT e.id,e.name,e.position,e.hourly_rate AS hourlyRate,e.skills_json AS skills,
    e.available_for_support AS availableForSupport,s.name AS homeStore
    FROM employees e JOIN stores s ON s.id=e.store_id WHERE e.tenant_id=? AND e.status='active'`).all(user.tenantId).map(row => ({...row,skills:JSON.parse(row.skills),availableForSupport:Boolean(row.availableForSupport)}));
  const shifts = db.prepare(`SELECT employee_id AS employeeId,start_at AS startAt,end_at AS endAt,status,store_id AS storeId
    FROM shifts WHERE tenant_id=? AND event_id=? AND status!='cancelled'`).all(user.tenantId,eventId);
  const pendingRequests = db.prepare(`SELECT request_type AS type,request_date AS date,hours,status FROM employee_requests
    WHERE tenant_id=? AND status='pending'`).all(user.tenantId);
  return { event, employees, shifts, pendingRequests };
}

export async function createAgentRun(db,user,{prompt,eventId='event-member-day'}) {
  requireManager(user);
  if (String(prompt || '').trim().length < 8) throw Object.assign(new Error('经营目标描述过短'),{status:400,code:'INVALID_PROMPT'});
  const runId = randomUUID();
  const model=hasOpenAIKey()?DEFAULT_MODEL:FALLBACK_MODEL;
  db.prepare(`INSERT INTO agent_runs(id,tenant_id,user_id,event_id,prompt,model,status,created_at) VALUES(?,?,?,?,?,?,?,?)`)
    .run(runId,user.tenantId,user.id,eventId,prompt.trim(),model,'UNDERSTANDING',now());
  let sequence = 0;
  const recordStep = ({state,toolName=null,input={},output=null,durationMs=0,status='completed'}) => {
    sequence += 1;
    db.prepare(`INSERT INTO agent_steps(id,run_id,sequence,state,tool_name,input_json,output_json,duration_ms,status,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)`).run(randomUUID(),runId,sequence,state,toolName,JSON.stringify(input),output == null ? null : JSON.stringify(output),durationMs,status,now());
    db.prepare('UPDATE agent_runs SET status=? WHERE id=?').run(state,runId);
  };
  try {
    recordStep({state:'UNDERSTANDING',input:{prompt}});
    let agent;
    if(hasOpenAIKey()) agent = await runWorkforceAgent({
      prompt,eventId,safetyIdentifier:`tenant_${user.tenantId}_user_${user.id}`,
      tools:{
        get_workforce_context:async () => workforceToolContext(db,user,eventId),
        run_demand_forecast:async () => forecastDemand(db,user,eventId),
        get_compliance_rules:async () => db.prepare(`SELECT rule_code AS ruleCode,expression_json AS expression,source,severity,version
          FROM compliance_rules WHERE tenant_id=? AND status='active'`).all(user.tenantId).map(row => ({...row,expression:JSON.parse(row.expression)}))
      },
      onStep:async step => recordStep({state:step.toolName === 'run_demand_forecast' ? 'GENERATING_OPTIONS' : 'COLLECTING_CONTEXT',...step})
    });
    else {
      const started=Date.now();
      const context=workforceToolContext(db,user,eventId);
      recordStep({state:'COLLECTING_CONTEXT',toolName:'get_workforce_context',output:context,durationMs:Date.now()-started});
      const forecastStarted=Date.now();
      const forecast=forecastDemand(db,user,eventId);
      recordStep({state:'GENERATING_OPTIONS',toolName:'run_demand_forecast',output:forecast,durationMs:Date.now()-forecastStarted});
      const rules=db.prepare(`SELECT rule_code AS ruleCode,expression_json AS expression,source,severity,version FROM compliance_rules WHERE tenant_id=? AND status='active'`).all(user.tenantId).map(row=>({...row,expression:JSON.parse(row.expression)}));
      recordStep({state:'COLLECTING_CONTEXT',toolName:'get_compliance_rules',output:rules});
      agent={model:FALLBACK_MODEL,responseId:null,intent:{...parseManagementIntent(prompt),eventId,summary:'由数据库上下文、统计预测和确定性规则引擎生成',rationale:['历史需求预测','实时人员与班次','版本化合规规则']}};
    }
    recordStep({state:'CHECKING_COMPLIANCE',output:{intent:agent.intent}});
    const plan = generatePlan(db,user,prompt,eventId);
    recordStep({state:'SIMULATING_IMPACT',output:{planId:plan.id,alternatives:plan.alternatives}});
    db.prepare(`UPDATE agent_runs SET status='AWAITING_CONFIRMATION',intent_json=?,plan_id=?,response_id=?,completed_at=? WHERE id=?`)
      .run(JSON.stringify(agent.intent),plan.id,agent.responseId,now(),runId);
    return { id:runId,status:'AWAITING_CONFIRMATION',model:agent.model,intent:agent.intent,plan };
  } catch (error) {
    db.prepare(`UPDATE agent_runs SET status='NEEDS_ATTENTION',error_message=?,completed_at=? WHERE id=?`).run(error.message,now(),runId);
    recordStep({state:'NEEDS_ATTENTION',output:{error:error.message,code:error.code || 'AGENT_ERROR'},status:'failed'});
    throw error;
  }
}

export function getAgentRun(db,user,id) {
  requireManager(user);
  const run = db.prepare(`SELECT id,event_id AS eventId,prompt,model,status,intent_json AS intent,plan_id AS planId,response_id AS responseId,
    error_message AS errorMessage,created_at AS createdAt,completed_at AS completedAt FROM agent_runs WHERE id=? AND tenant_id=?`).get(id,user.tenantId);
  if (!run) throw Object.assign(new Error('Agent 运行记录不存在'),{status:404,code:'NOT_FOUND'});
  const steps = db.prepare(`SELECT sequence,state,tool_name AS toolName,input_json AS input,output_json AS output,duration_ms AS durationMs,status,created_at AS createdAt
    FROM agent_steps WHERE run_id=? ORDER BY sequence`).all(id).map(row => ({...row,input:JSON.parse(row.input),output:row.output?JSON.parse(row.output):null}));
  return {...run,intent:run.intent?JSON.parse(run.intent):null,steps};
}

export function selectAgentPlan(db,user,id,optionId) {
  requireManager(user);
  const run=db.prepare(`SELECT * FROM agent_runs WHERE id=? AND tenant_id=?`).get(id,user.tenantId);
  if(!run)throw Object.assign(new Error('Agent 运行记录不存在'),{status:404,code:'NOT_FOUND'});
  if(run.status!=='AWAITING_CONFIRMATION')throw Object.assign(new Error('当前运行不可切换方案'),{status:409,code:'INVALID_AGENT_STATE'});
  const plan=db.prepare(`SELECT * FROM workforce_plans WHERE id=? AND tenant_id=?`).get(run.plan_id,user.tenantId);
  const alternative=JSON.parse(plan.alternatives_json||'[]').find(item=>item.id===optionId);
  if(!alternative)throw Object.assign(new Error('候选方案不存在'),{status:404,code:'OPTION_NOT_FOUND'});
  const option=alternative.executionOption;
  db.prepare('UPDATE workforce_plans SET option_json=? WHERE id=?').run(JSON.stringify(option),plan.id);
  db.prepare(`INSERT INTO decision_feedback(id,tenant_id,plan_id,user_id,action,reason,created_at) VALUES(?,?,?,?,?,?,?)`)
    .run(randomUUID(),user.tenantId,plan.id,user.id,'selected_alternative',optionId,now());
  db.prepare(`INSERT INTO agent_steps(id,run_id,sequence,state,tool_name,input_json,output_json,duration_ms,status,created_at)
    VALUES(?,?,?,?,?,?,?,?,?,?)`).run(randomUUID(),id,900,'AWAITING_CONFIRMATION','select_plan',JSON.stringify({optionId}),JSON.stringify(option),0,'completed',now());
  return {runId:id,planId:plan.id,option,alternative};
}

export function confirmAgentRun(db,user,id) {
  requireManager(user);
  const run = db.prepare(`SELECT * FROM agent_runs WHERE id=? AND tenant_id=?`).get(id,user.tenantId);
  if (!run) throw Object.assign(new Error('Agent 运行记录不存在'),{status:404,code:'NOT_FOUND'});
  if (run.status === 'COMPLETED') return {id,status:'COMPLETED',idempotent:true};
  if (run.status !== 'AWAITING_CONFIRMATION') throw Object.assign(new Error('当前 Agent 运行不可确认'),{status:409,code:'INVALID_AGENT_STATE'});
  const result = executePlan(db,user,run.plan_id);
  db.prepare(`UPDATE agent_runs SET status='COMPLETED',completed_at=? WHERE id=?`).run(now(),id);
  db.prepare(`INSERT INTO decision_feedback(id,tenant_id,plan_id,user_id,action,reason,weight_before,weight_after,created_at)
    VALUES(?,?,?,?,?,?,?,?,?)`).run(randomUUID(),user.tenantId,run.plan_id,user.id,'accepted','管理者确认执行',0.60,0.64,now());
  db.prepare(`INSERT INTO agent_steps(id,run_id,sequence,state,tool_name,input_json,output_json,duration_ms,status,created_at)
    VALUES(?,?,?,?,?,?,?,?,?,?)`).run(randomUUID(),id,999,'COMPLETED','execute_confirmed_plan','{}',JSON.stringify(result),0,'completed',now());
  return {id,status:'COMPLETED',execution:result};
}

export async function createEmployeeCommand(db,user,text) {
  if(user.role!=='employee')throw Object.assign(new Error('仅员工账号可使用员工伙伴'),{status:403,code:'FORBIDDEN'});
  if(String(text||'').trim().length<2)throw Object.assign(new Error('请输入具体诉求'),{status:400,code:'INVALID_COMMAND'});
  const schedules=listShifts(db,user);
  const ai=hasOpenAIKey()?await interpretEmployeeCommand({text:text.trim(),context:schedules,safetyIdentifier:`tenant_${user.tenantId}_employee_${user.employeeId}`}):deterministicEmployeeCommand(text.trim(),schedules);
  const id=randomUUID();
  const immediate=ai.intent.action==='query_schedule'?{schedules}:null;
  const status=ai.intent.requiresConfirmation?'awaiting_confirmation':'completed';
  db.prepare(`INSERT INTO employee_commands(id,tenant_id,user_id,employee_id,raw_text,model,intent_json,status,result_json,created_at)
    VALUES(?,?,?,?,?,?,?,?,?,?)`).run(id,user.tenantId,user.id,user.employeeId,text.trim(),ai.model,JSON.stringify(ai.intent),status,immediate?JSON.stringify(immediate):null,now());
  return {id,status,model:ai.model,intent:ai.intent,result:immediate};
}

function deterministicEmployeeCommand(text,schedules){
  const parsed=parseEmployeeIntent(text);
  const hoursMatch=text.match(/(\d+(?:\.\d+)?)\s*(?:小时|h)/i);
  let requestDate=null;
  if(parsed.dayOfWeek!=null){
    const base=new Date();
    const delta=(parsed.dayOfWeek-base.getDay()+7)%7 || 7;
    base.setDate(base.getDate()+delta);
    requestDate=base.toISOString().slice(0,10);
  }
  if(parsed.action==='swap'&&!requestDate)requestDate=schedules.find(shift=>['planned','confirmed'].includes(shift.status))?.shiftDate||null;
  const hours=hoursMatch?Number(hoursMatch[1]):parsed.action==='leave'?8:parsed.action==='overtime'?2:parsed.action==='attendance_correction'?1:null;
  return {model:FALLBACK_MODEL,responseId:null,intent:{
    action:parsed.action,requestDate,startAt:null,endAt:null,hours,reason:parsed.reason||text,
    punchType:parsed.action==='punch'?parsed.punchType:null,summary:text,
    requiresConfirmation:!['query_schedule','unknown'].includes(parsed.action)
  }};
}

export function confirmEmployeeCommand(db,user,id) {
  if(user.role!=='employee')throw Object.assign(new Error('仅员工账号可确认'),{status:403,code:'FORBIDDEN'});
  const command=db.prepare('SELECT * FROM employee_commands WHERE id=? AND tenant_id=? AND user_id=?').get(id,user.tenantId,user.id);
  if(!command)throw Object.assign(new Error('员工命令不存在'),{status:404,code:'NOT_FOUND'});
  if(command.status==='completed')return {id,status:'completed',result:command.result_json?JSON.parse(command.result_json):null,idempotent:true};
  if(command.status!=='awaiting_confirmation')throw Object.assign(new Error('当前命令不可确认'),{status:409,code:'INVALID_COMMAND_STATE'});
  const intent=JSON.parse(command.intent_json);
  let result;
  if(['leave','overtime','attendance_correction'].includes(intent.action)){
    result=createRequest(db,user,{requestType:intent.action,requestDate:intent.requestDate,startAt:intent.startAt,endAt:intent.endAt,hours:intent.hours||1,reason:intent.reason||intent.summary});
  }else if(intent.action==='punch'){
    result=punch(db,user,{eventType:intent.punchType,occurredAt:intent.requestDate&&intent.startAt?`${intent.requestDate}T${intent.startAt}:00+08:00`:undefined});
  }else if(intent.action==='swap'){
    if(!intent.requestDate)throw Object.assign(new Error('换班诉求缺少日期，请重新描述要交换的班次'),{status:400,code:'MISSING_SHIFT_DATE'});
    const shift=db.prepare("SELECT id,shift_date AS shiftDate,start_at AS startAt,end_at AS endAt,role_required AS roleRequired FROM shifts WHERE employee_id=? AND shift_date=? AND status IN ('planned','confirmed') ORDER BY start_at LIMIT 1").get(user.employeeId,intent.requestDate);
    if(!shift)throw Object.assign(new Error('该日期没有可申请交换的有效班次'),{status:409,code:'NO_SWAPPABLE_SHIFT'});
    const swapId=randomUUID();
    db.prepare(`INSERT INTO shift_swap_requests(id,tenant_id,employee_id,shift_id,reason,status,created_at) VALUES(?,?,?,?,?,'pending',?)`)
      .run(swapId,user.tenantId,user.employeeId,shift.id,intent.reason||intent.summary,now());
    db.prepare(`INSERT INTO audit_logs(id,tenant_id,user_id,action,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)`)
      .run(randomUUID(),user.tenantId,user.id,'shift_swap.requested','shift_swap_request',swapId,JSON.stringify({shiftId:shift.id,reason:intent.reason||intent.summary}),now());
    result={id:swapId,type:'shift_swap',status:'pending',shift};
  }else if(intent.action==='preference'){
    const current=db.prepare("SELECT COALESCE(MAX(version),0) AS version FROM employee_preferences WHERE employee_id=?").get(user.employeeId);
    db.prepare("UPDATE employee_preferences SET status='superseded',superseded_at=? WHERE employee_id=? AND status='active'").run(now(),user.employeeId);
    const preferenceId=randomUUID();
    db.prepare(`INSERT INTO employee_preferences(id,tenant_id,employee_id,preference_text,effective_date,status,version,created_at) VALUES(?,?,?,?,?,'active',?,?)`)
      .run(preferenceId,user.tenantId,user.employeeId,intent.reason||intent.summary,intent.requestDate||null,current.version+1,now());
    db.prepare(`INSERT INTO audit_logs(id,tenant_id,user_id,action,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)`)
      .run(randomUUID(),user.tenantId,user.id,'preference.updated','employee_preference',preferenceId,JSON.stringify({version:current.version+1}),now());
    result={id:preferenceId,type:'preference',status:'active',version:current.version+1,preferenceText:intent.reason||intent.summary};
  }else{
    throw Object.assign(new Error('该员工诉求尚不支持自动写入，已保留为待人工处理'),{status:409,code:'MANUAL_PROCESS_REQUIRED'});
  }
  db.prepare(`UPDATE employee_commands SET status='completed',result_json=?,confirmed_at=? WHERE id=?`).run(JSON.stringify(result),now(),id);
  return {id,status:'completed',result};
}

export function resetDemoScenario(db,user) {
  requireManager(user);
  if(user.tenantCode!=='DEMO')throw Object.assign(new Error('仅演示租户允许重置剧情'),{status:403,code:'RESET_NOT_ALLOWED'});
  db.exec('BEGIN IMMEDIATE');
  try{
    const runIds=db.prepare('SELECT id FROM agent_runs WHERE tenant_id=?').all(user.tenantId).map(row=>row.id);
    const planIds=db.prepare('SELECT id FROM workforce_plans WHERE tenant_id=?').all(user.tenantId).map(row=>row.id);
    runIds.forEach(id=>db.prepare('DELETE FROM agent_steps WHERE run_id=?').run(id));
    planIds.forEach(id=>db.prepare("DELETE FROM compliance_checks WHERE subject_type='workforce_plan' AND subject_id=?").run(id));
    ['decision_feedback','employee_commands','shift_swap_requests','employee_preferences','agent_runs','demand_forecasts','feedback_metrics','payroll_estimates','labor_allocations','time_bank_entries','time_results','attendance_events','employee_requests','workforce_plans'].forEach(table=>db.prepare(`DELETE FROM ${table} WHERE tenant_id=?`).run(user.tenantId));
    db.prepare("DELETE FROM shifts WHERE tenant_id=? AND event_id='event-member-day'").run(user.tenantId);
    const stamp=now();
    const insert=db.prepare(`INSERT INTO shifts(id,tenant_id,employee_id,store_id,event_id,shift_date,start_at,end_at,role_required,source,status,labor_account_id,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`);
    [['emp-linxiao','导购','09:00','18:00'],['emp-zhangmin','收银','10:00','19:00'],['emp-lijun','库存','12:00','21:00'],['emp-heping','管理','09:00','18:00']].forEach((row,index)=>insert.run(`reset-shift-${index}-${Date.now()}`,user.tenantId,row[0],'store-flagship','event-member-day','2026-08-08',row[2],row[3],row[1],'demo_reset','planned','account-member-day',stamp));
    db.prepare("UPDATE events SET status='planning',actual_traffic=NULL,actual_sales=NULL,required_headcount=8 WHERE id='event-member-day' AND tenant_id=?").run(user.tenantId);
    db.prepare("UPDATE labor_accounts SET spent=0 WHERE id='account-member-day' AND tenant_id=?").run(user.tenantId);
    db.prepare(`INSERT INTO audit_logs(id,tenant_id,user_id,action,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)`).run(randomUUID(),user.tenantId,user.id,'demo.reset','tenant',user.tenantId,JSON.stringify({eventId:'event-member-day'}),stamp);
    db.exec('COMMIT');
    return {reset:true,eventId:'event-member-day'};
  }catch(error){db.exec('ROLLBACK');throw error}
}
