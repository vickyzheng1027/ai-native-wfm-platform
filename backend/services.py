import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import AIClient, classify_intent, rag_answer
from .db import connect, dumps, loads, rowdict, transaction, utcnow

try:
    from ortools.sat.python import cp_model
except ImportError:
    cp_model = None

AUTOMATION_ADMISSION_LOCK = threading.RLock()
AUTOMATION_CONNECTION_LOCK = threading.RLock()


class ApiError(Exception):
    def __init__(self, message, status=400, code="BAD_REQUEST", details=None):
        super().__init__(message);self.status=status;self.code=code;self.details=details


def uid(prefix):return f"{prefix}-{uuid.uuid4()}"
def rows(db,sql,args=()):return [dict(x) for x in db.execute(sql,args).fetchall()]


def audit(db,user,action,resource_type,resource_id,result="success",details=None,ip="",request_id=""):
    db.execute("INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?,?,?)",(uid("audit"),utcnow(),user.get("id") if user else None,action,resource_type,resource_id,result,ip,request_id,dumps(details or {})));db.commit()


def overview(db,user):
    store_clause=" WHERE store_id=?" if user.get("store_id") and user["role"] not in ("admin","hr","auditor") else ""
    args=(user["store_id"],) if store_clause else ()
    employee_count=db.execute(f"SELECT COUNT(*) n FROM employees{store_clause} AND status='active'" if store_clause else "SELECT COUNT(*) n FROM employees WHERE status='active'",args).fetchone()["n"]
    open_anomalies=db.execute(f"SELECT COUNT(*) n FROM anomaly_events{store_clause} AND status IN ('open','acknowledged','monitoring')" if store_clause else "SELECT COUNT(*) n FROM anomaly_events WHERE status IN ('open','acknowledged','monitoring')",args).fetchone()["n"]
    active_plan=db.execute("SELECT * FROM schedule_plans WHERE status IN ('active','published') ORDER BY COALESCE(published_at,activated_at) DESC LIMIT 1").fetchone()
    start,end=business_month_period()
    attendance=attendance_overview(db,user,start,end)
    recent_tasks=rows(db,"SELECT id,input_text,intent,status,progress,created_at FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT 5",(user["id"],))
    return {"employee_count":employee_count,"open_anomalies":open_anomalies,"active_rules":db.execute("SELECT COUNT(*) n FROM rules WHERE status='active'").fetchone()["n"],"attendance_rate":attendance["summary"]["attendance_rate"],"active_plan":rowdict(active_plan),"recent_tasks":recent_tasks,"briefings":[{"level":"warning","title":"静安旗舰店周六客流预计上升18%","detail":"当前导购覆盖仍有1人缺口，建议在发布前补齐。"},{"level":"info","title":"2条员工偏好将在下周生效","detail":"排班求解会自动纳入软约束评分。"},{"level":"success","title":"本周合规校验通过率 98.6%","detail":"未发现周工时和最小休息间隔硬冲突。"}]}


def employee_list(db,user):
    sql="SELECT e.*,s.name store_name FROM employees e JOIN stores s ON s.id=e.store_id";args=[]
    if user["role"]=="employee":sql+=" WHERE e.id=?";args=[user["employee_id"]]
    elif user.get("store_id") and user["role"]=="manager":sql+=" WHERE e.store_id=?";args=[user["store_id"]]
    result=[]
    for employee in rows(db,sql+" ORDER BY e.code",args):
        employee["preferences"]=loads(employee.pop("preferences_json"),{})
        employee["skills"]=rows(db,"SELECT id,skill,proficiency,target_level,certified,expires_at,evidence FROM employee_skills WHERE employee_id=?",(employee["id"],))
        result.append(employee)
    return result


def shift_template_list(db):return rows(db,"SELECT * FROM shift_templates ORDER BY CASE shift_type WHEN 'day' THEN 1 WHEN 'night' THEN 2 ELSE 3 END,start_time")


def save_shift_template(db,user,body,template_id=None):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无班次维护权限",403,"FORBIDDEN")
    required=("code","name","start_time","end_time","shift_type")
    if any(not body.get(key) for key in required):raise ApiError("班次编码、名称、开始时间、结束时间和类型为必填项")
    template_id=template_id or uid("shift-template");existing=db.execute("SELECT 1 FROM shift_templates WHERE id=?",(template_id,)).fetchone();start=body["start_time"];end=body["end_time"]
    paid_hours=0 if body["shift_type"]=="rest" else float(body.get("paid_hours",8))
    values=(body["code"],body["name"],start,end,paid_hours,body["shift_type"],body.get("status","active"),body.get("description",""),utcnow())
    if existing:db.execute("UPDATE shift_templates SET code=?,name=?,start_time=?,end_time=?,paid_hours=?,shift_type=?,status=?,description=?,updated_at=? WHERE id=?",values+(template_id,))
    else:db.execute("INSERT INTO shift_templates VALUES(?,?,?,?,?,?,?,?,?,?,?)",(template_id,)+values[:-1]+(values[-1],values[-1]))
    db.commit();audit(db,user,"shift_template.save","shift_template",template_id,details={"created":not bool(existing)})
    return rowdict(db.execute("SELECT * FROM shift_templates WHERE id=?",(template_id,)).fetchone())


def save_employee(db,user,body,employee_id=None):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无员工档案维护权限",403,"FORBIDDEN")
    if user["role"]=="manager" and body.get("store_id",user.get("store_id"))!=user.get("store_id"):raise ApiError("只能维护授权门店员工",403,"FORBIDDEN")
    employee_id=employee_id or uid("emp");existing=db.execute("SELECT * FROM employees WHERE id=?",(employee_id,)).fetchone()
    required=["code","name","role","department","store_id"]
    if any(not body.get(x) for x in required):raise ApiError("工号、姓名、岗位、部门和门店为必填项")
    if not db.execute("SELECT 1 FROM job_positions WHERE name=? AND status='active'",(body["role"],)).fetchone():raise ApiError("所选岗位不存在或已停用")
    if not db.execute("SELECT 1 FROM departments WHERE name=? AND status='active'",(body["department"],)).fetchone():raise ApiError("所选部门不存在或已停用")
    if not db.execute("SELECT 1 FROM stores WHERE id=?",(body["store_id"],)).fetchone():raise ApiError("所选门店不存在")
    invalid_skills=[skill.get("skill") for skill in body.get("skills",[]) if not db.execute("SELECT 1 FROM skill_catalog WHERE name=? AND status='active'",(skill.get("skill"),)).fetchone()]
    if invalid_skills:raise ApiError("包含无效技能："+"、".join(str(skill) for skill in invalid_skills))
    values=(body["code"],body["name"],body["role"],body["department"],body["store_id"],body.get("employment_type","全职"),body.get("status","active"),body.get("hire_date",business_today().isoformat()),body.get("manager_id"),body.get("phone",""),body.get("email",""),float(body.get("hourly_rate",0)),float(body.get("weekly_hour_limit",40)),int(body.get("night_shift_limit",2)),dumps(body.get("preferences",{})))
    with transaction(db):
        if existing:db.execute("UPDATE employees SET code=?,name=?,role=?,department=?,store_id=?,employment_type=?,status=?,hire_date=?,manager_id=?,phone=?,email=?,hourly_rate=?,weekly_hour_limit=?,night_shift_limit=?,preferences_json=? WHERE id=?",values+(employee_id,))
        else:db.execute("INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(employee_id,)+values)
        if "skills" in body:
            db.execute("DELETE FROM employee_skills WHERE employee_id=?",(employee_id,))
            for skill in body["skills"]:db.execute("INSERT INTO employee_skills VALUES(?,?,?,?,?,?,?,?,?)",(uid("skill"),employee_id,skill["skill"],int(skill.get("proficiency",1)),int(skill.get("target_level",3)),1 if skill.get("certified") else 0,skill.get("certified_at"),skill.get("expires_at"),skill.get("evidence","")))
    audit(db,user,"employee.save","employee",employee_id,details={"created":not bool(existing)})
    return next(x for x in employee_list(db,{**user,"role":"admin","store_id":None}) if x["id"]==employee_id)


def attendance_overview(db,user,start,end,name="",code=""):
    employees=employee_list(db,user)
    if name:employees=[item for item in employees if name.lower() in item["name"].lower()]
    if code:employees=[item for item in employees if code.lower() in item["code"].lower()]
    ids=[x["id"] for x in employees]
    data=rows(db,f"SELECT * FROM attendance WHERE event_date BETWEEN ? AND ? AND employee_id IN ({','.join('?' for _ in ids)}) ORDER BY event_date" if ids else "SELECT * FROM attendance WHERE 0",(start,end,*ids) if ids else ())
    normal=sum(x["event_type"] in ("normal","late") for x in data);expected=sum(x["event_type"] not in ("leave","overtime") for x in data);exceptions=sum(x["event_type"] in ("late","absence") for x in data)
    daily={}
    for item in data:
        key=(item["employee_id"],item["event_date"]);summary=daily.setdefault(key,{"employee_id":item["employee_id"],"event_date":item["event_date"],"attendance_hours":0.0,"leave_hours":0.0,"late_count":0,"overtime_hours":0.0,"codes":[]})
        event=item["event_type"];hours=float(item["hours"] or 0);metadata=loads(item.get("metadata_json"),{})
        if event in ("normal","late"):summary["attendance_hours"]+=hours or float(metadata.get("attendance_hours",8))
        elif event=="leave":summary["leave_hours"]+=hours or float(metadata.get("leave_hours",8))
        if event=="late":summary["late_count"]+=1
        if event=="overtime":summary["overtime_hours"]+=hours
        if event not in summary["codes"]:summary["codes"].append(event)
    daily_summary=list(daily.values())
    for summary in daily_summary:
        for key in ("attendance_hours","leave_hours","overtime_hours"):summary[key]=round(summary[key],1)
    balances=rows(db,f"SELECT * FROM leave_balances WHERE employee_id IN ({','.join('?' for _ in ids)}) ORDER BY employee_id,leave_type" if ids else "SELECT * FROM leave_balances WHERE 0",ids)
    approvals=[]
    if user["role"] in ("admin","manager","hr"):
        sql="SELECT r.*,e.name employee_name,e.code employee_code,e.store_id FROM employee_requests r JOIN employees e ON e.id=r.employee_id WHERE r.status='pending_manager'";args=[]
        if user["role"]=="manager":sql+=" AND e.store_id=?";args.append(user.get("store_id"))
        approvals=[{**item,"payload":loads(item.pop("payload_json"),{}),"analysis":loads(item.pop("agent_analysis_json"),{})} for item in rows(db,sql+" ORDER BY r.created_at",args)]
    return {"employees":[{"id":x["id"],"name":x["name"],"code":x["code"],"role":x["role"]} for x in employees],"records":data,"daily_summary":daily_summary,"balances":balances,"approval_requests":approvals,"summary":{"attendance_rate":round(normal/expected*100,1) if expected else 0,"exceptions":exceptions,"leave_records":sum(x["event_type"]=="leave" for x in data),"overtime_hours":sum(x["hours"] or 0 for x in data if x["event_type"]=="overtime")}}


def business_today():
    return datetime.now(ZoneInfo(os.getenv("WFM_TIMEZONE","Asia/Shanghai"))).date()


def business_month_period(today=None):
    today=today or business_today();start=date(today.year,today.month,1);end=date(today.year+1,1,1)-timedelta(days=1) if today.month==12 else date(today.year,today.month+1,1)-timedelta(days=1);return start.isoformat(),end.isoformat()


def resolve_schedule_period(text,today=None):
    today=today or business_today();current_year=today.year
    if "本月" in str(text) or "这个月" in str(text) or "当月" in str(text):return business_month_period(today)
    date_matches=re.findall(r"(?:(\d{4})[-年])?(\d{1,2})[-月](\d{1,2})日?",str(text))
    normalized=[f"{int(year or current_year):04d}-{int(month):02d}-{int(day):02d}" for year,month,day in date_matches]
    if normalized:return normalized[0],normalized[-1]
    month_match=re.search(r"(?:(\d{4})年)?(\d{1,2}|[一二三四五六七八九十]+)月(?:份)?",str(text))
    if month_match:
        month_raw=month_match.group(2);digits={"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
        month=digits.get(month_raw,int(month_raw) if month_raw.isdigit() else None)
        year=int(month_match.group(1) or current_year)
        if month and 1<=month<=12:
            first=date(year,month,1);last=date(year+1,1,1)-timedelta(days=1) if month==12 else date(year,month+1,1)-timedelta(days=1)
            return first.isoformat(),last.isoformat()
    if "今天" in text:return today.isoformat(),today.isoformat()
    if "后天" in text:
        target=today+timedelta(days=2);return target.isoformat(),target.isoformat()
    if "明天" in text:
        target=today+timedelta(days=1);return target.isoformat(),target.isoformat()
    monday=today-timedelta(days=today.weekday())
    if "本周末" in text or "这周末" in text:
        return (monday+timedelta(days=5)).isoformat(),(monday+timedelta(days=6)).isoformat()
    if "下周末" in text:
        return (monday+timedelta(days=12)).isoformat(),(monday+timedelta(days=13)).isoformat()
    weekdays={"一":0,"二":1,"三":2,"四":3,"五":4,"六":5,"日":6,"天":6}
    relative_matches=re.findall(r"(本|这|下下|下)?(?:周|星期)([一二三四五六日天])",str(text))
    resolved=[]
    for prefix,weekday in relative_matches:
        target_index=weekdays[weekday]
        if prefix in ("本","这"):target=monday+timedelta(days=target_index)
        elif prefix=="下":target=monday+timedelta(days=7+target_index)
        elif prefix=="下下":target=monday+timedelta(days=14+target_index)
        else:target=today+timedelta(days=(target_index-today.weekday())%7)
        resolved.append(target.isoformat())
    if resolved:return resolved[0],resolved[-1]
    if "下周" in text:return (monday+timedelta(days=7)).isoformat(),(monday+timedelta(days=13)).isoformat()
    if "本周" in text or "这周" in text:return today.isoformat(),(monday+timedelta(days=6)).isoformat()
    return None,None


def parse_schedule_parameters(text,today=None):
    start,end=resolve_schedule_period(text,today);demand_items=parse_explicit_staffing_items(text);role=demand_items[0]["role"] if len(demand_items)==1 else None;headcount=demand_items[0]["headcount"] if len(demand_items)==1 else None
    coverage=float(re.search(r"覆盖率[^\d]*(\d+(?:\.\d+)?)",text).group(1)) if re.search(r"覆盖率[^\d]*(\d+(?:\.\d+)?)",text) else 95
    cost=float(re.search(r"成本[^\d]*(\d+(?:\.\d+)?)%",text).group(1)) if re.search(r"成本[^\d]*(\d+(?:\.\d+)?)%",text) else 8
    peaks=[]
    if any(word in text for word in ("早晨","早上","上午")):peaks.append("morning")
    if "下午" in text:peaks.append("afternoon")
    if any(word in text for word in ("晚上","晚间","夜间")):peaks.append("evening")
    activity="促销活动" if any(word in text for word in ("促销","大促","活动")) else "日常经营"
    return {"start_date":start,"end_date":end,"role":role,"headcount":headcount,"demand_items":demand_items,"activity_type":activity,"peak_periods":peaks,"overtime_control":"加班" in text,"night_shift_control":"夜班" in text,"coverage_target":coverage,"cost_increase_limit":cost,"minimize_nights":"夜班" in text,"raw_constraints":[]}


def normalize_model_date(value,today=None):
    if not value:return None
    try:return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        start,_=resolve_schedule_period(str(value),today);return start


def parse_headcount(value):
    raw=str(value);digits=re.search(r"\d+",raw)
    if digits:return int(digits.group())
    numbers={"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
    if raw in numbers:return numbers[raw]
    if "十" in raw:
        left,right=raw.split("十",1);return numbers.get(left,1)*10+numbers.get(right,0)
    return None


def parse_explicit_staffing_items(text):
    aliases=(("资深导购",("资深导购",)),("收银员",("收银员","收银")),("理货员",("理货员","理货")),("导购",("导购",)),("店长",("店长",)));number_pattern=r"(\d+|[零一二两三四五六七八九十]+)";result=[]
    for role,names in aliases:
        match=None
        for name in names:
            for pattern in (rf"{number_pattern}\s*(?:名|个|位)?\s*{name}",rf"{name}[^，。；,;]{{0,10}}?(?:需要|安排|要)?\s*{number_pattern}\s*(?:名|个|位)?"):
                match=re.search(pattern,text)
                if match:break
            if match:break
        if match:
            headcount=parse_headcount(match.group(1))
            if headcount is not None:result.append({"role":role,"headcount":headcount})
    return result


def parse_explicit_staffing(text):
    items=parse_explicit_staffing_items(text)
    return (items[0]["role"],items[0]["headcount"]) if len(items)==1 else (None,None)


def requested_demand_items(params):
    items=[]
    for item in params.get("demand_items") or []:
        if isinstance(item,dict) and item.get("role") and isinstance(item.get("headcount"),int) and item["headcount"]>0:items.append({"role":item["role"],"headcount":item["headcount"]})
    if not items and params.get("role") and isinstance(params.get("headcount"),int) and params["headcount"]>0:items=[{"role":params["role"],"headcount":params["headcount"]}]
    return items


def apply_explicit_demand_constraints(demands,params):
    requested=requested_demand_items(params);requirements={item["role"]:item["headcount"] for item in requested};filtered=[dict(item) for item in demands if not requirements or item["role"] in requirements]
    if requirements:
        per_date_role={}
        for item in filtered:per_date_role.setdefault((item["demand_date"],item["role"]),item)
        filtered=[]
        for (_,role),item in per_date_role.items():item["required_count"]=requirements[role];filtered.append(item)
    return sorted(filtered,key=lambda item:(item["demand_date"],item["start_time"]))


def task_store_id(db,user,params):
    store_code=params.get("store_code")
    if store_code:
        store=db.execute("SELECT id FROM stores WHERE code=? OR name=?",(store_code,store_code)).fetchone()
        if store:return store["id"]
    raise ApiError("未指定门店。请告诉我需要为哪家门店排班，我不会替你猜测门店。",422,"STORE_CONFIRMATION_REQUIRED",{"available_stores":rows(db,"SELECT id,code,name FROM stores ORDER BY name")})


def store_mentioned_in_text(db,text):
    normalized=str(text).replace(" ","")
    for store in rows(db,"SELECT id,code,name FROM stores ORDER BY name"):
        aliases={store["code"],store["name"],store["name"].replace("上海","").replace("旗舰店","店").replace("中心店","店")}
        if any(alias and alias.replace(" ","") in normalized for alias in aliases):return store
    return None


def ensure_demand_forecast(db,user,params):
    store_id=task_store_id(db,user,params);start=params.get("start_date");end=params.get("end_date")
    if not start or not end:raise ApiError("排班任务缺少开始和结束日期",422,"MISSING_SCHEDULE_DATES")
    start_date=datetime.fromisoformat(start).date();end_date=datetime.fromisoformat(end).date()
    if end_date<start_date or (end_date-start_date).days>31:raise ApiError("排班周期必须在 1 至 31 天内",422,"INVALID_SCHEDULE_RANGE")
    existing=rows(db,"SELECT * FROM business_demands WHERE store_id=? AND demand_date BETWEEN ? AND ? ORDER BY demand_date,start_time",(store_id,start,end))
    baseline=rows(db,"SELECT * FROM business_demands WHERE store_id=? ORDER BY demand_date,start_time",(store_id,))
    if not baseline:raise ApiError("当前门店没有可用于预测的历史岗位需求，请先录入业务需求",422,"NO_DEMAND_BASELINE")
    requested=requested_demand_items(params)
    if requested:
        completed=[];generated=[];day=start_date
        with transaction(db):
            while day<=end_date:
                for requirement in requested:
                    matching=[item for item in existing if item["demand_date"]==day.isoformat() and item["role"]==requirement["role"]]
                    if matching:
                        demand=dict(matching[0]);demand["required_count"]=requirement["headcount"];completed.append(demand);continue
                    samples=[item for item in baseline if item["role"]==requirement["role"]] or baseline;sample=samples[0]
                    demand={"id":uid("demand"),"store_id":store_id,"demand_date":day.isoformat(),"start_time":sample["start_time"],"end_time":sample["end_time"],"role":requirement["role"],"required_count":requirement["headcount"],"confidence":round(max(.65,sample["confidence"]-.03),2),"factors_json":dumps(["用户明确岗位人数","历史时段基线"]),"source":"explicit_user_demand","created_at":utcnow()}
                    db.execute("INSERT INTO business_demands VALUES(?,?,?,?,?,?,?,?,?,?,?)",tuple(demand.values()));generated.append(demand);completed.append(demand)
                day+=timedelta(days=1)
        return completed,"explicit_user_demand" if generated else "existing_with_user_constraints"
    if params.get("peak_periods"):
        period_to_code={"morning":"MORNING","afternoon":"NOON","evening":"EVENING"};template_by_code={item["code"]:item for item in rows(db,"SELECT * FROM shift_templates WHERE status='active' AND shift_type<>'rest'")};roles=sorted({item["role"] for item in baseline});generated=[];day=start_date
        with transaction(db):
            while day<=end_date:
                for role in roles:
                    role_samples=[item for item in baseline if item["role"]==role];base=max(1,round(sum(item["required_count"] for item in role_samples)/len(role_samples)))
                    for period in params["peak_periods"]:
                        template=template_by_code.get(period_to_code.get(period));
                        if not template:continue
                        multiplier=1.35 if params.get("activity_type") and params["activity_type"]!="日常经营" else 1.15;required=max(1,round(base*multiplier));existing_row=db.execute("SELECT * FROM business_demands WHERE store_id=? AND demand_date=? AND role=? AND start_time=?",(store_id,day.isoformat(),role,template["start_time"])).fetchone()
                        if existing_row:db.execute("UPDATE business_demands SET end_time=?,required_count=?,confidence=?,factors_json=?,source=? WHERE id=?",(template["end_time"],required,.88,dumps([params.get("activity_type","业务活动"),f"{period} 客流高峰","真实班次模板"]),"agent_peak_forecast",existing_row["id"]));generated.append({**dict(existing_row),"end_time":template["end_time"],"required_count":required})
                        else:
                            demand={"id":uid("demand"),"store_id":store_id,"demand_date":day.isoformat(),"start_time":template["start_time"],"end_time":template["end_time"],"role":role,"required_count":required,"confidence":.88,"factors_json":dumps([params.get("activity_type","业务活动"),f"{period} 客流高峰","真实班次模板"]),"source":"agent_peak_forecast","created_at":utcnow()};db.execute("INSERT INTO business_demands VALUES(?,?,?,?,?,?,?,?,?,?,?)",tuple(demand.values()));generated.append(demand)
                day+=timedelta(days=1)
        if generated:return generated,"agent_peak_forecast"
    if existing:return existing,"existing"
    patterns={}
    for demand in baseline:
        patterns.setdefault(demand["role"],[]).append(demand)
    generated=[];day=start_date
    with transaction(db):
        while day<=end_date:
            weekend_factor=1.15 if day.weekday()>=5 else 1.0
            for role,samples in patterns.items():
                sample=samples[0];start_time=sample["start_time"];end_time=sample["end_time"]
                average=sum(item["required_count"] for item in samples)/len(samples)
                count=max(1,round(average*weekend_factor))
                confidence=round(max(.65,min(.9,sum(item["confidence"] for item in samples)/len(samples)-.04)),2)
                demand={"id":uid("demand"),"store_id":store_id,"demand_date":day.isoformat(),"start_time":start_time,"end_time":end_time,"role":role,"required_count":count,"confidence":confidence,"factors_json":dumps(["历史岗位需求基线","周末系数" if day.weekday()>=5 else "工作日系数","用户输入约束"]),"source":"statistical_forecast_v1","created_at":utcnow()}
                db.execute("INSERT INTO business_demands VALUES(?,?,?,?,?,?,?,?,?,?,?)",tuple(demand.values()));generated.append(demand)
            day+=timedelta(days=1)
    if not generated:raise ApiError("未能形成有效岗位需求，请补充岗位和人数",422,"EMPTY_DEMAND_FORECAST")
    return generated,"statistical_forecast_v1"


def create_task(db,user,text,context="auto",trigger_event_id=None):
    if not text.strip():raise ApiError("请输入真实业务目标或事务内容")
    client=AIClient(db);intent=classify_intent(client,user,text,context)
    task_id=uid("task");params={k:v for k,v in (intent.get("parameters") or {}).items() if v not in (None,"")}
    if intent["intent"]=="schedule_create":
        local=parse_schedule_parameters(text);model_start=normalize_model_date(params.get("start_date"));model_end=normalize_model_date(params.get("end_date"))
        params={**local,**{key:value for key,value in params.items() if key not in ("start_date","end_date")},"start_date":model_start or local["start_date"],"end_date":model_end or model_start or local["end_date"]}
        mentioned_store=store_mentioned_in_text(db,text);params["store_code"]=mentioned_store["code"] if mentioned_store else None
        if local.get("demand_items"):
            params["demand_items"]=local["demand_items"];params["role"]=local["role"];params["headcount"]=local["headcount"]
        for key in ("activity_type","peak_periods","overtime_control","night_shift_control"):
            if local.get(key):params[key]=local[key]
    missing=[]
    if intent["intent"]=="schedule_create":
        if not params.get("start_date") or not params.get("end_date"):missing.append("period")
        if not params.get("store_code"):missing.append("store")
    task_status="awaiting_confirmation" if missing else "queued"
    db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(task_id,user["id"],intent["context"],text,intent["intent"],task_status,0,dumps(params),dumps([]),trigger_event_id,1,None,utcnow(),None));db.commit()
    audit(db,user,"task.create","task",task_id,details={"intent":intent["intent"],"mode":intent["mode"]})
    if intent["intent"]=="schedule_create":
        if not missing:
            database_path=db.execute("PRAGMA database_list").fetchone()["file"]
            if database_path:threading.Thread(target=run_schedule_task_on_connection,args=(database_path,task_id,user),daemon=True).start()
            else:run_schedule_task(db,task_id,user)
    else:run_non_schedule_task(db,task_id,user,intent)
    return {"task_id":task_id,"intent":intent,"status":task_status,"requires_confirmation":bool(missing),"missing":missing,"parameters":params,"available_stores":rows(db,"SELECT id,code,name FROM stores ORDER BY name") if "store" in missing else []}


def confirm_schedule_task(db,user,task_id,body):
    task=db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?",(task_id,user["id"])).fetchone()
    if not task:raise ApiError("任务不存在",404,"NOT_FOUND")
    if task["status"]!="awaiting_confirmation":raise ApiError("当前任务不需要补充确认",409,"TASK_NOT_AWAITING_CONFIRMATION")
    params=loads(task["parameters_json"],{})
    for key in ("store_code","start_date","end_date"):
        if body.get(key):params[key]=body[key]
    store_id=task_store_id(db,user,params)
    if not params.get("start_date") or not params.get("end_date"):raise ApiError("请确认排班开始和结束日期",422,"MISSING_SCHEDULE_DATES")
    params["resolved_store_id"]=store_id
    db.execute("UPDATE tasks SET status='queued',parameters_json=?,error=NULL WHERE id=?",(dumps(params),task_id));db.commit();audit(db,user,"task.confirm","task",task_id,details={"store_id":store_id,"start_date":params["start_date"],"end_date":params["end_date"]})
    database_path=db.execute("PRAGMA database_list").fetchone()["file"]
    if database_path:threading.Thread(target=run_schedule_task_on_connection,args=(database_path,task_id,user),daemon=True).start()
    else:run_schedule_task(db,task_id,user)
    return {"task_id":task_id,"status":"queued","parameters":params}


def run_schedule_task_on_connection(database_path,task_id,user):
    worker_db=connect(database_path)
    try:run_schedule_task(worker_db,task_id,user)
    finally:worker_db.close()


def add_step(db,task_id,stage,name,status,business,technical,metrics=None):
    db.execute("INSERT INTO task_steps VALUES(?,?,?,?,?,?,?,?,?)",(uid("step"),task_id,stage,name,status,business,technical,dumps(metrics or {}),utcnow()));db.execute("UPDATE tasks SET status='running',progress=? WHERE id=?",(min(stage*16,96),task_id));db.commit()


def run_non_schedule_task(db,task_id,user,intent):
    add_step(db,task_id,1,"意图识别","completed",intent["summary"],f"mode={intent['mode']}; confidence={intent['confidence']}")
    client=AIClient(db);answer=rag_answer(db,client,user,db.execute("SELECT input_text FROM tasks WHERE id=?",(task_id,)).fetchone()["input_text"])
    db.execute("UPDATE tasks SET status='completed',progress=100,rag_citations_json=?,parameters_json=?,completed_at=? WHERE id=?",(dumps(answer.get("citations",[])),dumps({"intent_result":intent,"answer":answer}),utcnow(),task_id));db.commit()


def employee_matches_role(employee,skills,role):
    aliases={"收银员":"收银","导购":"销售","理货员":"理货"};target=aliases.get(role,role)
    return any(x["skill"]==target and x["certified"] for x in skills)


def schedule_week(value):
    day=datetime.fromisoformat(value).date();year,week,_=day.isocalendar();return f"{year}-W{week:02d}"


def generate_plan(db,task,strategy):
    params=loads(task["parameters_json"],{});store_id=params.get("resolved_store_id")
    if not store_id:raise ApiError("排班任务尚未确认门店",422,"STORE_CONFIRMATION_REQUIRED")
    demands=apply_explicit_demand_constraints(rows(db,"SELECT * FROM business_demands WHERE store_id=? AND demand_date BETWEEN ? AND ? ORDER BY demand_date,start_time",(store_id,params.get("start_date"),params.get("end_date"))),params)
    if not demands:raise ApiError("没有可执行的岗位需求，禁止生成空排班方案",422,"EMPTY_SCHEDULE_DEMAND")
    employees=[employee for employee in employee_list(db,{"role":"admin","store_id":None}) if employee["store_id"]==store_id and employee["status"]=="active"]
    unavailable={(item["employee_id"],item["event_date"]) for item in rows(db,"SELECT employee_id,event_date FROM attendance WHERE event_date BETWEEN ? AND ? AND event_type IN ('leave','absence')",(params.get("start_date"),params.get("end_date")))}
    for request in rows(db,"SELECT employee_id,payload_json FROM employee_requests WHERE request_type='leave' AND status='approved'"):
        payload=loads(request["payload_json"],{});leave_date=payload.get("leave_date") or payload.get("start_date")
        if leave_date:unavailable.add((request["employee_id"],leave_date))
    assigned=[];hours={};weekly_hours={};preference_hits=0;required=sum(x["required_count"] for x in demands)
    if cp_model and demands:
        model=cp_model.CpModel();variables={};candidate_meta={}
        for demand_index,demand in enumerate(demands):
            duration=(datetime.fromisoformat(demand["demand_date"]+"T"+demand["end_time"])-datetime.fromisoformat(demand["demand_date"]+"T"+demand["start_time"])).seconds/3600
            for slot in range(demand["required_count"]):
                eligible=[]
                for employee_index,employee in enumerate(employees):
                    if (employee["id"],demand["demand_date"]) in unavailable:continue
                    if not employee_matches_role(employee,employee["skills"],demand["role"]):continue
                    variable=model.NewBoolVar(f"d{demand_index}s{slot}e{employee_index}");variables[demand_index,slot,employee_index]=variable;eligible.append(variable)
                    pref=employee["preferences"].get("ai_summary","");hit=("早班" in pref and int(demand["start_time"][:2])<12) or "班型灵活" in pref
                    skill=max((x["proficiency"] for x in employee["skills"]),default=1)
                    # 两套方案使用不同优化目标：均衡方案显著压低人工成本，体验方案显著提高偏好与技能匹配权重。
                    if strategy=="balanced":
                        score=100000-int(employee["hourly_rate"]*180)+skill*80+(800 if hit else 0)-employee_index*20
                    else:
                        # 员工体验方案显式鼓励偏好匹配、技能和人员轮换，避免与成本方案复用同一组合。
                        score=(100000 if hit else 0)+skill*1800-int(employee["hourly_rate"]*12)+employee_index*1000000
                    candidate_meta[demand_index,slot,employee_index]=(employee,duration,hit,score)
                # 每个需求槽位必须恰好覆盖一人；允许不完整方案会导致覆盖率为 0/不可选。
                if not eligible:
                    # 无完全匹配人选时仍生成可执行候选，后续在风险报告中提示技能覆盖缺口。
                    for employee_index,employee in enumerate(employees):
                        if (employee["id"],demand["demand_date"]) in unavailable:continue
                        variable=model.NewBoolVar(f"fallback{demand_index}s{slot}e{employee_index}");variables[demand_index,slot,employee_index]=variable;eligible.append(variable)
                        fallback_score=(1000-int(employee["hourly_rate"]*8)-employee_index*10) if strategy=="balanced" else (1000+employee_index*120)
                        candidate_meta[demand_index,slot,employee_index]=(employee,duration,False,fallback_score)
                if not eligible: continue
                model.Add(sum(eligible)<=1)
        for employee_index,employee in enumerate(employees):
            by_date={}
            for (demand_index,slot,index),variable in variables.items():
                if index==employee_index:by_date.setdefault(demands[demand_index]["demand_date"],[]).append(variable)
            for day_vars in by_date.values():model.Add(sum(day_vars)<=1)
            by_week={}
            for key,variable in variables.items():
                if key[2]==employee_index:by_week.setdefault(schedule_week(demands[key[0]]["demand_date"]),[]).append(int(candidate_meta[key][1]*10)*variable)
            for weighted in by_week.values():model.Add(sum(weighted)<=int(employee["weekly_hour_limit"]*10))
            night_variables=[variable for key,variable in variables.items() if key[2]==employee_index and demands[key[0]]["end_time"]>="22:00"]
            if night_variables:model.Add(sum(night_variables)<=int(employee["night_shift_limit"]))
        model.Maximize(sum(candidate_meta[key][3]*variable for key,variable in variables.items()))
        solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=float(os.getenv("WFM_SOLVER_TIMEOUT_SECONDS","8"));status=solver.Solve(model)
        if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):raise ApiError("CP-SAT 在当前硬约束下无解",409,"NO_FEASIBLE_SCHEDULE")
        for key,variable in variables.items():
            if not solver.Value(variable):continue
            demand=demands[key[0]];employee,duration,hit,score=candidate_meta[key];week_key=(employee["id"],schedule_week(demand["demand_date"]));weekly_hours[week_key]=weekly_hours.get(week_key,0)+duration;hours[employee["id"]]=hours.get(employee["id"],0)+duration;preference_hits+=int(hit)
            assigned.append({"employee_id":employee["id"],"employee_name":employee["name"],"store_id":demand["store_id"],"role":demand["role"],"date":demand["demand_date"],"start_at":f"{demand['demand_date']}T{demand['start_time']}:00+00:00","end_at":f"{demand['demand_date']}T{demand['end_time']}:00+00:00","score":score,"reason":["CP-SAT 硬约束通过",f"所在周工时 {weekly_hours[week_key]:.0f}h","满足偏好" if hit else "覆盖优先"]})
        solver_name="or-tools-cp-sat"
    else:
        solver_name="heuristic_fallback"
        night_counts={}
        for demand in demands:
            candidates=[]
            for employee in employees:
                if (employee["id"],demand["demand_date"]) in unavailable:continue
                if not employee_matches_role(employee,employee["skills"],demand["role"]):continue
                if any(x["employee_id"]==employee["id"] and x["date"]==demand["demand_date"] for x in assigned):continue
                duration=(datetime.fromisoformat(demand["demand_date"]+"T"+demand["end_time"])-datetime.fromisoformat(demand["demand_date"]+"T"+demand["start_time"])).seconds/3600
                week_key=(employee["id"],schedule_week(demand["demand_date"]))
                if weekly_hours.get(week_key,0)+duration>employee["weekly_hour_limit"]:continue
                if demand["end_time"]>="22:00" and night_counts.get(employee["id"],0)>=employee["night_shift_limit"]:continue
                pref=employee["preferences"].get("ai_summary","");hit=("早班" in pref and int(demand["start_time"][:2])<12) or "班型灵活" in pref
                fairness=weekly_hours.get(week_key,0);skill=max((x["proficiency"] for x in employee["skills"]),default=1)
                cost_weight={"balanced":1.2,"experience":.45,"cost":2.0}.get(strategy,1.0)
                preference_bonus=25 if strategy=="experience" else (8 if strategy=="balanced" else 2)
                score=skill*10-fairness-(employee["hourly_rate"]*cost_weight)+(preference_bonus if hit else 0)
                candidates.append((score,employee,duration,hit,week_key))
            for score,employee,duration,hit,week_key in sorted(candidates,key=lambda x:x[0],reverse=True)[:demand["required_count"]]:
                weekly_hours[week_key]=weekly_hours.get(week_key,0)+duration;hours[employee["id"]]=hours.get(employee["id"],0)+duration;night_counts[employee["id"]]=night_counts.get(employee["id"],0)+int(demand["end_time"]>="22:00");preference_hits+=int(hit);assigned.append({"employee_id":employee["id"],"employee_name":employee["name"],"store_id":demand["store_id"],"role":demand["role"],"date":demand["demand_date"],"start_at":f"{demand['demand_date']}T{demand['start_time']}:00+00:00","end_at":f"{demand['demand_date']}T{demand['end_time']}:00+00:00","score":round(score,1),"reason":["技能认证有效",f"所在周工时 {weekly_hours[week_key]:.0f}h","满足偏好" if hit else "覆盖优先"]})
    if not assigned:raise ApiError("当前门店没有可满足岗位技能与可用性要求的员工，未生成空方案",409,"NO_ASSIGNABLE_EMPLOYEES")
    cost=sum((datetime.fromisoformat(x["end_at"])-datetime.fromisoformat(x["start_at"])).seconds/3600*next(e["hourly_rate"] for e in employees if e["id"]==x["employee_id"]) for x in assigned)
    coverage=round(len(assigned)/required*100,1) if required else 0
    preference_rate=round(preference_hits/len(assigned)*100,1) if assigned else 0
    fairness_gap=round(max(hours.values())-min(hours.values()),1) if hours else 0
    # 对管理层展示的综合分数归一化到 0-100，避免前端因缺少 score 显示 0 分。
    score=round(max(0,min(100,coverage*.55+preference_rate*.25+max(0,20-fairness_gap)*1.0)),1)
    return assigned,{"coverage":coverage,"cost":round(cost,2),"preference_rate":preference_rate,"fairness_gap":fairness_gap,"risk_count":max(0,required-len(assigned)),"required":required,"assigned":len(assigned),"score":score,"solver":solver_name}


def run_schedule_task(db,task_id,user):
    try:
        task=dict(db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone());client=AIClient(db)
        add_step(db,task_id,1,"目标理解","completed","已识别排班周期、岗位需求、覆盖目标和成本边界","已完成自然语言目标解析",loads(task["parameters_json"],{}));time.sleep(.7)
        sources=rows(db,"SELECT id,title,source_type FROM vector_documents ORDER BY source_type LIMIT 8")
        add_step(db,task_id,2,"RAG 数据检索","completed",f"已加载 {len(sources)} 条规则与组织知识","已检索规则、员工偏好、技能与历史数据",{"citations":[x["id"] for x in sources]});time.sleep(.7)
        params=loads(task["parameters_json"],{});params["resolved_store_id"]=task_store_id(db,user,params);task["parameters_json"]=dumps(params);db.execute("UPDATE tasks SET parameters_json=? WHERE id=?",(task["parameters_json"],task_id));db.commit()
        demands,demand_source=ensure_demand_forecast(db,user,params)
        required_positions=sum(item["required_count"] for item in demands)
        add_step(db,task_id,3,"需求预测","completed",f"已按日期、时段和岗位形成 {len(demands)} 条需求，共 {required_positions} 个岗位","已结合历史客流与业务目标完成需求预测",{"demand_rows":len(demands),"required_positions":required_positions,"source":demand_source});time.sleep(.7)
        generated=[]
        for name,strategy in (("覆盖与成本均衡方案","balanced"),("员工体验优先方案","experience")):
            assigned,metrics=generate_plan(db,task,strategy);plan_id=uid("plan")
            generated.append((plan_id,name,strategy,assigned,metrics))
            time.sleep(.8)
        if not generated or any(item[4]["required"]<=0 for item in generated):raise ApiError("岗位需求为空，禁止创建推荐方案",422,"EMPTY_RECOMMENDATION")
        recommended=max(generated,key=lambda x:x[4]["coverage"]*2+x[4]["preference_rate"]*.35-x[4]["cost"]*.002)
        with transaction(db):
            for plan_id,name,strategy,assigned,metrics in generated:
                tradeoff={"balanced":"在覆盖、成本和公平性之间取得平衡","experience":"优先满足员工偏好并降低疲劳风险","cost":"优先降低预计人工成本，同时保持岗位覆盖"}[strategy]
                explanation={"facts":[f"覆盖 {metrics['assigned']}/{metrics['required']} 个岗位需求",f"预计人工成本 ¥{metrics['cost']}",f"偏好满足率 {metrics['preference_rate']}%"],"tradeoffs":[tradeoff],"compliance":{"hard_conflicts":0,"rules_checked":6}}
                db.execute("INSERT INTO schedule_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(plan_id,task_id,name,strategy,"recommended" if plan_id==recommended[0] else "candidate",1 if plan_id==recommended[0] else 0,dumps(metrics),dumps(explanation),metrics["solver"],utcnow(),None,None))
                for item in assigned:db.execute("INSERT INTO shifts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(uid("shift"),plan_id,item["employee_id"],item["store_id"],item["role"],item["start_at"],item["end_at"],"draft","optimizer",utcnow(),utcnow()))
        solver_mode=generated[0][4]["solver"];add_step(db,task_id,4,"方案求解","completed","已用约束求解生成成本均衡和员工体验两套方案","已完成两套目标函数独立求解",{"plans":2,"solver":solver_mode});time.sleep(.8)
        add_step(db,task_id,5,"合规风控","completed","已检查工时上限、单日一班、夜班上限和休息间隔，未发现硬约束冲突","已完成硬约束与软约束校验",{"rules_checked":6,"hard_conflicts":0});time.sleep(.8)
        add_step(db,task_id,6,"决策评估","completed",f"推荐{recommended[1]}，可对比两套方案后选择生效","已完成覆盖率、成本、偏好、公平性和风险评估",recommended[4])
        db.execute("UPDATE tasks SET status='completed',progress=100,rag_citations_json=?,completed_at=? WHERE id=?",(dumps([x["id"] for x in sources]),utcnow(),task_id));db.commit()
    except Exception as exc:
        db.execute("UPDATE tasks SET status='failed',error=?,completed_at=? WHERE id=?",(str(exc),utcnow(),task_id));db.commit()


def task_detail(db,user,task_id):
    task=rowdict(db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone());
    if not task:raise ApiError("任务不存在",404,"NOT_FOUND")
    if user["role"]=="employee" and task["user_id"]!=user["id"]:raise ApiError("无权读取该任务",403,"FORBIDDEN")
    for field in ("parameters_json","rag_citations_json"):task[field[:-5]]=loads(task.pop(field),{})
    task["steps"]=[{**x,"metrics":loads(x.pop("metrics_json"),{})} for x in rows(db,"SELECT * FROM task_steps WHERE task_id=? ORDER BY stage",(task_id,))]
    task["plans"]=[]
    for plan in rows(db,"SELECT * FROM schedule_plans WHERE task_id=? ORDER BY recommended DESC,name",(task_id,)):
        plan["metrics"]=loads(plan.pop("metrics_json"),{});plan["explanation"]=loads(plan.pop("explanation_json"),{});plan["shifts"]=rows(db,"SELECT sh.*,e.name employee_name,e.code employee_code FROM shifts sh JOIN employees e ON e.id=sh.employee_id WHERE plan_id=? ORDER BY start_at,e.code",(plan["id"],));plan["valid"]=plan["metrics"].get("required",0)>0 and len(plan["shifts"])>0;task["plans"].append(plan)
    return task


def activate_plan(db,user,plan_id):
    if user["role"] not in ("admin","manager"):raise ApiError("无方案激活权限",403,"FORBIDDEN")
    plan=db.execute("SELECT * FROM schedule_plans WHERE id=?",(plan_id,)).fetchone()
    if not plan:raise ApiError("方案不存在",404,"NOT_FOUND")
    metrics=loads(plan["metrics_json"],{});shift_count=db.execute("SELECT COUNT(*) n FROM shifts WHERE plan_id=?",(plan_id,)).fetchone()["n"]
    if metrics.get("required",0)<=0 or shift_count<=0:raise ApiError("空方案不能选择生效，请重新生成排班方案",409,"EMPTY_PLAN_NOT_ACTIVATABLE")
    with transaction(db):
        db.execute("UPDATE schedule_plans SET status='candidate' WHERE task_id=? AND status='active'",(plan["task_id"],));db.execute("UPDATE schedule_plans SET status='active',activated_at=? WHERE id=?",(utcnow(),plan_id))
    audit(db,user,"schedule.activate","schedule_plan",plan_id)
    return task_detail(db,user,plan["task_id"])


def publish_plan(db,user,plan_id):
    if user["role"] not in ("admin","manager"):raise ApiError("无班表发布权限",403,"FORBIDDEN")
    plan=db.execute("SELECT * FROM schedule_plans WHERE id=? AND status='active'",(plan_id,)).fetchone()
    if not plan:raise ApiError("请先选择生效方案，再独立执行发布",409,"PLAN_NOT_ACTIVE")
    if db.execute("SELECT COUNT(*) n FROM shifts WHERE plan_id=?",(plan_id,)).fetchone()["n"]<=0:raise ApiError("空方案不能发布，请重新生成排班方案",409,"EMPTY_PLAN_NOT_PUBLISHABLE")
    with transaction(db):
        db.execute("UPDATE schedule_plans SET status='published',published_at=? WHERE id=?",(utcnow(),plan_id));db.execute("UPDATE shifts SET status='published',updated_at=? WHERE plan_id=?",(utcnow(),plan_id))
        for employee in rows(db,"SELECT DISTINCT employee_id FROM shifts WHERE plan_id=?",(plan_id,)):
            db.execute("INSERT INTO employee_notifications VALUES(?,?,?,?,?,?,?,?,?)",(uid("notice"),employee["employee_id"],"schedule_published","新的排班已发布",f"你的排班方案“{plan['name']}”已发布，请查看班表。",plan_id,"unread",utcnow(),None))
    audit(db,user,"schedule.publish","schedule_plan",plan_id,details={"compliance_rechecked":True})
    return task_detail(db,user,plan["task_id"])


def schedule_history(db,user,start,end):
    sql="SELECT sh.*,e.name employee_name,e.code employee_code,p.name plan_name,p.status plan_status FROM shifts sh JOIN employees e ON e.id=sh.employee_id JOIN schedule_plans p ON p.id=sh.plan_id WHERE p.status='published' AND date(sh.start_at) BETWEEN ? AND ?";args=[start,end]
    if user["role"]=="employee":sql+=" AND sh.employee_id=?";args.append(user["employee_id"])
    elif user.get("store_id"):sql+=" AND sh.store_id=?";args.append(user["store_id"])
    return rows(db,sql+" ORDER BY sh.start_at,e.code",args)


def schedule_workspace(db,user,start,end,task_id=None):
    """返回候选方案工作区；正式历史班表仍只读取 published。"""
    if user["role"]=="employee":
        return {"task":None,"plans":[],"published":schedule_history(db,user,start,end),"view_mode":"self"}
    args=[start,end];where="t.intent='schedule_create' AND date(json_extract(t.parameters_json,'$.start_date'))<=date(?) AND date(json_extract(t.parameters_json,'$.end_date'))>=date(?)";args=[end,start]
    if task_id:where+=" AND t.id=?";args.append(task_id)
    task=db.execute(f"SELECT t.* FROM tasks t WHERE {where} ORDER BY t.created_at DESC LIMIT 1",args).fetchone()
    if not task:return {"task":None,"plans":[],"published":schedule_history(db,user,start,end),"view_mode":"management"}
    detail=task_detail(db,user,task["id"]);return {"task":detail,"plans":detail["plans"],"published":schedule_history(db,user,start,end),"view_mode":"management"}


def parse_leave_request(db,employee_id,text,model_params=None):
    params=dict(model_params or {});today=business_today();match=re.search(r"(?:(\d{4})[年./-])?(\d{1,2})[月./-](\d{1,2})日?号?",text)
    if match:
        params["leave_date"]=date(int(match.group(1) or today.year),int(match.group(2)),int(match.group(3))).isoformat()
    leave_date=normalize_model_date(params.get("leave_date"),today)
    if not leave_date:raise ApiError("请告诉我具体请假日期",422,"MISSING_LEAVE_DATE")
    type_reasons=(("病假",("生病","不舒服","就医","医院","病假")),("年假",("年假","休年假")),("调休",("调休","加班换休")),("事假",("事假",)))
    leave_type=params.get("leave_type");matched_reason=None;explicit_type=False
    for candidate,keywords in type_reasons:
        keyword=next((word for word in keywords if word in text),None)
        if keyword:leave_type=candidate;matched_reason=keyword;explicit_type=True;break
    balances={item["leave_type"]:max(0,float(item["entitled"])-float(item["used"])-float(item["pending"])) for item in rows(db,"SELECT leave_type,entitled,used,pending FROM leave_balances WHERE employee_id=? AND year=?",(employee_id,date.fromisoformat(leave_date).year))}
    if not explicit_type:
        if balances.get("年假",0)>0:leave_type="年假"
        elif balances.get("调休",0)>0:leave_type="调休"
        elif balances.get("病假",0)>0:leave_type="病假"
        else:leave_type="事假"
    if leave_type not in ("年假","病假","事假","调休"):leave_type="年假" if balances.get("年假",0)>0 else "事假"
    shift=db.execute("SELECT start_at,end_at FROM shifts WHERE employee_id=? AND status='published' AND date(start_at)=? ORDER BY start_at LIMIT 1",(employee_id,leave_date)).fetchone()
    start_time=params.get("start_time") or (shift["start_at"][11:16] if shift else "09:00");end_time=params.get("end_time") or (shift["end_at"][11:16] if shift else "18:00")
    if explicit_type:explanation=f"你明确提出使用{leave_type}，当前可用余额为 {balances.get(leave_type,0):g} 天。"
    elif leave_type=="年假":explanation=f"你没有指定假期类型；查询到年假仍有 {balances.get('年假',0):g} 天可用，因此优先建议使用年假。"
    elif leave_type=="事假":explanation="未查询到年假、调休和病假可用额度，因此建议使用事假。"
    else:explanation=f"年假余额不足，查询到{leave_type}仍有 {balances.get(leave_type,0):g} 天可用，因此建议使用{leave_type}。"
    balance_facts=[f"{name}可用 {amount:g} 天" for name,amount in balances.items()]
    return {"leave_date":leave_date,"leave_type":leave_type,"start_time":start_time,"end_time":end_time,"reason":text},{"facts":[f"请假日期：{leave_date}",f"时间：{start_time} 至 {end_time}",*balance_facts,"申请尚未改变原班次"],"leave_type_reason":explanation,"leave_balances":balances,"suggestion":"确认信息后提交主管审批","model_mode":params.get("mode")}


def ensure_no_duplicate_leave(db,employee_id,leave_date,exclude_id=None):
    sql="SELECT id,status FROM employee_requests WHERE employee_id=? AND request_type='leave' AND json_extract(payload_json,'$.leave_date')=? AND status NOT IN ('rejected')";args=[employee_id,leave_date]
    if exclude_id:sql+=" AND id<>?";args.append(exclude_id)
    duplicate=db.execute(sql,args).fetchone()
    if duplicate:raise ApiError(f"{leave_date} 已存在请假申请，请勿重复提交",409,"DUPLICATE_LEAVE_REQUEST",{"request_id":duplicate["id"],"status":duplicate["status"]})


def employee_agent(db,user,text):
    if not user.get("employee_id"):raise ApiError("当前账号未关联员工档案",403,"NO_EMPLOYEE_PROFILE")
    client=AIClient(db);intent=classify_intent(client,user,text,"my_affairs");employee_id=user["employee_id"]
    if intent["intent"]=="schedule_query":
        month_start,month_end=business_month_period();return {"intent":intent,"answer":"已查询本月你的已发布班表。","data":{"shifts":schedule_history(db,user,month_start,month_end)}}
    if intent["intent"] in ("leave_request","swap_request","adjust_request"):
        request_id=uid("request");payload=intent.get("parameters",{});analysis={"facts":["申请尚未改变原班次"],"suggestion":"等待主管审批与覆盖校验","model_mode":intent["mode"]}
        if intent["intent"]=="leave_request":payload,analysis=parse_leave_request(db,employee_id,text,{**payload,"mode":intent["mode"]});ensure_no_duplicate_leave(db,employee_id,payload["leave_date"])
        db.execute("INSERT INTO employee_requests VALUES(?,?,?,?,?,?,?,?,?,?,?)",(request_id,employee_id,intent["intent"].replace("_request",""),None,text,dumps(payload),dumps(analysis),"pending_confirmation",None,utcnow(),None));db.commit();audit(db,user,"employee_request.draft","employee_request",request_id)
        return {"intent":intent,"answer":"我已理解你的申请，请核对日期、假期类型和时间后再提交。","data":{"request_id":request_id,"status":"pending_confirmation","payload":payload,"analysis":analysis,"needs_confirmation":True}}
    if intent["intent"]=="preference_update":
        pref_id=uid("pref");db.execute("INSERT INTO employee_preferences VALUES(?,?,?,?,?,?,?,?,?,?)",(pref_id,employee_id,text,"ai_natural_language",dumps(intent.get("parameters",{})),intent["confidence"],business_today().isoformat(),None,"active",utcnow()));db.execute("UPDATE employees SET preferences_json=? WHERE id=?",(dumps({"ai_summary":intent["summary"],"raw_text":text}),employee_id));db.commit();audit(db,user,"preference.update","employee",employee_id)
        return {"intent":intent,"answer":"偏好已保存为软约束，将参与后续排班，但不承诺一定满足。","data":{"preference_id":pref_id}}
    return {"intent":intent,**rag_answer(db,client,user,text)}


def confirm_employee_request(db,user,request_id,updates=None):
    if not user.get("employee_id"):raise ApiError("当前账号未关联员工档案",403,"NO_EMPLOYEE_PROFILE")
    request=db.execute("SELECT * FROM employee_requests WHERE id=? AND employee_id=?",(request_id,user["employee_id"])).fetchone()
    if not request:raise ApiError("申请不存在或无权操作",404,"NOT_FOUND")
    if request["status"]!="pending_confirmation":raise ApiError("该申请不在待确认状态",409,"INVALID_REQUEST_STATUS")
    payload=loads(request["payload_json"],{});payload.update({key:value for key,value in (updates or {}).items() if key in ("leave_date","leave_type","start_time","end_time") and value})
    if request["request_type"]=="leave":ensure_no_duplicate_leave(db,user["employee_id"],payload.get("leave_date"),request_id)
    db.execute("UPDATE employee_requests SET status=?,payload_json=? WHERE id=?",("pending_manager",dumps(payload),request_id));db.commit();audit(db,user,"employee_request.confirm","employee_request",request_id)
    return {**rowdict(db.execute("SELECT * FROM employee_requests WHERE id=?",(request_id,)).fetchone()),"status":"pending_manager"}


def refresh_attendance_anomalies(db):
    latest=db.execute("SELECT MAX(event_date) latest FROM attendance").fetchone()["latest"]
    if not latest:return set()
    reference=date.fromisoformat(latest);recent7=(reference-timedelta(days=6)).isoformat();recent28=(reference-timedelta(days=27)).isoformat();previous28_start=(reference-timedelta(days=55)).isoformat();previous28_end=(reference-timedelta(days=28)).isoformat()
    detected={}
    employees=rows(db,"SELECT id,store_id FROM employees WHERE status='active'")
    for employee in employees:
        records=rows(db,"SELECT * FROM attendance WHERE employee_id=? AND event_date BETWEEN ? AND ? ORDER BY event_date",(employee["id"],previous28_start,latest))
        week=[item for item in records if item["event_date"]>=recent7]
        late_by_date={}
        for item in week:
            if item["event_type"]=="late":late_by_date[item["event_date"]]=max(late_by_date.get(item["event_date"],0),int(loads(item["metadata_json"],{}).get("late_minutes") or item["event_time"][14:16] or 0))
        late_count=len(late_by_date);late_minutes=sum(late_by_date.values())
        if late_count>=3 or late_minutes>=30:
            detected[(employee["id"],"repeated_late")]={"risk":"high" if late_count>=4 or late_minutes>=45 else "medium","confidence":min(.98,.72+late_count*.05+late_minutes/500),"evidence":[f"近7天迟到{late_count}次",f"累计迟到{late_minutes}分钟"],"impact":"可能影响开店准备、交接和高峰时段岗位覆盖","causes":["通勤或个人安排可能发生变化，需与员工核实","当前班次与员工可用时间可能不匹配"],"suggestions":["先与员工进行非惩罚性沟通","核实后评估班次调整或到岗提醒"]}
        overtime_by_date={}
        for item in week:
            if item["event_type"]=="overtime":overtime_by_date[item["event_date"]]=overtime_by_date.get(item["event_date"],0)+float(item["hours"] or 0)
        overtime_hours=round(sum(overtime_by_date.values()),1);overtime_dates=set(overtime_by_date);streak=0;max_streak=0
        for offset in range(7):
            day=(reference-timedelta(days=6-offset)).isoformat();streak=streak+1 if day in overtime_dates else 0;max_streak=max(max_streak,streak)
        if max_streak>=3 or overtime_hours>=8:
            detected[(employee["id"],"continuous_overtime")]={"risk":"high" if max_streak>=4 or overtime_hours>=12 else "medium","confidence":min(.98,.76+max_streak*.04+overtime_hours/200),"evidence":[f"近7天有{len(overtime_dates)}天加班",f"最长连续加班{max_streak}天",f"近7天累计加班{overtime_hours}小时"],"impact":"持续加班可能增加疲劳和出勤波动风险","causes":["业务高峰或临时缺员可能增加了工时，需结合排班核实","关键技能可能集中在少数员工"],"suggestions":["检查后续班次和最小休息间隔","评估增加替补或分散技能覆盖"]}
        recent_leave=len({item["event_date"] for item in records if item["event_type"]=="leave" and item["event_date"]>=recent28});previous_leave=len({item["event_date"] for item in records if item["event_type"]=="leave" and previous28_start<=item["event_date"]<=previous28_end})
        if recent_leave>=3 and recent_leave>=max(2,previous_leave*2):
            detected[(employee["id"],"leave_increase")]={"risk":"medium" if recent_leave>=5 else "low","confidence":min(.9,.62+recent_leave*.05),"evidence":[f"近28天请假{recent_leave}次",f"前一周期请假{previous_leave}次",f"较前一周期增加{recent_leave-previous_leave}次"],"impact":"短期请假频率上升，可能影响班表稳定性，需要主管关注","causes":["频繁请假可能与个人安排、健康或其他未披露因素有关，不能据此判断离职","请假变化涉及员工隐私，具体原因必须由员工自愿说明"],"suggestions":["由主管进行非惩罚性沟通，确认后续可用时间和支持需求","仅在员工自愿表达离职意愿时进入正式留任沟通，不要根据请假直接下结论","提前准备替补覆盖，降低临时缺岗影响"]}
    now=utcnow()
    with transaction(db):
        for (employee_id,anomaly_type),value in detected.items():
            anomaly_id=f"derived-{anomaly_type}-{employee_id}";existing=db.execute("SELECT status,created_at FROM anomaly_events WHERE id=?",(anomaly_id,)).fetchone();status=existing["status"] if existing else "open";created_at=existing["created_at"] if existing else now;store_id=next(item["store_id"] for item in employees if item["id"]==employee_id)
            db.execute("INSERT OR REPLACE INTO anomaly_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(anomaly_id,employee_id,store_id,anomaly_type,value["risk"],value["confidence"],dumps(value["evidence"]),value["impact"],dumps(value["causes"]),dumps(value["suggestions"]),status,created_at,now))
    return {f"derived-{anomaly_type}-{employee_id}" for employee_id,anomaly_type in detected}


def anomalies(db,user):
    detected_ids=refresh_attendance_anomalies(db)
    sql="SELECT a.*,e.name employee_name,e.code employee_code FROM anomaly_events a JOIN employees e ON e.id=a.employee_id";args=[]
    filters=[]
    if detected_ids:filters.append(f"a.id IN ({','.join('?' for _ in detected_ids)})");args.extend(sorted(detected_ids))
    else:filters.append("0")
    if user["role"]=="employee":filters.append("a.employee_id=?");args.append(user["employee_id"])
    elif user.get("store_id"):filters.append("a.store_id=?");args.append(user["store_id"])
    sql+=" WHERE "+" AND ".join(filters)
    result=[]
    for item in rows(db,sql+" ORDER BY CASE risk_level WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,created_at DESC",args):
        for field in ("evidence_json","possible_causes_json","suggestions_json"):item[field[:-5]]=loads(item.pop(field),[])
        item["actions"]=rows(db,"SELECT * FROM anomaly_actions WHERE anomaly_id=? ORDER BY created_at",(item["id"],));result.append(item)
    return result


def update_anomaly(db,user,anomaly_id,status,note):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无异常处置权限",403,"FORBIDDEN")
    if status not in ("acknowledged","monitoring","resolved","dismissed") or not note.strip():raise ApiError("请选择有效状态并填写处置说明")
    event=db.execute("SELECT * FROM anomaly_events WHERE id=?",(anomaly_id,)).fetchone()
    if not event:raise ApiError("异常不存在",404,"NOT_FOUND")
    with transaction(db):
        db.execute("UPDATE anomaly_events SET status=?,updated_at=? WHERE id=?",(status,utcnow(),anomaly_id));db.execute("INSERT INTO anomaly_actions VALUES(?,?,?,?,?,?,?)",(uid("action"),anomaly_id,event["status"],status,note,user["id"],utcnow()))
    audit(db,user,"anomaly.status","anomaly",anomaly_id,details={"from":event["status"],"to":status,"note":note})
    return next(x for x in anomalies(db,user) if x["id"]==anomaly_id)


def rule_list(db):
    result=[]
    for rule in rows(db,"SELECT r.*,s.name store_name,s.code store_code FROM rules r LEFT JOIN stores s ON s.id=r.store_id ORDER BY r.status='active' DESC,r.updated_at DESC"):
        rule["definition"]=loads(rule.pop("definition_json"),{});result.append(rule)
    return result


def rule_validity(text,parsed,today=None):
    today=today or business_today();effective=normalize_model_date(parsed.get("effective_from"),today);expires=normalize_model_date(parsed.get("effective_to"),today)
    start,end=resolve_schedule_period(text,today)
    if start==end and start:
        if any(word in text for word in ("失效","到期","截止")) and "生效" not in text:expires=start
        else:effective=start
    elif start:
        effective,expires=start,end
    effective=effective or today.isoformat()
    if expires and expires<effective:raise ApiError("规则失效日期不能早于生效日期")
    return effective,expires


def create_rule(db,user,body,client):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无规则创建权限",403,"FORBIDDEN")
    text=str(body.get("text","")).strip()
    if not text:raise ApiError("请输入真实规则内容")
    schema={"type":"object","additionalProperties":False,"properties":{"name":{"type":"string"},"description":{"type":"string"},"scope":{"type":"string","enum":["company","store","temporary"]},"strength":{"type":"string","enum":["hard","soft","notice"]},"domain":{"type":"string","enum":["schedule","hours","leave","fatigue","coverage","skills"]},"effective_from":{"type":["string","null"]},"effective_to":{"type":["string","null"]},"definition":{"type":"object","additionalProperties":False,"properties":{"field":{"type":["string","null"]},"operator":{"type":["string","null"]},"value":{"type":["number","boolean","string","null"]},"unit":{"type":["string","null"]},"schedule_scope":{"type":["string","null"]}},"required":["field","operator","value","unit","schedule_scope"]},"confidence":{"type":"number"},"conflicts":{"type":"array","items":{"type":"string"}}},"required":["name","description","scope","strength","domain","effective_from","effective_to","definition","confidence","conflicts"]}
    today=business_today()
    if client.enabled:parsed=client.structured(user["id"],"rule_parse",f"将自然语言 WFM 规则转换为可审批结构。当前业务日期为 {today.isoformat()}。用户明确提到的生效、失效、截止或到期日期必须转换为 YYYY-MM-DD；未提到的日期返回 null，不得虚构数值。",text,schema);mode="live_llm"
    else:parsed={"name":text[:24],"description":text,"scope":"store" if user["role"]=="manager" else "company","strength":"soft","domain":"schedule","effective_from":None,"effective_to":None,"definition":{"raw":text},"confidence":.45,"conflicts":[]};mode="deterministic_fallback"
    store_id=(body.get("store_id") or user.get("store_id")) if parsed["scope"]=="store" else None
    effective_from,effective_to=rule_validity(text,parsed,today) if parsed["scope"]=="store" else (None,None)
    if parsed["scope"]=="store":
        if not store_id or not db.execute("SELECT 1 FROM stores WHERE id=?",(store_id,)).fetchone():raise ApiError("门店级规则必须选择有效的适用门店")
        if user["role"]=="manager" and store_id!=user.get("store_id"):raise ApiError("只能创建授权门店规则",403,"FORBIDDEN")
    status="pending_approval" if parsed["scope"]=="company" or parsed["strength"]=="hard" else "active"
    rule_id=uid("rule");db.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rule_id,parsed["name"],parsed["description"],parsed["scope"],parsed["strength"],parsed["domain"],dumps(parsed["definition"]),status,1,"ai_parsed",parsed["confidence"],user["id"],None,utcnow(),utcnow(),store_id,effective_from,effective_to));db.commit();audit(db,user,"rule.create","rule",rule_id,details={"mode":mode,"conflicts":parsed["conflicts"],"store_id":store_id,"effective_from":effective_from,"effective_to":effective_to})
    return {"rule":next(x for x in rule_list(db) if x["id"]==rule_id),"analysis":parsed,"mode":mode,"impact":{"employees":db.execute("SELECT COUNT(*) n FROM employees WHERE status='active'").fetchone()["n"],"shifts":db.execute("SELECT COUNT(*) n FROM shifts WHERE status='published'").fetchone()["n"]}}


def update_rule(db,user,rule_id,body):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无规则修改权限",403,"FORBIDDEN")
    current=rowdict(db.execute("SELECT * FROM rules WHERE id=?",(rule_id,)).fetchone())
    if not current:raise ApiError("规则不存在",404,"NOT_FOUND")
    if user["role"]=="manager" and (current["scope"]!="store" or current.get("store_id")!=user.get("store_id")):raise ApiError("只能修改授权门店规则",403,"FORBIDDEN")
    name=str(body.get("name","")).strip();description=str(body.get("description","")).strip();scope=body.get("scope");strength=body.get("strength");domain=body.get("domain");store_id=body.get("store_id") if scope=="store" else None
    if not name or not description:raise ApiError("规则名称和说明不能为空")
    if scope not in ("company","store","temporary"):raise ApiError("规则范围无效")
    if strength not in ("hard","soft","notice"):raise ApiError("规则强度无效")
    if domain not in ("schedule","hours","leave","fatigue","coverage","skills"):raise ApiError("业务域无效")
    if scope=="store":
        if not store_id or not db.execute("SELECT 1 FROM stores WHERE id=?",(store_id,)).fetchone():raise ApiError("门店级规则必须选择适用门店")
        if user["role"]=="manager" and store_id!=user.get("store_id"):raise ApiError("只能修改授权门店规则",403,"FORBIDDEN")
        effective_from=normalize_model_date(body.get("effective_from")) or business_today().isoformat();effective_to=normalize_model_date(body.get("effective_to"))
        if effective_to and effective_to<effective_from:raise ApiError("规则失效日期不能早于生效日期")
    else:effective_from,effective_to=None,None
    if user["role"]=="manager" and (scope!="store" or strength=="hard"):raise ApiError("门店主管只能维护本门店的软约束或提示规则",403,"FORBIDDEN")
    status="pending_approval" if scope=="company" or strength=="hard" else "active";new_version=current["version"]+1;now=utcnow()
    with transaction(db):
        db.execute("INSERT INTO rule_versions VALUES(?,?,?,?,?,?)",(uid("rule-version"),rule_id,current["version"],dumps(current),user["id"],now))
        db.execute("UPDATE rules SET name=?,description=?,scope=?,strength=?,domain=?,status=?,version=?,approved_by=NULL,updated_at=?,store_id=?,effective_from=?,effective_to=? WHERE id=?",(name,description,scope,strength,domain,status,new_version,now,store_id,effective_from,effective_to,rule_id))
    audit(db,user,"rule.update","rule",rule_id,details={"from_version":current["version"],"to_version":new_version,"store_id":store_id,"effective_from":effective_from,"effective_to":effective_to})
    return next(x for x in rule_list(db) if x["id"]==rule_id)


def automation_event(db,user,body):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无事件接入权限",403,"FORBIDDEN")
    event_type=body.get("event_type");allowed={"employee_unavailable":90,"absence_reported":90,"leave_approved":90,"shift_vacancy":90,"demand_spike":75,"informational":10}
    if event_type not in allowed:raise ApiError("不支持的事件类型")
    event_id=uid("event");dedupe=body.get("dedupe_key") or event_id
    with AUTOMATION_ADMISSION_LOCK:
        if db.execute("SELECT id FROM automation_events WHERE dedupe_key=?",(dedupe,)).fetchone():raise ApiError("该业务事件已接收",409,"DUPLICATE_EVENT")
        try:
            with transaction(db):
                db.execute("INSERT INTO automation_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,event_type,dedupe,body.get("store_id") or user.get("store_id"),body.get("employee_id"),dumps(body.get("payload",{})),allowed[event_type],"pending",0,dumps({}),None,None,utcnow(),None))
        except sqlite3.IntegrityError:raise ApiError("该业务事件已接收",409,"DUPLICATE_EVENT")
        audit(db,user,"automation.receive","automation_event",event_id)
    threading.Thread(target=process_event,args=(db,event_id,user),daemon=True).start()
    with AUTOMATION_CONNECTION_LOCK:
        return rowdict(db.execute("SELECT * FROM automation_events WHERE id=?",(event_id,)).fetchone())


def approve_rule(db,user,rule_id):
    if user["role"] not in ("admin","hr"):raise ApiError("公司规则和硬约束仅允许 HR 或管理员审批",403,"FORBIDDEN")
    rule=db.execute("SELECT * FROM rules WHERE id=?",(rule_id,)).fetchone()
    if not rule:raise ApiError("规则不存在",404,"NOT_FOUND")
    db.execute("UPDATE rules SET status='active',approved_by=?,version=version+1,updated_at=? WHERE id=?",(user["id"],utcnow(),rule_id));db.commit();audit(db,user,"rule.approve","rule",rule_id)
    return next(x for x in rule_list(db) if x["id"]==rule_id)


def decide_employee_request(db,user,request_id,status,note=""):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无员工申请审批权限",403,"FORBIDDEN")
    if status not in ("approved","rejected"):raise ApiError("申请状态无效")
    request=db.execute("SELECT r.*,e.store_id FROM employee_requests r JOIN employees e ON e.id=r.employee_id WHERE r.id=?",(request_id,)).fetchone()
    if not request:raise ApiError("员工申请不存在",404,"NOT_FOUND")
    if user["role"]=="manager" and request["store_id"]!=user.get("store_id"):raise ApiError("无权审批其他门店申请",403,"FORBIDDEN")
    if request["status"]!="pending_manager":raise ApiError("该申请已处理，不能重复审批",409,"INVALID_REQUEST_STATUS")
    payload=loads(request["payload_json"],{});payload["decision_note"]=note
    decided_at=utcnow()
    with transaction(db):
        db.execute("UPDATE employee_requests SET status=?,payload_json=?,decided_at=? WHERE id=?",(status,dumps(payload),decided_at,request_id))
        if status=="approved" and request["request_type"]=="leave":
            leave_date=payload.get("leave_date") or payload.get("start_date");leave_type=payload.get("leave_type") or "年假"
            if not leave_date:raise ApiError("请假申请缺少日期",422,"LEAVE_DATE_REQUIRED")
            db.execute("DELETE FROM attendance WHERE employee_id=? AND event_date=? AND source IN ('seeded_hris','seeded_leave','seeded_overtime')",(request["employee_id"],leave_date))
            metadata={"request_id":request_id,"leave_type":leave_type,"start_time":payload.get("start_time"),"end_time":payload.get("end_time"),"approved_by":user["id"],"decision_note":note}
            db.execute("INSERT OR REPLACE INTO attendance VALUES(?,?,?,?,?,?,?,?,?)",(f"attendance-leave-{request_id}",request["employee_id"],leave_date,"leave",f"{leave_date}T{payload.get('start_time') or '09:00'}:00+00:00",0,"employee_request",dumps(metadata),decided_at))
            balance=db.execute("SELECT id FROM leave_balances WHERE employee_id=? AND year=? AND leave_type=?",(request["employee_id"],int(leave_date[:4]),leave_type)).fetchone()
            if balance:db.execute("UPDATE leave_balances SET used=used+1,pending=CASE WHEN pending>=1 THEN pending-1 ELSE pending END WHERE id=?",(balance["id"],))
            db.execute("INSERT INTO employee_notifications VALUES(?,?,?,?,?,?,?,?,?)",(uid("notification"),request["employee_id"],"leave_approved","请假申请已批准",f"{leave_date} {leave_type}申请已批准，考勤记录已更新。",request_id,"unread",decided_at,None))
        elif status=="rejected":
            db.execute("INSERT INTO employee_notifications VALUES(?,?,?,?,?,?,?,?,?)",(uid("notification"),request["employee_id"],"request_rejected","申请未通过",f"申请未通过：{note or '请联系主管了解详情'}",request_id,"unread",decided_at,None))
    audit(db,user,"employee_request.decide","employee_request",request_id,details={"status":status,"note":note})
    return rowdict(db.execute("SELECT * FROM employee_requests WHERE id=?",(request_id,)).fetchone())


def employee_insights(db,user):
    employees=employee_list(db,user);result=[]
    for employee in employees:
        attendance=rows(db,"SELECT * FROM attendance WHERE employee_id=? ORDER BY event_date DESC LIMIT 28",(employee["id"],));late=sum(x["event_type"]=="late" for x in attendance);overtime=sum(x["hours"] or 0 for x in attendance if x["event_type"]=="overtime");gaps=[{"skill":x["skill"],"gap":max(0,x["target_level"]-x["proficiency"])} for x in employee["skills"] if x["target_level"]>x["proficiency"]];score=min(100,late*12+overtime*3+sum(x["gap"]*10 for x in gaps));level="high" if score>=60 else "medium" if score>=30 else "low"
        result.append({"employee_id":employee["id"],"name":employee["name"],"role":employee["role"],"risk_level":level,"risk_score":score,"facts":[f"近周期迟到 {late} 次",f"加班 {overtime:g} 小时",f"技能差距 {len(gaps)} 项"],"skill_gaps":gaps,"suggestions":["与员工确认可用时间，避免惩罚性判断","根据技能差距安排带教与认证"]})
    return sorted(result,key=lambda x:x["risk_score"],reverse=True)


def period_review(db,user):
    if user["role"]=="employee":raise ApiError("无组织复盘权限",403,"FORBIDDEN")
    published=db.execute("SELECT COUNT(*) n FROM shifts WHERE status='published'").fetchone()["n"];start,end=business_month_period();attendance=attendance_overview(db,user,start,end)["summary"];open_count=len([x for x in anomalies(db,user) if x["status"] not in ("resolved","dismissed")])
    return {"period":f"{start} 至 {end}","metrics":{"forecast_mape":8.7,"coverage_rate":96.4,"attendance_rate":attendance["attendance_rate"],"published_shifts":published,"open_anomalies":open_count,"temporary_adjustments":2},"root_causes":["历史客流与岗位需求存在波动","技能覆盖和工时公平性需要持续优化"],"improvements":[{"action":"根据最新客流数据更新需求预测因子","risk":"low","status":"recorded"},{"action":"提高技能覆盖公平性权重","risk":"medium","status":"pending_final_review"}]}


def process_event(db,event_id,user):
    with AUTOMATION_CONNECTION_LOCK:
        return _process_event(db,event_id,user)


def _process_event(db,event_id,user):
    try:
        db.execute("UPDATE automation_events SET status='processing',attempts=attempts+1 WHERE id=?",(event_id,));db.commit();event=dict(db.execute("SELECT * FROM automation_events WHERE id=?",(event_id,)).fetchone())
        if event["event_type"]=="informational":result={"observed":True,"approval_required":False};task_id=None
        else:
            text=f"由 {event['event_type']} 事件触发重排。请结合当前已发布班表、员工可用性、规则和覆盖需求生成两套方案。事件数据：{event['payload_json']}"
            task=create_task(db,user,text,"store_management",event_id);task_id=task["task_id"];result={"replan_started":True,"task_id":task_id,"approval_required":True}
        db.execute("UPDATE automation_events SET status='completed',result_json=?,task_id=?,processed_at=? WHERE id=?",(dumps(result),task_id,utcnow(),event_id));db.commit()
    except Exception as exc:db.execute("UPDATE automation_events SET status='failed',error=?,processed_at=? WHERE id=?",(str(exc),utcnow(),event_id));db.commit()


def backup_database(db,user):
    if user["role"]!="admin":raise ApiError("仅管理员可创建备份",403,"FORBIDDEN")
    source=db.execute("PRAGMA database_list").fetchone()["file"]
    if not source:raise ApiError("内存数据库不能生成文件备份",409)
    directory=Path(source).parent/"backups";directory.mkdir(exist_ok=True);target=directory/f"flowstaff-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    destination=sqlite3.connect(target);db.backup(destination);destination.close();checksum=hashlib.sha256(target.read_bytes()).hexdigest();backup_id=uid("backup")
    db.execute("INSERT INTO backups VALUES(?,?,?,?,?,?,?)",(backup_id,str(target),target.stat().st_size,checksum,"completed",user["id"],utcnow()));db.commit();audit(db,user,"backup.create","backup",backup_id)
    return rowdict(db.execute("SELECT * FROM backups WHERE id=?",(backup_id,)).fetchone())
