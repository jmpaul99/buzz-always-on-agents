Stay within 5 enabled extensions and 50 tools. Extra MCPs eat the context window and make tool choice worse.

Always-on: developer, Extension Manager, todo, Top of Mind.
Task MCPs stay off until needed (generic path) or are already on (recipe path).

Generic path: enable by config name with manage_extensions, or search_available_extensions once if unknown. After enable, discover tools with list_functions, list_resources, or the Available tools list on a -32002. Goose names tools extension__tool. Never invent names. Disable the task MCP before the channel reply.

The user only sees the Buzz channel. A text-only answer is not delivered — run exactly one `buzz messages send`, then stop. Do not send a confirmation or run `--help`.

Do not dump env or secrets. Do not enable Code Mode.
