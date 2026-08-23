# Pi Web Access — Curator UI Design System

> A reusable design system extracted from the Pi Web Access search curator interface. Dark-first with automatic light mode support.

---

## 1. Design Tokens

### 1.1 Color Palette

| Token | Dark | Light | Usage |
|-------|------|-------|-------|
| `--bg` | `#18181e` | `#f5f5f7` | Page background |
| `--bg-card` | `#1e1e24` | `#ffffff` | Card / panel background |
| `--bg-elevated` | `#252530` | `#eeeef0` | Elevated surfaces (inputs, kbd) |
| `--bg-hover` | `#2b2b37` | `#e4e4e8` | Hover states |
| `--fg` | `#e0e0e0` | `#1a1a1e` | Primary text |
| `--fg-muted` | `#909098` | `#6c6c74` | Secondary text |
| `--fg-dim` | `#606068` | `#9a9aa2` | Tertiary / placeholder text |
| `--accent` | `#8abeb7` | `#5f8787` | Primary accent (teal) |
| `--accent-hover` | `#9dcec7` | `#4a7272` | Accent hover |
| `--accent-muted` | `rgba(138,190,183,0.15)` | `rgba(95,135,135,0.12)` | Subtle accent tint |
| `--accent-subtle` | `rgba(138,190,183,0.08)` | `rgba(95,135,135,0.06)` | Very subtle accent tint |
| `--border` | `#2a2a34` | `#dcdce0` | Borders |
| `--border-muted` | `#353540` | `#c8c8d0` | Subtle borders |
| `--border-checked` | `#8abeb7` | `#5f8787` | Checked card border |
| `--check-bg` | `#8abeb7` | `#5f8787` | Checkbox fill |
| `--btn-primary` | `#8abeb7` | `#5f8787` | Primary button bg |
| `--btn-primary-hover` | `#9dcec7` | `#4a7272` | Primary button hover |
| `--btn-primary-fg` | `#18181e` | `#ffffff` | Primary button text |
| `--btn-secondary` | `#252530` | `#e4e4e8` | Secondary button bg |
| `--btn-secondary-hover` | `#2b2b37` | `#d4d4d8` | Secondary button hover |
| `--timer-bg` | `#252530` | `#e4e4e8` | Timer badge bg |
| `--timer-fg` | `#909098` | `#6c6c74` | Timer badge text |
| `--timer-warn-bg` | `rgba(240,198,116,0.15)` | `rgba(217,119,6,0.10)` | Timer warning bg |
| `--timer-warn-fg` | `#f0c674` | `#92400e` | Timer warning text |
| `--timer-urgent-bg` | `rgba(204,102,102,0.15)` | `rgba(175,95,95,0.10)` | Timer urgent bg |
| `--timer-urgent-fg` | `#cc6666` | `#991b1b` | Timer urgent text |
| `--overlay-bg` | `rgba(24,24,30,0.92)` | `rgba(255,255,255,0.92)` | Modal overlay |
| `--success` | `#b5bd68` | `#4d7c0f` | Success states |
| `--warning` | `#f0c674` | `#b45309` | Warning states |

### 1.2 Typography

| Token | Font | Weight | Usage |
|-------|------|--------|-------|
| `--font` | `'Outfit', system-ui, -apple-system, sans-serif` | 400–700 | Body, UI elements |
| `--font-display` | `'Instrument Serif', Georgia, serif` | 400 (italic) | Hero titles |
| `--font-mono` | `'SF Mono', Consolas, monospace` | 500 | Code, shortcuts |

| Scale | Size | Line-Height | Letter-Spacing |
|-------|------|-------------|----------------|
| Hero title | `40px` | `1.1` | `-0.01em` |
| Section title | `14px` | `1.3` | — |
| Body | `13.5px` | `1.5` | — |
| Small | `12px` | `1.45` | `0.04em` (uppercase) |
| Micro | `10px–11px` | `1.3` | `0.06em` (uppercase) |

### 1.3 Spacing & Shape

| Token | Value |
|-------|-------|
| `--radius` | `10px` |
| `--radius-sm` | `6px` |
| `--radius-pill` | `999px` |
| Max content width | `640px` |
| Card padding | `14px 16px` |
| Section gap | `8px–14px` |
| Action bar height | `72px` (incl. safe area) |

### 1.4 Shadows & Effects

```css
/* Card shadow */
box-shadow: 0 1px 2px rgba(0,0,0,0.06);

/* Timer badge shadow */
box-shadow: 0 2px 8px rgba(0,0,0,0.2);

/* Action bar backdrop */
backdrop-filter: blur(12px);
background: color-mix(in srgb, var(--bg) 90%, transparent);

/* Hero radial gradient */
background-image: radial-gradient(ellipse at 50% 0%, var(--accent-muted) 0%, transparent 60%);
```

---

## 2. Layout Architecture

```
┌─────────────────────────────────────┐
│  [Timer Badge]              top:20  │  z:50
│                                     │
│  ┌─────────────────────────────┐    │
│  │  HERO                        │    │
│  │  kicker / title / desc       │    │
│  │  status · provider buttons   │    │
│  └─────────────────────────────┘    │
│                                     │
│  ┌─────────────────────────────┐    │
│  │  RESULT CARDS                │    │
│  │  ┌─ searching card ─┐       │    │
│  │  ├─ result card ────┤       │    │
│  │  └─ error card ─────┘       │    │
│  └─────────────────────────────┘    │
│                                     │
│  ┌─────────────────────────────┐    │
│  │  ADD SEARCH                  │    │
│  │  + [input........] [✨]      │    │
│  └─────────────────────────────┘    │
│                                     │
│  ┌─────────────────────────────┐    │
│  │  SUMMARY PANEL               │    │
│  │  header / generating /       │    │
│  │  textarea / actions          │    │
│  └─────────────────────────────┘    │
│                                     │
│  ┌─────────────────────────────┐    │  z:10 fixed
│  │  ACTION BAR                  │    │  bottom:0
│  │  shortcuts          [submit] │    │
│  └─────────────────────────────┘    │
│                                     │
│  [Success Overlay]        z:200     │
│  [Expired Overlay]        z:200     │
│  [Preview Modal]          z:250     │
│  [Error Banner]           z:50      │
└─────────────────────────────────────┘
```

---

## 3. Component Specifications

### 3.1 Timer Badge

- **Position**: `fixed; top: 20px; right: 24px;`
- **Shape**: Pill (`border-radius: 999px`)
- **States**:
  - `idle`: opacity 0.5, hover → 1.0
  - `active`: opacity 1.0
  - `warn` (>30s): amber tint
  - `urgent` (>15s): red tint
- **Interaction**: Click to reveal inline input for timeout adjustment

### 3.2 Hero Section

```
.kicker     → 11px uppercase, accent color, letter-spacing 0.1em
.title      → 40px italic serif, line-height 1.1
.desc       → 14px muted, max-width 480px
.meta       → flex row, status dot + provider buttons
```

### 3.3 Provider Buttons (Pills)

- **Shape**: Pill (`border-radius: 999px`)
- **Size**: `font-size: 12px; padding: 3px 10px;`
- **States**:
  - `idle`: transparent bg, muted border
  - `loading`: accent tint, pulsing `…` suffix
  - `searched`: filled bg, `✓` suffix in success green
  - `is-default`: bottom accent inset shadow + accent border
- **Interaction**: Click to switch default provider or trigger batch search

### 3.4 Result Card

```
┌────────────────────────────────────┐
│ [☑]  Query text          [Provider]│  ← header (clickable)
│      3 sources · domain.com        │
│      Preview snippet...            │
│                            ▼       │
├────────────────────────────────────┤
│ [Also try] [Brave] [Exa] [Jina]    │  ← alt providers
├────────────────────────────────────┤
│ ## Markdown answer                 │  ← body (collapsible)
│ - Bullet points                    │
├────────────────────────────────────┤
│ SOURCES                            │
│ link title · domain.com            │
└────────────────────────────────────┘
```

- **States**: `searching` (shimmer), `checked` (accent border), `error` (red border)
- **Checkbox**: Custom 16px square, accent fill when checked
- **Expand**: `▼` / `▲` toggle on header click

### 3.5 Provider Tags

Each provider has a unique color scheme (background 14% opacity + border 30% opacity):

| Provider | Color |
|----------|-------|
| OpenAI | `#a6e3a1` (green) |
| Brave | `#f38ba8` (pink) |
| Exa | `#8dd3ff` (blue) |
| Gemini | `#f5c27b` (amber) |
| Perplexity | `#cba6f7` (purple) |
| Jina | `#f9e2af` (yellow) |
| All | `#f5e0a6` (cream) |

### 3.6 Summary Panel

```
┌────────────────────────────────────┐
│ Review summary draft      [Model ▼]│  ← header
│ Edit before approving.             │
├────────────────────────────────────┤
│ ● Planning summary…                │  ← generating indicator
│ ████████░░                         │
├────────────────────────────────────┤
│ [Summary textarea.................]│
│ [Optional feedback for regen......]│
├────────────────────────────────────┤
│ [Back] [Regen] [Preview] [Approve] │
└────────────────────────────────────┘
```

- **Generating indicator**: Pulsing orb + 3 animated progress bars
- **Textarea**: `min-height: 180px`, resizable vertical
- **Updating state**: Top shimmer bar + reduced opacity on inputs

### 3.7 Action Bar

- **Position**: Fixed bottom, full width
- **Height**: ~56px + safe area
- **Left**: Keyboard shortcuts (`A` Toggle all, `Enter` Generate, `Esc` Cancel)
- **Right**: Primary submit button
- **Backdrop**: `blur(12px)` + semi-transparent bg

### 3.8 Loading Skeleton

- **Card**: 2 placeholder cards with 3 rows each
- **Animation**: Shimmer sweep (`translateX(-130%) → 130%`) over 2s
- **Rows**: `short` (35%), `mid` (58%), `long` (78%)

### 3.9 Overlays

| Overlay | Trigger | Content |
|---------|---------|---------|
| **Success** | Submit approved | OK icon + "Results sent" |
| **Expired** | Timer runs out | Warning icon + countdown |
| **Preview** | Preview button | Rendered markdown + popover quote feedback |

---

## 4. Interaction Patterns

### 4.1 Keyboard Shortcuts

| Key | Context | Action |
|-----|---------|--------|
| `A` | Results stage | Toggle all checkboxes |
| `Enter` | Results stage | Generate summary |
| `Ctrl/Cmd + Enter` | Summary stage | Approve |
| `Esc` | Any stage | Cancel / go back |

### 4.2 State Machine

```
results ──[select + Enter]──→ generating-summary ──[success]──→ summary-review
    ↑                              │                              │
    │                              │ [error]                      │ [Back]
    │                              ↓                              │
    └────────────────────── results (with error) ◄────────────────┘
                                                              │ [Approve]
                                                              ↓
                                                           submitted
```

### 4.3 Auto-Summary Flow

1. All searches complete (`searchesDone = true`)
2. If selection changed → `requestSummary(selected)`
3. Show generating indicator → fetch from `/summarize`
4. Populate textarea → enter `summary-review` stage
5. User can edit, regenerate with feedback, or approve

---

## 5. Responsive Behavior

| Breakpoint | Changes |
|------------|---------|
| `≤ 500px` | Hero title `28px`, hide shortcuts, reduce padding `16px`, stack summary controls |
| `prefers-reduced-motion` | Disable all animations (shimmer, pulse, sweep) |
| `prefers-color-scheme: light` | Full light mode color swap |

---

## 6. Assets

### 6.1 External Dependencies

```html
<!-- Fonts -->
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">

<!-- Markdown renderer -->
<script src="https://cdn.jsdelivr.net/npm/marked@15/marked.min.js"></script>
```

### 6.2 Iconography

- No icon font — uses Unicode symbols:
  - `✓` success, `✨` rewrite, `▼/▲` expand, `×` close, `+` add
  - `●` generating orb (CSS animated)

---

## 7. Accessibility

- All interactive elements have `:focus` states with accent ring
- `aria-live="polite"` on generating indicator and overlays
- `aria-label` on model dropdowns
- Checkbox is native `<input type="checkbox">` with custom styling
- `prefers-reduced-motion` disables animations
- Color contrast meets WCAG AA in both themes

---

## 8. Implementation Notes

1. **CSS Custom Properties**: All colors use CSS variables for instant theme switching
2. **Color Mix**: Extensive use of `color-mix(in srgb, ...)` for subtle tints
3. **Backdrop Filter**: Action bar uses `backdrop-filter: blur(12px)` — provide fallback
4. **Skeleton Loading**: Pure CSS shimmer via `::after` pseudo-element + `transform`
5. **Markdown Sanitization**: Required before injecting rendered HTML (strip scripts, sanitize hrefs)
6. **No Build Step**: Pure HTML/CSS/JS — suitable for embedding in any environment
