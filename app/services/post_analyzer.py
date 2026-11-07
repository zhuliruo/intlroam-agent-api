"""帖子分析器 — 分类 + 回复生成"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.llm_client import LlmResult, call_llm
from app.services.price_client import get_or_fetch_price, format_price_for_reply
from app.services.keyword_filter import has_connectivity_demand
from app.utils.logger import get_logger

log = get_logger(__name__)

# Prompt 文件路径（项目根目录的 prompts/）
PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
# 分类器只有 Facebook/Instagram 会走 (threads/tiktok 在 classify_intent 里短路了)
CLASSIFIER_PROMPT_FILES = {
    "vi": PROMPT_DIR / "vi_classifier.txt",
    "en": PROMPT_DIR / "en_classifier.txt",
}

# (platform, language) -> prompt。threads_reply.txt 自身就是多语言的, 按帖子语言回复,
# 所以 threads 不分语言。
REPLY_PROMPT_FILES = {
    ("facebook", "vi"):  PROMPT_DIR / "vi_reply.txt",
    ("facebook", "en"):  PROMPT_DIR / "en_reply.txt",
    ("instagram", "vi"): PROMPT_DIR / "vi_reply.txt",
    ("instagram", "en"): PROMPT_DIR / "en_reply.txt",
    ("threads", "vi"):   PROMPT_DIR / "threads_reply.txt",
    ("threads", "en"):   PROMPT_DIR / "threads_reply.txt",
    ("tiktok", "vi"):    PROMPT_DIR / "vi_reply_tiktok.txt",
    ("tiktok", "en"):    PROMPT_DIR / "en_reply_tiktok.txt",
    # Reddit (2026-09-20): 独立 prompt。不能回落到 facebook —— 那套默认"对方是卖家"
    # 且允许贴链接, 在 Reddit 上贴链接会被 automod 删评并连累账号。
    ("reddit", "vi"):    PROMPT_DIR / "reddit_reply.txt",
    ("reddit", "en"):    PROMPT_DIR / "reddit_reply.txt",
}

SUPPORTED_LANGUAGES = ("vi", "en")
# 2026-07-14 全面转英语市场之后, 语言没传就该回落到 en 而不是 vi。
# (同一批漏改见 memory intlroam-agent-post-language-fallback —— 那次修了发帖的
#  两个 resolve 函数, 这个默认值当时漏了, 而它是下面 5 个函数的默认参数。)
DEFAULT_LANGUAGE = "en"

# ── 越南语识别 (2026-09-14 重写) ─────────────────────────────────────────
# 旧版注释写的是"英语/中文/日语都不会出现这些字符", 但**漏掉了法语/西班牙语/
# 葡萄牙语/意大利语/冰岛语** —— 正好是英语市场的主力目的地。旧 regex 含
# a-grave/a-acute/e-acute/i-acute/o-acute/u-acute 以及 circumflex 一族, 于是欧洲地名
# 一出现就整篇判成越南语, 接着走 vi_reply prompt 回一条越南语评论:
#     Telefonica(o-acute) / Hallgrimskirkja(i-acute) / Visegrad(a-acute) / chateaux(a-circumflex)
# 实测近 30 天 30450 条语料里 **1537 条 (5.0%) 是这么被误判的**, 0 条反向回归。
#
# 新判据只保留越南语**独有**的字符:
#   - 基础字母 a-breve / o-horn / u-horn / d-stroke  —— 法/西/葡/意/冰岛都没有
#   - 双重符号 (声调 + 元音变体), 越南语专有
#   - 下点号和钩号
# 刻意**不含**欧洲语言共用的单重音字母和 tilde/umlaut/cedilla。
_VI_STRONG = re.compile(
    r"[ăĂơƠưƯđĐ]"
    r"|[ằắẳẵặầấẩẫậ]"
    r"|[ềếểễệ]"
    r"|[ồốổỗộờớởỡợ]"
    r"|[ừứửữự]"
    r"|[ạẹịọụỵ]"
    r"|[ảẻỉỏủỷ]"
    r"|[ỳỷỹỵ]"
)

# 单个强字符可能来自人名/借词, 要求至少两个才翻篇。转英语市场之后判错方向的代价
# 是不对称的: 给越南语帖回英语只是不够地道, 给欧洲帖回越南语是明显的机器人特征。
_VI_MIN_HITS = 2


def detect_language(content: str) -> str:
    """帖子语言。目前只需要区分 vi / en。"""
    if not content:
        return "en"
    return "vi" if len(_VI_STRONG.findall(content)) >= _VI_MIN_HITS else "en"


def normalize_language(language: str | None) -> str:
    lang = (language or "").lower()
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

# 加载一次缓存
_classifier_prompts: dict[str, str] = {}
_reply_prompts: dict[tuple[str, str], str] = {}


def _load_prompt(path: Path) -> str:
    """加载 prompt 文件，去掉 # 开头的注释行"""
    text = path.read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#") or line.strip() == ""
    ).strip()


def get_classifier_prompt(language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    if lang not in _classifier_prompts:
        _classifier_prompts[lang] = _load_prompt(CLASSIFIER_PROMPT_FILES[lang])
    return _classifier_prompts[lang]


# 2026-09-15 业务转型: 找代理 (reseller) 用另一套 B2B prompt。
# 两个代理占全站 83% 收入, 16 个纯直客加起来 $62 —— 一次代理对话的价值
# 远超几百条直客评论, 所以这条链路单独走, 不跟消费者话术混。
B2B_REPLY_PROMPT = PROMPT_DIR / "en_reply_b2b.txt"

# Threads/TikTok 专用补充。这两个平台的帖子没跑过分类器, 我们**不知道**发帖人
# 卖不卖东西 —— 主 prompt 默认"对方是卖家", 直接套用会对着游客喊"你卖不卖货"。
# 各平台的硬上限见下方 _generate_reply_once 的 min_len/max_len:
#   facebook 20-300 / threads 6-200 / tiktok 8-100
# prompt 里原本写死 "under 280", 那是按 facebook 调的; 放开到全平台后
# threads/tiktok 必然超标被整条丢弃 (实测 4 条里 3 条)。所以按平台覆盖。
B2B_LENGTH_NOTE = {
    "facebook": """

## LENGTH — OVERRIDES ANY OTHER LIMIT ABOVE
Under 280 characters. Longer is discarded by the system and the lead is lost.""",
    "threads": """

## LENGTH — OVERRIDES ANY OTHER LIMIT ABOVE
Under 170 characters. That is about 26 words — two short sentences, no more.
Longer is DISCARDED by the system and the lead is lost. Count before you answer.

SHAPE (copy the length, never the wording — it is set in a country you will not be given):
  "lovely shot of the Atlas mountains. IntlRoam wholesales travel eSIMs to agencies,
   so if anyone here runs trips out that way, happy to share trade pricing."
That is 156 characters. Treat it as the ceiling, not the average.""",
    "reddit": """

## LENGTH — OVERRIDES ANY OTHER LIMIT ABOVE
Under 400 characters. Reddit shows comments in full, so a two-word reaction plus a
pitch is transparently an advert. Answer the thread first in one or two sentences,
then make the trade point in one more. NEVER include a link — automod removes
comments with links and the account pays for it.""",
    "tiktok": """

## LENGTH — OVERRIDES ANY OTHER LIMIT ABOVE
Under 95 characters, and that budget is the whole problem — you cannot both react to
the video properly and make the trade point. Anything over 95 is DISCARDED.

So split it exactly like this:
  1. A nod to the video in AT MOST FOUR WORDS. "great shot." / "kyoto looks unreal."
     Not a sentence, not a compliment paragraph. Four words, then stop.
  2. The trade clause. This is what the comment is for.
No greeting, no sign-off, no hashtags, no emoji, no invite if it does not fit.

SHAPE (copy the proportions, never the wording):
  "great shot. IntlRoam supplies travel eSIMs to agencies — DM for rates"
That is 69 characters. Aim there, not at the 95 ceiling.""",
}

B2B_OPEN_AUDIENCE_NOTE = """

## PLATFORM NOTE — Threads / TikTok (overrides the assumption above)

You do NOT know whether the person who posted this sells anything. Most of them do not.
So: never address them as a seller, never say "your clients" or "your packages", and
never ask what they sell.

Write to the OTHER PEOPLE READING THE THREAD instead. Plenty of agencies, guides and
phone-shop owners read these posts. Leave a comment that is genuinely responsive to the
post itself, and let the trade angle sit inside it as one short clause aimed sideways —
"we wholesale these to agencies if anyone here runs trips" — not as the point of the
comment.

## THIS ALSO OVERRIDES THE SILENCE RULES ABOVE

On these two platforms, "no sign this person sells anything" is NOT a reason to stay
silent — it is the normal case, and the whole point of the platform note. Do NOT return
skip_reason "not_reseller" here. The only reasons left to skip are:
  - the author is an established eSIM brand  -> "competitor"
  - IntlRoam already appears in the thread   -> "already_intlroam"
  - the post is empty or unreadable          -> "cannot_engage"
Anything else: write the comment.

Still no prices.

## ROTATE THE TRADE CLAUSE — every comment must differ

⚠️ THE TRADE CLAUSE IS MANDATORY. Every single comment carries it. A reply that is
only a friendly remark about the post is a FAILED reply — that is the consumer flow,
and this is not it. What rotates is the WORDING, never whether it is there.

The trade angle is one short clause, and it MUST be phrased differently every time.
Two of our comments reading alike in the same feed gets the account flagged as a bot,
which is a worse outcome than not commenting at all.

Rotate BOTH what you name and how you address the trade:
- what:  wholesale pricing / trade rates / supplying agencies / no stock to hold /
         instant provisioning / we supply operators / reseller-friendly terms
- who:   "if anyone here runs trips" / "for anyone selling travel out that way" /
         "agencies and guides" / "if you book this route for clients" /
         "anyone putting packages together" / "for the operators reading this"
- close: "DM for rates" / "happy to share trade pricing" / "can send the trade sheet" /
         "message me" — or drop ONLY the close when the budget is tight. The trade
         clause itself still stays.

Never reuse the wording of any SHAPE example above — those exist to show length only."""


def get_reply_prompt(platform: str = "facebook", language: str = DEFAULT_LANGUAGE,
                     intent: str | None = None) -> str:
    # 2026-09-15 v2: 复用 warm 而不是新增枚举 —— warm 已重定义为"代理线索"
    # (旅行社/导游/本地 SIM 卖家), 普通旅行者下沉到 cold。
    # B2B 话术不分语言 (prompt 内部要求跟随对方语言), 但**分平台**, 见下。
    #
    # PIVOT-TT 2026-09-15: 闸门从"只有 facebook"放开到全平台。
    # 早先收口是因为 Threads/TikTok 的短路无条件判 warm, 那个 warm 不含任何
    # "这个人在卖东西"的信号 (当时真给一个旅游博主发出过招商话术)。现在短路
    # 改成了按需求分流 —— 有需求判 hot, 没需求才判 warm —— warm 重新有了含义:
    # "这条不是买家, 按找代理处理"。
    #
    # 但 Threads/TikTok 上**不能假设发帖人自己是卖家**, 所以下面按平台追加一段
    # 说明, 让话术面向帖子下的同行读者讲, 而不是指认发帖人。
    if (intent or "").lower() == "warm":
        plat = (platform or "facebook").lower()
        key = ("_b2b", plat)
        if key not in _reply_prompts:
            base = _load_prompt(B2B_REPLY_PROMPT)
            if plat in ("threads", "tiktok", "reddit"):
                base += B2B_OPEN_AUDIENCE_NOTE
            # 长度说明放最后 —— 它要覆盖 prompt 正文里写死的 280
            base += B2B_LENGTH_NOTE.get(plat, B2B_LENGTH_NOTE["facebook"])
            _reply_prompts[key] = base
        return _reply_prompts[key]

    p = (platform or "facebook").lower()
    lang = normalize_language(language)
    if (p, lang) not in REPLY_PROMPT_FILES:
        p = "facebook"
    key = (p, lang)
    if key not in _reply_prompts:
        _reply_prompts[key] = _load_prompt(REPLY_PROMPT_FILES[key])
    return _reply_prompts[key]


# ── Threads: 联系方式 CTA 兜底剥离 (2026-07-26) ─────────────────────────
# threads_reply.txt v5 已明令禁止 "DM me / 私訊我 / nhắn mình", 但 prompt 不是硬约束:
# 改版前 14 天里 83 条成功回复有 39 条带这类邀约, 整条评论因此读起来像广告。
# 这里按句切分, 丢掉带邀约的整句 —— 比整条回复作废划算 (剩下的部分本身就是有用回答)。
_CTA_PATTERNS = re.compile(
    # "DM我" 里 M 和 我 之间没有 \b (中文也是 \w), 所以尾部不能收 \b
    r"(?:\bd\.?m\.?|\bp\.?m\.?)\s*(?:me\b|mình|我|us\b)"
    r"|message\s+me|inbox\s+me|\binbox\b|hit\s+me\s+up|reach\s+out\s+to\s+me"
    r"|feel\s+free\s+to\s+(?:ask|message|reach|dm)"
    r"|let\s+me\s+know\s+if\s+you\s+(?:want|need)"
    r"|happy\s+to\s+(?:share|send)\s+(?:the|a)?\s*(?:link|details|more)"
    r"|nhắn\s*(?:mình|tin|cho\s*mình)|\bib\s*mình\b"
    r"|私訊|私信|可以找我|問我|问我"
    r"|DMください|DMして|メッセージください"
    r"|메시지\s*주세요|DM\s*주세요|디엠"
    r"|ทักแชท|ทักมา",
    re.IGNORECASE,
)


def strip_contact_cta(text: str) -> str:
    """删掉"来找我私聊"性质的整句, 保留回复里真正有用的部分。"""
    parts = [p for p in re.split(r"(?<=[.!?。！？\n])\s*", text) if p.strip()]
    kept = [p.strip() for p in parts if not _CTA_PATTERNS.search(p)]
    out = ""
    for part in kept:
        # 中日韩句号后面不加空格 (原文本来就没有), 拉丁文才加
        out = f"{out} {part}" if out and out[-1] in ".!?" else out + part
    return out.strip()


@dataclass
class ClassificationResult:
    intent: str          # hot / warm / cold / competitor / ignore
    confidence: float
    reasoning: str | None
    matched_keywords: list[str]
    destination_country: str | None
    llm_provider: str
    llm_model: str
    llm_latency_ms: int
    raw_text: str | None = None


@dataclass
class ReplyResult:
    reply: str
    tone: str
    llm_provider: str
    llm_model: str
    llm_latency_ms: int


# ---------- API ----------

# Threads/TikTok 短于这个长度就不送分类器 —— 判不出东西, 白花一次调用。
# 2026-09-16: 从 40 降到 25。40 太保守 ——
# "Sunset over the Bund, so lively tonight" 只有 39 字符, 明摆着是风景照,
# 却因为差一个字符走兜底判成 warm, 白吃一条招商留言。
# 25 以下通常真的只剩标签串或 "(no text extracted)", 那才是判不出来的。
# 正文被 "See more" 截断的标记。客户端 v1.39 起会先展开再抓, 但旧版本还在跑,
# 而截断内容判出来的 warm 有相当一部分其实是旅客 (实测日报线索 27 条里
# 9 条不该进名单, 其中 5 条是截断的)。
_TRUNCATED_RE = re.compile(r"(…|\.\.\.)\s*(more|xem thêm|更多|展开)\s*$", re.IGNORECASE)


def is_truncated(content: str) -> bool:
    return bool(_TRUNCATED_RE.search((content or "").rstrip()))


MIN_CLASSIFY_CHARS = 25

VALID_INTENTS = {"hot", "warm", "cold", "competitor", "ignore"}
MIN_CONFIDENCE = 0.5


async def classify_intent(
    content: str,
    platform: str = "facebook",
    comment_count: int | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> ClassificationResult:
    """
    LLM 分类。结果默认按 confidence 阈值过滤：
    - 低置信度的 hot/warm 自动降级为 cold

    Sprint 1 W2 (TikTok): 关键词搜索已预筛相关性, 跳过 LLM 分类,
    默认 warm. (competitor / advertiser 在 posts.py 上层已检测)
    """
    # ── Threads / TikTok: 改走真分类器 (2026-09-16) ──────────────
    #
    # 这两个平台原来**完全不跑分类器**, 短路给默认值 (有需求->hot, 其余->warm)。
    # 后果是每条帖子都"可回": 35 小时 threads 462 条、tiktok 149 条全部 warm/hot,
    # 风景照/游记/闲聊照样收到招商留言 —— 就是用户说的"评论偏差"。
    #
    # 短路当初的两条理由现在都不成立:
    #   ① "Threads 多语言, VI 分类器搞不定" —— 分类器早就按语言选到
    #      en_classifier.txt, 那是 LLM prompt, 本来就读得了任何语言。
    #   ② "TikTok 关键词已预筛相关性" —— 关键词只保证"沾旅游", 不保证
    #      "这个人在卖东西"。转型后要的是后者, 那正是分类器在判的。
    #
    # 只保留一种短路: 文本太少时没法分类 (TikTok 常抓不到 caption),
    # 硬送 LLM 只是白花钱, 维持原来的代理导向默认。
    if (platform or "").lower() in ("threads", "tiktok"):
        if len((content or "").strip()) < MIN_CLASSIFY_CHARS:
            demand = has_connectivity_demand(content)
            log.info("classify_thin_content_bypass", platform=platform,
                     chars=len((content or "").strip()), demand=demand)
            return ClassificationResult(
                intent="hot" if demand else "warm",
                confidence=0.90 if demand else 0.85,
                reasoning=(f"{platform}: content too thin to classify "
                           f"({len((content or '').strip())} chars) -> "
                           f"{'hot' if demand else 'warm (reseller-oriented)'}"),
                matched_keywords=[],
                destination_country=None,
                llm_provider="bypass",
                llm_model="thin_content_default",
                llm_latency_ms=0,
                raw_text=None,
            )
        # 文本够 —— 落到下面的正常 LLM 分类


    system = get_classifier_prompt(language)
    user = f"Platform: {platform}\n\nPost content:\n{content}"

    result: LlmResult = await call_llm(
        system_instruction=system,
        user_message=user,
        temperature=0.2,    # 分类用低温
        json_mode=True,
    )

    # 失败兜底
    if not result.success or not result.data:
        log.warning("classify_failed", error=result.error, raw=result.raw_text)
        return ClassificationResult(
            intent="ignore", confidence=0.0,
            reasoning=f"LLM error: {result.error}",
            matched_keywords=[], destination_country=None,
            llm_provider=result.provider, llm_model=result.model,
            llm_latency_ms=result.latency_ms,
            raw_text=result.raw_text,
        )

    data = result.data
    intent = str(data.get("intent", "ignore")).lower()
    confidence = float(data.get("confidence", 0))

    if intent not in VALID_INTENTS:
        log.warning("classify_unknown_intent", got=intent)
        intent = "ignore"

    # 低置信度的强信号 → 降级
    if intent in ("hot", "warm") and confidence < MIN_CONFIDENCE:
        log.info("classify_downgrade_low_conf",
                 intent=intent, conf=confidence)
        intent = "cold"

    return ClassificationResult(
        intent=intent,
        confidence=confidence,
        reasoning=data.get("reasoning"),
        matched_keywords=data.get("matched_keywords", []) or [],
        destination_country=data.get("destination_country"),
        llm_provider=result.provider,
        llm_model=result.model,
        llm_latency_ms=result.latency_ms,
        raw_text=result.raw_text,
    )


# ── 品牌提及率控制 (2026-08-10) ──────────────────────────────────────
# 起因: 12d18 的 TikTok 账号评论被限流。近 10 天该机 298 条 TikTok 评论
# **100%** 都点名 "IntlRoam"(threads 99.2%, facebook 76.9%) —— 文案条条不重复,
# 但"一个账号每条评论都念同一个品牌名"本身就是平台反垃圾最标准的特征。
#
# 为什么在服务端掷骰子而不是在 prompt 里写"20% 的回复不要提品牌":
# 每次 LLM 调用是独立无状态的, 它不知道前面 4 条提没提过, 这类概率指令实测
# 要么塌成 0% 要么远超目标。掷骰子决定 mode、再下发**确定性**的指令, 比例才可控。
#
# 各平台的强制"无品牌"概率 = 1 - 目标/实测自然提及率:
#   tiktok  自然 100.0% → 0.20  (→ 80%)
#   threads 自然  99.2% → 0.20  (→ 79%)
#   facebook 自然 76.9% → 0.00  ← 已经低于 80% 目标, 再强制会掉到 ~62%。
#            en_reply.txt 本来就写着 "Set mentions_intlroam honestly", 所以 FB
#            天然有两成多的无品牌回复。留 0.0 是"已达标", 不是"没做"。
# 2026-11-02: 用户要求所有评论品牌率 ≥80%。配合"缺品牌返工一次", 全平台统一 0.10
# (落点约 85-90%, 给返工失败留余量)。FB 从 0.0 改 0.10: 返工后自然值会接近 100%。
NO_BRAND_RATE = {
    "tiktok": 0.10,
    "threads": 0.10,
    "facebook": 0.10,
    "instagram": 0.10,
    # Reddit (2026-09-20 新接入): 版主是真人, 帖子下面的人会公开质问"你是不是托",
    # 条条点名是这里最快的封号路径。没有历史数据可依, 先按一半无品牌起步,
    # 跑一周看 raw_comments 的存活率再调。
    # 2026-11-02: 品牌率 ≥80% 要求覆盖所有平台 → 0.10。
    "reddit": 0.10,
}

_BRAND_RE = re.compile(r"intl\s*roam|intlroam\.com", re.IGNORECASE)

# 2026-11-02: B2B (warm) 的无品牌概率单独一张表。
# 审计 (10-29~11-01) 发现 warm 回复品牌率只有 threads 19% / tiktok 5% / facebook 58%,
# 拉低总品牌率到 20-30%。原因: en_reply_b2b.txt 只写 "Set mentions_intlroam honestly",
# 而 B2B_LENGTH_NOTE 的 SHAPE 示例是 "we supply ..." 不带品牌, LLM 照抄。
# 招商留言不带品牌等于白发 —— 同行读者不知道该找谁。现在 warm 强制带品牌
# (_B2B_BRAND_DIRECTIVE + 缺品牌返工一次), 再用这张表掷骰稀释到 ~80%,
# 避开 TikTok 那次"100% 点名"招来的限流。facebook 也要 0.2: 强制带品牌后
# 不稀释就是 100%。
NO_BRAND_RATE_B2B = {
    "facebook": 0.10,
    "threads": 0.10,
    "tiktok": 0.10,
    "instagram": 0.10,
    "reddit": 0.10,
}

_HOT_BRAND_RETRY = """

## YOUR PREVIOUS DRAFT DID NOT NAME INTLROAM. REWRITE IT.
Same post, same length budget, same helpful answer — but name IntlRoam once,
naturally, as the thing you used, in the SECOND sentence or later (never open with
the brand). One word: IntlRoam. Set "mentions_intlroam": true.
"""

# warm + 带品牌模式时追加在 system 末尾 (在长度说明之后)。
_B2B_BRAND_DIRECTIVE = """

## NAME THE BRAND — MANDATORY IN THIS REPLY
The trade clause must name IntlRoam, as the subject of the sentence:
"IntlRoam wholesales ...", "IntlRoam supplies ...", "we're IntlRoam — we ...".
A trade pitch with no name ("we supply eSIMs to agencies") is useless: the
operators reading it have no idea who to contact. Write it as one word: IntlRoam.
Set "mentions_intlroam": true. The length limit above still wins — cut the
reaction to the post, never the brand.
"""

# warm + 无品牌模式: 通用的 _NO_BRAND_DIRECTIVE 叫它"写旅行者评论", 跟 B2B
# "招商角度必须在"直接冲突 (结果是先出旅行者寒暄 → _TRADE_SIGNAL 不过 → 返工,
# 白烧一次调用)。B2B 单独一段: 招商角度保留, 只是不点名。
_NO_BRAND_DIRECTIVE_B2B = """

===================================================================
NO-BRAND MODE - THIS OVERRIDES THE BRAND RULES ABOVE
===================================================================
For THIS ONE reply only: do NOT write "IntlRoam" or "intlroam.com".
Keep the trade clause exactly as the rules above require — it is still mandatory —
just phrase it without the name ("we wholesale travel eSIMs to agencies").
Do NOT return an empty reply because of this. Set "mentions_intlroam": false.
"""

_B2B_BRAND_RETRY = """

## YOUR PREVIOUS DRAFT DID NOT NAME INTLROAM. REWRITE IT.
Same post, same length budget, same trade clause — but the trade clause must
name IntlRoam ("IntlRoam supplies ...", "IntlRoam wholesales ..."). Shorten the
reaction to the post if you need room.
"""

# 追加在 system prompt **末尾** —— 所有 reply prompt 都写着"必须提到 IntlRoam"
# (threads 更是把 mentions_intlroam=true 写进了 JSON 硬约束), 所以这段必须
# 显式声明覆盖上文, 且必须堵死"提不了品牌就返回空"这条退路。
_NO_BRAND_DIRECTIVE = """

===================================================================
NO-BRAND MODE - THIS OVERRIDES EVERY RULE ABOVE
===================================================================
For THIS ONE reply only: do NOT write "IntlRoam", do NOT write "intlroam.com",
and do NOT hint at any eSIM or roaming brand at all.

Write a genuinely useful traveller comment that stands on its own: a concrete tip,
a specific detail about the destination, a short piece of your own experience, or a
question back to them. It must still obey every other rule above - length limits, no
call to action, no naming other apps, no competitor names, no links.

Wherever a rule above says the brand MUST appear, or that a reply is only worth
posting if the brand comes up, that rule DOES NOT APPLY to this reply.

Do NOT talk them into a competing way of getting data either - no airport SIM counters,
no pocket wifi rental, no carrier roaming add-on. If connectivity comes up at all, keep it
neutral ("sort your data out before you fly") or just answer a different part of their post.
Recommending the airport SIM desk is worse than saying nothing.

Do NOT return an empty reply just because you cannot name the brand. A brand-free
helpful comment is exactly what is wanted here.
If the JSON schema has "mentions_intlroam", set it to false. If it has "skip_reason",
it stays "none" (you are replying, not skipping).
"""


class _GenuineSkip:
    """prompt 判定"这帖子本来就不该回"(竞品/非旅行/已提过我们)。
    和"这次生成失败了"区分开 —— 后者在 no-brand 模式下值得换回带品牌重试一次,
    前者重试多少次都还是跳过, 白烧一次 LLM 调用。threads 有 ~94% 的调用是跳过,
    不区分的话光重试就会把 threads 的 LLM 量抬高近 20%。"""
    __slots__ = ()


_GENUINE_SKIP = _GenuineSkip()
# prompt 里定义的"内容本身不该回"的跳过原因
_GENUINE_SKIP_REASONS = {"competitor", "not_travel", "already_intlroam"}


# warm = 代理线索, 回复必须带招商角度。这里只做"有没有"的粗判, 不判好坏。
# 多语言: 车队实测在用越南语/马来语/印尼语/中文/阿拉伯语/法语/印地语/日语,
# 只认英文会把 "on fournit en gros aux revendeurs"、"بأسعار جملة" 这类真招商
# 误判成没带 —— 漏判的代价只是多一次调用, 所以宁可宽。
_TRADE_SIGNAL = re.compile(
    r"wholesale|trade (pricing|rate|sheet|price|term)|resell|reseller|distributor|"
    r"supplier|agenc(y|ies)|tour operator|operators|if anyone here|anyone selling|"
    r"putting packages|packages together|for the operators|no stock|b2b|bulk|"
    r"we supply|we provide|supply (travel )?esim|for agencies|in the trade|"
    r"your clients|for guests|per customer|provision"
    r"|đại lý|bán buôn|bán sỉ|giá sỉ|nhập sỉ|esim sỉ|cung cấp esim|bạn bán|khách của|bên mình"
    r"|agen |agensi|pengedar|borong|grosir|harga grosir|yang jual|untuk agen"
    r"|sesiapa yang|kalau ada yang"
    r"|代理|分销|批发|同行|旅行社|供应|批發|分銷"
    r"|جملة|بيبيع|نوفر"
    r"|en gros|revendeur|grossiste"
    r"|टूर पैकेज|थोक"
    r"|卸|代理店",
    re.I)

_TRADE_RETRY = """

## YOUR PREVIOUS DRAFT HAD NO TRADE ANGLE AT ALL. THAT IS A FAILED REPLY.

This post was routed here because it is NOT someone asking to buy — so a friendly
traveller comment is the wrong output entirely. Rewrite it so that one short clause
speaks to the people in this thread who SELL travel: wholesale supply, trade pricing,
"if anyone here runs trips", or asking what they sell. Keep it inside the same length
budget. If you genuinely cannot say anything on-topic, return skip_reason instead of
another traveller pleasantry.
"""


def reply_len_bounds(platform: str) -> tuple[int, int]:
    """各平台回复字数上下限。抽成函数是为了返工时能引用同一个数, 不再两处写死。"""
    p = (platform or "").lower()
    if p == "tiktok":
        return 8, 100
    if p == "threads":
        return 6, 200
    # Reddit: 评论是被"读"的, 一句话的短评论在这里反而像机器人; 但超过 400
    # 就开始像软文长贴。见 reddit_reply.txt 的 LENGTH 段 (两处必须一致)。
    if p == "reddit":
        return 30, 400
    # FB: 250 是按越南语短评论风格定的 (vi_reply 只要 30-100 字)。英语软文要装下
    # "先答疑 + 顺带提品牌 + 一条诚实缺点", 250 会把合格回复静默丢掉 -> 放宽到 300。
    return 20, 300


# 超长返工。模型对"字数上限"这类软约束不稳, 但给它**实际写了多少**之后准得多
# —— 发帖链路 (posts_w1.py) 早就这么做, 这里给回复链路补齐。只返工一次:
# 第二次仍超说明这条帖子就是撑不进预算, 再问也是浪费一次调用。
_LEN_RETRY = """

## YOUR PREVIOUS DRAFT WAS {got} CHARACTERS. THE LIMIT IS {cap}.

Rewrite it under {cap} characters. Keep the point of the comment — cut the scene-setting
and any pleasantries first, then the closing invite. Count before you answer.
"""


async def _generate_reply_once(
    content: str,
    intent: str,
    destination_country: str | None = None,
    redis = None,  # Hotfix 3.12: 接受 Redis 客户端用于价格注入
    platform: str = "facebook",  # Sprint 1: 决定 prompt + 长度限制
    comment_count: int | None = None,  # Fix C: 评论数 → LLM viral signal
    language: str = DEFAULT_LANGUAGE,  # 决定用哪套 prompt (vi/en)
    no_brand: bool = False,  # 2026-08-10: 本次强制不提品牌 (见 NO_BRAND_RATE)
    promo: str | None = None,  # 2026-10-31: 群组国家主推产品段落 (country_promo), 拼到 system 末尾
    _over_len: int | None = None,  # 内部用: 上一稿的字数, 非 None 表示这是返工
    _no_trade: bool = False,       # 内部用: 上一稿没带招商角度, 这是返工
    _brand_retry: bool = False,    # 内部用: 上一稿 (warm) 没点名品牌, 这是返工
) -> ReplyResult | None | _GenuineSkip:
    """
    为 hot/warm 帖子生成越南语回复。
    """
    if intent not in ("hot", "warm", "cold"):   # cold: 2026-11-02 起 FB/TikTok 普通旅行者走直客话术
        return None

    is_b2b = (intent or "").lower() == "warm"
    system = get_reply_prompt(platform, language, intent)
    if promo:
        system += promo
    if no_brand:
        system += _NO_BRAND_DIRECTIVE_B2B if is_b2b else _NO_BRAND_DIRECTIVE
    elif is_b2b:
        system += _B2B_BRAND_DIRECTIVE
    if _brand_retry:
        system += _B2B_BRAND_RETRY if is_b2b else _HOT_BRAND_RETRY
    if _over_len is not None:
        system += _LEN_RETRY.format(got=_over_len, cap=reply_len_bounds(platform)[1])
    if _no_trade:
        system += _TRADE_RETRY
    extra = ""
    if destination_country:
        extra = f"\nDestination: {destination_country}"

    # Fix C v2: 评论数作为 viral signal hint (软化版 — 让 LLM 不被强约束逼到放弃)
    if comment_count is not None and comment_count > 0:
        if comment_count >= 500:
            extra += f"\nVideo has {comment_count} comments (very engaged audience — confident reply, mention 2-3 selling points, max 120 chars)"
        elif comment_count >= 100:
            extra += f"\nVideo has {comment_count} comments (engaged audience)"
        elif comment_count >= 20:
            extra += f"\nVideo has {comment_count} comments (moderate engagement — natural reply 60-100 chars)"
    # If content is too vague or domestic-only Vietnam destination, it's OK to return short/empty reply
    user = f"Intent: {intent}{extra}\n\nPost content:\n{content}"

    result: LlmResult = await call_llm(
        system_instruction=system,
        user_message=user,
        temperature=0.9,    # Hotfix 3.11: anti-template, more variety
        json_mode=True,
    )

    if not result.success or not result.data:
        log.warning("reply_gen_failed", error=result.error, platform=platform)
        return None

    # Sprint 1: 兼容 str / dict 两种返回
    # 某些 platform prompt 可能直接吐纯文本而非 JSON
    if isinstance(result.data, dict):
        # 空 reply ≠ 故障。threads_reply.txt 明令"该跳过时 reply 必须是空字符串 +
        # skip_reason 说明原因" —— threads 走 classify_bypass 不做意图分类，
        # 全靠这个 prompt 兜底筛掉信息流噪音，所以空 reply 是设计好的过滤信号。
        # 以前这里和"键真的缺失"共用 missing_reply_key + warning，实测 3 天 1856 条
        # 里 100% 是主动跳过（threads 占 1905/1912 的空回复），假告警把真故障埋了，
        # 而且 skip_reason 被整个丢掉，没法判断跳过是否合理。
        # 行为不变：两种情况都 return None（本来就没东西可发）。
        if "reply" not in result.data:
            log.warning("reply_gen_failed", error="missing_reply_key",
                        platform=platform, keys=sorted(result.data.keys())[:8])
            return None
        reply_text = str(result.data["reply"]).strip()
        if not reply_text:
            skip_reason = result.data.get("skip_reason") or "unspecified"
            log.info("reply_skipped", platform=platform, intent=intent,
                     skip_reason=skip_reason, no_brand=no_brand)
            # 内容层面的跳过 = 换个模式再问也是同样结果, 别重试。
            if skip_reason in _GENUINE_SKIP_REASONS:
                return _GENUINE_SKIP
            return None
    elif isinstance(result.data, str):
        # 纯文本回退: 尝试解析嵌套 JSON, 否则当成原始 reply
        raw = result.data.strip().lstrip("`").rstrip("`")  # 去 markdown 代码围栏
        if raw.startswith("{"):
            import json as _json
            try:
                parsed = _json.loads(raw)
                reply_text = str(parsed.get("reply", raw)).strip()
            except Exception:
                reply_text = raw
        else:
            reply_text = raw
    else:
        log.warning("reply_gen_failed",
                    error="invalid_data_type",
                    data_type=type(result.data).__name__)
        return None
    # Threads: 剥离 "DM/私訊/nhắn mình" 这类联系邀约 (prompt 之外的兜底, 见上方注释)
    if platform in ("threads", "reddit"):
        stripped = strip_contact_cta(reply_text)
        if stripped != reply_text:
            log.info("reply_cta_stripped",
                     platform=platform, before=reply_text[:80], after=stripped[:80])
            reply_text = stripped

    # 安全检查：长度限制
    # Hotfix 2.1: 长度放宽，激进模式回复更长（带价格 + 优惠码）
    # Sprint 1: TikTok 期望 15-50 字, FB 保持 20-250
    min_len, max_len = reply_len_bounds(platform)
    # LLM 判定"不该回"时返回 null, 上面 str() 成了 "None" —— 那是正常跳过,
    # 不是长度异常。以前混在一起记 warning: Facebook 197 条里 196 条是这个,
    # 真正超长的只有 1 条, 日志完全读不出真相 (排查时把我自己也误导了)。
    if reply_text.strip().lower() in ("none", "null"):
        log.info("reply_skipped", platform=platform, intent=intent,
                 skip_reason="llm_returned_null", no_brand=no_brand)
        return _GENUINE_SKIP

    # 超长 → 带实际字数返工一次 (只一次, 见 _LEN_RETRY 注释)
    if len(reply_text) > max_len and _over_len is None:
        log.info("reply_len_retry", platform=platform, length=len(reply_text), cap=max_len)
        return await _generate_reply_once(
            content=content, intent=intent, destination_country=destination_country,
            redis=redis, platform=platform, comment_count=comment_count,
            language=language, no_brand=no_brand, promo=promo, _over_len=len(reply_text),
            _no_trade=_no_trade, _brand_retry=_brand_retry,
        )

    if len(reply_text) > max_len or len(reply_text) < min_len:
        log.warning("reply_length_invalid",
                    length=len(reply_text), platform=platform, text=reply_text[:50])
        return None

    # warm 必须带招商角度 —— 没带就返工一次 (2026-09-16)。
    # 只对 warm 生效: hot 是"直接问 eSIM 的真实需求", 那条路走消费者话术,
    # 这正是用户定的唯一例外。
    if (intent or "").lower() == "warm" and not _no_trade \
            and not _TRADE_SIGNAL.search(reply_text):
        log.info("reply_no_trade_retry", platform=platform, text=reply_text[:70])
        return await _generate_reply_once(
            content=content, intent=intent, destination_country=destination_country,
            redis=redis, platform=platform, comment_count=comment_count,
            language=language, no_brand=no_brand, promo=promo, _no_trade=True,
        )

    # 安全检查：禁词（Hotfix 2.1 — 激进开放模式，仅禁竞品名 + 短链接 + 错误电话）
    BANNED = [
        # 竞品名
        "airalo", "holafly", "saily", "klook esim", "klook sim",
        "ubigi", "yoho mobile", "travel wifi", "fly wifi", "skyroam",
        # 2026-09-15: "pocket wifi" 已移出 —— 它是品类词, 不是品牌。
        # 14 天生产数据: 本规则共触发 3 次, 3 次全是它, 0 次命中真竞品。
        # 消费者问"该租哪个随身 wifi"正是最高意图的提问, 回复只是常规对比
        # ("要多带一个设备充电, eSIM 更省事"), 却被整条丢掉; B2B 流程里对方
        # 往往自己就在卖随身 wifi, 复述其原话同样会被误杀。品牌名仍全部禁用。
        # 短链接服务（intlroam.com 是允许的）
        "bit.ly", "tinyurl", "t.co/", "goo.gl",
        # 明显的诈骗标志
        "miễn phí 100%", "cam kết hoàn tiền 100%",
        # http/https 但允许 intlroam.com（在下面判断里特殊处理）
    ]
    # Hotfix 2.1: 简单子串匹配 + 特殊处理 http (允许 intlroam.com)
    # TikTok hides/removes comments that name competing social/messaging apps,
    # so ban naming them in TikTok replies specifically (other platforms unaffected).
    BANNED_TIKTOK = [
        "zalo", "facebook", "instagram", "messenger", "whatsapp",
        "telegram", "wechat", "weixin", "viber", "kakaotalk", "kakao",
    ]
    reply_lower = reply_text.lower()
    active_banned = BANNED + (BANNED_TIKTOK if platform == "tiktok" else [])
    if any(b.lower() in reply_lower for b in active_banned):
        log.warning("reply_banned_term", text=reply_text, platform=platform)
        return None
    # 单独处理 http: 允许 intlroam.com，但禁其他链接
    import re
    other_links = re.findall(r'https?://(?!(?:www\.)?intlroam\.com)[^\s)]+', reply_lower)
    if other_links:
        log.warning("reply_external_link", links=other_links, text=reply_text)
        return None

    # 指令没被遵守时留个痕, 用来核对实际提及率有没有落在 80% 附近。
    # 不作废重来: 回复本身仍然合格, 只是这一条没起到稀释作用。
    if no_brand and _BRAND_RE.search(reply_text):
        log.info("no_brand_leaked", platform=platform, text=reply_text[:80])

    # 2026-11-02: warm 带品牌模式下没点名 → 返工一次。返工稿必须**同时**带品牌和
    # 招商角度才采用, 否则保留这一稿 (它已通过全部检查, 不能因为返工失败丢掉线索)。
    # 2026-11-02 v2: 用户要求所有评论品牌率 ≥80% → 返工扩到 hot (直客) 也做。
    # hot 走的 en_reply.txt 写着 "Set mentions_intlroam honestly", FB 直客自然值只有 ~77%。
    if not no_brand and not _brand_retry and not _BRAND_RE.search(reply_text):
        log.info("brand_retry", platform=platform, intent=intent, text=reply_text[:70])
        again = await _generate_reply_once(
            content=content, intent=intent, destination_country=destination_country,
            redis=redis, platform=platform, comment_count=comment_count,
            language=language, no_brand=False, promo=promo,
            _no_trade=True, _brand_retry=True,
        )
        if (isinstance(again, ReplyResult) and _BRAND_RE.search(again.reply)
                and (not is_b2b or _TRADE_SIGNAL.search(again.reply))):
            return again
        log.info("brand_retry_failed_keep_first", platform=platform, intent=intent)

    return ReplyResult(
        reply=reply_text,
        tone=str(result.data.get("tone", "casual")),
        llm_provider=result.provider,
        llm_model=result.model,
        llm_latency_ms=result.latency_ms,
    )


async def generate_reply(
    content: str,
    intent: str,
    destination_country: str | None = None,
    redis = None,
    platform: str = "facebook",
    comment_count: int | None = None,
    language: str = DEFAULT_LANGUAGE,
    promo: str | None = None,
) -> ReplyResult | None:
    """按 NO_BRAND_RATE 掷骰子决定这一条提不提品牌, 其余逻辑全在 _generate_reply_once。

    无品牌模式下如果没能产出可用回复(空回复/超长/命中禁词), 会**带品牌重试一次** ——
    宁可这条没起到稀释作用, 也不能因为不让提品牌就把一条真线索整个丢掉。
    """
    plat = (platform or "facebook").lower()

    # ── 第 1 步: 永远先走带品牌路径 ────────────────────────────────────────
    # 它同时承担**相关性判定** —— threads 没有独立分类器 (raw_posts 里 1298 条
    # 全是 warm), reply prompt 的 skip_reason 就是唯一的门槛。跳过就是跳过,
    # 不给无品牌模式"捡漏"的机会。
    res = await _generate_reply_once(
        content=content, intent=intent, destination_country=destination_country,
        redis=redis, platform=platform, comment_count=comment_count,
        language=language, no_brand=False, promo=promo,
    )
    if isinstance(res, _GenuineSkip) or res is None:
        return None

    # ── 第 2 步: 确认这帖子值得回之后, 才掷骰决定这一条提不提品牌 ──────────
    # 2026-08-19 修: 老代码在调用层面掷骰, 而两种模式的跳过率是 ~96% vs ~38%
    # (无品牌指令里写了"不许因为不能提品牌就返回空"), 于是 20% 的**调用**份额
    # 被跳过率放大成 ~79% 的**发出**份额, 品牌率从 99% 掉到 33.7%。副作用比数字
    # 更糟: 无品牌模式在回那些本该跳过的帖子, 实测样本里有纯寒暄和印尼语/韩语回复。
    # 现在掷骰在门槛之后, 直接控制发出份额, 两种模式共用同一条相关性判定。
    rate_table = NO_BRAND_RATE_B2B if (intent or "").lower() == "warm" else NO_BRAND_RATE
    if random.random() >= rate_table.get(plat, 0.0):
        return res

    nb = await _generate_reply_once(
        content=content, intent=intent, destination_country=destination_country,
        redis=redis, platform=platform, comment_count=comment_count,
        language=language, no_brand=True, promo=promo,
    )
    if isinstance(nb, _GenuineSkip) or nb is None:
        # 无品牌版没生成出来 -> 用带品牌那条。这帖子已经过了门槛, 是条真线索,
        # 不能因为"这次抽中不提品牌"就整个丢掉。
        log.info("no_brand_regen_failed_keep_brand", platform=plat, intent=intent)
        return res
    return nb


def content_hash(content: str, author: str | None = None, group_id: int | None = None) -> str:
    """生成帖子去重 hash (Hotfix 3.15: 改进版)
    
    改进点:
    - 用前 80 字符(标准化后) 计算 hash
    - 商家发的同样广告内容微变 → 前 80 字一致 → 同 hash
    - 删除 emoji/标点/空白避免微变干扰
    
    例:
    "Có ai như mình không? Sang đến nước bạn..." → 5 次出现，前 80 字一致 → 同 hash
    """
    # 1. 标准化：转小写 + 删除 emoji/标点/空白
    normalized = re.sub(r"[^\w\u4e00-\u9fff\u00c0-\u024fà-ỹ]+", "", content.lower().strip())
    # 2. 只取前 80 字
    key = normalized[:80]
    # 3. 加 group_id (同帖子在不同群组算不同 hash)
    raw = f"{group_id or 0}|{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ════════════════════════════════════════════════════════════
# 自发内容指纹 (2026-09-16) —— 防止把自己发的评论当帖子重新抓回来
# ════════════════════════════════════════════════════════════
#
# Threads 工作流会把评论当帖子抓。转型后我们在 Threads 的评论量涨了一个量级,
# 于是开始回收自己的输出: 35 小时里 50 条 raw_posts 逐字等于自己发过的评论,
# 8 条还真的回复了 —— 公开 thread 里自己夸自己, 是账号可信度问题。
#
# 不能靠"含不含 intlroam"判: 无品牌模式下我们的评论里没有品牌名。
# 所以直接拿发过的正文做指纹。前 80 个字符足够 —— 平台可能截断长评论的尾巴,
# 但开头是稳定的。

OWN_REPLY_TTL = 7 * 24 * 3600
_OWN_PREFIX_LEN = 80


def own_reply_fingerprint(text: str) -> str:
    """自发内容的指纹键。归一化空白 + 小写, 抗平台的排版差异。"""
    norm = " ".join((text or "").lower().split())[:_OWN_PREFIX_LEN]
    return "own_reply:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]


async def remember_own_reply(rds, text: str) -> None:
    """评论发出去之后记一笔。失败不该影响主流程。"""
    if not rds or not text or len(text.strip()) < 20:
        return
    try:
        await rds.set(own_reply_fingerprint(text), "1", ex=OWN_REPLY_TTL)
    except Exception as e:
        log.warning("own_reply_remember_failed", err=str(e))


async def is_own_reply(rds, text: str) -> bool:
    """这条"帖子"是不是我们自己发过的评论。读不到就放行, 不要卡住主流程。"""
    if not rds or not text or len(text.strip()) < 20:
        return False
    try:
        return bool(await rds.exists(own_reply_fingerprint(text)))
    except Exception:
        return False
