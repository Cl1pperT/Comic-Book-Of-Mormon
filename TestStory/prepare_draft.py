"""Editorial preparation of this run; keeps source facts and exact excerpts."""
import re
from bom_comic.storage import Store
from bom_comic.models import Scene, Claim, Speech, Verse
from bom_comic.analysis import deterministic_issues
from bom_comic.scripture import coordinate

store = Store('runs/full-story')
scenes = [Scene.model_validate(s) for s in store.read('scenes.json')]
verses = [Verse.model_validate(v) for v in store.read('source.json')]
source = {v.ref: v.text for v in verses}

# Keep explicit excerpt fragments, never add ellipsis punctuation to scripture.
for s in scenes:
    for field in ('narration', 'spoken_dialogue'):
        fixed = []
        for q in getattr(s, field):
            for part in re.split(r'\.{3}|…', q.text):
                part = part.strip()
                if not part:
                    continue
                refs = [r for r in q.refs if part in source[r]]
                if not refs:
                    refs = q.refs
                data = q.model_dump()
                data.update(text=part, refs=refs)
                fixed.append(type(q).model_validate(data))
        setattr(s, field, fixed)

# Consolidate consecutive sections of off-screen teaching; never join distinct times/events.
groups = [(1, 6, 'The unseen voice recounts the destroyed cities'),
          (7, 8, 'Waters, hills, and valleys'), (9, 12, 'The voice explains the judgments'),
          (13, 14, 'The invitation to repent'), (15, 18, 'Christ identifies himself through the voice'),
          (19, 20, 'A broken heart and a contrite spirit'), (21, 22, 'Come unto me and be saved')]
chapter9 = [s for s in scenes if all(coordinate(r)[0] == 9 for r in s.refs)]
merged = []
for lo, hi, title in groups:
    subset = [s for s in chapter9 if lo <= min(coordinate(r)[1] for r in s.refs) <= hi]
    if not subset:
        continue
    result = subset[0].model_copy(deep=True)
    result.title = title
    result.summary = ' '.join(s.summary for s in subset)
    result.refs = sorted({r for s in subset for r in s.refs}, key=coordinate)
    for field in ('explicit_facts', 'reasonable_visual_inferences', 'unspecified_visual_details',
                  'spoken_dialogue', 'narration', 'doctrinal_or_story_notes', 'prohibited_inventions'):
        combined = []
        for s in subset:
            for item in getattr(s, field):
                if item not in combined:
                    combined.append(item)
        setattr(result, field, combined)
    result.characters = []
    result.locations = ['The land during the three days of darkness']
    result.reasonable_visual_inferences = ['A black panel represents the absence of visible light while the voice is heard.']
    result.unspecified_visual_details = ['Separate speech boxes are added during assembly; the image is entirely black.']
    result.prohibited_inventions += ['No visible people, divine figures, flames, glow, stars, cities, or literal visualization of metaphors.']
    result.doctrinal_or_story_notes += ['Off-screen speech during the darkness; reported judgments are not new events occurring in this panel.']
    result.importance = 'normal'
    merged.append(result)
scenes = [s for s in scenes if s not in chapter9]
scenes.extend(merged)
scenes.sort(key=lambda s: coordinate(s.refs[0]))

# Concise exact excerpts selected for the seven chapter-nine speech panels.
excerpts = {
  1: [(1, 'And it came to pass that there was a voice heard among all the inhabitants of the earth, upon all the face of this land, crying:'),
      (3, 'Behold, that great city Zarahemla have I burned with fire, and the inhabitants thereof.'),
      (4, 'And behold, that great city Moroni have I caused to be sunk in the depths of the sea,')],
  7: [(7, 'Yea, and the city of Onihah and the inhabitants thereof, and the city of Mocum and the inhabitants thereof, and the city of Jerusalem and the inhabitants thereof; and waters have I caused to come up in the stead thereof,'),
      (8, 'all these have I caused to be sunk, and made hills and valleys in the places thereof;')],
  9: [(9, 'And behold, that great city Jacobugath, which was inhabited by the people of king Jacob, have I caused to be burned with fire because of their sins and their wickedness,'),
      (12, 'And many great destructions have I caused to come upon this land, and upon this people, because of their wickedness and their abominations.')],
  13: [(13, 'will ye not now return unto me, and repent of your sins, and be converted, that I may heal you?'),
       (14, 'Behold, mine arm of mercy is extended towards you, and whosoever will come, him will I receive;')],
  15: [(15, 'Behold, I am Jesus Christ the Son of God.'),
       (17, 'for behold, by me redemption cometh, and in me is the law of Moses fulfilled.'),
       (18, 'I am the light and the life of the world. I am Alpha and Omega, the beginning and the end.')],
  19: [(19, 'And ye shall offer up unto me no more the shedding of blood;'),
       (20, 'And ye shall offer for a sacrifice unto me a broken heart and a contrite spirit.'),
       (20, 'And whoso cometh unto me with a broken heart and a contrite spirit, him will I baptize with fire and with the Holy Ghost,')],
  21: [(21, 'Behold, I have come unto the world to bring redemption unto the world, to save the world from sin.'),
       (22, 'therefore repent, and come unto me ye ends of the earth, and be saved.')]
}
for s in scenes:
    if coordinate(s.refs[0])[0] == 9:
        start = coordinate(s.refs[0])[1]
        s.narration = []
        s.spoken_dialogue = []
        for n, text in excerpts[start]:
            ref = f'3 Nephi 9:{n}'
            if text not in source[ref]:
                raise ValueError(f'Editorial excerpt does not exactly match {ref}: {text}')
            if n == 1:
                s.narration.append(Claim(text=text, refs=[ref]))
            else:
                s.spoken_dialogue.append(Speech(text=text, refs=[ref], speaker='Jesus Christ (voice only)'))
    # Prefer speech; remove verbose redundant narration when it would overcrowd a panel.
    while sum(len(q.text.split()) for q in s.spoken_dialogue+s.narration) > 65 and s.narration:
        s.narration.pop(0)
    # Pacing: reserve full pages for the first storm and the final introduction.
    s.importance = 'major' if s.refs[0] == '3 Nephi 8:5' or '3 Nephi 11:7' in s.refs else 'normal'
for i,s in enumerate(scenes,1):
    s.scene_id = f'scene_{i:03d}'
    issues = deterministic_issues(s, verses)
    if issues:
        print(s.scene_id, s.title, issues)
store.write('scenes.json', [s.model_dump() for s in scenes])
store.event('editorial_preparation', note='Consolidated consecutive chapter-nine speech scenes; exact source excerpts; no automatic approvals.')
print('Prepared',len(scenes),'scenes')
