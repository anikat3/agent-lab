import { useState } from "react";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { deleteScenario, deleteSuite, getSuite, updateSuite } from "@/lib/api/suites.functions";
import { EVAL_TYPE_LABELS, type EvalType } from "@/lib/api/eval-config";
import type { Tables } from "@/integrations/supabase/types";
import { DemoBadge, EmptyState, ErrorState, LoadingBlock, PageHeader, SignInPrompt } from "@/components/states";
import { ScenarioForm } from "@/components/scenario-form";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export const Route = createFileRoute("/suites/$id")({
  head: () => ({
    meta: [
      { title: "Test suite — AgentLab" },
      { name: "description", content: "Edit test scenarios and evaluation criteria." },
      { property: "og:title", content: "Test suite — AgentLab" },
      { property: "og:description", content: "Edit test scenarios and evaluation criteria." },
    ],
  }),
  component: SuiteDetail,
});

function SuiteDetail() {
  const { id } = Route.useParams();
  const { userId } = useAuth();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const fetchSuite = useServerFn(getSuite);
  const q = useAppQuery(["suite", id], () => fetchSuite({ data: { id } }));
  const [editing, setEditing] = useState<Tables<"test_scenarios"> | "new" | null>(null);
  const [editSuite, setEditSuite] = useState(false);

  const delScenario = useServerFn(deleteScenario);
  const delSuite = useServerFn(deleteSuite);
  const delScenarioM = useMutation({
    mutationFn: (sid: string) => delScenario({ data: { id: sid } }),
    onSuccess: () => { toast.success("Scenario deleted"); qc.invalidateQueries({ queryKey: ["suite", id] }); },
    onError: (e) => toast.error((e as Error).message),
  });
  const delSuiteM = useMutation({
    mutationFn: () => delSuite({ data: { id } }),
    onSuccess: () => { toast.success("Suite deleted"); qc.invalidateQueries({ queryKey: ["suites"] }); navigate({ to: "/suites" }); },
    onError: (e) => toast.error((e as Error).message),
  });

  if (q.isLoading) return <LoadingBlock rows={6} />;
  if (q.isError || !q.data) return <ErrorState error={q.error ?? "Not found"} onRetry={() => q.refetch()} />;
  const { suite, scenarios } = q.data;
  const canEdit = !!userId && !suite.is_demo;

  return (
    <div>
      <Link to="/suites" className="text-xs text-muted-foreground hover:underline">← Test suites</Link>
      <PageHeader
        title={suite.name}
        description={suite.description ?? undefined}
        actions={
          <>
            {suite.is_demo && <DemoBadge />}
            {canEdit && (
              <>
                <Button size="sm" variant="outline" onClick={() => setEditSuite(true)}><Pencil className="h-3.5 w-3.5" /> Edit</Button>
                <Button size="sm" variant="outline" onClick={() => confirm("Delete this suite and all its scenarios and runs?") && delSuiteM.mutate()}>
                  <Trash2 className="h-3.5 w-3.5" /> Delete
                </Button>
                <Button size="sm" onClick={() => setEditing("new")}><Plus className="h-4 w-4" /> Add scenario</Button>
              </>
            )}
          </>
        }
      />
      {!userId && <div className="mb-6"><SignInPrompt /></div>}
      {suite.is_demo && userId && <p className="mb-4 text-xs text-muted-foreground">Demo suites are read-only. Create your own suite to add scenarios.</p>}
      {scenarios.length === 0 ? (
        <EmptyState title="No scenarios yet" body="Add a scenario with input messages and an evaluation method." />
      ) : (
        <div className="rounded-md border border-border bg-card">
          <table className="w-full text-sm">
            <thead className="border-b border-border text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Scenario</th>
                <th className="px-4 py-2 font-medium">Category</th>
                <th className="px-4 py-2 font-medium">Evaluation</th>
                <th className="px-4 py-2 font-medium">Tags</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {scenarios.map((s) => (
                <tr key={s.id} className="border-b border-border align-top last:border-0">
                  <td className="px-4 py-3">
                    <p className="font-medium">{s.name}</p>
                    <p className="mt-0.5 line-clamp-1 font-mono text-xs text-muted-foreground">
                      {(s.input_messages as { content: string }[])?.at(-1)?.content}
                    </p>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">{s.category ?? "—"}</td>
                  <td className="px-4 py-3"><span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-xs">{EVAL_TYPE_LABELS[s.eval_type as EvalType]}</span></td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">{s.tags.join(", ")}</td>
                  <td className="px-4 py-3 text-right">
                    {canEdit && (
                      <div className="flex justify-end gap-1">
                        <Button size="icon" variant="ghost" className="h-7 w-7" aria-label="Edit" onClick={() => setEditing(s)}><Pencil className="h-3.5 w-3.5" /></Button>
                        <Button size="icon" variant="ghost" className="h-7 w-7" aria-label="Delete" onClick={() => confirm("Delete scenario?") && delScenarioM.mutate(s.id)}><Trash2 className="h-3.5 w-3.5" /></Button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader><DialogTitle>{editing === "new" ? "New scenario" : "Edit scenario"}</DialogTitle></DialogHeader>
          {editing !== null && (
            <ScenarioForm
              suiteId={id}
              initial={editing === "new" ? undefined : editing}
              onDone={() => { setEditing(null); qc.invalidateQueries({ queryKey: ["suite", id] }); qc.invalidateQueries({ queryKey: ["suites"] }); }}
            />
          )}
        </DialogContent>
      </Dialog>
      {editSuite && <EditSuiteDialog suite={suite} onClose={() => setEditSuite(false)} />}
    </div>
  );
}

function EditSuiteDialog({ suite, onClose }: { suite: Tables<"test_suites">; onClose: () => void }) {
  const qc = useQueryClient();
  const update = useServerFn(updateSuite);
  const [name, setName] = useState(suite.name);
  const [description, setDescription] = useState(suite.description ?? "");
  const m = useMutation({
    mutationFn: () => update({ data: { id: suite.id, name, description: description || null } }),
    onSuccess: () => { toast.success("Suite updated"); qc.invalidateQueries({ queryKey: ["suite", suite.id] }); qc.invalidateQueries({ queryKey: ["suites"] }); onClose(); },
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Edit suite</DialogTitle></DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }} className="space-y-3">
          <Input required value={name} onChange={(e) => setName(e.target.value)} />
          <Textarea value={description} onChange={(e) => setDescription(e.target.value)} />
          {m.isError && <ErrorState error={m.error} />}
          <div className="flex justify-end"><Button type="submit" disabled={m.isPending}>Save</Button></div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
