# Re0: Research From Zero

**v0.2.0 · Agent-first · 本地单用户 Alpha。**

输入研究目标，由模型自主选择检索工具、阅读返回材料、补查资源并输出有来源的研究报告。文献库不再是主入口，而是研究结果的保存位置。**这不是把固定脚本包装成 agent，也没有在未配置模型时用演示结果冒充分析。**

## 从一个问题开始

例如：

> 检索 Layout 生成相关论文，追查训练代码、checkpoint 和评测配置。区分作者声明与实际文件线索，列出证据不足的部分。

运行中的任务会展示公开行动计划、真实工具事件、已获取证据、累计调用量。模型可以根据搜索结果改变后续行动，而不是只能按固定顺序调用一遍工具。最终每条发现必须引用本任务实际生成的证据 ID；**ID 有效不代表语义正确，结论仍需人工复核**。

## 启动

Python 3.11+。前端无需 Node.js、npm 安装或构建；基本依赖与 v0.1 相同。

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

打开 `http://127.0.0.1:8000`，然后：

1. 打开「模型与工具设置」，在**服务商**下拉里选一个平台（OpenAI、DeepSeek、阿里云百炼、月之暗面 Kimi、智谱 GLM、硅基流动、火山方舟、OpenRouter，或本地 Ollama），地址自动填入；然后**只需要粘 API Key**。不知道填哪个模型时点「拉取可用模型」，从返回的列表里挑一个填进 Model ID（也可以手动输入）。确认接收端与材料发送范围。同一面板下方的「任务预算与数据权限」用于设置**新建任务**的默认调用上限和本地文献库授权，保存后写入本地数据库，重启仍然保留。
2. 保存并执行「测试工具调用」。这会发出一次真实模型请求，可能计费；不测试科研能力。**拉取到的模型列表只说明该服务报告了哪些模型，不代表它们支持工具调用**，这一步不能省。
3. 输入目标、确认发送材料后启动任务。没有模型配置时不能启动 AI 任务，但 `/library` 的旧文献库仍可单独使用。任务页只显示一行当前预算摘要，需要改动时回到设置；已创建的任务保留自己的上限，不受之后修改默认值影响。
4. 查看报告与来源；候选论文仅在点击批准后写入文献库。可取消任务、导出结果，对失败／中断任务进行有限次数的手动恢复。

Base URL 通常包含 `/v1`，不要填写 `/chat/completions`；本地服务示例为 `http://127.0.0.1:11434/v1`。**具体模型必须支持工具调用**；不是任何聊天模型、厂商专有推理模式、原生 Messages/Responses API 都已适配。若供应商要求 `max_completion_tokens`，可在界面切换输出参数。此版不会自动切换到另一模型服务。不是每个供应商都实现 `GET /models`，拉取失败时手动填写 Model ID 即可。

## v0.2 已实现的架构

```text
研究目标 / 显式授权 / 调用预算
                │
        Agent Runtime（可保存状态的执行循环）
          ↕ 模型自主选择下一步
        Model Gateway（用户自带模型配置）
                │ tool_calls
        Tool Registry（只读、受限网络工具）
                │
        Evidence Store + Task Checkpoints
                │
        结构化报告 → 用户批准 → 文献库
```

核心是自研的轻量 Python 执行循环，**没有使用 LangGraph、LangChain 或多模型编队**。FastAPI 负责 API，单个后台线程执行任务，SQLite 保存检查点／证据／历史，原生 ES modules 提供界面。一次仅执行一个研究任务，不支持多 worker 部署。模块和取舍见 [架构说明](docs/ARCHITECTURE.md)。

界面提供浅色／深色两套主题（顶栏或侧栏一键切换，选择保存在浏览器 localStorage，默认浅色），全部颜色集中在 `web/theme.css` 的变量里。

### 当前可调用的工具

| 工具 | 实际范围 |
|---|---|
| `search_papers` / `resolve_paper` | arXiv、Crossref 的论文元数据与可用摘要；**不是全文阅读** |
| `search_repositories` | GitHub 仓库搜索，名称匹配不代表官方实现 |
| `search_hub` | Hugging Face 模型／数据集候选搜索 |
| `inspect_resource` | 复用静态核验：版本、文件清单、README、有限 Release、候选外链 |
| `read_repository_file` | 按解析到的 commit 读取有大小与行数上限的仓库文本；**不执行代码** |
| `search_release_discussions` | 指定仓库的 GitHub Issue／PR 标题与正文搜索；不包含完整评论串 |
| `search_library` | 授权来自设置中的任务默认值，并在每次新建任务时随同意项确认；仅检索书目、摘要与方向，排除笔记、附件和虚构演示数据 |
| `search_web` | 可选 Tavily 搜索摘要；需要部署者配置 `TAVILY_API_KEY`，**不是任意网页抓取器** |
| `update_plan` / `finish_report` | 更新公开行动计划、提交并校验结构化结果 |

未配置 Tavily 时，模型仍可搜索论文、GitHub 和 Hub，但不会得到一个假装能全网搜索的工具。模型可以继续检查支持的候选资源，网盘、任意项目主页、PDF 全文等尚未接入。

### 状态与约束

- 保留模型对话协议、待执行工具、已完成工具结果和证据。恢复时重用已提交结果；进程重启不会自动重发付费请求。
- 默认每任务 12 次模型调用、20 次工具调用、每次运行 360 秒；这些默认值在设置面板中修改，保存在本地数据库。任务创建时即固定自己的上限，之后修改默认值不影响它。恢复不重置累计调用数；最多手动恢复 3 次。
- 超时／取消在工具边界生效，进行中的请求可能完成或计费。时间上限不是强制终止正在运行的网络请求；没有精确货币预算。
- 输出记录服务端报告的 token 使用量；未返回 usage 或失败请求的实际费用不能据此推算。
- 每条报告发现引用本任务证据，禁止跨任务或虚构 ID；不自动证明证据蕴含结论，也不验证模型的科研推理正确性。
- agent 没有 shell、删除文献、修改配置、任意文件写入权限；论文入库是独立的显式批准接口。

## 模型、密钥和数据

网页填写的模型配置仅保留在**本地服务内存**；不会返回 API Key 到界面，不进入任务数据库、JSON 导出或 git。重启后需要重新设置，或通过启动前的环境变量配置。浏览器向本地后端提交 Key，后端再向选定的模型服务发送请求。

默认允许的远程模型主机：`api.openai.com`、`api.deepseek.com`、`dashscope.aliyuncs.com`、`dashscope-intl.aliyuncs.com`、`openrouter.ai`、`open.bigmodel.cn`、`api.moonshot.cn`、`api.siliconflow.cn`、`ark.cn-beijing.volces.com`。远程必须 HTTPS；自定义域名需由部署者通过 `RE0_LLM_ALLOWED_HOSTS` 明确加入。这个列表是**网络目的地许可，不是已通过实测的模型兼容性清单**；界面里的服务商预设是本列表的子集，且「拉取可用模型」只向这些地址之一发请求。本地服务允许 `127.0.0.1` / `[::1]` 加显式端口，Key 可以留空。

| 环境变量 | 用途 |
|---|---|
| `RE0_LLM_BASE_URL` / `RE0_LLM_MODEL` / `RE0_LLM_API_KEY` | 启动时加载模型配置 |
| `RE0_LLM_TOKEN_PARAMETER` | `max_tokens` 或 `max_completion_tokens`，默认前者 |
| `RE0_LLM_ALLOWED_HOSTS` | 额外允许的远程模型域名，逗号分隔；不得交由模型修改 |
| `TAVILY_API_KEY` | 可选全网搜索摘要服务，与模型 Key 不同 |
| `GITHUB_TOKEN` | 可选 GitHub API 凭证，仅发往 GitHub API |
| `RE0_DB` | SQLite 路径，默认仓库下 `.data/re0.sqlite3` |
| `RE0_HOST` / `RE0_PORT` | 默认 `127.0.0.1:8000` |

`.env.example` 只是说明文件，**不自动加载**。环境配置启动后仍只保存在进程内存；从界面清除并不会删除 shell 环境变量，下一次启动可能重新加载。HTTP 客户端当前不读取系统代理变量。

**本地部署不等于材料不出本机。** 执行任务会把目标、工具返回的公开材料、以及经授权的文献库元数据发送到你选择的模型服务。任务和证据在本地明文存储，导出也可能包含敏感研究主题。无登录、认证、多用户隔离或加密数据库，**不要直接暴露到公网或不可信局域网**。安全限制见 [SECURITY.md](SECURITY.md)。

## 从 v0.1 升级

没有删除或重建原论文表；新增独立版本的 agent 表。原文献、笔记、分类、静态检查记录均保留，旧界面移到 `/library`。

先用旧版自带备份工具建立完整备份，再停止旧服务、替换代码、启动新版：

```bash
python scripts/backup.py --output backups/re0-before-agent.sqlite3
```

不要覆盖 `.data` 或 `.env`。在另一个目录试用新版时，默认会创建另一个空库；要使用原库，设置 `RE0_DB` 为原 SQLite 文件的**绝对路径**，且不能让两个版本同时写同一文件。恢复时先停止服务，再指向备份路径；不需要破坏原文件。

SQLite 备份覆盖论文与 agent 所有表。文献库 JSON 导出不含 agent 任务；任务有独立导出，目前两者均不是一键还原格式。

v0.2 的基准是上游 `7ab5cedc67eb86f1aa3a3b900e67634abb52a881`。本提交发布先前交付的 agent 重构；已有仓库请先备份、停止服务，再执行 `git pull --ff-only origin main`。升级与原始补丁说明见 [交付说明](docs/DELIVERY.md)。

## 验证与边界

```bash
python -m pip install -e '.[test]'
python -m pytest
# 开发测试需要 Node.js 20+，无需 npm install
npm test
npm run check
```

浏览器测试需另装 Playwright 和 Chromium，详见 [测试记录](docs/TESTING.md)。已用模拟模型／提供商响应测试真实执行循环和页面，不把 fixture 当成真实科研检索。**本次没有真实模型 Key，云端／本地模型成功调用、线上检索质量、Docker 构建均未实测。**

下一步是用真实模型与真实论文评测这条闭环，再接入全文阅读、可补充目标的持续会话、Zotero 同步和更深层的理论结构。不要把本版称为已完成全学科 Deep Research 或自动理论验证。见 [开发路线](docs/ROADMAP.md)。

许可证仍待项目所有者选择；公开可见的源码不自动授予开源使用许可。见 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)。
