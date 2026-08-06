import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 4


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value, fallback=None):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 180_000).hex()
    return f"pbkdf2_sha256$180000${salt}${digest}"


def verify_password(password, encoded):
    try:
        _, rounds, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def connect(path=None):
    target = path or os.getenv("DATABASE_PATH", str(Path(__file__).parents[1] / "data" / "flowstaff.db"))
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, check_same_thread=False, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    migrate(db)
    return db


@contextmanager
def transaction(db):
    try:
        db.execute("BEGIN IMMEDIATE")
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY,code TEXT UNIQUE,name TEXT,city TEXT);
    CREATE TABLE IF NOT EXISTS departments(id TEXT PRIMARY KEY,code TEXT UNIQUE,name TEXT UNIQUE,status TEXT);
    CREATE TABLE IF NOT EXISTS job_positions(id TEXT PRIMARY KEY,code TEXT UNIQUE,name TEXT UNIQUE,department TEXT,status TEXT);
    CREATE TABLE IF NOT EXISTS skill_catalog(id TEXT PRIMARY KEY,code TEXT UNIQUE,name TEXT UNIQUE,category TEXT,status TEXT);
    CREATE TABLE IF NOT EXISTS shift_templates(id TEXT PRIMARY KEY,code TEXT UNIQUE,name TEXT UNIQUE,start_time TEXT,end_time TEXT,paid_hours REAL,shift_type TEXT,status TEXT,description TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS employees(id TEXT PRIMARY KEY,code TEXT UNIQUE,name TEXT,role TEXT,department TEXT,store_id TEXT,employment_type TEXT,status TEXT,hire_date TEXT,manager_id TEXT,phone TEXT,email TEXT,hourly_rate REAL,weekly_hour_limit REAL,night_shift_limit INTEGER,preferences_json TEXT,FOREIGN KEY(store_id) REFERENCES stores(id));
    CREATE TABLE IF NOT EXISTS employee_skills(id TEXT PRIMARY KEY,employee_id TEXT,skill TEXT,proficiency INTEGER,target_level INTEGER,certified INTEGER,certified_at TEXT,expires_at TEXT,evidence TEXT,FOREIGN KEY(employee_id) REFERENCES employees(id));
    CREATE TABLE IF NOT EXISTS employee_preferences(id TEXT PRIMARY KEY,employee_id TEXT,raw_text TEXT,preference_type TEXT,value_json TEXT,confidence REAL,effective_from TEXT,effective_to TEXT,status TEXT,created_at TEXT,FOREIGN KEY(employee_id) REFERENCES employees(id));
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT,display_name TEXT,role TEXT,employee_id TEXT,store_id TEXT,status TEXT,failed_attempts INTEGER DEFAULT 0,locked_until TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT,token_hash TEXT UNIQUE,expires_at TEXT,revoked_at TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS attendance(id TEXT PRIMARY KEY,employee_id TEXT,event_date TEXT,event_type TEXT,event_time TEXT,hours REAL,source TEXT,metadata_json TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS leave_balances(id TEXT PRIMARY KEY,employee_id TEXT,year INTEGER,leave_type TEXT,entitled REAL,used REAL,pending REAL);
    CREATE TABLE IF NOT EXISTS employee_requests(id TEXT PRIMARY KEY,employee_id TEXT,request_type TEXT,shift_id TEXT,reason TEXT,payload_json TEXT,agent_analysis_json TEXT,status TEXT,peer_employee_id TEXT,created_at TEXT,decided_at TEXT);
    CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,name TEXT,description TEXT,scope TEXT,strength TEXT,domain TEXT,definition_json TEXT,status TEXT,version INTEGER,source TEXT,confidence REAL,created_by TEXT,approved_by TEXT,created_at TEXT,updated_at TEXT,store_id TEXT,effective_from TEXT,effective_to TEXT);
    CREATE TABLE IF NOT EXISTS rule_versions(id TEXT PRIMARY KEY,rule_id TEXT,version INTEGER,snapshot_json TEXT,changed_by TEXT,changed_at TEXT);
    CREATE TABLE IF NOT EXISTS business_demands(id TEXT PRIMARY KEY,store_id TEXT,demand_date TEXT,start_time TEXT,end_time TEXT,role TEXT,required_count INTEGER,confidence REAL,factors_json TEXT,source TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,user_id TEXT,context TEXT,input_text TEXT,intent TEXT,status TEXT,progress INTEGER,parameters_json TEXT,rag_citations_json TEXT,trigger_event_id TEXT,approval_required INTEGER,error TEXT,created_at TEXT,completed_at TEXT);
    CREATE TABLE IF NOT EXISTS task_steps(id TEXT PRIMARY KEY,task_id TEXT,stage INTEGER,name TEXT,status TEXT,business_message TEXT,technical_message TEXT,metrics_json TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS schedule_plans(id TEXT PRIMARY KEY,task_id TEXT,name TEXT,strategy TEXT,status TEXT,recommended INTEGER,metrics_json TEXT,explanation_json TEXT,solver TEXT,created_at TEXT,activated_at TEXT,published_at TEXT);
    CREATE TABLE IF NOT EXISTS shifts(id TEXT PRIMARY KEY,plan_id TEXT,employee_id TEXT,store_id TEXT,role TEXT,start_at TEXT,end_at TEXT,status TEXT,source TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS employee_notifications(id TEXT PRIMARY KEY,employee_id TEXT,notification_type TEXT,title TEXT,content TEXT,resource_id TEXT,status TEXT,created_at TEXT,read_at TEXT);
    CREATE TABLE IF NOT EXISTS anomaly_events(id TEXT PRIMARY KEY,employee_id TEXT,store_id TEXT,anomaly_type TEXT,risk_level TEXT,confidence REAL,evidence_json TEXT,impact TEXT,possible_causes_json TEXT,suggestions_json TEXT,status TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS anomaly_actions(id TEXT PRIMARY KEY,anomaly_id TEXT,from_status TEXT,to_status TEXT,note TEXT,actor_id TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS vector_documents(id TEXT PRIMARY KEY,source_type TEXT,source_id TEXT,title TEXT,content TEXT,embedding_json TEXT,embedding_model TEXT,content_hash TEXT,metadata_json TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS automation_events(id TEXT PRIMARY KEY,event_type TEXT,dedupe_key TEXT UNIQUE,store_id TEXT,employee_id TEXT,payload_json TEXT,priority INTEGER,status TEXT,attempts INTEGER,result_json TEXT,error TEXT,task_id TEXT,created_at TEXT,processed_at TEXT);
    CREATE TABLE IF NOT EXISTS ai_invocations(id TEXT PRIMARY KEY,user_id TEXT,purpose TEXT,provider TEXT,model TEXT,status TEXT,duration_ms INTEGER,input_tokens INTEGER,output_tokens INTEGER,error_summary TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS audit_logs(id TEXT PRIMARY KEY,occurred_at TEXT,user_id TEXT,action TEXT,resource_type TEXT,resource_id TEXT,result TEXT,ip TEXT,request_id TEXT,details_json TEXT);
    CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY,path TEXT,size_bytes INTEGER,checksum TEXT,status TEXT,created_by TEXT,created_at TEXT);
    """)
    rule_columns={column["name"] for column in db.execute("PRAGMA table_info(rules)")}
    if "store_id" not in rule_columns:db.execute("ALTER TABLE rules ADD COLUMN store_id TEXT")
    if "effective_from" not in rule_columns:db.execute("ALTER TABLE rules ADD COLUMN effective_from TEXT")
    if "effective_to" not in rule_columns:db.execute("ALTER TABLE rules ADD COLUMN effective_to TEXT")
    db.execute("UPDATE rules SET store_id='store-a' WHERE scope='store' AND store_id IS NULL")
    business_date=datetime.now(ZoneInfo(os.getenv("WFM_TIMEZONE","Asia/Shanghai"))).date().isoformat()
    db.execute("UPDATE rules SET effective_from=? WHERE scope='store' AND effective_from IS NULL",(business_date,))
    current = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not current:
        db.execute("INSERT INTO meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    if not db.execute("SELECT 1 FROM stores LIMIT 1").fetchone():
        seed(db)
    seed_organization_expansion(db)
    seed_demo_operations(db)
    db.execute("UPDATE meta SET value=? WHERE key='schema_version'",(str(SCHEMA_VERSION),))
    db.commit()


def seed_organization_expansion(db):
    now=utcnow()
    stores=[("store-d","SH-D04","上海虹桥天地店","上海"),("store-e","SH-E05","上海五角场店","上海"),("store-f","SH-F06","上海中山公园店","上海"),("store-g","SH-G07","上海前滩太古里店","上海"),("store-h","SH-H08","上海大宁国际店","上海")]
    departments=[("dept-ops","OPS","门店运营","active"),("dept-sales","SALES","销售服务","active"),("dept-front","FRONT","前台服务","active"),("dept-warehouse","WAREHOUSE","仓储运营","active"),("dept-digital","DIGITAL","数字运营","active")]
    positions=[("pos-service","SERVICE","客服专员","前台服务","active"),("pos-stock","STOCK","库存专员","仓储运营","active"),("pos-display","DISPLAY","陈列专员","销售服务","active"),("pos-supervisor","SUPERVISOR","值班主管","门店运营","active"),("pos-online","ONLINE","线上订单专员","数字运营","active")]
    base_positions=[("pos-manager","MANAGER","店长","门店运营","active"),("pos-cashier","CASHIER","收银员","前台服务","active"),("pos-senior-sales","SENIOR_SALES","资深导购","销售服务","active"),("pos-sales","SALES_ASSOCIATE","导购","销售服务","active"),("pos-stock-clerk","STOCK_CLERK","理货员","仓储运营","active")]
    skills=[("skill-cat-sales","SALES","销售","销售服务","active"),("skill-cat-cashier","CASHIER","收银","前台服务","active"),("skill-cat-service","SERVICE","顾客服务","前台服务","active"),("skill-cat-stocking","STOCKING","理货","仓储运营","active"),("skill-cat-inventory","INVENTORY","库存","仓储运营","active"),("skill-cat-ops","OPERATIONS","运营管理","门店运营","active"),("skill-cat-display","DISPLAY","商品陈列","销售服务","active"),("skill-cat-fulfillment","FULFILLMENT","线上履约","数字运营","active"),("skill-cat-safety","SAFETY","安全巡检","门店运营","active"),("skill-cat-data","DATA","数据分析","数字运营","active")]
    db.executemany("INSERT OR IGNORE INTO stores VALUES(?,?,?,?)",stores)
    db.executemany("INSERT OR IGNORE INTO departments VALUES(?,?,?,?)",departments)
    db.executemany("INSERT OR IGNORE INTO job_positions VALUES(?,?,?,?,?)",base_positions+positions)
    db.executemany("INSERT OR IGNORE INTO skill_catalog VALUES(?,?,?,?,?)",skills)
    names=["陆安","沈佳","韩雪","蒋欣","曹宇","邓琳","冯超","彭悦","董浩","袁静","潘晨","吕雯","梁峰","谢雨","宋杰","苏晴","叶凡","程璐","魏宁","余可"]
    roles=[("客服专员","前台服务",("顾客服务","收银")),("库存专员","仓储运营",("库存","理货")),("陈列专员","销售服务",("商品陈列","销售")),("值班主管","门店运营",("运营管理","安全巡检")),("线上订单专员","数字运营",("线上履约","数据分析"))]
    store_ids=["store-a"]*12+["store-d","store-e","store-f","store-g","store-h","store-d","store-e","store-f"]
    employees=[];employee_skills=[];balances=[]
    for index,name in enumerate(names,13):
        role,department,role_skills=roles[(index-13)%len(roles)];employee_id=f"emp-{index:03d}";store_id=store_ids[index-13];employment="全职" if index%4 else "兼职";weekly=40 if employment=="全职" else 24
        employees.append((employee_id,f"FS{index:03d}",name,role,department,store_id,employment,"active",f"202{2+(index%4)}-{1+(index%9):02d}-15",None,f"138****{2100+index}",f"fs{index:03d}@flowstaff.local",30+(index%8),weekly,1+(index%2),dumps({"ai_summary":["偏好早班","周末可用","班型灵活","避免连续夜班"][index%4]})))
        for skill_index,skill in enumerate(role_skills):employee_skills.append((f"skill-{employee_id}-{skill_index}",employee_id,skill,3+(index+skill_index)%3,4,1,"2026-01-01","2028-01-01","组织技能认证"))
        for leave_index,(leave_type,entitled) in enumerate((("年假",10),("病假",8),("事假",5),("调休",12))):balances.append((f"lb-{employee_id}-{leave_index}",employee_id,2026,leave_type,entitled,index%3,0))
    db.executemany("INSERT OR IGNORE INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",employees)
    db.executemany("INSERT OR IGNORE INTO employee_skills VALUES(?,?,?,?,?,?,?,?,?)",employee_skills)
    db.executemany("INSERT OR IGNORE INTO leave_balances VALUES(?,?,?,?,?,?,?)",balances)


def seed_demo_operations(db):
    """初始化可读的班次主数据、8月1-6日正式班表及考勤示例，仅在对应数据不存在时写入。"""
    now=utcnow();templates=[("shift-morning","MORNING","早班","09:00","17:00",8,"day","标准早班"),("shift-noon","NOON","午班","12:00","20:00",8,"day","标准午班"),("shift-evening","EVENING","晚班","14:00","22:00",8,"night","晚间客流班"),("shift-rest","REST","休息班","00:00","00:00",0,"rest","休息日，不计入工时")]
    db.executemany("INSERT OR IGNORE INTO shift_templates VALUES(?,?,?,?,?,?,?,?,?,?,?)",[(x[0],x[1],x[2],x[3],x[4],x[5],x[6],"active",x[7],now,now) for x in templates])
    employees=[dict(item) for item in db.execute("SELECT id,store_id,role,weekly_hour_limit,night_shift_limit,preferences_json FROM employees WHERE status='active' ORDER BY code")]
    if not employees:return
    schedule_version=db.execute("SELECT value FROM meta WHERE key='demo_schedule_version'").fetchone()
    if not schedule_version or schedule_version["value"]!="3":
        db.execute("DELETE FROM shifts WHERE plan_id='plan-seed-august-1-6'");db.execute("DELETE FROM schedule_plans WHERE id='plan-seed-august-1-6'")
        db.execute("INSERT INTO schedule_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",("plan-seed-august-1-6",None,"8月1日至6日基础班表","seed","published",0,dumps({"coverage":100,"required":len(employees)*6,"assigned":len(employees)*6,"cost":0,"preference_rate":0,"risk_count":0}),dumps({"facts":["系统初始化的基础班表"],"tradeoffs":["用于演示班次、请假和加班数据"],"compliance":{"hard_conflicts":0,"rules_checked":6}}),"seed",now,now,now))
        rest_by_employee={};night_counts={employee["id"]:0 for employee in employees}
        for index,employee in enumerate(employees):
            preference=loads(employee["preferences_json"],{}).get("ai_summary","");employee["preference"]=preference;max_work_days=min(5,int(float(employee["weekly_hour_limit"])//8));rest_days=[]
            if index<6:rest_days.append(index+1)
            if "周二休息" in preference:rest_days.append(4)
            if "周三" in preference and "不可用" in preference:rest_days.append(5)
            if "仅工作日" in preference:rest_days.extend([1,2])
            for candidate in range(1,7):
                if len(set(rest_days))>=6-max_work_days:break
                if candidate not in rest_days:rest_days.append(candidate)
            rest_by_employee[employee["id"]]=set(rest_days[:6-max_work_days])
        stores=sorted({employee["store_id"] for employee in employees})
        for day in range(1,7):
            for store_id in stores:
                store_employees=[employee for employee in employees if employee["store_id"]==store_id];working=[employee for employee in store_employees if day not in rest_by_employee[employee["id"]]]
                ratio=(.30,.40,.30) if day in (1,2) else (.40,.35,.25);raw=[len(working)*value for value in ratio];base,remainder=divmod(len(working),3);counts=[base,base,base]
                for slot in sorted(range(3),key=lambda item:raw[item]-int(raw[item]),reverse=True)[:remainder]:counts[slot]+=1
                slots=[template_index for template_index,count in enumerate(counts) for _ in range(count)]
                workers=sorted(working,key=lambda employee:("早班" not in employee["preference"] and "中班" not in employee["preference"] and "避免" not in employee["preference"],employee["id"]))
                for employee in workers:
                    preference=employee["preference"];preferred=0 if "早班" in preference else 1 if "中班" in preference or "10:00后" in preference else None
                    allowed=[slot for slot in slots if not (slot==2 and (("避免" in preference and "夜班" in preference) or night_counts[employee["id"]]>=int(employee["night_shift_limit"])))]
                    if not allowed:
                        slots.remove(2);slots.append(1);allowed=[1]
                    selected=preferred if preferred in allowed else allowed[0];slots.remove(selected);template=templates[selected];night_counts[employee["id"]]+=int(selected==2)
                    db.execute("INSERT INTO shifts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(f"shift-seed-{day:02d}-{employee['id']}","plan-seed-august-1-6",employee["id"],employee["store_id"],employee["role"],f"2026-08-{day:02d}T{template[3]}:00+00:00",f"2026-08-{day:02d}T{template[4]}:00+00:00","published",f"seed:{template[0]}",now,now))
                for employee in store_employees:
                    if employee in working:continue
                    template=templates[3]
                    db.execute("INSERT INTO shifts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(f"shift-seed-{day:02d}-{employee['id']}","plan-seed-august-1-6",employee["id"],employee["store_id"],employee["role"],f"2026-08-{day:02d}T{template[3]}:00+00:00",f"2026-08-{day:02d}T{template[4]}:00+00:00","published",f"seed:{template[0]}",now,now))
        db.execute("INSERT OR REPLACE INTO meta VALUES('demo_schedule_version','3')")
    for index,employee in enumerate(employees[:6]):
        leave_date=f"2026-08-{index+1:02d}";db.execute("INSERT OR IGNORE INTO attendance VALUES(?,?,?,?,?,?,?,?,?)",(f"att-demo-leave-{employee['id']}",employee["id"],leave_date,"leave",f"{leave_date}T09:00:00+00:00",0,"seeded_leave",dumps({"leave_type":"年假","demo":True}),now))
    for index,employee in enumerate(employees[6:12]):
        overtime_date=f"2026-08-{index+1:02d}";db.execute("INSERT OR IGNORE INTO attendance VALUES(?,?,?,?,?,?,?,?,?)",(f"att-demo-overtime-{employee['id']}",employee["id"],overtime_date,"overtime",f"{overtime_date}T20:30:00+00:00",2,"seeded_overtime",dumps({"approved":True,"demo":True}),now))


def seed(db):
    now = utcnow()
    db.executemany("INSERT INTO stores VALUES(?,?,?,?)", [
        ("store-a", "SH-A01", "上海静安旗舰店", "上海"),
        ("store-b", "SH-B02", "上海徐汇中心店", "上海"),
        ("store-c", "SH-C03", "上海浦东陆家嘴店", "上海"),
    ])
    employees = [
        ("emp-001","FS001","林薇","店长","门店运营","store-a","全职","active","2021-03-15",None,"138****2101","linwei@flowstaff.local",52,40,2,{"ai_summary":"偏好工作日早班，周三晚间不可用"}),
        ("emp-002","FS002","陈晨","收银员","前台服务","store-a","全职","active","2022-06-01","emp-001","138****2102","chenchen@flowstaff.local",35,40,2,{"ai_summary":"偏好早班，可接受周末"}),
        ("emp-003","FS003","李敏","资深导购","销售服务","store-a","全职","active","2020-09-12","emp-001","138****2103","limin@flowstaff.local",42,40,1,{"ai_summary":"避免夜班，希望周二休息"}),
        ("emp-004","FS004","王磊","理货员","仓储运营","store-a","全职","active","2023-01-08","emp-001","138****2104","wanglei@flowstaff.local",33,40,2,{"ai_summary":"班型灵活，可跨岗支援"}),
        ("emp-005","FS005","周颖","导购","销售服务","store-a","兼职","active","2024-04-20","emp-001","138****2105","zhouying@flowstaff.local",31,24,1,{"ai_summary":"仅工作日10:00后可用"}),
        ("emp-006","FS006","赵强","收银员","前台服务","store-b","全职","active","2022-11-03",None,"138****2106","zhaoqiang@flowstaff.local",34,40,2,{"ai_summary":"接受跨店，偏好中班"}),
        ("emp-007","FS007","孙悦","导购","销售服务","store-b","全职","active","2023-05-16",None,"138****2107","sunyue@flowstaff.local",36,40,1,{"ai_summary":"周末可用，避免连续夜班"}),
        ("emp-008","FS008","高远","理货员","仓储运营","store-c","全职","active","2021-12-12",None,"138****2108","gaoyuan@flowstaff.local",33,40,3,{"ai_summary":"可跨店，技能覆盖广"}),
        ("emp-009","FS009","何欣","收银员","前台服务","store-a","兼职","active","2025-02-10","emp-001","138****2109","hexin@flowstaff.local",30,20,1,{"ai_summary":"周五至周日可用"}),
        ("emp-010","FS010","许婷","导购","销售服务","store-c","全职","active","2022-08-18",None,"138****2110","xuting@flowstaff.local",38,40,1,{"ai_summary":"希望参与带教，偏好早班"}),
        ("emp-011","FS011","刘洋","收银员","前台服务","store-b","全职","active","2024-01-15",None,"138****2111","liuyang@flowstaff.local",32,40,2,{"ai_summary":"班型灵活"}),
        ("emp-012","FS012","唐宁","理货员","仓储运营","store-a","兼职","active","2025-06-01","emp-001","138****2112","tangning@flowstaff.local",29,24,1,{"ai_summary":"周末与晚间可用"}),
    ]
    db.executemany("INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [x[:-1] + (dumps(x[-1]),) for x in employees])
    skill_rows=[]
    role_skills={"店长":[("运营管理",5,5), ("收银",4,4)],"收银员":[("收银",4,4),("顾客服务",3,4)],"资深导购":[("销售",5,5),("顾客服务",5,5)],"导购":[("销售",3,4),("顾客服务",4,4)],"理货员":[("理货",4,4),("库存",3,4)]}
    for employee in employees:
        for index,(skill,level,target) in enumerate(role_skills[employee[3]]):
            skill_rows.append((f"skill-{employee[0]}-{index}",employee[0],skill,level,target,1,"2025-01-01","2027-01-01","内部认证"))
    db.executemany("INSERT INTO employee_skills VALUES(?,?,?,?,?,?,?,?,?)", skill_rows)
    users=[("user-admin","admin","FlowStaff123!","系统管理员","admin",None,None),("user-manager","manager","Manager123!","林薇","manager","emp-001","store-a"),("user-hr","hr","FlowHR123!","顾晓","hr",None,None),("user-auditor","auditor","Audit12345!","审计员","auditor",None,None),("user-employee","employee","Employee123!","陈晨","employee","emp-002","store-a")]
    db.executemany("INSERT INTO users(id,username,password_hash,display_name,role,employee_id,store_id,status,created_at) VALUES(?,?,?,?,?,?,?,'active',?)",[(x[0],x[1],password_hash(x[2]),x[3],x[4],x[5],x[6],now) for x in users])
    rules=[
        ("rule-1","周工时上限","员工每周排班不得超过合同约定周工时","company","hard","hours",{"operator":"lte","field":"weekly_hours","value":40},"active",3,"manual",1.0),
        ("rule-2","单日一班","同一员工自然日最多安排一个班次","company","hard","schedule",{"max_shifts_per_day":1},"active",2,"manual",1.0),
        ("rule-3","最小休息间隔","相邻班次至少间隔11小时","company","hard","fatigue",{"min_rest_hours":11},"active",2,"manual",1.0),
        ("rule-4","员工偏好优先","在满足覆盖与合规后尽量满足班型偏好","store","soft","schedule",{"preference_weight":18},"active",1,"manual",1.0),
        ("rule-5","高峰技能覆盖","高峰每班至少一名高熟练员工","store","soft","skills",{"min_proficiency":4,"min_count":1},"active",1,"manual",1.0),
        ("rule-6","连续夜班提醒","连续两次夜班触发疲劳提醒","company","notice","fatigue",{"night_streak":2},"active",1,"manual",1.0),
    ]
    today=datetime.now(ZoneInfo(os.getenv("WFM_TIMEZONE","Asia/Shanghai"))).date().isoformat()
    db.executemany("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",[(x[0],x[1],x[2],x[3],x[4],x[5],dumps(x[6]),x[7],x[8],x[9],x[10],"user-admin","user-hr",now,now,"store-a" if x[3]=="store" else None,today if x[3]=="store" else None,None) for x in rules])
    start=datetime(2026,8,1,tzinfo=timezone.utc)
    attendance=[]
    states=["normal","normal","late","normal","overtime","leave","normal","absence"]
    for day in range(1,8):
        for idx,employee in enumerate(employees[:9]):
            state=states[(day+idx)%len(states)]
            hours=2 if state=="overtime" else 0
            attendance.append((f"att-{day}-{idx}",employee[0],f"2026-08-{day:02d}",state,f"2026-08-{day:02d}T09:{(idx*3)%20:02d}:00+00:00",hours,"seeded_hris",dumps({"verified":True}),now))
    db.executemany("INSERT INTO attendance VALUES(?,?,?,?,?,?,?,?,?)",attendance)
    leave_types=[("年假",10,3,1),("病假",8,1,0),("事假",5,1,0),("调休",12,4,2)]
    db.executemany("INSERT INTO leave_balances VALUES(?,?,?,?,?,?,?)",[(f"lb-{e[0]}-{i}",e[0],2026,t,*v) for e in employees for i,(t,*v) in enumerate(leave_types)])
    anomalies=[
        ("anom-1","emp-004","store-a","repeated_late","medium",.88,["近7天迟到3次","累计迟到29分钟"],"早班交接稳定性下降",["通勤变化","排班适配度不足"],["与员工沟���近期到岗困难","评估临时调整班型"],"open"),
        ("anom-2","emp-003","store-a","continuous_overtime","high",.93,["连续4天加班","近7天加班11.5小时"],"疲劳风险增加",["促销期客流上升","技能覆盖集中"],["降低后续两日工时","增加具备销售技能人员"],"monitoring"),
        ("anom-3","emp-007","store-b","leave_increase","low",.72,["近28天请假3次"],"班表稳定性需关注",["个人安排变化，需人工核实"],["采用非惩罚性沟通确认可用时间"],"acknowledged"),
    ]
    db.executemany("INSERT INTO anomaly_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",[(x[0],x[1],x[2],x[3],x[4],x[5],dumps(x[6]),x[7],dumps(x[8]),dumps(x[9]),x[10],now,now) for x in anomalies])
    demands=[("demand-1","2026-08-06","09:00","17:00","收银员",2,.88,["工作日基线","午间客流"]),("demand-2","2026-08-06","10:00","18:00","导购",2,.84,["新品活动"]),("demand-3","2026-08-07","09:00","17:00","收银员",3,.91,["周五晚高峰"]),("demand-4","2026-08-08","10:00","19:00","导购",3,.86,["周末","商圈活动"]),("demand-5","2026-08-08","08:00","16:00","理货员",2,.9,["周末补货"])]
    db.executemany("INSERT INTO business_demands VALUES(?,?,?,?,?,?,?,?,?,?,?)",[(x[0],"store-a",x[1],x[2],x[3],x[4],x[5],x[6],dumps(x[7]),"forecast_seed",now) for x in demands])
    for rule in rules:
        content=f"{rule[1]}。{rule[2]}。范围：{rule[3]}，强度：{rule[4]}，业务域：{rule[5]}。"
        db.execute("INSERT INTO vector_documents VALUES(?,?,?,?,?,?,?,?,?,?)",(f"doc-{rule[0]}","rule",rule[0],rule[1],content,None,"local_hash",hashlib.sha256(content.encode()).hexdigest(),dumps({"status":"active"}),now))
    db.commit()


def rowdict(row):
    return dict(row) if row else None
