var hub={context:null,run:null,chosen:null,phase:0,busy:false,user:null};
var $=function(id){return document.getElementById(id)};
var GOAL='请根据数据库中的历史客流预测本次会员日需求，在活动预算内保障覆盖率不低于95%，全程遵守生效规则，并把新增工时计入活动劳动力账户。';

async function api(path,options){
  options=options||{};
  var response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},body:options.body?JSON.stringify(options.body):undefined});
  var data=await response.json();
  if(!response.ok){var error=new Error(data.error||'请求失败');error.code=data.code;throw error}
  return data.data||data;
}
function esc(value){return String(value==null?'':value).replace(/[&<>"']/g,function(char){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]})}
function money(value){return '¥'+Number(value||0).toLocaleString('zh-CN',{maximumFractionDigits:0})}
function showError(error){alert(error.message+(error.code?'（'+error.code+'）':''))}

async function ensureSession(){
  try{hub.user=(await api('/api/me')).user;return true}catch(error){}
  var layer=document.createElement('div');
  layer.id='authLayer';
  layer.innerHTML='<form id="authForm"><h2>登录 Workforce Copilot</h2><p>使用真实账户进入劳动力运营中枢</p><label>租户编码<input id="authTenant" value="DEMO" required></label><label>用户名<input id="authUser" value="manager" required></label><label>密码<input id="authPassword" type="password" value="Demo@2026" required></label><button type="submit">登录</button><div id="authError"></div></form>';
  var style=document.createElement('style');
  style.textContent='#authLayer{position:fixed;inset:0;z-index:999;background:#f4f6f8;display:flex;align-items:center;justify-content:center}#authForm{width:380px;background:#fff;border:1px solid #e8eaed;border-radius:8px;padding:24px;box-shadow:0 8px 24px rgba(0,0,0,.12)}#authForm h2{font-size:20px;margin-bottom:8px}#authForm p{color:#86909c;font-size:13px;margin-bottom:20px}#authForm label{display:block;font-size:12px;color:#4e5969;margin-bottom:12px}#authForm input{width:100%;height:36px;border:1px solid #d8dce3;border-radius:4px;padding:0 12px;margin-top:4px}#authForm button{width:100%;height:38px;border:0;border-radius:4px;background:#00b578;color:#fff;font-weight:700;cursor:pointer}#authError{color:#d4380d;font-size:12px;margin-top:8px}';
  document.head.appendChild(style);document.body.appendChild(layer);
  return new Promise(function(resolve){$('authForm').onsubmit=async function(event){event.preventDefault();try{var result=await api('/api/auth/login',{method:'POST',body:{tenantCode:$('authTenant').value,username:$('authUser').value,password:$('authPassword').value}});hub.user=result.user;layer.remove();resolve(true)}catch(error){$('authError').textContent=error.message}}});
}

async function init(){
  await ensureSession();
  if(hub.user.role==='employee'){location.href='employee.html';return}
  await loadContext();
}
async function loadContext(){
  hub.context=await api('/api/operations/context');
  var context=hub.context,event=context.event,metrics=context.metrics;
  $('tbEvent').textContent=event.name;
  $('tbTime').textContent=['活动前','活动中','活动后'][hub.phase];
  $('tbBudget').textContent=money(event.budget);
  $('tbCoverage').textContent=metrics.coverage+'%';
  $('tbCoverage').className=metrics.coverage>=95?'ok':'warn';
  $('tbCompliance').textContent=context.rules.length+' 条规则';
  $('mCov').textContent=metrics.coverage+'%';$('mCov').className='v '+(metrics.coverage>=95?'ok':'warn');
  $('mBudget').textContent=money(metrics.budgetRemaining);
  renderPhases();renderModules();renderInitialChanges();renderForecast();
}
function renderPhases(){
  var phases=[['活动前','预测 · 方案 · 确认'],['活动中','执行 · 考勤 · 异常'],['活动后','结算 · 复盘 · 进化']];
  $('phases').innerHTML=phases.map(function(item,index){return '<button type="button" class="phase '+(index===hub.phase?'active':index<hub.phase?'done':'')+'" data-phase="'+index+'"><div class="pn">'+(index+1)+'</div><div class="pt"><b>'+item[0]+'</b><span>'+item[1]+'</span></div></button>'}).join('');
}
function renderModules(){
  $('modules').innerHTML=hub.context.modules.map(function(module){return '<div class="mod"><span class="md"></span>'+esc(module.name)+'<small>API</small></div>'}).join('');
}
function renderInitialChanges(){
  var requests=hub.context.requests.filter(function(request){return request.status==='pending'});
  $('changes').innerHTML=requests.length?requests.map(function(request){return '<div class="change-item">'+esc(request.employee)+' · '+esc(request.requestType)+' · '+request.hours+'h · 待审批</div>'}).join(''):'<div class="empty">当前无待处理员工申请</div>';
}
function renderForecast(){
  if(!hub.context.forecast)return;
  var forecast=hub.context.forecast;
  $('changes').innerHTML='<div class="change-item">预测 v'+forecast.version+'：客流 '+forecast.predictedTraffic+'（'+forecast.lowerBound+'-'+forecast.upperBound+'），建议 '+forecast.requiredHeadcount+' 人，置信度 '+Math.round(forecast.confidence*100)+'%</div>'+$('changes').innerHTML;
}
function fillGoal(){$('goalInput').value=GOAL}

async function generate(){
  var prompt=$('goalInput').value.trim();if(!prompt||hub.busy)return;
  var engineLabel=hub.context.ai.mode==='openai'?'OpenAI Responses API':'规则 + 统计预测引擎';
  hub.busy=true;$('genBtn').disabled=true;$('aiStateBox').style.display='block';$('plans').innerHTML='';$('canvas').innerHTML='<div class="task"><div class="ti">AI</div><div class="tc"><b>正在运行劳动力编排</b><div class="tool">'+engineLabel+' · '+esc(hub.context.ai.model)+'</div><div>读取数据库上下文并运行统计预测，通常需要数秒。</div></div><div class="st">…</div></div>';
  $('aiNow').textContent='RUNNING';
  try{
    hub.run=await api('/api/agent/runs',{method:'POST',body:{prompt:prompt,eventId:'event-member-day'}});
    var details=await api('/api/agent/runs/'+hub.run.id);
    $('aiNow').textContent=details.status;
    renderSteps(details.steps);hub.chosen=hub.run.plan.option;renderPlans(hub.run.plan.alternatives);
    applyChosen(hub.run.plan.option);$('confirmBar').style.display='block';
  }catch(error){$('aiNow').textContent='NEEDS_ATTENTION';$('canvas').innerHTML+='<div class="task risk"><div class="ti">!</div><div class="tc"><b>Agent 运行失败</b><div>'+esc(error.message)+'</div></div></div>';showError(error)}
  finally{hub.busy=false;$('genBtn').disabled=false}
}
function renderSteps(steps){
  $('canvas').innerHTML=steps.map(function(step){return '<div class="task '+(step.status==='failed'?'risk':'')+'"><div class="ti">'+(step.toolName?'T':'AI')+'</div><div class="tc"><b>'+esc(step.state)+'</b><div class="tool">'+esc(step.toolName||'agent_state')+'</div><div>'+(step.toolName?'真实工具耗时 '+step.durationMs+'ms':'状态已持久化')+'</div></div><div class="st">'+(step.status==='failed'?'!':'✓')+'</div></div>'}).join('');
}
function renderPlans(plans){
  $('plans').innerHTML=plans.map(function(plan){var impact=plan.impact,comp=plan.compliance,selected=hub.chosen&&hub.chosen.id===plan.id;return '<button type="button" class="plan '+(plan.recommended?'rec ':'')+(selected?'selected':'')+'" data-plan-id="'+esc(plan.id)+'">'+(plan.recommended?'<div class="rec-tag">引擎推荐</div>':'')+'<h4>'+esc(plan.name)+(selected?' · 已选择':'')+'</h4><div class="row"><span>覆盖率</span><b>'+impact.coverageBefore+'% → '+impact.coverageAfter+'%</b></div><div class="row"><span>新增成本</span><b>'+money(impact.addedCost)+'</b></div><div class="row"><span>预算余量</span><b>'+money(impact.budgetRemaining)+'</b></div><div class="row"><span>影响员工</span><b>'+impact.affectedEmployees+' 人</b></div><div class="comp '+(comp.passed?'ok':'no')+'">'+(comp.passed?'可进入人工确认':'硬规则拦截，仅供比较')+'</div></button>'}).join('');
}
async function pickPlan(id){
  if(!hub.run||hub.busy)return;var plan=hub.run.plan.alternatives.find(function(item){return item.id===id});if(!plan)return;
  hub.busy=true;try{var selected=await api('/api/agent/runs/'+hub.run.id+'/select',{method:'POST',body:{optionId:id}});hub.chosen=selected.option;hub.run.plan.option=selected.option;renderPlans(hub.run.plan.alternatives);applyChosen(selected.option)}catch(error){showError(error)}finally{hub.busy=false}
}
function applyChosen(option){
  $('mCost').textContent=money(option.cost);$('mEmp').textContent=option.affectedEmployees+' 人';
  $('mBudget').textContent=money(hub.context.event.budget-hub.context.event.spent-option.cost);
  $('guardWrap').innerHTML='<div class="guard '+(option.checks.every(function(check){return check.passed})?'ok':'')+'"><h4>确定性合规引擎</h4>'+option.checks.map(function(check){return '<div class="item">'+(check.passed?'✓':'⛔')+' <span><b>'+esc(check.name)+'</b>：'+esc(check.evidence)+'</span></div>'}).join('')+'</div>';
  $('changes').innerHTML=option.candidates.map(function(candidate){return '<div class="change-item">跨店支援：'+esc(candidate.name)+' · '+esc(candidate.store)+' → 旗舰店</div>'}).join('')+option.extensions.map(function(item){return '<div class="change-item">延长班次：'+esc(item.name)+' · 延长 '+item.hours+'h</div>'}).join('')+'<div class="change-item">当前选择：'+esc(option.name)+'；新增工时写入活动劳动力账户</div>';
  var blocked=option.checks.some(function(check){return !check.passed&&check.blocking!==false});$('confirmBtn').disabled=blocked;$('confirmBtn').textContent=blocked?'当前方案被硬规则拦截':'确认执行当前方案';
}
async function confirmPlan(){
  if(!hub.run||hub.busy)return;hub.busy=true;$('confirmBtn').disabled=true;
  try{var result=await api('/api/agent/runs/'+hub.run.id+'/confirm',{method:'POST'});$('resultGrid').innerHTML='<div class="rg"><div class="v">'+result.execution.addedShifts+'</div><div class="l">新增真实班次</div></div><div class="rg"><div class="v">'+esc(result.status)+'</div><div class="l">Agent 状态</div></div>';$('resultCard').className='result-card show';$('confirmBar').style.display='none';hub.phase=1;await loadContext();gotoPhase(1)}catch(error){showError(error)}finally{hub.busy=false;$('confirmBtn').disabled=false}
}
function gotoPhase(index){hub.phase=index;$('tbTime').textContent=['活动前','活动中','活动后'][index];renderPhases();$('panelDuring').className='phase-panel'+(index===1?' show':'');$('panelAfter').className='phase-panel'+(index===2?' show':'');if(index===1)renderDuring();if(index===2)renderAfter()}
function renderDuring(){
  $('duringBody').innerHTML='<div class="mini-card"><b>执行状态来自数据库</b><br>当前有效班次 '+hub.context.metrics.planned+' 个，待审批申请 '+hub.context.metrics.pendingCount+' 项。请到员工端进行打卡、请假、补卡和加班。</div>';
}
function renderAfter(){
  var feedback=hub.context.feedback||[];
  $('afterBody').innerHTML='<div class="mini-card"><b>活动结算与复盘</b><br>填写实际经营结果，系统将结合数据库考勤计算工时、账户、薪资和人效。<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px"><label>实际客流<br><input id="actualTraffic" type="number" min="0" step="1" placeholder="例如 1420" style="padding:8px;border:1px solid #cbd5e1;border-radius:4px"></label><label>实际销售额<br><input id="actualSales" type="number" min="0" step="0.01" placeholder="例如 180000" style="padding:8px;border:1px solid #cbd5e1;border-radius:4px"></label></div><button type="button" class="btn btn-primary" style="margin-top:10px" data-action="close-event">确认并执行结算</button></div>';
  $('fbLbl').style.display='block';$('fbWrap').innerHTML=feedback.length?feedback.map(function(item){return '<div class="fb-card"><b>'+esc(item.metric_key)+'</b><div>'+item.before_value+' → '+item.after_value+'</div><small>'+esc(item.evidence)+'</small></div>'}).join(''):'<div class="empty">尚未产生可信反哺记录</div>';
}
async function closeAndReview(){var traffic=Number($('actualTraffic').value);var sales=Number($('actualSales').value);if(!Number.isFinite(traffic)||traffic<0||!Number.isFinite(sales)||sales<0){alert('请填写有效的实际客流和实际销售额');return}if(!window.confirm('确认以客流 '+traffic+'、销售额 '+money(sales)+' 结算？结算后将生成工时、成本、薪资和反哺数据。'))return;try{var result=await api('/api/events/event-member-day/close',{method:'POST',body:{actualTraffic:traffic,actualSales:sales}});alert('结算完成：'+result.processed+'个班次，'+result.totalHours+'小时，成本'+money(result.totalCost));await loadContext();renderAfter()}catch(error){showError(error)}}
async function resetDemo(){if(!window.confirm('确认清空演示租户的业务执行数据并恢复初始班次？该操作会写入审计日志。'))return;try{await api('/api/admin/demo-reset',{method:'POST'});hub.run=null;hub.chosen=null;hub.phase=0;$('plans').innerHTML='';$('canvas').innerHTML='';$('aiStateBox').style.display='none';$('resultCard').className='result-card';$('confirmBar').style.display='none';await loadContext()}catch(error){showError(error)}}
function handleClick(event){
  var phase=event.target.closest('[data-phase]');if(phase){gotoPhase(Number(phase.dataset.phase));return}
  var plan=event.target.closest('[data-plan-id]');if(plan){pickPlan(plan.dataset.planId);return}
  var action=event.target.closest('[data-action]');if(!action)return;
  ({reset:resetDemo,schedule:function(){location.href='schedule.html'},employee:function(){location.href='employee.html'},'fill-goal':fillGoal,generate:generate,'confirm-plan':confirmPlan,'close-event':closeAndReview}[action.dataset.action]||function(){})();
}
document.addEventListener('DOMContentLoaded',function(){document.addEventListener('click',handleClick);init()});
