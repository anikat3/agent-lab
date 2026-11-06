<!-- LOVABLE:BEGIN -->
> [!IMPORTANT]
> This project is connected to [Lovable](https://lovable.dev). Avoid rewriting
> published git history — force pushing, or rebasing/amending/squashing commits
> that are already pushed — as it rewrites history on Lovable's side and the
> user will likely lose their project history.
>
> Commits you push to the connected branch sync back to Lovable and show up in
> the editor, so keep the branch in a working state.

## What Lovable manages

Rules outside this block are the project's own. Change the pieces below in
Lovable, not by hand. With the [Lovable MCP](https://mcp.lovable.dev)
connected, `send_message` asks Lovable to do it; otherwise ask the user to
use the editor.

- **Database.** Do not add or edit `supabase/migrations/` or `drizzle/`.
  Ask Lovable to change the schema.
- **Generated files.** Do not edit a file whose header says it is automatically
  generated.
- **AI gateway.** Keep model calls on Lovable's gateway, and keep
  `LOVABLE_API_KEY` out of browser code.
- **Connectors.** Lovable provides native connectors. `list_connectors` shows
  which are enabled; `add_connector` only returns the dashboard link. When a
  connector is enabled, calls go through the connector gateway and need
  `LOVABLE_API_KEY`. When Lovable does not provide one, users can bring their
  own key and store it in Secrets.
- **Secrets.** Lovable manages them. Do not put secret values in the repo.
<!-- LOVABLE:END -->

## Project rules
- The evaluation engine is an external Python service; the app only proxies to it via server functions in `src/lib/api/` (`engine.server.ts`). Never simulate execution in-app — results must come from the engine.
- `docs/api-contract.md` and `src/lib/api/types.ts` describe the same REST contract; change both together.
- Eval-config validation lives in `src/lib/api/eval-config.ts` and is reused by client forms and server functions so both sides reject the same input.
- Demo rows are flagged `is_demo`; anon access is restricted to them by RLS and by explicit filters in read server functions (`optionalAuth`). Real metrics exclude demo rows.
- Pure run rules (transitions, retry/backoff, rate limit, demo scoping) live in `engine-logic.ts` and are unit-tested in `src/test/engine-logic.test.ts`.
- Retrying a run inserts a new linked run (`retry_of_run_id`); results are upserted on `(run_id, scenario_id)`.
