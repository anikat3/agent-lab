import { useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus } from "lucide-react";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { createSuite, listSuites } from "@/lib/api/suites.functions";
import { DemoBadge, EmptyState, ErrorState, fmtDate, LoadingBlock, PageHeader, SignInPrompt } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export const Route = createFileRoute("/suites/")({
  head: () => ({
    meta: [
      { title: "Test suites — AgentLab" },
      { name: "description", content: "Organize test scenarios for your conversational AI agents." },
      { property: "og:title", content: "Test suites — AgentLab" },
      { property: "og:description", content: "Organize test scenarios for your conversational AI agents." },
    ],
  }),
  component: SuitesPage,
});

function SuitesPage() {
  const { userId } = useAuth();
  const fetchSuites = useServerFn(listSuites);
  const q = useAppQuery(["suites"], () => fetchSuites());
  const [open, setOpen] = useState(false);

  return (
    <div>
      <PageHeader
        title="Test suites"
        description="Groups of scenarios with expected behavior and evaluation criteria."
        actions={userId && <Button size="sm" onClick={() => setOpen(true)}><Plus className="h-4 w-4" /> New suite</Button>}
      />
      {!userId && <div className="mb-6"><SignInPrompt /></div>}
      {q.isLoading ? (
        <LoadingBlock />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : !q.data?.length ? (
        <EmptyState title="No suites yet" body="Create a suite to start adding test scenarios." />
      ) : (
        <div className="rounded-md border border-border bg-card">
          <table className="w-full text-sm">
            <thead className="border-b border-border text-left text-xs uppercase text-muted-foreground">
              <tr><th className="px-4 py-2 font-medium">Name</th><th className="px-4 py-2 font-medium">Scenarios</th><th className="px-4 py-2 font-medium">Updated</th></tr>
            </thead>
            <tbody>
              {q.data.map((s) => (
                <tr key={s.id} className="border-b border-border last:border-0 hover:bg-accent/40">
                  <td className="px-4 py-3">
                    <Link to="/suites/$id" params={{ id: s.id }} className="font-medium hover:underline">{s.name}</Link>
                    {s.is_demo && <DemoBadge className="ml-2" />}
                    {s.description && <p className="mt-0.5 text-xs text-muted-foreground">{s.description}</p>}
                  </td>
                  <td className="px-4 py-3 font-mono">{s.scenario_count}</td>
                  <td className="px-4 py-3 text-muted-foreground">{fmtDate(s.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <SuiteDialog open={open} onOpenChange={setOpen} />
    </div>
  );
}

export function SuiteDialog({
  open, onOpenChange, initial,
}: { open: boolean; onOpenChange: (o: boolean) => void; initial?: { id: string; name: string; description: string | null } }) {
  const qc = useQueryClient();
  const create = useServerFn(createSuite);
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const m = useMutation({
    mutationFn: () => create({ data: { name, description: description || null } }),
    onSuccess: () => {
      toast.success("Suite created");
      qc.invalidateQueries({ queryKey: ["suites"] });
      onOpenChange(false);
      setName(""); setDescription("");
    },
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>New test suite</DialogTitle></DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }} className="space-y-3">
          <div className="space-y-1.5"><Label>Name</Label><Input required value={name} onChange={(e) => setName(e.target.value)} /></div>
          <div className="space-y-1.5"><Label>Description</Label><Textarea value={description} onChange={(e) => setDescription(e.target.value)} /></div>
          {m.isError && <ErrorState error={m.error} />}
          <DialogFooter><Button type="submit" disabled={m.isPending}>{m.isPending ? "Saving…" : "Create"}</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
