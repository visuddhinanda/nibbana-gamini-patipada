# 译文上传 wikipali 设计文档

目标：把 `<vol>/chinese/` 下已翻译好的 markdown 文件，对应到 wikipali 站点
(`next.wikipali.org`) 上事先建好的文章树（anthology），最终把译文内容写入
对应文章。**本文档记录整体设计，目前脚本 `tools/wikipali_sync.py` 只实现了
"匹配" 与 "校验" 两步，尚未做真正的上传/写入。**

## 背景 API

- 文章树（anthology 下所有文章标题、层级、article_id）：
  `GET https://next.wikipali.org/api/v2/article-map?view=anthology&id=<collection_uuid>`
  返回 `data.rows`，每行形如：
  ```json
  {"article_id": "9826ed28-...", "level": 2, "children": 0,
   "title": "NGP-Vol5-[1]c ကလာပသမ္မသန ခေါ် နယဝိပဿနာ", ...}
  ```
  - `level` 是文章树深度（1 = 卷内一级章节容器，2/3 = 更深层级），不是"是否有正文"的标志。
  - `children` > 0 表示该行是"容器"节点（对应本地一个子目录），仍可能有自己的正文页
    （例：`[1]b` 既是容器，也有一篇很短的正文 `[1]b 道非道智见清净释 - 思惟.md`）。
  - `children` == 0 是叶子文章，一定有正文。

- 单篇文章正文：
  `GET https://next.wikipali.org/api/v2/article/<article_id>`
  返回 `data.content`（`content_type` 为 `markdown` 的**字符串**，不是数组）。
  内容形如：
  ```
  {{3013-1-11-11}}

  {{3013-2-11-11}}

  {{3013-3-11-11}}
  {{3013-3-21-21}}
  {{3013-3-31-31}}
  ```
  每个 `{{...}}` 是一个"句子占位符"（未来上传译文时，大概率是按占位符逐句回填）。
  空行分隔段落；同一段落内可能有多行占位符（对应译文里同一段落被人工拆成多个短句）。

## 匹配算法（第一步，`match` 子命令）

1. 从 `_manifest.tsv`/其它工具沿用的通用 key 正则（与 `build_manifest.py`
   / `build_epub.py` / `translate_vol.sh` 一致，避免行为分裂）：
   ```
   KEY_RE = r"^\s*(\[[^\]]+\][A-Za-z]?)"
   ```
   对本地文件名（去掉目录、`.md` 后缀）取开头的 `[页码]字母` 作为 key，
   例如 `[1]c 聚思惟即理观.md` → key = `[1]c`。

2. 递归扫描 `<vol>/chinese/` 下所有 `*.md`（跳过下划线开头的脚手架文件如
   `_manifest.tsv`、`_TRANSLATE_INSTRUCTIONS.md` 等），建立 `key -> [本地路径,...]`
   索引。正常情况下每个 key 只对应一个文件；如果对应多个（见"已知坑"），
   该 key 标记为 `本地重复`，交给人工/`_produced.tsv` 消歧（目前 vol_5 没有
   `_produced.tsv`，视为该卷暂无冲突）。

3. wikipali 标题里 key 前常带前缀 `NGP-Vol<N>-`（level 1 容器节点没有此前缀，
   直接以 `[key]` 开头）。用
   ```
   TITLE_KEY_RE = r"^(?:NGP-?Vol-?\d+-)?\s*(\[[^\]]+\][A-Za-z]?)"
   ```
   先剥前缀再取 key。

4. 用 key 去本地索引里查，查到即为匹配；查不到 / 标题解析不出 key，都记为
   未匹配，原样列出供人工核对，不做猜测性模糊匹配。

5. 结果写入 `<vol>/chinese/_wikipali_map.tsv`：
   `article_id  level  children  key  title  local_path  status`
   （`status` ∈ `matched` / `unmatched` / `dup_local` / `parse_fail`）。

### 已知边界情况（vol_5 实测，484 行里 472 匹配）

- **标题本身有笔误**：个别标题缺开头 `[`（如 `133] ဒုက္ခအခြင်းအရာ`），
  `TITLE_KEY_RE` 匹配不到 key，归为 `parse_fail`，需要人工在 wikipali 后台改标题。
- **标题用位置后缀而非字母后缀**：个别标题是 `NGP-Vol-5[330]-1` /
  `NGP-Vol-5[330]-2` 这种"同一 key 后面挂 `-1`/`-2`"，而本地对应文件其实是
  `[330]a` / `[330]b`（字母后缀）。这种命名不一致目前也归为 `parse_fail`，
  不做"猜测 -1→a、-2→b"的自动映射，避免匹配错行。
- **容器节点本身不对应正文文件**：某些 `children>0` 的行（如 `[210]`、
  `[343]`、`[349]`、`[463]`）在本地只是一个目录（`[343]省察随观智章/`），
  真正的正文是目录下带字母后缀的第一篇（`[343]a 省察随观智章.md`）。这类行
  会落入 `unmatched`，这是预期行为，不是 bug——它们不需要单独的正文，未来
  上传时应跳过或仅用于建目录层级。
- **本地 key 重复**：vol_5 里 `[135]` 同时对应两个文件（`[135]所谓百年.md`
  与 `[135]二、以衰老坏灭而观的方法.md`），需人工消歧（可能是源文件拆分时
  译者忘记按 `[nnn]a`/`[nnn]b` 规范命名）。

## 校验算法（第二步，`verify` 子命令）

目的：确认"本地译文"与"wikipali 正文占位符"是逐行对应的——即本地译文里
空行分隔出的每一段，行数要和 wikipali 正文里对应段落的占位符行数完全一致。
逐字符对应无法验证（wikipali 侧是句子编号，不是中文原文），但**结构对齐**
（段落数、每段行数、空行位置）可以验证，这也是未来"按占位符顺序回填译文"
能不能对上的前提。

算法（对 `match` 阶段产出的每个 `matched` 条目）：

1. `GET /api/v2/article/<article_id>`，取 `data.content`（markdown 字符串），
   按 `\n` 切行，去掉结尾的空行残留。
2. 读本地译文文件，同样按 `\n` 切行。
3. 两边各自按"空行分段"，得到每段的**非空行数**列表，例如
   `[1, 1, 3, 2, 1, 1, 1, 2, ...]`。
4. 比较两个列表是否完全相等：
   - 相等 → `OK`（段数、每段行数都对上，等价于"行数、空行都能对应上"）。
   - 不等 → `MISMATCH`，报告出第一处不同的段号、两边的行数，便于人工去看
     具体是本地译文分段有问题，还是 wikipali 占位符结构有问题。

已用 `NGP-Vol5-[1]c ကလာပသမ္မသန ခေါ် နယဝိပဿနာ`
(article_id `9826ed28-ea01-476f-b103-6c18a2bcf747`) 实测：
两边都是 34 段，`[1,1,3,2,1,1,1,2,1,1,1,1,3,6,1,1,1,25,1,3,1,1,3,2,7,8,1,2,1,1,1,6,3,5]`，
完全一致。

## 上传（第三步，`upload` 子命令）

以**一个 md 文件为一个事务**：拉正文 → 校验 → 组句 → 写入 → 记进度。任何一篇
出问题都只影响那一篇，其余照常继续。

### 认证：三种 token，职责不能混

| Token | 从哪来 | 代表谁 | 用在哪 | 有效期 |
|---|---|---|---|---|
| userToken | `POST /v2/sign-in` | 人类操作者 | 查/建 ai-model、签 access token、列 channel | 365 天 |
| modelToken | `GET /v2/ai-model-token/{uid}` | AI 模型身份 | 写句子时的 `Authorization` 头 | 30 天，可撤销 |
| accessToken | `POST /v2/access-token` | 被委托的 channel 编辑权 | 写句子时**句子对象里的字段** | 7 天 |

用错会让 `editor_uid` 落成人类用户，署名与审计就废了。凭据存在
`~/.wikipali/credentials.json`（0600），与 `wikipali-plugins` 插件**共用同一份**，
两边可互相顶替。密码只经 `getpass` 或操作系统密码框读入内存，不落盘、不进日志、
不进命令行参数。

签 access token 时 `book` **必须是整数**——服务端用 `$jwt->book !== $book` 严格
比较，写成 `"0"` 会让 `"0" !== 0` 恒真而永远鉴权失败。vol_5 跨十几个 book id
（3013、3031、…、3559），所以签 `book: 0`（不限 book）的一张 token。
返回 `count: 0` 表示对该 channel 无编辑权（服务端静默跳过，HTTP 仍是 200），
等同 403，**立即中止，不进入写入循环**。

### 占位符 → 句子坐标

正文里每个非空行**恰好一个** `{{book-paragraph-word_start-word_end}}`，与
`POST /v2/sentence` 的四个坐标字段一一对应（实测 470 篇无例外）。

行内可能带 markdown 标记：`- {{…}}`、`### {{…}}`、`## {{…}}`。
**标记是 markdown 的排版语法，不属于句子内容，一律不上传**，分两道处理：

1. **模板给了标记**，本地行必须也有，剥掉；对不上就报错跳过整篇——这能抓出
   译文与占位符错位（`[80]b` 就是这么发现的）。
   远端 `### {{3559-1009-1-1}}` + 本地 `### 修习心…` → content 为 `修习心…`
   （不剥会渲染成 `### ### 修习心`）。
2. **模板是裸占位符、标记只在本地行**（几乎每篇的第一行都是这样），同样剥掉。
   远端 `{{3013-1-11-11}}` + 本地 `### 聚思惟…` → content 为 `聚思惟…`。

剥的只有**标题 `#`～`######` 与无序列表 `- * +`**。有序列表刻意不收：本文里
`一、能直接令其生起的因之缘…`、`1. 六门…` 这类开头是译文正文，剥掉会毁内容。
剥完为空 → 报错跳过整篇，绝不猜测。

### markdown 表格：整张算一句

**一张表格无论多少行，都是一个句子**，对应远端的一个占位符。段内连续的以 `|`
开头的行合并成一个单元，`content` 是整张表格（含换行）。

实证：远端 `[135] ၂။ ၀ယောဝုဍ္ဎတ္ထင်္ဂမ`（book 3439）第 4 段是单个
`{{3439-4-1-1}}`，本地对应的是 12 行的表格。合并前后对照：

```
合并前  本地 [1, 2, 2, 12, 2, 1, 4, 1, 1, 6, 2, 2, 6, 2]
合并后  本地 [1, 2, 2,  1, 2, 1, 4, 1, 1, 6, 2, 2, 6, 2]
远端    　　 [1, 2, 2,  1, 2, 1, 4, 1, 1, 6, 2, 2, 6, 2]
```

**校验分段行数时用的是同一套成句算法**（`split_blocks` → `group_units`），
否则会出现「校验通过但组句错位」或反之。合并只在段内进行，不跨空行，
免得把相邻两段各自结尾/开头的表格误并成一张。

### 写入

`POST /v2/sentence`，`Authorization: Bearer <modelToken>`，每个句子对象里带
`access_token`。单篇内部每 50 条一批（vol_5 有 35 篇超过 50 行，最长 `[300]`
有 178 行）。

按 `(book_id, paragraph, word_start, word_end, channel_uid)` 做 `firstOrNew`：
**存在即覆盖**。天然幂等（重跑安全），但也意味着会覆盖同 channel 同坐标的已有
句子，所以写入前必须回显 channel 并等确认。

⚠ **HTTP 200 不等于全部写入**：服务端对逐句鉴权失败是静默 `continue` 掉的。
必须把返回的 rows 与提交的句子逐条比对——注意返回用的是另一套字段名
（`book` 而不是 `book_id`、`channel.uid`），差集要如实报告。

### 进度与重跑

`<vol>/chinese/_wikipali_progress.tsv`，每篇写完立即落盘（Ctrl-C 或断网都不丢）：

```
article_id  key  status  count  channel_uid  updated_at  local_path  detail
```

`status` ∈ `done` / `partial` / `build_error` / `fetch_error` / `write_error` /
`local_missing` / `dup_remote`。重跑同一条命令时 `done` 的自动跳过，
只重试失败的；`--retry-done` 可强制重传。

进度按 `(article_id, channel_uid)` 索引，所以换 channel 重传不会被旧记录挡住。

## 已知边界情况：全部只记录、只跳过，不猜测

除了前面 `match` 阶段的 `parse_fail`(7) / `unmatched`(5) / `dup_local`(2)，
上传阶段还会跳过：

- **分段行数不一致**：本地译文与远端占位符结构对不上，报出第一处不同的段号；
- **标记对不上**：模板前缀无法从本地行剥掉；
- **`dup_remote`**：同一个 key 在远端挂着多篇文章（vol_5 里 `[354]a` 有两篇，
  标题不同却共用 key），它们会全部指向同一个本地文件，最多只有一篇是对的，
  其余会把译文写到错误坐标。默认全部跳过，确认无误后可加 `--allow-dup-remote`。

处理完（改后台标题、拆分本地文件、调整分段）**原样重跑同一条命令**即可。

## CLI 用法

```bash
# 首次准备（凭据已在则跳过）
python3 tools/wikipali_sync.py login            # 密码只经 getpass/系统密码框
python3 tools/wikipali_sync.py model --name claude-opus-5
python3 tools/wikipali_sync.py whoami           # token 一律打码
python3 tools/wikipali_sync.py channels --search claude

# 第一步：匹配 + 落地 <vol>/chinese/_wikipali_map.tsv
python3 tools/wikipali_sync.py match vol_5 22ae16b4-68b3-4403-b155-ede40c509c7e

# 第二步：校验行数/空行是否逐行对应（默认对 match 产出里全部 matched 条目跑一遍）
python3 tools/wikipali_sync.py verify vol_5 22ae16b4-68b3-4403-b155-ede40c509c7e

# 校验时可加 --limit 先抽查几篇，或 --sleep 调整请求间隔（默认 0.2s，避免打太快）
python3 tools/wikipali_sync.py verify vol_5 22ae16b4-68b3-4403-b155-ede40c509c7e --limit 20

# 第三步：先 dry-run 看哪些会被跳过（不签 token、不发任何写请求）
python3 tools/wikipali_sync.py upload vol_5 22ae16b4-68b3-4403-b155-ede40c509c7e \
        --channel 73c03e1a-f333-11f0-808a-438f0af4b9e9 --dry-run

# 抽一篇真写，核对网站上的署名与位置
python3 tools/wikipali_sync.py upload vol_5 22ae16b4-68b3-4403-b155-ede40c509c7e \
        --channel 73c03e1a-f333-11f0-808a-438f0af4b9e9 --keys "[1]c"

# 确认无误后全卷；中断后原样重跑，done 的自动跳过
python3 tools/wikipali_sync.py upload vol_5 22ae16b4-68b3-4403-b155-ede40c509c7e \
        --channel 73c03e1a-f333-11f0-808a-438f0af4b9e9
```

`upload` 每次都会重新拉 article-map，所以在 wikipali 后台改完标题笔误，
下次跑自动生效，不必单独重跑 `match`。

不引入第三方库，只用标准库 `urllib.request` 发请求。
