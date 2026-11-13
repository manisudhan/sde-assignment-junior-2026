"""Linking events to leads.

Run after every ingest. Two passes, in this order, because the second depends on
the first:

1. **By phone.** An event links to the lead with the same
   ``(workflow_id, phone_bidx)``. The blind index makes this an indexed join on
   a column that contains no plaintext phone number.
2. **By call.** ``disposition.override`` carries no phone, only a ``call_id``,
   so it inherits the lead that the rest of its call linked to in pass 1.

Both passes only ever *insert* links, and only for events that do not have one,
which is what makes this incremental and idempotent at the same time: running it
again is a no-op, and running it after a new drop picks up exactly the events
whose lead has since arrived. The ledger is never rewritten - an event that
sat as an orphan after drop 1 is the same row after drop 2, it has simply
acquired a link.
"""
from __future__ import annotations

import sqlite3

LINK_BY_PHONE = """
INSERT OR IGNORE INTO event_link(event_id, lead_id, link_via)
SELECT e.event_id, l.lead_id, 'phone'
FROM interaction_event e
JOIN leads l
  ON l.workflow_id = e.workflow_id
 AND l.phone_bidx  = e.phone_bidx
LEFT JOIN event_link k ON k.event_id = e.event_id
WHERE e.phone_bidx IS NOT NULL
  AND k.event_id IS NULL
"""

# The lead of a call, as established by the events of that call that carried a
# usable phone. HAVING COUNT(DISTINCT ...) = 1 refuses to guess when a call_id
# was somehow reused across two leads: better an orphan than a wrong attribution
# in a client's renewal numbers.
LINK_BY_CALL = """
INSERT OR IGNORE INTO event_link(event_id, lead_id, link_via)
SELECT e.event_id, c.lead_id, 'call'
FROM interaction_event e
JOIN (
    SELECT ie.call_id, MIN(k.lead_id) AS lead_id
    FROM interaction_event ie
    JOIN event_link k ON k.event_id = ie.event_id
    WHERE ie.call_id IS NOT NULL
    GROUP BY ie.call_id
    HAVING COUNT(DISTINCT k.lead_id) = 1
) c ON c.call_id = e.call_id
LEFT JOIN event_link k2 ON k2.event_id = e.event_id
WHERE e.call_id IS NOT NULL
  AND e.phone_bidx IS NULL
  AND k2.event_id IS NULL
"""


def relink(conn: sqlite3.Connection) -> dict[str, int]:
    """Attach every linkable event to its lead. Returns links created per pass."""
    before = conn.total_changes
    conn.execute(LINK_BY_PHONE)
    by_phone = conn.total_changes - before

    before = conn.total_changes
    conn.execute(LINK_BY_CALL)
    by_call = conn.total_changes - before

    return {"linked_by_phone": by_phone, "linked_by_call": by_call}
