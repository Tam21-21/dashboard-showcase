"""The optimise and walk-forward stages of a generator v2 strategy over its whole stock group (lane GV2-7a, DECISIONS #445 items 1 and 4).

run.py is the command (`python -m research_lab.theory_stage.run --spec <spec.json> --out <dir>`); it writes the rows and files the
existing judge reads, with a group label in the ticker slot. lookback.py gives a theory's lookback at its picks (the walk-forward
embargo). Meant for research-runner:m3-latest with research_lab mounted; the stages need vbt.
"""
