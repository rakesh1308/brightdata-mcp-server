# Bright Data MCP Server (Custom)

A custom Model Context Protocol (MCP) server that wraps **all Bright Data API products** into MCP tools. Use it from Claude Desktop, Cursor, Claude Code, or any MCP-compatible client.

## 🚀 Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your Bright Data API token
```

Get your API token: [Bright Data Dashboard → Settings → API](https://brightdata.com/cp/settings/api)

### 3. Run locally

```bash
python brightdata_mcp.py
```

### 4. Connect from Claude Desktop / Cursor

Add to your MCP client config:

```json
{
  "mcpServers": {
    "brightdata-custom": {
      "command": "python",
      "args": ["C:/RAKESH/WORK/MCP_and_AI/MCP/bright-data-scrape/brightdata_mcp.py"]
    }
  }
}
```

## 🛠️ Available Tools (17)

| # | Tool | Product | Description |
|---|------|---------|-------------|
| 1 | `scraper_scrape_sync` | Scrapers | Single URL sync scrape |
| 2 | `scraper_scrape_batch` | Scrapers | Multiple URLs sync batch |
| 3 | `scraper_trigger_async` | Scrapers | Trigger async job (1000s of URLs) |
| 4 | `scraper_get_snapshot` | Scrapers | Poll async job results |
| 5 | `scraper_discover` | Discover API | Find records by keyword |
| 6 | `linkedin_jobs_by_url` | LinkedIn Jobs | Scrape single job posting |
| 7 | `linkedin_jobs_search` | LinkedIn Jobs | Search jobs by keyword+location |
| 8 | `linkedin_profile` | LinkedIn | Scrape person profile |
| 9 | `linkedin_company` | LinkedIn | Scrape company page |
| 10 | `serp_search` | SERP API | Google/Bing/Yandex search |
| 11 | `web_unlock` | Web Unlocker | Unblock any webpage |
| 12 | `web_unlock_with_instructions` | Web Unlocker | Unblock with custom actions |
| 13 | `browser_navigate` | Browser API | Cloud browser navigation |
| 14 | `browser_interact` | Browser API | Multi-step browser automation |
| 15 | `proxy_request` | Proxy | Request via residential proxy |
| 16 | `list_datasets` | Utility | List all available scraper datasets |
| 17 | `scrape_as_markdown` | Web Unlocker | Scrape URL → clean markdown |

## 💬 Usage Examples

| You say... | MCP tool called |
|------------|----------------|
| *"Search LinkedIn for Lead Android Developer jobs in Pune"* | `linkedin_jobs_search("Lead Android Developer", "Pune")` |
| *"Scrape this LinkedIn job: linkedin.com/jobs/view/123"* | `linkedin_jobs_by_url("https://...")` |
| *"Google search for Android Architect jobs India"* | `serp_search("Android Architect jobs India")` |
| *"Scrape this webpage as markdown: example.com"* | `scrape_as_markdown("https://...")` |
| *"Unlock this blocked page: example.com"* | `web_unlock("https://...")` |
| *"Discover LinkedIn profiles for 'Android Architect'"* | `scraper_discover("gd_l1viktl72bvl7bjuj0", "Android Architect")` |
| *"Scrape this Amazon product: amazon.com/dp/B08"* | `scraper_scrape_sync("gd_l4e8uuj844u8hh", "https://...")` |

## 📦 Products Covered

| Product | API Endpoint | Pricing |
|---------|-------------|---------|
| Scrapers / Datasets | `api.brightdata.com/datasets/v3/scrape` | $1.5–$5/1k records |
| SERP API | `api.brightdata.com/request` | Pay-as-you-go |
| Web Unlocker | `api.brightdata.com/request` | Pay-as-you-go |
| Browser API | `api.brightdata.com/request` | Pay-as-you-go |
| Proxy Infrastructure | `brd.superproxy.io:22225` | Pay-as-you-go |
| Discover API | `api.brightdata.com/datasets/v3/discover` | Included with scrapers |

> **Note:** 5,000 free credits/month are shared across Web Unlocker, SERP, Web Scraper, and Scraper Studio.

## 🚢 Deploy to Zeabur

### Option A: One-click deploy

1. Push this repo to GitHub (private repo recommended)
2. Go to [zeabur.com](https://zeabur.com) → **New Project** → **Deploy from GitHub**
3. Select this repository
4. Zeabur will auto-detect the `Dockerfile` and build
5. Set environment variables in Zeabur dashboard:
   - `BRIGHTDATA_API_TOKEN`
   - `SERP_ZONE`
   - `WEB_UNLOCKER_ZONE`
   - `BROWSER_ZONE`
   - `BRIGHTDATA_CUSTOMER_ID`
   - `BRIGHTDATA_PROXY_PASSWORD`
6. Deploy

### Option B: Zeabur CLI

```bash
npm install -g @zeabur/cli
zeabur login
zeabur deploy
```

### MCP Transport on Zeabur

This server uses **stdio** transport by default, which is the standard for local MCP clients. For remote/server-side use, run with **SSE** transport:

```bash
MCP_TRANSPORT=sse python brightdata_mcp.py
```

To expose an SSE endpoint, set `MCP_TRANSPORT=sse` and bind to `0.0.0.0`:

```bash
# In Zeabur, set environment variable:
MCP_TRANSPORT=sse
HOST=0.0.0.0
PORT=8080
```

## 🔧 Client Configurations

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "brightdata-custom": {
      "command": "python",
      "args": ["C:/path/to/brightdata_mcp.py"]
    }
  }
}
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "brightdata-custom": {
      "command": "python",
      "args": ["C:/path/to/brightdata_mcp.py"]
    }
  }
}
```

### Claude Code (CLI)

```bash
claude mcp add brightdata-custom python /path/to/brightdata_mcp.py
```

## 🔗 Documentation

- [Scrapers Overview](https://docs.brightdata.com/datasets/scrapers/overview)
- [SERP API](https://docs.brightdata.com/scraping-automation/serp-api/quickstart)
- [Web Unlocker](https://docs.brightdata.com/scraping-automation/web-unlocker)
- [Browser API](https://docs.brightdata.com/scraping-automation/scraping-browser/introduction)
- [Full Scraper Catalog](https://brightdata.com/cp/scrapers/browse)

## 📄 License

MIT
