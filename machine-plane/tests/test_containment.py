"""Containment detection, consent, and the startup matrix.

The point of these tests is not that a boolean flips. It is that **consent is a
claim about a specific present**, so it expires when the situation it describes
changes — including when a machine quietly becomes less protected than it was.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from machine_plane.containment import (
    Consent,
    ConsentKind,
    Detection,
    Level,
    Startup,
    consent_message,
    decide_startup,
    detect,
    read_consent,
    write_consent,
)

CONFIG = "f" * 64


def advisory_detection(
    *,
    account: str = "agent-account",
    uid: int = 1000,
    grants: tuple[str, ...] = ("/etc/sudoers.d/<grant-a>",),
    groups: tuple[str, ...] = ("agent-account",),
    is_root: bool = False,
    can_control_units: bool = False,
    reasons: tuple[str, ...] = ("passwordless sudo grant: /etc/sudoers.d/<grant-a>",),
) -> Detection:
    return Detection(
        account=account,
        uid=uid,
        host_id="host",
        grants=grants,
        groups=groups,
        is_root=is_root,
        can_control_units=can_control_units,
        level=Level.ADVISORY,
        reasons=reasons,
    )


def enforced_detection(
    *,
    account: str = "test-unprivileged",
    uid: int = 1001,
    grants: tuple[str, ...] = (),
    groups: tuple[str, ...] = ("test-unprivileged",),
    is_root: bool = False,
    can_control_units: bool = False,
    reasons: tuple[str, ...] = (),
) -> Detection:
    return Detection(
        account=account,
        uid=uid,
        host_id="host",
        grants=grants,
        groups=groups,
        is_root=is_root,
        can_control_units=can_control_units,
        level=Level.ENFORCED,
        reasons=reasons,
    )


def consent_for(detection: Detection, config: str = CONFIG) -> Consent:
    return Consent(
        level=detection.level,
        fingerprint=detection.fingerprint(),
        config_fingerprint=config,
        detected_at=1.0,
        consent_kind=ConsentKind.FLAG,
        consented_at=2.0,
    )


class TestDetection(unittest.TestCase):
    def test_passwordless_grant_makes_it_advisory(self) -> None:
        detection = detect(
            account="agent-account",
            uid=1000,
            host_id="host",
            sudo_lister=lambda _: "    (ALL : ALL) NOPASSWD: ALL\n",
            sudoers_lister=lambda _: ("/etc/sudoers.d/<grant-a>",),
            group_lister=lambda _: ("agent-account",),
        )
        self.assertIs(detection.level, Level.ADVISORY)
        self.assertIn("/etc/sudoers.d/<grant-a>", detection.grants)

    def test_passwordless_we_cannot_name_is_still_advisory(self) -> None:
        """An unnamed grant must not read as 'no escalation'."""
        detection = detect(
            account="agent-account",
            uid=1000,
            host_id="host",
            sudo_lister=lambda _: "    (ALL : ALL) NOPASSWD: ALL\n",
            sudoers_lister=lambda _: (),
            group_lister=lambda _: ("agent-account",),
        )
        self.assertIs(detection.level, Level.ADVISORY)
        self.assertIn("merged-policy:passwordless", detection.grants)

    def test_no_grants_and_no_privileged_groups_is_enforced(self) -> None:
        detection = detect(
            account="test-unprivileged",
            uid=1001,
            host_id="host",
            sudo_lister=lambda _: "User test-unprivileged is not allowed to run sudo",
            group_lister=lambda _: ("test-unprivileged",),
            unit_controller=lambda _: False,
        )
        self.assertIs(detection.level, Level.ENFORCED)
        self.assertEqual(detection.reasons, ())

    def test_a_privileged_group_is_advisory_without_any_sudo_rule(self) -> None:
        detection = detect(
            account="agent-account",
            uid=1000,
            host_id="host",
            sudo_lister=lambda _: "not allowed to run sudo",
            group_lister=lambda _: ("agent-account", "docker"),
        )
        self.assertIs(detection.level, Level.ADVISORY)

    def test_the_detector_discriminates_rather_than_always_warning(self) -> None:
        """Checks 11 and 16: same code path, two accounts, two answers."""
        unprivileged = detect(
            account="a",
            uid=1001,
            host_id="host",
            sudo_lister=lambda _: "not allowed to run sudo",
            sudoers_lister=lambda _: (),
            group_lister=lambda a: (a,),
        )
        privileged = detect(
            account="b",
            uid=0,
            host_id="host",
            sudo_lister=lambda _: "not allowed to run sudo",
            sudoers_lister=lambda _: (),
            group_lister=lambda a: (a,),
        )
        self.assertIs(unprivileged.level, Level.ENFORCED)
        self.assertIs(privileged.level, Level.ADVISORY)


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_is_stable_and_order_independent(self) -> None:
        first = advisory_detection(grants=("a", "b"), groups=("x", "y"))
        second = advisory_detection(grants=("b", "a"), groups=("y", "x"))
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_a_new_grant_changes_the_fingerprint(self) -> None:
        before = advisory_detection()
        after = advisory_detection(
            grants=("/etc/sudoers.d/<grant-a>", "/etc/sudoers.d/<grant-b>"),
            reasons=(
                "passwordless sudo grant: /etc/sudoers.d/<grant-a>",
                "passwordless sudo grant: /etc/sudoers.d/<grant-b>",
            ),
        )
        self.assertNotEqual(before.fingerprint(), after.fingerprint())


class TestStartupMatrix(unittest.TestCase):
    def test_unknown_detection_refuses_first(self) -> None:
        decision = decide_startup(
            detection=None,
            detection_error="cannot read sudo rules",
            consent=None,
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.REFUSE_DETECTION)
        self.assertEqual(decision.exit_code, 2)

    def test_advisory_without_consent_refuses(self) -> None:
        decision = decide_startup(
            detection=advisory_detection(),
            detection_error=None,
            consent=None,
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.REFUSE_NO_CONSENT)
        self.assertEqual(decision.exit_code, 3)
        self.assertIn("advisory", consent_message(decision, decision.detection).lower())

    def test_advisory_with_matching_consent_starts(self) -> None:
        detection = advisory_detection()
        decision = decide_startup(
            detection=detection,
            detection_error=None,
            consent=consent_for(detection),
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.OK_ADVISORY)
        self.assertEqual(decision.exit_code, 0)

    def test_a_changed_fingerprint_refuses_and_names_the_field(self) -> None:
        old = advisory_detection()
        new = advisory_detection(
            grants=("/etc/sudoers.d/<grant-a>", "/etc/sudoers.d/<grant-b>")
        )
        decision = decide_startup(
            detection=new,
            detection_error=None,
            consent=consent_for(old),
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.REFUSE_FINGERPRINT)
        self.assertEqual(decision.exit_code, 4)
        self.assertIn("grants", decision.changed_fields)

    def test_enforced_to_advisory_requires_fresh_consent(self) -> None:
        """A machine becoming less protected is never automatic."""
        enforced = enforced_detection()
        consent = consent_for(enforced)  # agreed while enforced
        later = advisory_detection()  # a grant appeared afterwards

        decision = decide_startup(
            detection=later,
            detection_error=None,
            consent=consent,
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.REFUSE_FINGERPRINT)
        self.assertFalse(decision.allowed)

    def test_advisory_to_enforced_proceeds_and_supersedes(self) -> None:
        advisory = advisory_detection()
        decision = decide_startup(
            detection=enforced_detection(),
            detection_error=None,
            consent=consent_for(advisory),
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.OK_ENFORCED_SUPERSEDES)
        self.assertTrue(decision.allowed)

    def test_enforced_needs_no_advisory_consent(self) -> None:
        decision = decide_startup(
            detection=enforced_detection(),
            detection_error=None,
            consent=None,
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.OK_ENFORCED)

    def test_a_changed_recording_configuration_refuses(self) -> None:
        detection = advisory_detection()
        decision = decide_startup(
            detection=detection,
            detection_error=None,
            consent=consent_for(detection, config="0" * 64),
            current_config=CONFIG,
        )
        self.assertIs(decision.outcome, Startup.REFUSE_CONFIG)
        self.assertEqual(decision.exit_code, 5)


class TestConsentFile(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-consent-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def test_round_trip(self) -> None:
        path = self.directory / "containment.json"
        consent = consent_for(advisory_detection())
        write_consent(path, consent)
        self.assertEqual(read_consent(path), consent)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertIsNone(read_consent(self.directory / "absent.json"))


if __name__ == "__main__":
    unittest.main()
