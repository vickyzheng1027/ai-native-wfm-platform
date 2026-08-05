import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname,join,extname } from 'node:path';
import { fileURLToPath,pathToFileURL } from 'node:url';
import { createDatabase,resetDatabase,parseJson } from './db.mjs';
import * as wfm from './ai-native-engine.mjs';
import { parseRulesWithAgent,parseDemandWithAgent,normalizeDemandAgentItem,isAiConfigured,MODEL } from './openai-agent.mjs';
import * as schedule from './scheduling.mjs';
import * as loop from './closed-loop.mjs';

const root=dirname(dirname(fileURLToPath(import.meta.url))), db=createDatabase(process.env.DATABASE_PATH||join(root,'data','wfm.db')), port=Number(process.env.PORT||4180);
const json=(res,status,data)=>{const body=JSON.stringify(data);res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Content-Length':Buffer.byteLength(body),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(body);};
async function body(req){const parts=[];let n=0;for await(const x of req){n+=x.length;if(n>262144)throw wfm.err('请求内容过大',413);parts.push(x);}try{return parts.length?JSON.parse(Buffer.concat(parts).toString('utf8')):{};}catch{throw wfm.err('JSON格式无效',400);}}
function match(path,pattern){const keys=[];const re=new RegExp('^'+pattern.replace(/:([\w]+)/g,(_,k)=>(keys.push(k),'([^/]+)'))+'$');const m=path.match(re);return m&&Object.fromEntries(keys.map((k,i)=>[k,decodeURIComponent(m[i+1])]));}
async function api(req,res,path,url){let p,b;
 if(req.method==='GET'&&path==='/api/dashboard')return json(res,200,{ok:true,data:wfm.dashboard(db)});
 if(req.method==='GET'&&path==='/api/metadata')return json(res,200,{ok:true,data:{...wfm.metadata(db),ai:{configured:isAiConfigured(),model:MODEL}}});
 if(req.method==='GET'&&path==='/api/employees')return json(res,200,{ok:true,data:schedule.listEmployees(db)});
 if(req.method==='POST'&&path==='/api/employees')return json(res,201,{ok:true,data:schedule.saveEmployee(db,await body(req))});
 p=match(path,'/api/employees/:id');if(req.method==='PUT'&&p)return json(res,200,{ok:true,data:schedule.saveEmployee(db,await body(req),p.id)});
 if(req.method==='GET'&&path==='/api/leaves')return json(res,200,{ok:true,data:schedule.listLeaves(db)});
 if(req.method==='POST'&&path==='/api/leaves')return json(res,201,{ok:true,data:schedule.createLeave(db,await body(req))});
 p=match(path,'/api/leaves/:id/approve');if(req.method==='POST'&&p){b=await body(req);return json(res,200,{ok:true,data:schedule.decideLeave(db,p.id,'approved',b.note)});}
 p=match(path,'/api/leaves/:id/reject');if(req.method==='POST'&&p){b=await body(req);return json(res,200,{ok:true,data:schedule.decideLeave(db,p.id,'rejected',b.note)});}
 p=match(path,'/api/leaves/:id/cancel');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:schedule.decideLeave(db,p.id,'cancelled')});
 if(req.method==='GET'&&path==='/api/calendar')return json(res,200,{ok:true,data:schedule.calendar(db,url.searchParams.get('start'),url.searchParams.get('end'))});
 if(req.method==='GET'&&path==='/api/demands')return json(res,200,{ok:true,data:schedule.listDemands(db,url.searchParams.get('start'),url.searchParams.get('end'),url.searchParams.get('storeId'))});
 if(req.method==='POST'&&path==='/api/demands')return json(res,201,{ok:true,data:schedule.saveDemand(db,await body(req))});
 p=match(path,'/api/demands/:id');if(req.method==='PUT'&&p)return json(res,200,{ok:true,data:schedule.saveDemand(db,await body(req),p.id)});
 if(req.method==='POST'&&path==='/api/demands/parse'){b=await body(req);const text=String(b.text||'').trim();if(!text)throw wfm.err('请输入业务需求',400);const deterministicItems=schedule.parseDemandText(text,db);if(!deterministicItems.length)throw wfm.err('未识别出可执行的业务目标，请补充门店、日期、时段、岗位和人数',422,'INCOMPLETE_DEMAND');let items,summary='',unresolved=[],fallbackReason,modelSource='gaia_agent',agentMeta;try{const metadata=wfm.metadata(db),result=await parseDemandWithAgent(text,metadata);agentMeta=result.agentMeta;items=(result.items||[]).map(x=>normalizeDemandAgentItem(x,metadata));if(!items.length)throw wfm.err('模型未识别出完整需求',422);summary=result.summary;unresolved=result.unresolved||[];}catch(e){items=deterministicItems;modelSource='deterministic_fallback';fallbackReason=e.message;summary='模型调用失败，已用本地规则提取，请人工核对后确认。';}return json(res,200,{ok:true,data:{items,modelSource,model:modelSource==='gaia_agent'?MODEL:null,summary,unresolved,fallbackReason,agentMeta}});}
 if(req.method==='POST'&&path==='/api/schedule-plans/generate')return json(res,201,{ok:true,data:schedule.generatePlans(db,await body(req))});
 p=match(path,'/api/schedule-plans/:id');if(req.method==='GET'&&p)return json(res,200,{ok:true,data:schedule.getPlan(db,p.id)});
 p=match(path,'/api/schedule-plans/:id/confirm');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:schedule.confirmPlan(db,p.id)});
 if(req.method==='GET'&&path==='/api/shifts')return json(res,200,{ok:true,data:schedule.listShifts(db,url.searchParams.get('start'),url.searchParams.get('end'),url.searchParams.get('storeId'))});
 if(req.method==='POST'&&path==='/api/shifts/validate')return json(res,200,{ok:true,data:schedule.validateShift(db,await body(req))});
 if(req.method==='POST'&&path==='/api/shifts')return json(res,201,{ok:true,data:schedule.saveShift(db,await body(req))});
 p=match(path,'/api/shifts/:id');if(req.method==='PUT'&&p)return json(res,200,{ok:true,data:schedule.saveShift(db,await body(req),p.id)});
 if(req.method==='GET'&&path==='/api/closed-loop')return json(res,200,{ok:true,data:loop.closedLoopOverview(db)});
 if(req.method==='POST'&&path==='/api/attendance'){b=await body(req);return json(res,201,{ok:true,data:loop.recordAttendance(db,b)});}
 if(req.method==='POST'&&path==='/api/operational-events'){b=await body(req);return json(res,201,{ok:true,data:loop.createOperationalEvent(db,b)});}
 p=match(path,'/api/operational-events/:id/remediate');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:loop.remediateEvent(db,p.id)});
 p=match(path,'/api/operational-events/:id/accept');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:loop.acceptRemediation(db,p.id,await body(req))});
 if(req.method==='POST'&&path==='/api/feedback/review')return json(res,201,{ok:true,data:loop.runFeedbackReview(db)});
 if(req.method==='POST'&&path==='/api/demo/reset')return json(res,200,{ok:true,data:resetDatabase(db)});
 if(req.method==='GET'&&path==='/api/rules')return json(res,200,{ok:true,data:wfm.activeRules(db)});
 if(req.method==='POST'&&path==='/api/rules/parse'){b=await body(req);let parsed,source='gaia_agent';try{const ai=await parseRulesWithAgent(String(b.text||''),wfm.metadata(db));parsed=wfm.deterministicParse(String(b.text||''),db);parsed.summary=ai.summary;for(const item of parsed.items){const a=ai.items?.find(x=>x.code===item.code);if(a&&a.value!==undefined&&Number(a.confidence)>=.7){item.value=a.value;item.confidence=a.confidence;}}parsed.unresolved=[...new Set([...parsed.unresolved,...(ai.unresolved||[])])];}catch(e){source='deterministic_fallback';parsed=wfm.deterministicParse(String(b.text||''),db);parsed.fallbackReason=e.message;}return json(res,201,{ok:true,data:wfm.saveDraft(db,String(b.text||''),parsed,source)});}
 p=match(path,'/api/rule-drafts/:id/rules/:ruleId');if(req.method==='PUT'&&p)return json(res,200,{ok:true,data:wfm.updateDraftItem(db,p.id,p.ruleId,await body(req))});
 p=match(path,'/api/rule-drafts/:id/activate');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:wfm.activateDraft(db,p.id)});
 p=match(path,'/api/rules/:id/versions');if(req.method==='GET'&&p)return json(res,200,{ok:true,data:db.prepare('SELECT * FROM rule_versions WHERE rule_id=? ORDER BY version DESC').all(p.id).map(x=>({...x,value:parseJson(x.value_json)}))});
 if(req.method==='POST'&&path==='/api/shortages'){b=await body(req);return json(res,201,{ok:true,data:wfm.createShortage(db,b)});}
 if(req.method==='GET'&&path==='/api/shortages')return json(res,200,{ok:true,data:wfm.listShortages(db)});
 p=match(path,'/api/shortages/:id/recommend');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:wfm.recommend(db,p.id)});
 p=match(path,'/api/shortages/:id/suggestions');if(req.method==='GET'&&p)return json(res,200,{ok:true,data:wfm.listSuggestions(db,p.id)});
 p=match(path,'/api/suggestions/:id/confirm');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:wfm.confirmSuggestion(db,p.id)});
 p=match(path,'/api/transfers/:id/validation');if(req.method==='GET'&&p)return json(res,200,{ok:true,data:wfm.validation(db,p.id)});
 if(req.method==='POST'&&path==='/api/rule-optimizations/run')return json(res,201,{ok:true,data:wfm.optimizeRules(db)});
 if(req.method==='GET'&&path==='/api/rule-optimizations')return json(res,200,{ok:true,data:db.prepare('SELECT o.*,r.name rule_name FROM rule_optimization_suggestions o JOIN rules r ON r.id=o.rule_id ORDER BY o.created_at DESC').all()});
 p=match(path,'/api/rule-optimizations/:id/accept');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:wfm.decideOptimization(db,p.id,true)});
 p=match(path,'/api/rule-optimizations/:id/reject');if(req.method==='POST'&&p)return json(res,200,{ok:true,data:wfm.decideOptimization(db,p.id,false)});
 throw wfm.err('接口不存在',404,'NOT_FOUND');
}
async function serve(res,path){const file=path==='/'?'index.html':path.slice(1);if(!['index.html','app.js'].includes(file))throw wfm.err('页面不存在',404);const data=await readFile(join(root,'public',file));res.writeHead(200,{'Content-Type':extname(file)==='.js'?'text/javascript; charset=utf-8':'text/html; charset=utf-8','Content-Length':data.length,'Cache-Control':'no-cache','Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:"});res.end(data);}
export const server=http.createServer(async(req,res)=>{try{const url=new URL(req.url||'/',`http://${req.headers.host}`);if(url.pathname==='/healthz')return json(res,200,{ok:true,status:'healthy'});if(url.pathname.startsWith('/api/'))return await api(req,res,url.pathname,url);if(req.method==='GET')return await serve(res,url.pathname);throw wfm.err('资源不存在',404);}catch(e){console.error(e.status>=500?e:'');json(res,e.status||500,{ok:false,error:e.message||'服务异常',code:e.code||'INTERNAL_ERROR',details:e.details});}});
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)server.listen(port,'0.0.0.0',()=>console.log(`AI Native WFM listening on http://0.0.0.0:${port}`));
export {db};
