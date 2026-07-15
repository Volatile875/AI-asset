# DecisionDNA Jira MCP Server

This MCP server fetches Jira ticket status from Atlassian Jira Cloud and stores:

- the current ticket status
- when Jira says the current status last changed
- every fetched status snapshot
- status transition history from the Jira changelog

## Environment

Set these variables before running:

```bash
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_atlassian_api_token
JIRA_STATUS_DB_PATH=./data/jira_status_history.db
```

`JIRA_STATUS_DB_PATH` is optional. If omitted, the server stores history at
`./data/jira_status_history.db` relative to this folder.

## Run

```bash
cd mcp-server
pip install -r requirements.txt
python server.py
```

## Tools

- `fetch_jira_ticket_status(ticket_key)` fetches one ticket from Jira and stores the snapshot/history.
- `fetch_many_jira_ticket_statuses(ticket_keys)` fetches multiple tickets.
- `get_stored_jira_ticket_status(ticket_key)` reads the latest stored status.
- `get_jira_ticket_status_history(ticket_key, limit)` reads stored snapshots and transitions.
