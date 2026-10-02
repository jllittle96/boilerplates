"""UI-agnostic adapter over boilerplates' core classes.

Everything the TUI needs (list kinds, load templates, edit variables, render, write) goes
through here. It imports only ``cli.core`` public classes and never Typer or Rich prompts,
so a different front end (web, GUI) could reuse it unchanged.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cli.core.config import ConfigManager
from cli.core.exceptions import BoilerplatesError
from cli.core.library import LibraryManager
from cli.core.template import Template, Variable, VariableSection

logger = logging.getLogger(__name__)


@dataclass
class KindInfo:
    name: str
    description: str


@dataclass
class RenderResult:
    files: dict[str, str] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class TemplateService:
    """Discovers kinds and templates, and opens editing sessions."""

    def __init__(self, libraries: Any | None = None, config: ConfigManager | None = None) -> None:
        # Both are injectable so tests can point at a temporary library.
        self.libraries = libraries or LibraryManager()
        self.config = config or ConfigManager()
        self.load_errors: list[str] = []

    def kinds(self) -> list[KindInfo]:
        """List registered module kinds (compose, terraform, ...).

        Reuses the CLI's own discovery: import every cli.modules.* package so each one
        registers itself, then read the registry.
        """
        import cli.modules
        from cli.core.registry import registry

        for mod in pkgutil.iter_modules(cli.modules.__path__):
            importlib.import_module(f"cli.modules.{mod.name}")
        return [KindInfo(name, getattr(cls, "description", "")) for name, cls in registry.iter_module_classes()]

    def templates(self, kind: str) -> list[Template]:
        """Load every non-draft template of a kind. Bad templates are skipped and recorded."""
        self.load_errors = []
        templates: list[Template] = []
        try:
            entries = self.libraries.find(kind, sort_results=True)
        except BoilerplatesError as exc:
            self.load_errors.append(str(exc))
            return templates

        for entry in entries:
            template_dir, library_name = entry[0], entry[1]
            needs_qualification = entry[2] if len(entry) > 2 else False
            library = next((lib for lib in self.libraries.libraries if lib.name == library_name), None)
            try:
                template = Template(
                    template_dir,
                    library_name=library_name,
                    library_type=library.library_type if library else "git",
                )
                if needs_qualification:
                    template.set_qualified_id()
                templates.append(template)
            except BoilerplatesError as exc:
                self.load_errors.append(f"{template_dir.name}: {exc}")
        return templates

    def open(self, kind: str, template: Template) -> TemplateSession:
        session = TemplateSession(kind, template)
        defaults = self.config.get_defaults(kind)
        if defaults and template.variables:
            template.variables.apply_defaults(defaults, "config")
        return session


class TemplateSession:
    """One template being edited: variable values in, rendered files out."""

    def __init__(self, kind: str, template: Template) -> None:
        self.kind = kind
        self.template = template
        self.variables = template.variables
        self._by_name: dict[str, Variable] = {
            var.name: var for section in self.sections() for var in section.variables.values()
        }
        self._pinned = False

    # --- structure -------------------------------------------------------------------------

    def sections(self) -> list[VariableSection]:
        return list(self.variables.get_sections().values()) if self.variables else []

    def variable(self, name: str) -> Variable:
        return self._by_name[name]

    def section_visible(self, section: VariableSection) -> bool:
        return self.variables.is_section_satisfied(section.key)

    def variable_visible(self, section: VariableSection, var: Variable) -> bool:
        """Show a variable when its own `needs` hold and its section is on.

        The section's toggle variable and required variables stay visible even when the
        section is switched off, otherwise the user could not switch it back on.
        """
        if not self.variables.is_variable_satisfied(var.name):
            return False
        return section.is_enabled() or var.name == section.toggle or var.required

    # --- editing ---------------------------------------------------------------------------

    def set_value(self, name: str, raw: Any) -> str | None:
        """Set a variable from raw widget input. Returns an error message, or None if OK."""
        var = self._by_name[name]
        try:
            var.value = var.validate_and_convert(raw, check_required=False)
        except (ValueError, BoilerplatesError) as exc:
            return str(exc)
        return None

    def errors(self) -> list[str]:
        """Whole-form validation (required fields etc.), using the core's own rules."""
        try:
            self.variables.validate_all()
        except ValueError as exc:
            return [str(exc)]
        return []

    # --- output ----------------------------------------------------------------------------

    def render(self) -> RenderResult:
        problems = self.errors()
        if problems:
            return RenderResult(error="\n".join(problems))
        try:
            files, values = self.template.render(self.variables)
        except BoilerplatesError as exc:
            return RenderResult(error=str(exc))
        self._pin_generated(values)
        return RenderResult(files=files, values=values)

    def _pin_generated(self, values: dict[str, Any]) -> None:
        """Keep autogenerated secrets stable across re-renders.

        Template.render() draws fresh random secrets into a local dict on every call, so a
        live preview would show new passwords on each keystroke, and the file you write
        would differ from the one you reviewed. After the first render we store the
        generated values on the variables, so preview and written output always match.
        """
        if self._pinned:
            return
        for var in self._by_name.values():
            if var.autogenerated and not var.value and values.get(var.name):
                var.value = values[var.name]
        self._pinned = True

    def conflicts(self, dest: Path, files: dict[str, str]) -> list[str]:
        return sorted(rel for rel in files if (dest / rel).exists())

    def write(self, dest: Path, files: dict[str, str]) -> list[Path]:
        """Write rendered files under dest. Refuses any path that escapes dest."""
        root = dest.expanduser().resolve()
        written: list[Path] = []
        for rel, content in files.items():
            target = (root / rel).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"Refusing to write outside the output folder: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(target)
        return written
