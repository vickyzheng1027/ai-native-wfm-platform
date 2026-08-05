import { randomUUID } from 'node:crypto';
import { parseJson,transaction } from './db.mjs';
import { err } from './ai-native-engine.mjs';
import { validateShift } from './scheduling.mjs';

const now=()=>new Date().toISOString();
const j=value=>JSON.stringify(value);
const mapJson=(row,fields)=>{const result={...row};for(const field of fields)result[field.replace('_json','')]=parseJson(row[field],{});return result;};

export function closedLoopOverview(db){
 const forecasts=db.prepare('SELECT f.*,s.name store_name FROM demand_forecasts f JOIN stores s ON s.id=f.store_id ORDER BY forecast_date DESC').all();
 const notifications=db.prepare('SELECT n.*,e.name employee_name FROM schedule_notifications n JOIN employees e ON e.id=n.employee_id ORDER BY sent_at DESC LIMIT 20').all();
 const attendance=db.prepare('SELECT a.*,e.name employee_name,sh.start_at planned_start,sh.end_at planned_end FROM attendance_records a JOIN employees e ON e.id=a.employee_id LEFT JOIN shifts sh ON sh.id=a.shift_id ORDER BY punched_at DESC LIMIT 20').all();
 const events=db.prepare('SELECT o.*,s.name store_name,e.name employee_name FROM operational_events o JOIN stores s ON s.id=o.store_id LEFT JOIN employees e ON e.id=o.employee_id ORDER BY occurred_at DESC LIMIT 20').all().map(x=>mapJson(x,['payload_json','remediation_json']));
 const feedback=db.prepare('SELECT * FROM feedback_records ORDER BY created_at DESC LIMIT 20').all();
 const runs=db.prepare('SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 10').all().map(run=>({...run,steps:db.prepare('SELECT * FROM agent_steps WHERE run_id=? ORDER BY created_at').all(run.id).map(x=>({...x,result:parseJson(x.result_json,{})}))}));
 const counts={published:db.prepare('SELECT COUNT(*) n FROM schedule_notifications').get().n,read:db.prepare("SELECT COUNT(*) n FROM schedule_notifications WHERE status='read'").get().n,punches:db.prepare('SELECT COUNT(*) n FROM attendance_records').get().n,exceptions:db.prepare('SELECT COUNT(*) n FROM attendance_records WHERE exception_code IS NOT NULL').get().n,openEvents:db.prepare("SELECT COUNT(*) n FROM operational_events WHERE status<>'resolved'").get().n,feedback:db.prepare('SELECT COUNT(*) n FROM feedback_records').get().n};
 return {counts,forecasts,notifications,attendance,events,feedback,runs};
}

export function recordAttendance(db,b){
 const employee=db.prepare('SELECT * FROM employees WHERE id=?').get(b.employeeId);if(!employee)throw err('员工不存在',404);
 const shift=b.shiftId?db.prepare('SELECT * FROM shifts WHERE id=? AND employee_id=?').get(b.shiftId,b.employeeId):null;if(b.shiftId&&!shift)throw err('班次不存在或不属于该员工',404);
 const punchedAt=b.punchedAt||now(),punchType=b.punchType||'clock_in';let exceptionCode=null;
 if(shift&&punchType==='clock_in'&&new Date(punchedAt)-new Date(shift.start_at)>5*60000)exceptionCode='LATE';
 if(shift&&punchType==='clock_out'&&new Date(shift.end_at)-new Date(punchedAt)>5*60000)exceptionCode='EARLY_LEAVE';
 const id=randomUUID();transaction(db,()=>{db.prepare('INSERT INTO attendance_records VALUES(?,?,?,?,?,?,?,?,?)').run(id,b.employeeId,b.shiftId||null,punchType,punchedAt,b.source||'移动打卡',b.verified===false?0:1,exceptionCode,now());if(exceptionCode&&shift)db.prepare('INSERT INTO operational_events VALUES(?,?,?,?,?,?,?,?,?,?,?)').run(randomUUID(),exceptionCode==='LATE'?'late':'early_leave',shift.store_id,b.employeeId,shift.id,punchedAt,'open',j({exceptionCode}),j({action:'等待合规Agent评估是否需要补位'}),now(),null);});return db.prepare('SELECT * FROM attendance_records WHERE id=?').get(id);
}

export function createOperationalEvent(db,b){const store=db.prepare('SELECT * FROM stores WHERE id=?').get(b.storeId);if(!store)throw err('门店不存在',404);const id=randomUUID(),payload=b.payload||{};db.prepare('INSERT INTO operational_events VALUES(?,?,?,?,?,?,?,?,?,?,?)').run(id,b.eventType||'absence',b.storeId,b.employeeId||null,b.shiftId||null,b.occurredAt||now(),'open',j(payload),j({}),now(),null);return remediateEvent(db,id);}

export function remediateEvent(db,id){const event=db.prepare('SELECT * FROM operational_events WHERE id=?').get(id);if(!event)throw err('事件不存在',404);const shift=event.shift_id?db.prepare('SELECT * FROM shifts WHERE id=?').get(event.shift_id):null,payload=parseJson(event.payload_json,{});let remediation;
 if(event.event_type==='traffic_surge'){const extra=Math.max(1,Number(payload.requiredExtra||1));remediation={action:'increase_staffing',summary:`客流超预测，建议增加${extra}名收银人员`,requiredExtra:extra,status:'pending_manager_confirmation'};}
 else if(shift){const candidates=db.prepare('SELECT e.* FROM employees e WHERE e.available=1 AND e.id<>?').all(event.employee_id||'').filter(e=>parseJson(e.skills_json,[]).includes(shift.role)&&!db.prepare("SELECT 1 FROM leave_requests WHERE employee_id=? AND status='approved' AND start_at<? AND end_at>?").get(e.id,shift.end_at,shift.start_at)).sort((a,b)=>a.monthly_hours-b.monthly_hours).slice(0,3);remediation={action:'replace_employee',summary:candidates.length?`已找到${candidates.length}名合规候选人`:'暂无合规候选人，需人工处理',candidates:candidates.map(x=>({employeeId:x.id,name:x.name,monthlyHours:x.monthly_hours,reason:`具备${shift.role}技能，当前月工时${x.monthly_hours}小时`})),status:'pending_manager_confirmation'};}
 else remediation={action:'manual_review',summary:'事件缺少关联班次，请人工确认',status:'needs_information'};
 db.prepare('UPDATE operational_events SET remediation_json=?,status=? WHERE id=?').run(j(remediation),'analyzed',id);return mapJson(db.prepare('SELECT * FROM operational_events WHERE id=?').get(id),['payload_json','remediation_json']);
}

export function acceptRemediation(db,id,b={}){
 const event=db.prepare('SELECT * FROM operational_events WHERE id=?').get(id);if(!event)throw err('事件不存在',404);if(event.status==='resolved')throw err('该事件已处理',409,'EVENT_ALREADY_RESOLVED');
 const remediation=parseJson(event.remediation_json,{});if(event.status!=='analyzed'||remediation.status!=='pending_manager_confirmation')throw err('该事件暂无可采纳的自愈建议',409,'REMEDIATION_NOT_READY');
 const acceptedAt=now(),manager=String(b.manager||'演示主管');let execution;
 transaction(db,()=>{
  if(remediation.action==='replace_employee'){
   const shift=db.prepare('SELECT * FROM shifts WHERE id=?').get(event.shift_id);if(!shift)throw err('关联班次不存在',404);
   const candidateId=b.employeeId||remediation.candidates?.[0]?.employeeId;if(!remediation.candidates?.some(x=>x.employeeId===candidateId))throw err('请选择 Agent 推荐的候选员工',409);
   const validation=validateShift(db,{employeeId:candidateId,storeId:shift.store_id,role:shift.role,startAt:shift.start_at,endAt:shift.end_at},shift.id);if(!validation.passed)throw Object.assign(err('候选员工已不满足最新合规规则，请重新分析',409,'CANDIDATE_STALE'),{details:validation});
   const before={...shift};db.prepare('UPDATE shifts SET employee_id=?,source=?,status=?,updated_at=? WHERE id=?').run(candidateId,'self_healing','published',acceptedAt,shift.id);
   db.prepare('INSERT INTO shift_adjustments VALUES(?,?,?,?,?,?,?,?,?,?)').run(randomUUID(),shift.id,'self_healing_replace',j(before),j({...before,employee_id:candidateId,source:'self_healing'}),j([]),j(validation.softConflicts),`采纳事件 ${id} 的 Agent 自愈建议`,manager,acceptedAt);
   db.prepare('INSERT INTO schedule_notifications VALUES(?,?,?,?,?,?,?,?)').run(randomUUID(),shift.id,candidateId,'应用内','sent',`${shift.start_at.slice(0,10)} ${shift.start_at.slice(11,16)}-${shift.end_at.slice(11,16)} ${shift.role}替补班次已下发`,acceptedAt,null);
   execution={action:'shift_reassigned',shiftId:shift.id,employeeId:candidateId};
  }else if(remediation.action==='increase_staffing'){
   const payload=parseJson(event.payload_json,{}),start=new Date(event.occurred_at),end=new Date(start.getTime()+4*3600000),shortageId=randomUUID();
   db.prepare('INSERT INTO shortage_events VALUES(?,?,?,?,?,?,?,?)').run(shortageId,event.store_id,'收银',start.toISOString(),end.toISOString(),remediation.requiredExtra||1,'open',acceptedAt);
   execution={action:'shortage_created',shortageId,headcount:remediation.requiredExtra||1};
  }else throw err('当前建议需要人工线下处理，不能自动执行',409);
  const resolved={...remediation,status:'accepted',acceptedBy:manager,acceptedAt,execution};db.prepare("UPDATE operational_events SET remediation_json=?,status='resolved',resolved_at=? WHERE id=?").run(j(resolved),acceptedAt,id);
 });
 return mapJson(db.prepare('SELECT * FROM operational_events WHERE id=?').get(id),['payload_json','remediation_json']);
}

export function runFeedbackReview(db){const forecasts=db.prepare('SELECT * FROM demand_forecasts WHERE actual_value IS NOT NULL').all();if(!forecasts.length)throw err('暂无可复盘的实际业务数据',409);const created=[];for(const f of forecasts){const deviation=Number((((f.actual_value-f.predicted_value)/f.predicted_value)*100).toFixed(1)),existing=db.prepare('SELECT * FROM feedback_records WHERE metric_key=? AND evidence LIKE ?').get(f.signal_type,`%${f.forecast_date}%`);if(existing){created.push(existing);continue}const id=randomUUID(),direction=deviation>0?'上调':'下调',action=`下一周期同类日预测系数${direction}${Math.min(10,Math.abs(deviation/2)).toFixed(1)}%`;db.prepare('INSERT INTO feedback_records VALUES(?,?,?,?,?,?,?,?,?)').run(id,f.signal_type,f.predicted_value,f.actual_value,deviation,`${f.forecast_date}实际${f.actual_value}，预测${f.predicted_value}`,action,'ready',now());created.push(db.prepare('SELECT * FROM feedback_records WHERE id=?').get(id));}return created;}
