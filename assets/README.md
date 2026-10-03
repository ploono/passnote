# passnote brand

Everyone remembers passing a note in class: folded small, handed across, read without anyone being
interrupted. That is what passnote does between Claude Code sessions. The mark is two folded notes
making a closing quotation mark (”): something said quietly, between two peers. It closes the
wordmark, so mark and name read as one.

## Files

| File | Use |
|---|---|
| `logo.svg`, `logo-dark.svg` | Lockup for light and dark backgrounds, with its clear space built in |
| `logo-tight.svg`, `logo-tight-dark.svg` | The same lockups cropped to the drawing, for layouts that set their own spacing (the README header) |
| `logo-mono.svg`, `logo-mono-white.svg` | One-colour lockups |
| `symbol.svg`, `symbol-dark.svg`, `symbol-mono.svg`, `symbol-mono-white.svg` | The mark alone, 32 px and up |
| `symbol-16.svg`, `symbol-16-dark.svg` | The mark at 24 px and below: wider gaps so the fold survives |
| `favicon.svg` | Small mark that follows `prefers-color-scheme` |
| `app-icon-paper.svg`, `app-icon-ink.svg`, `app-icon-red.svg` | App icons on a squircle tile |
| `sly.svg`, `sly-dark.svg` | Sly, the companion character, for docs, empty states and stickers. Never inside the logo |
| `social-preview.svg`, `social-preview.png` | 1280×640 GitHub social card |

The wordmark is outlined, so no font is needed to show it. Leave clear space around the lockup
as drawn in its viewBox, and don't recolour, stretch or add effects to it.

## Colour

| Name | Hex | Role |
|---|---|---|
| Paper | `#f5f1e8` | Background: notebook stock, warm rather than white |
| Ink | `#1b1714` | Type and marks: a warm near-black |
| Margin | `#e8344e` | Signature: the red margin line of a school notebook; what needs attention |
| Highlighter | `#ffe14d` | Only for the doorbell, the one thing that matters now |
| Ruled | `#3d5bd9` | Links and data |
| Graphite | `#6e6862` | Secondary text and events |

`passnote watch` uses this palette when the terminal sets `COLORTERM=truecolor` (or `24bit`): asks,
naks and errors in Margin, props, claims and status in Ruled, events in Graphite, and a rung doorbell
in Ink on Highlighter.

## Type

Fraunces (600, opsz 72, SOFT 50) for display and the wordmark, Instrument Sans for text, Geist Mono
for code. All three are under the SIL Open Font License.
