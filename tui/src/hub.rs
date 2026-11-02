//! Hub source: the same serve routes `tidetable --server` uses (remote.py `ROUTES`).
//!
//! The token only ever lives in the `Authorization` header of a request. It is never part of a
//! URL, an error string, a `Debug` print or a file.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::sync::mpsc::Sender;
use std::time::Duration;

use serde_json::{json, Value};

use crate::source::{build_snapshot, changelog_of, ids_of, Action, Snapshot, Source, SourceError};

/// One round trip to the hub. A trait so tests serve fixtures without a socket.
pub trait Http {
    fn get(&self, path: &str) -> Result<Value, String>;
    fn post(&self, path: &str, body: &Value) -> Result<Value, String>;
}

/// `https://user:pw@host/x` -> `https://host/x`; credentials in a URL must not reach a frame.
pub fn redact_url(url: &str) -> String {
    match url.split_once("://") {
        Some((scheme, rest)) => {
            let (auth, tail) = rest
                .split_once('/')
                .map_or((rest, None), |(a, t)| (a, Some(t)));
            let host = auth.rsplit_once('@').map_or(auth, |(_, h)| h);
            match tail {
                Some(t) => format!("{scheme}://{host}/{t}"),
                None => format!("{scheme}://{host}"),
            }
        }
        None => url.to_string(),
    }
}

fn pct(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

pub struct UreqHttp {
    agent: ureq::Agent,
    base: String,
    token: Option<String>,
}

impl std::fmt::Debug for UreqHttp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("UreqHttp")
            .field("base", &redact_url(&self.base))
            .field("token", &self.token.as_ref().map(|_| "<redacted>"))
            .finish()
    }
}

impl UreqHttp {
    pub fn new(base: &str, token: Option<String>) -> Result<Self, String> {
        let base = base.trim().trim_end_matches('/').to_string();
        if !(base.starts_with("http://") || base.starts_with("https://")) {
            return Err(format!(
                "server must be an http(s) URL, got {:?}",
                redact_url(&base)
            ));
        }
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .build();
        Ok(Self {
            agent,
            base,
            token: token.filter(|t| !t.trim().is_empty()),
        })
    }

    fn req(&self, method: &str, path: &str) -> ureq::Request {
        let r = self
            .agent
            .request(method, &format!("{}{}", self.base, path))
            .set("Accept", "application/json");
        match &self.token {
            Some(t) => r.set("Authorization", &format!("Bearer {}", t.trim())),
            None => r,
        }
    }

    fn fail(&self, e: ureq::Error) -> String {
        match e {
            ureq::Error::Status(code, resp) => {
                let msg = resp
                    .into_json::<Value>()
                    .ok()
                    .and_then(|v| v.get("error").and_then(Value::as_str).map(str::to_string));
                match (code, msg) {
                    (401, _) => format!(
                        "{AUTH_401} token rejected (HTTP 401); set --token or $TIDETABLE_TOKEN"
                    ),
                    (403, _) => format!(
                        "{AUTH_403} no access to this project (HTTP 403); ask for a token scoped to it"
                    ),
                    (_, Some(m)) => m,
                    (c, None) => format!("hub answered HTTP {c}"),
                }
            }
            // ureq's transport errors name the URL, never the headers; redact URL credentials anyway.
            ureq::Error::Transport(t) => format!(
                "cannot reach {}: {}",
                redact_url(&self.base),
                redact_url(&t.to_string())
            ),
        }
    }

    /// Open the event stream; `Err` also when the hub does not offer one.
    pub fn open_stream(&self, slug: &str) -> Result<Box<dyn std::io::Read + Send + Sync>, String> {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(5))
            .timeout_read(Duration::from_secs(45))
            .build();
        let url = format!("{}/api/stream?slugs={}", self.base, pct(slug));
        let mut r = agent.get(&url).set("Accept", "text/event-stream");
        if let Some(t) = &self.token {
            r = r.set("Authorization", &format!("Bearer {}", t.trim()));
        }
        let resp = r.call().map_err(|e| self.fail(e))?;
        if !resp.content_type().contains("event-stream") {
            return Err("hub has no event stream".into());
        }
        Ok(resp.into_reader())
    }
}

impl Http for UreqHttp {
    fn get(&self, path: &str) -> Result<Value, String> {
        let resp = self.req("GET", path).call().map_err(|e| self.fail(e))?;
        resp.into_json()
            .map_err(|e| format!("hub sent unreadable JSON: {e}"))
    }

    fn post(&self, path: &str, body: &Value) -> Result<Value, String> {
        let resp = self
            .req("POST", path)
            .send_json(body.clone())
            .map_err(|e| self.fail(e))?;
        resp.into_json()
            .map_err(|e| format!("hub sent unreadable JSON: {e}"))
    }
}

/// Projects the calling token may open: `[(slug, name)]` from `GET /api/projects`.
///
/// The hub already drops slugs a scoped token cannot see; the TUI
/// only turns the payload into picker rows.
pub fn list_projects<H: Http>(http: &H) -> Result<Vec<(String, String)>, String> {
    let payload = http.get("/api/projects")?;
    let rows = payload
        .get("projects")
        .and_then(Value::as_array)
        .ok_or_else(|| "hub sent no project list".to_string())?;
    Ok(rows
        .iter()
        .filter_map(|p| {
            let slug = p.get("slug").and_then(Value::as_str)?.trim();
            if slug.is_empty() {
                return None;
            }
            let name = p
                .get("name")
                .or_else(|| p.get("title"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|n| !n.is_empty())
                .unwrap_or(slug);
            Some((slug.to_string(), name.to_string()))
        })
        .collect())
}

/// Prefixes `UreqHttp::fail` puts on auth refusals so `HubSource` can turn them
/// into [`SourceError::Auth`] without matching prose.
pub const AUTH_401: &str = "auth401:";
pub const AUTH_403: &str = "auth403:";

/// Turn an HTTP error string into the source-facing error, keeping 401 and 403 apart.
fn hub_error(message: String) -> SourceError {
    if let Some(rest) = message.strip_prefix(AUTH_401) {
        return SourceError::Auth {
            forbidden: false,
            message: rest.trim().to_string(),
        };
    }
    if let Some(rest) = message.strip_prefix(AUTH_403) {
        return SourceError::Auth {
            forbidden: true,
            message: rest.trim().to_string(),
        };
    }
    SourceError::Cli(message)
}

pub struct HubSource<H: Http> {
    http: H,
    slug: String,
    owner: Option<String>,
}

impl<H: Http> HubSource<H> {
    pub fn new(http: H, slug: impl Into<String>) -> Self {
        Self {
            http,
            slug: slug.into(),
            owner: None,
        }
    }

    pub fn with_owner(mut self, owner: Option<String>) -> Self {
        self.owner = owner;
        self
    }

    fn route(&self, suffix: &str) -> String {
        format!("/api/{}/{suffix}", pct(&self.slug))
    }

    fn owner(&self) -> String {
        self.owner
            .clone()
            .or_else(|| {
                std::env::var("TIDETABLE_OWNER")
                    .ok()
                    .filter(|o| !o.is_empty())
            })
            .unwrap_or_else(|| {
                let host = std::env::var("COMPUTERNAME")
                    .or_else(|_| std::env::var("HOSTNAME"))
                    .unwrap_or_else(|_| "tui".into());
                format!("{host}:{}", std::process::id())
            })
    }
}

impl<H: Http> Source for HubSource<H> {
    fn snapshot(&mut self) -> Result<Snapshot, SourceError> {
        let get = |s: &str| self.http.get(&self.route(s)).map_err(hub_error);
        let list = get("list")?;
        let ready = get("ready")?;
        let disjoint = get("ready?disjoint=1")?;
        let look = get("look-queue")?;
        let history = get("history?limit=50")?;
        let mut snap = build_snapshot(
            &PathBuf::from(format!("{}.json", self.slug)),
            &list,
            &ready,
            &look,
        )?;
        snap.project = self.slug.clone();
        snap.todo_file = self.slug.clone();
        snap.disjoint = ids_of(disjoint.get("ready"));
        snap.changelog = changelog_of(&history);
        Ok(snap)
    }

    fn act(&mut self, action: &Action) -> Result<String, String> {
        let host = std::env::var("COMPUTERNAME")
            .or_else(|_| std::env::var("HOSTNAME"))
            .unwrap_or_else(|_| "tui".into());
        match action {
            Action::Done { id, changelog } => {
                let mut body =
                    json!({ "task": id, "owner": self.owner(), "force": false, "host": host });
                if let Some(c) = changelog
                    .as_deref()
                    .map(str::trim)
                    .filter(|c| !c.is_empty())
                {
                    body["changelog"] = json!(c);
                }
                self.http.post(&self.route("done"), &body)?;
                Ok(format!("✓ Task {id} marked as done."))
            }
            Action::Start { id } => {
                let body =
                    json!({ "task": id, "owner": self.owner(), "force": false, "host": host });
                let resp = self.http.post(&self.route("start"), &body)?;
                let lease = resp
                    .get("lease_expires_at")
                    .and_then(Value::as_str)
                    .unwrap_or("?");
                let verb = if resp
                    .get("refreshed")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                {
                    "lease refreshed"
                } else {
                    "started"
                };
                Ok(format!(
                    "► Task {id} {verb} by {}, lease to {lease}.",
                    self.owner()
                ))
            }
            Action::Defer { id, note } => {
                let mut body =
                    json!({ "task": id, "owner": self.owner(), "force": false, "host": host });
                if !note.trim().is_empty() {
                    body["note"] = json!(note.trim());
                }
                let resp = self.http.post(&self.route("look-defer"), &body)?;
                let from = resp.get("from").and_then(Value::as_str).unwrap_or("?");
                Ok(format!("◐ Task {id} queued for a human look (was {from})."))
            }
            Action::Clear { id } | Action::Reject { id } => {
                let reject = matches!(action, Action::Reject { .. });
                let mut body =
                    json!({ "task": id, "owner": self.owner(), "force": false, "host": host });
                if reject {
                    body["reject"] = json!(true);
                }
                let resp = self.http.post(&self.route("look-clear"), &body)?;
                let to = resp.get("to").and_then(Value::as_str).unwrap_or("done");
                let icon = if to == "done" { "✓" } else { "·" };
                Ok(format!("{icon} Task {id} cleared: {to}."))
            }
        }
    }
}

/// What the event-stream thread tells the main loop.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StreamMsg {
    Connected,
    /// A `data:` frame arrived: something changed.
    Changed,
    Disconnected,
    /// The hub offers no stream; fall back to polling for good.
    Unsupported,
    /// The hub refused the credential or the project; stop, do not reconnect.
    Denied {
        forbidden: bool,
    },
}

/// SSE comments (`: heartbeat`) and `id:`/`event:` lines are not changes; only `data:` is.
pub fn is_change_line(line: &str) -> bool {
    line.starts_with("data:")
}

/// Read frames until the stream ends; returns how many changes were seen.
pub fn pump(reader: impl std::io::Read, tx: &Sender<StreamMsg>) -> usize {
    let mut n = 0;
    for line in BufReader::new(reader).lines() {
        let Ok(line) = line else { break };
        if is_change_line(&line) {
            n += 1;
            if tx.send(StreamMsg::Changed).is_err() {
                break;
            }
        }
    }
    n
}

/// Background reader. Reconnects with a growing pause; a hub without a stream ends the thread.
pub fn spawn_stream(http: UreqHttp, slug: String, tx: Sender<StreamMsg>) {
    std::thread::spawn(move || {
        let mut pause = 2u64;
        loop {
            match http.open_stream(&slug) {
                Ok(r) => {
                    if tx.send(StreamMsg::Connected).is_err() {
                        return;
                    }
                    pause = 2;
                    pump(r, &tx);
                    if tx.send(StreamMsg::Disconnected).is_err() {
                        return;
                    }
                }
                Err(e) if e == "hub has no event stream" => {
                    let _ = tx.send(StreamMsg::Unsupported);
                    return;
                }
                Err(e) if e.starts_with(AUTH_401) || e.starts_with(AUTH_403) => {
                    let _ = tx.send(StreamMsg::Denied {
                        forbidden: e.starts_with(AUTH_403),
                    });
                    return;
                }
                Err(_) => {
                    if tx.send(StreamMsg::Disconnected).is_err() {
                        return;
                    }
                }
            }
            std::thread::sleep(Duration::from_secs(pause));
            pause = (pause * 2).min(30);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;

    struct Fixture {
        routes: HashMap<String, Value>,
        posts: Mutex<Vec<(String, Value)>>,
    }

    impl Http for Fixture {
        fn get(&self, path: &str) -> Result<Value, String> {
            self.routes
                .get(path)
                .cloned()
                .ok_or_else(|| format!("no fixture for {path}"))
        }
        fn post(&self, path: &str, body: &Value) -> Result<Value, String> {
            self.posts
                .lock()
                .unwrap()
                .push((path.to_string(), body.clone()));
            Ok(json!({"ok": true}))
        }
    }

    #[test]
    fn url_credentials_are_redacted() {
        assert_eq!(
            redact_url("https://bob:pw@hub.example/api"),
            "https://hub.example/api"
        );
        assert_eq!(redact_url("http://hub:8080"), "http://hub:8080");
    }

    #[test]
    fn debug_and_errors_never_carry_the_token() {
        let h = UreqHttp::new("http://127.0.0.1:1", Some("sekret-token".into())).unwrap();
        assert!(!format!("{h:?}").contains("sekret-token"));
        let e = h.get("/api/x/list").unwrap_err();
        assert!(!e.contains("sekret-token"), "{e}");
        assert!(UreqHttp::new("ftp://x", Some("sekret-token".into()))
            .unwrap_err()
            .contains("http(s)"));
    }

    #[test]
    fn routes_are_the_serve_routes_and_slugs_are_encoded() {
        let mut hub = HubSource::new(
            Fixture {
                routes: HashMap::new(),
                posts: Mutex::new(vec![]),
            },
            "a b",
        );
        assert_eq!(hub.route("list"), "/api/a%20b/list");
        let err = hub.snapshot().unwrap_err().to_string();
        assert!(err.contains("/api/a%20b/list"), "{err}");
    }

    #[test]
    fn every_action_posts_its_serve_route_and_body() {
        let mut hub = HubSource::new(
            Fixture {
                routes: HashMap::new(),
                posts: Mutex::new(vec![]),
            },
            "demo",
        )
        .with_owner(Some("tui".into()));
        assert_eq!(
            hub.act(&Action::Start { id: "1.0.1".into() }).unwrap(),
            "► Task 1.0.1 started by tui, lease to ?."
        );
        assert_eq!(
            hub.act(&Action::Done {
                id: "1.0.1".into(),
                changelog: Some(" shipped ".into()),
            })
            .unwrap(),
            "✓ Task 1.0.1 marked as done."
        );
        assert_eq!(
            hub.act(&Action::Defer {
                id: "1.0.1".into(),
                note: " eyeball ".into(),
            })
            .unwrap(),
            "◐ Task 1.0.1 queued for a human look (was ?)."
        );
        assert_eq!(
            hub.act(&Action::Clear { id: "1.0.1".into() }).unwrap(),
            "✓ Task 1.0.1 cleared: done."
        );
        assert_eq!(
            hub.act(&Action::Reject { id: "1.0.1".into() }).unwrap(),
            "✓ Task 1.0.1 cleared: done."
        );
        let posts = hub.http.posts.lock().unwrap();
        let paths: Vec<&str> = posts.iter().map(|(p, _)| p.as_str()).collect();
        assert_eq!(
            paths,
            vec![
                "/api/demo/start",
                "/api/demo/done",
                "/api/demo/look-defer",
                "/api/demo/look-clear",
                "/api/demo/look-clear",
            ]
        );
        for (_, body) in posts.iter() {
            assert_eq!(body["task"], "1.0.1");
            assert_eq!(body["owner"], "tui");
            assert_eq!(body["force"], false);
            assert!(body["host"].is_string());
        }
        assert_eq!(posts[1].1["changelog"], "shipped");
        assert_eq!(posts[2].1["note"], "eyeball");
        assert!(posts[3].1.get("reject").is_none());
        assert_eq!(posts[4].1["reject"], true);
    }

    #[test]
    fn project_list_keeps_names_and_skips_blank_slugs() {
        let mut routes = HashMap::new();
        routes.insert(
            "/api/projects".to_string(),
            json!({"projects": [
                {"slug": "demo", "name": "Demo"},
                {"slug": "other"},
                {"slug": " "}
            ]}),
        );
        let projects = list_projects(&Fixture {
            routes,
            posts: Mutex::new(vec![]),
        })
        .unwrap();
        assert_eq!(
            projects,
            vec![
                ("demo".to_string(), "Demo".to_string()),
                ("other".to_string(), "other".to_string())
            ]
        );
    }

    #[test]
    fn auth_errors_stay_distinct_and_name_the_fix() {
        let unauthorized = hub_error(format!(
            "{AUTH_401} token rejected (HTTP 401); set --token or $TIDETABLE_TOKEN"
        ));
        assert!(matches!(
            unauthorized,
            SourceError::Auth {
                forbidden: false,
                ..
            }
        ));
        let forbidden = hub_error(format!(
            "{AUTH_403} no access to this project (HTTP 403); ask for a token scoped to it"
        ));
        assert!(matches!(
            forbidden,
            SourceError::Auth {
                forbidden: true,
                ..
            }
        ));
        assert!(unauthorized.to_string().contains("TIDETABLE_TOKEN"));
        assert!(forbidden.to_string().contains("scoped"));
    }

    fn one_shot_http(status: &str) -> (String, std::thread::JoinHandle<()>) {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let status = status.to_string();
        let handle = std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut buf = [0u8; 1024];
            let _ = sock.read(&mut buf);
            let _ = sock.write_all(
                format!("HTTP/1.1 {status}\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}")
                    .as_bytes(),
            );
        });
        (format!("http://{addr}"), handle)
    }

    #[test]
    fn http_401_and_403_never_echo_the_token() {
        for (status, want) in [
            ("401 Unauthorized", "token rejected"),
            ("403 Forbidden", "no access"),
        ] {
            let (base, server) = one_shot_http(status);
            let http = UreqHttp::new(&base, Some("sekret-token".into())).unwrap();
            let err = http.get("/api/projects").unwrap_err();
            assert!(err.contains(want), "{err}");
            assert!(!err.contains("sekret-token"), "{err}");
            server.join().unwrap();
        }
    }

    #[test]
    fn only_data_lines_count_as_changes() {
        let (tx, rx) = std::sync::mpsc::channel();
        let body = ": heartbeat\n\nid: 1\nevent: done\ndata: {\"task\":\"1.0.1\"}\n\n: heartbeat\ndata: {}\n\n";
        assert_eq!(pump(body.as_bytes(), &tx), 2);
        assert_eq!(rx.try_iter().count(), 2);
    }
}
