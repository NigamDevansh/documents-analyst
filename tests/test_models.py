"""Local models and the degradation ladder — Phase 2C.

Two halves, and the split is the point:

  * **Unmarked tests** never load a model. They check the ladder in
    `CODE_ARCHITECTURE.md` §2.5 — what happens when each model is *absent* —
    which is the part that has to be right on a machine that has none.
  * **`models`-marked tests** load the real weights. In the image those come
    from `/opt/models`, baked at build time, so they need no network. On a host
    they need `uv sync --extra ml` and a prefetched cache, and they skip
    politely when either is missing rather than failing.

A degraded path that has never been executed is a paragraph in a document, not
a behaviour, which is why the first half exists at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from documents_analyst_core import model_cache
from documents_analyst_core.embeddings.base import DIMENSIONS
from documents_analyst_core.embeddings.openai_embeddings import (
    BATCH,
    EmbeddingUnavailable,
    OpenAIEmbeddings,
)
from documents_analyst_core.models import load_embeddings, load_ner, load_nli
from documents_analyst_core.nli.deberta import LABEL_ALIASES, DebertaNLI
from documents_analyst_core.nli.stub import StubNLI
from documents_analyst_core.portable.ner_gliner import DisabledNER

ABSENT = "not-a-real-org/not-a-real-model"

HOST_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "models"


def cache_root() -> str | None:
    """The image's baked cache, else a developer's local one.

    Checked with `is_populated`, not `exists`. The Dockerfile creates the tree
    before anything is fetched, so an existence check calls an empty cache ready
    and every model load then fails one at a time, deep inside a run.
    """
    for candidate in (model_cache.DEFAULT_ROOT, str(HOST_CACHE)):
        if model_cache.is_populated(candidate):
            return candidate
    return None


def needs_models() -> str | None:
    if cache_root() is None:
        return "no populated model cache — uv run python scripts/prefetch_models.py --dir .cache/models"
    try:
        import torch  # noqa: F401
    except ImportError:
        return "the ml extra is not installed — run: uv sync --extra ml"
    return None


# ── the ladder: embeddings are the one hard failure ───────────────────────


def test_embeddings_raise_rather_than_degrade() -> None:
    """The single model with no fallback.

    A stand-in embedder would return vectors, cosines would return numbers, and
    the results would be quietly wrong — a pile that finds no conflicts looks
    exactly like a pile that has none. There is no honest version of that, so
    there is no fallback.
    """
    with pytest.raises(EmbeddingUnavailable) as caught:
        load_embeddings(api_key="")

    message = str(caught.value)
    assert "OPENAI_API_KEY" in message, "name the setting that controls it"
    assert "no honest fallback" in message


def test_the_embedding_failure_is_raised_at_load_not_at_first_use() -> None:
    """Minutes into a pile is the wrong time to discover there is no key.

    This used to be a download that could fail; it is a missing key now, which
    is cheaper to check and has to be checked in the same place.
    """
    with pytest.raises(EmbeddingUnavailable):
        OpenAIEmbeddings(api_key="")


def test_a_key_alone_is_not_a_network_probe() -> None:
    """`is_available` answers without calling. A probe at every worker start
    would spend money on a question a missing key already answers, and would
    fail on a blip the first real call's retry would ride out."""
    assert OpenAIEmbeddings(api_key="sk-test").is_available is True


# ── the ladder: NLI degrades ──────────────────────────────────────────────


def test_a_missing_nli_model_degrades_to_neutral() -> None:
    """`StubNLI` returns pure neutral, which routes every candidate pair to LLM
    adjudication. The dishonest alternative — assuming "no contradiction" —
    would silently drop real conflicts."""
    client, loaded = load_nli(enabled=True, model_name=ABSENT)

    assert isinstance(client, StubNLI)
    assert loaded.degraded is True
    assert ABSENT in loaded.warning
    assert "escalates to the LLM" in loaded.warning

    result = client.classify("Payment is Net 30.", "Payment is Net 45.")
    assert (result.entailment, result.neutral, result.contradiction) == (0.0, 1.0, 0.0)


def test_switched_off_and_failed_to_load_do_not_read_the_same() -> None:
    """Both yield `StubNLI`, and a report that cannot tell them apart turns a
    deliberate configuration choice into a suspected outage."""
    off, off_state = load_nli(enabled=False, model_name=ABSENT)
    broken, broken_state = load_nli(enabled=True, model_name=ABSENT)

    assert isinstance(off, StubNLI) and isinstance(broken, StubNLI)
    assert off_state.disabled is True and off_state.degraded is False
    assert broken_state.degraded is True and broken_state.disabled is False
    assert "NLI_ENABLED=false" in off_state.warning


# ── the ladder: GLiNER degrades ───────────────────────────────────────────


def test_a_missing_gliner_degrades_to_no_candidates() -> None:
    """Party extraction yields **no candidates** — deliberately not "LLM-only".

    `DisabledNER` returns no spans, `pick_parties` chooses among candidates, so
    it returns nothing. Letting the model name parties freely instead would be
    the obvious "graceful" fallback and would break the 2B guarantee that a
    party name is always a verbatim span from the document.
    """
    client, loaded = load_ner(enabled=True, model_name=ABSENT)

    assert isinstance(client, DisabledNER)
    assert loaded.degraded is True
    assert client.extract("Between Acme and Beta.", ["organization"]) == []


def test_gliner_switched_off_is_distinguishable_too() -> None:
    _, off = load_ner(enabled=False, model_name=ABSENT)
    _, broken = load_ner(enabled=True, model_name=ABSENT)

    assert off.disabled is True and off.degraded is False
    assert broken.degraded is True
    assert "GLINER_ENABLED=false" in off.warning


def test_every_warning_names_the_model_and_what_was_lost() -> None:
    """A degradation message nobody can act on is noise on a banner."""
    for _, loaded in (
        load_nli(enabled=True, model_name=ABSENT),
        load_ner(enabled=True, model_name=ABSENT),
    ):
        assert ABSENT in loaded.warning
        assert "Unavailable capability" in loaded.warning


# ── label handling, no model needed ───────────────────────────────────────


def test_nli_labels_are_read_from_the_checkpoint_not_assumed() -> None:
    """MNLI checkpoints disagree about label ORDER. Assuming one is how you get
    a working model that reports contradiction as entailment — every real
    conflict silently reclassified as agreement."""

    class Config:
        id2label = {0: "contradiction", 1: "neutral", 2: "entailment"}

    assert DebertaNLI._read_labels(Config()) == ["contradiction", "neutral", "entailment"]


def test_unrecognised_labels_refuse_rather_than_guess() -> None:
    """A binary checkpoint — `entailment` / `not_entailment` — cannot express
    contradiction separately, which is this tier's entire job. Both real
    `deberta-v3-xsmall` NLI models are binary, which is why the architecture's
    original model choice could not work."""

    class Binary:
        id2label = {0: "entailment", 1: "not_entailment"}

    with pytest.raises(ValueError, match="refusing to guess"):
        DebertaNLI._read_labels(Binary())


def test_the_alias_table_covers_the_three_labels() -> None:
    assert set(LABEL_ALIASES.values()) == {"entailment", "neutral", "contradiction"}


# ── real models ───────────────────────────────────────────────────────────


# ── embeddings, against a fake SDK ────────────────────────────────────────
#
# No `models` marker and no network. The provider is an HTTP API now, so the
# behaviour worth pinning is what we *send* and what we do with what comes
# back — width, order, batching, retries, cost — and every one of those is
# checkable without a live key. A test that called the real endpoint would cost
# money to assert arithmetic.


class FakeEmbeddings:
    """Records every request and answers with vectors of the asked-for width."""

    def __init__(self, *, width: int | None = None, fail_first: int = 0) -> None:
        self.calls: list[dict[str, object]] = []
        self._width = width
        self._fail_first = fail_first
        self.embeddings = self

    def create(self, *, model: str, input: list[str], dimensions: int) -> object:  # noqa: A002
        self.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        if self._fail_first > 0:
            self._fail_first -= 1
            raise RuntimeError("rate limited")
        width = self._width or dimensions
        rows = [
            type("Row", (), {"index": i, "embedding": [float(i)] * width})()
            for i, _ in enumerate(input)
        ]
        usage = type("Usage", (), {"prompt_tokens": 7 * len(input)})()
        return type("Response", (), {"data": rows, "usage": usage})()


def fake_client(monkeypatch: pytest.MonkeyPatch, fake: FakeEmbeddings) -> OpenAIEmbeddings:
    client = OpenAIEmbeddings(api_key="sk-test")
    monkeypatch.setattr(client, "_sdk", lambda: fake)
    return client


def test_the_width_the_column_expects_is_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """`vector(384)` is fixed in the schema, and `text-embedding-3-small` is
    natively 1536. The `dimensions` argument is the whole reason changing
    provider was not also a migration — dropping it fails at INSERT with a
    message about the column rather than about the model."""
    fake = FakeEmbeddings()
    vectors = fake_client(monkeypatch, fake).embed_documents(["Net 30", "36 months"])

    assert fake.calls[0]["dimensions"] == DIMENSIONS
    assert [len(v) for v in vectors] == [DIMENSIONS, DIMENSIONS]


def test_a_vector_of_the_wrong_width_is_refused_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caught at the client, not at the INSERT three frames later."""
    client = fake_client(monkeypatch, FakeEmbeddings(width=1536))

    with pytest.raises(EmbeddingUnavailable) as caught:
        client.embed_documents(["anything"])
    assert "384" in str(caught.value)


def test_order_survives_a_response_that_arrives_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers zip these back against their facts by position. A reordering
    would attach every vector to the wrong text, and wrong vectors still
    cluster — so nothing would look broken."""

    class Shuffled(FakeEmbeddings):
        def create(self, **kwargs: object) -> object:  # type: ignore[override]
            response = super().create(**kwargs)  # type: ignore[arg-type]
            response.data = list(reversed(response.data))  # type: ignore[attr-defined]
            return response

    vectors = fake_client(monkeypatch, Shuffled()).embed_documents(["a", "b", "c"])

    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0]


def test_a_long_list_is_batched_and_comes_back_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeEmbeddings()
    texts = [f"subject {i}" for i in range(BATCH + 5)]

    vectors = fake_client(monkeypatch, fake).embed_documents(texts)

    assert len(fake.calls) == 2
    assert len(vectors) == len(texts)
    assert len(fake.calls[0]["input"]) == BATCH  # type: ignore[arg-type]


def test_a_transient_failure_is_retried_because_embedding_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opposite call to `chat_async`, and for a reason that is in the code:
    an embedding is a pure read, so a retry after an ambiguous timeout cannot
    duplicate anything."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    client = fake_client(monkeypatch, FakeEmbeddings(fail_first=1))

    vectors = client.embed_documents(["retry me"])

    assert len(vectors) == 1
    assert client.last_usage is not None
    assert client.last_usage.attempts == 2, "a retry that cost tokens is still recorded"


def test_a_permanent_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad key, a model that does not exist and a malformed request answer
    the same way three times. The common one is real: `EMBEDDING_MODEL` left on
    `BAAI/bge-small-en-v1.5` after the switch, which is a 404 — and the useful
    response is the message, not 1.5 seconds of sleeps before it."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)

    class NotFound(FakeEmbeddings):
        def create(self, **kwargs: object) -> object:  # type: ignore[override]
            super().create(**kwargs)  # type: ignore[arg-type]
            error = RuntimeError("model not found")
            error.status_code = 404  # type: ignore[attr-defined]
            raise error

    fake = NotFound()
    with pytest.raises(EmbeddingUnavailable) as caught:
        fake_client(monkeypatch, fake).embed_documents(["anything"])

    assert len(fake.calls) == 1, "a 404 is the answer, not a reason to wait"
    assert slept == []
    assert "not found" in str(caught.value)


def test_an_empty_string_does_not_take_the_batch_down_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API rejects an empty input, and rejects the whole batch for one of
    them. Dropping it instead would be worse than the 400: every later vector
    would shift onto the wrong fact, and nothing would look broken."""
    fake = FakeEmbeddings()
    vectors = fake_client(monkeypatch, fake).embed_documents(["real subject", "", "   "])

    assert len(vectors) == 3, "one blank must not cost the other two their vectors"
    assert all(sent.strip() != "" or sent == " " for sent in fake.calls[0]["input"])  # type: ignore[union-attr]
    assert "" not in fake.calls[0]["input"]  # type: ignore[operator]


def test_what_the_batch_cost_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embeddings were free when they ran locally and nothing recorded them.
    They are billed now, and this repo's rule is that a cost report showing a
    billable call as free is lying."""
    client = fake_client(monkeypatch, FakeEmbeddings())

    client.embed_documents(["a", "b", "c"])

    assert client.last_usage is not None
    assert client.last_usage.input_tokens == 21
    assert client.last_usage.output_tokens == 0, "an embedding has no completion"
    assert client.last_usage.model == "text-embedding-3-small"


def test_embedding_nothing_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document with no open subjects must not spend a request — and must not
    leave the previous batch's cost lying around to be recorded twice."""
    fake = FakeEmbeddings()
    client = fake_client(monkeypatch, fake)
    client.embed_documents(["warm"])

    assert client.embed_documents([]) == []
    assert client.last_usage is None
    assert len(fake.calls) == 1


@pytest.mark.models
def test_nli_calls_a_real_contradiction_a_contradiction() -> None:
    """The behaviour tier 2 exists for. Verified on a contract-shaped pair
    rather than a textbook one."""
    if (reason := needs_models()) is not None:
        pytest.skip(reason)

    client, loaded = load_nli(
        enabled=True,
        model_name="MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
        cache_dir=str(model_cache.hf_hub(cache_root())),
    )
    assert loaded.degraded is False

    conflict = client.classify("Payment is due within 30 days.", "Payment is due within 45 days.")
    unrelated = client.classify(
        "The agreement is governed by Delaware law.", "The initial term is 36 months."
    )

    assert conflict.contradiction > 0.5
    assert unrelated.neutral > conflict.neutral

    # The three scores are probabilities, but only approximately: transformers
    # runs this checkpoint in reduced precision, so a real softmax sums to about
    # 0.9998 rather than exactly 1. A 1% tolerance still catches the failure
    # worth catching — logits handed back unnormalised, or a label read into the
    # wrong slot, both of which miss by far more than rounding.
    total = conflict.entailment + conflict.neutral + conflict.contradiction
    assert total == pytest.approx(1.0, abs=0.01)


@pytest.mark.models
def test_gliner_finds_organisations_with_offsets_that_verify() -> None:
    """The real model going through the same span validation as a stub."""
    if (reason := needs_models()) is not None:
        pytest.skip(reason)

    from documents_analyst_core.entities.parties import extract_party_candidates
    from documents_analyst_core.portable.ner_gliner import GLiNERClient

    preamble = (
        "This Master Services Agreement is entered into between Acme Corporation "
        '("Customer") and Beta Logistics LLC ("Supplier").'
    )
    ner = GLiNERClient(
        model_name="knowledgator/gliner-pii-base-v1.0",
        cache_dir=str(model_cache.hf_hub(cache_root())),
    )
    candidates = extract_party_candidates(preamble, ner, threshold=0.3)

    assert candidates, "the real model found no organisations in a plain preamble"
    for candidate in candidates:
        assert preamble[candidate.start : candidate.end] == candidate.text


# ── the layout is one definition ──────────────────────────────────────────


def test_the_dockerfile_exports_exactly_what_model_cache_declares() -> None:
    """The guard against the bug this module was written for.

    The prefetch script wrote weights to one layout, the tests read from
    another, and the Dockerfile exported a third — so the image baked 2GB at
    build time and downloaded them again at runtime. Nothing failed: a second
    download just works, slowly, with a network. This asserts the three agree.
    """
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()

    for key, value in model_cache.environment(model_cache.DEFAULT_ROOT).items():
        assert f"{key}={value}" in dockerfile, f"Dockerfile does not export {key}={value}"


def test_the_hub_directory_is_below_hf_home() -> None:
    """HuggingFace derives its cache as `$HF_HOME/hub`. Passing `cache_dir=
    $HF_HOME` writes one level above where an env-driven lookup searches — which
    is exactly how the duplicate download happened."""
    assert model_cache.hf_hub("/tmp/x") == model_cache.hf_home("/tmp/x") / "hub"


def test_an_empty_cache_tree_does_not_count_as_populated(tmp_path: Path) -> None:
    """The Dockerfile creates the directories before fetching anything."""
    model_cache.hf_hub(str(tmp_path)).mkdir(parents=True)

    assert model_cache.is_populated(str(tmp_path)) is False
    assert len(model_cache.missing(str(tmp_path))) == 2


def test_a_partial_cache_is_not_populated(tmp_path: Path) -> None:
    """One of two is not ready.

    "At least one model" called a cache with NLI but no GLiNER *ready* — and the
    real-model tests then failed one at a time with a download error rather than
    the run saying plainly what was absent.
    """
    hub = model_cache.hf_hub(str(tmp_path))
    (hub / "models--MoritzLaurer--DeBERTa-v3-base-mnli-fever-anli").mkdir(parents=True)

    assert model_cache.is_populated(str(tmp_path)) is False
    assert model_cache.missing(str(tmp_path)) == ["models--knowledgator--gliner-pii-base-v1.0"]


def test_a_complete_cache_is_populated(tmp_path: Path) -> None:
    for name in model_cache.EXPECTED["hub"]:
        (model_cache.hf_hub(str(tmp_path)) / name).mkdir(parents=True)

    assert model_cache.missing(str(tmp_path)) == []
    assert model_cache.is_populated(str(tmp_path)) is True


def test_the_cache_no_longer_expects_an_embedding_model(tmp_path: Path) -> None:
    """Embeddings moved to OpenAI, so a complete cache is two directories, not
    three. A layout that still named a model nothing loads would report every
    cache incomplete for ever — and the fix would be to download weights that
    nothing reads."""
    assert "fastembed" not in model_cache.EXPECTED
    assert not hasattr(model_cache, "fastembed_dir")
    assert "FASTEMBED_CACHE_PATH" not in model_cache.environment(str(tmp_path))


def test_the_host_cache_is_reachable_without_passing_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The documented host workflow, end to end.

    `prefetch_models.py --dir .cache/models` fills one place; a locally started
    worker passes no `cache_dir` at all. Before this, the loaders fell through
    to each library's own `~/.cache` and downloaded a second copy — the same bug
    the image had, on a developer's laptop.
    """
    monkeypatch.setenv("MODEL_CACHE_DIR", str(tmp_path))

    assert model_cache.hf_hub() == tmp_path.resolve() / "hf" / "hub"


def test_a_loader_given_no_cache_dir_uses_the_declared_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`cache_dir=None` must mean "the layout we declared", not "wherever the
    library keeps its own cache". The classes stay explicit — it is the loaders
    that apply the default, so a caller constructing one directly still has to
    say where the weights are."""
    monkeypatch.setenv("MODEL_CACHE_DIR", str(tmp_path))
    seen: dict[str, object] = {}

    class Recording(DebertaNLI):
        def __init__(self, **kwargs: object) -> None:
            seen["cache_dir"] = kwargs.get("cache_dir")
            super().__init__(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("documents_analyst_core.models.DebertaNLI", Recording)

    load_nli(enabled=True, model_name=ABSENT)

    assert seen["cache_dir"] == str(tmp_path.resolve() / "hf" / "hub")
    assert DebertaNLI(model_name=ABSENT).cache_dir is None, "the class stays explicit"


# ── the Dockerfile's layer order is load-bearing ──────────────────────────


def test_the_models_stage_cannot_see_application_code() -> None:
    """Guards a bug the docs described the opposite of.

    The models stage used to be `FROM builder`, and `builder` copies
    `packages/` and `backend/`. So a one-character edit to any Python file
    invalidated it and re-ran `uv sync --extra ml` plus a 2.2GB re-download —
    while BUILD_AND_RUNTIME.md claimed models were fetched first. Verified by
    touching a file and watching the prefetch run again.
    """
    lines = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text().splitlines()

    stage_of: list[str] = []
    current = ""
    for line in lines:
        if line.startswith("FROM "):
            current = line.split(" AS ")[-1].strip() if " AS " in line else "runtime"
        stage_of.append(current)

    models_start = next(i for i, s in enumerate(stage_of) if s == "models")
    models_end = next(
        (i for i in range(models_start, len(stage_of)) if stage_of[i] != "models"), len(stage_of)
    )
    body = lines[models_start:models_end]

    assert any(line.startswith("FROM deps AS models") for line in body), (
        "the models stage must branch from `deps`, which carries no source"
    )
    for line in body:
        if line.startswith("COPY "):
            assert "packages/ packages/" not in line and "backend/ backend/" not in line, (
                f"models stage copies application code: {line!r}"
            )


# ── the configuration handoff ─────────────────────────────────────────────


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`resolve_cache` calls `os.environ.update` by design — that is the point
    of it. Clearing the keys through monkeypatch records their real values and
    restores them at teardown, so one test cannot redirect every later model
    load in the session.

    `delenv`, not `setenv(…, "")`. An environment variable takes priority over
    `.env` in pydantic-settings, so an empty string does not mean "unset" — it
    shadows the file with a blank value, which is exactly the bug this suite is
    here to catch and would have hidden it.
    """
    for key in model_cache.environment():
        monkeypatch.delenv(key, raising=False)


def test_model_cache_dir_from_dotenv_reaches_the_loaders(
    tmp_path: Path, isolated_env: None
) -> None:
    """The gap this composition module was written to close.

    pydantic reads `.env` into a `Settings` field, which is **not** the same as
    exporting a process environment variable. So the inline form worked and the
    `.env` form silently did not — a documented knob that did nothing, and
    nothing failed: models just downloaded a second time into `~/.cache`.
    """
    import os

    from documents_analyst.models import resolve_cache
    from documents_analyst.settings import Settings

    dotenv = tmp_path / "probe.env"
    dotenv.write_text(f"MODEL_CACHE_DIR={tmp_path}\n")
    settings = Settings(_env_file=str(dotenv))  # type: ignore[call-arg]

    assert settings.model_cache_dir == str(tmp_path)
    assert resolve_cache(settings) == str(tmp_path)

    # Applied to the process, not just returned — as the variables the
    # libraries actually read.
    assert os.environ["HF_HOME"] == str(tmp_path.resolve() / "hf")
    assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(tmp_path.resolve() / "hf" / "hub")

    # And **not** as `MODEL_CACHE_DIR`. Writing our own input back into the
    # environment put an absolute path ahead of the configured one for every
    # later `Settings()` in the process, so what the setting resolved to
    # depended on whether a model had been loaded yet.
    assert "MODEL_CACHE_DIR" not in os.environ


def test_a_blank_setting_leaves_the_environment_alone(
    monkeypatch: pytest.MonkeyPatch, isolated_env: None
) -> None:
    """Inside the image the Dockerfile already exported `/opt/models`. A blank
    setting must not overwrite it with a default computed somewhere else."""
    from documents_analyst.models import resolve_cache
    from documents_analyst.settings import Settings

    monkeypatch.setenv("MODEL_CACHE_DIR", "/opt/models")
    assert resolve_cache(Settings(_env_file=None, model_cache_dir="")) is None  # type: ignore[call-arg]
    assert model_cache.root() == Path("/opt/models")


def test_gliner_enabled_is_settable_and_reaches_the_loader(tmp_path: Path) -> None:
    """`GLINER_ENABLED=false` was in the loader, the docs and the ladder — and
    in no settings file, so only a test could reach it."""
    from documents_analyst.settings import Settings

    dotenv = tmp_path / "off.env"
    dotenv.write_text("GLINER_ENABLED=false\nNLI_ENABLED=false\n")
    settings = Settings(_env_file=str(dotenv))  # type: ignore[call-arg]

    assert settings.gliner_enabled is False
    assert settings.nli_enabled is False

    _, state = load_ner(enabled=settings.gliner_enabled, model_name=settings.gliner_model)
    assert state.disabled is True and state.degraded is False


def test_cache_status_reports_without_loading_anything(tmp_path: Path, isolated_env: None) -> None:
    """`/health` calls this. It must not pull 2GB of weights into memory to
    answer whether the process is up."""
    from documents_analyst.models import cache_status
    from documents_analyst.settings import Settings

    status = cache_status(Settings(_env_file=None, model_cache_dir=str(tmp_path)))  # type: ignore[call-arg]

    assert status["complete"] is False
    assert len(status["missing"]) == 2  # type: ignore[arg-type]
    assert str(tmp_path) in str(status["root"])


# ── Gemini embeddings (the embedding option for Claude and Gemini users) ───


class _FakeGeminiModels:
    def __init__(self, width: int) -> None:
        self.width = width
        self.calls: list[dict] = []

    def embed_content(self, *, model, contents, config):  # noqa: ANN001, ANN201
        from types import SimpleNamespace

        self.calls.append({"model": model, "contents": list(contents), "config": config})
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(values=[float(i + 1)] * self.width) for i, _ in enumerate(contents)
            ]
        )


def _gemini(width: int = 384):  # noqa: ANN202
    from types import SimpleNamespace

    from documents_analyst_core.embeddings.gemini_embeddings import GeminiEmbeddings

    client = GeminiEmbeddings(api_key="g-test")
    fake = _FakeGeminiModels(width)
    client._client = SimpleNamespace(models=fake)
    return client, fake


def test_gemini_embeddings_are_384_wide_normalised_and_in_order() -> None:
    import math

    client, fake = _gemini()
    vectors = client.embed_documents(["first", "", "third"])

    assert len(vectors) == 3 and all(len(v) == 384 for v in vectors)
    assert all(abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9 for v in vectors)
    sent = fake.calls[0]
    assert sent["contents"][1] == " ", "an empty string is padded, never dropped"
    assert sent["config"].output_dimensionality == 384
    usage = client.last_usage
    assert usage is not None and usage.input_tokens > 0, "a billable call is never shown free"


def test_gemini_embeddings_refuse_the_wrong_width() -> None:
    from documents_analyst_core.embeddings.openai_embeddings import EmbeddingUnavailable

    client, _ = _gemini(width=768)
    with pytest.raises(EmbeddingUnavailable, match="384"):
        client.embed_documents(["x"])


def test_load_embeddings_picks_the_provider() -> None:
    from documents_analyst_core.embeddings.gemini_embeddings import GeminiEmbeddings
    from documents_analyst_core.embeddings.openai_embeddings import OpenAIEmbeddings
    from documents_analyst_core.models import load_embeddings

    gemini, _ = load_embeddings(api_key="g", model_name="gemini-embedding-001", provider="gemini")
    openai, _ = load_embeddings(api_key="o", model_name="text-embedding-3-small")
    assert isinstance(gemini, GeminiEmbeddings) and isinstance(openai, OpenAIEmbeddings)
