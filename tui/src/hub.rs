//! Hub source: the same serve routes `tidetable --server` uses (remote.py `ROUTES`).
//!
//! The token only ever lives in the `Authorization` header of a request. It is never part of a
//! URL, an error string, a `Debug` print or a file.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::sync::mpsc::Sender;
#[cfg(feature = "hub")]
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

#[cfg(feature = "hub")]
pub struct UreqHttp {
    agent: ureq::Agent,
    base: String,
    token: Option<String>,
}

#[cfg(feature = "hub")]
impl std::fmt::Debug for UreqHttp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("UreqHttp")
            .field("base", &redact_url(&self.base))
            .field("token", &self.token.as_ref().map(|_| "<redacted>"))
            .finish()
    }
}

#[cfg(feature = "hub")]
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
                    (401 | 403, _) => {
                        format!("hub refused access (HTTP {code}); set --token or $TIDETABLE_TOKEN")
                    }
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

#[cfg(feature = "hub")]
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
        let get = |s: &str| self.http.get(&self.route(s)).map_err(SourceError::Cli);
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
        match action {
            Action::Done { id, changelog } => {
                let host = std::env::var("COMPUTERNAME")
                    .or_else(|_| std::env::var("HOSTNAME"))
                    .unwrap_or_else(|_| "tui".into());
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
                Ok(format!("done {id}"))
            }
            Action::Start { id } => {
                let body = json!({ "task": id, "owner": self.owner(), "force": false });
                self.http.post(&self.route("start"), &body)?;
                Ok(format!("started {id}"))
            }
            other => Err(format!(
                "the hub has no route for {} yet; run it against the checkout",
                other.verb()
            )),
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

#[cfg(feature = "hub")]
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

    #[cfg(feature = "hub")]
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
    fn done_posts_the_serve_body_and_other_actions_are_refused() {
        let mut hub = HubSource::new(
            Fixture {
                routes: HashMap::new(),
                posts: Mutex::new(vec![]),
            },
            "demo",
        )
        .with_owner(Some("tui".into()));
        hub.act(&Action::Done {
            id: "1.0.1".into(),
            changelog: Some(" shipped ".into()),
        })
        .unwrap();
        let posts = hub.http.posts.lock().unwrap();
        assert_eq!(posts[0].0, "/api/demo/done");
        assert_eq!(posts[0].1["task"], "1.0.1");
        assert_eq!(posts[0].1["owner"], "tui");
        assert_eq!(posts[0].1["changelog"], "shipped");
        drop(posts);
        assert!(hub
            .act(&Action::Clear { id: "1.0.1".into() })
            .unwrap_err()
            .contains("no route"));
    }

    #[test]
    fn only_data_lines_count_as_changes() {
        let (tx, rx) = std::sync::mpsc::channel();
        let body = ": heartbeat\n\nid: 1\nevent: done\ndata: {\"task\":\"1.0.1\"}\n\n: heartbeat\ndata: {}\n\n";
        assert_eq!(pump(body.as_bytes(), &tx), 2);
        assert_eq!(rx.try_iter().count(), 2);
    }
}
