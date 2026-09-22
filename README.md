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
| `search_papers` | **多源**论文元数据与可用摘要：`source="all"` 会查 Semantic Scholar、OpenAlex、arXiv、OpenReview、Crossref 并按 **DOI > arXiv ID > 归一化标题** 合并重复项；可给 `start_year`／`end_year`；某个来源失败会被单独列出，**不当作"不存在"**；**不是全文阅读** |
| `resolve_paper` | 用 DOI／arXiv ID 解析来源元数据 |
| `search_repositories` | GitHub 仓库搜索，名称匹配不代表官方实现 |
| `search_hub` | Hugging Face 模型／数据集候选搜索 |
| `inspect_resource` | 复用静态核验：版本、文件清单、README、有限 Release、候选外链 |
| `read_repository_file` | 按解析到的 commit 读取有大小与行数上限的仓库文本；**不执行代码** |
| `read_evidence` | 按证据 ID 取回**本任务**已保存的来源正文切片（200–12000 字符，可用 offset 续读）。工具结果进入对话时只带**有界摘录**，完整正文留在证据库 |
| `search_release_discussions` | 指定仓库的 GitHub Issue／PR 标题与正文搜索；不包含完整评论串 |
| `search_library` | 授权来自设置中的任务默认值，并在每次新建任务时随同意项确认；仅检索书目、摘要与方向，排除笔记、附件和虚构演示数据 |
| `search_web` | 可选 Tavily 搜索摘要；需要部署者配置 `TAVILY_API_KEY`，**不是任意网页抓取器** |
| `update_plan` / `finish_report` | 更新公开行动计划、提交并校验结构化结果 |

未配置 Tavily 时，模型仍可搜索论文、GitHub 和 Hub，但不会得到一个假装能全网搜索的工具。模型可以继续检查支持的候选资源，网盘、任意项目主页、PDF 全文等尚未接入。

### 状态与约束

- 保留模型对话协议、待执行工具、已完成工具结果和证据。恢复时重用已提交结果；进程重启不会自动重发付费请求。
- 默认每任务 12 次模型调用、20 次工具调用、每次运行 360 秒；这些默认值在设置面板中修改，保存在本地数据库。任务创建时即固定自己的上限，之后修改默认值不影响它。恢复不重置累计调用数；最多手动恢复 3 次。
- 超时／取消在工具边界生效，进行中的请求可能完成或计费。时间上限不是强制终止正在运行的网络请求；没有精确货币预算。
- **上下文体积**：工具结果进入对话时只带**有界摘录**（每次工具调用共用 6000 字符），完整正文留在证据库；模型需要时用 `read_evidence` 按 ID 取回切片。对话超过 110,000 字符会收起较早的摘录（保留最近两条）并在执行记录中写明，**证据本身不会被删除**；150,000 字符硬上限仍是最后的显式失败点。这是为了控制上下文，**不等于已优化计费**，也不做金额估算。
- 输出记录服务端报告的 token 使用量；未返回 usage 或失败请求的实际费用不能据此推算。
- 每条报告发现引用本任务证据，禁止跨任务或虚构 ID；不自动证明证据蕴含结论，也不验证模型的科研推理正确性。
- agent 没有 shell、删除文献、修改配置、任意文件写入权限；论文入库是独立的显式批准接口。

## 模型、密钥和数据

网页填写的模型配置仅保留在**本地服务内存**；不会返回 API Key 到界面，不进入任务数据库、JSON 导出或 git。重启后需要重新设置，或通过启动前的环境变量配置。浏览器向本地后端提交 Key，后端再向选定的模型服务发送请求。

默认允许的远程模型主机：`api.openai.com`、`api.deepseek.com`、`dashscope.aliyuncs.com`、`dashscope-intl.aliyuncs.com`、`openrouter.ai`、`open.bigmodel.cn`、`api.moonshot.cn`、`api.siliconflow.cn`、`ark.cn-beijing.volces.com`。远程必须 HTTPS；自定义域名需由部署者通过 `RE0_LLM_ALLOWED_HOSTS` 明确加入。这个列表是**网络目的地许可，不是已通过实测的模型兼容性清单**；界面里的服务商预设是本列表的子集，且「拉取可用模型」只向这些地址之一发请求。本地服务允许 `127.0.0.1` / `[::1]` 加显式端口，Key 可以留空。

**文献检索有另一套独立白名单**（`providers.py` 的 `ALLOWED_HOSTS`）：`api.github.com`、`huggingface.co`、`export.arxiv.org`、`api.crossref.org`、`api.openalex.org`、`api.semanticscholar.org`、`api2.openreview.net`。模型和检索的目标列表互不影响，模型也无法把工具调用变成任意 URL 请求。

| 环境变量 | 用途 |
|---|---|
| `RE0_LLM_BASE_URL` / `RE0_LLM_MODEL` / `RE0_LLM_API_KEY` | 启动时加载模型配置。可用 `RE0_ENV_FILE=... python scripts/model_probe.py` 先自检：它只报告**变量名是否已设置**与工具调用协议是否通过，**从不打印任何值**，端点 URL 可打印是因为 `validate_endpoint` 已拒绝带凭据或查询串的 URL |
| `RE0_LLM_TOKEN_PARAMETER` | `max_tokens` 或 `max_completion_tokens`，默认前者 |
| `RE0_LLM_ALLOWED_HOSTS` | 额外允许的远程模型域名，逗号分隔；不得交由模型修改 |
| `TAVILY_API_KEY` | 可选全网搜索摘要服务，与模型 Key 不同 |
| `GITHUB_TOKEN` | 可选：提高 GitHub 限额（**search 匿名 10 次/分钟**，实测自响应头；core 匿名 60 次/小时）。**是普通环境变量，不是配置文件里的字面量**：可以 `export GITHUB_TOKEN=...`，也可以写进 `RE0_ENV_FILE` 指向的文件（同样只填未设置的变量、从不打印值）。**不要提交进仓库**；`.env` 已被 `.gitignore` 覆盖。无 scope 的 classic token 就够 |
| `RE0_ENV_FILE` | 可选：**显式指定**一个 dotenv 文件，启动时载入其中尚未设置的变量。不设置就不读任何文件；已在环境中存在的变量优先。只打印变量名与个数，**不打印值** |
| `OPENALEX_API_KEY` / `OPENALEX_MAILTO` | 可选：OpenAlex 检索（`mailto` 用于礼貌池） |
| `SEMANTIC_SCHOLAR_API_KEY`（别名 `SEMANTICSCHOLAR_API_KEY`） | 可选但**强烈建议**：Semantic Scholar 匿名调用会被硬限流 |
| `OPENREVIEW_TOKEN` | 可选：OpenReview 的公开检索不需要账号，token 用于更宽的读取范围 |
| `GITHUB_TOKEN` | 可选 GitHub API 凭证，仅发往 GitHub API |
| `RE0_DB` | SQLite 路径，默认仓库下 `.data/re0.sqlite3` |
| `RE0_HOST` / `RE0_PORT` | 默认 `127.0.0.1:8000` |

`.env.example` 只是说明文件，**不自动加载**。环境配置启动后仍只保存在进程内存；从界面清除并不会删除 shell 环境变量，下一次启动可能重新加载。HTTP 客户端当前不读取系统代理变量。

**本地部署不等于材料不出本机。** 执行任务会把目标、工具返回的公开材料、以及经授权的文献库元数据发送到你选择的模型服务。任务和证据在本地明文存储，导出也可能包含敏感研究主题。无登录、认证、多用户隔离或加密数据库，**不要直接暴露到公网或不可信局域网**。安全限制见 [SECURITY.md](SECURITY.md)。

## 作为 skill 使用（多源文献检索）

`skills/re0-paper-search/` 是一个自包含的 skill：一条命令跨五个学术源检索并合并重复项。

```bash
python skills/re0-paper-search/scripts/paper_search.py \
    --query "KV cache compression for long-context LLMs" --start-year 2024 --end-year 2026
```

**装到宿主里用 `re0 skill install`，不要手工 `cp`。** skill 目录现在作为**包数据**随 wheel 交付，所以装好 `re0-research` 之后，任何工作目录下都能：

```bash
re0 skill show                                          # 会用哪个目录、每个文件的 sha256
re0 skill install --target ~/.learnbuddy/skills --dry-run   # 只预览，什么都不写
re0 skill install --target ~/.learnbuddy/skills           # 生成 <target>/re0-paper-search
re0 skill package --output /tmp/re0-skill                 # 自包含副本 + MANIFEST.json
```

写入前每个文件先被分成 `new`／`identical`／`conflict`。**冲突（同名但内容不同）会让安装以退出码 3 停下，你那个文件一个字节都不动** —— 它可能是你自己改过的。`--force` 才替换，而且是把旧文件改名留成 `<名字>.re0-backup-<UTC 时间戳>`，不是删除。重复安装是幂等的：内容一致的文件跳过而不是重写。`MANIFEST.json` 记录每个文件的 sha256 和整棵树的哈希，之后可以核对副本有没有漂移。`re0 skill show` 还会说明 skill 是**从安装的数据目录找到的**还是**从旁边的仓库 checkout 找到的**，因为宿主里跑着旧副本时这是第一个要问的问题。

`SKILL.md` 按 Agent Skills 规范拆过：入口 229 行，凭据、发表状态、开源状态三块深度内容**原文**移到 `references/`，没有做摘要删减；入口里保留索引和链接。

输出形如 `per-source hits: semanticscholar=3, openalex=2, arxiv=0, … · 5 unique (1 duplicates merged)`，并在 stderr 单独列出**失败**的来源。survey／review 类论文被标 `[survey]` 并沉到列表末尾，但**不会被删掉**。

每条结果给三级链接，最优在前：**arXiv → DOI → 来源记录页**。很多服务只报 arXiv 的 DOI（`10.48550/arXiv.<id>`），所以没有直接给出 arXiv ID 时会从 DOI 还原 —— 那通常才是想点开的那个链接。

每条结果还报告**接收状态**与**机构**：

```
     2021 · 已收录于会议或期刊 · Neural Information Processing Systems（据 semanticscholar）
     机构: Microsoft Research, Tsinghua University
```

状态有四种：`有会议或期刊版本`／`投稿或评审中`／`仅见预印本版本`／`来源未给出发表信息`。**这些描述的是"来源报了什么"，不是对论文下的结论。** 没有 venue 时写"来源未给出发表信息"，**不会写成"仅见预印本版本"** —— 服务没提供等于什么也没说，把它当预印本是编出来的结论。原始 venue 字符串永远跟在状态后面，猜错可核对。同一篇论文**既可能是预印本也被收录**，这时强声明胜出、弱声明仍然报出（`注: 同一工作另有预印本版本被索引`）。

**为什么是"仅见预印本版本"这个措辞**：预印本和它的已发表版本通常是**两条独立记录**（OpenAlex 里 arXiv 版本与 ICCV 版本各有自己的 DOI），所以某一轮只命中预印本记录时，只能说明**这一轮没见到**会议/期刊版本。**修这个的最大杠杆是给 `SEMANTIC_SCHOLAR_API_KEY`** —— 它会把同一工作的多个版本合并成一个条目并给出 venue；其次是提高 `--max-papers`，因为已发表记录得先被某个源返回才谈得上合并。

机构取 Semantic Scholar 与 OpenAlex，去重后最多显示 3 个。**「各来源均未提供」很常见，含义是"服务没给"，不是"作者没有机构"** —— 预印本的机构信息主要靠 Semantic Scholar，而它匿名调用会被限流，所以配上 `SEMANTIC_SCHOLAR_API_KEY` 会明显改善覆盖率。

**开源情况（code／weights／dataset）分两层，由你决定走多远。**

**先说清能查到什么**：第一层只读**摘要**，而摘要通常不含代码链接 —— 所以单靠它常常为空。

**所以默认还有第二层：按论文项目名去 GitHub 与 Hugging Face Hub 搜**（`--find-artifacts N`，默认对前 10 篇生效）。很多论文在任何元数据字段里都没有链接，但确实发布了开源；项目名就是标题冒号前那段，也正是这些项目给仓库起名的习惯。实测 `RevealLayer: Disentangling Hidden and Visible Layers…` 的摘要与正文都没有链接，而这一层找到了 `github.com/360CVGroup/RevealLayer`、`huggingface.co/qihoo360/RevealLayer` 与 `RevealLayer-100K` 数据集。

**每个结果都是「名称匹配」，不是作者身份证明**，并且会标注：`描述与论文标题相符`（仓库描述复述了论文标题，强得多）或 `仅名称匹配`。归属仍需你确认 —— `--verify` 回答的是另一个问题：这个候选能不能打开、是不是空的。

该行**总会打印**，并把三种情况分开：**找到候选** / **搜了但确实没有** / **检索未完成**（每个接口重试一次；瞬时网络故障绝不能被说成「没有仓库」）。

第一层永远生效：给出上面的链接，并把**作者自己在摘要里写的** code／data 链接抽出来，标为「开源线索（摘要中自述，未核验）」。

第二层是 `--verify N`（0–8，默认 0）：对前 N 个候选链接跑 Re0 已有的**有界资源核验**，并把结果作为**字段级审计记录**写进 `--json`（`documents[].resource_audits`），而不只是打印一段摘要。下面是 2026-09-22 对 `microsoft/LoRA` 的一次真实运行（匿名额度，未配 `GITHUB_TOKEN`）：

```
     → https://github.com/microsoft/LoRA
       状态 部分可用（partially_available）；提供商状态 metadata_accessible · 深度 file_listing · 提供商 github
       仓库元数据可访问；扫描到 1189 个文件条目，功能与可复现性尚未验证。
       资源类别: code_training=有候选, code_inference=有候选, code_evaluation=有候选, checkpoint=有候选,
                 dataset=有候选, data_split=有候选, preprocessing=有候选, environment=有候选
       许可证（来源声明，不是使用权限结论）: code=MIT
       limit: 检测到 adapter/LoRA 形式的权重候选（examples/NLU/roberta_base_lora_mnli.bin、
              examples/NLU/roberta_large_lora_mnli.bin），通常需要对应的基础模型才能使用；本次未验证该对应关系。
       limit: 未建立论文版本与资源版本的对应关系；version_match=unknown 表示未判断，不表示不匹配。
```

这一行里值得注意的三件事，都是**真实输出**而不是设计意图：状态是 `partially_available` 而不是 `metadata_readable`，因为权重候选的文件名标着 `lora`——**adapter 不是能直接跑的 checkpoint**，缺的那一半（基础模型）文件名里没写；`checkpoint=有候选` 的每个结论在 JSON 里都带**钉在 commit `c4593f0` 上的文件链接**，所以"有候选"可以被打开验证；而 `attribution` 仍然是 `unconfirmed`——**哪怕这是 microsoft/LoRA**，因为名称与作者页面之外没有交叉证据，晋升为官方归属是人工确认的事。

**审计状态是一个封闭词表，而且描述的是「这次检查看到了什么」，不是「资源存不存在」**：`not_checked` 未检查、`candidate_located` 候选已定位、`metadata_readable` 元数据可读、`access_required` 需申请、`partially_available` 部分可用、`access_failed` 访问失败、`not_found_in_scope` 检查范围内未找到、`unsupported` 不支持。提供商自己的状态（HTTP 码、`gated`、`empty_repository`）保留在 `provider_status` 里，所以 **404 与 429 不会混成一件事**，超时也不会被读成"没开源"。

**八类资源分别记录**（训练/推理/评测代码、checkpoint、数据集、数据划分、预处理、环境），每类取值 `present` 有候选 / `absent_in_scope` 检查范围内未见 / `not_applicable` 不适用 / `unknown` 未知 / `requires_access` 需申请 / `check_failed` 检查未完成。**「不适用」和「未知」是两种答案，不会合并**：前者是"这篇论文不需要 checkpoint"，后者是"这次没查出来"。每个肯定结论都带**可跳转的来源**（钉在具体 commit 上的文件链接）；模型拒绝写入一个没有来源的 `present`。

这正好补上"光看链接判断不了"的几种情况：**链接真实但仓库是空的** → GitHub 明确回答"仓库无提交"，状态是 `not_found_in_scope` 而**不是**访问失败；**链接打开 404** → `access_failed` + `provider_status: HTTP 404`，文案是"可能不存在、已移动或无访问权限"；**只有推理代码** → `code_inference=有候选` 而 `code_training=检查范围内未见`；**权重是 adapter/LoRA** → 降级为 `partially_available`，并写明"通常需要对应的基础模型"；**数据需申请** → `access_required`，八类全部 `requires_access`；**文件树被截断** → 缺席项一律降级为 `unknown`，因为提供商自己说了清单不完整。

**`--resource-matrix PREFIX` 把 2–6 篇论文的审计并成一张表**，同时写 `PREFIX.json`、`PREFIX.md`、`PREFIX.csv`：每行一个资源候选，带状态、八类覆盖、许可证声明、**「不可直接比较的条件」**和可跳转来源。CSV 对 `=` `+` `-` `@` 开头的单元格做转义（这些字符串来自外部服务，而电子表格会把它们当公式执行），链接只写 http(s)。**没有候选的论文也占一行**，并且带上它自己的状态——否则"检索失败"和"检索过但没有"会在表里长得一样。`PREFIX.json` 里还有一段 `approval`，是同一批结果的可导入载荷：`POST /api/import/resource-audits` 接受它，**默认只预览**。

**为什么默认不核验**：一次核验约 4 个 GitHub 请求，匿名额度约每小时 60 次，所以 `--verify` 默认 0、上限 8，并且会**显示还有几条未核验**；**未被核验的链接仍按候选打印、仍写成 `not_checked` 的审计行，不会显示成失败**。要查更多请配 `GITHUB_TOKEN`。名称检索与核验预算都是**整轮共享**的（不是每篇一份），运行结束会打印覆盖分母 —— 下面是同一次真实运行的结尾（两个查询、12 篇、`--find-artifacts 3 --verify 2`，当时 huggingface.co 在本机不可达）：

```
开源审计覆盖（分母 12 篇）：检索完成 0 · 部分完成 3 · 全部失败 0 · 无项目名可检索 5 · 未检索 4（名称检索预算 3，已用 3）
  ← 未检索的论文是预算用尽所致，不是没有开源；--find-artifacts 可调大（GitHub 搜索限 10 次/分钟）。
候选 12 个（作者自述 3，名称匹配 9）；已核验 2，未核验 10（核验预算 2，剩余 0）
审计结论分布: 未检查 10 · 元数据可读 1 · 部分可用 1
名称检索失败 6 次（明细见对应论文行；失败不是「没有结果」）
```

**没有分母的计数会被当成结论**："核验了 0 个"到底是"没有可核验的"还是"预算用完了"，只有分母能区分；"检索完成 0"配上"部分完成 3"才说明是 Hub 不可达而不是没有开源。**没有用 subagent** —— subagent 给你一段描述，这个检查给你一个能横向比较的状态，且不会把请求失败说成"没开源"。要看得更深，用仓库里已有的 `search_repositories`／`search_hub`／`inspect_resource`／`read_repository_file`／`search_release_discussions`（走 MCP 或任务），不在 skill 里另搭流水线。`inspect_resource` 现在也返回同一套审计行，所以 MCP 客户端和 skill 读到的是同一种词表。

**归属和版本对应关系不会自动判定。** 名称相符（哪怕仓库名与项目名完全一致）只是候选，`attribution` 始终是 `unconfirmed`；`version_match` 始终是 `unknown`，因为元数据层面无法确认某个 checkpoint 对应论文的哪个版本。两者都由**人工确认**改写：`POST /api/resources/{id}/confirmations` 把确认作为**另一种记录**追加到该资源的历史里（`record_kind: confirmation`，机器检查是 `observation`），不覆盖原观察；**没有可定位依据就拒绝写入**。外部宿主在工具结果里写的 `confirmed: true` 不算审批 —— 工具面是只读的，没有任何参数能到达写路径。

**召回的上限是页大小**：`--max-papers` 默认 20、工具上限 25。**没有源返回的论文就无法被合并、更正或统计**，所以列表太薄时先调大它。**但要注意代价**：这些源按自己的相关性排序，**本工具不做重排**，所以页越大头部越杂（实测 `layer decomposition` 这种两词查询会把数学和金融论文排到前面）。**召回靠页大小，精确度靠查询词** —— 用目标文献真正在用的词（`image layer decomposition RGBA`），并且**跑多个短查询而不是一个长句**：arXiv 和 Semantic Scholar 对空格分隔的词是受限匹配，长句返回的反而可能**更少**。

### 评测（`evals/`）

把"能不能用"从一次幸运的运行变成可复现的记录。三条通道**互不混算**：`unit`（fixtures，`pytest`/`npm test`）、
`connector`（真实学术与资源服务，**不需要模型**）、`live`（真实任务，需要 BYOK）。

```bash
python evals/runner.py --list          # 任务清单（版本化，含标签）
python evals/runner.py                 # connector 通道，打真网、不用模型
python evals/runner.py --channel live  # 需要模型配置；没有就记 blocked
python evals/score.py                  # 写 evals/results/SUMMARY.md
```

**两条规则写在代码里，不是写在文档里**：① **没有分母就不给 100%** —— 没有样本被人工判为
official 时，准确率报 `no value` 并给出原因；② **未命中就是未命中** —— 检索完成但结果里没有期望的
标识符时记 `missed`，任务变 `partial`（这条是评测自己第一次运行就抓到的 bug：当时把一次**召回未命中**
报成了 `completed`）。

其余状态严格区分：`unknown` = **本次没查成**（网络/限流），不是否定结果；`deferred` = 从同一任务的
另一步读取；`human` = 需要人判断，跑不出来。**记录里永不写入密钥值**，只记哪些变量名被配置过。

### 命令行入口

skill 脚本和 `re0` 命令走**同一份实现**（`backend/re0/skill_search.py`），所以两者不会分叉：

```bash
re0 doctor                                            # 现在能跑什么；默认不联网、不花钱
re0 paper search --query "layer decomposition" --start-year 2025
re0 mcp                                               # 只读工具走 stdio，本机可用
re0 mcp --workspace ./ws                              # 可选：把工具取得的来源存成稳定 ID
re0 session list                                      # 会话与它们的累计账本（不联网、不花钱）
re0 session scope <run id> --goal "只保留有训练代码的两篇" --reuse-evidence ev_xxx
```

`mcp` **默认无状态**：不打开目录、不碰文献数据库。只有显式给 `--workspace DIR` 才会把工具取得的来源存下来，每份有**稳定 ID**（按内容寻址，同一来源不会存成两份），可导出/导入。导入**默认只预览**、幂等、**不会自动把论文入文献库**，并且**拒绝来自别的工作区的包**。**模型写出的文字不允许当成来源存进去** —— 工作区里只有工具真正取回的材料。

`doctor` 会分清**不需要模型**的能力（`paper search`、`paper text`、`mcp`、`skill` 与 `session list/show/delta/scope`）和**需要 BYOK** 的独立任务（一个自主任务，以及 `session follow-up`／`retry`）；网络探针**只有**传 `--probe-network` 才会跑，所以日常自检不会产生费用或触发限流。CLI 自己**不会启动第二个 LLM** —— 宿主工具模式和独立 agent 模式是两件事。`re0 paper search --help` 打印的就是 skill 自己的真实参数，不是另一份简化版。

**凭据优先级**：`RE0_ENV_FILE` **精确生效**（指了但读不到会照实报告，**不会静默改用别的文件**），然后 `./.env` 与 `~/.re0/.env`。**其他客户端的 `.env`（如 `~/.codex/skills/.env`）默认不读** —— 读到别的客户端登录的账号是凭据混用，不是便利；要用就显式给 `RE0_ENV_FILE=<路径>` 或设 `RE0_ENV_INCLUDE_AGENT_DIRS=1`。输出只显示**变量名与是否配置**，从不打印值。

> 当前产品顺序：**先把 skill 做成可用的科研能力，Web 平台复用同一核心**。赛事材料是单独分支。

**会议论文（CVPR／NeurIPS／ACL 这类只发在会议上的）已经被覆盖** —— Crossref、Semantic Scholar、OpenAlex 都索引 proceedings，venue 会报出来。`--venue NAME` 在**能验证的地方是真过滤，不能验证的地方只是查询提示**，并且每次都打印自己属于哪一种：

```bash
python skills/re0-paper-search/scripts/paper_search.py --query "diffusion watermarking" --venue CVPR --start-year 2024
```

OpenAlex 这条路 **2026-09-22 已联网验证**：先用 `/sources?filter=display_name.search:NAME` 把名字解析成稳定的 source ID，再按 `primary_location.source.id` 过滤（`mode=strict`）。**但严格过滤只覆盖解析到的那些 source**，而 OpenAlex 把一个会议系列拆成按届的多条记录 —— 实测搜 `CVPR` 只返回一条 `2022 IEEE/CVF …(CVPR)`。所以解析到的名字一定会打印出来，让读者看见范围被收窄到了哪一届；解析不到就退回查询提示并标 `resolve_failed`，此时结果**更宽而不是更窄**，也没有静默丢记录。Semantic Scholar 的 `venue=` 参数有文档但至今没验证成功（2026-09-22 再次被 HTTP 429 限流），所以仍然只作提示（`mode=hint`）；DBLP 对非浏览器客户端返回反爬挑战页而不是 JSON，不可用；arXiv／Crossref／OpenReview 这里没有 venue 参数。**多接一个源并不能解决剩下的缺口** —— 五个源已经包含数亿条会议记录，缺的是"某一届到底收了哪些"这种问法。

### 读全文：`re0 paper text`

检索给的是书目记录和摘要，不是全文。**资源声明、实验设置、作者自己写的局限，通常只在正文里。**

```bash
re0 paper text 2312.00286v1 --toc              # 目录 + 每个分块的 locator，不打印正文
re0 paper text 2024.acl-long.1 --locator p.3   # 只读第 3 页那一段
re0 paper text 2312.00286v1 --workspace .data/ws --json out.json
```

只接受 **arXiv ID** 和 **ACL Anthology ID**，**没有 url 参数** —— 地址由标识符按固定白名单（`arxiv.org`、
`aclanthology.org`）拼出来，所以论文正文里的链接没法把这个读取器指到别处。**DOI 会被拒绝**：解析 DOI 会跳到
出版商，那里可能是付费墙，本工具不绕过。每次跳转都重新校验（协议、端口、白名单、以及域名解析出的**每一个**
地址必须是公网地址），回环／私网／链路本地（云元数据地址就在那一段）／组播／保留地址一律拒绝，重定向回环、
超过 5 跳、解压后超过 8 MiB、内容类型不符也一律拒绝，并且**从不附带任何凭据或 Cookie**。

locator 是从读到的字节推出来的：HTML 是"第几节第几段"，PDF 是页码，所以**同一版本再读一次，locator 不变**，
引用可复核；返回里带着版本、抓取时间、解析器名、`content_sha256` 和 `parse_quality`。**一次只返回一个有界切片**，
其余以 locator 列出，正文按块存进 workspace（`--workspace`，可选）；模型不会拿到整篇论文。参考文献／致谢／附录
里的块标 `back_matter` —— 那里提到的工作是别人的，不会自动归给本论文。**不做 OCR**：扫描版 PDF 报 `scan_only`
并给出页数，损坏的报 `unsupported_format`，没装 PDF 依赖报 `parser_missing` 并给出安装命令和许可证；
免费解析失败**不会**自动改走收费模型。

> **本机网络的坑**：如果所在网络的 DNS 走本机透明代理（实测 `arxiv.org` 被解析到 `198.18.1.3` 和
> `fdfe:dcba:9876::f9`），公网地址校验会拒绝连接 —— 这是对的默认行为，但会让全文读取完全不可用。
> 设 `RE0_ALLOW_LOCAL_RESOLVER=1` 显式放行，结果里会记录实际连到了哪些地址。

**召回上限现在是"每页条数 × 页数"**：`--max-papers` 是**每页**上限（默认 20、工具上限 25），`--max-pages`（默认 1，最大 10）继续往后翻页。只有 OpenAlex 和 Semantic Scholar 的游标有公开文档，其余三个源只读一页，并在 `coverage.pagination` 里写 `stop_reason=not_supported`，**不假装查全**。`--max-requests`（默认 40）是整次调用跨所有源和查询的总请求上限；预算用完会如实报出来，**不会变成"没有结果"**。`--refresh` 强制重新取而不复用 TTL 内的缓存；缓存按**已配置的凭据种类**分作用域，匿名调用取到的结果绝不会给带凭据的调用复用，反之亦然，作用域里只有凭据的种类名，没有凭据本身。

设计取舍写在 `skills/re0-paper-search/SKILL.md` 里，其中三条值得单独说明：

- **去重优先级 DOI > arXiv ID > 归一化标题**，且一条记录会用它的**全部**标识符参与匹配 —— 只用单一 key 的话，"一家报了 DOI、另一家只报了标题"这种最常见的情况就合并不了。
- **来源失败不是负面结果。** 限流、超时、坏 token 都会被显式列出；"没搜到"与"没搜成"必须分开，否则会把一次故障读成"这工作不存在"。
- **不设"模型记忆"来源。** 让模型凭训练数据回忆论文，是文献列表长出"看起来很像但不存在"的标题的最常见方式。这里只返回服务真正返回的东西；经典老论文若在所有源都缺失，这个缺口会被如实报告，而不是用记忆补上。

凭据全部来自环境变量，**没有任何硬编码**。脚本按 `$RE0_ENV_FILE` → `./.env` → `~/.codex/skills/.env` → `~/.re0/.env` 顺序找到第一个可用文件，只填充**尚未设置**的变量；真实环境变量永远优先。运行时只打印载入了几个变量，**从不打印值**。

### 接着上一轮追问：`re0 session`

一轮跑完不等于问完了。真正的下一步通常是**改约束**——"只保留有训练代码的两篇，并补查它们的数据划分"——而这句话的价值在于**复用已经取到的证据**，不是把全网再搜一遍。

一个 **run** 是一次尝试；一个**会话（conversation）**是共享证据、共享**一本累计账本**的若干次尝试。三种轮次分开，因为它们授权的东西不同：`new`（新问题，新会话）、`followup`（在已有工作上加条件）、`retry`（原目标原文重来，所以带不进新范围）。

```bash
re0 session list                       # 会话、轮数、模型/工具累计
re0 session show cv_xxx                # 上限、累计账本、每一轮
re0 session delta <run id>             # 这一轮相对上一轮：新增/改变/本轮未再提/仍不确定
re0 session scope <run id> --goal "..." --reuse-evidence ev_xxx    # 只校验作用域，不创建、不花钱
re0 session follow-up <run id> --goal "..." --reuse-evidence ev_xxx --dry-run
```

几条不会让步的规则：

- **先校验作用域，再把历史材料交给模型。** 复用只认两种 id：同一会话里更早轮次的证据 id，或工作区里按内容寻址的 source id。**别的会话的 id 会被拒绝，并且说清是"属于另一个会话"**——不是"找不到"，那会让人去查拼写错误，而真正的问题是越界。
- **什么都不静默继承。** 本地文献库授权每轮重新给；模型目的地与上一轮不同就要显式 `trust_new_destination`，否则旧材料会被发往你没同意的服务。每轮存一份**不可变**的 `origin` 快照（目标、目的地、权限、可用工具、预算、开轮前的账本），之后的检查点改不了它。
- **账本是累计的，而且是重新求和出来的，不是加出来的。** 所以追问**不能靠"新建一轮"绕过上限**；上限只能用 `raise_session_caps` 在需要它的那一次请求里显式提高，不能借追问降低。到达上限时本轮**在执行中就会停**，不是只在准入时检查一次。
- **重复提交不是第二轮。** `idempotency_key` 命中就返回已经创建的那一轮；这个检查排在"一次只跑一个任务"之前，所以双击得到的是已有轮次，而不是一句拒绝。
- **新报告是新版本。** 上一轮的报告不被覆盖，仍可单独导出；`delta` 说的是新增/改变/本轮未再提/仍不确定。"本轮未再提"**只表示这轮没重复那句话，不表示上一轮结论被推翻**。
- 复用的材料保留**原来源时间与父轮记录**；超过 30 天会标为过时。**过时只是提示，不会自动付费重抓**——要不要补查由你决定。
- 交给新一轮的是上一轮的**目标与报告**，不是上一轮的完整对话记录；上一轮的证据 id 属于上一轮，所以**不会打印出来**（引用它会被拒）。这里复用的是既有的上下文压缩与 `read_evidence`，**不是另建一套"长期记忆"**。

`re0 session` 的读命令不联网、不需要模型密钥；发起一轮需要密钥，而密钥只存在于运行中服务的内存里，所以 `follow-up`／`retry` 是**发给本地服务**的，CLI 自己不起模型。Web 上只在已停止的一轮下面加了一个默认折叠的追问框：新条件、勾选要复用的证据、两个确认勾。**判断都在共享服务里，不在页面里**，所以 skill／MCP／CLI 都不依赖浏览器。

> 数据库从 agent schema v1 升到 v2 是**纯增量**的：新增 `agent_conversations` 与 `agent_runs` 的五个列，每个既有任务被各自收进一个单轮会话，账本按其最近检查点填写，**没有重置任何计数、没有删除任何行**。版本号比本机新会被拒绝，而不是被降级。

## 从 Zotero 只读增量同步

已经用 Zotero 管文献的人不需要重新录一遍。**同步是只读的、增量的，而且默认只预览。**

```bash
export ZOTERO_API_KEY=...            # 只发往 api.zotero.org；不写库、不打印、不进日志
re0 zotero collections --library-id 12345            # 只读集合名称与 key
re0 zotero select --library-id 12345 --collection COLL0001
re0 zotero preview  --library-id 12345               # 读远端，报告会发生什么，不写任何东西
re0 zotero sync     --library-id 12345 --apply       # 预览 + 一个事务里提交
re0 zotero status   --library-id 12345               # 映射、游标、集合选择、最近同步（不联网）
re0 zotero disconnect --library-id 12345             # 忘记游标与选择；论文一篇都不删
```

几条不会让步的规则：

- **不会去读本机 Zotero 数据库。** 只走 `api.zotero.org`，密钥只发给它，也**只存在于这次调用里**——不入库、不进同步日志、不出现在任何响应里，连打码都不打印。CLI 从环境变量读密钥而不是命令行参数，因为命令行会留在 shell 历史和进程列表里。
- **附件、批注、笔记一律不传输。** 请求带的是 34 种文献类型的**白名单**，所以私人笔记正文不是"取回来再丢掉"，而是**根本没有被请求**。这让响应比变更列表窄，于是差额被算成 `unaccounted` 并报出来——**范围窄不能冒充范围全**。
- 身份是 `(library_type, library_id, item_key)` + 远端版本；DOI/arXiv 只用来**关联**，不替代远端身份。所以**同一篇出现在两个库里会保留两条映射**、指向同一篇本地论文；没有 DOI 的条目照样能同步。
- **远端删除是 tombstone，不是级联删除。** 映射标记为 `remote_deleted`，论文、笔记、资源与观察记录**全部保留**。Zotero 不再收录某条记录，只说明 Zotero 的事，不说明这里做过的工作。删掉的条目又回来时是**重新关联**，不是新建一条。
- **游标与映射在同一个事务里写入。** 中断的同步会重读同一个窗口，不会把游标推过没人写进去的条目。分页没读完（`Total-Results` 没被满足）时**整次同步被拒绝**，理由同上。
- 更新只覆盖书目字段。`notes`、`topics`、阅读 `status` 从库里读回来再合并，所以**远端的一次编辑不可能把你的批注重置成默认值**。
- 把远端条目关联到**库里已有**的论文（比如先前 CSL 导入的）时**不改写那条记录**——关联不等于导入。
- 冲突在**规划阶段**就带着原因拒绝，不是等到写入时才发现：DOI 已属于另一条记录、同一批里两条共用一个 DOI、库内两条标题相同（无法确定对应哪一条，所以**不猜**）。每一次 deliberate skip 都记进同步日志，并说明**游标仍会前进、不会自动重试**。
- `disconnect` 只忘记游标与集合选择，论文一篇不删；加 `--remove-links` 才删映射，**同样不删任何研究记录**。
- **筛选范围不会被悄悄缩小。** 选三个集合就发三个（Zotero 的 `collection` 参数支持逗号分隔）；多个标签按**并集**发送，并且响应里**写明是并集**，不让人猜成交集。实际生效的范围随计划一起回报，也存在游标行上。

同一套服务也在 HTTP 与界面上，判断都在服务里，不在页面里：

```
GET  /api/zotero/status       # 映射、游标、集合选择、最近同步；不需要凭据
GET  /api/zotero/links        # 哪条远端记录对应哪篇本地论文；不需要凭据
POST /api/zotero/collections  # 只读集合名称与 key
PUT  /api/zotero/selection    # 存下勾选的集合
POST /api/zotero/sync         # body 里带 apply；不带就是预览，什么都不写
POST /api/zotero/disconnect
```

文献库页面的设置里就是这个流程：填库号与密钥 → 读集合列表 → 勾选 → 预览 → **显式提交**。密钥以 `SecretStr` 过线，所以它不会跟着 repr、日志行或校验错误体跑出去；**用完即弃**，提交返回后输入框被清空，关闭对话框再清一次。`/api/zotero/sync` 由一把锁串行化，同时来第二个同步得到 **409**，而不是两个写者抢同一个游标。

> 本轮**没有做**：双向写回（连 dry-run 契约都还没开）、以及**任何真实账号下的运行**——那需要所有者在本机授权，凭据不进 Issue，所以现在标为 `live` 待授权。全部 35 个 Zotero 测试都是**离线 fixture**，没有联网、没有真实密钥；浏览器冒烟里的 `api.zotero.org` 也是进程内 fixture 应答的，其他任何主机照旧让测试失败。

## 作为 MCP 服务器接入其他 agent

Re0 的只读检索工具可以脱离本仓库的 UI，作为一个 **MCP over stdio** 服务器给别的 agent 调用（Claude Code、Codex、dsh 等任何支持 MCP 的客户端）：

```bash
python -m re0.mcp_server
```

客户端配置示例：

```json
{"mcpServers": {"re0": {"command": "python", "args": ["-m", "re0.mcp_server"]}}}
```

**暴露的工具**：`search_papers`、`resolve_paper`、`search_repositories`、`search_hub`、`inspect_resource`、`read_repository_file`、`search_release_discussions`（配置 `TAVILY_API_KEY` 后还会多出 `search_web`）。工具描述与入参 schema 直接取自任务内模型看到的那同一份 Pydantic 契约，所以两个入口不会漂移。

**不暴露的工具**，以及为什么：

- `update_plan` / `finish_report` 是 Re0 任务内部的协议步骤，脱离任务没有意义；
- `read_evidence` 是任务作用域的（按本任务证据 ID 取回），MCP 调用没有对应的任务；
- `search_library` 需要**每次任务的显式同意**，MCP 调用不是那个上下文。

**这个入口的边界，请勿误读**：MCP 调用**不创建任务、不写检查点、不产生证据 ID**。返回的是带 locator 和来源 URL 的来源材料，**Re0「每条结论引用本任务证据 ID」的约束适用于它自己的任务运行，不由这个接口继承**。返回正文按 1500 字符摘录截断，并在结果里写明字符总数。

**零额外运行时依赖**：MCP 的 stdio 传输就是换行分隔的 JSON-RPC 2.0，所以这个服务器不需要官方 SDK，Re0 的依赖仍然只有 4 个包。协议兼容性已用官方 `mcp` 客户端验证过（见 [docs/TESTING.md](docs/TESTING.md)）。

这个接口是**只读检索**，不改文献库、不写文件、不读本地文献库。它仍然是无认证的本地进程 —— 只让可信的本机 agent 调用。

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
