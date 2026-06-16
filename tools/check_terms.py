#!/usr/bin/env python3
"""Post-translation terminology checker for the Chinese translation.

Does NOT modify text. It scans translated Chinese markdown files and, by the
convention 术语首现写成「中文（pali）」(e.g. 色（rūpa）), reports:

  1. 重复首现   — same Pali annotated with （pali） more than once
                  (the bracket should appear only at the term's first mention).
  2. 译法不一致 — the same Pali rendered with different Chinese terms,
                  or a Chinese term that disagrees with the glossary.
  3. 未知巴利   — an annotated Pali word not found in the glossary
                  (possible typo, or a new term to add to the glossary).
  4. 缺少首现   — (opt-in, --check-missing) a glossary Chinese meaning appears
                  in the text but its Pali was never introduced with （pali）.

Usage:
  python3 tools/check_terms.py vol_1/markdown/**/*.md
  python3 tools/check_terms.py --check-missing FILE...
  python3 tools/check_terms.py --json FILE... > report.json

Exit code is non-zero when any issue is found (handy for CI / pre-commit).
"""
import argparse
import json
import re
import sys
from collections import defaultdict

GLOSSARY = "glossary/glossary-merged.json"

# Chinese run immediately followed by a parenthesised Pali word.
# Supports full-width （） and half-width (), optional space before the paren.
CJK = r"[㐀-鿿《》·]"
PALI_INNER = r"[A-Za-zĀāĪīŪūṀṁṂṃṄṅÑñṆṇṬṭḌḍḶḷḸḹṢṣŚśṚṛṜṝḤḥṅṁ.̀-ͯ \-]"
ANNOT_RE = re.compile(
    rf"(?P<zh>{CJK}{{1,12}})\s*[（(](?P<pali>{PALI_INNER}{{2,40}}?)[）)]"
)


def load_glossary(path):
    """word(lowercased) -> set of accepted Chinese meanings.
    Keys are lowercased so matching is case-insensitive (glossary has some
    Capitalized proper-noun/title keys, e.g. Atthasālinī)."""
    raw = json.load(open(path, encoding="utf-8"))
    g = {}
    for w, ms in raw.items():
        g.setdefault(w.strip().lower(), set()).update(ms)
    return g


def norm_pali(s):
    return s.strip().lower()


def resolve_term(raw_zh, pali, glossary):
    """The regex grabs a generous run of CJK before （pali）; the real term is a
    suffix of it. If the Pali is known, pick the longest accepted meaning that is
    a suffix of raw_zh — that both isolates the term and confirms the rendering.
    Returns (term, consistent_with_glossary)."""
    # Only trim whitespace; do NOT strip 《》 — book-title terms like 《经集》
    # end in 》 and need it intact for the suffix match below.
    raw = raw_zh.strip()
    def _nobook(s):
        return s.replace("《", "").replace("》", "")
    if pali in glossary:
        cands = sorted(glossary[pali], key=len, reverse=True)
        for mn in cands:
            # match book-title terms whether or not 《》 are present on either side
            if raw.endswith(mn) or _nobook(raw).endswith(_nobook(mn)):
                return mn, True
        # no accepted meaning matches: keep last 4 chars as a guess, flag mismatch
        return raw[-4:], False
    # unknown pali: we can't know the term boundary; keep a short tail
    return raw[-4:], True


def scan(files, glossary, check_missing):
    # pali -> list of (term, file, lineno, consistent)
    annots = defaultdict(list)
    # corpus text per file for missing-mention heuristic
    file_lines = {}

    for fp in files:
        try:
            lines = open(fp, encoding="utf-8").read().splitlines()
        except OSError as e:
            print(f"!! cannot read {fp}: {e}", file=sys.stderr)
            continue
        file_lines[fp] = lines
        for i, line in enumerate(lines, 1):
            for m in ANNOT_RE.finditer(line):
                pali = norm_pali(m.group("pali"))
                term, ok = resolve_term(m.group("zh"), pali, glossary)
                annots[pali].append((term, fp, i, ok))

    issues = {"重复首现": [], "译法不一致": [], "未知巴利": [],
              "跨文件重复": [], "缺少首现": []}

    for pali, occ in annots.items():
        # 1. duplicate first-mention bracket — first mention is PER FILE, so only
        # multiple annotations of the same Pali WITHIN ONE FILE are errors;
        # cross-file repeats are expected (serial publication) → informational.
        from collections import Counter
        per_file = Counter(fp for _, fp, _, _ in occ)
        dup_files = [fp for fp, n in per_file.items() if n > 1]
        if dup_files:
            for fp in dup_files:
                locs = ", ".join(f"{fp}:{ln}" for _, f2, ln, _ in occ if f2 == fp)
                issues["重复首现"].append(f"{pali} 在同一文件内注解多次: {locs}")
        if len(per_file) > 1:
            issues["跨文件重复"].append(
                f"{pali} 在 {len(per_file)} 个文件各自首现（每篇重标，正常）"
            )
        # 2a. same pali, different Chinese renderings
        zh_variants = {term for term, _, _, _ in occ}
        if len(zh_variants) > 1:
            locs = "; ".join(f"{term} @{fp}:{ln}" for term, fp, ln, _ in occ)
            issues["译法不一致"].append(
                f"{pali} 出现多种译法 {sorted(zh_variants)}: {locs}"
            )
        # 2b. compare against glossary (consistency resolved during scan)
        if pali in glossary:
            accepted = sorted(glossary[pali])
            for term, fp, ln, ok in occ:
                if not ok:
                    issues["译法不一致"].append(
                        f"{fp}:{ln} 「…{term}」（{pali}）与术语表不一致，"
                        f"术语表建议: {accepted}"
                    )
        # 3. unknown pali
        else:
            issues["未知巴利"].append(
                f"{pali} 不在术语表中（{occ[0][1]}:{occ[0][2]} 等 {len(occ)} 处）"
            )

    # 4. opt-in: glossary meaning present but Pali never introduced
    if check_missing:
        introduced = set(annots.keys())
        # build reverse map: distinctive Chinese meaning -> pali
        meaning2pali = defaultdict(set)
        for pali, ms in glossary.items():
            for mn in ms:
                if len(mn) >= 2:  # skip 1-char meanings (too noisy)
                    meaning2pali[mn].add(pali)
        for fp, lines in file_lines.items():
            text = "\n".join(lines)
            seen = set()
            for mn, palis in meaning2pali.items():
                if mn in seen:
                    continue
                # only flag if NONE of its palis were introduced anywhere
                if palis & introduced:
                    continue
                if mn in text:
                    seen.add(mn)
                    issues["缺少首现"].append(
                        f"{fp}: 出现「{mn}」但未引入巴利"
                        f"（候选: {sorted(palis)[:3]}）"
                    )

    return issues


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="translated Chinese markdown files")
    ap.add_argument("--glossary", default=GLOSSARY)
    ap.add_argument("--check-missing", action="store_true",
                    help="also flag glossary meanings whose Pali was never introduced (noisy)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    glossary = load_glossary(args.glossary)
    issues = scan(args.files, glossary, args.check_missing)

    total = sum(len(v) for v in issues.values())
    if args.json:
        print(json.dumps(issues, ensure_ascii=False, indent=2))
    else:
        for cat, items in issues.items():
            if not items:
                continue
            print(f"\n## {cat} ({len(items)})")
            for it in items:
                print(f"  - {it}")
        print(f"\n共 {total} 项。检查文件 {len(args.files)} 个，术语表 {len(glossary)} 词。")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
