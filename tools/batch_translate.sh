#!/usr/bin/env bash
# 逐文件批量翻译某一卷（缅->中）。串行处理全部待译文件，每个文件由 `claude -p`
# 翻译并立即写盘，脚本随后用 check_lines 复核逐行对齐。可中断后重跑（断点续译）。
#
# 用法:  tools/batch_translate.sh [vol_5]
#        OVERWRITE=1 tools/batch_translate.sh vol_5   # 已存在也重译
#        LIMIT=20    tools/batch_translate.sh vol_5   # 本次最多处理 20 个
#
# 进度/日志写入  <vol>/chinese/_batch_translate.log
# 注意: 本脚本只新起 `claude -p` 子进程, 从不 kill 任何进程, 不会影响 pali-translab。
set -uo pipefail

ROOT="/mnt/visuddhinanda/workspace/nibbana-gamini-patipada"
cd "$ROOT" || exit 1

VOL="${1:-vol_5}"
MANIFEST="$VOL/chinese/_manifest.tsv"
LOG="$VOL/chinese/_batch_translate.log"
INSTR="$VOL/chinese/_TRANSLATE_INSTRUCTIONS.md"
PER_FILE_TIMEOUT="${PER_FILE_TIMEOUT:-1800}"   # 单文件最长 30 分钟
LIMIT="${LIMIT:-0}"                            # 0 = 不限
OVERWRITE="${OVERWRITE:-0}"

[ -f "$MANIFEST" ] || { echo "找不到 manifest: $MANIFEST" >&2; exit 1; }
[ -f "$INSTR" ]    || { echo "找不到翻译指令: $INSTR" >&2; exit 1; }

log() { echo "$*" | tee -a "$LOG"; }

# 判断某目标目录是否已存在与 key 对应的译文(返回该路径), 无则空
found_target() {  # $1=tdir  $2=key
  python3 - "$1" "$2" <<'PY'
import sys, os, re
tdir, key = sys.argv[1], sys.argv[2]
KEY = re.compile(r'^\s*(\[\d+\][a-z]?)')
def k(n):
    m = KEY.match(n); return m.group(1) if m else None
if os.path.isdir(tdir):
    for f in sorted(os.listdir(tdir)):
        if f.endswith('.md') and k(f) == key:
            print(os.path.join(tdir, f)); break
PY
}

# 取待译源文件列表(按页码顺序), 每行一个源路径
mapfile -t PENDING < <(python3 tools/translate_status.py --list 99999 2>/dev/null \
  | grep -E '^[[:space:]]+p' \
  | sed -E 's/^[[:space:]]*p[[:space:]]*[0-9]+[[:space:]]+//')

total=${#PENDING[@]}
log "[$(date '+%F %T')] === 批量翻译 $VOL 开始：待译 $total 个 (timeout=${PER_FILE_TIMEOUT}s, LIMIT=$LIMIT, OVERWRITE=$OVERWRITE) ==="

done_cnt=0 ok=0 warn=0 fail=0
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
目标文件名 = 把上面源文件名译成中文, 并【保留开头的 [页码] 前缀(含字母如 [22]a)】, 文件名内不加巴利。

辅助材料:
  英文参考(仅帮助理解长难句, 禁止直译英文): ${eng}
  术语子集(pali->中文, 有则用表中用词): ${subset}
  勘误表(其中错误条目不采用, 用正确译法): glossary/errata.tsv

硬性要求: 逐行对齐——中文文件行数必须与源文件完全相同, 中文第N行=源第N行的译文, 源空行↔中文空行位置完全一致, 禁止合并/拆分/折行/增删空行; 术语首现写「中文(pali)」其后仅用中文; 纯巴利句/行只给罗马转写不翻译; 书名固定《去向涅槃之道》; 保留 markdown 标记。

写完后【必须】运行:
  python3 tools/check_lines.py \"${src}\" \"<你写的目标文件>\"
若不是逐行对齐就修正并重写, 直到 check_lines 显示 ✓。
不要 commit, 不要翻译其它文件, 不要输出多余说明。"

  if timeout "$PER_FILE_TIMEOUT" claude -p "$prompt" --dangerously-skip-permissions >>"$LOG" 2>&1; then
    :
  else
    log "[$done_cnt/$total] ⚠ claude 退出码非0 (key=$key), 继续后验。"
  fi

  tgt=$(found_target "$tdir" "$key")
  if [ -z "$tgt" ]; then
    log "[$done_cnt/$total] ✗ 未写出译文 key=$key"; fail=$((fail+1)); continue
  fi
  if python3 tools/check_lines.py "$src" "$tgt" >/dev/null 2>&1; then
    log "[$done_cnt/$total] ✓ 完成且逐行对齐: $(basename "$tgt")"; ok=$((ok+1))
  else
    log "[$done_cnt/$total] ⚠ 已写出但行数不齐, 需人工/重译: $(basename "$tgt")"; warn=$((warn+1))
  fi
done

prog=$(python3 tools/translate_status.py 2>/dev/null | head -1)
log "[$(date '+%F %T')] === 结束: 本次处理 $done_cnt, ✓$ok ⚠$warn ✗$fail。 $prog ==="
