"""Parsing and validation of identifiers read by OCR.

Identifiers handled:
  * USDOT numbers      - "USDOT 1234567" painted on the tractor door
  * Trailer / container numbers - ISO 6346 format, e.g. "NXTU 482913 4"
    (4-letter owner code, 6-digit serial, check digit)
  * License plates     - 5-8 alphanumerics
  * Seal numbers       - bolt-seal numbers on the rear doors

OCR engines confuse look-alike glyphs (O/0, I/1, S/5, B/8 ...). Each parser
knows which positions must be letters or digits and repairs them; ISO 6346
check digits are then used to reject (or repair) bad reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product

# --- glyph confusion tables -------------------------------------------------
TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "J": "1",
            "T": "7", "Z": "2", "S": "5", "B": "8", "G": "6", "A": "4"}
TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A", "7": "T"}


def clean(text: str) -> str:
    """Upper-case and strip everything except A-Z / 0-9."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def as_digits(s: str) -> str:
    return "".join(TO_DIGIT.get(c, c) for c in s)


def as_letters(s: str) -> str:
    return "".join(TO_LETTER.get(c, c) for c in s)


# --- ISO 6346 ---------------------------------------------------------------
_ISO_LETTER_VALUES: dict[str, int] = {}
_v = 10
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    if _v % 11 == 0:
        _v += 1
    _ISO_LETTER_VALUES[_ch] = _v
    _v += 1


def iso6346_check_digit(code10: str) -> int:
    """Check digit for the first 10 characters (owner code + category + serial)."""
    if len(code10) != 10:
        raise ValueError("ISO 6346 code body must be 10 characters")
    total = 0
    for i, ch in enumerate(code10):
        val = int(ch) if ch.isdigit() else _ISO_LETTER_VALUES[ch]
        total += val * (2 ** i)
    return total % 11 % 10


def is_valid_iso6346(code: str) -> bool:
    code = clean(code)
    if not re.fullmatch(r"[A-Z]{3}[UJZR][0-9]{7}", code):
        return False
    return iso6346_check_digit(code[:10]) == int(code[10])


def format_iso6346(code: str) -> str:
    code = clean(code)
    return f"{code[:4]} {code[4:10]} {code[10]}"


# --- result type ------------------------------------------------------------
@dataclass
class Reading:
    value: str
    confidence: float
    raw: str = ""
    corrected: bool = False
    box: list | None = None          # polygon in source image coords
    frame: int = 0                    # index of the camera frame it came from
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"value": self.value, "confidence": round(self.confidence, 3), "raw": self.raw,
                "corrected": self.corrected, "frame": self.frame, "notes": self.notes}


# --- parsers ----------------------------------------------------------------
DOT_RE = re.compile(r"(?:US\s*[-.]?\s*)?D\s*[.]?\s*O\s*[.]?\s*T\s*[#:.\-]?\s*(?:NO\.?\s*)?([0-9OQDILSBZG ]{5,10})")


def parse_dot(text: str) -> tuple[str, bool] | None:
    """Return (dot_number, corrected) from a text line containing a DOT marking."""
    t = text.upper()
    m = DOT_RE.search(t)
    if not m:
        return None
    digits_raw = m.group(1).replace(" ", "")
    digits = as_digits(digits_raw)
    if not re.fullmatch(r"[0-9]{5,8}", digits):
        return None
    return digits, digits != digits_raw


def parse_trailer(text: str) -> tuple[str, bool] | None:
    """Find an ISO 6346 code in `text`, repairing look-alike glyphs if needed.

    Returns (formatted_code, corrected) or None. A code is only accepted when
    its check digit validates, which makes false positives very unlikely.
    """
    t = clean(text)
    for start in range(0, max(1, len(t) - 10)):
        chunk = t[start:start + 11]
        if len(chunk) < 11:
            break
        owner = as_letters(chunk[:4])
        serial = as_digits(chunk[4:11])
        candidate = owner + serial
        if is_valid_iso6346(candidate):
            return format_iso6346(candidate), candidate != chunk
        if re.fullmatch(r"[A-Z]{4}[0-9]{7}", candidate):
            repaired = _repair_iso(candidate)
            if repaired:
                return format_iso6346(repaired), True
    # The check digit is sometimes boxed and missed by OCR: accept 10 chars
    # and recompute the check digit, but mark it as corrected.
    m = re.search(r"([A-Z0-9]{4})([0-9OQDILSBZG]{6})$", t)
    if m:
        owner = as_letters(m.group(1))
        serial = as_digits(m.group(2))
        if re.fullmatch(r"[A-Z]{3}[UJZR]", owner) and serial.isdigit():
            cd = iso6346_check_digit(owner + serial)
            return format_iso6346(owner + serial + str(cd)), True
    return None


_CONFUSABLE = [set("UJVY"), set("ODQC"), set("ILT"), set("MNH"), set("EF"), set("PR"),
               set("0689"), set("17"), set("35"), set("38"), set("25"), set("49")]


def _repair_iso(code: str) -> str | None:
    """Try single-glyph substitutions that make the check digit validate.

    Positions are tried in order of how often OCR gets them wrong: the
    category letter (almost always 'U'), then the owner code, then the serial.
    Within a tier only a *unique* repair is accepted.
    """
    for tier in ((3,), (0, 1, 2), tuple(range(4, 11))):
        hits = set()
        for i in tier:
            ch = code[i]
            for group in _CONFUSABLE:
                if ch in group:
                    for alt in group - {ch}:
                        cand = code[:i] + alt + code[i + 1:]
                        if is_valid_iso6346(cand):
                            hits.add(cand)
        if len(hits) == 1:
            return hits.pop()
        if len(hits) > 1:
            return None
    return None


SEAL_RE = re.compile(r"SEA[L1I]\s*(?:NO\.?|#|:)?\s*([A-Z0-9]{6,10})")


def parse_seal(text: str) -> tuple[str, bool] | None:
    t = text.upper().replace(" ", "")
    m = SEAL_RE.search(t)
    if not m:
        return None
    raw = m.group(1)
    # Seal numbers in this yard are numeric with an optional 2-letter prefix.
    mm = re.fullmatch(r"([A-Z0-9]{2})?([0-9A-Z]{6,8})", raw)
    if not mm:
        return None
    prefix = as_letters(mm.group(1)) if mm.group(1) and not mm.group(1).isdigit() else (mm.group(1) or "")
    digits = as_digits(mm.group(2))
    if not digits.isdigit():
        return None
    value = prefix + digits
    return value, value != raw


PLATE_RE = re.compile(r"^[A-Z0-9]{5,8}$")
_PLATE_STOPWORDS = {"USDOT", "TRAILER", "SEAL", "FREIGHT", "LOGISTICS", "TRANSPORT",
                    "EXPRESS", "CARRIERS", "LINES", "TRUCKING"}


def looks_like_plate(text: str) -> str | None:
    t = clean(text)
    if not PLATE_RE.match(t) or t in _PLATE_STOPWORDS:
        return None
    digits = sum(c.isdigit() for c in t)
    letters = len(t) - digits
    if digits < 2 or letters < 1:
        return None
    return t


def plate_variants(plate: str, max_variants: int = 64) -> list[str]:
    """Look-alike variants of a plate, used for fuzzy DB matching."""
    options = []
    for c in plate:
        alts = {c}
        if c in TO_DIGIT:
            alts.add(TO_DIGIT[c])
        if c in TO_LETTER:
            alts.add(TO_LETTER[c])
        options.append(sorted(alts))
    out = []
    for combo in product(*options):
        out.append("".join(combo))
        if len(out) >= max_variants:
            break
    return out


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
