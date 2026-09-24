"""Build the allowlisted, frontend-only Vercel demo into dist/static-demo."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
OUT = ROOT / "dist" / "static-demo"
ASSETS = (
    "theme-bootstrap.js", "theme.js", "theme.css", "agent.css", "search.css",
    "search.js", "search-core.js", "core.js", "api.js", "login-core.js", "icons.js", "skill.css", "skill.js",
    "favicon.svg",
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Remove only the known output directory, after resolving it under the repository's dist tree.
    if not OUT.resolve().is_relative_to((ROOT / "dist").resolve()):
        raise RuntimeError("Static output escaped dist")
    shutil.rmtree(OUT)
    (OUT / "static").mkdir(parents=True)
    (OUT / "samples").mkdir()
    for name in ASSETS:
        shutil.copy2(WEB / name, OUT / "static" / name)
    shutil.copy2(ROOT / "samples" / "lora-2026-09-22.json", OUT / "samples" / "lora-2026-09-22.json")

    search = (WEB / "search.html").read_text(encoding="utf-8")
    search = search.replace('<body>', '<body data-static-demo="true">')
    start = search.index('    <nav aria-label="主要导航">')
    end = search.index('</nav>', start) + len('</nav>')
    search = search[:start] + '''    <nav aria-label="主要导航"><a class="selected" href="/">交互式历史案例 <span>体验</span></a><a href="/skill.html">Skill 核验记录 <span>证据</span></a></nav>''' + search[end:]
    search = search.replace('href="/static/search.html"', 'href="/search.html"')
    search = search.replace('href="/static/skill.html"', 'href="/skill.html"')
    search = search.replace('读 <code>re0 paper search --json</code> 写出的那一份结果：检索覆盖、候选论文、资源审计矩阵、可复制的 BibTeX。检索本身在命令行或 skill 里跑，这一页不发检索请求。',
                            '从 2026-09-22 历史案例出发，查看论文候选、资源矩阵和来源。自己的 Re0 结果 JSON 也可在浏览器内打开。')
    search = search.replace('LOCAL WORKSPACE <span>v0.2</span>',
                            'STATIC DEMO <a href="/build-info.json">构建信息 ↗</a>')
    search = search.replace('重新载入结果', '切换结果')
    search = search.replace('尚未载入结果', '历史案例')
    search = search.replace('Re0 · 检索工作台', 'Re0 · 交互式历史案例')
    (OUT / "index.html").write_text(search, encoding="utf-8")
    (OUT / "search.html").write_text(search, encoding="utf-8")

    skill = (WEB / "skill.html").read_text(encoding="utf-8")
    skill = skill.replace('./search.html', '/search.html').replace('./skill.html', '/skill.html')
    for name in ("theme-bootstrap.js", "favicon.svg", "theme.css", "agent.css", "skill.css", "skill.js"):
        skill = skill.replace(f'./{name}', f'/static/{name}')
    skill = skill.replace('检索结果工作台 <span>JSON</span>', '交互式历史案例 <span>体验</span>')
    (OUT / "skill.html").write_text(skill, encoding="utf-8")
    (OUT / "404.html").write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>页面不存在 · Re0</title><h1>页面不存在</h1><p><a href="/">返回演示首页</a></p></html>', encoding="utf-8")
    (OUT / "vercel.json").write_text(json.dumps({"framework": None, "cleanUrls": False}, ensure_ascii=False), encoding="utf-8")

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    info = {"source_commit": commit, "built_at": datetime.now(timezone.utc).isoformat(),
            "scope": "static frontend demo; no API or backend"}
    (OUT / "build-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built {OUT} from {commit}: {sum(1 for file in OUT.rglob('*') if file.is_file())} allowlisted files")


if __name__ == "__main__":
    main()

