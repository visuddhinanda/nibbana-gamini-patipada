#!/usr/bin/env python3
"""Extract a per-section glossary subset for translation.

The full glossary is ~150K+ tokens — too large to feed the LLM per chunk.
The English First Edition contains the Pali terms in *romanised* form, so for
each English reference markdown file (one page range) we find which glossary
Pali words actually occur, and write out just that subset (pali -> meanings).
Feed the subset alongside the Burmese source when translating that page range.

Outputs one file per input into glossary/subsets/:
  <name>.tsv   pali<TAB>meaning1 / meaning2 ...

Usage:
  python3 tools/extract_term_subset.py "vol_1/English-First-Edition-markdown/"*.md
  python3 tools/extract_term_subset.py --outdir glossary/subsets FILE...
"""
import argparse
import json
import os
import re
import sys

GLOSSARY = "glossary/glossary-merged.json"
OUTDIR = "glossary/subsets"

# A Pali "letter" = ASCII letter or a romanised-Pali diacritic letter.
PALI_LETTERS = "a-zA-ZĀāĪīŪūṀṁṂṃṄṅÑñṆṇṬṭḌḍḶḷḸḹṢṣŚśṚṛṜṝḤḥ"
TOKEN_RE = re.compile(f"[{PALI_LETTERS}]+")

# Short Latin tokens that are English/grammar noise, not Pali terms.
STOP = {
    "a", "an", "the", "of", "to", "in", "is", "as", "at", "be", "by", "or",
    "on", "it", "he", "we", "so", "no", "do", "if", "up", "me", "my", "us",
    "and", "for", "are", "but", "not", "you", "all", "can", "her", "him",
    "one", "two", "out", "see", "way", "who", "has", "his", "its", "may",
    "cf", "fr", "pl", "sg", "sk", "pr", "pp", "nt", "ti", "vs", "eg", "ie",
}


def is_pali_candidate(word):
    """Decide whether a glossary `word` should be matched against English text."""
    w = word.strip()
    if not w:
        return False
    # must be purely romanised letters / spaces / hyphens (drop Burmese, CJK,
    # digits, '@', all-caps grammar abbreviations, etc.)
    if not re.fullmatch(f"[{PALI_LETTERS} \\-]+", w):
        return False
    if w.isupper():
        return False
    # single-token terms must be long enough and not an English stopword
    if " " not in w and "-" not in w:
        return len(w) >= 3 and w.lower() not in STOP
    return True


def load_glossary(path):
    raw = json.load(open(path, encoding="utf-8"))
    singles, multis = {}, {}   # key(lowercased) -> (original_word, meanings)
    for word, meanings in raw.items():
        if not is_pali_candidate(word):
            continue
        key = word.lower()
        if " " in word or "-" in word:
            multis[key] = (word, meanings)
        else:
            singles[key] = (word, meanings)
    return singles, multis


def extract(text, singles, multis):
    low = text.lower()
    found = {}
    # single-word terms: tokenise and set-lookup (fast, word-boundary safe)
    tokens = {t.lower() for t in TOKEN_RE.findall(text)}
    for tok in tokens:
        if tok in singles:
            w, m = singles[tok]
            found[w] = m
    # multi-word / hyphenated terms: substring scan
    for key, (w, m) in multis.items():
        if key in low:
            found[w] = m
    return found


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="English reference markdown files")
    ap.add_argument("--glossary", default=GLOSSARY)
    ap.add_argument("--outdir", default=OUTDIR)
    args = ap.parse_args()

    singles, multis = load_glossary(args.glossary)
    os.makedirs(args.outdir, exist_ok=True)
    print(f"候选术语: {len(singles)} 单词 + {len(multis)} 多词 "
          f"(共 {len(singles)+len(multis)} / 术语表全量)")

    grand = 0
    for fp in args.files:
        try:
            text = open(fp, encoding="utf-8").read()
        except OSError as e:
            print(f"!! 跳过 {fp}: {e}", file=sys.stderr)
            continue
        found = extract(text, singles, multis)
        base = os.path.splitext(os.path.basename(fp))[0]
        out = os.path.join(args.outdir, base + ".tsv")
        with open(out, "w", encoding="utf-8") as f:
            for w in sorted(found):
                f.write(f"{w}\t{' / '.join(found[w])}\n")
        grand += len(found)
        print(f"  {os.path.basename(fp)} -> {len(found):4d} 术语  ({out})")
    print(f"完成。{len(args.files)} 个文件，命中术语合计 {grand} 条（含跨文件重复）。")


if __name__ == "__main__":
    main()
