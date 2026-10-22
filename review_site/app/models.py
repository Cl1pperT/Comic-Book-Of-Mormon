"""Database tables. One review = one reviewer's pass over one version of one chapter.

Chapters have versions (a fingerprint of their page images), versions have pages, pages have clickable panels.
Issues hang off a review, so a reviewer only ever sees their own, and history survives new versions."""
import json
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, TypeDecorator,
                        UniqueConstraint, false, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores UTC, returns timezone-aware datetimes on every database (SQLite drops the zone)."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="reviewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    credit_ok: Mapped[bool] = mapped_column(Boolean, default=False)   # show display_name on the thank-you page
    can_choose: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())   # pre-approved to pick any chapter
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="reviewer")
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    can_choose: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())   # new users start pre-approved


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Chapter(Base):
    __tablename__ = "chapters"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)        # the run folder, e.g. "alma/018"
    book: Mapped[str] = mapped_column(String(40))
    number: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(12), default="closed")  # closed | open | retired
    versions: Mapped[list["ChapterVersion"]] = relationship(back_populates="chapter", order_by="ChapterVersion.number")

    @property
    def title(self):
        return f"{self.book} {self.number}"

    @property
    def current(self):
        return next((v for v in self.versions if v.state == "current"), None)

    @property
    def pending(self):
        return next((v for v in self.versions if v.state == "pending"), None)


class ChapterVersion(Base):
    __tablename__ = "chapter_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(12))                     # current | pending | superseded
    review_scope: Mapped[str] = mapped_column(String(16), default="full")   # full | changed_pages
    verses_json: Mapped[str] = mapped_column(Text, default="{}")      # {"1 Nephi 1:1": "text", ...}
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    made_live_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    chapter: Mapped[Chapter] = relationship(back_populates="versions")
    pages: Mapped[list["Page"]] = relationship(back_populates="version", order_by="Page.number",
                                               cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("chapter_id", "number"),
        Index("uq_one_current_version", "chapter_id", unique=True, sqlite_where=text("state = 'current'"),
              postgresql_where=text("state = 'current'")),
        Index("uq_one_pending_version", "chapter_id", unique=True, sqlite_where=text("state = 'pending'"),
              postgresql_where=text("state = 'pending'")),
    )

    @property
    def verses(self):
        return json.loads(self.verses_json)


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("chapter_versions.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)                       # 0 is the cover page
    label: Mapped[str] = mapped_column(String(40), default="")
    image_hash: Mapped[str] = mapped_column(String(64))                # sha256 of the source PNG; also the media key
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    changed: Mapped[bool] = mapped_column(Boolean, default=True)       # differs from the previous live version
    version: Mapped[ChapterVersion] = relationship(back_populates="pages")
    panels: Mapped[list["Panel"]] = relationship(back_populates="page", order_by="Panel.id",
                                                 cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("version_id", "number"),)


class Panel(Base):
    __tablename__ = "panels"
    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(40))                       # "panel_003" or "cover", as in the run folder
    scene_id: Mapped[str] = mapped_column(String(40), default="")
    refs_label: Mapped[str] = mapped_column(String(120), default="")   # "1 Nephi 1:1-3"
    refs_json: Mapped[str] = mapped_column(Text, default="[]")
    x: Mapped[float] = mapped_column(Float)                            # frame as fractions of the page, 0..1
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    page: Mapped[Page] = relationship(back_populates="panels")

    @property
    def refs(self):
        return json.loads(self.refs_json)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("chapter_versions.id"))
    state: Mapped[str] = mapped_column(String(12), default="active")
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    active_seconds: Mapped[int] = mapped_column(Integer, default=0)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)     # editor removed it from coverage
    excluded_reason: Mapped[str] = mapped_column(String(300), default="")
    user: Mapped[User] = relationship()
    version: Mapped[ChapterVersion] = relationship()
    checks: Mapped[list["PageCheck"]] = relationship(cascade="all, delete-orphan")
    issues: Mapped[list["Issue"]] = relationship(back_populates="review", cascade="all, delete-orphan",
                                                 order_by="Issue.id")
    __table_args__ = (
        Index("uq_one_active_review_per_user", "user_id", unique=True, sqlite_where=text("state = 'active'"),
              postgresql_where=text("state = 'active'")),
        Index("uq_one_review_per_user_version", "user_id", "version_id", unique=True,
              sqlite_where=text("state IN ('active', 'submitted')"),
              postgresql_where=text("state IN ('active', 'submitted')")),
        Index("ix_reviews_version_state", "version_id", "state"),
    )


class PageCheck(Base):
    """A reviewer's explicit 'this page looks good'. A page with issues counts as checked without a row."""
    __tablename__ = "page_checks"
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id", ondelete="CASCADE"), primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id"), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Issue(Base):
    __tablename__ = "issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id", ondelete="CASCADE"))
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id"))
    panel_id: Mapped[int | None] = mapped_column(ForeignKey("panels.id"), nullable=True)
    x: Mapped[float | None] = mapped_column(Float, nullable=True)      # pin position as fractions of the page
    y: Mapped[float | None] = mapped_column(Float, nullable=True)
    category: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(12))
    description: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text, default="")
    verse_ref: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(12), default="open")
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    exported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    review: Mapped[Review] = relationship(back_populates="issues")
    page: Mapped[Page] = relationship()
    panel: Mapped[Panel | None] = relationship()
    events: Mapped[list["IssueEvent"]] = relationship(cascade="all, delete-orphan", order_by="IssueEvent.id")
    __table_args__ = (Index("ix_issues_status", "status"), Index("ix_issues_review", "review_id"))


class IssueEvent(Base):
    __tablename__ = "issue_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    from_status: Mapped[str] = mapped_column(String(12))
    to_status: Mapped[str] = mapped_column(String(12))
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[User | None] = relationship()


class AuditLog(Base):
    """Admin and editor actions that aren't issue status changes (opening chapters, going live, excluding reviews)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[User | None] = relationship()


class Meta(Base):
    """Tiny key/value table. 'claim_lock' is bumped to take the database write lock before choosing a chapter."""
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
