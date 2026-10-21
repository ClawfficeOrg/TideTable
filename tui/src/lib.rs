//! TideTable TUI: data layer first, chrome later.
//!
//! The crate never writes a todo file. AGENTS.md makes the CLI (or the hub) the
//! single writer, so every mutation goes through a `Source`, never `File::create`.
//!
//! Library and binary: [`Pane`] is the widget (state, data feed, input, drawing into any
//! `Rect` of any `Buffer`); `src/main.rs` is a thin terminal shell around one. An embedder
//! depends on this crate with `default-features = false`, which drops the `hub` feature and
//! with it `ureq`, `UreqHttp` and `hub::spawn_stream`. See `README.md` in this directory.

pub mod app;
pub mod hub;
pub mod motion;
pub mod pane;
pub mod refresh;
pub mod review;
pub mod source;
pub mod theme;
pub mod ui;

pub use pane::{KeyOutcome, Pane};
pub use source::{
    build_snapshot, parse_utc, phase_of, resolve_todo, Action, Blocked, CliRunner, Detail,
    LocalSource, LookItem, PhaseProgress, Runner, Running, Snapshot, Source, SourceError, TaskRef,
    TODO_SEARCH_DIRS,
};
