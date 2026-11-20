//! The tokens of an RTF file. Nothing is interpreted here: a token is what the bytes say.

/// Is this an RTF file? Leading white space, then `{\rtf`.
pub fn is_rtf(bytes: &[u8]) -> bool {
    bytes.trim_ascii_start().starts_with(b"{\\rtf")
}

/// Borrowed spelling of a control word, without interpreting its argument.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ControlWord<'a> {
    /// ASCII letters, without the leading backslash.
    pub name: &'a str,
    /// Decimal digits, optionally preceded by a minus. A lone minus is preserved too.
    pub argument: Option<&'a str>,
    /// Absolute byte offset after the word, argument and optional single-space delimiter.
    pub end: usize,
}

/// Scans a control word starting at the backslash at `at`.
///
/// Returns `None` unless that position starts a backslash followed by an ASCII letter.
/// Names and arguments are borrowed in full, without truncation, numeric conversion or
/// allocation. A lone minus is an argument spelling, not a valid number. Only one ASCII
/// space is consumed as a delimiter; other following bytes are left untouched. `bin` is
/// just a word here: its payload is not consumed.
///
/// This is for readers that need raw spelling or their own numeric range. Work is linear
/// in the scanned span, bounded by `bytes.len()`; callers should bound their input size.
/// [`Tokens`] retains its bounded names, arguments and binary-payload handling.
pub fn control_word(bytes: &[u8], at: usize) -> Option<ControlWord<'_>> {
    scan_word(bytes, at, usize::MAX, usize::MAX, true)
}

fn scan_word(bytes: &[u8], at: usize, name_limit: usize, digit_limit: usize, lone_minus: bool) -> Option<ControlWord<'_>> {
    if bytes.get(at) != Some(&b'\\') {
        return None;
    }
    let start = at.checked_add(1)?;
    if !bytes.get(start).is_some_and(u8::is_ascii_alphabetic) {
        return None;
    }
    let mut j = start;
    while j - start < name_limit && bytes.get(j).is_some_and(u8::is_ascii_alphabetic) {
        j += 1;
    }
    let name = std::str::from_utf8(bytes.get(start..j)?).ok()?;
    let neg = bytes.get(j) == Some(&b'-');
    let digits = j + usize::from(neg);
    let mut end = digits;
    while end - digits < digit_limit && bytes.get(end).is_some_and(u8::is_ascii_digit) {
        end += 1;
    }
    let argument = if end > digits || neg && lone_minus {
        Some(std::str::from_utf8(bytes.get(j..end)?).ok()?)
    } else {
        end = j;
        None
    };
    if bytes.get(end) == Some(&b' ') {
        end += 1;
    }
    Some(ControlWord { name, argument, end })
}

/// One token.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Token<'a> {
    /// `{`
    Open,
    /// `}`
    Close,
    /// A control word: `\par`, `\fs24`, `\li-720`. The parameter is clamped into `i32`.
    Word { name: &'a str, param: Option<i32> },
    /// `\*`: the destination that follows may be ignored by a reader that does not know it.
    Star,
    /// `\'hh`: one byte of text in the current code page.
    Hex(u8),
    /// Any other control symbol, by the byte after the backslash: `\~`, `\-`, `\_`, `\\`,
    /// `\{`, `\}`, and `\` before a line end (which is `\par`).
    Symbol(u8),
    /// The data of `\binN`: the `N` bytes after the word's delimiter, as they are.
    Bin(&'a [u8]),
    /// A run of text bytes in the current code page: no `{`, `}`, `\`, line end or NUL.
    Text(&'a [u8]),
}

/// The tokens of `bytes`, in order. Never fails: a malformed escape is dropped and whatever
/// follows it is read as the structure it is.
///
/// - A control word is at most 32 letters and its parameter at most 10 digits; what follows is
///   text. A `-` starts a parameter only when a digit follows it.
/// - One space after a control word is its delimiter and not text.
/// - Line ends and NUL bytes between tokens are not tokens.
/// - `\'` without two hex digits gives no token: it and the hex digits it does have are
///   skipped, and a `}` or `\` after it is read as itself.
/// - A date word (`\revdttm`, `\revdttmdel`, `\crdate`, `\prdate`) holds a 32-bit pattern that
///   some writers put unsigned: it is read as that pattern, not clamped.
#[derive(Clone, Debug)]
pub struct Tokens<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Tokens<'a> {
    pub fn new(bytes: &'a [u8]) -> Tokens<'a> {
        Tokens { bytes, at: 0 }
    }

    /// The offset of the next token.
    pub fn offset(&self) -> usize {
        self.at
    }

    fn word(&mut self) -> Token<'a> {
        let b = self.bytes;
        // Tokenization keeps its existing resource limits and numeric interpretation.
        let Some(ControlWord { name, argument, end: k }) = scan_word(b, self.at, 32, 10, false) else {
            return Token::Word { name: "", param: None };
        };
        let param = argument.map(|argument| {
            // At most 10 digits plus a sign, so it fits an i64.
            let v = argument.parse::<i64>().unwrap_or(0);
            let dttm = matches!(name, "revdttm" | "revdttmdel" | "crdate" | "prdate");
            let v = if dttm && v > i64::from(i32::MAX) && v <= i64::from(u32::MAX) { v - (1i64 << 32) } else { v };
            v.clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32
        });
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

impl<'a> Iterator for Tokens<'a> {
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

#[cfg(test)]
mod tests {
    use super::*;
    use Token::{Bin, Close, Hex, Open, Star, Symbol, Text};

    fn w(name: &str, param: Option<i32>) -> Token<'_> {
        Token::Word { name, param }
    }

    fn lex(s: &[u8]) -> Vec<Token<'_>> {
        Tokens::new(s).collect()
    }

    #[test]
    fn groups_words_symbols_and_text() {
        assert_eq!(
            lex(br"{\rtf1\ansi {\*\generator X;}\b Hi\b0  there\par\~\'e9}"),
            vec![
                Open,
                w("rtf", Some(1)),
                w("ansi", None),
                Open,
                Star,
                w("generator", None),
                Text(b"X;"),
                Close,
                w("b", None),
                Text(b"Hi"),
                w("b", Some(0)),
                Text(b" there"),
                w("par", None),
                Symbol(b'~'),
                Hex(0xE9),
                Close
            ]
        );
        assert!(is_rtf(b" \r\n{\\rtf1}") && !is_rtf(b"{rtf1}") && !is_rtf(b""));
    }

    #[test]
    fn a_hyphen_is_a_sign_only_before_a_digit() {
        assert_eq!(
            lex(br"\li-720 a\emdash-roll\tab- x"),
            vec![w("li", Some(-720)), Text(b"a"), w("emdash", None), Text(b"-roll"), w("tab", None), Text(b"- x")]
        );
    }

    #[test]
    fn long_words_and_parameters_are_cut_not_scanned_without_end() {
        let long = format!("\\{}1", "a".repeat(40));
        assert_eq!(lex(long.as_bytes()), vec![w(&"a".repeat(32), None), Text(b"aaaaaaaa1")]);
        assert_eq!(lex(br"\fs123456789012 x"), vec![w("fs", Some(1_234_567_890)), Text(b"12 x")]);
        assert_eq!(lex(br"\fs9999999999\li-9999999999"), vec![w("fs", Some(i32::MAX)), w("li", Some(i32::MIN))]);
    }

    #[test]
    fn dates_keep_their_bit_pattern() {
        assert_eq!(
            lex(br"\revdttm4294967295\crdate2147483648\fs4294967295"),
            vec![w("revdttm", Some(-1)), w("crdate", Some(i32::MIN)), w("fs", Some(i32::MAX))]
        );
    }

    #[test]
    fn malformed_hex_escapes_leave_the_structure_alone() {
        assert_eq!(lex(br"{\b A\'4}B"), vec![Open, w("b", None), Text(b"A"), Close, Text(b"B")]);
        assert_eq!(lex(br"a\'+fb\'\par"), vec![Text(b"a"), Text(b"+fb"), w("par", None)]);
        assert_eq!(lex(br"\'zz\'4"), vec![Text(b"zz")]);
        assert_eq!(lex(b"a\\"), vec![Text(b"a")], "a backslash at the end is dropped");
    }

    #[test]
    fn binary_data_is_not_scanned() {
        assert_eq!(lex(b"{\\pict\\bin5 {\\}\0\\x}"), vec![Open, w("pict", None), Bin(b"{\\}\0\\"), Text(b"x"), Close]);
        assert_eq!(lex(b"\\bin99 ab"), vec![Bin(b"ab")], "cut at the end of the file");
        assert_eq!(lex(b"\\bin-4 ab\\bin"), vec![Bin(b""), Text(b"ab"), Bin(b"")]);
    }

    #[test]
    fn line_ends_and_nul_are_not_text() {
        assert_eq!(lex(b"a\r\nb\0c\\\nd\\\r\ne"), vec![Text(b"a"), Text(b"b"), Text(b"c"), Symbol(b'\n'), Text(b"d"), Symbol(b'\n'), Text(b"e")]);
        assert_eq!(lex(b"\\\xe9x"), vec![Symbol(0xE9), Text(b"x")]);
        let mut t = Tokens::new(b"ab{");
        assert_eq!((t.next(), t.offset()), (Some(Text(b"ab")), 2));
    }
}

#[cfg(test)]
#[path = "lex_span_tests.rs"]
mod span_tests;
