from datetime import datetime, timedelta, timezone
from typing import Optional
import json
import random
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.services.llm_client import call_llm
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)

# per-platform cooldown: tiktok 8h, threads 20h
# 2026-07-26: threads 12h -> 20h。12h 会出现同一天两帖 (07-23 就是 00:33 + 13:02),
# 频率过高。20h + 下面的当日 1 帖硬顶 = 每天最多 1 帖, 且不会卡在固定时刻。
# 2026-10-25 reddit 改每天 5 帖 + 2h 冷却 (用户要求提量)。下面 24h 的旧理由保留作参考。
# reddit 24h: 新号在 Reddit 发第二帖的风险远高于多发一帖的收益, 且我们只发
# 自己主页 (u/), 主页帖本来就没有时效压力。
COOLDOWN_HOURS = {"tiktok": 8, "threads": 20, "reddit": 2}
DEFAULT_COOLDOWN_HOURS = 8

# 每个自然日 (DB 时区 Asia/Hong_Kong) 的发帖上限。没列的平台只受 cooldown 约束。
DAILY_POST_CAP = {"threads": 1, "reddit": 5}

# 关闭发帖的平台 (只保留评论)。在这里关而不是在客户端关: 所有在跑的 APK 立刻生效,
# 客户端对非 ok 状态一律跳过, 也不会白调一次 LLM 生成。
# reddit: 2026-10-25 关闭 —— Reddit 2026.42 App 已取消"发到自己主页", 发版块帖有版规/封号风险,
#   用户决定只保留评论。
POSTING_DISABLED = {"reddit", "threads"}  # 2026-11-02: threads 也暂停 (用户决定)

# 帖子字数上限。threads 超过 ~300 会被折进 "more" (软限制, 后半段没人看而已)。
# tiktok 是**硬限制**: 文字帖输入框只收 250 字符, 多出来的被输入法直接吞掉, 发出去的是
# 一条断在半句话上的帖子 —— 而 TikTokPostWorkflow 点完发布就无条件上报 success,
# 所以 device_post_history 里显示 100% 成功, 从数据上完全看不出来。
# 2026-08-20: tiktok 340 -> 240 (留 10 字符余量给 emoji / 换行的计数差异)。实测改之前
# 10 天 27 条里 23 条超 250 (均值 277, 最长 340), 真因是 prompt 里那条写着"照抄它的长度"
# 的示例本身就有 276 字符 —— 示例是比任何"不要超过 N 字"都强的锚。
# 超了带实际字数返工一次, 第二次仍超走 _trim_to_limit 确定性截断 (见 generate_post)。
# reddit 正文 900: Reddit 没有平台硬限 (40k), 但主页上没人读长文;
# 900 ≈ 150 词, 够写"一条真有用的经验 + 一个诚实缺点"。标题另算 (见 REDDIT_TITLE_MAX)。
POST_MAX_CHARS = {"threads": 320, "tiktok": 240, "reddit": 900}

# Reddit 标题硬上限是 300 字符 (平台限制); 超了发布按钮直接不可用,
# 所以这里一定要在服务端截断, 不能指望模型守约。
REDDIT_TITLE_MAX = 300


_HASHTAG_PARA = re.compile(r"^\s*(?:#\S+\s*)+$")


def _cut_at_boundary(s: str, budget: int) -> str:
    """把一段切到 budget 以内, 优先切在句子边界, 退而求其次切在词边界。

    绝不断在词中间 —— 那正是 TikTok 输入框吃满 250 后干的事, 我们自己截断如果也断在
    半个词上就毫无意义了。
    """
    if len(s) <= budget:
        return s
    cut = s[:budget]
    end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if end > budget // 2:
        return cut[: end + 1].rstrip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip()


def _trim_to_limit(content: str, max_len: int) -> str:
    """两次返工还超长时的最后兜底 —— 我们自己按段落/句子砍。

    2026-08-20: 之前第二次仍超就照发, 对 threads 只是被折叠, 对 tiktok 是真丢内容:
    输入框收满 250 就不再收, 帖子发出去断在半句话上。宁可少一段, 不能断在半句话。

    保留优先级: 第一段(那条 tip) > 含 IntlRoam 那段 > 结尾的纯 hashtag 段 > 其余。
    所以先丢"既不是开头也不含品牌"的段落, 还超才去收缩最长的那一段。
    """
    if len(content) <= max_len:
        return content

    paras = [p.strip() for p in content.split("\n\n") if p.strip()]
    tail = paras.pop() if len(paras) > 1 and _HASHTAG_PARA.match(paras[-1]) else None

    def join(body: list[str]) -> str:
        return "\n\n".join(body + ([tail] if tail else []))

    # 1) 丢掉可丢的段落 (既不是第一段, 也不含品牌), 从后往前
    while len(paras) > 1 and len(join(paras)) > max_len:
        droppable = [j for j in range(1, len(paras)) if "intlroam" not in paras[j].lower()]
        if not droppable:
            break
        paras.pop(droppable[-1])

    # 2) 仍超 -> 收缩当前最长的那段。guard 只是防死循环, 正常 1-2 轮就收敛。
    for _ in range(8):
        over = len(join(paras)) - max_len
        if over <= 0:
            break
        k = max(range(len(paras)), key=lambda j: len(paras[j]))
        paras[k] = _cut_at_boundary(paras[k], max(len(paras[k]) - over, 24))
        if len(paras) > 1 and len(paras[k]) <= 24:
            paras.pop(k)

    return join(paras)


class GeneratePostReq(BaseModel):
    device_id: str
    platform: str = "tiktok"


class PostResult(BaseModel):
    device_id: str
    success: bool
    content: Optional[str] = None
    primary_destination: Optional[str] = None
    platform: str = "tiktok"
    # 客户端给的失败原因, 例: input_truncated_250_of_284 / composer_still_open_after_publish
    fail_reason: Optional[str] = None


# ─────────────── TikTok (W1) prompt — unchanged ───────────────
SYSTEM_INSTRUCTION = """Bạn là copywriter cho IntlRoam (thương hiệu eSIM du lịch quốc tế).
Viết post TikTok ngắn (text post trên background màu) để tăng nhận diện thương hiệu trên trang công khai.
Phong cách: QUẢNG CÁO TRỰC TIẾP (không cần tự nhiên).
LUÔN trả về JSON đúng format, KHÔNG có text ngoài JSON."""


def build_user_message(recent: list[str]) -> str:
    recent_str = ", ".join(recent) if recent else "(none)"
    return f"""Viết 1 post TikTok với:
- Độ dài: 80-150 ký tự
- Phải nhắc rõ tên "IntlRoam" 1 lần
- Chọn 1-2 điểm đến từ pool: japan, korea, taiwan, thailand, singapore, usa, europe, china
- 1-2 emoji cờ ở đầu
- 2-3 bullet với ✓
- 2-3 hashtag ở cuối: #destination_hashtag + #esim + #intlroam
- Xuống dòng để dễ đọc trên background màu

Tránh lặp điểm đến đã dùng 24h qua: {recent_str}

Output JSON:
{{"content":"<full text with \\n>","destinations":["japan"],"primary_destination":"japan"}}

Ví dụ:
{{"content":"🇯🇵 Đi Nhật siêu rẻ với IntlRoam eSIM\\n✓ Active liền khi xuống sân bay\\n✓ Vào TikTok/Zalo mượt mà\\n✓ Hỗ trợ tiếng Việt 24/7\\n\\n#dulichnhatban #esim #intlroam","destinations":["japan"],"primary_destination":"japan"}}"""


# ─────────────── Threads prompt — no hashtags, multi-destination long-tail keywords ───────────────
THREADS_SYSTEM_INSTRUCTION = """Bạn là một người đi du lịch thật, chia sẻ trên Threads cá nhân.
Viết 1 bài đăng Threads bằng tiếng Việt: kinh nghiệm thật, KHÔNG phải quảng cáo.
Bạn có dùng IntlRoam (eSIM du lịch quốc tế) và nhắc tới nó đúng 1 lần, như một chi tiết
trong câu chuyện — giống cách bạn nhắc tên một khách sạn hay hãng bay đã dùng.
LUÔN trả về JSON đúng format, KHÔNG có text ngoài JSON."""


def build_threads_user_message(recent: list[str]) -> str:
    recent_str = ", ".join(recent) if recent else "(none)"
    return f"""Viết 1 bài đăng Threads với:
- Độ dài: 120-250 ký tự, tiếng Việt
- Bài phải CÓ ÍCH kể cả khi bỏ hết phần thương hiệu: 1 mẹo cụ thể, 1 kinh nghiệm thật,
  hoặc 1 lỗi bạn từng mắc. Mở đầu bằng chính cái mẹo đó, không mở đầu bằng tên thương hiệu.
- Nhắc tên "IntlRoam" đúng 1 lần, ở nửa sau bài, như chi tiết phụ chứ không phải tiêu đề
- Lồng tự nhiên 1-2 từ khóa đuôi dài về điểm đến (để dễ được tìm thấy khi người ta search),
  ví dụ pool: "eSIM đi Nhật", "sim du lịch Hàn Quốc", "eSIM Đài Loan", "mua sim đi Thái Lan",
  "internet đi Singapore", "eSIM châu Âu", "sim du lịch Mỹ", "eSIM Trung Quốc"
  -> viết lồng trong câu, KHÔNG liệt kê khô khan, KHÔNG nhồi 3-4 cái một lúc
- Nêu TỐI ĐA 1 điểm mạnh (chọn cái hợp với câu chuyện):
  mạng nội địa (không phải roaming nên sóng khỏe & nhanh) / kết nối ổn định / giá rẻ / hỗ trợ 24/7
- Kèm 1 nhược điểm thật (eSIM du lịch chỉ có data, vẫn phải giữ sim thường để nhận SMS/OTP;
  phải cài trước khi bay vì quét QR cần wifi) — đây là thứ làm bài đáng tin
- TUYỆT ĐỐI KHÔNG mời inbox / nhắn tin / "ai cần thì ib mình" / để lại liên hệ.
  Người thật kể chuyện thì không nhận inbox. Kết bài bằng 1 câu hỏi cho mọi người là được.
- KHÔNG dùng từ ngữ bán hàng: "siêu rẻ", "tốt nhất", "nên mua", "cam kết", "uy tín"
- TUYỆT ĐỐI KHÔNG dùng hashtag (không có ký tự #), không để link
- Có thể xuống dòng cho dễ đọc

Tránh lặp điểm đến chính đã dùng 24h qua: {recent_str}

Output JSON (KHÔNG có text gì khác):
{{"content":"<full text with \\n, KHÔNG hashtag>","destinations":["japan","korea"],"primary_destination":"japan"}}

Ví dụ:
{{"content":"Bài học nhớ đời: cài eSIM đi Nhật ở sân bay mới thấy quét QR phải có wifi, loay hoay mất 20 phút. Giờ mình cài sẵn ở nhà, xuống máy bay là có mạng luôn, chuyến vừa rồi dùng IntlRoam hết hơn 100k cho một tuần. Cái dở là chỉ có data nên sim thường vẫn phải giữ để nhận OTP. Mọi người hay cài trước hay ra sân bay mới mua?","destinations":["japan"],"primary_destination":"japan"}}"""


# ─────────────── English prompts (2026-07-14) ───────────────
# 设备的抽词语言设成 en (devices.languages) 时用这套。默认仍是越南语。

EN_SYSTEM_INSTRUCTION = """You are a real traveller sharing on your own TikTok profile.
Write like a person posting, not like a brand running an ad. Honest tone, contractions,
no hype. You use IntlRoam and mention it once, as a detail in the story.

The post must be worth reading on its own: a specific tip, an honest experience, or a
mistake you made. Include one honest caveat (e.g. travel eSIMs are data only, so you keep
your normal sim in for calls and 2FA texts) — that is what makes it credible.

LENGTH: TikTok's text-post box stops accepting characters at 250 — anything past that is
silently swallowed, so a long draft is published cut off mid-word. Every post you write is
about 32 words, UNDER 200 CHARACTERS, in one-sentence paragraphs. It also renders large on
a coloured background, so a wall of text would not work here anyway. One thing, said well,
in short sentences.
ALWAYS return valid JSON, with no text outside the JSON."""


# 内容类型轮换 —— 单一模板发久了会被平台和真人同时识别成机器人
EN_POST_ANGLES = [
    "a concrete travel tip about staying connected abroad that most people get wrong",
    "an honest mini-review of using a travel eSIM, including one real downside",
    "a mistake you personally made on a trip and what you do differently now",
    "a short 'things I wish I knew before flying to X' list",
    "a myth about roaming or eSIMs that you want to correct",
    "a quick cost comparison between carrier roaming and an eSIM, with real numbers",
]


def build_user_message_en(recent: list[str]) -> str:
    """TikTok 个人帖 —— 与 Facebook 个人主页 / Threads 同一套框架 (2026-07-26)。

    角度池 = posting.py 的旅行环节角度 + 本文件的内容形态角度 (13 个轮换);
    卖点口径共用 posting.py 的 EN_FACT_BANK; 目的地由服务端挑 (避开 24h 内用过的)。
    与 Threads 的差别只有两处: TikTok 要 hashtag, 且严禁点名其它社交 app。
    """
    import random
    from app.api.v2.posting import EN_FACT_BANK, EN_PERSONAL_ANGLES

    recent_str = ", ".join(recent) if recent else "(none)"
    angle = random.choice(list(EN_PERSONAL_ANGLES) + EN_POST_ANGLES)
    destination = _pick_destination(recent)

    return f"""Write ONE TikTok text post (renders large on a coloured background):
a genuinely useful travel tip about {angle}, set in {destination}.

FRAME — read carefully, this is the whole point (same rules as your Facebook profile
and Threads posts):
- This is a helpful travel TIP, not a trip report. Do NOT open with "Just got back
  from...", "Just landed in...", "Wish I'd known..." or any "I returned from my trip"
  framing — posting a different-country trip report every day is an obvious bot pattern
  and the single thing to avoid here.
- Lead with the tip itself. A viewer should learn something genuinely useful about
  {destination} even if they completely ignore the brand.
- This activity needs mobile data (you have to be online to do it), so mention — once, in
  passing — that you used an IntlRoam eSIM to stay connected for it. It's a detail, not
  the subject of the post.

{EN_FACT_BANK}

FACTS TO STAY CONSISTENT WITH (do not contradict these):
- You INSTALL the eSIM before you fly (scanning the QR needs wifi). It comes ONLINE when
  you land. Never tell people to skip installing before departure.
- Travel eSIMs are DATA ONLY. You keep your normal sim in for calls and 2FA texts.
- Airport sim counters are poor value.
- For multi-country trips, one regional plan beats one plan per country.
- VPN is allowed (policy changed 2026-09-15): plans include a free built-in VPN, and on
  our roaming network the usual apps generally just work, so there is often nothing
  extra to install. Say what we provide; never write a how-to for getting around a
  block, and do not comment on any country's network policy.

Rules:
- LENGTH IS A HARD CONSTRAINT, and it is not a style preference: the TikTok composer
  refuses every character past 250, so a longer draft goes out truncated mid-word and the
  post is wasted. The whole post is 140-200 characters / 26-36 words, hashtags and line
  breaks included. Every paragraph is ONE sentence — none of them gets a second one.
  Match the example below: if your draft is longer than it, you have written too much.
  Cut the scene-setting first; the tip and the caveat never go.
- Name "IntlRoam" EXACTLY ONCE, in the second half of the post. Required — this is your
  own profile. But as a passing detail, never as a headline or a pitch.
- Use AT MOST ONE selling point from the fact bank, and only if the angle needs it.
- Sound like a person, not a brand. Contractions, plain words, no corporate tone.
- Give one CONCRETE, checkable fact (a price, a number, a specific gotcha).
  Vague enthusiasm is worthless.
- Include ONE honest limitation or caveat. Real reviews have them.
  (e.g. "data only, so keep your normal sim in for texts")
- At most 1 emoji, and only if it earns its place.
- 2-3 hashtags at the end: one destination hashtag + one topic hashtag
  (#traveltips / #esim / #solotravel). Never #ad, never #sponsored.

BANNED (these read as ads and get suppressed):
- Bullet lists of features with check marks
- "Game changer", "must-have", "cheapest", "best", "you NEED this", "check it out",
  "worth a look", "highly recommend", "trust me"
- Stacked emojis, ALL CAPS, exclamation spam
- Any call to action at all: "DM me", "link in bio", "comment below", "get one"
- Recommending ANY other product or service (no apps, no gear, no other brands) —
  the only product named in the post is IntlRoam
- 🚫 NAMING ANY OTHER SOCIAL OR MESSAGING APP. Never write WhatsApp, Instagram,
  Facebook, Messenger, Telegram, WeChat, LINE, KakaoTalk, Viber, Snapchat, X or Twitter.
  TikTok suppresses posts that name rival apps. Say "your usual apps" / "everything you
  normally use" instead.

The destination for this post is fixed: {destination}. Do not write about anywhere else.
(Already used in the last 24h, kept out of the pool for you: {recent_str})

Output JSON — primary_destination MUST be {destination} in lowercase:
{{"content":"<full text with \\n>","destinations":["<destination>"],"primary_destination":"<destination>"}}

Good example — copy its LENGTH and SHAPE, never its wording. It is 191 characters and
36 words, and that is the CEILING, not the average: your post must not be longer than
this one. It is deliberately set in a destination you will never be assigned
(angle = attraction tickets on the app):
{{"content":"Book Blue Lagoon slots on the app before you land in Iceland.\\n\\nNeeds data — I set up an IntlRoam eSIM the night before, $4 for the week.\\n\\nData only, normal sim stays in.\\n\\n#iceland #traveltips","destinations":["iceland"],"primary_destination":"iceland"}}

Your post must be about {destination}, in your own words. Reusing the example's sentences
is an automatic fail."""


EN_THREADS_SYSTEM_INSTRUCTION = """You are a real traveller sharing on your own Threads profile.
Write like a person posting a status, not like a brand running an ad. Honest tone,
contractions, no hype. You use IntlRoam and mention it once, as a detail in the story.

The post must be worth reading on its own: a specific tip, an honest experience, or a
mistake you made. Include one honest caveat (e.g. travel eSIMs are data only, so you keep
your normal sim in for calls and 2FA texts) — that is what makes it credible.
Threads also rewards conversational, slightly imperfect writing: talk to a friend.

LENGTH: every post you write is UNDER 300 CHARACTERS — about 45 words, four short
sentences. Threads folds anything longer behind a "more" link. Say one thing well.
ALWAYS return valid JSON, with no text outside the JSON."""


# ═══════════════════════════════════════════════════════════════════
# 招商帖 (PIVOT 2026-09-15) —— 主动发帖转向找代理分销商
# ═══════════════════════════════════════════════════════════════════
#
# 掷骰决定这一条发消费者帖还是招商帖。不全量切换的原因见文件头注释:
# 账号要靠消费者内容维持"真人"外观。Threads 每天只有 1 条额度, 给到一半;
# TikTok 冷却 8h (一天约 2-3 条), 给 40% —— 每天仍有消费者帖打底。
B2B_POST_RATE = {"tiktok": 0.4, "threads": 0.5}

# 招商帖的市场锚点。消费者帖按"目的地"轮换, 招商帖按"哪个市场的同行"轮换 ——
# 落库的 primary_destination 仍然有值, 24h 避重逻辑照常工作。
B2B_MARKETS = [
    "oman", "the gulf", "vietnam", "thailand", "the philippines",
    "indonesia", "malaysia", "japan", "south korea", "turkey",
]

# 招商帖的角度池。单一模板发久了会被平台和真人同时认成机器人。
B2B_POST_ANGLES = [
    "why holding physical SIM stock stopped making sense for small agencies",
    "the moment you realise your clients are asking you about data every single trip",
    "how a tour operator adds connectivity to a package without touching inventory",
    "what margin on travel data actually looks like when you set your own price",
    "why a built-in VPN matters for clients heading somewhere calls get patchy",
    "the difference between a cheap roaming route and a premium local carrier",
    "what to ask a supplier before you resell their eSIM",
    "how instant provisioning changes what you can promise a client last-minute",
]

_B2B_FRAME = """FRAME — this post talks to PEOPLE WHO SELL TO TRAVELLERS, not to travellers.
Your reader runs a travel agency, guides tours, or sells SIM cards / phones in
{market}. You are a peer in the trade, not a brand doing a campaign.

- Open on THEIR problem or THEIR business, never on us.
- Name at most TWO of these, whichever fit the angle. Rotate across posts:
  wholesale pricing they mark up themselves / a free built-in VPN on the plan /
  premium local carriers rather than the cheapest roaming route / no stock and no
  upfront / instant provisioning, even last-minute / 170+ countries on one account.
  (VPN is fine to name — say what we provide, never how to get around a block.)
- NO public prices, percentages or margin numbers. Public numbers invite undercutting
  and get the post reported as an ad.
- ONE soft invite at the end, and it must point to the COMMENTS, not to private
  messages: "comment if you want the trade sheet" reads as normal trade chatter,
  while a DM push reads as spam and gets suppressed.
- Never write "reseller program", "affiliate", "partnership opportunity",
  "business opportunity", "earn passive income" — those phrases are spam markers.
- Never promise earnings or exclusivity.

BANNED (same as always — these get the post suppressed):
- Bullet lists with check marks, stacked emojis, ALL CAPS, exclamation spam
- "Game changer", "must-have", "cheapest", "best", "huge margins", "trust me"
- Naming any other brand or product"""

_B2B_APP_RULE = """- NAMING ANY OTHER SOCIAL OR MESSAGING APP. Never write WhatsApp, Instagram,
  Facebook, Messenger, Telegram, WeChat, LINE, KakaoTalk, Viber, Snapchat, X or
  Twitter. TikTok suppresses posts that name rival apps."""


EN_B2B_SYSTEM_INSTRUCTION = """You work with a travel eSIM supplier (IntlRoam) and post on
your own TikTok profile about the trade. You are talking to travel agencies, tour
operators, guides and phone/SIM shops — people who sell to travellers.

Write like a person in the business, not like a brand running an ad. Plain words,
contractions, no hype. Naming IntlRoam once is fine and expected: a trade contact needs
to know who they would be dealing with.

LENGTH: TikTok's text-post box stops accepting characters at 250 — anything past that is
silently swallowed, so a long draft publishes cut off mid-word. Every post is about
32 words, UNDER 200 CHARACTERS, in one-sentence paragraphs.
ALWAYS return valid JSON, with no text outside the JSON."""


EN_B2B_THREADS_SYSTEM_INSTRUCTION = """You work with a travel eSIM supplier (IntlRoam) and
post on your own Threads profile about the trade. You are talking to travel agencies, tour
operators, guides and phone/SIM shops — people who sell to travellers.

Write like a person in the business, not like a brand running an ad. Plain words,
contractions, no hype. Threads rewards conversational, slightly imperfect writing.
Naming IntlRoam once is fine: a trade contact needs to know who they would deal with.

LENGTH: every post is UNDER 300 CHARACTERS — about 45 words, four short sentences.
Threads folds anything longer behind a "more" link.
ALWAYS return valid JSON, with no text outside the JSON."""


def build_b2b_user_message_en(recent: list[str], platform: str) -> str:
    """招商帖的 user message。JSON 契约与消费者帖完全一致 (下游落库不用改)。"""
    import random

    recent_low = {r.strip().lower() for r in recent}
    pool = [m for m in B2B_MARKETS if m not in recent_low]
    market = random.choice(pool or B2B_MARKETS)
    angle = random.choice(B2B_POST_ANGLES)
    recent_str = ", ".join(recent) if recent else "(none)"

    if platform == "tiktok":
        head = ("Write ONE TikTok text post (renders large on a coloured background) "
                "aimed at people who sell travel in {m}: {a}.")
        # TIKTOK 额外收紧 (2026-09-15): 240 是硬限制, 装不下"两条优势 + 3 个
        # hashtag + 邀约"。实测不收紧时首次命中只有 1/5, 每条都要多烧一次返工调用。
        extra = ("\n- ⚠️ TikTok only: name EXACTLY ONE advantage, not two. There is no "
                 "room for more.\n"
                 "\n- Exactly 2 hashtags at the end, trade-facing: one of "
                 "#travelagent / #touroperator plus one market hashtag. "
                 "Never #ad, never #sponsored.\n"
                 + _B2B_APP_RULE)
    else:
        head = "Write ONE Threads post aimed at people who sell travel in {m}: {a}."
        extra = "\n- No hashtags at all on Threads."

    # LENGTH CALIBRATION —— 只有 TikTok 需要 (240 是硬限制, 超了被静默截断)。
    # 示例锚在 morocco: 它不在 B2B_MARKETS 里, 永远不会被派到, 所以即便模型
    # 照抄用词也套不进任何一条真帖 —— 这是既往"prompt 逐字示例"那个坑的解法。
    if platform == "tiktok":
        length_cal = """

LENGTH CALIBRATION — copy this example's LENGTH and SHAPE, never its wording.
It is 188 characters. That is the CEILING, not the average. It is deliberately set in
a market you will never be assigned:
{"content":"Selling Morocco tours? Your clients ask about data every time.\\n\\nYou can add an eSIM with no stock, set your own price.\\n\\nComment for the trade sheet.\\n\\n#travelagent #esim #morocco","destinations":["morocco"],"primary_destination":"morocco"}

Reusing that example's sentences is an automatic fail. Count your characters before
you answer: over 240 and the post publishes cut off mid-word."""
    else:
        length_cal = ""

    return (head.format(m=market, a=angle) + "\n\n"
            + _B2B_FRAME.format(market=market) + extra + f"""

(Markets already used in the last 24h, kept out of the pool for you: {recent_str})

Output JSON — primary_destination MUST be "{market}" in lowercase:
{{{{"content":"<full text with \\n>","destinations":["{market}"],"primary_destination":"{market}"}}}}

Write it in your own words, about {market}. Do not reuse any phrasing you have seen in
these instructions — every post must read as if a different person wrote it.""" + length_cal)


def _pick_destination(recent: list[str]) -> str:
    """服务端挑目的地, 避开 24h 内用过的 (原来交给模型自己避重, 它并不真的避)。"""
    import random
    from app.api.v2.posting import EN_PERSONAL_DESTINATIONS

    recent_low = {r.strip().lower() for r in recent}
    pool = [d for d in EN_PERSONAL_DESTINATIONS if d.lower().replace("the ", "") not in recent_low]
    return random.choice(pool or EN_PERSONAL_DESTINATIONS)


def build_threads_user_message_en(recent: list[str]) -> str:
    """Threads 个人帖 —— 与 Facebook 个人主页发帖同一套框架 (2026-07-26)。

    共用 posting.py 的旅行环节角度 (EN_PERSONAL_ANGLES) 和卖点口径 (EN_FACT_BANK):
    两边人设/事实一致, 改 posting.py 的事实库就是改所有英语发帖。
    目的地由服务端挑 (避开 24h 内用过的), 不再交给模型自己选。
    """
    import random
    from app.api.v2.posting import EN_FACT_BANK, EN_PERSONAL_ANGLES

    recent_str = ", ".join(recent) if recent else "(none)"
    angle = random.choice(EN_PERSONAL_ANGLES)
    destination = _pick_destination(recent)

    return f"""Write ONE Threads post in English: a genuinely useful travel tip about
{angle}, set in {destination}.

FRAME — read carefully, this is the whole point (same rules as your Facebook profile posts):
- This is a helpful travel TIP, not a trip report. Do NOT open with "Just got back from...",
  "Just landed in...", "Wish I'd known...", "Mildly obsessed with..." or any "I returned from
  my trip" framing — posting a different-country trip report every day is an obvious bot
  pattern and the single thing to avoid here.
- Lead with the tip itself. A reader should learn something genuinely useful about
  {destination} even if they completely ignore the brand.
- This activity needs mobile data (you have to be online to do it), so mention — once, in
  passing — that you used an IntlRoam eSIM to stay connected for it. It's a detail, not the
  subject of the post.

{EN_FACT_BANK}

FACTS TO STAY CONSISTENT WITH (do not contradict these):
- You INSTALL the eSIM before you fly (scanning the QR needs wifi). It comes ONLINE when
  you land. Never tell people to skip installing before departure.
- Travel eSIMs are DATA ONLY. You keep your normal sim in for calls and 2FA texts.
- Airport sim counters are poor value.
- For multi-country trips, one regional plan beats one plan per country.
- VPN is allowed (policy changed 2026-09-15): plans include a free built-in VPN, and on
  our roaming network the usual apps generally just work, so there is often nothing
  extra to install. Say what we provide; never write a how-to for getting around a
  block, and do not comment on any country's network policy.

Rules:
- LENGTH IS A HARD CONSTRAINT: 150-280 characters, absolute ceiling 300. That is roughly
  40-55 words — SHORTER than a Facebook status. Count the characters before you answer;
  if the draft is over, delete the scene-setting sentence, never the tip or the caveat.
  Threads hides anything past ~300 characters behind a "more" link, so a long post is a
  post nobody reads.
- The post has to be worth reading with the brand deleted. Lead with the tip, the
  mistake or the observation — never with the brand, never with a product claim.
- Work in ONE search-style phrase for {destination} naturally, so the post surfaces when
  people search (e.g. "esim for japan", "korea travel sim", "sim card for thailand",
  "europe esim"). Weave it into a sentence — never list phrases, never stack two or more
  destinations: keyword stuffing is the clearest bot tell on Threads.
- Name "IntlRoam" EXACTLY ONCE, in the second half of the post. Required, but as a
  passing detail — the thing you happened to use, not a recommendation with reasons.
- Use AT MOST ONE selling point from the fact bank, and only if the angle needs it.
- Give one concrete fact (a real price, a specific gotcha, a number).
- Include one honest downside. This is what makes it readable rather than promotional.
- End with a genuine question or an opening for replies — Threads is a conversation.
- ABSOLUTELY NO hashtags (no # character anywhere).
- No calls to action of any kind: no "DM me", no "message me", no "let me know if you
  want the link", no links, no handles. You are a traveller telling a story, not a shop.
- Banned sales vocabulary: "check it out", "worth a look", "highly recommend", "you
  should get", "trust me", "great deal", "game changer", "must-have".
- Do NOT recommend any other product or service (no VPNs, no apps, no other brands).

The destination for this post is fixed: {destination}. Do not write about anywhere else.
(Already used in the last 24h, kept out of the pool for you: {recent_str})

Output JSON (nothing else) — primary_destination MUST be {destination} in lowercase:
{{"content":"<full text, NO hashtags>","destinations":["<destination>"],"primary_destination":"<destination>"}}

Good example — copy its LENGTH and SHAPE, never its wording. It is deliberately set in a
destination you will never be assigned (angle = getting from the airport into the city):
{{"content":"Land at Keflavik late and the bus options thin out fast, so book the transfer before you clear customs. Which needs you online already — I set up an esim for iceland before flying, four dollars for the week on IntlRoam. Data only though, normal sim stays in for bank texts. Anyone got a better late option?","destinations":["iceland"],"primary_destination":"iceland"}}

Your post must be about {destination}, in your own words. Reusing the example's sentences
is an automatic fail."""

EN_REDDIT_SYSTEM_INSTRUCTION = """You are a real traveller writing a post on your own
Reddit profile (u/yourname), not in a subreddit and not on behalf of any brand.

Reddit is read by people who downvote and publicly call out anything that reads like
marketing. A post survives here only when it teaches the reader something concrete —
a number, a mistake, a step that is not obvious. You mention IntlRoam once, the way you
would name the airline or the hostel that worked out; it is a detail inside the story,
never the subject.

A Reddit post has a TITLE and a BODY. The title carries the whole post: it states the
useful thing, it is not clickbait, it has no ALL CAPS, no emoji and no brand name.

NEVER include a link of any kind. Automod strips promotional links and it costs the
account.

ALWAYS return valid JSON, with no text outside the JSON."""


def build_reddit_user_message_en(recent: list[str]) -> str:
    """Reddit 主页帖 —— 和 Threads/FB 个人帖共用事实库与角度池 (见 build_threads_user_message_en)。

    唯一的结构差别: Reddit 有标题, 而且标题比正文重要 —— 列表页上只看得到标题。
    """
    import random
    from app.api.v2.posting import EN_FACT_BANK, EN_PERSONAL_ANGLES

    recent_str = ", ".join(recent) if recent else "(none)"
    angle = random.choice(EN_PERSONAL_ANGLES)
    destination = _pick_destination(recent)

    return f"""Write ONE Reddit profile post in English: a genuinely useful travel tip about
{angle}, set in {destination}.

FRAME — this is the whole point:
- It is a TIP or a lesson learned, not a trip report. Do NOT open with "Just got back
  from...", "Just landed in..." or "Wish I had known...". Posting a different-country
  trip report every day is an obvious bot pattern.
- A reader must learn something useful about {destination} even if the brand name is
  deleted from the post.
- The activity needs mobile data, so mention — once, in passing — that you used an
  IntlRoam eSIM to stay online for it.

{EN_FACT_BANK}

FACTS TO STAY CONSISTENT WITH (do not contradict these):
- You INSTALL the eSIM before you fly (scanning the QR needs wifi). It comes ONLINE when
  you land.
- Travel eSIMs are DATA ONLY. You keep your normal sim in for calls and 2FA texts.
- Airport sim counters are poor value.
- For multi-country trips, one regional plan beats one plan per country.
- VPN is allowed: plans include a free built-in VPN, and on our roaming network the usual
  apps generally just work. Never write a how-to for getting around a block, and do not
  comment on any country's network policy.

TITLE RULES:
- 40-110 characters, a plain statement of the useful thing, lower-case sentence style.
- No brand name, no emoji, no ALL CAPS, no clickbait ("You won't believe...").
- It must make sense on its own in a list of titles.

BODY RULES:
- 400-800 characters, two or three short paragraphs separated by a blank line.
- Lead with the tip itself. One concrete fact (a real price, a specific gotcha, a number).
- Include one honest downside — that is what makes it readable rather than promotional.
- Name "IntlRoam" EXACTLY ONCE, in the second half. A passing detail, not a recommendation
  with reasons. Use AT MOST ONE selling point from the fact bank.
- End with a genuine question so people can answer.
- NO links, NO handles, NO hashtags, NO calls to action ("DM me", "let me know if you
  want the link"). Banned sales vocabulary: "check it out", "worth a look", "highly
  recommend", "you should get", "trust me", "great deal", "game changer", "must-have".
- Do NOT recommend any other product or service.

The destination is fixed: {destination}. Do not write about anywhere else.
(Already used in the last 24h, kept out of the pool for you: {recent_str})

Output JSON (nothing else) — primary_destination MUST be {destination} in lowercase:
{{"title":"<plain useful title>","content":"<body>","primary_destination":"<destination>"}}

Good example — copy its SHAPE and LENGTH, never its wording. It is deliberately set in a
destination you will never be assigned (angle = getting from the airport into the city):
{{"title":"if you land at keflavik after 10pm, book the transfer before you fly","content":"The late bus options thin out fast and the counters shut, so the cheap way in disappears exactly when you need it. I booked the transfer the morning before flying and it was about half what the taxi rank wanted at 1am.\n\nThat does mean being online the second you land, which caught me out the first time. I had an IntlRoam data plan set up before the flight, four dollars for the week, and it connected at the gate. Worth knowing it is data only though, so my normal sim stayed in for bank texts.\n\nAnyone found a better late-night option that does not involve waiting until morning?","primary_destination":"iceland"}}

Your post must be about {destination}, in your own words. Reusing the example's sentences
is an automatic fail."""


async def resolve_device_language(db, device_id: str) -> str:
    """设备的抽词语言决定发帖语言。devices.languages 为空 -> 越南语 (原行为)。"""
    from sqlalchemy import select
    from app.models.device import Device
    langs = (await db.execute(
        select(Device.languages).where(Device.device_id == device_id)
    )).scalar_one_or_none()
    # 显式配了就按配的来 (同时配了 vi+en 时以 vi 优先, 保持原行为)。
    if langs:
        if "vi" in langs:
            return "vi"
        if "en" in langs:
            return "en"
    # 空 = 用平台默认。2026-08-11: 从 "vi" 改成 "en", 对齐
    # services/keywords.py 的 PLATFORM_DEFAULT_LANGUAGES (facebook/tiktok/threads 全是
    # ["en"], 2026-07-14 转英语市场时改的)。当时漏了发帖这条路, 结果后注册的机器
    # (languages=[]) 搜英语词、进英语群, 却发越南语帖子。
    return "en"


def _extract_llm_text(result) -> str:
    """LlmResult 可能有多种 attr 名, 兜底处理"""
    for attr in ("text", "content", "reply", "output", "response"):
        v = getattr(result, attr, None)
        if v and isinstance(v, str):
            return v
    if hasattr(result, "__dict__"):
        for k, v in result.__dict__.items():
            if isinstance(v, str) and len(v) > 20:
                return v
    return str(result)


@router.post("/generate_post")
async def generate_post(req: GeneratePostReq, db: AsyncSession = Depends(get_db)):
    platform = req.platform or "tiktok"
    if platform in POSTING_DISABLED:
        return {"status": "disabled", "remaining_sec": 86400, "content": None}
    cooldown_hours = COOLDOWN_HOURS.get(platform, DEFAULT_COOLDOWN_HOURS)

    # 1. cooldown (per platform)
    row = (await db.execute(text("""
        SELECT MAX(posted_at) AS last_at FROM device_post_history
        WHERE device_id = :did AND success = TRUE AND platform = :plat
    """), {"did": req.device_id, "plat": platform})).first()

    if row and row.last_at:
        elapsed = datetime.now(timezone.utc) - row.last_at
        if elapsed < timedelta(hours=cooldown_hours):
            remaining = int((timedelta(hours=cooldown_hours) - elapsed).total_seconds())
            return {"status": "cooldown", "remaining_sec": remaining, "content": None}

    # 1b. 当日上限 (threads = 1 帖/天)。cooldown 只管"距上一帖多久", 跨天时挡不住第二帖。
    cap = DAILY_POST_CAP.get(platform)
    if cap:
        today_row = (await db.execute(text("""
            SELECT COUNT(*) AS n,
                   EXTRACT(EPOCH FROM (date_trunc('day', now()) + INTERVAL '1 day' - now()))::int AS to_midnight
            FROM device_post_history
            WHERE device_id = :did AND platform = :plat AND success = TRUE
              AND posted_at >= date_trunc('day', now())
        """), {"did": req.device_id, "plat": platform})).first()
        if today_row and today_row.n >= cap:
            return {
                "status": "daily_limit",
                "remaining_sec": int(today_row.to_midnight or 0),
                "content": None,
            }

    # 2. Recent destinations (24h, per platform) for avoid-repeat
    recent_rows = (await db.execute(text("""
        SELECT DISTINCT primary_destination FROM device_post_history
        WHERE device_id = :did AND platform = :plat AND success = TRUE
          AND posted_at > NOW() - INTERVAL '24 hours'
    """), {"did": req.device_id, "plat": platform})).all()
    recent_dest = [r.primary_destination for r in recent_rows if r.primary_destination]

    # 3. Call LLM (prompt per platform + language)
    post_lang = await resolve_device_language(db, req.device_id)

    # PIVOT 2026-09-15: 掷骰决定这条发招商帖还是消费者帖。
    # 只对英语设备生效 —— 越南语那套 prompt 没有招商版本, 硬套会发出中英混杂的帖子。
    is_b2b = (post_lang == "en"
              and random.random() < B2B_POST_RATE.get(platform, 0.0))

    if is_b2b:
        sys_inst = (EN_B2B_THREADS_SYSTEM_INSTRUCTION if platform == "threads"
                    else EN_B2B_SYSTEM_INSTRUCTION)
        user_msg = build_b2b_user_message_en(recent_dest, platform)
    elif platform == "reddit":
        # Reddit 只有英语 prompt: 主页帖发给英语市场, 越南语那套没有 Reddit 版本,
        # 硬套会发出语言和标题风格都不对的帖子 (同 b2b 的处理)。
        sys_inst = EN_REDDIT_SYSTEM_INSTRUCTION
        user_msg = build_reddit_user_message_en(recent_dest)
    elif platform == "threads":
        if post_lang == "en":
            sys_inst = EN_THREADS_SYSTEM_INSTRUCTION
            user_msg = build_threads_user_message_en(recent_dest)
        else:
            sys_inst = THREADS_SYSTEM_INSTRUCTION
            user_msg = build_threads_user_message(recent_dest)
    else:
        if post_lang == "en":
            sys_inst = EN_SYSTEM_INSTRUCTION
            user_msg = build_user_message_en(recent_dest)
        else:
            sys_inst = SYSTEM_INSTRUCTION
            user_msg = build_user_message(recent_dest)
    log.info("post_audience", platform=platform, device_id=req.device_id,
             audience="b2b" if is_b2b else "consumer")

    # 4. Call + parse, 超长再要一次 (2026-07-26)
    # Threads 超过 ~300 字符会被折进 "more", 后半段没人看。模型对"字数上限"这类软约束
    # 经常超标 (实测 417-469), 所以给一次带实际字数的返工机会, 第二次仍超就照发。
    max_len = POST_MAX_CHARS.get(platform)
    content = primary_dest = None
    title = None
    for attempt in (1, 2):
        msg = user_msg
        if attempt == 2:
            msg = (
                f"{user_msg}\n\nYOUR PREVIOUS DRAFT WAS {len(content)} CHARACTERS — TOO LONG.\n"
                f"Rewrite it under {max_len} characters. Cut the scene-setting, keep the tip, "
                f"the one concrete detail, the honest caveat and the closing question."
            )
        try:
            result = await call_llm(
                system_instruction=sys_inst,
                user_message=msg,
                temperature=0.9,
                json_mode=True,
            )
        except Exception as e:
            raise HTTPException(500, f"LLM call failed: {e}")

        llm_text = _extract_llm_text(result)

        try:
            m = re.search(r'\{.*\}', llm_text, re.DOTALL)
            if not m:
                raise ValueError(f"no JSON found in: {llm_text[:200]}")
            parsed = json.loads(m.group(0))
            content = parsed.get("content", "").strip()
            primary_dest = parsed.get("primary_destination", "unknown")
            if platform == "reddit":
                title = (parsed.get("title") or "").strip()
                if len(title) < 10:
                    raise ValueError(f"reddit title too short: {title!r}")
            if platform == "threads":
                # belt-and-suspenders: strip any hashtag the model slips in
                content = re.sub(r'\s*#\S+', '', content).strip()
            if not content or len(content) < 30:
                raise ValueError(f"content too short: {len(content)} chars")
            if "IntlRoam" not in content and "intlroam" not in content.lower():
                raise ValueError(f"no IntlRoam mention in content: {content[:100]}")
        except Exception as e:
            raise HTTPException(500, f"LLM parse error: {e} | raw: {llm_text[:200]}")

        if not max_len or len(content) <= max_len:
            break

    # 返工两次仍超 -> 确定性截断。超长内容绝不能送到手机上 (见 _trim_to_limit)。
    if max_len and len(content) > max_len:
        before = len(content)
        content = _trim_to_limit(content, max_len)
        log.warning(
            "post_trimmed_to_limit",
            platform=platform, device_id=req.device_id,
            before=before, after=len(content), limit=max_len,
        )

    # 标题由平台硬限制 (300), 模型守不住就我们截 —— 超限时 Reddit 的发布按钮直接是灰的,
    # 手机端只会表现成"发帖静默失败"。
    if title and len(title) > REDDIT_TITLE_MAX:
        title = _cut_at_boundary(title, REDDIT_TITLE_MAX)

    resp = {
        "status": "ok",
        "content": content,
        "primary_destination": primary_dest,
        "raw_length": len(content),
    }
    if title:
        resp["title"] = title
    return resp


@router.post("/report_post_result")
async def report_post_result(req: PostResult, db: AsyncSession = Depends(get_db)):
    """2026-08-20: 失败也要落库。

    原来只在 success 时 INSERT, 于是 device_post_history 里永远 100% 成功 —— 加上客户端
    点完发布按钮就无条件上报 success, "TikTok 超 250 字符被输入框静默截断"这个 bug
    在数据上完全隐形, 两周里 23/27 条帖子发的是断句残文都没人看得见。

    失败行不会影响节流: cooldown 和当日上限两处查询都带 success = TRUE, 所以一次失败
    之后下一个 session 仍然可以重试。
    """
    await db.execute(text("""
        INSERT INTO device_post_history
            (device_id, primary_destination, content, success, platform, fail_reason)
        VALUES (:did, :dest, :content, :ok, :plat, :reason)
    """), {
        "did": req.device_id,
        "dest": req.primary_destination,
        "content": req.content,
        "ok": bool(req.success),
        "plat": req.platform or "tiktok",
        "reason": None if req.success else (req.fail_reason or "unknown"),
    })
    await db.commit()

    if not req.success:
        log.warning(
            "post_publish_failed",
            device_id=req.device_id, platform=req.platform or "tiktok",
            reason=req.fail_reason or "unknown", length=len(req.content or ""),
        )
    return {"ok": True}
