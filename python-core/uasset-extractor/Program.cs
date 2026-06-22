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

        static bool IsTranslatable(string value)
        {
            string v = value.Trim();
            if (v.Length == 0) return false;
            if (!v.Any(char.IsLetter)) return false;
            if (TechnicalPattern.IsMatch(v)) return false;
            if (LooksLikeIdentifier(v)) return false;
            return true;
        }

        static void ExtractProps(IEnumerable<PropertyData> props, NormalExport export,
            UAsset asset, string filePath, string internalPath, string assetClass,
            List<ExtractedItem> items, bool insideStruct = false)
        {
            foreach (var prop in props)
            {
                string? valText = null;
                string propType = "Unknown";

                if (prop is TextPropertyData textProp)
                {
                    propType = "Text";
                    if (textProp.CultureInvariantString != null && !string.IsNullOrEmpty(textProp.CultureInvariantString.Value))
                        valText = textProp.CultureInvariantString.Value;
                    else if (textProp.Value != null && !string.IsNullOrEmpty(textProp.Value.Value))
                        valText = textProp.Value.Value;
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
                    ExtractProps(structProp.Value, export, asset, filePath, internalPath, assetClass, items, true);
                    continue;
                }
                else if (prop is ArrayPropertyData arrProp && arrProp.Value != null)
                {
                    foreach (var elem in arrProp.Value)
                    {
                        if (elem is StructPropertyData arrElem && arrElem.Value != null)
                            ExtractProps(arrElem.Value, export, asset, filePath, internalPath, assetClass, items, true);
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
                        AssetClass = assetClass
                    });
                }
            }
        }

        // Extract all translatable items from ONE .uasset. Returns [] (and never
        // throws) on a failed/unreadable asset so a batch run skips it cleanly.
        static List<ExtractedItem> ProcessAsset(string filePath, EngineVersion version)
        {
            var items = new List<ExtractedItem>();
            try
            {
                var asset = new UAsset(filePath, version);
                string internalPath = asset.FolderName?.Value ?? "";
                if (!string.IsNullOrEmpty(internalPath) && !internalPath.StartsWith("/Game/"))
                {
                    internalPath = "/Game/Mods" + internalPath;
                }

                foreach (var export in asset.Exports.OfType<NormalExport>())
                {
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

                    ExtractProps(export.Data, export, asset, filePath, internalPath, assetClass, items);
                }
            }
            catch
            {
                // A single bad asset must not abort a whole-directory batch run.
            }
            return items;
        }

        static void Main(string[] args)
        {
            string filePath = "";
            string inputDir = "";
            string outputPath = "";
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
                if (args[i] == "--engine" && i + 1 < args.Length)
                {
                    if (Enum.TryParse<EngineVersion>(args[i + 1], out var parsedVer))
                    {
                        version = parsedVer;
                    }
                }
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
                foreach (var f in Directory.EnumerateFiles(inputDir, "*.uasset", SearchOption.AllDirectories))
                {
                    all.AddRange(ProcessAsset(f, version));
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
