# Vendored Thorlabs MCM301 SDK

`MCM301Lib_x64.dll` is Thorlabs' own MCM301 command library, copied verbatim
from a Thorlabs MCM301 software installation:

    C:\Program Files (x86)\Thorlabs\MCM301\Sample\Thorlabs_MCM301_C++ SDK\MCM301Lib_x64.dll

It is **unmodified**, and remains the property of Thorlabs. It is redistributed
here under the terms of `Thorlabs End-user License.rtf` in this folder, which
permits copies provided they carry the agreement and its proprietary notices.
Anything else in this repository is unaffected by that license.

## Why it's vendored

[`../mcm301_driver.py`](../mcm301_driver.py) drives the MCM301 through this DLL —
the controller does not speak a documented wire protocol of its own. Keeping a
copy here means an acquisition machine needs no Thorlabs software installed.

The driver looks for it here first, then falls back to the two standard install
paths, so deleting this folder only means the Thorlabs software must be
installed. **x64 only** — it must match the Python interpreter's architecture.

## Updating it

Re-copy from a newer Thorlabs install and re-run the driver's own self test
against the hardware (read-only, no motion):

    .venv\Scripts\python.exe devices\stage\mcm301_driver.py COM10

Check the signatures in `mcm301_driver.py`'s `_declare()` still match the SDK's
`MCM301CommandLibrary.h` — a changed struct or argument list will not fail
loudly, it will return quiet nonsense.
