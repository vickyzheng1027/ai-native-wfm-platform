import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname,join,extname } from 'node:path';
import { fileURLToPath,pathToFileURL } from 'node:url';
import { createDatabase,resetDatabase,parseJson } from './db.mjs';
import * as wfm from './ai-native-engine.mjs';
import { parseRulesWithAgent,isAiConfigured,MODEL } from './openai-agent.mjs';

const root=dirname(dirname(fileURLToPath(import.meta.url))), db=createDatabase(process.env.DATABASE_PATH||join(root,'data','wfm.db')), port=Number(process.env.PORT||4180);
const json=(res,status,data)=>{const body=JSON.stringify(data);res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Content-Length':Buffer.byteLength(body),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(body);};
async function body(req){const parts=[];let n=0;for await(const x of req){n+=x.length;if(n>262144)throw wfm.err('请求内容过大',413);parts.push(x);}try{return parts.length?JSON.parse(Buffer.concat(parts).toString('utf8')):{};}catch{throw wfm.err('JSON格式无效',400);}}
function match(path,pattern){const keys=[];const re=new RegExp('^'+pattern.replace(/:([\w]+)/g,(_,k)=>(keys.push(k),'([^/]+)'))+'$');const m=path.match(re);return m&&Object.fromEntries(keys.map((k,i)=>[k,decodeURIComponent(m[i+1])]));}
async function api(req,res,path){let p,b;
 if(req.method==='GET'&&path==='/api/dashboard')return json(res,200,{ok:true,data:wfm.dashboard(db)});
 if(req.method==='GET'&&path==='/api/metadata')return json(res,200,{ok:true,data:wfm.metadata(db),ai:{configured:isAiConfigured(),model:MODEL}});
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
export const server=http.createServer(async(req,res)=>{try{const url=new URL(req.url||'/',`http://${req.headers.host}`);if(url.pathname==='/healthz')return json(res,200,{ok:true,status:'healthy'});if(url.pathname.startsWith('/api/'))return await api(req,res,url.pathname);if(req.method==='GET')return await serve(res,url.pathname);throw wfm.err('资源不存在',404);}catch(e){console.error(e.status>=500?e:'');json(res,e.status||500,{ok:false,error:e.message||'服务异常',code:e.code||'INTERNAL_ERROR'});}});
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)server.listen(port,'0.0.0.0',()=>console.log(`AI Native WFM listening on http://0.0.0.0:${port}`));
export {db};
