"""Hermes model-provider plugin: floating model aliases.

Imported by Hermes' provider loader (``spec_from_file_location`` with this
directory as the package root); registration must happen at import.
"""

from .model_aliases.plugin import register_all

register_all()
