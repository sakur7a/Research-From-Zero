# Re0: Research From Zero

从论文到实验：按方向组织文献，检查资源线索，为每次判断保留证据。

**v0.1.0 · 本地单用户 Alpha。** 无需 LLM 密钥，不上传私人笔记，不执行论文仓库代码。自动检查目前是受限的静态检查，不是全网搜索 agent，也不代表成功复现。

## 运行

需要 Python 3.11 或以上；本次在 Python 3.13 验证。运行前端不需要 Node.js、npm 安装或构建。

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 改用：.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

浏览器打开 **http://127.0.0.1:8000**。服务启动后一直保留这个终端，按 Ctrl+C 停止。

首次启动为空库。点击「体验演示数据」可加入 6 条**虚构且有标记**的论文及资源状态，用来体验界面；它们不是对真实研究的判断，不执行真实网络核验。「数据与设置」可以只清除演示记录，不影响自行添加的论文。

## 现在可以做什么

| 功能 | 当前实现 |
|---|---|
| 文献库 | 新建、编辑、删除、关键词搜索、卡片/表格、多方向标签、阅读状态、笔记 |
| 元数据导入 | 单条 DOI / arXiv ID 查询；导入前可修改。依赖 Crossref / arXiv 网络可用性 |
| 资源管理 | 代码、权重、数据、评测、环境、演示链接；人工记录归属、发布声明及依据 |
| GitHub 静态检查 | 默认分支提交、文件名线索、前 10 个 Release、README 内的候选资源链接 |
| Hugging Face 检查 | 模型/数据集元数据、文件名、访问门槛、卡片许可证声明 |
| 证据历史 | 保存检查时间、版本、检查范围、证据摘录、限制；重复检查追加历史 |
| 对比与关系 | 选择 2–6 篇比较资源条件；方向—论文—资源的分类关系视图 |
| Zotero 过渡导入 | 从 Zotero 导出的 CSL JSON 预览、逐条校验、保守去重后导入 |
| 导出/备份 | JSON（包含笔记和历史）、BibTeX、SQLite 安全备份脚本 |

关系视图**不包含自动提取的理论、引用或证明关系**。资源统计是记录统计，不是“复现率”或论文评分。

### 建议先体验这条路径

1. 加载演示，按「Layout 生成」筛选，选择两篇论文对比，打开某项资源的证据历史。
2. 新建自己的论文，可手填或通过 DOI / arXiv 获取元数据，添加研究方向和笔记。
3. 添加一个 GitHub **仓库首页**或 Hugging Face **模型/数据集首页**，点击静态检查，查看范围和限制。
4. 从 Zotero 导出「CSL JSON」，在「数据与设置」预览后导入。初版最多 500 条、文件不超过 3 MiB。

## 核验结果的语义

- **元数据可读**：接口返回了元数据或文件清单；不代表文件已下载、脚本可运行或资源对应论文。
- **需申请访问**：接口明确声明访问门槛；不等于链接失效或未发布。
- **暂无法判断 / 访问失败**：可能是限流、权限、网络、404 或解析问题；不推断作者未开放。
- **仅保存链接**：当前没有适配器，链接仍可保存，但不会由服务端访问。

“用户标记官方”“声明已发布”均为用户输入，要求填写依据；自动检查不会把这些人工记录覆盖成机器结论。README 外链可能属于基线、依赖或相关工作，需要用户确认后添加，系统不会自动跟随。

支持的自动检查链接示例：

```text
https://github.com/<owner>/<repo>
https://huggingface.co/<owner>/<model>
https://huggingface.co/datasets/<owner>/<dataset>
```

不支持自动检查任意网页、GitHub 文件页、Hugging Face Spaces、网盘或自定义端口。只需保存它们时仍可作为资源记录使用。

## 数据与安全

默认 SQLite 文件位于 `.data/re0.sqlite3`，重启不会丢失。**没有账号、认证、多用户隔离或加密存储；不要暴露到公网或不可信局域网。** 默认只监听 `127.0.0.1`。

外部接口仅在明确查询元数据或点击检查时访问；元数据查询发送标识符，资源检查发送仓库标识符，不发送论文笔记或 PDF。本版不包含 PDF 上传。可选 GitHub token 只发送给 `api.github.com`，不进入数据库或浏览器。导出的 JSON 含笔记，请谨慎分享。

请求使用固定提供商白名单、不跟随重定向、限制请求次数/时长/响应大小；只读取元数据、README 和文件清单，不下载大型权重、不安装远端依赖、不执行远端代码。完整边界见 [SECURITY.md](SECURITY.md)。

### 配置

环境变量需要在启动前设置；`.env.example` **仅为示例，不会自动加载**。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `RE0_DB` | 仓库下 `.data/re0.sqlite3` | 数据库路径 |
| `RE0_HOST` | `127.0.0.1` | 监听地址；不要直接改成公网地址 |
| `RE0_PORT` | `8000` | 本地端口 |
| `GITHUB_TOKEN` | 空 | 可选，只用于 GitHub API；无 token 也可检查公开仓库，但更易限流 |
| `RE0_ALLOWED_HOSTS` | 本地主机集合 | Host 校验；不是身份认证，不建议扩大 |

当前网络客户端设置 `trust_env=False`，不读取环境中的代理配置。受限网络需要后续配置代理适配；连接失败只会保留失败证据，不会改判“未开源”。

### 安全备份

不要在应用运行时只复制 SQLite 主文件而忽略 WAL。使用标准库备份 API：

```bash
python scripts/backup.py --output backups/re0-before-upgrade.sqlite3
```

输出文件存在时拒绝覆盖。恢复时先停止应用，然后用新的路径启动，避免覆盖已有库：

```bash
# macOS / Linux；Windows 可先设置 $env:RE0_DB
RE0_DB=backups/re0-before-upgrade.sqlite3 python run.py
```

JSON 用于查看与转移数据结构，目前**没有 JSON 一键恢复入口**；完整恢复请使用 SQLite 备份。论文删除会级联删除其资源与历史，界面有确认，请先备份。

## 测试与开发

```bash
python -m pip install -e '.[test]'
python -m pytest
# 可选，Node.js 20+；不需要 npm install
npm test
npm run check
```

可选浏览器测试：

```bash
python -m pip install playwright
python -m playwright install chromium
python scripts/browser_smoke.py
```

已有 Chromium 时可用 `CHROMIUM_PATH` 指定。浏览器测试采用 **Chromium DOM + 真实 FastAPI TestClient 桥接**，不绕过运行环境的浏览器网络政策，也不冒充真实 TCP / 外网 / 部署端到端测试。详见 [测试记录](docs/TESTING.md)。

## Docker（可选配置）

```bash
docker compose up --build
```

仅映射宿主机 `127.0.0.1:8000`，容器以非 root 身份运行，数据放入命名卷。**本次环境没有 Docker，配置未进行镜像构建验证。** 最先体验请使用上述 Python 启动方式。不要使用 `docker compose down -v`，除非明确要删除数据卷。

## 仓库与后续开发

远端仓库：`sakur7a/Research-From-Zero`。克隆后按上方 Python 步骤启动：

```bash
git clone https://github.com/sakur7a/Research-From-Zero.git re0
cd re0
```

本地 ZIP 也包含相同应用源码。不要把 `.data`、笔记导出或密钥提交到仓库。

下一阶段优先根据实际使用反馈改进：真实论文的资源审计评测、更多资源适配器、可恢复导入、Zotero 只读同步；再考虑受证据约束的 agent 和理论结构。见 [ROADMAP.md](docs/ROADMAP.md) 与 [架构说明](docs/ARCHITECTURE.md)。

许可证尚未由项目所有者选择；不要仅因能看到源码就认为获得了开源授权。第三方依赖保持各自许可证。详见 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)。
