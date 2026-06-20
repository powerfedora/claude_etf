"""
push.py - 把最新的 HTML 扫描报告发布到【公开】的 GitHub Pages 仓库
======================================================================
用法:
    python push.py          # 单独发布当前目录下最新的 report_*.html
    python main.py          # 跑完扫描后会自动调用本脚本(见 main.py 末尾)

首次准备(只做一次):
    1. 在 GitHub 上新建一个【公开(Public)】仓库, 例如 claude_etf_report,
       专门用来托管报告网页(不要用放策略代码的私有仓库)。
    2. 进入该仓库 Settings -> Pages -> Build and deployment,
       Source 选 "Deploy from a branch", Branch 选 main / (root), 保存。
    3. 把下面的 PAGES_REPO 改成这个仓库的地址(https 或 ssh 均可)。
    4. 发布后访问: https://<你的用户名>.github.io/<仓库名>/
       (最新一期是首页 index.html; 历史按 report_日期.html 归档)

注意: 这是【公开】网页, 任何人拿到链接都能看, 不要放入隐私信息。
"""
import shutil
import subprocess
import sys
from pathlib import Path

# ====== 配置: 改成你的【公开】Pages 仓库地址 ======
PAGES_REPO = "https://github.com/powerfedora/REPLACE_ME.git"
# ================================================

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".pages_publish"      # 本地克隆 Pages 仓库的工作目录(已被 .gitignore 忽略)


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"命令失败: {' '.join(cmd)}\n{r.stderr.strip() or r.stdout.strip()}")
    return r


def latest_report() -> Path:
    reports = sorted(ROOT.glob("report_*.html"))
    if not reports:
        sys.exit("没找到 report_*.html, 请先运行 python main.py 生成报告。")
    return reports[-1]


def pages_url() -> str:
    """根据仓库地址推断 GitHub Pages 访问地址。"""
    tail = PAGES_REPO.rstrip("/").removesuffix(".git")
    name = tail.split("/")[-1]
    owner = tail.split("/")[-2].split(":")[-1]   # 兼容 https 和 git@host:owner/repo
    return f"https://{owner}.github.io/{name}/"


def publish_latest() -> str:
    if "REPLACE_ME" in PAGES_REPO:
        sys.exit("请先在 push.py 顶部把 PAGES_REPO 改成你的【公开】Pages 仓库地址。")

    report = latest_report()

    # 准备本地工作副本: 已克隆则拉取最新, 否则克隆
    if (WORK / ".git").exists():
        run(["git", "pull", "--ff-only"], cwd=WORK, check=False)
    else:
        if WORK.exists():
            shutil.rmtree(WORK)
        run(["git", "clone", PAGES_REPO, str(WORK)])

    # 最新报告作为首页 index.html, 同时保留按日期归档的一份
    shutil.copy(report, WORK / "index.html")
    shutil.copy(report, WORK / report.name)

    run(["git", "add", "-A"], cwd=WORK)
    if not run(["git", "status", "--porcelain"], cwd=WORK).stdout.strip():
        print("报告无变化, 无需发布。")
    else:
        run(["git", "commit", "-m", f"publish {report.name}"], cwd=WORK)
        run(["git", "push"], cwd=WORK)
        print(f"已发布: {report.name}")

    url = pages_url()
    print(f"访问地址: {url}{report.name}  (最新: {url})")
    return url


if __name__ == "__main__":
    publish_latest()
