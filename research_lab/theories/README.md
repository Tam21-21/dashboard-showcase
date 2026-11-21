# Writing a theory file

A theory is one TOML file. It says when to enter, when to leave, which stocks, and which numbers may be
searched. A new idea is a new file, never new code. The checker is `form.load(path)` in `form.py`; it
refuses a bad file with `TheoryFormError`, which carries a code and the part it names. `grid.py` counts
what a file can make; `expand.py` turns a template (a theory with slots) into theories. `TEMPLATE.toml` holds every field with a comment line. Two worked examples sit beside
this file: `capitulation_rebound.toml` (the test theory, DECISIONS #440 item 2) and `dolzhenko_7_2.toml`
(Dolzhenko's SubTheory from chapter 7 of her book, as printed).

A theory has no part for "why it works" and none for "who is on the other side" (DECISIONS #441 item 1).
A theory is a signal model: entry and exit signals, the stocks, the risk and money methods, the numbers to
search, and the variations. Words used below: an operand is one named thing a condition can read; a letter
is a parameter pick, a name for a number the search chooses; the ranges file is `ranges.toml`.

Order of the parts: theory, operands, entry groups, exit groups and exit rules, stocks, risk, money,
parameter picks, variations. The headings below follow that order.

## `[theory]`: name, display_name, bar, side, entry_time, status, kind, template, choices

- `name`: a non-empty text.
- `display_name`: optional, a non-empty text. The name pages show for the strategy (1 to 60 characters). A theory
  run refuses a theory without it, so give every theory one.
- `bar`: the default bar size, `"1d"` if left out.
- `side`: `"long"`, the only side today (`"long"` if left out).
- `entry_time`: when the order goes in. `"next_open"` (next bar's open), `"same_close"` (this bar's close) or
  `"after_open:<minutes>"` (a set number of minutes after the open, minutes above 0). Anything else is
  `bad_time`. Daily bars enter at the next open (DECISIONS #440, design draft "Settled in discussion" item 3).
- `status`: `"test"` for a planted test theory, `"research"` for a real one.
- `kind`, `template`, `choices`: for templates, see "Templates" below. `kind = "template"` marks a template
  file and `form.load` refuses it (`bad_template`, "expand it first"). An expanded theory has no `kind`; it
  has `template` (the template's name) and `choices` (a table of slot name to the choice taken).

## `[operand.NAME]`: kind, indicator, params, output, field, value, expr, bar, offset, span

One table per operand; NAME is the name conditions use. `kind` is one of four, and each kind takes only its
own fields (plus `bar`, `offset`, `span`); any other field is refused.

- `kind = "indicator"`: `indicator` is a name in `research_lab/indicators/limits.py` (sma, ema, rsi,
  bollinger, atr, stochastic, macd, roc, donchian, volume_ratio, constant). An unknown name is
  `unknown_indicator`. `params` is a table of the indicator's parameters; every parameter of the indicator
  must be there, each a number or a letter. `output` names which output to read; it is required when the
  indicator has several (bollinger: middle, upper, lower, pct_b, width; stochastic: k, d; macd: line,
  signal, hist) and optional when it has one. An unknown output is also `unknown_indicator`.
- `kind = "field"`: `field` is `open`, `high`, `low`, `close` or `volume`, or one of five earnings fields, each known only
  from the report's own time: `eps_surprise_pct` (percent of the estimate's size), `eps_beat_miss` (1 beat, 0 in line, -1 miss),
  `revenue_surprise_pct` (percent), `days_since_report` (trading days since the report's first session) and `report_move_pct`
  (percent move of the close on the report's first session over the close before it). `form.earnings_fields(theory)` lists the ones a theory reads.
- `kind = "constant"`: `value` is a number or a letter.
- `kind = "formula"`: `expr` is a text over operand names, numbers and `+ - * /`. Anything else in it is
  `bad_op`; an unknown name is `unknown_operand`; a formula that refers back to itself is refused too.
- `bar`: this operand's own bar size, a text or a letter. A text that is also a letter is read as the letter.
- `offset`: how many bars back to read, a whole number from 0 or a letter (default 0). In Dolzhenko's book
  this is `CandleFrom`.
- `span`: extra bars back on which the condition must also hold, a whole number from 0 or a letter
  (default 0). In her book this is `CandleInterval`; her `CandleTo` is offset plus span.

Every number in `params`, `value`, `offset`, `span` and in a condition may be a letter. A letter must have
a `[parameter_picks.<letter>]` part, or the file is refused with `missing_part`.

## `[[entry.group]]`: conditions

One or more groups. Any one group is enough to enter (OR). Inside a group every condition must hold (AND).
`conditions` is a non-empty list of `{ left, op, right }`. `op` is `<`, `<=`, `>`, `>=`, `crosses_above` or
`crosses_below`; any other is `bad_op`. `left` and `right` are each an operand name, a number or a letter;
a name that is none of these is `unknown_operand`. A name that is both an operand and a letter is refused.
An entry part with no group is `missing_part`.

## `[[exit.group]]` and `[exit]`: conditions, stop, target, trail, trail_atr, max_hold, expected_price

Exit groups work exactly like entry groups and are optional: any group is enough to leave. The rest of the
exit part is `[exit]`:

- `stop` and `target`: `"calibrated"`, `"none"`, a letter or a number, a fraction of the entry price.
  `"calibrated"` means the exit calibration chooses it on generated paths (DECISIONS #440 item 5); a
  calibrated, absent or `"none"` rule adds nothing to the count. Default `"none"`.
- `trail` and `trail_atr`: a trailing stop. `trail` is its distance in ATRs, above 0, a number or a letter; `trail_atr` is the
  window of the ATR it reads (average true range), a whole number of at least 2, a number or a letter. Give both or neither: a
  missing one is `missing_part` naming it, a value outside the limits is `outside_hard_limits`. The stop level for a bar is the
  highest close since the fill, up to the bar before, minus `trail` times that bar's ATR, and it never moves down; a bar whose low
  reaches it ends the trade at the level, or at the open when the bar opens below it. A trail counts as a way out.
- `max_hold`: the most bars to hold, a whole number from 1 or a letter.
- `expected_price`: an operand name, the price expected after the move. Required when `stop` or `target` is
  `"calibrated"`; if it is missing the file is refused with `expected_price_missing`.

The theory needs a way out: an exit group, a stop, a target, a trail or `max_hold`; otherwise `missing_part`.

## `[stocks]`: group, universe

Give exactly one. `group` is `"trending"`, `"volatile"`, `"flat"` or `"any"`. `universe` is the name of a
universe rule as a non-empty text; the checker does not resolve it (the search lane does).

## `[risk]`: method

`method` is one name from a list hardcoded in `form.py` (DECISIONS #442): today `"volatility_stop"` only.
Another name is `bad_risk`. The risk method is never searched. Together with the money method it is the one
sizing rule of DECISIONS #323.

## `[money]`: method

`method` is one name from a list hardcoded in `form.py`: today `"fixed_fraction"` and `"equal_slots"`. Another
name is `bad_money`. Never searched. `"equal_slots"` is the generator v2 test's method (DECISIONS #445 item 1): the
strategy trades its whole stock group as one portfolio with shared cash, and each entry is sized to at most the
group's value at the fill bar divided by the number of stocks (`research_lab/theory_run/simulate.py`).
`"fixed_fraction"` sizes each entry by `strategies/helpers/sizing.py` (DECISIONS #323) on the group's value just before
that buy, with the job's five sizing values; the theory file does not carry them (TH1). Under `"fixed_fraction"` the
sizing stop is also an exit, the trailing stop of `strategies/helpers/exits.py`, seen on a bar and executed at the next
open (TH1c); `"equal_slots"` has no such stop.

## `[parameter_picks.LETTER]`: range, from, to, step

One table per letter. `range` is a key of `ranges.toml`; an unknown key is `unknown_range`. A pick takes
all of that range. It may narrow it with `from`, `to` and `step`, never widen it: `from` and `to` must lie
inside the range and on its step, and `step` must be a whole multiple of the range's step; otherwise
`wider_than_range`. A range given as a list of values cannot be narrowed. A letter may be named anything,
such as `N`, or in the Dolzhenko file `open.bbw.n` (signal, operand, parameter).

Hard limits are checked on every pick. Each end of a range goes through `limits.check_params` of the
indicator it is used in (an RSI window of 1 is refused). A letter or a number compared with an indicator's
output goes through `limits.check_threshold` on that output (an RSI line of 140 is refused), for the
indicator and for every alternative a variation may put in its place. A letter compared with a field or a
constant has only its own range checked. A logic rule between two letters, such as a MACD's fast below its
slow, is not a range fault: the counts drop the pairs that break it. A fault is `outside_hard_limits`.

## `[variations.NAME]`: alternatives

NAME is an operand that may be replaced. `alternatives` is a non-empty list of other operand names that may
take its place everywhere it is read. Each combination of choices (the operand itself, or one alternative,
for every varied operand) is a combination, and `grid.combinations` counts it. (A sub-theory is now a search
round, DECISIONS #467 item 1; do not use the word for a combination.) A name that is not an operand is
`unknown_operand`.

## `[search]`: values_per_number, round_size, best, max_rounds, all_below, keep

Optional. The settings of the search for this theory (DECISIONS #470, #467 items 3 and 4), six whole numbers; when
the table is there, all six must be. A boolean or a decimal is `bad_search`.

- `values_per_number`: round 1 gives each number about this many values; a range with more values gets a coarser step
  by narrow's own rule (Dolzhenko, Table 3-2). At least 2. Later rounds narrow with the same limit.
- `round_size`: the strategies a round runs when its box is too big to run whole.
- `best`: how many of the best strategies of a round the next round narrows around. At least 1, not above `round_size`.
- `max_rounds`: the most rounds. At least 1.
- `all_below`: a box with fewer valid strategies than this is run whole. At least 1.
- `keep`: how many finalists the search keeps. At least 1.

The starter values are 20, 500, 5, 5, 5000, 3. After round 1 the book's quality condition (Figure 3-8) says whether
to go on; `rounds.json` of a search records each round and the answer.

## Templates: slot, choice, words, kind, template, choices

A template is the book's way to make theories (DECISIONS #467 item 1): one signal with slots, each slot a list
of choices, and every combination of one choice per slot is a theory. A template is a theory file with
`kind = "template"` in `[theory]` and a `display_name`, plus `[slot.NAME]` and `[choice.NAME]` tables.
`form.load_template(path)` checks it and every combination; `expand.expand(path)` and the command
`python -m research_lab.theories.expand <template> --out <folder>` write one loading theory file per
combination (a folder that is not empty is refused, exit 2). Nothing runs a theory here.

- `[slot.NAME]`: `choices` is a non-empty list of choice names, each with a `[choice.NAME]` table. A choice
  belongs to one slot, once. Slots are read in file order, choices in list order.
- `[choice.NAME]`: `words` is the plain words the display name uses; `operand` is a table of operand name to an
  operand table (same fields as `[operand.NAME]`), written `[choice.NAME.operand.X]`. A choice may not fill an
  operand the template already has, nor one that another slot fills, nor one that no condition, formula or
  `expected_price` reads; every choice of a slot fills the same operands.
- The rest is the theory form: operands, one entry part, exit, stocks, risk, money and `[parameter_picks]`.
  A template has no `[variations]`. Picks that no operand of the combination uses are dropped from its theory.
- Each theory is named `<template name>__<choice>__<choice>` and its display name is
  `<template display_name>, <words>, <words>`, at most 60 characters. A template with no slot makes one theory
  named and worded as the template.
- The theory's `[theory]` carries `template` and `choices` and has no `kind`; its text starts with a comment
  line saying it was made by expand.py and that the template is the file to edit.
- Every fault is `bad_template` and names its part, such as `choice.rsi.operand.osc_line`. A fault inside one
  combination is raised with that combination's name in the message.

Example, `templates/rebound.toml`: a slot `low_line` (Bollinger lower band or Donchian low) and a slot
`oscillator` (RSI or stochastic) make 4 theories, such as `rebound__bollinger_lower__rsi`, "Rebound after a
crash, Bollinger lower band, RSI". That theory has the entry conditions of `capitulation_rebound.toml` and its
own stop, target and hold. The other three starter templates are `average_crossover` (2 theories), `pullback`
(2) and `channel_breakout` (1).

## The ranges file: from, to, step, values, why

`ranges.toml` has one table per key. A range is `from`, `to` and `step`, or `values = [...]`, and always a
`why`: one non-empty line saying where it comes from (a paper, a page, a ruling; no book is needed,
DECISIONS #441). A range with no `why` is `missing_why`. The checker reads `ranges.toml` beside `form.py`
unless a path is given.

## Counts

`grid.combinations(theory)` gives one entry per combination of variations with the letters it uses and its number of
strategies: the product of the number of values of each letter its operands and conditions use, after the
indicators' logic rules (for a MACD with fast 5 to 30 step 5 and slow 10 to 30 step 10: 9, not 18). Only
parameter picks count. `grid.total(theory)` adds the combinations. Worked: `capitulation_rebound` is 1
combination of 7 x 4 x 6 x 3 x 5 x 4 = 10080 strategies.

## Error codes

Every refusal is a `TheoryFormError` with a `code` and a `part`. `missing_part` also covers a field the form
does not have and a value outside a list of allowed words. The fourteen codes: `missing_part`,
`unknown_indicator`, `unknown_operand`, `unknown_range`, `wider_than_range`, `outside_hard_limits`,
`missing_why`, `expected_price_missing`, `bad_time`, `bad_op`, `bad_risk`, `bad_money`, `bad_template`, `bad_search`.

## The two worked examples

- `capitulation_rebound.toml`: close below the lower Bollinger band (N, W), volume at least k times its
  average (volume_ratio N), RSI (R) below L; leaves by the calibrated stop and target, or after H bars.
  Expected price: the close 3 bars back. Group volatile. No variations. Its numbers are "planted test
  values".
- `dolzhenko_7_2.toml`: her open signal (Volume < Const1, Bbw < Const2, Lp < BbwL), her close signal
  (Hp > BbwU) and a stop with one coefficient. Her 27 rows of Table 7-2 are 27 picks named signal.operand.param.
  Comments in the file flag the two oddities of her print.
