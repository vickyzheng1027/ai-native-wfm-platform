# AI Native WFM Platform

面向管理层演示的全模块 AI Native 劳动力管理系统。系统使用真实数据库读写和业务计算，初始人员、门店与活动为可替换的演示数据，不代表真实公司数据。

## 已实现能力

- 经理与员工分角色登录，HttpOnly Cookie 会话和权限隔离
- 人事、技能、门店与假期余额维护
- 排班冲突、单日工时、假期余额和月度加班上限校验
- 员工请假、补卡、加班申请及经理审批
- 员工上下班打卡并关联计划班次
- 自然语言经营目标解析、候选人选择、成本与合规检查
- 基于 `ai-native-wfm` 领域规则重构的服务端 AI 引擎，生成仅加班、仅跨店和组合三种候选方案
- AI 状态机、九大 WFM 模块调用轨迹和候选方案持久化
- 员工自然语言理解：查班、请假、换班、补卡、加班、打卡和偏好登记
- 换班申请和排班偏好确认后真实持久化、版本管理并写入审计
- AI 只生成提案，经理确认后才创建真实班次
- 活动结算、工时计算、工时银行、劳动力账户分摊和薪资预估
- 活动结算由管理者录入实际客流与销售额，不使用代码内固定经营结果
- 业务、员工、策略三层数据反哺及全链路审计
- OpenAI Responses API 真实 Agent，使用严格工具 Schema 和结构化输出
- 基于历史客流的加权移动平均与趋势预测，保存版本、特征、区间和置信度
- 直接复用运营中枢、门店排班台和员工伙伴 UI，页面数据全部来自后端 API

## 主要接口

- `POST /api/ai/plans`：解析管理目标并生成三种候选方案
- `POST /api/ai/plans/:id/execute`：经理确认后执行推荐方案
- `POST /api/ai/employee-intent`：理解员工自然语言并返回业务上下文与下一步动作
- `POST /api/events/:id/close`：结算工时、账户、薪资、人效与三层反哺

## 本地运行

要求 Node.js 22.5 或更高版本。

```bash
npm test
npm start
```

打开 `http://localhost:4180`。

启用真实 AI Agent 前配置：

```bash
export OPENAI_API_KEY="你的 API Key"
export OPENAI_MODEL="gpt-5.6-terra"
```

没有有效的 `OPENAI_API_KEY` 时，系统自动使用 `deterministic-wfm-v1`：从数据库读取人员和班次，运行统计需求预测与版本化合规规则，再由确定性编排引擎生成可确认方案。页面会明确标注当前模式，不伪装成 OpenAI。配置以 `sk-` 开头的有效 Key 后自动切换为 OpenAI Agent 编排。

演示账号：

- 租户：`DEMO`
- 经理：`manager / Demo@2026`
- 员工：`employee / Demo@2026`

数据库默认位于 `data/wfm.db`，可通过 `DATABASE_PATH` 指定其他路径。

## 数据说明

所有功能、权限、校验、状态变化和计算均由后端真实执行并写入 SQLite。首次启动时会初始化演示租户数据；后续启动不会覆盖已存在的数据。

Render 免费实例的文件系统会在重建或休眠恢复时丢失。正式持久化部署需要挂载 Persistent Disk 并把 `DATABASE_PATH` 配置为磁盘目录，或切换到托管 PostgreSQL。
