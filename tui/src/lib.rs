//! TideTable TUI: data layer first, chrome later.
//!
//! The crate never writes a todo file. AGENTS.md makes the CLI (or the hub) the
//! single writer, so every mutation goes through a `Source`, never `File::create`.

pub mod app;
pub mod hub;
pub mod motion;
pub mod refresh;
pub mod review;
pub mod slug;
pub mod source;
pub mod theme;
pub mod ui;

pub use source::{
    build_snapshot, parse_utc, phase_of, resolve_todo, Action, Blocked, CliRunner, Detail,
    LocalSource, LookItem, PhaseProgress, Runner, Running, Snapshot, Source, SourceError, TaskRef,
    TODO_SEARCH_DIRS,
};
