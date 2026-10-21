# tidetable-tui

The ratatui front end for TideTable todo files and hubs. One crate, two faces:

| Face | Where | What it owns |
| --- | --- | --- |
| `tidetable_tui::Pane` | `src/pane.rs` (library) | View state, the data feed, focus and read-only flags, key and mouse handling, drawing into any `Rect` of any `Buffer`. |
| `tidetable-tui` | `src/main.rs` (binary) | The terminal: argument parsing, raw mode, the event loop, the hub event stream. Nothing else. |

Both front ends — this binary and an embedder such as Oarlock's sidebar — draw the
same `Pane`. Neither scrapes the other's screen and neither schedules work: task
state still changes only through the `tidetable` CLI or a hub (see `AGENTS.md`).

## Features

| Feature | Default | Pulls in |
| --- | --- | --- |
| `hub` | on | `ureq`, `hub::UreqHttp`, `hub::spawn_stream` and the binary's `--server` mode |

`hub::HubSource` and its `Http` trait are plain logic and stay available without
the feature. An embedder that only reads local todo files turns it off:

```toml
tidetable-tui = { path = "…/TideTable/tui", version = "=0.12.0", default-features = false }
```

Built without `hub`, the binary still runs locally and refuses `--server`.

## Embedding a pane

```rust
use tidetable_tui::{CliRunner, KeyOutcome, LocalSource, Pane};
use tidetable_tui::app::ModeArg;
use tidetable_tui::theme::Theme;

let (theme, _) = Theme::select(None, |k| std::env::var(k).ok());
let source = LocalSource::new(CliRunner::default(), cwd);
let mut pane = Pane::new(theme, ModeArg::Auto)
    .with_background_source(source); // the CLI runs on the pane's own thread
pane.set_read_only(true);

// In the host's loop:
if pane.tick(Instant::now(), false) { /* repaint */ }
pane.render(area, frame.buffer_mut());          // any Rect, buffer coordinates
if pane.focused() {
    match pane.handle_key(key) {                // Ignored while unfocused
        KeyOutcome::Release => pane.set_focused(false),
        _ => {}
    }
    pane.handle_mouse(mouse);                   // only inside the last drawn area
}
```

- `with_source` calls the source inline from `tick` (the binary does this);
  `with_background_source` calls it on a worker thread so a host's loop never
  waits on the CLI. Without either, the host calls `set_snapshot` and
  `set_unavailable` itself.
- A pane that cannot read its todo file or source draws a single
  `⊘ Could not read the todo file · <reason>` line in every layout mode.
- A new pane is unfocused; it ignores keys and mouse until `set_focused(true)`.
  `q`, `Esc` and `Ctrl-C` outside a dialog come back as `KeyOutcome::Release`:
  the binary quits, an embedder takes focus back.
- The layout mode follows the area (`ModeArg::Auto`): a tall narrow sidebar gets
  the side layout, a short wide strip the bottom one.

```bash
cargo test                          # default features
cargo test --no-default-features    # the embedder's build, no ureq
cargo clippy --all-targets -- -D warnings
```
