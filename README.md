# Daily AI News to Feishu

每日抓取 AI 资讯源，整理成中文日报，并通过飞书群机器人推送。

推荐部署方式：GitHub Actions。这样电脑关机也能每天自动推送。

## 功能

- 抓取 RSS / Atom AI 资讯源
- 去重、按关键词排序，保留每日重点新闻
- 推送飞书群机器人富文本消息
- 支持飞书 webhook 签名密钥
- 支持本地 dry run 预览
- 支持 GitHub Actions 每日定时运行
- 可选支持 Windows 计划任务本地运行

## GitHub Actions 部署

1. 新建一个 GitHub 仓库，例如 `ai-news-feishu`。
2. 把本目录里的所有文件上传到仓库，包括 `.github/workflows/daily-ai-news-feishu.yml`。
3. 在飞书群中添加「自定义机器人」，复制 webhook。
4. 打开 GitHub 仓库：
   `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`
5. 添加 Secret：

```text
FEISHU_WEBHOOK_URL = https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
```

如果飞书机器人启用了签名校验，再添加：

```text
FEISHU_WEBHOOK_SECRET = 飞书机器人签名密钥
```

6. 打开仓库的 `Actions` 页面，选择 `Daily AI News to Feishu`。
7. 点击 `Run workflow` 可以手动测试。

默认每天北京时间 09:00 自动推送。对应 workflow 里的 UTC cron 是：

```yaml
cron: "0 1 * * *"
```

## 本地测试

在 PowerShell 中进入项目目录：

```powershell
cd D:\ai-news-feishu
```

预览日报：

```powershell
python .\ai_news_feishu.py --dry-run
```

本地立即推送：

```powershell
$env:FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
python .\ai_news_feishu.py
```

如果机器人启用了签名校验：

```powershell
$env:FEISHU_WEBHOOK_SECRET="飞书机器人签名密钥"
```

## Windows 计划任务

如果你也想在本机开机时定时跑，可以使用：

```powershell
.\install_daily_task.ps1
```

指定时间：

```powershell
.\install_daily_task.ps1 -Time "08:30"
```

注意：本地计划任务要求电脑开机、联网。关机时不会执行。

## 配置资讯源

编辑 [sources.json](./sources.json) 可以增删 RSS / Atom 源。

常用环境变量：

- `FEISHU_WEBHOOK_URL`：必填，飞书机器人 webhook。
- `FEISHU_WEBHOOK_SECRET`：可选，飞书机器人签名密钥。
- `AI_NEWS_MAX_ITEMS`：可选，默认 `12`。
- `AI_NEWS_LOOKBACK_HOURS`：可选，GitHub Actions 默认 `24`。
- `AI_NEWS_SOURCES_FILE`：可选，自定义资讯源 JSON 路径。
- `AI_NEWS_STATE_FILE`：可选，已推送链接记录，默认 `.state/sent_links.json`。

