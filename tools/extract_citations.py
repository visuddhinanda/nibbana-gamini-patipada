#!/usr/bin/env python3
"""提取缅文原书 (vol_1..vol_5/markdown) 中括号内的引文缩写，去重后输出总表。

书里的引文写在圆括号里，例如::

    (ဝိသုဒ္ဓိ-၂-၉၁။)        → ဝိသုဒ္ဓိ-၂     清净道论 第2册 第91页
    (သံ-၂-၂၃-၂၄။)          → သံ-၂          相应部 第2册 第23-24页
    (အဘိ၊ဋ္ဌ၊၂၊၃၈၈။)        → အဘိ-ဋ္ဌ-၂     论藏义注 第2册 第388页
    (ပဋိသံ၊၅၂။)             → ပဋိသံ         无碍解道 第52页 (该书不分册)

页码一律丢弃，只保留「缩写 + 册号」。

因为原文是手工录入的，格式并不统一，脚本对以下情况做了归一化:

* 分隔符可能是 ``၊`` (缅文逗号)、``-``、空格、``.``、``,``，或几种混用；
* 分隔符两边可能有多余空格，也可能完全没有分隔符两边的空格；
* 一个括号里可能塞了好几条引文 (以 ``။`` 分隔)；
* 引文前面可能带一段说明性缅文 (如 ``အကျယ်ကို-အဘိ-ဋ္ဌ-၁-၃၂၂။`` "详见…")，
  这段前缀会被剥掉并单独记录；
* 括号里也有大量「数量」表达 (如 ``(စေတသိက် ၅၂ မျိုး)`` "心所52种")，
  这些不是引文，会被排除。

判定引文用两道关卡:

1. 结构: ``缅文词(+分隔符)…数字(+分隔符+数字)…``，且数字后面不能紧跟缅文字母
   (紧跟字母的基本都是 "N个/N种/N遍" 之类的量词短语)。
2. 词表: 词元序列里必须能匹配上 KNOWN_SOURCES 里的某个已知典籍缩写；
   匹配不上的一律进 review 文件，不会被静默丢弃。

册号 vs 页码的判定是按缩写统计出来的: 只看「至少有两个数字」的引文，
如果这些引文的首个数字绝大多数 ≤ MAX_VOLUME_NO，就认为该书分册、首数字是册号；
否则 (如 ပဋိသံ、ဣတိဝုတ္တက-ဋ္ဌ) 认为不分册，首数字就是页码。
只有一个数字的引文一律当作「无册号」。

输出 (默认写到 glossary/):

  citations-master.tsv   总表: 去重后的「缩写-册号」，含各卷出现次数与原始写法
  citations-sources.tsv  按典籍汇总: 每本书出现过哪些册号
  citations-raw.tsv      每一条引文的原始记录 (便于核对)
  citations-review.tsv   没能识别的候选串 (人工过目，判断是漏网引文还是正文)

Usage:
  python3 tools/extract_citations.py
  python3 tools/extract_citations.py --outdir /tmp/out vol_4
  python3 tools/extract_citations.py --stats
"""
import argparse
import collections
import os
import re
import sys

# ---------------------------------------------------------------- 字符类

MY_DIGITS = "၀၁၂၃၄၅၆၇၈၉"
_MY2AR = str.maketrans(MY_DIGITS, "0123456789")

# 缅文字母块 U+1000..U+103F (含叠写符 ္ 与元音符号)，不含 U+1040.. 的数字和标点
LETTER_RUN = r"[က-ဿ]+"
DIGIT_RUN = r"[၀-၉]+"
# 词元分隔符: 缅文逗号 ၊、连字符、空格、半/全角逗号句点
SEP = r"[\s၊\-–—,，\.]+"

PAREN_RE = re.compile(r"[\(（][^()（）]*[\)）]")
BRACKET_RE = re.compile(r"[\[［][^\[\]［］]*[\]］]")
CITE_RE = re.compile(rf"((?:{LETTER_RUN}{SEP})+{DIGIT_RUN}(?:{SEP}{DIGIT_RUN})*)")
SPLIT_RE = re.compile(SEP)
IS_DIGIT_TOKEN = re.compile(rf"^{DIGIT_RUN}$")
# 数字后面允许出现的东西: 句点 ။、逗号 ၊、括号收尾、"- 说明"、格助词 ၌
TAIL_OK_RE = re.compile(r"^\s*(?:။|၊|$|[-–—]\s|၌|၏|[\)）\]］])")
# 括号外的裸引文容易和正文数字混淆，要求必须以 ။ 收尾 (或后接格助词 ၌)
TAIL_STRICT_RE = re.compile(r"^\s*(?:။|[-–—]?\s*၌)")

# 册号上限: 缅文版最多的 ခု (小部) 也就十来册，超过这个数的首数字肯定是页码
MAX_VOLUME_NO = 20
# 判定「该书分册」所需的比例
VOLUME_RATIO = 0.7

# ------------------------------------------------------- 已知典籍缩写词表
# key = 用 "-" 连接的词元序列(已归一化)，value = (罗马转写/巴利名, 中文名)
# 只列出「缩写本身」；册号、页码不在这里。

KNOWN_SOURCES = {
    # --- 巴利三藏 mūla ---
    "ဒီ": ("Dīgha-nikāya", "长部"),
    "မ": ("Majjhima-nikāya", "中部"),
    "သံ": ("Saṃyutta-nikāya", "相应部"),
    "အံ": ("Aṅguttara-nikāya", "增支部"),
    "ခု": ("Khuddaka-nikāya", "小部"),
    "ဝိ": ("Vinaya-piṭaka", "律藏"),
    "အဘိ": ("Abhidhamma-piṭaka", "论藏"),
    "ပဋိသံ": ("Paṭisambhidāmagga", "无碍解道"),
    "ပဋိသမ္ဘိဒါမဂ်": ("Paṭisambhidāmagga", "无碍解道"),
    "ပဋ္ဌာန": ("Paṭṭhāna", "发趣论"),
    "ဝိဘင်္ဂ": ("Vibhaṅga", "分别论"),
    "ဓမ္မပဒ": ("Dhammapada", "法句经"),
    "ဥဒါန": ("Udāna", "自说经"),
    "ဥဒါန်းပါဠိတော်": ("Udāna-pāḷi", "自说经巴利圣典"),
    "ဣတိဝုတ္တက": ("Itivuttaka", "如是语"),
    "သုတ္တနိပါတ": ("Suttanipāta", "经集"),
    "သုတ္တနိ": ("Suttanipāta", "经集"),
    "မဟာနိ": ("Mahāniddesa", "大义释"),
    "မဟာနိဒ္ဒေသ": ("Mahāniddesa", "大义释"),
    "စူဠနိ": ("Cūḷaniddesa", "小义释"),
    "စူဠနိဒ္ဒေသ": ("Cūḷaniddesa", "小义释"),
    "ဇာတက": ("Jātaka", "本生"),
    "ဇာ": ("Jātaka", "本生"),
    "ဗုဒ္ဓဝံသ": ("Buddhavaṃsa", "佛种姓经"),
    "အပဒါန်": ("Apadāna", "譬喻经"),
    "အပဒါနပါဠိ": ("Apadāna-pāḷi", "譬喻经巴利圣典"),
    "ထေရဂါထာ": ("Theragāthā", "长老偈"),
    "ထေရီဂါထာ": ("Therīgāthā", "长老尼偈"),
    "ပေတဝတ္ထု": ("Petavatthu", "饿鬼事"),
    "ဝိမာနဝတ္ထု": ("Vimānavatthu", "天宫事"),
    "နေတ္တိ": ("Nettippakaraṇa", "导论"),
    "မိလိန္ဒ": ("Milindapañha", "弥兰王问经"),
    "မိလိန္ဒပဉှ": ("Milindapañha", "弥兰王问经"),
    "မိလိန္ဒပဥှာ": ("Milindapañha", "弥兰王问经"),
    "မူလပဏ္ဏာသ": ("Mūlapaṇṇāsa", "根本五十经篇"),
    "မူလပဏ္ဏာသပါဠိတော်": ("Mūlapaṇṇāsa-pāḷi", "根本五十经篇巴利圣典"),
    "မူလပဏ္ဏာသပါဠိတော်မြန်မာပြန်": ("Mūlapaṇṇāsa-pāḷi (Bur. tr.)",
                                     "根本五十经篇巴利圣典缅译"),
    # --- 义注 aṭṭhakathā (-ဋ္ဌ) ---
    "ဒီ-ဋ္ဌ": ("Dīgha-aṭṭhakathā", "长部义注"),
    "မ-ဋ္ဌ": ("Majjhima-aṭṭhakathā", "中部义注"),
    "သံ-ဋ္ဌ": ("Saṃyutta-aṭṭhakathā", "相应部义注"),
    "အံ-ဋ္ဌ": ("Aṅguttara-aṭṭhakathā", "增支部义注"),
    "အဘိ-ဋ္ဌ": ("Abhidhamma-aṭṭhakathā", "论藏义注"),
    "ဝိ-ဋ္ဌ": ("Vinaya-aṭṭhakathā", "律藏义注"),
    "ပဋိသံ-ဋ္ဌ": ("Paṭisambhidāmagga-aṭṭhakathā", "无碍解道义注"),
    "ဇာတက-ဋ္ဌ": ("Jātaka-aṭṭhakathā", "本生义注"),
    "ဇာ-ဋ္ဌ": ("Jātaka-aṭṭhakathā", "本生义注"),
    "ဓမ္မပဒ-ဋ္ဌ": ("Dhammapada-aṭṭhakathā", "法句义注"),
    "ဓမ္မပဒအဋ္ဌကထာ": ("Dhammapada-aṭṭhakathā", "法句义注"),
    "ဇာတကအဋ္ဌကထာ": ("Jātaka-aṭṭhakathā", "本生义注"),
    "ဥဒါန-ဋ္ဌ": ("Udāna-aṭṭhakathā", "自说经义注"),
    "ဥဒါန်းအဋ္ဌကထာ": ("Udāna-aṭṭhakathā", "自说经义注"),
    "ဣတိဝုတ္တက-ဋ္ဌ": ("Itivuttaka-aṭṭhakathā", "如是语义注"),
    "ဣတိဝုတ္တကဋ္ဌကထာ": ("Itivuttaka-aṭṭhakathā", "如是语义注"),
    "သုတ္တနိပါတ-ဋ္ဌ": ("Suttanipāta-aṭṭhakathā", "经集义注"),
    "သုတ္တနိ-ဋ္ဌ": ("Suttanipāta-aṭṭhakathā", "经集义注"),
    "မဟာနိ-ဋ္ဌ": ("Mahāniddesa-aṭṭhakathā", "大义释义注"),
    "စူဠနိ-ဋ္ဌ": ("Cūḷaniddesa-aṭṭhakathā", "小义释义注"),
    "နေတ္တိ-ဋ္ဌ": ("Nettippakaraṇa-aṭṭhakathā", "导论义注"),
    "မူလပဏ္ဏာသ-ဋ္ဌ": ("Mūlapaṇṇāsa-aṭṭhakathā", "根本五十经篇义注"),
    "ပဋ္ဌာနပကရဏအဋ္ဌကထာ": ("Paṭṭhāna-aṭṭhakathā", "发趣论义注"),
    # --- 复注 ṭīkā (-ဋီ) ---
    "ဒီ-ဋီ": ("Dīgha-ṭīkā", "长部复注"),
    "မ-ဋီ": ("Majjhima-ṭīkā", "中部复注"),
    "သံ-ဋီ": ("Saṃyutta-ṭīkā", "相应部复注"),
    "အံ-ဋီ": ("Aṅguttara-ṭīkā", "增支部复注"),
    "အဘိ-ဋီ": ("Abhidhamma-ṭīkā", "论藏复注"),
    "ဝိ-ဋီ": ("Vinaya-ṭīkā", "律藏复注"),
    "သီ-ဋီ": ("Sīlakkhandhavagga-ṭīkā", "戒蕴品复注"),
    # --- 清净道论系统 ---
    "ဝိသုဒ္ဓိ": ("Visuddhimagga", "清净道论"),
    "ဝိသုဒ္ဓိ-ဋီ": ("Visuddhimagga-ṭīkā", "清净道论复注"),
    "မဟာဋီ": ("Visuddhimagga-mahāṭīkā", "大复注 (清净道论大疏钞)"),
    "မဟာဋီကာ": ("Visuddhimagga-mahāṭīkā", "大复注 (清净道论大疏钞)"),
    "မူလဋီ": ("Mūlaṭīkā", "根本复注"),
    "မူလဋီကာ": ("Mūlaṭīkā", "根本复注"),
    "အနုဋီ": ("Anuṭīkā", "随复注"),
    "အနုဋီကာ": ("Anuṭīkā", "随复注"),
    "ဝိမတိ": ("Vimativinodanī-ṭīkā", "除疑复注"),
    "ဝိမတိဋီ": ("Vimativinodanī-ṭīkā", "除疑复注"),
    "သာရတ္ထ": ("Sāratthadīpanī-ṭīkā", "显扬心义复注"),
    "သာရတ္ထဒီပနီဋီကာ": ("Sāratthadīpanī-ṭīkā", "显扬心义复注"),
    "ဝိနည်းသာရတ္ထဒီပနီဋီကာ": ("Vinaya-sāratthadīpanī-ṭīkā", "律显扬心义复注"),
    "ဝိ-သင်္ဂဟ": ("Vinayasaṅgaha", "律摄"),
    # --- 缅甸论书 / 缅译本 (nissaya) ---
    "ပြည်": ("Pyi-nissaya", "卑谬版尼思耶"),
    "ပြည်နိဿယ": ("Pyi-nissaya", "卑谬版尼思耶"),
    "ပြည်-ဝိသုဒ္ဓိမဂ်နိဿယ": ("Pyi Visuddhimagga-nissaya", "卑谬版清净道论尼思耶"),
    "ပြည်-ဝိသုဒ္ဓိမဂ်-နိဿယ": ("Pyi Visuddhimagga-nissaya", "卑谬版清净道论尼思耶"),
    "ဝိသုဒ္ဓိမဂ်နိဿယ": ("Visuddhimagga-nissaya", "清净道论尼思耶"),
    "ဝိသုဒ္ဓိမဂ္ဂနိဿယ": ("Visuddhimagga-nissaya", "清净道论尼思耶"),
    "မဟာဋီကာနိဿယ": ("Mahāṭīkā-nissaya", "大复注尼思耶"),
    "ဝိသုဒ္ဓိမဂ်မဟာဋီကာနိဿယ": ("Visuddhimagga-mahāṭīkā-nissaya", "清净道论大复注尼思耶"),
    "အဘိဓမ္မတ္ထသင်္ဂဟ": ("Abhidhammatthasaṅgaha", "摄阿毗达摩义论"),
    "သင်္ဂြိုဟ်ဘာသာဋီကာ": ("Saṅgaha-bhāsāṭīkā", "摄义论缅文复注"),
    "အဋ္ဌသာလိနီဘာသာဋီကာ": ("Aṭṭhasālinī-bhāsāṭīkā", "殊胜义注缅文复注"),
    "သမ္မောဟဝိနောဒနီဘာသာဋီကာ": ("Sammohavinodanī-bhāsāṭīkā", "迷惑冰消缅文复注"),
    "ပရမတ္ထဒီပနီ": ("Paramatthadīpanī", "胜义灯"),
    "ပရမတ္ထသရူပဘေဒနီ": ("Paramatthasarūpabhedanī", "胜义自性分别"),
    "ကစ္စာယနသာရ": ("Kaccāyanasāra", "迦旃延精要"),
    "မဟာဗုဒ္ဓဝင်": ("Mahābuddhavaṃsa", "大佛史"),
    "ကမ္မဋ္ဌာန်းကျမ်းကြီး": ("Kammaṭṭhāna-kyan-gyi", "业处大典"),
    "ဝီထိပုံ-ဘုံစဉ်-ဆန်းပုံ-သိမ်ပုံကျမ်း": ("Vīthi-bhūmi-kyan", "心路·界地·历算·结界典"),
    "ဝီထိပုံ-ဘုံစဉ်-ဆန်းပုံ-သိမ်ပုံ-ကျမ်း": ("Vīthi-bhūmi-kyan", "心路·界地·历算·结界典"),
    "ကိုယ်ကျင့်အဘိဓမ္မာ": ("Koyakyint-Abhidhammā", "实修阿毗达摩"),
    "ဥပရိပဏ္ဏာသဋီကာ": ("Uparipaṇṇāsa-ṭīkā", "后分五十经篇复注"),
    "ဇိနာလင်္ကာရဋီကာ": ("Jinālaṅkāra-ṭīkā", "胜者庄严复注"),
    "မဃဒေဝ": ("Maghadeva-laṅkā", "摩伽提婆linkā (缅文诗)"),
    "တိပိဋက-ပါဠိ-မြန်မာ-အဘိဓာန်": ("Tipiṭaka Pāḷi-Myanmar Dictionary",
                                    "三藏巴缅辞典"),
}

# 明显的手误 / 异体，归一到标准写法；输出时会在 note 列标出
TYPO_FIXES = {
    "ဒ္ဒီဋ္ဌ": "ဒီ-ဋ္ဌ",          # 缺分隔符且多写一个 ဒ္
    "သမ္မောဟဝိနောဒနီဘာသာဋီ": "သမ္မောဟဝိနောဒနီဘာသာဋီကာ",
    "မိလန္ဒပဥှာပါဠိတော်": "မိလိန္ဒပဥှာ",
    "မိလိန္ဒပဉှာပါဠိတော်": "မိလိန္ဒပဉှ",
    "အဘိဓမ္မတ္ထသင်္ဂဟကျမ်း": "အဘိဓမ္မတ္ထသင်္ဂဟ",
}

# 缩写最多占几个词元 (超过的部分当作前后说明文字)
MAX_ABBREV_TOKENS = 6

# 册号有时不写数字而写缅文序数词 (ပဉ္စမတွဲ = 第五册)，这里换算回数字
ORDINALS = {"ပထမ": 1, "ဒုတိယ": 2, "တတိယ": 3, "စတုတ္ထ": 4, "ပဉ္စမ": 5,
            "ဆဋ္ဌမ": 6, "ဆဋ္ဌ": 6, "သတ္တမ": 7, "အဋ္ဌမ": 8, "နဝမ": 9,
            "ဒသမ": 10}
ORDINAL_UNITS = ("တွဲ", "အုပ်", "ပိုင်း", "ဋ္ဌ")
ORDINAL_RE = re.compile(
    "^(" + "|".join(sorted(ORDINALS, key=len, reverse=True)) + ")"
    "(" + "|".join(ORDINAL_UNITS) + ")$")

# 手工录入时常把字母 ဝ 误打成缅文数字 ၀ (字形相同)。
# 只在「单独一个 ၀ 且紧贴缅文字母、两侧都不是数字」时还原，避免动到 ၁၀ 这类真数字。
ZERO_AS_WA_RE = re.compile(r"(?<![၀-၉])(?<=[က-ဿ])၀(?![၀-၉])|"
                           r"(?<![၀-၉])၀(?=[က-ဿ])(?![၀-၉])")

VOL_DIRS = ["vol_1", "vol_2", "vol_3", "vol_4", "vol_5"]


# 兜底匹配用: 单词元、够长的缩写，短的 (如 မ, ဝိ) 会误伤普通缅文词
GLUED_CANDIDATES = sorted(
    (k for k in KNOWN_SOURCES if "-" not in k and len(k) >= 4),
    key=len, reverse=True)


def my2int(s):
    """缅文数字 → int"""
    return int(s.translate(_MY2AR))


def int2my(n):
    """int → 缅文数字"""
    return str(n).translate(str.maketrans("0123456789", MY_DIGITS))


def iter_md(root, vols):
    for v in vols:
        base = os.path.join(root, v, "markdown")
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in sorted(filenames):
                if fn.endswith(".md"):
                    yield v, os.path.join(dirpath, fn)


def tokenize(raw):
    """把一条候选串切成 (字母词元列表, 数字词元列表)。

    引文的形状永远是「若干字母词元 + 若干数字词元」，所以第一个数字词元之后
    就不会再有字母了 (正则已经保证了这点)。
    """
    toks = [t for t in SPLIT_RE.split(raw) if t]
    letters, nums = [], []
    for t in toks:
        (nums if IS_DIGIT_TOKEN.match(t) else letters).append(t)
    return letters, nums


def match_source(letters):
    """在字母词元序列里找已知典籍缩写。

    返回 (缩写key, 前缀说明, 后缀说明)；找不到返回 (None, None, None)。
    取最长匹配；长度相同时取靠后的 (前面通常是 "အကျယ်ကို…" 这类说明文字)。
    """
    n = len(letters)
    for size in range(min(n, MAX_ABBREV_TOKENS), 0, -1):
        for start in range(n - size, -1, -1):
            key = "-".join(letters[start:start + size])
            key = TYPO_FIXES.get(key, key)
            if key in KNOWN_SOURCES:
                return key, letters[:start], letters[start + size:]
    # 兜底: 说明文字和缩写之间漏了分隔符，粘成了一个词元
    # (如 ဤစကားကိုမဟာဋီကာ၊၂၊၄၇၀)。只认长缩写，避免误伤普通缅文词。
    for key in GLUED_CANDIDATES:
        if letters[-1].endswith(key) and letters[-1] != key:
            return key, letters[:-1] + [letters[-1][:-len(key)]], []
    return None, None, None


def ordinal_volume(tokens):
    """从 "ပဉ္စမတွဲ" 这类词元里读出册号 (int)，读不出返回 None。"""
    for t in tokens:
        m = ORDINAL_RE.match(t)
        if m:
            return ORDINALS[m.group(1)]
    return None


Cite = collections.namedtuple(
    "Cite", "vol path abbrev nums raw context where prefix suffix typo")


def container_map(text):
    """标出每个字符落在哪种「容器」里: () 圆括号、[] 方括号，或 none 正文。

    引文绝大多数写在圆括号里，但手工录入的书里也有写进方括号、
    或者干脆漏掉括号直接写在正文里的。
    """
    spans = []
    for rx, kind in ((PAREN_RE, "()"), (BRACKET_RE, "[]")):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), kind))
    spans.sort()
    return spans


def where_of(spans, pos):
    for start, end, kind in spans:
        if start <= pos < end:
            return kind
        if start > pos:
            break
    return "none"


def extract(root, vols):
    """扫描全部缅文 markdown，返回 (引文列表, 待人工核对列表)。"""
    cites, review = [], []
    for vol, path in iter_md(root, vols):
        with open(path, encoding="utf-8") as fh:
            text = ZERO_AS_WA_RE.sub("ဝ", fh.read())
        spans = container_map(text)
        for cm in CITE_RE.finditer(text):
            raw = cm.group(1).strip()
            where = where_of(spans, cm.start())
            # 数字后面紧跟缅文字母的，是 "52种/7遍" 之类的量词短语，不是引文
            tail_re = TAIL_OK_RE if where != "none" else TAIL_STRICT_RE
            if not tail_re.match(text[cm.end():]):
                continue
            letters, nums = tokenize(raw)
            if not letters or not nums:
                continue
            line = text[text.rfind("\n", 0, cm.start()) + 1:
                        cm.end() + 20].strip()
            abbrev, prefix, suffix = match_source(letters)
            if abbrev is None:
                if where != "none":       # 正文里的噪音太多，不进核对表
                    review.append((vol, path, raw, line, where))
                continue
            typo = "-".join(letters) if "-".join(letters) in TYPO_FIXES else ""
            cites.append(Cite(vol, path, abbrev, nums, raw, line, where,
                              "-".join(prefix), "-".join(suffix), typo))
    return cites, review


def decide_volumed(cites):
    """判断每个缩写所指的书是否分册 (首数字是册号还是页码)。"""
    stat = collections.defaultdict(lambda: [0, 0])  # abbrev -> [多数字条数, 首数字小的条数]
    for c in cites:
        if len(c.nums) >= 2:
            stat[c.abbrev][0] += 1
            if my2int(c.nums[0]) <= MAX_VOLUME_NO:
                stat[c.abbrev][1] += 1
    return {ab: (tot > 0 and small / tot >= VOLUME_RATIO)
            for ab, (tot, small) in stat.items()}


def plausible_max_volume(cites, volumed):
    """每部书「站得住脚」的最大册号 = 至少出现过两次的册号里最大的那个。

    只出现一次、又比它大的册号多半是漏写册号 (如 ``အံ၊၁၈-၁၉`` 其实是页码)，
    在总表里打上问号让人工核对。
    """
    seen = collections.defaultdict(collections.Counter)
    for c in cites:
        if volumed.get(c.abbrev) and len(c.nums) >= 2:
            v = my2int(c.nums[0])
            if v <= MAX_VOLUME_NO:
                seen[c.abbrev][v] += 1
    out = {}
    for ab, counter in seen.items():
        solid = [v for v, n in counter.items() if n >= 2]
        out[ab] = max(solid) if solid else max(counter)
    return out


def key_of(cite, volumed):
    """引文 → 去掉页码后的规范 key、册号 (缅文数字，无册号为空串)、备注。"""
    if not volumed.get(cite.abbrev):
        return cite.abbrev, "", ""
    if len(cite.nums) >= 2:
        vol_no = cite.nums[0]
        if my2int(vol_no) <= MAX_VOLUME_NO:
            return f"{cite.abbrev}-{vol_no}", vol_no, ""
        # 首数字大得不像册号，多半是漏写了册号、直接写了页码范围
        return cite.abbrev, "", f"疑似漏写册号 (只有页码 {'-'.join(cite.nums)})"
    # 册号写成了序数词，如 ပဉ္စမတွဲ
    ordv = ordinal_volume(cite.prefix.split("-") + cite.suffix.split("-"))
    if ordv:
        vol_no = int2my(ordv)
        return f"{cite.abbrev}-{vol_no}", vol_no, "册号写作缅文序数词"
    return cite.abbrev, "", "疑似漏写册号 (只有一个数字)"


def _cell(x):
    """引文可能跨行书写，制表符/换行必须压平，否则 TSV 会串列。"""
    return re.sub(r"\s+", " ", str(x)).strip() if x is not None else ""


def write_tsv(path, header, rows):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(header) + "\n")
        for r in rows:
            fh.write("\t".join(_cell(x) for x in r) + "\n")
    print(f"  {path}  ({len(rows)} 行)")


def main():
    ap = argparse.ArgumentParser(
        description="提取缅文原书括号内的引文缩写并去重成总表")
    ap.add_argument("vols", nargs="*", default=VOL_DIRS,
                    help="要处理的卷目录 (默认全部五卷)")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), help="仓库根目录")
    ap.add_argument("--outdir", default=None,
                    help="输出目录 (默认 <root>/glossary)")
    ap.add_argument("--stats", action="store_true", help="额外打印统计摘要")
    args = ap.parse_args()

    outdir = args.outdir or os.path.join(args.root, "glossary")
    os.makedirs(outdir, exist_ok=True)

    cites, review = extract(args.root, args.vols)
    if not cites:
        print("没有找到任何引文，检查 --root / 卷目录是否正确", file=sys.stderr)
        return 1
    volumed = decide_volumed(cites)
    vmax = plausible_max_volume(cites, volumed)

    # ---- 汇总 ----
    agg = collections.OrderedDict()
    for c in cites:
        key, vol_no, note = key_of(c, volumed)
        e = agg.setdefault(key, {
            "abbrev": c.abbrev, "vol_no": vol_no,
            "count": 0, "per_vol": collections.Counter(),
            "raw": collections.Counter(), "notes": set(),
            "first": (c.vol, os.path.relpath(c.path, args.root)),
        })
        e["count"] += 1
        e["per_vol"][c.vol] += 1
        if c.where == "[]":
            e["notes"].add("有写在方括号里的")
        elif c.where == "none":
            e["notes"].add("有漏写括号、直接写在正文里的")
        # 记录原始写法 (含页码)，让人一眼看出分隔符/空格上的各种不统一
        e["raw"][re.sub(r"\s+", " ", c.raw).strip()] += 1
        if c.typo:
            e["notes"].add(f"手误归一: {c.typo}")
        if c.prefix:
            e["notes"].add(f"前缀说明: {c.prefix}")
        if note:
            e["notes"].add(note)

    # 孤例又超出常见册数的册号，八成是漏写册号把页码当成了册号
    for key, e in agg.items():
        if e["count"] == 1 and e["vol_no"] \
                and my2int(e["vol_no"]) > vmax.get(e["abbrev"], MAX_VOLUME_NO):
            e["notes"].add(f"册号可疑 (该书常见册号最大到 "
                           f"{int2my(vmax[e['abbrev']])}，疑为漏写册号)")

    def sort_key(item):
        k, e = item
        return (-e["count"], e["abbrev"], my2int(e["vol_no"]) if e["vol_no"] else 0)

    ordered = sorted(agg.items(), key=sort_key)

    print("输出:")
    # ---- 总表 ----
    rows = []
    for key, e in ordered:
        pali, zh = KNOWN_SOURCES[e["abbrev"]]
        rows.append([
            key, e["abbrev"],
            e["vol_no"], my2int(e["vol_no"]) if e["vol_no"] else "",
            pali, zh, e["count"],
            *[e["per_vol"].get(v, 0) for v in VOL_DIRS],
            " | ".join(f"{r}×{n}" for r, n in e["raw"].most_common(6)),
            "; ".join(sorted(e["notes"])),
            e["first"][1],
        ])
    write_tsv(os.path.join(outdir, "citations-master.tsv"),
              ["key", "abbrev", "vol_my", "vol_no", "pali", "zh", "count",
               *VOL_DIRS, "raw_forms", "notes", "first_seen"], rows)

    # ---- 按典籍汇总 ----
    by_src = collections.OrderedDict()
    for key, e in ordered:
        s = by_src.setdefault(e["abbrev"],
                              {"count": 0, "vols": set(), "suspect": set()})
        s["count"] += e["count"]
        if e["vol_no"]:
            v = my2int(e["vol_no"])
            bucket = "suspect" if any("册号可疑" in n for n in e["notes"]) \
                else "vols"
            s[bucket].add(v)
    rows = []
    for ab, s in sorted(by_src.items(), key=lambda x: -x[1]["count"]):
        pali, zh = KNOWN_SOURCES[ab]
        rows.append([ab, pali, zh, s["count"],
                     "分册" if volumed.get(ab) else "不分册",
                     ",".join(int2my(v) for v in sorted(s["vols"])),
                     ",".join(str(v) for v in sorted(s["vols"])),
                     ",".join(str(v) for v in sorted(s["suspect"]))])
    write_tsv(os.path.join(outdir, "citations-sources.tsv"),
              ["abbrev", "pali", "zh", "count", "volumed", "vols_my", "vols",
               "suspect_vols"], rows)

    # ---- 逐条原始记录 ----
    rows = []
    for c in cites:
        key, vol_no, note = key_of(c, volumed)
        pages = c.nums[1:] if (vol_no and c.nums and c.nums[0] == vol_no) \
            else c.nums
        rows.append([c.vol, os.path.relpath(c.path, args.root), key, c.abbrev,
                     vol_no, "-".join(pages),
                     c.raw, c.where, c.prefix, c.suffix, c.typo, note])
    write_tsv(os.path.join(outdir, "citations-raw.tsv"),
              ["vol", "file", "key", "abbrev", "vol_my", "pages",
               "raw", "in", "prefix", "suffix", "typo_of", "note"], rows)

    # ---- 待人工核对 ----
    rev = collections.Counter()
    rev_where = {}
    for vol, path, raw, line, where in review:
        rev[raw] += 1
        rev_where.setdefault(
            raw, (vol, os.path.relpath(path, args.root), where, line))
    rows = [[raw, n, *rev_where[raw]] for raw, n in rev.most_common()]
    write_tsv(os.path.join(outdir, "citations-review.tsv"),
              ["candidate", "count", "vol", "first_seen", "in", "context"],
              rows)

    print(f"\n引文 {len(cites)} 条 → 去重后 {len(agg)} 个「缩写-册号」，"
          f"涉及 {len(by_src)} 部典籍；待核对候选 {len(rev)} 种。")

    if args.stats:
        print("\n按典籍 (出现次数):")
        for ab, s in sorted(by_src.items(), key=lambda x: -x[1]["count"]):
            vols = ",".join(int2my(v) for v in sorted(s["vols"])) or "—"
            sus = ("  ⚠可疑册号: "
                   + ",".join(str(v) for v in sorted(s["suspect"]))) \
                if s["suspect"] else ""
            print(f"  {s['count']:6d}  {ab:22s} 册: {vols:16s} "
                  f"{KNOWN_SOURCES[ab][1]}{sus}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
