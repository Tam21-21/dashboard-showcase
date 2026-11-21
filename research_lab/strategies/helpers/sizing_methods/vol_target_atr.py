"""The one sizing method this registry carries today (#376 ruling 4).

Named after what it actually computes -- an exponentially smoothed true range, ``sizing.py``'s
``atr_volatility`` estimator, over a 278-bar span -- never "EWMA volatility targeting" (the
ideas brief's wrong name for it; the ruling corrects the wording, not the arithmetic). ``sizing.py``
line 147 already defaults ``SizingSpec.estimator`` to ``"atr"`` with ``estimator_window`` at 278
(``DECISIONS.md`` #304 ruling 4, Carver's 36-day exponential anchor converted to bars); this
method's own spec is ``SizingSpec()`` with every field left at its own default, so this registry
change resolves every un-migrated strategy to EXACTLY the numbers it already produces today (AC-4):
migrating the registry in does not move a single saved result.
"""
from __future__ import annotations  # lazy annotations: this module carries no vbt, no pandas

from strategies.helpers.sizing import SizingSpec  # the five-field config this method resolves to
from strategies.helpers.sizing_methods import SizingMethod  # the record shape every method module declares

# METHOD is the one attribute _discover_methods reads from this file. spec=SizingSpec() (no field
# overrides) is deliberate: sizing.py's own dataclass field defaults ARE the canonical numbers this
# method names, so there is exactly one place those five values are written down, not two.
METHOD = SizingMethod(
    id="vol_target_atr",
    description=(
        "Exponentially smoothed true range volatility targeting (278-bar span, Carver's 36-day "
        "exponential anchor, DECISIONS #304 ruling 4): size each entry so its stop distance risks "
        "a fixed fraction of equity."
    ),
    spec=SizingSpec(),
)
