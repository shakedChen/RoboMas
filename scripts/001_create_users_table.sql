-- Create users table for authentication
CREATE TABLE IF NOT EXISTS public.users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username TEXT UNIQUE NOT NULL,
  email TEXT UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_login TIMESTAMPTZ
);

-- Create index on username for faster lookups
CREATE INDEX IF NOT EXISTS idx_users_username ON public.users(username);
CREATE INDEX IF NOT EXISTS idx_users_email ON public.users(email);

-- Enable RLS
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only read their own data (except admins)
CREATE POLICY "users_select_own" ON public.users 
  FOR SELECT 
  USING (auth.uid()::text = id::text OR is_admin = TRUE);

-- Insert the admin user (password: gsdgsdg#@$@#23dfs!)
-- Using bcrypt hash for the password
INSERT INTO public.users (username, email, password_hash, is_admin)
VALUES (
  'shutzibutzi',
  'admin@robomas.co.il',
  '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4EdgY9GVL/GYvHxu',
  TRUE
) ON CONFLICT (username) DO NOTHING;
