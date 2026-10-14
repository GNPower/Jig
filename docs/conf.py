"""Sphinx configuration for Jig's documentation (Read the Docs project jig-dsl).

Build from the repository root, with the package installed (``pip install -e
.[docs]``) so autodoc and the ``jig`` Pygments lexer come from this checkout::

    python -m sphinx -W --keep-going -E docs docs/_build/html
"""

from __future__ import annotations

import importlib.metadata
import os

# -- Project ------------------------------------------------------------------

project = "Jig"
author = "Graham Power"
copyright = "2026, Graham Power"
release = importlib.metadata.version("jig-dsl")
version = ".".join(release.split(".")[:2])

# -- General ------------------------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Code blocks name their language; plans use the jig lexer that jig-dsl
# registers under the pygments.lexers entry point.
highlight_language = "text"

myst_enable_extensions = ["deflist"]
myst_heading_anchors = 3

# Links into Python's documentation fetch its inventory from docs.python.org.
# JIG_DOCS_OFFLINE=1 builds without them, where there is no network (Sphinx
# warns when the fetch fails, and -W turns that into an error).
intersphinx_mapping = (
    {} if os.environ.get("JIG_DOCS_OFFLINE") else {"python": ("https://docs.python.org/3", None)}
)

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
autodoc_member_order = "bysource"
# Docstrings are reST prose without Google or NumPy sections, so no napoleon:
# it would read "The settings: ..." on a property as a type.
autodoc_typehints = "signature"

# -- HTML ---------------------------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_title = f"Jig {release}"
html_theme_options = {"navigation_depth": 3}
