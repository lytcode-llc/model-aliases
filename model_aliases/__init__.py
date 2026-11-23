"""Package root for the model-aliases plugin modules.

Kept side-effect-free: Hermes loads the *repository root* ``__init__.py`` via
spec_from_file_location (which triggers registration); tests import this
package directly without registering anything.
"""
