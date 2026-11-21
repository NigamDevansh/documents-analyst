"""Where local model weights live. One definition, every consumer.

The alternative failed: the prefetch script, the tests and the Dockerfile each
named a different layout, so the image baked 2GB of models and then downloaded
them again at runtime — silently, since a second download just works. So the
layout is declared once here and everything asks this module.

    <root>/
    └── hf/            HF_HOME
        └── hub/       HUGGINGFACE_HUB_CACHE  NLI + GLiNER

`hub/` is not decoration: HuggingFace derives its cache as `$HF_HOME/hub`, so
`cache_dir=$HF_HOME` puts files one level above where an env-driven lookup
searches — the mismatch that produced the duplicate download.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = "/opt/models"
"""Where the image bakes them. A host run passes something else explicitly."""


def root(explicit: str | None = None) -> Path:
    """The cache root: an argument, then `MODEL_CACHE_DIR`, then the image path."""
    return Path(explicit or os.environ.get("MODEL_CACHE_DIR") or DEFAULT_ROOT).resolve()


def hf_home(explicit: str | None = None) -> Path:
    return root(explicit) / "hf"


def hf_hub(explicit: str | None = None) -> Path:
    """What `cache_dir=` must be given for transformers and gliner.

    Equal to `$HF_HOME/hub`, which is what the libraries compute themselves
    when no `cache_dir` is passed. Keeping both routes on the same directory is
    the whole point of this module.
    """
    return hf_home(explicit) / "hub"


def environment(explicit: str | None = None) -> dict[str, str]:
    """The variables the Dockerfile sets, derived from the same functions.

    Returned rather than exported so a caller can diff them against a running
    process — `test_model_cache.py` asserts the Dockerfile agrees with this.
    """
    return {
        "MODEL_CACHE_DIR": str(root(explicit)),
        "HF_HOME": str(hf_home(explicit)),
        "HUGGINGFACE_HUB_CACHE": str(hf_hub(explicit)),
    }


DERIVED = ("HF_HOME", "HUGGINGFACE_HUB_CACHE")
"""What the libraries read. `MODEL_CACHE_DIR` is our *input*, not one of these."""


def apply_environment(explicit: str | None = None) -> dict[str, str]:
    """Export the layout, for a process that will load models by env alone.

    **`MODEL_CACHE_DIR` is deliberately not exported.** It is the setting this
    module derives everything else *from*, and `Settings` reads the environment
    ahead of `config/runtime.yaml` — so writing it back turned a relative
    configured value into an absolute one for every later read in the process,
    and the value depended on whether anything had loaded a model yet. Harmless
    in effect, invisible in cause, and it made the test suite order-dependent,
    which is how it was found.
    """
    env = {key: value for key, value in environment(explicit).items() if key in DERIVED}
    os.environ.update(env)
    return env


EXPECTED = {
    "hub": (
        "models--MoritzLaurer--DeBERTa-v3-base-mnli-fever-anli",
        "models--knowledgator--gliner-pii-base-v1.0",
    ),
}
"""The directories a complete cache holds, by name.

Named rather than counted. "At least one model" calls a cache with NLI but no
GLiNER *ready*, and the real-model tests then fail one at a time with a download
error instead of the run saying plainly that the cache is incomplete.
"""


def missing(explicit: str | None = None) -> list[str]:
    """Which expected model directories are absent. Empty means complete.

    Checked as "does this `models--*` directory exist", not "does the folder
    exist": the Dockerfile creates the tree before anything is fetched, so an
    existence check would call an empty cache ready.
    """
    return [n for n in EXPECTED["hub"] if not (hf_hub(explicit) / n).is_dir()]


def is_populated(explicit: str | None = None) -> bool:
    """True only when **every** expected model is present."""
    return not missing(explicit)
