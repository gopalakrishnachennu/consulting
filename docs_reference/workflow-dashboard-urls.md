# Consultant workflow dashboard — URL parameters

Base path: `/submissions/workflow/` (name: `workflow-dashboard`).

Query parameters are optional. The app **remembers** the last `q`, `filter`, `sort`, and selected `consultant` in the **session** when you open the page without those parameters, so bookmarks and Slack links can be minimal.

| Parameter   | Values | Meaning |
|------------|--------|---------|
| `q`        | string | Case-insensitive substring on full name, username, or email. |
| `filter`   | `all`, `needs_work`, `needs_assigned`, `needs_draft`, `has_submitted` | Restrict the sidebar list to consultants matching pipeline counts. |
| `sort`     | `name`, `pending`, `submitted` | **Starred** consultants stay at the top; then sort by name, by assigned+draft count (desc), or by submitted count (desc). |
| `consultant` | integer (PK) | Pre-select a consultant and load the right-hand pipeline panel. |
| `clear`    | `1`    | Clears saved session state for this page and redirects to a clean dashboard. |

**Examples**

- Everyone who still has assigned or draft work:  
  `/submissions/workflow/?filter=needs_work`
- Open Alice’s pipeline directly:  
  `/submissions/workflow/?consultant=123`
- Combined (e.g. for Slack):  
  `/submissions/workflow/?filter=needs_work&q=alice&consultant=123&sort=pending`

**Starred consultants** are stored per user in the database (not in the URL). Use the ☆/★ control on each row.

**Stale** badges use the oldest relevant job/draft age; threshold is `WORKFLOW_STALE_DAYS` in settings (default **7**).
