#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent 车队恢复情况日报 —— 一次性/按需跑, 结果发到 ALERT_EMAIL。

为什么不放在云端定时 agent 里: 数据在这台机的 Postgres 上, 云端环境既没有
SSH key 也连不到库。放在数据所在的机器上跑, 不用把任何凭据送出去。

用法: python3 /opt/intlroam_api/tools/agent_recovery_report.py [--stdout]
"""
import re
import ssl
import smtplib
import subprocess
import sys
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

ENV = Path("/opt/intlroam_api/.env")


def load_env() -> dict:
    """自己解析 .env —— 值可能带单引号, xargs 那套会把引号一起塞进去 (踩过)。"""
    out = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = re.sub(r"^['\"]|['\"]$", "", v.strip())
    return out


def psql(env: dict, sql: str) -> str:
    r = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-U", "intlroam", "-d", "intlroam", "-P", "pager=off", "-c", sql],
        capture_output=True, text=True,
        env={"PGPASSWORD": env["POSTGRES_PASSWORD"], "PATH": "/usr/bin:/bin"},
    )
    if r.returncode != 0:
        return f"[查询失败] {r.stderr.strip()[:300]}"
    return r.stdout.rstrip()


def fleet_versions() -> str:
    """心跳里才有 app_version (devices 表没有这列), 从 journald 捞每台机最后一次上报。"""
    r = subprocess.run(
        ["journalctl", "-u", "intlroam-api", "--since", "36 hours ago", "--no-pager"],
        capture_output=True, text=True,
    )
    last = {}
    for line in r.stdout.splitlines():
        if '"event": "heartbeat"' not in line:
            continue
        dev = re.search(r'"device_id": "([^"]+)"', line)
        ver = re.search(r'"app_version": "([^"]*)"', line)
        ts = re.search(r'"timestamp": "([^"]+)"', line)
        if dev:
            last[dev.group(1)] = (ver.group(1) if ver else "?", ts.group(1) if ts else "?")
    if not last:
        return "  (36h 内没有任何心跳)"
    rows = ["  %-18s %-8s %s" % ("device", "版本", "最后心跳(UTC)")]
    for dev, (ver, ts) in sorted(last.items(), key=lambda kv: kv[1][1], reverse=True):
        flag = "" if ver.startswith("1.59") else "   ← 未升级"
        rows.append("  %-18s %-8s %s%s" % (dev[:16], ver, ts, flag))
    return "\n".join(rows)


SECTIONS = [
    ("① FB 评论按天 (修复 1.59 于 09-20 12:5x 发版; 09-16 之前是正常水位)", """
SELECT date_trunc('day', attempt_at)::date AS d,
       count(*) FILTER (WHERE success) AS ok,
       count(*) FILTER (WHERE failure_stage='find_button') AS find_button,
       count(*) FILTER (WHERE success IS FALSE AND failure_stage<>'find_button') AS other_fail,
       count(*) AS total
FROM comment_attempts WHERE platform='facebook' AND attempt_at > now() - interval '8 days'
GROUP BY 1 ORDER BY 1;"""),

    ("② FB 锚点失败细分 (近 24h) —— 1.59 之后应以 no_anchor 为主, buttons_all_above_anchor 应显著下降", """
SELECT left(coalesce(failure_reason,'-'), 56) AS reason, count(*)
FROM comment_attempts
WHERE platform='facebook' AND failure_stage='find_button' AND attempt_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;"""),

    ("③ 各平台评论 近24h / 近48h", """
SELECT platform,
       count(*) FILTER (WHERE success AND attempt_at > now() - interval '24 hours') AS ok_24h,
       count(*) FILTER (WHERE NOT success AND attempt_at > now() - interval '24 hours') AS fail_24h,
       count(*) FILTER (WHERE success) AS ok_48h,
       count(*) FILTER (WHERE NOT success) AS fail_48h
FROM comment_attempts WHERE attempt_at > now() - interval '48 hours'
GROUP BY 1 ORDER BY 1;"""),

    ("④ 各平台发帖 近48h", """
SELECT platform, success, count(*) AS n, count(DISTINCT device_id) AS devices,
       left(string_agg(DISTINCT coalesce(fail_reason,''), ' | '), 70) AS reasons
FROM device_post_history WHERE posted_at > now() - interval '48 hours'
GROUP BY 1,2 ORDER BY 1,2;"""),

    ("⑤ 开不开得了 App (近24h, facebook_not_open 是 1.59 重点修的那个)", """
SELECT device_id, coalesce(failure_reason,'-') AS reason, count(*) AS n, max(created_at) AS last
FROM device_group_visits WHERE created_at > now() - interval '24 hours' AND failure_reason IS NOT NULL
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12;"""),

    ("⑥ 有效产出 (近24h 真正浏览到帖子的访问)", """
SELECT v.device_id, count(*) AS visits, sum(v.posts_scanned) AS scanned,
       (SELECT count(*) FROM comment_attempts c
         WHERE c.device_id = v.device_id AND c.platform='facebook'
           AND c.attempt_at > now() - interval '24 hours') AS fb_cmt_try
FROM device_group_visits v
WHERE v.created_at > now() - interval '24 hours' AND v.action_taken <> 'aborted'
GROUP BY 1 ORDER BY 3 DESC;"""),
]


def build_report(env: dict) -> str:
    parts = ["IntlRoam agent 车队 —— 恢复情况报告", "=" * 62, ""]
    for title, sql in SECTIONS:
        parts += [title, psql(env, sql), ""]
    parts += ["⑦ 车队版本与心跳 (1.59 = 本次修复版)", fleet_versions(), ""]
    parts += [
        "判读要点:",
        "  - FB 评论 ok 回到两位数/天 = 锚点修复生效 (修复前 09-17~09-19 每天只有 2 条)",
        "  - ⑤ 里 facebook_not_open 明显减少 = 开 App 超时 10s→25s 生效",
        "  - 仍停在 1.43 的机器说明 OTA 没人点确认, 需要人去手机上装",
    ]
    return "\n".join(parts)


def main() -> None:
    env = load_env()
    body = build_report(env)
    if "--stdout" in sys.argv:
        print(body)
        return
    to = env.get("ALERT_EMAIL")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "[Agent] 车队恢复情况报告 (FB 评论 / 开 App / 发帖)"
    msg["From"] = env["SMTP_FROM"]
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    # .env 里只写了 SMTP_USER/FROM/PASSWORD, host/port 用的是 app/config.py 的默认值
    host = env.get("SMTP_HOST", "smtp-relay.brevo.com")
    port = int(env.get("SMTP_PORT", 587))
    with smtplib.SMTP(host, port, timeout=25) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        s.sendmail(env["SMTP_FROM"], [to], msg.as_string())
    print(f"报告已发往 {to}")


if __name__ == "__main__":
    main()
