"""Composition modules: pure, parametrised wiring functions.

Unlike ``ump.asgi`` — which is the process entry point and therefore carries
import-time side effects (starting file watchers, opening DB connections,
building the ASGI app) — modules under ``ump.composition`` contain only plain
functions that take their dependencies as arguments and return fully wired
objects.  They can be imported and unit-tested with zero infrastructure.
"""
