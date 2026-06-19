#!/usr/bin/env bash
# 通用·逐文件批量翻译某一卷（缅->中），自包含(不依赖 translate_status.py)。
# 串行处理全部待译文件: 每个由 `claude -p` 翻译并立即写盘, 随后 check_lines 复核
# 逐行对齐。可中断后重跑(断点续译)。全卷译完后(可选)自动生成 epub。
#
# key 兼容两种命名: vol_5 字母在括号外 [93]a, vol_1 字母在括号内 [186a]。
#
# 用法:  tools/translate_vol.sh vol_1
#        OVERWRITE=1 tools/translate_vol.sh vol_1     # 已存在也重译
#        LIMIT=20    tools/translate_vol.sh vol_1     # 本次最多处理 20 个
#        BUILD_EPUB=0 tools/translate_vol.sh vol_1    # 译完不自动生成 epub
#
# 进度/日志写入  <vol>/chinese/_batch_translate.log
# 注意: 只新起 `claude -p` 子进程, 从不 kill 任何进程, 不影响 pali-translab。
set -uo pipefail

ROOT="/mnt/visuddhinanda/workspace/nibbana-gamini-patipada"
cd "$ROOT" || exit 1

VOL="${1:?用法: translate_vol.sh <vol_1|vol_5|...>}"
MANIFEST="$VOL/chinese/_manifest.tsv"
LOG="$VOL/chinese/_batch_translate.log"
INSTR="$VOL/chinese/_TRANSLATE_INSTRUCTIONS.md"
PER_FILE_TIMEOUT="${PER_FILE_TIMEOUT:-1800}"
LIMIT="${LIMIT:-0}"
OVERWRITE="${OVERWRITE:-0}"
BUILD_EPUB="${BUILD_EPUB:-1}"
WAIT_ON_LIMIT="${WAIT_ON_LIMIT:-0}"   # 1=撞配额时等待重试(不停止)
LIMIT_WAIT="${LIMIT_WAIT:-1800}"      # 等待秒数

[ -f "$MANIFEST" ] || { echo "找不到 manifest: $MANIFEST" >&2; exit 1; }
[ -f "$INSTR" ]    || { echo "找不到翻译指令: $INSTR" >&2; exit 1; }

log() { echo "$*" | tee -a "$LOG"; }

# 通用 key 正则: 兼容 [93]a/[186a]/[000A]/[က]/[11] 等各卷命名
PYKEY='import re; KEY=re.compile(r"^\s*(\[[^\]]+\][A-Za-z]?)")'

# 列出某卷全部"待译"源路径(按 page 排序), 每行一个
list_pending() {
  python3 - "$VOL" "$MANIFEST" <<PY
import os, sys, re
$PYKEY
vol, manifest = sys.argv[1], sys.argv[2]
root = os.path.join(vol, "chinese")
def k(n):
    m = KEY.match(n); return m.group(1) if m else None
rows = []
for ln in open(manifest, encoding="utf-8"):
    ln = ln.rstrip("\n")
    if not ln or ln.startswith("#"): continue
    c = ln.split("\t")
    rows.append(c)
def done(c):
    treldir, key = c[1], c[2]
    tdir = os.path.join(root, treldir) if treldir else root
    if not os.path.isdir(tdir): return False
    return any(f.endswith(".md") and k(f) == key for f in os.listdir(tdir))
rows = [c for c in rows if not done(c)]
rows.sort(key=lambda c: (int(c[3]) if c[3].isdigit() else 0, c[0]))
for c in rows:
    print(c[0])
PY
}

# 在目标目录找与 key 对应的已写文件(返回路径或空)
found_target() {  # $1=tdir $2=key
  python3 - "$1" "$2" <<PY
import os, sys, re
$PYKEY
tdir, key = sys.argv[1], sys.argv[2]
def k(n):
    m = KEY.match(n); return m.group(1) if m else None
if os.path.isdir(tdir):
    for f in sorted(os.listdir(tdir)):
        if f.endswith(".md") and k(f) == key:
            print(os.path.join(tdir, f)); break
PY
}

mapfile -t PENDING < <(list_pending)
total=${#PENDING[@]}
log "[$(date '+%F %T')] === 批量翻译 $VOL 开始：待译 $total 个 (timeout=${PER_FILE_TIMEOUT}s LIMIT=$LIMIT OVERWRITE=$OVERWRITE) ==="

done_cnt=0 ok=0 warn=0 fail=0 LIMIT_HIT=0
for src in "${PENDING[@]}"; do
  [ "$LIMIT" -gt 0 ] && [ "$done_cnt" -ge "$LIMIT" ] && { log "达到 LIMIT=$LIMIT, 停止本次。"; break; }
  done_cnt=$((done_cnt+1))

  row=$(awk -F'\t' -v s="$src" '$1==s{print; exit}' "$MANIFEST")
  if [ -z "$row" ]; then log "[$done_cnt/$total] ⚠ manifest 无此源, 跳过: $src"; warn=$((warn+1)); continue; fi
  treldir=$(printf '%s' "$row" | cut -f2)
  key=$(printf '%s'     "$row" | cut -f3)
  eng=$(printf '%s'     "$row" | cut -f5)
  subset=$(printf '%s'  "$row" | cut -f6)
  if [ -n "$treldir" ]; then tdir="$VOL/chinese/$treldir"; else tdir="$VOL/chinese"; fi
  mkdir -p "$tdir"

  if [ "$OVERWRITE" != "1" ]; then
    existing=$(found_target "$tdir" "$key")
    if [ -n "$existing" ]; then log "[$done_cnt/$total] ⏭ 已存在跳过 key=$key"; continue; fi
  fi

  log "[$done_cnt/$total] ▶ $(date '+%T') 翻译 key=$key  $src"

  prompt="你是把帕奥西亚多《去向涅槃之道》从缅文逐行翻译成简体中文的翻译器。

第一步: 用 Read 读 ${INSTR} 并严格遵循其中【全部】规则。

只翻译这一个源文件(底本=缅文, 逐句逐行译):
  ${src}

把译文用 Write 工具写入目录:
  ${tdir}/
目标文件名 = 把上面源文件名译成中文, 并【原样保留开头的方括号 [页码] 前缀(vol_1 形如 [186a]/[002a]/[769], 字母在括号内、可能补零, 一字不差)】, 文件名内不加巴利。

辅助材料:
  英文参考(仅帮助理解长难句, 禁止直译英文): ${eng}
  术语子集(pali->中文, 有则用表中用词): ${subset}
  勘误表(其中错误条目不采用, 用正确译法): glossary/errata.tsv

硬性要求: 逐行对齐——中文文件行数必须与源文件完全相同, 中文第N行=源第N行的译文, 源空行↔中文空行位置完全一致, 禁止合并/拆分/折行/增删空行; 术语首现写「中文(pali)」其后仅用中文; 纯巴利句/行只给罗马转写不翻译; 书名固定《去向涅槃之道》; 保留 markdown 标记。

写完后【必须】运行:
  python3 tools/check_lines.py \"${src}\" \"<你写的目标文件>\"
若不是逐行对齐就修正并重写, 直到 check_lines 显示 ✓。
不要 commit, 不要翻译其它文件, 不要输出多余说明。"

  while :; do
    tmpout=$(mktemp)
    if timeout "$PER_FILE_TIMEOUT" claude -p "$prompt" --dangerously-skip-permissions >"$tmpout" 2>&1; then rc=0; else rc=$?; fi
    if grep -qiE 'session limit|usage limit|hit your .*limit|rate.?limit' "$tmpout"; then
      rm -f "$tmpout"
      if [ "$WAIT_ON_LIMIT" = "1" ]; then
        log "[$done_cnt/$total] ⏳ $(date '+%T') 撞配额/限流, 等待 ${LIMIT_WAIT}s 后重试同一文件…"
        sleep "$LIMIT_WAIT"
        continue
      fi
      log "[$done_cnt/$total] ⛔ 撞配额/限流, 停止本次(已译保留, 配额恢复后重跑续译)。"
      LIMIT_HIT=1
      break 2
    fi
    cat "$tmpout" >>"$LOG"; rm -f "$tmpout"
    [ "$rc" != "0" ] && log "[$done_cnt/$total] ⚠ claude 退出码非0 (key=$key), 继续后验。"
    break
  done

  tgt=$(found_target "$tdir" "$key")
  if [ -z "$tgt" ]; then
    log "[$done_cnt/$total] ✗ 未写出译文 key=$key"; fail=$((fail+1)); continue
  fi
  if python3 tools/check_lines.py "$src" "$tgt" >/dev/null 2>&1; then
    log "[$done_cnt/$total] ✓ 完成且逐行对齐: $(basename "$tgt")"; ok=$((ok+1))
  else
    log "[$done_cnt/$total] ⚠ 已写出但行数不齐, 需重译: $(basename "$tgt")"; warn=$((warn+1))
  fi
done

remain=$(list_pending | wc -l)
hit_note=""; [ "$LIMIT_HIT" = "1" ] && hit_note=" (因配额停止)"
log "[$(date '+%F %T')] === 结束$hit_note: 本次 ✓$ok ⚠$warn ✗$fail。 $VOL 剩余待译 $remain ==="

if [ "$BUILD_EPUB" = "1" ] && [ "$remain" -eq 0 ]; then
  log "[$(date '+%F %T')] 全卷译完, 生成 epub …"
  python3 tools/build_epub.py "$VOL" >>"$LOG" 2>&1 && log "epub 生成完成。" || log "⚠ epub 生成失败, 见日志。"
fi
