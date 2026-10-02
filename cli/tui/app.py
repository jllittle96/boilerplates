"""Textual front end. Three screens: browse -> edit variables with live preview -> write dialog.

Written against Textual 1.x-style APIs. Pin and test against the version you ship.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    Select,
    Static,
    Switch,
    TextArea,
)
from textual.widgets.option_list import Option

from .service import RenderResult, TemplateService, TemplateSession

# TextArea highlighting only works for languages Textual ships a grammar for.
_LANG_BY_SUFFIX = {".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml", ".md": "markdown", ".sh": "bash", ".py": "python"}


def _describe(template: Any) -> str:
    """Markdown shown in the detail pane of the browser."""
    meta = template.metadata
    lines = [f"# {meta.name or template.id}", "", f"`{template.id}` · library `{meta.library}`"]
    if meta.version:
        lines.append(f"Version: {meta.version}")
    if meta.tags:
        lines.append("Tags: " + ", ".join(meta.tags))
    lines += ["", meta.description]
    return "\n".join(lines)


# =============================================================================================
# Screen 1: browse
# =============================================================================================
class BrowserScreen(Screen):
    BINDINGS = [Binding("/", "focus_search", "Search"), Binding("enter", "open", "Open", priority=False)]

    DEFAULT_CSS = """
    BrowserScreen #kinds { width: 22; border-right: solid $primary; }
    BrowserScreen #middle { width: 1fr; }
    BrowserScreen #detail { width: 1fr; border-left: solid $primary; padding: 0 1; }
    BrowserScreen DataTable { height: 1fr; }
    """

    def __init__(self, service: TemplateService) -> None:
        super().__init__()
        self.service = service
        self.kind: str | None = None
        self._templates: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="kinds")
            with Vertical(id="middle"):
                yield Input(placeholder="Filter templates (press / to focus)", id="search")
                yield DataTable(id="table", cursor_type="row")
            with VerticalScroll(id="detail"):
                yield Markdown("Select a template.", id="detail-md")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Template", "Name", "Library")
        try:
            kinds = self.service.kinds()
        except Exception as exc:  # discovery imports every module; surface any failure
            self.notify(f"Could not list kinds: {exc}", severity="error")
            return
        self.query_one("#kinds", OptionList).add_options([Option(f"{k.name}", id=k.name) for k in kinds])

    # -- events --
    @on(OptionList.OptionHighlighted, "#kinds")
    def _kind_chosen(self, event: OptionList.OptionHighlighted) -> None:
        self.kind = event.option.id
        self._load(self.kind)

    @on(Input.Changed, "#search")
    def _filter(self, event: Input.Changed) -> None:
        self._fill(event.value)

    @on(DataTable.RowHighlighted, "#table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value in self._templates:
            self.query_one("#detail-md", Markdown).update(_describe(self._templates[event.row_key.value]))

    @on(DataTable.RowSelected, "#table")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    # -- actions --
    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_open(self) -> None:
        table = self.query_one("#table", DataTable)
        if not self.kind or table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        template = self._templates.get(row_key.value)
        if template is not None:
            self.app.push_screen(GenerateScreen(self.service, self.kind, template))

    # -- loading (I/O can be slow when libraries are git checkouts, so keep it off the UI thread) --
    @work(thread=True, exclusive=True)
    def _load(self, kind: str) -> None:
        templates = self.service.templates(kind)
        errors = list(self.service.load_errors)
        self.app.call_from_thread(self._loaded, templates, errors)

    def _loaded(self, templates: list[Any], errors: list[str]) -> None:
        self._templates = {t.id: t for t in templates}
        self._fill(self.query_one("#search", Input).value)
        for message in errors[:3]:
            self.notify(message, severity="warning", timeout=8)

    def _fill(self, needle: str = "") -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        needle = needle.lower().strip()
        for tid, template in self._templates.items():
            hay = f"{tid} {template.metadata.name} {' '.join(template.metadata.tags)}".lower()
            if needle in hay:
                table.add_row(tid, template.metadata.name, template.metadata.library, key=tid)


# =============================================================================================
# Screen 2: variables form + live preview
# =============================================================================================
class GenerateScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+s", "write", "Write files"),
    ]

    DEFAULT_CSS = """
    GenerateScreen #form { width: 1fr; min-width: 40; padding: 0 1; }
    GenerateScreen #preview { width: 2fr; border-left: solid $primary; }
    GenerateScreen #files { height: 8; border-bottom: solid $primary; }
    GenerateScreen #viewer { height: 1fr; }
    GenerateScreen #status { height: auto; padding: 0 1; }
    GenerateScreen .row { height: auto; margin-bottom: 1; }
    GenerateScreen .err { color: $error; height: auto; }
    GenerateScreen .-invalid { border: tall $error; }
    """

    def __init__(self, service: TemplateService, kind: str, template: Any) -> None:
        super().__init__()
        self.session: TemplateSession = service.open(kind, template)
        self.result = RenderResult()
        self._timer: Any = None

    # -- layout --
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with VerticalScroll(id="form"):
                for section in self.session.sections():
                    with Collapsible(title=section.title, id=f"sec-{section.key}"):
                        for var in section.variables.values():
                            with Vertical(classes="row", id=f"row-{var.name}"):
                                yield Label(var.prompt or var.name)
                                yield self._widget_for(var)
                                yield Static("", classes="err", id=f"err-{var.name}")
            with Vertical(id="preview"):
                yield OptionList(id="files")
                yield TextArea("", read_only=True, id="viewer")
                yield Static("", id="status")
        yield Footer()

    def _widget_for(self, var: Any) -> Any:
        """Map a Variable's declared type to a widget. The type vocabulary comes from variable.py."""
        wid = f"var-{var.name}"
        current = var.convert(var.value) if var.value is not None else None

        if var.type == "bool":
            return Switch(value=bool(current), id=wid)
        if var.type == "enum" and var.config.options:
            kwargs: dict[str, Any] = {"allow_blank": not var.required, "id": wid}
            if current in var.config.options:
                kwargs["value"] = current
            return Select([(o, o) for o in var.config.options], **kwargs)
        if var.config.textarea:
            area = TextArea("" if current is None else str(current), id=wid)
            return area
        kind = {"int": "integer", "float": "number"}.get(var.type, "text")
        placeholder = var.config.placeholder or ("auto-generated" if var.autogenerated else "")
        return Input(
            value="" if current is None else str(current),
            placeholder=placeholder,
            password=var.is_secret(),
            type=kind,
            id=wid,
        )

    def on_mount(self) -> None:
        self.title = self.session.template.metadata.name or self.session.template.id
        self._refresh_visibility()
        self._render_now()

    # -- editing: every widget funnels into _edited --
    @on(Input.Changed)
    def _input(self, e: Input.Changed) -> None:
        self._edited(e.input.id, e.value)

    @on(Switch.Changed)
    def _switch(self, e: Switch.Changed) -> None:
        self._edited(e.switch.id, e.value)

    @on(Select.Changed)
    def _select(self, e: Select.Changed) -> None:
        # Blank selection is a sentinel object whose name differs across Textual versions.
        value = e.value if isinstance(e.value, str) else ""
        self._edited(e.select.id, value)

    @on(TextArea.Changed, "#form TextArea")
    def _textarea(self, e: TextArea.Changed) -> None:
        self._edited(e.text_area.id, e.text_area.text)

    def _edited(self, widget_id: str | None, raw: Any) -> None:
        if not widget_id or not widget_id.startswith("var-"):
            return
        name = widget_id[len("var-"):]
        error = self.session.set_value(name, raw)
        self.query_one(f"#err-{name}", Static).update(error or "")
        self.query_one(f"#{widget_id}").set_class(bool(error), "-invalid")
        self._refresh_visibility()
        # Debounce: wait for a pause in typing before re-rendering.
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(0.3, self._render_now)

    def _refresh_visibility(self) -> None:
        """Re-evaluate section toggles and `needs` after any change."""
        for section in self.session.sections():
            self.query_one(f"#sec-{section.key}", Collapsible).display = self.session.section_visible(section)
            for var in section.variables.values():
                self.query_one(f"#row-{var.name}").display = self.session.variable_visible(section, var)

    # -- preview --
    def _render_now(self) -> None:
        self.result = self.session.render()
        files = self.query_one("#files", OptionList)
        viewer = self.query_one("#viewer", TextArea)
        status = self.query_one("#status", Static)
        files.clear_options()
        if not self.result.ok:
            viewer.text = ""
            status.update(f"[red]{self.result.error}[/red]")
            return
        files.add_options([Option(path, id=path) for path in sorted(self.result.files)])
        if self.result.files:
            files.highlighted = 0
        status.update(f"{len(self.result.files)} file(s) ready. Ctrl+S to write.")

    @on(OptionList.OptionHighlighted, "#files")
    def _show_file(self, e: OptionList.OptionHighlighted) -> None:
        viewer = self.query_one("#viewer", TextArea)
        viewer.text = self.result.files.get(e.option.id, "")
        try:
            viewer.language = _LANG_BY_SUFFIX.get(Path(e.option.id).suffix)
        except Exception:
            viewer.language = None  # grammar not installed: plain text is fine

    # -- actions --
    def action_back(self) -> None:
        self.app.pop_screen()

    def action_write(self) -> None:
        if not self.result.ok or not self.result.files:
            self.notify("Nothing valid to write yet.", severity="warning")
            return
        default = Path.cwd() / self.session.template.id
        self.app.push_screen(WriteDialog(self.session, self.result, default), self._after_dialog)

    def _after_dialog(self, dest: Path | None) -> None:
        if dest is None:
            return
        try:
            written = self.session.write(dest, self.result.files)
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Wrote {len(written)} file(s) to {dest}")


# =============================================================================================
# Screen 3: confirm destination
# =============================================================================================
class WriteDialog(ModalScreen[Path]):
    DEFAULT_CSS = """
    WriteDialog { align: center middle; }
    WriteDialog > Vertical { width: 70; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    WriteDialog Horizontal { height: auto; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session: TemplateSession, result: RenderResult, default: Path) -> None:
        super().__init__()
        self.session, self.result, self.default = session, result, default

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Write files to:")
            yield Input(str(self.default), id="dest")
            yield Static("", id="warn")
            with Horizontal():
                yield Button("Write", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._check(str(self.default))

    @on(Input.Changed, "#dest")
    def _changed(self, e: Input.Changed) -> None:
        self._check(e.value)

    def _check(self, raw: str) -> None:
        dest = Path(raw).expanduser()
        clashes = self.session.conflicts(dest, self.result.files)
        self.query_one("#warn", Static).update(
            f"[yellow]{len(clashes)} existing file(s) will be overwritten.[/yellow]" if clashes else ""
        )
        self.query_one("#ok", Button).label = "Overwrite and write" if clashes else "Write"

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(Path(self.query_one("#dest", Input).value).expanduser())

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# =============================================================================================
class BoilerplatesTUI(App):
    TITLE = "boilerplates"
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, service: TemplateService | None = None) -> None:
        super().__init__()
        self.service = service or TemplateService()

    def on_mount(self) -> None:
        try:
            self.theme = "catppuccin-mocha"  # ships with recent Textual releases
        except Exception:
            pass
        self.push_screen(BrowserScreen(self.service))
