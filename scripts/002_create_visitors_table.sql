-- Create visitor statistics table
CREATE TABLE IF NOT EXISTS public.visitors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id TEXT NOT NULL,
  ip_address TEXT,
  user_agent TEXT,
  page_visited TEXT NOT NULL,
  visited_at TIMESTAMPTZ DEFAULT NOW(),
  user_id UUID REFERENCES public.users(id) ON DELETE SET NULL
);

-- Create indexes for analytics queries
CREATE INDEX IF NOT EXISTS idx_visitors_visited_at ON public.visitors(visited_at);
CREATE INDEX IF NOT EXISTS idx_visitors_page ON public.visitors(page_visited);
CREATE INDEX IF NOT EXISTS idx_visitors_session ON public.visitors(session_id);

-- Enable RLS (only admins can view visitor data)
ALTER TABLE public.visitors ENABLE ROW LEVEL SECURITY;

-- Policy: Only admins can read visitor statistics
CREATE POLICY "visitors_admin_only" ON public.visitors 
  FOR ALL 
  USING (
    EXISTS (
      SELECT 1 FROM public.users 
      WHERE id = auth.uid() AND is_admin = TRUE
    )
  );

-- Create a daily stats summary view for quick dashboard access
CREATE OR REPLACE VIEW public.visitor_daily_stats AS
SELECT 
  DATE(visited_at) as visit_date,
  COUNT(*) as total_visits,
  COUNT(DISTINCT session_id) as unique_visitors,
  COUNT(DISTINCT page_visited) as pages_viewed
FROM public.visitors
GROUP BY DATE(visited_at)
ORDER BY visit_date DESC;
