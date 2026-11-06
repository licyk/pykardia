# Original Rust guest acceptance fixture

`sdk/src/*.rs` and `fixture/src/*.rs` are unmodified copies from
`cordis-rs` commit `0ce472c391b6a8bf4fafa443000348e9e48af44a`:

- `crates/kardia-guest/src/`
- `crates/kardia-hmr/tests/fixtures/rust-guest/src/`

They retain the original GPL-3.0-only license (repository LICENSE and NOTICE).
The two Cargo manifests only detach these test sources from the original workspace.
The generated fixture Cargo.lock matches the original fixture's lockfile.
Do not modify these sources to make Python implementations pass.

`build.py` uses Rust 1.96.0 and `wasm32-unknown-unknown` for real process,
dynamic-library and WASM artifacts, with v1/v2 kept as separate hard links.
Rust is needed for these acceptance tests, not to import or run the Python package.
Build outputs live under the OS temporary directory and are not distributed.
