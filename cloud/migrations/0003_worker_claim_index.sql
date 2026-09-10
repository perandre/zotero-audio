CREATE INDEX IF NOT EXISTS jobs_worker ON jobs(worker_id,status,lease_expires);
