"""Embedder Protocol and the default sentence-transformers implementation.

The default embedder is SkillRet-Embedding-0.6B (a Qwen3 fine-tune for
skill retrieval). Any HuggingFace sentence-transformers model can be
plugged in by setting ``[embedder] model = "<hf-id>"`` in
``~/.config/mega-tron/config.toml`` or by exporting
``MEGA_EMBEDDER_MODEL=<hf-id>``.

The generic :class:`SentenceTransformerEmbedder` handles every standard
model. SkillRet's instruction prefix is special-cased because the model
card requires it; unknown model ids pass texts through as-is, which is
correct for symmetric retrievers like BGE / e5 / all-MiniLM.

The cache fingerprint includes the weight hash, so swapping models is
safe and the only thing the cache needs from the embedder is ``dim``,
``model_id``, and a stable ``fingerprint``.
"""
from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np


# ---------------------------------------------------------------------------
# Known asymmetric models — prefix queries differently from docs.
# ---------------------------------------------------------------------------
#
# Keep this table tiny. The only models that absolutely require a prefix
# are instruction-tuned retrievers; anything else can use raw text on
# both sides. New entries go in only after we've verified them against
# the model card.

KNOWN_QUERY_PREFIXES: dict[str, str] = {
    "ThakiCloud/SKILLRET-Embedding-0.6B": (
        "Instruct: Given a skill search query, retrieve relevant skills "
        "that match the query\nQuery: "
    ),
    "ThakiCloud/SKILLRET-Embedding-8B": (
        "Instruct: Given a skill search query, retrieve relevant skills "
        "that match the query\nQuery: "
    ),
    # Qwen3-Embedding family uses the same instruction format as the
    # SkillRet fine-tunes derived from it. The official model card
    # recommends task-specific instructions on the query side only; we
    # use the skill-retrieval phrasing so cross-lingual skill matching
    # stays semantically aligned across English / Korean / Hindi / etc.
    "Qwen/Qwen3-Embedding-0.6B": (
        "Instruct: Given a skill search query, retrieve relevant skills "
        "that match the query\nQuery: "
    ),
    "Qwen/Qwen3-Embedding-4B": (
        "Instruct: Given a skill search query, retrieve relevant skills "
        "that match the query\nQuery: "
    ),
    "Qwen/Qwen3-Embedding-8B": (
        "Instruct: Given a skill search query, retrieve relevant skills "
        "that match the query\nQuery: "
    ),
}


def _pick_device(preferred: str = "auto") -> str:
    """Pick the best available device. ``auto`` → CUDA → MPS → CPU."""
    if preferred != "auto":
        return preferred
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


@runtime_checkable
class Embedder(Protocol):
    """Anything that maps a list of strings to a 2D numpy array of unit vectors.

    Returned shape: (len(texts), dim). Vectors should be L2-normalized so the
    Router can use plain dot product instead of full cosine.

    ``fingerprint`` is the cache invalidation key — implementers should derive
    it from any state that affects the output (model weights, version pin,
    quantization, instruction prefix, etc.).

    ``query_prefix`` (optional) is an instruction string the Router prepends to
    query text before embedding. Cache builds never apply it, so document
    embeddings stay prefix-free as the asymmetric retrievers expect.
    """

    model_id: str
    dim: int

    @property
    def fingerprint(self) -> str:  # pragma: no cover — protocol
        ...

    def embed(self, texts: list[str]) -> "np.ndarray":  # pragma: no cover — protocol
        ...


class SentenceTransformerEmbedder:
    """Generic adapter for any sentence-transformers model on HuggingFace.

    Loads the model on the best available device (CUDA → MPS → CPU),
    reads ``dim`` from the loaded model so we stay honest if a variant
    ships with different dimensions, and applies the known query prefix
    when the model id is in :data:`KNOWN_QUERY_PREFIXES` (the Router
    prepends ``query_prefix`` to query text only — never to documents).
    """

    def __init__(
        self,
        model_id: str,
        device: str = "auto",
        *,
        query_prefix: Optional[str] = None,
        trust_remote_code: bool = True,
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self.device = _pick_device(device)
        # Asymmetric models (SkillRet, instruction-tuned Qwen3, …) need a
        # query prefix. Caller can override; otherwise we look up the
        # known-models table.
        if query_prefix is None:
            query_prefix = KNOWN_QUERY_PREFIXES.get(model_id, "")
        self.query_prefix = query_prefix
        # Offline-first load order: try ``local_files_only=True`` first so
        # that the model is loaded from the on-disk HuggingFace cache
        # without any network reach. Only fall back to the online load
        # path when the cache genuinely doesn't have the model (first-
        # install case). This is the fix for the codex-sandbox failure
        # mode where ``mega-tron search`` was invoked under a network-
        # restricted shell — the model is already cached from setup, so
        # there's no reason for every CLI call to round-trip to
        # ``huggingface.co`` and crash.
        #
        # ``HF_HUB_OFFLINE=1`` is honored implicitly by the HF Hub
        # library; users who set it explicitly force the strict path
        # and skip the online fallback below.
        offline_env = os.environ.get("HF_HUB_OFFLINE") in {"1", "true", "TRUE"}
        self._model = self._load_model(
            model_id,
            trust_remote_code=trust_remote_code,
            strict_offline=offline_env,
        )
        try:
            dim = self._model.get_sentence_embedding_dimension()
        except AttributeError:
            dim = getattr(self._model, "get_embedding_dimension", lambda: 0)()
        self.dim = int(dim or 0)
        self._fingerprint: str | None = None

    def _load_model(
        self,
        model_id: str,
        *,
        trust_remote_code: bool,
        strict_offline: bool,
    ):
        """Load the SentenceTransformer model, offline-first.

        Tries the local HF cache first (``local_files_only=True``) so a
        cached model loads without any network reach. Falls back to the
        online path only when the cache genuinely lacks the model — that
        is, on the user's first install, before ``mega-tron setup`` has
        downloaded the embedder.

        ``strict_offline=True`` (set when ``HF_HUB_OFFLINE`` is in the
        environment) skips the online fallback entirely, so a missing
        cache surfaces as a real error instead of a silent network
        attempt that the user has already declared unwanted.
        """
        from sentence_transformers import SentenceTransformer

        def _try_load(*, local_only: bool):
            kwargs = {"device": self.device}
            if trust_remote_code:
                kwargs["trust_remote_code"] = True
            if local_only:
                kwargs["local_files_only"] = True
            try:
                return SentenceTransformer(model_id, **kwargs)
            except TypeError:
                # Older sentence-transformers builds don't accept
                # ``trust_remote_code`` (and may not accept
                # ``local_files_only`` either). Retry with the minimal
                # signature so the standard BGE / e5 / all-MiniLM
                # models still load on older environments. We can't
                # enforce offline-only on those builds, but the cached
                # model still loads from disk on the next attempt
                # because HF Hub's download path consults the local
                # cache before going to the network anyway.
                return SentenceTransformer(model_id, device=self.device)

        # 1. Strict offline first — works whenever the model is cached.
        try:
            return _try_load(local_only=True)
        except Exception:
            if strict_offline:
                # User explicitly asked for offline-only; propagate the
                # error rather than silently reaching out to HF.
                raise
            # Fall through to the online path: cache genuinely missing.

        # 2. Online fallback — first-install path.
        return _try_load(local_only=False)

    @property
    def fingerprint(self) -> str:
        """``model_id@<sha256-of-first-layer-weights[:16]>``.

        Detects silent model changes on rolling HuggingFace tags. Computed
        once per process, lazily.
        """
        if self._fingerprint is None:
            try:
                first = next(self._model.parameters())
                h = hashlib.sha256(
                    first.detach().cpu().numpy().tobytes()
                ).hexdigest()[:16]
            except Exception:  # noqa: BLE001 — defensive: any failure → "unknown"
                h = "unknown"
            self._fingerprint = f"{self.model_id}@{h}"
        return self._fingerprint

    def embed(self, texts: list[str]) -> "np.ndarray":
        """Returns shape ``(len(texts), dim)``, L2-normalized float32."""
        import numpy as np

        embs = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return embs.astype(np.float32, copy=False)


def make_embedder(
    model_id: str | None = None,
    *,
    device: str = "auto",
) -> SentenceTransformerEmbedder:
    """Factory honouring (in order) ``model_id`` arg → ``MEGA_EMBEDDER_MODEL``
    env → ``[embedder] model`` in config → :data:`DEFAULT_EMBEDDER_MODEL`.

    Returns a :class:`SentenceTransformerEmbedder` ready to plug into
    :class:`Router`. The cache fingerprint switches automatically with
    the model id, so swapping embedders never accidentally reuses a
    stale cache (see :meth:`Cache.sync`).
    """
    if model_id is None:
        env = os.environ.get("MEGA_EMBEDDER_MODEL", "").strip()
        if env:
            model_id = env
    if model_id is None:
        # Defer the import so callers that pass model_id explicitly
        # don't pay the cost of loading config.toml.
        from mega_tron.config import DEFAULT_EMBEDDER_MODEL, Config

        cfg = Config.load()
        model_id = cfg.embedder_model or DEFAULT_EMBEDDER_MODEL
    return SentenceTransformerEmbedder(model_id, device=device)


def fingerprint_of(embedder: object) -> str:
    """Read ``embedder.fingerprint`` if present, else fall back to
    ``model_id``. Allows custom :class:`Embedder` implementations that
    don't compute a weight hash."""
    fp = getattr(embedder, "fingerprint", None)
    if isinstance(fp, str) and fp:
        return fp
    return str(getattr(embedder, "model_id", "unknown"))


__all__ = [
    "Embedder",
    "SentenceTransformerEmbedder",
    "make_embedder",
    "fingerprint_of",
    "KNOWN_QUERY_PREFIXES",
]
