"""Redaction runs in the producer, before anything is written.

A path is not automatically private, so the policy has to decide what survives
rather than assuming a path is safe because it is not a file's contents.
"""

from __future__ import annotations

import unittest

from machine_plane.privacy import PathDisplay, PrivacyPolicy


class ExcludedPathTest(unittest.TestCase):
    def test_credential_paths_are_excluded_regardless_of_roots(self) -> None:
        policy = PrivacyPolicy(
            allowed_roots=("/home/x",), path_display=PathDisplay.FULL
        )
        result = policy.redact_path("/home/x/.ssh/id_ed25519")
        self.assertIsNone(result.value)
        self.assertTrue(result.redacted)
        self.assertEqual(result.reason, "excluded_path")

    def test_browser_and_keychain_paths_are_excluded(self) -> None:
        policy = PrivacyPolicy(allowed_roots=("/home/x",))
        for path in (
            "/home/x/.config/google-chrome/Default/Cookies",
            "/home/x/Library/Keychains/login.keychain-db",
        ):
            with self.subTest(path=path):
                self.assertIsNone(policy.redact_path(path).value)


class PathDisplayTest(unittest.TestCase):
    def test_basename_outside_allowed_roots_is_dropped(self) -> None:
        policy = PrivacyPolicy(
            allowed_roots=("/home/x/work",), path_display=PathDisplay.BASENAME
        )
        result = policy.redact_path("/home/x/Documents/medical/diagnosis.pdf")
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "outside_allowed_roots")

    def test_basename_inside_allowed_roots_survives_as_a_name_only(self) -> None:
        policy = PrivacyPolicy(
            allowed_roots=("/home/x/work",), path_display=PathDisplay.BASENAME
        )
        result = policy.redact_path("/home/x/work/report.txt")
        self.assertEqual(result.value, "report.txt")
        self.assertTrue(result.redacted)

    def test_full_path_survives_only_inside_allowed_roots(self) -> None:
        policy = PrivacyPolicy(
            allowed_roots=("/home/x/work",), path_display=PathDisplay.FULL
        )
        self.assertEqual(
            policy.redact_path("/home/x/work/report.txt").value,
            "/home/x/work/report.txt",
        )
        self.assertIsNone(policy.redact_path("/home/x/private/notes.txt").value)

    def test_hashed_paths_are_deterministic_and_not_reversible_by_eye(self) -> None:
        policy = PrivacyPolicy(path_display=PathDisplay.HASHED)
        first = policy.redact_path("/home/x/notes.txt").value
        second = policy.redact_path("/home/x/notes.txt").value
        other = policy.redact_path("/home/x/other.txt").value
        self.assertIsNotNone(first)
        self.assertIsNotNone(other)
        assert first is not None and other is not None
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 32)

    def test_hashed_paths_differ_with_a_different_salt(self) -> None:
        one = PrivacyPolicy(path_display=PathDisplay.HASHED, hash_salt=b"a")
        two = PrivacyPolicy(path_display=PathDisplay.HASHED, hash_salt=b"b")
        self.assertNotEqual(
            one.redact_path("/home/x/notes.txt").value,
            two.redact_path("/home/x/notes.txt").value,
        )

    def test_omitted_drops_everything(self) -> None:
        policy = PrivacyPolicy(path_display=PathDisplay.OMITTED)
        self.assertIsNone(policy.redact_path("/home/x/notes.txt").value)


class DetailSanitisationTest(unittest.TestCase):
    def test_command_lines_are_stripped(self) -> None:
        policy = PrivacyPolicy()
        cleaned, removed = policy.sanitize_details(
            {"pid": 4821, "command_line": "echo hello", "argv": ["echo", "hello"]}
        )
        self.assertEqual(cleaned, {"pid": 4821})
        self.assertIn("command_line", removed)
        self.assertIn("argv", removed)

    def test_nested_content_is_stripped_too(self) -> None:
        policy = PrivacyPolicy()
        cleaned, removed = policy.sanitize_details(
            {"proc": {"name": "python3", "args": ["-c", "secret"]}}
        )
        self.assertEqual(cleaned, {"proc": {"name": "python3"}})
        self.assertIn("proc.args", removed)


if __name__ == "__main__":
    unittest.main()
