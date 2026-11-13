"""
data/numerology.py
===================
Deterministic date numerology. The reduction itself (digit-sum down to a
single digit, preserving master numbers 11/22/33) is the standard technique
numerology systems use. The BIAS TABLE below is a placeholder belief system
-- swap in your real number meanings.

Signal convention matches celestial.py: -1..+1, positive leans FAVORITE.
"""

MASTER_NUMBERS = {11, 22, 33}

# EDIT ME: your real read on what each reduced number means for betting.
NUMBER_BIAS = {
    1: 0.3,    # leadership/new start -> favorite
    2: -0.1,   # partnership/balance -> mild underdog
    3: 0.15,   # expression/luck -> favorite
    4: -0.2,   # structure disrupted -> underdog
    5: -0.15,  # change/chaos -> underdog
    6: 0.2,    # harmony -> favorite
    7: 0.05,   # introspection -> ~neutral, slight favorite
    8: -0.25,  # power reversal -> strongest underdog lean
    9: 0.25,   # completion -> favorite
    11: 0.35,  # master number -> strong favorite lean
    22: -0.35, # master number -> strong underdog lean
    33: 0.1,
}


def reduce_date(d):
    """Reduces YYYYMMDD to a single digit, keeping 11/22/33 as master numbers
    if they show up mid-reduction."""
    digits = f"{d.year:04d}{d.month:02d}{d.day:02d}"
    total = sum(int(c) for c in digits)
    while total not in MASTER_NUMBERS and total > 9:
        total = sum(int(c) for c in str(total))
    return total


def numerology_signal_for(d):
    number = reduce_date(d)
    signal = max(-1.0, min(1.0, NUMBER_BIAS.get(number, 0.0)))
    tag = " (master number)" if number in MASTER_NUMBERS else ""
    reasoning = f"Date reduces to {number}{tag} -> {'favorite' if signal >= 0 else 'underdog'} lean"
    return signal, reasoning, {"number": number}
