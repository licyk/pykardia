use kardia_core::{Context, FiberState};
use kardia_hmr::{Exports, GuestService, ModuleGraph, WasmSource};
use serde_json::json;

#[tokio::main]
async fn main() {
    let path = std::env::args().nth(1).expect("guest wasm path");
    let graph = ModuleGraph::new(WasmSource::new());
    let Exports::Plugin(definition) = graph.import(&path).unwrap() else {
        panic!("expected a plugin");
    };
    let first = Context::new();
    let second = Context::new();
    let one = first
        .plugin_def(&definition, json!({"initial": 10}))
        .unwrap();
    let two = second
        .plugin_def(&definition, json!({"initial": 100}))
        .unwrap();
    one.settled().await.unwrap();
    two.settled().await.unwrap();
    assert_eq!(one.state(), FiberState::Active);
    assert_eq!(two.state(), FiberState::Active);
    assert_eq!(
        first.view("counter").unwrap(),
        json!({"initial": 10, "language": "Python"})
    );
    let counter = first.get_as::<GuestService>("counter").unwrap();
    assert_eq!(
        counter.call("increment", json!(2)).unwrap(),
        json!({"count": 12, "text": "你好"})
    );
    assert_eq!(
        counter.call("increment", json!(3)).unwrap(),
        json!({"count": 15, "text": "你好"})
    );
    let other = second.get_as::<GuestService>("counter").unwrap();
    assert_eq!(
        other.call("increment", json!(1)).unwrap(),
        json!({"count": 101, "text": "你好"})
    );
    assert!(
        counter
            .call("missing", json!(0))
            .unwrap_err()
            .to_string()
            .contains("counter has no method missing")
    );
    one.dispose().await.unwrap();
    assert!(first.view("counter").is_err());
    assert!(
        first
            .log_buffer()
            .iter()
            .any(|message| message.text == "Python guest disposed")
    );
    two.dispose().await.unwrap();
    println!(
        "Rust host verified Python WASM meta/apply/call/dispose, UTF-8, errors and isolated state"
    );
}
