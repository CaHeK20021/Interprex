"""Parser for i18n formats (Stardew Valley i18n JSON and RimWorld Languages XML)."""

from __future__ import annotations

import os
import json
import xml.etree.ElementTree as ET
from .base import BaseParser, TranslationString, make_id

# Mappings from user-selected languages (codes or full names) to RimWorld
# Languages/ folder names. These MUST match the base game's .tar names
# (Data/Core/Languages/<name>.tar) — the engine loads by exact folder name.
RIMWORLD_LANGS = {
    "russian": "Russian (\u0420\u0443\u0441\u0441\u043a\u0438\u0439)",
    "ru": "Russian (\u0420\u0443\u0441\u0441\u043a\u0438\u0439)",
    "german": "German (Deutsch)",
    "de": "German (Deutsch)",
    "spanish": "Spanish (Espa\u00f1ol(Castellano))",
    "es": "Spanish (Espa\u00f1ol(Castellano))",
    "french": "French (Fran\u00e7ais)",
    "fr": "French (Fran\u00e7ais)",
    "italian": "Italian (Italiano)",
    "it": "Italian (Italiano)",
    "chinese": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "chinese (simplified)": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "chinesesimplified": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "zh": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "chinesetraditional": "ChineseTraditional (\u7e41\u9ad4\u4e2d\u6587)",
    "japanese": "Japanese (\u65e5\u672c\u8a9e)",
    "ja": "Japanese (\u65e5\u672c\u8a9e)",
    "korean": "Korean (\ud55c\uad6d\uc5b4)",
    "ko": "Korean (\ud55c\uad6d\uc5b4)",
    "polish": "Polish (Polski)",
    "pl": "Polish (Polski)",
    "portuguese": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "portuguese (brazil)": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "portuguesebrazilian": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "pt": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "turkish": "Turkish (T\u00fcrk\u00e7e)",
    "tr": "Turkish (T\u00fcrk\u00e7e)",
    "ukrainian": "Ukrainian (\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430)",
    "uk": "Ukrainian (\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430)",
    "czech": "Czech (\u010ce\u0161tina)",
    "cs": "Czech (\u010ce\u0161tina)",
    "dutch": "Dutch (Nederlands)",
    "nl": "Dutch (Nederlands)",
    "danish": "Danish (Dansk)",
    "da": "Danish (Dansk)",
    "finnish": "Finnish (Suomi)",
    "fi": "Finnish (Suomi)",
    "hungarian": "Hungarian (Magyar)",
    "hu": "Hungarian (Magyar)",
    "spanishlatin": "SpanishLatin (Espa\u00f1ol(Latinoam\u00e9rica))",
    "spanish (latin american)": "SpanishLatin (Espa\u00f1ol(Latinoam\u00e9rica))",
}

STARDEW_LANGS = {
    "russian": "ru",
    "ru": "ru",
    "german": "de",
    "de": "de",
    "spanish": "es",
    "es": "es",
    "french": "fr",
    "fr": "fr",
    "italian": "it",
    "it": "it",
    "chinese": "zh",
    "chinese (simplified)": "zh",
    "chinesesimplified": "zh",
    "chinesetraditional": "zh",
    "zh": "zh",
    "japanese": "ja",
    "ja": "ja",
    "korean": "ko",
    "ko": "ko",
    "polish": "pl",
    "pl": "pl",
    "portuguese": "pt",
    "portuguese (brazil)": "pt",
    "portuguesebrazilian": "pt",
    "pt": "pt",
    "turkish": "tr",
    "tr": "tr",
    "ukrainian": "uk",
    "uk": "uk",
}


def get_rimworld_folder(target: str) -> str:
    clean = target.strip().lower()
    return RIMWORLD_LANGS.get(clean, target)


def is_rimworld_mod(root: str) -> bool:
    """A RimWorld mod is uniquely identified by About/About.xml (<ModMetaData>) —
    every Workshop/local mod has one, and no other engine ships that marker. This
    is the single source of truth so a mod with ONLY Defs/ or ONLY a .dll (no
    Languages/ yet) is still recognized as RimWorld (engine i18n), instead of
    falling through to unity/csharp. Used by detect() here AND by csharp/unity to
    yield. Checks root + one level of version subfolders (1.5/About, 1.6/About)."""
    import xml.etree.ElementTree as _ET

    def _has_modmetadata(about_xml: str) -> bool:
        if not os.path.isfile(about_xml):
            return False
        try:
            # utf-8-sig: About.xml commonly ships with a BOM.
            with open(about_xml, "r", encoding="utf-8-sig") as f:
                return _ET.parse(f).getroot().tag == "ModMetaData"
        except Exception:
            # A malformed About.xml is still a strong RimWorld signal.
            return True

    for about_rel in ("About/About.xml", "about/About.xml", "About/about.xml"):
        if _has_modmetadata(os.path.join(root, *about_rel.split("/"))):
            return True
    # Version subfolders (1.6/About/About.xml etc.).
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and entry.name not in _SKIP_DIRS:
                if _has_modmetadata(os.path.join(entry.path, "About", "About.xml")):
                    return True
    except OSError:
        pass
    return False


_SKIP_DIRS = frozenset({
    "About", "Source", "Defs", "Assemblies", "Textures",
    "Patches", "Sound", "Meshes", "UI", "bin", "obj",
    ".git", ".vs", "node_modules", "venv", ".interprex_backups",
})


def _find_rimworld_lang_dirs(root: str) -> list[tuple[str, str]]:
    """Find all Languages/<lang> directories under root (including inside
    version subfolders like 1.5/, 1.6/). Returns list of (lang_dir_path, lang_name)
    e.g. (".1.6/Languages/English", "English")."""
    results: list[tuple[str, str]] = []

    def _scan_languages(langs_dir: str) -> None:
        try:
            for entry in os.scandir(langs_dir):
                if entry.is_dir():
                    results.append((entry.path, entry.name))
        except OSError:
            pass

    # 1. Root level
    root_langs = os.path.join(root, "Languages")
    if os.path.isdir(root_langs):
        _scan_languages(root_langs)

    # 2. One level of subdirs (version folders: 1.0, 1.5, 1.6 etc.)
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and entry.name not in _SKIP_DIRS:
                langs = os.path.join(entry.path, "Languages")
                if os.path.isdir(langs):
                    _scan_languages(langs)
    except OSError:
        pass

    return results


def _find_rimworld_versions(root: str) -> list[str]:
    """Find available RimWorld version folders (1.0-1.6+). Returns sorted
    list newest-first, e.g. ["1.6", "1.5", "1.4"]. Empty if no versions
    found (mod uses root-level Languages/ only)."""
    import re
    versions: list[tuple[float, str]] = []
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and entry.name not in _SKIP_DIRS:
                langs = os.path.join(entry.path, "Languages")
                if os.path.isdir(langs):
                    m = re.match(r'^(\d+\.\d+)$', entry.name)
                    if m:
                        versions.append((float(m.group(1)), entry.name))
    except OSError:
        pass
    versions.sort(key=lambda v: -v[0])
    return [v[1] for v in versions]


def _rimworld_has_lang(root: str, target_lang: str) -> bool:
    """True if Languages/<target_lang>/ (short or long form) exists in the mod.
    Matches both directions: mod has long form and we check short, or vice versa."""
    target = RIMWORLD_LANGS.get(target_lang.strip().lower(), target_lang)
    # Extract the short code before "(" for bidirectional matching
    target_short = target.split(" (")[0] if " (" in target else target
    for _, name in _find_rimworld_lang_dirs(root):
        if name == target or name == target_short:
            return True
        name_short = name.split(" (")[0] if " (" in name else name
        if name_short == target_short:
            return True
    return False


def _find_rimworld_english_dirs(root: str) -> list[str]:
    """Find Languages/English directories under root. If version subfolders
    exist (1.5/, 1.6/), returns ONLY the latest version's dirs.
    Falls back to root-level Languages/English if no versioned dirs found."""
    all_langs = _find_rimworld_lang_dirs(root)
    english_dirs = [(path, name) for path, name in all_langs if name == "English"]

    # Group by whether they're in a version subfolder
    import re
    versioned: list[tuple[float, str]] = []
    root_dirs: list[str] = []
    for path, _ in english_dirs:
        # Check if path contains a version folder like /1.6/Languages/English
        parts = os.path.normpath(path).split(os.sep)
        found_version = False
        for part in parts:
            m = re.match(r'^(\d+\.\d+)$', part)
            if m:
                versioned.append((float(m.group(1)), path))
                found_version = True
                break
        if not found_version:
            root_dirs.append(path)

    if versioned:
        # Return only the latest version
        versioned.sort(key=lambda v: -v[0])
        latest = versioned[0][0]
        return [p for ver, p in versioned if ver == latest]

    # No versioned dirs — use root-level
    seen: set[str] = set()
    unique: list[str] = []
    for p in root_dirs:
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# RimWorld: generate DefInjected from Defs/
#
# Most mods ship NO English DefInjected — the translatable text (label,
# description, work reports, letters, abilities, ...) lives directly in
# Defs/*.xml. We walk each Def and synthesize the engine's DefInjected key
# `<defName.fieldPath>` so those strings can be translated. RimWorld does NOT
# crash on unknown/extra DefInjected keys (it logs a warning), so a tight
# field whitelist + exact paths is safe.
# ---------------------------------------------------------------------------

# Translatable leaf field names (case-sensitive). Derived from the real
# frequency of leaf tags across 5730 shipped DefInjected files in the user's
# mod library. A field is emitted ONLY if its leaf tag is here — everything
# else (defName, thingClass, texPath, statBases numbers, ...) is never emitted.
# Adding a name only widens coverage; it never changes an existing id (the id
# depends on file + dotted key + text, and the key already contains the field).
#
# ⚠️ NEVER add grammar/RulePack fields here. `rulesStrings`, `rulesHidden`,
# `rules`, `rep`, `trans`, `untranslatedRules`, `compClass`-style symbol fields
# hold the engine's GRAMMAR SYNTAX (`keyword->value`, `[TAG]`, `(p=weight)`),
# NOT display text. Translating them corrupts RimWorld's GrammarResolver, which
# then throws on EVERY text resolution and the WHOLE UI falls back to English
# (real bug: tabs/research/build menu went English, grammar "Bad string pass").
# Translatable fields are user-visible captions ONLY.
_RIMWORLD_TRANSLATABLE_FIELDS = frozenset({
    "label", "description", "reportString", "jobString", "verb", "gerund",
    "tip", "jobReportString", "labelNoun", "helpText", "GizmoLabel",
    "ResearchLabel", "ResearchDesc", "ResearchDescDisc", "groupingLabel",
    "stuffAdjective", "labelPlural", "useLabel", "headerTip",
    "letterText", "letterLabel", "labelSocial", "labelFemale", "labelMale",
    "labelFemalePlural", "labelMalePlural", "ingestCommandString",
    "ingestReportString", "deathMessage", "fixedName", "pawnLabel",
    "baseInspectLine", "customLabel", "labelShort",
    "skillLabel", "summary", "title", "beginLetterLabel",
    "beginLetter", "endMessage", "recoveryMessage", "deflationMessage",
    "calledOffMessage", "gerundLabel", "pawnsPlural", "pawnLabelPlural",
    "successfullyRemovedHediffMessage", "StepLabel", "StepDesc",
    "ProjTypeLabel", "useLabelNoun",
    "ritualExplanation", "extraTooltip", "overrideLabel", "labelTip",
    "onMapInstruction", "approachOrderString", "approachingReportString",
    "spectatorsLabel", "spectateLeadJobString",
})


def _looks_translatable(text: str) -> bool:
    """Second safety net behind the whitelist: skip values that are NOT plain
    display text. Critically this rejects RimWorld GRAMMAR/RulePack syntax — a
    field may be whitelisted yet hold a grammar rule (e.g. a `<rules>` list, or a
    caption that is actually a `keyword->value` rule). Translating those corrupts
    GrammarResolver and the whole UI falls back to English."""
    import re
    t = text.strip()
    if not t:
        return False
    if t.lower() in ("true", "false"):
        return False
    if re.match(r'^-?\d+(\.\d+)?$', t):
        return False
    # Grammar rule: "keyword->value" (the RulePack/rulesStrings syntax). The "->"
    # IS the rule separator; never translate these.
    if "->" in t:
        return False
    # A value that is ENTIRELY a single grammar symbol/tag, e.g. "[INITIATOR_label]"
    # or "(p=2)" — these are interpolation tokens, not prose.
    if re.fullmatch(r'\[[^\]]+\]', t) or re.fullmatch(r'\([^)]*\)', t):
        return False
    return True


def _find_rimworld_defs_dirs(root: str) -> list[str]:
    """Find Defs/ directories under root. If version subfolders exist (1.5/,
    1.6/), returns ONLY the latest version's dirs. Falls back to root-level
    Defs/ if no versioned dirs found. Mirrors _find_rimworld_english_dirs."""
    import re
    defs_dirs: list[str] = []

    # 1. Root level Defs/
    root_defs = os.path.join(root, "Defs")
    if os.path.isdir(root_defs):
        defs_dirs.append(root_defs)

    # 2. One level of version subfolders (1.0, 1.5, 1.6, ...). _SKIP_DIRS
    #    contains "Defs" but it's only applied to the version-folder name here,
    #    so it doesn't block the root-level Defs check above.
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and entry.name not in _SKIP_DIRS:
                d = os.path.join(entry.path, "Defs")
                if os.path.isdir(d):
                    defs_dirs.append(d)
    except OSError:
        pass

    # Version selection: if any are inside a version folder, keep only latest.
    versioned: list[tuple[float, str]] = []
    root_dirs: list[str] = []
    for path in defs_dirs:
        parts = os.path.normpath(path).split(os.sep)
        found_version = False
        for part in parts:
            m = re.match(r'^(\d+\.\d+)$', part)
            if m:
                versioned.append((float(m.group(1)), path))
                found_version = True
                break
        if not found_version:
            root_dirs.append(path)

    if versioned:
        versioned.sort(key=lambda v: -v[0])
        latest = versioned[0][0]
        return [p for ver, p in versioned if ver == latest]

    seen: set[str] = set()
    unique: list[str] = []
    for p in root_dirs:
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            unique.append(p)
    return unique


def _walk_def_element(el, prefix_path: list[str], out: list[tuple[list[str], str]]) -> None:
    """Recursively collect (field_path_segments, text) for translatable leaves
    inside a Def element. prefix_path is the dotted path so far (after defName).

    - whitelisted leaf  -> emit (prefix + [tag], text)
    - <li> list:
        * plain-string li -> emit (prefix + [tag, i], li.text) only if tag is
          whitelisted (e.g. rulesStrings.0, stats.0)
        * object li       -> recurse with (prefix + [tag, i])
    - generic container  -> recurse with (prefix + [tag])
    - everything else    -> skip
    """
    for child in el:
        tag = child.tag
        if not isinstance(tag, str) or tag == "defName":
            continue

        # Children that are real elements (ignore comments/PIs).
        elem_children = [c for c in child if isinstance(c.tag, str)]
        li_children = [c for c in elem_children if c.tag == "li"]

        if li_children:
            # A list. Index by position among <li> children, document order.
            for i, li in enumerate(li_children):
                li_elem_children = [c for c in li if isinstance(c.tag, str)]
                if li_elem_children:
                    # Object li -> recurse.
                    _walk_def_element(li, prefix_path + [tag, str(i)], out)
                else:
                    # Plain-string li -> only emit for whitelisted list fields.
                    if tag in _RIMWORLD_TRANSLATABLE_FIELDS and li.text and _looks_translatable(li.text):
                        out.append((prefix_path + [tag, str(i)], li.text))
        elif elem_children:
            # Generic container (e.g. <comps><CompProperties_X>...) -> recurse.
            _walk_def_element(child, prefix_path + [tag], out)
        else:
            # Leaf.
            if tag in _RIMWORLD_TRANSLATABLE_FIELDS and child.text and _looks_translatable(child.text):
                out.append((prefix_path + [tag], child.text))


def _definjected_rel_dir(defs_dir: str, root: str) -> str:
    """Given an absolute Defs/ directory, return the DefInjected output directory
    RELATIVE TO ROOT, mirroring the mod's own layout. E.g.
      <root>/1055485938/1.6/Defs        -> "1055485938/1.6/Languages/English/DefInjected"
      <root>/MyMod/Defs                 -> "MyMod/Languages/English/DefInjected"
      <root>/Defs                       -> "Languages/English/DefInjected"
    The path PREFIX before "Defs" (mod folder + optional version) is preserved so
    the generated string's `file` resolves to the right mod (the table filter and
    inject both key off this), exactly like curated English files do."""
    rel = os.path.relpath(defs_dir, root).replace("\\", "/")
    # Strip the trailing "Defs" segment, keep everything before it as the prefix.
    parts = rel.split("/")
    if parts and parts[-1].lower() == "defs":
        parts = parts[:-1]
    prefix = "/".join(parts)
    if prefix:
        prefix += "/"
    return prefix + "Languages/English/DefInjected"


def _iter_generated_definjected(base_path: str, root: str):
    """Single source of truth for Defs->DefInjected generation, shared by
    extract, inject, and count so the id math is guaranteed identical.

    Yields (synthetic_file, def_type, key, original) where:
      - synthetic_file = "<verPrefix>Languages/English/DefInjected/<DefType>/<basename>.xml"
        (forward slashes, relative to root) — mirrors the curated English layout
        so inject's existing Languages/English -> Languages/<lang> rewrite works.
      - def_type = the Def's XML tag (ThingDef, PawnKindDef, ...)
      - key = "<defName>.<fieldPath>"
      - original = the leaf text
    """
    for defs_dir in _find_rimworld_defs_dirs(base_path):
        definjected_rel = _definjected_rel_dir(defs_dir, root)
        for dirpath, _, filenames in os.walk(defs_dir):
            for filename in filenames:
                if not filename.lower().endswith(".xml"):
                    continue
                abspath = os.path.join(dirpath, filename)
                try:
                    tree = ET.parse(abspath)
                    defs_root = tree.getroot()
                except Exception as e:
                    print(f"Error reading RimWorld Defs {filename}: {e}")
                    continue

                basename = filename

                for def_el in defs_root:
                    def_type = def_el.tag
                    if not isinstance(def_type, str):
                        continue
                    # Skip abstract templates and defs without a defName.
                    if (def_el.get("Abstract") or "").strip().lower() == "true":
                        continue
                    defname_el = def_el.find("defName")
                    if defname_el is None or not defname_el.text or not defname_el.text.strip():
                        continue
                    def_name = defname_el.text.strip()

                    collected: list[tuple[list[str], str]] = []
                    _walk_def_element(def_el, [], collected)
                    if not collected:
                        continue

                    synthetic_file = f"{definjected_rel}/{def_type}/{basename}"
                    for field_segments, text in collected:
                        key = ".".join([def_name] + field_segments)
                        yield (synthetic_file, def_type, key, text)


def _dedup_field_key(key: str) -> str:
    """Normalize a DefInjected key for DEDUP comparison only (not for the id).
    RimWorld matches a translation key case-insensitively on the FIELD path but
    the defName is an identifier. Authors sometimes write the field in a different
    case (`MakeX.jobstring` vs the def's `jobString`) — RimWorld accepts both, so
    a dup there still crashes. Keep the defName (first segment) verbatim, lower the
    rest (fields + numeric indices). Used ONLY to compare against author keys."""
    parts = key.split(".")
    if len(parts) <= 1:
        return key
    return parts[0] + "." + ".".join(p.lower() for p in parts[1:])


def _normalize_def_type(def_type: str) -> str:
    """Normalize a DefInjected folder/def-type name for matching. RimWorld matches
    a translation to a def by type, and folder names vary between authors and us:
    the engine treats the trailing plural `s` as equivalent (`ThingDefs` folder
    holds `ThingDef` translations). Measured in real mods: an author ships
    `RecipeDefs/` (plural) while we generate `RecipeDef/` (singular, from the XML
    tag). Without this, dedup misses the author's key and RimWorld crashes with
    "A translation for X already exists". Lower-case + strip one trailing `s` from
    a `*defs` name. Keep the type (don't dedup by bare key) so different types with
    the same defName (ThingDef "Seal" vs RecipeDef "Seal") stay distinct."""
    dt = def_type.lower().strip()
    if dt.endswith("defs"):
        return dt[:-1]
    return dt


def _find_rimworld_target_dirs(root: str, target_lang: str) -> list[str]:
    """Find Languages/<target_lang>/ dirs (case-insensitive, version-filtered to
    latest). Mirrors _find_rimworld_english_dirs but for the user's target. Matches
    both folder forms: long "Russian (Русский)" and short "Russian", any case."""
    import re
    target = get_rimworld_folder(target_lang)
    target_short = target.split(" (")[0] if " (" in target else target
    target_lower = target.lower()
    target_short_lower = target_short.lower()

    target_dirs: list[str] = []
    for path, name in _find_rimworld_lang_dirs(root):
        name_lower = name.lower()
        name_short_lower = (name.split(" (")[0] if " (" in name else name).lower()
        if (name_lower == target_lower or name_lower == target_short_lower
                or name_short_lower == target_short_lower):
            target_dirs.append(path)

    versioned: list[tuple[float, str]] = []
    root_dirs: list[str] = []
    for path in target_dirs:
        parts = os.path.normpath(path).split(os.sep)
        found = False
        for part in parts:
            m = re.match(r'^(\d+\.\d+)$', part)
            if m:
                versioned.append((float(m.group(1)), path))
                found = True
                break
        if not found:
            root_dirs.append(path)
    if versioned:
        versioned.sort(key=lambda v: -v[0])
        latest = versioned[0][0]
        return [p for v, p in versioned if v == latest]
    return root_dirs


def _target_definjected_keys(base_path: str, target_lang: str) -> set:
    """Set of (_normalize_def_type(folder), key) already present in the target
    language's DefInjected — author's existing translation we must NOT duplicate."""
    keys: set = set()
    for target_dir in _find_rimworld_target_dirs(base_path, target_lang):
        definjected = os.path.join(target_dir, "DefInjected")
        if not os.path.isdir(definjected):
            continue
        for dirpath, _, filenames in os.walk(definjected):
            def_type = _normalize_def_type(os.path.basename(dirpath))
            for filename in filenames:
                if not filename.lower().endswith(".xml"):
                    continue
                try:
                    tree = ET.parse(os.path.join(dirpath, filename))
                    for child in tree.getroot():
                        if isinstance(child.tag, str):
                            keys.add((def_type, _dedup_field_key(child.tag)))
                except Exception:
                    pass
    return keys


def _target_keyed_keys(base_path: str, target_lang: str) -> set:
    """Set of Keyed keys already present in the target language's Keyed/."""
    keys: set = set()
    for target_dir in _find_rimworld_target_dirs(base_path, target_lang):
        keyed = os.path.join(target_dir, "Keyed")
        if not os.path.isdir(keyed):
            continue
        for dirpath, _, filenames in os.walk(keyed):
            for filename in filenames:
                if not filename.lower().endswith(".xml"):
                    continue
                try:
                    tree = ET.parse(os.path.join(dirpath, filename))
                    for child in tree.getroot():
                        if isinstance(child.tag, str):
                            keys.add(child.tag)
                except Exception:
                    pass
    return keys


def _english_definjected_keys(base_path: str) -> set:
    """Set of (DefType, key) already present in curated Languages/English/
    DefInjected — used to suppress generated duplicates (curated English wins).
    DefType = the immediate parent directory of the DefInjected xml."""
    keys: set = set()
    for english_dir in _find_rimworld_english_dirs(base_path):
        definjected = os.path.join(english_dir, "DefInjected")
        if not os.path.isdir(definjected):
            continue
        for dirpath, _, filenames in os.walk(definjected):
            def_type = os.path.basename(dirpath)
            for filename in filenames:
                if not filename.lower().endswith(".xml"):
                    continue
                try:
                    tree = ET.parse(os.path.join(dirpath, filename))
                    for child in tree.getroot():
                        if isinstance(child.tag, str):
                            keys.add((def_type, child.tag))
                except Exception:
                    pass
    return keys


def count_generated_definjected(root: str, target_lang: str | None = None) -> int:
    """Count translatable strings generated from Defs/ after dedup vs curated
    English DefInjected AND vs the target language's existing (author) DefInjected.
    Used by main.py count_mod_strings — so a partially-translated mod reports only
    the MISSING strings, not its already-translated ones."""
    try:
        english_keys = _english_definjected_keys(root)
        target_keys = _target_definjected_keys(root, target_lang) if target_lang else set()
        seen: set = set()
        count = 0
        for synthetic_file, def_type, key, original in _iter_generated_definjected(root, root):
            if (def_type, key) in english_keys:
                continue
            if (_normalize_def_type(def_type), _dedup_field_key(key)) in target_keys:
                continue
            sid = make_id("i18n", synthetic_file, [key], original)
            if sid in seen:
                continue
            seen.add(sid)
            count += 1
        return count
    except Exception:
        return 0


def get_stardew_code(target: str) -> str:
    clean = target.strip().lower()
    if clean in STARDEW_LANGS:
        return STARDEW_LANGS[clean]
    # Fallback: first two characters
    return clean[:2]


def flatten_json(d: dict, current_path: list[str] = None) -> list[tuple[list[str], str]]:
    if current_path is None:
        current_path = []
    items = []
    for k, v in d.items():
        new_path = current_path + [k]
        if isinstance(v, dict):
            items.extend(flatten_json(v, new_path))
        elif isinstance(v, str):
            items.append((new_path, v))
    return items


def set_by_path(d: dict, path: list[str], value: str) -> None:
    for step in path[:-1]:
        d = d.setdefault(step, {})
    d[path[-1]] = value


def _strip_json_comments(text: str) -> str:
    """Strip // and /* */ comments + trailing commas from JSON text.
    Stardew Valley i18n files commonly use these non-standard features."""
    import re
    # Remove single-line // comments (but not inside strings)
    result = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        c = text[i]
        if escape:
            result.append(c)
            escape = False
            i += 1
            continue
        if c == '\\' and in_string:
            result.append(c)
            escape = True
            i += 1
            continue
        if c == '"' and not escape:
            in_string = not in_string
            result.append(c)
            i += 1
            continue
        if not in_string and c == '/' and i + 1 < len(text) and text[i + 1] == '/':
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        if not in_string and c == '/' and i + 1 < len(text) and text[i + 1] == '*':
            i += 2
            while i + 1 < len(text) and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        result.append(c)
        i += 1
    text = ''.join(result)
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


class I18nParser(BaseParser):
    engine = "i18n"

    def engine_prompt_addon(self) -> str:
        return (
            "LOCALIZATION FILE STRINGS: these strings come from a JSON/INI locale file "
            "and are displayed in menus, HUD, or system messages.\n"
            "FORMAT SPECIFIERS: preserve {0}, {1}, {player}, %s, %d and similar "
            "patterns EXACTLY — they are filled in at runtime.\n"
            "ESCAPE SEQUENCES: keep literal \\n and \\t as-is inside strings.\n"
            "UI TAB/BUTTON LABELS: if a string's context says it is a UI tab or button "
            "label, the on-screen width is very limited — translate it as SHORT as "
            "possible (ideally 1-2 words), even if that means a looser wording.\n"
            "TONE: use a neutral, professional register. Avoid overly literary style."
        )

    @staticmethod
    def detect(root: str) -> bool:
        # 1. Stardew Valley i18n
        stardew_default = os.path.join(root, "i18n", "default.json")
        if os.path.isfile(stardew_default):
            return True

        # 2. RimWorld — identified by About/About.xml (the one reliable marker).
        #    Covers mods with only Defs/ or only a .dll and no Languages/ yet.
        if is_rimworld_mod(root):
            return True

        # 3. RimWorld Languages — any language, root or version subfolders
        #    (kept for non-mod folders that lack an About.xml).
        if _find_rimworld_lang_dirs(root):
            return True

        return False

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        results: list[TranslationString] = []

        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]

        for base_path in paths_to_check:
            # 1. Extract Stardew JSON
            stardew_default = os.path.join(base_path, "i18n", "default.json")
            if os.path.isfile(stardew_default):
                rel_path = os.path.relpath(stardew_default, root).replace("\\", "/")
                try:
                    with open(stardew_default, "r", encoding="utf-8-sig") as f:
                        raw = f.read()
                    data = json.loads(_strip_json_comments(raw))
                    if isinstance(data, dict):
                        for path, val in flatten_json(data):
                            results.append(self._mk(rel_path, path, val, "Stardew Valley i18n"))
                except Exception as e:
                    print(f"Error reading Stardew i18n default.json: {e}")

            # 2. Extract RimWorld XMLs — root + version subfolders
            rimworld_english_dirs = _find_rimworld_english_dirs(base_path)
            seen_ids: set[str] = set()
            for rimworld_english in rimworld_english_dirs:
                for dirpath, _, filenames in os.walk(rimworld_english):
                    for filename in filenames:
                        if filename.endswith(".xml"):
                            abspath = os.path.join(dirpath, filename)
                            rel_path = os.path.relpath(abspath, root).replace("\\", "/")
                            try:
                                tree = ET.parse(abspath)
                                root_el = tree.getroot()
                                for child in root_el:
                                    if isinstance(child.tag, str) and child.text is not None:
                                        original = child.text
                                        if not original.strip():
                                            continue
                                        sid = make_id(self.engine, rel_path, [child.tag], original)
                                        if sid in seen_ids:
                                            continue
                                        seen_ids.add(sid)
                                        ctx = f"RimWorld | {filename.replace('.xml', '')} | {child.tag}"
                                        # Width-limited UI tab/button label -> ask for a short translation.
                                        rp = rel_path.lower()
                                        if "researchtabdef" in rp or "mainbuttondef" in rp:
                                            ctx += " (UI tab/button label — keep translation very short, 1-2 words)"
                                        results.append(self._mk(rel_path, [child.tag], original, ctx))
                            except Exception as e:
                                print(f"Error reading RimWorld XML {filename}: {e}")

            # 3. Generate RimWorld DefInjected from Defs/ (the bulk of mod text
            #    lives here; most mods ship no curated English DefInjected).
            #    Curated English wins on (DefType, key) collisions. seen_ids
            #    spans steps 2 and 3 so generated strings never collide with
            #    curated ones at the id level.
            try:
                english_keys = _english_definjected_keys(base_path)
                for synthetic_file, def_type, key, original in _iter_generated_definjected(base_path, root):
                    if (def_type, key) in english_keys:
                        continue
                    sid = make_id(self.engine, synthetic_file, [key], original)
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    ctx = f"RimWorld Defs | {def_type} | {key}"
                    # Width-limited UI tab/button label -> ask for a short translation.
                    if def_type.lower() in ("researchtabdef", "mainbuttondef"):
                        ctx += " (UI tab/button label — keep translation very short, 1-2 words)"
                    results.append(self._mk(synthetic_file, [key], original, ctx))
            except Exception as e:
                print(f"Error generating RimWorld DefInjected from Defs: {e}")

        return results

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0

        if not target_lang:
            target_lang = "Russian"

        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]

        for base_path in paths_to_check:
            # --- RimWorld: dedup against the AUTHOR's existing target translation
            # so we top up only what's missing and NEVER duplicate a key (a dup
            # string-translation key crashes RimWorld). "Author" keys = keys in
            # target-language files we did NOT create ourselves. Our own prior
            # output is tracked as type='created' in the backup metadata; we
            # EXCLUDE those files so a re-run overwrites them instead of treating
            # them as untouchable. (Keying off file provenance, not off which keys
            # we translated — translating a key that also exists in the author's
            # file must still skip it, or we'd duplicate and crash.)
            author_def_keys: set = set()
            author_keyed_keys: set = set()
            try:
                created_files: set = set()
                meta_path = os.path.join(root, ".interprex_backups", "metadata.json")
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as mf:
                            meta = json.load(mf)
                        for rel, info in meta.items():
                            if isinstance(info, dict) and info.get("type") == "created":
                                created_files.add(os.path.normpath(os.path.join(root, rel)))
                    except Exception:
                        pass

                for target_dir in _find_rimworld_target_dirs(base_path, target_lang):
                    for sub, is_def in (("DefInjected", True), ("Keyed", False)):
                        sub_dir = os.path.join(target_dir, sub)
                        if not os.path.isdir(sub_dir):
                            continue
                        for dp, _, fns in os.walk(sub_dir):
                            dt_norm = _normalize_def_type(os.path.basename(dp))
                            for fn in fns:
                                if not fn.lower().endswith(".xml"):
                                    continue
                                fpath = os.path.join(dp, fn)
                                # Skip our own prior output — it's overwritable.
                                if os.path.normpath(fpath) in created_files:
                                    continue
                                try:
                                    for ch in ET.parse(fpath).getroot():
                                        if not isinstance(ch.tag, str):
                                            continue
                                        if is_def:
                                            author_def_keys.add((dt_norm, _dedup_field_key(ch.tag)))
                                        else:
                                            author_keyed_keys.add(ch.tag)
                                except Exception:
                                    pass
            except Exception as e:
                print(f"Error computing RimWorld author keys: {e}")

            # 1. Inject Stardew JSON
            stardew_default = os.path.join(base_path, "i18n", "default.json")
            if os.path.isfile(stardew_default):
                lang_code = get_stardew_code(target_lang)
                default_rel_path = os.path.relpath(stardew_default, root).replace("\\", "/")
                target_filepath = os.path.join(base_path, "i18n", f"{lang_code}.json")

                # Load original default JSON
                try:
                    with open(stardew_default, "r", encoding="utf-8-sig") as f:
                        default_data = json.load(f)
                except Exception as e:
                    print(f"Error loading Stardew default.json: {e}")
                    default_data = {}

                if isinstance(default_data, dict):
                    # Load existing target JSON if present
                    existing_target = {}
                    if os.path.isfile(target_filepath):
                        try:
                            with open(target_filepath, "r", encoding="utf-8-sig") as f:
                                existing_target = json.load(f)
                            if not isinstance(existing_target, dict):
                                existing_target = {}
                        except Exception:
                            pass

                    # Back up the target file if it already exists
                    if os.path.isfile(target_filepath):
                        self.backup_file(root, target_filepath)

                    # Merge translations
                    has_updates = False
                    for path, original in flatten_json(default_data):
                        sid = make_id(self.engine, default_rel_path, path, original)
                        if sid in translations:
                            set_by_path(existing_target, path, translations[sid])
                            written += 1
                            has_updates = True

                    if has_updates or os.path.isfile(target_filepath):
                        os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
                        with open(target_filepath, "w", encoding="utf-8-sig") as f:
                            json.dump(existing_target, f, ensure_ascii=False, indent=2)

            # 2. Inject RimWorld XMLs — root + version subfolders
            rimworld_english_dirs = _find_rimworld_english_dirs(base_path)
            lang_folder = get_rimworld_folder(target_lang)

            for rimworld_english in rimworld_english_dirs:
                for dirpath, _, filenames in os.walk(rimworld_english):
                    for filename in filenames:
                        if filename.endswith(".xml"):
                            source_abspath = os.path.join(dirpath, filename)
                            source_rel = os.path.relpath(source_abspath, root).replace("\\", "/")

                            # Compute target path: replace Languages/English with Languages/<target>
                            idx = source_rel.find("Languages/English")
                            if idx != -1:
                                target_rel = source_rel[:idx] + "Languages/" + lang_folder + source_rel[idx + len("Languages/English"):]
                            else:
                                target_rel = source_rel.replace("Languages/English", "Languages/" + lang_folder)

                            target_filepath = os.path.join(root, target_rel)

                            try:
                                # Load original XML elements
                                source_tree = ET.parse(source_abspath)
                                source_root = source_tree.getroot()

                                # Is this a DefInjected or a Keyed file? Used to skip
                                # keys the author already translated (avoid dup crash).
                                srl = source_rel.lower()
                                is_definjected = "definjected" in srl
                                # DefType = parent folder name (DefInjected/<DefType>/file.xml)
                                src_def_type = _normalize_def_type(os.path.basename(os.path.dirname(source_abspath)))

                                # Check if we have any translations for this file
                                file_translations = {}
                                for child in source_root:
                                    if isinstance(child.tag, str) and child.text is not None:
                                        # Skip a key the author already provides in the
                                        # target language — never overwrite/duplicate it.
                                        if is_definjected:
                                            if (src_def_type, _dedup_field_key(child.tag)) in author_def_keys:
                                                continue
                                        else:
                                            if child.tag in author_keyed_keys:
                                                continue
                                        sid = make_id(self.engine, source_rel, [child.tag], child.text)
                                        if sid in translations:
                                            file_translations[child.tag] = translations[sid]

                                if not file_translations and not os.path.isfile(target_filepath):
                                    # Nothing to write and target doesn't exist, skip
                                    continue

                                # Load or create target XML
                                file_pre_existed = os.path.isfile(target_filepath)
                                target_root = None
                                if file_pre_existed:
                                    try:
                                        # Backup before writing
                                        self.backup_file(root, target_filepath)
                                        target_tree = ET.parse(target_filepath)
                                        target_root = target_tree.getroot()
                                    except Exception:
                                        pass

                                if target_root is None:
                                    target_root = ET.Element(source_root.tag)

                                # Build maps of existing elements in target XML to update in place
                                target_elements = {el.tag: el for el in target_root if isinstance(el.tag, str)}

                                # Update or append elements
                                for tag, trans_val in file_translations.items():
                                    if tag in target_elements:
                                        target_elements[tag].text = trans_val
                                    else:
                                        new_el = ET.Element(tag)
                                        new_el.text = trans_val
                                        target_root.append(new_el)
                                        target_elements[tag] = new_el
                                    written += 1

                                # Format and save target XML using utf-8-sig encoding for disk output
                                os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
                                ET.indent(target_root, space="  ")
                                xml_bytes = ET.tostring(target_root, encoding="utf-8", xml_declaration=True)
                                xml_str = xml_bytes.decode("utf-8")
                                with open(target_filepath, "w", encoding="utf-8-sig") as f:
                                    f.write(xml_str)

                                # A newly-created file must be registered type='created'
                                if not file_pre_existed:
                                    import hashlib
                                    from .base import update_metadata
                                    rel_to_root = os.path.relpath(target_filepath, root).replace("\\", "/")
                                    mod_sha = hashlib.sha256(xml_bytes).hexdigest()
                                    update_metadata(root, rel_to_root, "", mod_sha, "created")

                            except Exception as e:
                                print(f"Error injecting RimWorld XML {filename}: {e}")

            # 3. Inject generated DefInjected from Defs/. These files don't exist
            #    on disk, so the loop above can't reach them — drive purely off
            #    the Defs walk + the translations dict.
            try:
                english_keys = _english_definjected_keys(base_path)
                # output_file_rel -> {key: translated}
                grouped: dict[str, dict[str, str]] = {}
                for synthetic_file, def_type, key, original in _iter_generated_definjected(base_path, root):
                    if (def_type, key) in english_keys:
                        continue
                    # Skip keys the author already translated in the target language
                    # (normalized type so ThingDefs/ folder matches ThingDef def).
                    if (_normalize_def_type(def_type), _dedup_field_key(key)) in author_def_keys:
                        continue
                    sid = make_id(self.engine, synthetic_file, [key], original)
                    if sid not in translations:
                        continue
                    # Same Languages/English -> Languages/<lang> rewrite as step 2.
                    idx = synthetic_file.find("Languages/English")
                    if idx != -1:
                        out_rel = synthetic_file[:idx] + "Languages/" + lang_folder + synthetic_file[idx + len("Languages/English"):]
                    else:
                        out_rel = synthetic_file.replace("Languages/English", "Languages/" + lang_folder)
                    grouped.setdefault(out_rel, {})[key] = translations[sid]

                for out_rel, kv in grouped.items():
                    target_filepath = os.path.join(root, *out_rel.split("/"))
                    file_pre_existed = os.path.isfile(target_filepath)

                    # Merge into an existing target file (re-injects, hand edits).
                    target_root = None
                    if file_pre_existed:
                        try:
                            self.backup_file(root, target_filepath)
                            target_root = ET.parse(target_filepath).getroot()
                        except Exception:
                            target_root = None
                    if target_root is None:
                        target_root = ET.Element("LanguageData")

                    existing = {el.tag: el for el in target_root if isinstance(el.tag, str)}
                    for key in sorted(kv):
                        trans_val = kv[key]
                        if key in existing:
                            existing[key].text = trans_val
                        else:
                            new_el = ET.Element(key)
                            new_el.text = trans_val
                            target_root.append(new_el)
                            existing[key] = new_el
                        written += 1

                    os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
                    ET.indent(target_root, space="  ")
                    xml_bytes = ET.tostring(target_root, encoding="utf-8", xml_declaration=True)
                    with open(target_filepath, "w", encoding="utf-8-sig") as f:
                        f.write(xml_bytes.decode("utf-8"))

                    # A newly-created file must be registered type='created' so
                    # backup restore deletes it (it had no original to revert to).
                    if not file_pre_existed:
                        import hashlib
                        from .base import update_metadata
                        rel_to_root = os.path.relpath(target_filepath, root).replace("\\", "/")
                        mod_sha = hashlib.sha256(xml_bytes).hexdigest()
                        update_metadata(root, rel_to_root, "", mod_sha, "created")
            except Exception as e:
                print(f"Error injecting generated RimWorld DefInjected: {e}")

        return written
