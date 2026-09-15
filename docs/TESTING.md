# 验证记录 · v0.1.0 · 2026-09-15

## 实际执行

| 层次 | 命令/方式 | 结果 |
|---|---|---|
| 后端与备份 | `python -m pytest` | 60 passed |
| 前端纯逻辑 | `npm test` | 17 passed |
| JS 语法 | `npm run check` | 通过 |
| Python 语法 | `python -m compileall -q backend/re0 run.py scripts` | 通过 |
| 浏览器 DOM / API 联调 | `CHROMIUM_PATH=/usr/bin/chromium python scripts/browser_smoke.py` | 10 组流程通过，0 个 pageerror |
| 本地 HTTP 服务 | 实际启动 `python run.py`，请求 health、首页、JS、CSS、OpenAPI | 5 个路径均 HTTP 200 |

本次环境：Python 3.13.5、Node.js 22.16.0；FastAPI 0.128.2、Pydantic 2.13.4、HTTPX 0.28.1、Uvicorn 0.48.0、pytest 9.0.2。

机器可读记录：[browser-report.json](browser-report.json)、[http-smoke.json](http-smoke.json)。
交付的界面截图来自实际 DOM 渲染，全部论文/状态为显式虚构的演示数据。

## 测试覆盖

后端覆盖 CRUD、持久化、DOI/arXiv 去重、版本冲突、写入失败回滚、人工声明依据、检查缓存、追加历史、级联删除、演示数据隔离、CSL 预览/部分导入、导出、请求头与 Origin 校验、请求大小、URL 校验、SQLite WAL 备份与拒绝覆盖。

提供商用 HTTPX MockTransport 测试：提交版本固定、截断树、README 失败保留局部证据、限流/403/404/重定向/超时、候选文件名而非运行断言、HF gated 与文件列表分离、标识符匹配、明确 arXiv 版本不自动替换、拒绝 XML 实体、响应预算、token 主机范围。**Mock 测试通过不能证明真实上游接口当下可用。**

浏览器流程覆盖空库、只触发一次演示导入、搜索和选择对比、分类关系图、查看证据历史、新建与 HTML 转义、修改笔记、保存不支持链接并记录观测、CSL 文件预览导入、持久化/导出、390px 移动端无横向溢出和详情打开。另检查了桌面及移动端截图。

## 没有声称完成的验证

运行环境的 Chromium 网络导航被管理员策略禁止；没有修改或绕过该策略。浏览器测试采用离线页面载入与 TestClient 桥接，真实 API、校验与数据库仍被执行，但**不覆盖浏览器 TCP、模块网络加载、CSP 执行或部署端到端网络行为**。单独 HTTP 冒烟只验证服务和资源能返回，不能替代完整浏览器部署测试。

环境外部 DNS 不可用，因此 GitHub / Hugging Face / arXiv / Crossref 的实时成功路径尚未在此环境跑通。需要在用户环境验证网络、权限、提供商变化和代理需求。

没有全新虚拟环境从公网安装的验证，没有 Docker 镜像构建测试，没有公网部署。CI 文件已编写；该记录仅报告本地实际测试，不预先声称 GitHub Actions 成功。Python 3.11 仅配置进 CI 矩阵，本次本机运行验证的是 3.13。

没有自动理论分析、LLM 评测、实际模型加载、训练/评测运行或实验复现；不要把这些当作已测试能力。

## 首次真实使用检查

启动后先检查演示是否正常，再导入少量自己的论文，使用一个自己能访问的公开仓库进行检查。对照提供商界面核对版本、文件和访问门槛；真实失败应保留为 unknown/access_failed，并在问题记录中提供检查时间与脱敏上下文。
