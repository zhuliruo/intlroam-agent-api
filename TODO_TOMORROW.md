# IntlRoam Agent - 明天 (2026-05-16) 待办

## P0 - 修复 V6 评论区扫描
当前 V6 BUG: collectComments 采集到 "Action chip profile picture" UI 元素 (不是真评论)
原因: V6 用 Button class + length>=12, 但未排除 FB UI 按钮

V7 方案:
1. text.length >= 5 (而非 20, 因为 "inbox" 等短评论也有效)
2. 严格排除 FB UI:
   - content-desc 含"按钮"/"button"/"nút"
   - content-desc 含 "profile picture" / "chip" / "avatar" / "头像"
   - 短文本 + 没空格/标点 = 可能是作者名 → 排除
3. 真评论特征: 
   - text 通常长 + 含空格/标点
   - content-desc 等于 text
   - long-clickable=true (评论本体)
4. 真实测试: 重新 UI dump 在评论区 看真评论的具体属性

## P1 - 新功能 1: 帖子时间识别 + 智能切群
当前: 没有判断帖子发布时间
新需求: 同群组连续 5 篇帖子超 7 天 → 停止采集该群组

实现:
- 解析帖子时间 ("1 phút"/"5 giờ"/"3 ngày"/"1 tuần"/"5月10日")
- raw_posts.post_created_at 字段填充
- 主循环计数: 连续 >7 天 → break + 上报"group_stale"
- 服务端调整: 该群组 cooldown 加长到 24h

## P1 - 新功能 2: 广告/赞助识别
新需求: 识别 "赞助"/"Được tài trợ"/"广告"/"Sponsored" 标识的帖子, 不点赞不评论

实现:
- 帖子卡片 traverse 时检测 "Được tài trợ"/"Sponsored"/"赞助" 文字
- 检测到 → mark is_ad=true → 跳过整个帖子 (不分析不评论)
- 服务端 raw_posts.is_ad 字段
