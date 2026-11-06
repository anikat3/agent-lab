import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient, type UseQueryOptions } from "@tanstack/react-query";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "@/integrations/supabase/client";

interface AuthState {
  session: Session | null;
  userId: string | null;
  email: string | null;
  ready: boolean;
}

const AuthContext = createContext<AuthState>({ session: null, userId: null, email: null, ready: false });

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const { data: sub } = supabase.auth.onAuthStateChange((event, s) => {
      setSession(s);
      if (event === "SIGNED_IN" || event === "SIGNED_OUT" || event === "USER_UPDATED") {
        queryClient.invalidateQueries();
      }
    });
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setReady(true);
    });
    return () => sub.subscription.unsubscribe();
  }, [queryClient]);

  return (
    <AuthContext.Provider
      value={{ session, userId: session?.user.id ?? null, email: session?.user.email ?? null, ready }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}

/** Query that waits for the session to hydrate and is keyed per user. */
export function useAppQuery<T>(
  key: unknown[],
  fn: () => Promise<T>,
  opts?: Omit<UseQueryOptions<T>, "queryKey" | "queryFn">,
) {
  const { ready, userId } = useAuth();
  return useQuery<T>({
    queryKey: [...key, userId ?? "anon"],
    queryFn: fn,
    ...opts,
    enabled: ready && (opts?.enabled ?? true),
  });
}
