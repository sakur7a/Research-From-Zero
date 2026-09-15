# Architecture · v0.1.0

## Scope and trade-off

首版选择 Python/FastAPI、SQLite 和原生浏览器 ES modules，目标是单机立即体验。
不是 React 工程，也没有另外的 Node 服务；前端由 Python 同源提供。无需密钥的确定性静态检查先验证资源管理与证据工作流，后续 agent 只能在同一证据模型上扩展，不能替换成无来源的自由文本结论。

```
Browser: web/app.js + core.js + api.js
     │ same-origin /api
FastAPI: backend/re0/main.py
     ├─ service.py → db.py → SQLite (WAL, foreign keys)
     └─ providers.py → bounded, allowlisted HTTPS APIs
                           GitHub / Hugging Face / arXiv / Crossref
```

## Files

- `models.py`：受校验的论文、资源、声明与观测模型，标识符规范化。
- `db.py`：schema version 1、事务与外键；`service.py`：业务规则、保守导入、导出。
- `providers.py`：只读资源适配器和论文元数据解析；没有通用 URL 代理。
- `main.py`：API、输入限制、请求守卫、静态服务、短期检查缓存。
- `web/core.js`：可独立测试的标签、筛选、转义、视图数据；`app.js`：界面与交互。

## Data model

| 表 | 内容 | 不变约束 |
|---|---|---|
| papers | JSON 元数据、独立 DOI/arXiv 基础 ID 索引、时间、演示标记 | DOI / 无版本 arXiv ID 唯一；冲突不覆盖 |
| resources | 类型、URL、人工归属/声明、依据、适用版本、paper_id | 外键引用论文，归属与声明由用户提供依据 |
| observations | append-only JSON 观测、resource_id、检查时间 | 每次检查独立保存；仅明确删除资源/论文时级联删除 |
| topics | 多方向标签名 | 名称去重；不把同一篇论文限定在一个方向 |
| schema_version | 当前数据库结构版本 | 不支持的版本拒绝打开，不能假装自动迁移 |

每篇论文目前保存一个**当前版本标签**，不是完备的多版本 PaperVersion 实体。
每次检查额外保存论文标识符与版本的快照，避免编辑论文后旧证据被重新解释为针对新版本。
后续正式多版本数据模型需要迁移，而不是宣称本版已完全实现。

观测记录包括 `status / provider / revision / depth / scope / limitations / evidence /
indicators / discovered / content_sha256 / paper_version_snapshot`。`content_sha256` 是成功响应
的 SHA-256 序列再散列，便于辨认一次采集的响应；本版不保留全部原始响应，也不能靠指纹复原数据。
`revision` 针对提供商检查到的版本，不代表已经确认对应论文实验。

## Resource checking semantics

1. Validate a supported repository-root URL; otherwise preserve as unsupported without fetching.
2. Fetch provider metadata. GitHub resolves its default branch to a commit before scanning its tree/README.
3. Report filename candidates only. Scan at most 10 releases and 60,000 README characters; do not follow discovered links.
4. Preserve unknown/gated/failed states and partial-scan limitations.
5. Append observation with evidence and scope. Checks within 60 seconds reuse the latest record and do not append a duplicate.

GitHub Release 元数据没有绑定到默认分支提交，应按检查时间理解，不把它冒充仓库文件树快照。
Hugging Face 的 gated 声明与文件清单是独立维度；只拿到 gated 元数据时，depth 为 metadata_only。
许可证是提供商声明字段，不进行授权法律判断。

## Import and persistence

CSL JSON 导入首先预览；按 DOI、arXiv 基础 ID 和规范化标题保守去重。
逐条导入可能部分成功，返回 inserted/duplicates/errors，不静默覆盖笔记。
当前不导入附件、完整标签体系或 Zotero 同步游标；模型预留 Zotero identity 字段不等于实现同步。
JSON 导出含完整扩展信息但没有恢复入口；SQLite backup API 是首版恢复途径。

## API conventions

所有写请求使用 `Content-Type: application/json` 与 `X-Re0-Client: web`。
详细 schema 可在本地 `/openapi.json` 查看；`/docs` 的 Swagger UI 可能需要外部 CDN。

| Route | Purpose |
|---|---|
| GET /api/health | Version and single-user mode |
| GET, POST /api/papers | List/create |
| GET, PUT, DELETE /api/papers/{id} | Read/replace/delete |
| GET, POST /api/topics | Topics |
| POST /api/papers/{id}/resources | Add resource |
| DELETE /api/resources/{id} | Delete resource and history |
| GET /api/resources/{id}/observations | Full history |
| POST /api/resources/{id}/check | Bounded static audit |
| POST /api/metadata/resolve | Resolve one DOI/arXiv identifier |
| POST /api/import/csl | Preview or import CSL items |
| GET /api/export?format=json\|bibtex | Export |
| POST, DELETE /api/demo | Explicit seed/clear fictional data |

No authentication, scheduler, vector index, LLM, PDF parser or graph database is present.
These are possible later components, not hidden dependencies.
