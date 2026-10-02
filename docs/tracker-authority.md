# Tracker ownership and ticket identity

A display code such as `GL-264` is unique only inside its tracker. A matching code in
Linear and a Tasks deployment does not identify the same issue and is not an import alias.

Repository `task.yaml` is authoritative over inherited workspace defaults. A repository
whose active issues remain in Linear must select that backend and its team/project there;
changing a global workspace default must not be used to migrate its issue identities.

The Tasks adapter accepts its configured team's key, or a canonical UUID in that same
Tasks deployment. A different team's key, arbitrary string or provider URL is rejected
before any request. A read response must identify the requested key/UUID before it may
be used for a comment or transition. An explicit Tasks UUID is not a Linear UUID alias.

Outside a pinned repository, a short key matching more than one tracker is ambiguous.
Use the owning repository's configuration instead of picking the first registry entry.
Do not merge unrelated issues solely because they share a display code. Cross-tracker
migration requires a separately verified source-to-destination mapping.

A UUID permits an explicit read or comment in the configured Tasks deployment. It does
not authorize applying the configured team's workflow to another team: status changes
verify the returned issue key before writing, and updates remain team-scoped.

For update calls, retain the team key on the Ticket returned by `get`, including when the
original read used a UUID. Replacing that id with a UUID or foreign key is refused before
label creation or any update request; no extra read is added and optimistic versions remain intact.
