import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import sys
import time
import traceback
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from backend.ai import AIClient, rag_answer
    from backend.db import connect, rowdict, utcnow, verify_password
    from backend.services import ApiError, activate_plan, anomalies, approve_rule, attendance_overview, audit, automation_event, backup_database, create_rule, create_task, decide_employee_request, employee_agent, employee_insights, employee_list, overview, period_review, publish_plan, rule_list, save_employee, schedule_history, task_detail, update_anomaly
else:
    from .ai import AIClient, rag_answer
    from .db import connect, rowdict, utcnow, verify_password
    from .services import ApiError, activate_plan, anomalies, approve_rule, attendance_overview, audit, automation_event, backup_database, create_rule, create_task, decide_employee_request, employee_agent, employee_insights, employee_list, overview, period_review, publish_plan, rule_list, save_employee, schedule_history, task_detail, update_anomaly

ROOT=Path(__file__).parents[1];PUBLIC=ROOT/"public"
print(f"[startup] Python {sys.version.split()[0]}，开始初始化 SQLite",flush=True)
DB=connect()
print("[startup] SQLite 初始化完成",flush=True)
RATE=defaultdict(deque)


def json_default(value):
    if hasattr(value,"keys"):return dict(value)
    raise TypeError()


class Handler(BaseHTTPRequestHandler):
    server_version="FlowStaffAI/3.0"

    def log_message(self,format,*args):
        print(f"{self.address_string()} [{self.log_date_time_string()}] {format%args}")

    def send_json(self,status,data):
        payload=json.dumps(data,ensure_ascii=False,default=json_default).encode()
        self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(payload)));self.send_header("Cache-Control","no-store");self.send_header("X-Content-Type-Options","nosniff");self.send_header("X-Frame-Options","DENY");self.send_header("Referrer-Policy","same-origin");self.end_headers();self.wfile.write(payload)

    def body(self):
        length=int(self.headers.get("Content-Length","0"))
        if length>1_000_000:raise ApiError("请求体过大",413,"PAYLOAD_TOO_LARGE")
        if not length:return {}
        try:return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:raise ApiError("JSON 格式无效")

    def rate_limit(self,key,limit=120,window=60):
        now=time.time();bucket=RATE[key]
        while bucket and bucket[0]<now-window:bucket.popleft()
        if len(bucket)>=limit:raise ApiError("请求过于频繁，请稍后重试",429,"RATE_LIMITED")
        bucket.append(now)

    def user(self,required=True):
        auth=self.headers.get("Authorization","")
        if not auth.startswith("Bearer "):
            if required:raise ApiError("请先登录",401,"UNAUTHORIZED")
            return None
        token_hash=hashlib.sha256(auth[7:].encode()).hexdigest()
        row=DB.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND u.status='active'",(token_hash,utcnow())).fetchone()
        if not row:
            if required:raise ApiError("会话无效或已过期",401,"UNAUTHORIZED")
            return None
        return dict(row)

    def match(self,path,pattern):
        actual=path.strip("/").split("/");expected=pattern.strip("/").split("/")
        if len(actual)!=len(expected):return None
        params={}
        for a,e in zip(actual,expected):
            if e.startswith("{"):params[e[1:-1]]=a
            elif a!=e:return None
        return params

    def handle_api(self,method,path,query):
        request_id=self.headers.get("X-Request-ID") or str(uuid.uuid4());ip=self.client_address[0];self.rate_limit(f"{ip}:all")
        if method=="GET" and path=="/healthz":return 200,{"ok":True,"service":"flowstaff-ai","database":"connected"}
        if method=="POST" and path=="/api/auth/login":
            body=self.body();username=str(body.get("username","")).strip();self.rate_limit(f"{ip}:login:{username}",10,300)
            user=DB.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
            if user and user["locked_until"] and user["locked_until"]>utcnow():raise ApiError("登录失败次数过多，账号暂时锁定",423,"ACCOUNT_LOCKED")
            if not user or not verify_password(str(body.get("password","")),user["password_hash"]):
                if user:
                    attempts=user["failed_attempts"]+1;locked=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat() if attempts>=5 else None;DB.execute("UPDATE users SET failed_attempts=?,locked_until=? WHERE id=?",(attempts,locked,user["id"]));DB.commit()
                audit(DB,dict(user) if user else None,"auth.login","user",user["id"] if user else username,"failed",ip=ip,request_id=request_id);raise ApiError("用户名或密码错误",401,"INVALID_CREDENTIALS")
            token=secrets.token_urlsafe(36);session_id=f"session-{uuid.uuid4()}";expires=(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat();DB.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",(session_id,user["id"],hashlib.sha256(token.encode()).hexdigest(),expires,None,utcnow()));DB.execute("UPDATE users SET failed_attempts=0,locked_until=NULL WHERE id=?",(user["id"],));DB.commit();audit(DB,dict(user),"auth.login","session",session_id,ip=ip,request_id=request_id)
            return 200,{"ok":True,"data":{"token":token,"expires_at":expires,"user":self.public_user(dict(user))}}
        user=self.user()
        if method=="POST" and path=="/api/auth/logout":
            token_hash=hashlib.sha256(self.headers["Authorization"][7:].encode()).hexdigest();DB.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?",(utcnow(),token_hash));DB.commit();audit(DB,user,"auth.logout","session","current",ip=ip,request_id=request_id);return 200,{"ok":True,"data":{"logged_out":True}}
        if method=="GET" and path=="/api/auth/me":return 200,{"ok":True,"data":self.public_user(user)}
        if method=="GET" and path=="/api/metadata":return 200,{"ok":True,"data":{"stores":[dict(x) for x in DB.execute("SELECT * FROM stores ORDER BY code")],"positions":[dict(x) for x in DB.execute("SELECT * FROM job_positions WHERE status='active' ORDER BY name")],"departments":[dict(x) for x in DB.execute("SELECT * FROM departments WHERE status='active' ORDER BY name")],"skills":[dict(x) for x in DB.execute("SELECT * FROM skill_catalog WHERE status='active' ORDER BY name")],"ai":{"enabled":AIClient(DB).enabled,"model":AIClient(DB).model,"mode":"live_llm_rag" if AIClient(DB).enabled else "retrieval_only"},"roles":["admin","manager","hr","auditor","employee"]}}
        if method=="GET" and path=="/api/overview":return 200,{"ok":True,"data":overview(DB,user)}
        if method=="POST" and path=="/api/tasks":
            body=self.body();return 202,{"ok":True,"data":create_task(DB,user,body.get("input_text",""),body.get("context","auto"))}
        match=self.match(path,"/api/tasks/{id}")
        if method=="GET" and match:return 200,{"ok":True,"data":task_detail(DB,user,match["id"])}
        if method=="GET" and path=="/api/schedules/history":return 200,{"ok":True,"data":schedule_history(DB,user,query.get("start",["2026-08-01"])[0],query.get("end",["2026-08-31"])[0])}
        match=self.match(path,"/api/schedules/{id}/activate")
        if method=="POST" and match:return 200,{"ok":True,"data":activate_plan(DB,user,match["id"])}
        match=self.match(path,"/api/schedules/{id}/publish")
        if method=="POST" and match:return 200,{"ok":True,"data":publish_plan(DB,user,match["id"])}
        if method=="GET" and path=="/api/organization/employees":return 200,{"ok":True,"data":employee_list(DB,user)}
        if method=="POST" and path=="/api/organization/employees":return 201,{"ok":True,"data":save_employee(DB,user,self.body())}
        match=self.match(path,"/api/organization/employees/{id}")
        if method=="PUT" and match:return 200,{"ok":True,"data":save_employee(DB,user,self.body(),match["id"])}
        if method=="GET" and path=="/api/attendance/overview":return 200,{"ok":True,"data":attendance_overview(DB,user,query.get("start",["2026-08-01"])[0],query.get("end",["2026-08-07"])[0])}
        if method=="GET" and path=="/api/anomalies":return 200,{"ok":True,"data":anomalies(DB,user)}
        match=self.match(path,"/api/anomalies/{id}/status")
        if method=="POST" and match:
            body=self.body();return 200,{"ok":True,"data":update_anomaly(DB,user,match["id"],body.get("status",""),body.get("note",""))}
        if method=="GET" and path=="/api/rules":return 200,{"ok":True,"data":rule_list(DB)}
        if method=="POST" and path=="/api/rules":return 201,{"ok":True,"data":create_rule(DB,user,self.body(),AIClient(DB))}
        match=self.match(path,"/api/rules/{id}/approve")
        if method=="POST" and match:return 200,{"ok":True,"data":approve_rule(DB,user,match["id"])}
        match=self.match(path,"/api/employee/requests/{id}/decision")
        if method=="POST" and match:
            body=self.body();return 200,{"ok":True,"data":decide_employee_request(DB,user,match["id"],body.get("status",""),body.get("note",""))}
        if method=="GET" and path=="/api/insights":return 200,{"ok":True,"data":employee_insights(DB,user)}
        if method=="GET" and path=="/api/reviews/current":return 200,{"ok":True,"data":period_review(DB,user)}
        if method=="POST" and path=="/api/employee/agent":return 200,{"ok":True,"data":employee_agent(DB,user,self.body().get("input_text",""))}
        if method=="GET" and path=="/api/employee/portal":return 200,{"ok":True,"data":{"profile":employee_list(DB,user)[0] if employee_list(DB,user) else None,"shifts":schedule_history(DB,user,"2026-08-01","2026-08-31"),"requests":[dict(x) for x in DB.execute("SELECT * FROM employee_requests WHERE employee_id=? ORDER BY created_at DESC",(user.get("employee_id"),))] if user.get("employee_id") else []}}
        if method=="POST" and path=="/api/ai/rag":return 200,{"ok":True,"data":rag_answer(DB,AIClient(DB),user,self.body().get("question",""))}
        if method=="GET" and path=="/api/ai/health":return 200,{"ok":True,"data":{"llm":{"enabled":AIClient(DB).enabled,"model":AIClient(DB).model,"base_url":AIClient(DB).base_url},"rag":{"mode":"live_llm_rag" if AIClient(DB).enabled else "retrieval_only","documents":DB.execute("SELECT COUNT(*) n FROM vector_documents").fetchone()["n"]}}}
        if method=="GET" and path=="/api/automation/events":return 200,{"ok":True,"data":[dict(x) for x in DB.execute("SELECT * FROM automation_events ORDER BY priority DESC,created_at DESC LIMIT 100")]}
        if method=="POST" and path=="/api/automation/events":return 202,{"ok":True,"data":automation_event(DB,user,self.body())}
        if method=="GET" and path=="/api/audit":
            if user["role"] not in ("admin","auditor","hr"):raise ApiError("无审计日志权限",403,"FORBIDDEN")
            return 200,{"ok":True,"data":[dict(x) for x in DB.execute("SELECT * FROM audit_logs ORDER BY occurred_at DESC LIMIT 200")]}
        if method=="GET" and path=="/api/admin/backups":
            if user["role"]!="admin":raise ApiError("无备份权限",403,"FORBIDDEN")
            return 200,{"ok":True,"data":[dict(x) for x in DB.execute("SELECT * FROM backups ORDER BY created_at DESC")]}
        if method=="POST" and path=="/api/admin/backups":return 201,{"ok":True,"data":backup_database(DB,user)}
        raise ApiError("接口不存在",404,"NOT_FOUND")

    def body_context_hint(self):
        return "auto"

    @staticmethod
    def public_user(user):
        return {key:user.get(key) for key in ("id","username","display_name","role","employee_id","store_id")}

    def serve_static(self,path):
        relative="index.html" if path in ("/","/index.html") else path.lstrip("/")
        target=(PUBLIC/relative).resolve()
        if PUBLIC.resolve() not in target.parents and target!=PUBLIC.resolve():raise ApiError("资源不存在",404)
        if not target.is_file():target=PUBLIC/"index.html"
        content=target.read_bytes();self.send_response(200);self.send_header("Content-Type",mimetypes.guess_type(target.name)[0] or "application/octet-stream");self.send_header("Content-Length",str(len(content)));self.send_header("Cache-Control","no-cache");self.end_headers();self.wfile.write(content)

    def dispatch(self,method):
        parsed=urlparse(self.path)
        try:
            if parsed.path.startswith("/api/") or parsed.path=="/healthz":status,data=self.handle_api(method,parsed.path,parse_qs(parsed.query));self.send_json(status,data)
            elif method=="GET":self.serve_static(parsed.path)
            else:raise ApiError("接口不存在",404,"NOT_FOUND")
        except ApiError as exc:self.send_json(exc.status,{"ok":False,"error":str(exc),"code":exc.code,"details":exc.details})
        except Exception as exc:
            traceback.print_exc();self.send_json(500,{"ok":False,"error":"服务器处理失败","code":"INTERNAL_ERROR","details":str(exc) if os.getenv("ENV")!="production" else None})

    def do_GET(self):self.dispatch("GET")
    def do_POST(self):self.dispatch("POST")
    def do_PUT(self):self.dispatch("PUT")


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--host",default=os.getenv("HOST","0.0.0.0"));parser.add_argument("--port",type=int,default=int(os.getenv("PORT","4173")));args=parser.parse_args()
    print(f"[startup] 正在绑定 {args.host}:{args.port}",flush=True)
    server=ThreadingHTTPServer((args.host,args.port),Handler);server.daemon_threads=True
    print(f"[startup] FlowStaff AI 已监听 http://{args.host}:{args.port}",flush=True)
    server.serve_forever()


if __name__=="__main__":main()
