use kardia_core::{Context, PluginDef};
use kardia_include::{Include, IncludePlugin};
use kardia_loader::{Loader, builtins};
use serde_json::{Value, json};
use std::{path::Path, sync::{Arc, Mutex}};

type Trace = Arc<Mutex<Vec<Value>>>;
fn record(trace: &Trace, value: Value) { trace.lock().unwrap().push(value); }
fn files(trace: &Trace, base: &Path, stage: &str) {
    record(trace, json!([stage, std::fs::read_to_string(base.join("outer.yml")).unwrap(), std::fs::read_to_string(base.join("inner.yml")).unwrap()]));
}
async fn settle(loader: &Loader, outer: &Include, inner: &Include) {
    inner.flush().await;
    outer.flush().await;
    loader.settled().await;
}
#[tokio::main(flavor = "current_thread")]
async fn main() {
    let base = std::env::temp_dir().join(format!("pykardia-include-probe-{}", std::process::id()));
    std::fs::create_dir_all(&base).unwrap();
    std::fs::write(base.join("inner.yml"), "# original\n- id: a\n  name: worker\n  config: 1\n").unwrap();
    std::fs::write(base.join("outer.yml"), "- id: child\n  name: include\n  config:\n    path: inner.yml\n    patches:\n      - id: a\n        config: 2\n").unwrap();
    let root = Context::new();
    let trace: Trace = Default::default();
    let observed = trace.clone();
    let worker = PluginDef::new(move |ctx, config| {
        let trace = observed.clone();
        async move {
            record(&trace, json!(["apply", config]));
            ctx.on_dispose(move || record(&trace, json!(["dispose"])))?;
            Ok(())
        }
    });
    let catalog = builtins();
    catalog.register::<IncludePlugin>();
    catalog.register_def("worker", worker);
    root.plugin_def(&Loader::plugin(catalog), Value::Null).unwrap().settled().await.unwrap();
    let loader = root.get::<Loader>().unwrap();
    loader.create(json!({"id":"inc","name":"include","config":{"path":base.join("outer.yml").to_string_lossy()}}),None,None).await.unwrap();
    loader.settled().await;
    let outer = Include::of(&loader.resolve("inc").unwrap().subtree().unwrap()).unwrap();
    let inner = Include::of(&loader.resolve("inc:child").unwrap().subtree().unwrap()).unwrap();
    files(&trace, &base, "initial");
    let original = loader.resolve("inc:child").unwrap().inner_fiber().unwrap();
    loader.update("inc:child:a", json!({"config":3}),None,None).await.unwrap();
    settle(&loader, &outer, &inner).await;
    files(&trace, &base, "patch write");
    record(&trace,json!(["include identity",original.id()==loader.resolve("inc:child").unwrap().inner_fiber().unwrap().id()]));
    loader.resolve("inc:child:a").unwrap().inner_fiber().unwrap().update(json!(4)).unwrap().await.unwrap();
    settle(&loader, &outer, &inner).await;
    files(&trace, &base, "self update");
    loader.update("inc:child", json!({"config":{"path":"inner.yml"}}),None,None).await.unwrap();
    settle(&loader, &outer, &inner).await;
    files(&trace, &base, "remove patch");
    loader.update("inc:child:a", json!({"config":5}),None,None).await.unwrap();
    settle(&loader, &outer, &inner).await;
    files(&trace, &base, "file write");
    root.fiber().dispose().await.unwrap();
    files(&trace, &base, "shutdown");
    println!("{}", serde_json::to_string(&*trace.lock().unwrap()).unwrap());
    std::fs::remove_dir_all(base).unwrap();
}
