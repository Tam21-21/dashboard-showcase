# syntax=docker/dockerfile:1.7
# research_lab/docker/nautilus-runner/Dockerfile
#
# nautilus-runner:1.231.0 -- the Nautilus retest's OWN image (lane NR, U.7s). This is a FRESH
# image built directly from python:3.12.13-slim, never chained on research-runner:m2-latest or
# m3-latest: AD7.3 of docs/briefs/nautilus_architecture_2026-09-03.md is explicit that "the
# architecture therefore does NOT require co-residency; separate environments are the default",
# and NautilusTrader has no need of the roughly 5-7 GB vbt-pro stack the research-runner images
# carry. AC-I1/AC-20 also require that nothing in this image or its baked entrypoint ever imports
# or names the pip package research-runner installs for vectorised backtesting -- a fresh base is
# what makes that trivially true rather than something a later change could accidentally undo.
#
# Pins, and why (stated here rather than only in the sprint report, so the next reader does not
# have to go hunting): nautilus_trader==1.231.0 is the version the spec brief, the architecture
# doc and the 2026-08-24 probe (docs/probes/nautilus_probe_2026-08-24.md) all measured against.
# pandas==3.0.5 and pyarrow==25.0.1 are the EXACT pair the real pip resolver chose for
# nautilus_trader==1.231.0 in the probe-writer's own throwaway resolve (docs/units/NR/report_1.md,
# "The wheel-resolution risk"). This build tried the closer-to-research-runner pins first
# (pandas==2.3.3, pyarrow==24.0.0, matching research_lab/docker/research-runner/requirements.txt)
# and the real resolver refused them: nautilus_trader 1.231.0 declares `pyarrow>=25.0.0`, so
# 24.0.0 can never be satisfied and research-runner's own pin cannot be matched here. This is a
# LEAD DECISION (lane NR resume, findings list item 1), not a re-litigation of the tradeoff: this
# image and research-runner never share a process, so the only contract between them is the
# on-disk parquet FILE FORMAT the retest reads (trades.parquet, price.parquet), never the arrow or
# pandas library version that wrote it, and parquet is a stable, versioned, self-describing format
# for exactly that reason. Both pins are verified against the REAL resolver at build time below,
# not assumed: a resolver conflict fails this build loudly, with pip's own output, rather than
# silently drifting to whatever pip would have picked on its own.
#
# Build (context is THIS directory, not research_lab/, since nothing here is baked from the
# strategies package -- the retest's replay strategy is baked directly into the entrypoint below,
# never mounted, because the entrypoint's OWN control flow, not a user's strategy file, is what
# AC-22/AC-30 read):
#   cd research_lab/docker/nautilus-runner
#   docker build --build-arg GIT_SHA=$(git rev-parse HEAD) -t nautilus-runner:1.231.0 .
FROM python:3.12.13-slim

# Bake the build-provenance string the retest record's own engine_version field is corroborated
# against (AC-C1), in the git:<sha>+nautilus:<version> shape the plan's own words name. GIT_SHA is
# passed at build time (.git is not in the build context); the nautilus_trader version is the pin
# just below, named a second time here rather than derived, because a build-arg cannot read a pip
# constraints file and this string is provenance metadata, not a runtime import.
ARG GIT_SHA=unknown
RUN echo "git:${GIT_SHA}+nautilus:1.231.0" > /etc/build_provenance

# No build tools needed: nautilus_trader 1.231.0 publishes a manylinux wheel for cp312 (confirmed
# by the probe-writer's own throwaway resolve, docs/units/NR/report_1.md's "wheel-resolution risk"
# section), so this is a pure pip install, no compiler stage, no dev headers.
RUN pip install --no-cache-dir pandas==3.0.5 pyarrow==25.0.1 nautilus_trader==1.231.0

# The entrypoint. Baked, never mounted: the retest's own logic (the shadow copy, the replay
# strategy, the fill export) is fixed by this build, not user-authored per-run code the way a
# backtest's strategy.py is, so there is nothing to mount at run time.
#
# No ENTRYPOINT or CMD is set: every caller (the worker's job handler, and every probe here)
# names its own command explicitly (`python3 /app/run_retest_entrypoint.py`, or `cat
# /etc/build_provenance` for AC-20's own provenance check), so a fixed ENTRYPOINT would only get
# in the way by prepending itself to whatever command a caller passes.
COPY run_retest_entrypoint.py /app/run_retest_entrypoint.py

WORKDIR /app
