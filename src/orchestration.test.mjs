import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createDatabase } from './db.mjs';
import { confirmEmployeeCommand, createAgentRun, createEmployeeCommand, operationsContext, selectAgentPlan, confirmAgentRun } from './orchestration.mjs';

const employeeUser = {
  id:'user-employee', tenantId:'tenant-demo', tenantCode:'DEMO', role:'employee', employeeId:'emp-linxiao'
};
const managerUser = {
  id:'user-manager', tenantId:'tenant-demo', tenantCode:'DEMO', role:'manager', employeeId:null
};

function command(db,intent) {
  const id=randomUUID();
  db.prepare(`INSERT INTO employee_commands(id,tenant_id,user_id,employee_id,raw_text,model,intent_json,status,created_at)
    VALUES(?,?,?,?,?,?,?,'awaiting_confirmation',?)`).run(id,employeeUser.tenantId,employeeUser.id,employeeUser.employeeId,intent.summary,'test-model',JSON.stringify(intent),new Date().toISOString());
  return id;
}

test('员工确认换班后生成可审批的真实换班申请和审计记录',()=>{
  const db=createDatabase(':memory:');
  const id=command(db,{action:'swap',requestDate:'2026-08-08',reason:'家庭安排',summary:'申请交换 8 月 8 日班次'});
  const result=confirmEmployeeCommand(db,employeeUser,id);
  assert.equal(result.result.type,'shift_swap');
  assert.equal(db.prepare('SELECT status FROM shift_swap_requests WHERE id=?').get(result.result.id).status,'pending');
  assert.equal(db.prepare("SELECT COUNT(*) AS count FROM audit_logs WHERE action='shift_swap.requested'").get().count,1);
});

test('员工偏好确认后保存新版本并停用旧版本',()=>{
  const db=createDatabase(':memory:');
  const first=command(db,{action:'preference',requestDate:null,reason:'优先早班',summary:'以后优先安排早班'});
  confirmEmployeeCommand(db,employeeUser,first);
  const second=command(db,{action:'preference',requestDate:null,reason:'周末优先晚班',summary:'周末优先安排晚班'});
  const result=confirmEmployeeCommand(db,employeeUser,second);
  assert.equal(result.result.version,2);
  assert.deepEqual(db.prepare('SELECT version,status FROM employee_preferences WHERE employee_id=? ORDER BY version').all(employeeUser.employeeId).map(row=>({...row})),[
    {version:1,status:'superseded'},{version:2,status:'active'}
  ]);
});

test('只有 Codex 凭证时自动使用确定性编排并生成可确认方案',async()=>{
  const original=process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY='cpx_not_an_openai_key';
  try{
    const db=createDatabase(':memory:');
    const context=operationsContext(db,managerUser);
    assert.equal(context.ai.mode,'deterministic');
    assert.equal(context.ai.model,'deterministic-wfm-v1');
    const run=await createAgentRun(db,managerUser,{prompt:'会员日覆盖率达到95%，预算不超2000元，全程合规并计入劳动力账户'});
    assert.equal(run.status,'AWAITING_CONFIRMATION');
    assert.equal(run.model,'deterministic-wfm-v1');
    assert.equal(run.plan.option.checks.every(check=>check.passed),true);
    assert.ok(db.prepare("SELECT COUNT(*) AS count FROM agent_steps WHERE run_id=? AND tool_name='run_demand_forecast'").get(run.id).count>0);
  }finally{
    if(original===undefined)delete process.env.OPENAI_API_KEY;else process.env.OPENAI_API_KEY=original;
  }
});

test('无 OpenAI Key 时员工自然语言由确定性引擎解析',async()=>{
  const original=process.env.OPENAI_API_KEY;
  delete process.env.OPENAI_API_KEY;
  try{
    const db=createDatabase(':memory:');
    const command=await createEmployeeCommand(db,employeeUser,'看看我这周的排班');
    assert.equal(command.model,'deterministic-wfm-v1');
    assert.equal(command.intent.action,'query_schedule');
    assert.equal(command.status,'completed');
    assert.ok(command.result.schedules.length>0);
  }finally{
    if(original!==undefined)process.env.OPENAI_API_KEY=original;
  }
});

test('管理者选择组合方案后后端保存并执行对应人员动作',async()=>{
  const original=process.env.OPENAI_API_KEY;
  delete process.env.OPENAI_API_KEY;
  try{
    const db=createDatabase(':memory:');
    const run=await createAgentRun(db,managerUser,{prompt:'会员日覆盖率达到95%，预算不超2000元，全程合规并计入劳动力账户'});
    const selected=selectAgentPlan(db,managerUser,run.id,'PLAN-MIX');
    assert.equal(selected.option.id,'PLAN-MIX');
    assert.equal(selected.option.candidates.length,2);
    assert.equal(selected.option.extensions.length,2);
    assert.equal(JSON.parse(db.prepare('SELECT option_json FROM workforce_plans WHERE id=?').get(run.plan.id).option_json).id,'PLAN-MIX');
    const result=confirmAgentRun(db,managerUser,run.id);
    assert.equal(result.execution.addedShifts,2);
    assert.equal(result.execution.extendedShifts,2);
  }finally{
    if(original!==undefined)process.env.OPENAI_API_KEY=original;
  }
});

test('硬规则拦截方案可以选择比较但不能确认执行',async()=>{
  const original=process.env.OPENAI_API_KEY;
  delete process.env.OPENAI_API_KEY;
  try{
    const db=createDatabase(':memory:');
    const run=await createAgentRun(db,managerUser,{prompt:'会员日覆盖率达到95%，预算不超2000元，全程合规并计入劳动力账户'});
    const selected=selectAgentPlan(db,managerUser,run.id,'PLAN-OT');
    assert.equal(selected.option.checks.some(check=>!check.passed&&check.blocking),true);
    assert.throws(()=>confirmAgentRun(db,managerUser,run.id),error=>error.code==='COMPLIANCE_FAILED');
  }finally{
    if(original!==undefined)process.env.OPENAI_API_KEY=original;
  }
});
