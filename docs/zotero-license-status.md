# Automatic license folder in Zotero

The local **Zotero Audio** add-on maintains the saved search
**Audio — License blocked** in My Library. Records stay in their existing
collections. The saved search matches the exact `audio:license:blocked` tag;
passing records have `audio:license:pass` instead. Missing or unverified rights
count as blocked, not as a claim that the source has a closed license.

The add-on evaluates live Rights, strict Rights/License lines in Extra, and
previously recorded PDF evidence through the same Python `resolve_license`
function used by podcast publication. Evidence is bound to the current PDF's
SHA-256. Old parent-item assertions are not reused after Rights is removed.
Existing embargo and conflict flags remain blocking. A record with multiple
PDFs is blocked if any PDF fails; standalone PDFs also receive status tags.

Each parent record gets a delimited **Zotero Audio license gate** block in Extra
with the status, license URL when known, and reason for each attachment. Rights,
other Extra text, unrelated tags, and collection membership are preserved.
Status tags are informational: they do not authorize or trigger publication.

Refresh runs at Zotero startup, two seconds after item changes, and every five
minutes while Zotero is open. Changed item fields, attachment lists, or file
timestamps during evaluation cause that item to be skipped until the next
refresh. Removing all PDFs removes the managed metadata on the next refresh.
Group libraries are outside this add-on's scope.

Version 0.2.0 also provides the [read-only local feed API](zotero-feed-api.md).
The plugin ID is unchanged from **Zotero Audio License Status** 0.1.0, so
installing the new package upgrades the existing plugin.
Version 0.2.1 waits for bibliographic fields to load before taking license
snapshots, including during startup.

## Installation

The existing Python runtime must have this repository installed. Package the
add-on outside the checkout:

```bash
RUNTIME="$HOME/Sites/zotero-audio-runtime"
"$RUNTIME/venv/bin/python" scripts/package_zotero_addon.py \
  "$RUNTIME/zotero-audio-license-status.xpi"
```

In Zotero, open Tools → Plugins, choose **Install Plugin From File**, and select
the XPI. The tested version is Zotero 9.0.6. Repackage and reinstall after editing
the add-on JavaScript. The Python evaluator uses the current repository code.

Defaults are `$HOME/Sites/zotero-audio` for the repository and
`$HOME/Sites/zotero-audio-runtime` for runtime data. Override them with the Zotero
preferences `extensions.zotero.audioLicenseStatus.repo` and
`extensions.zotero.audioLicenseStatus.runtime` when needed. The runtime's
`full-library/bundles` and `podcast/license-evidence` directories supply existing
source evidence. No Zotero API key is needed, and no SQLite writes are made.

## Verification and troubleshooting

`zotero-license-status-sync.json` under the runtime directory records the latest
success or failure, counts, changed records, and items skipped due to concurrent
changes. `zotero-license-status.json` records per-attachment outcomes. These files
contain library metadata and belong outside Git. Errors are also logged in
Zotero's Error Console. A failed refresh retains the previous tags until a
successful refresh; it never changes the publication gate.

From Zotero's Run JavaScript window (async enabled), a manual refresh is:

```javascript
await Zotero.AudioLicenseStatus.refresh();
return Zotero.AudioLicenseStatus.lastResult;
```

Run the evaluator and metadata-preservation checks with:

```bash
"$RUNTIME/venv/bin/python" -m pytest
node --test tests/test_zotero_addon.cjs tests/test_zotero_feed_api.cjs
```
