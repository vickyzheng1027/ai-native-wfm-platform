# FlowStaff AI

AI Native 劳动力管理系统，依据《FlowStaff_AI_WFM_完整需求文档_v3.0》重写。系统采用 Python 3.12、SQLite、OR-Tools CP-SAT、OpenAI 兼容 Responses/Embedding API 和无框架 SPA。

## 真实能力

- PBKDF2-SHA256 账号密码、Bearer 会话、角色与数据范围双重权限校验
- 统一自然语言 Agent，自动识别门店管理与本人事务
- 真实模型意图识别、规则解析和 RAG；未配置模型时明确返回降级模式
- 六阶段异步排班任务，两套独立方案，推荐、生效、发布严格分离
- OR-Tools CP-SAT 硬约束求解，依赖不可用时明确标识启发式降级
- 员工班表查询、请假/换班/调班申请和自然语言偏好
- 员工、技能认证、考勤矩阵、假期额度、异常处置、规则治理
- 自动事件监听、幂等接入和需主管审批的重排任务
- AI 调用、登录、任务、规则、发布、异常、事件与备份审计
- SQLite 在线备份与校验和

## 本地运行

```bash
python3.12 -m pip install -r backend/requirements.txt
python3.12 -m unittest discover -s tests -v
python3.12 backend/app.py --host 127.0.0.1 --port 4173
```

打开 `http://127.0.0.1:4173`。

现有 Render 服务如果仍是 Node runtime，可以继续使用原来的 `npm test` 和 `npm start`：`package.json` 会安装 Python 依赖、执行测试并启动 Python 后端。新建 Blueprint 服务则直接使用 `render.yaml` 的 Python runtime。

测试账号：

- 主管：`manager / Manager123!`
- 员工：`employee / Employee123!`
- 管理员：`admin / FlowStaff123!`
- HR：`hr / FlowHR123!`
- 审计员：`auditor / Audit12345!`

## 真实 AI 配置

```bash
export OPENAI_BASE_URL="https://coding.gaiaworks.net/openai/v1"
export OPENAI_API_KEY="公司内部 API Key"
export WFM_LLM_MODEL="gpt-5.5"
export WFM_EMBEDDING_MODEL="text-embedding-3-small"
export WFM_LLM_TIMEOUT_SECONDS="45"
```

密钥只从环境变量读取。系统不会把密钥写入前端、数据库或日志。可通过 `GET /api/ai/health` 查看当前真实运行模式。

## 数据持久化

默认数据库为 `data/flowstaff.db`，可通过 `DATABASE_PATH` 修改。Render 免费实例的 `/tmp` 不保证持久化；正式环境需挂载 Persistent Disk，或迁移 PostgreSQL。
