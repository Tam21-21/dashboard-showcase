"""The generator v2 search over a theory's strategies on its stock group (lane GV2-5b, DECISIONS #440 and #445).

sample.py draws the balanced sample of step 1, narrow.py narrows the intervals around the best strategies for step 2
(Dolzhenko), onc.py clusters the trials and gives the variance of the clustered trials (Lopez de Prado and Lewis 2018),
family.py turns a trial set into the numbers the judge will use (K and V), calibrate.py fits the stop and target of the
finalists (GV2-6, AFML chapter 13), search.py runs the steps, run.py is the command, measure.py measures the budget.
Imports vbt only inside the research-runner images (through research_lab.theory_run).
"""
