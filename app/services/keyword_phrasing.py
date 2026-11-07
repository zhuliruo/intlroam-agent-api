"""
app/services/keyword_phrasing.py
下发前把"裸词"扩写成搜索短语 (只影响 role=search 的下发, 不改库里的词)。

为什么在下发时做而不是改库:
  - 同一个词每次 session 换一种说法, 覆盖面比写死一条短语大;
  - 手机端 Settings 仍然只用敲 "tokyo" 这种最短的词, 录入不变;
  - 库里存的仍是规范化的裸词, 唯一键 (lower(keyword), language, role) 不受影响。

两类扩写:
  1. 裸地名 ("tokyo") → "visit Tokyo" / "travel to Tokyo" / "things to do in Tokyo"
     裸地名搜出来的是本地人日常/新闻/地名重名, 加旅行动词才是"要出门的人"。
  2. 裸主题词 ("esim" / "visa") → 绑一个国家: "esim for japan" / "visa for japan"
     绑国家而不是绑机型 —— "esim for samsung" 搜出来的是修手机/刷机内容, 实测无效。

只处理 language="en" (当前市场)。其它语言原样下发。
"""
import random

# ── 1. 裸地名模板 ────────────────────────────────────────────────
DESTINATION_TEMPLATES = [
    "visit {p}",
    "travel to {p}",
    "discover {p}",
    "trip to {p}",
    "things to do in {p}",
    "{p} travel tips",
    "{p} itinerary",
    "exploring {p}",
    "first time in {p}",
    "{p} travel guide",
    "flying to {p}",
    "{p} travel",
]

# ── 2. 裸主题词 → 主题 + 国家 ────────────────────────────────────
# 国家池: 用户点名的那批 + 几个高频出境目的地。
TOPIC_COUNTRIES = [
    "japan", "usa", "canada", "australia", "uk", "france", "germany", "italy",
    "korea", "singapore", "thailand", "indonesia", "vietnam", "china", "taiwan",
    "spain", "portugal", "turkey", "mexico", "philippines", "malaysia", "india",
    "new zealand", "switzerland", "netherlands", "greece", "dubai", "brazil",
]

# 每个主题词自己的说法 —— 不能共用一套模板 ("best visa for japan" 是病句)。
TOPIC_PHRASINGS: dict[str, list[str]] = {
    "esim": [
        "esim for {c}", "{c} esim", "esim in {c}",
        "best esim for {c}", "travel esim for {c}", "buy esim for {c}",
    ],
    "e-sim": [
        "esim for {c}", "{c} esim", "travel esim for {c}",
    ],
    "visa": [
        "visa for {c}", "{c} visa", "visa to {c}",
        "{c} visa requirements", "tourist visa for {c}", "how to apply {c} visa",
    ],
    "sim": [
        "sim card for {c}", "{c} sim card", "travel sim for {c}",
    ],
    "sim card": [
        "sim card for {c}", "{c} sim card", "prepaid sim card {c}",
    ],
    "data": [
        "mobile data in {c}", "data plan for {c}", "data roaming in {c}",
    ],
    "wifi": [
        "wifi in {c}", "pocket wifi {c}", "internet access in {c}",
    ],
    "internet": [
        "internet in {c}", "staying online in {c}", "internet for travel to {c}",
    ],
    "roaming": [
        "roaming in {c}", "roaming charges {c}", "avoid roaming fees in {c}",
    ],
    # 2026-08-09 随关键词导入补上。这两个是 category='generic', 不走
    # DESTINATION_TEMPLATES; 不挂模板就会**原样**下发裸词 "advice" / "recommendation",
    # 在 TikTok/Threads 上等于全网泛搜, 抓回来的几乎全是 not_travel 噪音。
    "advice": [
        "travel advice for {c}", "{c} travel advice", "any advice for {c}",
        "tips and advice for {c}",
    ],
    "recommendation": [
        "{c} travel recommendations", "recommendations for {c}",
        "any recommendations for {c}", "where to go in {c} recommendations",
    ],
}

# 出现这些词说明关键词已经是短语了, 不再套模板 (库里的 "esim japan" / "no wifi in japan")
_ALREADY_PHRASED = {
    "esim", "e-sim", "sim", "card", "visa", "travel", "trip", "visit", "tour",
    "guide", "tips", "itinerary", "wifi", "wi-fi", "data", "internet", "roaming",
    "flight", "flights", "in", "to", "for", "things", "how", "best", "cheap",
    "buy", "vs", "not", "no", "my", "abroad", "plan", "vpn",
}

# title() 会把 "usa" 变成 "Usa"、"tiger's nest" 变成 "Tiger'S Nest", 都不能用。
_UPPERCASE_PLACES = {"usa", "uk", "uae", "nyc", "la", "pek", "hk"}

# 地名里的连接词不大写 ("Rio de Janeiro", 不是 "Rio De Janeiro")
_LOWERCASE_PARTICLES = {"de", "da", "do", "del", "della", "di", "du", "des", "of",
                        "and", "the", "la", "le", "el", "los", "las", "on", "upon", "van", "von"}


def _pretty_place(place: str) -> str:
    """首字母大写, 但不动词内其它字符 (撇号/连字符原样); 连接词保持小写。"""
    if place in _UPPERCASE_PLACES:
        return place.upper()
    words = place.split()
    out = []
    for i, w in enumerate(words):
        if i > 0 and w in _LOWERCASE_PARTICLES:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(out)


def _is_bare_place(keyword: str) -> bool:
    """纯地名 = 没有任何主题/动词词根, 且不超过 4 个词。"""
    words = keyword.split()
    if not words or len(words) > 4:
        return False
    return not any(w in _ALREADY_PHRASED for w in words)


def phrase_search_keyword(
    keyword: str,
    category: str | None = None,
    language: str = "en",
    rng: random.Random | None = None,
) -> str:
    """把一个搜索词扩写成短语。不需要扩写时原样返回。"""
    if (language or "").lower() != "en":
        return keyword
    k = " ".join((keyword or "").strip().lower().split())
    if not k:
        return keyword
    r = rng or random

    # 裸主题词 → 主题 + 国家 (不管 category, 库里 "visa" 挂在 activity 下)
    phrasings = TOPIC_PHRASINGS.get(k)
    if phrasings:
        return r.choice(phrasings).format(c=r.choice(TOPIC_COUNTRIES))

    # 裸地名 → 旅行短语
    if (category or "") == "destination" and _is_bare_place(k):
        return r.choice(DESTINATION_TEMPLATES).format(p=_pretty_place(k))

    return keyword
