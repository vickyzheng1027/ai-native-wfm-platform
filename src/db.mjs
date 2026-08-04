import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';

const now = () => new Date().toISOString();
const j = value => JSON.stringify(value);
export const parseJson = (value, fallback = null) => { try { return JSON.parse(value); } catch { return fallback; } };

export function createDatabase(path = ':memory:') {
  if (path !== ':memory:') mkdirSync(dirname(path), { recursive:true });
  const db = new DatabaseSync(path);
  db.exec('PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;');
  migrate(db);
  if (!db.prepare('SELECT COUNT(*) n FROM stores').get().n) resetDatabase(db);
  return db;
}

function migrate(db) {
  const schemaVersion=3;
  const current=db.prepare('PRAGMA user_version').get().user_version;
  if(current!==schemaVersion){
    db.exec('PRAGMA foreign_keys=OFF');
    for(const table of ['shift_adjustments','rule_validation_logs','shifts','schedule_plan_shifts','schedule_plans','business_demands','calendar_days','leave_requests','employee_availability','agent_steps','agent_runs','cost_allocations','compliance_checks','transfer_orders','transfer_suggestions','shortage_events','rule_optimization_suggestions','rule_draft_items','rule_drafts','rule_versions','rules','employees','stores','sessions','tenants','users','labor_accounts','events','demand_history','compliance_rules','requests','attendance_punches','ai_plans','audit_logs']) db.exec(`DROP TABLE IF EXISTS ${table}`);
    db.exec(`PRAGMA user_version=${schemaVersion}; PRAGMA foreign_keys=ON;`);
  }
  db.exec(`
    CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, city TEXT);
    CREATE TABLE IF NOT EXISTS employees(id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, store_id TEXT, skills_json TEXT, cross_store INTEGER, monthly_hours REAL, consecutive_days INTEGER, hourly_rate REAL, travel_cost REAL, available INTEGER, contract_hours REAL, preference_json TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS employee_availability(id TEXT PRIMARY KEY, employee_id TEXT, weekday INTEGER, start_time TEXT, end_time TEXT, available INTEGER);
    CREATE TABLE IF NOT EXISTS leave_requests(id TEXT PRIMARY KEY, employee_id TEXT, leave_type TEXT, start_at TEXT, end_at TEXT, reason TEXT, status TEXT, requested_at TEXT, decided_at TEXT, decision_note TEXT);
    CREATE TABLE IF NOT EXISTS calendar_days(date TEXT PRIMARY KEY, day_type TEXT, name TEXT, is_workday INTEGER, note TEXT);
    CREATE TABLE IF NOT EXISTS business_demands(id TEXT PRIMARY KEY, store_id TEXT, demand_date TEXT, start_time TEXT, end_time TEXT, role TEXT, required_count INTEGER, forecast_traffic INTEGER, forecast_sales REAL, source TEXT, status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS schedule_plans(id TEXT PRIMARY KEY, plan_type TEXT, store_id TEXT, week_start TEXT, status TEXT, metrics_json TEXT, rule_snapshot_json TEXT, data_signature TEXT, explanation TEXT, created_at TEXT, confirmed_at TEXT);
    CREATE TABLE IF NOT EXISTS schedule_plan_shifts(id TEXT PRIMARY KEY, plan_id TEXT, demand_id TEXT, employee_id TEXT, store_id TEXT, role TEXT, start_at TEXT, end_at TEXT, score REAL, reasons_json TEXT);
    CREATE TABLE IF NOT EXISTS shifts(id TEXT PRIMARY KEY, employee_id TEXT, store_id TEXT, role TEXT, start_at TEXT, end_at TEXT, source TEXT, status TEXT, plan_id TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS shift_adjustments(id TEXT PRIMARY KEY, shift_id TEXT, action TEXT, before_json TEXT, after_json TEXT, hard_conflicts_json TEXT, soft_conflicts_json TEXT, override_reason TEXT, operator TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS rule_validation_logs(id TEXT PRIMARY KEY, object_type TEXT, object_id TEXT, passed INTEGER, hard_conflicts_json TEXT, soft_conflicts_json TEXT, rule_versions_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, category TEXT, value_json TEXT, unit TEXT, severity TEXT, version INTEGER, status TEXT, source_text TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS rule_versions(id TEXT PRIMARY KEY, rule_id TEXT, version INTEGER, value_json TEXT, source TEXT, reason TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS rule_drafts(id TEXT PRIMARY KEY, input_text TEXT, status TEXT, model_source TEXT, confidence REAL, unresolved_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS rule_draft_items(id TEXT PRIMARY KEY, draft_id TEXT, rule_id TEXT, code TEXT, name TEXT, category TEXT, value_json TEXT, unit TEXT, severity TEXT, confidence REAL, status TEXT);
    CREATE TABLE IF NOT EXISTS shortage_events(id TEXT PRIMARY KEY, store_id TEXT, role TEXT, start_at TEXT, end_at TEXT, headcount INTEGER, status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS transfer_suggestions(id TEXT PRIMARY KEY, shortage_id TEXT, employee_id TEXT, rank_no INTEGER, score REAL, risk TEXT, reason TEXT, status TEXT, rule_snapshot_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS transfer_orders(id TEXT PRIMARY KEY, shortage_id TEXT, suggestion_id TEXT, employee_id TEXT, status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS compliance_checks(id TEXT PRIMARY KEY, transfer_id TEXT, passed INTEGER, details_json TEXT, rule_versions_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS cost_allocations(id TEXT PRIMARY KEY, transfer_id TEXT, hours REAL, hourly_rate REAL, labor_cost REAL, travel_cost REAL, total_cost REAL, from_account TEXT, to_account TEXT, formula TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS rule_optimization_suggestions(id TEXT PRIMARY KEY, rule_id TEXT, metric_json TEXT, current_value_json TEXT, proposed_value_json TEXT, reason TEXT, status TEXT, created_at TEXT, decided_at TEXT);
    CREATE TABLE IF NOT EXISTS agent_runs(id TEXT PRIMARY KEY, agent_type TEXT, input_text TEXT, status TEXT, model_source TEXT, summary TEXT, created_at TEXT, completed_at TEXT);
    CREATE TABLE IF NOT EXISTS agent_steps(id TEXT PRIMARY KEY, run_id TEXT, step_name TEXT, business_summary TEXT, result_json TEXT, created_at TEXT);
  `);
}

export function resetDatabase(db) {
  db.exec('BEGIN IMMEDIATE');
  try {
    for (const table of ['shift_adjustments','rule_validation_logs','shifts','schedule_plan_shifts','schedule_plans','business_demands','calendar_days','leave_requests','employee_availability','agent_steps','agent_runs','cost_allocations','compliance_checks','transfer_orders','transfer_suggestions','shortage_events','rule_optimization_suggestions','rule_draft_items','rule_drafts','rule_versions','rules','employees','stores']) db.exec(`DELETE FROM ${table}`);
    const store = db.prepare('INSERT INTO stores VALUES(?,?,?,?)');
    [['store-a','A','上海静安店','上海'],['store-b','B','上海徐汇店','上海'],['store-c','C','上海浦东店','上海']].forEach(x=>store.run(...x));
    const employee = db.prepare('INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)');
    [
      ['e01','E001','陈晨','store-b',['收银','理货'],1,132,3,32,20,1],
      ['e02','E002','李敏','store-c',['收银'],1,164,5,35,35,1],
      ['e03','E003','王磊','store-b',['收银'],1,169,2,30,20,1],
      ['e04','E004','周颖','store-c',['导购'],1,120,4,31,35,1],
      ['e05','E005','赵强','store-b',['收银'],0,118,2,29,20,1],
      ['e06','E006','孙悦','store-c',['收银'],1,176,6,34,35,1],
      ['e07','E007','何欣','store-a',['收银'],1,150,2,33,0,1],
      ['e08','E008','高远','store-b',['理货'],1,142,1,30,20,1],
      ['e09','E009','林静','store-c',['收银'],1,105,1,36,35,0]
    ].forEach(x=>employee.run(x[0],x[1],x[2],x[3],j(x[4]),...x.slice(5),160,j({preferredShift:x[0]==='e02'?'morning':'any'}),'active'));
    const availability=db.prepare('INSERT INTO employee_availability VALUES(?,?,?,?,?,?)');
    for(const e of ['e01','e02','e03','e04','e05','e06','e07','e08','e09']) for(let day=1;day<=6;day++) availability.run(randomUUID(),e,day,'08:00','22:00',1);
    db.prepare('INSERT INTO leave_requests VALUES(?,?,?,?,?,?,?,?,?,?)').run('leave-approved','e02','年假','2026-08-05T00:00:00.000Z','2026-08-06T00:00:00.000Z','家庭事务','approved',now(),now(),'已核对余额');
    db.prepare('INSERT INTO leave_requests VALUES(?,?,?,?,?,?,?,?,?,?)').run('leave-pending','e04','事假','2026-08-07T09:00:00.000Z','2026-08-07T18:00:00.000Z','个人事务','pending',now(),null,null);
    const cal=db.prepare('INSERT INTO calendar_days VALUES(?,?,?,?,?)');
    for(let month=0;month<12;month++){const days=new Date(Date.UTC(2026,month+1,0)).getUTCDate();for(let day=1;day<=days;day++){const d=new Date(Date.UTC(2026,month,day)),iso=d.toISOString().slice(0,10),week=d.getUTCDay();cal.run(iso,week===0||week===6?'weekend':'workday',week===0||week===6?'周末':'工作日',week!==0&&week!==6?1:0,'');}}
    [['2026-01-01','holiday','元旦',0,'法定节假日'],['2026-02-17','holiday','春节',0,'法定节假日'],['2026-04-05','holiday','清明节',0,'法定节假日'],['2026-05-01','holiday','劳动节',0,'法定节假日'],['2026-06-19','holiday','端午节',0,'法定节假日'],['2026-09-25','holiday','中秋节',0,'法定节假日'],['2026-10-01','holiday','国庆节',0,'法定节假日'],['2026-08-08','makeup_workday','调休工作日',1,'周六调休上班']].forEach(x=>db.prepare('INSERT OR REPLACE INTO calendar_days VALUES(?,?,?,?,?)').run(...x));
    const demand=db.prepare('INSERT INTO business_demands VALUES(?,?,?,?,?,?,?,?,?,?,?,?)');
    [['d1','2026-08-03','09:00','17:00','收银',2,620,78000],['d2','2026-08-04','09:00','17:00','收银',2,680,82000],['d3','2026-08-05','09:00','17:00','收银',2,720,90000],['d4','2026-08-06','10:00','18:00','导购',2,760,96000],['d5','2026-08-07','09:00','17:00','收银',2,800,105000],['d6','2026-08-08','09:00','17:00','收银',3,980,130000]].forEach(x=>demand.run(x[0],'store-a',...x.slice(1),'demo_seed','confirmed',now()));
    const rules = [
      ['r-hours','MONTHLY_HOURS','月工时上限','合规',180,'小时','block','每人每月工时不得超过180小时'],
      ['r-days','CONSECUTIVE_DAYS','连续工作天数上限','合规',6,'天','block','连续工作不得超过6天'],
      ['r-skill','SKILL_REQUIRED','岗位技能匹配','排班',true,'','block','员工必须具备目标岗位技能'],
      ['r-cross','CROSS_STORE_REQUIRED','跨店资格','排班',true,'','block','跨店补位必须具备跨店资格'],
      ['r-distance','TRAVEL_COST_LIMIT','单次交通成本上限','成本',50,'元','warn','单次调剂交通成本不超过50元']
    ];
    const insertRule=db.prepare('INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?)');
    const insertVersion=db.prepare('INSERT INTO rule_versions VALUES(?,?,?,?,?,?,?)');
    for (const r of rules) { insertRule.run(r[0],r[1],r[2],r[3],j(r[4]),r[5],r[6],1,'active',r[7],now()); insertVersion.run(randomUUID(),r[0],1,j(r[4]),'demo_seed','比赛演示初始规则',now()); }
    db.exec('COMMIT');
    return { resetAt:now(), stores:3, employees:9, rules:5 };
  } catch (e) { db.exec('ROLLBACK'); throw e; }
}

export function transaction(db, fn) { db.exec('BEGIN IMMEDIATE'); try { const value=fn(); db.exec('COMMIT'); return value; } catch(e){ db.exec('ROLLBACK'); throw e; } }
