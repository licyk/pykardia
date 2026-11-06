use kardia_core::{Context, Disposer, PluginDef};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};

type Trace = Arc<Mutex<Vec<Value>>>;
fn record(trace: &Trace, value: Value) {
    trace.lock().unwrap().push(value);
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let root = Context::new();
    let trace: Trace = Default::default();
    let provider_trace = trace.clone();
    let provider = PluginDef::new(move |ctx, config| {
        let trace = provider_trace.clone();
        async move {
            let version = config.as_i64().unwrap();
            ctx.provide_as("foo", version)?;
            record(&trace, json!(["provider apply", version]));
            ctx.on_dispose(move || record(&trace, json!(["provider cleanup", version])))?;
            Ok(())
        }
    })
    .name("provider");
    let consumer_trace = trace.clone();
    let consumer = PluginDef::new(move |ctx, config| {
        let trace = consumer_trace.clone();
        async move {
            let value = *ctx.get_as::<i64>("foo")?;
            record(&trace, json!(["consumer apply", value, config]));
            let reader = ctx.clone();
            ctx.defer(
                "read",
                Disposer::future(move || async move {
                    tokio::task::yield_now().await;
                    let value = *reader.get_as::<i64>("foo").unwrap();
                    record(&trace, json!(["consumer cleanup", value]));
                }),
            )?;
            Ok(())
        }
    })
    .inject(["foo"])
    .name("consumer");
    let dependent = root.plugin_def(&consumer, json!("old")).unwrap();
    record(
        &trace,
        json!(["initial", format!("{:?}", dependent.state())]),
    );
    let supplying = root.plugin_def(&provider, json!(1)).unwrap();
    supplying.settled().await.unwrap();
    dependent.settled().await.unwrap();
    let first = supplying.update(json!(2)).unwrap();
    let second = dependent.update(json!("new")).unwrap();
    let (a, b) = tokio::join!(first, second);
    a.unwrap();
    b.unwrap();
    dependent.settled().await.unwrap();
    let replacement_trace = trace.clone();
    let replacement = PluginDef::new(move |_, config| {
        record(&replacement_trace, json!(["replacement apply", config]));
        async { Ok(()) }
    });
    root.registry().replace(&consumer, &replacement);
    dependent.settled().await.unwrap();
    record(
        &trace,
        json!([
            "identity",
            dependent.id().get(),
            supplying.id().get(),
            dependent.config()
        ]),
    );
    supplying.restart().await.unwrap();
    root.fiber().dispose().await.unwrap();
    record(
        &trace,
        json!([
            "final",
            root.registry().size(),
            root.fiber().effect_count(),
            root.bindings("foo").len(),
            format!("{:?}", root.fiber().state())
        ]),
    );
    println!(
        "{}",
        serde_json::to_string(&*trace.lock().unwrap()).unwrap()
    );
}
