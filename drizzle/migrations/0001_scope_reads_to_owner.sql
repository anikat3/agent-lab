DROP POLICY IF EXISTS "auth reads all" ON public.agent_configs;
CREATE POLICY "auth reads own or demo" ON public.agent_configs FOR SELECT TO authenticated
  USING (is_demo = true OR created_by = auth.uid());

DROP POLICY IF EXISTS "auth reads all" ON public.test_suites;
CREATE POLICY "auth reads own or demo" ON public.test_suites FOR SELECT TO authenticated
  USING (is_demo = true OR created_by = auth.uid());

DROP POLICY IF EXISTS "auth reads all" ON public.eval_runs;
CREATE POLICY "auth reads own or demo" ON public.eval_runs FOR SELECT TO authenticated
  USING (is_demo = true OR created_by = auth.uid());

DROP POLICY IF EXISTS "auth reads all" ON public.test_scenarios;
CREATE POLICY "auth reads own or demo" ON public.test_scenarios FOR SELECT TO authenticated
  USING (is_demo = true OR EXISTS (SELECT 1 FROM public.test_suites s WHERE s.id = suite_id AND s.created_by = auth.uid()));

DROP POLICY IF EXISTS "auth reads all" ON public.eval_results;
CREATE POLICY "auth reads own or demo" ON public.eval_results FOR SELECT TO authenticated
  USING (is_demo = true OR EXISTS (SELECT 1 FROM public.eval_runs r WHERE r.id = run_id AND r.created_by = auth.uid()));