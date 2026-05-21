"""Local HTTP observability + human-in-the-loop verdict editor.

Run via ``mega-tron dashboard`` (see :mod:`mega_tron.cli`); the
package itself never imports :class:`mega_tron.core.MegaCore` so the
dashboard process never pays the embedder cold-load cost. All reads
go through :class:`mega_tron.verdicts.store.Store` + frontmatter scans;
writes touch the SQLite ``verdicts`` table, ``verdict_embeddings.npz``
(best-effort), and SKILL.md ``mega_meta`` (canonical).
"""
