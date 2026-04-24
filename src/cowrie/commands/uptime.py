# Copyright (c) 2009 Upi Tamminen <desaster@gmail.com>
# See the COPYRIGHT file for more information

from __future__ import annotations

import random
import time

from cowrie.core import utils
from cowrie.shell.command import HoneyPotCommand

commands = {}

# Fake Pi boot time: pretend the box has been up for ~4 hours when cowrie
# first loads. Defaulting to `self.protocol.uptime()` (cowrie's own process
# uptime) means a fresh cowrie shows "up 0 min" — a fingerprint that
# screams honeypot. Red-team engagements typically happen hours into a
# Pi's actual uptime, so a multi-hour fake is plausible.
_FAKE_BOOT = time.time() - (4 * 3600 + random.randint(0, 1800))


class Command_uptime(HoneyPotCommand):
    def call(self) -> None:
        # Debian's uptime output has a leading space:
        # " 12:21:36 up 20:30,  5 users,  load average: 0.13, 0.03, 0.01"
        # Per-call jitter on load avg so consecutive calls aren't byte-
        # identical (also a trivially-fingerprinted honeypot tell).
        l1 = max(0.0, 0.08 + random.uniform(-0.03, 0.05))
        l5 = max(0.0, 0.04 + random.uniform(-0.02, 0.03))
        l15 = max(0.0, 0.02 + random.uniform(-0.01, 0.02))
        self.write(
            " {}  up {},  1 user,  "
            "load average: {:.2f}, {:.2f}, {:.2f}\n".format(
                time.strftime("%H:%M:%S"),
                utils.uptime(time.time() - _FAKE_BOOT),
                l1, l5, l15,
            )
        )


commands["/usr/bin/uptime"] = Command_uptime
commands["uptime"] = Command_uptime
