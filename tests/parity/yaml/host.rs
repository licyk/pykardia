use kardia_include::yaml;
use kardia_core::PathSegment;
use serde_json::{json, Value};
use std::io::{self, Read};
fn path(value: &Value) -> Vec<PathSegment> {
    value.as_array().unwrap().iter().map(|v| if let Some(k) = v.as_str() { PathSegment::Key(k.to_string()) } else { PathSegment::Index(v.as_u64().unwrap() as usize) }).collect()
}
fn main() {
    let mut input = String::new(); io::stdin().read_to_string(&mut input).unwrap();
    let input: Value = serde_json::from_str(&input).unwrap();
    let output: Vec<Value> = input.as_array().unwrap().iter().map(|case| {
        match yaml::parse(case["source"].as_str().unwrap()) {
            Err(error) => json!({"error": error.message,"position":[error.position.line,error.position.column]}),
            Ok((value,syntax)) => {
                let positions: Vec<Value> = case["paths"].as_array().unwrap().iter().map(|p| syntax.locate(&path(p)).map(|p| json!([p.line,p.column])).unwrap_or(Value::Null)).collect();
                let expressions: Vec<Value> = case["exprs"].as_array().unwrap().iter().map(|p| syntax.locate_expr(&path(&p[0]),p[1].as_u64().unwrap() as usize).map(|p| json!([p.line,p.column])).unwrap_or(Value::Null)).collect();
                json!({"value":value,"dump":yaml::dump(&value),"positions":positions,"expressions":expressions})
            }
        }
    }).collect();
    println!("{}",serde_json::to_string(&output).unwrap());
}
