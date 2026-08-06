import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import dumps, loads, utcnow


class AIClient:
    def __init__(self, db):
        self.db = db
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.api_key = os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY")
        self.model = os.getenv("WFM_LLM_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.embedding_model = os.getenv("WFM_EMBEDDING_MODEL", "text-embedding-3-small")
        self.timeout = int(os.getenv("WFM_LLM_TIMEOUT_SECONDS", "30"))

    @property
    def enabled(self):
        return bool(self.api_key and self.base_url and self.model)

    def _audit(self, user_id, purpose, status, started, error=None, usage=None):
        duration = int((time.monotonic() - started) * 1000)
        self.db.execute("INSERT INTO ai_invocations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            str(uuid.uuid4()), user_id, purpose, self.base_url, self.model, status, duration,
            int((usage or {}).get("input_tokens", 0)), int((usage or {}).get("output_tokens", 0)),
            str(error)[:300] if error else None, utcnow()))
        self.db.commit()

    def structured(self, user_id, purpose, system, prompt, schema):
        if not self.enabled:
            raise RuntimeError("未配置真实模型 API Key")
        started = time.monotonic()
        payload = {
            "model": self.model,
            "instructions": system,
            "input": prompt,
            "text": {"format": {"type": "json_schema", "name": purpose.replace("-", "_"), "strict": True, "schema": schema}},
        }
        request = urllib.request.Request(
            f"{self.base_url}/responses", data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
            output = data.get("output_text")
            if not output:
                for item in data.get("output", []):
                    for content in item.get("content", []):
                        if content.get("type") in ("output_text", "text"):
                            output = content.get("text")
                            break
            result = json.loads(output)
            self._audit(user_id, purpose, "success", started, usage=data.get("usage"))
            return result
        except Exception as exc:
            self._audit(user_id, purpose, "failed", started, error=exc)
            raise RuntimeError(f"真实模型调用失败：{exc}") from exc

    def embedding(self, user_id, text):
        if not self.enabled:
            return local_embedding(text), "local_hash"
        started=time.monotonic()
        payload={"model":self.embedding_model,"input":text}
        request=urllib.request.Request(f"{self.base_url}/embeddings",data=json.dumps(payload).encode(),method="POST",headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(request,timeout=self.timeout) as response:data=json.loads(response.read().decode())
            vector=data["data"][0]["embedding"]
            self._audit(user_id,"embedding","success",started,usage=data.get("usage"))
            return vector,self.embedding_model
        except Exception as exc:
            self._audit(user_id,"embedding","failed",started,error=exc)
            return local_embedding(text),"local_hash"


def local_embedding(text, dimensions=96):
    vector=[0.0]*dimensions
    normalized="".join(str(text).lower().split())
    for i in range(max(1,len(normalized)-1)):
        token=normalized[i:i+2]
        digest=hashlib.sha256(token.encode()).digest()
        index=int.from_bytes(digest[:2],"big")%dimensions
        vector[index]+=1 if digest[2]%2 else -1
    norm=math.sqrt(sum(x*x for x in vector)) or 1
    return [x/norm for x in vector]


def cosine(a,b):
    if not a or not b:return 0
    return sum(x*y for x,y in zip(a,b))/(math.sqrt(sum(x*x for x in a))*math.sqrt(sum(y*y for y in b)) or 1)


INTENT_SCHEMA={"type":"object","additionalProperties":False,"properties":{
    "intent":{"type":"string","enum":["schedule_create","schedule_query","leave_request","swap_request","adjust_request","preference_update","attendance_query","employee_query","rule_query","anomaly_query","general_wfm"]},
    "context":{"type":"string","enum":["store_management","my_affairs"]},
    "summary":{"type":"string"},"confidence":{"type":"number"},
    "parameters":{"type":"object","additionalProperties":False,"properties":{
        "start_date":{"type":["string","null"]},"end_date":{"type":["string","null"]},"store_code":{"type":["string","null"]},"role":{"type":["string","null"]},"headcount":{"type":["integer","null"]},"demand_items":{"type":"array","items":{"type":"object","additionalProperties":False,"properties":{"role":{"type":"string"},"headcount":{"type":"integer"}},"required":["role","headcount"]}},"activity_type":{"type":["string","null"]},"peak_periods":{"type":"array","items":{"type":"string","enum":["morning","afternoon","evening"]}},"overtime_control":{"type":"boolean"},"night_shift_control":{"type":"boolean"},"coverage_target":{"type":["number","null"]},"cost_increase_limit":{"type":["number","null"]},"leave_date":{"type":["string","null"]},"leave_type":{"type":["string","null"]},"start_time":{"type":["string","null"]},"end_time":{"type":["string","null"]},"shift_id":{"type":["string","null"]},"preference_type":{"type":["string","null"]},"preference_value":{"type":["string","null"]}},"required":["start_date","end_date","store_code","role","headcount","demand_items","activity_type","peak_periods","overtime_control","night_shift_control","coverage_target","cost_increase_limit","leave_date","leave_type","start_time","end_time","shift_id","preference_type","preference_value"]}},
    "required":["intent","context","summary","confidence","parameters"]}


def classify_intent(client,user,text,requested_context="auto"):
    today=datetime.now(ZoneInfo(os.getenv("WFM_TIMEZONE","Asia/Shanghai"))).date().isoformat()
    system=f"""你是 FlowStaff AI 的意图理解 Agent，只负责把用户原话转换成 WFM 结构化参数，不负责扩大需求。
当前业务日期是 {today}。主管既可能管理门店，也可能办理本人事务。
日期规则：理解今天、明天、本周五、下周等相对日期，start_date 和 end_date 必须输出 YYYY-MM-DD。
人数与岗位规则：demand_items 必须完整保留用户提到的每一个岗位及对应人数，不能只取第一个。单岗位时同时填写 role/headcount；多岗位时 role/headcount 返回 null，以 demand_items 为准。明确人数禁止依据历史数据扩大，禁止增加用户未要求的岗位。
业务目标规则：识别促销、盘点、新店开业等 activity_type；早晨、上午映射 morning，下午映射 afternoon，晚上、晚间映射 evening；识别控制加班和夜班目标。
缺失规则：没有输入的日期、人数、岗位或业务目标必须返回 null，禁止猜测或使用示例值。
请假规则：识别请假日期、假期类型、开始和结束时间。只有用户明确表达生病、年假、调休或事假时才判断相应类型；无法确定时 leave_type 返回 null，由业务服务结合原因和余额给出可解释建议。
示例1：“本周五只需要一名导购”应输出 demand_items=[{{"role":"导购","headcount":1}}]，并填写 role=导购、headcount=1。
示例2：“本周五要一名导购、一名收银员”应输出 demand_items=[{{"role":"导购","headcount":1}},{{"role":"收银员","headcount":1}}]，role/headcount 返回 null。"""
    prompt=f"用户角色：{user['role']}；可访问门店：{user.get('store_id') or '组织范围'}；上下文选择：{requested_context}；用户输入：{text}"
    if client.enabled:
        result=client.structured(user["id"],"intent_classification",system,prompt,INTENT_SCHEMA)
        result["mode"]="live_llm"
        if requested_context in ("store_management","my_affairs"):result["context"]=requested_context
        return result
    lowered=text.lower()
    mappings=[("leave_request",["请假","休假"]),("swap_request",["换班"]),("adjust_request",["调班"]),("preference_update",["偏好","不想排","不要排","希望排","早班","晚班"]),("attendance_query",["考勤","迟到","打卡"]),("schedule_query",["我的班","班表查询","下一班"]),("schedule_create",["排班","客流","需要","覆盖率"]),("rule_query",["规则","合规"]),("anomaly_query",["异常","风险"]),("employee_query",["员工","技能"]) ]
    intent=next((name for name,words in mappings if any(w in lowered for w in words)),"general_wfm")
    context=requested_context if requested_context!="auto" else ("my_affairs" if intent in {"leave_request","swap_request","adjust_request","preference_update","schedule_query","attendance_query"} and user["employee_id"] else "store_management")
    return {"intent":intent,"context":context,"summary":"本地规则完成基础路由，未调用大模型","confidence":.55,"parameters":{},"mode":"deterministic_fallback","fallback_reason":"未配置真实模型 API Key"}


def rag_answer(db,client,user,question):
    query_vector,embedding_model=client.embedding(user["id"],question)
    documents=[]
    for row in db.execute("SELECT * FROM vector_documents").fetchall():
        vector=loads(row["embedding_json"])
        if not vector:
            vector=local_embedding(row["content"])
        documents.append((cosine(query_vector,vector),dict(row)))
    top=sorted(documents,key=lambda x:x[0],reverse=True)[:5]
    sources=[{"id":row["id"],"type":row["source_type"],"title":row["title"],"score":round(score,3),"excerpt":row["content"][:180]} for score,row in top]
    if not client.enabled:
        return {"answer":"已完成知识检索，但当前未配置真实模型，以下来源需要人工阅读后判断。","reasoning_steps":["将问题转换为本地哈希向量","按余弦相似度检索知识库","返回相关来源，不生成模型结论"],"citations":[x["id"] for x in sources],"confidence":round(top[0][0],2) if top else 0,"mode":"retrieval_only","sources":sources}
    schema={"type":"object","additionalProperties":False,"properties":{"answer":{"type":"string"},"reasoning_steps":{"type":"array","items":{"type":"string"}},"citations":{"type":"array","items":{"type":"string"}},"confidence":{"type":"number"}},"required":["answer","reasoning_steps","citations","confidence"]}
    context="\n".join(f"[{x['id']}] {x['title']}: {x['excerpt']}" for x in sources)
    result=client.structured(user["id"],"rag_answer","你只能依据给定资料回答。区分事实、推断和建议，不得绕过硬约束或人工审批。",f"问题：{question}\n资料：\n{context}",schema)
    return {**result,"mode":"live_llm_rag","sources":sources,"embedding_model":embedding_model}
