"""Top-level package for all Research Lab strategies.

The worker runs with ``~/trading/research_lab/`` as its working directory, so
``strategies`` resolves as a top-level import package (M3 spec L15, DECISIONS
#11). The strategy contract lives in ``strategies.base``; concrete strategies
(e.g. ``strategies.ma_cross``) sit alongside it.
"""
# No exports here: importing the contract surface goes through strategies.base,
# and concrete strategies are imported by their own module path. Keeping this
# file bare avoids importing vbt/pandas just to touch the package.
