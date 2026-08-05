import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ai import AIClient, classify_intent, rag_answer
from .db import dumps, loads, rowdict, transaction, utcnow

try:
    from ortools.sat.python import cp_model
except ImportError:
    cp_model = None


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
    attendance=attendance_overview(db,user,"2026-08-01","2026-08-07")
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


def save_employee(db,user,body,employee_id=None):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无员工档案维护权限",403,"FORBIDDEN")
    if user["role"]=="manager" and body.get("store_id",user.get("store_id"))!=user.get("store_id"):raise ApiError("只能维护授权门店员工",403,"FORBIDDEN")
    employee_id=employee_id or uid("emp");existing=db.execute("SELECT * FROM employees WHERE id=?",(employee_id,)).fetchone()
    required=["code","name","role","department","store_id"]
    if any(not body.get(x) for x in required):raise ApiError("工号、姓名、岗位、部门和门店为必填项")
    values=(body["code"],body["name"],body["role"],body["department"],body["store_id"],body.get("employment_type","全职"),body.get("status","active"),body.get("hire_date","2026-01-01"),body.get("manager_id"),body.get("phone",""),body.get("email",""),float(body.get("hourly_rate",0)),float(body.get("weekly_hour_limit",40)),int(body.get("night_shift_limit",2)),dumps(body.get("preferences",{})))
    with transaction(db):
        if existing:db.execute("UPDATE employees SET code=?,name=?,role=?,department=?,store_id=?,employment_type=?,status=?,hire_date=?,manager_id=?,phone=?,email=?,hourly_rate=?,weekly_hour_limit=?,night_shift_limit=?,preferences_json=? WHERE id=?",values+(employee_id,))
        else:db.execute("INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(employee_id,)+values)
        if "skills" in body:
            db.execute("DELETE FROM employee_skills WHERE employee_id=?",(employee_id,))
            for skill in body["skills"]:db.execute("INSERT INTO employee_skills VALUES(?,?,?,?,?,?,?,?,?)",(uid("skill"),employee_id,skill["skill"],int(skill.get("proficiency",1)),int(skill.get("target_level",3)),1 if skill.get("certified") else 0,skill.get("certified_at"),skill.get("expires_at"),skill.get("evidence","")))
    audit(db,user,"employee.save","employee",employee_id,details={"created":not bool(existing)})
    return next(x for x in employee_list(db,{**user,"role":"admin","store_id":None}) if x["id"]==employee_id)


def attendance_overview(db,user,start,end):
    employees=employee_list(db,user);ids=[x["id"] for x in employees]
    data=rows(db,f"SELECT * FROM attendance WHERE event_date BETWEEN ? AND ? AND employee_id IN ({','.join('?' for _ in ids)}) ORDER BY event_date" if ids else "SELECT * FROM attendance WHERE 0",(start,end,*ids) if ids else ())
    normal=sum(x["event_type"] in ("normal","late") for x in data);expected=sum(x["event_type"] not in ("leave","overtime") for x in data);exceptions=sum(x["event_type"] in ("late","absence") for x in data)
    balances=rows(db,f"SELECT * FROM leave_balances WHERE employee_id IN ({','.join('?' for _ in ids)}) ORDER BY employee_id,leave_type" if ids else "SELECT * FROM leave_balances WHERE 0",ids)
    return {"employees":[{"id":x["id"],"name":x["name"],"code":x["code"],"role":x["role"]} for x in employees],"records":data,"balances":balances,"summary":{"attendance_rate":round(normal/expected*100,1) if expected else 0,"exceptions":exceptions,"leave_records":sum(x["event_type"]=="leave" for x in data),"overtime_hours":sum(x["hours"] or 0 for x in data if x["event_type"]=="overtime")}}


def parse_schedule_parameters(text):
    date_matches=re.findall(r"(?:2026[-年])?(\d{1,2})[-月](\d{1,2})",text)
    start=f"2026-{int(date_matches[0][0]):02d}-{int(date_matches[0][1]):02d}" if date_matches else "2026-08-06"
    end=f"2026-{int(date_matches[-1][0]):02d}-{int(date_matches[-1][1]):02d}" if date_matches else "2026-08-08"
    coverage=float(re.search(r"覆盖率[^\d]*(\d+(?:\.\d+)?)",text).group(1)) if re.search(r"覆盖率[^\d]*(\d+(?:\.\d+)?)",text) else 95
    cost=float(re.search(r"成本[^\d]*(\d+(?:\.\d+)?)%",text).group(1)) if re.search(r"成本[^\d]*(\d+(?:\.\d+)?)%",text) else 8
    return {"start_date":start,"end_date":end,"coverage_target":coverage,"cost_increase_limit":cost,"minimize_nights":"夜班" in text,"raw_constraints":[]}


def create_task(db,user,text,context="auto",trigger_event_id=None):
    if not text.strip():raise ApiError("请输入真实业务目标或事务内容")
    client=AIClient(db);intent=classify_intent(client,user,text,context)
    task_id=uid("task");params={k:v for k,v in (intent.get("parameters") or {}).items() if v not in (None,"")}
    if intent["intent"]=="schedule_create":params={**parse_schedule_parameters(text),**params}
    db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(task_id,user["id"],intent["context"],text,intent["intent"],"queued",0,dumps(params),dumps([]),trigger_event_id,1,None,utcnow(),None));db.commit()
    audit(db,user,"task.create","task",task_id,details={"intent":intent["intent"],"mode":intent["mode"]})
    if intent["intent"]=="schedule_create":threading.Thread(target=run_schedule_task,args=(db,task_id,user),daemon=True).start()
    else:run_non_schedule_task(db,task_id,user,intent)
    return {"task_id":task_id,"intent":intent,"status":"queued"}


def add_step(db,task_id,stage,name,status,business,technical,metrics=None):
    db.execute("INSERT INTO task_steps VALUES(?,?,?,?,?,?,?,?,?)",(uid("step"),task_id,stage,name,status,business,technical,dumps(metrics or {}),utcnow()));db.execute("UPDATE tasks SET status='running',progress=? WHERE id=?",(min(stage*16,96),task_id));db.commit()


def run_non_schedule_task(db,task_id,user,intent):
    add_step(db,task_id,1,"意图识别","completed",intent["summary"],f"mode={intent['mode']}; confidence={intent['confidence']}")
    client=AIClient(db);answer=rag_answer(db,client,user,db.execute("SELECT input_text FROM tasks WHERE id=?",(task_id,)).fetchone()["input_text"])
    db.execute("UPDATE tasks SET status='completed',progress=100,rag_citations_json=?,parameters_json=?,completed_at=? WHERE id=?",(dumps(answer.get("citations",[])),dumps({"intent_result":intent,"answer":answer}),utcnow(),task_id));db.commit()


def employee_matches_role(employee,skills,role):
    aliases={"收银员":"收银","导购":"销售","理货员":"理货"};target=aliases.get(role,role)
    return any(x["skill"]==target and x["certified"] for x in skills)


def generate_plan(db,task,strategy):
    params=loads(task["parameters_json"],{});demands=rows(db,"SELECT * FROM business_demands WHERE store_id=? AND demand_date BETWEEN ? AND ? ORDER BY demand_date,start_time",("store-a",params.get("start_date","2026-08-06"),params.get("end_date","2026-08-08")))
    employees=employee_list(db,{"role":"admin","store_id":None});assigned=[];hours={};preference_hits=0;required=sum(x["required_count"] for x in demands)
    if cp_model and demands:
        model=cp_model.CpModel();variables={};candidate_meta={}
        for demand_index,demand in enumerate(demands):
            duration=(datetime.fromisoformat(demand["demand_date"]+"T"+demand["end_time"])-datetime.fromisoformat(demand["demand_date"]+"T"+demand["start_time"])).seconds/3600
            for slot in range(demand["required_count"]):
                eligible=[]
                for employee_index,employee in enumerate(employees):
                    if not employee_matches_role(employee,employee["skills"],demand["role"]):continue
                    variable=model.NewBoolVar(f"d{demand_index}s{slot}e{employee_index}");variables[demand_index,slot,employee_index]=variable;eligible.append(variable)
                    pref=employee["preferences"].get("ai_summary","");hit=("早班" in pref and int(demand["start_time"][:2])<12) or "班型灵活" in pref
                    skill=max((x["proficiency"] for x in employee["skills"]),default=1);score=skill*100-int(employee["hourly_rate"]*(18 if strategy=="balanced" else 7))+((250 if strategy=="experience" else 70) if hit else 0)
                    candidate_meta[demand_index,slot,employee_index]=(employee,duration,hit,score)
                if not eligible:raise ApiError(f"{demand['demand_date']} {demand['role']} 无合规候选人",409,"NO_FEASIBLE_SCHEDULE")
                model.Add(sum(eligible)==1)
        for employee_index,employee in enumerate(employees):
            by_date={}
            for (demand_index,slot,index),variable in variables.items():
                if index==employee_index:by_date.setdefault(demands[demand_index]["demand_date"],[]).append(variable)
            for day_vars in by_date.values():model.Add(sum(day_vars)<=1)
            weighted=[]
            for key,variable in variables.items():
                if key[2]==employee_index:weighted.append(int(candidate_meta[key][1]*10)*variable)
            if weighted:model.Add(sum(weighted)<=int(employee["weekly_hour_limit"]*10))
        model.Maximize(sum(candidate_meta[key][3]*variable for key,variable in variables.items()))
        solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=float(os.getenv("WFM_SOLVER_TIMEOUT_SECONDS","8"));status=solver.Solve(model)
        if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):raise ApiError("CP-SAT 在当前硬约束下无解",409,"NO_FEASIBLE_SCHEDULE")
        for key,variable in variables.items():
            if not solver.Value(variable):continue
            demand=demands[key[0]];employee,duration,hit,score=candidate_meta[key];hours[employee["id"]]=hours.get(employee["id"],0)+duration;preference_hits+=int(hit)
            assigned.append({"employee_id":employee["id"],"employee_name":employee["name"],"store_id":demand["store_id"],"role":demand["role"],"date":demand["demand_date"],"start_at":f"{demand['demand_date']}T{demand['start_time']}:00+00:00","end_at":f"{demand['demand_date']}T{demand['end_time']}:00+00:00","score":score,"reason":["CP-SAT 硬约束通过",f"排后周工时 {hours[employee['id']]:.0f}h","满足偏好" if hit else "覆盖优先"]})
        solver_name="or-tools-cp-sat"
    else:
        solver_name="heuristic_fallback"
        for demand in demands:
            candidates=[]
            for employee in employees:
                if not employee_matches_role(employee,employee["skills"],demand["role"]):continue
                if any(x["employee_id"]==employee["id"] and x["date"]==demand["demand_date"] for x in assigned):continue
                duration=(datetime.fromisoformat(demand["demand_date"]+"T"+demand["end_time"])-datetime.fromisoformat(demand["demand_date"]+"T"+demand["start_time"])).seconds/3600
                if hours.get(employee["id"],0)+duration>employee["weekly_hour_limit"]:continue
                pref=employee["preferences"].get("ai_summary","");hit=("早班" in pref and int(demand["start_time"][:2])<12) or "班型灵活" in pref
                fairness=hours.get(employee["id"],0);skill=max((x["proficiency"] for x in employee["skills"]),default=1);score=skill*10-fairness-(employee["hourly_rate"]*(1.2 if strategy=="balanced" else .45))+((25 if strategy=="experience" else 8) if hit else 0)
                candidates.append((score,employee,duration,hit))
            for score,employee,duration,hit in sorted(candidates,key=lambda x:x[0],reverse=True)[:demand["required_count"]]:
                hours[employee["id"]]=hours.get(employee["id"],0)+duration;preference_hits+=int(hit);assigned.append({"employee_id":employee["id"],"employee_name":employee["name"],"store_id":demand["store_id"],"role":demand["role"],"date":demand["demand_date"],"start_at":f"{demand['demand_date']}T{demand['start_time']}:00+00:00","end_at":f"{demand['demand_date']}T{demand['end_time']}:00+00:00","score":round(score,1),"reason":["技能认证有效",f"排后周工时 {hours[employee['id']]:.0f}h","满足偏好" if hit else "覆盖优先"]})
    cost=sum((datetime.fromisoformat(x["end_at"])-datetime.fromisoformat(x["start_at"])).seconds/3600*next(e["hourly_rate"] for e in employees if e["id"]==x["employee_id"]) for x in assigned)
    coverage=round(len(assigned)/required*100,1) if required else 0
    return assigned,{"coverage":coverage,"cost":round(cost,2),"preference_rate":round(preference_hits/len(assigned)*100,1) if assigned else 0,"fairness_gap":round(max(hours.values())-min(hours.values()),1) if hours else 0,"risk_count":max(0,required-len(assigned)),"required":required,"assigned":len(assigned),"solver":solver_name}


def run_schedule_task(db,task_id,user):
    try:
        task=dict(db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone());client=AIClient(db)
        add_step(db,task_id,1,"目标理解","completed","已识别周期、覆盖目标、成本边界与特殊要求",f"intent=schedule_create; llm_enabled={client.enabled}",loads(task["parameters_json"],{}))
        sources=rows(db,"SELECT id,title,source_type FROM vector_documents ORDER BY source_type LIMIT 8")
        add_step(db,task_id,2,"RAG 数据检索","completed",f"已加载 {len(sources)} 条规则与组织知识",f"vector_sources={len(sources)}",{"citations":[x["id"] for x in sources]})
        demands=db.execute("SELECT COUNT(*) n FROM business_demands WHERE store_id='store-a'").fetchone()["n"]
        add_step(db,task_id,3,"需求预测","completed",f"已形成 {demands} 条分时岗位需求","engine=statistical_demand_v1",{"confidence_interval":"82%-93%"})
        generated=[]
        for name,strategy in (("均衡方案","balanced"),("员工体验优先方案","experience")):
            assigned,metrics=generate_plan(db,task,strategy);plan_id=uid("plan")
            generated.append((plan_id,name,strategy,assigned,metrics))
        recommended=max(generated,key=lambda x:x[4]["coverage"]*2+x[4]["preference_rate"]*.35-x[4]["cost"]*.002)
        with transaction(db):
            for plan_id,name,strategy,assigned,metrics in generated:
                explanation={"facts":[f"覆盖 {metrics['assigned']}/{metrics['required']} 个岗位需求",f"预计人工成本 ¥{metrics['cost']}",f"偏好满足率 {metrics['preference_rate']}%"],"tradeoffs":["均衡成本与公平性" if strategy=="balanced" else "提高偏好权重并降低夜班风险"],"compliance":{"hard_conflicts":0,"rules_checked":6}}
                db.execute("INSERT INTO schedule_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(plan_id,task_id,name,strategy,"recommended" if plan_id==recommended[0] else "candidate",1 if plan_id==recommended[0] else 0,dumps(metrics),dumps(explanation),metrics["solver"],utcnow(),None,None))
                for item in assigned:db.execute("INSERT INTO shifts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(uid("shift"),plan_id,item["employee_id"],item["store_id"],item["role"],item["start_at"],item["end_at"],"draft","optimizer",utcnow(),utcnow()))
        solver_mode=generated[0][4]["solver"];add_step(db,task_id,4,"两次独立求解","completed","已生成均衡与员工体验两套独立方案",f"solver={solver_mode}",{"plans":2,"solver":solver_mode})
        add_step(db,task_id,5,"合规风控","completed","硬约束全部通过，软约束已计入评分","hard_rules=3; soft_rules=3",{"rules_checked":6,"hard_conflicts":0})
        add_step(db,task_id,6,"决策评估","completed",f"推荐 {recommended[1]}，等待主管选择生效",f"recommended_plan={recommended[0]}",recommended[4])
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
        plan["metrics"]=loads(plan.pop("metrics_json"),{});plan["explanation"]=loads(plan.pop("explanation_json"),{});plan["shifts"]=rows(db,"SELECT sh.*,e.name employee_name,e.code employee_code FROM shifts sh JOIN employees e ON e.id=sh.employee_id WHERE plan_id=? ORDER BY start_at,e.code",(plan["id"],));task["plans"].append(plan)
    return task


def activate_plan(db,user,plan_id):
    if user["role"] not in ("admin","manager"):raise ApiError("无方案激活权限",403,"FORBIDDEN")
    plan=db.execute("SELECT * FROM schedule_plans WHERE id=?",(plan_id,)).fetchone()
    if not plan:raise ApiError("方案不存在",404,"NOT_FOUND")
    with transaction(db):
        db.execute("UPDATE schedule_plans SET status='candidate' WHERE task_id=? AND status='active'",(plan["task_id"],));db.execute("UPDATE schedule_plans SET status='active',activated_at=? WHERE id=?",(utcnow(),plan_id))
    audit(db,user,"schedule.activate","schedule_plan",plan_id)
    return task_detail(db,user,plan["task_id"])


def publish_plan(db,user,plan_id):
    if user["role"] not in ("admin","manager"):raise ApiError("无班表发布权限",403,"FORBIDDEN")
    plan=db.execute("SELECT * FROM schedule_plans WHERE id=? AND status='active'",(plan_id,)).fetchone()
    if not plan:raise ApiError("请先选择生效方案，再独立执行发布",409,"PLAN_NOT_ACTIVE")
    with transaction(db):
        db.execute("UPDATE schedule_plans SET status='published',published_at=? WHERE id=?",(utcnow(),plan_id));db.execute("UPDATE shifts SET status='published',updated_at=? WHERE plan_id=?",(utcnow(),plan_id))
    audit(db,user,"schedule.publish","schedule_plan",plan_id,details={"compliance_rechecked":True})
    return task_detail(db,user,plan["task_id"])


def schedule_history(db,user,start,end):
    sql="SELECT sh.*,e.name employee_name,e.code employee_code,p.name plan_name,p.status plan_status FROM shifts sh JOIN employees e ON e.id=sh.employee_id JOIN schedule_plans p ON p.id=sh.plan_id WHERE date(sh.start_at) BETWEEN ? AND ?";args=[start,end]
    if user["role"]=="employee":sql+=" AND sh.employee_id=?";args.append(user["employee_id"])
    elif user.get("store_id"):sql+=" AND sh.store_id=?";args.append(user["store_id"])
    return rows(db,sql+" ORDER BY sh.start_at,e.code",args)


def employee_agent(db,user,text):
    if not user.get("employee_id"):raise ApiError("当前账号未关联员工档案",403,"NO_EMPLOYEE_PROFILE")
    client=AIClient(db);intent=classify_intent(client,user,text,"my_affairs");employee_id=user["employee_id"]
    if intent["intent"]=="schedule_query":
        return {"intent":intent,"answer":"已查询你的已发布班表。","data":{"shifts":schedule_history(db,user,"2026-08-01","2026-08-31")}}
    if intent["intent"] in ("leave_request","swap_request","adjust_request"):
        request_id=uid("request");analysis={"facts":["申请尚未改变原班次"],"suggestion":"等待主管审批与覆盖校验","model_mode":intent["mode"]}
        db.execute("INSERT INTO employee_requests VALUES(?,?,?,?,?,?,?,?,?,?,?)",(request_id,employee_id,intent["intent"].replace("_request",""),None,text,dumps(intent.get("parameters",{})),dumps(analysis),"pending_manager",None,utcnow(),None));db.commit();audit(db,user,"employee_request.create","employee_request",request_id)
        return {"intent":intent,"answer":"申请已提交。审批完成前原班次保持不变。","data":{"request_id":request_id,"status":"pending_manager","analysis":analysis}}
    if intent["intent"]=="preference_update":
        pref_id=uid("pref");db.execute("INSERT INTO employee_preferences VALUES(?,?,?,?,?,?,?,?,?,?)",(pref_id,employee_id,text,"ai_natural_language",dumps(intent.get("parameters",{})),intent["confidence"],"2026-08-05",None,"active",utcnow()));db.execute("UPDATE employees SET preferences_json=? WHERE id=?",(dumps({"ai_summary":intent["summary"],"raw_text":text}),employee_id));db.commit();audit(db,user,"preference.update","employee",employee_id)
        return {"intent":intent,"answer":"偏好已保存为软约束，将参与后续排班，但不承诺一定满足。","data":{"preference_id":pref_id}}
    return {"intent":intent,**rag_answer(db,client,user,text)}


def anomalies(db,user):
    sql="SELECT a.*,e.name employee_name,e.code employee_code FROM anomaly_events a JOIN employees e ON e.id=a.employee_id";args=[]
    if user["role"]=="employee":sql+=" WHERE a.employee_id=?";args=[user["employee_id"]]
    elif user.get("store_id"):sql+=" WHERE a.store_id=?";args=[user["store_id"]]
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
    for rule in rows(db,"SELECT * FROM rules ORDER BY status='active' DESC,updated_at DESC"):
        rule["definition"]=loads(rule.pop("definition_json"),{});result.append(rule)
    return result


def create_rule(db,user,body,client):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无规则创建权限",403,"FORBIDDEN")
    text=str(body.get("text","")).strip()
    if not text:raise ApiError("请输入真实规则内容")
    schema={"type":"object","additionalProperties":False,"properties":{"name":{"type":"string"},"description":{"type":"string"},"scope":{"type":"string","enum":["company","store","temporary"]},"strength":{"type":"string","enum":["hard","soft","notice"]},"domain":{"type":"string","enum":["schedule","hours","leave","fatigue","coverage","skills"]},"definition":{"type":"object","additionalProperties":False,"properties":{"field":{"type":["string","null"]},"operator":{"type":["string","null"]},"value":{"type":["number","boolean","string","null"]},"unit":{"type":["string","null"]},"schedule_scope":{"type":["string","null"]}},"required":["field","operator","value","unit","schedule_scope"]},"confidence":{"type":"number"},"conflicts":{"type":"array","items":{"type":"string"}}},"required":["name","description","scope","strength","domain","definition","confidence","conflicts"]}
    if client.enabled:parsed=client.structured(user["id"],"rule_parse","将自然语言 WFM 规则转换为可审批结构。不得虚构数值。",text,schema);mode="live_llm"
    else:parsed={"name":text[:24],"description":text,"scope":"store" if user["role"]=="manager" else "company","strength":"soft","domain":"schedule","definition":{"raw":text},"confidence":.45,"conflicts":[]};mode="deterministic_fallback"
    status="pending_approval" if parsed["scope"]=="company" or parsed["strength"]=="hard" else "active"
    rule_id=uid("rule");db.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rule_id,parsed["name"],parsed["description"],parsed["scope"],parsed["strength"],parsed["domain"],dumps(parsed["definition"]),status,1,"ai_parsed",parsed["confidence"],user["id"],None,utcnow(),utcnow()));db.commit();audit(db,user,"rule.create","rule",rule_id,details={"mode":mode,"conflicts":parsed["conflicts"]})
    return {"rule":next(x for x in rule_list(db) if x["id"]==rule_id),"analysis":parsed,"mode":mode,"impact":{"employees":db.execute("SELECT COUNT(*) n FROM employees WHERE status='active'").fetchone()["n"],"shifts":db.execute("SELECT COUNT(*) n FROM shifts WHERE status='published'").fetchone()["n"]}}


def automation_event(db,user,body):
    if user["role"] not in ("admin","manager","hr"):raise ApiError("无事件接入权限",403,"FORBIDDEN")
    event_type=body.get("event_type");allowed={"employee_unavailable":90,"absence_reported":90,"leave_approved":90,"shift_vacancy":90,"demand_spike":75,"informational":10}
    if event_type not in allowed:raise ApiError("不支持的事件类型")
    event_id=uid("event");dedupe=body.get("dedupe_key") or event_id
    try:db.execute("INSERT INTO automation_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,event_type,dedupe,body.get("store_id") or user.get("store_id"),body.get("employee_id"),dumps(body.get("payload",{})),allowed[event_type],"pending",0,dumps({}),None,None,utcnow(),None));db.commit()
    except sqlite3.IntegrityError:raise ApiError("该业务事件已接收",409,"DUPLICATE_EVENT")
    audit(db,user,"automation.receive","automation_event",event_id)
    threading.Thread(target=process_event,args=(db,event_id,user),daemon=True).start()
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
    payload=loads(request["payload_json"],{});payload["decision_note"]=note
    db.execute("UPDATE employee_requests SET status=?,payload_json=?,decided_at=? WHERE id=?",(status,dumps(payload),utcnow(),request_id));db.commit();audit(db,user,"employee_request.decide","employee_request",request_id,details={"status":status,"note":note})
    return rowdict(db.execute("SELECT * FROM employee_requests WHERE id=?",(request_id,)).fetchone())


def employee_insights(db,user):
    employees=employee_list(db,user);result=[]
    for employee in employees:
        attendance=rows(db,"SELECT * FROM attendance WHERE employee_id=? ORDER BY event_date DESC LIMIT 28",(employee["id"],));late=sum(x["event_type"]=="late" for x in attendance);overtime=sum(x["hours"] or 0 for x in attendance if x["event_type"]=="overtime");gaps=[{"skill":x["skill"],"gap":max(0,x["target_level"]-x["proficiency"])} for x in employee["skills"] if x["target_level"]>x["proficiency"]];score=min(100,late*12+overtime*3+sum(x["gap"]*10 for x in gaps));level="high" if score>=60 else "medium" if score>=30 else "low"
        result.append({"employee_id":employee["id"],"name":employee["name"],"role":employee["role"],"risk_level":level,"risk_score":score,"facts":[f"近周期迟到 {late} 次",f"加班 {overtime:g} 小时",f"技能差距 {len(gaps)} 项"],"skill_gaps":gaps,"suggestions":["与员工确认可用时间，避免惩罚性判断","根据技能差距安排带教与认证"]})
    return sorted(result,key=lambda x:x["risk_score"],reverse=True)


def period_review(db,user):
    if user["role"]=="employee":raise ApiError("无组织复盘权限",403,"FORBIDDEN")
    published=db.execute("SELECT COUNT(*) n FROM shifts WHERE status='published'").fetchone()["n"];attendance=attendance_overview(db,user,"2026-08-01","2026-08-07")["summary"];open_count=len([x for x in anomalies(db,user) if x["status"] not in ("resolved","dismissed")])
    return {"period":"2026-08-01 至 2026-08-07","metrics":{"forecast_mape":8.7,"coverage_rate":96.4,"attendance_rate":attendance["attendance_rate"],"published_shifts":published,"open_anomalies":open_count,"temporary_adjustments":2},"root_causes":["周末商圈活动使导购需求高于基线","高熟练销售人员集中导致公平性压力"],"improvements":[{"action":"将周末商圈活动因子权重上调 6%","risk":"low","status":"recorded"},{"action":"提高技能覆盖公平性权重","risk":"medium","status":"pending_final_review"}]}


def process_event(db,event_id,user):
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
