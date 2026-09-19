"""Render the GUI and capture a screenshot (dev tool for README/visual QA).

Usage: python tools/shot_ui.py
Requires numpy and Pillow in addition to the runtime dependencies. The window
is placed deterministically, raised above other windows and captured via a
full-screen grab so winfo coordinates match physical pixels (the app opts
into per-monitor DPI awareness at import).
"""
from __future__ import annotations

import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from gui import Cube2IccApp  # noqa: E402


def make_sample_cubes(directory: Path) -> None:
    lines = ["TITLE \"SampleLook\"", "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0",
             "LUT_3D_SIZE 2", ""]
    # Identity 2x2x2 (R slowest: r outer, then g, then b).
    for r in (0.0, 1.0):
        for g in (0.0, 1.0):
            for b in (0.0, 1.0):
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    (directory / "Gold200.cube").write_text("\n".join(lines), encoding="utf-8")
    lines[0] = "TITLE \"OtherLook\""
    (directory / "Teal_Light.cube").write_text("\n".join(lines), encoding="utf-8")


def grab_clean(root: tk.Tk, tries: int = 5):
    # Deterministic placement, away from taskbar/overlay hot spots.
    root.geometry("+260+220")
    root.attributes("-topmost", True)
    root.update()
    for attempt in range(tries):
        time.sleep(0.4)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        full = ImageGrab.grab(all_screens=True).convert("RGB")
        image = full.crop((x, y, x + w, y + h))
        arr = np.asarray(image)
        # Foreign tooltips show up as near-solid bright rows; text glyphs do not.
        solid_rows = int((arr.mean(axis=2).mean(axis=1) > 190).sum())
        if solid_rows == 0:
            return image, (arr.mean(axis=2) > 190).mean()
        print(f"capture attempt {attempt + 1}: {solid_rows} solid bright rows, moving window")
        root.geometry(f"+{260 + attempt * 140}+{220 + attempt * 90}")
        root.update()
    raise SystemExit("screen keeps containing bright foreign UI; aborting")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        make_sample_cubes(tmpdir)
        root = tk.Tk()
        try:
            app = Cube2IccApp(root)
            app.add_paths([str(tmpdir / "Gold200.cube"), str(tmpdir / "Teal_Light.cube")])
            rows = app.tree.get_children()
            if rows:
                app.tree.set(rows[0], "status", "完了")
                app.tree.set(rows[0], "verify", "0.0019 → PASS")
                app.tree.set(rows[0], "output", r"ICC\LeicaSL601-Gold200.icc")
                app.tree.item(rows[0], tags=("success",))
                app.tree.set(rows[1], "status", "完了")
                app.tree.set(rows[1], "verify", "0.0022 → PASS")
            app.progress.configure(maximum=2, value=2)
            app.progress_text.set("2 / 2")
            app.status_text.set("処理終了: 完了 2 / エラー 0 / スキップ 0 / 中止 0（レポート保存済み）")
            app.log("2 ファイルの変換を開始します。")
            app.log("Gold200.cube を変換中…  Wrote: ICC\\LeicaSL601-Gold200.icc")
            app.log("[完了] Gold200.cube: ΔE2000 平均 0.0019 / P95 0.0041 / 最大 0.0288"
                    "  |  lcms2 平均 0.0021 / 最大 0.0304  |  → PASS")
            app.log("[完了] Teal_Light.cube: ΔE2000 平均 0.0022 / P95 0.0047 / 最大 0.0331"
                    "  |  lcms2 平均 0.0024 / 最大 0.0352  |  → PASS")
            app.log("実行レポート: ICC\\COneLUT-run-20260919-101253.json")
            app.log("処理終了: 完了 2 / エラー 0 / スキップ 0 / 中止 0")
            app.log("検証サマリ: 2 ファイル / PASS  |  最悪 平均 ΔE2000 0.0022・最大 0.0331")
            root.update_idletasks()
            root.update()
            image, bright = grab_clean(root)
            out = ROOT / "art" / "ui_preview.png"
            image.save(out)
            image.convert("RGB").save(ROOT / "art" / "ui_preview.jpg", quality=92)
            print("saved", out, image.size, f"bright={bright:.3%}")
        finally:
            root.destroy()


if __name__ == "__main__":
    main()
