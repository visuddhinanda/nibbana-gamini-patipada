#!/usr/bin/env bash
# 单进程串行把多卷依次翻译完。顺序默认 vol_5 vol_1 vol_2 vol_3 vol_4。
# 对每一卷:
#   1) 缺脚手架则自动补(dirmap 用 claude -p 翻目录名, manifest, 翻译指令)
#   2) 串行逐文件翻译(translate_vol.sh, 撞配额则等待重试、不停止, 跳过已译)
#   3) 全卷译完 -> 生成 epub -> git commit
# 无并发, 从不 kill 任何进程(不影响 pali-translab)。
set -uo pipefail
ROOT="/mnt/visuddhinanda/workspace/nibbana-gamini-patipada"
cd "$ROOT" || exit 1

VOLS=(${VOLS:-vol_5 vol_1 vol_2 vol_3 vol_4})
GLOG="dist/translate_all.log"
mkdir -p dist
export WAIT_ON_LIMIT=1
export LIMIT_WAIT="${LIMIT_WAIT:-1800}"
export PER_FILE_TIMEOUT="${PER_FILE_TIMEOUT:-1800}"

glog() { echo "$*" | tee -a "$GLOG"; }

PYKEY='import re; KEY=re.compile(r"^\s*(\[[^\]]+\][A-Za-z]?)")'

pending_count() {  # $1=vol
  python3 - "$1" <<PY
import os,sys,re
$PYKEY
vol=sys.argv[1]; manifest=f"{vol}/chinese/_manifest.tsv"; root=f"{vol}/chinese"
def k(n):
    m=KEY.match(n); return m.group(1) if m else None
if not os.path.exists(manifest): print(-1); sys.exit()
n=0
for ln in open(manifest,encoding="utf-8"):
    ln=ln.rstrip("\n")
    if not ln or ln.startswith("#"): continue
    c=ln.split("\t"); treldir,key=c[1],c[2]
    tdir=os.path.join(root,treldir) if treldir else root
    done=os.path.isdir(tdir) and any(f.endswith(".md") and k(f)==key for f in os.listdir(tdir))
    if not done: n+=1
print(n)
PY
}

claude_wait() {  # 调 claude -p, 撞配额则等待重试。 $1=prompt  输出到 stdout
  local prompt="$1" out
  while :; do
    out="$(claude -p "$prompt" --dangerously-skip-permissions 2>&1)"
    if printf '%s' "$out" | grep -qiE 'session limit|usage limit|hit your .*limit|rate.?limit'; then
      glog "  ⏳ $(date '+%T') 脚手架调用撞配额, 等待 ${LIMIT_WAIT}s…"; sleep "$LIMIT_WAIT"; continue
    fi
    printf '%s' "$out"; return 0
  done
}

ensure_scaffold() {  # $1=vol
  local vol="$1"
  [ -f "$vol/chinese/_manifest.tsv" ] && return 0
  glog "[$(date '+%F %T')] $vol 缺脚手架, 自动生成…"
  mkdir -p "$vol/chinese"

  if [ ! -f "$vol/chinese/_TRANSLATE_INSTRUCTIONS.md" ]; then
    cat > "$vol/chinese/_TRANSLATE_INSTRUCTIONS.md" <<'INSTR'
# 翻译指令（每个翻译进程必读）

把指定的【一个】缅文源文件翻译为中文，单遍到位（逐行对齐 + 术语合规），写入指定目标目录。

## 规则
1. **底本=缅文**，译成现代汉语、清晰易懂的书面语；禁文言/古译经腔。
2. **逐行对齐（硬约束）**：中文行数必须与源完全相同；中文第 N 行=源第 N 行译文；源空行↔中文空行位置完全一致；禁止合并/拆分/折行/增删空行；一行很长也保持一行。文件末尾换行与源一致。
3. **术语**：查 manifest 该行 subset 列；术语表有的用表中用词。`glossary/errata.tsv` 中错误条目不采用，用正确译法。表中没有的词按知识库翻译并标注巴利。
4. **专有名词**（人名/地名）用术语表的巴利音译。
5. **术语首现**：术语在【本文件】首次出现时写「中文（pali）」，其后仅用中文。
6. **纯巴利句/行**：直接给罗马巴利转写，不翻译；缅文字体的巴利优先从英文参考取罗马转写。
7. **书名**固定为《去向涅槃之道》。
8. 保留 markdown 标记。
9. 英文参考(manifest english_ref 列)仅辅助理解，**禁止直译英文**。

## 输出与自验
- 目标文件名 = 把缅文源文件名翻译为中文，**原样保留开头的方括号 `[页码]` 前缀**（各卷形态不一：可能是 `[93]a`、`[186a]`、`[002a]`、`[000A]`、`[11]`、`[က]` 等，一字不差地保留；无方括号前缀的文件则正常翻译标题），**文件名内不加巴利**。写入指定目标目录。
- 自验直到通过：`python3 tools/check_lines.py "<源>" "<目标>"` 必须 ✓。
INSTR
    glog "  ✔ 写入 _TRANSLATE_INSTRUCTIONS.md"
  fi

  if [ ! -f "$vol/chinese/_dirmap.tsv" ]; then
    local dirs; dirs=$(find "$vol/markdown" -mindepth 1 -type d ! -path '*备份*' -printf '%P\n' | sort)
    if [ -z "$dirs" ]; then
      : > "$vol/chinese/_dirmap.tsv"
      glog "  ✔ 无子目录, 写空 _dirmap.tsv"
    else
      glog "  …生成 dirmap (claude -p 翻译目录名)"
      local p="下面是缅文佛典《去向涅槃之道》的源目录相对路径列表(每行一个)。把每个翻译成简体中文目录名:
- 输出 TSV, 每行 <原路径><TAB><中文路径>, 只输出这些行, 无表头/解释/代码块围栏。
- 保留每段开头的方括号 [前缀](如 [240]/[11]/[000A]/[က]) 原样不动。
- 多级路径逐段翻译; 同一缅文段在不同行必须译成相同中文。
- 上座部惯用译法: ပိုင်း=篇 ခန်း=章 ကထာ=论 နိဒ္ဒေသ=释; samādhi 定 jhāna 禅 vipassanā 内观 ñāṇa 智 等。

路径列表:
$dirs"
      claude_wait "$p" > "$vol/chinese/_dirmap.tsv"
      # 校验列数与覆盖
      if awk -F'\t' 'NF!=2{exit 1}' "$vol/chinese/_dirmap.tsv" && [ -s "$vol/chinese/_dirmap.tsv" ]; then
        glog "  ✔ _dirmap.tsv 生成 ($(wc -l < "$vol/chinese/_dirmap.tsv") 行)"
      else
        glog "  ⚠ _dirmap.tsv 格式异常, 将以缅文目录名回退(build_manifest 会警告)"
      fi
    fi
  fi

  python3 tools/build_manifest.py "$vol" >>"$GLOG" 2>&1
  glog "  ✔ manifest 生成: $(pending_count "$vol") 待译"
}

commit_vol() {  # $1=vol
  local vol="$1"
  git add "$vol/chinese" dist "tools/$(basename "$0")" tools/translate_vol.sh tools/build_manifest.py tools/build_epub.py >/dev/null 2>&1
  if git diff --cached --quiet; then glog "  (无改动可提交)"; return; fi
  local n; n=$(python3 - "$vol" <<'PY'
import sys,os
vol=sys.argv[1]; print(sum(1 for _ in (f for dp,_,fs in os.walk(f"{vol}/chinese") for f in fs if f.endswith(".md") and not f.startswith("_"))))
PY
)
  git commit -q -F - <<EOF
翻译 $vol：全卷完成($n 篇)，生成 epub

由 tools/translate_all.sh 单进程串行翻译，逐行对齐校验通过。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
  glog "  ✔ 已提交 $vol ($(git rev-parse --short HEAD))"
}

glog "[$(date '+%F %T')] ===== translate_all 启动, 顺序: ${VOLS[*]} ====="
for vol in "${VOLS[@]}"; do
  [ -d "$vol/markdown" ] || { glog "[$(date '+%F %T')] 跳过 $vol (无 markdown)"; continue; }
  ensure_scaffold "$vol"
  rem=$(pending_count "$vol")
  glog "[$(date '+%F %T')] >>> 开始 $vol (待译 $rem)"
  BUILD_EPUB=1 tools/translate_vol.sh "$vol" >>"$GLOG" 2>&1
  rem=$(pending_count "$vol")
  if [ "$rem" -eq 0 ]; then
    glog "[$(date '+%F %T')] <<< $vol 全部完成, 提交…"
    commit_vol "$vol"
  else
    glog "[$(date '+%F %T')] <<< $vol 仍剩 $rem 未译(非配额原因, 见日志), 跳过提交, 继续下一卷。"
  fi
done
glog "[$(date '+%F %T')] ===== translate_all 结束 ====="
