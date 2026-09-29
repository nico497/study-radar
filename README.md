# Study Radar

**What mattered in your niche this week, in 60 seconds.**

Study Radar watches the companies in your niche that publish original research. Each week it finds their new studies, surveys and data analyses, skips everything else, and gives you one page:

- **This week:** a two-sentence summary plus the 3 takeaways that matter, each with how far to trust it.
- **One card per study, in plain English (Smart Brevity style):** the finding, why it matters, and how solid the evidence is. Must-know studies sit at the top and minor ones are greyed out.
- **Details on demand:** the key numbers, how the study was done (size, source, timing), whether the publisher is selling something, and what to watch out for.
- **★ Save** anything worth keeping.

It runs free on GitHub, with no server and nothing to host. You only pay for your own Claude API usage.

<!-- After your first run, add a screenshot: ![Study Radar dashboard](docs/screenshot.png) -->

---

## How it works

```
Every Monday (GitHub Actions)
  1. Discover   check each source's RSS feed, listing page or sitemap for new posts
  2. Triage     a cheap model separates studies from how-tos, news and product updates
  3. Brief      a stronger model reads each study: finding, importance, method check
  4. Summarise  one short "this week" summary across the new studies
  5. Publish    results are committed to the repo; the dashboard (GitHub Pages) updates
```

It has three details that make the output trustworthy:

- **It won't invent numbers.** If the method isn't disclosed, the brief says "Not disclosed". A missing method counts as a finding.
- **It flags gated reports.** If the full report sits behind an email form, the card is marked "Gated" and links any PDFs it found.
- **It fails loudly.** When a site blocks it or a page won't render, the dashboard shows that in the source health table and on the card. Nothing gets dropped silently.

## Setup (about 10 minutes)

1. **Copy the repo.** Click **Use this template** (or fork it). Public or private both work. GitHub Pages on a private repo needs a paid GitHub plan.
2. **Add your API key.** Get a key at [console.anthropic.com](https://console.anthropic.com). In your repo, go to **Settings → Secrets and variables → Actions → New repository secret**, name it `ANTHROPIC_API_KEY`, and paste the key.
3. **Turn on the dashboard.** Go to **Settings → Pages**, set Source to *Deploy from a branch*, choose branch `main` and folder `/docs`, then click Save.
4. **Pick your niche.** Edit `config.yaml`: set `niche`, `audience` and your `sources` (see below).
5. **Run it.** Go to **Actions → Study Radar → Run workflow**. When it finishes (a few minutes), open `https://<you>.github.io/<repo>/`.

Your copy starts clean: the template owner's briefs and history are cleared on your first run.

After that it runs every Monday on its own. To change the day or time, edit the `cron` line in `.github/workflows/radar.yml`.

## Adding sources

Each source needs a `name` and one way to find new posts:

```yaml
sources:
  # Best: an RSS feed. Gives dates and summaries. Try /feed, /rss or /blog/feed
  - name: Ahrefs
    feed: https://ahrefs.com/blog/feed/

  # No feed? Point at a listing page and give a regex the post URLs match
  - name: BrightEdge
    page: https://www.brightedge.com/resources/research-reports
    link_pattern: /resources/research-reports/[a-z0-9-]+/?$
    all_studies: true        # everything here is research, so skip triage

  # Or use a sitemap
  - name: Example
    sitemap: https://example.com/sitemap.xml
    link_pattern: /research/
```

Optional per source:

| Option | What it does |
|---|---|
| `all_studies: true` | Skip triage. Use this for pages that only list research. |
| `link_pattern` | Only keep URLs that match this regex. |
| `exclude_pattern` | Drop URLs that match this regex (e.g. `/webinars/`). |
| `first_run_keep` | Listing pages have no dates, so on the first run only the top N (default 3) get briefed. |

**Test sources before committing** (no API key needed):

```bash
pip install -r requirements.txt
python radar.py --check
```

```
OK   Ahrefs                 feed      20 items
       2026-09-25  How to Optimize for AI Search...
FAIL SomeSite               blocked by the site (403)
```

### What makes a good source

Pick companies that publish their **own data**: tool vendors with big datasets, agencies that run experiments, and analysts who survey. A blog that mixes studies with how-tos is fine, because triage filters out the how-tos. News sites are a poor fit because they mostly report other people's research.

**Official sources** (like Google) announce changes rather than publish studies. Mark them `kind: official`: triage then keeps real changes (ranking updates, new features, documentation changes) and drops events and community posts, and the cards show "Official update" instead of a confidence rating.

Busy official sources can take a `focus` so only posts on that topic get through. For example, the OpenAI source only keeps posts about ChatGPT search, crawlers and citations, not model launches or customer stories.

The starter list covers SEO and GEO:
- **Official:** Google Search Central, Google Search Status, Bing Webmaster, OpenAI (search-related only)
- **Research:** Ahrefs, Semrush, Growth Memo, Seer Interactive, Profound, Orbit Media, Peec AI, AirOps, BrightEdge

Swap them for your own niche.

## Cost

Each run makes one small triage call per 30 new posts, plus one brief call per study. A brief call reads the article text, usually 5k–20k tokens. `max_briefs_per_run` (default 12) caps spend, and any extra studies wait for the next run. For current per-token rates, see [anthropic.com/pricing](https://www.anthropic.com/pricing). You can change both models in `config.yaml`.

## Using the dashboard

- **Tabs:** This week (the latest run) · ★ Saved · All studies (the archive, with search and filters).
- **Keyboard:** `j`/`k` move, `enter` details, `s` save, `o` open source, `c` copy brief.
- **Copy all** on the Saved tab copies every saved brief as Markdown.

Saves are stored in your browser. They don't sync between devices, and clearing browser data removes them. The briefs themselves live in the repo, so every device sees them.

## Limits

- **JavaScript-only pages** can't be read. They show up as "couldn't read" cards with a link.
- **Gated reports:** only the landing page is briefed.
- **Bot blocking:** some sites (often behind Cloudflare) block automated requests. Source health shows it. Use a different URL for that company, like its feed, or drop it.
- **Web-only:** research published only on LinkedIn, in newsletters or in PDFs sent by email isn't covered.
- **AI briefs can be wrong.** The method check is there so you know how much to trust each study, and every card links to the source. Check the numbers before you quote them.

## Files

```
radar.py                     the whole pipeline (one file)
config.yaml                  your niche and sources
docs/index.html              the dashboard (static, no build step)
docs/data.json               briefs (written by the workflow)
state/state.json             what's been seen, so nothing is briefed twice
.github/workflows/radar.yml  weekly schedule
```

**Changed the writing style?** Edit `STYLE_RULES` in `radar.py`, then go to **Actions → Study Radar → Run workflow → mode: restyle**. That rewrites your existing briefs without re-reading the articles.

Run it locally: `ANTHROPIC_API_KEY=... python radar.py`, then `cd docs && python -m http.server` and open http://localhost:8000.

## License

MIT
