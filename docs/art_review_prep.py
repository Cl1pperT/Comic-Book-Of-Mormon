import json, sys, os, glob
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, '.')
from bom_comic.comic import frames
from bom_comic.models import Panel

SP = Path(r"C:\Users\Johnc\AppData\Local\Temp\claude\c--Users-Johnc-Desktop-ComicBOM\368b2364-e9ab-4845-92e9-164d870cc52e\scratchpad\pg")


def chapters():
    out = []
    for m in glob.glob('runs/book/**/final/manifest.json', recursive=True):
        run = Path(m).parent.parent
        if 'archive' in run.parts:
            continue
        st = json.loads((run / 'status.json').read_text(encoding='utf-8'))
        out.append(((st['book'], st['chapter']), run.as_posix()))
    return out


def prep(run, W=1050, chars=True):
    run = Path(run)
    man = json.loads((run / 'final/manifest.json').read_text(encoding='utf-8'))
    panels = [Panel.model_validate(p) for p in man['panels']]
    lay = frames(panels)
    bypage = {}
    for p in panels:
        bypage.setdefault(p.page, []).append(p)
    cpath = run / 'continuity/characters.json'
    cd = json.loads(cpath.read_text(encoding='utf-8')) if cpath.exists() else {}
    tag = '_'.join(run.parts[-2:])
    files = []
    font = ImageFont.truetype('arial.ttf', 20)
    cov = run / 'pages/page_000.png'
    if cov.exists():
        im = Image.open(cov).convert('RGB')
        s = W / im.width
        im = im.resize((W, int(im.height * s)))
        fn = SP / f'{tag}_p0.jpg'
        im.save(fn, quality=80)
        files.append(str(fn))
    for n in sorted(bypage):
        im = Image.open(run / f'pages/page_{n:03d}.png').convert('RGB')
        s = W / im.width
        im = im.resize((W, int(im.height * s)))
        d = ImageDraw.Draw(im)
        for p in bypage[n]:
            x, y, w, h = [v * s for v in lay[p.panel_id]]
            t = p.panel_id.split('_')[1]
            d.rectangle([x + 3, y + 3, x + 40, y + 26], fill=(255, 255, 0))
            d.text((x + 6, y + 4), t, fill=(0, 0, 0), font=font)
        fn = SP / f'{tag}_p{n}.jpg'
        im.save(fn, quality=80)
        files.append(str(fn))
        for p in sorted(bypage[n], key=lambda p: p.panel_number):
            print(f'[p{n}] {p.panel_id[6:]} {p.shot} vis={p.characters_visible} loc={p.location} :: {p.action[:150]}')
    print('FILES')
    for f in files:
        print(f)
    if chars:
        names = set(c for p in panels for c in p.characters_visible)
        seenf = SP.parent / 'seen.txt'
        seen = set(seenf.read_text(encoding='utf-8').splitlines()) if seenf.exists() else set()
        for k, v in cd.items():
            if isinstance(v, dict) and (k in names or v.get('name') in names) and k not in seen:
                print('CHAR', k, '|', ' '.join(v.get('visual_design_choices', []))[:260])
                seen.add(k)
        seenf.write_text('\n'.join(sorted(seen)), encoding='utf-8')


if __name__ == '__main__':
    if sys.argv[1] == 'list':
        for i, (k, r) in enumerate(sorted(chapters())):
            print(i, k, r)
    else:
        prep(sys.argv[1], chars=len(sys.argv) < 3)
