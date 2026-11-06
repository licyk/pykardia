use serde_json::{Value,json};
use std::io::{self, Read};
fn main() {
    let mut input = String::new(); io::stdin().read_to_string(&mut input).unwrap();
    let cases: Vec<String> = serde_json::from_str(&input).unwrap();
    let output: Vec<Value> = cases.iter().map(|text| match serde_json::from_str::<Value>(text) {
        Ok(value) => json!({"pretty":serde_json::to_string_pretty(&value).unwrap(),"value":value}),
        Err(error) => json!({"error":error.to_string().split(" at line").next().unwrap(),"position":[error.line(),error.column()]})
    }).collect();
    println!("{}",serde_json::to_string(&output).unwrap());
}
