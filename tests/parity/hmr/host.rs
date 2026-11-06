use std::{collections::HashMap, path::PathBuf, sync::{Arc, Mutex}};
use kardia_core::{Context, Error, Event, PluginDef, Result};
use kardia_hmr::{Exports, Hmr, HmrExt, ModuleCode, ModuleGraph, ModuleSource};
use kardia_loader::Loader;
use serde_json::{Value, json};

#[derive(Clone, Default)]
struct Files(Arc<Mutex<HashMap<String, ModuleCode>>>);
impl ModuleSource for Files {
    fn load(&self, id: &str) -> Result<ModuleCode> {
        self.0.lock().unwrap().get(id).cloned().ok_or_else(|| Error::msg("missing module"))
    }
}
struct Query(&'static str);
impl Event for Query { type Output = Option<Value>; }
fn code(name: &'static str, dep: Option<&'static str>, version: &'static str, fail: bool) -> ModuleCode {
    ModuleCode::new(dep.into_iter().map(String::from).collect(), move |imports| {
        if fail { return Err(Error::msg("broken evaluation")); }
        let value = dep.and_then(|dep| imports.value(dep)).unwrap_or(json!(version));
        Ok(Exports::Plugin(PluginDef::new(move |ctx, _| {
            let value = value.clone();
            async move {
                ctx.on(move |query: &Query| (query.0 == name).then(|| value.clone()))?;
                Ok(())
            }
        })))
    })
}
fn data(version: &'static str) -> ModuleCode {
    ModuleCode::new(vec![], move |_| Ok(Exports::Value(json!(version))))
}
#[tokio::main(flavor="current_thread")]
async fn main() {
    let root = Context::new();
    let files = Files::default();
    let graph = ModuleGraph::new(files.clone());
    let mut trace = Vec::new();
    files.0.lock().unwrap().insert("/app/dep".into(),data("v1"));
    for name in ["left","right"] {
        files.0.lock().unwrap().insert(format!("/app/{name}"), code(name,Some("/app/dep"),"",false));
    }
    files.0.lock().unwrap().insert("/app/other".into(),code("other",None,"v1",false));
    root.plugin_def(&Loader::plugin(graph.clone()),Value::Null).unwrap().settled().await.unwrap();
    root.plugin_def(&Hmr::plugin(graph.clone()),Value::Null).unwrap().settled().await.unwrap();
    let loader = root.get::<Loader>().unwrap();
    let hmr = root.get::<Hmr>().unwrap();
    for name in ["left","right","other"] {
        loader.create(json!({"id":name,"name":format!("/app/{name}"),"config":{"marker":name}}),None,None).await.unwrap();
    }
    loader.settled().await;
    let original: Vec<_> = ["left","right","other"].iter().map(|id|loader.resolve(id).unwrap().inner_fiber().unwrap()).collect();
    trace.push(json!(["initial",graph.loaded(),root.bail(&Query("left")).flatten(),root.bail(&Query("right")).flatten(),root.bail(&Query("other")).flatten()]));
    for stage in ["abort shared", "recover", "skip host"] {
        let roots: Vec<PathBuf>;
        if stage == "abort shared" {
            files.0.lock().unwrap().insert("/app/dep".into(),data("v2"));
            files.0.lock().unwrap().insert("/app/left".into(),code("left",Some("/app/dep"),"",true));
            files.0.lock().unwrap().insert("/app/other".into(),code("other",None,"v2",false));
            roots=vec!["/app/dep".into(),"/app/left".into(),"/app/other".into()];
        } else if stage == "recover" {
            files.0.lock().unwrap().insert("/app/left".into(),code("left",Some("/app/dep"),"",false));
            roots=vec!["/app/dep".into()];
        } else {
            graph.mark_host(["/app/other"]);
            files.0.lock().unwrap().insert("/app/other".into(),code("other",None,"v3",false));
            roots=vec!["/app/other".into()];
        }
        let result=hmr.reconcile(||async{Ok(roots)}).await.unwrap();
        // HashSet restoration order is unspecified in Rust; record per-entry
        // membership, preserving failure-module order and all source configs.
        let settled: Vec<_>=original.iter().map(|fiber|result.settled_fibers.contains(fiber)).collect();
        let failures: Vec<_>=result.import_failures.iter().map(|failure|json!([failure.modules,failure.error.to_string()])).collect();
        let identity: Vec<_>=["left","right","other"].iter().zip(&original).map(|(id,fiber)|loader.resolve(id).unwrap().inner_fiber().as_ref()==Some(fiber)).collect();
        trace.push(json!([stage,settled,failures,result.skipped_host_modules,identity,
            root.bail(&Query("left")).flatten(),root.bail(&Query("right")).flatten(),root.bail(&Query("other")).flatten(),
            graph.import("/app/dep").ok().and_then(|e|if let Exports::Value(v)=e{Some(v)}else{None}),loader.root().data()]));
    }
    root.fiber().dispose().await.unwrap();
    trace.push(json!(["shutdown",root.bail(&Query("left")).flatten(),root.bail(&Query("right")).flatten(),root.bail(&Query("other")).flatten()]));
    println!("{}",serde_json::to_string(&trace).unwrap());
}
