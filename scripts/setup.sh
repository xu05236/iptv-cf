#!/usr/bin/env bash
# ============================================================
# iptv-cf 一键部署脚本（Cloudflare 侧）
# 用法：在项目根目录运行  bash scripts/setup.sh
# 前置：nodejs 18+ 已安装；wrangler 已登录（npx wrangler login）
# ============================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/worker"

BLUE='\033[1;34m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; NC='\033[0m'
say()  { echo -e "${BLUE}[setup]${NC} $*"; }
ok()   { echo -e "${GREEN}[done ]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn ]${NC} $*"; }
die()  { echo -e "${RED}[fail ]${NC} $*"; exit 1; }

# 0. 环境检查
command -v node >/dev/null 2>&1 || die "需要 Node.js 18+，先安装"
say "安装依赖（wrangler v4）..."
npm install --legacy-peer-deps --silent || npm install --silent || die "npm install 失败"

say "检查 wrangler 登录状态..."
if ! npx wrangler whoami >/dev/null 2>&1; then
  warn "未登录 Cloudflare，即将弹出浏览器授权（或复制链接打开）..."
  npx wrangler login || die "wrangler login 失败"
fi

# 1. R2 bucket
say "创建 R2 bucket: iptv-cf"
if npx wrangler r2 bucket create iptv-cf 2>&1 | grep -qi "already exists"; then
  ok "R2 bucket 已存在，跳过"
else
  ok "R2 bucket 就绪"
fi

# 2. KV namespace（自动回填 wrangler.jsonc）
say "创建 KV namespace: KV"
KV_OUT="$(npx wrangler kv namespace create KV 2>/dev/null || true)"
KV_ID="$(echo "$KV_OUT" | grep -o '"id": *"[^"]*"' | head -1 | grep -o '[a-f0-9]\{32\}')"
if [ -z "$KV_ID" ]; then
  KV_ID="$(npx wrangler kv namespace list 2>/dev/null | grep -o '"id": *"[^"]*"' | head -1 | grep -o '[a-f0-9]\{32\}')"
  [ -n "$KV_ID" ] && ok "KV 已存在，复用" || die "无法获取 KV id，请手动创建：npx wrangler kv namespace create KV"
else
  ok "KV 就绪: $KV_ID"
fi

# 3. D1 database（自动回填 wrangler.jsonc）
say "创建 D1 数据库: iptv-cf"
D1_OUT="$(npx wrangler d1 create iptv-cf 2>/dev/null || true)"
D1_ID="$(echo "$D1_OUT" | grep -o 'database_id *= *"[^"]*"' | head -1 | grep -o '[a-f0-9]\{32\}')"
if [ -z "$D1_ID" ]; then
  D1_ID="$(npx wrangler d1 list 2>/dev/null | grep -o 'database_id *= *"[^"]*"' | head -1 | grep -o '[a-f0-9]\{32\}')"
  [ -n "$D1_ID" ] && ok "D1 已存在，复用" || die "无法获取 D1 id，请手动创建：npx wrangler d1 create iptv-cf"
else
  ok "D1 就绪: $D1_ID"
fi

# 4. 回填 wrangler.jsonc
say "回填 wrangler.jsonc（KV id / D1 id / GITHUB_REPO）..."
REPO="$(cd "$ROOT" && git remote get-url origin 2>/dev/null | sed -E 's#.*github.com[:/]##; s#\.git$##' || true)"
if [ -z "$REPO" ]; then
  REPO="REPLACE_ME/REPLACE_ME"
  warn "未检测到 git remote，稍后手动改 wrangler.jsonc 里的 GITHUB_REPO"
fi
python3 - "$KV_ID" "$D1_ID" "$REPO" << 'EOF'
import json, sys, re
kv_id, d1_id, repo = sys.argv[1], sys.argv[2], sys.argv[3]
p = "wrangler.jsonc"
s = open(p).read()
s = s.replace("REPLACE_ME_KV_ID", kv_id).replace("REPLACE_ME_D1_ID", d1_id)
s = s.replace("REPLACE_ME/REPLACE_ME", repo)
open(p, "w").write(s)
print("wrangler.jsonc 已更新")
EOF

# 5. 初始化 schema
say "初始化 D1 schema..."
npx wrangler d1 execute iptv-cf --remote --file ../db/schema.sql >/dev/null 2>&1 \
  && ok "schema 已导入" || warn "schema 导入警告（可能已初始化过）"

# 6. Secrets
say "配置 Secret：ADMIN_TOKEN（自动生成）"
ADMIN_TOKEN="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(24))')"
echo "$ADMIN_TOKEN" | npx wrangler secret put ADMIN_TOKEN >/dev/null 2>&1 && ok "ADMIN_TOKEN 已设置"
echo -e "${YELLOW}============================================================${NC}"
echo -e "${YELLOW}你的 ADMIN_TOKEN（保存好！GitHub Actions Secret 也要填这个）:${NC}"
echo -e "${GREEN}$ADMIN_TOKEN${NC}"
echo -e "${YELLOW}============================================================${NC}"

say "配置 Secret：GH_TOKEN（去 GitHub 生成 PAT）"
echo -e "${BLUE}生成 PAT：GitHub → Settings → Developer settings → Personal access tokens${NC}"
echo -e "${BLUE}  → Fine-grained tokens → 选你的仓库 → Repository permissions${NC}"
echo -e "${BLUE}  → Actions 选 Read and write → 生成${NC}"
read -r -s -p "粘贴 GH_TOKEN（输入不回显）: " GH_TOKEN && echo
echo "$GH_TOKEN" | npx wrangler secret put GH_TOKEN >/dev/null 2>&1 && ok "GH_TOKEN 已设置"

# 7. 部署
say "部署 Worker..."
DEPLOY_OUT="$(npx wrangler deploy 2>&1 | tail -5)"
echo "$DEPLOY_OUT"
WORKER_HOST="$(echo "$DEPLOY_OUT" | grep -o 'https://[a-z0-9.-]*\.workers\.dev' | head -1)"

echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}Cloudflare 侧部署完成！${NC}"
if [ -n "$WORKER_HOST" ]; then
  echo -e "${GREEN}Worker 地址：${NC} $WORKER_HOST"
  echo -e "${GREEN}订阅地址：${NC} $WORKER_HOST/iptv.m3u"
else
  echo -e "${GREEN}Worker 地址：${NC} 见上方输出（xxx.workers.dev）"
fi
echo -e "${GREEN}============================================================${NC}"
echo ""
echo "接下来（GitHub 侧）："
echo "  1. 把项目推到 GitHub 仓库"
echo "  2. Settings → Secrets and variables → Actions → New repository secret："
echo "     WORKER_URL = 上面的 Worker 地址"
echo "     ADMIN_TOKEN = 上面生成的 token"
echo "  3. 编辑 config/sources.yaml 换成你的真实源"
echo "  4. 推送后到 Actions 页手动跑一次「采集源」，链条自动接力（测速→聚合）"
echo "  5. 验证：浏览器打开 Worker 地址/iptv.m3u"
