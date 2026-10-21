//! The embeddable pane: one [`App`] plus where its data comes from, drawn into any `Rect` of
//! any `Buffer`.
//!
//! The `tidetable-tui` binary is a thin terminal shell around a [`Pane`], and an embedder
//! (Oarlock's sidebar) draws the same widget into its own frame. The pane never owns a
//! terminal, never reads events and never sleeps: the host hands it keys and mouse events,
//! calls [`Pane::tick`] from its own loop, and repaints when `tick` says something changed.
//!
//! Data comes from a [`Source`] the host supplies. [`Pane::with_source`] calls it inline from
//! `tick`, which suits a shell with nothing else to do; [`Pane::with_background_source`] moves
//! it to a thread the pane owns, so a host's event loop never waits on the `tidetable` CLI.
//! Without a source the host feeds [`Pane::set_snapshot`] and [`Pane::set_unavailable`] itself.

use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Instant;

use crossterm::event::{KeyEvent, MouseEvent, MouseEventKind};
use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};

use crate::app::{run_pending, App, Load, ModeArg};
use crate::refresh::{Refresher, Watcher};
use crate::review::Ticks;
use crate::source::{Action, Snapshot, Source, SourceError};
use crate::theme::Theme;
use crate::ui;

/// What a key did inside the pane.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyOutcome {
    /// The pane used it.
    Consumed,
    /// The pane is not focused; the host keeps the key.
    Ignored,
    /// `q`, `Esc` or `Ctrl-C` outside a dialog: the standalone shell quits, an embedder takes
    /// focus back.
    Release,
}

/// A source the pane can own, inline or on its worker thread.
pub type BoxedSource = Box<dyn Source + Send>;

enum Req {
    Snapshot,
    Act(Action),
    Choose(PathBuf),
}

enum Reply {
    Snapshot(Box<Result<Snapshot, SourceError>>),
    Acted(Result<String, String>),
}

enum Feed {
    /// Data arrives through `set_snapshot` / `set_unavailable` only.
    None,
    Inline(BoxedSource),
    Worker {
        tx: Sender<Req>,
        rx: Receiver<Reply>,
        /// A snapshot request is out; never queue a second behind it.
        waiting: bool,
    },
}

/// The worker answers requests in order and ends when the pane drops its sender.
fn spawn_worker(mut source: BoxedSource) -> (Sender<Req>, Receiver<Reply>) {
    let (req_tx, req_rx) = mpsc::channel::<Req>();
    let (reply_tx, reply_rx) = mpsc::channel();
    std::thread::spawn(move || {
        for req in req_rx {
            let reply = match req {
                Req::Snapshot => Reply::Snapshot(Box::new(source.snapshot())),
                Req::Act(a) => Reply::Acted(source.act(&a)),
                Req::Choose(f) => {
                    source.choose(f);
                    continue;
                }
            };
            if reply_tx.send(reply).is_err() {
                return;
            }
        }
    });
    (req_tx, reply_rx)
}

fn now_epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

/// The TideTable view as a widget: state, data feed, focus and read-only flags.
pub struct Pane {
    app: App,
    feed: Feed,
    refresher: Refresher,
    watcher: Watcher,
    hub: bool,
    /// The first snapshot is asked for on the first `tick`, not on a timer.
    started: bool,
    /// Where the last `render` drew, for mouse routing.
    area: Option<Rect>,
}

impl std::fmt::Debug for Pane {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Pane")
            .field("load", &self.app.load)
            .field("focused", &self.app.focused)
            .field("read_only", &self.app.read_only)
            .field("hub", &self.hub)
            .field("area", &self.area)
            .finish_non_exhaustive()
    }
}

impl Pane {
    /// An unfocused pane with no source, showing "Loading…" until data arrives.
    pub fn new(theme: Theme, mode: ModeArg) -> Self {
        let mut app = App::new(theme, mode, Ticks::memory());
        app.focused = false;
        Self {
            app,
            feed: Feed::None,
            refresher: Refresher::new(false, Instant::now()),
            watcher: Watcher::default(),
            hub: false,
            started: false,
            area: None,
        }
    }

    /// Review ticks to read and write (the standalone shell loads the sidecar).
    pub fn with_ticks(mut self, ticks: Ticks) -> Self {
        self.app.ticks = ticks;
        self
    }

    /// Call `source` from [`Pane::tick`] on the host's thread.
    pub fn with_source(mut self, source: impl Source + Send + 'static) -> Self {
        self.feed = Feed::Inline(Box::new(source));
        self
    }

    /// Call `source` on a thread the pane owns; `tick` only sends requests and drains replies.
    pub fn with_background_source(mut self, source: impl Source + Send + 'static) -> Self {
        let (tx, rx) = spawn_worker(Box::new(source));
        self.feed = Feed::Worker {
            tx,
            rx,
            waiting: false,
        };
        self
    }

    /// Hub timing: faster polls, backoff on failure, and the last snapshot stays on screen
    /// (dimmed, labelled offline) when the hub drops out.
    pub fn set_hub(&mut self, hub: bool) {
        self.hub = hub;
        self.refresher = Refresher::new(hub, Instant::now());
    }

    /// A hub event stream connected (`true`) or dropped (`false`).
    pub fn set_stream(&mut self, live: bool) {
        self.refresher.set_stream(live);
    }

    /// Row pulses on status changes (see `motion.rs`); off by default.
    pub fn set_motion(&mut self, on: bool) {
        self.app.motion = on;
    }

    pub fn focused(&self) -> bool {
        self.app.focused
    }

    /// Only a focused pane takes keys and mouse events; it also draws its list border lit.
    pub fn set_focused(&mut self, focused: bool) {
        self.app.focused = focused;
        if !focused {
            self.app.dragging = false;
        }
    }

    pub fn read_only(&self) -> bool {
        self.app.read_only
    }

    /// No action keys and no action hints; ticks and navigation still work.
    pub fn set_read_only(&mut self, read_only: bool) {
        self.app.read_only = read_only;
    }

    /// Show `snap` as fresh data.
    pub fn set_snapshot(&mut self, snap: Snapshot) {
        self.app.now_epoch = now_epoch();
        self.app.set_snapshot(snap);
        self.app.last_ok = Some(self.app.now_epoch);
    }

    /// Replace the view with the one-line "could not read" notice.
    pub fn set_unavailable(&mut self, reason: impl Into<String>) {
        self.app.load = Load::Failed(reason.into());
    }

    pub fn load(&self) -> &Load {
        &self.app.load
    }

    pub fn app(&self) -> &App {
        &self.app
    }

    pub fn app_mut(&mut self) -> &mut App {
        &mut self.app
    }

    /// The rectangle the last `render` drew into.
    pub fn area(&self) -> Option<Rect> {
        self.area
    }

    /// True when the cell was inside the last drawn area.
    pub fn contains(&self, column: u16, row: u16) -> bool {
        self.area
            .is_some_and(|a| a.contains(Position::new(column, row)))
    }

    /// True while a pulse still owes frames; a host may wake faster until it clears.
    pub fn animating(&self) -> bool {
        self.app.fx.live()
    }

    /// Draw into `area` of `buf`, then run motion over what was drawn.
    pub fn render(&mut self, area: Rect, buf: &mut Buffer) {
        let area = area.intersection(buf.area);
        self.area = Some(area);
        ui::render(area, buf, &mut self.app);
        let rows = self.app.row_rects();
        let accent = self.app.theme.accent;
        self.app.fx.run(buf, area, &rows, accent, Instant::now());
    }

    /// One key press. An unfocused pane ignores it so the host's own keys keep working.
    pub fn handle_key(&mut self, k: KeyEvent) -> KeyOutcome {
        if !self.app.focused {
            return KeyOutcome::Ignored;
        }
        self.app.on_key(k);
        if std::mem::take(&mut self.app.quit) {
            KeyOutcome::Release
        } else {
            KeyOutcome::Consumed
        }
    }

    /// One mouse event; `true` when the pane used it. Only a focused pane takes events, and only
    /// inside its area, except that a divider drag follows the pointer until the button is up.
    pub fn handle_mouse(&mut self, m: MouseEvent) -> bool {
        if !self.app.focused {
            return false;
        }
        let drag =
            self.app.dragging && matches!(m.kind, MouseEventKind::Drag(_) | MouseEventKind::Up(_));
        if !drag && !self.contains(m.column, m.row) {
            return false;
        }
        self.app.on_mouse(m);
        true
    }

    /// Ask for a snapshot on the next `tick`.
    pub fn request_refresh(&mut self) {
        self.app.want_refresh = true;
    }

    /// Housekeeping from the host's loop: run a confirmed action, hand a picked file to the
    /// source, refresh when due (`pushed`: a stream or file-change notice arrived), and collect
    /// what the worker finished. Returns `true` when the pane needs a repaint.
    pub fn tick(&mut self, now: Instant, pushed: bool) -> bool {
        let mut dirty = self.drain();
        let mut pushed = pushed;
        if !self.hub && !self.app.snap.todo_file.is_empty() {
            if !self.watcher.is_for(&self.app.snap.todo_file) {
                self.watcher = Watcher::for_todo(&self.app.snap.todo_file);
            }
            pushed |= self.watcher.changed();
        }
        if let Some(action) = self.app.pending.take() {
            match &mut self.feed {
                Feed::Inline(source) => {
                    self.app.pending = Some(action);
                    run_pending(&mut self.app, source.as_mut());
                }
                Feed::Worker { tx, .. } => {
                    let _ = tx.send(Req::Act(action));
                }
                Feed::None => self.app.toast = Some(("no source to act through".into(), true)),
            }
            dirty = true;
        }
        if let Some(file) = self.app.chosen.take() {
            match &mut self.feed {
                Feed::Inline(source) => source.choose(file),
                Feed::Worker { tx, .. } => {
                    let _ = tx.send(Req::Choose(file));
                }
                Feed::None => {}
            }
            self.app.want_refresh = true;
        }
        let due = self.app.want_refresh || !self.started || self.refresher.due(now, pushed);
        match &mut self.feed {
            Feed::Inline(source) if due => {
                let result = source.snapshot();
                self.after_snapshot(result);
                dirty = true;
            }
            Feed::Worker { tx, waiting, .. } if due && !*waiting => {
                *waiting = tx.send(Req::Snapshot).is_ok();
                self.app.want_refresh = false;
                self.started = true;
            }
            _ => {}
        }
        let conn = self.refresher.conn(now);
        if conn != self.app.conn {
            self.app.conn = conn;
            self.app.now_epoch = now_epoch();
            dirty = true;
        }
        dirty || self.app.fx.live()
    }

    /// Fold in whatever the worker finished; `true` when anything arrived.
    fn drain(&mut self) -> bool {
        let Feed::Worker { rx, waiting, .. } = &mut self.feed else {
            return false;
        };
        let replies: Vec<Reply> = rx.try_iter().collect();
        if replies.iter().any(|r| matches!(r, Reply::Snapshot(_))) {
            *waiting = false;
        }
        let any = !replies.is_empty();
        for r in replies {
            match r {
                Reply::Snapshot(result) => self.after_snapshot(*result),
                Reply::Acted(Ok(msg)) => {
                    self.app.toast = Some((msg, false));
                    self.app.want_refresh = true;
                }
                Reply::Acted(Err(msg)) => self.app.toast = Some((msg, true)),
            }
        }
        any
    }

    fn after_snapshot(&mut self, result: Result<Snapshot, SourceError>) {
        self.app.want_refresh = false;
        self.started = true;
        self.app.now_epoch = now_epoch();
        let ok = match result {
            Ok(s) => {
                self.set_snapshot(s);
                true
            }
            Err(SourceError::Pick(files)) => {
                self.app.load = Load::Pick(files);
                true
            }
            // A hub that drops out keeps the last snapshot on screen; the footer says it is stale.
            Err(_) if self.hub && self.app.load == Load::Ready => false,
            Err(e) => {
                self.app.load = Load::Failed(e.to_string());
                false
            }
        };
        // Local reads never back off: a missing file or CLI is retried at the normal pace.
        self.refresher.record(ok || !self.hub, Instant::now());
    }
}
