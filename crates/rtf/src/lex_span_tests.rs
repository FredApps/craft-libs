#![allow(clippy::unwrap_used)]

use super::*;

// Frozen pre-extraction tokenizer: test oracle, not a second production scanner.
#[derive(Clone, Debug)]
struct LegacyTokens<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> LegacyTokens<'a> {
    fn new(bytes: &'a [u8]) -> LegacyTokens<'a> {
        LegacyTokens { bytes, at: 0 }
    }

    /// The offset of the next token.
    fn offset(&self) -> usize {
        self.at
    }

    fn word(&mut self) -> Token<'a> {
        let b = self.bytes;
        let start = self.at + 1;
        let mut j = start;
        while b.get(j).is_some_and(u8::is_ascii_alphabetic) && j - start < 32 {
            j += 1;
        }
        let name = b.get(start..j).and_then(|w| std::str::from_utf8(w).ok()).unwrap_or("");
        let neg = b.get(j) == Some(&b'-');
        let digits = j + usize::from(neg);
        let mut k = digits;
        while b.get(k).is_some_and(u8::is_ascii_digit) && k - digits < 10 {
            k += 1;
        }
        let param = if k > digits {
            // At most 10 digits, so it fits an i64.
            let v = b.get(digits..k).and_then(|d| std::str::from_utf8(d).ok()).and_then(|d| d.parse::<i64>().ok()).unwrap_or(0);
            let v = if neg { -v } else { v };
            let dttm = matches!(name, "revdttm" | "revdttmdel" | "crdate" | "prdate");
            let v = if dttm && v > i64::from(i32::MAX) && v <= i64::from(u32::MAX) { v - (1i64 << 32) } else { v };
            Some(v.clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32)
        } else {
            k = j;
            None
        };
        if b.get(k) == Some(&b' ') {
            k += 1;
        }
        self.at = k;
        if name == "bin" {
            let n = usize::try_from(param.unwrap_or(0)).unwrap_or(0);
            let end = k.saturating_add(n).min(b.len());
            self.at = end;
            return Token::Bin(b.get(k..end).unwrap_or_default());
        }
        Token::Word { name, param }
    }
}

impl<'a> Iterator for LegacyTokens<'a> {
    type Item = Token<'a>;

    fn next(&mut self) -> Option<Token<'a>> {
        let b = self.bytes;
        loop {
            let c = *b.get(self.at)?;
            match c {
                b'{' | b'}' => {
                    self.at += 1;
                    return Some(if c == b'{' { Token::Open } else { Token::Close });
                }
                b'\r' | b'\n' | 0 => self.at += 1,
                b'\\' => {
                    let n = *b.get(self.at + 1)?;
                    if n.is_ascii_alphabetic() {
                        return Some(self.word());
                    }
                    self.at += 2;
                    match n {
                        b'\'' => {
                            let hex = b.get(self.at..self.at + 2).filter(|h| h.iter().all(u8::is_ascii_hexdigit));
                            if let Some(v) = hex.and_then(|h| std::str::from_utf8(h).ok()).and_then(|h| u8::from_str_radix(h, 16).ok()) {
                                self.at += 2;
                                return Some(Token::Hex(v));
                            }
                            // Malformed: skip the hex digits of a truncated pair, nothing else.
                            let mut n = 0;
                            while n < 2 && b.get(self.at).is_some_and(u8::is_ascii_hexdigit) {
                                self.at += 1;
                                n += 1;
                            }
                        }
                        b'*' => return Some(Token::Star),
                        b'\r' => return Some(Token::Symbol(b'\n')),
                        n => return Some(Token::Symbol(n)),
                    }
                }
                _ => {
                    let start = self.at;
                    while b.get(self.at).is_some_and(|c| !matches!(c, b'{' | b'}' | b'\\' | b'\r' | b'\n' | 0)) {
                        self.at += 1;
                    }
                    return Some(Token::Text(b.get(start..self.at).unwrap_or_default()));
                }
            }
        }
    }
}

#[test]
fn raw_word_preserves_spelling_offsets_and_one_delimiter() {
    let bytes = br"x\li-9223372036854775808  text";
    let word = control_word(bytes, 1).unwrap();
    assert_eq!(word, ControlWord { name: "li", argument: Some("-9223372036854775808"), end: 25 });
    assert_eq!(word.name.as_ptr(), bytes[2..].as_ptr());
    assert_eq!(word.argument.unwrap().as_ptr(), bytes[4..].as_ptr());
    assert_eq!(&bytes[word.end..], b" text");
    assert_eq!(control_word(br"\bin3 abc", 0), Some(ControlWord { name: "bin", argument: Some("3"), end: 6 }));
    assert_eq!(control_word(br"\a- x", 0), Some(ControlWord { name: "a", argument: Some("-"), end: 4 }));
    assert_eq!(control_word(br"\a+1", 0), Some(ControlWord { name: "a", argument: None, end: 2 }));
}

#[test]
fn raw_word_rejects_invalid_starts_and_handles_every_truncation() {
    for bytes in [b"".as_slice(), b"\\", b"\\1", b"\\'ff", b"word", b"\\\xff"] {
        assert_eq!(control_word(bytes, 0), None);
    }
    let bytes = br"\word-123 ";
    for end in 0..=bytes.len() {
        let prefix = &bytes[..end];
        let word = control_word(prefix, 0);
        assert_eq!(word.is_some(), end >= 2);
        if let Some(word) = word {
            assert_eq!(word.end, end);
        }
        assert_eq!(control_word(prefix, prefix.len()), None);
        assert_eq!(control_word(prefix, usize::MAX), None);
    }
}

#[test]
fn raw_word_leaves_arbitrary_bytes_after_its_ascii_span() {
    for byte in 0..=u8::MAX {
        let mut bytes = b"\xff\\b-1".to_vec();
        bytes.push(byte);
        let word = control_word(&bytes, 1).unwrap();
        let consumed = usize::from(byte.is_ascii_digit() || byte == b' ');
        assert_eq!(word.end, 5 + consumed, "{byte}");
        assert_eq!(word.name, "b");
        if !byte.is_ascii_digit() {
            assert_eq!(word.argument, Some("-1"));
        }
    }
    assert_eq!(control_word("\\nameé".as_bytes(), 0), Some(ControlWord { name: "name", argument: None, end: 5 }));
}

#[test]
fn raw_word_borrows_long_runs_without_numeric_conversion() {
    let name = "a".repeat(4096);
    let argument = format!("-{}", "9".repeat(4096));
    let input = format!("\\{name}{argument} x");
    assert_eq!(control_word(input.as_bytes(), 0), Some(ControlWord { name: &name, argument: Some(&argument), end: input.len() - 1 }));
}

fn assert_legacy_tokens(bytes: &[u8]) {
    let mut old = LegacyTokens::new(bytes);
    let mut new = Tokens::new(bytes);
    loop {
        let expected = old.next();
        let actual = new.next();
        assert_eq!((actual, new.offset()), (expected, old.offset()), "{bytes:?}");
        if expected.is_none() {
            break;
        }
    }
}

#[test]
fn tokens_keep_legacy_limits_dates_binary_and_malformed_recovery() {
    for name in ["a", "li", "bin", "revdttm", "crdate", &"a".repeat(32), &"b".repeat(33)] {
        for argument in ["", "-", "+1", "-0", "2147483648", "4294967295", "-9999999999", "999999999999999999999999"] {
            let input = format!("{{\\{name}{argument} x\\'4}}y\\é\\bin3 {{x}}\\par");
            for end in 0..=input.len() {
                assert_legacy_tokens(&input.as_bytes()[..end]);
            }
        }
    }
    // Deterministic arbitrary-byte corpus also covers non-UTF-8 symbols and incomplete escapes.
    let mut seed = 0x778_u32;
    for len in 0..=32 {
        for _ in 0..128 {
            let mut bytes = Vec::with_capacity(len);
            for _ in 0..len {
                seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                bytes.push((seed >> 24) as u8);
            }
            assert_legacy_tokens(&bytes);
        }
    }
}
