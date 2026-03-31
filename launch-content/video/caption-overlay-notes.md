# Caption Overlay Notes -- MEHO Demo Video

Production reference for adding text captions during video editing. Every caption from the demo script is listed here with exact timing, style, and positioning.

---

## 1. Caption Style Guide

### Font

- **Family:** System sans-serif -- SF Pro Display (macOS), Inter, or Helvetica Neue
- **Weight:** Bold (700) for all captions, Extra Bold (800) for emphasis captions
- **Size (at 1080p):**
  - Standard captions: 28pt equivalent
  - Emphasis captions: 42pt equivalent (1.5x standard)
  - Data captions: 24pt equivalent
- **Scaling:** If exporting at 4K, double all sizes (56pt / 84pt / 48pt)

### Colors

- **Text color:** White (#FFFFFF)
- **Background:** Dark semi-transparent pill
  - CSS: `background: rgba(0, 0, 0, 0.75); border-radius: 8px; padding: 12px 24px;`
  - For emphasis captions: add a subtle white outer glow (`box-shadow: 0 0 30px rgba(255, 255, 255, 0.1)`)

### Positioning

- **Default:** Bottom-center of screen, 80px above the bottom edge
- **Data overlays:** Top-center, 60px below the top edge (so they don't cover the connector result cards)
- **Emphasis captions:** Dead center of screen (both horizontally and vertically)

### Timing

- **Standard captions:** 2-3 seconds on screen (adjust for reading length)
- **Emphasis captions:** 3-4 seconds on screen
- **Data captions:** 2 seconds on screen

### Transitions

- **Fade in:** 0.3 seconds
- **Hold:** Duration specified per caption below
- **Fade out:** 0.3 seconds
- **Gap between consecutive captions:** Minimum 0.5 seconds (no overlapping captions)

---

## 2. Caption Table

Every caption from the demo script, in chronological order.

| # | Timestamp | Caption Text | Style | Position | Duration |
|---|-----------|-------------|-------|----------|----------|
| 1 | 0:02 | MEHO -- AI-powered cross-system investigation | standard | center | 4s |
| 2 | 0:06 | GITHUB_URL | standard | bottom-center | 3s |
| 3 | 0:12 | 5 systems connected: Prometheus, Kubernetes, Loki, VMware, ArgoCD | standard | bottom-center | 3s |
| 4 | 0:25 | An operator just got paged: payment service latency is through the roof | standard | bottom-center | 3s |
| 5 | 0:42 | One question. Let MEHO investigate. | standard | bottom-center | 3s |
| 6 | 1:02 | Querying Prometheus metrics... | data | top-center | 2s |
| 7 | 1:10 | Latency 20x normal. Not a traffic surge. | standard | bottom-center | 3s |
| 8 | 1:22 | Inspecting Kubernetes pods... | data | top-center | 2s |
| 9 | 1:32 | 3 OOMKills. Memory is the bottleneck. | standard | bottom-center | 3s |
| 10 | 1:47 | Pulling error logs... | data | top-center | 2s |
| 11 | 1:57 | 47 connection pool exhaustion errors. The app is drowning. | standard | bottom-center | 3s |
| 12 | 2:12 | Checking the underlying hypervisor... | data | top-center | 2s |
| 13 | 2:22 | ESXi host at 92% memory. Ballooning active. | standard | bottom-center | 3s |
| 14 | 2:30 | This is where traditional troubleshooting stops. Most operators never check the hypervisor. | emphasis | center | 4s |
| 15 | 2:42 | What changed recently? | data | top-center | 2s |
| 16 | 2:50 | A deployment 4 minutes before the spike. New in-memory cache. | standard | bottom-center | 3s |
| 17 | 3:05 | MEHO connects the evidence across all 5 systems... | standard | bottom-center | 3s |
| 18 | 3:20 | Root cause: new cache + hypervisor memory pressure = cascade failure | emphasis | center | 4s |
| 19 | 3:32 | One question. 5 systems. ~60 seconds. Actual root cause. | emphasis | center | 4s |
| 20 | 3:48 | Without MEHO, this investigation takes 30+ minutes across 6 dashboards | standard | bottom-center | 3s |
| 21 | 3:55 | And requires expertise in Prometheus, Kubernetes, VMware, Loki, AND ArgoCD | standard | bottom-center | 3s |
| 22 | 4:03 | MEHO closes the expertise gap. | emphasis | center | 3s |
| 23 | 4:12 | 15 connectors. All open source. 5-minute setup. | emphasis | center | 3s |
| 24 | 4:17 | git clone GITHUB_URL && docker compose up -d | standard | center | 3s |
| 25 | 4:22 | MEHO_AI_URL | standard | center | 3s |
| 26 | 4:26 | Star us on GitHub | standard | center | 3s |

**Total captions:** 26

---

## 3. Key Moment Callouts

These captions need special visual treatment -- they are the emotional peaks of the video.

### Callout 1: The Hypervisor Reveal (2:30)

| Field | Value |
|-------|-------|
| **Caption** | This is where traditional troubleshooting stops. Most operators never check the hypervisor. |
| **Style** | Emphasis -- 42pt, bold, white with glow |
| **Position** | Dead center of screen |
| **Duration** | 4 seconds |
| **Context** | Appears after the VMware query result. This is the moment the viewer realizes MEHO goes deeper than they would manually. |
| **Visual treatment** | Dim the background UI slightly (overlay at 20% opacity) so the caption is the focal point. |

### Callout 2: The Root Cause Reveal (3:20)

| Field | Value |
|-------|-------|
| **Caption** | Root cause: new cache + hypervisor memory pressure = cascade failure |
| **Style** | Emphasis -- 42pt, extra bold, white with glow |
| **Position** | Dead center of screen |
| **Duration** | 4 seconds |
| **Context** | The synthesis just appeared in the chat. This caption summarizes the root cause in one line. |
| **Visual treatment** | This is the peak. Consider a subtle zoom pulse (scale 100% to 102% over 0.5s, then back). Dim background at 30% opacity. |

### Callout 3: The Perspective Shift (3:32)

| Field | Value |
|-------|-------|
| **Caption** | One question. 5 systems. ~60 seconds. Actual root cause. |
| **Style** | Emphasis -- 42pt, bold, white with glow |
| **Position** | Dead center of screen |
| **Duration** | 4 seconds |
| **Context** | Immediately follows the root cause reveal. This is the "holy shit" punchline. |
| **Visual treatment** | Let this caption sit on a slightly zoomed-out view of the full chat, showing the entire investigation in one window. The contrast between "one question" and the full investigation visible behind the caption drives the point home. |

### Callout 4: End Card CTA (4:12)

| Field | Value |
|-------|-------|
| **Caption** | 15 connectors. All open source. 5-minute setup. |
| **Style** | Emphasis -- 42pt, bold, white |
| **Position** | Center, on the end card (dark background) |
| **Duration** | 3 seconds |
| **Context** | First text on the end card. Sets up the call to action. |
| **Visual treatment** | Clean text on dark background. No background blur needed (already on the end card). |

---

## 4. Caption Style Reference

Quick reference for the three caption styles used throughout the video.

### Standard Style

```
Font: SF Pro Display Bold, 28pt (at 1080p)
Color: #FFFFFF
Background: rgba(0, 0, 0, 0.75), border-radius 8px, padding 12px 24px
Position: bottom-center (default) or top-center (for data overlays)
Duration: 2-3 seconds
Transition: Fade in 0.3s, fade out 0.3s
```

Used for: Narrative captions, context-setting text, connector query descriptions.

### Emphasis Style

```
Font: SF Pro Display Extra Bold, 42pt (at 1080p)
Color: #FFFFFF
Background: rgba(0, 0, 0, 0.80), border-radius 12px, padding 16px 32px
Glow: box-shadow 0 0 30px rgba(255, 255, 255, 0.1)
Position: dead center of screen
Duration: 3-4 seconds
Transition: Fade in 0.3s, fade out 0.3s
Optional: Dim background UI to 20-30% opacity during emphasis captions
```

Used for: Root cause reveal, perspective shift, end card CTA, expertise gap statement.

### Data Style

```
Font: SF Pro Display Bold, 24pt (at 1080p)
Color: #FFFFFF
Background: rgba(0, 0, 0, 0.65), border-radius 6px, padding 8px 16px
Position: top-center (above the connector result card)
Duration: 2 seconds
Transition: Fade in 0.2s, fade out 0.2s
```

Used for: "Querying Prometheus metrics...", "Inspecting Kubernetes pods...", and other system query labels.

---

## 5. Social Media Clip Notes

Segments to extract for short-form social media distribution.

### Twitter/LinkedIn Clip (30 seconds)

| Field | Value |
|-------|-------|
| **Source timestamps** | 3:00 - 3:45 |
| **Content** | The synthesis reveal -- MEHO connecting evidence from 5 systems to a single root cause |
| **Aspect ratio** | 16:9 (native) |
| **Why this segment** | It is the "holy shit" moment standalone. Viewers see the synthesis appear and the root cause caption. Works without context because the synthesis text itself explains the full story. |
| **Captions to include** | #17, #18, #19 from the caption table |
| **Add to clip** | Opening text "MEHO just traced a payment service slowdown across 5 systems in 60 seconds..." (2s, then cut to the synthesis reveal) |
| **End of clip** | Quick flash of GITHUB_URL and MEHO_AI_URL (2s) |

### Instagram/TikTok Vertical Clip (60 seconds)

| Field | Value |
|-------|-------|
| **Source timestamps** | 0:35 - 1:45 |
| **Content** | The question being typed, Prometheus result, Kubernetes result -- the first two "clues" |
| **Aspect ratio** | 9:16 (vertical) -- crop the center of the 16:9 frame, focusing on the chat area |
| **Why this segment** | Shows the core experience: type a question, get real data from real systems. The OOMKilled reveal is a mini-hook that makes people want to watch the full video. |
| **Captions to include** | #5, #6, #7, #8, #9 from the caption table |
| **Add to clip** | Opening text "What if you could diagnose infrastructure problems with one question?" (3s) |
| **End of clip** | "Watch the full investigation -- link in bio" + MEHO_AI_URL (3s) |

### YouTube Shorts Clip (45 seconds)

| Field | Value |
|-------|-------|
| **Source timestamps** | 2:10 - 3:00 |
| **Content** | VMware query (the hypervisor reveal) + ArgoCD query (the deployment correlation) |
| **Aspect ratio** | 9:16 (vertical) -- crop center of frame |
| **Why this segment** | Shows the depth of MEHO's investigation -- going from application layer to hypervisor layer is the differentiator. The "most operators never check the hypervisor" caption is the hook. |
| **Captions to include** | #12, #13, #14, #15, #16 from the caption table |
| **Add to clip** | Opening hook "Most operators never check the hypervisor." (2s, emphasis style) |
| **End of clip** | "Full demo on our channel" + subscribe CTA (3s) |

---

## 6. Placeholder Links

The following placeholders appear in captions and must be replaced before publishing:

| Placeholder | Replace With | Where Used |
|-------------|-------------|------------|
| `GITHUB_URL` | The public GitHub repository URL (e.g., `https://github.com/evoila/meho`) | Caption #2, #24 |
| `MEHO_AI_URL` | The marketing website URL (e.g., `https://meho.ai`) | Caption #25 |

**Do not publish the video until both URLs are finalized and live.**
