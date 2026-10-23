//! Hub project slug resolution.
//!
//! Order: the positional argument, `$TIDETABLE_PROJECT`, a `slug` key in the
//! nearest `.tidetable/config.json`, then the picker. Never reads
//! git remotes: the suite is not tied to one host.

use std::path::Path;

/// Environment variable that names the project when the argument is absent.
pub const ENV: &str = "TIDETABLE_PROJECT";

/// First non-empty of argument, environment, config — in that order.
pub fn resolve(
    arg: Option<String>,
    env_project: Option<String>,
    config: Option<String>,
) -> Option<String> {
    [arg, env_project, config]
        .into_iter()
        .flatten()
        .map(|s| s.trim().to_string())
        .find(|s| !s.is_empty())
}

/// The `slug` key of the nearest `.tidetable/config.json`, walking up from `start`.
///
/// A config without a slug does not stop the walk: the repo root may be above a
/// subdirectory that has its own unrelated `.tidetable` directory.
pub fn config_slug(start: &Path) -> Option<String> {
    let mut dir = Some(start);
    while let Some(d) = dir {
        let path = d.join(".tidetable").join("config.json");
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) {
                if let Some(slug) = value.get("slug").and_then(serde_json::Value::as_str) {
                    let slug = slug.trim();
                    if !slug.is_empty() {
                        return Some(slug.to_string());
                    }
                }
            }
        }
        dir = d.parent();
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn argument_beats_env_beats_config() {
        let arg = Some("arg".to_string());
        let env = Some("env".to_string());
        let cfg = Some("cfg".to_string());
        assert_eq!(resolve(arg.clone(), env.clone(), cfg.clone()), arg);
        assert_eq!(resolve(None, env.clone(), cfg.clone()), env);
        assert_eq!(resolve(None, None, cfg.clone()), cfg);
        assert_eq!(resolve(None, None, None), None);
    }

    #[test]
    fn blank_values_do_not_win() {
        assert_eq!(
            resolve(Some("  ".into()), Some("".into()), Some("cfg".into())),
            Some("cfg".to_string())
        );
    }

    #[test]
    fn config_slug_walks_up_and_ignores_a_config_without_one() {
        let root = std::env::temp_dir().join(format!("tt-slug-{}", std::process::id()));
        let nested = root.join("a").join("b");
        std::fs::create_dir_all(nested.join(".tidetable")).unwrap();
        std::fs::write(
            nested.join(".tidetable").join("config.json"),
            r#"{"other": 1}"#,
        )
        .unwrap();
        assert_eq!(config_slug(&nested), None);
        std::fs::create_dir_all(root.join(".tidetable")).unwrap();
        std::fs::write(
            root.join(".tidetable").join("config.json"),
            r#"{"slug": " demo "}"#,
        )
        .unwrap();
        assert_eq!(config_slug(&nested), Some("demo".to_string()));
        std::fs::remove_dir_all(&root).ok();
    }
}
