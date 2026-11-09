"""partner_offer — 招募分销商时可以说的价格例子 + 介绍材料链接 (2026-11-07)

用户 11-07: 问对方有没有兴趣做分销时, 带上日本/泰国等主推产品的 eSIMHub starter 价当例子;
有人要介绍材料就发 10-30 做的 PDF (英/越/阿三份)。

价格 = eSIMHub starter 等级 (成本×1.25+0.20), 与介绍材料 PDF 同一口径; 11-07 用公开接口
api.intlroam.com/items/products 复核过 (bundle 135/109/112), 与 10-30 一致。
TSIM 调价后要同步改这里 (以及 PDF)。VND 按 26000。
"""
from __future__ import annotations

import re

# (英文描述, 越南语描述, USD, VND 文字)
PRICE_EXAMPLES = [
    ("Japan 7 days, 30GB/day (local DOCOMO network)", "Nhật Bản 7 ngày, 30GB/ngày (mạng DOCOMO)", "8.95", "233.000đ"),
    ("Thailand 10 days, 50GB + local calls (DTAC)", "Thái Lan 10 ngày, 50GB + gọi nội địa (DTAC)", "5.57", "145.000đ"),
    ("Singapore/Malaysia/Thailand/Indonesia/Hong Kong 30 days, 300GB (5G)",
     "Singapore/Malaysia/Thái Lan/Indonesia/Hồng Kông 30 ngày, 300GB (5G)", "15.83", "412.000đ"),
]
ALLOWED_AMOUNTS = {p[2] for p in PRICE_EXAMPLES} | {re.sub(r"\D", "", p[3]) for p in PRICE_EXAMPLES}

BROCHURE_BASE = "https://agent.intlroam.com/brochures/"
BROCHURES = {
    "en": BROCHURE_BASE + "IntlRoam-Partner-Brochure-EN.pdf",
    "vi": BROCHURE_BASE + "IntlRoam-eSIM-Vietnam-VI.pdf",
    "ar": BROCHURE_BASE + "IntlRoam-eSIM-Oman-AR.pdf",
}
_ARABIC_REGIONS = {"OM", "AE", "SA", "QA", "BH", "KW", "JO", "EG", "MA"}

# 11-09 用户: 除了介绍材料, 也引导对方去 eSIMHub 注册 (邮箱验证即激活), 登录后看全部产品和自己等级的价格
SIGNUP_BASE = "https://esimhub.link/{lang}/register"


def _lang(platform: str, region: str | None) -> str:
    if platform == "zalo" or region == "VN":
        return "vi"
    return "ar" if region in _ARABIC_REGIONS else "en"


def signup_url(platform: str, region: str | None) -> str:
    return SIGNUP_BASE.format(lang=_lang(platform, region)) + f"?utm_source=agent&utm_medium={platform}"


def brochure_url(platform: str, region: str | None) -> str:
    return BROCHURES[_lang(platform, region)]


def price_lines(vietnamese: bool) -> str:
    if vietnamese:
        return "\n".join(f"  - {vi}: {vnd} (~${usd})" for _, vi, usd, vnd in PRICE_EXAMPLES)
    return "\n".join(f"  - {en}: ${usd}" for en, _, usd, _ in PRICE_EXAMPLES)


_MONEY_RE = re.compile(r"(?:[$€£]\s?\d[\d.,]*|\d[\d.,]*\s?(?:usd|vnd|đ|dollars?|k\b))", re.I)


def prices_ok(msg: str) -> bool:
    """消息里出现的每个金额都必须是 PRICE_EXAMPLES 里的; 不许出现百分比 (佣金比例由同事给)。"""
    if "%" in msg:
        return False
    for m in _MONEY_RE.finditer(msg):
        digits = re.sub(r"[^\d.]", "", m.group(0)).strip(".")
        plain = re.sub(r"\D", "", m.group(0))
        if digits not in ALLOWED_AMOUNTS and plain not in ALLOWED_AMOUNTS:
            return False
    return True


def strip_links_except(msg: str, allowed: str | list[str]) -> str:
    """去掉所有链接, 给定的链接 (介绍材料 / eSIMHub 注册页) 除外。"""
    ok = {allowed} if isinstance(allowed, str) else set(allowed)

    bases = {re.sub(r"^https?://", "", a.split("?")[0]).rstrip("/").lower(): a for a in ok}

    def sub(m):
        u = m.group(0).rstrip(".,)")
        tail = m.group(0)[len(u):]
        if u in ok or u.lower() in ("intlroam.com", "www.intlroam.com"):
            return m.group(0)
        # LLM 常把 ?utm_… 吞掉或去掉 https:// → 认出是哪条允许的链接就换回标准写法
        base = re.sub(r"^https?://", "", u.split("?")[0]).rstrip("/").lower()
        return bases[base] + tail if base in bases else ""
    out = re.sub(r"https?://\S+|www\.\S+|\b[\w-]+\.(?:com|link|net)\b\S*", sub, msg)
    return re.sub(r"\s{2,}", " ", out).strip()
