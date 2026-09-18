"""Threaded GUI conversion end-to-end: real worker thread, real event pump.

Guards the regressions that froze the GUI at "変換を準備しています…":
- worker events must match what poll_events/handle_event consume
- the polling loop must survive a failing handler
- Tk variables must not be read from the worker thread
"""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from helpers import identity_cube, make_synthetic_base, write_cube  # noqa: E402


@pytest.fixture()
def tk_root():
    # Creating the next Tk root right after destroying the previous one can
    # fail transiently on Windows; retry before giving up.
    root = None
    for _ in range(3):
        try:
            root = tk.Tk()
            break
        except tk.TclError:
            time.sleep(0.3)
    if root is None:
        pytest.skip("no display available for Tkinter")
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def _pump(root, app, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while app.worker is not None and time.time() < deadline:
        root.update()
        time.sleep(0.01)
    assert app.worker is None, "worker never finished (UI would stay stuck)"


def test_threaded_conversion_completes(tk_root, tmp_path):
    from gui import Cube2IccApp

    base = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    cube = write_cube(tmp_path / "film.cube", identity_cube(9))
    app = Cube2IccApp(tk_root)
    app.base_icc_path.set(str(base))
    app.add_paths([cube])
    app.output_dir.set(str(tmp_path / "out"))
    app.validation_samples.set("1500")
    app.start_generation()
    assert app.worker is not None

    _pump(tk_root, app)

    status = app.status_text.get()
    assert "処理終了" in status, status
    assert "完了 1" in status, status
    row = next(iter(app.path_rows.values()))
    assert app.tree.item(row, "values")[2] == "完了"
    outputs = list((tmp_path / "out").glob("*.icc"))
    reports = list((tmp_path / "out").glob("*.validation.json"))
    assert len(outputs) == 1
    assert len(reports) == 1
    # UI recovered: controls re-enabled, worker cleared.
    assert str(app.generate_button.cget("state")) != "disabled"


def test_threaded_conversion_reports_file_errors(tk_root, tmp_path):
    from gui import Cube2IccApp

    base = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    bad_cube = tmp_path / "broken.cube"
    bad_cube.write_text('TITLE "bad"\nLUT_3D_SIZE 2\n0 0 0\n0 0 0\n', encoding="ascii")
    app = Cube2IccApp(tk_root)
    app.base_icc_path.set(str(base))
    app.add_paths([bad_cube])
    app.output_dir.set(str(tmp_path / "out"))
    app.validation_samples.set("500")
    app.start_generation()

    _pump(tk_root, app)

    status = app.status_text.get()
    assert "処理終了" in status, status
    assert "エラー 1" in status, status
    row = next(iter(app.path_rows.values()))
    assert app.tree.item(row, "values")[2] == "エラー"
    assert not list((tmp_path / "out").glob("*.icc"))


def test_zero_sample_batch_keeps_both_same_name_outputs(tk_root, tmp_path):
    from gui import Cube2IccApp, EXISTING_POLICIES
    base = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    paths = []
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        paths.append(write_cube(tmp_path / folder / "film.cube", identity_cube(3)))
    app = Cube2IccApp(tk_root)
    app.base_icc_path.set(str(base))
    app.add_paths(paths)
    app.output_dir.set(str(tmp_path / "out"))
    app.validation_samples.set("0")
    app.existing_policy.set(next(key for key, value in EXISTING_POLICIES.items() if value == "overwrite"))
    app.start_generation()
    _pump(tk_root, app)
    assert "完了 2" in app.status_text.get(), app.log_area.get("1.0", "end")
    assert len(list((tmp_path / "out").glob("*.icc"))) == 2


def test_negative_samples_rejected_before_worker(tk_root, tmp_path, monkeypatch):
    from gui import Cube2IccApp
    messages = []
    monkeypatch.setattr("gui.messagebox.showerror", lambda *args, **kwargs: messages.append(args))
    app = Cube2IccApp(tk_root)
    app.base_icc_path.set(str(make_synthetic_base(tmp_path / "base.icc")))
    app.add_paths([write_cube(tmp_path / "film.cube", identity_cube(3))])
    app.validation_samples.set("-1")
    app.start_generation()
    assert app.worker is None
    assert messages


def test_legacy_toggle_forces_compat_settings(tk_root, tmp_path):
    """Checking the legacy box alone must reproduce the 1.x pipeline settings.

    Regression: the C1-curve selector staying on 'Linear Response' used to
    make read_settings raise, blocking conversion entirely.
    """
    from gui import Cube2IccApp

    base = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    app = Cube2IccApp(tk_root)
    app.base_icc_path.set(str(base))
    app.legacy.set(True)  # curve/interpolation/CAT selectors stay at defaults
    _base, params, _out = app.read_settings()

    assert params.legacy is True
    assert params.c1_curve == "film-standard-legacy"
    assert params.interpolation == "trilinear"
    assert params.cat == "CAT02"
    assert params.precision == "8bit"
