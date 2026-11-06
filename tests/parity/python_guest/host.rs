use kardia_core::{Context, FiberState};
use kardia_hmr::{
    DylibSource, Exports, GuestService, ModuleCode, ModuleGraph, ModuleSource, ProcessSource,
    WasmSource,
};
use serde_json::json;

struct WithData(Box<dyn ModuleSource>);
impl ModuleSource for WithData {
    fn load(&self, id: &str) -> kardia_core::Result<ModuleCode> {
        if std::path::Path::new(id)
            .file_name()
            .is_some_and(|name| name == "data")
        {
            Ok(ModuleCode::new(vec![], |_| {
                Ok(Exports::Value(json!({"text":"依赖"})))
            }))
        } else {
            self.0.load(id)
        }
    }
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let source: Box<dyn ModuleSource> = match args[1].as_str() {
        "process" => Box::new(ProcessSource),
        "dylib" => Box::new(DylibSource),
        "wasm" => Box::new(WasmSource::new()),
        _ => panic!("unknown guest kind"),
    };
    let graph = ModuleGraph::new(WithData(source));
    let Exports::Plugin(definition) = graph.import(&args[2]).unwrap() else {
        panic!("expected a plugin");
    };
    let first = Context::new();
    let second = Context::new();
    let one = first
        .plugin_def(&definition, json!({"initial": 10, "unknown": 42}))
        .unwrap();
    let two = second
        .plugin_def(&definition, json!({"initial": 100}))
        .unwrap();
    one.settled().await.unwrap();
    two.settled().await.unwrap();
    assert_eq!(one.state(), FiberState::Active);
    assert_eq!(two.state(), FiberState::Active);
    let counter = first.get_as::<GuestService>("counter").unwrap();
    let other = second.get_as::<GuestService>("counter").unwrap();
    assert_eq!(counter.call("increment", json!(2)).unwrap(), json!(12));
    assert_eq!(counter.call("increment", json!(3)).unwrap(), json!(15));
    assert_eq!(other.call("increment", json!(1)).unwrap(), json!(101));
    assert_eq!(
        counter.call("view", json!("counter")).unwrap(),
        json!({"initial":10,"imports":{}})
    );
    for (method, message) in [
        ("fail", "counter refuses"),
        ("raise", "the guest panicked: Python exception 你好"),
        ("exit", "the guest panicked: guest requested exit"),
        ("unknown", "counter has no method unknown"),
    ] {
        assert_eq!(
            counter.call(method, json!(null)).unwrap_err().to_string(),
            message
        );
    }
    assert!(
        counter
            .call(
                "invoke",
                json!({"key":"counter","method":"increment","args":1})
            )
            .unwrap_err()
            .to_string()
            .contains("re-entrant")
    );
    assert!(counter.call("view", json!("missing")).is_err());
    assert!(
        first
            .log_buffer()
            .iter()
            .any(|message| message.text == "Python guest applied 你好")
    );
    if let Some(path) = args.get(3) {
        first.provide_serialized_as("nothing", json!(null)).unwrap();
        let Exports::Plugin(proxy_def) = graph.import(path).unwrap() else {
            panic!("expected proxy")
        };
        let proxy_fiber = first.plugin_def(&proxy_def, json!({"initial":2})).unwrap();
        proxy_fiber.settled().await.unwrap();
        let proxy = first.get_as::<GuestService>("proxy").unwrap();
        let dependency = std::path::Path::new(path).parent().unwrap().join("data");
        assert_eq!(
            proxy.value(),
            &json!({"imports":{(dependency.to_str().unwrap()):{"text":"依赖"}}})
        );
        assert_eq!(counter.call("increment", json!(0)).unwrap(), json!(17));
        assert_eq!(
            proxy
                .call(
                    "invoke",
                    json!({"key":"counter","method":"increment","args":3})
                )
                .unwrap(),
            json!(20)
        );
        assert_eq!(
            proxy.call("view", json!("counter")).unwrap(),
            json!({"initial":10,"imports":{}})
        );
        if args[1] == "process" {
            assert_eq!(
                proxy
                    .call("view", json!("nothing"))
                    .unwrap_err()
                    .to_string(),
                "EOF while parsing a value at line 1 column 0"
            );
        } else {
            assert_eq!(proxy.call("view", json!("nothing")).unwrap(), json!(null));
        }
        proxy_fiber.dispose().await.unwrap();
    }
    one.dispose().await.unwrap();
    assert!(first.view("counter").is_err());
    assert!(
        counter
            .call("increment", json!(1))
            .unwrap_err()
            .to_string()
            .contains("disposed")
    );
    assert!(
        first
            .log_buffer()
            .iter()
            .any(|message| message.text == "Python guest disposed")
    );
    two.dispose().await.unwrap();
    println!(
        "Rust host accepted Python {} SDK: state, config, callbacks, UTF-8, errors, disposal",
        args[1]
    );
}
