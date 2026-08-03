import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID, scryptSync, randomBytes } from 'node:crypto';

export function hashPassword(password, salt = randomBytes(16).toString('hex')) {
  return `${salt}:${scryptSync(password, salt, 64).toString('hex')}`;
}

export function createDatabase(filename = ':memory:') {
  if (filename !== ':memory:') mkdirSync(dirname(filename), { recursive: true });
  const db = new DatabaseSync(filename);
  db.exec('PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 5000;');
  migrate(db);
  seed(db);
  return db;
}

function migrate(db) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS tenants (
      id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, timezone TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stores (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
      region TEXT NOT NULL, latitude REAL, longitude REAL,
      UNIQUE(tenant_id, code), FOREIGN KEY(tenant_id) REFERENCES tenants(id)
    );
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, username TEXT NOT NULL, password_hash TEXT NOT NULL,
      display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('manager','employee','admin')),
      employee_id TEXT, active INTEGER NOT NULL DEFAULT 1,
      UNIQUE(tenant_id, username), FOREIGN KEY(tenant_id) REFERENCES tenants(id)
    );
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL,
      created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS employees (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_no TEXT NOT NULL, name TEXT NOT NULL,
      store_id TEXT NOT NULL, department TEXT NOT NULL, position TEXT NOT NULL, employment_type TEXT NOT NULL,
      hourly_rate REAL NOT NULL, skills_json TEXT NOT NULL, available_for_support INTEGER NOT NULL DEFAULT 1,
      status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
      UNIQUE(tenant_id, employee_no), FOREIGN KEY(tenant_id) REFERENCES tenants(id), FOREIGN KEY(store_id) REFERENCES stores(id)
    );
    CREATE TABLE IF NOT EXISTS leave_balances (
      id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, leave_type TEXT NOT NULL, balance_hours REAL NOT NULL,
      version INTEGER NOT NULL DEFAULT 1, UNIQUE(employee_id, leave_type), FOREIGN KEY(employee_id) REFERENCES employees(id)
    );
    CREATE TABLE IF NOT EXISTS labor_accounts (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
      account_type TEXT NOT NULL, budget REAL NOT NULL, spent REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
      UNIQUE(tenant_id, code), FOREIGN KEY(tenant_id) REFERENCES tenants(id)
    );
    CREATE TABLE IF NOT EXISTS events (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL, labor_account_id TEXT NOT NULL,
      name TEXT NOT NULL, event_date TEXT NOT NULL, forecast_traffic INTEGER NOT NULL, actual_traffic INTEGER,
      forecast_sales REAL NOT NULL, actual_sales REAL, required_headcount INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'planning', created_at TEXT NOT NULL,
      FOREIGN KEY(tenant_id) REFERENCES tenants(id), FOREIGN KEY(store_id) REFERENCES stores(id),
      FOREIGN KEY(labor_account_id) REFERENCES labor_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS shifts (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL, store_id TEXT NOT NULL,
      event_id TEXT, shift_date TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
      role_required TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', status TEXT NOT NULL DEFAULT 'planned',
      labor_account_id TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(employee_id) REFERENCES employees(id), FOREIGN KEY(store_id) REFERENCES stores(id),
      FOREIGN KEY(event_id) REFERENCES events(id), FOREIGN KEY(labor_account_id) REFERENCES labor_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS employee_requests (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL,
      request_type TEXT NOT NULL CHECK(request_type IN ('leave','attendance_correction','overtime')),
      request_date TEXT NOT NULL, start_at TEXT, end_at TEXT, hours REAL NOT NULL,
      reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', approver_user_id TEXT,
      decision_note TEXT, created_at TEXT NOT NULL, decided_at TEXT,
      FOREIGN KEY(employee_id) REFERENCES employees(id), FOREIGN KEY(approver_user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS attendance_events (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL, shift_id TEXT,
      event_type TEXT NOT NULL CHECK(event_type IN ('clock_in','clock_out','correction')),
      occurred_at TEXT NOT NULL, source TEXT NOT NULL, request_id TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(employee_id) REFERENCES employees(id), FOREIGN KEY(shift_id) REFERENCES shifts(id),
      FOREIGN KEY(request_id) REFERENCES employee_requests(id)
    );
    CREATE TABLE IF NOT EXISTS compliance_checks (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
      rule_code TEXT NOT NULL, rule_name TEXT NOT NULL, passed INTEGER NOT NULL, evidence TEXT NOT NULL,
      checked_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workforce_plans (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, prompt TEXT NOT NULL,
      intent_json TEXT NOT NULL, option_json TEXT NOT NULL, alternatives_json TEXT, state_trace_json TEXT,
      modules_json TEXT, status TEXT NOT NULL DEFAULT 'proposed',
      created_by TEXT NOT NULL, confirmed_by TEXT, created_at TEXT NOT NULL, confirmed_at TEXT,
      FOREIGN KEY(event_id) REFERENCES events(id), FOREIGN KEY(created_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS time_results (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL, shift_id TEXT NOT NULL,
      work_date TEXT NOT NULL, regular_hours REAL NOT NULL, overtime_hours REAL NOT NULL,
      leave_hours REAL NOT NULL, status TEXT NOT NULL, calculated_at TEXT NOT NULL,
      UNIQUE(shift_id), FOREIGN KEY(employee_id) REFERENCES employees(id), FOREIGN KEY(shift_id) REFERENCES shifts(id)
    );
    CREATE TABLE IF NOT EXISTS time_bank_entries (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL, entry_type TEXT NOT NULL,
      hours REAL NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
      FOREIGN KEY(employee_id) REFERENCES employees(id)
    );
    CREATE TABLE IF NOT EXISTS labor_allocations (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, time_result_id TEXT NOT NULL, labor_account_id TEXT NOT NULL,
      hours REAL NOT NULL, cost REAL NOT NULL, allocation_ratio REAL NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(time_result_id) REFERENCES time_results(id), FOREIGN KEY(labor_account_id) REFERENCES labor_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS payroll_estimates (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL, event_id TEXT NOT NULL,
      regular_pay REAL NOT NULL, overtime_pay REAL NOT NULL, support_allowance REAL NOT NULL,
      total_pay REAL NOT NULL, calculated_at TEXT NOT NULL,
      FOREIGN KEY(employee_id) REFERENCES employees(id), FOREIGN KEY(event_id) REFERENCES events(id)
    );
    CREATE TABLE IF NOT EXISTS feedback_metrics (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, metric_type TEXT NOT NULL,
      metric_key TEXT NOT NULL, before_value REAL NOT NULL, after_value REAL NOT NULL,
      evidence TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(event_id) REFERENCES events(id)
    );
    CREATE TABLE IF NOT EXISTS audit_logs (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT, action TEXT NOT NULL,
      entity_type TEXT NOT NULL, entity_id TEXT, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_shifts_event ON shifts(event_id, shift_date);
    CREATE INDEX IF NOT EXISTS idx_requests_status ON employee_requests(tenant_id, status);
    CREATE INDEX IF NOT EXISTS idx_attendance_employee ON attendance_events(employee_id, occurred_at);
    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(tenant_id, created_at DESC);
  `);
  const planColumns = new Set(db.prepare('PRAGMA table_info(workforce_plans)').all().map(column => column.name));
  if (!planColumns.has('alternatives_json')) db.exec('ALTER TABLE workforce_plans ADD COLUMN alternatives_json TEXT');
  if (!planColumns.has('state_trace_json')) db.exec('ALTER TABLE workforce_plans ADD COLUMN state_trace_json TEXT');
  if (!planColumns.has('modules_json')) db.exec('ALTER TABLE workforce_plans ADD COLUMN modules_json TEXT');
}

function seed(db) {
  const exists = db.prepare('SELECT COUNT(*) AS count FROM tenants').get().count;
  if (exists) return;
  const now = new Date().toISOString();
  const tenantId = 'tenant-demo';
  const stores = [
    ['store-flagship', 'S001', '旗舰店', '市中心', 31.2304, 121.4737],
    ['store-north', 'S002', '北区店', '北区', 31.2700, 121.4800],
    ['store-west', 'S003', '西区店', '西区', 31.2200, 121.4000]
  ];
  db.exec('BEGIN');
  try {
    db.prepare('INSERT INTO tenants(id,code,name,timezone) VALUES(?,?,?,?)').run(tenantId, 'DEMO', '星桥零售', 'Asia/Shanghai');
    const insertStore = db.prepare('INSERT INTO stores(id,tenant_id,code,name,region,latitude,longitude) VALUES(?,?,?,?,?,?,?)');
    stores.forEach(store => insertStore.run(store[0], tenantId, ...store.slice(1)));
    const insertEmployee = db.prepare(`INSERT INTO employees
      (id,tenant_id,employee_no,name,store_id,department,position,employment_type,hourly_rate,skills_json,available_for_support,status,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`);
    const employeeRows = [
      ['emp-linxiao','E001','林晓','store-flagship','零售运营','资深导购','full_time',120,['导购','会员服务'],0],
      ['emp-zhangmin','E002','张敏','store-flagship','零售运营','收银员','full_time',110,['收银'],0],
      ['emp-chenchen','E003','陈晨','store-north','零售运营','资深导购','full_time',118,['导购','闭店盘点'],1],
      ['emp-wanglei','E004','王蕾','store-west','零售运营','收银员','part_time',105,['收银','导购'],1],
      ['emp-lijun','E005','李军','store-flagship','零售运营','库存专员','full_time',115,['库存','闭店盘点'],0],
      ['emp-zhaoyu','E006','赵雨','store-north','零售运营','导购','part_time',100,['导购'],1],
      ['emp-sunyue','E007','孙悦','store-west','零售运营','导购','full_time',108,['导购','会员服务'],1],
      ['emp-heping','E008','何平','store-flagship','门店管理','店长','full_time',180,['管理','收银'],0]
    ];
    employeeRows.forEach(row => insertEmployee.run(row[0], tenantId, row[1], row[2], row[3], row[4], row[5], row[6], row[7], JSON.stringify(row[8]), row[9], 'active', now));
    const insertBalance = db.prepare('INSERT INTO leave_balances(id,employee_id,leave_type,balance_hours) VALUES(?,?,?,?)');
    employeeRows.forEach(row => insertBalance.run(randomUUID(), row[0], 'annual', row[0] === 'emp-linxiao' ? 40 : 32));
    db.prepare('INSERT INTO users(id,tenant_id,username,password_hash,display_name,role,employee_id) VALUES(?,?,?,?,?,?,?)')
      .run('user-manager', tenantId, 'manager', hashPassword('Demo@2026'), '周岚', 'manager', null);
    db.prepare('INSERT INTO users(id,tenant_id,username,password_hash,display_name,role,employee_id) VALUES(?,?,?,?,?,?,?)')
      .run('user-employee', tenantId, 'employee', hashPassword('Demo@2026'), '林晓', 'employee', 'emp-linxiao');
    db.prepare('INSERT INTO labor_accounts(id,tenant_id,code,name,account_type,budget,spent) VALUES(?,?,?,?,?,?,?)')
      .run('account-member-day', tenantId, 'ACT-MEMBER-DAY', '会员日促销账户', 'event', 2000, 0);
    db.prepare('INSERT INTO labor_accounts(id,tenant_id,code,name,account_type,budget,spent) VALUES(?,?,?,?,?,?,?)')
      .run('account-daily', tenantId, 'STORE-DAILY', '旗舰店日常账户', 'store', 20000, 0);
    db.prepare(`INSERT INTO events(id,tenant_id,store_id,labor_account_id,name,event_date,forecast_traffic,forecast_sales,required_headcount,status,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?)`).run('event-member-day', tenantId, 'store-flagship', 'account-member-day', '8月会员日', '2026-08-08', 1350, 168000, 8, 'planning', now);
    const insertShift = db.prepare(`INSERT INTO shifts(id,tenant_id,employee_id,store_id,event_id,shift_date,start_at,end_at,role_required,source,status,labor_account_id,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`);
    [
      ['shift-linxiao','emp-linxiao','导购','09:00','18:00'],
      ['shift-zhangmin','emp-zhangmin','收银','10:00','19:00'],
      ['shift-lijun','emp-lijun','库存','12:00','21:00'],
      ['shift-heping','emp-heping','管理','09:00','18:00']
    ].forEach(row => insertShift.run(row[0], tenantId, row[1], 'store-flagship', 'event-member-day', '2026-08-08', row[3], row[4], row[2], 'seed', 'planned', 'account-member-day', now));
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

export function withTransaction(db, operation) {
  db.exec('BEGIN IMMEDIATE');
  try {
    const result = operation();
    db.exec('COMMIT');
    return result;
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}
