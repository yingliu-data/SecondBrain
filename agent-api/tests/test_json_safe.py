import unittest

from app.util.json_safe import parse_json_defensive, DefensiveJSONError


class TestParseJsonDefensive(unittest.TestCase):
    def test_clean_object(self):
        self.assertEqual(parse_json_defensive('{"x": 1}'), {"x": 1})

    def test_clean_array(self):
        self.assertEqual(parse_json_defensive("[1, 2, 3]"), [1, 2, 3])

    def test_markdown_fenced_json_lang(self):
        raw = '```json\n{"x": 1}\n```'
        self.assertEqual(parse_json_defensive(raw), {"x": 1})

    def test_markdown_fenced_no_lang(self):
        raw = '```\n{"x": 1}\n```'
        self.assertEqual(parse_json_defensive(raw), {"x": 1})

    def test_json_prefix(self):
        self.assertEqual(parse_json_defensive('json\n{"x": 1}'), {"x": 1})

    def test_trailing_prose(self):
        raw = '{"x": 1} — that should do it.'
        self.assertEqual(parse_json_defensive(raw), {"x": 1})

    def test_leading_prose(self):
        raw = 'Sure! Here you go: {"x": 1}'
        self.assertEqual(parse_json_defensive(raw), {"x": 1})

    def test_empty_raises(self):
        with self.assertRaises(DefensiveJSONError):
            parse_json_defensive("")

    def test_whitespace_only_raises(self):
        with self.assertRaises(DefensiveJSONError):
            parse_json_defensive("   \n\t  ")

    def test_garbage_raises(self):
        with self.assertRaises(DefensiveJSONError):
            parse_json_defensive("this is not json at all")

    def test_expect_mismatch_raises(self):
        with self.assertRaises(DefensiveJSONError):
            parse_json_defensive("[1, 2, 3]", expect=dict)

    def test_expect_match(self):
        self.assertEqual(parse_json_defensive('{"a": 1}', expect=dict), {"a": 1})

    def test_nested_braces_in_string(self):
        raw = '{"msg": "hello {world}"}'
        self.assertEqual(parse_json_defensive(raw), {"msg": "hello {world}"})

    def test_escaped_quote_in_string(self):
        raw = r'prose {"msg": "she said \"hi\""} more prose'
        self.assertEqual(parse_json_defensive(raw), {"msg": 'she said "hi"'})

    def test_non_string_raises(self):
        with self.assertRaises(DefensiveJSONError):
            parse_json_defensive(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
