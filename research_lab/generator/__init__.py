"""Generator unit 1 (lane-list row 3.2, DECISIONS #308/#329): turns an operator-authored
theory file into concrete, sweepable strategy modules by filling a skeleton from the
theory's own two lists (admissible skeletons and admissible fillers). Runs in two
processes joined by one facts file: compile_facts.py inside research-runner:m3-latest
(the only file here that imports vectorbtpro), then cli.py on the host (theory loading,
the two gates, the emitter, the loop, and the strategy write).

Intentionally empty otherwise: nothing here imports vbt or the dashboard package, so
importing this package costs nothing on either side of the container boundary.
"""
