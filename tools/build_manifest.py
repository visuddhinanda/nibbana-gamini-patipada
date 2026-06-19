#!/usr/bin/env python3
"""Build a volume's translation manifest (resumable work-list).

For every Burmese source .md under <vol>/markdown (excluding 备份/), compute:
  source_path, target_reldir, key, page, english_ref, subset

- target_reldir comes from <vol>/chinese/_dirmap.tsv (src_reldir<TAB>tgt_reldir;
  root dir is the empty string "").  Target FILENAME is decided by the
  translator at translation time (it translates the Burmese title but keeps the
  leading [page] key), so the manifest stores only the directory + key.
- key = leading bracket token incl. optional letter, e.g. [187]a / [6] / [76].
  Unique within a directory; used to detect "already translated".
- english_ref / subset = the page-range reference files for that page. The page
  ranges are auto-derived from the filenames in <vol>/English-First-Edition-markdown
  (e.g. "Vol 1,Pg 001-40.md" -> pages 1..40; "Vol 5,Introduction.md" -> page 0).
  The matching subset is glossary/subsets/<same basename>.tsv. Pages that fall in
  a gap / beyond the last range fall back to the nearest range file.

Usage:
  python3 tools/build_manifest.py vol_1     # writes vol_1/chinese/_manifest.tsv
  python3 tools/build_manifest.py vol_5
"""
import argparse
import os
import re

# key 通用: 兼容 [93]a(vol_5) / [186a][002a](vol_1) / [000A](vol_4) / [က](vol_3) / [11](vol_2)
KEY_RE = re.compile(r"^\s*(\[[^\]]+\][A-Za-z]?)")
PAGE_RE = re.compile(r"\[0*(\d+)")
RANGE_RE = re.compile(r"Pg\s*0*(\d+)\s*-\s*0*(\d+)")
SUB_DIR = "glossary/subsets"


def derive_ranges(eng_dir):
    """Return (ranges, intro_prf). ranges = sorted [(lo, hi, basename)]."""
    ranges = []
    intro = None
    if not os.path.isdir(eng_dir):
        return ranges, intro
    for fn in os.listdir(eng_dir):
        if not fn.endswith(".md"):
            continue
        prf = fn[:-3]
        if "intro" in prf.lower():
            intro = prf
            continue
        m = RANGE_RE.search(prf)
        if m:
            ranges.append((int(m.group(1)), int(m.group(2)), prf))
    ranges.sort()
    return ranges, intro


def make_page_range_file(ranges, intro):
    def page_range_file(page):
        if page == 0 and intro:
            return intro
        for lo, hi, name in ranges:
            if lo <= page <= hi:
                return name
        if not ranges:
            return intro or ""
        # gap / out-of-bounds: nearest range by distance to its [lo,hi]
        def dist(r):
            lo, hi, _ = r
            if page < lo:
                return lo - page
            if page > hi:
                return page - hi
            return 0
        return min(ranges, key=dist)[2]
    return page_range_file


def load_dirmap(dirmap_path):
    m = {"": ""}
    if os.path.exists(dirmap_path):
        for ln in open(dirmap_path, encoding="utf-8"):
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
    ap = argparse.ArgumentParser()
    ap.add_argument("vol", nargs="?", default="vol_5", help="如 vol_1 vol_5")
    args = ap.parse_args()
    vol = args.vol.rstrip("/")

    root = f"{vol}/markdown"
    out = f"{vol}/chinese/_manifest.tsv"
    dirmap_path = f"{vol}/chinese/_dirmap.tsv"
    eng_dir = f"{vol}/English-First-Edition-markdown"

    ranges, intro = derive_ranges(eng_dir)
    page_range_file = make_page_range_file(ranges, intro)
    dirmap = load_dirmap(dirmap_path)

    rows = []
    missing_dirs = set()
    for dirpath, _, filenames in os.walk(root):
        if "备份" in dirpath:
            continue
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            src = os.path.join(dirpath, fn)
            reldir = os.path.relpath(dirpath, root)
            if reldir == ".":
                reldir = ""
            if reldir not in dirmap:
                missing_dirs.add(reldir)
            tgt_reldir = dirmap.get(reldir, reldir)
            page_m = PAGE_RE.search(fn)
            page = int(page_m.group(1)) if page_m else 0
            key = key_of(fn) or fn
            prf = page_range_file(page)
            eng = f"{eng_dir}/{prf}.md" if prf else ""
            sub = f"{SUB_DIR}/{prf}.tsv" if prf else ""
            rows.append((src, tgt_reldir, key, str(page), eng, sub))

    rows.sort(key=lambda r: (int(r[3]), r[0]))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("# source\ttarget_reldir\tkey\tpage\tenglish_ref\tsubset\n")
        for r in rows:
            f.write("\t".join(r) + "\n")
    print(f"manifest: {len(rows)} 源文件 -> {out}")
    print(f"  英文页段 {len(ranges)} 段" + (f", intro={intro}" if intro else ""))
    if missing_dirs:
        print("⚠ 以下目录未在 _dirmap.tsv 中(将原样保留缅文名):")
        for d in sorted(missing_dirs):
            print(f"   {d}")


if __name__ == "__main__":
    main()
