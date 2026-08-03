import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createDatabase } from './db.mjs';
import { confirmEmployeeCommand } from './orchestration.mjs';

const employeeUser = {
  id:'user-employee', tenantId:'tenant-demo', tenantCode:'DEMO', role:'employee', employeeId:'emp-linxiao'
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
