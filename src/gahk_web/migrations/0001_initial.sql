-- Everything here is a cache or an outbox; intern.gahk.dk holds the truth.

-- The single row of site state: the cached snapshot and when the site was last rendered from it.
CREATE TABLE site_state (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    snapshot jsonb,
    revision text,
    published_at timestamptz,
    received_at timestamptz,
    rendered_at timestamptz,
    rendered_on date,
    rendered_version text
);
INSERT INTO site_state (id) VALUES (1);

-- Image bytes received with PUT /api/images/<sha256>, kept as blobs under state_dir/images.
CREATE TABLE images (
    sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content_type text NOT NULL,
    size integer NOT NULL,
    stored_at timestamptz NOT NULL DEFAULT now()
);

-- The public copies of those blobs: public_html/billeder/<sha16>/<name>. The name comes from the
-- snapshot, so one blob can have several public names.
CREATE TABLE image_files (
    rel_path text PRIMARY KEY,
    sha256 text NOT NULL
);

-- Every file the renderer wrote into public_html, so a page that disappears is deleted and nothing
-- else ever is. url is the canonical address of an HTML page (NULL for other files): the CGI
-- fallback redirects the other slash form of a page there.
CREATE TABLE generated_files (
    rel_path text PRIMARY KEY,
    url text,
    rendered_at timestamptz NOT NULL
);

-- Outbox: optagelse form submissions until intern pulls and acknowledges them.
CREATE TABLE applications (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('rundvisning', 'fremleje')),
    fields jsonb NOT NULL,
    submitted_at timestamptz NOT NULL
);

-- Form submissions per client (HMAC of the IP) for the hourly rate limit; purged after a day.
CREATE TABLE form_rate_limit (
    client text NOT NULL,
    at timestamptz NOT NULL
);
CREATE INDEX form_rate_limit_client_at ON form_rate_limit (client, at);

-- Outbox: views and unique visitors per day and path ('*' = the whole site) until intern acks them.
CREATE TABLE visits (
    day date NOT NULL,
    path text NOT NULL,
    views integer NOT NULL DEFAULT 0,
    visitors integer NOT NULL DEFAULT 0,
    PRIMARY KEY (day, path)
);

-- Which visitor hashes were already counted per day and path; purged after two days.
CREATE TABLE visit_seen (
    day date NOT NULL,
    path text NOT NULL,
    visitor text NOT NULL,
    PRIMARY KEY (day, path, visitor)
);
