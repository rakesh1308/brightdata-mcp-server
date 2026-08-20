# Bright Data MCP Server (Custom)

A self-hosted MCP server wrapping Bright Data's Web Scraper API, SERP API, Web Unlocker API, and account-gated Discover API. The first three draw from Bright Data's **5,000 monthly free-credit pool** for eligible accounts; Discover requires separate enablement.

Use it from Claude Desktop, Cursor, Claude Code, or any MCP-compatible client.

## 🚀 Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your Bright Data API key (as BRIGHTDATA_API_KEY)
```

Get your API key: [Bright Data Dashboard → Account Settings → API keys](https://brightdata.com/cp/setting/users) → click **"Add API key"**. The key is shown **once** — copy it immediately. It's a long opaque string with no `brd_` prefix (that prefix is only used by proxy-protocol usernames).

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

## 🛠️ Available Tools (9)

Web Scraper, SERP, and Web Unlocker calls consume the shared **5,000 monthly free credits** on eligible accounts. `discover` uses a separate, account-gated API and returns a clear message when it is not enabled.

| # | Tool | What it does |
|---|------|-------------|
| 1 | `search_engine` | Google / Bing / Yandex structured SERP |
| 2 | `search_engine_batch` | Up to 10 search queries in parallel |
| 3 | `scrape_as_markdown` | Any URL → clean markdown (bypasses anti-bot/CAPTCHA) |
| 4 | `scrape_as_html` | Any URL → raw HTML |
| 5 | `scrape_batch` | Up to 10 URLs in parallel → markdown |
| 6 | `discover` | AI-ranked public-web discovery (separate enablement) |
| 7 | `scrape` | **Generic Web Scraper — works for any platform** (LinkedIn, Amazon, Instagram, TikTok, X, YouTube, Reddit, Crunchbase, etc.) |
| 8 | `scrape_poll` | Poll an async scrape job until ready |
| 9 | `list_datasets` | List available datasets (cached 1h) |

### The `scrape` tool replaces ~21 convenience wrappers

Use the generic `scrape` tool for any platform-specific data. Pass the dataset as a friendly name:

| Want | Call |
|---|---|
| LinkedIn profile | `scrape(dataset="linkedin_profile", urls=["https://linkedin.com/in/satyanadella"])` |
| LinkedIn jobs | `scrape(dataset="linkedin_jobs", urls=["https://www.linkedin.com/jobs/search/?keywords=android"])` |
| Amazon product | `scrape(dataset="amazon_product", urls=["https://www.amazon.com/dp/B08..."])` |
| Instagram profile | `scrape(dataset="instagram_profile", urls=["https://instagram.com/..."])` |
| TikTok posts | `scrape(dataset="tiktok_posts", urls=["https://tiktok.com/@user"])` |
| Reddit posts | `scrape(dataset="reddit_posts", urls=["https://reddit.com/r/..."])` |

Aliases also work: `"amazon"`, `"linkedin"`, `"insta"`, `"tt"`, `"x"`, etc. See `list_datasets()` for the full catalog.

> **Intentionally excluded** (paid add-ons, not in the free pool): Browser Automation, LLM Insights (ChatGPT/Grok/Perplexity), npm/PyPI package data.

## 💬 Usage Examples

| You say... | MCP tool called |
|------------|----------------|
| *"Search LinkedIn for Lead Android Developer jobs in Pune"* | `scrape(dataset="linkedin_jobs", urls=["https://www.linkedin.com/jobs/search/?keywords=Lead+Android+Developer&location=Pune"])` |
| *"Scrape this LinkedIn job: linkedin.com/jobs/view/123"* | `scrape(dataset="linkedin_jobs", urls=["https://linkedin.com/jobs/view/123"])` |
| *"Google search for Android Architect jobs India"* | `search_engine(query="Android Architect jobs India")` |
| *"Scrape this webpage as markdown: example.com"* | `scrape_as_markdown(url="https://example.com")` |
| *"Unlock this blocked page: example.com"* | `scrape_as_markdown(url="https://example.com")` |
| *"Discover recent Android architecture research"* | `discover(query="Android architecture", intent="Recent authoritative engineering articles")` |
| *"Scrape this Amazon product: amazon.com/dp/B08"* | `scrape(dataset="amazon_product", urls=["https://www.amazon.com/dp/B08..."])` |

## 📦 Products Covered

| Product | Free Credits? |
|---------|---------------|
| Web Scraper / Datasets (LinkedIn, Amazon, etc.) | ✅ Yes |
| SERP API (Google/Bing/Yandex) | ✅ Yes |
| Web Unlocker API (any URL) | ✅ Yes |
| Discover API | ⚠️ Separate account-gated product |
| Browser API (Scraping Browser) | ❌ Not exposed by this server |
| Proxy Infrastructure | ❌ Separate paid product |
| LLM Insights (ChatGPT/Grok/Perplexity) | ❌ Separate paid product |
| npm/PyPI package data | ❌ Separate paid product |

> **Note:** As of August 20, 2026, 5,000 monthly free credits are shared across Web Unlocker, SERP, Web Scraper, and Scraper Studio. Bright Data documents Browser API inclusion starting September 1, 2026; this server does not expose Browser API.

## 🚢 Deploy to Zeabur

### Option A: One-click deploy

1. Push this repo to GitHub (private repo recommended)
2. Go to [zeabur.com](https://zeabur.com) → **New Project** → **Deploy from GitHub**
3. Select this repository
4. Zeabur will auto-detect the `Dockerfile` and build
5. Set environment variables in Zeabur dashboard:
   - `BRIGHTDATA_API_KEY` (required — your Bright Data API key from `/cp/setting/users`)
   - `SERP_ZONE` (required — e.g. `serp_api`, must match a SERP API zone in your dashboard)
   - `WEB_UNLOCKER_ZONE` (required — e.g. `mcp_unlocker`, must match a Web Unlocker zone)
6. Deploy

### Option B: Zeabur CLI

```bash
npm install -g @zeabur/cli
zeabur login
zeabur deploy
```

### MCP Transport on Zeabur

This server uses **stdio** transport by default for local MCP clients. Zeabur is configured to use modern **Streamable HTTP** transport:

```bash
MCP_TRANSPORT=http python brightdata_mcp.py
```

Set the host and port with the variables used by this project:

```bash
# In Zeabur, set environment variable:
MCP_TRANSPORT=http
MCP_HOST=0.0.0.0
MCP_PORT=8080
MCP_PATH=/mcp
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
