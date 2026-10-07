---
description: check that a list of .onion services is reachable over Tor
packs: web, system
every: 30m
---
For each address below, call fetch_url with via_tor=true and report whether it loaded and its page title.
Show the results as a table, then call notify with a one-line summary.

{{input}}
