# MEHO Demo Video Script

## Video Metadata

| Field | Value |
|-------|-------|
| **Title** | MEHO: One question. Every system. Actual root cause. |
| **Target Length** | 4:00 - 4:30 |
| **Format** | Screen recording with text caption overlays (no voiceover) |
| **Resolution** | 1920x1080 minimum, 4K (3840x2160) preferred |
| **Frame Rate** | 30fps |
| **UI Mode** | Dark mode MEHO interface |
| **Recording Tool** | Screen Studio (macOS) -- auto-zoom and cursor highlighting |
| **Publish To** | YouTube (primary), embed on MEHO_AI_URL hero section |
| **Export Format** | MP4 (H.264) for YouTube upload |

---

## Pre-Recording Checklist

- [ ] MEHO running locally with dark mode UI enabled
- [ ] Browser: Chrome or Firefox, zoomed to 110-120% for YouTube readability
- [ ] Browser bookmarks bar hidden
- [ ] Unnecessary browser extensions hidden or disabled
- [ ] Chat history cleared (start with a fresh, empty session)
- [ ] Screen Studio open and configured for the browser window
- [ ] Notifications silenced (macOS Focus mode)
- [ ] **Connectors configured and healthy (green status):**
  - [ ] Prometheus -- with metrics showing a latency spike on payment-svc
  - [ ] Kubernetes -- with a pod in OOMKilled state on node-7
  - [ ] Loki -- with connection pool exhaustion error logs
  - [ ] VMware -- with ESXi host esxi-prod-03 at high memory utilization
  - [ ] ArgoCD -- with a recent payment-svc deployment (v2.3.1 to v2.4.0)
- [ ] Demo data verified: all five systems return the expected investigation data
- [ ] Test run completed at least once to confirm the full investigation flows end-to-end

---

## The Investigation Scenario

**Setup:** An on-call operator gets paged -- the payment service is experiencing high latency. Instead of opening Grafana, kubectl, vCenter, and four other dashboards, they open MEHO and ask one question.

**What MEHO discovers across 5 systems:**
1. **Prometheus** -- p99 latency spiked 20x, error rate elevated, traffic normal
2. **Kubernetes** -- Pod OOMKilled 3 times, memory at limit, node under pressure
3. **Loki** -- Connection pool exhaustion errors flooding the logs
4. **VMware** -- ESXi host at 92% memory, ballooning active on the VM backing the K8s node
5. **ArgoCD** -- A deployment 4 minutes before the spike introduced an unbounded in-memory cache

**Root cause:** The new deployment's in-memory cache pushed memory usage to the pod limit. The underlying ESXi host was already at 92% memory, causing ballooning on the K8s node VM. The pod hit 512Mi and OOMKilled. Remaining pods overloaded, connection pool saturated, latency spiked 20x.

**Why this matters:** Without MEHO, this investigation requires expertise in five different systems and at least 30 minutes of manual dashboard hopping. MEHO does it in one conversation, in about 60 seconds.

---

## Timestamped Script

### [0:00 - 0:10] Title Card

**Screen:** Black background, MEHO logo centered.

**TEXT CAPTION:** "MEHO -- AI-powered cross-system investigation"

**TEXT CAPTION:** "GITHUB_URL"

**Action:** Hold for 3 seconds, then fade to the MEHO UI.

**Production note:** Use a clean title card with the MEHO logo at native resolution. Subtitle fades in 1 second after the main text.

---

### [0:10 - 0:35] Context Setup

**Screen:** The MEHO chat interface, clean and empty. Dark mode. Sidebar visible showing connected systems.

**Action:** Mouse moves slowly to the sidebar. Hover over each connector status indicator to show they are connected and healthy (green dots).

**TEXT CAPTION:** "5 systems connected: Prometheus, Kubernetes, Loki, VMware, ArgoCD"

**Action:** Brief pause (1-2 seconds) on the connector list. Let the viewer absorb that five production systems are wired up.

**TEXT CAPTION:** "An operator just got paged: payment service latency is through the roof"

**Production note:** The sidebar connector badges should be clearly visible. If Screen Studio auto-zoom is on, let it zoom into the sidebar briefly, then zoom back out to full view.

---

### [0:35 - 1:00] Ask the Question

**Screen:** Full MEHO chat interface, input field in focus.

**Action:** Click into the chat input field. Type the following message at a natural pace (not too fast -- the viewer needs to read along):

> The payment service has been slow for the last 30 minutes

**Action:** Pause for 2 seconds after typing. Let the viewer read the question.

**TEXT CAPTION:** "One question. Let MEHO investigate."

**Action:** Press Enter (or click Send). The "Agent" mode indicator should activate, showing MEHO is dispatching an investigation.

**Production note:** The typing should feel human-paced. Screen Studio cursor highlighting will draw attention to the input field. After sending, the agent mode badge switching from idle to active is a key visual moment.

---

### [1:00 - 1:20] Prometheus Query

**Screen:** MEHO chat area showing the investigation in progress.

**Action:** MEHO dispatches to the Prometheus connector. The connector badge lights up.

**TEXT CAPTION:** "Querying Prometheus metrics..."

**Action:** The Prometheus result appears in the chat:

> p99 latency spiked from 120ms to 2.4s at 14:32 UTC. Error rate 12.3%. Request volume normal -- not a traffic surge.

**TEXT CAPTION:** "Latency 20x normal. Not a traffic surge."

**Action:** Brief pause (2 seconds) for the viewer to absorb the first data point.

**Production note:** If Screen Studio auto-zoom is active, let it zoom into the Prometheus result card as it appears, then pull back.

---

### [1:20 - 1:45] Kubernetes Query

**Screen:** MEHO chat area, previous Prometheus result visible above.

**Action:** MEHO dispatches to the Kubernetes connector. The K8s badge lights up.

**TEXT CAPTION:** "Inspecting Kubernetes pods..."

**Action:** The Kubernetes result appears:

> Pod payment-svc-7b9f4d-xk2p4 on node-7: 3 OOMKilled restarts in last hour. Memory limit 512Mi, usage peaked at 510Mi. Node-7 memory pressure: True.

**TEXT CAPTION:** "3 OOMKills. Memory is the bottleneck."

**Production note:** The OOMKilled text in the result is the first major clue. If possible, let Screen Studio briefly highlight the pod name and the OOMKilled count.

---

### [1:45 - 2:10] Loki Query

**Screen:** MEHO chat area, Prometheus and K8s results visible above.

**Action:** MEHO dispatches to the Loki connector. The Loki badge lights up.

**TEXT CAPTION:** "Pulling error logs..."

**Action:** The Loki result appears:

> ERROR 14:31:47 "Connection pool exhausted, waiting for available connection" (47 occurrences). WARN "GC pause 340ms" (12 occurrences).

**TEXT CAPTION:** "47 connection pool exhaustion errors. The app is drowning."

**Production note:** The log data reinforces the Kubernetes finding. The connection pool saturation is a direct consequence of pods restarting. Let the viewer connect these dots.

---

### [2:10 - 2:40] VMware Query

**Screen:** MEHO chat area, three connector results visible above (scrolling if needed).

**Action:** MEHO dispatches to the VMware connector. The VMware badge lights up.

**TEXT CAPTION:** "Checking the underlying hypervisor..."

**Action:** The VMware result appears:

> VM node-7: 4 vCPU, 8GB RAM. Host esxi-prod-03: 92% memory utilization (187GB / 204GB). Memory ballooning active on 6 VMs including node-7.

**TEXT CAPTION:** "ESXi host at 92% memory. Ballooning active."

**Action:** Pause slightly longer here (3 seconds). This is the inflection point of the investigation -- the moment where MEHO goes deeper than most operators would.

**TEXT CAPTION:** "This is where traditional troubleshooting stops. Most operators never check the hypervisor."

**Production note:** This is the most important transition in the video. The VMware finding reveals the infrastructure layer that is invisible to most application teams. The slightly longer pause lets the significance sink in before the investigation continues.

---

### [2:40 - 3:00] ArgoCD Query

**Screen:** MEHO chat area, four connector results visible (may need to scroll).

**Action:** MEHO dispatches to the ArgoCD connector. The ArgoCD badge lights up.

**TEXT CAPTION:** "What changed recently?"

**Action:** The ArgoCD result appears:

> Last sync 14:28 UTC: payment-svc image updated v2.3.1 to v2.4.0. Change: added in-memory cache (commit abc1234).

**TEXT CAPTION:** "A deployment 4 minutes before the spike. New in-memory cache."

**Production note:** The timeline correlation -- deployment at 14:28, latency spike at 14:32 -- is immediately suspicious. MEHO connects this without the operator needing to check ArgoCD manually.

---

### [3:00 - 3:45] Synthesis -- The "Holy Shit" Moment

**Screen:** MEHO chat area. All five connector results are visible. MEHO begins synthesizing.

**Action:** MEHO's synthesis indicator activates. The "Synthesizing root cause..." label appears.

**TEXT CAPTION:** "MEHO connects the evidence across all 5 systems..."

**Action:** The full synthesis appears in a highlighted result card:

> payment-svc v2.4.0 (deployed 14:28) added an unbounded in-memory cache, increasing baseline memory usage. Combined with ESXi host esxi-prod-03 at 92% memory causing ballooning on the node-7 VM, the pod hits its 512Mi limit and OOMKills. Remaining pods overloaded, saturating the connection pool (20/20 active, 47 pending), driving p99 latency from 120ms to 2.4s.

**Action:** Slow zoom (Screen Studio) on the synthesis text. Hold for 4-5 seconds. Let the viewer read the complete root cause.

**TEXT CAPTION:** "Root cause: new cache + hypervisor memory pressure = cascade failure"

**Action:** Hold the zoomed synthesis for 3 more seconds.

**TEXT CAPTION:** "One question. 5 systems. ~60 seconds. Actual root cause."

**Production note:** This is the peak moment of the video. The synthesis text must be fully readable on screen. Use Screen Studio's slow zoom to draw the viewer into the synthesis card. The two emphasis captions ("Root cause..." and "One question...") should feel like a one-two punch. Do not rush this section.

---

### [3:45 - 4:10] The Perspective Shift

**Screen:** Pull back from the synthesis to show the full MEHO interface -- the complete investigation visible in one scrollable chat.

**TEXT CAPTION:** "Without MEHO, this investigation takes 30+ minutes across 6 dashboards"

**TEXT CAPTION:** "And requires expertise in Prometheus, Kubernetes, VMware, Loki, AND ArgoCD"

**TEXT CAPTION:** "MEHO closes the expertise gap."

**Action:** Brief pause (2 seconds) on the full interface view. The complete investigation is visible as a single conversation -- one input, five system queries, one synthesis.

**Production note:** The wide shot after the zoom creates visual contrast. The viewer sees the entire investigation fits in a single chat window. This reinforces the "one conversation" message.

---

### [4:10 - 4:30] End Card

**Screen:** Fade from the MEHO interface to a clean end card. MEHO logo centered. Dark background.

**TEXT CAPTION:** "15 connectors. All open source. 5-minute setup."

**TEXT CAPTION:** "git clone GITHUB_URL && docker compose up -d"

**TEXT CAPTION:** "MEHO_AI_URL"

**TEXT CAPTION:** "Star us on GitHub"

**Action:** Hold end card for 5 seconds. Fade to black.

**Production note:** The `git clone` command should be displayed in a monospace font or code-styled caption to differentiate it from the other text. The GitHub CTA is the final action the viewer should take.

---

## Production Notes

### Timing Summary

| Segment | Timestamps | Duration | Content |
|---------|-----------|----------|---------|
| Title Card | 0:00 - 0:10 | 10s | Logo, tagline, GitHub URL |
| Context Setup | 0:10 - 0:35 | 25s | Connected systems, page context |
| Ask the Question | 0:35 - 1:00 | 25s | Type question, send, agent activates |
| Prometheus Query | 1:00 - 1:20 | 20s | Latency spike data |
| Kubernetes Query | 1:20 - 1:45 | 25s | OOMKilled pods |
| Loki Query | 1:45 - 2:10 | 25s | Connection pool errors |
| VMware Query | 2:10 - 2:40 | 30s | Hypervisor memory pressure |
| ArgoCD Query | 2:40 - 3:00 | 20s | Recent deployment |
| Synthesis | 3:00 - 3:45 | 45s | Root cause reveal |
| Perspective Shift | 3:45 - 4:10 | 25s | Context and impact |
| End Card | 4:10 - 4:30 | 20s | CTA and links |
| **Total** | | **~4:30** | |

### Caption Guidelines

- Every TEXT CAPTION should stay on screen for a minimum of 2-3 seconds for comfortable reading
- Use bold, larger captions for key moments: root cause reveal, perspective shift, end card CTA
- White text (#FFFFFF) with dark semi-transparent background (rgba(0, 0, 0, 0.75)) for all captions
- Captions positioned at bottom-center by default, top-center when overlaying system results
- Cursor highlighting via Screen Studio auto-zoom on key UI elements (connector badges, result cards, synthesis)
- Dark mode MEHO UI throughout -- do not switch themes
- No voiceover -- text captions are the only narration
- Transitions between segments should be smooth (no hard cuts except to/from title and end cards)

### Recording Tips

1. **Practice the typing:** The question should be typed naturally, not pasted. Practice typing "The payment service has been slow for the last 30 minutes" at a comfortable pace.
2. **Let the UI breathe:** After each connector result appears, wait 2-3 seconds before the next query starts. The viewer needs time to read.
3. **Screen Studio zoom:** Enable auto-zoom. It will naturally zoom into the active area (input field, result cards). This creates a professional, focused feel without manual editing.
4. **Multiple takes:** Record 2-3 complete takes. Pick the one with the best pacing and cleanest UI interactions.
5. **Check scroll position:** As connector results stack up, ensure the chat auto-scrolls so the latest result is always visible. If not, manually scroll before the next result appears.
6. **Browser setup:** Use a clean browser profile with no extensions visible. Set zoom to 110-120% so text is readable at 1080p on YouTube.

### Post-Production Checklist

- [ ] Add text captions at all marked positions (see caption-overlay-notes.md)
- [ ] Add title card (0:00 - 0:10) and end card (4:10 - 4:30)
- [ ] Verify all captions are readable at 720p (minimum YouTube quality)
- [ ] Trim dead time (pauses longer than 3 seconds between segments)
- [ ] Add subtle background music (optional -- low ambient, not distracting)
- [ ] Export at 1080p minimum, 4K preferred
- [ ] Upload to YouTube with title, description, tags, and thumbnail
- [ ] Embed YouTube link on MEHO_AI_URL hero section
- [ ] Extract social media clips (see caption-overlay-notes.md for timestamps)
