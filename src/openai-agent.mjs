const API_URL = 'https://api.openai.com/v1/responses';
const DEFAULT_MODEL = process.env.OPENAI_MODEL || 'gpt-5.6-terra';

const toolDefinitions = [
  ['get_workforce_context','读取活动、人员、班次、请假、预算和账户上下文'],
  ['run_demand_forecast','运行可复算的统计需求预测并返回版本与置信区间'],
  ['get_compliance_rules','读取当前生效的版本化合规规则']
].map(([name,description]) => ({ type:'function',name,description,strict:true,parameters:{ type:'object',properties:{},required:[],additionalProperties:false } }));

const resultSchema = {
  type:'object',
  properties:{
    action:{type:'string',enum:['optimize_workforce']},
    store:{type:['string','null']}, eventId:{type:'string'},
    trafficIncreasePct:{type:'number'}, budgetCeiling:{type:'number'}, minimumCoveragePct:{type:'number'},
    complianceRequired:{type:'boolean'}, laborAccountRequired:{type:'boolean'},
    summary:{type:'string'}, rationale:{type:'array',items:{type:'string'}}
  },
  required:['action','store','eventId','trafficIncreasePct','budgetCeiling','minimumCoveragePct','complianceRequired','laborAccountRequired','summary','rationale'],
  additionalProperties:false
};

async function request(body) {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw Object.assign(new Error('尚未配置 OPENAI_API_KEY，真实 AI Agent 不可用'),{ status:503,code:'OPENAI_NOT_CONFIGURED' });
  const response = await fetch(API_URL,{ method:'POST',headers:{ Authorization:`Bearer ${apiKey}`,'Content-Type':'application/json' },body:JSON.stringify(body),signal:AbortSignal.timeout(45000) });
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error?.message || 'OpenAI API 调用失败'),{ status:502,code:'OPENAI_API_ERROR' });
  return data;
}

function outputText(response) {
  return response.output?.flatMap(item => item.content || []).find(content => content.type === 'output_text')?.text || response.output_text || '';
}

export async function runWorkforceAgent({ prompt, eventId, safetyIdentifier, tools, onStep }) {
  const base = {
    model:DEFAULT_MODEL,
    reasoning:{effort:'low'},
    store:false,
    safety_identifier:safetyIdentifier,
    instructions:'你是 WFM 劳动力运营编排 Agent。必须先调用全部三个工具获取真实上下文。你只负责理解、编排和解释，禁止自行计算工时、薪资、成本或合规结论，禁止执行数据库写操作。所有数字必须来自工具结果。',
    tools:toolDefinitions,
    text:{format:{type:'json_schema',name:'workforce_intent',strict:true,schema:resultSchema}}
  };
  let response = await request({ ...base,input:`活动ID：${eventId}\n管理者目标：${prompt}` });
  for (let round=0;round<4;round+=1) {
    const calls = (response.output || []).filter(item => item.type === 'function_call');
    if (!calls.length) {
      const text = outputText(response);
      if (!text) throw Object.assign(new Error('OpenAI 未返回结构化结果'),{status:502,code:'OPENAI_EMPTY_OUTPUT'});
      return { model:DEFAULT_MODEL,responseId:response.id,intent:JSON.parse(text) };
    }
    const outputs = [];
    for (const call of calls) {
      if (!tools[call.name]) throw Object.assign(new Error(`Agent 请求了未授权工具：${call.name}`),{status:502,code:'UNAUTHORIZED_TOOL'});
      const started = Date.now();
      const value = await tools[call.name](JSON.parse(call.arguments || '{}'));
      await onStep?.({ toolName:call.name,input:JSON.parse(call.arguments || '{}'),output:value,durationMs:Date.now()-started });
      outputs.push({ type:'function_call_output',call_id:call.call_id,output:JSON.stringify(value) });
    }
    response = await request({ ...base,input:[...(response.output || []),...outputs] });
  }
  throw Object.assign(new Error('Agent 工具调用轮次超过限制'),{status:502,code:'AGENT_MAX_ROUNDS'});
}

const employeeSchema={
  type:'object',properties:{
    action:{type:'string',enum:['query_schedule','leave','swap','attendance_correction','overtime','punch','preference','unknown']},
    requestDate:{type:['string','null']},startAt:{type:['string','null']},endAt:{type:['string','null']},
    hours:{type:['number','null']},reason:{type:['string','null']},punchType:{type:['string','null'],enum:['clock_in','clock_out',null]},
    summary:{type:'string'},requiresConfirmation:{type:'boolean'}
  },required:['action','requestDate','startAt','endAt','hours','reason','punchType','summary','requiresConfirmation'],additionalProperties:false
};

export async function interpretEmployeeCommand({text,context,safetyIdentifier}) {
  const response=await request({
    model:DEFAULT_MODEL,reasoning:{effort:'low'},store:false,safety_identifier:safetyIdentifier,
    instructions:'你是 WFM 员工事务理解 Agent。结合员工本人排班，把口语诉求转成结构化意图。不得批准申请，不得计算工资或合规结论。日期必须输出 YYYY-MM-DD；信息不足时 action=unknown。查班不需确认，其他写操作必须 requiresConfirmation=true。',
    input:`员工诉求：${text}\n员工本人排班上下文：${JSON.stringify(context)}`,
    text:{format:{type:'json_schema',name:'employee_command',strict:true,schema:employeeSchema}}
  });
  const textOutput=outputText(response);
  if(!textOutput)throw Object.assign(new Error('OpenAI 未返回员工意图'),{status:502,code:'OPENAI_EMPTY_OUTPUT'});
  return {model:DEFAULT_MODEL,responseId:response.id,intent:JSON.parse(textOutput)};
}

export { DEFAULT_MODEL };
