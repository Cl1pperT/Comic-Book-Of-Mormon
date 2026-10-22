"""Fixed vocabularies shared by the models, services and templates."""

ROLES = ("reviewer", "editor", "admin")           # each includes the powers of the ones before it
ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}

CATEGORIES = [
    ("scripture_accuracy", "Scripture / story accuracy", "The picture or words don't match what the verses say."),
    ("missing_content", "Missing content", "Something important from the verses isn't shown."),
    ("invented_content", "Invented or unsupported content", "Something is shown that the verses don't support."),
    ("character_consistency", "Character consistency", "A person looks different from how they look elsewhere."),
    ("location_continuity", "Location / visual continuity", "The place, clothing or objects don't match nearby panels."),
    ("art_error", "Art or AI-generation error", "Odd hands or faces, stray marks, modern objects, melted details."),
    ("dialogue_attribution", "Dialogue attribution", "The wrong person is speaking or being quoted."),
    ("spelling_grammar", "Spelling / grammar", "A typo or wording mistake in the lettering."),
    ("layout_readability", "Layout / readability", "Text is hard to read, cut off, or covers important art."),
    ("other", "Other", "Anything else you think should be fixed."),
]
CATEGORY_KEYS = {key for key, _, _ in CATEGORIES}
CATEGORY_LABELS = {key: label for key, label, _ in CATEGORIES}

SEVERITIES = [
    ("minor", "Minor", "Small flaw; most readers wouldn't notice."),
    ("moderate", "Moderate", "Noticeable and worth fixing."),
    ("major", "Major", "Clearly wrong or distracting."),
    ("blocking", "Blocking", "Must be fixed before the book is shared (e.g. contradicts scripture)."),
]
SEVERITY_KEYS = [key for key, _, _ in SEVERITIES]
SEVERITY_LABELS = {key: label for key, label, _ in SEVERITIES}
SEVERITY_RANK = {key: rank for rank, key in enumerate(SEVERITY_KEYS)}

ISSUE_STATUSES = [
    ("open", "Open"), ("accepted", "Accepted"), ("fixed", "Fixed"), ("verified", "Verified"),
    ("rejected", "Rejected"), ("duplicate", "Duplicate"),
]
ISSUE_STATUS_KEYS = [key for key, _ in ISSUE_STATUSES]
ISSUE_STATUS_LABELS = dict(ISSUE_STATUSES)

REVIEW_STATES = ("active", "submitted", "expired", "skipped", "released")
CHAPTER_STATUSES = ("closed", "open", "retired")
VERSION_STATES = ("current", "pending", "superseded")
REVIEW_SCOPES = ("full", "changed_pages")

COVER_KEY = "cover"
