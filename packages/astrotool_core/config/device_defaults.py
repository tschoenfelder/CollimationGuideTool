"""Built-in device identities and the default INDI endpoint -- the ONE owner
of these values (#55 D01/D02; CONTRIBUTING.md "Duplicated knowledge").

Used only when no configuration names something else. Config readers
(`filter_wheel.config`, `onstep.settings`) decide *which source wins*; this
module only holds the built-in values they fall back to, and every adapter,
client and simulator default refers here instead of re-stating them.
`tests/contracts/test_config_source_contract.py` fails if one of these literals
reappears anywhere else in production code.
"""

from __future__ import annotations

from typing import Final

#: The rig's filter wheel as its INDI driver names it. Corrected 2026-09-24
#: (#47): the original "EFW 1" guess was never checked against the real
#: indiserver (`indi_getprop`); the rig's driver reports itself as "EFW 2".
EFW_DEVICE_NAME: Final = "ToupTek EFW 2"

#: The local indiserver every INDI consumer talks to (the filter wheel and
#: OnStepAdapter's INDI runtime). Numeric loopback, not "localhost":
#: indiserver listens on IPv4 only, and "localhost" first tries ::1 -- on
#: Windows a refused ::1 costs about 2 s per connect before the IPv4 retry.
INDI_HOST: Final = "127.0.0.1"
#: indiserver's standard port.
INDI_PORT: Final = 7624
