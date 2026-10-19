# Provenance

This component is maintained in ailocal under the repository's Apache-2.0 license. It contains no copied third-party implementation. Its subprocess runtime installs the official MCP Python SDK, pinned in `requirements.txt`; that project is MIT licensed: [upstream](https://github.com/modelcontextprotocol/python-sdk).

Custom code supplies enforced site scoping, explicit metered-provider selection, bounded public-page reading, and local synthesis through LiteLLM. SearXNG supplies search and provider integration; the official MCP SDK supplies the protocol. No alternate crawler, model service or MCP protocol implementation is bundled.
