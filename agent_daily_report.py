#!/usr/bin/env python3
"""
社媒 agent 每日运营日报 —— 每天 17:00 HKT 发送。

统计窗口 = 过去 24 小时 (到发送时刻为止)。17:00 发、回看 24h, 覆盖的是
"昨天傍晚场 + 今天白天场"; work_hours 是 [[8,12],[14,22]] HKT, 所以这个
窗口正好装下一整个工作日的量, 只是相位偏了。

内容:
  1. 汇总卡片 (评论/发帖/警告数, 带昨日环比)
  2. 设备 × 平台矩阵 —— 每台机在每个平台发了几条帖、几条评论、成功率
  3. 警告区 —— shadow_ban / 发帖连败 / 空跑 / 逼近日限 / 选择器失效
  4. 失败原因 TOP
  5. 线索漏斗与补发状态

用法:
    python3 agent_daily_report.py            # 发信
    python3 agent_daily_report.py --dry-run  # 只打印摘要, 不发信
    python3 agent_daily_report.py --html     # 把 HTML 打到 stdout

环境变量 (从 /opt/intlroam_api/.env 加载):
    POSTGRES_* , SMTP_USER, SMTP_PASSWORD, SMTP_FROM, ALERT_EMAIL
"""
from __future__ import annotations

import os
import smtplib
import sys
import re
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg

HKT = timezone(timedelta(hours=8))
WINDOW_HOURS = 24

# 警告阈值 —— 超过就进"需要处理"区
TH_SHADOW_BAN = 3        # 单机单日 shadow_ban 条数
TH_ABORTED = 5           # 单机空跑 (打不开 App) 次数
TH_POST_FAILS = 2        # 单机单平台发帖失败数 (且 0 成功) 即视为链路坏了
TH_LIMIT_RATIO = 0.9     # 评论尝试达到日限的这个比例就提醒

SELECTOR_STAGES = ("find_button", "find_input", "find_send", "open_comment")


# 三张发帖表的统一视图。FB 群帖/个人帖的平台记作 facebook / facebook_me,
# 矩阵里能和 FB 评论放在同一台机下面看。
ALL_POSTS_SQL = """
    SELECT device_id::text, platform, posted_at, success, fail_reason
      FROM device_post_history
    UNION ALL
    SELECT device_id::text, 'facebook', posted_at, success,
           coalesce(failure_stage || ': ', '') || coalesce(failure_reason, '')
      FROM group_posts
    UNION ALL
    SELECT device_id::text, 'facebook_me', posted_at, success,
           coalesce(failure_stage || ': ', '') || coalesce(failure_reason, '')
      FROM personal_posts
"""


# ────────────────────────────────────────────────────────────────
# 基础设施
# ────────────────────────────────────────────────────────────────
def _strip(v: str) -> str:
    """.env 里的值可能带引号 (POSTGRES_PASSWORD 就是), systemd 会剥, 手工读不会。"""
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def connect():
    return psycopg.connect(
        host=_strip(os.environ.get("POSTGRES_HOST", "127.0.0.1")),
        port=int(_strip(os.environ.get("POSTGRES_PORT", "5432")) or 5432),
        dbname=_strip(os.environ.get("POSTGRES_DB", "intlroam")),
        user=_strip(os.environ.get("POSTGRES_USER", "intlroam")),
        password=_strip(os.environ.get("POSTGRES_PASSWORD", "")),
    )


def send_email(to_email: str, subject: str, html: str) -> None:
    user = _strip(os.environ.get("SMTP_USER", ""))
    password = _strip(os.environ.get("SMTP_PASSWORD", ""))
    sender = _strip(os.environ.get("SMTP_FROM", "support@intlroam.com"))
    if not (user and password):
        raise RuntimeError(
            "SMTP_USER / SMTP_PASSWORD 没配 —— 在 /opt/intlroam_api/.env 里补上 "
            "(值同 FR 的 /opt/intlroam/.env)"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"IntlRoam Agent <{sender}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))

    server = smtplib.SMTP("smtp-relay.brevo.com", 587, timeout=30)
    server.starttls()
    server.login(user, password)
    server.sendmail(sender, to_email, msg.as_string())
    server.quit()


def q(cur, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


# ────────────────────────────────────────────────────────────────
# 取数
# ────────────────────────────────────────────────────────────────
def collect(cur, since: datetime, until: datetime) -> dict:
    prev_since = since - timedelta(hours=WINDOW_HOURS)
    w = (since, until)
    d: dict = {}

    # 设备名
    d["names"] = {r[0]: (r[1] or r[0]) for r in
                  q(cur, "SELECT device_id, name FROM devices")}

    # 各机装的 APK 版本 (心跳时从 User-Agent 落进 Redis, 见 monitoring.heartbeat)。
    # 车队版本不一致是**真实故障源**: 2026-09-15 查到 5a5cf 的 TikTok 发送失败
    # 与 9ed4 的成功, 差别就在装没装新包 —— 当时服务端读不到版本, 只能靠行为反推。
    # 2026-09-26: 主口径改成 devices 表 (心跳落库, 不过期)。Redis 那份只当兜底 ——
    # 本次改动之前上报的版本只在 Redis 里, 且监控面板还在读它。
    d["versions"] = {}
    d["version_codes"] = {}
    d["version_seen"] = {}
    for dev, ver, code, at in q(cur, """
        SELECT device_id, app_version, app_version_code, app_version_at
        FROM devices WHERE app_version IS NOT NULL
    """):
        d["versions"][dev] = ver
        d["version_codes"][dev] = code
        d["version_seen"][dev] = at
    try:
        import redis as _redis
        _r = _redis.Redis(host=_strip(os.environ.get("REDIS_HOST", "127.0.0.1")),
                          port=int(_strip(os.environ.get("REDIS_PORT", "6379")) or 6379),
                          db=int(_strip(os.environ.get("REDIS_DB", "0")) or 0),
                          socket_timeout=3)
        for dev in d["names"]:
            if dev in d["versions"]:
                continue        # 库里有就以库为准
            v = _r.get(f"dev_app_ver:{dev}")
            if v:
                d["versions"][dev] = v.decode() if isinstance(v, bytes) else str(v)
    except Exception:
        pass   # 读不到版本不该让日报发不出去

    # ── 评论 ──
    d["comments"] = q(cur, """
        SELECT device_id, platform,
               count(*) FILTER (WHERE success)                          AS ok,
               count(*) FILTER (WHERE NOT success)                      AS fail,
               count(*) FILTER (WHERE failure_stage = 'shadow_ban')     AS shadow,
               count(*) FILTER (WHERE failure_stage = ANY(%s))          AS selector,
               count(*) FILTER (WHERE failure_stage = 'already_commented') AS already,
               count(*) FILTER (WHERE success AND reply_text ILIKE '%%intlroam%%') AS branded
        FROM comment_attempts
        WHERE attempt_at >= %s AND attempt_at < %s
        GROUP BY 1, 2
    """, (list(SELECTOR_STAGES), *w))

    # ── 发帖 ──
    # 2026-11-02 修: 以前只查 device_post_history —— 那张表只有 TikTok/Threads 的
    # 主页帖。FB 群发帖在 group_posts、FB 个人主页帖在 personal_posts, 日报从来没算,
    # 每天少报 ~25 条 (报 9-10, 实际 ~35)。success IS NULL = 已派发未回执, 不计。
    d["posts"] = q(cur, f"""
        SELECT device_id, platform,
               count(*) FILTER (WHERE success)     AS ok,
               count(*) FILTER (WHERE NOT success) AS fail
        FROM ({ALL_POSTS_SQL}) p
        WHERE posted_at >= %s AND posted_at < %s
        GROUP BY 1, 2
    """, w)

    # ── 品牌提及率按 平台 × 意图 拆开 ──
    # hot = 直客话术, warm = 招商(B2B)话术, 两者目标和自然基线完全不同,
    # 混成一个数会让人误判 (2026-11-02 总数 20-30% 几乎全是 warm 拉低的)。
    d["brand_split"] = q(cur, """
        SELECT ca.platform,
               CASE WHEN rp.intent = 'warm' THEN 'warm' ELSE 'hot' END AS kind,
               count(*) AS n,
               count(*) FILTER (WHERE ca.reply_text ~* 'intl\\s*roam') AS branded
        FROM comment_attempts ca
        LEFT JOIN raw_posts rp ON rp.id = ca.raw_post_id
        WHERE ca.success AND ca.attempt_at >= %s AND ca.attempt_at < %s
        GROUP BY 1, 2 ORDER BY 1, 2
    """, w)

    # ── 设备在线 (当下快照) ──
    # 只看 7 天内出现过的机器, 早已退役的测试机不该天天报。
    d["last_seen"] = {r[0]: r[1] for r in q(cur, """
        SELECT device_id, last_seen_at FROM devices
        WHERE last_seen_at >= now() - interval '7 days'
    """)}

    # ── 群访问 / 空跑 ──
    d["visits"] = q(cur, """
        SELECT device_id, platform,
               count(*)                                                AS visits,
               count(*) FILTER (WHERE NOT verified)                    AS aborted,
               coalesce(sum(posts_scanned), 0)                         AS scanned
        FROM device_group_visits
        WHERE created_at >= %s AND created_at < %s
        GROUP BY 1, 2
    """, w)

    # ── 失败原因 TOP ──
    d["fail_reasons"] = q(cur, """
        SELECT platform,
               coalesce(failure_stage, '-')          AS stage,
               left(coalesce(failure_reason, '-'), 64) AS reason,
               count(*)                              AS n
        FROM comment_attempts
        WHERE attempt_at >= %s AND attempt_at < %s AND NOT success
        GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 10
    """, w)

    d["post_fail_reasons"] = q(cur, f"""
        SELECT device_id, platform, coalesce(fail_reason, '-') AS reason, count(*) AS n
        FROM ({ALL_POSTS_SQL}) p
        WHERE posted_at >= %s AND posted_at < %s AND NOT success
        GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 10
    """, w)

    # ── 线索漏斗 ──
    d["funnel"] = q(cur, """
        SELECT platform,
               count(*)                                          AS scraped,
               count(*) FILTER (WHERE intent IN ('hot','warm'))   AS relevant,
               count(*) FILTER (WHERE suggested_reply IS NOT NULL
                                 AND suggested_reply <> '')       AS generated,
               count(*) FILTER (WHERE action_status = 'replied')  AS replied
        FROM raw_posts
        WHERE captured_at >= %s AND captured_at < %s
        GROUP BY 1 ORDER BY 2 DESC
    """, w)

    # ── 补发状态 (当下快照, 不是窗口内) ──
    row = q(cur, """
        SELECT count(*) FILTER (WHERE action_status = 'pending'
                                 AND captured_at >= now() - interval '24 hours') AS in_window,
               count(*) FILTER (WHERE action_status = 'pending'
                                 AND captured_at <  now() - interval '24 hours') AS overdue,
               count(*) FILTER (WHERE action_status = 'expired'
                                 AND captured_at >= %s)                          AS expired_today
        FROM raw_posts
        WHERE suggested_reply IS NOT NULL AND suggested_reply <> ''
    """, (since,))
    d["stranded"] = row[0] if row else (0, 0, 0)

    # ── 昨日同窗口 (环比) ──
    prev = q(cur, """
        SELECT count(*) FILTER (WHERE success)                      AS ok,
               count(*) FILTER (WHERE failure_stage = 'shadow_ban') AS shadow
        FROM comment_attempts
        WHERE attempt_at >= %s AND attempt_at < %s
    """, (prev_since, since))
    d["prev_comments"] = prev[0] if prev else (0, 0)

    prev_p = q(cur, f"""
        SELECT count(*) FILTER (WHERE success) FROM ({ALL_POSTS_SQL}) p
        WHERE posted_at >= %s AND posted_at < %s
    """, (prev_since, since))
    d["prev_posts_ok"] = prev_p[0][0] if prev_p else 0

    # ── 日限用量 ──
    # ⚠️ 必须用 UTC 自然日, 不能用报表这个滚动 24h 窗口 —— 服务端闸门
    # (device_guard.comment_attempts_today) 就是按 UTC 日算的, 拿滚动窗口
    # 的数去比那个上限是两个口径, 会报出"已达 134%"这种不存在的超限。
    d["fb_limit"] = int(_strip(os.environ.get("MAX_DAILY_COMMENTS_FB", "50")) or 50)
    d["today_fb"] = {r[0]: r[1] for r in q(cur, """
        SELECT device_id, count(*)
        FROM comment_attempts
        WHERE platform = 'facebook'
          AND attempt_at >= date_trunc('day', %s AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
          AND attempt_at <  date_trunc('day', %s AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '1 day'
        GROUP BY 1
    """, (since + timedelta(hours=12), since + timedelta(hours=12)))}

    # ── 代理线索 (PIVOT 2026-09-15, 2026-09-16 收窄到 facebook) ──
    # warm 已重定义为"代理线索"(旅行社/导游/本地 SIM·eSIM 卖家), 走 B2B 话术。
    # 这里把过去 24h 真正**发出去**的招商评论拉成一张名单, 供人工接手私信。
    #
    # ⚠️ 只收 facebook。原来不分平台, 结果 threads/tiktok 把名单挤爆了:
    #   - 它们走短路, intent_confidence 是写死的 0.85~0.95, 而排序按置信度降序,
    #     于是假置信度把 facebook 的真线索挤到后面 (实测 40 条里只剩 8 条 fb)
    #   - 它们没有 group_id → 拼不出深链; 也没有 author_name → 显示"(未采到)"
    #     40 条里 32 条既没名字也没链接, 人工根本无法接手
    #   - 按业务规则, threads/tiktok 的 warm 只是"默认按代理导向推广",
    #     不代表对方真是卖家; 只有 facebook 的分类器在识别"这个人在卖东西"
    # 它们的招商评论量单独统计 (见 leads_other), 不混进名单。
    #
    # 排序也换了: 先按"能不能接手"(有作者名 > 能拼链接), 再按置信度。
    d["leads"] = q(cur, r"""
        SELECT rp.device_id,
               coalesce(nullif(rp.author_name, ''), '(未采到)'),
               coalesce(g.name, g.external_id, '?'),
               left(rp.content, 180),
               coalesce(rp.actual_reply_text, rp.suggested_reply, ''),
               rp.intent_confidence,
               rp.replied_at,
               g.external_id,
               rp.fb_post_url,
               (rp.content ~ '(…|\.\.\.)\s*(more|更多|xem thêm)$') AS truncated
        FROM raw_posts rp
        LEFT JOIN groups g ON g.id = rp.group_id
        WHERE rp.intent = 'warm'
          AND rp.action_status = 'replied'
          AND rp.platform = 'facebook'
          AND rp.replied_at >= %s AND rp.replied_at < %s
        ORDER BY (rp.author_name IS NOT NULL) DESC,
                 (g.external_id IS NOT NULL) DESC,
                 rp.intent_confidence DESC NULLS LAST,
                 rp.replied_at DESC
        LIMIT 40
    """, w)

    # threads/tiktok 的招商评论量 —— 只报数字, 不进线索名单 (理由见上)
    d["leads_other"] = q(cur, """
        SELECT platform, count(*)
        FROM raw_posts
        WHERE intent = 'warm' AND action_status = 'replied'
          AND platform <> 'facebook'
          AND replied_at >= %s AND replied_at < %s
        GROUP BY 1 ORDER BY 2 DESC
    """, w)

    # ── FB 互动回复 (2026-11-03): 私信 / 回复我们评论的人 / 评论我们帖子的人 ──
    # 表可能还没建 (老库), 查不到就当 0, 不让整份日报挂掉
    try:
        d["engage"] = q(cur, """
            SELECT kind,
                   count(*) FILTER (WHERE status = 'sent')    AS sent,
                   count(*) FILTER (WHERE status = 'failed')  AS failed,
                   count(*) FILTER (WHERE status = 'skipped') AS skipped,
                   count(*) FILTER (WHERE status = 'sent' AND brand) AS branded
            FROM fb_engagements
            WHERE created_at >= %s AND created_at < %s
            GROUP BY 1
        """, w)
        # 私信: 自动回过第一句, 待人工接手 (不限窗口 —— 没处理完的一直挂着, 最多 7 天)
        # 私信多轮自动回复 (11-03 起): 只有转人工 (handover) 的会话需要人接手
        d["engage_dms"] = q(cur, """
            SELECT device_id, counterpart, left(their_text, 200), left(our_reply, 300),
                   coalesce(sent_at, updated_at), turns, coalesce(destination, '')
            FROM fb_engagements
            WHERE kind = 'dm' AND handover AND human_status = 'pending'
              AND updated_at >= now() - interval '7 days'
            ORDER BY updated_at DESC LIMIT 40
        """)
        d["engage_dm_convos"] = q(cur, """
            SELECT count(*), coalesce(sum(turns), 0)
            FROM fb_engagements
            WHERE kind = 'dm' AND updated_at >= %s AND updated_at < %s
        """, w)[0]
    except Exception:
        cur.connection.rollback()
        d["engage"], d["engage_dms"], d["engage_dm_convos"] = [], [], (0, 0)

    return d


# ────────────────────────────────────────────────────────────────
# 分析 → 警告
# ────────────────────────────────────────────────────────────────
def build_warnings(d: dict) -> list[tuple[str, str, str]]:
    """返回 [(严重度, 设备, 说明)]，严重度 = high | med"""
    out: list[tuple[str, str, str]] = []
    short = lambda x: x[:8]

    # (2026-11-02: 掉线告警按用户要求去掉 —— 工作机每天只在线几小时, 不在线是常态)

    for dev, plat, ok, fail, shadow, selector, already, branded in d["comments"]:
        if shadow >= TH_SHADOW_BAN:
            out.append(("high", short(dev),
                        f"{plat} shadow ban {shadow} 次（评论写进去了但看不见）"))
        total = ok + fail
        if total and selector / total >= 0.3 and selector >= 5:
            out.append(("med", short(dev),
                        f"{plat} 选择器失效 {selector}/{total}（找不到评论按钮/输入框）"))
    # 日限只看 UTC 自然日 —— 和服务端闸门同口径 (见 collect 里的注释)
    for dev, used in d["today_fb"].items():
        if used >= d["fb_limit"]:
            out.append(("med", short(dev),
                        f"facebook 当日(UTC)已用满 {used}/{d['fb_limit']} 条，派发已被拦停"))
        elif used >= d["fb_limit"] * TH_LIMIT_RATIO:
            out.append(("med", short(dev),
                        f"facebook 当日(UTC) {used}/{d['fb_limit']} 条，接近日限"))

    for dev, plat, ok, fail in d["posts"]:
        if ok == 0 and fail >= TH_POST_FAILS:
            out.append(("high", short(dev),
                        f"{plat} 发帖 {fail} 次全失败、0 成功 —— 这条链路是坏的"))

    for dev, plat, visits, aborted, scanned in d["visits"]:
        if aborted >= TH_ABORTED:
            out.append(("high", short(dev),
                        f"{plat} 空跑 {aborted}/{visits} 次（打不开 App，已触发熔断）"))
        elif visits >= 10 and scanned == 0:
            out.append(("med", short(dev),
                        f"{plat} 访问了 {visits} 个群但一条帖子都没扫到"))

    # 车队版本不一致 —— 修复装了一半是最难查的状态
    vers = {v for v in d["versions"].values() if v}
    if len(vers) > 1:
        # 谁落后要按 **versionCode** 判, 不能按 versionName 字符串:
        # max(["1.9","1.10"]) == "1.9" —— 会把最新的那台说成落后的。
        codes = {dev: c for dev, c in d.get("version_codes", {}).items() if c}
        if codes and len(codes) == len([v for v in d["versions"].values() if v]):
            newest = max(codes.values())
            stale = sorted({dev[:8] for dev, c in codes.items() if c < newest})
            basis = f"versionCode 最新 {newest}"
        else:
            # 车队里还有没带 code 的老客户端 —— 退回按版本名列出, 但说清是哪种口径
            stale = sorted({dev[:8] for dev, v in d["versions"].items()
                            if v != max(vers)})
            basis = "按版本名粗判(部分机器是旧客户端,没上报 versionCode)"
        if stale:
            out.append(("med", "—",
                        f"车队 APK 版本不一致 [{basis}]: {sorted(vers)} —— "
                        f"落后的机器: {', '.join(stale)}"))

    # 从没上报过版本的活跃设备。以前这行算出来就扔了 (死代码), 于是"一台机装的哪个包
    # 完全不知道"这件事在日报里毫无痕迹 —— 而它比"版本落后"更值得查:
    # 要么这台机压根没心跳(装死了), 要么 UA 被改坏了。
    # 2026-11-02: 只看 7 天内出现过的机器 —— 以前把 5 月就退役的测试机天天报一遍, 纯噪音。
    missing = sorted(dev[:8] for dev in d.get("last_seen", {}) if dev not in d["versions"])
    if missing:
        out.append(("med", "—",
                    f"{len(missing)} 台设备从未上报过 APK 版本: {', '.join(missing)} "
                    f"—— 要么没心跳, 要么 User-Agent 不对"))

    overdue = d["stranded"][1]
    if overdue >= 20:
        out.append(("med", "—",
                    f"{overdue} 条已生成的回复超过 24h 仍未发出（补发任务应已标记过期，"
                    f"若持续增长说明补发没跑通）"))

    order = {"high": 0, "med": 1}
    out.sort(key=lambda x: order[x[0]])
    return out


# ────────────────────────────────────────────────────────────────
# 渲染
# ────────────────────────────────────────────────────────────────
CARD = ("<div style='background:{bg};border:1px solid {bd};border-radius:12px;"
        "padding:14px 20px;flex:1;min-width:110px;'>"
        "<p style='color:#6b7280;font-size:12px;margin:0;'>{label}</p>"
        "<p style='color:{fg};font-size:26px;font-weight:700;margin:4px 0 0;'>{value}</p>"
        "<p style='color:#9ca3af;font-size:11px;margin:2px 0 0;'>{sub}</p></div>")

TH = ("<th style='padding:9px 8px;text-align:{align};border-bottom:2px solid #e5e7eb;"
      "font-size:12px;color:#374151;'>{t}</th>")
TD = ("<td style='padding:8px;border-bottom:1px solid #f1f5f9;font-size:13px;"
      "text-align:{align};color:{color};'>{v}</td>")


def _lead_link(external_id, post_text) -> str:
    """拼一条能落到该帖子的群内搜索深链。

    取帖子正文前 8 个词做搜索词 —— 太短会搜出一片, 太长 FB 反而匹配不上。
    去掉 emoji 和换行, 它们进 URL 只会干扰匹配。
    """
    if not external_id or not post_text:
        return ""
    gid = str(external_id).strip()
    # 纯数字群号才拼得出 /groups/<id>/; 自定义短链 (如 tourtooman) 同样可用
    words = re.sub(r"[^\w\s'-]", " ", str(post_text), flags=re.UNICODE).split()
    q = " ".join(words[:8]).strip()
    if not q:
        return ""
    return ("https://www.facebook.com/groups/" + quote(gid, safe="")
            + "/search/?q=" + quote(q, safe=""))


def _other_platform_line(d: dict) -> str:
    """threads/tiktok 的招商评论量 —— 只报数字。

    它们没有作者名也没有可点链接, 放进名单只会把 facebook 的真线索挤掉
    (实测 40 条里只剩 8 条 fb), 所以单独列一行。
    """
    rows = d.get("leads_other") or []
    if not rows:
        return ""
    parts = " · ".join(f"{p} {n} 条" for p, n in rows)
    return ("<p style='color:#94a3b8;font-size:12px;margin:4px 0 10px;'>"
            "另有 " + _esc(parts) + " 招商留言（这两个平台按规则默认走代理导向，"
            "对方未必真是卖家，且没有作者名/链接可接手，故不列入名单）</p>")


def _esc(v) -> str:
    """线索栏渲染的是**别人写的帖子原文**, 必须转义 —— 否则一个 < 就能把邮件排版
    打烂, 帖子里带 HTML 的情况在旅游群里并不罕见。"""
    return (str(v or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _delta(cur_v: int, prev_v: int) -> str:
    if prev_v == 0:
        return "昨日 0"
    diff = cur_v - prev_v
    sign = "+" if diff > 0 else ""
    return f"昨日 {prev_v}（{sign}{diff}）"


def _brand_sub(d: dict) -> str:
    """直客(hot) / 招商(warm) 两种话术的品牌率分开报, 再附各平台。"""
    agg: dict[str, list[int]] = {}
    per_plat: dict[str, list[int]] = {}
    for plat, kind, n, br in d.get("brand_split", []):
        a = agg.setdefault(kind, [0, 0]); a[0] += n; a[1] += br
        p = per_plat.setdefault(plat, [0, 0]); p[0] += n; p[1] += br
    pct = lambda v: f"{v[1] / v[0]:.0%}" if v[0] else "—"
    head = f"直客 {pct(agg.get('hot', [0, 0]))} · 招商 {pct(agg.get('warm', [0, 0]))}"
    plats = " ".join(f"{p[:2]}{pct(v)}" for p, v in sorted(per_plat.items()))
    return f"{head}<br>{plats}" if plats else head


def render(d: dict, since: datetime, until: datetime, warnings: list) -> str:
    names = d["names"]

    c_ok = sum(r[2] for r in d["comments"])
    c_fail = sum(r[3] for r in d["comments"])
    c_shadow = sum(r[4] for r in d["comments"])
    c_branded = sum(r[7] for r in d["comments"])
    p_ok = sum(r[2] for r in d["posts"])
    p_fail = sum(r[3] for r in d["posts"])
    v_abort = sum(r[3] for r in d["visits"])
    v_total = sum(r[2] for r in d["visits"])

    prev_ok, prev_shadow = d["prev_comments"]
    brand_pct = f"{c_branded / c_ok:.0%}" if c_ok else "—"
    shadow_pct = f"{c_shadow / (c_ok + c_fail):.1%}" if (c_ok + c_fail) else "—"
    abort_pct = f"{v_abort / v_total:.0%}" if v_total else "—"

    cards = (
        CARD.format(bg="#f0fdf4", bd="#bbf7d0", fg="#16a34a", label="评论成功",
                    value=c_ok, sub=_delta(c_ok, prev_ok)) +
        CARD.format(bg="#eff6ff", bd="#bfdbfe", fg="#2563eb", label="发帖成功",
                    value=p_ok, sub=_delta(p_ok, d["prev_posts_ok"])) +
        CARD.format(bg="#fef2f2" if c_shadow else "#f8fafc",
                    bd="#fecaca" if c_shadow else "#e5e7eb",
                    fg="#dc2626" if c_shadow else "#6b7280",
                    label="Shadow Ban", value=c_shadow,
                    sub=f"占尝试 {shadow_pct} · {_delta(c_shadow, prev_shadow)}") +
        CARD.format(bg="#fffbeb" if v_abort else "#f8fafc",
                    bd="#fde68a" if v_abort else "#e5e7eb",
                    fg="#d97706" if v_abort else "#6b7280",
                    label="空跑访问", value=v_abort,
                    sub=f"占群访问 {abort_pct}") +
        CARD.format(bg="#f8fafc", bd="#e5e7eb", fg="#374151", label="品牌提及率",
                    value=brand_pct, sub=f"{c_branded}/{c_ok} 条 · {_brand_sub(d)}")
    )

    # ── 警告区 ──
    if warnings:
        rows = ""
        for sev, dev, text in warnings:
            color = "#dc2626" if sev == "high" else "#d97706"
            tag = "严重" if sev == "high" else "注意"
            rows += (
                "<tr>"
                + TD.format(align="left", color=color, v=f"<strong>{tag}</strong>")
                + TD.format(align="left", color="#111827", v=dev)
                + TD.format(align="left", color="#374151", v=text)
                + "</tr>"
            )
        warn_html = (
            "<h2 style='color:#dc2626;font-size:17px;margin:28px 0 12px;'>⚠️ 需要处理 ("
            + str(len(warnings)) + ")</h2>"
            "<table width='100%' style='border-collapse:collapse;'>"
            "<thead><tr style='background:#fef2f2;'>"
            + TH.format(align="left", t="级别") + TH.format(align="left", t="设备")
            + TH.format(align="left", t="说明")
            + "</tr></thead><tbody>" + rows + "</tbody></table>"
        )
    else:
        warn_html = (
            "<div style='margin:28px 0 12px;padding:16px 20px;background:#f0fdf4;"
            "border-left:4px solid #16a34a;border-radius:6px;'>"
            "<p style='margin:0;color:#166534;font-size:14px;'>✅ 没有需要处理的告警</p></div>"
        )

    # ── 设备 × 平台矩阵 ──
    by_key: dict[tuple[str, str], dict] = {}
    for dev, plat, ok, fail, shadow, selector, already, branded in d["comments"]:
        by_key.setdefault((dev, plat), {}).update(
            c_ok=ok, c_fail=fail, shadow=shadow, selector=selector, branded=branded)
    for dev, plat, ok, fail in d["posts"]:
        by_key.setdefault((dev, plat), {}).update(p_ok=ok, p_fail=fail)
    for dev, plat, visits, aborted, scanned in d["visits"]:
        by_key.setdefault((dev, plat), {}).update(
            visits=visits, aborted=aborted, scanned=scanned)

    matrix_rows = ""
    for (dev, plat) in sorted(by_key, key=lambda k: (names.get(k[0], k[0]), k[1])):
        m = by_key[(dev, plat)]
        c_o, c_f = m.get("c_ok", 0), m.get("c_fail", 0)
        rate = f"{c_o / (c_o + c_f):.0%}" if (c_o + c_f) else "—"
        sh = m.get("shadow", 0)
        ab = m.get("aborted", 0)
        matrix_rows += (
            "<tr>"
            + TD.format(align="left", color="#111827",
                        v=f"<code style='font-size:12px;'>{dev[:8]}</code>")
            + TD.format(align="left", color="#6b7280",
                        v=d["versions"].get(dev, "?"))
            + TD.format(align="left", color="#374151", v=plat)
            + TD.format(align="right", color="#2563eb", v=m.get("p_ok", 0))
            + TD.format(align="right", color="#dc2626" if m.get("p_fail") else "#9ca3af",
                        v=m.get("p_fail", 0))
            + TD.format(align="right", color="#16a34a", v=c_o)
            + TD.format(align="right", color="#dc2626" if c_f else "#9ca3af", v=c_f)
            + TD.format(align="right", color="#374151", v=rate)
            + TD.format(align="right", color="#dc2626" if sh else "#9ca3af", v=sh)
            + TD.format(align="right", color="#d97706" if ab else "#9ca3af", v=ab)
            + TD.format(align="right", color="#6b7280", v=m.get("scanned", 0))
            + "</tr>"
        )

    matrix_html = (
        "<h2 style='color:#111827;font-size:17px;margin:28px 0 12px;'>📱 设备 × 平台</h2>"
        "<table width='100%' style='border-collapse:collapse;'>"
        "<thead><tr style='background:#f8fafc;'>"
        + TH.format(align="left", t="设备") + TH.format(align="left", t="APK")
        + TH.format(align="left", t="平台")
        + TH.format(align="right", t="发帖✓") + TH.format(align="right", t="发帖✗")
        + TH.format(align="right", t="评论✓") + TH.format(align="right", t="评论✗")
        + TH.format(align="right", t="成功率") + TH.format(align="right", t="ShadowBan")
        + TH.format(align="right", t="空跑") + TH.format(align="right", t="扫帖")
        + "</tr></thead><tbody>" + (matrix_rows or
          "<tr><td colspan='11' style='padding:16px;color:#9ca3af;font-size:13px;'>"
          "窗口内没有任何设备活动</td></tr>") + "</tbody></table>"
    )

    # ── 漏斗 ──
    funnel_rows = ""
    for plat, scraped, relevant, generated, replied in d["funnel"]:
        conv = f"{replied / scraped:.1%}" if scraped else "—"
        funnel_rows += (
            "<tr>"
            + TD.format(align="left", color="#374151", v=plat)
            + TD.format(align="right", color="#6b7280", v=scraped)
            + TD.format(align="right", color="#6b7280", v=relevant)
            + TD.format(align="right", color="#2563eb", v=generated)
            + TD.format(align="right", color="#16a34a", v=replied)
            + TD.format(align="right", color="#6b7280", v=conv)
            + "</tr>"
        )
    in_win, overdue, expired_today = d["stranded"]
    funnel_html = (
        "<h2 style='color:#111827;font-size:17px;margin:28px 0 12px;'>🔎 线索漏斗</h2>"
        "<table width='100%' style='border-collapse:collapse;'>"
        "<thead><tr style='background:#f8fafc;'>"
        + TH.format(align="left", t="平台") + TH.format(align="right", t="抓取")
        + TH.format(align="right", t="相关") + TH.format(align="right", t="生成文案")
        + TH.format(align="right", t="已发出") + TH.format(align="right", t="转化率")
        + "</tr></thead><tbody>" + (funnel_rows or
          "<tr><td colspan='6' style='padding:16px;color:#9ca3af;font-size:13px;'>"
          "窗口内没有抓取</td></tr>") + "</tbody></table>"
        "<p style='margin:10px 0 0;color:#6b7280;font-size:12px;'>"
        f"待补发（24h 窗口内）<strong>{in_win}</strong> 条 · "
        f"超期未发 <strong>{overdue}</strong> 条 · 今日标记过期 <strong>{expired_today}</strong> 条"
        "</p>"
    )

    # ── 失败原因 ──
    fr_rows = ""
    for plat, stage, reason, n in d["fail_reasons"]:
        fr_rows += ("<tr>"
                    + TD.format(align="left", color="#374151", v=plat)
                    + TD.format(align="left", color="#111827", v=stage)
                    + TD.format(align="left", color="#6b7280", v=reason)
                    + TD.format(align="right", color="#dc2626", v=n)
                    + "</tr>")
    for dev, plat, reason, n in d["post_fail_reasons"]:
        fr_rows += ("<tr>"
                    + TD.format(align="left", color="#374151", v=f"{plat}（发帖）")
                    + TD.format(align="left", color="#111827", v=dev[:8])
                    + TD.format(align="left", color="#6b7280", v=reason)
                    + TD.format(align="right", color="#dc2626", v=n)
                    + "</tr>")
    fail_html = (
        "<h2 style='color:#111827;font-size:17px;margin:28px 0 12px;'>🧩 失败原因</h2>"
        "<table width='100%' style='border-collapse:collapse;'>"
        "<thead><tr style='background:#f8fafc;'>"
        + TH.format(align="left", t="平台") + TH.format(align="left", t="环节/设备")
        + TH.format(align="left", t="原因") + TH.format(align="right", t="次数")
        + "</tr></thead><tbody>" + (fr_rows or
          "<tr><td colspan='4' style='padding:16px;color:#9ca3af;font-size:13px;'>"
          "窗口内没有失败</td></tr>") + "</tbody></table>"
    )

    # ── 代理线索名单 (半自动 DM 的人工清单) ──────────────────────
    leads = d.get("leads") or []
    if leads:
        rows = []
        for dev, author, grp, post, reply, conf, at, ext_id, permalink, truncated in leads:
            # 优先用客户端取到的永久链接 —— 群内搜索链接实地验证会跳到别的帖,
            # 人工点过去看不到我们的留言, 会误以为"没发出去"。
            link = permalink or _lead_link(ext_id, post)
            # 真永久链接标成"直达", 搜索链接标成"搜索" —— 后者只能大致定位
            _label = "→ 打开这条帖子" if permalink else "→ 在群里搜这条帖"
            link_html = ("<br><a href='" + _esc(link) + "' style='color:#2563eb;"
                         "font-size:11px;text-decoration:none;'>" + _label + "</a>"
                         ) if link else ""
            rows.append(
                "<tr style='border-top:1px solid #e5e7eb;'>"
                "<td style='padding:9px 10px;vertical-align:top;white-space:nowrap;'>"
                "<b style='color:#0f172a;'>" + _esc(author) + "</b><br>"
                "<span style='color:#94a3b8;font-size:11px;'>"
                + _esc(names.get(dev, dev)) + " · " + f"{(conf or 0):.2f}" + "</span>"
                + link_html + "</td>"
                "<td style='padding:9px 10px;vertical-align:top;color:#475569;font-size:12px;'>"
                + _esc(grp) + "</td>"
                "<td style='padding:9px 10px;vertical-align:top;color:#334155;font-size:12px;'>"
                + _esc(post)
                # 正文被 "See more" 截断 —— 分类只看到半句话, 值得点开确认。
                # 实测日报 27 条线索里 9 条不该进名单, 其中 5 条正是截断的。
                + ("<br><span style='color:#b45309;font-size:11px;'>"
                   "⚠️ 正文被折叠，判断只基于这半句</span>" if truncated else "")
                + "</td>"
                "<td style='padding:9px 10px;vertical-align:top;color:#0369a1;font-size:12px;"
                "background:#f0f9ff;'>" + _esc(reply) + "</td></tr>"
            )
        noname = sum(1 for r in leads if r[1] == "(未采到)")
        hint = ("<p style='color:#b45309;font-size:12px;margin:0 0 10px;'>⚠️ "
                + str(noname) + " / " + str(len(leads)) +
                " 条没采到作者名，无法定位到人 —— 需要新版 APK（作者名锚点修复）。</p>"
                ) if noname else ""
        leads_html = (
            "<h2 style='font-size:15px;color:#0f172a;margin:26px 0 6px;'>"
            "🎯 代理线索 · " + str(len(leads)) + " 条<span style='font-weight:400;"
            "color:#94a3b8;font-size:12px;'>（Facebook，已留言，待人工私信接手）"
            "</span></h2>" + _other_platform_line(d)
            + hint +
            "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
            "<tr style='background:#f1f5f9;text-align:left;'>"
            "<th style='padding:8px 10px;'>对方</th><th style='padding:8px 10px;'>群组</th>"
            "<th style='padding:8px 10px;'>他发的帖</th>"
            "<th style='padding:8px 10px;'>我们的留言</th></tr>"
            + "".join(rows) + "</table>"
        )
    else:
        leads_html = (
            "<h2 style='font-size:15px;color:#0f172a;margin:26px 0 6px;'>🎯 代理线索"
            "<span style='font-weight:400;color:#94a3b8;font-size:12px;'>"
            "（Facebook）</span></h2>"
            "<p style='color:#94a3b8;font-size:12px;margin:0;'>"
            "过去 24 小时 Facebook 没有发出招商留言。</p>" + _other_platform_line(d)
        )

    # ── FB 互动回复 ──
    _kind_cn = {"dm": "私信回复", "comment_reply": "回复我们评论的人", "post_comment": "回复评论我们帖子的人"}
    eg = {r[0]: r for r in d.get("engage", [])}
    eg_line = " · ".join(
        f"{_kind_cn[k]} <b>{eg[k][1]}</b>" + (f"<span style='color:#dc2626;'>（失败 {eg[k][2]}）</span>" if eg[k][2] else "")
        for k in ("dm", "comment_reply", "post_comment") if k in eg) or "今天没有新的互动推送"
    c_sent = sum(eg[k][1] for k in ("comment_reply", "post_comment") if k in eg)
    c_brand = sum(eg[k][4] for k in ("comment_reply", "post_comment") if k in eg)
    if c_sent:
        eg_line += f" · 评论类品牌率 {c_brand / c_sent:.0%}"
    _conv, _turns = d.get("engage_dm_convos", (0, 0))
    if _conv:
        eg_line += f" · 私信会话 {_conv} 个（累计自动回复 {_turns} 条）"
    dm_rows = "".join(
        "<tr style='border-top:1px solid #e5e7eb;'>"
        "<td style='padding:8px 10px;vertical-align:top;white-space:nowrap;'><b>" + _esc(who) + "</b><br>"
        "<span style='color:#94a3b8;font-size:11px;'>" + _esc(names.get(dev, dev)) + " · "
        + (at.astimezone(HKT).strftime("%m-%d %H:%M") if at else "")
        + f" · 自动回了 {turns} 条" + (" · " + _esc(dest) if dest else "") + "</span></td>"
        "<td style='padding:8px 10px;vertical-align:top;color:#334155;font-size:12px;'>" + _esc(theirs) + "</td>"
        "<td style='padding:8px 10px;vertical-align:top;color:#0369a1;font-size:12px;background:#f0f9ff;'>"
        + _esc(ours) + "</td></tr>"
        for dev, who, theirs, ours, at, turns, dest in d.get("engage_dms", []))
    engage_html = (
        "<h2 style='font-size:15px;color:#0f172a;margin:26px 0 6px;'>💬 互动回复"
        "<span style='font-weight:400;color:#94a3b8;font-size:12px;'>（Facebook / Messenger）</span></h2>"
        "<p style='color:#475569;font-size:13px;margin:0 0 10px;'>" + eg_line + "</p>"
        + (("<p style='color:#b45309;font-size:12px;margin:0 0 8px;'>以下私信已转人工（客户要付款 / 订单问题 / "
            "要真人 / 问批发，或自动回复已达上限），请在对应手机的 Messenger 里接手：</p>"
            "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
            "<tr style='background:#f1f5f9;text-align:left;'><th style='padding:8px 10px;'>对方</th>"
            "<th style='padding:8px 10px;'>对方说</th><th style='padding:8px 10px;'>我们回的</th></tr>"
            + dm_rows + "</table>") if dm_rows else "")
    )

    return (
        "<html><body style='margin:0;padding:0;background:#f1f5f9;"
        "font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,sans-serif;'>"
        "<div style='max-width:860px;margin:0 auto;background:#fff;'>"
        "<div style='background:#1e293b;padding:28px 40px;'>"
        "<h1 style='color:#fff;margin:0;font-size:23px;'>Social Agent Daily Report</h1>"
        "<p style='color:#94a3b8;margin:8px 0 0;font-size:13px;'>"
        + since.astimezone(HKT).strftime("%Y-%m-%d") + " 全天（HKT 00:00 → 24:00）</p>"
        "</div>"
        "<div style='padding:28px 40px;'>"
        "<div style='display:flex;gap:12px;margin-bottom:8px;flex-wrap:wrap;'>"
        + cards + "</div>"
        + warn_html + leads_html + engage_html + matrix_html + funnel_html + fail_html +
        "</div>"
        "<div style='background:#f8fafc;padding:18px 40px;text-align:center;"
        "border-top:1px solid #e5e7eb;'>"
        "<p style='color:#9ca3af;font-size:11px;margin:0;'>"
        "intlroam-api @ HK · 数据源 comment_attempts / device_post_history / "
        "device_group_visits / raw_posts</p>"
        "</div></div></body></html>"
    )


# ────────────────────────────────────────────────────────────────
def main() -> int:
    dry = "--dry-run" in sys.argv
    html_only = "--html" in sys.argv

    # 2026-11-02: 统计区间改成 HKT 自然日 0-24 点 (用户要求)。默认报"昨天",
    # cron 改到早上发; --date YYYY-MM-DD 可补报任意一天。
    day = None
    for a in sys.argv[1:]:
        if a.startswith("--date="):
            day = datetime.strptime(a.split("=", 1)[1], "%Y-%m-%d").date()
    if day is None:
        day = (datetime.now(HKT) - timedelta(days=1)).date()
    since = datetime(day.year, day.month, day.day, tzinfo=HKT).astimezone(timezone.utc)
    until = since + timedelta(hours=WINDOW_HOURS)

    with connect() as conn, conn.cursor() as cur:
        d = collect(cur, since, until)

    warnings = build_warnings(d)
    html = render(d, since, until, warnings)

    c_ok = sum(r[2] for r in d["comments"])
    p_ok = sum(r[2] for r in d["posts"])
    c_shadow = sum(r[4] for r in d["comments"])

    subject = (f"Agent 日报 {since.astimezone(HKT).strftime('%Y-%m-%d')} — "
               f"评论 {c_ok} · 发帖 {p_ok}")
    if c_shadow:
        subject += f" · shadow ban {c_shadow}"
    high = sum(1 for w in warnings if w[0] == "high")
    if high:
        subject += f" · ⚠️ {high} 项待处理"

    if html_only:
        print(html)
        return 0

    print(f"[{datetime.now(HKT):%Y-%m-%d %H:%M:%S}] {subject}")
    for sev, dev, text in warnings:
        print(f"  [{sev:4}] {dev:10} {text}")

    if dry:
        print("--dry-run: 未发信")
        return 0

    to = _strip(os.environ.get("ALERT_EMAIL", "")) or "support@intlroam.com"
    send_email(to, subject, html)
    print(f"已发送 → {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
