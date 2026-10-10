# SkyRules AI: UI specification

The production UI matches the team's approved clickable mockup ("SkyRules AI UI Mockup"). Its source is in `docs/ui/mockup_reference.dc.html`, a design-tool format that does not run on its own: read it for layout, styles and behavior, but build the real page as plain HTML, CSS and JavaScript in `src/app/static/`.

**Owner:** Pavan (P3, P4 in `CLAUDE.md`). The UI is time-boxed; evaluation work always comes first.

## 1. Principles

- **Only show verified content.** An answer is never streamed token by token. The user sees pipeline stages live, then the checked answer.
- **Make grounding visible:** a citation on every sentence, the passage preview on hover, source cards with page dates, and the retrieval trace.
- **Be honest about limits:** a reliability banner, a clear refusal card and a location referral.
- **Aviation feel without imitation:** never use FAA logos, seals or official branding, because the app must not look like an official FAA tool.

## 2. Tokens

Fonts: **IBM Plex Sans** (400, 500, 600, 700) for text and **IBM Plex Mono** (400, 500, 600) for numbers, timings, IDs and labels, loaded from Google Fonts with `system-ui, sans-serif` and `monospace` fallbacks.

Sizes: body 16 px, line height 1.5; answer text 17 px at 1.6; card titles 17 px semibold; hero title 32 px bold; small labels 12 to 13 px; mono labels in capitals with 0.06 to 0.08em letter spacing.

Shape: cards have a 16 px radius with a 1 px border; inner panels 10 to 12 px; pills 999 px; touch targets at least 44 px tall (header controls 36 px).

Colors are CSS custom properties on the root element; cockpit mode swaps the whole set.

| Token | Daylight | Cockpit (dark) | Used for |
|---|---|---|---|
| `--bg` | `#F2F4F6` | `#0B1522` | page background |
| `--card` | `#FFFFFF` | `#13233A` | cards |
| `--card2` | `#FAFBFC` | `#101E31` | B2 card, stage rows |
| `--ink` | `#0E1A2B` | `#E8EEF5` | main text |
| `--ink2` | `#33435A` | `#C9D3DD` | secondary text |
| `--muted` | `#4A5868` | `#A3B3C5` | captions, labels |
| `--faint` | `#9AA9BC` | `#5E7088` | pending states, dashed borders |
| `--line` | `#D5DCE3` | `#2A3D57` | card borders |
| `--line2` | `#E1E6EB` | `#22344C` | dividers |
| `--line3` | `#C9D3DD` | `#3A5072` | input and chip borders |
| `--sel` | `#F3F7FC` | `#18304F` | selected source, kept trace rows |
| `--tint` | `#E7EFF8` | `#1A3253` | banner, badges, follow-up chips |
| `--tintInk` | `#173F6E` | `#C4DAF4` | text on tint |
| `--tintLine` | `#B9CEE6` | `#2F5585` | tint borders |
| `--track` | `#EEF1F4` | `#22344C` | score bar track |
| `--blue` | `#1D5FA8` | `#7DB2EE` | links, citation text |
| `--warnBg` | `#FDEEE6` | `#36200F` | referral, flags |
| `--warnLine` | `#F0B999` | `#7A3D1F` | referral border |
| `--warnInk` | `#8A2F08` | `#FFB892` | warning text |
| `--warnText` | `#3B2417` | `#F3D9CA` | referral body |
| `--activeBg` | `#FFF5EF` | `#2C1F16` | active stage |
| `--good` | `#1E7A46` | `#5BCB8D` | "Cleared" text |
| `--goodBg` | `#E5F4EB` | `#123526` | fresh badge |
| `--solid` / `--onSolid` | `#0E1A2B` / `#FFFFFF` | `#E8EEF5` / `#0B1522` | primary link buttons |
| `--bubble` | `#0E1A2B` | `#2B4A73` | current user question |
| `--bubbleOld` | `#33435A` | `#26364C` | earlier questions |
| `--tip` | `#0E1A2B` | `#22344C` | citation preview popover |
| `--head` | `#0E1A2B` | `#08111C` | header bar |

Fixed colors in both modes:
- **Caution orange** `#D9480F`: the drone body, refusal border, stopped stage and B2 toggle when on.
- **Primary blue button** `#1D5FA8` with white text: the Ask button and active citation chip.
- **Progress blue** `#4C8BD6`: the track fill, rotor tint and kept rerank bars.
- **Check green** `#1E7A46` / `#2E9E5E`.

Contrast: text at least 4.5:1 on its background in both modes.

## 3. Layout

1. **Header bar** (`--head`): drone mark + "SkyRules AI" + "U.S. drone rules, answered from FAA pages"; on the right, the pilot toggle, "Compare B2 vs B3", and "Cockpit mode" / "Daylight mode". Wraps on narrow screens.
2. **Reliability banner** (`--tint`): "Student project, not official FAA guidance. Check the linked FAA page before you fly."
3. **Main area:** max width 1440 px, 24 to 32 px padding, two columns that wrap:
   - **Conversation** (`flex: 999 1 560px`)
   - **Side panel** (`flex: 1 1 340px`): Sources card, then the Retrieval trace card
4. **Phone width:** columns stack, the stage grid collapses, and the header wraps. Nothing scrolls sideways.

## 4. Components and states

### 4.1 Pilot toggle
A segmented control: "Recreational", "Part 107", "Not sure" (default). It uses `aria-pressed` and is sent as `pilot_type` to `/api/ask`. The router uses it, and the answer header shows "For: Recreational flyer" / "Part 107 pilot" / "Not sure, covering both".

### 4.2 Empty state
- **Background:** a faint sectional-chart pattern of three dashed airspace rings and a few contour lines, in SVG with low opacity.
- **Drone:** a hovering drone with spinning rotors.
- **Title:** "Ask about U.S. drone rules"
- **Text:** "Every answer comes only from FAA pages, with a source on each claim. If the pages don't cover your question, SkyRules says so instead of guessing."
- **Example chips** (the demo questions):
  - "How high can I fly as a hobby flyer?"
  - "Can I fly in San Diego tonight at 10 pm?"
  - "Which drone should I buy?"

### 4.3 Conversation
- **Turn label** "Turn N · chat history is used to understand follow-ups", plus a "New chat" button that clears history.
- **Earlier turns:** a compact question bubble (`--bubbleOld`) and a one-line summary, for example "Verified answer, 1 source, 4 of 4 checks passed." or "Refused: not covered by the FAA pages."
- **Current question:** a right-aligned bubble (`--bubble`). If `rewritten_question` is set, show under it in mono 12 px: "Rewritten with chat history: …".

### 4.4 Preflight card (driven by server-sent events)
- **Header:** the mono label "PREFLIGHT" plus a status line:
  - "Running checklist" (ink) while running
  - "Cleared: answer verified" (`--good`) when answered
  - "Not cleared: not enough support in FAA pages" (`--warnInk`) when refused
  - elapsed seconds on the right
- **Flight path:** a dashed line, with a solid fill growing to the current stage, one diamond waypoint per stage (filled when reached, orange when stopped) and the small drone moving along it with a 0.5 s ease.
- **Stage grid:** one tile per stage, with a status icon (green check, orange spinner, grey ring, or orange "!" when stopped), its name and "N ms" / "in progress" / "waiting".
- **Stages for an answer:** "Understanding question", "Searching 20 FAA passages", "Reranking to top 5", "Writing answer", "Checking 4 safeguards".
- **Stages for a refusal at the gate:** the first three, then "Evidence gate: not enough support" (stopped).

### 4.5 Verified answer card
- **Header:** "Verified answer", the pilot badge and, when `router_tag` isn't normal, a warning-colored tag such as "Location-specific".
- **Key figure (optional):** if the first sentence contains a number with an aviation unit, show it as an instrument readout: the dark panel, the value in mono 44 px, the unit in orange, "KEY FIGURE" and a caption.
  - Units: feet/ft, pounds/lb, grams/g, mph, knots, miles, days, months, years.
  - The number must come from the number-and-date check, so it is verified, never generated.
- **Sentences:** revealed one by one, about 380 ms apart, each followed by citation chips `[n]`.
- **Citation chip:**
  - A real `<button>`.
  - Clicking it highlights the matching source card.
  - Hovering or focusing it shows a popover (`--tip`, 300 px wide) with "PASSAGE FROM SOURCE n", the supporting passage (at most 25 words, the matched words highlighted in `#FFD8C2`) and the source title and path.
- **Location referral** (when `referral.kind == "location"`): a warning panel, "Check your exact location before you fly", the referral text, and the buttons "Open B4UFLY" (https://www.faa.gov/uas/getting_started/b4ufly) and "UAS Facility Maps" (https://www.faa.gov/uas/commercial_operators/uas_facility_maps).
- **Checks row:** the mono label "CLEARED 4/4", then checks for "Citations", "Claims", "Numbers" and "Quotes", and "Answered in X.XX s" on the right. Show the real results from `trace.checks`.

### 4.6 Refusal card
- **Style:** a 2 px orange border.
- **Text:** "I don't have that information in the FAA pages I can search."
- **Second line:** "SkyRules only answers from official FAA drone pages, so it won't guess. Try a question about registration, airspace, Remote ID or night flying."
- **Footer:** "Evidence gate: no passage scored above the threshold" (or which check failed) and the time.

### 4.7 Compare B2 vs B3
When the toggle is on, the backend runs configs b2 and b3 for the same question.
- **B2 card:** a dashed border on `--card2`, "B2 BASELINE", "Dense retrieval, fixed chunks, no reranker, no answer checks".
- **Under each B2 sentence:** a warning flag naming the check it would fail, computed by running the B3 checks over the B2 output: "No citation", "Claim not supported by cited passage", "Number not found in source", "Quote over 25 words".
- **B3 side:** labelled "B3 FINAL SYSTEM" above the normal answer.
- **Unlike the mockup,** there is no "illustrative" footer; these are real outputs.

### 4.8 Follow-up chips
After the answer, show "Ask next" with 2 chips. Build them from the top reranked chunks' heading paths, for example "Tell me more about Remote ID", or use fixed suggestions by router tag. Never use an extra LLM call for this.

### 4.9 Sources card
One card per citation:
- `[n]`, a `page_type` badge and a **freshness badge** from `effective_date`:
  - **within 1 year:** green, "Updated within a year"
  - **1 to 2 years:** blue, "Over a year old"
  - **over 2 years:** amber, "Over 2 years old, check for changes"
  - **unknown:** dashed, "Date not found on page"
- The title as a link to `source_url`, the mono path, and "Last updated: <date>".

The empty state text is "The FAA pages behind each answer appear here, with how recently each page was updated."

### 4.10 Retrieval trace card
A collapsible card: "Retrieval trace" with "Show" / "Hide" (`aria-expanded`).
- **Contents:** all 20 candidates from `trace.candidates` with the label "title › heading", the rerank score in mono, and two bars (dense, rerank). The top 5 after reranking are highlighted (`--sel`).
- **Hover detail:** the BM25 and fused scores show in a tooltip or a small table row.

### 4.11 Input
A text field labelled "Your drone question" (visually hidden label), with the placeholder "Ask about registration, airspace, Remote ID, night flying..." and the "Ask" button. Enter submits.

## 5. Behavior and data

- **`POST /api/ask`** with `{question, history, pilot_type, compare}` returns a server-sent event stream:
  - `stage`: `{name, status: started|done|stopped, ms}`
  - `answer`: the full `answer()` contract
  - with `compare`, a second `answer` event with `config: "b2"`
- **History:** kept in the page (the last 6 turns) and sent with each request; `answer()` uses it only for the follow-up rewrite.
- **Remove the "Mockup · sample data" tag;** every number on screen comes from the real response.
- **Motion:** rotor spin, hero hover and reveal fades; all off under `prefers-reduced-motion`.
- **Errors:** if the stream fails, show the refusal card with "Something went wrong, so SkyRules did not answer. Try again." Never show a partial answer.

## 6. Accessibility

- Real `<button>`, `<a href>` and `<input>` with a `<label>`; no click handlers on divs.
- Icon-only controls get `aria-label`; decorative SVGs get `aria-hidden="true"`.
- A visible focus outline (3 px, `#4C8BD6`); everything works by keyboard (Tab to citation chips opens the preview).
- Colors that carry meaning also differ in lightness and come with text (for example "CLEARED 4/4", not color alone).
