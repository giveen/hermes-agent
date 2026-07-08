"""Minimal plugin adapter loader.

Loads a platform adapter plugin under a unique module name so
sibling tests in the same xdist worker don't collide.
"""

import importlib
import sys
from pathlib import Path


def load_plugin_adapter(plugin_name: str):
    """Load a platform adapter plugin under a unique module name.

    Returns the loaded module.
    """
    plugin_root = Path(__file__).resolve().parent.parent.parent / "plugins" / "platforms" / plugin_name
    if not plugin_root.is_dir():
        raise ImportError(f"Plugin directory not found: {plugin_root}")

    module_name = f"plugin_adapter_{plugin_name}"

    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_root / "adapter.py",
        submodule_search_locations=[],
    )
    if spec is None:
        raise ImportError(f"Cannot load adapter from {plugin_root / 'adapter.py'}")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod
