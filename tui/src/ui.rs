//! Drawing. Everything colours through `Theme`; regions are surfaces, not boxes, and only the
//! focused list gets a border.

use std::sync::OnceLock;

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, BorderType, Clear, Padding, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState,
    Tabs, Wrap,
};
use ratatui::widgets::{StatefulWidget, Widget};
use ratatui::Frame;

use crate::app::{App, Divider, Entry, Hit, Kind, Load, Mode, Tab};
use crate::source::PhaseProgress;
use crate::theme::{Depth, Status};

const SPRITE: &str = include_str!("../assets/clock-crab.json");
/// Ink 950 and Mist 50 from the Clawffice palettes: sprite details that do not follow a theme.
const INK: (u8, u8, u8) = (0x03, 0x0C, 0x11);
const MIST: (u8, u8, u8) = (0xE9, 0xFB, 0xF6);

fn sprite_rows() -> &'static Vec<Vec<char>> {
    static ROWS: OnceLock<Vec<Vec<char>>> = OnceLock::new();
    ROWS.get_or_init(|| {
        let v: serde_json::Value =
            serde_json::from_str(SPRITE).expect("vendored sprite is valid JSON");
        v["rows"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|r| r.as_str().unwrap_or("").chars().collect())
                    .collect()
            })
            .unwrap_or_default()
    })
}

/// Cut to `w` columns with an ellipsis.
pub fn trunc(s: &str, w: usize) -> String {
    if s.chars().count() <= w {
        s.to_string()
    } else if w == 0 {
        String::new()
    } else {
        let mut out: String = s.chars().take(w - 1).collect();
        out.push('…');
        out
    }
}

fn bar(done: usize, total: usize, width: usize) -> String {
    let filled = (done * width + total / 2).checked_div(total).unwrap_or(0);
    format!(
        "{}{}",
        "█".repeat(filled.min(width)),
        "░".repeat(width.saturating_sub(filled))
    )
}

fn current_phase(app: &App) -> Option<&PhaseProgress> {
    app.snap
        .phases
        .iter()
        .find(|p| !p.complete)
        .or(app.snap.phases.last())
}

pub fn wordmark(app: &App) -> Vec<Span<'static>> {
    // Rule 3: the product hue highlights the "table" syllable.
    vec![
        Span::styled("tide", app.theme.strong()),
        Span::styled("table", app.theme.accent_style()),
    ]
}

/// The drawing code's view of a [`Buffer`]: the two `Frame` calls it uses, so the same code
/// renders into a whole terminal or into one `Rect` of an embedder's buffer.
pub struct Canvas<'a> {
    buf: &'a mut Buffer,
}

impl<'a> Canvas<'a> {
    pub fn new(buf: &'a mut Buffer) -> Self {
        Self { buf }
    }

    fn render_widget(&mut self, w: impl Widget, area: Rect) {
        w.render(area, self.buf);
    }

    fn render_stateful_widget<W: StatefulWidget>(
        &mut self,
        w: W,
        area: Rect,
        state: &mut W::State,
    ) {
        w.render(area, self.buf, state);
    }

    fn buffer_mut(&mut self) -> &mut Buffer {
        self.buf
    }
}

/// The whole terminal frame; the standalone binary's entry point.
pub fn draw(f: &mut Frame, app: &mut App) {
    let area = f.area();
    render(area, f.buffer_mut(), app);
}

/// Draw the app into `area` of `buf`. Hit rectangles are in `buf` coordinates, so an embedder
/// passes mouse events through unchanged. `area` is clipped to the buffer first.
pub fn render(area: Rect, buf: &mut Buffer, app: &mut App) {
    let area = area.intersection(buf.area);
    let f = &mut Canvas::new(buf);
    app.hits.clear();
    app.divider = None;
    f.render_widget(Block::new().style(app.theme.base()), area);
    if area.width < 12 || area.height < 3 {
        // Too small for any layout: say who we are and nothing else.
        let t = trunc("tidetable", area.width as usize);
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(t, app.theme.dim()))),
            area,
        );
        return;
    }
    match Mode::resolve(app.mode_arg, area.width, area.height) {
        Mode::Standard => draw_standard(f, app, area),
        Mode::Side => draw_side(f, app, area),
        Mode::Bottom => draw_bottom(f, app, area),
    }
    if app.is_offline() {
        // Stale data stays readable but visibly dim; the label says why.
        let body = Rect::new(area.x, area.y, area.width, area.height - 1);
        f.buffer_mut()
            .set_style(body, Style::new().add_modifier(Modifier::DIM));
        draw_offline(f, app, area);
    }
    draw_toast(f, app, area);
    draw_dialog(f, app, area);
}

/// Offline is the one state every mode must show, so it owns the bottom row's right edge.
fn draw_offline(f: &mut Canvas<'_>, app: &App, area: Rect) {
    let Some((st, text)) = app.conn_label() else {
        return;
    };
    let label = format!(" {} {text} ", st.icon());
    let w = (label.chars().count() as u16).min(area.width);
    let row = Rect::new(area.x + area.width - w, area.y + area.height - 1, w, 1);
    f.render_widget(
        Paragraph::new(Span::styled(
            trunc(&label, w as usize),
            app.theme.status_style(st).add_modifier(Modifier::BOLD),
        ))
        .style(app.theme.panel()),
        row,
    );
}

/// The last action's result over the bottom row; a refusal keeps the CLI's own words.
fn draw_toast(f: &mut Canvas<'_>, app: &App, area: Rect) {
    let Some((msg, err)) = &app.toast else { return };
    let row = Rect::new(area.x, area.y + area.height - 1, area.width, 1);
    let st = if *err { Status::Blocked } else { Status::Done };
    let text = trunc(
        &format!("{} {}", st.icon(), msg.replace('\n', " ")),
        area.width as usize,
    );
    f.render_widget(
        Paragraph::new(Span::styled(
            text,
            app.theme.status_style(st).add_modifier(Modifier::BOLD),
        ))
        .style(app.theme.panel()),
        row,
    );
}

/// Sized to content, anchored under the selected row, list left visible around it.
fn draw_dialog(f: &mut Canvas<'_>, app: &App, area: Rect) {
    let Some(d) = &app.dialog else { return };
    let w = area.width.saturating_sub(2).clamp(1, 60);
    let inner = w.saturating_sub(4) as usize;
    let mut lines: Vec<Line> = d
        .body
        .lines()
        .map(|l| Line::from(Span::styled(trunc(l, inner), app.theme.base())))
        .collect();
    if let Some((label, v)) = &d.input {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            trunc(&format!("{label}:"), inner),
            app.theme.dim(),
        )));
        let width = inner.saturating_sub(3);
        let scroll = v.visual_scroll(width);
        let shown: String = v.value().chars().skip(scroll).take(width).collect();
        lines.push(Line::from(vec![
            Span::styled(format!("> {shown}"), app.theme.strong()),
            Span::styled("▏", app.theme.accent_style()),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "Enter confirm · Esc cancel",
        app.theme.dim(),
    )));
    let h = (lines.len() as u16 + 2).min(area.height);
    let want = Hit::Row(app.selected_index(app.tab));
    let anchor = app
        .hits
        .iter()
        .find(|(_, hit)| *hit == want)
        .map(|(r, _)| *r);
    let y = match anchor {
        Some(r) if r.y + r.height + h <= area.y + area.height => r.y + r.height,
        Some(r) if r.y >= area.y + h => r.y - h,
        _ => area.y + area.height.saturating_sub(h) / 2,
    };
    let x = anchor
        .map_or(area.x, |r| r.x)
        .min(area.x + area.width.saturating_sub(w));
    let rect = Rect::new(x, y, w, h);
    f.render_widget(Clear, rect);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(app.theme.border_style(true))
        .title(Span::styled(
            format!(" {} ", trunc(&d.title, inner)),
            app.theme.strong(),
        ))
        .style(app.theme.panel())
        .padding(Padding::horizontal(1));
    f.render_widget(Paragraph::new(lines).block(block), rect);
}

// ---------------------------------------------------------------- standard

/// Below this width the detail panel goes under the list instead of beside it.
const SIDE_BY_SIDE_MIN: u16 = 110;

fn draw_standard(f: &mut Canvas<'_>, app: &mut App, area: Rect) {
    let big = area.width >= 40 && area.height >= 24 && app.theme.depth != Depth::Mono;
    let head_h = if big { 6 } else { 2 };
    let [head, tabs, body, foot] = Layout::vertical([
        Constraint::Length(head_h),
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(1),
    ])
    .areas(area);

    draw_header(f, app, head, big);
    draw_tabs(f, app, tabs);

    if app.load != Load::Ready {
        draw_state(f, app, body);
    } else if body.width >= SIDE_BY_SIDE_MIN {
        let [list, detail] =
            Layout::horizontal([Constraint::Percentage(app.split), Constraint::Fill(1)])
                .spacing(1)
                .areas(body);
        app.divider = Some(Divider {
            grab: Rect::new(list.right().saturating_sub(1), body.y, 3, body.height),
            body,
            vertical: false,
        });
        draw_list(f, app, list, app.tab, app.focused);
        draw_detail(f, app, detail);
    } else if body.height >= 16 {
        let [list, detail] =
            Layout::vertical([Constraint::Percentage(app.split), Constraint::Fill(1)]).areas(body);
        app.divider = Some(Divider {
            grab: Rect::new(body.x, list.bottom().saturating_sub(1), body.width, 2),
            body,
            vertical: true,
        });
        draw_list(f, app, list, app.tab, app.focused);
        draw_detail(f, app, detail);
    } else {
        draw_list(f, app, body, app.tab, app.focused);
    }

    let hints = app.hints();
    let label = if app.is_offline() {
        None
    } else {
        app.conn_label()
    };
    let lw = label
        .as_ref()
        .map_or(0, |(_, t)| t.chars().count() as u16 + 3);
    let [hint_area, conn_area] = Layout::horizontal([
        Constraint::Fill(1),
        Constraint::Length(if foot.width > lw + 24 { lw } else { 0 }),
    ])
    .areas(foot);
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            trunc(&hints, hint_area.width as usize),
            app.theme.dim(),
        ))),
        hint_area,
    );
    if let (Some((st, t)), true) = (label, conn_area.width > 0) {
        f.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(format!("{} ", st.icon()), app.theme.status_style(st)),
                Span::styled(t, app.theme.status_style(st)),
            ])),
            conn_area,
        );
    }
}

fn draw_header(f: &mut Canvas<'_>, app: &App, area: Rect, big: bool) {
    let mut text_area = area;
    if big {
        let [art, rest] =
            Layout::horizontal([Constraint::Length(13), Constraint::Fill(1)]).areas(area);
        draw_sprite(f, app, art);
        text_area = rest;
    }
    let mut lines = vec![Line::from(wordmark(app))];
    let proj = format!(
        "{}  ·  {}",
        app.snap.project,
        if app.snap.todo_file.is_empty() {
            "…".into()
        } else {
            app.snap.todo_file.clone()
        }
    );
    lines.push(Line::from(Span::styled(
        trunc(&proj, text_area.width as usize),
        app.theme.dim(),
    )));
    if big {
        if let Some(p) = current_phase(app) {
            lines.push(Line::from(""));
            lines.push(Line::from(vec![
                Span::styled(format!("Phase {} ", p.id), app.theme.strong()),
                Span::styled(trunc(&p.title, 28), app.theme.base()),
            ]));
            lines.push(Line::from(vec![
                Span::styled(bar(p.done, p.total, 16), app.theme.accent_style()),
                Span::styled(format!(" {}/{}", p.done, p.total), app.theme.dim()),
            ]));
        }
    }
    f.render_widget(Paragraph::new(lines), text_area);
}

fn draw_sprite(f: &mut Canvas<'_>, app: &App, area: Rect) {
    let t = &app.theme;
    let colour = |c: char| match c {
        '#' => Some(t.accent),
        'o' => Some(t.second),
        'w' => Some(t.paint(MIST)),
        'k' => Some(t.paint(INK)),
        _ => None,
    };
    let rows = sprite_rows();
    let buf = f.buffer_mut();
    for cy in 0..(rows.len() / 2).min(area.height as usize) {
        for x in 0..12.min(area.width as usize) {
            let at = |r: usize| rows[r].get(x).copied().and_then(colour);
            let (up, low) = (at(cy * 2), at(cy * 2 + 1));
            let cell = &mut buf[(area.x + x as u16, area.y + cy as u16)];
            match (up, low) {
                (None, None) => {}
                (Some(a), Some(b)) => {
                    cell.set_char('▀').set_style(Style::new().fg(a).bg(b));
                }
                (Some(a), None) => {
                    cell.set_char('▀').set_style(Style::new().fg(a));
                }
                (None, Some(b)) => {
                    cell.set_char('▄').set_style(Style::new().fg(b));
                }
            }
        }
    }
}

fn tab_label(app: &App, tab: Tab) -> String {
    let n = match tab {
        Tab::Now => app.snap.in_progress.len(),
        Tab::Upcoming => app.snap.ready.len() + app.snap.blocked.len(),
        Tab::Review => app.review_count(),
        Tab::Done => app.snap.done.len(),
    };
    if n > 0 {
        format!(" {} {} ", tab.title(), n)
    } else {
        format!(" {} ", tab.title())
    }
}

fn draw_tabs(f: &mut Canvas<'_>, app: &mut App, area: Rect) {
    let mut x = area.x;
    let mut titles = vec![];
    for tab in Tab::ALL {
        let label = tab_label(app, tab);
        let w = label.chars().count() as u16;
        if x + w <= area.x + area.width {
            app.hits.push((Rect::new(x, area.y, w, 1), Hit::Tab(tab)));
        }
        x += w + 1;
        titles.push(Line::from(label));
    }
    let review_hot = app.review_count() > 0;
    let styled: Vec<Line> = Tab::ALL
        .iter()
        .zip(titles)
        .map(|(tab, l)| {
            if *tab == Tab::Review && *tab != app.tab && review_hot {
                l.style(
                    Style::new()
                        .fg(app.theme.second)
                        .add_modifier(Modifier::BOLD),
                )
            } else {
                l
            }
        })
        .collect();
    let at = Tab::ALL.iter().position(|t| *t == app.tab).unwrap_or(0);
    f.render_widget(
        Tabs::new(styled)
            .select(at)
            .style(app.theme.dim())
            .highlight_style(app.theme.selected())
            .divider(" ")
            .padding("", ""),
        area,
    );
}

// ---------------------------------------------------------------- list

fn empty_copy(tab: Tab) -> &'static str {
    match tab {
        Tab::Now => "Nothing in progress.\nClaim a task with `tidetable start`.",
        Tab::Upcoming => "Nothing waiting.\nEvery phase is done, or no tasks were added.",
        Tab::Review => {
            "No human look needed.\nTasks deferred with `tidetable look-defer` appear here."
        }
        Tab::Done => "Nothing finished yet.",
    }
}

fn draw_state(f: &mut Canvas<'_>, app: &App, area: Rect) {
    let (head, body, style) = match &app.load {
        Load::Loading => (
            "Loading…",
            "Asking tidetable for the current state.".to_string(),
            app.theme.dim(),
        ),
        Load::Failed(m) => {
            // One line in every mode, so a pane a few rows high (an embedder's sidebar) still says
            // what is wrong instead of losing the reason under its heading.
            let text = format!(
                "{} Could not read the todo file · {}",
                Status::Blocked.icon(),
                m.split_whitespace().collect::<Vec<_>>().join(" ")
            );
            let pad = if area.width > 8 { 1 } else { 0 };
            let row = Rect::new(
                area.x + pad,
                area.y,
                area.width - pad * 2,
                area.height.min(1),
            );
            f.render_widget(
                Paragraph::new(Span::styled(
                    trunc(&text, row.width as usize),
                    Style::new().fg(app.theme.danger),
                )),
                row,
            );
            return;
        }
        Load::Pick(files) => {
            let mut lines = vec![
                Line::from(Span::styled(
                    format!("{} todo files found", files.len()),
                    app.theme.strong(),
                )),
                Line::from(""),
            ];
            for (i, f) in files.iter().take(9).enumerate() {
                lines.push(Line::from(vec![
                    Span::styled(format!("{} ", i + 1), app.theme.accent_style()),
                    Span::styled(f.to_string_lossy().into_owned(), app.theme.base()),
                ]));
            }
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                "Press a number to open one.",
                app.theme.dim(),
            )));
            f.render_widget(
                Paragraph::new(lines).block(Block::new().padding(Padding::new(2, 2, 1, 0))),
                area,
            );
            return;
        }
        Load::Ready => return,
    };
    let lines = vec![
        Line::from(Span::styled(head, style.add_modifier(Modifier::BOLD))),
        Line::from(Span::styled(body, app.theme.dim())),
    ];
    f.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: true })
            .block(Block::new().padding(Padding::new(2, 2, 1, 0))),
        area,
    );
}

fn entry_lines(app: &App, e: &Entry, selected: bool, width: usize) -> Vec<Line<'static>> {
    let t = &app.theme;
    let mark = if selected { "▌" } else { " " };
    let mark_span = Span::styled(mark, Style::new().fg(t.accent));
    let sel_style = if selected { t.selected() } else { Style::new() };
    let mut lines = vec![];
    match &e.kind {
        Kind::Header => {
            lines.push(Line::from(Span::styled(
                trunc(&e.title, width),
                t.dim().add_modifier(Modifier::BOLD),
            )));
        }
        Kind::Task => {
            let st = e.status.unwrap_or(Status::Pending);
            let label = st.label();
            let used = 2 + label.chars().count() + 2 + e.id.chars().count() + 2;
            lines.push(
                Line::from(vec![
                    mark_span,
                    Span::raw(" "),
                    Span::styled(label, t.status_style(st)),
                    Span::raw("  "),
                    Span::styled(e.id.clone(), t.dim()),
                    Span::raw("  "),
                    Span::styled(
                        trunc(&e.title, width.saturating_sub(used)),
                        if selected { t.strong() } else { t.base() },
                    ),
                ])
                .style(sel_style),
            );
            for s in &e.sub {
                lines.push(
                    Line::from(vec![
                        Span::raw("    "),
                        Span::styled(trunc(s, width.saturating_sub(4)), t.dim()),
                    ])
                    .style(sel_style),
                );
            }
            if let Some((st, text)) = &e.note {
                lines.push(Line::from(vec![
                    Span::raw("    "),
                    Span::styled(
                        trunc(&format!("{} {}", st.icon(), text), width.saturating_sub(4)),
                        t.status_style(*st),
                    ),
                ]));
            }
        }
        Kind::Check { on, .. } => {
            let (box_, style) = if *on {
                ("[x]", t.status_style(Status::Done))
            } else {
                ("[ ]", t.base())
            };
            lines.push(
                Line::from(vec![
                    mark_span,
                    Span::raw("   "),
                    Span::styled(box_, style),
                    Span::raw(" "),
                    Span::styled(
                        trunc(&e.title, width.saturating_sub(8)),
                        if *on { t.dim() } else { t.base() },
                    ),
                ])
                .style(sel_style),
            );
        }
    }
    lines
}

pub fn draw_list(f: &mut Canvas<'_>, app: &mut App, area: Rect, tab: Tab, focused: bool) {
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(app.theme.border_style(focused))
        .style(app.theme.panel());
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width < 4 || inner.height < 1 {
        return;
    }
    if app.load != Load::Ready {
        draw_state(f, app, inner);
        return;
    }
    let entries = app.entries(tab);
    if entries.is_empty() {
        f.render_widget(
            Paragraph::new(empty_copy(tab))
                .style(app.theme.dim())
                .wrap(Wrap { trim: true })
                .block(Block::new().padding(Padding::new(1, 1, 1, 0))),
            inner,
        );
        return;
    }
    let width = inner.width.saturating_sub(2) as usize;
    let selected = app.selected_index(tab);
    let mut lines: Vec<Line> = vec![];
    // (first line, height, selectable index) per selectable entry
    let mut spans: Vec<(usize, usize, usize)> = vec![];
    let mut sel_n = 0;
    for e in &entries {
        let is_sel = e.selectable() && sel_n == selected;
        let ls = entry_lines(app, e, is_sel, width);
        if e.selectable() {
            spans.push((lines.len(), ls.len(), sel_n));
            sel_n += 1;
        }
        lines.extend(ls);
    }
    let vh = inner.height as usize;
    let mut off = app.scroll.get(&tab).copied().unwrap_or(0);
    if let Some(&(start, h, _)) = spans.get(selected) {
        if start < off {
            off = start;
        } else if start + h > off + vh {
            off = start + h - vh;
        }
        // Show the group heading above the first row of a group.
        if start == off + 1 && off > 0 {
            off -= 1;
        }
    }
    off = off.min(lines.len().saturating_sub(vh));
    app.scroll.insert(tab, off);

    let text_area = Rect::new(
        inner.x,
        inner.y,
        inner.width.saturating_sub(1),
        inner.height,
    );
    let shown: Vec<Line> = lines.iter().skip(off).take(vh).cloned().collect();
    f.render_widget(Paragraph::new(shown).style(app.theme.panel()), text_area);

    for (start, h, idx) in spans {
        let (a, b) = (start.max(off), (start + h).min(off + vh));
        if a < b {
            app.hits.push((
                Rect::new(
                    text_area.x,
                    text_area.y + (a - off) as u16,
                    text_area.width,
                    (b - a) as u16,
                ),
                Hit::Row(idx),
            ));
        }
    }
    if lines.len() > vh {
        let mut sb = ScrollbarState::new(lines.len().saturating_sub(vh)).position(off);
        f.render_stateful_widget(
            Scrollbar::new(ScrollbarOrientation::VerticalRight)
                .style(app.theme.dim())
                .begin_symbol(None)
                .end_symbol(None),
            inner,
            &mut sb,
        );
    }
}

// ---------------------------------------------------------------- detail

pub fn detail_lines(app: &App) -> Vec<Line<'static>> {
    let t = &app.theme;
    let Some(e) = app.selected_entry() else {
        return vec![Line::from(Span::styled(
            "Select a row to see its goal, touches and success criteria.",
            t.dim(),
        ))];
    };
    let snap = &app.snap;
    let mut out = vec![];
    let st = match &e.kind {
        Kind::Check { .. } => Some(Status::NeedsLook),
        _ => e.status,
    };
    out.push(Line::from(vec![
        Span::styled(e.id.clone(), t.accent_style()),
        Span::raw("  "),
        st.map_or(Span::raw(""), |s| t.status_span(s)),
    ]));
    let title = snap
        .look_queue
        .iter()
        .find(|q| q.id == e.id)
        .map(|q| q.title.clone())
        .unwrap_or_else(|| {
            if matches!(e.kind, Kind::Check { .. }) {
                String::new()
            } else {
                e.title.clone()
            }
        });
    out.push(Line::from(Span::styled(title, t.strong())));
    let section = |out: &mut Vec<Line<'static>>, name: &str, items: Vec<String>| {
        if items.is_empty() {
            return;
        }
        out.push(Line::from(""));
        out.push(Line::from(Span::styled(
            name.to_string(),
            t.dim().add_modifier(Modifier::BOLD),
        )));
        for i in items {
            out.push(Line::from(Span::styled(i, t.base())));
        }
    };
    if let Some(d) = snap.details.get(&e.id) {
        section(
            &mut out,
            "Goal",
            if d.goal.is_empty() {
                vec![]
            } else {
                vec![d.goal.clone()]
            },
        );
        section(&mut out, "Touches", d.touches.clone());
        section(&mut out, "Prerequisites", d.prerequisites.clone());
        section(
            &mut out,
            "Success criteria",
            d.success.iter().map(|s| format!("• {s}")).collect(),
        );
    }
    if let Some(b) = snap.blocked.iter().find(|b| b.id == e.id) {
        section(&mut out, "Waits on", b.blocked_by.clone());
    }
    if let Some(q) = snap.look_queue.iter().find(|q| q.id == e.id) {
        section(
            &mut out,
            "Goal",
            if q.goal.is_empty() {
                vec![]
            } else {
                vec![q.goal.clone()]
            },
        );
        if !q.note.is_empty() && !crate::app::is_pointer(&q.note) {
            section(&mut out, "Check", vec![q.note.clone()]);
        }
    }
    if let Some(c) = snap.changelog.get(&e.id) {
        section(&mut out, "Changelog", vec![c.clone()]);
    }
    out
}

fn draw_detail(f: &mut Canvas<'_>, app: &App, area: Rect) {
    let block = Block::new()
        .style(app.theme.panel())
        .padding(Padding::new(2, 2, 1, 0));
    f.render_widget(
        Paragraph::new(detail_lines(app))
            .wrap(Wrap { trim: false })
            .block(block),
        area,
    );
}

// ---------------------------------------------------------------- side

fn push_section(
    lines: &mut Vec<Line<'static>>,
    app: &App,
    name: &str,
    count: usize,
    items: Vec<Line<'static>>,
    cap: usize,
) {
    if cap == 0 {
        return;
    }
    lines.push(Line::from(vec![
        Span::styled(name.to_string(), app.theme.accent_style()),
        Span::styled(format!(" {count}"), app.theme.dim()),
    ]));
    if items.is_empty() {
        lines.push(Line::from(Span::styled("  none", app.theme.dim())));
        return;
    }
    let n = items.len();
    let cap = cap.max(1);
    if n <= cap {
        lines.extend(items);
    } else {
        let keep = cap.saturating_sub(1);
        lines.extend(items.into_iter().take(keep));
        lines.push(Line::from(Span::styled(
            format!("  +{} more", n - keep),
            app.theme.dim(),
        )));
    }
}

fn item_line(app: &App, width: usize, st: Status, id: &str, title: &str) -> Line<'static> {
    let label = st.label();
    let used = label.chars().count() + id.chars().count() + 3;
    Line::from(vec![
        Span::styled(label, app.theme.status_style(st)),
        Span::raw(" "),
        Span::styled(id.to_string(), app.theme.dim()),
        Span::raw(" "),
        Span::styled(trunc(title, width.saturating_sub(used)), app.theme.base()),
    ])
}

fn draw_side(f: &mut Canvas<'_>, app: &App, area: Rect) {
    let w = area.width.saturating_sub(2) as usize;
    let s = &app.snap;
    if app.load != Load::Ready {
        draw_state(f, app, area);
        return;
    }
    let body_h = area.height.saturating_sub(2) as usize;
    let avail = body_h.saturating_sub(3);
    let now: Vec<_> = s
        .in_progress
        .iter()
        .map(|r| {
            item_line(
                app,
                w,
                if r.lease_expired {
                    Status::StaleLease
                } else {
                    Status::InProgress
                },
                &r.id,
                &r.title,
            )
        })
        .collect();
    let review: Vec<_> = s
        .look_queue
        .iter()
        .map(|q| item_line(app, w, Status::NeedsLook, &q.id, &q.title))
        .collect();
    let up: Vec<_> = s
        .ready
        .iter()
        .map(|t| item_line(app, w, Status::Pending, &t.id, &t.title))
        .chain(
            s.blocked
                .iter()
                .map(|t| item_line(app, w, Status::Blocked, &t.id, &t.title)),
        )
        .collect();
    let (nn, nr, nu) = (now.len(), review.len(), up.len());
    let cap_now = nn.clamp(1, (avail / 3).max(2));
    let cap_rev = nr.clamp(1, (avail / 4).max(2));
    let cap_up = avail
        .saturating_sub(cap_now.min(nn.max(1)) + cap_rev.min(nr.max(1)))
        .max(2);

    let mut lines = vec![Line::from(wordmark(app))];
    push_section(&mut lines, app, "CURRENT", nn, now, cap_now);
    push_section(&mut lines, app, "REVIEW", nr, review, cap_rev);
    push_section(&mut lines, app, "UPCOMING", nu, up, cap_up);
    lines.truncate(area.height.saturating_sub(1) as usize);
    let [main, foot] = Layout::vertical([Constraint::Fill(1), Constraint::Length(1)]).areas(area);
    f.render_widget(
        Paragraph::new(lines).block(Block::new().padding(Padding::horizontal(1))),
        main,
    );
    if let Some(p) = current_phase(app) {
        let barw = w
            .saturating_sub(format!("{} {}/{}", p.id, p.done, p.total).chars().count() + 2)
            .clamp(0, 20);
        let line = Line::from(vec![
            Span::styled(format!("{} ", p.id), app.theme.strong()),
            Span::styled(bar(p.done, p.total, barw), app.theme.accent_style()),
            Span::styled(format!(" {}/{}", p.done, p.total), app.theme.dim()),
        ]);
        f.render_widget(
            Paragraph::new(line).block(Block::new().padding(Padding::horizontal(1))),
            foot,
        );
    }
}

// ---------------------------------------------------------------- bottom

fn draw_bottom(f: &mut Canvas<'_>, app: &mut App, area: Rect) {
    if app.load != Load::Ready {
        draw_state(f, app, area);
        return;
    }
    let s = &app.snap;
    let w = area.width as usize;
    let cur = match s.in_progress.first() {
        Some(r) => format!(
            "{} {} {}",
            if r.lease_expired {
                Status::StaleLease
            } else {
                Status::InProgress
            }
            .icon(),
            r.id,
            r.title
        ),
        None => "· idle".into(),
    };
    let review = format!("{} review {}", Status::NeedsLook.icon(), app.review_count());
    let next = match s.ready.first() {
        Some(t) => format!("next {} {}", t.id, t.title),
        None => "next none".into(),
    };
    let (prog, barw) = match current_phase(app) {
        Some(p) => (format!(" {}/{}", p.done, p.total), (p.done, p.total)),
        None => (String::new(), (0, 0)),
    };
    let [a, b, c, d] = Layout::horizontal([
        Constraint::Fill(3),
        Constraint::Length(review.chars().count() as u16 + 3),
        Constraint::Fill(2),
        Constraint::Length(((w / 6).clamp(6, 16) + prog.chars().count()) as u16),
    ])
    .spacing(1)
    .areas(Rect::new(area.x, area.y, area.width, 1));
    let t = &app.theme;
    f.render_widget(
        Paragraph::new(Span::styled(trunc(&cur, a.width as usize), t.strong())),
        a,
    );
    let rstyle = if app.review_count() > 0 {
        Style::new().fg(t.second).add_modifier(Modifier::BOLD)
    } else {
        t.dim()
    };
    f.render_widget(Paragraph::new(Span::styled(review, rstyle)), b);
    f.render_widget(
        Paragraph::new(Span::styled(trunc(&next, c.width as usize), t.base())),
        c,
    );
    let line = Line::from(vec![
        Span::styled(
            bar(
                barw.0,
                barw.1,
                (d.width as usize).saturating_sub(prog.chars().count()),
            ),
            t.accent_style(),
        ),
        Span::styled(prog, t.dim()),
    ]);
    f.render_widget(Paragraph::new(line), d);

    if area.height >= 2 {
        let sel = app.selected_entry();
        let text = match sel {
            Some(e) => {
                let goal = app
                    .snap
                    .details
                    .get(&e.id)
                    .map(|d| d.goal.clone())
                    .filter(|g| !g.is_empty())
                    .or_else(|| e.sub.first().cloned())
                    .unwrap_or_default();
                format!("[{}] {} {}  {}", app.tab.title(), e.id, e.title, goal)
            }
            None => format!("[{}] nothing selected", app.tab.title()),
        };
        let row = Rect::new(area.x, area.y + 1, area.width, 1);
        f.render_widget(
            Paragraph::new(Span::styled(trunc(&text, w), app.theme.dim())),
            row,
        );
    }
    if area.height >= 4 {
        let list = Rect::new(area.x, area.y + 2, area.width, area.height - 2);
        let tab = app.tab;
        let focused = app.focused;
        draw_list(f, app, list, tab, focused);
    }
}
