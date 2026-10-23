use std::fmt;
use std::path::{Path, PathBuf};
use std::process::Command;

use std::collections::{HashMap, HashSet};

use serde::Deserialize;
use serde_json::Value;

/// Same search order as `tidetable.cli.TODO_SEARCH_DIRS`; first directory with a match wins.
pub const TODO_SEARCH_DIRS: [&str; 3] = [".", "docs", ".tidetable"];

/// How many finished tasks a snapshot carries; the Done view is "recent", not history.
const RECENT_DONE: usize = 10;

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Default)]
pub struct TaskRef {
    pub id: String,
    #[serde(default)]
    pub title: String,
}

/// What the detail pane shows for a task. Only ready and running tasks carry it today.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Default)]
pub struct Detail {
    #[serde(default)]
    pub goal: String,
    #[serde(default)]
    pub touches: Vec<String>,
    #[serde(default)]
    pub prerequisites: Vec<String>,
    #[serde(default)]
    pub success: Vec<String>,
}

/// `3.1.2` lives in phase `3.1`; the validator guarantees task ids agree with their phase.
pub fn phase_of(task_id: &str) -> &str {
    task_id.rsplit_once('.').map_or(task_id, |(p, _)| p)
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Running {
    pub id: String,
    pub title: String,
    pub owner: Option<String>,
    /// `...Z` timestamp from the claim; use [`Running::age_secs`] to render age.
    pub claimed_at: Option<String>,
    /// `...Z` when the claim lapses; use [`Running::lease_left_secs`] for time left.
    pub lease_expires_at: Option<String>,
    pub lease_expired: bool,
}

impl Running {
    pub fn age_secs(&self, now_epoch: i64) -> Option<i64> {
        Some((now_epoch - parse_utc(self.claimed_at.as_deref()?)?).max(0))
    }

    /// Seconds until the lease lapses; negative once it has. None when unclaimed.
    pub fn lease_left_secs(&self, now_epoch: i64) -> Option<i64> {
        Some(parse_utc(self.lease_expires_at.as_deref()?)? - now_epoch)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Default)]
pub struct Blocked {
    pub id: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub blocked_by: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Default)]
pub struct LookItem {
    pub id: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub phase: String,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub goal: String,
    #[serde(default)]
    pub success: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct PhaseProgress {
    pub id: String,
    pub title: String,
    pub done: usize,
    pub total: usize,
    pub complete: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Snapshot {
    /// The todo file stem; `list --json` carries no project name and we never open the file.
    pub project: String,
    pub todo_file: String,
    pub in_progress: Vec<Running>,
    pub ready: Vec<TaskRef>,
    pub blocked: Vec<Blocked>,
    pub done: Vec<TaskRef>,
    pub look_queue: Vec<LookItem>,
    pub phases: Vec<PhaseProgress>,
    pub details: HashMap<String, Detail>,
    /// Ready tasks that `ready --disjoint` would dispatch together (no shared `touches`).
    pub disjoint: HashSet<String>,
    /// Last `done` changelog line per task, from the append-only event log.
    pub changelog: HashMap<String, String>,
}

#[derive(Debug)]
pub enum SourceError {
    /// No todo*.json in any search directory.
    NoTodo { searched: Vec<String> },
    /// More than one candidate; the TUI shows a picker instead of prompting.
    Pick(Vec<PathBuf>),
    /// The CLI failed or could not be started.
    Cli(String),
    /// The hub refused the credential (401) or the project (403); retrying cannot help.
    Auth { forbidden: bool, message: String },
    /// The CLI answered something that is not the JSON we expect.
    Parse(String),
}

impl fmt::Display for SourceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoTodo { searched } => {
                write!(f, "no todo*.json found in {}", searched.join(", "))
            }
            Self::Pick(files) => write!(f, "{} todo files found; pick one", files.len()),
            Self::Cli(m) => write!(f, "tidetable failed: {m}"),
            Self::Auth { message, .. } => write!(f, "{message}"),
            Self::Parse(m) => write!(f, "unexpected tidetable output: {m}"),
        }
    }
}

impl std::error::Error for SourceError {}

/// A change the human asks for. The CLI (or a hub) performs it; this crate never edits a todo.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    Done {
        id: String,
        changelog: Option<String>,
    },
    /// `look-clear`: a human looked, the task becomes done.
    Clear { id: String },
    /// `look-clear --reject`: the check failed, back to pending.
    Reject { id: String },
    /// `look-defer`: park a finished task in the human queue.
    Defer { id: String, note: String },
    /// `start`: claim a ready task.
    Start { id: String },
}

/// One owner for every claim and finish from this TUI, so `done` matches `start`.
/// `--owner`, then `$TIDETABLE_OWNER`, then `tui@<host>`.
pub fn default_owner(flag: Option<String>) -> String {
    flag.or_else(|| {
        std::env::var("TIDETABLE_OWNER")
            .ok()
            .filter(|o| !o.is_empty())
    })
    .unwrap_or_else(|| {
        let host = std::env::var("COMPUTERNAME")
            .or_else(|_| std::env::var("HOSTNAME"))
            .unwrap_or_else(|_| "local".into());
        format!("tui@{host}")
    })
}

impl Action {
    pub fn id(&self) -> &str {
        match self {
            Action::Done { id, .. }
            | Action::Clear { id }
            | Action::Reject { id }
            | Action::Defer { id, .. }
            | Action::Start { id } => id,
        }
    }

    pub fn verb(&self) -> &'static str {
        match self {
            Action::Done { .. } => "done",
            Action::Clear { .. } => "clear",
            Action::Reject { .. } => "reject",
            Action::Defer { .. } => "defer",
            Action::Start { .. } => "start",
        }
    }

    /// The exact CLI argv (after `--local`) for this action against `file`.
    pub fn argv(&self, file: &str, owner: Option<&str>) -> Vec<String> {
        let mut a: Vec<String> = match self {
            Action::Done { id, .. } => vec!["done".into(), file.into(), id.clone()],
            Action::Clear { id } | Action::Reject { id } => {
                vec!["look-clear".into(), file.into(), id.clone()]
            }
            Action::Defer { id, .. } => vec!["look-defer".into(), file.into(), id.clone()],
            Action::Start { id } => vec!["start".into(), file.into(), id.clone()],
        };
        match self {
            Action::Done {
                changelog: Some(c), ..
            } if !c.trim().is_empty() => a.extend(["--changelog".into(), c.trim().into()]),
            Action::Reject { .. } => a.push("--reject".into()),
            Action::Defer { note, .. } if !note.trim().is_empty() => {
                a.extend(["--note".into(), note.trim().into()])
            }
            _ => {}
        }
        if let Some(o) = owner {
            a.extend(["--owner".into(), o.into()]);
        }
        a
    }
}

/// Anything that can produce a [`Snapshot`] and carry out an [`Action`]: local files via the
/// CLI, or a hub.
pub trait Source {
    fn snapshot(&mut self) -> Result<Snapshot, SourceError>;
    /// `Ok` is the CLI's confirmation line, `Err` its own refusal text.
    fn act(&mut self, action: &Action) -> Result<String, String>;
}

/// Runs one `tidetable` invocation and returns stdout. A trait so tests inject a fake.
pub trait Runner {
    fn run(&self, args: &[String]) -> Result<String, String>;
}

pub struct CliRunner {
    pub program: String,
}

impl Default for CliRunner {
    fn default() -> Self {
        Self {
            program: "tidetable".into(),
        }
    }
}

impl Runner for CliRunner {
    fn run(&self, args: &[String]) -> Result<String, String> {
        let out = Command::new(&self.program)
            .args(args)
            .env("PYTHONIOENCODING", "utf-8")
            .output()
            .map_err(|e| format!("{}: {e}", self.program))?;
        if !out.status.success() {
            let msg = String::from_utf8_lossy(&out.stdout);
            let err = String::from_utf8_lossy(&out.stderr);
            return Err(format!("{} {}", msg.trim(), err.trim()).trim().to_string());
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }
}

/// First search directory with any `todo*.json` wins, matching the CLI. Never prompts.
pub fn resolve_todo(base: &Path) -> Result<PathBuf, SourceError> {
    for sub in TODO_SEARCH_DIRS {
        let dir = if sub == "." {
            base.to_path_buf()
        } else {
            base.join(sub)
        };
        let Ok(entries) = std::fs::read_dir(dir) else {
            continue;
        };
        let mut found: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| {
                p.is_file()
                    && p.extension().is_some_and(|x| x == "json")
                    && p.file_name()
                        .and_then(|n| n.to_str())
                        .is_some_and(|n| n.starts_with("todo"))
            })
            .collect();
        found.sort();
        match found.len() {
            0 => continue,
            1 => return Ok(found.remove(0)),
            _ => return Err(SourceError::Pick(found)),
        }
    }
    Err(SourceError::NoTodo {
        searched: TODO_SEARCH_DIRS.iter().map(|s| s.to_string()).collect(),
    })
}

pub struct LocalSource<R: Runner> {
    runner: R,
    base: PathBuf,
    todo: Option<PathBuf>,
    owner: Option<String>,
}

impl<R: Runner> LocalSource<R> {
    pub fn new(runner: R, base: PathBuf) -> Self {
        Self {
            runner,
            base,
            todo: None,
            owner: None,
        }
    }

    /// Identity passed as `--owner`; unset means the CLI's own default (`$TIDETABLE_OWNER`, else host:pid).
    pub fn with_owner(mut self, owner: Option<String>) -> Self {
        self.owner = owner;
        self
    }

    /// The picker's answer; skips the search chain.
    pub fn choose(&mut self, file: PathBuf) {
        self.todo = Some(file);
    }

    fn json(&self, cmd: &str, file: &str, flags: &[&str]) -> Result<Value, SourceError> {
        // --local: an exported TIDETABLE_SERVER must not reroute a local read to a hub.
        let mut args: Vec<String> = vec!["--local".into(), cmd.into(), file.into()];
        args.extend(flags.iter().map(|s| s.to_string()));
        let out = match self.runner.run(&args) {
            Ok(o) => o,
            // `ready` exits 1 when nothing is ready (all done, or all blocked); the JSON is still the answer.
            Err(e) if cmd == "ready" && serde_json::from_str::<Value>(&e).is_ok() => e,
            Err(e) => return Err(SourceError::Cli(e)),
        };
        serde_json::from_str(&out).map_err(|e| SourceError::Parse(format!("{cmd}: {e}")))
    }
}

impl<R: Runner> Source for LocalSource<R> {
    fn act(&mut self, action: &Action) -> Result<String, String> {
        let todo = match &self.todo {
            Some(t) => t.clone(),
            None => resolve_todo(&self.base).map_err(|e| e.to_string())?,
        };
        let mut args: Vec<String> = vec!["--local".into()];
        args.extend(action.argv(&todo.to_string_lossy(), self.owner.as_deref()));
        let out = self.runner.run(&args)?;
        Ok(out.lines().next().unwrap_or("done").trim().to_string())
    }

    fn snapshot(&mut self) -> Result<Snapshot, SourceError> {
        let todo = match &self.todo {
            Some(t) => t.clone(),
            None => resolve_todo(&self.base)?,
        };
        let file = todo.to_string_lossy().into_owned();
        let list = self.json("list", &file, &["--json"])?;
        let ready = self.json("ready", &file, &["--json"])?;
        let disjoint = self.json("ready", &file, &["--disjoint", "--json"])?;
        let look = self.json("look-queue", &file, &["-v", "--json"])?;
        let history = self.json("history", &file, &["--json"])?;
        let mut snap = build_snapshot(&todo, &list, &ready, &look)?;
        snap.disjoint = ids_of(disjoint.get("ready"));
        snap.changelog = changelog_of(&history);
        Ok(snap)
    }
}

fn parse_vec<T: for<'de> Deserialize<'de>>(
    v: Option<&Value>,
    what: &str,
) -> Result<Vec<T>, SourceError> {
    match v {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(v) => serde_json::from_value(v.clone())
            .map_err(|e| SourceError::Parse(format!("{what}: {e}"))),
    }
}

pub(crate) fn ids_of(v: Option<&Value>) -> HashSet<String> {
    v.and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|t| str_of(t, "id"))
        .collect()
}

pub(crate) fn changelog_of(history: &Value) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for e in history
        .get("events")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if e.get("event").and_then(Value::as_str) == Some("done") {
            if let Some(c) = e
                .get("changelog")
                .and_then(Value::as_str)
                .filter(|c| !c.is_empty())
            {
                out.insert(str_of(e, "task"), c.to_string());
            }
        }
    }
    out
}

fn str_of(v: &Value, key: &str) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

pub fn build_snapshot(
    todo: &Path,
    list: &Value,
    ready: &Value,
    look: &Value,
) -> Result<Snapshot, SourceError> {
    let mut snap = Snapshot {
        project: todo
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("todo")
            .to_string(),
        todo_file: todo.to_string_lossy().into_owned(),
        ..Snapshot::default()
    };

    let is_done = |t: &Value| t.get("status").and_then(Value::as_str) == Some("done");
    for p in list
        .get("phases")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let tasks = p
            .get("tasks")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        snap.phases.push(PhaseProgress {
            id: str_of(p, "id"),
            title: str_of(p, "title"),
            done: tasks.iter().filter(|t| is_done(t)).count(),
            total: tasks.len(),
            complete: p.get("complete").and_then(Value::as_bool).unwrap_or(false),
        });
        for t in tasks.iter().filter(|t| is_done(t)) {
            snap.done.push(TaskRef {
                id: str_of(t, "id"),
                title: str_of(t, "title"),
            });
        }
    }
    // File order runs oldest to newest; show the tail, newest first.
    snap.done.reverse();
    snap.done.truncate(RECENT_DONE);

    for t in ready
        .get("in_progress")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        snap.in_progress.push(Running {
            id: str_of(t, "id"),
            title: str_of(t, "title"),
            owner: t
                .get("claimed_by")
                .and_then(Value::as_str)
                .map(String::from),
            claimed_at: t
                .get("claimed_at")
                .and_then(Value::as_str)
                .map(String::from),
            lease_expires_at: t
                .get("lease_expires_at")
                .and_then(Value::as_str)
                .map(String::from),
            lease_expired: t
                .get("lease_expired")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        });
    }
    for t in ready
        .get("ready")
        .and_then(Value::as_array)
        .into_iter()
        .chain(ready.get("in_progress").and_then(Value::as_array))
        .flatten()
    {
        let d: Detail = serde_json::from_value(t.clone()).unwrap_or_default();
        snap.details.insert(str_of(t, "id"), d);
    }
    snap.ready = parse_vec(ready.get("ready"), "ready")?;
    snap.blocked = parse_vec(ready.get("blocked"), "blocked")?;
    snap.look_queue = parse_vec(look.get("queue"), "queue")?;
    Ok(snap)
}

/// Seconds since the Unix epoch for `YYYY-MM-DDTHH:MM:SSZ`; no chrono for one format.
pub fn parse_utc(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() < 19 || b[4] != b'-' || b[7] != b'-' || b[10] != b'T' {
        return None;
    }
    let n = |r: std::ops::Range<usize>| s.get(r)?.parse::<i64>().ok();
    let (y, m, d) = (n(0..4)?, n(5..7)?, n(8..10)?);
    let (hh, mm, ss) = (n(11..13)?, n(14..16)?, n(17..19)?);
    // Howard Hinnant's days-from-civil.
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some((era * 146097 + doe - 719468) * 86400 + hh * 3600 + mm * 60 + ss)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    const LIST: &str = r#"{"phases":[
      {"id":"1","title":"One","complete":false,"tasks":[
        {"id":"1.1","title":"first","status":"done"},
        {"id":"1.2","title":"second","status":"done"},
        {"id":"1.3","title":"third","status":"in_progress"},
        {"id":"1.4","title":"fourth","status":"pending"}]}],
      "summary":{}}"#;
    const READY: &str = r#"{"ready":[{"id":"1.4","title":"fourth","goal":"g4","success":["ok"],"prerequisites":["1.3"]}],
      "in_progress":[{"id":"1.3","title":"third","goal":"g3","touches":["a/"],"claimed_by":"bot:1","claimed_at":"2026-10-17T06:29:33Z","lease_expired":false}],
      "blocked":[{"id":"1.5","title":"fifth","blocked_by":["task 1.4"]}]}"#;
    const DISJOINT: &str = r#"{"ready":[{"id":"1.4"}]}"#;
    const HISTORY: &str = r#"{"events":[{"event":"start","task":"1.1"},{"event":"done","task":"1.1","changelog":"did it"}]}"#;
    const LOOK: &str = r#"{"queue":[{"id":"1.2","title":"second","phase":"1","note":"eyeball","goal":"g","success":["a","b"]}]}"#;

    struct Fake {
        calls: RefCell<Vec<Vec<String>>>,
    }

    impl Runner for Fake {
        fn run(&self, args: &[String]) -> Result<String, String> {
            self.calls.borrow_mut().push(args.to_vec());
            match args.get(1).map(String::as_str) {
                Some("list") => Ok(LIST.into()),
                Some("ready") if args.iter().any(|a| a == "--disjoint") => Ok(DISJOINT.into()),
                Some("ready") => Ok(READY.into()),
                Some("history") => Ok(HISTORY.into()),
                Some("look-queue") => Ok(LOOK.into()),
                other => Err(format!("unexpected {other:?}")),
            }
        }
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tt-tui-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    struct HubFake;

    impl crate::hub::Http for HubFake {
        fn get(&self, path: &str) -> Result<Value, String> {
            let body = match path {
                "/api/todo-x/list" => LIST,
                "/api/todo-x/ready" => READY,
                "/api/todo-x/ready?disjoint=1" => DISJOINT,
                "/api/todo-x/look-queue" => LOOK,
                "/api/todo-x/history?limit=50" => HISTORY,
                other => return Err(format!("unexpected {other}")),
            };
            Ok(serde_json::from_str(body).unwrap())
        }
        fn post(&self, _: &str, _: &Value) -> Result<Value, String> {
            Err("read-only fixture".into())
        }
    }

    /// `tidetable ready` exits 1 when nothing is ready; stdout is still the JSON answer.
    struct ExitOneOnEmptyReady;

    impl Runner for ExitOneOnEmptyReady {
        fn run(&self, args: &[String]) -> Result<String, String> {
            match args.get(1).map(String::as_str) {
                Some("ready") => {
                    Err(r#"{"ready":[],"disjoint":false,"in_progress":[],"blocked":[]}"#.into())
                }
                _ => Fake {
                    calls: RefCell::new(vec![]),
                }
                .run(args),
            }
        }
    }

    #[test]
    fn nothing_ready_is_a_state_not_an_error() {
        let base = tmp("empty-ready");
        std::fs::write(base.join("todo-x.json"), "{}").unwrap();
        let mut src = LocalSource::new(ExitOneOnEmptyReady, base);
        let snap = src
            .snapshot()
            .expect("exit 1 with JSON must not fail the snapshot");
        assert!(snap.ready.is_empty());
    }

    #[test]
    fn hub_and_local_sources_yield_identical_snapshots() {
        let base = tmp("parity");
        std::fs::write(base.join("todo-x.json"), "{}").unwrap();
        let mut local = LocalSource::new(
            Fake {
                calls: RefCell::new(vec![]),
            },
            base,
        )
        .snapshot()
        .unwrap();
        let hub = crate::hub::HubSource::new(HubFake, "todo-x")
            .snapshot()
            .unwrap();
        // The only legitimate difference is where the data came from.
        local.todo_file = hub.todo_file.clone();
        assert_eq!(local, hub);
    }

    #[test]
    fn snapshot_from_fake_cli() {
        let base = tmp("snap");
        std::fs::create_dir_all(base.join("docs")).unwrap();
        std::fs::write(base.join("docs/todo-x.json"), "{}").unwrap();
        let mut src = LocalSource::new(
            Fake {
                calls: RefCell::new(vec![]),
            },
            base.clone(),
        );
        let s = src.snapshot().unwrap();
        assert_eq!(s.project, "todo-x");
        assert_eq!(s.in_progress[0].owner.as_deref(), Some("bot:1"));
        assert_eq!(s.ready[0].id, "1.4");
        assert_eq!(s.details["1.4"].success, vec!["ok"]);
        assert_eq!(s.details["1.3"].touches, vec!["a/"]);
        assert_eq!(s.blocked[0].blocked_by, vec!["task 1.4"]);
        assert_eq!(
            s.done.iter().map(|t| t.id.as_str()).collect::<Vec<_>>(),
            ["1.2", "1.1"]
        );
        assert_eq!(s.look_queue[0].success, vec!["a", "b"]);
        assert_eq!((s.phases[0].done, s.phases[0].total), (2, 4));
        let calls = src.runner.calls.borrow();
        assert_eq!(calls.len(), 5);
        assert!(s.disjoint.contains("1.4"));
        assert_eq!(s.changelog.get("1.1").map(String::as_str), Some("did it"));
        assert_eq!(phase_of("3.1.2"), "3.1");
        // Every call is --local so an exported hub variable cannot reroute reads.
        assert!(calls
            .iter()
            .all(|c| c[0] == "--local" && c[2].ends_with("todo-x.json")));
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn resolution_matches_cli_rules() {
        let base = tmp("res");
        assert!(matches!(
            resolve_todo(&base),
            Err(SourceError::NoTodo { .. })
        ));
        std::fs::create_dir_all(base.join("docs")).unwrap();
        std::fs::create_dir_all(base.join(".tidetable")).unwrap();
        std::fs::write(base.join(".tidetable/todo.json"), "{}").unwrap();
        std::fs::write(base.join("docs/todo-a.json"), "{}").unwrap();
        // docs/ is searched before .tidetable/, and a single match resolves.
        assert_eq!(
            resolve_todo(&base).unwrap().file_name().unwrap(),
            "todo-a.json"
        );
        std::fs::write(base.join("docs/todo-b.json"), "{}").unwrap();
        match resolve_todo(&base) {
            Err(SourceError::Pick(f)) => assert_eq!(f.len(), 2),
            other => panic!("{other:?}"),
        }
        // "." wins over docs/, and a non-todo json is ignored.
        std::fs::write(base.join("other.json"), "{}").unwrap();
        std::fs::write(base.join("todo.json"), "{}").unwrap();
        assert_eq!(
            resolve_todo(&base).unwrap().file_name().unwrap(),
            "todo.json"
        );
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn actions_invoke_exactly_the_expected_argv() {
        struct Rec(RefCell<Vec<Vec<String>>>, Result<String, String>);
        impl Runner for Rec {
            fn run(&self, args: &[String]) -> Result<String, String> {
                self.0.borrow_mut().push(args.to_vec());
                self.1.clone()
            }
        }
        let base = tmp("act");
        std::fs::write(base.join("todo.json"), "{}").unwrap();
        let f = base.join("todo.json").to_string_lossy().into_owned();
        let mut src = LocalSource::new(
            Rec(
                RefCell::new(vec![]),
                Ok("✓ Task 1 marked as done.
more"
                    .into()),
            ),
            base.clone(),
        );
        let cases: Vec<(Action, Vec<&str>)> = vec![
            (
                Action::Done {
                    id: "1.1".into(),
                    changelog: None,
                },
                vec!["done", &f, "1.1"],
            ),
            (
                Action::Done {
                    id: "1.1".into(),
                    changelog: Some(" shipped ".into()),
                },
                vec!["done", &f, "1.1", "--changelog", "shipped"],
            ),
            (Action::Start { id: "1.4".into() }, vec!["start", &f, "1.4"]),
            (
                Action::Clear { id: "1.2".into() },
                vec!["look-clear", &f, "1.2"],
            ),
            (
                Action::Reject { id: "1.2".into() },
                vec!["look-clear", &f, "1.2", "--reject"],
            ),
            (
                Action::Defer {
                    id: "1.3".into(),
                    note: "eyeball".into(),
                },
                vec!["look-defer", &f, "1.3", "--note", "eyeball"],
            ),
        ];
        for (action, want) in &cases {
            assert_eq!(src.act(action).unwrap(), "✓ Task 1 marked as done.");
            let mut expect = vec!["--local".to_string()];
            expect.extend(want.iter().map(|s| s.to_string()));
            assert_eq!(src.runner.0.borrow().last().unwrap(), &expect);
        }
        src.owner = Some("mike".into());
        src.act(&cases[2].0).unwrap();
        assert_eq!(
            &src.runner.0.borrow().last().unwrap()[4..],
            ["--owner", "mike"]
        );
        let mut refused = LocalSource::new(
            Rec(
                RefCell::new(vec![]),
                Err("Error: task 1.1 is held by bot".into()),
            ),
            base.clone(),
        );
        assert_eq!(
            refused.act(&cases[0].0).unwrap_err(),
            "Error: task 1.1 is held by bot"
        );
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn parses_claim_age() {
        let t = parse_utc("2026-10-17T06:29:33Z").unwrap();
        let r = Running {
            claimed_at: Some("2026-10-17T06:29:33Z".into()),
            lease_expires_at: Some("2026-10-17T07:29:33Z".into()),
            ..Running::default()
        };
        assert_eq!(r.age_secs(t + 90), Some(90));
        assert_eq!(r.lease_left_secs(t), Some(3600));
        assert_eq!(r.lease_left_secs(t + 3601), Some(-1));
        assert_eq!(parse_utc("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(parse_utc("junk"), None);
    }

    #[test]
    fn data_layer_never_opens_for_writing() {
        // Guard for the single-writer rule: no write APIs in the non-test source.
        for src in [
            include_str!("source.rs"),
            include_str!("hub.rs"),
            include_str!("refresh.rs"),
        ] {
            let prod = src.split("#[cfg(test)]").next().unwrap();
            for banned in ["File::create", "fs::write", "OpenOptions", "remove_file"] {
                assert!(!prod.contains(banned), "{banned} in data layer");
            }
        }
    }
}
