# 验证记录 · v0.2.0 · 2026-09-15

## 本地实际执行

| 层次 | 命令／方式 | 结果 |
|---|---|---|
| Python 后端、网关、执行器、工具、数据与备份 | `python -m pytest` | **92 passed**（原 60 项＋agent 32 项，含参数化案例） |
| JavaScript 纯逻辑 | `npm test` | **20 passed**（原 17 项＋agent 3 项） |
| JS 语法 | `npm run check` | 5 个模块通过 |
| Python 语法 | `python -m compileall -q backend/re0 run.py scripts` | 通过 |
| Agent 桌面／移动浏览器 | `scripts/agent_browser_smoke.py` | 6 组通过，0 个 pageerror |
| 原文献库浏览器回归 | `scripts/browser_smoke.py` | 10 组通过，0 个 pageerror |
| 真实本地 HTTP 服务 | `scripts/http_smoke.py` | 7 个路径 HTTP 200；3 项拒绝／配置缺失检查符合预期 |

本地版本：Python 3.13.5、Node.js 22.16.0。原有依赖保持不变：FastAPI
0.128.2、Pydantic 2.13.4、HTTPX 0.28.1、Uvicorn 0.48.0、pytest 9.0.2。
Python 3.11 是声明支持目标与原 CI 配置，不是本轮实机验证版本。

机器可读记录：[agent-browser-report.json](agent-browser-report.json)、
[browser-report.json](browser-report.json)、[http-smoke.json](http-smoke.json)。

## 新增覆盖

真实应用、真实 gateway、真实 agent loop 和真实 SQLite 在测试中运行；只有
模型／外部提供商 HTTP 被 `httpx.MockTransport` 替换。覆盖：

- 未设置模型不可提交；任务材料发送授权、工具作用域；旧库保留；
- 凭证不出现在配置响应、错误输入回显、数据库与导出中；远程 endpoint
  scheme/host/port/path 限制，本地和部署者允许的自定义地址；
- Chat Completions 工具协议、多轮 tool_call_id 对应关系、模型重复 call ID
  的规范化、可选 token 参数、无 usage、普通聊天不能通过工具测试；
- 模型响应 HTTP 302/400/401/429/500 不跟随、不重试、不泄漏远端错误体；
- 公开计划→论文检索→实际证据→结构化报告的多轮流程；
- 虚构引用不通过并返回工具错误供模型修正；未授权／未知工具不能执行；
- 运行中设置锁、并发提交拒绝、在模型请求期间取消后不执行后续工具；
- 应用重开后手动恢复，原模型匹配与累计调用数，已完成工具不重新检索；
- Crossref/Hugging Face／按 commit 固定的有界文本读取；
- 人工审批、幂等入库、已有 DOI/arXiv 和私人笔记不覆盖。

这些测试验证执行和约束行为，不验证模型能否正确识别官方仓库、理解论文，
也不能得出科学判断准确率。生产路径没有 fixture 模式或预置“成功报告”。

## 浏览器与 HTTP 的区别

浏览器测试使用离线 Chromium DOM＋真实 FastAPI TestClient 桥接，不修改
环境网络政策。它验证用户点击、设置、任务历史、引用定位、批准入库、390px
移动布局和前端错误，不覆盖浏览器真实 TCP 导航、ES module 网络加载或实际
CSP 生效。Agent 浏览器示例报告均带 `Fixture` 标记，不是真实研究结果。

独立 HTTP 脚本确实启动 `python run.py` 子进程并向 loopback 发起 HTTP 请求，
验证 `/`、`/library`、静态 JS/CSS、API 与 OpenAPI 能返回，并检查无模型、无
客户端头、跨来源写入被拒绝。没有用它冒充完整的浏览器部署端到端测试。

## 复跑

```bash
python -m pip install -e '.[test]'
python -m pytest
npm test
npm run check
python scripts/http_smoke.py

# 可选浏览器测试：需要 Playwright 和 Chromium
python -m pip install playwright
python -m playwright install chromium
python scripts/agent_browser_smoke.py
python scripts/browser_smoke.py
```

已有 Chromium 时可设置 `CHROMIUM_PATH`，本轮使用 `/usr/bin/chromium`。
浏览器测试依赖测试 extras，因为 agent fixture 来自后端测试模块。

## 尚未验证

**没有真实 LLM Key，也没有真实本地模型。** 未运行真实模型成功调用、模型
科研质量评测、真实外网搜索成功链路或收费核对。模型可用性／兼容性需要在
用户环境中选择实际 Model ID，通过工具调用测试后再用真实论文评估。

本环境外部 DNS 不可用；没有绕过网络限制。没有干净虚拟环境公网安装测试、
Docker 构建、多用户隔离、PDF 全文读取或任意代码执行验证。初次本地交付时没有推送远端。
发布前在相同依赖下重新执行：92 项 Python 测试、20 项 JavaScript 测试和
`npm run check` 均通过。新版远端 CI 请查看本提交对应的 GitHub Actions；
旧版 CI 结果或本地测试不代表新版远端 CI 已通过。

## 2026-09-16 变更后复跑（任务默认值移入设置）

把任务预算与文献库授权从新建任务表单移入设置面板后，在隔离环境
（Python 3.13.12、Node.js 22.22.2）重新执行：

| 层次 | 命令 | 结果 |
|---|---|---|
| Python | `python -m pytest` | **96 passed**（v0.2.0 的 92 项＋4 项任务默认值用例） |
| JavaScript | `node --test tests/*.test.js` | **24 passed**（v0.2.0 的 20 项＋4 项默认值与同意文案用例） |
| JS 语法 | `node --check web/{app,core,api,agent,agent-core}.js` | 5 个模块通过 |

新增的 4 项 Python 用例覆盖：省略预算字段时回落到已保存的默认值、显式单次值
仍然优先、默认值跨进程重启保留、越界或多余字段返回 422 且不改动已保存行、
清除模型内存配置不影响默认值且不回显 Key。

本轮仍未配置真实模型 Key：96／24 只覆盖协议、存储与权限分支，不是真实模型
科研效果评测。浏览器 smoke 与真实 HTTP smoke 未在本次变更后重跑。


## 2026-09-16 变更后复跑（艾米莉亚双主题 UI）

前端改为浅色／深色双主题（`web/theme.css` 变量 + `web/theme.js` 切换，两个页面
CSS 的全部硬编码色值已消除）后，在隔离环境（Python 3.13.12、Node.js 22.22.2）
重新执行：

| 层次 | 命令 | 结果 |
|---|---|---|
| Python | `python -m pytest` | **96 passed** |
| JavaScript | `node --test tests/*.test.js` | **24 passed** |
| JS 语法 | `node --check web/{app,core,api,agent,agent-core,theme}.js` | 6 个模块通过 |
| Agent 浏览器 smoke | `python scripts/agent_browser_smoke.py .data/agent-browser` | **8 组全过**，`page_errors` 为空 |
| 文献库浏览器 smoke | `python scripts/browser_smoke.py .data/browser` | **10 组全过**，`page_errors` 为空 |
| HTTP smoke | `python scripts/http_smoke.py .data/http-smoke` | 7 个路径全部 200；3 个守卫（缺客户端头 403／未配置模型 422／跨源 403）通过 |

说明：两个浏览器 smoke 脚本现在内联 `theme.css` 并加载 `theme.js`，与本文件
上一节“浏览器 smoke 未重跑”的记录相比已补齐。三个 smoke 脚本改为先写报告再
关闭浏览器、临时目录使用 `ignore_cleanup_errors=True`，规避 Windows 下子进程
句柄延迟释放在清理阶段抛 `PermissionError` 的问题（检查结果不受影响）。

视觉核对：用 Playwright 对两个页面 × 两主题截图人工检查（含文献抽屉、证据
弹窗、设置面板、关系图），对比度与配色正常；`#goal` 输入框等透明背景组件在
深色主题下已修正。

本轮仍未配置真实模型 Key：96／24 只覆盖协议、存储与权限分支，不是真实模型
科研效果评测；真实模型质量与在线部署仍未验证。

## 2026-09-17 变更后复跑（服务商预设 + 拉取模型列表）

设置面板改为「选服务商 → 只填 API Key → 拉取可用模型 → 选填 Model ID」后，
在隔离环境（Python 3.13.12、Node.js 22.22.2）重新执行：

| 层次 | 命令 | 结果 |
|---|---|---|
| Python | `python -m pytest` | **101 passed**（96 项＋5 项模型列表用例） |
| JavaScript | `node --test tests/*.test.js` | **25 passed**（24 项＋1 项模型候选归一化用例） |
| JS 语法 | `node --check web/{app,core,api,agent,agent-core,theme}.js` | 6 个模块通过 |
| Agent 浏览器 smoke | `python scripts/agent_browser_smoke.py .data/agent-browser` | **9 组全过**，`browser_errors` 为空 |

新增的 5 项 Python 用例覆盖：预设表的 host 必须落在目的地允许列表内（防止两者
漂移）、`POST /api/agent/models` 要求信任标记与允许列表内的主机、远程主机必须有
Key、返回 ID 去重排序且不把 Key 写进响应或任务配置、供应商返回 401 时只回应用自身
的措辞（不转发供应商响应体与 Key）、超限响应与非 JSON 响应各自返回 422。

浏览器 smoke 新增 `model_picker_fills_candidates_and_its_toast_is_not_hidden_by_the_modal`：
用 fixture 的 `GET /models` 分支点「拉取可用模型」，断言 `#model-options` 填入 2 个
候选，并用 `elementFromPoint` 证明提示条上方没有其他层覆盖。

### 同轮修复：设置面板内的提示条看不见

现象：在设置面板里点「拉取可用模型」后，右下角提示条看起来被模糊、看不见。
原因是 `dialog::backdrop{backdrop-filter:blur(3px)}` —— `#notice` 是 `position:fixed`
挂在 `<body>` 上，不属于 top layer，因此被画在模态遮罩**下方**。影响范围不止拉取
模型：**设置面板打开期间任何提示都是隐形的**，包括 Key 填错返回的 422。

修法：`notice()` 在有 `dialog[open]` 时把提示条节点移入该对话框（留在 top layer），
对话框关闭时移回 `<body>`；`openSettings()` 重渲染前先归位，避免节点随 `innerHTML`
被销毁。上面的 smoke 断言就是这条回归的守卫。截图核对：`agent-settings-model-picker.png`。

**现在浏览器 smoke 已覆盖「拉取可用模型」交互**（上一版记录里写的待补项已完成）。

仍未配置真实模型 Key：101／25 只覆盖协议、存储与权限分支，不是真实模型科研效果
评测。四个新增国内平台的**预设地址取自多方公开文档，但其真实可用性、`GET /models`
是否实现、以及是否支持工具调用，都需要用户用真实 Key 跑一次才能确认**。

## 2026-09-17 变更后复跑（上下文体积：有界摘录 + read_evidence + 压缩）

| 层次 | 命令 | 结果 |
|---|---|---|
| Python | `python -m pytest` | **105 passed**（101 项＋4 项上下文用例） |
| JavaScript | `node --test tests/*.test.js` | **26 passed**（25 项＋1 项轨迹文案用例） |
| JS 语法 | `node --check web/{app,core,api,agent,agent-core,theme}.js` | 6 个模块通过 |
| Agent 浏览器 smoke | `python scripts/agent_browser_smoke.py .data/agent-browser` | **9 组全过**，`browser_errors` 为空 |

新增用例覆盖：工具结果只携带元数据与有界摘录（断言 `content` 不在对话里、`content_chars`
与 `elided` 存在、`evidence_note` 指明取回方式）、摘录预算在同一次工具结果内被共享
（`[4000,2000,0,0,0,0]`）、`read_evidence` 返回的切片与证据库正文逐字符一致、
跨任务 ID 与低于 schema 下限的读取都被拒绝、压缩收起旧摘录后证据数量不变且
`context_compacted` 事件带 `read_evidence` 指引。

### 顺带修好的测试脆弱性

`wait_done()` 的完成等待上限原为 10 秒。实测这台机器上**一个平凡的 GET 也要
51–193 ms**（Windows 加当前负载下的 SQLite 往返延迟），因此若干轮模型／工具往返的
fixture 任务需要 10 秒以上墙钟时间，导致
`test_saved_defaults_persist_and_are_applied_to_new_tasks` 等用例**偶发**
"Fixture task did not finish"（单独运行则通过）。已把上限放宽到 30 秒；该值只影响
失败用例的等待时长，正常任务仍立即返回。

**未实测**：这些是 prompt 体积上限，不是计费优化。是否命中提供商的 KV cache、真实
任务的 token 用量与实际费用均未测量，也没有做金额估算。

## 2026-09-17 变更后复跑（MCP over stdio 检索入口）

| 层次 | 命令 | 结果 |
|---|---|---|
| Python | `python -m pytest` | **112 passed**（105 项＋7 项 MCP 协议用例） |
| JavaScript | `node --test tests/*.test.js` | **26 passed** |
| JS 语法 | `node --check web/{app,core,api,agent,agent-core,theme}.js` | 6 个模块通过 |

MCP 协议用例全部在进程内驱动 `serve()`，**不引入 MCP 依赖**：握手与工具清单（断言只暴露
7 个只读检索工具；`update_plan`／`finish_report`／`read_evidence`／`search_library` 均不在内，
未配置 `TAVILY_API_KEY` 时也没有 `search_web`）、通知与畸形输入不产生响应、未知方法返回
`-32601`、未暴露工具与越界参数返回 `isError` 且文案不含 traceback、工具调用返回带 locator
的有界文本、**用 monkeypatch 让 `Database.__init__` 直接抛错以证明该入口绝不打开文献库数据库**、
上游 HTTP 500 的响应体不会被转发、空结果被表述为"空"而不是"不存在"。

### 与官方 MCP 客户端的兼容性（手工验证，未纳入自动化）

用官方 `mcp` 2.2.0 客户端以子进程方式连接 `python -m re0.mcp_server`：

- 握手成功，服务端标识为 `re0-research 0.2.0`，`tools/list` 返回 7 个工具的合法 schema；
- 客户端请求的协议版本是 **`2025-11-25`，比服务端默认值 `2025-06-18` 更新**；服务端回显
  客户端版本后握手正常完成 —— 这条回显路径值得保留；
- 越界参数返回 `isError`（文案只列出出错字段），`finish_report` 这类任务工具被拒绝；
- 服务进程 stderr 为空，stdout 只有协议消息。

这次验证**没有纳入自动化测试**，因为官方 SDK 会拉入 cryptography／pyjwt／jsonschema／
opentelemetry 等一批依赖，而本仓库的运行时依赖刻意只有 4 个包。复现方式：安装 `mcp` 后按
上述方式连接即可。

**未实测**：没有从真实第三方 agent（Claude Code / Codex / dsh）内部跑过工具调用；MCP 入口
下的真实检索质量与外网连通性同样未验证。

## 2026-09-17 变更后复跑（多源文献检索 + skill）

| 层次 | 命令 | 结果 |
|---|---|---|
| Python | `python -m pytest` | **129 passed**（112 项＋17 项检索／环境变量用例） |
| JavaScript | `node --test tests/*.test.js` | **26 passed** |
| JS 语法 | `node --check web/{app,core,api,agent,agent-core,theme}.js` | 6 个模块通过 |

新增的 17 项用例覆盖（在前 15 项之外还有两项：从 arXiv DOI 还原 arXiv ID 及其拒绝分支、
从摘要正文抽取开源候选链接并排除论文自身 venue 与尾部标点）：五源并发后跨源合并、**用全部标识符匹配**（一条有 DOI、一条只有标题
也必须合并）、短标题不参与匹配（`"Survey"` 不产生 key）、合并时取最大引用数并补齐首个来源
缺失的字段、某个源失败只记为该源的失败且不影响其他源、**全部源失败时报错而不是返回空列表**、
某个源返回非 JSON 时只记该源失败、年份区间既下发到各源 API 又对本地结果生效、**未知年份不被
过滤**、起止年份倒置被契约拒绝、OpenAlex 的倒排索引被还原成摘要正文、**每个 key 只发往拥有
它的主机**（并断言 key 不出现在结果里）、以及 dotenv 解析（引号／注释／`export ` 前缀／空值
跳过／不覆盖已存在的环境变量）。

### 真实来源可用性（手工实测，未纳入自动化）

用 `RE0_ENV_FILE` 指向一份真实的凭据文件跑 `skills/paper-search/scripts/paper_search.py`：

- **成功过的来源**：Semantic Scholar（返回 3 条并带摘要）、OpenAlex（返回 2 条并带
  `cited_by_count`）、Crossref（返回 2–3 条）、OpenReview（返回 1 条）。
- **跨源去重实测生效**：同一篇 ChunkKV 由 Semantic Scholar 与 Crossref 同时返回，被合并为
  一条并保留两处来源，DOI 与 arXiv ID 都被补齐。
- **`[survey]` 沉底实测生效**：一条标题含 "Review of:" 的记录被标 `[survey]` 并排到最后。
- **失败隔离实测生效**：某次运行中 S2 返回 429、arXiv 连接失败，两者被分别列为
  `source failures`，其余来源的结果照常返回。

**必须说明的环境限制**：本机沙箱对 Python 的 TLS 出网**不稳定** —— 同一批 URL 用 `curl` 可
拿到 HTTP 200，用 Python 却间歇性报
`ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`；`trust_env=True/False` 都失败，环境中
也没有代理变量，因此**不是** Re0 不继承代理造成的。所以这五个来源的**真实可用性是部分验证**，
不能据此声称它们在任何网络下都能用。

### 链接与开源候选（手工实测）

同一次真实运行里实测：

- **三级链接按 arXiv → DOI → 来源记录页输出**。查 "Low-Rank Adaptation LoRA" 时四源全部命中，
  OpenAlex 只报了 arXiv 的 DOI（`10.48550/arxiv.2106.09685`），**arXiv ID 从 DOI 前缀还原后
  补出了 `https://arxiv.org/abs/2106.09685`** —— 这正是没有这一步时唯一缺失的主链接。
- **开源候选抽取生效**：从重建出的摘要正文里抽到 `https://github.com/microsoft/LoRA`，并按
  `artifact candidate (from the abstract, unverified)` 标注。**未核验**，也刻意没有自动去核验。
- 同一次运行还暴露出脚本的一个参数 bug：`--sources openalex` 被拆成列表传给只接受单值的契约，
  已改为「只接受 `all` 或单个来源名」并在传入多源时报错。**这就是跑真实路径的价值。**

**未实测**：Semantic Scholar 的凭据在样本文件里是空的，因此其带 key 的调用路径未验证；真实
检索的召回率与结果质量没有评测；DBLP、Exa 等未接入；开源候选链接的**核验路径**（`inspect_resource`
等）没有在这次运行里跟着走一遍。




