import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

# User and APIKey now live in the `Authentication` app.


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class EmbeddingMixin(models.Model):
    """Stores vector embeddings for RAG retrieval.

    Stored as JSON for portability — works on SQLite and is swappable
    for pgvector later (see services/retrieval.py).
    """

    embedding = models.JSONField(null=True, blank=True)
    embedding_model = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        abstract = True


class DocumentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


def upload_document_path(instance, filename):
    return f"documents/{instance.id}/{filename}"


class SourceType(models.TextChoices):
    FILE = "file", "File Upload"
    API = "api", "API Sync"
    WEBSITE = "website", "Website Crawl"


class UploadedDocument(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(upload_to=upload_document_path, null=True, blank=True)
    filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(default=0)
    processing_status = models.CharField(
        max_length=20,
        choices=DocumentStatus,
        default=DocumentStatus.PENDING,
    )
    error_message = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )
    source_type = models.CharField(
        max_length=10,
        choices=SourceType,
        default=SourceType.FILE,
    )
    api_url = models.URLField(blank=True, default="", help_text="Source API URL (if synced from API)")
    items_key = models.CharField(max_length=100, blank=True, default="", help_text="JSON key containing items")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["processing_status", "is_active"])]

    def __str__(self):
        return self.filename

    @property
    def file_size_mb(self):
        return round(self.file_size / (1024 * 1024), 2) if self.file_size else 0


class DocumentChunk(TimestampedModel, EmbeddingMixin):
    """A persisted chunk of a parsed document, ready for retrieval."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(UploadedDocument, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField(default=0)
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    # Incremental sync: stable id of the source record (API item / row) and a
    # hash of that record's content, so re-syncs only re-embed what changed.
    source_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # Set when a human corrected this chunk through the knowledge API. Re-syncs
    # and re-indexes rebuild chunks from the source, so this marks the ones that
    # would be overwritten by that -- and lets the UI list them.
    edited_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["document_id", "position"]
        indexes = [
            models.Index(fields=["document", "position"]),
            models.Index(fields=["document", "source_id"]),
        ]
        constraints = [models.UniqueConstraint(fields=["document", "position"], name="unique_chunk_position")]

    def __str__(self):
        return f"{self.document_id}#{self.position}"


class APISourceRecord(TimestampedModel):
    """The verbatim record as the source API returned it — the lossless archive.

    Chunking and embedding are lossy-ish transforms (text is split, whitespace
    trimmed, embeddings are one-way) and they can fail halfway. This table is
    written *before* any of that, so a synced record survives an embedding
    outage, a chunking change, or a re-design of the RAG pipeline: everything
    can be rebuilt from ``raw`` without re-fetching the source.

    ``analysis`` holds the AI reading of the record; it is stored next to the
    raw JSON rather than replacing it, so enrichment never costs us the original.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(UploadedDocument, on_delete=models.CASCADE, related_name="api_records")
    source_id = models.CharField(max_length=255, db_index=True)
    # The untouched record (or page-metadata envelope) exactly as fetched.
    raw = models.JSONField()
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    page = models.PositiveIntegerField(default=1, help_text="1-based page of the paginated source")
    analysis = models.TextField(blank=True, default="")
    analysis_model = models.CharField(max_length=64, blank=True, default="")
    analyzed_at = models.DateTimeField(null=True, blank=True)

    # The canonical structured view of ``raw``: every leaf path, its assigned
    # role, the relationships derived from the nesting, and the identifiers.
    # Kept beside the raw record so an answer can be traced field-by-field, and
    # so the index can be rebuilt without re-fetching or re-analysing anything.
    structured = models.JSONField(null=True, blank=True)
    # The deterministic text rendered from ``structured`` -- what gets embedded
    # (before the AI analysis is appended). Stored so a retrieval problem can be
    # diagnosed from the database instead of by re-running the pipeline.
    rag_text = models.TextField(blank=True, default="")
    entity_type = models.CharField(max_length=64, blank=True, default="", db_index=True)
    title = models.CharField(max_length=255, blank=True, default="")
    # Fraction of the raw record's leaf fields preserved in ``structured``.
    coverage = models.FloatField(null=True, blank=True)
    # This record's quality-control verdict (missing paths, rejected analysis).
    validation = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["document_id", "page", "source_id"]
        indexes = [
            models.Index(fields=["document", "source_id"]),
            models.Index(fields=["document", "entity_type"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["document", "source_id"], name="unique_api_record_per_document")
        ]

    def __str__(self):
        return f"{self.document_id}:{self.source_id}"


class IngestionStage(models.TextChoices):
    """Where a sync got to. Ordered as the pipeline runs."""

    QUEUED = "queued", "Queued"
    FETCHING = "fetching", "Fetching"
    EXTRACTING = "extracting", "Extracting"
    ANALYZING = "analyzing", "Analyzing"
    STRUCTURING = "structuring", "Structuring"
    VALIDATING = "validating", "Validating"
    EMBEDDING = "embedding", "Embedding"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked (quality control)"


class APIIngestionRun(TimestampedModel):
    """One sync attempt, with the real numbers it produced.

    Exists so ingestion is observable after the fact: which endpoint, how many
    records arrived, how many fields survived, what the AI did, and -- when a
    sync is refused -- exactly why the previous knowledge base was kept instead.
    Every number here comes from the run that produced it; none is estimated.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        UploadedDocument,
        on_delete=models.CASCADE,
        related_name="ingestion_runs",
        null=True,
        blank=True,
    )
    # Denormalised owner: a run must stay attributable to its tenant even if the
    # document is later deleted, and it is the column tenant filtering uses.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_ingestion_runs",
        null=True,
        blank=True,
    )
    endpoint = models.URLField(blank=True, default="")
    stage = models.CharField(max_length=20, choices=IngestionStage, default=IngestionStage.FETCHING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    pages_fetched = models.PositiveIntegerField(default=0)
    truncated = models.BooleanField(default=False)
    records_fetched = models.PositiveIntegerField(default=0)
    records_processed = models.PositiveIntegerField(default=0)
    chunks_created = models.PositiveIntegerField(default=0)
    embeddings_created = models.PositiveIntegerField(default=0)
    fields_total = models.PositiveIntegerField(default=0)
    fields_covered = models.PositiveIntegerField(default=0)

    # Raw-payload census taken before any transform ran.
    source_stats = models.JSONField(default=dict, blank=True)
    # Sync stats plus the validation report.
    result_stats = models.JSONField(default=dict, blank=True)
    # Why the run was refused, when it was. Never contains credentials.
    blocked_reason = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["user", "-started_at"]),
            models.Index(fields=["document", "-started_at"]),
        ]

    def __str__(self):
        return f"{self.endpoint or self.document_id} [{self.stage}]"

    @property
    def field_coverage(self) -> float:
        return 1.0 if not self.fields_total else self.fields_covered / self.fields_total


class FeedbackRating(models.TextChoices):
    UP = "up", "Helpful"
    DOWN = "down", "Not helpful"


class AnswerSource(models.TextChoices):
    RAG = "rag", "RAG"


class ChatFeedback(TimestampedModel):
    """User feedback on a chat answer — feeds quality monitoring + retraining."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.TextField()
    answer = models.TextField()
    source = models.CharField(
        max_length=16,
        choices=AnswerSource,
        default=AnswerSource.RAG,
    )
    source_id = models.CharField(max_length=64, blank=True, default="")
    rating = models.CharField(max_length=8, choices=FeedbackRating)
    comment = models.TextField(blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_feedback",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["rating", "source"])]

    def __str__(self):
        return f"{self.rating} {self.question[:40]}"


class BotTone(models.TextChoices):
    FRIENDLY = "friendly", "Friendly"
    FORMAL = "formal", "Formal"
    CONCISE = "concise", "Concise"


class ChatbotConfig(TimestampedModel):
    """Per-user chatbot identity + retrieval settings.

    Identity fields (name/company/language/tone) are injected into a
    system-controlled prompt; the strict grounding rules themselves are NOT
    user-editable, so every tenant's bot answers only from its own data.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chatbot_config",
    )
    assistant_name = models.CharField(max_length=100, blank=True, default="Assistant")
    company_name = models.CharField(max_length=150, blank=True, default="")
    # "auto" = reply in the user's language; otherwise a fallback language code.
    default_language = models.CharField(max_length=10, blank=True, default="auto")
    tone = models.CharField(max_length=20, choices=BotTone, default=BotTone.FRIENDLY)
    # When True (default), the bot never answers outside the retrieved context.
    strict_grounding = models.BooleanField(default=True)
    # Optional static reply when nothing matches the user's data. Blank => the
    # LLM produces a localized "I don't have that" handoff (still grounded).
    no_answer_message = models.TextField(blank=True, default="")
    # Optional per-bot retrieval overrides (fall back to global settings if null).
    top_k = models.PositiveIntegerField(null=True, blank=True)
    similarity_threshold = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"ChatbotConfig({self.user.username})"


class TenantQuota(TimestampedModel):
    """Per-tenant resource limits. A tenant is a User.

    Any field left null falls back to the global default in settings, so a row
    only needs to override what differs for that tenant. Enforced in
    services/quota.py at upload time (documents/size) and chat time (rate limit,
    monthly token cap).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="quota",
    )
    max_documents = models.PositiveIntegerField(null=True, blank=True)
    max_total_mb = models.FloatField(null=True, blank=True)
    max_requests_per_min = models.PositiveIntegerField(null=True, blank=True)
    # Monthly input+output token budget. null/0 = unlimited.
    monthly_token_cap = models.PositiveBigIntegerField(null=True, blank=True)
    # Hard stop: a suspended tenant cannot upload or chat.
    is_suspended = models.BooleanField(default=False)

    def __str__(self):
        return f"TenantQuota({self.user.username})"


class UsageKind(models.TextChoices):
    CHAT = "chat", "Chat"
    EMBEDDING = "embedding", "Embedding"
    ANALYSIS = "analysis", "AI analysis"
    DOCUMENT = "document", "Document indexing"
    KNOWLEDGE = "knowledge", "Knowledge edit"
    CRAWL = "crawl", "Website crawl"
    API_SYNC = "api_sync", "API import"
    TRANSLATION = "translation", "Tree translation"


class UsageRecord(TimestampedModel):
    """One metered operation for a tenant (a chat answer, a document indexed, a
    tree translation run, ...) — its tokens, cost and the points it was charged.

    Stores token counts, estimated cost, latency and retrieval confidence. The
    question text is intentionally NOT stored here (privacy); ChatFeedback keeps
    text when the user opts to rate an answer.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_records",
    )
    kind = models.CharField(max_length=16, choices=UsageKind, default=UsageKind.CHAT)
    model = models.CharField(max_length=64, blank=True, default="")
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    cost_usd = models.FloatField(default=0.0)
    # Points charged, in milli-points (0 for staff, whose usage is never charged).
    points_milli = models.BigIntegerField(default=0)
    response_time_ms = models.PositiveIntegerField(default=0)
    confident = models.BooleanField(default=True)
    chunk_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["user", "kind", "created_at"]),
        ]

    def __str__(self):
        return f"Usage({self.user_id} {self.kind} {self.tokens_in}+{self.tokens_out})"


class UnansweredStatus(models.TextChoices):
    NEW = "new", "New"
    REVIEWED = "reviewed", "Reviewed"
    ANSWERED = "answered", "Answered"
    DISMISSED = "dismissed", "Dismissed"


class UnansweredQuestion(TimestampedModel):
    """A question the bot could not confidently answer — a knowledge gap.

    Captured (per tenant) when a chat turn comes back low-confidence AND an AI
    filter judges it a genuine question the business should be able to answer
    (greetings/chit-chat/off-topic are dropped; the kept reason is stored).

    De-duplicated per tenant on ``question_key`` (a normalised form of the
    question), so the same gap asked many ways bumps ``occurrences`` instead of
    creating rows. Review by frequency, then resolve into knowledge.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="unanswered_questions",
    )
    # The original question text (first occurrence) shown to reviewers.
    question = models.TextField()
    # Normalised dedup key (lowercased, punctuation stripped, whitespace collapsed).
    question_key = models.CharField(max_length=500, db_index=True)
    language = models.CharField(max_length=8, blank=True, default="")
    reason = models.TextField(blank=True, default="", help_text="Why the AI kept it.")
    status = models.CharField(
        max_length=12,
        choices=UnansweredStatus,
        default=UnansweredStatus.NEW,
    )
    occurrences = models.PositiveIntegerField(default=1)
    last_asked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-last_asked_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["user", "last_asked_at"]),
        ]
        constraints = [models.UniqueConstraint(fields=["user", "question_key"], name="unique_user_question_key")]

    def __str__(self):
        return f"Unanswered({self.user_id} x{self.occurrences}: {self.question[:40]})"


class AvailableLanguage(TimestampedModel):
    """A language the guided question tree can be presented in.

    The canonical language (settings.GUIDED_TREE_CANONICAL_LANGUAGE) is where all
    edits happen; every other active language is a generated mirror of it.

    ``owner`` set = a language that tenant added for its own tree; ``owner`` null
    = a global language (staff-managed) mirrored for every tenant.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tree_languages",
    )
    code = models.CharField(max_length=10, help_text="e.g. 'ar', 'en'")
    name = models.CharField(max_length=64, help_text="Display name, e.g. 'العربية'")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "code"], name="uniq_tree_language_per_owner"),
            models.UniqueConstraint(
                fields=["code"], condition=models.Q(owner__isnull=True), name="uniq_global_tree_language"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class QuestionTreeNode(TimestampedModel):
    """A node in a tenant's guided question tree.

    Tap a node → get its ``answer`` and/or its child questions to drill into.
    The tree exists once per language: the canonical language is authored, and
    every other language is a MIRROR matched **by position** (same ``order`` under
    the matching parent), never by foreign key. ``order`` must be unique among a
    node's active siblings (per owner + language) or positional matching breaks —
    enforced in the write path (services.guided_tree).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="question_tree_nodes",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    title = models.CharField(max_length=500)
    answer = models.TextField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    language = models.CharField(max_length=10, default="ar", db_index=True)
    # Mirrors only: hash of the canonical title + answer this translation was made
    # from. A mismatch (or blank, after a failed translation) means re-translate.
    source_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["order", "created_at"]
        indexes = [
            models.Index(fields=["owner", "language", "parent"]),
            models.Index(fields=["owner", "language", "is_active"]),
        ]

    def __str__(self):
        return f"[{self.language}] {self.title[:50]}"

    def is_root(self) -> bool:
        return self.parent_id is None

    def has_children(self) -> bool:
        return self.children.filter(is_active=True).exists()

    def get_children(self):
        return self.children.filter(is_active=True).order_by("order", "created_at")
