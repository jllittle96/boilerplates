"""Optional Textual front end for boilerplates.

Install with ``pip install 'boilerplates[tui]'`` and launch with ``boilerplates tui``.
The ``service`` module has no Textual dependency, so it can be tested without it.
"""

from __future__ import annotations


def run() -> None:
    """Start the TUI. Imported lazily so the CLI works without Textual installed."""
    from .app import BoilerplatesTUI

    BoilerplatesTUI().run()
