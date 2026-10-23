# IPTV-CF — Cloudflare 免费额度内的 IPTV 聚合系统

架构：**D1 唯一事实源 → GitHub Actions 采集/测速/聚合 → Worker 单一通道分发 → R2 快照**

```
用户播放器 ──GET──> Worker /iptv.m3u ──(Cache API 60s)──> R2 publish/
                          ▲
GitHub Actions            │ 上传 + purge（Bearer ADMIN_TOKEN 单一通道）
  fetch_sources.py ──采集──┤
  speed_test.py   ──测速──┤
  aggregate.py    ──聚合──┤
                          │
Worker /api/internal/* ──> D1 读写代理（Actions 不直接碰 D1/R2，无需 S3 凭据）
```

## 一、Cloudflare 侧准备（一次性，5 分钟）

```bash
# 1. 登录 wrangler
npx wrangler login

# 2. 创建 R2 bucket
npx wrangler r2 bucket create iptv-cf

# 3. 创建 KV namespace（记录返回的 id，填入 wrangler.jsonc）
npx wrangler kv namespace create KV

# 4. 创建 D1 数据库（记录返回的 database_id，填入 wrangler.jsonc）
npx wrangler d1 create iptv-cf

# 5. 初始化 schema
npx wrangler d1 execute iptv-cf --remote --file=db/schema.sql

# 6. 配置两个 Secret（token 自己生成，例如 openssl rand -hex 24）
npx wrangler secret put ADMIN_TOKEN
npx wrangler secret put GH_TOKEN   # GitHub PAT：仓库级 Actions 写权限

# 7. 部署 Worker
cd worker && npm install && npm run deploy
# 记下返回的 https://iptv-cf.<你的子域>.workers.dev
```

## 二、GitHub 侧准备

1. 把本项目推到 GitHub 仓库（`GITHUB_REPO` vars 改成 `用户名/仓库名`）。
2. Settings → Secrets → Actions 添加：
   - `WORKER_URL`：`https://iptv-cf.xxx.workers.dev`
   - `ADMIN_TOKEN`：与 Worker secret 一致
   - `SOURCE_PROXY` / `SOURCE_PROXY_HOSTS`：可选，国内源抓不动时配代理
3. 编辑 `config/sources.yaml` 填入你的源，推送后 workflow 自动跑（也可手动触发）。

## 三、调度（UTC 时间，均按北京时间标注）

| Workflow | cron | 说明 |
|---|---|---|
| fetch（采集源） | 北京 00:15 / 12:15 | 条件请求，未变不重复下载 |
| speed（测速） | 北京 01:30 / 13:30，采集完成后自动触发 | 两段式稳态速率 |
| aggregate（聚合导出） | 北京 02:00 / 14:00，测速完成后自动触发 | 上传 R2 + purge 缓存 |

## 四、状态机（照 iptv-hub 实测参数）

```
ok ──连续失败──> fail_count 1→2→3 ──> active=0 剔除（订阅隐藏）
                                     │
                                     ├─ 14 天后复活（fail_count 清零，revive_count+1）
                                     └─ 复活过且仍失效 90 天 → purged 永久停用
```

- 失败线路 20h 复测、正常线路 72h 复检（五档预算在 `pending_entries()`）
- 测速算法：512KB 预热段丢弃 → 1MB 采样 → 8s 首窗口 → 慢启动延长 12s → 稳态样本 <32KB 退回整段平均
- 测速关自动跳转 + 内网地址守卫（防 SSRF）

## 五、订阅地址

- `https://<worker域名>/iptv.m3u`（标准 + M3U Plus，带 tvg-id/logo/group-title/catchup/EXTVLCOPT 头）
- `https://<worker域名>/iptv.txt`（TVBox/DIYP 分组格式）
- `https://<worker域名>/iptv.json`
- `https://<worker域名>/epg.xml`（Phase 4）

## 六、关于你的域名 xu05236.dpdns.org

dpdns.org 是花生壳共享后缀，**NS 在花生壳手里，无法把整个 zone 加进 Cloudflare**，
所以 Worker 自定义域名/R2 自定义域名都绑不了它。两条务实路线：

1. **零成本**：直接用 `xxx.workers.dev`（免费自带），播放器完全支持。
2. **想要自己的域名**：注册一个便宜域名（.top/.xyz 约 10-30 元/年，NS 改成 CF 的），
   在 CF 里建 zone 后绑到 Worker（Custom Domains）。花生壳域名继续做动态 DNS 用，互不影响。
   或者用花生壳面板把 xu05236.dpdns.org 做 301 跳转到 workers.dev 地址（大多数播放器跟随）。

## 七、Phase 路线

- [x] Phase 0：骨架 + schema + 采集/测速/聚合 + Worker + workflow
- [ ] Phase 1：多格式解析增强（HWCTC/JSP 严格解析、TVBox 子源白名单）
- [ ] Phase 2：快照回滚 API + 试播反代（CORS）
- [ ] Phase 3：别名库全量导入（iptv-api alias.txt 2798 行）+ 频道详情联动
- [ ] Phase 4：EPG 多源合并（照 laobaiepg：Actions 抓取 → R2 → /epg.xml）
- [ ] Phase 5：管理后台 API（源 CRUD、检测中心、token 中间件——骨架已留）
- [ ] Phase 6：iOS 风格前端（照 cloud_iptv：React + Apple HIG，毛玻璃卡片）
- [ ] Phase 7：信誉分调度、diff 通知（Telegram）、众包纠错
