"""Competitive-intent Radar: the rules, kept free of I/O like scoring.py.

A *signal* is one piece of public evidence that a company runs, or is
struggling with, a product Solix displaces: a job posting that names a
competitor product, a posting about archiving or retirement work, a DNS or
certificate record that fingerprints a competitor, a filing, a tender, a news
story. Signals roll up into a *company* (one per organisation), which gets:

* strength  - Strong / Medium / Weak / Unrated, from the best role seen in its
              job postings (people who operate the tool are the strongest tell);
* score     - every signal's points, decayed with the same half-life as lead
              intent, times a learned weight per signal type;
* surge     - three or more new signals inside seven days;
* route     - named account first, then US state (California split by ZIP),
              then country, exactly as the original Leads Radar routed.

Everything a person sets (owner, status, notes) lives on the company and is
never touched by these functions.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import scoring

# --- Competitors ----------------------------------------------------------------
# key: stable id. terms: phrases matched (case-insensitive, whole word) in
# postings, filings and news. slugs: the Solix products that displace it, so a
# Radar company gets a primary product line like an inbound lead does.
COMPETITORS: List[dict] = [
    {"key": "opentext-documentum", "vendor": "OpenText", "product": "OpenText Documentum", "terms": ["Documentum", "OpenText Content Server", "Content Server"], "slugs": ["enterprise-content-services"]},
    {"key": "opentext-infoarchive", "vendor": "OpenText", "product": "OpenText InfoArchive", "terms": ["InfoArchive", "Info Archive", "OpenText Archive Center", "Archive Center", "Structured Data Manager"], "slugs": ["enterprise-archiving", "application-retirement"]},
    {"key": "informatica-idmc", "vendor": "Informatica", "product": "Informatica IDMC", "terms": ["IDMC", "Informatica Intelligent Data Management Cloud", "Intelligent Data Management Cloud", "IICS", "Informatica Cloud"], "slugs": ["common-data-platform"]},
    {"key": "informatica-ilm", "vendor": "Informatica", "product": "Informatica Data Archive", "terms": ["Informatica Data Archive", "ILM Nearline", "Informatica ILM", "Informatica TDM"], "slugs": ["database-archiving", "application-retirement"]},
    {"key": "ibm-optim", "vendor": "IBM", "product": "IBM Optim", "terms": ["IBM Optim", "InfoSphere Optim", "Optim Archive"], "slugs": ["database-archiving"]},
    {"key": "delphix", "vendor": "Delphix (Perforce)", "product": "Delphix", "terms": ["Delphix"], "slugs": ["enterprise-data-governance"]},
    {"key": "archive360", "vendor": "Archive360", "product": "Archive360", "terms": ["Archive360", "Archive 360"], "slugs": ["enterprise-archiving"]},
    {"key": "smarsh", "vendor": "Smarsh", "product": "Smarsh", "terms": ["Smarsh"], "slugs": ["email-archiving", "active-archiving-compliance"]},
    {"key": "global-relay", "vendor": "Global Relay", "product": "Global Relay", "terms": ["Global Relay"], "slugs": ["email-archiving", "active-archiving-compliance"]},
    {"key": "mimecast", "vendor": "Mimecast", "product": "Mimecast", "terms": ["Mimecast"], "slugs": ["email-archiving"]},
    {"key": "proofpoint", "vendor": "Proofpoint", "product": "Proofpoint", "terms": ["Proofpoint"], "slugs": ["email-archiving", "active-archiving-compliance"]},
    {"key": "veritas-ev", "vendor": "Veritas", "product": "Veritas Enterprise Vault", "terms": ["Enterprise Vault", "Veritas Enterprise Vault"], "slugs": ["email-archiving"]},
    {"key": "platform3-archon", "vendor": "Platform 3", "product": "Platform 3 Archon", "terms": ["Platform 3 Archon", "Archon"], "slugs": ["application-retirement"]},
    {"key": "jivs", "vendor": "Data Migration Services", "product": "JiVS", "terms": ["JiVS"], "slugs": ["sap-archiving", "application-retirement"]},
]
COMPETITOR_BY_KEY = {c["key"]: c for c in COMPETITORS}

# DNS / certificate fingerprints: a substring found in an MX, SPF/TXT, CNAME or
# certificate name that shows the competitor is in use.
DNS_FINGERPRINTS: List[dict] = [
    {"key": "mimecast", "patterns": ["mimecast.com", "mimecast-offshore.com"]},
    {"key": "proofpoint", "patterns": ["pphosted.com", "ppe-hosted.com", "proofpoint.com"]},
    {"key": "smarsh", "patterns": ["smarsh.com", "smarsh"]},
    {"key": "global-relay", "patterns": ["globalrelay.net", "globalrelay.com"]},
    {"key": "archive360", "patterns": ["archive360"]},
    {"key": "opentext-documentum", "patterns": ["documentum", "d2.", "otcs."]},
    {"key": "opentext-infoarchive", "patterns": ["infoarchive"]},
    {"key": "veritas-ev", "patterns": ["enterprisevault", "evault"]},
    {"key": "ibm-optim", "patterns": ["optim."]},
    {"key": "delphix", "patterns": ["delphix"]},
]

PROBLEM_PATTERNS = [r"archiv\w*", r"retention", r"decommission\w*", r"retir(?:e|ement|ing)\b", r"legacy", r"lifecycle", r"records management",
                    r"test data", r"data governance", r"migrat\w*", r"sunset\w*", r"rationali[sz]\w*", r"consolidat\w*"]
# A posting is a problem signal only on these stronger phrases; "migration" alone is too common.
PROBLEM_TRIGGERS = [r"application (?:retirement|decommission\w*)", r"legacy (?:application|system|platform)s? (?:retirement|decommission\w*|sunset\w*)",
                    r"data archiv\w+", r"archiv\w+ (?:platform|solution|strategy)", r"records retention", r"decommission\w* (?:legacy|application|system)s?",
                    r"retention (?:policy|policies|schedule)", r"application rationali[sz]ation", r"(?:sap|oracle|mainframe) (?:archiv\w+|decommission\w*)"]

_TERM_RX = {c["key"]: re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in sorted(c["terms"], key=len, reverse=True)) + r")(?![\w])", re.I) for c in COMPETITORS}
_PROBLEM_RX = re.compile(r"(?<![\w])(" + "|".join(PROBLEM_TRIGGERS) + r")(?![\w])", re.I)
# "Archon" and "Optim" alone are common words; require a vendor nearby.
_NEEDS_VENDOR = {"platform3-archon": re.compile(r"platform\s*3", re.I), "ibm-optim": re.compile(r"\b(ibm|infosphere)\b", re.I)}


def find_competitors(text: str) -> List[dict]:
    """Competitor products mentioned in `text`, each with the sentence it appears in."""
    text = text or ""
    found = []
    for key, rx in _TERM_RX.items():
        m = rx.search(text)
        if not m:
            continue
        need = _NEEDS_VENDOR.get(key)
        if need and not need.search(text):
            continue
        found.append({"key": key, "product": COMPETITOR_BY_KEY[key]["product"], "term": m.group(1), "sentence": sentence_around(text, m.start())})
    return found


def find_problem(text: str) -> Optional[dict]:
    m = _PROBLEM_RX.search(text or "")
    if not m:
        return None
    return {"term": m.group(1).lower(), "sentence": sentence_around(text, m.start())}


def sentence_around(text: str, pos: int, width: int = 280) -> str:
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos), text.rfind("•", 0, pos))
    start = 0 if start < 0 else start + 1
    end_candidates = [i for i in (text.find(".", pos), text.find("\n", pos)) if i >= 0]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    s = re.sub(r"\s+", " ", text[start:end]).strip()
    if len(s) > width:
        lo = max(0, pos - start - width // 2)
        s = "..." + s[lo:lo + width].strip() + "..."
    return s


def highlight_terms() -> dict:
    """What the admin highlights: competitor terms red, problem words gold."""
    terms = sorted({t for c in COMPETITORS for t in c["terms"] + [c["product"], c["vendor"]]}, key=len, reverse=True)
    return {"competitors": terms, "problems": PROBLEM_PATTERNS}


# --- Roles -----------------------------------------------------------------------
STRENGTHS = ["Strong", "Medium", "Weak", "Unrated"]
STRENGTH_RANK = {s: i for i, s in enumerate(STRENGTHS)}
# Scaled so one fresh Strong posting is ~20 and an account needs two signals (or a deep
# dive) to clear EchoLocate's "account score above 30" outreach floor.
STRENGTH_POINTS = {"Strong": 20, "Medium": 12, "Weak": 6, "Unrated": 4}
OTHER_SIGNAL_POINTS = 16

# Hands-on operators of the tool are the strongest tell; then the people who own
# the budget; administrative roles say the least.
_STRONG = re.compile(r"\b(it|engineer\w*|engg|eng|platform|data|developer|dev ?ops|sw|software|architect\w*|dba|database|infrastructure|systems?|analyst|ecm|content management|"
                     r"etl|integration|cloud|technical|technology|programmer|sre|admin(?:istrator)? (?:ii|iii|iv|sr|senior)|(?:system|systems|database|platform|etl|application|documentum|sharepoint|m365|exchange) administrator)\b", re.I)
_MEDIUM = re.compile(r"\b(manager|director|head|vp|avp|svp|evp|vice president|chief|cio|cto|cdo|ciso|lead|principal|owner|product management)\b", re.I)
_WEAK = re.compile(r"\b(administrative|assistant|admin|technician|coordinator|clerk|specialist|associate|steno|navigator|expeditor|intern|co-op)\b", re.I)


def role_strength(title: str) -> str:
    t = title or ""
    if _STRONG.search(t) and not re.search(r"\badministrative\b", t, re.I):
        return "Strong"
    if _MEDIUM.search(t):
        return "Medium"
    if _WEAK.search(t):
        return "Weak"
    return "Unrated"


# --- Company identity -------------------------------------------------------------
_SUFFIX = re.compile(r"\s+(inc|llc|ltd|limited|plc|corp|corporation|co|company|group|holdings|incorporated|sa|ag|nv|gmbh|lp|llp)$")
STAFFING = re.compile(r"\b(staffing|recruit\w*|talent|consult\w*|teksystems|kforce|insight global|apex systems|cybercoders|robert half|randstad|adecco|manpower|"
                      r"infosys|cognizant|accenture|deloitte|capgemini|wipro|hcl\w*|tata consultancy|tcs|ltimindtree|kpmg|pwc|ernst|genpact|dice|jobot|motion recruitment)\b", re.I)


def company_key(name: str) -> str:
    """'The Hartford' == 'Hartford'; 'Kiewit Corp' == 'Kiewit'; 'TD Bank Group' keeps 'td bank'."""
    k = (name or "").strip().lower()
    k = re.sub(r"[.,'’\"()|]", " ", k)
    k = re.sub(r"\s+", " ", k).strip()
    k = re.sub(r"^the ", "", k)
    for _ in range(2):
        k = _SUFFIX.sub("", k).strip()
    return k


def domain_of(value: Optional[str]) -> Optional[str]:
    """Registrable-ish domain from an email or URL: 'jane@mail.lpl.com' -> 'lpl.com'."""
    if not value:
        return None
    v = value.strip().lower()
    v = v.split("@")[-1]
    v = re.sub(r"^[a-z]+://", "", v).split("/")[0].split(":")[0]
    if not v or "." not in v:
        return None
    parts = v.split(".")
    two_level = {"co.uk", "com.au", "co.in", "co.jp", "com.br", "com.sg", "co.nz", "org.uk", "ac.uk", "gov.uk", "com.mx", "co.za"}
    if len(parts) >= 3 and ".".join(parts[-2:]) in two_level:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_staffing(name: str) -> bool:
    return bool(STAFFING.search(name or ""))


def dedupe_key(*parts) -> str:
    raw = "|".join(str(p or "").strip().lower() for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()


# --- Geography and routing -----------------------------------------------------------
US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
             "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
             "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
             "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
             "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
             "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
             "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico"}
NAME_TO_ABBR = {v.lower(): k for k, v in US_STATES.items()}
REGIONS = {
    "Europe & UK": ["United Kingdom", "England", "Scotland", "Wales", "Northern Ireland", "Ireland", "Germany", "France", "Netherlands", "Belgium", "Switzerland", "Austria",
                    "Italy", "Spain", "Portugal", "Sweden", "Norway", "Denmark", "Finland", "Iceland", "Poland", "Czech Republic", "Czechia", "Slovakia", "Hungary", "Romania",
                    "Bulgaria", "Greece", "Croatia", "Slovenia", "Serbia", "Lithuania", "Latvia", "Estonia", "Luxembourg", "Malta", "Cyprus", "Ukraine", "Jersey", "Guernsey", "Liechtenstein"],
    "MEA": ["United Arab Emirates", "Saudi Arabia", "Qatar", "Kuwait", "Bahrain", "Oman", "Israel", "Jordan", "Lebanon", "Turkey", "Egypt", "South Africa", "Nigeria", "Kenya",
            "Ghana", "Morocco", "Tunisia", "Ethiopia", "Tanzania", "Uganda", "Mauritius"],
    "APAC": ["India", "Pakistan", "Bangladesh", "Sri Lanka", "Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam", "Philippines", "China", "Hong Kong", "Taiwan",
             "South Korea", "Korea", "Japan", "Australia", "New Zealand"],
    "LATAM": ["Mexico", "Brazil", "Argentina", "Chile", "Colombia", "Peru", "Costa Rica", "Panama", "Uruguay", "Ecuador", "Guatemala", "Dominican Republic"],
}
COUNTRY_TO_REGION = {c: r for r, cs in REGIONS.items() for c in cs}
TERRITORY_ORDER = ["USA", "Canada", "Europe & UK", "MEA", "APAC", "LATAM", "Unrouted"]
ISO2 = {"united states": "US", "canada": "CA", "united kingdom": "GB", "germany": "DE", "poland": "PL", "france": "FR", "netherlands": "NL", "ireland": "IE",
        "spain": "ES", "italy": "IT", "switzerland": "CH", "austria": "AT", "sweden": "SE", "denmark": "DK", "norway": "NO", "finland": "FI", "belgium": "BE",
        "india": "IN", "singapore": "SG", "australia": "AU", "united arab emirates": "AE", "japan": "JP", "liechtenstein": "LI", "luxembourg": "LU", "portugal": "PT"}

# The routing table the original Leads Radar used, as the editable default.
DEFAULT_TERRITORIES: dict = {
    "territory_owners": {"USA": "Jason Russo", "LATAM": "Jason Russo", "Canada": "Bob Wright", "Europe & UK": "Matt Long", "MEA": "Anil Kumar", "APAC": "Samit Mandal", "Unrouted": "Jason Russo"},
    "us_states": {
        **{s: "Andy Hebert" for s in ("CT", "MA", "RI", "NH", "VT", "ME")},
        **{s: "Tracy Blackburn" for s in ("OH", "KY", "TN", "GA", "AL", "MS", "NJ")},
        **{s: "Jason Russo" for s in ("NY", "PA", "PR")},
        **{s: "Bo Deas" for s in ("NC", "SC", "VA", "WV", "DE", "DC", "FL", "MD")},
        **{s: "Hameed Hemmat" for s in ("NV", "WA", "WY", "ID", "AK", "HI", "MT", "OR", "MO")},
        **{s: "Keith Lee" for s in ("CO", "TX", "IN")},
        **{s: "Brenda Goldstein" for s in ("AZ", "UT", "NM", "IL")},
        **{s: "Steve Morris" for s in ("WI", "MI", "LA", "ND", "SD", "NE", "KS", "OK", "MN", "AR", "IA")},
    },
    # California splits by HQ ZIP: 93600 and above is north.
    "ca_split": {"zip": 93600, "north": "Hameed Hemmat", "south": "Brenda Goldstein"},
    # Country overrides inside a region (e.g. the EchoLocate EU AEs). Empty = the region owner.
    "countries": {},
    "named_accounts": [
        {"match": m, "rep": r} for m, r in [
            ("ahold delhaize", "Andy Hebert"), ("fcci insurance", "Andy Hebert"), ("sensata", "Andy Hebert"), ("shaw industries", "Andy Hebert"), ("terex", "Andy Hebert"), ("whitecap", "Andy Hebert"),
            ("avery dennison", "Tracy Blackburn"), ("dana incorporated", "Tracy Blackburn"), ("eaton", "Tracy Blackburn"), ("ge appliances", "Tracy Blackburn"), ("georgia-pacific", "Tracy Blackburn"),
            ("progressive", "Tracy Blackburn"), ("citi", "Tracy Blackburn"), ("stanley black", "Tracy Blackburn"),
            ("sealed air", "Bo Deas"), ("tempur sealy", "Bo Deas"), ("w. l. gore", "Bo Deas"), ("w.l. gore", "Bo Deas"), ("willis towers watson", "Bo Deas"), ("trane", "Bo Deas"), ("molson coors", "Bo Deas"),
            ("pvh", "Bo Deas"), ("reynolds", "Bo Deas"), ("american tire", "Bo Deas"), ("ryder", "Bo Deas"), ("brink", "Bo Deas"),
            ("adeia", "Hameed Hemmat"), ("broadcom", "Hameed Hemmat"), ("coopervision", "Hameed Hemmat"), ("juniper", "Hameed Hemmat"), ("linkedin", "Hameed Hemmat"), ("ross stores", "Hameed Hemmat"),
            ("shutterfly", "Hameed Hemmat"), ("starbucks", "Hameed Hemmat"), ("deckers", "Hameed Hemmat"), ("wells fargo", "Hameed Hemmat"), ("amazon", "Hameed Hemmat"), ("netapp", "Hameed Hemmat"),
            ("bio-rad", "Hameed Hemmat"), ("roseburg", "Hameed Hemmat"), ("infor", "Hameed Hemmat"),
            ("amway", "Keith Lee"), ("cuyahoga", "Keith Lee"), ("insignia financial", "Keith Lee"), ("jll", "Keith Lee"), ("jones lang lasalle", "Keith Lee"), ("kaiser permanente", "Keith Lee"),
            ("optum", "Keith Lee"), ("unitedhealth", "Keith Lee"), ("heidelberg materials", "Keith Lee"), ("stryker", "Keith Lee"),
            ("delta dental", "Brenda Goldstein"), ("mattel", "Brenda Goldstein"), ("paramount", "Brenda Goldstein"), ("schreiber foods", "Brenda Goldstein"), ("honest company", "Brenda Goldstein"),
            ("ttm technologies", "Brenda Goldstein"), ("wismettac", "Brenda Goldstein"), ("herbalife", "Brenda Goldstein"), ("universal music", "Brenda Goldstein"), ("panasonic", "Brenda Goldstein"),
            ("zurich", "Brenda Goldstein"), ("health care service corp", "Brenda Goldstein"),
            ("acco brands", "Steve Morris"), ("aig", "Steve Morris"), ("corebridge", "Steve Morris"), ("usg", "Steve Morris"), ("helen of troy", "Steve Morris"), ("j.b. hunt", "Steve Morris"),
            ("jb hunt", "Steve Morris"), ("johnson controls", "Steve Morris"), ("motorola", "Steve Morris"), ("pathward", "Steve Morris"), ("regal rexnord", "Steve Morris"), ("donaldson", "Steve Morris"),
            ("cargill", "Alan Schneider"), ("ecolab", "Alan Schneider"), ("elevance", "Alan Schneider"), ("mcdonald", "Alan Schneider"), ("medline", "Alan Schneider"),
            ("merck & co", "Robert Hicks"), ("metlife", "Robert Hicks"), ("pepsico", "Robert Hicks"),
            ("alberta health", "Bob Wright"), ("suncor", "Bob Wright"), ("bc liquor", "Bob Wright"), ("irving oil", "Bob Wright"), ("enbridge", "Bob Wright"),
        ]
    ],
    "rep_territory": {"Bob Wright": "Canada", "Matt Long": "Europe & UK", "Anil Kumar": "MEA", "Samit Mandal": "APAC"},
    # rep name -> staff email, so routed accounts land on a real admin user / team member.
    "rep_emails": {},
}
_CA_NORTH_CITIES = re.compile(r"san francisco|oakland|san jose|sacramento|palo alto|mountain view|sunnyvale|santa clara|fremont|redwood|menlo park|san mateo|berkeley|walnut creek|pleasanton|"
                              r"san ramon|santa rosa|napa|stockton|modesto|fresno|cupertino|milpitas|foster city|south san francisco|emeryville|hayward|concord|livermore|dublin, ca|san rafael|"
                              r"petaluma|roseville|folsom|rancho cordova", re.I)
_CA_SOUTH_CITIES = re.compile(r"los angeles|san diego|irvine|santa barbara|santa monica|long beach|anaheim|burbank|glendale|pasadena|torrance|carlsbad|riverside|san bernardino|ontario, ca|"
                              r"bakersfield|thousand oaks|el segundo|culver city|costa mesa|newport beach|orange, ca|santa ana|calabasas|woodland hills|oxnard|temecula|la jolla|escondido|"
                              r"chula vista|san marcos|westlake village|aliso viejo|lake forest|foothill ranch|camarillo|ventura", re.I)


def country_norm(c: Optional[str]) -> str:
    c = (c or "").strip()
    if re.fullmatch(r"(united states of america|usa|us|u\.s\.a?\.?)", c, re.I):
        return "United States"
    if re.fullmatch(r"(uk|great britain|gb|britain)", c, re.I):
        return "United Kingdom"
    return c


def iso2(country: Optional[str]) -> Optional[str]:
    c = country_norm(country).lower()
    if len(c) == 2:
        return c.upper()
    return ISO2.get(c)


def territory_of(country: Optional[str]) -> str:
    c = country_norm(country)
    if c in ("United States", "Puerto Rico"):
        return "USA"
    if c == "Canada":
        return "Canada"
    return COUNTRY_TO_REGION.get(c, "Unrouted")


def state_abbr(location: Optional[str], state: Optional[str]) -> str:
    loc = (location or "").strip()
    m = re.search(r",\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$", loc)
    if m and m.group(1) in US_STATES:
        return m.group(1)
    m = re.search(r",\s*([A-Za-z .]+?),\s*(United States|USA|US)\b", loc, re.I)
    if m and m.group(1).strip().lower() in NAME_TO_ABBR:
        return NAME_TO_ABBR[m.group(1).strip().lower()]
    if loc.lower() in NAME_TO_ABBR:
        return NAME_TO_ABBR[loc.lower()]
    st = (state or "").strip()
    if st.lower() in NAME_TO_ABBR:
        return NAME_TO_ABBR[st.lower()]
    if st.upper() in US_STATES:
        return st.upper()
    return ""


def route(company_name: str, country: Optional[str], location: Optional[str] = None, state: Optional[str] = None, zip_code: Optional[str] = None, cfg: Optional[dict] = None) -> dict:
    """Owner for one piece of geography. Named accounts win, then US state, then country/region."""
    cfg = cfg or DEFAULT_TERRITORIES
    owners = cfg.get("territory_owners") or {}
    name = company_key(company_name)
    for na in cfg.get("named_accounts") or []:
        m = (na.get("match") or "").strip().lower()
        if m and re.search(r"(?<![a-z0-9])" + re.escape(m) + r"(?![a-z0-9])", name):
            rep = na.get("rep")
            territory = (cfg.get("rep_territory") or {}).get(rep) or (territory_of(country) if country else "USA")
            return {"territory": territory, "rep": rep, "basis": "named"}
    territory = territory_of(country)
    if territory == "USA":
        st = state_abbr(location, state)
        if not st:
            return {"territory": territory, "rep": owners.get("USA"), "basis": "needs_routing"}
        if st == "CA":
            split = cfg.get("ca_split") or {}
            z = re.search(r"\b(9[0-6]\d{3})\b", f"{zip_code or ''} {location or ''}")
            if z:
                return {"territory": territory, "rep": split.get("north") if int(z.group(1)) >= int(split.get("zip", 93600)) else split.get("south"), "basis": "state"}
            if _CA_NORTH_CITIES.search(location or ""):
                return {"territory": territory, "rep": split.get("north"), "basis": "state"}
            if _CA_SOUTH_CITIES.search(location or ""):
                return {"territory": territory, "rep": split.get("south"), "basis": "state"}
            return {"territory": territory, "rep": owners.get("USA"), "basis": "needs_routing"}
        rep = (cfg.get("us_states") or {}).get(st)
        return {"territory": territory, "rep": rep or owners.get("USA"), "basis": "state" if rep else "needs_routing"}
    country_rep = (cfg.get("countries") or {}).get(country_norm(country))
    if country_rep:
        return {"territory": territory, "rep": country_rep, "basis": "country"}
    return {"territory": territory, "rep": owners.get(territory) or owners.get("Unrouted"), "basis": "needs_routing" if territory == "Unrouted" else "country"}


def route_company(name: str, signals: List[dict], hq: Optional[dict], cfg: Optional[dict] = None) -> dict:
    """A company's route: its HQ when known, else the majority of its signals' locations."""
    if hq and hq.get("country"):
        return route(name, hq.get("country"), hq.get("location"), hq.get("state"), hq.get("zip"), cfg)
    votes: Dict[tuple, int] = {}
    named = None
    for s in signals:
        r = route(name, s.get("country"), s.get("location"), s.get("state"), s.get("zip"), cfg)
        if r["basis"] == "named":
            named = r
        k = (r["territory"], r["rep"], r["basis"])
        votes[k] = votes.get(k, 0) + 1
    if named:
        return named
    if not votes:
        r = route(name, None, None, None, None, cfg)
        return r if r["basis"] == "named" else {"territory": "Unrouted", "rep": (cfg or DEFAULT_TERRITORIES).get("territory_owners", {}).get("Unrouted"), "basis": "needs_routing"}
    (t, rep, basis), _ = max(votes.items(), key=lambda kv: kv[1])
    return {"territory": t, "rep": rep, "basis": basis}


# --- Scoring ------------------------------------------------------------------------
SIGNAL_TYPES = ["competitor_job", "problem_job", "dns", "cert", "linkedin", "news", "filing", "tender", "website"]
TYPE_WEIGHTS = {"competitor_job": 1.0, "problem_job": 0.7, "dns": 0.8, "cert": 0.8, "linkedin": 0.6, "news": 0.5, "filing": 0.6, "tender": 0.9, "website": 0.8}
SURGE_COUNT, SURGE_DAYS = 3, 7
READY_SCORE = 6  # deep dive score that means "ready for outreach"


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso)[:32].replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.fromisoformat(str(iso)[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def signal_points(sig: dict, half_life_days: float, weights: Optional[dict] = None, now: Optional[datetime] = None) -> float:
    strength = sig.get("role_strength") or "Unrated"
    base = STRENGTH_POINTS.get(strength, 4) if sig.get("type") in ("competitor_job", "problem_job", "linkedin") else OTHER_SIGNAL_POINTS
    w = (weights or {}).get(sig.get("type"), TYPE_WEIGHTS.get(sig.get("type"), 0.5))
    when = _parse(sig.get("posted_at") or sig.get("first_seen_at"))
    now = now or datetime.now(timezone.utc)
    days = max(0.0, (now - when).total_seconds() / 86400) if when else 0.0
    return round(base * w * 0.5 ** (days / max(half_life_days, 1)), 2)


def rollup(signals: List[dict], *, half_life_days: float = 30, weights: Optional[dict] = None, website: Optional[dict] = None, research: Optional[dict] = None,
           now: Optional[datetime] = None) -> dict:
    """Everything the Radar shows about a company that is computed, not typed by a person."""
    now = now or datetime.now(timezone.utc)
    strength = "Unrated"
    comps: Dict[str, int] = {}
    slugs: Dict[str, float] = {}
    types = set()
    score = 0.0
    latest = None
    first = None
    recent = 0
    last_30 = 0
    for s in signals:
        if s.get("type") in ("competitor_job", "problem_job") and STRENGTH_RANK.get(s.get("role_strength") or "Unrated", 3) < STRENGTH_RANK[strength]:
            strength = s.get("role_strength") or "Unrated"
        pts = signal_points(s, half_life_days, weights, now)
        score += pts
        types.add(s.get("type"))
        key = s.get("competitor_key")
        if key:
            comps[key] = comps.get(key, 0) + 1
            for slug in (COMPETITOR_BY_KEY.get(key) or {}).get("slugs", []):
                slugs[slug] = slugs.get(slug, 0) + pts
        when = s.get("posted_at") or s.get("first_seen_at")
        if when and (latest is None or str(when) > latest):
            latest = str(when)[:25]
        fs = _parse(s.get("first_seen_at"))
        if fs:
            if first is None or fs < first:
                first = fs
            if now - fs <= timedelta(days=SURGE_DAYS):
                recent += 1
            if now - fs <= timedelta(days=30):
                last_30 += 1
    if website and website.get("score"):
        types.add("website")
        score += float(website["score"]) * TYPE_WEIGHTS["website"] * (weights or {}).get("website", 1) / max(TYPE_WEIGHTS["website"], 0.01)
        for slug, v in (website.get("product_scores") or {}).items():
            slugs[slug] = slugs.get(slug, 0) + v * 0.5
    if research and research.get("status") in ("approved", "draft") and research.get("score") is not None:
        score += max(0, float(research["score"]) - 4) * 5
    lines = scoring.line_scores(slugs)
    primary_line = max(lines, key=lines.get) if lines else None
    primary_product = max((p for p in slugs if scoring.PRODUCT_TO_LINE.get(p) == primary_line), key=lambda p: slugs[p], default=None) if primary_line else None
    return {
        "strength": strength,
        "score": round(score, 1),
        "surge": recent >= SURGE_COUNT,
        "signals_7d": recent,
        "signals_30d": last_30,
        "signal_count": len(signals),
        "signal_types": sorted(t for t in types if t),
        "competitors": [COMPETITOR_BY_KEY[k]["product"] for k, _ in sorted(comps.items(), key=lambda kv: -kv[1]) if k in COMPETITOR_BY_KEY],
        "competitor_keys": [k for k, _ in sorted(comps.items(), key=lambda kv: -kv[1])],
        "primary_line": primary_line,
        "primary_product": primary_product,
        "latest_signal_at": latest,
        "first_seen_at": first.isoformat() if first else None,
    }


def verdict_class(research: Optional[dict]) -> Optional[str]:
    if not research:
        return None
    v = (research.get("verdict") or "").lower()
    if v.startswith("ready") or (research.get("score") or 0) >= READY_SCORE:
        return "ready"
    if v.startswith("research"):
        return "more"
    return "not"


def freshness_stamp(company: dict) -> str:
    """Changes whenever a viewer should see the company as 'Updated'."""
    return f"{company.get('latest_signal_at') or ''}|{company.get('research_score') if company.get('research_score') is not None else ''}|{company.get('signal_count') or 0}"


STATUSES = ["new", "researching", "ready", "contacted", "replied", "meeting_booked", "opportunity", "won", "lost", "not_a_fit", "excluded"]
STATUS_LABELS = {"new": "New", "researching": "Researching", "ready": "Ready for outreach", "contacted": "Contacted", "replied": "Replied", "meeting_booked": "Meeting booked",
                 "opportunity": "Opportunity", "won": "Won", "lost": "Lost", "not_a_fit": "Not a fit", "excluded": "Excluded"}
# Statuses set by people (or by a reply) are never moved back by the system.
SYSTEM_STATUSES = {"new", "researching", "ready", "excluded"}
LEGACY_STATUS = {"new": "new", "contacted": "contacted", "meeting booked": "meeting_booked", "not a fit": "not_a_fit", "researching": "researching"}


def legacy_status(value: Optional[str]) -> str:
    return LEGACY_STATUS.get((value or "new").strip().lower(), "new")


def is_research_candidate(company: dict, now_iso_str: str, refresh_days: int = 30) -> bool:
    if company.get("excluded_reason") or company.get("status") in ("not_a_fit", "excluded", "won", "lost"):
        return False
    hot = company.get("strength") == "Strong" or len(company.get("signal_types") or []) >= 2 or company.get("surge")
    if not hot:
        return False
    last = _parse(company.get("research_at"))
    now = _parse(now_iso_str) or datetime.now(timezone.utc)
    return last is None or (now - last) > timedelta(days=refresh_days)
