//! The embeddable pane: drawn into a sub-rectangle, fed by a source inline or on its worker,
//! and taking input only while focused.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::Terminal;
use tidetable_tui::app::{Load, ModeArg, Tab};
use tidetable_tui::pane::{KeyOutcome, Pane};
use tidetable_tui::source::{
    Action, LookItem, PhaseProgress, Running, Snapshot, Source, SourceError, TaskRef,
};
use tidetable_tui::theme::Theme;

fn snap() -> Snapshot {
    Snapshot {
        project: "demo".into(),
        todo_file: "docs/todo.json".into(),
        in_progress: vec![Running {
            id: "1.1.1".into(),
            title: "Build the pane".into(),
            owner: Some("bot:1".into()),
            claimed_at: None,
            lease_expired: false,
        }],
        ready: vec![
            TaskRef {
                id: "1.1.2".into(),
                title: "Embed it".into(),
            },
            TaskRef {
                id: "1.1.3".into(),
                title: "Ship it".into(),
            },
        ],
        look_queue: vec![LookItem {
            id: "1.0.9".into(),
            title: "Look at it".into(),
            phase: "1.0".into(),
            note: String::new(),
            goal: String::new(),
            success: vec![],
        }],
        phases: vec![PhaseProgress {
            id: "1.1".into(),
            title: "Pane".into(),
            done: 1,
            total: 4,
            complete: false,
        }],
        ..Snapshot::default()
    }
}

fn theme() -> Theme {
    Theme::select(None, |_| None).0
}

fn text(buf: &Buffer, area: Rect) -> String {
    let mut s = String::new();
    for y in area.y..area.bottom() {
        for x in area.x..area.right() {
            s.push_str(buf[(x, y)].symbol());
        }
        s.push('\n');
    }
    s
}

fn key(c: KeyCode) -> KeyEvent {
    KeyEvent::new(c, KeyModifiers::NONE)
}

fn click(column: u16, row: u16) -> MouseEvent {
    MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column,
        row,
        modifiers: KeyModifiers::NONE,
    }
}

/// Answers from a script and records the actions it was asked to carry out.
#[derive(Clone, Default)]
struct Fake {
    fail: Option<&'static str>,
    acted: Arc<Mutex<Vec<Action>>>,
    chosen: Arc<Mutex<Vec<PathBuf>>>,
}

impl Source for Fake {
    fn snapshot(&mut self) -> Result<Snapshot, SourceError> {
        match self.fail {
            Some(m) => Err(SourceError::Cli(m.into())),
            None => Ok(snap()),
        }
    }
    fn act(&mut self, action: &Action) -> Result<String, String> {
        self.acted.lock().unwrap().push(action.clone());
        Ok("started".into())
    }
    fn choose(&mut self, file: PathBuf) {
        self.chosen.lock().unwrap().push(file);
    }
}

#[test]
fn the_pane_draws_only_inside_its_rect_and_hits_stay_there() {
    let mut pane = Pane::new(theme(), ModeArg::Auto);
    pane.set_snapshot(snap());
    let mut buf = Buffer::empty(Rect::new(0, 0, 80, 30));
    let area = Rect::new(44, 3, 34, 24);
    pane.render(area, &mut buf);
    let inside = text(&buf, area);
    assert!(
        inside.contains("CURRENT") && inside.contains("1.1.1"),
        "{inside}"
    );
    // Nothing leaks left of or above the rectangle.
    let left = text(&buf, Rect::new(0, 0, 44, 30));
    let above = text(&buf, Rect::new(0, 0, 80, 3));
    assert!(left.trim().is_empty() && above.trim().is_empty());
    assert_eq!(pane.area(), Some(area));
    for (r, _) in &pane.app().hits {
        assert!(area.contains(r.as_position()) && r.right() <= area.right());
    }
    // An area larger than the buffer is clipped instead of indexing out of range.
    pane.render(Rect::new(70, 25, 40, 40), &mut buf);
    assert_eq!(pane.area(), Some(Rect::new(70, 25, 10, 5)));
}

#[test]
fn the_standalone_shell_and_an_embedder_draw_the_same_widget() {
    let mut pane = Pane::new(theme(), ModeArg::Side);
    pane.set_snapshot(snap());
    // The binary's draw call: the pane over the whole terminal frame.
    let mut term = Terminal::new(TestBackend::new(36, 20)).unwrap();
    term.draw(|f| pane.render(f.area(), f.buffer_mut()))
        .unwrap();
    let whole = text(term.backend().buffer(), Rect::new(0, 0, 36, 20));
    // An embedder: the same size, offset inside a bigger buffer.
    let mut buf = Buffer::empty(Rect::new(0, 0, 100, 40));
    let area = Rect::new(60, 10, 36, 20);
    pane.render(area, &mut buf);
    assert_eq!(text(&buf, area), whole);
    assert!(whole.contains("REVIEW 1"), "{whole}");
}

#[test]
fn an_unreadable_source_is_a_one_line_notice_at_any_size() {
    let mut pane = Pane::new(theme(), ModeArg::Auto).with_source(Fake {
        fail: Some("tidetable: No such file or directory"),
        ..Fake::default()
    });
    pane.tick(Instant::now(), false);
    assert!(matches!(pane.load(), Load::Failed(_)));
    let area = Rect::new(0, 0, 36, 24);
    let mut buf = Buffer::empty(area);
    pane.render(area, &mut buf);
    let t = text(&buf, area);
    let used: Vec<&str> = t.lines().filter(|l| !l.trim().is_empty()).collect();
    assert_eq!(used.len(), 1, "{t}");
    assert!(used[0].contains("Could not read"), "{t}");
    // A notice set by the host directly, with a multi-line reason, at cramped sizes.
    pane.set_unavailable("no todo*.json found\nin ., docs");
    for (w, h) in [(36, 1), (12, 3), (1, 1), (80, 2), (200, 1)] {
        let area = Rect::new(0, 0, w, h);
        let mut buf = Buffer::empty(area);
        pane.render(area, &mut buf);
    }
    let area = Rect::new(0, 0, 58, 30);
    let mut buf = Buffer::empty(area);
    pane.render(area, &mut buf);
    let t = text(&buf, area);
    assert_eq!(t.lines().filter(|l| !l.trim().is_empty()).count(), 1, "{t}");
    assert!(t.contains("· no todo*.json found"), "{t}");
}

#[test]
fn keys_and_mouse_reach_the_pane_only_while_it_is_focused() {
    let mut pane = Pane::new(theme(), ModeArg::Standard);
    pane.set_snapshot(snap());
    let area = Rect::new(10, 0, 90, 30);
    let mut buf = Buffer::empty(Rect::new(0, 0, 100, 30));
    pane.render(area, &mut buf);
    assert!(!pane.focused());
    assert_eq!(pane.handle_key(key(KeyCode::Tab)), KeyOutcome::Ignored);
    assert_eq!(pane.app().tab, Tab::Now);
    assert!(!pane.handle_mouse(click(20, 10)));

    pane.set_focused(true);
    assert_eq!(pane.handle_key(key(KeyCode::Tab)), KeyOutcome::Consumed);
    assert_eq!(pane.app().tab, Tab::Upcoming);
    assert!(!pane.handle_mouse(click(2, 10)), "outside the pane");
    pane.render(area, &mut buf);
    let (tab_rect, _) = pane.app().hits[0];
    assert!(pane.handle_mouse(click(tab_rect.x, tab_rect.y)));
    assert_eq!(pane.app().tab, Tab::Now);
    // Leaving is the pane's quit keys; the host decides what that means.
    assert_eq!(pane.handle_key(key(KeyCode::Esc)), KeyOutcome::Release);
    assert_eq!(
        pane.handle_key(key(KeyCode::Char('q'))),
        KeyOutcome::Release
    );
    assert!(!pane.app().quit);
}

#[test]
fn read_only_hides_actions_and_a_confirmed_action_goes_through_the_source() {
    let fake = Fake::default();
    let mut pane = Pane::new(theme(), ModeArg::Standard).with_source(fake.clone());
    pane.set_focused(true);
    pane.tick(Instant::now(), false);
    assert_eq!(pane.load(), &Load::Ready);
    pane.handle_key(key(KeyCode::Char('2')));

    pane.set_read_only(true);
    pane.handle_key(key(KeyCode::Char('s')));
    assert!(pane.app().dialog.is_none());
    assert!(pane.app().hints().contains("read-only"));

    pane.set_read_only(false);
    pane.handle_key(key(KeyCode::Char('s')));
    assert!(pane.app().dialog.is_some());
    pane.handle_key(key(KeyCode::Enter));
    assert!(pane.tick(Instant::now(), false));
    assert_eq!(
        fake.acted.lock().unwrap().as_slice(),
        &[Action::Start { id: "1.1.2".into() }]
    );
    assert_eq!(pane.app().toast, Some(("started".into(), false)));
}

#[test]
fn a_background_source_fills_the_pane_without_blocking_tick() {
    let fake = Fake::default();
    let mut pane = Pane::new(theme(), ModeArg::Auto).with_background_source(fake.clone());
    assert_eq!(pane.load(), &Load::Loading);
    let deadline = Instant::now() + Duration::from_secs(5);
    while pane.load() != &Load::Ready && Instant::now() < deadline {
        pane.tick(Instant::now(), false);
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(pane.load(), &Load::Ready);
    assert_eq!(pane.app().snap.project, "demo");

    // The picker's answer reaches the worker's source.
    pane.app_mut().chosen = Some("docs/todo-b.json".into());
    pane.tick(Instant::now(), false);
    let deadline = Instant::now() + Duration::from_secs(5);
    while fake.chosen.lock().unwrap().is_empty() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        fake.chosen.lock().unwrap().as_slice(),
        &[PathBuf::from("docs/todo-b.json")]
    );
}

#[test]
fn a_background_failure_is_the_same_notice() {
    let mut pane = Pane::new(theme(), ModeArg::Auto).with_background_source(Fake {
        fail: Some("boom"),
        ..Fake::default()
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    while pane.load() == &Load::Loading && Instant::now() < deadline {
        pane.tick(Instant::now(), false);
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(pane.load(), &Load::Failed("tidetable failed: boom".into()));
}
