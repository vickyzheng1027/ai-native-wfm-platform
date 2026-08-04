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
  const schemaVersion=2;
  const current=db.prepare('PRAGMA user_version').get().user_version;
  if(current!==schemaVersion){
    db.exec('PRAGMA foreign_keys=OFF');
    for(const table of ['agent_steps','agent_runs','cost_allocations','compliance_checks','transfer_orders','transfer_suggestions','shortage_events','rule_optimization_suggestions','rule_draft_items','rule_drafts','rule_versions','rules','employees','stores','sessions','tenants','users','labor_accounts','events','demand_history','compliance_rules','shifts','requests','attendance_punches','ai_plans','audit_logs']) db.exec(`DROP TABLE IF EXISTS ${table}`);
    db.exec(`PRAGMA user_version=${schemaVersion}; PRAGMA foreign_keys=ON;`);
  }
  db.exec(`
    CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, city TEXT);
    CREATE TABLE IF NOT EXISTS employees(id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, store_id TEXT, skills_json TEXT, cross_store INTEGER, monthly_hours REAL, consecutive_days INTEGER, hourly_rate REAL, travel_cost REAL, available INTEGER);
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
    for (const table of ['agent_steps','agent_runs','cost_allocations','compliance_checks','transfer_orders','transfer_suggestions','shortage_events','rule_optimization_suggestions','rule_draft_items','rule_drafts','rule_versions','rules','employees','stores']) db.exec(`DELETE FROM ${table}`);
    const store = db.prepare('INSERT INTO stores VALUES(?,?,?,?)');
    [['store-a','A','上海静安店','上海'],['store-b','B','上海徐汇店','上海'],['store-c','C','上海浦东店','上海']].forEach(x=>store.run(...x));
    const employee = db.prepare('INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?)');
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
    ].forEach(x=>employee.run(x[0],x[1],x[2],x[3],j(x[4]),...x.slice(5)));
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
