use kardia_core::{Context, PluginDef};
use kardia_loader::{Loader, builtins};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};

type Trace = Arc<Mutex<Vec<Value>>>;
fn record(trace: &Trace, value: Value) { trace.lock().unwrap().push(value); }
fn plugin(trace: &Trace, label: &'static str) -> PluginDef {
    let trace = trace.clone();
    PluginDef::new(move |ctx, config| {
        let trace = trace.clone();
        async move {
            record(&trace, json!([label, "apply", config]));
            ctx.on_dispose(move || record(&trace, json!([label, "dispose"])))?;
            Ok(())
        }
    })
}
#[tokio::main(flavor = "current_thread")]
async fn main() {
    let root = Context::new();
    let trace: Trace = Default::default();
    let catalog = builtins();
    catalog.register_def("foo", plugin(&trace, "foo"));
    catalog.register_def("bar", plugin(&trace, "bar"));
    root.plugin_def(&Loader::plugin(catalog), Value::Null).unwrap().settled().await.unwrap();
    let loader = root.get::<Loader>().unwrap();
    loader.create(json!({"id":"g","name":"group","group":true,"config":[]}), None, None).await.unwrap();
    loader.create(json!({"id":"x","name":"foo","config":{"v":1},"custom":"keep"}), None, None).await.unwrap();
    loader.settled().await;
    let entry = loader.resolve("x").unwrap();
    let fiber = entry.inner_fiber().unwrap();
    record(&trace, json!(["source", loader.root().data()]));
    loader.update("x", json!({}), Some(Some("g")), None).await.unwrap();
    loader.settled().await;
    record(&trace, json!(["move identity", entry == loader.resolve("x").unwrap(), fiber.id() == entry.inner_fiber().unwrap().id()]));
    loader.update("x", json!({"config":{"v":2}}), None, None).await.unwrap();
    loader.settled().await;
    loader.update("x", json!({"name":"bar"}), None, None).await.unwrap();
    loader.settled().await;
    record(&trace, json!(["rename identity", fiber.id() == entry.inner_fiber().unwrap().id()]));
    loader.update("g", json!({"disabled":true}), None, None).await.unwrap();
    loader.settled().await;
    record(&trace, json!(["disabled", entry.is_disabled(), loader.root().data()]));
    loader.update("g", json!({"disabled":null}), None, None).await.unwrap();
    loader.settled().await;
    entry.inner_fiber().unwrap().update(json!({"v":3})).unwrap().await.unwrap();
    record(&trace, json!(["self update", entry.options()]));
    entry.inner_fiber().unwrap().dispose().await.unwrap();
    loader.settled().await;
    record(&trace, json!(["self dispose", entry.options()]));
    loader.remove("g").unwrap();
    loader.settled().await;
    record(&trace, json!(["empty", loader.root().data(), loader.entries().len()]));
    root.fiber().dispose().await.unwrap();
    println!("{}", serde_json::to_string(&*trace.lock().unwrap()).unwrap());
}
