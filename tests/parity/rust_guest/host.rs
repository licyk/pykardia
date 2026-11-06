use kardia_core::Context;
use kardia_hmr::{DylibSource, Exports, GuestService, ModuleGraph, ProcessSource, WasmSource};
use serde_json::json;

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let graph = match args[1].as_str() {
        "process" => ModuleGraph::new(ProcessSource),
        "dylib" => ModuleGraph::new(DylibSource),
        "wasm" => ModuleGraph::new(WasmSource::new()),
        _ => panic!("unknown guest kind"),
    };
    let Exports::Plugin(definition) = graph.import(&args[2]).unwrap() else {
        panic!("expected plugin")
    };
    let root = Context::new();
    let second = Context::new();
    let one = root
        .plugin_def(&definition, json!({"text":"你好"}))
        .unwrap();
    let two = second.plugin_def(&definition, json!(null)).unwrap();
    one.settled().await.unwrap();
    two.settled().await.unwrap();
    let guest = root.get_as::<GuestService>("greeter").unwrap();
    assert_eq!(
        guest.value(),
        &json!({"version":"v1","input":{"config":{"text":"你好"},"imports":{}}})
    );
    assert_eq!(
        guest.call("hello", json!(1)).unwrap(),
        json!({"version":"v1","calls":1,"request":{"key":"greeter","method":"hello","args":1}})
    );
    assert_eq!(
        guest.call("fail", json!(null)).unwrap_err().to_string(),
        "greeter refuses"
    );
    if args[1] != "wasm" {
        assert_eq!(
            guest.call("panic", json!(null)).unwrap_err().to_string(),
            "the guest panicked: greeter panics"
        );
    }
    assert_eq!(
        second
            .get_as::<GuestService>("greeter")
            .unwrap()
            .call("hello", json!(0))
            .unwrap()["calls"],
        json!(1)
    );
    one.dispose().await.unwrap();
    two.dispose().await.unwrap();
    assert!(
        guest
            .call("hello", json!(0))
            .unwrap_err()
            .to_string()
            .contains("disposed")
    );
    println!("Rust host accepted shared {} artifact", args[1]);
}
