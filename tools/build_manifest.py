#!/usr/bin/env python3
"""Build the vol_5 translation manifest (resumable work-list).

For every Burmese source .md under vol_5/markdown (excluding 备份/), compute:
  source_path, target_reldir, key, page, english_ref, subset

- target_reldir comes from vol_5/chinese/_dirmap.tsv (src_reldir<TAB>tgt_reldir;
  root dir is the empty string "").  Target FILENAME is decided by the
  translator fork at translation time (it translates the Burmese title but keeps
  the leading [page] key), so the manifest stores only the directory + key.
- key = leading bracket token incl. optional letter, e.g. [187]a / [6] / [76].
  Unique within a directory; used to detect "already translated".
- english_ref / subset = the page-range reference files for that page.

Usage: python3 tools/build_manifest.py  > (writes vol_5/chinese/_manifest.tsv)
"""
import os
import re

ROOT = "vol_5/markdown"
OUT = "vol_5/chinese/_manifest.tsv"
DIRMAP = "vol_5/chinese/_dirmap.tsv"
ENG_DIR = "vol_5/English-First-Edition-markdown"
SUB_DIR = "glossary/subsets"

KEY_RE = re.compile(r"^\s*(\[\d+\][a-z]?)")


def page_range_file(page):
    """Return the English/subset basename (without extension) for a page."""
    if page == 0:
        return "Vol 5,Introduction"
    bounds = [
        (1, 50, "Vol 5,Pg1-50"),
        (51, 100, "Vol 5,Pg51-100"),
        (101, 150, "Vol 5,Pg101-150"),
        (151, 200, "Vol 5,Pg151-200"),
        (201, 250, "Vol 5,Pg201-250"),
        (251, 300, "Vol 5,Pg251-300"),
        (301, 348, "Vol 5,Pg301-350"),
        (349, 384, "Vol 5,Pg349-385#"),
        (385, 432, "Vol 5,Pg385-432#"),
        (433, 9999, "Vol 5,Pg433-539#"),
    ]
    for lo, hi, name in bounds:
        if lo <= page <= hi:
            return name
    return "Vol 5,Pg433-539#"


def load_dirmap():
    m = {"": ""}
    if os.path.exists(DIRMAP):
        for ln in open(DIRMAP, encoding="utf-8"):
            ln = ln.rstrip("\n")
            if not ln or ln.startswith("#"):
                continue
            src, tgt = ln.split("\t")
            m[src] = tgt
    return m


def key_of(name):
    mt = KEY_RE.match(name)
    return mt.group(1) if mt else None


def main():
    dirmap = load_dirmap()
    rows = []
    missing_dirs = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        if "备份" in dirpath:
            continue
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            src = os.path.join(dirpath, fn)
            reldir = os.path.relpath(dirpath, ROOT)
            if reldir == ".":
                reldir = ""
            if reldir not in dirmap:
                missing_dirs.add(reldir)
            tgt_reldir = dirmap.get(reldir, reldir)
            page_m = re.search(r"\[(\d+)\]", fn)
            page = int(page_m.group(1)) if page_m else 0
            key = key_of(fn) or fn
            prf = page_range_file(page)
            eng = f"{ENG_DIR}/{prf}.md"
            sub = f"{SUB_DIR}/{prf}.tsv"
            rows.append((src, tgt_reldir, key, str(page), eng, sub))

    rows.sort(key=lambda r: (int(r[3]), r[0]))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# source\ttarget_reldir\tkey\tpage\tenglish_ref\tsubset\n")
        for r in rows:
            f.write("\t".join(r) + "\n")
    print(f"manifest: {len(rows)} 源文件 -> {OUT}")
    if missing_dirs:
        print(f"⚠ 以下目录未在 _dirmap.tsv 中(将原样保留缅文名):")
        for d in sorted(missing_dirs):
            print(f"   {d}")


if __name__ == "__main__":
    main()
