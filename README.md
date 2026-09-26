# Re0 — Research From Zero

> 从“找到论文”继续向前：核对代码、Checkpoint、数据集和评测资源，并把结论绑定到可检查的来源。
![Uploading image.png…]()

[**在线体验 → re0-skill-demo.vercel.app**](https://re0-skill-demo.vercel.app/) ·
[Skill](skills/re0-paper-search/SKILL.md) ·
[测试记录](docs/TESTING.md) ·
[项目路线](docs/ROADMAP.md)

## 项目简介

Re0 是一个面向科研工作流的证据型研究助手原型。

普通文献检索通常能回答“有哪些相关论文”，但真正准备做实验时，还需要继续确认：

- 代码仓库是否真实存在，是否可能是官方实现；
- 是否包含训练、推理和评测入口；
- Checkpoint / Adapter / 基础模型之间是什么关系；
- 数据集、划分、预处理和环境文件是否可获得；
- “没有找到”究竟是资源不存在、没有检查到、访问受限，还是检索失败。

Re0 的核心设计不是给论文简单打一个“已开源”标签，而是把**候选、观察、来源、未知项和人工确认分开保存**。

## 在线 Demo

当前比赛交付是一个部署在 **Vercel** 的纯前端交互式 Demo：

### https://re0-skill-demo.vercel.app/

打开后无需安装、登录或配置 API Key，可以直接体验：

1. 查看一条带日期的历史论文资源核验案例；
2. 浏览论文卡片、摘要与来源；
3. 筛选候选论文；
4. 查看代码 / Checkpoint / 数据 / 评测等资源矩阵；
5. 展开来源和“仍未确认”的信息；
6. 复制 BibTeX，或下载 JSON / CSV 结果；
7. 可选：在浏览器本地载入自己的 Re0 结果 JSON。

### Demo 的真实性边界

这次在线版本是**前端产品原型**，不是实时在线 Agent：

- 不调用 LLM；
- 不接收 API Key；
- 不在云端执行论文检索；
- 不把浏览器数据保存到服务器；
- 页面中的案例保留原检查日期，不会伪装成今天重新核验的结果。

仓库内仍保留完整的 Skill、CLI、MCP、Agent、文献库等研究实现，供后续继续迭代；它们不是本次 Vercel 静态部署的一部分。

## 为什么做 Re0

一个典型科研流程往往是：

```text
研究问题
   ↓
搜索相关论文
   ↓
筛选候选方法
   ↓
寻找代码 / 权重 / 数据
   ↓
判断资源是否真的可用
   ↓
选择 baseline 并开始实验
```

现有工具在前两步已经很强，Re0 主要关注后面的“**资源判断与证据整理**”。

例如，一个 GitHub 链接存在，并不能自动推出：

```text
有仓库
≠ 官方仓库
≠ 有训练代码
≠ 有对应权重
≠ 数据可获得
≠ 可以运行
≠ 可以复现论文结果
```

因此 Re0 尽量保留“本次检查到底证明了什么，以及没有证明什么”。

## 已实现的主要能力

| 能力 | 当前仓库 |
| --- | --- |
| 多源论文检索 | Semantic Scholar、OpenAlex、arXiv、OpenReview、Crossref |
| 跨源去重 | DOI / arXiv ID / 保守标题匹配 |
| 资源发现 | GitHub、Hugging Face 候选 |
| 资源核验 | 文件清单、Release、README、仓库文本等有界检查 |
| 结构化资源审计 | 训练、推理、评测、Checkpoint、数据、划分、预处理、环境 |
| 来源与不确定性 | 区分未检查、访问失败、需要授权、范围内未找到等状态 |
| Skill | `skills/re0-paper-search/` |
| CLI / MCP | 可独立接入其他本地 Agent |
| 本地 Agent | BYOK 模型 + 多轮工具调用 + 证据报告 |
| 文献工作台 | 文献、资源观察、人工修订、导入导出 |
| 扩展实验 | 有界全文读取、持续追问、知识结构、Zotero 只读同步 |

其中部分高级能力仍属于研究原型，具体边界见 [ROADMAP](docs/ROADMAP.md) 和 [TESTING](docs/TESTING.md)。

## 证据优先的数据语义

Re0 尽量避免把不同状态混在一起：

| 状态 | 含义 |
| --- | --- |
| candidate | 找到了可能相关的资源 |
| observed | 工具实际读取到了某项信息 |
| inference | 基于证据做出的推断 |
| unknown | 当前证据不足 |
| access required | 资源存在，但需要申请或登录 |
| not found in scope | 在本次明确检查范围内没有找到 |
| failed | 本次检查失败，不能推出资源不存在 |
| human confirmed | 用户人工确认后的记录 |

对资源的肯定结论应能回到来源；工具失败不会被改写成“作者没有开源”。

## 本地体验静态 Demo

如果只想预览最终前端，不需要启动 FastAPI：

```bash
python scripts/build_static_demo.py
python -m http.server 8765 --directory dist/static-demo
```

然后打开：

```text
http://127.0.0.1:8765/
```

`scripts/build_static_demo.py` 使用白名单生成静态发布目录，只复制前端资源和审核过的示例，不会把数据库、`.env`、后端代码或 API Key 放进 Vercel 产物。

## 运行完整本地项目

完整研究原型仍可以在本机运行：

```bash
git clone https://github.com/sakur7a/Research-From-Zero.git
cd Research-From-Zero

python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
python run.py
```

浏览器打开：

```text
http://127.0.0.1:8000
```

完整本地模式可以配置支持 Chat Completions `tool_calls` 的模型服务。模型密钥不要提交到 GitHub。

## Paper Search Skill

仓库中的 `re0-paper-search` 可以独立使用：

```bash
re0 paper search \
  --query "image layer decomposition" \
  --start-year 2024 \
  --find-artifacts 10 \
  --verify 4 \
  --json out.json
```

它会保留各数据源是否成功、检索覆盖范围和资源候选，不使用模型记忆补造论文。

Skill 详情见：

- [skills/re0-paper-search/SKILL.md](skills/re0-paper-search/SKILL.md)

## 项目结构

```text
Research-From-Zero/
├─ web/                      # 浏览器 UI
├─ samples/                  # 审核过的静态 Demo 数据
├─ skills/re0-paper-search/  # 文献检索 Skill
├─ backend/re0/              # 本地 Agent、检索、文献与证据服务
├─ evals/                    # 评测任务与结果工具
├─ scripts/                  # 构建、smoke、备份等脚本
├─ docs/                     # 架构、测试、路线与交付记录
└─ tests/                    # JavaScript / 前端测试
```

## 测试

常用检查：

```bash
python -m pip install -e '.[test]'
python -m pytest
npm test
npm run check
```

仓库 CI 同时覆盖 Python 3.11 / 3.13、前端检查、安装 smoke，以及当前发布流程相关的浏览器测试。历史与最新验证记录见 [docs/TESTING.md](docs/TESTING.md)。

## 当前交付范围

本次提交阶段已经主动停止继续扩后端功能，优先保证：

- GitHub 仓库公开可读；
- README 能快速解释项目价值和边界；
- Vercel 地址无需账号即可打开；
- 静态 Demo 没有死按钮和后端依赖；
- 历史结果与实时能力明确区分；
- 用户可以实际操作筛选、矩阵、来源和导出。

后续若继续开发，再恢复 BYOK 在线 Agent、长期科研知识库和 Zotero 等完整平台能力。

## Repository status

- GitHub repository: **public**
- Online demo: **Vercel static frontend**
- Online LLM/API backend: **not deployed**
- Demo data: **dated historical audit**
- Cloud persistence: **none**

## License

仓库目前公开可见，但项目所有者尚未选择正式开源许可证。

**公开源码与授予开源许可证是两件不同的事。** 在许可证确定前，请参阅 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)。

---

**Re0 的目标不是替研究者做最终判断，而是让“为什么这样判断”更容易检查、比较和继续研究。**
