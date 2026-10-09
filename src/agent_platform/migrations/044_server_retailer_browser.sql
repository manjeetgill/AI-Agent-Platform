ALTER TABLE retailer_browser_jobs ADD COLUMN tool text NOT NULL DEFAULT 'browser.retailer_observe';
ALTER TABLE retailer_browser_jobs ADD COLUMN executor text NOT NULL DEFAULT 'paired_browser'
 CHECK(executor IN ('paired_browser','server'));
ALTER TABLE retailer_browser_jobs ADD COLUMN run_token uuid;
CREATE INDEX retailer_browser_server_pending ON retailer_browser_jobs(created_at) WHERE executor='server' AND state='PENDING';
