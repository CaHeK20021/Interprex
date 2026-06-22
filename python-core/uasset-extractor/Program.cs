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

        static bool IsTranslatable(string value)
        {
            string v = value.Trim();
            if (v.Length == 0) return false;
            if (!v.Any(char.IsLetter)) return false;
            if (TechnicalPattern.IsMatch(v)) return false;
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
                    items.Add(new ExtractedItem
                    {
                        AssetPath = filePath,
                        InternalPath = internalPath,
                        PropName = prop.Name?.ToString() ?? "",
                        Value = valText,
                        Type = propType,
                        AssetClass = assetClass
                    });
                }
            }
        }

        static void Main(string[] args)
        {
            string filePath = "";
            string outputPath = "";
            EngineVersion version = EngineVersion.VER_UE5_4;

            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--input" && i + 1 < args.Length)
                {
                    filePath = args[i + 1];
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

            if (string.IsNullOrEmpty(filePath))
            {
                Console.WriteLine("Error: Input file path is required. Use --input <path>");
                Environment.Exit(1);
            }

            if (!File.Exists(filePath))
            {
                Console.WriteLine($"Error: File not found: {filePath}");
                Environment.Exit(1);
            }

            try
            {
                var asset = new UAsset(filePath, version);
                string internalPath = asset.FolderName?.Value ?? "";
                if (!string.IsNullOrEmpty(internalPath) && !internalPath.StartsWith("/Game/"))
                {
                    internalPath = "/Game/Mods" + internalPath;
                }
                var items = new List<ExtractedItem>();

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
            catch (Exception ex)
            {
                Console.WriteLine("Error: " + ex.Message);
                Environment.Exit(1);
            }
        }
    }
}
