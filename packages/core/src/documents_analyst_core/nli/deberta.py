"""DeBERTa-v3-base-mnli-fever-anli via transformers.

Only ever sees prose pairs — scalar fields are decided by exact comparison in
tier 1, since asking a model whether 30 differs from 45 is slower and worse than
`!=`. Base, not xsmall: both real xsmall NLI checkpoints are binary
(`entailment` / `not_entailment`), folding neutral and contradiction into one
class, and distinguishing those two — "unrelated" must not read as "disagree" —
is the entire job of this tier. The cost is size, ~184M rather than ~70M.
"""

from __future__ import annotations

from typing import Any

from .base import NLIResult


class ModelUnavailable(Exception):
    """The NLI model would not load.

    Unlike bge-small this is **not** fatal: `StubNLI` returns pure neutral and
    every candidate pair escalates to LLM adjudication. Slower and pricier,
    still correct. The message still names the model and the setting, because
    "conflicts got expensive" is otherwise a mystery three days later.
    """

    def __init__(self, *, model: str, env_var: str = "NLI_MODEL") -> None:
        self.model = model
        self.env_var = env_var
        super().__init__(
            f"Could not load {model!r} (set by {env_var}). "
            "Unavailable capability: tier-2 conflict classification — every "
            "candidate pair escalates to the LLM instead."
        )


LABEL_ALIASES = {
    "entailment": "entailment",
    "entail": "entailment",
    "neutral": "neutral",
    "contradiction": "contradiction",
    "contradict": "contradiction",
}
"""MNLI checkpoints disagree about label spelling and about ORDER.

Some publish `id2label` as `{0: 'entailment', ...}`, others as
`{0: 'contradiction', ...}`, and a few use `LABEL_0`. Assuming an order is the
classic way to get a working model that reports contradiction as entailment —
every real conflict silently reclassified as agreement. So the labels are read
from the model's own config and mapped by name.
"""


class DebertaNLI:
    def __init__(
        self,
        *,
        model_name: str = "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
        cache_dir: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any = None
        self._tokenizer: Any = None
        self._order: list[str] = []
        self._load_error: Exception | None = None

    def load(self) -> tuple[Any, Any]:
        if self._model is not None:
            return self._tokenizer, self._model
        if self._load_error is not None:
            raise ModelUnavailable(model=self.model_name) from self._load_error
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.model_name, cache_dir=self.cache_dir
            )
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name, cache_dir=self.cache_dir
            )
            self._model.eval()
            self._order = self._read_labels(self._model.config)
        except Exception as exc:  # noqa: BLE001
            self._load_error = exc
            raise ModelUnavailable(model=self.model_name) from exc
        return self._tokenizer, self._model

    @staticmethod
    def _read_labels(config: Any) -> list[str]:
        """The checkpoint's own label order, normalised to our three names."""
        raw = getattr(config, "id2label", None) or {}
        order = [str(raw[key]).strip().lower() for key in sorted(raw, key=int)]
        mapped = [LABEL_ALIASES.get(label, "") for label in order]
        if sorted(mapped) != ["contradiction", "entailment", "neutral"]:
            raise ValueError(f"unrecognised NLI labels {order!r} — refusing to guess an order")
        return mapped

    @property
    def is_available(self) -> bool:
        try:
            self.load()
        except ModelUnavailable:
            return False
        return True

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        """Softmax over the checkpoint's own three labels.

        Raises `ModelUnavailable` rather than returning neutral on a load
        failure. Returning neutral here would be indistinguishable from a
        genuine "these do not contradict" — the caller must choose `StubNLI`
        deliberately, so the degradation appears once at startup instead of
        being invented on every call.
        """
        import torch

        tokenizer, model = self.load()
        inputs = tokenizer(
            premise, hypothesis, return_tensors="pt", truncation=True, max_length=512
        )
        with torch.no_grad():
            logits = model(**inputs).logits[0]
        scores = torch.softmax(logits, dim=-1).tolist()

        by_label = dict(zip(self._order, scores, strict=True))
        return NLIResult(
            entailment=float(by_label["entailment"]),
            neutral=float(by_label["neutral"]),
            contradiction=float(by_label["contradiction"]),
        )
