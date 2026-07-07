"""CLI commands for Mnemosyne memory provider setup."""

from __future__ import annotations

import sys
from pathlib import Path


def register_cli(subparser) -> None:
    """Register ``hermes mnemosyne`` CLI subcommands."""
    p = subparser.add_parser("mnemosyne", help="Mnemosyne memory provider commands")
    p.set_defaults(func=_cmd_mnemosyne)

    subs = p.add_subparsers(dest="mnemosyne_subcommand")

    setup = subs.add_parser("setup", help="Initialize Mnemosyne database and config")
    setup.set_defaults(mnemosyne_subcommand="setup")

    stats = subs.add_parser("stats", help="Show memory statistics")
    stats.set_defaults(mnemosyne_subcommand="stats")

    status = subs.add_parser("status", help="Check Mnemosyne health and config")
    status.set_defaults(mnemosyne_subcommand="status")


def _cmd_mnemosyne(args) -> None:
    sub = getattr(args, "mnemosyne_subcommand", "")
    if sub == "setup":
        _cmd_setup()
    elif sub == "stats":
        _cmd_stats()
    elif sub == "status":
        _cmd_status()
    else:
        print("Usage: hermes mnemosyne [setup|stats|status]")


def _cmd_setup() -> None:
    """Run one-time setup: create DB, verify SDK, configure."""
    print("⚙  Setting up Mnemosyne...")

    # Check SDK
    try:
        import mnemosyne  # noqa: F401
    except ImportError:
        print("  ✗ mnemosyne SDK not found. Install with: pip install mnemosyne-memory")
        sys.exit(1)

    # Create data directory
    from hermes_constants import get_hermes_home

    data_dir = get_hermes_home() / "mnemosyne" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "mnemosyne.db"
    print(f"  ✓ Data directory: {data_dir}")

    # Initialize database
    try:
        _client = mnemosyne.Mnemosyne(db_path=db_path)
        _client.remember(
            content="Mnemosyne memory system initialized",
            source="setup",
            importance=0.1,
        )
        print(f"  ✓ Database: {db_path}")
    except Exception as exc:
        print(f"  ✗ Database init failed: {exc}")
        sys.exit(1)

    # Configure Hermes
    from hermes_cli.config import DEFAULT_CONFIG_PATH, save_config
    from hermes_cli.config import load_config as _load_config

    cfg = _load_config()
    if isinstance(cfg, dict):
        cfg.setdefault("memory", {})["provider"] = "mnemosyne"
        cfg["memory"]["memory_enabled"] = False
        cfg["user_profile_enabled"] = False
        save_config(cfg)
        print("  ✓ Config: memory.provider → mnemosyne")
        print("  ✓ Config: memory.memory_enabled → false")
        print("  ✓ Config: user_profile_enabled → false")

    print()
    print("✅ Mnemosyne setup complete. Restart Hermes to activate.")
    print("   Verify with: hermes memory status")
    print("   Stats:       hermes mnemosyne stats")


def _cmd_stats() -> None:
    """Show memory statistics."""
    from hermes_constants import get_hermes_home

    db_path = get_hermes_home() / "mnemosyne" / "data" / "mnemosyne.db"
    if not db_path.exists():
        print("Mnemosyne database not found. Run: hermes mnemosyne setup")
        return

    try:
        import mnemosyne

        client = mnemosyne.Mnemosyne(db_path=db_path)
        stats = client.get_stats()
        if stats:
            print(f"  Total memories: {stats.get('total_memories', 0)}")
            print(f"  Sessions:       {stats.get('total_sessions', 0)}")
            print(f"  Last memory:    {stats.get('last_memory', '—')}")
            sources = stats.get("sources", {})
            if sources:
                print(f"  Sources:        {sources}")
        else:
            print("No memories yet.")
    except Exception as exc:
        print(f"Stats error: {exc}")


def _cmd_status() -> None:
    """Check Mnemosyne health."""
    from hermes_constants import get_hermes_home

    try:
        import mnemosyne
        print("  ✓ mnemosyne SDK: installed")
    except ImportError:
        print("  ✗ mnemosyne SDK: not installed")
        return

    db_path = get_hermes_home() / "mnemosyne" / "data" / "mnemosyne.db"
    if db_path.exists():
        size = db_path.stat().st_size
        print(f"  ✓ Database: {db_path} ({size:,} bytes)")
    else:
        print(f"  ✗ Database: not found at {db_path}")

    from hermes_cli.config import load_config

    cfg = load_config()
    provider = ((cfg.get("memory", {}) or {}).get("provider") or "").strip()
    if provider == "mnemosyne":
        print("  ✓ Config: memory.provider = mnemosyne")
    else:
        print(f"  ✗ Config: memory.provider = {provider!r} (expected 'mnemosyne')")
