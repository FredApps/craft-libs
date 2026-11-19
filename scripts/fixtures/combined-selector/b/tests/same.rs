#[test]
fn gated_same_name() { assert!(cfg!(feature = "gate")); }
