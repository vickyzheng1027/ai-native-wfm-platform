var emp={busy:false,user:null,pending:null};
var $=function(id){return document.getElementById(id)};var chat=null;
function esc(value){return String(value==null?'':value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function scrollBottom(){chat.scrollTop=chat.scrollHeight}
function userBubble(text){var d=document.createElement('div');d.className='b-user';d.innerHTML='<div class="bubble">'+esc(text)+'</div>';chat.appendChild(d);scrollBottom()}
function aiBubble(html){var d=document.createElement('div');d.className='b-ai';d.innerHTML='<div class="av">AI</div><div class="bubble">'+html+'</div>';chat.appendChild(d);scrollBottom();return d}
function typing(){return aiBubble('<div class="typing"><i></i><i></i><i></i></div>')}
async function api(path,options){options=options||{};var response=await fetch(path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},body:options.body?JSON.stringify(options.body):undefined});var body=await response.json();if(!response.ok){var error=new Error(body.error||'请求失败');error.code=body.code;throw error}return body.data||body}
async function login(){
  try{emp.user=(await api('/api/me')).user;if(emp.user.role!=='employee')throw new Error('请使用员工账号登录');return}catch(error){}
  var username=window.prompt('员工账号','employee');if(!username)throw new Error('需要登录');
  var password=window.prompt('员工密码','Demo@2026');
  var result=await api('/api/auth/login',{method:'POST',body:{tenantCode:'DEMO',username:username,password:password}});emp.user=result.user;
  if(emp.user.role!=='employee'){await api('/api/auth/logout',{method:'POST'});throw new Error('该账号不是员工角色')}
}
function quick(text){$('input').value=text;onSend()}
async function onSend(){var text=$('input').value.trim();if(!text||emp.busy)return;$('input').value='';userBubble(text);emp.busy=true;var loading=typing();try{var command=await api('/api/employee/commands',{method:'POST',body:{text:text}});loading.remove();renderCommand(command)}catch(error){loading.remove();aiBubble('<h5>处理失败</h5>'+esc(error.message)+'<div class="note warn">系统没有使用模拟结果代替真实 AI 调用。</div>')}finally{emp.busy=false}}
function scheduleHtml(schedules){if(!schedules||!schedules.length)return '<div class="note">你当前没有有效班次。</div>';return '<div class="sc-card">'+schedules.map(function(shift){return '<div class="sc-row"><span><span class="sc-day">'+esc(shift.shiftDate)+'</span><br><span class="sc-time">'+esc(shift.store)+'</span></span><span><span class="sc-shift">'+esc(shift.roleRequired)+'</span><br><span class="sc-time">'+esc(shift.startAt)+'-'+esc(shift.endAt)+'</span></span></div>'}).join('')+'</div>'}
function renderCommand(command){var intent=command.intent;if(command.status==='completed'&&command.result){aiBubble('<h5>我的真实排班</h5>'+scheduleHtml(command.result.schedules)+'<div class="note">数据来自后端数据库，刷新后仍保持一致。</div>');return}
  if(intent.action==='unknown'){aiBubble('<h5>还需要一点信息</h5>'+esc(intent.summary));return}
  emp.pending=command;
  aiBubble('<h5>AI 已理解你的诉求</h5><div class="sc-card"><div class="sc-row"><span class="sc-day">动作</span><span>'+esc(intent.action)+'</span></div><div class="sc-row"><span class="sc-day">日期</span><span>'+esc(intent.requestDate||'待补充')+'</span></div><div class="sc-row"><span class="sc-day">时长</span><span>'+esc(intent.hours==null?'--':intent.hours+'h')+'</span></div><div class="sc-row"><span class="sc-day">说明</span><span>'+esc(intent.summary)+'</span></div></div><div class="note warn">这是一项数据库写操作，确认后才会提交。</div><button class="tag-ok" id="confirmEmployeeCommand">确认提交</button>');
  $('confirmEmployeeCommand').addEventListener('click',confirmCommand);
}
async function confirmCommand(){if(!emp.pending||emp.busy)return;emp.busy=true;try{var result=await api('/api/employee/commands/'+emp.pending.id+'/confirm',{method:'POST'});aiBubble('<h5>提交成功</h5>状态已写入数据库：'+esc(result.status));emp.pending=null}catch(error){aiBubble('<h5>未能提交</h5>'+esc(error.message)+'<div class="note warn">未发生静默写入，请按提示修改诉求或联系主管。</div>')}finally{emp.busy=false}}
window.addEventListener('DOMContentLoaded',async function(){chat=$('chat');document.addEventListener('click',function(event){var target=event.target.closest('[data-quick]');if(target)quick(target.dataset.quick)});$('sendEmployeeCommand').addEventListener('click',onSend);try{await login();$('uName').textContent=emp.user.displayName;aiBubble('<h5>你好，'+esc(emp.user.displayName)+'</h5>我是连接真实 WFM 数据与 OpenAI 的员工伙伴。查班会直接返回；请假、换班、补卡、加班、打卡和排班偏好会在你确认后写入系统。')}catch(error){aiBubble('<h5>登录失败</h5>'+esc(error.message))}$('input').addEventListener('keydown',function(event){if(event.key==='Enter'){event.preventDefault();onSend()}})});
