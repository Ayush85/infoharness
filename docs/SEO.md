# SEO plan for infoharness

No keyword-volume tool was available when this was written, so the keywords below are grouped by search intent, not ranked by volume. Check volume and difficulty in Search Console (after 2-4 weeks of data), Google Keyword Planner or Ahrefs/Semrush free tiers, then adjust.

## Keyword map (one page per intent)

| Intent | Example searches | Page |
|---|---|---|
| Tool: docs QA | documentation checker, docs quality checker, documentation audit tool | /tools/documentation-checker |
| Tool: help center | help center audit tool, help center audit checklist, zendesk help center audit | /tools/help-center-audit-tool |
| Tool: knowledge base | knowledge base audit, knowledge base quality checker, kb health check | /tools/knowledge-base-audit |
| Tool: links | broken link checker documentation, find broken links help center | /tools/broken-link-checker-for-documentation |
| Tool: duplicates | duplicate content checker, find duplicate pages, duplicate articles knowledge base | /tools/duplicate-content-checker |
| Tool: stale | outdated content checker, find stale pages, content decay checker | /tools/outdated-content-checker |
| Tool: orphans | orphan page finder, find orphan pages | /tools/orphan-page-finder |
| Tool: AI | rag knowledge base quality, check documents before indexing, ai chatbot knowledge base audit | /tools/rag-knowledge-base-checker |
| Tool: conflicts | contradiction checker docs, conflicting information knowledge base | /tools/contradiction-checker |
| Question | how to audit a knowledge base | /guides/how-to-audit-a-knowledge-base |
| Question | how to find broken links on a website | /guides/how-to-find-broken-links-on-a-website |
| Question | why does my ai chatbot give wrong answers | /guides/fix-ai-chatbot-wrong-answers |
| Question | rag data quality checklist | /guides/rag-knowledge-base-quality-checklist |
| Question | knowledge base maintenance best practices | /guides/knowledge-base-maintenance-best-practices |
| Question | how to find duplicate / outdated pages | /guides/find-duplicate-and-outdated-pages |
| Long tail (automatic) | "<site> docs health", "<site> broken links" | /r/{id} public reports, listed at /reports |

## Done in code
- One title, description, H1, canonical, JSON-LD (WebApplication, FAQPage, BreadcrumbList, Article) per page; sitemap.xml, robots.txt, llms.txt cover all of them.
- Internal links: home -> tools hub -> each tool -> related guides -> other tools; footer links to /tools, /guides, /reports.
- Question-style FAQ on the home page (FAQPage markup) for "how do I find broken links...", "why does my AI chatbot...".
- Reports of public scans are indexable (latest per site), which grows long-tail pages without writing them.

## You do (off-site, in this order)
1. Google Search Console: add infoharness.com as a Domain property (DNS TXT), submit https://infoharness.com/sitemap.xml, request indexing for /, /tools and the 9 tool pages.
2. Bing Webmaster Tools: import from Search Console (also feeds DuckDuckGo and several AI answer engines).
3. Week 1-2: post the "docs health of popular docs sites" results (run the Benchmark workflow) on Hacker News (Show HN), Reddit r/technicalwriting, r/webdev, Dev.to, and link the /reports pages. Original data earns links.
4. List on directories: Product Hunt, AlternativeTo, SaaSHub, There's An AI For That, Indie Hackers, G2/Capterra (free listing), "awesome" lists for documentation tooling.
5. Add the README badge (/badge/{id}.svg) to your own repos and ask users to add theirs: each badge is a backlink.
6. Publish one new guide every 1-2 weeks, each aimed at one question people ask (see the table); add it to GUIDES in app/seo.py and a template under app/templates/guides/.
7. After 4 weeks open Search Console > Performance, sort queries by impressions, and write a page for every query that gets impressions but no page of its own; rewrite titles for pages with impressions and a low click rate.

## Risks
- New domains take weeks to months to rank; expect little traffic before month 2-3.
- Generic searches ("documentation checker") are competitive; the question-style guides and the public /reports pages are the likelier early wins.
- Public reports name third-party sites with a grade; keep the removal route on /contact working.
