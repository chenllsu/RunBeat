"""前端静态校验：确认 app.js 引用的每个元素 id 在 index.html 中真实存在。

这个检查能抓住前端最常见的一类 bug —— id 拼写不一致导致的事件绑定失效。

用法（在 runbeat 目录下）：
    .venv\\Scripts\\python.exe scripts\\check_frontend.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def main() -> int:
    html_path = STATIC / "index.html"
    js_path = STATIC / "app.js"
    css_path = STATIC / "style.css"

    for path in (html_path, js_path, css_path):
        if not path.is_file():
            print(f"缺少文件：{path}")
            return 2

    html = html_path.read_text(encoding="utf-8")
    js = js_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    problems: list[str] = []

    print("=" * 62)
    print("1. id 引用一致性")
    print("=" * 62)

    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js_ids = set(re.findall(r"\$\('([^']+)'\)", js))

    missing = sorted(js_ids - html_ids)
    unused = sorted(html_ids - js_ids)

    print(f"   index.html 定义 id : {len(html_ids)} 个")
    print(f"   app.js 引用 id     : {len(js_ids)} 个")
    if missing:
        print("   [BAD] app.js 引用了不存在的 id：")
        for item in missing:
            print("         -", item)
        problems.append(f"{len(missing)} 个 id 在 HTML 中不存在")
    else:
        print("   [OK ] 所有被 JS 引用的 id 都存在")

    if unused:
        print(f"   [提示] HTML 中定义但 JS 未直接引用的 id（{len(unused)} 个）：")
        print("         " + ", ".join(unused))

    print()
    print("=" * 62)
    print("2. querySelector 用到的选择器")
    print("=" * 62)
    selectors = sorted(set(re.findall(r"querySelector(?:All)?\('([^']+)'\)", js)))
    for sel in selectors:
        if sel.startswith("#"):
            root = sel[1:].split()[0].split(">")[0].strip()
            if root not in html_ids:
                print(f"   [BAD] {sel} -> #{root} 不存在")
                problems.append(f"选择器 {sel} 的根 id 不存在")
            else:
                print(f"   [OK ] {sel}")
        else:
            print(f"   [跳过] {sel}（非 id 选择器，需人工确认）")

    print()
    print("=" * 62)
    print("3. 元素 id 是否重复")
    print("=" * 62)
    all_ids = re.findall(r'\bid="([^"]+)"', html)
    duplicates = sorted({x for x in all_ids if all_ids.count(x) > 1})
    if duplicates:
        print("   [BAD] 重复的 id：", ", ".join(duplicates))
        problems.append(f"重复 id：{', '.join(duplicates)}")
    else:
        print(f"   [OK ] {len(all_ids)} 个 id 均唯一")

    print()
    print("=" * 62)
    print("4. CSS 花括号配对与可疑字符")
    print("=" * 62)
    if css.count("{") != css.count("}"):
        print(f"   [BAD] 花括号不匹配：{css.count('{')} 个 {{ ，{css.count('}')} 个 }}")
        problems.append("CSS 花括号不匹配")
    else:
        print(f"   [OK ] 花括号配对（各 {css.count('{')} 个）")

    # 先剥掉 /* ... */ 注释再查非 ASCII，否则中文注释会被误报成「可疑字符」。
    # 替换成等量换行，保持行号不变，报出来的行号才对得上原文件。
    def _blank_comment(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    cleaned = re.sub(r"/\*.*?\*/", _blank_comment, css, flags=re.S)
    non_ascii = [
        (i + 1, line)
        for i, line in enumerate(cleaned.splitlines())
        if re.search(r"[^\x00-\x7F]", line)
    ]
    if non_ascii:
        print(f"   [提示] CSS 中有 {len(non_ascii)} 行含非 ASCII 字符，确认是否为误输入：")
        for lineno, line in non_ascii[:10]:
            print(f"         第 {lineno} 行: {line.strip()[:90]}")
    else:
        print("   [OK ] CSS 中没有非 ASCII 字符")

    print()
    print("=" * 62)
    print("5. HTML 标签闭合粗查")
    print("=" * 62)
    for tag in ("div", "section", "button", "audio", "label"):
        opens = len(re.findall(rf"<{tag}[\s>]", html))
        closes = len(re.findall(rf"</{tag}>", html))
        flag = "OK " if opens == closes else "BAD"
        print(f"   [{flag}] <{tag}> 开 {opens} / 闭 {closes}")
        if opens != closes:
            problems.append(f"<{tag}> 标签未闭合（开 {opens} 闭 {closes}）")

    print()
    print("=" * 62)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for item in problems:
            print("  -", item)
        return 1
    print("前端静态校验全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
