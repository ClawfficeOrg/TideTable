//! When to refresh, and what the connection looks like. Pure: callers pass `Instant`s, so the
//! timing rules are tested without sleeping.

use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime};

pub const LOCAL_POLL: Duration = Duration::from_secs(5);
pub const HUB_POLL: Duration = Duration::from_secs(2);
/// With a live stream the poll is only a safety net.
pub const STREAM_SAFETY: Duration = Duration::from_secs(30);
const BACKOFF: [u64; 5] = [2, 4, 8, 16, 30];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Conn {
    /// Reading files; nothing to show.
    #[default]
    Local,
    Live,
    Polling,
    Offline {
        retry_in: u64,
    },
    /// 401/403: retrying cannot help; fix the token or project scope and restart.
    Denied {
        forbidden: bool,
    },
}

/// Detects a todo file or its `.events.jsonl` changing on disk.
#[derive(Debug, Default)]
pub struct Watcher {
    paths: Vec<PathBuf>,
    seen: Vec<Option<SystemTime>>,
}

impl Watcher {
    /// Watch `todo` and `<todo>.events.jsonl`.
    pub fn for_todo(todo: &str) -> Self {
        let paths = vec![
            PathBuf::from(todo),
            PathBuf::from(format!("{todo}.events.jsonl")),
        ];
        let seen = paths.iter().map(mtime).collect();
        Self { paths, seen }
    }

    pub fn is_for(&self, todo: &str) -> bool {
        self.paths
            .first()
            .is_some_and(|p| p == &PathBuf::from(todo))
    }

    pub fn changed(&mut self) -> bool {
        let now: Vec<_> = self.paths.iter().map(mtime).collect();
        let changed = now != self.seen;
        self.seen = now;
        changed
    }
}

fn mtime(p: &PathBuf) -> Option<SystemTime> {
    std::fs::metadata(p).and_then(|m| m.modified()).ok()
}

#[derive(Debug)]
pub struct Refresher {
    hub: bool,
    stream: bool,
    last: Instant,
    fails: usize,
    retry_at: Option<Instant>,
    denied: Option<bool>,
}

impl Refresher {
    pub fn new(hub: bool, now: Instant) -> Self {
        // `last` is `now`: the first snapshot is taken explicitly before the loop.
        Self {
            hub,
            stream: false,
            last: now,
            fails: 0,
            retry_at: None,
            denied: None,
        }
    }

    /// Stop for good: a 401/403 is not transient.
    pub fn deny(&mut self, forbidden: bool) {
        self.denied = Some(forbidden);
        self.retry_at = None;
    }

    pub fn set_stream(&mut self, live: bool) {
        self.stream = live;
    }

    /// `pushed`: a stream event or a file-change notice arrived since the last call.
    pub fn due(&self, now: Instant, pushed: bool) -> bool {
        if self.denied.is_some() {
            return false;
        }
        if let Some(at) = self.retry_at {
            return now >= at;
        }
        if pushed {
            return true;
        }
        let every = match (self.hub, self.stream) {
            (false, _) => LOCAL_POLL,
            (true, false) => HUB_POLL,
            (true, true) => STREAM_SAFETY,
        };
        now.duration_since(self.last) >= every
    }

    pub fn record(&mut self, ok: bool, now: Instant) {
        self.last = now;
        if ok {
            self.fails = 0;
            self.retry_at = None;
            self.denied = None;
        } else {
            let wait = BACKOFF[self.fails.min(BACKOFF.len() - 1)];
            self.fails += 1;
            self.retry_at = Some(now + Duration::from_secs(wait));
        }
    }

    pub fn conn(&self, now: Instant) -> Conn {
        if let Some(forbidden) = self.denied {
            return Conn::Denied { forbidden };
        }
        if let Some(at) = self.retry_at {
            let left = at.saturating_duration_since(now);
            return Conn::Offline {
                retry_in: left.as_secs() + u64::from(left.subsec_nanos() > 0),
            };
        }
        match (self.hub, self.stream) {
            (false, _) => Conn::Local,
            (true, true) => Conn::Live,
            (true, false) => Conn::Polling,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn secs(n: u64) -> Duration {
        Duration::from_secs(n)
    }

    #[test]
    fn local_polls_every_five_seconds_unless_a_change_is_pushed() {
        let t0 = Instant::now();
        let r = Refresher::new(false, t0);
        assert!(!r.due(t0 + secs(4), false));
        assert!(r.due(t0 + secs(5), false));
        assert!(r.due(t0 + secs(1), true));
        assert_eq!(r.conn(t0), Conn::Local);
    }

    #[test]
    fn hub_polls_every_two_seconds_without_a_stream_and_rarely_with_one() {
        let t0 = Instant::now();
        let mut r = Refresher::new(true, t0);
        assert!(!r.due(t0 + secs(1), false) && r.due(t0 + secs(2), false));
        assert_eq!(r.conn(t0), Conn::Polling);
        r.set_stream(true);
        assert!(!r.due(t0 + secs(10), false));
        assert!(
            r.due(t0 + secs(10), true),
            "a stream event refreshes at once"
        );
        assert_eq!(r.conn(t0), Conn::Live);
    }

    #[test]
    fn failures_back_off_with_a_countdown_and_recover() {
        let t0 = Instant::now();
        let mut r = Refresher::new(true, t0);
        r.record(false, t0);
        assert_eq!(r.conn(t0), Conn::Offline { retry_in: 2 });
        assert!(
            !r.due(t0 + secs(1), true),
            "no hammering an offline hub, even on events"
        );
        assert!(r.due(t0 + secs(2), false));
        r.record(false, t0 + secs(2));
        assert_eq!(r.conn(t0 + secs(2)), Conn::Offline { retry_in: 4 });
        assert_eq!(r.conn(t0 + secs(3)), Conn::Offline { retry_in: 3 });
        for i in 0..10 {
            r.record(false, t0 + secs(10 + i));
        }
        assert_eq!(
            r.conn(t0 + secs(19)),
            Conn::Offline { retry_in: 30 },
            "capped"
        );
        r.record(true, t0 + secs(60));
        assert_eq!(r.conn(t0 + secs(60)), Conn::Polling);
    }

    #[test]
    fn auth_denial_stops_the_retry_loop_until_a_later_success() {
        let t0 = Instant::now();
        let mut r = Refresher::new(true, t0);
        r.deny(false);
        assert_eq!(r.conn(t0), Conn::Denied { forbidden: false });
        assert!(!r.due(t0 + secs(600), true), "no retry after 401");
        r.deny(true);
        assert_eq!(r.conn(t0), Conn::Denied { forbidden: true });
        r.record(true, t0);
        assert_eq!(r.conn(t0), Conn::Polling);
        assert!(r.due(t0 + secs(2), false));
    }

    #[test]
    fn watcher_sees_todo_and_event_log_changes() {
        let dir = std::env::temp_dir().join(format!("tt-watch-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let todo = dir.join("todo.json");
        std::fs::write(&todo, "{}").unwrap();
        let name = todo.to_string_lossy().into_owned();
        let mut w = Watcher::for_todo(&name);
        assert!(!w.changed());
        let log = dir.join("todo.json.events.jsonl");
        std::fs::write(&log, "{}\n").unwrap();
        assert!(w.changed());
        assert!(!w.changed());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
