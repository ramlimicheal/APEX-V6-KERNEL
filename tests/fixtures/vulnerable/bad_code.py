"""Deliberately insecure code used ONLY as a test fixture for Layer 3.

Bandit should flag the subprocess shell=True injection (B602) as HIGH severity.
Do not use any of this for real.
"""

import subprocess


def run_user_command(user_input):
    # B602: subprocess call with shell=True and untrusted input -> HIGH severity.
    return subprocess.call("ls " + user_input, shell=True)
