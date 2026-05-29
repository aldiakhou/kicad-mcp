"""Schematic engine: netlist-first, atomic, verifiable schematic generation.

This package implements the pipeline:
  intent → canonical circuit/netlist → schematic writer → KiCad CLI verification → commit or rollback

Optional dependencies (install with `pip install kicad-mcp[schematic-engine]`):
  - skidl: Circuit connectivity compiler & netlist generation
  - kiutils: KiCad file structured parser/serializer
  - kicad-skip: KiCad S-expression schematic manipulation
"""

from kicad_mcp.schematic_engine.result import EngineResult

__all__ = ["EngineResult"]
