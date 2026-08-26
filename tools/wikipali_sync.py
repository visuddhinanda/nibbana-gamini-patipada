#!/usr/bin/env python3
"""把 <vol>/chinese/ 下的译文和 wikipali 网站上的文章树对应起来，并上传译文。

设计见 doc/wikipali-sync-design.md。子命令：
  match    -- 拉取 article-map，按 key 把每篇文章匹配到本地译文文件，
              写出 <vol>/chinese/_wikipali_map.tsv
  verify   -- 对 match 匹配上的条目，逐篇拉取正文，按"空行分段的每段行数"
              校验本地译文与 wikipali 正文占位符是否逐行(含空行)对应
  login    -- 登录 wikipali，缓存用户 token（密码只经 getpass/系统密码框，不落盘）
  whoami   -- 查看当前站点与凭据状态（token 一律打码）
  model    -- 注册/复用 AI 模型身份并取模型 token（句子的作者署名）
  channels -- 列出当前账号可编辑的 channel
  upload   -- 以「一个 md 文件」为一个事务，逐篇校验并把译文按句子坐标写入 channel

凭据与插件 wikipali-plugins 共用 ~/.wikipali/credentials.json（0600），两边可互相顶替。

不引入第三方库，只用标准库。

Usage:
  python3 tools/wikipali_sync.py match  vol_5 <collection_uuid>
  python3 tools/wikipali_sync.py verify vol_5 <collection_uuid> [--limit N] [--sleep 0.2]
  python3 tools/wikipali_sync.py upload vol_5 <collection_uuid> --channel <uid> [--dry-run]
"""
import argparse
import base64
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_API_URL = "https://next.wikipali.org/api"
# 由 main() 按 --api / 凭据文件覆盖；match/verify/upload 都走它
API_BASE = DEFAULT_API_URL + "/v2"
USER_AGENT = "nibbana-gamini-patipada-wikipali-sync/1.0"

# 与 build_manifest.py / build_epub.py / translate_vol.sh 保持一致的通用 key 正则
KEY_RE = re.compile(r"^\s*(\[[^\]]+\][A-Za-z]?)")
# wikipali 标题里 key 前常带 "NGP-Vol5-" / "NGP-Vol-5-" 之类前缀，容器节点(level 1)则没有
TITLE_KEY_RE = re.compile(r"^(?:NGP-?Vol-?\d+-)?\s*(\[[^\]]+\][A-Za-z]?)")
# vol_4 的远端标题把 [页码] 放在**末尾**（`ရုပ်…အခန်း[010]`），与 vol_5 的开头式相反。
# 仅在开头式解析失败时回退，vol_5 行为不变。缅文标题末尾常跟零宽字符(U+200B/C/D)。
TITLE_KEY_TAIL_RE = re.compile(r"(\[[^\]]+\][A-Za-z]?)[\s​‌‍]*$")
# 带 NGP-Vol 前缀却在开头解析不出 key 的，是 `NGP-Vol-5[330]-1` / `NGP-Vol-5[332]`
# 这类后台遗留的位置后缀节点：它们与真正的缅文标题文章撞 key（vol_5 的
# `NGP-Vol-5[332]` 就撞了已上传的 `[332] ကြောက်မက်ဖွယ်…`），一律不做尾部回退。
NGP_PREFIX_RE = re.compile(r"^NGP-?Vol-?\d+")

MAP_HEADER = "article_id\tlevel\tchildren\tkey\ttitle\tlocal_path\tstatus\n"


class ApiError(RuntimeError):
    """HTTP 层失败：状态码是服务端的明确答复，不该被网络重试掩盖。"""

    def __init__(self, status, message, url=None):
        super().__init__(message)
        self.status = status
        self.url = url


def api_call(method, path, token=None, body=None, query=None,
             timeout=30, retries=2):
    """发一个 JSON 请求，返回 data 字段。

    只有网络层不可达才重试；HTTP 错误直接抛 ApiError。写入接口按坐标 firstOrNew，
    天然幂等，所以 POST 重试也是安全的。
    """
    # 路径里可能有巴利词，urllib 只接受 ASCII，必须先百分号编码
    url = API_BASE + "/" + urllib.parse.quote(path.lstrip("/"), safe="/")
    if query:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None})
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                msg = (json.loads(raw) or {}).get("message")
            except ValueError:
                msg = None
            raise ApiError(e.code, msg or f"HTTP {e.code}", url=url)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(1 + attempt)
    else:
        raise ApiError(0, f"连不上 {url}：{last_err}", url=url)

    if not payload.get("ok", False):
        raise ApiError(200, f"API 返回 ok=false: {payload.get('message')}", url=url)
    return payload.get("data")


def fetch_article_map(collection_id):
    data = api_call("GET", "article-map",
                    query={"view": "anthology", "id": collection_id})
    return data["rows"]


def fetch_article_content(article_id, token=None):
    # 个别文章匿名读会被拒（These credentials do not match our records），
    # 有用户 token 时一律带上
    data = api_call("GET", f"article/{article_id}", token=token)
    return data.get("content", "")


def key_of_filename(filename):
    stem = filename[:-3] if filename.endswith(".md") else filename
    m = KEY_RE.match(stem)
    return m.group(1) if m else None


def key_of_title(title):
    title = title.strip()
    m = TITLE_KEY_RE.match(title)
    if m:
        return m.group(1)
    if NGP_PREFIX_RE.match(title):
        return None
    m = TITLE_KEY_TAIL_RE.search(title)
    return m.group(1) if m else None


def build_local_index(vol):
    """扫描 <vol>/chinese/ 下所有正文 .md 文件，返回 key -> [路径,...]"""
    chinese_dir = os.path.join(vol, "chinese")
    index = {}
    for dirpath, _dirnames, filenames in os.walk(chinese_dir):
        for fn in filenames:
            if fn.startswith("_") or not fn.endswith(".md"):
                continue
            key = key_of_filename(fn)
            if key is None:
                continue
            index.setdefault(key, []).append(os.path.join(dirpath, fn))
    return index


def do_match(vol, collection_id):
    rows = fetch_article_map(collection_id)
    local_index = build_local_index(vol)

    out_rows = []
    n_matched = n_unmatched = n_parse_fail = n_dup_local = 0
    for r in rows:
        title = r["title"]
        key = key_of_title(title)
        if key is None:
            status = "parse_fail"
            local_path = ""
            n_parse_fail += 1
        else:
            candidates = local_index.get(key, [])
            if len(candidates) == 1:
                status = "matched"
                local_path = candidates[0]
                n_matched += 1
            elif len(candidates) > 1:
                status = "dup_local"
                local_path = ";".join(candidates)
                n_dup_local += 1
            else:
                status = "unmatched"
                local_path = ""
                n_unmatched += 1
        out_rows.append((
            r["article_id"], str(r["level"]), str(r["children"]),
            key or "", title, local_path, status,
        ))

    out_path = os.path.join(vol, "chinese", "_wikipali_map.tsv")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(MAP_HEADER)
        for row in out_rows:
            f.write("\t".join(row) + "\n")

    print(f"article-map 共 {len(rows)} 行 -> {out_path}")
    print(f"  matched={n_matched}  unmatched={n_unmatched}  "
          f"parse_fail={n_parse_fail}  dup_local={n_dup_local}")
    if n_unmatched:
        print("  未匹配(可能是容器节点，正文在带字母后缀的子文章里，属预期):")
        for row in out_rows:
            if row[6] == "unmatched":
                print(f"    {row[3]}\t{row[4]}")
    if n_parse_fail:
        print("  标题解析不出 key(需人工核对 wikipali 标题):")
        for row in out_rows:
            if row[6] == "parse_fail":
                print(f"    {row[4]}")
    if n_dup_local:
        print("  本地 key 重复(需人工消歧):")
        for row in out_rows:
            if row[6] == "dup_local":
                print(f"    {row[3]}\t{row[5]}")
    return out_rows


def load_map(vol):
    path = os.path.join(vol, "chinese", "_wikipali_map.tsv")
    if not os.path.exists(path):
        sys.exit(f"找不到 {path}，请先跑 match 子命令")
    rows = []
    with open(path, encoding="utf-8") as f:
        next(f)  # header
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln:
                continue
            rows.append(ln.split("\t"))
    return rows


# markdown 表格行。整张表格无论多少行都是**一个句子**，与远端的一个占位符相对应
# （实证：远端 [135] 的 {{3439-4-1-1}} 对应本地 12 行的表格）。
TABLE_LINE_RE = re.compile(r"^\s*\|")


def split_blocks(text):
    """按空行分段，返回每段的非空行列表。"""
    blocks, cur = [], []
    for line in text.split("\n"):
        if line.strip() == "":
            if cur:
                blocks.append(cur)
            cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def group_units(lines):
    """段内成句：连续的表格行并成一个单元，其余每行一个单元。"""
    units, table = [], []
    for line in lines:
        if TABLE_LINE_RE.match(line):
            table.append(line.rstrip())
            continue
        if table:
            units.append("\n".join(table))
            table = []
        units.append(line)
    if table:
        units.append("\n".join(table))
    return units


def blank_separated_block_lengths(text):
    """按空行分段，返回每段的句子数列表（表格整张算一句）。"""
    return [len(group_units(b)) for b in split_blocks(text)]


def non_blank_lines(text):
    """全文的句子单元列表（表格整张算一句），保持原顺序。

    分段后再成句，避免把相邻两段各自结尾/开头的表格误并成一张。
    """
    units = []
    for block in split_blocks(text):
        units.extend(group_units(block))
    return units


PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")


def do_verify(vol, collection_id, limit, sleep_sec, show_pairs):
    rows = load_map(vol)
    matched = [r for r in rows if r[6] == "matched"]
    if limit:
        matched = matched[:limit]

    n_ok = n_mismatch = n_error = 0
    for i, row in enumerate(matched):
        article_id, _level, _children, key, title, local_path, _status = row
        try:
            content = fetch_article_content(article_id)
        except (urllib.error.URLError, RuntimeError) as e:
            n_error += 1
            print(f"✗ {key}\t{title}\t抓取失败: {e}")
            continue

        local_text = open(local_path, encoding="utf-8").read()
        remote_blocks = blank_separated_block_lengths(content)
        local_blocks = blank_separated_block_lengths(local_text)

        if remote_blocks == local_blocks:
            n_ok += 1
            print(f"✓ {key}\t{title}\t{len(local_blocks)} 段对齐")
            if show_pairs:
                remote_lines = non_blank_lines(content)
                local_lines = non_blank_lines(local_text)
                for j in range(min(show_pairs, len(remote_lines))):
                    tags = " ".join(PLACEHOLDER_RE.findall(remote_lines[j])) or remote_lines[j][:40]
                    txt = local_lines[j].strip()
                    if len(txt) > 40:
                        txt = txt[:40] + "…"
                    print(f"      {tags}  ->  {txt}")
        else:
            n_mismatch += 1
            print(f"✗ {key}\t{title}")
            print(f"    wikipali {len(remote_blocks)} 段: {remote_blocks}")
            print(f"    本地译文 {len(local_blocks)} 段: {local_blocks}")
            n = min(len(remote_blocks), len(local_blocks))
            for j in range(n):
                if remote_blocks[j] != local_blocks[j]:
                    print(f"    第一处不同: 第 {j+1} 段, "
                          f"wikipali={remote_blocks[j]} 行, 本地={local_blocks[j]} 行")
                    break

        if sleep_sec and i < len(matched) - 1:
            time.sleep(sleep_sec)

    print(f"\n共校验 {len(matched)} 篇: OK={n_ok}  MISMATCH={n_mismatch}  抓取失败={n_error}")
    return n_mismatch == 0 and n_error == 0


# ---------------------------------------------------------------------------
# 凭据：~/.wikipali/credentials.json（0600），与 wikipali-plugins 共用同一份
# ---------------------------------------------------------------------------

CREDS_DIR = os.path.join(os.path.expanduser("~"), ".wikipali")
CREDS_PATH = os.path.join(CREDS_DIR, "credentials.json")

# 线上四个地址共享同一个库与同一把 jwt 密钥，凭据通用，共用 online 桶；
# 开发机是另一个库、另一把密钥，自成一桶。www/next 是代码版本，不是数据环境。
SITES = {
    "www": "https://www.wikipali.org/api",
    "www.cc": "https://www.wikipali.cc/api",
    "next": "https://next.wikipali.org/api",
    "next.cc": "https://next.wikipali.cc/api",
    "local": "http://127.0.0.1:8000/api",
}
ONLINE_URLS = [SITES["www"], SITES["www.cc"], SITES["next"], SITES["next.cc"]]

TOKEN_REFRESH_MARGIN = 3600  # 剩余有效期不足 1 小时就重签
WRITE_TIMEOUT = 120
DEFAULT_BATCH = 50


def note(msg):
    print(msg, file=sys.stderr)


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask(token):
    if not token:
        return "(无)"
    if len(token) <= 16:
        return token[:4] + "…"
    return token[:8] + "…" + token[-4:]


def token_expiry(token):
    """不验签地读出 JWT 的 exp，仅用于显示与判断何时重签。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part.encode("ascii"))).get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def fmt_ts(ts):
    if not ts:
        return "未知"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M")


def expand_api(value):
    value = (value or "").strip()
    if value in SITES:
        return SITES[value]
    if "://" in value:
        return value.rstrip("/")
    raise SystemExit(f"无法识别的站点：{value}。可用：{' / '.join(SITES)}，或给完整 url")


def bucket_name_for(api_url):
    return "online" if api_url in ONLINE_URLS else "site:" + api_url


def load_creds():
    if not os.path.exists(CREDS_PATH):
        return {"current": "online"}
    with open(CREDS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"凭据文件格式不对（{CREDS_PATH}），应为 JSON 对象")
    data.setdefault("current", "online")
    return data


def save_creds(creds):
    os.makedirs(CREDS_DIR, mode=0o700, exist_ok=True)
    tmp = CREDS_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(creds, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception:
        os.unlink(tmp)
        raise
    os.replace(tmp, CREDS_PATH)
    os.chmod(CREDS_PATH, stat.S_IRUSR | stat.S_IWUSR)


class Session:
    """凭据桶 + 当前站点。所有需要 token 的操作都从它取。"""

    def __init__(self, api_url, source):
        self.api_url = api_url
        self.source = source
        self.creds = load_creds()
        self.name = bucket_name_for(api_url)
        bucket = self.creds.setdefault(self.name, {})
        bucket.setdefault("api_url", api_url)
        bucket.setdefault("user", {})
        bucket.setdefault("model", {})
        bucket.setdefault("access_tokens", {})
        self.bucket = bucket

    @property
    def user_token(self):
        token = (self.bucket.get("user") or {}).get("token")
        if not token:
            raise SystemExit(
                "尚未登录。请执行：python3 tools/wikipali_sync.py login\n"
                "（密码只经 getpass 或系统密码框读入，不落盘、不进日志）")
        return token

    @property
    def model(self):
        model = self.bucket.get("model") or {}
        if not model.get("token"):
            raise SystemExit(
                "尚未取得模型身份 token。请执行："
                "python3 tools/wikipali_sync.py model --name claude-opus-5")
        return model

    def save(self):
        save_creds(self.creds)

    def api_note(self):
        src = {"cli": "--api", "creds": "凭据文件", "default": "内置默认"}[self.source]
        return f"{self.api_url}（来源：{src}）"


def resolve_api_url(cli_api):
    """地址优先级：--api > 凭据文件 > 内置默认。

    --api 是一次性覆盖，不写回凭据文件——否则「上周试了一次 www」会一直粘着。
    顺带把模块级 API_BASE 设好，match/verify/upload 共用同一个地址。
    """
    global API_BASE
    if cli_api:
        api_url, source = expand_api(cli_api), "cli"
    else:
        creds = load_creds()
        bucket = creds.get(creds.get("current", "online"))
        if isinstance(bucket, dict) and bucket.get("api_url"):
            api_url, source = bucket["api_url"].rstrip("/"), "creds"
        else:
            api_url, source = DEFAULT_API_URL, "default"
    API_BASE = api_url + "/v2"
    return api_url, source


def make_session(args):
    return Session(*resolve_api_url(getattr(args, "api", None)))


# ---------------------------------------------------------------------------
# login / whoami / model / channels
# ---------------------------------------------------------------------------

def gui_askpass(prompt, secret=True):
    """没有 TTY 时用操作系统的密码对话框读一行输入。

    密码由用户直接输给操作系统，不经过终端、不经过 argv，调用方全程看不到。
    没有图形界面时返回 None。
    """
    def run(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout.rstrip("\n") if p.returncode == 0 else None

    if sys.platform == "darwin":
        hidden = " with hidden answer" if secret else ""
        return run(["osascript", "-e",
                    f'display dialog "{prompt}" with title "WikiPali" '
                    f'default answer ""{hidden}',
                    "-e", "text returned of result"])
    if os.name == "nt":
        ps = (f'$s = Read-Host -AsSecureString "{prompt}"; '
              '[Runtime.InteropServices.Marshal]::PtrToStringAuto('
              '[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))'
              ) if secret else f'Read-Host "{prompt}"'
        return run(["powershell", "-NoProfile", "-Command", ps])
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    if shutil.which("zenity"):
        if secret:
            return run(["zenity", "--password", "--title", f"WikiPali — {prompt}"])
        return run(["zenity", "--entry", "--title", "WikiPali", "--text", prompt])
    if shutil.which("kdialog"):
        return run(["kdialog", "--title", "WikiPali",
                    "--password" if secret else "--inputbox", prompt])
    return None


def do_login(args):
    sess = make_session(args)
    print(f"登录站点：{sess.api_note()}")

    interactive = sys.stdin.isatty()
    use_gui = not interactive and not args.password_stdin

    username = args.username
    if not username:
        if interactive:
            username = input("用户名或邮箱：").strip()
        elif use_gui:
            username = (gui_askpass("用户名或邮箱", secret=False) or "").strip()
        else:
            sys.exit("错误：--password-stdin 模式必须同时给 --username。")
    if not username:
        sys.exit("错误：用户名为空。")

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    elif interactive:
        try:
            password = getpass.getpass("密码（不会被保存）：")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\n已取消。")
    else:
        password = gui_askpass(f"{username} 的 WikiPali 密码")
        if password is None:
            sys.exit(
                "错误：当前既没有交互式终端，也没有可用的图形界面，无法安全读取密码。\n"
                "  · 另开一个真正的终端跑本命令；\n"
                "  · 或自动化环境：... | python3 tools/wikipali_sync.py login "
                "--username <名字> --password-stdin\n"
                "无论哪种，都不要把密码写进命令行参数或打进与 AI 的对话。")
    if not password:
        sys.exit("错误：密码为空。")

    try:
        token = api_call("POST", "sign-in",
                         body={"username": username, "password": password})
    except ApiError as e:
        # sign-in 失败时服务端返回 400 + 'invalid token'，措辞会让人以为是 token 问题
        if e.status in (400, 401):
            sys.exit("错误：用户名或密码不正确。")
        raise
    finally:
        del password

    if not isinstance(token, str) or not token:
        sys.exit("错误：服务端没有返回 token。")

    current = api_call("GET", "auth/current", token=token)
    sess.bucket["user"] = {
        "uid": current.get("id"),
        "username": current.get("realName"),
        "nickname": current.get("nickName"),
        "token": token,
        "logged_in_at": iso_now(),
    }
    sess.save()
    print(f"登录成功：{current.get('nickName')}"
          f"（realName={current.get('realName')}，用作 studio_name）")
    print(f"token {mask(token)} 到期 {fmt_ts(token_expiry(token))}，"
          f"已写入 {CREDS_PATH}（0600）")
    print("下一步：python3 tools/wikipali_sync.py model --name claude-opus-5")
    return 0


def do_whoami(args):
    sess = make_session(args)
    print(f"API      : {sess.api_note()}")
    print(f"凭据文件 : {CREDS_PATH}（桶：{sess.name}）")
    user = sess.bucket.get("user") or {}
    if user.get("token"):
        print(f"用户     : {user.get('username')}  uid={user.get('uid')}")
        print(f"           token {mask(user['token'])} "
              f"到期 {fmt_ts(token_expiry(user['token']))}")
    else:
        print("用户     : (未登录)")
    model = sess.bucket.get("model") or {}
    if model.get("token"):
        print(f"模型     : {model.get('name')}  uid={model.get('uid')}")
        print(f"           token {mask(model['token'])} "
              f"到期 {fmt_ts(token_expiry(model['token']))}")
    else:
        print("模型     : (未注册)")
    for uid, item in (sess.bucket.get("access_tokens") or {}).items():
        scope = "全部 book" if item.get("book") == 0 else f"book {item.get('book')}"
        print(f"access   : {uid[:8]}… {item.get('channel_name', '')}  {scope}  "
              f"到期 {fmt_ts(item.get('exp'))}")
    return 0


def do_model(args):
    sess = make_session(args)
    token = sess.user_token
    name = args.name or (sess.bucket.get("model") or {}).get("name")
    if not name:
        sys.exit("必须指定模型名：--name <模型标识>（如 claude-opus-5）。"
                 "该名字会成为句子的作者署名，不要冒用别的模型的名字。")

    current = api_call("GET", "auth/current", token=token)
    studio_name = current.get("realName")
    if not studio_name:
        sys.exit("服务端没有返回 realName，无法确定 studio_name。")

    def lookup():
        # keyword 是 like %kw% 模糊匹配，必须客户端自己做精确比对
        listed = api_call("GET", "ai-model", token=token,
                          query={"view": "studio", "name": studio_name,
                                 "keyword": name}) or {}
        return next((r for r in (listed.get("rows") or []) if r.get("name") == name),
                    None)

    found = lookup()
    if found:
        print(f"已存在模型记录：{name}  uid={found['uid']}")
    else:
        try:
            found = api_call("POST", "ai-model", token=token,
                             body={"name": name, "studio_name": studio_name,
                                   "privacy": args.privacy})
            print(f"已创建模型记录：{name}  uid={found['uid']}")
        except ApiError as e:
            if e.status != 409:
                raise
            found = lookup()  # 409 = 同 studio 内重名，回查取 uid
            if not found:
                sys.exit(f"服务端说 {name} 已存在（409），但列表里查不到，无法继续。")
            print(f"已存在模型记录：{name}  uid={found['uid']}")

    issued = api_call("GET", f"ai-model-token/{found['uid']}", token=token)
    sess.bucket["model"] = {
        "uid": issued["uid"], "name": issued["name"],
        "token": issued["token"], "issued_at": iso_now(),
    }
    sess.save()
    print(f"模型身份 token 已缓存：{mask(issued['token'])}  "
          f"到期 {fmt_ts(token_expiry(issued['token']))}")
    print(f"写入的句子将署名为该模型（editor_uid={issued['uid']}）。")
    return 0


def fetch_channels(sess, search=None):
    data = api_call("GET", "channel", token=sess.user_token,
                    query={"view": "user-edit", "order": "updated_at",
                           "dir": "desc", "limit": 200, "search": search}) or {}
    return data.get("rows") or []


def do_channels(args):
    sess = make_session(args)
    rows = fetch_channels(sess, args.search)
    if args.search:
        # 服务端的 search 不总是生效，客户端再滤一遍，免得几百行里翻找
        rows = [c for c in rows
                if args.search.lower() in (c.get("name") or "").lower()]
    if not rows:
        print("当前账号没有任何可编辑的 channel。")
        return 1
    print(f"可编辑 channel（{len(rows)} 个，按更新时间倒序）：")
    for i, ch in enumerate(rows, 1):
        print(f"  {i:>3}) {ch.get('name', '')[:32]:<34} {str(ch.get('lang', '')):<8} "
              f"{ch.get('uid', '')}  {ch.get('role', '')}")
    return 0


def resolve_channel(sess, given):
    """把 uid / 名字片段解析成 (uid, name)。"""
    try:
        rows = fetch_channels(sess)
    except ApiError as e:
        # channel 列表接口挂过一次(2026-08-14 一直 500)。它只是用来取个名字，
        # 真正的权限闸门是签 access token(count: 0 = 无编辑权)，所以给了完整
        # uid 时降级继续，名字片段则没法猜，照旧中止。
        if len(given) >= 32:
            note(f"⚠ channel 列表接口不可用（{e}），无法回显名字；"
                 f"按 uid {given} 继续，编辑权仍以 access token 为准。")
            return given, None
        sys.exit(f"channel 列表接口不可用（{e}），无法按名字「{given}」解析，"
                 "请改用完整 uid。")
    for ch in rows:
        if ch.get("uid") == given:
            return ch["uid"], ch.get("name")
    matched = [c for c in rows if given.lower() in (c.get("name") or "").lower()]
    if len(matched) == 1:
        return matched[0]["uid"], matched[0].get("name")
    if len(matched) > 1:
        names = ", ".join(c.get("name", "") for c in matched[:5])
        sys.exit(f"「{given}」匹配到多个 channel：{names}…… 请给完整 uid。")
    if len(given) >= 32:
        note(f"⚠ {given} 不在可编辑列表中，仍按 uid 使用——"
             "签发 access token 时可能返回 count: 0。")
        return given, None
    sys.exit(f"找不到 channel：{given}")


def grant_access_token(sess, channel_uid, channel_name, book=0, force=False):
    """签一张 channel 编辑权 token。book=0 表示不限 book。"""
    cached = (sess.bucket.get("access_tokens") or {}).get(channel_uid)
    if cached and not force and cached.get("token") and cached.get("book") == book:
        exp = cached.get("exp") or token_expiry(cached["token"])
        if exp and exp - time.time() > TOKEN_REFRESH_MARGIN:
            return cached

    # book 必须是整数：服务端用 !== 严格比较，"0" !== 0 恒真会导致鉴权永远失败
    payload = [{"res_type": "channel", "res_id": channel_uid,
                "power": "edit", "book": int(book)}]
    data = api_call("POST", "access-token", token=sess.user_token,
                    body={"payload": payload}) or {}
    rows = data.get("rows") or []
    if not rows:
        # 无权时服务端静默跳过该条，rows 为空、HTTP 仍 200——等同 403，绝不能继续写
        sys.exit(
            f"签发 access token 返回 count: 0，说明当前账号对 channel "
            f"{channel_uid} 没有编辑权。\n"
            "已中止，未写入任何内容。请确认选对了 channel，或让 owner 授予 "
            "≥ editor 权限。")
    item = {
        "token": rows[0]["token"],
        "book": int(book),
        "exp": (rows[0].get("payload") or {}).get("exp"),
        "granted_at": iso_now(),
    }
    if channel_name:
        item["channel_name"] = channel_name
    sess.bucket.setdefault("access_tokens", {})[channel_uid] = item
    sess.save()
    return item


def refresh_model_token(sess):
    note("⚠ 模型 token 被拒（过期或已撤销），正在重新签发……")
    model = sess.bucket.get("model") or {}
    if not model.get("uid"):
        sys.exit("缓存里没有模型 uid，无法重签。请跑："
                 "python3 tools/wikipali_sync.py model --name <模型名>")
    issued = api_call("GET", f"ai-model-token/{model['uid']}", token=sess.user_token)
    model.update({"uid": issued["uid"], "name": issued["name"],
                  "token": issued["token"], "issued_at": iso_now()})
    sess.bucket["model"] = model
    sess.save()
    return issued["token"]


# ---------------------------------------------------------------------------
# 占位符 -> 句子坐标
# ---------------------------------------------------------------------------

# 正文里每个非空行恰好一个 {{book-paragraph-word_start-word_end}}，
# 与 POST /v2/sentence 的四个坐标字段一一对应
COORD_RE = re.compile(r"\{\{\s*(\d+)-(\d+)-(\d+)-(\d+)\s*\}\}")


class BuildError(Exception):
    """本篇无法安全地组句——记录原因、跳过整篇，绝不猜测性上传。"""


def split_template_line(line, lineno):
    """把远端模板行拆成 (前缀, 坐标, 后缀)。"""
    hits = list(COORD_RE.finditer(line))
    if len(hits) != 1:
        raise BuildError(
            f"远端第 {lineno} 行有 {len(hits)} 个句子占位符（应为 1 个）：{line[:60]}")
    m = hits[0]
    coord = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return line[:m.start()], coord, line[m.end():]


# 标题（#～######）与无序列表（- * +）的行首标记，一律不进句子内容。
# 有序列表刻意不收：本文里 `一、`、`1.` 开头的多半是译文正文，剥掉会毁内容。
MARKER_RE = re.compile(r"^(?:#{1,6}\s*|[-*+]\s+)")


def strip_markers(local_line, prefix, suffix, lineno):
    """剥掉 markdown 标记，只留标题/条目本身的文字。

    两道：
    1. 模板给了什么字面标记（`## {{…}}`、`- {{…}}`），本地行必须也有，剥掉；
       对不上就报错跳过整篇——这能抓出译文与占位符错位（[80]b 就是这么发现的）。
    2. 模板是裸 `{{…}}` 但本地行自带 `###` / `- ` 时，同样剥掉：标记是 markdown
       的排版语法，不属于句子内容。
    """
    text = local_line.strip()
    head, tail = prefix.strip(), suffix.strip()
    if head:
        if not text.startswith(head):
            raise BuildError(
                f"第 {lineno} 行标记对不上：远端模板前缀是 {head!r}，"
                f"本地行是 {local_line[:40]!r}")
        text = text[len(head):].lstrip()
    if tail:
        if not text.endswith(tail):
            raise BuildError(
                f"第 {lineno} 行标记对不上：远端模板后缀是 {tail!r}，"
                f"本地行是 {local_line[:40]!r}")
        text = text[:-len(tail)].rstrip()
    text = MARKER_RE.sub("", text, count=1).strip()
    if not text:
        raise BuildError(f"第 {lineno} 行剥掉标记后内容为空：{local_line[:40]!r}")
    return text


def build_sentences(content, local_text, channel_uid):
    """把一篇文章的远端正文与本地译文配成句子列表。

    先过结构闸门（空行分段的每段行数必须完全一致），再逐行配对——
    结构一致时非空行数必然相等，不会错位。
    """
    remote_blocks = blank_separated_block_lengths(content)
    local_blocks = blank_separated_block_lengths(local_text)
    if remote_blocks != local_blocks:
        detail = f"远端 {len(remote_blocks)} 段 / 本地 {len(local_blocks)} 段"
        for i in range(min(len(remote_blocks), len(local_blocks))):
            if remote_blocks[i] != local_blocks[i]:
                detail += (f"；第一处不同在第 {i + 1} 段："
                           f"远端 {remote_blocks[i]} 行、本地 {local_blocks[i]} 行")
                break
        raise BuildError("分段行数不一致（" + detail + "）")

    remote_lines = non_blank_lines(content)
    local_lines = non_blank_lines(local_text)
    sentences = []
    for i, (rl, ll) in enumerate(zip(remote_lines, local_lines), 1):
        prefix, coord, suffix = split_template_line(rl, i)
        book, para, ws, we = coord
        sentences.append({
            "book_id": book, "paragraph": para,
            "word_start": ws, "word_end": we,
            "channel_uid": channel_uid,
            "content": strip_markers(ll.rstrip("\r"), prefix, suffix, i),
            "content_type": "markdown",
        })
    return sentences


# ---------------------------------------------------------------------------
# 进度表：一个 md 文件一条记录，每篇写完就落盘
# ---------------------------------------------------------------------------

PROGRESS_HEADER = ("article_id\tkey\tstatus\tcount\tchannel_uid\t"
                   "updated_at\tlocal_path\tdetail\n")


def progress_path(vol):
    return os.path.join(vol, "chinese", "_wikipali_progress.tsv")


def ignore_path(vol):
    return os.path.join(vol, "chinese", "_wikipali_ignore.tsv")


def load_ignore(vol):
    """人工判定作废的远端文章：`article_id \t key \t reason`。

    用于把「同一 key 挂多篇文章」里错误的那些永久排除掉——排除后剩下唯一一篇，
    dup_remote 自然解除，正确的那篇就能正常上传。
    """
    path = ignore_path(vol)
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            cols = ln.rstrip("\n").split("\t")
            if len(cols) >= 2 and cols[0] and cols[0] != "article_id":
                out[cols[0]] = cols[2] if len(cols) > 2 else ""
    return out


def load_progress(vol):
    path = progress_path(vol)
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as f:
        next(f, None)
        for ln in f:
            cols = ln.rstrip("\n").split("\t")
            if len(cols) >= 6:
                done[(cols[0], cols[4])] = cols
    return done


def save_progress(vol, records):
    path = progress_path(vol)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(PROGRESS_HEADER)
        for cols in records.values():
            f.write("\t".join(cols) + "\n")
    os.replace(tmp, path)


def record(records, vol, article_id, key, status, count, channel_uid,
           local_path, detail=""):
    records[(article_id, channel_uid)] = [
        article_id, key, status, str(count), channel_uid,
        iso_now(), local_path, detail.replace("\t", " ").replace("\n", " "),
    ]
    save_progress(vol, records)


# ---------------------------------------------------------------------------
# upload：一个 md 文件一个事务
# ---------------------------------------------------------------------------

def write_sentences(sess, sentences, access_token, batch_size):
    """分批写入，返回 (已确认写入的坐标集合, 服务端返回的第一行)。

    HTTP 200 不等于全部写入：store() 对逐句鉴权失败是静默 continue 的，
    所以必须把返回的 rows 与提交的句子逐条比对。
    """
    written, sample = set(), None
    model_token = sess.model["token"]
    for start in range(0, len(sentences), batch_size):
        batch = sentences[start:start + batch_size]
        body = {"sentences": [dict(s, access_token=access_token) for s in batch]}
        try:
            data = api_call("POST", "sentence", token=model_token, body=body,
                            timeout=WRITE_TIMEOUT)
        except ApiError as e:
            if e.status != 401:
                raise
            model_token = refresh_model_token(sess)
            data = api_call("POST", "sentence", token=model_token, body=body,
                            timeout=WRITE_TIMEOUT)
        for row in (data or {}).get("rows") or []:
            # 返回用的是另一套字段名：book（不是 book_id）、channel 的 uid 放在
            # `id` 键里（references/api-write.md 写的是 channel.uid，与实际不符——
            # 认成 uid 会让比对全部落空，把「已写入」误报成「一条没写」）
            ch = row.get("channel") or {}
            written.add((int(row.get("book", -1)), int(row.get("paragraph", -1)),
                         int(row.get("word_start", -1)), int(row.get("word_end", -1)),
                         ch.get("uid") or ch.get("id")))
            sample = sample or row
    return written, sample


def sent_key(s):
    return (s["book_id"], s["paragraph"], s["word_start"], s["word_end"],
            s["channel_uid"])


def do_upload(args, vol):
    sess = make_session(args)
    sess.user_token           # 缺凭据时不该让用户看半张回显
    model = sess.model
    channel_uid, channel_name = resolve_channel(sess, args.channel)

    # 每次重跑都重新拉 article-map：后台改完标题笔误，下次跑自动生效
    print("拉取 article-map 并按 key 匹配本地译文……")
    rows = do_match(vol, args.collection_id)

    todo = [r for r in rows if r[6] == "matched"]
    records = load_progress(vol)

    # 人工判定作废的远端文章，永久排除
    ignored = load_ignore(vol)
    if ignored:
        hit = [r for r in todo if r[0] in ignored]
        for r in hit:
            record(records, vol, r[0], r[3], "ignored", 0, channel_uid, r[5],
                   ignored[r[0]])
        if hit:
            print(f"忽略名单（{ignore_path(vol)}）排除了 {len(hit)} 篇：" +
                  "、".join(f"{r[3]}/{r[0][:8]}…" for r in hit))
        todo = [r for r in todo if r[0] not in ignored]

    # 同一个 key 挂着多篇远端文章时，它们会全部指向同一个本地文件——最多只有一篇
    # 是对的，其余会把这份译文写到错误的坐标去。默认全部跳过交人工处理。
    dup_keys = {k for k in {r[3] for r in todo}
                if sum(1 for r in todo if r[3] == k) > 1}
    if dup_keys and not args.allow_dup_remote:
        for r in [r for r in todo if r[3] in dup_keys]:
            record(records, vol, r[0], r[3], "dup_remote", 0,
                   channel_uid, r[5], f"远端有多篇文章共用 key {r[3]}：{r[4]}")
        note(f"⚠ {len(dup_keys)} 个 key 在远端对应多篇文章，"
             f"已跳过（{'、'.join(sorted(dup_keys))}）。"
             "请在后台改正标题，或确认无误后加 --allow-dup-remote。")
        todo = [r for r in todo if r[3] not in dup_keys]

    if args.keys:
        want = {k.strip() for k in args.keys.split(",") if k.strip()}
        todo = [r for r in todo if r[3] in want]
    if not args.retry_done:
        skipped_done = [r for r in todo
                        if records.get((r[0], channel_uid), ["", "", ""])[2] == "done"]
        todo = [r for r in todo
                if records.get((r[0], channel_uid), ["", "", ""])[2] != "done"]
        if skipped_done:
            print(f"进度表里已 done 的 {len(skipped_done)} 篇将跳过"
                  "（要重传加 --retry-done）")
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("没有待上传的文章。")
        return True

    total_lines = 0
    for r in todo:
        with open(r[5], encoding="utf-8") as f:
            total_lines += sum(1 for ln in f if ln.strip())

    print("=" * 72)
    print(f"API      : {sess.api_note()}")
    print(f"channel  : {channel_name or '(未知)'}  {channel_uid}")
    print(f"模型身份 : {model.get('name')}  uid={model.get('uid')}")
    print(f"待上传   : {len(todo)} 篇，约 {total_lines} 句（每篇内部每 "
          f"{args.batch} 条一批）")
    print(f"进度表   : {progress_path(vol)}")
    print("-" * 72)
    print("⚠ 相同坐标（book/paragraph/word_start/word_end/channel）的已有句子将被覆盖。")
    print("=" * 72)

    if args.dry_run:
        print("--dry-run：只做匹配与校验，不签 token、不发任何写请求。")
        return dry_run_check(todo, channel_uid, args, sess.user_token)

    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("未加 --yes 且当前不是交互式终端，已中止，未写入任何内容。")
        if input("确认写入？ [y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消，未写入任何内容。")
            return False

    # vol_5 跨十几个 book id，所以签 book=0（不限 book）的一张 token
    access = grant_access_token(sess, channel_uid, channel_name, book=0)
    print(f"access token 已就绪：{mask(access['token'])} "
          f"到期 {fmt_ts(access.get('exp'))}\n")

    n_done = n_partial = n_skip = 0
    for i, r in enumerate(todo, 1):
        article_id, key, local_path = r[0], r[3], r[5]
        head = f"[{i}/{len(todo)}] {key}"
        try:
            content = fetch_article_content(article_id, token=sess.user_token)
        except ApiError as e:
            n_skip += 1
            print(f"⊘ {head}\t抓取正文失败: {e}")
            record(records, vol, article_id, key, "fetch_error", 0,
                   channel_uid, local_path, str(e))
            continue
        try:
            with open(local_path, encoding="utf-8") as f:
                local_text = f.read()
        except OSError as e:
            n_skip += 1
            print(f"⊘ {head}\t读不了本地译文: {e}")
            record(records, vol, article_id, key, "local_missing", 0,
                   channel_uid, local_path, str(e))
            continue

        try:
            sentences = build_sentences(content, local_text, channel_uid)
        except BuildError as e:
            n_skip += 1
            print(f"⊘ {head}\t{e}")
            record(records, vol, article_id, key, "build_error", 0,
                   channel_uid, local_path, str(e))
            continue

        try:
            written, sample = write_sentences(sess, sentences, access["token"],
                                              args.batch)
        except ApiError as e:
            n_skip += 1
            print(f"✗ {head}\t写入失败: {e}")
            record(records, vol, article_id, key, "write_error", 0,
                   channel_uid, local_path, str(e))
            continue

        missing = [s for s in sentences if sent_key(s) not in written]
        if missing:
            n_partial += 1
            detail = "；".join(
                f"{s['book_id']}-{s['paragraph']}-{s['word_start']}-{s['word_end']}"
                for s in missing[:10])
            print(f"✗ {head}\t提交 {len(sentences)} 条，确认 "
                  f"{len(sentences) - len(missing)} 条，漏 {len(missing)} 条")
            print(f"    漏写坐标: {detail}"
                  f"{'……' if len(missing) > 10 else ''}")
            record(records, vol, article_id, key, "partial",
                   len(sentences) - len(missing), channel_uid, local_path, detail)
        else:
            n_done += 1
            editor = ((sample or {}).get("editor") or {}).get("nickName") or ""
            print(f"✓ {head}\t{len(sentences)} 句已写入"
                  f"{('  editor=' + editor) if i == 1 and editor else ''}")
            record(records, vol, article_id, key, "done", len(sentences),
                   channel_uid, local_path)

        if args.sleep and i < len(todo):
            time.sleep(args.sleep)

    print("-" * 72)
    print(f"共处理 {len(todo)} 篇：done={n_done}  partial={n_partial}  "
          f"skipped={n_skip}")
    write_todo(vol, rows, records, channel_uid)
    report_skipped(records, channel_uid)
    return n_partial == 0 and n_skip == 0


def dry_run_check(todo, channel_uid, args, user_token=None):
    """只跑匹配 + 校验 + 组句，不写库；把会被跳过的篇目提前暴露出来。"""
    n_ok = n_bad = 0
    n_sent = 0
    for i, r in enumerate(todo, 1):
        article_id, key, local_path = r[0], r[3], r[5]
        try:
            content = fetch_article_content(article_id, token=user_token)
            with open(local_path, encoding="utf-8") as f:
                local_text = f.read()
            sentences = build_sentences(content, local_text, channel_uid)
        except (ApiError, OSError, BuildError) as e:
            n_bad += 1
            print(f"⊘ [{i}/{len(todo)}] {key}\t{e}")
            continue
        n_ok += 1
        n_sent += len(sentences)
        print(f"✓ [{i}/{len(todo)}] {key}\t{len(sentences)} 句")
        if args.show_pairs:
            for s in sentences[:args.show_pairs]:
                txt = s["content"][:50] + ("…" if len(s["content"]) > 50 else "")
                print(f"      {s['book_id']}-{s['paragraph']}-{s['word_start']}"
                      f"-{s['word_end']}  {txt}")
        if args.sleep and i < len(todo):
            time.sleep(args.sleep)
    print("-" * 72)
    print(f"dry-run：{n_ok} 篇可上传（共 {n_sent} 句），{n_bad} 篇会被跳过。")
    return n_bad == 0


TODO_SECTIONS = [
    ("build_error", "分段行数或 markdown 标记对不上",
     "本地译文与 wikipali 占位符结构不一致。按 detail 里指出的段号去看：\n"
     "多半是译文该分段的地方没分、或多分了一段。改完原样重跑即可。"),
    ("partial", "部分句子没写进去",
     "服务端对逐句鉴权失败是静默跳过的。detail 里是漏写的坐标。"),
    ("write_error", "写入时报错", "看 detail 里的服务端消息。"),
    ("fetch_error", "拉不到 wikipali 正文", "多半是权限或网络，重跑一般能好。"),
    ("local_missing", "本地译文文件读不到", "检查文件是否被移动或改名。"),
    ("dup_remote", "远端多篇文章共用同一个 key",
     "它们会全部指向同一个本地文件，最多只有一篇是对的。请在 wikipali 后台\n"
     "改正标题；确认作废的那篇写进 _wikipali_ignore.tsv 即可自动排除。"),
    ("ignored", "已按人工判定作废（无需处理，仅备查）", ""),
]


def write_todo(vol, rows, records, channel_uid):
    """把所有需要人工处理的条目汇总成一份 markdown log。"""
    path = os.path.join(vol, "chinese", "_wikipali_todo.md")
    mine = [c for c in records.values() if c[4] == channel_uid]
    by_status = {}
    for c in mine:
        by_status.setdefault(c[2], []).append(c)

    lines = [f"# {vol} 上传待处理清单", "",
             f"生成时间：{iso_now()}　channel：{channel_uid}", "",
             "处理完**原样重跑同一条 upload 命令**即可——进度表里 `done` 的会自动跳过，"
             "只补这里列出的。", ""]

    total = 0
    for status, title, howto in TODO_SECTIONS:
        items = by_status.get(status) or []
        if not items:
            continue
        if status != "ignored":
            total += len(items)
        lines.append(f"## {title}（{len(items)} 篇）")
        lines.append("")
        if howto:
            lines.append(howto)
            lines.append("")
        for c in sorted(items, key=lambda x: x[1]):
            lines.append(f"- **{c[1]}**　`{c[6]}`")
            if c[7]:
                lines.append(f"  - {c[7]}")
            lines.append(f"  - article_id `{c[0]}`")
        lines.append("")

    # match 阶段就没进入上传循环的那些，同样要人工处理
    match_groups = [
        ("parse_fail", "wikipali 标题里解析不出 key",
         "标题笔误（如缺开头的 `[`）或用了 `-1`/`-2` 而非 `a`/`b` 后缀。"
         "需要在后台改标题。"),
        ("dup_local", "本地有多个文件共用同一个 key",
         "需要按 `[nnn]a`/`[nnn]b` 规范重命名消歧。"),
        ("unmatched", "wikipali 上有、本地找不到对应文件",
         "多为容器节点（本地只是一个目录），属预期，一般无需处理。"),
    ]
    for status, title, howto in match_groups:
        items = [r for r in rows if r[6] == status]
        if not items:
            continue
        if status != "unmatched":
            total += len(items)
        lines.append(f"## {title}（{len(items)} 条）")
        lines.append("")
        lines.append(howto)
        lines.append("")
        for r in sorted(items, key=lambda x: x[3]):
            lines.append(f"- **{r[3] or '(无 key)'}**　{r[4]}")
            if r[5]:
                lines.append(f"  - `{r[5]}`")
            lines.append(f"  - article_id `{r[0]}`")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"待处理清单（{total} 项需要你处理）-> {path}")
    return path


def report_skipped(records, channel_uid):
    bad = [c for c in records.values()
           if c[4] == channel_uid and c[2] not in ("done",)]
    if not bad:
        return
    print(f"\n需要人工处理的 {len(bad)} 篇（处理完原样重跑即可，done 的会自动跳过）：")
    for c in bad:
        print(f"  {c[2]:<14} {c[1]:<10} {c[6]}")
        if c[7]:
            print(f"                 {c[7][:100]}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_match = sub.add_parser("match", help="按 key 匹配 article-map 与本地译文")
    p_match.add_argument("vol", help="如 vol_5")
    p_match.add_argument("collection_id", help="wikipali anthology 的 uuid")
    p_match.add_argument("--api")

    p_verify = sub.add_parser("verify", help="校验匹配上的文章是否逐行(含空行)对应")
    p_verify.add_argument("vol")
    p_verify.add_argument("collection_id")
    p_verify.add_argument("--api")
    p_verify.add_argument("--limit", type=int, default=0, help="只校验前 N 篇(0=全部)")
    p_verify.add_argument("--sleep", type=float, default=0.2, help="每次请求间隔秒数")
    p_verify.add_argument("--show-pairs", type=int, default=0,
                           help="对齐成功时，额外打印前 N 行 占位符<->译文 的逐行样例")

    p_login = sub.add_parser("login", help="登录 wikipali 并缓存用户 token")
    p_login.add_argument("--api", help="本次使用的站点（next / www / 完整 url）")
    p_login.add_argument("--username", help="用户名或邮箱；省略则交互输入")
    p_login.add_argument("--password-stdin", action="store_true",
                         help="从 stdin 读密码（供自动化用；别让密码经过 argv 或对话）")

    p_who = sub.add_parser("whoami", help="查看站点与凭据状态")
    p_who.add_argument("--api")

    p_model = sub.add_parser("model", help="注册/复用 AI 模型身份并取模型 token")
    p_model.add_argument("--name", default="claude-opus-5",
                         help="模型标识，会成为句子的作者署名")
    p_model.add_argument("--privacy", type=int, default=0)
    p_model.add_argument("--api")

    p_ch = sub.add_parser("channels", help="列出可编辑的 channel")
    p_ch.add_argument("--search", help="按名字过滤")
    p_ch.add_argument("--api")

    p_up = sub.add_parser("upload", help="逐篇校验并把译文按句子坐标写入 channel")
    p_up.add_argument("vol")
    p_up.add_argument("collection_id", help="wikipali anthology 的 uuid")
    p_up.add_argument("--channel", required=True, help="目标 channel 的 uid 或名字片段")
    p_up.add_argument("--dry-run", action="store_true",
                      help="只做匹配+校验+组句，不签 token、不写任何内容")
    p_up.add_argument("--yes", "-y", action="store_true", help="跳过写入前的确认")
    p_up.add_argument("--limit", type=int, default=0, help="只处理前 N 篇(0=全部)")
    p_up.add_argument("--keys", help='只处理指定 key，逗号分隔，如 "[1]c,[6]"')
    p_up.add_argument("--retry-done", action="store_true",
                      help="连进度表里已 done 的也重传")
    p_up.add_argument("--allow-dup-remote", action="store_true",
                      help="允许上传「远端多篇文章共用同一 key」的条目（默认跳过）")
    p_up.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                      help="单篇内部每批提交的句子数")
    p_up.add_argument("--sleep", type=float, default=0.2, help="每篇之间的间隔秒数")
    p_up.add_argument("--show-pairs", type=int, default=0,
                      help="dry-run 时每篇打印前 N 句 坐标<->译文 样例")
    p_up.add_argument("--api")

    args = ap.parse_args()

    if args.cmd in ("login", "whoami", "model", "channels"):
        handler = {"login": do_login, "whoami": do_whoami,
                   "model": do_model, "channels": do_channels}[args.cmd]
        sys.exit(handler(args) or 0)

    vol = args.vol.rstrip("/")
    if args.cmd != "upload":     # upload 走 make_session，那里已经解析过
        resolve_api_url(args.api)
    if args.cmd == "match":
        do_match(vol, args.collection_id)
    elif args.cmd == "verify":
        ok = do_verify(vol, args.collection_id, args.limit, args.sleep, args.show_pairs)
        sys.exit(0 if ok else 1)
    elif args.cmd == "upload":
        sys.exit(0 if do_upload(args, vol) else 1)


if __name__ == "__main__":
    main()
