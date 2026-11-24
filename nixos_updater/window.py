"""The GTK4 / libadwaita UI. Layout mirrors the prototype, top to bottom:
status card, flake inputs, working directory, update pipeline, roll back, garbage collection.

Flow: buttons call engine.start(job) -> the engine's worker thread changes engine.s and
calls _on_change -> render() (on the UI thread) copies engine.s onto the widgets.
"""
import re
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

from .config import CHECK_FREQUENCIES, GC_DAYS, GC_FREQUENCIES, default_commands  # noqa: E402
from .engine import Engine  # noqa: E402

CSS = """
/* status card: tinted by tone, built from libadwaita's own colours so light/dark both work */
.status-card { border-radius: 22px; padding: 22px 20px; border: 1px solid transparent; }
.tone-dark { border-color: alpha(@window_fg_color, 0.15); }
.tone-blue { background: alpha(@accent_bg_color, 0.22); }
.tone-green { background: alpha(@success_bg_color, 0.22); }
.tone-red { background: alpha(@error_bg_color, 0.22); }
.badge { min-width: 64px; min-height: 64px; border-radius: 32px; padding: 0; background: alpha(@window_fg_color, 0.2); }
.tone-blue .badge { background: alpha(@accent_bg_color, 0.45); color: @accent_color; }
.tone-green .badge { background: alpha(@success_bg_color, 0.45); color: @success_color; }
.tone-red .badge { background: alpha(@error_bg_color, 0.45); color: @error_color; }
.status-title { font-size: 28px; font-weight: 700; }

/* action row */
.actions button { min-height: 44px; padding: 0 20px; border-radius: 12px; }
.split button:first-child { border-radius: 12px 0 0 12px; }
.split button:last-child { border-radius: 0 12px 12px 0; }
.actions button.pill { border-radius: 999px; padding: 0 24px; font-weight: 700; }
.status-card button.pill { min-height: 44px; padding: 0 20px; }

/* terminal */
.term { border: 1px solid alpha(@window_fg_color, 0.15); border-radius: 20px; padding: 14px 18px; }
.term-head { padding: 0; min-height: 0; }
.term-last { font-family: monospace; font-size: 13.5px; margin-top: 8px; }
.term-log { background: alpha(@window_fg_color, 0.07); border-radius: 12px; margin-top: 10px; }
.terminal, .terminal text { background: transparent; font-family: monospace; font-size: 12.5px; }

.dim-row { opacity: 0.55; }
"""

# (icon, title, tone). While running, the icon/title depend on which step is running.
PHASES = {
    "error":    ("window-close-symbolic", "Error", "red"),
    "ready":    ("3d-box-symbolic", "Ready to Switch", "blue"),
    "updates":  ("document-save-symbolic", "Updates Available", "blue"),
    "restart":  ("system-shutdown-symbolic", "Restart to Finish", "green"),
    "uptodate": ("object-select-symbolic", "Up to Date", "green"),
}
RUNNING = {
    "update":   ("arrows-circle-symbolic", "Updating Flake Inputs"),
    "build":    ("3d-box-symbolic", "Building…"),
    "switch":   ("arrows-circle-symbolic", "Switching…"),
    "rollback": ("edit-undo-symbolic", "Rolling Back…"),
    "gc":       ("user-trash-symbolic", "Collecting Garbage…"),
}
TONES = ["tone-dark", "tone-blue", "tone-green", "tone-red"]
# terminal colours (dark theme, light theme)
INK = {True: {"green": "#86E3A0", "blue": "#80B3F4", "red": "#FF8C80"},
       False: {"green": "#1B7A3C", "blue": "#1B5CC0", "red": "#B3261E"}}
BUILT_PREFIX = re.compile(r"\[\d+/\d+ built\]")


ICON_FALLBACK = {"3d-box-symbolic": "package-x-generic-symbolic", "arrows-circle-symbolic": "view-refresh-symbolic"}


def icon(name):
    """Icon name, or a stock fallback if the theme doesn't ship it."""
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    return name if theme.has_icon(name) else ICON_FALLBACK.get(name, name)


def phase_view(s, phase):
    if phase == "running":
        return (*RUNNING.get(s.running, ("arrows-circle-symbolic", "Starting…")), "dark")
    return PHASES[phase]


def log_colour(line):
    """Which colour a terminal line gets (None = default)."""
    if line.startswith(("Build finished", "Switch complete", "flake.lock restored")):
        return "green"
    if line.startswith(("Error", "error:")):
        return "red"
    return None


def clock(ts):
    return time.strftime("%H:%M" if time.localtime(ts).tm_yday == time.localtime().tm_yday else "%b %d", time.localtime(ts))


STEP_ICONS = {  # pipeline row status -> (icon, css class)
    "running": ("arrows-circle-symbolic", "accent"),
    "done": ("object-select-symbolic", "success"),
    "failed": ("dialog-error-symbolic", "error"),
}
PIPELINE = [("update", "Update"), ("build", "Build"), ("switch", "Switch")]


def ago(ts):
    if not ts:
        return "never"
    mins = int((time.time() - ts) / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 60 * 24:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def duration(seconds):
    return f"{int(seconds) // 60} m {int(seconds) % 60:02d} s"


def icon_button(icon, tooltip, callback):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER, css_classes=["flat"])
    b.connect("clicked", callback)
    return b


def combo_row(title, labels, selected, callback):
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
    row.set_selected(selected)
    row.connect("notify::selected", lambda r, _p: callback(r.get_selected()))
    return row


class Window(Adw.ApplicationWindow):
    def __init__(self, app, cfg, auto_check=False, prompt=False):
        super().__init__(application=app, title="NixOS Updater", default_width=640, default_height=900)
        self.cfg = cfg
        self.engine = Engine(cfg, self._on_change, self._ask_password,
                             lambda: GLib.idle_add(self._show_switch_dialog))
        self._render_pending = False
        self._log_shown = 0
        self._pulse_id = 0
        self._inputs_sig = None
        self.show_unchanged = False

        # Ship the GNOME Icon Library symbolics the theme lacks (new-format SVGs: need GTK >= 4.20 to recolour) (data/icons/hicolor/...). Added as
        # a search path, so an icon the user's theme already has still wins.
        icons = Path(__file__).resolve().parent.parent / "data" / "icons"
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons))
        Gtk.Window.set_default_icon_name("dev.local.NixosUpdater")   # the app icon (data/icons/.../scalable/apps)

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Page = header bar + scrolled column of sections.
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                       margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)
        for section in (self._build_status(), self._build_inputs(), self._build_pipeline(),
                        self._build_options(), self._build_rollback(), self._build_gc()):
            page.append(section)
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=640, child=page)))
        view.add_top_bar(Adw.HeaderBar())
        self.set_content(view)

        self.render()
        if auto_check:
            GLib.idle_add(lambda: self.engine.start("check"))
        if prompt:
            GLib.idle_add(self._prompt_pending)

    # ====================================================================
    # Top section: status card, action buttons, terminal
    # ====================================================================
    def _build_status(self):
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        top.append(self._build_card())
        top.append(self._build_actions())
        top.append(self._build_terminal())
        return top

    def _build_card(self):
        # The card hugs its contents. The progress bar and the two pill buttons sit in Revealers, so when
        # they appear/disappear the card's height animates instead of jumping. (spacing=0: a hidden Revealer
        # still counts as a child, so gaps come from margins on the widgets instead.)
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["status-card", "tone-dark"])
        # The badge is a fixed 64x64 circle: explicit expand=False so the card can't stretch it,
        # and the image fills it (a GtkImage centres its icon in whatever space it gets).
        self.status_icon = Gtk.Image(pixel_size=34, hexpand=True, vexpand=True)
        badge = Gtk.Box(css_classes=["badge"], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                        hexpand=False, vexpand=False, margin_bottom=8)
        badge.set_size_request(64, 64)
        badge.append(self.status_icon)
        self.status_title = Gtk.Label(css_classes=["status-title"])
        self.status_sub = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER,
                                    margin_top=6)
        self.progress = Gtk.ProgressBar(halign=Gtk.Align.CENTER, margin_top=10)
        self.progress.set_size_request(280, -1)
        self.btn_cancel = Gtk.Button(label="Cancel", css_classes=["pill"], halign=Gtk.Align.CENTER, margin_top=8)
        self.btn_cancel.connect("clicked", lambda _b: self.engine.cancel())
        self.btn_review = Gtk.Button(label="Review & Switch", css_classes=["pill", "suggested-action"],
                                     halign=Gtk.Align.CENTER, margin_top=8)
        self.btn_review.connect("clicked", lambda _b: self._show_switch_dialog())
        def revealed(child):
            return Gtk.Revealer(child=child, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                transition_duration=250)
        self.progress_rev, self.cancel_rev, self.review_rev = (
            revealed(self.progress), revealed(self.btn_cancel), revealed(self.btn_review))
        for w in (badge, self.status_title, self.status_sub, self.progress_rev, self.cancel_rev, self.review_rev):
            self.card.append(w)
        return self.card

    def _build_actions(self):
        """[Check for Updates | Roll Back]  |  Rebuild   Rebuild & Switch"""
        def button(label, icon_name, job, classes=()):
            b = Gtk.Button(child=Adw.ButtonContent(label=label, icon_name=icon(icon_name)), css_classes=list(classes))
            b.connect("clicked", lambda _b: self.engine.start(job))
            return b
        self.btn_check = button("Check for Updates", "arrows-circle-symbolic", "check")
        self.btn_rollback = button("Roll Back", "edit-undo-symbolic", "rollback")
        self.btn_rebuild = button("Rebuild", "3d-box-symbolic", "rebuild")
        self.btn_switch = button("Rebuild & Switch", "media-playback-start-symbolic", "rebuild_switch",
                                 ["pill", "suggested-action"])
        split = Gtk.Box(css_classes=["linked", "split"])
        split.append(self.btn_check)
        split.append(self.btn_rollback)
        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, css_classes=["actions"])
        for w in (split, Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_top=2, margin_bottom=2),
                  self.btn_rebuild, self.btn_switch):
            row.append(w)
        return row

    def _build_terminal(self):
        """Hidden until the first command runs. Collapsed: last output line. Expanded: full log (slides open)."""
        self.terminal = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["term"], visible=False)
        self.term_word = Gtk.Label(label="Expand")
        self.term_chevron = Gtk.Image(icon_name="pan-down-symbolic")
        head_box = Gtk.Box(spacing=6)
        head_box.append(Gtk.Label(label="Terminal", hexpand=True, xalign=0))
        head_box.append(self.term_word)
        head_box.append(self.term_chevron)
        head = Gtk.Button(child=head_box, css_classes=["flat", "term-head"])
        head.connect("clicked", self._toggle_terminal)

        self.last_line = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["term-last"])
        self.last_revealer = Gtk.Revealer(child=self.last_line, reveal_child=True, transition_duration=280,
                                          transition_type=Gtk.RevealerTransitionType.SLIDE_UP)

        self.log_buffer = Gtk.TextBuffer()
        self.log_tags = {name: self.log_buffer.create_tag(name) for name in ("green", "blue", "red")}
        self.log_view = Gtk.TextView(buffer=self.log_buffer, editable=False, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["terminal"])
        log_scroll = Gtk.ScrolledWindow(child=self.log_view, min_content_height=240, max_content_height=280,
                                        propagate_natural_height=True, css_classes=["term-log"])
        self.log_revealer = Gtk.Revealer(child=log_scroll, transition_duration=280,
                                         transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.term_open = False
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self._recolour_terminal())
        self._recolour_terminal()

        for w in (head, self.last_revealer, self.log_revealer):
            self.terminal.append(w)
        return self.terminal

    def _recolour_terminal(self):
        ink = INK[Adw.StyleManager.get_default().get_dark()]
        for name, tag in self.log_tags.items():
            tag.set_property("foreground", ink[name])
        self._render_last_line()

    def _toggle_terminal(self, *_):
        self.term_open = not self.term_open
        self.last_revealer.set_reveal_child(not self.term_open)
        self.log_revealer.set_reveal_child(self.term_open)
        self.term_word.set_label("Collapse" if self.term_open else "Expand")
        self.term_chevron.set_from_icon_name("pan-up-symbolic" if self.term_open else "pan-down-symbolic")

    # ====================================================================
    # Flake inputs: changed rows always visible, the rest behind a bottom toggle row
    # ====================================================================
    def _build_inputs(self):
        self.inputs_group = Adw.PreferencesGroup(title="Flake Inputs")
        self.input_rows = []
        self.toggle_row = Adw.ActionRow(title="All inputs", activatable=True)
        self.toggle_row.add_css_class("dim-row")
        self.toggle_chevron = Gtk.Image(icon_name="pan-down-symbolic")
        self.toggle_row.add_suffix(self.toggle_chevron)
        self.toggle_row.connect("activated", self._toggle_unchanged)
        return self.inputs_group

    def _toggle_unchanged(self, *_):
        self.show_unchanged = not self.show_unchanged
        self.render()

    def _render_inputs(self, s):
        sig = (tuple((i.name, i.old, i.new) for i in s.inputs), self.show_unchanged)
        if sig == self._inputs_sig:
            return
        self._inputs_sig = sig
        for row in self.input_rows + [self.toggle_row]:
            if row.get_parent():
                self.inputs_group.remove(row)
        self.input_rows = []
        changed = [i for i in s.inputs if i.changed]
        unchanged = [i for i in s.inputs if not i.changed]
        for i in changed + unchanged:
            row = Adw.ActionRow(title=i.name, use_markup=False)
            if i.changed:
                row.set_subtitle(f"{i.old[:7] or 'new'} → {i.new[:7]}")
                row.add_prefix(Gtk.Image(icon_name="software-update-available-symbolic", css_classes=["accent"]))
            else:
                row.set_subtitle(i.new[:7])
                row.add_css_class("dim-row")
                row.set_visible(self.show_unchanged)
            self.inputs_group.add(row)
            self.input_rows.append(row)
        if unchanged:
            self.toggle_row.set_subtitle(f"{len(unchanged)} unchanged")
            self.toggle_chevron.set_from_icon_name("pan-up-symbolic" if self.show_unchanged else "pan-down-symbolic")
            self.inputs_group.add(self.toggle_row)
        self.inputs_group.set_description(
            f"{len(changed)} updated" if changed else ("" if s.inputs else "No flake.lock found in the working directory"))

    # ====================================================================
    # Working directory + check frequency
    # ====================================================================
    def _build_directory(self):
        """The directory row. It lives at the top of the Update Pipeline group."""
        self.dir_row = Adw.EntryRow(title="Directory", text=self.cfg.directory, show_apply_button=True)
        self.dir_row.add_suffix(icon_button("folder-open-symbolic", "Choose folder…", self._pick_folder))
        self.dir_row.connect("apply", self._apply_directory)
        self._lock_if_managed(self.dir_row, "directory")
        return self.dir_row

    def _build_options(self):
        group = Adw.PreferencesGroup(title="Update Options")
        self.auto_update_row = Adw.SwitchRow(title="Automatic Updates", active=self.cfg.auto_update,
                                             subtitle="Check in the background on the schedule below")
        self.auto_update_row.connect("notify::active", self._set_auto_update)

        def set_freq(i):
            self.cfg.check_frequency = CHECK_FREQUENCIES[i]
            self.cfg.save()
        self.freq_row = combo_row("Update Frequency", [f.capitalize() for f in CHECK_FREQUENCIES],
                                  CHECK_FREQUENCIES.index(self.cfg.check_frequency), set_freq)
        auto_build = Adw.SwitchRow(title="Build After Updating", active=self.cfg.auto_build,
                                   subtitle="Start building as soon as an update finds changes")
        auto_build.connect("notify::active", self._set_auto_build)

        for row, key in ((self.auto_update_row, "auto_update"), (self.freq_row, "check_frequency"),
                         (auto_build, "auto_build")):
            self._lock_if_managed(row, key)
            group.add(row)
        self._update_freq_sensitivity()
        return group

    def _set_auto_update(self, row, _param):
        self.cfg.auto_update = row.get_active()
        self.cfg.save()
        self._update_freq_sensitivity()

    def _update_freq_sensitivity(self):
        """The frequency only matters while automatic updates are on (and isn't fixed by the module)."""
        self.freq_row.set_sensitive(self.cfg.auto_update and not self.cfg.is_managed("check_frequency"))

    def _set_auto_build(self, row, _param):
        self.cfg.auto_build = row.get_active()
        self.cfg.save()

    def _lock_if_managed(self, row, key):
        """Settings fixed in the NixOS module can't be changed here."""
        if self.cfg.is_managed(key):
            row.set_sensitive(False)
            row.set_tooltip_text("Set by your NixOS configuration")
            if hasattr(row, "set_subtitle"):
                row.set_subtitle("Set by your NixOS configuration")

    def _pick_folder(self, *_):
        Gtk.FileDialog(title="Choose flake directory").select_folder(self, None, self._folder_picked)

    def _folder_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:  # dismissed
            return
        self.dir_row.set_text(folder.get_path())
        self._apply_directory()

    def _apply_directory(self, *_):
        self.cfg.directory = self.dir_row.get_text().strip()
        self.cfg.save()
        defaults = default_commands(self.cfg.directory)   # un-edited commands follow the directory
        for key, (row, _img, _reset) in self.cmd_rows.items():
            if key not in self.cfg.custom:
                row.set_text(defaults[key])
        self.engine.refresh()
        self.render()

    # ====================================================================
    # Update pipeline: one editable command per step
    # ====================================================================
    def _build_pipeline(self):
        group = Adw.PreferencesGroup(title="Update Pipeline",
                                     description="Commands run with bash in the working directory. "
                                                 "`sudo` asks for your password in a dialog.")
        self.cmd_rows = {}
        group.add(self._build_directory())
        for key, title in PIPELINE:
            self.cmd_rows[key] = self._command_row(group, key, title)
        return group

    def _command_row(self, group, key, title, extra_suffix=None):
        row = Adw.EntryRow(title=title, text=self.cfg.command(key), show_apply_button=True)
        status = Gtk.Image(visible=False)
        reset = icon_button("edit-undo-symbolic", "Reset to default", lambda _b: self._reset_command(key))
        reset.set_visible(key in self.cfg.custom)
        if extra_suffix:
            row.add_suffix(extra_suffix)
        for w in (status, reset):
            row.add_suffix(w)
        row.connect("apply", lambda r: self._save_command(key, r.get_text().strip()))
        group.add(row)
        return row, status, reset

    def _save_command(self, key, text):
        if not text or text == default_commands(self.cfg.directory)[key]:
            self.cfg.custom.pop(key, None)
        else:
            self.cfg.custom[key] = text
        self.cfg.save()
        self.cmd_rows[key][2].set_visible(key in self.cfg.custom)

    def _reset_command(self, key):
        self.cfg.custom.pop(key, None)
        self.cfg.save()
        row, _img, reset = self.cmd_rows[key]
        row.set_text(self.cfg.command(key))
        reset.set_visible(False)

    # ====================================================================
    # Roll back: its own section, since it isn't part of the pipeline
    # ====================================================================
    def _build_rollback(self):
        group = Adw.PreferencesGroup(title="Roll Back",
                                     description="The Roll Back button runs this to restore flake.lock "
                                                 "to what it was at the last successful switch.")
        self.cmd_rows["rollback"] = self._command_row(group, "rollback", "Rollback command")
        return group

    # ====================================================================
    # Garbage collection
    # ====================================================================
    def _build_gc(self):
        group = Adw.PreferencesGroup(title="Garbage Collection",
                                     description="Runs after a successful switch, when it is due.")
        enabled = Adw.SwitchRow(title="Collect garbage automatically", active=self.cfg.gc_enabled)

        def set_enabled(row, _p):
            self.cfg.gc_enabled = row.get_active()
            self.cfg.save()
        enabled.connect("notify::active", set_enabled)
        group.add(enabled)

        def set_freq(i):
            self.cfg.gc_frequency = GC_FREQUENCIES[i]
            self.cfg.save()

        def set_days(i):
            self.cfg.gc_days = GC_DAYS[i]
            self.cfg.save()
        for row in (combo_row("Frequency", [f.capitalize() for f in GC_FREQUENCIES],
                              GC_FREQUENCIES.index(self.cfg.gc_frequency), set_freq),
                    combo_row("Delete Older Than", [f"{d} days" for d in GC_DAYS],
                              GC_DAYS.index(self.cfg.gc_days) if self.cfg.gc_days in GC_DAYS else 2, set_days)):
            enabled.bind_property("active", row, "sensitive", GObject.BindingFlags.SYNC_CREATE)
            group.add(row)
        return group

    # ====================================================================
    # Rendering: engine state -> widgets. Always on the UI thread.
    # ====================================================================
    def _on_change(self):
        """Called from any thread. Coalesces bursts of output into one redraw."""
        if not self._render_pending:
            self._render_pending = True
            GLib.idle_add(self._render_idle)

    def _render_idle(self):
        self._render_pending = False
        self.render()
        return GLib.SOURCE_REMOVE

    def render(self):
        s = self.engine.s
        phase = s.phase()
        icon_name, title, tone = phase_view(s, phase)
        running = phase == "running"

        self.status_icon.set_from_icon_name(icon(icon_name))
        self.status_title.set_label(title)
        self.status_sub.set_label(self._subtitle(s, phase))
        for cls in TONES:
            self.card.remove_css_class(cls)
        self.card.add_css_class(f"tone-{tone}")

        self.cancel_rev.set_reveal_child(running and s.running != "rollback")
        self.review_rev.set_reveal_child(phase == "ready")
        show_progress = running and s.running == "build"
        self.progress_rev.set_reveal_child(show_progress)
        if show_progress:
            if s.progress and s.progress[1]:
                self.progress.set_fraction(s.progress[0] / s.progress[1])
            elif not self._pulse_id:
                self._pulse_id = GLib.timeout_add(200, self._pulse)
        for b in (self.btn_check, self.btn_rebuild, self.btn_switch):
            b.set_sensitive(not running)
        self.btn_rollback.set_sensitive(s.rollback_available and not running)

        self._render_log(s)
        self._render_inputs(s)
        for key, (_row, img, _reset) in self.cmd_rows.items():
            state = s.steps.get(key, "idle")
            img.set_visible(state in STEP_ICONS)
            if state in STEP_ICONS:
                img.set_from_icon_name(icon(STEP_ICONS[state][0]))
                img.set_css_classes([STEP_ICONS[state][1]])

    def _pulse(self):
        """Keeps the progress bar moving until the first [n/m built] line arrives."""
        s = self.engine.s
        if s.running == "build" and not s.progress:
            self.progress.pulse()
            return GLib.SOURCE_CONTINUE
        self._pulse_id = 0
        return GLib.SOURCE_REMOVE

    def _subtitle(self, s, phase):
        gen = f"Generation {s.generation or '?'}"
        if phase == "running":
            return {"update": f"Running {self.cfg.command('update')}",
                    "build": (f"Built {s.progress[0]} of {s.progress[1]} derivations" if s.progress else "Starting build…"),
                    "switch": f"Activating generation {s.new_generation or ''}",
                    "rollback": "Restoring flake.lock"}.get(s.running, "")
        if phase == "error":
            return s.error
        if phase == "updates":
            n = sum(i.changed for i in s.inputs)
            return f"{n} input{'' if n == 1 else 's'} updated · {gen} · Linux {s.kernel}"
        if phase == "ready":
            kernel = f" · Linux {s.new_kernel}" if s.new_kernel not in ("?", s.kernel) else ""
            return f"Generation {s.new_generation} is built and waiting{kernel}"
        if phase == "restart":
            return f"{gen} · Restart to run Linux {s.kernel}"
        checked = f" · Checked {clock(self.cfg.last_check)}" if self.cfg.last_check else ""
        return f"{gen} · Linux {s.kernel}{checked}"

    def _render_log(self, s):
        if len(s.log) < self._log_shown:        # a new job started: start the buffer over
            self.log_buffer.set_text("")
            self._log_shown = 0
        new = s.log[self._log_shown:]
        for line in new:
            self._append_log_line(line)
        if new:
            self._log_shown = len(s.log)
            self.log_buffer.place_cursor(self.log_buffer.get_end_iter())
            self.log_view.scroll_mark_onscreen(self.log_buffer.get_insert())
            self.terminal.set_visible(True)   # first output: the terminal appears (and stays)
            self._render_last_line()

    def _append_log_line(self, line):
        buf = self.log_buffer
        if m := BUILT_PREFIX.match(line):       # "[4/15 built]" in blue
            buf.insert_with_tags_by_name(buf.get_end_iter(), m.group(0), "blue")
            line = line[m.end():]
        colour = log_colour(line)
        if colour:
            buf.insert_with_tags_by_name(buf.get_end_iter(), line + "\n", colour)
        else:
            buf.insert(buf.get_end_iter(), line + "\n")

    def _render_last_line(self):
        log = self.engine.s.log
        if not log:
            return
        ink = INK[Adw.StyleManager.get_default().get_dark()]
        line, markup = log[-1], ""
        if m := BUILT_PREFIX.match(line):
            markup = f'<span foreground="{ink["blue"]}">{GLib.markup_escape_text(m.group(0))}</span>'
            line = line[m.end():]
        colour = log_colour(line)
        text = GLib.markup_escape_text(line)
        markup += f'<span foreground="{ink[colour]}">{text}</span>' if colour else text
        self.last_line.set_markup(markup)

    # ====================================================================
    # Dialogs: sudo password (called from the worker thread) and "switch now?"
    # ====================================================================
    def _ask_password(self):
        """Runs on the worker thread: shows the dialog on the UI thread and waits for the answer."""
        done, answer = threading.Event(), {}
        GLib.idle_add(self._show_password_dialog, done, answer)
        done.wait()
        return answer.get("password")

    def _show_password_dialog(self, done, answer):
        entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
        dialog = Adw.AlertDialog.new("Authentication Required", "Enter your password to run commands with sudo.")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "ok":
                answer["password"] = entry.get_text()
            done.set()
        dialog.connect("response", on_response)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def _prompt_pending(self):
        """Opened from a notification: go straight to the confirmation that is waiting."""
        phase = self.engine.s.phase()
        if phase == "ready":
            self._show_switch_dialog()
        elif phase == "updates":
            self._show_build_dialog()
        return GLib.SOURCE_REMOVE

    def _show_build_dialog(self):
        n = sum(i.changed for i in self.engine.s.inputs)
        dialog = Adw.AlertDialog.new("Rebuild & Switch?", f"{n} flake input{'s' if n != 1 else ''} changed since the "
                                     "last switch. This builds the new system, then asks you to confirm before switching.")
        dialog.add_response("later", "Not now")
        dialog.add_response("build", "Rebuild & Switch")
        dialog.set_response_appearance("build", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("build")
        dialog.set_close_response("later")
        dialog.connect("response", lambda _d, r: self.engine.start("rebuild_switch") if r == "build" else None)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def _show_switch_dialog(self):
        s = self.engine.s
        lines = [f"Generation {s.generation} → {s.new_generation}", f"Kernel {s.kernel} → {s.new_kernel}"]
        if s.packages:
            c, a, r = s.packages
            lines.append(f"Packages: {c} changed · {a} added · {r} removed")
        if s.new_kernel != s.kernel:
            lines.append("\nThe kernel changed: reboot after switching to run it.")
        dialog = Adw.AlertDialog.new(f"Switch to generation {s.new_generation}?", "\n".join(lines))
        dialog.add_response("later", "Not now")
        dialog.add_response("boot", "On next boot")
        dialog.add_response("now", "Switch now")
        dialog.set_response_appearance("now", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("now")
        dialog.set_close_response("later")
        dialog.connect("response", lambda _d, r: {"now": lambda: self.engine.start("switch"),
                                                  "boot": lambda: self.engine.start("switch_boot")}.get(r, lambda: None)())
        dialog.present(self)
        return GLib.SOURCE_REMOVE


class App(Adw.Application):
    def __init__(self, cfg, auto_check=False, prompt=False):
        super().__init__(application_id="dev.local.NixosUpdater")
        self.cfg, self.auto_check, self.prompt = cfg, auto_check, prompt

    def do_activate(self):
        (self.props.active_window or Window(self, self.cfg, self.auto_check, self.prompt)).present()
