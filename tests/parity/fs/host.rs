use kardia_fs::{compile, admits};
use serde_json::{Value, json};
use std::io::{self, Read};
fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    let data: Value = serde_json::from_str(&input).unwrap();
    let mut output = Vec::new();
    for case in data.as_array().unwrap() {
        let patterns: Vec<String> = case["patterns"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
        output.push(match compile(&patterns) {
            Err(error) => json!({"error":error.to_string()}),
            Ok(keep) => json!({"results":case["paths"].as_array().unwrap().iter().map(|p| {
                let path = p[0].as_str().unwrap(); let dir = p[1].as_bool().unwrap();
                json!([keep(path,dir), admits(&keep,path,dir)])
            }).collect::<Vec<_>>()})
        });
    }
    println!("{}", serde_json::to_string(&output).unwrap());
}
