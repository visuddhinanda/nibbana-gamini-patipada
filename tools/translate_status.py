#!/usr/bin/env python3
"""Show vol_5 translation progress (resumable position) from the manifest.

A source file is DONE when its target directory already contains a .md file
whose leading [page] key matches the source's key. Progress is therefore derived
purely from the filesystem — it survives crashes/restarts with no separate state.

Usage:
  python3 tools/translate_status.py            # counts + next pending files
  python3 tools/translate_status.py --list N   # list N pending source paths
  python3 tools/translate_status.py --verify   # also run check_lines/check_terms on done files
"""
import argparse
import os
import re
import subprocess
import sys

MANIFEST = "vol_5/chinese/_manifest.tsv"
CHINESE_ROOT = "vol_5/chinese"
KEY_RE = re.compile(r"^\s*(\[\d+\][a-z]?)")


def key_of(name):
    m = KEY_RE.match(name)
    return m.group(1) if m else None


def load_manifest():
    rows = []
    for ln in open(MANIFEST, encoding="utf-8"):
        ln = ln.rstrip("\n")
        if not ln or ln.startswith("#"):
            continue
        rows.append(ln.split("\t"))
    return rows


def target_for(row):
    """Return (target_dir, source_key, existing_target_path_or_None)."""
    src, tgt_reldir, key = row[0], row[1], row[2]
    tdir = os.path.join(CHINESE_ROOT, tgt_reldir) if tgt_reldir else CHINESE_ROOT
    found = None
    if os.path.isdir(tdir):
        for fn in os.listdir(tdir):
            if fn.endswith(".md") and key_of(fn) == key:
                found = os.path.join(tdir, fn)
                break
    return tdir, key, found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", type=int, default=10, help="how many pending to list")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    rows = load_manifest()
    done, pending = [], []
    for r in rows:
        _, _, found = target_for(r)
        (done if found else pending).append(r)

    print(f"vol_5 进度: {len(done)}/{len(rows)} 已完成, {len(pending)} 待译")

    if args.verify and done:
        bad = []
        for r in done:
            _, _, tgt = target_for(r)
            src = r[0]
            cl = subprocess.run([sys.executable, "tools/check_lines.py", src, tgt],
                                capture_output=True)
            ct = subprocess.run([sys.executable, "tools/check_terms.py", tgt],
                                capture_output=True)
            if cl.returncode or ct.returncode:
                bad.append((tgt, cl.returncode, ct.returncode))
        print(f"校验: {len(done)-len(bad)}/{len(done)} 通过两道校验")
        for tgt, c1, c2 in bad[:20]:
            flags = []
            if c1: flags.append("行不齐")
            if c2: flags.append("术语问题")
            print(f"   ✗ {os.path.basename(tgt)}  ({','.join(flags)})")

    if pending:
        print(f"\n接下来待译 {min(args.list, len(pending))} 个:")
        for r in pending[:args.list]:
            print(f"   p{r[3]:>3}  {r[0]}")


if __name__ == "__main__":
    main()
