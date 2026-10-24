"""Built-in adapters.

Concrete modules land per the milestone plan:

- M1: :mod:`gseek.adapters.blast`, :mod:`gseek.adapters.minimap2`
- M2: :mod:`gseek.adapters.kma`, :mod:`gseek.adapters.mummer`
- M3: :mod:`gseek.adapters.bwa`, :mod:`gseek.adapters.mash`,
  :mod:`gseek.adapters.sourmash`
- M4: protein BLAST modes, sourmash gather, :mod:`gseek.adapters.skani`

Each module exposes a class named ``ADAPTER``; add its dotted module path to
:data:`gseek.registry.BUILTIN_MODULES` when it is ready.
"""

from __future__ import annotations

__all__: list[str] = []
