from typing import List, Literal
from pydantic import BaseModel, ConfigDict, Field

class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Verse(Model):
    book: str = Field(min_length=1)
    chapter: int = Field(ge=1)
    verse: int = Field(ge=1)
    text: str = Field(min_length=1)
    @property
    def ref(self):
        return f"{self.book} {self.chapter}:{self.verse}"

class Claim(Model):
    text: str = Field(min_length=1)
    refs: List[str] = Field(min_length=1)

class Speech(Claim):
    speaker: str

class Scene(Model):
    scene_id: str = Field(pattern=r"^scene_[0-9]+$")
    title: str
    refs: List[str] = Field(min_length=1)
    summary: str
    characters: List[str] = []
    locations: List[str] = []
    explicit_facts: List[Claim] = Field(min_length=1)
    reasonable_visual_inferences: List[str] = []
    unspecified_visual_details: List[str] = []
    spoken_dialogue: List[Speech] = []
    narration: List[Claim] = []
    doctrinal_or_story_notes: List[str] = []
    prohibited_inventions: List[str] = []
    importance: Literal["normal", "major"] = "normal"

class SceneBatch(Model):
    scenes: List[Scene] = Field(min_length=1)

class Verdict(Model):
    status: Literal["PASS", "PASS WITH WARNINGS", "REJECT"]
    issues: List[str] = []

class Panel(Model):
    panel_id: str = Field(pattern=r"^panel_[0-9]+$")
    scene_id: str
    page: int = Field(ge=1)
    panel_number: int = Field(ge=1)
    refs: List[str]
    characters_visible: List[str]
    location: List[str]
    action: str
    shot: Literal["splash", "wide", "tall", "medium", "close"] = "medium"
    mood: str = "Reverent, serious"
    camera: str = "Wide establishing view"
    composition: str = "Clear focal point, restrained detail"
    dialogue: List[Speech]
    narration: List[Claim]
    visual_facts: List[Claim]
    visual_inferences: List[str]
    creative_details: List[str]
    prohibited: List[str]
