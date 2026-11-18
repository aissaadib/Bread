# Python analysis worker

The analysis worker is intentionally a separate layer from the React interface and Rust desktop runtime. The first desktop milestone currently uses Rust and FFprobe to inspect local media. The worker protocol and analysis stages will be added next; no model or Python package is required to open the current desktop shell.

Python 3.11+ is the intended worker baseline. The system Python executable is not currently available in this workspace, so the worker is not yet connected to the desktop application.
