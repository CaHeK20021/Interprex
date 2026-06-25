using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using System.Reflection;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace UAssetExtractor
{
    public class ExtractedItem
    {
        public string? AssetPath { get; set; }
        public string? InternalPath { get; set; }
        public string? PropName { get; set; }
        public string? Value { get; set; }
        public string? Type { get; set; }
        public string? AssetClass { get; set; }
        // Disambiguators carried to Python: HistoryType filters base-game string
        // table refs; ExportName + ArrayIndex make the stable-id path unique when an
        // asset holds many strings under one prop (widget Text exports, struct
        // arrays like BP_MkPlusSubsystem.Desc).
        public string? HistoryType { get; set; }
        public string? Namespace { get; set; }
        public int ArrayIndex { get; set; } = -1;
        public string? ArrayName { get; set; }
        public string? ContainerPath { get; set; }
        public string? ExportName { get; set; }
        // ContentLib patch routing (export-level facts; Python decides the kind).
        //   SuperClass — the owning ClassExport's parent (SuperStruct) ObjectName,
        //     e.g. FGRecipe / FGItemCategory / FGItemDescriptor / FGSchematic. This
        //     is the ground truth ContentLib type-gates on ("Was not FGRecipe").
        //     retoc to-legacy erases a base-game parent to "UnknownExport"/"" — for
        //     that case Python falls back to HasIngredientsAndProduct.
        //   HasIngredientsAndProduct — the export carries BOTH mIngredients and
        //     mProduct => it's a recipe even when SuperClass was erased. Categories
        //     have only mDisplayName/mMenuPriority, so this stays false for them.
        public string? SuperClass { get; set; }
        public bool HasIngredientsAndProduct { get; set; }
        // CDO full-array-replace support (Part B). A value that lives inside a
        // struct array can only be patched by replacing the WHOLE array via a CDO
        // (ContentLib can't edit a single array element). For such a value we emit:
        //   CdoClass     — the LoadObject path of the object that OWNS the array
        //                  (a sub-object like ".FGUserSetting_IntSelector_0", or the
        //                  class default "..._C" for subsystem arrays).
        //   CdoArrayProp — the array property name to replace (e.g.
        //                  "IntegerSelectionValues", "droneStation").
        //   CdoArrayJson — (first element only) the FULL array serialized as a
        //                  ContentLib JSON value, with each translatable text
        //                  replaced by a placeholder token the Python side swaps for
        //                  the translation. Empty on the other elements.
        //   CdoPlaceholder — the placeholder token for THIS element's value, so
        //                  Python knows which token maps to this item's translation.
        public string? CdoClass { get; set; }
        public string? CdoArrayProp { get; set; }
        public string? CdoArrayJson { get; set; }       // full array JSON, FIRST item of the array only
        public string? CdoPlaceholderToken { get; set; } // this item's placeholder inside that JSON
        public bool CdoArrayOmittedFields { get; set; }  // true if a field (e.g. Ingredients) was dropped
    }

    class Program
    {
        static readonly Regex TechnicalPattern = new(@"^[\{\[\(][A-Z0-9_\-\s]+[\}\]\)]$", RegexOptions.Compiled);

        // Property names that hold technical identifiers / config, NOT user-visible
        // text — never translate (matched case-insensitively). Translating these can
        // break mod logic that compares the key in code (e.g. ContentLib StrId).
        // Verified against real mods: every value under these props was a code
        // key / config (0 overlap with user-visible text). NOTE deliberately NOT
        // here: DisplayCategory ("Mods", "Smart!") and SelectedOption ("All
        // Sources") ARE shown in mod-config UI — they must stay translatable.
        static readonly HashSet<string> TechnicalProps = new(StringComparer.OrdinalIgnoreCase)
        {
            "StrId", "CommandName", "UniqueEmitterName", "DeveloperComment",
            "SourceFilename", "RulePrefix", "Icon File Extension",
            "CurrentStartingLocation", "MapName",
            "keepLevel", "skyAbove", "noRoll", "waterBelow",
        };

        static bool IsTechnicalProp(string? propName)
        {
            if (string.IsNullOrEmpty(propName)) return false;
            if (TechnicalProps.Contains(propName)) return true;
            // Auto-generated Blueprint pin names: <Name>_<idx>_<32-hex>
            if (Regex.IsMatch(propName, @"_[0-9A-Fa-f]{16,}$")) return true;
            return false;
        }

        // A namespaced code key, e.g. "EnhancedConveyors.BeltMk1.Cost" — TWO+ dots,
        // no spaces. A SINGLE dot is NOT enough: real captions like "4NUMER.CNT3R"
        // or "Decoration." have one and must be kept (verified against golden set).
        static bool LooksLikeIdentifier(string v)
        {
            v = v.Trim();
            if (v.Contains(' ')) return false;
            if (v.Count(c => c == '.') >= 2) return true;                // a.b.c namespace
            if (Regex.IsMatch(v, @"_[0-9A-Fa-f]{16,}$")) return true;    // hex-suffixed pin name
            return false;
        }

        // An FText whose HistoryType is StringTableEntry is a REFERENCE into a
        // string table (usually the BASE GAME's, e.g. value "Production/Constructor/
        // Description"). The engine resolves it to the player's language at runtime,
        // so its "value" is a code key, NOT translatable text — extracting it both
        // pollutes the string list and (worse) lets a CDO patch hardcode English,
        // breaking the base-game localization. Skip these everywhere FText is read.
        static bool IsStringTableRef(string? historyType)
        {
            return string.Equals(historyType, "StringTableEntry", StringComparison.OrdinalIgnoreCase);
        }

        static bool IsTranslatable(string value)
        {
            string v = value.Trim();
            if (v.Length == 0) return false;
            if (!v.Any(char.IsLetter)) return false;
            if (TechnicalPattern.IsMatch(v)) return false;
            if (LooksLikeIdentifier(v)) return false;
            return true;
        }

        // The LoadObject path of an export = its package path + the export outer
        // chain. For a top-level export this is "<internalPath>.<ObjectName>"; for a
        // sub-object it threads the outer exports with ':'/'.' as UE does. ContentLib
        // resolves the CDO `Class` field with LoadObject, so this path can point at a
        // sub-object (e.g. an FGUserSetting_IntSelector) or a class default.
        static string GetExportLoadPath(Export export, UAsset asset, string internalPath)
        {
            try
            {
                var names = new List<string>();
                Export cur = export;
                int guard = 0;
                while (cur != null && guard++ < 16)
                {
                    names.Add(cur.ObjectName?.ToString() ?? "");
                    var outer = cur.OuterIndex;
                    if (outer == null || outer.IsNull() || !outer.IsExport()) break;
                    cur = outer.ToExport(asset);
                }
                names.Reverse();
                // First object joins the package with '.', deeper sub-objects with ':'.
                if (names.Count == 0) return internalPath;
                string path = internalPath + "." + names[0];
                for (int i = 1; i < names.Count; i++) path += ":" + names[i];
                return path;
            }
            catch { return internalPath + "." + (export.ObjectName?.ToString() ?? ""); }
        }

        // ---- Part B: CDO full-array-replace serialization --------------------
        // ContentLib can't edit a single array element — it empties the array and
        // rebuilds every element from the JSON, applying ONLY the struct fields the
        // JSON names (others reset to default). So to translate one FText inside a
        // struct array we must re-emit the WHOLE array with EVERY field, swapping
        // just the translatable texts for placeholder tokens Python fills in.
        //
        // Returns the placeholder token for a translatable text at a known location.
        static string CdoPlaceholder(int exportIdx, string arrayProp, int elemIdx, string field)
            => $"@@IPX:{exportIdx}:{arrayProp}:{elemIdx}:{field}@@";

        // Serialize one property's VALUE to a ContentLib JSON token. Translatable
        // FText/FString become placeholder strings (so Python can substitute the
        // translation); everything else is emitted verbatim so the rebuilt array
        // element keeps Buildable/Recipe/Vol/Ingredients etc. Object refs become
        // their full LoadObject path string (ContentLib swaps the reference by
        // path). `skipUnresolvedObjects`: when an Object resolves to UnknownExport
        // (a base-game item retoc couldn't name) we CANNOT emit a valid path; we
        // return null so the caller omits that field (and, for risky arrays, the
        // whole containing field like Ingredients).
        static JToken? PropToCdoJson(PropertyData prop, UAsset asset, int exportIdx,
            string arrayProp, int elemIdx, bool placeholderText, out bool unresolved)
        {
            unresolved = false;
            switch (prop)
            {
                case TextPropertyData tp:
                {
                    string field = prop.Name?.ToString() ?? "";
                    string val = tp.CultureInvariantString?.Value ?? tp.Value?.Value ?? "";
                    bool translatable = !IsStringTableRef(tp.HistoryType.ToString())
                        && !string.IsNullOrEmpty(val) && !IsTechnicalProp(field)
                        && !LooksLikeIdentifier(val);
                    if (placeholderText && translatable)
                        return new JValue(CdoPlaceholder(exportIdx, arrayProp, elemIdx, field));
                    return new JValue(val);
                }
                case StrPropertyData sp:
                {
                    string field = prop.Name?.ToString() ?? "";
                    string val = sp.Value?.Value ?? "";
                    bool translatable = IsTranslatable(val) && !IsTechnicalProp(field);
                    if (placeholderText && translatable)
                        return new JValue(CdoPlaceholder(exportIdx, arrayProp, elemIdx, field));
                    return new JValue(val);
                }
                case ObjectPropertyData op:
                {
                    string path = ResolveObjectPath(op.Value, asset);
                    if (string.IsNullOrEmpty(path) || path.Contains("UnknownExport"))
                    {
                        unresolved = true;
                        return null;
                    }
                    return new JValue(path);
                }
                case IntPropertyData ip: return new JValue(ip.Value);
                case Int64PropertyData i64: return new JValue(i64.Value);
                case FloatPropertyData fp: return new JValue(fp.Value);
                case DoublePropertyData dp: return new JValue(dp.Value);
                case BoolPropertyData bp: return new JValue(bp.Value);
                case BytePropertyData byp: return new JValue(byp.ByteType == BytePropertyType.Byte ? (object)byp.Value : byp.EnumValue?.ToString() ?? "");
                case NamePropertyData np: return new JValue(np.Value?.ToString() ?? "");
                case EnumPropertyData ep: return new JValue(ep.Value?.ToString() ?? "");
                case StructPropertyData stp when stp.Value != null:
                {
                    var obj = new JObject();
                    foreach (var sub in stp.Value)
                    {
                        var tok = PropToCdoJson(sub, asset, exportIdx, arrayProp, elemIdx, placeholderText, out bool u);
                        // An unresolved object ANYWHERE inside this struct (e.g.
                        // Ingredients[i].ItemClass = UnknownExport) poisons the whole
                        // struct — a half-built struct with a missing ref is worse
                        // than omitting the field. Propagate up so the caller drops it.
                        if (u) { unresolved = true; return null; }
                        if (tok != null) obj[sub.Name?.ToString() ?? ""] = tok;
                    }
                    return obj;
                }
                case ArrayPropertyData ap when ap.Value != null:
                {
                    var arr = new JArray();
                    foreach (var el in ap.Value)
                    {
                        var tok = PropToCdoJson(el, asset, exportIdx, arrayProp, elemIdx, false, out bool u);
                        if (u) { unresolved = true; return null; }  // any unresolved obj poisons the array
                        if (tok != null) arr.Add(tok);
                    }
                    return arr;
                }
            }
            return null;
        }

        static bool IsTranslatableText(TextPropertyData tp)
        {
            string val = tp.CultureInvariantString?.Value ?? tp.Value?.Value ?? "";
            return !IsStringTableRef(tp.HistoryType.ToString())
                && !string.IsNullOrEmpty(val)
                && !IsTechnicalProp(tp.Name?.ToString())
                && !LooksLikeIdentifier(val);
        }

        // Serialize a TOP-LEVEL array into a ContentLib JSON array, putting a
        // placeholder token wherever a translatable text sits so Python can fill in
        // translations. Unresolvable object refs (base-game items retoc names
        // UnknownExport) can't be a valid path, so the field holding them is OMITTED
        // (sets omitted=true) — for a struct array that drops e.g. `Ingredients` and
        // ContentLib leaves it at the rebuilt element's default. `omitted` is
        // surfaced so the user knows the replacement is lossy (the Part-B recipe risk).
        static JArray SerializeArrayForCdo(ArrayPropertyData arr, UAsset asset,
            int exportIdx, string arrayProp, out bool omitted, out int placeholderCount)
        {
            omitted = false;
            placeholderCount = 0;
            var outArr = new JArray();
            int elemIdx = 0;
            foreach (var elem in arr.Value)
            {
                if (elem is StructPropertyData es && es.Value != null)
                {
                    var obj = new JObject();
                    foreach (var f in es.Value)
                    {
                        string fname = f.Name?.ToString() ?? "";
                        if (f is TextPropertyData tp && IsTranslatableText(tp))
                        {
                            obj[fname] = new JValue(CdoPlaceholder(exportIdx, arrayProp, elemIdx, fname));
                            placeholderCount++;
                        }
                        else
                        {
                            var tok = PropToCdoJson(f, asset, exportIdx, arrayProp, elemIdx, false, out bool u);
                            if (u || tok == null) { omitted = true; continue; }  // unresolvable -> omit field
                            obj[fname] = tok;
                        }
                    }
                    outArr.Add(obj);
                }
                else if (elem is TextPropertyData et)
                {
                    if (IsTranslatableText(et))
                    {
                        outArr.Add(new JValue(CdoPlaceholder(exportIdx, arrayProp, elemIdx, "")));
                        placeholderCount++;
                    }
                    else
                        outArr.Add(new JValue(et.CultureInvariantString?.Value ?? et.Value?.Value ?? ""));
                }
                else
                {
                    var tok = PropToCdoJson(elem, asset, exportIdx, arrayProp, elemIdx, false, out bool u);
                    if (u || tok == null) omitted = true;
                    else outArr.Add(tok);
                }
                elemIdx++;
            }
            return outArr;
        }

        // Two pieces of position context thread DOWN through struct recursion:
        //   - containerPath: the full nesting address of the current struct, e.g.
        //     "droneStation[0]" (array element), "drone" (plain struct prop), or
        //     "droneStation[0].Ingredients[1]" (nested). This is what makes the
        //     stable-id path UNIQUE: without it every element's `Desc` in the MkPlus
        //     subsystem collapses onto one id and all but one translation is lost.
        //   - arrayName/arrayIndex: the NEAREST enclosing array's name + element
        //     index (null/-1 outside an array). Kept separately so Part B can
        //     reconstruct the array for a CDO full-array-replace patch.
        static void ExtractProps(IEnumerable<PropertyData> props, NormalExport export,
            UAsset asset, string filePath, string internalPath, string assetClass,
            List<ExtractedItem> items, bool insideStruct = false,
            string? arrayName = null, int arrayIndex = -1, string containerPath = "",
            int exportIndex = -1)
        {
            foreach (var prop in props)
            {
                string? valText = null;
                string propType = "Unknown";
                string? historyType = null;
                string? ns = null;

                if (prop is TextPropertyData textProp)
                {
                    propType = "Text";
                    historyType = textProp.HistoryType.ToString();
                    ns = textProp.Namespace?.Value;
                    // A StringTableEntry FText is a base-game reference, not text.
                    if (!IsStringTableRef(historyType))
                    {
                        if (textProp.CultureInvariantString != null && !string.IsNullOrEmpty(textProp.CultureInvariantString.Value))
                            valText = textProp.CultureInvariantString.Value;
                        else if (textProp.Value != null && !string.IsNullOrEmpty(textProp.Value.Value))
                            valText = textProp.Value.Value;
                    }
                }
                else if (prop is StrPropertyData strProp && !insideStruct)
                {
                    if (strProp.Value != null && !string.IsNullOrEmpty(strProp.Value.Value))
                    {
                        string sv = strProp.Value.Value;
                        if (IsTranslatable(sv))
                        {
                            valText = sv;
                            propType = "Str";
                        }
                    }
                }
                else if (prop is StructPropertyData structProp && structProp.Value != null)
                {
                    // Plain (non-array) nested struct: extend the container path with
                    // this prop name; it is NOT an array element so reset arrayName/idx.
                    string sName = structProp.Name?.ToString() ?? "";
                    string childPath = string.IsNullOrEmpty(containerPath) ? sName : $"{containerPath}.{sName}";
                    ExtractProps(structProp.Value, export, asset, filePath, internalPath,
                        assetClass, items, true, null, -1, childPath, exportIndex);
                    continue;
                }
                else if (prop is ArrayPropertyData arrProp && arrProp.Value != null)
                {
                    string arrName = arrProp.Name?.ToString() ?? "";
                    string arrBase = string.IsNullOrEmpty(containerPath) ? arrName : $"{containerPath}.{arrName}";
                    int itemsBeforeArray = items.Count;
                    int idx = 0;
                    foreach (var elem in arrProp.Value)
                    {
                        if (elem is StructPropertyData arrElem && arrElem.Value != null)
                            ExtractProps(arrElem.Value, export, asset, filePath, internalPath,
                                assetClass, items, true, arrName, idx, $"{arrBase}[{idx}]", exportIndex);
                        else if (elem is TextPropertyData arrText)
                        {
                            // TArray<FText> element (e.g. BP_MkPlusSubsystem.Desc).
                            string? av = null;
                            if (arrText.CultureInvariantString != null && !string.IsNullOrEmpty(arrText.CultureInvariantString.Value))
                                av = arrText.CultureInvariantString.Value;
                            else if (arrText.Value != null && !string.IsNullOrEmpty(arrText.Value.Value))
                                av = arrText.Value.Value;
                            string apropName = arrProp.Name?.ToString() ?? "";
                            if (!string.IsNullOrEmpty(av) && !IsStringTableRef(arrText.HistoryType.ToString())
                                && !IsTechnicalProp(apropName) && !LooksLikeIdentifier(av!))
                            {
                                items.Add(new ExtractedItem
                                {
                                    AssetPath = filePath, InternalPath = internalPath,
                                    PropName = apropName, Value = av, Type = "Text",
                                    AssetClass = assetClass,
                                    HistoryType = arrText.HistoryType.ToString(),
                                    Namespace = arrText.Namespace?.Value,
                                    ArrayIndex = idx, ArrayName = arrName,
                                    ContainerPath = $"{arrBase}[{idx}]",
                                });
                            }
                        }
                        else if (elem is StrPropertyData arrStr && arrStr.Value != null && !string.IsNullOrEmpty(arrStr.Value.Value))
                        {
                            string sv = arrStr.Value.Value;
                            string apropName = arrProp.Name?.ToString() ?? "";
                            if (IsTranslatable(sv) && !IsTechnicalProp(apropName) && !LooksLikeIdentifier(sv))
                            {
                                items.Add(new ExtractedItem
                                {
                                    AssetPath = filePath, InternalPath = internalPath,
                                    PropName = apropName, Value = sv, Type = "Str",
                                    AssetClass = assetClass, ArrayIndex = idx, ArrayName = arrName,
                                    ContainerPath = $"{arrBase}[{idx}]",
                                });
                            }
                        }
                        idx++;
                    }

                    // Part B: if this is a TOP-LEVEL array (ContentLib can only edit
                    // top-level props) that produced any translatable items, attach
                    // the full-array CDO JSON so inject can do a full-array-replace.
                    // The owning object's LoadObject path is this export; ContentLib
                    // resolves it and the array prop name to the array being rebuilt.
                    if (string.IsNullOrEmpty(containerPath) && itemsBeforeArray < items.Count)
                    {
                        try
                        {
                            var cdoArr = SerializeArrayForCdo(arrProp, asset, exportIndex,
                                arrName, out bool omitted, out int phc);
                            if (phc > 0)
                            {
                                string cdoClass = GetExportLoadPath(export, asset, internalPath);
                                string cdoJson = cdoArr.ToString(Formatting.None);
                                bool jsonAttached = false;
                                for (int k = itemsBeforeArray; k < items.Count; k++)
                                {
                                    var it = items[k];
                                    if (it.ArrayName != arrName) continue;
                                    it.CdoClass = cdoClass;
                                    it.CdoArrayProp = arrName;
                                    it.CdoArrayOmittedFields = omitted;
                                    it.CdoPlaceholderToken = CdoPlaceholder(exportIndex, arrName,
                                        it.ArrayIndex, it.PropName ?? "");
                                    // attach the (large) array JSON to the FIRST item only
                                    if (!jsonAttached)
                                    {
                                        it.CdoArrayJson = cdoJson;
                                        jsonAttached = true;
                                    }
                                }
                            }
                        }
                        catch { /* CDO serialization is best-effort; never abort extract */ }
                    }
                    continue;
                }

                if (!string.IsNullOrEmpty(valText))
                {
                    string propName = prop.Name?.ToString() ?? "";
                    // Skip technical-identifier properties (StrId, CommandName, …)
                    // and values that are clearly code keys, regardless of prop type.
                    // These hold identifiers the mod compares in code; translating
                    // them breaks logic. Applies to Text too, not just Str.
                    if (IsTechnicalProp(propName)) continue;
                    if (LooksLikeIdentifier(valText)) continue;

                    items.Add(new ExtractedItem
                    {
                        AssetPath = filePath,
                        InternalPath = internalPath,
                        PropName = propName,
                        Value = valText,
                        Type = propType,
                        AssetClass = assetClass,
                        HistoryType = historyType,
                        Namespace = ns,
                        ArrayName = arrayName,
                        ArrayIndex = arrayIndex,
                        ContainerPath = containerPath,
                    });
                }
            }
        }

        // ---- Asset byte-patch (write FText/FString in place) -----------------
        // For strings that ContentLib can't reach (struct-array FText: selector
        // options, subsystem build descriptions), we rewrite the value directly in
        // the .uasset and repack the mod, bypassing ContentLib. An edit is located
        // by the SAME (ExportIndex, ContainerPath, PropName) triple the extractor
        // emits — so the locator always matches what `path[]` was built from.
        public class AssetEdit
        {
            public string? AssetPath { get; set; }   // legacy .uasset path on disk
            public int ExportIndex { get; set; } = -1;
            public string? ContainerPath { get; set; }
            public string? PropName { get; set; }
            public string? NewValue { get; set; }
        }

        // Walk one export's props EXACTLY like ExtractProps and, when the locator
        // matches an edit, set the FText/FString value. Returns how many edits were
        // applied. Mirror of ExtractProps — keep the traversal identical so the
        // (exportIndex, containerPath, propName) coordinates line up byte-for-byte.
        static int ApplyEditsToProps(IEnumerable<PropertyData> props, int exportIndex,
            string containerPath, Dictionary<string, AssetEdit> byKey)
        {
            int applied = 0;
            foreach (var prop in props)
            {
                if (prop is StructPropertyData structProp && structProp.Value != null)
                {
                    string sName = structProp.Name?.ToString() ?? "";
                    string childPath = string.IsNullOrEmpty(containerPath) ? sName : $"{containerPath}.{sName}";
                    applied += ApplyEditsToProps(structProp.Value, exportIndex, childPath, byKey);
                    continue;
                }
                if (prop is ArrayPropertyData arrProp && arrProp.Value != null)
                {
                    string arrName = arrProp.Name?.ToString() ?? "";
                    string arrBase = string.IsNullOrEmpty(containerPath) ? arrName : $"{containerPath}.{arrName}";
                    int idx = 0;
                    foreach (var elem in arrProp.Value)
                    {
                        string elemPath = $"{arrBase}[{idx}]";
                        if (elem is StructPropertyData arrElem && arrElem.Value != null)
                            applied += ApplyEditsToProps(arrElem.Value, exportIndex, elemPath, byKey);
                        else if (elem is TextPropertyData arrText)
                        {
                            // direct TArray<FText>: PropName = array name, container = elem path
                            if (TryEditText(arrText, exportIndex, elemPath, arrName, byKey)) applied++;
                        }
                        else if (elem is StrPropertyData arrStr)
                        {
                            if (TryEditStr(arrStr, exportIndex, elemPath, arrName, byKey)) applied++;
                        }
                        idx++;
                    }
                    continue;
                }
                // leaf FText / FString at the current container level
                string pn = prop.Name?.ToString() ?? "";
                if (prop is TextPropertyData tp)
                {
                    if (TryEditText(tp, exportIndex, containerPath, pn, byKey)) applied++;
                }
                else if (prop is StrPropertyData sp)
                {
                    if (TryEditStr(sp, exportIndex, containerPath, pn, byKey)) applied++;
                }
            }
            return applied;
        }

        static string EditKey(int exportIndex, string containerPath, string propName)
            => $"{exportIndex}{containerPath}{propName}";

        // Fidelity gate: is this asset safe to re-serialize with UAssetAPI?
        // A Blueprint-class asset carries compiled Kismet bytecode (Class/Function/
        // Struct exports). UAssetAPI does NOT model that bytecode, so re-emitting the
        // asset on Write drops/garbles ~half the .uexp — the loaded class becomes NULL
        // and SML crashes ("Attempt to register NULL ModSubsystem"). DataAssets (e.g.
        // FGUserSetting_IntSelector selector options) have ONLY NormalExports and round-
        // trip byte-exact, so they are safe to byte-patch. We gate on EXPORT TYPE — the
        // ground-truth structural marker — not on a fragile byte compare. The earlier
        // byte-compare approach was unreliable: a plain load→Write of a Blueprint can
        // serialize clean while the actual property EDIT still corrupts it.
        static readonly Type[] _bytecodeExportTypes = new[]
        {
            typeof(UAssetAPI.ExportTypes.ClassExport),
            typeof(UAssetAPI.ExportTypes.FunctionExport),
            typeof(UAssetAPI.ExportTypes.StructExport),
        };

        // Fidelity gate: is this asset safe to re-serialize with UAssetAPI after a real
        // property EDIT? Two structural markers, both load-bearing:
        //
        // 1. BYTECODE EXPORTS (the crash). A Blueprint-class asset carries compiled
        //    Kismet bytecode in its Class/Function/Struct exports. UAssetAPI does NOT
        //    model that bytecode. A NO-OP load→Write can come out byte-identical (raw
        //    export bytes are preserved when nothing in the tree changed) — but the
        //    moment ANY property is edited, UAssetAPI re-serializes the whole .uexp from
        //    its parsed tree and DROPS ~half of it (measured on BP_MkPlusSubsystem:
        //    .uexp 44127→43671, 22780 bytes differ). The loaded class then becomes NULL
        //    → SML `Attempt to register NULL ModSubsystem` crash on map load (a real
        //    shipped crash, reproduced 2026-06-25). So a no-op byte round-trip is NOT a
        //    valid safety test — the export TYPE is. RawExport = UAssetAPI couldn't parse
        //    the export at all → also unsafe.
        //
        // 2. (NO LONGER GATED) UnknownExport imports. The old gate also rejected any
        //    asset whose import table held an UnknownExport stub, on the theory that
        //    retoc erased a base-game script import the game then can't resolve. Since
        //    Satisfactory updated to UE5.6 it ships script objects in global.utoc, so
        //    retoc resolves FGUserSetting_IntSelector etc.; the residual UnknownExport
        //    stubs on a selector are plain base-game Object field refs (e.g. an empty
        //    SubOptionTo), NOT a load blocker — and gating on them discarded exactly the
        //    selector DataAssets we want. DataAssets are NormalExport-only and edit-then-
        //    write byte-clean (selector verified: only the edited FText changes), so they
        //    pass this gate and ship. We trust the export-type structural marker, NOT the
        //    import table and NOT a no-op byte compare.
        static bool IsLoadWriteRoundTripSafe(string path, EngineVersion version,
            UAssetAPI.Unversioned.Usmap? mappings)
        {
            try
            {
                var probe = mappings != null ? new UAsset(path, version, mappings) : new UAsset(path, version);
                foreach (var export in probe.Exports)
                {
                    if (export is UAssetAPI.ExportTypes.RawExport) return false;
                    var t = export.GetType();
                    foreach (var bt in _bytecodeExportTypes)
                        if (bt.IsAssignableFrom(t)) return false;
                }
                return true;
            }
            catch { return false; }
        }

        static bool TryEditText(TextPropertyData tp, int exportIndex, string containerPath,
            string propName, Dictionary<string, AssetEdit> byKey)
        {
            if (!byKey.TryGetValue(EditKey(exportIndex, containerPath, propName), out var edit))
                return false;
            // Write the new value where the engine reads it. Prefer the field that
            // currently holds the source value (CultureInvariant first, else Value).
            var nv = new FString(edit.NewValue ?? "");
            if (tp.CultureInvariantString != null && !string.IsNullOrEmpty(tp.CultureInvariantString.Value))
                tp.CultureInvariantString = nv;
            else
                tp.Value = nv;
            return true;
        }

        static bool TryEditStr(StrPropertyData sp, int exportIndex, string containerPath,
            string propName, Dictionary<string, AssetEdit> byKey)
        {
            if (!byKey.TryGetValue(EditKey(exportIndex, containerPath, propName), out var edit))
                return false;
            sp.Value = new FString(edit.NewValue ?? "");
            return true;
        }

        // Apply a batch of edits to legacy .uasset files (in place). Edits are
        // grouped by AssetPath; each asset is opened, patched across its exports,
        // and written back via UAssetAPI (recomputes .uexp offsets). Returns total
        // edits applied. Never throws on one bad asset.
        // Returns (appliedCount, writtenAssetPaths). Only assets that round-trip with
        // byte-perfect fidelity (VerifyBinaryEquality before any edit) are written; an
        // asset UAssetAPI can't faithfully re-serialize (Blueprints — their bytecode is
        // not modelled, a no-op Write mangles ~half the .uexp and the loaded class goes
        // NULL → SML crash) is SKIPPED entirely. DataAssets (e.g. FGUserSetting_IntSelector
        // selector options) round-trip exactly, so they ARE patched. This is the
        // "translate everything we safely can" gate: unsafe assets stay English, never crash.
        static (int applied, List<string> written) ApplyEdits(
            List<AssetEdit> edits, EngineVersion version, UAssetAPI.Unversioned.Usmap? mappings = null)
        {
            int total = 0;
            var written = new List<string>();
            foreach (var grp in edits.GroupBy(e => e.AssetPath))
            {
                string? path = grp.Key;
                if (string.IsNullOrEmpty(path) || !File.Exists(path)) continue;
                try
                {
                    var byKey = new Dictionary<string, AssetEdit>();
                    foreach (var e in grp)
                        byKey[EditKey(e.ExportIndex, e.ContainerPath ?? "", e.PropName ?? "")] = e;

                    // GATE: serialize a fresh load to a temp file and require it to match
                    // pristine disk BYTE-FOR-BYTE. UAssetAPI re-emits each export from its
                    // property tree on Write; for a Blueprint that tree is incomplete (the
                    // bytecode isn't modelled), so even a plain load→Write loses ~half the
                    // .uexp — which NULLs the loaded class and crashes SML. DataAssets
                    // re-serialize byte-identical. Unsafe assets are skipped entirely
                    // (stay English, never crash). Pristine bytes are read inside, before
                    // any write touches `path`.
                    bool safe = IsLoadWriteRoundTripSafe(path, version, mappings);
                    if (!safe)
                    {
                        Console.Error.WriteLine($"SKIP (not round-trip safe): {Path.GetFileName(path)}");
                        continue;
                    }

                    var asset = mappings != null
                        ? new UAsset(path, version, mappings)
                        : new UAsset(path, version);
                    int applied = 0;
                    int exportIdx = -1;
                    foreach (var export in asset.Exports.OfType<NormalExport>())
                    {
                        exportIdx++;
                        applied += ApplyEditsToProps(export.Data, exportIdx, "", byKey);
                    }
                    if (applied > 0)
                    {
                        asset.Write(path);   // rewrites .uasset (+ .uexp) with new offsets
                        total += applied;
                        written.Add(path);
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"ApplyEdits failed for {path}: {ex.Message}");
                }
            }
            return (total, written);
        }

        // Extract all translatable items from ONE .uasset. Returns [] (and never
        // throws) on a failed/unreadable asset so a batch run skips it cleanly.
        static List<ExtractedItem> ProcessAsset(string filePath, EngineVersion version)
        {
            return ProcessAsset(filePath, version, out _, out _);
        }

        // Overload that also reports the asset's RAW package path (FolderName, used to
        // build the package->file index for inheritance) and one CdoExportRecord per
        // CDO export (consumed in batch pass 2 to synthesize inherited items).
        static List<ExtractedItem> ProcessAsset(string filePath, EngineVersion version,
            out string rawFolderName, out List<CdoExportRecord> cdoRecords)
        {
            var items = new List<ExtractedItem>();
            rawFolderName = "";
            cdoRecords = new List<CdoExportRecord>();
            try
            {
                var asset = new UAsset(filePath, version);
                rawFolderName = asset.FolderName?.Value ?? "";
                string internalPath = asset.FolderName?.Value ?? "";
                if (!string.IsNullOrEmpty(internalPath) && !internalPath.StartsWith("/Game/"))
                {
                    internalPath = "/Game/Mods" + internalPath;
                }

                int exportIdx = -1;
                foreach (var export in asset.Exports.OfType<NormalExport>())
                {
                    exportIdx++;
                    string assetClass = "Unknown";
                    try
                    {
                        if (export.ClassIndex.IsImport())
                        {
                            assetClass = export.ClassIndex.ToImport(asset)?.ObjectName?.ToString() ?? "Import";
                        }
                        else if (export.ClassIndex.IsExport())
                        {
                            assetClass = export.ClassIndex.ToExport(asset)?.ObjectName?.ToString() ?? "Export";
                        }
                    }
                    catch
                    {
                        try
                        {
                            assetClass = export.ClassIndex.ToString();
                        }
                        catch {}
                    }

                    // Resolve the PARENT class (SuperStruct) of this export's class so
                    // Python can route the ContentLib patch by the same type ContentLib
                    // gates on. export.ClassIndex -> the "<Name>_C" ClassExport (a
                    // StructExport); its SuperStruct is the parent (FGRecipe / FGItem-
                    // Category / FGItemDescriptor / FGSchematic ...). Base-game parents
                    // are imports; a mod intermediate parent is an export. retoc erases
                    // a direct base-game parent to UnknownExport -> we leave "" and let
                    // HasIngredientsAndProduct decide.
                    string superClass = "";
                    try
                    {
                        if (export.ClassIndex.IsExport() &&
                            export.ClassIndex.ToExport(asset) is UAssetAPI.ExportTypes.StructExport se)
                        {
                            var sup = se.SuperStruct;
                            if (!sup.IsNull())
                            {
                                if (sup.IsImport())
                                    superClass = sup.ToImport(asset)?.ObjectName?.ToString() ?? "";
                                else if (sup.IsExport())
                                    superClass = sup.ToExport(asset)?.ObjectName?.ToString() ?? "";
                            }
                        }
                    }
                    catch { /* unresolvable -> "" -> Python uses the property fallback */ }

                    // Recipe signal that survives retoc parent-erasure: a recipe export
                    // carries BOTH mIngredients and mProduct at top level; a category
                    // has only mDisplayName/mMenuPriority.
                    bool hasIngProd = false;
                    try
                    {
                        var topNames = new HashSet<string>(
                            export.Data.Select(p => p.Name?.ToString() ?? ""),
                            StringComparer.OrdinalIgnoreCase);
                        hasIngProd = topNames.Contains("mIngredients") && topNames.Contains("mProduct");
                    }
                    catch {}

                    int before = items.Count;
                    ExtractProps(export.Data, export, asset, filePath, internalPath, assetClass,
                        items, false, null, -1, "", exportIdx);
                    // ObjectName alone is NOT unique across exports — an asset can
                    // hold several sub-objects with the SAME ObjectName (e.g.
                    // SmartFoundations Smart_Config has six exports all named
                    // BP_ConfigPropertyBool_C_0). Append the export TABLE INDEX so
                    // the discriminator is unique per export. The index is stable for
                    // an unchanged asset (we re-extract the same bytes), so the
                    // stable id is reproducible across runs.
                    string exportName = (export.ObjectName?.ToString() ?? "") + "#" + exportIdx;
                    for (int k = before; k < items.Count; k++)
                    {
                        items[k].ExportName = exportName;
                        items[k].SuperClass = superClass;
                        items[k].HasIngredientsAndProduct = hasIngProd;
                    }

                    // Record this CDO export so batch pass 2 can resolve inherited
                    // translatable props (present in the parent's CDO but absent here).
                    // Only the "Default__..._C" CDO export has a meaningful parent link.
                    string superPkg = GetSuperPackagePath(export, asset);
                    if (!string.IsNullOrEmpty(superPkg))
                    {
                        var ownProps = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                        foreach (var p in export.Data)
                        {
                            var n = p.Name?.ToString();
                            if (!string.IsNullOrEmpty(n)) ownProps.Add(n!);
                        }
                        cdoRecords.Add(new CdoExportRecord
                        {
                            FilePath = filePath,
                            InternalPath = internalPath,
                            SuperPackagePath = superPkg,
                            ExportName = exportName,
                            AssetClass = assetClass,
                            SuperClass = superClass,
                            HasIngredientsAndProduct = hasIngProd,
                            OwnTopLevelProps = ownProps,
                        });
                    }
                }
            }
            catch
            {
                // A single bad asset must not abort a whole-directory batch run.
            }
            return items;
        }

        // Resolve an FPackageIndex to a full UE object path "/Package/Path.ObjectName"
        // by walking the import/export outer chain. Returns "" if unresolvable.
        static string ResolveObjectPath(FPackageIndex idx, UAsset asset)
        {
            try
            {
                if (idx.IsNull()) return "";
                if (idx.IsImport())
                {
                    var imp = idx.ToImport(asset);
                    if (imp == null) return "";
                    string self = imp.ObjectName?.ToString() ?? "";
                    string outer = ResolveObjectPath(imp.OuterIndex, asset);
                    if (string.IsNullOrEmpty(outer)) return self;
                    // The first non-package outer is joined with '.', package parts with '/'.
                    string cls = imp.ClassName?.ToString() ?? "";
                    return outer + (cls == "Package" ? "" : ".") + self;
                }
                if (idx.IsExport())
                {
                    var exp = idx.ToExport(asset);
                    return exp?.ObjectName?.ToString() ?? "";
                }
            }
            catch {}
            return "";
        }

        // === Inherited translatable property resolution ======================
        // A Blueprint child class stores in its CDO ONLY the properties it OVERRIDES;
        // an inherited property (e.g. a variant that keeps the parent's mDescription)
        // is baked into the PARENT's CDO and is absent from the child .uasset. So the
        // child's inherited caption never extracts -> never translates -> stays English
        // (real bug: Build_GlassTank_4Pipes had a RU name but EN description; its
        // sibling Build_GlassTank_Child had the opposite). Patching the parent does NOT
        // cascade (ContentLib loads the child as a separate object whose inherited value
        // was already compiled in). Fix: for each child CDO, find which translatable
        // props are MISSING vs its parent chain, read them from the parent .uasset, and
        // emit them as additional items stamped with the CHILD's coordinates so they
        // translate and get patched onto the CHILD's class.

        // One resolved translatable top-level CDO prop (already filtered).
        class ResolvedProp
        {
            public string Value = "";
            public string Type = "Text";   // "Text" | "Str"
            public string? HistoryType;
            public string? Namespace;
        }

        // A parent asset's translatable top-level CDO props + its own parent link,
        // cached per raw package path so N children of one parent resolve in O(1).
        class ResolvedCdo
        {
            public string SuperPackagePath = "";  // raw pkg path of THIS asset's parent ("" if base/erased)
            public Dictionary<string, ResolvedProp> Props =
                new(StringComparer.OrdinalIgnoreCase);   // translatable props this asset DEFINES
        }

        // Per-child CDO export record captured in pass 1, consumed in pass 2 to
        // synthesize inherited items. Carries the CHILD's coordinates/routing facts.
        class CdoExportRecord
        {
            public string FilePath = "";
            public string InternalPath = "";       // post-/Game/Mods rewrite (child)
            public string SuperPackagePath = "";   // raw parent pkg path (from GetSuperPackagePath)
            public string ExportName = "";         // "Default__X_C#<idx>"
            public string AssetClass = "";
            public string SuperClass = "";
            public bool HasIngredientsAndProduct;
            public HashSet<string> OwnTopLevelProps = new(StringComparer.OrdinalIgnoreCase);
        }

        // Read a CDO export's ClassExport.SuperStruct -> the parent CLASS import ->
        // its Package outer ObjectName = the parent's RAW package path (the same form
        // as FolderName, e.g. "/GlassFluidTank/Build_GlassTank"). Returns "" when the
        // super is null/erased (UnknownExport / UnknownPackage = base-game parent).
        static string GetSuperPackagePath(NormalExport cdoExport, UAsset asset)
        {
            try
            {
                // CDO export's ClassIndex -> the "<Name>_C" ClassExport (a StructExport).
                var ci = cdoExport.ClassIndex;
                if (!ci.IsExport()) return "";
                if (ci.ToExport(asset) is not UAssetAPI.ExportTypes.StructExport se) return "";
                var sup = se.SuperStruct;
                if (sup.IsNull() || !sup.IsImport()) return "";
                var parentClassImp = sup.ToImport(asset);
                if (parentClassImp == null) return "";
                // parent class import's outer is a Package import -> its ObjectName is
                // the parent's package path.
                var outer = parentClassImp.OuterIndex;
                if (!outer.IsImport()) return "";
                var pkgImp = outer.ToImport(asset);
                string pkg = pkgImp?.ObjectName?.ToString() ?? "";
                if (string.IsNullOrEmpty(pkg) || pkg.StartsWith("/Engine/")) return "";
                return pkg;
            }
            catch { return ""; }
        }

        // Load (and cache) one asset's translatable top-level CDO props + its super
        // link. Returns null if the package isn't a mod asset on disk (so the chain
        // stops). NEVER throws.
        static ResolvedCdo? ResolveCdo(string pkgPath, Dictionary<string, string> packageIndex,
            Dictionary<string, ResolvedCdo?> cache, EngineVersion version)
        {
            if (cache.TryGetValue(pkgPath, out var cached)) return cached;
            ResolvedCdo? result = null;
            try
            {
                if (packageIndex.TryGetValue(pkgPath, out var file) && File.Exists(file))
                {
                    var asset = new UAsset(file, version);
                    var rc = new ResolvedCdo();
                    foreach (var export in asset.Exports.OfType<NormalExport>())
                    {
                        // The CDO is the "Default__..._C" export. Read its super link
                        // once and collect its translatable top-level props.
                        if (string.IsNullOrEmpty(rc.SuperPackagePath))
                            rc.SuperPackagePath = GetSuperPackagePath(export, asset);
                        foreach (var prop in export.Data)
                        {
                            var rp = TryReadTranslatableTopLevel(prop);
                            if (rp == null) continue;
                            string pn = prop.Name?.ToString() ?? "";
                            if (string.IsNullOrEmpty(pn)) continue;
                            if (!rc.Props.ContainsKey(pn)) rc.Props[pn] = rp;
                        }
                    }
                    result = rc;
                }
            }
            catch { result = null; }
            cache[pkgPath] = result;
            return result;
        }

        // Walk the SuperStruct chain from a child to the first ancestor that DEFINES a
        // translatable value for `propName`. Nearest-parent wins (UE inheritance).
        // Returns null if none, or the chain ends at a non-mod parent. NEVER throws.
        static ResolvedProp? ResolveInheritedProp(string childSuperPkgPath, string propName,
            Dictionary<string, string> packageIndex, Dictionary<string, ResolvedCdo?> cache,
            EngineVersion version)
        {
            string pkg = childSuperPkgPath;
            var guard = new HashSet<string>(StringComparer.OrdinalIgnoreCase);  // cycle guard
            while (!string.IsNullOrEmpty(pkg) && guard.Add(pkg))
            {
                var rc = ResolveCdo(pkg, packageIndex, cache, version);
                if (rc == null) return null;       // parent not on disk (base-game) -> stop
                if (rc.Props.TryGetValue(propName, out var rp)) return rp;
                pkg = rc.SuperPackagePath;
            }
            return null;
        }

        // Collect every translatable top-level prop NAME defined anywhere up the parent
        // chain (used to know which props a child could inherit). NEVER throws.
        static HashSet<string> CollectInheritablePropNames(string childSuperPkgPath,
            Dictionary<string, string> packageIndex, Dictionary<string, ResolvedCdo?> cache,
            EngineVersion version)
        {
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            string pkg = childSuperPkgPath;
            var guard = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            while (!string.IsNullOrEmpty(pkg) && guard.Add(pkg))
            {
                var rc = ResolveCdo(pkg, packageIndex, cache, version);
                if (rc == null) break;
                foreach (var k in rc.Props.Keys) names.Add(k);
                pkg = rc.SuperPackagePath;
            }
            return names;
        }

        // Read a single TOP-LEVEL translatable value from a property, applying the SAME
        // filters as ExtractProps (StringTableEntry refs, technical props, identifiers).
        // Returns null for non-translatable / non-text props. Mirrors the Text/Str
        // branches of ExtractProps at the top level (insideStruct == false).
        static ResolvedProp? TryReadTranslatableTopLevel(PropertyData prop)
        {
            try
            {
                string propName = prop.Name?.ToString() ?? "";
                if (IsTechnicalProp(propName)) return null;
                if (prop is TextPropertyData tp)
                {
                    string ht = tp.HistoryType.ToString();
                    if (IsStringTableRef(ht)) return null;
                    string? v = null;
                    if (tp.CultureInvariantString != null && !string.IsNullOrEmpty(tp.CultureInvariantString.Value))
                        v = tp.CultureInvariantString.Value;
                    else if (tp.Value != null && !string.IsNullOrEmpty(tp.Value.Value))
                        v = tp.Value.Value;
                    if (string.IsNullOrEmpty(v) || LooksLikeIdentifier(v!)) return null;
                    return new ResolvedProp { Value = v!, Type = "Text",
                        HistoryType = ht, Namespace = tp.Namespace?.Value };
                }
                if (prop is StrPropertyData sp && sp.Value != null && !string.IsNullOrEmpty(sp.Value.Value))
                {
                    string sv = sp.Value.Value;
                    if (!IsTranslatable(sv) || LooksLikeIdentifier(sv)) return null;
                    return new ResolvedProp { Value = sv, Type = "Str" };
                }
            }
            catch { }
            return null;
        }

        // TEMP DIAGNOSTIC (remove before ship): dump the property tree of one asset
        // so we can see the exact struct field names + array nesting for Part B.
        static void DumpTree(IEnumerable<PropertyData> props, UAsset asset, int depth, System.Text.StringBuilder sb)
        {
            string pad = new string(' ', depth * 2);
            foreach (var prop in props)
            {
                string name = prop.Name?.ToString() ?? "?";
                if (prop is TextPropertyData tp)
                    sb.AppendLine($"{pad}{name} : Text[{tp.HistoryType}] = {(tp.CultureInvariantString?.Value ?? tp.Value?.Value ?? "")?.Substring(0, Math.Min(40, (tp.CultureInvariantString?.Value ?? tp.Value?.Value ?? "").Length))}");
                else if (prop is StrPropertyData sp)
                    sb.AppendLine($"{pad}{name} : Str = {sp.Value?.Value}");
                else if (prop is ObjectPropertyData op)
                {
                    sb.AppendLine($"{pad}{name} : Object -> {ResolveObjectPath(op.Value, asset)}");
                }
                else if (prop is IntPropertyData ip)
                    sb.AppendLine($"{pad}{name} : Int = {ip.Value}");
                else if (prop is StructPropertyData stp && stp.Value != null)
                {
                    sb.AppendLine($"{pad}{name} : Struct[{stp.StructType}]");
                    DumpTree(stp.Value, asset, depth + 1, sb);
                }
                else if (prop is ArrayPropertyData ap && ap.Value != null)
                {
                    sb.AppendLine($"{pad}{name} : Array[{ap.Value.Length}] (elemType={ap.ArrayType})");
                    for (int i = 0; i < ap.Value.Length; i++)
                    {
                        var elem = ap.Value[i];
                        if (elem is StructPropertyData es && es.Value != null)
                        {
                            sb.AppendLine($"{pad}  [{i}] Struct[{es.StructType}]");
                            DumpTree(es.Value, asset, depth + 2, sb);
                        }
                        else if (elem is TextPropertyData et)
                            sb.AppendLine($"{pad}  [{i}] Text[{et.HistoryType}] = {(et.CultureInvariantString?.Value ?? et.Value?.Value ?? "")}");
                        else
                            sb.AppendLine($"{pad}  [{i}] {elem.GetType().Name} = {elem}");
                    }
                }
                else
                    sb.AppendLine($"{pad}{name} : {prop.GetType().Name}");
            }
        }

        static void Main(string[] args)
        {
            string filePath = "";
            string inputDir = "";
            string outputPath = "";
            string applyEdits = "";   // path to edits JSON
            string baseDir = "";      // legacy asset dir the edits' AssetPath is relative to
            string mappingsPath = ""; // .usmap mappings for honest unversioned-property round-trip
            bool dumpTree = false;
            bool verifyRoundtrip = false;
            EngineVersion version = EngineVersion.VER_UE5_4;

            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--input" && i + 1 < args.Length)
                {
                    filePath = args[i + 1];
                }
                if (args[i] == "--input-dir" && i + 1 < args.Length)
                {
                    inputDir = args[i + 1];
                }
                if (args[i] == "--output" && i + 1 < args.Length)
                {
                    outputPath = args[i + 1];
                }
                if (args[i] == "--apply-edits" && i + 1 < args.Length)
                {
                    applyEdits = args[i + 1];
                }
                if (args[i] == "--base-dir" && i + 1 < args.Length)
                {
                    baseDir = args[i + 1];
                }
                if (args[i] == "--mappings" && i + 1 < args.Length)
                {
                    mappingsPath = args[i + 1];
                }
                if (args[i] == "--engine" && i + 1 < args.Length)
                {
                    if (Enum.TryParse<EngineVersion>(args[i + 1], out var parsedVer))
                    {
                        version = parsedVer;
                    }
                }
                if (args[i] == "--dump-tree") dumpTree = true;
                if (args[i] == "--dump-imports") { dumpTree = true; /* filePath set via --input, handled below */ }
                if (args[i] == "--verify-roundtrip") verifyRoundtrip = true;
            }

            // Diagnostic: report the byte-patch fidelity gate decision per asset —
            // SAFE (DataAsset, only NormalExports → edit-then-write is byte-clean) vs
            // SKIP (carries Class/Function/Struct/Raw exports → a real EDIT corrupts the
            // .uexp → SML crash). This mirrors the EXACT gate ApplyEdits uses, so it is
            // an honest preview of what will/won't be byte-patched. (A no-op load→Write
            // byte compare is NOT used — it falsely passes Blueprints, whose raw export
            // bytes survive an unchanged write but are destroyed by an actual edit.)
            if (verifyRoundtrip)
            {
                UAssetAPI.Unversioned.Usmap? mappings = null;
                if (!string.IsNullOrEmpty(mappingsPath) && File.Exists(mappingsPath))
                {
                    try { mappings = new UAssetAPI.Unversioned.Usmap(mappingsPath); }
                    catch (Exception ex) { Console.Error.WriteLine($"Usmap load failed (continuing without): {ex.Message}"); }
                }
                var files = new List<string>();
                if (!string.IsNullOrEmpty(inputDir) && Directory.Exists(inputDir))
                    files.AddRange(Directory.EnumerateFiles(inputDir, "*.uasset", SearchOption.AllDirectories));
                else if (!string.IsNullOrEmpty(filePath))
                    files.Add(filePath);
                int safec = 0, skipc = 0;
                foreach (var fp in files)
                {
                    string name = Path.GetFileName(fp);
                    if (IsLoadWriteRoundTripSafe(fp, version, mappings)) { safec++; Console.WriteLine($"SAFE  {name}"); }
                    else { skipc++; Console.WriteLine($"SKIP  {name}"); }
                }
                Console.WriteLine($"\n=== gate: SAFE={safec} SKIP={skipc} total={files.Count} ===");
                return;
            }

            // Byte-patch mode: read edits JSON, rewrite FText/FString in the legacy
            // .uasset files in place. AssetPath in each edit is relative to baseDir.
            if (!string.IsNullOrEmpty(applyEdits))
            {
                if (!File.Exists(applyEdits))
                {
                    Console.Error.WriteLine($"Edits file not found: {applyEdits}");
                    Environment.Exit(1);
                }
                var raw = File.ReadAllText(applyEdits);
                var edits = JsonConvert.DeserializeObject<List<AssetEdit>>(raw) ?? new List<AssetEdit>();
                foreach (var e in edits)
                {
                    if (!string.IsNullOrEmpty(baseDir) && !string.IsNullOrEmpty(e.AssetPath)
                        && !Path.IsPathRooted(e.AssetPath))
                        e.AssetPath = Path.Combine(baseDir, e.AssetPath);
                }
                UAssetAPI.Unversioned.Usmap? mappings = null;
                if (!string.IsNullOrEmpty(mappingsPath) && File.Exists(mappingsPath))
                {
                    try { mappings = new UAssetAPI.Unversioned.Usmap(mappingsPath); }
                    catch (Exception ex) { Console.Error.WriteLine($"Usmap load failed (continuing without): {ex.Message}"); }
                }
                var (applied, written) = ApplyEdits(edits, version, mappings);
                // Emit the absolute paths actually written so the caller packs ONLY
                // those into the _P container (skipped/unsafe assets are excluded).
                var resultObj = new { applied, requested = edits.Count, written };
                Console.WriteLine(JsonConvert.SerializeObject(resultObj));
                return;
            }

            // Dump the raw import table to see if the unresolved ItemClass object
            // names survive anywhere (retoc prints UnknownExport but the import
            // record may still carry ObjectName/OuterIndex we can reconstruct).
            if (args.Contains("--dump-imports") && !string.IsNullOrEmpty(filePath))
            {
                var ai = new UAsset(filePath, version);
                var sbi = new System.Text.StringBuilder();
                int ix = 0;
                foreach (var imp in ai.Imports)
                {
                    sbi.AppendLine($"[{ix}] obj='{imp.ObjectName}' class='{imp.ClassName}' pkg='{imp.ClassPackage}' outer={imp.OuterIndex.Index} bImport={imp.bImportOptional}");
                    ix++;
                }
                if (!string.IsNullOrEmpty(outputPath))
                    File.WriteAllText(outputPath, sbi.ToString(), new System.Text.UTF8Encoding(false));
                else
                    Console.WriteLine(sbi.ToString());
                return;
            }

            if (dumpTree && !string.IsNullOrEmpty(filePath))
            {
                var a = new UAsset(filePath, version);
                var sb = new System.Text.StringBuilder();
                foreach (var export in a.Exports.OfType<NormalExport>())
                {
                    sb.AppendLine($"=== EXPORT {export.ObjectName} ===");
                    DumpTree(export.Data, a, 0, sb);
                }
                if (!string.IsNullOrEmpty(outputPath))
                    File.WriteAllText(outputPath, sb.ToString(), new System.Text.UTF8Encoding(false));
                else
                    Console.WriteLine(sb.ToString());
                return;
            }

            // Batch mode: one process processes EVERY .uasset under a directory and
            // emits a single combined JSON array. This kills the per-file process-
            // spawn overhead (the old --input path spawned the CLR once per asset —
            // hundreds of times per mod). AssetPath on each item tells the caller
            // which file it came from.
            if (!string.IsNullOrEmpty(inputDir))
            {
                if (!Directory.Exists(inputDir))
                {
                    Console.WriteLine($"Error: Directory not found: {inputDir}");
                    Environment.Exit(1);
                }
                var all = new List<ExtractedItem>();
                // Pass 1: extract every asset's own strings; build the package->file
                // index (raw FolderName) and gather CDO export records for inheritance.
                var packageIndex = new Dictionary<string, string>();   // raw pkg path -> file
                var cdoRecords = new List<CdoExportRecord>();
                foreach (var f in Directory.EnumerateFiles(inputDir, "*.uasset", SearchOption.AllDirectories))
                {
                    all.AddRange(ProcessAsset(f, version, out string folder, out var recs));
                    if (!string.IsNullOrEmpty(folder) && !packageIndex.ContainsKey(folder))
                        packageIndex[folder] = f;
                    cdoRecords.AddRange(recs);
                }
                // Pass 2: for each child CDO, resolve translatable props it INHERITS
                // (present up the parent chain but absent in its own CDO) and emit them
                // as items stamped with the CHILD's coordinates, so they translate and
                // get patched onto the child's class. Read-only on parents; never throws.
                var cdoCache = new Dictionary<string, ResolvedCdo?>(StringComparer.OrdinalIgnoreCase);
                foreach (var rec in cdoRecords)
                {
                    if (string.IsNullOrEmpty(rec.SuperPackagePath)) continue;
                    var inheritable = CollectInheritablePropNames(
                        rec.SuperPackagePath, packageIndex, cdoCache, version);
                    foreach (var propName in inheritable)
                    {
                        if (rec.OwnTopLevelProps.Contains(propName)) continue;   // child overrides it
                        var rp = ResolveInheritedProp(
                            rec.SuperPackagePath, propName, packageIndex, cdoCache, version);
                        if (rp == null) continue;
                        all.Add(new ExtractedItem
                        {
                            AssetPath = rec.FilePath,
                            InternalPath = rec.InternalPath,
                            PropName = propName,
                            Value = rp.Value,
                            Type = rp.Type,
                            AssetClass = rec.AssetClass,
                            HistoryType = rp.HistoryType,
                            Namespace = rp.Namespace,
                            ArrayIndex = -1,
                            ArrayName = null,
                            ContainerPath = "",
                            ExportName = rec.ExportName,
                            SuperClass = rec.SuperClass,
                            HasIngredientsAndProduct = rec.HasIngredientsAndProduct,
                        });
                    }
                }
                string batchJson = JsonConvert.SerializeObject(all, Formatting.Indented);
                if (!string.IsNullOrEmpty(outputPath))
                    File.WriteAllText(outputPath, batchJson, new System.Text.UTF8Encoding(false));
                else
                    Console.WriteLine(batchJson);
                return;
            }

            if (string.IsNullOrEmpty(filePath))
            {
                Console.WriteLine("Error: Input file path is required. Use --input <path> or --input-dir <dir>");
                Environment.Exit(1);
            }

            if (!File.Exists(filePath))
            {
                Console.WriteLine($"Error: File not found: {filePath}");
                Environment.Exit(1);
            }

            var items = ProcessAsset(filePath, version);
            string jsonStr = JsonConvert.SerializeObject(items, Formatting.Indented);
            if (!string.IsNullOrEmpty(outputPath))
            {
                File.WriteAllText(outputPath, jsonStr, new System.Text.UTF8Encoding(false));
            }
            else
            {
                Console.WriteLine(jsonStr);
            }
        }
    }
}
