fn main() {}
#[test]
fn gated_example() { assert!(cfg!(feature = "gate")); }
