// Fieldwork Neo4j schema initialization
// Runs once at first startup via the neo4j-init compose service.

// Core entities (created by OpenCorporates / engine crawls)
CREATE CONSTRAINT person_id   IF NOT EXISTS FOR (p:Person)   REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT company_id  IF NOT EXISTS FOR (c:Company)  REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT location_id IF NOT EXISTS FOR (l:Location) REQUIRE l.id IS UNIQUE;

// Entities populated by SpiderFoot promotion
CREATE CONSTRAINT email_id    IF NOT EXISTS FOR (e:Email)    REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT phone_id    IF NOT EXISTS FOR (p:Phone)    REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT username_id IF NOT EXISTS FOR (u:Username) REQUIRE u.id IS UNIQUE;
CREATE CONSTRAINT account_id  IF NOT EXISTS FOR (a:Account)  REQUIRE a.id IS UNIQUE;
CREATE CONSTRAINT ip_id       IF NOT EXISTS FOR (i:IP)       REQUIRE i.id IS UNIQUE;
CREATE CONSTRAINT domain_id   IF NOT EXISTS FOR (d:Domain)   REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT breach_id   IF NOT EXISTS FOR (b:Breach)   REQUIRE b.id IS UNIQUE;
CREATE CONSTRAINT leak_id     IF NOT EXISTS FOR (l:Leak)     REQUIRE l.id IS UNIQUE;
CREATE CONSTRAINT wallet_id   IF NOT EXISTS FOR (w:Wallet)   REQUIRE w.id IS UNIQUE;

// Entities populated by crawler integrations (GitHub, NewsAPI, crt.sh)
CREATE CONSTRAINT article_id IF NOT EXISTS FOR (a:Article) REQUIRE a.id IS UNIQUE;

// Lookup indexes
CREATE INDEX person_name     IF NOT EXISTS FOR (p:Person)   ON (p.name);
CREATE INDEX company_name    IF NOT EXISTS FOR (c:Company)  ON (c.name);
CREATE INDEX email_address   IF NOT EXISTS FOR (e:Email)    ON (e.address);
CREATE INDEX username_handle IF NOT EXISTS FOR (u:Username) ON (u.handle);
CREATE INDEX article_url     IF NOT EXISTS FOR (a:Article)  ON (a.url);

// Phase 4 — NLP pipeline indexes
// Filter articles by language or sentiment without full scans
CREATE INDEX article_language       IF NOT EXISTS FOR (a:Article) ON (a.language);
CREATE INDEX article_sentiment      IF NOT EXISTS FOR (a:Article) ON (a.sentiment_label);
// Find NER-sourced nodes quickly (for resolver pass in Phase 5)
CREATE INDEX person_source          IF NOT EXISTS FOR (p:Person)  ON (p.source);
CREATE INDEX company_source         IF NOT EXISTS FOR (c:Company) ON (c.source);

// Phase 6 — New data source node types
CREATE CONSTRAINT aircraft_id        IF NOT EXISTS FOR (a:Aircraft)         REQUIRE a.id IS UNIQUE;
CREATE CONSTRAINT flight_id          IF NOT EXISTS FOR (f:Flight)            REQUIRE f.id IS UNIQUE;
CREATE CONSTRAINT darkweb_mention_id IF NOT EXISTS FOR (m:DarkWebMention)    REQUIRE m.id IS UNIQUE;
CREATE CONSTRAINT telegram_channel_id IF NOT EXISTS FOR (c:TelegramChannel)  REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT telegram_post_id   IF NOT EXISTS FOR (p:TelegramPost)      REQUIRE p.id IS UNIQUE;

// Lookup indexes for new node types
CREATE INDEX aircraft_registration IF NOT EXISTS FOR (a:Aircraft)      ON (a.registration);
CREATE INDEX aircraft_owner        IF NOT EXISTS FOR (a:Aircraft)      ON (a.owner);
CREATE INDEX flight_icao24         IF NOT EXISTS FOR (f:Flight)        ON (f.icao24);
CREATE INDEX flight_route          IF NOT EXISTS FOR (f:Flight)        ON (f.origin_airport, f.dest_airport);
CREATE INDEX darkweb_query         IF NOT EXISTS FOR (m:DarkWebMention) ON (m.query);
CREATE INDEX telegram_channel_name IF NOT EXISTS FOR (c:TelegramChannel) ON (c.name);

// Phase 5 — Graph quality
// The dedup scanner queries POSSIBLE_DUPLICATE and CONFIRMED_DISTINCT
// relationships heavily; relationship type indexes speed this up.
// Note: Neo4j 5 supports relationship property indexes natively.
CREATE INDEX dup_confidence IF NOT EXISTS FOR ()-[r:POSSIBLE_DUPLICATE]-() ON (r.confidence);

// Phase 9 — Investigation CRM
CREATE CONSTRAINT case_id IF NOT EXISTS FOR (c:Case) REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT note_id IF NOT EXISTS FOR (n:Note) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT task_id IF NOT EXISTS FOR (t:Task) REQUIRE t.id IS UNIQUE;
// Fast lookup: all notes/tasks for a given case
CREATE INDEX case_status  IF NOT EXISTS FOR (c:Case) ON (c.status);
CREATE INDEX note_case_id IF NOT EXISTS FOR (n:Note) ON (n.case_id);
CREATE INDEX task_case_id IF NOT EXISTS FOR (t:Task) ON (t.case_id);

// Phase 13 — Multi-user auth
CREATE CONSTRAINT user_id  IF NOT EXISTS FOR (u:User)       REQUIRE u.id IS UNIQUE;
CREATE CONSTRAINT audit_id IF NOT EXISTS FOR (a:AuditEvent) REQUIRE a.id IS UNIQUE;
// Fast lookup: login by username, audit by actor or target
CREATE INDEX user_username  IF NOT EXISTS FOR (u:User)       ON (u.username);
CREATE INDEX user_active    IF NOT EXISTS FOR (u:User)       ON (u.active);
CREATE INDEX audit_user_id  IF NOT EXISTS FOR (a:AuditEvent) ON (a.user_id);
CREATE INDEX audit_target   IF NOT EXISTS FOR (a:AuditEvent) ON (a.target_id);
CREATE INDEX audit_action   IF NOT EXISTS FOR (a:AuditEvent) ON (a.action);
CREATE INDEX audit_ts       IF NOT EXISTS FOR (a:AuditEvent) ON (a.ts);

// Phase 17 — Document ingestion
CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE;
CREATE INDEX document_sha256  IF NOT EXISTS FOR (d:Document) ON (d.sha256);
CREATE INDEX document_mime    IF NOT EXISTS FOR (d:Document) ON (d.mime_type);

// Phase 7 — Continuous monitoring
CREATE CONSTRAINT watch_id IF NOT EXISTS FOR (w:WatchedSubject) REQUIRE w.id IS UNIQUE;
CREATE CONSTRAINT alert_id IF NOT EXISTS FOR (a:Alert)          REQUIRE a.id IS UNIQUE;
// Fast lookup for due-watch query and alert badge count
CREATE INDEX watch_active   IF NOT EXISTS FOR (w:WatchedSubject) ON (w.active);
CREATE INDEX alert_seen     IF NOT EXISTS FOR (a:Alert)          ON (a.seen);
CREATE INDEX alert_watch_id IF NOT EXISTS FOR (a:Alert)          ON (a.watch_id);

// Phase 18 — Global full-text search
// Single multi-label Lucene index covering every searchable text property.
// Managed at runtime by search.py::ensure_fulltext_index(); declared here so
// a clean neo4j-init run also creates it.
CREATE FULLTEXT INDEX fieldwork_fts IF NOT EXISTS
FOR (n:Person|Company|Location|Email|Phone|Username|Account|IP|Domain|Breach|Leak|Wallet|Article|Document|Aircraft|Flight|DarkWebMention|TelegramChannel|TelegramPost|Case|Note|Task)
ON EACH [n.name, n.title, n.handle, n.address, n.content, n.text,
         n.description, n.summary, n.subject, n.body,
         n.url, n.registration, n.owner, n.query, n.filename, n.username];
