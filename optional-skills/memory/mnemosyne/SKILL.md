---
name: mnemosyne-memory
description: Install and configure Mnemosyne memory provider.
version: 1.0.0
author: Hermes Agent
license: MIT
dependencies: [mnemosyne-hermes]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [memory, mnemosyne, setup, provider]
    category: memory
    config:
      memory.provider: mnemosyne
      memory.memory_enabled: false
      user_profile_enabled: false
---

# Mnemosyne Memory Skill

Installs Mnemosyne — a zero-dependency, SQLite-backed memory system — as
the active Hermes memory provider. Handles pip install, plugin symlinking,
config activation, and verification.

## When to Use

Use when you want persistent, semantic memory across Hermes sessions with
no external services. Mnemosyne stores everything in a local SQLite
database with optional local embeddings.

Do NOT use if you prefer Hermes's built-in MEMORY.md/USER.md flat-file
memory, or if you already have a different memory provider active.

## Prerequisites

- Hermes Agent installed and working
- `pip` available (the skill installs `mnemosyne-hermes`)
- For embeddings: `mnemosyne-memory[embeddings]` or `mnemosyne-memory[all]`
  (optional, adds ~800 MB of local ML deps)
- At least ~50 MB free disk for core, ~800 MB for embeddings

## How to Run

```bash
hermes skills install official/memory/mnemosyne
```

Then follow the prompts. The skill will:
1. Install `mnemosyne-hermes` via pip
2. Symlink the plugin into `~/.hermes/plugins/mnemosyne/`
3. Set `memory.provider: mnemosyne` in config.yaml
4. Disable built-in memory (MEMORY.md/USER.md)
5. Run `hermes memory setup`
6. Verify the installation

## Quick Reference

```bash
# After install, manage via:
hermes mnemosyne stats           # Show memory statistics
hermes mnemosyne stats --global  # Stats across all sessions
hermes mnemosyne inspect "..."   # Search memories
hermes mnemosyne sleep           # Run memory consolidation
hermes mnemosyne export --output backup.json  # Export all memories
```

## Procedure

### Step 1: Install the package

```bash
pip install mnemosyne-hermes
```

For local embeddings (recommended for offline use):
```bash
pip install "mnemosyne-hermes[embeddings]"
```

### Step 2: Link the plugin

Hermes discovers plugins by scanning `~/.hermes/plugins/`. Create the
symlink so the plugin is discovered on next startup:

```bash
mkdir -p ~/.hermes/plugins/mnemosyne
PYTHON=$(which python3 || which python)
ln -sfn "$($PYTHON -c "import pathlib, mnemosyne_hermes; print(pathlib.Path(mnemosyne_hermes.__file__).resolve().parent)")"/* \
  ~/.hermes/plugins/mnemosyne/
```

### Step 3: Configure Hermes

Set the active memory provider in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: mnemosyne
  memory_enabled: false
user_profile_enabled: false
```

Or via the CLI:
```bash
hermes config set memory.provider mnemosyne
hermes config set memory.memory_enabled false
hermes config set user_profile_enabled false
```

`memory_enabled: false` turns off Hermes's built-in MEMORY.md system.
`user_profile_enabled: false` stops USER.md injection. Both are redundant
once Mnemosyne is active.

> **Why disable built-in memory?** Hermes's built-in memory and Mnemosyne
> serve the same purpose. Running both wastes context window and creates
> conflicting signals for the model. Disabling the built-in system makes
> Mnemosyne the sole memory authority.

### Step 4: Run setup

```bash
hermes memory setup
```

This initializes the Mnemosyne database and registers session hooks.

### Step 5: Verify

```bash
hermes memory status
# Expected: Provider: mnemosyne
```

```bash
hermes mnemosyne stats
# Expected: Working memory + episodic memory counts
```

If `hermes mnemosyne stats` gives "invalid choice: 'mnemosyne'", the
plugin CLI registration hasn't loaded. Use the fallback:
```bash
hermes hermes-mnemosyne stats
```

If that also fails, restart Hermes entirely or re-run Step 2 (the symlink
may need a fresh plugin scan).

## How It Works

Mnemosyne hooks into the Hermes agent lifecycle through the MemoryProvider
interface:

| Hook | Behavior |
|---|---|
| `pre_llm_call` | Injects relevant memories into the prompt |
| `on_session_start` | Initializes session memory state |
| `post_tool_call` | Captures tool results as memories (if configured) |

Registered tools (23 total): `mnemosyne_remember`, `mnemosyne_recall`,
`mnemosyne_stats`, `mnemosyne_triple_add`, `mnemosyne_triple_query`,
`mnemosyne_scratchpad_write`, `mnemosyne_scratchpad_read`, and more.

Data is stored at `~/.hermes/mnemosyne/data/mnemosyne.db`.

## Pitfalls

- **Symlink must point at installed package.** If you install mnemosyne-hermes
  in a different venv, the symlink will be a dead link. Use the correct
  Python interpreter in Step 2.
- **Plugin discovery is at startup.** After symlinking, restart Hermes or
  run `hermes plugins list` to trigger a re-scan. The plugin won't appear
  mid-session.
- **Disable conflicting providers.** If you previously used Honcho, Mem0, or
  another provider, switch `memory.provider` and ensure only one provider
  is active.
- **Minimal install has no local embeddings.** Without `[embeddings]` extra,
  Mnemosyne needs `MNEMOSYNE_EMBEDDING_API_URL` pointing at an external
  embedding service. With the extra, it uses local `fastembed`.

## Verification

```bash
# 1. Package installed
python3 -c "import mnemosyne_hermes; print(mnemosyne_hermes.__file__)"

# 2. Plugin symlinked
ls -la ~/.hermes/plugins/mnemosyne/__init__.py

# 3. Provider active
hermes memory status | grep -q mnemosyne && echo "ACTIVE"

# 4. Tools registered
hermes tools list | grep mnemosyne

# 5. Smoke test
hermes mnemosyne stats
```
