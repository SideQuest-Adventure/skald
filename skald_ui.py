"""Keyboard-accessible settings and draft review windows for the Skald overlay."""
from dataclasses import replace
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk

from skald_writing import MODES

BG, FG, SUB, GOLD = "#23252B", "#EDE5D3", "#AAA5B0", "#E8C766"


def window(root, title, geometry):
    win = tk.Toplevel(root)
    win.title(title)
    win.geometry(geometry)
    win.configure(bg=BG, padx=18, pady=14)
    win.attributes("-topmost", True)
    win.minsize(620, 480)
    try:
        win.iconbitmap(root.iconbitmap())
    except tk.TclError:
        pass
    win.lift()
    win.after(100, win.focus_force)
    win.bind("<Escape>", lambda event: win.destroy())
    return win


def label(parent, text, **kwargs):
    widget = tk.Label(parent, text=text, bg=BG, fg=FG, anchor="w",
                      justify="left", font=("Segoe UI", 10), **kwargs)
    widget.pack(fill="x", pady=(0, 8))
    def wrap(event):
        width = max(200, event.width - 2)
        if int(widget.cget("wraplength")) != width:
            widget.configure(wraplength=width)
    widget.bind("<Configure>", wrap)
    return widget


def editor(parent, height=8):
    wrap = tk.Frame(parent, bg=BG)
    wrap.pack(fill="both", expand=True, pady=(0, 10))
    text = tk.Text(wrap, height=height, wrap="word", bg="#191B20", fg=FG,
                   insertbackground=GOLD, selectbackground="#66512D", relief="flat",
                   padx=10, pady=10, undo=True, font=("Segoe UI", 11))
    scroll = ttk.Scrollbar(wrap, command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    return text


def show_settings(root, settings, save, devices=()):
    win = window(root, "Skald | Settings", "740x660")
    label(win, "Local settings. Changes apply to your next dictation.")
    notebook = ttk.Notebook(win)
    notebook.pack(fill="both", expand=True, pady=(0, 12))
    fields = {}

    def page(name):
        frame = tk.Frame(notebook, bg=BG, padx=14, pady=14)
        notebook.add(frame, text=name)
        return frame

    def choice(parent, name, title, values):
        label(parent, title)
        var = tk.StringVar(value=getattr(settings, name))
        combo = ttk.Combobox(parent, textvariable=var, values=values, state="readonly")
        combo.pack(fill="x", pady=(0, 10))
        fields[name] = var

    def check(parent, name, title):
        var = tk.BooleanVar(value=getattr(settings, name))
        tk.Checkbutton(parent, text=title, variable=var, bg=BG, fg=FG,
                       selectcolor=BG, activebackground=BG, activeforeground=GOLD,
                       font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=4)
        fields[name] = var

    def entry(parent, name, title, values=None):
        label(parent, title)
        var = tk.StringVar(value=str(getattr(settings, name)))
        control = ttk.Entry(parent, textvariable=var) if values is None else ttk.Combobox(
            parent, textvariable=var, values=values)
        control.pack(fill="x", pady=(0, 10))
        fields[name] = var

    writing = page("Writing")
    choice(writing, "mode", "Writing mode", MODES)
    label(writing, "Exact keeps wording. Clean removes um/uh/erm.\nBullets uses your spoken line breaks; it does not summarize.")
    check(writing, "review_before_paste", "Brain-dump mode: review before pasting")
    check(writing, "spoken_editing", "Enable editing commands with the 'skald' prefix")
    entry(writing, "auto_stop_silence", "Stop after prolonged silence (seconds; 0 disables)")
    label(writing, "Try: skald new paragraph / scratch that / replace Tuesday with Thursday\nSay each command as its own phrase, with a short pause on either side.")

    tables = {}
    for name, title, description in (
        ("vocabulary", "Vocabulary", "One correction per line: heard spelling = preferred spelling\nExample: skull = Skald. Matches whole terms, ignoring case."),
        ("snippets", "Shortcuts", "One shortcut per line: name = text to insert\nSay 'skald insert name'. Use \\n for a line break in the text."),
        ("profiles", "App profiles", "One app per line: executable.exe = exact, clean, or bullets\nExample: notepad.exe = clean. Other apps use your default mode."),
    ):
        frame = page(title)
        label(frame, description)
        text = editor(frame, height=12)
        text.insert("1.0", "\n".join(k + " = " + v.replace("\n", "\\n") for k, v in getattr(settings, name).items()))
        tables[name] = text

    controls = page("Controls")
    entry(controls, "hotkey", "Hotkey: a single character or a named key (ctrl_r, f8, etc.)")
    entry(controls, "device", "Microphone (empty uses Windows default)", ("", *devices))
    entry(controls, "model", "CPU model (auto, tiny.en, base.en, small.en, etc.)")
    entry(controls, "language", "Language code (empty detects language automatically)")
    check(controls, "chime", "Play start and stop horn cues")
    check(controls, "preserve_clipboard", "Keep previous clipboard contents when delivering dictation")
    check(controls, "enable_app_commands", "Enable app launch/search/send voice commands (advanced)")
    label(controls, "Microphone, model, and language changes require a restart.")

    privacy = page("Privacy")
    check(privacy, "save_history", "Save dictations and recovery history on this computer")
    entry(privacy, "retention_days", "Keep unpinned history for this many days (0 keeps forever)")
    label(privacy, "History contains plain text. Pinned entries do not expire.\nTurning saving off stops future writes; it does not erase earlier history.\nWindows clipboard history and synced folders have their own privacy settings.")
    error = label(win, "")
    error.configure(fg=GOLD)

    def commit():
        try:
            values = {name: var.get() for name, var in fields.items()}
            values["retention_days"] = int(values["retention_days"])
            values["auto_stop_silence"] = float(values["auto_stop_silence"])
            for name, text in tables.items():
                table = {}
                for line in text.get("1.0", "end-1c").splitlines():
                    if not line.strip():
                        continue
                    key, sep, value = line.partition("=")
                    if not sep or not key.strip() or not value.strip():
                        raise ValueError(f"{name}: use name = value on each line")
                    key, value = key.strip(), value.strip()
                    if key.casefold() in {k.casefold() for k in table}:
                        raise ValueError(f"{name}: duplicate name {key}")
                    table[key] = value.replace("\\n", "\n") if name == "snippets" else value
                values[name] = table
            updated = replace(settings, **values).validate()
            save(updated)
        except (ValueError, TypeError, OSError) as exc:
            error.configure(text=str(exc))
            return
        win.destroy()

    buttons = tk.Frame(win, bg=BG)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Save settings", command=commit).pack(side="right")
    ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=8)
    win.bind("<Control-s>", lambda event: commit())
    return win


def show_review(root, original, output, mode, copy_text, save=None, title="Review draft", learn=None):
    win = window(root, f"Skald | {title}", "760x680")
    label(win, "Edit your draft, then copy it and paste into the app you choose.")
    label(win, f"Writing mode: {mode.title()}   |   Original speech is kept separately.")
    tabs = ttk.Notebook(win)
    tabs.pack(fill="both", expand=True, pady=(0, 10))
    final_frame, raw_frame = tk.Frame(tabs, bg=BG), tk.Frame(tabs, bg=BG)
    tabs.add(final_frame, text="Draft")
    tabs.add(raw_frame, text="Original speech")
    final = editor(final_frame, 16)
    final.insert("1.0", output)
    raw = editor(raw_frame, 16)
    raw.insert("1.0", original)
    raw.configure(state="disabled")
    status = label(win, "Copy replaces the clipboard. This window never sends or auto-pastes.")

    if learn:
        correction = tk.Frame(win, bg=BG)
        correction.pack(fill="x", pady=(0, 12))
        tk.Label(correction, text="Heard", bg=BG, fg=FG).pack(side="left")
        heard = ttk.Entry(correction, width=17)
        heard.pack(side="left", padx=6)
        tk.Label(correction, text="Use", bg=BG, fg=FG).pack(side="left")
        preferred = ttk.Entry(correction, width=17)
        preferred.pack(side="left", padx=6)

        def remember():
            try:
                alias, spelling = heard.get().strip(), preferred.get().strip()
                if not alias or not spelling:
                    raise ValueError("Enter the heard spelling and the preferred spelling")
                learn(alias, spelling)
                from skald_writing import apply_vocabulary
                corrected = apply_vocabulary(final.get("1.0", "end-1c"), {alias: spelling})
                final.delete("1.0", "end")
                final.insert("1.0", corrected)
                status.configure(text="Spelling remembered. Draft updated; original speech is unchanged.")
            except (ValueError, OSError) as exc:
                status.configure(text=str(exc))
        ttk.Button(correction, text="Remember spelling", command=remember).pack(side="left", padx=4)

    def copy():
        text = final.get("1.0", "end-1c")
        try:
            copy_text(text)
        except Exception as exc:
            status.configure(text=f"Could not copy: {exc}")
            return
        try:
            if save:
                save(text)
            status.configure(text="Copied. Click your destination and paste when ready.")
        except Exception as exc:
            status.configure(text=f"Copied, but local save failed: {exc}")

    def save_draft():
        try:
            save(final.get("1.0", "end-1c"))
            status.configure(text="Draft updated. Original speech is unchanged.")
        except Exception as exc:
            status.configure(text=f"Could not save draft: {exc}")

    buttons = tk.Frame(win, bg=BG)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Copy draft", command=copy).pack(side="right")
    if save:
        ttk.Button(buttons, text="Save draft", command=save_draft).pack(side="right", padx=8)
    ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right", padx=8)
    win.bind("<Control-Return>", lambda event: copy())
    final.focus_set()
    return win


def show_history(root, history, review):
    win = window(root, "Skald | Recent dictations", "820x620")
    label(win, "Search, recover, and pin your dictations. Saved only on this computer.")
    query = tk.StringVar()
    search = ttk.Entry(win, textvariable=query)
    search.pack(fill="x", pady=(0, 12))
    search.focus_set()
    tree = ttk.Treeview(win, columns=("time", "mode", "preview"), show="headings", selectmode="browse")
    for name, title, width in (("time", "When", 155), ("mode", "Mode", 65), ("preview", "Draft", 520)):
        tree.heading(name, text=title)
        tree.column(name, width=width, minwidth=50)
    tree.pack(fill="both", expand=True)
    status = label(win, "")
    rows = {}

    def refresh(*args):
        try:
            entries = history.recent(query.get())
            tree.delete(*tree.get_children())
            rows.clear()
            for row in entries:
                rows[row["id"]] = row
                when = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%b %d, %I:%M %p")
                preview = ("[Pinned] " if row["pinned"] else "") + (row["output"] or row["original"]).replace("\n", " ")
                tree.insert("", "end", iid=row["id"], values=(when, row["mode"], preview[:140]))
            status.configure(text=f"{len(entries)} recent matches. Double-click an entry to review.")
        except Exception as exc:
            status.configure(text=f"History unavailable: {exc}")

    def selected():
        selection = tree.selection()
        return rows.get(selection[0]) if selection else None

    def open_entry(event=None):
        row = selected()
        if row:
            review(row)

    def pin():
        row = selected()
        if row:
            try:
                history.pin(row["id"], not row["pinned"])
                refresh()
            except Exception as exc:
                status.configure(text=f"Could not update entry: {exc}")

    def delete():
        row = selected()
        if row and messagebox.askyesno("Delete dictation", "Delete this saved dictation?", parent=win):
            try:
                history.delete(row["id"])
                refresh()
            except Exception as exc:
                status.configure(text=f"Could not delete entry: {exc}")

    buttons = tk.Frame(win, bg=BG)
    buttons.pack(fill="x")
    for text, action in (("Review", open_entry), ("Pin / unpin", pin), ("Delete", delete)):
        ttk.Button(buttons, text=text, command=action).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")
    query.trace_add("write", refresh)
    tree.bind("<Double-1>", open_entry)
    tree.bind("<Return>", open_entry)
    refresh()
    return win
