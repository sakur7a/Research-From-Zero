# v0.2 publication and upgrade

This commit publishes the previously delivered agent-first refactor. It builds
on the existing repository history; no force push or database replacement is
part of the upgrade. The original ZIP and patch were produced before publication
and therefore describe their original local-only delivery status.

## Baseline

- repository: `sakur7a/Research-From-Zero`
- parent baseline: `7ab5cedc67eb86f1aa3a3b900e67634abb52a881`
- baseline tree: `6b5c0fdfeffee6c3b2319e47f62320a554f36137`

Application code is the same as the previously delivered v0.2 ZIP. Publication
updates only documentation about delivery. No user databases, API keys, private
reports, `.env` files or generated build caches are included.

## Upgrade an existing checkout

Back up the database using the old application before replacing code:

```bash
python scripts/backup.py --output backups/re0-before-agent.sqlite3
```

Stop the old service and ensure the Git worktree is clean, then:

```bash
git status --short
git switch main
git pull --ff-only origin main
python -m pip install -r requirements.txt
python run.py
```

Do not discard local edits or force a merge when Git reports divergence. Preserve
those changes on a separate branch and reconcile them first. Do not replace
`.data` or `.env`. Backups contain private research data and must not be committed.

The homepage is now the agent workbench; `/library` retains the literature UI.
Configure and test a tool-calling model before starting an AI task. The model
connection test makes a real request and may incur charges.

## Fresh checkout or source archive

```bash
git clone https://github.com/sakur7a/Research-From-Zero.git re0
cd re0
```

Follow README for the Python environment and startup. A separate checkout or
unzipped directory uses an empty database by default. To reuse an old library,
back it up, stop the old service and set `RE0_DB` to the original database's
absolute path. Never run both versions against one database concurrently.

## Original patch

The original patch applies to the baseline above. It is an alternative to pulling
the new source, not another installation step. Do not apply it again after this
commit. For a clean baseline checkout, `git apply --check` must pass before
applying it; otherwise merge the changes instead of forcing an overwrite.

## Verification

Before publication, 92 Python tests, 20 JavaScript tests and JavaScript syntax
checks passed again locally. Browser and local HTTP validation from the initial
delivery are recorded in `docs/TESTING.md`. Model/provider responses in those
protocol tests are fixtures, not live research-quality evaluations.

Remote CI status is recorded in GitHub Actions for this commit. A source push,
local test pass, or old CI run is not proof of this commit's remote CI outcome.

## Docker Compose：托管模板

Compose 将容器内服务绑定到 `0.0.0.0:8000`，但把宿主机端口只发布在
`127.0.0.1:8000`。因为进程看不见 Docker 端口映射，模板明确使用
`RE0_MODE=hosted`；`RE0_SESSION_SECRET`、`RE0_PUBLIC_ENTRY`、
`RE0_ALLOWED_ORIGINS` 和 `RE0_STORAGE_MODE` 留空时服务会拒绝启动，不会退回无登录的本地模式。
TLS 由同一台机器上受信任的反向代理终止，代理再连回环端口。不要把容器端口改成
公网映射，也不要把本模板的存在当成公网安全验收。

Dockerfile 使用固定的 `python:3.13.15-slim-bookworm` 标签；Python 依赖由
`requirements.txt` 的精确版本约束。更新任一版本后，应重新构建并跑容器 HTTP 验收。

在部署主机上用密钥管理器设置三个托管变量，或在仅本人可读、已被 Git 忽略的本地
`.env` 中填写它们。生成新会话密钥时，先构建镜像再运行一次性命令：

```bash
docker compose build
docker compose run --rm --no-deps re0 python -m re0 auth secret
```

把命令给出的新值放进 `RE0_SESSION_SECRET`；再填写真实的 HTTPS 域名：

```dotenv
RE0_SESSION_SECRET=<fresh-random-value>
RE0_PUBLIC_ENTRY=https://research.example.org
RE0_ALLOWED_ORIGINS=https://research.example.org
RE0_STORAGE_MODE=persistent
```

Compose 默认选择 `persistent`，并挂载名为 `re0_data` 的卷。这个变量是运营契约，不会探测
卷是否真的挂载；备份和恢复步骤仍须按下文执行。临时演示环境应显式选择
`RE0_STORAGE_MODE=ephemeral-demo`，并告知访问者重启或休眠会丢失 SQLite 与工作区文件。

然后启动、创建首个账户：

```bash
docker compose up -d
docker compose exec re0 python -m re0 auth create-user
```

模型 Key 仍由每个用户在自己的登录会话里提交，只保留在服务进程内存；不要把模型 Key
写进 Compose 环境或 `.env`。

### 容器数据库备份与恢复

备份会使用 SQLite backup API，因此也包含已提交到 WAL 的数据。输出文件名必须未被使用：

```bash
docker compose exec re0 python scripts/backup.py --output /app/.data/re0-backup-20260923.sqlite3
```

注意：`backup.py` 只备份 SQLite。Agent Web 导入的来源 bundle 保存在同一数据卷的
`workspaces/` 子目录中，但不在 SQLite 内；完整灾备还要单独导出各 workspace JSON
或复制整个 `/app/.data/workspaces/` 目录。单独使用下面的 SQLite restore 不会重建这些来源文件。

恢复前先停止服务，并将目标备份放在同一持久卷里。恢复命令创建一个新数据库文件，先做
SQLite 完整性、外键和 Re0 schema 检查；它拒绝覆盖任何已有文件：

```bash
docker compose stop re0
docker compose run --rm --no-deps re0 python scripts/restore.py --source /app/.data/re0-backup-20260923.sqlite3 --destination /app/.data/re0-restored.sqlite3
```

检查新文件无误后，在私有 Compose 环境中将 `RE0_DB` 改为
`/app/.data/re0-restored.sqlite3` 并启动服务。原数据库和备份都不被改写；回滚时把
`RE0_DB` 改回旧路径。备份与数据库含有私人研究记录，应另行复制到加密的受控存储，不能
提交到仓库或和服务的唯一数据卷放在一起。

Compose/镜像构建、真实 TLS 反代、第二用户和独立设备验收需要 Docker 引擎及受控部署环境；
本仓库代码和离线测试不能替代这些发布门槛。

### Render Free 演示模板

仓库中的 [`render.yaml`](../render.yaml) 是唯一提议的免费演示路径，采用 Docker、单实例、
`/api/health` 健康检查和手动部署。它显式声明 `ephemeral-demo`：Render Free 没有持久磁盘，
休眠、重启或重新部署可能丢失数据库与工作区来源文件。页面会显示此限制。Blueprint 需要
部署者填写 `RE0_SESSION_SECRET`；`RENDER=true` 时应用只从平台的
`RENDER_EXTERNAL_URL` / `RENDER_EXTERNAL_HOSTNAME` 推导站点入口、来源与 Host allowlist，
不会从访客请求猜测。部署前仍要由项目所有者确认数据丢失策略、托管账户和竞赛规则；本仓库
当前没有创建 Render 服务，也没有公开 URL。
