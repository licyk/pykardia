use kardia_hmr::{ModuleGraph, WasmSource};
fn main() {
    for path in std::env::args().skip(1) {
        let graph = ModuleGraph::new(WasmSource::new());
        let result = match graph.import(&path) {
            Ok(_) => "ok".to_string(),
            Err(error) => error.to_string().replace(&path, "<module>"),
        };
        println!(
            "{}",
            serde_json::json!([
                std::path::Path::new(&path)
                    .file_stem()
                    .unwrap()
                    .to_str()
                    .unwrap(),
                result
            ])
        );
    }
}
