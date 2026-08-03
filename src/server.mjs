import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';
import { createDatabase } from './db.mjs';
import { operationsContext, createAgentRun, getAgentRun, confirmAgentRun, createEmployeeCommand, confirmEmployeeCommand, resetDemoScenario } from './orchestration.mjs';
import {
  DomainError, login, logout, authenticate, dashboard, listEmployees, createEmployee, updateEmployee,
  listStores, listShifts, createShift, createRequest, listRequests, decideRequest, punch,
  generatePlan, executePlan, closeEvent, accountReport, payrollReport, auditReport, understandEmployeeCommand
} from './domain.mjs';

const rootDir = dirname(dirname(fileURLToPath(import.meta.url)));
const databasePath = process.env.DATABASE_PATH || join(rootDir, 'data', 'wfm.db');
const db = createDatabase(databasePath);
const port = Number(process.env.PORT || 4180);
const maxBodyBytes = 128 * 1024;

function securityHeaders(res) {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'SAMEORIGIN');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
}

function sendJson(res, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  securityHeaders(res);
  res.writeHead(status, { 'Content-Type':'application/json; charset=utf-8', 'Content-Length':Buffer.byteLength(body), 'Cache-Control':'no-store', ...headers });
  res.end(body);
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > maxBodyBytes) throw new DomainError('请求内容超过限制', 413, 'PAYLOAD_TOO_LARGE');
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new DomainError('请求 JSON 格式无效', 400, 'INVALID_JSON'); }
}

function cookies(req) {
  return Object.fromEntries(String(req.headers.cookie || '').split(';').map(item => item.trim()).filter(Boolean).map(item => {
    const index = item.indexOf('=');
    return [decodeURIComponent(item.slice(0,index)), decodeURIComponent(item.slice(index+1))];
  }));
}

function tokenFrom(req) {
  const bearer = String(req.headers.authorization || '').match(/^Bearer\s+(.+)$/i);
  return bearer?.[1] || cookies(req).wfm_session;
}

function currentUser(req) {
  return authenticate(db, tokenFrom(req));
}

function routeMatch(pathname, pattern) {
  const keys = [];
  const regex = new RegExp('^' + pattern.replace(/:([A-Za-z]+)/g, (_, key) => { keys.push(key); return '([^/]+)'; }) + '$');
  const match = pathname.match(regex);
  return match ? Object.fromEntries(keys.map((key,index) => [key,decodeURIComponent(match[index+1])])) : null;
}

async function api(req, res, pathname, url) {
  if (req.method === 'POST' && pathname === '/api/auth/login') {
    const body = await readJson(req);
    const result = login(db, String(body.tenantCode || ''), String(body.username || ''), String(body.password || ''));
    const secure = process.env.NODE_ENV === 'production' ? '; Secure' : '';
    return sendJson(res, 200, { ok:true, user:result.user, expiresAt:result.expiresAt }, { 'Set-Cookie':`wfm_session=${encodeURIComponent(result.token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200${secure}` });
  }
  if (req.method === 'POST' && pathname === '/api/auth/logout') {
    logout(db, tokenFrom(req));
    return sendJson(res, 200, { ok:true }, { 'Set-Cookie':'wfm_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0' });
  }
  const user = currentUser(req);
  if (req.method === 'GET' && pathname === '/api/me') return sendJson(res, 200, { ok:true, user });
  if (req.method === 'GET' && pathname === '/api/dashboard') return sendJson(res, 200, { ok:true, data:dashboard(db,user) });
  if (req.method === 'GET' && pathname === '/api/operations/context') return sendJson(res,200,{ok:true,data:operationsContext(db,user,url.searchParams.get('eventId') || undefined)});
  if (req.method === 'POST' && pathname === '/api/admin/demo-reset') return sendJson(res,200,{ok:true,data:resetDemoScenario(db,user)});
  if (req.method === 'GET' && pathname === '/api/stores') return sendJson(res, 200, { ok:true, data:listStores(db,user) });
  if (req.method === 'GET' && pathname === '/api/employees') return sendJson(res, 200, { ok:true, data:listEmployees(db,user) });
  if (req.method === 'POST' && pathname === '/api/employees') return sendJson(res, 201, { ok:true, data:createEmployee(db,user,await readJson(req)) });
  let params = routeMatch(pathname, '/api/employees/:id');
  if (req.method === 'PUT' && params) return sendJson(res, 200, { ok:true, data:updateEmployee(db,user,params.id,await readJson(req)) });
  if (req.method === 'GET' && pathname === '/api/shifts') return sendJson(res, 200, { ok:true, data:listShifts(db,user,url.searchParams.get('eventId') || undefined) });
  if (req.method === 'POST' && pathname === '/api/shifts') return sendJson(res, 201, { ok:true, data:createShift(db,user,await readJson(req)) });
  if (req.method === 'GET' && pathname === '/api/requests') return sendJson(res, 200, { ok:true, data:listRequests(db,user) });
  if (req.method === 'POST' && pathname === '/api/requests') return sendJson(res, 201, { ok:true, data:createRequest(db,user,await readJson(req)) });
  params = routeMatch(pathname, '/api/requests/:id/decision');
  if (req.method === 'POST' && params) {
    const body = await readJson(req);
    return sendJson(res, 200, { ok:true, data:decideRequest(db,user,params.id,body.decision,body.note) });
  }
  if (req.method === 'POST' && pathname === '/api/attendance/punch') return sendJson(res, 201, { ok:true, data:punch(db,user,await readJson(req)) });
  if (req.method === 'POST' && pathname === '/api/ai/employee-intent') {
    const body = await readJson(req);
    return sendJson(res, 200, { ok:true, data:understandEmployeeCommand(db,user,String(body.text || '')) });
  }
  if (req.method === 'POST' && pathname === '/api/ai/plans') {
    const body = await readJson(req);
    return sendJson(res, 201, { ok:true, data:generatePlan(db,user,String(body.prompt || ''),body.eventId) });
  }
  if (req.method === 'POST' && pathname === '/api/agent/runs') return sendJson(res,201,{ok:true,data:await createAgentRun(db,user,await readJson(req))});
  params = routeMatch(pathname, '/api/agent/runs/:id');
  if (req.method === 'GET' && params) return sendJson(res,200,{ok:true,data:getAgentRun(db,user,params.id)});
  params = routeMatch(pathname, '/api/agent/runs/:id/confirm');
  if (req.method === 'POST' && params) return sendJson(res,200,{ok:true,data:confirmAgentRun(db,user,params.id)});
  if (req.method === 'POST' && pathname === '/api/employee/commands') {
    const body=await readJson(req);
    return sendJson(res,201,{ok:true,data:await createEmployeeCommand(db,user,String(body.text||''))});
  }
  params = routeMatch(pathname, '/api/employee/commands/:id/confirm');
  if (req.method === 'POST' && params) return sendJson(res,200,{ok:true,data:confirmEmployeeCommand(db,user,params.id)});
  params = routeMatch(pathname, '/api/ai/plans/:id/execute');
  if (req.method === 'POST' && params) return sendJson(res, 200, { ok:true, data:executePlan(db,user,params.id) });
  params = routeMatch(pathname, '/api/events/:id/close');
  if (req.method === 'POST' && params) return sendJson(res, 200, { ok:true, data:closeEvent(db,user,params.id,await readJson(req)) });
  if (req.method === 'GET' && pathname === '/api/reports/accounts') return sendJson(res, 200, { ok:true, data:accountReport(db,user) });
  if (req.method === 'GET' && pathname === '/api/reports/payroll') return sendJson(res, 200, { ok:true, data:payrollReport(db,user,url.searchParams.get('eventId') || undefined) });
  if (req.method === 'GET' && pathname === '/api/audit') return sendJson(res, 200, { ok:true, data:auditReport(db,user) });
  throw new DomainError('API 不存在', 404, 'NOT_FOUND');
}

async function serve(res, filename, contentType) {
  const body = await readFile(join(rootDir, 'public', filename));
  securityHeaders(res);
  res.writeHead(200, {
    'Content-Type':contentType,
    'Content-Length':body.length,
    'Cache-Control':'no-cache',
    'Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'"
  });
  res.end(body);
}

export const server = http.createServer(async (req,res) => {
  const requestId = randomUUID();
  res.setHeader('X-Request-Id',requestId);
  try {
    const url = new URL(req.url || '/',`http://${req.headers.host || 'localhost'}`);
    if (req.method === 'GET' && url.pathname === '/healthz') return sendJson(res,200,{ ok:true,status:'healthy',database:'sqlite',timestamp:nowIso() });
    if (url.pathname.startsWith('/api/')) return await api(req,res,url.pathname,url);
    const staticFiles = new Map([
      ['/','index.html'],['/index.html','index.html'],['/operations-hub.html','operations-hub.html'],
      ['/home.html','home.html'],['/schedule.html','schedule.html'],['/employee.html','employee.html'],
      ['/hub-app.js','hub-app.js'],['/schedule-app.js','schedule-app.js'],['/employee-app.js','employee-app.js']
    ]);
    if (req.method === 'GET' && staticFiles.has(url.pathname)) {
      const filename = staticFiles.get(url.pathname);
      return await serve(res,filename,filename.endsWith('.js')?'text/javascript; charset=utf-8':'text/html; charset=utf-8');
    }
    throw new DomainError('资源不存在',404,'NOT_FOUND');
  } catch (error) {
    const status = error instanceof DomainError ? error.status : 500;
    if (status >= 500) console.error(`[${requestId}]`,error);
    sendJson(res,status,{ ok:false,error:error.message || '服务异常',code:error.code || 'INTERNAL_ERROR',requestId });
  }
});

function nowIso(){ return new Date().toISOString(); }

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  server.listen(port,'0.0.0.0',() => console.log(`AI Native WFM Platform listening on http://0.0.0.0:${port}`));
}

export { db };
