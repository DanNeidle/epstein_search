<!-- (c) Dan Neidle and Tax Policy Associates 2026 -->
# Epstein Agentic Search (Streamlit UI)

(c) Dan Neidle and Tax Policy Associates 2026

This folder contains the prototype web application for agentic search over the indexed Epstein archive.

For download, Docker, indexing, and environment setup instructions, see the main project guide: `../readme.md`.

## App framework

The app is a Streamlit frontend plus an autonomous tool-using agent:

1. `app.py` handles auth, conversation/session lifecycle, user input, and rendering.
2. The agent loop executes tool calls (`es_search`, `es_count`, `es_read`, `es_list`) until it has enough evidence.
3. Results are post-processed into citations/download links and then sent through a verification pass.
4. Messages, tool logs, downloads, and cost metadata are persisted in SQLite and reloaded per conversation.

## Module map (`.py` files)

- `__init__.py`: Package marker for `ai_search`.
- `app.py`: Main Streamlit entrypoint; wires auth, sidebar/navigation, chat UX, agent execution, verification, and persistence.
- `agent_loop.py`: Core autonomous investigation loop; runs model tool calls, enforces full-document reads, validates quote snippets.
- `verification_agent.py`: Second-pass compliance/auditor model that checks draft responses against source text and appends verification status.
- `tooling.py`: Tool definitions and invocation plumbing for Gemini function calls; intent validation, tool output formatting, and cost estimation.
- `es_client.py`: Read-only Elasticsearch/Sist2 adapter used by tools (`search`, `count`, `read`, `list_documents`) plus source-content fetch helpers.
- `citations.py`: Citation extraction/rendering, markdown-to-HTML formatting, and download-link construction from doc IDs/Bates references.
- `assets_utils.py`: Asset/system prompt loading plus safe static-file hydration for download links in restored messages.
- `config.py`: Central constants, regexes, limits, pricing settings, paths, and environment-derived defaults.
- `auth_db.py`: User/auth session database layer; password hashing/verification, login/session cookies, admin/user management.
- `chat_db.py`: Conversation and message persistence; create/list/delete chats, message save/load, title updates.
- `session_state.py`: Streamlit session-state bootstrap and restoration helpers, including cookie auth restore and conversation load.
- `ui_sidebar.py`: Sidebar UI including chat history, new/delete chat actions, sign out, and admin panel toggles.
- `ui_admin.py`: Admin settings and user-management panels, including delete/amend flows.
- `ui_components.py`: Shared UI primitives and theming helpers (brand CSS, hero/header, icon buttons, avatar assets).
