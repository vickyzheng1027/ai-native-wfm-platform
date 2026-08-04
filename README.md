# AI Native WFM Platform

面向管理层比赛演示的 AI Native 跨店补位决策系统。系统以一条端到端业务链贯穿规则、排班、人员、工时、合规、成本账户与数据反哺，所有状态都真实写入 SQLite。

## 已实现能力

- 自然语言规则解析、元数据防幻觉、人工确认与规则版本激活
- 规则库、历史版本和基于运行结果的规则优化建议
- 门店缺口创建、技能/资格/工时/连续工作天数确定性过滤
- 候选评分、风险与基于真实数字的推荐理由
- 确认前按最新规则二次校验，规则变化后自动废弃并重新推荐
- 调剂成本实时计算并归属劳动力账户
- 公司 Gaia OpenAI Responses API，10 秒超时、重试一次、明确兜底
- 一键重置可重复演示数据

## 主要接口

- `POST /api/rules/parse`：解析自然语言规则并生成草稿
- `POST /api/rule-drafts/:id/activate`：人工确认后激活规则版本
- `POST /api/shortages`：创建真实用工缺口
- `POST /api/shortages/:id/recommend`：运行确定性过滤与推荐
- `POST /api/suggestions/:id/confirm`：最新规则校验与成本入账
- `POST /api/rule-optimizations/run`：基于运行数据生成规则优化建议
- `POST /api/demo/reset`：重置比赛数据

## 本地运行

要求 Node.js 22.5 或更高版本。

```bash
npm test
npm start
```

打开 `http://localhost:4180`。

启用真实 AI Agent 前配置：

```bash
export OPENAI_BASE_URL="https://coding.gaiaworks.net/openai/v1"
export CODEX_API_KEY="你的公司内部 API Key"
export OPENAI_MODEL="gpt-5.5"
```

公司内部网关使用 `OPENAI_BASE_URL`、`CODEX_API_KEY` 和 `OPENAI_MODEL`。系统会调用 `${OPENAI_BASE_URL}/responses`，密钥只应配置在运行环境中，不得写入代码或提交到 Git。使用 OpenAI 官方地址时也兼容 `OPENAI_API_KEY`，且要求密钥以 `sk-` 开头。

没有有效 API Key 或模型连续两次失败时，规则理解切换到明确标识的确定性解析。候选过滤、合规、工时、成本和规则优化始终由确定性引擎负责，不依赖模型生成数字。

数据库默认位于 `data/wfm.db`，可通过 `DATABASE_PATH` 指定其他路径。

## 数据说明

系统内置 A、B、C 三家门店和覆盖关键边界条件的员工数据。页面不需要登录，所有操作、校验、版本和成本均由后端执行并写入 SQLite。

Render 免费实例的文件系统会在重建或休眠恢复时丢失。正式持久化部署需要挂载 Persistent Disk 并把 `DATABASE_PATH` 配置为磁盘目录，或切换到托管 PostgreSQL。
