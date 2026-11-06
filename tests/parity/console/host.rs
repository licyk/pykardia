//! Fixed-input console observations from the original Rust implementation.
use std::io::Read;
use kardia_core::{FiberId, Level, Message};
use kardia_logger_console::{ConsoleConfig, ConsoleExporter, color, format_duration, name_color};
use serde_json::{Value, json};

fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let input: Value = serde_json::from_str(&input).unwrap();
    let mut colors = Vec::new();
    for name in input["names"].as_array().unwrap() {
        for level in [0, 1, 2, 3, 255] {
            let name = name.as_str().unwrap();
            let code = name_color(name, level);
            colors.push(json!([code, color(level, code, name, ";1")]));
        }
    }
    let durations: Vec<_> = input["durations"].as_array().unwrap().iter()
        .map(|value| format_duration(value.as_i64().unwrap())).collect();
    let mut rendered = Vec::new();
    for row in input["render"].as_array().unwrap() {
        let config: ConsoleConfig = serde_json::from_value(row["config"].clone()).unwrap();
        let exporter = ConsoleExporter::new(config);
        let message = |ts: u64, text: &str| Message {
            sn: 1, ts, name: row["name"].as_str().unwrap().into(),
            level: Level::Info, text: text.into(), fiber: FiberId::ROOT,
        };
        exporter.render(&message(1000, "warm up"));
        rendered.push(exporter.render(&message(row["ts"].as_u64().unwrap(), row["text"].as_str().unwrap())));
    }
    println!("{}", serde_json::to_string_pretty(&json!({"colors": colors, "durations": durations, "render": rendered})).unwrap());
}
