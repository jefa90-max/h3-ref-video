# H3 Reference Video Workbench Design System

## Direction and Purpose

The local workbench is a calm, offline-capable studio desk for reviewing storyboards, character references, and video segments before spending cloud GPU credits. Surfaces stay quiet (`#f3f5f2` canvas, `#ffffff` cards, `#edf5ef` wash) so generated frames, reference portraits, and video clips hold visual priority.

## Color Tokens

| Token | Value | Role |
|---|---|---|
| `--ink` | `#203831` | Primary body text, headings, active labels |
| `--muted` | `#52655a` | Secondary metadata, helper copy, inactive tabs |
| `--accent` | `#237b60` | Primary actions, focus rings, active tab indicators, links |
| `--border` | `#e2e7e1` | Card, toolbar, and section dividers |
| `--surface` | `#ffffff` | Primary panel and control background |
| `--wash` | `#edf5ef` | Subtle preview backdrops and hover wash |
| Warning | `#81612a` on `#fff8e8` | Live-billing cautions, dirty-frame notices, unapplied drafts |
| Error | `#9b4528` on `#fff0e9` | Validation, moderation, and terminal task errors |
| Log terminal | `#d0dfcd` on `#263c30` | Monospace worker log stream |

## Typography

- **Primary UI stack**: `-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif`
  - Deliberately uses native OS CJK + Latin system fonts (`PingFang SC` on macOS, `Microsoft YaHei` + `Segoe UI` on Windows) with zero external font network requests so packaged offline Windows and macOS builds render Chinese (`zh-CN`) and English (`en`) copy immediately under `default-src 'self'` CSP.
- **Monospace stack**: `"SFMono-Regular", Consolas, "Liberation Mono", monospace` (`--mono`)
  - Used for `tabular`/technical counters, step badges, file paths, JSON script editing, and worker logs.
- **Measure**: Full-width panel helper prose (`.hint` paragraphs) is capped at `68ch` so wide desktop viewports remain scannable.

## Layout and Interaction Rules

- **Shell**: `max-width: 1380px` centered workspace; responsive breakpoints at `1060px`, `760px`, and `420px` collapse multi-column grids to single-column stacks without horizontal overflow.
- **Interactive states**: Every clickable button, tab, history row, artifact link, and back link exposes distinct `:hover`, `:focus-visible` (`2px solid var(--accent)`), and `:disabled` states, and honors `prefers-reduced-motion: reduce`.
- **Paid-action clarity**: Live cloud actions use explicit billing labels and amber live-mode framing; ambiguous or terminal cloud tasks lock automatic retry and explain manual recovery in place.
