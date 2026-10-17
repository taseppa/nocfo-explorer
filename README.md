# nocfo-explorer

A [yazi](https://yazi-rs.github.io)-inspired terminal explorer for
[NoCFO](https://developers.nocfo.io) files and folders: three Miller columns,
one key per verb. Browse the folder tree, preview files, and run the common
file operations — rename, move, copy, delete, new folder — plus whole-business
search and multi-select, straight against the NoCFO REST API.

* **parent** — read-only listing of the parent folder, the current one marked.
* **current** — folders then files of the open folder; owns every key.
* **preview** — the hovered item: metadata, folder contents, or a text
  preview (capped at 256 kB / 500 lines), scrolled with `J`/`K`.

## Install

Python 3.11+; then either

```sh
pipx install git+git@github.com:taseppa/nocfo-explorer.git   # or pip install .
```

## Quick start

```sh
nocfo-explorer
```

With no configuration, the first-run wizard asks for a personal access token,
validates it, picks the business (auto-chosen when there is only one) and
writes `~/.config/nocfo-explorer/config.toml` for you. Or copy
`config.example.toml` into place:

```toml
[nocfo]
base_url = "https://api.nocfo.io"       # test env: https://api-tst.nocfo.io
token = "your-personal-access-token"
business_slug = "your-business-slug"
```

The config file is found in this order: `--config PATH` → the
`NOCFO_EXPLORER_CONFIG` environment variable → `./config.toml` →
`~/.config/nocfo-explorer/config.toml` → the repo root (for development
checkouts). `NOCFO_TOKEN` / `NOCFO_BUSINESS` override the file's values.

```sh
nocfo-explorer --config ~/work/prod.toml --business acme --version
```

## Keys

All interaction happens in the middle pane, like yazi; the cursor never wraps.

| key | action |
| --- | --- |
| `h` / `←` | parent folder |
| `l` / `→` / `Enter` | enter folder (files open in the browser) |
| `H` / `L` | back / forward in history |
| `j` / `k`, `ctrl+d` / `ctrl+u` | cursor / half-page |
| `gg` / `G` | top / bottom |
| `J` / `K` | scroll the preview |
| `o` | open the hovered file in the browser |
| `space` | mark / unmark (advances) |
| `ctrl+a` / `ctrl+r` | mark all / invert marks |
| `Esc` | clear marks, or leave search |
| `y` / `x` | yank (copy) / cut — marked entries, else the highlighted one |
| `Y` / `X` | forget the yank / cut |
| `p` / `P` | paste / force-paste (overwrite same-named files) |
| `d` | delete (permanent — confirmation dialog lists the targets) |
| `a` | new folder in the open folder |
| `r` | rename (cursor lands before the extension) |
| `/` | search file names across the whole business |
| `R` | refresh from the API |
| `q` | quit |

## What the operations really do

NoCFO's API has no trash bin and no transactional moves, so the explorer is
careful on your behalf:

* **Delete** is permanent. The dialog lists every target and flags what will
  be skipped or risky: non-empty folders (the API refuses to delete them) and
  files that are attached to bookings or marked not deletable.
* **Move** refuses to move a folder into itself or its own subtree.
* **Copy** (`y` + `p`) re-uploads file bytes, preserving MIME type and notes;
  folders are copied recursively. Name collisions become `report (copy).pdf`,
  `report (copy 2).pdf`, …; `P` overwrites instead — it uploads the new
  content first and only then deletes the old copy, refusing locked victims
  before touching anything.
* **Rename / new folder** reject empty names, slashes and duplicates up
  front, before any request is made.
* Multi-item operations keep going after a per-item failure and report the
  summary ("moved 2/3 — …"). A cut survives a failed paste so you can retry.

## Search

`/` searches **file names** (the API offers no folder search) across every
folder of the business. Results are real entries: mark, yank, cut, delete —
all work on them. `Enter`/`l` on a result jumps to the folder that holds it
(cursor on the file); `Esc` or `R` returns to the open folder.

## Development

```sh
git clone git@github.com:taseppa/nocfo-explorer.git
cd nocfo-explorer
uv sync          # or: python -m venv .venv && .venv/bin/pip install -e . pytest pytest-asyncio
.venv/bin/python -m pytest
```

The suite (unit + TUI) runs entirely against a fake in-memory client — no
network, no credentials. CI runs it on Python 3.11–3.14.

Layout: `src/nocfo_explorer/` — `nocfo.py` (API client), `config.py`,
`screens.py` (wizard + dialogs), `ops.py` (pure operations layer),
`app.py` (the TUI), `cli.py`.
