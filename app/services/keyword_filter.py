"""关键词预筛选 (Hotfix 3.11 Step 5)

在调用 LLM 之前，用关键词快速判断帖子是否与旅游/连接相关。
没有匹配的帖子直接 ignore，节省 LLM token。

越南语 + 中文（运营查看用）+ 英文（混杂 FB 帖子）全部覆盖。
"""
from __future__ import annotations
import re
import unicodedata

# ============ 旅游相关关键词 ============

TRAVEL_KEYWORDS = {
    # 出行动词（越南语）
    "đi", "đến", "tới", "sang", "bay",
    "du lịch", "đi du lịch", "công tác",
    "du học", "định cư", "xuất ngoại", "nước ngoài",
    "phượt", "trekking", "khám phá",
    # 准备/计划
    "sắp đi", "chuẩn bị đi", "lịch trình", "kế hoạch",
    "book vé", "đặt vé", "đặt tour", "đặt phòng",
    # 中文
    "出国", "旅游", "旅行", "出差", "留学",
    # 英文（原本只有 4 个词, 跑英语群组时覆盖严重不足 → 2026-07-14 补全）
    "travel", "trip", "vacation", "holiday",
    # 出行动词/短语
    "going to", "heading to", "flying to", "flying out",
    "visiting", "backpacking", "sightseeing", "exploring",
    "abroad", "overseas", "international trip",
    # 计划/准备（英语帖里最强的出行信号）
    "planning a trip", "planning to visit", "planning my trip",
    "itinerary", "travel plans", "leaving for", "about to travel",
    "first time in", "first time visiting", "any tips",
    "any recommendations", "recommendations for",
    # 出行类型
    "road trip", "solo travel", "solo trip", "family trip",
    "honeymoon", "business trip", "study abroad", "digital nomad",
    "expat", "layover", "stopover", "gap year",
}

# ============ 连接/通讯关键词（最高价值） ============

CONNECTIVITY_KEYWORDS = {
    # SIM/eSIM 类
    "sim", "esim", "e-sim", "e sim",
    # 网络
    "wifi", "wi-fi", "wf",
    "mạng", "internet", "data",
    "4g", "5g", "3g", "lte",
    # Roaming
    "roaming", "chuyển vùng",
    # 设备
    "pocket wifi", "pocketwifi",
    # 问题描述
    "mất sóng", "không có mạng", "đắt mạng", "chậm mạng",
    "không vào được", "tốc độ chậm",
    # 中文
    "电话卡", "上网卡", "网络", "信号", "漫游",
    # 英文（2026-07-14 补全 —— 这是转化价值最高的一桶）
    "sim card", "simcard", "local sim", "prepaid sim", "physical sim",
    "data plan", "data package", "mobile data", "cellular data",
    "unlimited data", "top up", "topup", "phone plan", "carrier",
    "network coverage", "coverage", "hotspot", "tethering",
    "connectivity", "stay connected", "get online",
    # 漫游 / 痛点（英语用户抱怨的原话）
    "roaming charges", "roaming fees", "data roaming", "roaming bill",
    "no signal", "no service", "no internet", "no wifi",
    "bad signal", "weak signal", "slow internet", "slow data",
    "expensive roaming", "roaming is expensive", "phone bill",
    "airport sim", "airport wifi",
}

# ============ 旅游服务（机票/签证/酒店/租车/门票等） ============

SERVICE_KEYWORDS = {
    # 机票
    "vé máy bay", "vé bay", "vé khứ hồi", "vé một chiều",
    "ticket", "flight",
    # 签证
    "visa", "passport", "hộ chiếu", "visa du lịch",
    "đậu visa", "làm visa", "xin visa",
    "签证", "护照",
    # 酒店/住宿
    "khách sạn", "hotel", "homestay", "airbnb",
    "phòng", "nhà nghỉ", "resort",
    "酒店", "民宿",
    # 租车
    "thuê xe", "rent car", "car rental",
    "xe máy", "ô tô",
    "租车",
    # 门票
    "vé tham quan", "vé universal", "vé disneyland",
    "vé entry", "ticket attraction",
    "门票",
    # 旅游产品
    "tour", "combo", "package", "lịch khởi hành",
    "charter", "đoàn", "đi đoàn",
    # 其他
    "guide", "hướng dẫn viên", "phiên dịch",
    # 英文（2026-07-14 补全）
    "flights", "airfare", "boarding pass", "e-ticket", "airline",
    "airport", "terminal", "immigration", "customs",
    "visa on arrival", "e-visa", "esta", "schengen", "visa free",
    "travel insurance", "hostel", "guesthouse", "booking",
    "check-in", "checkin", "accommodation",
    "car hire", "rental car", "scooter rental",
    "day tour", "guided tour", "tour package", "travel agent",
    "entry ticket", "attraction ticket", "theme park",
}

# ============ Destination 国家/地区名 ============

DESTINATION_KEYWORDS = {
    # 越南语国家名（注意单字 "nhật"/"hàn"/"thái" 会误中 "sinh nhật"/"hàn ngày"/"thái độ"
    #  所以只保留完整词）
    "nhật bản",
    "hàn quốc",
    "trung quốc",
    "đài loan",
    "hong kong", "hồng kông",
    "singapore", "sing-ga-po",
    "thái lan",
    "malaysia", "mã lai",
    "indonesia",
    "philippines", "phi-lip-pin",
    "campuchia", "lào",
    "ấn độ", "nepal",
    "úc đại lợi", "australia",
    "new zealand",
    "hoa kỳ", "america",
    "canada", "ca-na-đa",
    "anh quốc", "britain",
    "pháp", "đức", "ý", "tây ban nha", "bồ đào nha",
    "hà lan", "thụy sĩ", "thụy điển", "phần lan",
    "na uy", "đan mạch",
    "russia",
    "thổ nhĩ kỳ", "turkey",
    "ai cập", "egypt",
    "dubai", "qatar", "saudi",
    "châu âu", "europe",
    "châu á",
    # 城市（大多数没有歧义）
    "tokyo", "osaka", "kyoto", "đông kinh",
    "seoul", "busan",
    "bangkok", "phuket", "pattaya", "chiang mai",
    "bali", "jakarta",
    "manila", "cebu",
    "kuala lumpur",
    "đài bắc", "đài trung", "cao hùng",
    "thượng hải", "bắc kinh", "quảng châu", "thâm quyến",
    "ma cao", "macau",
    "paris", "london", "rome", "barcelona", "madrid",
    "amsterdam", "berlin", "munich",
    "new york", "los angeles", "san francisco", "las vegas",
    "sydney", "melbourne",
    # 越南国内
    "đà nẵng", "nha trang", "phú quốc", "hạ long",
    "sapa", "đà lạt", "hội an",
    # 中文国家名
    "日本", "韩国", "台湾", "香港", "新加坡",
    "泰国", "马来", "印尼", "菲律宾",
    "美国", "加拿大", "英国", "法国", "德国",
    "意大利", "西班牙", "瑞士", "荷兰",
    "澳洲", "纽西兰", "迪拜",

    # ─── 英文国家名 (2026-07-14 补全, 对齐已导入的英语群组覆盖的 42 国) ───
    # 注意: 匹配规则对 >=4 字符的词是 contains, 所以刻意不收
    # "oman"(会中 "roman")、"chad"、"mali" 这类会误伤普通英文词的短国名。
    "vietnam", "japan", "korea", "south korea", "china", "taiwan",
    "thailand", "cambodia", "laos", "myanmar", "brunei",
    "india", "nepal", "bhutan", "bangladesh", "sri lanka",
    "mongolia", "kazakhstan",
    "austria", "belgium", "netherlands", "switzerland", "germany",
    "france", "italy", "spain", "portugal", "greece", "croatia",
    "albania", "bosnia", "herzegovina", "bulgaria", "romania",
    "hungary", "czech", "poland", "slovenia", "slovakia",
    "norway", "sweden", "finland", "denmark", "iceland", "ireland",
    "scotland", "england", "united kingdom",
    "armenia", "georgia", "azerbaijan", "algeria", "andorra",
    "morocco", "tunisia", "jordan", "israel", "bahrain",
    "brazil", "argentina", "bolivia", "peru", "colombia", "mexico",
    "caribbean", "bahamas", "barbados", "antigua", "barbuda", "aruba",
    "united states",

    # ─── 英文城市/景点 ───
    "hanoi", "ho chi minh", "da nang", "ha long", "ninh binh",
    "shanghai", "beijing", "guangzhou", "hangzhou", "chengdu",
    "chongqing", "changsha", "yunnan", "kunming", "lijiang",
    "xi'an", "xinjiang", "tibet", "harbin", "zhangjiajie",
    "great wall", "forbidden city", "dunhuang",
    "ulaanbaatar",
    "jeju", "incheon", "gyeongju", "myeongdong", "gwangju",
    "taipei", "taichung", "kaohsiung", "alishan",
    "yokohama", "mount fuji", "shibuya", "shinjuku", "asakusa",
    "okinawa", "sapporo",
    "brisbane", "cairns", "adelaide", "tasmania", "uluru",
    "great barrier reef",
    "vienna", "salzburg", "innsbruck", "hallstatt",
    "hawaii", "oahu",
    "tirana", "berat", "gjirokaster", "sarande", "ksamil", "shkoder",
    "sardinia", "milan", "amalfi", "positano", "sorrento", "capri",
    "florence", "venice", "sicily", "palermo",
    "yerevan", "tbilisi", "baku",
    "nassau", "paradise island", "exumas",
    "brussels", "antwerp", "ghent", "bruges",
    "thimphu", "paro", "punakha",
    "rio de janeiro", "sao paulo",
    "buenos aires", "mendoza",
    "zagreb", "split", "dubrovnik", "zadar",
    "oranjestad", "eagle beach", "palm beach",
}

# ============ 业务/营销词（商家广告标识） ============
# ============ 卖家信号 (PIVOT 2026-09-16) ============
#
# 转型后要找的是**在群里卖东西的人** (旅行社/导游/司机/手机店/SIM 卖家)。
# 他们的广告帖未必带旅游词 —— "Available today, DM for rates" 里一个旅游
# 关键词都没有, 以前直接被拦在入库之前。
#
# 只收"在招揽生意"的信号, 不收泛泛的商业词 —— 后者会把群公告和活动贴也放进来。
SELLER_KEYWORDS = {
    # 英文: 报价 / 私信 / 预订招揽
    "dm me", "dm for", "inbox me", "inbox for", "pm me", "message me",
    "whatsapp me", "contact us", "contact me", "for rates", "for pricing",
    "for price", "price list", "best price", "book now", "booking now",
    "available now", "available today", "slots available", "seats available",
    "join us", "open trip", "open for booking", "we offer", "we provide",
    "we arrange", "we organize", "we organise", "our packages", "our services",
    "per pax", "per person rate", "group rate", "special rate",
    # 批发/代理 (最高价值 —— 对方直接在找供应商)
    "wholesale", "reseller", "distributor", "supplier", "bulk order",
    "dropship", "trade price", "agent price", "b2b",
    # 越南语
    "nhận đặt", "nhận tour", "báo giá", "bảng giá", "giá sỉ", "bán sỉ",
    "đại lý", "nhận khách", "liên hệ đặt",
    # 马来语/印尼语
    "harga", "boleh book", "sila pm", "sila whatsapp", "buka booking",
    "harga borong", "agen", "pengedar",
    # 中文
    "接单", "报价", "批发", "代理", "分销", "私信", "私訊", "欢迎咨询",
    # 泰语
    "ราคา", "จองเลย", "ทักแชท",
}

# 这些不是过滤白名单，而是辅助标识 is_business_account
BUSINESS_INDICATORS = {
    "chuyên", "uy tín", "đảm bảo",
    "ưu đãi", "khuyến mãi", "giá tốt", "giá rẻ",
    "inbox em", "ib em", "liên hệ", "zalo",
    "đậu rồi mới thanh toán",
}

# ============ 排除关键词（FB UI 元素，非帖子内容） ============

EXCLUDE_PATTERNS = [
    # FB UI 元素
    r"^打开.*的快拍",
    r"^未读快拍$",
    r"^抢先点赞吧",
    r"^已拒绝\s+详细了解",
    r"^•关注$",
    # ── 英文界面噪音 (2026-09-16) ──
    # 原来这 5 条全是中文, 而实测被拦下的噪音几乎都是英文界面文字。
    # 它们以前靠"没有关键词"被动挡掉; 加了 SELLER 白名单之后
    # ("book"/"available"/"contact" 这些词噪音里也有), 必须主动排除。
    r"^\s*\d+([.,]\d+)?[kmb]?\s*(members?|reactions?|comments?|shares?|likes?)\b",
    r"\breactions?,\s*\d+\s*comments?",          # "384 reactions, 8 comments, 7 shares"
    r"^\s*\d+\+?\s*posts? a day\b",
    r"^\s*repl(y|ying|ies) to\b",                 # "Replying to X" / "Replies to your comment"
    r"\blikes your (answer|comment|reply|post)\b",
    r"\breacted to your\b",
    r"^\s*(joined|is a member|are members)\b",
    r"\band \d+ (other )?friends? (is|are) a member\b",
    r"^\s*(see more|view more|show more|see translation)\s*$",
    r"^\s*(active|last active)\s+\d+",
    r"^\s*shared link:\s",
]

# 长度下限单独判 (见 filter_post) —— 不能用 `^.{0,15}$` 一刀切, 那是按拉丁文
# 定的, 会把完整的中文商家广告当碎片丢掉。
MIN_EFFECTIVE_LEN = 16

# 合并所有白名单关键词
ALL_KEYWORDS = (
    TRAVEL_KEYWORDS
    | CONNECTIVITY_KEYWORDS
    | SERVICE_KEYWORDS
    | DESTINATION_KEYWORDS
    | SELLER_KEYWORDS   # PIVOT 2026-09-16: 商家广告帖不带旅游词也要放进来
)


# CJK 区段: 中日韩统一表意文字 + 日文假名 + 韩文谚文。
_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def _effective_len(text: str) -> int:
    """按信息量估算长度 —— CJK 一个字约等于 2.5 个拉丁字符。

    原来的下限是 `^.{0,15}$` 一刀切, 按拉丁文定的。中文 13 个字
    ("接单：日本自由行，私信报价") 是一条完整的商家广告, 却被当碎片丢掉。
    """
    cjk = len(_CJK_CHAR.findall(text or ""))
    return len(text or "") + int(cjk * 1.5)


def _normalize(text: str) -> str:
    """小写化 + 去多余空白 + Unicode 兼容折叠。

    NFKC 是 2026-09-16 补的: 商家爱用数学粗体字吸睛 (💥𝗕𝗮𝗹𝗶 𝗕𝗲𝘀𝘁 𝗔𝗱𝘃𝗲𝗻𝘁𝘂𝗿𝗲),
    而 `𝗕𝗮𝗹𝗶` (U+1D5D5...) 与 `bali` 在字节层面毫无关系, 关键词一条都匹配不上 ——
    实测同一句话粗体版被拒、普通版通过。转型后这类"商家广告帖"正是我们要找的
    代理, 漏掉的是最对口的一批。
    NFKC 同时折叠全角/上下标, 对越南语·阿拉伯语·泰语等正常文本无损 (本就是 NFC)。
    先折叠再小写 —— 顺序反了的话部分字符折叠后又会带回大写。
    """
    folded = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", folded.lower().strip())


def filter_post(content: str) -> tuple[bool, list[str]]:
    """关键词预筛选

    Args:
        content: 帖子内容

    Returns:
        (should_pass, matched_keywords)
        - should_pass=True 表示应该继续处理
        - should_pass=False 表示直接 ignore
        - matched_keywords 是命中的关键词列表（供 reasoning 用）
    """
    if not content or not content.strip():
        return False, []

    # 太短 —— 按信息量算, CJK 一个字顶 2.5 个拉丁字符
    if _effective_len(content) < MIN_EFFECTIVE_LEN:
        return False, []

    # 排除明显的 FB UI 元素
    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return False, []

    normalized = _normalize(content)

    # 关键词匹配
    matched = []
    for kw in ALL_KEYWORDS:
        # 全词匹配（避免 "đi" 误中 "điều")
        # 但允许 contains 模式对于多字词（"khách sạn"）
        # CJK 词必须走 contains —— 中文/日文/韩文没有空格, \b 要求词的前后是
        # 非词字符, 而 CJK 文本里前后都是汉字/假名, 于是 2-3 字的 CJK 关键词
        # 永远匹配不上 (出国/旅游/签证/酒店… 长期全部失效, 2026-09-16 实测发现)。
        if " " in kw or len(kw) >= 4 or _CJK_CHAR.search(kw):
            # 多字词 / 长词 / CJK：contains 匹配
            if kw in normalized:
                matched.append(kw)
        else:
            # 短的拉丁词：用 word boundary 匹配, 避免 "đi" 误中 "điều"
            if re.search(rf"\b{re.escape(kw)}\b", normalized):
                matched.append(kw)
        # 早停：命中 3 个就够了
        if len(matched) >= 3:
            break

    return (len(matched) > 0, matched)


# ============ 测试入口 ============

if __name__ == "__main__":
    test_cases = [
        # 应该过的
        ("Em sắp đi Nhật 7 ngày, mua sim ở đâu rẻ ạ?", True),
        ("Vé máy bay đi Singapore tháng sau ai đi cùng không?", True),
        ("🇭🇰 CHUYÊN VISA HONG KONG", True),
        ("Khách sạn Bangkok review nào tốt?", True),
        ("Thuê xe máy đi Đà Nẵng giá bao nhiêu", True),
        ("Vé Universal Studios Singapore mua ở đâu?", True),
        # 不应该过的
        ("Chúc mừng sinh nhật chị Lan", False),
        ("Hôm nay mưa quá", False),
        ("Ai bán chó con không", False),
        ("打开Minh Truong的快拍, 未读快拍", False),
        ("已拒绝   详细了解", False),
        ("•关注", False),
        # 边缘情况
        ("Mất sim ở khách sạn phải làm sao?", True),  # 含 sim + khách sạn (但是丢失 sim 不是买)
    ]

    for content, expected in test_cases:
        passed, kws = filter_post(content)
        emoji = "✅" if passed == expected else "❌"
        print(f"{emoji} pass={passed} (expected {expected}) kws={kws[:3]} content='{content[:50]}...'")


# ============= Hotfix 3.16 H4: 强关键词直判 (跳过 LLM) =============
# 用简单 patterns, 优先稳定性

COMPETITOR_PATTERNS = [
    r'(bán|cung cấp|nhận order|nhận đặt|sẵn|sll|sỉ|dư|còn dư)\s+\S{0,30}(sim|esim|wifi|data|gb)',
    r'\bsim\s+(tour|tuor|du lịch|data|4g|5g)\b',
    r'\besim\s+(trung|nhật|hàn|thái|sing|đài|mỹ|úc|châu)',
    r'(zalo|liên hệ|inbox|ib\s*mình|sđt)\s*[:.\s]*\d{2,4}[.\s]\d{3,4}[.\s]\d{3,4}',
    r'\b0\d{2,3}[.\s]\d{3,4}[.\s]\d{3,4}\b',
    r'(shopee\.vn|lazada\.vn|tiki\.vn)',
    r'\b(mobifone|viettel|vinaphone|vietnamobile)\b.{0,40}\d+(k|gb|GB)',
    r'\d+\s*(gb|GB)\s*/\s*(ngày|tháng)',
    r'(book sim|đặt sim|order sim)',
    r'\b(klook|airalo|nomad|holafly|simoptions|3homo|jetpac|gigago)\b',
]

HOT_PATTERNS = [
    r'\b(sim|esim|wifi|mạng)\s+(nào|gì|tốt|rẻ|giá)\b',
    r'\b(mua|tìm|cần|nên dùng|gợi ý)\s+(sim|esim|wifi)\b',
    r'\b(sim|esim|wifi)\s+(ở đâu|chỗ nào|mua đâu)\b',
]

# V17: 营销广告关键词 (旅游/SIM 公司常用)
# 命中 → 直接判 cold (跳过 LLM 节省调用)
ADVERTISER_PATTERNS = [
    # "VI VU + 国家/SIM" - 营销开头
    r'\bvi vu\s+(trung|nhật|hàn|thái|sing|đài|mỹ|úc|châu|dubai|hong kong|malaysia)\b',
    r'\bvi vu\s+\S{0,20}(sim|esim|wifi)',
    # 营销话术 (强烈营销标志)
    r'\bphải có (sim|esim|wifi)\b',
    r'\bcần sim (xịn|tốt|mạnh)\b',
    r'\bkhỏi lo mất (sóng|mạng|kết nối)\b',
    r'\bkhông giật lag\b.*\b(sim|esim|wifi|tiktok)\b',
    r'\b(sim|esim) du lịch\b',
    # "đi là phải" 营销口号
    r'\bđi là phải\s+(có sim|có esim|có wifi|nhớ)\b',
    # 中文营销 (中文用户偶有)
    r'(出国|出境)\s*(必备|首选|推荐)\s*(sim|esim|wifi|卡)',
]


def has_advertiser_intent(content: str) -> bool:
    """V17: 检测营销广告意图 → 跳过 LLM, 直接 cold"""
    if not content:
        return False
    lower = content.lower()
    for pattern in ADVERTISER_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            return True
    return False



def has_strong_competitor_intent(content: str) -> bool:
    """检测明确 competitor 意图 → 跳过 LLM"""
    if not content:
        return False
    lower = content.lower()
    for pattern in COMPETITOR_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            return True
    return False


def has_strong_hot_intent(content: str) -> bool:
    """检测明确 hot 意图 → 跳过 LLM"""
    if not content:
        return False
    lower = content.lower()
    for pattern in HOT_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            return True
    return False



# ════════════════════════════════════════════════════════════
# P2: 越南国内目的地黑名单 — 不需要国际漫游 SIM
# ════════════════════════════════════════════════════════════

DOMESTIC_VN_DESTINATIONS = {
    "phú quốc", "phuquoc", "phu quoc",
    "đà nẵng", "danang", "da nang",
    "hạ long", "halong", "ha long",
    "sapa", "sa pa", "sa-pa",
    "đà lạt", "dalat", "da lat",
    "vũng tàu", "vungtau", "vung tau",
    "nha trang", "nhatrang",
    "hội an", "hoian", "hoi an",
    "mũi né", "muine", "mui ne",
    "huế", " hue",  # 加空格避免误匹配 "huệ" 等
    "hà nội", "hanoi", "ha noi",
    "sài gòn", "saigon", "sai gon",
    "hồ chí minh", "ho chi minh",
    "hà giang", "hagiang", "ha giang",
    "cát bà", "catba", "cat ba",
    "cần thơ", "cantho", "can tho",
    "côn đảo", "condao", "con dao",
    "phong nha", "phongnha",
    "tam đảo", "tamdao", "tam dao",
    "quy nhơn", "quynhon", "quy nhon",
    "ninh bình", "ninhbinh", "ninh binh",
    "lào cai", "laocai", "lao cai",
    "buôn ma thuột", "buon ma thuot",
    "thanh hóa", "thanh hoa",
    "nghệ an", "nghe an",
    "quảng bình", "quang binh",
    "quảng ninh", "quang ninh",
    "bình thuận", "binh thuan",
    "tây ninh", "tay ninh",
    "bà rịa", "ba ria",
    "long an",
    "an giang",
    "kiên giang", "kien giang",
}

# 国际目的地关键词 — 如果同时出现, 不算 domestic
INTERNATIONAL_DEST_MARKERS = [
    "đi nhật", "đi hàn", "đi đài", "đi trung", "đi mỹ", "đi sing",
    "đi thái", "đi malay", "đi indo", "đi úc", "đi châu âu",
    "đi pháp", "đi anh", "đi đức",
    "qua nhật", "qua hàn", "qua đài", "qua trung",
    "nhật bản", "hàn quốc", "đài loan", "trung quốc",
    "japan", "korea", "taiwan", "china", "thailand", "thai lan",
    "malaysia", "singapore", "indonesia", "australia",
    "europe", "châu âu", "france", "pháp", "germany", "đức",
    "tokyo", "osaka", "seoul", "busan",
    "đài bắc", "thượng hải", "bắc kinh", "hong kong",
    "bangkok", "kuala lumpur",
    "du học", "xkld", "xklđ", "xuất khẩu lao động",
    "cuộc sống bên", "ở nhật", "ở hàn", "ở đài", "ở trung",
    "visa nhật", "visa hàn", "visa mỹ",
    "vé bay đi", "vé máy bay đi",
]


def is_domestic_only(content: str) -> tuple[bool, str | None]:
    """
    检测视频/帖子是否仅涉及越南国内目的地.
    
    Return (is_domestic_only, matched_destination):
        True  → 内容仅提国内目的地, 没提国际目的地 → 应判 cold
        False → 含国际目的地, 或没提任何目的地 → 正常处理
    """
    lc = content.lower()
    
    # 找国内目的地
    matched_domestic = next((d for d in DOMESTIC_VN_DESTINATIONS if d in lc), None)
    if not matched_domestic:
        return (False, None)
    
    # 同时检查国际目的地, 同时出现则不算 domestic_only
    has_international = any(i in lc for i in INTERNATIONAL_DEST_MARKERS)
    if has_international:
        return (False, matched_domestic)
    
    return (True, matched_domestic.strip())


# ── Threads 多语言相关性粗筛 (en/vi/ja/ko/zh-TW/th)：连接 + 旅行 + 目的地 ──
# 命中任一词即放行交给 LLM；竞品/无关/品牌的精筛由 threads_reply prompt 负责
_THREADS_CONNECTIVITY = {
    "sim", "esim", "e-sim", "wifi", "wi-fi", "roaming", "hotspot", "mobile data",
    "4g", "5g", "lte", "prepaid", "internet", "data",
    "mạng", "sóng", "cục phát", "hòa mạng", "mất sóng",
    "ローミング", "通信", "データ", "回線",
    "ポケットwifi", "圏外",
    "유심", "이심", "와이파이", "로밍", "데이터", "선불",
    "網路", "上網", "漫遊", "流量", "數據", "網卡",
    "行動網路", "訊號", "上網卡", "儲值卡",
    "ซิม", "อินเตอร์เน็ต", "เน็ต",
    "โรมมิ่ง", "ไวไฟ", "สัญญาณ", "เติมเงิน",
}
_THREADS_TRAVEL = {
    "travel", "trip", "abroad", "overseas", "flight", "visa", "tour", "vacation",
    "holiday", "itinerary", "layover", "airport", "backpack",
    "du lịch", "du học", "nước ngoài", "chuyến đi", "vé máy bay",
    "sân bay", "xuất ngoại", "đi nhật", "đi hàn", "đi thái",
    "旅行", "海外", "観光", "出張", "航空券", "空港", "ビザ", "留学",
    "여행", "해외", "출장", "관광", "항공권", "공항", "비자", "유학",
    "旅遊", "出國", "自由行", "機票", "機場", "簽證", "跨團", "打工度假",
    "เที่ยว", "ท่องเที่ยว", "ต่างประเทศ",
    "ทริป", "ตั๋วเครื่องบิน", "สนามบิน", "วีซ่า",
}
_THREADS_DESTINATIONS = {
    "japan", "nhật bản", "日本", "일본", "ญี่ปุ่น",
    "korea", "hàn quốc", "韓國", "한국", "เกาหลี",
    "taiwan", "đài loan", "台灣", "대만", "ไต้หวัน",
    "china", "trung quốc", "中國", "중국", "จีน",
    "thailand", "thái lan", "泰國", "태국",
    "singapore", "新加坡", "싱가포르", "สิงคโปร์",
    "hong kong", "hồng kông", "香港", "홍콩",
    "malaysia", "馬來西亞", "indonesia", "bali", "philippines",
    "europe", "châu âu", "歐洲", "유럽", "ยุโรป",
    "france", "nước pháp", "法國", "italy", "義大利", "germany", "nước đức",
    "spain", "switzerland", "america", "nước mỹ", "美國", "미국",
    "australia", "nước úc", "澳洲", "호주", "dubai", "canada",
    "tokyo", "東京", "도쿄", "osaka", "大阪", "kyoto", "京都",
    "seoul", "首爾", "서울", "busan", "부산",
    "taipei", "台北", "bangkok", "กรุงเทพ", "曼谷", "방콕",
    "fuji", "富士山", "disney", "universal", "環球影城", "usj",
    "myeongdong", "명동", "shibuya", "渋谷", "西門町", "九份",
}
# ── 2026-08-08 数据驱动剪枝 ───────────────────────────────────────────────
# 依据：14 天 threads 全量 6444 条(381 条最终生成了回复 / 6063 条被 reply prompt
# 判定跳过)。对每个词统计"它是唯一命中词"的情况 —— 那正是移除它的边际影响。
# 下面这 9 个词单独放行了 549 条噪音, 且**从未**单独带来过一条回复。
# 实测: 剪枝后 KEEP 381/381 (一条线索都不丢), threads 送 LLM 的量降 8.5%。
#
# 刻意只从 TRAVEL/DEST 桶里剪, 且要求噪音贡献 >=10:
#   · CONNECTIVITY 是最强意图信号(eSIM/漫游/流量), 一律保留;
#   · 韩/泰/日语在这批 KEEP 里只有 3/1/6 条, 这些语种的词必然显示"零贡献",
#     那是小样本假象不是真没用 —— 按统计剪掉 "이심"(韩语 eSIM) 会直接砸掉韩国市场。
_THREADS_PRUNED = {
    "flight", "abroad", "visa", "itinerary", "vacation",
    "bali", "australia", "backpack", "overseas",
    # 2026-09-14 第二轮, 同样的 sole-match 口径 (近 30 天 12796 条):
    # 这 4 个词单独放行了 105 条噪音, 且从未单独带来过一条线索。
    # 仍然只从 TRAVEL/DEST 桶里剪 —— CONNECTIVITY 一律保留, 所以
    # 数据上同样"零贡献"的 lte(19) / 5g(12) 不动, 理由同 08-08 那条注释。
    "canada", "indonesia", "spain", "philippines",
}

# "sim" 是子串匹配, 会命中 simple/simply/similar/simulation(实测 92 条噪音)。
# 匹配前先把这些词抹掉 —— 不能直接给 "sim" 加词边界, 那样会丢掉 "esim"(444 条)。
_THREADS_NOISE = re.compile(
    r"simple|simpler|simplest|simply|similar|similarly|simulation|simulator",
    re.IGNORECASE,
)

_THREADS_BUCKET = (
    _THREADS_CONNECTIVITY | _THREADS_TRAVEL | _THREADS_DESTINATIONS
) - _THREADS_PRUNED


# ════════════════════════════════════════════════════════════
# 2026-09-14: Threads 排除层 —— 对标 Facebook 的 EXCLUDE_PATTERNS
#
# Facebook 那边一直有"UI 元素不是帖子正文"这道门 (EXCLUDE_PATTERNS),
# Threads 从来没有。后果实测 (近 30 天 12796 条):
#   · 18.9% 的入库内容根本没有正文, 只是抓到了作者头部行或回复上下文行
#   · 其中 1055 条最终**真的发到了 Threads 上** —— 评论是照着用户名瞎猜的:
#       原帖 'europe_simply'            → "Enjoy Europe! ... IntlRoam 的欧洲套餐…"
#       原帖 'chalosingaporemalaysia'   → "Malaysia and Singapore are both amazing!…"
#       原帖 'donnyjkimball …Japan 16m' → "Enjoy Japan! …"
#     这既是白花的 LLM 钱, 也是实打实的账号风险 —— 在毫不相关的帖子下留
#     "Enjoy Europe!" 正是平台判垃圾评论的特征。
#
# 收益 (以"reply prompt 最终有没有出文案"为标签):
#   砍掉无效 LLM 调用 2243 条 (占全部跳过的 19.5%);
#   被"误杀"的 171 条全部是没有正文的碎片, 本来就不该给它们写评论。
#
# ⚠️ 判据刻意避开"实义词数"这类长度指标: CJK 不按空格分词, 按词数判会把
#    '去日本玩六天，想請問SIM卡有推薦買哪個嗎？' 这种优质帖误杀。
#    所以只匹配三种确定无疑的碎片形态, 且带 CJK 的内容一律放行。
# ════════════════════════════════════════════════════════════

# 1. Threads 的"回复某人"上下文行, 不是帖子正文
_THREADS_FRAG_REPLY_TO = re.compile(r"^\s*reply to\s+\S+\s*$", re.IGNORECASE)

# 2. 光一个用户名, 没有任何句子
_THREADS_FRAG_HANDLE_ONLY = re.compile(r"^\s*[a-z0-9._]{3,40}\s*$", re.IGNORECASE)

# 3. 作者头部行: "<handle> [栏目] <相对时间戳>", 例 'donnyjkimball Japan 16m'
_THREADS_REL_TS_END = re.compile(r"\b\d{1,3}\s*[smhdw]\s*$", re.IGNORECASE)
_THREADS_HAS_CJK = re.compile(
    r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\u0e00-\u0e7f]"
)
_THREADS_SENTENCE_END = re.compile(r"[.!?。！？…]")
_THREADS_FRAG_MAX_LEN = 60   # 长正文偶尔也会以 '5d' 结尾, 加个长度闸

# 4. 已经提过自家品牌 —— reply prompt 的 already_intlroam, 实测 275 噪音 / 3 线索
_THREADS_OWN_BRAND = re.compile(r"intlroam", re.IGNORECASE)

# 5. 竞品 eSIM 品牌 (量不大, 但是纯噪音: 33 噪音 / 2 线索)
_THREADS_COMPETITORS = re.compile(
    r"airalo|holafly|nomad esim|saily|ubigi|gigsky|flexiroam|truphone|keepgo|"
    r"simoptions|drimsim|yesim|roamless|jetpac|maya mobile|alosim|instabridge|"
    r"firsty|mobimatter|sim local|simify|klook esim|ais sim2fly",
    re.IGNORECASE,
)


def _is_threads_fragment(text: str) -> bool:
    """抓到的不是帖子正文, 而是作者头部行 / 回复上下文行 / 光一个用户名。"""
    t = " ".join((text or "").split())
    if not t:
        return True
    if _THREADS_FRAG_REPLY_TO.match(t):
        return True
    if _THREADS_FRAG_HANDLE_ONLY.match(t):
        return True
    if (
        _THREADS_REL_TS_END.search(t)
        and not _THREADS_SENTENCE_END.search(t)
        and not _THREADS_HAS_CJK.search(t)
        and len(t) <= _THREADS_FRAG_MAX_LEN
    ):
        return True
    return False


def threads_exclude_reason(content: str) -> str | None:
    """命中排除层返回原因字符串, 否则 None。给日志和 reasoning 用。"""
    if _is_threads_fragment(content):
        return "no_post_body"
    if _THREADS_OWN_BRAND.search(content or ""):
        return "already_intlroam"
    if _THREADS_COMPETITORS.search(content or ""):
        return "competitor_brand"
    return None


def filter_post_threads(content: str) -> tuple[bool, list[str]]:
    """Threads 多语言相关性粗筛。命中任一词 -> (True, 命中词)，否则 (False, [])。
    自包含，CJK/泰语用子串匹配；精筛交给 threads_reply prompt。

    2026-09-14: 白名单之前先过一道排除层 (见上方注释)。"""
    if not content:
        return False, []
    if threads_exclude_reason(content) is not None:
        return False, []
    low = _THREADS_NOISE.sub(" ", content.lower())
    matched = [kw for kw in _THREADS_BUCKET if kw in low]
    return (len(matched) > 0, matched)


# ════════════════════════════════════════════════════════════
# PIVOT 2026-09-15: Threads/TikTok 默认走代理导向, 除非看得出真实需求
# ════════════════════════════════════════════════════════════

# 需求信号 —— 光出现 "wifi"/"data" 不算需求 (一篇讲旅行的文也会提),
# 必须同时有"在问/在找"的语气。多语言, 与 _THREADS_CONNECTIVITY 同覆盖面。
_DEMAND_SIGNAL = re.compile(
    r"\?|？"
    r"|\b(which|what|where|how|any|anyone|best|cheap(?:est)?|recommend\w*|suggest\w*"
    r"|need|looking for|help|advice|worth it|should i|can i|do i)\b"
    r"|nào|gì|ở đâu|nên|cần|tìm|giúp|khuyên"
    r"|どこ|どれ|おすすめ|教えて|必要"
    r"|어디|어떤|추천|필요"
    r"|哪个|哪裡|哪里|推荐|推薦|怎麼|怎么|需要|求助|求推"
    r"|ไหน|แนะนำ|ต้องการ",
    re.IGNORECASE,
)


def has_connectivity_demand(content: str) -> bool:
    """这条帖子是不是在**要**流量/SIM/eSIM。

    判据 = 连接性词 + 需求语气。两者缺一不可:
      "Which esim for Japan?"        -> True  (词 + 问号)
      "I used an esim in Japan"      -> False (只是提到, 不是在要)
      "Any recommendations?"         -> False (没有连接性词, 可能在问别的)
    判 False 的走代理导向话术, 判 True 的照旧走消费者话术。
    """
    if not content:
        return False
    low = _normalize(content)
    if not any(k in low for k in _THREADS_CONNECTIVITY):
        return False
    return bool(_DEMAND_SIGNAL.search(content))
