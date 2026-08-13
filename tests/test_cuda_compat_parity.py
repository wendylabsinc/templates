"""Drift tripwire for the JetPack 7 / CUDA 12 compat shim (WDY-2437 D1).

The shim (see WendyOS#1370 / WendyOS PR #1379) is copy-pasted verbatim into
every Dockerfile that builds on a JetPack-7 `dustynv/*` base, because this
repo has no way to centralize it: `wendy init` extracts only a single
`{language}/{template-name}/` directory from the repo tarball, so a shared
`common/` snippet can't reach the device build context, and a published base
image would need a registry pipeline this repo doesn't have (see the E1.2
task notes). Instead, this test asserts the RUN/ENV block stays byte-for-byte
identical everywhere it's copied, so a future edit to one copy can't silently
drift from the rest.

Only the executable RUN/ENV block is compared, not the surrounding prose
comment: python/go2-initial-test/gpu/Dockerfile's comment predates this fix
and describes the failure in torch-specific terms, while the other five
copies use generic wording — a pre-existing, harmless divergence that isn't
this test's concern.

No CI runs this yet (tracked as a follow-up); run it manually with:
    python3 -m pytest tests/
"""

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Anchor used only to confirm the shim's doc comment is present; the identity
# check below compares the RUN/ENV command text, not this comment.
HEADER = (
    "# ── JetPack 7 / WendyOS 0.17 GPU fix "
    "(WendyOS#1370; mirrors WendyOS PR #1379) ──"
)
BLOCK_START = "RUN mkdir -p /opt/cuda12/lib"
BLOCK_END = "ENV LD_LIBRARY_PATH=/opt/cuda12/lib"

EXTRA_DOCKERFILES = (
    pathlib.Path("python") / "go2-initial-test" / "gpu" / "Dockerfile",
)


def _dockerfiles_with_the_shim() -> list[pathlib.Path]:
    paths = sorted(REPO_ROOT.glob("*/camera-feed-yolo/Dockerfile"))
    paths += [REPO_ROOT / rel for rel in EXTRA_DOCKERFILES]
    return paths


def _extract_run_env_block(text: str, path: pathlib.Path) -> str:
    assert HEADER in text, (
        f"{path} is missing the JetPack 7 CUDA compat header comment — "
        f"either the shim was removed, or this test's glob needs updating."
    )
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END, start) + len(BLOCK_END)
    return text[start:end]


def test_cuda_compat_shim_present_in_exactly_the_expected_files():
    files = _dockerfiles_with_the_shim()
    assert len(files) == 6, (
        f"expected 6 Dockerfiles carrying the CUDA 12 compat shim "
        f"(5x */camera-feed-yolo/Dockerfile + python/go2-initial-test/gpu), "
        f"found {len(files)}: {[str(p) for p in files]}. A GPU template was "
        f"added, removed, or renamed — update this test's expectations "
        f"(and, if it's a new dustynv/* JetPack-7 template, make sure it "
        f"carries the shim too)."
    )


def test_cuda_compat_run_env_block_is_byte_identical_everywhere():
    files = _dockerfiles_with_the_shim()
    blocks = {
        path: _extract_run_env_block(path.read_text(), path) for path in files
    }

    reference_path, reference_block = next(iter(blocks.items()))
    for path, block in blocks.items():
        assert block == reference_block, (
            f"CUDA 12 compat RUN/ENV block in {path} has drifted from "
            f"{reference_path}. Keep the block byte-identical across all "
            f"six Dockerfiles, or update every copy together."
        )
