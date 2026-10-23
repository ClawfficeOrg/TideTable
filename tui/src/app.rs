//! App state, the four views as plain data, and input handling. No drawing here.

use std::collections::HashMap;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;

use tui_input::backend::crossterm::EventHandler;

use crate::motion::{self, Motion};
use crate::refresh::Conn;
use crate::review::Ticks;
use crate::source::{phase_of, Action, Snapshot, Source};
use crate::theme::{Status, Theme};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Tab {
    Now,
    Upcoming,
    Review,
    Done,
}

impl Tab {
    pub const ALL: [Tab; 4] = [Tab::Now, Tab::Upcoming, Tab::Review, Tab::Done];

    pub fn title(self) -> &'static str {
        match self {
            Tab::Now => "Now",
            Tab::Upcoming => "Upcoming",
            Tab::Review => "Review",
            Tab::Done => "Done",
        }
    }

    pub fn index(self) -> usize {
        Self::ALL.iter().position(|t| *t == self).unwrap_or(0)
    }

    fn shift(self, by: isize) -> Tab {
        let n = Self::ALL.len() as isize;
        Self::ALL[(self.index() as isize + by).rem_euclid(n) as usize]
    }
}

/// What `--mode` asked for; `Auto` re-evaluates on every resize.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModeArg {
    Auto,
    Standard,
    Side,
    Bottom,
}

impl ModeArg {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "auto" => Some(Self::Auto),
            "standard" => Some(Self::Standard),
            "side" => Some(Self::Side),
            "bottom" => Some(Self::Bottom),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Standard,
    Side,
    Bottom,
}

impl Mode {
    /// Tall narrow pane -> side; short wide pane -> bottom; otherwise standard.
    pub fn auto(width: u16, height: u16) -> Mode {
        if width < 60 && height > width / 2 {
            Mode::Side
        } else if height <= 14 && width >= 80 {
            Mode::Bottom
        } else {
            Mode::Standard
        }
    }

    pub fn resolve(arg: ModeArg, width: u16, height: u16) -> Mode {
        match arg {
            ModeArg::Auto => Mode::auto(width, height),
            ModeArg::Standard => Mode::Standard,
            ModeArg::Side => Mode::Side,
            ModeArg::Bottom => Mode::Bottom,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Kind {
    /// A phase heading; never selectable.
    Header,
    Task,
    /// One success criterion of a Review item, with its tick.
    Check {
        task: String,
        idx: usize,
        total: usize,
        on: bool,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub kind: Kind,
    pub id: String,
    pub title: String,
    pub status: Option<Status>,
    /// Dim continuation lines under the row.
    pub sub: Vec<String>,
    /// Highlighted continuation (e.g. "ready to clear").
    pub note: Option<(Status, String)>,
}

impl Entry {
    fn header(title: String) -> Self {
        Self {
            kind: Kind::Header,
            id: String::new(),
            title,
            status: None,
            sub: vec![],
            note: None,
        }
    }
    fn task(id: &str, title: &str, status: Status, sub: Vec<String>) -> Self {
        Self {
            kind: Kind::Task,
            id: id.into(),
            title: title.into(),
            status: Some(status),
            sub,
            note: None,
        }
    }
    pub fn selectable(&self) -> bool {
        self.kind != Kind::Header
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Hit {
    Tab(Tab),
    /// Index into the selectable entries of the current tab.
    Row(usize),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Load {
    Loading,
    Ready,
    /// Several todo files matched; the user picks one (never a prompt on stdin).
    Pick(Vec<std::path::PathBuf>),
    /// Hub mode has no slug; the user picks a project (slug, name).
    PickProjects(Vec<(String, String)>),
    Failed(String),
}

pub struct App {
    pub snap: Snapshot,
    pub load: Load,
    pub theme: Theme,
    pub mode_arg: ModeArg,
    pub tab: Tab,
    /// Selection per tab as an index into the selectable entries, so a mode change keeps it.
    pub sel: HashMap<Tab, usize>,
    /// First visible line per tab.
    pub scroll: HashMap<Tab, usize>,
    pub ticks: Ticks,
    /// Hit rectangles stored by the last render, for mouse hit-testing.
    pub hits: Vec<(Rect, Hit)>,
    pub now_epoch: i64,
    pub quit: bool,
    pub want_refresh: bool,
    /// `--read-only`: no action keys, no action hints.
    pub read_only: bool,
    /// Connection state shown in the footer; `Local` shows nothing.
    pub conn: Conn,
    /// Motion on/off (see motion.rs); off by default so tests and NO_COLOR stay still.
    pub motion: bool,
    pub fx: Motion,
    /// List share of the list/detail split, percent.
    pub split: u16,
    pub divider: Option<Divider>,
    pub dragging: bool,
    /// Epoch seconds of the last successful snapshot, for the offline label.
    pub last_ok: Option<i64>,
    pub dialog: Option<Dialog>,
    /// Confirmed by the user; [`run_pending`] executes it through the Source.
    pub pending: Option<Action>,
    /// Last action result; `true` marks a refusal. Cleared by the next key.
    pub toast: Option<(String, bool)>,
    /// A todo file the user chose in the picker; main hands it to the source.
    pub chosen: Option<std::path::PathBuf>,
    /// A hub project slug chosen in the picker; main rebuilds the hub source.
    pub chosen_slug: Option<String>,
    /// Cursor in the hub project picker.
    pub pick_sel: usize,
}

/// The draggable boundary between list and detail, as drawn last frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Divider {
    /// Cells that grab it (the border pair either side of the gap).
    pub grab: Rect,
    /// The area the two panes share; the drag position is a share of this.
    pub body: Rect,
    pub vertical: bool,
}

pub const SPLIT_MIN: u16 = 20;
pub const SPLIT_MAX: u16 = 80;

/// A confirm box for one [`Action`]; `input` is the optional changelog / note line.
#[derive(Debug, Clone)]
pub struct Dialog {
    pub action: Action,
    pub title: String,
    pub body: String,
    pub input: Option<(String, tui_input::Input)>,
}

/// An agent note that only points at the criteria is noise once the criteria are listed
/// (same rule as `look-queue -v`).
pub fn is_pointer(note: &str) -> bool {
    note.len() < 80 && note.to_lowercase().contains("success criteria")
}

impl App {
    pub fn new(theme: Theme, mode_arg: ModeArg, ticks: Ticks) -> Self {
        Self {
            snap: Snapshot::default(),
            load: Load::Loading,
            theme,
            mode_arg,
            tab: Tab::Now,
            sel: HashMap::new(),
            scroll: HashMap::new(),
            ticks,
            hits: vec![],
            now_epoch: 0,
            quit: false,
            want_refresh: false,
            read_only: false,
            conn: Conn::Local,
            motion: false,
            fx: Motion::default(),
            split: 55,
            divider: None,
            dragging: false,
            last_ok: None,
            dialog: None,
            pending: None,
            toast: None,
            chosen: None,
            chosen_slug: None,
            pick_sel: 0,
        }
    }

    pub fn set_snapshot(&mut self, snap: Snapshot) {
        if self.motion && self.load == Load::Ready {
            let before = motion::lanes(&self.snap);
            self.fx.queue(motion::changed(&before, &snap));
        }
        self.snap = snap;
        self.load = Load::Ready;
        for t in Tab::ALL {
            self.clamp(t);
        }
    }

    /// Screen rect of each visible task row with its id, from the last render.
    pub fn row_rects(&self) -> Vec<(Rect, String)> {
        let ids: Vec<String> = self
            .entries(self.tab)
            .into_iter()
            .filter(Entry::selectable)
            .map(|e| e.id)
            .collect();
        self.hits
            .iter()
            .filter_map(|(r, h)| match h {
                Hit::Row(i) => ids.get(*i).map(|id| (*r, id.clone())),
                Hit::Tab(_) => None,
            })
            .collect()
    }

    pub fn review_count(&self) -> usize {
        self.snap.look_queue.len()
    }

    pub fn entries(&self, tab: Tab) -> Vec<Entry> {
        let s = &self.snap;
        match tab {
            Tab::Now => s
                .in_progress
                .iter()
                .map(|r| {
                    let status = if r.lease_expired {
                        Status::StaleLease
                    } else {
                        Status::InProgress
                    };
                    let age = r.age_secs(self.now_epoch).map_or("?".into(), fmt_age);
                    let owner = r.owner.as_deref().unwrap_or("unclaimed");
                    let lease = match r.lease_left_secs(self.now_epoch) {
                        Some(left) if left > 0 => format!("lease {} left", fmt_age(left)),
                        Some(_) => "lease expired".to_string(),
                        None if r.lease_expired => "lease expired".to_string(),
                        None => "lease ok".to_string(),
                    };
                    let mut sub = vec![format!("{owner} · {age} · {lease}")];
                    if let Some(d) = s.details.get(&r.id).filter(|d| !d.goal.is_empty()) {
                        sub.push(d.goal.clone());
                    }
                    Entry::task(&r.id, &r.title, status, sub)
                })
                .collect(),
            Tab::Upcoming => {
                let mut out = vec![];
                for p in &s.phases {
                    let ready: Vec<_> =
                        s.ready.iter().filter(|t| phase_of(&t.id) == p.id).collect();
                    let blocked: Vec<_> = s
                        .blocked
                        .iter()
                        .filter(|t| phase_of(&t.id) == p.id)
                        .collect();
                    if ready.is_empty() && blocked.is_empty() {
                        continue;
                    }
                    out.push(Entry::header(format!("Phase {} · {}", p.id, p.title)));
                    for t in ready {
                        let sub = if s.disjoint.contains(&t.id) {
                            vec!["∥ safe to run in parallel".into()]
                        } else {
                            vec![]
                        };
                        out.push(Entry::task(&t.id, &t.title, Status::Pending, sub));
                    }
                    for t in blocked {
                        let sub = vec![format!("→ waits on {}", t.blocked_by.join(", "))];
                        out.push(Entry::task(&t.id, &t.title, Status::Blocked, sub));
                    }
                }
                out
            }
            Tab::Review => {
                let mut out = vec![];
                for q in &s.look_queue {
                    let total = q.success.len();
                    let sub = if q.note.is_empty() || is_pointer(&q.note) {
                        vec![]
                    } else {
                        vec![format!("check: {}", q.note)]
                    };
                    let mut item = Entry::task(&q.id, &q.title, Status::NeedsLook, sub);
                    if self.ticks.all(&s.project, &q.id, total) {
                        item.note = Some((Status::Done, "all checked, ready to clear".into()));
                    }
                    out.push(item);
                    for (idx, text) in q.success.iter().enumerate() {
                        let on = self.ticks.get(&s.project, &q.id, idx);
                        out.push(Entry {
                            kind: Kind::Check {
                                task: q.id.clone(),
                                idx,
                                total,
                                on,
                            },
                            id: q.id.clone(),
                            title: text.clone(),
                            status: None,
                            sub: vec![],
                            note: None,
                        });
                    }
                }
                out
            }
            Tab::Done => s
                .done
                .iter()
                .map(|t| {
                    let sub = s
                        .changelog
                        .get(&t.id)
                        .map(|c| vec![c.clone()])
                        .unwrap_or_default();
                    Entry::task(&t.id, &t.title, Status::Done, sub)
                })
                .collect(),
        }
    }

    pub fn selectable_count(&self, tab: Tab) -> usize {
        self.entries(tab).iter().filter(|e| e.selectable()).count()
    }

    pub fn selected_index(&self, tab: Tab) -> usize {
        self.sel.get(&tab).copied().unwrap_or(0)
    }

    pub fn selected_entry(&self) -> Option<Entry> {
        self.entries(self.tab)
            .into_iter()
            .filter(Entry::selectable)
            .nth(self.selected_index(self.tab))
    }

    fn clamp(&mut self, tab: Tab) {
        let n = self.selectable_count(tab);
        let cur = self.selected_index(tab);
        self.sel
            .insert(tab, if n == 0 { 0 } else { cur.min(n - 1) });
    }

    pub fn move_sel(&mut self, by: isize) {
        let n = self.selectable_count(self.tab) as isize;
        if n == 0 {
            return;
        }
        let cur = self.selected_index(self.tab) as isize;
        self.sel
            .insert(self.tab, (cur + by).clamp(0, n - 1) as usize);
    }

    pub fn set_sel(&mut self, idx: usize) {
        self.sel.insert(self.tab, idx);
        self.clamp(self.tab);
    }

    pub fn toggle_tick(&mut self) {
        if let Some(Entry {
            kind: Kind::Check {
                task, idx, total, ..
            },
            ..
        }) = self.selected_entry()
        {
            self.ticks
                .toggle(&self.snap.project.clone(), &task, idx, total);
        }
    }

    pub fn hints(&self) -> String {
        let mut h = String::from("j/k move · tab/1-4 view · space tick · r refresh");
        if self.read_only {
            h.push_str(" · read-only");
        } else {
            match self.tab {
                Tab::Now => h.push_str(" · d done · l defer"),
                Tab::Upcoming => h.push_str(" · s start"),
                Tab::Review => h.push_str(" · c clear · x reject"),
                _ => {}
            }
        }
        if self.divider.is_some() {
            h.push_str(" · [/] resize");
        }
        h.push_str(" · q quit");
        h
    }

    pub fn nudge_split(&mut self, by: i16) {
        self.split = (self.split as i16 + by).clamp(SPLIT_MIN as i16, SPLIT_MAX as i16) as u16;
    }

    fn drag_to(&mut self, col: u16, row: u16) {
        let Some(d) = self.divider else { return };
        let (pos, origin, len) = if d.vertical {
            (row, d.body.y, d.body.height)
        } else {
            (col, d.body.x, d.body.width)
        };
        if len == 0 {
            return;
        }
        let pct = (pos.saturating_sub(origin) as u32 * 100 / len as u32) as u16;
        self.split = pct.clamp(SPLIT_MIN, SPLIT_MAX);
    }

    fn begin(&mut self, verb: char) {
        let Some(e) = self.selected_entry() else {
            return;
        };
        let (want, at) = match verb {
            'd' | 'l' => (Tab::Now, "Now"),
            's' => (Tab::Upcoming, "Upcoming"),
            _ => (Tab::Review, "Review"),
        };
        if self.tab != want {
            self.toast = Some((format!("{verb}: select a task in {at} first"), true));
            return;
        }
        let (action, title, body, input) = match verb {
            'd' => (
                Action::Done {
                    id: e.id.clone(),
                    changelog: None,
                },
                "Mark done",
                "Finish this task?",
                Some("Changelog (optional)"),
            ),
            'l' => (
                Action::Defer {
                    id: e.id.clone(),
                    note: String::new(),
                },
                "Defer to human look",
                "Park it in the review queue?",
                Some("What should be checked"),
            ),
            's' => (
                Action::Start { id: e.id.clone() },
                "Start",
                "Claim this task?",
                None,
            ),
            'c' => (
                Action::Clear { id: e.id.clone() },
                "Clear",
                "A human looked: mark it done?",
                None,
            ),
            _ => (
                Action::Reject { id: e.id.clone() },
                "Reject",
                "The check failed: send it back to pending?",
                None,
            ),
        };
        self.dialog = Some(Dialog {
            action,
            title: format!("{title} {}", e.id),
            body: format!("{body}\n{}", e.title),
            input: input.map(|l| (l.to_string(), tui_input::Input::default())),
        });
    }

    fn on_dialog_key(&mut self, k: KeyEvent) {
        let Some(d) = self.dialog.as_mut() else {
            return;
        };
        match k.code {
            KeyCode::Esc => self.dialog = None,
            KeyCode::Enter => {
                let text = d
                    .input
                    .as_ref()
                    .map(|(_, v)| v.value().trim().to_string())
                    .unwrap_or_default();
                let action = match d.action.clone() {
                    Action::Done { id, .. } => Action::Done {
                        id,
                        changelog: (!text.is_empty()).then_some(text),
                    },
                    Action::Defer { id, .. } => Action::Defer { id, note: text },
                    other => other,
                };
                self.pending = Some(action);
                self.dialog = None;
            }
            _ => {
                if let Some((_, v)) = d.input.as_mut() {
                    v.handle_event(&crossterm::event::Event::Key(k));
                }
            }
        }
    }

    pub fn on_key(&mut self, k: KeyEvent) {
        if self.dialog.is_some() {
            self.on_dialog_key(k);
            return;
        }
        self.toast = None;
        if let Load::PickProjects(projects) = &self.load {
            let last = projects.len().saturating_sub(1);
            match k.code {
                KeyCode::Char('j') | KeyCode::Down => self.pick_sel = (self.pick_sel + 1).min(last),
                KeyCode::Char('k') | KeyCode::Up => self.pick_sel = self.pick_sel.saturating_sub(1),
                KeyCode::Enter => {
                    if let Some((slug, _)) = projects.get(self.pick_sel) {
                        self.chosen_slug = Some(slug.clone());
                    }
                }
                KeyCode::Char(c @ ('1'..='9')) => {
                    if let Some((slug, _)) = projects.get(c as usize - '1' as usize) {
                        self.chosen_slug = Some(slug.clone());
                    }
                }
                KeyCode::Char('q') | KeyCode::Esc => self.quit = true,
                _ => {}
            }
            return;
        }
        if let (Load::Pick(files), KeyCode::Char(c @ '1'..='9')) = (&self.load, k.code) {
            self.chosen = files.get(c as usize - '1' as usize).cloned();
            return;
        }
        match (k.code, k.modifiers) {
            (KeyCode::Char('q'), _)
            | (KeyCode::Esc, _)
            | (KeyCode::Char('c'), KeyModifiers::CONTROL) => self.quit = true,
            (KeyCode::Char('j'), _) | (KeyCode::Down, _) => self.move_sel(1),
            (KeyCode::Char('k'), _) | (KeyCode::Up, _) => self.move_sel(-1),
            (KeyCode::Char('g'), _) | (KeyCode::Home, _) => self.set_sel(0),
            (KeyCode::Char('G'), _) | (KeyCode::End, _) => self.set_sel(usize::MAX),
            (KeyCode::Char(c @ ('d' | 'l' | 's' | 'c' | 'x')), KeyModifiers::NONE)
                if !self.read_only =>
            {
                self.begin(c)
            }
            (KeyCode::Char('['), _) => self.nudge_split(-5),
            (KeyCode::Char(']'), _) => self.nudge_split(5),
            (KeyCode::Tab, _) | (KeyCode::Right, _) => self.tab = self.tab.shift(1),
            (KeyCode::BackTab, _) | (KeyCode::Left, _) | (KeyCode::Char('h'), _) => {
                self.tab = self.tab.shift(-1)
            }
            (KeyCode::Char(' '), _) => self.toggle_tick(),
            (KeyCode::Char('r'), _) => self.want_refresh = true,
            (KeyCode::Char(c @ '1'..='4'), _) => self.tab = Tab::ALL[c as usize - '1' as usize],
            _ => {}
        }
    }

    pub fn on_mouse(&mut self, m: MouseEvent) {
        match m.kind {
            MouseEventKind::ScrollDown => self.move_sel(1),
            MouseEventKind::ScrollUp => self.move_sel(-1),
            MouseEventKind::Down(MouseButton::Left) => {
                if self.dialog.is_none()
                    && self.divider.is_some_and(|d| {
                        let g = d.grab;
                        m.column >= g.x
                            && m.column < g.x + g.width
                            && m.row >= g.y
                            && m.row < g.y + g.height
                    })
                {
                    self.dragging = true;
                    return;
                }
                let hit = self
                    .hits
                    .iter()
                    .find(|(r, _)| {
                        m.column >= r.x
                            && m.column < r.x + r.width
                            && m.row >= r.y
                            && m.row < r.y + r.height
                    })
                    .map(|(_, h)| *h);
                match hit {
                    Some(Hit::Tab(t)) => self.tab = t,
                    Some(Hit::Row(i)) => self.set_sel(i),
                    None => {}
                }
            }
            MouseEventKind::Drag(MouseButton::Left) if self.dragging => {
                self.drag_to(m.column, m.row)
            }
            MouseEventKind::Up(MouseButton::Left) => self.dragging = false,
            _ => {}
        }
    }
}

/// Carry out the confirmed action through the source. A refusal is shown as the CLI worded it
/// and leaves the snapshot untouched; success asks for a refresh.
pub fn run_pending(app: &mut App, source: &mut impl Source) {
    let Some(action) = app.pending.take() else {
        return;
    };
    match source.act(&action) {
        Ok(msg) => {
            app.toast = Some((msg, false));
            app.want_refresh = true;
        }
        Err(msg) => app.toast = Some((msg, true)),
    }
}

impl App {
    /// Footer label for the connection: icon + word, never colour alone.
    pub fn conn_label(&self) -> Option<(Status, String)> {
        match self.conn {
            Conn::Local => None,
            Conn::Live => Some((Status::Done, "live".into())),
            Conn::Polling => Some((Status::Pending, "polling".into())),
            Conn::Offline { retry_in } => {
                let age = self.last_ok.map_or(String::new(), |t| {
                    format!(" · data {} old", fmt_age((self.now_epoch - t).max(0)))
                });
                Some((Status::Blocked, format!("offline · retry {retry_in}s{age}")))
            }
            Conn::Denied { forbidden: false } => Some((
                Status::Blocked,
                "token rejected · set $TIDETABLE_TOKEN".into(),
            )),
            Conn::Denied { forbidden: true } => Some((
                Status::Blocked,
                "no access to this project · ask for a scoped token".into(),
            )),
        }
    }

    pub fn is_offline(&self) -> bool {
        matches!(self.conn, Conn::Offline { .. })
    }

    pub fn is_denied(&self) -> bool {
        matches!(self.conn, Conn::Denied { .. })
    }
}

pub fn fmt_age(secs: i64) -> String {
    match secs {
        s if s < 60 => format!("{s}s"),
        s if s < 3600 => format!("{}m", s / 60),
        s if s < 86400 => format!("{}h{:02}m", s / 3600, s % 3600 / 60),
        s => format!("{}d", s / 86400),
    }
}
