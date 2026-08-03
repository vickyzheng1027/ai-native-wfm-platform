import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const pages=['index.html','schedule.html','employee.html'];

test('受安全策略保护的页面不使用会被浏览器拦截的内联点击事件',async()=>{
  for(const page of pages){
    const html=await readFile(new URL(`../public/${page}`,import.meta.url),'utf8');
    assert.doesNotMatch(html,/\sonclick\s*=/i,`${page} 仍包含内联 onclick`);
  }
});

test('三个交互页面均加载外部脚本并注册点击处理',async()=>{
  const expected={
    'index.html':'hub-app.js',
    'schedule.html':'schedule-app.js',
    'employee.html':'employee-app.js'
  };
  for(const [page,script] of Object.entries(expected)){
    const html=await readFile(new URL(`../public/${page}`,import.meta.url),'utf8');
    const js=await readFile(new URL(`../public/${script}`,import.meta.url),'utf8');
    assert.match(html,new RegExp(`<script src=["']${script.replace('.','\\.')}["']`));
    assert.match(html,/<script src=["']ui-feedback\.js["']/);
    assert.match(js,/addEventListener\(['"]click['"]/);
  }
});

test('页面脚本不使用浏览器原生提示框',async()=>{
  for(const script of ['hub-app.js','schedule-app.js','employee-app.js']){
    const js=await readFile(new URL(`../public/${script}`,import.meta.url),'utf8');
    assert.doesNotMatch(js,/(?:^|[^\w$.])(?:window\.)?(?:alert|confirm|prompt)\s*\(/,`${script} 仍使用浏览器原生提示框`);
  }
});
