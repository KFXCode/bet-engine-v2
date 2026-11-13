"""
data/celestial.py
==================
Pure-math moon phase + MOON SIGN for a given date. No API needed -- both are
computed directly from real Sun/Moon position formulas, so there's nothing to
scrape and nothing that can go stale.

IMPORTANT: "zodiac" here means the sign the MOON is currently transiting
(what mooncalendar.astro-seek.com and every daily-moon-sign calendar show),
NOT your sun/birth sign. The Moon changes sign every ~2.25 days as it moves
around the whole zodiac in ~27.3 days -- it is NOT the same as the calendar
sun-sign date ranges (Jun 21-Jul 22 = Cancer, etc.) that a birthday horoscope
uses.

Both the phase and the sign are computed from the Meeus "Astronomical
Algorithms" low-precision Sun/Moon theories (truncated series) -- standard,
public formulas accurate to a fraction of a degree. The PHASE comes from the
true Sun-Moon elongation, NOT a linear days-since-a-fixed-new-moon estimate
(that drifts ~1 day and flips the label right at a boundary).

The BIAS TABLES below (MOON_PHASE_BIAS, ZODIAC_ELEMENT_BIAS) turn "Waxing
Gibbous, Moon in Scorpio" into a number the scoring engine can use. Edit them
freely; they're kept small (see FACTOR_WEIGHTS["moon_zodiac"]) so they nudge,
not drive, the model.

Signal convention: -1.0 .. +1.0, positive = leans toward the market FAVORITE,
negative = leans toward the market UNDERDOG.
"""

import math
from datetime import datetime, timezone

SYNODIC_MONTH = 29.53058867
REFERENCE_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

# Phase name by Sun-Moon elongation (0deg = New, 90 = First Quarter,
# 180 = Full, 270 = Last Quarter). The four PRINCIPAL phases only get a
# narrow +/-12deg (~1 day) window; everything else is a crescent/gibbous.
MOON_PHASE_ANGLE_BANDS = [
    (6.0,   "New Moon"),
    (84.0,  "Waxing Crescent"),
    (96.0,  "First Quarter"),
    (174.0, "Waxing Gibbous"),
    (186.0, "Full Moon"),
    (264.0, "Waning Gibbous"),
    (276.0, "Last Quarter"),
    (354.0, "Waning Crescent"),
    (360.0, "New Moon"),

]

# EDIT ME: your real read on how each phase should lean.
MOON_PHASE_BIAS = {
    "New Moon": 0.25,
    "Waxing Crescent": 0.1,
    "First Quarter": 0.0,
    "Waxing Gibbous": -0.1,
    "Full Moon": -0.3,
    "Waning Gibbous": -0.1,
    "Last Quarter": 0.0,
    "Waning Crescent": 0.15,
}

ZODIAC_SIGN_ORDER = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]

ZODIAC_ELEMENT = {
    "Aries": "fire", "Leo": "fire", "Sagittarius": "fire",
    "Taurus": "earth", "Virgo": "earth", "Capricorn": "earth",
    "Gemini": "air", "Libra": "air", "Aquarius": "air",
    "Cancer": "water", "Scorpio": "water", "Pisces": "water",
}

# EDIT ME: your real read on how each element should lean.
ZODIAC_ELEMENT_BIAS = {
    "fire": 0.2,
    "earth": 0.15,
    "air": -0.05,
    "water": -0.2,
}


def _julian_day(y, m, d, hour=12.0):
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + hour / 24.0 + b - 1524.5


def _norm_deg(deg):
    return deg % 360.0


def moon_ecliptic_longitude(d):
    """Geocentric ecliptic longitude of the Moon (0-360 deg) at 12:00 UTC via
    Meeus' truncated ELP2000 series -- accurate to a fraction of a degree."""
    jd = _julian_day(d.year, d.month, d.day, 12.0)
    t = (jd - 2451545.0) / 36525.0

    Lp = _norm_deg(218.3164477 + 481267.88123421 * t - 0.0015786 * t**2 + t**3 / 538841 - t**4 / 65194000)
    D = _norm_deg(297.8501921 + 445267.1114034 * t - 0.0018819 * t**2 + t**3 / 545868 - t**4 / 113065000)
    M = _norm_deg(357.5291092 + 35999.0502909 * t - 0.0001536 * t**2 + t**3 / 24490000)
    Mp = _norm_deg(134.9633964 + 477198.8675055 * t + 0.0087414 * t**2 + t**3 / 69699 - t**4 / 14712000)
    F = _norm_deg(93.2720950 + 483202.0175233 * t - 0.0036539 * t**2 - t**3 / 3526000 + t**4 / 863310000)

    d_, m_, mp_, f_ = math.radians(D), math.radians(M), math.radians(Mp), math.radians(F)

    delta_l = (
        6.288774 * math.sin(mp_)
        + 1.274027 * math.sin(2 * d_ - mp_)
        + 0.658314 * math.sin(2 * d_)
        + 0.213618 * math.sin(2 * mp_)
        - 0.185116 * math.sin(m_)
        - 0.114332 * math.sin(2 * f_)
        + 0.058793 * math.sin(2 * d_ - 2 * mp_)
        + 0.057066 * math.sin(2 * d_ - m_ - mp_)
        + 0.053322 * math.sin(2 * d_ + mp_)
        + 0.045758 * math.sin(2 * d_ - m_)
        - 0.040923 * math.sin(m_ - mp_)
        - 0.034720 * math.sin(d_)
        - 0.030383 * math.sin(m_ + mp_)
        + 0.015327 * math.sin(2 * d_ - 2 * f_)
        - 0.012528 * math.sin(mp_ + 2 * f_)
        + 0.010980 * math.sin(mp_ - 2 * f_)
        + 0.010675 * math.sin(4 * d_ - mp_)
        + 0.010034 * math.sin(3 * mp_)
        + 0.008548 * math.sin(4 * d_ - 2 * mp_)
        - 0.007888 * math.sin(2 * d_ + m_ - mp_)
    )

    return _norm_deg(Lp + delta_l)


def _sun_ecliptic_longitude(d):
    """Geocentric ecliptic longitude of the Sun (0-360 deg) at 12:00 UTC via
    Meeus' low-precision solar theory."""
    jd = _julian_day(d.year, d.month, d.day, 12.0)
    t = (jd - 2451545.0) / 36525.0
    L0 = _norm_deg(280.46646 + 36000.76983 * t + 0.0003032 * t**2)
    M = math.radians(_norm_deg(357.52911 + 35999.05029 * t - 0.0001537 * t**2))
    C = ((1.914602 - 0.004817 * t - 0.000014 * t**2) * math.sin(M)
         + (0.019993 - 0.000101 * t) * math.sin(2 * M)
         + 0.000289 * math.sin(3 * M))
    return _norm_deg(L0 + C)


def moon_phase_for(d):
    """Returns (phase_name, illumination_fraction 0..1) from the true
    Sun-Moon elongation. Elongation 0=new, 90=first quarter, 180=full."""
    elongation = _norm_deg(moon_ecliptic_longitude(d) - _sun_ecliptic_longitude(d))
    illumination = (1 - math.cos(math.radians(elongation))) / 2
    for upper, name in MOON_PHASE_ANGLE_BANDS:
        if elongation < upper:
            return name, round(illumination, 3)
    return "New Moon", round(illumination, 3)


def moon_sign_for(d):
    """The zodiac sign the MOON is currently transiting on date `d`."""
    longitude = moon_ecliptic_longitude(d)
    return ZODIAC_SIGN_ORDER[int(longitude // 30) % 12]


def celestial_signal_for(d):
    """Single -1..+1 signal blending moon phase (60%) + Moon-sign element (40%)."""
    phase_name, illum = moon_phase_for(d)
    sign = moon_sign_for(d)
    element = ZODIAC_ELEMENT[sign]
    moon_bias = MOON_PHASE_BIAS.get(phase_name, 0.0)
    element_bias = ZODIAC_ELEMENT_BIAS.get(element, 0.0)
    signal = max(-1.0, min(1.0, 0.6 * moon_bias + 0.4 * element_bias))
    reasoning = (f"{phase_name} ({illum:.0%} illuminated), Moon in {sign} ({element} sign) -> "
                 f"{'favorite' if signal > 0 else 'underdog'} lean")
    return signal, reasoning, {"phase": phase_name, "illumination": illum, "sign": sign, "element": element}
