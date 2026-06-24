use std::fs;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let data = fs::read(&args[1]).expect("Failed to read");
    let jmap: jmap::Jmap = serde_json::from_slice(&data).expect("Failed to parse jmap");
    
    let mut structs = Vec::new();
    for (path, obj) in &jmap.objects {
        if let jmap::ObjectType::Class(c) = obj {
            structs.push(serde_json::json!({
                "name": path.rsplit('.').next().unwrap_or(path),
                "path": path,
                "super": c.r#struct.super_struct,
            }));
        } else if let jmap::ObjectType::ScriptStruct(s) = obj {
            structs.push(serde_json::json!({
                "name": path.rsplit('.').next().unwrap_or(path),
                "path": path,
                "super": s.r#struct.super_struct,
            }));
        }
    }
    
    let output = serde_json::json!({"structs": structs});
    let out_path = args.get(2).map(|s| s.as_str()).unwrap_or("-");
    if out_path == "-" {
        println!("{}", serde_json::to_string_pretty(&output).unwrap());
    } else {
        fs::write(out_path, serde_json::to_string_pretty(&output).unwrap()).unwrap();
        eprintln!("Done: {} structs -> {}", structs.len(), out_path);
    }
}
