"""Safe BetInAsia / Black login dry run.

Connects to the first AdsPower profile, logs into BetInAsia, opens the Black
product, logs into Black, and leaves the browser open.

Usage:
    python dry_run_betinasia.py
"""

from login import run_all_profiles


def main() -> None:
    run_all_profiles(expected_profiles=1, wait_for_enter=True)


if __name__ == "__main__":
    main()