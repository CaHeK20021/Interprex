import json, sys, time

def main():
    from parsers.unreal4_5 import UnrealEngine4_5Parser
    P=r"G:/SteamLibrary/steamapps/common/Satisfactory/Interprex/project.json"
    d=json.load(open(P,encoding="utf-8"))
    root=d["root"]
    trans={k:v["translated"] for k,v in d["strings"].items() if v.get("translated")}
    print(f"[regen] {len(trans)} translations, root={root}", flush=True)
    p=UnrealEngine4_5Parser()
    t0=time.time()
    n=p._inject_into_uassets(root, trans, None)
    print(f"[regen] wrote {n} property patches in {time.time()-t0:.0f}s", flush=True)

if __name__ == "__main__":
    main()
