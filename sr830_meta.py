"""
sr830_meta.py -- Experiment-context panel for sr830_gui.

The lock-in knows its own settings and writes them into the CSV header. What it
cannot know is the experiment around it: which gas is flowing, at what
concentration, what the laser is doing, what the beam power was. Without those,
a data file records what the instrument did but not what was measured.

This module adds a panel for that context. Every field becomes an extra column
on each CSV row, so the values travel with the data rather than living in a
lab book that may or may not be findable later.

Fields are free text on purpose -- half of them come from instruments that are
not on the bus, and a field you can type into gets filled in while a field that
demands a connection stays empty.

Usage (already wired if you are using the patched sr830_gui.py):

    from sr830_meta import MetadataPanel
    panel = MetadataPanel(parent, on_change=worker_setter)
"""

import tkinter as tk
from tkinter import ttk

__all__ = ["MetadataPanel", "FIELDS"]


# (csv_column, label, default, width)
FIELDS = [
    ("gas_matrix",      "Matrix gas",        "N2",  10),
    ("analyte",         "Analyte",           "CO2", 10),
    ("analyte_sccm",    "Analyte (sccm)",    "",     8),
    ("matrix_sccm",     "Matrix (sccm)",     "",     8),
    ("conc_pct",        "Concentration (%)", "",     8),
    ("tec_setpoint_C",  "TEC (\u00b0C)",     "17.5", 8),
    ("ld_current_mA",   "LD current (mA)",   "152",  8),
    ("transmitted_mW",  "Transmitted (mW)",  "",     8),
    ("pressure",        "Pressure",          "ambient", 10),
    ("operator",        "Operator",          "",    10),
]


class MetadataPanel(ttk.LabelFrame):
    """Free-text experiment context, appended as extra CSV columns.

    on_change(dict) is called whenever any field is edited, and once at
    construction, so the consumer always holds the current values.
    """

    def __init__(self, parent, on_change=None, columns=2, **kw):
        super().__init__(parent, text="Experiment context", padding=6, **kw)
        self._on_change = on_change
        self.vars = {}

        for i, (key, label, default, width) in enumerate(FIELDS):
            r, c = divmod(i, columns)
            cell = ttk.Frame(self)
            cell.grid(row=r, column=c, sticky="ew", padx=(0, 10), pady=1)
            ttk.Label(cell, text=label, width=17, anchor="w").pack(side="left")
            var = tk.StringVar(value=default)
            var.trace_add("write", self._changed)
            ttk.Entry(cell, textvariable=var, width=width).pack(side="left")
            self.vars[key] = var

        for c in range(columns):
            self.columnconfigure(c, weight=1)

        # Note field spans the full width -- this is where the useful detail
        # usually ends up, so give it room.
        note = ttk.Frame(self)
        note.grid(row=(len(FIELDS) + columns - 1) // columns, column=0,
                  columnspan=columns, sticky="ew", pady=(4, 0))
        ttk.Label(note, text="Note", width=17, anchor="w").pack(side="left")
        self.vars["note"] = tk.StringVar(value="")
        self.vars["note"].trace_add("write", self._changed)
        ttk.Entry(note, textvariable=self.vars["note"]).pack(
            side="left", fill="x", expand=True)

        self._changed()

    # ------------------------------------------------------------------

    def _changed(self, *_):
        if self._on_change:
            self._on_change(self.values())

    def values(self):
        """Current field values as an ordered dict of CSV column -> string."""
        return {k: v.get().strip() for k, v in self.vars.items()}

    def set(self, **kw):
        """Set fields programmatically, e.g. panel.set(conc_pct='70')."""
        for k, v in kw.items():
            if k in self.vars:
                self.vars[k].set(str(v))
