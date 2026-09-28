# Claude Code Usage — menu bar

A [SwiftBar](https://github.com/swiftbar/SwiftBar) plugin that shows your Claude Code
subscription usage in the macOS menu bar, mirroring what `/usage` prints inside Claude Code.

- **Bright bar** — current 5-hour session %
- **Dim bar** — weekly limit (all models)
- Orange → amber at 85% → red at 100%
- Dropdown: both limits with reset times, plus a rough local token/cost estimate
  for the last 5h per model (Opus / Sonnet / Haiku / subagents)

## Install

Needs macOS, [Claude Code](https://claude.com/claude-code) logged in, Python 3 and Pillow.

```sh
brew install --cask swiftbar
pip3 install pillow
git clone <this repo> ~/claude-stats-menubar
chmod +x ~/claude-stats-menubar/swiftbar-plugins/claude-stats.300s.py
```

The first line of the plugin is `#!/opt/homebrew/bin/python3.14` — change it to your
Python (`which python3`) if that path doesn't exist.

Open SwiftBar and set its plugin folder to `~/claude-stats-menubar/swiftbar-plugins`.

## Notes

- Refreshes every 5 minutes (`.300s.` in the filename — rename to change). Use
  **Refresh** in the dropdown to force one.
- Percentages come from `claude -p "/usage"`, which costs zero tokens. The plugin deletes
  the throwaway transcripts that call leaves behind.
- If no Claude Code transcript changed since the last run, it reprints a cached result
  instead of calling `claude` (saves battery).
- The cost estimate uses public list prices in `PRICE` and is approximate.
