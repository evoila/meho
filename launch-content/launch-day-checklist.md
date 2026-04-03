# MEHO Launch Day Checklist

## Pre-Launch Checklist (Day Before)

### Content Review
- [ ] Blog post reviewed and approved: `launch-content/blog/why-we-open-sourced-meho.md`
- [ ] Technical deep-dive reviewed and approved: `launch-content/deep-dive/cross-system-reasoning.md`
- [ ] Demo video script reviewed: `launch-content/video/demo-script.md`
- [ ] HN submission reviewed: `launch-content/social/hn-show-hn.md`
- [ ] Reddit posts reviewed: `reddit-devops.md`, `reddit-kubernetes.md`, `reddit-selfhosted.md`, `reddit-sysadmin.md`
- [ ] LinkedIn posts reviewed: `linkedin-personal.md`, `linkedin-company.md`
- [ ] Twitter thread reviewed: `launch-content/social/twitter-thread.md`
- [ ] Dev.to article reviewed (publish 2-3 days later): `launch-content/social/devto-article.md`

### Placeholder Link Replacement
- [ ] Replace `GITHUB_URL` with actual GitHub repo URL in all content files
- [ ] Replace `MEHO_AI_URL` with actual meho.ai URL in all content files
- [ ] Replace `DEEP_DIVE_URL` with actual deep-dive article URL in all content files
- [ ] Replace `BLOG_URL` with actual blog post URL in all content files
- [ ] Verify no `GITHUB_URL`, `MEHO_AI_URL`, `DEEP_DIVE_URL`, or `BLOG_URL` placeholders remain: `grep -r "GITHUB_URL\|MEHO_AI_URL\|DEEP_DIVE_URL\|BLOG_URL" launch-content/`

### Infrastructure Verification
- [ ] GitHub repo is public with polished README, CONTRIBUTING.md, and LICENSE
- [ ] meho.ai is live and serving HTTPS (Cloudflare Pages)
- [ ] Blog posts published on meho.ai/blog (accessible but not yet promoted)
- [ ] Deep-dive article published on meho.ai/blog (accessible but not yet promoted)
- [ ] Demo video uploaded to YouTube (unlisted until launch morning)
- [ ] Docker images available on Docker Hub / GHCR
- [ ] Full flow tested end-to-end: site loads, `git clone` works, `docker compose up` works, UI accessible at localhost:5173

### Account Readiness
- [ ] YouTube channel exists with proper branding (evoila or MEHO)
- [ ] Damir's HN account active (check karma, recent activity)
- [ ] Damir's Reddit account has organic participation history (if not, limit to 1-2 subreddits on day one)
- [ ] Damir's LinkedIn profile up to date
- [ ] Damir's Twitter/X account active
- [ ] evoila company LinkedIn page ready
- [ ] Dev.to account exists (for +2-3 day publication)

### Final Dry Run
- [ ] Open each content file side-by-side with the platform's posting interface -- confirm formatting looks correct
- [ ] All images/screenshots referenced in posts are prepared and sized for each platform
- [ ] Topology graph screenshot exported at high resolution
- [ ] Investigation screenshot exported at high resolution
- [ ] Demo video thumbnail created

---

## Launch Day Timeline (Pacific Time)

| Time (PT) | Action | Platform | Account | Content File |
|-----------|--------|----------|---------|--------------|
| 7:45 AM | Final check: site live, repo public, blog posts accessible, Docker image pullable | -- | -- | -- |
| 8:00 AM | Make YouTube video public, verify embed on meho.ai | YouTube | evoila/MEHO | -- |
| 8:00 AM | Verify blog posts accessible at meho.ai/blog | meho.ai | -- | -- |
| 8:15 AM | Submit Show HN (link to deep-dive article URL, NOT GitHub) | Hacker News | Damir personal | `hn-show-hn.md` |
| 8:16 AM | Post first comment on Show HN immediately | Hacker News | Damir personal | `hn-show-hn.md` (first comment section) |
| 8:30 AM | Post to r/selfhosted (most welcoming to OSS founders) | Reddit | Damir personal | `reddit-selfhosted.md` |
| 9:00 AM | Personal LinkedIn post with images | LinkedIn | Damir personal | `linkedin-personal.md` |
| 9:00 AM | Company LinkedIn post with image | LinkedIn | evoila | `linkedin-company.md` |
| 9:30 AM | Post Twitter thread (all 6 tweets) | Twitter/X | Damir personal | `twitter-thread.md` |
| 10:00 AM | Post to r/devops | Reddit | Damir personal | `reddit-devops.md` |
| 10:30 AM | Post to r/kubernetes | Reddit | Damir personal | `reddit-kubernetes.md` |
| 11:00 AM | Post to r/sysadmin | Reddit | Damir personal | `reddit-sysadmin.md` |
| +2-3 days | Publish Dev.to article (with canonical_url set) | Dev.to | Damir | `devto-article.md` |

**Why this order:**
- Blog posts go live first so every link in every post resolves immediately
- HN at 8:15 AM PT targets the critical first-30-minutes window (engineers check news before stand-up)
- r/selfhosted first because that community is most welcoming to open-source founders
- LinkedIn during business hours (engagement algorithm rewards daytime posting)
- Twitter after HN has initial traction (can reference HN discussion naturally)
- Reddit posts staggered by 30 minutes to avoid spam detection
- Dev.to delayed 2-3 days for canonical URL protection (Google needs to index meho.ai first)

---

## Post-Launch Monitoring (First 4 Hours)

### Hacker News (HIGHEST PRIORITY)
- [ ] Stay active answering comments for 3-4 hours minimum
- [ ] Data-backed responses, humility about limitations, technical depth when challenged
- [ ] If asked about architecture, reference the deep-dive article with specific sections
- [ ] If asked about limitations, be honest (Ollama quality gap, UI polish, early community)
- [ ] Do NOT share the HN URL anywhere asking for upvotes -- share the blog URL instead
- [ ] Monitor position on front page, comment count, upvote trajectory

### Reddit
- [ ] Respond to every comment within 2-4 hours across all 4 subreddits
- [ ] Be transparent about limitations when asked
- [ ] If a post gets removed by moderators, do NOT repost -- message the mods politely
- [ ] r/selfhosted and r/sysadmin tend to have the most detailed technical questions

### LinkedIn
- [ ] Engage with all comments on both personal and company posts (algorithm rewards early engagement)
- [ ] React to shares and mentions
- [ ] Reply to DMs within same day

### Twitter/X
- [ ] Reply to all mentions
- [ ] Retweet positive reactions (with discretion)
- [ ] Quote-tweet interesting takes with thoughtful responses

---

## Anti-Gaming Reminders

**These rules are critical -- violating them can kill the entire launch:**

- Do NOT share the HN URL in Slack, Discord, email, or any internal channel asking for upvotes. HN's ring detection is aggressive and as few as 5-6 coordinated upvotes from shared IPs or timing patterns trigger penalties or post death.
- Share the **blog post URL** instead. People who discover the HN submission organically will upvote it on their own.
- Do NOT edit the HN title after submission. Editing resets the engagement timestamp and loses accumulated ranking momentum.
- Do NOT resubmit to HN if the first attempt doesn't gain traction. Wait at least 24 hours before considering a retry.
- Reddit posts are staggered by 30 minutes to avoid Reddit's automatic spam detection on accounts posting to multiple subreddits in quick succession.
- Do NOT use the same title across Reddit posts. Each subreddit has a different audience and expects different framing.

---

## Metrics to Track

### Day 1 Targets (Aspirational)

| Metric | Target | Where to Check |
|--------|--------|----------------|
| HN front page position | Top 30 | news.ycombinator.com |
| HN comment count | 20+ | Show HN thread |
| GitHub stars | 100+ | GITHUB_URL |
| GitHub forks | 10+ | GITHUB_URL |
| Docker Hub pulls | 50+ | Docker Hub |
| Website unique visitors | 1,000+ | Cloudflare Analytics |
| Reddit total upvotes | 100+ (across 4 subs) | Reddit |
| LinkedIn impressions (personal) | 5,000+ | LinkedIn Analytics |
| Twitter impressions | 2,000+ | Twitter/X Analytics |

### Ongoing (Week 1)

| Metric | Source |
|--------|--------|
| GitHub stars over time | GITHUB_URL |
| GitHub clones | GitHub Insights |
| Docker image pulls | Docker Hub / GHCR |
| Website visitors and time on page | Cloudflare Analytics |
| Community signups (Discord) | Discord server |
| Issues opened | GitHub Issues |
| Contributor interest | GitHub PRs, Discussions |

---

## Day After

- [ ] Write a brief internal retrospective: what worked, what didn't, what to do differently next time
- [ ] Respond to any remaining unanswered comments across all platforms
- [ ] If HN traction was strong, plan a follow-up technical blog post for 1-2 weeks later
- [ ] If any subreddit post was particularly successful, engage more deeply in that community over the following week
- [ ] Review GitHub issues that came in -- triage and label, respond to each one
- [ ] Post a "thank you" update on Twitter/LinkedIn if the launch got meaningful traction
- [ ] Update the internal metrics dashboard with day-1 actuals vs. targets
- [ ] If Dev.to article hasn't been published yet, finalize and schedule it
