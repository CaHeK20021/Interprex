using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Mono.Cecil;
using Mono.Cecil.Cil;

namespace DllEditor
{
    class ExtractedString
    {
        public string original { get; set; } = "";
        public List<string> path { get; set; } = new List<string>();
        public string context { get; set; } = "";
    }

    class Program
    {
        static int Main(string[] args)
        {
            if (args.Length < 2)
            {
                Console.WriteLine("Usage:");
                Console.WriteLine("  DllEditor extract <dll_path>");
                Console.WriteLine("  DllEditor find-tech <dll_path>");
                Console.WriteLine("  DllEditor inject <dll_path> <translations_json_path>");
                return 1;
            }

            string command = args[0].ToLower();
            string dllPath = args[1];

            if (!File.Exists(dllPath))
            {
                Console.Error.WriteLine($"Error: DLL file not found: {dllPath}");
                return 2;
            }

            try
            {
                if (command == "extract")
                {
                    return ExtractStrings(dllPath, false);
                }
                else if (command == "find-tech")
                {
                    return ExtractStrings(dllPath, true);
                }
                else if (command == "inject")
                {
                    if (args.Length < 3)
                    {
                        Console.Error.WriteLine("Error: Missing translations JSON file path.");
                        return 1;
                    }
                    string jsonPath = args[2];
                    if (!File.Exists(jsonPath))
                    {
                        Console.Error.WriteLine($"Error: JSON file not found: {jsonPath}");
                        return 2;
                    }
                    return InjectStrings(dllPath, jsonPath);
                }
                else
                {
                    Console.Error.WriteLine($"Error: Unknown command '{command}'");
                    return 1;
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"Fatal Error: {ex.Message}");
                Console.Error.WriteLine(ex.StackTrace);
                return 3;
            }
        }

        static int ExtractStrings(string dllPath, bool onlyTechnical)
        {
            using (var assembly = AssemblyDefinition.ReadAssembly(dllPath))
            {
                var strings = new List<ExtractedString>();
                foreach (var module in assembly.Modules)
                {
                    foreach (var type in module.Types)
                    {
                        ExtractFromType(type, strings, onlyTechnical);
                    }
                }

                var jsonOptions = new JsonSerializerOptions { WriteIndented = true };
                string json = JsonSerializer.Serialize(strings, jsonOptions);
                Console.WriteLine(json);
            }
            return 0;
        }

        private static readonly HashSet<string> SkipNamespaces = new(StringComparer.OrdinalIgnoreCase)
        {
            "UnityStandardAssets",
            "ProBuilder",
            "UnityEngine.ProBuilder",
            "UnityEngine.Timeline",
            "UnityEngine.Playables", 
            "Unity.Collections",
            "Unity.TextMeshPro",
            "TMPro",
            "Unity.Analytics",
            "Unity.Services",
            "Newtonsoft.Json",
            "AstarPathfindingProject",
            "Pathfinding"
        };

        static bool ShouldSkipType(string typeFullName)
        {
            if (string.IsNullOrEmpty(typeFullName)) return false;
            foreach (var ns in SkipNamespaces)
            {
                if (typeFullName.StartsWith(ns + ".") || typeFullName == ns)
                {
                    return true;
                }
            }
            return false;
        }

        static void ExtractFromType(TypeDefinition type, List<ExtractedString> strings, bool onlyTechnical)
        {
            if (ShouldSkipType(type.FullName)) return;

            foreach (var method in type.Methods)
            {
                if (method.HasBody)
                {
                    var techVars = GetTechnicalVariables(method);
                    int strIdx = 0;
                    foreach (var instr in method.Body.Instructions)
                    {
                        if (instr.OpCode == OpCodes.Ldstr)
                        {
                            string val = (string)instr.Operand;
                            
                            bool isTech = IsTechnicalString(val) || IsTechnicalUsage(instr, method.Body, techVars);



                            if (onlyTechnical)
                            {
                                if (isTech)
                                {
                                    var path = new List<string>();
                                    if (!string.IsNullOrEmpty(type.FullName)) path.Add(type.FullName);
                                    if (!string.IsNullOrEmpty(method.Name)) path.Add(method.Name);
                                    path.Add($"str_{strIdx}");

                                    strings.Add(new ExtractedString
                                    {
                                        original = val,
                                        path = path,
                                        context = $"Class: {type.FullName}, Method: {method.Name} (Technical)"
                                    });
                                }
                            }
                            else
                            {
                                if (!isTech)
                                {
                                    var path = new List<string>();
                                    if (!string.IsNullOrEmpty(type.FullName)) path.Add(type.FullName);
                                    if (!string.IsNullOrEmpty(method.Name)) path.Add(method.Name);
                                    path.Add($"str_{strIdx}");

                                    strings.Add(new ExtractedString
                                    {
                                        original = val,
                                        path = path,
                                        context = $"Class: {type.FullName}, Method: {method.Name}"
                                    });
                                }
                            }

                            if (!isTech)
                            {
                                strIdx++;
                            }
                        }
                    }
                }
            }

            foreach (var nestedType in type.NestedTypes)
            {
                ExtractFromType(nestedType, strings, onlyTechnical);
            }
        }

        static int InjectStrings(string dllPath, string jsonPath)
        {
            string jsonText = File.ReadAllText(jsonPath);
            var translations = JsonSerializer.Deserialize<Dictionary<string, string>>(jsonText);
            if (translations == null)
            {
                Console.Error.WriteLine("Error: Failed to parse translations JSON.");
                return 4;
            }

            int replacedCount = 0;

            using (var assembly = AssemblyDefinition.ReadAssembly(dllPath, new ReaderParameters { ReadWrite = true }))
            {
                foreach (var module in assembly.Modules)
                {
                    foreach (var type in module.Types)
                    {
                        replacedCount += InjectIntoType(type, translations);
                    }
                }

                if (replacedCount > 0)
                {
                    assembly.Write();
                }
            }

            Console.WriteLine($"SUCCESS:{replacedCount}");
            return 0;
        }

        static int InjectIntoType(TypeDefinition type, Dictionary<string, string> translations)
        {
            if (ShouldSkipType(type.FullName)) return 0;
            int replacedCount = 0;

            foreach (var method in type.Methods)
            {
                if (method.HasBody)
                {
                    var techVars = GetTechnicalVariables(method);
                    int strIdx = 0;
                    foreach (var instr in method.Body.Instructions)
                    {
                        if (instr.OpCode == OpCodes.Ldstr)
                        {
                            string val = (string)instr.Operand;
                            
                            bool isTech = IsTechnicalString(val) || IsTechnicalUsage(instr, method.Body, techVars);
                            if (isTech) continue;

                            var path = new List<string>();
                            if (!string.IsNullOrEmpty(type.FullName)) path.Add(type.FullName);
                            if (!string.IsNullOrEmpty(method.Name)) path.Add(method.Name);
                            path.Add($"str_{strIdx}");

                            string pathStr = string.Join("\x01", path);
                            if (translations.TryGetValue(pathStr, out string? translated))
                            {
                                instr.Operand = translated;
                                replacedCount++;
                            }
                            strIdx++;
                        }
                    }
                }
            }

            foreach (var nestedType in type.NestedTypes)
            {
                replacedCount += InjectIntoType(nestedType, translations);
            }

            return replacedCount;
        }

        static VariableDefinition? GetVariable(Instruction instr, MethodBody body)
        {
            Code code = instr.OpCode.Code;
            if (code == Code.Ldloc_0 || code == Code.Stloc_0) return body.Variables.Count > 0 ? body.Variables[0] : null;
            if (code == Code.Ldloc_1 || code == Code.Stloc_1) return body.Variables.Count > 1 ? body.Variables[1] : null;
            if (code == Code.Ldloc_2 || code == Code.Stloc_2) return body.Variables.Count > 2 ? body.Variables[2] : null;
            if (code == Code.Ldloc_3 || code == Code.Stloc_3) return body.Variables.Count > 3 ? body.Variables[3] : null;
            
            if (code == Code.Ldloc || code == Code.Ldloc_S || code == Code.Stloc || code == Code.Stloc_S)
            {
                return instr.Operand as VariableDefinition;
            }
            return null;
        }

        // Variable dataflow analysis: linear only, branching not supported.
        // Known limitation: Class fields and array elements are not tracked.
        static HashSet<VariableDefinition> GetTechnicalVariables(MethodDefinition method)
        {
            var techVars = new HashSet<VariableDefinition>();
            if (!method.HasBody) return techVars;

            var instructions = method.Body.Instructions;
            for (int i = 0; i < instructions.Count; i++)
            {
                var instr = instructions[i];
                if (instr.OpCode == OpCodes.Call || instr.OpCode == OpCodes.Callvirt)
                {
                    if (instr.Operand is MethodReference methodRef)
                    {
                        string declType = methodRef.DeclaringType.FullName;
                        string methodName = methodRef.Name;

                        if (IsTechnicalMethod(declType, methodName))
                        {
                            // Look back up to 4 instructions for a string variable load
                            for (int j = i - 1; j >= Math.Max(0, i - 4); j--)
                            {
                                var prevInstr = instructions[j];
                                var variable = GetVariable(prevInstr, method.Body);
                                if (variable != null && variable.VariableType.FullName == "System.String")
                                {
                                    techVars.Add(variable);
                                }
                            }
                        }
                    }
                }
            }
            return techVars;
        }

        static bool IsTechnicalMethod(string typeName, string methodName)
        {
            // Input
            if (typeName == "UnityEngine.Input" || typeName == "UnityStandardAssets.CrossPlatformInput.CrossPlatformInputManager")
            {
                return methodName == "GetAxis" || methodName == "GetAxisRaw" ||
                       methodName == "GetButton" || methodName == "GetButtonDown" || methodName == "GetButtonUp" ||
                       methodName.StartsWith("GetKey") || methodName.StartsWith("Set") || methodName.StartsWith("Register");
            }


            // GameObject / Component / Transform
            if (typeName == "UnityEngine.GameObject" || typeName == "UnityEngine.Component")
            {
                return methodName == "FindWithTag" || methodName == "FindGameObjectsWithTag" || 
                       methodName == "Find" || methodName == "CompareTag" ||
                       methodName == "GetComponent" || methodName == "GetComponentInChildren" || 
                       methodName == "GetComponentInParent" || methodName == "GetComponents" ||
                       methodName == "AddComponent";
            }
            if (typeName == "UnityEngine.Transform")
            {
                return methodName == "Find";
            }

            // MonoBehaviour
            if (typeName == "UnityEngine.MonoBehaviour")
            {
                return methodName == "Invoke" || methodName == "InvokeRepeating" || 
                       methodName == "StartCoroutine" || methodName == "StopCoroutine" || 
                       methodName == "SendMessage" || methodName == "SendMessageUpwards" || 
                       methodName == "BroadcastMessage" || methodName == "IsInvoking";
            }

            // SceneManager
            if (typeName == "UnityEngine.SceneManagement.SceneManager")
            {
                return methodName == "LoadScene" || methodName == "LoadSceneAsync" ||
                       methodName == "UnloadScene" || methodName == "UnloadSceneAsync" ||
                       methodName == "GetSceneByName" || methodName == "GetSceneByPath";
            }

            // Animator
            if (typeName == "UnityEngine.Animator")
            {
                return methodName == "StringToHash" || methodName == "Play" || methodName == "PlayInFixedTime" ||
                       methodName == "CrossFade" || methodName == "SetTrigger" || methodName == "ResetTrigger" ||
                       methodName == "SetBool" || methodName == "SetFloat" || methodName == "SetInteger" ||
                       methodName == "GetFloat" || methodName == "GetBool" || methodName == "GetInteger";
            }

            // Animation
            if (typeName == "UnityEngine.Animation")
            {
                return methodName == "Play" || methodName == "CrossFade" || methodName == "Blend" || 
                       methodName == "Stop" || methodName == "PlayQueued" || methodName == "CrossFadeQueued";
            }

            // Shader & Material & MaterialPropertyBlock
            if (typeName == "UnityEngine.Shader")
            {
                return methodName == "PropertyToID" || methodName == "Find";
            }
            if (typeName == "UnityEngine.Material" || typeName == "UnityEngine.MaterialPropertyBlock")
            {
                return methodName.StartsWith("Set") || methodName.StartsWith("Get") || methodName == "HasProperty" ||
                       methodName == "EnableKeyword" || methodName == "DisableKeyword";
            }

            // AudioMixer
            if (typeName == "UnityEngine.Audio.AudioMixer")
            {
                return methodName == "SetFloat" || methodName == "GetFloat";
            }

            // Resources
            if (typeName == "UnityEngine.Resources")
            {
                return methodName == "Load" || methodName == "LoadAsync" || methodName == "LoadAll";
            }

            // AssetBundle
            if (typeName == "UnityEngine.AssetBundle")
            {
                return methodName == "LoadAsset" || methodName == "LoadAssetAsync" || methodName == "Contains";
            }

            // PlayerPrefs
            if (typeName == "UnityEngine.PlayerPrefs")
            {
                return methodName.StartsWith("Get") || methodName.StartsWith("Set") ||
                       methodName == "HasKey" || methodName == "DeleteKey";
            }

            return false;
        }

        static bool IsUnityTechnicalCall(Instruction ldstrInstr)
        {
            Instruction? cur = ldstrInstr.Next;
            int limit = 3;
            while (cur != null && limit > 0)
            {
                if (cur.OpCode == OpCodes.Call || cur.OpCode == OpCodes.Callvirt)
                {
                    if (cur.Operand is MethodReference methodRef)
                    {
                        string declType = methodRef.DeclaringType.FullName;
                        string methodName = methodRef.Name;

                        if (IsTechnicalMethod(declType, methodName))
                        {
                            return true;
                        }
                    }
                    break;
                }
                
                if (cur.OpCode == OpCodes.Ret || cur.OpCode == OpCodes.Br || cur.OpCode == OpCodes.Br_S)
                {
                    break;
                }

                cur = cur.Next;
                limit--;
            }
            return false;
        }



        static bool IsStoredInTechnicalVariable(Instruction ldstrInstr, MethodBody body, HashSet<VariableDefinition> techVars)
        {
            if (techVars.Count == 0) return false;

            Instruction? next = ldstrInstr.Next;
            if (next != null)
            {
                var variable = GetVariable(next, body);
                if (variable != null && techVars.Contains(variable))
                {
                    return true;
                }
            }
            return false;
        }

        static bool IsTechnicalUsage(Instruction ldstrInstr, MethodBody body, HashSet<VariableDefinition> techVars)
        {
            return IsUnityTechnicalCall(ldstrInstr) || IsStoredInTechnicalVariable(ldstrInstr, body, techVars);
        }

        static bool IsTechnicalString(string s)
        {
            if (string.IsNullOrWhiteSpace(s)) return true;

            // Check if contains letters
            bool hasLetters = false;
            foreach (char c in s)
            {
                if (char.IsLetter(c))
                {
                    hasLetters = true;
                    break;
                }
            }
            if (!hasLetters) return true;

            // File paths / asset paths
            if (s.Contains("/") || s.Contains("\\"))
            {
                string normalized = s.Replace("\\", "/");
                int lastSlash = normalized.LastIndexOf('/');
                string lastPart = lastSlash >= 0 ? normalized.Substring(lastSlash + 1) : normalized;
                if (lastPart.Contains("."))
                {
                    int lastDot = lastPart.LastIndexOf('.');
                    string ext = lastPart.Substring(lastDot + 1).ToLower();
                    var techExts = new HashSet<string>
                    {
                        "png", "jpg", "jpeg", "tga", "wav", "mp3", "ogg", "prefab",
                        "asset", "json", "xml", "txt", "dll", "cs", "shader",
                        "cginc", "unity", "fbx", "mat"
                    };
                    if (techExts.Contains(ext)) return true;
                }
            }

            // Internal keys (no spaces, has underscores)
            if (!s.Contains(" ") && s.Contains("_")) return true;

            // Common technical words (as a fallback)
            var commonTechWords = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "MainCamera", "Player", "GameController", "Untagged", "Respawn", "Finish", "EditorOnly",
                "Horizontal", "Vertical", "Jump", "Mouse X", "Mouse Y",
                "Invisible", "Visible", "open", "Switch", "Trip",
                "\\n", "\\r", "\\t", "\\r\\n"
            };
            if (commonTechWords.Contains(s)) return true;

            return false;
        }
    }
}

