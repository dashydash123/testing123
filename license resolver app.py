#!/usr/bin/env python3
"""
License Resolver — single-URL edition
=====================================

Give it ONE Hugging Face URL, e.g.

    https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2

…and it automatically:
  1. discovers every component  — the MODEL, its DATASETS, and its DEPENDENCIES
  2. resolves each one's license using the built-in Lookup Cascade
     (it picks the method automatically, highest-accuracy source first)
  3. shows results grouped component-wise per module, flagging any
     low-confidence license.

No other input is required. Public, keyless sources only.

Self-contained: on first launch it pip-installs the optional helper libraries
(requests, beautifulsoup4) if missing. It still runs on the standard library
alone if installation isn't possible.

Run:        python license_resolver_app.py
Headless:   python license_resolver_app.py --headless https://huggingface.co/<...>
"""

from __future__ import annotations

import csv
import importlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Self-installing optional dependencies
# ─────────────────────────────────────────────────────────────────────────────
_OPTIONAL = [("requests", "requests>=2.31"), ("bs4", "beautifulsoup4>=4.12")]
_HAS_REQUESTS = False
_HAS_BS4 = False
_HAS_SELENIUM = False
requests = None
BeautifulSoup = None


def _load_optional_libs():
    global _HAS_REQUESTS, _HAS_BS4, _HAS_SELENIUM, requests, BeautifulSoup
    try:
        import requests as _rq
        requests = _rq; _HAS_REQUESTS = True
    except Exception:
        _HAS_REQUESTS = False
    try:
        from bs4 import BeautifulSoup as _bs
        BeautifulSoup = _bs; _HAS_BS4 = True
    except Exception:
        _HAS_BS4 = False
    try:
        import selenium  # noqa: F401
        _HAS_SELENIUM = True
    except Exception:
        _HAS_SELENIUM = False


def ensure_dependencies(log=lambda m: None):
    """pip-install missing optional libs (best effort, never fatal)."""
    for mod, pip_spec in _OPTIONAL:
        try:
            importlib.import_module(mod)
        except ImportError:
            log(f"Installing {pip_spec} …")
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                                "--disable-pip-version-check", pip_spec],
                               check=False, timeout=180)
            except Exception as e:
                log(f"  (could not install {pip_spec}: {e}; using stdlib fallback)")
    _load_optional_libs()


_load_optional_libs()

USER_AGENT = "Mozilla/5.0 (compatible; license-resolver/3.0; +compliance-tooling)"
TIMEOUT = 20


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer
# ─────────────────────────────────────────────────────────────────────────────
def _http(url: str, headers: Optional[dict] = None) -> Optional[str]:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    if _HAS_REQUESTS:
        try:
            r = requests.get(url, headers=h, timeout=TIMEOUT)
            return r.text if r.status_code == 200 else None
        except Exception:
            return None
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def get_text(url: str, headers: Optional[dict] = None) -> Optional[str]:
    return _http(url, headers)


def get_json(url: str, headers: Optional[dict] = None) -> Optional[Any]:
    raw = _http(url, headers)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# License-text detection
# ─────────────────────────────────────────────────────────────────────────────
LICENSE_FINGERPRINTS: Dict[str, List[str]] = {
    "Apache-2.0": ["apache license version 2 0 january 2004",
                   "licensed under the apache license version 2 0"],
    "MIT": ["permission is hereby granted free of charge to any person obtaining a copy"],
    "BSD-3-Clause": ["neither the name of the copyright holder nor the names of its contributors"],
    "BSD-2-Clause": ["redistribution and use in source and binary forms with or without "
                     "modification are permitted provided that the following conditions are met"],
    "ISC": ["permission to use copy modify and or distribute this software for any purpose"],
    "GPL-3.0": ["gnu general public license version 3 29 june 2007"],
    "GPL-2.0": ["gnu general public license version 2 june 1991"],
    "LGPL-3.0": ["gnu lesser general public license version 3 29 june 2007"],
    "LGPL-2.1": ["gnu lesser general public license version 2 1 february 1999"],
    "AGPL-3.0": ["gnu affero general public license version 3 19 november 2007"],
    "MPL-2.0": ["mozilla public license version 2 0"],
    "Unlicense": ["this is free and unencumbered software released into the public domain"],
    "CC0-1.0": ["creative commons cc0 1 0 universal", "cc0 1 0 universal"],
    "CC-BY-4.0": ["creative commons attribution 4 0 international", "attribution 4 0 international"],
    "CC-BY-SA-4.0": ["creative commons attribution sharealike 4 0 international",
                     "attribution sharealike 4 0 international"],
    "CC-BY-NC-4.0": ["creative commons attribution noncommercial 4 0 international",
                     "attribution noncommercial 4 0 international"],
    "CC-BY-NC-SA-4.0": ["creative commons attribution noncommercial sharealike 4 0 international",
                        "attribution noncommercial sharealike 4 0 international"],
    "OpenRAIL-M": ["openrail", "responsible ai license", "creativeml open rail m",
                   "bigscience open rail m", "bigcode openrail m"],
    "Llama-3": ["llama 3 community license agreement", "llama 3 1 community license agreement",
                "llama 3 2 community license agreement", "llama 3 3 community license agreement"],
    "Llama-2": ["llama 2 community license agreement"],
    "Gemma": ["gemma terms of use"],
    "Falcon-LLM-License": ["falcon llm license", "tii falcon llm license"],
}
_DETECT_ORDER = ["CC-BY-NC-SA-4.0", "CC-BY-NC-4.0", "CC-BY-SA-4.0", "CC-BY-4.0", "CC0-1.0",
                 "AGPL-3.0", "LGPL-3.0", "LGPL-2.1", "GPL-3.0", "GPL-2.0",
                 "Llama-3", "Llama-2", "Gemma", "Falcon-LLM-License", "OpenRAIL-M",
                 "MPL-2.0", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause",
                 "ISC", "MIT", "Unlicense"]
ALIASES = {
    "apache-2.0": "Apache-2.0", "apache2.0": "Apache-2.0", "apache 2.0": "Apache-2.0",
    "mit": "MIT", "bsd-3-clause": "BSD-3-Clause", "bsd": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause", "gpl-3.0": "GPL-3.0", "gpl-2.0": "GPL-2.0",
    "lgpl-3.0": "LGPL-3.0", "lgpl-2.1": "LGPL-2.1", "agpl-3.0": "AGPL-3.0",
    "mpl-2.0": "MPL-2.0", "isc": "ISC", "unlicense": "Unlicense", "cc0-1.0": "CC0-1.0",
    "cc-by-4.0": "CC-BY-4.0", "cc-by-sa-4.0": "CC-BY-SA-4.0", "cc-by-nc-4.0": "CC-BY-NC-4.0",
    "cc-by-nc-sa-4.0": "CC-BY-NC-SA-4.0", "creativeml-openrail-m": "OpenRAIL-M",
    "openrail": "OpenRAIL-M", "bigscience-openrail-m": "OpenRAIL-M",
    "bigcode-openrail-m": "OpenRAIL-M", "apache": "Apache-2.0",
    "llama2": "Llama-2", "llama3": "Llama-3", "llama3.1": "Llama-3",
    "llama3.2": "Llama-3", "llama3.3": "Llama-3", "gemma": "Gemma",
}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower()))


def normalise_declared(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return ALIASES.get(value.strip().lower(), value.strip())


def detect_license_from_text(text: Optional[str]) -> Optional[str]:
    if not text or len(text.strip()) < 20:
        return None
    norm = _normalise(text)
    for spdx in _DETECT_ORDER:
        for fp in LICENSE_FINGERPRINTS.get(spdx, []):
            if _normalise(fp) in norm:
                return spdx
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Confidence tiers
# ─────────────────────────────────────────────────────────────────────────────
C_VERIFIED, C_TEXT, C_DECLARED = "verified", "text-match", "declared"
C_INHERITED, C_SEARCH, C_NONE = "inherited", "search-derived", "none"
CONF_RANK = {C_VERIFIED: 5, C_TEXT: 4, C_DECLARED: 3, C_INHERITED: 2, C_SEARCH: 1, C_NONE: 0}
LOW_CONFIDENCE_BELOW = CONF_RANK[C_DECLARED]   # inherited / search-derived / none flagged


@dataclass
class LicenseResult:
    component: str
    component_type: str
    license: Optional[str] = None
    declared: Optional[str] = None
    detected: Optional[str] = None
    confidence: str = C_NONE
    source: str = ""
    trail: List[str] = field(default_factory=list)
    license_link: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def log(self, m: str):
        self.trail.append(m)

    @property
    def low_confidence(self) -> bool:
        return CONF_RANK.get(self.confidence, 0) < LOW_CONFIDENCE_BELOW

    @property
    def low_confidence_reason(self) -> str:
        if self.confidence == C_NONE:
            return "No license found — treat as all-rights-reserved."
        if self.confidence == C_SEARCH:
            return "Found via web/KB search — confirm against the source."
        if self.confidence == C_INHERITED:
            return "Inherited from base model — verify it applies."
        if any("DRIFT" in n for n in self.notes):
            return "Declared tag and file text disagree."
        return ""

    def finalise(self) -> "LicenseResult":
        if self.confidence in (C_INHERITED, C_SEARCH):
            return self
        if self.declared and self.detected:
            if normalise_declared(self.declared) == self.detected:
                self.license, self.confidence = self.detected, C_VERIFIED
            else:
                self.license, self.confidence = self.detected, C_TEXT
                self.notes.append(f"LICENSE DRIFT: declared '{self.declared}' but file text "
                                  f"matches '{self.detected}'. Text trusted; review manually.")
        elif self.detected:
            self.license, self.confidence = self.detected, C_TEXT
        elif self.declared:
            self.license, self.confidence = normalise_declared(self.declared), C_DECLARED
        else:
            self.confidence = C_NONE
            self.notes.append("No license declared or detected. Default in most jurisdictions = "
                              "all rights reserved; commercial use NOT permitted without grant.")
        return self


def _apply_search(r: LicenseResult, lic: str, source: str, url: Optional[str]) -> LicenseResult:
    r.license = normalise_declared(lic) or lic
    r.confidence = C_SEARCH
    r.source = source
    if url:
        r.license_link = r.license_link or url
    r.log(f"search-derived '{r.license}' via {source}")
    r.notes.append("SEARCH-DERIVED: found via web/external KB, not the component's own metadata "
                   "or LICENSE file. Requires analyst confirmation.")
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────
LICENSE_FILES = ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENSE.rst",
                 "license", "License"]


def try_license_files(raw_base: str) -> Optional[Tuple[str, str]]:
    for f in LICENSE_FILES:
        txt = get_text(raw_base + f)
        if txt:
            spdx = detect_license_from_text(txt)
            if spdx:
                return spdx, f
    return None


def github_from_text(s: str) -> Optional[str]:
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?<>\"')]+)", s or "")
    return f"{m.group(1)}/{re.sub(r'.git$', '', m.group(2))}" if m else None


def github_license(owner_repo: str) -> Tuple[Optional[str], Optional[str]]:
    data = get_json(f"https://api.github.com/repos/{owner_repo}/license",
                    {"Accept": "application/vnd.github+json"})
    if not data:
        return None, None
    spdx = (data.get("license") or {}).get("spdx_id")
    if spdx in (None, "NOASSERTION"):
        dl = data.get("download_url")
        spdx = detect_license_from_text(get_text(dl) or "") if dl else None
    return spdx, data.get("html_url")


def parse_yaml_license(readme: Optional[str]) -> Optional[str]:
    if not readme:
        return None
    m = re.search(r"(?im)^\s*license\s*:\s*([A-Za-z0-9.\-]+)\s*$", readme)
    return m.group(1) if m else None


# ── keyless web search / KB ──────────────────────────────────────────────────
def ddg_search(query: str, limit: int = 6) -> List[str]:
    html = get_text("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})) \
        or get_text("https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query}))
    if not html:
        return []
    urls: List[str] = []
    if _HAS_BS4:
        for a in BeautifulSoup(html, "html.parser").select("a.result__a, a.result-link"):
            href = a.get("href", "")
            mm = re.search(r"uddg=([^&]+)", href)
            if mm:
                href = urllib.parse.unquote(mm.group(1))
            if href.startswith("http") and "duckduckgo.com" not in href:
                urls.append(href)
    else:
        for m in re.finditer(r'href="(https?://[^"]+|//duckduckgo\.com/l/\?uddg=[^"]+)"', html):
            href = m.group(1)
            um = re.search(r"uddg=([^&\"]+)", href)
            if um:
                href = urllib.parse.unquote(um.group(1))
            if href.startswith("http") and "duckduckgo.com" not in href:
                urls.append(href)
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
        if len(out) >= limit:
            break
    return out


def wikidata_license(name: str) -> Optional[Tuple[str, str]]:
    q = urllib.parse.urlencode({"action": "wbsearchentities", "search": name,
                                "language": "en", "format": "json", "type": "item", "limit": "3"})
    hits = get_json("https://www.wikidata.org/w/api.php?" + q)
    for hit in (hits or {}).get("search", []):
        qid = hit.get("id")
        if not qid:
            continue
        cq = urllib.parse.urlencode({"action": "wbgetclaims", "entity": qid,
                                     "property": "P275", "format": "json"})
        claims = get_json("https://www.wikidata.org/w/api.php?" + cq)
        for claim in ((claims or {}).get("claims") or {}).get("P275", []):
            try:
                lq = claim["mainsnak"]["datavalue"]["value"]["id"]
            except (KeyError, TypeError):
                continue
            lbl = _wikidata_label(lq)
            if lbl:
                return lbl, f"https://www.wikidata.org/wiki/{qid}"
    return None


def _wikidata_label(qid: str) -> Optional[str]:
    q = urllib.parse.urlencode({"action": "wbgetentities", "ids": qid,
                                "props": "labels", "languages": "en", "format": "json"})
    data = get_json("https://www.wikidata.org/w/api.php?" + q)
    label = (((((data or {}).get("entities") or {}).get(qid) or {})
              .get("labels") or {}).get("en") or {}).get("value")
    if not label:
        return None
    low = label.lower()
    if "apache" in low: return "Apache-2.0"
    if "attribution-noncommercial-sharealike" in low: return "CC-BY-NC-SA-4.0"
    if "attribution-noncommercial" in low: return "CC-BY-NC-4.0"
    if "attribution-sharealike" in low: return "CC-BY-SA-4.0"
    if "creative commons attribution" in low: return "CC-BY-4.0"
    if "cc0" in low or "public domain" in low: return "CC0-1.0"
    if low in ("mit license", "mit"): return "MIT"
    if "gnu general public" in low and "3" in low: return "GPL-3.0"
    if "bsd" in low: return "BSD-3-Clause"
    return label


def web_search_license(name: str, kind: str) -> Optional[Tuple[str, str, str]]:
    wd = wikidata_license(name)
    if wd:
        return wd[0], "Wikidata P275", wd[1]
    for url in ddg_search(f'"{name}" {kind} license'):
        spdx = detect_license_from_text(get_text(url))
        if spdx:
            return spdx, f"web search ({urllib.parse.urlparse(url).netloc})", url
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MODEL / DATASET resolvers
# ─────────────────────────────────────────────────────────────────────────────
def resolve_model(model_id: str, _depth: int = 0, _api: Optional[dict] = None) -> LicenseResult:
    r = LicenseResult(component=model_id, component_type="model")
    if _depth > 4:
        return r.finalise()
    lf = try_license_files(f"https://huggingface.co/{model_id}/raw/main/")
    if lf:
        r.detected, fn = lf
        r.source = f"HF repo file: {fn}"
        r.log(f"Detected {r.detected} from {fn}")
    api = _api if _api is not None else get_json(
        f"https://huggingface.co/api/models/{urllib.parse.quote(model_id)}")
    card = (api or {}).get("cardData") or {}
    declared = card.get("license")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    if declared:
        r.declared = str(declared)
        r.log(f"HF API license = {declared}")
    if card.get("license_link"):
        r.license_link = card["license_link"]
    if not r.declared and not r.detected:
        y = parse_yaml_license(get_text(f"https://huggingface.co/{model_id}/raw/main/README.md"))
        if y:
            r.declared = y
            r.log(f"README YAML license = {y}")
    if r.declared or r.detected:
        if not r.source:
            r.source = "HF API"
        return r.finalise()
    base = card.get("base_model")
    if isinstance(base, list):
        base = base[0] if base else None
    if base and isinstance(base, str) and base != model_id:
        r.log(f"Resolving base_model {base}")
        parent = resolve_model(base, _depth + 1)
        if parent.license:
            r.declared, r.detected = parent.declared, parent.detected
            r.confidence, r.source = C_INHERITED, f"inherited from {base}"
            r.notes.append(f"Inherited from base model '{base}'. A derivative cannot be more "
                           f"permissive than its base.")
            return r
    gh = github_from_text(json.dumps(api or {})) or github_from_text(
        get_text(f"https://huggingface.co/{model_id}/raw/main/README.md") or "")
    if gh:
        spdx, link = github_license(gh)
        if spdx:
            r.detected, r.source = spdx, f"GitHub {gh}"
            r.license_link = r.license_link or link
            return r.finalise()
    hit = web_search_license(model_id.split("/")[-1], "model")
    if hit:
        return _apply_search(r, hit[0], hit[1], hit[2])
    return r.finalise()


def resolve_dataset(dataset_id: str) -> LicenseResult:
    r = LicenseResult(component=dataset_id, component_type="dataset")
    lf = try_license_files(f"https://huggingface.co/datasets/{dataset_id}/raw/main/")
    if lf:
        r.detected, fn = lf
        r.source = f"HF dataset file: {fn}"
        r.log(f"Detected {r.detected} from {fn}")
    api = get_json(f"https://huggingface.co/api/datasets/{urllib.parse.quote(dataset_id)}")
    card = (api or {}).get("cardData") or {}
    declared = card.get("license")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    if declared:
        r.declared = str(declared)
        r.log(f"HF datasets API license = {declared}")
    if not r.declared:
        cr = get_json(f"https://huggingface.co/api/datasets/{urllib.parse.quote(dataset_id)}/croissant")
        lic = (cr or {}).get("license")
        if isinstance(lic, list):
            lic = lic[0] if lic else None
        if lic:
            r.declared = str(lic)
            r.log(f"Croissant license = {lic}")
    if not r.declared:
        info = get_json("https://datasets-server.huggingface.co/info?dataset=" +
                        urllib.parse.quote(dataset_id))
        lic = (((info or {}).get("dataset_info") or {}).get("license"))
        if lic:
            r.declared = str(lic)
            r.log(f"dataset-viewer license = {lic}")
    if not r.declared and not r.detected:
        y = parse_yaml_license(get_text(
            f"https://huggingface.co/datasets/{dataset_id}/raw/main/README.md"))
        if y:
            r.declared = y
            r.log(f"README YAML license = {y}")
    if r.declared or r.detected:
        if not r.source:
            r.source = "HF datasets API"
        return r.finalise()
    gh = github_from_text(json.dumps(api or {}))
    if gh:
        spdx, link = github_license(gh)
        if spdx:
            r.detected, r.source, r.license_link = spdx, f"GitHub {gh}", link
            return r.finalise()
    pw = get_json("https://paperswithcode.com/api/v1/datasets/?" +
                  urllib.parse.urlencode({"q": dataset_id.split("/")[-1]}))
    for res in (pw or {}).get("results", [])[:3]:
        if res.get("license"):
            r.declared, r.source = str(res["license"]), "Papers With Code"
            r.license_link = res.get("url")
            return r.finalise()
    hit = web_search_license(dataset_id.split("/")[-1], "dataset")
    if hit:
        return _apply_search(r, hit[0], hit[1], hit[2])
    return r.finalise()


# ─────────────────────────────────────────────────────────────────────────────
# DEPENDENCY resolver
# ─────────────────────────────────────────────────────────────────────────────
DEPS_DEV_SYS = {"pypi": "PYPI", "npm": "NPM", "go": "GO", "maven": "MAVEN",
                "cargo": "CARGO", "nuget": "NUGET"}
ECOSYSTE_MS_REG = {"pypi": "pypi.org", "npm": "npmjs.org", "cargo": "crates.io",
                   "rubygems": "rubygems.org", "composer": "packagist.org",
                   "nuget": "nuget.org", "go": "proxy.golang.org", "hex": "hex.pm",
                   "conda": "anaconda.org", "cran": "cran.r-project.org",
                   "maven": "repo1.maven.org"}


def _deps_dev(name: str, sys_name: str, version: Optional[str]) -> Optional[Tuple[str, str]]:
    ver = version
    if not ver:
        pkg = get_json(f"https://api.deps.dev/v3/systems/{sys_name}/packages/"
                       + urllib.parse.quote(name, safe=""))
        default = next((v for v in (pkg or {}).get("versions", []) if v.get("isDefault")), None)
        ver = (default.get("versionKey") or {}).get("version") if default else None
    if not ver:
        return None
    vd = get_json(f"https://api.deps.dev/v3/systems/{sys_name}/packages/"
                  + urllib.parse.quote(name, safe="") + "/versions/"
                  + urllib.parse.quote(ver, safe=""))
    lics = (vd or {}).get("licenses") or []
    return (", ".join(lics), ver) if lics else None


def _ecosystems(name: str, reg: str) -> Optional[Tuple[str, str]]:
    data = get_json(f"https://packages.ecosyste.ms/api/v1/registries/{reg}/packages/"
                    + urllib.parse.quote(name, safe=""))
    if not data:
        return None
    norm = data.get("normalized_licenses") or []
    if norm:
        return ", ".join(norm), data.get("repository_url") or ""
    if data.get("licenses"):
        return str(data["licenses"]), data.get("repository_url") or ""
    return None


def _native_registry(name: str, eco: str, version: Optional[str]):
    if eco == "pypi":
        m = get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
        info = (m or {}).get("info") or {}
        cls = [c.split("::")[-1].strip() for c in info.get("classifiers", [])
               if c.startswith("License ::")]
        lic = "; ".join(cls) if cls else (info.get("license") or None)
        repo = github_from_text(json.dumps(info.get("project_urls") or {}) + (info.get("home_page") or ""))
        return (lic.strip()[:160] if lic else None, "PyPI", f"https://pypi.org/project/{name}/", repo)
    if eco == "npm":
        m = get_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@/')}")
        lic = (m or {}).get("license")
        if isinstance(lic, dict):
            lic = lic.get("type")
        repo = github_from_text(json.dumps((m or {}).get("repository") or {}) + ((m or {}).get("homepage") or ""))
        return (str(lic) if lic else None, "npm", f"https://www.npmjs.com/package/{name}", repo)
    if eco in ("rubygems", "gem"):
        m = get_json(f"https://rubygems.org/api/v1/gems/{urllib.parse.quote(name)}.json")
        lics = (m or {}).get("licenses") or []
        repo = github_from_text(((m or {}).get("source_code_uri") or "") + ((m or {}).get("homepage_uri") or ""))
        return (", ".join(lics) if lics else None, "RubyGems", f"https://rubygems.org/gems/{name}", repo)
    if eco in ("composer", "packagist"):
        m = get_json(f"https://repo.packagist.org/p2/{name}.json")
        vers = (((m or {}).get("packages") or {}).get(name)) or []
        chosen = next((v for v in vers if v.get("version") == version), None) or (vers[0] if vers else {})
        l = chosen.get("license") or []
        lic = ", ".join(l) if isinstance(l, list) and l else (l or None)
        repo = github_from_text(json.dumps(chosen.get("source") or {}))
        return (lic, "Packagist", f"https://packagist.org/packages/{name}", repo)
    if eco in ("cargo", "crates"):
        m = get_json(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")
        vers = (m or {}).get("versions") or []
        chosen = next((v for v in vers if v.get("num") == version), None) or (vers[0] if vers else {})
        repo = github_from_text(((m or {}).get("crate") or {}).get("repository") or "")
        return (chosen.get("license") or None, "crates.io", f"https://crates.io/crates/{name}", repo)
    if eco == "hex":
        m = get_json(f"https://hex.pm/api/packages/{urllib.parse.quote(name)}")
        lics = ((m or {}).get("meta") or {}).get("licenses") or []
        repo = github_from_text(json.dumps(((m or {}).get("meta") or {}).get("links") or {}))
        return (", ".join(lics) if lics else None, "Hex.pm", f"https://hex.pm/packages/{name}", repo)
    if eco == "conda":
        for ch in ("conda-forge", "anaconda", "bioconda"):
            m = get_json(f"https://api.anaconda.org/package/{ch}/{urllib.parse.quote(name)}")
            if m and m.get("license"):
                repo = github_from_text((m.get("dev_url") or "") + (m.get("home") or ""))
                return (m["license"], f"Anaconda ({ch})", f"https://anaconda.org/{ch}/{name}", repo)
        return (None, "Conda", None, None)
    if eco == "cran":
        m = get_json(f"https://crandb.r-pkg.org/{urllib.parse.quote(name)}")
        repo = github_from_text((m or {}).get("URL") or "")
        return ((m or {}).get("License") or None, "CRAN", f"https://cran.r-project.org/package={name}", repo)
    if eco == "maven":
        if ":" not in name:
            return (None, "Maven", None, None)
        group, artifact = name.split(":", 1)
        if not version:
            sr = get_json(f'https://search.maven.org/solrsearch/select?q=g:"{group}"+AND+a:"{artifact}"&rows=1&wt=json')
            docs = (((sr or {}).get("response") or {}).get("docs")) or []
            version = docs[0].get("latestVersion") if docs else None
        if not version:
            return (None, "Maven", None, None)
        pom = get_text(f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}/"
                       f"{version}/{artifact}-{version}.pom")
        lic, repo = None, None
        if pom:
            mm = re.search(r"<licenses>(.*?)</licenses>", pom, re.S)
            if mm:
                names = re.findall(r"<name>\s*(.*?)\s*</name>", mm.group(1), re.S)
                lic = ", ".join(n.strip() for n in names) if names else None
            repo = github_from_text(pom)
        return (lic, f"Maven Central {version}",
                f"https://central.sonatype.com/artifact/{group}/{artifact}", repo)
    if eco == "nuget":
        idl = name.lower()
        idx = (get_json(f"https://api.nuget.org/v3/registration5-gz-semver2/{idl}/index.json")
               or get_json(f"https://api.nuget.org/v3/registration5-semver1/{idl}/index.json"))
        lic, repo = None, None
        for page in (idx or {}).get("items", []):
            for it in page.get("items", []):
                e = it.get("catalogEntry") or {}
                lic = e.get("licenseExpression") or e.get("licenseUrl") or lic
                repo = repo or github_from_text(e.get("projectUrl") or "")
        return (lic, "NuGet", f"https://www.nuget.org/packages/{name}", repo)
    return None


def resolve_dependency(name: str, ecosystem: str = "pypi",
                       version: Optional[str] = None) -> LicenseResult:
    eco = ecosystem.lower()
    r = LicenseResult(component=f"{name}{('@'+version) if version else ''}",
                      component_type=f"dependency:{eco}")
    if eco in DEPS_DEV_SYS:
        dd = _deps_dev(name, DEPS_DEV_SYS[eco], version)
        if dd:
            r.declared, r.source = dd[0], f"deps.dev ({DEPS_DEV_SYS[eco]} {dd[1]})"
            r.log(r.source)
            return r.finalise()
        r.log("deps.dev: no license")
    if eco in ECOSYSTE_MS_REG:
        em = _ecosystems(name, ECOSYSTE_MS_REG[eco])
        if em and em[0]:
            r.declared, r.source = em[0], f"ecosyste.ms ({ECOSYSTE_MS_REG[eco]})"
            r.log(r.source)
            return r.finalise()
        r.log("ecosyste.ms: no license")
    nr = _native_registry(name, eco, version)
    repo = None
    if nr:
        lic, src, url, repo = nr
        if lic:
            r.declared, r.source, r.license_link = lic, src, url
            r.log(f"{src} license = {lic}")
            return r.finalise()
        r.log(f"{src}: no license field")
    if repo:
        spdx, link = github_license(repo)
        if spdx:
            r.detected, r.source = spdx, f"GitHub {repo}"
            r.license_link = r.license_link or link
            return r.finalise()
    hit = web_search_license(name, "library")
    if hit:
        return _apply_search(r, hit[0], hit[1], hit[2])
    return r.finalise()


# ─────────────────────────────────────────────────────────────────────────────
# Discovery — one URL → model + datasets + dependencies
# ─────────────────────────────────────────────────────────────────────────────
def parse_hf_input(text: str) -> Optional[Tuple[str, str]]:
    s = text.strip()
    if not s:
        return None
    if "huggingface.co" in s:
        path = urllib.parse.urlparse(s if s.startswith("http") else "https://" + s).path
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None
        if parts[0] == "datasets":
            rest = parts[1:3] if len(parts) >= 3 else parts[1:2]
            return ("dataset", "/".join(rest)) if rest else None
        if parts[0] == "models":
            rest = parts[1:3] if len(parts) >= 3 else parts[1:2]
            return ("model", "/".join(rest)) if rest else None
        if parts[0] in ("spaces", "organizations", "settings", "blog", "docs"):
            return None
        rest = parts[0:2] if len(parts) >= 2 else parts[0:1]
        return "model", "/".join(rest)
    if "/" in s and " " not in s and not s.startswith("http"):
        return "model", s
    return None


LIB_TO_PIP = {
    "sentence-transformers": "sentence-transformers", "transformers": "transformers",
    "diffusers": "diffusers", "timm": "timm", "spacy": "spacy", "flair": "flair",
    "fastai": "fastai", "keras": "keras", "stable-baselines3": "stable-baselines3",
    "sklearn": "scikit-learn", "scikit-learn": "scikit-learn", "open_clip": "open-clip-torch",
    "espnet": "espnet", "nemo": "nemo-toolkit", "speechbrain": "speechbrain",
    "peft": "peft", "adapter-transformers": "adapters", "setfit": "setfit",
    "pytorch": "torch", "tensorflow": "tensorflow", "jax": "jax", "onnx": "onnx",
}


def _deps_dev_direct(name: str) -> List[str]:
    pkg = get_json("https://api.deps.dev/v3/systems/PYPI/packages/" + urllib.parse.quote(name, safe=""))
    default = next((v for v in (pkg or {}).get("versions", []) if v.get("isDefault")), None)
    ver = (default.get("versionKey") or {}).get("version") if default else None
    if not ver:
        return []
    dd = get_json(f"https://api.deps.dev/v3/systems/PYPI/packages/{urllib.parse.quote(name, safe='')}"
                  f"/versions/{urllib.parse.quote(ver, safe='')}:dependencies")
    out = []
    for node in (dd or {}).get("nodes", []):
        if node.get("relation") == "DIRECT":
            nm = (node.get("versionKey") or {}).get("name")
            if nm:
                out.append(nm)
    return out


def discover_components(url: str, log=lambda m: None) -> Dict[str, Any]:
    parsed = parse_hf_input(url)
    if not parsed:
        return {"error": "Not a recognisable Hugging Face model/dataset URL.",
                "root_type": None, "model": None, "datasets": [], "dependencies": []}
    kind, cid = parsed
    if kind == "dataset":
        log(f"Input is a dataset: {cid}")
        return {"root_type": "dataset", "model": None, "datasets": [cid],
                "dependencies": [], "card": {}}
    log(f"Input is a model: {cid}")
    api = get_json(f"https://huggingface.co/api/models/{urllib.parse.quote(cid)}") or {}
    card = api.get("cardData") or {}

    datasets: List[str] = []
    ds = card.get("datasets")
    if isinstance(ds, str):
        ds = [ds]
    for d in (ds or []):
        if d:
            datasets.append(str(d))
    for t in api.get("tags", []):
        if isinstance(t, str) and t.startswith("dataset:"):
            datasets.append(t.split(":", 1)[1])
    seen = set()
    datasets = [d for d in datasets if not (d in seen or seen.add(d))]
    log(f"Found {len(datasets)} dataset(s).")

    dep_names: List[str] = []
    lib = card.get("library_name") or api.get("library_name")
    primary = LIB_TO_PIP.get(lib, lib) if lib else None
    if primary:
        dep_names.append(primary)
        log(f"Primary library: {primary}")
    req = get_text(f"https://huggingface.co/{cid}/raw/main/requirements.txt")
    if req:
        for line in req.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
            if m:
                dep_names.append(m.group(1))
        log("Parsed requirements.txt.")
    if primary:
        direct = _deps_dev_direct(primary)
        dep_names.extend(direct)
        if direct:
            log(f"Expanded {len(direct)} direct dependencies of {primary}.")
    seen, dep_unique = set(), []
    for d in dep_names:
        dl = d.lower()
        if dl not in seen:
            seen.add(dl); dep_unique.append(d)
    dep_unique = dep_unique[:30]
    log(f"Total dependencies to resolve: {len(dep_unique)}.")
    return {"root_type": "model", "model": cid, "datasets": datasets,
            "dependencies": [(d, "pypi") for d in dep_unique], "card": card, "_api": api}


# ─────────────────────────────────────────────────────────────────────────────
# Resolve-all orchestration
# ─────────────────────────────────────────────────────────────────────────────
def resolve_all(url: str, on_discovered=lambda s: None,
                on_result=lambda module, r: None, log=lambda m: None) -> Dict[str, Any]:
    disc = discover_components(url, log=log)
    on_discovered(disc)
    out = {"url": url, "model": None, "datasets": [], "dependencies": [], "error": disc.get("error")}
    if disc.get("error"):
        return out
    if disc.get("model"):
        log("Resolving model license…")
        mr = resolve_model(disc["model"], _api=disc.get("_api"))
        out["model"] = mr
        on_result("model", mr)
    for did in disc.get("datasets", []):
        log(f"Resolving dataset {did}…")
        dr = resolve_dataset(did)
        out["datasets"].append(dr)
        on_result("dataset", dr)
    for (name, eco) in disc.get("dependencies", []):
        log(f"Resolving dependency {name}…")
        pr = resolve_dependency(name, eco)
        out["dependencies"].append(pr)
        on_result("dependency", pr)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# GUI  (tkinter, single input)
# ─────────────────────────────────────────────────────────────────────────────
def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import threading, queue

    BG, SURF, CARD, BORDER = "#0E1726", "#172338", "#1F3050", "#2A3F63"
    ACCENT, TEXT, MUTED = "#5B8DEF", "#E8EDF4", "#8198B8"
    GREEN, AMBER, RED, BLUE = "#2DD4AA", "#F5A623", "#F8717A", "#60A5FA"
    CONF_COLOR = {C_VERIFIED: GREEN, C_TEXT: "#34D399", C_DECLARED: BLUE,
                  C_INHERITED: AMBER, C_SEARCH: "#FB923C", C_NONE: RED}

    state = {"results": []}
    q: "queue.Queue" = queue.Queue()

    root = tk.Tk()
    root.title("License Resolver")
    root.configure(bg=BG)
    root.geometry("1060x760")
    root.minsize(880, 600)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TFrame", background=BG)
    style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Head.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 16, "bold"))
    style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"), padding=8)
    style.map("Accent.TButton", background=[("!disabled", ACCENT), ("active", "#4878d6")],
              foreground=[("!disabled", "#0E1726")])
    style.configure("TButton", font=("Segoe UI", 10), padding=6)
    style.configure("Treeview", background=SURF, fieldbackground=SURF, foreground=TEXT,
                    rowheight=30, font=("Segoe UI", 10), borderwidth=0)
    style.configure("Treeview.Heading", background=CARD, foreground=MUTED,
                    font=("Segoe UI", 9, "bold"), relief="flat")
    style.map("Treeview", background=[("selected", CARD)])

    head = ttk.Frame(root); head.pack(fill="x", padx=22, pady=(18, 4))
    ttk.Label(head, text="License Resolver", style="Head.TLabel").pack(anchor="w")
    ttk.Label(head, text="Paste one Hugging Face link. It finds the model, its datasets and "
                         "dependencies, then resolves every license automatically.",
              style="Sub.TLabel").pack(anchor="w")

    panel = tk.Frame(root, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
    panel.pack(fill="x", padx=22, pady=12)
    prow = tk.Frame(panel, bg=SURF); prow.pack(fill="x", padx=16, pady=14)
    inp = tk.Entry(prow, bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 12))
    inp.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 12))
    inp.insert(0, "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2")
    run_btn = ttk.Button(prow, text="Resolve  →", style="Accent.TButton")
    run_btn.pack(side="left")

    wrap = tk.Frame(root, bg=BG); wrap.pack(fill="both", expand=True, padx=22, pady=(2, 4))
    cols = ("license", "confidence", "flag", "source")
    tree = ttk.Treeview(wrap, columns=cols, show="tree headings", selectmode="browse")
    tree.heading("#0", text="Component"); tree.column("#0", width=360, anchor="w")
    for c, (lbl, w) in {"license": ("License", 150), "confidence": ("Confidence", 120),
                        "flag": ("Flag", 160), "source": ("Source", 210)}.items():
        tree.heading(c, text=lbl); tree.column(c, width=w, anchor="w")
    vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vs.set)
    tree.pack(side="left", fill="both", expand=True); vs.pack(side="right", fill="y")
    for tier, col in CONF_COLOR.items():
        tree.tag_configure(tier, foreground=col)
    tree.tag_configure("low", background="#3A2A12")
    tree.tag_configure("group", font=("Segoe UI", 10, "bold"), foreground=TEXT)

    parents = {}

    def ensure_parents(disc):
        counts = {"model": 1 if disc.get("model") else 0,
                  "dataset": len(disc.get("datasets", [])),
                  "dependency": len(disc.get("dependencies", []))}
        for mod, label in (("model", "MODEL"), ("dataset", "DATASETS"),
                           ("dependency", "DEPENDENCIES")):
            pid = tree.insert("", "end", text=f"{label}  ({counts[mod]})", open=True,
                              values=("", "", "", ""), tags=("group",))
            parents[mod] = pid

    bottom = tk.Frame(root, bg=BG); bottom.pack(fill="x", padx=22, pady=(0, 6))
    status = tk.Label(bottom, text="Ready.", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w")
    status.pack(side="left")
    ttk.Button(bottom, text="Export CSV", command=lambda: export("csv")).pack(side="right", padx=(8, 0))
    ttk.Button(bottom, text="Export JSON", command=lambda: export("json")).pack(side="right")
    ttk.Button(bottom, text="Clear", command=lambda: clear_all()).pack(side="right", padx=(0, 8))

    logbox = tk.Text(root, height=5, bg="#0A1120", fg=MUTED, relief="flat",
                     font=("Consolas", 9), padx=10, pady=6)
    logbox.pack(fill="x", padx=22, pady=(0, 14))
    logbox.insert("1.0", "Log:\n"); logbox.config(state="disabled")

    def ui_log(m):
        logbox.config(state="normal"); logbox.insert("end", m + "\n")
        logbox.see("end"); logbox.config(state="disabled")

    def add_result_row(module, r: LicenseResult):
        flag, tags = "", [r.confidence]
        if r.low_confidence:
            flag = "⚠ LOW — " + r.low_confidence_reason; tags.append("low")
        elif any("DRIFT" in n for n in r.notes):
            flag = "⚠ drift"
        state["results"].append((module, r))
        tree.insert(parents.get(module, ""), "end", text="   " + r.component,
                    values=(r.license or "—", r.confidence, flag, r.source or "—"), tags=tags)

    def refresh_status():
        rs = [r for _, r in state["results"]]
        low = sum(1 for r in rs if r.low_confidence)
        status.config(text=f"{len(rs)} components resolved · {low} low-confidence (flagged)")

    def worker(url):
        try:
            resolve_all(url, on_discovered=lambda d: q.put(("disc", d)),
                        on_result=lambda mod, r: q.put(("res", mod, r)),
                        log=lambda m: q.put(("log", m)))
            q.put(("done",))
        except Exception as e:
            q.put(("err", str(e)))

    def poll():
        try:
            while True:
                msg = q.get_nowait()
                if msg[0] == "disc":
                    if msg[1].get("error"):
                        ui_log("Error: " + msg[1]["error"])
                    else:
                        ensure_parents(msg[1])
                elif msg[0] == "res":
                    add_result_row(msg[1], msg[2]); refresh_status()
                elif msg[0] == "log":
                    ui_log(msg[1])
                elif msg[0] == "done":
                    run_btn.config(state="normal"); ui_log("Done.")
                elif msg[0] == "err":
                    run_btn.config(state="normal"); ui_log("Error: " + msg[1])
        except queue.Empty:
            pass
        root.after(120, poll)

    def run():
        url = inp.get().strip()
        if not url:
            return
        clear_all()
        run_btn.config(state="disabled")
        ui_log(f"Resolving {url}")
        threading.Thread(target=worker, args=(url,), daemon=True).start()
    run_btn.config(command=run)
    inp.bind("<Return>", lambda e: run())

    def clear_all():
        state["results"].clear(); parents.clear()
        for i in tree.get_children():
            tree.delete(i)
        status.config(text="Ready.")

    def show_detail(event):
        sel = tree.selection()
        if not sel:
            return
        comp = tree.item(sel[0], "text").strip()
        match = next((r for _, r in state["results"] if r.component == comp), None)
        if not match:
            return
        win = tk.Toplevel(root); win.title(match.component); win.configure(bg=BG); win.geometry("720x560")
        txt = tk.Text(win, bg=SURF, fg=TEXT, relief="flat", wrap="word",
                      font=("Consolas", 10), padx=14, pady=14)
        txt.pack(fill="both", expand=True, padx=12, pady=12)
        lines = [f"Component   : {match.component}", f"Type        : {match.component_type}",
                 f"License     : {match.license or '—'}", f"Confidence  : {match.confidence}",
                 f"Low conf?   : {'YES — ' + match.low_confidence_reason if match.low_confidence else 'no'}",
                 f"Declared    : {match.declared or '—'}", f"Detected    : {match.detected or '—'}",
                 f"Source      : {match.source or '—'}", f"Link        : {match.license_link or '—'}",
                 "", "── Notes ──"] + (["• " + n for n in match.notes] or ["(none)"]) + \
                ["", "── Resolution trail ──"] + ["→ " + t for t in match.trail]
        txt.insert("1.0", "\n".join(lines)); txt.config(state="disabled")
    tree.bind("<Double-1>", show_detail)

    def grouped_payload():
        g = {"model": [], "datasets": [], "dependencies": []}
        for mod, r in state["results"]:
            key = "model" if mod == "model" else "datasets" if mod == "dataset" else "dependencies"
            g[key].append({**asdict(r), "low_confidence": r.low_confidence,
                           "low_confidence_reason": r.low_confidence_reason})
        return g

    def export(kind):
        if not state["results"]:
            messagebox.showinfo("Export", "Nothing to export yet."); return
        if kind == "json":
            path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"url": inp.get().strip(), "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "components": grouped_payload()}, f, indent=2)
        else:
            path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
            if not path:
                return
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["module", "component", "license", "confidence", "low_confidence",
                            "reason", "source", "declared", "detected", "link"])
                for mod, r in state["results"]:
                    w.writerow([mod, r.component, r.license, r.confidence,
                                "YES" if r.low_confidence else "NO", r.low_confidence_reason,
                                r.source, r.declared, r.detected, r.license_link])
        status.config(text=f"Exported to {path}")

    poll()
    root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
def _headless(url: str):
    def log(m): print("·", m, file=sys.stderr)
    out = resolve_all(url, log=log)
    payload = {"url": url, "model": None, "datasets": [], "dependencies": []}
    if out.get("model"):
        payload["model"] = {**asdict(out["model"]), "low_confidence": out["model"].low_confidence}
    payload["datasets"] = [{**asdict(r), "low_confidence": r.low_confidence} for r in out["datasets"]]
    payload["dependencies"] = [{**asdict(r), "low_confidence": r.low_confidence} for r in out["dependencies"]]
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    if "--headless" in sys.argv:
        url = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
        if url:
            ensure_dependencies(lambda m: print("·", m, file=sys.stderr))
            _headless(url)
    else:
        ensure_dependencies()
        launch_gui()
