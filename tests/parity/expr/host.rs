//! Observe expression values and located failures from the original evaluator.
use std::io::Read;
use kardia_core::Context;
use kardia_loader::expr::{ExprError, interpolate};
use serde_json::{Value, json};

fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let input: Value = serde_json::from_str(&input).unwrap();
    let ctx = Context::new();
    for (name, value) in input["services"].as_object().unwrap() {
        ctx.provide_as(name.as_str(), value.clone()).unwrap();
    }
    let output: Vec<_> = input["expressions"].as_array().unwrap().iter().map(|source| {
        let source = source.as_str().unwrap();
        match interpolate(&ctx, &json!({"config": [{"__jsExpr": source}]})) {
            Ok(value) => json!({"value": value}),
            Err(error) => {
                let error = error.downcast_ref::<ExprError>().unwrap();
                json!({"error": error.to_string(), "offset": error.offset,
                    "path": error.path.iter().map(ToString::to_string).collect::<Vec<_>>()})
            }
        }
    }).collect();
    println!("{}", serde_json::to_string_pretty(&output).unwrap());
}
