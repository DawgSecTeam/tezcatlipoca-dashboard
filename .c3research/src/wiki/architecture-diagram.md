# Architecture Diagram

The system map in `README.md` is **generated, not hand-drawn**. Edit the
Mermaid source and re-render — never hand-edit an SVG, it will be overwritten
on the next render.

## Sources

| File | Role |
|---|---|
| `diagrams/mermaid/C3rberus_full.md` | Master map (red bottom → blue top). This is the one the README embeds. |
| `diagrams/mermaid/C3rberus_detailed.md` | Left-to-right variant. |
| `diagrams/mermaid/components/A_red_team_clients.md` … `G_blue_team_nets.md` | Per-tier breakdowns (A–G). |

## Rebuild

```bash
bash diagrams/mermaid/render.sh
```

Writes `diagrams/mermaid/render/<name>.svg` for every `.md` in the tree and its
`components/` subdirectory. README embeds `render/C3rberus_full.svg`. **Commit
the regenerated SVG together with the source change** — they drift silently
otherwise.

Toolchain: `mmdc` (`@mermaid-js/mermaid-cli`, installed globally) plus system
chromium via `diagrams/mermaid/.puppeteer.json`. Per file the pipeline is
mmdc render → `postprocess_svg.py` → rename (mmdc emits `<name>-1.svg`; the
rename gives the clean embed name). The ELK layout engine ships inside
mermaid-cli — no separate install.

## What postprocess_svg.py does

Two SVG-only passes, no effect on the Mermaid source:

1. Lifts subgraph titles above the edges, adds a white background, scales the
   title font 1.15×.
2. Injects OS icons as native `<path>` elements onto the `win` / `lin` / `pf`
   nodes from `diagrams/mermaid/os_icons.json`.

`os_icons.json` **mirrors `OS_PATHS` in
`react/src/components/OsIcon.tsx`** — if those icons change, re-extract it.

Both passes emit native SVG so they survive the README's `<img>` embed. A
GitHub-native ` ```mermaid ` block renders neither the lifted titles nor the
icons, which is why the README embeds the rendered SVG instead.

## Conventions when editing C3rberus_full.md

- The init directive sets `layout: elk`, `nodePlacementStrategy: SIMPLE`
  (centers all 5 tier boxes on one vertical axis — every other strategy offsets
  blue against red), `mergeEdges: false` (separate edge origination ports, no
  stacking), and `wrappingWidth: 1000` (lets a node box widen to its longest
  `<br/>` line instead of wrapping).
- Tier color = a `classDef` per tier plus a matching `style <SUBGRAPH>`.
  Protocol color = `linkStyle <idx>`.
- **`linkStyle` indices are positional.** Adding, removing, or reordering any
  edge shifts every later index. After an edge change, recount edge order
  top-to-bottom through the file (0-based) and fix every `linkStyle` line.
- C2-egress → blue fan-out is one edge per (source, box) with the protocol on
  the label; ELK routes them. Keep the protocol colors consistent: red = mTLS,
  green = NXC SMB/SSH, orange = HMAC, pink = impacket, teal = pivot,
  blue = web, purple = gRPC.

## When to update it

Any change that adds a route, subsystem, or protocol surface belonging on the
map: edit `C3rberus_full.md`, re-render, eyeball the SVG, and commit source +
regenerated SVG together.

<!-- audited 2026-07-30 — page created from CLAUDE.md § Architecture diagram. render.sh pipeline (mmdc -> postprocess_svg.py -> rename, *.md + components/*.md), .puppeteer.json path, os_icons.json, and the components/A-G file names verified on disk; README embed path verified at README.md:9. -->
