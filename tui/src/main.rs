use std::path::PathBuf;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyEventKind};
use tidetable_tui::app::{run_pending, App, Load, ModeArg};
use tidetable_tui::hub::{spawn_stream, HubSource, StreamMsg, UreqHttp};
use tidetable_tui::motion;
use tidetable_tui::refresh::{Refresher, Watcher};
use tidetable_tui::review::Ticks;
use tidetable_tui::source::{Action, CliRunner, LocalSource, Snapshot, Source, SourceError};
use tidetable_tui::theme::Theme;
use tidetable_tui::ui;

const USAGE: &str = "usage: tidetable-tui [TODO_FILE | SLUG] [--mode auto|standard|side|bottom] [--palette NAME]\n\
                     \x20                   [--read-only] [--owner NAME] [--server URL] [--token TOKEN] [--local]\n\
                     hub: with --server (or $TIDETABLE_SERVER) the argument is the project slug,\n\
                     else $TIDETABLE_PROJECT, then .tidetable/config.json \"slug\", then a picker;\n\
                     prefer $TIDETABLE_TOKEN over --token, which shows up in process listings";

struct Args {
    file: Option<String>,
    mode: ModeArg,
    palette: Option<String>,
    read_only: bool,
    owner: Option<String>,
    server: Option<String>,
    token: Option<String>,
    local: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        file: None,
        mode: ModeArg::Auto,
        palette: None,
        read_only: false,
        owner: None,
        server: None,
        token: None,
        local: false,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--mode" => {
                let v = it.next().ok_or("--mode needs a value")?;
                a.mode = ModeArg::parse(&v).ok_or(format!("unknown mode {v:?}"))?;
            }
            "--palette" => a.palette = Some(it.next().ok_or("--palette needs a value")?),
            "--read-only" => a.read_only = true,
            "--local" => a.local = true,
            "--owner" => a.owner = Some(it.next().ok_or("--owner needs a value")?),
            "--server" => a.server = Some(it.next().ok_or("--server needs a value")?),
            "--token" => a.token = Some(it.next().ok_or("--token needs a value")?),
            "-h" | "--help" => return Err(String::new()),
            f if !f.starts_with('-') => a.file = Some(f.to_string()),
            other => return Err(format!("unknown option {other}")),
        }
    }
    Ok(a)
}

fn now_epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

enum Backend {
    Local(LocalSource<CliRunner>),
    Hub(HubSource<UreqHttp>),
}

impl Source for Backend {
    fn snapshot(&mut self) -> Result<Snapshot, SourceError> {
        match self {
            Backend::Local(s) => s.snapshot(),
            Backend::Hub(s) => s.snapshot(),
        }
    }
    fn act(&mut self, action: &Action) -> Result<String, String> {
        match self {
            Backend::Local(s) => s.act(action),
            Backend::Hub(s) => s.act(action),
        }
    }
}

/// What one refresh attempt did; `Denied` must halt the retry loop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Refreshed {
    /// A snapshot is on screen (or a picker was opened).
    Ok,
    /// Transient: keep the last snapshot and retry with backoff.
    Stale,
    /// 401/403: retrying cannot help.
    Denied { forbidden: bool },
    /// No data and no retry value (local failure).
    Failed,
}

/// One snapshot into the app.
fn refresh(app: &mut App, source: &mut Backend) -> Refreshed {
    app.now_epoch = now_epoch();
    match source.snapshot() {
        Ok(s) => {
            app.set_snapshot(s);
            app.last_ok = Some(app.now_epoch);
            Refreshed::Ok
        }
        Err(SourceError::Pick(files)) => {
            app.load = Load::Pick(files);
            Refreshed::Ok
        }
        Err(SourceError::Auth { forbidden, message }) => {
            app.load = Load::Failed(message);
            Refreshed::Denied { forbidden }
        }
        // A hub that drops out keeps the last snapshot on screen; the footer says it is stale.
        Err(_) if matches!(source, Backend::Hub(_)) && app.load == Load::Ready => Refreshed::Stale,
        Err(e) => {
            app.load = Load::Failed(e.to_string());
            Refreshed::Failed
        }
    }
}

/// Feed one attempt into the poll/backoff state. Local failures keep polling.
fn note(refresher: &mut Refresher, outcome: Refreshed, hub: bool, now: Instant) {
    match outcome {
        Refreshed::Denied { forbidden } => refresher.deny(forbidden),
        Refreshed::Ok => refresher.record(true, now),
        Refreshed::Stale | Refreshed::Failed => refresher.record(!hub, now),
    }
}

/// Widgets first, then tachyonfx over the finished buffer.
fn draw(f: &mut ratatui::Frame, app: &mut App) {
    ui::draw(f, app);
    let rows = app.row_rects();
    let area = f.area();
    let accent = app.theme.accent;
    app.fx
        .run(f.buffer_mut(), area, &rows, accent, Instant::now());
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(m) => {
            if !m.is_empty() {
                eprintln!("{m}");
            }
            eprintln!("{USAGE}");
            std::process::exit(2);
        }
    };
    let (theme, warn) = Theme::select(args.palette.as_deref(), |k| std::env::var(k).ok());
    if let Some(w) = warn {
        eprintln!("{w}");
    }
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.trim().is_empty());
    let server = if args.local {
        None
    } else {
        args.server.clone().or_else(|| env("TIDETABLE_SERVER"))
    };
    let token = args.token.clone().or_else(|| env("TIDETABLE_TOKEN"));

    let owner = Some(tidetable_tui::source::default_owner(args.owner.clone()));
    let cwd = std::env::current_dir().unwrap_or_default();
    let mut projects_picker: Option<Vec<(String, String)>> = None;
    let (mut rx, mut source) = match &server {
        Some(url) => {
            let http = |t: Option<String>| UreqHttp::new(url, t);
            let configured = tidetable_tui::slug::resolve(
                args.file.clone(),
                env("TIDETABLE_PROJECT"),
                tidetable_tui::slug::config_slug(&cwd),
            );
            match configured {
                Some(slug) => {
                    let (main_http, stream_http) = match (http(token.clone()), http(token.clone()))
                    {
                        (Ok(a), Ok(b)) => (a, b),
                        (Err(e), _) | (_, Err(e)) => {
                            eprintln!("{e}");
                            std::process::exit(2);
                        }
                    };
                    let (tx, rx) = mpsc::channel();
                    spawn_stream(stream_http, slug.clone(), tx);
                    (
                        Some(rx),
                        Backend::Hub(HubSource::new(main_http, slug).with_owner(owner.clone())),
                    )
                }
                None => {
                    // No slug anywhere: list what this token can see and let the user pick.
                    let main_http = match http(token.clone()) {
                        Ok(h) => h,
                        Err(e) => {
                            eprintln!("{e}");
                            std::process::exit(2);
                        }
                    };
                    match tidetable_tui::hub::list_projects(&main_http) {
                        Ok(projects) => projects_picker = Some(projects),
                        Err(e) => {
                            eprintln!("{e}");
                            std::process::exit(2);
                        }
                    }
                    (
                        None,
                        Backend::Hub(
                            HubSource::new(main_http, String::new()).with_owner(owner.clone()),
                        ),
                    )
                }
            }
        }
        None => {
            let mut local =
                LocalSource::new(CliRunner::default(), cwd.clone()).with_owner(owner.clone());
            if let Some(f) = &args.file {
                local.choose(PathBuf::from(f));
            }
            (None, Backend::Local(local))
        }
    };
    let mut hub = server.is_some();
    let mut app = App::new(theme, args.mode, Ticks::load(&cwd));
    app.read_only = args.read_only;
    app.motion = motion::enabled(|k| std::env::var(k).ok());
    if let Some(projects) = projects_picker.take() {
        app.load = Load::PickProjects(projects);
    }

    // ratatui::init installs a panic hook that restores the terminal.
    let mut terminal = ratatui::init();
    let _ = crossterm::execute!(std::io::stdout(), crossterm::event::EnableMouseCapture);
    let _ = terminal.draw(|f| draw(f, &mut app));
    let mut refresher = Refresher::new(hub, Instant::now());
    let picking = matches!(app.load, Load::PickProjects(_));
    if !picking {
        let outcome = refresh(&mut app, &mut source);
        note(&mut refresher, outcome, hub, Instant::now());
    }
    let mut watcher = Watcher::default();
    let mut dirty = true;

    while !app.quit {
        // Repaint only when something changed: an idle TUI costs no frames.
        if dirty && terminal.draw(|f| draw(f, &mut app)).is_err() {
            break;
        }
        dirty = false;
        // Fast wake-ups only while a pulse is live; otherwise block for the housekeeping tick.
        let wait = if app.fx.live() { 50 } else { 250 };
        match event::poll(Duration::from_millis(wait)) {
            Ok(true) => {
                match event::read() {
                    Ok(Event::Key(k)) if k.kind == KeyEventKind::Press => app.on_key(k),
                    Ok(Event::Mouse(m)) => app.on_mouse(m),
                    Ok(_) => {}
                    Err(_) => break,
                }
                dirty = true;
            }
            Ok(false) => {}
            Err(_) => break,
        }

        let now = Instant::now();
        if app.fx.live() {
            dirty = true;
        }
        let mut pushed = false;
        if let Some(rx) = &rx {
            for m in rx.try_iter() {
                match m {
                    StreamMsg::Connected => refresher.set_stream(true),
                    StreamMsg::Changed => pushed = true,
                    StreamMsg::Disconnected | StreamMsg::Unsupported => refresher.set_stream(false),
                    StreamMsg::Denied { forbidden } => {
                        refresher.set_stream(false);
                        refresher.deny(forbidden);
                    }
                }
            }
        } else if !app.snap.todo_file.is_empty() {
            if !watcher.is_for(&app.snap.todo_file) {
                watcher = Watcher::for_todo(&app.snap.todo_file);
            }
            pushed = watcher.changed();
        }

        if app.pending.is_some() {
            run_pending(&mut app, &mut source);
            dirty = true;
        }
        if let Some(f) = app.chosen.take() {
            if let Backend::Local(l) = &mut source {
                l.choose(f);
            }
            app.want_refresh = true;
        }
        if let Some(slug) = app.chosen_slug.take() {
            let url = server.clone().unwrap_or_default();
            match (
                UreqHttp::new(&url, token.clone()),
                UreqHttp::new(&url, token.clone()),
            ) {
                (Ok(main_http), Ok(stream_http)) => {
                    let (tx, new_rx) = mpsc::channel();
                    spawn_stream(stream_http, slug.clone(), tx);
                    source =
                        Backend::Hub(HubSource::new(main_http, slug).with_owner(owner.clone()));
                    rx = Some(new_rx);
                    hub = true;
                    app.load = Load::Loading;
                    app.want_refresh = true;
                    refresher = Refresher::new(hub, Instant::now());
                    dirty = true;
                }
                (Err(e), _) | (_, Err(e)) => {
                    app.load = Load::Failed(e);
                    dirty = true;
                }
            }
        }
        if !matches!(app.load, Load::PickProjects(_))
            && (app.want_refresh || refresher.due(now, pushed))
        {
            app.want_refresh = false;
            let outcome = refresh(&mut app, &mut source);
            note(&mut refresher, outcome, hub, Instant::now());
            dirty = true;
        }
        let conn = refresher.conn(Instant::now());
        if conn != app.conn {
            app.conn = conn;
            app.now_epoch = now_epoch();
            dirty = true;
        }
    }
    let _ = crossterm::execute!(std::io::stdout(), crossterm::event::DisableMouseCapture);
    ratatui::restore();
}
