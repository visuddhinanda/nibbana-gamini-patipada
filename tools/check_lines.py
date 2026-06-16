#!/usr/bin/env python3
"""Verify a Chinese translation is line-for-line aligned with its Burmese source.

Hard rule: the translation file must have the SAME number of lines as the source,
line N translating source line N, with blank lines in identical positions
(no merging, splitting, or reflowing).

This checks the structural part (counts + blank-line positions); it cannot judge
whether the content of each line is a faithful translation.

Usage:
  python3 tools/check_lines.py SOURCE.md TRANSLATION.md
  python3 tools/check_lines.py --pairs pairs.tsv      # SOURCE<TAB>TRANSLATION per line

Exit code non-zero if any file pair is misaligned.
"""
import argparse
import sys


def lines_of(path):
    # keep trailing-newline semantics consistent: splitlines drops the final \n
    return open(path, encoding="utf-8").read().split("\n")


def check_pair(src, tgt):
    s = lines_of(src)
    t = lines_of(tgt)
    problems = []
    if len(s) != len(t):
        problems.append(f"行数不一致: 缅文 {len(s)} 行, 中文 {len(t)} 行")
    n = min(len(s), len(t))
    for i in range(n):
        s_blank = (s[i].strip() == "")
        t_blank = (t[i].strip() == "")
        if s_blank != t_blank:
            kind = "缅文空/中文非空" if s_blank else "缅文非空/中文空"
            problems.append(f"第 {i+1} 行 空行不对应 ({kind})")
    return problems


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?")
    ap.add_argument("translation", nargs="?")
    ap.add_argument("--pairs", help="TSV file: source<TAB>translation per line")
    args = ap.parse_args()

    pairs = []
    if args.pairs:
        for ln in open(args.pairs, encoding="utf-8"):
            ln = ln.rstrip("\n")
            if not ln or ln.startswith("#"):
                continue
            src, tgt = ln.split("\t")
            pairs.append((src, tgt))
    elif args.source and args.translation:
        pairs.append((args.source, args.translation))
    else:
        ap.error("provide SOURCE TRANSLATION, or --pairs")

    total_bad = 0
    for src, tgt in pairs:
        probs = check_pair(src, tgt)
        name = tgt.split("/")[-1]
        if probs:
            total_bad += 1
            print(f"\n✗ {name}")
            for p in probs[:20]:
                print(f"    - {p}")
            if len(probs) > 20:
                print(f"    … 另有 {len(probs)-20} 处")
        else:
            print(f"✓ {name} 逐行对齐")
    print(f"\n{len(pairs)} 对文件，{total_bad} 个不对齐。")
    sys.exit(1 if total_bad else 0)


if __name__ == "__main__":
    main()
