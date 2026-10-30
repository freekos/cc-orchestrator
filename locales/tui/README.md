# TUI translations

`cc tui` supports English (`en`) and Russian (`ru`). Press **F2** on the main
screen to select a language. The choice applies immediately and is saved in
`~/.cc/tui.json`, independently of the orchestrator's `state.json`.
Missing, malformed, or unsupported preferences use Russian. If a preference
cannot be saved, the TUI reports the error and retains the current language.

The catalogs in this directory are UTF-8 JSON objects. Keys are complete English
source messages; `en.json` maps each key to itself and `ru.json` supplies Russian
translations. A missing, empty, or invalid translation falls back to the English
source message. A missing or malformed catalog also falls back to English.

To add a language:

1. Copy `en.json` to `<code>.json` and translate the values, keeping keys intact.
2. Register the code and its native display name in `LANGUAGES` in `tui_i18n.py`.
3. Preserve `%s` / `%d` placeholders in their original order, Rich markup tags,
   keyboard shortcuts, and literal command/branch names.
4. Run `python3 tests/test_tui_i18n.py` using an environment with `textual`
   installed, and check dialogs at typical terminal widths.

When adding interface text, pass the literal template to `tr()` before
interpolating data: `tr("Create MR — %s") % title`. Add the message to every
catalog. Binding descriptions and the legend are translated when displayed so
that switching languages does not require restarting the application.

Translate interface text only. Internal status codes, IDs, command arguments,
user-authored task names, Jira text, agent replies, logs, and subprocess output
must retain their original values. Textual's built-in widget help and command
palette are supplied by Textual and are not part of these catalogs.

The Timeline and lost-task recovery panels render structured engine JSON with
translated action names, field labels, and empty-state messages. Values from
records and unknown action names stay intact. Lost-task details are translated
in the main panel too. Other command output, including project-add diagnostics,
is displayed verbatim.

The language picker is available from the main screen. Finish or cancel an open
dialog before switching; this avoids discarding any unsaved form input.
