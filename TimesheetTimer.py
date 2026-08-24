"""
Timesheet Timer
---------------
A small always-on-top overlay that tracks time spent per account/task.

Hotkeys (work even when the window doesn't have focus; defaults shown,
customizable from Preferences):
  Ctrl+Alt+T  -> open a popup to type an account name and start the timer
                 (automatically stops/banks whatever was running before)
  Ctrl+Alt+S  -> stop the current timer (banks its elapsed time)
                 (automatically outputs the current timer to CSV)
  Ctrl+Alt+O  -> resets the position of the timer to the default corner selected in the settings
                 (also saves that positioning for future reloads)

Right-click the overlay for a menu (new task / stop / save CSV / open CSV /
preferences / quit).
Left-click-drag the overlay to reposition it (position is saved automatically).

Preferences lets you customize:
  - Background color, account/timer text colors
  - Font family & size for each label
  - Which screen corner the overlay defaults to
  - The two global hotkeys (type a combo like "ctrl+alt+t" or click Record
    and press the keys you want)
Settings persist to timesheet_config.json next to this script and reload
automatically next run.

On quit (or every 60s autosave), writes all accounts and their total time
to a CSV file (default: timesheet.csv, next to this script).

Notes:
  - On Windows, global hotkeys via the `keyboard` package usually need the
    script to be run as Administrator.
  - On Linux, the `keyboard` package generally needs to run as root (sudo)
    because it reads raw input devices.
  - On macOS, you'll need to grant Accessibility/Input Monitoring permission
    to your terminal/Python in System Settings > Privacy & Security.
"""

import tkinter as tk
from tkinter import ttk, colorchooser, font as tkfont, messagebox
import queue
import threading
import time
import csv
import json
import os
import subprocess
import sys
from datetime import timedelta
from infi.systray import SysTrayIcon
from datetime import datetime
from pathlib import Path

import keyboard
from infi.systray.traybar import SysTrayIcon

CSV_FILE = "timesheet_" + datetime.today().strftime('%m-%d-%y') + ".csv"
CONFIG_FILE = "timesheet_config.json"
DEFAULT_AUTOSAVE_INTERVAL_SEC = 60

OVERLAY_WIDTH = 220
OVERLAY_HEIGHT = 70
SCREEN_MARGIN = 10

CORNER_OPTIONS = ["top-left", "top-right", "bottom-left", "bottom-right"]

DEFAULT_APPEARANCE = {
    "bg": "#1e1e1e",
    "account_fg": "#ffffff",
    "time_fg": "#00ff90",
    "font_family": "Segoe UI",
    "time_font_family": "Consolas",
    "font_size": 10,
    "time_font_size": 16,
    "corner": "top-right",
    "pos_x": None,
    "pos_y": None,
    "hotkey_open": "ctrl+alt+t",
    "hotkey_stop": "ctrl+alt+s",
    "hotkey_reset": "ctrl+alt+o",
    "autosave_interval_sec": 60
}


class TimesheetApp:
    def __init__(self):
        self.accounts = {}    
        self.current_account = None
        self.current_start = None
        self.hotkey_queue = queue.Queue()
        self.appearance = self._load_appearance()
        self.autosave_interval = self.set_autosave_interval(self.appearance.get("autosave_interval_sec"))
        self.csvPath = self.get_dir()
        self.accounts = self.check_csv()

        # --- Overlay window ---
        self.root = tk.Tk()
        self.root.overrideredirect(True)      # no title bar/borders
        self.root.attributes("-topmost", True)

        w, h = OVERLAY_WIDTH, OVERLAY_HEIGHT
        self.root.update_idletasks()
        x, y = self._resolve_position()
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.account_label = tk.Label(self.root, text="No active task")
        self.account_label.pack(pady=(8, 0))

        self.time_label = tk.Label(self.root, text="00:00:00")
        self.time_label.pack()

        self._apply_appearance()

        # Drag to move
        self.root.bind("<ButtonPress-1>", self._start_move)
        self.root.bind("<B1-Motion>", self._do_move)

        # Right-click menu
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="New Task", command=self.open_popup)
        self.menu.add_command(label="Stop Timer", command=self.stop_timer)
        self.menu.add_separator()
        self.menu.add_command(label="Save CSV Now", command=self.save_csv)
        self.menu.add_command(label="Open CSV", command=self.open_csv)
        self.menu.add_separator()
        self.menu.add_command(label="Preferences...", command=self.open_preferences)
        self.menu.add_command(label="Quit", command=self.quit_app)
        self.root.bind("<Button-3>", self._show_menu)

        # Global hotkeys (fire on a background thread -> pushed to a queue,
        # then handled on the Tk main thread for thread safety)
        self._register_hotkeys()

        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)

        self._update_clock()
        self._check_hotkey_queue()
        self._autosave_loop()
        self.systray_setup()
        
        # if the window is off screen at boot (started on a smaller screen than when it was closed)
        # this resets the location to the selected corner
        # if type(self.appearance["pos_x"]) == None or type(self.appearance["pos_y"]) == None:
        #     self.reset_timer()  
        try:
            if self.appearance["pos_x"] > self.root.winfo_screenwidth() or self.appearance["pos_y"] > self.root.winfo_screenheight():
                self.reset_timer()
        except:
            self.reset_timer()

        
    def systray_setup(self):
        menu_options = (("New Task", None, lambda event: self.open_tray_popup(event)),
                        ("Stop Timer", None, lambda event: self.stop_tray_timer(event)),
                        ("Save CSV Now", None, lambda event: self.save_tray_csv(event)),
                        ("Open CSV Now", None, lambda event: self.open_tray_csv(event)),
                        ("Preferences", None, lambda event: self.open_tray_preferences(event)),)
        systray = SysTrayIcon(self.resource("vincueblacktimer.ico"), "TimesheetTimer", menu_options, on_quit=lambda event: self.on_quit_callback(event))
        systray.start()
        
    def on_quit_callback(self, event):
        self.quit_app()
        
    def open_tray_popup(self, event):
        self.open_popup()        
    
    def stop_tray_timer(self, event):
        self.stop_timer()
        
    def save_tray_csv(self, event):
        self.save_csv()
    
    def open_tray_csv(self, event):
        self.open_csv()
        
    def open_tray_preferences(self, event):
        self.open_preferences()

    # ---------- window dragging ----------
    def _start_move(self, event):
        self._dx, self._dy = event.x, event.y

    def _do_move(self, event):
        x = self.root.winfo_pointerx() - self._dx
        y = self.root.winfo_pointery() - self._dy
        self.root.geometry(f"+{x}+{y}")
        self.appearance["pos_x"] = x
        self.appearance["pos_y"] = y
        self._save_appearance()

    def _show_menu(self, event):
        open_hk = self.appearance.get("hotkey_open", DEFAULT_APPEARANCE["hotkey_open"])
        stop_hk = self.appearance.get("hotkey_stop", DEFAULT_APPEARANCE["hotkey_stop"])
        self.menu.entryconfig(0, label=f"New Task ({open_hk})")
        self.menu.entryconfig(1, label=f"Stop Timer ({stop_hk})")
        self.menu.tk_popup(event.x_root, event.y_root)

    # ---------- hotkey handling ----------
    def _register_hotkeys(self):
        keyboard.unhook_all()
        keyboard.add_hotkey(
            self.appearance.get("hotkey_open", DEFAULT_APPEARANCE["hotkey_open"]),
            lambda: self.hotkey_queue.put("open"),
        )
        keyboard.add_hotkey(
            self.appearance.get("hotkey_stop", DEFAULT_APPEARANCE["hotkey_stop"]),
            lambda: self.hotkey_queue.put("stop"),
        )
        keyboard.add_hotkey(
            self.appearance.get("hotkey_reset", DEFAULT_APPEARANCE["hotkey_reset"]),
            lambda: self.hotkey_queue.put("reset"),
        )

    def _check_hotkey_queue(self):
        try:
            while True:
                cmd = self.hotkey_queue.get_nowait()
                if cmd == "open":      
                    self.open_popup()           
                elif cmd == "stop":
                    self.stop_timer()
                    self.save_csv()
                elif cmd == "reset":
                    self.reset_timer()
        except queue.Empty:
            pass
        self.root.after(150, self._check_hotkey_queue)

    # ---------- popup for entering a new task ----------
    def open_popup(self):
        popup = tk.Toplevel(self.root)
        popup.title("New Task")
        popup.attributes("-topmost", True)
        popup.geometry("340x130")
        popup.resizable(False, False)
        
        # pyautogui.press("alt")
        # win_activate(window_title="tk", partial_match=False)   

        tk.Label(popup, text="Account / task name:").pack(pady=(12, 4))

        names = sorted(self.accounts.keys())
        var = tk.StringVar()
        combo = ttk.Combobox(popup, textvariable=var, values=names, width=38)
        combo.pack(padx=10)
               
        combo.focus_set()

        def start_and_close(event=None):
            name = var.get().strip()
            if name:
                self.start_timer(name)
            popup.destroy()

        combo.bind("<Return>", start_and_close)
        tk.Button(popup, text="Start Timer", command=start_and_close).pack(pady=12)
        popup.grab_set()

    # ---------- window position ----------
    def _corner_coords(self, corner):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        m = SCREEN_MARGIN
        if corner == "top-left":
            return m, m
        elif corner == "top-right":
            return sw - OVERLAY_WIDTH - m, m
        elif corner == "bottom-left":
            return m, sh - OVERLAY_HEIGHT - m
        elif corner == "bottom-right":
            return sw - OVERLAY_WIDTH - m, sh - OVERLAY_HEIGHT - m
        return m, m

    def _resolve_position(self):
        # Explicit saved coordinates (e.g. from dragging) take priority;
        # otherwise fall back to the selected corner.
        px, py = self.appearance.get("pos_x"), self.appearance.get("pos_y")
        if px is not None and py is not None:
            return px, py
        corner = self.appearance.get("corner", "top-right")
        return self._corner_coords(corner)

    def move_to_corner(self, corner):
        x, y = self._corner_coords(corner)
        self.root.geometry(f"+{x}+{y}")
        self.appearance["corner"] = corner
        self.appearance["pos_x"] = x
        self.appearance["pos_y"] = y

    # ---------- appearance (colors & fonts) ----------
    def _load_appearance(self):
        settings = dict(DEFAULT_APPEARANCE)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    saved = json.load(f)
                settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass
        return settings

    def _save_appearance(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.appearance, f, indent=2)
        except OSError:
            pass

    def _apply_appearance(self):
        a = self.appearance
        self.root.configure(bg=a["bg"])
        self.account_label.config(
            bg=a["bg"], fg=a["account_fg"],
            font=(a["font_family"], a["font_size"], "bold"),
        )
        self.time_label.config(
            bg=a["bg"], fg=a["time_fg"],
            font=(a["time_font_family"], a["time_font_size"]),
        )

    def open_preferences(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Preferences")
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        # work on a copy so Cancel discards changes
        temp = dict(self.appearance)
        families = sorted(tkfont.families())

        def pick_color(key, label):
            current = temp.get(key, "#ffffff")
            rgb, hexval = colorchooser.askcolor(color=current, title=f"Choose {label} color")
            if hexval:
                temp[key] = hexval
                swatches[key].config(bg=hexval)

        row = 0
        swatches = {}

        def add_color_row(key, label):
            nonlocal row
            tk.Label(dlg, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=6)
            swatch = tk.Label(dlg, text="        ", bg=temp.get(key, "#ffffff"), relief="groove")
            swatch.grid(row=row, column=1, padx=(0, 6))
            swatches[key] = swatch
            tk.Button(dlg, text="Choose...", command=lambda k=key, l=label: pick_color(k, l)).grid(
                row=row, column=2, padx=(0, 10)
            )
            row += 1

        add_color_row("bg", "Background")
        add_color_row("account_fg", "Account text")
        add_color_row("time_fg", "Timer text")

        # Font family + sizes
        tk.Label(dlg, text="Label font").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        font_var = tk.StringVar(value=temp.get("font_family", DEFAULT_APPEARANCE["font_family"]))
        font_combo = ttk.Combobox(dlg, textvariable=font_var, values=families, width=20, state="readonly")
        font_combo.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10))
        row += 1

        tk.Label(dlg, text="Label size").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        size_var = tk.IntVar(value=temp.get("font_size", DEFAULT_APPEARANCE["font_size"]))
        tk.Spinbox(dlg, from_=6, to=32, textvariable=size_var, width=5).grid(
            row=row, column=1, sticky="w", padx=(0, 10)
        )
        row += 1

        tk.Label(dlg, text="Timer font").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        time_font_var = tk.StringVar(value=temp.get("time_font_family", DEFAULT_APPEARANCE["time_font_family"]))
        time_font_combo = ttk.Combobox(dlg, textvariable=time_font_var, values=families, width=20, state="readonly")
        time_font_combo.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10))
        row += 1

        tk.Label(dlg, text="Timer size").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        time_size_var = tk.IntVar(value=temp.get("time_font_size", DEFAULT_APPEARANCE["time_font_size"]))
        tk.Spinbox(dlg, from_=8, to=48, textvariable=time_size_var, width=5).grid(
            row=row, column=1, sticky="w", padx=(0, 10)
        )
        row += 1

        ttk.Separator(dlg, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", padx=10, pady=(4, 4)
        )
        row += 1

        tk.Label(dlg, text="Screen corner").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        corner_var = tk.StringVar(value=temp.get("corner", DEFAULT_APPEARANCE["corner"]))
        corner_labels = {
            "top-left": "Top-left",
            "top-right": "Top-right",
            "bottom-left": "Bottom-left",
            "bottom-right": "Bottom-right",
        }
        corner_frame = tk.Frame(dlg)
        corner_frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10))
        for corner in CORNER_OPTIONS:
            tk.Radiobutton(
                corner_frame, text=corner_labels[corner], variable=corner_var, value=corner
            ).pack(anchor="w")
        row += 1

        ttk.Separator(dlg, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", padx=10, pady=(4, 4)
        )
        row += 1

        # ---- Hotkeys ----
        open_hk_var = tk.StringVar(value=temp.get("hotkey_open", DEFAULT_APPEARANCE["hotkey_open"]))
        stop_hk_var = tk.StringVar(value=temp.get("hotkey_stop", DEFAULT_APPEARANCE["hotkey_stop"]))
        reset_hk_var = tk.StringVar(value=temp.get("hotkey_reset", DEFAULT_APPEARANCE["hotkey_reset"]))

        def record_hotkey(var, button):
            button.config(text="Press keys...", state="disabled")
            result_q = queue.Queue()

            def worker():
                try:
                    combo = keyboard.read_hotkey(suppress=False)
                except Exception:
                    combo = None
                result_q.put(combo)

            threading.Thread(target=worker, daemon=True).start()

            def poll():
                try:
                    combo = result_q.get_nowait()
                except queue.Empty:
                    dlg.after(100, poll)
                    return
                if combo:
                    var.set(combo)
                button.config(text="Record", state="normal")

            dlg.after(100, poll)

        tk.Label(dlg, text="Open task hotkey").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        open_hk_entry = tk.Entry(dlg, textvariable=open_hk_var, width=18)
        open_hk_entry.grid(row=row, column=1, sticky="w")
        open_hk_btn = tk.Button(dlg, text="Record")
        open_hk_btn.config(command=lambda: record_hotkey(open_hk_var, open_hk_btn))
        open_hk_btn.grid(row=row, column=2, padx=(0, 10))
        row += 1

        tk.Label(dlg, text="Stop timer hotkey").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        stop_hk_entry = tk.Entry(dlg, textvariable=stop_hk_var, width=18)
        stop_hk_entry.grid(row=row, column=1, sticky="w")
        stop_hk_btn = tk.Button(dlg, text="Record")
        stop_hk_btn.config(command=lambda: record_hotkey(stop_hk_var, stop_hk_btn))
        stop_hk_btn.grid(row=row, column=2, padx=(0, 10))
        row += 1
        
        tk.Label(dlg, text="Reset position hotkey").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        reset_hk_entry = tk.Entry(dlg, textvariable=reset_hk_var, width=18)
        reset_hk_entry.grid(row=row, column=1, sticky="w")
        reset_hk_btn = tk.Button(dlg, text="Record")
        reset_hk_btn.config(command=lambda: record_hotkey(reset_hk_var, reset_hk_btn))
        reset_hk_btn.grid(row=row, column=2, padx=(0, 10))
        row += 1

        tk.Label(
            dlg, text="Type a combo like ctrl+alt+t, or click Record and press the keys.",
            font=("Segoe UI", 8), fg="#666666",
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        row += 1
        
        ttk.Separator(dlg, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", padx=10, pady=(4, 4)
        )
        row += 1
        
        # ---- Autosave Timing ----
        
        autosave_time_var = tk.StringVar(value = temp.get("autosave_interval_sec", DEFAULT_APPEARANCE["autosave_interval_sec"]))
        
        tk.Label(dlg, text="Timer autosave interval").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        autosave_time_entry = tk.Entry(dlg, textvariable=autosave_time_var, width=4)
        autosave_time_entry.grid(row=row, column=1, sticky="w")
        row += 1
        
        tk.Label(
            dlg, text="Enter a number of seconds for the autosave interval.\nAnything other than a number resets the value to default (60)",
            font=("Segoe UI", 8), fg="#666666",
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        row += 1

        def gather_and_preview():
            temp["font_family"] = font_var.get()
            temp["font_size"] = size_var.get()
            temp["time_font_family"] = time_font_var.get()
            temp["time_font_size"] = time_size_var.get()
            temp["corner"] = corner_var.get()
            temp["hotkey_open"] = open_hk_var.get().strip()
            temp["hotkey_stop"] = stop_hk_var.get().strip()
            temp["hotkey_reset"] = reset_hk_var.get().strip()
            try:
                temp["autosave_interval_sec"] = int(autosave_time_entry.get())
            except:               
                temp["autosave_interval_sec"] = DEFAULT_AUTOSAVE_INTERVAL_SEC
            
        def apply_preview():
            gather_and_preview()
            previous_hotkeys = (
                self.appearance.get("hotkey_open"),
                self.appearance.get("hotkey_stop"),
                self.appearance.get("hotkey_reset")
            )
            self.appearance = dict(temp)
            self.autosave_update(int(temp["autosave_interval_sec"]))
            self._apply_appearance()
            self.move_to_corner(self.appearance["corner"])
            try:
                self._register_hotkeys()
            except Exception as e:
                messagebox.showerror(
                    "Invalid hotkey",
                    f"Couldn't set that hotkey combination:\n{e}\n\nReverting to the previous hotkeys.",
                    parent=dlg,
                )
                self.appearance["hotkey_open"], self.appearance["hotkey_stop"], self.appearance["hotkey_reset"] = previous_hotkeys
                open_hk_var.set(previous_hotkeys[0])
                stop_hk_var.set(previous_hotkeys[1])
                reset_hk_var.set(previous_hotkeys[2])
                self._register_hotkeys()
                
        def save_and_close():
            apply_preview()
            self._save_appearance()
            dlg.destroy()

        def reset_defaults():
            temp.clear()
            temp.update(DEFAULT_APPEARANCE)
            for key, swatch in swatches.items():
                swatch.config(bg=temp[key])
            font_var.set(temp["font_family"])
            size_var.set(temp["font_size"])
            time_font_var.set(temp["time_font_family"])
            time_size_var.set(temp["time_font_size"])
            corner_var.set(temp["corner"])
            open_hk_var.set(temp["hotkey_open"])
            stop_hk_var.set(temp["hotkey_stop"])
            reset_hk_var.set(temp["hotkey_reset"])
            autosave_time_var.set(temp["autosave_interval_sec"])
            
            apply_preview()

        btn_frame = tk.Frame(dlg)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=(10, 12))
        tk.Button(btn_frame, text="Preview", command=apply_preview).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Reset to defaults", command=reset_defaults).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Save", command=save_and_close).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side="left", padx=5)

        dlg.grab_set()

    # ---------- timer logic ----------
    def start_timer(self, name):
        self.stop_timer()  # bank whatever was running
        if len(self.accounts) > 0:
            for key in self.accounts.keys():
                if name == key:
                    self.current_account = key
                    self.current_start = time.time() - self.accounts[key]
                    self.accounts.setdefault(key, self.accounts[key])
                    self.account_label.config(text=key)
                    break
                else:
                    self.current_account = name
                    self.current_start = time.time()
                    self.accounts.setdefault(name, 0.0)
                    self.account_label.config(text=name)
                    break
        else:
            self.current_account = name
            self.current_start = time.time()
            self.accounts.setdefault(name, 0.0)
            self.account_label.config(text=name)

    def stop_timer(self):
        if self.current_account and self.current_start:
            elapsed = time.time() - self.current_start
            self.accounts[self.current_account] = (
                self.accounts.get(self.current_account, 0.0) + elapsed
            )
        self.current_account = None
        self.current_start = None
        self.account_label.config(text="No active task")
        self.time_label.config(text="00:00:00")

    def _update_clock(self):
        if self.current_account and self.current_start:
            elapsed = time.time() - self.current_start + self.accounts[self.current_account]
            self.time_label.config(text=str(timedelta(seconds=int(elapsed))))
        self.root.after(500, self._update_clock)

    # ---------- CSV export ----------    
    def resource(self, relativePath):
            basePath = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            return os.path.join(basePath, relativePath)

    def get_dir(self):
        Path('Timesheets').mkdir(parents=False, exist_ok=True)
        
        return Path('Timesheets')
    
    def check_csv(self):        
        accountDict = {}
        if os.path.exists(str(self.csvPath) + '\\' + CSV_FILE):
            with open(str(self.csvPath) + '\\' + CSV_FILE, "r") as f:
                reader = csv.reader(f)
                for row in reader:
                    if row[0] == 'account':
                        continue
                    else:
                        foo = row[0]
                        bar = row[1]
                        accountDict[foo] = float(bar)
        else:
            self.save_csv()        
        return accountDict
        
    def save_csv(self):
        # Include currently-running elapsed time without stopping the timer
        snapshot = dict(self.accounts)
        if self.current_account and self.current_start:
            elapsed = time.time() - self.current_start
            snapshot[self.current_account] = snapshot.get(self.current_account, 0.0) + elapsed

        with open(str(self.csvPath) + '\\' + CSV_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["account", "total_seconds", "total_hms"])
            for name, secs in sorted(snapshot.items()):
                writer.writerow([name, round(secs, 1), str(timedelta(seconds=int(secs)))])

    def open_csv(self):
        # Make sure there's an up-to-date file to open
        if not os.path.exists(str(self.csvPath) + '\\' + CSV_FILE):
            self.save_csv()
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(self.csvPath) + '\\' + CSV_FILE)  # noqa: only exists on Windows
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.csvPath + '\\' + CSV_FILE])
            else:
                subprocess.Popen(["xdg-open", self.csvPath + '\\' + CSV_FILE])
        except OSError:
            pass

    def _autosave_loop(self):
        self.save_csv()
        self.root.after(self.autosave_interval * 1000, self._autosave_loop)
    
    def autosave_update(self, sec):
        self.autosave_interval = self.set_autosave_interval(sec)
                
    def set_autosave_interval(self, sec):
        if sec == 0:
            sec = DEFAULT_AUTOSAVE_INTERVAL_SEC
        
        self.appearance.update({"autosave_interval_sec": sec})
        
        self._save_appearance()
        return sec

    def quit_app(self):
        self.stop_timer()
        self.save_csv()
        os._exit(0)
        
    def run(self):
        self.root.mainloop()
        
    def reset_timer(self):
        self.move_to_corner(self.appearance.get("corner"))
        self._save_appearance()


if __name__ == "__main__":
    app = TimesheetApp()
    app.run()
