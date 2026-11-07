"""
posting.py - 个人主页 + 群组发帖 API

设计:
1. 服务端写时间戳是"开始发帖前", 防止竞态条件
2. 内容用 LLM 生成, 含大量长尾关键词 (SEO 优化)
3. 个人主页: device_id 维度, 每天 2 帖, 间隔 3 小时
4. 群组: group_id 维度, 间隔 3 小时, 不限数量, 任何 device 都能发
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
import random

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.database import get_db
from app.deps import get_redis, get_device_id
from app.services.post_analyzer import remember_own_reply
from app.services.country_promo import b2b_block, consumer_block, featured_for_country
from app.models.group import Group
from app.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter()

# ── FB 群组发帖频率 (2026-08-09 调低) ────────────────────────────
# 原来是每机每天 10 帖 / 两帖间隔 20 分钟, 实测 074f 连续多天贴着 9/天 跑。
# 发帖比评论更像营销行为, 也是 4824 被限的那一类动作 —— 降下来, 把额度让给评论。
DEVICE_DAILY_GROUP_POSTS = 6
POST_MIN_GAP_MINUTES = 20

# ════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════

class GeneratePersonalPostRequest(BaseModel):
    pass  # device_id 通过 header

class GeneratePersonalPostResponse(BaseModel):
    eligible: bool
    personal_post_id: Optional[int] = None
    content: Optional[str] = None
    reason: Optional[str] = None  # too_soon / daily_limit / generation_failed


class ConfirmPersonalPostRequest(BaseModel):
    personal_post_id: int
    success: bool
    failure_stage: Optional[str] = None
    failure_reason: Optional[str] = None
    elapsed_ms: Optional[int] = None


class ConfirmPersonalPostResponse(BaseModel):
    ok: bool
    personal_post_id: int
    final_status: str


class GenerateGroupPostRequest(BaseModel):
    group_id: int


class GenerateGroupPostResponse(BaseModel):
    eligible: bool
    group_post_id: Optional[int] = None
    content: Optional[str] = None
    reason: Optional[str] = None
    # 2026-09-16: 这个群需要管理员审核。客户端据此在发帖前先进
    # "群组 → You → Manage posts"(标题 Your content) 查 Pending/Declined,
    # 非空就别发 —— 路径和字符串是真机实测确认的 (Visit Venice)。
    # 只对这类群做, 普通群不值得多两次导航。
    needs_approval: bool = False


class ConfirmGroupPostRequest(BaseModel):
    group_post_id: int
    success: bool
    failure_stage: Optional[str] = None
    failure_reason: Optional[str] = None
    elapsed_ms: Optional[int] = None
    # 2026-09-16: 客户端在编辑器里看到 "Submit a public post for admin approval"
    # 就带 true。服务端据此给群打 #needs_approval —— 之后这个群在上一条确认
    # 可见之前不再发帖 (见 generate_group_post 的限流 0)。
    needs_approval: Optional[bool] = None


class ConfirmGroupPostResponse(BaseModel):
    ok: bool
    group_post_id: int
    final_status: str


# ════════════════════════════════════════════════════════════
# LLM 内容生成
# ════════════════════════════════════════════════════════════

# 个人主页发帖: 按出行目的地组织, 5 个目的地循环
PERSONAL_DESTINATIONS = [
    ("Nhật Bản", "eSIM Nhật Bản, SIM 4G Tokyo, wifi Nhật rẻ, eSIM Osaka, sim du lịch Nhật, pocket wifi Nhật Bản"),
    ("Hàn Quốc", "eSIM Hàn Quốc, SIM 5G Seoul, wifi Hàn Quốc, sim du lịch Korea, eSIM Busan, pocket wifi Hàn"),
    ("Trung Quốc", "eSIM Trung Quốc, SIM TQ vào Facebook, wifi Trung Quốc VPN, sim Bắc Kinh, eSIM Thượng Hải, sim du lịch China"),
    ("Thái Lan", "eSIM Thái Lan, SIM 4G Bangkok, wifi Pattaya, sim du lịch Thailand, eSIM Phuket, sim 5G Thái"),
    ("Châu Âu", "eSIM Châu Âu, SIM du lịch Schengen, wifi Pháp Đức Ý, eSIM Schengen rẻ, sim 4G EU, roaming Châu Âu"),
    ("Singapore", "eSIM Singapore, SIM 5G Singapore, wifi Sing rẻ, sim du lịch Singapore, sim Đông Nam Á"),
    ("Đài Loan", "eSIM Đài Loan, SIM 4G Đài Bắc, wifi Đài Loan, sim du lịch Taiwan, eSIM Taipei"),
    ("Mỹ", "eSIM Mỹ, SIM 4G USA, wifi du lịch Mỹ, sim 5G America, eSIM California New York"),
]


async def generate_personal_post_content(destination: str, keywords: str) -> str:
    """
    用 LLM 生成个人主页发帖, 含大量长尾关键词.
    包含: 出行场景 + 痛点 + IntlRoam 推荐 + 价格 + 售后 + hashtag
    """
    from app.services.llm_client import call_llm
    
    system = """Bạn là người dùng Việt Nam chia sẻ kinh nghiệm du lịch nước ngoài trên Facebook.
Viết bài đăng tự nhiên như một du khách, không gồng như quảng cáo.
Bao gồm nhiều từ khóa SEO du lịch + eSIM một cách tự nhiên.
Đề cập IntlRoam như giải pháp cá nhân đã dùng và hài lòng.
KHÔNG dùng emoji giật gân (🔥🔥🔥, !!!), giữ tone chân thật.

CỰC KỲ QUAN TRỌNG: Câu mở phải đa dạng, không được lặp lại theo mẫu cũ.
TRÁNH các câu mở thường gặp như: "Sắp tới mình...", "Mình sắp đi...", "Em chuẩn bị...".
THỬ các kiểu câu mở khác như:
- Câu hỏi trực tiếp: "Cả nhà cho hỏi đi [địa điểm] dùng eSIM nào ngon?"
- Chia sẻ kinh nghiệm: "Vừa về từ [địa điểm], chia sẻ chút kinh nghiệm cho ae"
- Trạng thái cảm xúc: "Đang lăn tăn vụ mạng cho chuyến đi [địa điểm]"
- Lưu ý hữu ích: "Đi [địa điểm] mà chưa biết vụ này thì hơi tiếc..."
- Câu chuyện ngắn: "Hôm trước đi [địa điểm], suýt nữa mất mạng giữa chừng..."

DOMAIN BẮT BUỘC: Website chính thức là intlroam.com (đuôi .com, KHÔNG PHẢI .vn).
TUYỆT ĐỐI KHÔNG viết intlroam.vn — đây là LỖI NGHIÊM TRỌNG.

Trả về CHỈ nội dung bài đăng, không có markdown, không giải thích."""
    
    user = f"""Viết bài đăng Facebook về chuyến đi {destination} sắp tới.

YÊU CẦU:
1. Bao gồm các từ khóa SEO (rải tự nhiên trong bài): {keywords}
2. Cấu trúc:
   - Câu mở: chuẩn bị/sắp đi {destination}, hỏi mọi người
   - Pain point: roaming Viettel/MobiFone đắt 200-300k/ngày, mạng kém
   - Giải pháp: dùng IntlRoam eSIM - kích hoạt nhanh, 5G full tốc độ, không lo mất sóng
   - Giá: rẻ hơn pocket wifi / roaming (không cần số cụ thể)
   - Hỗ trợ: chăm sóc 24/7 tiếng Việt, đổi gói/refund linh hoạt
   - CTA: inbox m hoặc xem intlroam.com
3. Cuối: 4-6 hashtag liên quan {destination} + eSIM + du lịch
4. ĐỘ DÀI BẮT BUỘC: 50-80 từ (KHÔNG vượt quá 400 ký tự). Ngắn gọn, tự nhiên như một status FB.
5. KHÔNG VIẾT TIÊU ĐỀ/HEADLINE. Bắt đầu thẳng vào nội dung. FB post không cần tiêu đề."""

    try:
        result = await call_llm(
            system_instruction=system,
            user_message=user,
            temperature=0.9,
            json_mode=False,
        )
        if result.success and result.raw_text:
            content = result.raw_text.strip()
            # 清除可能的 markdown wrap
            if content.startswith("```"):
                content = content.strip("`").strip()
                if content.startswith(("markdown", "text", "vietnamese")):
                    content = content.split("\n", 1)[1] if "\n" in content else content
            if len(content) > 50:
                return content
    except Exception as e:
        log.warn("personal_post_llm_failed", err=str(e))
    
    # Fallback 模板 (LLM 失败时)
    return f"""Sắp đi {destination} mà còn lăn tăn về vấn đề mạng và data?

Mình từng bị roaming Viettel ăn 2-300k/ngày khi đi {destination}, vừa đắt vừa chậm. Đổi qua eSIM IntlRoam thì khỏe hẳn: kích hoạt 1 phút bằng QR code, có 5G/4G full tốc độ, không lo mất sóng vùng heo hút.

{keywords.replace(', ', ' / ')} - bên IntlRoam có hết, giá rẻ hơn pocket wifi và roaming nước ngoài.

Bonus: chat support 24/7 bằng tiếng Việt, đổi gói hoặc refund nếu có vấn đề. Mình đi {destination} mấy lần đều ok.

Inbox m hoặc xem intlroam.com để hỏi nha.

#eSIM{destination.replace(' ', '')} #SimDuLich #IntlRoam #DuLich{destination.replace(' ', '')} #eSIMre #wifiQuocTe"""


async def generate_group_post_content(group_url: str, group_name: Optional[str]) -> str:
    """
    根据群组主题生成发帖. 群组 URL/name 含线索 (sim, japan, korea, du-lich 等).
    """
    from app.services.llm_client import call_llm
    
    # 推断群组主题
    url_lower = group_url.lower()
    name_lower = (group_name or "").lower()
    combined = url_lower + " " + name_lower
    
    if any(k in combined for k in ["nhat", "japan", "tokyo", "osaka", "kyoto"]):
        theme = "Nhật Bản"
        keywords = "eSIM Nhật Bản, SIM Tokyo, wifi Nhật, sim du lịch Nhật"
    elif any(k in combined for k in ["han", "korea", "seoul", "busan"]):
        theme = "Hàn Quốc"
        keywords = "eSIM Hàn Quốc, SIM Seoul, wifi Hàn Quốc, sim Korea"
    elif any(k in combined for k in ["trung", "china", "bac-kinh", "shanghai"]):
        theme = "Trung Quốc"
        keywords = "eSIM Trung Quốc, SIM TQ Facebook, wifi VPN TQ"
    elif any(k in combined for k in ["thai", "bangkok", "phuket"]):
        theme = "Thái Lan"
        keywords = "eSIM Thái Lan, SIM Bangkok, wifi Thái"
    elif any(k in combined for k in ["dai-loan", "taiwan", "taipei"]):
        theme = "Đài Loan"
        keywords = "eSIM Đài Loan, SIM Đài Bắc, wifi Đài Loan"
    elif any(k in combined for k in ["singapore", "sing", "malaysia"]):
        theme = "Đông Nam Á"
        keywords = "eSIM Singapore Malaysia, SIM 4G Sing, wifi Đông Nam Á"
    elif any(k in combined for k in ["chau-au", "europe", "schengen"]):
        theme = "Châu Âu"
        keywords = "eSIM Châu Âu, SIM Schengen, wifi Pháp Đức Ý"
    elif any(k in combined for k in ["my", "america", "usa"]):
        theme = "Mỹ"
        keywords = "eSIM Mỹ, SIM 4G USA, wifi du lịch Mỹ"
    else:
        # 通用旅游群
        theme = random.choice(["Nhật Bản", "Hàn Quốc", "Thái Lan", "Châu Âu"])
        keywords = f"eSIM {theme}, SIM du lịch quốc tế, wifi đa quốc gia"
    
    from app.services.llm_client import call_llm
    
    system = """Bạn là thành viên thường xuyên của group Facebook du lịch.
Viết bài đăng tự nhiên, chia sẻ kinh nghiệm, không gồng như quảng cáo.
Bao gồm từ khóa SEO du lịch + eSIM một cách tự nhiên.
Đề cập IntlRoam như giải pháp cá nhân đã dùng và hài lòng.
KHÔNG dùng emoji giật gân, giữ tone chân thật.
KHÔNG dùng hashtag (#) trong bài đăng group.
DOMAIN BẮT BUỘC: Website chính thức là intlroam.com (đuôi .com, KHÔNG PHẢI .vn).
TUYỆT ĐỐI KHÔNG viết intlroam.vn — đây là LỖI NGHIÊM TRỌNG.

Trả về CHỈ nội dung bài đăng, không có markdown, không giải thích."""
    
    user = f"""Viết bài đăng cho group du lịch {theme} (100-180 từ).

YÊU CẦU:
1. Từ khóa SEO bắt buộc (rải tự nhiên): {keywords}
2. Đề cập:
   - Vấn đề: roaming Viettel/MobiFone đắt khi đi {theme}, mạng kém/mất sóng
   - eSIM IntlRoam: kích hoạt nhanh bằng QR, 4G/5G full speed, ổn định
   - Giá rẻ hơn pocket wifi nhiều
   - Chăm sóc 24/7 tiếng Việt
3. CTA mềm: inbox hoặc intlroam.com
4. TUYỆT ĐỐI KHÔNG dùng hashtag (#). KHÔNG viết bất kỳ hashtag nào. Chỉ viết văn bản thuần.
5. ĐỘ DÀI BẮT BUỘC: 40-70 từ (KHÔNG vượt quá 350 ký tự). Ngắn gọn cho group, không lan man.
6. KHÔNG VIẾT TIÊU ĐỀ/HEADLINE. Bắt đầu thẳng vào nội dung. FB group post không cần tiêu đề."""

    try:
        result = await call_llm(
            system_instruction=system,
            user_message=user,
            temperature=0.9,
            json_mode=False,
        )
        if result.success and result.raw_text:
            content = result.raw_text.strip()
            if content.startswith("```"):
                content = content.strip("`").strip()
                if content.startswith(("markdown", "text", "vietnamese")):
                    content = content.split("\n", 1)[1] if "\n" in content else content
            if len(content) > 50:
                return content
    except Exception as e:
        log.warn("group_post_llm_failed", err=str(e), group=group_url[:40])
    
    # Fallback
    return f"""Chia sẻ với cả nhà đi {theme}:

Đợt trước mình đi {theme}, roaming Viettel/MobiFone ăn 200k/ngày mà mạng yếu lề mề. Sau chuyển qua eSIM IntlRoam thì ổn áp hẳn - kích hoạt bằng QR 1 phút, 4G/5G full speed, không lo mất sóng.

{keywords} bên IntlRoam có đủ, giá rẻ hơn nhiều so với pocket wifi hay roaming.

Có chăm sóc 24/7 tiếng Việt, đổi gói linh hoạt. Cần thì inbox m hoặc xem intlroam.com nha mọi người.

#eSIM{theme.replace(' ', '')} #SimDuLich{theme.replace(' ', '')} #IntlRoam #DuLichQuocTe"""


# ════════════════════════════════════════════════════════════
# 英语发帖 (2026-07-14)
# ════════════════════════════════════════════════════════════

EN_PERSONAL_DESTINATIONS = [
    "Japan", "South Korea", "Thailand", "Europe", "China",
    "Taiwan", "Singapore", "Vietnam", "the USA",
]

# 2026-07-24: 个人帖不再写"just got back from X"式的假旅行报告 —— 改成从具体旅行环节
# (酒店/景点/交通/找路/美食/一日游) 切入一个实用 tip, eSIM 只是"做这件事需要联网"的顺带细节。
# 每天轮换角度, 让主页看起来像"分享旅行经验的人", 而不是"每天从不同国家旅行回来的人"。
EN_PERSONAL_ANGLES = [
    "getting from the airport into the city — booking a ride-hail or taxi on your phone the moment you land",
    "checking into your hotel — pulling up the booking, the reviews and the map to actually find the place",
    "visiting a famous landmark or attraction — buying skip-the-line or timed-entry tickets on the app",
    "getting around on local public transport — transit apps, QR metro tickets, live departure times",
    "not getting lost — live maps and navigation when the printed directions fall apart",
    "finding good food — reading reviews, translating a menu, or ordering on a delivery app",
    "booking day trips and tours on the go — and messaging the guide or host to sort details",
]

# 四套 prompt 共用的事实库。改这里 = 改所有英语发帖的卖点口径。
EN_FACT_BANK = """WHAT IS ACTUALLY TRUE ABOUT INTLROAM — use AT MOST TWO of these, whichever fit:
- Plans start at $0.99. Use the number; never say "cheapest" or "best value".
- 170+ countries, 10,000+ plans.
- Japan and Vietnam run on a premium local network there — better quality than the
  standard partner network. Only mention for those two.
- Every Europe plan is multi-country: one plan for the whole trip, no swapping at borders.
  ⚠️ ONLY for European destinations. Never bring up Europe plans in a post about
  Argentina, Japan, Thailand or anywhere else — it is irrelevant and instantly reads
  as copy-paste marketing.
- Real 24/7 human support.
- In China your usual apps just work normally. ⚠️ ONLY for China.
- Plans include a free built-in VPN. You may say so; frame it as something we
  provide, never as a how-to for getting around a block.

⚠️ RELEVANCE RULE: a fact that does not apply to the destination in question is worse
than no fact at all. If none of the above fits, just give a useful tip and skip them.

BANNED, WITHOUT EXCEPTION:
- Naming any other social or messaging app (WhatsApp, Instagram, Telegram, WeChat...).
  Say "your usual apps" instead.
- Naming any competitor or any other product/brand.
- "cheapest" / "best" / "game changer" / "must-have" / ALL CAPS / emoji spam.
- Vietnamese carriers (Viettel, MobiFone) or anything Vietnam-specific — this is an
  English-speaking audience, they have never heard of them."""


def _en_theme_from_group(group) -> str:
    """英语群组的目的地。优先用 groups.country (2026-10-31); 多国群退回 notes
    里导入时写的地区 ('seed 20260714 / Europe')。"""
    if getattr(group, "country", None):
        return group.country
    notes = (group.notes or "")
    if "/" in notes:
        country = notes.rsplit("/", 1)[-1].strip()
        if country and len(country) < 40:
            return country.split("#")[0].strip() or "international travel"
    name = (group.name or "") or (group.url or "")
    for c in ("Japan", "Korea", "Thailand", "Vietnam", "China", "Taiwan", "Singapore",
              "Europe", "Italy", "Spain", "Portugal", "Croatia", "Albania", "Greece",
              "Turkey", "Morocco", "Mexico", "Brazil", "Australia", "Iceland"):
        if c.lower() in name.lower():
            return c
    return "international travel"


async def generate_personal_post_content_en(destination: str) -> str:
    """个人主页发帖 (英语)。软文口吻, 不是硬广。按旅行环节角度轮换, 不写'刚旅行回来'。"""
    from app.services.llm_client import call_llm
    import random
    angle = random.choice(EN_PERSONAL_ANGLES)

    system = f"""You are a real traveller sharing on your own Facebook profile.
Write like a person posting a status, not like a brand running an ad. Honest tone,
contractions, no hype. You use IntlRoam and mention it once, as a detail in the story.

The post must be worth reading on its own: a specific tip, an honest experience, or a
mistake you made. Include one honest caveat (e.g. travel eSIMs are data only, so you keep
your normal sim in for calls and 2FA texts) — that is what makes it credible.

{EN_FACT_BANK}

Official site is intlroam.com — never write intlroam.vn.

Return ONLY the post text. No markdown, no preamble, no headline."""

    user = f"""Write a short Facebook status sharing ONE genuine travel tip about {angle}, set in {destination}.

FRAME — read carefully, this is the whole point:
- This is a helpful travel TIP, not a trip report. Do NOT open with "Just got back from...",
  "Just landed in...", "Wish I'd known...", "Soaking up..." or any "I returned from my trip"
  framing — posting a different-country trip report every day is an obvious bot pattern and
  the single thing to avoid here.
- Lead with the tip itself about {angle}. A reader should learn something genuinely useful
  about {destination} even if they completely ignore the brand.
- This activity needs mobile data (you have to be online to do it), so mention — once, in
  passing — that you used an IntlRoam eSIM to stay connected for it. It's a detail, not the
  subject of the post.

- 50-80 words, hard max 400 characters. A status, not an essay.
- One concrete, checkable detail (a price, a number, a specific gotcha).
- One honest caveat (travel eSIMs are data only — keep your normal sim in for calls/2FA).
- Optionally end with a soft "DM me" or intlroam.com. Not both.
- 3-5 hashtags at the end (destination + esim + travel). No #ad, no #sponsored."""

    try:
        result = await call_llm(
            system_instruction=system, user_message=user,
            temperature=0.9, json_mode=False,
        )
        if result.success and result.raw_text:
            content = result.raw_text.strip()
            if content.startswith("```"):
                content = content.strip("`").strip()
                if content.startswith(("markdown", "text", "english")):
                    content = content.split("\n", 1)[1] if "\n" in content else content
            if len(content) > 50:
                return content
    except Exception as e:
        log.warn("personal_post_en_llm_failed", err=str(e))

    return (
        f"Sorting data for {destination} before I fly this time — learned that one the hard "
        f"way after a roaming bill that made my eyes water. Installed an IntlRoam eSIM at home "
        f"(you need wifi to scan the QR) and it was live the moment I landed, a few dollars for "
        f"the week. Only catch: it's data only, so my normal sim stays in for texts. "
        f"intlroam.com if you want a look.\n\n#travel #esim #IntlRoam #traveltips"
    )


# ── 群发帖开场角度池 (2026-07-31) ──
# 改之前 prompt 里直接给了 4 个逐字示例开场, LLM 没把它们当"角度", 而是照抄了下来:
# 近 7 天成功的群发帖里 "Made the classic error of waiting until I landed" 出现 7 次
# 跨 3 台机, "Coverage out in the Australian countryside" 5 次跨 4 台机, 07-30/31
# 的 18 条群发帖有 12 条以 "Coverage out in the ... countryside" 开头。
# 多台机共用同一批群 → 同一开场在同一个群里被不同账号反复贴出来 = 明显的批量特征。
# 现在改成: 服务端每次随机挑一个角度, 只描述角度不给例句, 并禁掉已经用烂的开头。
GROUP_POST_ANGLES = [
    "answer a question the group asks constantly, in a couple of sentences",
    "a small logistical detail people get wrong on arrival day",
    "a comparison between two ways of getting online there, with the trade-off",
    "something that surprised you about coverage or signal in a specific area",
    "a cost breakdown — what you actually paid and what you expected to pay",
    "a warning about a specific thing that does not work the way people assume",
    "advice aimed at one particular kind of traveller (families, solo, long-stay)",
    "a short account of a moment when being offline actually cost you something",
    "what you would do differently next time, stated plainly",
    "a practical checklist of two or three things to do before departure",
    "correcting a piece of advice that gets repeated in these groups but is outdated",
    "a note on how it differs between the capital and everywhere else",
]

# 每次随机禁掉一个高频开头词, 逼开场句式分散
GROUP_POST_FIRST_WORD_BANS = [
    "Just", "Coverage", "Made", "Anyone", "I", "If", "For", "Quick", "Heads", "So",
]


async def generate_group_post_content_en(group) -> str:
    """群组发帖 (英语)。群组比个人主页更严格 —— 版主对广告零容忍。"""
    from app.services.llm_client import call_llm

    theme = _en_theme_from_group(group)
    featured = await featured_for_country(theme)

    system = f"""You are a regular member of an English-speaking Facebook travel group.
You are posting something USEFUL to the group, not an advert. Group moderators remove
promotional posts and ban the accounts that make them — a post that reads like marketing
is worse than no post at all.

Lead with real, specific information. You use IntlRoam and mention it once, in passing,
as the thing you happened to use. Include one honest caveat.

{EN_FACT_BANK}

Official site is intlroam.com — never write intlroam.vn.

Return ONLY the post text. No markdown, no preamble, no headline."""

    angle = random.choice(GROUP_POST_ANGLES)
    opening_word = random.choice(GROUP_POST_FIRST_WORD_BANS)

    user = f"""Write a post for a travel group about {theme}.

- 70-110 words, hard max 600 characters. Long enough to say something real, short
  enough that nobody scrolls past it.
- ANGLE FOR THIS POST (use this one, not any other): {angle}
  Write the opening sentence yourself, in your own words, grounded in {theme}.
  Do not reuse any phrasing from these instructions verbatim.
- Your first sentence must NOT begin with the word "{opening_word}".
- Do NOT open with any variation of "Just got back from" / "Just returned from" /
  "Just spent a week in", nor with "Coverage out in the", "Made the classic error of",
  or "Anyone else find" — every one of those is worn out. Do not start with a summary
  of your trip at all; start with the substance.
- Share something genuinely useful about staying connected in/around {theme}, and make
  it specific to {theme} — a place name, a route, a carrier quirk, a real situation.
  Generic travel filler that would fit any country is a failure.
- Name "IntlRoam" exactly once, as a passing detail — never as a recommendation with a
  feature list.
- One concrete fact (a price, a number, a specific gotcha).
- One honest caveat.
- ABSOLUTELY NO hashtags. Not one # character.
- Soft close only: "happy to share what I used if anyone's heading there" or
  intlroam.com. Never a hard CTA."""
    if featured:
        # 按群组国家主推一款产品 + 网站实时价 (见 services/country_promo.py)。
        # "IntlRoam 只出现一次" 仍然成立: 产品名是对 IntlRoam 那一处的具体化。
        system += consumer_block(featured)
        user += (f"\n- The thing you used is the featured plan: \"{featured['title']}\". "
                 f"Your one concrete fact may be its real price from the list.")

    try:
        result = await call_llm(
            system_instruction=system, user_message=user,
            temperature=0.9, json_mode=False,
        )
        if result.success and result.raw_text:
            content = result.raw_text.strip()
            if content.startswith("```"):
                content = content.strip("`").strip()
                if content.startswith(("markdown", "text", "english")):
                    content = content.split("\n", 1)[1] if "\n" in content else content
            content = content.replace("#", "")  # 群组帖硬性无 hashtag
            if len(content) > 50:
                log.info("group_post_featured", country=theme,
                         featured=(featured or {}).get("title"))
                return content
    except Exception as e:
        log.warn("group_post_en_llm_failed", err=str(e), group=(group.url or "")[:40])

    return (
        f"For anyone heading to {theme}: install your eSIM before you fly. You need wifi to "
        f"scan the QR, and airport wifi is usually painful. I used IntlRoam last time, a few "
        f"dollars for the week and it was online the second I landed. Worth knowing it's data "
        f"only, so keep your normal sim in for texts."
    )


async def _resolve_device_lang(db, device_id: str) -> str:
    """设备的抽词语言决定发帖语言。空 -> 越南语 (原行为)。"""
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


# ════════════════════════════════════════════════════════════
# 个人主页发帖 API
# ════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# FB 招商帖 (PIVOT 2026-09-15) —— 面向旅行社/导游/手机店, 不是面向游客
# ═══════════════════════════════════════════════════════════════════
#
# 比例分开定的理由见本段上方的模块注释: 群组有版主、账号封了就没法评论,
# 所以群组只给 0.25, 大头继续发真有用的旅行内容维持账号可信度。
B2B_FB_RATE = {"personal": 0.5, "group": 0.25}

# 招商帖能讲什么。⚠️ 这里**没有 VPN** —— 全系统禁词, 见 EN_FACT_BANK。
# 它对代理确实是强卖点, 但属于私下对话的内容, 不进公开帖。
B2B_FB_FACTS = """WHAT WE CAN OFFER A RESELLER — name AT MOST TWO, whichever fit:
- Wholesale pricing they mark up themselves; they set their own retail price.
- No stock and no upfront: eSIMs are provisioned per customer, instantly.
- Premium local carriers on the main routes, not the cheapest roaming hop.
- 170+ countries on one account, so one supplier covers the whole book.
- Real 24/7 human support — their customer problem does not become their problem.
- Works for agencies, guides, tour operators, phone shops and existing SIM sellers.
- A free built-in VPN comes with the plan, and on our roaming network the usual
  apps generally just work — so their client has nothing extra to install.

BANNED, WITHOUT EXCEPTION:
- ⚠️ NEVER name a specific mobile network or carrier (Vodafone, O2, Turkcell, NTT,
  Airtel, du, Ooredoo, whoever). You do not have the routing list in front of you, and
  a guess here is a false claim about our supply — your reader is in the trade, will
  check, and may repeat it to their own customers. Say "premium local carriers" or
  "the main local network", never a brand name.
- Any price, percentage or margin figure. Public numbers invite undercutting and get
  the post reported as an advert.
- "reseller program", "affiliate", "partnership opportunity", "business opportunity",
  "passive income", "earn while you sleep" — every one reads as spam.
- Promising earnings, exclusivity or territory.
- Naming any competitor, or any other product/brand.
- Naming another social or messaging app. Say "the usual apps".
- "cheapest" / "best" / "game changer" / "must-have" / ALL CAPS / emoji spam."""

B2B_FB_ANGLES = [
    "what changes for an agency once clients stop asking where to buy a local SIM",
    "why holding physical SIM stock stopped making sense for small operators",
    "the awkward moment a client lands and cannot get online before the transfer",
    "how a tour operator adds connectivity to a package without touching inventory",
    "what to ask a supplier before you resell their eSIM",
    "the difference a carrier choice makes to what the client actually experiences",
    "handling a last-minute booking when there is no time to post anything physical",
    "who actually ends up fielding the 'my data is not working' message, and when",
]


async def generate_personal_post_b2b_en(destination: str) -> str:
    """FB 个人主页招商帖。读者是同行, 不是游客。"""
    from app.services.llm_client import call_llm
    import random
    angle = random.choice(B2B_FB_ANGLES)

    system = f"""You work with a travel eSIM supplier (IntlRoam) and post on your own
Facebook profile about the trade. Your readers are travel agencies, tour operators,
guides and phone/SIM shops — people who SELL to travellers, not travellers themselves.

Write like a person in the business, not like a brand running a campaign. Plain words,
contractions, no hype. Naming IntlRoam once is fine and expected: a trade contact needs
to know who they would be dealing with.

{B2B_FB_FACTS}

Official site is intlroam.com — never write intlroam.vn.

Return ONLY the post text. No markdown, no preamble, no headline."""

    user = f"""Write a short Facebook status aimed at people who sell travel around
{destination}: {angle}.

- Open on THEIR business or THEIR problem, never on us.
- LENGTH IS A HARD CONSTRAINT: 50-75 words and UNDER 400 characters, hashtags
  included. Count before you answer.
- One concrete, checkable detail — a real situation, not vague enthusiasm.
- ONE soft invite at the end, pointing at a conversation, not a sale:
  "happy to send the trade pricing if that's useful" or "drop me a message".
  Never a link plus an invite; pick one.
- 3-4 trade-facing hashtags (#travelagent / #touroperator / #esim + one market).
  No #ad, no #sponsored.
- Do not reuse any phrasing from these instructions verbatim."""

    try:
        result = await call_llm(
            system_instruction=system, user_message=user,
            temperature=0.9, json_mode=False,
        )
        if result.success and result.raw_text:
            content = result.raw_text.strip()
            if content.startswith("```"):
                content = content.strip("`").strip()
                if content.startswith(("markdown", "text", "english")):
                    content = content.split("\n", 1)[1] if "\n" in content else content
            if len(content) > 50:
                return content
    except Exception as e:
        log.warn("personal_post_b2b_llm_failed", err=str(e))

    # 兜底也必须是招商口吻, 不能掉回消费者话术
    return (
        f"If you sell trips around {destination}, the data question comes up on every "
        f"booking. We supply travel eSIMs wholesale at IntlRoam — nothing to stock, "
        f"provisioned per customer, and you set your own retail price. Happy to send "
        f"the trade pricing if it's useful.\n\n#travelagent #touroperator #esim"
    )


async def generate_group_post_b2b_en(group) -> str:
    """FB 群组招商帖。群里有版主 —— 必须读起来像同行闲聊, 不是广告。"""
    from app.services.llm_client import call_llm
    import random

    theme = _en_theme_from_group(group)
    featured = await featured_for_country(theme)
    angle = random.choice(B2B_FB_ANGLES)
    opening_word = random.choice(GROUP_POST_FIRST_WORD_BANS)

    system = f"""You are a regular member of an English-speaking Facebook travel group who
works in the trade. Plenty of people in these groups sell travel — agencies, guides,
drivers, shops — and you are talking to them, not to the holidaymakers.

Moderators remove promotional posts and ban the accounts that make them. A post that
reads like an advert is worse than no post at all, and this account's real job is
answering people in the comments — losing it to an ad removal costs far more than this
post is worth. So: lead with a real observation about the trade, not with an offer.

{B2B_FB_FACTS}

Official site is intlroam.com — never write intlroam.vn.

Return ONLY the post text. No markdown, no preamble, no headline."""

    user = f"""Write a post for a travel group about {theme}, aimed at the people in it who
SELL travel: {angle}.

- LENGTH IS A HARD CONSTRAINT: 70-100 words and UNDER 600 characters. Count before
  you answer — a long post in a group reads as a pitch and gets removed.
- Open with a concrete observation or question from the trade, grounded in {theme}.
  Your first sentence must NOT begin with the word "{opening_word}".
- Do NOT open with "Just got back from" / "Coverage out in the" / "Anyone else find" —
  all worn out.
- Name "IntlRoam" exactly once, as who you work with — never as a recommendation with
  a feature list.
- ABSOLUTELY NO hashtags. Not one # character.
- Close by inviting a reply IN THE COMMENTS, not a DM: "curious how others handle it"
  or "happy to compare notes". A DM push reads as spam and gets the post removed.
- Do not reuse any phrasing from these instructions verbatim."""
    if featured:
        # 招商帖仍以批发为主, 主推产品只作为"可以卖给客户的例子", 不报价。
        system += b2b_block(featured)

    try:
        result = await call_llm(
            system_instruction=system, user_message=user,
            temperature=0.9, json_mode=False,
        )
        if result.success and result.raw_text:
            content = result.raw_text.strip()
            if content.startswith("```"):
                content = content.strip("`").strip()
                if content.startswith(("markdown", "text", "english")):
                    content = content.split("\n", 1)[1] if "\n" in content else content
            content = content.replace("#", "")
            if len(content) > 50:
                return content
    except Exception as e:
        log.warn("group_post_b2b_llm_failed", err=str(e), group=(group.url or "")[:40])

    return (
        f"A question for anyone here who actually sells {theme} trips: who ends up "
        f"fielding the 'my data isn't working' message, and at what hour? We supply "
        f"travel eSIMs at IntlRoam and that one seems to land on the agency every time. "
        f"Curious how others handle it."
    )


@router.post("/generate_personal_post", response_model=GeneratePersonalPostResponse)
async def generate_personal_post_endpoint(
    body: GeneratePersonalPostRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
):
    """
    检查 eligibility, 通过则生成内容 + 预写 pending 记录, 返回 content.
    APP 收到后立即去 FB 发帖, 完成后调 confirm.
    """
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")

    now = datetime.now(timezone.utc)
    three_hours_ago = now - timedelta(hours=3)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    # 检查 3 小时间隔
    # 只考虑成功 (success=true) 或 pending (success IS NULL) 的记录
    # 失败 (success=false) 不算, 让 APP 立刻重试 (FB UI 不稳定/选择器变化等)
    last_post_q = await db.execute(
        __import__('sqlalchemy').text(
            "SELECT MAX(posted_at) FROM personal_posts "
            "WHERE device_id = :dev "
            # 2026-06-05: pending (success IS NULL) 限 5 分钟内 in-flight
            # 防 APP force-stop/崩溃留下孤儿 pending 永久锁设备
            "AND ("
            "  success = true "
            "  OR (success IS NULL AND posted_at > NOW() - INTERVAL '5 minutes') "
            ")"
        ),
        {"dev": device_id},
    )
    last_post_at = last_post_q.scalar()
    if last_post_at and last_post_at > three_hours_ago:
        return GeneratePersonalPostResponse(
            eligible=False,
            reason=f"too_soon (last={last_post_at.isoformat()})",
        )

    # 检查今日 2 帖上限 (只算成功 + pending, 失败的不算)
    today_count_q = await db.execute(
        __import__('sqlalchemy').text(
            "SELECT COUNT(*) FROM personal_posts "
            "WHERE device_id = :dev "
            "AND posted_at >= :start "
            # 2026-06-05: 同样规则, 孤儿 pending 不计入今日额度
            "AND ("
            "  success = true "
            "  OR (success IS NULL AND posted_at > NOW() - INTERVAL '5 minutes') "
            ")"
        ),
        {"dev": device_id, "start": today_start},
    )
    today_count = today_count_q.scalar() or 0
    if today_count >= 1:
        return GeneratePersonalPostResponse(
            eligible=False,
            reason=f"daily_limit_reached ({today_count}/1)",
        )

    post_lang = await _resolve_device_lang(db, device_id)

    if post_lang == "en":
        destination = EN_PERSONAL_DESTINATIONS[
            (today_count + hash(device_id)) % len(EN_PERSONAL_DESTINATIONS)
        ]
        # PIVOT 2026-09-15: 掷骰决定这条发招商帖还是消费者帖
        if random.random() < B2B_FB_RATE["personal"]:
            content = await generate_personal_post_b2b_en(destination)
            log.info("post_audience", channel="fb_personal", audience="b2b",
                     device_id=device_id)
        else:
            content = await generate_personal_post_content_en(destination)
            log.info("post_audience", channel="fb_personal", audience="consumer",
                     device_id=device_id)
    else:
        # 选目的地 (轮换避免内容重复)
        destination, keywords = PERSONAL_DESTINATIONS[
            (today_count + hash(device_id)) % len(PERSONAL_DESTINATIONS)
        ]
        content = await generate_personal_post_content(destination, keywords)
    if not content or len(content) < 50:
        return GeneratePersonalPostResponse(
            eligible=False,
            reason="generation_failed",
        )

    # 预写 pending 记录 (锁占用 3 小时窗口)
    insert_q = __import__('sqlalchemy').text(
        "INSERT INTO personal_posts (device_id, posted_at, content, destination_keyword, success) "
        "VALUES (:dev, NOW(), :content, :dest, NULL) "
        "RETURNING id"
    )
    result = await db.execute(insert_q, {
        "dev": device_id,
        "content": content,
        "dest": destination,
    })
    post_id = result.scalar()
    await db.commit()

    log.info("personal_post_generated",
             device_id=device_id, post_id=post_id,
             dest=destination, content_len=len(content))

    return GeneratePersonalPostResponse(
        eligible=True,
        personal_post_id=post_id,
        content=content,
    )


@router.post("/confirm_personal_post", response_model=ConfirmPersonalPostResponse)
async def confirm_personal_post_endpoint(
    body: ConfirmPersonalPostRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
):
    """APP 完成发帖后调用, UPDATE 状态."""
    update_q = __import__('sqlalchemy').text(
        "UPDATE personal_posts "
        "SET success = :s, failure_stage = :fs, failure_reason = :fr, "
        "    elapsed_ms = :em, updated_at = NOW() "
        "WHERE id = :pid AND device_id = :dev "
        "RETURNING id"
    )
    result = await db.execute(update_q, {
        "s": body.success,
        "fs": body.failure_stage,
        "fr": body.failure_reason,
        "em": body.elapsed_ms,
        "pid": body.personal_post_id,
        "dev": device_id,
    })
    updated_id = result.scalar()
    await db.commit()

    if updated_id is None:
        return ConfirmPersonalPostResponse(
            ok=False,
            personal_post_id=body.personal_post_id,
            final_status="not_found_or_device_mismatch",
        )

    log.info("personal_post_confirmed",
             device_id=device_id, post_id=updated_id,
             success=body.success, failure_stage=body.failure_stage)

    return ConfirmPersonalPostResponse(
        ok=True,
        personal_post_id=updated_id,
        final_status="success" if body.success else (body.failure_stage or "failed"),
    )


# ════════════════════════════════════════════════════════════
# 群组发帖 API
# ════════════════════════════════════════════════════════════

# 需管理员审核的群 (2026-09-16)。
# 标签打在 groups.notes 上, 与 #priority 同一套做法 —— 不加列就不用迁移。
NEEDS_APPROVAL_TAG = "#needs_approval"
# 待审多久还没看到自己的帖子, 就当它不会过了, 允许再试一次。
# 7 天是拍的: 大多数群的管理员几天内会处理, 拖过一周基本就是石沉大海。
PENDING_APPROVAL_HOLD_DAYS = 7


@router.post("/generate_group_post", response_model=GenerateGroupPostResponse)
async def generate_group_post_endpoint(
    body: GenerateGroupPostRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """检查 3 小时间隔, 通过则生成内容 + 预写 pending."""
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")

    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    twenty_min_ago = now - timedelta(minutes=POST_MIN_GAP_MINUTES)

    # 活跃判定: 成功 或 5 分钟内的 pending (孤儿 pending 不永久锁, 与个人帖一致)
    live_pred = (
        "(success = true "
        " OR (success IS NULL AND posted_at > NOW() - INTERVAL '5 minutes'))"
    )

    # 获取群组信息
    group_q = await db.execute(select(Group).where(Group.id == body.group_id))
    group = group_q.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="group not found")

    # 限流 0) 需审核的群: 上一条还在待审就不再发 (2026-09-16)
    #
    # 这种群发出去的帖子要管理员点头才可见。按 1 天 1 贴的节奏继续发, 只会在
    # 待审队列里堆一摞 —— 浪费生成配额, 在管理员那边也像垃圾账号。
    # 实测群池里有 12 个这种群。
    #
    # "上一条过审了没" 无法直接查, 用的是间接证据: agent 巡群时如果读到自己
    # 发过的那段内容, 说明已经可见 = 过审 (复用自发内容指纹那套)。
    # 超过 PENDING_APPROVAL_HOLD_DAYS 还没见到, 就当它不会过了, 放行再试一次。
    if NEEDS_APPROVAL_TAG in (group.notes or ""):
        last_pending_q = await db.execute(
            __import__("sqlalchemy").text(
                "SELECT MAX(posted_at) FROM group_posts "
                "WHERE group_id = :gid AND success = true "
                "  AND posted_at > NOW() - (:days || ' days')::interval"
            ),
            {"gid": body.group_id, "days": PENDING_APPROVAL_HOLD_DAYS},
        )
        last_pending = last_pending_q.scalar()
        # 巡群时读到过自己发的帖 = 已经可见 = 过审了, 放行。
        # 这个标记由 analyze_post 的自发内容分支打 (见 posts.py)。
        seen = False
        try:
            seen = bool(await rds.exists(f"group_post_seen:{body.group_id}"))
        except Exception:
            pass   # Redis 读不到就按"没见过"处理, 宁可少发不要多发
        if last_pending and not seen:
            log.info("group_post_hold_pending_approval",
                     group_id=body.group_id, last_posted=last_pending.isoformat())
            return GenerateGroupPostResponse(
                eligible=False,
                reason=(f"pending_approval_hold (last post {last_pending.isoformat()}, "
                        f"still not seen in feed)"),
            )

    # 限流 1) 每个群每天最多 1 贴 (失败的不算, 允许重试)
    grp_today_q = await db.execute(
        __import__('sqlalchemy').text(
            "SELECT COUNT(*) FROM group_posts "
            "WHERE group_id = :gid AND posted_at >= :start AND " + live_pred
        ),
        {"gid": body.group_id, "start": today_start},
    )
    if (grp_today_q.scalar() or 0) >= 1:
        return GenerateGroupPostResponse(
            eligible=False,
            reason="group_daily_limit (1/day per group)",
        )

    # 限流 2) 设备级: 任意两次群组发帖间隔 >= 20 分钟
    dev_last_q = await db.execute(
        __import__('sqlalchemy').text(
            "SELECT MAX(posted_at) FROM group_posts "
            "WHERE device_id = :dev AND " + live_pred
        ),
        {"dev": device_id},
    )
    dev_last = dev_last_q.scalar()
    if dev_last and dev_last > twenty_min_ago:
        return GenerateGroupPostResponse(
            eligible=False,
            reason=f"too_soon_device (last={dev_last.isoformat()})",
        )

    # 限流 3) 设备级: 每天最多 10 个群组发帖
    dev_today_q = await db.execute(
        __import__('sqlalchemy').text(
            "SELECT COUNT(*) FROM group_posts "
            "WHERE device_id = :dev AND posted_at >= :start AND " + live_pred
        ),
        {"dev": device_id, "start": today_start},
    )
    dev_today = dev_today_q.scalar() or 0
    if dev_today >= DEVICE_DAILY_GROUP_POSTS:
        return GenerateGroupPostResponse(
            eligible=False,
            reason=f"device_daily_limit ({dev_today}/{DEVICE_DAILY_GROUP_POSTS})",
        )

    # LLM 生成。语言以群组的 language_hint 为准 —— 帖子发进哪个群, 就用那个群的语言,
    # 而不是设备的语言 (一台英语设备也可能还留着几个越南语群)。
    group_lang = "en" if (group.language_hint or "").lower().startswith("en") else "vi"
    if group_lang == "en":
        # PIVOT 2026-09-15: 群组比例更低, 理由见 B2B_FB_RATE 注释
        if random.random() < B2B_FB_RATE["group"]:
            content = await generate_group_post_b2b_en(group)
            log.info("post_audience", channel="fb_group", audience="b2b",
                     device_id=device_id)
        else:
            content = await generate_group_post_content_en(group)
            log.info("post_audience", channel="fb_group", audience="consumer",
                     device_id=device_id)
    else:
        content = await generate_group_post_content(group.url, group.name)
    if not content or len(content) < 50:
        return GenerateGroupPostResponse(
            eligible=False,
            reason="generation_failed",
        )

    # 预写 pending
    insert_q = __import__('sqlalchemy').text(
        "INSERT INTO group_posts (group_id, device_id, posted_at, content, success) "
        "VALUES (:gid, :dev, NOW(), :content, NULL) "
        "RETURNING id"
    )
    result = await db.execute(insert_q, {
        "gid": body.group_id,
        "dev": device_id,
        "content": content,
    })
    post_id = result.scalar()
    await db.commit()

    log.info("group_post_generated",
             group_id=body.group_id, device_id=device_id,
             post_id=post_id, content_len=len(content))

    return GenerateGroupPostResponse(
        eligible=True,
        group_post_id=post_id,
        content=content,
        needs_approval=NEEDS_APPROVAL_TAG in (group.notes or ""),
    )


@router.post("/confirm_group_post", response_model=ConfirmGroupPostResponse)
async def confirm_group_post_endpoint(
    body: ConfirmGroupPostRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    update_q = __import__('sqlalchemy').text(
        "UPDATE group_posts "
        "SET success = :s, failure_stage = :fs, failure_reason = :fr, "
        "    elapsed_ms = :em, updated_at = NOW() "
        "WHERE id = :pid AND device_id = :dev "
        "RETURNING id"
    )
    result = await db.execute(update_q, {
        "s": body.success,
        "fs": body.failure_stage,
        "fr": body.failure_reason,
        "em": body.elapsed_ms,
        "pid": body.group_post_id,
        "dev": device_id,
    })
    updated_id = result.scalar()
    await db.commit()

    if updated_id is None:
        return ConfirmGroupPostResponse(
            ok=False,
            group_post_id=body.group_post_id,
            final_status="not_found_or_device_mismatch",
        )

    log.info("group_post_confirmed",
             device_id=device_id, post_id=updated_id,
             success=body.success, failure_stage=body.failure_stage)

    # 客户端在编辑器里看到 "admin approval" → 给这个群打上 #needs_approval。
    # 用 notes 标签而不是加列: 与 #priority 同一套做法, 不用迁移。
    # 打上之后, generate_group_post 的限流 0 会在上一条帖子确认可见之前
    # 不再给这个群生成内容 —— 避免帖子在待审队列里堆成一摞。
    # 新发了一条 → 旧的"已见过"标记作废, 重新开始等这一条过审
    if body.success:
        try:
            gp = await db.execute(
                __import__("sqlalchemy").text(
                    "SELECT group_id, content FROM group_posts WHERE id = :pid"
                ), {"pid": body.group_post_id})
            row = gp.first()
            if row and row[0]:
                await rds.delete(f"group_post_seen:{row[0]}")
                # 记下这条群帖的指纹, 巡群时撞上就知道它过审了
                if row[1]:
                    await remember_own_reply(rds, row[1])
        except Exception as e:
            log.warning("group_post_fingerprint_failed", err=str(e))

    if body.needs_approval:
        tagged = await db.execute(
            __import__("sqlalchemy").text(
                "UPDATE groups SET notes = trim(coalesce(notes,'') || ' ' || :tag) "
                "WHERE id = (SELECT group_id FROM group_posts WHERE id = :pid) "
                "  AND coalesce(notes,'') NOT LIKE '%' || :tag || '%' "
                "RETURNING id"
            ),
            {"tag": NEEDS_APPROVAL_TAG, "pid": body.group_post_id},
        )
        gid = tagged.scalar()
        if gid:
            await db.commit()
            log.info("group_marked_needs_approval", group_id=gid)

    # 2026-09-14: 群没了/本机被踢 → 拉黑 (device, group), 别再派过去。
    #
    # 客户端以前把四种完全不同的情况都报成 group_trigger_not_found, 所以这里
    # 什么都做不了, 只能一遍遍重试一个打不开的群。现在客户端会分开报, 其中
    # group_unavailable 对应屏幕上的 "This isn't available / Refresh / Back"。
    #
    # 用**设备级**黑名单而不是把 groups.is_active 关掉: 这个多半是本机账号被
    # 踢了, 换台机可能照样进得去; 而且黑名单有 30 天 TTL (BLACKLIST_TTL),
    # 会自动回流, 不会永久损失群组。若真是群被删了, 各台机会各自撞一次然后
    # 全都拉黑, 效果等同于停派。
    if body.success is False and body.failure_stage == "group_unavailable":
        try:
            from sqlalchemy.dialects.postgresql import insert as _pg_insert
            from app.models.device_group_blacklist import DeviceGroupBlacklist

            gid_row = await db.execute(
                __import__('sqlalchemy').text(
                    "SELECT group_id FROM group_posts WHERE id = :pid"
                ),
                {"pid": updated_id},
            )
            gid = gid_row.scalar()
            if gid:
                await db.execute(
                    _pg_insert(DeviceGroupBlacklist)
                    .values(device_id=device_id, group_id=gid,
                            reason="group_unavailable")
                    .on_conflict_do_nothing(index_elements=["device_id", "group_id"])
                )
                await db.commit()
                log.info("group_blacklisted_unavailable",
                         device_id=device_id, group_id=gid)
        except Exception as e:
            # 拉黑失败不该让上报失败 —— 帖子结果已经落库了
            log.warning("group_unavailable_blacklist_failed",
                        device_id=device_id, post_id=updated_id, err=str(e))

    return ConfirmGroupPostResponse(
        ok=True,
        group_post_id=updated_id,
        final_status="success" if body.success else (body.failure_stage or "failed"),
    )
