# 1 More Paper agent plugin

This package follows [Agent Plugins 1.0.0](https://agent-plugins.org/specification):
the portable manifest is root `plugin.json`, MCP connections are in root
`mcp.json`, and the skill is under `skills/research-library/`. No client-specific
manifest replaces these files.

Install the application so `za` is available in the client's executable search
path, then load this directory in a client that supports the Agent Plugins
standard. The included stdio connection runs `za mcp`. It uses the official
Python MCP SDK and the same local catalog and jobs as the standalone CLI.

Local stdio runs as the local user and can read that user's private research.
It exposes named article, job and configured PhD document operations. Project
paths are relative to an explicitly configured root; no arbitrary shell or
filesystem access is exposed. The normal local library location is discovered by the
application; this package contains no credentials or machine-specific paths.

For a phone or other remote client, use the application's deployed Cloudflare
MCP URL shown in setup. Authenticate through the server's OAuth flow. Local
stdio cannot be reached directly from a phone. A portable remote MCP entry has
`type: "streamable-http"` and the deployed HTTPS `/mcp` URL; credentials are
managed by the client and must never be embedded in `mcp.json`.

The CLI and dashboard work independently of this plugin and independently of
any AI. MCP tools provide search/fetch of complete Markdown, library status,
durable job creation/progress/cancellation/retry and complete QA review briefs.

To maintain the package, bump its version when its interface/skill changes.
Validate `plugin.json` and `mcp.json` against their respective authoritative JSON
Schemas using a JSON Schema validator; this is separate from the local MCP
protocol tests in `tests/test_app_mcp.py`. Also load the package in the intended
client to verify its installation behavior. The authoritative schemas are
[plugin.schema.json](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json)
and [mcp.schema.json](https://agent-plugins.org/schemas/1.0.0/mcp.schema.json).

The configured PhD workspace also supports current priorities, document
browsing/reading/search and revision-checked Markdown saves. Remote tools read
and commit directly to GitHub while the Mac is asleep; local stdio uses the
local checkout. See
[the project document guide](../../docs/phd-project-mcp.md).
