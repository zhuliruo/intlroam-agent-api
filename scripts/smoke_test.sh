#!/usr/bin/env bash
# ====================================================================
# smoke_test.sh — 部署后快速验证 5 个端点
# 用法：bash scripts/smoke_test.sh [base_url]
#        默认 base_url = http://127.0.0.1:8000
# ====================================================================
set -uo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
DEVICE_ID="phone_smoke_$(date +%s)"

GREEN='\033[32m'
RED='\033[31m'
YELLOW='\033[33m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; FAILS=$((FAILS + 1)); }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }

FAILS=0

echo "=================================================="
echo " IntlRoam API v2 Smoke Test"
echo " Target: $BASE_URL"
echo " Device ID: $DEVICE_ID"
echo "=================================================="
echo ""

# ---------- 1. /health ----------
echo "▶ [1/5] GET /health"
RESP=$(curl -s -o /tmp/smoke.json -w "%{http_code}" "$BASE_URL/health")
if [[ "$RESP" == "200" ]]; then
    cat /tmp/smoke.json | python3 -m json.tool
    if grep -q '"status": "ok"' /tmp/smoke.json; then
        pass "health = ok"
    else
        warn "health 返回 200 但 status 不是 ok"
    fi
else
    fail "health HTTP $RESP"
fi
echo ""

# ---------- 2. /api/v2/heartbeat ----------
echo "▶ [2/5] POST /api/v2/heartbeat"
RESP=$(curl -s -o /tmp/smoke.json -w "%{http_code}" \
    -X POST "$BASE_URL/api/v2/heartbeat" \
    -H "Content-Type: application/json" \
    -H "X-Device-Id: $DEVICE_ID" \
    -d '{"battery": 80, "status": "idle", "today_stats": {"posts_scanned": 0}}')
if [[ "$RESP" == "200" ]]; then
    cat /tmp/smoke.json | python3 -m json.tool
    pass "heartbeat OK"
else
    fail "heartbeat HTTP $RESP"
    cat /tmp/smoke.json
fi
echo ""

# ---------- 3. /api/v2/device_config ----------
echo "▶ [3/5] GET /api/v2/device_config"
RESP=$(curl -s -o /tmp/smoke.json -w "%{http_code}" \
    -H "X-Device-Id: $DEVICE_ID" \
    "$BASE_URL/api/v2/device_config")
if [[ "$RESP" == "200" ]]; then
    cat /tmp/smoke.json | python3 -m json.tool
    pass "device_config OK（应该看到自动注册的设备）"
else
    fail "device_config HTTP $RESP"
fi
echo ""

# ---------- 4. /api/v2/next_groups ----------
echo "▶ [4/5] GET /api/v2/next_groups?count=3"
RESP=$(curl -s -o /tmp/smoke.json -w "%{http_code}" \
    -H "X-Device-Id: $DEVICE_ID" \
    "$BASE_URL/api/v2/next_groups?platform=facebook&count=3")
if [[ "$RESP" == "200" ]]; then
    cat /tmp/smoke.json | python3 -m json.tool
    GROUP_COUNT=$(python3 -c "import json; print(len(json.load(open('/tmp/smoke.json'))['groups']))")
    if [[ "$GROUP_COUNT" -gt 0 ]]; then
        pass "next_groups 返回 $GROUP_COUNT 个群组"
    else
        warn "next_groups 返回 0 个群组（数据库还没有 verified 群组，先跑 seed_groups.py）"
    fi
else
    fail "next_groups HTTP $RESP"
fi
echo ""

# ---------- 5. /api/v2/analyze_post ----------
echo "▶ [5/5] POST /api/v2/analyze_post（会调 Gemini，需要有 GEMINI_API_KEY）"
RESP=$(curl -s -o /tmp/smoke.json -w "%{http_code}" \
    -X POST "$BASE_URL/api/v2/analyze_post" \
    -H "Content-Type: application/json" \
    -H "X-Device-Id: $DEVICE_ID" \
    -d '{
        "content": "Em sắp đi Nhật 7 ngày, mua sim ở đâu rẻ ạ?",
        "platform": "facebook"
    }')
if [[ "$RESP" == "200" ]]; then
    cat /tmp/smoke.json | python3 -m json.tool
    INTENT=$(python3 -c "import json; print(json.load(open('/tmp/smoke.json'))['intent'])")
    if [[ "$INTENT" == "hot" ]]; then
        pass "analyze_post 正确识别为 hot"
    else
        warn "intent 是 $INTENT（预期 hot）"
    fi
else
    fail "analyze_post HTTP $RESP"
    cat /tmp/smoke.json
fi
echo ""

echo "=================================================="
if [[ $FAILS -eq 0 ]]; then
    echo -e "${GREEN}✅ 所有测试通过${NC}"
else
    echo -e "${RED}❌ $FAILS 项失败${NC}"
fi
echo "=================================================="
exit $FAILS
