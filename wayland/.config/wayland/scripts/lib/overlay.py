"""Reusable GTK4 layer-shell overlay scaffolding.

Every helper in this module is UI-only — anchors a window to the
compositor, wires a close/header row, builds role-labeled cards,
styled pills, collapsibles, and a fuzzy-match command palette.
Script-specific behaviour (business logic, socket plumbing, MCP
hooks, markdown rendering, etc.) stays in the calling script.

Importing this module pulls `gi` / `Gtk` / `Gtk4LayerShell` — don't
import it from subprocesses that run headless (e.g. `pilot.py
mcp-server`). The parent package (`lib/__init__.py`) therefore
does NOT re-export the overlay symbols eagerly; callers reach for
`from lib.overlay import LayerOverlayWindow` explicitly. A lazy
`__getattr__` in the package still makes `from lib import
LayerOverlayWindow` work for convenience."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from enum import Enum
from typing import Any, Callable, Iterable, Optional, Sequence

log = logging.getLogger("lib.overlay")

# ── GTK imports ─────────────────────────────────────────────────
# IMPORTANT: do NOT import `ensure_layer_shell_preload` here. Callers
# must reach it through `lib.layer_shell` directly — importing it via
# `lib.overlay` drags the `gi` module in below, which must only happen
# AFTER LD_PRELOAD has been set by the re-exec.
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import (  # noqa: E402
    Gdk,  # ty: ignore[unresolved-import]
    GLib,  # ty: ignore[unresolved-import]
    Gtk,  # ty: ignore[unresolved-import]
    Gtk4LayerShell,  # ty: ignore[unresolved-import]
)

# ── Monitor helpers ─────────────────────────────────────────────

def focused_monitor_name() -> Optional[str]:
    """Ask the compositor for the connector name of the focused output
    (e.g., 'DP-1', 'HDMI-A-1'). Returns None if neither Hyprland nor
    sway is available or neither answers cleanly."""
    try:
        out = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        ).stdout
        for m in json.loads(out):
            if m.get("focused"):
                return m.get("name")
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        pass
    try:
        out = subprocess.run(
            ["swaymsg", "-t", "get_outputs"],
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        ).stdout
        for o in json.loads(out):
            if o.get("focused"):
                return o.get("name")
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        pass

    return None

def focused_gdk_monitor():
    """Resolve the compositor-focused output to a `GdkMonitor` by
    matching connector names. Returns None on miss."""
    name = focused_monitor_name()
    display = Gdk.Display.get_default()
    if display is None or name is None:
        return None
    monitors = display.get_monitors()
    for i in range(monitors.get_n_items()):
        monitor = monitors.get_item(i)
        if monitor.get_connector() == name:
            return monitor

    return None

# ── CSS pipeline ────────────────────────────────────────────────

def load_css_from_path(path: str, *, tag: str = "overlay.css") -> Gtk.CssProvider:
    """Register `path` (absolute or resolvable) as a Gtk.CssProvider at
    `USER + 1` priority. Parsing errors are routed through `log` so
    bad rules surface in `-v` runs instead of vanishing silently.
    Returns the installed provider so callers can keep a reference if
    they need to swap it out later. `tag` is used purely for log
    messages."""
    with open(path, "rb") as fh:
        css = fh.read()
    provider = Gtk.CssProvider()

    def _on_error(_prov, section, err):
        start = section.get_start_location()
        log.warning(
            "%s parse error at line %d col %d: %s",
            tag,
            start.lines + 1,
            start.line_chars + 1,
            err.message,
        )

    provider.connect("parsing-error", _on_error)
    provider.load_from_data(css, len(css))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
    )

    return provider

def load_overlay_css() -> Gtk.CssProvider:
    """Install the shared `overlay.css` sitting next to this module at
    `USER + 1` priority — same priority pilot.py uses for pilot.css so
    overlay tokens take precedence over generic theme CSS. Callers
    should invoke this BEFORE registering script-specific CSS; the
    later registration wins on ties thanks to CSS cascade order, so
    pilot-specific rules still override the shared base."""
    css_path = os.path.join(os.path.dirname(__file__), "overlay.css")

    return load_css_from_path(css_path, tag="overlay.css")

# ── Layer-shell base window ─────────────────────────────────────

class LayerOverlayWindow(Gtk.ApplicationWindow):
    """Gtk.ApplicationWindow subclass that configures itself as a
    layer-shell surface on construction.

    Subclasses pick anchors, namespace, keyboard mode, and the target
    width fraction via constructor kwargs. The class handles:
      * `Gtk4LayerShell.init_for_window` + namespace / layer / anchors
      * Keyboard mode
      * `.overlay` root CSS class
      * Initial width sizing via `_overlay_width(fraction)`
      * `_bind_to_focused_monitor()` — call on every toggle-to-visible
        so monitor switches rehome the surface.

    This is a pure scaffold — no compose / queue / permission plumbing.
    Callers compose their own widget tree inside `set_child(...)`."""

    def __init__(
        self,
        *,
        application: Gtk.Application,
        title: str = "Overlay",
        namespace: str = "overlay",
        layer: Any = None,
        anchors: Sequence[str] = ("top", "bottom", "right"),
        keyboard_mode: Any = None,
        width_fraction: float = 0.4,
        min_width: int = 320,
        fallback_width: int = 520,
    ):
        super().__init__(application=application, title=title)
        # `.overlay` is the shared root scope — every rule in overlay.css
        # and any script-specific CSS is namespaced under it so theme
        # selectors like `window { background: … }` can't win.
        self.add_css_class("overlay")

        self._namespace = namespace
        self._layer = layer if layer is not None else Gtk4LayerShell.Layer.TOP
        self._anchors = tuple(anchors)
        self._keyboard_mode = (
            keyboard_mode
            if keyboard_mode is not None
            else Gtk4LayerShell.KeyboardMode.EXCLUSIVE
        )
        self._width_fraction = float(width_fraction)
        self._min_width = int(min_width)
        self._fallback_width = int(fallback_width)

        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_namespace(self, namespace)
        Gtk4LayerShell.set_layer(self, self._layer)
        edge_map = {
            "top": Gtk4LayerShell.Edge.TOP,
            "bottom": Gtk4LayerShell.Edge.BOTTOM,
            "left": Gtk4LayerShell.Edge.LEFT,
            "right": Gtk4LayerShell.Edge.RIGHT,
        }
        for anchor in self._anchors:
            edge = edge_map.get(anchor)
            if edge is None:
                log.warning("unknown layer-shell anchor %r — ignored", anchor)
                continue
            Gtk4LayerShell.set_anchor(self, edge, True)
        Gtk4LayerShell.set_keyboard_mode(self, self._keyboard_mode)

        self.set_default_size(self._overlay_width(), -1)

    # -- width / monitor helpers --------------------------------------

    def _overlay_width(
        self,
        fraction: Optional[float] = None,
        monitor=None,
    ) -> int:
        """Fraction of the focused monitor's logical width. Caller can
        pass a specific `monitor` (already resolved); otherwise we
        query the compositor live. Falls back to GDK's first monitor,
        then `fallback_width`, when no info is available."""
        if fraction is None:
            fraction = self._width_fraction
        width = None
        if monitor is None:
            monitor = focused_gdk_monitor()
        if monitor is not None:
            width = monitor.get_geometry().width
        if width is None:
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors.get_n_items() > 0:
                    width = monitors.get_item(0).get_geometry().width
        if not width:
            return self._fallback_width

        return max(self._min_width, int(width * fraction))

    def _bind_to_focused_monitor(self) -> None:
        """Pin the layer-shell surface to whichever output is currently
        focused, resize width to `width_fraction` of that output.
        Called on every toggle-to-visible so monitor switches rehome
        the overlay.

        A layer-shell surface's size follows the widget's requested
        size on commit. `set_default_size` + `set_size_request` +
        `queue_resize` marks the widget tree dirty; the next map cycle
        (toggle hides + shows) then commits a new layer surface with
        the updated dimensions. We do NOT manually call `unmap()` /
        `map()` here — on some builds that emits the `map` signal
        twice, which breaks caller-composed child layouts.

        Subclasses may override to size additional children against
        the newly-bound monitor's geometry (e.g. capping a compose
        scroller at a fraction of the monitor height) — call super
        first to keep the resize path intact."""
        monitor = focused_gdk_monitor()
        if monitor is not None:
            Gtk4LayerShell.set_monitor(self, monitor)
        width = self._overlay_width(monitor=monitor)
        self.set_default_size(width, -1)
        self.set_size_request(width, -1)
        self.queue_resize()
        self._on_monitor_bound(monitor)

    def _on_monitor_bound(self, monitor) -> None:
        """Subclass hook. Called after every `_bind_to_focused_monitor`
        resize with the resolved `GdkMonitor` (may be None if no output
        is discoverable). Default is a no-op. Pilot uses this to cap
        the compose scroller at 25% of the monitor height."""
        return None

# ── Card / collapsible / pill / button helpers ──────────────────

def make_card(role: str, title: str) -> tuple[Gtk.Box, Gtk.Label]:
    """Build the shared card skeleton: a vertical Gtk.Box with an
    `.overlay-card` class plus a role-coded variant, holding a single
    role Label at the top. Returns `(box, role_label)` — the caller
    appends their own content widgets (markdown label, input box,
    etc.) to the Box after the role label.

    Scripts layer extra classes on top (e.g. `pilot-card`,
    `pilot-card-<role>`) for script-specific styling without losing
    the shared structural rules."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
    box.add_css_class("overlay-card")
    box.add_css_class(f"overlay-card-{role}")

    role_label = Gtk.Label(label=title, xalign=0.0)
    role_label.add_css_class("overlay-card-role")
    role_label.add_css_class(f"overlay-card-role-{role}")
    box.append(role_label)

    return box, role_label

def make_collapsible(label: str, child: Gtk.Widget) -> Gtk.Expander:
    """Pre-styled Gtk.Expander for secondary/collapsible content like
    reasoning traces or log snippets. Matches pilot's thinking-block
    look — dim, italicised, expanded by default. The caller can
    re-style individual blocks by adding extra classes on top of the
    base `.overlay-collapsible`."""
    expander = Gtk.Expander(label=label, expanded=True)
    expander.add_css_class("overlay-collapsible")
    expander.set_child(child)

    return expander

class PillVariant(str, Enum):
    """Tokens for `make_pill`. Values match the CSS class names so
    callers can also pass raw strings if preferred."""

    ACCENT = "accent"
    APPROVE = "approve"
    REJECT = "reject"

def make_pill(label: str, variant: str = "accent") -> Gtk.Button:
    """Compose-style pill button. Variant picks the tint:
      * `accent`  — neutral highlight, pairs with hover-to-remove
      * `approve` — green (standing approval / success)
      * `reject`  — red (standing deny)
    Each variant has a matching hover state defined in overlay.css."""
    btn = Gtk.Button(label=label)
    btn.add_css_class("overlay-pill")
    if isinstance(variant, PillVariant):
        variant = variant.value
    btn.add_css_class(variant)

    return btn

class ButtonVariant(str, Enum):
    """Tokens for `make_button`. Values map 1:1 to the CSS classes
    in overlay.css so callers can use either the enum or a raw
    string."""

    ALLOW = "allow"
    TRUST = "trust"
    DENY = "deny"
    AUTOREJECT = "autoreject"

def make_button(label: str, variant: str) -> Gtk.Button:
    """Secondary action button with the shared shadow recipe from
    overlay.css. `variant` picks the hover tint — pairs with
    PillVariant on structure but represents a meatier click target
    (full text + shadow, not a compact pill). Used for permission
    rows (allow/trust/deny/autoreject)."""
    btn = Gtk.Button(label=label)
    btn.add_css_class("overlay-button")
    if isinstance(variant, ButtonVariant):
        variant = variant.value
    btn.add_css_class(variant)

    return btn

# ── Header row ──────────────────────────────────────────────────

class Header(Gtk.Box):
    """Top-row: a provider pill + a close button. Exposes the pill
    label (for title updates) and the pill CSS classes (for phase
    switching — `idle` / `streaming` / `pending` / `awaiting`). The
    close button runs `on_close()` when clicked; callers typically
    wire that to `window.close()`.

    Scripts can layer extra classes on top of `.overlay-header` for
    per-script tweaks (e.g. pilot adds `pilot-header` to keep its
    older selectors working)."""

    _PHASE_CLASSES = ("idle", "pending", "streaming", "awaiting")

    def __init__(
        self,
        title: str,
        on_close: Callable[[], None],
        *,
        extra_classes: Sequence[str] = (),
    ):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class("overlay-header")
        for cls in extra_classes:
            self.add_css_class(cls)

        self._label = Gtk.Label(label=title, xalign=0.0, hexpand=True)
        self._label.add_css_class("overlay-provider")
        self._label.add_css_class("idle")

        close_btn = Gtk.Button(label="✕")
        close_btn.add_css_class("overlay-close")
        close_btn.connect("clicked", lambda _b: on_close())

        self.append(self._label)
        self.append(close_btn)

    @property
    def label(self) -> Gtk.Label:
        """Direct handle on the provider label so callers can add
        script-specific CSS classes or swap the title string in
        place."""
        return self._label

    def set_title(self, title: str) -> None:
        self._label.set_label(title)

    def set_phase(self, phase: str) -> None:
        """Flip the provider pill's phase class. Unknown phases are
        ignored — callers pass one of idle/pending/streaming/
        awaiting (the classes overlay.css paints tints for)."""
        for cls in self._PHASE_CLASSES:
            if cls == phase:
                self._label.add_css_class(cls)
            else:
                self._label.remove_css_class(cls)

# ── Command palette ─────────────────────────────────────────────

CommandPaletteEntry = tuple[str, str, str, str]  # (kind, name, desc, preview)

class CommandPalette(Gtk.Box):
    """Rofi-like fuzzy-match palette. Floats inside a `Gtk.Overlay`
    supplied by the host window — doesn't create its own chrome.

    Flow:
      * `open(entries)` — seed the list; palette grabs keyboard focus.
      * Tab / Shift+Tab    — toggle the highlighted row's active state.
      * Down / Ctrl+N      — next row.
      * Up   / Ctrl+P      — previous row.
      * Enter              — close, call `on_commit(active_entries)`.
      * Escape             — close without committing.

    The palette is intentionally agnostic about WHAT gets committed:
    it hands the caller the tuples it was opened with (filtered to
    whichever entries are toggled active). Callers decide what to do
    with them — inject tokens into a compose box, spawn subcommands,
    etc.

    `preseed_active(entries)` is an optional hook the host can call
    before `open()` to mark a starting subset as active (e.g. because
    the user has already referenced them elsewhere in their input).
    Pass an iterable of `(kind, name)` pairs. After `open()` the set
    persists until the next open."""

    def __init__(
        self,
        host_overlay: Gtk.Overlay,
        on_commit: Callable[[list[CommandPaletteEntry]], None],
        *,
        on_cancel: Optional[Callable[[], None]] = None,
        placeholder: str = "Search — Tab toggles · Enter commits · Esc cancels",
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("overlay-palette")
        # Preserve pilot's older class names so existing CSS keeps
        # landing on the same widgets. Scripts can layer more on top.
        self.add_css_class("pilot-palette")
        # Floating centred panel. CENTER alignment + an explicit
        # size-request on the palette Box itself (set by `set_size`)
        # means the overlay positions us over the middle of the host
        # — same visual pattern as VSCode / rofi / swayncc's command
        # palettes. `.pilot-palette-frame` CSS paints a chunky border
        # + shadow so the panel reads as "floating above the chat"
        # rather than inline with it.
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.add_css_class("pilot-palette-frame")

        self._host_overlay = host_overlay
        self._on_commit = on_commit
        self._on_cancel = on_cancel

        self._entries: list[CommandPaletteEntry] = []
        self._filtered: list[CommandPaletteEntry] = []
        self._active: set[tuple[str, str]] = set()
        self._attached = False

        self._search = Gtk.Entry(placeholder_text=placeholder, hexpand=True)
        self._search.add_css_class("overlay-palette-search")
        self._search.add_css_class("pilot-palette-search")
        self._search.connect("changed", self._on_search_changed)
        # Forward Enter from the entry to the commit path. The palette's
        # own key controller catches Enter at the capture phase when the
        # list has focus, but the entry consumes Return by default.
        self._search.connect("activate", lambda _e: self._commit_and_close())
        self.append(self._search)

        self._scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # Palette should feel generous — we reserve a chunky default
        # min height so small result sets still get a visible scroller
        # frame, and `set_height` lets callers push it to half the
        # overlay when they want extra real estate.
        self._scroller.set_min_content_height(320)
        self._default_height = 320
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        # Keep the Entry as the sole focus target. A focusable ListBox
        # yanks keyboard focus the moment `select_row` fires during
        # `_rebuild_list`, which stole every character past the first
        # from the search input. All keyboard navigation (Tab, Enter,
        # arrows) already routes through the palette's `_on_key`, so
        # the listbox never needs keyboard focus of its own.
        self._listbox.set_can_focus(False)
        self._listbox.add_css_class("overlay-palette-list")
        self._listbox.add_css_class("pilot-palette-list")
        self._scroller.set_child(self._listbox)
        self.append(self._scroller)

        # Capture-phase controller so Tab / Enter / Escape beat the
        # default Entry / ListBox bindings. `_on_key` pivots on the
        # palette state and returns True to stop propagation whenever
        # it handled the key.
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    # ── lifecycle ──
    def preseed_active(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Mark `(kind, name)` tuples as initially active. Unknown
        pairs (not present in the next `open()` entries list) are
        silently dropped when the list rebuilds."""
        self._active = set(pairs)

    def set_size(self, width: int, height: int) -> None:
        """Force the palette to `(width, height)` via `set_size_request`
        on the Box itself AND on the inner scroller. Setting only the
        scroller's min_content_height was a hint the Gtk.Overlay was
        free to ignore — requesting on BOTH widgets guarantees the
        floating panel actually takes the requested footprint instead
        of collapsing to natural content size."""
        if height <= 0:
            height = self._default_height
        if width <= 0:
            width = -1
        self.set_size_request(width, height)
        body = max(0, height - 60)  # leave room for the Entry + padding
        self._scroller.set_min_content_height(body)
        self._scroller.set_max_content_height(body)

    def is_open(self) -> bool:
        """True between `open()` and `close()`. Prefer this over
        `get_visible()` for key-dispatch decisions — the attachment
        state flips synchronously, while GTK's visibility can lag."""
        return self._attached

    def open(self, entries: Sequence[CommandPaletteEntry]) -> None:
        """Populate with `entries`, keep any preseeded active pairs
        that survive the new entry set, then add ourselves to the
        host overlay and focus the search entry. Safe to call
        repeatedly — each call reseeds the list."""
        self._entries = list(entries)
        known = {(k, n) for k, n, _d, _p in self._entries}
        self._active = {pair for pair in self._active if pair in known}
        self._search.set_text("")
        self._rebuild_list()
        if not self._attached:
            self._host_overlay.add_overlay(self)
            self._attached = True
        self.set_visible(True)
        # Idle-add the focus so GTK has finished the overlay
        # attachment by the time we reach for the entry. The Entry
        # re-selects its text on a plain `grab_focus()`, so use
        # `grab_focus_without_selecting()` to keep the cursor at the
        # end without wiping whatever the user typed pre-open.
        def _claim_focus() -> bool:
            self._search.grab_focus_without_selecting()
            return False

        GLib.idle_add(_claim_focus)

    def close(self) -> None:
        """Hide + detach without committing. Stateful widgets keep
        their contents — the next `open()` call re-seeds them from
        scratch. Calls the optional `on_cancel` so hosts can reclaim
        focus (e.g. pilot refocuses the compose textview)."""
        self.set_visible(False)
        if self._attached:
            self._host_overlay.remove_overlay(self)
            self._attached = False
        if self._on_cancel is not None:
            try:
                self._on_cancel()
            except Exception:
                log.exception("palette on_cancel raised")

    def active_entries(self) -> list[CommandPaletteEntry]:
        """Return the full entry tuples for every currently-active
        row, sorted by (kind, name) for stable output."""
        by_key = {(k, n): (k, n, d, p) for (k, n, d, p) in self._entries}
        out: list[CommandPaletteEntry] = []
        for key in sorted(self._active):
            entry = by_key.get(key)
            if entry is not None:
                out.append(entry)

        return out

    # ── rendering ──
    @staticmethod
    def _fuzzy_score(query: str, haystack: str) -> Optional[int]:
        """Subsequence-match `query` against `haystack` (both
        lowercased). Returns a non-negative score where LOWER is
        better (earlier + more contiguous matches score higher); None
        if `query` isn't a subsequence of `haystack`.

        Scoring:
          - Start position of the first matched char (earlier = lower).
          - Plus the sum of gaps between consecutive matched chars.
          - Empty query scores 0.

        Cheap and good enough for N<1000 palette entries; matches the
        fzf behaviour users expect without pulling in a native lib."""
        if not query:
            return 0
        hi = 0
        score = 0
        first = -1
        last = -1
        for ch in query:
            hi = haystack.find(ch, hi)
            if hi < 0:
                return None
            if first < 0:
                first = hi
            if last >= 0:
                score += (hi - last - 1)
            last = hi
            hi += 1
        return first + score

    def _rebuild_list(self) -> None:
        """Wipe and rebuild the ListBox from `_entries`, fuzzy-matched
        (subsequence) against the search input. Case-insensitive;
        searches `name + kind + description` concatenated."""
        child = self._listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._listbox.remove(child)
            child = nxt

        query = (self._search.get_text() or "").strip().lower()
        scored: list[tuple[int, int, CommandPaletteEntry]] = []
        for idx, entry in enumerate(self._entries):
            kind, name, desc, _preview = entry
            haystack = f"{name} {kind} {desc}".lower()
            score = self._fuzzy_score(query, haystack)
            if score is None:
                continue
            scored.append((score, idx, entry))
        scored.sort(key=lambda t: (t[0], t[1]))
        self._filtered = [e for _s, _i, e in scored]
        for entry in self._filtered:
            kind, name, desc, _preview = entry
            row = self._make_row(kind, name, desc)
            self._listbox.append(row)

        # Select the first row so arrow keys + Tab have something to
        # operate on without the user first clicking.
        first = self._listbox.get_row_at_index(0)
        if first is not None:
            self._listbox.select_row(first)
            self._ensure_row_visible(first)
        # Do NOT call `grab_focus()` here — `Gtk.Entry.grab_focus()`
        # re-selects all existing text by default, so the next
        # keystroke would replace the entry's content. Listbox + rows
        # are already `can_focus=False`, so the entry never loses
        # focus during a rebuild and no re-grab is needed.

    def _make_row(self, kind: str, name: str, desc: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        # Matches the ListBox-level `set_can_focus(False)` — keyboard
        # navigation runs entirely through the palette's `_on_key`, so
        # rows should never become the keyboard focus target.
        row.set_can_focus(False)
        row.add_css_class("overlay-palette-row")
        row.add_css_class("pilot-palette-row")
        # Track the identity on the row so `_toggle_active` /
        # `_commit_and_close` can look it up without a dict.
        row._palette_key = (kind, name)  # type: ignore[attr-defined]
        if (kind, name) in self._active:
            row.add_css_class("active")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        mark = Gtk.Label(label="☑" if (kind, name) in self._active else "☐")
        mark.add_css_class("overlay-palette-mark")
        mark.add_css_class("pilot-palette-mark")
        box.append(mark)
        row._palette_mark = mark  # type: ignore[attr-defined]

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        title = Gtk.Label(xalign=0.0, hexpand=True)
        title.set_markup(
            f'<span weight="bold">{GLib.markup_escape_text(name)}</span>'
            f' <span alpha="60%">({GLib.markup_escape_text(kind)})</span>'
        )
        title.add_css_class("overlay-palette-name")
        title.add_css_class("pilot-palette-name")
        text_box.append(title)
        if desc:
            # `Pango.EllipsizeMode.END` is the normal way to write this,
            # but we avoid importing Pango at module import — callers
            # may have their own pinned gi versions. The int value 3
            # matches `PANGO_ELLIPSIZE_END`.
            desc_label = Gtk.Label(label=desc, xalign=0.0)
            desc_label.set_ellipsize(3)
            desc_label.add_css_class("overlay-palette-desc")
            desc_label.add_css_class("pilot-palette-desc")
            text_box.append(desc_label)
        box.append(text_box)

        row.set_child(box)

        return row

    # ── interaction ──
    def _on_search_changed(self, _entry) -> None:
        self._rebuild_list()

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._commit_and_close()
            return True
        if keyval == Gdk.KEY_Tab or keyval == Gdk.KEY_ISO_Left_Tab:
            self._toggle_current()
            return True
        if keyval == Gdk.KEY_Down or (ctrl and keyval == Gdk.KEY_n):
            self._move_selection(1)
            return True
        if keyval == Gdk.KEY_Up or (ctrl and keyval == Gdk.KEY_p):
            self._move_selection(-1)
            return True

        return False

    def _move_selection(self, direction: int) -> None:
        row = self._listbox.get_selected_row()
        idx = row.get_index() if row is not None else -1
        target = idx + direction
        nxt = self._listbox.get_row_at_index(target)
        if nxt is not None:
            self._listbox.select_row(nxt)
            self._ensure_row_visible(nxt)

    def _ensure_row_visible(self, row: Gtk.ListBoxRow) -> None:
        """Scroll the palette's ScrolledWindow so `row` is on screen.
        Called on every keyboard-driven selection change. We can't rely
        on `row.grab_focus()` to pull the viewport along because the
        row is intentionally non-focusable — drive the vadjustment
        directly. `idle_add` defers the computation to the next tick
        so GTK has finished laying out any just-appended children."""
        def _scroll() -> bool:
            adj = self._scroller.get_vadjustment()
            if adj is None:
                return False
            alloc = row.get_allocation()
            if alloc.height <= 0:
                return False
            top = alloc.y
            bottom = top + alloc.height
            view_top = adj.get_value()
            view_bottom = view_top + adj.get_page_size()
            if top < view_top:
                adj.set_value(top)
            elif bottom > view_bottom:
                adj.set_value(bottom - adj.get_page_size())
            return False

        GLib.idle_add(_scroll)

    def _toggle_current(self) -> None:
        row = self._listbox.get_selected_row()
        if row is None:
            return
        key = getattr(row, "_palette_key", None)
        mark = getattr(row, "_palette_mark", None)
        if not key:
            return
        if key in self._active:
            self._active.discard(key)
            row.remove_css_class("active")
            if mark is not None:
                mark.set_label("☐")
        else:
            self._active.add(key)
            row.add_css_class("active")
            if mark is not None:
                mark.set_label("☑")

    def _commit_and_close(self) -> None:
        """Enter / Entry `activate`: detach, then fire the commit
        callback with whichever entries are currently active. The
        caller handles the token insertion / dispatch; on_cancel is
        NOT fired on a commit path since the host is probably about
        to refocus something itself."""
        self.set_visible(False)
        if self._attached:
            self._host_overlay.remove_overlay(self)
            self._attached = False
        active = self.active_entries()
        try:
            self._on_commit(active)
        except Exception:
            log.exception("palette on_commit raised")

__all__ = [
    "ButtonVariant",
    "CommandPalette",
    "CommandPaletteEntry",
    "Header",
    "LayerOverlayWindow",
    "PillVariant",
    "focused_gdk_monitor",
    "focused_monitor_name",
    "load_css_from_path",
    "load_overlay_css",
    "make_button",
    "make_card",
    "make_collapsible",
    "make_pill",
]
