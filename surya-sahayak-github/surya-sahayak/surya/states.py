"""Indian states / UTs: code <-> name, and discom -> state hints. No network, no AWS."""
from __future__ import annotations

import re

STATES = {
    "AN": "Andaman and Nicobar Islands", "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam",
    "BR": "Bihar", "CH": "Chandigarh", "CG": "Chhattisgarh", "DH": "Dadra and Nagar Haveli and Daman and Diu",
    "DL": "Delhi", "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana", "HP": "Himachal Pradesh",
    "JK": "Jammu and Kashmir", "JH": "Jharkhand", "KA": "Karnataka", "KL": "Kerala", "LA": "Ladakh",
    "LD": "Lakshadweep", "MP": "Madhya Pradesh", "MH": "Maharashtra", "MN": "Manipur", "ML": "Meghalaya",
    "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha", "PY": "Puducherry", "PB": "Punjab",
    "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu", "TG": "Telangana", "TR": "Tripura",
    "UP": "Uttar Pradesh", "UK": "Uttarakhand", "WB": "West Bengal",
}

# Old / alternative ISO and spelling variants -> current code.
_CODE_ALIASES = {"TS": "TG", "OR": "OD", "UT": "UK", "UA": "UK", "DD": "DH", "DN": "DH"}
_NAME_ALIASES = {
    "orissa": "OD", "nct of delhi": "DL", "national capital territory of delhi": "DL", "new delhi": "DL",
    "pondicherry": "PY", "uttaranchal": "UK", "telengana": "TG", "chattisgarh": "CG",
    "andaman and nicobar": "AN", "dadra and nagar haveli": "DH", "daman and diu": "DH",
    "tamilnadu": "TN", "jammu and kashmir ut": "JK",
}


def _norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s)).strip()


_BY_NAME = {_norm(n): c for c, n in STATES.items()}


def state_code(value) -> str | None:
    """'DL' / 'IN-DL' / 'Delhi' / 'NCT of Delhi' / 'Orissa' -> 'DL'.  Unknown -> None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    up = v.upper()
    if up.startswith("IN-"):
        up = up[3:]
    if len(up) == 2 and up.isalpha():
        up = _CODE_ALIASES.get(up, up)
        return up if up in STATES else None
    n = _norm(v)
    return _BY_NAME.get(n) or _NAME_ALIASES.get(n)


def state_name(code: str | None) -> str | None:
    return STATES.get(code) if code else None


# Rough state centroids (state capital) — last-resort location when nothing else works.
CAPITALS = {
    "DL": (28.6139, 77.2090), "UP": (26.8467, 80.9462), "MH": (19.0760, 72.8777), "KA": (12.9716, 77.5946),
    "TN": (13.0827, 80.2707), "HR": (30.7333, 76.7794), "PB": (30.7333, 76.7794), "RJ": (26.9124, 75.7873),
    "GJ": (23.2156, 72.6369), "WB": (22.5726, 88.3639), "MP": (23.2599, 77.4126), "TG": (17.3850, 78.4867),
    "AP": (16.5062, 80.6480), "KL": (8.5241, 76.9366), "BR": (25.5941, 85.1376), "OD": (20.2961, 85.8245),
    "CG": (21.2514, 81.6296), "JH": (23.3441, 85.3096), "UK": (30.3165, 78.0322), "HP": (31.1048, 77.1734),
    "AS": (26.1445, 91.7362), "GA": (15.4909, 73.8278), "CH": (30.7333, 76.7794), "JK": (34.0837, 74.7973),
}

# discom name fragments -> state. Order matters: first match wins (Delhi variants first).
DISCOM_STATE = [
    ("tata power-ddl", "DL"), ("tata power ddl", "DL"), ("tata power delhi", "DL"), ("tpddl", "DL"),
    ("bses", "DL"), ("ndmc", "DL"), ("delhi", "DL"),
    ("msedcl", "MH"), ("mahavitaran", "MH"), ("mseb", "MH"), ("best undertaking", "MH"),
    ("adani electricity", "MH"), ("tata power", "MH"), ("maharashtra", "MH"),
    ("uppcl", "UP"), ("pvvnl", "UP"), ("mvvnl", "UP"), ("dvvnl", "UP"), ("puvvnl", "UP"),
    ("kesco", "UP"), ("noida power", "UP"), ("npcl", "UP"), ("uttar pradesh", "UP"),
    ("dhbvn", "HR"), ("uhbvn", "HR"), ("haryana", "HR"),
    ("pspcl", "PB"), ("punjab state", "PB"),
    ("jvvnl", "RJ"), ("avvnl", "RJ"), ("jdvvnl", "RJ"), ("vidyut vitran nigam", "RJ"), ("rajasthan", "RJ"),
    ("dgvcl", "GJ"), ("mgvcl", "GJ"), ("pgvcl", "GJ"), ("ugvcl", "GJ"), ("torrent power", "GJ"), ("gujarat", "GJ"),
    ("bescom", "KA"), ("mescom", "KA"), ("hescom", "KA"), ("gescom", "KA"), ("karnataka", "KA"),
    ("tangedco", "TN"), ("tneb", "TN"), ("tnpdcl", "TN"), ("tamil nadu", "TN"),
    ("apspdcl", "AP"), ("apepdcl", "AP"), ("apcpdcl", "AP"), ("andhra", "AP"),
    ("tgspdcl", "TG"), ("tsspdcl", "TG"), ("tsnpdcl", "TG"), ("tgnpdcl", "TG"), ("telangana", "TG"),
    ("kseb", "KL"), ("kerala", "KL"),
    ("wbsedcl", "WB"), ("cesc limited", "WB"), ("cesc ltd", "WB"), ("west bengal", "WB"),
    ("mppkvvcl", "MP"), ("mpmkvvcl", "MP"), ("mppokvvcl", "MP"), ("madhya pradesh", "MP"),
    ("cspdcl", "CG"), ("chhattisgarh", "CG"),
    ("nbpdcl", "BR"), ("sbpdcl", "BR"), ("bsphcl", "BR"), ("bihar", "BR"),
    ("jbvnl", "JH"), ("jharkhand", "JH"),
    ("tpcodl", "OD"), ("tpnodl", "OD"), ("tpsodl", "OD"), ("tpwodl", "OD"), ("optcl", "OD"), ("odisha", "OD"),
    ("upcl", "UK"), ("uttarakhand", "UK"), ("hpsebl", "HP"), ("himachal", "HP"),
    ("jpdcl", "JK"), ("kpdcl", "JK"), ("apdcl", "AS"), ("assam", "AS"), ("goa electricity", "GA"),
]


def state_from_discom(discom: str | None) -> str | None:
    if not discom:
        return None
    d = discom.lower()
    return next((s for k, s in DISCOM_STATE if k in d), None)
