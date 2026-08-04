const baseUrl=()=>String(process.env.OPENAI_BASE_URL||'https://coding.gaiaworks.net/openai/v1').replace(/\/+$/,'');
const apiKey=()=>String(process.env.CODEX_API_KEY||process.env.OPENAI_API_KEY||'').trim();
export const MODEL=process.env.OPENAI_MODEL||'gpt-5.5';
export const isAiConfigured=()=>Boolean(apiKey());
function outputText(data){return data.output_text||data.output?.flatMap(x=>x.content||[]).find(x=>x.type==='output_text')?.text||'';}
async function once(input,instructions){
 const response=await fetch(`${baseUrl()}/responses`,{method:'POST',headers:{Authorization:`Bearer ${apiKey()}`,'Content-Type':'application/json'},body:JSON.stringify({model:MODEL,store:false,reasoning:{effort:'low'},instructions,input,text:{format:{type:'json_object'}}}),signal:AbortSignal.timeout(10000)});
 const data=await response.json(); if(!response.ok)throw new Error(data.error?.message||'模型网关调用失败'); const text=outputText(data);if(!text)throw new Error('模型未返回内容');return JSON.parse(text);
}
async function withRetry(input,instructions){if(!isAiConfigured())throw new Error('未配置公司模型凭证');let last;for(let i=0;i<2;i++){try{return await once(input,instructions);}catch(e){last=e;}}throw last;}
export async function parseRulesWithAgent(text,metadata){return withRetry(`用户规则：${text}\n系统元数据：${JSON.stringify(metadata)}`,'你是WFM规则配置Agent。只抽取明确规则，不创造门店或数字。输出JSON：items数组，每项含code,value,confidence；unresolved数组；summary。code只能是MONTHLY_HOURS、CONSECUTIVE_DAYS、SKILL_REQUIRED、CROSS_STORE_REQUIRED、TRAVEL_COST_LIMIT。');}
export async function explainCandidates(context){return withRetry(JSON.stringify(context),'你是WFM补位推荐解释Agent。只能使用输入数字，不能改分数或合规结论。输出JSON：reasons对象，键为员工ID，值为一句中文推荐理由。');}
