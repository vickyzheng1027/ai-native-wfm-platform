import test from 'node:test';
import assert from 'node:assert/strict';
import { runWorkforceAgent } from './openai-agent.mjs';

test('OpenAI Agent 真实工具循环只执行白名单工具并解析严格结构输出',async()=>{
  const originalFetch=globalThis.fetch;
  const originalKey=process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY='test-key';
  let call=0;
  const called=[];
  globalThis.fetch=async(_url,options)=>{
    const body=JSON.parse(options.body);call+=1;
    assert.equal(body.tools.every(tool=>tool.strict===true),true);
    if(call===1)return new Response(JSON.stringify({id:'resp-1',output:[
      {type:'function_call',name:'get_workforce_context',call_id:'c1',arguments:'{}'},
      {type:'function_call',name:'run_demand_forecast',call_id:'c2',arguments:'{}'},
      {type:'function_call',name:'get_compliance_rules',call_id:'c3',arguments:'{}'}
    ]}),{status:200,headers:{'Content-Type':'application/json'}});
    assert.equal(body.previous_response_id,undefined);
    assert.equal(body.input.filter(item=>item.type==='function_call_output').length,3);
    return new Response(JSON.stringify({id:'resp-2',output:[{type:'message',content:[{type:'output_text',text:JSON.stringify({
      action:'optimize_workforce',store:'旗舰店',eventId:'event-member-day',trafficIncreasePct:35,budgetCeiling:2000,
      minimumCoveragePct:95,complianceRequired:true,laborAccountRequired:true,summary:'已完成编排',rationale:['使用历史数据预测']
    })}]}]}),{status:200,headers:{'Content-Type':'application/json'}});
  };
  try{
    const result=await runWorkforceAgent({prompt:'生成方案',eventId:'event-member-day',safetyIdentifier:'test',tools:{
      get_workforce_context:async()=>{called.push('context');return {employees:8}},
      run_demand_forecast:async()=>{called.push('forecast');return {predictedTraffic:1300}},
      get_compliance_rules:async()=>{called.push('rules');return [{ruleCode:'DAILY-HOURS'}]}
    }});
    assert.deepEqual(called,['context','forecast','rules']);
    assert.equal(result.intent.minimumCoveragePct,95);
    assert.equal(result.responseId,'resp-2');
  }finally{
    globalThis.fetch=originalFetch;
    if(originalKey===undefined)delete process.env.OPENAI_API_KEY;else process.env.OPENAI_API_KEY=originalKey;
  }
});
