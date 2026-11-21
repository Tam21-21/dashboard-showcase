"""Running one generator v2 strategy over its whole stock group (lane GV2-5a, DECISIONS #445 item 1 and #440).

frames.py loads the group's bars, signals.py turns a theory and its parameter picks into entry signals, simulate.py runs
the group as one portfolio with shared cash, one.py is the command, identity.py names a group of datasets.
Imports vbt only inside the research-runner images (simulate.py and the indicator library import it when called).
"""
