# lotus documentation

The full reference. For a quick start, see the [README](README.md).

- [The problem it solves](#the-problem-it-solves)
- [Install](#install)
- [Use](#use)
- [Staying on task (context rot)](#staying-on-task-context-rot)
- [Light and dark terminals](#light-and-dark-terminals)
- [Renders](#renders)
- [Tool packs](#tool-packs)
- [Memory](#memory)
- [Automation](#automation)
- [Extending](#extending)
- [Configuration](#configuration)
- [Layout](#layout)
- [Status](#status)

## The problem it solves

Most agent harnesses are built for frontier models and then pointed at Ollama. On a local model that breaks in confusing ways:

- **Silent truncation.** Ollama loads models with a small default context and drops whatever doesn't fit, without an error. Harnesses that send 10k+ tokens of system prompt and tool schemas lose the user's actual question, and the model looks "dumb".
- **Broken tool calls.** The OpenAI-compatible `/v1` endpoint ignores per-request `num_ctx` and mangles streamed tool calls. Many small models also write tool calls as plain text even when they "support" tools.
- **Context rot.** One `cat` of a log file or one web page fills the window, and a few turns later the agent forgets the task.

Lotus is designed around those constraints:

| Problem | What lotus does |
|---|---|
| Silent truncation | Reads each model's true context length from `/api/show`, estimates every prompt, and requests a `num_ctx` step that fits. It only grows within a session, because every change forces Ollama to reload the model. It warns if a prompt still fills the window. |
| Prompt bloat | A ~250-token system prompt. Tools come in packs (`web`, `browser`, `system`, `agents`, MCP servers, plugins); only the active ones are sent, and the model can call `load_tools("web")` when it needs more. `/context` shows exactly what is using the window. |
| Tool calling | Native `/api/chat` tool calls when the model supports them; otherwise a compact text protocol. In both modes, calls written as text (`<tool_call>`, `<tool>`, fenced JSON) are rescued, broken JSON is repaired, and wrong argument names are mapped (`{"cmd": "ls"}` → `shell(command=)`). Unknown tools get a "did you mean". |
| Context rot | See [Staying on task](#staying-on-task-context-rot): output paging, observation masking, stale-read retirement, a plan and pins that sit at the end of the window, repeat-call detection, structured compaction, and sub-agents with a fresh context. |

## Install

Linux and macOS, one line:

```bash
curl -fsSL https://raw.githubusercontent.com/Het-Joshi/lotus/main/install.sh | sh
```

With browser control (also installs Playwright and Chromium, about 150 MB):

```bash
curl -fsSL https://raw.githubusercontent.com/Het-Joshi/lotus/main/install.sh | sh -s -- --browser
```

Running the browser line later adds browser control to an existing install. The script uses `uv` or `pipx` if you have them, and otherwise a private virtualenv in `~/.local/share/lotus` linked into `~/.local/bin`. It never needs root. `--uninstall` removes it and keeps your `~/.lotus` config and memory. [Read it first](install.sh) if you like.

Or with pip, anywhere, Windows included:

```bash
pip install "lotus-agent @ https://github.com/Het-Joshi/lotus/archive/main.tar.gz"
pip install ".[browser]" && python -m playwright install chromium    # from a clone, with browser control
```

You also need [Ollama](https://ollama.com) running and at least one model (`ollama pull qwen3:4b`). `lotus doctor` checks everything.

## Use

```bash
lotus                                       # interactive session
lotus -m qwen3:8b                           # pick a model
lotus "what's using port 8080?"             # one-shot
git diff | lotus "write a commit message"   # pipe anything in
lotus "summarise @notes.md" > summary.md    # stdout is clean markdown when redirected
lotus -r                                    # resume the last session
lotus doctor                                # check Ollama, models, Tor, Playwright
```

### The prompt

Type `/` and a menu of every command opens under the prompt, with a description of each, and narrows as you type (`/co` → `/compact`, `/context`, `/copy`). `↑↓` picks, `Tab` completes, `Enter` runs, `Esc` closes. The same menu completes arguments (`/theme ` offers auto, dark, light; `/model ` lists installed models; `/tools ` lists packs) and `@paths`. Recipes and plugin commands show up in it too, and run as `/<name>`.

- `Alt+Enter` or a trailing `\` adds a line. Pastes keep their newlines; big ones collapse into a `[pasted #1 +40 lines]` placeholder that expands when you send.
- `↑` walks history, filtered by whatever you've typed. History lives in `~/.lotus/history.jsonl`.
- The footer shows the model, how full the context is, plan progress, pins, and the permission mode when it isn't `ask`.
- `Ctrl+C` stops a reply or clears the line; twice on an empty line quits. Emacs keys work (`Ctrl+A/E/W/U/K`), and `Ctrl+L` redraws.

### Staying in control while it works

While lotus is thinking or running tools, a lotus blooms at the bottom of the screen with what it's doing and for how long (`✿ vichāra · reflecting · 4s · esc to stop`, `✿ shell · npm test · 12s`).

- `Esc` (or `Ctrl+C`) stops the turn wherever it is. The reply stream is closed, so Ollama stops generating. A running shell command and everything it started is killed, a slow browser action is abandoned, and sub-agents are cancelled. The history stays well-formed, and the model is told it was stopped, so your next message can steer it somewhere else.
- Keep typing while it works. What you type shows next to the spinner. `Enter` queues it as your next message, and an unfinished line comes back in the prompt when the turn ends.
- Approvals are a small box with the full command or file content, answered with one key: `y` once, `a` always, `n` no, `Esc` to stop the whole turn.

The live menu needs a POSIX terminal. On Windows, or with `LOTUS_SIMPLE_INPUT=1`, lotus uses a plain readline prompt with the same Tab completion.

### Essentials

`/help` lists everything.

- `@path` attaches a file, folder or image to your message. Dragging an image path into the terminal works too.
- `!command` runs a shell command yourself and shares the output with the model.
- `/model` lists models with their context size and abilities (tools, vision, thinking) and lets you switch.
- `/think on|off|low|medium|high` controls reasoning on models that support it, `/think hide` collapses it. On models without native reasoning, `on` adds a plan-first prompt.
- `/explore` is a small file browser: open folders, preview files, attach them, or make a folder the working directory.
- `/retry` regenerates the last reply; `/undo` drops the last exchange and puts your message back in the prompt to edit.
- `/copy` puts the last reply on the clipboard (over SSH it uses OSC 52, so it lands on your own machine). `/export [file.md]` saves the conversation as markdown.
- `/context`, `/compact [what to keep]`, `/clear` manage the window. `/todo` shows the agent's plan, and `/pin <note>` keeps a goal or constraint in front of it. `/mem`, `/remember`, `/forget` manage long-term memory.
- `/perm ask|auto|readonly` sets approval for tools that change things. The default asks before shell commands, file writes, typing into web pages and opening apps.

If you attach an image while using a text-only model, lotus switches to an installed vision model for that message (or the one set in `vision_model`).

## Staying on task (context rot)

Small models degrade as the window fills: they lose the request in the middle of long tool loops, trust stale copies of files, and repeat themselves. What lotus does about it, cheapest first:

| Mitigation | How |
|---|---|
| Output paging | A tool result over `tool_output_share` of the window is clipped to its head and tail and stashed; the model reads the rest with `page_output`. |
| Stale reads retired | When a file is read again, the earlier read of the same lines becomes a one-line note. When a file is edited or rewritten, earlier reads of it are marked stale. The model never holds two conflicting copies. |
| Observation masking | At 60% full, tool output from before your last two messages shrinks to a short head and a note saying to rerun the tool. The model's own reasoning and answers stay. This is cheaper than summarising and keeps the shape of the conversation. |
| Goal, plan and pins at the end | The `todo` tool keeps a plan. It, your `/pin` notes, relevant memory facts and (after a couple of tool rounds) the request being worked on are attached to the end of the newest message. They're never buried, and they survive compaction. |
| Stable prompt prefix | The system prompt no longer changes from turn to turn. Ollama reuses its KV cache for an unchanged prefix, so long sessions aren't re-read from scratch every turn. |
| Repeat-call guard | An identical tool call with nothing changed since (no writes or commands in between) gets "you already have this result" instead of being run again. Two rounds of nothing but repeats end the turn. |
| Step budget | Near `max_steps` the model is told to wrap up with its best answer instead of being cut off mid-task. |
| Loop guard | Reasoning that repeats itself (the same passage, or the same thought coming back) or runs past `think_budget` is cut off. The step is then asked again with reasoning off and a nudge to answer directly. A reply that starts repeating is stopped and trimmed to one copy. |
| Structured compaction | At 80% full, older history becomes a summary under fixed headings (Goal, Done, Facts, Open), seeded with the plan and pins. The exact paths of files touched are appended, so they're never lost. `/compact <focus>` says what must survive. |
| Fresh contexts | `delegate` hands self-contained tasks to sub-agents that start empty and return only their answer. |

`/context` shows how much of the window each part takes, including the plan, pins and memory.

## Light and dark terminals

Lotus has a dark and a light palette and picks one by asking the terminal for its background color (OSC 11). It asks again before each prompt, so if your terminal switches theme mid-session (for example, following the OS at sunset) the colors follow. Terminals that don't answer cost one round trip at startup and are never asked again; lotus then falls back to `COLORFGBG`, then the system appearance (macOS, Windows, GNOME), then dark.

Force a palette with `/theme dark|light` (saved to config), `--theme`, `"theme"` in config, or `LOTUS_THEME`. `/theme` alone shows the palette and how it was chosen.

## Renders

The model can draw in your terminal by writing a fenced block, or by calling `show_chart` / `show_table`:

````
```chart
{"type": "line", "title": "latency", "labels": ["mon", "sun"],
 "series": {"p50": [3, 4, 2, 5, 6, 4, 7], "p99": [9, 12, 8, 15, 11, 10, 14]}}
```
````

Types: `bar`, `line`, `pie`, `spark`, `graph` (edges render as a tree), `table`. Markdown tables, headings, lists and code blocks render as they stream.

## Tool packs

| Pack | Tools |
|---|---|
| core (always on) | `read_file`, `write_file`, `edit_file`, `list_dir`, `find_files`, `grep`, `shell`, `todo`, `remember`, `recall`, `page_output`, `load_tools`, `view_image` |
| render | `show_chart`, `show_table` |
| web | `web_search` (DuckDuckGo, or your SearXNG), `fetch_url` (page as text with numbered links) |
| browser | `browser_open`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_press`, `browser_select`, `browser_scroll`, `browser_find`, `browser_wait`, `browser_back`, `browser_tabs`, `browser_screenshot`, `browser_close` |
| system | `open_path`, `launch_app`, `list_apps`, `clipboard_get`, `clipboard_set`, `notify`, `system_info` |
| agents | `delegate` (parallel sub-agents with fresh context) |

The browser shows pages to the model as text plus numbered elements, with their state, so a small model can act with `browser_click("3")` instead of writing selectors:

```
[2] input:email Email  (value="me@example.org")
[3] select Country  (selected="India", options: India | Nepal | …)
[4] input:checkbox Remember me  (unchecked)
[5] button Place order
```

Keeping it controlled:

- Playwright runs on its own thread, so `Esc` abandons a slow page at once without breaking the browser.
- Clicks on buttons that look like they spend money, send, publish or delete (`buy`, `pay`, `send`, `delete`, `submit`, …) ask first, unless `/perm auto`. Typing into pages always asks in `ask` mode.
- `browser.allow` / `browser.block` limit which sites it may open, links and redirects included.
- `alert` dialogs are accepted. `confirm` and `prompt` dialogs are dismissed and reported to the model, so it never agrees to something on its own.
- Popups and `target=_blank` links become the current tab. Downloads go to `~/.lotus/downloads`.
- After an action, the page text is resent only if it changed, and long pages point the model at `browser_find`. Both keep small windows small.
- With a visible window, the element being clicked or typed into flashes with a lotus-pink outline.

Safe browsing, on by default:

- Pages are checked against public malware and phishing lists (URLhaus and OpenPhish, cached in `~/.lotus/safebrowsing` and refreshed every 6 hours) before they load. Add `"phishing-database"` to `safe_browsing.lists` for about 400,000 more phishing domains, or set `safe_browsing.google_api_key` to also ask Google Safe Browsing. URL lists are matched exactly, so a shared host like GitHub isn't blocked because one file on it is bad.
- A listed page opened on purpose (`browser_open`, `fetch_url`) asks you first, even in `auto` mode; saying yes trusts that site for the session. A listed page reached by a click or redirect is blocked and reported. Search results on a list are marked.
- Page text is labelled as untrusted data. Text written to steer an AI ("ignore previous instructions…", "if you are an AI assistant…") is flagged so the model tells you instead of acting on it.
- Passwords, card numbers, CVVs and one-time codes are never typed without a fresh yes, whatever the permission mode.
- Programs and scripts (`.exe`, `.dmg`, `.sh`, `.apk`, macro documents, …) are never downloaded unless `safe_browsing.allow_executables` is on.
- Only `http` and `https` pages open (no `file:`, `javascript:` or `data:`), and unencrypted `http` pages carry a warning.

Tor: with `/tor on` (or `--tor`) the browser runs behind Tor as well, not only the web tools. Any `.onion` address switches it to Tor by itself, and it stays on Tor until closed. Names are resolved inside Tor, WebRTC can't reveal your address, QUIC is off, and the locale and time zone are generic. Switching Tor on or off restarts the browser, and the snapshot says `(via Tor)`. Your own Chrome (`browser.cdp_url`) can't be put behind Tor from lotus.

`/browser` shows the open tabs, `/browser close` closes it, and `/browser show` / `/browser hide` switch between a window and headless. Set `browser.cdp_url` to `http://localhost:9222` to drive your own Chrome (start it with `--remote-debugging-port=9222`).

### Tor

Start Tor (the `tor` service on port 9050, or Tor Browser on 9150), then use `lotus --tor`, `/tor on`, or let the model pass `via_tor=true`. `.onion` URLs always go through Tor. Web requests run through `curl --socks5-hostname`, and the browser through Chromium's SOCKS5 proxy, so DNS also resolves inside Tor. Check with `/tor`.

## Memory

- `~/.lotus/memory.md` holds facts about you, one per line, editable by hand. Only the few relevant to the current message are injected, so memory never crowds a small window.
- `LOTUS.md` in the folder you launched lotus from holds that project's notes, in three sections: **Instructions** (yours: conventions, commands to use, things to avoid), **Project** (what it is, layout, how to build and test) and **Memory** (facts and decisions picked up while working). It's read at the start of every session there. When it outgrows its share of the window, instructions stay whole and the newest memories win. Without a `LOTUS.md`, `AGENTS.md` in the folder or its git root is read instead.
  - `/init` creates it and has lotus look around and write the Project section.
  - The model saves project facts with the `project_note` tool, and personal facts with `remember`.
  - `/note <text>` adds a memory (`-i` an instruction, `-p` about the project), `/note` lists them numbered, and `/note -3` removes one.
- `.LOTUS_REM.txt` in the same folder records where the last session left off: the goal, what's done, what's next, gotchas and files touched. The model writes it when you quit, building on the previous note; Ctrl+C at that moment writes a plain recap instead. It's also refreshed for free whenever history is compacted. The next session in that folder loads it (lotus says it's picking up where you left off). `/rem` shows it, `/rem save` writes it now and `/rem clear` forgets it. Only interactive sessions write it, so one-shot runs and recipes don't scatter files.
- Turn either off with `project.notes` / `project.rem` in config, or set `project.rem_by_model` to `false` to skip the model call at exit.

## Automation

Recipes are reusable prompts in `~/.lotus/recipes/` or `./.lotus/recipes/`:

```markdown
---
description: standup from git
packs: system
every: 1d
---
Run `git log --since=yesterday --stat` and write a standup. Context: {{input}}
```

```bash
lotus run standup "focus on the api"     # run once (add -y to allow tools without asking)
git log | lotus run standup              # stdin becomes part of the input
lotus watch onion-watch --every 30m --notify
```

Each run is appended to `~/.lotus/logs/<recipe>.log`. For scheduling across reboots, call `lotus run -y <recipe>` from cron, launchd or Task Scheduler. See `examples/recipes/`.

## Extending

### Plugins

Drop a `.py` file in `~/.lotus/plugins/` (or `./.lotus/plugins/`). Signatures and docstrings become the tool schema:

```python
from lotus.tools import tool, pack, command

pack("weather", "current weather")

@tool(pack="weather")
def weather(city: str, days: int = 1, _ctx=None):
    """Current weather for a city.
    city: city name
    days: forecast days"""
    ...

@command("weather", "quick weather")
def weather_cmd(agent, arg):
    return weather(arg)
```

`danger=True` makes a tool ask for approval; for finer checks a tool can call `_ctx.approve(name, detail)` itself. A `_ctx` parameter receives the running agent (`cwd`, `cfg`, `ui`, `queue_image`, `cancel`, ...). Long-running tools can watch `_ctx.cancel` (a `threading.Event`) to stop when the user presses Esc. See `examples/plugins/weather.py`.

### MCP servers

Any [MCP](https://modelcontextprotocol.io) server becomes a tool pack. It starts only when loaded:

```json
"mcp": {
  "fs": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/me"],
         "description": "files in my home folder", "trust": false},
  "github": {"command": ["npx", "-y", "@modelcontextprotocol/server-github"],
             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}}
}
```

Load with `/tools mcp:fs`, or let the model call `load_tools("mcp:fs")`. Tools from untrusted servers ask before running.

## Configuration

`~/.lotus/config.json` is created on first run (set `LOTUS_HOME` to move it). The settings that matter most on small hardware:

| Key | Default | Meaning |
|---|---|---|
| `ctx_max` | 32768 | Largest `num_ctx` lotus will request. Lower it on 8 GB machines, raise it for long agent runs. |
| `ctx_min` | 4096 | Smallest `num_ctx`. |
| `packs` | core, render | Packs active at start. |
| `tool_mode` | auto | `native`, `text`, or `auto` (native when the model reports the tools capability). |
| `tool_output_share` | 0.2 | Largest share of the window one tool result may take before it is paged. |
| `vision_model` | "" | Model used for images when the current one can't see. Empty means pick an installed one. |
| `search.searxng_url` | "" | Use your SearXNG instance instead of DuckDuckGo. |
| `shell` | "" | e.g. `powershell` or `/bin/zsh`. |
| `think_budget` | auto | Tokens of reasoning per step before it's cut short. `auto` is 8000, or 2000 / 6000 / 16000 with `/think low|medium|high`. `0` turns the limit off. |
| `repeat_penalty` | null | For example `1.1` if a model keeps repeating itself. `null` keeps the model's default. |
| `max_output_tokens` | 0 | Cap on one reply (Ollama's `num_predict`). lotus warns when a reply hits it. |
| `theme` | auto | `auto` follows the terminal background; `dark` or `light` forces one. |
| `safe_browsing.enabled` | true | Check pages against malware and phishing lists before they load. |
| `safe_browsing.lists` | urlhaus, openphish | Add `phishing-database` for ~400k phishing domains (11 MB). |
| `safe_browsing.google_api_key` | "" | Also check Google Safe Browsing. |
| `safe_browsing.ignore` | [] | Sites never to flag. |
| `browser.allow` / `browser.block` | [] | Sites the browser may (or may never) open, e.g. `["wikipedia.org"]`. Subdomains count. |
| `browser.confirm_risky` | true | Ask before clicking buy / send / delete / submit-like buttons. |
| `browser.dialogs` | dismiss | What to do with `confirm()` / `prompt()` dialogs: `dismiss` or `accept`. |
| `browser.headless` | null | `null` opens a window when there's a display; `true` / `false` force it. |

`OLLAMA_HOST` and `LOTUS_MODEL` override the config. `NO_COLOR` disables color, `LOTUS_THEME=dark|light` forces a palette, `LOTUS_ASCII=1` uses plain glyphs, `LOTUS_NO_ANIM=1` skips the opening animation.

## Layout

```
lotus/
  cli.py         REPL, command registry and menu, explorer, one-shot and recipe modes
  lineedit.py    the prompt: live "/" menu, multi-line, paste, history, footer
  agent.py       the loop: context budgeting, compaction, tools, sub-agents, sessions
  ollama.py      native /api client (stdlib urllib)
  context.py     num_ctx sizing and token estimates calibrated from Ollama's counts
  textcalls.py   text tool protocol, JSON repair, stream splitter for <think>/<tool>
  render.py      streaming markdown, tables, UI, spinner;  charts.py  terminal charts
  keys.py        Esc-to-stop and type-ahead while a turn runs
  theme.py       palettes, light/dark detection, ANSI helpers
  memory.py      memory.md, LOTUS.md, .LOTUS_REM.txt;  mcp.py  MCP stdio client
  safety.py      safe browsing: threat lists, prompt-injection check, download rules
  plugins.py     plugin loader;  automation.py  recipes
  tools/         core, web, browser, system
```

## Status

0.2.0. The agent loop, both tool-calling modes, rendering, compaction, sub-agents, MCP, plugins, recipes and Playwright control are tested against a mock of Ollama's native API and a real headless Chromium. The Windows and macOS branches (clipboard, notifications, app launching) are written but not yet tested on those systems.

MIT licensed.
