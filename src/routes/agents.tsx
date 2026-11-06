import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { createAgent, deleteAgent, listAgents, updateAgent } from "@/lib/api/agents.functions";
import { agentInputSchema } from "@/lib/api/eval-config";
import type { Tables } from "@/integrations/supabase/types";
import { DemoBadge, EmptyState, ErrorState, LoadingBlock, PageHeader, SignInPrompt } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export const Route = createFileRoute("/agents")({
  head: () => ({
    meta: [
      { title: "Agents — AgentLab" },
      { name: "description", content: "Configure the agents (model and system prompt) your evaluation engine executes." },
      { property: "og:title", content: "Agents — AgentLab" },
      { property: "og:description", content: "Agent configurations for evaluation runs." },
    ],
  }),
  component: AgentsPage,
});

type Agent = Tables<"agent_configs">;

function AgentsPage() {
  const { userId } = useAuth();
  const qc = useQueryClient();
  const fetchAgents = useServerFn(listAgents);
  const del = useServerFn(deleteAgent);
  const q = useAppQuery(["agents"], () => fetchAgents());
  const [editing, setEditing] = useState<Agent | "new" | null>(null);
  const delM = useMutation({
    mutationFn: (id: string) => del({ data: { id } }),
    onSuccess: () => { toast.success("Agent deleted"); qc.invalidateQueries({ queryKey: ["agents"] }); },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <div>
      <PageHeader
        title="Agents"
        description="Agent name, model and system prompt are sent to the engine with every run."
        actions={userId && <Button size="sm" onClick={() => setEditing("new")}><Plus className="h-4 w-4" /> New agent</Button>}
      />
      {!userId && <div className="mb-6"><SignInPrompt /></div>}
      {q.isLoading ? <LoadingBlock /> : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : !q.data?.length ? (
        <EmptyState title="No agents yet" body="Create an agent configuration to launch evaluations." />
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {q.data.map((a) => (
            <div key={a.id} className="rounded-md border border-border bg-card p-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <p className="font-medium">{a.name} {a.is_demo && <DemoBadge className="ml-1" />}</p>
                  <p className="mt-0.5 font-mono text-xs text-primary">{a.model}</p>
                </div>
                {userId && !a.is_demo && (
                  <div className="flex gap-1">
                    <Button size="icon" variant="ghost" className="h-7 w-7" aria-label="Edit" onClick={() => setEditing(a)}><Pencil className="h-3.5 w-3.5" /></Button>
                    <Button size="icon" variant="ghost" className="h-7 w-7" aria-label="Delete" onClick={() => confirm("Delete agent?") && delM.mutate(a.id)}><Trash2 className="h-3.5 w-3.5" /></Button>
                  </div>
                )}
              </div>
              <p className="mt-3 line-clamp-3 whitespace-pre-wrap font-mono text-xs text-muted-foreground">{a.system_prompt || "(no system prompt)"}</p>
            </div>
          ))}
        </div>
      )}
      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="max-w-xl">
          <DialogHeader><DialogTitle>{editing === "new" ? "New agent" : "Edit agent"}</DialogTitle></DialogHeader>
          {editing !== null && <AgentForm initial={editing === "new" ? undefined : editing} onDone={() => { setEditing(null); qc.invalidateQueries({ queryKey: ["agents"] }); }} />}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function AgentForm({ initial, onDone }: { initial?: Agent | undefined; onDone: () => void }) {
  const create = useServerFn(createAgent);
  const update = useServerFn(updateAgent);
  const [name, setName] = useState(initial?.name ?? "");
  const [model, setModel] = useState(initial?.model ?? "");
  const [prompt, setPrompt] = useState(initial?.system_prompt ?? "");
  const [err, setErr] = useState<string | null>(null);
  const m = useMutation({
    mutationFn: (d: { name: string; model: string; system_prompt: string }) =>
      initial ? update({ data: { id: initial.id, ...d } }) : create({ data: d }),
    onSuccess: () => { toast.success("Agent saved"); onDone(); },
  });
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        const d = { name, model, system_prompt: prompt };
        const r = agentInputSchema.safeParse(d);
        if (!r.success) return setErr(r.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; "));
        setErr(null);
        m.mutate(d);
      }}
    >
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5"><Label>Name</Label><Input value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div className="space-y-1.5"><Label>Model</Label><Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="gpt-4o-mini" /></div>
      </div>
      <div className="space-y-1.5"><Label>System prompt</Label><Textarea rows={8} className="font-mono text-xs" value={prompt} onChange={(e) => setPrompt(e.target.value)} /></div>
      {(err || m.isError) && <p role="alert" className="font-mono text-xs text-destructive">{err ?? (m.error as Error).message}</p>}
      <div className="flex justify-end"><Button type="submit" disabled={m.isPending}>{m.isPending ? "Saving…" : "Save"}</Button></div>
    </form>
  );
}
