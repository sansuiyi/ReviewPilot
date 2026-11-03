"""PR-Agent 交互式控制台（游戏设置菜单风格）。

用法：
    .venv\\Scripts\\python.exe launch.py     # 或在项目根目录运行 rp.cmd

纯 UX 封装：只调用项目自带的 pr_agent.cli，不改动任何审查管线与配置体系。
层级页面导航（可前进/后退），Windows 交互终端支持方向键，其他环境退化为数字菜单。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import unicodedata
from pathlib import Path

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import msvcrt

ROOT = Path(__file__).resolve().parent

# ── 品牌信息 ──
APP_NAME = "ReviewPilot 代码合并审查控制台"
APP_VERSION = "v1.0-By.GYX"

CYAN, DIM, BOLD, GREEN, YELLOW, RED, RESET = (
    "\x1b[36m", "\x1b[2m", "\x1b[1m", "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[0m",
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 可选命令：(命令名, 说明)。local 模式只支持其中子集（见 LocalGitProvider 文档）。
COMMANDS = [
    ("review", "审查代码，指出 bug / 安全 / 测试问题，评估审查工作量"),
    ("describe", "生成 PR 标题、类型、摘要与代码走读"),
    ("improve", "给出可执行的代码改进建议"),
    ("ask", "就这次改动自由提问"),
    ("update_changelog", "根据改动内容更新 CHANGELOG"),
    ("add_docs", "为新增代码生成文档"),
    ("generate_labels", "为 PR 生成标签"),
]
LOCAL_COMMANDS = ("review", "describe", "improve")
LOCAL_OUTPUT = {"review": "review.md", "describe": "description.md", "improve": "improve.md"}


# ────────────────────────── 终端基础 ──────────────────────────

def _enable_ansi():
    if IS_WINDOWS:
        os.system("")  # 触发 conhost 启用 ANSI 转义序列


def _stdin_is_interactive():
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


USE_ARROW = IS_WINDOWS and _stdin_is_interactive()


def _read_key():
    """读取一个按键，返回 'up'/'down'/'enter'/'esc' 或字符本身。"""
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        return {"H": "up", "P": "down"}.get(ch2, "other")
    if ch == "\r":
        return "enter"
    if ch == "\x1b":
        return "esc"
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch


def vlen(s):
    """字符串的终端可见宽度（中文/全角按 2 列计，忽略 ANSI 转义）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in ANSI_RE.sub("", s))


_VERSION = ""


def _version():
    global _VERSION
    if not _VERSION:
        try:
            with open(ROOT / "pyproject.toml", "rb") as fh:
                _VERSION = tomllib.load(fh)["project"]["version"]
        except Exception:
            _VERSION = ""
    return _VERSION


def _term_width():
    return max(60, min(shutil.get_terminal_size((100, 30)).columns - 2, 96))


def wrap(text, width):
    return textwrap.wrap(text, width=max(20, width), replace_whitespace=False) or [""]


SEP = object()  # 面板内部水平分隔线哨兵


def panel(title, lines, width):
    """绘制一个 box-drawing 面板。title 为空则顶部为普通边框；SEP 为内部分隔线。"""
    inner = width - 6  # 左右各留 2 空格 + 2 边框
    if title:
        dash = "─" * max(0, width - vlen(title) - 5)
        out = [f"{CYAN}╭─ {BOLD}{title}{RESET}{CYAN} {dash}╮{RESET}"]
    else:
        out = [f"{CYAN}╭{'─' * (width - 2)}╮{RESET}"]
    for line in lines:
        if line is SEP:
            out.append(f"{CYAN}├{'─' * (width - 2)}┤{RESET}")
            continue
        segs = [line] if vlen(line) <= inner else wrap(line, inner)
        for seg in segs or [""]:
            pad = " " * max(0, inner - vlen(seg))
            out.append(f"{CYAN}│{RESET}  {seg}{RESET}{pad}  {CYAN}│{RESET}")
    out.append(f"{CYAN}╰{'─' * (width - 2)}╯{RESET}")
    return out


def draw_screen(breadcrumb, title, body_lines, desc=None):
    """整屏重绘：单面板 = 品牌行 + 面包屑行 + 分隔线 + 内容 (+ 分隔线 + 选中项说明)。"""
    w = _term_width()
    sys.stdout.write("\x1b[2J\x1b[H")
    gap = max(1, w - 6 - vlen(APP_NAME) - vlen(APP_VERSION))
    head = f"{CYAN}{BOLD}{APP_NAME}{RESET}" + " " * gap + f"{DIM}{APP_VERSION}{RESET}"
    crumb = f"{DIM}› {breadcrumb} ·{RESET} {BOLD}{title}{RESET}"
    lines = [head, crumb, SEP] + list(body_lines)
    if desc:
        lines.append(SEP)
        for seg in wrap(desc, w - 10):
            lines.append(f"{CYAN}⏵ {RESET}{DIM}{seg}{RESET}")
    for line in panel(None, lines, w):
        print(line)
    sys.stdout.flush()


def pause_enter():
    input(f"\n  {DIM}按 Enter 返回...{RESET}")


def ask_text(prompt, default=""):
    hint = f" {DIM}({default}){RESET}" if default else ""
    raw = input(f"\n  {CYAN}? {prompt}{RESET}{hint}: ").strip()
    return raw if raw else (default or None)


# ────────────────────────── 菜单组件 ──────────────────────────

def _menu_arrow(breadcrumb, title, items, intro=None):
    """方向键菜单。items: (label, desc) / ("--", 小节名) / None（间隔）。返回选中索引，Esc 返回 None。"""
    valid = [i for i, it in enumerate(items) if it and it[0] != "--"]
    idx = valid[0]
    while True:
        body = []
        if intro:
            body += [intro, ""]
        for i, it in enumerate(items):
            if it is None:
                body.append(f"  {DIM}{'·' * 24}{RESET}")
            elif it[0] == "--":
                body.append(f"{YELLOW}{BOLD}  ── {it[1]} ──{RESET}")
            elif i == idx:
                body.append(f"{CYAN}{BOLD}  ■ {it[0]}{RESET}")
            else:
                body.append(f"{DIM}  □ {it[0]}{RESET}")
        draw_screen(breadcrumb, title, body, desc=items[idx][1])
        key = _read_key()
        if key == "up":
            idx = valid[(valid.index(idx) - 1) % len(valid)]
        elif key == "down":
            idx = valid[(valid.index(idx) + 1) % len(valid)]
        elif key == "enter":
            return idx
        elif key == "esc":
            return None
        elif isinstance(key, str) and key.isdigit() and int(key) < len(valid):
            return valid[int(key)]


def _menu_numbered(breadcrumb, title, items, intro=None):
    """非交互/非 Windows 退化菜单：一次性展示，数字只给可选项编号，空输入返回。"""
    body = []
    if intro:
        body += [intro, ""]
    sel = []
    for i, it in enumerate(items):
        if it is None:
            body.append(f"  {DIM}{'·' * 24}{RESET}")
        elif it[0] == "--":
            body.append(f"{YELLOW}{BOLD}  ── {it[1]} ──{RESET}")
        else:
            body.append(f"  {CYAN}{len(sel)}{RESET}. {it[0]}  {DIM}{it[1]}{RESET}")
            sel.append(i)
    draw_screen(breadcrumb, title, body)
    while True:
        raw = input(f"  {CYAN}编号{RESET} (空输入返回): ").strip()
        if raw == "":
            return None
        if raw.isdigit() and 0 <= int(raw) < len(sel):
            return sel[int(raw)]
        print(f"  {YELLOW}无效编号，请重新输入{RESET}")


def menu(breadcrumb, title, items, intro=None):
    fn = _menu_arrow if USE_ARROW else _menu_numbered
    return fn(breadcrumb, title, items, intro)


def choose_command(breadcrumb, scope="github"):
    """返回 (命令名, 额外参数列表) 或 (None, [])。"""
    entries = [c for c in COMMANDS if c[0] in LOCAL_COMMANDS] if scope == "local" else COMMANDS
    items = [(f"{name} — {desc}", "") for name, desc in entries] + [("返回", "取消选择，回到上一级")]
    idx = menu(breadcrumb, "选择要执行的命令", items)
    if idx is None or items[idx][0] == "返回":
        return None, []
    return entries[idx][0], []


def info_page(breadcrumb, title, paragraphs):
    """纯展示页：Esc/空输入返回。paragraphs: list[str]，空串表示空行。"""
    w = _term_width()
    body = []
    for p in paragraphs:
        if p == "":
            body.append("")
        elif p.startswith(" "):
            body.append(f"  {p}")  # 空格开头 = 预格式行，不重新换行
        else:
            for i, seg in enumerate(wrap(p, w - 8)):
                body.append(f"{DIM}  {seg}{RESET}" if i else f"  {seg}{RESET}")
    draw_screen(breadcrumb, title, body)
    if USE_ARROW:
        _read_key()
    else:
        input()


# ────────────────────────── 命令执行 ──────────────────────────

def run_cli(args, cwd=None, extra_env=None):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    print(f"\n  {DIM}$ python -m pr_agent.cli {' '.join(args)}{RESET}\n")
    try:
        code = subprocess.call([sys.executable, "-m", "pr_agent.cli", *args], cwd=cwd, env=env)
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}已取消当前命令{RESET}")
        return
    print(f"\n  {GREEN}✔ 命令执行完成{RESET}" if code == 0
          else f"\n  {RED}✘ 命令失败（退出码 {code}）{RESET}")


def _resolve_repo(default):
    raw = ask_text("项目路径", default=default)
    if not raw:
        return None
    repo = Path(raw).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"  {RED}✘ 未找到 git 仓库: {repo}{RESET}")
        pause_enter()
        return None
    return repo


def _maybe_ask_question(rest):
    q = ask_text("要问 AI 的问题")
    if not q:
        return False
    rest.append(q)
    return True


# ────────────────────────── 审查流程页 ──────────────────────────

def page_online():
    crumb = "主页 › 代码审查 › ① 在线 PR 审查"
    while True:
        idx = menu(crumb, "在线 PR 审查（自动识别平台）", [
            ("粘贴 PR 网址开始", "支持 GitHub / GitLab / BitBucket / Azure DevOps / Gitea 等，按网址自动路由；结果发布为 PR 评论，自己的仓库还会自动打标签"),
            ("返回上级", "回到代码审查菜单"),
        ])
        if idx is None or idx == 1:
            return
        print(f"  {DIM}示例: https://github.com/组织/仓库/pull/编号{RESET}")
        url = ask_text("PR 网址（GitHub / GitLab / BitBucket / Azure 等均可）")
        if not url:
            continue
        if "/pull/" not in url and "/-/merge_requests/" not in url and "/pull-requests/" not in url:
            print(f"  {YELLOW}提示: 这看起来不像 PR 网址，仍会尝试执行{RESET}")
        cmd, rest = choose_command(crumb)
        if cmd is None:
            continue
        if cmd == "ask" and not _maybe_ask_question(rest):
            continue
        run_cli(["--pr_url", url, cmd, *rest])
        pause_enter()


def page_local_branch():
    crumb = "主页 › 代码审查 › ② 本地分支对比"
    while True:
        idx = menu(crumb, "审查本地分支改动",
                   [("选择项目并对比目标分支", "要求工作区改动已提交（干净）；结果写入仓库根目录 review.md / description.md / improve.md"),
                    ("返回上级", "回到代码审查菜单")])
        if idx is None or idx == 1:
            return
        repo = _resolve_repo(os.getcwd())
        if repo is None:
            continue
        target = ask_text("目标分支（对比基准）", default="main")
        if not target:
            continue
        cmd, rest = choose_command(crumb, scope="local")
        if cmd is None:
            continue
        run_cli(["--pr_url", target, cmd, *rest], cwd=str(repo),
                extra_env={"CONFIG__GIT_PROVIDER": "local"})
        print(f"  {DIM}提示: 输出文件在 {repo / LOCAL_OUTPUT[cmd]}{RESET}")
        pause_enter()


def page_diff_file():
    crumb = "主页 › 代码审查 › ③ diff 文件审查"
    while True:
        idx = menu(crumb, "审查 diff 文件",
                   [("指定 unified diff 文件", "无需 git 仓库；结果打印到终端"),
                    ("返回上级", "回到代码审查菜单")])
        if idx is None or idx == 1:
            return
        path = ask_text("diff 文件路径")
        if not path:
            continue
        p = Path(path).expanduser()
        if not p.exists():
            print(f"  {RED}✘ 文件不存在: {p}{RESET}")
            pause_enter()
            continue
        cmd, rest = choose_command(crumb)
        if cmd is None:
            continue
        if cmd == "ask" and not _maybe_ask_question(rest):
            continue
        run_cli(["--diff-file", str(p), cmd, *rest])
        pause_enter()


def page_git_change():
    crumb = "主页 › 代码审查 › ④ 审查未提交改动"
    while True:
        idx = menu(crumb, "审查当前仓库的 git 改动",
                   [("自动生成 diff 并审查", "从指定仓库提取差异，保存到临时文件后审查"),
                    ("返回上级", "回到代码审查菜单")])
        if idx is None or idx == 1:
            return
        repo = _resolve_repo(os.getcwd())
        if repo is None:
            continue
        scope = menu(crumb, "差异范围", [
            ("未提交改动", "暂存 + 未暂存，等价 git diff HEAD"),
            ("分支差异", "git diff <基准>...HEAD，需要已存在的基准分支"),
            ("返回", "取消本次审查，回到上一级"),
        ])
        if scope is None or scope == 2:
            continue
        if scope == 0:
            git_args = ["diff", "HEAD"]
        else:
            base = ask_text("基准分支", default="main")
            if not base:
                continue
            git_args = ["diff", f"{base}...HEAD"]
        res = subprocess.run(["git", *git_args], cwd=str(repo), capture_output=True)
        if res.returncode != 0:
            print(f"  {RED}✘ git 失败: {res.stderr.decode('utf-8', errors='replace').strip()}{RESET}")
            pause_enter()
            continue
        diff = res.stdout.decode("utf-8", errors="replace")
        if not diff.strip():
            print(f"  {YELLOW}没有检测到代码差异{RESET}")
            pause_enter()
            continue
        out = Path(tempfile.gettempdir()) / f"pr_agent_{time.strftime('%Y%m%d_%H%M%S')}.diff"
        out.write_text(diff, encoding="utf-8")
        print(f"  {DIM}已生成 diff: {out}（{len(diff.splitlines())} 行）{RESET}")
        cmd, rest = choose_command(crumb)
        if cmd is None:
            continue
        if cmd == "ask" and not _maybe_ask_question(rest):
            continue
        run_cli(["--diff-file", str(out), cmd, *rest])
        pause_enter()


def page_review():
    crumb = "主页 › 代码审查"
    while True:
        idx = menu(crumb, "代码审查", [
            ("--", "🔺 如何选择？"),
            ("1️⃣ 在线 PR 审查", "代码已推送并开了 PR；粘贴 PR 网址，结果发成 PR 评论，平台自动识别"),
            ("2️⃣ 本地分支对比", "代码在自己电脑、已提交、还没开 PR；对比基准分支，结果生成 review.md"),
            ("3️⃣ diff 文件审查", "手头有 .diff / .patch 文件；不需要 git 仓库，结果打印在终端"),
            ("4️⃣ 审查未提交改动", "代码刚写完还没 commit；自动跑 git diff 再审，结果打印在终端"),
            ("返回主菜单", ""),
        ])
        if idx is None or idx == 5:
            return
        elif idx == 1:
            page_online()
        elif idx == 2:
            page_local_branch()
        elif idx == 3:
            page_diff_file()
        elif idx == 4:
            page_git_change()


# ────────────────────────── 功能总览 ──────────────────────────

FEATURES = [
    ("代码审查（review）", "最常用：让 AI 找出改动里的 bug 和风险", [
        "什么时候用：改完代码，想在合并前让人（或 AI）把关一遍。",
        "",
        "它会做什么：",
        "  · 找出 bug、安全隐患、缺测试的地方",
        "  · 给出审查工作量评分（1-5，数字越大越要仔细看）",
        "  · 输出 Reviewer Guide：重点看哪里、为什么",
        "",
        "结果发到哪：在线 PR → PR 评论区；本地分支 → review.md 文件；diff 文件 → 直接打印在终端。",
    ]),
    ("生成 PR 描述（describe）", "不想手写 PR 标题和描述时用它", [
        "什么时候用：代码写完了，懒得写 PR 标题和描述。",
        "",
        "它会做什么：根据改动自动生成 PR 标题、变更类型、内容摘要和逐文件走读。",
        "",
        "结果发到哪：在线 PR → 直接更新 PR 描述；本地分支 → description.md 文件。",
    ]),
    ("改进建议（improve）", "让 AI 提出可执行的代码改进", [
        "什么时候用：想在合并前把代码质量再提一档。",
        "",
        "它会做什么：给出带代码片段的改进建议，按重要程度排序。",
        "",
        "结果发到哪：在线 PR → 评论；本地分支 → improve.md 文件。",
    ]),
    ("自由问答（ask）", "就当前改动向 AI 提任何问题", [
        "什么时候用：对这段改动有具体疑问，比如「有并发问题吗」「为什么改这里」。",
        "",
        "它会做什么：以当前改动内容为上下文回答你的问题。",
        "",
        "示例：ask \"这个改动有什么风险？\"",
    ]),
    ("更新日志（update_changelog）", "按惯例格式生成 CHANGELOG 条目", [
        "什么时候用：发版前整理 CHANGELOG。",
        "",
        "它会做什么：阅读改动内容，生成符合惯例的 CHANGELOG 追加条目。",
    ]),
    ("生成文档（add_docs）", "为新代码补文档", [
        "什么时候用：新增了函数或类，但还没写文档。",
        "",
        "它会做什么：找出缺文档的公共接口，生成 docstring / Wiki 内容。",
    ]),
    ("自动打标签（generate_labels）", "给 PR 自动打标签（需要仓库权限）", [
        "什么时候用：维护自己的仓库，想让 PR 自动分类。",
        "",
        "注意：对别人的仓库会 403（没有写权限），评论不受影响。",
    ]),
    ("审查入口怎么选？（4 种方式）", "重点：同一个 AI 引擎，4 种喂代码的方式", [
        "详见：主页 › 代码审查 › 四种方式怎么选。",
        "",
        "  已开 PR → 在线审查 | 已提交未开 PR → 本地分支对比 | 有 diff 文件 → 文件审查 | 还没提交 → 审查未提交改动",
    ]),
    ("支持哪些平台？", "10 个内置平台，粘贴网址自动识别", [
        "GitHub、GitLab、BitBucket（云/服务器）、Azure DevOps、Gitea、Gerrit、CodeCommit、本地 git、纯 diff。",
        "",
        "不需要手动选平台：粘贴 PR 网址后，程序根据网址自动路由到对应平台。",
    ]),
    ("配置放在哪里？", "密钥 / 默认配置 / 仓库级覆盖 三层", [
        "  · 密钥和模型：pr_agent/settings/.secrets.toml（不会提交到 git）",
        "  · 全局默认：pr_agent/settings/configuration.toml",
        "  · 某仓库专属配置：该仓库根目录的 .pr_agent.toml",
        "",
        "注意：git_provider 等键不允许用命令行参数传（防注入），只能改配置文件或用环境变量。",
    ]),
]


def page_feature_detail(crumb, feat):
    info_page(crumb, f"功能详情 · {feat[0]}", [f"{BOLD}{feat[1]}{RESET}", ""] + feat[2])


def page_features():
    crumb = "主页 › 功能总览"
    feat_by_name = {f[0]: f for f in FEATURES}
    items = [
        ("--", "核心能力"),
        (FEATURES[0][0], FEATURES[0][1]),
        (FEATURES[1][0], FEATURES[1][1]),
        (FEATURES[2][0], FEATURES[2][1]),
        (FEATURES[3][0], FEATURES[3][1]),
        ("--", "辅助能力"),
        (FEATURES[4][0], FEATURES[4][1]),
        (FEATURES[5][0], FEATURES[5][1]),
        (FEATURES[6][0], FEATURES[6][1]),
        ("--", "系统能力"),
        (FEATURES[7][0], FEATURES[7][1]),
        (FEATURES[8][0], FEATURES[8][1]),
        (FEATURES[9][0], FEATURES[9][1]),
        ("返回主菜单", "回到主页"),
    ]
    while True:
        idx = menu(crumb, "功能总览 · 选择查看详情", items)
        if idx is None or items[idx][0] == "返回主菜单":
            return
        page_feature_detail(crumb, feat_by_name[items[idx][0]])


# ────────────────────────── 状态与配置 ──────────────────────────

def _load_status():
    info = {"version": "?", "model": "?", "providers": [], "github": "未配置", "github_user": "-"}
    try:
        with open(ROOT / "pyproject.toml", "rb") as fh:
            info["version"] = tomllib.load(fh)["project"]["version"]
    except Exception:
        pass
    try:
        with open(ROOT / "pr_agent/settings/.secrets.toml", "rb") as fh:
            data = tomllib.load(fh)
        info["model"] = data.get("config", {}).get("model", "?")
        for section, val in data.items():
            if isinstance(val, dict) and str(val.get("key", val.get("user_token", ""))).strip():
                info["providers"].append(section)
            if section == "github" and isinstance(val, dict):
                if str(val.get("user_token", "")).strip():
                    info["github"] = f"已配置（{val.get('deployment_type', 'user')}）"
    except Exception:
        pass
    return info


def page_status():
    crumb = "主页 › 状态与配置"
    s = _load_status()
    lines = [
        f"{BOLD}程序版本{RESET}    pr-agent {s['version']}",
        f"{BOLD}当前模型{RESET}    {s['model']}",
        f"{BOLD}GitHub{RESET}      {s['github']}",
        f"{BOLD}已配置密钥{RESET}  {', '.join(s['providers']) or '无'}",
        "",
        f"{DIM}修改模型/密钥：编辑 pr_agent/settings/.secrets.toml（该文件不提交）{RESET}",
        f"{DIM}仓库级行为覆盖：目标仓库的 .pr_agent.toml{RESET}",
    ]
    info_page(crumb, "当前状态", lines)


def page_guide():
    crumb = "主页 › 使用指南"
    info_page(crumb, "快速上手", [
        f"{BOLD}三步上手{RESET}",
        "① 主页 › 代码审查，选择一种输入方式（推荐先玩 diff 文件或 git 改动，零门槛）。",
        "② 选择命令（第一次建议 review）。",
        "③ 按提示填路径，等 AI 输出结果。",
        "",
        f"{BOLD}常见问题{RESET}",
        "· 给别人仓库打标签 403？正常，打标签需要仓库管理员权限，评论不受影响。",
        "· 本地分支对比报「工作区不干净」？先 commit 或 stash 未提交改动。",
        "· 想换模型？编辑 pr_agent/settings/.secrets.toml 的 [config] model。",
        "· 命令执行中断？Ctrl+C 只取消当前命令，自动回到菜单。",
    ])


# ────────────────────────── 主框架 ──────────────────────────

def page_main():
    crumb = "主页"
    while True:
        idx = menu(crumb, "主菜单", [
            ("功能总览", "这个系统有什么功能、各在什么时候用，逐项查看说明"),
            ("代码审查", "四种入口：在线 PR / 本地分支 / diff 文件 / 未提交改动"),
            ("状态与配置", "版本、当前模型、已配置的密钥一览"),
            ("使用指南", "三步上手 + 常见问题"),
            ("退出", "结束本次会话"),
        ], intro=f"{DIM}代码审查 Agent 智能助手 · 多平台（GitHub / GitLab / BitBucket / Azure 等）· 同时支持本地代码审查{RESET}")
        if idx is None:
            return "exit"
        elif idx == 0:
            page_features()
        elif idx == 1:
            page_review()
        elif idx == 2:
            page_status()
        elif idx == 3:
            page_guide()
        elif idx == 4:
            return "exit"


def page_exit_confirm():
    idx = menu("主页 › 退出", "确定要退出吗？", [
        ("确认退出", "再见！"),
        ("返回主菜单", "继续使用"),
    ])
    return idx == 0


def main():
    _enable_ansi()
    # 切换到备用屏幕缓冲区（Claude Code / vim / less 同方案）：
    # 界面在独立画布上刷新，不进入终端滚动历史，退出后原终端内容完整还原
    sys.stdout.write("\x1b[?1049h\x1b[H\x1b[2J")
    sys.stdout.flush()
    try:
        while True:
            result = page_main()
            if result == "exit":
                if page_exit_confirm():
                    break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        # 无论正常退出 / Ctrl+C / 异常，都切回主屏幕，避免终端残留在备用画布
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()
    print(f"\n  {DIM}再见！{RESET}")


if __name__ == "__main__":
    main()
