#!/usr/bin/env python3
"""COneLUT desktop interface.

Basic mode keeps the simple preset flow recommended by the spec (section 24);
advanced mode exposes the full conversion model. Only plain Python values and
queue events cross the worker-thread boundary.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from conelut.capture_one import find_profiles_dir, install_profile
from conelut.cms import PRECISION_CHOICES, BaseProfile, BaseProfileError
from conelut.colorspaces import CAT_CHOICES, GAMUT_CHOICES, TRANSFER_CHOICES
from conelut.convert import CONVERT_ERRORS, convert_file
from conelut.cube import parse_cube
from conelut.pipeline import C1_CURVES, DEFAULT_GRID, ICC_GRID_CHOICES, ConversionParams
from conelut.presets import PRESETS, resolve_preset
from conelut.files import destination as choose_destination
from conelut.report import (
    RunReport,
    format_metrics_line,
    run_report_path,
    settings_from_params,
)
from conelut.theme import (
    CARD_FRAME,
    DIM_LABEL,
    PRIMARY_BUTTON,
    STATUS_COLORS,
    apply_capture_one_style,
    configure_log_text,
    enable_high_dpi,
)
from conelut.validation import DEFAULT_RANDOM_SAMPLES

# Must run before the first Tk window is created (crisp rendering and honest
# winfo coordinates on scaled displays).
if sys.platform == "win32":
    enable_high_dpi()

CUSTOM_PRESET = "カスタム"
EXISTING_POLICIES = {
    "連番を付けて保存": "rename",
    "既存ファイルをスキップ": "skip",
    "既存ファイルを上書き": "overwrite",
}
STATUS_LABELS = {"success": "完了", "error": "エラー", "skipped": "スキップ", "cancelled": "中止"}
C1_CURVE_LABELS = {
    "Linear Response（推奨）": "linear",
    "Film Standard Legacy（1.x互換の近似）": "film-standard-legacy",
}
INTERPOLATION_LABELS = {
    "tetrahedral（四面体・推奨）": "tetrahedral",
    "trilinear（三線形）": "trilinear",
    "nearest（最近傍・非推奨）": "nearest",
}
PRECISION_LABELS = {
    "float（浮動小数点・推奨）": "float",
    "lcms（native lcms2、見つかれば）": "lcms",
    "8bit（旧版の量子化パス）": "8bit",
}
DESC_MODE_LABELS = {
    "ベースICCの名称を引き継ぐ（カメラ紐づけ・既定）": "base",
    "カメラ名-LUT名（見分けやすい）": "look",
}


class Cube2IccApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("C-One LUT — CUBE → ICC")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.events: Queue = Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self.completed = 0
        self.paths: dict[str, Path] = {}
        self.path_rows: dict[str, str] = {}
        self.next_row = 0
        self.controls: list[tuple[ttk.Widget, str]] = []
        self.profile_dir, self.profile_files = find_profiles_dir()
        # Include profiles in subfolders so keyword search covers everything.
        self.all_profile_files = (
            _scan_profile_files(self.profile_dir) if self.profile_dir else list(self.profile_files)
        )
        self.base_icc_path = tk.StringVar()
        self.profile_search = tk.StringVar()
        self.profile_match_count = tk.StringVar()
        if self.all_profile_files:
            default_profile = next(
                (name for name in self.all_profile_files
                 if name.casefold().endswith(("generic.icm", "generic.icc"))),
                next((name for name in self.all_profile_files if "prostandard" in name.lower()),
                     self.all_profile_files[0]),
            )
            self.base_icc_path.set(default_profile)

        # Basic settings
        self.input_preset = tk.StringVar(value="Rec.709 Gamma 2.4")
        self.output_preset = tk.StringVar(value="Rec.709 Gamma 2.4")
        self.c1_curve_label = tk.StringVar(value=next(iter(C1_CURVE_LABELS)))
        self.midtone_gamma = tk.StringVar(value="1.0")
        self.validate = tk.BooleanVar(value=True)

        # Advanced settings
        self.input_gamut = tk.StringVar(value="ITU-R BT.709")
        self.input_transfer = tk.StringVar(value="Gamma 2.4")
        self.output_gamut = tk.StringVar(value="ITU-R BT.709")
        self.output_transfer = tk.StringVar(value="Gamma 2.4")
        self.interpolation_label = tk.StringVar(value=next(iter(INTERPOLATION_LABELS)))
        self.icc_grid = tk.StringVar(value=str(DEFAULT_GRID))
        self.icc_intent = tk.StringVar(value="perceptual")
        self.cat = tk.StringVar(value="Bradford")
        self.domain_policy = tk.StringVar(value="clamp")
        self.lut_domain_policy = tk.StringVar(value="clamp")
        self.precision_label = tk.StringVar(value=next(iter(PRECISION_LABELS)))
        self.desc_mode_label = tk.StringVar(value=next(iter(DESC_MODE_LABELS)))
        self.validation_samples = tk.StringVar(value="50000")
        self.legacy = tk.BooleanVar(value=False)

        # Output settings
        self.output_dir = tk.StringVar()
        self.existing_policy = tk.StringVar(value=next(iter(EXISTING_POLICIES)))
        self.install_profile_var = tk.BooleanVar(value=False)

        self.count_text = tk.StringVar(value="0 ファイル")
        self.status_text = tk.StringVar(value="CUBE ファイルとベース ICC を選択してください。")
        self.progress_text = tk.StringVar(value="0 / 0")
        self.metadata_text = tk.StringVar(value="CUBE メタデータ: —")
        self.setup_ui()
        self._fit_window()
        self.apply_presets()
        self.log(f"Capture One プロファイル: {self.profile_dir}" if self.profile_dir else
                 "Capture One のプロファイルが見つかりません。ベース ICC を参照して選択してください。")
        root.after(80, self.poll_events)

    # ---------------------------------------------------------------- UI

    def control(self, widget, state="normal"):
        self.controls.append((widget, state))
        return widget

    def setup_ui(self):
        apply_capture_one_style(self.root, ttk.Style(self.root))
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=3)
        frame.rowconfigure(6, weight=1)
        heading = ttk.Frame(frame)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(heading, text="CUBE → ICC", style="Heading.TLabel").pack(side=tk.LEFT)
        ttk.Label(heading, text="ベース ICC に LUT を焼き込み、ΔE2000 で検証します",
                  style=DIM_LABEL).pack(side=tk.LEFT, padx=14)
        base = ttk.Frame(frame)
        base.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        base.columnconfigure(1, weight=1)
        ttk.Label(base, text="プロファイル検索").grid(row=0, column=0, padx=(0, 12), sticky="w")
        search_entry = self.control(ttk.Entry(base, textvariable=self.profile_search),
                                    "normal" if self.all_profile_files else "disabled")
        search_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(base, textvariable=self.profile_match_count, style=DIM_LABEL).grid(
            row=0, column=2, padx=(8, 0), sticky="w")
        ttk.Label(base, text="ベース ICC").grid(row=1, column=0, padx=(0, 12), pady=(6, 0))
        self.base_combo = self.control(ttk.Combobox(base, textvariable=self.base_icc_path,
                                                    values=self.all_profile_files))
        self.base_combo.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.control(ttk.Button(base, text="参照…", command=self.browse_icc)).grid(
            row=1, column=2, padx=(8, 0), pady=(6, 0), sticky="w")
        self.profile_search.trace_add("write", self.apply_profile_filter)

        queue_frame = ttk.LabelFrame(frame, text="変換するファイル", padding=8)
        queue_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(queue_frame, style=CARD_FRAME)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        for label, command in [("ファイルを追加…", self.browse_cubes), ("フォルダーを追加…", self.browse_folder)]:
            self.control(ttk.Button(toolbar, text=label, command=command)).pack(side=tk.LEFT, padx=(0, 6))
        self.control(ttk.Button(toolbar, text="全てクリア", command=self.clear_files)).pack(side=tk.RIGHT)
        self.control(ttk.Button(toolbar, text="選択を削除", command=self.remove_selected)).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Label(toolbar, textvariable=self.count_text, style="Card.TLabel").pack(side=tk.RIGHT, padx=10)
        self.tree = ttk.Treeview(queue_frame, columns=("name", "folder", "status", "output"),
                                 show="headings", selectmode="extended", height=6)
        for column, label, width, stretch in [
            ("name", "CUBE ファイル", 230, True), ("folder", "入力フォルダー", 240, True),
            ("status", "状態", 80, False), ("output", "出力先", 330, True),
        ]:
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=60, stretch=stretch)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll_y = ttk.Scrollbar(queue_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll_y.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll_y.set)
        for tag, color in STATUS_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<Delete>", lambda _event: self.remove_selected())
        ttk.Label(queue_frame, textvariable=self.metadata_text, wraplength=980,
                  style="CardDim.TLabel").grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        notebook = ttk.Notebook(frame)
        notebook.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        notebook.add(self._basic_tab(notebook), text="基本設定")
        notebook.add(self._advanced_tab(notebook), text="詳細設定")
        notebook.add(self._output_tab(notebook), text="保存先・既存ファイル")

        progress_frame = ttk.Frame(frame)
        progress_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=1)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=12, anchor="e").grid(row=0, column=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        self.generate_button = self.control(ttk.Button(buttons, text="一括変換を開始",
                                                       command=self.start_generation,
                                                       style=PRIMARY_BUTTON))
        self.generate_button.pack(side=tk.LEFT, ipadx=20, ipady=3)
        self.cancel_button = ttk.Button(buttons, text="中止", command=self.cancel, state="disabled")
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 12), ipady=3)
        ttk.Label(buttons, text="Capture One 側では「ICC = 生成プロファイル / Curve = Linear Response」で使用してください。",
                  wraplength=560, justify=tk.LEFT, style=DIM_LABEL).pack(side=tk.LEFT, fill=tk.X)
        log_frame = ttk.LabelFrame(frame, text="処理ログ", padding=5)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=(0, 7))
        log_body = ttk.Frame(log_frame, style=CARD_FRAME)
        log_body.pack(fill=tk.BOTH, expand=True)
        log_body.columnconfigure(0, weight=1)
        log_body.rowconfigure(0, weight=1)
        self.log_area = configure_log_text(tk.Text(log_body, height=6, state="disabled", wrap=tk.WORD))
        self.log_area.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_body, orient=tk.VERTICAL, command=self.log_area.yview,
                                   style="Log.TScrollbar")
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_area.configure(yscrollcommand=log_scroll.set)
        ttk.Label(frame, textvariable=self.status_text, anchor="w", wraplength=1000).grid(row=7, column=0, sticky="ew")

    def _basic_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=10)
        for column in (1, 3):
            tab.columnconfigure(column, weight=1)
        ttk.Label(tab, text="LUT 入力プリセット").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        input_preset = self.control(ttk.Combobox(tab, textvariable=self.input_preset,
                                                 values=[*PRESETS, CUSTOM_PRESET], state="readonly"), "readonly")
        input_preset.grid(row=0, column=1, sticky="ew", pady=4)
        input_preset.bind("<<ComboboxSelected>>", lambda _e: self.apply_presets())
        ttk.Label(tab, text="LUT 出力プリセット").grid(row=0, column=2, sticky="w", padx=(20, 10), pady=4)
        output_preset = self.control(ttk.Combobox(tab, textvariable=self.output_preset,
                                                  values=[*PRESETS, CUSTOM_PRESET], state="readonly"), "readonly")
        output_preset.grid(row=0, column=3, sticky="ew", pady=4)
        output_preset.bind("<<ComboboxSelected>>", lambda _e: self.apply_presets())
        ttk.Label(tab, text="Capture One Curve").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self.control(ttk.Combobox(tab, textvariable=self.c1_curve_label,
                                  values=list(C1_CURVE_LABELS), state="readonly"), "readonly").grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(tab, text="追加中間調ガンマ").grid(row=1, column=2, sticky="w", padx=(20, 10), pady=4)
        self.control(ttk.Entry(tab, textvariable=self.midtone_gamma)).grid(row=1, column=3, sticky="ew", pady=4)
        self.control(ttk.Checkbutton(tab, text="ΔE2000 検証を実行してレポートを保存する（推奨）",
                                     variable=self.validate)).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(tab, text=" Alliance などの Rec.709 系 LUT は「Rec.709 Gamma 2.4」推奨。sRGB 専用 LUT は「sRGB」。"
                            "ヘッダの #Input: コメントは参考情報であり、トランスファーは自動決定されません。",
                  wraplength=960, style=DIM_LABEL).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        return tab

    def _advanced_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=10)
        for column in (1, 3):
            tab.columnconfigure(column, weight=1)
        rows = [
            ("入力色域", self.input_gamut, list(GAMUT_CHOICES)),
            ("入力トランスファー", self.input_transfer, list(TRANSFER_CHOICES)),
            ("出力色域", self.output_gamut, list(GAMUT_CHOICES)),
            ("出力トランスファー", self.output_transfer, list(TRANSFER_CHOICES)),
            ("CUBE 補間", self.interpolation_label, list(INTERPOLATION_LABELS)),
            ("ICC グリッド", self.icc_grid, [str(g) for g in ICC_GRID_CHOICES]),
            ("ICC レンダリングインテント", self.icc_intent, ["perceptual", "relative", "saturation", "mirror"]),
            ("色順応（CAT）", self.cat, list(CAT_CHOICES)),
            ("ドメインポリシー", self.domain_policy, ["clamp", "error", "extrapolate"]),
            ("LUT ドメインポリシー", self.lut_domain_policy, ["clamp", "error"]),
            ("CMS 精度", self.precision_label, list(PRECISION_LABELS)),
            ("プロファイル名（desc）", self.desc_mode_label, list(DESC_MODE_LABELS)),
            ("検証サンプル数", self.validation_samples, None),
        ]
        for index, (label, variable, values) in enumerate(rows):
            row, column = divmod(index, 2)
            ttk.Label(tab, text=label).grid(row=row, column=column * 2, sticky="w", padx=((0, 10) if column == 0 else (20, 10)), pady=3)
            widget = (ttk.Combobox(tab, textvariable=variable, values=values, state="readonly")
                      if values is not None else ttk.Entry(tab, textvariable=variable))
            self.control(widget, "readonly" if values is not None else "normal")
            widget.grid(row=row, column=column * 2 + 1, sticky="ew", pady=3)
        last_row = (len(rows) + 1) // 2
        self.control(ttk.Checkbutton(tab, text="旧版 (1.x) 互換モードで実行する（8bit CMM・三線形・33³ 再サンプル・Film Standard 補正）",
                                     variable=self.legacy)).grid(row=last_row, column=0, columnspan=4, sticky="w", pady=(8, 0))
        return tab

    def _output_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=10)
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="保存先").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.control(ttk.Entry(tab, textvariable=self.output_dir)).grid(row=0, column=1, sticky="ew")
        self.control(ttk.Button(tab, text="参照…", command=self.browse_output)).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(tab, text="空欄の場合は各 CUBE ファイルと同じフォルダーに保存").grid(
            row=1, column=1, sticky="w", pady=(2, 8))
        ttk.Label(tab, text="同名ファイル").grid(row=2, column=0, sticky="w", padx=(0, 10))
        self.control(ttk.Combobox(tab, textvariable=self.existing_policy,
                                  values=list(EXISTING_POLICIES), state="readonly"), "readonly").grid(row=2, column=1, sticky="ew")
        install_check = self.control(ttk.Checkbutton(
            tab, text="変換後に Capture One のプロファイルフォルダーへコピーする",
            variable=self.install_profile_var), "normal" if self.profile_dir else "disabled")
        install_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(9, 0))
        hint = (f"コピー先: {self.profile_dir}（書き込み権限が必要）" if self.profile_dir
                else "Capture One のインストール先は検出されていません。")
        ttk.Label(tab, text=hint, wraplength=850).grid(row=4, column=0, columnspan=3, sticky="w", pady=(3, 0))
        return tab

    def _fit_window(self):
        """Size the default window so every control, buttons included, is visible.

        The previous fixed 1080x860 clipped the bottom rows on DPI-scaled
        displays: the default size now follows the layout's requested size
        (clamped to the screen), and the vertical minimum prevents shrinking
        the window back into a clipped state.
        """
        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 1000)
        height = max(self.root.winfo_reqheight(), 700)
        width = min(width + 24, self.root.winfo_screenwidth() - 40)
        height = min(height + 24, self.root.winfo_screenheight() - 120)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(900, width), height)

    def apply_profile_filter(self, *_):
        """Filter the base profile dropdown by whitespace-separated keywords (AND).

        The selection box follows the search in real time: while keywords are
        active it shows the top hit unless the current profile still matches.
        Clearing the search restores the full list without touching the
        selection (a browsed absolute path is never clobbered).
        """
        if not hasattr(self, "base_combo"):
            return
        terms = self.profile_search.get().casefold().split()
        if terms:
            matches = [name for name in self.all_profile_files
                       if all(term in name.casefold() for term in terms)]
        else:
            matches = self.all_profile_files
        self.base_combo.configure(values=matches)
        self.profile_match_count.set(
            f"{len(matches)} / {len(self.all_profile_files)} 件" if self.all_profile_files else "")
        if terms and matches and self.base_icc_path.get() not in matches:
            self.base_icc_path.set(matches[0])

    def apply_presets(self):
        for preset_name, gamut_var, transfer_var in (
            (self.input_preset.get(), self.input_gamut, self.input_transfer),
            (self.output_preset.get(), self.output_gamut, self.output_transfer),
        ):
            preset = resolve_preset(preset_name)
            if preset:
                gamut_var.set(preset["input_gamut"] if preset_name == self.input_preset.get() else preset["output_gamut"])
                transfer_var.set(preset["input_transfer"] if preset_name == self.input_preset.get() else preset["output_transfer"])

    # ---------------------------------------------------------------- files

    def browse_icc(self):
        options = {"initialdir": str(self.profile_dir)} if self.profile_dir else {}
        filename = filedialog.askopenfilename(parent=self.root, title="ベース ICC プロファイルを選択",
            filetypes=[("ICC プロファイル", "*.icm *.icc"), ("全てのファイル", "*.*")], **options)
        if filename:
            self.base_icc_path.set(filename)

    def browse_cubes(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="CUBE ファイルを選択（複数選択可）",
            filetypes=[("CUBE LUT", "*.cube"), ("全てのファイル", "*.*")])
        self.add_paths(paths)

    def browse_folder(self):
        directory = filedialog.askdirectory(parent=self.root, title="CUBE ファイルを含むフォルダーを選択")
        if directory:
            self.add_paths(sorted(Path(directory).glob("*.cube")) + sorted(Path(directory).glob("*.CUBE")))

    def browse_output(self):
        directory = filedialog.askdirectory(parent=self.root, title="ICC の保存先を選択")
        if directory:
            self.output_dir.set(directory)

    @staticmethod
    def path_key(path: Path) -> str:
        return os.path.normcase(str(path))

    def add_paths(self, paths):
        if self.worker:
            return
        added = 0
        for value in paths:
            try:
                path = Path(value).expanduser().resolve()
                if path.suffix.lower() != ".cube" or not path.is_file():
                    continue
            except (OSError, ValueError):
                continue
            key = self.path_key(path)
            if key in self.path_rows:
                continue
            row = str(self.next_row)
            self.next_row += 1
            self.paths[row] = path
            self.path_rows[key] = row
            self.tree.insert("", tk.END, iid=row, values=(path.name, str(path.parent), "待機", ""))
            added += 1
        self.count_text.set(f"{len(self.paths)} ファイル")
        if added:
            self.status_text.set(f"{added} ファイルを追加しました。合計 {len(self.paths)} ファイル。")
            self.show_metadata(next(iter(self.paths.values())))

    def show_metadata(self, path: Path):
        try:
            lut = parse_cube(path)
            hints = lut.metadata_hints
            hint_text = f"、Input ヒント: {hints['input']}（トランスファーは未確定）" if "input" in hints else ""
            domain = "" if (np_is_zero(lut.domain_min) and np_is_one(lut.domain_max)) else \
                f"、DOMAIN {lut.domain_min.tolist()} .. {lut.domain_max.tolist()}"
            self.metadata_text.set(
                f"CUBE メタデータ: {lut.title} | {lut.size_3d or '-'}^3"
                + (f" + 1D shaper {lut.size_1d}" if lut.size_1d else "")
                + domain + hint_text)
        except Exception as error:
            self.metadata_text.set(f"CUBE メタデータを読めませんでした: {error}")

    def remove_selected(self):
        if self.worker:
            return
        for row in self.tree.selection():
            path = self.paths.pop(row)
            self.path_rows.pop(self.path_key(path), None)
            self.tree.delete(row)
        self.count_text.set(f"{len(self.paths)} ファイル")

    def clear_files(self):
        if self.worker:
            return
        self.tree.delete(*self.tree.get_children())
        self.paths.clear()
        self.path_rows.clear()
        self.count_text.set("0 ファイル")
        self.progress.configure(value=0)
        self.progress_text.set("0 / 0")
        self.metadata_text.set("CUBE メタデータ: —")
        self.status_text.set("CUBE ファイルを追加してください。")

    # ---------------------------------------------------------------- run

    def read_settings(self) -> tuple[BaseProfile, ConversionParams, Path | None]:
        raw_base = self.base_icc_path.get().strip()
        if not raw_base:
            raise ValueError("ベース ICC プロファイルを選択してください。")
        base_file = Path(raw_base).expanduser()
        if self.profile_dir and not base_file.is_absolute() and (self.profile_dir / base_file).is_file():
            base_file = self.profile_dir / base_file
        if not base_file.is_file():
            raise ValueError(f"ベース ICC が見つかりません: {base_file}")
        try:
            gamma = float(self.midtone_gamma.get())
        except ValueError as error:
            raise ValueError("追加中間調ガンマには 0 より大きい数値を入力してください。") from error
        if not (math.isfinite(gamma) and gamma > 0):
            raise ValueError("追加中間調ガンマには 0 より大きい有限の数値を入力してください。")
        output = self.output_dir.get().strip()
        output_dir = Path(output).expanduser().resolve() if output else None
        legacy = self.legacy.get()
        base = BaseProfile(base_file, precision="8bit" if legacy
                           else PRECISION_LABELS[self.precision_label.get()])
        # Legacy mode forces the same overrides as the CLI --legacy flag so the
        # 1.x pipeline is reproduced without requiring the user to also switch
        # the curve/interpolation/CAT selectors by hand (the ICC grid is forced
        # to 33 inside the pipeline).
        params = ConversionParams(
            input_gamut=self.input_gamut.get().strip(),
            input_transfer=self.input_transfer.get().strip(),
            output_gamut=self.output_gamut.get().strip(),
            output_transfer=self.output_transfer.get().strip(),
            c1_curve="film-standard-legacy" if legacy else C1_CURVE_LABELS[self.c1_curve_label.get()],
            midtone_gamma=gamma,
            interpolation="trilinear" if legacy else INTERPOLATION_LABELS[self.interpolation_label.get()],
            icc_grid=int(self.icc_grid.get()),
            icc_intent=self.icc_intent.get(),
            cat="CAT02" if legacy else self.cat.get(),
            domain_policy=self.domain_policy.get(),
            lut_domain_policy=self.lut_domain_policy.get(),
            precision=base.precision,
            legacy=legacy,
            desc_mode=DESC_MODE_LABELS[self.desc_mode_label.get()],
        )
        params.validate()
        return base, params, output_dir

    def start_generation(self):
        if self.worker:
            return
        if not self.paths:
            messagebox.showinfo("CUBE ファイルがありません", "変換する CUBE ファイルを追加してください。", parent=self.root)
            return
        try:
            base, params, output_dir = self.read_settings()
        except CONVERT_ERRORS as error:
            messagebox.showerror("設定を確認してください", str(error), parent=self.root)
            return
        paths = list(self.paths.values())
        # Snapshot every Tk variable here, in the main thread: the worker must
        # only see plain Python values. Tkinter calls from a worker thread
        # raise RuntimeError and used to kill the thread silently, leaving the
        # UI stuck at "変換を準備しています…".
        try:
            validation_samples = int(self.validation_samples.get() or DEFAULT_RANDOM_SAMPLES)
        except ValueError:
            validation_samples = DEFAULT_RANDOM_SAMPLES
            self.log("検証サンプル数の値が不正なため既定値を使用します。")
        if validation_samples < 0:
            messagebox.showerror("設定を確認してください", "検証サンプル数は0以上にしてください。0は格子点のみで検証します。", parent=self.root)
            return
        options = {
            "existing": EXISTING_POLICIES[self.existing_policy.get()],
            "validate": self.validate.get(),
            "validation_samples": validation_samples,
            "install": bool(self.install_profile_var.get() and self.profile_dir),
        }
        self.completed = 0
        self.cancel_event.clear()
        self.progress.configure(maximum=len(paths), value=0)
        self.progress_text.set(f"0 / {len(paths)}")
        for row, path in self.paths.items():
            self.tree.item(row, values=(path.name, str(path.parent), "待機", ""), tags=())
        self.log(f"\n{len(paths)} ファイルの変換を開始します。")
        self.log(f"ベース ICC: {base.path}  (PCS: {base.pcs.decode()})")
        self.log(f"CUBE 入力: {params.input_gamut} / {params.input_transfer}  |  "
                 f"CUBE 出力: {params.output_gamut} / {params.output_transfer}  |  "
                 f"C1 Curve: {params.c1_curve}  |  中間調ガンマ: {params.midtone_gamma}")
        self.worker = threading.Thread(
            target=self.conversion_worker,
            args=(paths, base, params, output_dir, options), name="conelut-convert")
        self.set_busy(True)
        self.status_text.set("変換を準備しています…")
        self.worker.start()

    def conversion_worker(self, paths, base, params, output_dir, options):
        results = []
        used = set()
        protected = set(paths) | {base.path}
        total = len(paths)
        run_report = RunReport.start(base.path,
                                     settings_from_params(params, options["validation_samples"]))
        try:
            for index, path in enumerate(paths, 1):
                if self.cancel_event.is_set():
                    results.append((path, None, "cancelled", "キャンセルしました。", None))
                    run_report.add(path, None, "cancelled", "キャンセルしました。")
                    self.events.put(("result", path, None, "cancelled", "キャンセルしました。", index, total, None))
                    continue
                self.events.put(("start", path, None, "", "", index, total, None))
                result = None
                try:
                    result = convert_file(
                        path, base, params,
                        output_dir=output_dir, existing=options["existing"],
                        validate=options["validate"],
                        validation_samples=options["validation_samples"],
                        write_json=False, verbose_log=False,
                        used=used, protected=protected,
                        log=lambda message: self.events.put(("log", message)),
                    )
                    results.append((path, result.output_path, result.status, result.message,
                                    result.summary))
                    run_report.add(path, result.output_path, result.status, "",
                                   summary=result.summary)
                    self.events.put(("result", path, result.output_path, result.status,
                                     result.message, index, total, result.summary))
                except CONVERT_ERRORS as error:
                    results.append((path, None, "error", str(error), None))
                    run_report.add(path, None, "error", str(error))
                    self.events.put(("result", path, None, "error", str(error), index, total, None))
                if result is not None and result.output_path and options["install"] and self.profile_dir:
                    try:
                        installed = install_profile(result.output_path, Path(self.profile_dir), options["existing"], protected=protected, used=used,
                                                    log=lambda message: self.events.put(("log", message)))
                        self.events.put(("log", f"インストール先: {installed}" if installed else "同名ICCがあるためインストールをスキップしました。"))
                    except CONVERT_ERRORS as error:
                        self.events.put(("log", f"ICCの保存は完了しましたが、インストールに失敗しました: {error}"))
        except BaseException as error:  # noqa: BLE001 - the UI must never stay stuck
            self.events.put(("log", f"内部エラー: {type(error).__name__}: {error}"))
            results.append((None, None, "error", f"内部エラー: {type(error).__name__}: {error}", None))
            run_report.add(None, None, "error", f"内部エラー: {type(error).__name__}: {error}")
        finally:
            # Always posted, even when the worker itself failed.
            report_path = self._write_run_report(run_report, paths, output_dir,
                                                 options["existing"], used, protected)
            self.events.put(("done", results, report_path))

    def _write_run_report(self, run_report, paths, output_dir, existing, used, protected):
        """One aggregate JSON per run, beside the outputs; never blocks the UI."""
        try:
            outputs = [entry["output_path"] for entry in run_report.entries if entry["output_path"]]
            first = Path(outputs[0]) if outputs else None
            directory = output_dir or (first.parent if first else None) or Path(paths[0]).parent
            path = choose_destination(run_report_path(directory), existing, used, protected,
                                      lambda _message: None)
            if path is not None:
                run_report.write(path)
                self.events.put(("log", f"実行レポート: {path}"))
                return path
        except OSError as error:
            self.events.put(("log", f"実行レポートの保存に失敗しました: {error}"))
        return None

    def set_busy(self, busy):
        for widget, state in self.controls:
            widget.configure(state="disabled" if busy else state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def cancel(self):
        if not self.worker:
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status_text.set("中止を要求しました。処理中のファイルが終わってから中止されます。")
        self.log("中止を要求しました。")

    def close(self):
        if self.worker:
            self.closing = True
            self.cancel()
            self.status_text.set("現在の処理が安全に終了するのを待ってからウィンドウを閉じます…")
        else:
            self.root.destroy()

    def log(self, message):
        self.log_area.configure(state="normal")
        self.log_area.insert(tk.END, str(message).rstrip() + "\n")
        line_count = int(self.log_area.index("end-1c").split(".")[0])
        if line_count > 5000:
            self.log_area.delete("1.0", f"{line_count - 4000}.0")
        self.log_area.see(tk.END)
        self.log_area.configure(state="disabled")

    def handle_event(self, event):
        """Dispatch one worker event.

        Event schemas posted by conversion_worker:
            ("log", message)
            ("done", results, report_path)
            ("start"|"result", path, output, status, message, index, total, summary)

        ``results`` is a list of (path, output, status, message, summary);
        ``summary`` carries the essential validation numbers only.
        """
        kind = event[0]
        if kind == "log":
            self.log(event[1])
            return
        if kind == "done":
            results, report_path = event[1], event[2]
            self.worker = None
            self.set_busy(False)
            counts = {"success": 0, "error": 0, "skipped": 0, "cancelled": 0}
            for entry in results:
                counts[entry[2]] = counts.get(entry[2], 0) + 1
            summary = " / ".join(f"{STATUS_LABELS[key]} {counts[key]}" for key in counts)
            self.status_text.set(f"処理終了: {summary}")
            self.log(f"処理終了: {summary}")
            summaries = [entry[4] for entry in results if entry[4]]
            if summaries:
                validated = sum(1 for s in summaries if s.get("mean") is not None)
                worst_mean = max(s["mean"] for s in summaries)
                worst_max = max(s["max"] for s in summaries)
                worst_status = max((s.get("validation_status") for s in summaries),
                                   key=lambda value: {"PASS": 0, "UNVERIFIED": 1, "REVIEW": 2}.get(value, 1))
                self.log(f"検証サマリ: {validated} ファイル / {worst_status}  |  "
                         f"最悪 平均 ΔE2000 {worst_mean:.4f}・最大 {worst_max:.4f}")
            if report_path is not None:
                self.status_text.set(f"処理終了: {summary}（レポート保存済み）")
            return
        _kind, path, output, status, message, _index, total, summary = event
        row = self.path_rows.get(self.path_key(Path(path))) if path is not None else None
        if kind == "start":
            if row is not None:
                self.tree.set(row, "status", "変換中")
                self.tree.see(row)
            if not self.cancel_event.is_set():
                self.status_text.set(f"変換中: {Path(path).name}")
        elif kind == "result":
            self.completed += 1
            label = STATUS_LABELS.get(status, status)
            if row is not None:
                self.tree.set(row, "status", label)
                self.tree.set(row, "output", str(output or ""))
                self.tree.item(row, tags=(status,))
            self.progress.configure(value=self.completed)
            self.progress_text.set(f"{self.completed} / {total}")
            metrics = format_metrics_line(summary) if summary else ""
            if metrics:
                # 必要数値だけ: file name + compact ΔE2000/lcms2/判定 line.
                self.log(f"[{label}] {Path(path).name}: {metrics}")
            else:
                self.log(f"[{label}] {Path(path).name}: {message}")

    def poll_events(self):
        # A failing handler must never kill the polling loop: if this after()
        # callback dies, no event is ever processed again and the UI freezes
        # at "変換を準備しています…" forever.
        try:
            for _ in range(200):
                try:
                    event = self.events.get_nowait()
                except Empty:
                    break
                try:
                    self.handle_event(event)
                except Exception as error:  # noqa: BLE001 - keep polling alive
                    self.log(f"内部エラー（イベント処理）: {type(error).__name__}: {error}")
                if self.closing and self.worker is None:
                    self.root.destroy()
                    return
        finally:
            self.root.after(80, self.poll_events)


def _scan_profile_files(directory: Path) -> list[str]:
    """Recursively list ICC/ICM files below the detected profile directory."""
    try:
        files = [
            path for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in {".icc", ".icm"}
        ]
    except OSError:
        return []
    return sorted((str(path.relative_to(directory)) for path in files), key=str.casefold)


def np_is_zero(values) -> bool:
    import numpy as np

    return bool(np.allclose(values, 0.0))


def np_is_one(values) -> bool:
    import numpy as np

    return bool(np.allclose(values, 1.0))


def apply_window_icon(root: tk.Tk) -> None:
    """Set the title-bar/taskbar icon (bundled at art/COneLUT.ico; bundled into
    _MEIPASS/art in frozen builds). Failure is cosmetic, never fatal."""
    here = Path(__file__).resolve().parent
    for candidate in (
        Path(getattr(sys, "_MEIPASS", here)) / "art" / "COneLUT.ico",
        here.parent / "art" / "COneLUT.ico",
    ):
        if candidate.is_file():
            try:
                root.iconbitmap(str(candidate))
                return
            except tk.TclError:
                continue


def main(argv=None):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if "--selftest" in arguments:
        # Headless smoke test for frozen builds: converts a synthetic profile
        # through the full pipeline and exits without opening a window.
        from conelut.selftest import run_selftest

        code, text = run_selftest()
        print(text)
        log_path = Path(os.environ.get("TEMP", ".")) / "COneLUT-selftest.log"
        try:
            log_path.write_text(text + "\n", encoding="utf-8")
            print(f"selftest log: {log_path}")
        except OSError:
            pass
        return code
    root = tk.Tk()
    apply_window_icon(root)
    app = Cube2IccApp(root)
    for value in arguments:
        path = Path(value).expanduser()
        if path.is_dir():
            app.add_paths(sorted(path.glob("*.cube")))
        else:
            app.add_paths([path])
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
