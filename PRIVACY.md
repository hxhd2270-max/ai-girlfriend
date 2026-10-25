# Privacy

Zhixia processes user text and, when enabled, microphone audio through the
providers selected by the user. Provider privacy terms and retention rules
therefore apply to those requests.

Local conversations, usage counters, N.E.K.O. memory, process state, logs, and
browser profiles are user data. They must be stored outside a read-only program
installation and must never be included in source archives, commits, releases,
diagnostic bundles, or bug reports by default.

Provider credentials stay in the backend. On Windows, the supported managed
path encrypts credentials for the current user with DPAPI. Encrypted DPAPI files
are still private, machine-bound data and must not be committed or shared.

The public source package contains no developer provider account, conversation,
memory database, recording, or usage ledger. Simulation mode must remain
available for development without a provider credential or paid request.
