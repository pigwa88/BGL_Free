"""Testy polityki bezpieczeństwa."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdbridge.policy import Policy, parse_rule_file  # noqa: E402


class PolicyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy()

    def test_allows_normal_commands(self) -> None:
        for command in [
            "dir",
            "echo hello",
            "python -m pytest",
            "git status",
            'cd "C:\\Projekt" && npm install',
            "del stary_plik.txt",
            "rm -rf ./build",
        ]:
            self.assertTrue(self.policy.check(command).allowed, msg=command)

    def test_blocks_destructive_commands(self) -> None:
        for command in [
            "format C:",
            "FORMAT c: /q",
            "diskpart",
            "del /f /s /q C:\\",
            "rd /s /q C:\\",
            "rm -rf /",
            "rm -rf --no-preserve-root /",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            "shutdown /s /t 0",
            "vssadmin delete shadows /all",
            "reg delete HKLM\\Software /f",
        ]:
            decision = self.policy.check(command)
            self.assertFalse(decision.allowed, msg=command)
            self.assertTrue(decision.rule, msg=command)

    def test_empty_command_rejected(self) -> None:
        self.assertFalse(self.policy.check("   ").allowed)

    def test_too_long_command_rejected(self) -> None:
        policy = Policy(max_command_length=10)
        self.assertEqual(policy.check("x" * 11).rule, "too-long")

    def test_allow_dangerous_disables_rules(self) -> None:
        self.assertTrue(Policy(allow_dangerous=True).check("format C:").allowed)

    def test_single_rule_can_be_disabled(self) -> None:
        policy = Policy(disabled_rules=["shutdown"])
        self.assertTrue(policy.check("shutdown /r").allowed)
        self.assertFalse(policy.check("format C:").allowed)

    def test_extra_rules_are_applied(self) -> None:
        policy = Policy(extra_rules=[("no-curl", r"\bcurl\b")])
        self.assertFalse(policy.check("curl http://example.com").allowed)

    def test_rule_file_parsing(self) -> None:
        rules = parse_rule_file(
            "# komentarz\n"
            "brak-npm = \\bnpm\\s+publish\\b\n"
            "\n"
            "\\bpip\\s+install\\b\n"
        )
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0][0], "brak-npm")
        self.assertTrue(rules[1][0].startswith("custom-"))


if __name__ == "__main__":
    unittest.main()
