"""Download every local model into a cache directory, ahead of time.

Runs in the Docker `models` stage, so the image carries the weights and the
test suite needs no network. "Offline tests, no live keys" is a scored
behaviour, and without this it is a behaviour that *appears* to pass on a
machine that happens to have a warm HuggingFace cache.

    python scripts/prefetch_models.py                 # → the env's cache
    python scripts/prefetch_models.py --dir .cache/models
    python scripts/prefetch_models.py --check         # report, download nothing

Two models. **DeBERTa NLI** and **GLiNER** both need torch, which is why they
live in the `[ml]` extra rather than the base dependencies.

Embeddings used to be a third — bge-small, through fastembed/ONNX. They are an
OpenAI call now, so there is nothing to prefetch and nothing to bake: see
`embeddings/openai_embeddings.py`. The offline test suite is unaffected, because
no test has ever called a live embedding API.

"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "core" / "src"))
from documents_analyst_core import model_cache  # noqa: E402 — path set above, before the import

MODELS = (
    ("nli", "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli", "tier-2 conflict classification"),
    ("gliner", "knowledgator/gliner-pii-base-v1.0", "party candidates and PII tier 2"),
)


def cache_env(directory: Path) -> None:
    """Point every library's cache at the layout `model_cache` declares.

    Overwritten, not `setdefault`: `--dir` has to win over whatever the shell
    already exported, or a developer with an old `HF_HOME` silently fills two
    caches and the one they asked for stays empty.
    """
    directory.mkdir(parents=True, exist_ok=True)
    model_cache.apply_environment(str(directory))


def fetch_nli(model: str, directory: Path) -> None:
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    cache = str(model_cache.hf_hub(str(directory)))
    AutoTokenizer.from_pretrained(model, cache_dir=cache)
    AutoModelForSequenceClassification.from_pretrained(model, cache_dir=cache)


def fetch_gliner(model: str, directory: Path) -> None:
    from gliner import GLiNER

    GLiNER.from_pretrained(model, cache_dir=str(model_cache.hf_hub(str(directory))))


FETCHERS = {"nli": fetch_nli, "gliner": fetch_gliner}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=os.environ.get("MODEL_CACHE_DIR", model_cache.DEFAULT_ROOT))
    ap.add_argument("--check", action="store_true", help="report state, download nothing")
    ap.add_argument("--only", choices=sorted(FETCHERS), help="fetch a single model")
    args = ap.parse_args()

    directory = Path(args.dir).resolve()
    cache_env(directory)

    wanted = [m for m in MODELS if not args.only or m[0] == args.only]
    if args.check:
        state = "populated" if model_cache.is_populated(str(directory)) else "EMPTY"
        print(f"cache: {directory}  ({state})")
        for key, value in model_cache.environment(str(directory)).items():
            print(f"  {key:24} {value}")
        for name, model, purpose in wanted:
            print(f"  {name:10} {model:42} {purpose}")
        return 0

    print(f"Prefetching {len(wanted)} model(s) into {directory}")
    failures: list[str] = []
    for name, model, purpose in wanted:
        print(f"  {name:10} {model} … ", end="", flush=True)
        try:
            FETCHERS[name](model, directory)
        except Exception as exc:  # noqa: BLE001 — report all, fail once at the end
            print(f"FAILED\n             {type(exc).__name__}: {exc}")
            failures.append(f"{name} ({purpose})")
            continue
        print("ok")

    if failures:
        # Non-zero, so a Docker build stops here rather than producing an image
        # that looks fine and downloads at runtime — which is exactly the
        # failure this script exists to prevent, moved somewhere less visible.
        print(f"\n{len(failures)} model(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"\nAll {len(wanted)} model(s) cached in {directory}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
