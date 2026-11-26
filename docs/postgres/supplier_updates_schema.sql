
CREATE TABLE api_prontocardio.supplier_bulk_jobs (
	id VARCHAR(36) NOT NULL,
	kind VARCHAR(15) NOT NULL,
	channel VARCHAR(15) NOT NULL,
	status VARCHAR(30) NOT NULL,
	created_by INTEGER NOT NULL,
	approved_by INTEGER,
	request_key VARCHAR(64) NOT NULL,
	version INTEGER NOT NULL,
	input_fingerprint VARCHAR(64) NOT NULL,
	configuration JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	approved_at TIMESTAMP WITH TIME ZONE,
	cancelled_by INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT uq_supplier_bulk_request UNIQUE (created_by, request_key)
)

;

CREATE TABLE api_prontocardio.supplier_payment_confirmations (
	id VARCHAR(36) NOT NULL,
	company INTEGER NOT NULL,
	company_name VARCHAR(200) NOT NULL,
	supplier VARCHAR(100) NOT NULL,
	name VARCHAR(200) NOT NULL,
	document VARCHAR(20) NOT NULL,
	items JSON NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	total NUMERIC(15, 2) NOT NULL,
	note TEXT NOT NULL,
	confirmed_by INTEGER NOT NULL,
	confirmed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE api_prontocardio.supplier_update_cases (
	id SERIAL NOT NULL,
	company INTEGER NOT NULL,
	supplier VARCHAR(100) NOT NULL,
	name VARCHAR(200) NOT NULL,
	document VARCHAR(20) NOT NULL,
	contact JSON NOT NULL,
	stage VARCHAR(30) NOT NULL,
	responsible VARCHAR(150) NOT NULL,
	next_action TEXT NOT NULL,
	next_date VARCHAR(10),
	version INTEGER NOT NULL,
	validated_balance NUMERIC(15, 2),
	created_by INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_supplier_update_company_supplier UNIQUE (company, supplier)
)

;

CREATE TABLE api_prontocardio.supplier_bulk_recipients (
	id VARCHAR(36) NOT NULL,
	job_id VARCHAR(36) NOT NULL,
	target VARCHAR(100) NOT NULL,
	snapshot JSON NOT NULL,
	destination VARCHAR(254) NOT NULL,
	destination_override BOOLEAN NOT NULL,
	status VARCHAR(25) NOT NULL,
	error TEXT NOT NULL,
	link_nonce VARCHAR(36) NOT NULL,
	link_ref VARCHAR(36),
	lease_token VARCHAR(36),
	lease_until TIMESTAMP WITH TIME ZONE,
	provider_id VARCHAR(255),
	accepted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_supplier_bulk_target UNIQUE (job_id, target),
	FOREIGN KEY(job_id) REFERENCES api_prontocardio.supplier_bulk_jobs (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_bulk_recipients_job_id ON api_prontocardio.supplier_bulk_recipients (job_id);

CREATE TABLE api_prontocardio.supplier_payment_events (
	id SERIAL NOT NULL,
	confirmation_id VARCHAR(36) NOT NULL,
	actor INTEGER NOT NULL,
	kind VARCHAR(40) NOT NULL,
	details JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(confirmation_id) REFERENCES api_prontocardio.supplier_payment_confirmations (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_payment_events_confirmation_id ON api_prontocardio.supplier_payment_events (confirmation_id);

CREATE TABLE api_prontocardio.supplier_payment_files (
	id VARCHAR(36) NOT NULL,
	confirmation_id VARCHAR(36) NOT NULL,
	name VARCHAR(200) NOT NULL,
	content_type VARCHAR(100) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	content BYTEA NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(confirmation_id) REFERENCES api_prontocardio.supplier_payment_confirmations (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_payment_files_confirmation_id ON api_prontocardio.supplier_payment_files (confirmation_id);

CREATE TABLE api_prontocardio.supplier_payment_items (
	key VARCHAR(100) NOT NULL,
	confirmation_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (key),
	FOREIGN KEY(confirmation_id) REFERENCES api_prontocardio.supplier_payment_confirmations (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_payment_items_confirmation_id ON api_prontocardio.supplier_payment_items (confirmation_id);

CREATE TABLE api_prontocardio.supplier_payment_links (
	id VARCHAR(36) NOT NULL,
	confirmation_id VARCHAR(36) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	created_by INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(confirmation_id) REFERENCES api_prontocardio.supplier_payment_confirmations (id),
	UNIQUE (token_hash)
)

;
CREATE INDEX ix_api_prontocardio_supplier_payment_links_confirmation_id ON api_prontocardio.supplier_payment_links (confirmation_id);

CREATE TABLE api_prontocardio.supplier_update_events (
	id SERIAL NOT NULL,
	case_id INTEGER NOT NULL,
	actor INTEGER NOT NULL,
	kind VARCHAR(40) NOT NULL,
	details JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(case_id) REFERENCES api_prontocardio.supplier_update_cases (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_update_events_case_id ON api_prontocardio.supplier_update_events (case_id);

CREATE TABLE api_prontocardio.supplier_update_invites (
	id VARCHAR(36) NOT NULL,
	case_id INTEGER NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	used_at TIMESTAMP WITH TIME ZONE,
	created_by INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(case_id) REFERENCES api_prontocardio.supplier_update_cases (id),
	UNIQUE (token_hash)
)

;
CREATE INDEX ix_api_prontocardio_supplier_update_invites_case_id ON api_prontocardio.supplier_update_invites (case_id);

CREATE TABLE api_prontocardio.supplier_bulk_attempts (
	id VARCHAR(36) NOT NULL,
	recipient_id VARCHAR(36) NOT NULL,
	status VARCHAR(25) NOT NULL,
	actor INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	error TEXT NOT NULL,
	provider_id VARCHAR(255),
	PRIMARY KEY (id),
	FOREIGN KEY(recipient_id) REFERENCES api_prontocardio.supplier_bulk_recipients (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_bulk_attempts_recipient_id ON api_prontocardio.supplier_bulk_attempts (recipient_id);

CREATE TABLE api_prontocardio.supplier_update_submissions (
	id VARCHAR(36) NOT NULL,
	case_id INTEGER NOT NULL,
	invite_id VARCHAR(36) NOT NULL,
	payload JSON NOT NULL,
	declared_balance NUMERIC(15, 2) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(case_id) REFERENCES api_prontocardio.supplier_update_cases (id),
	UNIQUE (invite_id),
	FOREIGN KEY(invite_id) REFERENCES api_prontocardio.supplier_update_invites (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_update_submissions_case_id ON api_prontocardio.supplier_update_submissions (case_id);

CREATE TABLE api_prontocardio.supplier_update_attachments (
	id VARCHAR(36) NOT NULL,
	submission_id VARCHAR(36) NOT NULL,
	name VARCHAR(200) NOT NULL,
	content_type VARCHAR(100) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	content BYTEA NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(submission_id) REFERENCES api_prontocardio.supplier_update_submissions (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_update_attachments_submission_id ON api_prontocardio.supplier_update_attachments (submission_id);

CREATE TABLE api_prontocardio.supplier_update_reviews (
	id SERIAL NOT NULL,
	case_id INTEGER NOT NULL,
	submission_id VARCHAR(36) NOT NULL,
	actor INTEGER NOT NULL,
	decision VARCHAR(25) NOT NULL,
	balance NUMERIC(15, 2),
	reason TEXT NOT NULL,
	as_of VARCHAR(10) NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	system_position JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(case_id) REFERENCES api_prontocardio.supplier_update_cases (id),
	FOREIGN KEY(submission_id) REFERENCES api_prontocardio.supplier_update_submissions (id)
)

;
CREATE INDEX ix_api_prontocardio_supplier_update_reviews_case_id ON api_prontocardio.supplier_update_reviews (case_id);
