use std::path::PathBuf;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyEventKind};
use tidetable_tui::app::ModeArg;
use tidetable_tui::hub::StreamMsg;
#[cfg(feature = "hub")]
use tidetable_tui::hub::{spawn_stream, HubSource, UreqHttp};
use tidetable_tui::motion;
use tidetable_tui::pane::{KeyOutcome, Pane};
use tidetable_tui::review::Ticks;
use tidetable_tui::source::{Action, CliRunner, LocalSource, Snapshot, Source, SourceError};
use tidetable_tui::theme::Theme;

const USAGE: &str = "usage: tidetable-tui [TODO_FILE | SLUG] [--mode auto|standard|side|bottom] [--palette NAME]\n\
                     \x20                   [--read-only] [--owner NAME] [--server URL] [--token TOKEN] [--local]\n\
                     hub: with --server (or $TIDETABLE_SERVER) the argument is the project slug;\n\
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

enum Backend {
    Local(LocalSource<CliRunner>),
    #[cfg(feature = "hub")]
    Hub(HubSource<UreqHttp>),
}

impl Source for Backend {
    fn snapshot(&mut self) -> Result<Snapshot, SourceError> {
        match self {
            Backend::Local(s) => s.snapshot(),
            #[cfg(feature = "hub")]
            Backend::Hub(s) => s.snapshot(),
        }
    }
    fn act(&mut self, action: &Action) -> Result<String, String> {
        match self {
            Backend::Local(s) => s.act(action),
            #[cfg(feature = "hub")]
            Backend::Hub(s) => s.act(action),
        }
    }
    fn choose(&mut self, file: PathBuf) {
        #[allow(irrefutable_let_patterns)]
        if let Backend::Local(s) = self {
            s.choose(file);
        }
    }
}

/// The hub backend plus the receiver its event-stream thread feeds.
#[cfg(feature = "hub")]
fn hub_backend(
    url: &str,
    slug: String,
    token: Option<String>,
    owner: Option<String>,
) -> (mpsc::Receiver<StreamMsg>, Backend) {
    let http = |t: Option<String>| UreqHttp::new(url, t);
    let (main_http, stream_http) = match (http(token.clone()), http(token)) {
        (Ok(a), Ok(b)) => (a, b),
        (Err(e), _) | (_, Err(e)) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    let (tx, rx) = mpsc::channel();
    spawn_stream(stream_http, slug.clone(), tx);
    (
        rx,
        Backend::Hub(HubSource::new(main_http, slug).with_owner(owner)),
    )
}

#[cfg(not(feature = "hub"))]
fn hub_backend(
    _url: &str,
    _slug: String,
    _token: Option<String>,
    _owner: Option<String>,
) -> (mpsc::Receiver<StreamMsg>, Backend) {
    eprintln!("this tidetable-tui was built without the `hub` feature; use --local");
    std::process::exit(2);
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
    let (rx, source) = match &server {
        Some(url) => {
            let Some(slug) = args.file.clone() else {
                eprintln!("hub mode needs a project slug\n{USAGE}");
                std::process::exit(2);
            };
            let (rx, backend) = hub_backend(url, slug, token, owner.clone());
            (Some(rx), backend)
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
    let hub = rx.is_some();
    // The binary is a shell around the same pane an embedder draws: it owns the terminal and
    // the event loop; the pane owns state, data and drawing.
    let mut pane = Pane::new(theme, args.mode)
        .with_ticks(Ticks::load(&cwd))
        .with_source(source);
    pane.set_hub(hub);
    pane.set_focused(true);
    pane.set_read_only(args.read_only);
    pane.set_motion(motion::enabled(|k| std::env::var(k).ok()));

    // ratatui::init installs a panic hook that restores the terminal.
    let mut terminal = ratatui::init();
    let _ = crossterm::execute!(std::io::stdout(), crossterm::event::EnableMouseCapture);
    let _ = terminal.draw(|f| pane.render(f.area(), f.buffer_mut()));
    // The first tick takes the first snapshot.
    pane.tick(Instant::now(), false);
    let mut dirty = true;

    loop {
        // Repaint only when something changed: an idle TUI costs no frames.
        if dirty
            && terminal
                .draw(|f| pane.render(f.area(), f.buffer_mut()))
                .is_err()
        {
            break;
        }
        dirty = false;
        // Fast wake-ups only while a pulse is live; otherwise block for the housekeeping tick.
        let wait = if pane.animating() { 50 } else { 250 };
        match event::poll(Duration::from_millis(wait)) {
            Ok(true) => {
                match event::read() {
                    Ok(Event::Key(k)) if k.kind == KeyEventKind::Press => {
                        if pane.handle_key(k) == KeyOutcome::Release {
                            break;
                        }
                    }
                    Ok(Event::Mouse(m)) => {
                        pane.handle_mouse(m);
                    }
                    Ok(_) => {}
                    Err(_) => break,
                }
                dirty = true;
            }
            Ok(false) => {}
            Err(_) => break,
        }

        let mut pushed = false;
        if let Some(rx) = &rx {
            for m in rx.try_iter() {
                match m {
                    StreamMsg::Connected => pane.set_stream(true),
                    StreamMsg::Changed => pushed = true,
                    StreamMsg::Disconnected | StreamMsg::Unsupported => pane.set_stream(false),
                }
            }
        }
        if pane.tick(Instant::now(), pushed) {
            dirty = true;
        }
    }
    let _ = crossterm::execute!(std::io::stdout(), crossterm::event::DisableMouseCapture);
    ratatui::restore();
}
