#!/usr/bin/env python3
"""Concatenate a volume's Chinese translation files and build an EPUB via pandoc.

For each given volume (vol_5, vol_4, ...) it gathers every translated markdown
file under vol_X/chinese/ (in page order, across nested section folders),
joins them with a BLANK LINE between files (so a leading `##` in the next file
is not swallowed by the previous file's last line), prepends the AI-generation
notice, and runs pandoc to produce an EPUB.

Usage:
  python3 tools/build_epub.py vol_5
  python3 tools/build_epub.py vol_5 vol_4 vol_3
  python3 tools/build_epub.py --notice doc/ai-notice.md --outdir dist vol_5

Helper files whose name starts with "_" (e.g. _TRANSLATE_INSTRUCTIONS.md) are
skipped. Works even if a volume is only partially translated (handy for preview).
"""
import argparse
import os
import re
import subprocess
import sys

KEY_RE = re.compile(r"^\s*\[(\d+)\]([a-z]?)")


def sort_key(path):
    """Reading order: by [page] number, then optional letter suffix, then path."""
    base = os.path.basename(path)
    m = KEY_RE.match(base)
    page = int(m.group(1)) if m else 10**9
    suf = m.group(2) if m else ""
    return (page, suf, path)


def collect(vol):
    cdir = os.path.join(vol, "chinese")
    if not os.path.isdir(cdir):
        sys.exit(f"找不到目录: {cdir}")
    files = []
    for dp, _, fns in os.walk(cdir):
        for fn in fns:
            if fn.endswith(".md") and not fn.startswith("_"):
                files.append(os.path.join(dp, fn))
    files.sort(key=sort_key)
    return files


def build(vol, notice_path, outdir):
    files = collect(vol)
    if not files:
        print(f"⚠ {vol}: 没有可用的中文译文，跳过")
        return
    parts = []
    if notice_path and os.path.exists(notice_path):
        parts.append(open(notice_path, encoding="utf-8").read().rstrip("\n"))
    cdir = os.path.join(vol, "chinese")
    for fp in files:
        # 在每个文件内容前加上文件名（相对 chinese/ 的路径，便于核对来源）
        rel = os.path.relpath(fp, cdir)
        header = f"**【文件：{rel}】**"
        body = open(fp, encoding="utf-8").read().rstrip("\n")
        # rstrip trailing newlines so the join controls spacing exactly
        parts.append(f"{header}\n\n{body}")
    # BLANK LINE between every part -> leading "##" headings stay valid
    combined = "\n\n".join(parts) + "\n"

    os.makedirs(outdir, exist_ok=True)
    n = vol.split("_")[-1]
    title = f"去向涅槃之道_第{n}册"
    md_path = os.path.join(outdir, f"{vol}.combined.md")
    epub_path = os.path.join(outdir, f"{title}.epub")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(combined)

    cmd = [
        "pandoc", md_path, "-o", epub_path,
        "--from", "markdown",
        "--metadata", f"title={title}",
        "--metadata", "lang=zh-Hans",
        "--metadata", "author=帕奥西亚多 (AI 译)",
        "--toc", "--toc-depth=3",
        "--split-level=1",
    ]
    print(f"{vol}: 合并 {len(files)} 个文件 -> pandoc 生成 epub …")
    r = subprocess.run(cmd)
    if r.returncode == 0:
        print(f"  ✓ {epub_path}")
        print(f"    (合并源: {md_path})")
    else:
        print(f"  ✗ pandoc 失败 (exit {r.returncode})；合并 markdown 在 {md_path}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("volumes", nargs="+", help="如 vol_5 vol_4")
    ap.add_argument("--notice", default="doc/ai-notice.md",
                    help="开头的 AI 警示说明 markdown (默认 doc/ai-notice.md)")
    ap.add_argument("--outdir", default="dist", help="输出目录 (默认 dist/)")
    args = ap.parse_args()
    for vol in args.volumes:
        build(vol.rstrip("/"), args.notice, args.outdir)


if __name__ == "__main__":
    main()
