# Clawback

<p align="center">
  <img src="assets/logo.png" alt="Clawback logo: a grinning cartoon crab with its claws raised" width="220">
</p>


**Run it:** `uv run python clawback.py run`

<hr>

A small, security-first desktop assistant. Python, terminal-first, with a tray
status icon. The model can *ask* for tools, but every tool runs inside a Docker
container whose mounts are the only filesystem it can see.

This is deliberately not a plugin platform, not a broad system agent, and not a
messaging hub. Version 1 is local-first and narrow.

---

## Why there is a container

A Python file cannot be a sandbox. Checks like `path.startswith("/work")` or
`Path.resolve()` are defeated by symlinks, TOCTOU races, `os.system`, and shell
tricks. The isolation has to come from the operating system.

So the app is the Python program you run, and Docker is the box it puts the
dangerous tools inside:

* the model runs on the host (it needs network to OpenRouter),
* it can only request typed tools,
* tool execution happens in a container with **no network**, a **read-only root
  filesystem**, **all capabilities dropped**, **no-new-privileges**, a **non-root
  user**, and resource limits,
* the only host paths visible are the explicit mounts below.

If Docker is not running, the assistant refuses to execute tools. There is no
fallback to the host filesystem.

> Tested on Windows 11 Home. Windows Sandbox is not available on Home, so Docker
> Desktop (WSL2 backend) is the isolation boundary.

---

## What the model can see

| Path | Mode | Meaning |
|---|---|---|
| `/work` | rw | The assistant's own workspace. Defaults to `%APPDATA%\Clawback\workspace`. |
| `/rw/<name>` | rw | A folder you explicitly grant read-write access to. |
| `/ro/Desktop`, `/ro/Documents`, `/ro/Downloads`, `/ro/Pictures`, `/ro/Music`, `/ro/Videos` | ro | Standard user folders. |
| `/ro/<name>` | ro | Any extra folder you add read-only. |

* A writable grant does **not** bypass confirmation. Granting the folder makes a
  write possible; every write still needs your approval.
* Dotfiles, `AppData`, and other credential/config locations are never mounted.
* A hidden folder is masked with an **empty read-only mount**, not a string
  check. Its contents are absent from the container.
* Host paths such as `C:\`, `/mnt/c`, and `/Users` do not exist in the container.
* `run_bash` is a shell *inside the container*. There is no host shell.

The model is told all of this in its system prompt and is instructed to treat
file contents and tool output as untrusted data, never as instructions.

### Granting folders

```powershell
uv run python clawback.py roots add "D:\Research"          # read-only
uv run python clawback.py roots add "D:\Scratch" --write   # read-write
uv run python clawback.py roots remove "D:\Scratch"
```

Or in the console: `/add-root`, `/add-write-root`, `/remove-root`.

Equivalent configuration:

```toml
extra_read_roots  = ["D:\\Research"]
extra_write_roots = ["D:\\Scratch"]
hidden_roots      = ["D:\\Scratch\\private"]
```

Runtime grants are stored in `state.json`, audited, and applied live with
`/add-*` or on restart with the CLI. A writable grant is still subject to
per-action confirmation.

---

## Requirements

* Windows 10/11
* Python 3.11+
* [uv](https://docs.astral.sh/uv/) (or a normal venv with the dependencies)
* Docker Desktop, running (Linux containers)
* An OpenRouter API key

---

## Setup

```powershell
uv sync --extra dev
```

Set the key as a **Windows user environment variable** and open a new terminal:

```powershell
[Environment]::SetEnvironmentVariable("OPENROUTER_API_KEY", "sk-or-...", "User")
```

Check everything:

```powershell
uv run python clawback.py doctor
```

Run it:

```powershell
uv run python clawback.py run
```

`python clawback.py` works too if the venv is activated
(`.venv\Scripts\Activate.ps1`).

The tray icon shows the state by color: blue = idle, green = running,
yellow = paused, red = error or sandbox unavailable. The tray menu has
Pause, Resume, Stop current cycle, and Quit.

Terminal-only, no tray:

```powershell
uv run python clawback.py run --no-tray
```

---

## Using it

Type a request at the prompt. `/help` lists commands.

```
clawback> summarize the files in /work and tell me what is unfinished
```

Consequential actions are shown with a preview (including a diff for writes)
and require `y`:

```
+-- Confirm: write_file  (id 1a2b3c4d) ----------------------------+
| Write /work/summary.md (812 bytes):                             |
| --- /work/summary.md (current)                                  |
| +++ /work/summary.md (new)                                      |
| ...                                                             |
+-----------------------------------------------------------------+
Approve? [y/N]
```

### Commands

| Command | Purpose |
|---|---|
| `/status` | state, model, sandbox, locations |
| `/pause` `/resume` | pause or resume scheduled wake cycles |
| `/stop` | cancel the current cycle/run |
| `/tasks` | scheduled tasks and next due time |
| `/enable NAME` `/disable NAME` | toggle a task |
| `/roots` | show mounted folders and their modes |
| `/add-root PATH` | grant read-only access to a folder (live reload) |
| `/add-write-root PATH` | grant read-write access (writes still confirmed) |
| `/remove-root PATH` | remove a read or write grant |
| `/hide-root PATH` | mask a folder with an empty mount |
| `/unhide-root PATH` | undo a hide |
| `/memory [show\|clear\|path]` | long-term memory file |
| `/remember TEXT` | add a note to memory |
| `/email [status\|check\|poll]` | inbound email status, connectivity, one poll |
| `/new` | clear this session's conversation context |
| `/log [n]` | recent activity |
| `/wipe` | delete audit log, memory, and runtime state |
| `/sandbox` | Docker sandbox health |
| `/path` | print data locations |
| `/quit` | stop and exit |

CLI equivalents: `status`, `log`, `wipe`, `roots`, `tasks`, `memory`, `email`,
`doctor`, `config`, `sandbox`.

---

## Scheduling and wake cycles

Tasks live in `%APPDATA%\Clawback\config.toml`. A task never runs unless it is
explicitly enabled.

```toml
[[tasks]]
name = "workspace-check"
enabled = true
every_minutes = 60
prompt = "List /work and report anything unfinished. Do not modify anything."
```

The scheduler wakes every `cycle_interval_minutes` (default 30), runs only tasks
that are due and enabled, and stops the whole cycle when any budget is hit:

```toml
[budgets]
max_seconds = 90          # wall clock for the cycle
max_tool_calls = 24
max_model_rounds = 10
tool_timeout_seconds = 30
max_tool_output_bytes = 120000
```

Scheduled cycles are unattended, so they cannot confirm actions. Any write,
move, delete, shell command, or email is refused and recorded as
`needs_approval`. Read-only work proceeds normally. After the cycle the
assistant returns to idle.

Enable or disable without editing the file:

```powershell
uv run python clawback.py tasks enable workspace-check
```

(Or `/enable workspace-check` in the running app.)

---

## Permissions

* **Read** tools (list, read, stat, search, copy-from-read-root) run without
  confirmation.
* **Consequential** tools are `write_file`, `append_file`, `make_dir`,
  `move_path`, `copy_path`, `delete_path`, `run_bash`, `send_email`.
* **Terminal sessions** ask you to confirm each consequential action. An
  approval is bound to a SHA-256 digest of the exact tool + arguments and
  expires.
* **Scheduled cycles** refuse consequential actions -- nobody is there to
  confirm them.
* **Email sessions** auto-approve consequential actions by policy. The sandbox
  still confines file operations, and `send_email` still cannot leave the
  owner/trusted allowlist.
* Denying an action tells the model to stop and report, not retry.
* Adding a root or hiding a folder is an explicit owner action and is audited.
  Hiding is enforced at the mount layer.
* Writable grants are mounted at `/rw/<name>`. Terminal sessions confirm each
  write; email sessions auto-approve it. The sandbox still confines the path.

`delete_path` moves things to a trash folder in the same writable root
(`/work/.trash` or `/rw/<name>/.clawback-trash`) instead of unlinking them.

---

## Memory

**Session context.** Within a running terminal session, Clawback keeps a bounded
conversation history, so follow-up questions work. `/new` clears it.

**Long-term memory.** `%APPDATA%\Clawback\memory.md` is a human-readable Markdown
file. After each interaction -- interactive or scheduled -- the host appends a
short entry: time, mode, your request, its answer, and the tools used. At the
start of each run the most recent portion is injected into the system prompt
inside a data block.

The model can also read and write it explicitly:

* `read_memory` -- read the file (optionally capped).
* `append_memory` -- append a note when you ask it to remember something.

Both are host-side tools scoped to `memory.md` only: no path argument, no access
to project files, appends capped at 4 KB, and every call audited. Memory content
is still treated as untrusted data by the model.

* `/memory` or `uv run python clawback.py memory show` -- read it.
* `/memory clear` or `uv run python clawback.py memory clear` -- forget everything.
* `/memory path` -- print the path.
* `/remember TEXT` -- add a note.
* `clawback wipe` also deletes it.

The file is capped at roughly 64 KB; oldest entries are trimmed first. Memory is
**untrusted data** as far as the model is concerned, and it is yours to edit or
delete.

---

## Email (optional, off by default)

Clawback can send mail, and -- when enabled -- receive it.

**Outbound.** `send_email` is a model tool. Recipients are limited to
`recipient` (you) and the `trusted` list; arbitrary addresses are refused.
Terminal sessions still confirm each send.

**Inbound.** An IMAP poller checks unread mail every `poll_minutes`:

1. Ignore our own address, auto-submitted/bulk mail, empty bodies, and
   attachments.
2. If the sender is on `trusted` and the headers agree (`From`, `Sender`,
   `Return-Path`, `Reply-To`; any `spf/dkim/dmarc=fail` demotes it), start a
   fresh agent session and auto-reply in the same thread.
3. Otherwise, run a separate classifier session with exactly one tool,
   `classify_email`:
   * `malicious: true` -> delete the message from the mailbox;
   * `safe` -> start a fresh agent session. No reply is sent to an unknown
     sender;
   * classifier failure -> fail closed: leave the message, mark it seen, audit.

Each email is its own session; it does not share the terminal conversation. It
does share `memory.md`.

A whitelisted sender whose headers do not match is treated as untrusted and
goes through the classifier.

### Setup

Environment variables (Windows user scope; reopen the terminal):

```powershell
# SMTP (outbound)
[Environment]::SetEnvironmentVariable("CLAWBACK_SMTP_HOST", "smtp.example.com", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_SMTP_PORT", "587", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_SMTP_USER", "clawback@example.com", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_SMTP_PASSWORD", "...", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_SMTP_FROM", "clawback@example.com", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_SMTP_SECURITY", "starttls", "User")  # or ssl

# IMAP (inbound)
[Environment]::SetEnvironmentVariable("CLAWBACK_IMAP_HOST", "imap.example.com", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_IMAP_PORT", "993", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_IMAP_USER", "clawback@example.com", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_IMAP_PASSWORD", "...", "User")
[Environment]::SetEnvironmentVariable("CLAWBACK_IMAP_SECURITY", "ssl", "User")  # or starttls/none
```

```toml
[email]
enabled = true
address = "clawback@example.com"
recipient = "you@example.com"
trusted = ["you@example.com"]

inbound = true
poll_minutes = 5
require_header_match = true
delete_malicious = true
auto_reply = true
max_body_bytes = 50000
```

Check it:

```powershell
uv run python clawback.py email status
uv run python clawback.py email check
uv run python clawback.py email poll
```

`email check` connects to IMAP without processing. `email poll` processes one
batch. In the console: `/email [status|check|poll]`.

### Tradeoffs

* Whitelisted senders bypass the classifier. If a trusted account is
  compromised, it can drive the assistant.
* The classifier is best-effort LLM screening, not a proof.
* Email sessions auto-approve consequential tools. The sandbox still confines
  file operations, and `send_email` cannot leave the owner/trusted allowlist,
  so an injected email cannot exfiltrate to a stranger.
* Attachments are ignored in v1. Plain text is preferred; HTML-only bodies are
  converted to text.
* SMS, IRC, Matrix, and XMPP remain out of scope.

---

## Activity history and data

Everything is under `%APPDATA%\Clawback`:

| File | Purpose |
|---|---|
| `config.toml` | declarative configuration |
| `state.json` | task toggles, last-run times, CLI-added roots, seen email IDs |
| `audit.jsonl` | append-only activity log (JSONL) |
| `memory.md` | long-term interaction memory (editable, deletable) |
| `workspace/` | the writable sandbox |

The audit records wake cycles, tasks, tool calls, permission decisions,
approvals, durations, and outcomes. File and email *contents* are not copied
into it -- only paths, sizes, and subjects. Secrets are redacted.

Review it with `/log` or `uv run python clawback.py log -n 100`.
Delete it with `/wipe` or `uv run python clawback.py wipe` (this also clears
memory). Add `--all` to also delete the workspace.

---

## Tests

```powershell
uv run pytest
```

Unit tests cover the path policy, approval binding, scheduler budgets, audit
redaction, config validation, session memory, the persistent memory file,
inbound email parsing, header trust, classifier decisions, deletion/dedup, reply
threading, and the agent loop (with a scripted model). The Docker integration
tests prove the container properties: non-root, `/work` writable, writable
grants, read-only roots, hidden-folder masking, absent host paths, no network,
and structured-operation traversal rejection. They skip automatically if Docker
is unavailable.

---

## Deliberate limitations

* **No autostart** yet. Start it manually.
* **No plugins**, no broad system access, no host shell.
* **No SMS / IRC / Matrix / XMPP** yet.
* **No cloud sync.** The profile is local.
* **Inbound email is a command channel for trusted senders.** A compromised
  trusted account can drive the assistant. Unknown senders are LLM-screened,
  which is best-effort, not a proof. Attachments are ignored.
* The model is external: prompts and tool output you send it go to OpenRouter.
  That is the only default network path; the sandbox itself has none.
* Docker Desktop is a runtime dependency. The app fails closed without it.
* The sandbox protects the host from the model. It does not make the model's
  judgment trustworthy. Terminal approvals, the email allowlist, and the
  classifier are the additional controls.

---

## How it fits together

```
terminal / tray
      |
      v
  Runtime  ──  Scheduler (bounded wake cycles)
      |    └──  EmailInbox (IMAP)  ──►  classifier (unknown senders)
      |                                   │
      |                                   ├── safe ──────► Agent (fresh email session)
      |                                   └── malicious ─► delete
      v
  Agent loop  ──► OpenRouter (DeepSeek V4.1 Flash 1017)
      |  ^
      |  └── Conversation (session) + memory.md (long-term)
      v
  ToolDispatcher
      | validate args (jsonschema)
      | check path policy (deny by default)
      | apply session policy (terminal confirms / email auto-approves)
      | audit decision
      v
  DockerSandbox  ──► container: /work rw, /rw/* rw, /ro/* ro, no network
      |
      └── AuditLog (JSONL)  ·  StateStore (JSON)  ·  MemoryStore (memory.md)  ·  config.toml
```

Secrets (`OPENROUTER_API_KEY`, SMTP/IMAP passwords) are read from the
environment and never written to the profile or the audit log.

---

## Credits

Logo: ["Cartoon crab png sticker, sea"](https://www.rawpixel.com/image/6482212/png-face-sticker)
from Rawpixel, released under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (public domain).
Found via [Openverse](https://openverse.org/), the Creative Commons search.
The background was removed for use as a logo. CC0 does not require attribution;
it is included as a courtesy.
