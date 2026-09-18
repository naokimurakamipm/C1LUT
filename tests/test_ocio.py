"""Optional independent CUBE parser/interpolator comparison (pip install opencolorio)."""
import numpy as np
import pytest

from c1lut.cube import parse_cube

ocio = pytest.importorskip("PyOpenColorIO")


@pytest.mark.parametrize("size", [17, 33, 65])
def test_native_cube_tetrahedral_matches_ocio(tmp_path, size):
    path = tmp_path / "nonlinear.cube"
    x = np.linspace(0, 1, size)
    # Deliberately asymmetric cross-channel mapping to expose grid/order errors.
    with path.open("w", encoding="ascii") as stream:
        stream.write(f"LUT_3D_SIZE {size}\n")
        for b in x:
            for g in x:
                for r in x:
                    stream.write(f"{.6*r*r+.2*g*b:.9f} {.7*g*g+.1*r*b:.9f} {.5*b*b+.3*r*g:.9f}\n")
    config = ocio.Config.CreateRaw()
    transform = ocio.FileTransform(src=str(path), interpolation=ocio.INTERP_TETRAHEDRAL)
    cpu = config.getProcessor(transform).getDefaultCPUProcessor()
    samples = np.random.default_rng(2309).random((2000, 3)).astype(np.float32)
    expected = samples.copy()
    cpu.applyRGB(expected)
    actual = parse_cube(path).apply(samples, "tetrahedral")
    assert np.allclose(actual, expected, atol=3e-7)
