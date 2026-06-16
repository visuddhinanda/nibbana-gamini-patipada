#!/usr/bin/env python3
"""Process wikipali term-vocabulary into local glossary files.

Source API (zh-Hans, community view):
  https://next.wikipali.org/api/v2/term-vocabulary?view=community&lang=zh-Hans
Keeps only the `word` (Pali) and `meaning` (Simplified Chinese) fields.

Outputs (in glossary/):
  - glossary.json   : [{"word","meaning"}, ...]  every row, order preserved
  - glossary.tsv    : word<TAB>meaning            every row, easy to grep
  - glossary-merged.json : {word: [meaning, ...]} deduped lookup table
"""
import json, os, sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "/tmp/term-vocab-raw.json"
OUT = "glossary"
os.makedirs(OUT, exist_ok=True)

rows = json.load(open(SRC, encoding="utf-8"))["data"]["rows"]

flat, merged = [], {}
for r in rows:
    w = (r.get("word") or "").strip()
    m = (r.get("meaning") or "").strip()
    if not w or not m:
        continue
    flat.append({"word": w, "meaning": m})
    merged.setdefault(w, [])
    if m not in merged[w]:
        merged[w].append(m)

with open(f"{OUT}/glossary.json", "w", encoding="utf-8") as f:
    json.dump(flat, f, ensure_ascii=False, indent=1)

with open(f"{OUT}/glossary.tsv", "w", encoding="utf-8") as f:
    for it in flat:
        f.write(f"{it['word']}\t{it['meaning']}\n")

with open(f"{OUT}/glossary-merged.json", "w", encoding="utf-8") as f:
    json.dump(merged, f, ensure_ascii=False, indent=1)

print(f"rows in source : {len(rows)}")
print(f"valid entries  : {len(flat)}  (dropped {len(rows)-len(flat)} empty)")
print(f"unique words   : {len(merged)}")
